# src/railctl/backup/file.py
"""Writer, reader and default-path logic for `railctl/backup/v1` files.

The writer is a pure function of the document: the caller supplies
`created_utc`, and everything else is serialized under the design's writer
rules (fixed key order, `cvs` ascending, `indent=2`, LF endings, a trailing
newline, `ensure_ascii=False`, no top-level key omitted, no `value` key
unless the row is `ok`). Two runs over an unchanged decoder therefore
produce byte-identical files once the timestamp is fixed.

The reader is deliberately strict, because M10's restore drives writes off
what it returns: a wrong schema, a missing key, a `value` that disagrees
with its `status` (in either direction), a duplicate CV row, or a stored
summary that disagrees with the rows is a `BackupFileError` naming the
first offence - never a silent default. Row order is sorted on write and
tolerated on read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from railctl.backup.types import (
    BACKUP_SCHEMA,
    SUMMARY_KEYS,
    BackupDocument,
    CvRecord,
    ReadStatus,
)
from railctl.errors import BackupFileError

#: Default backups live in `~/railctl-backups`, resolved against the home
#: directory at call time so a test can point HOME elsewhere.
BACKUP_DIR_NAME: Final[str] = "railctl-backups"

#: `--out -` means "write the document to stdout instead of a file".
STDOUT_TARGET: Final[str] = "-"

#: A CV number as the operator sees it, and the byte a CV holds. The file
#: format's own bounds - the reader owns them, so a station never has to be
#: importable to validate a file.
CV_MIN: Final[int] = 1
CV_MAX: Final[int] = 1024
VALUE_MIN: Final[int] = 0
VALUE_MAX: Final[int] = 255

#: The fixed top-level key order - the writer emits exactly these, in this
#: order, and the reader rejects a file missing any of them. `interrupted`
#: is not here on purpose: it is emitted between `summary` and `cvs` only
#: when true, so a run that finished produces a file matching the design
#: example key for key.
TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (
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
)

#: Nested-block key orders. The writer re-orders each block itself so the
#: bytes cannot hang on the caller's dict insertion order; a key a block
#: legitimately omits (a `decoder` field that failed to read is a hole, not
#: an abort) is simply skipped, and unknown keys follow the fixed ones in
#: sorted order.
_LOCO_KEYS: Final[tuple[str, ...]] = ("address", "kind")
_CATALOG_KEYS: Final[tuple[str, ...]] = ("family", "schema")
_LINK_KEYS: Final[tuple[str, ...]] = (
    "identity",
    "protocol",
    "protocol_version",
    "command_station_id",
)
_CAPABILITY_KEYS: Final[tuple[str, ...]] = (
    "pom_read",
    "pom_result_channel",
    "pom_echo_zero_based",
    "service_direct_cv",
    "service_ext_cv",
    "z21_cv_opcodes",
)
_DECODER_KEYS: Final[tuple[str, ...]] = (
    "manufacturer_id",
    "decoder_version",
    "decoder_type",
    "serial_bytes",
)

_ROW_REQUIRED_KEYS: Final[tuple[str, ...]] = ("cv", "name", "status", "source")


def backup_path(address: int, set_name: str, out: str | None) -> Path | None:
    """Where a backup lands: the resolved file path, or `None` for stdout.

    `out` is the raw `--out` value. `None` resolves to the default
    `~/railctl-backups/loco-<address:04d>-<set>.json`; `"-"` means stdout;
    a path that exists as a directory gets the generated name appended; any
    other path is taken as given (whether it may be overwritten is the
    caller's `--force` business, decided before anything opens a port).
    """
    name = f"loco-{address:04d}-{set_name}.json"
    if out is None:
        return Path.home() / BACKUP_DIR_NAME / name
    if out == STDOUT_TARGET:
        return None
    target = Path(out).expanduser()
    if target.is_dir():
        return target / name
    return target


def write_backup(document: BackupDocument) -> str:
    """The exact serialized document, deterministic per the writer rules.

    Raises `ValueError` on duplicate CV rows: the reader rejects them, and a
    writer that can produce what its own reader refuses is two contracts
    pretending to be one.
    """
    numbers = [record.cv for record in document.cvs]
    duplicates = sorted({cv for cv in numbers if numbers.count(cv) > 1})
    if duplicates:
        raise ValueError(f"duplicate CV rows for {duplicates}; one row per CV")
    top: dict[str, object] = {
        "schema": BACKUP_SCHEMA,
        "created_utc": document.created_utc,
        "tool": document.tool,
        "note": document.note,
        "loco": _ordered_block(document.loco, _LOCO_KEYS),
        "catalog": _ordered_block(document.catalog, _CATALOG_KEYS),
        "set": document.set_name,
        "mode": document.mode,
        "cv_encoding": document.cv_encoding,
        "page": list(document.page),
        "speed_table_included": document.speed_table_included,
        "sweep_range": None if document.sweep_range is None else list(document.sweep_range),
        "link": _ordered_block(document.link, _LINK_KEYS),
        "capabilities": _ordered_block(document.capabilities, _CAPABILITY_KEYS),
        "decoder": _ordered_block(document.decoder, _DECODER_KEYS),
        "summary": document.summary,
    }
    if document.interrupted:
        top["interrupted"] = True
    top["cvs"] = [_cv_row(record) for record in sorted(document.cvs, key=lambda r: r.cv)]
    return json.dumps(top, indent=2, ensure_ascii=False) + "\n"


def write_backup_to(document: BackupDocument, path: Path) -> None:
    """Serialize `document` and write it to `path`, creating parent
    directories - the default backup directory does not exist until the
    first backup lands in it. A refused write is a `BackupFileError` naming
    the path, never a bare OSError dressed up as a railctl bug."""
    text = write_backup(document)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    except OSError as exc:
        raise BackupFileError(f"cannot write backup file {path}: {exc}") from exc


def read_backup(path: Path) -> BackupDocument:
    """Load and validate a backup file; `BackupFileError` names the first
    offence. Row order is tolerated as found - the returned `cvs` keep file
    order, and the next write sorts them again."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupFileError(f"cannot read backup file {path}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupFileError(f"{path} does not parse as JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BackupFileError(f"{path} holds a JSON {type(parsed).__name__}, not a backup object")
    schema = parsed.get("schema")
    if schema != BACKUP_SCHEMA:
        raise BackupFileError(f"{path}: schema is {schema!r}, expected {BACKUP_SCHEMA!r}")
    missing = [key for key in TOP_LEVEL_KEYS if key not in parsed]
    if missing:
        raise BackupFileError(f"{path} is missing top-level keys {missing}")

    interrupted = parsed.get("interrupted", False)
    if not isinstance(interrupted, bool):
        raise BackupFileError(f"{path}: interrupted must be a boolean, got {interrupted!r}")
    document = BackupDocument(
        created_utc=_str_of(parsed, "created_utc", path),
        tool=_str_of(parsed, "tool", path),
        note=_optional_str_of(parsed, "note", path),
        loco=_object_of(parsed, "loco", path),
        catalog=_object_of(parsed, "catalog", path),
        set_name=_str_of(parsed, "set", path),
        mode=_str_of(parsed, "mode", path),
        cv_encoding=_optional_str_of(parsed, "cv_encoding", path),
        page=_int_pair(parsed["page"], "page", path),
        speed_table_included=_bool_of(parsed, "speed_table_included", path),
        sweep_range=(
            None
            if parsed["sweep_range"] is None
            else _int_pair(parsed["sweep_range"], "sweep_range", path)
        ),
        link=_object_of(parsed, "link", path),
        capabilities=_object_of(parsed, "capabilities", path),
        decoder=_object_of(parsed, "decoder", path),
        cvs=_records_of(parsed["cvs"], path),
        interrupted=interrupted,
    )
    stored_summary = _object_of(parsed, "summary", path)
    for key, expected in document.summary.items():
        if key in SUMMARY_KEYS and stored_summary.get(key) != expected:
            raise BackupFileError(
                f"{path}: summary[{key!r}] is {stored_summary.get(key)!r} but the cv "
                f"rows say {expected!r}"
            )
    return document


def _ordered_block(block: Mapping[str, object], key_order: tuple[str, ...]) -> dict[str, object]:
    ordered: dict[str, object] = {key: block[key] for key in key_order if key in block}
    ordered |= {key: block[key] for key in sorted(set(block) - set(key_order))}
    return ordered


def _cv_row(record: CvRecord) -> dict[str, object]:
    # `value` only when the record holds one - `CvRecord` already guarantees
    # that means `status == "ok"` - and `detail` only when there is one, so a
    # hole in the file is a missing key, never a null.
    row: dict[str, object] = {"cv": record.cv, "name": record.name, "status": record.status.value}
    if record.value is not None:
        row["value"] = record.value
    row["source"] = record.source
    if record.detail is not None:
        row["detail"] = record.detail
    return row


def _str_of(parsed: Mapping[str, object], key: str, path: Path) -> str:
    value = parsed[key]
    if not isinstance(value, str):
        raise BackupFileError(f"{path}: {key} must be a string, got {value!r}")
    return value


def _optional_str_of(parsed: Mapping[str, object], key: str, path: Path) -> str | None:
    value = parsed[key]
    if value is not None and not isinstance(value, str):
        raise BackupFileError(f"{path}: {key} must be a string or null, got {value!r}")
    return value


def _bool_of(parsed: Mapping[str, object], key: str, path: Path) -> bool:
    value = parsed[key]
    if not isinstance(value, bool):
        raise BackupFileError(f"{path}: {key} must be a boolean, got {value!r}")
    return value


def _object_of(parsed: Mapping[str, object], key: str, path: Path) -> dict[str, object]:
    value = parsed[key]
    if not isinstance(value, dict):
        raise BackupFileError(f"{path}: {key} must be an object, got {type(value).__name__}")
    return value


def _is_int(value: object) -> bool:
    # bool is an int subclass, and `"cv": true` must not read as CV 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _int_pair(value: object, key: str, path: Path) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or not all(_is_int(item) for item in value):
        raise BackupFileError(f"{path}: {key} must be a pair of integers, got {value!r}")
    return (value[0], value[1])


def _records_of(raw: object, path: Path) -> tuple[CvRecord, ...]:
    if not isinstance(raw, list):
        raise BackupFileError(f"{path}: cvs must be an array, got {type(raw).__name__}")
    records: list[CvRecord] = []
    seen: set[int] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise BackupFileError(f"{path}: cvs[{index}] is not an object, got {row!r}")
        missing = [key for key in _ROW_REQUIRED_KEYS if key not in row]
        if missing:
            raise BackupFileError(f"{path}: cvs[{index}] is missing {missing}")
        cv = row["cv"]
        if not _is_int(cv) or not CV_MIN <= cv <= CV_MAX:
            raise BackupFileError(
                f"{path}: cvs[{index}]: cv must be an integer in {CV_MIN}..{CV_MAX}, got {cv!r}"
            )
        if cv in seen:
            raise BackupFileError(f"{path}: duplicate row for CV {cv}")
        seen.add(cv)
        try:
            status = ReadStatus(row["status"])
        except ValueError:
            raise BackupFileError(f"{path}: CV {cv}: unknown status {row['status']!r}") from None
        value: int | None = None
        if status is ReadStatus.OK:
            if "value" not in row:
                raise BackupFileError(f'{path}: CV {cv} is "ok" but has no value')
            value = row["value"]
            if not _is_int(value) or not VALUE_MIN <= value <= VALUE_MAX:
                raise BackupFileError(
                    f"{path}: CV {cv}: value must be an integer in "
                    f"{VALUE_MIN}..{VALUE_MAX}, got {value!r}"
                )
        elif "value" in row:
            raise BackupFileError(
                f"{path}: CV {cv}: status {status.value!r} must carry no value, "
                f"got {row['value']!r} - a hole is never a number"
            )
        name = row["name"]
        if not isinstance(name, str):
            raise BackupFileError(f"{path}: CV {cv}: name must be a string, got {name!r}")
        source = row["source"]
        if not isinstance(source, str):
            raise BackupFileError(f"{path}: CV {cv}: source must be a string, got {source!r}")
        detail = row.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise BackupFileError(f"{path}: CV {cv}: detail must be a string, got {detail!r}")
        records.append(
            CvRecord(cv=cv, name=name, status=status, source=source, value=value, detail=detail)
        )
    return tuple(records)
