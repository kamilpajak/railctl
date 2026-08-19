# src/railctl/backup/__init__.py
"""CV backup files: the `railctl/backup/v1` writer, reader and vocabulary.

This package sits above the station facade exactly like `railctl.catalog`:
it speaks in human CV numbers (CV1 = 1) and plain integers, talks only to
`Station` results, and never to the wire layers - tests/test_layering.py
rule 5 enforces that mechanically. `types.py` and `file.py` depend on the
standard library alone, so M10's restore can read a backup with no station
attached; `mapping.py` and `plan.py` are the two modules here that name
station types, and neither of them touches a link - `plan.py` is a pure
function of a file, the live values already read, and the catalog.
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
from railctl.backup.plan import (
    ADDRESS_SET,
    STAGE_C_ORDER,
    STAGES,
    PlannedWrite,
    never_write_cvs,
    plan_restore,
)
from railctl.backup.types import (
    BACKUP_SCHEMA,
    SOURCE_CATALOG,
    SOURCE_SWEEP,
    SUMMARY_KEYS,
    BackupDocument,
    CvRecord,
    ReadStatus,
)

__all__ = [
    "ADDRESS_SET",
    "BACKUP_DIR_NAME",
    "BACKUP_SCHEMA",
    "CV_MAX",
    "CV_MIN",
    "NOT_ATTEMPTED_DETAIL",
    "SOURCE_CATALOG",
    "SOURCE_SWEEP",
    "STAGES",
    "STAGE_C_ORDER",
    "STDOUT_TARGET",
    "SUMMARY_KEYS",
    "TOP_LEVEL_KEYS",
    "VALUE_MAX",
    "VALUE_MIN",
    "BackupDocument",
    "CvRecord",
    "PlannedWrite",
    "ReadStatus",
    "backup_path",
    "never_write_cvs",
    "plan_restore",
    "read_backup",
    "record_for",
    "status_for",
    "write_backup",
    "write_backup_to",
]
