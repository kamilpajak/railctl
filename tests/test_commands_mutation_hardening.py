"""Pinning tests written to kill surviving mutants in `commands.py`.

Provenance: cosmic-ray run of 2026-08-04, 417 mutants, 401 killed by the prior
suite. See docs/test-hardening.md for the full triage.

Every test here is an exact boundary. The property tests next door already
sweep these ranges, but a sweep is a distribution: asking for 25 or 100 draws
from `integers(-100, 20000)` and hoping one of them is exactly 10000 is not a
test of the value 10000. Hypothesis is good at finding the shape of a bug and
poor at hitting a named constant, so the constants are named here instead.
"""

from __future__ import annotations

import pytest

from tools.probe import commands
from tools.probe.commands import MAX_CV


@pytest.mark.parametrize("address", [1, 9999])
def test_the_locomotive_address_range_accepts_its_own_endpoints(address: int):
    commands.loco_address_bytes(address)


@pytest.mark.parametrize("address", [0, 10000])
def test_the_locomotive_address_range_rejects_just_outside_its_endpoints(address: int):
    with pytest.raises(ValueError):
        commands.loco_address_bytes(address)


@pytest.mark.parametrize("cv", [1, MAX_CV])
def test_the_cv_range_accepts_its_own_endpoints(cv: int):
    commands.cv_wire(cv)
    commands.service_ext_read(cv)


@pytest.mark.parametrize("cv", [0, MAX_CV + 1])
def test_the_cv_range_rejects_just_outside_its_endpoints(cv: int):
    with pytest.raises(ValueError):
        commands.cv_wire(cv)
    with pytest.raises(ValueError):
        commands.service_ext_read(cv)


def test_cv1024_reaches_the_decoder_through_band_zero_slot_zero():
    """The awkward one. CV1024 has no band of its own and rides in the slot that
    CV0 would have occupied, so it is reachable even though `cv >> 8` would put
    it past the last opcode."""
    assert commands.service_ext_read(MAX_CV) == b"\x22\x18\x00"


def test_the_legacy_direct_read_stops_at_255():
    commands.service_direct_read(255)
    with pytest.raises(ValueError):
        commands.service_direct_read(256)
    with pytest.raises(ValueError):
        commands.service_direct_read(257)


@pytest.mark.parametrize("index", [0, 28])
def test_the_function_index_range_accepts_its_own_endpoints(index: int):
    commands.single_function(3, index, 1)


@pytest.mark.parametrize("index", [-1, 29])
def test_the_function_index_range_rejects_just_outside_its_endpoints(index: int):
    with pytest.raises(ValueError):
        commands.single_function(3, index, 1)


@pytest.mark.parametrize("action", [0, 1, 2])
def test_every_documented_function_action_is_accepted(action: int):
    commands.single_function(3, 0, action)


@pytest.mark.parametrize("action", [-1, 3])
def test_an_undocumented_function_action_is_rejected(action: int):
    """Action 3 does not exist, and letting it through would set the two action
    bits to a pattern whose meaning is undefined on the decoder."""
    with pytest.raises(ValueError):
        commands.single_function(3, 0, action)


@pytest.mark.parametrize(
    ("address", "expected_long"),
    [(99, False), (100, True), (127, True), (128, True)],
)
def test_the_long_address_threshold_sits_exactly_at_100(address: int, expected_long: bool):
    """Address 100 is the first long address. The 100..127 band is where the
    XpressNet and Z21 conventions diverge, so both of its ends are named here."""
    high, _ = commands.loco_address_bytes(address)
    assert ((high & 0xC0) == 0xC0) is expected_long
