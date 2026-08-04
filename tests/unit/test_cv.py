"""CV number conversions.

Four conventions live in one module, and they do not agree:

    POM      (E6 30)      ZERO-based   wire = cv - 1
    Z21      (23 11)      ZERO-based   wire = cv - 1
    direct   (22 15)      ONE-based    wire = cv, 1..255
    extended (22 18..1B)  ONE-based    band opcode + (cv & 0xFF)

Measured on the YD7010 (docs/probe-results.md): `23 11 00 07` reads CV8, and the
answer comes back as `63 14 08`. The request is zero-based and the echo is
one-based, on the same exchange. Routing a service-mode opcode through the
zero-based rule reads the CV next door and reports the value under the right
name - nothing in the output looks wrong.

No web summary states this correctly. These tables come from the hardware and
from Lenz 23151, and they are the reason this module exists at all.
"""

from __future__ import annotations

import pytest

from railctl.errors import CvOutOfRangeError, ProgrammingError, exit_code_for
from railctl.xbus import cv as cvmod
from railctl.xbus import dialect
from railctl.xbus.codec import encode
from railctl.xbus.cv import (
    CV_FOR_PAGE0_ZERO,
    EXT_READ_OPCODES,
    MAX_CV_DIRECT,
    MAX_CV_POM,
    MAX_CV_Z21,
    CvEncoding,
    decode_echo,
    direct_cv_byte,
    echo_candidates,
    ext_cv_fields,
    join_cv_field,
    pom_cv_fields,
    resolve_service_cv,
    z21_cv_fields,
)

POM_VECTORS = [
    (1, (0, 0x00)),
    (8, (0, 0x07)),
    (29, (0, 0x1C)),
    (255, (0, 0xFE)),
    (256, (0, 0xFF)),
    (257, (1, 0x00)),
    (265, (1, 0x08)),
    (1024, (3, 0xFF)),
]

DIRECT_VECTORS = [(1, 1), (8, 8), (29, 29), (255, 255)]

EXT_VECTORS = [
    (1, (0, 0x01)),
    (8, (0, 0x08)),
    (255, (0, 0xFF)),
    (256, (1, 0x00)),
    (257, (1, 0x01)),
    (265, (1, 0x09)),
    (511, (1, 0xFF)),
    (512, (2, 0x00)),
    (767, (2, 0xFF)),
    (768, (3, 0x00)),
    (1023, (3, 0xFF)),
    (1024, (0, 0x00)),
]

Z21_VECTORS = [
    (1, (0, 0x00)),
    (8, (0, 0x07)),
    (29, (0, 0x1C)),
    (256, (0, 0xFF)),
    (265, (1, 0x08)),
    (1024, (3, 0xFF)),
]


def test_the_cv_encoding_enum_is_the_dialect_one():
    """One enum, two import paths. Two enums would compare unequal in silence."""
    assert CvEncoding is dialect.CvEncoding


@pytest.mark.parametrize(("cv", "expected"), POM_VECTORS)
def test_pom_fields_are_zero_based(cv: int, expected: tuple[int, int]):
    assert pom_cv_fields(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), DIRECT_VECTORS)
def test_the_direct_byte_is_one_based(cv: int, expected: int):
    assert direct_cv_byte(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), EXT_VECTORS)
def test_extended_fields_are_one_based_and_band_relative(cv: int, expected: tuple[int, int]):
    assert ext_cv_fields(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), Z21_VECTORS)
def test_z21_fields_are_zero_based_across_sixteen_bits(cv: int, expected: tuple[int, int]):
    assert z21_cv_fields(cv) == expected


def test_the_two_families_disagree_by_exactly_one_for_every_cv():
    """The property the probe pinned, here exhaustive rather than sampled.

    1024 iterations is cheaper than a hypothesis run and visits every boundary,
    including the two awkward ones: CV256, the first CV of band 1, and CV1024,
    which rides in band 0's vacant slot 0.
    """
    for cv in range(1, MAX_CV_POM + 1):
        zero_based = join_cv_field(*pom_cv_fields(cv))
        assert zero_based == join_cv_field(*z21_cv_fields(cv))
        assert zero_based == cv - 1
        page, c = ext_cv_fields(cv)
        one_based = CV_FOR_PAGE0_ZERO if (page, c) == (0, 0) else 256 * page + c
        assert one_based == cv
        assert one_based - zero_based == 1
        if cv <= MAX_CV_DIRECT:
            assert direct_cv_byte(cv) - zero_based == 1


