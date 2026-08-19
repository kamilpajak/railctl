# tests/hardware/test_m11_acceptance.py
"""M11 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_m11_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional: every stage gates on a human pressing Enter, and without `-s` pytest owns stdin,
the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the ZIMO MS450P22 (locomotive 3) standing on the PROGRAMMING track with good
      wheel contact - every read here runs in service mode
    * **about 45 minutes**, most of it stage 3. This is the longest run in the project:
      a full sweep reads every CV from 1 to 1024, and at the rate measured on
      2026-08-19 that is 30 to 45 minutes of steady work.

WHAT EACH STAGE PROVES (one observable each)
    1. the probe, so service mode has proven encodings to resolve against
    2. the sweep refuses to start unasked, and says how long it would take
    3. a full sweep completes, and every CV the curated backup already knows reads
       the same in it

Stage 3 is the milestone's sentence. The count of unreadable CVs is a RESULT, not a
failure: most CV numbers are not implemented in any decoder, and on this hardware
silence cannot be told from "this CV does not exist". Exit 9 is the expected ending.

THIS FILE WRITES NOTHING TO THE DECODER. A sweep is reads only, and it never writes the
CV31/CV32 index selectors - if any stage reports a write, stop and record it.

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
from railctl.cli.commands._sweep import HIGHEST_EXERCISED_CV, SWEEP_SET_NAME
from railctl.cli.main import app

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = "3"
#: Exit 9 is `backup_incomplete` and it is what a sweep normally ends with.
INCOMPLETE_EXIT_CODE = 9
#: Exit 2 is `confirmation_required` - what a long sweep does when stdin cannot answer.
USAGE_EXIT_CODE = 2
#: The bound a station with the Z21 CV opcodes proven should reach.
EXPECTED_BOUND = 1024
#: The keeper curated backup, taken before the sweep existed. Stage 3 checks the sweep
#: against it: every CV that file read, this one must read the same. Override with
#: RAILCTL_BASELINE_BACKUP; the comparison skips if the file is not there.
BASELINE_BACKUP = Path(
    os.environ.get("RAILCTL_BASELINE_BACKUP", "~/railctl-backups/loco-0003-curated.json")
).expanduser()

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    return tmp_path_factory.mktemp("railctl-m11")


@pytest.fixture(scope="module")
def backups(tmp_path_factory):
    return tmp_path_factory.mktemp("railctl-m11-backups")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _require_stage_1(bench_config) -> None:
    """Stages that resolve `--mode service` need the capabilities stage 1 wrote."""
    if not (bench_config / "railctl" / "capabilities.json").exists():
        pytest.skip(
            "stage 1 has not run in this invocation, so there are no proven service-mode "
            "encodings to resolve against - run the whole file, not a single stage: "
            "uv run pytest -m hardware -s tests/hardware/test_m11_acceptance.py"
        )


def _gate(question: str) -> None:
    if not sys.stdin.isatty():
        pytest.skip("run with -s: this file gates every stage on a human pressing Enter")
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _run(*argv: str, **kwargs):
    result = runner.invoke(app, list(argv), **kwargs)
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _stderr_envelope(result) -> dict:
    """stderr is the mixed stream - notices ride above the error object, which is
    its last line. Same reader `tests/cli/test_cv.py` uses."""
    return json.loads(result.stderr.strip().splitlines()[-1])


def test_1_doctor_populates_the_capabilities_the_sweep_resolves_against(bench_config):
    """Stage 1. The probe, so the sweep's bound comes from measurement."""
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
    # The bound the sweep will take comes from this one being a measured yes.
    assert caps["z21_cv_opcodes"] is True
    print(f"\ncapabilities written under {bench_config}")


