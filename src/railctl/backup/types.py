# src/railctl/backup/types.py
"""The `railctl/backup/v1` vocabulary: `ReadStatus`, `CvRecord`, `BackupDocument`.

This module mirrors the file format and depends on the standard library alone,
so the reader in `file.py` - which M10's restore consumes - never needs a
station attached. The one place the station's outcome vocabulary becomes a
file status is `mapping.py`, the only module in this package that names
station types.

The three-valued rule, as it applies to a file: a hole is never a number.
`value` exists exactly when `status` is `"ok"` - never `null`, never 0 for
silence - and `CvRecord` enforces that at construction, so an inconsistent
row cannot even be built, let alone written. `summary` is computed from the
rows rather than stored, so the counts and `complete` cannot disagree with
what the file actually says per CV.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

BACKUP_SCHEMA: Final[str] = "railctl/backup/v1"

#: `source` for a CV the curated catalog names.
SOURCE_CATALOG: Final[str] = "catalog"

#: `source` for a CV no catalog entry names - only an `--all` sweep produces
#: these rows, and `restore` refuses to write them without `--include-sweep`.
SOURCE_SWEEP: Final[str] = "sweep"


class ReadStatus(enum.StrEnum):
    """One CV row's outcome in the file.

    There is deliberately no "does not exist" member: neither programming
    path has a "no such CV" reply, and a missing acknowledgement may equally
    be an unimplemented CV or a decoder that failed to draw enough current
    for the ACK pulse. Recording that guess in a file that later drives
    writes is how a decoder gets corrupted.
    """

    OK = "ok"
    NO_RESPONSE = "no_response"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CvRecord:
    """One row of the file's `cvs` array.

    `detail` carries the station's own text for a non-`ok` row (or the
    writer-side reason for a deliberate skip); it is never invented for an
    `ok` row by the mapping, though nothing forbids one.

    `attempts` is stream-side metadata: how many reads the station tried
    before giving up, when the error recorded it. The ndjson `cv` line
    emits it; the file writer emits its explicit keys only, so it never
    reaches a file row.
    """

    cv: int
    name: str
    status: ReadStatus
    source: str = SOURCE_CATALOG
    value: int | None = None
    detail: str | None = None
    attempts: int | None = None

    def __post_init__(self) -> None:
        if self.status is ReadStatus.OK and self.value is None:
            raise ValueError(f'CV {self.cv}: status "ok" requires a value')
        if self.status is not ReadStatus.OK and self.value is not None:
            raise ValueError(
                f"CV {self.cv}: status {self.status.value!r} must carry no value - "
                f"a hole is never a number"
            )


@dataclass(frozen=True, slots=True)
class Caveat:
    """One statement the document makes about what its own rows can prove.

    The split is the error envelope's: `code` is the machine-stable token a
    script branches on and may never be renamed or repurposed within a major
    version, `message` is prose and is free to be reworded. A caveat is not a
    warning about the run - it is a property of the file, and it is written
    into the file so the reader who asks the question months later gets the
    answer without the run that produced it.
    """

    code: str
    message: str


#: The one caveat this tool writes today. A CV row that answered `0` is
#: recorded exactly like a CV that genuinely holds zero, because on this
#: hardware the two are indistinguishable - docs/probe-results.md, "What is
#: still open", is explicit that no read can settle it and that it applies at
#: every CV number rather than in any particular range.
CAVEAT_ZERO_IS_NOT_PROOF: Final[str] = "zero_is_not_proof"

#: What a swept document carries, whatever its rows turned out to hold. Keyed
#: off the set rather than off the values: a key whose presence depended on
#: whether the decoder happened to answer zero somewhere would make the shape
#: of the file depend on the decoder's mood, and a consumer could not tell a
#: sweep with no zeroes from a sweep written by an older tool.
SWEEP_CAVEATS: Final[tuple[Caveat, ...]] = (
    Caveat(
        code=CAVEAT_ZERO_IS_NOT_PROOF,
        message=(
            "any row that answered 0 may be an implemented CV holding zero or a CV the decoder "
            "does not implement answering zero; this hardware cannot tell the two apart at "
            "any CV number, and no read can settle it"
        ),
    ),
)


#: `summary`'s fixed key order. The reader compares a file's stored summary
#: against the recomputed one key by key, tolerating extra keys, because
#: within a major version optional fields may be added but these six may not
#: drift from the rows.
SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "requested",
    "ok",
    "no_response",
    "error",
    "skipped",
    "complete",
)


@dataclass(frozen=True, slots=True)
class BackupDocument:
    """The `railctl/backup/v1` document, one field per top-level key in the
    file's fixed order.

    `schema` is not a field: it can only ever hold `BACKUP_SCHEMA`, and a
    field would let a caller construct a document that lies about it.
    `set_name` serializes under the key `"set"`, which shadows a builtin
    here. `interrupted` and `caveats` are the keys outside the fixed
    order: `interrupted` is absent from a file a run wrote to the end and
    `true` in the partial file a Ctrl-C leaves behind, and `caveats` is
    absent unless the document has something to say about what its rows can
    prove.
    """

    created_utc: str
    tool: str
    note: str | None
    loco: dict[str, object]
    catalog: dict[str, object]
    set_name: str
    mode: str
    cv_encoding: str | None
    page: tuple[int, int]
    speed_table_included: bool
    sweep_range: tuple[int, int] | None
    link: dict[str, object]
    capabilities: dict[str, object]
    decoder: dict[str, object]
    cvs: tuple[CvRecord, ...]
    interrupted: bool = False
    caveats: tuple[Caveat, ...] = ()

    @property
    def summary(self) -> dict[str, object]:
        """Counts derived from the rows. `complete` is `no_response == 0 and
        error == 0 and not interrupted` - a `skipped` row is a recorded
        decision, not a hole, so skips never make a file incomplete; an
        interrupted run is not complete by definition, whatever its rows say.
        Both the writer (which stores this) and the reader (which recomputes
        and compares it) go through this one property, so the stored
        `complete` of an interrupted file is false on both sides."""
        counts = dict.fromkeys(ReadStatus, 0)
        for record in self.cvs:
            counts[record.status] += 1
        return {
            "requested": len(self.cvs),
            "ok": counts[ReadStatus.OK],
            "no_response": counts[ReadStatus.NO_RESPONSE],
            "error": counts[ReadStatus.ERROR],
            "skipped": counts[ReadStatus.SKIPPED],
            "complete": (
                counts[ReadStatus.NO_RESPONSE] == 0
                and counts[ReadStatus.ERROR] == 0
                and not self.interrupted
            ),
        }
