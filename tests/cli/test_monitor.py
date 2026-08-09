# tests/cli/test_monitor.py
"""Pins `monitor`'s decoding, its ndjson streaming contract (contiguous sequence
numbers, always ending in a summary - even on Ctrl-C), and the split between what goes
to stdout and what goes to stderr.
"""

from __future__ import annotations

import io
import json

import pytest
from typer.testing import CliRunner

from railctl.cli._errors import report_for
from railctl.cli._meta import COMMANDS
from railctl.cli.commands import monitor
from railctl.cli.commands.monitor import MONITOR_SCHEMA, build_monitor, stream_monitor
from railctl.cli.main import app as real_app
from railctl.cli.render import NdjsonStream, render
from railctl.errors import DecoderNotRespondingError
from railctl.station import EVENT_NAMES, StationEvent


class _EventStation:
    """A fake `Station` exposing only `events()` and `close()` - `stream_monitor` and
    `build_monitor` never touch anything else, which is exactly what lets this fake
    stay this small.
    """

    def __init__(self, events: list[StationEvent], *, interrupt_after: int | None = None) -> None:
        self._events = events
        self._interrupt_after = interrupt_after
        self.closed = False

    def events(self, *, interval: float = 0.25):
        for index, event in enumerate(self._events):
            yield event
            if self._interrupt_after is not None and (index + 1) == self._interrupt_after:
                raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


def test_stream_monitor_emits_contiguous_sequence_ending_in_a_summary():
    events = [
        StationEvent(at=1.0, name="power.on", detail="track power on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="track power off", payload={}),
    ]
    station = _EventStation(events)
    buf = io.StringIO()
    count = stream_monitor(station, ndjson=NdjsonStream(buf), limit=2)
    assert count == 2
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [line["sequence"] for line in lines] == [0, 1, 2]
    assert [line["type"] for line in lines] == ["event", "event", "summary"]
    assert lines[-1] == {
        "type": "summary",
        "sequence": 2,
        "count": 2,
        "complete": True,
        "exit_code": 0,
    }


def test_an_exhausted_event_source_ends_the_run_complete_with_no_limit_at_all():
    """The real `Station.events()` polls forever, so on hardware only `--limit` or
    Ctrl-C ends this loop. The fall-through still has to be right: a source that
    simply runs out is a run that finished, `complete: true`, exit 0 - not one that
    was cut short.
    """
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    buf = io.StringIO()
    assert stream_monitor(_EventStation(events), ndjson=NdjsonStream(buf)) == 1
    assert json.loads(buf.getvalue().splitlines()[-1]) == {
        "type": "summary",
        "sequence": 1,
        "count": 1,
        "complete": True,
        "exit_code": 0,
    }


def test_stream_monitor_writes_three_events_then_a_summary_on_keyboard_interrupt():
    """The fake station's event iterator raises `KeyboardInterrupt` after three
    events. `stream_monitor` must not swallow it - the interrupt still has to reach
    the caller, which is what decides the process's exit code - but the ndjson stream
    on stdout must already carry its ending summary line by the time it does.
    """
    events = [
        StationEvent(at=1.0, name="power.on", detail="d1", payload={}),
        StationEvent(at=2.0, name="power.off", detail="d2", payload={}),
        StationEvent(at=3.0, name="loco.emergency_stop", detail="d3", payload={}),
    ]
    station = _EventStation(events, interrupt_after=3)
    buf = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        stream_monitor(station, ndjson=NdjsonStream(buf))
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [line["type"] for line in lines] == ["event", "event", "event", "summary"]
    assert [line["sequence"] for line in lines] == [0, 1, 2, 3]
    assert lines[-1] == {
        "type": "summary",
        "sequence": 3,
        "count": 3,
        "complete": False,
        "exit_code": 9,
    }


def test_unknown_telegram_is_reported_not_dropped():
    """An unrecognised broadcast is `reply.unknown` with its bytes preserved - never
    silently discarded, which is the instrument defect this project exists to avoid.
    """
    events = [
        StationEvent(
            at=1.0,
            name="reply.unknown",
            detail="undecoded broadcast: 63 FF FF",
            payload={"telegram": "63 FF FF"},
        ),
    ]
    # streamed=False: this is the buffered (json) contract, where build_monitor is the
    # only place these events are ever turned into lines at all.
    result = build_monitor(events, complete=True, streamed=False)
    assert result.result["events"] == [
        {
            "name": "reply.unknown",
            "detail": "undecoded broadcast: 63 FF FF",
            "payload": {"telegram": "63 FF FF"},
        }
    ]
    assert any("reply.unknown" in line for line in result.lines)
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=False)
    body = json.loads(out.getvalue())
    assert body["schema"] == MONITOR_SCHEMA
    assert body["result"]["events"][0]["payload"]["telegram"] == "63 FF FF"


def test_build_monitor_marks_an_interrupted_run_incomplete_with_exit_nine():
    result = build_monitor([], complete=False, streamed=False)
    assert result.ok is False
    assert result.exit_code == 9
    assert "interrupted" in result.lines


def test_build_monitor_marks_a_completed_run_ok_with_exit_zero():
    result = build_monitor([], complete=True, streamed=False)
    assert result.ok is True
    assert result.exit_code == 0
    assert "no broadcasts seen" in result.lines


