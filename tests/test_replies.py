from tools.probe.replies import (
    ACK,
    BUSY,
    NO_ACK,
    READY,
    SHORT_CIRCUIT,
    UNSUPPORTED,
    CvValue,
    Status,
    Unknown,
    Version,
    parse,
)


def test_parses_the_version_reply_measured_on_the_yd7010():
    assert parse(b"\x63\x21\x40\x12") == Version(
        xpressnet_major=4, xpressnet_minor=0, command_station_id=0x12
    )


def test_parses_a_status_reply():
    assert parse(b"\x62\x22\x07") == Status(raw=0x07)


def test_status_decodes_bit_2_as_start_mode_not_short_circuit():
    status = Status(raw=0x07)
    assert status.emergency_off is True
    assert status.emergency_stop is True
    assert status.auto_start_mode is True
    assert status.service_mode is False


def test_status_service_mode_is_bit_3():
    assert Status(raw=0x08).service_mode is True


def test_parses_a_direct_cv_result():
    assert parse(b"\x63\x14\x07\x91") == CvValue(raw_cv=0x07, value=0x91, ident=0x14)


def test_parses_a_register_or_paged_result():
    assert parse(b"\x63\x10\x01\x03") == CvValue(raw_cv=0x01, value=0x03, ident=0x10)


def test_parses_the_generic_interface_acknowledgement():
    assert parse(b"\x01\x04") is ACK


def test_parses_the_programming_status_replies():
    assert parse(b"\x61\x11") is READY
    assert parse(b"\x61\x12") is SHORT_CIRCUIT
    assert parse(b"\x61\x13") is NO_ACK
    assert parse(b"\x61\x1f") is BUSY


def test_parses_instruction_not_supported():
    assert parse(b"\x61\x82") is UNSUPPORTED


def test_unrecognised_telegram_becomes_unknown():
    assert parse(b"\x55\xaa") == Unknown(telegram=b"\x55\xaa")


def test_empty_telegram_becomes_unknown():
    assert parse(b"") == Unknown(telegram=b"")
