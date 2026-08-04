"""Property tests for the framing layer.

The example tests next door pin behaviour that was observed on the wire. These
pin the LAWS the layer has to obey for every input, which is the level at which
this project's characteristic bug lives: a frame that arrives and is not
returned looks exactly like a capability the hardware does not have.

Two properties are load-bearing:

- `test_a_well_formed_stream_survives_arbitrary_chunk_boundaries` covers
  `SerialLink.collect`, which appends whatever `os.read` happens to hand back
  and re-runs `split_frames` on the accumulated buffer. Chunk boundaries are
  decided by the USB stack, so a parse that depends on them would drop replies
  non-deterministically - the hardest possible failure to reproduce.
- `test_every_returned_frame_really_occurs_in_the_buffer` is the converse
  guard: the parser must never manufacture a frame. A resync that invents one
  would report a capability that was never demonstrated.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from tools.probe.frames import (
    LI_BROADCAST,
    LI_COMMAND,
    build,
    split_frames,
    telegram_length,
    xor,
)

BYTES = st.integers(min_value=0, max_value=255)


@st.composite
def payloads(draw: st.DrawFn) -> bytes:
    """A well-formed X-Bus payload: header plus exactly the data bytes it declares.

    The low nibble of the header IS the data-byte count (see telegram_length), so
    a payload is only well formed when the two agree. Drawing them independently
    would mostly generate telegrams no command station would ever send.
    """
    header = draw(BYTES)
    data = draw(st.lists(BYTES, min_size=header & 0x0F, max_size=header & 0x0F))
    return bytes([header, *data])


@st.composite
def frame_bytes(draw: st.DrawFn) -> tuple[bytes, bytes, bytes]:
    """(wire bytes, prefix, payload) for one complete frame of either kind."""
    prefix = draw(st.sampled_from([LI_COMMAND, LI_BROADCAST]))
    payload = draw(payloads())
    return prefix + payload + bytes([xor(payload)]), prefix, payload


# Noise that cannot start a prefix and cannot combine with the byte after it to
# form one, because every prefix begins with 0xFF. Keeping 0xFF out is what makes
# "the frames are recovered exactly" a true statement rather than a wish: the
# format has no escaping, so genuine 0xFF noise can be indistinguishable from a
# frame boundary, and no parser can be required to tell them apart.
noise = st.binary(max_size=12).filter(lambda b: 0xFF not in b)


def test_xor_closes_the_telegram_to_zero():
    """The checksum's defining property: the whole telegram XORs to zero."""

    @given(payloads())
    def prop(payload: bytes) -> None:
        assert xor(payload + bytes([xor(payload)])) == 0

    prop()


@given(st.binary(max_size=32), st.binary(max_size=32))
def test_xor_distributes_over_concatenation(left: bytes, right: bytes):
    """XOR over a byte string is a homomorphism, so order and grouping cannot matter."""
    assert xor(left + right) == xor(left) ^ xor(right)


@given(payloads())
def test_build_then_split_returns_the_payload_unchanged(payload: bytes):
    frames, rest = split_frames(build(payload))
    assert rest == b""
    assert [(f.prefix, f.telegram) for f in frames] == [(LI_COMMAND, payload)]
    assert frames[0].solicited is True


@given(payloads())
def test_a_built_frame_is_exactly_as_long_as_its_header_declares(payload: bytes):
    """A header whose nibble disagrees with the body desynchronises the whole stream.

    Everything after such a frame is read from the wrong offset, so this is not a
    cosmetic check: it is the difference between one bad reply and every
    subsequent reply being lost.
    """
    frame = build(payload)
    assert len(frame) == len(LI_COMMAND) + telegram_length(payload[0])


@given(st.binary(max_size=200))
def test_split_frames_never_raises_on_arbitrary_bytes(buffer: bytes):
    """The probe reads from a USB port shared with a telemetry stream, so the
    parser must treat every byte sequence as ordinary input, not as an error."""
    frames, rest = split_frames(buffer)
    assert isinstance(rest, bytes)
    assert all(isinstance(f.telegram, bytes) for f in frames)


@given(st.binary(max_size=200))
def test_the_remainder_is_always_a_suffix_of_the_input(buffer: bytes):
    _frames, rest = split_frames(buffer)
    assert buffer.endswith(rest)
    assert len(rest) <= len(buffer)


