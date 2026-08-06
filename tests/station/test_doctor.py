"""railctl doctor: checks D0-D12 and the verdict block.

Every scripted scenario here uses a `Responder` keyed by exact request bytes,
never `FakeTransport.expect()`'s ordered queue: `run_probe` sends a variable
number of exchanges depending on which capability path each check takes, and
pinning that count would break the moment an unrelated internal refactor
changed it. `Responder` distinguishes two kinds of answer: a PERSISTENT one
(the same reply every time a request recurs - `station.status()` is called
more than once in a single `run_probe`, and both calls must see the same
track-power bit unless a test explicitly changes it) and a QUEUED one (answer
this exact request once, then fall back to whatever is persistent or
default - later checks in this project send the identical `21 10 31` poll
telegram for several different CVs in a row, and each must get its own answer
in turn).
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest

from railctl.station import doctor as doctor_module
from railctl.station.capabilities import Capabilities
from railctl.station.doctor import (
    CHECK_IDS,
    CHECK_TITLES,
    PROBE_CV,
    PROBE_CV_VALUE,
    _check_d0,
    exit_code_for_report,
    run_probe,
    verdict_lines,
)
from railctl.station.types import Check, DoctorReport
from railctl.transport.fake import FakeTransport
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    DB_DIRECT_WRITE,
    DB_Z21_WRITE,
    POM_WRITE_BIT_BASE,
    POM_WRITE_BYTE_BASE,
    REQ_4_DATA,
    FunctionAction,
    FunctionGroup,
    cmd_function_group,
    cmd_function_single,
    cmd_loco_info,
    cmd_pom_read_byte,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_service_result_request,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
)
from railctl.xbus.cv import EXT_WRITE_OPCODES
from railctl.xbus.dialect import XPRESSNET, Z21

if TYPE_CHECKING:
    from tests.station.conftest import Bench

VERSION_REPLY = encode(0x63, 0x21, 0x40, 0x12)  # XpressNet 4.0, station id 0x12 (Z21 family)
STATUS_REPLY_POWERED = encode(0x62, 0x22, 0x04)  # auto-start bit only: track_power True
STATUS_REPLY_UNPOWERED = encode(0x62, 0x22, 0x07)  # measured (docs/probe-results.md)
GENERIC_ACK = encode(0x01, 0x04)
UNSUPPORTED_REPLY = encode(0x61, 0x82)
NOACK_REPLY = encode(0x61, 0x13)


def cv_reply(ident: int, echo: int, value: int) -> bytes:
    """A `63 <ident> <echo> <value>` service-result telegram - the one reply
    shape every read opcode in this file answers through (cv.py, xbus/replies.py):
    POM, direct, extended and Z21 reads all come back via `63 14..17` on this
    hardware, never the alternate 6-byte Z21 form. Not exercised by D0-D3, but
    defined here since every later split of this test file imports it from
    here rather than redefining it."""
    return encode(0x63, ident, echo, value)


def loco_info_reply(*, busy: bool = False) -> bytes:
    ident = 0x08 if busy else 0x00  # bit 3 = in_use_by_other; high nibble stays 0
    return encode(0xE4, ident, 0, 0, 0)


class Responder:
    """`on_write` for a scriptless FakeTransport. See the module docstring.

    on_write is handed the FRAMED request, and every table in this class is keyed on bare
    telegrams, so unframing happens here, once. Keying the table on framed bytes instead would
    make every lookup miss and every probe answer GENERIC_ACK - a doctor that measures nothing
    and reports success.

    D4 through D8 all poll the identical `21 10` service-result telegram inside one run_probe, and
    the station answers a given CV on the same ident+echo bytes regardless of which opcode asked
    for it (`docs/probe-results.md`: CV8 always comes back on `63 14 08`, whether POM, a direct
    service read, or an extended service read asked) - so a queue keyed on the poll telegram
    alone cannot tell one check's poll from another's, and cannot even fall back to matching on
    the reply's own bytes, since a reply that would satisfy one check's matcher can equally
    satisfy a different check's matcher for the same CV. `queue_once_for` scopes a reply to the
    request that must immediately precede the poll it answers - tracked here as `_last_probe`,
    the most recent DIFFERENT request this responder has seen - so a reply queued for one check's
    poll stays invisible to every other check's.
    """

    def __init__(self, bench: Bench) -> None:
        self._bench = bench
        self._persistent: dict[bytes, bytes | None] = {
            cmd_station_version(): VERSION_REPLY,
            cmd_station_status(): STATUS_REPLY_POWERED,
        }
        self._queues: dict[bytes, deque[bytes | None]] = {}
        self._scoped_queues: dict[bytes, deque[bytes | None]] = {}
        self._previous_key: bytes = b""
        self._last_probe: bytes = b""

    def set(self, request: bytes, reply: bytes | None) -> None:
        """Override every future answer to `request`."""
        self._persistent[bytes(request)] = reply

    def queue_once(self, request: bytes, reply: bytes | None) -> None:
        """Answer the next occurrence of `request`, then fall through."""
        self._queues.setdefault(bytes(request), deque()).append(reply)

    def queue_once_for(self, probe: bytes, reply: bytes | None) -> None:
        """Answer the next poll that comes immediately after a write of `probe`, then fall
        through. `probe` is the check's own request - `cmd_z21_cv_read(1)`,
        `cmd_service_ext_read(257)`, `cmd_service_direct_read(29)` - never the shared `21 10`
        poll telegram itself, and never the reply's own bytes: this scopes by what the station
        was just SENT, which is unique per check, not by what a reply looks like, which is not.
        """
        self._scoped_queues.setdefault(bytes(probe), deque()).append(reply)

    def __call__(self, framed: bytes, transport: FakeTransport) -> None:
        key = self._bench.unframe(framed)
        if key != self._previous_key:
            self._last_probe = self._previous_key
        self._previous_key = key
        scoped = self._scoped_queues.get(self._last_probe)
        if scoped:
            reply = scoped.popleft()
        else:
            queue = self._queues.get(key)
            reply = queue.popleft() if queue else self._persistent.get(key, GENERIC_ACK)
        if reply is not None:
            self._bench.reply(reply)


@pytest.fixture
def doctor_bench(bench_factory):
    """A `Bench` with no default address, built directly from `bench_factory` rather than from
    `bench`: `bench`'s own default (`BENCH_DEFAULT_ADDRESS = 3`, ADDENDUM.md part A.2) would make
    "no address given" untestable here, since `_resolved_address` falls back to
    `station.default_address` whenever a check's own `address=` argument is `None` -
    `test_d4_is_skipped_with_no_address_even_if_the_track_is_powered` (task-7b.md) and
    `test_d11_and_d12_are_skipped_with_no_address` (task-7d.md) both call `run_probe` with no
    `address=` and depend on that fallback being absent.

    One check reads the address a different way and is unaffected either way: `_check_d9`'s
    `_best_effort_read` (task-7d.md) reads `station.default_address` directly, never through
    `_resolved_address`, so with no default address it always skips its POM path and falls
    straight to `service_read` - which is what task-7d.md's D9 tests already assume, address or
    not.
    """
    bench = bench_factory(default_address=None)
    bench.transport.on_write = Responder(bench)
    return bench


def test_check_ids_are_thirteen_and_unique():
    assert len(CHECK_IDS) == 13
    assert len(set(CHECK_IDS)) == 13
    assert set(CHECK_TITLES) == set(CHECK_IDS)


def test_d0_records_link_description_and_identity_and_drains(doctor_bench):
    doctor_bench.push(encode(0x81, 0x00))
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d0 = report.check("D0")
    assert d0.status == "ok"
    assert doctor_bench.station.link.description in d0.detail
    assert doctor_bench.station.link.identity in d0.detail
    # The queued broadcast must be gone: drain() is what D0 promises.
    assert doctor_bench.link.poll(0.0) == []


def test_d0_drains_before_any_exchange(doctor_bench):
    """D0's drain must be observable before D1/D2 pump the link on their own
    solicited exchanges - otherwise a deleted `station.link.drain()` call is
    indistinguishable from one that ran, since D1/D2's own polling would
    dispatch the pushed broadcast anyway."""
    doctor_bench.push(encode(0x81, 0x00))
    _check_d0(doctor_bench.station)
    assert doctor_bench.link.poll(0.0) == []


def test_d1_records_xpressnet_version_and_station_id(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D1").status == "ok"
    assert report.capabilities.xpressnet_version == "4.0"
    assert report.capabilities.command_station_id == 0x12
    assert report.capabilities.probed_at == "2026-08-05T00:00:00Z"


def test_d2_decodes_the_status_bits(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d2 = report.check("D2")
    assert d2.status == "ok"
    assert "track power on" in d2.detail.lower() or "powered" in d2.detail.lower()


def test_d3_reports_ok_when_track_already_powered(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D3").status == "ok"


def test_d3_unpowered_without_power_on_is_unknown_not_a_failure(doctor_bench):
    """Pinned: an unpowered bench with no --power-on must not read as a link
    failure. report.ok stays True and the exit code stays 0 - this is the
    ordinary state of a bench with the layout switched off, not a defect."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D3").status == "unknown"
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_d3_powers_on_when_allowed_and_the_reread_confirms_it(doctor_bench):
    """`use_programming_track=False`: with the D5-D8 batch disabled,
    `exit_service_mode` never runs and so can never supply its own
    `cmd_track_power_on()` (its unconditional resume-operations telegram) -
    the only source of that telegram left in this run is D3's own
    `station.power_on()`, which is exactly the claim this test pins."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    doctor_bench.transport.on_write.set(cmd_track_power_on(), encode(0x61, 0x01))
    report = run_probe(
        doctor_bench.station,
        allow_power_on=True,
        use_programming_track=False,
        now_utc=lambda: "2026-08-05T00:00:00Z",
    )
    assert report.check("D3").status == "ok"
    assert "turned on" in report.check("D3").detail
    assert cmd_track_power_on() in doctor_bench.sent


def test_d4_success_records_pom_read_true_and_the_echo_convention(doctor_bench):
    """CV8 echoed as 7 (zero-based) fixes pom_echo_zero_based True."""
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 7, PROBE_CV_VALUE)
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    d4 = report.check("D4")
    assert d4.status == "ok"
    assert report.capabilities.pom_read is True
    assert report.capabilities.pom_echo_zero_based is True
    assert "145" in d4.detail
    assert "expected" not in d4.detail  # value matched - no mismatch note


def test_d4_success_with_one_based_echo_and_a_mismatched_value_adds_a_note(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), cv_reply(0x14, 8, 3))
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is True
    assert report.capabilities.pom_echo_zero_based is False
    d4 = report.check("D4")
    assert d4.status == "ok"
    assert "3" in d4.detail and "145" in d4.detail  # a note, not a silent pass


def test_d4_unsupported_sets_pom_read_false_with_no_silence_note(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is False
    assert report.capabilities.pom_read_provenance == "unsupported"
    assert report.capabilities.notes == ()
    assert report.check("D4").status == "ok"


def test_d4_noack_keeps_pom_read_unknown_and_points_at_railcom(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    for _ in range(3):  # TIMING.pom_read_attempts
        doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), NOACK_REPLY)
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is None
    d4 = report.check("D4")
    assert d4.status == "unknown"
    assert "railcom" in d4.detail.lower()


def test_d4_total_silence_sets_pom_read_false_with_a_silence_note(doctor_bench):
    """Pinned: this is the ONE place False is written without a 61 82, and the
    note naming it is the price. A plain cv_read hitting the same silence
    (Task 4/5's own tests) must leave pom_read at None - only the doctor makes
    this call, because AUTO would otherwise retry POM for 6s forever."""
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    # No queued reply for 21 10 31 at all: every poll gets the persistent
    # default (a generic ack, never a value or a 61 13) until the budget runs out.
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is False
    assert report.capabilities.pom_result_channel == "none"
    # The note is no longer the ONLY place the difference lives: the two ways
    # of reaching False are now distinguishable by type. A reader that must
    # not act on a guess checks this field instead of parsing prose.
    assert report.capabilities.pom_read_provenance == "silence"
    d4 = report.check("D4")
    note = next(n for n in report.capabilities.notes if "silence" in n.lower())
    assert "silence" in note.lower()
    assert "re-run" in note.lower() and "doctor" in note.lower()
    # The note must send the reader to the DETECTOR, not to the decoder. Measured
    # 2026-08-06: this decoder has CV29 bit 3 set and CV28=3, so RailCom is
    # configured and always was; an earlier version of this note told the user to
    # fix the one thing that was already correct. Nothing pinned that advice, so
    # it was free to be wrong.
    assert "detector" in note.lower()
    assert note in d4.detail


def test_d4_is_unknown_when_the_track_is_unpowered(doctor_bench):
    """D4 (like D10) is 'unknown', not 'skip', when the reason it did not run
    is that the track is off without --power-on: spec line 855, "D4 and D10
    are skipped as unknown". 'skip' is reserved for a genuine opt-out (no
    address given)."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D4").status == "unknown"
    assert report.capabilities.pom_read is None


