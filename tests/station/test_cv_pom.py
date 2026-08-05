"""The shared CV wait loop, the CV matcher, POM read, and AUTO mode resolution.

`Task 12` in the M2-M4 core plan is the model for these fixtures' shape:
`bench` and `bench_factory` (`tests/station/conftest.py`, Task 2) wrap a real
`Station` over a `FakeTransport`-backed `Link`, already past the version
handshake, so every test below scripts bare X-Bus telegrams and never touches
framing itself.
"""

from __future__ import annotations

import pytest

from railctl.envelope import Frame, Kind
from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    PomReadUnsupportedError,
    ShortCircuitError,
    TrackPowerError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import CvMatcher, CvProgrammer, TimedOut, resolve_mode
from railctl.station.timing import TIMING
from railctl.station.types import ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import cmd_pom_read_byte, cmd_service_result_request, cmd_station_status
from railctl.xbus.cv import CvEncoding
from railctl.xbus.replies import CvValue, NoAck, Ready

STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x06)  # measured after 21 80; bit 1 is emergency off
POLL = cmd_service_result_request()
ACK = encode(0x01, 0x04)
UNSUPPORTED = encode(0x61, 0x82)
NO_ACK_BYTES = encode(0x61, 0x13)
SHORT_CIRCUIT_BYTES = encode(0x61, 0x12)
ZIMO_CV8 = 145  # the MS450P22's known CV8 value - also why the doctor reads CV8 for D4


def cv_value(ident: int, c: int, value: int) -> bytes:
    return encode(0x63, ident, c, value)


def test_matcher_accepts_either_echo_form_while_pom_echo_zero_based_is_unknown():
    """CV8's two candidate echoes, 7 and 8, both have to match until the doctor
    has read one and pinned the convention down (docs/probe-results.md, R1)."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=None)
    assert matcher(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert matcher(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))


def test_matcher_narrows_to_one_form_once_zero_based_is_learned():
    zero_based = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=True)
    assert zero_based(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert not zero_based(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))

    one_based = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    assert one_based(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert not one_based(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))


def test_matcher_rejects_a_right_byte_wrong_band_reply():
    """A request for CV265 must not accept `63 14 09`, which is CV9.

    `echo_candidates` narrows only WITHIN a band; `result_ident_for` is what
    supplies the band. Skip either check and CV9's value comes back reported
    under CV265's name - CV265 is a ZIMO sound-project CV this tool backs up.
    """
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 265, zero_based=False)
    right_band = CvValue(raw_cv=9, value=7, ident=0x15, z21_form=False)
    wrong_band = CvValue(raw_cv=9, value=7, ident=0x14, z21_form=False)
    assert matcher(right_band)
    assert not matcher(wrong_band)


def test_matcher_decodes_the_documented_but_never_measured_z21_form_branch():
    """No `64 14` reply to a POM request has ever been observed on this
    hardware (docs/probe-results.md, R1). Kept general rather than rejected: a
    station that answered this way would still be answering, and dropping the
    reply would turn a real (if unmeasured) answer into silence - which reads
    as "unsupported" one layer up, the exact failure this project exists to
    catch.
    """
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    # CV8 zero-based wire value is 7, joined as the 16-bit field 0x0007.
    assert matcher(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=True))
    # raw_cv=8 decodes (Z21's own rule) to CV9, not CV8.
    assert not matcher(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=True))


def test_matcher_ignores_non_cv_replies():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    assert not matcher(NoAck())
    assert not matcher(Ready())


def test_value_of_reads_the_plain_byte():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    reply = CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.value_of(reply) == ZIMO_CV8


@pytest.mark.parametrize(("raw", "expected"), [(7, True), (8, False)])
def test_echo_says_zero_based_reads_cv8_either_way(raw: int, expected: bool):
    """CV8 is the doctor's probe CV precisely because 7 and 8 are distinguishable."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=None)
    reply = CvValue(raw_cv=raw, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.echo_says_zero_based(reply) is expected


def test_echo_says_zero_based_is_none_once_the_convention_is_already_fixed():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=True)
    reply = CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.echo_says_zero_based(reply) is None


