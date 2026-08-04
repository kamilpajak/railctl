"""The five properties of the pure X-Bus layer.

Property tests state a law that must hold for every input. The example tests
next door pin the bytes one afternoon's hardware produced. Neither replaces the
other: hypothesis finds the shape of a bug, it does not promise to visit a named
constant, so boundaries stay in the example files.

Deliberately absent: any property over a command encoder. Rebuilding
`cmd_drive_128` inside its own test asserts that two copies of the same mistake
agree.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from railctl.errors import XBusDecodeError
from railctl.xbus.address import (
    LOCO_ADDR_MAX,
    LOCO_ADDR_MIN,
    decode_loco_address,
    encode_loco_address,
)
from railctl.xbus.codec import decode, encode, xor
from railctl.xbus.dialect import DIALECTS, DIVERGENCE_BAND, XPRESSNET, Z21, Dialect
from railctl.xbus.speed import (
    EMERGENCY_STOP_WIRE,
    MAX_SPEED_STEP,
    SPEED_MASK,
    Direction,
    decode_speed_128,
    encode_speed_128,
)

ADDRESSES = st.integers(min_value=LOCO_ADDR_MIN, max_value=LOCO_ADDR_MAX)
STEPS = st.integers(min_value=0, max_value=MAX_SPEED_STEP)
DIRECTIONS = st.sampled_from(list(Direction))


@st.composite
def telegrams(draw: st.DrawFn) -> bytes:
    """A valid telegram of any shape: header, matching data count, real XOR."""
    header = draw(st.integers(min_value=0, max_value=255))
    count = header & 0x0F
    data = draw(st.lists(st.integers(min_value=0, max_value=255), min_size=count, max_size=count))
    return encode(header, *data)


PROFILE_EXAMPLES = {"default": 100, "mutation": 25, "ci": 500}
DERANDOMISED_PROFILES = {"ci", "mutation"}


def test_the_ci_profile_is_derandomised():
    """A newly discovered example must not fail an unrelated CI run.

    Random draws also make any mutation score a sample rather than a
    measurement; two runs of identical code scored differently before this was
    set on the probe (docs/test-hardening.md).

    This checks the REGISTERED profile only. Registering it is not the same as
    running under it - see the next test.
    """
    assert settings.get_profile("ci").derandomize is True


def test_the_loaded_profile_matches_the_environment():
    """settings.default is the profile load_profile() actually installed.

    Without this, `HYPOTHESIS_PROFILE=ci pytest` looks identical to a plain run:
    both pass, and nothing anywhere states that the 500-example pass really
    happened. Reading `settings.default` rather than `get_profile("ci")` is the
    whole point - it is the only value that differs when conftest.py never saw
    the environment variable.

    An unknown profile name raises KeyError here on purpose: a typo in
    HYPOTHESIS_PROFILE must not silently fall back to 100 random examples.
    """
    name = os.environ.get("HYPOTHESIS_PROFILE", "default")
    assert settings.default.derandomize is (name in DERANDOMISED_PROFILES)
    assert settings.default.max_examples == PROFILE_EXAMPLES[name]


@given(ADDRESSES, st.sampled_from(DIALECTS))
def test_an_address_survives_the_round_trip_under_every_dialect(address: int, dialect: Dialect):
    """Property 1. Whatever the threshold, the address that went in comes out."""
    high, low = encode_loco_address(address, long_threshold=dialect.long_address_threshold)
    assert decode_loco_address(high, low) == address


@given(ADDRESSES.filter(lambda a: a not in DIVERGENCE_BAND))
def test_the_dialects_agree_outside_the_divergence_band(address: int):
    """Property 2. Only 100..127 is contested; everywhere else the bytes match."""
    assert encode_loco_address(
        address, long_threshold=XPRESSNET.long_address_threshold
    ) == encode_loco_address(address, long_threshold=Z21.long_address_threshold)


@given(st.sampled_from(list(DIVERGENCE_BAND)))
def test_the_dialects_differ_inside_the_divergence_band(address: int):
    """Property 3. XpressNet sends 100..127 long, Z21 sends them short.

    Stated as a law so that "simplifying" the two thresholds into one fails
    loudly. A decoder configured short in this range ignores the long form in
    silence, which is indistinguishable from a decoder that is not there.
    """
    xpressnet = encode_loco_address(address, long_threshold=XPRESSNET.long_address_threshold)
    z21 = encode_loco_address(address, long_threshold=Z21.long_address_threshold)
    assert xpressnet != z21
    assert xpressnet[0] == 0xC0
    assert z21[0] == 0x00


@given(STEPS, DIRECTIONS)
def test_a_speed_step_round_trips_and_never_collides_with_emergency_stop(
    step: int, direction: Direction
):
    """Property 4. Wire value 1 is reserved; no ordinary step may reach it."""
    wire = encode_speed_128(step, direction)
    assert wire & SPEED_MASK != EMERGENCY_STOP_WIRE
    assert decode_speed_128(wire) == (step, direction, False)


@given(telegrams(), st.data())
def test_a_single_flipped_bit_is_always_detected(telegram: bytes, data: st.DataObject):
    """Property 5. One corrupted bit must never decode as a valid telegram.

    A corrupted frame that slips through is worse than a lost one: it becomes a
    reply the station never sent. Flipping a bit inside the header changes the
    declared length, so that case surfaces as a length mismatch instead of a
    checksum mismatch. XBusIncompleteError cannot occur here - flipping a bit
    never shortens the buffer, and the shortest telegram is already
    MIN_TELEGRAM_LEN - but catching the parent keeps this law about "no corrupted
    frame decodes" rather than about which of the three faults fired. Which one
    fires is pinned in test_codec.py, by class and not by message text.
    """
    index = data.draw(st.integers(min_value=0, max_value=len(telegram) - 1))
    bit = data.draw(st.integers(min_value=0, max_value=7))
    corrupted = bytearray(telegram)
    corrupted[index] ^= 1 << bit
    if index > 0:
        assert xor(bytes(corrupted)) != 0
    with pytest.raises(XBusDecodeError):
        decode(bytes(corrupted))
