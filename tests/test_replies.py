from tools.probe.replies import (
    ACK,
    BUSY,
    NO_ACK,
    READY,
    SHORT_CIRCUIT,
    STATION_BUSY,
    TRANSFER_ERROR,
    UNSUPPORTED,
    CvValue,
    LocoInfo,
    RegisterValue,
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


def test_a_register_or_paged_result_is_not_a_cv_value():
    # XpressNet 2.1.5.5: 63 10 in answer to a Direct Mode request means the
    # station fell back to Register/Paged mode, and data byte 2 is then a
    # register number. Parsing it as a CvValue would let a register value be
    # reported as the contents of a CV.
    reply = parse(b"\x63\x10\x01\x03")
    assert reply == RegisterValue(register=0x01, value=0x03)
    assert not isinstance(reply, CvValue)


def test_parses_the_cv256_to_511_band_and_decodes_the_absolute_cv():
    # Lenz 23151 section 3.1.2.7: on header 0x15, C = 0..255 maps to CV256..511.
    assert parse(b"\x63\x15\x09\x2d") == CvValue(raw_cv=0x09, value=0x2D, ident=0x15, cv=265)


def test_parses_the_cv512_to_767_band():
    assert parse(b"\x63\x16\x00\x2d") == CvValue(raw_cv=0x00, value=0x2D, ident=0x16, cv=512)


def test_parses_the_cv768_to_1023_band():
    assert parse(b"\x63\x17\xff\x2d") == CvValue(raw_cv=0xFF, value=0x2D, ident=0x17, cv=1023)


def test_the_direct_band_leaves_the_absolute_cv_undecoded():
    # 0x14 carries both service-mode results (one-based) and POM results, whose
    # convention on this station is what the probe is measuring. Decoding it
    # would report a CV number that has not been established.
    assert parse(b"\x63\x14\x07\x91").cv is None


def test_parses_the_z21_form_cv_result_with_its_16_bit_address():
    # Z21 LAN Protocol 6.5: 64 14 CVAdr_MSB CVAdr_LSB Value.
    assert parse(b"\x64\x14\x00\x07\x91") == CvValue(raw_cv=7, value=0x91, ident=0x14, cv=None)
    assert parse(b"\x64\x14\x01\x08\x2d") == CvValue(raw_cv=264, value=0x2D, ident=0x14, cv=None)


def test_parses_command_station_busy():
    assert parse(b"\x61\x81") is STATION_BUSY


def test_parses_transfer_error():
    assert parse(b"\x61\x80") is TRANSFER_ERROR


def test_loco_info_exposes_the_speed_step_mode():
    # Identification byte is 0000 BFFF (XpressNet 2.1.14.1).
    assert parse(b"\xe4\x00\x00\x00\x00").speed_step_mode == 14
    assert parse(b"\xe4\x01\x00\x00\x00").speed_step_mode == 27
    assert parse(b"\xe4\x02\x00\x00\x00").speed_step_mode == 28
    assert parse(b"\xe4\x04\x00\x00\x00").speed_step_mode == 128


def test_loco_info_reports_a_reserved_speed_step_pattern_as_unknown():
    assert parse(b"\xe4\x03\x00\x00\x00").speed_step_mode is None


def test_loco_info_exposes_the_busy_flag():
    assert parse(b"\xe4\x0c\x00\x00\x00").busy is True
    assert parse(b"\xe4\x04\x00\x00\x00").busy is False


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
