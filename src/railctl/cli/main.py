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
from typer.core import TyperGroup

from railctl.cli._click_errors import ClickException, ClickUsageError
from railctl.cli._errors import (
    OutputContext,
    _internal_report,
    parse_failure_report,
    report_for,
    usage_report,
)
from railctl.cli._meta import GLOBAL_OPTIONS, TREE_ORDER, root_epilog, typer_option
from railctl.cli.commands import (
    backup,
    basics,
    cv,
    diff,
    doctor,
    monitor,
    power,
    restore,
    schema,
    throttle,
)
from railctl.cli.config import VERBOSE_ENV, Config, config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging, context_for
from railctl.cli.render import render_error
from railctl.cli.result import ErrorReport
from railctl.errors import AbortedError, RailctlError


class _TreeOrderGroup(TyperGroup):
    """Lists the root's commands in `_meta.COMMANDS` tree order.

    Typer's `get_group_from_info` assembles every plain command first and every
    `add_typer` group after them, whatever order the `register()` calls in this
    file ran in - so the moment `cv` became a group (the first two-word paths,
    M8), `railctl --help` listed it after `schema` while the manifest said it
    comes before. That is the same two-tables-of-contents defect the COMMANDS
    tuple exists to prevent, arriving through the class of the registration
    instead of its order. `list_commands` is what both Click's and Typer's help
    rendering iterate, and `tests/cli/test_schema.py` walks it too.
    """

    def list_commands(self, ctx: object) -> list[str]:
        return sorted(super().list_commands(ctx), key=TREE_ORDER.index)  # type: ignore[arg-type]


# No `no_args_is_help=True`: measured on typer 0.27.1 with this group callback and one
# command, it puts 944 bytes of help on stdout and exits 2. The contract is error to stderr,
# non-zero exit, empty stdout - so a bare `railctl` must produce the "Missing command" error
# Click writes to stderr on its own, which is what leaving this off gives (0 bytes on stdout,
# 457 on stderr, exit 2).
app = typer.Typer(
    add_completion=False,
    cls=_TreeOrderGroup,
    context_settings={"max_content_width": 100},
    # The design's fixed headings apply at EVERY level, and the root is the page an
    # operator reaches first; without this it was the one `--help` in the tool with no
    # OUTPUT / EXIT CODES / EXAMPLES at all. Generated from the same `COMMANDS` table the
    # subcommand epilogs come from - see `_meta.root_epilog`.
    epilog=root_epilog(),
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


# Registration order IS the order `railctl --help` lists commands in, and
# `tests/cli/test_schema.py` compares that order against `_meta.COMMANDS`. Two
# tables of contents for one tool is a bug that once shipped, so these calls
# follow the tuple: doctor, status, version, power, stop, drive, function, monitor,
# cv read, cv write, backup, restore, diff, schema.
doctor.register(app)
basics.register(app)
power.register(app)
throttle.register(app)
monitor.register(app)
cv.register(app)
backup.register(app)
restore.register(app)
diff.register(app)
schema.register(app)


def main() -> None:
    """Process entry point. Catches everything that can happen BEFORE a command's own
    `run()` gets a chance to: an invocation Click's parser refuses, a bad `config.toml`,
    or an invalid global option (an out-of-range --address, a --json/--format conflict).
    Once a command body starts, `run()` (Task 8) already converts its failures to
    `typer.Exit`. Resolution is deferred to the first `ctx.obj` read (see `CliContext`), so
    the config and global-option failures leave the command function rather than the
    callback - still before its `run()` starts, and still handled here.

    `standalone_mode=False` is what makes the parse failures reachable at all. In the
    default mode Typer handles a `UsageError` itself: it prints a Rich box - escape codes
    included, even when stderr is a file - and calls `sys.exit()` on its own, so
    `railctl --json bogus` ended in prose no script could parse and with no `code` to
    branch on. Turning it off hands both the exception and the exit code to this function.

    Owning the exit code is the load-bearing half of that switch. `typer.core._main`
    RETURNS `typer.Exit`'s code as an int in this mode rather than raising it, and every
    command in this tool signals its exit code by raising `typer.Exit` from `run()` - so a
    `main()` that dropped the return value would turn a sweep that must exit 9 and a failed
    `cv read` into exit 0. Anything that is not an int (a command's own `None`) is success.
    There is deliberately no `except typer.Exit` branch: in this mode it never reaches one.

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
        outcome = app(standalone_mode=False)
    except ClickUsageError as exc:
        # Unknown command, unknown option, bad value, missing argument, and the bare
        # `railctl` with no verb at all - one class covers every way the parser can refuse
        # an invocation, which is why this is caught before `ClickException` below.
        _fail(parse_failure_report(exc))
    except ClickException as exc:
        # A Click exception that is NOT a usage error. This tool raises none of its own, so
        # reaching here means something unexpected came out of the parser - a bug, reported
        # as one, rather than an invocation the operator can fix.
        _fail(_internal_report(exc, _entry_output(), verbose=_verbosity_in(sys.argv)))
    except typer.Abort:
        # The operator stopped the run. Deliberately the same envelope `run()` publishes for
        # a KeyboardInterrupt inside a command body, with the same wording: one event must
        # not answer differently depending on how far the invocation had got.
        _fail(report_for(AbortedError("interrupted by the operator"), command="railctl"))
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
    else:
        # Every branch above ends in `_fail`, which raises. This is the only path where the
        # app ran to completion, and `outcome` is what `typer.core._main` handed back: the
        # int a `typer.Exit` carried, or the command's own return value (`None`) for a run
        # that never raised one.
        raise SystemExit(outcome if isinstance(outcome, int) else 0)


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
