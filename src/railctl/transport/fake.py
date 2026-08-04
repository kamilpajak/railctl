"""A Transport that replays a scripted station: no threads, no sleeps, no real clock.

Shipped rather than test-only, so a hardware probe can dry-run against it.

Four properties earn this file its place, and each exists because the M1 probe's
FakeLink lacked it:

1. The script is an ORDERED queue, so the same request may be answered
   differently each time it is sent. The probe's fake answered a payload with the
   same bytes for ever, which made a station that returns nothing until asked
   again with 21 10 inexpressible - so every mutant inside the service-result
   poll loop survived, including the one that deletes the loop. That deletion is
   the largest error of M1: no poll means the whole Lenz opcode family reads as
   silent, and two further capabilities get recorded as absent.
2. Writing a second command while the reply to the first is still queued raises.
   The LI-USB one-command rule becomes a mechanical check, not a review note.
3. A read that finds nothing advances the fake clock by its own timeout.
   Without it a Link waiting on monotonic() spins for ever against frozen time
   and the timeout path is untestable.
4. chunk_size=1 replays worst-case USB CDC fragmentation, so the whole suite can
   run twice: whole-frame and byte-at-a-time.

It knows nothing about framing. Every byte string here is exactly what would
appear on the wire; callers render bare telegrams through the envelope under
test, which is what keeps framing bytes out of the station and CLI suites.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from railctl.envelope import hex_bytes
from railctl.errors import PortNotOpen


class FakeClock:
    """monotonic()/sleep() with no real time. Duck-typed against railctl.link.Clock."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


@dataclass(frozen=True, slots=True)
class Exchange:
    request: bytes
    reply: bytes = b""


class FakeTransport:
    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        chunk_size: int | None = None,
        max_write: int | None = None,
        on_write: Callable[[bytes, FakeTransport], None] | None = None,
        description: str = "fake xpressnet",
        identity: str = "fake",
        diagnostic_hint: str = "check the station is reachable on the network",
    ) -> None:
        self.clock = FakeClock() if clock is None else clock
        self.chunk_size = chunk_size
        self.max_write = max_write
        self.on_write = on_write
        self.written: list[bytes] = []
        self.write_chunks: list[bytes] = []
        self.flushes = 0
        self._description = description
        self._identity = identity
        self._diagnostic_hint = diagnostic_hint
        self._script: deque[Exchange] = deque()
        self._rx = bytearray()
        self._partial = bytearray()
        self._open = False
        self._in_flight = False

    # -- scripting ---------------------------------------------------------
    def expect(self, request: bytes, *, reply: bytes = b"") -> FakeTransport:
        """Queue one exchange. reply=b"" scripts a station that says nothing."""
        self._script.append(Exchange(bytes(request), bytes(reply)))
        return self

    def queue(self, data: bytes) -> None:
        """Push bytes with no request behind them - a broadcast."""
        self._rx += data

    @property
    def script_pending(self) -> list[Exchange]:
        return list(self._script)

    # -- Transport ---------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return self._description

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def diagnostic_hint(self) -> str:
        # Link quotes this verbatim when a handshake or an exchange fails, so no
        # connection-specific advice has to live in link.py.
        return self._diagnostic_hint

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def flush_input(self) -> None:
        self._require_open()
        self._rx.clear()
        self.flushes += 1

    def write(self, data: bytes) -> None:
        self._require_open()
        limit = len(data) if self.max_write is None else max(1, self.max_write)
        offset = 0
        while offset < len(data):
            piece = data[offset : offset + limit]
            offset += len(piece)
            self.write_chunks.append(piece)
            self._accept(piece)
        if self._partial and not self._script:
            self._finish_callback_request()

    def read(self, max_bytes: int, timeout: float) -> bytes:
        self._require_open()
        if not self._rx:
            # The station said nothing. Burn the budget the caller was prepared
            # to wait, exactly as a real select() would.
            self.clock.sleep(timeout)
            self._in_flight = False
            return b""
        size = min(max_bytes, len(self._rx))
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        out = bytes(self._rx[:size])
        del self._rx[:size]
        if not self._rx:
            self._in_flight = False
        return out

    # -- internals ---------------------------------------------------------
    def _require_open(self) -> None:
        if not self._open:
            raise PortNotOpen("fake transport is not open")

    def _accept(self, piece: bytes) -> None:
        if self._in_flight:
            raise AssertionError(
                f"second command written while the reply to "
                f"{hex_bytes(self.written[-1])} was still outstanding; "
                f"LI-USB allows exactly one command in flight"
            )
        self._partial += piece
        if not self._script:
            return  # scriptless mode: on_write sees the whole write() call
        expected = self._script[0].request
        if not expected.startswith(bytes(self._partial)):
            raise AssertionError(
                "unexpected request\n"
                f"  expected {hex_bytes(expected)}\n"
                f"  got      {hex_bytes(bytes(self._partial))}"
            )
        if len(self._partial) < len(expected):
            return
        exchange = self._script.popleft()
        self._complete(exchange.request, exchange.reply)

    def _finish_callback_request(self) -> None:
        if self.on_write is None:
            raise AssertionError(
                f"unexpected request {hex_bytes(bytes(self._partial))}; the script is exhausted"
            )
        request = bytes(self._partial)
        self._complete(request, b"")
        self.on_write(request, self)

    def _complete(self, request: bytes, reply: bytes) -> None:
        self.written.append(request)
        self._partial.clear()
        self._rx += reply
        self._in_flight = True