def test_d4_is_skipped_with_no_address_even_if_the_track_is_powered(doctor_bench):
    """Distinct from the case above: track power is fine, there is simply
    nothing to address a POM read at - that is an opt-out, 'skip'."""
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D4").status == "skip"
    assert report.capabilities.pom_read is None


def test_d4_ignores_a_stale_pom_read_false_verdict_and_re_measures(bench_factory):
    """`CvProgrammer.pom_read` short-circuits on `capabilities.pom_read is
    False` before it ever builds a telegram (programming.py:1044) - that is
    the ordinary runtime-learning behaviour, correct for every OTHER caller.
    D4 is the one caller that must not inherit it: it exists to re-probe and
    overwrite whatever a previous run (or an ordinary cv_read that hit a 61
    82 mid-session) recorded, so it clears pom_read/pom_result_channel/
    pom_echo_zero_based before calling in. `telegram in bench.sent` is the
    assertion that actually distinguishes this from the stale verdict simply
    being echoed back without ever touching the wire."""
    bench = bench_factory(default_address=None, pom_read=False)
    bench.transport.on_write = Responder(bench)
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=bench.station.threshold)
    bench.transport.on_write.set(telegram, GENERIC_ACK)
    bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 7, PROBE_CV_VALUE)
    )
    report = run_probe(bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D4").status == "ok"
    assert report.capabilities.pom_read is True
    # A stale provenance must not outlive the verdict it explained. POM read
    # now works, so nothing is left claiming it was refused or went silent.
    assert report.capabilities.pom_read_provenance is None
    assert telegram in bench.sent


def test_d4_re_measures_a_stale_false_verdict_even_when_it_confirms_unsupported(bench_factory):
    """Companion to the test above, with the opposite outcome: this run
    measures unsupported again rather than flipping to supported. Without
    the clearing fix `pom_read is False` is trivially true either way -
    it was already the stale value - so `telegram in bench.sent` is the one
    assertion that tells "measured again" apart from "never asked"."""
    bench = bench_factory(default_address=None, pom_read=False)
    bench.transport.on_write = Responder(bench)
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=bench.station.threshold)
    bench.transport.on_write.set(telegram, UNSUPPORTED_REPLY)
    report = run_probe(bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D4").status == "ok"
    assert report.capabilities.pom_read is False
    assert telegram in bench.sent


def test_d4_ignores_a_stale_pom_echo_zero_based_and_re_learns_it(bench_factory):
    """Companion to the two stale-`pom_read` tests above, pinning the OTHER field
    `_check_d4` clears: a stale `pom_echo_zero_based=True` fixes `CvMatcher`'s accepted
    echo byte to the zero-based candidate only (programming.py:1076, echo_candidates
    narrows on a fixed `zero_based`). This station actually echoes one-based (CV8 comes
    back as `8`, not `7`) - if the stale `True` survives into this run's matcher, every
    attempt fails to match, `pom_read` runs out the clock and D4 (mis)records
    `pom_read=False` from silence. Clearing `pom_echo_zero_based` before probing lets
    this run's own one-based echo match and re-learn the field correctly."""
    bench = bench_factory(default_address=None, pom_echo_zero_based=True)
    bench.transport.on_write = Responder(bench)
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=bench.station.threshold)
    bench.transport.on_write.set(telegram, GENERIC_ACK)
    bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 8, PROBE_CV_VALUE)
    )
    report = run_probe(bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is True
    assert report.capabilities.pom_echo_zero_based is False


def test_d4_short_circuit_is_unknown_and_the_report_still_reaches_d12(doctor_bench):
    """`station.programmer.pom_read` can raise a `ProgrammingError` subclass
    that is none of the three named ones - a short circuit reading CV8 over
    POM, here. `_check_d4`'s `except RailctlError` catch-all is what keeps
    the probe alive for it: D4 reads 'unknown' with nothing recorded (a
    short circuit is not a verdict about the capability), and every later
    check still runs. `report.check("D12")` existing is what actually pins
    that the report survived - without the catch-all this would propagate
    out of run_probe entirely and the test would fail on an uncaught
    ShortCircuitError instead of an assertion."""
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), encode(0x61, 0x12))
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    d4 = report.check("D4")
    assert d4.status == "unknown"
    assert "short circuit" in d4.detail.lower()
    assert report.capabilities.pom_read is None
    assert report.check("D12") is not None


