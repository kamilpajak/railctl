"""`railctl backup`: the wired command, all three formats, and the M9 acceptance criteria.

The fake answers the facade surface backup touches (`cv_read` singletons,
`cv_read_many` with `on_progress`, `status`, `version`, `capabilities`) and
records what it was asked, so every test is about the CLI contract - the run
order, the file on disk, the stream, the exit code - never about wire bytes.
`on_progress` is called once per spec exactly like the real batch, because
the Ctrl-C partial file is built from what that callback delivered before
the interrupt; a fake that only returned the finished list would test a
collection path the real station never takes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import railctl.cli.commands.backup as backup_module
from railctl.backup import BACKUP_SCHEMA, read_backup
from railctl.catalog import curated_cvs, load_catalog
from railctl.cli._meta import command_meta
from railctl.cli.commands.backup import SET_NAME
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, SILENCE_GUIDANCE
from railctl.cli.main import app
from railctl.errors import (
    CvOutOfRangeError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    ServiceEncodingUnknownError,
    UnsupportedCommandError,
)
from railctl.station import (
    Capabilities,
    CvEncoding,
    CvReadOutcome,
    CvResult,
    ProgMode,
    Station,
)
from railctl.xbus.cv import MAX_CV_DIRECT
from railctl.xbus.replies import StationStatus, StationVersion

runner = CliRunner()

CATALOG = load_catalog()
#: The curated set a CV29 of 0 selects (bit 4 clear: no speed table) - the
#: design's 77-CV run, read off the shipped catalog rather than retyped.
CURATED = curated_cvs(CATALOG, 0)
OVER_BOUND = [cv for cv in CURATED if cv > MAX_CV_DIRECT]

#: A fixed timestamp for the byte-identity acceptance; the value is the
#: design example's.
FROZEN_CLOCK = "2026-08-03T18:42:11Z"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    # HOME too, not only XDG: `backup_path`'s default resolves against
    # `Path.home()`, and a test that forgets `--out` must never touch the
    # real ~/railctl-backups.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


SERVICE_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3", pom_read=False, service_direct_cv=True
)
POM_YES_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3", pom_read=True, service_direct_cv=True
)
POM_UNKNOWN_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3", pom_read=None, service_direct_cv=True
)
EXT_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3",
    pom_read=False,
    service_direct_cv=True,
    service_ext_cv=True,
)

#: The selectors and CV29 answer 0 (default page, no speed table) unless a
#: test overrides them; every other CV answers this one byte.
DEFAULT_VALUE = 42


class FakeBackupStation:
    """Answers the facade surface `backup` touches, and records the calls."""

    identity = "serial:7010A0001194:3"

    def __init__(
        self,
        *,
        capabilities: Capabilities | None = None,
        raw_status: int = 0x00,
        read_values: dict[int, int] | None = None,
        read_errors: dict[int, Exception] | None = None,
        interrupt_after: int | None = None,
        interrupt_singleton: int | None = None,
        emit_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        self.capabilities = capabilities or SERVICE_CAPS
        self.raw_status = raw_status
        self.read_values = {29: 0, 31: 0, 32: 0, **(read_values or {})}
        self.read_errors = read_errors or {}
        #: Raise KeyboardInterrupt once this many BATCH reads have completed -
        #: the shape a real Ctrl-C takes mid-`cv_read_many`.
        self.interrupt_after = interrupt_after
        #: Raise KeyboardInterrupt when this CV is read as a singleton - a
        #: Ctrl-C before the curated list exists.
        self.interrupt_singleton = interrupt_singleton
        self.emit_events = emit_events or []
        self.on_event: object | None = None
        self.singleton_calls: list[dict[str, object]] = []
        self.batch_calls: list[dict[str, object]] = []
        self.status_calls = 0
        self.reads_done = 0

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

    def _result(self, cv: int, mode: ProgMode) -> CvResult:
        return CvResult(
            cv=cv,
            value=self.read_values.get(cv, DEFAULT_VALUE),
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="read",
            verified=None,
            elapsed=0.01,
        )

    def cv_read(self, cv, *, address=None, mode=ProgMode.SERVICE, page=None):
        if self.interrupt_singleton == cv:
            raise KeyboardInterrupt
        self.singleton_calls.append({"cv": cv, "address": address, "mode": mode})
        error = self.read_errors.get(cv)
        if error is not None:
            raise error
        return self._result(cv, mode)

    def cv_read_many(self, specs, *, address=None, mode=ProgMode.SERVICE, on_progress=None):
        self.batch_calls.append({"specs": list(specs), "address": address, "mode": mode})
        self._emit_all()
        outcomes = []
        total = len(specs)
        for index, spec in enumerate(specs):
            if self.interrupt_after is not None and self.reads_done >= self.interrupt_after:
                raise KeyboardInterrupt
            error = self.read_errors.get(spec.cv)
            if error is not None:
                outcome = CvReadOutcome(spec=spec, result=None, error=error)
            else:
                outcome = CvReadOutcome(spec=spec, result=self._result(spec.cv, mode), error=None)
            self.reads_done += 1
            if on_progress is not None:
                on_progress((index, total, outcome))
            outcomes.append(outcome)
        return outcomes

    def close(self) -> None:
        pass


def _install(monkeypatch, fake: FakeBackupStation) -> FakeBackupStation:
    def fake_open(*_a, **kwargs):
        fake.on_event = kwargs.get("on_event")
        return fake

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    return fake


def _boom_open(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("the refusal must come before any port is touched")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))


def _stderr_envelope(result) -> dict[str, object]:
    return json.loads(result.stderr.strip().splitlines()[-1])


def _published(code: int) -> bool:
    return code in command_meta("backup").exit_codes


def _freeze_clock(monkeypatch) -> None:
    monkeypatch.setattr(backup_module, "utc_timestamp", lambda: FROZEN_CLOCK)


# -- the metadata row ---------------------------------------------------------


def test_the_backup_row_publishes_its_safety_facts():
    meta = command_meta("backup")
    # A backup never writes the decoder - the CV31/CV32 selectors included -
    # and never confirms; an agent reads exactly these two fields to decide
    # whether it is safe to run unattended.
    assert meta.mutates is False
    assert meta.confirms is False
    assert meta.schema == BACKUP_SCHEMA
    # `cv read`'s whole set: a backup is a batch of CV reads through the same
    # station paths, and 9/16 - the codes the brief adds - were already in it.
    assert meta.exit_codes == command_meta("cv read").exit_codes
    assert {2, 9, 12, 13, 16, 17, 20} <= set(meta.exit_codes)


# -- the happy path, all three formats ---------------------------------------


def test_backup_json_result_is_the_file_document_plus_the_path(monkeypatch, tmp_path):
    fake = _install(monkeypatch, FakeBackupStation())
    out = tmp_path / "loco3.json"
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == BACKUP_SCHEMA
    assert payload["link"]["identity"] == fake.identity
    body = payload["result"]
    assert body["path"] == str(out)
    # The envelope result IS the written document, plus the one fact the file
    # cannot carry - where it went.
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert {key: value for key, value in body.items() if key != "path"} == on_disk
    assert body["loco"] == {"address": 3, "kind": "short"}
    assert body["catalog"] == {"family": "zimo-ms-mx", "schema": 1}
    assert body["set"] == SET_NAME
    assert body["mode"] == "service"
    assert body["cv_encoding"] == "SERVICE_DIRECT"
    assert body["page"] == [0, 0]
    assert body["sweep_range"] is None
    assert body["capabilities"]["pom_read"] is False
    assert body["decoder"] == {
        "manufacturer_id": DEFAULT_VALUE,
        "decoder_version": DEFAULT_VALUE,
        "decoder_type": DEFAULT_VALUE,
        "serial_bytes": [DEFAULT_VALUE] * 3,
    }
    summary = body["summary"]
    assert summary["requested"] == len(CURATED)
    assert summary["ok"] == len(CURATED) - len(OVER_BOUND)
    assert summary["skipped"] == len(OVER_BOUND)
    assert summary["complete"] is True
    assert "interrupted" not in body
    assert PROG_TRACK_NOTICE in result.stderr


def test_two_backups_of_an_unchanged_decoder_are_byte_identical(monkeypatch, tmp_path):
    """The M9 acceptance: with the injected clock pinned, everything else in
    the document is deterministic, so the second run reproduces the first
    byte for byte."""
    _freeze_clock(monkeypatch)
    _install(monkeypatch, FakeBackupStation())
    out = tmp_path / "twice.json"
    first = runner.invoke(app, ["backup", "--address", "3", "--out", str(out)])
    assert first.exit_code == 0, first.stderr
    first_bytes = out.read_bytes()
    _install(monkeypatch, FakeBackupStation())
    second = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--force"])
    assert second.exit_code == 0, second.stderr
    assert out.read_bytes() == first_bytes
    assert json.loads(first_bytes)["created_utc"] == FROZEN_CLOCK


def test_backup_human_lists_every_row_and_the_written_path(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    out = tmp_path / "human.json"
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--mode", "service"]
    )
    assert result.exit_code == 0, result.stderr
    assert f"CV8 manufacturer_id = {DEFAULT_VALUE}" in result.stdout
    assert (
        "CV397 volume_up_key: skipped (cv 397 > MAX_CV_DIRECT 255; extended opcodes unavailable)"
    ) in result.stdout
    assert f"{len(CURATED) - len(OVER_BOUND)} of {len(CURATED)} CVs read" in result.stdout
    assert "complete: yes" in result.stdout
    assert f"written to {out}" in result.stdout


def test_backup_to_stdout_prints_the_document_and_writes_no_file(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-"])
    assert result.exit_code == 0, result.stderr
    # Human rendering: the status line, then the document text itself.
    document = json.loads("\n".join(result.stdout.splitlines()[1:]))
    assert document["schema"] == BACKUP_SCHEMA
    assert not (Path(str(tmp_path / "home")) / "railctl-backups").exists()


def test_backup_to_stdout_in_json_reports_a_null_path(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)  # exactly one value on stdout
    assert payload["result"]["path"] is None
    assert not (Path(str(tmp_path / "home")) / "railctl-backups").exists()


def test_backup_default_path_is_the_home_backup_dir_with_the_padded_address(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(app, ["backup", "--address", "3", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    expected = tmp_path / "home" / "railctl-backups" / "loco-0003-curated.json"
    assert expected.exists()
    assert json.loads(result.stdout)["result"]["path"] == str(expected)


def test_backup_address_past_the_short_boundary_is_a_long_loco(monkeypatch, tmp_path):
    # 99 is the last short address; 100 is the first long one (the XpressNet
    # threshold). Both sit ON the boundary, not somewhere past it.
    _install(monkeypatch, FakeBackupStation())
    short = runner.invoke(app, ["backup", "--address", "99", "--out", "-", "--format", "json"])
    assert json.loads(short.stdout)["result"]["loco"] == {"address": 99, "kind": "short"}
    _install(monkeypatch, FakeBackupStation())
    long_ = runner.invoke(app, ["backup", "--address", "100", "--out", "-", "--format", "json"])
    assert json.loads(long_.stdout)["result"]["loco"] == {"address": 100, "kind": "long"}


def test_backup_note_is_stored_and_null_when_absent(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    noted = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", "-", "--note", "stock settings", "--json"],
    )
    assert json.loads(noted.stdout)["result"]["note"] == "stock settings"
    _install(monkeypatch, FakeBackupStation())
    bare = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--json"])
    assert json.loads(bare.stdout)["result"]["note"] is None


def test_backup_station_events_reach_the_envelope_as_warnings(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeBackupStation(emit_events=[("cv.stale_result", {"cv": 8, "echoed": 7})]),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    warnings = json.loads(result.stdout)["warnings"]
    by_name = {w["name"]: w for w in warnings}
    assert by_name["cv.stale_result"]["details"] == {"cv": 8, "echoed": 7}


# -- refusals before the station opens ----------------------------------------


def test_backup_existing_file_is_refused_before_the_station_opens(monkeypatch, tmp_path):
    _boom_open(monkeypatch)
    out = tmp_path / "already.json"
    out.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 2, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "usage"
    assert envelope["details"]["reason"] == "backup_file_exists"
    # The suggestion is the typed invocation with --force appended - runnable
    # as-is, never a sentence to parse.
    assert envelope["suggestions"] == [
        ["railctl", "backup", "--address", "3", "--out", str(out), "--force"]
    ]


def test_backup_refusal_suggestion_keeps_every_typed_flag(monkeypatch, tmp_path):
    _boom_open(monkeypatch)
    default = tmp_path / "home" / "railctl-backups"
    default.mkdir(parents=True)
    (default / "loco-0003-curated.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "backup",
            "--address",
            "3",
            "--note",
            "x",
            "--mode",
            "service",
            "--page",
            "0:0",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2, result.stderr
    assert _stderr_envelope(result)["suggestions"] == [
        [
            "railctl",
            "backup",
            "--address",
            "3",
            "--note",
            "x",
            "--mode",
            "service",
            "--page",
            "0:0",
            "--force",
        ]
    ]


def test_backup_force_overwrites_the_existing_file(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    out = tmp_path / "old.json"
    out.write_text("not a backup", encoding="utf-8")
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--force", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    # Round trip through the strict reader: the overwrite left a valid file.
    assert read_backup(out).summary["complete"] is True


def test_backup_without_an_address_is_a_usage_error(monkeypatch):
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--format", "json"])
    assert result.exit_code == 2
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "usage"
    assert envelope["suggestions"] == [["railctl", "backup", "--address", "3"]]


def test_backup_a_bad_mode_exits_2(monkeypatch):
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--address", "3", "--mode", "xml", "--format", "json"])
    assert result.exit_code == 2
    assert "--mode must be one of" in _stderr_envelope(result)["message"]


def test_backup_a_bad_page_exits_2(monkeypatch):
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--address", "3", "--page", "145", "--format", "json"])
    assert result.exit_code == 2
    assert _stderr_envelope(result)["details"]["reason"] == "malformed_page"


# -- mode resolution -----------------------------------------------------------


def test_backup_mode_pom_on_a_measured_no_exits_16(monkeypatch):
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", "-", "--mode", "pom", "--format", "json"]
    )
    assert result.exit_code == 16, result.stderr
    assert _published(16)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "pom_read_unsupported"
    # Both remedies, named: the re-probe as a runnable argv, the programming
    # track in the hint.
    assert envelope["suggestions"] == [["railctl", "doctor"]]
    assert "--mode service" in envelope["hint"]


def test_backup_mode_pom_with_pom_read_unknown_is_attempted(monkeypatch, tmp_path):
    # Only a MEASURED no refuses; unknown is a real POM run whose outcome the
    # station records.
    fake = _install(monkeypatch, FakeBackupStation(capabilities=POM_UNKNOWN_CAPS))
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", "-", "--mode", "pom", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls >= 1  # the POM pre-flight read the status
    assert fake.batch_calls[0]["mode"] is ProgMode.POM
    assert fake.batch_calls[0]["address"] == 3
    assert json.loads(result.stdout)["result"]["mode"] == "pom"
    assert PROG_TRACK_NOTICE not in result.stderr


def test_backup_auto_resolves_pom_only_on_a_measured_yes(monkeypatch, tmp_path):
    yes = _install(monkeypatch, FakeBackupStation(capabilities=POM_YES_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert yes.batch_calls[0]["mode"] is ProgMode.POM

    # Unknown is NOT yes: `cv read`'s AUTO would gamble on POM here; a 77-CV
    # backup resolves to the programming track instead.
    unknown = _install(monkeypatch, FakeBackupStation(capabilities=POM_UNKNOWN_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert unknown.batch_calls[0]["mode"] is ProgMode.SERVICE
    assert PROG_TRACK_NOTICE in result.stderr


@pytest.mark.parametrize(
    ("raw_status", "expected_exit"),
    [(0x02, 20), (0x01, 20), (0x08, 12)],
    ids=["emergency-off", "emergency-stop", "service-mode"],
)
def test_backup_pom_preflight_refusals_exit_with_published_codes(
    monkeypatch, raw_status: int, expected_exit: int
):
    fake = _install(
        monkeypatch, FakeBackupStation(capabilities=POM_YES_CAPS, raw_status=raw_status)
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == expected_exit, result.stderr
    assert _published(expected_exit)
    assert fake.batch_calls == []  # nothing was read


def test_backup_explicit_service_mode_skips_the_preflight(monkeypatch):
    # An emergency-stopped layout must not veto a programming-track backup.
    fake = _install(monkeypatch, FakeBackupStation(raw_status=0x01))
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", "-", "--mode", "service", "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    assert fake.status_calls == 0


# -- the page rule -------------------------------------------------------------


def test_backup_nonzero_page_without_the_flag_exits_17(monkeypatch, tmp_path):
    out = tmp_path / "never.json"
    _install(monkeypatch, FakeBackupStation(read_values={31: 145}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 17, result.stderr
    assert _published(17)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "index_page_required"
    assert "--page 145:0" in envelope["message"]  # the runnable acknowledgement
    assert not out.exists()


def test_backup_page_flag_acknowledges_and_records_the_read_pair(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(read_values={31: 145}))
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", "-", "--page", "145:0", "--format", "json"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["page"] == [145, 0]
    assert not any(w["name"] == "backup.page_mismatch" for w in payload["warnings"])


def test_backup_page_mismatch_records_the_measurement_and_warns(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(read_values={31: 10, 32: 2}))
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", "-", "--page", "145:0", "--format", "json"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    # The file records what was READ; the declared pair is a warning, never
    # the recorded value.
    assert payload["result"]["page"] == [10, 2]
    warning = next(w for w in payload["warnings"] if w["name"] == "backup.page_mismatch")
    assert warning["details"] == {"declared": [145, 0], "read": [10, 2]}


# -- aborts during collection --------------------------------------------------


def test_backup_cv29_silence_aborts_13_with_the_placement_hint(monkeypatch, tmp_path):
    out = tmp_path / "never.json"
    _install(
        monkeypatch,
        FakeBackupStation(read_errors={29: DecoderNotRespondingError("no result", cv=29)}),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 13, result.stderr
    assert _published(13)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "decoder_not_responding"
    assert envelope["hint"] == SILENCE_GUIDANCE
    assert not out.exists()


def test_backup_a_selector_failure_aborts_with_its_own_code(monkeypatch):
    _install(
        monkeypatch,
        FakeBackupStation(read_errors={31: UnsupportedCommandError("station answered 61 82")}),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 6, result.stderr
    assert _published(6)


# -- holes: the exit-9 incomplete file ----------------------------------------


def test_backup_an_unreadable_cv_is_a_no_response_hole_and_exit_9(monkeypatch, tmp_path):
    """The M9 acceptance: the row says `no_response` with NO value key, the
    summary says `complete: false`, the process exits 9 - and the file is
    still written, because the file is the product."""
    out = tmp_path / "holes.json"
    _install(
        monkeypatch,
        FakeBackupStation(
            read_errors={
                253: DecoderNotRespondingError("no answer after 3 attempts", cv=253),
                250: DecoderNotRespondingError("no answer after 3 attempts", cv=250),
            }
        ),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 9, result.stderr
    assert _published(9)
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "backup_incomplete"
    assert "CV250" in envelope["message"] and "CV253" in envelope["message"]
    assert envelope["details"]["path"] == str(out)
    assert envelope["details"]["no_response"] == [250, 253]
    assert envelope["details"]["error"] == []
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    row = next(r for r in on_disk["cvs"] if r["cv"] == 253)
    assert row["status"] == "no_response"
    assert "value" not in row
    assert row["detail"] == "no answer after 3 attempts"
    assert on_disk["summary"]["complete"] is False
    assert on_disk["summary"]["no_response"] == 2
    # The decoder block records the holes as ABSENT fields, never nulls.
    assert "decoder_type" not in on_disk["decoder"]
    assert "serial_bytes" not in on_disk["decoder"]
    assert on_disk["decoder"]["manufacturer_id"] == DEFAULT_VALUE


def test_backup_a_station_error_row_also_makes_the_file_incomplete(monkeypatch, tmp_path):
    out = tmp_path / "err.json"
    _install(
        monkeypatch,
        FakeBackupStation(read_errors={65: UnsupportedCommandError("station answered 61 82")}),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 9, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["details"]["error"] == [65]
    row = next(r for r in json.loads(out.read_text())["cvs"] if r["cv"] == 65)
    assert row["status"] == "error"


@pytest.mark.parametrize(
    "error",
    [
        PomReadUnsupportedError("the station answered 61 82 mid-run"),
        ServiceEncodingUnknownError("no service-mode encoding established yet"),
    ],
    ids=["pom-refusal", "encoding-unknown"],
)
def test_backup_a_mid_run_refusal_is_an_error_row_and_exit_9(monkeypatch, tmp_path, error):
    """A live refusal is the instrument failing to measure, never a recorded
    decision: the row is `error`, the file incomplete, the exit code 9."""
    out = tmp_path / "refused.json"
    _install(monkeypatch, FakeBackupStation(read_errors={65: error}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 9, result.stderr
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    row = next(r for r in on_disk["cvs"] if r["cv"] == 65)
    assert row["status"] == "error"
    assert row["detail"] == str(error)
    assert on_disk["summary"]["complete"] is False


@pytest.mark.parametrize(
    "error",
    [
        CvOutOfRangeError("direct opcodes only cover CV1..255"),
        IndexPageRequiredError("CV65 lives behind an index page"),
    ],
    ids=["out-of-range", "index-page"],
)
def test_backup_a_preflight_skip_row_stays_skipped_and_exits_0(monkeypatch, tmp_path, error):
    # The boundary of the reclassification: these two are pre-flight refusals
    # inside the programmer - no telegram sent - so they remain recorded
    # decisions and never touch the exit code.
    out = tmp_path / "skips.json"
    _install(monkeypatch, FakeBackupStation(read_errors={65: error}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    row = next(r for r in on_disk["cvs"] if r["cv"] == 65)
    assert row["status"] == "skipped"
    assert on_disk["summary"]["complete"] is True


def test_backup_ext_service_opcodes_reach_past_the_direct_bound(monkeypatch, tmp_path):
    # With the extended encoding a measured yes, the over-255 curated CVs are
    # read instead of skipped.
    _install(monkeypatch, FakeBackupStation(capabilities=EXT_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)["result"]
    assert body["summary"]["skipped"] == 0
    assert body["summary"]["ok"] == len(CURATED)
    row = next(r for r in body["cvs"] if r["cv"] == 397)
    assert row["status"] == "ok"


def test_backup_write_failure_is_backup_file_exit_9(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", str(blocker / "x.json"), "--format", "json"],
    )
    assert result.exit_code == 9, result.stderr
    assert _stderr_envelope(result)["code"] == "backup_file"


# -- Ctrl-C: the partial file --------------------------------------------------


def test_backup_ctrl_c_writes_the_partial_file_and_exits_9(monkeypatch, tmp_path):
    out = tmp_path / "partial.json"
    _install(monkeypatch, FakeBackupStation(interrupt_after=8))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 9, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "aborted"
    assert envelope["details"]["path"] == str(out)
    # The strict reader accepts the partial file: rows for the WHOLE curated
    # set, the unvisited ones skipped as "not attempted", never missing.
    document = read_backup(out)
    assert document.interrupted is True
    assert document.summary["requested"] == len(CURATED)
    # An interrupted run is not complete by definition, whatever its rows say.
    assert document.summary["complete"] is False
    details = {record.detail for record in document.cvs}
    assert "not attempted" in details


def test_backup_ctrl_c_before_the_curated_list_writes_nothing(monkeypatch, tmp_path):
    out = tmp_path / "nothing.json"
    _install(monkeypatch, FakeBackupStation(interrupt_singleton=31))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 9, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "aborted"
    assert "no backup file was written" in envelope["message"]
    assert not out.exists()


def test_backup_ctrl_c_with_a_stdout_target_names_the_reason_nothing_was_kept(monkeypatch):
    _install(monkeypatch, FakeBackupStation(interrupt_after=2))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 9, result.stderr
    assert "stdout" in _stderr_envelope(result)["message"]


# -- ndjson --------------------------------------------------------------------


def _ndjson_lines(stdout: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stdout.splitlines()]


def test_backup_ndjson_sequence_is_contiguous_and_ends_in_summary(monkeypatch, tmp_path):
    """The M9 acceptance for the stream: sequence numbers count up without a
    gap from 0, and the last line is the summary."""
    out = tmp_path / "stream.json"
    _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 0, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    start = lines[0]
    assert start["type"] == "start"
    assert start["schema"] == BACKUP_SCHEMA
    assert start["address"] == 3
    assert start["mode"] == "service"
    assert start["total"] == len(CURATED)
    cv_lines = [line for line in lines if line["type"] == "cv"]
    assert len(cv_lines) == len(CURATED)
    ok_line = next(line for line in cv_lines if line["status"] == "ok")
    assert "value" in ok_line and "elapsed_ms" in ok_line
    skipped = next(line for line in cv_lines if line["status"] == "skipped")
    assert "value" not in skipped and "detail" in skipped
    summary = lines[-1]
    assert summary["type"] == "summary"
    assert summary["requested"] == len(CURATED)
    assert summary["complete"] is True
    assert summary["path"] == str(out)
    assert summary["exit_code"] == 0
    assert out.exists()


def test_backup_ndjson_incomplete_run_still_ends_in_a_summary_with_exit_9(monkeypatch, tmp_path):
    out = tmp_path / "stream9.json"
    _install(
        monkeypatch,
        FakeBackupStation(
            read_errors={253: DecoderNotRespondingError("no answer after 3 attempts", cv=253)}
        ),
    )
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 9, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    summary = lines[-1]
    assert summary["type"] == "summary"
    assert summary["complete"] is False
    assert summary["no_response"] == 1
    assert summary["exit_code"] == 9
    assert _stderr_envelope(result)["code"] == "backup_incomplete"
    assert out.exists()


def test_backup_ndjson_ctrl_c_streams_the_summary_after_the_partial_file(monkeypatch, tmp_path):
    out = tmp_path / "streamed-partial.json"
    _install(monkeypatch, FakeBackupStation(interrupt_after=8))
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 9, result.stderr
    lines = _ndjson_lines(result.stdout)
    summary = lines[-1]
    assert summary["type"] == "summary"
    assert summary["exit_code"] == 9
    assert summary["path"] == str(out)
    document = read_backup(out)
    assert document.interrupted is True
    # The stream and the file describe the SAME run: every summary count and
    # `complete` on the last line match the document the file was written
    # from, and the counts sum to their own `requested`.
    for key in ("requested", "ok", "no_response", "error", "skipped", "complete"):
        assert summary[key] == document.summary[key], key
    assert summary["complete"] is False
    assert (
        summary["ok"] + summary["no_response"] + summary["error"] + summary["skipped"]
        == summary["requested"]
    )
    assert _stderr_envelope(result)["code"] == "aborted"


def test_backup_ndjson_mode_refusal_still_owes_the_stream_its_summary(monkeypatch):
    # The station opened, so the stream exists and must be closed off - with
    # zero counts, because nothing was measured.
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", "-", "--mode", "pom", "--format", "ndjson"],
    )
    assert result.exit_code == 16, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert len(lines) == 1
    assert lines[0]["type"] == "summary"
    assert lines[0]["requested"] == 0
    assert lines[0]["complete"] is False
    assert lines[0]["exit_code"] == 16
    assert _stderr_envelope(result)["code"] == "pom_read_unsupported"


def test_backup_ndjson_page_refusal_summarises_what_little_was_asked(monkeypatch):
    # The abort comes before CV29, so no start line was ever owed - but the
    # summary still is, and it reports the counts as they stood.
    _install(monkeypatch, FakeBackupStation(read_values={31: 145}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "ndjson"])
    assert result.exit_code == 17, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["type"] for line in lines] == ["summary"]
    assert lines[0]["exit_code"] == 17
    assert _stderr_envelope(result)["code"] == "index_page_required"


def test_backup_ndjson_usage_refusal_produces_no_stream_at_all(monkeypatch):
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--address", "3", "--mode", "xml", "--format", "ndjson"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert _stderr_envelope(result)["code"] == "usage"


def test_backup_ndjson_interrupt_before_the_station_opened_exits_9_quietly(monkeypatch):
    def interrupted_open(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(Station, "open", staticmethod(interrupted_open))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "ndjson"])
    assert result.exit_code == 9
    assert result.stdout == ""
    assert result.stderr == ""


def test_backup_ndjson_station_events_and_the_page_mismatch_ride_as_event_lines(
    monkeypatch, tmp_path
):
    _install(
        monkeypatch,
        FakeBackupStation(
            read_values={31: 10, 32: 2},
            emit_events=[("cv.stale_result", {"cv": 8, "echoed": 7})],
        ),
    )
    result = runner.invoke(
        app,
        [
            "backup",
            "--address",
            "3",
            "--out",
            "-",
            "--page",
            "145:0",
            "--format",
            "ndjson",
        ],
    )
    assert result.exit_code == 0, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    events = [line for line in lines if line["type"] == "event"]
    by_name = {event["name"]: event for event in events}
    assert by_name["cv.stale_result"]["details"] == {"cv": 8, "echoed": 7}
    assert by_name["backup.page_mismatch"]["details"] == {
        "declared": [145, 0],
        "read": [10, 2],
    }
    assert lines[-1]["type"] == "summary"


# -- the run order -------------------------------------------------------------


def test_backup_reads_selectors_and_cv29_as_singletons_then_identity_then_the_rest(
    monkeypatch,
):
    fake = _install(monkeypatch, FakeBackupStation())
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert [call["cv"] for call in fake.singleton_calls] == [31, 32, 29]
    # Two batches: the identity CVs first (design C6 step 4), then the rest
    # ascending - and nothing already read is re-read.
    assert [spec.cv for spec in fake.batch_calls[0]["specs"]] == [7, 8, 250, 251, 252, 253]
    rest = [spec.cv for spec in fake.batch_calls[1]["specs"]]
    assert rest == sorted(rest)
    read_anywhere = (
        [call["cv"] for call in fake.singleton_calls]
        + [spec.cv for spec in fake.batch_calls[0]["specs"]]
        + rest
    )
    assert sorted(read_anywhere) == sorted(cv for cv in CURATED if cv <= MAX_CV_DIRECT)
    # Service mode reads carry no locomotive address - the address names the
    # file, not the read target.
    assert fake.batch_calls[0]["address"] is None
