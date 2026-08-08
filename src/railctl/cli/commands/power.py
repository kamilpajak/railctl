# src/railctl/cli/commands/power.py
"""The `power` and `stop` commands.

`stop`'s own `--address` is deliberately NOT the same value as the global
`--address` / `RAILCTL_ADDRESS` / config default that `drive` and `function`
read through `settings.address`. A user with `address = 3` configured who hits
the panic button `railctl stop` means "stop everything"; falling back to the
convenient default would stop only locomotive 3. `stop` therefore ignores
`settings.address` entirely and narrows to one locomotive only when
`--address` is typed on the `stop` invocation itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import typer

from railctl.cli._errors import run
from railctl.cli._meta import (
    POWER_STATE_ARG,
    STOP_ADDRESS_OPT,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    DIRECTION_TEXT,
    check_address,
    checked_enum,
    merged_output,
    open_station,
    read_loco,
)
from railctl.cli.result import PARTIAL_EXIT_CODE, CommandResult, error_code
from railctl.errors import RailctlError
from railctl.station import Station
from railctl.xbus.replies import StationStatus
from railctl.xbus.speed import Direction

_POWER_META = command_meta("power")
_STOP_META = command_meta("stop")

POWER_SCHEMA: Final[str] = _POWER_META.schema
STOP_SCHEMA: Final[str] = _STOP_META.schema

#: The two states `power` accepts, read off the metadata row so the manifest's
#: `enum` and the check that enforces it are one tuple rather than two that
#: agree today. `typer_argument` builds no Click-level check for an `enum` row,
#: for the same reason `typer_option` builds no `callback=`: a
#: `typer.BadParameter` exits through Click's own usage box and never emits the
#: `railctl/error/v1` envelope the rest of this CLI is judged on.
POWER_STATES: Final[tuple[str, ...]] = POWER_STATE_ARG.enum or ()

# Built once, at import time - see the identical B008 note in basics.py.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_STATE_ARG = typer_argument(POWER_STATE_ARG)
_STOP_ADDRESS = typer_option(STOP_ADDRESS_OPT)


@dataclass(frozen=True, slots=True)
class Idled:
    """What the speed-0 telegram `power on` sends to `--address` carried.

    `direction_preserved` is the honest half. `power on` used to send
    `Direction.FORWARD` unconditionally, so a locomotive stored in reverse came
    back forward - a change nobody asked for, from a command whose name says
    nothing about direction. It is preserved when the station answers with one,
    and when it does not this says so instead of presenting the fallback as the
    locomotive's own direction.
    """

    address: int
    direction: Direction
    direction_preserved: bool


def build_power(
    state: str,
    status: StationStatus,
    *,
    changed: bool,
    idled: Idled | None,
    completed: Sequence[str],
) -> CommandResult:
    outcome = CommandResult(schema=POWER_SCHEMA, command="power")
    outcome.result = {
        "state": state,
        "track_power": status.track_power,
        # Read and then discarded before: `power on` decided the whole
        # question with `track_power` and `auto_start_mode`, and these two are
        # what decide whether anything can actually move. Bit 0 is emergency
        # STOP and bit 1 is emergency OFF on this hardware, the reverse of the
        # Lenz spec - measured, docs/probe-results.md. Read by field name, so a
        # correction there reaches this envelope with no edit here.
        "emergency_stop": status.emergency_stop,
        "emergency_off": status.emergency_off,
        "auto_start_mode": status.auto_start_mode,
        "changed": changed,
        "idled_address": None if idled is None else idled.address,
        "idled_direction": None if idled is None else DIRECTION_TEXT[idled.direction],
        "idled_direction_preserved": None if idled is None else idled.direction_preserved,
        # The same two keys `build_power_partial` fills in, so one schema
        # describes both endings and a caller reads "what ran" off the same
        # field whichever it got.
        "completed": list(completed),
        "failed_step": None,
    }
    outcome.say(
        f"track power is {'on' if status.track_power else 'off'} "
        f"({'changed' if changed else 'no change'})"
    )
    # Start mode is bit 2, and it is named exactly once, in
    # `xbus/replies.py`, as `auto_start_mode`. This reads that field rather
    # than retyping the bit, so a correction there cannot leave this wording
    # disagreeing with `build_status`'s. It is never "short circuit": neither
    # manual defines any status bit that way.
    if status.auto_start_mode:
        outcome.say(
            "start mode is automatic: locomotives resume their last speed as soon as power returns"
        )
    else:
        outcome.say("start mode is manual: locomotives stay stopped until driven")
    _say_emergency_state(outcome, state, status)
    if idled is not None:
        direction_text = DIRECTION_TEXT[idled.direction]
        outcome.say(
            f"loco {idled.address} was sent speed 0 {direction_text} so it does not move on its own"
        )
        if not idled.direction_preserved:
            outcome.warn(
                "direction_not_preserved",
                f"loco {idled.address}'s stored direction could not be read, so the speed-0 "
                f"telegram went out {direction_text}; if it was running the other way, that "
                f"is now changed",
                address=idled.address,
                sent=direction_text,
            )
    return outcome


def _say_emergency_state(outcome: CommandResult, state: str, status: StationStatus) -> None:
    """The two bits that decide whether the layout can move, in the human text
    - and as a warning when `power on` left one of them set.

    Emergency stop is the one this exists for: it leaves the track powered, so
    `track_power: true` alone reads as success while every locomotive is held
    at standstill. An operator who runs `power on`, sees nothing move and has
    to run a second command to find out why was told half the answer by the
    command they already ran.
    """
    if status.emergency_stop:
        outcome.say(
            "emergency stop is active: the track has voltage and every locomotive is held "
            "at standstill"
        )
    if status.emergency_off:
        outcome.say("emergency off is active: the track has no voltage")
    if state == "on" and (status.emergency_stop or status.emergency_off):
        outcome.warn(
            "layout_cannot_move",
            "the station still reports an emergency state after power on, so nothing will "
            "move until it is cleared",
            emergency_stop=status.emergency_stop,
            emergency_off=status.emergency_off,
        )


def build_power_partial(*, completed: Sequence[str], failure: BaseException) -> CommandResult:
    """`power on` got past `power_on()` and then failed. Exit 8, not 9.

    Three mutations run in sequence and the second one energises the track. A
    failure after that went to `run()`'s generic handler, which told the caller
    the command failed - while the layout was now live and possibly with a
    locomotive not yet idled. Those are different situations and a script has
    to be able to act on the difference, so this is a RESULT carrying what
    completed, not an error carrying a message about what did not.

    `track_power` and the status bits are `null`, never `false`: the status
    read is one of the steps that may have failed, and reporting a track this
    command just energised as unpowered would be the founding rule broken in
    the report of a failure to follow it.
    """
    failed_step = STEP_READ_STATUS if STEP_READ_STATUS not in completed else STEP_IDLE_ADDRESS
    outcome = CommandResult(
        schema=POWER_SCHEMA, command="power", ok=False, exit_code=PARTIAL_EXIT_CODE
    )
    outcome.result = {
        "state": "on",
        "track_power": None,
        "emergency_stop": None,
        "emergency_off": None,
        "auto_start_mode": None,
        "changed": True,
        "idled_address": None,
        "idled_direction": None,
        "idled_direction_preserved": None,
        "completed": list(completed),
        "failed_step": failed_step,
    }
    outcome.warn(
        "power_on_incomplete",
        f"the track was switched on and then {failed_step} failed: {failure}",
        completed=list(completed),
        failed_step=failed_step,
        error_code=error_code(failure),
    )
    outcome.say(f"track power was switched on; {failed_step} did not complete")
    return outcome


def build_stop(address: int | None) -> CommandResult:
    outcome = CommandResult(schema=STOP_SCHEMA, command="stop")
    outcome.result = {"address": address, "scope": "single" if address is not None else "all"}
    outcome.say(f"loco {address} stopped" if address is not None else "all locomotives stopped")
    return outcome


#: The steps `power on` and `power off` run, named so the envelope can report
#: which of them completed. `power on` is the one that needs it: its second
#: step energises the track, so "the command failed" stops being the whole
#: answer from that point on.
STEP_STOP_ALL: Final[str] = "stop_all"
STEP_POWER_ON: Final[str] = "power_on"
STEP_READ_STATUS: Final[str] = "read_status"
STEP_IDLE_ADDRESS: Final[str] = "idle_address"
STEP_POWER_OFF: Final[str] = "power_off"


def _power_on(station: Station, address: int | None) -> CommandResult:
    # MEASURED (docs/probe-results.md): this station's start mode is automatic,
    # and a stored speed does resume when power returns - that is what the
    # doctor's D3 run drove a locomotive with, and what the backup relay note
    # warns about. NOT MEASURED: whether the stop-all prefix is what clears
    # that stored speed. Nobody has captured what the station's refresh buffer
    # holds across a power cycle, so the prefix is a precaution taken on the
    # strength of the first fact, not a proven remedy. Only the last step below
    # - speed 0 to the resolved address - is proven.
    station.emergency_stop(address=None)
    station.power_on()
    completed = [STEP_STOP_ALL, STEP_POWER_ON]
    # The track is live from here. Anything that fails below is PARTIAL, and
    # the caller has to be able to tell that from "nothing happened".
    try:
        status = station.status()
        completed.append(STEP_READ_STATUS)
        idled = _idle(station, address)
        if idled is not None:
            completed.append(STEP_IDLE_ADDRESS)
    except RailctlError as exc:
        return build_power_partial(completed=completed, failure=exc)
    return build_power("on", status, changed=True, idled=idled, completed=completed)


def _power_off(station: Station) -> CommandResult:
    """The one of the two that reads status first: skipping its only mutation
    when nothing needs doing is exactly what `changed: false` is for. `power
    on` cannot do the same, because its own first call is the stop-all."""
    before = station.status()
    completed = [STEP_READ_STATUS]
    was_on = before.track_power
    if was_on:
        station.power_off()
        completed.append(STEP_POWER_OFF)
        after = station.status()
    else:
        after = before
    return build_power("off", after, changed=was_on, idled=None, completed=completed)


def _idle(station: Station, address: int | None) -> Idled | None:
    """Send speed 0 to `address`, keeping its stored direction where one can be
    read. `None` when no address is configured: there is nothing to idle.

    Speed 0 is the point of this telegram - the station's start mode is
    automatic on this bench (docs/probe-results.md), so a stored speed resumes
    the moment power returns. The direction was never the point, and sending
    `Direction.FORWARD` unconditionally overwrote whatever the locomotive had.

    The read is `read_loco`, so it can never abort the idle: a direction that
    cannot be read is reported as not preserved, and the telegram still goes
    out. Leaving a locomotive able to start by itself in order to protect its
    direction would be the wrong way round.
    """
    if address is None:
        return None
    was = read_loco(station, address)
    stored = was.direction if was is not None else None
    direction = Direction.FORWARD if stored is None else stored
    station.drive(address, 0, direction)
    return Idled(address=address, direction=direction, direction_preserved=stored is not None)


def _checked_state(state: str) -> str:
    return checked_enum(
        state,
        name=POWER_STATE_ARG.name,
        allowed=POWER_STATES,
        suggestions=[["railctl", "power", value] for value in POWER_STATES],
    )


def register(app: typer.Typer) -> None:
    """Attach `power` and `stop` to `app`, in that order.

    `power` redeclares all eight global options, same as every command in
    `throttle.py`. `stop` redeclares only SEVEN: its own `STOP_ADDRESS_OPT`
    already claims the `--address`/`-a` flag names for a command-scoped meaning
    (see the module docstring), and a second `global_option("--address")` under
    the same flag names would either collide in Click or quietly reintroduce
    the fallback that decision rules out.
    """

    @app.command("power", help=_POWER_META.help, epilog=help_epilog(_POWER_META))
    def power_command(
        ctx: typer.Context,
        state: str = _STATE_ARG,
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
            wanted = _checked_state(state)
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                if wanted == "on":
                    return _power_on(station, settings.address)
                return _power_off(station)
            finally:
                station.close()

        run("power", output, work)

    @app.command("stop", help=_STOP_META.help, epilog=help_epilog(_STOP_META))
    def stop_command(
        ctx: typer.Context,
        address: int | None = _STOP_ADDRESS,
        target: str | None = _TARGET,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        # `address` is deliberately NOT passed to merged_output: this
        # command's --address is its own scope selector, not an override of the
        # resolved default. Handing it over would put it into
        # `settings.address`, which is the fallback this command must not have.
        settings, output = merged_output(
            cli_ctx.settings,
            cli_ctx.output,
            target=target,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            # `stop`'s --address reaches no `Settings`, by design (see the
            # module docstring), so `merge_settings` never sees it and it needs
            # the bound applied here - the same function, on the same value, so
            # the manifest's published range is the range every entry point
            # enforces.
            check_address(address)
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                # Through the facade's own emergency-stop path, which keeps
                # track power on and is not an ordinary speed-zero command.
                station.emergency_stop(address=address)
                return build_stop(address)
            finally:
                station.close()

        run("stop", output, work)
