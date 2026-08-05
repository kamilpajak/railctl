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
from typing import TYPE_CHECKING, Final

from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    PomReadUnsupportedError,
    RailctlError,
    UnsupportedCommandError,
)
from railctl.station.programming import CvMatcher
from railctl.station.timing import TIMING
from railctl.station.types import Check, DoctorReport
from railctl.xbus.commands import cmd_service_direct_read
from railctl.xbus.cv import CvEncoding
from railctl.xbus.replies import (
    UNSUPPORTED,
    CvValue,
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

_PLACEHOLDER_DETAIL: Final[str] = "not implemented yet"


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
    try:
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
    except UnsupportedCommandError:
        # A 61 82 to the 21 10 result poll is the same refusal as a 61 82 to
        # the read itself - the station can reject either half of the
        # exchange, and both mean the same thing here.
        return UNSUPPORTED
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
    # `_d4_noack` (whether this run's D4 saw 61 13) is not consumed here yet -
    # task-7c's D8 reads it to decide whether it may run at all. Capturing the
    # tuple now, once, avoids reshaping every call site a second time later.
    _d4_noack = False
    if track_powered and resolved_address is not None:
        d4_check, _d4_noack = _check_d4(station, address=resolved_address)
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
        power_before = station.status().track_power
        try:
            checks.append(_check_d5(station))
            for check_id in ("D6", "D7", "D8"):
                checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
        finally:
            station.programmer.exit_service_mode(restore_power=power_before)
    else:
        skip_detail = "programming track disabled (--no-programming-track)"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", skip_detail))

    for check_id in CHECK_IDS[9:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities)


def verdict_lines(report: DoctorReport) -> list[str]:  # placeholder, task-7e implements it
    return ["", "", "", ""]


def exit_code_for_report(report: DoctorReport) -> int:
    return 0 if report.ok else 3
