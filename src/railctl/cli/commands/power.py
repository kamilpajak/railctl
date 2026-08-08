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
from railctl.cli.deps import UsageProblem, merged_output, open_station
from railctl.cli.result import CommandResult
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


def build_power(
    state: str, status: StationStatus, *, changed: bool, idled_address: int | None
) -> CommandResult:
    outcome = CommandResult(schema=POWER_SCHEMA, command="power")
    outcome.result = {
        "state": state,
        "track_power": status.track_power,
        "auto_start_mode": status.auto_start_mode,
        "changed": changed,
        "idled_address": idled_address,
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
    if idled_address is not None:
        outcome.say(f"loco {idled_address} was set to speed 0 so it does not move on its own")
    return outcome


def build_stop(address: int | None) -> CommandResult:
    outcome = CommandResult(schema=STOP_SCHEMA, command="stop")
    outcome.result = {"address": address, "scope": "single" if address is not None else "all"}
    outcome.say(f"loco {address} stopped" if address is not None else "all locomotives stopped")
    return outcome


def _checked_state(state: str) -> str:
    if state in POWER_STATES:
        return state
    raise UsageProblem(
        f"power takes {' or '.join(POWER_STATES)}, not {state!r}",
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
                    # MEASURED (docs/probe-results.md): this station's start
                    # mode is automatic, and a stored speed does resume when
                    # power returns - that is what the doctor's D3 run drove a
                    # locomotive with, and what the backup relay note warns
                    # about. NOT MEASURED: whether the stop-all prefix is what
                    # clears that stored speed. Nobody has captured what the
                    # station's refresh buffer holds across a power cycle, so
                    # the prefix is a precaution taken on the strength of the
                    # first fact, not a proven remedy. Only the last step
                    # below - speed 0 to the resolved address - is proven.
                    station.emergency_stop(address=None)
                    station.power_on()
                    status = station.status()
                    idled_address = settings.address
                    if idled_address is not None:
                        station.drive(idled_address, 0, Direction.FORWARD)
                    return build_power("on", status, changed=True, idled_address=idled_address)
                # `power off` is the one of the two that reads status first:
                # skipping its only mutation when nothing needs doing is
                # exactly what `changed: false` is for. `power on` cannot do
                # the same, because its own first call is the stop-all above.
                before = station.status()
                was_on = before.track_power
                if was_on:
                    station.power_off()
                    after = station.status()
                else:
                    after = before
                return build_power("off", after, changed=was_on, idled_address=None)
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
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                # Through the facade's own emergency-stop path, which keeps
                # track power on and is not an ordinary speed-zero command.
                station.emergency_stop(address=address)
                return build_stop(address)
            finally:
                station.close()

        run("stop", output, work)
