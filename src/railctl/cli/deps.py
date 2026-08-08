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
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TextIO

from railctl import errors
from railctl.cli._errors import OutputContext
from railctl.cli.config import DEFAULT_TARGET, VERBOSE_ENV, Config, pick
from railctl.cli.render import want_color
from railctl.cli.result import Format, LinkInfo, StationInfo
from railctl.station import TIMING, Station
from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN
from railctl.xbus.replies import LocoInfo
from railctl.xbus.speed import Direction

#: A direction is a word in every rendering, never a wire value, and it is
#: spelled here once. `drive` and `power` both report one, and two private
#: copies of this table are how they start disagreeing about the spelling a
#: script matches on.
DIRECTION_TEXT: Final[dict[Direction, str]] = {
    Direction.FORWARD: "forward",
    Direction.REVERSE: "reverse",
}

# Public, not `_ALLOWED_*`, because `cli/_meta.py` publishes these exact tuples as the
# `enum` list of `--format` and `--color` in `railctl schema`'s manifest. Read there, never
# retyped: a manifest that advertises a choice this module rejects is a documented lie, and
# a fourth format added here has to appear in the manifest without a second edit.
ALLOWED_FORMATS: Final[tuple[str, ...]] = ("human", "json", "ndjson")

# Checked here, by the same mechanism and in the same function as `ALLOWED_FORMATS`, because
# `Settings.color` is declared `Literal["auto", "always", "never"]` and `want_color` falls
# through anything it does not recognise to `stream.isatty()`. Unvalidated, `--color=nevr`
# and `--color=off` are accepted and silently mean "auto": a caller who asked for plain text
# gets escape codes and an exit status that says nothing was wrong.
ALLOWED_COLORS: Final[tuple[str, ...]] = ("auto", "always", "never")

