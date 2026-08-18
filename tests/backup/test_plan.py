# tests/backup/test_plan.py
"""`plan_restore`: one test per stage boundary, per action, and per refusal.

The catalog under test is the shipped one, not a fixture: `min`/`max` and
`restorable` are the data restore is steered by, and a hand-built catalog would
prove the planner agrees with the test author rather than with the file the
tool ships. The three CVs with a narrowed range (CV1 1..127, CV17 192..231,
CV56 0..99) are what the boundary tests are written against.

Every plan here is built from live values passed in as a mapping. Nothing in
this file opens a link, and `test_the_planner_never_names_the_facade_class`
is the guard that keeps the module that way.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from railctl.backup import PlannedWrite, never_write_cvs, plan_restore
from railctl.backup.plan import (
    ADDRESS_SET,
    CV29_LONG_ADDRESS_MASK,
    REASON_ADDRESS_WITHOUT_FLAG,
    REASON_CV29_DEFAULT,
    REASON_CV29_LIVE_UNREAD,
    REASON_CV29_MERGED,
    REASON_CV29_WHOLE,
    REASON_LIVE_UNREAD,
    REASON_NEVER_RESTORED,
    REASON_NOT_IN_CATALOG,
    REASON_UNCHANGED,
    STAGES,
)
from railctl.backup.types import CvRecord, ReadStatus
from railctl.catalog import load_catalog
from railctl.errors import AddressSetIncompleteError, CvOutOfRangeError
from railctl.station.capabilities import Capabilities

CATALOG = load_catalog()

#: Everything the station has been measured to do: nothing at all. The planner
#: must produce the same rows for this as for a station probed and found
#: wanting - see `test_capabilities_change_no_row`.
UNMEASURED = Capabilities.unknown("serial:test:3")

#: The set the design names, written out here and only here. The production
#: side derives it from the catalog; this is the other end of that pin.
DESIGN_NEVER_WRITE = frozenset({7, 8, 31, 32, 250, 251, 252, 253})

CV1, CV3, CV17, CV18, CV28, CV29, CV56, CV144 = 1, 3, 17, 18, 28, 29, 56, 144

#: A CV number no [[cv]] or [[range]] block covers, standing in for the sweep
#: source M11 adds. `include_sweep` has no way to reach a file before then.
SWEEP_CV = 900

#: CV29 = 0b0000_1110: RailCom on, 28/128 speed steps, bit 5 clear, so the
#: decoder answers on the short address in CV1.
CV29_SHORT = 0b0000_1110
#: The same byte with bit 5 set - the long address in CV17/CV18 selected.
CV29_LONG = CV29_SHORT | CV29_LONG_ADDRESS_MASK
#: A live byte differing from CV29_SHORT in bit 0 as well as in bit 5, so a
#: merge that copied the whole live byte would pass a test written against
#: bit 5 alone.
CV29_LIVE_LONG_OTHER = 0b0010_1101


def _record(
    cv: int,
    value: int | None = None,
    *,
    status: ReadStatus = ReadStatus.OK,
    detail: str | None = None,
) -> CvRecord:
    entry = CATALOG.get(cv)
    return CvRecord(
        cv=cv,
        name=entry.slug if entry is not None else f"sweep_cv{cv}",
        status=status,
        value=value,
        detail=detail,
    )


#: CV1, CV17, CV18 and CV29 all `ok` - the file `--with-address` demands.
def _address_records() -> tuple[CvRecord, ...]:
    return (
        _record(CV1, 3),
        _record(CV17, 192),
        _record(CV18, 10),
        _record(CV29, CV29_SHORT),
    )


def _plan(
    records: tuple[CvRecord, ...],
    live: dict[int, int | None] | None = None,
    *,
    with_address: bool = False,
    merge_cv29: bool = False,
    include_sweep: bool = False,
    caps: Capabilities = UNMEASURED,
) -> list[PlannedWrite]:
    return plan_restore(
        records,
        live if live is not None else {},
        CATALOG,
        caps,
        with_address=with_address,
        merge_cv29=merge_cv29,
        include_sweep=include_sweep,
    )


def _by_cv(rows: list[PlannedWrite]) -> dict[int, PlannedWrite]:
    return {row.num: row for row in rows}


# --- stages -----------------------------------------------------------------


def test_an_ordinary_cv_is_a_stage_a_write():
    rows = _plan((_record(CV3, 26),), {CV3: 20})

    assert (rows[0].stage, rows[0].action, rows[0].new_value) == ("A", "write", 26)


def test_cv28_and_cv29_are_stage_b_with_cv28_first():
    rows = _plan((_record(CV29, CV29_SHORT), _record(CV28, 3)), {CV28: 0, CV29: 0})

    assert [(row.num, row.stage) for row in rows] == [(CV28, "B"), (CV29, "B")]


def test_the_address_cvs_are_stage_c_in_the_order_17_18_1():
    """The literal order, not `STAGE_C_ORDER`: a test written against the
    constant it is meant to pin passes whatever the constant is changed to."""
    rows = _plan(_address_records(), {}, with_address=True)

    assert [row.num for row in rows if row.stage == "C"] == [CV17, CV18, CV1]


def test_cv144_is_stage_d_and_runs_last():
    rows = _plan((_record(CV144, 0), _record(CV3, 26), _record(CV28, 3)), {})

    assert (rows[-1].num, rows[-1].stage) == (CV144, "D")


def test_the_stages_run_a_then_b_then_c_then_d():
    records = (*_address_records(), _record(CV3, 26), _record(CV28, 3), _record(CV144, 0))

    rows = _plan(records, {}, with_address=True)

    stage_indexes = [STAGES.index(row.stage) for row in rows]
    assert stage_indexes == sorted(stage_indexes)


def test_cvs_run_ascending_within_a_stage():
    """Stage C is the documented exception and is asserted on its own above -
    every other stage runs in CV order, whatever order the file listed."""
    records = (_record(CV144, 0), _record(CV29, CV29_SHORT), _record(CV28, 3), _record(CV3, 26))

    rows = _plan(records, {}, merge_cv29=True)

    outside_c = [row.num for row in rows if row.stage != "C"]
    assert outside_c == sorted(outside_c)


def test_every_record_gets_a_row():
    """A skip that is invisible reads as a CV nobody considered."""
    records = (_record(CV3, 26), _record(8, 145), _record(CV29, CV29_SHORT))

    rows = _plan(records, {})

    assert len(rows) == len(records)


# --- CV29 -------------------------------------------------------------------


def test_cv29_is_skipped_by_default():
    rows = _plan((_record(CV29, CV29_SHORT),), {CV29: CV29_LONG})

    assert (rows[0].action, rows[0].new_value, rows[0].reason) == (
        "skip",
        None,
        REASON_CV29_DEFAULT,
    )


def test_merge_cv29_keeps_a_live_long_address_bit():
    """The live decoder is long-addressed and the file is not. The merged byte
    takes bits 0-4 from the file and bit 5 from the decoder."""
    rows = _plan((_record(CV29, CV29_SHORT),), {CV29: CV29_LIVE_LONG_OTHER}, merge_cv29=True)

    assert rows[0].new_value == CV29_LONG


def test_merge_cv29_drops_the_files_long_address_bit():
    """The reverse direction: a file carrying bit 5 must not turn a
    short-addressed decoder into a long-addressed one."""
    rows = _plan((_record(CV29, CV29_LONG),), {CV29: CV29_SHORT}, merge_cv29=True)

    assert rows[0].new_value == CV29_SHORT


def test_merge_cv29_reports_the_merge_as_the_reason():
    rows = _plan((_record(CV29, CV29_SHORT),), {CV29: CV29_LIVE_LONG_OTHER}, merge_cv29=True)

    assert rows[0].reason == REASON_CV29_MERGED


def test_a_merged_cv29_that_already_matches_is_unchanged():
    rows = _plan((_record(CV29, CV29_SHORT),), {CV29: CV29_LONG}, merge_cv29=True)

    assert rows[0].action == "unchanged"


def test_merge_cv29_is_skipped_when_the_live_byte_did_not_read():
    """Without the live bit there is nothing to preserve, and writing the
    file's byte whole is exactly the address move the merge exists to avoid."""
    rows = _plan((_record(CV29, CV29_SHORT),), {CV29: None}, merge_cv29=True)

    assert (rows[0].action, rows[0].reason) == ("skip", REASON_CV29_LIVE_UNREAD)


