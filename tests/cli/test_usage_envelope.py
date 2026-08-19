"""Click's own parse failures leave the same JSON error envelope everything else does.

Driven through the real entry point - `main()` with a patched `sys.argv` - and never
through `CliRunner`. The runner invokes the Typer app directly, which is exactly the
layer that never sees `main()`'s exception handling: a test written with it passes
while the envelope is missing, which is how this hole survived until issue #30.

The throwaway-app tests replace `cli.main.app` rather than adding a command to the real
one. `main()` reads the module attribute when it runs, so the substitution is complete,
and none of these invocations can reach `Station.open` - no station is needed and none
must ever be opened by the suite.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
import typer

import railctl.cli.main as cli_main
from railctl.cli._click_errors import ClickException, ClickUsageError
from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import error_codes
from railctl.cli._parse_context import ParseContextTyper
from railctl.cli.result import ERROR_SCHEMA, INTERNAL_CODE, USAGE_CODE, USAGE_EXIT_CODE


def _exit_code(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["railctl", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    return caught.value.code


def _envelope(capsys) -> dict:
    """The one JSON value on stderr, with stdout asserted empty on the way past.

    `count("\\n") == 1` rather than a bare `json.loads`: the contract is a single JSON
    value on stderr, and a parser that skips trailing text would accept a Click banner
    printed above or below the object.
    """
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    return json.loads(captured.err)


def _manifest_usage_row() -> dict:
    """The `usage` row `railctl schema` publishes, read from the manifest builder itself.

    The envelope and the manifest derive the same two facts from different places -
    `parse_failure_report` writes `retryable` and `exit_code` by hand, `_meta.error_codes`
    derives them from `RETRYABLE_CODES` and `_RESERVED_EXIT_CODES` - so nothing but a test
    that reads both stops them describing the same `code` differently.
    """
    return {row["code"]: row for row in error_codes()}[USAGE_CODE]


def _throwaway_app(monkeypatch, body) -> None:
    app = typer.Typer()
    app.command()(body)
    monkeypatch.setattr(cli_main, "app", app)


# -- the five measured invocations -------------------------------------------

PARSE_FAILURES = [
    pytest.param(["--format", "json", "--address", "abc", "status"], id="bad-option-value"),
    pytest.param(["--json", "bogus"], id="unknown-command"),
    pytest.param(["--json", "--nosuchopt", "status"], id="unknown-root-option"),
    pytest.param(["--format", "json", "cv", "read", "--nope"], id="unknown-subcommand-option"),
    pytest.param(["--format", "json", "cv", "read"], id="missing-argument"),
]


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_exits_2_with_one_usage_envelope_on_stderr(monkeypatch, capsys, argv):
    assert _exit_code(monkeypatch, argv) == USAGE_EXIT_CODE
    payload = _envelope(capsys)
    assert payload["schema"] == ERROR_SCHEMA
    assert payload["code"] == USAGE_CODE


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_exits_with_the_status_the_manifest_publishes(monkeypatch, capsys, argv):
    """The process status, the envelope's own `exit_code` and the manifest's `usage` row
    are one answer read three ways.

    Comparing the envelope against the process status alone proves nothing: `_fail` renders
    the report and then raises `SystemExit(report.exit_code)`, so both sides come off the
    same field and no change to `parse_failure_report` can make them disagree. The manifest
    row is the independent third reading - it is what `railctl schema` tells a caller to
    expect, and it is derived from `_RESERVED_EXIT_CODES`, not from the report.
    """
    published = _manifest_usage_row()["exit_code"]
    code = _exit_code(monkeypatch, argv)
    assert code == published
    assert _envelope(capsys)["exit_code"] == published


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_is_retryable_exactly_as_the_manifest_says(monkeypatch, capsys, argv):
    """`retryable` decides whether a wrapper runs the command again.

    `parse_failure_report` hardcodes it; `_meta.error_codes` derives the same code's answer
    from `RETRYABLE_CODES`. Nothing else compares the two, so a one-token change here would
    otherwise leave `railctl --json bogus` telling a caller to retry a typo forever while
    `railctl schema` kept publishing `usage -> retryable: false`.
    """
    _exit_code(monkeypatch, argv)
    assert _envelope(capsys)["retryable"] == _manifest_usage_row()["retryable"]


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_carries_no_hint(monkeypatch, capsys, argv):
    # `hint` is a sentence for a human, and there is none to give: everything a caller can
    # act on is already in `message` (Click's own wording) and in the one runnable argv
    # array in `suggestions`. The key is present and null, never absent - see
    # `ErrorReport.envelope`.
    _exit_code(monkeypatch, argv)
    assert _envelope(capsys)["hint"] is None


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_carries_no_escape_code_and_no_usage_banner(monkeypatch, capsys, argv):
    # Today every one of these lands as a Rich box with escape codes in it, even when
    # stderr is a file. JSON output must never carry an escape code, and the `Usage:`
    # block is prose a script has to parse around.
    #
    # The envelope is read first, on purpose: two `not in` assertions are both satisfied by
    # a stderr that is completely empty, so without a positive reading of what WAS written
    # this test would keep passing for a run that published nothing at all.
    _exit_code(monkeypatch, argv)
    captured = capsys.readouterr()
    assert json.loads(captured.err)["schema"] == ERROR_SCHEMA
    assert "\x1b" not in captured.err
    assert "Usage:" not in captured.err


def test_the_message_is_clicks_own_sentence(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--json", "bogus"])
    assert _envelope(capsys)["message"] == "No such command 'bogus'."


def test_a_bad_option_value_names_the_option_and_the_value(monkeypatch, capsys):
    # `format_message()`, not `str(exc)`: the raw message is "'abc' is not a valid int."
    # with no mention of which option was being parsed.
    _exit_code(monkeypatch, ["--format", "json", "--address", "abc", "status"])
    message = _envelope(capsys)["message"]
    assert "--address" in message
    assert "abc" in message


# -- suggestions: the failing level, not the root ----------------------------


def test_a_root_level_failure_suggests_the_root_help(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--json", "bogus"])
    assert _envelope(capsys)["suggestions"] == [["railctl", "--help"]]


def test_a_subcommand_failure_suggests_that_subcommands_help(monkeypatch, capsys):
    # Click's own `Try 'railctl cv read --help'` line, as the runnable argv array the
    # project's CLI rules require. The root's help does not list `--mode` or `--page`.
    _exit_code(monkeypatch, ["--format", "json", "cv", "read", "--nope"])
    assert _envelope(capsys)["suggestions"] == [["railctl", "cv", "read", "--help"]]


def test_an_option_missing_its_value_suggests_the_failing_levels_help(monkeypatch, capsys):
    """`--mode` with nothing after it is refused by the vendored parser itself.

    `_get_value_from_state` raises `BadOptionUsage` with no `ctx` at all - Click's
    `augment_usage_errors` only wraps `Context.invoke` and `Parameter.handle_parse_result`,
    neither of which the raw option parser runs inside. Left alone, a failure three words
    deep answers with the root's help, which lists no `--mode` and no `--page`: the exact
    "wrong page" `parse_failure_report`'s docstring is about.
    """
    _exit_code(monkeypatch, ["--format", "json", "cv", "read", "--mode"])
    assert _envelope(capsys)["suggestions"] == [["railctl", "cv", "read", "--help"]]


def test_a_sub_group_answers_with_its_own_level_by_being_a_parse_context_typer(monkeypatch, capsys):
    """A GROUP inherits the context-attaching class from `ParseContextTyper.__init__`.

    Two defaults carry that class, and only one of them is reachable through the real
    tree today. `ParseContextTyper.command` covers the leaves, and `railctl cv read
    --mode` above exercises it. `ParseContextTyper.__init__` covers the groups, and the
    root does not exercise it - `main.app` passes its own `cls=_TreeOrderGroup`, so it
    keeps the behaviour with the default deleted. The one group that does rely on the
    default is `cv` (`cli/commands/cv.py` builds it as a bare `ParseContextTyper`), and
    `cv` declares no option of its own that takes a value, so the invocation that would
    show the loss cannot be typed against the real tree at all.

    So the group here is `cv`'s construction with one such option added: a nested
    `ParseContextTyper` added with `add_typer`, given a callback option that requires a
    value. `--flavour` with nothing after it is refused by the raw parser, which raises
    `BadOptionUsage` with no context - and with no context the envelope names no level
    and sends the operator to the root's help.
    """
    parent = ParseContextTyper()
    child = ParseContextTyper()

    @child.callback()
    def child_options(flavour: str = typer.Option("plain")) -> None:
        return None

    @child.command("go")
    def go() -> None:
        return None

    parent.add_typer(child, name="sub")
    monkeypatch.setattr(cli_main, "app", parent)

    assert _exit_code(monkeypatch, ["sub", "--flavour"]) == USAGE_EXIT_CODE
    payload = _envelope(capsys)
    assert payload["details"]["command"] == "railctl sub"
    assert payload["suggestions"] == [["railctl", "sub", "--help"]]


# -- details -----------------------------------------------------------------


def test_details_name_the_click_class_and_the_command_path(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--format", "json", "cv", "read"])
    details = _envelope(capsys)["details"]
    assert details["parse_error"] == "MissingParameter"
    assert details["command"] == "railctl cv read"


def test_details_name_the_click_class_for_a_root_failure(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--json", "--nosuchopt", "status"])
    assert _envelope(capsys)["details"]["parse_error"] == "NoSuchOption"


def test_details_name_the_command_path_when_the_parser_raised_without_a_context(
    monkeypatch, capsys
):
    # The other half of the ctx-less `BadOptionUsage` above: `details["command"]` is the
    # level that failed, not missing, so a caller can tell WHERE the invocation was refused
    # without re-deriving it from argv.
    _exit_code(monkeypatch, ["--format", "json", "cv", "read", "--mode"])
    details = _envelope(capsys)["details"]
    assert details["parse_error"] == "BadOptionUsage"
    assert details["command"] == "railctl cv read"


def test_details_name_the_root_when_a_root_option_is_missing_its_value(monkeypatch, capsys):
    # Same parser path one level up: the root group owns `--target`, so the context the
    # failure is answered with must be the root's, and it must be present.
    _exit_code(monkeypatch, ["--format", "json", "--target"])
    assert _envelope(capsys)["details"]["command"] == "railctl"


# -- the exit code main() now owns -------------------------------------------


def test_a_command_that_exits_9_still_exits_9_through_main(monkeypatch):
    """The regression guard for `standalone_mode=False`.

    Every command signals its exit code by raising `typer.Exit` from `run()`. In
    non-standalone mode Typer does not raise that on to the caller - it RETURNS the
    code as an int - so a `main()` that ignores the return value turns every failing
    sweep and every failed `cv read` into exit 0, the loudest possible silent defect.
    """

    def boom() -> None:
        raise typer.Exit(code=9)

    _throwaway_app(monkeypatch, boom)
    assert _exit_code(monkeypatch, []) == 9


def test_a_command_that_exits_0_exits_0_through_main(monkeypatch):
    def fine() -> None:
        raise typer.Exit(code=0)

    _throwaway_app(monkeypatch, fine)
    assert _exit_code(monkeypatch, []) == 0


def test_a_command_that_returns_without_exiting_exits_0(monkeypatch):
    # `_main` hands back the command's own return value, `None` for every railctl
    # command. Anything that is not an int is success.
    def quiet() -> None:
        return None

    _throwaway_app(monkeypatch, quiet)
    assert _exit_code(monkeypatch, []) == 0


# -- help and the bare invocation --------------------------------------------


def test_help_still_writes_to_stdout_and_exits_0(monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["--help"]) == 0
    captured = capsys.readouterr()
    assert "USAGE" in captured.out.upper()
    assert captured.err == ""


def test_a_bare_invocation_is_a_usage_envelope(monkeypatch, capsys):
    # Today this is Click's plain "Missing command." text; enveloping it is the point.
    assert _exit_code(monkeypatch, []) == USAGE_EXIT_CODE
    assert _envelope(capsys)["code"] == USAGE_CODE


# -- the two non-usage outcomes main() now owns ------------------------------


def _keyboard_interrupt_envelope() -> dict:
    """What `run()` publishes for a KeyboardInterrupt, read from `run()` itself."""
    stderr = io.StringIO()
    ctx = OutputContext(
        fmt="json",
        stdout_color=False,
        stderr_color=False,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit):
        run("railctl", ctx, interrupted)
    return json.loads(stderr.getvalue())


def test_an_abort_answers_exactly_as_an_interrupt_inside_a_command_does(monkeypatch, capsys):
    """`typer.Abort` out of the parser and `KeyboardInterrupt` inside a command body are
    the same event - the operator stopped the run - so they must not answer differently
    depending on how far the invocation had got."""

    def aborted() -> None:
        raise typer.Abort

    expected = _keyboard_interrupt_envelope()
    _throwaway_app(monkeypatch, aborted)
    code = _exit_code(monkeypatch, [])
    payload = _envelope(capsys)
    assert payload["code"] == expected["code"]
    assert payload["message"] == expected["message"]
    assert payload["exit_code"] == expected["exit_code"]
    assert code == expected["exit_code"]


def test_an_eof_reaches_the_same_envelope_behind_typers_own_blank_line(monkeypatch, capsys):
    """An `EOFError` is the one Abort route that does not leave stderr byte-clean.

    `typer.core._main` echoes an empty line to stderr and only then converts the error to
    an `Abort`, so that byte is written before `main()` sees anything and nothing in this
    tool can take it back. Pinned rather than fixed, and pinned as what it is: the envelope
    is still the only JSON value on the stream and `json.loads` still reads it, because
    leading whitespace is legal JSON. A test that asserted `err.count("\n") == 1` here
    would be asserting something typer decides.
    """

    def at_end_of_file() -> None:
        raise EOFError

    expected = _keyboard_interrupt_envelope()
    _throwaway_app(monkeypatch, at_end_of_file)
    assert _exit_code(monkeypatch, []) == expected["exit_code"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("\n")
    assert json.loads(captured.err) == expected


def test_a_click_exception_that_is_not_a_usage_error_is_an_internal_envelope(monkeypatch, capsys):
    # This tool raises none of its own, so reaching that branch means something
    # unexpected came out of the parser - reported as the bug it is, not as the
    # operator's typo.
    def broken() -> None:
        raise ClickException("the vendored parser gave up")

    _throwaway_app(monkeypatch, broken)
    assert _exit_code(monkeypatch, []) == 1
    payload = _envelope(capsys)
    assert payload["code"] == INTERNAL_CODE
    assert payload["exit_code"] == 1


# -- the guard on the typer assumption ---------------------------------------


def test_the_click_names_still_resolve_to_the_vendored_hierarchy():
    """`_click_errors` reaches the two classes through `typer.BadParameter.__base__`
    rather than importing the private `typer._click` module. If a typer upgrade breaks
    that, the envelope quietly stops being published - so it fails here instead."""
    assert ClickUsageError.__name__ == "UsageError"
    assert ClickException.__name__ == "ClickException"
    assert issubclass(typer.BadParameter, ClickUsageError)
    assert issubclass(ClickUsageError, ClickException)
    assert ClickUsageError.exit_code == USAGE_EXIT_CODE


def test_typer_exit_and_abort_are_the_classes_the_vendored_click_raises():
    assert typer.Exit.__module__.startswith("typer.")
    assert typer.Abort.__module__.startswith("typer.")
    # And the non-standalone contract itself: an eager `--help` comes back as the int 0
    # rather than as a raised `typer.Exit`.
    assert cli_main.app(["--help"], standalone_mode=False) == 0
