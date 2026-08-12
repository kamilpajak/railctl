# src/railctl/backup/mapping.py
"""`CvReadOutcome` -> file status: the design's C5 table, in one place.

The backup layer matches on exception TYPES only, never on telegram bytes,
and this module is the single owner of that match so the CLI cannot invent
its own. The `detail` string for a non-`ok` row is the station's own opaque
text (`str(error)`) - reported, never composed here.

This is the one module in the package that names station types; `types.py`
and `file.py` stay standard-library-only so M10's restore can read a file
with no station attached.
"""

from __future__ import annotations

from typing import Final

from railctl.backup.types import SOURCE_CATALOG, CvRecord, ReadStatus
from railctl.errors import (
    CvOutOfRangeError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    RailctlError,
)
from railctl.station import CvReadOutcome

#: The detail for a CV whose outcome carries neither a result nor an error -
#: `CvReadOutcome`'s contract for "never attempted", e.g. the rest of a batch
#: after an abort. The writer of a deliberate skip (a CV the resolved mode
#: cannot reach) supplies its own, more specific detail instead.
NOT_ATTEMPTED_DETAIL: Final[str] = "not attempted"

#: Silence in both flavours - the station reported no acknowledgement, or
#: nothing came back at all after every attempt. A `no_response` row, never
#: an `error`: the design has no "does not exist" status precisely because
#: these two are indistinguishable from an unimplemented CV.
_NO_RESPONSE_ERRORS: Final[tuple[type[RailctlError], ...]] = (
    DecoderNoAckError,
    DecoderNotRespondingError,
)

#: "This mode cannot reach this CV" - a pre-flight refusal inside the
#: programmer, no telegram ever sent, so the row is a recorded decision
#: (`skipped`), not a failure. Deliberately NARROWER than the four-class skip
#: set `cv read` uses (ADDENDUM A4): `PomReadUnsupportedError` (a live 61 82
#: refusal) and `ServiceEncodingUnknownError` (no proven encoding to read
#: with) are the instrument failing to measure mid-run, and backup's exit
#: code rides on `summary.complete` - mapping them to `skipped` would keep
#: `complete` true and let a refused run exit 0. Both fall through to
#: `ERROR` below, per the design's C5 rule (unsupported -> error).
_SKIP_ERRORS: Final[tuple[type[RailctlError], ...]] = (
    CvOutOfRangeError,
    IndexPageRequiredError,
)


def status_for(outcome: CvReadOutcome) -> tuple[ReadStatus, str | None]:
    """One outcome's file status and detail, per the design's C5 table.

    Branches on `result is None` first, per `CvReadOutcome`'s own contract.
    Everything that is neither silence nor a skip - a refusal, a short
    circuit, a garbled reply, a link timeout, a station still busy after its
    retries - is `error` with the station's text as the detail.
    """
    if outcome.result is not None:
        return ReadStatus.OK, None
    if outcome.error is None:
        return ReadStatus.SKIPPED, NOT_ATTEMPTED_DETAIL
    if isinstance(outcome.error, _NO_RESPONSE_ERRORS):
        return ReadStatus.NO_RESPONSE, str(outcome.error)
    if isinstance(outcome.error, _SKIP_ERRORS):
        return ReadStatus.SKIPPED, str(outcome.error)
    return ReadStatus.ERROR, str(outcome.error)


def record_for(outcome: CvReadOutcome, *, source: str = SOURCE_CATALOG) -> CvRecord:
    """The whole file row for one outcome: `status_for`'s verdict plus the
    value (only when there is one) and the name the spec carried. Built here
    so a row and its status cannot come from two different places."""
    status, detail = status_for(outcome)
    value = outcome.result.value if outcome.result is not None else None
    return CvRecord(
        cv=outcome.spec.cv,
        name=outcome.spec.name,
        status=status,
        source=source,
        value=value,
        detail=detail,
    )
