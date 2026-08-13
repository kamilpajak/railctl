# tests/backup/test_writer.py
"""Every writer rule from the design's C4 section, each pinned on the bytes.

The golden literal below is the design example's shape with three rows, so
the fixed key order is proved against the spec's own text rather than
against the writer's own constants - a writer and a test that both read
`TOP_LEVEL_KEYS` would agree with each other and with nothing else.
"""

from __future__ import annotations

import json

import pytest

from railctl.backup import (
    TOP_LEVEL_KEYS,
    CvRecord,
    ReadStatus,
    write_backup,
    write_backup_to,
)
from railctl.errors import BackupFileError
from tests.backup.documents import example_records, make_document

GOLDEN = """\
{
  "schema": "railctl/backup/v1",
  "created_utc": "2026-08-03T18:42:11Z",
  "tool": "railctl 0.1.0",
  "note": "stock settings",
  "loco": {
    "address": 3,
    "kind": "short"
  },
  "catalog": {
    "family": "zimo-ms-mx",
    "schema": 1
  },
  "set": "curated",
  "mode": "pom",
  "cv_encoding": "POM_ZERO_BASED",
  "page": [
    0,
    0
  ],
  "speed_table_included": false,
  "sweep_range": null,
  "link": {
    "identity": "serial:7010A0001194:3",
    "protocol": "xpressnet",
    "protocol_version": "4.0",
    "command_station_id": 18
  },
  "capabilities": {
    "pom_read": true,
    "pom_result_channel": "poll",
    "pom_echo_zero_based": true,
    "service_direct_cv": true,
    "service_ext_cv": false,
    "z21_cv_opcodes": false
  },
  "decoder": {
    "manufacturer_id": 145,
    "decoder_version": 34,
    "decoder_type": 217,
    "serial_bytes": [
      10,
      27,
      44
    ]
  },
  "summary": {
    "requested": 3,
    "ok": 1,
    "no_response": 1,
    "error": 0,
    "skipped": 1,
    "complete": false
  },
  "cvs": [
    {
      "cv": 1,
      "name": "primary_address",
      "status": "ok",
      "value": 3,
      "source": "catalog"
    },
    {
      "cv": 253,
      "name": "serial_byte_3",
      "status": "no_response",
      "source": "catalog",
      "detail": "no answer after 3 attempts (pom)"
    },
    {
      "cv": 397,
      "name": "volume_up_key",
      "status": "skipped",
      "source": "catalog",
      "detail": "cv 397 > MAX_CV_DIRECT 255; extended opcodes unavailable"
    }
  ]
}
"""


def test_the_writer_produces_the_design_examples_exact_shape():
    assert write_backup(make_document()) == GOLDEN


def test_two_writes_with_the_same_inputs_are_byte_identical():
    # Two documents built independently, not one written twice: this is the
    # M9 acceptance ("two consecutive backups of an unchanged decoder are
    # byte-identical") with the timestamp injection the design decided on.
    first = write_backup(make_document())
    second = write_backup(make_document())
    assert first == second


def test_top_level_key_order_is_fixed():
    # json.loads preserves file order in the dict, so list() reads the order
    # the writer actually emitted.
    assert list(json.loads(write_backup(make_document()))) == list(TOP_LEVEL_KEYS)


def test_cvs_are_sorted_ascending_regardless_of_input_order():
    shuffled = make_document(cvs=tuple(reversed(example_records())))
    rows = json.loads(write_backup(shuffled))["cvs"]
    assert [row["cv"] for row in rows] == [1, 253, 397]


def test_indent_is_two_spaces_with_lf_endings_and_one_trailing_newline():
    text = write_backup(make_document())
    assert text.startswith('{\n  "schema"')
    assert "\r" not in text
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")


def test_non_ascii_text_is_written_literally():
    text = write_backup(make_document(note="Rangierlok Kö II, München"))
    assert "Rangierlok Kö II, München" in text
    assert "\\u" not in text


def test_a_value_key_appears_on_ok_rows_and_on_no_other():
    rows = json.loads(write_backup(make_document()))["cvs"]
    by_status = {row["status"]: row for row in rows}
    assert by_status["ok"]["value"] == 3
    assert "value" not in by_status["no_response"]
    assert "value" not in by_status["skipped"]


def test_row_keys_follow_the_examples_order():
    rows = json.loads(write_backup(make_document()))["cvs"]
    assert list(rows[0]) == ["cv", "name", "status", "value", "source"]
    assert list(rows[1]) == ["cv", "name", "status", "source", "detail"]


def test_the_summary_is_computed_from_the_rows():
    summary = json.loads(write_backup(make_document()))["summary"]
    assert summary == {
        "requested": 3,
        "ok": 1,
        "no_response": 1,
        "error": 0,
        "skipped": 1,
        "complete": False,
    }


def test_skips_alone_do_not_make_a_file_incomplete():
    records = (
        CvRecord(cv=1, name="primary_address", status=ReadStatus.OK, value=3),
        CvRecord(cv=397, name="volume_up_key", status=ReadStatus.SKIPPED, detail="out of reach"),
    )
    summary = json.loads(write_backup(make_document(cvs=records)))["summary"]
    assert summary["complete"] is True


