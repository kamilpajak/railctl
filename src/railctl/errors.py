# src/railctl/errors.py
"""The whole railctl exception tree, the exit-code map, and `exit_code_for`.

Nothing else in the package defines an exception type. One module means one
place to look when a caller asks "what can this raise", and it is what makes
tests/test_layering.py able to check that rule mechanically.

The distinction this project exists to preserve is between three answers:

* `LinkTimeout`             - the station said **nothing**. Unknown, not "no".
* `UnsupportedCommandError` - the station said **no** (`61 82`). A real answer.
* `UnsupportedFeatureError` - **we** decided it is out of scope. Never measured.

They are three classes with three `error.code` strings because collapsing them is
exactly how milestone M1 recorded four capabilities as absent when the
instrument, not the hardware, was at fault. They had three exit codes too (5, 6
and 7) until 0.3.0, and they no longer do: `LinkTimeout` is the retryable code
and the other two share the domain-failure code. That was given up deliberately -
see `railctl/CLAUDE.md` and issue #65 - because `error.code` already carried the
distinction and a second copy of it in the process status was what made this tool
publish twenty-one exit codes.

**`error.code` is the versioned public contract, not the number.** Within a major
version no `code` string may be renamed, repurposed or retired, and a new error
class declares its own rather than reusing one. What the process status still
separates is the one thing a caller can act on without reading anything:
`RETRYABLE_EXIT_CODE` means the same invocation may succeed later, and every
other non-zero value means it will not. Which failure it was is read from
`error.code`, and `railctl schema` lists every value it can take, per command as
well as in one table.

The eight published statuses and their meanings live in `railctl/exit_codes.py`;
`EXIT_CODES` below maps classes onto them, and most classes are deliberately not
in it - inheriting the base code is the normal case now, not an omission.

Numbers do not belong in the class docstrings below. `_meta._class_error_row`
publishes each one's first paragraph as the `summary` in `railctl schema`, so a
docstring naming an exit code becomes a row whose prose contradicts its own
`exit_code` field the moment the map changes. It did: `AbortedError` said "exit
9" while the row said 130.
"""

from __future__ import annotations

from typing import ClassVar, Final

from railctl.exit_codes import (
    DOMAIN_FAILURE_EXIT_CODE,
    INTERNAL_EXIT_CODE,
    INTERRUPTED_EXIT_CODE,
    NOT_FOUND_EXIT_CODE,
    RETRYABLE_EXIT_CODE,
    USAGE_EXIT_CODE,
)


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


#: The values `TrackPowerError.details["condition"]` may carry. Declared here,
#: next to the class, because three layers write them (`Station._settle_power`,
#: `cli/commands/throttle.preflight`, `cli/commands/power`) and one layer reads
#: them back (`cli/_errors.default_suggestions`, which maps each to a runnable
#: recovery). Free strings in four modules is how the reader and the writers
#: stop agreeing.
#:
#: A `TrackPowerError` whose condition is not one of these gets NO suggestion.
#: That is the point of naming them: the suggestion table used to answer any
#: condition-less `TrackPowerError` with `railctl power resume`, the one command
#: measured to start locomotives (docs/probe-results.md, "`power on`'s stop-all
#: was in the wrong order", run 5), including for a `power off` that failed to
#: settle - a failure that has nothing to do with a hold.
CONDITION_EMERGENCY_STOP: Final[str] = "emergency_stop"
CONDITION_EMERGENCY_OFF: Final[str] = "emergency_off"
#: `power resume` asked to release a hold on a track with no voltage. Distinct
#: from `emergency_off`, which is the same reading refusing a *throttle*
#: command: the recovery here is `power on` alone, and offering the release a
#: second time would be offering the command that was just refused.
CONDITION_TRACK_DEAD: Final[str] = "track_dead"
#: The station still disagreed with the power state it was commanded into,
#: after the settle pause and a re-read. Named by what was REQUESTED, because
#: what the track is actually doing is precisely what is not known.
CONDITION_POWER_ON_UNSETTLED: Final[str] = "power_on_unsettled"
CONDITION_POWER_OFF_UNSETTLED: Final[str] = "power_off_unsettled"


class FunctionGroupUnreadableError(StationError):
    """`function` could not read a group's current state before flipping one bit.

    `Station.function_set`/`function_toggle` refuse to blind-write a group
    rather than seed it all-zeros, because a group command carries every bit of
    its group and a wrong zero switches off a function nobody touched. This
    class carries the CLI's own retry command - the function and state tokens
    the operator actually typed, plus `--force-group` - since
    `cli/_errors.default_suggestions` is keyed by exception type and could not
    reconstruct that argv from the exception alone.

    No row in `EXIT_CODES`: like its `StationError` parent it resolves to the
    base 9, which is what the design spec's L6 safety rules already say this
    failure exits with.
    """

    code: ClassVar[str] = "function_group_unreadable"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, object] | None = None,
        retry_argv: list[str],
    ) -> None:
        super().__init__(message, hint=hint, details=details)
        self.retry_argv = retry_argv


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


