"""The shared CV wait loop, the echo matcher, POM reads, and AUTO mode
resolution (design doc lines 708-743, 786-787, 916-924).

`station/` may hold no framing bytes, no port names, and no CV arithmetic
(`tests/test_layering.py`, rules 1 and 2) - `xbus.cv`'s `echo_candidates`,
`decode_echo` and `result_ident_for` are what let this module compare CV
numbers and reply idents without ever touching a wire byte itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    ShortCircuitError,
    TrackPowerError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import CvResult, ProgMode
from railctl.xbus.commands import cmd_pom_read_byte, cmd_service_result_request
from railctl.xbus.cv import CvEncoding, decode_echo, echo_candidates, result_ident_for
from railctl.xbus.replies import (
    TRANSIENT_REPLIES,
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
    "CvMatcher",
    "CvProgrammer",
    "ResultChannelSeen",
    "TimedOut",
    "WaitOutcome",
    "resolve_mode",
]


@dataclass(frozen=True, slots=True)
class TimedOut:
    """The wait ran out with no answer. Distinct from `NoAck`: the station may
    simply never have replied, which is UNKNOWN, never a negative answer."""

    polls: int
    ready_streak: int
    saw_no_ack: bool


WaitOutcome = Reply | TimedOut
ResultChannelSeen = Literal["broadcast", "poll"]


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
        self._pages: dict[object, object] = {}

    def invalidate_pages(self) -> None:
        """Registered with `station.register_cache` in `Station.__init__`. A
        no-op stub until Task 6 populates `_ensure_page`'s cache - there is
        nothing to clear yet, but the hook has to exist now."""
        self._pages.clear()

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