def test_d5_through_d8_are_skipped_under_no_programming_track(doctor_bench):
    # D4 (POM, unrelated to use_programming_track) shares the same 21 10 31
    # result-poll telegram D5-D8 use, so it is answered 61 82 here - D4
    # settles on its own request without ever polling - and this test's
    # "nothing 21 10/22/23-shaped was sent" assertion measures D5-D8 alone,
    # not D4's ordinary POM traffic.
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, UNSUPPORTED_REPLY)
    report = run_probe(
        doctor_bench.station,
        address=50,
        use_programming_track=False,
        now_utc=lambda: "2026-08-05T00:00:00Z",
    )
    for check_id in ("D5", "D6", "D7", "D8"):
        check = report.check(check_id)
        assert check.status == "skip"
    assert report.capabilities.service_direct_cv is None
    assert report.capabilities.z21_cv_opcodes is None
    assert report.capabilities.service_ext_cv is None
    # No service-mode telegram was sent at all - not even an entry attempt.
    # doctor_bench.sent, not .transport.written: the latter is framed (an
    # LI-USB header the envelope adds), so request[:1] would compare against
    # the frame prefix instead of the telegram's own first byte.
    assert not any(
        request.startswith(b"\x21\x10") or request[:1] == b"\x22" or request[:1] == b"\x23"
        for request in doctor_bench.sent
    )