def test_2_a_long_sweep_refuses_to_start_unasked_and_says_how_long(bench_config, backups):
    """Stage 2. The confirmation, which costs nothing but decides everything.

    A sweep of 1024 CVs is the longest thing this tool does, and it must not
    start because someone typed `--all` without reading it. With stdin unable
    to answer, the run refuses rather than blocking - a sweep launched from a
    script with no terminal would otherwise wait forever - and the refusal
    carries the estimate and a command that runs.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 2 of 3 - the refusal. This opens the port, resolves the bound and\n"
        "then STOPS. It reads no CVs and takes a few seconds."
    )
    refused = backups / "never-written.json"
    result = _run(
        "backup",
        "--all",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(refused),
        "--mode",
        "service",
        "--format",
        "json",
        input="",
    )
    assert result.exit_code == USAGE_EXIT_CODE, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    assert not refused.exists(), "a refused sweep must not leave a file behind"
    # The suggestion has to be a command that RUNS, not a sentence to parse.
    assert any("--yes" in argv for argv in envelope["suggestions"]), envelope["suggestions"]
    print(f"\nrefusal: {envelope['message']}\nsuggestions: {envelope['suggestions']}")


def test_3_a_full_sweep_completes_and_agrees_with_the_curated_backup(bench_config, backups):
    """Stage 3. THE M11 ACCEPTANCE SENTENCE.

    Two claims in one run. The sweep completes over the whole measured bound,
    with a recorded wall clock and a recorded count of CVs that did not answer.
    And every CV the curated backup already reads, the sweep reads the same:
    the wider net must not change what the known CVs say, which is the only
    way to tell a working sweep from one that is reading off by an index.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 3 of 3 - THE FULL SWEEP. Reads only; nothing is written to the\n"
        "decoder, not even the CV31/CV32 index selectors.\n"
        "\n"
        "CV1 to CV1024, about 30 to 45 MINUTES. Progress appears on stderr every\n"
        "32 CVs with a revised estimate after the first ten. Do not touch the\n"
        "layout while it runs.\n"
        "\n"
        "EXIT 9 IS THE PASS HERE. Most CV numbers are not implemented in any\n"
        "decoder, and this hardware cannot tell that from silence, so hundreds\n"
        "of rows will read no_response. The file is the product."
    )
    swept = backups / "sweep.json"
    start = time.monotonic()
    result = _run(
        "backup",
        "--all",
        "--yes",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(swept),
        "--mode",
        "service",
        "--format",
        "json",
    )
    elapsed = time.monotonic() - start
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    assert swept.exists(), "a sweep must leave its file behind whatever its exit code"
    payload = json.loads(result.stdout)
    document = json.loads(swept.read_text(encoding="utf-8"))
    assert document["schema"] == BACKUP_SCHEMA
    assert document["set"] == SWEEP_SET_NAME
    assert document["sweep_range"] == [1, EXPECTED_BOUND]
    rows = {row["cv"]: row for row in document["cvs"]}
    assert len(rows) == EXPECTED_BOUND, "every CV in the range needs a row of its own"
    summary = document["summary"]
    answered = sorted(cv for cv, row in rows.items() if row["status"] == ReadStatus.OK.value)
    print(
        f"\nsweep: {len(rows)} CVs in {elapsed / 60:.1f} min "
        f"({elapsed / len(rows):.2f} s per CV)\n"
        f"summary: {summary}\n"
        f"answered above CV{HIGHEST_EXERCISED_CV}: "
        f"{[cv for cv in answered if cv > HIGHEST_EXERCISED_CV] or 'none'}"
    )
    # The claim about the unexercised range is published, not just printed.
    warning = next((w for w in payload["warnings"] if w["name"] == "sweep.unexercised_range"), None)
    assert warning is not None, "a sweep past CV511 must say nothing has been read there"
    assert warning["details"]["to"] == EXPECTED_BOUND
    # The reader is strict, and a file it refuses is a defect in the writer.
    assert read_backup(swept).loco["address"] == int(ACCEPTANCE_ADDRESS)

    if not BASELINE_BACKUP.exists():
        pytest.skip(
            f"no curated baseline at {BASELINE_BACKUP} to compare against - the sweep "
            f"above stands, the agreement claim is unproven this run"
        )
    baseline = {row["cv"]: row for row in json.loads(BASELINE_BACKUP.read_text())["cvs"]}
    disagreeing = {
        cv: (row.get("value"), rows[cv].get("value"))
        for cv, row in baseline.items()
        if row["status"] == ReadStatus.OK.value and rows[cv].get("value") != row.get("value")
    }
    assert not disagreeing, (
        f"the sweep read these CVs differently from {BASELINE_BACKUP.name} "
        f"(curated, swept): {disagreeing}"
    )
    named = [cv for cv in baseline if rows[cv]["name"] == baseline[cv]["name"]]
    print(
        f"\nagreement: {len(baseline)} curated CVs, {len(named)} keeping their catalog name, "
        f"no value disagreed\n"
        "\nM11 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the wall clock, the "
        "seconds per CV, how many CVs answered, whether anything above CV511 answered at "
        "all, and that the curated values were unchanged."
    )
