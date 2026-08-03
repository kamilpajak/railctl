"""X-Bus payload builders (header + data, no LI prefix and no XOR).

CV numbers are 1-based on the way in. cv_wire() is the ONLY place the
zero-based conversion happens, so it cannot be applied twice.
"""

from __future__ import annotations

MAX_CV = 1024
XPRESSNET_LONG_ADDRESS_THRESHOLD = 100


def cv_wire(cv: int) -> int:
    if not 1 <= cv <= MAX_CV:
        raise ValueError(f"CV {cv} out of range 1..{MAX_CV}")
    return cv - 1


def loco_address_bytes(
    address: int, *, threshold: int = XPRESSNET_LONG_ADDRESS_THRESHOLD
) -> tuple[int, int]:
    if not 1 <= address <= 9999:
        raise ValueError(f"loco address {address} out of range 1..9999")
    value = address + 0xC000 if address >= threshold else address
    return (value >> 8) & 0xFF, value & 0xFF


def version() -> bytes:
    return b"\x21\x21"


def status() -> bytes:
    return b"\x21\x24"


def service_result() -> bytes:
    return b"\x21\x10"


def pom_read(address: int, cv: int) -> bytes:
    wire = cv_wire(cv)
    high, low = loco_address_bytes(address)
    option = 0xE4 | ((wire >> 8) & 0x03)
    return bytes([0xE6, 0x30, high, low, option, wire & 0xFF, 0x00])


def service_direct_read(cv: int) -> bytes:
    """Legacy direct read. Only CV1..255 — wire value 0 is ambiguous between
    CV256 and CV1024 across the two Lenz documents, so it is refused."""
    if cv > 256:
        raise ValueError(f"CV {cv} exceeds the 256 CV limit of the legacy direct read")
    if cv == 256:
        raise ValueError("CV256 encodes as wire 0, which is ambiguous; use the extended opcodes")
    wire = max(1, cv_wire(cv))
    return bytes([0x22, 0x15, wire])


def service_ext_read(cv: int) -> bytes:
    band = cv >> 8
    if band > 3:
        raise ValueError(f"CV {cv} outside the extended opcode range 1..1024")
    return bytes([0x22, 0x18 + band, cv & 0xFF])


def z21_service_read(cv: int) -> bytes:
    wire = cv_wire(cv)
    return bytes([0x23, 0x11, (wire >> 8) & 0xFF, wire & 0xFF])


def function_group(address: int, group: int, bits: int) -> bytes:
    high, low = loco_address_bytes(address)
    return bytes([0xE4, group, high, low, bits & 0xFF])


def single_function(address: int, index: int, action: int) -> bytes:
    """action: 0 = off, 1 = on, 2 = toggle. index: F0..F28."""
    if not 0 <= index <= 28:
        raise ValueError(f"function index {index} out of range 0..28")
    if action not in (0, 1, 2):
        raise ValueError(f"action {action} must be 0 (off), 1 (on) or 2 (toggle)")
    high, low = loco_address_bytes(address)
    return bytes([0xE4, 0xF8, high, low, (action << 6) | index])
