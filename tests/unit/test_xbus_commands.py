"""Golden byte vectors and behaviour tests for the X-Bus command encoders.

Every expected telegram in this file is a literal from the design document. An
encoder change that is intentional edits one row here; an encoder change that is
not, fails one row here.
"""

from __future__ import annotations

import pytest

from railctl.xbus.commands import (
    FUNCTION_BITS,
    GROUP_FUNCTIONS,
    MAX_FUNCTION,
    FunctionGroup,
    pack_function_bits,
)


def test_every_function_from_f0_to_f28_has_a_group_and_a_bit():
    assert sorted(FUNCTION_BITS) == list(range(0, MAX_FUNCTION + 1))


def test_f0_lives_in_bit_4_of_group_1_not_bit_0():
    """F0 is the headlight and it does NOT sit where F1 sits. The E4 20 byte is
    000 F0 F4 F3 F2 F1, so bit 4 is the headlight and bit 0 is F1."""
    assert FUNCTION_BITS[0] == (FunctionGroup.G1, 4)
    assert FUNCTION_BITS[1] == (FunctionGroup.G1, 0)


@pytest.mark.parametrize(
    ("group", "functions"),
    [
        (FunctionGroup.G1, (0, 1, 2, 3, 4)),
        (FunctionGroup.G2, (5, 6, 7, 8)),
        (FunctionGroup.G3, (9, 10, 11, 12)),
        (FunctionGroup.G4, (13, 14, 15, 16, 17, 18, 19, 20)),
        (FunctionGroup.G5, (21, 22, 23, 24, 25, 26, 27, 28)),
    ],
)
def test_each_group_owns_exactly_its_documented_functions(group, functions):
    assert GROUP_FUNCTIONS[group] == functions


def test_no_two_functions_in_one_group_share_a_bit():
    for group, functions in GROUP_FUNCTIONS.items():
        bits = [FUNCTION_BITS[f][1] for f in functions]
        assert len(set(bits)) == len(bits), group


@pytest.mark.parametrize(
    ("group", "state", "expected"),
    [
        (FunctionGroup.G1, {0: True, 1: False, 2: False, 3: False, 4: False}, 0x10),
        (FunctionGroup.G1, {0: False, 1: True, 2: False, 3: False, 4: False}, 0x01),
        (FunctionGroup.G1, {0: True, 1: True, 2: True, 3: True, 4: True}, 0x1F),
        (FunctionGroup.G2, {5: True, 6: False, 7: False, 8: False}, 0x01),
        (FunctionGroup.G3, {9: True, 10: False, 11: False, 12: False}, 0x01),
        (FunctionGroup.G4, dict.fromkeys(range(13, 21), False) | {13: True}, 0x01),
        (FunctionGroup.G5, dict.fromkeys(range(21, 29), False) | {21: True}, 0x01),
        (FunctionGroup.G5, dict.fromkeys(range(21, 29), True), 0xFF),
    ],
)
def test_pack_function_bits_matches_the_wire_layout(group, state, expected):
    assert pack_function_bits(group, state) == expected


def test_pack_function_bits_ignores_functions_belonging_to_other_groups():
    """The station holds one shadow map for all 29 functions and hands the whole
    map to each group in turn."""
    whole_shadow = dict.fromkeys(range(0, MAX_FUNCTION + 1), False) | {0: True, 5: True}
    assert pack_function_bits(FunctionGroup.G1, whole_shadow) == 0x10
    assert pack_function_bits(FunctionGroup.G2, whole_shadow) == 0x01


def test_pack_function_bits_refuses_a_state_missing_a_function_of_the_group():
    """E4 20 writes all five bits at once, so a caller that supplies only F0
    would silently switch F1-F4 off. Treating a missing key as False is the
    'absence read as a negative fact' failure this project keeps producing, so
    it is an error instead."""
    with pytest.raises(ValueError, match="missing"):
        pack_function_bits(FunctionGroup.G1, {0: True})


def test_pack_function_bits_refuses_a_function_index_that_does_not_exist():
    state = dict.fromkeys(range(0, 5), False) | {29: True}
    with pytest.raises(ValueError, match="out of range"):
        pack_function_bits(FunctionGroup.G1, state)


from railctl.xbus.commands import (  # noqa: E402
    cmd_drive_128,
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_function_group,
    cmd_function_state_13_28,
    cmd_loco_info,
    cmd_pom_read_byte,
    cmd_pom_write_bit,
    cmd_pom_write_byte,
    cmd_service_direct_read,
    cmd_service_direct_write,
    cmd_service_ext_read,
    cmd_service_ext_write,
    cmd_service_result_request,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
    cmd_z21_cv_write,
)
from railctl.xbus.dialect import XPRESSNET  # noqa: E402
from railctl.xbus.speed import Direction  # noqa: E402

XN = XPRESSNET.long_address_threshold  # 100


