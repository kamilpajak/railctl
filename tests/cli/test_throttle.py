"""The four commands that change the layout's state: `power`, `stop`, `drive`, `function`.

NOTHING IN THIS FILE TOUCHES HARDWARE. Every test runs against `FakeStation`, a
recording stand-in for `railctl.station.Station`. What that proves is that this
CLI layer calls the facade in the documented order, with the documented
arguments, and refuses in the documented cases. It proves nothing about the
YD7010 or about a locomotive: a refusal asserted here is a refusal in our code,
not a train observed not moving. Each safety test below names the bench check
that would settle its half of the claim.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from railctl.cli import deps
from railctl.cli._errors import OutputContext
from railctl.cli.commands import power, throttle
from railctl.cli.deps import Settings
from railctl.errors import (
    FunctionGroupUnreadableError,
    LinkTimeout,
    ProtocolError,
    StationBusyError,
    StationError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
    XBusChecksumError,
)
from railctl.xbus.replies import LocoInfo, StationStatus
from railctl.xbus.speed import Direction

runner = CliRunner()
# No mix_stderr=False: the pinned Typer/Click raises TypeError for that argument.
# tests/cli/test_schema.py already runs CliRunner() plain and reads `.stderr`.

# Raw status bytes are fine in a test file - test_layering.py only scans
# src/railctl, not tests/. STATUS_EMERGENCY_STOP=0x01, STATUS_EMERGENCY_OFF=0x02,
# STATUS_AUTO_START=0x04, STATUS_SERVICE_MODE=0x08 (railctl/xbus/replies.py).
# Bits 0 and 1 are the MEASURED order on this hardware, the reverse of the Lenz
# spec - see docs/probe-results.md. These constants are built through
# `from_raw`, never by naming the flags, so a future correction to that mapping
# reaches this file without an edit.
CLEAR_STATUS = StationStatus.from_raw(0x00)
AUTO_START_STATUS = StationStatus.from_raw(0x04)
EMERGENCY_STOP_STATUS = StationStatus.from_raw(0x01)
EMERGENCY_OFF_STATUS = StationStatus.from_raw(0x02)
SERVICE_MODE_STATUS = StationStatus.from_raw(0x08)

LOCO_128 = LocoInfo(
    raw_ident=0b10000100,
    raw_speed=0x8F,
    speed_steps=128,
    in_use_by_other=False,
    function_bits=(False,) * 13,
    speed=14,
    direction=Direction.FORWARD,
    emergency_stopped=False,
)
LOCO_14_STEP = LocoInfo(
    raw_ident=0b00000000,
    raw_speed=0x07,
    speed_steps=14,
    in_use_by_other=False,
    function_bits=(False,) * 13,
)
LOCO_STANDING = replace(LOCO_128, speed=0)

#: Every exception a `loco_info` request can fail with that is NOT a
#: `StationError`. Each one is raised by `Station.exchange` or below it, and
#: each subclasses `RailctlError` directly - which is why a `except
#: StationError` around the cosmetic pre-read let all five abort a command
#: whose own telegram had not been sent yet. Two of them describe a WORKING
#: link: `61 82` is the station refusing this one request, and a checksum fault
#: is one garbled reply frame.
#: What `Station._function_set_group_path` says when it will not blind-write a
#: group whose other bits it has not read.
GROUP_UNREADABLE_MESSAGE = "F2 shares group G1 with F[1], whose state has not been read"

COSMETIC_READ_FAILURES = [
    LinkTimeout("no reply to the loco-info request within 0.5 s"),
    TransportError("the port went away mid-exchange"),
    ProtocolError("the reply was well framed and did not parse"),
    XBusChecksumError("the trailing XOR byte does not match the telegram body"),
    UnsupportedCommandError("station answered 61 82 to the loco-info request"),
]


class FakeStation:
    """A stand-in for `railctl.station.Station`. Records every call so a test
    can assert both the return value and the ORDER calls happened in - the
    power-on test below depends on order, not just on which methods ran."""

    def __init__(
        self,
        *,
        status: StationStatus = CLEAR_STATUS,
        loco_info: LocoInfo | None = LOCO_128,
        loco_info_raises: BaseException | None = None,
        function_toggle_result: bool = True,
        function_raises: bool = False,
        function_post_write_error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._status = status
        self._loco_info = loco_info
        self._loco_info_raises = loco_info_raises
        self._function_toggle_result = function_toggle_result
        self._function_raises = function_raises
        self._function_post_write_error = function_post_write_error

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    @property
    def call_names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]

    def status(self) -> StationStatus:
        self._record("status")
        return self._status

    def power_on(self) -> None:
        self._record("power_on")

    def power_off(self) -> None:
        self._record("power_off")

    def emergency_stop(self, address: int | None = None) -> None:
        self._record("emergency_stop", address=address)

    def drive(self, address: int, speed: int, direction: Direction) -> None:
        self._record("drive", address, speed, direction)

    def loco_info(self, address: int) -> LocoInfo:
        self._record("loco_info", address)
        if self._loco_info_raises is not None:
            raise self._loco_info_raises
        if self._loco_info is None:
            raise StationError(f"no loco info available for {address}")
        return self._loco_info

    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None:
        self._record("function_set", address, function, on, force_group=force_group)
        if self._function_raises and not force_group:
            # `hint=` exactly as the real facade sets it on the two raises that
            # mean "the current group could not be read". A fake that omitted it
            # would let the CLI's wrapper fire on failures the facade marks
            # differently, which is the defect this argument now pins.
            raise StationError(GROUP_UNREADABLE_MESSAGE, hint="--force-group")
        if self._function_post_write_error is not None:
            raise self._function_post_write_error

    def function_toggle(self, address: int, function: int, *, force_group: bool = False) -> bool:
        self._record("function_toggle", address, function, force_group=force_group)
        if self._function_raises and not force_group:
            raise StationError(GROUP_UNREADABLE_MESSAGE, hint="--force-group")
        if self._function_post_write_error is not None:
            raise self._function_post_write_error
        return self._function_toggle_result

    def close(self) -> None:
        self._record("close")


def _settings(*, address: int | None = 3, fmt: str = "human", yes: bool = False) -> Settings:
    return Settings(
        target="fake:test",
        address=address,
        fmt=fmt,  # type: ignore[arg-type]
        verbose=0,
        color="auto",
        assume_yes=yes,
        interactive=True,
    )


@dataclass(frozen=True, slots=True)
class _FakeCliContext:
    """Stands in for `railctl.cli.main.CliContext` - importing the real class
    would import `railctl.cli.main`, which imports `power` and `throttle` back
    to call `register(app)`, and a test module importing its own subject
    modules' importer is the cycle every command module already avoids."""

    settings: Settings
    output: OutputContext


