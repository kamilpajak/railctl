# src/railctl/cli/_errors.py
"""Exception-to-exit-code-and-JSON, in one place, so every command wraps its work the same way.

`run()` is the only function in this package allowed to catch an exception and decide an exit
code from it - a command module that catches RailctlError itself and picks its own exit code
would fork the mapping errors.py already owns.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, NoReturn, TextIO

import typer

from railctl.cli.config import VERBOSE_ENV
from railctl.cli.render import render, render_error
from railctl.cli.result import (
    INTERNAL_CODE,
    INTERNAL_EXIT_CODE,
    RETRYABLE_CODES,
    USAGE_CODE,
    USAGE_EXIT_CODE,
    CommandResult,
    ErrorReport,
    Format,
    error_code,
)
from railctl.errors import (
    CONDITION_EMERGENCY_OFF,
    CONDITION_EMERGENCY_STOP,
    CONDITION_TRACK_DEAD,
    REASON_VALUE_OUT_OF_RANGE,
    AbortedError,
    ConfirmationRequiredError,
    CvOutOfRangeError,
    DecoderNotRespondingError,
    FunctionGroupUnreadableError,
    PomReadUnsupportedError,
    RailctlError,
    TrackPowerError,
    exit_code_for,
)


@dataclass(frozen=True, slots=True)
class OutputContext:
    """Two colour decisions, never one.

    `stdout_color` and `stderr_color` are separate fields, and neither has a default, because
    the single `color` flag they replaced was what let `railctl status 2> errors.log` from a
    terminal write escape codes into the log: stdout was a terminal, so stderr got painted too,
    and `grep '^error:'` over that file then matched nothing. A default value here is what would
    let a construction site silently go back to answering the question once.
    """

    fmt: Format
    stdout_color: bool
    stderr_color: bool
    stdout: TextIO
    stderr: TextIO


#: The runnable recovery for each `TrackPowerError` condition, and nothing for a
#: condition this table does not name.
#:
#: `power on` stopped being the recovery for an emergency stop: as of 2026-08-09
#: it ENERGISES AND HOLDS (docs/probe-results.md, "`power on`'s stop-all was in
#: the wrong order"), so offering it to an operator who is already held puts them
#: back where they started. The release alone is the whole recovery there. A dead
#: track has to be energised first, and that leaves it held, so the release is
#: the second half - both argv arrays are runnable, in this order.
#:
#: The unconditional fallback this replaced answered ANY condition-less
#: `TrackPowerError` with those same two commands, and `railctl power resume` is
#: measured to start every locomotive the station holds a speed for (run 5). A
#: `power off` that failed to settle got told to release the layout. A command
#: that may start a locomotive is only ever offered for a condition that
#: justifies it; where the condition is unknown, the right number of suggestions
#: is none.
TRACK_POWER_RECOVERY: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    CONDITION_EMERGENCY_STOP: (("railctl", "power", "resume"),),
    CONDITION_EMERGENCY_OFF: (
        ("railctl", "power", "on"),
        ("railctl", "power", "resume"),
    ),
    CONDITION_TRACK_DEAD: (("railctl", "power", "on"),),
}


def default_suggestions(
    exc: BaseException, *, command: str, cv: int | None = None
) -> list[list[str]]:
    """The five suggestions this project has actually needed (docs/probe-results.md R1: a POM
    read the station never answers; a confirmation nothing can ask for on a non-interactive
    stdin; a function group whose current state could not be read; a track this tool
    refuses to drive because the layout is in an emergency state; and a CV outside what the
    resolved mode reaches, where a re-probe is the answer). Everything else defaults to
    no suggestion rather than a guess that reads as authoritative advice it is not.

    `FunctionGroupUnreadableError` is the one whose argv this function cannot build. It is
    keyed by exception type plus at most a `cv`, and the retry command needs the function token
    (`"f2"`) and the state token (`"on"`) the operator actually typed - neither of which is
    anywhere in the exception's type. So the raiser (`cli/commands/throttle.py`) assembles the
    array and this reads it back, rather than the table guessing at a command-specific shape.

    `TrackPowerError` is answered from `TRACK_POWER_RECOVERY` above, keyed by the condition the
    raiser recorded. An unrecognised or missing condition gets an empty list: this table used to
    guess there, and the guess was the command that releases the layout.
    """
    if isinstance(exc, FunctionGroupUnreadableError):
        # `getattr` and `_argv_arrays`, not a bare `exc.retry_argv`: the manifest builder
        # (`_meta._class_error_row`) and the error-tree tests probe every class with
        # `klass.__new__(klass)`, which runs no `__init__` at all, so the attribute may be
        # absent. `report_for` reaches for `cv` with the same default for the same reason.
        return _argv_arrays([getattr(exc, "retry_argv", None)])
    if isinstance(exc, CvOutOfRangeError):
        # The design's rule for a CV the resolved mode cannot reach: exit 15 with
        # `railctl doctor`, because the bound is a fact about what this station has
        # proven and a re-probe is the one thing that could move it. A VALUE outside
        # the catalog's min/max shares the class and the exit code but not the
        # remedy - nothing the doctor measures changes 300 not fitting in 0..255 -
        # so that reason gets no suggestion rather than a runnable command that
        # cannot help. `details` is read with a default because `_meta._class_error_row`
        # probes every class with `klass.__new__(klass)`, which runs no `__init__`.
        if (getattr(exc, "details", None) or {}).get("reason") == REASON_VALUE_OUT_OF_RANGE:
            return []
        return [["railctl", "doctor"]]
    if isinstance(exc, (PomReadUnsupportedError, DecoderNotRespondingError)):
        # Both are what "a POM read failed" looks like on this hardware, and the design's own
        # rule - a failed POM read always suggests `railctl doctor` first - is written about
        # the failure, not about one of its two shapes. `DecoderNotRespondingError` is the
        # SILENT one (three attempts, no result at all: docs/probe-results.md R1), and left
        # out of this branch it fell through to the final `return []`: the exact case where
        # the operator most needs to be told to re-probe got no runnable answer at all.
        suggestions = [["railctl", "doctor"]]
        if cv is not None:
            suggestions.append(["railctl", "cv", "read", str(cv), "--mode", "service"])
        return suggestions
    if isinstance(exc, ConfirmationRequiredError):
        # The raiser's own full argv with `--yes` appended, when it supplied
        # one (`deps.confirm`'s `retry_argv`); the bare command-path fallback
        # stays for a raiser that did not, but it is a last resort - an argv
        # with no arguments in it exits 2 rather than running.
        override = _argv_arrays([getattr(exc, "retry_argv", None)])
        return override or [["railctl", *command.split(), "--yes"]]
    if isinstance(exc, TrackPowerError):
        # `details` is read with a default because `_meta._class_error_row`
        # probes every exception class with `klass.__new__(klass)`, which runs
        # no `__init__` and leaves the attribute absent.
        condition = (getattr(exc, "details", None) or {}).get("condition")
        return [list(argv) for argv in TRACK_POWER_RECOVERY.get(condition, ())]
    return []


def report_for(
    exc: BaseException,
    *,
    command: str,
    details: dict[str, object] | None = None,
    suggestions: list[list[str]] | None = None,
) -> ErrorReport:
    """`details` is a three-way merge, in this fixed order - `exc.details` (whatever the station
    layer already recorded, e.g. `{"address": 3, "mode": "pom", "attempts": 3}`), then
    `{"cv": exc.cv}` when the exception carries one, then the caller's own `details=` argument
    last, so an explicit call-site value always wins over what the exception recorded. Merging
    in the other order would let a stale `cv` inside `exc.details` silently shadow the one this
    function itself resolves from `exc.cv`.
    """
    code = error_code(exc)
    cv = getattr(exc, "cv", None)
    merged_details: dict[str, object] = dict(getattr(exc, "details", None) or {})
    if cv is not None:
        merged_details["cv"] = cv
    if details:
        merged_details.update(details)
    return ErrorReport(
        code=code,
        message=str(exc),
        retryable=code in RETRYABLE_CODES,
        exit_code=exit_code_for(exc),
        details=merged_details,
        suggestions=(
            suggestions
            if suggestions is not None
            else default_suggestions(exc, command=command, cv=cv)
        ),
        hint=getattr(exc, "hint", None),
    )


def usage_report(exc: BaseException) -> ErrorReport:
    """The exit-2 report for a malformed invocation - one definition, two callers.

    `run()` uses it for a `ValueError` raised inside a command's `work()`, and `cli/main.py`
    uses it for one raised out of the Typer callback before any command started. Building it
    in two places is how the process exit code and the `exit_code` inside the JSON envelope
    drift apart: `report_for` would answer 1/`internal` for a plain `ValueError`, because
    neither `error_code` nor `exit_code_for` knows anything about it, and a script would then
    be told this tool has a bug when the operator simply typed a bad flag.

    `suggestions` and `details` are read off the exception rather than hardcoded to `[]`/`{}`,
    so a `UsageProblem` (`cli/deps.py`) reaches the envelope with the runnable argv array and
    the structured reason it was raised with. Any other `ValueError` has neither attribute and
    still publishes an empty list and an empty object.
    """
    details = getattr(exc, "details", None)
    return ErrorReport(
        code=USAGE_CODE,
        message=str(exc),
        retryable=False,
        exit_code=USAGE_EXIT_CODE,
        details=dict(details) if isinstance(details, dict) else {},
        suggestions=_argv_arrays(getattr(exc, "suggestions", None)),
        hint=getattr(exc, "hint", None),
    )


def _argv_arrays(value: object) -> list[list[str]]:
    """`value` as a list of argv arrays, or an empty list if it is not already one.

    This reads an attribute off an arbitrary `ValueError`, so the shape is whatever the
    raiser put there - `UsageProblem` annotates `list[list[str]]` but an annotation stops
    nothing at runtime, and any `ValueError` from any library may carry a `.suggestions` of
    its own. Without the check, `suggestions="railctl doctor"` published
    `[["r"], ["a"], ["i"], ["l"], ...]`: valid JSON, plausible shape, and nonsense in the one
    field whose entire purpose is to be handed to `subprocess.run` without a shell.

    A malformed value yields NO suggestion rather than a mangled one. Offering nothing costs
    a caller one idea; offering `[["r"]]` costs them a command that runs. `UsageProblem`
    itself rejects a bad value at the raise site, so our own code fails where the mistake is
    and never arrives here.
    """
    if not isinstance(value, list):
        return []
    if not all(
        isinstance(argv, list) and all(isinstance(word, str) for word in argv) for argv in value
    ):
        return []
    return [list(argv) for argv in value]


def _verbose() -> bool:
    # The one place this package reads an environment variable directly: RAILCTL_VERBOSE is
    # the global --verbose flag's env fallback (design L2), and `main.global_options` writes
    # the RESOLVED verbosity back into it before any command body runs, rather than every
    # command re-deriving verbosity on its own. That write is what makes `railctl -vv status`
    # print a traceback for an internal error; without it the flag and this switch disagree
    # and the only way to get one is an environment variable no help text mentions.
    return os.environ.get(VERBOSE_ENV, "") not in ("", "0")


def _internal_report(
    exc: BaseException, ctx: OutputContext, *, verbose: bool | None = None
) -> ErrorReport:
    """The safety net: this tool has a bug. Never a domain answer, never the operator's fault.

    `verbose` overrides the `RAILCTL_VERBOSE` lookup for the one caller that knows the answer
    before the variable is written. `main.global_options` can only write it AFTER resolution
    succeeds - it reads the same variable as the environment level for `--verbose`, so writing
    first would make this process's own flag look like an inherited value - which leaves a
    window where a failure DURING resolution has no traceback switch to consult. That window
    holds exactly the failures `main()`'s safety net exists to report, an unreadable
    `config.toml` among them, so `-vv` would go unanswered precisely when it was asked.
    """
    if _verbose() if verbose is None else verbose:
        traceback.print_exc(file=ctx.stderr)
    return ErrorReport(
        code=INTERNAL_CODE,
        message=str(exc),
        retryable=False,
        exit_code=INTERNAL_EXIT_CODE,
        details={},
        suggestions=[],
        hint=None,
    )


def run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn:
    start = time.monotonic()
    try:
        result = work()
    except KeyboardInterrupt:
        report = report_for(AbortedError("interrupted by the operator"), command=command)
    except typer.Exit:
        # typer.Exit inherits RuntimeError, so the generic `except Exception` below would
        # otherwise catch it and replace whatever exit code it carries with 1/"internal" -
        # a deliberate outcome recorded as an unexplained bug, which is this project's
        # defining failure mode one layer up. Let it through untouched.
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Both are ValueError subclasses, so without this branch they would land in the
        # `usage` case below and tell a script "you typed the command wrong" when a file on
        # disk was malformed. Neither is a usage error, and neither is a domain answer: a
        # command that reads a file owes the caller a RailctlError describing it (the way
        # Capabilities.load already does). Reaching here means one did not, which is a bug
        # in that command - so it is reported as one, where it will be noticed and fixed.
        report = _internal_report(exc, ctx)
    except ValueError as exc:
        report = usage_report(exc)
    except RailctlError as exc:
        report = report_for(exc, command=command)
    except Exception as exc:  # the safety net: anything else is a bug, never a domain answer
        report = _internal_report(exc, ctx)
    else:
        # Timed here, not inside build_<command> - every command gets this for free, and a
        # command that only measured its own body would miss the argv-parsing and station-open
        # time a script comparing two invocations actually cares about.
        result.elapsed_ms = round((time.monotonic() - start) * 1000)
        render(result, fmt=ctx.fmt, stdout=ctx.stdout, color=ctx.stdout_color)
        raise typer.Exit(code=result.exit_code)

    render_error(report, stderr=ctx.stderr, fmt=ctx.fmt, color=ctx.stderr_color)
    raise typer.Exit(code=report.exit_code)
