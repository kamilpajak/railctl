"""The one file in the link and transport suites allowed to contain the literal
LI-USB prefix bytes.

The link, station and CLI suites hold bare telegrams and take the envelope as a
fixture parameter, so adding Z21Envelope later re-runs them against new framing
with zero test edits. tests/unit/test_envelope_isolation.py carries the full
allow-list and fails if any file outside it starts spelling these bytes out.
"""

from __future__ import annotations

import itertools
import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st

from railctl.envelope import EnvelopeStats, Frame, Kind, hex_bytes
from railctl.envelope.liusb import MAX_BUFFER, LiUsbEnvelope

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"  # measured: XpressNet 4.0, station id 0x12
ACK = b"\x01\x04\x05"  # measured: the LI interface ack
BROADCAST = b"\x61\x01\x60"  # track power on, unsolicited
DRIVE_126 = b"\xe4\x13\x00\x03\xff\x0b"  # loco 3, step 126 forward: payload holds 0xFF
TELEMETRY = b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C\r\n"


@pytest.fixture
def env() -> LiUsbEnvelope:
    return LiUsbEnvelope()


def test_wrap_prepends_the_solicited_prefix_and_changes_nothing_else(env):
    assert env.wrap(VERSION_REQUEST) == b"\xff\xfe\x21\x21\x00"


def test_frame_renders_both_kinds(env):
    assert env.frame(Kind.SOLICITED, ACK) == b"\xff\xfe" + ACK
    assert env.frame(Kind.UNSOLICITED, BROADCAST) == b"\xff\xfd" + BROADCAST