def _app(
    station: FakeStation,
    monkeypatch: pytest.MonkeyPatch,
    *,
    address: int | None = 3,
    fmt: str = "human",
) -> typer.Typer:
    def _open(_settings: Settings, *, capabilities_path: Any = None) -> FakeStation:
        return station

    monkeypatch.setattr(power, "open_station", _open)
    monkeypatch.setattr(throttle, "open_station", _open)
    monkeypatch.setattr(power, "capabilities_path", lambda: None)
    monkeypatch.setattr(throttle, "capabilities_path", lambda: None)

    app = typer.Typer()
    settings = _settings(address=address, fmt=fmt)

    @app.callback()
    def _root(ctx: typer.Context) -> None:
        # sys.stdout/sys.stderr are read HERE, inside the callback body, not
        # captured in the enclosing _app() scope - CliRunner.invoke() swaps
        # them in only once app() actually dispatches, which happens after
        # _app() has already returned. A reference captured earlier would point
        # at the real, un-swapped streams and every stderr/stdout assertion
        # below would silently see an empty buffer for the wrong reason.
        ctx.obj = _FakeCliContext(
            settings=settings,
            output=OutputContext(
                fmt=settings.fmt,
                stdout_color=False,
                stderr_color=False,
                stdout=sys.stdout,
                stderr=sys.stderr,
            ),
        )

    power.register(app)
    throttle.register(app)
    return app


# --- preflight: three refusal conditions, one pass-through ---
#
# BENCH CHECK THESE STAND IN FOR: with the layout powered down (or after 80 80),
# `railctl drive 30 --address 3` must leave the locomotive standing when power
# is restored. Only a person watching the track can confirm that; these four
# only prove the CLI refuses on the three status bits the spec names.


def test_preflight_returns_status_when_track_is_clear():
    station = FakeStation(status=CLEAR_STATUS)
    assert throttle.preflight(station, speed=30) is CLEAR_STATUS


def test_preflight_refuses_when_emergency_off():
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    with pytest.raises(TrackPowerError):
        throttle.preflight(station, speed=30)


def test_preflight_refuses_when_emergency_stop():
    station = FakeStation(status=EMERGENCY_STOP_STATUS)
    with pytest.raises(TrackPowerError):
        throttle.preflight(station, speed=30)


def test_preflight_refuses_when_service_mode_active():
    station = FakeStation(status=SERVICE_MODE_STATUS)
    with pytest.raises(StationBusyError):
        throttle.preflight(station, speed=None)


def test_preflight_names_the_speed_it_refused_when_there_is_one():
    """`speed` only phrases the refusal, so both phrasings need a caller: a
    message naming "speed None" would be this project's own error text going
    the way of a capability recorded by a broken instrument."""
    with pytest.raises(TrackPowerError, match="speed 30"):
        throttle.preflight(FakeStation(status=EMERGENCY_OFF_STATUS), speed=30)
    with pytest.raises(TrackPowerError, match="this command"):
        throttle.preflight(FakeStation(status=EMERGENCY_OFF_STATUS), speed=None)


