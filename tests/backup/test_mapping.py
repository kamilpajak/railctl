# tests/backup/test_mapping.py
"""The design's C5 table, one row per outcome kind.

`status_for` matches on exception types only, so each parametrized case is
one class the station can actually put in a `CvReadOutcome` - and the
whole-tree test at the bottom proves every station error lands in exactly
one of the three non-`ok` buckets rather than falling through unmapped.
"""

from __future__ import annotations

import pytest

from railctl.backup import (
    NOT_ATTEMPTED_DETAIL,
    SOURCE_CATALOG,
    ReadStatus,
    record_for,
    status_for,
)
from railctl.errors import (
    CvOutOfRangeError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    LinkTimeout,
    PomReadUnsupportedError,
    RailctlError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
    StationBusyError,
    UnsupportedCommandError,
    XBusDecodeError,
)
from railctl.station import CvEncoding, CvReadOutcome, CvResult, CvSpec, ProgMode

CV = 8
VALUE = 145
ELAPSED_S = 1.7


def _ok_outcome() -> CvReadOutcome:
    spec = CvSpec(cv=CV, name="manufacturer_id")
    result = CvResult(
        cv=CV,
        value=VALUE,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="read",
        verified=None,
        elapsed=ELAPSED_S,
    )
    return CvReadOutcome(spec=spec, result=result, error=None)


def _failed_outcome(error: RailctlError) -> CvReadOutcome:
    return CvReadOutcome(spec=CvSpec(cv=CV, name="manufacturer_id"), result=None, error=error)


def test_a_value_returned_is_ok_with_no_detail():
    assert status_for(_ok_outcome()) == (ReadStatus.OK, None)


def test_never_attempted_is_skipped_with_the_fixed_detail():
    # `CvReadOutcome`'s contract: result and error both None is "not
    # attempted", e.g. the rest of a batch after an earlier abort.
    outcome = CvReadOutcome(spec=CvSpec(cv=CV), result=None, error=None)
    assert status_for(outcome) == (ReadStatus.SKIPPED, NOT_ATTEMPTED_DETAIL)


@pytest.mark.parametrize(
    "error",
    [
        DecoderNoAckError("no acknowledgement from the decoder"),
        DecoderNotRespondingError("no answer after 3 attempts (pom)"),
    ],
)
def test_silence_in_both_flavours_is_no_response_with_the_stations_text(error: RailctlError):
    assert status_for(_failed_outcome(error)) == (ReadStatus.NO_RESPONSE, str(error))


@pytest.mark.parametrize(
    "error",
    [
        CvOutOfRangeError("direct opcodes only cover CV1..255"),
        IndexPageRequiredError("CV397 lives behind an index page"),
    ],
)
def test_a_cv_the_mode_cannot_reach_is_skipped_with_the_stations_text(error: RailctlError):
    assert status_for(_failed_outcome(error)) == (ReadStatus.SKIPPED, str(error))


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedCommandError("the station answered: it understood, and it refuses"),
        ShortCircuitError("short on the programming track"),
        LinkTimeout("no reply within 5.0 s"),
        StationBusyError("still busy after the station-side retries"),
        XBusDecodeError("garbled reply"),
        # The instrument failing to measure mid-run, not a recorded decision:
        # a live 61 82 refusal, or a service read with no proven encoding.
        # `skipped` here would keep `complete` true and let a refused run
        # exit 0 - backup's exit code rides on `complete`.
        PomReadUnsupportedError("pom reads are recorded unavailable here"),
        ServiceEncodingUnknownError("no service-mode encoding established yet"),
    ],
)
def test_a_refusal_a_short_a_timeout_or_a_garble_is_error_with_the_stations_text(
    error: RailctlError,
):
    assert status_for(_failed_outcome(error)) == (ReadStatus.ERROR, str(error))


def test_every_station_error_lands_in_exactly_one_bucket():
    """No error class may fall through to a status nobody decided for it in
    the sense of being unmappable: `status_for` must return one of the three
    non-`ok` statuses for ANY `RailctlError`, including one added later, and
    the fallback is `error` - the loud bucket, never a silent skip."""

    def tree(root: type[RailctlError]) -> set[type[RailctlError]]:
        found = {root}
        for sub in root.__subclasses__():
            found |= tree(sub)
        return found

    for klass in tree(RailctlError):
        status, detail = status_for(_failed_outcome(klass.__new__(klass)))
        assert status in {ReadStatus.NO_RESPONSE, ReadStatus.SKIPPED, ReadStatus.ERROR}, klass
        assert detail is not None, klass


def test_record_for_an_ok_outcome_carries_the_value_and_the_specs_name():
    record = record_for(_ok_outcome())
    assert record.cv == CV
    assert record.name == "manufacturer_id"
    assert record.status is ReadStatus.OK
    assert record.value == VALUE
    assert record.detail is None
    assert record.attempts is None
    assert record.source == SOURCE_CATALOG


def test_record_for_a_silent_outcome_has_no_value_and_keeps_the_stations_text():
    error = DecoderNotRespondingError("no answer after 3 attempts (pom)")
    record = record_for(_failed_outcome(error))
    assert record.status is ReadStatus.NO_RESPONSE
    assert record.value is None
    assert record.detail == str(error)


def test_record_for_carries_the_attempts_the_error_recorded():
    # `station/programming.py` puts `details={"attempts": ...}` on its raise
    # sites; the record carries it for the ndjson `cv` line, never the file.
    error = DecoderNotRespondingError("no answer after 3 attempts (pom)", details={"attempts": 3})
    assert record_for(_failed_outcome(error)).attempts == 3


def test_record_for_an_error_without_the_attempts_detail_leaves_it_none():
    error = DecoderNoAckError("no acknowledgement from the decoder")
    assert record_for(_failed_outcome(error)).attempts is None


def test_record_for_accepts_an_explicit_source():
    record = record_for(_ok_outcome(), source="sweep")
    assert record.source == "sweep"
