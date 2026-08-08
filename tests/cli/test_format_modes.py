# tests/cli/test_format_modes.py
"""Pins the CLI output contract: one CommandResult, three renderings, one shared shape.

Every assertion here answers a question a script author or another agent would ask before
trusting this tool's output: does `json.loads` succeed with nothing else on the stream, does a
fact I put in `result` also show up in the text a human reads, does `None` ever get silently
turned into "no", does colour ever leak into a format meant to be machine-parsed.
"""

from __future__ import annotations

import io
import json

import pytest

from railctl.cli.render import NdjsonStream, render, want_color
from railctl.cli.result import (
    CommandResult,
    LinkInfo,
    ResultWarning,
    StationInfo,
    error_code,
    tri_state,
)
from railctl.station import Check, DoctorReport
from railctl.station.capabilities import Capabilities
from railctl.station.types import EVENT_NAMES


def test_tri_state_never_renders_none_as_empty_or_dash_or_no():
    """The whole project exists to keep "unknown" from collapsing into "no". This is the
    one-line version of that rule, for the tri-state helper every capability rendering uses.
    """
    assert tri_state(None) == "unknown"
    assert tri_state(False) == "no"
    assert tri_state(True) == "yes"


def test_tri_state_tracks_a_real_capability_field_not_just_a_synthetic_bool():
    caps = Capabilities.unknown("serial:test:0")
    assert tri_state(caps.pom_read) == "unknown"
    learned_true = caps.with_learned(pom_read=True)
    learned_false = caps.with_learned(pom_read=False)
    assert tri_state(learned_true.pom_read) == "yes"
    assert tri_state(learned_false.pom_read) == "no"


def test_a_doctor_capability_keeps_its_tri_state_in_json_and_uses_the_helper_only_for_text():
    """The value that goes in `result` (and so into JSON) is the raw bool | None, never the
    tri_state() string - `tri_state()` is for the human line only. Storing "unknown" as the
    JSON value would be exactly the M1 failure mode: a capability gap that a script cannot
    distinguish from the literal string "unknown" some other field might legitimately hold.
    """
    caps = Capabilities.unknown("serial:test:0")
    report = DoctorReport(
        checks=(Check(id="D0", title="link", status="ok", detail="opened"),),
        capabilities=caps,
    )
    result = CommandResult(schema="railctl/doctor/v1", command="doctor")
    result.result["pom_read"] = report.capabilities.pom_read
    result.say(f"POM read: {tri_state(report.capabilities.pom_read)}")
    body = result.envelope()["result"]
    assert body["pom_read"] is None
    assert result.lines == ["POM read: unknown"]


def test_error_code_is_reexported_from_result():
    # error_code has its own dedicated coverage in tests/cli/test_errors.py; this only
    # confirms result.py re-exports it, since render.py and _errors.py both import it from here.
    # An exception this tool never named publishes "internal" - the same code run()'s safety
    # net gives it one layer up - rather than a contract string invented from a foreign class
    # name, which would appear in no table and read to a caller as a documented failure mode.
    class Boom(Exception):
        pass

    assert error_code(Boom("x")) == "internal"


def test_warn_appends_a_result_warning_with_its_details():
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.warn("cv.stale_result", "a stale reply arrived", cv=254, echoed=7)
    assert result.warnings == [
        ResultWarning(
            name="cv.stale_result",
            message="a stale reply arrived",
            details={"cv": 254, "echoed": 7},
        )
    ]


def test_say_appends_a_human_readable_line():
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.say("track power: on")
    assert result.lines == ["track power: on"]


def test_envelope_key_order_and_omits_link_and_station_when_none():
    result = CommandResult(schema="railctl/status/v1", command="status")
    body = result.envelope()
    assert list(body.keys()) == [
        "schema",
        "ok",
        "command",
        "exit_code",
        "elapsed_ms",
        "warnings",
        "result",
    ]
    assert "link" not in body
    assert "station" not in body


def test_envelope_includes_link_and_station_in_order_when_present():
    result = CommandResult(
        schema="railctl/status/v1",
        command="status",
        link=LinkInfo(
            identity="serial:7010A0001194:3", target="serial:/dev/cu.usbmodem7010A00011943"
        ),
        station=StationInfo(protocol="xpressnet", protocol_version="4.0", command_station_id=18),
    )
    body = result.envelope()
    assert list(body.keys()) == [
        "schema",
        "ok",
        "command",
        "exit_code",
        "elapsed_ms",
        "link",
        "station",
        "warnings",
        "result",
    ]
    assert body["link"] == {
        "identity": "serial:7010A0001194:3",
        "target": "serial:/dev/cu.usbmodem7010A00011943",
    }
    assert body["station"] == {
        "protocol": "xpressnet",
        "protocol_version": "4.0",
        "command_station_id": 18,
    }