# --- parse_function / parse_state ---


def test_parse_function_accepts_f_prefixed_bare_number_and_light_alias():
    assert throttle.parse_function("f2") == 2
    assert throttle.parse_function("2") == 2
    assert throttle.parse_function("light") == 0
    assert throttle.parse_function(" LIGHTS ") == 0


def test_parse_function_rejects_out_of_range_and_non_numeric():
    for token in ("29", "f29", "xyz"):
        with pytest.raises(ValueError, match=r"0\.\.28"):
            throttle.parse_function(token)


def test_parse_state_defaults_to_on_when_omitted():
    assert throttle.parse_state(None) == "on"


def test_parse_state_accepts_and_rejects():
    assert throttle.parse_state("off") == "off"
    assert throttle.parse_state("toggle") == "toggle"
    with pytest.raises(ValueError):
        throttle.parse_state("sideways")


# --- build_drive ---


def test_build_drive_direction_is_a_word_never_a_wire_value():
    result = throttle.build_drive(
        3, 30, Direction.FORWARD, was=LOCO_128, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["direction"] == "forward"
    result = throttle.build_drive(
        3, 20, Direction.REVERSE, was=LOCO_128, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["direction"] == "reverse"


def test_build_drive_changed_true_when_speed_or_direction_differs():
    was = replace(LOCO_128, speed=10, direction=Direction.FORWARD)
    result = throttle.build_drive(
        3, 30, Direction.FORWARD, was=was, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["changed"] is True


def test_build_drive_changed_false_when_nothing_differs():
    was = replace(LOCO_128, speed=30, direction=Direction.FORWARD)
    result = throttle.build_drive(
        3, 30, Direction.FORWARD, was=was, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["changed"] is False


def test_build_drive_changed_unknown_when_prior_state_unavailable():
    result = throttle.build_drive(
        3, 30, Direction.FORWARD, was=None, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["changed"] is None
    assert result.result["previous_speed_decoded"] is None
    assert any("could not be read" in line for line in result.lines)


def test_build_drive_changed_unknown_when_prior_step_mode_not_decoded():
    """LOCO_14_STEP.speed is None because speed.py only defines the 128-step
    layout - replies.py leaves it undecoded rather than guess. Reporting
    `changed` here would mean comparing a number against a layout railctl never
    decoded, which is exactly the "recorded absent by a defective instrument"
    failure this project exists to avoid."""
    result = throttle.build_drive(
        3, 30, Direction.FORWARD, was=LOCO_14_STEP, direction_source=throttle.DIRECTION_KEPT
    )
    assert result.result["changed"] is None
    assert result.result["previous_speed_decoded"] is False
    assert any("not decoded" in line for line in result.lines)


def test_build_drive_keeps_the_three_outcomes_apart_in_the_human_text():
    # true / false / unknown must stay distinguishable in the human rendering
    # too, not only in the JSON - that is the project's whole rule, applied to
    # `changed`.
    yes = throttle.build_drive(
        3,
        30,
        Direction.FORWARD,
        was=replace(LOCO_128, speed=10),
        direction_source=throttle.DIRECTION_KEPT,
    )
    no = throttle.build_drive(
        3,
        30,
        Direction.FORWARD,
        was=replace(LOCO_128, speed=30),
        direction_source=throttle.DIRECTION_KEPT,
    )
    unknown = throttle.build_drive(
        3, 30, Direction.FORWARD, was=None, direction_source=throttle.DIRECTION_KEPT
    )
    assert "yes changed" in yes.lines[0]
    assert "no changed" in no.lines[0]
    assert "unknown changed" in unknown.lines[0]


# --- build_function ---


def test_build_function_reports_requested_state_and_resulting_bit():
    result = throttle.build_function(3, 2, "on", now_on=True)
    assert result.result == {"address": 3, "function": 2, "requested": "on", "now_on": True}
    assert result.schema == throttle.FUNCTION_SCHEMA
    assert result.command == "function"
    assert "F2 is now on" in result.lines[0]


def test_build_function_says_off_when_the_resulting_bit_is_off():
    result = throttle.build_function(3, 2, "toggle", now_on=False)
    assert result.result["now_on"] is False
    assert "F2 is now off" in result.lines[0]


# --- build_power / build_stop ---


def test_build_power_reports_manual_start_mode_when_bit_2_is_clear():
    result = power.build_power("on", CLEAR_STATUS, changed=True, idled=None)
    assert result.result["auto_start_mode"] is False
    assert any("manual" in line for line in result.lines)
    assert not any("speed 0" in line for line in result.lines)


def test_build_power_reports_the_track_as_off_and_unchanged():
    result = power.build_power("off", EMERGENCY_OFF_STATUS, changed=False, idled=None)
    assert result.result["track_power"] is False
    assert "track power is off (no change)" in result.lines[0]


def test_build_power_names_the_direction_the_idle_telegram_carried():
    idled = power.Idled(address=3, direction=Direction.REVERSE, direction_preserved=True)
    result = power.build_power("on", AUTO_START_STATUS, changed=True, idled=idled)
    assert result.result["idled_address"] == 3
    assert result.result["idled_direction"] == "reverse"
    assert result.result["idled_direction_preserved"] is True
    assert "loco 3 was sent speed 0 reverse" in result.lines[-1]
    assert result.warnings == []


def test_build_power_warns_when_the_idle_telegram_may_have_changed_the_direction():
    """`power on` writes to a locomotive. When it could not read which way that
    locomotive was pointing, the forward it sent is a choice, not a copy."""
    idled = power.Idled(address=3, direction=Direction.FORWARD, direction_preserved=False)
    result = power.build_power("on", AUTO_START_STATUS, changed=True, idled=idled)
    assert result.result["idled_direction_preserved"] is False
    assert [w.name for w in result.warnings] == ["direction_not_preserved"]
    assert result.warnings[0].details == {"address": 3, "sent": "forward"}


def test_build_stop_reports_scope_all_when_no_address_is_given():
    result = power.build_stop(None)
    assert result.result == {"address": None, "scope": "all"}
    assert "all locomotives stopped" in result.lines[0]


# --- drive: direction default, preflight wiring, notice, global options ---


def test_drive_keeps_current_direction_when_reverse_not_given(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, direction=Direction.REVERSE))
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    assert station.calls[-2] == ("drive", (3, 30, Direction.REVERSE), {})


def test_drive_reverse_flag_overrides_current_direction(monkeypatch):
    station = FakeStation(loco_info=LOCO_128)  # currently forward
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "20", "--reverse"])
    assert result.exit_code == 0
    assert station.calls[-2] == ("drive", (3, 20, Direction.REVERSE), {})


def test_drive_refuses_a_positive_speed_when_the_direction_was_never_decoded(monkeypatch):
    """The founding rule at the one place where it moves a train.

    LOCO_14_STEP has direction None because replies.py decodes only the
    128-step layout. Answering FORWARD here sent an undecoded value to the
    track as a measured one, and a locomotive already running in reverse
    reversed on the spot.

    BENCH CHECK: put a decoder in 28-step mode, run it in reverse, and run
    `railctl drive 40 --address 3`. Nothing may move. This test only shows that
    no `drive` call was made.
    """
    station = FakeStation(loco_info=LOCO_14_STEP)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 2
    assert "drive" not in station.call_names
    error = json.loads(result.stderr)
    assert error["code"] == "usage"
    assert error["details"] == {"reason": "direction_undecoded", "speed_steps": 14}
    assert "14 speed steps" in error["message"]
    assert error["suggestions"] == [
        ["railctl", "drive", "30", "--address", "3", "--forward"],
        ["railctl", "drive", "30", "--address", "3", "--reverse"],
    ]


def test_drive_refuses_a_positive_speed_when_the_locomotive_cannot_be_read(monkeypatch):
    station = FakeStation(loco_info=None)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 2
    assert "drive" not in station.call_names
    error = json.loads(result.stderr)
    assert error["details"] == {"reason": "direction_unread", "speed_steps": None}


@pytest.mark.parametrize("flag", ["--forward", "--reverse"])
def test_the_direction_flags_are_what_the_refusal_asks_for_and_they_work(monkeypatch, flag):
    """A refusal that names a flag the CLI does not accept is a dead end.

    `--forward` exists only because of the refusal above: with `--reverse` the
    only spelling, there was no runnable answer that meant forward.
    """
    station = FakeStation(loco_info=LOCO_14_STEP)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30", flag])
    assert result.exit_code == 0, result.stderr
    expected = Direction.FORWARD if flag == "--forward" else Direction.REVERSE
    assert station.calls[-2] == ("drive", (3, 30, expected), {})


def test_forward_and_reverse_together_are_refused_before_a_station_is_opened(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30", "--forward", "--reverse"])
    assert result.exit_code == 2
    assert station.calls == []
    error = json.loads(result.stderr)
    assert error["details"] == {"reason": "contradictory_direction_flags"}


def test_the_stop_says_it_chose_forward_without_reading_rather_than_claiming_it_kept_one(
    monkeypatch,
):
    """`drive 0` reads nothing, so "forward" in its envelope is a choice, not a
    measurement, and `direction_source` is what keeps the two apart."""
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "0"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["direction"] == "forward"
    assert payload["result"]["direction_source"] == "stop-default"

    human = runner.invoke(_app(FakeStation(), monkeypatch), ["drive", "0"])
    assert "without reading the locomotive first" in human.stdout


def test_a_typed_direction_is_reported_as_typed_not_as_kept(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "0", "--reverse"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["direction"] == "reverse"
    assert payload["result"]["direction_source"] == "flag"


def test_a_kept_direction_is_reported_as_kept(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, direction=Direction.REVERSE))
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["direction_source"] == "kept"


def test_drive_positive_speed_refuses_on_emergency_off(monkeypatch):
    # BENCH CHECK: cut track power, run this, restore power, and watch that the
    # locomotive stays still. Only that settles whether the refusal actually
    # keeps a speed out of the station's refresh buffer.
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 20
    assert "drive" not in station.call_names


def test_drive_positive_speed_refuses_on_emergency_stop(monkeypatch):
    station = FakeStation(status=EMERGENCY_STOP_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 20
    assert "drive" not in station.call_names


def test_drive_positive_speed_refuses_on_service_mode(monkeypatch):
    station = FakeStation(status=SERVICE_MODE_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 12
    assert "drive" not in station.call_names


def test_drive_zero_skips_preflight_and_is_always_sent(monkeypatch):
    # A stop that needs permission is not a stop. The station here reports
    # emergency off - the state that refuses every positive speed above - and
    # speed 0 must still go out, with no status read at all.
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "0"])
    assert result.exit_code == 0
    assert "status" not in station.call_names
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})


@pytest.mark.parametrize("failure", COSMETIC_READ_FAILURES, ids=lambda exc: type(exc).__name__)
def test_drive_zero_is_sent_even_when_the_loco_info_read_fails(monkeypatch, failure):
    """The stop, driven against a station whose `loco_info` fails five ways.

    Every one of these used to abort the command before `station.drive` was
    reached, because the pre-read ran ahead of the `speed > 0` guard and was
    wrapped in `except StationError`, which none of these five is. Two of them
    describe a working link and a healthy track.

    BENCH CHECK THIS STANDS IN FOR: unplug nothing, put the decoder in a state
    where the station refuses the loco-info request, run `railctl drive 0` and
    watch the locomotive stop. Only that settles whether the telegram this test
    sees in a call list reaches the track.
    """
    station = FakeStation(status=EMERGENCY_OFF_STATUS, loco_info_raises=failure)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "0"])
    assert result.exit_code == 0, result.stderr
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})


