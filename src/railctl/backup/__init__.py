# src/railctl/backup/__init__.py
"""CV backup files: the `railctl/backup/v1` writer, reader and vocabulary.

This package sits above the station facade exactly like `railctl.catalog`:
it speaks in human CV numbers (CV1 = 1) and plain integers, talks only to
`Station` results, and never to the wire layers - tests/test_layering.py
rule 5 enforces that mechanically. `types.py` and `file.py` depend on the
standard library alone, so M10's restore can read a backup with no station
attached; `mapping.py` is the one module here that names station types.
"""

from railctl.backup.file import (
    BACKUP_DIR_NAME,
    CV_MAX,
    CV_MIN,
    STDOUT_TARGET,
    TOP_LEVEL_KEYS,
    VALUE_MAX,
    VALUE_MIN,
    backup_path,
    read_backup,
    write_backup,
    write_backup_to,
)
from railctl.backup.mapping import NOT_ATTEMPTED_DETAIL, record_for, status_for
from railctl.backup.types import (
    BACKUP_SCHEMA,
    SOURCE_CATALOG,
    SUMMARY_KEYS,
    BackupDocument,
    CvRecord,
    ReadStatus,
)

__all__ = [
    "BACKUP_DIR_NAME",
    "BACKUP_SCHEMA",
    "CV_MAX",
    "CV_MIN",
    "NOT_ATTEMPTED_DETAIL",
    "SOURCE_CATALOG",
    "STDOUT_TARGET",
    "SUMMARY_KEYS",
    "TOP_LEVEL_KEYS",
    "VALUE_MAX",
    "VALUE_MIN",
    "BackupDocument",
    "CvRecord",
    "ReadStatus",
    "backup_path",
    "read_backup",
    "record_for",
    "status_for",
    "write_backup",
    "write_backup_to",
]
