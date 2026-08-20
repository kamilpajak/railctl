# src/railctl/xbus/replies.py
"""Typed views over X-Bus reply telegrams (framing already stripped).

`parse` is TOTAL. It never raises, for any byte string, because this port is
shared with a telemetry stream and because a parser that raises turns a frame we
did not understand into an exception in a layer that was measuring something
else. An unrecognised telegram becomes `Other(telegram)`, which carries the
bytes: that is an UNKNOWN, distinct from silence and never a negative answer.

`parse` also never claims MORE than the header entitles it to. The dangerous
direction is not a missed reply - that shows up as `Other` and the station
treats it as unresolved - it is a reply invented from an unrelated telegram,
which the station would treat as a measurement.

Length handling: `codec.decode` has already enforced
`len(telegram) == (header & 0x0F) + 2`, so a header of 0x63 guarantees exactly
three data bytes and a header of 0xE4 exactly four. No branch below needs its
own length guard, and none has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from railctl.errors import ProtocolError, XBusChecksumError
from railctl.xbus import codec
from railctl.xbus.cv import join_cv_field
from railctl.xbus.dialect import DEFAULT_STATUS_BIT_ORDER, StatusBitOrder
from railctl.xbus.speed import SPEED_STEPS, Direction, decode_speed_128

HDR_INTERFACE = 0x01
HDR_PROGRAMMING = 0x61
HDR_STATUS = 0x62
HDR_RESULT_5 = 0x63
HDR_RESULT_6 = 0x64
HDR_BROADCAST = 0x81
HDR_FUNCTION_STATE = 0xE3
HDR_LOCO_INFO = 0xE4

DB_FUNCTION_STATE_13_28 = 0x52

DB_GENERIC_ACK = 0x04
DB_STATUS = 0x22
DB_PAGED_RESULT = 0x10
DB_VERSION = 0x21
DB_EMERGENCY_STOP = 0x00
DB_Z21_CV_RESULT = 0x14

# 63 14 / 15 / 16 / 17 (Lenz 23151 sections 3.1.2.6 to 3.1.2.9). Which CV each
# one names depends on the request that was issued, so the number is resolved by
# cv.resolve_service_cv and NOT here.
CV_RESULT_IDENTS = (0x14, 0x15, 0x16, 0x17)

# Identification byte of a loco info reply is 0000 BFFF: bit 3 is "another
# device is in control", bits 0-2 are the speed step mode, and the HIGH NIBBLE IS
# ZERO. That last part is load bearing. 0xE4 is also the request header for drive
# (E4 13) and for functions (E4 20..28, E4 F8), so a stray or echoed command
# reaches this parser under the same header as a reply. Only db0 0x00..0x0F can
# be a reply; anything else is a command and becomes Other.
SPEED_STEP_MODES: dict[int, int] = {0b000: 14, 0b001: 27, 0b010: 28, 0b100: 128}
IDENT_BUSY_MASK = 0x08
IDENT_SPEED_STEPS_MASK = 0x07
IDENT_RESERVED_MASK = 0xF0

# Lenz 23151 lists 0x00 LZ100, 0x01 LH200, 0x02 DPC and 0x03 Control Plus.
# 0x12 is the Z21 family and is the ONLY one measured here (probe-results.md,
# 2026-08-04). An id outside the table reports "unknown" rather than a guess.
STATION_FAMILIES: dict[int, str] = {
    0x00: "LZ100",
    0x01: "LH200",
    0x02: "DPC",
    0x03: "Control Plus",
    0x12: "Z21",
}
UNKNOWN_FAMILY = "unknown"

# Bits 0 and 1 are NOT here: the two documents disagree about them, so they are
# named data in `xbus/dialect.py` (StatusBitOrder) and injected into
# `StationStatus.from_raw`. The four below are not in dispute in either document
# and have never needed a second reading.
STATUS_AUTO_START = 0x04
STATUS_SERVICE_MODE = 0x08
STATUS_POWERING_UP = 0x40
STATUS_RAM_ERROR = 0x80

# (byte index within (FA, FB), mask), indexed by function number. F0 is bit 4 of
# FA and F1 is bit 0 of FA - the same irregular layout the E4 20 command byte
# uses, which is what tests/unit/test_xbus_replies.py cross-checks against
# commands.FUNCTION_BITS.
LOCO_INFO_FUNCTION_BITS: tuple[tuple[int, int], ...] = (
    (0, 0x10),  # F0
    (0, 0x01),  # F1
    (0, 0x02),  # F2
    (0, 0x04),  # F3
    (0, 0x08),  # F4
    (1, 0x01),  # F5
    (1, 0x02),  # F6
    (1, 0x04),  # F7
    (1, 0x08),  # F8
    (1, 0x10),  # F9
    (1, 0x20),  # F10
    (1, 0x40),  # F11
    (1, 0x80),  # F12
)
FUNCTIONS_IN_LOCO_INFO = len(LOCO_INFO_FUNCTION_BITS)


@dataclass(frozen=True, slots=True)
class GenericAck:
    """01 04 05 - the interface forwarded the command. NOT a value."""


@dataclass(frozen=True, slots=True)
class InterfaceStatus:
    """Any other 01 XX frame. The code is carried verbatim; mapping it to an
    exception is the transport layer's job, not this module's."""

    code: int


@dataclass(frozen=True, slots=True)
class StationVersion:
    raw: int
    station_id: int

    @property
    def version(self) -> str:
        return f"{self.raw >> 4}.{self.raw & 0x0F}"

    @property
    def family(self) -> str:
        return STATION_FAMILIES.get(self.station_id, UNKNOWN_FAMILY)


@dataclass(frozen=True, slots=True)
class StationStatus:
    """62 22 S, decoded under an INJECTED order for bits 0 and 1.

    Which of those two bits is emergency stop and which is emergency off is a
    per-station fact this module cannot know, because two documents disagree.
    Lenz XpressNet 2.1.7 makes bit 0 emergency off and bit 1 emergency stop
    (`dialect.LENZ_SPEC`, and what JMRI implements); the German 23151 manual
    swaps them (`dialect.LENZ_23151`). Both readings fit the states this bench
    spends most of its time in (`0x04` powered, `0x07` held and dead), so only a
    state that separates them decides it. That state is reached with `80 80`, and
    the Lenz spec is what makes it decisive: 2.2.4 says "The DCC track power
    remains switched on". Measured 2026-08-05 on the YD7010, against the
    front-panel Track Out LED:

        21 81 -> 62 22 04   powered                    green steady
        80 80 -> 62 22 05   bit 0, track still powered green FLASHING
        21 80 -> 62 22 06   bit 1, track dead          red

    So on THAT station bit 0 is emergency stop and bit 1 is emergency off - the
    23151 order, which is why it is `dialect.DEFAULT_STATUS_BIT_ORDER` and why
    injecting the order changed no reading. It is a default, not a claim about
    XpressNet: `railctl doctor` D13 measures the order the attached station uses,
    `Capabilities.status_bit_order` records it, and `station/facade.py` re-derives
    this object from `raw` with whatever was measured.

    Getting these two the wrong way round is not cosmetic: it makes `track_power`
    report a dead track as powered, which made `power_off()` raise on every
    successful call and would have let the doctor run D4 and D10 on an unpowered
    track. Neither document defines any bit as "short circuit".

    `raw` is always preserved, which is what lets the layer that HAS the
    capabilities re-decode without this parser ever being asked twice.
    """

    raw: int
    emergency_off: bool
    emergency_stop: bool
    auto_start_mode: bool
    service_mode: bool
    powering_up: bool
    ram_error: bool

    @classmethod
    def from_raw(cls, raw: int, order: StatusBitOrder = DEFAULT_STATUS_BIT_ORDER) -> StationStatus:
        """Decode `raw`. The default keeps every caller that has no station to ask
        - `parse` among them - reading exactly what it read before."""
        return cls(
            raw=raw,
            emergency_off=bool(raw & order.emergency_off_mask),
            emergency_stop=bool(raw & order.emergency_stop_mask),
            auto_start_mode=bool(raw & STATUS_AUTO_START),
            service_mode=bool(raw & STATUS_SERVICE_MODE),
            powering_up=bool(raw & STATUS_POWERING_UP),
            ram_error=bool(raw & STATUS_RAM_ERROR),
        )

    @property
    def track_power(self) -> bool:
        """A pure derivation from the decoded fields, carrying no station-specific
        knowledge of its own: with the order injected, "the track is live unless
        the station says emergency off" is true in both documents."""
        return not self.emergency_off