def test_drive_zero_reads_nothing_at_all_before_sending_the_stop(monkeypatch):
    """Not just "the read may fail" - the stop path does not make the read.

    A pre-read that cannot veto is still a round trip the panic command waits
    on, and `LinkTimeout` costs the whole reply budget before it gives up.
    """
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "0"])
    assert result.exit_code == 0
    assert station.call_names == ["drive", "close"]


@pytest.mark.parametrize("failure", COSMETIC_READ_FAILURES, ids=lambda exc: type(exc).__name__)
def test_a_failed_cosmetic_read_never_vetoes_a_positive_speed_either(monkeypatch, failure):
    """The other half of the same fix: the widened catch.

    `drive 0` proves nothing about it now that the stop path makes no read at
    all, so this drives a speed that DOES read. The direction is typed, so the
    only thing the unreadable reply could still decide is whether the command
    runs - and it must not.
    """
    station = FakeStation(loco_info_raises=failure)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30", "--reverse"])
    assert result.exit_code == 0, result.stderr
    assert station.calls[-2] == ("drive", (3, 30, Direction.REVERSE), {})


def test_drive_reads_the_status_before_the_locomotive(monkeypatch):
    """Order matters for which refusal an operator is shown.

    With the pre-read first, a station that refuses every request answered the
    loco-info request first, the CLI swallowed that refusal as UNKNOWN, and the
    operator was told about a direction rather than about the station.
    """
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    assert station.call_names == ["status", "loco_info", "drive", "close"]


