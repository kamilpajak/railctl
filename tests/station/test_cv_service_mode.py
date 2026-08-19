"""CvProgrammer's service-mode read: the encoding ladder, the 95 s timeout regime,
exit_service_mode, the 63 10 register fallback, and Station.cv_read.

Every test here goes through `bench` / `bench_factory` (tests/station/conftest.py,
Task 2), which wrap a real Station over a FakeTransport-backed Link, already past
the version handshake, driven by a fake clock. A service-mode read costs up to
TIMING.service_result (95.0 s) against a real clock; under the fake clock every
test in this file runs in microseconds. If a test in this file takes real
seconds, a real time.sleep leaked in through Station(sleep=...).
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    CvOutOfRangeError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
    StationBusyError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import (
    SERVICE_BATCH_SIZE,
    SERVICE_ENCODING_ORDER,
    UNEXERCISED_BANDS,
    CvProgrammer,
    TimedOut,
)
from railctl.station.timing import TIMING
from railctl.station.types import EVENT_NAMES, CvReadOutcome, CvResult, CvSpec, ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_service_result_request,
    cmd_station_status,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
)
from railctl.xbus.cv import MAX_CV_DIRECT
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import GENERIC_ACK, Busy, CvValue, NoAck, PagedCvValue, ShortCircuit

STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x01)
STATUS_SERVICE_MODE = encode(0x62, 0x22, 0x08)
POLL = cmd_service_result_request()
ACK = encode(0x01, 0x04)
UNSUPPORTED = encode(0x61, 0x82)


def make_capabilities(**overrides: bool | None) -> Capabilities:
    """`Capabilities.unknown` with a hand-picked True/False/None combination,
    passed straight into `bench_factory(capabilities=...)`.

    Goes through `with_learned` rather than `bench.station.learn(...)` on
    purpose: most of the fields this file sets (`z21_cv_opcodes`,
    `service_ext_cv`) are not in `LEARNABLE_FIELDS` at all, and only
    `Station.learn` enforces that list - `with_learned` accepts any real
    field name (1.3).
    """
    return Capabilities.unknown("bench").with_learned(**overrides)


def test_service_encoding_order_is_z21_then_direct_then_extended():
    """Pins the order itself: the earlier design put service_direct first,
    which on this station is the one encoding that answers nothing until
    separately polled. Z21 covers CV1..1024 in one field and its result
    arrives unsolicited - the only channel that cannot return a stale
    stored result. Task 6 iterates this tuple as
    `for field_name, encoding in SERVICE_ENCODING_ORDER` and gates each step
    on `getattr(capabilities, field_name) is True` - a bare-enum form
    crashes, which is exactly why the tuple carries the field name alongside
    the encoding rather than the encoding alone."""
    assert [name for name, _ in SERVICE_ENCODING_ORDER] == [
        "z21_cv_opcodes",
        "service_direct_cv",
        "service_ext_cv",
    ]
    assert [encoding for _, encoding in SERVICE_ENCODING_ORDER] == [
        CvEncoding.Z21_16BIT,
        CvEncoding.SERVICE_DIRECT,
        CvEncoding.SERVICE_EXT,
    ]


def test_unexercised_bands_are_the_two_high_pages_never_answered_here():
    """Only 63 14 (CV1-255) and 63 15 (CV256-511) have ever been answered on
    real hardware; 63 16 and 63 17 come from the Lenz document alone."""
    assert UNEXERCISED_BANDS == frozenset({2, 3})


def test_service_read_telegram_prefers_z21_when_every_capability_is_true(bench_factory):
    capabilities = make_capabilities(
        z21_cv_opcodes=True, service_direct_cv=True, service_ext_cv=True
    )
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(8)

    assert telegram == cmd_z21_cv_read(8)
    assert encoding is CvEncoding.Z21_16BIT
    assert page == 0


def test_service_read_telegram_falls_back_to_direct_when_z21_is_unavailable(bench_factory):
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(8)

    assert telegram == cmd_service_direct_read(8)
    assert encoding is CvEncoding.SERVICE_DIRECT
    assert page == 0


def test_service_read_telegram_never_sends_direct_opcode_above_cv255(bench_factory):
    """cv <= 255 gates step 2 even when service_direct_cv is True: CV265 has
    no valid direct-mode wire form at all (direct_cv_byte refuses it)."""
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "pom" in caught.value.hint.lower()


def test_service_read_telegram_uses_extended_opcode_for_cv265_with_only_that_capability(
    bench_factory,
):
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(265)

    assert telegram.hex(" ").upper().startswith("22 19 09")
    assert telegram == cmd_service_ext_read(265)
    assert encoding is CvEncoding.SERVICE_EXT
    assert page == 1


def test_service_read_telegram_with_no_encoding_probed_names_the_bound_and_suggests_doctor(
    bench_factory,
):
    """An unprobed station never sends an opcode that has not been observed
    to work: None is not enough, only True is.

    The TYPE is the point, not just the message (issue #16). CV8 is plainly
    in range and the identical call succeeds once a probe has run, so
    `CvOutOfRangeError` named a cause that does not exist and sent a reader
    hunting the CV arithmetic in `xbus/cv.py` - the one place in this
    codebase where an off-by-one silently corrupts a decoder. A caller must
    be able to tell "your CV number is wrong", which the user fixes by typing
    another number, from "this station has not been probed", which the user
    fixes by probing.
    """
    capabilities = make_capabilities(
        z21_cv_opcodes=None, service_direct_cv=None, service_ext_cv=None
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(ServiceEncodingUnknownError) as caught:
        bench.station.programmer.service_read_telegram(8)
    assert str(MAX_CV_DIRECT) in str(caught.value)
    assert "doctor" in caught.value.hint
    assert caught.value.cv == 8
    # Not a range error, and not catchable as one: the whole point is that a
    # `except CvOutOfRangeError` must no longer swallow this case.
    assert not isinstance(caught.value, CvOutOfRangeError)


def test_service_read_telegram_when_the_cv_exceeds_every_available_encoding_suggests_pom(
    bench_factory,
):
    capabilities = make_capabilities(
        z21_cv_opcodes=False, service_direct_cv=True, service_ext_cv=False
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "pom" in caught.value.hint.lower()


def test_exit_service_mode_retries_once_then_raises_station_busy_error(bench):
    bench.expect(cmd_track_power_on(), reply=ACK)  # attempt 1
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)  # attempt 2 (the retry)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)

    with pytest.raises(StationBusyError):
        bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=False)
    assert bench.transport.script_pending == []


def test_exit_service_mode_succeeds_on_the_second_attempt(bench):
    """The retry itself, not just its two ends: an implementation that gives
    up after the FIRST resume-operations telegram would leave the second
    scripted exchange unconsumed, which `script_pending` below catches -
    a fails-after-two-attempts test alone cannot tell a real retry from a
    single attempt that happens to time out the same way."""
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=False)

    assert bench.transport.script_pending == []


def test_exit_service_mode_does_not_power_off_when_track_was_powered_before(bench):
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=False)

    # No track_power_off exchange is scripted: if the implementation sent one
    # anyway, FakeTransport would raise its own AssertionError ("unexpected
    # request") before this line runs.
    assert bench.transport.script_pending == []


def test_exit_service_mode_powers_off_when_track_was_unpowered_before(bench):
    """The measured state of this hardware is an unpowered bench track
    (docs/probe-results.md): resume-operations always re-energises the main
    track, and the station's start mode is automatic, so skipping this
    would start every locomotive moving at its last speed."""
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)
    bench.expect(cmd_track_power_off(), reply=ACK)

    bench.station.programmer.exit_service_mode(restore_power=False, restore_hold=False)

    assert bench.transport.script_pending == []


def test_exit_service_mode_puts_back_a_hold_the_session_found(bench):
    """Resume-operations is the telegram that CLEARS an emergency stop - MEASURED
    2026-08-09, run 5, where a locomotive held with step 80 stored accelerated away on
    it. A session that started on a held layout must therefore hold it again before it
    returns, or every service-mode command silently releases the layout.
    """
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_emergency_stop_all(), reply=ACK)

    bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=True)

    assert bench.transport.script_pending == []


def test_exit_service_mode_holds_before_it_powers_the_track_back_off(bench):
    """The order is the measurement, not a preference: runs 1 and 2 measured that a
    stop telegram sent to a DEAD track changes nothing at all, and runs 3 and 4 that
    the same telegram held stored steps 15 and 80 on a live one. The ordered script
    below fails if the two are swapped."""
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)
    bench.expect(cmd_emergency_stop_all(), reply=ACK)
    bench.expect(cmd_track_power_off(), reply=ACK)

    bench.station.programmer.exit_service_mode(restore_power=False, restore_hold=True)

    assert bench.transport.script_pending == []


def test_a_service_read_on_a_held_layout_leaves_it_held(bench_factory):
    """The whole path, not just the exit: `service_read` reads the status BEFORE it
    opens the session, and that reading is what decides whether the hold goes back.
    `0x05` is the measured held-and-live byte - bit 0 is emergency stop on this
    hardware, the reverse of the Lenz spec."""
    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    held_and_live = encode(0x62, 0x22, 0x05)
    bench.expect(STATUS_REQUEST, reply=held_and_live)
    bench.expect(cmd_service_direct_read(8), reply=ACK)
    bench.expect(POLL, reply=encode(0x63, 0x14, 0x08, 145))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_emergency_stop_all(), reply=ACK)

    bench.station.programmer.service_read(8)

    assert bench.transport.script_pending == []


def test_exit_service_mode_every_exchange_uses_the_programming_timeout(bench):
    """Nothing is scripted for the resume-operations reply, so the exchange
    times out - the fake clock is what proves the BUDGET it was given, not a
    fixed reply. At `li_ack_normal` (5.0 s) this would time out nineteen
    times sooner than the measured 95 s regime."""
    bench.expect(cmd_track_power_on(), reply=b"")  # silent on purpose

    started = bench.clock.monotonic()
    with pytest.raises(LinkTimeout):
        bench.station.programmer.exit_service_mode(restore_power=False, restore_hold=False)
    elapsed = bench.clock.monotonic() - started

    assert elapsed == pytest.approx(TIMING.li_ack_programming, abs=0.5)


def test_exit_service_mode_always_invalidates_the_page_cache(bench, monkeypatch):
    """The decoder on the programming track is not necessarily the one on
    the main track."""
    calls = []
    monkeypatch.setattr(bench.station, "invalidate_caches", lambda: calls.append(None))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=False)

    assert len(calls) == 1


def test_exit_service_mode_invalidates_the_cache_even_when_it_raises(bench, monkeypatch):
    calls = []
    monkeypatch.setattr(bench.station, "invalidate_caches", lambda: calls.append(None))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)

    with pytest.raises(StationBusyError):
        bench.station.programmer.exit_service_mode(restore_power=True, restore_hold=False)
    assert len(calls) == 1


def _script_read_and_clean_exit(bench, read_telegram: bytes, *, read_reply: bytes = ACK) -> None:
    """Every `service_read` test below needs the same shape around the one
    call whose outcome differs: the power-before status check, the read
    telegram itself, and `exit_service_mode`'s own resume-operations
    exchange and status check (service_mode already False, track already
    powered, so the exit path in every one of these tests takes exactly one
    attempt and sends no power-off). Factored out so each test scripts only
    what makes it different - the read telegram's own reply, or the stubbed
    `await_result` outcome."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(read_telegram, reply=read_reply)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)


def test_the_service_window_uses_the_programming_timeout_on_the_telegram_and_every_poll(
    bench_factory, monkeypatch
):
    """Running a service read at 5 s and polling every 0.5 s sends a new
    command while the previous one is unacknowledged and desynchronises the
    link. service_poll_interval is a minimum GAP between polls, never a
    reply deadline - that gap is await_result's job, not re-measured here;
    this test only pins the BUDGET every call into await_result gets. The
    read telegram's own timeout is pinned separately, below, because a
    stubbed await_result never exercises it."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    recorded: dict[str, object] = {}

    def fake_await_result(matcher, **kwargs):
        recorded.update(kwargs)
        return CvValue(raw_cv=8, value=42, ident=0x14, z21_form=True)

    monkeypatch.setattr(bench.station.programmer, "await_result", fake_await_result)

    bench.station.programmer.service_read(8)

    assert recorded["timeout"] == TIMING.service_result
    assert recorded["exchange_timeout"] == TIMING.li_ack_programming
    assert recorded["first_delay"] == TIMING.service_first_poll_delay
    assert recorded["interval"] == TIMING.service_poll_interval
    assert recorded["allow_poll"] is True
    assert recorded["ready_means_done"] is False
    assert recorded["context"] == "service"


def test_the_read_telegram_itself_uses_the_programming_timeout(bench_factory):
    """Nothing is scripted for the read telegram's own reply, so this
    exchange times out - proving the BUDGET it was given, not a fixed reply.
    `exit_service_mode` still runs from the `finally` afterwards, so its own
    two exchanges are scripted too."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(8), reply=b"")  # silent on purpose
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    started = bench.clock.monotonic()
    with pytest.raises(LinkTimeout):
        bench.station.programmer.service_read(8)
    elapsed = bench.clock.monotonic() - started

    assert elapsed == pytest.approx(TIMING.li_ack_programming, abs=0.5)


def test_the_station_rejecting_the_read_opcode_raises_unsupported_command_error(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8), read_reply=UNSUPPORTED)
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda *a, **k: pytest.fail("must not be reached")
    )

    with pytest.raises(UnsupportedCommandError):
        bench.station.programmer.service_read(8)


