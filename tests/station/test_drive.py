# tests/station/test_drive.py
"""Station.drive, .loco_info and the function family, including the E4 F8
single-function path.

Every telegram here is a literal byte string, computed by hand from the
wire layouts in xbus/commands.py and xbus/replies.py, not by calling the
encoders under test a second time: a station-level test that built its own
expectations by calling cmd_drive_128 would go green even if cmd_drive_128
and Station.drive agreed on a WRONG byte, because both sides would be
wrong the same way. tests/unit/test_xbus_commands.py is where the encoders
themselves are pinned against the design document; this file is where the
FACADE's choices - which encoder, which capability gate, which telegram
gets sent at all - are pinned.
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    LinkTimeout,
    StationError,
    UnsupportedCommandError,
    UnsupportedFeatureError,
)
from railctl.xbus.speed import Direction

ACK = b"\x01\x04\x05"
UNSUPPORTED = b"\x61\x82\xe3"
STATUS_REPLY = b"\x62\x22\x07\x47"  # a real reply, but not the one any method here expects
# E5 00 00 00 00 00 <xor>: a well-formed, correctly-checksummed telegram
# under a header nobody has listed - spec line 704's "extended loco info"
# form. XOR of E5 with five zero data bytes is E5 itself, so the xor byte
# is E5 too. parse() has no branch for 0xE5, so this becomes
# Other(telegram, reason=REASON_UNKNOWN_FORM) - exactly the shape the
# EXTENDED_LOCO_INFO_HEADERS branch in exchange() exists to catch before
# the reason-based Other dispatch calls it a bare RailctlError.
EXTENDED_LOCO_INFO_REPLY_E5 = b"\xe5\x00\x00\x00\x00\x00\xe5"

# drive(3, 30, FORWARD) at the default (XpressNet) threshold: wire step is
# 30 + 1 = 31 = 0x1F, with the direction bit (0x80) set for FORWARD.
DRIVE_30_FWD_REQUEST = b"\xe4\x13\x00\x03\x9f\x6b"

LOCO_INFO_REQUEST = b"\xe3\x00\x00\x03\xe0"
# 128-step mode (ident low 3 bits = 0b100), not busy, stopped, forward, no
# functions on: ident=0x04, raw_speed=0x80 (STOP_WIRE with the direction bit).
LOCO_INFO_REPLY_IDLE = b"\xe4\x04\x80\x00\x00\x60"
# Same, but bit 3 of ident (0x08) set: another device holds this locomotive.
LOCO_INFO_REPLY_BUSY = b"\xe4\x0c\x80\x00\x00\x68"
# ident low 3 bits = 0b000: 14-step mode. raw_speed is deliberately NOT a
# valid 128-step byte's worth of anything meaningful - it must never be
# decoded, only preserved.
LOCO_INFO_REPLY_14_STEP = b"\xe4\x00\x05\x00\x00\xe1"

FUNCTION_STATE_REQUEST = b"\xe3\x09\x00\x03\xe9"
# E3 52 D1 D2: D1 bit 0 is F13, so F13 on and F14..F28 off.
FUNCTION_STATE_REPLY_F13_ON = b"\xe3\x52\x01\x00\xb0"

# E4 F8 00 03 <payload> <xor>: F0 is bit 4 of the G1 byte, so its single-
# function index is 0; ON is TT=01.
FUNCTION_SINGLE_F0_ON = b"\xe4\xf8\x00\x03\x40\x5f"
# F14's index is 14; ON is TT=01: payload = (1 << 6) | 14 = 0x4E.
FUNCTION_SINGLE_F14_ON = b"\xe4\xf8\x00\x03\x4e\x51"

# E4 20 AH AL BITS X: G1 bits, F2 on (bit 1) and F0/F1/F3/F4 off.
FUNCTION_GROUP_G1_F2_ON = b"\xe4\x20\x00\x03\x02\xc5"
# G1 bits, F0 on (bit 4) and F1..F4 off - the group write a fresh loco_info()
# (all functions off) plus an unreadable F13..F28 read still allows, because
# G1 is always fully known.
FUNCTION_GROUP_G1_F0_ON = b"\xe4\x20\x00\x03\x10\xd7"
# E4 23 AH AL BITS X: G4 bits, F14 on (bit 1) and F13/F15..F20 seeded False.
FUNCTION_GROUP_G4_F14_ON_SEEDED = b"\xe4\x23\x00\x03\x02\xc6"

# drive(address, 0, FORWARD) - wire speed byte is always 0x80 (STOP_WIRE
# with the direction bit) regardless of address - at addresses that straddle
# DIVERGENCE_BAND = range(100, 128), default threshold 100.
DRIVE_STOP_FWD_BY_ADDRESS = {
    99: b"\xe4\x13\x00\x63\x80\x14",
    100: b"\xe4\x13\xc0\x64\x80\xd3",
    127: b"\xe4\x13\xc0\x7f\x80\xc8",
    128: b"\xe4\x13\xc0\x80\x80\x37",
}

# loco_info(100) with capabilities.loco_address_threshold overridden to 128:
# 100 < 128, so the address goes out SHORT even though it sits inside
# DIVERGENCE_BAND.
LOCO_INFO_REQUEST_ADDR_100_THR_128 = b"\xe3\x00\x00\x64\x87"


# -- drive() ----------------------------------------------------------------


def test_drive_sends_the_128_step_telegram_and_expects_the_ack(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, ACK)
    bench.station.drive(3, 30, Direction.FORWARD)
    assert bench.sent == [DRIVE_30_FWD_REQUEST]


@pytest.mark.parametrize("speed", [-1, 127])
def test_drive_rejects_out_of_range_speed_before_sending_anything(bench, speed):
    with pytest.raises(ValueError, match="speed"):
        bench.station.drive(3, speed, Direction.FORWARD)
    assert bench.sent == []


@pytest.mark.parametrize("address", [0, 10000])
def test_drive_rejects_out_of_range_address_before_sending_anything(bench, address):
    with pytest.raises(ValueError, match="loco address"):
        bench.station.drive(address, 30, Direction.FORWARD)
    assert bench.sent == []


def test_drive_treats_a_refusal_as_unsupported_command(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        bench.station.drive(3, 30, Direction.FORWARD)


def test_drive_raises_station_error_on_an_unrecognized_reply(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, STATUS_REPLY)
    with pytest.raises(StationError):
        bench.station.drive(3, 30, Direction.FORWARD)


# -- loco_info() --------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "busy"),
    [(LOCO_INFO_REPLY_IDLE, False), (LOCO_INFO_REPLY_BUSY, True)],
)
def test_loco_info_fills_the_address_and_flags_in_use_by_other(bench, reply, busy):
    bench.expect(LOCO_INFO_REQUEST, reply)
    info = bench.station.loco_info(3)
    assert info.address == 3
    assert info.in_use_by_other is busy
    if busy:
        assert bench.event_names() == ["loco.in_use_by_other"]
        assert bench.events[0][1] == {"address": 3}
    else:
        assert bench.event_names() == []


def test_loco_info_in_14_step_mode_leaves_speed_none_and_keeps_raw_speed(bench):
    """The facade must not decode a non-128-step reply with the 128-step
    layout - it only ever passes through what replies.py already decided."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_14_STEP)
    info = bench.station.loco_info(3)
    assert info.speed_steps == 14
    assert info.speed is None
    assert info.direction is None
    assert info.emergency_stopped is None
    assert info.raw_speed == 0x05


