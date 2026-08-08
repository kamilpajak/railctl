# src/railctl/cli/render.py
"""Turns one CommandResult or ErrorReport into bytes on a stream. Nothing else in this package
writes to stdout or stderr directly - see result.py's module docstring for why that split matters.

Colour is decided and applied entirely in this module. `CommandResult.lines` never contains an
escape code; `render()` paints a copy of the text at write time, so the same result object
renders identically whether `color` is True or False, and the JSON and NDJSON branches never
call the painting helper at all - JSON output must never carry an escape code regardless of the
`color` argument, because a consumer piping `--format=json` through `jq` is not a terminal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TextIO

from railctl.cli.result import CommandResult, ErrorReport, Format

_RESET = "\x1b[0m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"

_JSON_SEPARATORS = (",", ":")  # compact: no space after "," or ":", in every JSON/NDJSON line


def want_color(choice: str, stream: TextIO, env: Mapping[str, str]) -> bool:
    """`choice` is the resolved `--color` value: `"always"`, `"never"` or `"auto"`.

    `"always"` wins even when `NO_COLOR` is set - an operator who explicitly asked for colour on
    a redirected stream gets it, because the explicit flag is a stronger signal than the
    environment convention it overrides. `NO_COLOR` counts only when it is a non-empty string -
    an unset variable and an empty one must decide the same way, "not set".
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False
    return stream.isatty()


def _paint(text: str, code: str, *, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _render_human(result: CommandResult, *, stdout: TextIO, color: bool) -> None:
    status = "ok" if result.ok else "failed"
    tone = _GREEN if result.ok else _RED
    stdout.write(_paint(f"{result.command}: {status}", tone, color=color) + "\n")
    for warning in result.warnings:
        line = f"warning: {warning.name}: {warning.message}"
        stdout.write(_paint(line, _YELLOW, color=color) + "\n")
    for line in result.lines:
        stdout.write(line + "\n")


def render(result: CommandResult, *, fmt: Format, stdout: TextIO, color: bool) -> None:
    if fmt == "human":
        _render_human(result, stdout=stdout, color=color)
        return
    if fmt == "json":
        stdout.write(json.dumps(result.envelope(), separators=_JSON_SEPARATORS))
        stdout.write("\n")
        return
    # ndjson: one summary line carries the whole envelope. A streaming command (backup,
    # restore, diff) builds its own NdjsonStream directly and never calls this branch; this
    # exists so a non-streaming command can still be asked for --format=ndjson and produce
    # something a line-oriented consumer can parse the same way.
    NdjsonStream(stdout).summary(**result.envelope())


def _render_error_human(report: ErrorReport, *, stderr: TextIO, color: bool) -> None:
    stderr.write(_paint(f"error: {report.message}", _RED, color=color) + "\n")
    if report.hint:
        stderr.write(f"hint: {report.hint}\n")
    for suggestion in report.suggestions:
        stderr.write("try: " + " ".join(suggestion) + "\n")


def render_error(report: ErrorReport, *, stderr: TextIO, fmt: Format, color: bool) -> None:
    """Errors are one JSON object on stderr in EVERY format mode but human - `ndjson` does not
    get its own error shape, because the error object is never part of the ndjson data stream:
    it is a diagnostic, and diagnostics are stderr-only regardless of what stdout is carrying.
    """
    if fmt == "human":
        _render_error_human(report, stderr=stderr, color=color)
        return
    stderr.write(json.dumps(report.envelope(), separators=_JSON_SEPARATORS))
    stderr.write("\n")


class NdjsonStream:
    """One compact JSON object per line, numbered from 0, always ending in a `summary` line -
    even when the caller's `finally` block is the only thing that runs, because a consumer
    that dies mid-run must be able to tell the run ended from the same stream it was reading.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.sequence = 0

    #: The two keys every NDJSON consumer routes on. `**fields` is expanded AFTER them, so a
    #: field of either name would silently win - a `summary` line whose "type" is not
    #: "summary" breaks every reader that filters on it, and nothing would say so. `render()`
    #: passes a whole envelope through here, so the day a later task adds an envelope key
    #: called "type" this must fail loudly instead of producing plausible wrong output.
    RESERVED_KEYS = ("type", "sequence")

    def event(self, type_: str, **fields: object) -> None:
        clashing = [key for key in self.RESERVED_KEYS if key in fields]
        if clashing:
            raise ValueError(
                f"NDJSON field name{'s' if len(clashing) > 1 else ''} "
                f"{', '.join(repr(k) for k in clashing)} would shadow the line's own "
                f"{' and '.join(self.RESERVED_KEYS)}; rename the field"
            )
        body: dict[str, object] = {"type": type_, "sequence": self.sequence, **fields}
        self._stream.write(json.dumps(body, separators=_JSON_SEPARATORS))
        self._stream.write("\n")
        self.sequence += 1

    def summary(self, **fields: object) -> None:
        self.event("summary", **fields)
