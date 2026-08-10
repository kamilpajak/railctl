# tests/cli/test_doctor.py
"""Pins the `doctor` command's rendering contract and, in the first test, the
`report_for` contract that a future `cv read --mode pom` (Plan 4) depends on: a station
that answered nothing at all must never read as "not supported". The contrasting case -
a `pom_read=false` conclusion naming where it came from - is Plan 4's own test (a station
that answers "no" must say so from a message *built* in the station layer, not from a
message this test writes for itself and then asserts against).
"""

from __future__ import annotations

import io
import json
from dataclasses import replace

from typer.testing import CliRunner

from railctl.cli._errors import report_for
from railctl.cli.commands import doctor
from railctl.cli.commands.doctor import (
    CAPABILITIES_NOT_SAVED_WARNING,
    DOCTOR_SCHEMA,
    build_doctor,
)
from railctl.cli.deps import HELD_LINES, RESUME_COMMAND
from railctl.cli.main import app as real_app
from railctl.cli.render import render
from railctl.cli.result import PARTIAL_EXIT_CODE, LinkInfo, StationInfo
from railctl.errors import DecoderNotRespondingError
from railctl.station import (
    UNKNOWN_IDENTITY,
    Capabilities,
    Check,
    DoctorReport,
    LayoutState,
    verdict_lines,
)


def test_decoder_not_responding_never_says_unsupported():
    """R1 (docs/probe-results.md): the station ACKs a POM read and returns nothing at
    all - no `61 13`, no `61 82`, no value. That is UNKNOWN, never a negative answer,
    and this is the end-to-end assertion Plan 4's `cv read --mode pom` will rely on.
    """
    exc = DecoderNotRespondingError(
        "CV8 produced no result over POM after 3 attempts "
        "(interface ack only; docs/probe-results.md, R1)",
        cv=8,
    )
    report = report_for(exc, command="cv read")
    assert report.code == "decoder_not_responding"
    assert report.exit_code == 13
    assert "unsupported" not in report.message.lower()
    assert "not supported" not in report.message.lower()
    assert report.suggestions[0] == ["railctl", "doctor"]


def _bench_report(*, powered: bool) -> DoctorReport:
    """Track unpowered, `--power-on` not given: D3 is `unknown`, not `fail` - the
    expected state of a bench setup. D4/D10 skip because they need main/programming
    track power respectively.
    """
    d3_status = "ok" if powered else "unknown"
    d3_detail = "track power on" if powered else "track power off; re-run with --power-on"
    checks = (
        Check(id="D0", title="link", status="ok", detail="link opened"),
        Check(id="D1", title="link alive", status="ok", detail="XpressNet 4.0, id 0x12"),
        Check(id="D2", title="status", status="ok", detail="raw status byte decoded"),
        Check(id="D3", title="track power", status=d3_status, detail=d3_detail),
        Check(id="D4", title="POM read", status="skip", detail="main track unpowered"),
        Check(id="D10", title="address band", status="skip", detail="no --address in 100..127"),
    )
    caps = Capabilities(
        link_identity="serial:BENCH:3",
        probed_at="2026-08-09T12:00:00Z",
        xpressnet_version="4.0",
        command_station_id=18,
        notes=(
            "z21_cv_opcodes reply bands 63 16 and 63 17 are documented (Lenz "
            "secondary summary) but not exercised on this station",
        ),
    )
    return DoctorReport(checks=checks, capabilities=caps)


def test_pom_read_none_renders_null_in_json_and_unknown_in_human_never_no():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    assert result.result["capabilities"]["pom_read"] is None
    assert "  POM read: unknown" in result.lines
    assert not any(line.strip() == "POM read: no" for line in result.lines)


def test_json_rendering_keeps_pom_read_null_through_the_real_render_pipeline():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=False)
    body = json.loads(out.getvalue())
    assert body["result"]["capabilities"]["pom_read"] is None


def test_four_check_statuses_render_four_different_labels_in_human_and_json():
    checks = (
        Check(id="D0", title="link", status="ok", detail="opened"),
        Check(id="D3", title="track power", status="fail", detail="still off after power-on"),
        Check(id="D5", title="service direct", status="skip", detail="--no-programming-track"),
        Check(id="D4", title="POM read", status="unknown", detail="no result on either channel"),
    )
    report = DoctorReport(checks=checks, capabilities=Capabilities.unknown("serial:test:0"))
    result = build_doctor(report, saved_to=None)
    # rstrip: the label is padded to the width of the longest one so the table lines
    # up, which is presentation - the four words are what must stay distinguishable.
    labels = {line.split("]")[0].rstrip() for line in result.lines if line.startswith("[")}
    assert labels == {"[OK", "[FAIL", "[SKIP", "[UNKNOWN"}
    assert [c["status"] for c in result.result["checks"]] == ["ok", "fail", "skip", "unknown"]


