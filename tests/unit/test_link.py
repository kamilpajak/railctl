from __future__ import annotations

import logging

import pytest

from railctl.envelope import Frame, Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import LinkProtocolError, LinkTimeout, PortNotXpressNet
from railctl.link import (
    DEFAULT_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    MAX_RETRIES,
    PROGRAMMING_TIMEOUT,
    SETTLE_TIME,
    Link,
    LinkStats,
)
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.codec import xor
from railctl.xbus.commands import cmd_station_version

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"
STATUS_REQUEST = b"\x21\x24\x05"
STATUS_REPLY = b"\x62\x22\x07\x47"
POLL = b"\x21\x10\x31"
CV8_RESULT = b"\x63\x14\x08\x91\xee"
ACK = b"\x01\x04\x05"
POWER_ON_BROADCAST = b"\x61\x01\x60"
UNSUPPORTED = b"\x61\x82\xe3"
BAD_XOR_REJECT = b"\x61\x80\xe1"
NOT_UNDERSTOOD = b"\x01\x0a\x0b"


class Fixture:
    """A Link over a scripted station, with the envelope doing all the framing."""

    def __init__(self, chunk_size=None, on_event=None):
        self.envelope = LiUsbEnvelope()
        self.clock = FakeClock()
        self.transport = FakeTransport(clock=self.clock, chunk_size=chunk_size)
        self.link = Link(self.transport, self.envelope, on_event=on_event, clock=self.clock)

    def expect(self, request: bytes, *replies: tuple[Kind, bytes]):
        reply = b"".join(self.envelope.frame(kind, tel) for kind, tel in replies)
        self.transport.expect(self.envelope.frame(Kind.SOLICITED, request), reply=reply)
        return self

    def push(self, kind: Kind, telegram: bytes):
        self.transport.queue(self.envelope.frame(kind, telegram))
        return self

    def open(self):
        self.expect(VERSION_REQUEST, (Kind.SOLICITED, VERSION_REPLY))
        self.link.open()
        return self


@pytest.fixture
def station(chunk_size) -> Fixture:
    return Fixture(chunk_size=chunk_size)


def test_the_handshake_bytes_agree_with_the_xbus_encoder():
    """link.py cannot import xbus - xbus sits above it - so the handshake is
    duplicated on purpose. A test may import both layers and pin them together.
    """
    from railctl.link import _HANDSHAKE_TELEGRAM

    # The encoder call goes on the left: ruff's SIM300 reads a SCREAMING_CASE
    # name on the left of == as a Yoda condition and fails the lint gate.
    assert cmd_station_version() == _HANDSHAKE_TELEGRAM
    assert xor(_HANDSHAKE_TELEGRAM) == 0


def test_open_runs_the_handshake_and_records_the_version(station):
    station.open()
    assert station.link.version_telegram == VERSION_REPLY
    assert station.transport.is_open is True
    assert station.transport.flushes == 1


def test_open_on_a_silent_port_raises_port_not_xpressnet_and_closes(station):
    station.expect(VERSION_REQUEST)
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "(none)" in str(caught.value)
    assert station.transport.is_open is False
    assert station.clock.monotonic() == pytest.approx(HANDSHAKE_TIMEOUT)