def test_loco_info_raises_station_error_on_an_unrecognized_reply(bench):
    bench.expect(LOCO_INFO_REQUEST, STATUS_REPLY)
    with pytest.raises(StationError):
        bench.station.loco_info(3)


def test_loco_info_raises_unsupported_feature_for_an_extended_e5_reply(bench):
    """E5/E2 are extended loco-info forms this station has not been probed
    for (spec line 704) - a feature this project has decided is out of
    scope, not an unrecognised reply. exchange() must raise
    UnsupportedFeatureError here, distinct from the bare StationError the
    test above gets for a form that really is unknown."""
    bench.expect(LOCO_INFO_REQUEST, EXTENDED_LOCO_INFO_REPLY_E5)
    with pytest.raises(UnsupportedFeatureError):
        bench.station.loco_info(3)


# -- address resolution -------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "band"),
    [(99, False), (100, True), (127, True), (128, False)],
)
def test_address_in_the_divergence_band_emits_once_and_the_edges_emit_nothing(bench, address, band):
    bench.expect(DRIVE_STOP_FWD_BY_ADDRESS[address], ACK)
    bench.station.drive(address, 0, Direction.FORWARD)
    if band:
        assert bench.events == [("address.band_unverified", {"address": address, "threshold": 100})]
    else:
        assert bench.events == []