def test_drive_prints_running_notice_on_stderr_only_for_nonzero_speed(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    moving = runner.invoke(app, ["drive", "30"])
    assert moving.exit_code == 0
    assert "loco 3 is running at step 30 forward" in moving.stderr
    assert "loco 3 is running" not in moving.stdout

    stopped = runner.invoke(app, ["drive", "0"])
    assert stopped.exit_code == 0
    assert "is running" not in stopped.stderr


def test_drive_prints_running_notice_on_stderr_in_json_mode(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    assert "loco 3 is running at step 30 forward" in result.stderr
    payload = json.loads(result.stdout)
    assert "is running" not in json.dumps(payload)


def test_drive_without_an_address_is_a_usage_error_naming_the_fix(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, address=None, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 2
    assert "drive" not in station.call_names
    error = json.loads(result.stderr)
    assert error["suggestions"] == [["railctl", "drive", "30", "--address", "3"]]


def test_drive_accepts_the_global_address_option_after_the_subcommand(monkeypatch):
    """The global-option-position pin (the spec's own worked example): Click
    only parses a Typer group's callback options BEFORE the subcommand name, so
    this only works because drive_cmd redeclares --address itself via
    global_option("--address") and merges it with merged_output - without that,
    this invocation would exit 2."""
    station = FakeStation()
    app = _app(station, monkeypatch, address=None)  # no address configured globally
    result = runner.invoke(app, ["drive", "30", "--address", "3"])
    assert result.exit_code == 0
    assert station.calls[-2] == ("drive", (3, 30, Direction.FORWARD), {})


def test_drive_format_option_after_the_subcommand_overrides_the_configured_default(monkeypatch):
    """Exercises the OTHER half of the same mechanism: --format typed on drive
    itself must rebuild the OutputContext used for THIS invocation rather than
    the one main.py's callback built from the group-level default, or
    `drive --format json` here would print human text."""
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="human")
    result = runner.invoke(app, ["drive", "0", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["speed"] == 0


# --- function: facade choice, preflight, force-group suggestion ---


def test_function_toggle_uses_function_toggle_facade(monkeypatch):
    station = FakeStation(function_toggle_result=True)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "toggle"])
    assert result.exit_code == 0
    assert ("function_toggle", (3, 2), {"force_group": False}) in station.calls
    assert not any(name == "function_set" for name in station.call_names)


def test_function_on_off_uses_function_set_facade(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "light", "off"])
    assert result.exit_code == 0
    assert ("function_set", (3, 0, False), {"force_group": False}) in station.calls


def test_function_defaults_to_on_when_no_state_is_typed(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2"])
    assert result.exit_code == 0
    assert ("function_set", (3, 2, True), {"force_group": False}) in station.calls


def test_function_force_group_reaches_the_facade(monkeypatch):
    station = FakeStation(function_raises=True)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on", "--force-group"])
    assert result.exit_code == 0
    assert ("function_set", (3, 2, True), {"force_group": True}) in station.calls


def test_function_refuses_on_service_mode(monkeypatch):
    # BENCH CHECK: start a service-mode read on the station and confirm the
    # station itself is what a real refusal comes from; here only our own
    # status decode is exercised.
    station = FakeStation(status=SERVICE_MODE_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 12
    assert not any(name in ("function_set", "function_toggle") for name in station.call_names)


def test_function_refuses_on_emergency_off(monkeypatch):
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 20
    assert not any(name in ("function_set", "function_toggle") for name in station.call_names)


def test_function_rejects_a_token_that_is_not_a_function(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "sideways", "on"])
    assert result.exit_code == 2
    assert station.calls == []  # refused before a station was ever opened


def test_function_suggests_force_group_when_state_cannot_be_read(monkeypatch):
    station = FakeStation(function_raises=True)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 9
    error = json.loads(result.stderr)
    assert error["code"] == "function_group_unreadable"
    assert error["suggestions"][0] == [
        "railctl",
        "function",
        "f2",
        "on",
        "--address",
        "3",
        "--force-group",
    ]


@pytest.mark.parametrize("state", ["on", "toggle"])
def test_a_failure_after_the_group_telegram_is_not_reported_as_a_failed_read(monkeypatch, state):
    """`Station._expect_ack` raises a bare `StationError` AFTER the group
    telegram has gone out. The old `except StationError` wrapped the whole
    write, so that arrived as "could not read the current state of F2" with a
    `--force-group` retry that skips a read which had already happened.

    The facade marks the two raises that really do mean "the group could not be
    read" with `hint="--force-group"`; anything else passes through as itself.
    """
    station = FakeStation(
        function_post_write_error=StationError("expected the generic ack, got Other(...)")
    )
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", state])
    assert result.exit_code == 9
    error = json.loads(result.stderr)
    assert error["code"] == "station"
    assert "could not read" not in error["message"]
    assert error["suggestions"] == []


@pytest.mark.parametrize("failure", COSMETIC_READ_FAILURES, ids=lambda exc: type(exc).__name__)
def test_the_post_action_read_can_never_change_the_functions_verdict(monkeypatch, failure):
    """The function IS set by the time that read happens.

    Reporting failure there tells a caller to retry, and the retry toggles the
    function back. BENCH CHECK: only watching the headlight through a retry
    settles that; this reads an exit code.
    """
    station = FakeStation(loco_info_raises=failure)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0, result.stderr
    assert ("function_set", (3, 2, True), {"force_group": False}) in station.calls
    assert json.loads(result.stdout)["result"]["now_on"] is True


def test_function_toggle_that_cannot_read_the_group_suggests_the_same_retry(monkeypatch):
    station = FakeStation(function_raises=True)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "toggle"])
    assert result.exit_code == 9
    error = json.loads(result.stderr)
    assert error["suggestions"][0][-1] == "--force-group"
    assert "toggle" in error["suggestions"][0]


