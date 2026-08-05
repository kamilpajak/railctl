# tests/station/test_cv_write.py
from __future__ import annotations

import pytest

from railctl.errors import (
    CvVerifyError,
    IndexPageRequiredError,
    TrackPowerError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import CvResult, ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import cmd_pom_read_byte, cmd_pom_write_byte, cmd_station_status
from railctl.xbus.dialect import CvEncoding

ADDRESS = 3
THRESHOLD = 100

ACK = encode(0x01, 0x04)
STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x01)


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
