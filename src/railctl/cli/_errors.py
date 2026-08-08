# src/railctl/cli/_errors.py
"""Exception-to-exit-code-and-JSON, in one place, so every command wraps its work the same way.

`run()` is the only function in this package allowed to catch an exception and decide an exit
code from it - a command module that catches RailctlError itself and picks its own exit code
would fork the mapping errors.py already owns.
"""

from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, TextIO

import typer

from railctl.cli.render import render, render_error
from railctl.cli.result import (
    INTERNAL_EXIT_CODE,
    RETRYABLE_CODES,
    USAGE_EXIT_CODE,
    CommandResult,
    ErrorReport,
    Format,
    error_code,
)
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    exit_code_for,
)


@dataclass(frozen=True, slots=True)
class OutputContext:
    fmt: Format
    color: bool
    stdout: TextIO
    stderr: TextIO


def default_suggestions(
    exc: BaseException, *, command: str, address: int | None = None, cv: int | None = None
) -> list[list[str]]:
    """The two suggestions this project has actually needed (docs/probe-results.md R1: a POM
    read the station never answers, and a confirmation nothing can ask for on a non-interactive
    stdin). Everything else defaults to no suggestion rather than a guess that reads as
    authoritative advice it is not.
    """
    if isinstance(exc, PomReadUnsupportedError):
        suggestions = [["railctl", "doctor"]]
        if cv is not None:
            suggestions.append(["railctl", "cv", "read", str(cv), "--mode", "service"])
        return suggestions
    if isinstance(exc, ConfirmationRequiredError):
        return [["railctl", *command.split(), "--yes"]]
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


def _verbose() -> bool:
    # The one place this package reads an environment variable directly: RAILCTL_VERBOSE is
    # the global --verbose flag's env fallback (design L2), and Task 9's Typer wiring sets it
    # before calling run() rather than every command re-deriving verbosity on its own.
    return os.environ.get("RAILCTL_VERBOSE", "") not in ("", "0")


def run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn:
    start = time.monotonic()
    try:
        result = work()
    except KeyboardInterrupt:
        report = report_for(AbortedError("interrupted by the operator"), command=command)
    except ValueError as exc:
        report = ErrorReport(
            code="usage",
            message=str(exc),
            retryable=False,
            exit_code=USAGE_EXIT_CODE,
            details={},
            suggestions=[],
            hint=None,
        )
    except RailctlError as exc:
        report = report_for(exc, command=command)
    except Exception as exc:  # the safety net: anything else is a bug, never a domain answer
        if _verbose():
            traceback.print_exc(file=ctx.stderr)
        report = ErrorReport(
            code="internal",
            message=str(exc),
            retryable=False,
            exit_code=INTERNAL_EXIT_CODE,
            details={},
            suggestions=[],
            hint=None,
        )
    else:
        # Timed here, not inside build_<command> - every command gets this for free, and a
        # command that only measured its own body would miss the argv-parsing and station-open
        # time a script comparing two invocations actually cares about.
        result.elapsed_ms = round((time.monotonic() - start) * 1000)
        render(result, fmt=ctx.fmt, stdout=ctx.stdout, color=ctx.color)
        raise typer.Exit(code=result.exit_code)

    render_error(report, stderr=ctx.stderr, fmt=ctx.fmt, color=ctx.color)
    raise typer.Exit(code=report.exit_code)
