"""Property tests for reply parsing.

`parse` is where this project's characteristic failure is manufactured. A reply
form the parser does not recognise is indistinguishable from no reply at all,
and every check above it reads silence as "the station cannot do this". That is
not hypothetical: a successful POM read really was recorded as `pom_read: False`
because the 0x64 0x14 form went unparsed, with the value sitting in the frame
dump underneath the verdict.

Two directions therefore have to hold at once, and each has its own test below:

- `parse` must never raise, whatever arrives on a shared USB port.
- `parse` must never claim MORE than the header entitles it to. Inventing a
  CvValue from an unrelated telegram would report a decoder value that no
  decoder ever sent.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from tools.probe import commands
from tools.probe.replies import (
    _EXT_BAND_BASE,
    _HEADER_61_REPLIES,
    _SPEED_STEP_MODES,
    CvValue,
    FunctionState13To28,
    LocoInfo,
    Marker,
    RegisterValue,
    Status,
    Unknown,
    Version,
    parse,
)

BYTES = st.integers(min_value=0, max_value=255)

# Which (header, db0) pairs are allowed to produce each typed reply. Every entry
# is read straight off the protocol documents, NOT off the implementation, so
# that a parser which starts recognising something new has to justify it here.
ALLOWED_HEADERS = {
    Version: {(0x63, 0x21)},
    Status: {(0x62, 0x22)},
    RegisterValue: {(0x63, 0x10)},
    FunctionState13To28: {(0xE3, 0x52)},
    CvValue: {(0x64, 0x14), (0x63, 0x14), (0x63, 0x15), (0x63, 0x16), (0x63, 0x17)},
}


@given(st.binary(max_size=24))
def test_parse_never_raises(telegram: bytes):
    """Anything at all may arrive: noise, a telemetry line, a truncated frame."""
    parse(telegram)


@given(st.binary(max_size=24))
def test_parse_only_claims_what_the_header_entitles_it_to(telegram: bytes):
    """A typed reply may only come from the headers that define it.

    The dangerous direction is not a missed reply - that shows up as silence and
    the checks already treat silence as unresolved. It is a reply invented from
    an unrelated telegram, which the checks would treat as a measurement.
    """
    reply = parse(telegram)
    for reply_type, headers in ALLOWED_HEADERS.items():
        if isinstance(reply, reply_type):
            assert len(telegram) >= 2
            assert (telegram[0], telegram[1]) in headers, telegram.hex(" ")


@given(st.binary(max_size=24))
def test_a_locomotive_info_reply_only_comes_from_header_e4(telegram: bytes):
    if isinstance(parse(telegram), LocoInfo):
        assert telegram[0] == 0xE4


@given(st.binary(min_size=2, max_size=24))
def test_a_telegram_too_short_for_its_form_stays_unknown(telegram: bytes):
    """Truncation must not be filled in with defaults. A short 0x63 0x14 is not a
    CV read of value zero; it is a frame we did not receive."""
    reply = parse(telegram[:2])
    known_two_byte_forms = telegram[0] == 0x61 or telegram[:2] == b"\x01\x04"
    if not known_two_byte_forms:
        assert isinstance(reply, Unknown)


@given(BYTES, BYTES, BYTES)
def test_a_version_reply_splits_the_version_byte_into_nibbles(
    version_byte: int, station: int, tail: int
):
    reply = parse(bytes([0x63, 0x21, version_byte, station, tail]))
    assert isinstance(reply, Version)
    assert reply.xpressnet_major == version_byte >> 4
    assert reply.xpressnet_minor == version_byte & 0x0F
    assert reply.xpressnet_major << 4 | reply.xpressnet_minor == version_byte
    assert reply.command_station_id == station


@given(st.sampled_from(sorted(_EXT_BAND_BASE)), BYTES, BYTES)
def test_a_banded_cv_reply_decodes_to_an_absolute_cv(band: int, raw_cv: int, value: int):
    reply = parse(bytes([0x63, band, raw_cv, value]))
    assert isinstance(reply, CvValue)
    assert reply.cv == _EXT_BAND_BASE[band] + raw_cv
    assert 256 <= reply.cv <= 1023
    assert reply.raw_cv == raw_cv
    assert reply.value == value


@given(BYTES, BYTES)
def test_the_ambiguous_cv_form_reports_no_absolute_cv(raw_cv: int, value: int):
    """0x63 0x14 carries both service-mode and POM results, whose conventions
    differ, so the parser must decline to name a CV rather than guess one."""
    reply = parse(bytes([0x63, 0x14, raw_cv, value]))
    assert isinstance(reply, CvValue)
    assert reply.cv is None
    assert reply.raw_cv == raw_cv


@given(BYTES, BYTES, BYTES)
def test_the_z21_cv_form_joins_both_address_bytes(high: int, low: int, value: int):
    reply = parse(bytes([0x64, 0x14, high, low, value]))
    assert isinstance(reply, CvValue)
    assert reply.raw_cv == (high << 8) | low
    assert reply.value == value
    assert reply.cv is None


@given(st.integers(min_value=256, max_value=1023), BYTES)
def test_an_extended_read_and_its_reply_agree_on_the_cv(cv: int, value: int):
    """End to end across the two modules: ask for a CV, decode the station's
    answer, and land back on the CV that was requested.

    The request names a band through its opcode (0x19/0x1A/0x1B) and the reply
    names the same band through its own header (0x15/0x16/0x17). Both encode the
    same fact in different numbers, so a shift on either side is invisible when
    each module is tested alone.
    """
    request = commands.service_ext_read(cv)
    reply_band = request[1] - 4  # 0x19 -> 0x15, 0x1A -> 0x16, 0x1B -> 0x17
    reply = parse(bytes([0x63, reply_band, request[2], value]))
    assert isinstance(reply, CvValue)
    assert reply.cv == cv
    assert reply.value == value


@given(BYTES, BYTES)
def test_a_register_reply_is_never_mistaken_for_a_cv(register: int, value: int):
    """0x63 0x10 means the station dropped to Register/Paged mode. The number is
    a register, not a CV, so reading it as one reports a decoder value that was
    never asked for."""
    reply = parse(bytes([0x63, 0x10, register, value]))
    assert isinstance(reply, RegisterValue)
    assert not isinstance(reply, CvValue)
    assert reply.register == register
    assert reply.value == value


@given(BYTES)
def test_each_status_bit_owns_exactly_one_flag(raw: int):
    """Flipping one bit must move one flag. Two flags sharing a mask would make
    a station in one state indistinguishable from a station in another."""
    flags = {
        "emergency_off": 0x01,
        "emergency_stop": 0x02,
        "auto_start_mode": 0x04,
        "service_mode": 0x08,
        "powering_up": 0x40,
        "ram_error": 0x80,
    }
    status = Status(raw=raw)
    for name, mask in flags.items():
        assert getattr(status, name) is bool(raw & mask)
        flipped = Status(raw=raw ^ mask)
        moved = {other for other in flags if getattr(status, other) != getattr(flipped, other)}
        assert moved == {name}


@given(BYTES, BYTES, BYTES, BYTES)
def test_locomotive_info_splits_its_function_byte_without_loss(
    ident: int, speed: int, fa: int, fb: int
):
    reply = parse(bytes([0xE4, ident, speed, fa, fb]))
    assert isinstance(reply, LocoInfo)
    # F0 and F1-F4 share one byte; together they must reconstruct its low 5 bits.
    assert (0x10 if reply.f0 else 0) | reply.f1_f4 == fa & 0x1F
    assert reply.f5_f12 == fb
    assert reply.speed == speed
    assert reply.busy is bool(ident & 0x08)


@given(BYTES)
def test_the_speed_step_mode_is_none_only_for_reserved_patterns(ident: int):
    reply = LocoInfo(ident=ident, speed=0, f0=False, f1_f4=0, f5_f12=0)
    pattern = ident & 0x07
    if pattern in _SPEED_STEP_MODES:
        assert reply.speed_step_mode == _SPEED_STEP_MODES[pattern]
        assert reply.speed_step_mode in (14, 27, 28, 128)
    else:
        assert reply.speed_step_mode is None


@given(BYTES, BYTES)
def test_a_function_state_reply_carries_both_bytes_unaltered(f13_f20: int, f21_f28: int):
    """These bytes go straight back out as group commands, so any alteration here
    switches off functions the probe promised to preserve."""
    reply = parse(bytes([0xE3, 0x52, f13_f20, f21_f28]))
    assert isinstance(reply, FunctionState13To28)
    assert (reply.f13_f20, reply.f21_f28) == (f13_f20, f21_f28)


@given(BYTES)
def test_every_header_61_byte_maps_to_its_own_marker_or_to_unknown(db0: int):
    """An unlisted 0x61 reply must stay Unknown rather than borrow a neighbour's
    meaning. The station saying "I could not process that" was once recorded as
    it saying "I support that", because 61 80 and 61 81 were not in this table."""
    reply = parse(bytes([0x61, db0]))
    if db0 in _HEADER_61_REPLIES:
        assert reply is _HEADER_61_REPLIES[db0]
    else:
        assert isinstance(reply, Unknown)


def test_the_markers_are_all_distinct_objects():
    """Markers are compared by identity throughout the checks, so two conditions
    sharing an object would make them indistinguishable."""
    markers = list(_HEADER_61_REPLIES.values())
    assert len({id(marker) for marker in markers}) == len(markers)
    assert len({marker.name for marker in markers}) == len(markers)
    assert all(isinstance(marker, Marker) for marker in markers)
