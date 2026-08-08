# src/railctl/cli/commands/throttle.py
"""The `drive` and `function` commands, and the `preflight` guard a later
task's `cv` commands import too.

Nothing here holds an opcode, a framing byte or CV arithmetic: every mutation
goes through a `Station` facade method, and the only wire-adjacent names are
`Direction` and an integer function number, both of which the design allows
through `cli/` (tests/test_layering.py rule 1).

`status()` is read as a decoded `StationStatus` and never as a raw byte
compared against a literal mask. The measured bit order on this hardware is
bit 0 emergency stop, bit 1 emergency off - the reverse of the Lenz spec - and
that fact is written down exactly once, in `xbus/replies.py`. This module reads
the field names, so a correction there reaches the refusals below without an
edit.
"""

from __future__ import annotations

from typing import Any, Final, Literal

import typer

from railctl.cli._errors import run
from railctl.cli._meta import (
    DRIVE_FORWARD_OPT,
    DRIVE_REVERSE_OPT,
    DRIVE_SPEED_ARG,
    FUNCTION_FORCE_GROUP_OPT,
    FUNCTION_FUNC_ARG,
    FUNCTION_STATE_ARG,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    DIRECTION_TEXT,
    UsageProblem,
    merged_output,
    open_station,
    read_loco,
    require_address,
)
from railctl.cli.result import CommandResult, tri_state
from railctl.errors import (
    FunctionGroupUnreadableError,
    StationBusyError,
    StationError,
    TrackPowerError,
)
from railctl.station import Station
from railctl.xbus.replies import LocoInfo, StationStatus
from railctl.xbus.speed import Direction

_DRIVE_META = command_meta("drive")
_FUNCTION_META = command_meta("function")

# Read off the metadata row, never retyped - the manifest says what a command
# emits and the envelope says what it emitted, and two literals drift.
DRIVE_SCHEMA: Final[str] = _DRIVE_META.schema
FUNCTION_SCHEMA: Final[str] = _FUNCTION_META.schema

FUNCTION_ALIASES: Final[dict[str, int]] = {"light": 0, "lights": 0, "headlight": 0}
RUNNING_NOTICE: Final[str] = (
    "loco {address} is running at step {speed} {direction}; "
    "it keeps running after this command exits"
)

MAX_FUNCTION: Final[int] = 28
#: Printed instead of a direction word when the reply carried none. A 14/27/28
#: step reply leaves `direction` None because `speed.py` decodes only the
#: 128-step layout; saying "forward" there would report a decode this tool
#: never performed as a measured fact.
_UNKNOWN_DIRECTION: Final[str] = "unknown direction"

#: The running reminder for a reply railctl could not decode a speed from. The
#: guard used to be `not info.speed`, which folded UNKNOWN into "standing
#: still" and skipped the notice for every 14/27/28-step decoder - the one
#: locomotive most likely to be moving unnoticed, because the same reply mode
#: is why `drive` cannot read its direction either.
UNKNOWN_SPEED_NOTICE: Final[str] = (
    "loco {address} is running: {state} - the station answered in {steps} speed steps and "
    "railctl decodes a speed only from the 128-step reply; whatever it was doing, it keeps "
    "doing after this command exits"
)

#: The `hint` the facade sets on the two `StationError`s that mean "the current
#: function group could not be read" - and on no other. `Station._expect_ack`
#: raises the same class after the group telegram has already been sent, so the
#: hint is what tells a failure BEFORE the write from one after it.
FORCE_GROUP_HINT: Final[str] = "--force-group"

#: Where the direction on the wire came from. Published in the envelope because
#: "forward" alone does not say whether the station reported it, the operator
#: typed it, or the stop path chose it without reading anything.
DIRECTION_FROM_FLAG: Final[str] = "flag"
DIRECTION_KEPT: Final[str] = "kept"
DIRECTION_STOP_DEFAULT: Final[str] = "stop-default"

# Built once, at import time, into names the signatures below reference - a
# call to `global_option(...)`/`typer_option(...)`/`typer_argument(...)`
# written directly as a parameter default would trip Ruff's B008 (function call
# in a default argument); its built-in allowlist covers a literal
# `typer.Option(...)`/`typer.Argument(...)` call, not a wrapper around one.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_SPEED_ARG = typer_argument(DRIVE_SPEED_ARG)
_REVERSE = typer_option(DRIVE_REVERSE_OPT)
_FORWARD = typer_option(DRIVE_FORWARD_OPT)
_FUNC_ARG = typer_argument(FUNCTION_FUNC_ARG)
_STATE_ARG = typer_argument(FUNCTION_STATE_ARG)
_FORCE_GROUP = typer_option(FUNCTION_FORCE_GROUP_OPT)