def test_effective_threshold_comes_from_capabilities_not_the_default(bench_factory):
    """capabilities.loco_address_threshold overrides the XpressNet default:
    with it set to 128, address 100 - inside DIVERGENCE_BAND - goes out
    SHORT, and no band-unverified event fires, because the ambiguity this
    event exists to flag has been resolved."""
    fixture = bench_factory(loco_address_threshold=128)
    fixture.expect(LOCO_INFO_REQUEST_ADDR_100_THR_128, LOCO_INFO_REPLY_IDLE)
    fixture.station.loco_info(100)
    assert fixture.event_names() == []


# -- function_state() ----------------------------------------------------------


def test_function_state_leaves_f13_28_absent_when_the_request_is_unsupported(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    state = bench.station.function_state(3)
    assert len(state) == 13
    assert 13 not in state
    assert all(value is False for value in state.values())


def test_function_state_leaves_f13_28_absent_when_the_request_times_out(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, reply=b"")  # no reply queued -> LinkTimeout
    state = bench.station.function_state(3)
    assert len(state) == 13
    assert 13 not in state


def test_function_state_reads_f13_28_when_the_request_succeeds(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    state = bench.station.function_state(3)
    expected = dict.fromkeys(range(29), False)
    expected[13] = True
    assert state == expected


# -- function_set()/function_toggle(): range validation -------------------------


@pytest.mark.parametrize("function", [-1, 29])
def test_function_set_rejects_out_of_range_function_before_sending_anything(bench, function):
    with pytest.raises(ValueError, match="function"):
        bench.station.function_set(3, function, True)
    assert bench.sent == []


@pytest.mark.parametrize("function", [-1, 29])
def test_function_toggle_rejects_out_of_range_function_before_sending_anything(bench, function):
    with pytest.raises(ValueError, match="function"):
        bench.station.function_toggle(3, function)
    assert bench.sent == []


# -- function_set(): tri-state dispatch ----------------------------------------


def test_function_set_prefers_single_function_when_capability_is_true(bench_factory):
    """The preferred path is chosen ONLY on single_function_cmd is True, and
    it sends exactly one telegram - no loco_info, no E3 09, no shadow."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    fixture.station.function_set(3, 0, True)
    assert fixture.sent == [FUNCTION_SINGLE_F0_ON]


def test_function_set_uses_the_group_path_when_capability_is_false(bench_factory):
    fixture = bench_factory(single_function_cmd=False)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G1_F0_ON, ACK)
    fixture.station.function_set(3, 0, True)
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_GROUP_G1_F0_ON]


def test_function_set_uses_the_group_path_when_capability_is_none(bench):
    """None is the default XpressNet cannot promise the single-function
    command exists, so the fall-through from True/False alone would hide a
    bug here - this is the third, separate case the design calls out."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    bench.expect(FUNCTION_GROUP_G1_F0_ON, ACK)
    bench.station.function_set(3, 0, True)
    assert bench.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_GROUP_G1_F0_ON]


# -- function_set(): the group path never guesses ------------------------------


def test_function_set_on_a_fully_known_group_needs_no_force_group(bench_factory):
    """F2 lives in G1, and G1 is always fully known from loco_info() alone -
    an unreadable F13..F28 must not block a write to a group that never
    depended on that read."""
    fixture = bench_factory(single_function_cmd=False)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G1_F2_ON, ACK)
    fixture.station.function_set(3, 2, True)


