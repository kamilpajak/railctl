"""Frame classification, checksum validation and the wire log.

The envelope owns the bytes that surround an X-Bus telegram, in both directions,
and it is the only layer that logs them. Link never logs wire bytes: with two
loggers the same frame appears twice or, worse, once with the framing and once
without, and the wire log is the primary instrument for every hardware probe.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class Kind(enum.Enum):
    SOLICITED = "solicited"  # a reply to the command we sent
    UNSOLICITED = "unsolicited"  # broadcast or spontaneous


@dataclass(frozen=True, slots=True)
class Frame:
    """A complete X-Bus telegram, header..XOR, framing stripped, XOR verified.

    Frozen because a frame is evidence: it is what a capability verdict rests on
    and what the wire log prints. A mutable one lets a later stage rewrite what
    an earlier stage saw.
    """

    kind: Kind
    payload: bytes


@dataclass
class EnvelopeStats:
    """Counters that make silence diagnosable.

    frames_ok stuck at 0 while bytes_dropped climbs is what distinguishes "the
    wrong CDC interface" from "a dead port", with no extra flag anywhere.
    """

    frames_ok: int = 0
    bytes_dropped: int = 0
    bad_xor: int = 0
    stray_replies: int = 0
    resyncs: int = 0


def hex_bytes(data: bytes) -> str:
    """Wire bytes as the log and every error message render them: 21 21 00."""
    return " ".join(f"{byte:02X}" for byte in data)


class Envelope(Protocol):
    def wrap(self, telegram: bytes) -> bytes: ...
    def frame(self, kind: Kind, telegram: bytes) -> bytes: ...
    def feed(self, data: bytes) -> None: ...
    def pop(self) -> Frame | None: ...
    def note_request(self, telegram: bytes) -> None: ...
    def note_reply(self, frame: Frame) -> None: ...
    def note_abandoned(self) -> None: ...
    def reset(self) -> None: ...
    @property
    def expects_ack(self) -> bool: ...
    @property
    def stats(self) -> EnvelopeStats: ...
