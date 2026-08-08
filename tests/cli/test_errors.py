# tests/cli/test_errors.py
"""Pins the exception-to-exit-code-and-JSON pipeline: railctl.errors -> ErrorReport -> stderr.

The five run() tests are the ones that matter most: they are the only place in this task that
proves KeyboardInterrupt, a bad CLI argument, a domain RailctlError and an honest-to-goodness
bug in this tool all end up with DIFFERENT exit codes and DIFFERENT `code` strings, because a
script reading only `$?` has no other way to tell "I pressed Ctrl-C" from "railctl has a bug".
"""

from __future__ import annotations

import io
import json
import re
from itertools import pairwise
from pathlib import Path

import pytest
import typer

from railctl.cli._errors import OutputContext, default_suggestions, report_for, run
from railctl.cli.render import render_error
from railctl.cli.result import (
    INTERNAL_CODE,
    RESERVED_CODES,
    CommandResult,
    ErrorReport,
    error_code,
)
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    CvVerifyError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    PortBusy,
    RailctlError,
    StationBusyError,
    TrackPowerError,
    UnsupportedCommandError,
    XBusChecksumError,
    XBusDecodeError,
)


def _tree(root: type[RailctlError] = RailctlError) -> set[type[RailctlError]]:
    """Every exception class railctl defines, and ONLY those.

    The `__module__` filter is load-bearing, not tidiness. CPython registers a class with its
    bases BEFORE running `__init_subclass__`, so a class whose hook raises stays in
    `__subclasses__()` as a zombie: defined enough to be walked, never finished enough to have
    a `code`. This file plants exactly that kind of class on purpose (see the guard tests
    below), and without the filter those zombies would leak into every other test that walks
    the tree - making the uniqueness and declaration checks pass or fail on test ORDER.
    """
    found = {root} if root.__module__ == "railctl.errors" else set()
    for sub in root.__subclasses__():
        found |= _tree(sub)
    return found


@pytest.mark.parametrize(
    ("exc_cls", "code"),
    [
        (LinkTimeout, "link_timeout"),
        (UnsupportedCommandError, "unsupported_command"),
        (PomReadUnsupportedError, "pom_read_unsupported"),
        (CvVerifyError, "cv_verify"),
        (TrackPowerError, "track_power"),
        (StationBusyError, "station_busy"),
        (PortBusy, "port_busy"),
        (AbortedError, "aborted"),
    ],
)
def test_error_code_maps_the_documented_names(exc_cls: type[RailctlError], code: str):
    assert error_code(exc_cls.__new__(exc_cls)) == code


#: Every published error code, frozen. Each one is DECLARED on its class in `errors.py`; this
#: table is the second copy, and its whole job is to make any edit to the first copy show up
#: in a diff as a contract change rather than a refactor. A code is public and may never be
#: renamed inside a major version, so the two copies disagreeing is the alarm, not the bug.
PUBLISHED_ERROR_CODES: dict[str, str] = {
    "AbortedError": "aborted",
    "AmbiguousPort": "ambiguous_port",
    "ConfirmationRequiredError": "confirmation_required",
    "CvOutOfRangeError": "cv_out_of_range",
    "CvVerifyError": "cv_verify",
    "DecoderNoAckError": "decoder_no_ack",
    "DecoderNotRespondingError": "decoder_not_responding",
    "FunctionGroupUnreadableError": "function_group_unreadable",
    "IndexPageRequiredError": "index_page_required",
    "LinkProtocolError": "link_protocol",
    "LinkTimeout": "link_timeout",
    "PomReadUnsupportedError": "pom_read_unsupported",
    "PortBusy": "port_busy",
    "PortConfigError": "port_config",
    "PortNotFound": "port_not_found",
    "PortNotOpen": "port_not_open",
    "PortNotXpressNet": "port_not_xpressnet",
    "ProgrammingError": "programming",
    "ProtocolError": "protocol",
    "RailctlError": "railctl",
    "ServiceEncodingUnknownError": "service_encoding_unknown",
    "ShortCircuitError": "short_circuit",
    "StationBusyError": "station_busy",
    "StationError": "station",
    "TrackPowerError": "track_power",
    "TransportError": "transport",
    "UnsupportedCommandError": "unsupported_command",
    "UnsupportedFeatureError": "unsupported_feature",
    "XBusChecksumError": "xbus_checksum",
    "XBusDecodeError": "xbus_decode",
    "XBusEncodeError": "xbus_encode",
    "XBusIncompleteError": "xbus_incomplete",
}


