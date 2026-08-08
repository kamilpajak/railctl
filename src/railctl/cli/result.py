# src/railctl/cli/result.py
"""The one result object every command builds, and nothing else writes to stdout with.

`CommandResult` is the object the review criterion "one result object, three renderings"
points at. `railctl.cli.render.render()` is the only function that turns it into bytes - no
command writes to `stdout` directly. Every command module from Task 9 onward exposes a pure
`build_<command>(...) -> CommandResult` that takes only facade objects (a `Station`, a
`Capabilities`, plain arguments) and returns a `CommandResult`; it opens no file, prints
nothing, reads no environment variable. The Typer function's only job is: parse argv, call
`build_*`, hand the result to `railctl.cli._errors.run()`. Splitting the object from its
renderings is what lets a test build one `CommandResult` and assert the same fact appears in
both the human text and the JSON body (design L4) - a fact recorded in only one of the two is
exactly the kind of drift this split exists to make structurally impossible.

`ERROR_SCHEMA` and every command's own `railctl/<command>/v1` string are a versioned public
contract (design L4): within a major version only optional fields may be added to an envelope;
removing a field, renaming one, or changing its type or unit needs a new `v2` schema string -
never a silent edit to what `v1` means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from railctl.errors import RailctlError

Format = Literal["human", "json", "ndjson"]

ERROR_SCHEMA: Final[str] = "railctl/error/v1"

# The two codes that name something other than a class in the exception tree, kept here
# rather than written as bare literals at their call sites in `_errors.py`. `usage` is a
# malformed invocation, `internal` is a bug in this tool. Both are as much a part of the
# published contract as any declared code, so `RESERVED_CODES` is what a test checks the
# tree against to be sure no exception ever claims one of these names for itself.
USAGE_CODE: Final[str] = "usage"
INTERNAL_CODE: Final[str] = "internal"
RESERVED_CODES: Final[frozenset[str]] = frozenset({USAGE_CODE, INTERNAL_CODE})

# Exactly these three: a script that retries on any other code is retrying a real answer
# (UnsupportedCommandError) or a bug (everything else), neither of which gets better on retry.
RETRYABLE_CODES: Final[frozenset[str]] = frozenset({"link_timeout", "station_busy", "port_busy"})

USAGE_EXIT_CODE: Final[int] = 2
INTERNAL_EXIT_CODE: Final[int] = 1


def error_code(exc: BaseException) -> str:
    """The published code for `exc` - read off the class, never computed from its name.

    This used to derive the string with a regex over `type(exc).__name__`, which read as one
    tidy rule with no lookup table to fall out of date. It had two faults. A class rename
    silently rewrote a contract string that may never be renamed; and the rule mangled the
    project's own acronyms, publishing `x_bus_checksum` and `port_not_xpress_net` while every
    other machine-readable field in the same output spells them `xbus` and `xpressnet`. The
    second fault is the one that settles it: any fix needs a table of acronyms, so a table
    exists either way - and a table of the codes themselves is the honest one, because it says
    what is published instead of what to do to a name to arrive at it.

    An exception this tool never named - a `RuntimeError`, something raised by a library -
    publishes `internal`, the same code `run()`'s safety net gives it one layer up. Minting a
    code on the spot from a foreign class name would put a string in the contract that appears
    in no table and no test, and would read to a caller as a documented failure mode.

    `type(exc).code`, not `exc.code`: what a class publishes must not be changeable by setting
    an attribute on one instance of it.
    """
    if isinstance(exc, RailctlError):
        return type(exc).code
    return INTERNAL_CODE


def tri_state(value: bool | None) -> Literal["yes", "no", "unknown"]:
    """`None` -> `"unknown"`, never `""`, `"-"` or `"no"`.

    This is the one-line version of the failure mode the whole project exists to avoid: a
    capability the doctor never probed must never render the same way as one it probed and
    found absent.
    """
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@dataclass(frozen=True, slots=True)
class ResultWarning:
    name: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LinkInfo:
    identity: str
    target: str


@dataclass(frozen=True, slots=True)
class StationInfo:
    protocol: str
    protocol_version: str | None
    command_station_id: int | None


@dataclass
class CommandResult:
    schema: str
    command: str
    ok: bool = True
    exit_code: int = 0
    elapsed_ms: int = 0
    link: LinkInfo | None = None
    station: StationInfo | None = None
    warnings: list[ResultWarning] = field(default_factory=list)
    result: dict[str, object] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def warn(self, name: str, message: str, **details: object) -> None:
        self.warnings.append(ResultWarning(name=name, message=message, details=dict(details)))

    def say(self, line: str) -> None:
        self.lines.append(line)

    def envelope(self) -> dict[str, object]:
        """The JSON body, in the documented key order. `link` and `station` are OMITTED, not
        `null`, when no link was opened - a doctor failure on D0 has no station to describe,
        and an absent key is a smaller claim than a null one.
        """
        body: dict[str, object] = {
            "schema": self.schema,
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.link is not None:
            body["link"] = {"identity": self.link.identity, "target": self.link.target}
        if self.station is not None:
            body["station"] = {
                "protocol": self.station.protocol,
                "protocol_version": self.station.protocol_version,
                "command_station_id": self.station.command_station_id,
            }
        body["warnings"] = [
            {"name": w.name, "message": w.message, "details": w.details} for w in self.warnings
        ]
        body["result"] = self.result
        return body


@dataclass(frozen=True, slots=True)
class ErrorReport:
    code: str
    message: str
    retryable: bool
    exit_code: int
    details: dict[str, object] = field(default_factory=dict)
    suggestions: list[list[str]] = field(default_factory=list)
    hint: str | None = None

    def envelope(self) -> dict[str, object]:
        """`hint` sits between `message` and `retryable`, and is `None` rather than omitted
        when there is none - the same optional-field rule `CommandResult.envelope()` follows,
        because a script and a human must be able to read the same fact off the same object.
        Dropping this key here is exactly how the human rendering (which already prints
        `report.hint` in `render.py`) and the JSON rendering drift apart.
        """
        return {
            "schema": ERROR_SCHEMA,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "exit_code": self.exit_code,
            "details": self.details,
            "suggestions": [list(s) for s in self.suggestions],
        }
