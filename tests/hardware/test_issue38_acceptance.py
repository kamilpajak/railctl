# tests/hardware/test_issue38_acceptance.py
"""Issue #38 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_issue38_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional: every stage stops at a gate and waits for you to press Enter, and without `-s`
pytest owns stdin, the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the ZIMO MS450P22 (locomotive 3) standing on the PROGRAMMING track with good
      wheel contact - every read here runs in service mode
    * about ten minutes of machine time, plus however long you take at the gates

WHAT THIS PROVES
Reading several CVs inside one service-mode session, instead of one session per CV,
makes the backup faster WITHOUT changing what it reads. Two claims, and the second one
is the one worth the bench time:

    1. the cost per CV drops well below the 6.05 s measured on 2026-08-13
    2. the file is the same file - every CV, every value, every status identical to
       the run that produced ~/railctl-backups/loco-0003-curated.json before the change

A faster backup that reads something different is not a faster backup. Claim 2 is what
tells the two apart, and only the decoder can make it.

The group size is 8 CVs, which holds one session open for about 25 s. Four CVs in one
session is the largest number this hardware has ever been asked for (issue #22), so
stage 2 is also the first evidence that a longer session answers to its last CV. A group
that starts answering `61 13` partway through would show up as holes clustered at the
end of each group of eight.

THIS FILE WRITES NOTHING TO THE DECODER. If any stage reports a write, stop and record
it - that is a finding, not a retry.

Your real ~/.config/railctl/capabilities.json is never touched: the run writes into a
temporary directory and prints the path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from railctl.backup import BACKUP_SCHEMA, ReadStatus, read_backup
from railctl.cli.main import app
from railctl.station.programming import SERVICE_BATCH_SIZE

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = "3"
#: Exit 9 is `backup_incomplete`, a legitimate ending: the file is the product
#: either way and it lists its own holes.
INCOMPLETE_EXIT_CODE = 9
#: Measured 2026-08-13 on this bench: 77 CVs in 466 s, one session per CV.
BASELINE_SECONDS_PER_CV = 6.05
#: The gap is 3.0 s per SESSION and the read itself costs about 3.0 s. With
#: groups of eight the gap is amortised over eight reads, so the arithmetic
#: says about 3.4 s per CV. The gate is deliberately loose - this asserts that
#: the change did something, not that it hit a predicted number. The number
#: that matters is the one the stage prints.
MAX_SECONDS_PER_CV = 5.0
#: The keeper backup taken on 2026-08-13, before this change, with the same
#: decoder on the same bench. Override with RAILCTL_BASELINE_BACKUP; the
#: comparison skips if the file is not there.
BASELINE_BACKUP = Path(
    os.environ.get("RAILCTL_BASELINE_BACKUP", "~/railctl-backups/loco-0003-curated.json")
).expanduser()
#: Fields that MUST differ or MAY differ between two runs, so they are dropped
#: before the comparison. Everything else has to match.
VOLATILE_KEYS = ("created_utc", "note")

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    """One config directory for the whole file, so the capabilities stage 1
    measures are what the later stages resolve against."""
    return tmp_path_factory.mktemp("railctl-38")


@pytest.fixture(scope="module")
def backups(tmp_path_factory):
    return tmp_path_factory.mktemp("railctl-38-backups")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _require_stage_1(bench_config) -> None:
    """Stages that resolve `--mode service` need the capabilities stage 1 wrote.

    The config directory is a fresh temporary directory every invocation, so a
    single stage run alone resolves against a bench with no proven encodings
    and dies with exit 18 `service_encoding_unknown` - a confusing verdict at
    the bench. Skip with the real reason instead.
    """
    if not (bench_config / "railctl" / "capabilities.json").exists():
        pytest.skip(
            "stage 1 has not run in this invocation, so there are no proven service-mode "
            "encodings to resolve against - run the whole file, not a single stage: "
            "uv run pytest -m hardware -s tests/hardware/test_issue38_acceptance.py"
        )


def _gate(question: str) -> None:
    """Stop, say what is about to happen, and wait. One observable per stage."""
    if not sys.stdin.isatty():
        pytest.skip("run with -s: this file gates every stage on a human pressing Enter")
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _run(*argv: str):
    result = runner.invoke(app, list(argv))
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _timed_backup(path: Path, *, fmt: str = "json"):
    """One backup run in service mode, with the wall clock around it."""
    start = time.monotonic()
    result = _run(
        "backup",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(path),
        "--mode",
        "service",
        "--format",
        fmt,
    )
    return result, time.monotonic() - start


def _document(result, path: Path) -> dict:
    """The file a backup left behind, whichever of its two endings it took."""
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    assert path.exists(), "a backup must leave its file behind whatever its exit code"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == BACKUP_SCHEMA
    return document


def _comparable(document: dict) -> dict:
    body = dict(document)
    for key in VOLATILE_KEYS:
        body.pop(key, None)
    return body


def test_1_doctor_populates_the_capabilities_the_backup_resolves_against(bench_config):
    """Stage 1. The probe, so `service` mode has proven encodings to pick from."""
    _gate(
        "STAGE 1 of 3 - doctor probe, no --power-on.\n"
        "\n"
        "Check before you press Enter:\n"
        "  - locomotive 3 on the PROGRAMMING track, wheels making contact\n"
        "  - nothing else driving the layout\n"
        "\n"
        "Expect the track to flicker: every service-mode session ends with\n"
        "resume-operations before restoring the state it found."
    )
    result = _run("doctor", "--address", ACCEPTANCE_ADDRESS, "--format", "json")
    assert result.exit_code == 0, result.stderr
    caps = json.loads(result.stdout)["result"]["capabilities"]
    assert caps["service_direct_cv"] is True
    print(f"\ncapabilities written under {bench_config}")


def test_2_the_grouped_backup_costs_less_per_cv_and_reads_the_same_file(bench_config, backups):
    """Stage 2. THE ACCEPTANCE SENTENCE: faster, and the same file.

    The timing is the point of the change and the file identity is the proof
    that the speed was not bought with anything. A run that comes back quicker
    with new holes has not made the backup faster, it has made it worse, and
    only the decoder can tell those two apart.
    """
    _require_stage_1(bench_config)
    _gate(
        f"STAGE 2 of 3 - A FULL CURATED BACKUP, TIMED. Reads only; nothing is\n"
        f"written to the decoder, not even the CV31/CV32 index selectors.\n"
        "\n"
        f"CVs are now read {SERVICE_BATCH_SIZE} to a session instead of one to a\n"
        f"session. Before the change this took about 8 minutes (6.05 s per CV,\n"
        f"measured 2026-08-13); expect roughly 4 to 5 now, with no output until\n"
        f"it finishes.\n"
        "\n"
        "Exit 9 is a PASS here if the only silent CVs are the same ones the\n"
        "baseline file records as silent."
    )
    grouped = backups / "grouped.json"
    result, elapsed = _timed_backup(grouped)
    document = _document(result, grouped)
    rows = document["cvs"]
    per_cv = elapsed / len(rows)
    print(
        f"\ngrouped backup: {len(rows)} CVs in {elapsed:.1f} s = {per_cv:.2f} s per CV\n"
        f"baseline 2026-08-13: {BASELINE_SECONDS_PER_CV} s per CV\n"
        f"summary: {document['summary']}"
    )
    # The reader is strict, and a file it refuses is a defect in the writer.
    assert read_backup(grouped).loco["address"] == int(ACCEPTANCE_ADDRESS)
    if not BASELINE_BACKUP.exists():
        pytest.skip(
            f"no baseline backup at {BASELINE_BACKUP} to compare against - the timing "
            f"above stands, the identity claim is unproven this run"
        )
    baseline = json.loads(BASELINE_BACKUP.read_text(encoding="utf-8"))
    before, after = _comparable(baseline), _comparable(document)
    differing = [key for key in before if before[key] != after.get(key)]
    assert not differing, (
        f"the grouped backup differs from {BASELINE_BACKUP} in {differing} - "
        f"reading CVs in groups must not change WHAT is read"
    )
    assert per_cv < MAX_SECONDS_PER_CV, (
        f"{per_cv:.2f} s per CV is no better than the {BASELINE_SECONDS_PER_CV} s "
        f"baseline; the grouping did not take effect"
    )


def test_3_the_stream_stays_contiguous_and_no_session_failed_to_close(bench_config, backups):
    """Stage 3. The streaming contract, and the new failure the change can have.

    A group holds the session open for about 25 s, which nothing on this bench
    has done before. Two things would show it going wrong: holes clustered at
    the end of each group of eight, meaning the decoder stopped answering
    partway through a long session, and a `service.session_close_failed`
    event, meaning the station would not leave service mode afterwards.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 3 of 3 - the same backup once more, streamed as NDJSON.\n"
        "\n"
        "This one you can watch: a line appears as each CV comes back. What to\n"
        "look for is the RHYTHM - eight CVs in quick succession, then a pause\n"
        "of about three seconds before the next eight."
    )
    streamed = backups / "streamed.json"
    result, elapsed = _timed_backup(streamed, fmt="ndjson")
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    assert lines[0]["type"] == "start"
    assert lines[-1]["type"] == "summary"
    cv_lines = [line for line in lines if line["type"] == "cv"]
    assert len(cv_lines) == lines[0]["total"]
    events = [line for line in lines if line["type"] == "event"]
    closed_badly = [line for line in events if line["name"] == "service.session_close_failed"]
    assert not closed_badly, (
        f"a group read its CVs and then could not leave service mode: {closed_badly}"
    )
    silent = [line["cv"] for line in cv_lines if line.get("status") != ReadStatus.OK.value]
    print(
        f"\nstream: {len(lines)} lines, {len(cv_lines)} CVs in {elapsed:.1f} s, "
        f"{len(events)} events\nsilent CVs: {silent or 'none'}"
    )
    print(
        "\nISSUE #38 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the seconds\n"
        "per CV before and after, whether the file matched the baseline, whether any\n"
        f"group of {SERVICE_BATCH_SIZE} lost the decoder partway through, and whether any\n"
        "session failed to close."
    )