@pytest.mark.parametrize(
    ("pom_read", "service_direct_cv", "expected"),
    [
        (True, True, ProgMode.POM),
        (True, False, ProgMode.POM),
        (True, None, ProgMode.POM),
        (None, True, ProgMode.POM),
        (None, False, ProgMode.POM),
        (None, None, ProgMode.POM),
        (False, True, ProgMode.SERVICE),
    ],
)
def test_resolve_mode_auto_picks_pom_whenever_it_might_still_work(
    pom_read: bool | None, service_direct_cv: bool | None, expected: ProgMode
):
    """`pom_read is not False` covers both a measured `True` and an unprobed
    `None` - an unknown capability is tried, per the design's own rule, never
    refused pre-emptively."""
    capabilities = Capabilities.unknown("bench").with_learned(
        pom_read=pom_read, service_direct_cv=service_direct_cv
    )
    assert resolve_mode(ProgMode.AUTO, capabilities, operation="read") == expected


@pytest.mark.parametrize("service_direct_cv", [False, None])
def test_resolve_mode_auto_refuses_when_pom_is_measured_false_and_service_is_not(
    service_direct_cv: bool | None,
):
    """SERVICE is the fallback only when POM is a MEASURED no and service mode
    is a MEASURED yes. An unprobed `service_direct_cv is None` cannot receive
    silent fallback traffic any more than an unprobed POM path could be
    assumed to work."""
    capabilities = Capabilities.unknown("bench").with_learned(
        pom_read=False, service_direct_cv=service_direct_cv
    )
    with pytest.raises(PomReadUnsupportedError, match="--mode service") as excinfo:
        resolve_mode(ProgMode.AUTO, capabilities, operation="write")
    assert "--mode service" in excinfo.value.hint


def test_resolve_mode_never_returns_auto_and_leaves_an_explicit_mode_untouched():
    capabilities = Capabilities.unknown("bench")
    assert resolve_mode(ProgMode.POM, capabilities, operation="read") is ProgMode.POM
    assert resolve_mode(ProgMode.SERVICE, capabilities, operation="write") is ProgMode.SERVICE


def test_station_wires_a_cv_programmer(bench):
    """Renamed from `..._with_a_page_cache_hook`: that name promised the cache
    hook was registered, but this body only ever checked the type. The hook
    itself is `test_closing_the_station_runs_the_registered_cache_hook`,
    below - `Station.__init__` could drop its `register_cache(...)` line and
    this test alone would stay green."""
    programmer = bench.station.programmer
    assert isinstance(programmer, CvProgrammer)


def test_invalidate_pages_clears_the_stub_cache_directly():
    """Task 6 fills `_ensure_page`'s real cache in here; today it is empty, but
    the hook has to exist now so `Station.__init__` is not touched twice."""
    programmer = CvProgrammer(station=object())  # no station method is called
    programmer._pages["probe"] = object()
    programmer.invalidate_pages()
    assert programmer._pages == {}


def test_closing_the_station_runs_the_registered_cache_hook(bench):
    bench.station.programmer._pages["probe"] = object()
    bench.station.close()
    assert bench.station.programmer._pages == {}