def preflight(station: Station, *, speed: int | None) -> StationStatus:
    """Guard for anything that could start or continue motion: `drive SPEED>0`,
    `function`, and (from a later task) every POM `cv` command.

    Never called for `drive 0` - the caller below skips it outright, because a
    stop must never be refused by a status this check dislikes. `speed` is only
    used to phrase the refusal; it is None for `function`, where there is no
    single speed value to name.

    The reason this exists at all is that a refused command is not the same as
    a command that did nothing. Without the power check the speed is accepted
    into the station's refresh buffer and the locomotive starts by itself the
    moment power returns - which is what happened to the doctor's D3 run
    (docs/probe-results.md).
    """
    status = station.status()
    if status.emergency_off or status.emergency_stop:
        target = f"speed {speed}" if speed is not None else "this command"
        raise TrackPowerError(
            f"track power is off or the layout is in emergency stop; refusing to send "
            f"{target}. Run `railctl power on` first."
        )
    if status.service_mode:
        raise StationBusyError(
            "a service-mode programming session is active on this station; it must "
            "finish or be cancelled before a throttle command can run"
        )
    return status


def parse_function(token: str) -> int:
    """`"f2"`, `"2"` or an alias in `FUNCTION_ALIASES` -> 0..28."""
    lowered = token.strip().lower()
    if lowered in FUNCTION_ALIASES:
        return FUNCTION_ALIASES[lowered]
    digits = lowered[1:] if lowered.startswith("f") else lowered
    if not digits.isdigit():
        raise ValueError(
            f"'{token}' is not a function: use f0..f{MAX_FUNCTION}, a bare number in "
            f"0..{MAX_FUNCTION}, or one of {sorted(FUNCTION_ALIASES)}"
        )
    value = int(digits)
    if not 0 <= value <= MAX_FUNCTION:
        raise ValueError(f"function {value} is out of range 0..{MAX_FUNCTION}")
    return value


def parse_state(token: str | None) -> Literal["on", "off", "toggle"]:
    """`None` (the argument was omitted) defaults to `"on"`."""
    if token is None:
        return "on"
    lowered = token.strip().lower()
    if lowered not in ("on", "off", "toggle"):
        raise ValueError(f"'{token}' is not a state: use on, off or toggle")
    return lowered  # type: ignore[return-value]


def build_drive(
    address: int,
    speed: int,
    direction: Direction,
    *,
    was: LocoInfo | None,
    direction_source: str,
) -> CommandResult:
    if was is None:
        changed: bool | None = None
        previous_speed_decoded: bool | None = None
    elif was.speed is None:
        # `was.speed` is None exactly when `was.speed_steps != 128` - speed.py
        # defines only the 128-step layout and replies.py leaves the rest
        # UNKNOWN rather than decode it wrong. Reporting a number here would be
        # the capability-recorded-as-absent failure this project exists to
        # avoid, aimed at `changed` instead of at a CV read.
        changed = None
        previous_speed_decoded = False
    else:
        changed = (was.speed, was.direction) != (speed, direction)
        previous_speed_decoded = True

    direction_text = DIRECTION_TEXT[direction]
    outcome = CommandResult(schema=DRIVE_SCHEMA, command="drive")
    outcome.result = {
        "address": address,
        "speed": speed,
        "direction": direction_text,
        "direction_source": direction_source,
        "changed": changed,
        "previous_speed_decoded": previous_speed_decoded,
        # Decoded off the ident byte and then dropped. It says another throttle
        # holds this locomotive - exactly what an operator needs to know before
        # commanding it, and the one field here whose UNKNOWN (no reply, or a
        # stop that reads nothing) is a different answer from `false`.
        "in_use_by_other": None if was is None else was.in_use_by_other,
    }
    outcome.say(
        f"loco {address} set to speed {speed} {direction_text} ({tri_state(changed)} changed)"
    )
    if was is not None and was.in_use_by_other:
        outcome.warn(
            "loco_in_use_by_other",
            f"another throttle holds loco {address}; this command was sent anyway and the "
            f"two of you are now driving the same locomotive",
            address=address,
        )
    if direction_source == DIRECTION_STOP_DEFAULT:
        outcome.say(
            "the stop was sent forward without reading the locomotive first - a stop never "
            "waits on a read - so this may have changed its stored direction"
        )
    if previous_speed_decoded is False:
        outcome.say(
            "the locomotive's previous speed step mode is not 128-step and was not "
            "decoded, so whether this changed its speed is unknown"
        )
    elif previous_speed_decoded is None:
        outcome.say("the locomotive's previous state could not be read")
    return outcome


