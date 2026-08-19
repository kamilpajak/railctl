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

import itertools
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import railctl.cli.commands.backup as backup_module
from railctl.backup import BACKUP_SCHEMA, read_backup
from railctl.catalog import curated_cvs, load_catalog
from railctl.cli._meta import command_meta
from railctl.cli.commands._sweep import (
    HIGHEST_EXERCISED_CV,
    SWEEP_ESTIMATE_AFTER,
    SWEEP_PROGRESS_EVERY,
)
from railctl.cli.commands.backup import SET_NAME
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, SILENCE_GUIDANCE
from railctl.cli.main import app
from railctl.errors import (
    CvOutOfRangeError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
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
from railctl.xbus.cv import MAX_CV_DIRECT, MAX_CV_EXT
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
NO_EXT_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3",
    pom_read=False,
    service_direct_cv=True,
    service_ext_cv=False,
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
        batch_raises: Exception | None = None,
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
        #: Raise this from `cv_read_many` before any batch read - the shape a
        #: station-level failure (a short, a busy station) takes mid-run.
        self.batch_raises = batch_raises
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
        if self.batch_raises is not None:
            raise self.batch_raises
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
    # A backup never writes the decoder - the CV31/CV32 selectors included.
    # It does confirm: `--all` is always over the 60 s gate, so an agent that
    # reads `confirms: false` here and runs a sweep unattended gets exit 2
    # instead of a file. These are the two fields it reads to decide.
    assert meta.mutates is False
    assert meta.confirms is True
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
    # SERVICE_CAPS never probed service_ext_cv, so the skip reason claims no
    # absence - only that nothing was measured.
    assert (
        "CV397 volume_up_key: skipped (cv 397 > MAX_CV_DIRECT 255; extended opcodes "
        "not probed (run railctl doctor))"
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
    # as-is, never a sentence to parse - typed global flags included.
    assert envelope["suggestions"] == [
        ["railctl", "backup", "--address", "3", "--out", str(out), "--format", "json", "--force"]
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
            "--format",
            "json",
            "--force",
        ]
    ]


def test_backup_refusal_suggestion_keeps_typed_global_flags(monkeypatch, tmp_path):
    # The suggestion is "the full argv as typed" - the typed globals ride
    # along after backup's own options, in registration order; globals the
    # operator never typed do not appear.
    _boom_open(monkeypatch)
    out = tmp_path / "exists.json"
    out.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "backup",
            "--address",
            "3",
            "--out",
            str(out),
            "--format",
            "json",
            "--target",
            "auto",
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.stderr
    assert _stderr_envelope(result)["suggestions"] == [
        [
            "railctl",
            "backup",
            "--address",
            "3",
            "--out",
            str(out),
            "--target",
            "auto",
            "--format",
            "json",
            "--yes",
            "--force",
        ]
    ]


def test_backup_refusal_suggestion_keeps_the_remaining_typed_globals(monkeypatch, tmp_path):
    _boom_open(monkeypatch)
    out = tmp_path / "exists.json"
    out.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "backup",
            "--address",
            "3",
            "--out",
            str(out),
            "--json",
            "-vv",
            "--color",
            "never",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 2, result.stderr
    assert _stderr_envelope(result)["suggestions"] == [
        [
            "railctl",
            "backup",
            "--address",
            "3",
            "--out",
            str(out),
            "--json",
            "--verbose",
            "--verbose",
            "--color",
            "never",
            "--non-interactive",
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


def test_backup_on_the_zimo_resting_bank_needs_no_acknowledgement(monkeypatch, tmp_path):
    """CV31=0 CV32=1 is where the reference MS450P22 rests, and it will not
    accept CV32=0 (measured 2026-08-13, the write read back as 1). Treating
    only 0:0 as neutral aborted every backup of a decoder in its normal
    state, so the resting bank runs straight through and is recorded."""
    out = tmp_path / "resting.json"
    _install(monkeypatch, FakeBackupStation(read_values={31: 0, 32: 1}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(out.read_text())["page"] == [0, 1]


def test_backup_on_a_real_cv_page_still_aborts_and_names_the_neutral_banks(monkeypatch, tmp_path):
    """A decoder parked on a genuine CV page - 145:2 holds the audio filters -
    is the case the refusal exists for: the same CV numbers mean something the
    catalog does not name there."""
    out = tmp_path / "never.json"
    _install(monkeypatch, FakeBackupStation(read_values={31: 145, 32: 2}))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out), "--format", "json"])
    assert result.exit_code == 17, result.stderr
    envelope = _stderr_envelope(result)
    assert "--page 145:2" in envelope["message"]
    assert "0:0, 0:1" in envelope["message"]
    assert envelope["details"]["neutral_pages"] == [[0, 0], [0, 1]]
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
    summary says `complete: false`, the process exits 9 - and the document is
    still DELIVERED, on disk and in the envelope, because the file is the
    product and the exit code is its honest label."""
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
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["exit_code"] == 9
    warning = next(w for w in payload["warnings"] if w["name"] == "backup.incomplete")
    assert "CV250" in warning["message"] and "CV253" in warning["message"]
    assert warning["details"]["path"] == str(out)
    assert warning["details"]["no_response"] == [250, 253]
    assert warning["details"]["error"] == []
    assert warning["details"]["skipped"] == sorted(OVER_BOUND)
    # The envelope result IS the written document, plus where it went.
    body = payload["result"]
    assert body["path"] == str(out)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert {key: value for key, value in body.items() if key != "path"} == on_disk
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
    payload = json.loads(result.stdout)
    warning = next(w for w in payload["warnings"] if w["name"] == "backup.incomplete")
    assert warning["details"]["error"] == [65]
    row = next(r for r in json.loads(out.read_text())["cvs"] if r["cv"] == 65)
    assert row["status"] == "error"


def test_backup_incomplete_to_stdout_still_delivers_the_document(monkeypatch):
    # `--out -` used to lose the measured data entirely on an incomplete run:
    # the raise threw the outcome away and only an error envelope survived.
    _install(
        monkeypatch,
        FakeBackupStation(read_errors={253: DecoderNotRespondingError("no answer", cv=253)}),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 9, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    body = payload["result"]
    assert body["schema"] == BACKUP_SCHEMA
    assert body["path"] is None
    assert body["summary"]["complete"] is False
    warning = next(w for w in payload["warnings"] if w["name"] == "backup.incomplete")
    assert warning["details"]["path"] is None


def test_backup_incomplete_human_still_prints_every_row(monkeypatch, tmp_path):
    out = tmp_path / "human9.json"
    _install(
        monkeypatch,
        FakeBackupStation(
            read_errors={253: DecoderNotRespondingError("no answer after 3 attempts", cv=253)}
        ),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out)])
    assert result.exit_code == 9, result.stderr
    assert f"CV8 manufacturer_id = {DEFAULT_VALUE}" in result.stdout
    assert "CV253 serial_byte_3: no_response (no answer after 3 attempts)" in result.stdout
    assert "complete: no" in result.stdout
    assert f"written to {out}" in result.stdout
    assert "backup.incomplete" in result.stdout


def test_backup_station_events_still_reach_an_incomplete_envelope(monkeypatch):
    _install(
        monkeypatch,
        FakeBackupStation(
            read_errors={253: DecoderNotRespondingError("no answer", cv=253)},
            emit_events=[("cv.stale_result", {"cv": 8, "echoed": 7})],
        ),
    )
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 9, result.stderr
    names = [w["name"] for w in json.loads(result.stdout)["warnings"]]
    assert "cv.stale_result" in names
    assert "backup.incomplete" in names


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


def _skip_detail_for_397(result) -> str:
    row = next(r for r in json.loads(result.stdout)["result"]["cvs"] if r["cv"] == 397)
    assert row["status"] == "skipped"
    return row["detail"]


def test_backup_bound_detail_names_the_measured_no(monkeypatch):
    # Service mode with service_ext_cv a MEASURED false: the one case
    # entitled to record the opcodes as unavailable.
    _install(monkeypatch, FakeBackupStation(capabilities=NO_EXT_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert (
        _skip_detail_for_397(result) == "cv 397 > MAX_CV_DIRECT 255; extended opcodes unavailable"
    )


def test_backup_bound_detail_says_not_probed_when_nothing_measured(monkeypatch):
    # service_ext_cv is None: nobody measured an absence, so none is claimed.
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert (
        _skip_detail_for_397(result)
        == "cv 397 > MAX_CV_DIRECT 255; extended opcodes not probed (run railctl doctor)"
    )


def test_backup_bound_detail_names_the_page_write_a_pom_backup_never_does(monkeypatch):
    # A POM run: the bound is about the CV31/CV32 page write a backup
    # refuses, not about extended opcodes at all.
    _install(monkeypatch, FakeBackupStation(capabilities=POM_YES_CAPS))
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert _skip_detail_for_397(result) == (
        "cv 397 > MAX_CV_DIRECT 255; indexed CVs need a CV31/CV32 page write, "
        "which a backup never performs"
    )


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


def test_backup_ndjson_no_response_line_carries_attempts_and_the_file_row_does_not(
    monkeypatch, tmp_path
):
    # The design's no_response example line carries "attempts": 3, read off
    # the error's own details; a silence that recorded no count emits no key,
    # and the file row never carries one either way.
    out = tmp_path / "attempts.json"
    _install(
        monkeypatch,
        FakeBackupStation(
            read_errors={
                253: DecoderNotRespondingError(
                    "no answer after 3 attempts", cv=253, details={"attempts": 3}
                ),
                250: DecoderNotRespondingError("no answer", cv=250),
            }
        ),
    )
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 9, result.stderr
    lines = _ndjson_lines(result.stdout)
    counted = next(line for line in lines if line["type"] == "cv" and line["cv"] == 253)
    assert counted["status"] == "no_response"
    assert counted["attempts"] == 3
    uncounted = next(line for line in lines if line["type"] == "cv" and line["cv"] == 250)
    assert "attempts" not in uncounted
    row = next(r for r in json.loads(out.read_text(encoding="utf-8"))["cvs"] if r["cv"] == 253)
    assert "attempts" not in row


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


def test_backup_ndjson_mode_refusal_still_owes_the_stream_its_summary(monkeypatch, tmp_path):
    # The station opened, so the stream exists and must be closed off - with
    # zero counts, because nothing was measured.
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "refused.json"
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", str(out), "--mode", "pom", "--format", "ndjson"],
    )
    assert result.exit_code == 16, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert len(lines) == 1
    assert lines[0]["type"] == "summary"
    assert lines[0]["requested"] == 0
    assert lines[0]["complete"] is False
    assert lines[0]["exit_code"] == 16
    assert _stderr_envelope(result)["code"] == "pom_read_unsupported"


def test_backup_ndjson_page_refusal_summarises_what_little_was_asked(monkeypatch, tmp_path):
    # The abort comes before CV29, so no start line was ever owed - but the
    # summary still is, and it reports the counts as they stood.
    _install(monkeypatch, FakeBackupStation(read_values={31: 145}))
    out = tmp_path / "page.json"
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 17, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["type"] for line in lines] == ["summary"]
    assert lines[0]["exit_code"] == 17
    assert _stderr_envelope(result)["code"] == "index_page_required"


def test_backup_ndjson_with_a_stdout_target_is_refused_before_anything_opens(monkeypatch):
    # The stream owns stdout, so `--out -` would deliver the document
    # NOWHERE. Refused before the station opens: no stream, no port cost,
    # and both remedies arrive as runnable argvs - drop `--out -` (keep the
    # stream, write the file), or keep stdout and switch to `--format json`.
    _boom_open(monkeypatch)
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", "-", "--format", "ndjson", "--yes"]
    )
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "usage"
    assert envelope["suggestions"] == [
        ["railctl", "backup", "--address", "3", "--format", "ndjson", "--yes"],
        ["railctl", "backup", "--address", "3", "--out", "-", "--yes", "--format", "json"],
    ]


def test_backup_ndjson_stdout_refusal_with_env_format_appends_format_json(monkeypatch):
    # ndjson picked by RAILCTL_FORMAT, not typed: nothing to strip from the
    # typed globals, and `--format json` (which outranks the variable) is
    # simply appended to the second remedy.
    _boom_open(monkeypatch)
    monkeypatch.setenv("RAILCTL_FORMAT", "ndjson")
    result = runner.invoke(app, ["backup", "--address", "3", "--out", "-"])
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    assert _stderr_envelope(result)["suggestions"] == [
        ["railctl", "backup", "--address", "3"],
        ["railctl", "backup", "--address", "3", "--out", "-", "--format", "json"],
    ]


def test_backup_ndjson_stdout_refusal_comes_before_the_missing_address(monkeypatch):
    # The combination is refused before the address is even resolved, so the
    # suggestions simply omit `--address` rather than inventing one.
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--out", "-", "--format", "ndjson"])
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    assert _stderr_envelope(result)["suggestions"] == [
        ["railctl", "backup", "--format", "ndjson"],
        ["railctl", "backup", "--out", "-", "--format", "json"],
    ]


def test_backup_ndjson_a_mid_batch_station_failure_counts_what_was_measured(monkeypatch, tmp_path):
    # A short mid-batch aborts before any document is assembled: the summary
    # still reports the counts as they stood - the three singletons and the
    # over-bound skips - against the full curated `requested`.
    _install(
        monkeypatch,
        FakeBackupStation(batch_raises=ShortCircuitError("short on the programming track")),
    )
    result = runner.invoke(
        app,
        ["backup", "--address", "3", "--out", str(tmp_path / "short.json"), "--format", "ndjson"],
    )
    assert result.exit_code == 11, result.stderr
    assert _stderr_envelope(result)["code"] == "short_circuit"
    summary = _ndjson_lines(result.stdout)[-1]
    assert summary["type"] == "summary"
    assert summary["requested"] == len(CURATED)
    assert summary["ok"] == 3
    assert summary["skipped"] == len(OVER_BOUND)
    assert summary["complete"] is False
    assert summary["exit_code"] == 11


def test_backup_ndjson_usage_refusal_produces_no_stream_at_all(monkeypatch):
    _boom_open(monkeypatch)
    result = runner.invoke(app, ["backup", "--address", "3", "--mode", "xml", "--format", "ndjson"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert _stderr_envelope(result)["code"] == "usage"


def test_backup_ndjson_interrupt_before_the_station_opened_exits_9_quietly(monkeypatch, tmp_path):
    def interrupted_open(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(Station, "open", staticmethod(interrupted_open))
    out = tmp_path / "never.json"
    result = runner.invoke(
        app, ["backup", "--address", "3", "--out", str(out), "--format", "ndjson"]
    )
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
            str(tmp_path / "events.json"),
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


# -- M11: the --all sweep ------------------------------------------------------
#
# The fake answers every CV, so a sweep over it is complete and exits 0. That is
# not what the bench does - most CV numbers answer nothing and the run exits 9,
# which the holes tests above already pin - but it is what makes the range, the
# names, the sources and the determinism assertable without a decoder.


def _sweep_argv(*extra: str, out: str | None = None) -> list[str]:
    argv = ["backup", "--address", "3", "--all"]
    if out is not None:
        argv += ["--out", out]
    return [*argv, *extra]


def _asked_cvs(fake: FakeBackupStation) -> list[int]:
    return sorted(
        [call["cv"] for call in fake.singleton_calls]
        + [spec.cv for call in fake.batch_calls for spec in call["specs"]]
    )


def _fake_clock(monkeypatch, step: float = 10.0) -> None:
    """A monotonic clock that advances `step` seconds per reading.

    The reporter reads it once when it is built and once per line it writes,
    so the tenth CV is the second reading: 10 s for 10 CVs, exactly 1.00 s
    per CV, and the revised estimate is a literal a test can pin.
    """
    ticks = itertools.count(0.0, step)
    monkeypatch.setattr(backup_module, "monotonic_seconds", lambda: next(ticks))


def test_backup_all_reads_every_cv_inside_the_bound_and_no_more(monkeypatch, tmp_path):
    # SERVICE_CAPS proves the direct encoding alone, so the sweep stops at 255.
    fake = _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "sweep.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    assert _asked_cvs(fake) == list(range(1, MAX_CV_DIRECT + 1))
    # The selectors are singletons before the plan; `cv_read_many` refuses them
    # in a payload, so they must never reach one.
    for call in fake.batch_calls:
        assert all(spec.cv not in (31, 32) for spec in call["specs"])
    body = json.loads(result.stdout)["result"]
    assert body["summary"]["requested"] == MAX_CV_DIRECT
    assert body["summary"]["ok"] == MAX_CV_DIRECT


def test_backup_all_records_the_set_the_range_and_both_sources(monkeypatch, tmp_path):
    # EXT_CAPS proves the extended encoding, so the sweep reaches 1024 and
    # covers CV617 - a number no catalog entry names.
    _install(monkeypatch, FakeBackupStation(capabilities=EXT_CAPS))
    out = tmp_path / "wide.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["set"] == "all"
    assert on_disk["sweep_range"] == [1, MAX_CV_EXT]
    rows = {row["cv"]: row for row in on_disk["cvs"]}
    assert rows[8]["name"] == "manufacturer_id"
    assert rows[8]["source"] == "catalog"
    assert rows[617]["name"] == "cv0617"
    assert rows[617]["source"] == "sweep"
    assert rows[1024]["name"] == "cv1024"
    # A fact about the RANGE, not about CV29 bit 4: the sweep covers CV67..94
    # whatever the speed-table bit says, and CV29 answered 0 here.
    assert on_disk["speed_table_included"] is True
    # The strict reader takes the file as written.
    assert read_backup(out).sweep_range == (1, MAX_CV_EXT)


def test_backup_all_writes_its_own_default_file_beside_a_curated_one(monkeypatch, tmp_path):
    backups = tmp_path / "home" / "railctl-backups"
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    curated = runner.invoke(app, ["backup", "--address", "3", "--format", "json"])
    assert curated.exit_code == 0, curated.stderr
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    swept = runner.invoke(app, _sweep_argv("--format", "json", "--yes"))
    assert swept.exit_code == 0, swept.stderr
    assert json.loads(swept.stdout)["result"]["path"] == str(backups / "loco-0003-all.json")
    # Two files, not one overwritten: a sweep can never clobber a curated backup.
    assert sorted(p.name for p in backups.iterdir()) == [
        "loco-0003-all.json",
        "loco-0003-curated.json",
    ]


def test_two_sweeps_of_an_unchanged_decoder_are_byte_identical(monkeypatch, tmp_path):
    _freeze_clock(monkeypatch)
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "twice-all.json"
    first = runner.invoke(app, _sweep_argv("--yes", out=str(out)))
    assert first.exit_code == 0, first.stderr
    first_bytes = out.read_bytes()
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    second = runner.invoke(app, _sweep_argv("--yes", "--force", out=str(out)))
    assert second.exit_code == 0, second.stderr
    assert out.read_bytes() == first_bytes


# -- the confirmation ----------------------------------------------------------


def test_a_sweep_past_the_minute_is_refused_on_a_non_interactive_stdin(monkeypatch, tmp_path):
    fake = _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "asked.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", out=str(out)))
    assert result.exit_code == 2, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    # The question names the count and the duration a person is agreeing to.
    assert f"{MAX_CV_DIRECT} CV" in envelope["message"]
    assert "10 min" in envelope["message"]
    # The retry is the whole invocation with --yes appended, and it runs.
    assert envelope["suggestions"] == [
        [
            "railctl",
            "backup",
            "--address",
            "3",
            "--out",
            str(out),
            "--all",
            "--format",
            "json",
            "--yes",
        ]
    ]
    # Refused before a single CV was read, and nothing was written.
    assert fake.singleton_calls == []
    assert not out.exists()


def test_the_refused_sweep_keeps_the_force_the_operator_typed(monkeypatch, tmp_path):
    # `--force` is why the run got past the overwrite check at all, so a retry
    # that drops it is refused by that same check instead of sweeping. The
    # published suggestion has to be a command that RUNS.
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "already-there.json"
    out.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, _sweep_argv("--force", "--format", "json", out=str(out)))
    assert result.exit_code == 2, result.stderr
    envelope = _stderr_envelope(result)
    assert envelope["code"] == "confirmation_required"
    suggestion = envelope["suggestions"][0]
    assert suggestion == [
        "railctl",
        "backup",
        "--address",
        "3",
        "--out",
        str(out),
        "--force",
        "--all",
        "--format",
        "json",
        "--yes",
    ]
    # Replayed verbatim it sweeps and replaces the file, rather than exiting 2
    # with `backup_file_exists`.
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    replay = runner.invoke(app, suggestion[1:])
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["set"] == "all"


def test_a_sweep_past_the_minute_runs_with_yes(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "agreed.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    assert out.exists()


def test_a_sweep_under_the_minute_never_asks(monkeypatch, tmp_path):
    # 20 CVs at the measured 2.4 s is 48 s - under the threshold, so no
    # question is owed and a non-interactive stdin is no obstacle.
    monkeypatch.setattr(backup_module, "sweep_bound", lambda mode, capabilities: 20)
    fake = _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "short.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", out=str(out)))
    assert result.exit_code == 0, result.stderr
    # The selectors and CV29 are singletons the run order reads whatever the
    # bound is - a real bound is 255 or 1024 and always contains them.
    assert _asked_cvs(fake) == sorted({*range(1, 21), 29, 31, 32})
    assert json.loads(out.read_text(encoding="utf-8"))["sweep_range"] == [1, 20]


# -- the revised estimate and the progress lines -------------------------------


def test_the_revised_estimate_lands_on_stderr_after_exactly_ten_cvs(monkeypatch, tmp_path):
    _fake_clock(monkeypatch)
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "revised.json"
    result = runner.invoke(app, _sweep_argv("--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    revisions = [line for line in result.stderr.splitlines() if "revised estimate" in line]
    # Once, never again: the operator already agreed, and a second question
    # mid-run on a non-interactive stream is a hang.
    assert len(revisions) == 1
    assert revisions[0] == (
        f"sweep: revised estimate after {SWEEP_ESTIMATE_AFTER} CVs - 1.00 s per CV, "
        f"4 min for all {MAX_CV_DIRECT} CVs, about 4 min left"
    )


def test_the_sweep_reports_progress_on_stderr_and_never_on_stdout(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "progress.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    progress = [line for line in result.stderr.splitlines() if line.startswith("sweep: ")]
    assert f"sweep: {SWEEP_PROGRESS_EVERY} of {MAX_CV_DIRECT} CVs" in progress[1]
    # One line per 32 CVs plus the closing one, and the closing one is last.
    assert progress[-1].startswith(f"sweep: {MAX_CV_DIRECT} of {MAX_CV_DIRECT} CVs read in ")
    # stdout in json mode holds exactly one JSON value - nothing from the
    # progress path leaked into it.
    payload = json.loads(result.stdout)
    assert payload["result"]["set"] == "all"
    assert "sweep:" not in result.stdout


def test_the_ndjson_sweep_carries_the_estimate_as_an_event_and_no_progress_lines(
    monkeypatch, tmp_path
):
    _fake_clock(monkeypatch)
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "streamed-sweep.json"
    result = runner.invoke(app, _sweep_argv("--format", "ndjson", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    lines = _ndjson_lines(result.stdout)
    # The extra event lines do not break the stream's two invariants.
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    assert lines[-1]["type"] == "summary"
    assert lines[0]["total"] == MAX_CV_DIRECT
    estimate = next(line for line in lines if line.get("name") == "sweep.estimate")
    assert estimate["type"] == "event"
    assert estimate["details"] == {
        "observed": SWEEP_ESTIMATE_AFTER,
        "total": MAX_CV_DIRECT,
        "seconds_per_cv": 1.0,
        "remaining_seconds": 245,
    }
    # The stream already carries one line per CV on stdout; repeating that on
    # stderr would be noise. The estimate itself still goes to stderr.
    assert "revised estimate" in result.stderr
    assert f"of {MAX_CV_DIRECT} CVs, about" not in result.stderr
    assert "CVs read in" not in result.stderr


# -- the unexercised range -----------------------------------------------------


def test_the_unexercised_range_is_named_once_when_the_sweep_passes_cv511(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(capabilities=EXT_CAPS))
    out = tmp_path / "unexercised.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    warning = next(
        w for w in json.loads(result.stdout)["warnings"] if w["name"] == "sweep.unexercised_range"
    )
    assert warning["details"]["from"] == HIGHEST_EXERCISED_CV + 1
    assert warning["details"]["to"] == MAX_CV_EXT
    # The claim is about the evidence, not about the hardware.
    assert "not corroborated" in warning["details"]["reason"]
    assert "does not mean those CVs do not work" in warning["details"]["reason"]
    said = [line for line in result.stderr.splitlines() if "has ever been answered" in line]
    assert len(said) == 1


def test_the_unexercised_range_is_silent_when_the_sweep_stops_at_255(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS))
    out = tmp_path / "inside.json"
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    names = [w["name"] for w in json.loads(result.stdout)["warnings"]]
    assert "sweep.unexercised_range" not in names
    assert "has ever been answered" not in result.stderr


def test_the_unexercised_range_rides_the_ndjson_stream_as_an_event(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation(capabilities=EXT_CAPS))
    out = tmp_path / "unexercised-stream.json"
    result = runner.invoke(app, _sweep_argv("--format", "ndjson", "--yes", out=str(out)))
    assert result.exit_code == 0, result.stderr
    lines = _ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    event = next(line for line in lines if line.get("name") == "sweep.unexercised_range")
    assert event["details"]["from"] == HIGHEST_EXERCISED_CV + 1
    # Before the reads start: no CV line precedes it.
    assert event["sequence"] < min(line["sequence"] for line in lines if line["type"] == "cv")


# -- the exit code and the human summary ---------------------------------------


def test_a_sweep_with_silent_cvs_exits_9_like_any_other_hole(monkeypatch, tmp_path):
    # The bench case: most CV numbers are not implemented in any decoder, and
    # this hardware cannot tell that from silence, so the sweep ends at 9 and
    # the file is still the product.
    silent = {cv: DecoderNotRespondingError("no answer after 3 attempts", cv=cv) for cv in (60, 61)}
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS, read_errors=silent))
    out = tmp_path / "holes-all.json"
    result = runner.invoke(app, _sweep_argv("--yes", out=str(out)))
    assert result.exit_code == 9, result.stderr
    assert "a sweep normally exits 9" in result.stdout
    assert "the file is the product either way" in result.stdout
    assert read_backup(out).summary["no_response"] == 2


def test_the_curated_human_summary_says_nothing_about_sweeps(monkeypatch, tmp_path):
    _install(monkeypatch, FakeBackupStation())
    out = tmp_path / "curated-note.json"
    result = runner.invoke(app, ["backup", "--address", "3", "--out", str(out)])
    assert result.exit_code == 0, result.stderr
    assert "sweep" not in result.stdout


# -- Ctrl-C mid-sweep ----------------------------------------------------------


def test_ctrl_c_mid_sweep_leaves_a_partial_file_answering_for_the_whole_range(
    monkeypatch, tmp_path
):
    out = tmp_path / "partial-all.json"
    _install(monkeypatch, FakeBackupStation(capabilities=SERVICE_CAPS, interrupt_after=8))
    result = runner.invoke(app, _sweep_argv("--format", "json", "--yes", out=str(out)))
    assert result.exit_code == 9, result.stderr
    assert _stderr_envelope(result)["code"] == "aborted"
    document = read_backup(out)
    assert document.interrupted is True
    assert document.summary["requested"] == MAX_CV_DIRECT
    assert document.sweep_range == (1, MAX_CV_DIRECT)
    unreached = next(record for record in document.cvs if record.cv == 200)
    assert unreached.status.value == "skipped"
    assert unreached.detail == "not attempted"
    # An unnamed CV the sweep never reached is still marked as the sweep's own.
    assert unreached.name == "cv0200"
    assert unreached.source == "sweep"
