# src/railctl/errors.py
"""The whole railctl exception tree, the exit-code map, and `exit_code_for`.

Nothing else in the package defines an exception type. One module means one
place to look when a caller asks "what can this raise", and it is what makes
tests/test_layering.py able to check that rule mechanically.

The distinction this project exists to preserve is between three answers:

* `LinkTimeout`             - the station said **nothing**. Unknown, not "no".
* `UnsupportedCommandError` - the station said **no** (`61 82`). A real answer.
* `UnsupportedFeatureError` - **we** decided it is out of scope. Never measured.

They are three classes with three exit codes (5, 6, 7) because collapsing them
is exactly how milestone M1 recorded four capabilities as absent when the
instrument, not the hardware, was at fault.

These exit codes are a versioned public contract. Within a major version no code
may be renumbered, repurposed, or retired; a new error class claims an unused
code above 20 instead of reusing one of these. The one code below that range is
`ConfirmationRequiredError: 2`, and it is not an exception to the rule so much as
the rule's other half: 2 is the CLI's documented *usage* code, shared with a
malformed argument, because both tell a script the same thing - fix the
invocation, do not retry. A domain failure never claims a low code. A future JSON envelope (M5
and later) can carry a stable machine-readable `error.code` string alongside the
process exit status, and that is where new domain detail belongs, not in a new
exit code.
"""

from __future__ import annotations

from typing import ClassVar, Final


class RailctlError(Exception):
    """Base for everything this package raises on purpose.

    `code` is the machine-readable string the CLI publishes in its JSON error
    envelope, and it is DECLARED here rather than derived from the class name.
    The two are separate on purpose: a class name is a Python identifier that
    any refactor may rename, and `code` is a public contract string that may
    never be renamed inside a major version. Deriving one from the other means
    a rename silently rewrites the contract - the refactor looks clean, the
    tests pass, and every script keyed on the old string breaks in the field.
    Declared, a rename moves nothing; changing what this tool publishes takes
    editing the line that spells it.

    `__init_subclass__` checks `cls.__dict__`, not `hasattr(cls, "code")`. A
    subclass inherits its parent's `code` attribute, so `hasattr` is satisfied
    by doing nothing at all - which is precisely the mistake declaring a code
    introduces and deriving one could not make: `PortBusy` would silently
    publish `transport`, and nothing would ever say so. Requiring the name in
    the class's OWN namespace makes an undeclared subclass a `TypeError` at
    import time, so it cannot reach a test run, a review, or a release.
    """

    code: ClassVar[str] = "railctl"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "code" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares no `code`. Every exception publishes a "
                f"machine-readable code in the CLI error envelope; inheriting one from "
                f"{cls.__mro__[1].__name__} would publish a wrong answer rather than no "
                f'answer. Add `code: ClassVar[str] = "..."` to {cls.__name__}.'
            )

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.details = details or {}


class TransportError(RailctlError):
    """Port vanished, write failed, or the LI reported an interface error."""

    code: ClassVar[str] = "transport"


class PortNotFound(TransportError):
    """No candidate port matched the requested target."""

    code: ClassVar[str] = "port_not_found"


class AmbiguousPort(TransportError):
    """More than one port matched and none was preferred."""

    code: ClassVar[str] = "ambiguous_port"


class PortBusy(TransportError):
    """The port exists but could not be opened - another process holds it,
    or permission was denied. The message carries the OS strerror either way."""

    code: ClassVar[str] = "port_busy"


class PortConfigError(TransportError):
    """The line settings were rejected."""

    code: ClassVar[str] = "port_config"


class PortNotOpen(TransportError):
    """A read or write was attempted before open()."""

    code: ClassVar[str] = "port_not_open"


class PortNotXpressNet(TransportError):
    """The port opened but the 21 21 00 handshake produced no 63 21 reply."""

    code: ClassVar[str] = "port_not_xpressnet"


class ProtocolError(RailctlError):
    """Well-framed but unparseable or unexpected telegram."""

    code: ClassVar[str] = "protocol"


class XBusEncodeError(ProtocolError):
    """A telegram could not be built from the given arguments."""

    code: ClassVar[str] = "xbus_encode"


class XBusDecodeError(ProtocolError):
    """A telegram could not be decoded."""

    code: ClassVar[str] = "xbus_decode"


class XBusChecksumError(XBusDecodeError):
    """The trailing XOR byte does not match the telegram body."""

    code: ClassVar[str] = "xbus_checksum"


class XBusIncompleteError(XBusDecodeError):
    """The buffer holds fewer bytes than the shortest possible telegram.

    Separate from its parent because the caller's response is different: an
    incomplete buffer means keep reading, a length or checksum fault means
    resync or retry. Both would otherwise be one XBusDecodeError separable only
    by message text, and a link that waits for bytes that will never come ends
    as "no reply" - how this project has recorded working capabilities as
    absent.
    """

    code: ClassVar[str] = "xbus_incomplete"


class LinkProtocolError(ProtocolError):
    """The station rejected the same telegram twice."""

    code: ClassVar[str] = "link_protocol"


