"""The shared CV wait loop, the echo matcher, POM reads, and AUTO mode
resolution (design doc lines 708-743, 786-787, 916-924).

`station/` may hold no framing bytes, no port names, and no CV arithmetic
(`tests/test_layering.py`, rules 1 and 2) - `xbus.cv`'s `echo_candidates`,
`decode_echo` and `result_ident_for` are what let this module compare CV
numbers and reply idents without ever touching a wire byte itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from railctl.errors import (
    CvOutOfRangeError,
    CvVerifyError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    LinkTimeout,
    PomReadUnsupportedError,
    ProtocolError,
    RailctlError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
    StationBusyError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import (
    ADDRESS_CVS,
    BLIND_WRITE_CVS,
    CV29_LONG_ADDRESS_BIT,
    INDEXED_CV_RANGE,
    PAGE_SELECTOR_CVS,
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    ProgMode,
)
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_pom_read_byte,
    cmd_pom_write_byte,
    cmd_service_direct_read,
    cmd_service_direct_write,
    cmd_service_ext_read,
    cmd_service_ext_write,
    cmd_service_result_request,
    cmd_station_status,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
    cmd_z21_cv_write,
)
from railctl.xbus.cv import (
    CV_MIN,
    MAX_CV_DIRECT,
    MAX_CV_EXT,
    MAX_CV_Z21,
    CvEncoding,
    decode_echo,
    echo_candidates,
    ext_cv_fields,
    result_ident_for,
)
from railctl.xbus.replies import (
    TRANSIENT_REPLIES,
    Busy,
    CvValue,
    NoAck,
    PagedCvValue,
    Ready,
    Reply,
    ShortCircuit,
    StationBusy,
    StationStatus,
    TrackShortCircuit,
    parse,
)

if TYPE_CHECKING:
    from railctl.station.facade import Station

__all__ = [
    "SERVICE_BATCH_SIZE",
    "SERVICE_ENCODING_ORDER",
    "UNEXERCISED_BANDS",
    "CvMatcher",
    "CvProgrammer",
    "PageKey",
    "ResultChannelSeen",
    "TimedOut",
    "WaitOutcome",
    "resolve_mode",
]


@dataclass(frozen=True, slots=True)
class PageKey:
    """The page cache's key: one selected CV31/CV32 page is scoped to one
    locomotive address (POM) or to the track as a whole (service mode, where
    `address` is always `None`) - and to the mode itself, since the same
    address can hold a different page selection under POM than under
    service."""

    address: int | None
    mode: ProgMode


@dataclass(frozen=True, slots=True)
class TimedOut:
    """The wait ran out with no answer. Distinct from `NoAck`: the station may
    simply never have replied, which is UNKNOWN, never a negative answer."""

    polls: int
    ready_streak: int
    saw_no_ack: bool


WaitOutcome = Reply | TimedOut
ResultChannelSeen = Literal["broadcast", "poll"]

_T = TypeVar("_T")

SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] = (
    ("z21_cv_opcodes", CvEncoding.Z21_16BIT),
    ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT),
)
UNEXERCISED_BANDS: Final[frozenset[int]] = frozenset({2, 3})  # 63 16 / 63 17, never answered here


def no_ack_hint(*, answered_in_session: bool) -> str:
    """What to tell an operator whose CV answered `61 13`.

    Two situations wear the same reply. When nothing has answered, the track
    is the first thing to check - the M8 acceptance proved a `61 13` can also
    be the tool's own timing, which is why the retry is named as already
    spent (`_retry_once_for_unknown_gap`). But when another CV answered inside
    THIS open session, the decoder is demonstrably in contact and drawing
    acknowledgement current, so sending the operator to look at the wheels
    describes something the run has already disproved (issue #46). Measured
    2026-08-19: `cv read 99-101` returned CV99 and CV101 and refused CV100,
    four times over, and the advice was to check the contact.

    The scope is one SESSION, not one invocation, and deliberately so. Inside
    an open session the contact and the programming current are shared by
    every read in it. Across sessions they are not the only thing that
    changes: a decoder that answers and then stops answering when the session
    is reopened is the measured behaviour issue #22 is about, so an earlier
    session's success is not evidence about this one.
    """
    if answered_in_session:
        return (
            "another CV answered inside this same service session, so the wheel contact "
            "and the programming current are not the cause - this CV did not acknowledge. "
            "Some CVs hold a live measurement rather than a stored value and never "
            "acknowledge a read"
        )
    return (
        "decoder did not acknowledge, and the session-gap retry already ran - "
        "check the wheels are making contact with the programming track; sound "
        "decoders can also fail on a 750 mA programming output"
    )


# Conditions no later CV in a shared service-mode session can survive.
#
# The first two cannot clear themselves by trying the next CV: a shorted track
# stays shorted, and a station that owns the bus for another operation still
# owns it. The last two qualify on cost rather than certainty - a dead link
# might in principle recover, but finding out costs `li_ack_programming`
# (95 s) per CV, so a batch of nine identity CVs would hang for a quarter of
# an hour before reporting what the first failure already said.
BATCH_ENDING_ERRORS: Final[tuple[type[RailctlError], ...]] = (
    ShortCircuitError,
    StationBusyError,
    LinkTimeout,
    TransportError,
)
#: How many CVs share one service-mode session. Four is the largest number
#: PROVEN on the bench (issue #22, 2026-08-07: four reads inside one session
#: all answered); everything above it is this project's choice, not a
#: measurement, which is why the number lives here with that caveat rather
#: than being spelled inline. Bounded rather than unbounded because a session
#: holds the station in service mode: at ~3 s per read a group of 8 keeps one
#: session under half a minute, so an operator's Ctrl-C, a short circuit or a
#: station reset costs at most that much progress.
#:
#: Eight rather than sixteen because doubling the group buys almost nothing
#: and doubles the unproven part. The gap is 3 s per SESSION: a 77 CV backup
#: pays it ten times at 8 (30 s) and five times at 16 (15 s), against ~235 s
#: of reading either way. Fifteen seconds is not worth holding an untested
#: 48 s session and risking 16 CVs of progress instead of 8. Raise it once a
#: bench run shows a long session answering to the last CV.
SERVICE_BATCH_SIZE: Final[int] = 8
_REGISTER_COLLISION_MAX: Final[int] = 8  # registers 1..8 collide with CV1..8


class CvMatcher:
    """Does a reply answer a request for `cv` under `encoding`?

    Two independent checks both have to hold for the measured `63 14..17`
    form, and mixing them up is the "right value, wrong CV name" failure this
    project exists to catch: `echo_candidates` narrows the C byte WITHIN a
    band, but two CVs 256 apart share a candidate set
    (`echo_candidates(POM_ZERO_BASED, 265) == echo_candidates(POM_ZERO_BASED,
    9)`), so the byte alone cannot tell CV265 from CV9. `result_ident_for`
    supplies the band the ident carries. Either check alone accepts a
    same-numbered CV from the wrong page.
    """

    def __init__(
        self,
        encoding: CvEncoding,
        cv: int,
        *,
        zero_based: bool | None = None,
        page_index: int | None = None,
    ) -> None:
        self.encoding = encoding
        self.cv = cv
        self._zero_based = zero_based
        # Consumed only when `encoding` is SERVICE_EXT (Task 5's service-mode
        # reads). POM's own matching needs only `cv` and `encoding`, because
        # `result_ident_for` already carries the band the SERVICE_EXT echo
        # byte cannot.
        self._page_index = page_index

    def __call__(self, reply: Reply) -> bool:
        if not isinstance(reply, CvValue):
            return False
        if reply.z21_form:
            # Documented, never measured on this hardware (docs/probe-results.md,
            # R1): no POM reply has ever been seen at all, let alone in this
            # form. Kept general anyway - see the module docstring on why a
            # real answer must never be treated as silence.
            try:
                return decode_echo(CvEncoding.Z21_16BIT, reply.raw_cv) == self.cv
            except ValueError:
                return False
        if reply.ident != result_ident_for(self.cv, self.encoding):
            return False
        return reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=self._zero_based)

    def value_of(self, reply: CvValue) -> int:
        """The decoded byte value. Trivial on its own - `CvValue.value` is
        already the plain 0..255 byte for every form `parse` produces - but
        kept as a method so a caller never reads `.value` off a reply it has
        not first confirmed matches, and so a future encoding with its own
        value convention has exactly one place to change."""
        return reply.value

    def echo_says_zero_based(self, reply: CvValue) -> bool | None:
        """Which POM echo convention `reply` demonstrates, or `None` if it
        does not settle the question.

        Only `POM_ZERO_BASED` has an unmeasured convention; every other
        encoding's echo rule is already fixed, so this always returns `None`
        for them. Also `None` once THIS matcher was already constructed with
        a fixed `zero_based` - the question this matcher was built to answer
        is already settled, and re-answering it here would let a later,
        differently-echoed reply overwrite an already-learned fact. Delegates
        the byte comparison to `echo_candidates` rather than computing the
        zero-based offset or masking a band byte here, which would be CV
        arithmetic in `station/` - forbidden by `tests/test_layering.py`.
        """
        if (
            self.encoding is not CvEncoding.POM_ZERO_BASED
            or reply.z21_form
            or self._zero_based is not None
        ):
            return None
        if reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=True):
            return True
        if reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=False):
            return False
        return None


def resolve_mode(
    mode: ProgMode, capabilities: Capabilities, *, operation: Literal["read", "write"]
) -> ProgMode:
    """AUTO never reaches the caller; every explicit mode passes through
    unchanged. `pom_read is not False` covers both `True` (measured working)
    and `None` (unknown - POM is tried and the outcome recorded). SERVICE is
    the fallback only when POM is a measured no AND service mode is a measured
    yes; nothing here is inferred from an unprobed capability.

    "Service mode is a measured yes" means ANY one of the three encodings is
    proven, which is what `reads_available` and `_service_encoding_for`
    already mean by it. This used to consult `service_direct_cv` alone, so a
    station proving only the Z21 opcode was refused an AUTO read and told to
    use `--mode service` - which then worked, because the encoding picker
    accepts what the fallback had declined to look at. Issue #25.

    Iterating `SERVICE_ENCODING_ORDER` rather than naming the three fields
    keeps the definition in one place: a fourth encoding joins that tuple and
    this fallback follows without being edited.
    """
    if mode is not ProgMode.AUTO:
        return mode
    if capabilities.pom_read is not False:
        return ProgMode.POM
    if any(getattr(capabilities, name) is True for name, _ in SERVICE_ENCODING_ORDER):
        return ProgMode.SERVICE
    # Never advise `--mode service` from here. Widening the condition above
    # means this line is reached only when NO service encoding is proven, so
    # that command would raise in its turn - `ServiceEncodingUnknownError` if
    # anything is still unprobed, `CvOutOfRangeError` if the station rejected
    # all three. Sending an operator to a mode this function just established
    # cannot work is the same defect as an error naming a cause that does not
    # exist (#16), one layer out: the remedy is the part that lies.
    unprobed = tuple(
        name for name, _ in SERVICE_ENCODING_ORDER if getattr(capabilities, name) is None
    )
    if unprobed:
        remedy = "run `railctl doctor` to probe the service-mode encodings"
        state = f"and no service-mode encoding is proven yet (unprobed: {', '.join(unprobed)})"
    else:
        remedy = "no CV path is available on this command station"
        state = "and the station rejected every service-mode encoding (61 82)"
    raise PomReadUnsupportedError(
        f"POM is unsupported on this command station for a CV {operation}, {state}",
        hint=remedy,
    )


class CvProgrammer:
    """POM reads (this task) and, from Task 5, service-mode reads and writes,
    sharing one wait loop.

    Takes the whole `Station` rather than a bag of collaborators, and reads
    `station.capabilities` fresh on every call rather than caching it here:
    `learn()` replaces the `Capabilities` object wholesale, so a `CvProgrammer`
    that cached it at construction time would never see a capability this same
    session just learned.
    """

    def __init__(self, station: Station) -> None:
        self._station = station
        self._pages: dict[PageKey, tuple[CvPage, float]] = {}
        # Keyed by (address, mode), like `_pages`, but the value records
        # WHICH page was verified there, not just that some page once was -
        # a second, different page selected under the same key must verify
        # again, not ride on the first page's read-back.
        self._verified_pages: dict[PageKey, CvPage] = {}
        # When the last service-mode session closed, or None if none has
        # closed since the gap it owed was already paid. See
        # `_await_session_gap`.
        self._last_session_end: float | None = None
        # Whether this INSTANCE has ever closed a service session. `None` in
        # `_last_session_end` is two different facts wearing one value: "the
        # gap was paid" and "this instance has no idea when the previous
        # invocation's session closed". Every CLI invocation builds a fresh
        # programmer, so the second fact is the normal one - and treating it
        # as the first is how the M8 acceptance's write failed `61 13` one
        # second after a read (docs/probe-results.md, "The session gap
        # crosses invocations"). This flag tells the two apart; see
        # `_retry_once_for_unknown_gap`.
        self._session_history_unknown = True
        # Whether any CV has answered inside the service session that is open
        # right now. Cleared when the session closes, because that is the
        # scope over which the wheel contact and the programming current are
        # one shared fact - see `no_ack_hint`.
        self._session_answered = False

    def _retry_once_for_unknown_gap(self, attempt: Callable[[], _T]) -> _T:
        """Run `attempt`; on a first-session `61 13` with unknown history, retry once.

        A session opened within ~1.5-1.75 s of the previous one closing fails
        wholesale with `61 13` (measured 2026-08-07), and a fresh programmer
        instance cannot know when the previous INVOCATION's session closed -
        `railctl cv read 3 && railctl cv write 3 26` failed exactly this way
        on the bench (2026-08-12) while the same write after an idle minute
        succeeded. The two causes of `61 13` are indistinguishable in the
        moment, but they separate under one retry: the failed attempt's exit
        stamps `_last_session_end`, so the second attempt's own
        `_await_session_gap` waits out the full measured gap - after which a
        second `61 13` is the real thing, a decoder that did not answer.

        Retries only while `_session_history_unknown`: an instance that has
        closed a session before owns its timing, and a `61 13` then is an
        answer, not an artefact. The retry emits `service.session_retried` so
        the envelope says the first attempt was discarded and why - a silent
        retry would hide the very timing fact this exists to handle.

        Idempotent by construction for both callers: re-reading a CV reads it
        again, and re-writing the same value writes the same value.
        """
        # Snapshot BEFORE the attempt: a failed attempt's own `finally` exits the
        # session and sets `_session_history_unknown = False`, so reading the flag
        # at catch time always says "known" and the retry never fires. That is
        # exactly how the first shipped version behaved on the bench (2026-08-12,
        # second acceptance run): the hint claimed the retry had run while the
        # helper had re-raised immediately - and the unit test missed it because
        # its mocked `exit_service_mode` skipped the very state transition that
        # breaks the check.
        history_was_unknown = self._session_history_unknown
        try:
            return attempt()
        except DecoderNoAckError:
            if not history_was_unknown:
                raise
            self._station.emit(
                "service.session_retried",
                {
                    "reason": "first session of this invocation answered 61 13; the "
                    "previous invocation's session may have closed moments ago",
                    "gap_s": self._station.timing.service_session_gap,
                },
            )
            return attempt()

    def _await_session_gap(self) -> None:
        """Wait out `service_session_gap` if a session closed too recently.

        A session opened too soon after the previous one closed fails
        outright - every CV in it answers `61 13`, the first one included.
        Measured 2026-08-07 (issue #22): three sessions run back to back,
        the first read its CVs, the second failed both, and a third 3 s
        later read them again.

        Paid at most once per session, not once per CV: the timestamp is
        cleared as soon as it has been honoured, so the reads that follow
        inside the same open session go straight through. Nothing is owed
        before the first session of a process, and nothing is owed after the
        last one - a trailing sleep no caller is waiting on would be pure
        cost.
        """
        if self._last_session_end is None:
            return
        remaining = self._station.timing.service_session_gap - (
            self._station.now() - self._last_session_end
        )
        if remaining > 0:
            self._station.pause(remaining)
        # Cleared AFTER the wait, not before: an interrupted pause leaves the
        # debt owed, so the next read tries again. Clearing first would let a
        # failed wait look like a paid one, which is a silent return to the
        # bug rather than an error anybody sees.
        self._last_session_end = None

    def reads_available(self, mode: ProgMode) -> bool:
        """Whether ANY read path is confirmed working for `mode`.

        Never `None`: an unprobed capability does not entitle a read attempt,
        the same rule Task 6b's write ladder uses. For POM this is
        deliberately narrower than "not False" - on THIS hardware `pom_read`
        is measured False (POM read returns nothing at all), so page
        selection over POM can never be verified here, and `select_page`
        must emit `page.unverified` rather than pretend to check.
        """
        caps = self._station.capabilities
        if mode is ProgMode.POM:
            return caps.pom_read is True
        return (
            caps.z21_cv_opcodes is True
            or caps.service_direct_cv is True
            or caps.service_ext_cv is True
        )

    def invalidate_pages(self) -> None:
        """Registered with `station.register_cache` in `Station.__init__` -
        that registration does not change here, only what this method clears.

        Takes no argument on purpose: the page cache is keyed by `(address, mode)`,
        but `power_off()`, `close()` and `exit_service_mode()` (which all call
        `station.invalidate_caches()`) mean "the track state is no longer
        trustworthy for ANYONE" - narrowing this clear to one address would buy
        nothing but a bug the day two locomotives share a session.
        """
        self._pages.clear()
        self._verified_pages.clear()

    def _raise_for_write_reply(self, reply: Reply, cv: int) -> None:
        """Maps the `61 xx` replies a write can get back, once `station.exchange`
        has already turned `61 82` into `UnsupportedCommandError` and every
        `InterfaceStatus`/damaged-reply case into its own exception. Everything
        that reaches this method is one of `station.exchange`'s pass-through
        forms - `GenericAck` (POM's `01 04 05`) and anything not named below
        fall through as accepted, exactly as the LI documentation says the
        generic ack means only "handed to the command station".
        """
        if isinstance(reply, ShortCircuit):
            raise ShortCircuitError(f"short circuit on the programming track writing CV{cv}", cv=cv)
        if isinstance(reply, TrackShortCircuit):
            raise ShortCircuitError(f"short circuit on the main track writing CV{cv}", cv=cv)
        if isinstance(reply, (Busy, StationBusy)):
            raise StationBusyError(f"station busy while writing CV{cv}", cv=cv)

    def _write_and_confirm(
        self, cv: int, value: int, *, address: int | None, mode: ProgMode
    ) -> tuple[CvEncoding, bool]:
        """Puts one CV write on the wire and confirms the station accepted it.

        Pays `_await_session_gap` like the read path, since 2026-08-12. The
        earlier asymmetry ("nothing does that today ... measure before adding
        a delay here") ended when M8's `cv write` followed `cv read`'s closed
        session and failed `61 13`, reproduced deterministically on the bench
        (docs/probe-results.md, "The session gap crosses invocations").

        POM: the interface ack IS the confirmation - there is no other channel
        (module docstring: "neither the PC nor the interface can determine
        whether a command reached the track"). Service (Task 6b): the same
        wait loop `service_read` uses, with `ready_means_done=True`, because
        after a WRITE `61 11` means the write finished, not "no result
        waiting" (spec line 780).

        Returns `(encoding, echo_confirmed_only)`. `encoding` is the encoding
        actually used, so a caller building a `CvResult` does not have to
        re-derive it. `echo_confirmed_only` is `True` only when the SERVICE
        branch's confirmation came from a matching `CvValue`/`PagedCvValue`
        echo rather than the definitive `Ready` (`61 11`) completion signal -
        callers use it to keep `verified` honest: the echo shows what the
        station's own result store holds, not that the decoder retained the
        value (docs/probe-results.md, "Service-mode WRITE works": "`63 14`...
        shows the command station produced that value; it does not by itself
        prove the decoder accepted and retained it"). Always `False` on the
        POM branch - POM's own confirmation is entirely `pom_write`'s
        business (a separate `cv_read`), never this method's.
        """
        if mode is ProgMode.POM:
            if address is None:
                raise ValueError("POM CV write needs a locomotive address")
            telegram = cmd_pom_write_byte(address, cv, value, threshold=self._station.threshold)
            reply = self._station.exchange(telegram, timeout=self._station.timing.li_ack_normal)
            self._raise_for_write_reply(reply, cv)
            self._station.pause(self._station.timing.pom_write_settle)
            return CvEncoding.POM_ZERO_BASED, False
        telegram, encoding, page_index = self.service_write_telegram(cv, value)
        # The comment that used to sit here said "nothing does that today" of a write
        # following a read's closed session, and asked for a measurement before adding
        # the delay. The measurement exists now: 2026-08-12 (docs/probe-results.md,
        # "The session gap crosses invocations"), `cv read 3` then `cv write 3 26`
        # back-to-back failed `61 13` while the identical write after an idle gap
        # succeeded. A service WRITE pays the same gap a read does.
        self._await_session_gap()
        before = self._status_before()
        echo_confirmed_only = False
        try:
            reply = self._station.exchange(
                telegram, timeout=self._station.timing.li_ack_programming
            )
            self._raise_for_write_reply(reply, cv)
            matcher = CvMatcher(
                encoding,
                cv,
                page_index=page_index if encoding is CvEncoding.SERVICE_EXT else None,
            )
            outcome = self.await_result(
                matcher,
                timeout=self._station.timing.service_result,
                first_delay=self._station.timing.service_first_poll_delay,
                interval=self._station.timing.service_poll_interval,
                exchange_timeout=self._station.timing.li_ack_programming,
                allow_poll=True,
                ready_means_done=True,
                context="service",
            )
            # Exhaustive on purpose, the way `_finish_service_read` is - but
            # `CvValue`/`PagedCvValue` are SUCCESS here, not the terminal
            # failure an earlier version of this ladder treated them as: this
            # hardware answers a service-mode write with exactly the `63 14`
            # direct-CV result the write itself requested
            # (docs/probe-results.md, "Service-mode WRITE works": `write 24
            # 12 00 02 24 -> 63 14 03 24`), never with `61 11`. Raising
            # DecoderNotRespondingError for that reply turned the one write
            # path this station has actually been measured to use into a hard
            # failure - a real answer discarded as no answer. A matching echo
            # still is not proof the decoder RETAINED the value, so it sets
            # `echo_confirmed_only` rather than being trusted the way `Ready`
            # is.
            # The hint no longer says "use POM instead": on this bench POM READ is
            # silence (R1), so that advice sent the operator to a mode that cannot
            # verify. And it names the wheel contact FIRST, because the M8 acceptance
            # proved a 61 13 can be the tool's own timing - now retried automatically
            # before this error is ever raised (see _retry_once_for_unknown_gap).
            hint = no_ack_hint(answered_in_session=self._session_answered)
            if isinstance(outcome, NoAck):
                raise DecoderNoAckError(
                    f"CV{cv} service-mode write: decoder did not acknowledge",
                    hint=hint,
                    cv=cv,
                )
            if isinstance(outcome, (ShortCircuit, TrackShortCircuit)):
                raise ShortCircuitError(
                    f"short circuit on the programming track writing CV{cv}", cv=cv
                )
            if isinstance(outcome, (Busy, StationBusy)):
                raise StationBusyError(f"station busy while writing CV{cv}", cv=cv)
            if isinstance(outcome, TimedOut):
                if outcome.saw_no_ack:
                    raise DecoderNoAckError(
                        f"CV{cv} service-mode write: decoder did not acknowledge",
                        hint=hint,
                        cv=cv,
                    )
                raise DecoderNotRespondingError(
                    f"no confirmation arrived for the CV{cv} service-mode write "
                    f"within {self._station.timing.service_result} s",
                    cv=cv,
                )
            if isinstance(outcome, CvValue):
                if outcome.value != value:
                    raise CvVerifyError(
                        f"CV{cv} service-mode write echoed {outcome.value}, not {value}",
                        cv=cv,
                    )
                echo_confirmed_only = True
            elif isinstance(outcome, PagedCvValue):
                self._station.learn(service_direct_cv=False)
                if cv <= _REGISTER_COLLISION_MAX:
                    raise DecoderNotRespondingError(
                        f"the station fell back to register mode for CV{cv}; register "
                        f"numbers 1-8 are indistinguishable from these CV numbers, so "
                        f"the write cannot be confirmed",
                        cv=cv,
                    )
                if cv > MAX_CV_DIRECT or outcome.raw_register not in echo_candidates(
                    CvEncoding.SERVICE_DIRECT, cv
                ):
                    raise DecoderNotRespondingError(
                        f"CV{cv}: the paged-mode fallback reply ({outcome.raw_register}, "
                        f"{outcome.value}) does not correspond to this CV",
                        cv=cv,
                    )
                if outcome.value != value:
                    raise CvVerifyError(
                        f"CV{cv} service-mode write echoed {outcome.value}, not {value}",
                        cv=cv,
                    )
                echo_confirmed_only = True
            elif not isinstance(outcome, Ready):
                raise DecoderNotRespondingError(
                    f"unexpected reply writing CV{cv} in service mode: {outcome!r}", cv=cv
                )
        finally:
            self.exit_service_mode(
                restore_power=before.track_power, restore_hold=before.emergency_stop
            )
        return encoding, echo_confirmed_only

    def raw_cv_write(self, cv: int, value: int, *, address: int | None, mode: ProgMode) -> None:
        """CV31 and CV32 route through here, never through `ensure_page`.

        Both sit outside `INDEXED_CV_RANGE`, so even a buggy call back into
        `ensure_page` would return immediately rather than loop - but going
        through it at all would be backwards: this IS the mechanism
        `select_page` uses to change the page, not something `ensure_page`
        should gate.
        """
        self._write_and_confirm(cv, value, address=address, mode=mode)

    def _require_page(self, cv: int, page: CvPage | None) -> None:
        """`ensure_page`'s validation, with no wire I/O, split out so a
        caller can fail fast - having sent nothing - ahead of dispatch, while
        the actual CV31/CV32 write stays wherever it is safe to run it. Both
        `cv_read` and `pom_read` call this before either touches the wire;
        `pom_read` alone goes on to call `ensure_page` itself, after its own
        track-power and address checks (see that method's docstring for why
        selecting must not run any earlier).
        """
        if cv in INDEXED_CV_RANGE and page is None:
            raise IndexPageRequiredError(
                f"CV{cv} is behind a ZIMO index page (CV31/CV32); pass --page "
                f"or a CvSpec that carries one",
                cv=cv,
            )

    def ensure_page(
        self, address: int | None, mode: ProgMode, cv: int, page: CvPage | None
    ) -> None:
        if cv not in INDEXED_CV_RANGE:
            return
        self._require_page(cv, page)  # raises when `page` is None; never returns in that case
        self.select_page(page, address=address, mode=mode, force=False)  # type: ignore[arg-type]

    def _page_key(self, address: int | None, mode: ProgMode) -> PageKey:
        """`PageKey`'s own rule, enforced instead of assumed.

        Service mode addresses the TRACK, so one selection is the track's and
        the address that came along for the ride names nothing. `PageKey` says
        as much - "the track as a whole (service mode, where `address` is
        always `None`)" - but nothing used to make it true, and the callers
        disagreed: `cv_write` passed `None`, while `cv_read_many` passed the
        resolved default address. The same physical selection then landed
        under two keys, so a lookup made by one path could not see what the
        other had recorded.
        """
        return PageKey(address=None if mode is ProgMode.SERVICE else address, mode=mode)

    def select_page(
        self,
        page: CvPage,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        force: bool = False,
    ) -> None:
        resolved_mode = resolve_mode(mode, self._station.capabilities, operation="write")
        key = self._page_key(address, resolved_mode)
        cached = self._pages.get(key)
        now = self._station.now()
        if (
            not force
            and cached is not None
            and cached[0] == page
            and (now - cached[1]) < self._station.timing.page_cache_ttl
        ):
            return
        cv31, cv32 = page
        try:
            self.raw_cv_write(31, cv31, address=address, mode=resolved_mode)
            self.raw_cv_write(32, cv32, address=address, mode=resolved_mode)
        except RailctlError:
            self._station.invalidate_caches()
            raise
        first_selection = self._verified_pages.get(key) != page
        if first_selection:
            if self.reads_available(resolved_mode):
                read31 = self.cv_read(31, address=address, mode=resolved_mode)
                read32 = self.cv_read(32, address=address, mode=resolved_mode)
                if (read31.value, read32.value) != page:
                    raise CvVerifyError(
                        f"CV31/CV32 index page selection did not stick: wrote "
                        f"{page}, read back {(read31.value, read32.value)}",
                        cv=31,
                    )
                self._verified_pages[key] = page
            else:
                self._station.emit("page.unverified", {"page": page, "mode": resolved_mode})
        self._pages[key] = (page, now)

    def _service_encoding_for(self, cv: int) -> tuple[CvEncoding, int]:
        """First encoding `SERVICE_ENCODING_ORDER` allows for `cv`, with its page
        index (0 unless the encoding is `SERVICE_EXT`). Shared by
        `service_read_telegram` and `service_write_telegram`, so a station that
        answers `23 11` for reads answers `24 12` for writes through the
        identical gate.

        Every step requires its capability to be exactly `True` - `None` means
        "not established", and an unprobed station never sends an opcode that
        has not been observed to work.

        Z21 first, not third: measured on the reference station, the Z21
        16-bit opcode covers CV1..1024 in one unambiguous field and its
        result arrives unsolicited - the only channel here that cannot
        return a stale stored result. `service_direct` answers nothing at
        all until separately polled with `21 10 31`; leading with it (the
        earlier order) means every read pays for a poll round trip the Z21
        opcode never needs.
        """
        caps = self._station.capabilities
        for field_name, encoding in SERVICE_ENCODING_ORDER:
            if getattr(caps, field_name) is not True:
                continue
            if encoding is CvEncoding.Z21_16BIT and cv <= MAX_CV_Z21:
                return encoding, 0
            if encoding is CvEncoding.SERVICE_DIRECT and cv <= MAX_CV_DIRECT:
                return encoding, 0
            if encoding is CvEncoding.SERVICE_EXT and cv <= MAX_CV_EXT:
                page_index, _c = ext_cv_fields(cv)
                return encoding, page_index
        proven = tuple(name for name, _ in SERVICE_ENCODING_ORDER if getattr(caps, name) is True)
        unprobed = tuple(name for name, _ in SERVICE_ENCODING_ORDER if getattr(caps, name) is None)
        if not proven and unprobed:
            # Not a range error: CV8 is as valid here as anywhere, and this
            # same call succeeds once a probe has run. Issue #16 - the doctor
            # reported the class name and sent a reader after the CV
            # arithmetic instead of after the missing probe.
            #
            # Gated on "nothing proven AND something unprobed", not on "all
            # three unknown". One `61 82` recorded against one encoding while
            # the others were never tried used to skip this branch entirely
            # and reach the message below, which asserts that direct opcodes
            # WORK and cover CV1..255 - about a capability nobody had
            # measured. Naming a state the station never reported is the same
            # defect #16 exists to remove, one step further in.
            raise ServiceEncodingUnknownError(
                f"CV{cv} is not reachable in service mode: no encoding is proven on this "
                f"command station (unprobed: {', '.join(unprobed)}; Z21 would cover "
                f"CV{CV_MIN}..{MAX_CV_Z21}, extended CV{CV_MIN}..{MAX_CV_EXT}, direct "
                f"CV{CV_MIN}..{MAX_CV_DIRECT})",
                hint="run `railctl doctor` to probe the service-mode encodings",
                cv=cv,
            )
        # Both remaining cases share a type because they share a remedy, which
        # is the test this file applies: every service-mode encoding was
        # rejected, or the proven ones do not reach this far. Either way the
        # operator's move is another CV number or `--mode pom`, never a probe.
        reached = (
            f"the encodings this station proved ({', '.join(proven)}) do not reach it"
            if proven
            else "this command station rejected every service-mode opcode (61 82)"
        )
        # The hint checks POM rather than assuming it. A caller who arrived
        # here through AUTO did so BECAUSE `pom_read` is False, so "use
        # `--mode pom`" would send them back to the path that sent them here.
        raise CvOutOfRangeError(
            f"CV{cv} is not reachable in service mode: {reached}",
            hint=(
                "use `--mode pom`"
                if caps.pom_read is not False
                else f"CV{cv} is unreachable on this station: POM is unsupported and the "
                f"service-mode encodings do not reach it"
            ),
            cv=cv,
        )

    def service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]:
        """Choose the wire encoding for a service-mode CV read."""
        encoding, page_index = self._service_encoding_for(cv)
        if encoding is CvEncoding.Z21_16BIT:
            return cmd_z21_cv_read(cv), encoding, page_index
        if encoding is CvEncoding.SERVICE_DIRECT:
            return cmd_service_direct_read(cv), encoding, page_index
        return cmd_service_ext_read(cv), encoding, page_index

    def service_write_telegram(self, cv: int, value: int) -> tuple[bytes, CvEncoding, int]:
        """Choose the wire encoding for a service-mode CV write, through the
        same gate `service_read_telegram` uses."""
        encoding, page_index = self._service_encoding_for(cv)
        if encoding is CvEncoding.Z21_16BIT:
            return cmd_z21_cv_write(cv, value), encoding, page_index
        if encoding is CvEncoding.SERVICE_DIRECT:
            return cmd_service_direct_write(cv, value), encoding, page_index
        return cmd_service_ext_write(cv, value), encoding, page_index

    def exit_service_mode(self, *, restore_power: bool, restore_hold: bool) -> None:
        """Leave service mode and restore the track state this session found.

        Always called from a `finally` by every service-mode caller: a
        `DecoderNoAckError` raised mid-read must still send resume-operations,
        because that is what re-energises the main track, and skipping it
        here would leave the layout dead until the next unrelated command
        happens to touch power.

        `restore_hold` is not optional and has no default, deliberately.
        Resume-operations is the telegram that CLEARS an emergency stop -
        MEASURED 2026-08-09 (docs/probe-results.md, run 5): a locomotive held
        with step 80 stored accelerated away on it, and its stored speed read
        80 both before and after, because the hold keeps the station's refresh
        buffer and never clears it. So every service-mode session on a held
        layout releases the hold halfway through, and this is the method that
        does it. A caller has to say what it found; a default would let the
        next service-mode path silently start a locomotive, which is the
        defect this parameter exists to close.

        The hold goes back BEFORE any power-off, never after: runs 1 and 2
        measured that a stop telegram sent to a dead track changes nothing,
        and runs 3 and 4 measured the same telegram holding stored steps 15
        and 80 on a live one.

        Every exchange in this method uses `TIMING.li_ack_programming`, the
        same 95 s budget as the read itself, through completion: the LI-USB
        rule is that no new command may be sent until the previous one is
        acknowledged, and the station may still be finishing the read's
        internal retries when this runs.

        Stamps `_last_session_end` on the way out, including when the exit
        itself fails: a session that closed badly is still a session the next
        one must not follow too closely.
        """
        try:
            left_service_mode = False
            for _ in range(2):
                self._station.exchange(
                    cmd_track_power_on(), timeout=self._station.timing.li_ack_programming
                )
                self._station.pause(self._station.timing.service_exit_settle)
                if not self._station.status().service_mode:
                    left_service_mode = True
                    break
            if not left_service_mode:
                raise StationBusyError(
                    "the command station is still reporting service mode after "
                    "resume-operations was sent twice"
                )
            if restore_hold:
                self._station.exchange(
                    cmd_emergency_stop_all(), timeout=self._station.timing.li_ack_programming
                )
            if not restore_power:
                # Not optional: the measured state of this hardware is an
                # unpowered bench track, and the station's start mode is
                # automatic, so every service read would otherwise start the
                # locomotives moving.
                self._station.exchange(
                    cmd_track_power_off(), timeout=self._station.timing.li_ack_programming
                )
        finally:
            self._session_answered = False
            self._last_session_end = self._station.now()
            # From here on this instance KNOWS its history: the clock above is
            # authoritative and `_await_session_gap` owns the timing.
            self._session_history_unknown = False
            self._station.invalidate_caches()

    def service_read(
        self, cv: int, *, page: CvPage | None = None, page_selected: bool = False
    ) -> CvResult:
        """Read one CV over the programming track in service mode.

        Service mode is addressed by TRACK, not by locomotive: there is no
        `address` parameter here at all, and nothing in this method sends
        one. `Station.cv_read` still accepts an address for the POM path
        and warns when one is given alongside `mode=SERVICE`.

        Never retried automatically, unlike POM's three attempts: a service
        read already costs up to `TIMING.service_result` (95 s) and the
        command station retries the decoder handshake internally. One
        failing read here is exactly one telegram plus its polls.

        Takes `page` for signature symmetry with `pom_read`/`cv_read`, but
        unlike those two this method never calls `_require_page`: a CV265
        read with `page=None` still succeeds here -
        `test_a_read_in_an_exercised_band_emits_no_note` and
        `test_paged_cv_value_above_max_cv_direct_raises_decoder_not_responding_not_cv_out_of_range`
        (Task 5) both rely on exactly that.

        CV31/CV32 select a decoder-side index window (CV257..512) that
        applies in BOTH POM and service mode - it is a ZIMO decoder feature,
        not a wire-format limit of either opcode. POM addresses CV1..1024
        directly on the wire too (`xbus.cv.MAX_CV_POM`, `pom_cv_fields`), so
        "POM's opcode only reaches one byte" is not why CV31/CV32 exists,
        and a page is exactly as meaningful over service mode as over POM.

        The reason this method cannot honour a given `page` yet is narrower:
        `select_page` over SERVICE routes through `_write_and_confirm`'s
        SERVICE branch, whose `service_write_telegram`/`_status_before` are
        added by Task 6b, not this one. Rather than silently drop a `page`
        this method cannot act on, a non-`None` page emits `page.not_selected`
        so a caller relying on paging in service mode finds out immediately,
        instead of reading whatever page the decoder already had selected.
        """

        def attempt() -> CvResult:
            before = self._station.status()
            try:
                return self._read_in_open_session(cv, page=page, page_selected=page_selected)
            finally:
                self.exit_service_mode(
                    restore_power=before.track_power, restore_hold=before.emergency_stop
                )

        return self._retry_once_for_unknown_gap(attempt)

    def service_read_many(
        self,
        cvs: Sequence[int],
        *,
        on_result: Callable[[CvReadOutcome], None] | None = None,
    ) -> list[CvReadOutcome]:
        """Read several CVs inside ONE service-mode session.

        Not a convenience wrapper around `service_read`: the session count is
        the point. Measured on the bench 2026-08-07 (issue #22), a decoder
        that answers inside an open session stops answering when the session
        is reopened immediately - the caller that read nine CVs as nine
        sessions got one value and eight `61 13`s, while four reads inside a
        single session all succeeded on the same run and the same track.

        One CV failing does not end the batch, and does not close the
        session: each outcome carries its own error, the way `cv_read_many`
        already reports partial failure. The session closes once, in the
        `finally`, whatever happened inside it.

        Two errors DO end it. A short circuit on the programming track and a
        station that reports another operation already running are both
        conditions no later CV in the batch can survive, so continuing would
        send telegrams that cannot work and bury the real fault under eight
        copies of its consequences. They are recorded as that CV's outcome
        and the batch stops there; every CV after it is IN the returned list,
        as `result=None, error=None`. Not attempted is not the same fact as
        failed, and it is not the same as being absent either - a caller that
        got a shorter list could not tell a CV nobody tried from one it
        forgot to ask about. `cv_read_many`'s batched path, the only caller
        that reaches this method, stops with the batch and reports the CVs of
        the sessions it never opened the same way; its per-CV path keeps
        reading, because each of those reads opens its own session anyway -
        here the whole batch shares one.

        No `page` parameter, unlike `service_read`. Every caller so far reads
        CVs below 256, and `service_read` cannot honour a page in service
        mode anyway - it only emits `page.not_selected`. Adding a parameter
        that could not be acted on would promise more than this can do.

        `on_result` is handed each outcome as it is produced, in list order,
        exactly once per element of the returned list - the not-attempted tail
        after a batch-ending error included - and always from INSIDE the open
        session, never after the list is built. `cv_read_many` needs that
        timing, not the finished list: its own `on_progress` is the only
        record a Ctrl-C leaves behind (`cli/commands/backup.py`, `_Collection`),
        so an outcome that arrived only in the return value would be lost with
        the interrupt, and one delivered per chunk would lose up to
        `SERVICE_BATCH_SIZE` CVs that had already answered.
        """
        if not cvs:
            # No session at all rather than an empty one: opening and closing
            # for nothing would stamp `_last_session_end` and make the NEXT
            # real read wait out a gap it does not owe.
            return []
        before = self._station.status()
        outcomes: list[CvReadOutcome] = []

        def record(outcome: CvReadOutcome) -> None:
            outcomes.append(outcome)
            if on_result is not None:
                on_result(outcome)

        try:
            for cv in cvs:
                spec = CvSpec(cv=cv)
                try:
                    result = self._read_in_open_session(cv)
                except BATCH_ENDING_ERRORS as exc:
                    record(CvReadOutcome(spec=spec, result=None, error=exc))
                    # The rest are reported as NOT ATTEMPTED - both fields
                    # None - which is what `CvReadOutcome`'s own docstring
                    # reserves that combination for. Copying the batch-ending
                    # error onto them would claim nine short circuits where
                    # the station reported one, and dropping them from the
                    # list would leave a caller unable to tell a CV that was
                    # never tried from one it forgot to ask about.
                    for skipped in cvs[len(outcomes) :]:
                        record(CvReadOutcome(spec=CvSpec(cv=skipped), result=None, error=None))
                    break
                except RailctlError as exc:
                    record(CvReadOutcome(spec=spec, result=None, error=exc))
                else:
                    record(CvReadOutcome(spec=spec, result=result, error=None))
        finally:
            self.exit_service_mode(
                restore_power=before.track_power, restore_hold=before.emergency_stop
            )
        return outcomes

    def _read_in_open_session(
        self, cv: int, *, page: CvPage | None = None, page_selected: bool = False
    ) -> CvResult:
        """One service-mode read, WITHOUT opening or closing the session.

        Every caller is responsible for `exit_service_mode`, which is why this
        is private: a caller that forgets it leaves the station in service
        mode and the layout dead.
        """
        if page is not None and cv in INDEXED_CV_RANGE and not page_selected:
            # Only an INDEXED CV cares which page is live: below CV257 the
            # argument is ignored by every layer that handles it, so warning
            # that it was not selected reports a non-event. Measured at the
            # bench on 2026-08-18: a restore passing the file's page to every
            # verification read - which is what pins a read to the same bank
            # as its write - decorated a plain CV3 read-back with
            # `page.not_selected`, and an operator reading that envelope
            # cannot tell it from a page that genuinely did not take.
            #
            # It also stays quiet when the caller says it OWNS the selection.
            # `cv read --page` asks the operator to approve a write of
            # CV31/CV32, `cv_read_many` performs exactly that selection, and
            # this warning then told the operator the write they had just
            # approved had not happened (issue #39).
            #
            # `page_selected` is a fact the CALLER states, not one this method
            # can look up. The page cache is the obvious candidate and it does
            # not work: every service read ends its session, and
            # `exit_service_mode` clears that cache, so a cache check
            # suppressed the warning for the first CV of a group and let it
            # fire for every one after it. What is left is the case the
            # warning was written for - a page named for a read that nothing
            # selected.
            self._station.emit("page.not_selected", {"cv": cv, "page": page, "mode": "service"})
        telegram, encoding, page_index = self.service_read_telegram(cv)
        self._await_session_gap()
        start = self._station.now()
        try:
            self._station.exchange(telegram, timeout=self._station.timing.li_ack_programming)
        except UnsupportedCommandError:
            # `Station.exchange` (facade.py) already turns a `61 82` reply
            # into this exception rather than returning `Unsupported` - by
            # the time a reply reaches this method it can never actually
            # BE `Unsupported`. Re-raised with a CV-specific message; the
            # general `61 82` -> `UnsupportedCommandError` mapping is
            # `Station.exchange`'s own docstring, not repeated here.
            raise UnsupportedCommandError(
                f"the command station rejected the service-mode read opcode for CV{cv}"
            ) from None
        matcher = CvMatcher(
            encoding,
            cv,
            page_index=page_index if encoding is CvEncoding.SERVICE_EXT else None,
        )
        outcome = self.await_result(
            matcher,
            timeout=self._station.timing.service_result,
            first_delay=self._station.timing.service_first_poll_delay,
            interval=self._station.timing.service_poll_interval,
            exchange_timeout=self._station.timing.li_ack_programming,
            allow_poll=True,
            ready_means_done=False,
            context="service",
        )
        result = self._finish_service_read(cv, encoding, page_index, outcome, start)
        # Reached only when the read produced a value: `_finish_service_read`
        # raises for every other outcome.
        self._session_answered = True
        return result

    def _finish_service_read(
        self,
        cv: int,
        encoding: CvEncoding,
        page_index: int,
        outcome: object,
        start: float,
    ) -> CvResult:
        # The write path composes the same hint, from the same function.
        hint = no_ack_hint(answered_in_session=self._session_answered)
        if isinstance(outcome, CvValue):
            if encoding is CvEncoding.SERVICE_EXT and page_index in UNEXERCISED_BANDS:
                self._station.emit("cv.unexercised_band", {"cv": cv, "page": page_index})
            return CvResult(
                cv=cv,
                value=outcome.value,
                mode=ProgMode.SERVICE,
                encoding=encoding,
                operation="read",
                verified=None,
                elapsed=self._station.now() - start,
            )
        if isinstance(outcome, PagedCvValue):
            self._station.learn(service_direct_cv=False)
            if cv <= _REGISTER_COLLISION_MAX:
                raise DecoderNotRespondingError(
                    f"the station fell back to register mode for CV{cv}; register "
                    f"numbers 1-8 are indistinguishable from these CV numbers, so "
                    f"the value is not usable",
                    cv=cv,
                )
            if cv <= MAX_CV_DIRECT and outcome.raw_register in echo_candidates(
                CvEncoding.SERVICE_DIRECT, cv
            ):
                return CvResult(
                    cv=cv,
                    value=outcome.value,
                    mode=ProgMode.SERVICE,
                    encoding=CvEncoding.SERVICE_DIRECT,
                    operation="read",
                    verified=None,
                    elapsed=self._station.now() - start,
                )
            raise DecoderNotRespondingError(
                f"CV{cv}: the paged-mode fallback reply ({outcome.raw_register}, "
                f"{outcome.value}) does not correspond to this CV",
                cv=cv,
            )
        if isinstance(outcome, NoAck):
            raise DecoderNoAckError(
                f"CV{cv}: no acknowledgement from the decoder (61 13)",
                hint=hint,
                cv=cv,
            )
        if isinstance(outcome, ShortCircuit):
            raise ShortCircuitError(f"short circuit on the programming track reading CV{cv}", cv=cv)
        if isinstance(outcome, Busy):
            raise StationBusyError(
                f"a programming operation was already running; CV{cv} read did not start",
                cv=cv,
            )
        if isinstance(outcome, TimedOut):
            if outcome.saw_no_ack:
                raise DecoderNoAckError(
                    f"CV{cv}: no acknowledgement from the decoder (61 13)",
                    hint=hint,
                    cv=cv,
                )
            raise DecoderNotRespondingError(
                f"no result arrived for CV{cv} within {self._station.timing.service_result} s",
                cv=cv,
            )
        raise DecoderNotRespondingError(
            f"unexpected reply reading CV{cv} in service mode: {outcome!r}", cv=cv
        )

    def cv_read(
        self,
        cv: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
        page_selected: bool = False,
    ) -> CvResult:
        """Read one CV, choosing POM or service mode through `resolve_mode`.

        `address` matters only on the POM path; service mode is addressed
        by track and never sends one. On the POM path this only validates -
        `self._require_page(cv, page)` raises `IndexPageRequiredError` having
        sent nothing when an indexed CV has no page - and leaves the actual
        selection to `pom_read` itself, which runs it after its own
        track-power and address checks. Selecting here, ahead of those
        checks, would write and cache CV31/CV32 before knowing whether the
        track that write went out on was even powered.

        Service mode does not select a page for `page` at all yet - see
        `service_read`'s own docstring for the current status.
        """
        try:
            resolved_mode = resolve_mode(mode, self._station.capabilities, operation="read")
            if resolved_mode is ProgMode.POM:
                self._require_page(cv, page)
                return self.pom_read(cv, address=address, page=page)
            return self.service_read(cv, page=page, page_selected=page_selected)
        except RailctlError:
            self.invalidate_pages()
            raise

    def cv_write(
        self,
        cv: int,
        value: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
        verify: bool = True,
    ) -> CvResult:
        try:
            resolved_mode = resolve_mode(mode, self._station.capabilities, operation="write")
            if resolved_mode is ProgMode.POM:
                if address is None:
                    raise ValueError(
                        "POM CV write needs a locomotive address: pass --address or set a default"
                    )
                self.ensure_page(address, resolved_mode, cv, page)
                return self.pom_write(cv, value, address=address, verify=verify, page=page)
            self.ensure_page(None, resolved_mode, cv, page)
            return self.service_write(cv, value, verify=verify, page=page)
        except RailctlError:
            self.invalidate_pages()
            raise

    def cv_read_many(
        self,
        specs: Sequence[CvSpec],
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        # Called with one 3-tuple argument, not three positional arguments -
        # `on_progress=list.append` (see tests/station/test_cv_write.py,
        # `test_cv_read_many_calls_on_progress_once_per_spec_and_captures_failures`)
        # is the whole reason the shape is a tuple rather than
        # `Callable[[int, int, CvReadOutcome], None]`.
        on_progress: Callable[[tuple[int, int, CvReadOutcome]], None] | None = None,
    ) -> list[CvReadOutcome]:
        """Read every spec, in sorted order, one outcome per spec.

        Two paths. In service mode with no page anywhere in the batch, the
        specs are read in groups of at most `SERVICE_BATCH_SIZE`, one session
        per group: a service read costs 6.05 s per CV measured at the bench
        2026-08-13 (issue #38, docs/probe-results.md, "A backup costs 6 s per
        CV, and half of it is the session gap"), and about 3.0 s of that is
        `Timing.service_session_gap`, waited out only because every CV opened
        its own session. `_await_session_gap` charges that gap once per
        session, so a group of 16 pays it once instead of 16 times. The M11
        `--all` sweep reads up to 1024 CVs, which is 1 h 42 min at one session
        per CV.

        Everything else keeps the per-CV path, and a spec that carries a page
        always does: page handling lives here (CV31/CV32 read before the first
        selection, `select_page` at the head of each group, the found pair
        re-selected in the `finally`), and `service_read` cannot select a page
        in service mode at all - it only emits `page.not_selected`. See
        `_service_batch_applies`.

        The batched path applies `BATCH_ENDING_ERRORS` across the whole call,
        not only within one session: the CV that met the error carries it and
        every CV after it comes back not attempted (both fields `None`).
        Opening a fresh session for a shorted track or a busy station only
        buries the fault under copies of its own consequences. The per-CV path
        keeps reading, as it always has, because each of its reads opens its
        own session anyway.

        A session that read its whole group and then would not close is
        reported as a `service.session_close_failed` event, not as some CV's
        error: no CV failed, and the next group's first CV - which no telegram
        was ever sent for - must not be written into a backup file as
        tried-and-failed. No further session is opened after it.

        `on_progress` fires once per spec, in sorted order, WHILE the batch is
        still running, on both paths - `cli/commands/backup.py` builds its
        Ctrl-C partial file out of those callbacks alone, because the returned
        list dies with the interrupt.
        """
        for spec in specs:
            if spec.cv in PAGE_SELECTOR_CVS:
                raise ValueError(
                    f"CV{spec.cv} is a ZIMO page cursor (CV31/CV32), not a "
                    f"setting; cv_read_many refuses to read it as part of a "
                    f"payload"
                )
        ordered = sorted(specs, key=lambda spec: (spec.page or (0, 0), spec.cv))
        if self._service_batch_applies(ordered, mode=mode):
            return self._read_specs_in_service_sessions(ordered, on_progress=on_progress)
        # Leave-as-found: a batch that will select a page writes CV31/CV32,
        # and a read's job is to leave the decoder as it found it. The pair is
        # read BEFORE the first selection - CV31/CV32 are plain 1-255 CVs,
        # readable as singletons through `cv_read` (the payload guard above is
        # about batching them BETWEEN selections, not about reading them) -
        # and re-selected in the `finally`, so an assertion or error inside
        # the batch cannot leave the cursor pointing at this batch's page. A
        # failure reading the pair propagates: selecting a page whose original
        # cannot be restored is exactly the mutation-dressed-as-a-read this
        # exists to prevent.
        found_page: CvPage | None = None
        if any(spec.page is not None for spec in ordered):
            found_page = (
                self.cv_read(31, address=address, mode=mode).value,
                self.cv_read(32, address=address, mode=mode).value,
            )
        total = len(ordered)
        outcomes: list[CvReadOutcome] = []
        current_page: CvPage | None = None
        try:
            for index, spec in enumerate(ordered):
                try:
                    if spec.page != current_page:
                        if spec.page is not None:
                            self.select_page(spec.page, address=address, mode=mode, force=True)
                        current_page = spec.page
                    result = self.cv_read(
                        spec.cv,
                        address=address,
                        mode=mode,
                        page=spec.page,
                        # This loop selected it, one branch above, and keeps it
                        # selected for the whole group. The page cache cannot
                        # answer this: every read ends its session, and
                        # `exit_service_mode` clears the cache, so only the
                        # FIRST read of a group would ever find a record there
                        # (checked, 2026-08-19). The caller that did the
                        # selecting is the one that knows.
                        page_selected=spec.page is not None,
                    )
                    outcome = CvReadOutcome(spec=spec, result=result, error=None)
                except RailctlError as exc:
                    outcome = CvReadOutcome(spec=spec, result=None, error=exc)
                outcomes.append(outcome)
                if on_progress is not None:
                    on_progress((index, total, outcome))
        finally:
            if found_page is not None:
                # Through `select_page`, not raw writes: the restore deserves
                # the same verification and cache bookkeeping as any other
                # selection. A restore that fails raises out of this finally -
                # loudly - because the decoder is then NOT as it was found,
                # and reporting the batch as if it were would be the lie.
                self.select_page(found_page, address=address, mode=mode, force=True)
        return outcomes

    def _service_batch_applies(self, ordered: Sequence[CvSpec], *, mode: ProgMode) -> bool:
        """Whether `cv_read_many` may read `ordered` as service-mode groups.

        Every spec must carry `page is None`, and the reason is not that a
        paged batch would be hard: `service_read` cannot select a page in
        service mode at all, it only emits `page.not_selected`, so the
        selection a paged spec needs happens in `cv_read_many`'s own per-CV
        loop and nowhere else. Backup (`cli/commands/backup.py`) and diff
        (`cli/commands/diff.py`) both build `CvSpec(cv=..., name=...)` with no
        page, so the batched path already covers every caller whose 6 s per CV
        was measured; teaching the batch to page would add a second selection
        path that no caller exercises.

        A `resolve_mode` failure is not decided here. It falls through to the
        per-CV path, where the same call raises inside the loop and is
        reported as one error outcome per spec, exactly as it was before this
        batching existed.
        """
        if not ordered:
            return False
        if any(spec.page is not None for spec in ordered):
            return False
        try:
            resolved = resolve_mode(mode, self._station.capabilities, operation="read")
        except RailctlError:
            return False
        return resolved is ProgMode.SERVICE

    def _read_specs_in_service_sessions(
        self,
        ordered: Sequence[CvSpec],
        *,
        on_progress: Callable[[tuple[int, int, CvReadOutcome]], None] | None,
    ) -> list[CvReadOutcome]:
        """`cv_read_many`'s batched path: one session per group of at most
        `SERVICE_BATCH_SIZE` CVs, instead of one session per CV.

        Measured 2026-08-13 (issue #38): `railctl backup` cost 6.05 s per CV,
        about 3.0 s of it the decoder read and about 3.0 s
        `Timing.service_session_gap`. The gap is owed once per SESSION, so
        grouping the reads is what removes it.
        """
        total = len(ordered)
        outcomes: list[CvReadOutcome] = []

        def report(outcome: CvReadOutcome) -> None:
            # `index` counts across the whole call, not per chunk, and the
            # callback goes out here rather than from a loop over the finished
            # list: a Ctrl-C takes the return value with it, and the rows of a
            # partial backup file come from these callbacks alone.
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress((len(outcomes) - 1, total, outcome))

        pending = list(ordered)
        aborted = False
        if self._session_history_unknown:
            # The first read of an invocation goes through `service_read`,
            # which carries `_retry_once_for_unknown_gap`: this instance cannot
            # know when the PREVIOUS invocation's session closed, and a session
            # opened too soon fails wholesale with `61 13` on every CV in it.
            # Retrying a whole chunk instead would re-run reads the first
            # attempt had already reported through `on_progress`, so the same
            # CV would arrive twice with two different outcomes - the retry
            # stays where it is already one CV wide, and the group follows it.
            first = pending.pop(0)
            try:
                result = self.service_read(first.cv)
            except BATCH_ENDING_ERRORS as exc:
                report(CvReadOutcome(spec=first, result=None, error=exc))
                aborted = True
            except RailctlError as exc:
                report(CvReadOutcome(spec=first, result=None, error=exc))
            else:
                report(CvReadOutcome(spec=first, result=result, error=None))
        while not aborted and pending:
            chunk, pending = pending[:SERVICE_BATCH_SIZE], pending[SERVICE_BATCH_SIZE:]
            before_chunk = len(outcomes)
            try:
                aborted = self._read_chunk_in_one_session(chunk, report)
            except RailctlError as exc:
                # Opening the session and closing it are the only two steps
                # that can raise out of `_read_chunk_in_one_session` - every
                # read inside it becomes an outcome. Either way no further
                # session is opened, and WHICH of the two failed decides who
                # carries the error. The count is taken against THIS chunk,
                # never against the whole call: `len(outcomes) < total` is
                # still true after a full chunk whenever another chunk is
                # pending, and it pointed the error at the next chunk's first
                # spec - a CV no telegram was ever sent for.
                aborted = True
                reported_here = len(outcomes) - before_chunk
                if reported_here < len(chunk):
                    # The session never opened, or died with CVs of this chunk
                    # still unread: the first of those carries the error, the
                    # same shape the per-CV path gave it.
                    report(CvReadOutcome(spec=chunk[reported_here], result=None, error=exc))
                else:
                    # Every CV in this chunk answered and only the CLOSE
                    # failed, so the error belongs to no CV: attaching it to
                    # one would either throw away a value that was measured or
                    # (worse) mark a never-attempted CV as tried-and-failed,
                    # which `backup/mapping.py::status_for` writes into the
                    # file as `error` instead of "not attempted". Dropping it
                    # is not the alternative - after a `StationBusyError` here
                    # the station is still in service mode and the main track
                    # is dead, and nothing else in the returned list says so.
                    # Same rule as `cli/deps.py::close_after`: the values the
                    # run measured survive, and the close failure rides beside
                    # them as a warning.
                    # The GROUP is named, not one CV. "The read after which it
                    # failed" would be a guess: a group that met a
                    # batch-ending error partway through reported its own
                    # tail as not attempted, so its last CV was never on the
                    # wire and naming it would put a CV nobody read at the
                    # scene of the failure.
                    self._station.emit(
                        "service.session_close_failed",
                        {
                            "reason": str(exc),
                            "group": [chunk[0].cv, chunk[-1].cv],
                            "not_attempted": len(pending),
                        },
                    )
        # Whatever is left after an abort was never attempted - both fields
        # None, never a copy of the error that stopped the run.
        for skipped in ordered[len(outcomes) :]:
            report(CvReadOutcome(spec=skipped, result=None, error=None))
        return outcomes

    def _read_chunk_in_one_session(
        self, chunk: Sequence[CvSpec], report: Callable[[CvReadOutcome], None]
    ) -> bool:
        """Read one group inside one session; say whether the batch ended.

        `service_read_many` reports each outcome against the bare
        `CvSpec(cv=...)` it was handed, while the caller's spec also carries
        the name a backup row is written under, so each outcome is re-tagged
        with the spec it came from, in order, as it arrives.

        A `BATCH_ENDING_ERRORS` member on any outcome means the batch ended;
        the caller stops instead of opening the next session. Read off the
        ERROR TYPE, not off the not-attempted tail `service_read_many` leaves
        behind it: that tail is `cvs[len(outcomes):]`, which is empty when the
        CV that met the error was the LAST of the chunk, so one failure
        position in every `SERVICE_BATCH_SIZE` produced no marker at all and
        the next session was opened onto a shorted track.
        """
        remaining = iter(chunk)

        def deliver(outcome: CvReadOutcome) -> None:
            spec = next(remaining, None)
            if spec is None:
                # `service_read_many` returns exactly one outcome per CV it
                # was given, so this is unreachable while both halves agree.
                # It is spelled out because the alternative is a bare
                # `StopIteration` raised from inside a callback, which says
                # nothing about which two counts disagreed.
                raise RuntimeError(
                    f"service_read_many reported more outcomes than the {len(chunk)} "
                    f"CVs it was given"
                )
            report(CvReadOutcome(spec=spec, result=outcome.result, error=outcome.error))

        outcomes = self.service_read_many([spec.cv for spec in chunk], on_result=deliver)
        return any(isinstance(outcome.error, BATCH_ENDING_ERRORS) for outcome in outcomes)

    def _learn_result_channel(
        self, context: Literal["pom", "service"], channel: ResultChannelSeen
    ) -> None:
        """Only a POM read's channel is a durable fact.

        A `61 82` answer to a service-mode `21 10` poll is the *expected*
        reply from a station that pushes its result instead of answering the
        poll directly - recording it here would misfile an ordinary
        service-mode moment as a POM capability. Called only on an actual
        match, never on `61 82` itself, so the conditional-polling branch in
        `await_result` never has to touch capabilities at all.
        """
        if context == "pom":
            self._station.learn(pom_result_channel=channel)

    def _consider(
        self,
        reply: Reply,
        matcher: CvMatcher,
        *,
        context: Literal["pom", "service"],
        channel: ResultChannelSeen,
    ) -> Reply | None:
        """One reply, turned into an outcome, or `None` meaning "not an
        answer, keep waiting". `TRANSIENT_REPLIES` (`Busy`, `StationBusy`,
        `ShortCircuit`, `TrackShortCircuit`, `TransferError`) says nothing
        about support one way or the other - except `ShortCircuit` and
        `TrackShortCircuit`, which end the whole operation, so those two are
        pulled out first and handled as terminal. `PagedCvValue` is also
        terminal, unconditionally and with no matcher check: it is a real
        answer - the `63 10` paged form Task 5's service-mode reads use - and
        this loop is the only place either mode ever gets to see one.
        Swallowing it here (returning `None` and letting the deadline turn it
        into `TimedOut`) would make Task 5's whole paged-read branch
        unreachable while looking, from the outside, like an ordinary
        timeout - the exact "real answer read as no answer" failure this
        project exists to catch. `GenericAck` and `Other` fall through to the
        final `None`: neither settles the question, and the caller's own
        deadline is what eventually turns silence into `TimedOut`, not this
        function.
        """
        if isinstance(reply, CvValue):
            if matcher(reply):
                self._learn_result_channel(context, channel)
                return reply
            self._station.emit(
                "cv.stale_result",
                {"cv": matcher.cv, "raw_cv": reply.raw_cv, "encoding": matcher.encoding.name},
            )
            return None
        if isinstance(reply, PagedCvValue):
            return reply
        if isinstance(reply, (ShortCircuit, TrackShortCircuit, Ready, NoAck)):
            return reply
        if reply in TRANSIENT_REPLIES:
            return None
        return None

    def await_result(
        self,
        matcher: CvMatcher,
        *,
        timeout: float,
        first_delay: float,
        interval: float,
        exchange_timeout: float,
        allow_poll: bool,
        ready_means_done: bool,
        context: Literal["pom", "service"],
    ) -> WaitOutcome:
        """The wait loop POM (this task) and service mode (Task 5) share.

        Each pass drains whatever is already sitting on the port
        (`link.poll(0.0)`) before deciding whether to poll again. Polling is
        conditional: `21 10 31` answered `61 82` means the station only pushes
        results, so `polling` is switched off for the rest of THIS attempt and
        the loop falls back to a passive `link.poll(...)` wait, which is what
        lets a push-only station's answer still be caught. Every exchange this
        function issues is clamped to `max(timing.min_exchange,
        min(exchange_timeout, remaining))`, and a `LinkTimeout` from one ends
        that pass rather than escaping - the next loop iteration's own
        `remaining <= 0` check is what turns it into `TimedOut`.
        """
        timing = self._station.timing
        deadline = self._station.now() + timeout
        if first_delay:
            self._station.pause(first_delay)
        polling = allow_poll
        polls = 0
        ready_streak = 0
        saw_no_ack = False

        def settle(reply: Reply, *, channel: ResultChannelSeen) -> Reply | None:
            nonlocal ready_streak, saw_no_ack
            outcome = self._consider(reply, matcher, context=context, channel=channel)
            if outcome is None:
                return None
            if isinstance(outcome, NoAck):
                saw_no_ack = True
                return outcome
            if isinstance(outcome, Ready):
                if ready_means_done:
                    return outcome
                ready_streak += 1
                if ready_streak >= timing.service_ready_limit:
                    saw_no_ack = True
                    return NoAck()
                return None
            return outcome

        while True:
            for frame in self._station.link.poll(0.0):
                settled = settle(parse(frame.payload), channel="broadcast")
                if settled is not None:
                    return settled
            remaining = deadline - self._station.now()
            if remaining <= 0:
                return TimedOut(polls=polls, ready_streak=ready_streak, saw_no_ack=saw_no_ack)
            budget = max(timing.min_exchange, min(exchange_timeout, remaining))
            if polling:
                polls += 1
                try:
                    reply = self._station.exchange(cmd_service_result_request(), timeout=budget)
                except LinkTimeout:
                    continue
                except UnsupportedCommandError:
                    # `Station.exchange` (facade.py) already turns a `61 82`
                    # reply into this exception rather than returning
                    # `Unsupported` - by the time a reply reaches this method
                    # it can never actually BE `Unsupported`. The station
                    # only pushes its result once polling has been switched
                    # off, so this ends the attempt's polling, not the read:
                    # never recorded as a durable capability (a poll's 61 82
                    # is the expected shape from a push-only station), and
                    # never raised past this loop.
                    polling = False
                    continue
                settled = settle(reply, channel="poll")
                if settled is not None:
                    return settled
                self._station.pause(interval)
            else:
                remaining = deadline - self._station.now()
                if remaining <= 0:
                    return TimedOut(polls=polls, ready_streak=ready_streak, saw_no_ack=saw_no_ack)
                for frame in self._station.link.poll(min(interval, remaining)):
                    settled = settle(parse(frame.payload), channel="broadcast")
                    if settled is not None:
                        return settled

    def _drain_stale(self, matcher: CvMatcher) -> None:
        """Clear whatever is sitting on the port before this attempt's own
        telegram goes out.

        `Link.drain()` is exactly `poll(0.0)` with the return value thrown
        away (see link.py); this needs the frames back, because a `CvValue`
        left over from an earlier request must be reported through
        `cv.stale_result`, not silently swallowed. Anything else pending (an
        old ack, a broadcast) is already logged in `Link.stats()` and needs no
        further action.
        """
        for frame in self._station.link.poll(0.0):
            reply = parse(frame.payload)
            if isinstance(reply, CvValue):
                self._station.emit(
                    "cv.stale_result",
                    {"cv": matcher.cv, "raw_cv": reply.raw_cv, "encoding": matcher.encoding.name},
                )

    def pom_read(
        self, cv: int, *, address: int | None = None, page: CvPage | None = None
    ) -> CvResult:
        started = self._station.now()
        capabilities = self._station.capabilities
        if capabilities.pom_read is False:
            # Naming *where* this refusal comes from matters: "POM does not
            # work" read on its own looks like a bug report against this
            # tool, not a fact this same station already taught it. Naming
            # the file, when it was probed, and the note recorded at the time
            # is what turns the message into something the operator can go
            # verify or clear (`railctl doctor` re-probes and overwrites it).
            probed = capabilities.probed_at or "an unknown time"
            note = f"; {capabilities.notes[-1]}" if capabilities.notes else ""
            raise PomReadUnsupportedError(
                f"POM reads are recorded as unsupported for this station in "
                f"capabilities.json (probed {probed}{note}) - CV{cv}",
                hint="put the loco on the programming track and use `--mode service`",
                cv=cv,
            )
        # Resolved and validated (never selected) before anything hits the
        # wire: an indexed CV with no page must raise IndexPageRequiredError
        # having sent nothing. Selecting the page itself - the CV31/CV32
        # writes `ensure_page` below issues - is deliberately NOT here: it
        # comes after the track-power and address checks, because those
        # writes go out on the same track a POM read does, and a page
        # selected (and then cached as selected) against a track that turns
        # out to be unpowered is a page the decoder never actually received.
        resolved = self._station.resolve_address(address)
        self._require_page(cv, page)
        # Built before the track-power check below, not after: `status()`
        # calls `self._station.exchange(...)`, whose own `Link.request()`
        # drains the port before writing anything (link.py) - a stale CV
        # reply left over from an earlier read would otherwise be discarded
        # right there, silently, before this method's own drain ever got a
        # turn to see and report it.
        matcher = CvMatcher(
            CvEncoding.POM_ZERO_BASED, cv, zero_based=capabilities.pom_echo_zero_based
        )
        self._drain_stale(matcher)
        if not self._station.status().track_power:
            raise TrackPowerError(
                "POM needs the main track powered; run `railctl power on`",
                hint="run `railctl power on`",
            )
        if resolved is None:
            raise ValueError(
                f"POM read of CV{cv} needs a locomotive address: pass address= "
                f"or set a default address"
            )
        # Only now - track powered, address resolved - is it safe to select
        # and cache a page.
        self.ensure_page(resolved, ProgMode.POM, cv, page)

        timing = self._station.timing
        saw_no_ack = False
        for attempt in range(1, timing.pom_read_attempts + 1):
            self._drain_stale(matcher)
            telegram = cmd_pom_read_byte(resolved, cv, threshold=self._station.threshold)
            try:
                reply = self._station.exchange(telegram, timeout=timing.li_ack_normal)
            except UnsupportedCommandError:
                # `Station.exchange` (facade.py) already turns a `61 82`
                # reply into this exception rather than returning
                # `Unsupported` - by the time a reply reaches this method it
                # can never actually BE `Unsupported`. This is the ONLY reply
                # that entitles this method to write `pom_read=False`: three
                # silent attempts stay `None` (see the bottom of this loop),
                # never `False`.
                self._station.learn(pom_read=False, pom_read_provenance="unsupported")
                raise PomReadUnsupportedError(
                    f"the command station answered `61 82` to a POM read of CV{cv}",
                    hint="put the loco on the programming track and use `--mode service`",
                    cv=cv,
                    details={
                        "cv": cv,
                        "address": resolved,
                        "mode": "pom",
                        "attempts": attempt,
                        "attempt_timeout_s": timing.pom_result,
                    },
                ) from None
            settled = self._consider(reply, matcher, context="pom", channel="broadcast")
            outcome: WaitOutcome = (
                settled
                if settled is not None
                else self.await_result(
                    matcher,
                    timeout=timing.pom_result,
                    first_delay=0.0,
                    interval=timing.pom_poll_interval,
                    exchange_timeout=timing.li_ack_normal,
                    allow_poll=True,
                    ready_means_done=False,
                    context="pom",
                )
            )
            if isinstance(outcome, CvValue):
                # The provenance is cleared with the verdict it explained, not
                # left behind: it says HOW a `False` was reached, and there is
                # no longer a `False`. Unconditional on purpose. Today a set
                # provenance almost always implies `pom_read is False`, which
                # short-circuits above before this line can run - but that
                # makes the invariant an argument about reachability rather
                # than a property of the code, and a capabilities file holding
                # `pom_read: null` beside a provenance reaches it directly.
                learned: dict[str, object] = {"pom_read": True, "pom_read_provenance": None}
                if capabilities.pom_echo_zero_based is None:
                    zero_based = matcher.echo_says_zero_based(outcome)
                    if zero_based is not None:
                        learned["pom_echo_zero_based"] = zero_based
                self._station.learn(**learned)
                return CvResult(
                    cv=cv,
                    value=matcher.value_of(outcome),
                    mode=ProgMode.POM,
                    encoding=CvEncoding.POM_ZERO_BASED,
                    operation="read",
                    verified=None,
                    elapsed=self._station.now() - started,
                )
            if isinstance(outcome, (ShortCircuit, TrackShortCircuit)):
                raise ShortCircuitError(
                    f"short circuit reading CV{cv} over POM",
                    cv=cv,
                    details={
                        "cv": cv,
                        "address": resolved,
                        "mode": "pom",
                        "attempts": attempt,
                        "attempt_timeout_s": timing.pom_result,
                    },
                )
            if isinstance(outcome, NoAck) or (isinstance(outcome, TimedOut) and outcome.saw_no_ack):
                saw_no_ack = True
            if attempt < timing.pom_read_attempts:
                self._station.pause(timing.pom_retry_delay)
        # These are the two failure paths that ran out the clock rather than
        # getting a definite refusal - a script's only way to tell "gave up
        # after one attempt" from "gave up after three" is `details["attempts"]`,
        # since the message text and exit code are otherwise identical either
        # way.
        failure_details = {
            "cv": cv,
            "address": resolved,
            "mode": "pom",
            "attempts": timing.pom_read_attempts,
            "attempt_timeout_s": timing.pom_result,
        }
        if saw_no_ack:
            raise DecoderNoAckError(
                f"the decoder did not acknowledge the POM read of CV{cv}",
                cv=cv,
                details=failure_details,
            )
        raise DecoderNotRespondingError(
            f"CV{cv} produced no result over POM after {timing.pom_read_attempts} "
            f"attempts (interface ack only; docs/probe-results.md, R1)",
            cv=cv,
            details=failure_details,
        )

    @staticmethod
    def _blind_reason(cv: int, verify: bool) -> str:
        if not verify:
            return "verify=False: no read-back was attempted"
        if cv in BLIND_WRITE_CVS:
            return f"CV{cv} has no reliable read-back on this station"
        return (
            "CV29 bit 5 changes the answering address; a read-back would ask the wrong locomotive"
        )

    def pom_write(
        self,
        cv: int,
        value: int,
        *,
        address: int,
        verify: bool,
        page: CvPage | None = None,
    ) -> CvResult:
        started = self._station.now()
        blind = not verify or cv in BLIND_WRITE_CVS
        old_value: int | None = None
        if verify and not blind:
            capabilities = self._station.capabilities
            if capabilities.pom_read is False:
                raise PomReadUnsupportedError(
                    f"CV{cv} POM write cannot be verified",
                    hint=(
                        "cannot verify POM writes on this station; re-run with "
                        "`--no-verify` or use `--mode service`"
                    ),
                    cv=cv,
                )
            # CV29 needs the pre-write value even when pom_read is already
            # known True, to detect a bit-5 (long/short address) flip; every
            # other CV only needs this read when pom_read is unestablished.
            if capabilities.pom_read is None or cv == 29:
                # Narrowed to the three failure shapes that genuinely mean "the
                # probing read did not work" - a broken instrument, not a
                # station capability. `TrackPowerError`, `IndexPageRequiredError`,
                # `ShortCircuitError`, `CvVerifyError` and `ValueError` all
                # propagate unchanged: each names a real fault of its own, and
                # reporting it as "this station cannot verify POM writes" would
                # hide it from the operator behind a capability verdict the
                # station never gave.
                try:
                    old_value = self.pom_read(cv, address=address, page=page).value
                except (
                    DecoderNotRespondingError,
                    DecoderNoAckError,
                    PomReadUnsupportedError,
                ) as exc:
                    if self._station.capabilities.pom_read is True:
                        # A known-working capability failing once is a real
                        # fault (e.g. DecoderNotRespondingError), not grounds
                        # to claim POM verification is unsupported.
                        raise
                    raise PomReadUnsupportedError(
                        f"CV{cv} POM write cannot be verified",
                        hint=(
                            "cannot verify POM writes on this station; re-run "
                            "with `--no-verify` or use `--mode service`"
                        ),
                        cv=cv,
                    ) from exc
            if cv == 29 and old_value is not None:
                if (old_value ^ value) & (1 << CV29_LONG_ADDRESS_BIT):
                    blind = True
        try:
            encoding, _echo_confirmed_only = self._write_and_confirm(
                cv, value, address=address, mode=ProgMode.POM
            )
        except RailctlError:
            self._station.invalidate_caches()
            raise
        if cv == 8 or cv in ADDRESS_CVS:
            self._station.invalidate_caches()
        if blind:
            self._station.emit(
                "cv.write_unverified",
                {"cv": cv, "value": value, "reason": self._blind_reason(cv, verify)},
            )
            # `None`, not `False`: no read-back ran, so nothing measured a
            # mismatch. `False` would claim one - a real mismatch raises
            # `CvVerifyError` below instead of ever returning.
            return CvResult(
                cv=cv,
                value=value,
                mode=ProgMode.POM,
                encoding=encoding,
                operation="write",
                verified=None,
                elapsed=self._station.now() - started,
            )
        read = self.cv_read(cv, address=address, mode=ProgMode.POM, page=page)
        if read.value != value:
            self._station.pause(self._station.timing.pom_write_settle)
            read = self.cv_read(cv, address=address, mode=ProgMode.POM, page=page)
            if read.value != value:
                raise CvVerifyError(
                    f"CV{cv} write verification failed twice: expected {value}, "
                    f"read back {read.value}",
                    cv=cv,
                )
        return CvResult(
            cv=cv,
            value=value,
            mode=ProgMode.POM,
            encoding=encoding,
            operation="write",
            verified=True,
            elapsed=self._station.now() - started,
        )

    def _status_before(self) -> StationStatus:
        """Read `21 24 05` once, self-contained: `service_write` needs this
        BEFORE entering service mode (to know what to restore afterwards, spec
        line 782), and it is simple enough not to need `await_result`'s poll
        machinery - a status reply is immediate, never paged.

        Returns the whole status rather than one bit. `exit_service_mode`
        restores two things - the track power and the hold - and reading them
        from one reply is what keeps them describing the same moment; a second
        round trip for the second bit would also cost a telegram on a link
        whose rule is one command at a time.
        """
        reply = self._station.exchange(
            cmd_station_status(), timeout=self._station.timing.li_ack_normal
        )
        if not isinstance(reply, StationStatus):
            raise ProtocolError(f"expected a station status reply, got {reply!r}")
        return reply

    def service_write(
        self, cv: int, value: int, *, verify: bool, page: CvPage | None = None
    ) -> CvResult:
        """`verified=True` comes from one of two channels: a decoder-level
        `Ready` (`61 11`) acknowledgement, or - when the station confirmed the
        write only through its own result echo - an independent `cv_read`
        performed here afterwards, whose value must match what was written.
        A matching `CvValue`/`PagedCvValue` echo alone is the weaker signal:
        it shows what the station's own result store holds, not that the
        decoder retained the value (docs/probe-results.md, "Service-mode
        WRITE works"), so `_write_and_confirm`'s `echo_confirmed_only` return
        is what decides whether the read-back has to run. A read-back that
        disagrees raises `CvVerifyError` naming both values; `verify=False`
        skips the read-back and reports `verified=None` - not measured, never
        a mismatch nobody measured.

        `BLIND_WRITE_CVS` - which exists to skip an unreliable POM re-read
        after a write that could change the answering address - has nothing to
        skip here: service mode is addressed by track, so the read-back asks
        the same decoder whatever was written.

        `page` is accepted, not forwarded to the read-back: `cv_write`'s own
        `ensure_page` call already selected it before this method is reached,
        and `service_read` cannot re-select a page - handing it one only
        emits `page.not_selected` for a page that IS selected.
        """
        started = self._station.now()

        def attempt() -> tuple[CvEncoding, bool]:
            return self._write_and_confirm(cv, value, address=None, mode=ProgMode.SERVICE)

        try:
            encoding, echo_confirmed_only = self._retry_once_for_unknown_gap(attempt)
        except RailctlError:
            self._station.invalidate_caches()
            raise
        if cv == 8 or cv in ADDRESS_CVS:
            # Service mode ignores address, and the decoder on the programming
            # track is not necessarily the one on the main track (spec line
            # 782) - there is no address to narrow to, so the whole cache goes.
            self._station.invalidate_caches()
        if not verify:
            self._station.emit(
                "cv.write_unverified",
                {"cv": cv, "value": value, "reason": self._blind_reason(cv, verify)},
            )
            return CvResult(
                cv=cv,
                value=value,
                mode=ProgMode.SERVICE,
                encoding=encoding,
                operation="write",
                verified=None,
                elapsed=self._station.now() - started,
            )
        if echo_confirmed_only:
            read = self.cv_read(cv, address=None, mode=ProgMode.SERVICE)
            if read.value != value:
                raise CvVerifyError(
                    f"CV{cv} write verification failed: wrote {value}, the independent "
                    f"read-back returned {read.value}",
                    cv=cv,
                    details={"wrote": value, "read_back": read.value},
                )
        return CvResult(
            cv=cv,
            value=value,
            mode=ProgMode.SERVICE,
            encoding=encoding,
            operation="write",
            verified=True,
            elapsed=self._station.now() - started,
        )