def test_function_set_on_an_unknown_group_member_refuses_and_sends_no_group_write(
    bench_factory,
):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    with pytest.raises(StationError) as caught:
        fixture.station.function_set(3, 14, True)
    assert caught.value.hint == "--force-group"
    # The read happened - it has to, to discover the group is incomplete -
    # but the write (E4 23) must never be among these two.
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST]


def test_function_set_with_force_group_seeds_false_and_emits_an_event(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G4_F14_ON_SEEDED, ACK)
    fixture.station.function_set(3, 14, True, force_group=True)
    assert fixture.sent == [
        LOCO_INFO_REQUEST,
        FUNCTION_STATE_REQUEST,
        FUNCTION_GROUP_G4_F14_ON_SEEDED,
    ]
    assert fixture.event_names() == ["function.group_seeded"]
    _name, payload = fixture.events[0]
    assert payload["address"] == 3
    assert payload["group"] == "G4"
    assert set(payload["functions"]) == {13, 15, 16, 17, 18, 19, 20}


def test_function_set_single_path_drops_the_shadow_so_a_stale_value_is_never_reported(
    bench_factory,
):
    """A single-function write touches exactly one function and never
    updates the shadow with a value of its own - but a shadow entry left
    UNTOUCHED after the write is just as wrong as one holding a bad value:
    the next refresh=False function_state() call would answer straight
    from the stale cache and report F0's OLD state, even though the write
    just changed it. Dropping the entry forces that next read back onto
    the wire, so this pins the telegram count rather than the boolean -
    a wrong VALUE from a wrongly-defaulted shadow and a wrong VALUE from an
    untouched one look identical from outside, only the wire traffic tells
    them apart."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    state = fixture.station.function_state(3)
    assert state[0] is False
    assert len(fixture.sent) == 2

    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    fixture.station.function_set(3, 0, True)
    assert len(fixture.sent) == 3

    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.station.function_state(3)
    assert len(fixture.sent) == 5


def test_function_set_single_path_drops_the_shadow_on_a_timeout_too(bench_factory):
    """The write's outcome can also be UNKNOWN, not just successful: silence
    after the single-function telegram raises LinkTimeout (CLAUDE.md -
    "Silence is unknown"). The shadow entry `function_state(3)` seeded a
    moment earlier must not survive that either - if it did, the very next
    function_state() would answer F0's OLD value as a settled False with no
    wire traffic, even though the station may well have executed the write.
    The drop has to happen before the exchange is attempted, not after it
    succeeds, so a raise never skips it."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    state = fixture.station.function_state(3)
    assert state[0] is False
    assert len(fixture.sent) == 2

    fixture.expect(FUNCTION_SINGLE_F0_ON, reply=b"")  # scripted silence -> LinkTimeout
    with pytest.raises(LinkTimeout):
        fixture.station.function_set(3, 0, True)
    assert len(fixture.sent) == 3

    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.station.function_state(3)
    assert len(fixture.sent) == 5


def test_function_set_group_path_drops_the_shadow_on_a_timeout_too(bench):
    """Same finding as above, group path: `_function_set_group_path` seeds
    the shadow with the pre-write group state via its own
    `function_state(refresh=True)` call moments before the group write. A
    write that then raises LinkTimeout must not leave that pre-write entry
    behind - it does not reflect `function`'s requested new value, and
    serving it as fact would be wrong even though it came from a real
    read."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    bench.expect(FUNCTION_GROUP_G1_F0_ON, reply=b"")  # scripted silence -> LinkTimeout
    with pytest.raises(LinkTimeout):
        bench.station.function_set(3, 0, True)
    assert len(bench.sent) == 3

    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    bench.station.function_state(3)
    assert len(bench.sent) == 5


# -- function_toggle() ---------------------------------------------------------


def test_function_toggle_reads_first_and_sends_an_explicit_action(bench_factory):
    """F0 is off in LOCO_INFO_REPLY_IDLE, so the toggle must send ON - never
    the TOGGLE wire value - and return True, a fact read back from the
    telegram it built, not a guess."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    assert fixture.station.function_toggle(3, 0) is True
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_SINGLE_F0_ON]


