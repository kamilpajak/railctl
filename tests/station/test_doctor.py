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

from railctl.station.doctor import (
    CHECK_IDS,
    CHECK_TITLES,
    PROBE_CV,
    PROBE_CV_VALUE,
    _check_d0,
    exit_code_for_report,
    run_probe,
)
from railctl.transport.fake import FakeTransport
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_pom_read_byte,
    cmd_service_result_request,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_on,
)

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
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    doctor_bench.transport.on_write.set(cmd_track_power_on(), encode(0x61, 0x01))
    report = run_probe(
        doctor_bench.station, allow_power_on=True, now_utc=lambda: "2026-08-05T00:00:00Z"
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
    d4 = report.check("D4")
    note = next(n for n in report.capabilities.notes if "silence" in n.lower())
    assert "silence" in note.lower()
    assert "re-run" in note.lower() and "doctor" in note.lower()
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
