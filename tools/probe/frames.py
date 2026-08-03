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


def split_frames(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Consume as many complete, checksum-valid frames as possible.

    Unrecognised bytes are skipped one at a time so the stream can resync after
    noise — for example after accidentally opening the YD.Control telemetry port.
    """
    frames: list[Frame] = []
    pos = 0
    while pos < len(buffer):
        prefix = buffer[pos : pos + 2]
        if len(prefix) < 2:
            break
        if prefix not in _PREFIXES:
            pos += 1
            continue
        if pos + 2 >= len(buffer):
            break
        header = buffer[pos + 2]
        size = telegram_length(header)
        end = pos + 2 + size
        if end > len(buffer):
            break
        telegram = buffer[pos + 2 : end]
        if xor(telegram[:-1]) == telegram[-1]:
            frames.append(Frame(prefix, telegram[:-1]))
            pos = end
        else:
            pos += 1
    return frames, buffer[pos:]
