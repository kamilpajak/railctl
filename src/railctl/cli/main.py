# src/railctl/cli/main.py
"""The Typer app: global options, `ctx.obj` wiring, and the process entry point.

Later tasks add commands by writing their own `commands/*.py` module with a
`register(app)` function and adding one import + one call at the bottom of
this file - they never need to edit `global_options` or `CliContext`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from typing import NoReturn

import typer

from railctl.cli._errors import OutputContext, _internal_report, report_for, usage_report
from railctl.cli._meta import GLOBAL_OPTIONS, typer_option
from railctl.cli.commands import basics, schema
from railctl.cli.config import VERBOSE_ENV, Config, config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging, context_for
from railctl.cli.render import render_error
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

# Built once, at import time, into names the callback below references. A call to
# `typer_option(...)` written directly as a parameter default would trip Ruff's B008
# (function call in a default argument); its built-in allowlist covers a literal
# `typer.Option(...)` call, not a wrapper around one. Keyed by flag name rather than
# unpacked positionally, so reordering `GLOBAL_OPTIONS` cannot silently hand a parameter
# the wrong row.
_ROOT_OPTIONS = {option.name: typer_option(option) for option in GLOBAL_OPTIONS}


class CliContext:
    """Resolved on the first read of `ctx.obj`, not while the group callback runs, and in
    two flavours: with `config.toml` folded in, and without it.

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

    The `_without_config_file` pair is the same resolution with `Config()`'s built-in
    defaults standing in for the file, for a command whose answer the file cannot change.
    `railctl schema` is the one such command: its manifest is a compile-time constant, and
    it is precisely what an agent runs first, on a machine with nothing plugged in and
    possibly nothing configured. Reading `config.toml` to print a constant is how a stray
    bracket in that file took down the one command that must always answer. The flag and
    environment levels still apply - `--format`, RAILCTL_FORMAT and `--color` decide how the
    manifest is rendered, and a bad value in either is still refused - it is only the file
    level that is skipped, and only for a value the file could not have affected.
    """

    __slots__ = ("_bare_settings", "_resolve", "_settings")

    def __init__(self, resolve: Callable[[Config], Settings]) -> None:
        self._resolve = resolve
        self._settings: Settings | None = None
        self._bare_settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self._resolve(load_config(config_path()))
        return self._settings

    @property
    def settings_without_config_file(self) -> Settings:
        if self._bare_settings is None:
            self._bare_settings = self._resolve(Config())
        return self._bare_settings

    @property
    def output(self) -> OutputContext:
        # Not memoised, unlike `settings`: this is a frozen dataclass built from the cached
        # `settings` plus the two real streams, so building it twice cannot answer
        # differently, and there is no second resolution hiding behind the second call.
        return self._output_for(self.settings)

    @property
    def output_without_config_file(self) -> OutputContext:
        return self._output_for(self.settings_without_config_file)

    @staticmethod
    def _output_for(settings: Settings) -> OutputContext:
        return context_for(settings, stdout=sys.stdout, stderr=sys.stderr)


@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = _ROOT_OPTIONS["--target"],
    address: int = _ROOT_OPTIONS["--address"],
    format_: str = _ROOT_OPTIONS["--format"],
    json_flag: bool = _ROOT_OPTIONS["--json"],
    verbose: int = _ROOT_OPTIONS["--verbose"],
    color: str = _ROOT_OPTIONS["--color"],
    yes: bool = _ROOT_OPTIONS["--yes"],
    non_interactive: bool = _ROOT_OPTIONS["--non-interactive"],
) -> None:
    def resolve(config: Config) -> Settings:
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
        # `_errors._verbose()` reads this variable to decide whether an internal error
        # prints a traceback, so the flag and the variable must not be able to disagree:
        # before this write, `railctl -vv status` configured logging and then printed the
        # one-line envelope with no traceback, and the only way to get one was a variable
        # no help text mentions. Written AFTER resolution, never before - `build_settings`
        # reads the same variable as the environment level for `verbose`, and writing first
        # would make this process's own flag look like an inherited environment value.
        os.environ[VERBOSE_ENV] = str(settings.verbose)
        return settings

    ctx.obj = CliContext(resolve)


basics.register(app)
schema.register(app)


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
        #
        # Verbosity is read off argv rather than the resolved settings, because nothing here
        # HAS resolved settings: every failure this branch catches happened while producing
        # them, before `global_options` wrote RAILCTL_VERBOSE. Consulting the variable would
        # answer "not verbose" for exactly the failures an operator reaches for `-vv` to
        # diagnose.
        _fail(_internal_report(exc, _entry_output(), verbose=_verbosity_in(sys.argv)))


def _verbosity_in(argv: Sequence[str]) -> bool:
    """Whether argv asks for verbose output, judged before anything parses it.

    Deliberately permissive: `--verbose`, `-v`, the repeated `-vv`, and a bundled short group
    like `-yv` all count. A false positive costs one traceback nobody asked for; a false
    negative costs the traceback someone did ask for, on a failure they cannot otherwise see.
    A bare `--` and anything after it is not inspected - past it, `-v` is a value.

    The known false positive is an option VALUE that looks like a flag: in
    `railctl --target -v`, Typer consumes `-v` as the value of `--target`, and this scan
    counts it anyway. Telling the two apart means knowing which options take a value, which
    is Typer's parse table - and this runs when parsing has already failed. One unasked-for
    traceback is the price; the alternative is withholding one that was asked for.
    """
    for arg in argv:
        if arg == "--":
            return False
        if arg == "--verbose":
            return True
        if len(arg) > 1 and arg[0] == "-" and arg[1] != "-" and "v" in arg:
            return True
    return False


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
