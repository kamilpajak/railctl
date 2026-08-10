# tests/station/test_cv_write.py
from __future__ import annotations

import pytest

from railctl.errors import (
    CvOutOfRangeError,
    CvVerifyError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    ShortCircuitError,
    StationBusyError,
    TrackPowerError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import PageKey, TimedOut
from railctl.station.timing import TIMING
from railctl.station.types import (
    ADDRESS_CVS,
    CV29_LONG_ADDRESS_BIT,
    CvReadOutcome,
    CvResult,
    CvSpec,
    ProgMode,
)
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_pom_read_byte,
    cmd_pom_write_byte,
    cmd_service_direct_write,
    cmd_service_ext_write,
    cmd_service_result_request,
    cmd_station_status,
    cmd_track_power_on,
    cmd_z21_cv_write,
)
from railctl.xbus.dialect import CvEncoding

ADDRESS = 3
THRESHOLD = 100

ACK = encode(0x01, 0x04)
STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x06)  # measured after 21 80; bit 1 is emergency off
UNSUPPORTED = encode(0x61, 0x82)


def make_capabilities(**overrides: object) -> Capabilities:
    return Capabilities.unknown("bench").with_learned(**overrides)


def make_cv_result(
    *,
    cv: int = 0,
    value: int,
    mode: ProgMode = ProgMode.POM,
    encoding: CvEncoding = CvEncoding.POM_ZERO_BASED,
    operation: str = "read",
    verified: bool | None = None,
) -> CvResult:
    return CvResult(
        cv=cv,
        value=value,
        mode=mode,
        encoding=encoding,
        operation=operation,
        verified=verified,
        elapsed=0.0,
    )


def watch_invalidations(station) -> list[None]:
    """`Station.invalidate_caches()` takes no address and unconditionally calls every
    registered callback - a spy is the direct way to count calls without depending on
    what else those callbacks clear.
    """
    calls: list[None] = []
    station.register_cache(lambda: calls.append(None))
    return calls


# -- reads_available -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "overrides", "expected"),
    [
        (ProgMode.POM, {"pom_read": True}, True),
        (ProgMode.POM, {"pom_read": False}, False),
        (ProgMode.POM, {"pom_read": None}, False),
        (ProgMode.SERVICE, {"z21_cv_opcodes": True}, True),
        (ProgMode.SERVICE, {"service_direct_cv": True}, True),
        (ProgMode.SERVICE, {"service_ext_cv": True}, True),
        (ProgMode.SERVICE, {}, False),
    ],
)
def test_reads_available(mode, overrides, expected, bench_factory):
    bench = bench_factory(capabilities=make_capabilities(**overrides))
    assert bench.station.programmer.reads_available(mode) is expected


def test_raw_cv_write_bypasses_ensure_page(bench, monkeypatch):
    programmer = bench.station.programmer

    def guard(*args, **kwargs):
        raise AssertionError("raw_cv_write must not call ensure_page")

    monkeypatch.setattr(programmer, "ensure_page", guard)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    programmer.raw_cv_write(31, 10, address=ADDRESS, mode=ProgMode.POM)


# -- ensure_page, select_page, and the page cache -----------------------------


def test_ensure_page_is_a_no_op_outside_the_indexed_range(bench):
    programmer = bench.station.programmer
    before = list(bench.sent)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 8, page=(9, 9))
    assert bench.sent == before


def test_ensure_page_requires_a_page_for_an_indexed_cv(bench):
    programmer = bench.station.programmer
    before = list(bench.sent)
    with pytest.raises(IndexPageRequiredError) as caught:
        programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=None)
    assert bench.sent == before
    assert caught.value.cv == 265


def test_ensure_page_cache_hit_sends_nothing(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    before = list(bench.sent)
    bench.clock.advance(5.0)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert bench.sent == before


def test_ensure_page_reselects_after_the_ttl_expires(bench):
    from railctl.station.timing import TIMING

    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    bench.clock.advance(TIMING.page_cache_ttl + 0.1)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert len(bench.sent) == 4


def test_select_page_verifies_the_first_time_when_reads_are_available(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(
            cv=cv, value={31: 10, 32: 2}[cv], mode=mode
        ),
    )
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert bench.events == []


def test_select_page_raises_cv_verify_error_when_the_page_did_not_stick(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=0, mode=mode),
    )
    with pytest.raises(CvVerifyError) as caught:
        programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert caught.value.cv == 31
    assert "did not stick" in str(caught.value)


