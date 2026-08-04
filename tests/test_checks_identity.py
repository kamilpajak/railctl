import json
from unittest.mock import patch

from tools.probe.checks import (
    DECODER_TYPES,
    check_address_band,
    check_identity,
    check_loco_info,
    read_f0,
    read_function_state_13_28,
)
from tools.probe.fake import FakeLink
from tools.probe.frames import build

VERSION = b"\x21\x21"
STATUS = b"\x21\x24"
LOCO_INFO_AT_3 = b"\xe3\x00\x00\x03"


def test_identity_reports_the_version_and_decoded_status():
    link = FakeLink(
        {
            VERSION: [b"\xff\xfe\x63\x21\x40\x12\x10"],
            STATUS: [b"\xff\xfe\x62\x22\x07\x47"],
        }
    )
    result = check_identity(link)
    assert result.value["xpressnet"] == "4.0"
    assert result.value["command_station_id"] == 0x12
    assert result.value["status_raw"] == 0x07
    assert result.value["auto_start_mode"] is True


def test_identity_warns_when_the_station_resumes_speeds_on_power_up():
    link = FakeLink(
        {
            VERSION: [b"\xff\xfe\x63\x21\x40\x12\x10"],
            STATUS: [b"\xff\xfe\x62\x22\x07\x47"],
        }
    )
    assert "last known speed" in check_identity(link).detail


def test_identity_without_a_version_reply_is_unknown():
    link = FakeLink({})
    assert check_identity(link).value is None


def test_ms450_is_a_known_decoder_type():
    assert DECODER_TYPES[6] == "MS450"


def test_address_band_confirms_the_threshold_when_only_one_form_answers():
    short_form = b"\xe3\x00\x00\x64"
    long_form = b"\xe3\x00\xc0\x64"
    link = FakeLink({long_form: [b"\xff\xfe\xe4\x04\x00\x00\x00\xe0"], short_form: []})
    result = check_address_band(link, address=100)
    assert result.value == 100


def test_address_band_is_unknown_when_both_forms_answer():
    short_form = b"\xe3\x00\x00\x64"
    long_form = b"\xe3\x00\xc0\x64"
    reply = b"\xff\xfe\xe4\x04\x00\x00\x00\xe0"
    link = FakeLink({long_form: [reply], short_form: [reply]})
    assert check_address_band(link, address=100).value is None


def test_address_band_does_not_count_a_rejection_as_an_answer():
    # A 61 82 rejection is a frame like any other. Testing merely whether frames
    # came back makes an explicit "not supported" indistinguishable from a real
    # locomotive information reply, which silences the only informative outcome.
    short_form = b"\xe3\x00\x00\x64"
    long_form = b"\xe3\x00\xc0\x64"
    link = FakeLink(
        {
            short_form: [build(b"\x61\x82")],
            long_form: [b"\xff\xfe\xe4\x04\x00\x00\x00\xe0"],
        }
    )
    result = check_address_band(link, address=100)
    assert result.value == 100
    assert "long form" in result.detail


def test_check_loco_info_reports_speed_steps_and_the_busy_flag():
    # E4 0C: B=1 (another device is driving), FFF=100 (128 speed steps).
    link = FakeLink({LOCO_INFO_AT_3: [build(b"\xe4\x0c\x20\x1f\x00")]})
    result, info, _ = check_loco_info(link, address=3)
    assert result.value == {"speed_step_mode": 128, "loco_busy": True, "f0": True}
    assert info.f0 is True
    assert "another XpressNet device" in result.detail


def test_check_loco_info_is_unknown_without_a_locomotive_information_reply():
    link = FakeLink({LOCO_INFO_AT_3: [build(b"\x61\x1f")]})
    result, info, frames = check_loco_info(link, address=3)
    assert result.value is None
    assert info is None
    assert len(frames) == 1


def test_read_f0_returns_true_when_f0_is_on():
    # E4 03 20 1F 00 with checksum D8
    link = FakeLink({LOCO_INFO_AT_3: [b"\xff\xfe\xe4\x03\x20\x1f\x00\xd8"]})
    f0_is_on, frames = read_f0(link, address=3)
    assert f0_is_on is True
    assert len(frames) > 0


