from tools.probe.checks import (
    check_function_groups,
    check_service_ext_cv,
    check_single_function,
    check_z21_opcodes,
)
from tools.probe.fake import FakeLink
from tools.probe.frames import build

EXT_CV1 = b"\x22\x18\x01"
EXT_CV265 = b"\x22\x19\x09"
DIRECT_CV1 = b"\x22\x15\x01"
Z21_CV29 = b"\x23\x11\x00\x1c"
DIRECT_CV29 = b"\x22\x15\x1d"
SINGLE_F0_AT_3 = b"\xe4\xf8\x00\x03\x00"
GROUP4_AT_3 = b"\xe4\x23\x00\x03\x00"
GROUP5_AT_3 = b"\xe4\x28\x00\x03\x00"


def test_single_function_accepted_when_the_station_does_not_reject_it():
    link = FakeLink({SINGLE_F0_AT_3: [b"\xff\xfe\x01\x04\x05"]})
    assert check_single_function(link, address=3, f0_is_on=False).value is True


def test_single_function_rejected_sets_it_false():
    link = FakeLink({SINGLE_F0_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_single_function(link, address=3, f0_is_on=False).value is False


def test_single_function_silence_leaves_it_unknown():
    link = FakeLink({})
    assert check_single_function(link, address=3, f0_is_on=False).value is None


def test_function_groups_need_both_group_4_and_group_5():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x61\x82\xe3"],
        }
    )
    assert check_function_groups(link, address=3, f13_f20=0, f21_f28=0).value is False


def test_function_groups_true_when_both_are_accepted():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x01\x04\x05"],
        }
    )
    assert check_function_groups(link, address=3, f13_f20=0, f21_f28=0).value is True


def test_function_groups_false_when_group_4_rejected_and_group_5_silent():
    link = FakeLink({GROUP4_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_function_groups(link, address=3, f13_f20=0, f21_f28=0)
    assert result.value is False
    assert "rejected" in result.detail


def test_function_groups_false_when_group_4_silent_and_group_5_rejected():
    link = FakeLink({GROUP5_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_function_groups(link, address=3, f13_f20=0, f21_f28=0)
    assert result.value is False
    assert "rejected" in result.detail


def test_function_groups_unknown_when_both_groups_silent():
    link = FakeLink({})
    result = check_function_groups(link, address=3, f13_f20=0, f21_f28=0)
    assert result.value is None
    assert "no reply" in result.detail


def test_function_groups_unknown_when_one_group_busy_and_the_other_accepted():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x61\x1f\x7e"],
        }
    )
    result = check_function_groups(link, address=3, f13_f20=0, f21_f28=0)
    assert result.value is None
    # Verify frame was actually parsed (not dropped for bad checksum) and BUSY was seen
    assert len(result.frames) == 2  # One ACK from G4, one BUSY marker from G5


def test_command_station_busy_is_not_acceptance_of_a_single_function_command():
    # 61 81 is "command station busy" (Lenz 2.1.4.2). It says nothing about
    # whether E4 F8 is implemented, so the capability must stay unresolved.
    link = FakeLink({SINGLE_F0_AT_3: [build(b"\x61\x81")]})
    assert check_single_function(link, address=3, f0_is_on=False).value is None


def test_transfer_error_is_not_acceptance_of_a_single_function_command():
    link = FakeLink({SINGLE_F0_AT_3: [build(b"\x61\x80")]})
    assert check_single_function(link, address=3, f0_is_on=False).value is None


def test_command_station_busy_is_not_acceptance_of_a_function_group():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [build(b"\x61\x81")],
        }
    )
    assert check_function_groups(link, address=3, f13_f20=0, f21_f28=0).value is None


