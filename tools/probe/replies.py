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
    raw_cv: int
    value: int
    ident: int


@dataclass(frozen=True)
class Marker:
    name: str


ACK = Marker("ack")
READY = Marker("ready")
SHORT_CIRCUIT = Marker("short_circuit")
NO_ACK = Marker("no_ack")
BUSY = Marker("busy")
UNSUPPORTED = Marker("unsupported")

_PROGRAMMING_MARKERS = {
    0x11: READY,
    0x12: SHORT_CIRCUIT,
    0x13: NO_ACK,
    0x1F: BUSY,
    0x82: UNSUPPORTED,
}


@dataclass(frozen=True)
class Unknown:
    telegram: bytes


Reply = Version | Status | CvValue | Marker | Unknown


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
    if header == 0x63 and db0 in (0x10, 0x14) and len(telegram) >= 4:
        return CvValue(raw_cv=telegram[2], value=telegram[3], ident=db0)
    if header == 0x61 and db0 in _PROGRAMMING_MARKERS:
        return _PROGRAMMING_MARKERS[db0]
    if header == 0x01 and db0 == 0x04:
        return ACK
    return Unknown(telegram=telegram)
