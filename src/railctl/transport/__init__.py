"""Byte pipes. A transport moves bytes and knows nothing about framing.

read() blocks up to `timeout`, returns as soon as at least one byte is
available, returns b"" on timeout, and never raises on timeout - framing is not
its problem. write() writes everything or raises TransportError.

diagnostic_hint is here rather than in link.py on purpose. When a handshake or an
exchange fails, the useful advice is about the CONNECTION, and only the transport
knows what kind of connection it is - a CDC interface index for the serial port, a
network address for the future Z21 UDP transport. Spec line 583 requires the LAN
transport to land with no edit to link.py, so link.py must not hold either
sentence.
"""

from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, max_bytes: int, timeout: float) -> bytes: ...
    def flush_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def description(self) -> str: ...
    @property
    def identity(self) -> str: ...
    @property
    def diagnostic_hint(self) -> str: ...
