# tests/hardware/test_m9_acceptance.py
"""M9 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_m9_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional: every stage stops at a gate and waits for you to press Enter, and without `-s`
pytest owns stdin, the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the ZIMO MS450P22 (locomotive 3) standing on the PROGRAMMING track with good
      wheel contact - every read here runs in service mode
    * patience: each backup reads the whole curated set one CV at a time, about
      1.7 s per CV measured 2026-08-11, so roughly TWO MINUTES per stage and about
      SEVEN MINUTES for the file. Expect the track to flicker between stages.

THIS FILE WRITES NOTHING TO THE DECODER. That is the milestone's central claim, not a
side effect: `backup` never writes a CV, not even the CV31/CV32 index selectors. If any
stage reports a write, stop and record it - that is a finding, not a retry.

WHAT EACH STAGE PROVES (one observable each, M6's rule)
    1. the probe, so service mode has proven encodings to resolve against
    2. a real backup file exists and validates against its own reader
    3. two consecutive backups of an unchanged decoder are byte-identical
    4. the NDJSON stream is contiguous and ends in a summary

Stage 3 is the one that needs the hardware. The unit tests prove the WRITER is
deterministic given identical reads; only the decoder can prove it answers identically
twice. Stage 3 also exercises the cross-invocation session gap at a scale nothing else
has: the second backup opens its first session moments after the first backup closed its
last one (docs/probe-results.md, "The session gap crosses invocations").

Your real ~/.config/railctl/capabilities.json is never touched: the run writes into a
temporary directory and prints the path. The backups also land there - the last stage
prints the command that writes a keeper copy to ~/railctl-backups.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from railctl.backup import BACKUP_SCHEMA, ReadStatus, read_backup
from railctl.cli.main import app

pytestmark = pytest.mark.hardware

ZIMO_MANUFACTURER_ID = 145
ACCEPTANCE_ADDRESS = "3"
#: Exit 9 is `backup_incomplete`, and on this bench it is the EXPECTED ending:
#: CV251-253 (the serial bytes) have never answered here. A run that exits 9
#: with only those three missing is a pass; the file is still the product.
INCOMPLETE_EXIT_CODE = 9
#: The three the bench has never got an answer from. Anything else silent is a
#: finding to record, not a known hole.
KNOWN_SILENT_CVS = frozenset({251, 252, 253})

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    """One config directory for the whole file, so the capabilities stage 1
    measures are what the later stages resolve against."""
    return tmp_path_factory.mktemp("railctl-m9")


@pytest.fixture(scope="module")
def backups(tmp_path_factory):
    """Where the stages put the files they produce, kept for the whole module
    so stage 3 can compare against what stage 2 wrote."""
    return tmp_path_factory.mktemp("railctl-m9-backups")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _require_stage_1(bench_config) -> None:
    """Stages that resolve `--mode service` need the capabilities stage 1 wrote.

    The config directory is a fresh temporary directory every invocation, so
    running one stage alone resolves against a bench with no proven encodings
    and dies with exit 18 `service_encoding_unknown` - a confusing verdict at
    the bench. Skip with the real reason instead: run the whole file.
    """
    if not (bench_config / "railctl" / "capabilities.json").exists():
        pytest.skip(
            "stage 1 has not run in this invocation, so there are no proven service-mode "
            "encodings to resolve against - run the whole file, not a single stage: "
            "uv run pytest -m hardware -s tests/hardware/test_m9_acceptance.py"
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


def _backup_to(path: Path):
    """One backup run in service mode. Returns the result; the caller decides
    what its exit code means, because 0 and 9 are both legitimate endings."""
    return _run(
        "backup",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(path),
        "--mode",
        "service",
        "--format",
        "json",
    )


def _assert_a_usable_backup(result, path: Path) -> dict:
    """The two endings a backup may have here, and the holes it may carry.

    Exit 0 means every CV answered; exit 9 means some did not and the file
    says which. Anything else - and any hole outside the three serial CVs -
    stops the run, because an unexplained hole in a file that later drives
    writes is exactly what this milestone exists to prevent.
    """
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    assert path.exists(), "a backup must leave its file behind whatever its exit code"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == BACKUP_SCHEMA
    assert document["loco"]["address"] == int(ACCEPTANCE_ADDRESS)
    rows = {row["cv"]: row for row in document["cvs"]}
    holes = {
        cv
        for cv, row in rows.items()
        if row["status"] in (ReadStatus.NO_RESPONSE.value, ReadStatus.ERROR.value)
    }
    assert holes <= KNOWN_SILENT_CVS, f"unexpected holes at {sorted(holes - KNOWN_SILENT_CVS)}"
    # Every ok row carries a value and every non-ok row carries none - the
    # file-level half of the three-valued rule, checked against real silence
    # rather than a fake's.
    for cv, row in rows.items():
        if row["status"] == ReadStatus.OK.value:
            assert isinstance(row["value"], int), row
        else:
            assert "value" not in row, f"CV{cv} is a hole and must carry no value: {row}"
    return document


def test_1_doctor_populates_the_capabilities_the_backup_resolves_against(bench_config):
    """Stage 1. The probe, so `service` mode has proven encodings to pick from."""
    _gate(
        "STAGE 1 of 4 - doctor probe, no --power-on.\n"
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


def test_2_a_real_backup_validates_against_its_own_reader(bench_config, backups):
    """Stage 2. The first real backup file this project has ever produced.

    The observable is "a file exists and its own reader accepts it" - the
    reader is strict on purpose (M10's restore drives writes off what it
    returns), so a file the reader refuses is a defect in the writer, found
    here rather than in a restore.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 2 of 4 - THE FIRST REAL BACKUP. Reads only; nothing is written\n"
        "to the decoder, not even the CV31/CV32 index selectors.\n"
        "\n"
        "This reads the whole curated set one CV at a time: expect about TWO\n"
        "MINUTES of steady work with no output until it finishes.\n"
        "\n"
        "Exit 9 is a PASS here if the only silent CVs are 251-253 (the serial\n"
        "bytes, which have never answered on this bench). The file is written\n"
        "either way, and it lists its own holes."
    )
    first = backups / "first.json"
    result = _backup_to(first)
    document = _assert_a_usable_backup(result, first)
    # The reader is the thing being accepted here, run against real output.
    parsed = read_backup(first)
    assert parsed.loco["address"] == int(ACCEPTANCE_ADDRESS)
    rows = {row["cv"]: row for row in document["cvs"]}
    assert rows[8]["value"] == ZIMO_MANUFACTURER_ID, "CV8 must read 145 on a ZIMO decoder"
    summary = document["summary"]
    print(
        f"\nbackup: {summary['ok']} ok, {summary['no_response']} no_response, "
        f"{summary['error']} error, {summary['skipped']} skipped, "
        f"complete: {summary['complete']}\nfile: {first}"
    )