def test_streamed_result_never_repeats_the_lines_the_caller_already_wrote():
    """The fix for the human-mode double-print: the caller already wrote each event to
    stdout itself before calling `build_monitor(..., streamed=True)`, so one per-event
    line here would be one extra copy on the operator's terminal."""
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    result = build_monitor(events, complete=True, streamed=True)
    assert not any("power.on" in line for line in result.lines)
    assert result.result["events"][0]["name"] == "power.on"


def test_the_published_event_vocabulary_is_the_facades_own_tuple():
    """Read off `station/types.py::EVENT_NAMES`, never retyped: a consumer branching
    on `name` needs the list the facade can actually emit, and a private copy here is
    how it starts advertising a name nothing sends."""
    result = build_monitor([], complete=True, streamed=False)
    assert result.result["known_events"] == list(EVENT_NAMES)


# -- the registered command --------------------------------------------------


def _wire(monkeypatch, station: _EventStation, tmp_path):
    """Invoke the real `railctl.cli.main.app`, exactly as `tests/cli/test_doctor.py`'s
    own `_wire` does and for the same reason: a throwaway app has no callback building
    a `CliContext`, and `monitor_command` needs its own per-command global options to
    parse flags written after the subcommand name at all.
    """
    monkeypatch.setattr(monitor, "open_station", lambda settings, *, capabilities_path: station)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return real_app


def test_json_format_writes_exactly_one_json_value_events_only_on_stdout(monkeypatch, tmp_path):
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    station = _EventStation(events)
    app = _wire(monkeypatch, station, tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--limit", "1", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)  # succeeds only if stdout holds nothing else
    assert body["result"]["count"] == 1
    assert "monitoring broadcasts" in result.stderr
    assert station.closed is True


def test_human_format_prints_each_event_as_it_arrives(monkeypatch, tmp_path):
    events = [
        StationEvent(at=1.0, name="power.on", detail="track power on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="track power off", payload={}),
    ]
    station = _EventStation(events)
    app = _wire(monkeypatch, station, tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--limit", "2", "--format", "human"])
    assert result.exit_code == 0, result.stderr
    # count(), not `in` - two substring checks pass whether the command streams the
    # line to stdout once (the real contract) or `build_monitor` ALSO puts it in
    # `.lines` for `render()` to print a second time.
    assert result.stdout.count("power.on: track power on") == 1
    assert result.stdout.count("power.off: track power off") == 1


def test_ndjson_format_end_to_end_via_the_registered_command(monkeypatch, tmp_path):
    events = [
        StationEvent(at=1.0, name="power.on", detail="on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="off", payload={}),
    ]
    station = _EventStation(events)
    app = _wire(monkeypatch, station, tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--limit", "2", "--format", "ndjson"])
    assert result.exit_code == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["event", "event", "summary"]
    assert lines[-1]["exit_code"] == 0
    assert station.closed is True


def test_an_interrupted_buffered_run_is_a_partial_result_not_an_error(monkeypatch, tmp_path):
    """Ctrl-C on the `json` path returns a normal, incomplete `CommandResult`: there
    is no line-per-event stdout contract to protect there, and `run()` needs a normal
    return to render it as one JSON value rather than as an error object."""
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    station = _EventStation(events, interrupt_after=1)
    app = _wire(monkeypatch, station, tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--format", "json"])
    assert result.exit_code == 9
    body = json.loads(result.stdout)
    assert body["result"]["complete"] is False
    assert body["result"]["count"] == 1
    assert station.closed is True


def test_an_interrupted_ndjson_run_ends_its_stream_and_exits_nine(monkeypatch, tmp_path):
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    station = _EventStation(events, interrupt_after=1)
    app = _wire(monkeypatch, station, tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--format", "ndjson"])
    assert result.exit_code == 9
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[-1] == {
        "type": "summary",
        "sequence": 1,
        "count": 1,
        "complete": False,
        "exit_code": 9,
    }
    assert station.closed is True


def test_ndjson_format_reports_a_railctlerror_with_the_same_envelope_and_exit_code_run_would(
    monkeypatch, tmp_path
):
    """`_run_ndjson` bypasses `_errors.run()` to avoid a second summary line, but a
    `RailctlError` raised while opening the station must still leave stderr and the
    exit code indistinguishable from what `run()` renders for every other command's
    failure - never `main()`'s catch-all envelope, and never exit code 1.
    """
    exc = DecoderNotRespondingError("CV8 produced no result over POM after 3 attempts", cv=8)

    def _raise(settings, *, capabilities_path):
        raise exc

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(monitor, "open_station", _raise)
    result = CliRunner().invoke(real_app, ["monitor", "--format", "ndjson"])
    report = report_for(exc, command="monitor")
    assert result.exit_code == report.exit_code
    assert json.loads(result.stderr.strip().splitlines()[-1]) == report.envelope()


@pytest.mark.parametrize("path", [meta.path for meta in COMMANDS])
def test_every_command_ships_the_three_fixed_help_sections(path: str):
    """A command registered without `epilog=help_epilog(...)` ships `--help` with no
    OUTPUT, no EXIT CODES and no EXAMPLES, and nothing else notices. This collects one
    case per registered command, so it goes red on any single path missing it - not
    only on `doctor` and `monitor`'s own.
    """
    result = CliRunner().invoke(real_app, [path, "--help"])
    assert result.exit_code == 0
    for heading in ("OUTPUT", "EXIT CODES", "EXAMPLES"):
        assert heading in result.stdout
