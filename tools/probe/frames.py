"""LI-USB framing for the YD7010 XpressNet port.

Every command must carry the FF FE prefix; without it the port stays silent.
The prefix bytes are never part of the XOR checksum.
"""

from __future__ import annotations

from dataclasses import dataclass

LI_COMMAND = b"\xff\xfe"
LI_BROADCAST = b"\xff\xfd"
_PREFIXES = (LI_COMMAND, LI_BROADCAST)


def xor(payload: bytes) -> int:
    """XOR checksum over an X-Bus telegram body (header + data, no prefix)."""
    result = 0
    for byte in payload:
        result ^= byte
    return result


def build(payload: bytes) -> bytes:
    """Wrap header+data into a complete LI-USB command frame."""
    return LI_COMMAND + payload + bytes([xor(payload)])


def telegram_length(header: int) -> int:
    """Total telegram size: header + N data bytes + XOR, where N is the low nibble."""
    return (header & 0x0F) + 2


@dataclass(frozen=True)
class Frame:
    prefix: bytes
    telegram: bytes

    @property
    def solicited(self) -> bool:
        return self.prefix == LI_COMMAND


INCOMPLETE = "incomplete"
CORRUPT = "corrupt"


def _frame_at(buffer: bytes, pos: int) -> tuple[Frame, int] | str:
    """A complete checksum-valid frame starting at pos, or why there is not one."""
    prefix = buffer[pos : pos + 2]
    if prefix not in _PREFIXES:
        return CORRUPT
    if pos + 2 >= len(buffer):
        return INCOMPLETE
    end = pos + 2 + telegram_length(buffer[pos + 2])
    if end > len(buffer):
        return INCOMPLETE
    telegram = buffer[pos + 2 : end]
    if xor(telegram[:-1]) != telegram[-1]:
        return CORRUPT
    return Frame(prefix, telegram[:-1]), end


def _salvage(buffer: bytes, start: int) -> tuple[Frame, int] | None:
    """The first checksum-valid frame at or after start, if any."""
    for pos in range(start, len(buffer) - 1):
        found = _frame_at(buffer, pos)
        if not isinstance(found, str):
            return found
    return None


def split_frames(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Consume as many complete, checksum-valid frames as possible.

    Unrecognised bytes are skipped one at a time so the stream can resync after
    noise — for example after accidentally opening the YD.Control telemetry port.

    A stray prefix is the awkward case. `FF FE` followed by a real frame used to
    return NOTHING: the header was read from the stray prefix's offset, the
    length it implied overran the buffer, and the loop broke to wait for bytes
    that never came. `SerialLink.exchange` flushes at the next command, so the
    frame was simply lost — a reply recorded as silence, which in this project
    is the difference between "unsupported" and "not established".

    So an incomplete candidate is no longer trusted on its own. If a
    checksum-valid frame exists further along, that is strong evidence the
    candidate was noise and the loop resyncs to the real frame. Only when no
    such frame exists does it wait, because then the candidate may genuinely
    still be arriving.
    """
    frames: list[Frame] = []
    pos = 0
    while pos < len(buffer) - 1:
        if buffer[pos : pos + 2] not in _PREFIXES:
            pos += 1
            continue
        found = _frame_at(buffer, pos)
        if found == CORRUPT:
            pos += 1
            continue
        if found == INCOMPLETE:
            rescued = _salvage(buffer, pos + 1)
            if rescued is None:
                break
            frame, pos = rescued
            frames.append(frame)
            continue
        frame, pos = found
        frames.append(frame)
    return frames, buffer[pos:]
