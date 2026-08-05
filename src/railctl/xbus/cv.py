# src/railctl/xbus/cv.py
"""CV number conversions - the single choke point.

Every function here takes a 1-based user CV number. No function anywhere else in
railctl accepts or produces a wire CV address; the layering test greps
`station/`, `cli/` and `xbus/commands.py` for `cv - 1`, `cv + 1`, `% 256`,
`>> 8` and `<< 8`, and this module is the one place they are allowed.

Four conventions, measured on the YaMoRC YD7010 with a ZIMO MS450P22
(docs/probe-results.md):

    Encoding        Wire formula                              Valid CV
    POM_ZERO_BASED  w = cv - 1; MM = w >> 8; LSB = w & 0xFF   1..1024
    SERVICE_DIRECT  byte = cv                                 1..255
    SERVICE_EXT     page = cv // 256; C = cv & 0xFF           1..1023 (+1024 -> 0, 0)
    Z21_16BIT       w = cv - 1; MSB = w >> 8; LSB = w & 0xFF   1..1024

The zero-based rule applies to POM and Z21 ONLY. `23 11 00 07` reads CV8; its
answer comes back as `63 14 08`. The request is zero-based, the echo one-based,
on the same exchange, and no web summary of the protocol states this correctly.

Getting it wrong is silent. The wrong CV is read and its value is reported under
the right name; once a write path exists, the same mistake writes to the wrong
CV.

Two exception families live here, split by whose mistake it is:

* a bad 1-based USER CV number (CV0, CV1025, CV256 on the direct opcodes) raises
  `CvOutOfRangeError`. That value came from the command line, so it needs the
  stable `code` in the `railctl/error/v1` envelope and exit code 15;
* a bad WIRE value (a non-byte echo, an unknown reply ident, a page index
  outside 0..3) raises plain `ValueError`. Only code inside this repo can pass
  one, and M2's rule is that internal argument validation is a `ValueError`,
  which `cli/_errors.py` reports as usage exit code 2.
"""

from __future__ import annotations

from railctl.errors import CvOutOfRangeError
from railctl.xbus.dialect import CvEncoding

__all__ = [
    "CV_FOR_PAGE0_ZERO",
    "CV_MIN",
    "EXT_PAGE_SIZE",
    "EXT_READ_OPCODES",
    "EXT_WRITE_OPCODES",
    "MAX_CV_DIRECT",
    "MAX_CV_EXT",
    "MAX_CV_POM",
    "MAX_CV_Z21",
    "POM_CV_MIN",
    "SERVICE_RESULT_IDENTS",
    "SERVICE_RESULT_IDENT_BASE",
    "CvEncoding",
    "decode_echo",
    "direct_cv_byte",
    "echo_candidates",
    "ext_cv_fields",
    "join_cv_field",
    "pom_cv_fields",
    "resolve_service_cv",
    "result_ident_for",
    "z21_cv_fields",
]

CV_MIN = 1
POM_CV_MIN = CV_MIN  # the design names this one explicitly; same value

MAX_CV_POM = 1024
# 255, not 256. Lenz 23151 sections 3.2.6 and 3.2.14: from station version 3.6
# onward a C of 0 on the legacy direct opcodes addresses CV1024, not CV256. The
# YD7010 reports 4.0, so sending C = 0 would touch the wrong CV with no error.
MAX_CV_DIRECT = 255
MAX_CV_EXT = 1024
MAX_CV_Z21 = 1024

EXT_READ_OPCODES = (0x18, 0x19, 0x1A, 0x1B)
EXT_WRITE_OPCODES = (0x1C, 0x1D, 0x1E, 0x1F)
EXT_PAGE_SIZE = 256
CV_FOR_PAGE0_ZERO = 1024

# `63 14/15/16/17 C V` - the service-mode result bands (Lenz 23151, 3.1.2.6).
SERVICE_RESULT_IDENT_BASE = 0x14
SERVICE_RESULT_IDENTS = (0x14, 0x15, 0x16, 0x17)

_BYTE_MIN = 0
_BYTE_MAX = 255


def _check_range(cv: int, maximum: int, what: str) -> None:
    """Guard a 1-based USER CV number.

    CvOutOfRangeError rather than ValueError: this value comes from the command
    line, and only a RailctlError reaches `exit_code_for`, which maps this class
    to exit code 15. A bare ValueError would exit 1 with a traceback instead of
    the documented `railctl/error/v1` envelope, and code 15 - reserved in M2 -
    would never be produced by anything.
    """
    if not CV_MIN <= cv <= maximum:
        raise CvOutOfRangeError(f"CV {cv} outside the {what} range {CV_MIN}..{maximum}", cv=cv)


