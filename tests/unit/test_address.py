"""Locomotive address wire form.

One function serves both dialects because XpressNet's "add 0xC000, then split"
and Z21's "DB1 = 0xC0 | Adr_MSB" produce identical bytes for every address the
station accepts. That claim is asserted exhaustively below rather than argued,
because the only real difference - the threshold at which an address becomes
long - is a single integer, and getting it wrong is silent: a decoder addressed
in the wrong form does nothing at all and reports nothing at all.
"""

from __future__ import annotations

import pytest

from railctl.xbus.address import (
    LOCO_ADDR_MAX,
    LOCO_ADDR_MIN,
    LONG_ADDRESS_FLAG,
    decode_loco_address,
    encode_loco_address,
)

# (address, threshold) -> (high, low). The 100..127 rows are the divergence band.
ADDRESS_VECTORS = [
    ((1, 100), (0x00, 0x01)),
    ((99, 100), (0x00, 0x63)),
    ((100, 100), (0xC0, 0x64)),
    ((127, 100), (0xC0, 0x7F)),
    ((128, 100), (0xC0, 0x80)),
    ((1000, 100), (0xC3, 0xE8)),
    ((1234, 100), (0xC4, 0xD2)),
    ((9999, 100), (0xE7, 0x0F)),
    ((100, 128), (0x00, 0x64)),
    ((127, 128), (0x00, 0x7F)),
    ((128, 128), (0xC0, 0x80)),
]


@pytest.mark.parametrize(("args", "expected"), ADDRESS_VECTORS)
def test_encode_matches_the_golden_bytes(args: tuple[int, int], expected: tuple[int, int]):
    address, threshold = args
    assert encode_loco_address(address, long_threshold=threshold) == expected


@pytest.mark.parametrize("address", [0, -1, 10000, 100000])
def test_encode_rejects_an_address_outside_the_station_range(address: int):
    with pytest.raises(ValueError, match="out of range"):
        encode_loco_address(address, long_threshold=100)


@pytest.mark.parametrize("high", [0x80, 0x40])
def test_decode_rejects_a_high_byte_with_only_one_marker_bit(high: int):
    """0xC0 means long. 0x80 or 0x40 alone is not a form any encoder produces.

    Returning a number for it would publish a locomotive address that nothing
    sent, so it is refused by name instead.
    """
    with pytest.raises(ValueError, match="not a locomotive address"):
        decode_loco_address(high, 0x64)


@pytest.mark.parametrize(("high", "low"), [(256, 0x00), (0x00, -1)])
def test_decode_rejects_a_wire_byte_outside_a_byte(high: int, low: int):
    with pytest.raises(ValueError, match="not a byte"):
        decode_loco_address(high, low)


def test_zero_decodes_to_zero_which_is_not_a_locomotive():
    """00 00 is the empty address slot, not loco 0. The caller decides."""
    assert decode_loco_address(0x00, 0x00) == 0
    assert LOCO_ADDR_MIN == 1


def test_every_address_survives_the_round_trip_under_both_thresholds():
    for threshold in (100, 128):
        for address in range(LOCO_ADDR_MIN, LOCO_ADDR_MAX + 1):
            high, low = encode_loco_address(address, long_threshold=threshold)
            assert decode_loco_address(high, low) == address


def test_the_two_dialect_formulas_produce_identical_bytes():
    """XpressNet: address | 0xC000, then split. Z21: DB1 = 0xC0 | Adr_MSB.

    Asserted for every long address the station accepts, so that "one function
    covers both dialects" is a measured claim rather than a convenience.
    """
    for address in range(100, LOCO_ADDR_MAX + 1):
        high, low = encode_loco_address(address, long_threshold=100)
        assert high == (0xC0 | ((address >> 8) & 0x3F))
        assert low == address & 0xFF


def test_the_long_marker_follows_the_threshold_and_nothing_else():
    for threshold in (100, 128):
        for address in range(LOCO_ADDR_MIN, LOCO_ADDR_MAX + 1):
            high, _ = encode_loco_address(address, long_threshold=threshold)
            is_long = (high << 8) & LONG_ADDRESS_FLAG == LONG_ADDRESS_FLAG
            assert is_long is (address >= threshold)