def test_function_groups_re_assert_the_current_state_instead_of_clearing_it():
    # The group commands carry the whole bitmask, so all-zero bits would switch
    # off every function currently on. F13, F15 and F22 are on here, and both
    # commands must go out carrying exactly those bits back.
    f13_f20, f21_f28 = 0b0000_0101, 0b0000_0010
    link = FakeLink({})
    check_function_groups(link, address=3, f13_f20=f13_f20, f21_f28=f21_f28)
    assert link.sent == [
        build(bytes([0xE4, 0x23, 0x00, 0x03, f13_f20])),
        build(bytes([0xE4, 0x28, 0x00, 0x03, f21_f28])),
    ]


# --- R2 / R4, anchored on a known constant instead of on a sibling opcode ---

EXT_CV8 = b"\x22\x18\x08"  # service_ext_read(8), band 0
EXT_CV265 = b"\x22\x19\x09"  # service_ext_read(265), band 1
Z21_CV8 = b"\x23\x11\x00\x07"  # z21_service_read(8), zero-based
CV8_IS_145 = build(b"\x63\x14\x08\x91")
CV265_ON_BAND_15 = build(b"\x63\x15\x09\x2d")


def test_both_extended_flags_true_when_the_constant_and_the_echo_match():
    link = FakeLink({EXT_CV8: [CV8_IS_145], EXT_CV265: [CV265_ON_BAND_15]})
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is True
    assert value["service_ext_cv_high_value"] == 0x2D


def test_a_working_low_band_does_not_imply_the_high_band():
    # Band 0x18 overlaps the legacy opcode, so a station can implement it and
    # still reject 0x19 - the band every ZIMO CV above 255 lives in.
    link = FakeLink({EXT_CV8: [CV8_IS_145], EXT_CV265: [build(b"\x61\x82")]})
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is False


def test_high_band_silence_leaves_the_high_band_unknown_not_false():
    link = FakeLink({EXT_CV8: [CV8_IS_145]})
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is None


def test_a_high_band_reply_for_the_wrong_cv_is_not_success():
    # 63 15 0A decodes to CV266, but CV265 was requested.
    link = FakeLink({EXT_CV8: [CV8_IS_145], EXT_CV265: [build(b"\x63\x15\x0a\x2d")]})
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv_high_band"] is None
    assert "CV266" in result.detail


def test_a_wrong_value_for_the_reference_cv_is_not_a_pass():
    # The station answered, so it implements the opcode - but the value is not the
    # ZIMO constant, which is how an off-by-one in the CV encoding surfaces. Two
    # opcodes sharing that bug would have agreed with each other.
    link = FakeLink({EXT_CV8: [build(b"\x63\x14\x08\x63")]})
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv"] is None
    assert "expected the known 145" in result.detail


def test_unsupported_extended_opcode_is_false():
    link = FakeLink({EXT_CV8: [build(b"\x61\x82")]})
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is False
    assert value["service_ext_cv_high_band"] is None


def test_a_register_mode_fallback_is_not_read_as_a_cv_value():
    link = FakeLink({EXT_CV8: [build(b"\x63\x10\x01\x03")]})
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv"] is None
    assert "Register/Paged" in result.detail


def test_z21_opcode_true_when_the_constant_matches():
    link = FakeLink({Z21_CV8: [CV8_IS_145]})
    assert check_z21_opcodes(link).value is True


def test_z21_opcode_rejected_is_false():
    link = FakeLink({Z21_CV8: [build(b"\x61\x82")]})
    assert check_z21_opcodes(link).value is False


def test_z21_opcode_silence_is_unknown():
    assert check_z21_opcodes(FakeLink({})).value is None


def test_a_silent_peer_opcode_can_no_longer_downgrade_a_proven_z21_read():
    # The regression this guards: the check used to validate 23 11 by comparing it
    # against 22 15, so a silent peer reported "unknown" for a capability just
    # demonstrated. Nothing but 23 11 is sent now.
    link = FakeLink({Z21_CV8: [CV8_IS_145]})
    assert check_z21_opcodes(link).value is True
    assert [s[2:4] for s in link.sent] == [b"\x23\x11"]