def test_3_two_consecutive_backups_of_an_unchanged_decoder_are_byte_identical(
    bench_config, backups
):
    """Stage 3. THE M9 ACCEPTANCE SENTENCE, and the only claim here that the
    unit tests cannot make.

    They prove the writer is deterministic given identical reads. Only the
    decoder can prove it answers identically twice, and only the station can
    prove that a second backup opening moments after the first one closed is
    not tripped by the inter-session gap.

    `created_utc` is the one field that must differ, so it is normalised
    before the comparison; everything else - including the capabilities block
    and the encoding - must match byte for byte. A difference anywhere else
    is a finding worth recording, not a flake to rerun away.
    """
    _require_stage_1(bench_config)
    first = backups / "first.json"
    if not first.exists():
        pytest.skip("stage 2 did not produce a file to compare against")
    _gate(
        "STAGE 3 of 4 - THE SAME BACKUP AGAIN, immediately.\n"
        "\n"
        "Do not touch the decoder or the layout between stage 2 and now: the\n"
        "claim is that an UNCHANGED decoder reads the same twice.\n"
        "\n"
        "Another two minutes. This run also starts its first session moments\n"
        "after stage 2 closed its last one, which is the case that used to\n"
        "fail with 61 13 before the retry fix."
    )
    second = backups / "second.json"
    result = _backup_to(second)
    _assert_a_usable_backup(result, second)
    before = json.loads(first.read_text(encoding="utf-8"))
    after = json.loads(second.read_text(encoding="utf-8"))
    stamps = (before.pop("created_utc"), after.pop("created_utc"))
    differing = [key for key in before if before[key] != after.get(key)]
    assert not differing, f"an unchanged decoder produced different {differing}"
    assert before == after
    print(f"\nidentical apart from created_utc ({stamps[0]} -> {stamps[1]})")


def test_4_the_ndjson_stream_is_contiguous_and_ends_in_a_summary(bench_config, backups):
    """Stage 4. The streaming contract against real hardware: sequence numbers
    count up without a gap from 0, `start` is the first line, and `summary` is
    the last one whatever the run's ending."""
    _require_stage_1(bench_config)
    _gate(
        "STAGE 4 of 4 - the same backup once more, streamed as NDJSON.\n"
        "\n"
        "This one you can watch: a line appears as each CV comes back, so the\n"
        "two silent minutes become visible progress. Another two minutes."
    )
    streamed = backups / "streamed.json"
    result = _run(
        "backup",
        "--address",
        ACCEPTANCE_ADDRESS,
        "--out",
        str(streamed),
        "--mode",
        "service",
        "--format",
        "ndjson",
    )
    assert result.exit_code in (0, INCOMPLETE_EXIT_CODE), result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    assert lines[0]["type"] == "start"
    assert lines[-1]["type"] == "summary"
    assert lines[-1]["exit_code"] == result.exit_code
    cv_lines = [line for line in lines if line["type"] == "cv"]
    assert len(cv_lines) == lines[0]["total"]
    print(
        f"\nstream: {len(lines)} lines, {len(cv_lines)} of them CVs, "
        f"summary exit_code {lines[-1]['exit_code']}"
    )
    print(
        "\nM9 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the curated count, "
        "which CVs were silent, the wall-clock per backup, and that stage 3 matched.\n"
        "\nTo keep a real backup outside the temporary directory, run:\n"
        f"    uv run railctl backup --address {ACCEPTANCE_ADDRESS} --mode service "
        '--note "post-KLUG stock settings"\n'
        "which writes ~/railctl-backups/loco-0003-curated.json."
    )
