# src/railctl/cli/commands/monitor.py
"""`railctl monitor` - decodes broadcasts and prints them until Ctrl-C.

Inherently a streaming command, so `ndjson` is its natural mode: `stream_monitor`
writes one `event` line per broadcast and always finishes with a `summary` line, even
when the operator interrupts it, because a consumer reading the stream must be able to
tell the run ended the same way whether it ended by running out of events or by Ctrl-C.
`human` prints the same information as it arrives; `json` buffers and renders exactly
once, so a script parsing `--format=json` never has to handle more than one value on
stdout.

`monitor` is the one command in this package allowed to write to stdout outside
`render()` - every other command's stdout goes through `render()` alone. A command
that must show output WHILE IT IS STILL RUNNING cannot wait for a single end-of-run
`render()` call to do it. `build_monitor`'s own `streamed` keyword exists because of
that: when the caller already wrote each event to stdout as it arrived, the returned
`CommandResult.lines` must not repeat them, or `render()` would print every broadcast
a second time on the one surface the operator is watching.

This command opens a station and reads. It sends no telegram of its own, changes no
track power and drives no locomotive - `mutates=False` in its metadata row is a fact
about this file, not a hope.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

import typer

from railctl.cli._errors import OutputContext, report_for, run
from railctl.cli._meta import MONITOR_LIMIT, command_meta, global_option, help_epilog, typer_option
from railctl.cli.config import capabilities_path
from railctl.cli.deps import close_after, close_quietly, merged_output, open_station
from railctl.cli.render import NdjsonStream
from railctl.cli.result import CommandResult
from railctl.errors import AbortedError, RailctlError, exit_code_for
from railctl.station import EVENT_NAMES

if TYPE_CHECKING:
    from railctl.cli.deps import Settings
    from railctl.station import Station, StationEvent

_MONITOR_META = command_meta("monitor")

#: Read off the metadata row, never retyped - see the identical note in basics.py.
MONITOR_SCHEMA: Final[str] = _MONITOR_META.schema

#: On stderr, never stdout: in JSON mode stdout holds exactly one value, and a
#: progress notice there would be the second one.
_START_NOTICE: Final[str] = "monitoring broadcasts; press Ctrl-C to stop\n"

#: The exit code an interrupted run leaves. Ctrl-C is how a monitor normally ends, and
#: `run()` turns it into `AbortedError`; the ndjson path renders its own ending rather
#: than going through `run()`, so it reads the code off the same class instead of
#: writing 9 twice in two files that would then be free to disagree. `__new__` and
#: never `AbortedError(...)`: nothing here needs an initialised instance, and
#: `exit_code_for` answers off the class - the same probe `_meta._class_error_row` uses.
_ABORTED_EXIT_CODE: Final[int] = exit_code_for(AbortedError.__new__(AbortedError))

# Built once, at import time - see the same B008 note in main.py.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")
_LIMIT = typer_option(MONITOR_LIMIT)


def _event_row(event: StationEvent) -> dict[str, object]:
    return {"name": event.name, "detail": event.detail, "payload": event.payload}


def build_monitor(seen: Sequence[StationEvent], *, complete: bool, streamed: bool) -> CommandResult:
    result = CommandResult(
        schema=MONITOR_SCHEMA,
        command="monitor",
        ok=complete,
        exit_code=0 if complete else _ABORTED_EXIT_CODE,
    )
    result.result["events"] = [_event_row(event) for event in seen]
    result.result["count"] = len(seen)
    result.result["complete"] = complete
    # The vocabulary a consumer has to branch on, read off the facade's own tuple
    # rather than retyped: a private copy here is how this command starts advertising
    # a name nothing sends, or misses one that arrives.
    result.result["known_events"] = list(EVENT_NAMES)
    if not seen:
        result.say("no broadcasts seen")
    elif not streamed:
        # `streamed=True` means the caller already wrote one line per event to stdout
        # itself - repeating them here would have `render()` print every broadcast a
        # second time.
        for event in seen:
            result.say(f"{event.name}: {event.detail}")
    if not complete:
        result.say("interrupted")
    return result


def stream_monitor(station: Station, *, ndjson: NdjsonStream, limit: int | None = None) -> int:
    """Stream `station.events()` as ndjson `event` lines, always finishing with one
    `summary` line - even on Ctrl-C. Returns the event count.

    `KeyboardInterrupt` is caught here only to record `complete=False, exit_code=9`
    for that closing line; the `finally` block writes it, and then the interrupt is
    RE-RAISED rather than swallowed. Catching it and returning normally instead would
    give stdout its summary line while leaving the caller with no way to know the run
    was cut short - and this project exists precisely to keep "the run ended early"
    and "the run ended cleanly" from becoming indistinguishable one layer up.

    Nothing wider is caught. `station.events()` is never wrapped in a blanket
    `except Exception` by this command: a link fault is a real failure with an
    envelope of its own, not an ending to be smoothed over.
    """
    count = 0
    complete = False
    exit_code = 0
    try:
        for event in station.events():
            ndjson.event("event", name=event.name, detail=event.detail, payload=event.payload)
            count += 1
            if limit is not None and count >= limit:
                break
        complete = True
        return count
    except KeyboardInterrupt:
        exit_code = _ABORTED_EXIT_CODE
        raise
    finally:
        ndjson.summary(count=count, complete=complete, exit_code=exit_code)


def _run_ndjson(settings: Settings, output: OutputContext, limit: int | None) -> None:
    """Bypasses `_errors.run()` entirely: `stream_monitor` has already written the
    ndjson `summary` line to stdout by the time it re-raises `KeyboardInterrupt`, and
    letting `run()` also render a `CommandResult` here would print a SECOND,
    sequence-reset summary line after the real one.

    That bypass is also why a `RailctlError` needs its own catch here: nothing else on
    this path calls `report_for`/`render_error`, so without it a failure in
    `open_station` or `stream_monitor` would escape to `main()`'s catch-all and print
    a different envelope, with a different exit code, than every other command's
    errors do. The block below renders the identical `railctl/error/v1` object `run()`
    would have rendered, by hand, because `run()` itself is exactly what this function
    exists to not call.

    `close_quietly` on every path, never `close_after`: there is no `CommandResult`
    here to hang a `link_close_failed` warning on, and the stream a consumer is
    reading has already been closed off with its summary line. Adding a fourteenth
    ndjson line after the summary to report a link that would not close would break
    the one contract this path is built around.
    """
    station: Station | None = None
    try:
        station = open_station(settings, capabilities_path=capabilities_path())
        print(_START_NOTICE, end="", file=output.stderr)
        stream_monitor(station, ndjson=NdjsonStream(output.stdout), limit=limit)
    except KeyboardInterrupt:
        raise typer.Exit(code=_ABORTED_EXIT_CODE) from None
    except RailctlError as exc:
        envelope = report_for(exc, command="monitor").envelope()
        output.stderr.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        raise typer.Exit(code=exit_code_for(exc)) from exc
    else:
        raise typer.Exit(code=0)
    finally:
        if station is not None:
            close_quietly(station)


def _work(settings: Settings, output: OutputContext, limit: int | None) -> CommandResult:
    """The buffered half: `human` streams each line as it arrives and `json` holds
    everything until the end, but both return ONE `CommandResult` for `run()` to
    render.

    `KeyboardInterrupt` is caught and turned into a normal, partial result rather than
    re-raised. There is no line-per-event stdout contract to protect on this path, and
    `run()` needs a normal return to render what was seen as one JSON or human value
    instead of an error object that carries none of it.
    """
    streamed = output.fmt == "human"
    station = open_station(settings, capabilities_path=capabilities_path())
    print(_START_NOTICE, end="", file=output.stderr)
    seen: list[StationEvent] = []
    try:
        for event in station.events():
            if streamed:
                output.stdout.write(f"{event.name}: {event.detail}\n")
            seen.append(event)
            if limit is not None and len(seen) >= limit:
                break
        outcome = build_monitor(seen, complete=True, streamed=streamed)
    except KeyboardInterrupt:
        outcome = build_monitor(seen, complete=False, streamed=streamed)
    except BaseException:
        close_quietly(station)
        raise
    return close_after(station, outcome)


def register(app: typer.Typer) -> None:
    """Attach `monitor` to `app`.

    Declares all eight global options a second time for the same reason every other
    command does: `railctl monitor --format ndjson` writes `--format` after the
    subcommand name, which `@app.callback()` never sees.
    """

    @app.command("monitor", help=_MONITOR_META.help, epilog=help_epilog(_MONITOR_META))
    def monitor_command(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
        limit: int | None = _LIMIT,
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
        if output.fmt == "ndjson":
            _run_ndjson(settings, output, limit)
        run("monitor", output, lambda: _work(settings, output, limit))
