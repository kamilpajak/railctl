"""Session, version, status, power, emergency stop and the event iterator.

The two LI-USB facts that shape every test here: exactly one solicited reply per command (never
an ack followed by data), and broadcasts are buffered while a command is outstanding.
"""

from __future__ import annotations

import logging
import threading

import pytest

# Kind is still used directly below: test_close_never_writes_capabilities_for_an_unknown_link_
# identity builds its own raw Link (never bench) because Bench has no way to ask for an unknown
# transport identity, so that one test still frames its own handshake by hand. Every other
# expect()/push() call in this file goes through Bench and never spells Kind.
from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import (
    CONDITION_POWER_OFF_UNSETTLED,
    CONDITION_POWER_ON_UNSETTLED,
    LinkTimeout,
    ProtocolError,
    RailctlError,
    TrackPowerError,
    TransportError,
    exit_code_for,
)
from railctl.link import Link
from railctl.station.capabilities import UNKNOWN_IDENTITY, Capabilities
from railctl.station.facade import Station
from railctl.station.timing import TIMING
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
)
from railctl.xbus.dialect import XPRESSNET
from railctl.xbus.replies import GENERIC_ACK, TRANSIENT_REPLIES

CMD_STATION_VERSION = cmd_station_version()  # 21 21 00
VERSION_REPLY = b"\x63\x21\x40\x12\x10"  # measured: XpressNet 4.0, station id 0x12
CMD_STATION_STATUS = cmd_station_status()  # 21 24 05
STATUS_UNPOWERED = b"\x62\x22\x07\x47"  # measured: track power off
STATUS_POWERED = b"\x62\x22\x04\x44"  # auto_start_mode only: track power on
CMD_TRACK_POWER_ON = cmd_track_power_on()  # 21 81 A0
CMD_TRACK_POWER_OFF = cmd_track_power_off()  # 21 80 A1
POWER_ON_REPLY = b"\x61\x01\x60"
POWER_OFF_REPLY = b"\x61\x00\x61"
CMD_EMERGENCY_STOP_ALL = cmd_emergency_stop_all()  # 80 80
CMD_EMERGENCY_STOP_LOCO_3 = cmd_emergency_stop_loco(3, threshold=100)
CMD_EMERGENCY_STOP_LOCO_105 = cmd_emergency_stop_loco(105, threshold=100)
ACK = b"\x01\x04\x05"
INTERFACE_STATUS_USAGE_REPLY = b"\x01\x09\x08"
# Must NOT start 01 0A: link.py's _RETRY_PREFIXES retries any reply whose first two bytes are
# 01 0A, so a scripted single reply of 01 0A xx would be retried once, the FakeTransport script
# would run out, and the test would fail with "unexpected request ...; the script is exhausted"
# instead of exercising this branch at all. 01 0B (XOR 0A) is not a retry prefix.
INTERFACE_STATUS_OTHER_REPLY = b"\x01\x0b\x0a"
UNSUPPORTED_REPLY = b"\x61\x82\xe3"
SHORT_CIRCUIT_REPLY = b"\x61\x12\x73"
TRACK_SHORT_CIRCUIT_REPLY = b"\x61\x08\x69"
BUSY_REPLY = b"\x61\x1f\x7e"
STATION_BUSY_REPLY = b"\x61\x81\xe0"
UNKNOWN_FORM_REPLY = b"\x71\x00\x71"
# Same header and data byte as UNKNOWN_FORM_REPLY (71 00 71), with the trailing XOR corrupted:
# xor(71 00) is 71, not 00, so replies.parse() raises XBusChecksumError and returns
# Other(reason=REASON_CHECKSUM) before it ever reaches the "no row for this form" fallback.
# REASON_CHECKSUM and REASON_UNKNOWN_FORM are two different byte strings here, not just two
# different labels hung on the same one.
BAD_CHECKSUM_REPLY = b"\x71\x00\x00"
EMERGENCY_STOP_BROADCAST_BYTES = b"\x81\x00\x81"
SERVICE_MODE_ENTRY_BROADCAST_BYTES = b"\x61\x02\x63"  # 61 02, another device entered service mode


# -- power and status --------------------------------------------------------


def test_power_on_reads_the_solicited_reply_in_one_exchange(bench):
    bench.expect(CMD_TRACK_POWER_ON, POWER_ON_REPLY)
    bench.station.power_on()
    assert bench.transport.script_pending == []
    assert bench.sent.count(CMD_TRACK_POWER_ON) == 1


