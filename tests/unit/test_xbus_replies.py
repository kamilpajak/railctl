"""Reply parsing tests.

`parse` is where this project's characteristic failure is manufactured: a reply
form the parser does not recognise is indistinguishable from no reply at all,
and the layer above reads silence as "the hardware cannot do this". That is not
a hypothetical: in M1 the whole Lenz opcode family was recorded as unimplemented
because the probe's `_read_value` never sent the `21 10` poll, so results the
station was holding were never collected (docs/probe-results.md, the R2/R4
correction). A capability was declared absent because of a defect in the
instrument measuring it.

Three things therefore have to hold at once:

- parse never raises, whatever arrives on a port shared with a telemetry stream;
- parse never claims MORE than the header entitles it to;
- an unrecognised telegram becomes Other(telegram), which carries the bytes and
  is an UNKNOWN - not the same thing as silence, and never the same thing as a
  negative answer.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.xbus.codec import encode
from railctl.xbus.replies import (
    HEADER_61_REPLIES,
    REASON_CHECKSUM,
    REASON_EMPTY,
    REASON_LENGTH,
    REASON_UNKNOWN_FORM,
    TRANSIENT_REPLIES,
    UNSUPPORTED,
    CvValue,
    EmergencyStopBroadcast,
    FunctionState13To28,
    GenericAck,
    InterfaceStatus,
    LocoInfo,
    Other,
    PagedCvValue,
    PowerState,
    StationStatus,
    StationVersion,
    Unsupported,
    parse,
)
from railctl.xbus.speed import Direction


def tg(text: str) -> bytes:
    return bytes.fromhex(text)


def test_the_measured_version_reply_is_xpressnet_40_on_a_z21_family_station():
    reply = parse(tg("63 21 40 12 10"))
    assert isinstance(reply, StationVersion)
    assert reply.raw == 0x40
    assert reply.station_id == 0x12
    assert reply.version == "4.0"
    assert reply.family == "Z21"


def test_an_unlisted_station_id_reports_its_family_as_unknown_not_as_a_guess():
    reply = parse(tg("63 21 40 7F 7D"))
    assert isinstance(reply, StationVersion)
    assert reply.family == "unknown"


def test_the_measured_status_reply_on_an_unpowered_track():
    """62 22 07 47 was measured on 2026-08-04. Bit 0 emergency off, bit 1
    emergency stop, bit 2 automatic start mode. XpressNet defines no
    short-circuit bit, and the earlier "short circuit" reading was dropped."""
    reply = parse(tg("62 22 07 47"))
    assert isinstance(reply, StationStatus)
    assert reply.raw == 0x07
    assert reply.emergency_off is True
    assert reply.emergency_stop is True
    assert reply.auto_start_mode is True
    assert reply.service_mode is False
    assert reply.powering_up is False
    assert reply.ram_error is False
    assert reply.track_power is False


def test_every_status_bit_owns_exactly_one_flag():
    """Flipping one bit must move one flag. Two flags sharing a mask would make a
    station in one state indistinguishable from a station in another. All 256
    raw bytes, not a sample."""
    flags = {
        "emergency_off": 0x01,
        "emergency_stop": 0x02,
        "auto_start_mode": 0x04,
        "service_mode": 0x08,
        "powering_up": 0x40,
        "ram_error": 0x80,
    }
    for raw in range(256):
        status = StationStatus.from_raw(raw)
        for name, mask in flags.items():
            assert getattr(status, name) is bool(raw & mask), (raw, name)
            flipped = StationStatus.from_raw(raw ^ mask)
            moved = {other for other in flags if getattr(status, other) != getattr(flipped, other)}
            assert moved == {name}, (raw, name, moved)


def test_track_power_is_the_inverse_of_emergency_off():
    assert StationStatus.from_raw(0x00).track_power is True
    assert StationStatus.from_raw(0x01).track_power is False


def test_the_lenz_cv_result_carries_the_raw_field_and_names_no_cv():
    """63 14 08 08 77. The encoding is NOT inferred here: 63 14 carries both
    service-mode and POM results whose conventions differ, and the caller knows
    which request it issued. cv.resolve_service_cv does that job."""
    reply = parse(tg("63 14 08 08 77"))
    assert isinstance(reply, CvValue)
    assert reply.raw_cv == 0x08
    assert reply.value == 0x08
    assert reply.ident == 0x14
    assert reply.z21_form is False


@pytest.mark.parametrize(
    ("telegram", "ident"),
    [("63 15 09 00 7F", 0x15), ("63 16 0A 40 3F", 0x16), ("63 17 FF 00 8B", 0x17)],
)
def test_each_extended_cv_result_band_is_parsed_and_keeps_its_ident(telegram: str, ident: int):
    reply = parse(tg(telegram))
    assert isinstance(reply, CvValue)
    assert reply.ident == ident
    assert reply.z21_form is False


def test_the_z21_cv_result_joins_both_address_bytes():
    """64 14 MSB LSB VAL is DOC ONLY - spec line 573.

    It has never been seen on this station. probe-results.md line 34 records that
    a POM read returned no 64 14 at all, and every CV read the probe measured came
    back as 63 14 or 63 15 (probe-results.md lines 148-152). It is parsed now so
    that if the Z21 LAN transport or a firmware update ever emits it, the value is
    not lost the way the missing 21 10 poll lost the Lenz results in M1.

    Whether raw_cv 7 names CV7 or CV8 is cv.resolve_service_cv's job and is
    UNMEASURED for this form, so nothing here asserts a CV number.
    """
    reply = parse(tg("64 14 00 07 91 E6"))
    assert isinstance(reply, CvValue)
    assert reply.raw_cv == 7
    assert reply.value == 145
    assert reply.ident == 0x14
    assert reply.z21_form is True


def test_a_register_result_is_a_valid_answer_and_never_a_cv_value():
    """63 10 01 03 71 means the station fell back to register or paged mode
    because the decoder did not answer a direct-mode read (23151 3.1.2.6). The
    number is a REGISTER, not a CV, so reading it as one publishes a value the
    decoder never sent. It is a valid answer, not an error."""
    reply = parse(tg("63 10 01 03 71"))
    assert isinstance(reply, PagedCvValue)
    assert not isinstance(reply, CvValue)
    assert reply.raw_register == 1
    assert reply.value == 3


def test_the_station_saying_it_could_not_process_that_is_parsed():
    """61 82 E3. In M1 this was recorded as "I support that" because the header
    pair was not in the table."""
    assert isinstance(parse(tg("61 82 E3")), Unsupported)


@pytest.mark.parametrize(
    ("telegram", "type_name"),
    [
        ("61 00 61", "PowerState"),
        ("61 01 60", "PowerState"),
        ("61 02 63", "ServiceModeEntry"),
        ("61 08 69", "TrackShortCircuit"),
        ("61 11 70", "Ready"),
        ("61 12 73", "ShortCircuit"),
        ("61 13 72", "NoAck"),
        ("61 1F 7E", "Busy"),
        ("61 80 E1", "TransferError"),
        ("61 81 E0", "StationBusy"),
        ("61 82 E3", "Unsupported"),
    ],
)
def test_every_header_61_form_is_pinned(telegram: str, type_name: str):
    assert type(parse(tg(telegram))).__name__ == type_name


def test_power_off_and_power_on_are_distinguishable():
    assert parse(tg("61 00 61")) == PowerState(on=False)
    assert parse(tg("61 01 60")) == PowerState(on=True)


def test_the_generic_ack_and_the_other_interface_frames_are_distinguishable():
    """01 04 05 means the interface forwarded the command - it is NOT a value.
    Every other 01 XX frame carries its code verbatim so the transport layer can
    map it without this module claiming to know what it means."""
    assert isinstance(parse(tg("01 04 05")), GenericAck)
    late = parse(tg("01 0A 0B"))
    assert isinstance(late, InterfaceStatus)
    assert late.code == 0x0A


def test_the_emergency_stop_broadcast_is_parsed():
    assert isinstance(parse(tg("81 00 81")), EmergencyStopBroadcast)


def test_a_locomotive_info_reply_in_128_step_mode():
    """E4 04 BD 10 00 4D: ident 0x04 is 128 speed steps and not busy, BD is step
    60 forward, FA 0x10 is F0 on with F1-F4 off."""
    reply = parse(tg("E4 04 BD 10 00 4D"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps == 128
    assert reply.speed == 60
    assert reply.direction is Direction.FORWARD
    assert reply.emergency_stopped is False
    assert reply.in_use_by_other is False
    assert reply.raw_speed == 0xBD
    assert reply.function_bits == (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_a_locomotive_info_reply_never_invents_the_address_it_was_not_sent():
    """The E4 reply carries no address field at all. The station knows which
    locomotive it asked about and fills this in with dataclasses.replace; parse
    must not guess."""
    assert parse(tg("E4 04 BD 10 00 4D")).address is None


def test_all_functions_on_and_another_device_in_control():
    reply = parse(tg("E4 0C BD 1F FF B5"))
    assert isinstance(reply, LocoInfo)
    assert reply.in_use_by_other is True
    assert reply.function_bits == tuple([True] * 13)


def test_a_locomotive_held_at_emergency_stop_is_not_reported_as_stopped_normally():
    """Wire speed 1 is emergency stop, wire speed 0 is a normal stop. Reporting
    the first as the second tells an operator the track is safe when it is not.

    This is the only positive case for emergency_stopped anywhere in the section.
    Without it an implementation that hardcodes emergency_stopped=False on the
    128-step path passes every other test, including the exhaustive sweep, and
    the one safety-relevant tri-state in LocoInfo goes unpinned.
    """
    reply = parse(tg("E4 04 81 00 00 61"))
    assert isinstance(reply, LocoInfo)
    assert reply.emergency_stopped is True
    assert reply.speed == 0
    assert reply.direction is Direction.FORWARD


def test_direction_is_carried_even_when_the_locomotive_is_stopped():
    reply = parse(tg("E4 04 00 00 00 E0"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed == 0
    assert reply.direction is Direction.REVERSE


def test_a_speed_step_mode_this_module_cannot_decode_reports_unknown_not_zero():
    """E4 02 BD 10 00 4B is a 28-step locomotive. speed.py defines only the
    128-step wire layout, so the speed is UNKNOWN, not 60 and not 0. Guessing
    would publish a speed the decoder never had."""
    reply = parse(tg("E4 02 BD 10 00 4B"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps == 28
    assert reply.speed is None
    assert reply.direction is None
    assert reply.emergency_stopped is None
    assert reply.raw_speed == 0xBD


def test_a_reserved_speed_step_pattern_reports_unknown():
    reply = parse(tg("E4 07 BD 10 00 4E"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps is None
    assert reply.speed is None


def test_an_e4_command_echoed_back_is_not_read_as_a_locomotive_info_reply():
    """E4 F8 is a COMMAND, not a reply - the single-function command the spec
    prefers (line 694). 0xE4 is the request header for drive and for functions as
    well as the loco-info reply header, and the reply is identified by an
    identification byte of the form 0000 BFFF, so only db0 0x00..0x0F is one.

    Parsing E4 F8 00 03 40 as LocoInfo would produce raw_ident 0xF8, and
    0xF8 & 0x07 == 0 means the parser would then claim 14 speed steps and
    in_use_by_other True for a locomotive nobody asked about. That is the exact
    thing the module docstring forbids: a reply invented from an unrelated
    telegram, which the station would treat as a measurement.
    """
    assert isinstance(parse(tg("E4 F8 00 03 40 5F")), Other)
    assert isinstance(parse(tg("E4 13 00 03 82 76")), Other)  # a drive command


def test_the_f13_to_f28_state_reply_is_parsed_so_the_station_never_blind_clears():
    """E3 52 D1 D2 answers E3 09 (Lenz 23151 section 3.1.9.2) and is the ONLY
    reply form carrying F13..F28; LocoInfo stops at F12.

    docs/probe-results.md lists this under Settled: "F13-F28 state readable |
    yes, E3 09 -> E3 52 D1 D2 | closes the blind-clear side effect". Leaving it
    unparsed forces the station to seed zeros before an E4 23 or E4 28 write,
    which switches off every function in the group it did not read - the failure
    spec line 1551 calls most likely to bite in practice.
    """
    reply = parse(tg("E3 52 01 80 30"))
    assert isinstance(reply, FunctionState13To28)
    assert reply.f13_f20 == 0x01
    assert reply.f21_f28 == 0x80


def test_the_replies_that_answer_nothing_either_way_are_named_in_one_place():
    """A Busy or a StationBusy says nothing about whether an opcode exists.

    Unsupported is the ONLY reply that entitles anything above to record a
    capability as False. Without one named set every consumer re-derives that
    list by hand, and the one that forgets StationBusy records a busy station as
    an absent capability - the exact M1 failure.
    """
    assert UNSUPPORTED not in TRANSIENT_REPLIES
    assert TRANSIENT_REPLIES <= set(HEADER_61_REPLIES.values())


def test_an_unrecognised_but_well_formed_telegram_becomes_other_without_raising():
    """71 AA DB. Other is an UNKNOWN carrying the bytes, so the layer above can
    print hex and a human can extend the table. It is not silence, and it is
    never a negative answer."""
    reply = parse(tg("71 AA DB"))
    assert isinstance(reply, Other)
    assert reply.telegram == tg("71 AA DB")
    assert reply.reason == REASON_UNKNOWN_FORM


def test_a_telegram_with_a_broken_xor_says_so_instead_of_just_being_unknown():
    """Three different causes must stay distinguishable, because the remedies are
    opposite: a corrupt XOR means the LINK is damaging bytes, a truncated frame
    means the read window closed early, and a well-formed telegram in a form
    nobody listed means the REPLY TABLE is incomplete. Collapsing all three into
    one value leaves the station unable to tell "the cable is bad" from "the
    station answered in a form we do not know", and both then look like an
    unresolved capability. Link.stats() counts bad_xor separately (spec line 293)
    for exactly this reason.
    """
    reply = parse(tg("62 22 07 48"))
    assert isinstance(reply, Other)
    assert reply.reason == REASON_CHECKSUM


def test_a_telegram_whose_length_disagrees_with_its_header_says_length():
    """A short 63 14 is not a CV read of value zero; it is a frame that did not
    arrive. Truncation is never filled in with defaults."""
    reply = parse(tg("63 14"))
    assert isinstance(reply, Other)
    assert reply.reason == REASON_LENGTH


def test_a_well_formed_telegram_with_no_data_bytes_is_reported_as_empty():
    """80 80 decodes cleanly - its header nibble declares zero data bytes - but
    no reply form in the index table has a zero-length body, so there is no db0
    to dispatch on. That is a third cause again, and it is not a checksum fault
    and not a truncation."""
    reply = parse(tg("80 80"))
    assert isinstance(reply, Other)
    assert reply.reason == REASON_EMPTY


@pytest.mark.parametrize(
    "reply",
    [
        parse(tg("63 21 40 12 10")),
        parse(tg("62 22 07 47")),
        parse(tg("63 14 08 08 77")),
        parse(tg("63 10 01 03 71")),
        parse(tg("E4 04 BD 10 00 4D")),
        parse(tg("01 04 05")),
        parse(tg("01 0A 0B")),
        parse(tg("61 82 E3")),
        parse(tg("71 AA DB")),
    ],
    ids=lambda r: type(r).__name__,
)
def test_every_parsed_reply_is_frozen(reply: object):
    """Parsed replies are the evidence a verdict rests on and are hex-dumped as
    an audit trail. One that could be edited after parsing would let a later
    stage rewrite what an earlier one saw."""
    fields = dataclasses.fields(reply)
    if not fields:
        pytest.skip("no fields to mutate")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(reply, fields[0].name, None)


def test_a_frozen_field_less_reply_still_refuses_a_new_attribute():
    """GenericAck has no fields, so the test above skips it, but it must still be
    impossible to hang an attribute on one.

    The exception type is not FrozenInstanceError here. On CPython 3.13 a
    frozen+slots dataclass raises TypeError from its __setattr__ when the name is
    not a declared field, and AttributeError on some versions, so all three are
    accepted. What is being pinned is the refusal, not the class of the refusal.
    """
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
        parse(tg("01 04 05")).forged = 1


def test_the_header_nibble_is_the_only_length_guard_parse_needs():
    """codec.decode has already enforced len == (header & 0x0F) + 2, so every
    branch below can index its data bytes without a second check. This test
    fixes that contract: build every reply form at its declared length and read
    the last byte each branch touches."""
    assert parse(encode(0x64, 0x14, 0x03, 0xFF, 0x2A)).value == 0x2A
    assert parse(encode(0xE4, 0x04, 0x00, 0x00, 0x80)).function_bits[12] is True


from railctl.xbus.commands import FUNCTION_BITS, FunctionGroup  # noqa: E402
from railctl.xbus.replies import (  # noqa: E402
    Busy,
    NoAck,
    Ready,
    ServiceModeEntry,
    ShortCircuit,
    StationBusy,
    TrackShortCircuit,
    TransferError,
)

# Written from the protocol documents, NOT from the parser, so a disagreement is
# a disagreement about the protocol rather than about control flow.
EXPECTED_61 = {
    0x00: PowerState,
    0x01: PowerState,
    0x02: ServiceModeEntry,
    0x08: TrackShortCircuit,
    0x11: Ready,
    0x12: ShortCircuit,
    0x13: NoAck,
    0x1F: Busy,
    0x80: TransferError,
    0x81: StationBusy,
    0x82: Unsupported,
}


def valid_telegram(header: int, db0: int) -> bytes:
    """A telegram of exactly the length its header declares, with a good XOR.

    Data bytes after the first are zero. Headers whose low nibble is 0 carry no
    data at all, so db0 is dropped for them - which is itself the right answer:
    no reply form in the index table has a zero-length body.
    """
    want = header & 0x0F
    data = ([db0] + [0x00] * want)[:want]
    return encode(header, *data)


def expected_type(header: int, db0: int) -> type:
    if header & 0x0F == 0:
        return Other
    if header == 0x01:
        return GenericAck if db0 == 0x04 else InterfaceStatus
    if header == 0x61 and db0 in EXPECTED_61:
        return EXPECTED_61[db0]
    if (header, db0) == (0x62, 0x22):
        return StationStatus
    if (header, db0) == (0x63, 0x10):
        return PagedCvValue
    if header == 0x63 and db0 in (0x14, 0x15, 0x16, 0x17):
        return CvValue
    if (header, db0) == (0x63, 0x21):
        return StationVersion
    if (header, db0) == (0x64, 0x14):
        return CvValue
    if (header, db0) == (0x81, 0x00):
        return EmergencyStopBroadcast
    if (header, db0) == (0xE3, 0x52):
        return FunctionState13To28
    if header == 0xE4 and db0 <= 0x0F:
        # The identification byte is 0000 BFFF, so only the low nibble is a
        # reply. E4 F8 and E4 13 are commands sharing the header.
        return LocoInfo
    return Other


def test_the_dispatch_table_matches_the_protocol_documents():
    """All 65536 header/db0 pairs, not a sample of them.

    Exhaustiveness is the point. The mutants that survived sampling in M1 were
    header comparisons weakened from == to >=, and each misbehaves for only a
    handful of specific byte pairs. A generated-input test can sweep the space
    but cannot promise to visit (0x62, 0x22) - which is exactly where "equal to"
    and "at least" stop agreeing.
    """
    wrong = []
    for header in range(256):
        for db0 in range(256):
            got = type(parse(valid_telegram(header, db0)))
            want = expected_type(header, db0)
            if got is not want:
                wrong.append((hex(header), hex(db0), want.__name__, got.__name__))
    assert not wrong, f"{len(wrong)} misparsed pairs, first few: {wrong[:6]}"


def test_no_header_pair_outside_the_table_produces_a_typed_reply():
    """The converse. Everything undocumented must land on Other.

    A parser that widens silently is how a station's "I could not process that"
    became "I support that": the reply had a header nobody had listed, and an
    unlisted reply that borrows a neighbour's meaning is worse than one that is
    not understood at all.
    """
    for header in range(256):
        for db0 in range(256):
            if expected_type(header, db0) is Other:
                assert isinstance(parse(valid_telegram(header, db0)), Other), (header, db0)


def test_parse_never_raises_on_anything_that_can_arrive_on_a_shared_port():
    corpus = [
        b"",
        b"\xff",
        b"\xff\xfe",
        b"\xff\xfe\x63\x21",
        b"TC=0 U=15.1 I=0\r\n",
        bytes(range(256)),
    ]
    for header in range(256):
        for length in range(0, 10):
            corpus.append(bytes([header]) * length)
            corpus.append(bytes([header] + [0xAA] * length))
    for header in range(256):
        for db0 in range(256):
            corpus.append(bytes([header, db0]))
    for telegram in corpus:
        parse(telegram)


# Every typed reply and the header pairs that are allowed to produce it. `Other`
# is the ONE deliberate exemption: it is the catch-all unknown and may come from
# any header pair at all, which is the whole reason it exists.
#
# The header-0x61 rows are derived from EXPECTED_61 rather than retyped, so the
# table cannot drift away from it. Unsupported is the one type this test most
# needs to constrain - it is the ONLY reply that entitles anything above to
# record a capability as False - and a hand-written table that happened to omit
# it would let a parser return UNSUPPORTED from an unrelated header untouched.
ALLOWED_HEADERS = {
    StationVersion: {(0x63, 0x21)},
    StationStatus: {(0x62, 0x22)},
    PagedCvValue: {(0x63, 0x10)},
    CvValue: {(0x63, 0x14), (0x63, 0x15), (0x63, 0x16), (0x63, 0x17), (0x64, 0x14)},
    FunctionState13To28: {(0xE3, 0x52)},
    LocoInfo: {(0xE4, db0) for db0 in range(0x10)},
    EmergencyStopBroadcast: {(0x81, 0x00)},
    GenericAck: {(0x01, 0x04)},
    InterfaceStatus: {(0x01, db0) for db0 in range(256) if db0 != 0x04},
} | {
    cls: {(0x61, db0) for db0, want in EXPECTED_61.items() if want is cls}
    for cls in set(EXPECTED_61.values())
}


def test_parse_only_claims_what_the_header_entitles_it_to():
    """A typed reply may only come from the headers that define it. Inventing a
    CvValue from an unrelated telegram would report a decoder value that no
    decoder ever sent."""
    assert set(ALLOWED_HEADERS) >= set(EXPECTED_61.values())
    for header in range(256):
        for db0 in range(256):
            telegram = valid_telegram(header, db0)
            reply = parse(telegram)
            for reply_type, headers in ALLOWED_HEADERS.items():
                if type(reply) is reply_type:
                    assert len(telegram) >= 2
                    assert (telegram[0], telegram[1]) in headers, telegram.hex(" ")


def test_the_header_61_singletons_are_distinct_objects():
    """The station compares some of these by identity, so two conditions sharing
    an object would make them indistinguishable."""
    from railctl.xbus.replies import HEADER_61_REPLIES

    assert len({id(reply) for reply in HEADER_61_REPLIES.values()}) == len(HEADER_61_REPLIES)


def test_the_loco_info_function_layout_agrees_with_the_command_byte_layout():
    """The reply's FA byte and the E4 20 command byte are the same layout, and
    FB packs group 2 into its low nibble and group 3 into its high nibble. If
    these ever disagree, the station re-asserts a function state it never read."""
    from railctl.xbus.replies import LOCO_INFO_FUNCTION_BITS

    for function in range(0, 5):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G1
        assert LOCO_INFO_FUNCTION_BITS[function] == (0, 1 << bit)
    for function in range(5, 9):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G2
        assert LOCO_INFO_FUNCTION_BITS[function] == (1, 1 << bit)
    for function in range(9, 13):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G3
        assert LOCO_INFO_FUNCTION_BITS[function] == (1, 1 << (bit + 4))