#: `CvOutOfRangeError.details["reason"]` when the refused number is the VALUE
#: being written, not the CV. Both refusals share the class and the code -
#: the catalog's min/max are enforcing on write - but not the remedy: a CV the
#: mode cannot reach suggests `railctl doctor` (a re-probe is what could move
#: the bound), while nothing the doctor measures changes 300 not fitting in
#: 0..255. `cli/_errors.default_suggestions` reads this to keep the doctor
#: suggestion off the value refusal. Declared here, beside the class, for the
#: same reason the `CONDITION_*` strings are: the writer (`cli/commands/cv.py`)
#: and the reader (`cli/_errors.py`) must not each spell it themselves.
REASON_VALUE_OUT_OF_RANGE: Final[str] = "value_out_of_range"


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


class BackupIncompleteError(StationError):
    """A backup run finished, but the file it wrote has holes.

    Raised AFTER the partial file is written - the file is the product, and
    this exit code is the honest label on it. `summary.complete` is false
    exactly when a `no_response` or `error` row exists; a `skipped` row is a
    recorded decision, not a hole, and never raises this. No row in
    `EXIT_CODES`: like its `StationError` parent it resolves to the base 9.
    """

    code: ClassVar[str] = "backup_incomplete"


class CatalogError(RailctlError):
    """The curated CV catalog failed to load.

    Unparseable TOML, a wrong family or schema, a duplicate CV number or slug,
    or a malformed block. A defect in shipped reference data rather than an
    operator mistake, so like `StationError` it takes no row in `EXIT_CODES`
    and resolves to the base 9; the machine-readable detail lives in `code`.
    """

    code: ClassVar[str] = "catalog"


class BackupFileError(RailctlError):
    """A backup file is malformed, or could not be read or written.

    Both directions of `railctl.backup.file`: a reader rejection (wrong
    schema, a missing key, a `value` that disagrees with its `status`, a
    duplicate CV row) and a file the OS refused to read or write. The message
    names the first offence found. Like `CatalogError` it describes a data
    file rather than the invocation, takes no row in `EXIT_CODES`, and
    resolves to the base 9; the machine-readable detail lives in `code`.
    """

    code: ClassVar[str] = "backup_file"


class AddressSetIncompleteError(RailctlError):
    """`restore --with-address` was asked for an address the file does not fully carry.

    CV1, CV17, CV18 and CV29 decide together which address a locomotive answers
    to: CV29 bit 5 selects between the short address in CV1 and the long address
    in CV17/CV18. Writing some of them and not the others leaves a decoder
    answering at an address that is in no file and on no label, and finding it
    again means sweeping the address space. So a run refuses before its first
    write when any of the four is missing from the file or did not read `ok`.
    Like `BackupFileError` it describes the file rather than the invocation,
    takes no row in `EXIT_CODES`, and resolves to the base 9.
    """

    code: ClassVar[str] = "address_set_incomplete"


class RestoreFileIncompleteError(RailctlError):
    """`restore` was handed a file with holes, and nobody said that was intended.

    Distinct from `BackupIncompleteError`, which labels a file this tool has
    just WRITTEN: this one refuses to write a decoder FROM such a file. The
    holes are never written either way - a `no_response` row carries no value
    to write - so the flag is not permission to guess, it is the operator
    stating that a partial restore is what they want. Refused before the link
    opens, because a refusal that costs a port is a refusal an operator learns
    to pre-empt with the flag. Takes no row in `EXIT_CODES` and resolves to
    the base 9.
    """

    code: ClassVar[str] = "restore_file_incomplete"


class DecoderIdentityMismatchError(RailctlError):
    """The decoder on the programming track is not the one the file came from.

    Service mode addresses the TRACK, not a locomotive, so nothing in the
    protocol stops a restore writing a whole CV set into whatever happens to
    be standing there - and the curated set deliberately skips CV1/17/18/29,
    removing the one symptom (a changed address) that would have shown up
    later. Only this gate stands between a restore and the wrong locomotive.

    Live CV8 against the file's `manufacturer_id` and live CV250 against its
    `decoder_type` are hard: no flag overrides them. A serial mismatch
    (CV251-253) is overridable only by `--confirm=<the serial just read>`,
    never by `--yes`, because restoring onto a replacement decoder is
    legitimate and a slip of the hand is not. Takes no row in `EXIT_CODES`
    and resolves to the base 9.
    """

    code: ClassVar[str] = "decoder_identity_mismatch"