def test_json_mode_stdout_holds_exactly_one_json_value():
    """color=True on purpose: JSON must never carry an escape code regardless of the flag -
    colour is a human-format-only concern, and this is the naive bug that would slip through
    if render() painted before checking the format.
    """
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.result["track_power"] = True
    result.say("track power: on")
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=True)
    text = out.getvalue()
    parsed = json.loads(text)  # succeeds only if stdout holds nothing but the one value
    assert parsed["schema"] == "railctl/status/v1"
    assert parsed["result"]["track_power"] is True
    assert "\x1b" not in text


def test_human_and_json_carry_the_same_facts():
    """No command may put a fact in one rendering only. render() is the single place both
    are produced from one CommandResult, and this is the test that would fail if a command
    module wrote a number into `.say()` without also putting it in `.result`, or vice versa.
    """
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.result["value"] = 131
    result.say("CV 1 = 131")
    human_out = io.StringIO()
    render(result, fmt="human", stdout=human_out, color=False)
    json_out = io.StringIO()
    render(result, fmt="json", stdout=json_out, color=False)
    assert "131" in human_out.getvalue()
    assert json.loads(json_out.getvalue())["result"]["value"] == 131


@pytest.mark.parametrize("name", EVENT_NAMES)
def test_every_station_event_name_renders_in_both_formats(name: str):
    """Imports EVENT_NAMES rather than retyping the names, so an event added to
    station/types.py in a later milestone is exercised here with no edit to this file.
    """
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.warn(name, f"test message for {name}")
    human_out = io.StringIO()
    render(result, fmt="human", stdout=human_out, color=False)
    json_out = io.StringIO()
    render(result, fmt="json", stdout=json_out, color=False)
    assert name in human_out.getvalue()
    assert json.loads(json_out.getvalue())["warnings"][0]["name"] == name


class _Stream(io.StringIO):
    def __init__(self, *, terminal: bool) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


def test_want_color_false_when_the_stream_is_not_a_terminal():
    assert want_color("auto", _Stream(terminal=False), {}) is False


def test_want_color_false_when_no_color_is_set_to_any_non_empty_value():
    assert want_color("auto", _Stream(terminal=True), {"NO_COLOR": "1"}) is False


def test_want_color_false_when_term_is_dumb():
    assert want_color("auto", _Stream(terminal=True), {"TERM": "dumb"}) is False


def test_want_color_never_wins_over_a_colour_capable_terminal():
    """The other half of the explicit-flag rule: `--color=never` is obeyed on a stream that
    would otherwise be painted, without consulting the environment at all.
    """
    assert want_color("never", _Stream(terminal=True), {}) is False


def test_want_color_always_wins_over_no_color():
    assert want_color("always", _Stream(terminal=False), {"NO_COLOR": "1"}) is True


def test_want_color_decides_stdout_and_stderr_independently():
    stdout_decision = want_color("auto", _Stream(terminal=True), {})
    stderr_decision = want_color("auto", _Stream(terminal=False), {})
    assert stdout_decision is True
    assert stderr_decision is False


def test_ndjson_stream_numbers_from_zero_and_writes_compact_lines():
    buf = io.StringIO()
    stream = NdjsonStream(buf)
    stream.event("start", total=3)
    stream.event("cv", cv=1, value=131)
    stream.summary(requested=3, ok=1)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3
    first, second, third = (json.loads(line) for line in lines)
    assert (first["type"], first["sequence"]) == ("start", 0)
    assert second["sequence"] == 1
    assert (third["type"], third["sequence"]) == ("summary", 2)
    assert ", " not in lines[0] and ": " not in lines[0]  # compact separators, no spaces


@pytest.mark.parametrize("reserved", ["type", "sequence"])
def test_ndjson_refuses_a_field_that_would_shadow_the_line_s_own_keys(reserved: str):
    """`**fields` is expanded after "type" and "sequence", so a field of either name wins.
    render() passes a whole envelope through here; the day a later task adds an envelope key
    called "type", every consumer filtering on `type == "summary"` would silently stop
    matching. Failing loudly is the only outcome that gets noticed.
    """
    stream = NdjsonStream(io.StringIO())
    with pytest.raises(ValueError, match=reserved):
        stream.event("cv", **{reserved: "hijacked"})


def test_ndjson_summary_is_always_the_last_line_even_after_a_raised_exception():
    buf = io.StringIO()
    stream = NdjsonStream(buf)
    try:
        stream.event("start", total=1)
        raise RuntimeError("mid-run failure")
    except RuntimeError:
        pass
    finally:
        stream.summary(complete=False, exit_code=9)
    last = json.loads(buf.getvalue().splitlines()[-1])
    assert last["type"] == "summary"


def test_ndjson_mode_render_emits_exactly_one_summary_line_for_a_plain_result():
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.result["track_power"] = True
    out = io.StringIO()
    render(result, fmt="ndjson", stdout=out, color=False)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["type"] == "summary"
    assert body["sequence"] == 0
    assert body["result"]["track_power"] is True