def test_every_published_error_code_is_exactly_what_it_was():
    """Renaming a class renames its published code. That is a breaking API change wearing a
    refactor's clothes, and nothing else in the suite would report it.
    """
    live = {k.__name__: error_code(k.__new__(k)) for k in _tree()}
    assert live == PUBLISHED_ERROR_CODES


def test_defining_a_subclass_with_no_code_fails_at_class_definition():
    """The primary guard is not a test - it is `RailctlError.__init_subclass__`, which raises
    while the module is still importing. An exception class that forgot its code cannot reach
    a test run, a review or a release; it stops the interpreter at the line that defines it.
    This is the tripwire proving that hook is still armed.
    """
    with pytest.raises(TypeError, match="declares no `code`"):

        class Undeclared(RailctlError):
            pass


def test_a_subclass_may_not_silently_inherit_its_parents_code():
    """The one mistake declaring a code introduces that deriving one could not make. A subclass
    inherits `code` as an attribute, so a hook written with `hasattr` would be satisfied by a
    class that declared nothing - and it would publish its parent's code: a wrong answer, not a
    missing one. `XBusDecodeError` is the parent here on purpose, because it is two levels down;
    a hook that only guarded the direct children of `RailctlError` would pass this and should not.
    """
    with pytest.raises(TypeError, match="declares no `code`"):

        class SilentlyInherits(XBusDecodeError):
            pass


def test_every_class_in_the_error_tree_declares_its_own_code():
    """Unfailable while the hook above is intact, and that is the point: it is the tripwire on
    the hook, and it covers `RailctlError` itself, which `__init_subclass__` never runs for.
    Do not read it as independent coverage of the 31 declarations.
    """
    undeclared = sorted(k.__name__ for k in _tree() if "code" not in k.__dict__)
    assert undeclared == []


def test_no_exception_claims_a_reserved_code():
    """`usage` and `internal` describe a malformed invocation and a bug in this tool. An
    exception answering to either would make a real domain failure indistinguishable from one
    of those two, and a caller has no second field to break the tie.
    """
    clashing = sorted(k.__name__ for k in _tree() if k.code in RESERVED_CODES)
    assert clashing == []


def test_an_exception_this_tool_never_named_publishes_internal():
    """A foreign exception must not mint a contract string from its class name. `internal` is
    what `run()`'s safety net already publishes for the same event, so the two agree.
    """

    class DefinedByATest(Exception):
        pass

    assert error_code(DefinedByATest("x")) == INTERNAL_CODE
    assert error_code(RuntimeError("x")) == INTERNAL_CODE


def _package_sources() -> str:
    """Every line of the package's own Python, as one blob to search."""
    root = Path(__file__).resolve().parents[2] / "src" / "railctl"
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py")))


def test_no_published_code_splits_a_word_this_package_spells_as_one():
    """The guard on the bug that started this: `x_bus_checksum` was unique, snake_case and
    entirely wrong, and every other check in this file was happy with it.

    The question it asks is not "is this spelled nicely" but "does the package itself write
    these letters as one word anywhere in its own source". `xbus` appears in 45 places and
    `xpressnet` in 4, so `x_bus_` and `xpress_net_` were this tool recording one fact two ways
    - in `code` split, in the status envelope's `"protocol": "xpressnet"` glued. A machine
    consumer cannot tell those are the same word.
    """
    sources = _package_sources()
    offenders = []
    for klass in _tree():
        parts = klass.code.split("_")
        for left, right in pairwise(parts):
            glued = f"{left}{right}"
            if len(glued) >= 4 and re.search(rf"\b{glued}\b", sources):
                offenders.append(f"{klass.__name__}.code={klass.code!r} splits {glued!r}")
    assert offenders == [], (
        f"{offenders} - the package spells these as one word in its own source. Either spell "
        f"the declared code the same way, or establish the split spelling in the package first."
    )


def test_the_spelling_scan_would_catch_the_bug_it_was_written_for():
    """Proves the scan above by running it against the code this project actually shipped for
    one commit. Without this, a scan that silently matched nothing would look identical to a
    scan that found nothing wrong.
    """
    sources = _package_sources()
    parts = "x_bus_checksum".split("_")
    glued = [
        f"{a}{b}"
        for a, b in pairwise(parts)
        if len(f"{a}{b}") >= 4 and re.search(rf"\b{a}{b}\b", sources)
    ]
    assert glued == ["xbus"]


