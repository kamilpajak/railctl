"""The one object the CLI and a future TUI talk to: session, telemetry, power, and CV
operations built in later tasks. `station/` never sees framing bytes, port names, or CV
arithmetic - it speaks only in Station API terms, built through `xbus`.

Two facts from the LI documentation shape everything below:

* Exactly one solicited reply per command - the generic ack, an interface status frame, or the
  data. There is never an ack followed by data, so "first solicited frame" is always the right
  one.
* Broadcasts are buffered while a command is outstanding and delivered after the command reply.
  A passive wait (`events()`) only observes pushes when nothing else is in flight.

Every public method holds one `threading.RLock` for its whole body. RLock, not Lock: a callback
registered through `on_event` can call back into `status()` while `emergency_stop()` still holds
the lock (see `_warn_if_unverified_band`), and a plain Lock would make that deadlock instead of
reenter.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from railctl.envelope import Frame, hex_bytes
from railctl.errors import (
    ProtocolError,
    RailctlError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
)
from railctl.link import Link
from railctl.station.capabilities import LEARNABLE_FIELDS, UNKNOWN_IDENTITY, Capabilities
from railctl.station.timing import TIMING, Timing
from railctl.station.types import StationEvent
from railctl.transport import open_link
from railctl.xbus import replies
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
)
from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET
from railctl.xbus.replies import (
    POWER_OFF,
    POWER_ON,
    REASON_CHECKSUM,
    REASON_LENGTH,
    UNSUPPORTED,
    EmergencyStopBroadcast,
    InterfaceStatus,
    Other,
    Reply,
    ServiceModeEntry,
    StationStatus,
    StationVersion,
)

# 01 09 08 measured during D10 (docs/probe-results.md): the interface's answer when a request
# was malformed on the way OUT, never a fact about the decoder or the station's support for an
# opcode. A bare ValueError (not a RailctlError) is deliberate: cli/_errors.py maps ValueError to
# exit code 2 (usage), and this is always a railctl bug, never something the operator caused.
INTERFACE_STATUS_USAGE: Final[int] = 0x09
# Named once so an error message can say which dialect produced an unrecognised reply, ahead of
# the day a second dialect (Z21 LAN) exists to be confused with this one.
PROTOCOL_NAME: Final[str] = "xpressnet"

_log = logging.getLogger("railctl.station")


class Station:
    def __init__(
        self,
        link: Link,
        capabilities: Capabilities,
        *,
        default_address: int | None = None,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.link = link
        self.timing = timing
        self.default_address = default_address
        self._capabilities = capabilities
        self._capabilities_path = capabilities_path
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event
        self._lock = threading.RLock()
        self._version_cache: StationVersion | None = None
        self._dirty = False
        self._cache_clears: list[Callable[[], None]] = []

    @classmethod
    def open(
        cls,
        target: str = "auto",
        *,
        default_address: int | None = None,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
    ) -> Station:
        """Resolve `target`, open the link, then load capabilities by its identity.

        The identity is not knowable before the link opens - it comes from the transport, which
        `open_link` has already asked - so this order (link, then capabilities) is forced, not a
        style choice.
        """
        link = open_link(target)
        capabilities = (
            Capabilities.load(capabilities_path, link.identity)
            if capabilities_path is not None
            else Capabilities.unknown(link.identity)
        )
        return cls(
            link,
            capabilities,
            default_address=default_address,
            capabilities_path=capabilities_path,
            timing=timing,
        )

    # -- read-only surface ---------------------------------------------------
    @property
    def capabilities(self) -> Capabilities:
        with self._lock:
            return self._capabilities

    @property
    def threshold(self) -> int:
        """The long-address cutoff for `encode_loco_address`: measured once the doctor runs,
        `XPRESSNET.long_address_threshold` (100) until then - never 128, even though this
        station's family id is Z21's. Z21's threshold applies only once D10 confirms it."""
        with self._lock:
            measured = self._capabilities.loco_address_threshold
        return measured if measured is not None else XPRESSNET.long_address_threshold

    @property
    def description(self) -> str:
        return self.link.description

    @property
    def identity(self) -> str:
        return self.link.identity

    # -- collaborator surface (CvProgrammer and doctor use exactly these) -----
    def now(self) -> float:
        return self._clock()

    def pause(self, seconds: float) -> None:
        self._sleep(seconds)

    def emit(self, name: str, payload: dict[str, object]) -> None:
        """Call the on_event callback, if any. A raising callback must not lose the operation
        that triggered it - the same reasoning as Link._dispatch for wire events."""
        if self._on_event is None:
            return
        try:
            self._on_event(name, payload)
        except Exception:
            _log.warning("on_event callback raised for %s", name, exc_info=True)

    def learn(self, **updates: object) -> None:
        """Update a capability a normal operation can establish without risk.

        Restricted to LEARNABLE_FIELDS (spec line 844): everything else needs an explicit
        `railctl doctor` run, because establishing it means sending an opcode a normal operation
        never sends. `record()` below is the doctor-only escape hatch with no such restriction.
        """
        with self._lock:
            unknown = sorted(set(updates) - LEARNABLE_FIELDS)
            if unknown:
                raise ValueError(
                    f"not learnable outside `railctl doctor`: {unknown}; "
                    f"learnable fields are {sorted(LEARNABLE_FIELDS)}"
                )
            self._capabilities = self._capabilities.with_learned(**updates)
            self._dirty = True

    def record(self, **updates: object) -> None:
        """Doctor-only: update any capability field, learnable or not."""
        with self._lock:
            self._capabilities = self._capabilities.with_learned(**updates)
            self._dirty = True

    def exchange(self, telegram: bytes, *, timeout: float) -> Reply:
        """Send one telegram, parse its solicited reply, and raise for every reply that is not
        an answer at all - never for one that merely disagrees with what the caller hoped for.

        This is the ONE place in `station/` that calls `link.request` and `replies.parse`, so
        this mapping table is written once here rather than reinvented per caller:

        * `InterfaceStatus(0x09)` -> `ValueError` - a malformed request, a railctl bug.
        * any other `InterfaceStatus` -> `TransportError` - the interface had a problem; this is
          never a capability verdict.
        * `Unsupported` (61 82) -> `UnsupportedCommandError` - the one reply that IS a real "no".
        * `Other` with reason `checksum` or `length` -> `ProtocolError` (exit 4): the LINK
          damaged or truncated the reply. Collapsing this into the row below would make a bad
          cable and an incomplete reply table indistinguishable at the exit code.
        * `Other` with reason `empty` or `unknown_form` -> the base `RailctlError` (exit 9): the
          reply arrived intact, but this REPLY TABLE has no row for it yet - the station is not
          at fault.
        * everything else - GenericAck, StationVersion, StationStatus, PowerState,
          EmergencyStopBroadcast, ServiceModeEntry, every CV reply Tasks 4-6 add, and every
          `TRANSIENT_REPLIES` member (ShortCircuit, TrackShortCircuit, Busy, StationBusy,
          TransferError) - is returned untouched. None of `TRANSIENT_REPLIES`' five members says
          anything about whether an opcode is implemented (that module's own docstring), so this
          method must not turn any of them into an exception, `StationBusy` included, even though
          it is the one member that can follow ANY command: a later CV caller attaches the CV
          number `ProgrammingError` carries, or applies its own retry policy, which this method
          has no way to know.
        """
        with self._lock:
            reply = replies.parse(self.link.request(telegram, timeout=timeout))
            if isinstance(reply, InterfaceStatus):
                if reply.code == INTERFACE_STATUS_USAGE:
                    raise ValueError(
                        f"{hex_bytes(telegram)} was rejected as malformed (interface status "
                        f"{INTERFACE_STATUS_USAGE:02X}); this is a railctl bug, not a station "
                        f"limit"
                    )
                raise TransportError(
                    f"the interface reported status {reply.code:02X} answering "
                    f"{hex_bytes(telegram)}; this is the interface having a problem, not a "
                    f"station capability verdict"
                )
            if reply == UNSUPPORTED:
                raise UnsupportedCommandError(
                    f"the station answered 61 82 to {hex_bytes(telegram)}: not supported"
                )
            if isinstance(reply, Other):
                if reply.reason in (REASON_CHECKSUM, REASON_LENGTH):
                    raise ProtocolError(
                        f"damaged reply to {hex_bytes(telegram)} ({reply.reason}): "
                        f"{hex_bytes(reply.telegram)}; this is the LINK, not the station or the "
                        f"decoder - check the cable, the port, and link.stats()"
                    )
                raise RailctlError(
                    f"unrecognised {PROTOCOL_NAME} reply to {hex_bytes(telegram)} "
                    f"({reply.reason}): {hex_bytes(reply.telegram)}"
                )
            return reply

    def resolve_address(self, address: int | None) -> int | None:
        with self._lock:
            return self.default_address if address is None else address

    def register_cache(self, clear: Callable[[], None]) -> None:
        with self._lock:
            self._cache_clears.append(clear)

    def invalidate_caches(self) -> None:
        with self._lock:
            for clear in self._cache_clears:
                clear()

    # -- session ---------------------------------------------------------
    def close(self) -> None:
        """Flush learned capabilities, then close the link. Never touches track power - a
        session ending is not a reason to stop the layout."""
        with self._lock:
            self.invalidate_caches()
            if (
                self._capabilities_path is not None
                and self._dirty
                and self.link.identity != UNKNOWN_IDENTITY
            ):
                self._capabilities.save(self._capabilities_path)
                self._dirty = False
            self.link.close()

    def __enter__(self) -> Station:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- operations ---------------------------------------------------------
    def version(self) -> StationVersion:
        with self._lock:
            if self._version_cache is None:
                reply = self.exchange(cmd_station_version(), timeout=self.timing.li_ack_normal)
                if not isinstance(reply, StationVersion):
                    raise RailctlError(f"expected a station version reply, got {reply!r}")
                self._version_cache = reply
            return self._version_cache

    def status(self) -> StationStatus:
        with self._lock:
            reply = self.exchange(cmd_station_status(), timeout=self.timing.li_ack_normal)
            if not isinstance(reply, StationStatus):
                raise RailctlError(f"expected a station status reply, got {reply!r}")
            return reply

    def power_on(self) -> None:
        with self._lock:
            reply = self.exchange(cmd_track_power_on(), timeout=self.timing.li_ack_normal)
            self._settle_power(reply, expected=True)

    def power_off(self) -> None:
        with self._lock:
            reply = self.exchange(cmd_track_power_off(), timeout=self.timing.li_ack_normal)
            self.invalidate_caches()
            self._settle_power(reply, expected=False)

    def _settle_power(self, reply: Reply, *, expected: bool) -> None:
        """`61 01` means on and `61 00` means off, read directly off the command's own reply -
        no unconditional status round trip. A disagreeing reply gets exactly one status()
        re-read after `power_settle`; if it still disagrees, TrackPowerError. Never a loop."""
        wanted = POWER_ON if expected else POWER_OFF
        if reply == wanted:
            return
        self.pause(self.timing.power_settle)
        second = self.status()
        if second.track_power != expected:
            state = "on" if expected else "off"
            seen = "on" if second.track_power else "off"
            raise TrackPowerError(
                f"commanded track power {state} but the station still reports {seen} "
                f"after {self.timing.power_settle}s"
            )

    def emergency_stop(self, address: int | None = None) -> None:
        """`None` sends 80 80 (all locomotives); an address sends 92 AH AL X. Track power stays
        on either way, so the rest of the layout keeps running."""
        with self._lock:
            if address is None:
                self.exchange(cmd_emergency_stop_all(), timeout=self.timing.li_ack_normal)
                return
            telegram = cmd_emergency_stop_loco(address, threshold=self.threshold)
            try:
                self.exchange(telegram, timeout=self.timing.li_ack_normal)
            finally:
                self._warn_if_unverified_band(address)

    def _warn_if_unverified_band(self, address: int) -> None:
        """Addresses 100..127 are where XpressNet and Z21 disagree about the wire form. Until
        the doctor's D10 measures which one this station uses, `threshold` defaults to 100 and
        this emits a warning event rather than silently guessing right or wrong."""
        if self._capabilities.loco_address_threshold is None and address in DIVERGENCE_BAND:
            self.emit("address.band_unverified", {"address": address, "threshold": self.threshold})

    def events(self, *, interval: float = 0.25) -> Iterator[StationEvent]:
        """Decode broadcasts from `link.poll(interval)` forever. The lock is held only around
        each `poll()` call, never across a `yield` - a caller that calls `status()` between two
        `next()` calls must not deadlock. `KeyboardInterrupt` is not caught here on purpose: the
        future `monitor` CLI command needs it to propagate.
        """
        while True:
            with self._lock:
                frames = self.link.poll(interval)
            for frame in frames:
                yield self._station_event(frame)

    def _station_event(self, frame: Frame) -> StationEvent:
        """`at` is required on `StationEvent` (no default), so every branch below stamps it with
        `self.now()` - the same clock `pause()` reads, never `time.monotonic()` directly, so a
        `FakeClock` in tests governs this timestamp too."""
        reply = replies.parse(frame.payload)
        telegram_hex = hex_bytes(frame.payload)
        if reply == POWER_ON:
            return StationEvent(
                at=self.now(),
                name="power.on",
                detail="track power turned on",
                payload={"telegram": telegram_hex},
            )
        if reply == POWER_OFF:
            return StationEvent(
                at=self.now(),
                name="power.off",
                detail="track power turned off",
                payload={"telegram": telegram_hex},
            )
        if isinstance(reply, EmergencyStopBroadcast):
            return StationEvent(
                at=self.now(),
                name="loco.emergency_stop",
                detail="emergency stop broadcast",
                payload={"telegram": telegram_hex},
            )
        if isinstance(reply, ServiceModeEntry):
            return StationEvent(
                at=self.now(),
                name="service.entered",
                detail="another device entered service mode",
                payload={"telegram": telegram_hex},
            )
        return StationEvent(
            at=self.now(),
            name="reply.unknown",
            detail=f"undecoded broadcast: {telegram_hex}",
            payload={"telegram": telegram_hex},
        )
