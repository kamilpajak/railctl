"""LI-USB framing for the YD7010 XpressNet port (Lenz 23151 section 1.3).

Every command carries the two-byte solicited prefix; without it the port stays
silent. Broadcasts carry the unsolicited prefix. Neither prefix is part of the
XOR, so the codec never sees them.

Why the header nibble and not a delimiter search: the prefix bytes occur inside
legitimate payloads - ff fe e4 13 00 03 ff 0b is loco 3 at step 126 forward. The
correct order is anchor once on the prefix, trust the low nibble for the length,
let the XOR confirm.

The XOR here is deliberately not imported from railctl.xbus.codec. xbus sits
above link and this module sits below it; importing upward would invert the
layering and drag xbus behind the future Z21Envelope. tests/unit/test_link.py
pins the two implementations against each other.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from railctl.envelope import EnvelopeStats, Frame, Kind, hex_bytes

PREFIX_SOLICITED = b"\xff\xfe"
PREFIX_UNSOLICITED = b"\xff\xfd"
MAX_BUFFER = 4096
_MARKER = 0xFF
_PREFIXES = {Kind.SOLICITED: PREFIX_SOLICITED, Kind.UNSOLICITED: PREFIX_UNSOLICITED}
_KIND_BY_SECOND_BYTE = {0xFE: Kind.SOLICITED, 0xFD: Kind.UNSOLICITED}
_ALL_PREFIXES = (PREFIX_SOLICITED, PREFIX_UNSOLICITED)
_MIN_TELEGRAM = 2

_wire = logging.getLogger("railctl.wire")


def _xor(telegram: bytes) -> int:
    result = 0
    for byte in telegram:
        result ^= byte
    return result


class LiUsbEnvelope:
    expects_ack = True  # 23151 section 1.3: every command is acknowledged

    def __init__(self) -> None:
        self._buf = bytearray()
        self._counters = EnvelopeStats()
        self._outstanding: bytes | None = None

    @property
    def stats(self) -> EnvelopeStats:
        # A copy: a snapshot taken before an operation must stay valid, and no
        # caller gets to edit the counters a hardware verdict is read from.
        return replace(self._counters)

    def frame(self, kind: Kind, telegram: bytes) -> bytes:
        """The exact bytes this envelope puts on the wire for a frame of `kind`."""
        return _PREFIXES[kind] + telegram

    def wrap(self, telegram: bytes) -> bytes:
        framed = self.frame(Kind.SOLICITED, telegram)
        if _wire.isEnabledFor(logging.DEBUG):
            _wire.debug("TX %s", hex_bytes(framed))
        return framed

    def note_request(self, telegram: bytes) -> None:
        self._outstanding = bytes(telegram)

    def note_reply(self, frame: Frame) -> None:
        self._outstanding = None

    def note_abandoned(self) -> None:
        self._outstanding = None

    def reset(self) -> None:
        # The buffer is per-connection, the counters are per-session: the M4
        # acceptance check reads bytes_dropped after a failed open.
        self._buf.clear()
        self._outstanding = None

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf += data
        excess = len(self._buf) - MAX_BUFFER
        if excess > 0:
            self._discard(excess)

    def pop(self) -> Frame | None:
        while True:
            start = self._buf.find(_MARKER)
            if start == -1:
                if self._buf:
                    self._discard(len(self._buf))
                return None
            if start:
                self._discard(start)
            if len(self._buf) < 2:
                return None
            kind = _KIND_BY_SECOND_BYTE.get(self._buf[1])
            if kind is None:
                self._discard(1)
                continue
            if len(self._buf) < 3:
                return None
            total = (self._buf[2] & 0x0F) + _MIN_TELEGRAM
            if len(self._buf) < 2 + total:
                rescue = self._salvage_start()
                if rescue is None:
                    return None
                self._discard(rescue)
                continue
            telegram = bytes(self._buf[2 : 2 + total])
            if _xor(telegram) != 0:
                self._counters.bad_xor += 1
                # One byte, never the whole candidate: the true start may lie
                # inside it.
                self._discard(1)
                continue
            del self._buf[: 2 + total]
            self._counters.frames_ok += 1
            if kind is Kind.SOLICITED and self._outstanding is None:
                self._counters.stray_replies += 1
            if _wire.isEnabledFor(logging.DEBUG):
                mark = "RX" if kind is Kind.SOLICITED else "RX!"
                _wire.debug("%s %s", mark, hex_bytes(_PREFIXES[kind] + telegram))
            return Frame(kind=kind, payload=telegram)

    def _discard(self, count: int) -> None:
        if _wire.isEnabledFor(logging.DEBUG):
            _wire.debug("RX? %s", hex_bytes(bytes(self._buf[:count])))
        self._counters.bytes_dropped += count
        self._counters.resyncs += 1
        del self._buf[:count]

    def _salvage_start(self) -> int | None:
        """Offset of the first complete, checksum-valid frame after position 0.

        An incomplete candidate is not trusted on its own. A stray prefix in
        front of a real frame reads its header from the stray's offset, and the
        length that implies overruns the buffer for ever - so the frame behind it
        was lost, and a lost reply is recorded one layer up as "the hardware
        cannot do this". If a checksum-valid frame exists further along, that is
        strong evidence the candidate was noise. Only when none exists do we
        wait, because then the candidate may genuinely still be arriving.
        """
        for pos in range(1, len(self._buf) - 1):
            if self._complete_at(pos):
                return pos
        return None

    def _complete_at(self, pos: int) -> bool:
        if bytes(self._buf[pos : pos + 2]) not in _ALL_PREFIXES:
            return False
        if pos + 3 > len(self._buf):
            return False
        end = pos + 2 + (self._buf[pos + 2] & 0x0F) + _MIN_TELEGRAM
        if end > len(self._buf):
            return False
        return _xor(bytes(self._buf[pos + 2 : end])) == 0
