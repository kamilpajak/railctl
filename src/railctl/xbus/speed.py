"""128-step speed and direction.

Wire layout RVVVVVVV:

    0        braked stop
    1        emergency stop (reserved - no ordinary step may encode to it)
    2..127   steps 1..126
    bit 7    direction, 1 = forward

Because wire value 1 is reserved, an ordinary step is `step + 1`. Direction is
carried even on a stop, so stopping a locomotive does not erase which way it was
facing.

`Direction` is defined here once and re-exported by `railctl.station`; the CLI
parses it as `Direction[value.upper()]`. Defining it twice is how a REVERSE that
means 1 in one module and 0 in another gets shipped.
"""

from __future__ import annotations

import enum

SPEED_STEPS = 128
MAX_SPEED_STEP = 126
DRIVE_IDENT_128 = 0x13

DIRECTION_BIT = 0x80
SPEED_MASK = 0x7F
STOP_WIRE = 0x00
EMERGENCY_STOP_WIRE = 0x01
WIRE_STEP_OFFSET = 1

_BYTE_MIN = 0
_BYTE_MAX = 255


class Direction(enum.IntEnum):
    REVERSE = 0
    FORWARD = 1


def encode_speed_128(step: int, direction: Direction) -> int:
    """Encode a 0..126 speed step. Step 0 is a braked stop, not an emergency stop."""
    if not 0 <= step <= MAX_SPEED_STEP:
        raise ValueError(f"speed step {step} out of range 0..{MAX_SPEED_STEP}")
    wire = STOP_WIRE if step == 0 else step + WIRE_STEP_OFFSET
    return wire | (DIRECTION_BIT if direction is Direction.FORWARD else 0)


def encode_emergency_stop_128(direction: Direction) -> int:
    """The reserved wire value 1, with the direction bit still set correctly."""
    return EMERGENCY_STOP_WIRE | (DIRECTION_BIT if direction is Direction.FORWARD else 0)


def decode_speed_128(byte: int) -> tuple[int, Direction, bool]:
    """Return `(step, direction, emergency)` for a speed byte."""
    if not _BYTE_MIN <= byte <= _BYTE_MAX:
        raise ValueError(f"{byte} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")
    direction = Direction.FORWARD if byte & DIRECTION_BIT else Direction.REVERSE
    wire = byte & SPEED_MASK
    if wire == EMERGENCY_STOP_WIRE:
        return 0, direction, True
    if wire == STOP_WIRE:
        return 0, direction, False
    return wire - WIRE_STEP_OFFSET, direction, False
