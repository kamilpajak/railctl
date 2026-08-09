# src/railctl/cli/commands/power.py
"""The `power` and `stop` commands.

`power` has three states, not two. `power on` energises the track and leaves
the whole layout HELD in emergency stop; `power resume` is the release. They
are separate commands because an emergency stop holds the station's refresh
buffer and never clears it - measured 2026-08-09, docs/probe-results.md - so
one command cannot hold and then quietly release, and the release is the moment
stored speeds start locomotives. `_power_on` carries the measurements and the
comparison with JMRI's power-state model.

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
from railctl.cli.result import PARTIAL_EXIT_CODE, CommandResult, error_code, tri_state
from railctl.errors import CONDITION_TRACK_DEAD, RailctlError, TrackPowerError
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


#: The one command that releases the hold `power on` leaves behind, named once
#: so the human text, the JSON and this module all quote the same string.
RESUME_COMMAND: Final[str] = "railctl power resume"


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
    #: Whether the telegram changed anything about this locomotive. `None` when
    #: the reply carried no decoded speed to compare against (a 14/27/28-step
    #: reply, or no reply at all) - unknown, never "nothing happened".
    changed: bool | None


def build_power(
    state: str,
    status: StationStatus,
    *,
    changed: bool | None,
    idled: Idled | None,
    completed: Sequence[str],
) -> CommandResult:
    """`changed` is tri-state, like `drive`'s.

    `power on` reported `true` unconditionally, which is not what a
    desired-state verb owes a caller. It is now computed: `true` if the power
    state moved or the idle telegram changed the locomotive, `false` if neither
    did, and `null` when the locomotive's previous speed was not in a layout
    railctl decodes, so whether the idle changed it cannot be known.
    """
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
        f"track power is {'on' if status.track_power else 'off'} ({tri_state(changed)} changed)"
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
    if state == "resume":
        _warn_stored_speeds_released(outcome, confirmed=True)
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


def _warn_stored_speeds_released(outcome: CommandResult, *, confirmed: bool) -> None:
    """The one token that says locomotives may now be moving.

    This is the machine-readable name a caller keys on, and the `power resume`
    PARTIAL path used to drop it - in exactly the run where the release telegram
    went out and the confirming read then failed, which is the run where a
    caller most needs it. It is emitted whenever `21 81` has been written,
    confirmed or not; `confirmed` says which of the two this is rather than
    silence standing in for "it did not happen".

    MEASURED 2026-08-09 (docs/probe-results.md, "`power on`'s stop-all was in
    the wrong order", run 5): a locomotive held in emergency stop with step 80
    stored accelerated away the moment `21 81` was sent, and `loco_info`
    reported speed=80 both while it was held and after the release. The hold
    keeps the station's refresh buffer; it never clears it. So this is not a
    precaution taken against an inference - it is what was watched happening on
    the rolling road.
    """
    lead = (
        "the hold is cleared"
        if confirmed
        else "the release telegram went out and was never confirmed, so the hold may be cleared"
    )
    outcome.warn(
        "stored_speeds_released",
        f"{lead}: every locomotive the station still has a speed for may now "
        "move, except one that was sent speed 0 while the layout was held. Measured "
        "2026-08-09: a locomotive held with step 80 stored accelerated away on the "
        "release, and its stored speed read 80 throughout",
        measured="2026-08-09",
        confirmed=confirmed,
    )


def _say_emergency_state(outcome: CommandResult, state: str, status: StationStatus) -> None:
    """The two bits that decide whether the layout can move, in the human text.

    Emergency stop is the one this exists for: it leaves the track powered, so
    `track_power: true` alone reads as success while every locomotive is held
    at standstill. An operator who runs a `power` command, sees nothing move
    and has to run a second command to find out why was told half the answer by
    the command they already ran.

    What each state does with those bits is not the same, and that is the whole
    point of this change: after `power on` an emergency stop is the INTENDED
    ending, and after `power resume` it means the release did not take.

    The three cases are one chain, not two independent lines. "The track has
    voltage" was hardcoded into the emergency-stop sentence while the
    emergency-off sentence printed on its own, so `0x07` - both bits set, a
    state this bench really sits in on power-up - said the track had voltage and
    then said it had none. Voltage is `track_power`, which is `emergency_off`
    read the other way round (`xbus/replies.py`), so only the combined branch
    can state it once and correctly.
    """
    if status.emergency_stop and status.emergency_off:
        outcome.say(
            "emergency stop and emergency off are both active: the track has no voltage, and "
            "the hold is still set under it"
        )
    elif status.emergency_stop:
        outcome.say(
            "emergency stop is active: the track has voltage and every locomotive is held "
            "at standstill"
        )
    elif status.emergency_off:
        outcome.say("emergency off is active: the track has no voltage")
    if state == "on":
        _say_the_hold(outcome, status)
    elif state == "resume" and (status.emergency_stop or status.emergency_off):
        _warn_cannot_move(outcome, status, after="power resume")


def _say_the_hold(outcome: CommandResult, status: StationStatus) -> None:
    """What `power on` promises, checked against what the station reported.

    `power on` ends HELD by design, so the emergency-stop bit here is success
    and gets said plainly rather than warned about. The other two readings are
    both failures of that promise and they are different failures: a dead track
    means the power-on did not take, and a track that is live with NO hold is
    the runaway measured on 2026-08-09 - runs 1 and 2, where the locomotive
    resumed its stored speed the instant power returned.
    """
    if status.emergency_off:
        _warn_cannot_move(outcome, status, after="power on")
    elif status.emergency_stop:
        outcome.say(
            "the layout is held: the track has voltage and nothing will move until the hold "
            "is released"
        )
        outcome.say(f"run `{RESUME_COMMAND}` to release it, with the layout in view")
    else:
        # Not a report of what we asked for - a report of what the station
        # answered. Saying "held" off the back of a telegram we sent, without
        # the bit that confirms it, is this project's founding rule broken in
        # the one field an operator would act on.
        outcome.warn(
            "hold_not_confirmed",
            "the track is live and the station reports NO emergency stop, so this command "
            "did not leave the layout held; measured 2026-08-09, a locomotive resumes its "
            "stored speed as soon as power returns",
            emergency_stop=status.emergency_stop,
            track_power=status.track_power,
        )
        # `CommandResult.warn` touches neither `ok` nor `exit_code`, so this
        # case - the only one this guard exists for - exited 0 with `ok: true`
        # and a script branching on `$?` carried on as if the layout were held.
        # A command that did not do what it says has not succeeded. Exit 8 is
        # the code `power` already publishes for "some of it happened": the
        # track really is live, which is why this is not a plain error either.
        outcome.ok = False
        outcome.exit_code = PARTIAL_EXIT_CODE


def _warn_cannot_move(outcome: CommandResult, status: StationStatus, *, after: str) -> None:
    outcome.warn(
        "layout_cannot_move",
        f"the station still reports an emergency state after {after}, so nothing will move "
        f"until it is cleared",
        emergency_stop=status.emergency_stop,
        emergency_off=status.emergency_off,
    )


def build_power_partial(
    state: str,
    *,
    steps: Sequence[str],
    completed: Sequence[str],
    before: StationStatus,
    failure: BaseException,
) -> CommandResult:
    """A mutating `power` state started sending telegrams and then failed. Exit 8, not 9.

    Both mutating states energise the track, and a failure from the energise
    onwards went to `run()`'s generic handler, which told the caller the command
    failed - while the layout may have been live. For `power on` that may be a
    live track with no hold on it or a locomotive not yet idled; for `power
    resume` it is a layout that has just been released. Those are different
    situations from "nothing happened" and a script has to be able to act on the
    difference, so this is a RESULT carrying what completed, not an error
    carrying a message about what did not.

    The energise itself is inside the covered range, which is why `steps` is
    passed in rather than read off `STEP_ORDER`: a run with no `--address` sends
    no idle telegram, and a later failure in such a run must not be reported as
    the step that never ran (`_on_steps`).

    `PARTIAL_HAZARD` is the sentence this envelope exists for. The most
    dangerous ending - a live track with nothing holding it - used to get the
    shortest report, one clause naming the step that failed; it now says what
    state the layout is in, because that is what an operator has to know before
    walking up to it.

    `track_power` and the status bits stay `null`, never `false`: the confirming
    status read is the last step, so any failure reported here happened before
    it, and publishing the pre-command reading in those fields would present a
    track this command has since energised as unpowered - the founding rule
    broken inside the report of a failure to follow it. The reading IS known and
    is published, under `*_before` keys in the warning's details, where it says
    what it is.
    """
    # `completed` is appended to in order and never out of it, so the step that
    # failed is the next one in the plan for this run. Deriving it by index
    # rather than by asking which named steps are missing is what keeps a third
    # step order from needing a third special case here.
    failed_step = steps[len(completed)]
    outcome = CommandResult(
        schema=POWER_SCHEMA, command="power", ok=False, exit_code=PARTIAL_EXIT_CODE
    )
    outcome.result = {
        "state": state,
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
    reached = _reached(state, completed)
    hazard = PARTIAL_HAZARD[state, failed_step]
    outcome.warn(
        "power_on_incomplete",
        f"{reached}, and then {failed_step} failed: {failure}. {hazard}",
        completed=list(completed),
        failed_step=failed_step,
        error_code=error_code(failure),
        **_before_details(before),
    )
    outcome.say(f"{reached}; {failed_step} did not complete")
    outcome.say(hazard)
    if state == "resume":
        # The release telegram is written before anything here can fail, so the
        # token that says locomotives may be moving belongs in this envelope
        # exactly as much as in the successful one.
        _warn_stored_speeds_released(outcome, confirmed=False)
    return outcome


def _reached(state: str, completed: Sequence[str]) -> str:
    """What already happened, in the caller's terms rather than in step names.

    Derived from `completed` rather than from the step that failed, because the
    two mutating states can skip a step: `power on` with no `--address` sends no
    idle telegram, and a fixed sentence per failure would claim one that never
    went out.

    An empty list of clauses means the FIRST mutation is the one that failed -
    every other step has a clause - and a telegram whose reply never came may
    still have been acted on. So that case names the dangerous reading of the
    silence, not the convenient one.
    """
    done = [STEP_REACHED[state][step] for step in completed if step in STEP_REACHED[state]]
    return " and ".join(done) if done else PARTIAL_UNCONFIRMED[state]


def _before_details(status: StationStatus) -> dict[str, object]:
    """The one reading a failed run is sure of: what the track was doing BEFORE it.

    Published under `*_before` names, not in the result's own status fields.
    Those describe the state this command left behind, which is what a partial
    does not know; this describes the state it found, which it does. Discarding
    it because it cannot be presented as the outcome would throw away the only
    measurement in the envelope.
    """
    return {
        "track_power_before": status.track_power,
        "emergency_stop_before": status.emergency_stop,
        "emergency_off_before": status.emergency_off,
        "auto_start_mode_before": status.auto_start_mode,
    }


def build_stop(address: int | None) -> CommandResult:
    outcome = CommandResult(schema=STOP_SCHEMA, command="stop")
    outcome.result = {"address": address, "scope": "single" if address is not None else "all"}
    outcome.say(f"loco {address} stopped" if address is not None else "all locomotives stopped")
    return outcome


#: The steps the three `power` states run, named so the envelope can report
#: which of them completed. `power on` and `power resume` are the two that need
#: it: their first mutation energises the track, so "the command failed" stops
#: being the whole answer from that point on.
STEP_READ_STATUS_BEFORE: Final[str] = "read_status_before"
STEP_STOP_ALL: Final[str] = "stop_all"
STEP_POWER_ON: Final[str] = "power_on"
STEP_READ_STATUS: Final[str] = "read_status"
STEP_IDLE_ADDRESS: Final[str] = "idle_address"
STEP_POWER_OFF: Final[str] = "power_off"

#: The order each mutating state runs its steps in. `build_power_partial` reads
#: the step that failed out of the plan for the run rather than re-deriving it
#: from the names that are missing, and `_power_on`/`_power_resume` append in
#: exactly this order - the one property that makes reading it by index correct.
#:
#: `STEP_READ_STATUS` is LAST for both states, after every telegram. It used to
#: sit before the idle, so the read that decided whether to claim "held" ran
#: before the final mutation. Run 7 (docs/probe-results.md, "`power on`'s
#: stop-all was in the wrong order") measured that the answer does not change on
#: this station - a per-locomotive speed telegram leaves the station-wide
#: emergency stop set, status still `0x05` - so the claim was true; it was
#: confirmed against a state that predated the last thing the command did, which
#: is the wrong shape whatever the station happens to answer.
STEP_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "on": (
        STEP_READ_STATUS_BEFORE,
        STEP_POWER_ON,
        STEP_STOP_ALL,
        STEP_IDLE_ADDRESS,
        STEP_READ_STATUS,
    ),
    "resume": (STEP_READ_STATUS_BEFORE, STEP_POWER_ON, STEP_READ_STATUS),
}

#: What each completed step means to the caller, in words. Keyed by state as
#: well as by step because `power_on` is not the same event in the two: for `on`
#: it energises a dead track, for `resume` it releases a hold. `read_status` has
#: no row because it is the last step of both - a run that completed it produced
#: a full result, never a partial.
STEP_REACHED: Final[dict[str, dict[str, str]]] = {
    "on": {
        STEP_POWER_ON: "the track was switched on",
        STEP_STOP_ALL: "the whole layout was held",
        STEP_IDLE_ADDRESS: "the addressed locomotive was sent speed 0",
    },
    "resume": {STEP_POWER_ON: "the hold was released"},
}

#: What the caller is told when the ENERGISE itself is the step that failed, so
#: no mutation is confirmed and the telegram may still have been acted on.
PARTIAL_UNCONFIRMED: Final[dict[str, str]] = {
    "on": "the power-on telegram went out and was never confirmed, so the track may be live",
    "resume": "the release telegram went out and was never confirmed, so the hold may be gone",
}

#: The state each partial leaves the layout in, keyed by the step that failed.
#: This is the sentence the operator acts on, and the two worst endings are the
#: ones that used to say the least: a live track with nothing holding it is the
#: runaway measured on 2026-08-09 (docs/probe-results.md, runs 1 and 2), and it
#: got the same one-clause report as a failed status read.
PARTIAL_HAZARD: Final[dict[tuple[str, str], str]] = {
    ("on", STEP_POWER_ON): (
        "if it is live then NOTHING is holding it, and a locomotive with a stored speed can "
        "start on its own - measured 2026-08-09, runs 1 and 2. Treat the layout as live and "
        "free until `railctl status` says otherwise"
    ),
    ("on", STEP_STOP_ALL): (
        "the track is live and NOTHING is holding it: a locomotive with a stored speed can "
        "start on its own, measured 2026-08-09, runs 1 and 2. This is the most dangerous "
        "state this command can leave"
    ),
    ("on", STEP_IDLE_ADDRESS): (
        "the layout is held, but the addressed locomotive still has its stored speed, so a "
        "later `railctl power resume` would start it - measured 2026-08-09, run 5"
    ),
    ("on", STEP_READ_STATUS): (
        "every telegram went out, but the station never confirmed the hold, so treat the "
        "layout as able to move until `railctl status` says otherwise"
    ),
    ("resume", STEP_POWER_ON): (
        "every locomotive the station still has a speed for may now be moving - measured "
        "2026-08-09, run 5"
    ),
    ("resume", STEP_READ_STATUS): (
        "every locomotive the station still has a speed for may now be moving - measured "
        "2026-08-09, run 5"
    ),
}


def _power_state(status: StationStatus) -> tuple[bool, bool, bool]:
    """The three bits `power` is about, as one comparable value."""
    return (status.track_power, status.emergency_stop, status.emergency_off)


def _on_steps(address: int | None) -> tuple[str, ...]:
    """The steps THIS `power on` run will take, which is not always all of them.

    `build_power_partial` names the failed step by its index in this sequence,
    which is only correct if the sequence describes the run rather than the
    state in general. With no `--address` there is no locomotive to idle and no
    telegram is sent for that step, so a later failure in such a run would
    otherwise be reported as `idle_address` - a step that never ran, named as
    the one that broke, in the envelope a script reads to find out what
    happened.
    """
    if address is None:
        return tuple(step for step in STEP_ORDER["on"] if step != STEP_IDLE_ADDRESS)
    return STEP_ORDER["on"]


def _power_on(station: Station, address: int | None) -> CommandResult:
    """Energise, hold the whole layout, zero the addressed locomotive - and
    stay held. Nothing moves until `power resume`.

    MEASURED 2026-08-09 on the rolling road, with nothing but railctl connected
    (docs/probe-results.md, "`power on`'s stop-all was in the wrong order").
    This command used to send `80 80` BEFORE energising, and that step did
    nothing at all: the locomotive resumed its stored speed with the prefix
    (run 2) exactly as it did in the control run without it (run 1). Sent AFTER
    the track is live, the same telegram works - runs 3 and 4 held stored steps
    15 and 80 and the locomotive never moved. So `power_on()` goes first, both
    because the hold only takes on a live track and because `power_on()` is
    itself what clears an existing hold.

    Run 5 is why this ends held rather than holding and releasing: from an
    emergency stop with step 80 stored, `21 81` made the locomotive accelerate
    away, and `loco_info` read speed=80 both while it was held and after the
    release. The hold keeps the station's refresh buffer; it never clears it.
    Run 6 is why the idle may come last: with the layout held, `drive(3, 0)`
    landed and `loco_info` then read speed 0, so the speed telegram does not
    have to precede the hold. Run 7 is why the confirming status read comes
    after it: the STATUS re-read after that same telegram was still `0x05`, so
    the hold survives a per-locomotive zero and the read that decides what to
    claim can happen where it belongs, after the last mutation.

    This is JMRI's `XNetPowerManager` state model with a safe default. Their
    `ON` is `21 81` with locomotives free, their `IDLE` is `80 80` with the
    track live and everything held, and their `OFF` is `21 80`. Our `power on`
    is their IDLE and our `power resume` is their ON - the opposite way round
    from what the names suggest, which is worth knowing if you arrive from
    JMRI. `setPower(ON)` there sends only `21 81`, with no stop before or
    after, which on this station is the runaway of runs 1 and 2.
    """
    # Read first, so `changed` can be computed rather than asserted. The
    # mutations below still run unconditionally: unlike `power off`, this
    # command has to re-assert the hold even on a track that already reads as
    # powered, because that is exactly the run where a stored speed is most
    # likely to be sitting in the station.
    before = station.status()
    completed = [STEP_READ_STATUS_BEFORE]
    steps = _on_steps(address)
    # `power_on()` is INSIDE the try. It sends `21 81` and then verifies, and
    # the verify is a `pause` plus a status read on this station (measured
    # 2026-08-05: the YD7010 never answers the power telegram with `61 01`, so
    # the fast path never fires). A timeout in that read left the track live,
    # nothing holding it, and `run()` rendering a plain `link_timeout` error
    # with no result at all - the caller told the command failed while the
    # layout was energised and free. From here on every failure is PARTIAL.
    try:
        station.power_on()
        completed.append(STEP_POWER_ON)
        station.emergency_stop(address=None)
        completed.append(STEP_STOP_ALL)
        idled = _idle(station, address)
        if idled is not None:
            completed.append(STEP_IDLE_ADDRESS)
        # Last, after the last mutation - see STEP_ORDER.
        status = station.status()
        completed.append(STEP_READ_STATUS)
    except RailctlError as exc:
        return build_power_partial(
            "on", steps=steps, completed=completed, before=before, failure=exc
        )
    return build_power(
        "on",
        status,
        changed=_changed(_power_state(before) != _power_state(status), idled),
        idled=idled,
        completed=completed,
    )


def _power_resume(station: Station) -> CommandResult:
    """Release the hold `power on` left, and nothing else.

    One mutation: `station.power_on()`, which sends `21 81` RESUME_OPS - the
    XpressNet primitive this state is named after, and what clears an emergency
    stop. No stop-all, no speed telegram, no locomotive read: the operator
    asked for the layout to be released and anything else this command sent
    would be a change they did not ask for at the moment they are watching the
    track.

    MEASURED 2026-08-09 (docs/probe-results.md, run 5): this is the telegram
    that starts locomotives. Held with step 80 stored, the locomotive
    accelerated away on the release and `loco_info` read speed=80 on both sides
    of the hold. `build_power` warns accordingly.

    It REFUSES on a dead track. `21 81` is the same telegram that energises,
    and it clears both emergency bits at once, so run this from `0x06` or
    `0x07` - `0x07` is what `power on` followed by `power off` leaves - and
    what came back was a live layout with nothing holding it. That is run 1,
    the runaway, reached through the command whose whole job is to be the
    deliberate half of the release. `railctl power on` is the command that
    energises, and it comes up held.
    """
    before = station.status()
    if not before.track_power:
        # Before any telegram: nothing is sent on this path at all.
        raise TrackPowerError(
            "the track has no voltage, so there is no hold to release; `railctl power on` "
            "energises it and comes up held, and this command releases that hold",
            details={
                "condition": CONDITION_TRACK_DEAD,
                "track_power": before.track_power,
                "emergency_stop": before.emergency_stop,
                "emergency_off": before.emergency_off,
            },
        )
    completed = [STEP_READ_STATUS_BEFORE]
    # `power_on()` is inside the try for the same reason as in `_power_on`: it
    # writes `21 81` and only then verifies, so a failure in the verify has
    # already released the layout. Everything from here is PARTIAL and
    # emphatically not "nothing happened".
    try:
        station.power_on()
        completed.append(STEP_POWER_ON)
        after = station.status()
        completed.append(STEP_READ_STATUS)
    except RailctlError as exc:
        return build_power_partial(
            "resume",
            steps=STEP_ORDER["resume"],
            completed=completed,
            before=before,
            failure=exc,
        )
    return build_power(
        "resume",
        after,
        changed=_power_state(before) != _power_state(after),
        idled=None,
        completed=completed,
    )


def _power_off(station: Station) -> CommandResult:
    """The one state that skips its own mutation when nothing needs doing,
    which is exactly what `changed: false` is for. `power on` cannot do the
    same: it has a hold to assert whatever the track was already doing."""
    before = station.status()
    completed = [STEP_READ_STATUS_BEFORE]
    was_on = before.track_power
    if was_on:
        station.power_off()
        completed += [STEP_POWER_OFF, STEP_READ_STATUS]
        after = station.status()
    else:
        after = before
    return build_power("off", after, changed=was_on, idled=None, completed=completed)


def _changed(power_moved: bool, idled: Idled | None) -> bool | None:
    """`true` beats `null` beats `false`.

    A command that definitely changed something says so; one that definitely
    changed nothing says so; and one that changed the power state not at all
    but sent a telegram to a locomotive it could not read reports UNKNOWN
    rather than the convenient `false`.
    """
    idle_moved = None if idled is None else idled.changed
    if power_moved or idle_moved is True:
        return True
    if idle_moved is None and idled is not None:
        return None
    return False


def _idle(station: Station, address: int | None) -> Idled | None:
    """Send speed 0 to `address`, keeping its stored direction where one can be
    read. `None` when no address is configured: there is nothing to idle.

    Speed 0 is the point of this telegram, and with the new sequence it is the
    one thing that makes a later `power resume` safe for this locomotive: the
    hold does not clear the station's stored speed (measured 2026-08-09, run
    5), so zeroing it is the only way the release leaves it standing. It goes
    out while the layout is already held, which run 6 measured working - the
    telegram landed and `loco_info` then read speed 0 - and run 7 measured that
    it leaves the hold intact. The direction was never
    the point, and sending `Direction.FORWARD` unconditionally overwrote
    whatever the locomotive had.

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
    # UNKNOWN, not False, when the previous speed was never decoded: a
    # 14/27/28-step reply carries no speed `speed.py` defines, and answering
    # "nothing changed" there is this project's founding rule broken in the
    # `changed` field rather than in a CV read.
    changed = (
        None
        if was is None or was.speed is None
        else (was.speed, was.direction)
        != (
            0,
            direction,
        )
    )
    station.drive(address, 0, direction)
    return Idled(
        address=address,
        direction=direction,
        direction_preserved=stored is not None,
        changed=changed,
    )


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
                if wanted == "resume":
                    return _power_resume(station)
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