def _check_byte(value: int, what: str) -> None:
    """Guard a WIRE byte. ValueError: only in-repo code can pass a non-byte."""
    if not _BYTE_MIN <= value <= _BYTE_MAX:
        raise ValueError(f"{what} {value} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")


def pom_cv_fields(cv: int) -> tuple[int, int]:
    """POM (E6 30) is ZERO-based: CV1 goes on the wire as 0.

    Returns `(MM, LSB)`. MM is the top two bits of the zero-based value and is
    OR-ed into the option byte by the caller: `0xE4 | MM`.
    """
    _check_range(cv, MAX_CV_POM, "POM")
    wire = cv - 1
    return (wire >> 8) & 0x03, wire & 0xFF


def direct_cv_byte(cv: int) -> int:
    """Legacy direct (22 15 / 23 16) is ONE-based: CV1 goes on the wire as 1.

    Routing this through the zero-based rule reads the CV next door. CV256 and
    above are refused: from station version 3.6 a C of 0 means CV1024 here.
    """
    if cv == MAX_CV_DIRECT + 1:
        raise CvOutOfRangeError(
            f"CV {cv} cannot be addressed with the direct opcodes: from station "
            f"version 3.6 a C of 0 means CV1024, not CV256. Use the extended or "
            f"Z21 opcodes.",
            cv=cv,
        )
    _check_range(cv, MAX_CV_DIRECT, "direct")
    return cv


def _band_fields(cv: int) -> tuple[int, int]:
    """`(page, C)` - the band arithmetic the extended encoding and the Z21 echo
    share, factored out so there is exactly one place a CV becomes a band byte.

    CV1024 rides band 0's vacant slot 0, so it stays reachable even though
    `cv // 256` would put it past the last band. Bands 1..3 are 256 wide and
    aligned, so `cv & 0xFF` is exactly `cv - 256 * page` for them, and the
    identity for band 0.

    This does NOT range-check `cv`: each caller has already checked it against
    its own valid CV space, because that space differs between them (the
    extended encoding excepts CV1024 from its normal 1..1023 range; Z21 does
    not need the exception, since its range is 1..1024 outright). Folding a
    range check in here would force one caller's bound onto the other.
    """
    if cv == CV_FOR_PAGE0_ZERO:
        return 0, 0
    return cv // EXT_PAGE_SIZE, cv & 0xFF


def ext_cv_fields(cv: int) -> tuple[int, int]:
    """Extended (22 18..1B / 23 1C..1F) is ONE-based and band-relative.

    Returns `(page_index, C)`, where the opcode is `EXT_READ_OPCODES[page_index]`
    or `EXT_WRITE_OPCODES[page_index]`:

        page 0  CV1..255 at their own numbers, and CV1024 at C = 0
        page 1  CV256..511    page 2  CV512..767    page 3  CV768..1023

    Bands 1..3 are 256 wide and aligned, so `cv & 0xFF` is exactly
    `cv - 256 * page` for them, and the identity for band 0.
    """
    if cv != CV_FOR_PAGE0_ZERO:
        _check_range(cv, MAX_CV_EXT - 1, f"extended (CV{CV_FOR_PAGE0_ZERO} excepted)")
    return _band_fields(cv)


def z21_cv_fields(cv: int) -> tuple[int, int]:
    """Z21 (23 11 / 24 12) is ZERO-based across a full 16-bit CV field."""
    _check_range(cv, MAX_CV_Z21, "Z21")
    wire = cv - 1
    return (wire >> 8) & 0xFF, wire & 0xFF


def join_cv_field(msb: int, lsb: int) -> int:
    """Rebuild the 16-bit wire CV of a `64 14` reply from its two bytes."""
    _check_byte(msb, "CV MSB")
    _check_byte(lsb, "CV LSB")
    return (msb << 8) | lsb


def _decode_zero_based_echo(raw: int, limit: int) -> int:
    """The zero-based inverse shared by POM (once its convention is stated) and
    Z21 (always, since Z21's is measured).

    Bounded by `limit`, the CV space of the encoding calling this - not by the
    width of the 16-bit wire field. See `decode_echo` for why an inverse bounded
    only by the field is dangerous.
    """
    if not 0 <= raw <= limit - 1:
        raise ValueError(f"echo {raw} is not a wire CV in 0..{limit - 1}")
    return raw + 1


