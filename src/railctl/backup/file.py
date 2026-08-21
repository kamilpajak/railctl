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
    Caveat,
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
#: and `caveats` are not here on purpose: they are emitted between `summary`
#: and `cvs` only when they apply, so a run that finished produces a file
#: matching the design example key for key.
#:
#: This tuple drives the missing-key check and nothing else - the reader has
#: no unknown-key rejection - so a reader built before a v1 addition still
#: loads a file that carries it, and this reader still loads every file
#: written before `caveats` existed.
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

#: A locomotive address as XpressNet carries it.
ADDRESS_MIN: Final[int] = 1
ADDRESS_MAX: Final[int] = 9999

#: The two values `loco.kind` may hold - the short/long split the writer
#: derives from the address.
LOCO_KINDS: Final[tuple[str, ...]] = ("short", "long")

#: The `decoder` fields that hold one CV byte each. Every one of them is
#: OPTIONAL: a CV that did not answer leaves its field out (a hole in the
#: block, never an abort), so the reader checks the type of what is there
#: and never demands the field itself.
_DECODER_BYTE_FIELDS: Final[tuple[str, ...]] = (
    "manufacturer_id",
    "decoder_version",
    "decoder_type",
)
SERIAL_BYTE_COUNT: Final[int] = 3

#: The five capabilities that are three-valued (`true`/`false`/`null`) and
#: the one that is a channel name or `null`. Split because coercing either
#: into the other is exactly the mistake the project's founding rule forbids:
#: a capability must never be recorded as absent because something could not
#: read it.
_TRISTATE_CAPABILITIES: Final[tuple[str, ...]] = (
    "pom_read",
    "pom_echo_zero_based",
    "service_direct_cv",
    "service_ext_cv",
    "z21_cv_opcodes",
)
_STRING_CAPABILITIES: Final[tuple[str, ...]] = ("pom_result_channel",)