@given(st.binary(max_size=200))
def test_returning_a_frame_always_consumes_input(buffer: bytes):
    """Without this, `SerialLink.collect` could re-emit the same frame forever:
    it feeds the remainder straight back in on the next read."""
    frames, rest = split_frames(buffer)
    if frames:
        assert len(rest) < len(buffer)


@given(st.binary(max_size=200))
def test_every_returned_frame_really_occurs_in_the_buffer(buffer: bytes):
    """No frame may be manufactured: prefix, telegram and checksum must all be
    bytes that actually arrived, in that order."""
    frames, _ = split_frames(buffer)
    for frame in frames:
        assert frame.prefix + frame.telegram + bytes([xor(frame.telegram)]) in buffer


@given(st.binary(max_size=200))
def test_every_returned_frame_has_a_valid_checksum_and_declared_length(buffer: bytes):
    frames, _ = split_frames(buffer)
    for frame in frames:
        assert frame.prefix in (LI_COMMAND, LI_BROADCAST)
        assert len(frame.telegram) + 1 == telegram_length(frame.telegram[0])


@given(noise, payloads())
def test_noise_without_a_prefix_byte_never_hides_a_frame(junk: bytes, payload: bytes):
    frames, rest = split_frames(junk + build(payload))
    assert [f.telegram for f in frames] == [payload]
    assert rest == b""


@given(st.integers(min_value=1, max_value=5), payloads())
def test_stray_prefixes_before_a_frame_never_produce_silence(strays: int, payload: bytes):
    """The regression that made this parser lose a reply entirely.

    The claim is deliberately weak - some frame must come back, not necessarily
    this one. A run of prefix bytes followed by arbitrary data can genuinely
    admit more than one valid reading, and the parser is not required to guess
    which the station meant. Returning NOTHING is the failure: silence is how
    this project records "the hardware cannot do it".
    """
    frames, _ = split_frames(LI_COMMAND * strays + build(payload))
    assert frames, "a complete frame was in the buffer and nothing came back"


@given(st.lists(st.tuples(noise, frame_bytes()), max_size=5), noise)
def test_a_well_formed_stream_is_parsed_frame_for_frame(
    segments: list[tuple[bytes, tuple[bytes, bytes, bytes]]], trailing: bytes
):
    buffer = b"".join(junk + wire for junk, (wire, _, _) in segments) + trailing
    expected = [(prefix, payload) for _, (_, prefix, payload) in segments]
    frames, rest = split_frames(buffer)
    assert [(f.prefix, f.telegram) for f in frames] == expected
    # Noise is discarded as it is scanned past, so the remainder is a SUFFIX of
    # the trailing bytes rather than all of them. What matters is that nothing
    # belonging to a frame is left behind, and that a partial frame still would
    # be - which the incomplete-frame example test pins directly.
    assert trailing.endswith(rest)


@given(
    st.lists(st.tuples(noise, frame_bytes()), max_size=5),
    st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=40),
)
def test_a_well_formed_stream_survives_arbitrary_chunk_boundaries(
    segments: list[tuple[bytes, tuple[bytes, bytes, bytes]]], sizes: list[int]
):
    """Reassembling across reads must give the same frames as one big read.

    This is the `SerialLink.collect` contract. The USB stack decides where a
    read stops, so any dependence on chunk boundaries turns into replies that go
    missing on some runs and not others.
    """
    buffer = b"".join(junk + wire for junk, (wire, _, _) in segments)
    expected = [(prefix, payload) for _, (_, prefix, payload) in segments]

    pending = b""
    collected = []
    position = 0
    while position < len(buffer):
        size = sizes[position % len(sizes)]
        pending += buffer[position : position + size]
        position += size
        frames, pending = split_frames(pending)
        collected.extend(frames)

    assert [(f.prefix, f.telegram) for f in collected] == expected
    assert pending == b""


@given(payloads(), payloads())
def test_two_frames_back_to_back_are_both_recovered(first: bytes, second: bytes):
    frames, rest = split_frames(build(first) + build(second))
    assert rest == b""
    assert [f.telegram for f in frames] == [first, second]


@given(payloads(), BYTES)
def test_a_corrupted_checksum_never_yields_that_frame(payload: bytes, wrong: int):
    """A frame whose checksum was damaged in transit must not be handed on as data."""
    assume(wrong != xor(payload))
    frames, _ = split_frames(LI_COMMAND + payload + bytes([wrong]))
    assert payload not in [f.telegram for f in frames]
