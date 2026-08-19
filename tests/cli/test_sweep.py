"""`cli/commands/_sweep.py`: the `--all` sweep's arithmetic, with no station in sight.

Every function here is pure, which is the reason the module exists apart from
`backup.py`: the bound, the naming rule and the estimate can be pinned at a
desk, and only the wiring needs a fake station next door in `test_backup.py`.

The bound table is written out row by row rather than parametrised over a
capability dict, because the interesting part is which capability wins when
two are set, and a table that generates its own expectation would agree with
whatever the code does.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from railctl.backup import SOURCE_CATALOG, SOURCE_SWEEP
from railctl.catalog import load_catalog
from railctl.cli.commands._sweep import (
    HIGHEST_EXERCISED_CV,
    SWEEP_CONFIRM_SECONDS,
    SWEEP_ESTIMATE_AFTER,
    SWEEP_PROGRESS_EVERY,
    SWEEP_SECONDS_PER_CV,
    SWEEP_SET_NAME,
    estimate_seconds,
    format_duration,
    sweep_bound,
    sweep_name,
)
from railctl.errors import ServiceEncodingUnknownError
from railctl.station import Capabilities, ProgMode
from railctl.xbus.cv import MAX_CV_DIRECT, MAX_CV_EXT, MAX_CV_Z21

CATALOG = load_catalog()

#: A CV no ZIMO catalog entry names, below 1000 so the zero padding is visible.
UNNAMED_CV = 617

IDENTITY = "serial:7010A0001194:3"


def caps(**fields: bool | None) -> Capabilities:
    """A capability record with everything unprobed but the named fields."""
    return Capabilities(link_identity=IDENTITY, **fields)


# -- sweep_bound: the table, row by row ----------------------------------------


def test_service_with_z21_opcodes_sweeps_to_the_z21_bound():
    assert sweep_bound(ProgMode.SERVICE, caps(z21_cv_opcodes=True)) == MAX_CV_Z21


def test_service_with_extended_opcodes_sweeps_to_the_extended_bound():
    assert sweep_bound(ProgMode.SERVICE, caps(service_ext_cv=True)) == MAX_CV_EXT


def test_service_with_direct_opcodes_only_stops_at_255():
    assert sweep_bound(ProgMode.SERVICE, caps(service_direct_cv=True)) == MAX_CV_DIRECT


def test_service_with_nothing_proven_refuses_rather_than_guessing_a_bound():
    with pytest.raises(ServiceEncodingUnknownError) as caught:
        sweep_bound(ProgMode.SERVICE, caps())
    assert "railctl doctor" in (caught.value.hint or "")


def test_an_unprobed_capability_never_earns_the_wider_bound():
    # The founding rule: `None` is "nobody measured this", not "probably yes".
    # A station whose Z21 opcodes were never probed sweeps to 255 on the
    # direct opcodes it did prove, not to 1024 on the ones it did not.
    bound = sweep_bound(ProgMode.SERVICE, caps(z21_cv_opcodes=None, service_direct_cv=True))
    assert bound == MAX_CV_DIRECT


def test_a_measured_no_falls_through_to_the_next_proven_encoding():
    bound = sweep_bound(
        ProgMode.SERVICE, caps(z21_cv_opcodes=False, service_ext_cv=False, service_direct_cv=True)
    )
    assert bound == MAX_CV_DIRECT


def test_pom_stops_at_255_even_when_the_wide_service_opcodes_are_proven():
    # POM's bound is not about opcodes at all: CV256 and up sit behind the
    # CV31/CV32 index page, and a backup never writes those selectors.
    bound = sweep_bound(ProgMode.POM, caps(z21_cv_opcodes=True, service_ext_cv=True))
    assert bound == MAX_CV_DIRECT


def test_pom_needs_no_service_capability_at_all():
    assert sweep_bound(ProgMode.POM, caps()) == MAX_CV_DIRECT


def test_only_the_z21_bound_reaches_past_what_the_bench_has_exercised():
    # The relation task 2's `sweep.unexercised_range` warning is keyed on.
    assert MAX_CV_DIRECT <= HIGHEST_EXERCISED_CV < MAX_CV_Z21


# -- sweep_name ----------------------------------------------------------------


def test_a_catalog_cv_keeps_its_slug_and_the_catalog_source():
    assert sweep_name(1, CATALOG) == (CATALOG[1].slug, SOURCE_CATALOG)
    assert CATALOG[1].slug == "primary_address"


def test_a_cv_the_catalog_does_not_name_gets_a_zero_padded_number():
    assert UNNAMED_CV not in CATALOG
    assert sweep_name(UNNAMED_CV, CATALOG) == ("cv0617", SOURCE_SWEEP)


def test_the_highest_swept_cv_keeps_its_four_digits_unpadded():
    assert sweep_name(MAX_CV_Z21, CATALOG) == ("cv1024", SOURCE_SWEEP)


def test_the_lowest_cv_is_padded_to_four_digits_when_the_catalog_is_empty():
    assert sweep_name(1, {}) == ("cv0001", SOURCE_SWEEP)


@given(cv=st.integers(min_value=1, max_value=MAX_CV_Z21))
def test_every_swept_cv_gets_a_non_empty_name_and_a_known_source(cv):
    name, source = sweep_name(cv, CATALOG)
    assert name.strip() == name
    assert name != ""
    assert source in {SOURCE_CATALOG, SOURCE_SWEEP}


@given(cv=st.integers(min_value=1, max_value=MAX_CV_Z21))
def test_the_source_says_whether_the_catalog_named_the_cv(cv):
    _name, source = sweep_name(cv, CATALOG)
    assert (source == SOURCE_CATALOG) is (cv in CATALOG)


# -- estimate_seconds ----------------------------------------------------------


def test_the_estimate_is_the_measured_rate_times_the_count():
    assert estimate_seconds(10) == pytest.approx(10 * SWEEP_SECONDS_PER_CV)


def test_the_rate_can_be_replaced_by_what_the_run_observes():
    assert estimate_seconds(10, 3.0) == pytest.approx(30.0)


def test_nothing_to_read_costs_nothing():
    assert estimate_seconds(0) == 0.0


def test_25_cvs_stay_under_the_confirmation_threshold():
    assert estimate_seconds(25) == pytest.approx(60.0)
    assert estimate_seconds(25) <= SWEEP_CONFIRM_SECONDS


def test_26_cvs_pass_the_confirmation_threshold():
    assert estimate_seconds(26) > SWEEP_CONFIRM_SECONDS


def test_a_full_1024_cv_sweep_is_the_better_part_of_an_hour():
    seconds = estimate_seconds(MAX_CV_Z21)
    assert seconds == pytest.approx(2457.6)
    assert format_duration(seconds) == "41 min"


# -- format_duration -----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0 s"),
        (45.0, "45 s"),
        (57.6, "58 s"),
        (59.4, "59 s"),
        (59.6, "1 min"),
        (60.0, "1 min"),
        (62.4, "1 min"),
        (90.0, "2 min"),
        (1920.0, "32 min"),
        (3540.0, "59 min"),
        (3600.0, "1 h"),
        (6120.0, "1 h 42 min"),
        (7200.0, "2 h"),
    ],
)
def test_a_duration_reads_the_way_a_person_would_say_it(seconds, expected):
    assert format_duration(seconds) == expected


def test_a_duration_never_reports_sixty_of_the_smaller_unit():
    # 3599 s rounds to 60 min, which must carry into "1 h" rather than print
    # a minute count nobody writes.
    assert format_duration(3599.0) == "1 h"
    assert format_duration(59.9) == "1 min"


# -- the pinned constants ------------------------------------------------------


def test_the_constants_are_the_measured_and_agreed_values():
    # Pinned rather than derived: each one is a decision recorded in the
    # design or a bench measurement, and a silent edit is what this catches.
    assert SWEEP_SECONDS_PER_CV == 2.4
    assert SWEEP_CONFIRM_SECONDS == 60.0
    assert SWEEP_ESTIMATE_AFTER == 10
    assert SWEEP_PROGRESS_EVERY == 32
    assert SWEEP_SET_NAME == "all"
    assert HIGHEST_EXERCISED_CV == 511
