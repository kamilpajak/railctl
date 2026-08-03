"""X-Bus payload builders (header + data, no LI prefix and no XOR).

CV numbers are 1-based on the way in, for every function here.

The wire conventions are NOT uniform, and this is the single most dangerous
detail in the module:

- POM (0xE6 0x30) and the Z21 opcodes (0x23 0x11) are ZERO-BASED:
  CV1 goes on the wire as 0. Those two, and only those two, call cv_wire().
- The legacy direct read (0x22 0x15) and the extended reads (0x22 0x18..0x1B)
  are ONE-BASED: CV1 goes on the wire as 1. Lenz XpressNet Protocol
  Description section 2.2.8, verbatim: "The range is from 1 to 256, CV256 is
  sent as 00".

Routing a service-mode opcode through cv_wire() reads the wrong CV off the
decoder and reports it under the right name, which is why each function
states its convention.
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


def loco_info(address: int) -> bytes:
    """Request locomotive information (F0 state, speed, etc.)."""
    high, low = loco_address_bytes(address)
    return bytes([0xE3, 0x00, high, low])


def service_result() -> bytes:
    return b"\x21\x10"


def pom_read(address: int, cv: int) -> bytes:
    wire = cv_wire(cv)
    high, low = loco_address_bytes(address)
    option = 0xE4 | ((wire >> 8) & 0x03)
    return bytes([0xE6, 0x30, high, low, option, wire & 0xFF, 0x00])


def service_direct_read(cv: int) -> bytes:
    """Legacy direct read, 0x22 0x15.

    IMPORTANT: this opcode is ONE-BASED, unlike POM. Lenz XpressNet Protocol
    Description section 2.2.8 states verbatim: "The range is from 1 to 256,
    CV256 is sent as 00". So CV1 goes on the wire as 0x01, CV29 as 0x1D.
    Do NOT route this through cv_wire() — that is the POM/Z21 convention.

    CV256 is refused because its wire value 0 collides with CV1024 in the
    newer LI-USB document, and nothing here needs to guess which the YD7010
    implements.
    """
    if not 1 <= cv <= 256:
        raise ValueError(f"CV {cv} exceeds the 256 CV limit of the legacy direct read")
    if cv == 256:
        raise ValueError("CV256 is sent as 0, which is ambiguous; use the extended opcodes")
    return bytes([0x22, 0x15, cv & 0xFF])


def service_ext_read(cv: int) -> bytes:
    """Extended read, 0x22 0x18..0x1B. Also ONE-BASED, banded by cv >> 8:
    0x18 covers CV1-255, 0x19 CV256-511, 0x1A CV512-767, 0x1B CV768-1023."""
    if not 1 <= cv <= MAX_CV:
        raise ValueError(f"CV {cv} outside the extended opcode range 1..{MAX_CV}")
    band = cv >> 8
    if band > 3:
        raise ValueError(f"CV {cv} outside the extended opcode range 1..{MAX_CV}")
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