def test_with_address_writes_cv29_whole():
    records = (*_address_records()[:3], _record(CV29, CV29_LONG))

    rows = _by_cv(_plan(records, {CV29: CV29_SHORT}, with_address=True))

    assert (rows[CV29].new_value, rows[CV29].reason) == (CV29_LONG, REASON_CV29_WHOLE)


def test_with_address_and_merge_cv29_contradict_each_other():
    with pytest.raises(ValueError, match="contradict"):
        _plan(_address_records(), {}, with_address=True, merge_cv29=True)


# --- the address set --------------------------------------------------------


def test_the_address_cvs_are_skipped_without_the_flag():
    rows = _by_cv(_plan(_address_records(), {}))

    assert [rows[num].reason for num in (CV17, CV18, CV1)] == [REASON_ADDRESS_WITHOUT_FLAG] * 3


def test_with_address_refuses_a_file_missing_an_address_cv():
    records = tuple(row for row in _address_records() if row.cv != CV17)

    with pytest.raises(AddressSetIncompleteError) as caught:
        _plan(records, {}, with_address=True)

    assert caught.value.details["missing"] == [CV17]


def test_with_address_refuses_an_address_cv_that_did_not_read():
    records = (
        _record(CV1, 3),
        _record(CV17, 192),
        _record(CV18, status=ReadStatus.NO_RESPONSE, detail="no answer after 3 attempts"),
        _record(CV29, CV29_SHORT),
    )

    with pytest.raises(AddressSetIncompleteError) as caught:
        _plan(records, {}, with_address=True)

    assert caught.value.details["not_ok"] == [CV18]