# The built-in defaults `build_settings` applies at the bottom of `pick()`, named here
# rather than written as literals inside the call, because `cli/_meta.py` publishes them as
# the `default` of `--format` and `--verbose` in the manifest. `--target`'s lives in
# `config.DEFAULT_TARGET`, beside the `Config` field that carries the same value. A manifest
# that says `null` where the CLI has a default is a documented lie about what a caller gets
# when they type nothing.
DEFAULT_FORMAT: Final[str] = "human"
DEFAULT_VERBOSE: Final[int] = 0

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

    def __init__(
        self,
        message: str,
        *,
        suggestions: list[list[str]],
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        # The same field `RailctlError` carries, for the same reason: the prose
        # of `message` is free to change and a caller must not have to parse it
        # to learn WHICH condition fired. `_errors.usage_report` publishes this
        # in the envelope's `details`.
        self.details = dict(details or {})
        # Checked, not merely annotated. The envelope hands this field straight to an agent
        # as something to run, and a bare string used to arrive there split into
        # `[["r"], ["a"], ["i"], ["l"], ...]` - valid JSON that runs nothing. Failing here
        # puts the error at the line that wrote the bad value; `_errors._argv_arrays` is the
        # second half, for a `ValueError` from somewhere this project does not control.
        if not isinstance(suggestions, list) or not all(
            isinstance(argv, list) and all(isinstance(word, str) for word in argv)
            for argv in suggestions
        ):
            raise TypeError(
                f"suggestions must be a list of argv arrays (list[list[str]]), got "
                f"{suggestions!r}. One list per runnable command, one string per argument."
            )
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
    # What the ROOT level actually had TYPED on the command line, kept beside the resolved
    # `fmt` so `merge_settings` can see both sides of the verb at once - see
    # `check_format_conflict`. Only the command line: the environment and the config file
    # stay out of these two fields, because `--json` is documented to win over
    # RAILCTL_FORMAT and folding the variable in here would turn that documented precedence
    # into a refusal. They default to "nothing was typed" so a test building a `Settings` by
    # hand describes the ordinary case without naming them.
    fmt_flag: str | None = None
    json_flag: bool = False


def check_choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    """Reject a value outside `allowed`, with one message shared by both callers.

    `build_settings` validates what the root callback resolved; `merge_settings` validates
    what a command's own copy of the same flag carried. Written twice, the two messages drift,
    and `railctl --format xml status` and `railctl status --format xml` start explaining the
    same mistake in two different sentences - which is exactly the parity M6 is judged on.
    """
    if value not in allowed:
        raise ValueError(f"--{name} must be one of {allowed}, got {value!r}")


def checked_enum(
    value: str, *, name: str, allowed: Sequence[str], suggestions: list[list[str]]
) -> str:
    """A positional enum, validated HERE and refused through OUR envelope.

    `typer_argument` attaches no Click-level check, for the same reason
    `typer_option` attaches no `callback=`: a `typer.BadParameter` exits through
    Click's own usage box and never emits `railctl/error/v1`. That leaves the
    check to the command body, and `power sideways` was silently running `power
    off` until someone noticed - the enum was published metadata that nothing
    enforced.

    `allowed` is passed in from the `Argument` row's own `enum` tuple, never
    retyped, so the list a caller reads out of `railctl schema` is the list this
    function compares against.
    """
    if value in allowed:
        return value
    raise UsageProblem(
        f"{name} takes {' or '.join(allowed)}, not {value!r}",
        suggestions=suggestions,
        details={"argument": name, "allowed": list(allowed), "got": value},
    )


def check_address(value: int | None) -> None:
    """Reject a locomotive address outside the bound the manifest publishes.

    One function, called wherever an address ENTERS this CLI: the root
    callback's copy through `build_settings`, a command's own `--address`
    through `merge_settings`, and `stop`'s command-scoped `--address`, which
    deliberately goes through neither. The check used to live only in
    `build_settings`, so the bound the manifest advertises and the bound a
    command enforced differed by where the flag was typed - `railctl --address
    20000 status` was refused at exit 2 and `railctl power on --address 20000`
    was not, and the range check `Station.drive` does on its own arrives after
    two mutations have already gone out.

    `None` passes: no address is a real answer, and `require_address` is what
    turns "none given" into a usage error for the commands that need one.
    """
    if value is not None and not (LOCO_ADDR_MIN <= value <= LOCO_ADDR_MAX):
        raise ValueError(f"--address {value} is outside {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}")


def check_format_conflict(*, json_flag: bool, fmt: str | None) -> None:
    """`--json` is an alias for `--format=json`, so it is folded into the same CLI-flag slot
    `pick()` sees - not a second, competing source. Passing both `--format=ndjson` and
    `--json` is a real conflict, not "last flag wins": on a CLI that drives a running train,
    silently picking one of two contradictory instructions is worse than refusing to guess.

    Both callers pass the union of the two flag positions, never one level's copy alone.
    Compared per level, `railctl --format ndjson status --json` and
    `railctl --json status --format ndjson` were each accepted and each silently produced
    the other format - so a wrapper that pins a house format in a prefix array and appends
    `--json` per call got JSON where it asked for NDJSON, and its line reader never found a
    `summary` event.
    """
    if json_flag and fmt is not None and fmt != "json":
        raise ValueError(f"--json conflicts with --format={fmt}; pass only one")


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
        target, env.get("RAILCTL_TARGET"), config.target, DEFAULT_TARGET, name="target", cast=str
    )

    resolved_address = pick(
        address, env.get("RAILCTL_ADDRESS"), config.address, None, name="address", cast=int
    )
    check_address(resolved_address)

    resolved_verbose = pick(
        verbose, env.get(VERBOSE_ENV), config.verbose, DEFAULT_VERBOSE, name="verbose", cast=int
    )

    check_format_conflict(json_flag=json_flag, fmt=fmt)
    format_flag = "json" if json_flag else fmt
    resolved_format = pick(
        format_flag, env.get("RAILCTL_FORMAT"), None, DEFAULT_FORMAT, name="format", cast=str
    )
    check_choice("format", resolved_format, ALLOWED_FORMATS)
    check_choice("color", color, ALLOWED_COLORS)

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
        fmt_flag=fmt,
        json_flag=json_flag,
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

    A value that IS typed here is checked by the same two functions
    `build_settings` uses, so `railctl status --format xml` fails with the same
    message, the same `usage` code and the same exit 2 as
    `railctl --format xml status`. Unchecked, the command-level copy was the
    hole: nothing else validates it, `render()` falls through an unknown format
    to its NDJSON branch and `want_color` falls through an unknown colour to
    `isatty()`, so a typo silently changed the output instead of refusing it.
    """
    updates: dict[str, object] = {}
    if target is not None:
        updates["target"] = target
    if address is not None:
        check_address(address)
        updates["address"] = address
    # The union of both flag positions, not this level's copy alone: `--format` before the
    # verb and `--json` after it are one contradiction, however they are spread around the
    # command name. `base.fmt` cannot stand in for `base.fmt_flag` here - it defaults to
    # "human", so comparing `--json` against it would refuse the ordinary
    # `railctl status --json`.
    check_format_conflict(
        json_flag=json_flag or base.json_flag,
        fmt=fmt if fmt is not None else base.fmt_flag,
    )
    resolved_fmt = "json" if json_flag else fmt
    if resolved_fmt is not None:
        check_choice("format", resolved_fmt, ALLOWED_FORMATS)
        updates["fmt"] = resolved_fmt
    if verbose > 0:
        updates["verbose"] = verbose
    if color is not None:
        check_choice("color", color, ALLOWED_COLORS)
        updates["color"] = color
    if yes:
        updates["assume_yes"] = True
    if non_interactive:
        updates["interactive"] = False
    if not updates:
        return base
    return dataclasses.replace(base, **updates)


def context_for(settings: Settings, *, stdout: TextIO, stderr: TextIO) -> OutputContext:
    """One `--color` value, but `want_color` is asked once per stream.

    The design spec requires stdout and stderr to be tested separately. Deciding once off
    stdout and painting both is how `railctl status 2> errors.log` run from a terminal ends
    up writing escape codes into the log; the converse - stdout redirected, stderr still on
    the operator's terminal - strips the colour off the one line they are meant to read.

    Lives here rather than in `main.py` because `merged_output` below needs it too, and a
    command module cannot import `main` (which imports every command module in turn). Two
    copies of a two-stream decision is how one of them quietly goes back to deciding once.
    """
    return OutputContext(
        fmt=settings.fmt,
        stdout_color=want_color(settings.color, stdout, os.environ),
        stderr_color=want_color(settings.color, stderr, os.environ),
        stdout=stdout,
        stderr=stderr,
    )


def merged_output(
    base: Settings,
    streams: OutputContext,
    *,
    target: str | None = None,
    address: int | None = None,
    fmt: str | None = None,
    json_flag: bool = False,
    verbose: int = 0,
    color: str | None = None,
    yes: bool = False,
    non_interactive: bool = False,
) -> tuple[Settings, OutputContext]:
    """Layer a command's own copy of the eight global options over `base`, then rebuild
    everything the root callback derived from them.

    Every registered command declares all eight global options a second time, because Click
    parses a group's own options only BEFORE the subcommand name - without the copy,
    `railctl status --address 3` is a usage error before `status` ever runs. This is the other
    half of that: a flag accepted after the verb has to actually take effect, or the copy is
    decoration. Rebuilding is unconditional rather than "only when something changed" -
    `merge_settings` returns `base` itself when nothing was typed, so the rebuild then costs
    one identical `OutputContext` instead of a second branch to get wrong.

    Logging and `RAILCTL_VERBOSE` are re-derived for the same reason: the root callback wrote
    them from the root's own `-v`, so without this `railctl status -vv` would resolve
    `verbose=2` and still print neither decoded diagnostics nor the traceback the flag exists
    to produce. Written after resolution, never before - see `main.global_options`.
    """
    settings = merge_settings(
        base,
        target=target,
        address=address,
        fmt=fmt,
        json_flag=json_flag,
        verbose=verbose,
        color=color,
        yes=yes,
        non_interactive=non_interactive,
    )
    configure_logging(settings.verbose, streams.stderr)
    os.environ[VERBOSE_ENV] = str(settings.verbose)
    return settings, context_for(settings, stdout=streams.stdout, stderr=streams.stderr)


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


def read_loco(station: Station, address: int) -> LocoInfo | None:
    """The locomotive's current state, or None when the station could not say.

    None means UNKNOWN, never "standing still": every caller branches on it
    separately rather than folding it into a default speed of 0.

    The catch is `RailctlError`, not `StationError`. Every read that reaches
    this function is cosmetic - a `changed` field, a direction to preserve, a
    running notice - and none of it may decide whether a mutation goes out.
    `LinkTimeout`, `TransportError`, `ProtocolError`, `XBusChecksumError` and
    `UnsupportedCommandError` all subclass `RailctlError` directly rather than
    `StationError`, so the narrower catch let a `61 82` refusal of the loco-info
    request - a working link, a healthy track - abort a command whose own
    telegram would have gone straight through.

    NOT `except Exception`. An address outside 1..9999 raises `ValueError` from
    `Station._validate_address`, and that has to keep failing: it says the
    caller asked about a locomotive that cannot exist, which is a different
    answer from the station declining to describe one that can.

    Lives here rather than in `commands/throttle.py` because `commands/power.py`
    needs the same guarantee for the direction it preserves, and one command
    module importing another is how the two copies start disagreeing about
    which exceptions are cosmetic.
    """
    try:
        return station.loco_info(address)
    except errors.RailctlError:
        return None


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