@pytest.mark.parametrize(
    ("func", "cv"),
    [
        (pom_cv_fields, 0),
        (pom_cv_fields, -1),
        (pom_cv_fields, 1025),
        (direct_cv_byte, 0),
        (direct_cv_byte, 256),
        (direct_cv_byte, 1025),
        (ext_cv_fields, 0),
        (ext_cv_fields, -1),
        (ext_cv_fields, 1025),
        (z21_cv_fields, 0),
        (z21_cv_fields, -1),
        (z21_cv_fields, 1025),
    ],
)
def test_every_encoder_refuses_a_cv_outside_its_own_range(func, cv: int):
    """CvOutOfRangeError, not a bare ValueError.

    `railctl cv read 1025` has to exit with the documented `railctl/error/v1`
    envelope and exit code 15. A ValueError is not a RailctlError, so
    `exit_code_for` cannot map it and the command exits 1 with a traceback.
    """
    with pytest.raises(CvOutOfRangeError, match="CV") as excinfo:
        func(cv)
    assert excinfo.value.cv == cv


def test_a_cv_range_fault_carries_the_programming_error_exit_code():
    """The exit code reserved in M2 must actually be reachable from here."""
    with pytest.raises(CvOutOfRangeError) as excinfo:
        z21_cv_fields(1025)
    assert isinstance(excinfo.value, ProgrammingError)
    assert exit_code_for(excinfo.value) == 15


def test_cv256_is_refused_on_the_direct_opcode_and_says_why():
    """From station version 3.6 a bare C = 0 addresses CV1024, not CV256.

    The YD7010 reports 4.0, so sending C = 0 here would read a different CV and
    report it under the name of CV256. MAX_CV_DIRECT is 255 for that reason;
    CV256 and above go out on the extended or Z21 opcodes.
    """
    assert MAX_CV_DIRECT == 255
    with pytest.raises(CvOutOfRangeError, match="1024") as excinfo:
        direct_cv_byte(256)
    assert excinfo.value.cv == 256


@pytest.mark.parametrize(
    ("encoding", "raw", "page_index", "zero_based", "expected"),
    [
        (CvEncoding.POM_ZERO_BASED, 0, None, True, 1),
        (CvEncoding.POM_ZERO_BASED, 7, None, True, 8),
        (CvEncoding.POM_ZERO_BASED, 1023, None, True, 1024),
        (CvEncoding.POM_ZERO_BASED, 1, None, False, 1),
        (CvEncoding.POM_ZERO_BASED, 8, None, False, 8),
        (CvEncoding.POM_ZERO_BASED, 1024, None, False, 1024),
        (CvEncoding.Z21_16BIT, 7, None, None, 8),
        (CvEncoding.SERVICE_DIRECT, 1, None, None, 1),
        (CvEncoding.SERVICE_DIRECT, 255, None, None, 255),
        (CvEncoding.SERVICE_EXT, 0, 0, None, 1024),
        (CvEncoding.SERVICE_EXT, 1, 0, None, 1),
        (CvEncoding.SERVICE_EXT, 0, 1, None, 256),
        (CvEncoding.SERVICE_EXT, 9, 1, None, 265),
        (CvEncoding.SERVICE_EXT, 0, 2, None, 512),
        (CvEncoding.SERVICE_EXT, 0, 3, None, 768),
    ],
)
def test_decode_echo_inverts_each_encoding(
    encoding: CvEncoding,
    raw: int,
    page_index: int | None,
    zero_based: bool | None,
    expected: int,
):
    """The extended inverse is NOT `raw or 256`.

    That fudge belongs to the legacy direct opcode. Used here it decodes CV256
    as 512, CV512 as 768 and CV768 as 1024 - three CVs a ZIMO backup touches,
    each silently wrong. `page_index` is supplied by the caller from the request
    it issued, because the reply alone cannot say which band it came from.
    `zero_based` is supplied for POM because its echo convention is unmeasured;
    Z21's is measured, so it takes neither keyword.
    """
    assert decode_echo(encoding, raw, page_index=page_index, zero_based=zero_based) == expected


