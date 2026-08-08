# src/railctl/cli/commands/basics.py
"""The two commands every session starts with: `version` and `status`.

Both builder functions are pure: given the facade's own reply objects, they
return a `CommandResult` with no I/O of their own. That is what lets
`tests/cli/test_wiring.py` pin the human/JSON parity rule - the same facts
appearing in `.lines` and in `.result` - without going through Typer at all,
and it is the shape every later command (Tasks 10-12) repeats.
"""

from __future__ import annotations

from typing import Final

import typer

from railctl import __version__
from railctl.cli._errors import run
from railctl.cli._meta import command_meta, global_option, help_epilog
from railctl.cli.config import capabilities_path
from railctl.cli.deps import link_info, merged_output, open_station, station_info
from railctl.cli.result import CommandResult, StationInfo
from railctl.xbus.replies import StationStatus, StationVersion

VERSION_SCHEMA: Final[str] = "railctl/version/v1"
STATUS_SCHEMA: Final[str] = "railctl/status/v1"

_VERSION_META = command_meta("version")
_STATUS_META = command_meta("status")

# Built once, at import time - see the same B008 note in main.py. Every registered command
# builds this identical eight-tuple; the duplication across command modules is the accepted
# shape, because Click needs a distinct parameter object per command and a shared constant
# would hand the same one to all of them.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")


def build_version(version: StationVersion, *, tool_version: str) -> CommandResult:
    outcome = CommandResult(schema=VERSION_SCHEMA, command="version")
    outcome.result = {
        "protocol": "xpressnet",
        "protocol_version": version.version,
        "command_station_id": version.station_id,
        "family": version.family,
        "tool_version": tool_version,
    }
    outcome.say(f"XpressNet {version.version} ({version.family})")
    outcome.say(f"command station id: {version.station_id}")
    outcome.say(f"railctl {tool_version}")
    return outcome


def build_status(status: StationStatus) -> CommandResult:
    # auto_start_mode is bit 2 - never printed or named "short circuit"
    # anywhere: neither the Lenz nor the German 23151 document defines any
    # status bit that way, and that mislabel is the exact trap the design
    # spec calls out by name.
    outcome = CommandResult(schema=STATUS_SCHEMA, command="status")
    outcome.result = {
        "raw": status.raw,
        "raw_hex": f"0x{status.raw:02X}",
        "track_power": status.track_power,
        "emergency_off": status.emergency_off,
        "emergency_stop": status.emergency_stop,
        "auto_start_mode": status.auto_start_mode,
        "service_mode": status.service_mode,
        "powering_up": status.powering_up,
        "ram_error": status.ram_error,
    }
    start_mode = "automatic" if status.auto_start_mode else "manual"
    outcome.say(f"raw status byte: 0x{status.raw:02X}")
    outcome.say(f"track power: {'on' if status.track_power else 'off'}")
    outcome.say(f"emergency off: {status.emergency_off}")
    outcome.say(f"emergency stop: {status.emergency_stop}")
    outcome.say(f"start mode: {start_mode} (bit 2)")
    outcome.say(f"service mode: {status.service_mode}")
    outcome.say(f"powering up: {status.powering_up}")
    outcome.say(f"ram error: {status.ram_error}")
    return outcome


def register(app: typer.Typer) -> None:
    """Wire `version` and `status` onto `app`.

    Both open a `Station`, build a `CommandResult`, and close the station in
    `finally` - even when building the result raises - so a spy on
    `Station.close` always sees exactly one call regardless of how `work()`
    ends. `run()` never returns (`NoReturn`): it renders the result (or an
    error) and raises `typer.Exit` itself, so neither command body wraps the
    call in its own `raise typer.Exit(code=...)`.

    Both also declare all eight global options a second time and hand them to
    `merged_output` - Click parses a group's own options only BEFORE the
    subcommand name, so without the copy `railctl status --address 3` is a
    usage error before `status` ever runs, and the design's own examples all
    put the flag after the verb.
    """

    @app.command("version", help=_VERSION_META.help, epilog=help_epilog(_VERSION_META))
    def version_command(
        ctx: typer.Context,
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
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                version = station.version()
                # Built inline from the SAME StationVersion already fetched
                # above, not via station_info(station) - that helper exists
                # for commands (like `status`, below) that have no
                # StationVersion of their own and would otherwise have to
                # query it a second time just for the envelope's station block.
                outcome = build_version(version, tool_version=__version__)
                outcome.link = link_info(station, settings)
                outcome.station = StationInfo(
                    protocol="xpressnet",
                    protocol_version=version.version,
                    command_station_id=version.station_id,
                )
            finally:
                station.close()
            return outcome

        run("version", output, work)

    @app.command("status", help=_STATUS_META.help, epilog=help_epilog(_STATUS_META))
    def status_command(
        ctx: typer.Context,
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
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                outcome = build_status(station.status())
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            finally:
                station.close()
            return outcome

        run("status", output, work)
