"""X-Bus command encoders.

Every function returns a complete telegram - header, data bytes, XOR - with no
framing prefix. The FF FE prefix belongs to the envelope and is never part of
the XOR.

CV numbers are 1-based on the way in, for every function here, and this module
does NO CV arithmetic at all: every wire field comes back from `xbus.cv`, the
single place where a 1-based CV becomes a wire value. The layering test greps
this file for CV arithmetic: subtraction or addition of one against a CV,
eight-bit shifts, and modulo 256. That sentence deliberately names the patterns
instead of spelling them, because a docstring that spells them is itself a match
and would turn the guard red on correct code.

The wire conventions are not uniform, and that is the most dangerous detail in
the module:

- POM (E6 30) and the Z21 opcodes (23 11 / 24 12) are ZERO-BASED: CV1 goes out
  as 0.
- The legacy direct opcodes (22 15 / 23 16) and the extended opcodes
  (22 18..1B / 23 1C..1F) are ONE-BASED: CV1 goes out as 1.

`xbus.cv` owns both rules. The encoders below only say which one they want.
Routing a service-mode opcode through the POM rule reads the wrong CV off the
decoder and reports it under the right name.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

from railctl.xbus.address import encode_loco_address
from railctl.xbus.codec import encode
from railctl.xbus.cv import (
    EXT_READ_OPCODES,
    EXT_WRITE_OPCODES,
    direct_cv_byte,
    ext_cv_fields,
    pom_cv_fields,
    z21_cv_fields,
)
from railctl.xbus.speed import DRIVE_IDENT_128, Direction, encode_speed_128

# X-Bus request headers. The low nibble is the data-byte count, which is why one
# opcode family appears under three headers: 22 15 (read, two data bytes),
# 23 16 (write, three), 24 12 (Z21 write with a value, four).
REQ_1_DATA = 0x21
REQ_2_DATA = 0x22
REQ_3_DATA = 0x23
REQ_4_DATA = 0x24

DB_VERSION = 0x21
DB_STATION_STATUS = 0x24
DB_POWER_ON = 0x81
DB_POWER_OFF = 0x80
DB_SERVICE_RESULT = 0x10
DB_DIRECT_READ = 0x15
DB_DIRECT_WRITE = 0x16
DB_Z21_READ = 0x11
DB_Z21_WRITE = 0x12

OP_EMERGENCY_STOP_ALL = 0x80
OP_EMERGENCY_STOP_LOCO = 0x92
OP_LOCO_INFO = 0xE3
DB_LOCO_INFO = 0x00
# E3 09 AH AL X asks for the F13..F28 ON/OFF state (Lenz 23151 section 3.1.9.2).
# Measured 2026-08-04: the YD7010 answers E3 52 D1 D2. This is the only way to
# learn F13..F28; the E4 loco-info reply stops at F12.
DB_FUNCTION_STATE_13_28 = 0x09
OP_LOCO_DRIVE = 0xE4
OP_POM = 0xE6
DB_POM = 0x30

POM_READ_BYTE_BASE = 0xE4
POM_WRITE_BYTE_BASE = 0xEC
POM_WRITE_BIT_BASE = 0xE8
POM_UNUSED_BYTE = 0x00
POM_BIT_VALUE_SHIFT = 3

MAX_BIT_INDEX = 7
MIN_BYTE_VALUE = 0
MAX_BYTE_VALUE = 255
MAX_FUNCTION = 28
DB_FUNCTION_SINGLE: Final[int] = 0xF8
FUNCTION_ACTION_SHIFT: Final[int] = 6


class FunctionGroup(enum.IntEnum):
    """The E4 sub-opcode that carries each block of functions.

    G4 and G5 need command station version 3.6 or later. The YD7010 reports 4.0
    and both were accepted on hardware (docs/probe-results.md, 2026-08-04), but
    that is a probed capability, not an assumption this module makes.
    """

    G1 = 0x20  # F0..F4
    G2 = 0x21  # F5..F8
    G3 = 0x22  # F9..F12
    G4 = 0x23  # F13..F20
    G5 = 0x28  # F21..F28


# Written out one entry at a time rather than generated, because the F0 row is
# the irregular one and a generator would hide it: F0 is bit 4 of the group 1
# byte, and F1 is bit 0. The byte is 000 F0 F4 F3 F2 F1.
FUNCTION_BITS: dict[int, tuple[FunctionGroup, int]] = {
    0: (FunctionGroup.G1, 4),
    1: (FunctionGroup.G1, 0),
    2: (FunctionGroup.G1, 1),
    3: (FunctionGroup.G1, 2),
    4: (FunctionGroup.G1, 3),
    5: (FunctionGroup.G2, 0),
    6: (FunctionGroup.G2, 1),
    7: (FunctionGroup.G2, 2),
    8: (FunctionGroup.G2, 3),
    9: (FunctionGroup.G3, 0),
    10: (FunctionGroup.G3, 1),
    11: (FunctionGroup.G3, 2),
    12: (FunctionGroup.G3, 3),
    13: (FunctionGroup.G4, 0),
    14: (FunctionGroup.G4, 1),
    15: (FunctionGroup.G4, 2),
    16: (FunctionGroup.G4, 3),
    17: (FunctionGroup.G4, 4),
    18: (FunctionGroup.G4, 5),
    19: (FunctionGroup.G4, 6),
    20: (FunctionGroup.G4, 7),
    21: (FunctionGroup.G5, 0),
    22: (FunctionGroup.G5, 1),
    23: (FunctionGroup.G5, 2),
    24: (FunctionGroup.G5, 3),
    25: (FunctionGroup.G5, 4),
    26: (FunctionGroup.G5, 5),
    27: (FunctionGroup.G5, 6),
    28: (FunctionGroup.G5, 7),
}

GROUP_FUNCTIONS: dict[FunctionGroup, tuple[int, ...]] = {
    group: tuple(f for f, (g, _) in FUNCTION_BITS.items() if g is group) for group in FunctionGroup
}


def pack_function_bits(group: FunctionGroup, state: Mapping[int, bool]) -> int:
    """Pack one group's byte. `state` must carry every function in the group.

    E4 20/21/22/23/28 sets EVERY function in its group in one telegram, so a
    caller that supplies only the function it wants to change switches the other
    four off. Defaulting a missing key to False would make that silent - the
    same shape as the failure this project keeps producing, an absence read as a
    negative fact - so a missing key raises instead and names what is missing.

    Functions belonging to other groups are ignored, so the station can hand its
    whole 29-entry shadow map to each group in turn.
    """
    unknown = sorted(f for f in state if f not in FUNCTION_BITS)
    if unknown:
        raise ValueError(f"function index out of range 0..{MAX_FUNCTION}: {unknown}")
    missing = [f for f in GROUP_FUNCTIONS[group] if f not in state]
    if missing:
        raise ValueError(f"{group.name} sets all its functions at once; state missing {missing}")
    bits = 0
    for function in GROUP_FUNCTIONS[group]:
        if state[function]:
            bits |= 1 << FUNCTION_BITS[function][1]
    return bits


def _check_byte(name: str, value: int) -> int:
    if not MIN_BYTE_VALUE <= value <= MAX_BYTE_VALUE:
        raise ValueError(f"{name} {value} out of range {MIN_BYTE_VALUE}..{MAX_BYTE_VALUE}")
    return value


def cmd_station_version() -> bytes:
    """21 21 00 - measured; the YD7010 answers 63 21 40 12 10 (XpressNet 4.0, id 0x12)."""
    return encode(REQ_1_DATA, DB_VERSION)


def cmd_station_status() -> bytes:
    """21 24 05 - measured; the YD7010 answered 62 22 07 47 on an unpowered track."""
    return encode(REQ_1_DATA, DB_STATION_STATUS)


def cmd_track_power_on() -> bytes:
    return encode(REQ_1_DATA, DB_POWER_ON)


def cmd_track_power_off() -> bytes:
    return encode(REQ_1_DATA, DB_POWER_OFF)


def cmd_emergency_stop_all() -> bytes:
    """80 80 - the only telegram here with no data byte at all."""
    return encode(OP_EMERGENCY_STOP_ALL)


def cmd_emergency_stop_loco(address: int, *, threshold: int) -> bytes:
    """92 AH AL X (XpressNet 2.2.5.2).

    The dedicated per-locomotive stop, NOT E4 13 with wire speed 1: it carries
    no direction bit, so a safety path never has to make a loco_info round trip
    first to learn which way the locomotive was facing.
    """
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_EMERGENCY_STOP_LOCO, high, low)


def cmd_drive_128(address: int, step: int, direction: Direction, *, threshold: int) -> bytes:
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_DRIVE, DRIVE_IDENT_128, high, low, encode_speed_128(step, direction))


def cmd_function_group(address: int, group: FunctionGroup, bits: int, *, threshold: int) -> bytes:
    """E4 20/21/22/23/28 AH AL BITS X - sets every function in the group at once."""
    _check_byte("function bits", bits)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_DRIVE, int(group), high, low, bits)


class FunctionAction(enum.IntEnum):
    """The TT bits of E4 F8's payload.

    TOGGLE exists on the wire and is never sent by station/facade.py: a
    toggle whose prior state is unknown would return a guess, not a fact,
    and that is the one thing this project's whole error model exists to
    keep out of a return value. See the design decision recorded there.
    """

    OFF = 0b00
    ON = 0b01
    TOGGLE = 0b10


def cmd_function_single(
    address: int, function: int, action: FunctionAction, *, threshold: int
) -> bytes:
    """E4 F8 AdrMSB AdrLSB TTNNNNNN X - one function, one telegram.

    Measured 2026-08-04 (docs/probe-results.md, D12): E4 F8 00 03 40 5F lit
    the headlight of loco 3. This is a Z21 extension, not classic XpressNet
    V2 - the station is probed for it (single_function_cmd) rather than
    assumed to have it just because it reports command station id 0x12.

    `FunctionAction(action)` validates the action: an int that is not 0, 1
    or 2 raises ValueError from the enum constructor itself
    ("3 is not a valid FunctionAction"), so this function does not spell
    the range out by hand a second time.
    """
    if not 0 <= function <= MAX_FUNCTION:
        raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
    action = FunctionAction(action)
    high, low = encode_loco_address(address, long_threshold=threshold)
    payload = _check_byte(
        "function single payload", (int(action) << FUNCTION_ACTION_SHIFT) | function
    )
    return encode(OP_LOCO_DRIVE, DB_FUNCTION_SINGLE, high, low, payload)


def cmd_loco_info(address: int, *, threshold: int) -> bytes:
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_INFO, DB_LOCO_INFO, high, low)


def cmd_function_state_13_28(address: int, *, threshold: int) -> bytes:
    """E3 09 AH AL X - ask for the ON/OFF state of F13..F28.

    Measured 2026-08-04 (docs/probe-results.md, "Settled"): the YD7010 answers
    E3 52 D1 D2, which Task 8 parses as FunctionState13To28.

    This encoder exists because the E4 loco-info reply carries F0..F12 and
    nothing above. Without it the station has no way to READ F13..F28, and the
    group path (E4 23 / E4 28) writes all eight bits of its group at once - so a
    station with no state to start from seeds zeros and blind-clears every
    function in the group. probe-results.md records that side effect as closed
    precisely because this request answers; dropping the encoder would reopen it.
    """
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_INFO, DB_FUNCTION_STATE_13_28, high, low)


def cmd_service_direct_read(cv: int) -> bytes:
    """22 15 C X - legacy direct read, ONE-based, CV1..255.

    `direct_cv_byte` refuses CV256 and above: from station version 3.6 a C of 0
    addresses CV1024, not CV256 (23151 sections 3.2.6 and 3.2.14), and the
    YD7010 reports 4.0, so a bare 0 here would touch the wrong CV with no error.
    Measured: this opcode answers only after a 21 10 poll.
    """
    return encode(REQ_2_DATA, DB_DIRECT_READ, direct_cv_byte(cv))


def cmd_service_direct_write(cv: int, value: int) -> bytes:
    """23 16 C V X - legacy direct write, ONE-based, CV1..255."""
    return encode(REQ_3_DATA, DB_DIRECT_WRITE, direct_cv_byte(cv), _check_byte("value", value))


def cmd_service_ext_read(cv: int) -> bytes:
    """22 18..1B C X - extended read, ONE-based within a 256-wide page.

    CV1024 is page 0 with C = 0, so 22 18 00 is CV1024 and NOT CV256. CV256 is
    22 19 00.
    """
    page, c = ext_cv_fields(cv)
    return encode(REQ_2_DATA, EXT_READ_OPCODES[page], c)


def cmd_service_ext_write(cv: int, value: int) -> bytes:
    """23 1C..1F C V X - extended write, same page scheme as the read."""
    page, c = ext_cv_fields(cv)
    return encode(REQ_3_DATA, EXT_WRITE_OPCODES[page], c, _check_byte("value", value))


def cmd_z21_cv_read(cv: int) -> bytes:
    """23 11 MSB LSB X - 16-bit, ZERO-based.

    Measured 2026-08-04: the only opcode family on this station that pushes its
    result without a 21 10 poll, and the one that reached CV265 and CV266.
    """
    msb, lsb = z21_cv_fields(cv)
    return encode(REQ_3_DATA, DB_Z21_READ, msb, lsb)


def cmd_z21_cv_write(cv: int, value: int) -> bytes:
    """24 12 MSB LSB V X - 16-bit, ZERO-based."""
    msb, lsb = z21_cv_fields(cv)
    return encode(REQ_4_DATA, DB_Z21_WRITE, msb, lsb, _check_byte("value", value))


def cmd_service_result_request() -> bytes:
    """21 10 31 - "Request for Service Mode results".

    XpressNet 2.2.8, verbatim: "The read instruction does not require an answer
    by the command station! A result must be specifically requested." M1
    recorded the whole Lenz opcode family as unimplemented because the probe
    never sent this telegram. It is the protocol, not a workaround.
    """
    return encode(REQ_1_DATA, DB_SERVICE_RESULT)


def cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes:
    """E6 30 AH AL (E4|MM) LSB 00 X - Operations Mode read, ZERO-based.

    Measured 2026-08-04: the YD7010 answers this with the interface ACK
    01 04 05 and nothing else - no 63 14, no 64 14, no 61 13, no 61 82, no
    broadcast, over an 8 s window and a 30 s raw capture. That is recorded as
    pom_read UNKNOWN, never False, and the judgement belongs to the station
    layer. This encoder claims nothing either way; it only guarantees the
    telegram is the documented one.
    """
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_POM, DB_POM, high, low, POM_READ_BYTE_BASE | mm, lsb, POM_UNUSED_BYTE)


def cmd_pom_write_byte(address: int, cv: int, value: int, *, threshold: int) -> bytes:
    """E6 30 AH AL (EC|MM) LSB V X - Operations Mode byte write, ZERO-based.

    Measured to work on this hardware even though the read does not: a write
    needs no return path from the decoder, while a read needs RailCom channel 2
    to come back. There is no confirmation channel, so the station verifies by
    reading the CV back in service mode, never by assuming success.
    """
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(
        OP_POM, DB_POM, high, low, POM_WRITE_BYTE_BASE | mm, lsb, _check_byte("value", value)
    )


def cmd_pom_write_bit(address: int, cv: int, bit: int, value: bool, *, threshold: int) -> bytes:
    """E6 30 AH AL (E8|MM) LSB (D<<3|BBB) X - Operations Mode bit write."""
    if not 0 <= bit <= MAX_BIT_INDEX:
        raise ValueError(f"bit {bit} out of range 0..{MAX_BIT_INDEX}")
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    payload = (int(value) << POM_BIT_VALUE_SHIFT) | bit
    return encode(OP_POM, DB_POM, high, low, POM_WRITE_BIT_BASE | mm, lsb, payload)


class TimeoutClass(enum.Enum):
    """Which Link budget a telegram needs: 5.0 s or 95.0 s."""

    NORMAL = "normal"
    PROGRAMMING = "programming"


# Service-mode exchanges can take a minute and the reply arrives as the command
# reply, so the wait is direct rather than a poll loop. Everything else - power,
# drive, function, POM in both directions - answers immediately.
PROGRAMMING_TELEGRAMS: frozenset[tuple[int, int]] = (
    frozenset(
        {
            (REQ_1_DATA, DB_SERVICE_RESULT),  # 21 10
            (REQ_2_DATA, DB_DIRECT_READ),  # 22 15
            (REQ_3_DATA, DB_DIRECT_WRITE),  # 23 16
            (REQ_3_DATA, DB_Z21_READ),  # 23 11
            (REQ_4_DATA, DB_Z21_WRITE),  # 24 12
        }
    )
    | frozenset((REQ_2_DATA, opcode) for opcode in EXT_READ_OPCODES)  # 22 18..1B
    | frozenset((REQ_3_DATA, opcode) for opcode in EXT_WRITE_OPCODES)  # 23 1C..1F
)


def timeout_class(telegram: bytes) -> TimeoutClass:
    """Classify a telegram so the station layer never inspects opcode bytes.

    Every telegram reaching this function was produced by an encoder above and is
    at least two bytes long, so anything shorter is a caller bug and is refused
    rather than given a plausible-looking NORMAL. Silently classifying an
    unclassifiable telegram is how a wrong budget gets chosen without anyone
    noticing.
    """
    if len(telegram) < 2:
        raise ValueError(f"telegram too short to classify: {telegram.hex(' ')!r}")
    if (telegram[0], telegram[1]) in PROGRAMMING_TELEGRAMS:
        return TimeoutClass.PROGRAMMING
    return TimeoutClass.NORMAL
