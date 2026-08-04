"""128-step speed byte.

Wire layout is RVVVVVVV: bit 7 is direction (1 = forward), the low seven bits
are 0 for a braked stop, 1 for emergency stop, and 2..127 for steps 1..126.
Wire value 1 is reserved, which is why an ordinary step is `step + 1` and never
collides with it. Direction is carried even on a stop, so a stop command must
not lose the direction the locomotive was travelling in.
"""

from __future__ import annotations

import pytest

from railctl.xbus.speed import (
    DRIVE_IDENT_128,
    EMERGENCY_STOP_WIRE,
    MAX_SPEED_STEP,
    SPEED_STEPS,
    Direction,
    decode_speed_128,
    encode_emergency_stop_128,
    encode_speed_128,
)

SPEED_VECTORS = [
    ((0, Direction.FORWARD), 0x80),
    ((0, Direction.REVERSE), 0x00),
    ((1, Direction.FORWARD), 0x82),
    ((60, Direction.FORWARD), 0xBD),
    ((63, Direction.FORWARD), 0xC0),
    ((126, Direction.FORWARD), 0xFF),
    ((126, Direction.REVERSE), 0x7F),
]

DECODE_VECTORS = [
    (0x80, (0, Direction.FORWARD, False)),
    (0x00, (0, Direction.REVERSE, False)),
    (0x82, (1, Direction.FORWARD, False)),
    (0xBD, (60, Direction.FORWARD, False)),
    (0xFF, (126, Direction.FORWARD, False)),
    (0x7F, (126, Direction.REVERSE, False)),
    (0x81, (0, Direction.FORWARD, True)),
    (0x01, (0, Direction.REVERSE, True)),
]


def test_the_constants_describe_128_step_mode():
    assert SPEED_STEPS == 128
    assert MAX_SPEED_STEP == 126
    assert DRIVE_IDENT_128 == 0x13


@pytest.mark.parametrize(("args", "expected"), SPEED_VECTORS)
def test_encode_matches_the_golden_byte(args: tuple[int, Direction], expected: int):
    step, direction = args
    assert encode_speed_128(step, direction) == expected


def test_emergency_stop_uses_the_reserved_wire_value_one():
    assert encode_emergency_stop_128(Direction.FORWARD) == 0x81
    assert encode_emergency_stop_128(Direction.REVERSE) == 0x01


@pytest.mark.parametrize(("byte", "expected"), DECODE_VECTORS)
def test_decode_matches_the_golden_triple(byte: int, expected: tuple[int, Direction, bool]):
    assert decode_speed_128(byte) == expected


@pytest.mark.parametrize("step", [-1, 127, 1000])
def test_encode_rejects_a_step_outside_zero_to_126(step: int):
    with pytest.raises(ValueError, match="out of range"):
        encode_speed_128(step, Direction.FORWARD)


@pytest.mark.parametrize("byte", [-1, 256])
def test_decode_rejects_a_wire_value_outside_a_byte(byte: int):
    with pytest.raises(ValueError, match="not a byte"):
        decode_speed_128(byte)


def test_direction_is_carried_even_on_a_stop():
    """A stop that forgets the direction makes the next start guess it."""
    assert decode_speed_128(encode_speed_128(0, Direction.FORWARD))[1] is Direction.FORWARD
    assert decode_speed_128(encode_speed_128(0, Direction.REVERSE))[1] is Direction.REVERSE


def test_every_step_round_trips_in_both_directions():
    for step in range(0, MAX_SPEED_STEP + 1):
        for direction in Direction:
            assert decode_speed_128(encode_speed_128(step, direction)) == (step, direction, False)


def test_no_ordinary_step_ever_lands_on_the_emergency_stop_value():
    for step in range(0, MAX_SPEED_STEP + 1):
        for direction in Direction:
            assert encode_speed_128(step, direction) & 0x7F != EMERGENCY_STOP_WIRE
