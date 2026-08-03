import pytest

from tools.probe.commands import (
    cv_wire,
    function_group,
    loco_address_bytes,
    pom_read,
    service_direct_read,
    service_ext_read,
    service_result,
    single_function,
    status,
    version,
    z21_service_read,
)
from tools.probe.frames import build


def test_cv_wire_is_zero_based():
    assert cv_wire(1) == 0
    assert cv_wire(8) == 7
    assert cv_wire(29) == 28
    assert cv_wire(256) == 255
    assert cv_wire(1024) == 1023


@pytest.mark.parametrize("bad", [0, -1, 1025])
def test_cv_wire_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        cv_wire(bad)


def test_short_address_is_sent_as_is():
    assert loco_address_bytes(3) == (0x00, 0x03)
    assert loco_address_bytes(99) == (0x00, 0x63)


def test_address_at_or_above_the_threshold_gets_the_c000_offset():
    assert loco_address_bytes(100) == (0xC0, 0x64)
    assert loco_address_bytes(1234) == (0xC4, 0xD2)


def test_threshold_is_configurable_for_the_z21_convention():
    assert loco_address_bytes(100, threshold=128) == (0x00, 0x64)
    assert loco_address_bytes(128, threshold=128) == (0xC0, 0x80)


def test_simple_system_commands():
    assert build(version()) == b"\xff\xfe\x21\x21\x00"
    assert build(status()) == b"\xff\xfe\x21\x24\x05"
    assert build(service_result()) == b"\xff\xfe\x21\x10\x31"


def test_pom_read_of_cv8_at_address_3_matches_the_spec():
    assert build(pom_read(3, 8)) == b"\xff\xfe\xe6\x30\x00\x03\xe4\x07\x00\x36"


def test_pom_read_puts_the_high_cv_bits_into_the_option_byte():
    # CV300 -> wire 299 = 0x12B -> MM = 1, LSB = 0x2B
    payload = pom_read(3, 300)
    assert payload[4] == 0xE5
    assert payload[5] == 0x2B


def test_service_direct_read_matches_the_spec():
    assert build(service_direct_read(1)) == b"\xff\xfe\x22\x15\x01\x36"
    assert build(service_direct_read(29)) == b"\xff\xfe\x22\x15\x1c\x2b"


def test_service_direct_read_refuses_cv_above_256():
    with pytest.raises(ValueError, match="256"):
        service_direct_read(257)


def test_service_direct_read_refuses_cv256_because_wire_zero_is_ambiguous():
    with pytest.raises(ValueError, match="ambiguous"):
        service_direct_read(256)


def test_extended_read_picks_the_right_band_opcode():
    assert build(service_ext_read(1)) == b"\xff\xfe\x22\x18\x01\x3b"
    assert build(service_ext_read(256)) == b"\xff\xfe\x22\x19\x00\x3b"
    assert build(service_ext_read(300)) == b"\xff\xfe\x22\x19\x2c\x17"


def test_z21_service_read_uses_16_bit_zero_based_addressing():
    assert build(z21_service_read(29)) == b"\xff\xfe\x23\x11\x00\x1c\x2e"


def test_function_group_probe_telegrams():
    assert build(function_group(3, 0x23, 0x00)) == b"\xff\xfe\xe4\x23\x00\x03\x00\xc4"
    assert build(function_group(3, 0x28, 0x00)) == b"\xff\xfe\xe4\x28\x00\x03\x00\xcf"


def test_single_function_off_for_f0_at_address_3():
    assert build(single_function(3, 0, action=0)) == b"\xff\xfe\xe4\xf8\x00\x03\x00\x1f"


def test_single_function_encodes_action_in_the_top_two_bits():
    assert single_function(3, 5, action=1)[4] == 0b01_000101


@pytest.mark.parametrize("bad", [-1, 29])
def test_single_function_rejects_an_out_of_range_index(bad):
    with pytest.raises(ValueError):
        single_function(3, bad, action=0)
