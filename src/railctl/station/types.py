"""The station facade's shared vocabulary: CV addressing rules, results, and
the doctor's report shape.

`Direction`, `StationVersion`, `StationStatus`, `LocoInfo` and `CvEncoding`
are defined in `xbus` and re-exported by `railctl.station.__init__`, not by
this module - importing `xbus` here as well as there would be exactly the
two-places-to-change split the design forbids for `CvEncoding`.

CV144 is family-dependent and must never be treated as a universal
programming lock. On the older ZIMO MX family CV144 is the programming/
update lock, and a non-zero value there blocks writes. ZIMO dropped that
lock for the MS family (change log 2021-05-12: "CV #144 (Programm./Update
lock): dropped, no longer necessary in new decoders") and later reused the
CV: on MS decoders CV144 bit 4 set to 1 turns on a confirmation jingle when a
CV is programmed (change log 2024-05-31). The decoder this tool targets for
0.1.0 is an MS450P22 - MS family - so on this hardware CV144 is a sound
setting, not a lock. `decoder_family` decides the family from
`DECODER_TYPE_CV`, and `treats_cv144_as_lock` answers `False` when the
family is unread (`None`) as well as when it is MS: guessing "locked" for a
decoder type nobody has read yet would abort restores that are perfectly
safe - the false negative this project exists to stop.

`BLIND_WRITE_CVS` excludes CV29 on purpose. CV29 only changes the address a
decoder answers to when bit 5 changes, and the most common reason to write
it at all is enabling RailCom by setting bit 3 - a change that must be
verified by reading the CV back, never written and trusted.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Literal

from railctl.errors import RailctlError
from railctl.station.capabilities import Capabilities
from railctl.xbus.dialect import CvEncoding

Address = int  # a locomotive address, 1..9999, always as the operator sees it
CvNumber = int  # a CV number, 1..1024, user-facing and always 1-based
CvPage = tuple[int, int]  # (CV31 value, CV32 value) for the extended index


class ProgMode(enum.Enum):
    """Which track and protocol a CV operation used - never AUTO once resolved."""

    AUTO = "auto"
    POM = "pom"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class CvSpec:
    """What the caller asked for: a CV number, and optionally a name for
    reporting and the index page it lives behind."""

    cv: CvNumber
    name: str = ""
    page: CvPage | None = None


@dataclass(frozen=True, slots=True)
class CvResult:
    """One resolved CV read or write."""

    cv: CvNumber
    value: int
    mode: ProgMode
    encoding: CvEncoding
    operation: Literal["read", "write"]
    verified: bool | None  # write: read back and confirmed, or not attempted; read: always None
    elapsed: float


@dataclass(frozen=True, slots=True)
class CvReadOutcome:
    """One CV's outcome inside a batch. `result` and `error` are independent:
    a CV skipped because an earlier one in the same batch raised leaves BOTH
    `None` - not attempted, not resolved, not failed - and callers branch on
    `result is None`, never on `error is not None`, or a skip reads as
    success.
    """

    spec: CvSpec
    result: CvResult | None
    error: RailctlError | None


@dataclass(frozen=True, slots=True)
class StationEvent:
    """A notable moment worth surfacing to the operator, not an exception -
    the operation that raised it still completed. `name` is one of
    `EVENT_NAMES`; `payload` carries whatever detail the emitting code has."""

    at: float
    name: str
    detail: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class Check:
    """One row of a `railctl doctor` report."""

    id: str
    title: str
    status: Literal["ok", "fail", "skip", "unknown"]
    detail: str


# Doctor checks D0-D2 establish the basics (link, track power, station
# identity); D3 probes a feature that may legitimately be absent, so an
# "unknown" or "skip" verdict there does not fail the report on its own -
# only D0-D2 failing, or D3 outright failing, does.
_REQUIRED_OK: Final[tuple[str, ...]] = ("D0", "D1", "D2")
_MUST_NOT_FAIL: Final[str] = "D3"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...]
    capabilities: Capabilities

    @property
    def ok(self) -> bool:
        by_id = {check.id: check for check in self.checks}
        for check_id in _REQUIRED_OK:
            check = by_id.get(check_id)
            if check is None or check.status != "ok":
                return False
        must_not_fail = by_id.get(_MUST_NOT_FAIL)
        return must_not_fail is None or must_not_fail.status != "fail"

    def check(self, check_id: str) -> Check | None:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None


ADDRESS_CVS: Final[frozenset[int]] = frozenset({1, 17, 18, 29})
BLIND_WRITE_CVS: Final[frozenset[int]] = frozenset({1, 8, 17, 18})
CV29_LONG_ADDRESS_BIT: Final[int] = 5
PAGE_SELECTOR_CVS: Final[tuple[int, int]] = (31, 32)
INDEXED_CV_RANGE: Final[range] = range(257, 513)
CV144: Final[int] = 144  # meaning depends on the decoder family - see module docstring
DECODER_TYPE_CV: Final[int] = 250
MS_DECODER_TYPES: Final[frozenset[int]] = frozenset({6, 7, 12})  # MS450, MS990, MS491

EVENT_NAMES: Final[tuple[str, ...]] = (
    # diagnostic events - something the operator may need to act on
    "cv.stale_result",
    "cv.write_unverified",
    "cv.unexercised_band",  # emitted by the CV-programming layer
    "page.unverified",
    "loco.in_use_by_other",
    "address.band_unverified",
    "function.group_seeded",  # emitted by the drive/function layer
    # station-state events - emitted by the facade, rendered by `monitor`
    "power.on",
    "power.off",
    "loco.emergency_stop",
    "service.entered",
    "reply.unknown",
)


def decoder_family(decoder_type: int | None) -> Literal["ms", "other", "unknown"]:
    """`None` means `DECODER_TYPE_CV` was never read - the family is unknown,
    never guessed. A report may not claim a family it never measured."""
    if decoder_type is None:
        return "unknown"
    return "ms" if decoder_type in MS_DECODER_TYPES else "other"


def treats_cv144_as_lock(decoder_type: int | None) -> bool:
    """`False` for `None` as well as for the MS family - see the module
    docstring. The restore path must not refuse a safe write just because
    the decoder type was never read; only a CONFIRMED non-MS family locks
    CV144."""
    return decoder_family(decoder_type) == "other"
