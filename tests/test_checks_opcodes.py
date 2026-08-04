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


def test_all_three_reads_agreeing_sets_both_extended_flags_true():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            EXT_CV265: [build(b"\x63\x15\x09\x2d")],
        }
    )
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is True
    assert value["service_ext_cv_high_value"] == 0x2D


def test_a_working_low_band_does_not_imply_the_high_band():
    # Band 0x18 overlaps the legacy 0x22 0x15 opcode, so a station can implement
    # it and still reject 0x19. Inferring the high band from the low one would
    # report the ZIMO CVs above 255 as reachable when they are not.
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            EXT_CV265: [b"\xff\xfe\x61\x82\xe3"],
        }
    )
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is False


def test_high_band_silence_leaves_the_high_band_unknown_not_false():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
        }
    )
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is True
    assert value["service_ext_cv_high_band"] is None


def test_a_high_band_reply_for_the_wrong_cv_is_not_taken_as_success():
    # 63 15 0A decodes to CV266, but CV265 was asked for.
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            EXT_CV265: [build(b"\x63\x15\x0a\x2d")],
        }
    )
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv_high_band"] is None
    assert "CV266" in result.detail


def test_unsupported_extended_opcode_sets_both_extended_flags_false():
    link = FakeLink({EXT_CV1: [b"\xff\xfe\x61\x82\xe3"]})
    value = check_service_ext_cv(link).value
    assert value["service_ext_cv"] is False
    assert value["service_ext_cv_high_band"] is False


def test_disagreeing_values_leave_service_ext_cv_unknown():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x09\x7e"],
        }
    )
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv"] is None
    assert "disagree" in result.detail


def test_a_register_mode_fallback_is_not_read_as_a_cv_value():
    # 63 10 means the decoder does not support Direct Mode and the station
    # dropped to Register/Paged mode, so the number is a register.
    link = FakeLink(
        {
            EXT_CV1: [build(b"\x63\x10\x01\x03")],
            DIRECT_CV1: [build(b"\x63\x10\x01\x03")],
        }
    )
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv"] is None
    assert "Register/Paged" in result.detail


def test_z21_check_reports_a_register_mode_fallback_as_unresolved():
    link = FakeLink(
        {
            Z21_CV29: [build(b"\x63\x10\x01\x03")],
            DIRECT_CV29: [build(b"\x63\x10\x01\x03")],
        }
    )
    result = check_z21_opcodes(link)
    assert result.value is None
    assert "register" in result.detail


def test_z21_opcode_matching_the_direct_read_sets_the_flag():
    link = FakeLink(
        {
            Z21_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"],
            DIRECT_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"],
        }
    )
    assert check_z21_opcodes(link).value is True


def test_z21_opcode_rejected_sets_the_flag_false():
    link = FakeLink({Z21_CV29: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_z21_opcodes(link).value is False


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
    assert check_function_groups(link, address=3).value is False


def test_function_groups_true_when_both_are_accepted():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x01\x04\x05"],
        }
    )
    assert check_function_groups(link, address=3).value is True


def test_function_groups_false_when_group_4_rejected_and_group_5_silent():
    link = FakeLink({GROUP4_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_function_groups(link, address=3)
    assert result.value is False
    assert "rejected" in result.detail


def test_function_groups_false_when_group_4_silent_and_group_5_rejected():
    link = FakeLink({GROUP5_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_function_groups(link, address=3)
    assert result.value is False
    assert "rejected" in result.detail


def test_function_groups_unknown_when_both_groups_silent():
    link = FakeLink({})
    result = check_function_groups(link, address=3)
    assert result.value is None
    assert "no reply" in result.detail


def test_function_groups_unknown_when_one_group_busy_and_the_other_accepted():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x61\x1f\x7e"],
        }
    )
    result = check_function_groups(link, address=3)
    assert result.value is None
    # Verify frame was actually parsed (not dropped for bad checksum) and BUSY was seen
    assert len(result.frames) == 2  # One ACK from G4, one BUSY marker from G5


def test_service_ext_cv_unknown_when_extended_succeeds_but_direct_silent():
    link = FakeLink({EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"]})
    result = check_service_ext_cv(link)
    assert result.value["service_ext_cv"] is None
    assert "no value" in result.detail


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
    assert check_function_groups(link, address=3).value is None


def test_z21_opcodes_unknown_when_z21_succeeds_but_direct_silent():
    link = FakeLink({Z21_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"]})
    result = check_z21_opcodes(link)
    assert result.value is None
    assert "no value" in result.detail
