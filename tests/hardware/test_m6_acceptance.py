# tests/hardware/test_m6_acceptance.py
"""M6 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED, AND A HUMAN WATCHING THE LAYOUT.

    uv run pytest -m hardware -s tests/hardware/test_m6_acceptance.py

Deselected by default (pyproject.toml's addopts carries -m 'not hardware'). `-s` is not
optional here: every stage stops at a gate and waits for you to press Enter, and
without `-s` pytest owns stdin, the gate cannot read it, and the whole file skips.

WHAT YOU NEED AT THE BENCH
    * the YD7010 connected over USB, nothing else driving the layout - a second
      throttle refreshing its own speeds invalidates stage 2 completely
    * locomotive 3 (the ZIMO MS450P22) on the rolling road, where you can see its
      wheels, and reachable to lift off the track
    * the decoder also readable on the PROGRAMMING track for D5-D8, or accept that
      those three capabilities come back unknown
    * the track power switched OFF before you start
    * a hand on the station's own STOP button throughout

STAGE 2 ENERGISES THE TRACK. Measured 2026-08-09 (docs/probe-results.md, "`power on`'s
stop-all was in the wrong order", runs 1 and 2): this station's start mode is automatic
and a locomotive resumes its stored speed the instant power returns. Stage 2 is the
acceptance run for the fix that holds the layout across exactly that moment (issue
#14). Watch the wheels. If anything moves, that IS the finding - write it down, stop,
and do not soften the test to make it pass.

The stages are ordered and share one temporary config directory, so run the whole file
rather than a single test. Your real ~/.config/railctl/capabilities.json is never
touched: the run writes into a temporary directory and prints the path.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app
from railctl.station import EVENT_NAMES

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = "3"
EXPECTED_XPRESSNET_VERSION = "4.0"
EXPECTED_STATION_ID = 0x12  # 18 - `63 21 40 12`, docs/probe-results.md "Settled"

runner = CliRunner()


@pytest.fixture(scope="module")
def bench_config(tmp_path_factory):
    """One config directory for the whole file, so stage 2 overwrites the entry stage 1
    wrote and the re-probe can be seen happening."""
    return tmp_path_factory.mktemp("railctl-m6")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, bench_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bench_config))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _gate(question: str) -> None:
    """Stop, say what is about to happen, and wait. One observable per stage.

    Reading stdin rather than sleeping is deliberate: a timed window means the run
    proceeds whether or not anybody was looking, and an observation nobody made is not
    an observation. Skips - never silently continues - when stdin is not a terminal,
    because that is the `-s`-less invocation and it would otherwise energise the track
    with nobody watching.
    """
    if not sys.stdin.isatty():
        pytest.skip("run with -s: this file gates every stage on a human pressing Enter")
    print(f"\n{'=' * 78}\n{question}\n{'=' * 78}")
    input("press Enter when ready, or Ctrl-C to stop here: ")


def _capabilities(bench_config) -> dict[str, object]:
    path = bench_config / "railctl" / "capabilities.json"
    print(f"\ncapabilities written to {path}")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["links"]) == 1, saved["links"]
    return next(iter(saved["links"].values()))


def _run(*argv: str):
    result = runner.invoke(app, list(argv))
    print(f"\n$ railctl {' '.join(argv)}\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def test_1_doctor_on_a_dead_track_measures_what_it_can_and_leaves_the_rest_unknown(
    bench_config,
):
    """Stage 1. Nothing moves: no `--power-on`, so the track stays as you left it.

    `railctl doctor --address 3`, with the flag AFTER the verb - that spelling is M6's
    own acceptance sentence, and it parses only because every command redeclares the
    eight global options itself.

    The assertion that matters most is the last one. With the track dead, D4 never
    sends a POM read, so `pom_read` must come back NULL. Not `false`. A capability
    nobody measured is not a capability that is absent, and this is the whole reason
    this project exists.

    D5-D8 still run: service mode needs no track power on this station (measured
    2026-08-06, four reads of CV8 with the rails dead). If those three come back null
    instead of true, the decoder is not on the programming track - that is a bench
    problem, not a railctl one.
    """
    _gate(
        "STAGE 1 of 4 - no --power-on, but the track WILL flicker.\n"
        "\n"
        "Check before you press Enter:\n"
        "  - track power OFF at the station\n"
        "  - locomotive 3 on the PROGRAMMING track. D5-D8 read CVs in service mode and\n"
        "    need it there; on the rolling road they come back null and this stage fails\n"
        "    on the bench, not on railctl. An earlier version of this line asked for both\n"
        "    places at once - it was written for a bench with a decoder on each.\n"
        "  - nothing else driving the layout\n"
        "\n"
        "This stage sends no power-on of its own, but it is NOT true that nothing is\n"
        "energised: `exit_service_mode` ends every service-mode session with `21 81`,\n"
        "which energises, before restoring the state it found. Expect the track to\n"
        "flicker several times. That is safe only because loco 3 has no stored speed -\n"
        "so if it twitches, the zero is not where we think it is. Say so."
    )
    result = _run("doctor", "--address", ACCEPTANCE_ADDRESS, "--format", "json")
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["schema"] == "railctl/doctor/v1"
    assert body["station"]["protocol_version"] == EXPECTED_XPRESSNET_VERSION
    assert body["station"]["command_station_id"] == EXPECTED_STATION_ID

    caps = body["result"]["capabilities"]
    assert caps["xpressnet_version"] == EXPECTED_XPRESSNET_VERSION
    assert caps["command_station_id"] == EXPECTED_STATION_ID
    assert caps["service_direct_cv"] is True
    assert caps["z21_cv_opcodes"] is True
    assert caps["service_ext_cv"] is True
    # Silence about the layout, because this run said nothing to it.
    assert body["result"]["layout"]["energised"] is False
    assert body["result"]["layout"]["held"] is None
    # The founding rule, end to end: never asked, so never answered.
    assert caps["pom_read"] is None, "D4 did not run, so pom_read must be null, never false"

    entry = _capabilities(bench_config)
    assert body["result"]["saved_to"] is not None
    assert entry["xpressnet_version"] == EXPECTED_XPRESSNET_VERSION
    assert entry["pom_read"] is None


def test_2_doctor_power_on_holds_the_layout_and_records_pom_read_false_from_silence(
    bench_config,
):
    """Stage 2. THIS ENERGISES THE TRACK. Watch the wheels of locomotive 3.

    Issue #14 and R1 in one run. The command energises, holds the whole layout with an
    emergency stop, and sends speed 0 to locomotive 3 before it measures anything, so
    the moment power returns is a moment nothing can move through. It then leaves the
    layout HELD and does not release it: the release is measured to be what starts
    locomotives (run 5), so a diagnostic command must not be what chooses that moment.
    Stage 4 switches the track off instead, which is how stage 1 found it.

    WHAT THE HOLD ACTUALLY DOES DURING THIS STAGE, corrected 2026-08-09. The layout is
    not held for one unbroken stretch. `exit_service_mode` ends every service-mode
    session with resume-operations, and that telegram CLEARS the emergency stop (run
    5) - so the hold drops at the end of the D5-D8 batch and again at the end of D9's
    identity reads. Each of those gaps is now closed by the exit path itself, which
    re-sends the stop before it returns, and the run makes a final re-assert and reads
    the bit back at the end. The gap is a status exchange wide, not a whole check, and
    the earlier version of this document told the watcher the layout was held for the
    whole stage while it was in fact released mid-probe and never put back.

    So: brief unheld windows are EXPECTED and are the thing to watch. Locomotive 3 was
    sent speed 0 while held, which is what makes those windows safe - the station has
    no stored speed left for it to resume (runs 6 and 7). A locomotive that twitches in
    one of those windows is a real finding: it means the speed-0 telegram did not take.

    `pom_read is False` here is the ONE place in this codebase where `false` follows
    something other than a `61 82`: D4 asked three times and got nothing at all, and
    leaving it null makes every AUTO operation retry POM for seconds on end forever.
    That exception carries its provenance, which is what the last two assertions are
    for - `"silence"`, not `"unsupported"`. A later `railctl cv read --mode pom` reads
    that field, not the bare boolean.

    WHAT NO ASSERTION HERE CAN PROVE: that the locomotive stayed still. Only you can.
    """
    _gate(
        "STAGE 2 of 4 - THIS TURNS THE TRACK ON.\n"
        "Watch locomotive 3. It must not turn a wheel at any point. Keep a hand on\n"
        "the station's STOP button.\n"
        "The hold is NOT one unbroken stretch. It is dropped and re-sent THREE times,\n"
        "and the three are not equally safe:\n"
        "\n"
        "  WINDOW 1, at the very start: D3 energises the track and then holds it. Loco\n"
        "    3 has NOT been sent speed 0 yet - the zero goes out after the hold - so if\n"
        "    it has a stored speed, this is the one window where it could legitimately\n"
        "    start. Measured 2026-08-09 the gap is about 0.51 s and this decoder's\n"
        "    acceleration curve is several seconds, so nothing was seen to move at\n"
        "    steps 15 or 80. A twitch HERE is the known hazard, not a defect - note it\n"
        "    and carry on.\n"
        "  WINDOWS 2 and 3, leaving service mode after D5-D8 and after D9: by then loco\n"
        "    3 has been sent speed 0, so it has nothing to resume. A twitch in EITHER of\n"
        "    these means the speed-0 telegram did not take. That is a finding.\n"
        "\n"
        "If you cannot tell which window a movement happened in, say so rather than\n"
        "guessing - window 1 is over within a second of the track going live.\n"
        "The layout is left HELD afterwards - stage 4 switches the track off rather\n"
        "than releasing it."
    )
    result = _run("doctor", "--address", ACCEPTANCE_ADDRESS, "--power-on", "--format", "json")
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)

    layout = body["result"]["layout"]
    assert layout["energised"] is True, "this run should have been the one that energised"
    assert layout["track_power"] is True
    assert layout["held"] is True, "the station did not confirm the hold - THIS IS THE FINDING"
    assert layout["must_leave_held"] is True, "the run has to own the hold it applied"
    assert layout["idled_address"] == int(ACCEPTANCE_ADDRESS)
    assert layout["idled"] is True
    assert body["warnings"] == [], body["warnings"]

    checks = {c["id"]: c for c in body["result"]["checks"]}
    assert checks["D3"]["status"] == "ok", checks["D3"]
    assert checks["D4"]["status"] == "ok", checks["D4"]

    caps = body["result"]["capabilities"]
    assert caps["pom_read"] is False
    assert caps["pom_read_provenance"] == "silence"
    assert caps["pom_result_channel"] == "none"
    assert caps["function_groups_4_5"] is True
    assert caps["single_function_cmd"] is True
    notes = body["result"]["notes"]
    assert any("silence" in note or "no result" in note for note in notes), notes

    entry = _capabilities(bench_config)
    assert entry["pom_read"] is False, "the re-probe should have overwritten stage 1's null"

    print(
        "\nOBSERVE AND WRITE DOWN: did locomotive 3 move at any point during this "
        "stage? A green run with a locomotive that moved is a FAILED acceptance."
    )
    _gate("STAGE 2 - confirm you watched the locomotive and it stayed still.")


def test_3_monitor_decodes_a_broadcast_and_ends_its_stream_with_a_summary():
    """Stage 3. Reads only - `monitor` sends nothing to the station at all.

    `--limit 1` ends the run on the first broadcast rather than on Ctrl-C, so this
    needs no second terminal. Press the station's own STOP button to produce one: the
    YD7010 sends `61 00` / `81 00` unsolicited, three times each (measured
    2026-08-06). That is also the safe direction - it cuts track power.

    What is being accepted here is the stream contract: one compact JSON object per
    line, sequence numbers from 0 with no gaps, and a `summary` line at the end. A
    consumer that dies mid-run has to be able to tell the run ended from the same
    stream it was reading.
    """
    _gate(
        "STAGE 3 of 4 - reads only, sends nothing.\n"
        "After you press Enter, press the station's own red STOP button once.\n"
        "\n"
        "The buttons MOVE the state, they do not set it (measured 2026-08-10): green\n"
        "always jumps to green-steady, and red degrades one step - green steady ->\n"
        "green flashing -> red. Stage 2 leaves the panel FLASHING, so one red press\n"
        "from here cuts the power and broadcasts `61 00`. From green steady the same\n"
        "press would produce an emergency stop with the voltage still on, and this\n"
        "stage would still pass on a different broadcast - so read the LED, do not\n"
        "assume which one you produced."
    )
    result = _run("monitor", "--limit", "1", "--format", "ndjson")
    assert result.exit_code == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    assert lines[-1]["type"] == "summary"
    assert lines[-1] == {
        "type": "summary",
        "sequence": 1,
        "count": 1,
        "complete": True,
        "exit_code": 0,
    }
    event = lines[0]
    assert event["type"] == "event"
    assert event["name"] in EVENT_NAMES, event
    print(f"\ndecoded broadcast: {event['name']} - {event['detail']}")


def test_4_the_bench_is_left_as_it_was_found_with_the_track_dead():
    """Stage 4. Put the bench back: track power OFF, which is how stage 1 found it.

    `power off` rather than `power resume`. The release is the telegram that starts
    locomotives (run 5), and an acceptance run must not end by choosing that moment -
    if you want the layout live afterwards, run `railctl power resume` yourself, with
    the layout in view.

    `railctl status` then has the last word: `track_power` read off the station's own
    byte, not off the fact that a telegram went out.
    """
    _gate("STAGE 4 of 4 - switching the track off and reading the state back.")
    off = _run("power", "off", "--format", "json", "--non-interactive")
    assert off.exit_code == 0, off.stderr
    assert json.loads(off.stdout)["result"]["track_power"] is False

    status = _run("status", "--format", "json")
    assert status.exit_code == 0, status.stderr
    body = json.loads(status.stdout)["result"]
    assert body["track_power"] is False
    print(f"\nfinal status byte: {body['raw_hex']}")
    print(
        "\nM6 ACCEPTANCE COMPLETE. Record in docs/probe-results.md: the four stages, "
        "whether locomotive 3 moved at any point, and the final status byte above."
    )
