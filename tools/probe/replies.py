"""Typed views over XpressNet reply telegrams (prefix and XOR already stripped)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    xpressnet_major: int
    xpressnet_minor: int
    command_station_id: int


@dataclass(frozen=True)
class Status:
    raw: int

    # Bit meanings are the XpressNet ones (Lenz section 2.1.7), NOT the Z21 ones.
    # In particular bit 2 is start mode, and XpressNet defines no short-circuit bit.
    @property
    def emergency_off(self) -> bool:
        return bool(self.raw & 0x01)

    @property
    def emergency_stop(self) -> bool:
        return bool(self.raw & 0x02)

    @property
    def auto_start_mode(self) -> bool:
        """True = every loco resumes its last speed when the station powers up."""
        return bool(self.raw & 0x04)

    @property
    def service_mode(self) -> bool:
        return bool(self.raw & 0x08)

    @property
    def powering_up(self) -> bool:
        return bool(self.raw & 0x40)

    @property
    def ram_error(self) -> bool:
        return bool(self.raw & 0x80)


@dataclass(frozen=True)
class CvValue:
    """A service-mode or POM CV read result.

    `raw_cv` is the CV address exactly as it arrived: one byte in the Lenz
    forms, and the 16-bit value in the Z21 form (0x64 0x14), where the two
    address bytes are joined. `cv` is the absolute CV number when the reply
    header determines it without ambiguity, and None otherwise:

    - 0x15/0x16/0x17 are unambiguous. Lenz 23151 sections 3.1.2.7 to 3.1.2.9
      map C = 0..255 onto CV256..511, CV512..767 and CV768..1023.
    - 0x14 is NOT decoded. In service mode it is one-based (C=1 is CV1, C=0 is
      CV1024), but the same header carries POM results whose convention on this
      command station is exactly what check_pom_read is measuring. Guessing here
      would report a CV number the probe has not established.
    """

    raw_cv: int
    value: int
    ident: int
    cv: int | None = None


@dataclass(frozen=True)
class RegisterValue:
    """Reply 0x63 0x10 - Register or Paged mode, NOT a direct CV read.

    XpressNet section 2.1.5.5: when this arrives in answer to a Direct Mode
    request, the command station has determined that the decoder does not
    support Direct Mode and has fallen back. Data byte 2 is then a register
    number, not a CV number, so it must never be read as a CV value.
    """

    register: int
    value: int


_SPEED_STEP_MODES = {0b000: 14, 0b001: 27, 0b010: 28, 0b100: 128}


@dataclass(frozen=True)
class LocoInfo:
    ident: int
    speed: int
    f0: bool
    f1_f4: int
    f5_f12: int

    # Identification byte is 0000 BFFF (XpressNet section 2.1.14.1).
    @property
    def busy(self) -> bool:
        """True = another XpressNet device is controlling this locomotive."""
        return bool(self.ident & 0x08)

    @property
    def speed_step_mode(self) -> int | None:
        """14, 27, 28 or 128 speed steps; None for a reserved bit pattern."""
        return _SPEED_STEP_MODES.get(self.ident & 0x07)


@dataclass(frozen=True)
class Marker:
    name: str


ACK = Marker("ack")
READY = Marker("ready")
SHORT_CIRCUIT = Marker("short_circuit")
NO_ACK = Marker("no_ack")
BUSY = Marker("busy")
UNSUPPORTED = Marker("unsupported")
TRANSFER_ERROR = Marker("transfer_error")
STATION_BUSY = Marker("station_busy")

# Every reply that shares header 0x61. The two at 0x80 and 0x81 are easy to
# forget because they are not programming replies, but leaving them unparsed
# is dangerous: an unrecognised frame looks like an ordinary reply, and a
# station saying "I could not process that" would be recorded as one saying
# "I support that". See TRANSIENT below.
_HEADER_61_REPLIES = {
    0x11: READY,
    0x12: SHORT_CIRCUIT,
    0x13: NO_ACK,
    0x1F: BUSY,
    0x80: TRANSFER_ERROR,
    0x81: STATION_BUSY,
    0x82: UNSUPPORTED,
}

# The station told us it could not act right now. None of these say anything
# about whether an opcode is implemented, so every capability check must treat
# them as unresolved rather than as an answer either way.
TRANSIENT = (SHORT_CIRCUIT, BUSY, STATION_BUSY, TRANSFER_ERROR)


@dataclass(frozen=True)
class Unknown:
    telegram: bytes


Reply = Version | Status | CvValue | RegisterValue | LocoInfo | Marker | Unknown

# Lenz 23151 sections 3.1.2.7 to 3.1.2.9: the reply header names the band, and
# C = 0..255 is an offset from its base. 0x14 is absent on purpose - see CvValue.
_EXT_BAND_BASE = {0x15: 256, 0x16: 512, 0x17: 768}


def parse(telegram: bytes) -> Reply:
    if len(telegram) < 2:
        return Unknown(telegram=telegram)
    header, db0 = telegram[0], telegram[1]

    if header == 0x63 and db0 == 0x21 and len(telegram) >= 4:
        version = telegram[2]
        return Version(
            xpressnet_major=version >> 4,
            xpressnet_minor=version & 0x0F,
            command_station_id=telegram[3],
        )
    if header == 0x62 and db0 == 0x22 and len(telegram) >= 3:
        return Status(raw=telegram[2])
    if header == 0x64 and db0 == 0x14 and len(telegram) >= 5:
        # Z21 LAN_X_CV_RESULT (Z21 LAN Protocol section 6.5). Different header
        # and a 16-bit CV address, unlike the 8-bit Lenz 63 14 form. The YD7010
        # reports command station id 0x12, the Z21 family, so its XpressNet port
        # may well answer in this form; leaving it unparsed made a successful
        # POM read look exactly like silence.
        return CvValue(
            raw_cv=(telegram[2] << 8) | telegram[3],
            value=telegram[4],
            ident=db0,
            cv=None,
        )
    if header == 0x63 and db0 == 0x10 and len(telegram) >= 4:
        return RegisterValue(register=telegram[2], value=telegram[3])
    if header == 0x63 and (db0 == 0x14 or db0 in _EXT_BAND_BASE) and len(telegram) >= 4:
        base = _EXT_BAND_BASE.get(db0)
        return CvValue(
            raw_cv=telegram[2],
            value=telegram[3],
            ident=db0,
            cv=None if base is None else base + telegram[2],
        )
    if header == 0xE4 and len(telegram) >= 5:
        return LocoInfo(
            ident=db0,
            speed=telegram[2],
            f0=bool(telegram[3] & 0x10),
            f1_f4=telegram[3] & 0x0F,
            f5_f12=telegram[4],
        )
    if header == 0x61 and db0 in _HEADER_61_REPLIES:
        return _HEADER_61_REPLIES[db0]
    if header == 0x01 and db0 == 0x04:
        return ACK
    return Unknown(telegram=telegram)
