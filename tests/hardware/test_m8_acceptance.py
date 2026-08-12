# tests/hardware/test_m8_acceptance.py
"""M8 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_m8_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional: every stage stops at a gate and waits for you to press Enter, and without `-s`
pytest owns stdin, the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the ZIMO MS450P22 (locomotive 3) standing on the PROGRAMMING track with good
      wheel contact - every stage here runs in service mode
    * the track power state does not matter: service mode needs no track power on
      this station (measured 2026-08-06), but expect the track to FLICKER - every
      service-mode session ends with resume-operations before restoring the state
      it found

THIS FILE WRITES ONE DECODER CV: stage 3 writes CV3 (acceleration rate), reads it
back, and then writes the original value back - the restore sits in a `finally`,
so it runs even when a check between the write and the restore fails. That is the
milestone's acceptance sentence and it is the only write. If the read-back
disagrees, STOP - do not rerun until you understand why; a wrong CV3 only changes
the acceleration curve, but a verification that failed is a finding to record,
not to retry away. Expect each `cv write` to take two service-mode sessions: the
write, then the command's own independent read-back (that read-back is what
`verified: true` means).

Your real ~/.config/railctl/capabilities.json is never touched: the run writes into
a temporary directory and prints the path. Stage 1 populates it with a doctor run so
the service-mode encodings are proven before anything reads a CV - on a fresh file
`--mode auto` resolves to POM (pom_read null is not a no), and POM read on this
hardware returns nothing, so the stages that follow say `--mode service` / the
default `--track prog` explicitly. That is the documented bench workflow: probe
first, then program.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app

pytestmark = pytest.mark.hardware

ZIMO_MANUFACTURER_ID = 145
EXPECTED_XPRESSNET_VERSION = "4.0"
#: The design's own worked-session value for CV3; stage 3 falls back to 21 when
#: the decoder already holds 20, so the write always changes the value.
ACCEPTANCE_CV3_VALUE = 20
ALTERNATE_CV3_VALUE = 21

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    """One config directory for the whole file, so the capabilities stage 1
    measures are what the later stages resolve against."""
    return tmp_path_factory.mktemp("railctl-m8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _require_stage_1(bench_config) -> None:
    """Stages that resolve `--mode service` need the capabilities stage 1 wrote.

    The config directory is a fresh temporary directory every invocation, so
    running one stage alone (`-k test_3`) resolves against a bench with no
    proven encodings and dies with exit 18 `service_encoding_unknown` - a
    confusing verdict at the bench (it happened on 2026-08-12). Skip with the
    real reason instead: run the whole file, stage 1 included.
    """
    if not (bench_config / "railctl" / "capabilities.json").exists():
        pytest.skip(
            "stage 1 has not run in this invocation, so there are no proven service-mode "
            "encodings to resolve against - run the whole file, not a single stage: "
            "uv run pytest -m hardware -s tests/hardware/test_m8_acceptance.py"
        )


def _gate(question: str) -> None:
    """Stop, say what is about to happen, and wait. One observable per stage.

    Skips - never silently continues - when stdin is not a terminal: that is the
    `-s`-less invocation, and this file must not touch a decoder with nobody
    watching.
    """
    if not sys.stdin.isatty():
        pytest.skip("run with -s: this file gates every stage on a human pressing Enter")
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _run(*argv: str):
    result = runner.invoke(app, list(argv))
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _read_one(cv: int) -> int:
    """Read a single CV in service mode and return its value, asserting the
    three-valued contract on the way: `ok` rows carry a value, and only they do."""
    result = _run("cv", "read", str(cv), "--mode", "service", "--format", "json")
    assert result.exit_code == 0, result.stderr
    row = json.loads(result.stdout)["result"]["cvs"][0]
    assert row["cv"] == cv
    assert row["status"] == "ok", row
    assert isinstance(row["value"], int), row
    return row["value"]


def test_1_doctor_populates_the_capabilities_the_cv_commands_resolve_against(bench_config):
    """Stage 1. The probe, so `service` mode has proven encodings to pick from.

    No `--power-on`: nothing here needs the main track. D5-D8 need the decoder on
    the programming track; if the service encodings come back null instead of
    true, that is a bench problem (decoder not on the programming track), not a
    railctl one - stop and fix the bench.
    """
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
    result = _run("doctor", "--address", "3", "--format", "json")
    assert result.exit_code == 0, result.stderr
    caps = json.loads(result.stdout)["result"]["capabilities"]
    assert caps["xpressnet_version"] == EXPECTED_XPRESSNET_VERSION
    assert caps["service_direct_cv"] is True
    assert caps["z21_cv_opcodes"] is True
    print(f"\ncapabilities written under {bench_config}")


def test_2_reading_the_identity_cvs_returns_plausible_values(bench_config):
    """Stage 2. Reads only: CV1, CV3, CV8, CV29 in one batch.

    Plausibility, not just presence: CV8 is the one value this bench KNOWS
    (a ZIMO decoder answers 145 - the same fact the placement-test guidance
    leans on), CV1 must be a short address, and CV3/CV29 must be bytes. The
    batch must come back complete - a `no_response` row here means wheel
    contact, and the warning on stderr should say exactly that.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 2 of 4 - reading CV1, CV3, CV8, CV29 in service mode.\n"
        "Reads only; nothing is written. Expect a few seconds per CV."
    )
    result = _run("cv", "read", "1", "3", "8", "29", "--mode", "service", "--format", "json")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "railctl/cv-read/v1"
    assert payload["result"]["requested"] == 4
    assert payload["result"]["ok"] == 4, payload["result"]
    rows = {row["cv"]: row for row in payload["result"]["cvs"]}
    assert rows[8]["value"] == ZIMO_MANUFACTURER_ID, "CV8 must read 145 on a ZIMO decoder"
    assert 1 <= rows[1]["value"] <= 127, "CV1 must be a short address"
    assert 0 <= rows[3]["value"] <= 255
    assert 0 <= rows[29]["value"] <= 255
    print(
        f"\nplausible: CV1={rows[1]['value']} CV3={rows[3]['value']} "
        f"CV8={rows[8]['value']} CV29={rows[29]['value']}"
    )


