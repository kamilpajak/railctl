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

from railctl.errors import RailctlError
from railctl.station.types import Check, DoctorReport
from railctl.xbus.replies import StationStatus, StationVersion

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
    d3_check, _track_powered = _check_d3(station, status, allow_power_on=allow_power_on)
    checks.append(d3_check)
    for check_id in CHECK_IDS[4:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities)


def verdict_lines(report: DoctorReport) -> list[str]:  # placeholder, task-7e implements it
    return ["", "", "", ""]


def exit_code_for_report(report: DoctorReport) -> int:
    return 0 if report.ok else 3