def test_a_no_ack_outcome_raises_decoder_no_ack_and_still_runs_exit_once(
    bench_factory, monkeypatch
):
    """Two must-pins share one script. exit_service_mode always runs, even
    when the read raises - proved by `script_pending` being empty, which it
    would not be if the exit exchanges were left unconsumed. And a service
    read is never retried automatically: they already cost up to 95 s and
    the station retries internally, so exactly one read telegram is
    scripted - a second identical request would not match the next scripted
    exchange (the exit-mode resume-operations telegram), and FakeTransport
    would raise its own AssertionError."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    # Known history: these pin the RAISE semantics. A fresh instance retries a
    # first-session 61 13 once (tests in test_cv_write.py); with the real exit
    # path scripted here, an unwanted retry shows up as an exhausted script.
    bench.station.programmer._session_history_unknown = False
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: NoAck())

    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.service_read(8)
    assert bench.transport.script_pending == []


def test_a_no_ack_hint_names_the_wheels_and_the_retry_never_pom(bench_factory, monkeypatch):
    """The old hint said "use POM instead" - advice into a mode whose READ is silence on
    this bench (R1), so it sent the operator somewhere that cannot verify. And the M8
    acceptance measured that a `61 13` can be the tool's own inter-session timing, which
    is retried automatically before this error is raised - so by the time an operator
    reads this hint, the retry has run and the likeliest remaining cause is contact."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    programmer = bench.station.programmer
    programmer._session_history_unknown = False  # the raise path, not the retry path
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(programmer, "await_result", lambda matcher, **kw: NoAck())

    with pytest.raises(DecoderNoAckError) as caught:
        programmer.service_read(8)
    hint = caught.value.hint.lower()
    assert "wheels" in hint
    assert "retry already ran" in hint
    assert "use pom instead" not in hint


