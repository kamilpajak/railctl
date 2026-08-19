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
def test_a_parse_failure_agrees_with_the_process_status(monkeypatch, capsys, argv):
    # The envelope's own `exit_code` and the process status are one answer, never two.
    code = _exit_code(monkeypatch, argv)
    assert _envelope(capsys)["exit_code"] == code


@pytest.mark.parametrize("argv", PARSE_FAILURES)
def test_a_parse_failure_carries_no_escape_code_and_no_usage_banner(monkeypatch, capsys, argv):
    # Today every one of these lands as a Rich box with escape codes in it, even when
    # stderr is a file. JSON output must never carry an escape code, and the `Usage:`
    # block is prose a script has to parse around.
    _exit_code(monkeypatch, argv)
    captured = capsys.readouterr()
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


# -- details -----------------------------------------------------------------


def test_details_name_the_click_class_and_the_command_path(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--format", "json", "cv", "read"])
    details = _envelope(capsys)["details"]
    assert details["parse_error"] == "MissingParameter"
    assert details["command"] == "railctl cv read"


def test_details_name_the_click_class_for_a_root_failure(monkeypatch, capsys):
    _exit_code(monkeypatch, ["--json", "--nosuchopt", "status"])
    assert _envelope(capsys)["details"]["parse_error"] == "NoSuchOption"


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
    assert payload["exit_code"] == expected["exit_code"]
    assert code == expected["exit_code"]


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
