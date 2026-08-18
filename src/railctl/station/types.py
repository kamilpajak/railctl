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

import dataclasses
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
    #: write: `True` = an independent read-back (or a decoder-level Ready)
    #: confirmed it; `None` = not measured (`--no-verify`, POM's blind CVs, a
    #: main-track write nothing can check). Never `False`: a read-back that
    #: disagrees raises `CvVerifyError` instead of returning, so `False` would
    #: claim a mismatch nobody measured. read: always `None`.
    verified: bool | None
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
class LayoutState:
    """What a `railctl doctor --power-on` run did to the layout, and the state it
    left behind. Issue #14.

    Every field is tri-state for the same reason every capability is, and for a
    sharper one: an operator reads this before walking up to the track. "The doctor
    did not confirm the hold" and "the doctor confirmed there is no hold" are
    different things to know, and both are different from "the doctor never touched
    the power".

    A hazard is the one place where UNKNOWN is not neutral. A capability nobody
    measured is simply not yet known; a hold nobody confirmed has to be treated as
    absent, because acting on it means standing next to a locomotive that may start.
    So the CLI's own reading of `held` is "anything but True is not safe" - which is
    the opposite direction from `pom_read`, deliberately, and only because this
    field describes a moving train rather than a decoder feature.
    """

    #: `False` - this run did not energise the track and changed nothing about its
    #: power. `True` - it energised it and the station confirmed. `None` - the
    #: power-on telegram went out and the verify failed, so the track MAY be live.
    energised: bool | None = False
    #: The track power the doctor left behind, read off the station's own status at
    #: the end of the run. `None` when the doctor changed nothing, or could not read
    #: it back.
    track_power: bool | None = None
    #: Whether the whole layout is held in emergency stop, read off the station's own
    #: bit and never off the fact that a stop telegram was sent.
    held: bool | None = None
    #: The address that was sent speed 0 so a later release cannot start it, and
    #: whether that telegram was accepted. `idled_address` is `None` when the run had
    #: no address to zero.
    idled_address: int | None = None
    idled: bool | None = None
    #: Whether the locomotive's stored direction was read and kept. `None` when no
    #: speed-0 telegram was accepted, so there is nothing to say either way.
    direction_preserved: bool | None = None
    #: Whether this run is RESPONSIBLE for leaving the layout held, and therefore for
    #: putting the hold back every time one of its own telegrams clears it.
    #:
    #: `True` in two cases: the run energised a dead track and held it, and the run
    #: found the layout already held. The second is not defensive - every service-mode
    #: session ends with resume-operations, which is exactly the telegram that releases
    #: an emergency stop (MEASURED 2026-08-09, run 5: a locomotive held with step 80
    #: stored accelerated away on it), so a plain `railctl doctor` on a held layout
    #: releases a hold it never applied.
    #:
    #: `False` means there is no hold to maintain: the run found none and applied none.
    #: The doctor must not invent one - stopping a layout that was running is a change
    #: nobody asked for - which is why this is a flag and not "hold whenever you can".
    #: Not tri-state, unlike every field above: this says what this run OWES, which is
    #: decided by code and always known, not what the station reported.
    must_leave_held: bool = False


#: The layout as a run that never touched the track power leaves it: nothing
#: energised, nothing held, nothing zeroed. Named once so a doctor report can be
#: compared against it rather than against seven separate fields.
LAYOUT_UNTOUCHED: Final[LayoutState] = LayoutState()


def layout_json(layout: LayoutState) -> dict[str, object]:
    """`LayoutState` as a JSON object, keys read off the dataclass.

    One definition, two callers: the `doctor` envelope's `layout` block, and the
    `details.layout` a probe that died partway attaches to its own exception. A
    second hand-written key list is how the successful ending and the failed one
    start describing the layout with different words.
    """
    return {f.name: getattr(layout, f.name) for f in dataclasses.fields(layout)}


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
    #: What the run did to the layout. Defaulted rather than required because the
    #: overwhelming majority of runs - every one without `--power-on`, and every one
    #: that found the track already live - leave it exactly as `LAYOUT_UNTOUCHED`
    #: describes, and a report that has to state that explicitly invites the one
    #: caller who forgets to.
    layout: LayoutState = LAYOUT_UNTOUCHED

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
#: The five ZIMO MS decoder types, as the catalog's own CV250 description
#: names them: 6 = MS450, 7 = MS990, 8 = MS590, 12 = MS491, 14 = MS540 (see
#: `catalog/zimo.toml`, CV250, and the ZIMO MS manual's decoder table). This
#: set and that description are two copies of one fact, so
#: `tests/station/test_types.py` parses the pairs out of the catalog and
#: refuses any the set does not carry. 8 and 14 were missing here for exactly
#: that reason, and the cost was not cosmetic: `treats_cv144_as_lock` answered
#: True on an MS590 or an MS540, which aborts a safe restore and tells the
#: operator to write CV144 = 0 - destroying the confirmation jingle on a
#: decoder where CV144 was never a lock.
MS_DECODER_TYPES: Final[frozenset[int]] = frozenset({6, 7, 8, 12, 14})

EVENT_NAMES: Final[tuple[str, ...]] = (
    # diagnostic events - something the operator may need to act on
    "cv.stale_result",
    "cv.write_unverified",
    "cv.unexercised_band",  # emitted by the CV-programming layer
    "page.unverified",
    "page.not_selected",
    "loco.in_use_by_other",
    "address.band_unverified",
    "function.group_seeded",  # emitted by the drive/function layer
    # station-state events - emitted by the facade, rendered by `monitor`
    "power.on",
    "power.off",
    "loco.emergency_stop",
    "service.entered",
    "service.session_retried",
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