def test_open_on_a_port_that_answers_the_wrong_thing_says_so(station):
    """A prompt answer that is not a version reply must not be reported as silence.

    This project exists because a reply recorded as silence reads one layer up as
    "the hardware cannot do this". Reproducing that failure inside Link would be
    the same mistake: the station DID answer, and the operator needs the bytes.
    """
    station.expect(VERSION_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "62 22 07 47" in str(caught.value)
    assert "did not answer" not in str(caught.value)


def test_the_handshake_failure_hint_comes_from_the_transport(station):
    """Spec line 583: the Z21 LAN transport lands with no edit to link.py.

    A hardcoded sentence about CDC interface indices would have to be edited the
    first time a handshake fails over the network, so the advice is read off the
    transport instead.
    """
    station.expect(VERSION_REQUEST)
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert caught.value.hint == station.transport.diagnostic_hint


def test_open_on_the_telemetry_port_quotes_what_it_saw(station):
    # Queued as the REPLY, not up front: open() calls flush_input() before the
    # handshake, so anything queued earlier is gone by the time it writes.
    station.transport.expect(
        station.envelope.frame(Kind.SOLICITED, VERSION_REQUEST),
        reply=b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C CT 35.7'C CA 26 CB 08\r\n",
    )
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "5B 43 53 30" in str(caught.value)  # "[CS0"
    assert station.envelope.stats.bytes_dropped > 0
    assert station.envelope.stats.frames_ok == 0


def test_request_returns_the_bare_telegram(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY


def test_a_request_that_is_never_answered_raises_link_timeout_with_the_stats(station):
    station.open().expect(STATUS_REQUEST)
    with pytest.raises(LinkTimeout) as caught:
        station.link.request(STATUS_REQUEST, timeout=3.0)
    message = str(caught.value)
    assert "21 24 05" in message
    assert "3.0" in message
    assert "frames_ok=1" in message
    assert station.link.stats().timeouts == 1


def test_a_timeout_does_not_flush_the_receive_buffer(station):
    """Flushing risks cutting a frame in half. A late reply is caught, counted as
    a stray by the next drain(), and KEPT.

    The reply has to be sitting in the transport's receive buffer WHILE the
    timeout budget is still running, not pushed in afterwards - otherwise a
    flush_input() call inserted right before the raise would find nothing to
    destroy and the test would stay green for the wrong reason. FakeTransport
    burns the clock one _READ_SLICE at a time when there is nothing to read
    (see FakeTransport.read), so the fake clock's sleep() is the one place a
    test can land bytes exactly at the moment the budget runs out: the
    injected sleep delivers the reply the instant the clock reaches the
    deadline, and by then _pump has already decided remaining <= 0 and will
    not call read() again to pick it up. That is what "sitting in the buffer
    while the timeout is running" means here.

    docs/probe-results.md investigation R1 is a station that acknowledges a
    request and returns no result. The question there is whether something
    arrived late and what it was, so a counter alone is not enough: "one stray
    reply happened" cannot be told apart from "a 63 14 08 91 EE arrived 3 s after
    the budget", and that is the difference between "POM read unsupported" and
    "POM read is slower than the budget".
    """
    station.open().expect(STATUS_REQUEST)
    late_reply = station.envelope.frame(Kind.SOLICITED, STATUS_REPLY)
    budget = 1.0
    deadline = station.clock.monotonic() + budget
    delivered = False
    real_sleep = station.clock.sleep

    def sleep_then_deliver_at_the_deadline(seconds: float) -> None:
        real_sleep(seconds)
        nonlocal delivered
        if not delivered and station.clock.monotonic() >= deadline - 1e-9:
            delivered = True
            station.transport.queue(late_reply)

    station.clock.sleep = sleep_then_deliver_at_the_deadline
    with pytest.raises(LinkTimeout):
        station.link.request(STATUS_REQUEST, timeout=budget)
    assert delivered, "the fake never reached the deadline; the timing rigging is broken"
    station.link.drain()
    assert station.link.stats().stray_replies == 1
    assert station.link.recent_late_replies() == [Frame(Kind.SOLICITED, STATUS_REPLY)]


def test_the_same_request_answered_first_with_silence_then_with_the_value(station):
    """The service-result poll loop in one test. If the fake ever loses its
    sequencing this fails, and with it the whole reason M4 is a milestone.
    """
    station.open()
    station.expect(POLL)
    station.expect(POLL, (Kind.SOLICITED, CV8_RESULT))
    with pytest.raises(LinkTimeout):
        station.link.request(POLL, timeout=1.0)
    assert station.link.request(POLL, timeout=1.0) == CV8_RESULT


def test_an_unsolicited_frame_during_a_request_is_dispatched_and_the_wait_continues():
    seen: list[Frame] = []
    fixture = Fixture(on_event=seen.append)
    fixture.open()
    fixture.expect(
        STATUS_REQUEST,
        (Kind.UNSOLICITED, POWER_ON_BROADCAST),
        (Kind.SOLICITED, STATUS_REPLY),
    )
    assert fixture.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert seen == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]
    assert fixture.link.recent_events() == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]


def test_an_on_event_callback_that_raises_cannot_lose_the_reply(caplog):
    def explode(frame: Frame) -> None:
        raise RuntimeError("bad callback")

    fixture = Fixture(on_event=explode)
    fixture.open()
    fixture.expect(
        STATUS_REQUEST,
        (Kind.UNSOLICITED, POWER_ON_BROADCAST),
        (Kind.SOLICITED, STATUS_REPLY),
    )
    with caplog.at_level(logging.WARNING, logger="railctl.link"):
        assert fixture.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert "bad callback" in caplog.text


def test_a_bad_xor_rejection_is_retried_exactly_once(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert station.link.stats().retries == 1


def test_a_not_understood_rejection_is_retried_exactly_once(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, NOT_UNDERSTOOD))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY


def test_two_rejections_raise_link_protocol_error(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    with pytest.raises(LinkProtocolError, match="twice"):
        station.link.request(STATUS_REQUEST)
    assert station.link.stats().retries == MAX_RETRIES


def test_unsupported_is_a_real_answer_and_is_never_retried(station):
    """61 82 is how a capability probe learns an opcode is unavailable. Retrying
    it, or turning it into an exception here, is how a capability gets recorded
    as absent for the wrong reason.
    """
    station.open().expect(POLL, (Kind.SOLICITED, UNSUPPORTED))
    assert station.link.request(POLL) == UNSUPPORTED
    assert station.link.stats().retries == 0


def test_send_waits_for_the_ack_when_the_envelope_expects_one(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, ACK))
    station.link.send(STATUS_REQUEST)
    assert station.link.stats().requests == 2