class ProgrammingLockedError(RailctlError):
    """Live CV144 is non-zero on a decoder family that reads it as the lock.

    On the older ZIMO MX family CV144 is the programming/update lock and a
    non-zero value blocks writes, so a restore that started anyway would fail
    CV by CV with nothing saying why. The tool refuses instead of clearing it:
    the lock is there because somebody set it, and unlocking a decoder is a
    decision, not a step. On the MS family CV144 is the confirmation jingle
    and this is not a precondition at all (`station.treats_cv144_as_lock`),
    which is also why an unread decoder type never raises this. Takes no row
    in `EXIT_CODES` and resolves to the base 9.
    """

    code: ClassVar[str] = "programming_locked"


class AbortedError(RailctlError):
    """The operator interrupted the run. Cleanup ran."""

    code: ClassVar[str] = "aborted"


class ConfirmationRequiredError(RailctlError):
    """A confirmation was needed and could not be asked for.

    `retry_argv` is the raiser's own full, runnable retry command with `--yes`
    already appended - the same mechanism `FunctionGroupUnreadableError` uses
    for the same reason: `cli/_errors.default_suggestions` is keyed by
    exception type and cannot reconstruct the CV and value the operator typed
    from the type alone, so without this it suggested
    `["railctl", "cv", "write", "--yes"]` - an argv that exits 2.
    """

    code: ClassVar[str] = "confirmation_required"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, object] | None = None,
        retry_argv: list[str] | None = None,
    ) -> None:
        super().__init__(message, hint=hint, details=details)
        self.retry_argv = retry_argv


#: Class -> process exit code. Eight values, declared in `railctl/exit_codes.py`;
#: this table only says which of them each failure lands on.
#:
#: Most rows are absent on purpose. `exit_code_for` walks the MRO, so anything
#: without its own row takes its nearest mapped ancestor's - and after #65 the
#: nearest ancestor is almost always `RailctlError` itself. Before that collapse
#: there were eighteen rows and eleven distinct numbers below, one per class,
#: which made `$?` a second spelling of `error.code`. What a caller can now act
#: on without reading anything is retryable-or-not; which failure it was is
#: `error.code`.
EXIT_CODES: Final[dict[type[RailctlError], int]] = {
    # The three the caller may usefully retry, and exactly these. Kept identical
    # to `cli.result.RETRYABLE_CODES` by
    # `tests/unit/test_exit_codes.py::test_exactly_the_retryable_codes_exit_with_the_retryable_status`,
    # which compares the two sets in both directions - a class added to one and
    # not the other is the defect this collapse was made to fix.
    LinkTimeout: RETRYABLE_EXIT_CODE,
    StationBusyError: RETRYABLE_EXIT_CODE,
    PortBusy: RETRYABLE_EXIT_CODE,
    # A port that was named and is not there. The only "not found" this tool has;
    # a malformed reply is a domain failure, and confusing the two is what made
    # the old 4 contradict the convention it shares a number with.
    PortNotFound: NOT_FOUND_EXIT_CODE,
    # Refused before anything ran, so the caller can fix the command line.
    ConfirmationRequiredError: USAGE_EXIT_CODE,
    # The operator stopped the run. `cli/_errors.run` raises SystemExit for this
    # rather than typer.Exit - see its comment; typer returns an Exit's code as a
    # plain int, which would reach `main()`'s 130 sentinel and print a second
    # envelope.
    AbortedError: INTERRUPTED_EXIT_CODE,
    # Everything else. Every class not listed above inherits this through the MRO.
    RailctlError: DOMAIN_FAILURE_EXIT_CODE,
}

#: Retired as a separate name in 0.3.0: it was 1 and so is `INTERNAL_EXIT_CODE`.
#: "No row matched" and "railctl has a bug" are the same fact - a class the map
#: cannot resolve IS a bug - so they are one name now.
UNMAPPED_EXIT_CODE: Final[int] = INTERNAL_EXIT_CODE


def exit_code_for(exc: BaseException) -> int:
    """Most specific mapped exit code for `exc`, or 1 when nothing matches.

    Walks `type(exc).__mro__`, so a subclass inherits its parent's code unless it
    is given one of its own - and after #65 most classes have none, resolving to
    `DOMAIN_FAILURE_EXIT_CODE` through `RailctlError`. That is the design, not an
    omission: the number says whether to retry, and `error.code` says what
    happened.
    """
    for klass in type(exc).__mro__:
        code = EXIT_CODES.get(klass)  # type: ignore[arg-type]
        if code is not None:
            return code
    return UNMAPPED_EXIT_CODE
