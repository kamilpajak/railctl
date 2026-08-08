# src/railctl/cli/deps.py
"""Global-option resolution, logging levels, and the two things every command
needs before it can do anything: an open `Station` and a confirmation gate.

Every function here is a thin, testable seam between Typer's parsed argv and
the rest of the CLI. None of it touches a wire byte or a port name - that is
`station.Station`'s job - which is what keeps this module inside
`tests/test_layering.py` rule 1.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TextIO

from railctl import errors
from railctl.cli.config import Config, pick
from railctl.cli.result import Format, LinkInfo, StationInfo
from railctl.station import TIMING, Station
from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN

_ALLOWED_FORMATS: Final[tuple[str, ...]] = ("human", "json", "ndjson")

# A fixed, illustrative loco number - never derived from any real address - so
# every "missing --address" message is reproducible and greppable. Design spec
# L2 spells out exactly this suggestion for `railctl drive 40`.
_EXAMPLE_ADDRESS: Final[str] = "3"


class UsageProblem(ValueError):
    """An exit-2 usage failure carrying a structured argv suggestion.

    Named "Problem", not "...Error": `tests/test_layering.py` rule 3 reserves
    class names ending in Error/Exception/Timeout, with a declared base, for
    `errors.py` alone. This is still a plain `ValueError` underneath -
    `railctl.cli._errors.report_for` already maps any `ValueError` to exit code
    2 - so the only thing this class adds is somewhere to hang a real argv
    array instead of a sentence an agent would have to parse back apart.
    """

    def __init__(self, message: str, *, suggestions: list[list[str]]) -> None:
        super().__init__(message)
        self.suggestions = suggestions


@dataclass(frozen=True, slots=True)
class Settings:
    target: str
    address: int | None
    fmt: Format
    verbose: int
    color: Literal["auto", "always", "never"]
    assume_yes: bool
    interactive: bool


def build_settings(
    *,
    target: str | None,
    address: int | None,
    fmt: str | None,
    json_flag: bool,
    verbose: int | None,
    color: str,
    yes: bool,
    non_interactive: bool,
    env: Mapping[str, str],
    config: Config,
    stdin: TextIO,
) -> Settings:
    """Resolve every global option, independently, per key.

    `RAILCTL_PORT` is never read here on purpose (design spec L3): it exists
    only so the hardware test suite can point at a device, and giving it any
    effect on `target` would make the shipped tool answer to a variable it
    never documents.
    """
    resolved_target = pick(
        target, env.get("RAILCTL_TARGET"), config.target, "auto", name="target", cast=str
    )

    resolved_address = pick(
        address, env.get("RAILCTL_ADDRESS"), config.address, None, name="address", cast=int
    )
    if resolved_address is not None and not (LOCO_ADDR_MIN <= resolved_address <= LOCO_ADDR_MAX):
        raise ValueError(
            f"--address {resolved_address} is outside {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}"
        )

    resolved_verbose = pick(
        verbose, env.get("RAILCTL_VERBOSE"), config.verbose, 0, name="verbose", cast=int
    )

    # `--json` is an alias for `--format=json`, so it is folded into the same
    # CLI-flag slot pick() sees - not a second, competing source. Passing both
    # `--format=ndjson` and `--json` is a real conflict, not "last flag wins":
    # on a CLI that drives a running train, silently picking one of two
    # contradictory instructions is worse than refusing to guess.
    if json_flag and fmt is not None and fmt != "json":
        raise ValueError(f"--json conflicts with --format={fmt}; pass only one")
    format_flag = "json" if json_flag else fmt
    resolved_format = pick(
        format_flag, env.get("RAILCTL_FORMAT"), None, "human", name="format", cast=str
    )
    if resolved_format not in _ALLOWED_FORMATS:
        raise ValueError(f"--format must be one of {_ALLOWED_FORMATS}, got {resolved_format!r}")

    return Settings(
        target=resolved_target,
        address=resolved_address,
        # `Format` is `Literal["human", "json", "ndjson"]`, not an enum class -
        # there is nothing to construct. `resolved_format` was already checked
        # against `_ALLOWED_FORMATS` above, so the plain string IS the value.
        fmt=resolved_format,
        verbose=resolved_verbose,
        color=color,
        assume_yes=yes,
        # stdin.isatty() is the ONLY thing that decides "interactive" - never
        # a literal path open. --non-interactive forces the non-interactive
        # branch even against a real terminal, for scripted use over a pseudo
        # terminal.
        interactive=stdin.isatty() and not non_interactive,
    )


def merge_settings(
    base: Settings,
    *,
    target: str | None = None,
    address: int | None = None,
    fmt: str | None = None,
    json_flag: bool = False,
    verbose: int = 0,
    color: str | None = None,
    yes: bool = False,
    non_interactive: bool = False,
) -> Settings:
    """Layer one command's own copy of the global options over `base`.

    Every parameter defaults to the sentinel for "not typed at the command
    level" - `None` for `target`/`address`/`fmt`/`color`, `False` for the three
    booleans, `0` for `verbose` - so a command that redeclares all eight global
    options (Tasks 10-12, worked around Click's group-options-before-subcommand
    parsing) can hand every one of them straight through and get `base`
    unchanged back when none of them were actually given on this invocation.
    """
    updates: dict[str, object] = {}
    if target is not None:
        updates["target"] = target
    if address is not None:
        updates["address"] = address
    resolved_fmt = "json" if json_flag else fmt
    if resolved_fmt is not None:
        updates["fmt"] = resolved_fmt
    if verbose > 0:
        updates["verbose"] = verbose
    if color is not None:
        updates["color"] = color
    if yes:
        updates["assume_yes"] = True
    if non_interactive:
        updates["interactive"] = False
    if not updates:
        return base
    return dataclasses.replace(base, **updates)


def configure_logging(verbose: int, stderr: TextIO) -> None:
    """Set logger levels only. `-v` is decoded diagnostics, `-vv` is raw bytes.

    This function never touches a `Frame` or a byte - it sets levels on
    `logging.getLogger("railctl")` and `logging.getLogger("railctl.wire")` by
    name. Layering rule 1 forbids wire vocabulary in `cli/`, and the envelope
    module already owns `railctl.wire` (the only wire log in the package).
    """
    handler = logging.StreamHandler(stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger("railctl")
    root.handlers = [handler]
    root.propagate = False
    root.setLevel(logging.INFO if verbose >= 1 else logging.WARNING)

    # Set explicitly rather than relying on inheritance from `root`: at
    # verbose=1 the wire logger must stay quiet even though `root` is now
    # INFO, and a logger's effective level is resolved from itself before any
    # walk up the tree happens.
    wire = logging.getLogger("railctl.wire")
    wire.handlers = [handler]
    wire.propagate = False
    wire.setLevel(logging.DEBUG if verbose >= 2 else logging.WARNING)


def open_station(settings: Settings, *, capabilities_path: Path | None) -> Station:
    """Open a `Station` for `settings.target`.

    Raises straight through on failure and adds no `try`/`except` of its own:
    `Station.open` owns closing whatever it partially opened, and swallowing
    or wrapping that here would just be a second place for the same cleanup
    to drift out of sync.
    """
    return Station.open(
        settings.target,
        default_address=settings.address,
        capabilities_path=capabilities_path,
        timing=TIMING,
    )


def link_info(station: Station, settings: Settings) -> LinkInfo:
    return LinkInfo(identity=station.identity, target=settings.target)


def station_info(station: Station) -> StationInfo:
    version = station.version()
    return StationInfo(
        protocol="xpressnet",
        protocol_version=version.version,
        command_station_id=version.station_id,
    )


def require_address(settings: Settings, *, argv_hint: list[str]) -> int:
    """`settings.address`, or an exit-2 `UsageProblem` with a runnable suggestion.

    No command in this tool takes the locomotive address positionally:
    `railctl drive 3 40` and `railctl drive 40 3` are indistinguishable to a
    human holding a running train, so the fix is always to append
    `--address <n>`, never to guess which bare number was meant.
    """
    if settings.address is not None:
        return settings.address
    raise UsageProblem(
        "no locomotive address given (neither --address, RAILCTL_ADDRESS, nor "
        "config.toml's address key); this command always needs one",
        suggestions=[[*argv_hint, "--address", _EXAMPLE_ADDRESS]],
    )


def confirm(question: str, *, settings: Settings, stdin: TextIO, stderr: TextIO) -> None:
    """Ask `question`, unless `--yes` already answered it.

    When `stdin` is not interactive this never blocks: it raises immediately,
    mentioning `--yes` in the message itself, and never reads a byte from
    `stdin`. Blocking here is how a `restore` launched from a cron job with no
    terminal at all would hang forever waiting for an answer that can never
    come. The exception carries no `suggestions` of its own -
    `ConfirmationRequiredError` only takes `hint`/`details` (Task 8) - the
    runnable `["railctl", <command>, "--yes"]` array a script sees in the JSON
    envelope is assembled later, by `_errors.py`'s `default_suggestions`, from
    the exception's type alone.
    """
    if settings.assume_yes:
        return
    if not settings.interactive:
        raise errors.ConfirmationRequiredError(
            f"{question} (refusing to guess: stdin is not interactive; rerun with --yes)"
        )
    print(f"{question} [y/N] ", end="", file=stderr, flush=True)
    answer = stdin.readline().strip().lower()
    if answer not in ("y", "yes"):
        raise errors.AbortedError(f"{question}: not confirmed")