def decode_echo(
    encoding: CvEncoding,
    raw: int,
    *,
    page_index: int | None = None,
    zero_based: bool | None = None,
) -> int:
    """Turn the CV a reply echoed back into a 1-based CV number.

    `raw` is the 16-bit wire field for POM and Z21 (see `join_cv_field`) and the
    single C byte for the direct and extended opcodes.

    `zero_based` applies ONLY to `POM_ZERO_BASED`, the one encoding whose echo
    convention `echo_candidates` already documents as unmeasured on this
    hardware (docs/probe-results.md, R1 - no POM reply has ever come back):

    * `zero_based=True` - the existing behaviour: `raw` must be a zero-based
      wire value in `0..MAX_CV_POM - 1`, and the answer is `raw + 1`.
    * `zero_based=False` - the one-based reading: `raw` must already be the
      1-based CV, in `CV_MIN..MAX_CV_POM`, and the answer is `raw` itself.
    * `zero_based=None` (the default) raises, rather than guessing which
      reading applies - a guessed convention could silently decode the first
      genuine POM reply under the wrong CV number, which is the exact
      "wrong value under the right name" failure this module exists to
      prevent. `Capabilities.pom_echo_zero_based` is where the real answer
      comes from once a POM reply is finally observed.

    `Z21_16BIT` is NOT affected by `zero_based`: its echo is the measured
    one-based band byte (see `echo_candidates`), never a guess.

    `page_index` applies ONLY to `SERVICE_EXT`, because that is the one
    encoding whose echo byte cannot carry its own band - the band comes from
    the request the caller issued, and defaulting it to band 0 would silently
    decode a band-1..3 echo as band 0: off by 256, 512 or 768, with no error.
    `page_index=None` (the default) raises rather than assuming band 0.

    Passing `zero_based` to any encoding but `POM_ZERO_BASED`, or `page_index`
    to any encoding but `SERVICE_EXT`, raises `ValueError` rather than being
    silently ignored: a parameter that does nothing is how the next reader
    learns the wrong lesson about what it controls.

    The extended inverse is NOT `raw or 256`. That fudge belongs to the legacy
    direct opcode; used here it decodes CV256 as 512, CV512 as 768 and CV768 as
    1024. `page_index` comes from the request the caller issued, because the
    reply on its own cannot say which band it belongs to.

    Every branch is bounded by the CV space of its own encoding, not by the width
    of the wire field. The 16-bit field holds 65536 values; POM and Z21 address
    1024 CVs. An inverse bounded only by the field would turn `raw = 5000` into
    CV5001 - a number outside every valid range - and hand it to the station
    layer as a legitimate result, which is the "wrong value under the right name"
    failure this whole module exists to prevent.
    """
    if encoding is CvEncoding.POM_ZERO_BASED:
        if page_index is not None:
            raise ValueError("page_index has no meaning for POM_ZERO_BASED; omit it")
        if zero_based is None:
            raise ValueError(
                "the POM echo convention is not established on this hardware: no "
                "POM reply has ever been observed (docs/probe-results.md, R1), so "
                "the caller must state zero_based explicitly. "
                "Capabilities.pom_echo_zero_based is where that answer comes from "
                "once a real reply is seen."
            )
        if zero_based:
            return _decode_zero_based_echo(raw, MAX_CV_POM)
        if not CV_MIN <= raw <= MAX_CV_POM:
            raise ValueError(f"echo {raw} is not a wire CV in {CV_MIN}..{MAX_CV_POM}")
        return raw
    if zero_based is not None:
        raise ValueError(
            f"zero_based has no meaning for {encoding.name}; only "
            f"POM_ZERO_BASED's echo convention is unmeasured"
        )
    if encoding is CvEncoding.Z21_16BIT:
        if page_index is not None:
            raise ValueError("page_index has no meaning for Z21_16BIT; omit it")
        return _decode_zero_based_echo(raw, MAX_CV_Z21)
    _check_byte(raw, "echo")
    if encoding is CvEncoding.SERVICE_DIRECT:
        if page_index is not None:
            raise ValueError("page_index has no meaning for SERVICE_DIRECT; omit it")
        if raw == 0:
            raise ValueError("raw 0 is not a direct-mode CV echo")
        return raw
    if page_index is None:
        raise ValueError(
            "SERVICE_EXT cannot decode an echo without page_index: the echo byte "
            "carries no band of its own, so a caller who forgets it decodes a "
            "band-1..3 echo as band 0 - off by 256, 512 or 768, with no error. "
            "Pass the page_index the request itself carried."
        )
    if not 0 <= page_index < len(EXT_READ_OPCODES):
        raise ValueError(f"page index {page_index} outside 0..{len(EXT_READ_OPCODES) - 1}")
    if page_index == 0 and raw == 0:
        return CV_FOR_PAGE0_ZERO
    return EXT_PAGE_SIZE * page_index + raw


