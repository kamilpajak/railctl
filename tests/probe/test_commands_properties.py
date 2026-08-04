"""Property tests for the payload builders.

The module docstring in `commands.py` calls the non-uniform CV encoding "the
single most dangerous detail in the module", because getting it wrong reads the
WRONG CV off the decoder and reports the value under the RIGHT name. Nothing in
the output looks unusual; the number is simply from somewhere else. Once railctl
gains a write path the same mistake writes to the wrong CV.

So these tests do not check the bytes each builder happens to emit - the example
tests already pin those. They decode each telegram back through the convention
its own docstring claims, for every CV in range, and assert the requested CV
comes back out. A convention stated in prose and a convention implemented in
code are two different things, and this is where they are forced to agree.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.probe import commands
from tools.probe.commands import MAX_CV, XPRESSNET_LONG_ADDRESS_THRESHOLD
from tools.probe.frames import telegram_length

CVS = st.integers(min_value=1, max_value=MAX_CV)
ADDRESSES = st.integers(min_value=1, max_value=9999)
BYTES = st.integers(min_value=0, max_value=255)

# The extended read splits CV space into four 256-wide bands, one opcode each.
EXT_BAND_OPCODES = (0x18, 0x19, 0x1A, 0x1B)


def decode_address(high: int, low: int) -> int:
    """Recover a locomotive address from the two wire bytes.

    XpressNet marks a long address by setting the top two bits of the high byte,
    so the address itself is the low 14 bits.
    """
    value = (high << 8) | low
    return value & 0x3FFF if value & 0xC000 else value


def decode_ext_read(payload: bytes) -> int:
    """Recover the CV an extended read (0x22 0x18..0x1B) is asking for."""
    band = payload[1] - 0x18
    offset = payload[2]
    # Band 0 offset 0 is CV1024, not CV0: the first band carries CV1-255 at
    # their own numbers and reuses the vacant slot 0 for the top of CV space.
    if band == 0 and offset == 0:
        return MAX_CV
    return band * 256 + offset


def decode_pom_read(payload: bytes) -> int:
    """Recover the ZERO-BASED wire CV from a POM read telegram."""
    option, low = payload[4], payload[5]
    return ((option & 0x03) << 8) | low


def decode_z21_read(payload: bytes) -> int:
    """Recover the ZERO-BASED wire CV from a Z21 service read telegram."""
    return (payload[2] << 8) | payload[3]


def every_builder_payload(address: int, cv: int, bits: int, index: int, action: int):
    """One payload from every builder, for the length-coherence property."""
    return [
        commands.version(),
        commands.status(),
        commands.service_result(),
        commands.loco_info(address),
        commands.function_state_13_28(address),
        commands.pom_read(address, cv),
        commands.service_ext_read(cv),
        commands.z21_service_read(cv),
        commands.function_group(address, 0x23, bits),
        commands.single_function(address, index, action),
    ]


@given(ADDRESSES, CVS, BYTES, st.integers(0, 28), st.integers(0, 2))
def test_every_builder_declares_its_own_length_correctly(
    address: int, cv: int, bits: int, index: int, action: int
):
    """The header's low nibble must equal the number of data bytes that follow.

    A builder that disagrees with its own header does not merely send one bad
    command: the station reads the next telegram from the wrong offset, so every
    later reply on that link is lost too.
    """
    for payload in every_builder_payload(address, cv, bits, index, action):
        assert len(payload) + 1 == telegram_length(payload[0]), payload.hex(" ")


@given(CVS)
def test_cv_wire_is_zero_based_and_stays_in_range(cv: int):
    wire = commands.cv_wire(cv)
    assert wire == cv - 1
    assert 0 <= wire <= MAX_CV - 1


@given(st.integers(min_value=-2000, max_value=4000))
def test_cv_wire_accepts_exactly_the_documented_range(cv: int):
    if 1 <= cv <= MAX_CV:
        commands.cv_wire(cv)
    else:
        with pytest.raises(ValueError):
            commands.cv_wire(cv)


@given(CVS, ADDRESSES)
def test_a_pom_read_asks_for_the_cv_it_was_given(cv: int, address: int):
    """POM is zero-based, and the top two CV bits ride in the option byte."""
    payload = commands.pom_read(address, cv)
    assert decode_pom_read(payload) == cv - 1
    assert payload[4] & 0xFC == 0xE4  # option byte keeps its 0xE4 identity
    assert decode_address(payload[2], payload[3]) == address


@given(CVS)
def test_a_z21_read_asks_for_the_cv_it_was_given(cv: int):
    """The Z21 opcode is zero-based like POM, but with a full 16-bit CV field."""
    assert decode_z21_read(commands.z21_service_read(cv)) == cv - 1


@given(CVS)
def test_an_extended_read_asks_for_the_cv_it_was_given(cv: int):
    """The extended read is ONE-BASED and band-relative; decoding must land on cv.

    This is the property that an off-by-one cannot survive: every CV in the whole
    1..1024 range has to come back as itself, including the two awkward ones -
    CV256, the first CV of band 1, and CV1024, which rides in band 0's vacant
    slot 0 rather than in a band of its own.
    """
    payload = commands.service_ext_read(cv)
    assert payload[0] == 0x22
    assert payload[1] in EXT_BAND_OPCODES
    assert decode_ext_read(payload) == cv


@given(st.integers(min_value=-2000, max_value=4000))
def test_the_extended_read_accepts_exactly_the_documented_range(cv: int):
    if 1 <= cv <= MAX_CV:
        commands.service_ext_read(cv)
    else:
        with pytest.raises(ValueError):
            commands.service_ext_read(cv)


@given(st.integers(min_value=1, max_value=255))
def test_the_legacy_direct_read_is_one_based(cv: int):
    payload = commands.service_direct_read(cv)
    assert payload[:2] == b"\x22\x15"
    assert payload[2] == cv


@given(st.integers(min_value=-2000, max_value=4000))
def test_the_legacy_direct_read_refuses_cv256_and_everything_past_it(cv: int):
    """CV256 is excluded on purpose even though the opcode can express it: the
    station could read a bare 0 under either convention, so the probe routes
    CV256 and above through the unambiguous extended opcode."""
    if 1 <= cv <= 255:
        commands.service_direct_read(cv)
    else:
        with pytest.raises(ValueError):
            commands.service_direct_read(cv)


@given(st.integers(min_value=1, max_value=255), ADDRESSES)
def test_the_two_conventions_genuinely_disagree(cv: int, address: int):
    """The one-based and zero-based paths must differ by exactly one.

    Stated as a test so that "tidying up" the two families into a single helper
    fails loudly. They look like duplication and are not: routing a service-mode
    opcode through cv_wire() reads the CV next door.
    """
    one_based = commands.service_direct_read(cv)[2]
    zero_based = commands.pom_read(address, cv)[5]
    assert one_based - zero_based == 1


@given(ADDRESSES)
def test_a_locomotive_address_survives_the_round_trip(address: int):
    high, low = commands.loco_address_bytes(address)
    assert decode_address(high, low) == address


@given(ADDRESSES)
def test_the_long_address_marker_follows_the_threshold(address: int):
    high, _low = commands.loco_address_bytes(address)
    is_long = (high & 0xC0) == 0xC0
    assert is_long == (address >= XPRESSNET_LONG_ADDRESS_THRESHOLD)


@given(ADDRESSES, st.integers(min_value=1, max_value=10000))
def test_the_threshold_argument_decides_the_form_and_nothing_else(address: int, threshold: int):
    """check_address_band asks the same address both ways, so the threshold has
    to be the only thing separating the two forms."""
    high, low = commands.loco_address_bytes(address, threshold=threshold)
    assert decode_address(high, low) == address
    assert ((high & 0xC0) == 0xC0) == (address >= threshold)


@given(st.integers(min_value=-100, max_value=20000))
def test_loco_address_bytes_accepts_exactly_the_documented_range(address: int):
    if 1 <= address <= 9999:
        commands.loco_address_bytes(address)
    else:
        with pytest.raises(ValueError):
            commands.loco_address_bytes(address)


@given(ADDRESSES, st.integers(0, 28), st.integers(0, 2))
def test_a_single_function_command_packs_index_and_action_recoverably(
    address: int, index: int, action: int
):
    payload = commands.single_function(address, index, action)
    assert payload[:2] == b"\xe4\xf8"
    assert decode_address(payload[2], payload[3]) == address
    assert payload[4] & 0x3F == index
    assert payload[4] >> 6 == action


@given(ADDRESSES, st.integers(min_value=-10, max_value=60), st.integers(min_value=-5, max_value=10))
def test_single_function_accepts_exactly_the_documented_index_and_action(
    address: int, index: int, action: int
):
    if 0 <= index <= 28 and action in (0, 1, 2):
        commands.single_function(address, index, action)
    else:
        with pytest.raises(ValueError):
            commands.single_function(address, index, action)


@given(ADDRESSES, BYTES, st.integers(min_value=-1000, max_value=100000))
def test_a_function_group_carries_the_bits_it_was_handed(address: int, group: int, bits: int):
    """The group commands set every function in the group at once, so the caller's
    read-back state has to arrive on the wire unaltered. Anything else switches
    off functions the probe was told to preserve."""
    payload = commands.function_group(address, group, bits)
    assert payload[0] == 0xE4
    assert payload[1] == group
    assert decode_address(payload[2], payload[3]) == address
    assert payload[4] == bits & 0xFF
