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

These sixteen exit codes are a versioned public contract. Within a major version
no code may be renumbered, repurposed, or retired; a new error class claims an
unused code above 20 instead of reusing one of these. A future JSON envelope (M5
and later) can carry a stable machine-readable `error.code` string alongside the
process exit status, and that is where new domain detail belongs, not in a new
exit code.
"""

from __future__ import annotations

from typing import Final


class RailctlError(Exception):
    """Base for everything this package raises on purpose."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class TransportError(RailctlError):
    """Port vanished, write failed, or the LI reported an interface error."""


class PortNotFound(TransportError):
    """No candidate port matched the requested target."""


class AmbiguousPort(TransportError):
    """More than one port matched and none was preferred."""


class PortBusy(TransportError):
    """The port exists but another process holds it."""


class PortConfigError(TransportError):
    """The line settings were rejected."""


class PortNotOpen(TransportError):
    """A read or write was attempted before open()."""


class PortNotXpressNet(TransportError):
    """The port opened but the 21 21 00 handshake produced no 63 21 reply."""


class ProtocolError(RailctlError):
    """Well-framed but unparseable or unexpected telegram."""


class XBusEncodeError(ProtocolError):
    """A telegram could not be built from the given arguments."""


class XBusDecodeError(ProtocolError):
    """A telegram could not be decoded."""


class XBusChecksumError(XBusDecodeError):
    """The trailing XOR byte does not match the telegram body."""


class XBusIncompleteError(XBusDecodeError):
    """The buffer holds fewer bytes than the shortest possible telegram.

    Separate from its parent because the caller's response is different: an
    incomplete buffer means keep reading, a length or checksum fault means
    resync or retry. Both would otherwise be one XBusDecodeError separable only
    by message text, and a link that waits for bytes that will never come ends
    as "no reply" - how this project has recorded working capabilities as
    absent.
    """


class LinkProtocolError(ProtocolError):
    """The station rejected the same telegram twice."""


class LinkTimeout(RailctlError):
    """No reply arrived within the budget. Silence - never a negative answer."""


class UnsupportedCommandError(RailctlError):
    """The station answered 61 82: it understood, and it refuses."""


class UnsupportedFeatureError(RailctlError):
    """Outside this tool's declared scope (consists, unprobed F13+)."""


class StationError(RailctlError):
    """Facade-level base. Has no row in EXIT_CODES on purpose; it resolves to the base 9."""


class TrackPowerError(StationError):
    """Track power is off, or in the wrong state for this operation."""


class ProgrammingError(StationError):
    """Base for CV operations. Carries the human (1-based) CV number when known."""

    def __init__(self, message: str, *, hint: str | None = None, cv: int | None = None) -> None:
        super().__init__(message, hint=hint)
        self.cv = cv


class DecoderNoAckError(ProgrammingError):
    """The station reported 61 13: no acknowledgement from the decoder."""


class ShortCircuitError(ProgrammingError):
    """The station reported a short on the programming or main track."""


class StationBusyError(ProgrammingError):
    """The station reported 61 1F: a programming operation is already running."""


class DecoderNotRespondingError(ProgrammingError):
    """Nothing came back at all - neither a value nor a no-ack."""


class CvVerifyError(ProgrammingError):
    """A write completed but the read-back value differs."""


class CvOutOfRangeError(ProgrammingError):
    """The CV number is outside the bound the selected mode supports."""


class PomReadUnsupportedError(ProgrammingError):
    """POM reading is recorded as unavailable for this station."""


class IndexPageRequiredError(ProgrammingError):
    """The CV lives behind an index page that could not be selected."""


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
    ProgrammingError: 19,
    TrackPowerError: 20,
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