def test_a_short_circuit_raises_short_circuit_error(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda matcher, **kw: ShortCircuit()
    )

    with pytest.raises(ShortCircuitError):
        bench.station.programmer.service_read(8)


def test_a_busy_state_raises_station_busy_error_not_decoder_not_responding(
    bench_factory, monkeypatch
):
    """61 1F means a programming operation is already running - a real,
    definitive signal, not silence, so it must not be reported the same way
    as no answer at all."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: Busy())

    with pytest.raises(StationBusyError):
        bench.station.programmer.service_read(8)


def test_a_timed_out_result_that_saw_a_no_ack_raises_decoder_no_ack(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    # Known history: these pin the RAISE semantics. A fresh instance retries a
    # first-session 61 13 once (tests in test_cv_write.py); with the real exit
    # path scripted here, an unwanted retry shows up as an exhausted script.
    bench.station.programmer._session_history_unknown = False
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: TimedOut(polls=6, ready_streak=0, saw_no_ack=True),
    )

    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.service_read(8)


def test_a_timed_out_result_with_nothing_seen_raises_decoder_not_responding(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: TimedOut(polls=6, ready_streak=8, saw_no_ack=False),
    )

    with pytest.raises(DecoderNotRespondingError):
        bench.station.programmer.service_read(8)


def test_an_unexpected_reply_from_await_result_raises_decoder_not_responding(
    bench_factory, monkeypatch
):
    """Totality: any reply shape service_read does not recognise is treated
    as unresolved, never silently accepted as success."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: GENERIC_ACK)

    with pytest.raises(DecoderNotRespondingError):
        bench.station.programmer.service_read(8)


