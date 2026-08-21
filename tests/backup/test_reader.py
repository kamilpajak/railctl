# tests/backup/test_reader.py
"""`read_backup`: the round trip, the tolerances, and every rejection.

Each rejection fixture is the writer's own valid output plus exactly one
mutation, so a test can only pass by the reader catching that mutation -
never because the fixture was broken in some second, accidental way.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from railctl.backup import (
    SWEEP_CAVEATS,
    Caveat,
    CvRecord,
    ReadStatus,
    read_backup,
    write_backup,
    write_backup_to,
)
from railctl.errors import BackupFileError
from tests.backup.documents import make_document

Mutation = Callable[[dict], None]


def _write_mutated(tmp_path: Path, mutate: Mutation) -> Path:
    parsed = json.loads(write_backup(make_document()))
    mutate(parsed)
    target = tmp_path / "backup.json"
    target.write_text(json.dumps(parsed), encoding="utf-8")
    return target


def _rejection(tmp_path: Path, mutate: Mutation) -> str:
    with pytest.raises(BackupFileError) as caught:
        read_backup(_write_mutated(tmp_path, mutate))
    return str(caught.value)


# -- the happy path and the tolerances ---------------------------------------


def test_a_written_file_reads_back_as_an_equal_document(tmp_path):
    target = tmp_path / "loco-0003-curated.json"
    write_backup_to(make_document(), target)
    assert read_backup(target) == make_document()


def test_an_interrupted_file_round_trips_with_the_flag_set(tmp_path):
    target = tmp_path / "partial.json"
    write_backup_to(make_document(interrupted=True), target)
    assert read_backup(target).interrupted is True


def test_an_interrupted_all_ok_file_round_trips_with_complete_false(tmp_path):
    # Writer and reader share the rule through `BackupDocument.summary`: the
    # stored summary of an interrupted file says `complete: false` even with
    # every row ok, and the reader's recomputation agrees rather than
    # rejecting its own writer's output.
    records = (CvRecord(cv=1, name="primary_address", status=ReadStatus.OK, value=3),)
    target = tmp_path / "interrupted.json"
    write_backup_to(make_document(cvs=records, interrupted=True), target)
    document = read_backup(target)
    assert document.interrupted is True
    assert document.summary["complete"] is False


def test_non_ascending_row_order_is_tolerated_and_kept(tmp_path):
    def reverse_rows(parsed: dict) -> None:
        parsed["cvs"] = list(reversed(parsed["cvs"]))

    document = read_backup(_write_mutated(tmp_path, reverse_rows))
    assert [record.cv for record in document.cvs] == [397, 253, 1]


def test_an_unknown_top_level_key_is_tolerated(tmp_path):
    # Within a major version optional fields may be added; a reader that
    # rejected them would break on every forward-compatible v1 addition.
    def add_key(parsed: dict) -> None:
        parsed["added_in_a_later_v1"] = "tolerated"

    assert read_backup(_write_mutated(tmp_path, add_key)) == make_document()


#: A `railctl/backup/v1` sweep exactly as the writer produced it BEFORE
#: `caveats` existed - typed out rather than generated, because a fixture the
#: current writer produces with the field cleared would follow the writer
#: wherever it goes and stop being evidence about old files.
PRE_CAVEAT_SWEEP = """\
{
  "schema": "railctl/backup/v1",
  "created_utc": "2026-08-19T11:04:07Z",
  "tool": "railctl 0.1.0",
  "note": null,
  "loco": {
    "address": 3,
    "kind": "short"
  },
  "catalog": {
    "family": "zimo-ms-mx",
    "schema": 1
  },
  "set": "all",
  "mode": "service",
  "cv_encoding": "SERVICE_DIRECT",
  "page": [
    0,
    0
  ],
  "speed_table_included": true,
  "sweep_range": [
    1,
    255
  ],
  "link": {
    "identity": "serial:7010A0001194:3",
    "protocol": "xpressnet",
    "protocol_version": "4.0",
    "command_station_id": 18
  },
  "capabilities": {
    "pom_read": false,
    "pom_result_channel": null,
    "pom_echo_zero_based": null,
    "service_direct_cv": true,
    "service_ext_cv": null,
    "z21_cv_opcodes": null
  },
  "decoder": {
    "manufacturer_id": 145
  },
  "summary": {
    "requested": 1,
    "ok": 1,
    "no_response": 0,
    "error": 0,
    "skipped": 0,
    "complete": true
  },
  "cvs": [
    {
      "cv": 1,
      "name": "primary_address",
      "status": "ok",
      "value": 3,
      "source": "catalog"
    }
  ]
}
"""


def test_a_sweep_written_before_caveats_existed_still_loads(tmp_path):
    # The compatibility claim the whole addition rests on: `caveats` is not in
    # `TOP_LEVEL_KEYS`, which is only consulted for the missing-key check, so a
    # file from before the key existed is not missing anything.
    target = tmp_path / "loco-0003-all.json"
    target.write_text(PRE_CAVEAT_SWEEP, encoding="utf-8", newline="")
    document = read_backup(target)
    assert document.set_name == "all"
    assert document.caveats == ()


def test_a_document_with_caveats_round_trips(tmp_path):
    target = tmp_path / "swept.json"
    write_backup_to(make_document(set_name="all", caveats=SWEEP_CAVEATS), target)
    assert read_backup(target) == make_document(set_name="all", caveats=SWEEP_CAVEATS)


def test_a_caveat_the_reader_has_never_heard_of_loads_as_written(tmp_path):
    # `code` is the token a script branches on, and this reader is not the
    # place that decides which tokens exist: a v1 file may carry a caveat
    # added after this reader was written.
    def add_caveat(parsed: dict) -> None:
        parsed["caveats"] = [{"code": "added_in_a_later_v1", "message": "something new"}]

    document = read_backup(_write_mutated(tmp_path, add_caveat))
    assert document.caveats == (Caveat(code="added_in_a_later_v1", message="something new"),)


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "zero_is_not_proof"},
        {"message": "no code"},
        {"code": 7, "message": "a number is not a token"},
        {"code": "zero_is_not_proof", "message": None},
        "a bare string",
    ],
)
def test_a_malformed_caveat_entry_is_rejected(tmp_path, bad: object):
    def add_caveat(parsed: dict) -> None:
        parsed["caveats"] = [bad]

    assert "caveats[0]" in _rejection(tmp_path, add_caveat)


def test_boundary_cv_numbers_and_values_are_accepted(tmp_path):
    # Both ends ON the boundary: CV 1 with value 0, CV 1024 with value 255.
    records = (
        CvRecord(cv=1, name="low", status=ReadStatus.OK, value=0),
        CvRecord(cv=1024, name="high", status=ReadStatus.OK, value=255),
    )
    target = tmp_path / "bounds.json"
    write_backup_to(make_document(cvs=records), target)
    assert [record.value for record in read_backup(target).cvs] == [0, 255]


# -- rejections: the file itself ---------------------------------------------


def test_a_missing_file_is_a_backup_file_error(tmp_path):
    with pytest.raises(BackupFileError, match="cannot read backup file"):
        read_backup(tmp_path / "absent.json")


def test_bytes_that_are_not_utf8_are_a_backup_file_error(tmp_path):
    target = tmp_path / "mojibake.json"
    target.write_bytes(b'{"schema": "\xff"}')
    with pytest.raises(BackupFileError, match="cannot read backup file"):
        read_backup(target)


def test_text_that_is_not_json_is_a_backup_file_error(tmp_path):
    target = tmp_path / "broken.json"
    target.write_text('{"schema": "railctl/backup/v1",', encoding="utf-8")
    with pytest.raises(BackupFileError, match="does not parse as JSON"):
        read_backup(target)


def test_a_json_document_that_is_not_an_object_is_rejected(tmp_path):
    target = tmp_path / "list.json"
    target.write_text("[]", encoding="utf-8")
    with pytest.raises(BackupFileError, match="not a backup object"):
        read_backup(target)


def test_a_wrong_schema_is_rejected_by_name(tmp_path):
    def wrong_schema(parsed: dict) -> None:
        parsed["schema"] = "railctl/backup/v2"

    message = _rejection(tmp_path, wrong_schema)
    assert "railctl/backup/v2" in message
    assert "railctl/backup/v1" in message


@pytest.mark.parametrize(
    "key",
    [
        "schema",
        "created_utc",
        "tool",
        "note",
        "loco",
        "catalog",
        "set",
        "mode",
        "cv_encoding",
        "page",
        "speed_table_included",
        "sweep_range",
        "link",
        "capabilities",
        "decoder",
        "summary",
        "cvs",
    ],
)
def test_every_missing_top_level_key_is_rejected_by_name(tmp_path, key: str):
    message = _rejection(tmp_path, lambda parsed: parsed.pop(key))
    assert key in message


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("created_utc", 3),
        ("tool", 1),
        ("note", 3),
        ("mode", None),
        ("cv_encoding", 7),
        ("set", 5),
        ("speed_table_included", "no"),
        ("page", [0]),
        ("page", ["0", "0"]),
        ("page", [0, True]),
        ("sweep_range", [1]),
        ("loco", "x"),
        ("catalog", 3),
        ("link", []),
        ("capabilities", 4),
        ("decoder", []),
        ("summary", []),
        ("interrupted", "yes"),
        ("caveats", {}),
        ("cvs", {}),
    ],
)
def test_a_wrongly_typed_top_level_field_is_rejected_by_name(tmp_path, key: str, bad: object):
    def retype(parsed: dict) -> None:
        parsed[key] = bad

    assert key in _rejection(tmp_path, retype)


@pytest.mark.parametrize(("key", "null_ok"), [("note", None), ("cv_encoding", None)])
def test_the_two_nullable_fields_accept_null(tmp_path, key: str, null_ok: None):
    def nullify(parsed: dict) -> None:
        parsed[key] = null_ok

    assert getattr(read_backup(_write_mutated(tmp_path, nullify)), key) is None


# -- rejections: the cv rows -------------------------------------------------


def test_a_row_that_is_not_an_object_is_rejected(tmp_path):
    def scalar_row(parsed: dict) -> None:
        parsed["cvs"][0] = 3

    assert "cvs[0]" in _rejection(tmp_path, scalar_row)


@pytest.mark.parametrize("key", ["cv", "name", "status", "source"])
def test_a_row_missing_a_required_key_is_rejected(tmp_path, key: str):
    def drop(parsed: dict) -> None:
        del parsed["cvs"][2][key]

    assert key in _rejection(tmp_path, drop)


@pytest.mark.parametrize("bad_cv", [0, 1025, True, "1", None])
def test_a_cv_number_off_the_1_to_1024_range_or_not_an_integer_is_rejected(tmp_path, bad_cv):
    # 0 and 1025 sit one past the two boundaries; 1 and 1024 are accepted in
    # the boundary test above. True is the bool-is-not-an-int trap.
    def bad_number(parsed: dict) -> None:
        parsed["cvs"][0]["cv"] = bad_cv

    assert "1..1024" in _rejection(tmp_path, bad_number)


def test_a_duplicate_cv_row_is_rejected(tmp_path):
    def duplicate(parsed: dict) -> None:
        parsed["cvs"][1]["cv"] = 1

    assert "duplicate row for CV 1" in _rejection(tmp_path, duplicate)


def test_an_unknown_status_is_rejected(tmp_path):
    def unknown(parsed: dict) -> None:
        parsed["cvs"][0]["status"] = "does_not_exist"

    assert "does_not_exist" in _rejection(tmp_path, unknown)


def test_an_ok_row_without_a_value_is_rejected(tmp_path):
    def strip_value(parsed: dict) -> None:
        del parsed["cvs"][0]["value"]

    assert 'CV 1 is "ok" but has no value' in _rejection(tmp_path, strip_value)


@pytest.mark.parametrize("status_index", [1, 2], ids=["no_response", "skipped"])
def test_a_value_on_a_non_ok_row_is_rejected(tmp_path, status_index: int):
    def add_value(parsed: dict) -> None:
        parsed["cvs"][status_index]["value"] = 0

    assert "must carry no value" in _rejection(tmp_path, add_value)


@pytest.mark.parametrize("bad_value", [-1, 256, True, "3", None])
def test_a_value_off_the_byte_range_or_not_an_integer_is_rejected(tmp_path, bad_value):
    # -1 and 256 sit one past the byte's two boundaries; 0 and 255 are
    # accepted in the boundary test above.
    def bad(parsed: dict) -> None:
        parsed["cvs"][0]["value"] = bad_value

    assert "0..255" in _rejection(tmp_path, bad)


@pytest.mark.parametrize("key", ["name", "source", "detail"])
def test_a_non_string_row_text_field_is_rejected(tmp_path, key: str):
    def retype(parsed: dict) -> None:
        parsed["cvs"][1][key] = 3

    assert key in _rejection(tmp_path, retype)


# -- rejections: a summary that lies -----------------------------------------


def test_a_stored_summary_that_disagrees_with_the_rows_is_rejected(tmp_path):
    def inflate_ok(parsed: dict) -> None:
        parsed["summary"]["ok"] = 2

    message = _rejection(tmp_path, inflate_ok)
    assert "summary" in message
    assert "2" in message


def test_a_stored_complete_that_disagrees_with_the_rows_is_rejected(tmp_path):
    # The M10 precondition reads `complete`; a file edited to claim it would
    # otherwise smuggle a hole past the restore gate.
    def claim_complete(parsed: dict) -> None:
        parsed["summary"]["complete"] = True

    assert "complete" in _rejection(tmp_path, claim_complete)


def test_an_extra_summary_key_is_tolerated(tmp_path):
    def add_key(parsed: dict) -> None:
        parsed["summary"]["elapsed_s"] = 171

    assert read_backup(_write_mutated(tmp_path, add_key)) == make_document()


# -- the identity blocks M10's restore drives off ----------------------------


def test_a_loco_block_without_an_address_is_rejected(tmp_path):
    # `loco.address` is what restore re-targets the station to; a file that
    # names no locomotive cannot say whose settings it holds.
    def drop_address(parsed: dict) -> None:
        del parsed["loco"]["address"]

    assert "loco.address" in _rejection(tmp_path, drop_address)


@pytest.mark.parametrize("address", [0, 10000, "3", True])
def test_a_loco_address_outside_the_band_is_rejected(tmp_path, address):
    # 0 and 10000 sit one past each edge of 1..9999; a string and a bool are
    # the two shapes JSON makes easy to mistake for a number.
    def set_address(parsed: dict) -> None:
        parsed["loco"]["address"] = address

    assert "loco.address" in _rejection(tmp_path, set_address)


@pytest.mark.parametrize("address", [1, 9999])
def test_a_loco_address_on_either_edge_is_accepted(tmp_path, address):
    def set_address(parsed: dict) -> None:
        parsed["loco"]["address"] = address

    assert read_backup(_write_mutated(tmp_path, set_address)).loco["address"] == address


def test_an_unknown_loco_kind_is_rejected(tmp_path):
    def set_kind(parsed: dict) -> None:
        parsed["loco"]["kind"] = "medium"

    assert "loco.kind" in _rejection(tmp_path, set_kind)


def test_a_decoder_identity_field_that_is_not_a_byte_is_rejected(tmp_path):
    # The restore identity gate compares this against a live CV8 read, so a
    # value no CV could hold is a broken file, not a curiosity.
    def set_manufacturer(parsed: dict) -> None:
        parsed["decoder"]["manufacturer_id"] = 256

    assert "decoder.manufacturer_id" in _rejection(tmp_path, set_manufacturer)


def test_a_missing_decoder_field_is_tolerated(tmp_path):
    # A hole in the identity block is exactly what an unanswered CV leaves
    # behind; the gate must be able to tell that from a recorded value.
    def drop_type(parsed: dict) -> None:
        del parsed["decoder"]["decoder_type"]

    assert "decoder_type" not in read_backup(_write_mutated(tmp_path, drop_type)).decoder


@pytest.mark.parametrize("serial", [[10, 27], [10, 27, 44, 61], [10, 27, 256], "10,27,44"])
def test_a_serial_that_is_not_three_bytes_is_rejected(tmp_path, serial):
    def set_serial(parsed: dict) -> None:
        parsed["decoder"]["serial_bytes"] = serial

    assert "serial_bytes" in _rejection(tmp_path, set_serial)


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_a_capability_that_is_not_three_valued_is_rejected(tmp_path, value):
    # The founding rule at the file boundary: a capability is true, false or
    # null. A string "false" or an integer 0 would read as a measurement
    # nobody made.
    def set_capability(parsed: dict) -> None:
        parsed["capabilities"]["pom_read"] = value

    assert "capabilities.pom_read" in _rejection(tmp_path, set_capability)


def test_a_null_capability_is_accepted(tmp_path):
    def unprobe(parsed: dict) -> None:
        parsed["capabilities"]["pom_read"] = None

    assert read_backup(_write_mutated(tmp_path, unprobe)).capabilities["pom_read"] is None


# -- the cursor recorded twice -----------------------------------------------


def test_a_page_that_disagrees_with_its_own_selector_rows_is_rejected(tmp_path):
    # While CV31/CV32 remain in the curated payload, the file states the
    # cursor twice. The two must agree or the file is not usable evidence of
    # which page its indexed values came from.
    def add_disagreeing_selectors(parsed: dict) -> None:
        parsed["cvs"].append(
            {"cv": 31, "name": "index_high", "status": "ok", "value": 145, "source": "catalog"}
        )
        parsed["summary"]["requested"] = 4
        parsed["summary"]["ok"] = 2

    message = _rejection(tmp_path, add_disagreeing_selectors)
    assert "CV31" in message
    assert "disagree" in message


def test_a_page_that_matches_its_selector_rows_is_accepted(tmp_path):
    def add_agreeing_selectors(parsed: dict) -> None:
        parsed["cvs"].append(
            {"cv": 31, "name": "index_high", "status": "ok", "value": 0, "source": "catalog"}
        )
        parsed["summary"]["requested"] = 4
        parsed["summary"]["ok"] = 2

    assert len(read_backup(_write_mutated(tmp_path, add_agreeing_selectors)).cvs) == 4


def test_an_unanswered_selector_row_says_nothing_about_the_page(tmp_path):
    # A selector that did not answer is silence, and silence never
    # contradicts a recorded page.
    def add_silent_selector(parsed: dict) -> None:
        parsed["cvs"].append(
            {"cv": 31, "name": "index_high", "status": "no_response", "source": "catalog"}
        )
        parsed["page"] = [145, 0]
        parsed["summary"]["requested"] = 4
        parsed["summary"]["no_response"] = 2

    assert read_backup(_write_mutated(tmp_path, add_silent_selector)).page == (145, 0)