def test_the_address_set_is_the_four_cvs_that_decide_an_address():
    assert ADDRESS_SET == {CV1, CV17, CV18, CV29}


# --- the never-write set ----------------------------------------------------


def test_the_never_write_set_is_exactly_the_catalogs_unrestorable_entries():
    """The design names eight CVs; the catalog marks eight `restorable =
    false`. This is the assertion that keeps them the same eight."""
    assert never_write_cvs(CATALOG) == DESIGN_NEVER_WRITE


def test_a_never_written_cv_is_skipped_even_when_it_differs():
    rows = _plan((_record(8, 145),), {8: 1})

    assert (rows[0].action, rows[0].reason) == ("skip", REASON_NEVER_RESTORED)


def test_a_never_written_cv_that_did_not_read_is_still_reported_as_never_written():
    """Precedence, not an accident: `unreadable` here would imply that a
    successful read would have restored CV250, and nothing ever restores it."""
    rows = _plan((_record(250, status=ReadStatus.NO_RESPONSE, detail="silence"),), {})

    assert (rows[0].action, rows[0].reason) == ("skip", REASON_NEVER_RESTORED)


# --- the remaining actions --------------------------------------------------


def test_a_value_the_decoder_already_holds_is_unchanged():
    rows = _plan((_record(CV3, 26),), {CV3: 26})

    assert (rows[0].action, rows[0].reason, rows[0].new_value) == (
        "unchanged",
        REASON_UNCHANGED,
        26,
    )


def test_a_record_that_is_not_ok_is_unreadable_and_writes_nothing():
    rows = _plan((_record(CV3, status=ReadStatus.ERROR, detail="short circuit"),), {CV3: 20})

    assert (rows[0].action, rows[0].new_value) == ("unreadable", None)


def test_an_unreadable_row_names_the_status_the_file_recorded():
    rows = _plan((_record(CV3, status=ReadStatus.NO_RESPONSE, detail="no answer"),), {})

    assert "no_response" in rows[0].reason


def test_a_live_value_that_did_not_read_is_written_rather_than_assumed():
    """Absent and `None` are the same thing to the planner: no value to compare
    against is not a reason to invent one, and the read-back verifies it."""
    rows = _plan((_record(CV3, 26),), {})

    assert (rows[0].action, rows[0].reason, rows[0].new_value) == ("write", REASON_LIVE_UNREAD, 26)


