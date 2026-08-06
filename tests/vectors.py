"""Golden byte vectors, as named constants.

Every row here is copied verbatim from the design document, with the one-line
reason that document gives for why the row exists. These are the top bug sources
of this protocol: the dialect divergence band, the three different CV encodings,
and the four reply forms that must stay distinguishable.

Keeping them in one table means an intentional encoder change is one reviewed
edit rather than a dozen scattered literals - and the two self-consistency tests
in tests/unit/test_xbus_vectors.py check the table itself before anything uses it.

An encode row carries the bytes its call must produce. A decode row carries the
whole reply OBJECT its telegram must parse to, built here by keyword from the
classes in railctl.xbus.replies, so the comparison in the test is one `==` over
the entire dataclass rather than a hand-picked list of fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from railctl.xbus.commands import (
    FunctionAction,
    cmd_drive_128,
    cmd_function_single,
    cmd_pom_read_byte,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_z21_cv_read,
)
from railctl.xbus.dialect import XPRESSNET, Z21
from railctl.xbus.replies import (
    CvValue,
    Other,
    PagedCvValue,
    Reply,
    StationStatus,
    Unsupported,
)
from railctl.xbus.speed import Direction

XPRESSNET_THRESHOLD = XPRESSNET.long_address_threshold  # 100
Z21_THRESHOLD = Z21.long_address_threshold  # 128


@dataclass(frozen=True)
class EncodeVector:
    """One encoder call and the bytes it must produce.

    `call` is annotated `Callable[[], bytes]`, not `object`: the tests below
    invoke it, and `object` is not callable to any type checker, which this
    repo's conventions require to pass.
    """

    name: str
    call: Callable[[], bytes]
    telegram: bytes
    why: str


@dataclass(frozen=True)
class DecodeVector:
    """One reply telegram and the WHOLE object `parse` must return for it.

    `expected` is a constructed reply instance, and it sits third - input
    second, expectation third, reason last - so this dataclass reads the same
    way round as EncodeVector.

    It replaces a field-by-field assertion, which is weaker in exactly the
    direction this protocol keeps producing bugs. `(reply.raw_cv, reply.value,
    reply.ident, reply.z21_form) == (8, 8, 0x14, False)` keeps passing after the
    parser grows a field, stops setting a field it used to set, or renames one.
    A frozen dataclass compares on its whole field tuple AND on its class, so
    `parse(telegram) == expected` fails on all three.
    """

    name: str
    telegram: bytes
    expected: Reply
    why: str


def _b(text: str) -> bytes:
    return bytes.fromhex(text)


FWD = Direction.FORWARD

ENCODE_VECTORS: tuple[EncodeVector, ...] = (
    EncodeVector(
        "drive_128(99, fwd, 1) xpressnet",
        lambda: cmd_drive_128(99, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 00 63 82 16"),
        "below the XpressNet threshold",
    ),
    EncodeVector(
        "drive_128(100, fwd, 1) xpressnet",
        lambda: cmd_drive_128(100, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 64 82 D1"),
        "at the threshold",
    ),
    EncodeVector(
        "drive_128(100, fwd, 1) z21",
        lambda: cmd_drive_128(100, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 00 64 82 11"),
        "dialects disagree in 100..127",
    ),
    EncodeVector(
        "drive_128(127, fwd, 1) xpressnet",
        lambda: cmd_drive_128(127, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 7F 82 CA"),
        "top of the divergence band",
    ),
    EncodeVector(
        "drive_128(127, fwd, 1) z21",
        lambda: cmd_drive_128(127, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 00 7F 82 0A"),
        "top of the divergence band, the short form the other dialect does not send",
    ),
    EncodeVector(
        "drive_128(128, fwd, 1) xpressnet",
        lambda: cmd_drive_128(128, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 80 82 35"),
        "dialects agree again",
    ),
    EncodeVector(
        "drive_128(128, fwd, 1) z21",
        lambda: cmd_drive_128(128, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 C0 80 82 35"),
        "dialects agree again",
    ),
    EncodeVector(
        "service_direct_read(1)",
        lambda: cmd_service_direct_read(1),
        _b("22 15 01 36"),
        "direct CV is NOT zero-based",
    ),
    EncodeVector(
        "service_direct_read(255)",
        lambda: cmd_service_direct_read(255),
        _b("22 15 FF C8"),
        "MAX_CV_DIRECT",
    ),
    EncodeVector(
        "service_ext_read(1)",
        lambda: cmd_service_ext_read(1),
        _b("22 18 01 3B"),
        "band 0",
    ),
    EncodeVector(
        "service_ext_read(256)",
        lambda: cmd_service_ext_read(256),
        _b("22 19 00 3B"),
        "22 18 00 is NOT CV256",
    ),
    EncodeVector(
        "service_ext_read(1024)",
        lambda: cmd_service_ext_read(1024),
        _b("22 18 00 3A"),
        "CV1024 is page 0 with C = 0",
    ),
    EncodeVector(
        "pom_read_byte(3, 1)",
        lambda: cmd_pom_read_byte(3, 1, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E4 00 00 31"),
        "POM is zero-based",
    ),
    EncodeVector(
        "pom_read_byte(3, 8)",
        lambda: cmd_pom_read_byte(3, 8, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E4 07 00 36"),
        "the probe telegram",
    ),
    EncodeVector(
        "pom_read_byte(3, 257)",
        lambda: cmd_pom_read_byte(3, 257, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E5 00 00 30"),
        "crosses into MM=1",
    ),
    EncodeVector(
        "z21_cv_read(29)",
        lambda: cmd_z21_cv_read(29),
        _b("23 11 00 1C 2E"),
        "16-bit zero-based",
    ),
    EncodeVector(
        "function_single(3, F0, ON)",
        lambda: cmd_function_single(3, 0, FunctionAction.ON, threshold=XPRESSNET_THRESHOLD),
        _b("E4 F8 00 03 40 5F"),
        "measured: lit the headlight of loco 3 (docs/probe-results.md, D12)",
    ),
)

# The unknown row's telegram appears twice - once as the input, once inside the
# Other the parser echoes it back in - so it is named rather than typed twice.
UNKNOWN_TELEGRAM = _b("71 AA DB")

DECODE_VECTORS: tuple[DecodeVector, ...] = (
    DecodeVector(
        "station status 62 22 07",
        _b("62 22 07 47"),
        # Written out field by field on purpose, NOT as StationStatus.from_raw(0x07):
        # from_raw is the parser's own bit-mask code, so calling it here would make
        # the expectation agree with the parser by construction and the row would
        # stop being evidence of anything.
        StationStatus(
            raw=0x07,
            emergency_off=True,
            emergency_stop=True,
            auto_start_mode=True,
            service_mode=False,
            powering_up=False,
            ram_error=False,
        ),
        "measured: emergency off + emergency stop + automatic start mode",
    ),
    DecodeVector(
        "lenz cv result 63 14 08 08",
        _b("63 14 08 08 77"),
        # ident is kept because 63 14/15/16/17 are four different result bands, and
        # z21_form False is what tells cv.resolve_service_cv this is the 8-bit form.
        CvValue(raw_cv=0x08, value=0x08, ident=0x14, z21_form=False),
        "a CV result whose CV number only the caller can resolve",
    ),
    DecodeVector(
        "z21 cv result 64 14 00 07 91",
        _b("64 14 00 07 91 E6"),
        # raw_cv 7 is the two address bytes joined by cv.join_cv_field, and it names
        # no CV number: whether it means CV7 or CV8 is cv.resolve_service_cv's job
        # and is UNMEASURED for this form. What the row pins is that the field
        # arrives whole and that z21_form is True, which is how the caller knows
        # which resolution rule to apply.
        CvValue(raw_cv=7, value=145, ident=0x14, z21_form=True),
        "doc only (spec line 573); never seen on this station, parsed so a Z21 LAN "
        "transport or a firmware update cannot lose the value",
    ),
    DecodeVector(
        "paged result 63 10 01 03",
        _b("63 10 01 03 71"),
        # A PagedCvValue and not a CvValue. The number is a REGISTER; reading it as
        # a CV publishes a value the decoder never sent. The class is half of what
        # the comparison checks, which is why the test asserts the type as well.
        PagedCvValue(raw_register=1, value=3),
        "a VALID answer meaning register-mode fallback, not an error",
    ),
    DecodeVector(
        "not supported 61 82",
        _b("61 82 E3"),
        # parse returns the UNSUPPORTED singleton; a freshly constructed Unsupported
        # compares equal to it, because a frozen dataclass compares on its class and
        # its field tuple and not on identity. Constructing one keeps the row
        # independent of which object the module happens to hand back.
        Unsupported(),
        "the only reply that entitles anything above to record a capability as False",
    ),
    DecodeVector(
        "unknown 71 AA",
        UNKNOWN_TELEGRAM,
        # reason is spelled out rather than left to the default, because the whole
        # point of Other is that "unknown_form", "checksum", "length" and "empty"
        # stay apart: a well-formed telegram in a form nobody listed must not come
        # back looking like a damaged one.
        Other(telegram=UNKNOWN_TELEGRAM, reason="unknown_form"),
        "must produce Other with no exception",
    ),
)

ALL_VECTORS: tuple[EncodeVector | DecodeVector, ...] = ENCODE_VECTORS + DECODE_VECTORS