def test_function_toggle_single_path_drops_the_shadow_so_a_stale_value_is_never_reported(
    bench_factory,
):
    """Same failure mode as the function_set test above, reached through
    function_toggle instead: `function_toggle(3, 0)` returns True, and a
    following function_state(3) must not answer False from a shadow the
    write never touched."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    assert fixture.station.function_toggle(3, 0) is True
    assert len(fixture.sent) == 3

    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.station.function_state(3)
    assert len(fixture.sent) == 5


def test_function_toggle_single_path_drops_the_shadow_on_a_timeout_too(bench_factory):
    """Same finding as function_set's timeout test above, reached through
    function_toggle instead: the pop has to happen BEFORE the exchange is
    attempted, not after it succeeds. function_toggle first does a
    refresh=True read (which itself seeds the shadow), then computes
    new_value and writes it. If the pop moved to after `_expect_ack()`, a
    LinkTimeout on the write would leave that refresh=True read's value
    behind as a settled fact - the exact stale-shadow failure this file
    already pins for function_set, just unreached for function_toggle until
    now, because ACK-only scripting can't tell "popped before" from "popped
    after" apart: both leave the shadow empty once the call returns
    normally. Only a raise between the two candidate pop points can tell
    them apart."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    state = fixture.station.function_state(3)
    assert state[0] is False
    assert len(fixture.sent) == 2

    # function_toggle's own refresh=True read re-seeds the shadow with F0
    # still False, right before the write that flips it - reply=b"" scripts
    # silence on that write, so the exchange raises LinkTimeout.
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_SINGLE_F0_ON, reply=b"")  # scripted silence -> LinkTimeout
    with pytest.raises(LinkTimeout):
        fixture.station.function_toggle(3, 0)
    assert len(fixture.sent) == 5

    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.station.function_state(3)
    assert len(fixture.sent) == 7


def test_function_toggle_uses_the_group_path_and_reads_state_only_once(bench):
    """The default fixture leaves single_function_cmd unset (None), so this
    is the group path - previously unreached by any test in this file.
    function_toggle already does one refresh=True read to compute the new
    value; the group path must reuse it rather than repeating the same
    loco_info + E3 09 pair a second time before the write."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    bench.expect(FUNCTION_GROUP_G1_F2_ON, ACK)
    assert bench.station.function_toggle(3, 2) is True
    assert bench.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_GROUP_G1_F2_ON]


def test_function_toggle_raises_when_state_is_unknown_and_sends_nothing(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    with pytest.raises(StationError) as caught:
        fixture.station.function_toggle(3, 14)
    assert caught.value.hint == "--force-group"
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST]


# -- F13..F28 capability gating, one path each ---------------------------------


def test_f13_28_on_the_group_path_needs_function_groups_4_5(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=False)
    with pytest.raises(UnsupportedFeatureError):
        fixture.station.function_set(3, 14, True)
    assert fixture.sent == []


def test_f13_28_on_the_single_function_path_only_needs_single_function_cmd(bench_factory):
    """function_groups_4_5 is False here on purpose: the single-function
    command reaches F13..F28 by a completely different wire form and does
    not depend on the group capability at all."""
    fixture = bench_factory(single_function_cmd=True, function_groups_4_5=False)
    fixture.expect(FUNCTION_SINGLE_F14_ON, ACK)
    fixture.station.function_set(3, 14, True)
    assert fixture.sent == [FUNCTION_SINGLE_F14_ON]


# -- shadow invalidation --------------------------------------------------------


def test_forget_loco_drops_the_shadow(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    # Cached: a second refresh=False call sends nothing more.
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    bench.station.forget_loco(3)
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 4


def test_invalidate_caches_drops_the_shadow(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    bench.station.invalidate_caches()
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 4
