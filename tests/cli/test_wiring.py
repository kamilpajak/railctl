"""Global-option resolution, logging levels, confirmation, and the two
commands every railctl session starts with.

Split by the module under test with a comment banner per section, because
this is the one test file this task's contract allows for `deps.py`,
`commands/basics.py` and `main.py` together.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from typer.testing import CliRunner

from railctl.cli.commands.basics import STATUS_SCHEMA, VERSION_SCHEMA, build_status, build_version
from railctl.cli.config import Config
from railctl.cli.deps import (
    Settings,
    UsageProblem,
    build_settings,
    confirm,
    configure_logging,
    link_info,
    merge_settings,
    open_station,
    require_address,
    station_info,
)
from railctl.errors import AbortedError, ConfirmationRequiredError, TransportError
from railctl.station import TIMING, Station
from railctl.xbus.replies import StationStatus, StationVersion


def _config(**overrides) -> Config:
    return Config(**overrides)


def _settings(**overrides) -> Settings:
    base = dict(
        target="auto",
        address=None,
        fmt="human",
        verbose=0,
        color="auto",
        assume_yes=False,
        interactive=False,
    )
    base.update(overrides)
    return Settings(**base)


# -- Settings / build_settings precedence -----------------------------------


def test_target_precedence_cli_over_env_over_config_over_default():
    common = dict(
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        stdin=io.StringIO(),
    )
    all_four = dict(
        target="cli-target",
        env={"RAILCTL_TARGET": "env-target"},
        config=_config(target="config-target"),
        **common,
    )
    assert build_settings(**all_four).target == "cli-target"
    assert build_settings(**{**all_four, "target": None}).target == "env-target"
    assert build_settings(**{**all_four, "target": None, "env": {}}).target == "config-target"
    assert (
        build_settings(**{**all_four, "target": None, "env": {}, "config": _config()}).target
        == "auto"
    )


def test_address_precedence_cli_over_env_over_config_over_default():
    common = dict(
        target=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        stdin=io.StringIO(),
    )
    all_four = dict(
        address=1,
        env={"RAILCTL_ADDRESS": "2"},
        config=_config(address=3),
        **common,
    )
    assert build_settings(**all_four).address == 1
    assert build_settings(**{**all_four, "address": None}).address == 2
    assert build_settings(**{**all_four, "address": None, "env": {}}).address == 3
    assert (
        build_settings(**{**all_four, "address": None, "env": {}, "config": _config()}).address
        is None
    )


def test_verbose_precedence_cli_over_env_over_config_over_default():
    common = dict(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        color="auto",
        yes=False,
        non_interactive=True,
        stdin=io.StringIO(),
    )
    all_four = dict(
        verbose=2,
        env={"RAILCTL_VERBOSE": "1"},
        config=_config(verbose=1),
        **common,
    )
    assert build_settings(**all_four).verbose == 2
    assert build_settings(**{**all_four, "verbose": None}).verbose == 1
    assert build_settings(**{**all_four, "verbose": None, "env": {}}).verbose == 1
    assert (
        build_settings(
            **{**all_four, "verbose": None, "env": {}, "config": _config(verbose=0)}
        ).verbose
        == 0
    )


def test_format_precedence_cli_over_env_over_default():
    # `format` has no config-file key at all (design spec L3): only three
    # keys ever live in config.toml, and format is not one of them.
    common = dict(
        target=None,
        address=None,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        stdin=io.StringIO(),
        config=_config(),
    )
    # Format is `Literal["human", "json", "ndjson"]`, not an enum class - there
    # is nothing to call it with, so the comparison is against the plain string.
    assert (
        build_settings(fmt="json", json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common).fmt
        == "json"
    )
    assert (
        build_settings(fmt=None, json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common).fmt
        == "ndjson"
    )
    assert build_settings(fmt=None, json_flag=False, env={}, **common).fmt == "human"


def test_precedence_is_decided_independently_per_key():
    # A config file supplying `address` and an environment variable supplying
    # `target` must BOTH take effect in the same run - proof that build_settings
    # walks each key's own four levels rather than picking one winning SOURCE
    # for the whole call.
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_TARGET": "z21:192.168.0.111:21105"},
        config=_config(address=7),
        stdin=io.StringIO(),
    )
    assert settings.target == "z21:192.168.0.111:21105"
    assert settings.address == 7


def test_json_flag_is_an_alias_for_format_json():
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=True,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.fmt == "json"


def test_json_flag_conflicts_with_an_explicit_non_json_format():
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=None,
            fmt="ndjson",
            json_flag=True,
            verbose=None,
            color="auto",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    assert "--json" in str(caught.value)
    assert "ndjson" in str(caught.value)


def test_address_outside_bounds_is_rejected_before_any_link_is_opened(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("Station.open must not run when --address is out of range")

    monkeypatch.setattr(Station, "open", staticmethod(fail_if_called))
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=20000,
            fmt=None,
            json_flag=False,
            verbose=None,
            color="auto",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    assert "9999" in str(caught.value)


def test_railctl_port_env_var_has_no_effect():
    with_port = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_PORT": "/dev/whatever-this-would-be"},
        config=_config(),
        stdin=io.StringIO(),
    )
    without_port = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert with_port == without_port


def test_interactive_is_decided_by_stdin_isatty():
    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=False,
        env={},
        config=_config(),
        stdin=_Terminal(),
    )
    assert settings.interactive is True

    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=False,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.interactive is False


# -- merge_settings -----------------------------------------------------
#
# Tasks 10-12 give every registered command its own copy of all eight global
# options (Click parses group options before the subcommand name, so a bare
# `railctl doctor --address 3` would otherwise be a usage error) and layer
# them over `ctx.obj.settings` with this function. It is pinned here, where
# `Settings` and the sentinel rule are defined, rather than left for the first
# downstream task to invent its own version.


def test_merge_settings_overrides_only_the_typed_fields():
    base = _settings(target="auto", address=None, fmt="human", color="auto")
    merged = merge_settings(base, address=7, fmt="json")
    assert merged.address == 7
    assert merged.fmt == "json"
    # Untyped fields (the sentinel: None/False/0) pass `base`'s value through
    # unchanged - this is what lets a command declare all eight options and
    # merge unconditionally, without first checking which ones the operator
    # actually passed on this particular invocation.
    assert merged.target == "auto"
    assert merged.color == "auto"
    assert merged.assume_yes is False


def test_merge_settings_json_flag_is_an_alias_for_format_json():
    merged = merge_settings(_settings(fmt="human"), json_flag=True)
    assert merged.fmt == "json"


def test_merge_settings_leaves_base_untouched_when_nothing_is_typed():
    base = _settings()
    assert merge_settings(base) == base


# -- require_address ----------------------------------------------------


def test_require_address_returns_the_configured_address():
    assert require_address(_settings(address=3), argv_hint=["railctl", "drive", "40"]) == 3


def test_require_address_raises_a_usage_problem_with_the_documented_suggestion():
    with pytest.raises(UsageProblem) as caught:
        require_address(_settings(address=None), argv_hint=["railctl", "drive", "40"])
    assert caught.value.suggestions == [["railctl", "drive", "40", "--address", "3"]]


# -- confirm --------------------------------------------------------------


def test_confirm_with_yes_returns_immediately_without_reading_stdin():
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must not read stdin when --yes was given")

    confirm(
        "really restore?",
        settings=_settings(assume_yes=True),
        stdin=_NeverRead(),
        stderr=io.StringIO(),
    )


def test_confirm_noninteractive_raises_confirmation_required_naming_yes():
    # `ConfirmationRequiredError` (Task 8) takes only `hint`/`details`, no
    # `suggestions` - the runnable `[..., "--yes"]` array a script sees in the
    # JSON envelope is assembled later, by `_errors.py`'s `default_suggestions`,
    # from the exception's type. All this layer can pin is that `confirm()`
    # itself still tells a human reader how to get past the prompt: this test
    # goes red the moment `confirm()`'s message stops mentioning `--yes`.
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must never block on a non-interactive stdin")

    with pytest.raises(ConfirmationRequiredError) as caught:
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=False),
            stdin=_NeverRead(),
            stderr=io.StringIO(),
        )
    assert "--yes" in str(caught.value)


def test_confirm_interactive_proceeds_on_y():
    confirm(
        "really restore?",
        settings=_settings(assume_yes=False, interactive=True),
        stdin=io.StringIO("y\n"),
        stderr=io.StringIO(),
    )


def test_confirm_interactive_aborts_on_anything_else():
    with pytest.raises(AbortedError):
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=True),
            stdin=io.StringIO("n\n"),
            stderr=io.StringIO(),
        )


# -- configure_logging ------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_loggers():
    yield
    for name in ("railctl", "railctl.wire"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_configure_logging_verbose_1_enables_decoded_diagnostics_and_keeps_wire_quiet(capsys):
    stderr = io.StringIO()
    configure_logging(1, stderr)
    logging.getLogger("railctl.station").info("decoded frame: status")
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    output = stderr.getvalue()
    assert "decoded frame: status" in output
    assert "TX 21 24 05" not in output
    assert capsys.readouterr().out == ""


def test_configure_logging_verbose_2_enables_wire_debug(capsys):
    stderr = io.StringIO()
    configure_logging(2, stderr)
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    assert "TX 21 24 05" in stderr.getvalue()
    assert capsys.readouterr().out == ""


# -- open_station / link_info / station_info --------------------------------


class _FakeStation:
    def __init__(self, *, identity="serial:7010A0001194:3", raw_version=0x40, station_id=0x12):
        self.identity = identity
        self._version = StationVersion(raw=raw_version, station_id=station_id)
        self.closed = False

    def version(self) -> StationVersion:
        return self._version

    def close(self) -> None:
        self.closed = True


def test_open_station_forwards_target_address_capabilities_path_and_timing(monkeypatch, tmp_path):
    calls = []

    def fake_open(target, *, default_address, capabilities_path, timing):
        calls.append((target, default_address, capabilities_path, timing))
        return _FakeStation()

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    caps = tmp_path / "capabilities.json"
    settings = _settings(target="serial:auto", address=3)
    open_station(settings, capabilities_path=caps)
    assert calls == [("serial:auto", 3, caps, TIMING)]


def test_open_station_lets_transport_error_propagate(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    with pytest.raises(TransportError):
        open_station(_settings(), capabilities_path=None)


def test_link_info_reads_identity_and_target():
    station = _FakeStation(identity="serial:7010A0001194:3")
    info = link_info(station, _settings(target="serial:auto"))
    assert info.identity == "serial:7010A0001194:3"
    assert info.target == "serial:auto"


def test_station_info_reads_protocol_facts_from_version():
    station = _FakeStation(raw_version=0x40, station_id=0x12)
    info = station_info(station)
    assert info.protocol == "xpressnet"
    assert info.protocol_version == "4.0"
    assert info.command_station_id == 18


# -- commands/basics.py: build_version / build_status ------------------------


def test_build_version_schema_and_command_fields():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    assert result.schema == VERSION_SCHEMA
    assert result.command == "version"


def test_build_version_result_and_lines_carry_the_same_three_facts():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    joined = " ".join(result.lines)
    for fact in ("4.0", "18", "0.1.0"):
        assert fact in json.dumps(result.result)
        assert fact in joined


def test_build_status_result_and_lines_carry_the_raw_byte_and_decoded_names():
    status = StationStatus.from_raw(0x04)  # bit 2 only: auto_start_mode
    result = build_status(status)
    assert result.schema == STATUS_SCHEMA
    assert result.result["raw"] == 0x04
    assert result.result["raw_hex"] == "0x04"
    assert result.result["auto_start_mode"] is True
    assert any("0x04" in line for line in result.lines)
    assert any("start mode" in line for line in result.lines)


def test_build_status_never_calls_bit_2_short_circuit():
    status = StationStatus.from_raw(0x04)
    result = build_status(status)
    assert "short" not in json.dumps(result.result).lower()
    assert "short" not in " ".join(result.lines).lower()


def test_build_status_track_power_is_false_when_emergency_off_is_set():
    # Bit 1 (0x02), NOT bit 0. The plan this test came from used the Lenz
    # order; the measured YD7010 order is the reverse (docs/probe-results.md,
    # and StationStatus's own docstring): bit 0 is emergency STOP, bit 1 is
    # emergency OFF, and only emergency off cuts track power.
    status = StationStatus.from_raw(0x02)
    result = build_status(status)
    assert result.result["emergency_off"] is True
    assert result.result["emergency_stop"] is False
    assert result.result["track_power"] is False
    assert "track power: off" in result.lines


def test_build_status_emergency_stop_alone_leaves_track_power_on():
    # The state that separates the two documents: 80 80 sets bit 0 and the
    # track stays powered. A build_status that read bit 0 as emergency off
    # would report a live track as dead here.
    status = StationStatus.from_raw(0x01)
    result = build_status(status)
    assert result.result["emergency_stop"] is True
    assert result.result["emergency_off"] is False
    assert result.result["track_power"] is True
    assert "track power: on" in result.lines