def test_d5_success_records_service_direct_cv_true(doctor_bench):
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_direct_cv is True
    assert report.check("D5").status == "ok"


def test_d5_unsupported_records_service_direct_cv_false(doctor_bench):
    doctor_bench.transport.on_write.set(cmd_service_direct_read(PROBE_CV), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_direct_cv is False
    assert report.check("D5").status == "ok"


def test_d5_noack_leaves_service_direct_cv_unknown_not_false(doctor_bench):
    """Pinned regression, D5's own version of D7's noack test: a decoder
    `61 13` on the programming track is a fact about the DECODER, not the
    station's opcode support. Only a `61 82` may write `service_direct_cv`
    False; a NoAck must leave the capability at None."""
    doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), NOACK_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_direct_cv is None
    assert report.check("D5").status == "unknown"


def test_d6_probes_only_the_z21_read_opcode_never_the_write_one(doctor_bench):
    """Pinned: 23 11 only. 23 11 has no meaning in classic XpressNet, so a
    station that lacks it answers 61 82 and nothing happens; probing 24 12
    (the write opcode) could modify a CV instead."""
    doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), cv_reply(0x14, 1, 5))
    run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    # doctor_bench.sent holds bare telegrams, so req[0] is the real X-Bus
    # header byte; .transport.written is framed and req[0] would be the
    # envelope's own first byte on every request, making this assertion
    # vacuously true no matter what doctor.py sends.
    written = doctor_bench.sent
    assert cmd_z21_cv_read(1) in written
    assert not any(req[0] == REQ_4_DATA and req[1] == DB_Z21_WRITE for req in written)


def test_d6_success_records_z21_cv_opcodes_true(doctor_bench):
    # queue_once_for, not queue_once: D5 polls the identical 21 10 telegram
    # before D6 does, and CV1's reply bytes would satisfy D5's own matcher
    # too (both are low-band reads) - queue_once would let D5 drain it first.
    doctor_bench.transport.on_write.queue_once_for(cmd_z21_cv_read(1), cv_reply(0x14, 1, 5))
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.z21_cv_opcodes is True


def test_d6_unsupported_records_z21_cv_opcodes_false(doctor_bench):
    doctor_bench.transport.on_write.set(cmd_z21_cv_read(1), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.z21_cv_opcodes is False


def test_d6_noack_leaves_z21_cv_opcodes_unknown_not_false(doctor_bench):
    """D6's version of the same D5/D7 regression: a `61 13` on the Z21 CV
    read is a decoder fact, not a station one. queue_once_for, not
    queue_once: D5 polls the identical 21 10 telegram first and would
    otherwise drain this NoAck before D6 ever gets a turn."""
    doctor_bench.transport.on_write.queue_once_for(cmd_z21_cv_read(1), NOACK_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.z21_cv_opcodes is None
    assert report.check("D6").status == "unknown"


EXT_HIGH_PROBE_CV = 257  # first CV of page 1: 22 19 01, the design's own example


def test_d7_both_bands_succeed_records_service_ext_cv_true(doctor_bench):
    # queue_once_for, not queue_once: D5 and D6 poll the identical 21 10
    # telegram before D7's low band does, and CV8's reply would satisfy D5's
    # own matcher too - a plain queue_once would let D5 (and, for the second
    # reply, D6) drain both replies before D7 ever gets a turn.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(EXT_HIGH_PROBE_CV), cv_reply(0x15, 1, 7)
    )
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is True
    d7 = report.check("D7")
    assert str(PROBE_CV) in d7.detail and str(EXT_HIGH_PROBE_CV) in d7.detail


def test_d7_high_band_rejected_records_false_and_names_the_band(doctor_bench):
    """Pinned: a station could accept the low page and refuse the high one -
    service_ext_cv is True only when BOTH succeed, and the failing band is
    named so a user knows CV257+ is unreachable in service mode here. A 61 82
    is the only reply this project ever lets a check record False from."""
    # queue_once_for, not queue_once, for the same reason as the test above:
    # D5 polls the identical 21 10 telegram first and CV8's reply satisfies its
    # matcher too, so a plain queue_once is drained by D5 and D7's low band gets
    # nothing. The assertions below would still pass - the detail names the high
    # band whenever the high band is refused, whatever happened to the low one -
    # so this test would go on reporting success while measuring a case its own
    # docstring does not describe: accepts the low page, refuses the high one.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.set(cmd_service_ext_read(EXT_HIGH_PROBE_CV), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is False
    assert str(EXT_HIGH_PROBE_CV) in report.check("D7").detail
    # A refused band is a real, measured fact about this station, not a
    # failure of the probe itself - "fail" would print FAIL for a classic
    # XpressNet station that is merely different, and D5/D6 render the
    # identical 61 82 fact as "ok" too.
    assert report.check("D7").status == "ok"


def test_d7_one_band_noack_leaves_service_ext_cv_unknown_not_false(doctor_bench):
    """Pinned regression: a decoder that fails to acknowledge on ONE band (a
    decoder fact) must not be recorded as 'this station lacks extended
    opcodes' (a station fact) in capabilities.json - that is the exact M1
    failure this project exists to avoid. Only an actual 61 82 may write
    False; a NoAck disagreement between the two bands leaves the capability
    None and the check 'unknown', naming which band was inconclusive."""
    # queue_once_for, not queue_once: without scoping to D7's own low-band
    # probe, D5 (which polls first and shares CV8's exact reply bytes with
    # D7's low band) drains this before D7 gets a turn, and D7's low band
    # ends up as inconclusive as its high band - naming CV8, not CV257, and
    # failing the assertion below for a reason the test is not about.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.set(cmd_service_ext_read(EXT_HIGH_PROBE_CV), NOACK_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is None
    d7 = report.check("D7")
    assert d7.status == "unknown"
    assert str(EXT_HIGH_PROBE_CV) in d7.detail


def test_d8_does_not_run_when_d4_was_silent_not_noack(doctor_bench):
    """Pinned: D8 runs only after D4 answers 61 13, never after D4's silence
    branch (a different, unrelated capability judgment)."""
    pom_telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(pom_telegram, GENERIC_ACK)
    # D4 sees total silence (no 61 13 ever) - falls into the "silence" branch.
    # queue_once_for, not queue_once: D4 polls the identical 21 10 telegram
    # first and would otherwise steal this on its very first (silent) poll -
    # and CV8's reply would match D4's own POM matcher too, turning D4's
    # intended silence into a false success before D4 ever exhausts its
    # attempts.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D5's own poll answer, so D5 passes and only D4's branch is under test
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.pom_read is False  # D4's silence override, not NoAck
    assert report.check("D8").status == "skip"


def test_d8_runs_after_d4_noack_and_d5_pass_and_reports_cv29_cv28(doctor_bench):
    pom_telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(pom_telegram, GENERIC_ACK)
    for _ in range(3):
        # D4 is always the first check to poll, so it drains these 3 in order
        # regardless of what runs after it - a plain queue_once is safe here.
        doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), NOACK_REPLY)
    # D5 (service direct on CV8) must PASS for D8 to run. Also safe unscoped:
    # D5 is next in line once D4's 3 NoAcks are spent, and nothing before it
    # can steal a 4th item that was never queued for it under a different key.
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    # D8 itself: CV29 then CV28. queue_once_for, not queue_once - D6 and D7
    # poll the identical 21 10 telegram before D8 does, and each mismatch
    # they see (neither replies below matches CV1 or CV257/CV8's band) makes
    # them poll again rather than stop, draining a plain queue before D8 ever
    # gets a turn.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(29), cv_reply(0x14, 29, 0x08)
    )
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(28), cv_reply(0x14, 28, 0x03)
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    d8 = report.check("D8")
    assert d8.status == "ok"
    assert "CV29" in d8.detail and "CV28" in d8.detail
    assert "bit 3" in d8.detail