def test_bench_scenario_track_unpowered_no_power_on_exits_zero():
    """A missing capability is information, not a failure."""
    report = _bench_report(powered=False)
    assert report.ok is True
    result = build_doctor(report, saved_to=None)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.result["checks"][3]["status"] == "unknown"  # D3
    assert result.result["checks"][4]["status"] == "skip"  # D4


def test_a_failed_link_check_exits_three():
    checks = (Check(id="D0", title="link", status="fail", detail="port not found"),)
    report = DoctorReport(checks=checks, capabilities=Capabilities.unknown("unknown"))
    assert report.ok is False
    result = build_doctor(report, saved_to=None)
    assert result.ok is False
    assert result.exit_code == 3


def test_human_output_ends_with_the_four_line_verdict_and_json_carries_the_same_lines():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    verdict = list(verdict_lines(report))
    assert len(verdict) == 4
    assert result.lines[-len(verdict) :] == verdict
    assert result.result["verdict"] == verdict


def test_notes_labelling_ranges_never_exercised_appear_in_both_renderings():
    report = _bench_report(powered=False)
    note = report.capabilities.notes[0]
    assert "63 16" in note and "not exercised" in note
    result = build_doctor(report, saved_to=None)
    assert note in result.result["notes"]
    assert any(note in line for line in result.lines)


def test_saved_to_is_the_path_in_both_renderings_and_null_when_nothing_was_written(tmp_path):
    report = _bench_report(powered=False)
    path = tmp_path / "capabilities.json"
    saved = build_doctor(report, saved_to=path)
    assert saved.result["saved_to"] == str(path)
    assert any(str(path) in line for line in saved.lines)
    assert build_doctor(report, saved_to=None).result["saved_to"] is None


# -- issue #14: what the run left the layout doing ----------------------------


def _powered_on_report(**layout: object) -> DoctorReport:
    base = _bench_report(powered=True)
    return DoctorReport(
        checks=base.checks,
        capabilities=base.capabilities,
        layout=LayoutState(**layout),
    )


def test_a_run_that_changed_no_track_power_says_so_and_still_exits_zero():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    assert result.result["layout"] == {
        "energised": False,
        "track_power": None,
        "held": None,
        "idled_address": None,
        "idled": None,
        "direction_preserved": None,
        "must_leave_held": False,
    }
    assert "  this run did not change the track power" in result.lines
    assert result.exit_code == 0
    assert result.warnings == []


def test_a_confirmed_hold_is_reported_with_power_ons_own_two_sentences():
    """The same two lines `railctl power on` prints, from the same constant: an
    operator must not have to learn a second vocabulary for the same hazard."""
    report = _powered_on_report(
        energised=True,
        track_power=True,
        held=True,
        idled_address=3,
        idled=True,
        direction_preserved=True,
    )
    result = build_doctor(report, saved_to=None)
    for line in HELD_LINES:
        assert f"  {line}" in result.lines
    assert RESUME_COMMAND in "\n".join(result.lines)
    assert any("loco 3 was sent speed 0" in line for line in result.lines)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.warnings == []


def test_a_hold_the_station_never_confirmed_is_a_partial_not_a_success():
    """`held is None` is UNKNOWN, and for a hazard - unlike for a capability -
    unknown has to be read as unsafe: acting on it means standing next to a
    locomotive that may start. Exit 8, the same partial code `power on` publishes
    for the same reading, and the same `hold_not_confirmed` warning name.
    """
    report = _powered_on_report(energised=True, track_power=True, held=None)
    result = build_doctor(report, saved_to=None)
    assert result.ok is False
    assert result.exit_code == PARTIAL_EXIT_CODE
    assert [w.name for w in result.warnings] == ["hold_not_confirmed"]


def test_a_layout_the_station_reports_free_is_the_measured_runaway():
    report = _powered_on_report(energised=True, track_power=True, held=False)
    result = build_doctor(report, saved_to=None)
    assert result.exit_code == PARTIAL_EXIT_CODE
    assert result.warnings[0].details["held"] is False
    assert any("stored speed" in line for line in result.lines)


def test_an_unconfirmed_energise_never_reads_as_a_dead_track():
    report = _powered_on_report(energised=None, held=None)
    result = build_doctor(report, saved_to=None)
    assert result.result["layout"]["energised"] is None
    assert any("MAY be live" in line for line in result.lines)