def test_decode_echo_refuses_a_zero_on_the_direct_opcode():
    with pytest.raises(ValueError, match="raw 0"):
        decode_echo(CvEncoding.SERVICE_DIRECT, 0)


def test_decode_echo_refuses_pom_when_the_echo_convention_is_unstated():
    """`zero_based=None` (the default) must not guess.

    No POM reply has ever been observed on this hardware (docs/probe-results.md,
    R1), so decoding one always requires the caller to say which convention
    applies. Guessing would risk decoding the first genuine reply under the
    wrong CV number - silently, since the wrong CV reads back fine under the
    right name.
    """
    with pytest.raises(ValueError, match="pom_echo_zero_based"):
        decode_echo(CvEncoding.POM_ZERO_BASED, 7)


@pytest.mark.parametrize(
    ("encoding", "raw"),
    [
        (CvEncoding.Z21_16BIT, 7),
        (CvEncoding.SERVICE_DIRECT, 1),
        (CvEncoding.SERVICE_EXT, 1),
    ],
)
def test_decode_echo_refuses_zero_based_for_an_encoding_with_no_unmeasured_convention(
    encoding: CvEncoding, raw: int
):
    """`zero_based` means something only for POM. Passing it anywhere else must
    raise rather than be silently ignored - an ignored argument is how the next
    reader learns the wrong lesson about what it controls."""
    with pytest.raises(ValueError, match="zero_based"):
        decode_echo(encoding, raw, zero_based=True)


@pytest.mark.parametrize(
    ("encoding", "raw"),
    [
        (CvEncoding.POM_ZERO_BASED, 7),
        (CvEncoding.Z21_16BIT, 7),
        (CvEncoding.SERVICE_DIRECT, 1),
    ],
)
def test_decode_echo_refuses_page_index_for_an_encoding_with_no_band(
    encoding: CvEncoding, raw: int
):
    """`page_index` means something only for SERVICE_EXT, the one encoding whose
    echo byte carries no band of its own. Passing it elsewhere must raise rather
    than be silently ignored."""
    with pytest.raises(ValueError, match="page_index"):
        decode_echo(encoding, raw, page_index=0)


def test_decode_echo_refuses_service_ext_without_a_page_index():
    """`page_index=None` (the default) must not assume band 0.

    The echo byte cannot carry its own band; a caller who forgets `page_index`
    would silently decode a band-1..3 echo as band 0 - off by 256, 512 or 768,
    with no error. CV265 and CV266, the ZIMO sound-project and master-volume
    CVs this tool backs up, sit in band 1.
    """
    with pytest.raises(ValueError, match="page_index"):
        decode_echo(CvEncoding.SERVICE_EXT, 9)


@pytest.mark.parametrize(
    ("encoding", "raw", "zero_based"),
    [
        (CvEncoding.POM_ZERO_BASED, 1024, True),
        (CvEncoding.POM_ZERO_BASED, 5000, True),
        (CvEncoding.POM_ZERO_BASED, 1025, False),
        (CvEncoding.Z21_16BIT, 1024, None),
        (CvEncoding.Z21_16BIT, 0xFFFF, None),
    ],
)
def test_decode_echo_refuses_a_wire_cv_past_the_encoding_maximum(
    encoding: CvEncoding, raw: int, zero_based: bool | None
):
    """The inverse is bounded by CV space, not by the width of the field.

    A 16-bit field holds 65536 values; POM and Z21 address 1024 CVs. Without
    this bound `decode_echo(POM_ZERO_BASED, 5000, zero_based=True)` returns 5001
    - a CV number outside every valid range, handed to the station layer as a
    legitimate result. Every other function in this module range-checks; this
    one must too, or it fabricates a plausible CV out of garbage, which is
    exactly the "wrong value under the right name" failure the module exists to
    prevent. `zero_based=False` gets the same bound from the other side: raw is
    already read as the 1-based CV, so 1025 is past MAX_CV_POM the same way.
    """
    with pytest.raises(ValueError, match="not a wire CV"):
        decode_echo(encoding, raw, zero_based=zero_based)


