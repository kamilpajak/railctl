"""Locomotive address wire form.

Below the threshold an address goes out as `(0x00, address)`. At or above it the
address is marked long: `value = address | 0xC000`, split into high and low
bytes. One function serves both dialects, because XpressNet's "add 0xC000 then
split" and Z21's "DB1 = 0xC0 | Adr_MSB" produce identical bytes for every
address in range - the ONLY difference between the dialects is the threshold,
100 or 128, and it arrives as one integer.

Measured on the YD7010: an address of 128 or above needs the 0xC0 marker on the
high byte. The 100..127 band, where the two dialects disagree, is documented
rather than measured on this hardware.

This module shifts an address by 8. That is not CV arithmetic, and the layering
grep for `>> 8` covers `station/`, `cli/` and `xbus/commands.py`, none of which
is this file.
"""

from __future__ import annotations

LOCO_ADDR_MIN = 1
LOCO_ADDR_MAX = 9999  # station limit; the wire field itself holds 14 bits

LONG_ADDRESS_FLAG = 0xC000
LONG_ADDRESS_MASK = 0x3FFF

_BYTE_MIN = 0
_BYTE_MAX = 255


def encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]:
    """Return `(adr_high, adr_low)` for a 1-based locomotive address."""
    if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
        raise ValueError(f"loco address {address} out of range {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}")
    value = address | LONG_ADDRESS_FLAG if address >= long_threshold else address
    return (value >> 8) & 0xFF, value & 0xFF


def decode_loco_address(adr_high: int, adr_low: int) -> int:
    """Recover a locomotive address from its two wire bytes.

    A high byte carrying exactly one of the two marker bits is refused rather
    than guessed at: no encoder produces that form, and turning it into a number
    would publish an address nothing ever sent.
    """
    for byte in (adr_high, adr_low):
        if not _BYTE_MIN <= byte <= _BYTE_MAX:
            raise ValueError(f"{byte} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")
    value = (adr_high << 8) | adr_low
    marker = value & LONG_ADDRESS_FLAG
    if marker == LONG_ADDRESS_FLAG:
        return value & LONG_ADDRESS_MASK
    if marker:
        raise ValueError(
            f"{adr_high:02X} {adr_low:02X} is not a locomotive address: the long marker "
            f"is 0x{LONG_ADDRESS_FLAG:04X}, both bits or neither"
        )
    return value
