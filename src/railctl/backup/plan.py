# src/railctl/backup/plan.py
"""`plan_restore`: what a restore writes, in what order, and why it skips the rest.

The dry-run table and the real execution order come from this ONE function, so
they cannot drift: `restore --dry-run` prints exactly the list the executor
then consumes, and `diff` compares a file against a decoder with the same
comparator rather than a second one written to agree with it.

Pure by construction. It takes rows already read from a file, live values
already read from the decoder, the catalog and the capabilities the station has
measured, and returns `list[PlannedWrite]`. It opens no link, performs no I/O,
and takes no station object - the only station names it uses are two constants
and the capabilities dataclass, the same way `mapping.py` names station result
types. `tests/backup/test_plan.py` greps this file for the facade class name so
the property stays true as the module grows.

Four stages, each fully written and then verified by the executor before the
next begins:

* **A** - every ordinary CV, ascending: the catalog names it, the catalog marks
  it restorable, and it is not CV28, CV29, CV144 or an address CV.
* **B** - CV28, then CV29. CV29 is skipped unless a flag asks for it, because
  its bit 5 selects between the short and the long address.
* **C** - the address CVs, only with `with_address`, in the order CV17, CV18,
  CV1 - the long address first, then the byte that selects a short one.
* **D** - CV144, last of all. On the older ZIMO MX family CV144 is the
  programming/update lock, so a lock written earlier in the run would block
  every later write, including the read-backs verification depends on. On the
  MS family it is only the confirmation jingle (see `station/types.py`), so
  writing it last is merely harmless there. One order is correct for both
  families, so there is one order and no family branch.

Restore is programming-track only (M10 decision D1). Service mode addresses the
TRACK and not a locomotive, so nothing here re-targets the station after the
address CVs are written, and there is no read-CV8-at-the-new-address
diagnostic: both are POM concerns, and a POM restore does not exist - on this
station a POM CV read is measured silent (`docs/probe-results.md`, R1), so it
could be neither identity-gated nor verified.

There is deliberately no RailCom check in stage B. Turning RailCom off matters
on the main track, where it is the only read path; on the programming track the
decoder answers with a current pulse, and CV28/CV29 change nothing about that.
If a future POM restore ever needs the check, it is a test on BITS in both
directions - RailCom is live when CV28 bits 0 and 1 are set AND CV29 bit 3 is
set - and never a comparison against a whole-byte literal: the reference
decoder reads CV28 = 3 and CV29 = 14 (MEASURED 2026-08-06, doctor D8/D9,
`docs/probe-results.md`), while the CV28 = 67 that circulates in ZIMO material
belongs to large-scale decoders and would read as "RailCom off" against a
literal 3.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from railctl.backup.types import CvRecord, ReadStatus
from railctl.catalog import CatalogEntry
from railctl.errors import (
    REASON_VALUE_OUT_OF_RANGE,
    AddressSetIncompleteError,
    CvOutOfRangeError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import CV29_LONG_ADDRESS_BIT, CV144

Stage = Literal["A", "B", "C", "D"]
Action = Literal["write", "unchanged", "skip", "unreadable"]

#: The stages in execution order. The list `plan_restore` returns is sorted by
#: this, so a caller never re-derives the order from the stage letters.
STAGES: Final[tuple[Stage, ...]] = ("A", "B", "C", "D")

CV28: Final[int] = 28
CV29: Final[int] = 29

#: Stage C's order is fixed and is NOT ascending: the long address (CV17, CV18)
#: is in place before CV1, so that a run interrupted between the two leaves the
#: decoder still answering where CV29 bit 5 says it should.
STAGE_C_ORDER: Final[tuple[int, ...]] = (17, 18, 1)

#: The four CVs that decide an address together, and therefore the four a
#: `with_address` run must find `ok` in the file before it writes any of them.
ADDRESS_SET: Final[frozenset[int]] = frozenset({*STAGE_C_ORDER, CV29})

#: CV29 bit 5 as a mask. The bit number itself lives in `station/types.py`; a
#: second literal here is how the two would eventually disagree.
CV29_LONG_ADDRESS_MASK: Final[int] = 1 << CV29_LONG_ADDRESS_BIT

REASON_WRITE: Final[str] = "the decoder holds a different value from the file"
REASON_UNCHANGED: Final[str] = "the decoder already holds the file's value"
REASON_LIVE_UNREAD: Final[str] = (
    "the live value did not read back, so this write cannot be ruled out as unnecessary"
)
REASON_NOT_IN_CATALOG: Final[str] = (
    "the curated catalog does not name this CV, and no sweep source exists yet"
)
REASON_NEVER_RESTORED: Final[str] = (
    "the catalog marks this CV as never restored - it is read-only identity data or an "
    "index page selector, and writing it would change what the decoder claims to be"
)
REASON_ADDRESS_WITHOUT_FLAG: Final[str] = (
    "an address CV is written only with --with-address, which writes CV17, CV18 and CV1 together"
)
REASON_CV29_DEFAULT: Final[str] = (
    "CV29 is skipped by default because bit 5 selects the locomotive's address: "
    "--merge-cv29 writes the file's byte with the LIVE long-address bit preserved, and "
    "--with-address writes the file's byte whole"
)
REASON_CV29_MERGED: Final[str] = (
    "--merge-cv29: the file's byte with the live long-address bit preserved"
)
REASON_CV29_WHOLE: Final[str] = "--with-address: the file's CV29 byte, long-address bit included"
REASON_CV29_LIVE_UNREAD: Final[str] = (
    "--merge-cv29 needs the live CV29 to preserve its long-address bit and it did not read "
    "back; writing the file's byte whole could move the locomotive to another address"
)


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    """One row of the plan: one CV, its stage, and what is to become of it.

    Every row of the file appears here, whatever its `action` - the plan is
    also the dry-run table and the diff, and a skip that is invisible reads as
    a CV nobody considered. The executor consumes exactly the `action ==
    "write"` rows, in list order.

    `new_value` is the INTENDED value, which is not always the file's: for a
    merged CV29 it is the masked byte. Verification compares against this
    field, never against `file_value`, or a merge would report as a mismatch
    against the file it deliberately did not copy. It is `None` exactly when
    nothing is to be written.
    """

    num: int
    name: str
    stage: Stage
    file_value: int | None
    live_value: int | None
    new_value: int | None
    action: Action
    reason: str


class _RowFactory(Protocol):
    """One CV's row, with `num`, `name`, `stage`, `file_value` and `live_value`
    already bound. Passing this closure around rather than those five values
    is what stops a helper building a row for a different CV than the one it
    was asked about."""

    def __call__(
        self, action: Action, reason: str, new_value: int | None = None
    ) -> PlannedWrite: ...


def never_write_cvs(catalog: Mapping[int, CatalogEntry]) -> frozenset[int]:
    """The CVs a restore never writes, read off the catalog's `restorable`.

    Derived and never listed here. The set is small and stable enough to write
    out (CV7, CV8, CV31, CV32 and CV250-253 today), and that is exactly why a
    literal copy is dangerous: it would keep passing every test while the
    catalog gained a ninth read-only CV that a restore then happily wrote.
    `tests/backup/test_plan.py` pins the shipped catalog against the set the
    design names, which is where a disagreement between the two belongs.
    """
    return frozenset(num for num, entry in catalog.items() if not entry.restorable)


def plan_restore(
    records: Iterable[CvRecord],
    live: Mapping[int, int | None],
    catalog: Mapping[int, CatalogEntry],
    caps: Capabilities,
    *,
    with_address: bool,
    merge_cv29: bool,
    include_sweep: bool,
) -> list[PlannedWrite]:
    """The whole plan for one restore, in execution order.

    `records` are the file's rows; `live` maps a CV number to the value read
    from the decoder just now, where `None` - or an absent key - means the live
    value is not known. The two are not distinguished on purpose: "never
    attempted" and "no answer" both leave the planner without a value to
    compare against, and neither is a reason to invent one.

    `caps` changes no row in M10, and the parameter is here rather than in the
    executor for a reason: the only capability-dependent skip anyone has
    proposed - a station MEASURED to have no extended-CV path cannot reach
    CV257 and up - is a decision about the plan, and it belongs in the function
    that owns the plan. It stays unbuilt because a capability is three-valued:
    only `False` could ever justify skipping, `None` means nobody has measured
    it, and pre-skipping on `None` would record a capability as absent that no
    instrument reported - the one failure this project exists to prevent. Until
    then the station refuses an unreachable CV at execution time with its own
    measured error, and `tests/backup/test_plan.py` pins the plan against a
    capabilities object with everything switched off.

    Raises `AddressSetIncompleteError` (exit 9) when `with_address` cannot be
    honoured in full, and `CvOutOfRangeError` (exit 15) listing every value the
    catalog refuses. Both fire before the caller performs any write.
    """
    if with_address and merge_cv29:
        # A usage error the CLI refuses first, with exit 2 and a suggestion.
        # This is the guard for every other caller: the two flags ask for
        # opposite things with CV29 bit 5, and silently letting one win would
        # move a locomotive's address on the strength of an argument order.
        raise ValueError(
            "with_address and merge_cv29 contradict each other: --with-address writes CV29 "
            "whole, --merge-cv29 keeps the decoder's own long-address bit"
        )

    rows = {record.cv: record for record in records}
    if with_address:
        _require_whole_address_set(rows)

    never_write = never_write_cvs(catalog)
    planned = [
        _plan_one(
            record,
            live_value=live.get(record.cv),
            entry=catalog.get(record.cv),
            never_write=never_write,
            with_address=with_address,
            merge_cv29=merge_cv29,
            include_sweep=include_sweep,
        )
        for record in rows.values()
    ]
    planned.sort(key=_execution_key)
    _refuse_out_of_range(planned, catalog)
    return planned


def _execution_key(row: PlannedWrite) -> tuple[int, int]:
    """Stage first, then ascending CV - except in stage C, whose three CVs run
    in `STAGE_C_ORDER`. Skipped and unreadable rows are sorted with the rest so
    the dry-run table reads in the order the executor will work through."""
    stage_index = STAGES.index(row.stage)
    if row.stage == "C":
        return stage_index, STAGE_C_ORDER.index(row.num)
    return stage_index, row.num


def _stage_of(num: int) -> Stage:
    if num in STAGE_C_ORDER:
        return "C"
    if num in (CV28, CV29):
        return "B"
    if num == CV144:
        return "D"
    return "A"


def _unreadable_reason(record: CvRecord) -> str:
    detail = f" ({record.detail})" if record.detail else ""
    return (
        f'the file records this CV as "{record.status.value}"{detail}, so there is no value '
        f"to write - a hole is never a number"
    )


def _require_whole_address_set(rows: Mapping[int, CvRecord]) -> None:
    """`with_address` needs all four address CVs `ok` in the file, or nothing.

    A partial address set is the one restore failure with no cheap recovery:
    the decoder ends up answering at an address that is neither the file's nor
    the label's, and the only way back is a sweep. Checked before the plan is
    built, so no stage runs at all.
    """
    missing = sorted(num for num in ADDRESS_SET if num not in rows)
    unreadable = sorted(
        num
        for num, record in rows.items()
        if num in ADDRESS_SET and record.status is not ReadStatus.OK
    )
    if not missing and not unreadable:
        return
    faults = []
    if missing:
        faults.append("missing from the file: " + ", ".join(f"CV{num}" for num in missing))
    if unreadable:
        faults.append(
            "not read ok: " + ", ".join(f"CV{num} ({rows[num].status.value})" for num in unreadable)
        )
    raise AddressSetIncompleteError(
        "--with-address needs CV1, CV17, CV18 and CV29 all read ok in the file, and "
        + "; ".join(faults)
        + ". Writing part of an address set leaves a locomotive answering at an address "
        "that is in no file",
        hint="back the decoder up again, or restore without --with-address",
        details={"missing": missing, "not_ok": unreadable},
    )


def _plan_one(
    record: CvRecord,
    *,
    live_value: int | None,
    entry: CatalogEntry | None,
    never_write: frozenset[int],
    with_address: bool,
    merge_cv29: bool,
    include_sweep: bool,
) -> PlannedWrite:
    """One row's verdict.

    The order of the checks below is the order of the REASONS, and it is
    deliberate: a policy that holds for every file ("this CV is never
    restored") is reported ahead of a fact about this one ("this row did not
    read"). A read-only CV250 that happens to be `no_response` reported as
    `unreadable` would imply that a successful read would have restored it,
    which is false.
    """
    num = record.cv
    stage = _stage_of(num)
    name = record.name or (entry.slug if entry is not None else "")

    def row(action: Action, reason: str, new_value: int | None = None) -> PlannedWrite:
        return PlannedWrite(
            num=num,
            name=name,
            stage=stage,
            file_value=record.value,
            live_value=live_value,
            new_value=new_value,
            action=action,
            reason=reason,
        )

    if entry is None and not include_sweep:
        # M10 has no sweep sources, so this is every uncurated CV. The branch
        # exists for M11's `--all`, which is the first thing that can put one
        # in a file.
        return row("skip", REASON_NOT_IN_CATALOG)
    if num in never_write:
        return row("skip", REASON_NEVER_RESTORED)
    if record.status is not ReadStatus.OK:
        return row("unreadable", _unreadable_reason(record))
    if stage == "C" and not with_address:
        return row("skip", REASON_ADDRESS_WITHOUT_FLAG)
    if num == CV29:
        return _plan_cv29(row, record.value, live_value, with_address, merge_cv29)
    # `status is OK` guarantees a value - `CvRecord` refuses to be built otherwise.
    return _compare(row, record.value, live_value, REASON_WRITE)


def _plan_cv29(
    row: _RowFactory,
    file_value: int | None,
    live_value: int | None,
    with_address: bool,
    merge_cv29: bool,
) -> PlannedWrite:
    if with_address:
        return _compare(row, file_value, live_value, REASON_CV29_WHOLE)
    if not merge_cv29:
        return row("skip", REASON_CV29_DEFAULT)
    if live_value is None:
        return row("skip", REASON_CV29_LIVE_UNREAD)
    # The masked write: every bit from the file except bit 5, which stays as
    # the decoder has it. A file carrying the bit set must not be able to turn
    # a short-addressed locomotive into a long-addressed one, and a file
    # carrying it clear must not be able to do the reverse.
    merged = (file_value & ~CV29_LONG_ADDRESS_MASK) | (live_value & CV29_LONG_ADDRESS_MASK)
    return _compare(row, merged, live_value, REASON_CV29_MERGED)


def _compare(
    row: _RowFactory, intended: int | None, live_value: int | None, write_reason: str
) -> PlannedWrite:
    if live_value is None:
        return row("write", REASON_LIVE_UNREAD, intended)
    if live_value == intended:
        return row("unchanged", REASON_UNCHANGED, intended)
    return row("write", write_reason, intended)


def _refuse_out_of_range(
    planned: Iterable[PlannedWrite], catalog: Mapping[int, CatalogEntry]
) -> None:
    """Every intended value against the catalog's bounds, before the first write.

    The whole list in one exception rather than one refusal per CV: a file
    written for another decoder produces dozens, and finding them one failed
    run at a time is how an operator gives up halfway through with the decoder
    part-written. A sweep row has no curated bound to check; the file reader
    has already held its value to 0..255.
    """
    offenders: list[dict[str, object]] = []
    for row in planned:
        entry = catalog.get(row.num)
        # `new_value` is never None on a write row; the test narrows the type.
        if row.action != "write" or entry is None or row.new_value is None:
            continue
        if not entry.min <= row.new_value <= entry.max:
            offenders.append(
                {"cv": row.num, "value": row.new_value, "min": entry.min, "max": entry.max}
            )
    if not offenders:
        return
    listed = ", ".join(
        f"CV{item['cv']} takes {item['min']}..{item['max']}, got {item['value']}"
        for item in offenders
    )
    raise CvOutOfRangeError(
        f"{len(offenders)} value(s) in this file fall outside the catalog's range: {listed}; "
        f"refused before any write - the catalog is enforcing on write",
        details={"reason": REASON_VALUE_OUT_OF_RANGE, "out_of_range": offenders},
    )
