# src/railctl/cli/main.py
"""The Typer app: global options, `ctx.obj` wiring, and the process entry point.

Later tasks add commands by writing their own `commands/*.py` module with a
`register(app)` function and adding one import + one call at the bottom of
this file - they never need to edit `global_options` or `CliContext`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import NoReturn, TextIO

import typer

from railctl.cli._errors import OutputContext, report_for, usage_report
from railctl.cli.commands import basics
from railctl.cli.config import config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging
from railctl.cli.render import render_error, want_color
from railctl.cli.result import ErrorReport
from railctl.errors import RailctlError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"max_content_width": 100},
)


@dataclass(frozen=True, slots=True)
class CliContext:
    """Resolved once per invocation, read by every command via `ctx.obj`."""

    settings: Settings
    output: OutputContext


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
    ctx.obj = CliContext(
        settings=settings,
        output=context_for(settings, stdout=sys.stdout, stderr=sys.stderr),
    )


basics.register(app)


def main() -> None:
    """Process entry point. Catches only what can be raised BEFORE a command's
    own `run()` gets a chance to: a bad `config.toml`, or an invalid global
    option (an out-of-range --address, a --json/--format conflict). Once a
    command body starts, `run()` (Task 8) already converts its failures to
    `typer.Exit`, which Typer's own dispatch handles without reaching here.

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


def _fail(report: ErrorReport) -> NoReturn:
    """Always JSON, never the human rendering: this runs while resolving `--format` itself,
    so there is no resolved format to honour. `render_error` rather than a second
    `json.dumps` call here - written by hand it came out with the interpreter's default
    spacing, so the same envelope looked different depending on whether it was produced
    before or after the command started.
    """
    render_error(report, stderr=sys.stderr, fmt="json", color=False)
    raise SystemExit(report.exit_code) from None