def test_select_page_verifies_a_different_page_selected_under_the_same_key(bench, monkeypatch):
    """`_verified_pages` has to remember WHICH page it verified, not merely
    that some page once was, under this `(address, mode)` key - otherwise a
    second, different page selected under the same key skips the read-back
    and reports itself trustworthy on faith. `cv_read_many` (Task 6c) calls
    `select_page(page, force=True)` at the head of every group; if only the
    first group's page were ever verified, every later group's page would
    silently go unchecked.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    current: dict[int, int] = {}
    read_calls: list[int] = []

    def fake_cv_read(cv, *, address, mode, page=None):
        read_calls.append(cv)
        return make_cv_result(cv=cv, value=current[cv], mode=mode)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)

    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    current.update({31: 10, 32: 2})
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert read_calls == [31, 32]

    read_calls.clear()
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 16, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 0, threshold=THRESHOLD), reply=ACK)
    current.update({31: 16, 32: 0})
    programmer.select_page((16, 0), address=ADDRESS, mode=ProgMode.POM)
    assert read_calls == [31, 32]


def test_select_page_reverifies_the_same_page_after_invalidate_caches(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    read_calls: list[int] = []

    def fake_cv_read(cv, *, address, mode, page=None):
        read_calls.append(cv)
        return make_cv_result(cv=cv, value={31: 10, 32: 2}[cv], mode=mode)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)

    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert read_calls == [31, 32]

    bench.station.invalidate_caches()

    read_calls.clear()
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert read_calls == [31, 32]


def test_select_page_emits_unverified_when_reads_are_not_available(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert bench.events == [("page.unverified", {"page": (10, 2), "mode": ProgMode.POM})]


def test_select_page_force_reselects_even_within_the_ttl(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM, force=True)
    assert len(bench.sent) == 4


def test_select_page_invalidates_the_cache_when_the_page_write_fails(bench):
    """The `RailctlError` branch inside `select_page`'s own `raw_cv_write` pair -
    rule 12's "any `RailctlError` from a CV operation" clause. Decided unconditionally
    rather than left as a coverage-gap maybe: `select_page` writes CV31/CV32 through
    `raw_cv_write`, and a station that refuses one of those writes has left the page
    cache pointing at a page that was never actually selected.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    invalidated = watch_invalidations(bench.station)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=encode(0x61, 0x82))
    with pytest.raises(UnsupportedCommandError):
        programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert len(invalidated) == 1


