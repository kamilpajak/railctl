"""The CV/result dataclasses, the doctor report shape, the CV addressing
constants, and the injectable `Timing` table.

`decoder_family` and `treats_cv144_as_lock` are tested side by side on
purpose: both read the same `None` (decoder type never read) and answer
differently, and that disagreement is the point, not a bug - see the module
docstring in `railctl.station.types`.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.errors import CvOutOfRangeError
from railctl.station.capabilities import Capabilities
from railctl.station.timing import TIMING, Timing
from railctl.station.types import (
    ADDRESS_CVS,
    BLIND_WRITE_CVS,
    CV29_LONG_ADDRESS_BIT,
    CV144,
    DECODER_TYPE_CV,
    EVENT_NAMES,
    INDEXED_CV_RANGE,
    MS_DECODER_TYPES,
    PAGE_SELECTOR_CVS,
    Check,
    CvReadOutcome,
    CvResult,
    CvSpec,
    DoctorReport,
    ProgMode,
    StationEvent,
    decoder_family,
    treats_cv144_as_lock,
)
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import LocoInfo, StationStatus, StationVersion
from railctl.xbus.speed import Direction

ALL_DATACLASSES = (
    CvSpec,
    CvResult,
    CvReadOutcome,
    StationEvent,
    Check,
    DoctorReport,
    Timing,
    Capabilities,
)


def _check(check_id: str, status: str) -> Check:
    return Check(id=check_id, title=check_id, status=status, detail="")


def test_prog_mode_has_exactly_the_three_documented_members():
    assert {mode.value for mode in ProgMode} == {"auto", "pom", "service"}
    assert ProgMode.AUTO.value == "auto"


def test_cv_spec_defaults_to_no_name_and_no_page():
    spec = CvSpec(cv=8)
    assert spec.name == ""
    assert spec.page is None


def test_cv_spec_can_carry_a_name_and_an_index_page():
    spec = CvSpec(cv=257, name="index page CV", page=(31, 2))
    assert spec.name == "index page CV"
    assert spec.page == (31, 2)


def test_cv_result_carries_the_resolved_mode_never_auto():
    result = CvResult(
        cv=8,
        value=3,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="read",
        verified=None,
        elapsed=1.7,
    )
    assert result.mode is ProgMode.SERVICE
    assert result.operation == "read"
    assert result.verified is None


def test_cv_read_outcome_can_carry_a_result():
    result = CvResult(
        cv=8,
        value=3,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="read",
        verified=None,
        elapsed=1.7,
    )
    outcome = CvReadOutcome(spec=CvSpec(cv=8), result=result, error=None)
    assert outcome.result is result
    assert outcome.error is None


def test_cv_read_outcome_can_carry_an_error():
    error = CvOutOfRangeError("cv 2000 out of range", cv=2000)
    outcome = CvReadOutcome(spec=CvSpec(cv=2000), result=None, error=error)
    assert outcome.result is None
    assert outcome.error is error


def test_cv_read_outcome_allows_neither_result_nor_error_as_a_distinct_state():
    """The spec never says a read must produce one or the other. A batch
    that stops after CV1 fails leaves CV17's outcome with both fields None -
    not attempted, not resolved, not failed. A consumer that branches on
    `error is not None` would read that as success; the contract this
    dataclass pins is that the caller must branch on `result is None`
    instead.
    """
    outcome = CvReadOutcome(spec=CvSpec(cv=17), result=None, error=None)
    assert outcome.result is None
    assert outcome.error is None


def test_station_event_carries_a_free_form_payload():
    event = StationEvent(
        at=12.5,
        name="cv.stale_result",
        detail="CV8 read after retry",
        payload={"cv": 8, "attempt": 2},
    )
    assert event.name in EVENT_NAMES
    assert event.payload == {"cv": 8, "attempt": 2}


def test_doctor_report_ok_requires_d0_through_d2_ok_and_d3_not_fail():
    report = DoctorReport(
        checks=(
            _check("D0", "ok"),
            _check("D1", "ok"),
            _check("D2", "ok"),
            _check("D3", "unknown"),
        ),
        capabilities=Capabilities.unknown("test"),
    )
    assert report.ok is True

    d3_fail = dataclasses.replace(
        report,
        checks=(_check("D0", "ok"), _check("D1", "ok"), _check("D2", "ok"), _check("D3", "fail")),
    )
    assert d3_fail.ok is False

    d0_fail = dataclasses.replace(
        report,
        checks=(_check("D0", "fail"), _check("D1", "ok"), _check("D2", "ok"), _check("D3", "skip")),
    )
    assert d0_fail.ok is False

    missing_d1 = dataclasses.replace(report, checks=(_check("D0", "ok"), _check("D2", "ok")))
    assert missing_d1.ok is False


def test_doctor_report_check_looks_up_by_id_and_returns_none_when_absent():
    report = DoctorReport(checks=(_check("D0", "ok"),), capabilities=Capabilities.unknown("test"))
    assert report.check("D0").status == "ok"
    assert report.check("D9") is None


def test_the_cv_addressing_constants_match_the_design_spec():
    assert ADDRESS_CVS == frozenset({1, 17, 18, 29})
    assert BLIND_WRITE_CVS == frozenset({1, 8, 17, 18})
    assert CV29_LONG_ADDRESS_BIT == 5
    assert PAGE_SELECTOR_CVS == (31, 32)
    assert list(INDEXED_CV_RANGE) == list(range(257, 513))
    assert CV144 == 144
    assert DECODER_TYPE_CV == 250
    assert MS_DECODER_TYPES == frozenset({6, 7, 12})


def test_blind_write_cvs_excludes_cv29():
    """CV29 only changes the answering address when bit 5 changes, and the
    commonest reason to write it is enabling RailCom (bit 3), which must be
    verifiable - so CV29 stays out of the blind-write set even though it is
    an address CV like CV1/17/18.
    """
    assert 29 not in BLIND_WRITE_CVS
    assert 29 in ADDRESS_CVS


def test_event_names_are_exactly_the_twelve_defined_events():
    """Twelve names from this task onward, not five: `cv.unexercised_band` is
    emitted by a later CV-programming task and `function.group_seeded` by a
    later drive/function task; `power.on`, `power.off`, `loco.emergency_stop`,
    `service.entered` and `reply.unknown` are emitted by the facade (Task 2)
    and rendered by `monitor`. A later CLI task pins that every name in this
    tuple has a rendering - so the tuple has to be complete here, before any
    emitter exists, or that later task has nothing to render against.
    """
    assert EVENT_NAMES == (
        "cv.stale_result",
        "cv.write_unverified",
        "cv.unexercised_band",
        "page.unverified",
        "loco.in_use_by_other",
        "address.band_unverified",
        "function.group_seeded",
        "power.on",
        "power.off",
        "loco.emergency_stop",
        "service.entered",
        "reply.unknown",
    )


def test_decoder_family_is_ms_for_every_known_ms_decoder_type():
    for decoder_type in sorted(MS_DECODER_TYPES):
        assert decoder_family(decoder_type) == "ms"


def test_decoder_family_is_other_for_a_non_ms_decoder_type():
    assert decoder_family(5) == "other"  # e.g. an older ZIMO MX-family decoder


def test_decoder_family_is_unknown_when_the_type_was_never_read():
    assert decoder_family(None) == "unknown"


def test_treats_cv144_as_lock_disagrees_with_decoder_family_about_none():
    """decoder_family(None) is "unknown" - a report may not claim a family it
    never measured. treats_cv144_as_lock(None) is False for the opposite
    reason: the restore path must not refuse a safe write just because it
    never read the decoder type. Same None, two different correct answers.
    """
    assert decoder_family(None) == "unknown"
    assert treats_cv144_as_lock(None) is False


def test_treats_cv144_as_lock_is_false_for_the_ms_family():
    assert treats_cv144_as_lock(6) is False  # MS450P22 family: CV144 is a sound setting


def test_treats_cv144_as_lock_is_true_outside_the_ms_family():
    assert treats_cv144_as_lock(5) is True  # e.g. an older MX-family decoder


def test_timing_matches_the_m5_table():
    timing = Timing()
    assert timing.li_ack_normal == 5.0
    assert timing.li_ack_programming == 95.0
    assert timing.min_exchange == 0.05
    assert timing.power_settle == 0.5
    assert timing.pom_result == 2.0
    assert timing.pom_poll_interval == 0.10
    assert timing.pom_read_attempts == 3
    assert timing.pom_retry_delay == 0.25
    assert timing.pom_write_settle == 0.5
    assert timing.service_result == 95.0
    assert timing.service_first_poll_delay == 0.20
    assert timing.service_poll_interval == 0.50
    assert timing.service_ready_limit == 8
    assert timing.service_exit_settle == 0.10
    assert timing.page_cache_ttl == 10.0


def test_timing_singleton_is_the_default_timing():
    assert TIMING == Timing()


def test_li_ack_timings_agree_with_the_link_module_constants():
    """Timing.li_ack_normal and li_ack_programming restate link.DEFAULT_TIMEOUT
    and link.PROGRAMMING_TIMEOUT as data the station can inject a fake clock
    against; importing both modules here is what stops them drifting apart
    if link.py's constants ever change without station/timing.py following.
    """
    from railctl.link import DEFAULT_TIMEOUT, PROGRAMMING_TIMEOUT

    assert Timing().li_ack_normal == DEFAULT_TIMEOUT
    assert Timing().li_ack_programming == PROGRAMMING_TIMEOUT


def test_every_dataclass_in_the_station_data_layer_is_frozen_and_uses_slots():
    for cls in ALL_DATACLASSES:
        params = cls.__dataclass_params__
        assert params.frozen is True, cls.__name__
        assert "__slots__" in cls.__dict__, cls.__name__


def test_a_frozen_dataclass_refuses_mutation():
    spec = CvSpec(cv=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.cv = 2


def test_the_station_package_reexports_the_xbus_types():
    import railctl.station as station

    assert station.Direction is Direction
    assert station.StationVersion is StationVersion
    assert station.StationStatus is StationStatus
    assert station.LocoInfo is LocoInfo
    assert station.CvEncoding is CvEncoding


def test_every_name_in_station_all_is_actually_importable():
    """A canary against a typo in `__all__`: a name listed there that does not
    exist on the module would otherwise only be caught by whoever first tries
    `from railctl.station import <that name>` - a later task, not this one.
    """
    import railctl.station as station

    for name in station.__all__:
        assert hasattr(station, name), name
