"""Pinning tests written to kill surviving mutants in `replies.py`.

Provenance: cosmic-ray run of 2026-08-04, 583 mutants, 513 killed by the prior
suite. See docs/test-hardening.md for the full triage.

The centrepiece is `test_the_dispatch_table_matches_the_protocol_documents`,
which walks all 65536 header/db0 combinations rather than sampling them. That
exhaustiveness is the point: the survivors it kills were all header comparisons
weakened from `==` to `>=` or `<=`, and each one only misbehaves for a handful
of specific byte pairs. A generated-input property test can sweep the space, but
it cannot promise to visit `(0x62, 0x22)` - and 0x62 with a db0 of 0x22 is
precisely where "equal to" and "at least" stop agreeing.
"""

from __future__ import annotations

import dataclasses

import pytest

from tools.probe.replies import (
    _EXT_BAND_BASE,
    _HEADER_61_REPLIES,
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

# A body long enough that every length guard in `parse` is satisfied, so this
# test isolates the header dispatch from the truncation handling.
BODY = b"\x00\x01\x02\x03"


def expected_type(header: int, db0: int) -> type:
    """What the protocol documents say this header pair is.

    Written from the specifications, not from `parse`, and deliberately in the
    same order the parser checks them so that a disagreement is a disagreement
    about the protocol rather than about control flow.
    """
    if (header, db0) == (0x63, 0x21):
        return Version
    if (header, db0) == (0x62, 0x22):
        return Status
    if (header, db0) == (0x64, 0x14):
        return CvValue
    if (header, db0) == (0x63, 0x10):
        return RegisterValue
    if header == 0x63 and (db0 == 0x14 or db0 in _EXT_BAND_BASE):
        return CvValue
    if (header, db0) == (0xE3, 0x52):
        return FunctionState13To28
    if header == 0xE4:
        return LocoInfo
    if header == 0x61 and db0 in _HEADER_61_REPLIES:
        return Marker
    if (header, db0) == (0x01, 0x04):
        return Marker
    return Unknown


def test_the_dispatch_table_matches_the_protocol_documents():
    """Every one of the 65536 header pairs, not a sample of them."""
    wrong = []
    for header in range(256):
        for db0 in range(256):
            reply = parse(bytes([header, db0]) + BODY)
            want = expected_type(header, db0)
            if not isinstance(reply, want):
                wrong.append((header, db0, want.__name__, type(reply).__name__))
    assert not wrong, f"{len(wrong)} misparsed pairs, first few: {wrong[:6]}"


def test_no_header_pair_outside_the_table_produces_a_typed_reply():
    """The converse: everything undocumented must land on Unknown.

    A parser that widens silently is how a station's "I could not process that"
    became "I support that" - the reply had a header nobody had listed, and an
    unlisted reply that borrows a neighbour's meaning is worse than one that is
    not understood at all.
    """
    for header in range(256):
        for db0 in range(256):
            if expected_type(header, db0) is Unknown:
                assert isinstance(parse(bytes([header, db0]) + BODY), Unknown)


REPLY_INSTANCES = [
    Version(xpressnet_major=3, xpressnet_minor=6, command_station_id=0x12),
    Status(raw=0x00),
    CvValue(raw_cv=8, value=145, ident=0x14),
    RegisterValue(register=1, value=5),
    LocoInfo(ident=0x04, speed=0, f0=True, f1_f4=0, f5_f12=0),
    FunctionState13To28(f13_f20=0, f21_f28=0),
    Marker("ack"),
    Unknown(telegram=b"\x61\x55"),
]


@pytest.mark.parametrize("reply", REPLY_INSTANCES, ids=lambda r: type(r).__name__)
def test_every_parsed_reply_is_immutable(reply: object):
    """Parsed replies are the evidence a verdict is justified by, and they are
    hex-dumped into the report as an audit trail. A reply that could be edited
    after parsing would let a later stage rewrite what an earlier one saw."""
    field = dataclasses.fields(reply)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(reply, field, None)


@pytest.mark.parametrize(
    ("fa", "f0", "f1_f4"),
    [
        (0x00, False, 0x0),
        (0x01, False, 0x1),  # bit 0 set, bit 4 clear: the pair the mask must not merge
        (0x10, True, 0x0),
        (0x11, True, 0x1),
        (0x1F, True, 0xF),
        (0xEF, False, 0xF),  # bits above the field must not leak into either half
    ],
)
def test_f0_comes_from_bit_4_alone(fa: int, f0: bool, f1_f4: int):
    """F0 and F1-F4 share a byte, and the mask that separates them must not pick
    up a neighbouring bit. Widening 0x10 to 0x11 makes F1 read as the headlight,
    which the probe would then re-assert as F0 - changing the layout it promised
    to leave alone."""
    reply = parse(bytes([0xE4, 0x00, 0x00, fa, 0x00]))
    assert isinstance(reply, LocoInfo)
    assert reply.f0 is f0
    assert reply.f1_f4 == f1_f4


@pytest.mark.parametrize(
    ("band", "raw_cv", "cv"),
    [
        (0x15, 0x00, 256), (0x15, 0xFF, 511),
        (0x16, 0x00, 512), (0x16, 0xFF, 767),
        (0x17, 0x00, 768), (0x17, 0xFF, 1023),
    ],
)
def test_each_band_decodes_at_both_of_its_ends(band: int, raw_cv: int, cv: int):
    reply = parse(bytes([0x63, band, raw_cv, 0x00]))
    assert isinstance(reply, CvValue)
    assert reply.cv == cv