def test_the_page_cache_is_cleared_by_the_shared_invalidation_hook(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert programmer._pages != {}
    bench.station.invalidate_caches()  # what power_off(), close() and exit_service_mode() all call
    assert programmer._pages == {}
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert len(bench.sent) == 4


# -- ensure_page wired into the read paths (spec line 709) ----------------------


def test_reading_an_indexed_cv_without_a_page_raises_before_any_telegram(bench):
    before = list(bench.sent)
    with pytest.raises(IndexPageRequiredError) as caught:
        bench.station.programmer.cv_read(265, address=ADDRESS, mode=ProgMode.POM, page=None)
    assert bench.sent == before
    assert caught.value.cv == 265


def test_reading_an_indexed_cv_with_a_page_selects_it_first(bench, monkeypatch):
    """`cv_read` only validates that a page was given; the actual selection
    happens inside `pom_read`, once its own track-power and address checks
    have passed - so this drives a real (unmocked) `pom_read` and only
    stubs `select_page` itself, rather than replacing `pom_read` wholesale,
    which would hide whether selection ever actually ran on this path.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True, pom_echo_zero_based=True)
    select_calls = []
    monkeypatch.setattr(
        programmer,
        "select_page",
        lambda page, *, address, mode, force: select_calls.append((page, address, mode, force)),
    )
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom265 = cmd_pom_read_byte(ADDRESS, 265, threshold=THRESHOLD)
    bench.expect(pom265, reply=encode(0x63, 0x15, 8, 7))  # CV265, zero-based echo byte 8

    result = programmer.cv_read(265, address=ADDRESS, mode=ProgMode.POM, page=(10, 2))

    assert select_calls == [((10, 2), ADDRESS, ProgMode.POM, False)]
    assert result.value == 7


def test_pom_read_never_selects_a_page_when_the_track_is_unpowered(bench):
    """Reproduces the review finding directly: page selection must not run -
    and so must not get cached as selected - ahead of the track-power check.
    The buggy order sent the two CV31/CV32 write telegrams and cached the
    page as selected before ever checking `status().track_power`, so a
    later read within the TTL would trust a page the decoder never actually
    received. Scripting only `STATUS_REQUEST` means a page-select write
    attempted first fails immediately as an unscripted request, rather than
    silently succeeding.
    """
    programmer = bench.station.programmer
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)

    with pytest.raises(TrackPowerError):
        programmer.pom_read(265, address=ADDRESS, page=(10, 2))

    assert bench.sent == [STATUS_REQUEST]
    assert programmer._pages == {}


def test_cv_read_never_selects_a_page_when_the_track_is_unpowered(bench):
    """The same hazard, driven through `cv_read` rather than `pom_read`
    directly - `cv_read`'s own call site used to select the page ahead of
    `pom_read` being reached at all, so fixing `pom_read` alone would not
    have closed this path.
    """
    programmer = bench.station.programmer
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)

    with pytest.raises(TrackPowerError):
        programmer.cv_read(265, address=ADDRESS, mode=ProgMode.POM, page=(10, 2))

    assert bench.sent == [STATUS_REQUEST]
    assert programmer._pages == {}


# -- pom_write: guards and verification --------------------------------------


def test_pom_write_refuses_before_sending_when_pom_read_is_known_false(bench):
    bench.station.learn(pom_read=False)
    before = list(bench.sent)
    with pytest.raises(PomReadUnsupportedError) as caught:
        bench.station.programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert bench.sent == before
    assert caught.value.hint == (
        "cannot verify POM writes on this station; re-run with `--no-verify` "
        "or use `--mode service`"
    )


def test_pom_write_probes_pom_read_first_when_unknown_and_refuses_if_that_fails(bench, monkeypatch):
    programmer = bench.station.programmer
    calls: list[tuple[int, int]] = []
    original_error = DecoderNotRespondingError("nothing came back", cv=5)

    def fake_pom_read(cv, *, address, page=None):
        calls.append((cv, address))
        raise original_error

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(PomReadUnsupportedError) as caught:
        programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert calls == [(5, ADDRESS)]
    assert caught.value.hint == (
        "cannot verify POM writes on this station; re-run with `--no-verify` "
        "or use `--mode service`"
    )
    # Pins the narrowed `except (...) as exc: ... from exc` (the review
    # finding this test extends): the wrapping `PomReadUnsupportedError` must
    # chain the real failure, not `from None`, so an operator (or a script
    # reading the traceback) can still see what the probing read actually
    # raised.
    assert caught.value.__cause__ is original_error


def test_pom_write_lets_a_track_power_error_from_the_probing_read_propagate_unchanged(
    bench, monkeypatch
):
    """The review finding this pins: the probing read's `except` clause used
    to be `except RailctlError`, wide enough to swallow a `TrackPowerError`
    from an unpowered main track (a broken instrument, not a station
    capability) and misreport it as "this station cannot verify POM writes" -
    exactly the failure mode this project exists to catch, just one layer
    deeper. Narrowed to the three shapes that genuinely mean "the probing
    read did not work" - `DecoderNotRespondingError`, `DecoderNoAckError`,
    `PomReadUnsupportedError`. `TrackPowerError` is not one of them and must
    reach the caller as itself. Reverting the narrowing back to
    `except RailctlError` makes this fail: `pom_write` would raise
    `PomReadUnsupportedError` instead.
    """
    programmer = bench.station.programmer

    def fake_pom_read(cv, *, address, page=None):
        raise TrackPowerError("POM needs the main track powered; run `railctl power on`")

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(TrackPowerError):
        programmer.pom_write(5, 10, address=ADDRESS, verify=True)


def test_pom_write_lets_an_index_page_required_error_from_the_probing_read_propagate_unchanged(
    bench, monkeypatch
):
    """Same reasoning as the `TrackPowerError` case above, for the other real
    fault the probing read can hit: an indexed CV whose page could not be
    selected is a bug report of its own, not evidence against POM
    verification.
    """
    programmer = bench.station.programmer

    def fake_pom_read(cv, *, address, page=None):
        raise IndexPageRequiredError(f"CV{cv} needs a page", cv=cv)

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(IndexPageRequiredError):
        programmer.pom_write(265, 10, address=ADDRESS, verify=True)


def test_pom_write_to_cv29_that_flips_bit_5_is_treated_as_blind(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    old_value = 0b00000010
    new_value = old_value | (1 << CV29_LONG_ADDRESS_BIT)
    monkeypatch.setattr(
        programmer,
        "pom_read",
        lambda cv, *, address, page=None: make_cv_result(cv=cv, value=old_value),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 29, new_value, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(29, new_value, address=ADDRESS, verify=True)
    assert result.verified is False
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert set(payload) == {"cv", "value", "reason"}


def test_pom_write_to_cv29_that_does_not_flip_bit_5_is_verified_normally(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    old_value = 0b00100010  # bit 5 (0x20) set
    new_value = 0b00101010  # bit 5 still set - only an unrelated bit changed
    monkeypatch.setattr(
        programmer,
        "pom_read",
        lambda cv, *, address, page=None: make_cv_result(cv=cv, value=old_value),
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=new_value, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 29, new_value, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(29, new_value, address=ADDRESS, verify=True)
    assert result.verified is True


def test_pom_write_reread_once_on_mismatch_then_succeeds(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    reads = [make_cv_result(cv=5, value=99), make_cv_result(cv=5, value=10)]
    seen_at: list[float] = []

    def fake_cv_read(cv, *, address, mode, page=None):
        seen_at.append(bench.station.now())
        return reads.pop(0)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)
    result = programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert result.verified is True
    assert len(seen_at) == 2
    assert seen_at[1] - seen_at[0] == pytest.approx(TIMING.pom_write_settle)


def test_pom_write_raises_cv_verify_error_after_second_mismatch(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=99),
    )
    with pytest.raises(CvVerifyError) as caught:
        programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert caught.value.cv == 5


def test_pom_write_verified_is_false_while_a_read_stays_none(bench):
    """CV1 is in BLIND_WRITE_CVS: no verify read at all, so `verified` is `False`
    even though nothing ever read `None` back. Goes red if `pom_write` stops
    treating CV1 as blind - the scripted reply list has no second exchange for a
    verify read, so `bench` raises `AssertionError: the script is exhausted`
    instead of the assertion below even firing.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 1, 10, threshold=THRESHOLD), reply=ACK)
    write_result = programmer.pom_write(1, 10, address=ADDRESS, verify=True)
    assert write_result.verified is False