def test_decode_echo_accepts_the_last_valid_wire_cv_of_each_encoding():
    """The bound is inclusive at 1023 -> CV1024, one below the field maximum."""
    assert decode_echo(CvEncoding.POM_ZERO_BASED, MAX_CV_POM - 1, zero_based=True) == MAX_CV_POM
    assert decode_echo(CvEncoding.Z21_16BIT, MAX_CV_Z21 - 1) == MAX_CV_Z21


@pytest.mark.parametrize(
    ("reply_ident", "c", "expected"),
    [
        (0x14, 0, 1024),
        (0x14, 1, 1),
        (0x14, 8, 8),
        (0x14, 255, 255),
        (0x15, 0, 256),
        (0x15, 9, 265),
        (0x16, 0, 512),
        (0x17, 0, 768),
    ],
)
def test_resolve_service_cv_matches_the_measured_replies(reply_ident: int, c: int, expected: int):
    """`63 14 08` answered a read of CV8; `63 15 09` answered CV265.

    Lenz 23151 section 3.1.2.6: on `63 14`, C = 0 means CV1024 and C = 1..255
    means CV1..255. Not 0xFF for CV1024 - a plausible-sounding claim the document
    contradicts.
    """
    assert resolve_service_cv(reply_ident, c) == expected


def test_resolve_service_cv_refuses_an_unknown_ident_or_a_non_byte():
    with pytest.raises(ValueError, match="ident"):
        resolve_service_cv(0x13, 0)
    with pytest.raises(ValueError, match="not a byte"):
        resolve_service_cv(0x14, 256)


@pytest.mark.parametrize(
    ("encoding", "cv", "zero_based", "expected"),
    [
        (CvEncoding.POM_ZERO_BASED, 8, None, {7, 8}),
        (CvEncoding.POM_ZERO_BASED, 8, True, {7}),
        (CvEncoding.POM_ZERO_BASED, 8, False, {8}),
        (CvEncoding.POM_ZERO_BASED, 256, None, {255, 0}),
        (CvEncoding.SERVICE_DIRECT, 8, None, {8}),
        (CvEncoding.SERVICE_EXT, 265, None, {9}),
        (CvEncoding.SERVICE_EXT, 1024, None, {0}),
        (CvEncoding.Z21_16BIT, 8, None, {8}),
    ],
)
def test_echo_candidates_covers_the_forms_the_station_may_answer_with(
    encoding: CvEncoding, cv: int, zero_based: bool | None, expected: set[int]
):
    assert echo_candidates(encoding, cv, zero_based=zero_based) == frozenset(expected)


def test_a_z21_read_is_matched_against_the_one_based_echo_that_was_measured():
    """The request is zero-based, the echo is one-based. Both were measured.

    `23 11 00 07` -> `63 14 08` (CV8), `23 11 01 08` -> `63 15 09` (CV265).
    Matching a Z21 reply against the byte the *request* carried would reject
    every real answer, and a rejected answer is indistinguishable from silence -
    which is exactly how M1 concluded that the Lenz opcode family did not work.
    """
    assert echo_candidates(CvEncoding.Z21_16BIT, 8) == frozenset({8})
    assert echo_candidates(CvEncoding.Z21_16BIT, 29) == frozenset({29})
    assert echo_candidates(CvEncoding.Z21_16BIT, 250) == frozenset({250})
    assert echo_candidates(CvEncoding.Z21_16BIT, 265) == frozenset({9})
    assert echo_candidates(CvEncoding.Z21_16BIT, 266) == frozenset({10})
    # A `63 14..17` reply is resolved by band, never by decode_echo: the same
    # byte 8 means CV8 through resolve_service_cv and CV9 through the 16-bit
    # inverse, and only the first matches the hardware.
    assert resolve_service_cv(0x14, 8) == 8
    assert decode_echo(CvEncoding.Z21_16BIT, 8) == 9


