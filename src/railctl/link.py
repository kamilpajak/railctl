"""One command in flight, retries, timeouts, and the frame pump.

Serialisation is a threading.RLock held for the whole duration of every method
that touches the transport, so the LI-USB one-command rule is structural rather
than conventional. RLock and not Lock because a station helper composing two
link calls in one critical section - a CV read is exactly that - must not
deadlock itself.

This module logs no wire bytes. The envelope owns the wire log in both
directions; see railctl/envelope/liusb.py.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from railctl.envelope import Envelope, Frame, Kind, hex_bytes
from railctl.errors import LinkProtocolError, LinkTimeout, PortNotXpressNet

if TYPE_CHECKING:
    from railctl.transport import Transport

DEFAULT_TIMEOUT = 5.0  # LI-USB normal-operation exchange budget
PROGRAMMING_TIMEOUT = 95.0  # service-mode budget: 1.5 min plus margin
HANDSHAKE_TIMEOUT = 2.0
SETTLE_TIME = 0.05  # send_no_reply only
MAX_RETRIES = 1
_READ_CHUNK = 256
_READ_SLICE = 0.2  # max blocking time per read, keeps Ctrl-C responsive
_EVENT_BUFFER = 256
_ERROR_SAMPLE = 32
# One MAX_BUFFER's worth. A port that never goes quiet is the YD.Control
# telemetry interface, which streams ASCII for ever; without this bound the
# non-blocking drain at the top of every request() never returns and railctl
# hangs with no timeout and no error instead of reporting the wrong interface.
_MAX_DRAIN_BYTES = 4096

# xbus sits ABOVE this layer, so cmd_station_version() is unreachable from here.
# tests/unit/test_link.py imports both and pins them together.
_HANDSHAKE_TELEGRAM = b"\x21\x21\x00"
_VERSION_HEADER = 0x63
_VERSION_MARKER = 0x21

# Both mean the telegram never arrived intact, so resending is safe: every
# command this tool sends is idempotent. 61 82 (not supported) is a real answer
# and is never retried - that is how a capability probe learns an opcode is
# unavailable.
_RETRY_PREFIXES = (b"\x61\x80", b"\x01\x0a")

_log = logging.getLogger("railctl.link")


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class LinkStats:
    requests: int
    retries: int
    timeouts: int
    frames_ok: int
    bytes_dropped: int
    bad_xor: int
    stray_replies: int


class Link:
    def __init__(
        self,
        transport: Transport,
        envelope: Envelope,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
        on_event: Callable[[Frame], None] | None = None,
        clock: Clock = time,
    ) -> None:
        self._transport = transport
        self._envelope = envelope
        self._default_timeout = default_timeout
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.RLock()
        self._events: deque[Frame] = deque(maxlen=_EVENT_BUFFER)
        # Late replies are kept, not just counted: see recent_late_replies().
        self._late: deque[Frame] = deque(maxlen=_EVENT_BUFFER)
        self._requests = 0
        self._retries = 0
        self._timeouts = 0
        self._version_telegram: bytes | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            self._transport.open()
            self._transport.flush_input()
            self._envelope.reset()
            seen = bytearray()
            try:
                self._requests += 1
                self._envelope.note_request(_HANDSHAKE_TELEGRAM)
                self._transport.write(self._envelope.wrap(_HANDSHAKE_TELEGRAM))
                deadline = self._clock.monotonic() + HANDSHAKE_TIMEOUT
                frame = self._pump(deadline, seen)
                if frame is None:
                    self._envelope.note_abandoned()
                    self._timeouts += 1
                    raise self._not_xpressnet(seen)
                self._envelope.note_reply(frame)
                if (
                    len(frame.payload) < 2
                    or frame.payload[0] != _VERSION_HEADER
                    or frame.payload[1] != _VERSION_MARKER
                ):
                    # A different failure from silence, and it gets a different
                    # message: the station answered, and the bytes it answered
                    # with are the evidence.
                    raise self._wrong_version_reply(frame)
                self._version_telegram = frame.payload
            except BaseException:
                self._transport.close()
                raise

    def _not_xpressnet(self, seen: bytes) -> PortNotXpressNet:
        """Nothing came back at all within the handshake budget."""
        sample = hex_bytes(bytes(seen[:_ERROR_SAMPLE])) or "(none)"
        return PortNotXpressNet(
            f"{self._transport.description} did not answer the XpressNet version "
            f"request {hex_bytes(_HANDSHAKE_TELEGRAM)} within {HANDSHAKE_TIMEOUT} s; "
            f"first bytes received: {sample}",
            hint=self._transport.diagnostic_hint,
        )

    def _wrong_version_reply(self, frame: Frame) -> PortNotXpressNet:
        """Something came back promptly, but it is not a version reply."""
        return PortNotXpressNet(
            f"{self._transport.description} answered {hex_bytes(frame.payload)}, "
            f"which is not an XpressNet version reply "
            f"(expected header {_VERSION_HEADER:02X} and first data byte "
            f"{_VERSION_MARKER:02X})",
            hint=self._transport.diagnostic_hint,
        )

    def close(self) -> None:
        with self._lock:
            self._transport.close()

    # -- exchanges ---------------------------------------------------------
    def request(self, telegram: bytes, *, timeout: float | None = None) -> bytes:
        budget = self._default_timeout if timeout is None else timeout
        with self._lock:
            attempts = 0
            while True:
                self.drain()
                self._requests += 1
                self._envelope.note_request(telegram)
                self._transport.write(self._envelope.wrap(telegram))
                frame = self._pump(self._clock.monotonic() + budget)
                if frame is None:
                    self._envelope.note_abandoned()
                    self._timeouts += 1
                    # The receive buffer is deliberately NOT flushed: flushing
                    # risks cutting a frame in half, and a late reply is caught
                    # and counted as a stray by the next drain(). The embedded
                    # stats are the diagnosis - bytes_dropped climbing with
                    # frames_ok at 0 is the wrong CDC interface, not a dead port.
                    raise LinkTimeout(
                        f"no reply to {hex_bytes(telegram)} within {budget} s; {self.stats()}",
                        # No connection-specific advice in this module: the
                        # transport supplies it, so the Z21 LAN transport lands
                        # without editing link.py (spec line 583).
                        hint=self._transport.diagnostic_hint,
                    )
                self._envelope.note_reply(frame)
                if frame.payload[:2] in _RETRY_PREFIXES:
                    if attempts >= MAX_RETRIES:
                        raise LinkProtocolError(
                            f"the station rejected {hex_bytes(telegram)} twice "
                            f"(last reply {hex_bytes(frame.payload)})"
                        )
                    attempts += 1
                    self._retries += 1
                    continue
                return frame.payload

    def send(self, telegram: bytes, *, timeout: float | None = None) -> None:
        # The request/response policy is read off the envelope, not hardcoded:
        # LI-USB acknowledges every command, the future Z21Envelope will not.
        if self._envelope.expects_ack:
            self.request(telegram, timeout=timeout)
        else:
            self.send_no_reply(telegram)

    def send_no_reply(self, telegram: bytes) -> None:
        with self._lock:
            self.drain()
            self._requests += 1
            self._envelope.note_request(telegram)
            self._transport.write(self._envelope.wrap(telegram))
            self._clock.sleep(SETTLE_TIME)
            # note_abandoned BEFORE the drain on purpose: if an ack does arrive
            # for a command declared reply-less, it lands in stray_replies, and
            # that counter is the evidence that expects_ack was wrong.
            self._envelope.note_abandoned()
            self.drain()

    def await_frame(self, match: Callable[[Frame], bool], *, timeout: float) -> Frame:
        with self._lock:
            deadline = self._clock.monotonic() + timeout
            while True:
                frame = self._envelope.pop()
                if frame is None:
                    remaining = deadline - self._clock.monotonic()
                    if remaining <= 0:
                        self._timeouts += 1
                        raise LinkTimeout(f"no matching frame within {timeout} s; {self.stats()}")
                    self._envelope.feed(
                        self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE))
                    )
                    continue
                if frame.kind is Kind.UNSOLICITED:
                    self._dispatch(frame)
                if match(frame):
                    return frame

    def poll(self, timeout: float = 0.0) -> list[Frame]:
        with self._lock:
            deadline = self._clock.monotonic() + timeout
            events: list[Frame] = []
            drained = 0
            while True:
                frame = self._envelope.pop()
                if frame is not None:
                    if frame.kind is Kind.UNSOLICITED:
                        self._dispatch(frame)
                        events.append(frame)
                    else:
                        # A solicited frame with nothing outstanding is a late
                        # reply. The envelope has already counted it, but the
                        # counter alone cannot tell "one stray reply happened"
                        # from "a 63 14 08 91 EE arrived after the budget", and
                        # that distinction is the whole R1 investigation in
                        # docs/probe-results.md. Keep the bytes.
                        self._late.append(frame)
                    continue
                # Non-blocking reads until the port is empty, so poll(0.0) is a
                # real drain even when the port hands over one byte at a time.
                chunk = self._transport.read(_READ_CHUNK, 0.0)
                if chunk:
                    self._envelope.feed(chunk)
                    drained += len(chunk)
                    if drained < _MAX_DRAIN_BYTES:
                        continue
                    # The port has not gone quiet in a whole buffer's worth of
                    # bytes, so it is not going to. Returning is what lets the
                    # wrong-interface diagnosis happen instead of a hang.
                    return events
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    return events
                self._envelope.feed(self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE)))

    def drain(self) -> None:
        with self._lock:
            self.poll(0.0)

    # -- reporting ---------------------------------------------------------
    def stats(self) -> LinkStats:
        counters = self._envelope.stats
        return LinkStats(
            requests=self._requests,
            retries=self._retries,
            timeouts=self._timeouts,
            frames_ok=counters.frames_ok,
            bytes_dropped=counters.bytes_dropped,
            bad_xor=counters.bad_xor,
            stray_replies=counters.stray_replies,
        )

    def recent_events(self) -> list[Frame]:
        return list(self._events)

    def recent_late_replies(self) -> list[Frame]:
        """Solicited frames that arrived with nothing outstanding, in order.

        `railctl doctor` reports these alongside stray_replies. "POM read
        unsupported" and "POM read is slower than the budget" look identical in
        the counter and different here.
        """
        return list(self._late)

    @property
    def description(self) -> str:
        return self._transport.description

    @property
    def identity(self) -> str:
        return self._transport.identity

    @property
    def version_telegram(self) -> bytes | None:
        return self._version_telegram

    # -- internals ---------------------------------------------------------
    def _pump(self, deadline: float, seen: bytearray | None = None) -> Frame | None:
        """Read and dispatch until a solicited frame arrives or the deadline passes.

        pop() is tried before the deadline check, so a frame already sitting in
        the envelope buffer is returned even on an expired budget: throwing away
        a frame we already hold is how a reply becomes silence.
        """
        while True:
            frame = self._envelope.pop()
            if frame is not None:
                if frame.kind is Kind.SOLICITED:
                    return frame
                self._dispatch(frame)
                continue
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return None
            chunk = self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE))
            if seen is not None:
                seen += chunk
            self._envelope.feed(chunk)

    def _dispatch(self, frame: Frame) -> None:
        self._events.append(frame)
        if self._on_event is None:
            return
        try:
            self._on_event(frame)
        except Exception:  # a bad callback must not lose a reply
            _log.warning("on_event callback raised for %r", frame, exc_info=True)