def test_pom_write_with_verify_false_is_blind(bench):
    programmer = bench.station.programmer
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(5, 10, address=ADDRESS, verify=False)
    assert result.verified is False
    assert bench.events[-1][0] == "cv.write_unverified"


@pytest.mark.parametrize("cv", [*sorted(ADDRESS_CVS), 8])
def test_pom_write_to_cv8_or_an_address_cv_invalidates_the_cache(cv, bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    invalidated = watch_invalidations(bench.station)
    monkeypatch.setattr(
        programmer, "pom_read", lambda c, *, address, page=None: make_cv_result(cv=c, value=0)
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda c, *, address, mode, page=None: make_cv_result(cv=c, value=0, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, cv, 0, threshold=THRESHOLD), reply=ACK)
    programmer.pom_write(cv, 0, address=ADDRESS, verify=True)
    assert len(invalidated) == 1


def test_pom_write_to_an_unrelated_cv_does_not_invalidate_the_cache(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    invalidated = watch_invalidations(bench.station)
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=10, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert invalidated == []


def test_pom_write_raises_unsupported_command_error_on_61_82(bench):
    programmer = bench.station.programmer
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        programmer.pom_write(5, 10, address=ADDRESS, verify=False)


# -- service_write_telegram: the write ladder mirrors the read ladder --------


def test_service_write_telegram_prefers_z21_when_available(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(z21_cv_opcodes=True, service_direct_cv=True)
    )
    telegram, encoding, page = bench.station.programmer.service_write_telegram(8, 145)
    assert telegram == cmd_z21_cv_write(8, 145)
    assert encoding is CvEncoding.Z21_16BIT
    assert page == 0


def test_service_write_telegram_falls_back_to_direct_for_low_cvs(bench_factory):
    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    telegram, encoding, page = bench.station.programmer.service_write_telegram(8, 145)
    assert telegram == cmd_service_direct_write(8, 145)
    assert encoding is CvEncoding.SERVICE_DIRECT
    assert page == 0


def test_service_write_telegram_falls_back_to_extended_for_high_cvs(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=False, service_direct_cv=False, service_ext_cv=True
        )
    )
    telegram, encoding, page = bench.station.programmer.service_write_telegram(265, 7)
    assert telegram == cmd_service_ext_write(265, 7)
    assert encoding is CvEncoding.SERVICE_EXT
    assert page == 1


def test_service_write_telegram_raises_cv_out_of_range_when_nothing_is_available(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=False, service_direct_cv=False, service_ext_cv=False
        )
    )
    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_write_telegram(8, 1)
    assert caught.value.cv == 8


def test_service_write_telegram_never_uses_an_unprobed_capability(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=None, service_direct_cv=None, service_ext_cv=True
        )
    )
    _telegram, encoding, _page = bench.station.programmer.service_write_telegram(8, 1)
    assert encoding is CvEncoding.SERVICE_EXT


STATUS_POWERED = encode(0x62, 0x22, 0x00)  # HDR_STATUS, DB_STATUS, track_power bit clear -> True


# -- service_write ------------------------------------------------------------


def test_service_write_succeeds_when_the_wait_loop_reports_ready(bench_factory, monkeypatch):
    from railctl.xbus.replies import Ready

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: Ready())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    result = programmer.service_write(8, 145, verify=True)
    assert result.verified is True
    assert result.mode is ProgMode.SERVICE


def test_service_write_raises_decoder_no_ack_when_the_wait_loop_reports_no_ack(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import NoAck

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: NoAck())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNoAckError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_calls_exit_service_mode_and_invalidates_cache_on_failure(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import NoAck

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    invalidated = watch_invalidations(bench.station)
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    exit_calls: list[tuple[bool, bool]] = []
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: NoAck())
    monkeypatch.setattr(
        programmer,
        "exit_service_mode",
        lambda *, restore_power, restore_hold: exit_calls.append((restore_power, restore_hold)),
    )
    with pytest.raises(DecoderNoAckError):
        programmer.service_write(8, 145, verify=True)
    # STATUS_POWERED carries no emergency-stop bit, so this write found the layout
    # live and free: nothing to restore, and re-asserting a hold nobody had would
    # stop a layout the operator was running.
    assert exit_calls == [(True, False)]
    assert len(invalidated) == 1


def test_service_write_with_verify_false_is_blind(bench_factory, monkeypatch):
    from railctl.xbus.replies import Ready

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: Ready())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    result = programmer.service_write(8, 145, verify=False)
    assert result.verified is False
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert set(payload) == {"cv", "value", "reason"}