def test_a_solicited_frame_comes_back_whole(env):
    env.note_request(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(kind=Kind.SOLICITED, payload=VERSION_REPLY)
    assert env.pop() is None
    assert env.stats.frames_ok == 1
    assert env.stats.bytes_dropped == 0
    assert env.stats.stray_replies == 0


def test_ff_fd_is_classified_as_unsolicited(env):
    env.feed(b"\xff\xfd" + BROADCAST)
    assert env.pop() == Frame(kind=Kind.UNSOLICITED, payload=BROADCAST)


def test_a_payload_containing_ff_is_not_mistaken_for_a_prefix(env):
    env.note_request(DRIVE_126)
    env.feed(b"\xff\xfe" + DRIVE_126)
    assert env.pop() == Frame(kind=Kind.SOLICITED, payload=DRIVE_126)
    assert env.stats.bytes_dropped == 0


def test_two_frames_in_one_chunk_come_back_in_arrival_order(env):
    env.feed(b"\xff\xfe" + ACK + b"\xff\xfd" + BROADCAST)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.pop() == Frame(Kind.UNSOLICITED, BROADCAST)
    assert env.pop() is None


def test_byte_at_a_time_feeding_yields_the_same_frame(env):
    stream = b"\xff\xfe" + DRIVE_126
    got = []
    for index in range(len(stream)):
        env.feed(stream[index : index + 1])
        frame = env.pop()
        if frame is not None:
            got.append(frame)
    assert got == [Frame(Kind.SOLICITED, DRIVE_126)]


def test_leading_noise_is_dropped_counted_and_the_frame_still_arrives(env):
    env.feed(b"hello" + b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.bytes_dropped == 5
    assert env.stats.resyncs == 1


def test_a_buffer_with_no_ff_at_all_is_discarded_whole(env):
    env.feed(TELEMETRY)
    assert env.pop() is None
    assert env.stats.frames_ok == 0
    assert env.stats.bytes_dropped == len(TELEMETRY)


def test_the_telemetry_port_produces_only_dropped_bytes(env):
    """The software half of the M4 hardware acceptance check."""
    for _ in range(4):
        env.feed(TELEMETRY)
        assert env.pop() is None
    assert env.stats.frames_ok == 0
    assert env.stats.bytes_dropped == 4 * len(TELEMETRY)


def test_a_stray_prefix_in_front_of_a_real_frame_does_not_swallow_it(env):
    """The exact regression tools/probe/frames.py was rewritten to fix.

    The stray prefix's header byte is 0xFF, so the length it implies is 17 and
    the candidate looks incomplete for ever. Trusting that reading loses the
    real frame behind it - a reply recorded as silence, which in this project is
    the difference between "unsupported" and "not established".
    """
    env.feed(b"\xff\xfe" + b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)
    assert env.stats.bytes_dropped == 2
    assert env.stats.resyncs == 1


def test_a_stray_prefix_further_into_the_buffer_still_resyncs(env):
    """Mutation pinning for `_salvage_start`'s own offset arithmetic (`pos`
    doubled to `pos << 1` instead of used directly), not the `_salvage(buffer,
    pos + 1)` base-offset shape docs/test-hardening.md records for
    tools/probe/frames.py - LiUsbEnvelope's scan has no base-offset argument to
    mutate that way.

    The real salvage offset has to land on an odd number, or `pos << 1` (which
    is always even) can coincide with it by chance and the mutant hides behind
    a green suite - that is exactly what happened when this test fed a stray
    prefix at an even offset: `_complete_at(1 << 1)` found the same frame
    `_complete_at(2)` would have, and the mutant passed. The extra `0xFF` here
    pushes the real frame to offset 3 so the two expressions diverge.
    """
    env.feed(b"\xff\xfe" + ACK + b"\xff\xfe\xff" + b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)
    assert env.pop() is None
    assert env.stats.bytes_dropped == 3


def test_an_incomplete_frame_with_nothing_behind_it_is_waited_for(env):
    env.feed(b"\xff\xfe\x63\x21\x40")
    assert env.pop() is None
    assert env.stats.bytes_dropped == 0
    env.feed(b"\x12\x10")
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)


def test_a_bad_checksum_costs_one_byte_and_the_next_good_frame_arrives(env):
    env.feed(b"\xff\xfe\x21\x81\x00" + b"\xff\xfe\x21\x81\xa0")
    assert env.pop() == Frame(Kind.SOLICITED, b"\x21\x81\xa0")
    assert env.stats.bad_xor == 1
    assert env.stats.frames_ok == 1
    assert env.stats.bytes_dropped == 5  # one byte for the bad frame, four resyncing
    assert env.stats.resyncs == 2


def test_a_second_ff_after_a_prefix_costs_exactly_one_byte(env):
    env.feed(b"\xff\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.bytes_dropped == 1


def test_the_buffer_is_bounded_and_the_discard_is_counted(env):
    env.feed(b"\x00" * (MAX_BUFFER + 500))
    assert env.stats.bytes_dropped == 500
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)


def test_a_solicited_frame_with_no_request_outstanding_is_a_stray(env):
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.stray_replies == 1


def test_note_reply_closes_the_lifecycle_so_the_next_reply_is_the_stray(env):
    """Named test. Forgetting note_reply on the success path silently breaks
    stray_replies and the future Z21 classification, and nothing else would
    notice.
    """
    env.note_request(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    first = env.pop()
    assert env.stats.stray_replies == 0
    env.note_reply(first)
    env.feed(b"\xff\xfe" + ACK)
    env.pop()
    assert env.stats.stray_replies == 1


def test_note_abandoned_also_closes_the_lifecycle(env):
    env.note_request(VERSION_REQUEST)
    env.note_abandoned()
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    env.pop()
    assert env.stats.stray_replies == 1


def test_an_unsolicited_frame_is_never_a_stray(env):
    env.feed(b"\xff\xfd" + BROADCAST)
    env.pop()
    assert env.stats.stray_replies == 0


def test_reset_clears_the_buffer_and_keeps_the_counters(env):
    env.feed(b"garbage")
    env.pop()
    env.reset()
    assert env.stats.bytes_dropped == 7
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)


def test_stats_is_a_snapshot_the_caller_cannot_edit(env):
    before = env.stats
    env.feed(b"junk")
    env.pop()
    assert before.bytes_dropped == 0
    assert env.stats.bytes_dropped == 4
    before.bytes_dropped = 999
    assert env.stats.bytes_dropped == 4
    assert isinstance(before, EnvelopeStats)


def test_expects_ack_is_true_for_li_usb(env):
    assert env.expects_ack is True


def test_the_wire_log_shows_bytes_as_they_appear_on_the_wire(env, caplog):
    caplog.set_level(logging.DEBUG, logger="railctl.wire")
    env.note_request(VERSION_REQUEST)
    env.wrap(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    env.pop()
    env.feed(b"\xff\xfd" + BROADCAST)
    env.pop()
    env.feed(b"\x12\x34")
    env.pop()
    assert [record.getMessage() for record in caplog.records] == [
        "TX FF FE 21 21 00",
        "RX FF FE 63 21 40 12 10",
        "RX! FF FD 61 01 60",
        "RX? 12 34",
    ]


def test_hex_bytes_is_the_one_wire_rendering():
    assert hex_bytes(b"\x01\x04\x05") == "01 04 05"
    assert hex_bytes(b"") == ""


STREAM = b"\xff\xfe" + VERSION_REPLY + b"\xff\xfd" + BROADCAST + b"\xff\xfe" + ACK
EXPECTED = [
    Frame(Kind.SOLICITED, VERSION_REPLY),
    Frame(Kind.UNSOLICITED, BROADCAST),
    Frame(Kind.SOLICITED, ACK),
]


@given(sizes=st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=20))
def test_arbitrary_chunking_yields_identical_frames(sizes):
    """USB CDC splits wherever it likes. Where the split falls must not change
    which frames come out, or a reply becomes silence for timing reasons alone.
    """
    env = LiUsbEnvelope()
    got: list[Frame] = []
    index = 0
    for size in itertools.cycle(sizes):
        if index >= len(STREAM):
            break
        env.feed(STREAM[index : index + size])
        index += size
        while (frame := env.pop()) is not None:
            got.append(frame)
    assert got == EXPECTED