def test_every_class_in_the_error_tree_gets_a_unique_code():
    """A whole-tree test, not just the eight pinned above: two exceptions sharing a code would
    let a script mistake one domain failure for another with no way to notice.
    """
    codes = [error_code(k.__new__(k)) for k in _tree()]
    assert len(codes) == len(set(codes)), codes


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (LinkTimeout("x"), True),
        (StationBusyError("x"), True),
        (PortBusy("x"), True),
        (DecoderNotRespondingError("x"), False),
        (CvVerifyError("x"), False),
        (TrackPowerError("x"), False),
        (UnsupportedCommandError("x"), False),
    ],
)
def test_retryable_is_true_for_exactly_three_classes(exc: RailctlError, expected: bool):
    """DecoderNotRespondingError is retryable in feel - retrying a service-mode read is a
    normal thing to do - but not in fact: the station said nothing, and asking again with no
    change of track power or wiring gets the same silence. Only a station that reported BUSY
    (retry will resolve on its own) or a link that timed out or a port that was busy are true.
    """
    assert report_for(exc, command="cv read").retryable is expected


def test_retryable_true_set_is_exactly_link_timeout_station_busy_and_port_busy():
    retryable = {k for k in _tree() if report_for(k.__new__(k), command="x").retryable}
    assert retryable == {LinkTimeout, StationBusyError, PortBusy}


def test_pom_read_failure_always_suggests_doctor_first():
    """The usual cause of a failed POM read is RailCom off or the track unpowered - both are
    what `doctor` reports first - so `doctor` always leads, even when a same-CV service-mode
    retry is also offered.
    """
    with_cv = report_for(
        PomReadUnsupportedError("no result for CV 8 on loco 3", cv=8), command="cv read"
    )
    assert with_cv.suggestions[0] == ["railctl", "doctor"]
    assert with_cv.suggestions[1] == ["railctl", "cv", "read", "8", "--mode", "service"]

    without_cv = report_for(
        PomReadUnsupportedError("pom reads are unsupported here"), command="cv read"
    )
    assert without_cv.suggestions == [["railctl", "doctor"]]


def test_suggestions_are_argv_arrays_never_shell_strings():
    """An agent must be able to subprocess.run(suggestion) with no shell - a single string
    element containing a space would be a shell command line smuggled into a JSON array.
    """
    report = report_for(PomReadUnsupportedError("no result for CV 8", cv=8), command="cv read")
    assert report.suggestions, "fixture exception produced no suggestions to check"
    for suggestion in report.suggestions:
        assert isinstance(suggestion, list)
        for arg in suggestion:
            assert isinstance(arg, str)
            assert " " not in arg


def test_an_explicit_details_argument_wins_over_what_the_exception_recorded():
    """The last step of `report_for`'s three-way merge, which its docstring claims but nothing
    else exercises: a call site that knows better than the exception overrides it. The reverse
    order would let a stale value recorded deep in the station layer shadow what the command
    itself resolved.
    """
    exc = DecoderNotRespondingError("no result", cv=8, details={"mode": "pom", "attempts": 3})
    report = report_for(exc, command="cv read", details={"mode": "service", "address": 3})
    assert report.details == {"mode": "service", "attempts": 3, "cv": 8, "address": 3}


def test_the_human_rendering_prints_each_suggestion_as_a_runnable_line():
    """The JSON side carries suggestions as argv arrays; a human reading stderr must get the
    same next command as text. Untested, this loop could silently print nothing and only the
    machine format would still carry the advice.
    """
    report = report_for(PomReadUnsupportedError("no result for CV 8", cv=8), command="cv read")
    err = io.StringIO()
    render_error(report, stderr=err, fmt="human", color=False)
    assert "try: railctl doctor" in err.getvalue()
    assert "try: railctl cv read 8 --mode service" in err.getvalue()


def test_report_for_reads_the_hint_off_the_exception():
    exc = TrackPowerError("track power is off", hint="run `railctl power on`")
    report = report_for(exc, command="drive")
    assert report.hint == "run `railctl power on`"
    assert report.code == "track_power"
    assert report.exit_code == 20


def test_default_suggestions_is_empty_for_an_exception_with_no_known_fix():
    # A garbled reply frame has no runnable remedy, and offering one would read as
    # authoritative advice this project does not have.
    assert default_suggestions(XBusChecksumError("trailing XOR byte"), command="drive") == []