class LinkTimeout(RailctlError):
    """No reply arrived within the budget. Silence - never a negative answer."""

    code: ClassVar[str] = "link_timeout"


class UnsupportedCommandError(RailctlError):
    """The station answered 61 82: it understood, and it refuses."""

    code: ClassVar[str] = "unsupported_command"


class UnsupportedFeatureError(RailctlError):
    """Outside this tool's declared scope (consists, unprobed F13+)."""

    code: ClassVar[str] = "unsupported_feature"


class StationError(RailctlError):
    """Facade-level base. Has no row in EXIT_CODES on purpose; it resolves to the base 9."""

    code: ClassVar[str] = "station"


class TrackPowerError(StationError):
    """Track power is off, or in the wrong state for this operation."""

    code: ClassVar[str] = "track_power"


class ProgrammingError(StationError):
    """Base for CV operations. Carries the human (1-based) CV number when known."""

    code: ClassVar[str] = "programming"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        cv: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, hint=hint, details=details)
        self.cv = cv


class DecoderNoAckError(ProgrammingError):
    """The station reported 61 13: no acknowledgement from the decoder."""

    code: ClassVar[str] = "decoder_no_ack"


class ShortCircuitError(ProgrammingError):
    """The station reported a short on the programming or main track."""

    code: ClassVar[str] = "short_circuit"


class StationBusyError(ProgrammingError):
    """The station reported 61 1F: a programming operation is already running."""

    code: ClassVar[str] = "station_busy"


class DecoderNotRespondingError(ProgrammingError):
    """Nothing came back at all - neither a value nor a no-ack."""

    code: ClassVar[str] = "decoder_not_responding"


class CvVerifyError(ProgrammingError):
    """A write completed but the read-back value differs."""

    code: ClassVar[str] = "cv_verify"


class CvOutOfRangeError(ProgrammingError):
    """The CV number is outside the bound the selected mode supports.

    Kept for what its name says. It still covers a CV out of reach of the
    encodings a station HAS proven - "direct opcodes only cover CV1..255" is a
    bound, and the remedy is the one a range error implies: another CV number,
    or another mode. What it no longer covers is a station nobody has probed;
    see `ServiceEncodingUnknownError`.
    """

    code: ClassVar[str] = "cv_out_of_range"


class ServiceEncodingUnknownError(ProgrammingError):
    """No service-mode encoding has been established on this station yet.

    A state error, not a usage error: the CV is fine and the identical call
    succeeds once a probe has run. It was `CvOutOfRangeError` until issue #16,
    where the doctor printed `every read failed (['CvOutOfRangeError'])` for
    CV7 and CV8 - both plainly valid - and the first reading of that line was
    "our CV numbering is broken", which is the most damaging fault this
    codebase can have. Minutes went into the wrong file before the real cause
    turned out to be "not probed yet".

    The remedy differs too, which is the test for whether two failures deserve
    one type: a range error is fixed by typing a different number, this one by
    running `railctl doctor`.
    """

    code: ClassVar[str] = "service_encoding_unknown"


class PomReadUnsupportedError(ProgrammingError):
    """POM reading is recorded as unavailable for this station."""

    code: ClassVar[str] = "pom_read_unsupported"


class IndexPageRequiredError(ProgrammingError):
    """The CV lives behind an index page that could not be selected."""

    code: ClassVar[str] = "index_page_required"


class AbortedError(RailctlError):
    """The operator interrupted the run. Cleanup ran; exit 9."""

    code: ClassVar[str] = "aborted"


class ConfirmationRequiredError(RailctlError):
    """A confirmation was needed and could not be asked for."""

    code: ClassVar[str] = "confirmation_required"


EXIT_CODES: Final[dict[type[RailctlError], int]] = {
    TransportError: 3,
    ProtocolError: 4,
    LinkTimeout: 5,
    UnsupportedCommandError: 6,
    UnsupportedFeatureError: 7,
    RailctlError: 9,
    DecoderNoAckError: 10,
    ShortCircuitError: 11,
    StationBusyError: 12,
    DecoderNotRespondingError: 13,
    CvVerifyError: 14,
    CvOutOfRangeError: 15,
    PomReadUnsupportedError: 16,
    IndexPageRequiredError: 17,
    ServiceEncodingUnknownError: 18,
    ProgrammingError: 19,
    TrackPowerError: 20,
    ConfirmationRequiredError: 2,
}

UNMAPPED_EXIT_CODE: Final[int] = 1


def exit_code_for(exc: BaseException) -> int:
    """Most specific mapped exit code for `exc`, or 1 when nothing matches.

    Walks `type(exc).__mro__`, so a new subclass inherits its parent's code
    until it is given one of its own. `StationError` has no row and resolves to
    the base 9 on purpose, exactly as the exit-code table states.
    """
    for klass in type(exc).__mro__:
        code = EXIT_CODES.get(klass)  # type: ignore[arg-type]
        if code is not None:
            return code
    return UNMAPPED_EXIT_CODE