def test_read_f0_returns_false_when_f0_is_off():
    # E4 03 20 00 00 with checksum C7
    link = FakeLink({LOCO_INFO_AT_3: [b"\xff\xfe\xe4\x03\x20\x00\x00\xc7"]})
    f0_is_on, frames = read_f0(link, address=3)
    assert f0_is_on is False
    assert len(frames) > 0


def test_read_f0_returns_none_with_non_empty_frames_on_non_loco_info_reply():
    # 61 1F BUSY reply with checksum 7E
    # This is the critical case: a valid, checksummed reply that is not LocoInfo
    link = FakeLink({LOCO_INFO_AT_3: [b"\xff\xfe\x61\x1f\x7e"]})
    f0_is_on, frames = read_f0(link, address=3)
    assert f0_is_on is None
    assert len(frames) > 0  # Non-empty frames is critical; this would crash without the fix


def test_read_f0_returns_none_with_empty_frames_on_timeout():
    # No reply at all
    link = FakeLink({})
    f0_is_on, frames = read_f0(link, address=3)
    assert f0_is_on is None
    assert len(frames) == 0


def test_main_skips_single_function_check_without_crashing_on_non_loco_info_reply(capsys):
    # End-to-end: main() must not crash when read_f0 gets a valid, checksummed
    # reply that is not a LocoInfo (here, a 61 1F BUSY), and it must record the
    # R5 check as skipped (None), not silently drop it and not guess a value.
    from tools.probe import __main__

    # - check_identity: version + status
    # - check_pom_read: CV value
    # - read_f0: 61 1F (BUSY) -- valid checksum, not LocoInfo -- this is the
    #   frame that used to crash main() via a double hex-dump
    # - check_function_groups: function group replies
    link = FakeLink(
        {
            b"\x21\x21": [b"\xff\xfe\x63\x21\x40\x12\x10"],  # version
            b"\x21\x24": [b"\xff\xfe\x62\x22\x07\x47"],  # status
            b"\xe6\x30\x00\x03\xe4\x07\x00": [b"\xff\xfe\x63\x14\x07\x91\xe1"],  # POM read
            b"\xe3\x00\x00\x03": [b"\xff\xfe\x61\x1f\x7e"],  # loco info -> BUSY (not LocoInfo)
            b"\xe4\x23\x00\x03\x00": [b"\xff\xfe\x01\x04\x05"],  # function group 4
            b"\xe4\x28\x00\x03\x00": [b"\xff\xfe\x01\x04\x05"],  # function group 5
        }
    )

    with patch("tools.probe.__main__.SerialLink") as mock_link_class:
        mock_link_class.return_value.__enter__.return_value = link
        mock_link_class.return_value.__exit__.return_value = None

        # --format json so the recorded result can be inspected, not just the
        # absence of an exception (a silently dropped check would also "not crash").
        result = __main__.main(
            ["--address", "3", "--format", "json", "--port", "/dev/fake", "--no-programming-track"]
        )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capabilities"]["single_function_cmd"] is None
    skipped = next(c for c in payload["checks"] if c["name"] == "single_function_cmd")
    assert "could not read F0" in skipped["detail"]


def test_read_function_state_13_28_parses_the_e3_52_reply():
    # Lenz 23151 section 3.1.9.2: E3 52 D1 D2, where D1 bit 0 is F13 and D2
    # bit 0 is F21.
    link = FakeLink({b"\xe3\x09\x00\x03": [build(b"\xe3\x52\x05\x02")]})
    state, frames = read_function_state_13_28(link, address=3)
    assert (state.f13_f20, state.f21_f28) == (0b0000_0101, 0b0000_0010)
    assert len(frames) == 1


def test_read_function_state_13_28_returns_none_without_a_state_reply():
    link = FakeLink({b"\xe3\x09\x00\x03": [build(b"\x61\x82")]})
    state, frames = read_function_state_13_28(link, address=3)
    assert state is None
    assert len(frames) == 1
