from tools.probe.checks import (
    check_function_groups,
    check_service_ext_cv,
    check_single_function,
    check_z21_opcodes,
)
from tools.probe.fake import FakeLink

EXT_CV1 = b"\x22\x18\x01"
DIRECT_CV1 = b"\x22\x15\x01"
Z21_CV29 = b"\x23\x11\x00\x1c"
DIRECT_CV29 = b"\x22\x15\x1d"
SINGLE_F0_AT_3 = b"\xe4\xf8\x00\x03\x00"
GROUP4_AT_3 = b"\xe4\x23\x00\x03\x00"
GROUP5_AT_3 = b"\xe4\x28\x00\x03\x00"


def test_extended_and_direct_reads_agreeing_sets_service_ext_cv_true():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
        }
    )
    assert check_service_ext_cv(link).value is True


def test_unsupported_extended_opcode_sets_service_ext_cv_false():
    link = FakeLink({EXT_CV1: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_service_ext_cv(link).value is False


def test_disagreeing_values_leave_service_ext_cv_unknown():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x09\x7e"],
        }
    )
    result = check_service_ext_cv(link)
    assert result.value is None
    assert "disagree" in result.detail


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
    assert result.value is None
    assert "no value" in result.detail


def test_z21_opcodes_unknown_when_z21_succeeds_but_direct_silent():
    link = FakeLink({Z21_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"]})
    result = check_z21_opcodes(link)
    assert result.value is None
    assert "no value" in result.detail