def test_power_off_reads_the_solicited_reply_in_one_exchange(bench):
    bench.expect(CMD_TRACK_POWER_OFF, POWER_OFF_REPLY)
    bench.station.power_off()
    assert bench.transport.script_pending == []
    assert bench.sent.count(CMD_TRACK_POWER_OFF) == 1


def test_power_on_disagreement_re_reads_once_then_raises(bench):
    bench.expect(CMD_TRACK_POWER_ON, POWER_OFF_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_UNPOWERED)
    with pytest.raises(TrackPowerError) as caught:
        bench.station.power_on()
    assert bench.clock.monotonic() == pytest.approx(TIMING.power_settle)
    assert bench.transport.script_pending == []
    assert caught.value.details["condition"] == CONDITION_POWER_ON_UNSETTLED


def test_power_off_disagreement_re_reads_once_then_raises(bench):
    bench.expect(CMD_TRACK_POWER_OFF, POWER_ON_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    with pytest.raises(TrackPowerError) as caught:
        bench.station.power_off()
    assert bench.clock.monotonic() == pytest.approx(TIMING.power_settle)
    assert bench.transport.script_pending == []
    assert caught.value.details["condition"] == CONDITION_POWER_OFF_UNSETTLED


def test_a_settle_failure_names_what_was_requested_and_what_the_station_reported(bench):
    """The CLI branches on `condition`, and this raise had none.

    `cli/_errors.default_suggestions` answered a condition-less `TrackPowerError` with
    `railctl power resume` - measured 2026-08-09 to be the telegram that starts every
    locomotive the station still holds a speed for (docs/probe-results.md, run 5). This
    raise is the one that produced that answer for a failed `power off`.

    `requested` and `reported` are separate fields because they are separate facts: what
    the operator asked for is known, and what the track is doing is exactly what did not
    settle.
    """
    bench.expect(CMD_TRACK_POWER_OFF, POWER_ON_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    with pytest.raises(TrackPowerError) as caught:
        bench.station.power_off()
    assert caught.value.details == {
        "condition": CONDITION_POWER_OFF_UNSETTLED,
        "requested": "off",
        "reported": "on",
    }


def test_power_off_invalidates_caches_even_when_it_ultimately_raises(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_TRACK_POWER_OFF, POWER_ON_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    with pytest.raises(TrackPowerError):
        bench.station.power_off()
    assert calls == ["clear"]


def test_version_is_cached_but_status_is_never_cached(bench):
    bench.expect(CMD_STATION_VERSION, VERSION_REPLY)
    first = bench.station.version()
    second = bench.station.version()
    assert first is second
    assert first.version == "4.0"
    assert first.family == "Z21"
    assert bench.sent.count(CMD_STATION_VERSION) == 1

    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.station.status()
    bench.station.status()
    assert bench.sent.count(CMD_STATION_STATUS) == 2


# -- emergency stop -----------------------------------------------------------
# Bench now opens with `default_address=BENCH_DEFAULT_ADDRESS` (3), not None. That default is
# invisible to every test below: `emergency_stop(None)` branches on `address is None` and sends
# `80 80` directly (see `emergency_stop`'s body above) - it never calls `resolve_address`, so
# whatever `bench.station.default_address` happens to be plays no part in what gets sent.


def test_emergency_stop_all_sends_80_80_and_no_power_telegram(bench):
    bench.expect(CMD_EMERGENCY_STOP_ALL, ACK)
    bench.station.emergency_stop()
    assert bench.sent[-1] == CMD_EMERGENCY_STOP_ALL
    assert CMD_TRACK_POWER_ON not in bench.sent
    assert CMD_TRACK_POWER_OFF not in bench.sent


def test_emergency_stop_address_sends_92_and_no_power_telegram(bench):
    bench.expect(CMD_EMERGENCY_STOP_LOCO_3, ACK)
    bench.station.emergency_stop(3)
    assert bench.sent[-1] == CMD_EMERGENCY_STOP_LOCO_3
    assert CMD_TRACK_POWER_ON not in bench.sent
    assert CMD_TRACK_POWER_OFF not in bench.sent
    assert bench.event_names() == []  # address 3 is well below the divergence band


def test_emergency_stop_in_the_divergence_band_emits_address_band_unverified(bench):
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    bench.station.emergency_stop(105)
    assert bench.events == [("address.band_unverified", {"address": 105, "threshold": 100})]


def test_status_call_from_inside_on_event_does_not_deadlock(bench):
    """emit() runs while emergency_stop() still holds the lock; RLock (not Lock) is what lets
    status() re-enter from inside that same callback without hanging the test forever."""
    reentered: list[bool] = []

    def reenter(name: str, payload: dict[str, object]) -> None:
        if name == "address.band_unverified":
            bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
            result = bench.station.status()
            reentered.append(result.track_power)

    bench.on_event_hook = reenter
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    bench.station.emergency_stop(105)
    assert reentered == [True]


def test_a_bad_on_event_callback_cannot_break_the_operation(bench, caplog):
    def explode(name: str, payload: dict[str, object]) -> None:
        raise RuntimeError("bad callback")

    bench.on_event_hook = explode
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    with caplog.at_level(logging.WARNING, logger="railctl.station"):
        bench.station.emergency_stop(105)  # must not raise
    assert "bad callback" in caplog.text


def test_emergency_stop_still_warns_on_the_divergence_band_when_the_station_stays_silent(bench):
    """address 105 is in DIVERGENCE_BAND with threshold unmeasured - exactly the case where
    address.band_unverified is the single most useful hint about why the exchange got no
    answer: the wire form for band 100..127 is unverified until D10 runs. The command is
    scripted with reply=b"" (silence, never implied), so exchange() raises LinkTimeout - and
    the warning must still have been emitted, not skipped because the exchange failed."""
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, reply=b"")
    with pytest.raises(LinkTimeout):
        bench.station.emergency_stop(105)
    assert bench.events == [("address.band_unverified", {"address": 105, "threshold": 100})]


# -- exchange() mapping table -------------------------------------------------


def test_exchange_returns_generic_ack_for_a_plain_command(bench):
    bench.expect(CMD_EMERGENCY_STOP_ALL, ACK)
    reply = bench.station.exchange(CMD_EMERGENCY_STOP_ALL, timeout=TIMING.li_ack_normal)
    assert reply == GENERIC_ACK


def test_exchange_maps_interface_status_09_to_value_error(bench):
    bench.expect(CMD_STATION_VERSION, INTERFACE_STATUS_USAGE_REPLY)
    with pytest.raises(ValueError):
        bench.station.version()


def test_exchange_maps_any_other_interface_status_to_transport_error(bench):
    bench.expect(CMD_STATION_VERSION, INTERFACE_STATUS_OTHER_REPLY)
    with pytest.raises(TransportError) as caught:
        bench.station.version()
    assert "0B" in str(caught.value)  # INTERFACE_STATUS_OTHER_REPLY's code byte is 0x0B


def test_exchange_maps_unsupported_to_unsupported_command_error(bench):
    from railctl.errors import UnsupportedCommandError

    bench.expect(CMD_STATION_VERSION, UNSUPPORTED_REPLY)
    with pytest.raises(UnsupportedCommandError):
        bench.station.version()


def test_exchange_maps_an_unknown_form_to_railctl_error_carrying_the_bytes(bench):
    bench.expect(CMD_STATION_VERSION, UNKNOWN_FORM_REPLY)
    with pytest.raises(RailctlError) as caught:
        bench.station.version()
    assert "71 00 71" in str(caught.value)


def test_exchange_keeps_a_bad_cable_and_an_unknown_reply_form_apart(bench, monkeypatch):
    """xbus/replies.py's own docstring on `Other.reason`: "Collapsing these into one value
    leaves the station unable to tell a bad cable from a reply form we do not know." A
    REASON_CHECKSUM/REASON_LENGTH `Other` is the LINK damaging bytes and gets `ProtocolError`
    (exit 4); a REASON_EMPTY/REASON_UNKNOWN_FORM `Other` is an incomplete reply table and stays
    on the base `RailctlError` (exit 9). This is the one test that pins the two exit codes
    apart from each other - folding either mapping into the other makes this go red.

    BAD_CHECKSUM_REPLY cannot be scripted through `bench.expect()`: the LI-USB envelope
    (`railctl.envelope.liusb.LiUsbEnvelope.pop`) validates the xbus XOR itself while it hunts
    for a frame boundary, so a reply with a bad checksum is discarded as noise before `Link`
    ever hands it back - the exchange would see a timeout, never a damaged reply. `Link.request`
    is monkeypatched directly instead, which is exactly what `exchange()` calls; this proves
    `exchange()`'s own mapping table, not the envelope's frame search.
    """
    original_request = bench.link.request
    monkeypatch.setattr(bench.link, "request", lambda telegram, *, timeout=None: BAD_CHECKSUM_REPLY)
    with pytest.raises(ProtocolError) as bad_cable:
        bench.station.version()
    assert exit_code_for(bad_cable.value) == 4
    monkeypatch.setattr(bench.link, "request", original_request)

    bench.expect(CMD_STATION_VERSION, UNKNOWN_FORM_REPLY)
    with pytest.raises(RailctlError) as unknown_form:
        bench.station.version()
    assert exit_code_for(unknown_form.value) == 9
    assert exit_code_for(bad_cable.value) != exit_code_for(unknown_form.value)


@pytest.mark.parametrize(
    "reply_bytes",
    [SHORT_CIRCUIT_REPLY, TRACK_SHORT_CIRCUIT_REPLY, BUSY_REPLY, STATION_BUSY_REPLY],
    ids=["short_circuit", "track_short_circuit", "busy", "station_busy"],
)
def test_exchange_returns_transient_replies_unchanged(bench, reply_bytes):
    """None of TRANSIENT_REPLIES' five members says anything about whether an opcode is
    implemented (xbus/replies.py's own docstring on TRANSIENT_REPLIES), so exchange() must not
    turn any of them into an exception - StationBusy included, even though it is the one member
    that can follow ANY command. A later CV task needs the raw reply back so it can attach the
    CV number ProgrammingError carries, which exchange() has no way to know. The fifth member,
    TransferError, is not exercised here: Link retries a 61 80 reply once and raises
    LinkProtocolError itself on a second one (_RETRY_PREFIXES), so a bare TransferError can
    never actually reach exchange() through link.request() to be scripted as a single reply."""
    bench.expect(CMD_STATION_VERSION, reply_bytes)
    reply = bench.station.exchange(CMD_STATION_VERSION, timeout=TIMING.li_ack_normal)
    assert reply in TRANSIENT_REPLIES


# -- capability learning -------------------------------------------------------


def test_learn_refuses_a_field_outside_learnable_fields(bench):
    with pytest.raises(ValueError):
        bench.station.learn(z21_cv_opcodes=True)


def test_learn_accepts_every_learnable_field(bench):
    bench.station.learn(pom_read=True, pom_result_channel="broadcast")
    assert bench.station.capabilities.pom_read is True
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_record_accepts_any_field_including_non_learnable_ones(bench):
    bench.station.record(z21_cv_opcodes=True, function_groups_4_5=False)
    assert bench.station.capabilities.z21_cv_opcodes is True
    assert bench.station.capabilities.function_groups_4_5 is False


# -- session lifecycle ---------------------------------------------------------


def test_close_flushes_learned_capabilities_when_a_path_is_set(bench_factory, tmp_path):
    path = tmp_path / "capabilities.json"
    fixture = bench_factory(capabilities_path=path)
    fixture.station.learn(pom_read=True)
    fixture.station.close()
    assert path.exists()
    # "bench", not "fake": Bench seeds its Capabilities with BENCH_IDENTITY ("bench"), a label
    # chosen independently of FakeTransport's own identity ("fake") - see ADDENDUM §A.2, "no test
    # may assert those two are equal". Loading under "fake" here would read no entry at all and
    # silently leave reloaded.pom_read at None, which is a false pass, not a real one.
    reloaded = Capabilities.load(path, "bench")
    assert reloaded.pom_read is True
    assert CMD_TRACK_POWER_ON not in fixture.sent
    assert CMD_TRACK_POWER_OFF not in fixture.sent


def test_close_does_not_write_capabilities_when_nothing_was_learned(bench_factory, tmp_path):
    path = tmp_path / "capabilities.json"
    fixture = bench_factory(capabilities_path=path)
    fixture.station.close()
    assert not path.exists()


def test_close_never_writes_capabilities_for_an_unknown_link_identity(tmp_path):
    """A transport with no stable identity must never grow a `capabilities.json` entry keyed
    "unknown" - two unrelated stations would then silently share one profile."""
    envelope = LiUsbEnvelope()
    clock = FakeClock()
    transport = FakeTransport(clock=clock, identity=UNKNOWN_IDENTITY)
    link = Link(transport, envelope, clock=clock)
    transport.expect(
        envelope.frame(Kind.SOLICITED, CMD_STATION_VERSION),
        reply=envelope.frame(Kind.SOLICITED, VERSION_REPLY),
    )
    link.open()
    path = tmp_path / "capabilities.json"
    station = Station(
        link,
        Capabilities.unknown(UNKNOWN_IDENTITY),
        capabilities_path=path,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    station.learn(pom_read=True)
    station.close()
    assert not path.exists()


def test_close_and_power_off_invalidate_registered_caches(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_TRACK_POWER_OFF, POWER_OFF_REPLY)
    bench.station.power_off()
    assert calls == ["clear"]
    bench.station.close()
    assert calls == ["clear", "clear"]


def test_version_and_status_do_not_invalidate_registered_caches(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_STATION_VERSION, VERSION_REPLY)
    bench.station.version()
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.station.status()
    assert calls == []


def test_threshold_defaults_to_xpressnet_when_capabilities_unset(bench):
    assert bench.station.threshold == XPRESSNET.long_address_threshold == 100


def test_threshold_uses_capabilities_when_measured(bench_factory):
    caps = Capabilities.unknown("fake").with_learned(loco_address_threshold=128)
    fixture = bench_factory(capabilities=caps)
    assert fixture.station.threshold == 128


# -- events() ------------------------------------------------------------------


def test_events_do_not_hold_the_lock_across_a_yield(bench):
    """threading.RLock is reentrant PER THREAD, and a generator body runs in whichever thread
    calls next() - so a same-thread interleaved call (e.g. bench.station.status() from the test
    body) would succeed via reentrancy even if events() held the lock across the yield, and
    would never pin the discipline this test is named for. Only a different thread can observe
    whether the lock is actually free at the yield point: it blocks forever on a real
    threading.Lock-style contested acquire, but returns immediately once events() has released
    the lock and is suspended at `yield`."""
    bench.push(EMERGENCY_STOP_BROADCAST_BYTES)
    bench.push(POWER_ON_REPLY)
    iterator = bench.station.events(interval=0.0)

    first = next(iterator)
    assert first.name == "loco.emergency_stop"

    acquired_from_other_thread: list[bool] = []

    def try_acquire() -> None:
        got = bench.station._lock.acquire(timeout=0.2)
        acquired_from_other_thread.append(got)
        if got:
            bench.station._lock.release()

    other = threading.Thread(target=try_acquire)
    other.start()
    other.join(timeout=1.0)
    assert not other.is_alive(), "another thread's acquire is still blocked after 1s"
    assert acquired_from_other_thread == [True]

    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    # This call would deadlock on the same RLock if events() held it across the yield above.
    assert bench.station.status().track_power is True

    second = next(iterator)
    assert second.name == "power.on"
    assert second.payload["telegram"] == "61 01 60"


def test_events_decodes_a_power_off_broadcast(bench):
    bench.push(POWER_OFF_REPLY)
    iterator = bench.station.events(interval=0.0)
    event = next(iterator)
    assert event.name == "power.off"
    assert event.detail == "track power turned off"
    assert event.payload["telegram"] == "61 00 61"


def test_events_decodes_a_service_mode_entry_broadcast(bench):
    bench.push(SERVICE_MODE_ENTRY_BROADCAST_BYTES)
    iterator = bench.station.events(interval=0.0)
    event = next(iterator)
    assert event.name == "service.entered"
    assert event.detail == "another device entered service mode"
    assert event.payload["telegram"] == "61 02 63"


def test_events_keeps_an_undecoded_broadcast_visible_as_unknown_rather_than_dropping_it(bench):
    """reply.unknown is the branch that keeps an undecoded broadcast visible rather than
    silently swallowed - its name/detail/payload are what M6's `monitor` will render."""
    bench.push(UNKNOWN_FORM_REPLY)
    iterator = bench.station.events(interval=0.0)
    event = next(iterator)
    assert event.name == "reply.unknown"
    assert event.detail == "undecoded broadcast: 71 00 71"
    assert event.payload["telegram"] == "71 00 71"


def test_power_on_settles_cleanly_when_the_re_read_agrees(bench):
    """A disagreeing command reply gets exactly one status() re-read after power_settle; when
    that re-read agrees with what was commanded, power_on() returns without raising and sends
    exactly two telegrams - the command itself and the one re-read, never a retry loop."""
    bench.expect(CMD_TRACK_POWER_ON, POWER_OFF_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.station.power_on()
    assert bench.sent == [CMD_TRACK_POWER_ON, CMD_STATION_STATUS]
    assert bench.transport.script_pending == []
