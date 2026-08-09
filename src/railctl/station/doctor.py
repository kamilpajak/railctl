"""`Station.probe()`: checks D0-D12 and the human verdict block.

Every check here is READ-ONLY against the decoder - see the design document,
"the doctor never writes a decoder CV" - and every service-mode check restores
the track power state it found before it ran. Three outcomes stay
distinguishable end to end: a capability is `True` (the station proved it),
`False` (the station said `61 82`, or - the one deliberate exception, D4 - the
POM read produced no result at all after three attempts and neither a value
nor `61 13` was ever seen), or `None` (nothing conclusive happened, or the
check did not run). No branch here ever writes `False` for any other reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    PomReadUnsupportedError,
    RailctlError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import CvMatcher
from railctl.station.timing import TIMING
from railctl.station.types import (
    DECODER_TYPE_CV,
    LAYOUT_UNTOUCHED,
    Check,
    DoctorReport,
    LayoutState,
    decoder_family,
)
from railctl.xbus.commands import (
    FunctionAction,
    FunctionGroup,
    cmd_function_group,
    cmd_function_single,
    cmd_loco_info,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_z21_cv_read,
)
from railctl.xbus.cv import CvEncoding
from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET, Z21
from railctl.xbus.replies import (
    UNSUPPORTED,
    CvValue,
    GenericAck,
    LocoInfo,
    NoAck,
    Reply,
    StationStatus,
    StationVersion,
    Unsupported,
)
from railctl.xbus.speed import Direction

if TYPE_CHECKING:
    from railctl.station.facade import Station

PROBE_CV: Final[int] = 8  # ZIMO manufacturer id, known constant 145
PROBE_CV_VALUE: Final[int] = 145
IDENTITY_CVS: Final[tuple[int, ...]] = (7, 8, 250, 1, 17, 18, 28, 29, 144)
RAILCOM_CVS: Final[tuple[int, int]] = (29, 28)

CHECK_IDS: Final[tuple[str, ...]] = (
    "D0",
    "D1",
    "D2",
    "D3",
    "D4",
    "D5",
    "D6",
    "D7",
    "D8",
    "D9",
    "D10",
    "D11",
    "D12",
)

CHECK_TITLES: Final[dict[str, str]] = {
    "D0": "link",
    "D1": "link alive",
    "D2": "station status",
    "D3": "track power",
    "D4": "POM read",
    "D5": "service direct read",
    "D6": "Z21 CV opcodes",
    "D7": "extended CV opcodes",
    "D8": "RailCom sanity",
    "D9": "decoder identity",
    "D10": "address band",
    "D11": "function groups 4/5",
    "D12": "single-function command",
}


def _iso_utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolved_address(station: Station, address: int | None) -> int | None:
    return address if address is not None else station.default_address


def _check_d0(station: Station) -> Check:
    station.link.drain()
    detail = f"{station.link.description} ({station.link.identity})"
    return Check("D0", CHECK_TITLES["D0"], "ok", detail)


def _check_d1(station: Station) -> Check:
    try:
        version: StationVersion = station.version()
    except RailctlError as exc:
        return Check("D1", CHECK_TITLES["D1"], "fail", str(exc))
    station.record(xpressnet_version=version.version, command_station_id=version.station_id)
    detail = (
        f"XpressNet {version.version}, station id 0x{version.station_id:02X} ({version.family})"
    )
    return Check("D1", CHECK_TITLES["D1"], "ok", detail)


def _check_d2(station: Station) -> tuple[Check, StationStatus | None]:
    try:
        status = station.status()
    except RailctlError as exc:
        return Check("D2", CHECK_TITLES["D2"], "fail", str(exc)), None
    power = "track power on" if status.track_power else "track power off"
    detail = (
        f"{power}, emergency_stop={status.emergency_stop}, "
        f"auto_start_mode={status.auto_start_mode}, service_mode={status.service_mode}"
    )
    return Check("D2", CHECK_TITLES["D2"], "ok", detail), status


def _stored_direction(station: Station, address: int) -> Direction | None:
    """The locomotive's current direction, or `None` when the station could not say.

    `None` is UNKNOWN, never "forward": the caller sends `Direction.FORWARD` anyway,
    because leaving a locomotive able to start by itself in order to protect its
    direction would be the wrong way round, but it then reports that the direction
    was not preserved rather than presenting the fallback as the locomotive's own.
    Exactly `commands/power.py::_idle`'s reasoning, applied to the same telegram.
    """
    try:
        return station.loco_info(address).direction
    except RailctlError:
        return None


def _idle_probe_address(station: Station, address: int | None) -> LayoutState:
    """Send speed 0 to the one locomotive this run knows about, while the layout is
    already held.

    MEASURED 2026-08-09 (docs/probe-results.md, "`power on`'s stop-all was in the
    wrong order"): the emergency stop HOLDS the station's refresh buffer and never
    clears it (run 5), so zeroing the stored speed is the only thing that keeps this
    locomotive standing when the hold is later released. Run 6 measured that the
    telegram lands while the layout is held, and run 7 that the hold survives it.
    """
    if address is None:
        return LayoutState(energised=True)
    stored = _stored_direction(station, address)
    try:
        station.drive(address, 0, Direction.FORWARD if stored is None else stored)
    except RailctlError:
        # Not a silent success: this locomotive still holds whatever speed the
        # station has for it, so a later release would start it.
        return LayoutState(energised=True, idled_address=address, idled=False)
    return LayoutState(
        energised=True,
        idled_address=address,
        idled=True,
        direction_preserved=stored is not None,
    )


def _abandon_energised_track(station: Station) -> tuple[Check, bool, LayoutState]:
    """The hold failed on a track this run energised. Put it back as it was found.

    A live track with nothing holding it is the runaway of runs 1 and 2 - the
    locomotive resumed its stored speed the instant power returned, with and without
    a stop telegram sent beforehand. The doctor found this track OFF, so switching it
    back off is not a change the operator did not ask for; it is the only state this
    command is entitled to leave behind once it cannot hold what it started.
    """
    try:
        station.power_off()
    except RailctlError as exc:
        detail = (
            f"track power was turned on, the emergency stop that should hold the layout "
            f"failed, and switching the track back off failed too ({exc}); the track MAY "
            f"BE LIVE with nothing holding it"
        )
        return (
            Check("D3", CHECK_TITLES["D3"], "fail", detail),
            False,
            LayoutState(energised=True, held=False),
        )
    detail = (
        "track power was turned on but the emergency stop that should hold the layout "
        "failed, so the track was switched back off, as it was found"
    )
    return (
        Check("D3", CHECK_TITLES["D3"], "fail", detail),
        False,
        LayoutState(energised=True, track_power=False, held=False),
    )


def _check_d3(
    station: Station, status: StationStatus | None, *, address: int | None, allow_power_on: bool
) -> tuple[Check, bool, LayoutState]:
    """Track power, and - when this run is the thing that energises it - the hold.

    A track the operator already had live is left exactly as found: the doctor is a
    diagnostic, and holding a layout that was running is a change nobody asked for.
    Only the `--power-on` path that actually energises a dead track holds it, because
    only that path created the hazard.
    """
    if status is None:
        detail = "D2 did not produce a status"
        return Check("D3", CHECK_TITLES["D3"], "fail", detail), False, LAYOUT_UNTOUCHED
    if status.track_power:
        detail = "track power already on"
        return Check("D3", CHECK_TITLES["D3"], "ok", detail), True, LAYOUT_UNTOUCHED
    if not allow_power_on:
        detail = "track power is off; re-run with --power-on to verify D4 and D10"
        return Check("D3", CHECK_TITLES["D3"], "unknown", detail), False, LAYOUT_UNTOUCHED
    try:
        station.power_on()
    except RailctlError as exc:
        # `energised=None`, never False: `power_on()` writes the telegram and only
        # then verifies, so a failure here leaves a track that may well be live.
        return Check("D3", CHECK_TITLES["D3"], "fail", str(exc)), False, LayoutState(energised=None)
    try:
        # AFTER the energise, never before. Runs 1 and 2 measured that a stop sent to
        # a dead track changes nothing at all - either the station ignores it or the
        # power-on clears it - and runs 3 and 4 measured that the same telegram sent
        # 0.51 s after the track came up held stored steps 15 and 80.
        station.emergency_stop(address=None)
    except RailctlError:
        return _abandon_energised_track(station)
    layout = _idle_probe_address(station, address)
    detail = (
        f"track power turned on, then the whole layout was held and "
        f"{'no locomotive was zeroed (no --address)' if address is None else f'loco {address} was sent speed 0'}"
        f"; the hold is re-asserted and read back at the end of the run"
    )
    return Check("D3", CHECK_TITLES["D3"], "ok", detail), True, layout


def _settle_hold(station: Station, layout: LayoutState) -> LayoutState:
    """Re-assert the hold this run applied, then read back the state it leaves behind.

    Not belt and braces about an emergency stop wearing off. `CvProgrammer.
    exit_service_mode` sends resume-operations unconditionally, and that telegram is
    exactly what clears a hold - MEASURED 2026-08-09, run 5, where a locomotive held
    with step 80 stored accelerated away on it. So a `--power-on` run with the
    programming track enabled RELEASES the hold D3 applied, halfway through its own
    checks, and D9 does it again. Re-asserting here is what makes `layout.held`
    describe the state the doctor actually leaves behind rather than one it set and
    then undid.

    The doctor never releases. Whether it should was the open question: releasing is
    a single telegram and it would leave the bench as it was found. Run 5 settles it -
    the release is the moment stored speeds start locomotives, and a diagnostic
    command must not be what chooses that moment. `railctl power on` already ends
    held for the same reason and `railctl power resume` is the deliberate half; the
    doctor points at it rather than growing a second way to do the same thing.
    """
    try:
        station.emergency_stop(address=None)
        after = station.status()
    except RailctlError:
        # The telegram went out and the station never said whether it took. UNKNOWN,
        # which the CLI treats as "not safe" - see LayoutState's own docstring.
        return replace(layout, held=None, track_power=None)
    return replace(layout, held=after.emergency_stop, track_power=after.track_power)


_SILENCE_NOTE: Final[str] = (
    "POM read produced no result at all (neither 61 13 nor 61 82) after "
    f"{TIMING.pom_read_attempts} attempts; recorded as unsupported from silence "
    "rather than left unknown, or every AUTO operation would retry POM for "
    f"{TIMING.pom_read_attempts * TIMING.pom_result:.0f}s on every call. A POM read result needs "
    "a RailCom "
    "DETECTOR on the layout - the command station's cutout alone does not deliver one - so check "
    "for that before suspecting the decoder, and re-run the doctor once one is fitted."
)


def _check_d4(station: Station, *, address: int) -> tuple[Check, bool]:
    # D4 always measures. A capability this check itself wrote from silence -
    # or a `61 82` `pom_read=False` learned outside the doctor - must not
    # short-circuit `CvProgrammer.pom_read` (programming.py:1044) before this
    # run's own probe ever reaches the wire: that is precisely the stale
    # verdict this check exists to refresh, and programming.py's own message
    # promises "`railctl doctor` re-probes and overwrites it". Clearing
    # `pom_echo_zero_based` too, since `pom_read` only re-learns it while it
    # is `None` (programming.py:1138).
    cleared_notes = tuple(note for note in station.capabilities.notes if note != _SILENCE_NOTE)
    station.record(
        pom_read=None,
        pom_read_provenance=None,
        pom_result_channel=None,
        pom_echo_zero_based=None,
        notes=cleared_notes,
    )
    try:
        result = station.programmer.pom_read(PROBE_CV, address=address)
    except PomReadUnsupportedError:
        # `pom_read=False` is written by pom_read() itself, one layer down, on
        # the 61 82 that entitles it. Recorded here too, so both ways of
        # reaching False carry how they got there.
        station.record(pom_read_provenance="unsupported")
        return Check("D4", CHECK_TITLES["D4"], "ok", "POM read unsupported (61 82)"), False
    except DecoderNoAckError:
        detail = (
            "decoder answered 61 13 (no acknowledgement) on the operations track; "
            "check RailCom wiring/configuration on the decoder and re-run the doctor"
        )
        return Check("D4", CHECK_TITLES["D4"], "unknown", detail), True
    except DecoderNotRespondingError:
        station.record(pom_read=False, pom_read_provenance="silence", pom_result_channel="none")
        capabilities = station.capabilities.with_note(_SILENCE_NOTE)
        station.record(notes=capabilities.notes)
        return Check("D4", CHECK_TITLES["D4"], "ok", _SILENCE_NOTE), False
    except RailctlError as exc:
        # A short circuit, a track-power drop mid-read, or a link fault - none
        # of these is a verdict about the capability, so nothing is recorded:
        # unknown, never False. Caught last, and separately from the three
        # ProgrammingError subclasses above, so this never shadows them.
        return Check("D4", CHECK_TITLES["D4"], "unknown", str(exc)), False
    if result.value == PROBE_CV_VALUE:
        detail = f"POM read confirmed (CV{PROBE_CV}={result.value})"
    else:
        detail = (
            f"POM read confirmed, but CV{PROBE_CV}={result.value}, expected the ZIMO "
            f"manufacturer id {PROBE_CV_VALUE} - verify this is a ZIMO decoder"
        )
    return Check("D4", CHECK_TITLES["D4"], "ok", detail), False


def _exchange(station: Station, telegram: bytes, *, timeout: float) -> Reply:
    """station.exchange, with the one refusal that is a real answer turned back into a value.

    61 82 is the ONLY reply entitled to write a capability False, and Station.exchange raises it.
    A check that let that raise reach its own `except RailctlError` would record "fail" and leave
    the capability at None - the one reply that CAN say "no" turned into "unknown", which is the
    exact M1 failure this project exists to prevent, running backwards.
    """
    try:
        return station.exchange(telegram, timeout=timeout)
    except UnsupportedCommandError:
        return UNSUPPORTED


def _service_probe(
    station: Station, telegram: bytes, cv: int, encoding: CvEncoding
) -> CvValue | Unsupported | NoAck | None:
    """One service-mode read, already inside a service-mode session the caller
    entered. Returns the value, a definitive Unsupported/NoAck, or None for
    'nothing conclusive' - never raises for a plain timeout, because the
    caller (D5-D8) needs to keep going to the next check either way."""
    reply = _exchange(station, telegram, timeout=TIMING.li_ack_programming)
    if isinstance(reply, Unsupported):
        return reply
    matcher = CvMatcher(encoding, cv)
    # No try/except UnsupportedCommandError here: CvProgrammer.await_result
    # (programming.py:995-1005) catches that exception itself, around its own
    # 21 10 31 poll, and explicitly documents "never raised past this loop" -
    # a poll's 61 82 just switches the loop from polling to passive waiting,
    # it is never treated as a durable capability verdict. Wrapping this call
    # in the same except here was dead code that could never run, and its
    # comment claimed the opposite of what programming.py's own comment says
    # about the identical reply.
    outcome = station.programmer.await_result(
        matcher,
        timeout=TIMING.service_result,
        first_delay=TIMING.service_first_poll_delay,
        interval=TIMING.service_poll_interval,
        exchange_timeout=TIMING.li_ack_programming,
        allow_poll=True,
        ready_means_done=False,
        context="service",
    )
    if isinstance(outcome, (CvValue, NoAck)):
        return outcome
    return None  # a stray reply or TimedOut: inconclusive, not a capability verdict


def _check_d5(station: Station) -> Check:
    try:
        outcome = _service_probe(
            station, cmd_service_direct_read(PROBE_CV), PROBE_CV, CvEncoding.SERVICE_DIRECT
        )
    except RailctlError as exc:
        return Check("D5", CHECK_TITLES["D5"], "fail", str(exc))
    if isinstance(outcome, Unsupported):
        station.record(service_direct_cv=False)
        return Check("D5", CHECK_TITLES["D5"], "ok", "service direct read unsupported (61 82)")
    if isinstance(outcome, CvValue):
        station.record(service_direct_cv=True)
        detail = f"service direct read confirmed (CV{PROBE_CV}={outcome.value})"
        return Check("D5", CHECK_TITLES["D5"], "ok", detail)
    if isinstance(outcome, NoAck):
        detail = "decoder answered 61 13 on the programming track"
        return Check("D5", CHECK_TITLES["D5"], "unknown", detail)
    return Check("D5", CHECK_TITLES["D5"], "unknown", "no result within the service-mode budget")


def _check_d6(station: Station) -> Check:
    z21_probe_cv = 1  # spec's own literal example: 23 11 00 00
    try:
        outcome = _service_probe(
            station, cmd_z21_cv_read(z21_probe_cv), z21_probe_cv, CvEncoding.Z21_16BIT
        )
    except RailctlError as exc:
        return Check("D6", CHECK_TITLES["D6"], "fail", str(exc))
    if isinstance(outcome, Unsupported):
        station.record(z21_cv_opcodes=False)
        return Check("D6", CHECK_TITLES["D6"], "ok", "Z21 CV opcode 23 11 unsupported (61 82)")
    if isinstance(outcome, CvValue):
        station.record(z21_cv_opcodes=True)
        detail = f"Z21 CV opcode confirmed (CV{z21_probe_cv}={outcome.value})"
        return Check("D6", CHECK_TITLES["D6"], "ok", detail)
    if isinstance(outcome, NoAck):
        return Check("D6", CHECK_TITLES["D6"], "unknown", "decoder answered 61 13")
    return Check("D6", CHECK_TITLES["D6"], "unknown", "no result within the service-mode budget")


def _check_d7(station: Station) -> Check:
    high_cv = 257  # first CV of page 1 - 22 19 01, the design's own example
    try:
        low = _service_probe(
            station, cmd_service_ext_read(PROBE_CV), PROBE_CV, CvEncoding.SERVICE_EXT
        )
        high = _service_probe(
            station, cmd_service_ext_read(high_cv), high_cv, CvEncoding.SERVICE_EXT
        )
    except RailctlError as exc:
        return Check("D7", CHECK_TITLES["D7"], "fail", str(exc))
    low_ok, high_ok = isinstance(low, CvValue), isinstance(high, CvValue)
    if low_ok and high_ok:
        detail = f"extended read confirmed on CV{PROBE_CV} and CV{high_cv}"
        station.record(service_ext_cv=True)
        return Check("D7", CHECK_TITLES["D7"], "ok", detail)
    low_unsupported, high_unsupported = isinstance(low, Unsupported), isinstance(high, Unsupported)
    if low_unsupported or high_unsupported:
        failed_cv = PROBE_CV if low_unsupported else high_cv
        station.record(service_ext_cv=False)
        detail = f"extended opcodes rejected for CV{failed_cv}'s band (61 82)"
        return Check("D7", CHECK_TITLES["D7"], "ok", detail)
    # Neither band was definitively rejected, yet they disagree (one silent,
    # one NoAck, or both inconclusive): a decoder-side non-answer is not a
    # station capability. Leave service_ext_cv at None rather than guessing.
    inconclusive_cv = PROBE_CV if not low_ok else high_cv
    detail = f"CV{inconclusive_cv}'s band gave no conclusive result within the service-mode budget"
    return Check("D7", CHECK_TITLES["D7"], "unknown", detail)


def _check_d8(station: Station, *, d4_noack: bool, d5_passed: bool) -> Check:
    if not (d4_noack and d5_passed):
        detail = "runs only after D4 answers 61 13 with D5 already confirmed"
        return Check("D8", CHECK_TITLES["D8"], "skip", detail)
    cv29, cv28 = RAILCOM_CVS
    try:
        result_29 = _service_probe(
            station, cmd_service_direct_read(cv29), cv29, CvEncoding.SERVICE_DIRECT
        )
        result_28 = _service_probe(
            station, cmd_service_direct_read(cv28), cv28, CvEncoding.SERVICE_DIRECT
        )
    except RailctlError as exc:
        return Check("D8", CHECK_TITLES["D8"], "fail", str(exc))
    if isinstance(result_29, CvValue) and isinstance(result_28, CvValue):
        bit3 = "set" if result_29.value & 0x08 else "clear"
        channel = result_28.value & 0x03
        detail = (
            f"CV{cv29}={result_29.value} (bit 3 {bit3}), CV{cv28}={result_28.value} "
            f"(bits 0-1 = {channel:02b}) - RailCom needs CV{cv29} bit 3 set and a "
            f"valid CV{cv28} channel selection"
        )
        return Check("D8", CHECK_TITLES["D8"], "ok", detail)
    return Check("D8", CHECK_TITLES["D8"], "unknown", "CV29/CV28 not readable in service mode")


def _best_effort_read(station: Station, cv: int) -> tuple[int | None, str | None]:
    """The POM half of one identity read: a value when `pom_read` is proven
    True and an address is resolvable, the failure's type name when POM was
    tried and raised, and `(None, None)` when POM was never eligible.

    Service mode is deliberately NOT here. It used to be, one `service_read`
    per CV, and that cost eight of nine reads on the bench (issue #22).
    `_identity_reads` now batches whatever POM did not deliver into a single
    session, so this function no longer takes `use_programming_track` - it
    cannot reach the programming track at all."""
    caps = station.capabilities
    address = station.default_address
    if caps.pom_read is True and address is not None:
        try:
            return station.programmer.pom_read(cv, address=address).value, None
        except RailctlError as exc:
            return None, type(exc).__name__
    return None, None


def _identity_reads(
    station: Station, *, use_programming_track: bool
) -> dict[int, tuple[int | None, str | None]]:
    """Every identity CV, POM first where it is proven, then ONE service-mode
    session for whatever POM did not deliver.

    The session count is the reason this exists rather than a loop over
    `_best_effort_read`. Reopening service mode per CV cost eight of nine
    reads on the bench (issue #22); `service_read_many` opens it once.

    POM stays per-CV because it needs no session at all - it runs on the
    operations track - so nothing is gained by batching it and the existing
    "POM first when proven" order is preserved exactly.
    """
    results = {cv: _best_effort_read(station, cv) for cv in IDENTITY_CVS}
    remaining = [cv for cv, (value, _) in results.items() if value is None]
    if not use_programming_track or not remaining:
        return results
    for outcome in station.programmer.service_read_many(remaining):
        cv = outcome.spec.cv
        pom_reason = results[cv][1]
        if outcome.result is not None:
            results[cv] = (outcome.result.value, None)
        else:
            # The service failure names the reason, not POM's earlier one:
            # service mode is the path that actually decided this CV.
            reason = type(outcome.error).__name__ if outcome.error is not None else pom_reason
            results[cv] = (None, reason)
    return results


def _check_d9(station: Station, *, use_programming_track: bool) -> Check:
    """Status vocabulary, which this check got wrong until a hardware run showed it:

    * `"skip"` - no read path was even ATTEMPTED. That happens when POM read is
      not proven and the programming track is disabled, so there was nothing to
      try. A deliberate opt-out, which is what `"skip"` means here.
    * `"unknown"` - a path WAS tried and no CV came back. The check ran and
      established nothing. It previously said `"skip"` for this, which claims we
      chose not to look.
    * `"ok"` - at least one identity CV was read.

    `use_programming_track` is honoured here and not only around D5-D8, because
    `_best_effort_read` falls through to `service_read`, which drives the
    programming track. Passing the flag down rather than gating the whole check
    keeps the POM path available: reading identity over POM on the main track is
    legitimate when POM read is proven, and `--no-programming-track` says nothing
    against it.
    """
    try:
        results = _identity_reads(station, use_programming_track=use_programming_track)
    except RailctlError as exc:
        # Every other check has this; D9 did not, and the batching change made
        # the omission reachable. `service_read_many`'s `finally` calls
        # `exit_service_mode`, which raises `StationBusyError` when the station
        # will not leave service mode - that used to be caught per CV inside
        # `_best_effort_read` and would now escape `run_probe` entirely,
        # losing D10-D12 and the whole report over one check's failure.
        return Check("D9", CHECK_TITLES["D9"], "fail", str(exc))
    values = {cv: value for cv, (value, _) in results.items()}
    family = decoder_family(values[DECODER_TYPE_CV])
    read_count = sum(1 for value in values.values() if value is not None)
    reasons = sorted({reason for _, reason in results.values() if reason is not None})
    rendered = ", ".join(
        f"CV{cv}={values[cv]}" if values[cv] is not None else f"CV{cv}=?" for cv in IDENTITY_CVS
    )
    detail = f"decoder family: {family}; {rendered}"
    if read_count:
        # A partial read is still "ok" - identity was established - but the
        # reasons the other CVs failed are worth carrying. `CV28=?` says WHICH
        # one did not answer; without this it never says why, and "the decoder
        # ignored it" and "the link faulted" look identical in the report.
        partial = f"; some reads failed ({reasons})" if reasons else ""
        return Check("D9", CHECK_TITLES["D9"], "ok", f"{detail}{partial}")
    if not reasons:
        attempted = "no read path was attempted: POM read is not proven"
        if not use_programming_track:
            attempted += " and the programming track is disabled"
        return Check("D9", CHECK_TITLES["D9"], "skip", f"{detail}; {attempted}")
    # "no identity CV was read", not "every read failed": a batch cut short by
    # a short circuit leaves most CVs never attempted, and claiming they failed
    # is the same error as reporting `skip` for a check that ran. The reasons
    # list is what carries the causes, and only for the CVs that produced one.
    return Check(
        "D9", CHECK_TITLES["D9"], "unknown", f"{detail}; no identity CV was read ({reasons})"
    )


AddressFormOutcome = Literal["accepted", "rejected", "ambiguous"]


def _classify_address_form(
    station: Station, telegram: bytes, *, timeout: float
) -> AddressFormOutcome:
    """One address-form probe for D10.

    "rejected" covers the two replies that DEFINITIVELY say this address form is
    wrong: `Unsupported` (61 82), and the bare `ValueError` `station.exchange`
    raises for interface status 0x09 (facade.py, `INTERFACE_STATUS_USAGE`) - the
    discriminator the brief names for a rejected address form. Both are caught
    here, not left to escape and abort the whole probe.

    Any other `RailctlError` is not caught: it is a real fault (a damaged
    cable, an interface problem), not a verdict about this address form, and
    must reach `_check_d10`'s own `except RailctlError` as a `"fail"`, not be
    silently absorbed into "rejected" or "ambiguous".

    "ambiguous" is everything else that is not a `LocoInfo` - a bare
    `GenericAck`, a `TRANSIENT_REPLIES` member, or any other reply type. None
    of those says the address form was rejected, only that this particular
    exchange did not measure it either way, so `_check_d10` must never treat
    "ambiguous" the same as "rejected" when deciding whether to write
    `loco_address_threshold`.
    """
    try:
        reply = station.exchange(telegram, timeout=timeout)
    except UnsupportedCommandError:
        return "rejected"
    except ValueError:
        return "rejected"
    return "accepted" if isinstance(reply, LocoInfo) else "ambiguous"


def _check_d10(station: Station, *, address: int | None, track_powered: bool) -> Check:
    if not track_powered:
        detail = "track power is off; re-run with --power-on to verify D10"
        return Check("D10", CHECK_TITLES["D10"], "unknown", detail)
    resolved = _resolved_address(station, address)
    if resolved is None or resolved not in DIVERGENCE_BAND:
        detail = "no address in 100..127 given; pass --address in that range"
        return Check("D10", CHECK_TITLES["D10"], "skip", detail)
    try:
        xpressnet_outcome = _classify_address_form(
            station,
            cmd_loco_info(resolved, threshold=XPRESSNET.long_address_threshold),
            timeout=TIMING.li_ack_normal,
        )
        z21_outcome = _classify_address_form(
            station,
            cmd_loco_info(resolved, threshold=Z21.long_address_threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D10", CHECK_TITLES["D10"], "fail", str(exc))
    if xpressnet_outcome == "accepted" and z21_outcome == "rejected":
        station.record(loco_address_threshold=XPRESSNET.long_address_threshold)
        detail = f"address {resolved} answers only the XpressNet (long from 100) form"
        return Check("D10", CHECK_TITLES["D10"], "ok", detail)
    if z21_outcome == "accepted" and xpressnet_outcome == "rejected":
        station.record(loco_address_threshold=Z21.long_address_threshold)
        detail = f"address {resolved} answers only the Z21 (long from 128) form"
        return Check("D10", CHECK_TITLES["D10"], "ok", detail)
    if xpressnet_outcome == "accepted" and z21_outcome == "accepted":
        detail = (
            f"address {resolved} answers identically under both encodings; threshold unresolved"
        )
        return Check("D10", CHECK_TITLES["D10"], "ok", detail)
    if xpressnet_outcome == "rejected" and z21_outcome == "rejected":
        detail = f"address {resolved} is rejected under both encodings; threshold unresolved"
        return Check("D10", CHECK_TITLES["D10"], "ok", detail)
    detail = (
        f"address {resolved} gave no conclusive answer under one or both encodings "
        "(neither a loco-info reply nor a definite rejection); threshold unresolved"
    )
    return Check("D10", CHECK_TITLES["D10"], "unknown", detail)


def _check_d11(station: Station, *, address: int | None) -> Check:
    resolved = _resolved_address(station, address)
    if resolved is None:
        detail = "no locomotive address given; pass --address"
        return Check("D11", CHECK_TITLES["D11"], "skip", detail)
    threshold = station.threshold
    try:
        g4 = _exchange(
            station,
            cmd_function_group(resolved, FunctionGroup.G4, 0, threshold=threshold),
            timeout=TIMING.li_ack_normal,
        )
        g5 = _exchange(
            station,
            cmd_function_group(resolved, FunctionGroup.G5, 0, threshold=threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D11", CHECK_TITLES["D11"], "fail", str(exc))
    if isinstance(g4, Unsupported) or isinstance(g5, Unsupported):
        station.record(function_groups_4_5=False)
        return Check("D11", CHECK_TITLES["D11"], "ok", "function groups 4/5 unsupported (61 82)")
    if isinstance(g4, GenericAck) and isinstance(g5, GenericAck):
        station.record(function_groups_4_5=True)
        return Check("D11", CHECK_TITLES["D11"], "ok", "function groups 4/5 accepted (F13-F28 off)")
    # Neither a real ack nor 61 82 for at least one of the two groups - a
    # TRANSIENT_REPLIES member (StationBusy, ShortCircuit, ...) or any other
    # unexpected reply. None of those says anything about whether the opcode
    # is implemented (replies.py's own docstring on TRANSIENT_REPLIES), so
    # nothing is recorded: unknown, never a guess in either direction.
    detail = "function groups 4/5 gave no conclusive answer (neither an ack nor 61 82)"
    return Check("D11", CHECK_TITLES["D11"], "unknown", detail)


def _check_d12(station: Station, *, address: int | None) -> Check:
    resolved = _resolved_address(station, address)
    if resolved is None:
        detail = "no locomotive address given; pass --address"
        return Check("D12", CHECK_TITLES["D12"], "skip", detail)
    try:
        reply = _exchange(
            station,
            cmd_function_single(resolved, 0, FunctionAction.OFF, threshold=station.threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D12", CHECK_TITLES["D12"], "fail", str(exc))
    if isinstance(reply, Unsupported):
        station.record(single_function_cmd=False)
        return Check(
            "D12", CHECK_TITLES["D12"], "ok", "single-function command unsupported (61 82)"
        )
    if isinstance(reply, GenericAck):
        station.record(single_function_cmd=True)
        return Check("D12", CHECK_TITLES["D12"], "ok", "single-function command accepted (F0 off)")
    # Neither a real ack nor 61 82 - a TRANSIENT_REPLIES member or any other
    # unexpected reply. Nothing is recorded: unknown, never a guess.
    detail = "single-function command gave no conclusive answer (neither an ack nor 61 82)"
    return Check("D12", CHECK_TITLES["D12"], "unknown", detail)


def run_probe(
    station: Station,
    *,
    address: int | None = None,
    allow_power_on: bool = False,
    use_programming_track: bool = True,
    now_utc: Callable[[], str] | None = None,
) -> DoctorReport:
    checks: list[Check] = [_check_d0(station), _check_d1(station)]
    d2_check, status = _check_d2(station)
    checks.append(d2_check)
    # Resolved BEFORE D3, not after: D3 is where a `--power-on` run zeroes the
    # locomotive it is about to probe, and it can only do that if it already knows
    # which address that is.
    resolved_address = _resolved_address(station, address)
    d3_check, track_powered, layout = _check_d3(
        station, status, address=resolved_address, allow_power_on=allow_power_on
    )
    checks.append(d3_check)

    d4_noack = False
    if track_powered and resolved_address is not None:
        d4_check, d4_noack = _check_d4(station, address=resolved_address)
    elif not track_powered:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "unknown", "track power is off; re-run with --power-on"
        )
    else:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "skip", "no locomotive address given; pass --address"
        )
    checks.append(d4_check)

    if use_programming_track:
        # Not gated on `track_powered`. This batch used to share D3's gate,
        # on the reasoning that entering service mode cuts main power and
        # that leaving it re-energises the track behind the operator's back.
        # Measured on the bench 2026-08-06 (docs/probe-results.md, "Service
        # mode needs no track power"), both are false on this station: a
        # service read returned CV8=145 four times out of four with the
        # track dead, and a multimeter on the rails never saw the track
        # energise on exit. Gating cost the operator three capabilities and
        # bought nothing. Issue #20.
        #
        # `power_before` still decides what exit_service_mode restores, so
        # an unpowered bench is left unpowered - that part was never in
        # dispute and stays exactly as it was.
        #
        # Scope: measured on the YD7010 only. `exit_service_mode` sends
        # resume-operations unconditionally, and on a station where that
        # telegram DOES energise a dead track, this batch would briefly power
        # a layout whose operator declined --power-on. Adding a station means
        # measuring that. The fix would then be a capability gating the EXIT
        # path, not this entry gate: gating entry costs the three
        # capabilities again, and D9 already drives the programming track
        # through the identical exit with no gate of its own - which is why
        # this precondition protected nothing even before it was measured.
        power_before = station.status().track_power
        try:
            checks.append(_check_d5(station))
            checks.append(_check_d6(station))
            checks.append(_check_d7(station))
            d5_passed = station.capabilities.service_direct_cv is True
            checks.append(_check_d8(station, d4_noack=d4_noack, d5_passed=d5_passed))
        finally:
            station.programmer.exit_service_mode(restore_power=power_before)
    else:
        skip_detail = "programming track disabled (--no-programming-track)"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", skip_detail))

    checks.append(_check_d9(station, use_programming_track=use_programming_track))
    checks.append(_check_d10(station, address=address, track_powered=track_powered))
    checks.append(_check_d11(station, address=address))
    checks.append(_check_d12(station, address=address))
    if track_powered and layout.energised is True:
        layout = _settle_hold(station, layout)
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities, layout=layout)


def _primary_cv_path(caps: Capabilities) -> str:
    if caps.pom_read is True:
        channel = caps.pom_result_channel or "unknown"
        return f"POM (results arrive via {channel})"
    if caps.pom_read is False:
        if caps.pom_result_channel == "none":
            return "POM unavailable (silence, not 61 82); see Fallback"
        return "POM unavailable (61 82); see Fallback"
    return "unknown (re-run the doctor to establish this)"


def _fallback(caps: Capabilities) -> str:
    if caps.service_direct_cv is True:
        return "service mode, direct opcodes, CV1-255 only"
    if caps.z21_cv_opcodes is True:
        return "service mode, Z21 opcodes, CV1-1024"
    if caps.service_ext_cv is True:
        return "service mode, extended opcodes"
    if (
        caps.service_direct_cv is None
        and caps.z21_cv_opcodes is None
        and caps.service_ext_cv is None
    ):
        return "unknown (re-run the doctor)"
    return "unavailable - service-mode opcodes unconfirmed"


def _cv_above_255(caps: Capabilities) -> str:
    if caps.z21_cv_opcodes is True:
        return "POM (write) + Z21 opcodes (read), CV1-1024"
    if caps.service_ext_cv is True:
        return "POM (write) + extended opcodes (read)"
    if caps.z21_cv_opcodes is False and caps.service_ext_cv is False:
        return "POM only (extended opcodes rejected: 61 82)"
    return "unknown (re-run the doctor with an address to establish this)"


def _loco_addresses(caps: Capabilities) -> str:
    if caps.loco_address_threshold == 100:
        return "1-99 short, 100+ long (XpressNet form confirmed)"
    if caps.loco_address_threshold == 128:
        return "1-127 short, 128+ long (Z21 form confirmed)"
    return "100..127 unknown (re-run with --address in that range)"


def verdict_lines(report: DoctorReport) -> list[str]:
    caps = report.capabilities
    return [
        f"Primary CV path: {_primary_cv_path(caps)}",
        f"Fallback:        {_fallback(caps)}",
        f"CV > 255:        {_cv_above_255(caps)}",
        f"Loco addresses:  {_loco_addresses(caps)}",
    ]


def exit_code_for_report(report: DoctorReport) -> int:
    return 0 if report.ok else 3