def test_default_suggestions_offers_power_on_for_a_refused_track():
    """The pre-flight refusal names `railctl power on` in its prose. An agent should not
    have to parse a sentence to find the one command that clears both emergency states."""
    exc = TrackPowerError("the layout is in emergency stop (the track still has voltage)")
    assert default_suggestions(exc, command="drive") == [["railctl", "power", "on"]]


def test_default_suggestions_offers_yes_for_a_blocked_confirmation():
    exc = ConfirmationRequiredError("restore needs --force on a decoder with pending writes")
    assert default_suggestions(exc, command="restore backup.json") == [
        ["railctl", "restore", "backup.json", "--yes"]
    ]


@pytest.mark.parametrize("fmt", ["human", "json", "ndjson"])
def test_errors_go_to_stderr_in_every_format_mode(fmt: str):
    report = report_for(LinkTimeout("no reply to 21 24 05 within 5.0 s"), command="status")
    err = io.StringIO()
    render_error(report, stderr=err, fmt=fmt, color=False)
    assert err.getvalue() != ""
    if fmt != "human":
        body = json.loads(err.getvalue())
        assert body["code"] == "link_timeout"
        assert body["retryable"] is True


def test_the_json_error_envelope_carries_the_same_hint_the_human_rendering_prints():
    """`_render_error_human` prints `report.hint` when set. A JSON branch that dropped it would
    let the hint distinguishing "POM is recorded unsupported, use `--mode service`" from a bare
    refusal reach a human and vanish for a script. Breaks if `envelope()` stops including `hint`.
    """
    report = ErrorReport(
        code="track_power",
        message="track power is off",
        retryable=False,
        exit_code=20,
        hint="run `railctl power on`",
    )
    human_out = io.StringIO()
    render_error(report, stderr=human_out, fmt="human", color=False)
    json_out = io.StringIO()
    render_error(report, stderr=json_out, fmt="json", color=False)
    assert "run `railctl power on`" in human_out.getvalue()
    assert json.loads(json_out.getvalue())["hint"] == "run `railctl power on`"


def _ctx(fmt: str = "json") -> OutputContext:
    return OutputContext(
        fmt=fmt,
        stdout_color=False,
        stderr_color=False,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )


def test_run_converts_keyboard_interrupt_to_aborted_exit_9():
    ctx = _ctx()

    def work() -> CommandResult:
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as caught:
        run("stop", ctx, work)
    assert caught.value.exit_code == 9
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body["code"] == "aborted"


def test_run_reports_a_value_error_as_usage_exit_2():
    ctx = _ctx()

    def work() -> CommandResult:
        raise ValueError("speed must be 0..126")

    with pytest.raises(typer.Exit) as caught:
        run("drive", ctx, work)
    assert caught.value.exit_code == 2
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body == {
        "schema": "railctl/error/v1",
        "code": "usage",
        "message": "speed must be 0..126",
        "hint": None,
        "retryable": False,
        "exit_code": 2,
        "details": {},
        "suggestions": [],
    }


def test_run_lets_a_typer_exit_through_with_the_code_it_carries():
    """`typer.Exit` inherits RuntimeError, so the generic `except Exception` would otherwise
    catch it and publish 1/"internal" instead of the code the command chose. A deliberate exit
    reported as an unexplained bug is this project's central failure mode, one layer up.
    """
    ctx = _ctx()

    def work() -> CommandResult:
        raise typer.Exit(code=8)

    with pytest.raises(typer.Exit) as caught:
        run("backup", ctx, work)
    assert caught.value.exit_code == 8
    assert ctx.stderr.getvalue() == ""
    assert ctx.stdout.getvalue() == ""