def build_function(address: int, function: int, state: str, *, now_on: bool) -> CommandResult:
    outcome = CommandResult(schema=FUNCTION_SCHEMA, command="function")
    outcome.result = {
        "address": address,
        "function": function,
        "requested": state,
        "now_on": now_on,
    }
    outcome.say(
        f"loco {address} F{function} is now {'on' if now_on else 'off'} (requested {state})"
    )
    return outcome


def _steps_text(info: LocoInfo) -> str:
    """The reply's speed-step mode as a word, never a guessed number."""
    return "an unrecognised number of" if info.speed_steps is None else str(info.speed_steps)


def _typed_direction(
    reverse: bool | None, forward: bool | None, *, argv_hint: list[str]
) -> Direction | None:
    """The direction the OPERATOR typed, or None when they typed neither."""
    if reverse and forward:
        raise UsageProblem(
            "--forward and --reverse contradict each other; pass one",
            suggestions=[[*argv_hint, "--forward"], [*argv_hint, "--reverse"]],
            details={"reason": "contradictory_direction_flags"},
        )
    if reverse:
        return Direction.REVERSE
    if forward:
        return Direction.FORWARD
    return None


def _direction_for(
    typed: Direction | None, was: LocoInfo | None, *, address: int, argv_hint: list[str]
) -> Direction:
    """The direction a POSITIVE speed goes out with: what the operator typed,
    or the direction the locomotive is already running (the spec's worked
    example, `railctl drive 30 --address 3  # keeps current direction`).

    When there is neither, this REFUSES. It used to answer `Direction.FORWARD`,
    which is this project's founding rule broken at the one place where it
    moves a train: `replies.py` leaves `direction` None for every 14/27/28-step
    `loco_info` reply because `speed.py` decodes only the 128-step layout, and
    an undecoded direction was then sent to the track as a measured one. On a
    decoder the station reports in 28 steps, `railctl drive 40` with no flag
    sent FORWARD to a locomotive that may have been running in reverse.

    Unknown is an answer. Saying so and naming the flag that settles it costs
    the operator one word and costs a guess nothing to make.
    """
    if typed is not None:
        return typed
    if was is not None and was.direction is not None:
        return was.direction
    if was is None:
        message = (
            f"loco {address}'s current direction could not be read, so there is no direction "
            f"to keep; pass --forward or --reverse to say which way this speed runs"
        )
        details: dict[str, object] = {"reason": "direction_unread", "speed_steps": None}
    else:
        message = (
            f"the station answered for loco {address} in {_steps_text(was)} speed steps, and "
            f"railctl decodes a direction only from the 128-step reply, so its current "
            f"direction is unknown; pass --forward or --reverse to say which way this speed "
            f"runs"
        )
        details = {"reason": "direction_undecoded", "speed_steps": was.speed_steps}
    raise UsageProblem(
        message,
        suggestions=[[*argv_hint, "--forward"], [*argv_hint, "--reverse"]],
        details=details,
    )


def _warn_if_running(info: LocoInfo | None, address: int, stderr: Any) -> None:
    """The running reminder, on stderr, whenever a command leaves a locomotive
    moving - or whenever railctl cannot tell whether it did.

    stdout carries the result only, so this never reaches the envelope in any
    format. The guard was `not info.speed`, which is True for `speed is None`
    as well as for 0 - so UNKNOWN was folded into "standing still" and the
    notice was silently skipped for every non-128-step reply.
    """
    if info is None:
        return
    if info.speed is None:
        print(
            UNKNOWN_SPEED_NOTICE.format(
                address=address, state=tri_state(None), steps=_steps_text(info)
            ),
            file=stderr,
        )
        return
    if info.speed == 0:
        return
    direction_text = (
        DIRECTION_TEXT[info.direction] if info.direction is not None else _UNKNOWN_DIRECTION
    )
    print(
        RUNNING_NOTICE.format(address=address, speed=info.speed, direction=direction_text),
        file=stderr,
    )


