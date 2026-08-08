# src/railctl/cli/main.py
"""The Typer app: global options, `ctx.obj` wiring, and the process entry point.

Later tasks add commands by writing their own `commands/*.py` module with a
`register(app)` function and adding one import + one call at the bottom of
this file - they never need to edit `global_options` or `CliContext`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import NoReturn, TextIO

import typer

from railctl.cli._errors import OutputContext, _internal_report, report_for, usage_report
from railctl.cli.commands import basics
from railctl.cli.config import config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging
from railctl.cli.render import render_error, want_color
from railctl.cli.result import ErrorReport
from railctl.errors import RailctlError

# No `no_args_is_help=True`: measured on typer 0.27.1 with this group callback and one
# command, it puts 944 bytes of help on stdout and exits 2. The contract is error to stderr,
# non-zero exit, empty stdout - so a bare `railctl` must produce the "Missing command" error
# Click writes to stderr on its own, which is what leaving this off gives (0 bytes on stdout,
# 457 on stderr, exit 2).
app = typer.Typer(
    add_completion=False,
    context_settings={"max_content_width": 100},
)


class CliContext:
    """Resolved on the first read of `ctx.obj`, not while the group callback runs.

    Click invokes a group's callback BEFORE a subcommand's own eager `--help`, so resolving
    `config.toml` and the eight global options inside the callback made `railctl status
    --help` exit 2 with empty stdout on a single typo in the config file - hiding the one
    page that would have told the operator which keys are recognised. `--help` must work
    offline at every level.

    Deferring is the fix rather than sniffing argv for `--help`: a help invocation reads no
    setting, so it resolves nothing by construction, and there is no list of help spellings
    to keep in step with Click's. A real command reads `ctx.obj` before it does anything
    else, so it still fails on exactly the same error with exactly the same exit code - only
    the exception now leaves the command function instead of the callback, and `main()`
    catches it either way.
    """

    __slots__ = ("_resolve", "_settings")

    def __init__(self, resolve: Callable[[], Settings]) -> None:
        self._resolve = resolve
        self._settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self._resolve()
        return self._settings

    @property
    def output(self) -> OutputContext:
        # Not memoised, unlike `settings`: this is a frozen dataclass built from the cached
        # `settings` plus the two real streams, so building it twice cannot answer
        # differently, and there is no second resolution hiding behind the second call.
        return context_for(self.settings, stdout=sys.stdout, stderr=sys.stderr)


def context_for(settings: Settings, *, stdout: TextIO, stderr: TextIO) -> OutputContext:
    """One `--color` value, but `want_color` is asked once per stream.

    The design spec requires stdout and stderr to be tested separately. Deciding once off
    stdout and painting both is how `railctl status 2> errors.log` run from a terminal ends
    up writing escape codes into the log; the converse - stdout redirected, stderr still on
    the operator's terminal - strips the colour off the one line they are meant to read.
    """
    return OutputContext(
        fmt=settings.fmt,
        stdout_color=want_color(settings.color, stdout, os.environ),
        stderr_color=want_color(settings.color, stderr, os.environ),
        stdout=stdout,
        stderr=stderr,
    )


@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = typer.Option(None, "--target", help="auto, serial:<path>, or z21:<host>:<port>"),
    address: int = typer.Option(None, "--address", "-a", help="locomotive address, 1..9999"),
    format_: str = typer.Option(None, "--format", help="human, json, or ndjson"),
    json_flag: bool = typer.Option(False, "--json", help="alias for --format=json"),
    verbose: int = typer.Option(
        None, "-v", "--verbose", count=True, help="repeatable: -v decoded frames, -vv raw bytes"
    ),
    color: str = typer.Option("auto", "--color", help="auto, always, or never"),
    yes: bool = typer.Option(False, "--yes", "-y", help="answer every confirmation yes"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="never prompt, even on a real terminal"
    ),
) -> None:
    def resolve() -> Settings:
        config = load_config(config_path())
        settings = build_settings(
            target=target,
            address=address,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
            env=os.environ,
            config=config,
            stdin=sys.stdin,
        )
        configure_logging(settings.verbose, sys.stderr)
        return settings

    ctx.obj = CliContext(resolve)


basics.register(app)


def main() -> None:
    """Process entry point. Catches only what can be raised BEFORE a command's
    own `run()` gets a chance to: a bad `config.toml`, or an invalid global
    option (an out-of-range --address, a --json/--format conflict). Once a
    command body starts, `run()` (Task 8) already converts its failures to
    `typer.Exit`, which Typer's own dispatch handles without reaching here.
    Resolution is deferred to the first `ctx.obj` read (see `CliContext`), so
    those two now leave the command function rather than the callback - still
    before its `run()` starts, and still handled here.

    The final `except Exception` is the safety net for everything else that can go
    wrong while resolving global options - an unreadable `config.toml` raising
    `PermissionError`, say. It publishes the same `internal` / exit 1 envelope
    `run()` publishes for a bug inside a command body, because a caller must never
    have to parse a traceback to find out what happened.

    The process exit code is read off the same `ErrorReport` that was just written to stderr,
    never computed a second time next to it - a run that exits 2 while its own JSON envelope
    says `"exit_code": 1` is two answers to one question, and a script has no way to tell
    which of them is the real one.
    """
    try:
        app()
    except RailctlError as exc:
        _fail(report_for(exc, command="railctl"))
    except ValueError as exc:
        # Not `report_for`: it resolves the code and the exit code off the exception's class,
        # and a plain `ValueError` is in neither table, so it would publish 1/`internal` for
        # an out-of-range `--address` or a `--json`/`--format` conflict. Both are usage
        # failures, and `usage_report` is the same builder `run()` uses for one raised inside
        # a command body, so the two paths cannot answer differently.
        _fail(usage_report(exc))
    except Exception as exc:
        # The same safety net `run()` gives a command body, so the two entry paths cannot
        # answer differently. Without it an unreadable `config.toml` leaves a Python
        # traceback on stderr and no `code` field for a wrapper to branch on.
        _fail(_internal_report(exc, _entry_output()))


def _entry_output() -> OutputContext:
    """The only field `_internal_report` reads off this is `stderr`, where it prints the
    traceback when RAILCTL_VERBOSE is set. Colour is off for both streams and the format is
    JSON because this runs while resolving `--color` and `--format` themselves, so there is
    no resolved answer to honour - the same choice `_fail` documents below.
    """
    return OutputContext(
        fmt="json",
        stdout_color=False,
        stderr_color=False,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _fail(report: ErrorReport) -> NoReturn:
    """Always JSON, never the human rendering: this runs while resolving `--format` itself,
    so there is no resolved format to honour. `render_error` rather than a second
    `json.dumps` call here - written by hand it came out with the interpreter's default
    spacing, so the same envelope looked different depending on whether it was produced
    before or after the command started.
    """
    render_error(report, stderr=sys.stderr, fmt="json", color=False)
    raise SystemExit(report.exit_code) from None