@pytest.mark.parametrize(
    "exc",
    [
        json.JSONDecodeError("Expecting value", "{bad", 1),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_run_does_not_blame_the_operator_for_a_malformed_file(exc: ValueError, monkeypatch):
    """Both are ValueError subclasses. Left to the `usage` branch they would tell a script
    "fix your command line" when a file on disk was corrupt or mis-encoded - and exit 2 tells
    a caller not to retry and not to look at its input. A command that reads a file owes a
    RailctlError naming it; reaching here means one did not, so it is reported as our bug.
    """
    monkeypatch.delenv("RAILCTL_VERBOSE", raising=False)
    ctx = _ctx()

    def work() -> CommandResult:
        raise exc

    with pytest.raises(typer.Exit) as caught:
        run("restore", ctx, work)
    assert caught.value.exit_code == 1
    body = json.loads(ctx.stderr.getvalue())
    assert body["code"] == "internal"


def test_run_maps_a_railctl_error_through_exit_code_for():
    ctx = _ctx()

    def work() -> CommandResult:
        raise CvVerifyError("mismatch", cv=8)

    with pytest.raises(typer.Exit) as caught:
        run("cv write", ctx, work)
    assert caught.value.exit_code == 14
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body["code"] == "cv_verify"


def test_run_reports_an_unmapped_exception_as_internal_exit_1_without_a_traceback(monkeypatch):
    monkeypatch.delenv("RAILCTL_VERBOSE", raising=False)
    ctx = _ctx()

    def work() -> CommandResult:
        raise RuntimeError("unreachable branch hit")

    with pytest.raises(typer.Exit) as caught:
        run("status", ctx, work)
    assert caught.value.exit_code == 1
    assert ctx.stdout.getvalue() == ""
    stderr_text = ctx.stderr.getvalue()
    assert json.loads(stderr_text)["code"] == "internal"
    assert "Traceback" not in stderr_text


def test_run_prints_the_traceback_only_in_verbose_mode(monkeypatch):
    monkeypatch.setenv("RAILCTL_VERBOSE", "1")
    ctx = _ctx()

    def work() -> CommandResult:
        raise RuntimeError("unreachable branch hit")

    with pytest.raises(typer.Exit):
        run("status", ctx, work)
    assert "Traceback" in ctx.stderr.getvalue()


def test_run_renders_the_result_and_exits_with_its_code():
    ctx = _ctx()

    def work() -> CommandResult:
        result = CommandResult(schema="railctl/status/v1", command="status")
        result.result["track_power"] = True
        return result

    with pytest.raises(typer.Exit) as caught:
        run("status", ctx, work)
    assert caught.value.exit_code == 0
    assert ctx.stderr.getvalue() == ""
    body = json.loads(ctx.stdout.getvalue())
    assert body["result"]["track_power"] is True


@pytest.mark.parametrize("fmt", ["human", "json", "ndjson"])
def test_run_sends_every_format_error_to_stderr_only(fmt: str):
    ctx = _ctx(fmt=fmt)

    def work() -> CommandResult:
        raise TrackPowerError("track power is off")

    with pytest.raises(typer.Exit) as caught:
        run("power on", ctx, work)
    assert caught.value.exit_code == 20
    assert ctx.stdout.getvalue() == ""
    assert ctx.stderr.getvalue() != ""


def test_run_times_the_work_and_reports_a_non_zero_elapsed_ms(monkeypatch):
    """`elapsed_ms` is stamped by `run()`, not by the command body. Left at the dataclass
    default of 0 forever, a script comparing a fast `status` call against a POM read that took
    three 2 s attempts would have no way to tell them apart. Patches the module's own
    `time.monotonic`, not the stdlib one, so the fixture is exact rather than racing a clock.
    """
    ticks = iter([100.0, 100.037])
    monkeypatch.setattr("railctl.cli._errors.time.monotonic", lambda: next(ticks))
    ctx = _ctx()

    def work() -> CommandResult:
        return CommandResult(schema="railctl/status/v1", command="status")

    with pytest.raises(typer.Exit):
        run("status", ctx, work)
    body = json.loads(ctx.stdout.getvalue())
    assert body["elapsed_ms"] == 37


def test_a_decoder_not_responding_error_carrying_details_surfaces_them_in_the_envelope():
    """Pins the merge order `report_for` documents - `exc.details` first, then `{"cv": exc.cv}`,
    then the caller's own `details=` argument. Breaks if `report_for` stops reading
    `exc.details`, or if it lets an explicit `cv` entry inside `exc.details` silently win over
    the `cv` keyword instead of the reverse.
    """
    ctx = _ctx()
    exc = DecoderNotRespondingError(
        "no result for CV 8 after 3 attempts",
        cv=8,
        details={"address": 3, "mode": "pom", "attempts": 3, "attempt_timeout_s": 2.0},
    )

    def work() -> CommandResult:
        raise exc

    with pytest.raises(typer.Exit) as caught:
        run("cv read", ctx, work)
    assert caught.value.exit_code == 13
    body = json.loads(ctx.stderr.getvalue())
    assert body["details"] == {
        "address": 3,
        "mode": "pom",
        "attempts": 3,
        "attempt_timeout_s": 2.0,
        "cv": 8,
    }
