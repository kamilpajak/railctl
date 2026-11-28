"""`cv write --page`, end to end: the CLI over a real `Station` over a scripted `FakeTransport`.

`tests/cli/test_cv.py` answers at the facade surface and so cannot see what the
selection puts on the wire. These tests run the real `Station`/`CvProgrammer`
under the CLI and script every telegram, so they pin the ORDER - CV31, CV32,
then the indexed CV - and what the envelope says about each part of it.

Two stubs, both the ones `tests/station/test_cv_write.py` already uses for its
service-write tests: `await_result` (the poll loop, which owns its own tests)
answers from a small decoder model, and `exit_service_mode` (which owns its own
tests in `tests/station/test_cv_service_mode.py`) sends nothing and only clears
the caches, as the real one does. Everything else - the telegrams, their order,
the page bookkeeping, the events - is real.

The bench is the one measured in docs/probe-results.md: a ZIMO MS450P22 resting
at CV31=0, CV32=1, and CV266 (`volume_master`) written to 35.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app
from railctl.errors import DecoderNotRespondingError
from railctl.exit_codes import INTERRUPTED_EXIT_CODE
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


def _decoder_model(monkeypatch, bench: Bench, cvs: dict[int, int | list[int]]) -> None:
    """Every service-mode wait answers with the model's value for the CV the
    request named: an echo after a write, a value after a read. The model holds
    the values AFTER the writes, so a read-back agrees exactly when the write
    went to the CV the test expects. A list answers in call order instead.

    `exit_service_mode` sends nothing, but it does what the real one does to
    the station's caches: every service session ends by invalidating them, so
    each page selection - the restore included - is verified by reading back,
    exactly as on the bench."""
    programmer = bench.station.programmer

    def answer(matcher, **_kwargs):
        value = cvs[matcher.cv]
        if isinstance(value, list):
            value = value.pop(0)
        return CvValue(raw_cv=matcher.cv, value=value, ident=0x14, z21_form=True)

    monkeypatch.setattr(programmer, "await_result", answer)
    monkeypatch.setattr(
        programmer, "exit_service_mode", lambda **_: bench.station.invalidate_caches()
    )


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


def _script_pair_read(bench: Bench) -> None:
    _script_service_read(bench, 31)
    _script_service_read(bench, 32)


def _script_selection(bench: Bench, page: tuple[int, int]) -> None:
    """select_page: write CV31, write CV32, then read both back (service reads
    ARE available on this station, so the selection is verified, not assumed)."""
    _script_service_write(bench, 31, page[0])
    _script_service_write(bench, 32, page[1])
    _script_pair_read(bench)


_PROG_ARGV = ["cv", "write", str(MASTER_VOLUME_CV), str(VALUE), "--page", "0:1", "--yes"]


def test_a_prog_write_with_a_page_reads_the_pair_selects_writes_and_puts_the_pair_back(
    monkeypatch,
):
    bench = _service_bench(monkeypatch)
    # The pair as found, read BEFORE anything is written.
    _script_pair_read(bench)
    _script_selection(bench, RESTING_PAGE)
    # The write itself, echo-confirmed, then the independent read-back.
    _script_service_write(bench, MASTER_VOLUME_CV, VALUE)
    _script_service_read(bench, MASTER_VOLUME_CV)
    # The restore: the found pair re-selected and read back, even though it
    # equals the page used - the same leave-as-found `cv read --page` does.
    _script_selection(bench, RESTING_PAGE)
    bench.expect(cmd_station_version(), reply=VERSION_REPLY)

    result = runner.invoke(app, [*_PROG_ARGV, "--format", "json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["cv"] == MASTER_VOLUME_CV
    assert payload["result"]["name"] == "volume_master"
    assert payload["result"]["page"] == {
        "pair": list(RESTING_PAGE),
        "verified": True,
        "source": "selected",
        "restore": {"pair": list(RESTING_PAGE), "verified": True},
    }
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
        cmd_z21_cv_write(31, RESTING_PAGE[0]),
        cmd_z21_cv_write(32, RESTING_PAGE[1]),
    ]


def test_a_prog_write_with_a_page_says_it_was_confirmed_and_put_back_in_human_output(
    monkeypatch,
):
    """The decoder sits on the audio bank 145:2 when the write starts: the
    write goes to 0:1, and 145:2 is what is put back - the pair FOUND, never a
    fixed "neutral" one (the ZIMO MS manual names three: pp. 68, 69, 70)."""
    bench = _service_bench(monkeypatch)
    _decoder_model(
        monkeypatch,
        bench,
        # pre-read, select echo, select read-back, restore echo, restore read-back
        {31: [145, 0, 0, 145, 145], 32: [2, 1, 1, 2, 2], MASTER_VOLUME_CV: VALUE},
    )
    _script_pair_read(bench)
    _script_selection(bench, RESTING_PAGE)
    _script_service_write(bench, MASTER_VOLUME_CV, VALUE)
    _script_service_read(bench, MASTER_VOLUME_CV)
    _script_selection(bench, (145, 2))
    bench.expect(cmd_station_version(), reply=VERSION_REPLY)

    result = runner.invoke(app, _PROG_ARGV)

    assert result.exit_code == 0, result.stderr
    assert "index page CV31:CV32 = 0:1 selected and confirmed by read-back" in result.stdout
    assert (
        "index page put back to 145:2, as found before the write, and confirmed by read-back"
        in result.stdout
    )
    assert "left on" not in result.stdout


def test_a_prog_write_whose_page_does_not_stick_names_the_pair_and_never_writes_the_indexed_cv(
    monkeypatch,
):
    """The bench case: the MS450P22 refuses CV32=0, so `--page 0:0` is echoed
    and then reads back as 0:1. The selection must fail against CV31 - the pair,
    not CV266 - CV266 must never be written onto a page nobody asked for, and
    the pair found before the write is put back all the same."""
    bench = Bench(
        capabilities=Capabilities.unknown("bench").with_learned(z21_cv_opcodes=True)
    ).open()
    # Per-CV answers in call order: the pre-read, the write echo, the
    # read-back, then the restore's echo and read-back.
    _decoder_model(monkeypatch, bench, {31: [0, 0, 0, 0, 0], 32: [1, 0, 1, 1, 1]})
    _install(monkeypatch, bench)
    _script_pair_read(bench)
    _script_selection(bench, (0, 0))
    _script_selection(bench, RESTING_PAGE)

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
    details = envelope["details"]
    assert details["cv"] == 31
    # The station's own account of the mismatch, not the indexed CV's.
    assert details["reason"] == "page_selection_mismatch"
    assert details["wrote"] == [0, 0]
    assert details["read_back"] == [0, 1]
    assert details["page_phase"] == "select"
    assert details["page_restore"] == {"pair": [0, 1], "verified": True}
    assert details["page_selection"]["page"] == [0, 0]
    assert details["page_selection"]["state"] == "unknown"
    assert f"CV{MASTER_VOLUME_CV} was not written" in envelope["hint"]
    assert "put back to 0:1" in envelope["hint"]
    assert cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE) not in bench.sent


def test_a_prog_write_whose_pair_cannot_be_read_first_writes_nothing_and_says_so(monkeypatch):
    """Silence on the pre-read: nothing may be selected, because the pair it
    would have to be put back to is unknown. The hint must not claim CV31/CV32
    "may already have been written" - they were not."""
    bench = _service_bench(monkeypatch)
    programmer = bench.station.programmer

    def silent(matcher, **_kwargs):
        raise DecoderNotRespondingError("silent", cv=matcher.cv)

    monkeypatch.setattr(programmer, "await_result", silent)
    _script_service_read(bench, 31)

    result = runner.invoke(app, [*_PROG_ARGV, "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["code"] == "decoder_not_responding", envelope
    assert envelope["details"]["page_phase"] == "pre_read"
    assert envelope["details"]["page_selection"]["state"] == "unknown"
    assert "nothing was written" in envelope["hint"]
    assert "may already have written" not in envelope["hint"]
    # Only the status check and the one CV31 read went out - no write at all.
    assert bench.sent == [STATUS_REQUEST, cmd_z21_cv_read(31)]
    # The read it suggests is of CV31 itself, which no page indexes: `--page`
    # there is refused as `page_not_applicable`, so it must not be added.
    assert envelope["suggestions"] == [
        ["railctl", "doctor"],
        ["railctl", "cv", "read", "31", "--mode", "service"],
    ]


def test_a_prog_write_whose_restore_does_not_stick_says_the_page_is_unknown(monkeypatch):
    """The write went through, then the found pair 145:2 did not read back as
    written: the decoder is NOT as it was found, and that is what the command
    reports - an error tagged `restore`, never a success."""
    bench = _service_bench(monkeypatch)
    _decoder_model(
        monkeypatch,
        bench,
        {31: [145, 0, 0, 145, 145], 32: [2, 1, 1, 2, 1], MASTER_VOLUME_CV: VALUE},
    )
    _script_pair_read(bench)
    _script_selection(bench, RESTING_PAGE)
    _script_service_write(bench, MASTER_VOLUME_CV, VALUE)
    _script_service_read(bench, MASTER_VOLUME_CV)
    _script_selection(bench, (145, 2))

    result = runner.invoke(app, [*_PROG_ARGV, "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["code"] == "cv_verify", envelope
    assert envelope["details"]["page_phase"] == "restore"
    assert envelope["details"]["reason"] == "page_selection_mismatch"
    assert envelope["details"]["wrote"] == [145, 2]
    assert envelope["details"]["read_back"] == [145, 1]
    assert envelope["details"]["page_selection"]["state"] == "unknown"
    assert "putting CV31/CV32 back to the pair found before the write failed" in envelope["hint"]
    assert "railctl cv read 31 32" in envelope["hint"]
    # The restore error replaced the write's result; the write's own outcome
    # rides on it, so "CV266 written, page lost" is not read as "CV266 failed".
    assert envelope["details"]["write"] == {
        "cv": MASTER_VOLUME_CV,
        "value": VALUE,
        "state": "written",
        "verified": True,
    }
    assert (
        f"CV{MASTER_VOLUME_CV} = {VALUE} was written and confirmed by read-back"
        in (envelope["hint"])
    )


def test_a_main_track_write_that_fails_after_the_selection_says_the_page_is_unknown(monkeypatch):
    """CV31/CV32 go out and are acked (page.unverified is emitted), then CV266
    is refused busy. The main track never puts the pair back, so the error
    envelope - which carries no warnings - must say in `details` and the hint
    that the page is unknown, and keep the page.unverified event rather than
    drop it."""
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
    assert "the main track never puts the pair back" in envelope["hint"]
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
    assert payload["result"]["page"] == {
        "pair": list(RESTING_PAGE),
        "verified": None,
        "source": "selected",
        "restore": None,
    }
    by_name = {w["name"]: w for w in payload["warnings"]}
    assert by_name["page.unverified"]["details"] == {"page": [0, 1], "mode": "pom"}
    assert "unknown" in by_name["page.unverified"]["message"]
    assert "0:1" in by_name["page.unverified"]["message"]
    # Exactly one `cv.write_unverified`: the command's own, carrying the
    # station's reason. The station's copy of the event is folded into it.
    (advice,) = [w for w in payload["warnings"] if w["name"] == "cv.write_unverified"]
    # The read-back advice has to be a command that runs on an indexed CV.
    assert f"railctl cv read {MASTER_VOLUME_CV} --page 0:1" in advice["message"]
    assert advice["details"]["reason"] == "verify=False: no read-back was attempted"


def test_a_main_track_write_with_a_page_says_the_same_in_human_output(monkeypatch):
    bench = _pom_bench(monkeypatch)
    _script_pom_write_with_page(bench)

    result = runner.invoke(app, _POM_ARGV)

    assert result.exit_code == 0, result.stderr
    assert "warning: page.unverified:" in result.stdout
    assert "verified: unknown" in result.stdout
    assert "verified: yes" not in result.stdout
    assert "index page CV31:CV32 = 0:1 selected, not confirmed" in result.stdout
    assert "the decoder is left on index page 0:1" in result.stdout


def test_an_interrupt_between_cv31_and_cv32_still_says_the_page_is_unknown(monkeypatch):
    """Ctrl-C after CV31 went out and before CV32 did: the decoder may be on half
    a page. `run()` alone reports a bare `aborted` with empty details, which hides
    exactly the fact `page_selection` exists to publish."""
    bench = _pom_bench(monkeypatch)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, RESTING_PAGE[0], threshold=THRESHOLD), reply=ACK)
    second_half = cmd_pom_write_byte(ADDRESS, 32, RESTING_PAGE[1], threshold=THRESHOLD)
    original_write = bench.transport.write

    def write(data: bytes) -> None:
        if second_half in data:
            raise KeyboardInterrupt
        original_write(data)

    monkeypatch.setattr(bench.transport, "write", write)

    result = runner.invoke(app, [*_POM_ARGV, "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code == INTERRUPTED_EXIT_CODE, result.stderr
    assert envelope["code"] == "aborted", envelope
    assert envelope["details"]["page_selection"]["page"] == [0, 1]
    assert envelope["details"]["page_selection"]["state"] == "unknown"
    assert "which index page the decoder is on is unknown" in envelope["hint"]


def test_an_interrupt_during_a_prog_write_puts_the_pair_back_and_never_says_main_track(
    monkeypatch,
):
    """Ctrl-C as CV266 goes out on the programming track: the station still
    re-selects the pair it found (the restore runs for any exception, not only
    a `RailctlError`), and the `aborted` envelope speaks of the programming
    track - never the main track's "never puts the pair back"."""
    bench = _service_bench(monkeypatch)
    _script_pair_read(bench)
    _script_selection(bench, RESTING_PAGE)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    indexed_write = cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE)
    _script_selection(bench, RESTING_PAGE)  # the restore
    original_write = bench.transport.write

    def write(data: bytes) -> None:
        if indexed_write in data:
            raise KeyboardInterrupt
        original_write(data)

    monkeypatch.setattr(bench.transport, "write", write)

    result = runner.invoke(app, [*_PROG_ARGV, "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code == INTERRUPTED_EXIT_CODE, result.stderr
    assert envelope["code"] == "aborted", envelope
    assert envelope["details"]["page_selection"]["state"] == "unknown"
    assert "never puts the pair back" not in envelope["hint"]
    assert "whether the pair found before the write was put back is unknown" in envelope["hint"]
    # The restore went out after the interrupt: CV31/CV32 written back.
    selection_writes = [
        telegram
        for telegram in bench.sent
        if telegram
        in (cmd_z21_cv_write(31, RESTING_PAGE[0]), cmd_z21_cv_write(32, RESTING_PAGE[1]))
    ]
    assert len(selection_writes) == 4


def test_an_interrupt_during_the_restore_still_records_that_the_cv_went_out_unverified(
    monkeypatch,
):
    """`--no-verify` on the programming track: CV266 is written, the station
    emits `cv.write_unverified`, then Ctrl-C lands on the restore's CV31 write.
    No `build_cv_write` runs and the interrupt carries no `details.write`, so
    `page_selection.events` is the only trace that the CV went out unverified.
    Folding the event into the command's warning must not erase it here."""
    bench = _service_bench(monkeypatch)
    _script_pair_read(bench)
    _script_selection(bench, RESTING_PAGE)
    _script_service_write(bench, MASTER_VOLUME_CV, VALUE)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    restore_cv31 = cmd_z21_cv_write(31, RESTING_PAGE[0])
    seen = 0
    original_write = bench.transport.write

    def write(data: bytes) -> None:
        nonlocal seen
        if restore_cv31 in data:
            seen += 1
            if seen == 2:  # the first is the selection, the second the restore
                raise KeyboardInterrupt
        original_write(data)

    monkeypatch.setattr(bench.transport, "write", write)

    result = runner.invoke(app, [*_PROG_ARGV, "--no-verify", "--format", "json"])

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code == INTERRUPTED_EXIT_CODE, result.stderr
    assert envelope["code"] == "aborted", envelope
    assert cmd_z21_cv_write(MASTER_VOLUME_CV, VALUE) in bench.sent
    assert envelope["details"]["page_selection"]["events"] == ["cv.write_unverified"]


def test_an_address_typed_before_the_verb_rides_along_in_the_confirmation_retry():
    """`railctl --address 7 cv write ...`: the root callback takes the address,
    and the verb's own `--address` stays None. A retry built from the verb's
    parameter named no locomotive - it exited 2, or wrote to the config's."""
    result = runner.invoke(
        app,
        [
            "--address",
            "7",
            "cv",
            "write",
            str(MASTER_VOLUME_CV),
            str(VALUE),
            "--track",
            "main",
            "--page",
            "0:1",
            "--non-interactive",
            "--format",
            "json",
        ],
    )

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["code"] == "confirmation_required", envelope
    retry = envelope["suggestions"][0]
    assert retry[retry.index("--address") + 1] == "7"
    assert retry[-1] == "--yes"


def test_a_silent_decoder_after_a_page_selection_suggests_a_read_on_that_page():
    """The hint says the page is unknown, so the suggested read has to select
    the write's own page; without `--page` it reads whichever page the decoder
    is left on. An error that recorded no page keeps the plain suggestion."""
    from railctl.cli._errors import report_for
    from railctl.cli.commands.cv import _note_page_left_unknown
    from railctl.cli.deps import StationEventLog
    from railctl.errors import DecoderNotRespondingError

    paged = DecoderNotRespondingError("silent", cv=MASTER_VOLUME_CV)
    _note_page_left_unknown(
        paged,
        RESTING_PAGE,
        StationEventLog(),
        cv=MASTER_VOLUME_CV,
        track="main",
        assume_yes=False,
    )
    plain = DecoderNotRespondingError("silent", cv=MASTER_VOLUME_CV)

    read = ["railctl", "cv", "read", str(MASTER_VOLUME_CV), "--mode", "service"]
    assert [*read, "--page", "0:1"] in report_for(paged, command="cv write").suggestions
    assert read in report_for(plain, command="cv write").suggestions


def _noted(error, *, track: str):
    from railctl.cli.commands.cv import _note_page_left_unknown
    from railctl.cli.deps import StationEventLog

    _note_page_left_unknown(
        error,
        (145, 2),
        StationEventLog(),
        cv=MASTER_VOLUME_CV,
        track=track,
        assume_yes=False,
    )
    return error


def test_a_prog_write_that_failed_after_a_confirmed_restore_says_the_pair_is_back():
    """The CV write failed and the station put the found pair back and read it
    back (`details.page_restore`): the hint says so instead of "unknown", while
    `page_selection.state` stays "unknown" - this layer measured nothing."""
    error = _noted(
        DecoderNotRespondingError(
            "silent",
            cv=MASTER_VOLUME_CV,
            details={"page_restore": {"pair": [0, 1], "verified": True}},
        ),
        track="prog",
    )
    assert error.details["page_selection"]["state"] == "unknown"
    assert "CV31/CV32 were put back to 0:1" in error.hint
    assert "read back to confirm it" in error.hint
    assert "unknown" not in error.hint
    # Not a selection failure: nothing claims the indexed CV went unwritten.
    assert "was not written" not in error.hint


def test_a_prog_write_that_failed_with_no_restore_fact_stays_hedged():
    """An interrupt, or anything else that carried no `page_restore`: whether
    the pair was put back is unknown, and the hint must not say it was - nor
    borrow the main track's "never puts the pair back"."""
    error = _noted(DecoderNotRespondingError("silent", cv=MASTER_VOLUME_CV), track="prog")
    assert "whether the pair found before the write was put back is unknown" in error.hint
    assert "never puts the pair back" not in error.hint
    assert "railctl cv read 31 32" in error.hint


def test_a_prog_write_whose_restore_was_not_read_back_never_claims_it_was_confirmed():
    """`page_restore.verified` is `null`: the pair was written back and nothing
    read it. Saying "read back to confirm it" would be a measurement nobody made."""
    error = _noted(
        DecoderNotRespondingError(
            "silent",
            cv=MASTER_VOLUME_CV,
            details={"page_restore": {"pair": [0, 1], "verified": None}},
        ),
        track="prog",
    )
    assert "CV31/CV32 were written back to 0:1" in error.hint
    assert "nothing read them back" in error.hint
    assert "confirm it" not in error.hint
    assert "railctl cv read 31 32" in error.hint


@pytest.mark.parametrize(
    ("write", "expected"),
    [
        (
            {"cv": 266, "value": 35, "state": "written", "verified": None},
            "CV266 = 35 was written but not confirmed by read-back",
        ),
        (
            {"cv": 266, "value": 35, "state": "failed", "error": "decoder_no_ack"},
            "writing CV266 = 35 failed first (decoder_no_ack)",
        ),
        (
            {"cv": 266, "value": 35, "state": "not_sent", "error": "cv_verify"},
            "CV266 was not written (selecting the page failed first)",
        ),
    ],
)
def test_a_failed_restore_says_what_happened_to_the_indexed_cv_first(write, expected):
    error = _noted(
        DecoderNotRespondingError(
            "silent", cv=31, details={"page_phase": "restore", "write": write}
        ),
        track="prog",
    )
    assert error.hint.startswith(expected), error.hint
    assert "putting CV31/CV32 back to the pair found before the write failed" in error.hint
    assert "confirmed by read-back, but" not in error.hint


def test_a_page_step_failing_on_cv31_suggests_a_read_of_it_without_page():
    """A page step fails against CV31 or CV32, which no page indexes: the read
    suggested for it carries no `--page`, which `page_not_applicable` refuses."""
    from railctl.cli._errors import report_for

    error = _noted(
        DecoderNotRespondingError("silent", cv=31, details={"page_phase": "restore"}),
        track="prog",
    )
    suggestions = report_for(error, command="cv write").suggestions
    assert ["railctl", "cv", "read", "31", "--mode", "service"] in suggestions
    assert not any("--page" in argv for argv in suggestions)