def register(app: typer.Typer) -> None:
    """Attach `drive` and `function` to `app`, in that order.

    Both redeclare all eight global options alongside their own arguments -
    Click parses a subcommand's own options anywhere after its name, but a
    `@app.callback()` group option only before it, and the spec's own worked
    session types `--address` after `drive`. `global_option` builds each one
    with a `None`/`False`/`0` sentinel default and no envvar (the root callback
    already resolved the environment once), and `merged_output` layers only the
    ones actually typed here over `ctx.obj.settings` before rebuilding the
    output context from the result.
    """

    @app.command("drive", help=_DRIVE_META.help, epilog=help_epilog(_DRIVE_META))
    def drive_command(
        ctx: typer.Context,
        speed: int = _SPEED_ARG,
        reverse: bool | None = _REVERSE,
        forward: bool | None = _FORWARD,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings, output = merged_output(
            cli_ctx.settings,
            cli_ctx.output,
            target=target,
            address=address,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            argv_hint = ["railctl", "drive", str(speed)]
            resolved = require_address(settings, argv_hint=argv_hint)
            typed = _typed_direction(
                reverse, forward, argv_hint=[*argv_hint, "--address", str(resolved)]
            )
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                # THE ONE BRANCH THAT MUST NOT ACQUIRE A CONDITION: speed 0 is
                # sent whatever the station's status says, and without reading
                # anything at all first. A stop that needs permission is not a
                # stop, and neither is one a cosmetic read can veto - the
                # pre-read used to run above this line, so a loco-info reply
                # the station refused or never sent aborted the panic command
                # over a `changed` field nobody was waiting for.
                #
                # The pre-flight runs BEFORE the pre-read, not after it: on a
                # station that refuses everything the operator is owed the
                # refusal the status read produced, not a second-order "the
                # direction could not be read".
                was: LocoInfo | None = None
                if speed > 0:
                    preflight(station, speed=speed)
                    was = read_loco(station, resolved)
                    direction = _direction_for(
                        typed,
                        was,
                        address=resolved,
                        argv_hint=[*argv_hint, "--address", str(resolved)],
                    )
                    source = DIRECTION_FROM_FLAG if typed is not None else DIRECTION_KEPT
                elif typed is not None:
                    direction, source = typed, DIRECTION_FROM_FLAG
                else:
                    # A speed telegram always carries a direction bit, so the
                    # stop has to send one, and it reads nothing first. Forward
                    # is what goes out - not because the locomotive was
                    # measured running forward, but because a stop cannot wait
                    # on a reply. `build_drive` says exactly that.
                    direction, source = Direction.FORWARD, DIRECTION_STOP_DEFAULT
                station.drive(resolved, speed, direction)
                if speed:
                    print(
                        RUNNING_NOTICE.format(
                            address=resolved,
                            speed=speed,
                            direction=DIRECTION_TEXT[direction],
                        ),
                        file=output.stderr,
                    )
                return build_drive(resolved, speed, direction, was=was, direction_source=source)
            finally:
                station.close()

        run("drive", output, work)

    @app.command("function", help=_FUNCTION_META.help, epilog=help_epilog(_FUNCTION_META))
    def function_command(
        ctx: typer.Context,
        function: str = _FUNC_ARG,
        state: str | None = _STATE_ARG,
        force_group: bool = _FORCE_GROUP,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings, output = merged_output(
            cli_ctx.settings,
            cli_ctx.output,
            target=target,
            address=address,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            func_num = parse_function(function)
            wanted_state = parse_state(state)
            resolved = require_address(
                settings, argv_hint=["railctl", "function", function, wanted_state]
            )
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                preflight(station, speed=None)
                try:
                    if wanted_state == "toggle":
                        now_on = station.function_toggle(
                            resolved, func_num, force_group=force_group
                        )
                    else:
                        on = wanted_state == "on"
                        station.function_set(resolved, func_num, on, force_group=force_group)
                        now_on = on
                except StationError as exc:
                    # The facade reads the current group before flipping one
                    # bit, because a group command carries every bit of its
                    # group and setting one function without reading the rest
                    # would silently clear them. It raises a bare StationError
                    # with no structured retry information; this CLI layer is
                    # what attaches the --force-group escape as a
                    # machine-readable suggestion, since `default_suggestions`
                    # is keyed by exception type and never sees the function or
                    # state tokens the operator typed.
                    #
                    # Only the READ. `Station._expect_ack` raises a bare
                    # `StationError` too, AFTER the group telegram has gone out,
                    # and this block reported that as "could not read the
                    # current state" - a message about a read, for a failure in
                    # a write, offering a retry flag that skips a read which had
                    # already happened. `FORCE_GROUP_HINT` is what the facade
                    # sets on the two raises this wrapper is for, and both name
                    # the CLI flag by the name it has here.
                    if getattr(exc, "hint", None) != FORCE_GROUP_HINT:
                        raise
                    raise FunctionGroupUnreadableError(
                        f"could not read the current state of F{func_num} on loco "
                        f"{resolved}: {exc}",
                        retry_argv=[
                            "railctl",
                            "function",
                            function,
                            wanted_state,
                            "--address",
                            str(resolved),
                            "--force-group",
                        ],
                    ) from exc
                result = build_function(resolved, func_num, wanted_state, now_on=now_on)
                _warn_if_running(read_loco(station, resolved), resolved, output.stderr)
                return result
            finally:
                station.close()

        run("function", output, work)