@dataclass(frozen=True, slots=True)
class CvValue:
    """A CV read result. `raw_cv` is the field exactly as received.

    The encoding is NOT inferred here. 63 14 carries both service-mode and POM
    results whose conventions differ, and the caller knows which request it
    issued; `cv.resolve_service_cv` and `cv.echo_candidates` turn `raw_cv` into a
    CV number. For the 64 14 form the two address bytes are combined by
    `cv.join_cv_field`, so no CV arithmetic escapes that module.
    """

    raw_cv: int
    value: int
    ident: int
    z21_form: bool


@dataclass(frozen=True, slots=True)
class PagedCvValue:
    """63 10 REG VAL - register or paged mode.

    23151 section 3.1.2.6: the station has determined the decoder does not
    support direct mode and has fallen back. This is a VALID answer, not an
    error. The number is a register, not a CV.
    """

    raw_register: int
    value: int


@dataclass(frozen=True, slots=True)
class Ready:
    """61 11 - service mode ready."""


@dataclass(frozen=True, slots=True)
class Busy:
    """61 1F - programming busy."""


@dataclass(frozen=True, slots=True)
class NoAck:
    """61 13 - the decoder did not acknowledge."""


@dataclass(frozen=True, slots=True)
class ShortCircuit:
    """61 12 - short circuit on the PROGRAMMING track."""