def test_function_warns_on_stderr_when_the_locomotive_is_running(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, speed=30, direction=Direction.REVERSE))
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "loco 3 is running at step 30 reverse" in result.stderr


def test_function_says_the_direction_is_unknown_rather_than_guessing_forward(monkeypatch):
    # A 14-step reply carries no decoded direction. Printing "forward" here
    # would be a decode this tool never performed, reported as a fact.
    station = FakeStation(loco_info=replace(LOCO_14_STEP, speed=30))
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "loco 3 is running at step 30 unknown direction" in result.stderr


def test_function_says_the_running_state_is_unknown_for_an_undecoded_speed(monkeypatch):
    """`speed is None` is UNKNOWN, not zero.

    The guard was `not info.speed`, which is True for both, so the notice was
    silently skipped for every 14/27/28-step decoder - the locomotive most
    likely to be moving unnoticed, since the same reply mode is why `drive`
    cannot read its direction either.
    """
    station = FakeStation(loco_info=LOCO_14_STEP)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "loco 3 is running: unknown" in result.stderr
    assert "14 speed steps" in result.stderr


def test_the_unknown_running_notice_never_invents_a_step_count(monkeypatch):
    # `speed_steps` is None when the ident byte names no mode this tool knows.
    station = FakeStation(loco_info=replace(LOCO_14_STEP, speed_steps=None))
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "an unrecognised number of speed steps" in result.stderr