# --- the sweep branch (M11) -------------------------------------------------


def test_an_uncurated_cv_is_skipped_without_include_sweep():
    rows = _plan((_record(SWEEP_CV, 5),), {})

    assert (rows[0].action, rows[0].reason) == ("skip", REASON_NOT_IN_CATALOG)


def test_include_sweep_plans_an_uncurated_cv_as_an_ordinary_write():
    rows = _plan((_record(SWEEP_CV, 5),), {SWEEP_CV: 4}, include_sweep=True)

    assert (rows[0].stage, rows[0].action, rows[0].new_value) == ("A", "write", 5)


# --- the catalog's range, enforced before any write -------------------------


def test_a_value_on_the_catalog_maximum_is_planned():
    top = CATALOG[CV56].max  # CV56 (reg_pi) takes 0..99

    rows = _plan((_record(CV56, top),), {CV56: 0})

    assert rows[0].new_value == top


def test_a_value_one_past_the_catalog_maximum_refuses_the_whole_run():
    over = CATALOG[CV56].max + 1

    with pytest.raises(CvOutOfRangeError) as caught:
        _plan((_record(CV56, over),), {CV56: 0})

    assert caught.value.details["out_of_range"] == [
        {"cv": CV56, "value": over, "min": CATALOG[CV56].min, "max": CATALOG[CV56].max}
    ]


def test_a_value_on_the_catalog_minimum_is_planned():
    bottom = CATALOG[CV17].min  # CV17 (ext_address_high) takes 192..231
    records = (_record(CV1, 3), _record(CV17, bottom), _record(CV18, 10), _record(CV29, CV29_SHORT))

    rows = _by_cv(_plan(records, {CV17: 200}, with_address=True))

    assert rows[CV17].new_value == bottom


def test_a_value_one_below_the_catalog_minimum_refuses_the_whole_run():
    under = CATALOG[CV17].min - 1
    records = (_record(CV1, 3), _record(CV17, under), _record(CV18, 10), _record(CV29, CV29_SHORT))

    with pytest.raises(CvOutOfRangeError) as caught:
        _plan(records, {CV17: 200}, with_address=True)

    assert [item["cv"] for item in caught.value.details["out_of_range"]] == [CV17]


def test_every_out_of_range_value_is_listed_in_one_refusal():
    """One failed run per bad value is how an operator gives up halfway
    through with the decoder part-written."""
    records = (_record(CV3, 300), _record(CV56, 100))

    with pytest.raises(CvOutOfRangeError) as caught:
        _plan(records, {CV3: 0, CV56: 0})

    assert [item["cv"] for item in caught.value.details["out_of_range"]] == [CV3, CV56]


def test_an_out_of_range_value_the_decoder_already_holds_does_not_refuse():
    """The bound is enforcing on WRITE. Nothing is written here, so refusing
    would block a restore over a bound the catalog has wrong, for no gain."""
    rows = _plan((_record(CV56, 100),), {CV56: 100})

    assert rows[0].action == "unchanged"


# --- capabilities and purity ------------------------------------------------


def test_capabilities_change_no_row():
    """M10 plans nothing off a capability. A station measured to support
    nothing must produce the same plan as one nobody has probed - the day that
    changes, it changes for a `False`, never for a `None`."""
    measured_absent = dataclasses.replace(
        UNMEASURED,
        pom_read=False,
        service_direct_cv=False,
        service_ext_cv=False,
        z21_cv_opcodes=False,
    )
    records = (_record(CV3, 26), _record(CV28, 3), _record(CV144, 0))

    assert _plan(records, {CV3: 20}, caps=measured_absent) == _plan(records, {CV3: 20})


def test_the_planner_never_names_the_facade_class():
    """The dry run and the real run share one plan only while this module
    cannot read or write anything itself. A text scan, like the layering
    guards: it also covers code no test exercises."""
    source = Path(plan_restore.__globals__["__file__"]).read_text(encoding="utf-8")

    assert source, "the guard read an empty file; the module moved"
    assert re.findall(r"\bStation\b", source) == []
