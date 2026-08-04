from __future__ import annotations

import pytest

from railctl.envelope import Kind, hex_bytes
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import PortNotOpen
from railctl.transport.fake import FakeClock, FakeTransport

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"
POLL = b"\x21\x10\x31"  # 21 10: request for service mode results
CV8_RESULT = b"\x63\x14\x08\x91\xee"  # measured: CV8 = 145 on the ZIMO MS450P22
ACK = b"\x01\x04\x05"


@pytest.fixture
def env() -> LiUsbEnvelope:
    return LiUsbEnvelope()


def _open(**kwargs) -> FakeTransport:
    transport = FakeTransport(**kwargs)
    transport.open()
    return transport


def test_the_same_request_is_answered_first_with_silence_and_then_with_the_value(env):
    """The single reason this test double exists.

    The M1 probe's FakeLink answered a payload with the same bytes every time,
    so a station that returns nothing to a read and produces the value only when
    asked AGAIN could not be expressed. That is the station docs/probe-results.md
    measured: 22 15, 22 18 and 22 19 deliver their result only after 21 10 is
    sent. Every mutant inside the poll loop survived the M1 suite, including the
    one that deletes the loop outright - and a missing poll makes the whole Lenz
    opcode family read as silent, which is two capabilities wrongly recorded as
    absent.

    If this test can be deleted without another one failing, the fake has
    regressed to the M1 shape.
    """
    solicited = lambda telegram: env.frame(Kind.SOLICITED, telegram)  # noqa: E731
    transport = _open()
    transport.expect(solicited(POLL), reply=b"")
    transport.expect(solicited(POLL), reply=solicited(CV8_RESULT))

    transport.write(solicited(POLL))
    assert transport.read(256, 1.0) == b""

    transport.write(solicited(POLL))
    assert transport.read(256, 1.0) == solicited(CV8_RESULT)


def test_a_second_command_while_a_reply_is_outstanding_raises(env):
    transport = _open()
    transport.expect(
        env.frame(Kind.SOLICITED, VERSION_REQUEST), reply=env.frame(Kind.SOLICITED, VERSION_REPLY)
    )
    transport.expect(env.frame(Kind.SOLICITED, POLL))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    with pytest.raises(AssertionError, match="exactly one command in flight"):
        transport.write(env.frame(Kind.SOLICITED, POLL))


def test_a_second_command_after_the_reply_was_read_is_fine(env):
    transport = _open()
    transport.expect(
        env.frame(Kind.SOLICITED, VERSION_REQUEST), reply=env.frame(Kind.SOLICITED, VERSION_REPLY)
    )
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=env.frame(Kind.SOLICITED, ACK))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, VERSION_REPLY)
    transport.write(env.frame(Kind.SOLICITED, POLL))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, ACK)


def test_a_silent_exchange_releases_the_one_command_rule(env):
    """After the station says nothing there is nothing to wait for, so the next
    command - a retry, or the 21 10 poll - must be allowed through.
    """
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=b"")
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=b"")
    transport.write(env.frame(Kind.SOLICITED, POLL))
    assert transport.read(256, 0.5) == b""
    transport.write(env.frame(Kind.SOLICITED, POLL))


def test_the_exact_request_telegram_is_asserted(env):
    """The expected bytes are rendered through hex_bytes, never typed out.

    Typing them would put the framing prefix into a second test file and
    tests/unit/test_envelope_isolation.py would fail: that guard lower-cases every
    scanned file and looks for the prefix as text as well as as an escape.
    """
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    with pytest.raises(AssertionError, match="unexpected request") as caught:
        transport.write(env.frame(Kind.SOLICITED, b"\x21\x24\x05"))
    assert hex_bytes(env.frame(Kind.SOLICITED, VERSION_REQUEST)) in str(caught.value)


def test_a_write_with_an_exhausted_script_raises(env):
    transport = _open()
    with pytest.raises(AssertionError, match="script is exhausted"):
        transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))


def test_a_read_that_finds_nothing_advances_the_fake_clock_by_its_timeout():
    """Without this a Link waiting on monotonic() spins for ever against frozen
    time and the timeout path cannot be tested at all.
    """
    clock = FakeClock()
    transport = _open(clock=clock)
    transport.queue(b"")
    assert transport.read(256, 2.5) == b""
    assert clock.monotonic() == pytest.approx(2.5)


def test_a_read_that_finds_bytes_does_not_advance_the_clock(env):
    clock = FakeClock()
    transport = _open(clock=clock)
    transport.queue(env.frame(Kind.UNSOLICITED, b"\x61\x01\x60"))
    assert transport.read(256, 2.5) != b""
    assert clock.monotonic() == 0.0


def test_chunk_size_one_replays_worst_case_usb_fragmentation(env):
    transport = _open(chunk_size=1)
    framed = env.frame(Kind.SOLICITED, VERSION_REPLY)
    transport.queue(framed)
    got = b""
    for _ in range(len(framed)):
        got += transport.read(256, 0.1)
    assert got == framed


def test_max_write_splits_the_write_but_delivers_everything(env):
    transport = _open(max_write=3)
    framed = env.frame(Kind.SOLICITED, VERSION_REQUEST)
    transport.expect(framed, reply=env.frame(Kind.SOLICITED, ACK))
    transport.write(framed)
    assert transport.written == [framed]
    assert transport.write_chunks == [framed[0:3], framed[3:5]]


def test_on_write_lets_a_test_script_a_station_with_no_queue(env):
    def station(request: bytes, transport: FakeTransport) -> None:
        if request.endswith(VERSION_REQUEST):
            transport.queue(env.frame(Kind.SOLICITED, VERSION_REPLY))

    transport = _open(on_write=station)
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, VERSION_REPLY)


def test_flush_input_drops_queued_bytes_and_is_counted(env):
    transport = _open()
    transport.queue(env.frame(Kind.SOLICITED, ACK))
    transport.flush_input()
    assert transport.read(256, 0.1) == b""
    assert transport.flushes == 1


def test_reading_or_writing_a_closed_transport_raises_port_not_open(env):
    transport = FakeTransport()
    with pytest.raises(PortNotOpen):
        transport.read(256, 0.1)
    with pytest.raises(PortNotOpen):
        transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.is_open is False


def test_description_identity_and_diagnostic_hint_are_reported():
    """diagnostic_hint keeps connection-specific advice behind the transport.

    Spec line 583 requires the Z21 LAN transport to land with no edit to link.py,
    so link.py may not hold a sentence about CDC interface indices. Each transport
    supplies its own.
    """
    transport = FakeTransport(description="fake xpressnet", identity="fake")
    assert transport.description == "fake xpressnet"
    assert transport.identity == "fake"
    assert transport.diagnostic_hint == "check the station is reachable on the network"


def test_script_pending_shows_what_was_never_sent(env):
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    transport.expect(env.frame(Kind.SOLICITED, POLL))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert [exchange.request for exchange in transport.script_pending] == [
        env.frame(Kind.SOLICITED, POLL)
    ]