#: The index selectors. When the payload happens to carry rows for them - the
#: curated set does today - their values and the top-level `page` describe
#: the same cursor, and the reader refuses a file where the two disagree.
PAGE_SELECTOR_ROWS: Final[tuple[int, int]] = (31, 32)


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

    Raises `ValueError` on duplicate CV rows and on a cv or value outside
    the reader's bounds: the reader rejects all of them, and a writer that
    can produce what its own reader refuses is two contracts pretending to
    be one.
    """
    numbers = [record.cv for record in document.cvs]
    duplicates = sorted({cv for cv in numbers if numbers.count(cv) > 1})
    if duplicates:
        raise ValueError(f"duplicate CV rows for {duplicates}; one row per CV")
    for record in document.cvs:
        if not CV_MIN <= record.cv <= CV_MAX:
            raise ValueError(
                f"CV {record.cv} is outside {CV_MIN}..{CV_MAX}; the reader rejects such a row"
            )
        if record.value is not None and not VALUE_MIN <= record.value <= VALUE_MAX:
            raise ValueError(
                f"CV {record.cv}: value {record.value} is outside {VALUE_MIN}..{VALUE_MAX}; "
                f"the reader rejects such a row"
            )
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
    if document.caveats:
        # Order as given: the document decides which caveats it carries and in
        # which order, and sorting them here would make the bytes depend on
        # the spelling of a code rather than on the decision that produced it.
        top["caveats"] = [
            {"code": caveat.code, "message": caveat.message} for caveat in document.caveats
        ]
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
        caveats=_caveats_of(parsed.get("caveats", []), path),
    )
    stored_summary = _object_of(parsed, "summary", path)
    for key, expected in document.summary.items():
        if key in SUMMARY_KEYS and stored_summary.get(key) != expected:
            raise BackupFileError(
                f"{path}: summary[{key!r}] is {stored_summary.get(key)!r} but the cv "
                f"rows say {expected!r}"
            )
    _check_loco(document.loco, path)
    _check_decoder(document.decoder, path)
    _check_capabilities(document.capabilities, path)
    _check_page_agrees_with_rows(document, path)
    return document


def _caveats_of(raw: object, path: Path) -> tuple[Caveat, ...]:
    """The `caveats` array, absent in every file written before the key
    existed - hence the `parsed.get(..., [])` at the call site.

    The shape is checked and the vocabulary is not: an entry must be an
    object with a string `code` and a string `message`, but an unrecognised
    `code` loads as written. This reader is not the place that decides which
    caveats exist, and refusing a token added after it was written would make
    a forward-compatible v1 file unreadable.
    """
    if not isinstance(raw, list):
        raise BackupFileError(f"{path}: caveats must be an array, got {type(raw).__name__}")
    caveats: list[Caveat] = []
    for index, item in enumerate(raw):
        code = item.get("code") if isinstance(item, Mapping) else None
        message = item.get("message") if isinstance(item, Mapping) else None
        if not isinstance(code, str) or not isinstance(message, str):
            raise BackupFileError(
                f"{path}: caveats[{index}] must be an object with a string code and a "
                f"string message, got {item!r}"
            )
        caveats.append(Caveat(code=code, message=message))
    return tuple(caveats)


def _check_loco(block: Mapping[str, object], path: Path) -> None:
    """`loco` is the one identity block with no optional part: M10's restore
    re-targets the station off `address`, and a file that names no locomotive
    cannot say whose settings it holds."""
    address = block.get("address")
    if not _is_int(address) or not ADDRESS_MIN <= address <= ADDRESS_MAX:  # type: ignore[operator]
        raise BackupFileError(
            f"{path}: loco.address must be an integer in {ADDRESS_MIN}..{ADDRESS_MAX}, "
            f"got {address!r}"
        )
    kind = block.get("kind")
    if kind not in LOCO_KINDS:
        raise BackupFileError(f"{path}: loco.kind must be one of {LOCO_KINDS}, got {kind!r}")


def _check_decoder(block: Mapping[str, object], path: Path) -> None:
    """Type-check what the `decoder` block carries without demanding it: an
    identity CV that did not answer leaves its field absent, and M10's
    identity gate must be able to tell "the file never learned this" from
    "the file says 145". A present field that is not a byte is a broken file,
    because the gate compares it against a live read."""
    for field in _DECODER_BYTE_FIELDS:
        if field not in block:
            continue
        value = block[field]
        if not _is_int(value) or not VALUE_MIN <= value <= VALUE_MAX:  # type: ignore[operator]
            raise BackupFileError(
                f"{path}: decoder.{field} must be an integer in "
                f"{VALUE_MIN}..{VALUE_MAX}, got {value!r}"
            )
    if "serial_bytes" not in block:
        return
    serial = block["serial_bytes"]
    if (
        not isinstance(serial, list)
        or len(serial) != SERIAL_BYTE_COUNT
        or not all(_is_int(byte) and VALUE_MIN <= byte <= VALUE_MAX for byte in serial)
    ):
        raise BackupFileError(
            f"{path}: decoder.serial_bytes must be {SERIAL_BYTE_COUNT} integers in "
            f"{VALUE_MIN}..{VALUE_MAX}, got {serial!r}"
        )


def _check_capabilities(block: Mapping[str, object], path: Path) -> None:
    """The three-valued rule, enforced at the file boundary. A capability may
    be `true`, `false` or `null` and nothing else - a string "false" or a 0
    would read as a measurement nobody made."""
    for key in _TRISTATE_CAPABILITIES:
        if key in block and block[key] is not None and not isinstance(block[key], bool):
            raise BackupFileError(
                f"{path}: capabilities.{key} must be true, false or null, got {block[key]!r}"
            )
    for key in _STRING_CAPABILITIES:
        if key in block and block[key] is not None and not isinstance(block[key], str):
            raise BackupFileError(
                f"{path}: capabilities.{key} must be a string or null, got {block[key]!r}"
            )


def _check_page_agrees_with_rows(document: BackupDocument, path: Path) -> None:
    """The cursor is recorded twice - as `page` and, while the curated set
    still contains them, as the CV31/CV32 rows - so the reader refuses a file
    where the two disagree. Only `ok` rows are compared: a selector that did
    not answer says nothing about the page. Silent on a payload with no
    selector rows, which is what a file written after they leave the curated
    set looks like."""
    rows = {record.cv: record for record in document.cvs}
    for cv, recorded in zip(PAGE_SELECTOR_ROWS, document.page, strict=True):
        row = rows.get(cv)
        if row is None or row.status is not ReadStatus.OK:
            continue
        if row.value != recorded:
            raise BackupFileError(
                f"{path}: page says CV{cv}={recorded} but the CV{cv} row reads "
                f"{row.value} - the file records one cursor twice and the two disagree"
            )


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