def test_the_passive_branch_binds_and_tests_the_frame_it_polls(bench, monkeypatch):
    """A passive branch that polls for a frame and discards it makes POM
    reads fail on exactly the behaviour they exist to support: a station that
    only pushes its result once polling has been switched off.

    `bench.link.poll(0.0)` - the drain at the TOP of `await_result`'s loop -
    runs unconditionally on every pass, before `polling` is even consulted,
    and `FakeTransport` hands over everything currently sitting on the port
    in that one call (`Link.poll`'s own drain loop keeps reading until the
    port goes quiet, not just once). So whatever this scenario's own
    `push()` or `broadcast=` puts on the port - however it is timed - is
    always visible to THAT poll first; it never survives to be the frame
    `link.poll(min(interval, remaining))` (the passive branch's own call,
    lines 337-339) discovers. Proven directly: attaching
    `broadcast=cv_value(...)` to this same `POLL`/`61 82` exchange (the
    shape that works for `test_a_61_82_answer_to_the_poll_never_becomes_a_
    durable_capability_fact`, which is exactly this drain path) still passes
    even with `link.poll`'s passive-branch call site replaced by a bare,
    discarding one - because the top-of-loop drain resolves it first and the
    passive branch's own call site is never reached with a frame in hand.

    `bench.link.poll` is monkeypatched instead, matching the existing
    `test_exchange_keeps_a_bad_cable_and_an_unknown_reply_form_apart`
    pattern (`tests/station/test_power_and_status.py`) for the same reason:
    the scenario needed does not exist on the wire `FakeTransport` can
    produce. The stub answers every `timeout=0.0` call (the drain) with
    nothing, and hands the CV frame to the first call with a non-zero
    timeout - the passive branch's own call, and nowhere else - which is
    what pins the frame to that exact call site rather than to "whichever
    poll happens to run first"."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=UNSUPPORTED)
    frame = Frame(kind=Kind.UNSOLICITED, payload=cv_value(0x14, 8, ZIMO_CV8))
    served = False

    def fake_poll(timeout: float = 0.0) -> list[Frame]:
        nonlocal served
        if timeout == 0.0:
            return []  # the top-of-loop drain: nothing ever sits on the port
        if not served:
            served = True
            return [frame]
        # A second passive-branch call means lines 337-339 discarded the
        # first frame instead of returning it - drive the clock past the
        # attempt's own deadline so the test fails clean, as a TimedOut, on
        # the very next iteration rather than looping forever.
        bench.clock.advance(timeout)
        return []

    monkeypatch.setattr(bench.link, "poll", fake_poll)
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, CvValue)
    assert outcome.value == ZIMO_CV8


def test_a_61_82_answer_to_the_poll_never_becomes_a_durable_capability_fact(bench):
    """`broadcast=`, not `push()`, so the CV answer only lands on the port once
    this exchange - the one that actually answers `61 82` - has completed.
    `push()`ing it up front (the previous shape of this test) put it on the
    port before `await_result` ever ran, so its own first `link.poll(0.0)`
    caught it before the scripted `POLL` was even sent: deleting the `expect`
    line below left the test green, proving no `61 82` was involved in what
    it measured. With `broadcast=` here, removing the `expect` line makes
    `FakeTransport` raise "the script is exhausted" instead."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=UNSUPPORTED, broadcast=cv_value(0x14, 8, ZIMO_CV8))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, CvValue)
    assert outcome.value == ZIMO_CV8
    # The eventual match arrived as an unsolicited broadcast after the 61 82,
    # not as the poll's own answer, so learning must say "broadcast" - never a
    # trace of the 61 82.
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_a_poll_answered_unsupported_switches_off_polling_for_the_rest_of_the_attempt(bench):
    """`Station.exchange` (facade.py) turns a `61 82` answer into
    `UnsupportedCommandError` rather than returning `Unsupported` - this
    proves the loop catches that around the poll's own exchange and falls
    back to passive listening instead of letting the exception escape or
    polling again. `FakeTransport` scripts the `POLL` exactly once; a loop
    that failed to switch `polling` off would try to send a second,
    unscripted `POLL` and `FakeTransport` would raise "the script is
    exhausted" instead of letting this reach a clean timeout.
    """
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=UNSUPPORTED)
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, TimedOut)
    assert bench.sent.count(POLL) == 1


def test_every_inner_exchange_is_clamped_to_the_remaining_attempt_budget(bench):
    """Without the clamp, a poll issued at `li_ack_normal` (5.0 s) would
    overrun a 2.0 s attempt by 3 s. `FakeTransport` asserts on an unscripted
    request rather than timing out on its own, so silence has to be scripted
    explicitly - one `expect(..., reply=b"")` per poll the loop is expected to
    issue. At `timeout=2.0` the very first exchange's own clamped budget
    already consumes the whole attempt (`min(exchange_timeout=5.0,
    remaining=2.0)` is 2.0, not 5.0), so exactly one silent poll is scripted;
    the fake clock is what proves it was allowed only the 2.0 s attempt
    budget, never the passed `exchange_timeout`."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=b"")
    started = bench.clock.monotonic()
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    elapsed = bench.clock.monotonic() - started
    assert isinstance(outcome, TimedOut)
    assert elapsed == pytest.approx(2.0, abs=0.2)


def test_ready_streak_at_the_limit_ends_the_wait_as_no_ack(bench):
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    ready_bytes = encode(0x61, 0x11)
    for _ in range(TIMING.service_ready_limit):
        bench.expect(POLL, reply=ready_bytes)
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=5.0,
        first_delay=0.0,
        interval=0.0,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="service",
    )
    assert isinstance(outcome, NoAck)


def test_ready_means_done_ends_the_wait_on_the_first_ready(bench):
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=encode(0x61, 0x11))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=5.0,
        first_delay=0.0,
        interval=0.0,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=True,
        context="service",
    )
    assert isinstance(outcome, Ready)


def test_a_transient_busy_reply_to_a_poll_keeps_the_wait_going(bench):
    """`StationBusy` (61 81) is in `TRANSIENT_REPLIES` - it says nothing about
    support either way, so `_consider` must swallow it (return `None`) and
    let the loop poll again, never treat it as an answer or an error."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=encode(0x61, 0x81))
    bench.expect(POLL, reply=cv_value(0x14, 8, ZIMO_CV8))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.0,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, CvValue)
    assert outcome.value == ZIMO_CV8


