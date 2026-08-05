"""The shared CV wait loop, the echo matcher, POM reads, and AUTO mode
resolution (design doc lines 708-743, 786-787, 916-924).

`station/` may hold no framing bytes, no port names, and no CV arithmetic
(`tests/test_layering.py`, rules 1 and 2) - `xbus.cv`'s `echo_candidates`,
`decode_echo` and `result_ident_for` are what let this module compare CV
numbers and reply idents without ever touching a wire byte itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from railctl.errors import (
    CvOutOfRangeError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    ShortCircuitError,
    StationBusyError,
    TrackPowerError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import CvPage, CvResult, ProgMode
from railctl.xbus.commands import (
    cmd_pom_read_byte,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_service_result_request,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
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
    TrackShortCircuit,
    parse,
)

if TYPE_CHECKING:
    from railctl.station.facade import Station

__all__ = [
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

SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] = (
    ("z21_cv_opcodes", CvEncoding.Z21_16BIT),
    ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT),
)
UNEXERCISED_BANDS: Final[frozenset[int]] = frozenset({2, 3})  # 63 16 / 63 17, never answered here
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
    """
    if mode is not ProgMode.AUTO:
        return mode
    if capabilities.pom_read is not False:
        return ProgMode.POM
    if capabilities.service_direct_cv is True:
        return ProgMode.SERVICE
    raise PomReadUnsupportedError(
        f"POM is unsupported on this command station for a CV {operation}; put "
        f"the loco on the programming track and use `--mode service`",
        hint="put the loco on the programming track and use `--mode service`",
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
        self._verified_pages: set[PageKey] = set()

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

    def service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]:
        """Choose the wire encoding for a service-mode CV read.

        Z21 first, not third: measured on the reference station, the Z21
        16-bit opcode covers CV1..1024 in one unambiguous field and its
        result arrives unsolicited - the only channel here that cannot
        return a stale stored result. `service_direct` answers nothing at
        all until separately polled with `21 10 31`; leading with it (the
        earlier order) means every read pays for a poll round trip the Z21
        opcode never needs.

        Every step requires its capability to be exactly True. `None` means
        "not established" - docs/probe-results.md distinguishes true, false
        and unknown throughout - and an unprobed station never sends an
        opcode that has not been observed to work.
        """
        capabilities = self._station.capabilities
        if capabilities.z21_cv_opcodes is True and cv <= MAX_CV_Z21:
            return cmd_z21_cv_read(cv), CvEncoding.Z21_16BIT, 0
        if capabilities.service_direct_cv is True and cv <= MAX_CV_DIRECT:
            return cmd_service_direct_read(cv), CvEncoding.SERVICE_DIRECT, 0
        if capabilities.service_ext_cv is True and cv <= MAX_CV_EXT:
            page, _ = ext_cv_fields(cv)
            return cmd_service_ext_read(cv), CvEncoding.SERVICE_EXT, page
        if all(getattr(capabilities, name) is None for name, _ in SERVICE_ENCODING_ORDER):
            raise CvOutOfRangeError(
                f"CV{cv} is not reachable in service mode: no encoding has been "
                f"probed on this command station (Z21 covers CV{CV_MIN}..{MAX_CV_Z21}, "
                f"extended CV{CV_MIN}..{MAX_CV_EXT}, direct CV{CV_MIN}..{MAX_CV_DIRECT}, "
                f"all unknown)",
                hint="run `railctl doctor` to probe the service-mode encodings",
                cv=cv,
            )
        raise CvOutOfRangeError(
            f"CV{cv} is not reachable in service mode on this command station "
            f"(no extended or Z21 CV opcodes; direct opcodes only cover "
            f"CV{CV_MIN}..{MAX_CV_DIRECT})",
            hint="use `--mode pom`",
            cv=cv,
        )

    def exit_service_mode(self, *, restore_power: bool) -> None:
        """Leave service mode and restore the pre-operation track power state.

        Always called from a `finally` by every service-mode caller: a
        `DecoderNoAckError` raised mid-read must still send resume-operations,
        because that is what re-energises the main track, and skipping it
        here would leave the layout dead until the next unrelated command
        happens to touch power.

        Every exchange in this method uses `TIMING.li_ack_programming`, the
        same 95 s budget as the read itself, through completion: the LI-USB
        rule is that no new command may be sent until the previous one is
        acknowledged, and the station may still be finishing the read's
        internal retries when this runs.
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
            if not restore_power:
                # Not optional: the measured state of this hardware is an
                # unpowered bench track, and the station's start mode is
                # automatic, so every service read would otherwise start the
                # locomotives moving.
                self._station.exchange(
                    cmd_track_power_off(), timeout=self._station.timing.li_ack_programming
                )
        finally:
            self._station.invalidate_caches()

    def service_read(self, cv: int) -> CvResult:
        """Read one CV over the programming track in service mode.

        Service mode is addressed by TRACK, not by locomotive: there is no
        `address` parameter here at all, and nothing in this method sends
        one. `Station.cv_read` still accepts an address for the POM path
        and warns when one is given alongside `mode=SERVICE`.

        Never retried automatically, unlike POM's three attempts: a service
        read already costs up to `TIMING.service_result` (95 s) and the
        command station retries the decoder handshake internally. One
        failing read here is exactly one telegram plus its polls.

        Takes no `page` keyword yet - Task 6 adds it, together with
        `ensure_page`'s call at the top of this method, in the step that
        implements `ensure_page` (1.5).
        """
        telegram, encoding, page_index = self.service_read_telegram(cv)
        power_before = self._station.status().track_power
        start = self._station.now()
        try:
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
            return self._finish_service_read(cv, encoding, page_index, outcome, start)
        finally:
            self.exit_service_mode(restore_power=power_before)

    def _finish_service_read(
        self,
        cv: int,
        encoding: CvEncoding,
        page_index: int,
        outcome: object,
        start: float,
    ) -> CvResult:
        no_ack_hint = (
            "decoder did not acknowledge; sound decoders often fail on a 750 mA "
            "programming track - use POM instead"
        )
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
                hint=no_ack_hint,
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
                    hint=no_ack_hint,
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
    ) -> CvResult:
        """Read one CV, choosing POM or service mode through `resolve_mode`.

        `address` matters only on the POM path; service mode is addressed
        by track and never sends one. `page` is accepted for signature
        symmetry with the write side - selecting the CV257..512 index page
        is `ensure_page`'s job, added in Task 6, not this one.
        """
        resolved = resolve_mode(mode, self._station.capabilities, operation="read")
        if resolved is ProgMode.POM:
            return self.pom_read(cv, address=address)
        return self.service_read(cv)

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

    def pom_read(self, cv: int, *, address: int | None = None) -> CvResult:
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
        resolved = self._station.resolve_address(address)
        if resolved is None:
            raise ValueError(
                f"POM read of CV{cv} needs a locomotive address: pass address= "
                f"or set a default address"
            )

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
                self._station.learn(pom_read=False)
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
                learned: dict[str, object] = {"pom_read": True}
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
