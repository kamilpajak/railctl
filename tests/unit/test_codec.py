# tests/unit/test_codec.py
"""Byte-exact tests for the X-Bus codec.

Two rules from the hardware are pinned here and nowhere else:

* a telegram is `(header & 0x0F) + 2` bytes long, header and XOR included;
* the XOR covers the bare telegram and NEVER the `FF FE` framing prefix.

The golden rows are the T4 table from the design document. Every row is there
because it is a known top bug source, so they are written as literal bytes: a
row computed from the same helper it is testing proves nothing.
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    XBusIncompleteError,
)
from railctl.xbus.codec import (
    MAX_TELEGRAM_LEN,
    MIN_TELEGRAM_LEN,
    decode,
    encode,
    telegram_length,
    xor,
)

# (encode arguments, expected telegram). The first five rows are the drive
# telegrams of the T4 table, whose data bytes Task 5 produces; the CV rows are
# the ones whose data bytes Task 6 produces. Here they are literal on both
# sides, so a codec regression fails without any other module being involved.
GOLDEN_TELEGRAMS = [
    ((0x21, 0x21), b"\x21\x21\x00"),
    ((0x21, 0x24), b"\x21\x24\x05"),
    ((0x21, 0x81), b"\x21\x81\xa0"),
    ((0x21, 0x80), b"\x21\x80\xa1"),
    ((0x80,), b"\x80\x80"),
    ((0x92, 0x00, 0x03), b"\x92\x00\x03\x91"),
    ((0xE4, 0x13, 0x00, 0x63, 0x82), b"\xe4\x13\x00\x63\x82\x16"),
    ((0xE4, 0x13, 0xC0, 0x64, 0x82), b"\xe4\x13\xc0\x64\x82\xd1"),
    ((0xE4, 0x13, 0x00, 0x64, 0x82), b"\xe4\x13\x00\x64\x82\x11"),
    ((0xE4, 0x13, 0xC0, 0x7F, 0x82), b"\xe4\x13\xc0\x7f\x82\xca"),
    ((0xE4, 0x13, 0xC0, 0x80, 0x82), b"\xe4\x13\xc0\x80\x82\x35"),
    ((0x22, 0x15, 0x01), b"\x22\x15\x01\x36"),
    ((0x22, 0x15, 0xFF), b"\x22\x15\xff\xc8"),
    ((0x22, 0x18, 0x01), b"\x22\x18\x01\x3b"),
    ((0x22, 0x19, 0x00), b"\x22\x19\x00\x3b"),
    ((0x22, 0x18, 0x00), b"\x22\x18\x00\x3a"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE4, 0x00, 0x00), b"\xe6\x30\x00\x03\xe4\x00\x00\x31"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE4, 0x07, 0x00), b"\xe6\x30\x00\x03\xe4\x07\x00\x36"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE5, 0x00, 0x00), b"\xe6\x30\x00\x03\xe5\x00\x00\x30"),
    ((0x23, 0x11, 0x00, 0x1C), b"\x23\x11\x00\x1c\x2e"),
]

# Replies captured on the YD7010 (docs/probe-results.md) plus the two forms the
# design names as decode rows worth keeping.
#
# CAUTION on the first row. The design document USED to write this example as
# `decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x40\x12")`, which contradicts its
# own length rule: the low nibble of 0x63 is 3, so the telegram carries THREE
# data bytes, `21 40 12`, and `10` is the checksum. The rule wins over the
# example - a two-byte answer here would mean `decode` had silently dropped the
# `21`, the byte that says which reply form this is. Step 9 of this task edits
# the design document itself so the two agree; leaving the wrong example in the
# authoritative file would invite the next reader to "fix" the code back.
GOLDEN_REPLIES = [
    (b"\x63\x21\x40\x12\x10", 0x63, b"\x21\x40\x12"),
    (b"\x62\x22\x07\x47", 0x62, b"\x22\x07"),
    (b"\x63\x14\x08\x08\x77", 0x63, b"\x14\x08\x08"),
    (b"\x61\x82\xe3", 0x61, b"\x82"),
    (b"\x71\xaa\xdb", 0x71, b"\xaa"),
]


def test_xor_of_a_complete_telegram_is_zero():
    """The identity decode() relies on: XOR over header+data+checksum cancels."""
    assert xor(b"\x63\x21\x40\x12\x10") == 0


def test_xor_of_an_empty_buffer_is_zero():
    assert xor(b"") == 0


def test_xor_never_covers_the_framing_prefix():
    """FF FE is added by the envelope and is not part of the checksum.

    Including it would change the checksum of every command by 0x01, and the
    station would answer nothing at all - the failure that reads as "this
    command is unsupported".
    """
    assert xor(b"\x21\x81") == 0xA0
    assert xor(b"\xff\xfe\x21\x81") != 0xA0


def test_telegram_length_is_the_low_nibble_plus_two():
    assert telegram_length(0x21) == 3
    assert telegram_length(0x62) == 4
    assert telegram_length(0x63) == 5
    assert telegram_length(0xE4) == 6
    assert telegram_length(0xE6) == 8


def test_telegram_length_spans_the_documented_extremes():
    assert telegram_length(0x80) == MIN_TELEGRAM_LEN
    assert telegram_length(0xEF) == MAX_TELEGRAM_LEN


@pytest.mark.parametrize(("args", "expected"), GOLDEN_TELEGRAMS)
def test_encode_matches_the_golden_telegram(args: tuple[int, ...], expected: bytes):
    assert encode(*args) == expected


def test_encode_returns_a_bare_telegram_with_no_framing_prefix():
    assert not encode(0x21, 0x21).startswith(b"\xff\xfe")
    assert not encode(0x21, 0x21).startswith(b"\xff\xfd")


def test_encode_rejects_too_few_data_bytes():
    with pytest.raises(XBusEncodeError, match="declares 1 data byte"):
        encode(0x21)


def test_encode_rejects_too_many_data_bytes():
    with pytest.raises(XBusEncodeError, match="declares 1 data byte"):
        encode(0x21, 0x21, 0x00)


def test_encode_rejects_a_data_byte_outside_a_byte():
    with pytest.raises(XBusEncodeError, match="data byte 0"):
        encode(0x21, 256)
    with pytest.raises(XBusEncodeError, match="data byte 0"):
        encode(0x21, -1)


def test_encode_rejects_a_header_outside_a_byte():
    with pytest.raises(XBusEncodeError, match="header"):
        encode(256, 0x00)


@pytest.mark.parametrize(("raw", "header", "data"), GOLDEN_REPLIES)
def test_decode_splits_header_and_data(raw: bytes, header: int, data: bytes):
    assert decode(raw) == (header, data)


def test_decode_round_trips_every_golden_telegram():
    for args, telegram in GOLDEN_TELEGRAMS:
        header, data = decode(telegram)
        assert (header, *data) == args


def test_decode_rejects_a_bad_checksum():
    with pytest.raises(XBusChecksumError):
        decode(b"\x21\x21\x01")


def test_a_length_mismatch_is_a_decode_error_and_not_a_checksum_error():
    """Truncation and corruption must not look like the same fault.

    A short read that is reported as a checksum error tells the layer above to
    retry the same command; a checksum error reported as truncation tells it to
    wait for more bytes that will never come. Both end as "no reply", which is
    how this project records a capability as absent.

    `type(...) is XBusDecodeError` is exact on purpose: it fails if this case
    ever starts raising the incomplete-buffer subclass, which is what keeps the
    two provably distinct rather than distinct-by-message-text.
    """
    with pytest.raises(XBusDecodeError) as excinfo:
        decode(b"\x63\x21\x40")
    assert type(excinfo.value) is XBusDecodeError
    assert not isinstance(excinfo.value, XBusIncompleteError)
    assert "declares 5 bytes" in str(excinfo.value)


@pytest.mark.parametrize("raw", [b"", b"\x21"])
def test_decode_rejects_a_buffer_below_the_minimum_length(raw: bytes):
    """A buffer too short to hold a telegram is its OWN exception class.

    The link layer must tell "the reply has not finished arriving" (keep
    reading) from "the reply arrived damaged" (resync or retry). If both are a
    bare XBusDecodeError, the only way to tell them apart is to match on message
    text, which no caller should ever do - so the difference becomes a class.
    """
    with pytest.raises(XBusIncompleteError, match="shorter than"):
        decode(raw)


def test_an_incomplete_buffer_is_still_caught_by_the_general_decode_error():
    """A caller that does not need the distinction writes one except clause."""
    assert issubclass(XBusIncompleteError, XBusDecodeError)
    assert issubclass(XBusChecksumError, XBusDecodeError)
    with pytest.raises(XBusDecodeError):
        decode(b"\x21")