def test_service_write_raises_decoder_not_responding_when_the_wait_loop_times_out_silently(
    bench_factory, monkeypatch
):
    """The review finding this pins: `_write_and_confirm`'s SERVICE branch used
    to raise only on `NoAck`/`ShortCircuit` and fall through to `return
    encoding` for everything else, so 95 s of complete silence (`TimedOut`,
    exactly what `await_result` returns when the deadline passes with no
    reply) came back as `verified=True` - a write the station never answered,
    reported as confirmed. `saw_no_ack=False` is what makes this silence
    rather than a negative answer, so the exception must be
    `DecoderNotRespondingError`, not `DecoderNoAckError`.
    """
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "await_result",
        lambda *a, **k: TimedOut(polls=190, ready_streak=0, saw_no_ack=False),
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNotRespondingError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_raises_decoder_no_ack_when_the_wait_loop_times_out_after_seeing_no_ack(
    bench_factory, monkeypatch
):
    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "await_result",
        lambda *a, **k: TimedOut(polls=190, ready_streak=0, saw_no_ack=True),
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNoAckError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_raises_short_circuit_error_on_a_track_short_circuit(
    bench_factory, monkeypatch
):
    """The same fall-through the review finding caught for `TimedOut` also
    swallowed `TrackShortCircuit`: the ladder checked `isinstance(outcome,
    ShortCircuit)` only, and `TrackShortCircuit` is a sibling class, not a
    subclass, so a short circuit on the main track was reported as a
    confirmed write too.
    """
    from railctl.xbus.replies import TrackShortCircuit

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: TrackShortCircuit())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(ShortCircuitError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_raises_station_busy_error_when_the_wait_loop_reports_busy(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import StationBusy

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: StationBusy())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(StationBusyError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_treats_a_matching_cv_value_echo_as_confirmed_but_not_verified(
    bench_factory, monkeypatch
):
    """The review finding this pins: the exhaustive ladder used to reject the
    one reply this hardware actually sends after a service-mode write - a
    `63 14` direct-CV result (a `CvValue`) carrying the written value, not
    `61 11` `Ready` (docs/probe-results.md, "Service-mode WRITE works":
    `write 24 12 00 02 24 -> 63 14 03 24`). A matching echo now completes the
    write instead of raising `DecoderNotRespondingError`, but it is the
    station's own result store, not an independent read-back
    (docs/probe-results.md: "it does not by itself prove the decoder
    accepted and retained it"), so `verified` stays `False` even though
    `verify=True` was requested.
    """
    from railctl.xbus.replies import CvValue

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "await_result",
        lambda *a, **k: CvValue(raw_cv=8, value=145, ident=0x14, z21_form=True),
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    result = programmer.service_write(8, 145, verify=True)
    assert result.verified is False
    assert result.value == 145
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert set(payload) == {"cv", "value", "reason"}
    assert "echo" in payload["reason"]


def test_service_write_raises_cv_verify_error_when_the_echoed_cv_value_does_not_match(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import CvValue

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(
        programmer,
        "await_result",
        lambda *a, **k: CvValue(raw_cv=8, value=99, ident=0x14, z21_form=True),
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(CvVerifyError) as caught:
        programmer.service_write(8, 145, verify=True)
    assert caught.value.cv == 8


def test_service_write_raises_decoder_not_responding_when_paged_cv_value_collides_with_a_register(
    bench_factory, monkeypatch
):
    """Mirrors `_finish_service_read`'s own register-collision handling
    (registers 1-8 are indistinguishable from CV1-8), decided deliberately
    for the write ladder rather than left to the generic catch-all.
    """
    from railctl.xbus.replies import PagedCvValue

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(
        programmer, "await_result", lambda *a, **k: PagedCvValue(raw_register=8, value=145)
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNotRespondingError, match="register mode"):
        programmer.service_write(8, 145, verify=True)
    assert bench.station.capabilities.service_direct_cv is False


def test_service_write_raises_decoder_not_responding_when_paged_cv_value_does_not_match_the_cv(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import PagedCvValue

    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_service_direct_write(9, 200), reply=ACK)
    monkeypatch.setattr(
        programmer, "await_result", lambda *a, **k: PagedCvValue(raw_register=250, value=200)
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNotRespondingError, match="does not correspond"):
        programmer.service_write(9, 200, verify=True)


def test_service_write_treats_a_matching_paged_cv_value_echo_as_confirmed_but_not_verified(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import PagedCvValue

    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_service_direct_write(9, 200), reply=ACK)
    monkeypatch.setattr(
        programmer, "await_result", lambda *a, **k: PagedCvValue(raw_register=9, value=200)
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    result = programmer.service_write(9, 200, verify=True)
    assert result.verified is False
    assert result.value == 200


def test_service_write_raises_cv_verify_error_when_the_paged_echoed_value_does_not_match(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import PagedCvValue

    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_service_direct_write(9, 200), reply=ACK)
    monkeypatch.setattr(
        programmer, "await_result", lambda *a, **k: PagedCvValue(raw_register=9, value=1)
    )
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(CvVerifyError) as caught:
        programmer.service_write(9, 200, verify=True)
    assert caught.value.cv == 9


def test_service_write_raises_decoder_not_responding_on_an_unrecognised_outcome(
    bench_factory, monkeypatch
):
    """The final catch-all, kept exhaustive on purpose: success is `Ready`,
    or a matching `CvValue`/`PagedCvValue` echo, and nothing else - so
    anything `await_result` could hand back that is none of those still has
    to end in a raised exception, never a silent `verified=True`. `Other` is
    what `parse()` returns for a well-formed reply this module does not
    recognise at all - the one shape genuinely left over once `CvValue` and
    `PagedCvValue` each got their own branch.
    """
    from railctl.xbus.replies import Other

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: Other(b"\x00"))
    monkeypatch.setattr(programmer, "exit_service_mode", lambda **_: None)
    with pytest.raises(DecoderNotRespondingError, match="unexpected reply"):
        programmer.service_write(8, 145, verify=True)


def test_service_write_confirms_a_real_63_14_reply_from_the_unstubbed_wait_loop(bench_factory):
    """The review finding this pins: no test drove `service_write` through
    the real, unstubbed `await_result`, so nothing caught that the exhaustive
    ladder rejected the one reply this hardware actually sends after a
    service-mode write. This scripts the exact bytes measured on the bench
    (docs/probe-results.md, "Service-mode WRITE works": `write 24 12 00 02 24
    -> 63 14 03 24`, CV3 to 36) and lets the wait loop poll for real -
    nothing here is stubbed except the wire replies.
    """
    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_service_direct_write(3, 36), reply=ACK)
    bench.expect(cmd_service_result_request(), reply=encode(0x63, 0x14, 3, 36))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    result = programmer.service_write(3, 36, verify=True)

    assert result.value == 36
    assert result.encoding is CvEncoding.SERVICE_DIRECT
    assert result.verified is False
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert payload["cv"] == 3


# -- cv_write: mode dispatch ------------------------------------------------------


def test_cv_write_dispatches_to_pom_when_capabilities_allow(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    calls: list[tuple[int, int, int, bool]] = []

    def fake_pom_write(cv, value, *, address, verify, page=None):
        calls.append((cv, value, address, verify))
        return make_cv_result(cv=cv, value=value, operation="write", verified=verify)

    monkeypatch.setattr(programmer, "pom_write", fake_pom_write)
    result = programmer.cv_write(5, 10, address=ADDRESS)
    assert calls == [(5, 10, ADDRESS, True)]
    assert result.value == 10


def test_cv_write_dispatches_to_service_when_pom_is_unavailable(bench_factory, monkeypatch):
    bench = bench_factory(capabilities=make_capabilities(pom_read=False, service_direct_cv=True))
    programmer = bench.station.programmer
    calls: list[tuple[int, int, bool]] = []

    def fake_service_write(cv, value, *, verify, page=None):
        calls.append((cv, value, verify))
        return make_cv_result(
            cv=cv, value=value, mode=ProgMode.SERVICE, operation="write", verified=verify
        )

    monkeypatch.setattr(programmer, "service_write", fake_service_write)
    programmer.cv_write(5, 10)
    assert calls == [(5, 10, True)]


def test_cv_write_requires_an_address_for_pom(bench):
    """`match` pins this to `cv_write`'s own guard specifically. Without it,
    deleting `cv_write`'s `if address is None: raise ValueError(...)` still
    passes this test: `_write_and_confirm` (programming.py) has its own,
    differently-worded `ValueError` for a POM write with no address ("POM CV
    write needs a locomotive address"), so a bare `pytest.raises(ValueError)`
    cannot tell which guard actually fired.
    """
    bench.station.learn(pom_read=True)
    with pytest.raises(ValueError, match="pass --address"):
        bench.station.programmer.cv_write(5, 10, address=None)


def test_cv_write_ensures_the_page_for_an_indexed_cv(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    ensure_calls: list[tuple[int | None, ProgMode, int, tuple[int, int] | None]] = []
    monkeypatch.setattr(
        programmer,
        "ensure_page",
        lambda address, mode, cv, page: ensure_calls.append((address, mode, cv, page)),
    )
    monkeypatch.setattr(
        programmer,
        "pom_write",
        lambda cv, value, *, address, verify, page=None: make_cv_result(
            cv=cv, value=value, operation="write", verified=verify
        ),
    )
    programmer.cv_write(265, 7, address=ADDRESS, page=(10, 2))
    assert ensure_calls == [(ADDRESS, ProgMode.POM, 265, (10, 2))]


def test_cv_write_verifies_an_indexed_pom_write_end_to_end(bench):
    """The review finding this pins: `pom_write` had no `page` parameter, so
    its own verify read - `self.cv_read(cv, address=address, mode=ProgMode.POM)`
    - ran with `page=None` and tripped `IndexPageRequiredError` on CV265, an
    indexed CV, even though `cv_write`'s own `ensure_page` call had already
    selected and cached the very page this read needed. The write reached the
    wire and then the operator was told to pass `--page`, which they already
    had. Runs `pom_write`, `cv_read` and `pom_read` for real - nothing here
    is stubbed except the wire replies - so a regression that drops `page`
    from any of those calls raises `IndexPageRequiredError` again instead of
    reaching the assertion below.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True, pom_echo_zero_based=True)
    # select_page: write CV31/CV32, then (pom_read is known True) read them
    # back to confirm the page selection stuck.
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(
        cmd_pom_read_byte(ADDRESS, 31, threshold=THRESHOLD), reply=encode(0x63, 0x14, 30, 10)
    )
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(
        cmd_pom_read_byte(ADDRESS, 32, threshold=THRESHOLD), reply=encode(0x63, 0x14, 31, 2)
    )
    # the write itself
    bench.expect(cmd_pom_write_byte(ADDRESS, 265, 7, threshold=THRESHOLD), reply=ACK)
    # the verify read of CV265 - the exchange that used to raise
    # IndexPageRequiredError before `page` reached it.
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(
        cmd_pom_read_byte(ADDRESS, 265, threshold=THRESHOLD), reply=encode(0x63, 0x15, 8, 7)
    )

    result = programmer.cv_write(265, 7, address=ADDRESS, page=(10, 2), verify=True)

    assert result.verified is True


def test_cv_write_invalidates_the_page_cache_on_any_failure(bench):
    """`pom_write` already calls `station.invalidate_caches()` on failure, which
    clears the page cache indirectly (Task 4 registered `invalidate_pages` as one
    of its callbacks) - this pins the direct wrap on `cv_write` itself, which
    stays correct even if that indirect path ever changes. Goes red if
    `cv_write`'s own `try/except` is removed, since nothing else in this
    specific scenario would clear `_pages`.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    programmer._pages[PageKey(address=ADDRESS, mode=ProgMode.POM)] = ((0, 0), 0.0)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        programmer.cv_write(5, 10, address=ADDRESS)
    assert programmer._pages == {}


def test_cv_read_invalidates_the_page_cache_on_any_failing_read(bench, monkeypatch):
    programmer = bench.station.programmer
    programmer._pages[PageKey(address=ADDRESS, mode=ProgMode.POM)] = ((0, 0), 0.0)

    def fake_pom_read(cv, *, address, page=None):
        raise DecoderNotRespondingError("no ack", cv=cv)

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(DecoderNotRespondingError):
        programmer.cv_read(8, address=ADDRESS, mode=ProgMode.POM)
    assert programmer._pages == {}


# -- cv_read_many -----------------------------------------------------------------


def test_cv_read_many_rejects_page_selector_cvs_in_the_payload(bench):
    with pytest.raises(ValueError, match="cursor"):
        bench.station.programmer.cv_read_many([CvSpec(cv=31)], address=ADDRESS, mode=ProgMode.POM)


def test_cv_read_many_selects_each_page_once_and_reads_in_sorted_order(bench, monkeypatch):
    programmer = bench.station.programmer
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        programmer,
        "select_page",
        lambda page, *, address, mode, force: calls.append(("select", page, force)),
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: (
            calls.append(("read", cv)),
            make_cv_result(cv=cv, value=cv, mode=mode),
        )[1],
    )
    specs = [
        CvSpec(cv=269, page=(10, 2)),
        CvSpec(cv=266, page=(10, 2)),
        CvSpec(cv=8),
        CvSpec(cv=265, page=(10, 2)),
        CvSpec(cv=267, page=(10, 2)),
        CvSpec(cv=268, page=(10, 2)),
    ]
    outcomes = programmer.cv_read_many(specs, address=ADDRESS, mode=ProgMode.POM)
    assert [o.spec.cv for o in outcomes] == [8, 265, 266, 267, 268, 269]
    assert calls == [
        ("read", 8),
        ("select", (10, 2), True),
        ("read", 265),
        ("read", 266),
        ("read", 267),
        ("read", 268),
        ("read", 269),
    ]


def test_cv_read_many_calls_on_progress_once_per_spec_and_captures_failures(bench, monkeypatch):
    programmer = bench.station.programmer

    def fake_cv_read(cv, *, address, mode, page=None):
        if cv == 6:
            raise DecoderNotRespondingError("no ack", cv=cv)
        return make_cv_result(cv=cv, value=cv, mode=mode)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)
    progress: list[tuple[int, int, CvReadOutcome]] = []
    specs = [CvSpec(cv=5), CvSpec(cv=6), CvSpec(cv=7)]
    outcomes = programmer.cv_read_many(
        specs, address=ADDRESS, mode=ProgMode.POM, on_progress=progress.append
    )
    assert [o.error is None for o in outcomes] == [True, False, True]
    assert isinstance(outcomes[1].error, DecoderNotRespondingError)
    assert [(index, total) for index, total, _outcome in progress] == [(0, 3), (1, 3), (2, 3)]


# -- facade delegation --------------------------------------------------------------


class _ProgrammerStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def cv_write(self, cv, value, *, address, mode, page, verify):
        self.calls.append(("cv_write", cv, value, address, mode, page, verify))
        return make_cv_result(cv=cv, value=value, operation="write", verified=verify)

    def cv_read_many(self, specs, *, address, mode, on_progress):
        self.calls.append(("cv_read_many", tuple(specs), address, mode, on_progress))
        return []

    def select_page(self, page, *, address, mode, force):
        self.calls.append(("select_page", page, address, mode, force))


def test_facade_cv_write_substitutes_the_default_address_and_delegates(bench, monkeypatch):
    """`bench_factory`'s default `default_address` is `ADDRESS` (3); substituting
    the whole `programmer` collaborator on a real, already-open `bench.station`
    is how Task 2's own rule ("monkeypatch the single method on
    `bench.station.programmer`") extends to a test whose whole point is the
    facade method's own delegation, not any one `CvProgrammer` method - there is
    no smaller surface to patch here.
    """
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.cv_write(5, 10)
    assert stub.calls == [("cv_write", 5, 10, ADDRESS, ProgMode.AUTO, None, True)]


def test_facade_cv_write_keeps_an_explicit_address(bench, monkeypatch):
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.cv_write(5, 10, address=99)
    assert stub.calls == [("cv_write", 5, 10, 99, ProgMode.AUTO, None, True)]


def test_facade_select_page_and_cv_read_many_delegate(bench, monkeypatch):
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.select_page((10, 2), force=True)
    bench.station.cv_read_many([CvSpec(cv=8)])
    assert stub.calls == [
        ("select_page", (10, 2), ADDRESS, ProgMode.AUTO, True),
        ("cv_read_many", (CvSpec(cv=8),), ADDRESS, ProgMode.AUTO, None),
    ]


# -- coverage gaps left by Tasks 6/6b, closed here per this task's own gate --


def test_write_and_confirm_requires_an_address_under_pom(bench):
    """`raw_cv_write` (Task 6) is the one caller that can reach
    `_write_and_confirm`'s own POM address guard with no earlier check ahead
    of it - `cv_write` and `pom_write` both validate the address themselves
    before ever calling in. Sends nothing: the guard raises before any wire
    I/O.
    """
    programmer = bench.station.programmer
    with pytest.raises(ValueError, match="locomotive address"):
        programmer.raw_cv_write(31, 10, address=None, mode=ProgMode.POM)


def test_select_page_skips_reverification_when_the_same_page_is_reselected_with_force(
    bench, monkeypatch
):
    """`cv_read_many` selects with `force=True` at the head of every group
    (Task 6c); once a page has been verified once under a key,
    `_verified_pages` is what lets reselecting the SAME page skip paying for
    a second read-back.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    read_calls: list[int] = []

    def fake_cv_read(cv, *, address, mode, page=None):
        read_calls.append(cv)
        return make_cv_result(cv=cv, value={31: 10, 32: 2}[cv], mode=mode)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert read_calls == [31, 32]

    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM, force=True)
    assert read_calls == [31, 32]


def test_cv_read_many_deselects_the_current_page_when_a_later_read_carries_none(bench, monkeypatch):
    """The sort key `spec.page or (0, 0)` puts every unpaged CV ahead of a
    positive page, so a real batch never walks backwards from a page to
    `None`. A page equal to `(0, 0)` is the one payload that still can:
    both it and an unpaged spec share the sort key, so cv order alone can
    put the unpaged one second - this is what exercises `current_page`
    actually being reset to `None` rather than merely being set once.
    """
    programmer = bench.station.programmer
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        programmer,
        "select_page",
        lambda page, *, address, mode, force: calls.append(("select", page, force)),
    )
    monkeypatch.setattr(
        programmer,
        "cv_read",
        lambda cv, *, address, mode, page=None: (
            calls.append(("read", cv)),
            make_cv_result(cv=cv, value=cv, mode=mode),
        )[1],
    )
    specs = [
        CvSpec(cv=5, page=(0, 0)),
        CvSpec(cv=9, page=None),
        CvSpec(cv=300, page=(0, 0)),
    ]
    outcomes = programmer.cv_read_many(specs, address=ADDRESS, mode=ProgMode.POM)
    assert [o.spec.cv for o in outcomes] == [5, 9, 300]
    assert calls == [
        ("select", (0, 0), True),
        ("read", 5),
        ("read", 9),
        ("select", (0, 0), True),
        ("read", 300),
    ]


def test_pom_write_to_cv29_lets_a_decoder_not_responding_error_propagate_when_pom_read_is_true(
    bench, monkeypatch
):
    """`pom_write`'s own docstring: "A known-working capability failing once
    is a real fault ..., not grounds to claim POM verification is
    unsupported." CV29 always re-probes the pre-write value even when
    `pom_read` is already `True` (to detect a bit-5 flip) - this is the one
    case where that probe can fail with `pom_read` already established, and
    the failure must reach the caller unchanged, not get reworded as
    `PomReadUnsupportedError`.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)

    def fake_pom_read(cv, *, address, page=None):
        raise DecoderNotRespondingError("no ack", cv=cv)

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(DecoderNotRespondingError):
        programmer.pom_write(29, 6, address=ADDRESS, verify=True)