def hexbytes(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize(
    ("telegram", "expected"),
    [
        (cmd_station_version(), "21 21 00"),
        (cmd_station_status(), "21 24 05"),
        (cmd_track_power_on(), "21 81 A0"),
        (cmd_track_power_off(), "21 80 A1"),
        (cmd_emergency_stop_all(), "80 80"),
        (cmd_emergency_stop_loco(3, threshold=XN), "92 00 03 91"),
        (cmd_emergency_stop_loco(1234, threshold=XN), "92 C4 D2 84"),
        (cmd_drive_128(3, 1, Direction.FORWARD, threshold=XN), "E4 13 00 03 82 76"),
        (cmd_drive_128(3, 60, Direction.FORWARD, threshold=XN), "E4 13 00 03 BD 49"),
        (cmd_drive_128(3, 126, Direction.FORWARD, threshold=XN), "E4 13 00 03 FF 0B"),
        (cmd_drive_128(1000, 63, Direction.FORWARD, threshold=XN), "E4 13 C3 E8 C0 1C"),
        (cmd_function_group(3, FunctionGroup.G1, 0x10, threshold=XN), "E4 20 00 03 10 D7"),
        (cmd_function_group(3, FunctionGroup.G2, 0x01, threshold=XN), "E4 21 00 03 01 C7"),
        (cmd_function_group(3, FunctionGroup.G3, 0x01, threshold=XN), "E4 22 00 03 01 C4"),
        (cmd_function_group(3, FunctionGroup.G4, 0x01, threshold=XN), "E4 23 00 03 01 C5"),
        (cmd_function_group(3, FunctionGroup.G5, 0x01, threshold=XN), "E4 28 00 03 01 CE"),
        (cmd_loco_info(3, threshold=XN), "E3 00 00 03 E0"),
        (cmd_loco_info(1234, threshold=XN), "E3 00 C4 D2 F5"),
        (cmd_function_state_13_28(3, threshold=XN), "E3 09 00 03 E9"),
        (cmd_service_direct_read(8), "22 15 08 3F"),
        (cmd_service_direct_write(144, 0), "23 16 90 00 A5"),
        (cmd_service_ext_read(8), "22 18 08 32"),
        (cmd_service_ext_read(256), "22 19 00 3B"),
        (cmd_service_ext_read(257), "22 19 01 3A"),
        (cmd_service_ext_read(512), "22 1A 00 38"),
        (cmd_service_ext_read(1023), "22 1B FF C6"),
        (cmd_service_ext_read(1024), "22 18 00 3A"),
        (cmd_service_ext_write(257, 5), "23 1D 01 05 3A"),
        (cmd_z21_cv_read(1), "23 11 00 00 32"),
        (cmd_z21_cv_read(8), "23 11 00 07 35"),
        (cmd_z21_cv_read(257), "23 11 01 00 33"),
        (cmd_z21_cv_read(1024), "23 11 03 FF CE"),
        (cmd_z21_cv_write(8, 12), "24 12 00 07 0C 3D"),
        (cmd_service_result_request(), "21 10 31"),
        (cmd_pom_read_byte(3, 8, threshold=XN), "E6 30 00 03 E4 07 00 36"),
        (cmd_pom_read_byte(3, 256, threshold=XN), "E6 30 00 03 E4 FF 00 CE"),
        (cmd_pom_read_byte(3, 257, threshold=XN), "E6 30 00 03 E5 00 00 30"),
        (cmd_pom_read_byte(3, 1024, threshold=XN), "E6 30 00 03 E7 FF 00 CD"),
        (cmd_pom_read_byte(1234, 300, threshold=XN), "E6 30 C4 D2 E5 2B 00 0E"),
        (cmd_pom_write_byte(3, 8, 12, threshold=XN), "E6 30 00 03 EC 07 0C 32"),
        (cmd_pom_write_byte(3, 31, 0, threshold=XN), "E6 30 00 03 EC 1E 00 27"),
        (cmd_pom_write_byte(3, 32, 0, threshold=XN), "E6 30 00 03 EC 1F 00 26"),
        (cmd_pom_write_bit(3, 29, 3, True, threshold=XN), "E6 30 00 03 E8 1C 0B 2A"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_encoder_golden_vectors(telegram: bytes, expected: str):
    assert telegram == hexbytes(expected)


def test_the_step_126_telegram_contains_a_literal_ff_byte():
    """E4 13 00 03 FF 0B is loco 3 at full speed forward and it carries FF in its
    payload. This is why the envelope must anchor on the FF FE prefix once and
    then trust the header nibble for the length, instead of searching for a
    delimiter."""
    assert b"\xff" in cmd_drive_128(3, 126, Direction.FORWARD, threshold=XN)


def test_a_pom_write_bit_index_above_seven_is_refused():
    with pytest.raises(ValueError, match="bit 8 out of range 0..7"):  # noqa: RUF043
        cmd_pom_write_bit(3, 29, 8, True, threshold=XN)


@pytest.mark.parametrize("value", [-1, 256])
def test_a_cv_value_outside_a_byte_is_refused(value: int):
    with pytest.raises(ValueError, match="value"):
        cmd_service_direct_write(8, value)
    with pytest.raises(ValueError, match="value"):
        cmd_z21_cv_write(8, value)
    with pytest.raises(ValueError, match="value"):
        cmd_pom_write_byte(3, 8, value, threshold=XN)


def test_function_bits_outside_a_byte_are_refused():
    with pytest.raises(ValueError, match="function bits"):
        cmd_function_group(3, FunctionGroup.G1, 256, threshold=XN)


def test_the_emergency_stop_for_one_loco_is_the_dedicated_92_instruction():
    """Not E4 13 with wire speed 1. The 92 instruction carries no direction bit,
    so a safety path never has to make a loco_info round trip first to find out
    which way the locomotive was facing."""
    assert cmd_emergency_stop_loco(3, threshold=XN)[0] == 0x92


from railctl.xbus.commands import TimeoutClass, timeout_class  # noqa: E402


@pytest.mark.parametrize(
    "telegram",
    [
        cmd_service_direct_read(8),
        cmd_service_direct_write(8, 1),
        cmd_service_ext_read(8),
        cmd_service_ext_read(256),
        cmd_service_ext_read(512),
        cmd_service_ext_read(768),
        cmd_service_ext_write(8, 1),
        cmd_service_ext_write(257, 1),
        cmd_z21_cv_read(8),
        cmd_z21_cv_write(8, 1),
        cmd_service_result_request(),
    ],
    ids=lambda t: t.hex(" "),
)
def test_service_mode_telegrams_get_the_long_budget(telegram: bytes):
    assert timeout_class(telegram) is TimeoutClass.PROGRAMMING


@pytest.mark.parametrize(
    "telegram",
    [
        cmd_station_version(),
        cmd_station_status(),
        cmd_track_power_on(),
        cmd_track_power_off(),
        cmd_emergency_stop_all(),
        cmd_emergency_stop_loco(3, threshold=XN),
        cmd_drive_128(3, 1, Direction.FORWARD, threshold=XN),
        cmd_function_group(3, FunctionGroup.G1, 0x10, threshold=XN),
        cmd_loco_info(3, threshold=XN),
        cmd_function_state_13_28(3, threshold=XN),
        cmd_pom_read_byte(3, 8, threshold=XN),
        cmd_pom_write_byte(3, 8, 1, threshold=XN),
        cmd_pom_write_bit(3, 29, 3, True, threshold=XN),
    ],
    ids=lambda t: t.hex(" "),
)
def test_normal_operation_telegrams_get_the_short_budget(telegram: bytes):
    """POM is NORMAL on purpose: its command reply is the interface ACK, which
    comes back immediately. The long wait for a POM result, if one ever arrives,
    is a separate await_frame in the station layer, not this budget."""
    assert timeout_class(telegram) is TimeoutClass.NORMAL


@pytest.mark.parametrize("telegram", [b"", b"\x21"])
def test_a_telegram_too_short_to_classify_is_refused(telegram: bytes):
    """Every telegram reaching this function was produced by an encoder in this
    module and is at least two bytes long. Something shorter is a caller bug, and
    handing it the short budget would hide that bug behind a plausible answer -
    the same shape as reading an absence as a fact."""
    with pytest.raises(ValueError, match="too short"):
        timeout_class(telegram)


def test_a_drive_telegram_that_happens_to_start_with_a_programming_pair_is_not_promoted():
    """22 15 as the FIRST TWO bytes is the direct read; the same two values
    further into a payload are not. Classification looks at position 0 and 1
    only."""
    assert timeout_class(b"\xe4\x13\x22\x15\x82\x00") is TimeoutClass.NORMAL


def test_every_service_mode_encoder_is_in_the_programming_table():
    """Derive the list instead of restating it.

    PROGRAMMING_TELEGRAMS is hand written, and the parametrize above is a second
    hand-written copy of the same calls. Nothing ties either to the set of
    encoders that actually exist. Add a cmd_service_* encoder later, forget the
    table, and its telegram gets the 5.0 s budget instead of 95.0 s (spec line
    314) - so the reply arrives after the window closes and the station records
    the opcode as unsupported. That is the M1 failure exactly: a capability
    recorded absent because the instrument measuring it was mis-set.
    """
    import railctl.xbus.commands as c

    for name in dir(c):
        if name.startswith(("cmd_service_", "cmd_z21_cv_")):
            fn = getattr(c, name)
            args = (8, 1) if "write" in name else ((8,) if "cv" in name or "read" in name else ())
            assert c.timeout_class(fn(*args)) is c.TimeoutClass.PROGRAMMING, name