def test_3_writing_cv3_and_reading_it_back_agrees_then_restores_the_original(bench_config):
    """Stage 3. THE ONE WRITE: CV3, verified by read-back, then put back.

    One observable: "a verified write, restored" (M6's rule - the reads exist
    to serve the write, not as observables of their own). Three properties an
    earlier version of this stage got wrong, all corrected here:

    * NOTHING touches the station before the gate - the pre-read of CV3 runs
      after you press Enter, so the values are printed then, not promised in
      the gate text.
    * `verified: true` is now a real claim: since the verify fix, `cv write`
      performs its own independent `cv read` after the write and only reports
      true when it agreed. The explicit `_read_one(3)` afterwards is the
      second witness, measured by a separate invocation.
    * The restore is in a `finally`, so a failing assertion between the write
      and the restore cannot leave CV3 changed - the file's own "writes the
      original value back" promise held only on the green path before. The
      finally PRINTS what it restored and whether the restore verified (an
      assert there could mask the original failure); the green-path asserts
      on the restore run after the try block.
    """
    _require_stage_1(bench_config)
    _gate(
        "STAGE 3 of 4 - THIS WRITES CV3.\n"
        "The stage reads CV3, writes a different value (20, or 21 if CV3 is\n"
        "already 20), reads it back, and then writes the original value back in\n"
        "a finally - the restore runs even if a check in the middle fails.\n"
        "CV3 is the acceleration rate - a wrong value cannot move the\n"
        "locomotive, but a failed read-back is a finding: record it, do not\n"
        "rerun blindly."
    )
    original = _read_one(3)
    target = ACCEPTANCE_CV3_VALUE if original != ACCEPTANCE_CV3_VALUE else ALTERNATE_CV3_VALUE
    print(f"\nCV3 = {original}; writing {target}, restoring {original} afterwards")
    try:
        written = _run("cv", "write", "3", str(target), "--format", "json")
        assert written.exit_code == 0, written.stderr
        body = json.loads(written.stdout)
        assert body["schema"] == "railctl/cv-write/v1"
        assert body["result"]["verified"] is True, body["result"]
        assert _read_one(3) == target, "the independent read-back must agree with the write"
    finally:
        restored = _run("cv", "write", "3", str(original), "--format", "json")
        restore_verified: object = "unknown"
        if restored.exit_code == 0 and restored.stdout.strip():
            restore_verified = json.loads(restored.stdout)["result"]["verified"]
        print(
            f"\nrestore: wrote CV3 = {original} back "
            f"(exit {restored.exit_code}, verified: {restore_verified})"
        )
    assert restored.exit_code == 0, restored.stderr
    assert json.loads(restored.stdout)["result"]["verified"] is True
    assert _read_one(3) == original, "the decoder must leave this stage as it entered it"
    print(f"\nCV3: {original} -> {target} -> {original}, each step verified")


def test_4_a_cv_above_the_mode_bound_exits_15_with_the_doctor_suggestion():
    """Stage 4. The refusal half of the acceptance: CV1025 is above the bound of
    every mode, exits 15 naming the bound, and suggests `railctl doctor`.

    This stage touches no hardware - the refusal must come before any telegram,
    which is itself part of what is being accepted.
    """
    _gate("STAGE 4 of 4 - refusal check, nothing is sent to the station.")
    result = _run("cv", "read", "1025", "--format", "json")
    assert result.exit_code == 15, result.stderr
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["code"] == "cv_out_of_range"
    assert "1..1024" in envelope["message"]
    assert ["railctl", "doctor"] in envelope["suggestions"]
    print(
        "\nM8 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the four stages, "
        "the CV values read, and that CV3 was restored to its original value."
    )