def test_paged_cv_value_for_cv1_to_8_raises_decoder_not_responding_with_register_wording(
    bench_factory, monkeypatch
):
    """23151 3.1.2.6: 63 10 means the station fell back to register mode.
    Register numbers 1-8 are indistinguishable from CV numbers 1-8, so the
    value is never usable there, whatever the register byte says."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=8, value=5),
    )

    with pytest.raises(DecoderNotRespondingError) as caught:
        bench.station.programmer.service_read(8)
    assert "register" in str(caught.value)
    assert bench.station.capabilities.service_direct_cv is False


def test_paged_cv_value_above_cv8_is_accepted_when_the_register_echoes_the_cv(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(9))
    # CV9's only direct-mode echo candidate is 9 (xbus.cv.echo_candidates).
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=9, value=200),
    )

    result = bench.station.programmer.service_read(9)

    assert result.value == 200
    assert result.encoding is CvEncoding.SERVICE_DIRECT
    assert bench.station.capabilities.service_direct_cv is False


def test_paged_cv_value_above_cv8_with_a_mismatched_register_raises_without_register_wording(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(9))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=200, value=1),
    )

    with pytest.raises(DecoderNotRespondingError) as caught:
        bench.station.programmer.service_read(9)
    assert "register" not in str(caught.value)


def test_paged_cv_value_above_max_cv_direct_raises_decoder_not_responding_not_cv_out_of_range(
    bench_factory, monkeypatch
):
    """CV265 sits above the direct-mode range (1..255, `MAX_CV_DIRECT`), so
    `echo_candidates(SERVICE_DIRECT, 265)` would itself raise
    `CvOutOfRangeError` if it were ever called for this CV. A `63 10` paged
    reply is a real station answer, not an invalid CV number - the operator's
    CV265 was fine, the station just fell back to register mode - so this
    must still raise `DecoderNotRespondingError` (exit 13), never let the
    direct-mode range check's own `CvOutOfRangeError` (exit 15) escape and
    blame the CV number for what was a station-side fallback."""
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_service_ext_read(265))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=9, value=200),
    )

    with pytest.raises(DecoderNotRespondingError) as caught:
        bench.station.programmer.service_read(265)
    assert "register" not in str(caught.value)


def test_a_successful_read_reports_service_mode_the_encoding_and_no_verification(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))

    def fake_await_result(matcher, **kw):
        bench.clock.advance(1.7)  # measured: about 1.7 s for one real service read
        return CvValue(raw_cv=8, value=145, ident=0x14, z21_form=True)

    monkeypatch.setattr(bench.station.programmer, "await_result", fake_await_result)

    result = bench.station.programmer.service_read(8)

    assert result.cv == 8
    assert result.value == 145
    assert result.mode is ProgMode.SERVICE
    assert result.encoding is CvEncoding.Z21_16BIT
    assert result.verified is None
    assert result.elapsed == pytest.approx(1.7)


def test_a_read_in_an_unexercised_band_emits_a_note(bench_factory, monkeypatch):
    """CV600 is band 2 (63 16) - documented but never answered on this
    station. The read still succeeds mechanically; the note is what stops
    the JSON output implying it was verified."""
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_service_ext_read(600))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=88, value=1, ident=0x16, z21_form=False),
    )

    bench.station.programmer.service_read(600)

    assert bench.events == [("cv.unexercised_band", {"cv": 600, "page": 2})]


def test_a_read_in_an_exercised_band_emits_no_note(bench_factory, monkeypatch):
    """CV265 is band 1 (63 15) - measured and answered on this hardware."""
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_service_ext_read(265))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=9, value=1, ident=0x15, z21_form=False),
    )

    bench.station.programmer.service_read(265)

    assert bench.events == []


#: A CV inside `INDEXED_CV_RANGE`, where the CV31/CV32 pair decides what the
#: number means and an unselected page is therefore worth a warning.
INDEXED_CV = 265


def test_service_read_emits_page_not_selected_when_a_page_is_given(bench_factory, monkeypatch):
    """`service_read` cannot select a page yet: `select_page` over SERVICE
    routes through `_write_and_confirm`'s SERVICE branch, whose
    `service_write_telegram`/`_track_power` are added by Task 6b, not this
    one. A caller-supplied page must not be silently dropped in the
    meantime - this is what stops a CV265 read under an assumed page from
    quietly reading whatever page the decoder already had selected.
    """
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(INDEXED_CV))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=INDEXED_CV, value=1, ident=0x14, z21_form=True),
    )

    bench.station.programmer.service_read(INDEXED_CV, page=(10, 2))

    assert bench.events == [
        ("page.not_selected", {"cv": INDEXED_CV, "page": (10, 2), "mode": "service"})
    ]
    # `Station.emit` never validates its own `name` argument (facade.py), so a
    # typo or an unregistered event name would still show up in `bench.events`
    # above and this assertion is the only thing that would catch it: it must
    # be a member of the pinned `EVENT_NAMES` tuple or no renderer will ever
    # reach it through the CLI.
    assert bench.events[0][0] in EVENT_NAMES


def test_service_read_emits_nothing_about_pages_when_none_is_given(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=8, value=1, ident=0x14, z21_form=True),
    )

    bench.station.programmer.service_read(8)

    assert bench.events == []


def test_cv_read_in_service_mode_with_a_page_still_emits_not_selected(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True, pom_read=False)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(INDEXED_CV))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=INDEXED_CV, value=1, ident=0x14, z21_form=True),
    )

    bench.station.programmer.cv_read(INDEXED_CV, mode=ProgMode.SERVICE, page=(10, 2))

    assert bench.events == [
        ("page.not_selected", {"cv": INDEXED_CV, "page": (10, 2), "mode": "service"})
    ]
    assert bench.events[0][0] in EVENT_NAMES


def test_a_plain_cv_read_under_a_page_says_nothing_about_pages(bench_factory, monkeypatch):
    """The other half of the rule, and the reason for it.

    Below CV257 the page argument is ignored by every layer that handles it,
    so a warning that it was not selected reports a non-event. Measured at the
    bench on 2026-08-18: `restore` pins each verification read to the file's
    page - which is what keeps a read-back on the same bank as its write - and
    a plain CV3 read-back came back decorated with `page.not_selected`, which
    an operator cannot tell from a page that genuinely did not take.
    """
    capabilities = make_capabilities(z21_cv_opcodes=True, pom_read=False)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(3))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=3, value=26, ident=0x14, z21_form=True),
    )

    bench.station.programmer.cv_read(3, mode=ProgMode.SERVICE, page=(0, 1))

    assert bench.events == []


def test_a_real_63_10_reply_drives_paged_cv_value_through_the_unstubbed_loop(bench_factory):
    """Every outcome test above stubs `await_result` and never drives a real
    `PagedCvValue` through the wait loop. Task 4's own `_consider` must
    return `PagedCvValue` as a TERMINAL outcome for this branch to ever
    complete (2.24 / 5h) - without that fix this test times out as
    `TimedOut` instead of returning a result, because `_consider` silently
    discards `PagedCvValue` the same way it discards `GenericAck` and
    `Other`.

    `broadcast=` on the poll's own `expect()`, not a standalone `push()`:
    a `push()`ed frame queued before this call would be silently discarded
    by `Link.request()`'s own leading `drain()` before the read telegram is
    even sent (link.py) - the same reasoning as
    `test_cv_pom.py::test_a_mismatched_cv_value_answered_inline_is_reported_stale_and_the_wait_continues`.
    """
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_service_direct_read(9), reply=ACK)
    bench.expect(
        POLL,
        reply=UNSUPPORTED,  # a poll may still answer 61 82
        broadcast=encode(0x63, 0x10, 9, 200),  # the real answer arrives unsolicited
    )
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    result = bench.station.programmer.service_read(9)

    assert result.value == 200
    assert result.encoding is CvEncoding.SERVICE_DIRECT
    assert bench.station.capabilities.service_direct_cv is False


def test_cv_read_uses_pom_when_the_resolved_mode_is_pom(bench_factory, monkeypatch):
    capabilities = make_capabilities(pom_read=True)
    bench = bench_factory(capabilities=capabilities)
    monkeypatch.setattr(
        bench.station.programmer,
        "pom_read",
        lambda cv, *, address, page=None: "POM-RESULT",
    )
    monkeypatch.setattr(
        bench.station.programmer,
        "service_read",
        lambda cv, *, page=None: pytest.fail("must not be reached"),
    )

    assert bench.station.programmer.cv_read(8, address=3) == "POM-RESULT"


def test_cv_read_uses_service_mode_when_the_resolved_mode_is_service(bench_factory, monkeypatch):
    capabilities = make_capabilities(pom_read=False, service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)
    monkeypatch.setattr(
        bench.station.programmer,
        "pom_read",
        lambda cv, *, address, page=None: pytest.fail("must not be reached"),
    )
    monkeypatch.setattr(
        bench.station.programmer,
        "service_read",
        lambda cv, *, page=None: "SERVICE-RESULT",
    )

    assert bench.station.programmer.cv_read(8) == "SERVICE-RESULT"


def test_station_cv_read_delegates_to_the_programmer(bench, monkeypatch):
    recorded: dict[str, object] = {}

    def fake_cv_read(self, cv, *, address=None, mode=ProgMode.AUTO, page=None):
        recorded.update(cv=cv, address=address, mode=mode, page=page)
        return "DELEGATED"

    monkeypatch.setattr(CvProgrammer, "cv_read", fake_cv_read)

    result = bench.station.cv_read(265, address=3, mode=ProgMode.SERVICE, page=(1, 2))

    assert result == "DELEGATED"
    assert recorded == {
        "cv": 265,
        "address": 3,
        "mode": ProgMode.SERVICE,
        "page": (1, 2),
    }


def test_service_read_many_opens_one_session_for_every_cv(bench_factory, monkeypatch):
    """Three CVs, one exit - not one exit per CV.

    Measured on the bench 2026-08-07 (issue #22): a decoder that answers
    inside an open service-mode session stops answering when the session is
    reopened immediately. The doctor's D9 read nine identity CVs as nine
    sessions and got one value and eight `61 13`s, while D5-D7's four reads
    inside a single session all succeeded on the same run.

    The proof is the script, not an assertion: exactly one
    resume-operations exchange is scripted, and `FakeTransport` raises on any
    unscripted request - so a second exit fails the test where it happens,
    naming the telegram. `await_result` is stubbed for the same reason the
    tests above stub it: this is about session boundaries, not polling.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)  # power_before, read once
    for cv in (7, 8, 250):
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)  # the ONLY exit
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    values = iter((5, 145, 6))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: CvValue(
            raw_cv=matcher.cv, value=next(values), ident=0x14, z21_form=True
        ),
    )

    outcomes = bench.station.programmer.service_read_many([7, 8, 250])

    assert [outcome.spec.cv for outcome in outcomes] == [7, 8, 250]
    assert [outcome.result.value for outcome in outcomes] == [5, 145, 6]
    assert all(outcome.error is None for outcome in outcomes)
    assert bench.transport.script_pending == []


