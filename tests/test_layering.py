"""Mechanical guards for the four layering rules in the design document.

They are text scans, not import checks: an import check only fires once a module
is imported, and these rules must hold for code no test exercises.

Being line-oriented text scans, they cannot see a violation split across two lines,
or one assembled by string concatenation. Rule 2's four patterns are not equally
strict either: `% 256`, `>> 8` and `<< 8` match whatever the variable is called, but
the off-by-one pattern is anchored on the name `cv`, so `wire = number - 1` passes
where `wire = cv - 1` is caught. They narrow where a violation can hide, not prove
one is absent.

Every guard is written so it cannot pass by finding nothing. `_offenders` is
proved against a planted violation, and the whole-package rules assert that the
file list they scanned is non-empty. A guard that silently scans zero files is
the defect this project keeps hitting: an instrument that reports "clean" when
it is merely blind.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "railctl"

RULE_1_FORBIDDEN = (
    "ff fe",
    "ff fd",
    r"\xff\xfe",
    r"\xff\xfd",
    "cu.usbmodem",
    "baud",
    "termios",
    "socket",
)

# `tty` is deliberately NOT in the tuple above: as a bare substring it also matches
# `sys.stdout.isatty()`, which the CLI output contract requires (colour and progress
# only when that stream is a TTY, stdout and stderr tested separately), and `pretty`.
# A guard that fires on correct M6 code gets weakened or deleted, so it is anchored
# instead. `\btty` still catches `/dev/ttyUSB0` and `ttys001` - `/` and start-of-word
# are word boundaries - while `isatty` and `pretty` have no boundary before `tty`.
RULE_1_PATTERNS = (
    *(re.compile(re.escape(token), re.IGNORECASE) for token in RULE_1_FORBIDDEN),
    re.compile(r"\btty", re.IGNORECASE),
)

RULE_2_PATTERNS = (
    re.compile(r"\bcv\s*[-+]\s*1\b"),
    re.compile(r"%\s*256"),
    re.compile(r">>\s*8"),
    re.compile(r"<<\s*8"),
)

RULE_3_PATTERN = re.compile(r"^\s*class\s+\w*(?:Error|Exception|Timeout)\b", re.MULTILINE)

RULE_4_PATTERNS = (re.compile(r"/dev/"), re.compile(r"usbmodem"))


def _python_files(*relative: str) -> list[Path]:
    found: list[Path] = []
    for rel in relative:
        target = PACKAGE / rel
        if target.is_dir():
            found.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            found.append(target)
    return found


def _package_files(exclude: tuple[str, ...] = ()) -> list[Path]:
    excluded = {PACKAGE / name for name in exclude}
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if not any(path == item or item in path.parents for item in excluded)
    ]


def _offenders(files: list[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    hits: list[str] = []
    for path in files:
        label = os.path.relpath(path, REPO_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{label}:{number}: {line.strip()}")
    return hits


def test_rule_1_no_wire_vocabulary_in_station_or_cli():
    """station/ and cli/ speak in Station API terms, never in bytes or port names."""
    assert _offenders(_python_files("station", "cli"), RULE_1_PATTERNS) == []


def test_rule_2_no_cv_arithmetic_outside_xbus_cv():
    """CV numbers are 1-based in every API; xbus/cv.py is the only place they shift."""
    files = _python_files("station", "cli", "xbus/commands.py")
    assert _offenders(files, RULE_2_PATTERNS) == []


def test_rule_3_only_errors_defines_exception_types():
    files = _package_files(exclude=("errors.py",))
    assert files, "the guard scanned no files; the package layout moved"
    assert _offenders(files, (RULE_3_PATTERN,)) == []


def test_rule_4_connection_targets_are_opaque_outside_transport():
    files = _package_files(exclude=("transport",))
    assert files, "the guard scanned no files; the package layout moved"
    assert _offenders(files, RULE_4_PATTERNS) == []


def test_the_rule_1_and_2_targets_are_scanned_once_they_exist():
    """Rules 1 and 2 pass on an empty file list. This is what stops that being silent.

    Today none of these paths exists, so every branch is the `not target.exists()`
    one. Once station/, cli/ or xbus/commands.py lands, this test is what says
    whether the two guards are measuring anything or reporting green over nothing.

    It cannot catch a rename: if the facade package is called facade/ instead of
    station/, this passes and rules 1 and 2 still scan nothing. Whoever creates
    those directories under a different name must add them to the tuple below and
    to _python_files(...) in the two rule tests, in the same commit, and plant a
    canary in the new directory to watch the guard fail once.
    """
    for rel in ("station", "cli", "xbus/commands.py"):
        target = PACKAGE / rel
        assert not target.exists() or _python_files(rel), (
            f"{rel} exists but the scanner found no files in it"
        )


def test_the_scanner_reports_a_planted_violation(tmp_path: Path):
    planted = tmp_path / "leaky.py"
    planted.write_text(
        'PORT = "/dev/ttyUSB0"\n'
        'NAME = "cu.usbmodem7010A00011943"\n'
        "RAW = cv - 1\n"
        "COLOUR = sys.stdout.isatty()\n",
        encoding="utf-8",
    )
    assert len(_offenders([planted], RULE_4_PATTERNS)) == 2
    assert len(_offenders([planted], RULE_2_PATTERNS)) == 1
    assert len(_offenders([planted], (RULE_3_PATTERN,))) == 0
    # ttyUSB0 and cu.usbmodem are hits; isatty() is not. The CLI output contract
    # requires isatty(), so rule 1 must never fire on it.
    assert len(_offenders([planted], RULE_1_PATTERNS)) == 2


def test_the_scanner_reports_a_planted_exception_class(tmp_path: Path):
    planted = tmp_path / "rogue.py"
    planted.write_text("class RogueError(Exception):\n    pass\n", encoding="utf-8")
    assert len(_offenders([planted], (RULE_3_PATTERN,))) == 1
