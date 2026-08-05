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
    Check,
    DoctorReport,
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


def _check_d3(
    station: Station, status: StationStatus | None, *, allow_power_on: bool
) -> tuple[Check, bool]:
    if status is None:
        return Check("D3", CHECK_TITLES["D3"], "fail", "D2 did not produce a status"), False
    if status.track_power:
        return Check("D3", CHECK_TITLES["D3"], "ok", "track power already on"), True
    if not allow_power_on:
        detail = "track power is off; re-run with --power-on to verify D4 and D10"
        return Check("D3", CHECK_TITLES["D3"], "unknown", detail), False
    try:
        station.power_on()
    except RailctlError as exc:
        return Check("D3", CHECK_TITLES["D3"], "fail", str(exc)), False
    return Check("D3", CHECK_TITLES["D3"], "ok", "track power turned on"), True


_SILENCE_NOTE: Final[str] = (
    "POM read produced no result at all (neither 61 13 nor 61 82) after "
    f"{TIMING.pom_read_attempts} attempts; recorded as unsupported from silence "
    "rather than left unknown, or every AUTO operation would retry POM for "
    "several seconds forever. Fix RailCom on the decoder and re-run the doctor."
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
        pom_result_channel=None,
        pom_echo_zero_based=None,
        notes=cleared_notes,
    )
    try:
        result = station.programmer.pom_read(PROBE_CV, address=address)
    except PomReadUnsupportedError:
        return Check("D4", CHECK_TITLES["D4"], "ok", "POM read unsupported (61 82)"), False
    except DecoderNoAckError:
        detail = (
            "decoder answered 61 13 (no acknowledgement) on the operations track; "
            "check RailCom wiring/configuration on the decoder and re-run the doctor"
        )
        return Check("D4", CHECK_TITLES["D4"], "unknown", detail), True
    except DecoderNotRespondingError:
        station.record(pom_read=False, pom_result_channel="none")
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


def _best_effort_read(station: Station, cv: int) -> int | None:
    """Read one CV through whichever path D4-D7 already proved works: POM
    first if `pom_read` is proven True and an address is resolvable, then a
    single high-level `service_read` call, which already walks
    SERVICE_ENCODING_ORDER (Z21, then direct, then extended) internally and
    raises when none of those is proven. This file does no band or page
    arithmetic of its own - that stays inside `service_read`, exactly as Rule
    2 under `station/` requires."""
    caps = station.capabilities
    address = station.default_address
    if caps.pom_read is True and address is not None:
        try:
            return station.programmer.pom_read(cv, address=address).value
        except RailctlError:
            pass
    try:
        return station.programmer.service_read(cv).value
    except RailctlError:
        return None


def _check_d9(station: Station) -> Check:
    values = {cv: _best_effort_read(station, cv) for cv in IDENTITY_CVS}
    family = decoder_family(values[DECODER_TYPE_CV])
    read_count = sum(1 for value in values.values() if value is not None)
    rendered = ", ".join(
        f"CV{cv}={values[cv]}" if values[cv] is not None else f"CV{cv}=?" for cv in IDENTITY_CVS
    )
    status = "ok" if read_count else "skip"
    return Check("D9", CHECK_TITLES["D9"], status, f"decoder family: {family}; {rendered}")


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
    d3_check, track_powered = _check_d3(station, status, allow_power_on=allow_power_on)
    checks.append(d3_check)

    resolved_address = _resolved_address(station, address)
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

    if use_programming_track and track_powered:
        # Gated on the same `track_powered` D3 already established, not merely
        # on the flag: entering service mode cuts main power, and leaving it
        # again (exit_service_mode) unconditionally sends resume-operations
        # (cmd_track_power_on()) to check the station left service mode -
        # briefly re-energising the main track even when the operator never
        # authorised power at all. D3 already recorded "unknown - re-run with
        # --power-on" for an unpowered bench with no --power-on; driving the
        # programming track here anyway would silently overrule that refusal.
        power_before = station.status().track_power
        try:
            checks.append(_check_d5(station))
            checks.append(_check_d6(station))
            checks.append(_check_d7(station))
            d5_passed = station.capabilities.service_direct_cv is True
            checks.append(_check_d8(station, d4_noack=d4_noack, d5_passed=d5_passed))
        finally:
            station.programmer.exit_service_mode(restore_power=power_before)
    elif use_programming_track:
        detail = "track power is off; re-run with --power-on to run D5-D8"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "unknown", detail))
    else:
        skip_detail = "programming track disabled (--no-programming-track)"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", skip_detail))

    checks.append(_check_d9(station))
    checks.append(_check_d10(station, address=address, track_powered=track_powered))
    checks.append(_check_d11(station, address=address))
    checks.append(_check_d12(station, address=address))
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities)


def _primary_cv_path(caps: Capabilities) -> str:
    if caps.pom_read is True:
        channel = caps.pom_result_channel or "unknown"
        return f"POM (results arrive via {channel})"
    if caps.pom_read is False:
        return "POM unavailable (61 82); see Fallback"
    return "unknown (re-run the doctor to establish this)"


def _fallback(caps: Capabilities) -> str:
    if caps.service_direct_cv is True:
        return "service mode, direct opcodes, CV1-255 only"
    if caps.z21_cv_opcodes is True:
        return "service mode, Z21 opcodes, CV1-1024"
    if caps.service_ext_cv is True:
        return "service mode, extended opcodes"
    if caps.service_direct_cv is None and caps.z21_cv_opcodes is None and caps.service_ext_cv is None:
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
