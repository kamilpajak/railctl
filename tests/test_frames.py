from tools.probe.frames import (
    LI_BROADCAST,
    LI_COMMAND,
    Frame,
    build,
    split_frames,
    telegram_length,
    xor,
)


def test_xor_of_track_power_on_payload():
    assert xor(b"\x21\x81") == 0xA0


def test_xor_of_empty_payload_is_zero():
    assert xor(b"") == 0


def test_build_prefixes_ff_fe_and_appends_checksum():
    assert build(b"\x21\x81") == b"\xff\xfe\x21\x81\xa0"


def test_build_version_request_matches_the_bytes_measured_on_hardware():
    assert build(b"\x21\x21") == b"\xff\xfe\x21\x21\x00"


def test_telegram_length_is_low_nibble_plus_two():
    assert telegram_length(0x21) == 3
    assert telegram_length(0x62) == 4
    assert telegram_length(0x63) == 5
    assert telegram_length(0xE6) == 8


def test_split_frames_returns_one_frame_and_no_remainder():
    frames, rest = split_frames(b"\xff\xfe\x63\x21\x40\x12\x10")
    assert rest == b""
    assert len(frames) == 1
    assert frames[0].telegram == b"\x63\x21\x40\x12"
    assert frames[0].solicited is True


def test_split_frames_marks_ff_fd_as_unsolicited():
    frames, _ = split_frames(b"\xff\xfd\x61\x00\x61")
    assert frames[0].solicited is False


def test_split_frames_keeps_a_partial_frame_as_remainder():
    frames, rest = split_frames(b"\xff\xfe\x63\x21\x40")
    assert frames == []
    assert rest == b"\xff\xfe\x63\x21\x40"


def test_split_frames_handles_two_concatenated_frames():
    frames, rest = split_frames(b"\xff\xfe\x61\x01\x60\xff\xfd\x61\x00\x61")
    assert rest == b""
    assert [f.telegram for f in frames] == [b"\x61\x01", b"\x61\x00"]


def test_split_frames_resyncs_past_leading_garbage():
    frames, rest = split_frames(b"hello\xff\xfe\x21\x81\xa0")
    assert rest == b""
    assert [f.telegram for f in frames] == [b"\x21\x81"]


def test_split_frames_drops_a_frame_with_a_bad_checksum():
    frames, rest = split_frames(b"\xff\xfe\x21\x81\x00\xff\xfe\x21\x81\xa0")
    assert [f.telegram for f in frames] == [b"\x21\x81"]
    assert rest == b""


def test_frame_solicited_reflects_the_prefix():
    assert Frame(LI_COMMAND, b"\x61\x01").solicited is True
    assert Frame(LI_BROADCAST, b"\x61\x01").solicited is False


def test_a_stray_prefix_before_a_real_frame_does_not_swallow_it():
    # This returned ZERO frames before the fix. The header was read from the
    # stray prefix's offset, the length it implied overran the buffer, and the
    # loop broke to wait for bytes that never came - so a reply was recorded as
    # silence, which in this project is the difference between "unsupported"
    # and "not established".
    good = build(b"\x63\x14\x08\x91")
    frames, rest = split_frames(LI_COMMAND + good)
    assert [f.telegram for f in frames] == [b"\x63\x14\x08\x91"]
    assert rest == b""


def test_a_genuinely_incomplete_frame_is_still_kept_for_the_next_read():
    # The salvage path must not cost us partial frames: with no valid frame
    # further along, the parser waits rather than discarding.
    good = build(b"\x63\x14\x08\x91")
    frames, rest = split_frames(good[:4])
    assert frames == []
    assert rest == good[:4]
    frames, rest = split_frames(rest + good[4:])
    assert [f.telegram for f in frames] == [b"\x63\x14\x08\x91"]


def test_a_frame_survives_arbitrary_leading_noise():
    good = build(b"\x63\x14\x08\x91")
    for junk in (b"", b"\x00", b"\xff", LI_COMMAND, LI_BROADCAST, LI_COMMAND * 2,
                 b"\xff\xfe\x63", b"[CS0] M: TC 0mA\r\n"):
        frames, _ = split_frames(junk + good)
        assert [f.telegram for f in frames] == [b"\x63\x14\x08\x91"], junk.hex()
