"""`cv read` and `cv write`: the grammar, the builders, and the wired commands.

The fakes here answer at the `Station` facade surface (`cv_read_many`,
`cv_write`, `status`, `capabilities`) and record what they were asked, so
every test is about the CLI contract - which mode was chosen, what went into
the envelope, which exit code came out - never about wire bytes, which
`tests/station/` owns.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from railctl.catalog import load_catalog
from railctl.cli._meta import (
    CV_READ_MODE_OPT,
    CV_WRITE_TRACK_OPT,
    command_meta,
    group_epilog,
)
from railctl.cli.commands.cv import (
    _CONFIRM_REASONS,
    CONFIRM_CVS,
    FACTORY_RESET_CV,
    MODE_FOR_TRACK,
    PROG_TRACK_NOTICE,
    SILENCE_GUIDANCE,
    TRACK_FOR_MODE,
    _all_failed,
    _confirm_question,
    _with_guidance,
    build_cv_read,
    build_cv_write,
    parse_page,
)
from railctl.cli.cvspec import parse_cv_spec
from railctl.cli.deps import UsageProblem
from railctl.cli.main import app
from railctl.cli.result import PARTIAL_EXIT_CODE
from railctl.errors import (
    CvOutOfRangeError,
    CvVerifyError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    UnsupportedCommandError,
)
from railctl.station import (
    Capabilities,
    CvEncoding,
    CvReadOutcome,
    CvResult,
    CvSpec,
    ProgMode,
    Station,
)
from railctl.xbus.replies import StationStatus, StationVersion

runner = CliRunner()

CATALOG = load_catalog()
PREFIX = ["railctl", "cv", "read"]

#: A CV the catalog does not curate (the catalog jumps from CV10 to CV17), so
#: its `name` must come out empty - never invented.
UNCURATED_CV = 11
#: An indexed CV (257..512), behind the ZIMO CV31/CV32 page.
INDEXED_CV = 260


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


# -- parse_cv_spec -------------------------------------------------------------


def test_parse_cv_spec_concatenates_collapses_and_keeps_first_appearance_order():
    cvs = parse_cv_spec(["29", "3-5", "1,3,29", "accel_rate"], CATALOG, argv_prefix=PREFIX)
    assert cvs == [29, 3, 4, 5, 1]


def test_parse_cv_spec_resolves_a_catalog_slug():
    assert parse_cv_spec(["accel_rate"], CATALOG, argv_prefix=PREFIX) == [3]


def test_parse_cv_spec_accepts_leading_zeroes():
    assert parse_cv_spec(["007"], CATALOG, argv_prefix=PREFIX) == [7]


def test_parse_cv_spec_unknown_slug_names_the_three_closest_and_suggests_them():
    with pytest.raises(UsageProblem) as caught:
        parse_cv_spec(["accel_rte"], CATALOG, argv_prefix=PREFIX)
    problem = caught.value
    assert "accel_rate" in str(problem)
    assert len(problem.suggestions) == 3
    assert problem.suggestions[0] == [*PREFIX, "accel_rate"]  # true near miss ranked first
    assert problem.details["reason"] == "unknown_slug"


def test_parse_cv_spec_unknown_slug_suggestions_keep_the_other_tokens():
    # The suggestion is the FULL invocation with the bad piece corrected in
    # place - dropping the other tokens produced a runnable command that no
    # longer did what the caller asked.
    with pytest.raises(UsageProblem) as caught:
        parse_cv_spec(["3-8", "1,accel_rte"], CATALOG, argv_prefix=PREFIX)
    assert caught.value.suggestions[0] == [*PREFIX, "3-8", "1,accel_rate"]


def test_parse_cv_spec_backwards_range_suggestions_keep_the_other_tokens():
    with pytest.raises(UsageProblem) as caught:
        parse_cv_spec(["29", "8-3"], CATALOG, argv_prefix=PREFIX)
    assert caught.value.suggestions == [[*PREFIX, "29", "3-8"]]


def test_parse_cv_spec_refuses_a_backwards_range_with_the_corrected_suggestion():
    with pytest.raises(UsageProblem) as caught:
        parse_cv_spec(["8-3"], CATALOG, argv_prefix=PREFIX)
    assert caught.value.suggestions == [[*PREFIX, "3-8"]]


def test_parse_cv_spec_refuses_an_empty_piece():
    with pytest.raises(UsageProblem, match="empty CV token"):
        parse_cv_spec(["1,,3"], CATALOG, argv_prefix=PREFIX)


def test_parse_cv_spec_refuses_an_empty_token_list():
    with pytest.raises(UsageProblem, match="no CVs given"):
        parse_cv_spec([], CATALOG, argv_prefix=PREFIX)


@pytest.mark.parametrize("token", ["0", "1025"])
def test_parse_cv_spec_bound_is_cv_out_of_range_not_usage(token: str):
    # Exit 15, per the design: a CV above the bound names the bound and (via
    # `default_suggestions`) suggests `railctl doctor`.
    with pytest.raises(CvOutOfRangeError, match=r"1\.\.1024"):
        parse_cv_spec([token], CATALOG, argv_prefix=PREFIX)


def test_parse_cv_spec_accepts_both_bound_edges():
    assert parse_cv_spec(["1", "1024"], CATALOG, argv_prefix=PREFIX) == [1, 1024]


def test_parse_cv_spec_checks_every_cv_of_a_range_against_the_bound():
    with pytest.raises(CvOutOfRangeError):
        parse_cv_spec(["1020-1030"], CATALOG, argv_prefix=PREFIX)


# -- parse_page ----------------------------------------------------------------


def test_parse_page_none_stays_none():
    assert parse_page(None, argv_hint=PREFIX) is None


def test_parse_page_parses_the_cv31_cv32_pair():
    assert parse_page("145:0", argv_hint=PREFIX) == (145, 0)


@pytest.mark.parametrize("token", ["145", "a:b", "1:2:3", ""])
def test_parse_page_refuses_a_malformed_token(token: str):
    with pytest.raises(UsageProblem) as caught:
        parse_page(token, argv_hint=PREFIX)
    assert caught.value.details["reason"] == "malformed_page"


@pytest.mark.parametrize(("token", "field"), [("300:0", "CV31"), ("0:300", "CV32")])
def test_parse_page_refuses_a_non_byte_value_naming_the_field(token: str, field: str):
    with pytest.raises(UsageProblem) as caught:
        parse_page(token, argv_hint=PREFIX)
    assert caught.value.details == {
        "reason": "page_value_not_a_byte",
        "field": field,
        "value": 300,
    }


def test_parse_page_accepts_both_byte_edges():
    # On the boundary, not 45 past it: 255 is a byte and must parse.
    assert parse_page("255:255", argv_hint=PREFIX) == (255, 255)
    assert parse_page("0:0", argv_hint=PREFIX) == (0, 0)


@pytest.mark.parametrize(("token", "field"), [("256:0", "CV31"), ("0:256", "CV32")])
def test_parse_page_refuses_one_past_the_byte_edge(token: str, field: str):
    with pytest.raises(UsageProblem) as caught:
        parse_page(token, argv_hint=PREFIX)
    assert caught.value.details == {
        "reason": "page_value_not_a_byte",
        "field": field,
        "value": 256,
    }


# -- the confirmation set and the track/mode maps ------------------------------


def test_the_confirmation_set_is_the_designs_literal():
    assert CONFIRM_CVS == frozenset({1, 8, 17, 18, 29, 31, 32, 144})


def test_every_confirmed_cv_but_cv8_has_a_reason_and_cv8_has_its_own_wording():
    assert set(_CONFIRM_REASONS) == set(CONFIRM_CVS) - {FACTORY_RESET_CV}
    assert "FACTORY-RESET" in _confirm_question(8, 8)
    assert "factory-resets" in _confirm_question(8, 145)
    assert "primary address" in _confirm_question(1, 4)
    assert "index-page selector" in _confirm_question(31, 145)


def test_the_track_maps_agree_with_the_manifest_enum_and_each_other():
    assert set(MODE_FOR_TRACK) == set(CV_WRITE_TRACK_OPT.enum or ())
    for track, mode in MODE_FOR_TRACK.items():
        assert TRACK_FOR_MODE[mode] == track
    assert set(CV_READ_MODE_OPT.enum or ()) == {m.value for m in ProgMode}


def test_the_two_rows_publish_their_safety_metadata():
    read_meta = command_meta("cv read")
    write_meta = command_meta("cv write")
    # `cv read` mutates and confirms: `--page` on an indexed CV writes the
    # CV31/CV32 index selectors, and a manifest that said `mutates: false`
    # published a read that writes (review finding C1).
    assert read_meta.mutates is True
    assert read_meta.confirms is True
    assert write_meta.mutates is True
    assert write_meta.confirms is True


def test_the_cv_group_epilog_carries_both_schemas_and_the_fixed_headings():
    epilog = group_epilog("cv")
    for needed in (
        "OUTPUT",
        "EXIT CODES",
        "EXAMPLES",
        "railctl/cv-read/v1 (cv read)",
        "railctl/cv-write/v1 (cv write)",
        "railctl cv read",
        "railctl cv write",
    ):
        assert needed in epilog


# -- builders ------------------------------------------------------------------


def _read_outcome(
    cv: int, value: int | None = None, error: Exception | None = None
) -> CvReadOutcome:
    result = None
    if value is not None:
        result = CvResult(
            cv=cv,
            value=value,
            mode=ProgMode.SERVICE,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="read",
            verified=None,
            elapsed=0.01,
        )
    return CvReadOutcome(spec=CvSpec(cv=cv), result=result, error=error)


def _write_result(cv: int, value: int, *, verified: bool | None, mode=ProgMode.SERVICE) -> CvResult:
    return CvResult(
        cv=cv,
        value=value,
        mode=mode,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="write",
        verified=verified,
        elapsed=0.02,
    )


def test_build_cv_read_all_ok_names_the_cvs_and_exits_zero():
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(UNCURATED_CV, 5)],
        mode=ProgMode.SERVICE,
        address=None,
        catalog=CATALOG,
    )
    assert built.ok is True
    assert built.exit_code == 0
    assert built.result["mode"] == "service"
    assert built.result["track"] == "prog"
    assert built.result["address"] is None
    assert built.result["requested"] == 2
    assert built.result["ok"] == 2
    assert built.result["failed"] == 0
    rows = built.result["cvs"]
    assert rows[0] == {"cv": 8, "name": "manufacturer_id", "status": "ok", "value": 145}
    # No catalog entry -> empty name, a smaller claim than an invented one.
    assert rows[1] == {"cv": UNCURATED_CV, "name": "", "status": "ok", "value": 5}
    assert "CV8 manufacturer_id = 145" in built.lines
    assert f"CV{UNCURATED_CV} = 5" in built.lines


def test_build_cv_read_catalog_range_is_advisory_on_read():
    # CV1's catalog range is 1..127; a decoder answering 200 is still `ok`.
    built = build_cv_read(
        [_read_outcome(1, 200)], mode=ProgMode.SERVICE, address=None, catalog=CATALOG
    )
    row = built.result["cvs"][0]
    assert row["status"] == "ok"
    assert row["value"] == 200
    assert row["note"] == "outside the catalog's 1..127"
    assert built.ok is True
    warning = built.warnings[0]
    assert warning.name == "cv.value_outside_catalog_range"
    assert "advisory on read" in warning.message


def test_build_cv_read_silence_is_no_response_with_no_value_key():
    # ADDENDUM A4: never value 0, never dropped from the list.
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(253, error=DecoderNotRespondingError("silence"))],
        mode=ProgMode.SERVICE,
        address=None,
        catalog=CATALOG,
    )
    row = built.result["cvs"][1]
    assert row["status"] == "no_response"
    assert "value" not in row
    assert row["error"] == "decoder_not_responding"
    assert built.ok is False
    assert built.exit_code == PARTIAL_EXIT_CODE
    assert built.result == {**built.result, "ok": 1, "failed": 1}
    assert any(w.name == "cv.no_response" and w.message == SILENCE_GUIDANCE for w in built.warnings)


def test_build_cv_read_pom_silence_gets_no_programming_track_guidance():
    # The guidance names the programming track; a POM batch is not on it.
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(253, error=DecoderNotRespondingError("silence"))],
        mode=ProgMode.POM,
        address=3,
        catalog=CATALOG,
    )
    assert built.result["track"] == "main"
    assert built.result["address"] == 3
    assert not any(w.name == "cv.no_response" for w in built.warnings)


def test_build_cv_read_a_mode_that_cannot_reach_a_cv_is_a_skip_with_a_reason():
    error = IndexPageRequiredError("CV260 is behind a ZIMO index page", cv=260)
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(260, error=error)],
        mode=ProgMode.SERVICE,
        address=None,
        catalog=CATALOG,
    )
    row = built.result["cvs"][1]
    assert row["status"] == "skipped"
    assert row["error"] == "index_page_required"
    assert "index page" in row["reason"]


def test_build_cv_read_not_attempted_is_a_skip_not_a_success():
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(9)],
        mode=ProgMode.SERVICE,
        address=None,
        catalog=CATALOG,
    )
    row = built.result["cvs"][1]
    assert row["status"] == "skipped"
    assert row["reason"] == "not attempted"
    assert built.exit_code == PARTIAL_EXIT_CODE


def test_build_cv_read_any_other_error_is_failed_with_its_code():
    built = build_cv_read(
        [_read_outcome(8, 145), _read_outcome(9, error=UnsupportedCommandError("61 82"))],
        mode=ProgMode.SERVICE,
        address=None,
        catalog=CATALOG,
    )
    row = built.result["cvs"][1]
    assert row["status"] == "failed"
    assert row["error"] == "unsupported_command"


def test_all_failed_tells_a_mixed_batch_from_a_dead_one():
    dead = [_read_outcome(8, error=DecoderNotRespondingError("x"))]
    mixed = [_read_outcome(8, error=DecoderNotRespondingError("x")), _read_outcome(9, 1)]
    assert _all_failed(dead) is True
    assert _all_failed(mixed) is False


def test_with_guidance_attaches_the_placement_test_only_to_service_silence():
    silence = DecoderNotRespondingError("no result", cv=253, details={"attempts": 3})
    guided = _with_guidance(silence, mode=ProgMode.SERVICE)
    assert isinstance(guided, DecoderNotRespondingError)
    assert guided.hint == SILENCE_GUIDANCE
    assert guided.cv == 253
    assert guided.details == {"attempts": 3}
    assert _with_guidance(silence, mode=ProgMode.POM) is silence
    other = UnsupportedCommandError("61 82")
    assert _with_guidance(other, mode=ProgMode.SERVICE) is other


def test_build_cv_write_verified_says_so_and_warns_nothing():
    built = build_cv_write(
        _write_result(3, 20, verified=True),
        track="prog",
        address=None,
        name="accel_rate",
        verify=True,
    )
    assert built.result == {
        "cv": 3,
        "name": "accel_rate",
        "value": 20,
        "mode": "service",
        "track": "prog",
        "address": None,
        "verified": True,
    }
    assert built.warnings == []
    assert "CV3 accel_rate = 20 written (service mode, verified: yes)" in built.lines


def test_build_cv_write_on_main_reports_verified_null_and_says_when_it_can_be_checked():
    built = build_cv_write(
        _write_result(3, 20, verified=None, mode=ProgMode.POM),
        track="main",
        address=3,
        name="accel_rate",
        verify=False,
    )
    # `null`, never `false`: nothing measured the decoder, and `false` would
    # claim a mismatch nobody measured.
    assert built.result["verified"] is None
    assert built.result["track"] == "main"
    assert "verified: unknown" in built.lines[0]
    warning = built.warnings[0]
    assert warning.name == "cv.write_unverified"
    assert "programming track" in warning.message
    assert "railctl cv read 3" in warning.message


def test_build_cv_write_no_verify_is_said_out_loud_and_carries_the_echo_only_warning():
    built = build_cv_write(
        _write_result(3, 20, verified=None),
        track="prog",
        address=None,
        name="accel_rate",
        verify=False,
    )
    assert "not read back (--no-verify)" in built.lines
    warning = built.warnings[0]
    assert warning.name == "cv.write_unverified"
    assert "echo" in warning.message


def test_build_cv_write_echo_only_confirmation_is_named_for_what_it_is():
    built = build_cv_write(
        _write_result(3, 20, verified=None),
        track="prog",
        address=None,
        name="accel_rate",
        verify=True,
    )
    warning = built.warnings[0]
    assert warning.name == "cv.write_unverified"
    assert "echo" in warning.message


# -- the wired commands --------------------------------------------------------


class FakeCvStation:
    """Answers the facade surface the cv commands touch, and records the calls."""

    identity = "serial:7010A0001194:3"

    def __init__(
        self,
        *,
        capabilities: Capabilities | None = None,
        raw_status: int = 0x00,
        read_values: dict[int, int] | None = None,
        read_errors: dict[int, Exception] | None = None,
        write_verified: bool | None = None,
        write_error: Exception | None = None,
        emit_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        self.capabilities = capabilities or Capabilities(
            link_identity=self.identity, pom_read=False, service_direct_cv=True
        )
        self.raw_status = raw_status
        self.read_values = read_values or {}
        self.read_errors = read_errors or {}
        self.write_verified = write_verified
        self.write_error = write_error
        self.read_calls: list[dict[str, object]] = []
        self.singleton_calls: list[dict[str, object]] = []
        self.write_calls: list[dict[str, object]] = []
        self.status_calls = 0
        #: Events the fake emits through the `on_event` callback `Station.open`
        #: was handed, once the first facade call runs - `None` callback means
        #: they are dropped, exactly like the real `Station.emit`.
        self.emit_events = emit_events or []
        self.on_event: object | None = None

    def _emit_all(self) -> None:
        if self.on_event is None:
            return
        for name, payload in self.emit_events:
            self.on_event(name, payload)
        self.emit_events = []

    def status(self) -> StationStatus:
        self.status_calls += 1
        return StationStatus.from_raw(self.raw_status)

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def cv_read(self, cv, *, address=None, mode=ProgMode.SERVICE, page=None):
        self.singleton_calls.append({"cv": cv, "address": address, "mode": mode})
        error = self.read_errors.get(cv)
        if error is not None:
            raise error
        return CvResult(
            cv=cv,
            value=self.read_values.get(cv, 145),
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="read",
            verified=None,
            elapsed=0.01,
        )

    def cv_read_many(self, specs, *, address=None, mode=ProgMode.SERVICE, on_progress=None):
        self.read_calls.append({"specs": list(specs), "address": address, "mode": mode})
        self._emit_all()
        outcomes = []
        # Station wire order, like the real batch: sorted by (page, cv). The
        # CLI owns putting rows back into request order.
        specs = sorted(specs, key=lambda spec: (spec.page or (0, 0), spec.cv))
        for spec in specs:
            error = self.read_errors.get(spec.cv)
            if error is not None:
                outcomes.append(CvReadOutcome(spec=spec, result=None, error=error))
                continue
            outcomes.append(
                CvReadOutcome(
                    spec=spec,
                    result=CvResult(
                        cv=spec.cv,
                        value=self.read_values.get(spec.cv, 145),
                        mode=mode,
                        encoding=CvEncoding.SERVICE_DIRECT,
                        operation="read",
                        verified=None,
                        elapsed=0.01,
                    ),
                    error=None,
                )
            )
        return outcomes

    def cv_write(self, cv, value, *, address=None, mode=ProgMode.SERVICE, page=None, verify=True):
        self.write_calls.append(
            {"cv": cv, "value": value, "address": address, "mode": mode, "verify": verify}
        )
        self._emit_all()
        if self.write_error is not None:
            raise self.write_error
        # What the fixed station reports: True after an independent read-back
        # (or Ready), None when nothing measured the decoder. `write_verified`
        # overrides for tests that need a specific value.
        default_verified = True if verify else None
        verified = default_verified if self.write_verified is None else self.write_verified
        return CvResult(
            cv=cv,
            value=value,
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="write",
            verified=verified,
            elapsed=0.02,
        )

    def close(self) -> None:
        pass


#: AUTO resolves to POM when nothing recorded it as a measured no.
POM_CAPS = Capabilities(link_identity=FakeCvStation.identity, pom_read=None)


def _install(monkeypatch, fake: FakeCvStation) -> FakeCvStation:
    def fake_open(*_a, **kwargs):
        # The same seam the real `Station.open` provides: the callback the CLI
        # hands over is what the fake's `_emit_all` calls.
        fake.on_event = kwargs.get("on_event")
        return fake

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    return fake


def _stderr_envelope(result) -> dict[str, object]:
    # stderr is the mixed stream (notices ride above the error object); the
    # envelope is its last line.
    return json.loads(result.stderr.strip().splitlines()[-1])


def _published(path: str, code: int) -> bool:
    return code in command_meta(path).exit_codes


def test_cv_read_service_happy_path_carries_both_keys_and_the_notice(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(read_values={8: 145}))
    result = runner.invoke(app, ["cv", "read", "8", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "railctl/cv-read/v1"
    assert payload["link"]["identity"] == fake.identity
    assert payload["result"]["mode"] == "service"
    assert payload["result"]["track"] == "prog"
    assert payload["result"]["cvs"] == [
        {"cv": 8, "name": "manufacturer_id", "status": "ok", "value": 145}
    ]
    assert PROG_TRACK_NOTICE in result.stderr
    assert fake.read_calls[0]["mode"] is ProgMode.SERVICE
    assert fake.read_calls[0]["address"] is None


def test_cv_read_human_and_json_carry_the_same_fact(monkeypatch):
    _install(monkeypatch, FakeCvStation(read_values={8: 145}))
    human = runner.invoke(app, ["cv", "read", "8"])
    assert human.exit_code == 0
    assert "CV8 manufacturer_id = 145" in human.stdout


def test_cv_read_auto_resolves_pom_and_requires_an_address(monkeypatch):
    _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS))
    result = runner.invoke(app, ["cv", "read", "8", "--format", "json"])
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "usage"
    assert envelope["suggestions"] == [["railctl", "cv", "read", "8", "--address", "3"]]


def test_cv_read_pom_runs_the_preflight_and_sends_the_address(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS))
    result = runner.invoke(app, ["cv", "read", "8", "--address", "3", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls >= 1  # the pre-flight read the status
    assert fake.read_calls[0]["mode"] is ProgMode.POM
    assert fake.read_calls[0]["address"] == 3
    payload = json.loads(result.stdout)
    assert payload["result"]["track"] == "main"
    assert payload["result"]["address"] == 3
    assert PROG_TRACK_NOTICE not in result.stderr


@pytest.mark.parametrize(
    ("raw_status", "expected_exit"),
    [(0x02, 20), (0x01, 20), (0x08, 12)],
    ids=["emergency-off", "emergency-stop", "service-mode"],
)
def test_cv_read_pom_preflight_refusals_exit_with_published_codes(
    monkeypatch, raw_status: int, expected_exit: int
):
    _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS, raw_status=raw_status))
    result = runner.invoke(app, ["cv", "read", "8", "--address", "3", "--format", "json"])
    assert result.exit_code == expected_exit, result.stderr
    assert _published("cv read", expected_exit)


def test_cv_read_explicit_service_mode_skips_the_preflight(monkeypatch):
    # An emergency-stopped layout must not veto a programming-track read:
    # service mode needs no track power on this station (measured 2026-08-06).
    fake = _install(monkeypatch, FakeCvStation(raw_status=0x01))
    result = runner.invoke(app, ["cv", "read", "8", "--mode", "service", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls == 0


def test_cv_read_auto_with_pom_ruled_out_and_nothing_proven_exits_16(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(
            capabilities=Capabilities(link_identity=FakeCvStation.identity, pom_read=False)
        ),
    )
    result = runner.invoke(app, ["cv", "read", "8", "--format", "json"])
    assert result.exit_code == 16, result.stderr
    assert _published("cv read", 16)
    assert _stderr_envelope(result)["code"] == "pom_read_unsupported"


def test_cv_read_above_the_bound_exits_15_with_the_doctor_suggestion(monkeypatch):
    # No station is ever opened: the bound refusal comes first.
    def _boom(*_a, **_k):
        raise AssertionError("the bound refusal must come before any port is touched")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))
    result = runner.invoke(app, ["cv", "read", "1025", "--format", "json"])
    assert result.exit_code == 15, result.stderr
    assert _published("cv read", 15)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "cv_out_of_range"
    assert envelope["suggestions"] == [["railctl", "doctor"]]
    assert "1..1024" in envelope["message"]


def test_cv_read_unknown_slug_is_a_usage_error_with_runnable_suggestions(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "read", "accel_rte", "--format", "json"])
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "usage"
    assert envelope["suggestions"][0] == ["railctl", "cv", "read", "accel_rate"]


def test_cv_read_a_bad_mode_exits_2(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "read", "8", "--mode", "xml", "--format", "json"])
    assert result.exit_code == 2
    assert "--mode must be one of" in _stderr_envelope(result)["message"]


def test_cv_read_a_bad_page_exits_2(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "read", "8", "--page", "145", "--format", "json"])
    assert result.exit_code == 2


def test_cv_read_attaches_the_declared_page_to_indexed_cvs_only(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(read_values={8: 145, INDEXED_CV: 7}))
    result = runner.invoke(
        app, ["cv", "read", f"8,{INDEXED_CV}", "--page", "145:0", "--yes", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    specs = fake.read_calls[0]["specs"]
    assert [(spec.cv, spec.page) for spec in specs] == [(8, None), (INDEXED_CV, (145, 0))]


def test_cv_read_with_a_page_on_an_indexed_cv_is_confirmed_before_the_station_opens(monkeypatch):
    """C1's confirmation half, plus the refusal-order proof (C10's shape): the
    gate runs the same `confirm()` `cv write` uses for CV31/CV32, and it runs
    BEFORE any port is touched - a fake whose `open` raises proves the refusal
    never reached it."""

    def _boom(*_a, **_k):
        raise AssertionError("the confirmation refusal must come before the station opens")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))
    result = runner.invoke(
        app,
        [
            "cv",
            "read",
            str(INDEXED_CV),
            "--page",
            "145:0",
            "--non-interactive",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    assert "CV31" in envelope["message"] and "CV32" in envelope["message"]
    assert envelope["suggestions"] == [
        ["railctl", "cv", "read", str(INDEXED_CV), "--page", "145:0", "--yes"]
    ]


def test_cv_read_with_a_page_and_no_indexed_cv_needs_no_confirmation(monkeypatch):
    # The page is attached to indexed CVs only, so nothing gets written and
    # nothing needs confirming.
    fake = _install(monkeypatch, FakeCvStation(read_values={8: 145}))
    result = runner.invoke(
        app, ["cv", "read", "8", "--page", "145:0", "--non-interactive", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    assert fake.read_calls[0]["specs"][0].page is None


def test_cv_read_rows_come_back_in_request_order_not_station_order(monkeypatch):
    # The spec sentence is "first-appearance order is kept". The station batch
    # returns wire order - sorted by (page, cv) - and the fake mimics that, so
    # this goes red if the CLI stops reordering the rows.
    _install(monkeypatch, FakeCvStation(read_values={29: 14, 3: 20, 8: 145}))
    result = runner.invoke(app, ["cv", "read", "29,3,8", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)["result"]["cvs"]
    assert [row["cv"] for row in rows] == [29, 3, 8]


def test_cv_read_31_alone_is_served_as_a_singleton_read(monkeypatch):
    # The station batch refuses CV31/CV32 (page-selection interleaving); the
    # published grammar accepts 1..1024, so the CLI reads them as singletons.
    fake = _install(monkeypatch, FakeCvStation(read_values={31: 145}))
    result = runner.invoke(app, ["cv", "read", "31", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert [call["cv"] for call in fake.singleton_calls] == [31]
    assert fake.read_calls == []  # no empty batch went out
    rows = json.loads(result.stdout)["result"]["cvs"]
    assert rows == [{"cv": 31, "name": "index_page_high", "status": "ok", "value": 145}]


def test_cv_read_a_range_containing_the_selectors_splits_and_merges_in_request_order(monkeypatch):
    fake = _install(
        monkeypatch,
        FakeCvStation(read_values={29: 14, 30: 1, 31: 145, 32: 0, 33: 2}),
    )
    result = runner.invoke(app, ["cv", "read", "29-33", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert [call["cv"] for call in fake.singleton_calls] == [31, 32]
    assert [spec.cv for spec in fake.read_calls[0]["specs"]] == [29, 30, 33]
    payload = json.loads(result.stdout)
    assert [row["cv"] for row in payload["result"]["cvs"]] == [29, 30, 31, 32, 33]
    assert payload["result"]["ok"] == 5


def test_cv_read_a_failing_selector_read_is_a_row_not_an_abort(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(
            read_values={29: 14},
            read_errors={31: DecoderNotRespondingError("silence", cv=31)},
        ),
    )
    result = runner.invoke(app, ["cv", "read", "31,29", "--format", "json"])
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    rows = json.loads(result.stdout)["result"]["cvs"]
    assert [(row["cv"], row["status"]) for row in rows] == [(31, "no_response"), (29, "ok")]


def test_cv_read_an_indexed_cv_without_a_page_exits_17(monkeypatch):
    error = IndexPageRequiredError(f"CV{INDEXED_CV} is behind a ZIMO index page", cv=INDEXED_CV)
    _install(monkeypatch, FakeCvStation(read_errors={INDEXED_CV: error}))
    result = runner.invoke(app, ["cv", "read", str(INDEXED_CV), "--format", "json"])
    assert result.exit_code == 17, result.stderr
    assert _published("cv read", 17)
    assert _stderr_envelope(result)["code"] == "index_page_required"


def test_cv_read_total_silence_exits_13_with_the_placement_test_hint(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(read_errors={253: DecoderNotRespondingError("no result for CV253", cv=253)}),
    )
    result = runner.invoke(app, ["cv", "read", "253", "--format", "json"])
    assert result.exit_code == 13, result.stderr
    assert _published("cv read", 13)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "decoder_not_responding"
    assert envelope["hint"] == SILENCE_GUIDANCE
    assert "railctl cv read 8" in envelope["hint"]
    assert ["railctl", "doctor"] in envelope["suggestions"]


def test_cv_read_total_pom_silence_keeps_the_stations_own_story(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(
            capabilities=POM_CAPS,
            read_errors={8: DecoderNotRespondingError("no POM result", cv=8)},
        ),
    )
    result = runner.invoke(app, ["cv", "read", "8", "--address", "3", "--format", "json"])
    assert result.exit_code == 13, result.stderr
    assert _stderr_envelope(result)["hint"] is None


def test_cv_read_a_mixed_batch_is_a_partial_result_not_an_error(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(
            read_values={8: 145},
            read_errors={253: DecoderNotRespondingError("silence", cv=253)},
        ),
    )
    result = runner.invoke(app, ["cv", "read", "8", "253", "--format", "json"])
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    assert _published("cv read", PARTIAL_EXIT_CODE)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["result"]["ok"] == 1
    assert payload["result"]["failed"] == 1
    statuses = [row["status"] for row in payload["result"]["cvs"]]
    assert statuses == ["ok", "no_response"]


def test_cv_read_station_events_reach_the_envelope_as_warnings(monkeypatch):
    """C5: `cv.stale_result`, `page.unverified` and friends are envelope
    warning content by design, and before the `on_event` seam existed they
    were dropped on the floor for every command. The payload is serialised -
    a ProgMode enum or a tuple must not crash the JSON renderer."""
    _install(
        monkeypatch,
        FakeCvStation(
            read_values={8: 145},
            emit_events=[
                ("cv.stale_result", {"cv": 8, "raw_cv": 3, "encoding": "SERVICE_DIRECT"}),
                ("page.unverified", {"page": (10, 2), "mode": ProgMode.SERVICE}),
            ],
        ),
    )
    result = runner.invoke(app, ["cv", "read", "8", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    warnings = json.loads(result.stdout)["warnings"]
    by_name = {w["name"]: w for w in warnings}
    assert by_name["cv.stale_result"]["details"] == {
        "cv": 8,
        "raw_cv": 3,
        "encoding": "SERVICE_DIRECT",
    }
    assert by_name["page.unverified"]["details"] == {"page": [10, 2], "mode": "service"}
    assert "cv.stale_result" in by_name["cv.stale_result"]["message"]


def test_cv_write_station_events_reach_the_envelope_as_warnings(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(emit_events=[("cv.unexercised_band", {"cv": 300, "page": 2})]),
    )
    result = runner.invoke(app, ["cv", "write", "3", "20", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    warnings = json.loads(result.stdout)["warnings"]
    assert {"cv": 300, "page": 2} in [w["details"] for w in warnings]


def test_cv_write_prog_happy_path_verifies_by_default(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "3", "20", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "railctl/cv-write/v1"
    assert payload["result"] == {
        "cv": 3,
        "name": "accel_rate",
        "value": 20,
        "mode": "service",
        "track": "prog",
        "address": None,
        "verified": True,
    }
    assert PROG_TRACK_NOTICE in result.stderr
    assert fake.write_calls == [
        {"cv": 3, "value": 20, "address": None, "mode": ProgMode.SERVICE, "verify": True}
    ]


def test_cv_write_accepts_a_catalog_slug(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "accel_rate", "20", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["cv"] == 3


def test_cv_write_no_verify_is_passed_through_and_reported(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "3", "20", "--no-verify", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["verify"] is False
    payload = json.loads(result.stdout)
    # `null`, never `false`: the read-back was skipped, so nothing measured
    # the decoder, and `false` would claim a mismatch nobody measured.
    assert payload["result"]["verified"] is None


def test_cv_write_on_main_needs_an_address_and_never_verifies(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS))
    refused = runner.invoke(app, ["cv", "write", "3", "20", "--track", "main", "--format", "json"])
    assert refused.exit_code == 2
    assert _stderr_envelope(refused)["suggestions"] == [
        ["railctl", "cv", "write", "3", "20", "--track", "main", "--address", "3"]
    ]
    result = runner.invoke(
        app, ["cv", "write", "3", "20", "--track", "main", "--address", "3", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls >= 1  # the POM pre-flight ran
    assert fake.write_calls == [
        {"cv": 3, "value": 20, "address": 3, "mode": ProgMode.POM, "verify": False}
    ]
    payload = json.loads(result.stdout)
    assert payload["result"]["track"] == "main"
    # `null`, never `false`: a main-track write cannot be checked, so nothing
    # measured the decoder.
    assert payload["result"]["verified"] is None
    assert payload["warnings"][0]["name"] == "cv.write_unverified"


def test_cv_write_explicit_verify_with_main_is_refused_not_downgraded(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS))
    result = runner.invoke(
        app,
        [
            "cv",
            "write",
            "3",
            "20",
            "--track",
            "main",
            "--verify",
            "--address",
            "3",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["details"]["reason"] == "verify_on_main"
    assert ["railctl", "cv", "write", "3", "20", "--track", "main", "--no-verify"] in envelope[
        "suggestions"
    ]
    assert fake.write_calls == []  # nothing went out


def test_cv_write_main_preflight_refuses_on_an_emergency_state(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS, raw_status=0x01))
    result = runner.invoke(
        app, ["cv", "write", "3", "20", "--track", "main", "--address", "3", "--format", "json"]
    )
    assert result.exit_code == 20, result.stderr
    assert _published("cv write", 20)
    assert fake.write_calls == []


def test_cv_write_prog_never_preflights_track_power(monkeypatch):
    # Service mode needs no track power on this station (measured 2026-08-06),
    # so a dead track must not veto a programming-track write.
    fake = _install(monkeypatch, FakeCvStation(raw_status=0x02))
    result = runner.invoke(app, ["cv", "write", "3", "20", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls == 0


def test_cv_write_catalog_range_is_enforcing_before_any_telegram(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("the value refusal must come before any port is touched")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))
    result = runner.invoke(app, ["cv", "write", "1", "200", "--format", "json"])
    assert result.exit_code == 15, result.stderr
    assert _published("cv write", 15)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "cv_out_of_range"
    assert "1..127" in envelope["message"]
    assert envelope["details"]["reason"] == "value_out_of_range"
    # `railctl doctor` re-measures the station; it cannot help a value refusal.
    assert envelope["suggestions"] == []


@pytest.mark.parametrize("value", [1, 127])
def test_cv_write_accepts_both_catalog_edges(monkeypatch, value: int):
    # CV1's catalog range is 1..127: both edges are inside it and must go out.
    # (--yes: CV1 is in the confirmation set.)
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "1", str(value), "--yes", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["value"] == value


@pytest.mark.parametrize("value", [0, 128])
def test_cv_write_one_past_either_catalog_edge_is_refused(monkeypatch, value: int):
    # One past the edge, not 73 past it: 128 (and 0) must be the refusal.
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "1", str(value), "--yes", "--format", "json"])
    assert result.exit_code == 15, result.stderr
    assert "1..127" in _stderr_envelope(result)["message"]
    assert fake.write_calls == []


def test_cv_write_a_non_byte_value_is_a_usage_error(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "11", "300", "--format", "json"])
    assert result.exit_code == 2
    assert _stderr_envelope(result)["details"]["reason"] == "value_not_a_byte"


@pytest.mark.parametrize("value", [0, 255])
def test_cv_write_accepts_both_value_edges(monkeypatch, value: int):
    # On the boundary: 0 and 255 are bytes and must go out. CV11 is uncurated,
    # so no catalog range narrows the byte.
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", str(UNCURATED_CV), str(value), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["value"] == value


def test_cv_write_one_past_the_value_edge_is_refused(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", str(UNCURATED_CV), "256", "--format", "json"])
    assert result.exit_code == 2
    assert _stderr_envelope(result)["details"]["reason"] == "value_not_a_byte"
    assert fake.write_calls == []


def test_cv_write_takes_exactly_one_cv(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "1,3", "20", "--yes", "--format", "json"])
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["details"]["reason"] == "multiple_cvs"
    assert ["railctl", "cv", "write", "1", "20"] in envelope["suggestions"]


def test_cv_write_a_confirmed_cv_refuses_without_yes_when_not_interactive(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "29", "6", "--non-interactive", "--format", "json"])
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    # The full runnable argv - CV and value included - never the bare
    # ["railctl", "cv", "write", "--yes"], which exits 2 when run.
    assert envelope["suggestions"] == [["railctl", "cv", "write", "29", "6", "--yes"]]
    assert fake.write_calls == []


def test_cv_write_a_blocked_confirmation_on_main_suggests_the_main_track_argv(monkeypatch):
    _install(monkeypatch, FakeCvStation(capabilities=POM_CAPS))
    result = runner.invoke(
        app,
        [
            "cv",
            "write",
            "29",
            "6",
            "--track",
            "main",
            "--address",
            "3",
            "--non-interactive",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    assert envelope["suggestions"] == [
        ["railctl", "cv", "write", "29", "6", "--track", "main", "--yes"]
    ]


def test_cv_write_confirmation_refusal_comes_before_the_station_opens(monkeypatch):
    """C10: the refusal test proved the refusal, not its ORDER - a fake whose
    `open` raises proves the gate runs before any port is touched, so a
    blocked confirmation costs the operator nothing on the wire."""

    def _boom(*_a, **_k):
        raise AssertionError("the confirmation refusal must come before the station opens")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))
    result = runner.invoke(app, ["cv", "write", "29", "6", "--non-interactive", "--format", "json"])
    assert result.exit_code == 2, result.stderr
    assert _stderr_envelope(result)["code"] == "confirmation_required"


def test_cv_write_yes_answers_the_confirmation(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "29", "6", "--yes", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["cv"] == 29


def test_cv_write_an_unconfirmed_cv_asks_nothing(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "3", "20", "--non-interactive", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert fake.write_calls[0]["cv"] == 3


def test_cv_write_an_uncurated_cv_has_no_catalog_gate_and_no_name(monkeypatch):
    fake = _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", str(UNCURATED_CV), "254", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["name"] == ""
    assert fake.write_calls[0]["cv"] == UNCURATED_CV


def test_cv_write_a_failed_verify_exits_14(monkeypatch):
    _install(
        monkeypatch,
        FakeCvStation(write_error=CvVerifyError("read back 19, expected 20", cv=3)),
    )
    result = runner.invoke(app, ["cv", "write", "3", "20", "--format", "json"])
    assert result.exit_code == 14, result.stderr
    assert _published("cv write", 14)
    assert _stderr_envelope(result)["code"] == "cv_verify"


def test_cv_write_a_bad_track_exits_2(monkeypatch):
    _install(monkeypatch, FakeCvStation())
    result = runner.invoke(app, ["cv", "write", "3", "20", "--track", "yard", "--format", "json"])
    assert result.exit_code == 2
    assert "--track must be one of" in _stderr_envelope(result)["message"]


def test_the_bare_cv_group_is_a_usage_error_with_empty_stdout():
    result = runner.invoke(app, ["cv"])
    assert result.exit_code == 2
    assert result.stdout == ""
