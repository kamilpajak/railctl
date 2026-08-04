# src/railctl/xbus/codec.py
"""X-Bus telegram codec: bare telegrams in, bare telegrams out.

A telegram is a header, N data bytes and an XOR. Its length is `(header & 0x0F) + 2`:
the low nibble of the header counts the data bytes, and the +2 covers the header
itself and the checksum byte.

Two things this module deliberately does NOT do:

* it never prepends `FF FE` (or `FF FD`). That prefix belongs to the LI-USB
  envelope, and it is never part of the XOR. A checksum computed over the prefix
  is wrong by 0x01, the station answers nothing, and "no answer" is how this
  project has repeatedly recorded a working capability as missing.
* it never interprets a telegram. `decode` returns the header and the data
  bytes; deciding what they mean is `replies.py`.
"""

from __future__ import annotations

from railctl.errors import (
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    XBusIncompleteError,
)

MAX_DATA_BYTES = 15
MAX_TELEGRAM_LEN = 17
MIN_TELEGRAM_LEN = 2

LENGTH_NIBBLE_MASK = 0x0F
LENGTH_OVERHEAD = 2
BYTE_MIN = 0
BYTE_MAX = 255


def xor(data: bytes) -> int:
    """XOR checksum over a bare telegram body.

    `xor(complete_telegram) == 0` for every valid telegram, which is the
    identity `decode` checks with.
    """
    result = 0
    for byte in data:
        result ^= byte
    return result


def telegram_length(header: int) -> int:
    """Total telegram size in bytes: header + N data bytes + XOR."""
    return (header & LENGTH_NIBBLE_MASK) + LENGTH_OVERHEAD


def encode(header: int, *data: int) -> bytes:
    """Build a complete telegram: header, data, XOR. No framing prefix.

    The data-byte count is derived from the header's low nibble and checked
    against the arguments, so an opcode whose argument list disagrees with its
    own declared length cannot ship. A telegram that lies about its length is
    worse than a rejected one: the station reads the *next* telegram from the
    wrong offset, so every later reply on that link is lost too.
    """
    if not BYTE_MIN <= header <= BYTE_MAX:
        raise XBusEncodeError(f"header {header} is not a byte in {BYTE_MIN}..{BYTE_MAX}")
    expected = header & LENGTH_NIBBLE_MASK
    if len(data) != expected:
        raise XBusEncodeError(
            f"header 0x{header:02X} declares {expected} data byte(s), got {len(data)}"
        )
    for index, byte in enumerate(data):
        if not BYTE_MIN <= byte <= BYTE_MAX:
            raise XBusEncodeError(
                f"data byte {index} = {byte} is not a byte in {BYTE_MIN}..{BYTE_MAX}"
            )
    body = bytes([header, *data])
    return body + bytes([xor(body)])


def decode(raw: bytes) -> tuple[int, bytes]:
    """Split a complete telegram into `(header, data)`, checksum removed.

    Three distinct faults, three distinct exception CLASSES, on purpose:

    * shorter than `MIN_TELEGRAM_LEN`  -> XBusIncompleteError (keep reading)
    * length disagrees with the header -> XBusDecodeError     (resync; more bytes
                                                               will not help)
    * checksum non-zero                -> XBusChecksumError   (complete but damaged)

    The first and third are subclasses of the second, so `except XBusDecodeError`
    still catches all three when the caller does not care. What matters is that a
    caller who DOES care separates them with `except`, never by matching on the
    message text - message text is free to change, and a link that keeps waiting
    for bytes that will never come ends as "no reply", which is how this project
    records a working capability as absent.
    """
    if len(raw) < MIN_TELEGRAM_LEN:
        raise XBusIncompleteError(
            f"telegram of {len(raw)} byte(s) is shorter than the minimum {MIN_TELEGRAM_LEN}"
        )
    expected = telegram_length(raw[0])
    if len(raw) != expected:
        raise XBusDecodeError(f"header 0x{raw[0]:02X} declares {expected} bytes, got {len(raw)}")
    if xor(raw) != 0:
        raise XBusChecksumError(
            f"checksum mismatch in {raw.hex(' ')}: expected 0x{xor(raw[:-1]):02X}"
        )
    return raw[0], raw[1:-1]
