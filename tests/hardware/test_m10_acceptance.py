# tests/hardware/test_m10_acceptance.py
"""M10 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_m10_acceptance.py

Deselected by default; `-s` is not optional, because every stage waits at a gate.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the ZIMO MS450P22 (locomotive 3) on the PROGRAMMING track, good wheel contact
    * about 20 minutes: stage 2 reads the whole curated set (80 CVs at 6 s each,
      from the 6 s measured over 77 CVs on 2026-08-13), the rest work on a
      five-CV subset and take under a minute apiece

THIS IS THE FIRST ACCEPTANCE THAT WRITES FROM A FILE. Stage 4 changes CV3, proves
the tools notice, and puts it back through `restore`. CV3 is the acceleration rate:
a wrong value cannot move a locomotive, but a failed read-back is a finding to
record, not to retry away. The change and the restore live in one stage with a
`finally` that puts CV3 back by hand if anything between them fails, so no ending
leaves the decoder altered.

WHY A SUBSET FILE. Stage 2 takes a real 80-CV backup - that is the artifact worth
keeping, and the last stage prints the command to copy it somewhere durable. The
later stages then work from a five-CV file DERIVED from it through the project's
own writer, because a `diff` reads every CV its file names: at 6 s each, three
full-set diffs would be 24 minutes of bench time to prove something five CVs
prove just as well. The derived file keeps the parent's `decoder` block, so the
identity gate still runs against real serial bytes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from railctl.backup import BackupDocument, read_backup, write_backup_to
from railctl.cli.main import app

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = "3"
#: Five ordinary stage-A CVs from the speed curve. CV3 is the one stage 4 changes.
SUBSET_CVS = (2, 3, 4, 5, 6)
CHANGED_CV = 3
#: The value stage 4 writes, and the fallback when CV3 already holds it, so the
#: change is always a change.
CHANGED_VALUE = 20
ALTERNATE_VALUE = 21
#: Exit 9 is `backup_incomplete`; on 2026-08-13 the full set came back 77 of 77
#: (the curated set was 77 CVs then and is 80 now), so it is accepted here
#: rather than expected.
INCOMPLETE_EXIT_CODE = 9

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    return tmp_path_factory.mktemp("railctl-m10")


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    """Where the stages leave the full backup and the derived subset.

    `RAILCTL_M10_FILES` points the run at a directory that already holds
    `full.json` and `subset.json`, so a re-run after a fault in the later
    stages need not pay stage 2's eight minutes again. Stage 2 skips itself
    when it finds them; nothing else changes.
    """
    override = os.environ.get("RAILCTL_M10_FILES")
    return Path(override) if override else tmp_path_factory.mktemp("railctl-m10-files")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _require(previous: Path, what: str) -> None:
    if not previous.exists():
        pytest.skip(f"{what} did not run in this invocation - run the whole file, not one stage")


def _require_stage_1(bench_config) -> None:
    if not (bench_config / "railctl" / "capabilities.json").exists():
        pytest.skip(
            "stage 1 has not run in this invocation, so no service-mode encodings are proven - "
            "run the whole file: uv run pytest -m hardware -s tests/hardware/test_m10_acceptance.py"
        )


def _gate(question: str) -> None:
    if not sys.stdin.isatty():
        pytest.skip("run with -s: this file gates every stage on a human pressing Enter")
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _run(*argv: str):
    result = runner.invoke(app, list(argv))
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _diff(path: Path):
    return _run("diff", str(path), "--address", ACCEPTANCE_ADDRESS, "--format", "json")


def _differences(result) -> list[dict]:
    """The rows `diff` calls different.

    `differences` in the envelope is a COUNT, not a list - the rows live in
    `cvs`, and a difference is one whose action is `write`. Returning the rows
    and cross-checking them against the count makes the two prove each other:
    a count that disagrees with its own rows is a finding in itself.

    `not_read` is asserted zero separately. A CV that did not answer is not a
    difference (M10 fix F6) - but on this bench it is not expected either, so
    it must not pass silently.
    """
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)["result"]
    rows = [row for row in body["cvs"] if row["action"] == "write"]
    assert body["differences"] == len(rows), (body["differences"], rows)
    assert body["not_read"] == 0, f"a CV did not answer: {body}"
    return rows


def test_1_doctor_populates_the_capabilities(bench_config):
    """Stage 1. The probe, so service mode has proven encodings to resolve against."""
    _gate(
        "STAGE 1 of 5 - doctor probe, no --power-on.\n"
        "\n"
        "Check before you press Enter:\n"
        "  - locomotive 3 on the PROGRAMMING track, wheels making contact\n"
        "  - nothing else driving the layout"
    )
    result = _run("doctor", "--address", ACCEPTANCE_ADDRESS, "--format", "json")
    assert result.exit_code == 0, result.stderr
    caps = json.loads(result.stdout)["result"]["capabilities"]
    assert caps["service_direct_cv"] is True


def test_2_a_full_backup_becomes_the_input_and_the_keeper(bench_config, files):
    """Stage 2. The real 80-CV backup: the artifact worth keeping, and the parent
    of the subset the later stages use. Reads only; nothing is written."""
    _require_stage_1(bench_config)
    if (files / "subset.json").exists():
        pytest.skip(
            f"{files} already holds a backup and its subset (RAILCTL_M10_FILES), so this "
            f"re-run keeps them rather than spending eight minutes reading the same decoder"
        )
    _gate(
        "STAGE 2 of 5 - a full backup. Reads only, about EIGHT MINUTES with no\n"
        "output until it finishes (80 CVs at 6 s each, from the 6 s measured\n"
        "over 77 CVs on 2026-08-13).\n"
        "\n"
        "Exit 9 is a pass if some CVs stay silent; the file lists its own holes."
    )
    full = files / "full.json"
    result = _run(
        "backup",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(full),
        "--mode",
        "service",
        "--format",
        "json",
    )
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    document = read_backup(full)
    present = {record.cv for record in document.cvs}
    missing = [cv for cv in SUBSET_CVS if cv not in present]
    assert not missing, f"the subset stages need {missing}, which the backup does not carry"
    # The derived file: the parent's identity block and page, five rows. Built
    # through the project's own writer, so it is a file the reader accepts.
    subset = BackupDocument(
        created_utc=document.created_utc,
        tool=document.tool,
        note="M10 acceptance subset, derived from the full backup",
        loco=document.loco,
        catalog=document.catalog,
        set_name=document.set_name,
        mode=document.mode,
        cv_encoding=document.cv_encoding,
        page=document.page,
        speed_table_included=document.speed_table_included,
        sweep_range=document.sweep_range,
        link=document.link,
        capabilities=document.capabilities,
        decoder=document.decoder,
        cvs=tuple(r for r in document.cvs if r.cv in SUBSET_CVS),
    )
    write_backup_to(subset, files / "subset.json")
    print(f"\nfull backup: {full}\nsubset: {files / 'subset.json'}")


def test_3_diff_of_an_unchanged_decoder_reports_nothing(bench_config, files):
    """Stage 3. The baseline: nothing has touched the decoder since stage 2, so
    the comparison must find nothing. A difference here would mean the decoder
    does not read back what it just reported."""
    _require_stage_1(bench_config)
    subset = files / "subset.json"
    _require(subset, "stage 2")
    _gate(
        "STAGE 3 of 5 - diff against an UNCHANGED decoder, five CVs, under a\n"
        "minute. Reads only. Expect zero differences."
    )
    assert _differences(_diff(subset)) == []


def test_4_a_changed_cv_is_found_planned_restored_and_verified(bench_config, files):
    """Stage 4. THE MILESTONE'S SENTENCE, and the only stage that writes.

    One observable: a hand-changed CV3 is noticed by `diff`, planned as exactly
    one write by `restore --dry-run`, written back and verified by `restore`,
    and gone from the next `diff`. The `finally` puts CV3 back by hand if any
    step between the change and the restore fails, so no ending leaves the
    decoder altered.
    """
    _require_stage_1(bench_config)
    subset = files / "subset.json"
    _require(subset, "stage 2")
    original = read_backup(subset)
    filed = next(r.value for r in original.cvs if r.cv == CHANGED_CV)
    target = CHANGED_VALUE if filed != CHANGED_VALUE else ALTERNATE_VALUE
    _gate(
        f"STAGE 4 of 5 - THIS WRITES THE DECODER.\n"
        f"\n"
        f"CV{CHANGED_CV} holds {filed} in the file. The stage writes {target}, proves\n"
        f"`diff` sees exactly that one difference, proves `restore --dry-run`\n"
        f"plans exactly one write, then lets `restore` put {filed} back and verify\n"
        f"it. Pressing Enter also approves restore's own confirmation.\n"
        f"\n"
        f"CV{CHANGED_CV} is the acceleration rate: a wrong value cannot move the\n"
        f"locomotive, but a failed read-back is a finding - record it, do not\n"
        f"rerun blindly."
    )
    changed = _run("cv", "write", str(CHANGED_CV), str(target), "--yes", "--format", "json")
    assert changed.exit_code == 0, changed.stderr
    try:
        found = _differences(_diff(subset))
        assert [row["cv"] for row in found] == [CHANGED_CV], found
        assert found[0]["live_value"] == target
        assert found[0]["file_value"] == filed

        planned = _run(
            "restore",
            str(subset),
            "--dry-run",
            "--address",
            ACCEPTANCE_ADDRESS,
            "--format",
            "json",
        )
        assert planned.exit_code == 0, planned.stderr
        plan = json.loads(planned.stdout)["result"]
        writes = [row for row in plan["cvs"] if row["action"] == "write"]
        assert [row["cv"] for row in writes] == [CHANGED_CV], writes
        # A dry run reads and plans; it must have written nothing at all.
        assert (plan["written"], plan["verified"], plan["stages_completed"]) == ([], [], []), plan

        restored = _run(
            "restore",
            str(subset),
            "--yes",
            "--address",
            ACCEPTANCE_ADDRESS,
            "--format",
            "json",
        )
        assert restored.exit_code == 0, restored.stderr
        body = json.loads(restored.stdout)["result"]
        assert body["dry_run"] is False
        # `written` and `verified` carry the CV NUMBERS, so the report answers
        # which CVs changed rather than only how many. Exactly one CV was out
        # of step, so exactly one must have been written, and the same one
        # must have read back.
        assert body["written"] == [CHANGED_CV], body
        assert body["verified"] == [CHANGED_CV], body
        assert body["stages_completed"] == ["A"], body
    finally:
        # Only fires when something above failed before `restore` put the value
        # back; a green path writes the same value twice, which is harmless.
        check = _run("cv", "read", str(CHANGED_CV), "--mode", "service", "--format", "json")
        live = (
            json.loads(check.stdout)["result"]["cvs"][0]["value"] if check.exit_code == 0 else None
        )
        if live != filed:
            print(f"\nputting CV{CHANGED_CV} back to {filed} by hand (it read {live})")
            _run("cv", "write", str(CHANGED_CV), str(filed), "--yes", "--format", "json")
    assert _differences(_diff(subset)) == [], "the decoder must leave this stage as it entered it"


def test_5_two_files_compare_offline(bench_config, files):
    """Stage 5. The offline path: a file against itself is zero differences, and
    the comparison never opens a link. That it opens none is proven by unit test
    (a Station.open that raises); what this proves is that it works at all."""
    subset = files / "subset.json"
    _require(subset, "stage 2")
    _gate("STAGE 5 of 5 - offline file-to-file diff. Nothing is sent to the station.")
    result = _run("diff", str(subset), str(subset), "--format", "json")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["result"]["differences"] == 0
    print(
        "\nM10 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the stages, the CV3\n"
        "values, and that the decoder ended as it started.\n"
        "\nKeep the full backup - it is the first one this project has saved anywhere\n"
        "durable:\n"
        f"    mkdir -p ~/railctl-backups && cp {files / 'full.json'} "
        "~/railctl-backups/loco-0003-curated.json"
    )