def test_echo_candidates_names_z21_not_extended_on_an_out_of_range_cv():
    """A Z21 CV is bounded by MAX_CV_Z21, not by the extended encoding's bound.

    Both encodings share the same band arithmetic, so the two bounds cover the
    same CVs in practice - but the diagnostic must still say Z21 when a Z21
    caller passes a bad CV, not "extended", or the error points whoever reads it
    at the wrong opcode family entirely.
    """
    with pytest.raises(CvOutOfRangeError, match="Z21") as excinfo:
        echo_candidates(CvEncoding.Z21_16BIT, 0)
    assert excinfo.value.cv == 0
    with pytest.raises(CvOutOfRangeError, match="Z21") as excinfo:
        echo_candidates(CvEncoding.Z21_16BIT, 1025)
    assert excinfo.value.cv == 1025


def test_echo_candidates_alone_cannot_separate_two_cvs_in_different_bands():
    """The candidate byte narrows WITHIN a band. It does not identify the band.

    Pinned so that nobody reads `echo_candidates` as a complete matcher. The
    hardware separates these two exchanges only by the reply ident
    (docs/probe-results.md lines 148-152): `23 11 00 07` (CV8) is answered
    `63 14 08`, and `23 11 01 08` (CV265) is answered `63 15 09`. A matcher that
    compares the C byte alone accepts a `63 14 09` - which is CV9 - as the answer
    to a CV265 request and reports CV9's value under the name CV265. CV265 and
    CV266 are the ZIMO sound-project and master-volume CVs this tool backs up.
    """
    assert echo_candidates(CvEncoding.Z21_16BIT, 265) == echo_candidates(CvEncoding.Z21_16BIT, 9)
    assert echo_candidates(CvEncoding.POM_ZERO_BASED, 265) == echo_candidates(
        CvEncoding.POM_ZERO_BASED, 9
    )
    # The band is what separates them, and only resolve_service_cv reads it.
    assert resolve_service_cv(0x15, 9) == 265
    assert resolve_service_cv(0x14, 9) == 9


def test_join_cv_field_rebuilds_a_sixteen_bit_wire_value():
    assert join_cv_field(0x01, 0x08) == 264
    with pytest.raises(ValueError, match="not a byte"):
        join_cv_field(256, 0)


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\x22\x15\x01\x36"),
        (255, b"\x22\x15\xff\xc8"),
    ],
)
def test_the_direct_read_golden_telegrams(cv: int, telegram: bytes):
    assert encode(0x22, 0x15, direct_cv_byte(cv)) == telegram


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\x22\x18\x01\x3b"),
        (256, b"\x22\x19\x00\x3b"),
        (1024, b"\x22\x18\x00\x3a"),
    ],
)
def test_the_extended_read_golden_telegrams(cv: int, telegram: bytes):
    """`22 18 00` is CV1024, not CV256. `22 19 00` is CV256."""
    page, c = ext_cv_fields(cv)
    assert encode(0x22, EXT_READ_OPCODES[page], c) == telegram


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\xe6\x30\x00\x03\xe4\x00\x00\x31"),
        (8, b"\xe6\x30\x00\x03\xe4\x07\x00\x36"),
        (257, b"\xe6\x30\x00\x03\xe5\x00\x00\x30"),
    ],
)
def test_the_pom_read_golden_telegrams(cv: int, telegram: bytes):
    """CV8 goes out as 07: POM is zero-based. CV257 pushes MM into the option byte."""
    mm, lsb = pom_cv_fields(cv)
    assert encode(0xE6, 0x30, 0x00, 0x03, 0xE4 | mm, lsb, 0x00) == telegram


def test_the_z21_read_golden_telegram():
    msb, lsb = z21_cv_fields(29)
    assert encode(0x23, 0x11, msb, lsb) == b"\x23\x11\x00\x1c\x2e"
    assert MAX_CV_Z21 == 1024
    assert cvmod.MAX_CV_POM == 1024
