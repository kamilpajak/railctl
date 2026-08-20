# tests/hardware/test_issue13_acceptance.py
"""Issue #13 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN AT THE BENCH.

    uv run pytest -m hardware -s tests/hardware/test_issue13_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional: every stage stops at a gate and waits for you to press Enter, and without `-s`
pytest owns stdin, the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout
    * the track LIVE and NOTHING HELD, which is status 0x04. D13 refuses to measure
      from any other state and says which half of that it found. Getting there:

          LED green steady   -> already right, `railctl status` to confirm 0x04
          LED green flashing -> `railctl power resume`
          LED red            -> `railctl power on` AND THEN `railctl power resume`

      Both commands, not just the first: `power on` energises the track and then
      holds it (measured 2026-08-09, "`power on`'s stop-all was in the wrong
      order"), so it leaves status 0x05 - live, with the emergency-stop bit set.
      That is a disputed bit already set, and D13 refuses on it. The release is
      the second half and it is the step that can start a locomotive, which is
      why the next bullet is not optional
    * NOTHING STANDING ON THE MAIN TRACK THAT CAN RUN AWAY. Stage 2 holds the whole
      layout with an emergency stop and then releases it, and a release is when a
      locomotive with a stored speed starts moving (measured 2026-08-09, run 5)
    * about two minutes

WHAT THIS PROVES
The bit order the tool applies is a MEASUREMENT of the attached station, not a constant
compiled into it. Two stages, one observable each:

    1. a plain `railctl doctor` does not measure it and does not touch the layout -
       D13 reads `skip` and `status_bit_order` stays null, which is "nobody asked",
       never "this station uses the default"
    2. `railctl doctor --measure-status-bits` records `lenz_23151`, matching the
       measurement made by hand on 2026-08-05 (docs/probe-results.md, "Status byte:
       bits 0 and 1 are the reverse of the Lenz spec")

Stage 2 is the acceptance sentence. `lenz_23151` means bit 0 is emergency stop and bit
1 is emergency off - the German 23151 order, the reverse of Lenz XpressNet 2.1.7 and of
what JMRI implements. If this run records `lenz_spec` instead, do not edit the default:
that is a station behaving differently from the one this project was built on, and it
is the finding.

WATCH THE FRONT-PANEL TRACK OUT LED during stage 2. It should go green FLASHING
(emergency stop, track voltage ON) for a moment and then back to green steady. Red at
any point means the track power dropped, which is not what `80 80` is supposed to do -
record it.

THIS FILE WRITES NOTHING TO THE DECODER. It touches no CV at all.

Your real ~/.config/railctl/capabilities.json is never touched: the run writes into a
temporary directory and prints the path.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app

pytestmark = pytest.mark.hardware

#: The order measured by hand on 2026-08-05 and the answer this bench must give.
EXPECTED_ORDER = "lenz_23151"

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    return tmp_path_factory.mktemp("railctl-issue13")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _gate(question: str, config: pytest.Config) -> None:
    """Stop until the operator confirms. Skips rather than guesses when it cannot ask.

    Two different things make stdin unanswerable here and the message has to say which,
    because one looks exactly like the other and the remedies are opposite. Without `-s`
    pytest has captured stdin and the fix is the flag. WITH `-s`, but launched from
    something that is not a terminal - an agent shell, a CI job, a pipe - the flag is
    already there and no flag will help: what this gate wants is a person looking at the
    track, and there is nobody on the other end of a pipe to look.

    The first is read off pytest's own `capture` option rather than sniffed off
    `sys.stdin`. Sniffing was the first attempt and it was wrong: pytest replaces stdin
    with a `DontReadFromInput` that carries a `buffer` attribute like a real stream, so
    every duck-typed test for "is this captured" answers no while it is captured.
    """
    if config.getoption("capture") != "no":
        pytest.skip("run with -s: pytest has captured stdin and the gate cannot read it")
    if not sys.stdin.isatty():
        pytest.skip(
            "stdin is not a terminal, so nobody can answer the gate - `-s` does not help "
            "here. Run this file from a real terminal: every stage waits for a human to "
            "confirm the track before it moves the layout"
        )
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _run(*argv: str, **kwargs):
    result = runner.invoke(app, list(argv), **kwargs)
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _check(payload: dict, check_id: str) -> dict:
    return next(check for check in payload["result"]["checks"] if check["id"] == check_id)


def test_1_a_plain_doctor_run_does_not_measure_the_order_and_does_not_hold_the_layout(
    bench_config, pytestconfig
):
    """Stage 1. The default, which must leave the layout exactly as it found it.

    `null` is the whole point of the field: a station nobody asked is not a station
    that uses the default. If this stage records an order, the flag is not gating
    what it says it gates.
    """
    _gate(
        "STAGE 1 of 2 - plain doctor, no --measure-status-bits.\n"
        "\n"
        "Check before you press Enter:\n"
        "  - the Track Out LED is GREEN STEADY (live, nothing held)\n"
        "  - nothing on the main track that could run away\n"
        "\n"
        "The layout must not so much as twitch during this stage.",
        pytestconfig,
    )
    result = _run("doctor", "--no-programming-track", "--format", "json")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["capabilities"]["status_bit_order"] is None
    d13 = _check(payload, "D13")
    assert d13["status"] == "skip"
    print(f"\nD13: {d13['detail']}")


def test_2_the_measured_order_is_the_one_the_led_showed_on_2026_08_05(bench_config, pytestconfig):
    """Stage 2. THE ISSUE #13 ACCEPTANCE SENTENCE.

    One emergency stop, one status read, one resume. The bit that moves is the
    emergency-stop bit, and which bit that is is the whole measurement - Lenz
    XpressNet 2.2.4 is what makes it decisive, because it says the DCC track power
    remains switched on through `80 80`, so the LED stays green while the bit is set.
    """
    _gate(
        "STAGE 2 of 2 - doctor --measure-status-bits.\n"
        "\n"
        "THIS HOLDS THE WHOLE LAYOUT with an emergency stop and then releases it.\n"
        "A locomotive with a speed stored WILL start moving on the release.\n"
        "\n"
        "Check before you press Enter:\n"
        "  - the Track Out LED is GREEN STEADY\n"
        "  - nothing on the main track that could run away\n"
        "\n"
        "WATCH THE LED: green steady -> green FLASHING -> green steady.\n"
        "Red at any point is a finding, not a retry.",
        pytestconfig,
    )
    result = _run("doctor", "--measure-status-bits", "--no-programming-track", "--format", "json")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    d13 = _check(payload, "D13")
    print(f"\nD13: {d13['status']}: {d13['detail']}")
    assert d13["status"] == "ok", d13["detail"]
    assert payload["result"]["capabilities"]["status_bit_order"] == EXPECTED_ORDER, (
        "this station answered with the other bit order; do not change the default - "
        "record what it did and why"
    )
    # The layout has to come back out released. `held` is read off the station's own
    # bit at the end of the run, never off the fact that a resume was sent.
    layout = payload["result"]["layout"]
    assert layout["held"] is not True, layout
    print(f"\nlayout on the way out: {layout}")
    print(f"capabilities written under {bench_config}")
