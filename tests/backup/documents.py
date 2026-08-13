# tests/backup/documents.py
"""One valid document, built the same way every time.

The values are the design document's C4 example (three rows instead of 77),
so every test in this package starts from a file the spec itself calls
well-formed, and a reader-rejection fixture is always "the good file plus
exactly one mutation".
"""

from __future__ import annotations

from railctl.backup import BackupDocument, CvRecord, ReadStatus


def example_records() -> tuple[CvRecord, ...]:
    return (
        CvRecord(cv=1, name="primary_address", status=ReadStatus.OK, value=3),
        CvRecord(
            cv=253,
            name="serial_byte_3",
            status=ReadStatus.NO_RESPONSE,
            detail="no answer after 3 attempts (pom)",
        ),
        CvRecord(
            cv=397,
            name="volume_up_key",
            status=ReadStatus.SKIPPED,
            detail="cv 397 > MAX_CV_DIRECT 255; extended opcodes unavailable",
        ),
    )


def make_document(**overrides: object) -> BackupDocument:
    fields: dict[str, object] = {
        "created_utc": "2026-08-03T18:42:11Z",
        "tool": "railctl 0.1.0",
        "note": "stock settings",
        "loco": {"address": 3, "kind": "short"},
        "catalog": {"family": "zimo-ms-mx", "schema": 1},
        "set_name": "curated",
        "mode": "pom",
        "cv_encoding": "POM_ZERO_BASED",
        "page": (0, 0),
        "speed_table_included": False,
        "sweep_range": None,
        "link": {
            "identity": "serial:7010A0001194:3",
            "protocol": "xpressnet",
            "protocol_version": "4.0",
            "command_station_id": 18,
        },
        "capabilities": {
            "pom_read": True,
            "pom_result_channel": "poll",
            "pom_echo_zero_based": True,
            "service_direct_cv": True,
            "service_ext_cv": False,
            "z21_cv_opcodes": False,
        },
        "decoder": {
            "manufacturer_id": 145,
            "decoder_version": 34,
            "decoder_type": 217,
            "serial_bytes": [10, 27, 44],
        },
        "cvs": example_records(),
    }
    fields.update(overrides)
    return BackupDocument(**fields)  # type: ignore[arg-type]