@dataclass(frozen=True, slots=True)
class TrackShortCircuit:
    """61 08 - short circuit on the MAIN track. Distinct from 61 12."""


@dataclass(frozen=True, slots=True)
class Unsupported:
    """61 82 - the station cannot process that instruction.

    This is the ONLY reply that entitles anything above to record a capability
    as False. Silence does not, and Other does not.
    """


@dataclass(frozen=True, slots=True)
class TransferError:
    """61 80 - the station saw a bad XOR from us. Resend once."""


@dataclass(frozen=True, slots=True)
class StationBusy:
    """61 81 - the station cannot act right now. Says nothing about support."""


@dataclass(frozen=True, slots=True)
class ServiceModeEntry:
    """61 02 - the station has entered service mode. Observed on the YD7010 on
    2026-08-04 as the first reply to a service-mode read."""


@dataclass(frozen=True, slots=True)
class PowerState:
    """61 00 / 61 01 - track power off / on."""

    on: bool


@dataclass(frozen=True, slots=True)
class EmergencyStopBroadcast:
    """81 00 81."""


@dataclass(frozen=True, slots=True)
class LocoInfo:
    """E4 IDENT SPD FA FB.

    `address` is always None from `parse`: the reply carries no address field at
    all. The station knows which locomotive it asked about and attaches it with
    `dataclasses.replace`; inventing it here would publish one locomotive's
    speed under another's number.

    `speed`, `direction` and `emergency_stopped` are None unless the ident byte
    says 128 speed steps, because `speed.py` defines only the 128-step wire
    layout. A 14/27/28-step reply keeps its `raw_speed` and reports the rest as
    UNKNOWN rather than decoding it with the wrong layout.

    `function_bits` has exactly FUNCTIONS_IN_LOCO_INFO entries, F0..F12. F13..F28
    are not carried by this reply and are absent rather than defaulted to False.
    """

    raw_ident: int
    raw_speed: int
    speed_steps: int | None
    in_use_by_other: bool
    function_bits: tuple[bool, ...]
    speed: int | None = None
    direction: Direction | None = None
    emergency_stopped: bool | None = None
    address: int | None = None


@dataclass(frozen=True, slots=True)
class FunctionState13To28:
    """E3 52 D1 D2 - the ON/OFF state of F13..F28 (Lenz 23151 section 3.1.9.2).

    Answers the E3 09 request. Measured 2026-08-04 (docs/probe-results.md,
    "Settled": "F13-F28 state readable | yes, E3 09 -> E3 52 D1 D2 | closes the
    blind-clear side effect").

    This is the ONLY reply form that carries F13..F28. LocoInfo stops at F12, and
    E4 23 / E4 28 write all eight bits of their group at once, so a station with
    nothing to read from would have to seed zeros and switch off every function
    in the group it never saw.
    """

    f13_f20: int
    f21_f28: int


