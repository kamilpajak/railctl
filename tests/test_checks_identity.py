from tools.probe.checks import DECODER_TYPES, check_address_band, check_identity
from tools.probe.fake import FakeLink

VERSION = b"\x21\x21"
STATUS = b"\x21\x24"


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