def test_send_uses_send_no_reply_when_the_envelope_does_not_expect_an_ack():
    class NoAckEnvelope(LiUsbEnvelope):
        expects_ack = False

    envelope = NoAckEnvelope()
    clock = FakeClock()
    transport = FakeTransport(clock=clock)
    link = Link(transport, envelope, clock=clock)
    transport.open()
    transport.expect(envelope.frame(Kind.SOLICITED, STATUS_REQUEST))
    link.send(STATUS_REQUEST)
    assert clock.monotonic() >= SETTLE_TIME


def test_poll_returns_unsolicited_frames_and_files_late_replies_separately(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.push(Kind.SOLICITED, STATUS_REPLY)
    assert station.link.poll(0.0) == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]
    assert station.link.stats().stray_replies == 1
    assert station.link.recent_late_replies() == [Frame(Kind.SOLICITED, STATUS_REPLY)]


class EndlessTelemetry(FakeTransport):
    """Interface ...45: the YD.Control stream never goes quiet.

    docs/probe-results.md, port map: ...41 is LocoNet (silent), ...43 is
    XpressNet, ...45 streams ASCII telemetry continuously at 57600 baud. read()
    on that port always has bytes ready.
    """

    LINE = b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C\r\n"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0

    def read(self, max_bytes: int, timeout: float) -> bytes:
        self.reads += 1
        if self.reads > 1000:
            raise AssertionError("poll(0.0) never stopped draining")
        return self.LINE[:max_bytes]


def test_poll_gives_up_on_a_port_that_never_goes_quiet():
    """poll(0.0) runs at the top of every request(), so an unbounded drain hangs
    railctl with no timeout and no error - the one outcome worse than a wrong
    answer. The bound turns it back into the wrong-interface diagnosis the
    counters were built for.
    """
    envelope = LiUsbEnvelope()
    clock = FakeClock()
    transport = EndlessTelemetry(clock=clock)
    transport.open()
    link = Link(transport, envelope, clock=clock)

    assert link.poll(0.0) == []

    assert transport.reads < 200  # 4096 / 43 bytes per line is about 96
    assert link.stats().frames_ok == 0
    assert link.stats().bytes_dropped > 4000


def test_drain_discards_everything_queued(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.link.drain()
    assert station.link.poll(0.0) == []


def test_await_frame_reads_without_writing(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.push(Kind.UNSOLICITED, CV8_RESULT)
    frame = station.link.await_frame(lambda f: f.payload[:2] == b"\x63\x14", timeout=1.0)
    assert frame.payload == CV8_RESULT
    assert station.transport.written == [station.envelope.frame(Kind.SOLICITED, VERSION_REQUEST)]


def test_await_frame_that_never_matches_raises_link_timeout(station):
    station.open()
    with pytest.raises(LinkTimeout):
        station.link.await_frame(lambda f: False, timeout=1.0)


def test_await_frame_files_a_non_matching_solicited_frame_as_a_late_reply(station):
    """await_frame must mirror poll(): a solicited frame that does not match
    the predicate is a late reply, not a frame silently thrown away. A
    discarded frame leaves only a counter, and a counter cannot say what
    arrived.
    """
    station.open()
    station.push(Kind.SOLICITED, STATUS_REPLY)
    station.push(Kind.SOLICITED, CV8_RESULT)
    frame = station.link.await_frame(lambda f: f.payload[:2] == b"\x63\x14", timeout=1.0)
    assert frame.payload == CV8_RESULT
    assert station.link.recent_late_replies() == [Frame(Kind.SOLICITED, STATUS_REPLY)]


def test_link_never_logs_wire_bytes(station, caplog):
    """The envelope owns the wire log in both directions. Two loggers means the
    same frame printed twice, or once with the framing and once without.
    """
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    with caplog.at_level(logging.DEBUG):
        station.link.request(STATUS_REQUEST)
    assert {record.name for record in caplog.records} == {"railctl.wire"}


def test_stats_carries_both_halves(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    station.link.request(STATUS_REQUEST)
    assert station.link.stats() == LinkStats(
        requests=2,
        retries=0,
        timeouts=0,
        frames_ok=2,
        bytes_dropped=0,
        bad_xor=0,
        stray_replies=0,
    )


def test_description_and_identity_come_from_the_transport(station):
    station.open()
    assert station.link.description == "fake xpressnet"
    assert station.link.identity == "fake"


def test_close_closes_the_transport(station):
    station.open()
    station.link.close()
    assert station.transport.is_open is False


def test_the_budgets_are_the_documented_ones():
    assert DEFAULT_TIMEOUT == 5.0
    assert PROGRAMMING_TIMEOUT == 95.0
    assert HANDSHAKE_TIMEOUT == 2.0
