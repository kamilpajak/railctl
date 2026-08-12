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

from railctl.backup import CvRecord, ReadStatus, read_backup, write_backup, write_backup_to
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