def test_service_read_many_keeps_the_session_open_after_one_cv_fails(bench_factory, monkeypatch):
    """A failing CV must not close the session or end the batch.

    D9 reads nine CVs and a decoder that answers none of them is an ordinary
    outcome, not an error to abort on - `cv_read_many` already reports
    partial failure the same way, one error per outcome. If a failure closed
    the session, the CVs after it would each reopen one and the fix above
    would be undone by its own error path.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    for cv in (7, 8):
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    outcomes_by_cv = {7: NoAck(), 8: CvValue(raw_cv=8, value=145, ident=0x14, z21_form=True)}
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: outcomes_by_cv[matcher.cv],
    )

    outcomes = bench.station.programmer.service_read_many([7, 8])

    assert outcomes[0].result is None
    assert isinstance(outcomes[0].error, DecoderNoAckError)
    assert outcomes[1].result is not None and outcomes[1].result.value == 145
    assert bench.transport.script_pending == []


def test_a_second_service_session_waits_out_the_gap(bench_factory, monkeypatch):
    """A session opened too soon after the previous one closed fails on the
    real station - every CV in it answers `61 13`, the first one included.
    Measured 2026-08-07 (issue #22): 0.0 to 1.5 s all failed, 1.75 s and
    above worked.

    The fake clock is the instrument here, exactly as in the exit-timeout
    tests above: nothing about the reply changes, only how much time the
    programmer spends before sending the second read.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    for _ in range(2):
        _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: CvValue(raw_cv=8, value=145, ident=0x14, z21_form=True),
    )

    bench.station.programmer.service_read(8)
    after_first = bench.clock.monotonic()
    bench.station.programmer.service_read(8)

    assert bench.clock.monotonic() - after_first >= TIMING.service_session_gap


def test_the_session_gap_is_paid_once_per_session_not_once_per_cv(bench_factory, monkeypatch):
    """Three CVs in one batch owe one gap between them and the session
    before, not three. Paying per CV would undo the batching this method
    exists for and add six seconds to every doctor run for nothing."""
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    for cv in (7, 8, 250):
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: CvValue(raw_cv=matcher.cv, value=1, ident=0x14, z21_form=True),
    )

    bench.station.programmer.service_read(8)
    after_first = bench.clock.monotonic()
    bench.station.programmer.service_read_many([7, 8, 250])
    elapsed = bench.clock.monotonic() - after_first

    assert elapsed >= TIMING.service_session_gap
    assert elapsed < 2 * TIMING.service_session_gap
    assert bench.transport.script_pending == []


def test_service_read_many_stops_the_batch_on_a_short_circuit(bench_factory, monkeypatch):
    """A shorted programming track ends the batch; it is not one CV's news.

    Every CV after it would fail for the same reason, so continuing buries
    the fault under copies of its own consequences and sends telegrams that
    cannot work. The CVs after the short are absent from the result rather
    than present and failed - "not attempted" and "attempted and failed" are
    different facts, which is why `CvReadOutcome` keeps `result` and `error`
    independent.

    The session still closes exactly once: only one resume-operations
    exchange is scripted, and `FakeTransport` raises on anything unscripted.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(7), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: ShortCircuit(),
    )

    outcomes = bench.station.programmer.service_read_many([7, 8, 250])

    assert isinstance(outcomes[0].error, ShortCircuitError)
    # The CVs after it are reported as NOT ATTEMPTED - both fields None -
    # rather than dropped from the list or handed a copy of the short
    # circuit they never met. `CvReadOutcome`'s docstring reserves that
    # combination for exactly this case.
    assert [(o.spec.cv, o.result, o.error) for o in outcomes[1:]] == [
        (8, None, None),
        (250, None, None),
    ]
    assert bench.transport.script_pending == []


def test_service_read_many_with_no_cvs_opens_no_session(bench_factory):
    """An empty batch must not open a session it does not need.

    Opening and closing one would stamp `_last_session_end`, and the next
    real read would then wait out a three-second gap it does not owe. Nothing
    is scripted here: `FakeTransport` raises on the first unscripted request,
    so any telegram at all fails the test.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))

    assert bench.station.programmer.service_read_many([]) == []
    assert bench.sent == []


def test_service_read_many_stops_the_batch_on_a_link_timeout(bench_factory, monkeypatch):
    """A dead link ends the batch on cost, not on certainty.

    A link that times out might in principle recover, unlike a shorted track.
    But each attempt costs `li_ack_programming` (95 s), so letting nine
    identity CVs each find out would hang a doctor run for a quarter of an
    hour to report what the first failure already said.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(7), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    def timeout(matcher, **kwargs):
        raise LinkTimeout("link gone")

    monkeypatch.setattr(bench.station.programmer, "await_result", timeout)

    outcomes = bench.station.programmer.service_read_many([7, 8, 250])

    assert isinstance(outcomes[0].error, LinkTimeout)
    assert [(o.result, o.error) for o in outcomes[1:]] == [(None, None), (None, None)]
    assert bench.transport.script_pending == []


def test_one_rejected_encoding_does_not_make_the_others_count_as_probed(bench_factory):
    """`z21_cv_opcodes=False` with the other two still unknown is an UNPROBED
    station, not a limited one.

    The gate used to be "all three are None", so a single `61 82` recorded
    against one encoding skipped this branch entirely and produced the
    out-of-reach message below - which states that direct opcodes work and
    cover CV1..255, about a capability nobody had measured. Naming a state
    the station never reported is the defect issue #16 exists to remove; this
    is the same defect one step further in, and reachable whenever a probe
    settles one encoding and stops.
    """
    capabilities = make_capabilities(
        z21_cv_opcodes=False, service_direct_cv=None, service_ext_cv=None
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(ServiceEncodingUnknownError) as caught:
        bench.station.programmer.service_read_telegram(8)
    assert "doctor" in caught.value.hint
    assert "service_direct_cv" in str(caught.value)  # names what is still unprobed
    assert "service_ext_cv" in str(caught.value)
    assert "z21_cv_opcodes" not in str(caught.value)  # that one WAS answered


def test_a_station_that_rejected_every_encoding_is_not_described_as_unprobed(bench_factory):
    """All three answered `61 82`: nothing is unknown, so running the doctor
    again changes nothing and the message must not send the operator there.

    Same type as the out-of-reach case on purpose - they share a remedy,
    which is this file's test for whether two failures deserve one type.
    Neither is fixed by probing; both are fixed by POM or another CV.
    """
    capabilities = make_capabilities(
        z21_cv_opcodes=False, service_direct_cv=False, service_ext_cv=False
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(8)
    assert "pom" in caught.value.hint.lower()
    assert "rejected every service-mode opcode" in str(caught.value)
    assert "doctor" not in str(caught.value)
    assert not isinstance(caught.value, ServiceEncodingUnknownError)


def test_an_unreachable_cv_is_not_sent_to_pom_when_pom_is_measured_unsupported(bench_factory):
    """`use --mode pom` is advice, not a reflex.

    A caller reaching this raise through AUTO got here BECAUSE `pom_read` is
    False - that is the only way AUTO resolves to service mode - so naming POM
    would send them back to the path that sent them here. The remedy is the
    part of an error most likely to be acted on, and a remedy the station has
    already ruled out is worse than none.
    """
    capabilities = make_capabilities(
        pom_read=False, service_direct_cv=True, z21_cv_opcodes=False, service_ext_cv=False
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "pom is unsupported" in caught.value.hint.lower()
    assert "use `--mode pom`" not in caught.value.hint


def test_an_unreachable_cv_still_suggests_pom_while_pom_remains_possible(bench_factory):
    """The companion: with `pom_read` unprobed or proven, POM is still worth
    trying and the advice stays as it was. Only a measured False withdraws
    it."""
    capabilities = make_capabilities(
        service_direct_cv=True, z21_cv_opcodes=False, service_ext_cv=False
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "use `--mode pom`" in caught.value.hint


# -- cv_read_many over service mode: one session per group (issue #38) ---------


def _service_bench(bench_factory):
    """A bench whose programmer already KNOWS its session history.

    A fresh programmer does not - it cannot know when the previous CLI
    invocation's session closed - so `cv_read_many` reads the first spec on
    its own through `service_read`, which carries the `61 13` retry. That
    branch has its own test below; every other test here is about the
    grouping, so the flag is set the way a programmer that has already closed
    a session in this process would have it.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    bench.station.programmer._session_history_unknown = False
    return bench