def echo_candidates(
    encoding: CvEncoding, cv: int, *, zero_based: bool | None = None
) -> frozenset[int]:
    """Every echo byte that could legitimately answer a request for `cv`.

    This exists so that no comparison logic anywhere else has to do CV
    arithmetic. It is NOT a complete matcher, and the caller must not use it as
    one.

    **The returned byte narrows only WITHIN one band; it cannot separate bands.**
    Two CVs 256 apart share a candidate set: `echo_candidates(Z21_16BIT, 265)`
    and `echo_candidates(Z21_16BIT, 9)` are both `{9}`, and the POM pair is both
    `{8, 9}`. A `63 14..17` reply MUST therefore be resolved with
    `resolve_service_cv(reply_ident, c)` first, because the ident is the only
    thing that carries the band: measured on the hardware,
    `23 11 00 07` (CV8) is answered `63 14 08` and `23 11 01 08` (CV265) is
    answered `63 15 09` (docs/probe-results.md). A matcher that compares the C
    byte alone accepts a `63 14 09` - CV9 - as the answer to a CV265 request and
    reports CV9's value under the name CV265. CV265 and CV266 are the ZIMO
    sound-project and master-volume CVs this tool backs up.

    For POM the station's echo convention is not settled on this hardware - no
    POM result has ever come back (docs/probe-results.md, R1) - so `None` returns
    BOTH forms and lets `Capabilities.pom_echo_zero_based` narrow it once a real
    reply is seen. A matcher that guessed one form would drop the first genuine
    reply, and a dropped reply reads as silence, which reads as "unsupported".

    For the service-mode encodings the echo is the ONE-based band byte, measured:
    `23 11 00 07` (CV8) is answered `63 14 08`, and `23 11 01 08` (CV265) is
    answered `63 15 09`. That holds for Z21 requests too, even though the request
    itself is zero-based.
    """
    if encoding is CvEncoding.POM_ZERO_BASED:
        _check_range(cv, MAX_CV_POM, "POM")
        zero_form = (cv - 1) & 0xFF
        one_form = cv & 0xFF
        if zero_based is True:
            return frozenset({zero_form})
        if zero_based is False:
            return frozenset({one_form})
        return frozenset({zero_form, one_form})
    if encoding is CvEncoding.SERVICE_DIRECT:
        return frozenset({direct_cv_byte(cv)})
    if encoding is CvEncoding.Z21_16BIT:
        _check_range(cv, MAX_CV_Z21, "Z21")
        return frozenset({_band_fields(cv)[1]})
    return frozenset({ext_cv_fields(cv)[1]})


def result_ident_for(cv: int, encoding: CvEncoding) -> int:
    """The `63 14..17` ident the station uses when it answers about `cv`.

    `SERVICE_RESULT_IDENT_BASE + ext_cv_fields(cv)[0]`. Measured: CV8 -> 0x14,
    CV265 -> 0x15 (docs/probe-results.md). The band a CV answers on is a
    property of the CV's own page, not of which opcode requested it - the
    station answers CV8 on `63 14` whether it was asked for through POM, the
    legacy direct opcode, or the extended opcodes - so `encoding` plays no part
    in the arithmetic. It IS used to apply the bound the caller's own encoding
    actually supports before doing that arithmetic: `SERVICE_DIRECT` tops out
    at CV255, and routing a CV300 request through here without that check would
    accept it via `ext_cv_fields`'s wider bound (which reaches 1023), silently
    promising a band the direct opcode family can never produce.

    Exists so that `station/` never has to add `SERVICE_RESULT_IDENT_BASE` to a
    band index itself - the layering test forbids CV arithmetic there, and this
    function is the whole reason `CvMatcher` can check the ident band without
    it.
    """
    if encoding is CvEncoding.SERVICE_DIRECT:
        direct_cv_byte(cv)  # validates 1..255 with the CV256 message; result unused
    return SERVICE_RESULT_IDENT_BASE + ext_cv_fields(cv)[0]


def resolve_service_cv(reply_ident: int, c: int) -> int:
    """`63 14..17 C V` -> the 1-based CV the station answered about.

    Lenz 23151 section 3.1.2.6: on `63 14`, C = 0 means CV1024 and C = 1..255
    means CV1..255; `63 15/16/17` carry 256/512/768 plus C. Measured: `63 15 09`
    is CV265, which is where the ZIMO CVs this tool backs up begin.
    """
    if reply_ident not in SERVICE_RESULT_IDENTS:
        raise ValueError(f"reply ident 0x{reply_ident:02X} is not a service-result ident")
    _check_byte(c, "C")
    page = reply_ident - SERVICE_RESULT_IDENT_BASE
    if page == 0 and c == 0:
        return CV_FOR_PAGE0_ZERO
    return EXT_PAGE_SIZE * page + c