def test_a_probe_failure_outranks_the_partial_hold_code_but_still_warns():
    """Both readings are bad and they are not equally bad: a doctor whose D0-D2
    could not establish the basics has not measured the layout at all, so exit 3
    stays rather than being softened to the partial.

    The WARNING is not part of that precedence. It used to be dropped whole for a
    failed report, which silenced the machine-readable layout token in exactly the
    runs most likely to end with a live, unheld track."""
    checks = (Check(id="D0", title="link", status="fail", detail="port not found"),)
    report = DoctorReport(
        checks=checks,
        capabilities=Capabilities.unknown("unknown"),
        layout=LayoutState(energised=True, held=None),
    )
    result = build_doctor(report, saved_to=None)
    assert result.exit_code == 3
    assert [w.name for w in result.warnings] == ["hold_not_confirmed"]


def test_an_idle_telegram_that_did_not_land_is_named_never_left_out():
    report = _powered_on_report(
        energised=True, track_power=True, held=True, idled_address=3, idled=False
    )
    result = build_doctor(report, saved_to=None)
    assert any("still holds its stored speed" in line for line in result.lines)
    assert result.result["layout"]["idled"] is False
    # H8: a refused speed-0 telegram used to exit 0 with no warning at all, so a
    # script saw a clean run and `railctl power resume` then started the locomotive.
    assert [w.name for w in result.warnings] == ["loco_not_idled"]
    assert result.exit_code == PARTIAL_EXIT_CODE
    assert result.ok is False


# -- C1: the hold this run found, and what releasing it has to say ------------


def test_a_run_that_found_the_layout_held_and_left_it_held_says_so():
    """A plain `railctl doctor` on a live, held layout: every service-mode session
    clears the hold with resume-operations (run 5), so this run is responsible for
    putting it back, and the report has to say the layout is still held. It used to
    print only "this run did not change the track power" - about a layout it had
    quietly released.
    """
    report = _powered_on_report(energised=False, track_power=True, held=True, must_leave_held=True)
    result = build_doctor(report, saved_to=None)
    assert "  this run did not change the track power" in result.lines
    for line in HELD_LINES:
        assert f"  {line}" in result.lines
    assert result.warnings == []
    assert result.exit_code == 0


def test_a_hold_this_run_found_and_could_not_put_back_is_the_loudest_ending():
    """The C1 runaway: the doctor released a hold it never applied and the re-assert
    did not confirm. Naming which run this is matters - "this run energised the track"
    would be a false statement about a track that was live before it started."""
    report = _powered_on_report(energised=False, track_power=True, held=False, must_leave_held=True)
    result = build_doctor(report, saved_to=None)
    assert [w.name for w in result.warnings] == ["hold_not_confirmed"]
    assert "released the hold it found" in result.warnings[0].message
    assert result.exit_code == PARTIAL_EXIT_CODE
    assert any("stored speed" in line for line in result.lines)


def test_a_live_layout_this_run_neither_energised_nor_held_gets_no_hazard_line():
    """The ordinary diagnostic on somebody else's running layout. It reports what it
    READ - the old text never mentioned the track power at all - and it does not warn:
    a token that fires on runs which changed nothing is one nobody reads."""
    report = _powered_on_report(energised=False, track_power=True, held=False)
    result = build_doctor(report, saved_to=None)
    assert "  the track was already live before this run started" in result.lines
    assert not any("can start on its own" in line for line in result.lines)
    assert result.warnings == []
    assert result.exit_code == 0


def test_a_declined_power_on_says_the_track_was_off_and_still_is():
    report = _powered_on_report(energised=False, track_power=False)
    result = build_doctor(report, saved_to=None)
    assert "  the track was off before this run started and is off now" in result.lines
    assert result.warnings == []


def test_a_failed_probe_that_could_not_idle_the_loco_keeps_exit_three():
    """Same precedence as the hold warning, in the other partial: the locomotive is
    still named as able to start, and the bigger failure keeps the exit code."""
    checks = (Check(id="D0", title="link", status="fail", detail="port not found"),)
    report = DoctorReport(
        checks=checks,
        capabilities=Capabilities.unknown("unknown"),
        layout=LayoutState(
            energised=True, track_power=True, held=True, idled_address=3, idled=False
        ),
    )
    result = build_doctor(report, saved_to=None)
    assert [w.name for w in result.warnings] == ["loco_not_idled"]
    assert result.exit_code == 3