def _script_one_session(bench, cvs) -> None:
    """One whole service-mode session: the power-before status check, one read
    telegram per CV, and `exit_service_mode`'s resume-operations exchange and
    status check (service mode already left, track already powered, so no
    second attempt and no power-off).

    A further session is deliberately NOT scripted - `FakeTransport` raises on
    the first unscripted request, so one opened by mistake fails the test
    where it happens, naming the telegram.
    """
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    for cv in cvs:
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)


def _cv_result(*, cv: int, value: int) -> CvResult:
    """What a stubbed `cv_read` hands back on the per-CV path, where the tests
    below care about WHICH reads happened, not about the values."""
    return CvResult(
        cv=cv,
        value=value,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.Z21_16BIT,
        operation="read",
        verified=None,
        elapsed=0.0,
    )


def _answer_every_read(bench, monkeypatch) -> None:
    """Every read answers with its own CV number as the value. Stubbed for the
    same reason the tests above stub it: these are about session boundaries,
    not about polling."""
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: CvValue(
            raw_cv=matcher.cv, value=matcher.cv, ident=0x14, z21_form=True
        ),
    )


def test_cv_read_many_in_service_mode_opens_one_session_for_the_whole_group(
    bench_factory, monkeypatch
):
    """Three specs, one session - not one session per spec.

    This is issue #38's whole point: `cv_read_many` used to loop over
    `cv_read`, and in service mode each of those opened and closed its own
    session. Exactly one resume-operations exchange is scripted, so a second
    session fails the test where it happens.
    """
    bench = _service_bench(bench_factory)
    _script_one_session(bench, (7, 8, 250))
    _answer_every_read(bench, monkeypatch)

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=250), CvSpec(cv=7), CvSpec(cv=8)], mode=ProgMode.SERVICE
    )

    assert [outcome.spec.cv for outcome in outcomes] == [7, 8, 250]
    assert [outcome.result.value for outcome in outcomes] == [7, 8, 250]
    assert bench.transport.script_pending == []


def test_cv_read_many_in_service_mode_keeps_the_caller_s_spec_on_every_outcome(
    bench_factory, monkeypatch
):
    """The outcome carries the spec the CALLER passed, name included.

    `service_read_many` knows CV numbers only, so it reports a bare
    `CvSpec(cv=...)`. A backup row is written under the catalog slug that
    lives on the caller's spec, so an outcome that came back with the bare
    spec would lose the name the file is keyed by.
    """
    bench = _service_bench(bench_factory)
    _script_one_session(bench, (7, 8))
    _answer_every_read(bench, monkeypatch)

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=7, name="accel"), CvSpec(cv=8, name="reset")], mode=ProgMode.SERVICE
    )

    assert [outcome.spec.name for outcome in outcomes] == ["accel", "reset"]