def test_an_error_row_makes_the_file_incomplete():
    records = (
        CvRecord(cv=1, name="primary_address", status=ReadStatus.OK, value=3),
        CvRecord(cv=8, name="manufacturer_id", status=ReadStatus.ERROR, detail="short circuit"),
    )
    summary = json.loads(write_backup(make_document(cvs=records)))["summary"]
    assert summary["complete"] is False


def test_an_interrupted_document_is_never_complete():
    # Every row ok, yet the run was cut short: `complete` answers for the
    # run, and an interrupted run is not complete by definition.
    records = (CvRecord(cv=1, name="primary_address", status=ReadStatus.OK, value=3),)
    summary = json.loads(write_backup(make_document(cvs=records, interrupted=True)))["summary"]
    assert summary["complete"] is False


def test_interrupted_is_absent_when_false_and_sits_before_cvs_when_true():
    completed = write_backup(make_document())
    assert "interrupted" not in json.loads(completed)
    partial = write_backup(make_document(interrupted=True))
    keys = list(json.loads(partial))
    assert keys == [*TOP_LEVEL_KEYS[:-1], "interrupted", "cvs"]
    assert json.loads(partial)["interrupted"] is True


def test_nested_block_order_does_not_depend_on_caller_insertion_order():
    scrambled = make_document(loco={"kind": "short", "address": 3})
    assert write_backup(scrambled) == write_backup(make_document())


def test_a_decoder_field_that_failed_to_read_is_omitted_not_nulled():
    # Design C6 step 4: a decoder-identity failure is a hole, not an abort,
    # and the field is omitted from the block.
    holey = make_document(decoder={"manufacturer_id": 145, "decoder_type": 217})
    decoder = json.loads(write_backup(holey))["decoder"]
    assert decoder == {"manufacturer_id": 145, "decoder_type": 217}
    assert list(decoder) == ["manufacturer_id", "decoder_type"]


def test_attempts_never_reaches_the_file_row():
    # `attempts` is stream-side metadata (the ndjson `cv` line carries it);
    # the file writer emits its explicit keys only, so the row stays the
    # design example's shape whether or not the record holds a count.
    record = CvRecord(
        cv=253,
        name="serial_byte_3",
        status=ReadStatus.NO_RESPONSE,
        detail="no answer after 3 attempts (pom)",
        attempts=3,
    )
    row = json.loads(write_backup(make_document(cvs=(record,))))["cvs"][0]
    assert "attempts" not in row


def test_the_writer_accepts_the_readers_boundary_cv_numbers_and_values():
    # Both ends ON the boundary, mirroring the reader's acceptance test:
    # CV 1 with value 0, CV 1024 with value 255.
    records = (
        CvRecord(cv=1, name="low", status=ReadStatus.OK, value=0),
        CvRecord(cv=1024, name="high", status=ReadStatus.OK, value=255),
    )
    rows = json.loads(write_backup(make_document(cvs=records)))["cvs"]
    assert [(row["cv"], row["value"]) for row in rows] == [(1, 0), (1024, 255)]


@pytest.mark.parametrize("cv", [0, 1025])
def test_the_writer_refuses_a_cv_off_the_readers_bounds(cv: int):
    # One step past each end: the reader rejects these rows, and a writer
    # that can produce what its own reader refuses is two contracts
    # pretending to be one - the same reasoning as the duplicate check.
    records = (CvRecord(cv=cv, name="off", status=ReadStatus.OK, value=1),)
    with pytest.raises(ValueError, match=f"CV {cv}"):
        write_backup(make_document(cvs=records))


@pytest.mark.parametrize("value", [-1, 256])
def test_the_writer_refuses_a_value_off_the_readers_bounds(value: int):
    records = (CvRecord(cv=1, name="off", status=ReadStatus.OK, value=value),)
    with pytest.raises(ValueError, match=f"value {value}"):
        write_backup(make_document(cvs=records))


def test_the_writer_refuses_duplicate_cv_rows():
    doubled = make_document(cvs=(*example_records(), example_records()[0]))
    with pytest.raises(ValueError, match=r"duplicate CV rows for \[1\]"):
        write_backup(doubled)


def test_write_backup_to_writes_exactly_the_serialized_bytes(tmp_path):
    target = tmp_path / "loco-0003-curated.json"
    write_backup_to(make_document(), target)
    assert target.read_bytes() == write_backup(make_document()).encode("utf-8")


def test_write_backup_to_creates_the_missing_backup_directory(tmp_path):
    target = tmp_path / "railctl-backups" / "loco-0003-curated.json"
    write_backup_to(make_document(), target)
    assert target.is_file()


def test_a_refused_write_is_a_backup_file_error_naming_the_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    target = blocker / "loco-0003-curated.json"
    with pytest.raises(BackupFileError, match="cannot write backup file"):
        write_backup_to(make_document(), target)


def test_a_cv_record_with_ok_and_no_value_cannot_be_built():
    with pytest.raises(ValueError, match='status "ok" requires a value'):
        CvRecord(cv=1, name="primary_address", status=ReadStatus.OK)


def test_a_cv_record_with_a_value_on_a_non_ok_status_cannot_be_built():
    with pytest.raises(ValueError, match="must carry no value"):
        CvRecord(cv=253, name="serial_byte_3", status=ReadStatus.NO_RESPONSE, value=0)


def test_a_document_round_trips_through_python_json_unchanged():
    # The file is also the `--format=json` envelope's `result`, so it must
    # survive a parse-and-redump by any JSON tool with its content intact.
    text = write_backup(make_document())
    assert json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n" == text