def test_function_prints_no_running_notice_for_a_standing_locomotive(monkeypatch):
    station = FakeStation(loco_info=LOCO_STANDING)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "is running" not in result.stderr


def test_function_prints_no_running_notice_when_the_locomotive_cannot_be_read(monkeypatch):
    station = FakeStation(loco_info=None)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 0
    assert "is running" not in result.stderr


# --- human/json parity, one per command ---


def test_drive_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, speed=10, direction=Direction.FORWARD))
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    payload = json.loads(result.stdout)
    assert payload["result"]["address"] == 3
    assert payload["result"]["speed"] == 30
    assert payload["result"]["direction"] == "forward"
    assert payload["result"]["changed"] is True

    station_human = FakeStation(loco_info=replace(LOCO_128, speed=10, direction=Direction.FORWARD))
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["drive", "30"])
    joined = human.stdout
    assert "3" in joined and "30" in joined and "forward" in joined


def test_function_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(function_toggle_result=True)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "toggle"])
    payload = json.loads(result.stdout)
    assert payload["result"] == {
        "address": 3,
        "function": 2,
        "requested": "toggle",
        "now_on": True,
    }

    station_human = FakeStation(function_toggle_result=True)
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["function", "f2", "toggle"])
    assert "3" in human.stdout and "F2" in human.stdout and "on" in human.stdout


# --- power ---


def test_power_on_runs_stop_all_then_power_on_then_status_then_idles_address(monkeypatch):
    # BENCH CHECK: with a speed stored for address 3 and the station in
    # automatic start mode, `railctl power on` must leave the locomotive
    # standing. docs/probe-results.md records the opposite happening to the
    # doctor's D3; what nobody has measured is whether the stop-all prefix is
    # what clears it.
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.call_names == [
        "emergency_stop",
        "power_on",
        "status",
        "loco_info",
        "drive",
        "close",
    ]
    assert station.calls[0] == ("emergency_stop", (), {"address": None})
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})


def test_power_on_keeps_the_stored_direction_of_the_locomotive_it_idles(monkeypatch):
    """Speed 0 is the point of that telegram; the direction never was.

    `power on` sent `Direction.FORWARD` unconditionally, so a locomotive stored
    in reverse came back forward - from a command whose name, help text and
    envelope said nothing about direction.

    BENCH CHECK: store a reverse speed for loco 3, cut power, run `railctl
    power on`, then drive it and watch which way it goes. Only that settles
    whether the decoder kept the direction; this reads a call list.
    """
    station = FakeStation(
        status=AUTO_START_STATUS, loco_info=replace(LOCO_128, direction=Direction.REVERSE)
    )
    app = _app(station, monkeypatch, address=3, fmt="json")
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.calls[-2] == ("drive", (3, 0, Direction.REVERSE), {})
    payload = json.loads(result.stdout)
    assert payload["result"]["idled_direction"] == "reverse"
    assert payload["result"]["idled_direction_preserved"] is True


@pytest.mark.parametrize("failure", COSMETIC_READ_FAILURES, ids=lambda exc: type(exc).__name__)
def test_power_on_still_idles_and_says_so_when_the_direction_cannot_be_read(monkeypatch, failure):
    """The idle is a safety telegram: a direction that cannot be read must not
    cost the operator the speed 0 that stops a locomotive resuming by itself.
    So it goes out forward, and the envelope and a warning both say the stored
    direction was not preserved rather than presenting forward as measured."""
    station = FakeStation(status=AUTO_START_STATUS, loco_info_raises=failure)
    app = _app(station, monkeypatch, address=3, fmt="json")
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0, result.stderr
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})
    payload = json.loads(result.stdout)
    assert payload["result"]["idled_direction_preserved"] is False
    assert [w["name"] for w in payload["warnings"]] == ["direction_not_preserved"]