def test_a_track_switched_back_off_is_never_reported_as_able_to_move():
    """`_abandon_energised_track`: the hold failed, so the doctor put the power back
    off. `held` is UNKNOWN there - the stop telegram raised - and the old text turned
    that into "treat the layout as able to move" for a track the station reports
    dead."""
    report = _powered_on_report(energised=True, track_power=False, held=None)
    result = build_doctor(report, saved_to=None)
    assert any("nothing can move until it is switched back on" in line for line in result.lines)
    assert not any("able to move" in line for line in result.lines)
    assert result.warnings == []


def test_a_direction_that_could_not_be_read_is_not_claimed_preserved():
    report = _powered_on_report(
        energised=True,
        track_power=True,
        held=True,
        idled_address=3,
        idled=True,
        direction_preserved=False,
    )
    result = build_doctor(report, saved_to=None)
    assert [w.name for w in result.warnings] == ["direction_not_preserved"]


# -- issue #15: the file every other command reads has to get written ---------


class _FakeStation:
    def __init__(self, report: DoctorReport) -> None:
        self._report = report
        self.calls: dict[str, object] = {}
        self.closed = False

    def probe(self, *, address=None, allow_power_on=False, use_programming_track=True):
        self.calls = {
            "address": address,
            "allow_power_on": allow_power_on,
            "use_programming_track": use_programming_track,
        }
        return self._report

    def close(self) -> None:
        self.closed = True


def _wire(monkeypatch, tmp_path, report: DoctorReport, *, identity="serial:FAKE:3"):
    """Invoke the REAL nine-command `railctl.cli.main.app` through `CliRunner`.

    A throwaway app has no callback declaring the eight global options, so
    `--address` written after `doctor` would be "No such option" before
    `doctor_command` ever runs - and that spelling is the acceptance sentence. Only
    `open_station`/`link_info`/`station_info` are monkeypatched, onto the names
    `doctor.py` imported them under, and both `$HOME` and `$XDG_CONFIG_HOME` are
    redirected: a HOME-only patch leaks onto the real config directory of any machine
    that has XDG_CONFIG_HOME set.
    """
    fake_station = _FakeStation(report)
    monkeypatch.setattr(doctor, "open_station", lambda settings, *, capabilities_path: fake_station)
    monkeypatch.setattr(
        doctor,
        "link_info",
        lambda station, settings: LinkInfo(identity=identity, target="serial:fake"),
    )
    monkeypatch.setattr(
        doctor,
        "station_info",
        lambda station: StationInfo(
            protocol="xpressnet", protocol_version="4.0", command_station_id=18
        ),
    )
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return real_app, fake_station


def _identified(report: DoctorReport, identity: str) -> DoctorReport:
    return DoctorReport(
        checks=report.checks,
        capabilities=replace(report.capabilities, link_identity=identity),
        layout=report.layout,
    )