def test_pom_read_refuses_before_sending_anything_when_pom_read_is_known_false(bench):
    bench.station.learn(pom_read=False)
    before = list(bench.sent)
    with pytest.raises(PomReadUnsupportedError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.sent == before


def test_pom_read_refuses_and_names_its_capabilities_provenance(bench_factory):
    """The precondition refusal's message has to say WHERE the "unsupported"
    fact came from - `capabilities.json`, when it was probed, and the note
    recorded at the time - or it reads as a bug in this tool rather than a
    fact the station already taught it in an earlier session. This is the
    provenance requirement moved here from the CLI layer's own tests (a
    message-content assertion belongs where the message is built, not where
    it is only ever copied through)."""
    capabilities = (
        Capabilities.unknown("bench")
        .with_learned(pom_read=False, probed_at="2026-01-01T00:00:00+00:00")
        .with_note("the command station answered `61 82` to a POM read of CV8")
    )
    bench = bench_factory(capabilities=capabilities)
    with pytest.raises(PomReadUnsupportedError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    message = str(excinfo.value)
    assert "capabilities.json" in message
    assert "2026-01-01T00:00:00+00:00" in message
    assert "61 82" in message


def test_pom_read_refuses_when_track_power_is_off(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)
    with pytest.raises(TrackPowerError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    assert excinfo.value.hint is not None
    assert "railctl power on" in excinfo.value.hint


def test_pom_read_refuses_with_no_address_anywhere(bench_factory):
    bench = bench_factory(default_address=None)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    with pytest.raises(ValueError, match="address"):
        bench.station.programmer.pom_read(8, address=None)


def test_three_silent_attempts_raise_decoder_not_responding_never_unsupported(bench):
    """The single most important assertion in M5: silence is measured
    behaviour on this hardware (docs/probe-results.md, R1), and this project
    exists because an earlier instrument recorded it as `False` instead."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    for _ in range(TIMING.pom_read_attempts):
        bench.expect(pom8, reply=ACK)
        # `FakeTransport` asserts on an unscripted request rather than timing
        # out on its own, so this attempt's own silence has to be scripted
        # too - one empty-reply poll per attempt. `pom_result`'s 2.0 s budget
        # clamps the inner exchange the same way as the clamp test above, so
        # one silent poll is exactly what each attempt consumes.
        bench.expect(POLL, reply=b"")
    started = bench.clock.monotonic()
    with pytest.raises(DecoderNotRespondingError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    elapsed = bench.clock.monotonic() - started
    message = str(excinfo.value).lower()
    assert "unsupported" not in message
    assert "not supported" not in message
    assert bench.station.capabilities.pom_read is None
    assert elapsed < 15.0
    # A script has no other way to tell "gave up after one attempt" from
    # "gave up after three" - the exit code and message text are identical
    # either way. `attempts` is what a caller (or a retry policy one layer up)
    # actually reads to make that distinction.
    assert excinfo.value.details == {
        "cv": 8,
        "address": 3,
        "mode": "pom",
        "attempts": TIMING.pom_read_attempts,
        "attempt_timeout_s": TIMING.pom_result,
    }


def test_unsupported_to_the_pom_telegram_raises_and_learns_pom_read_false(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=UNSUPPORTED)
    with pytest.raises(PomReadUnsupportedError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.station.capabilities.pom_read is False


def test_no_ack_seen_on_any_attempt_raises_decoder_no_ack_and_leaves_pom_read_unknown(bench):
    """`saw_no_ack` from attempt 1 has to survive attempts 2 and 3 coming back
    silent, so the final exception is still `DecoderNoAckError`, not
    `DecoderNotRespondingError`. Attempts 2 and 3's silence is scripted
    explicitly - `FakeTransport` asserts on an unscripted request rather than
    timing out on its own, so leaving `POLL` unscripted for those attempts
    would fail with `unexpected request`, not with the timeout this test
    means to exercise."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=ACK)
    bench.expect(POLL, reply=NO_ACK_BYTES)
    bench.expect(pom8, reply=ACK)  # attempt 2: silence
    bench.expect(POLL, reply=b"")
    bench.expect(pom8, reply=ACK)  # attempt 3: silence
    bench.expect(POLL, reply=b"")
    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.station.capabilities.pom_read is None


def test_a_successful_read_learns_zero_based_true_from_echo_seven(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 7, ZIMO_CV8))
    result = bench.station.programmer.pom_read(8, address=3)
    assert result.value == ZIMO_CV8
    assert result.mode is ProgMode.POM
    assert result.operation == "read"
    assert result.verified is None
    assert bench.station.capabilities.pom_read is True
    assert bench.station.capabilities.pom_echo_zero_based is True
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_a_successful_read_learns_zero_based_false_from_echo_eight(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 8, ZIMO_CV8))
    result = bench.station.programmer.pom_read(8, address=3)
    assert result.value == ZIMO_CV8
    assert bench.station.capabilities.pom_echo_zero_based is False


def test_short_circuit_ends_the_read_immediately_with_no_retry(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=ACK)
    bench.expect(POLL, reply=SHORT_CIRCUIT_BYTES)
    with pytest.raises(ShortCircuitError):
        bench.station.programmer.pom_read(8, address=3)
    # handshake + status() + the one POM telegram + the one poll - no second
    # or third attempt. `Link.open()` counts the version handshake as request
    # 1 before this test ever calls anything (`link.py` L111), so the count
    # a bare "three things happened" reading of this test would expect is off
    # by exactly that one request.
    assert bench.station.link.stats().requests == 4


def test_a_mismatched_cv_value_answered_inline_is_reported_stale_and_the_wait_continues(bench):
    """`_consider`'s own stale-reporting branch, distinct from
    `_drain_stale`'s: here the POM telegram's OWN reply is a `CvValue` for
    the wrong CV, arriving as the immediate exchange answer rather than a
    leftover frame drained before the request went out. `broadcast=` (not
    `push()`) is what puts the real CV8 answer where `await_result`'s own
    `link.poll(0.0)` can see it - a `push()`ed frame queued before this call
    would be silently discarded by `Link.request()`'s own leading `drain()`
    before the POM telegram is even sent (link.py)."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(
        pom8,
        # raw_cv=9 on CV8's own band (0x14) matches neither of CV8's two
        # candidate echoes ({7, 8}) whether or not `pom_echo_zero_based` is
        # already pinned - unlike raw_cv=7, which IS one of CV8's own
        # candidates while the convention is still unknown and so would be
        # accepted, not reported stale.
        reply=cv_value(0x14, 9, 200),
        broadcast=cv_value(0x14, 8, ZIMO_CV8),
    )
    result = bench.station.programmer.pom_read(8, address=3)
    assert result.value == ZIMO_CV8
    stale = [payload for name, payload in bench.events if name == "cv.stale_result"]
    assert stale == [{"cv": 8, "raw_cv": 9, "encoding": "POM_ZERO_BASED"}]


def test_a_stale_result_from_an_earlier_read_is_discarded_and_reported(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom7 = cmd_pom_read_byte(3, 7, threshold=bench.station.threshold)
    bench.expect(pom7, reply=cv_value(0x14, 7, 200))
    first = bench.station.programmer.pom_read(7, address=3)
    assert first.value == 200

    # A late reply belonging to that already-finished CV7 read is still
    # sitting on the port - exactly what the per-attempt drain exists to
    # clear before CV8's own telegram goes out.
    bench.push(cv_value(0x14, 7, 201))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 8, ZIMO_CV8))
    second = bench.station.programmer.pom_read(8, address=3)
    assert second.value == ZIMO_CV8

    stale = [payload for name, payload in bench.events if name == "cv.stale_result"]
    assert stale == [{"cv": 8, "raw_cv": 7, "encoding": "POM_ZERO_BASED"}]