def test_power_on_does_not_preserve_a_direction_the_reply_never_decoded(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS, loco_info=LOCO_14_STEP)
    app = _app(station, monkeypatch, address=3, fmt="json")
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})
    assert json.loads(result.stdout)["result"]["idled_direction_preserved"] is False


def test_power_on_does_not_idle_when_no_address_is_configured(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=None)
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.call_names == ["emergency_stop", "power_on", "status", "close"]


def test_power_off_reports_changed_false_when_already_off(monkeypatch):
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["power", "off"])
    payload = json.loads(result.stdout)
    assert payload["result"]["changed"] is False
    assert "power_off" not in station.call_names


def test_power_off_reports_changed_true_when_it_was_on(monkeypatch):
    station = FakeStation(status=CLEAR_STATUS)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["power", "off"])
    payload = json.loads(result.stdout)
    assert payload["result"]["changed"] is True
    assert "power_off" in station.call_names


def test_power_rejects_a_state_that_is_neither_on_nor_off(monkeypatch):
    """`typer_argument` builds no Click-level enum check on purpose (`_meta`:
    a Click callback exits through Click's own usage box instead of the
    railctl/error/v1 envelope), so the enum row is enforced here, in the
    command body, and `power sideways` must not reach the station."""
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["power", "sideways"])
    assert result.exit_code == 2
    assert station.calls == []
    error = json.loads(result.stderr)
    assert error["code"] == "usage"
    assert error["suggestions"] == [["railctl", "power", "on"], ["railctl", "power", "off"]]


def test_power_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3, fmt="json")
    result = runner.invoke(app, ["power", "on"])
    payload = json.loads(result.stdout)
    assert payload["result"]["state"] == "on"
    assert payload["result"]["auto_start_mode"] is True
    assert payload["result"]["idled_address"] == 3

    station_human = FakeStation(status=AUTO_START_STATUS)
    app_human = _app(station_human, monkeypatch, address=3, fmt="human")
    human = runner.invoke(app_human, ["power", "on"])
    assert "on" in human.stdout and "3" in human.stdout and "automatic" in human.stdout


# --- stop ---


def test_stop_uses_emergency_stop_facade_with_address(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["stop", "--address", "7"])
    assert result.exit_code == 0
    assert station.calls == [("emergency_stop", (), {"address": 7}), ("close", (), {})]


def test_stop_uses_emergency_stop_facade_for_all_locomotives_when_no_address(monkeypatch):
    station = FakeStation()
    # settings.address is 3, but stop's own --address is not given, so this
    # must stop everything, not narrow to the configured default. A panic
    # button that quietly stopped one locomotive would be the most dangerous
    # kind of convenient default.
    app = _app(station, monkeypatch, address=3)
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert station.calls == [("emergency_stop", (), {"address": None}), ("close", (), {})]


def test_stop_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["stop", "--address", "7"])
    payload = json.loads(result.stdout)
    assert payload["result"] == {"address": 7, "scope": "single"}

    station_human = FakeStation()
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["stop", "--address", "7"])
    assert "7" in human.stdout


# --- never confirmed ---


def test_none_of_the_four_commands_ever_calls_confirm(monkeypatch):
    def _confirm_must_not_be_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("confirm called")

    monkeypatch.setattr(deps, "confirm", _confirm_must_not_be_called)
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    for args in (
        ["power", "on"],
        ["power", "off"],
        ["stop"],
        ["drive", "10"],
        ["function", "f2", "on"],
    ):
        result = runner.invoke(app, args, input="")
        assert result.exit_code == 0, (args, result.stdout, result.stderr)


def test_all_five_invocations_succeed_without_yes_and_without_a_prompt(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    for args in (
        ["power", "on"],
        ["power", "off"],
        ["stop"],
        ["drive", "10"],
        ["function", "f2", "on"],
    ):
        result = runner.invoke(app, args, input="")
        assert result.exit_code == 0, (args, result.stdout, result.stderr)
        assert "[y/N]" not in result.stderr


# --- the exception this task owns ---


def test_function_group_unreadable_carries_its_retry_argv_and_the_station_exit_code():
    from railctl.errors import exit_code_for

    exc = FunctionGroupUnreadableError("nope", retry_argv=["railctl", "function", "f2", "on"])
    assert exc.retry_argv == ["railctl", "function", "f2", "on"]
    # No row of its own in EXIT_CODES: it resolves to RailctlError's base 9,
    # which is what the spec's "exit 9 with a --force-group suggestion" says.
    assert exit_code_for(exc) == 9