def test_cv_read_many_in_service_mode_pays_the_session_gap_once_for_the_group(
    bench_factory, monkeypatch
):
    """The saving, on the clock: three CVs through `cv_read_many` owe ONE
    `service_session_gap`, not one per CV.

    Measured 2026-08-13 (issue #38): a backup cost 6.05 s per CV, about half
    of it this gap, waited out because every CV opened its own session. The
    fake clock is the instrument, exactly as in
    `test_a_second_service_session_waits_out_the_gap`.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    _script_one_session(bench, (7, 8, 250))
    _answer_every_read(bench, monkeypatch)

    bench.station.programmer.service_read(8)
    after_first = bench.clock.monotonic()
    bench.station.programmer.cv_read_many(
        [CvSpec(cv=7), CvSpec(cv=8), CvSpec(cv=250)], mode=ProgMode.SERVICE
    )
    elapsed = bench.clock.monotonic() - after_first

    assert elapsed >= TIMING.service_session_gap
    assert elapsed < 2 * TIMING.service_session_gap


def test_cv_read_many_opens_one_session_per_group_of_service_batch_size(bench_factory, monkeypatch):
    """Nine specs open exactly two sessions: eight CVs, then one.

    The group is bounded because a session holds the station in service mode:
    an interrupt, a short circuit or a station reset costs at most one group's
    progress. Both sessions are scripted in full, so a third one - or a group
    that ran long and read CV number 9 inside the first - fails the test.

    Nine and eight are written out rather than computed from
    `SERVICE_BATCH_SIZE`, and the constant is asserted against the literal
    here. A test that builds its fixture out of the constant it is pinning
    adapts to any value the constant is given and so cannot catch a change to
    it - which is exactly what a mutation run found on 2026-08-18, when
    setting the constant to 1 left this test green while seven of its
    neighbours went red.
    """
    assert SERVICE_BATCH_SIZE == 8
    bench = _service_bench(bench_factory)
    cvs = list(range(101, 110))
    _script_one_session(bench, cvs[:8])
    _script_one_session(bench, cvs[8:])
    _answer_every_read(bench, monkeypatch)

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=cv) for cv in cvs], mode=ProgMode.SERVICE
    )

    assert [outcome.spec.cv for outcome in outcomes] == cvs
    assert bench.sent.count(cmd_track_power_on()) == 2
    assert bench.transport.script_pending == []


def test_cv_read_many_reports_progress_per_cv_from_inside_the_open_session(
    bench_factory, monkeypatch
):
    """One `on_progress` call per spec, in sorted order, numbered across the
    whole call - and every one of them BEFORE the session closes.

    `cli/commands/backup.py` builds a Ctrl-C partial file out of these
    callbacks alone, because the returned list dies with the interrupt. A
    callback delivered per group instead of per CV would lose up to a whole
    group of CVs that had already answered, and one delivered after the return
    would lose all of them. The proof of the timing is the resume-operations
    telegram: it is the last thing a session sends, so a callback that has not
    seen it yet ran inside the session.
    """
    bench = _service_bench(bench_factory)
    _script_one_session(bench, (7, 8, 250))
    _answer_every_read(bench, monkeypatch)
    progress: list[tuple[int, int, int, bool]] = []

    def record(update: tuple[int, int, CvReadOutcome]) -> None:
        index, total, outcome = update
        progress.append((index, total, outcome.spec.cv, cmd_track_power_on() in bench.sent))

    bench.station.programmer.cv_read_many(
        [CvSpec(cv=250), CvSpec(cv=7), CvSpec(cv=8)], mode=ProgMode.SERVICE, on_progress=record
    )

    assert progress == [(0, 3, 7, False), (1, 3, 8, False), (2, 3, 250, False)]


def test_one_failing_cv_does_not_end_the_batched_read_or_close_the_session(
    bench_factory, monkeypatch
):
    """A decoder that does not answer one CV is that CV's news, not the
    group's.

    The same rule `service_read_many` already applies inside one session, now
    reached through `cv_read_many`: the failure is recorded as that CV's error
    and the reads after it continue in the SAME session. One exit is
    scripted, so a failure that closed the session fails the test.
    """
    bench = _service_bench(bench_factory)
    _script_one_session(bench, (7, 8, 250))
    replies = {
        7: CvValue(raw_cv=7, value=5, ident=0x14, z21_form=True),
        8: NoAck(),
        250: CvValue(raw_cv=250, value=6, ident=0x14, z21_form=True),
    }
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda matcher, **kwargs: replies[matcher.cv]
    )

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=7), CvSpec(cv=8), CvSpec(cv=250)], mode=ProgMode.SERVICE
    )

    assert isinstance(outcomes[1].error, DecoderNoAckError)
    assert [outcome.result is not None for outcome in outcomes] == [True, False, True]
    assert bench.transport.script_pending == []


def test_a_short_circuit_in_the_first_group_leaves_every_later_cv_unattempted(
    bench_factory, monkeypatch
):
    """A batch-ending error stops the whole call, across groups.

    A shorted programming track cannot clear itself by opening the next
    session, so the CV that met it carries the error and every CV after it -
    the rest of its own group and all of the group that was never opened -
    comes back `result is None and error is None`. Not attempted is a
    different fact from failed: `backup/mapping.py::status_for` branches on
    exactly that, and copying the short circuit onto them would claim
    seventeen shorts where the station reported one.
    """
    bench = _service_bench(bench_factory)
    cvs = list(range(101, 101 + SERVICE_BATCH_SIZE + 1))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(cvs[0]), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda matcher, **kwargs: ShortCircuit()
    )
    progress: list[CvReadOutcome] = []

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=cv) for cv in cvs],
        mode=ProgMode.SERVICE,
        on_progress=lambda update: progress.append(update[2]),
    )

    assert isinstance(outcomes[0].error, ShortCircuitError)
    assert [(o.spec.cv, o.result, o.error) for o in outcomes[1:]] == [
        (cv, None, None) for cv in cvs[1:]
    ]
    assert [outcome.spec.cv for outcome in progress] == cvs
    assert bench.sent.count(cmd_track_power_on()) == 1
    assert bench.transport.script_pending == []


def test_a_short_circuit_on_the_last_cv_of_a_group_still_ends_the_batch(bench_factory, monkeypatch):
    """The batch ends on the ERROR, not on the tail the error leaves behind.

    `service_read_many` marks the CVs after a batch-ending error as not
    attempted by iterating `cvs[len(outcomes):]`, which is empty when the CV
    that met the error was the LAST of its group. Reading the abort off that
    tail therefore missed one failure position in every `SERVICE_BATCH_SIZE`,
    and the next session was opened onto a track the station had just
    reported shorted. Only the first session is scripted, so an
    implementation that reads the abort off the tail fails here.
    """
    bench = _service_bench(bench_factory)
    cvs = list(range(101, 101 + SERVICE_BATCH_SIZE + 1))
    _script_one_session(bench, cvs[:SERVICE_BATCH_SIZE])
    last_of_group = cvs[SERVICE_BATCH_SIZE - 1]
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: (
            ShortCircuit()
            if matcher.cv == last_of_group
            else CvValue(raw_cv=matcher.cv, value=matcher.cv, ident=0x14, z21_form=True)
        ),
    )

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=cv) for cv in cvs], mode=ProgMode.SERVICE
    )

    assert isinstance(outcomes[SERVICE_BATCH_SIZE - 1].error, ShortCircuitError)
    assert [(o.spec.cv, o.result, o.error) for o in outcomes[SERVICE_BATCH_SIZE:]] == [
        (cv, None, None) for cv in cvs[SERVICE_BATCH_SIZE:]
    ]
    assert bench.transport.script_pending == []


def _script_session_that_will_not_close(bench, cvs) -> None:
    """One session that answers every read and then refuses to leave service
    mode.

    `exit_service_mode` sends resume-operations twice, the station keeps
    reporting `62 22 08` both times, and it raises `StationBusyError` out of
    `service_read_many`'s `finally` - after every CV of the group already has
    an outcome. Nothing beyond that failing close is scripted, so a further
    session fails the test where it happens.
    """
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    for cv in cvs:
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)


def test_a_session_that_will_not_close_never_blames_the_next_group_s_first_cv(
    bench_factory, monkeypatch
):
    """A close that fails between groups leaves the next group NOT ATTEMPTED.

    Its first CV had no telegram sent for it, and `CvReadOutcome` reserves
    `result=None, error=None` for exactly that. Reported with the previous
    session's close error instead, `backup/mapping.py::status_for` writes it
    into the file as `error` - a decoder that was asked and failed - rather
    than as a CV the sweep never reached.
    """
    bench = _service_bench(bench_factory)
    cvs = list(range(101, 101 + SERVICE_BATCH_SIZE + 1))
    _script_session_that_will_not_close(bench, cvs[:SERVICE_BATCH_SIZE])
    _answer_every_read(bench, monkeypatch)

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=cv) for cv in cvs], mode=ProgMode.SERVICE
    )

    assert [(o.spec.cv, o.result, o.error) for o in outcomes[SERVICE_BATCH_SIZE:]] == [
        (cv, None, None) for cv in cvs[SERVICE_BATCH_SIZE:]
    ]
    assert [outcome.error for outcome in outcomes] == [None] * len(cvs)
    assert bench.transport.script_pending == []


def test_a_session_that_will_not_close_on_the_last_group_reports_the_failure(
    bench_factory, monkeypatch
):
    """A group that answered in full and then could not close says so.

    `StationBusyError` out of `exit_service_mode` means the station is still
    in service mode and the main track is dead. No CV failed, so no outcome
    can carry that - and returning a list of clean successes would let
    `railctl backup` write a complete file and exit 0 over a layout the run
    left wedged. The values are kept and the fault rides beside them as a
    `service.session_close_failed` warning, the same rule
    `cli/deps.py::close_after` applies to a link that will not close.
    """
    bench = _service_bench(bench_factory)
    _script_session_that_will_not_close(bench, (7, 8))
    _answer_every_read(bench, monkeypatch)

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=7), CvSpec(cv=8)], mode=ProgMode.SERVICE
    )

    payload = next(load for name, load in bench.events if name == "service.session_close_failed")
    # The GROUP, never one CV. A group that met a batch-ending error partway
    # through reported its own tail as not attempted, so naming "the CV it
    # failed after" would put a CV nobody read at the scene.
    assert payload["group"] == [7, 8]
    assert payload["not_attempted"] == 0
    assert [outcome.result.value for outcome in outcomes] == [7, 8]
    assert bench.transport.script_pending == []


def test_each_group_after_the_first_pays_one_session_gap_and_no_more(bench_factory, monkeypatch):
    """Nine CVs in two groups owe exactly ONE gap between them.

    The single-group test above proves the gap is not paid per CV; this one
    proves it is still paid per session once there is more than one session.
    A group boundary that skipped it would send the second session's first
    read inside the measured danger window (0.0 to 1.5 s all failed, issue
    #22), and one that paid it twice would give back a third of the saving.
    """
    bench = _service_bench(bench_factory)
    cvs = list(range(101, 110))
    _script_one_session(bench, cvs[:8])
    _script_one_session(bench, cvs[8:])
    _answer_every_read(bench, monkeypatch)

    start = bench.clock.monotonic()
    bench.station.programmer.cv_read_many([CvSpec(cv=cv) for cv in cvs], mode=ProgMode.SERVICE)
    elapsed = bench.clock.monotonic() - start

    assert elapsed >= TIMING.service_session_gap
    assert elapsed < 2 * TIMING.service_session_gap


def test_more_outcomes_than_cvs_is_named_rather_than_raised_as_stop_iteration(
    bench_factory, monkeypatch
):
    """The two counts disagreeing says which two, not `StopIteration`.

    `service_read_many` returns exactly one outcome per CV it was given, so
    this cannot happen while both halves agree. It is pinned because the
    failure it replaces is a bare `StopIteration` raised from inside a
    callback - an exception that names nothing and points nowhere.
    """
    bench = _service_bench(bench_factory)
    programmer = bench.station.programmer
    monkeypatch.setattr(
        programmer,
        "service_read_many",
        lambda cvs, *, on_result=None: [
            on_result(CvReadOutcome(spec=CvSpec(cv=cv), result=None, error=None))
            for cv in [*cvs, 999]
        ],
    )

    with pytest.raises(RuntimeError, match="more outcomes than the 1 CVs"):
        programmer.cv_read_many([CvSpec(cv=7)], mode=ProgMode.SERVICE)


def test_a_spec_that_carries_a_page_keeps_the_per_cv_path(bench_factory, monkeypatch):
    """Paging still reads CV31/CV32 first, selects, and restores.

    Service mode cannot select a page at all - `service_read` only emits
    `page.not_selected` - so the selection a paged spec needs happens in
    `cv_read_many`'s per-CV loop and nowhere else. `service_read_many` fails
    the test if it is reached: the batch must not quietly read a paged spec
    off whatever page the decoder happened to have selected.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        programmer,
        "service_read_many",
        lambda cvs, *, on_result=None: pytest.fail("the paged batch must not be grouped"),
    )
    monkeypatch.setattr(
        programmer,
        "select_page",
        lambda page, *, address, mode, force: calls.append(("select", page, force)),
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: (
            calls.append(("read", cv)),
            _cv_result(cv=cv, value=7 if cv == 31 else 3),
        )[1],
    )

    programmer.cv_read_many([CvSpec(cv=265, page=(10, 2))], mode=ProgMode.SERVICE)

    assert calls == [
        ("read", 31),
        ("read", 32),
        ("select", (10, 2), True),
        ("read", 265),
        ("select", (7, 3), True),
    ]