# The four causes `Other.reason` keeps apart. A plain `str` field lets a caller
# compare against a mistyped literal and never match, which silently treats a
# damaged cable the same as an unrecognised opcode - the distinction Other
# exists to preserve is then lost again at the comparison site.
Reason = Literal["checksum", "length", "empty", "unknown_form"]

REASON_CHECKSUM: Reason = "checksum"
REASON_LENGTH: Reason = "length"
REASON_EMPTY: Reason = "empty"
REASON_UNKNOWN_FORM: Reason = "unknown_form"

# Spec line 704: E5 and E2 are extended loco-info reply forms this module
# does not decode - there is no dataclass for either, so they fall through
# to Other(telegram, reason=REASON_UNKNOWN_FORM) like any other unlisted
# header. Naming them here lets Station.exchange (station/facade.py) treat
# that specific Other as "a feature we have not probed for" rather than
# "a reply form nobody has ever seen" - the two have different remedies,
# and collapsing them back into one throws that distinction away.
EXTENDED_LOCO_INFO_HEADERS: Final[frozenset[int]] = frozenset({0xE5, 0xE2})


@dataclass(frozen=True, slots=True)
class Other:
    """Anything this module does not turn into a typed reply, bytes preserved.

    `reason` keeps four different causes apart, because their remedies are
    opposite:

    - "checksum" - the XOR did not hold. The LINK is damaging bytes; check the
      cable, the port and Link.stats().bad_xor.
    - "length"   - the frame did not match the length its header declares. The
      read window closed early, or this is not a telegram at all.
    - "empty"    - decoded cleanly but carries no data byte to dispatch on. No
      reply form in the index table has a zero-length body.
    - "unknown_form" - well formed, correct length, good XOR, and in a form
      nobody has listed. The REPLY TABLE is incomplete; this is the one that
      wants a new row.

    Collapsing these into one value leaves the station unable to tell a bad cable
    from a reply form we do not know, and both then read as an unresolved
    capability. The default keeps `Other(telegram)` constructible positionally,
    which is the shape the design document uses (spec line 538).
    """

    telegram: bytes
    reason: Reason = REASON_UNKNOWN_FORM


Reply = (
    GenericAck
    | InterfaceStatus
    | StationVersion
    | StationStatus
    | CvValue
    | PagedCvValue
    | Ready
    | Busy
    | NoAck
    | ShortCircuit
    | TrackShortCircuit
    | Unsupported
    | TransferError
    | StationBusy
    | ServiceModeEntry
    | PowerState
    | EmergencyStopBroadcast
    | LocoInfo
    | FunctionState13To28
    | Other
)

GENERIC_ACK = GenericAck()
READY = Ready()
BUSY = Busy()
NO_ACK = NoAck()
SHORT_CIRCUIT = ShortCircuit()
TRACK_SHORT_CIRCUIT = TrackShortCircuit()
UNSUPPORTED = Unsupported()
TRANSFER_ERROR = TransferError()
STATION_BUSY = StationBusy()
SERVICE_MODE_ENTRY = ServiceModeEntry()
POWER_ON = PowerState(on=True)
POWER_OFF = PowerState(on=False)
EMERGENCY_STOP_BROADCAST = EmergencyStopBroadcast()

# Not an answer either way. None of these say anything about whether an opcode
# is implemented, so every capability verdict must treat them as unresolved.
#
# Naming the set once is the point. Unsupported is the ONLY reply that entitles
# anything above to record a capability as False; if each consumer re-derives
# "which ones mean nothing" by hand, the one that forgets StationBusy records a
# busy station as an absent capability - the M1 failure again.
TRANSIENT_REPLIES: frozenset[Reply] = frozenset(
    {SHORT_CIRCUIT, TRACK_SHORT_CIRCUIT, BUSY, STATION_BUSY, TRANSFER_ERROR}
)

