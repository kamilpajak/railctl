# src/railctl/cli/commands/doctor.py
"""`railctl doctor` - runs the capability probe and writes capabilities.json.

The doctor is the first command a new user runs, and it is the command that WRITES
the file every other command reads, so a wrong measurement here becomes a wrong fact
for every later run. Its human output leads with the raw evidence - the check table,
then the tri-state capabilities, then any notes - and closes with the four-line
verdict block `railctl.station.verdict_lines` already builds: the checks are what the
probe measured, the verdict is what that means for the CLI's other commands, and
putting the takeaway last is what "ends with the verdict" means in practice.

Three outcomes stay distinguishable in every rendering. In the JSON a capability is
`true`, `false` or `null`; in the human text `yes`, `no` or `unknown`; and `null` is
never rendered as the word "unknown" INSIDE the JSON, because a script could then no
longer tell a real gap from a string some other field might legitimately hold.

The one place this file reads "unknown" as bad news rather than as neutral is
`LayoutState.held` - see `_layout_lines`. A capability nobody measured is simply not
yet known; a hold nobody confirmed has to be treated as absent, because acting on it
means standing next to a locomotive that may start.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, TextIO

import typer

from railctl.cli._errors import run
from railctl.cli._meta import (
    DOCTOR_NO_PROGRAMMING_TRACK,
    DOCTOR_NO_SAVE,
    DOCTOR_POWER_ON,
    command_meta,
    global_option,
    help_epilog,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    HELD_LINES,
    RESUME_COMMAND,
    close_after,
    close_quietly,
    link_info,
    merged_output,
    open_station,
    station_info,
)
from railctl.cli.result import PARTIAL_EXIT_CODE, CommandResult, ResultWarning, tri_state
from railctl.station import (
    Capabilities,
    Check,
    DoctorReport,
    exit_code_for_report,
    layout_json,
    verdict_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

    from railctl.station import LayoutState

_DOCTOR_META = command_meta("doctor")

#: Read off the metadata row, never retyped - see the identical note in basics.py.
DOCTOR_SCHEMA: Final[str] = _DOCTOR_META.schema

_LABEL_WIDTH: Final[int] = len("UNKNOWN")

#: (attribute, human title, kind). "bool" renders through `tri_state()`; "text"
#: renders through `_text()`. The four non-boolean tri-state fields - a version
#: string, two integers and a result-channel enum string - must never be handed to
#: `tri_state()`, which only ever means yes/no/unknown for an actual `bool | None`.
#: Passing `"4.0"` through it would be a silent type confusion, not a caught bug.
CAPABILITY_FIELDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("xpressnet_version", "XpressNet version", "text"),
    ("command_station_id", "Command station id", "text"),
    ("pom_read", "POM read", "bool"),
    ("pom_read_provenance", "POM read provenance", "text"),
    ("pom_result_channel", "POM result channel", "text"),
    ("pom_echo_zero_based", "POM echo zero-based", "bool"),
    ("loco_address_threshold", "Long-address threshold", "text"),
    ("service_direct_cv", "Service mode: direct CV", "bool"),
    ("service_ext_cv", "Service mode: extended CV", "bool"),
    ("z21_cv_opcodes", "Z21 16-bit CV opcodes", "bool"),
    ("function_groups_4_5", "Function groups 4/5 (F13-F28)", "bool"),
    ("single_function_cmd", "Single-function command", "bool"),
)

#: What an operator is told when the track is live and the station then reported NO
#: emergency stop under it. MEASURED 2026-08-09 (docs/probe-results.md, runs 1 and 2):
#: a locomotive resumes its stored speed the instant power returns.
_FREE_LAYOUT: Final[str] = (
    "the track is live and the station reports NO emergency stop, so nothing is holding the "
    "layout: a locomotive with a stored speed can start on its own"
)
#: The same two readings with the power confirmed off. `_FREE_LAYOUT` and
#: `_HOLD_UNCONFIRMED` were printed for this too, so a run that energised the track,
#: failed to hold it and switched it back OFF - the one ending where nothing can move -
#: told the operator to treat the layout as able to move.
_POWER_OFF_NOW: Final[str] = (
    "the station reports the track power off, so nothing can move until it is switched back on"
)
_HOLD_UNCONFIRMED: Final[str] = (
    "the hold telegram went out and the station never confirmed it, so treat the layout as "
    "able to move until `railctl status` says otherwise"
)
_MAY_BE_LIVE: Final[str] = (
    "the power-on telegram went out and was never confirmed, so the track MAY be live"
)
_NOT_TOUCHED: Final[str] = "this run did not change the track power"
#: What the run READ, for a layout it neither energised nor is responsible for
#: holding. `_layout_lines` never looked at `track_power` at all, so an untouched
#: layout got one line saying the power was not changed and nothing saying what it is.
_FOUND_LIVE: Final[str] = "the track was already live before this run started"
_FOUND_DEAD: Final[str] = "the track was off before this run started and is off now"
_POWER_UNREAD: Final[str] = "the track power was never read, so it is unknown"

#: The warning name for a probe that finished and could not write its own record.
#: `saved_to: null` says a file was not written; this says why, under a name a script
#: can branch on without matching prose.
CAPABILITIES_NOT_SAVED_WARNING: Final[str] = "capabilities_not_saved"

# Built once, at import time - see the same B008 note in main.py.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_POWER_ON = typer_option(DOCTOR_POWER_ON)
_NO_PROGRAMMING_TRACK = typer_option(DOCTOR_NO_PROGRAMMING_TRACK)
_NO_SAVE = typer_option(DOCTOR_NO_SAVE)


def _text(value: object) -> str:
    """`None` reads as "unknown", never blank and never "no" - a capability nobody
    has probed yet is not evidence it is absent."""
    return "unknown" if value is None else str(value)


def _capabilities_payload(caps: Capabilities) -> dict[str, object]:
    """The raw tri-state values, untouched, for JSON - true/false/null, never the
    `tri_state()` word."""
    return {name: getattr(caps, name) for name, _, _ in CAPABILITY_FIELDS}


def _capability_lines(caps: Capabilities) -> list[str]:
    lines: list[str] = []
    for name, title, kind in CAPABILITY_FIELDS:
        value = getattr(caps, name)
        rendered = tri_state(value) if kind == "bool" else _text(value)
        lines.append(f"  {title}: {rendered}")
    return lines


def _check_line(check: Check) -> str:
    return f"[{check.status.upper():<{_LABEL_WIDTH}}] {check.id} {check.title}: {check.detail}"


def _layout_lines(layout: LayoutState) -> list[str]:
    """What this run left the layout doing, in the operator's terms.

    A diagnostic command that changes the layout and does not say so is the same
    defect as a capability recorded absent because the instrument was broken, one
    level up: the report is confident and the state it describes is not the state in
    front of you.

    Two kinds of run, and they get different text. A run that neither energised the
    track nor found a hold under it is responsible for nothing, so it reports what it
    READ and stops - no hazard sentence about a layout it never touched. A run that
    energised the track, or that found the layout held and therefore owes the hold
    back at the end (`must_leave_held`), reports the hazard: for those, "the station
    says there is no hold" is something to act on.
    """
    if layout.energised is False and not layout.must_leave_held:
        return [
            f"  {_NOT_TOUCHED}",
            f"  {_found_power_line(layout)}",
            *_idle_lines(layout),
        ]
    lines = [f"  {_opening_line(layout)}"] if layout.energised is not True else []
    if layout.held is True:
        lines += [f"  {line}" for line in HELD_LINES]
    elif layout.track_power is False:
        # Read off `track_power`, not assumed live: a layout with the power confirmed
        # OFF cannot move whether or not a hold is under it, and telling an operator a
        # locomotive may start is how a report loses the credit it needs for the times
        # when one can.
        lines.append(f"  {_POWER_OFF_NOW}")
    elif layout.held is False:
        lines.append(f"  {_FREE_LAYOUT}")
    else:
        lines.append(f"  {_HOLD_UNCONFIRMED}")
    return lines + _idle_lines(layout)


def _opening_line(layout: LayoutState) -> str:
    """The power sentence for a run that is responsible for the layout: `None` is the
    unconfirmed energise, `False` is a run that found the hold rather than making it."""
    return _MAY_BE_LIVE if layout.energised is None else _NOT_TOUCHED


def _found_power_line(layout: LayoutState) -> str:
    """What the track power WAS, for a run that changed none of it. Three-valued, like
    everything else here: `None` means D2 never produced a status to read it from."""
    if layout.track_power is True:
        return _FOUND_LIVE
    if layout.track_power is False:
        return _FOUND_DEAD
    return _POWER_UNREAD


def _idle_lines(layout: LayoutState) -> list[str]:
    if layout.idled_address is None:
        return []
    if layout.idled:
        return [f"  loco {layout.idled_address} was sent speed 0 so it does not move on its own"]
    return [
        f"  loco {layout.idled_address} could not be sent speed 0 and still holds its stored "
        f"speed, so `{RESUME_COMMAND}` would start it"
    ]


def _warn_about_the_layout(
    outcome: CommandResult, layout: LayoutState, *, may_degrade: bool
) -> None:
    """The things a caller branches on, and the exit code that goes with them.

    `hold_not_confirmed` is `commands/power.py`'s own warning name, deliberately:
    both commands can leave a live track with nothing proven to be holding it, and a
    script must not need two names for one hazard. Exit 8 for the same reason - the
    probe itself may have gone perfectly, so this is a partial result and not an
    error, exactly as `power on`'s is.

    `may_degrade` is False when the probe itself failed. The WARNINGS still go out -
    this used to be skipped whole when `report.ok` was False, which dropped the
    machine-readable layout warning exactly in the runs that end with a possibly live,
    definitely unheld track - but the exit code stays the probe's own 3, because a
    doctor that could not establish the basics has measured nothing and that is the
    bigger answer of the two.
    """
    if _responsible_for(layout) and _may_move(layout):
        outcome.warn(
            "hold_not_confirmed",
            f"{_hold_lead(layout)} and "
            f"{_FREE_LAYOUT if layout.held is False else _HOLD_UNCONFIRMED}"
            f"; measured 2026-08-09, a locomotive resumes its stored speed as soon as power "
            f"returns",
            held=layout.held,
            track_power=layout.track_power,
        )
        if may_degrade:
            # `warn` touches neither `ok` nor `exit_code`, so without this the run
            # exited 0 with `ok: true` and a script branching on `$?` carried on as if
            # the layout were held. A command that did not do what it says has not
            # succeeded.
            outcome.ok = False
            outcome.exit_code = PARTIAL_EXIT_CODE
    if layout.idled is False:
        outcome.warn(
            "loco_not_idled",
            f"loco {layout.idled_address} refused the speed-0 telegram, so it still holds "
            f"whatever speed the station has for it and `{RESUME_COMMAND}` would start it "
            f"(measured 2026-08-09, run 5)",
            address=layout.idled_address,
        )
        if may_degrade:
            # The same reading as above: the run did part of what it says it does. A
            # locomotive left able to start is not a success to be reported as one.
            outcome.ok = False
            outcome.exit_code = PARTIAL_EXIT_CODE
    if layout.idled and layout.direction_preserved is False:
        outcome.warn(
            "direction_not_preserved",
            f"loco {layout.idled_address}'s stored direction could not be read, so the "
            f"speed-0 telegram went out forward; if it was running the other way, that is "
            f"now changed",
            address=layout.idled_address,
        )


def _responsible_for(layout: LayoutState) -> bool:
    """Whether this run has to answer for the state of the layout.

    True when it touched the power (`energised` is `True` or the unconfirmed `None`)
    and when it owes a hold back. False for the ordinary diagnostic run on somebody
    else's live, free layout: warning there would make the token that says "a
    locomotive may start because of this command" fire on runs that changed nothing,
    and a warning that fires on every run is one nobody reads.
    """
    return layout.energised is not False or layout.must_leave_held


def _may_move(layout: LayoutState) -> bool:
    """`held is not True` is the hazard, unless the power is confirmed off.

    `track_power is False` is the one reading that rules movement out, and it is a
    reading - never an assumption. `None` counts as a hazard: an unconfirmed power-on
    telegram has gone out and nobody knows what the track is doing.
    """
    return layout.held is not True and layout.track_power is not False


def _hold_lead(layout: LayoutState) -> str:
    """Which run this is, in the warning's own first clause. A run that energised the
    track made the hazard; a run that found the layout held and could not put the hold
    back released one that was already there - a distinction an operator standing at
    the layout needs, and the second case used to produce no warning at all."""
    if layout.energised is False:
        return "this run released the hold it found on the layout"
    return "this run energised the track"


def build_doctor(report: DoctorReport, *, saved_to: Path | None) -> CommandResult:
    caps = report.capabilities
    result = CommandResult(
        schema=DOCTOR_SCHEMA,
        command="doctor",
        ok=report.ok,
        exit_code=exit_code_for_report(report),
    )
    result.result["checks"] = [
        {"id": c.id, "title": c.title, "status": c.status, "detail": c.detail}
        for c in report.checks
    ]
    result.result["capabilities"] = _capabilities_payload(caps)
    result.result["notes"] = list(caps.notes)
    result.result["layout"] = layout_json(report.layout)
    verdict = list(verdict_lines(report))
    result.result["verdict"] = verdict
    result.result["saved_to"] = None if saved_to is None else str(saved_to)

    result.say("Checks:")
    for check in report.checks:
        result.say(_check_line(check))
    result.say("")
    result.say("Capabilities:")
    for line in _capability_lines(caps):
        result.say(line)
    if caps.notes:
        result.say("")
        result.say("Notes:")
        for note in caps.notes:
            result.say(f"  - {note}")
    result.say("")
    result.say("Layout:")
    for line in _layout_lines(report.layout):
        result.say(line)
    if saved_to is not None:
        result.say("")
        result.say(f"Capabilities saved to {saved_to}")
    result.say("")
    # The verdict is last, after the evidence it is built from - and after the two
    # sections above, so `result.lines[-4:]` is the block and nothing else.
    for line in verdict:
        result.say(line)

    # Always, on every ending. `may_degrade` carries the exit-code precedence that
    # used to be spelled by skipping this call: a doctor whose D0-D2 could not
    # establish the basics has measured nothing at all, and exit 3 is the bigger
    # answer of the two, so the partial must not soften it. The WARNINGS belong in
    # both cases - a failed probe is if anything more likely to have left a live,
    # unheld track, and that was the run publishing no layout warning at all.
    _warn_about_the_layout(result, report.layout, may_degrade=report.ok)
    return result


def _save_capabilities(
    caps: Capabilities, *, no_save: bool, stderr: TextIO
) -> tuple[Path | None, ResultWarning | None]:
    """Write the probe's own record, and return the path ONLY when something was
    written. Issue #15: every command reads `capabilities_path()` and nothing wrote
    it, so every run started from `Capabilities.unknown`.

    A path in the envelope means a file on disk. `save()` returns `False` without
    writing when the link identity is unknown - an identity with no stable name has
    nowhere safe to persist to - and reporting the path anyway would tell a caller to
    go and read a file that is not there.

    `OSError` is caught here rather than left to `run()`. `Capabilities.save` mkdirs,
    writes a temp file and renames, and any of those can fail on a read-only or full
    config directory; uncaught, that turned a finished 30-second probe into exit 1,
    `code: internal`, EMPTY stdout - the probe's own measurements gone, and with them
    the layout block saying whether the run left the track live. Exactly the failure
    `deps.close_after` records this project fixing for `power on`, on the save path
    instead of the close path, and worse here because the doctor is the command that
    may have energised the layout.

    So the probe's result wins: a file that could not be written is a warning and a
    `saved_to: null`, never a reason to withhold what the doctor just measured. The
    exit code stays the probe's own - what failed is a side effect, not the
    measurement - and the warning is what a script branches on.
    """
    if no_save:
        print("capabilities not saved (--no-save)", file=stderr)
        return None, None
    path = capabilities_path(os.environ)
    try:
        written = caps.save(path)
    except OSError as exc:
        return None, ResultWarning(
            name=CAPABILITIES_NOT_SAVED_WARNING,
            message=(
                f"the probe finished, but writing {path} failed: {exc}. Every other command "
                f"reads that file, so they will keep starting from what it held before this run"
            ),
            details={"path": str(path), "error": str(exc)},
        )
    if written:
        return path, None
    print(
        f"capabilities not saved: the link identity is {caps.link_identity!r}, which has no "
        f"stable name to file them under",
        file=stderr,
    )
    return None, None


def register(app: typer.Typer) -> None:
    """Attach `doctor` to `app`.

    Declares all eight global options a second time, like every other command: Click
    parses a group's own options only BEFORE the subcommand name, so without the copy
    `railctl doctor --address 3` is a usage error before `doctor` ever runs - and that
    spelling is M6's own acceptance sentence.

    `open_station` is called with `capabilities_path=None` on purpose. The doctor
    starts from `Capabilities.unknown` so a stale file cannot colour a fresh probe,
    and `Station.close()` then flushes nothing, so `_save_capabilities` below is the
    single write and the envelope's `saved_to` describes it exactly.
    """

    @app.command("doctor", help=_DOCTOR_META.help, epilog=help_epilog(_DOCTOR_META))
    def doctor_command(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
        power_on: bool = _POWER_ON,
        no_programming_track: bool = _NO_PROGRAMMING_TRACK,
        no_save: bool = _NO_SAVE,
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
            station = open_station(settings, capabilities_path=None)
            try:
                report = station.probe(
                    address=settings.address,
                    allow_power_on=power_on,
                    use_programming_track=not no_programming_track,
                )
                saved_to, save_warning = _save_capabilities(
                    report.capabilities, no_save=no_save, stderr=output.stderr
                )
                outcome = build_doctor(report, saved_to=saved_to)
                if save_warning is not None:
                    outcome.warnings.append(save_warning)
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            except BaseException:
                close_quietly(station)
                raise
            return close_after(station, outcome)

        run("doctor", output, work)