def test_d5_through_d8_batch_calls_exit_service_mode_and_restores_power_on(doctor_bench):
    """Pinned: the D5-D8 try/finally must call
    `station.programmer.exit_service_mode` on the ordinary, no-exception
    path. The default bench already has the track powered, so D3 itself
    never sends `cmd_track_power_on()` - it only does that when it finds the
    track OFF (see `_check_d3`) - so the only possible source of that
    telegram in this run is `exit_service_mode`'s own unconditional
    resume-operations call."""
    run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert cmd_track_power_on() in doctor_bench.sent


def test_d5_through_d8_batch_restores_power_off_when_it_found_the_track_off(doctor_bench):
    """Companion to the test above: `restore_power` must follow the power
    state the batch actually found before it ran, not default to leaving
    power on. `allow_power_on=True` lets D3 succeed - which is what opens the
    `track_powered` gate the D5-D8 batch now shares with D3 - while the
    persistent station-status reply keeps reporting unpowered throughout, so
    `power_before` (read fresh right before the batch) still comes back
    False, the same as a station that really was off when the batch started."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    doctor_bench.transport.on_write.set(cmd_track_power_on(), encode(0x61, 0x01))
    run_probe(doctor_bench.station, allow_power_on=True, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert cmd_track_power_off() in doctor_bench.sent


def test_d5_through_d8_batch_exits_service_mode_even_when_a_check_raises(doctor_bench, monkeypatch):
    """Pinned: the try/finally must call `exit_service_mode` even when a
    check raises past its own `except RailctlError` - a genuine bug in the
    check, not a modelled protocol error. Skipping it would leave the
    layout stuck off the main track until an unrelated command happens to
    touch power (`exit_service_mode`'s own docstring). Monkeypatching
    `_check_d5` is simpler and more direct than crafting a damaged reply
    that happens to make production code raise something other than a
    `RailctlError` subclass."""

    def _raise(_station):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor_module, "_check_d5", _raise)
    with pytest.raises(RuntimeError):
        run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert cmd_track_power_on() in doctor_bench.sent


def test_d5_through_d8_report_unknown_when_track_is_off_without_power_on(doctor_bench):
    """Pinned: entering service mode cuts main power, and leaving it again
    unconditionally re-energises the main track through
    `exit_service_mode`'s own resume-operations telegram - so the D5-D8
    batch must not run at all when D3 refused to power the track on. Running
    it anyway would silently overrule D3's own '--power-on' refusal and
    briefly re-power a track the operator left off on purpose. Distinct from
    `test_d5_through_d8_are_skipped_under_no_programming_track`: that one is
    an explicit opt-out (`skip`), this one is an unmet precondition
    (`unknown`), the same distinction D4 already makes for the same reason."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    for check_id in ("D5", "D6", "D7", "D8"):
        check = report.check(check_id)
        assert check.status == "unknown"
        assert "power" in check.detail.lower()
    assert report.capabilities.service_direct_cv is None
    assert report.capabilities.z21_cv_opcodes is None
    assert report.capabilities.service_ext_cv is None
    # No service-mode telegram was sent at all - not even an entry attempt -
    # and exit_service_mode never ran, so neither power telegram appears.
    assert not any(
        request.startswith(b"\x21\x10") or request[:1] == b"\x22" or request[:1] == b"\x23"
        for request in doctor_bench.sent
    )
    assert cmd_track_power_on() not in doctor_bench.sent
    assert cmd_track_power_off() not in doctor_bench.sent


def test_d9_with_no_established_read_path_reports_family_unknown_never_ms(doctor_bench):
    """Pinned: an unread CV250 must render as 'unknown', never as 'ms' - the
    same guard decoder_family() itself enforces (Task 1), exercised here
    through the doctor's own aggregation over IDENTITY_CVS."""
    doctor_bench.transport.on_write.set(cmd_service_direct_read(PROBE_CV), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_z21_cv_read(1), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_service_ext_read(PROBE_CV), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_service_ext_read(257), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d9 = report.check("D9")
    assert "unknown" in d9.detail
    assert "ms" not in d9.detail.lower().replace("unknown", "")
    # "unknown", not "skip": every read path was TRIED and every one failed.
    # "skip" would claim the doctor chose not to look, and a hardware run showed
    # this check reporting exactly that after actually running.
    assert d9.status == "unknown"
    assert "every read failed" in d9.detail


def test_d9_names_the_failure_reasons_when_only_some_identity_cvs_are_read(
    doctor_bench, monkeypatch
):
    """A partial read stays "ok" - identity WAS established - but the report
    must still say why the rest did not answer.

    The rendered values already carry `CV28=?` for each CV that failed, so
    WHICH ones failed was never in doubt. What was missing is why: a decoder
    that ignores a CV and a link that faulted produce the same `?`.

    `_best_effort_read` is substituted rather than scripted on the wire.
    Driving nine identity CVs to a specific mix of successes and failures
    through the service-mode result machinery would make the test about that
    machinery, and the branch under test is the formatting decision in
    `_check_d9`, which sits above it.
    """
    reads = {7: (145, None), 8: (145, None)}

    def fake_read(_station, cv, *, use_programming_track):
        return reads.get(cv, (None, "DecoderNotRespondingError"))

    monkeypatch.setattr(doctor_module, "_best_effort_read", fake_read)
    check = doctor_module._check_d9(doctor_bench.station, use_programming_track=True)
    assert check.status == "ok"
    assert "CV7=145" in check.detail
    assert "CV28=?" in check.detail
    assert "some reads failed" in check.detail
    assert "DecoderNotRespondingError" in check.detail


def test_d9_skips_the_programming_track_when_it_is_disabled(doctor_bench):
    """`--no-programming-track` must stop D9 too, not only D5-D8.

    `_best_effort_read` falls through to `service_read`, which drives the
    programming track, and D9 runs AFTER run_probe's `exit_service_mode`
    restored the power state - so an unguarded D9 could re-enter service mode
    behind the operator's back. With no POM read proven either, nothing is
    attempted at all, which is the one case that really is a "skip".
    """
    report = run_probe(
        doctor_bench.station,
        use_programming_track=False,
        now_utc=lambda: "2026-08-05T00:00:00Z",
    )
    d9 = report.check("D9")
    assert d9.status == "skip"
    assert "no read path was attempted" in d9.detail
    # The proof is the wire, not the status string: no service-mode telegram
    # may have been sent on D9's behalf.
    assert not any(request[:1] in (b"\x22", b"\x23") for request in doctor_bench.sent)


def test_d10_is_unknown_when_the_track_is_unpowered_without_power_on(doctor_bench):
    """Distinct from the no-address skip below - spec line 855, 'D4 and D10
    are skipped as unknown'. An operator who forgot --power-on must not read
    this as 'nothing to probe', but as 'this genuinely could not be
    established'."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D10").status == "unknown"
    assert report.capabilities.loco_address_threshold is None


def test_d10_is_skipped_with_no_address_in_the_divergence_band(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D10").status == "skip"
    assert report.capabilities.loco_address_threshold is None


def test_d10_identical_replies_leave_the_threshold_unresolved(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), loco_info_reply()
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold is None
    assert report.check("D10").status == "ok"


def test_d10_only_the_xpressnet_form_answers_records_threshold_100(doctor_bench):
    """Mirror of the Z21-only case below: the XpressNet form (long addresses
    from 100) is the one that answers, the Z21 form is rejected."""
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), loco_info_reply()
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), UNSUPPORTED_REPLY
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold == XPRESSNET.long_address_threshold
    assert report.check("D10").status == "ok"


def test_d10_only_the_z21_form_answers_records_threshold_128(doctor_bench):
    """The rejected form gets `01 09 08` - the interface status `station.exchange`
    maps to a bare `ValueError` (`test_power_and_status.py::
    test_exchange_maps_interface_status_09_to_value_error`, facade.py), and the
    brief itself names this exact reply as the discriminator for a rejected
    address form. `_check_d10` must catch that `ValueError` and treat it as
    this form being rejected, not let it escape and abort the whole probe."""
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), encode(0x01, 0x09)
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold == 128
    assert report.check("D10").status == "ok"


def test_d10_unsupported_reply_is_a_rejected_form_too(doctor_bench):
    """`61 82` also identifies a rejected address form, distinctly from `01 09
    08` - both collapse to "rejected" so either can pair with a `LocoInfo` on
    the other encoding to resolve the threshold."""
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), UNSUPPORTED_REPLY
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold == 128
    assert report.check("D10").status == "ok"


def test_d10_rejected_under_both_encodings_leaves_the_threshold_unresolved(doctor_bench):
    """An address rejected by both forms is just as unresolved as one accepted
    by both - neither tells us which encoding the station expects for
    addresses IN the divergence band, only that this specific address is
    invalid one way or another under both."""
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), UNSUPPORTED_REPLY
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), encode(0x01, 0x09)
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold is None
    assert report.check("D10").status == "ok"


def test_d10_ambiguous_reply_on_one_form_leaves_threshold_unresolved(doctor_bench):
    """A bare `01 04 05` generic ack is NOT a rejection of the address form -
    unlike `61 82` and `01 09 08`, it says nothing about which encoding this
    station expects. Recording a threshold from it would be the same mistake
    D11/D12 make with `TRANSIENT_REPLIES`: writing a verdict from a reply that
    never measured the thing being asked."""
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), GENERIC_ACK
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.loco_address_threshold is None
    assert report.check("D10").status == "unknown"


def test_d11_sends_all_zero_bits_on_groups_4_and_5(doctor_bench):
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    threshold = doctor_bench.station.threshold
    # doctor_bench.sent, not .transport.written: the latter is framed, so
    # neither cmd_function_group() call would ever appear inside it verbatim.
    written = doctor_bench.sent
    assert cmd_function_group(50, FunctionGroup.G4, 0, threshold=threshold) in written
    assert cmd_function_group(50, FunctionGroup.G5, 0, threshold=threshold) in written
    assert report.capabilities.function_groups_4_5 is True


def test_d11_unsupported_records_false(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_function_group(50, FunctionGroup.G4, 0, threshold=doctor_bench.station.threshold),
        UNSUPPORTED_REPLY,
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.function_groups_4_5 is False


def test_d11_transient_reply_leaves_capability_unknown(doctor_bench):
    """`61 81` (STATION_BUSY) says nothing about whether the opcode is
    implemented (`TRANSIENT_REPLIES`, replies.py) - it must not be recorded as
    either `True` or `False`."""
    doctor_bench.transport.on_write.set(
        cmd_function_group(50, FunctionGroup.G4, 0, threshold=doctor_bench.station.threshold),
        encode(0x61, 0x81),
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.function_groups_4_5 is None
    assert report.check("D11").status == "unknown"


def test_d12_sends_the_f0_off_single_function_telegram(doctor_bench):
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    threshold = doctor_bench.station.threshold
    expected = cmd_function_single(50, 0, FunctionAction.OFF, threshold=threshold)
    assert expected in doctor_bench.sent
    assert report.capabilities.single_function_cmd is True


def test_d12_unsupported_records_false(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_function_single(50, 0, FunctionAction.OFF, threshold=doctor_bench.station.threshold),
        UNSUPPORTED_REPLY,
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.single_function_cmd is False


def test_d12_transient_reply_leaves_capability_unknown(doctor_bench):
    """`61 81` (STATION_BUSY) says nothing about whether the opcode is
    implemented - it must not be recorded as either `True` or `False`."""
    doctor_bench.transport.on_write.set(
        cmd_function_single(50, 0, FunctionAction.OFF, threshold=doctor_bench.station.threshold),
        encode(0x61, 0x81),
    )
    report = run_probe(doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.single_function_cmd is None
    assert report.check("D12").status == "unknown"


def test_d11_and_d12_are_skipped_with_no_address(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D11").status == "skip"
    assert report.check("D12").status == "skip"


def test_the_doctor_never_writes_a_decoder_cv(doctor_bench):
    """The mechanical version of the design's central promise. Every read
    scenario answers successfully so every check actually runs, then every
    telegram this run sent is checked against every CV-write encoding:
    E6 30 .. EC|MM / E8|MM (POM byte/bit write), 23 16 (direct write),
    23 1C..1F (extended write), 24 12 (Z21 write)."""
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 7, PROBE_CV_VALUE)
    )  # D4 (POM), zero-based echo
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D5 (service direct)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 1, 5)
    )  # D6 (Z21)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D7 low
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x15, 1, 7)
    )  # D7 high
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), encode(0x01, 0x09)
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")

    # Unframed one write() call at a time, NOT doctor_bench.sent: that property
    # joins every frame written since open() into a single buffer and feeds it
    # through one LiUsbEnvelope, whose `feed()` caps its buffer at MAX_BUFFER
    # (envelope/liusb.py) and silently drops the oldest bytes once a run's
    # telegrams overflow it - this D0-D12 run is long enough to trigger exactly
    # that truncation, which would leave the early checks unchecked while this
    # test still reports green. `transport.written` holds one complete framed
    # telegram per write() call (chunk_size only fragments *reads*), so
    # unframing each entry with its own fresh decoder can't truncate.
    telegrams = [doctor_bench.unframe(framed) for framed in doctor_bench.transport.written]
    assert len(telegrams) > 20, (
        "the run sent far fewer telegrams than expected - probe wiring broke"
    )
    for telegram in telegrams:
        header, db0 = telegram[0], telegram[1]
        assert not (header == 0xE6 and db0 == 0x30 and telegram[4] & 0xFC == POM_WRITE_BYTE_BASE)
        assert not (header == 0xE6 and db0 == 0x30 and telegram[4] & 0xFC == POM_WRITE_BIT_BASE)
        assert not (header == 0x23 and db0 == DB_DIRECT_WRITE)
        assert not (header == 0x23 and db0 in EXT_WRITE_OPCODES)
        assert not (header == 0x24 and db0 == DB_Z21_WRITE)


def test_every_check_id_appears_exactly_once_in_order(doctor_bench):
    report = run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert tuple(check.id for check in report.checks) == CHECK_IDS


def test_every_check_id_appears_exactly_once_when_gated_paths_are_taken(doctor_bench):
    """Same pin, walking the OTHER branch of every gate this task added:
    unpowered track (D4/D10 read 'unknown' rather than the '--power-on'-given
    path) and --no-programming-track (skips D5-D8). CHECK_IDS must still be
    complete."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(
        doctor_bench.station, use_programming_track=False, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert tuple(check.id for check in report.checks) == CHECK_IDS


def test_verdict_lines_are_exactly_four():
    caps = Capabilities.unknown("test")
    report = DoctorReport(checks=(), capabilities=caps)
    assert len(verdict_lines(report)) == 4


def test_verdict_lines_on_an_all_unknown_capability_set_say_unknown_never_bare_no():
    """Pinned: every line must say 'unknown', and none may contain the bare
    word 'no' - a naive substring check would flag 'unknown' itself (it
    contains 'no' as consecutive letters), so this asserts a WORD boundary."""
    import re

    caps = Capabilities.unknown("test")
    report = DoctorReport(checks=(), capabilities=caps)
    lines = verdict_lines(report)
    assert len(lines) == 4
    for line in lines:
        assert line.strip() != ""
        assert "unknown" in line.lower()
        assert re.search(r"\bno\b", line.lower()) is None


def test_verdict_primary_cv_path_reports_pom_and_its_result_channel():
    caps = Capabilities.unknown("test").with_learned(pom_read=True, pom_result_channel="poll")
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM (results arrive via poll)" in verdict_lines(report)[0]


def test_verdict_primary_cv_path_falls_back_to_unknown_channel_when_none_recorded():
    caps = Capabilities.unknown("test").with_learned(pom_read=True, pom_result_channel=None)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM (results arrive via unknown)" in verdict_lines(report)[0]


def test_verdict_primary_cv_path_points_at_fallback_when_pom_is_unsupported():
    caps = Capabilities.unknown("test").with_learned(pom_read=False)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM unavailable (61 82); see Fallback" in verdict_lines(report)[0]


def test_verdict_primary_cv_path_names_silence_not_61_82_when_that_is_the_provenance():
    """D4's own exception (doctor.py's `_SILENCE_NOTE` path) records `pom_read=False` with
    `pom_result_channel="none"` when the POM read produced no result at all - neither a value
    nor `61 13` nor `61 82`. The verdict line must not claim the station said `61 82` when the
    real cause was silence; the two origins must render two different lines."""
    unsupported_caps = Capabilities.unknown("test").with_learned(pom_read=False)
    silence_caps = Capabilities.unknown("test").with_learned(
        pom_read=False, pom_result_channel="none"
    )
    unsupported_line = verdict_lines(DoctorReport(checks=(), capabilities=unsupported_caps))[0]
    silence_line = verdict_lines(DoctorReport(checks=(), capabilities=silence_caps))[0]
    assert "POM unavailable (silence, not 61 82); see Fallback" in silence_line
    assert silence_line != unsupported_line


def test_verdict_fallback_names_direct_service_mode_first():
    caps = Capabilities.unknown("test").with_learned(service_direct_cv=True)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "service mode, direct opcodes, CV1-255 only" in verdict_lines(report)[1]


def test_verdict_fallback_names_z21_service_mode():
    caps = Capabilities.unknown("test").with_learned(z21_cv_opcodes=True)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "service mode, Z21 opcodes, CV1-1024" in verdict_lines(report)[1]


def test_verdict_fallback_names_extended_service_mode():
    caps = Capabilities.unknown("test").with_learned(service_ext_cv=True)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "service mode, extended opcodes" in verdict_lines(report)[1]


def test_verdict_fallback_says_unavailable_when_every_service_opcode_is_confirmed_absent():
    caps = Capabilities.unknown("test").with_learned(
        service_direct_cv=False, z21_cv_opcodes=False, service_ext_cv=False
    )
    report = DoctorReport(checks=(), capabilities=caps)
    assert "unavailable - service-mode opcodes unconfirmed" in verdict_lines(report)[1]


def test_verdict_cv_above_255_names_z21_opcodes():
    caps = Capabilities.unknown("test").with_learned(z21_cv_opcodes=True)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM (write) + Z21 opcodes (read), CV1-1024" in verdict_lines(report)[2]


def test_verdict_cv_above_255_names_extended_opcodes():
    caps = Capabilities.unknown("test").with_learned(service_ext_cv=True)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM (write) + extended opcodes (read)" in verdict_lines(report)[2]


def test_verdict_cv_above_255_says_pom_only_when_both_read_paths_are_rejected():
    caps = Capabilities.unknown("test").with_learned(z21_cv_opcodes=False, service_ext_cv=False)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "POM only (extended opcodes rejected: 61 82)" in verdict_lines(report)[2]


def test_verdict_loco_addresses_names_the_xpressnet_form():
    caps = Capabilities.unknown("test").with_learned(loco_address_threshold=100)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "1-99 short, 100+ long (XpressNet form confirmed)" in verdict_lines(report)[3]


def test_verdict_loco_addresses_names_the_z21_form():
    caps = Capabilities.unknown("test").with_learned(loco_address_threshold=128)
    report = DoctorReport(checks=(), capabilities=caps)
    assert "1-127 short, 128+ long (Z21 form confirmed)" in verdict_lines(report)[3]


def test_exit_code_for_report_is_zero_when_ok():
    caps = Capabilities.unknown("test")
    checks = tuple(Check(cid, CHECK_TITLES[cid], "ok", "") for cid in ("D0", "D1", "D2"))
    report = DoctorReport(checks=checks, capabilities=caps)
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_exit_code_for_report_is_three_when_d1_failed():
    caps = Capabilities.unknown("test")
    checks = (
        Check("D0", CHECK_TITLES["D0"], "ok", ""),
        Check("D1", CHECK_TITLES["D1"], "fail", "no reply"),
        Check("D2", CHECK_TITLES["D2"], "ok", ""),
    )
    report = DoctorReport(checks=checks, capabilities=caps)
    assert report.ok is False
    assert exit_code_for_report(report) == 3


def test_station_probe_delegates_to_run_probe(doctor_bench, monkeypatch):
    """Pins the wiring itself, not something incidentally true of every wiring: a spy replaces
    `run_probe` (patched on `railctl.station.doctor`, which is where `Station.probe`'s lazy
    `from railctl.station.doctor import run_probe` resolves it at call time) and records the
    exact arguments it was called with, so corrupting any one of the three keyword arguments -
    or the station reference itself - fails this test directly rather than leaving it green
    because `report.check("D0")` happens to hold regardless of what `probe()` forwarded."""
    calls: list[dict[str, object]] = []

    def spy(
        station: object,
        *,
        address: int | None = None,
        allow_power_on: bool = False,
        use_programming_track: bool = True,
    ) -> DoctorReport:
        calls.append(
            {
                "station": station,
                "address": address,
                "allow_power_on": allow_power_on,
                "use_programming_track": use_programming_track,
            }
        )
        return DoctorReport(
            checks=(Check("D0", CHECK_TITLES["D0"], "ok", ""),),
            capabilities=doctor_bench.station.capabilities,
        )

    monkeypatch.setattr(doctor_module, "run_probe", spy)
    report = doctor_bench.station.probe(
        address=50, allow_power_on=True, use_programming_track=False
    )
    assert isinstance(report, DoctorReport)
    assert report.check("D0") is not None
    assert calls == [
        {
            "station": doctor_bench.station,
            "address": 50,
            "allow_power_on": True,
            "use_programming_track": False,
        }
    ]


def test_run_probe_verdict_lines_and_exit_code_for_report_are_exported_from_the_station_package():
    from railctl.station import (
        exit_code_for_report as exported_exit_code_for_report,
    )
    from railctl.station import (
        run_probe as exported_run_probe,
    )
    from railctl.station import (
        verdict_lines as exported_verdict_lines,
    )

    assert exported_run_probe is run_probe
    assert exported_verdict_lines is verdict_lines
    assert exported_exit_code_for_report is exit_code_for_report
