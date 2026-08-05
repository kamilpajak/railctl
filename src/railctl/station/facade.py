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

import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final

from railctl.envelope import Frame, hex_bytes
from railctl.errors import (
    LinkTimeout,
    ProtocolError,
    RailctlError,
    StationError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
    UnsupportedFeatureError,
)
from railctl.link import Link
from railctl.station.capabilities import LEARNABLE_FIELDS, UNKNOWN_IDENTITY, Capabilities
from railctl.station.programming import CvProgrammer
from railctl.station.timing import TIMING, Timing
from railctl.station.types import (
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    DoctorReport,
    ProgMode,
    StationEvent,
)
from railctl.transport import open_link
from railctl.xbus import replies
from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN
from railctl.xbus.commands import (
    FUNCTION_BITS,
    GROUP_FUNCTIONS,
    MAX_FUNCTION,
    FunctionAction,
    FunctionGroup,
    cmd_drive_128,
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_function_group,
    cmd_function_single,
    cmd_function_state_13_28,
    cmd_loco_info,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
    pack_function_bits,
)
from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET
from railctl.xbus.replies import (
    EXTENDED_LOCO_INFO_HEADERS,
    POWER_OFF,
    POWER_ON,
    REASON_CHECKSUM,
    REASON_LENGTH,
    UNSUPPORTED,
    EmergencyStopBroadcast,
    FunctionState13To28,
    GenericAck,
    InterfaceStatus,
    LocoInfo,
    Other,
    Reply,
    ServiceModeEntry,
    StationStatus,
    StationVersion,
)
from railctl.xbus.speed import MAX_SPEED_STEP, Direction

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
        self._function_shadow: dict[int, dict[int, bool]] = {}
        self.register_cache(self._function_shadow.clear)
        self.programmer = CvProgrammer(self)
        self.register_cache(self.programmer.invalidate_pages)

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
            if isinstance(reply, Other) and reply.telegram[0] in EXTENDED_LOCO_INFO_HEADERS:
                raise UnsupportedFeatureError(
                    f"{hex_bytes(telegram)} answered with an extended loco-info form "
                    f"({reply.telegram[0]:02X}) this station has not been probed for"
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

    # -- CV programming -------------------------------------------------
    def cv_read(
        self,
        cv: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
    ) -> CvResult:
        with self._lock:
            return self.programmer.cv_read(cv, address=address, mode=mode, page=page)

    def cv_write(
        self,
        cv: int,
        value: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
        verify: bool = True,
    ) -> CvResult:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            return self.programmer.cv_write(
                cv, value, address=resolved_address, mode=mode, page=page, verify=verify
            )

    def cv_read_many(
        self,
        specs: Sequence[CvSpec],
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        on_progress: Callable[[tuple[int, int, CvReadOutcome]], None] | None = None,
    ) -> list[CvReadOutcome]:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            return self.programmer.cv_read_many(
                specs, address=resolved_address, mode=mode, on_progress=on_progress
            )

    def select_page(
        self,
        page: CvPage,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        force: bool = False,
    ) -> None:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            self.programmer.select_page(page, address=resolved_address, mode=mode, force=force)

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

    def probe(
        self,
        *,
        address: int | None = None,
        allow_power_on: bool = False,
        use_programming_track: bool = True,
    ) -> DoctorReport:
        # Imported here, not at module level: doctor.py imports Station only
        # under TYPE_CHECKING, but facade.py importing doctor.py at module
        # level would need doctor.py to import facade.py for a real (not
        # type-only) Station reference somewhere else first - keeping this
        # one import lazy avoids finding out the hard way which of the two
        # modules a future refactor makes load first.
        from railctl.station.doctor import run_probe

        return run_probe(
            self,
            address=address,
            allow_power_on=allow_power_on,
            use_programming_track=use_programming_track,
        )

    # -- drive, loco_info and functions --------------------------------------
    def _validate_address(self, address: int) -> None:
        """Validate `address` and, once per call, warn about the one band
        where XpressNet and Z21 disagree.

        This is NOT `resolve_address` - Task 2 already owns that name, and
        it does something unrelated (substitute `default_address` for a
        `None`). Every address this task's methods take is required, so
        there is never a `None` to resolve here, only a value to check.

        DIVERGENCE_BAND is a FIXED range - the two dialects always disagree
        there - but the warning only fires while the threshold is still
        unmeasured (`capabilities.loco_address_threshold is None`). Once a
        doctor run has established it, the ambiguity this event exists to
        flag is resolved, and repeating the warning would just be noise.
        """
        if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
            raise ValueError(
                f"loco address {address} out of range {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}"
            )
        if self.capabilities.loco_address_threshold is None and address in DIVERGENCE_BAND:
            self.emit("address.band_unverified", {"address": address, "threshold": self.threshold})

    def drive(self, address: int, speed: int, direction: Direction) -> None:
        """speed 0..126 (0 is a braked stop); the drive telegram gets no
        answer of its own, so the expected reply is the generic ack."""
        with self._lock:
            if not 0 <= speed <= MAX_SPEED_STEP:
                raise ValueError(f"speed {speed} out of range 0..{MAX_SPEED_STEP}")
            self._validate_address(address)
            telegram = cmd_drive_128(address, speed, direction, threshold=self.threshold)
            reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
            self._expect_ack(reply)

    def loco_info(self, address: int) -> LocoInfo:
        """Never raises for `in_use_by_other` - another device holding this
        locomotive blocks nothing here, it only gets reported."""
        with self._lock:
            self._validate_address(address)
            telegram = cmd_loco_info(address, threshold=self.threshold)
            reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
            if not isinstance(reply, LocoInfo):
                raise StationError(f"unexpected reply to loco info: {reply!r}")
            info = dataclasses.replace(reply, address=address)
            if info.in_use_by_other:
                self.emit("loco.in_use_by_other", {"address": address})
            return info

    def _expect_ack(self, reply: object) -> None:
        """The `Unsupported` (61 82) case is NOT handled here on purpose:
        `exchange` already turned it into `UnsupportedCommandError` before
        returning (Task 2's reply mapping), so by the time a reply reaches
        this method it can never BE `Unsupported` - an `isinstance` branch
        for it here is dead code that would show up as an uncovered branch
        at the coverage gate. `test_drive_treats_a_refusal_as_unsupported_command`
        still pins the refusal end to end; it just does so through
        `exchange`, one layer below this method, which is where the refusal
        is actually detected.
        """
        if isinstance(reply, GenericAck):
            return
        raise StationError(f"expected the generic ack, got {reply!r}")

    def function_state(self, address: int, *, refresh: bool = False) -> dict[int, bool]:
        """F0..F12 from loco_info(); F13..F28 from E3 09, best-effort.

        A refused (61 82, which `exchange` has already turned into
        `UnsupportedCommandError` by the time it reaches here) or silent
        (`LinkTimeout`) E3 09 both leave keys 13..28 ABSENT from the result -
        never False. Absence read as a negative fact is the exact failure
        mode this project exists to stop, and it would happen here first:
        the group write path below trusts this dict completely, so a
        wrongly-defaulted False would blind-clear a function nobody ever
        measured. The two exceptions are read the same way here - "we could
        not read F13..F28" - even though one is a real answer and the other
        is silence; what they share is that neither entitles this method to
        invent a value.
        """
        with self._lock:
            if not refresh and address in self._function_shadow:
                return dict(self._function_shadow[address])
            info = self.loco_info(address)
            state: dict[int, bool] = dict(enumerate(info.function_bits))
            telegram = cmd_function_state_13_28(address, threshold=self.threshold)
            try:
                reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
            except (LinkTimeout, UnsupportedCommandError):
                reply = None
            if isinstance(reply, FunctionState13To28):
                for function in GROUP_FUNCTIONS[FunctionGroup.G4]:
                    state[function] = bool(reply.f13_f20 & (1 << FUNCTION_BITS[function][1]))
                for function in GROUP_FUNCTIONS[FunctionGroup.G5]:
                    state[function] = bool(reply.f21_f28 & (1 << FUNCTION_BITS[function][1]))
            self._function_shadow[address] = dict(state)
            return dict(state)

    def forget_loco(self, address: int) -> None:
        """Drop one address's function shadow - the next read starts fresh."""
        with self._lock:
            self._function_shadow.pop(address, None)

    def _require_function_capability(self, function: int, *, single_function_path: bool) -> None:
        group = FUNCTION_BITS[function][0]
        if group not in (FunctionGroup.G4, FunctionGroup.G5):
            return
        if single_function_path:
            # single_function_cmd is already True to have reached this path,
            # and E4 F8 needs nothing more for F13..F28 - unlike the group
            # path, it never touches the other seven functions in the group.
            return
        if self.capabilities.function_groups_4_5 is not True:
            raise UnsupportedFeatureError(
                f"F{function} (group {group.name}) needs function_groups_4_5, "
                "which this station has not confirmed"
            )

    def _function_set_group_path(
        self,
        address: int,
        function: int,
        on: bool,
        *,
        force_group: bool,
        state: dict[int, bool] | None = None,
    ) -> None:
        """`state`, when given, is an already-fresh read (`refresh=True`)
        the caller has in hand - `function_toggle` reads it to compute
        `on` in the first place. Accepting it here instead of reading
        again avoids repeating the loco_info + E3 09 pair for the same
        exchange; a caller with no such read (`function_set`) leaves it
        `None` and this method does its own `refresh=True` read as before.
        The dict is copied so mutating it here never reaches back into a
        caller's own variable.
        """
        group = FUNCTION_BITS[function][0]
        state = dict(state) if state is not None else self.function_state(address, refresh=True)
        # `function` gets its real requested value here, before `missing` is
        # computed, so the function being written is never counted among the
        # ones this call has to seed - it is known, by the caller's own hand.
        state[function] = on
        missing = [f for f in GROUP_FUNCTIONS[group] if f not in state]
        if missing and not force_group:
            raise StationError(
                f"F{function} shares group {group.name} with F{missing}, whose state "
                "has not been read; a blind write would clobber them",
                hint="--force-group",
            )
        if missing:
            for f in missing:
                state[f] = False
            self.emit(
                "function.group_seeded",
                {"address": address, "group": group.name, "functions": tuple(missing)},
            )
        bits = pack_function_bits(group, state)
        telegram = cmd_function_group(address, group, bits, threshold=self.threshold)
        # Drop whatever the `function_state(refresh=True)` call above just
        # wrote for this address BEFORE the exchange is attempted, not
        # after it succeeds: a write that raises - LinkTimeout included,
        # CLAUDE.md's "silence is unknown" - must not leave that pre-write
        # picture behind for the next function_state() to serve as a
        # settled fact with no wire traffic. The freshly-validated `state`
        # (with `function`'s new value already folded in) is only stored
        # once the exchange and its ack both succeed.
        self._function_shadow.pop(address, None)
        reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
        self._expect_ack(reply)
        self._function_shadow[address] = state

    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None:
        with self._lock:
            if not 0 <= function <= MAX_FUNCTION:
                raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
            single = self.capabilities.single_function_cmd is True
            self._require_function_capability(function, single_function_path=single)
            if single:
                # No shadow, no read-modify-write: this touches exactly one
                # function, so a stale shadow can never switch off a function
                # another throttle turned on. What it CAN do is leave a
                # SHADOWED function stale - if `address` already had an
                # entry from an earlier group read, that entry now
                # disagrees with the station about `function`. The drop
                # happens BEFORE the exchange, not after a successful one:
                # a write that raises - LinkTimeout included, CLAUDE.md's
                # "silence is unknown" - must leave no entry behind either,
                # or the next function_state() would serve the pre-write
                # value as a settled fact with no wire traffic at all.
                # Dropping unconditionally produces "unknown, re-read" on
                # the next function_state() call, never a value this
                # method only half-knows to be true.
                self._function_shadow.pop(address, None)
                action = FunctionAction.ON if on else FunctionAction.OFF
                telegram = cmd_function_single(address, function, action, threshold=self.threshold)
                reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
                self._expect_ack(reply)
                return
            self._function_set_group_path(address, function, on, force_group=force_group)

    def function_toggle(self, address: int, function: int, *, force_group: bool = False) -> bool:
        """Read the current value, send an explicit ON or OFF - never the
        TOGGLE wire action - and return the new value as a fact, not a
        guess. Raises StationError, sending nothing, when the state cannot
        be read at all."""
        with self._lock:
            if not 0 <= function <= MAX_FUNCTION:
                raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
            single = self.capabilities.single_function_cmd is True
            self._require_function_capability(function, single_function_path=single)
            state = self.function_state(address, refresh=True)
            if function not in state:
                raise StationError(
                    f"F{function} state is unknown; a toggle cannot guess it",
                    hint="--force-group",
                )
            new_value = not state[function]
            if single:
                # Same reasoning as function_set's single-function branch:
                # drop BEFORE the exchange, not after a successful one, so
                # a write that raises (LinkTimeout included) leaves no
                # stale entry for the shadow to answer from either.
                self._function_shadow.pop(address, None)
                action = FunctionAction.ON if new_value else FunctionAction.OFF
                telegram = cmd_function_single(address, function, action, threshold=self.threshold)
                reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
                self._expect_ack(reply)
                return new_value
            # `state` above is already a fresh (refresh=True) read - pass
            # it through instead of letting the group path repeat the same
            # loco_info + E3 09 pair for the exchange this call already
            # made.
            self._function_set_group_path(
                address, function, new_value, force_group=force_group, state=state
            )
            return new_value
