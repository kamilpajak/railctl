# tests/cli/test_doctor.py
"""Pins the `doctor` command's rendering contract and, in the first test, the
`report_for` contract that a future `cv read --mode pom` (Plan 4) depends on: a station
that answered nothing at all must never read as "not supported". The contrasting case -
a `pom_read=false` conclusion naming where it came from - is Plan 4's own test (a station
that answers "no" must say so from a message *built* in the station layer, not from a
message this test writes for itself and then asserts against).
"""

from __future__ import annotations

from railctl.cli._errors import report_for
from railctl.errors import DecoderNotRespondingError


def test_decoder_not_responding_never_says_unsupported():
    """R1 (docs/probe-results.md): the station ACKs a POM read and returns nothing at
    all - no `61 13`, no `61 82`, no value. That is UNKNOWN, never a negative answer,
    and this is the end-to-end assertion Plan 4's `cv read --mode pom` will rely on.
    """
    exc = DecoderNotRespondingError(
        "CV8 produced no result over POM after 3 attempts "
        "(interface ack only; docs/probe-results.md, R1)",
        cv=8,
    )
    report = report_for(exc, command="cv read")
    assert report.code == "decoder_not_responding"
    assert report.exit_code == 13
    assert "unsupported" not in report.message.lower()
    assert "not supported" not in report.message.lower()
    assert report.suggestions[0] == ["railctl", "doctor"]