# Every reply that shares header 0x61. The two at 0x80 and 0x81 are easy to
# forget because they are not programming replies, but leaving one unparsed is
# how a station saying "I could not process that" gets recorded as one saying
# "I support that".
HEADER_61_REPLIES: dict[int, Reply] = {
    0x00: POWER_OFF,
    0x01: POWER_ON,
    0x02: SERVICE_MODE_ENTRY,
    0x08: TRACK_SHORT_CIRCUIT,
    0x11: READY,
    0x12: SHORT_CIRCUIT,
    0x13: NO_ACK,
    0x1F: BUSY,
    0x80: TRANSFER_ERROR,
    0x81: STATION_BUSY,
    0x82: UNSUPPORTED,
}


def _loco_info(data: bytes) -> LocoInfo:
    ident, raw_speed, fa, fb = data[0], data[1], data[2], data[3]
    speed_steps = SPEED_STEP_MODES.get(ident & IDENT_SPEED_STEPS_MASK)
    function_bytes = (fa, fb)
    function_bits = tuple(
        bool(function_bytes[index] & mask) for index, mask in LOCO_INFO_FUNCTION_BITS
    )
    if speed_steps != SPEED_STEPS:
        return LocoInfo(
            raw_ident=ident,
            raw_speed=raw_speed,
            speed_steps=speed_steps,
            in_use_by_other=bool(ident & IDENT_BUSY_MASK),
            function_bits=function_bits,
        )
    step, direction, emergency = decode_speed_128(raw_speed)
    return LocoInfo(
        raw_ident=ident,
        raw_speed=raw_speed,
        speed_steps=speed_steps,
        in_use_by_other=bool(ident & IDENT_BUSY_MASK),
        function_bits=function_bits,
        speed=step,
        direction=direction,
        emergency_stopped=emergency,
    )


def parse(telegram: bytes) -> Reply:
    """Turn one bare telegram into a typed reply. Never raises.

    The four failure causes are kept apart in Other.reason - see Other. The
    XBusChecksumError branch must come first: it is a subclass of
    XBusDecodeError, so catching ProtocolError first would swallow it and every
    corrupt link would look like a truncated frame.
    """
    try:
        header, data = codec.decode(telegram)
    except XBusChecksumError:
        return Other(telegram=telegram, reason=REASON_CHECKSUM)
    except ProtocolError:
        return Other(telegram=telegram, reason=REASON_LENGTH)
    if not data:
        return Other(telegram=telegram, reason=REASON_EMPTY)
    db0 = data[0]

    if header == HDR_INTERFACE:
        return GENERIC_ACK if db0 == DB_GENERIC_ACK else InterfaceStatus(code=db0)
    if header == HDR_PROGRAMMING and db0 in HEADER_61_REPLIES:
        return HEADER_61_REPLIES[db0]
    if header == HDR_STATUS and db0 == DB_STATUS:
        return StationStatus.from_raw(data[1])
    if header == HDR_RESULT_5 and db0 == DB_PAGED_RESULT:
        return PagedCvValue(raw_register=data[1], value=data[2])
    if header == HDR_RESULT_5 and db0 in CV_RESULT_IDENTS:
        return CvValue(raw_cv=data[1], value=data[2], ident=db0, z21_form=False)
    if header == HDR_RESULT_5 and db0 == DB_VERSION:
        return StationVersion(raw=data[1], station_id=data[2])
    if header == HDR_RESULT_6 and db0 == DB_Z21_CV_RESULT:
        return CvValue(
            raw_cv=join_cv_field(data[1], data[2]),
            value=data[3],
            ident=db0,
            z21_form=True,
        )
    if header == HDR_BROADCAST and db0 == DB_EMERGENCY_STOP:
        return EMERGENCY_STOP_BROADCAST
    if header == HDR_FUNCTION_STATE and db0 == DB_FUNCTION_STATE_13_28:
        return FunctionState13To28(f13_f20=data[1], f21_f28=data[2])
    # The identification-byte guard, not just the header. See IDENT_RESERVED_MASK:
    # E4 F8 and E4 13 are COMMANDS under the same header, and without this test
    # E4 F8 00 03 40 would come back as LocoInfo(raw_ident=0xF8) claiming 14
    # speed steps and in_use_by_other for a locomotive nobody asked about.
    if header == HDR_LOCO_INFO and not data[0] & IDENT_RESERVED_MASK:
        return _loco_info(data)
    return Other(telegram=telegram, reason=REASON_UNKNOWN_FORM)
