"""A bare exit-code number in `tests/` is a defect. This is the scan that says so.

Modelled on `tests/test_layering.py`, and a text scan for the same reason: it must
hold for lines no test exercises, and it must be readable at the point of the
violation rather than inferred from a failure somewhere else.

WHY IT EXISTS. Issue #65 collapsed twenty-one exit codes to eight and touched about
three hundred assertions. `RAILCTL_EXIT_CODE_CANARY=1` (see `tests/conftest.py`) is
what proves the suite is not coupled to the numbers, and it is the stronger of the
two guards - it finds a literal in a parametrize row where no pattern can, because
`[(0, 0, False), (1, 20, True)]` gives nothing away about which number is a speed
step and which is an exit code. This scan is the cheaper half: it fires at the line
that is wrong, in a normal run, before anyone thinks to set the variable.

WHAT IT DOES NOT DO. Being line-oriented it cannot see a literal split across two
lines, one built by arithmetic, or one hidden in a tuple. That is the canary's job.
These two guards overlap on purpose and neither replaces the other.

`0` is legal everywhere. Nothing folds into it, the canary never shifts it, and
`CommandResult.ok`, `SystemExit` and every `CliRunner` assertion key on it
structurally - about 230 sites, none of them a risk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"

#: The file this scan may not read. `tests/unit/test_exit_codes.py` is the SECOND
#: copy of the contract `railctl/exit_codes.py` declares, and its literals are the
#: whole point: they make an edit to the first copy show up in a diff as a contract
#: change rather than as a refactor. `tests/conftest.py` excludes the same file from
#: the canary, for the same reason, and this file excludes itself - the patterns
#: below are written out as text here.
EXEMPT: tuple[str, ...] = (
    "unit/test_exit_codes.py",
    "test_exit_code_literals.py",
)

#: The shapes an exit code is written in. Each ends at a non-zero digit, so `== 0`
#: and `"exit_code": 0` pass. The first three are assertions; the fourth catches a
#: literal handed to `typer.Exit`, which is how a test fakes a command's own ending.
PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexit_code\b[^=#\n]*==\s*[1-9]\d*"),
    re.compile(r'"exit_code":\s*[1-9]\d*'),
    re.compile(r"\.code\s*==\s*[1-9]\d*"),
    # Anchored on `raise` at the start of the line, not on `Exit(` anywhere. Several
    # docstrings in this suite explain the interrupt route by quoting `typer.Exit(130)`
    # in prose, and a guard that fires on the sentence describing the rule is a guard
    # someone weakens or deletes.
    re.compile(r"^\s*raise\s+(?:typer\.)?Exit\(\s*(?:code\s*=\s*)?[1-9]\d*\s*\)"),
)


def _scanned_files() -> list[Path]:
    return [
        path
        for path in sorted(TESTS.rglob("test_*.py"))
        if not any(str(path).endswith(name) for name in EXEMPT)
    ]


#: The one way to keep a literal. A line ending in this marker plus a reason is
#: allowed, and the reason is required - there is exactly one legitimate case, a
#: number that belongs to Click or to typer rather than to railctl, and writing it
#: as a railctl constant would claim a relationship that does not exist. An
#: exemption with no stated reason is how a marker becomes a habit.
ALLOW = re.compile(r"#\s*exit-code-literal:\s*\S")


def _offenders(files: list[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    hits: list[str] = []
    for path in files:
        label = os.path.relpath(path, REPO_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#") or ALLOW.search(line):
                continue
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{label}:{number}: {line.strip()}")
    return hits


def test_no_test_asserts_an_exit_code_as_a_bare_number():
    """Name the constant, or read it off the class with `exit_code_for`.

    A number says what the code IS; the constant says what it MEANS, and after the
    collapse most of them mean "a real failure happened" and nothing finer. A test
    that pins the number is testing the contract, which is `tests/unit/test_exit_codes.py`'s
    job and no other file's.
    """
    assert _offenders(_scanned_files(), PATTERNS) == []


def test_the_scan_reads_a_non_empty_file_list():
    """The failure mode this project keeps hitting: an instrument reporting "clean"
    when it is merely blind. A path typo in `TESTS` or an over-broad `EXEMPT` would
    otherwise leave a permanently green guard."""
    scanned = _scanned_files()
    assert len(scanned) > 50
    assert all(path.name.startswith("test_") for path in scanned)


def test_the_scanner_reports_a_planted_violation(tmp_path: Path):
    """Proved against a planted file, exactly as `test_layering.py` proves its own.

    Five violations and five legal lines, so a pattern that stopped matching and a
    pattern that started matching everything both go red here. The last violation is
    a marker with no reason after it, which must NOT buy an exemption.
    """
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "\n".join(
            [
                "assert result.exit_code == 9",
                'assert payload["exit_code"] == 14',
                "assert caught.value.code == 2",
                "raise typer.Exit(code=8)",
                "a docstring may say `typer.Exit(130)` without that being an assertion",
                "assert result.exit_code == 0",
                'assert payload["exit_code"] == 0',
                'assert envelope["code"] == "cv_verify"',
                "assert x.exit_code == 2  # exit-code-literal: Click's own, not railctl's",
                "assert x.exit_code == 2  # exit-code-literal:",
            ]
        ),
        encoding="utf-8",
    )

    hits = _offenders([planted], PATTERNS)

    assert len(hits) == 5
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 3, 4, 10]


def test_the_exempt_file_really_is_exempt_and_really_does_hold_literals(tmp_path: Path):
    """Both halves, because an exemption for a file that no longer needs one is a
    hole. `tests/unit/test_exit_codes.py` must be skipped by the scan AND must still
    be the place the literals live - if it ever stops holding them, the contract has
    no second copy and this exemption should go with it."""
    contract = TESTS / "unit" / "test_exit_codes.py"

    assert contract not in _scanned_files()
    assert _offenders([contract], PATTERNS) != []