def test_power_on_and_no_programming_track_reach_station_probe(monkeypatch, tmp_path):
    report = _bench_report(powered=False)
    app, fake_station = _wire(monkeypatch, tmp_path, report)
    result = CliRunner().invoke(
        app,
        ["doctor", "--power-on", "--no-programming-track", "--no-save", "--address", "3"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert fake_station.calls == {
        "address": 3,
        "allow_power_on": True,
        "use_programming_track": False,
    }
    assert fake_station.closed is True


def test_doctor_saves_capabilities_json_merged_with_an_existing_station(monkeypatch, tmp_path):
    report = _identified(_bench_report(powered=False), "serial:FAKE:3")
    app, _ = _wire(monkeypatch, tmp_path, report)
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    cap_path.parent.mkdir(parents=True)
    cap_path.write_text(
        json.dumps({"version": 1, "links": {"serial:OTHER:9": {"probed_at": None}}}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    saved = json.loads(cap_path.read_text(encoding="utf-8"))
    assert set(saved["links"]) == {"serial:OTHER:9", "serial:FAKE:3"}
    assert saved["links"]["serial:FAKE:3"]["xpressnet_version"] == "4.0"
    assert json.loads(result.stdout)["result"]["saved_to"] == str(cap_path)


def test_no_save_touches_nothing_and_says_so_on_stderr(monkeypatch, tmp_path):
    report = _identified(_bench_report(powered=False), "serial:FAKE:3")
    app, _ = _wire(monkeypatch, tmp_path, report)
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    result = CliRunner().invoke(app, ["doctor", "--no-save", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert not cap_path.exists()
    assert "--no-save" in result.stderr
    assert json.loads(result.stdout)["result"]["saved_to"] is None


def test_unknown_identity_writes_nothing_and_never_reports_a_path(monkeypatch, tmp_path):
    """`save()` refuses an identity with no stable name, and the envelope has to agree:
    a `saved_to` path pointing at a file that was never written sends the next run
    looking for measurements that are not there."""
    report = _identified(_bench_report(powered=False), UNKNOWN_IDENTITY)
    app, _ = _wire(monkeypatch, tmp_path, report, identity=UNKNOWN_IDENTITY)
    result = CliRunner().invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert not (tmp_path / ".config" / "railctl" / "capabilities.json").exists()
    assert UNKNOWN_IDENTITY in result.stderr
    assert json.loads(result.stdout)["result"]["saved_to"] is None


def test_the_envelope_carries_the_link_and_the_station_blocks(monkeypatch, tmp_path):
    report = _identified(_bench_report(powered=False), "serial:FAKE:3")
    app, _ = _wire(monkeypatch, tmp_path, report)
    result = CliRunner().invoke(app, ["doctor", "--no-save", "--format", "json"])
    body = json.loads(result.stdout)
    assert body["link"]["identity"] == "serial:FAKE:3"
    assert body["station"]["command_station_id"] == 18
    assert body["schema"] == DOCTOR_SCHEMA


def test_address_after_the_verb_parses_which_is_m6s_own_acceptance_sentence(monkeypatch, tmp_path):
    """`railctl doctor --address 3`, not only `railctl --address 3 doctor`. Click
    hands an option written after the subcommand name to the subcommand, so this only
    parses because `doctor_command` redeclares all eight global options itself."""
    report = _bench_report(powered=False)
    app, fake_station = _wire(monkeypatch, tmp_path, report)
    result = CliRunner().invoke(app, ["doctor", "--address", "3", "--no-save"])
    assert result.exit_code == 0, result.stderr
    assert fake_station.calls["address"] == 3


def test_a_capabilities_file_that_cannot_be_written_still_prints_the_probe(monkeypatch, tmp_path):
    """C3: `Capabilities.save` mkdirs, writes a temp file and renames, and every one
    of those raises a bare `OSError` on a read-only config directory. Uncaught, that
    turned a finished probe into exit 1, `code: internal`, with EMPTY stdout - the
    measurements gone, and the layout block that says whether the track is live gone
    with them. The probe's result wins: a warning and `saved_to: null`.
    """
    report = _identified(_bench_report(powered=False), "serial:FAKE:3")
    app, _ = _wire(monkeypatch, tmp_path, report)
    config_dir = tmp_path / ".config" / "railctl"
    config_dir.mkdir(parents=True)
    config_dir.chmod(0o500)
    try:
        result = CliRunner().invoke(app, ["doctor", "--format", "json"])
    finally:
        config_dir.chmod(0o700)

    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)  # stdout is not empty and holds one JSON value
    assert body["result"]["saved_to"] is None
    assert body["result"]["checks"][0]["id"] == "D0"
    assert [w["name"] for w in body["warnings"]] == [CAPABILITIES_NOT_SAVED_WARNING]
    assert body["warnings"][0]["details"]["path"] == str(config_dir / "capabilities.json")


def test_the_envelope_describes_the_file_it_names_not_the_run_that_wrote_it(monkeypatch, tmp_path):
    """`saved_to` points at a file; `capabilities` must describe THAT file.

    `Capabilities.save` merges this run's record over the stored one, so a field this run
    established nothing about keeps whatever an earlier, wider run measured. The envelope
    used to publish the run's own record beside the path, so a doctor whose D4 was
    inconclusive printed `pom_read: null` while the file it had just named held `true` -
    two answers under one `saved_to`, and the JSON one is what a script reads.
    """
    identity = "serial:FAKE:3"
    base = _bench_report(powered=False)
    # This run establishes nothing about POM; it does learn a service-mode encoding.
    report = _identified(
        DoctorReport(
            checks=base.checks,
            capabilities=Capabilities(link_identity=identity, z21_cv_opcodes=True),
        ),
        identity,
    )
    app, _ = _wire(monkeypatch, tmp_path, report, identity=identity)
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    cap_path.parent.mkdir(parents=True)
    Capabilities(link_identity=identity, pom_read=True, service_direct_cv=True).save(cap_path)

    result = CliRunner().invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 0, result.stderr

    payload = json.loads(result.stdout)["result"]
    on_disk = json.loads(cap_path.read_text(encoding="utf-8"))["links"][identity]
    assert payload["saved_to"] == str(cap_path)
    # The field this run said nothing about: the file kept it, so the envelope must too.
    assert on_disk["pom_read"] is True
    assert payload["capabilities"]["pom_read"] is True
    # And what the run did learn reaches both.
    assert on_disk["z21_cv_opcodes"] is True
    assert payload["capabilities"]["z21_cv_opcodes"] is True
