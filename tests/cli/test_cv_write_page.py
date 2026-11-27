"""`cv write --page`, end to end: the CLI over a real `Station` over a scripted `FakeTransport`.

`tests/cli/test_cv.py` answers at the facade surface and so cannot see what the
selection puts on the wire. These tests run the real `Station`/`CvProgrammer`
under the CLI and script every telegram, so they pin the ORDER - CV31, CV32,
then the indexed CV - and what the envelope says about each part of it.

Two stubs, both the ones `tests/station/test_cv_write.py` already uses for its
service-write tests: `await_result` (the poll loop, which owns its own tests)
answers from a small decoder model, and `exit_service_mode` (which owns its own
tests in `tests/station/test_cv_service_mode.py`) does nothing. Everything else
- the telegrams, their order, the page bookkeeping, the events - is real.

The bench is the one measured in docs/probe-results.md: a ZIMO MS450P22 resting
at CV31=0, CV32=1, and CV266 (`volume_master`) written to 35.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app
from railctl.station import Station
from railctl.station.capabilities import Capabilities
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_pom_write_byte,
    cmd_station_status,
    cmd_station_version,
    cmd_z21_cv_read,
    cmd_z21_cv_write,
)
from railctl.xbus.replies import CvValue
from tests.station.conftest import Bench

runner = CliRunner()

ADDRESS = 3
THRESHOLD = 100
MASTER_VOLUME_CV = 266
VALUE = 35
RESTING_PAGE = (0, 1)

ACK = encode(0x01, 0x04)
STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
VERSION_REPLY = encode(0x63, 0x21, 0x40, 0x12)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _install(monkeypatch, bench: Bench) -> Bench:
    """`Station.open` hands back the bench's station, and the CLI's event
    callback is wired into the bench's own hook - the same seam the real
    `Station.open(on_event=...)` provides."""

    def fake_open(*_a, **kwargs):
        bench.on_event_hook = kwargs.get("on_event")
        return bench.station

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    return bench


def _decoder_model(monkeypatch, bench: Bench, cvs: dict[int, int]) -> None:
    """Every service-mode wait answers with the model's value for the CV the
    request named: an echo after a write, a value after a read. The model holds
    the values AFTER the writes, so a read-back agrees exactly when the write
    went to the CV the test expects."""
    programmer = bench.station.programmer

    def answer(matcher, **_kwargs):
        return CvValue(raw_cv=matcher.cv, value=cvs[matcher.cv], ident=0x14, z21_form=True)

    monkeypatch.setattr(programmer, "await_result", answer)
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)


def _service_bench(monkeypatch) -> Bench:
    bench = Bench(
        capabilities=Capabilities.unknown("bench").with_learned(z21_cv_opcodes=True)
    ).open()
    _decoder_model(
        monkeypatch,
        bench,
        {31: RESTING_PAGE[0], 32: RESTING_PAGE[1], MASTER_VOLUME_CV: VALUE},
    )
    return _install(monkeypatch, bench)


def _script_service_write(bench: Bench, cv: int, value: int) -> None:
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_write(cv, value), reply=ACK)


def _script_service_read(bench: Bench, cv: int) -> None:
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(cv), reply=ACK)


def test_a_prog_write_with_a_page_selects_reads_the_page_back_then_writes_and_verifies(
    monkeypatch,
):
    bench = _service_bench(monkeypatch)
    # select_page: write CV31, write CV32, then read both back (service reads
    # ARE available on this station, so the selection is verified, not assumed).
    _script_service_write(bench, 31, RESTING_PAGE[0])
    _script_service_write(bench, 32, RESTING_PAGE[1])
    _script_service_read(bench, 31)
    _script_service_read(bench, 32)
    # The write itself, echo-confirmed, then the independent read-back.
    _script_service_write(bench, MASTER_VOLUME_CV, VALUE)
    _script_service_read(bench, MASTER_VOLUME_CV)
    bench.expect(cmd_station_version(), reply=VERSION_REPLY)

    result = runner.invoke(
        app,
        [
            "cv",
            "write",
            str(MASTER_VOLUME_CV),
            str(VALUE),
            "--page",
            "0:1",
            "--yes",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["cv"] == MASTER_VOLUME_CV
    assert payload["result"]["name"] == "volume_master"
    assert payload["result"]["page"] == list(RESTING_PAGE)
    assert payload["result"]["verified"] is True
    # The selection read back as written, so nothing is flagged unverified.
    assert [w["name"] for w in payload["warnings"]] == []
    writes = [
        telegram
        for telegram in bench.sent
        if telegram
        in (
            cmd_z21_cv_write(31, RESTING_PAGE[0]),
            cmd_z21_cv_write(32, RESTING_PAGE[1]),
            cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE),
        )
    ]
    assert writes == [
        cmd_z21_cv_write(31, RESTING_PAGE[0]),
        cmd_z21_cv_write(32, RESTING_PAGE[1]),
        cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE),
    ]


def test_a_prog_write_whose_page_does_not_stick_names_cv31_and_never_writes_the_indexed_cv(
    monkeypatch,
):
    """The bench case: the MS450P22 refuses CV32=0, so `--page 0:0` is echoed
    and then reads back as 0:1. The selection must fail against CV31 - the pair,
    not CV266 - and CV266 must never be written onto a page nobody asked for."""
    bench = Bench(
        capabilities=Capabilities.unknown("bench").with_learned(z21_cv_opcodes=True)
    ).open()
    programmer = bench.station.programmer
    # Per-CV answers in call order: the write echo first, then the read-back.
    answers = {31: [0, 0], 32: [0, 1]}

    def answer(matcher, **_kwargs):
        return CvValue(
            raw_cv=matcher.cv, value=answers[matcher.cv].pop(0), ident=0x14, z21_form=True
        )

    monkeypatch.setattr(programmer, "await_result", answer)
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    _install(monkeypatch, bench)
    _script_service_write(bench, 31, 0)
    _script_service_write(bench, 32, 0)
    _script_service_read(bench, 31)
    _script_service_read(bench, 32)

    result = runner.invoke(
        app,
        [
            "cv",
            "write",
            str(MASTER_VOLUME_CV),
            str(VALUE),
            "--page",
            "0:0",
            "--yes",
            "--format",
            "json",
        ],
    )

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code != 0
    assert envelope["code"] == "cv_verify", envelope
    assert envelope["details"]["cv"] == 31
    assert envelope["details"]["page_selection"]["page"] == [0, 0]
    assert envelope["details"]["page_selection"]["state"] == "unknown"
    assert cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE) not in bench.sent


def test_a_main_track_write_that_fails_after_the_selection_says_the_page_is_unknown(monkeypatch):
    """CV31/CV32 go out and are acked (page.unverified is emitted), then CV266
    is refused busy. `cv write` does not put the pair back, so the error envelope
    - which carries no warnings - must say in `details` and the hint that the
    page is unknown, and keep the page.unverified event rather than drop it."""
    bench = _pom_bench(monkeypatch)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, RESTING_PAGE[0], threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, RESTING_PAGE[1], threshold=THRESHOLD), reply=ACK)
    bench.expect(
        cmd_pom_write_byte(ADDRESS, MASTER_VOLUME_CV, VALUE, threshold=THRESHOLD),
        reply=encode(0x61, 0x81),
    )

    result = runner.invoke(app, [*_POM_ARGV, "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["code"] == "station_busy", envelope
    selection = envelope["details"]["page_selection"]
    assert selection == {"page": [0, 1], "state": "unknown", "events": ["page.unverified"]}
    assert "which index page the decoder is on is unknown" in envelope["hint"]
    assert "railctl cv read 31 32" in envelope["hint"]


def _pom_bench(monkeypatch) -> Bench:
    # The reference station as measured: POM read is silence, recorded false.
    bench = Bench(capabilities=Capabilities.unknown("bench").with_learned(pom_read=False)).open()
    return _install(monkeypatch, bench)


def _script_pom_write_with_page(bench: Bench) -> None:
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)  # the POM pre-flight
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, RESTING_PAGE[0], threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, RESTING_PAGE[1], threshold=THRESHOLD), reply=ACK)
    bench.expect(
        cmd_pom_write_byte(ADDRESS, MASTER_VOLUME_CV, VALUE, threshold=THRESHOLD), reply=ACK
    )
    bench.expect(cmd_station_version(), reply=VERSION_REPLY)


_POM_ARGV = [
    "cv",
    "write",
    str(MASTER_VOLUME_CV),
    str(VALUE),
    "--track",
    "main",
    "--address",
    str(ADDRESS),
    "--page",
    "0:1",
    "--yes",
]


def test_a_main_track_write_with_a_page_warns_the_page_is_unverified_and_claims_nothing(
    monkeypatch,
):
    bench = _pom_bench(monkeypatch)
    _script_pom_write_with_page(bench)

    result = runner.invoke(app, [*_POM_ARGV, "--format", "json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    # Three telegrams on the main track, in this order, and nothing read back.
    assert bench.sent == [
        STATUS_REQUEST,
        cmd_pom_write_byte(ADDRESS, 31, RESTING_PAGE[0], threshold=THRESHOLD),
        cmd_pom_write_byte(ADDRESS, 32, RESTING_PAGE[1], threshold=THRESHOLD),
        cmd_pom_write_byte(ADDRESS, MASTER_VOLUME_CV, VALUE, threshold=THRESHOLD),
        cmd_station_version(),
    ]
    # `null`, never `true`: nothing read the CV back, and nothing read the page
    # back either. The page is reported as SELECTED, and the warning says the
    # selection is unknown.
    assert payload["result"]["verified"] is None
    assert payload["result"]["page"] == list(RESTING_PAGE)
    by_name = {w["name"]: w for w in payload["warnings"]}
    assert by_name["page.unverified"]["details"] == {"page": [0, 1], "mode": "pom"}
    assert "unknown" in by_name["page.unverified"]["message"]
    assert "0:1" in by_name["page.unverified"]["message"]
    # The read-back advice has to be a command that runs on an indexed CV. Two
    # `cv.write_unverified` warnings ride here - the command's own and the
    # station's event - so the advice is looked for among all of them.
    advice = [w["message"] for w in payload["warnings"] if w["name"] == "cv.write_unverified"]
    assert any(f"railctl cv read {MASTER_VOLUME_CV} --page 0:1" in text for text in advice)


def test_a_main_track_write_with_a_page_says_the_same_in_human_output(monkeypatch):
    bench = _pom_bench(monkeypatch)
    _script_pom_write_with_page(bench)

    result = runner.invoke(app, _POM_ARGV)

    assert result.exit_code == 0, result.stderr
    assert "warning: page.unverified:" in result.stdout
    assert "verified: unknown" in result.stdout
    assert "verified: yes" not in result.stdout
    assert "index page CV31:CV32 = 0:1 selected before the write" in result.stdout
