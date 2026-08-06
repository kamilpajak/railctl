"""The one test double every tests/station/ test is built on.

Bare telegrams in, bare telegrams out. The envelope under test does all the framing, so nothing
in tests/station/ ever names the framing prefix - which tests/unit/test_envelope_isolation.py
enforces (tests/station is in its SCANNED list and this file is not on its ALLOWED list), and
which is what lets a future Z21Envelope run this whole suite with no edit here.

Two axes come from tests/conftest.py and apply to every test that takes `bench` or
`bench_factory`: `chunk_size` (whole-frame, then one byte at a time) and `envelope_factory` (the
envelope class under test). Neither is ever passed by a test.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.link import Link
from railctl.station.capabilities import Capabilities
from railctl.station.facade import Station
from railctl.station.timing import TIMING, Timing
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.commands import cmd_station_version

BENCH_IDENTITY = "bench"
BENCH_DEFAULT_ADDRESS = 3
# 21 21 00 - the telegram Link.open() sends itself. Taken from the encoder rather than typed, so
# this file cannot drift from link.py's _HANDSHAKE_TELEGRAM (tests/unit/test_link.py pins those two
# against each other already).
_HANDSHAKE_REQUEST = cmd_station_version()
_HANDSHAKE_REPLY = b"\x63\x21\x40\x12\x10"


class Bench:
    """An open Station over a scripted FakeTransport, speaking bare telegrams."""

    def __init__(
        self,
        *,
        chunk_size: int | None = None,
        envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
        capabilities: Capabilities | None = None,
        default_address: int | None = BENCH_DEFAULT_ADDRESS,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
        **capability_overrides: object,
    ) -> None:
        self._envelope_cls = envelope_cls
        self.envelope = envelope_cls()
        self.clock = FakeClock()
        self.transport = FakeTransport(clock=self.clock, chunk_size=chunk_size)
        self.link = Link(self.transport, self.envelope, clock=self.clock)
        self.events: list[tuple[str, dict[str, object]]] = []
        self.on_event_hook: Callable[[str, dict[str, object]], None] | None = None
        self._handshake_writes = 0
        caps = Capabilities.unknown(BENCH_IDENTITY) if capabilities is None else capabilities
        if capability_overrides:
            # with_learned accepts any real field name and raises ValueError naming a wrong one,
            # so bench_factory(loco_address_threshold=128) is checked rather than swallowed.
            caps = caps.with_learned(**capability_overrides)
        self.station = Station(
            self.link,
            caps,
            default_address=default_address,
            capabilities_path=capabilities_path,
            timing=timing,
            clock=self.clock.monotonic,
            sleep=self.clock.sleep,
            on_event=self._on_event,
        )

    # -- scripting ---------------------------------------------------------
    def expect(
        self,
        request: bytes,
        reply: bytes | tuple[bytes, ...] = b"",
        *,
        broadcast: bytes | tuple[bytes, ...] = (),
    ) -> Bench:
        """Queue exactly one exchange, in bare telegrams.

        `request` is the next telegram the station must send; a mismatch fails the test from
        inside FakeTransport, naming both telegrams in hex.

        `reply` is one telegram, or a tuple of telegrams delivered as one burst of solicited
        frames. `reply=b""` scripts a station that ACCEPTS the request and then says nothing, so
        the exchange ends in LinkTimeout.

        SILENCE IS ALWAYS SCRIPTED, NEVER IMPLIED. An unscripted request does not time out - it
        makes FakeTransport raise AssertionError("the script is exhausted"). A test that expects a
        timeout must still call expect(), and must write `reply=b""` explicitly rather than
        leaving the argument off, so the intent is greppable.

        `broadcast` appends unsolicited frames after the solicited ones, for the rare case where a
        broadcast must arrive inside one exchange. A broadcast that merely has to be seen belongs
        in push() before the call.
        """
        self.transport.expect(
            self.envelope.frame(Kind.SOLICITED, request),
            reply=self._frames(Kind.SOLICITED, reply) + self._frames(Kind.UNSOLICITED, broadcast),
        )
        return self

    def push(self, telegram: bytes) -> Bench:
        """An UNSOLICITED frame with no request behind it - a broadcast.

        Solicited frames answer a command and are consumed by the exchange that asked for them;
        an unsolicited one is dispatched to Station's on_event as it passes and never satisfies a
        pending request. This is the only way a test produces a broadcast.
        """
        self.transport.queue(self.envelope.frame(Kind.UNSOLICITED, telegram))
        return self

    def reply(self, telegram: bytes) -> Bench:
        """One solicited frame, queued directly. For an on_write responder answering the request
        it was just handed; tests use expect() instead."""
        self.transport.queue(self.envelope.frame(Kind.SOLICITED, telegram))
        return self

    def open(self) -> Bench:
        self.expect(_HANDSHAKE_REQUEST, _HANDSHAKE_REPLY)
        self.link.open()
        self._handshake_writes = len(self.transport.written)
        return self

    # -- what was sent -----------------------------------------------------
    @property
    def sent(self) -> list[bytes]:
        """Every telegram written since open(), BARE and in order.

        transport.written holds what Link handed the transport, which is framed. Comparing that
        against a bare telegram silently never matches, so no test reads it: they read this.
        The handshake is excluded - it is fixture ceremony, not part of any test's subject - so
        `len(bench.sent)` counts the operation's own telegrams, and `bench.sent.count(...)` of
        cmd_station_version() counts version() calls without the open() handshake in the tally.

        Decoded through a FRESH envelope instance so the one under test keeps its buffer and its
        counters untouched.
        """
        decoder = self._envelope_cls()
        decoder.feed(b"".join(self.transport.written[self._handshake_writes :]))
        out: list[bytes] = []
        while (frame := decoder.pop()) is not None:
            out.append(frame.payload)
        return out

    def unframe(self, framed: bytes) -> bytes:
        """One framed request (as an on_write responder is handed) back to its bare telegram."""
        decoder = self._envelope_cls()
        decoder.feed(framed)
        frame = decoder.pop()
        assert frame is not None, f"not one complete frame: {len(framed)} bytes"
        return frame.payload

    # -- events ------------------------------------------------------------
    def _on_event(self, name: str, payload: dict[str, object]) -> None:
        self.events.append((name, payload))
        if self.on_event_hook is not None:
            self.on_event_hook(name, payload)

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    # -- internals ---------------------------------------------------------
    def _frames(self, kind: Kind, telegrams: bytes | tuple[bytes, ...]) -> bytes:
        if isinstance(telegrams, bytes):
            telegrams = (telegrams,)
        return b"".join(self.envelope.frame(kind, one) for one in telegrams if one)


@pytest.fixture
def bench_factory(chunk_size, envelope_factory) -> Callable[..., Bench]:
    """Build and OPEN a fresh Bench. One handshake exchange is already spent.

    Depends on both `chunk_size` and `envelope_factory` (tests/conftest.py), so every test built
    on bench/bench_factory runs under both axes, not just the byte-delivery one.
    """

    def make(**kwargs: object) -> Bench:
        return Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs).open()

    return make


@pytest.fixture
def bench(bench_factory: Callable[..., Bench]) -> Bench:
    return bench_factory()