def test_pom_mode_still_reads_one_cv_at_a_time(bench_factory, monkeypatch):
    """The grouping is service mode's alone.

    POM has no session and no session gap; there is nothing to save and
    `service_read_many` sends service-mode telegrams, which is the wrong
    track. `resolve_mode` returning POM must keep the per-CV loop.
    """
    bench = bench_factory(capabilities=make_capabilities(pom_read=True))
    programmer = bench.station.programmer
    read: list[int] = []
    monkeypatch.setattr(
        programmer,
        "service_read_many",
        lambda cvs, *, on_result=None: pytest.fail("POM must not open a service session"),
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: (read.append(cv), _cv_result(cv=cv, value=1))[1],
    )

    programmer.cv_read_many([CvSpec(cv=8), CvSpec(cv=7)], address=3, mode=ProgMode.POM)

    assert read == [7, 8]


def test_an_unresolvable_mode_is_still_reported_once_per_spec(bench_factory, monkeypatch):
    """A `resolve_mode` failure must not escape `cv_read_many`.

    POM measured unsupported with no service encoding proven leaves AUTO with
    nowhere to go, and `resolve_mode` raises. That happened inside the per-CV
    loop before this change, so every spec came back with that error as its
    outcome; deciding the path up front must not turn it into an exception the
    caller never had to handle.
    """
    bench = bench_factory(capabilities=make_capabilities(pom_read=False))
    monkeypatch.setattr(
        bench.station.programmer,
        "service_read_many",
        lambda cvs, *, on_result=None: pytest.fail("no mode was resolved"),
    )

    outcomes = bench.station.programmer.cv_read_many([CvSpec(cv=7), CvSpec(cv=8)])

    assert [isinstance(outcome.error, PomReadUnsupportedError) for outcome in outcomes] == [
        True,
        True,
    ]
    assert bench.sent == []


def test_the_first_cv_of_an_invocation_is_read_alone_so_the_gap_retry_still_covers_it(
    bench_factory, monkeypatch
):
    """With the session history unknown, spec one goes through `service_read`.

    A fresh programmer cannot know when the previous CLI invocation's session
    closed, and a session opened too soon fails wholesale with `61 13` on
    every CV in it - `service_read` carries the one retry that separates that
    artefact from a decoder that really did not answer, and
    `service_read_many` does not. Retrying a whole GROUP instead would re-run
    reads the first attempt had already reported through `on_progress`, so a
    CV would arrive twice with two different outcomes.

    Three sessions are scripted: the first attempt that answers `61 13`, its
    retry, and the one group holding the remaining two CVs.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(7))
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(7))
    _script_one_session(bench, (8, 250))
    answers = iter([NoAck()])
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kwargs: next(
            answers, CvValue(raw_cv=matcher.cv, value=matcher.cv, ident=0x14, z21_form=True)
        ),
    )

    outcomes = bench.station.programmer.cv_read_many(
        [CvSpec(cv=7), CvSpec(cv=8), CvSpec(cv=250)], mode=ProgMode.SERVICE
    )

    assert [outcome.spec.cv for outcome in outcomes] == [7, 8, 250]
    assert [outcome.result.value for outcome in outcomes] == [7, 8, 250]
    assert "service.session_retried" in bench.event_names()
    assert bench.transport.script_pending == []


def test_service_read_many_hands_every_outcome_to_on_result_in_order(bench_factory, monkeypatch):
    """`on_result` fires once per element of the returned list, in list order,
    the not-attempted tail included.

    That is what lets `cv_read_many` report progress per CV while the session
    is still open. A tail delivered only in the return value would be missing
    from a Ctrl-C partial file, and a caller collecting incrementally would
    see a shorter list than the one the batch returns.
    """
    bench = _service_bench(bench_factory)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    for cv in (7, 8):
        bench.expect(cmd_z21_cv_read(cv), reply=ACK)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    replies = {
        7: CvValue(raw_cv=7, value=5, ident=0x14, z21_form=True),
        8: ShortCircuit(),
    }
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda matcher, **kwargs: replies[matcher.cv]
    )
    seen: list[CvReadOutcome] = []

    outcomes = bench.station.programmer.service_read_many([7, 8, 250], on_result=seen.append)

    assert seen == outcomes
    assert [(o.spec.cv, o.result is None, o.error is None) for o in outcomes] == [
        (7, False, True),
        (8, True, False),
        (250, True, True),
    ]
