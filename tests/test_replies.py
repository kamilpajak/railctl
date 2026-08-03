from tools.probe.replies import (
    ACK,
    BUSY,
    NO_ACK,
    READY,
    SHORT_CIRCUIT,
    UNSUPPORTED,
    CvValue,
    LocoInfo,
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


def test_parses_loco_info_with_f0_on():
    # E4 ident speed [f0-f4] [f5-f12]
    # f0 is bit 4 of the f0-f4 byte
    result = parse(b"\xE4\x03\x20\x1F\x00")
    assert result == LocoInfo(ident=0x03, speed=0x20, f0=True, f1_f4=0x0F, f5_f12=0x00)
    assert result.f0 is True


def test_parses_loco_info_with_f0_off():
    # f0 is bit 4 of the f0-f4 byte, so 0x00 means f0 is off
    result = parse(b"\xE4\x03\x20\x00\x00")
    assert result == LocoInfo(ident=0x03, speed=0x20, f0=False, f1_f4=0x00, f5_f12=0x00)
    assert result.f0 is False


def test_too_short_loco_info_telegram_becomes_unknown():
    # Only 4 bytes, needs at least 5
    result = parse(b"\xE4\x03\x20\x1F")
    assert result == Unknown(telegram=b"\xE4\x03\x20\x1F")
