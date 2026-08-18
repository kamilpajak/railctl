"""`railctl restore`: the wired command, all three formats, and M10's acceptance sentences.

The fake answers the facade surface restore touches (`cv_read` singletons,
`cv_read_many` for the live pass, `cv_write`, `version`, `capabilities`) and
records every call, so each test is about the CLI contract - the gate, the
plan, the stage order, the exit code - and never about wire bytes. It models
the one decoder behaviour verification exists for: `ignore_writes` makes a CV
accept the telegram and keep its old value, which is exactly what an
unverified write looks like from up here.

Nothing in this file touches hardware, and the layout it describes is a fake
one: every "the decoder does X" below is a statement about the fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from railctl.backup import BackupDocument, CvRecord, ReadStatus, write_backup_to
from railctl.cli._meta import command_meta
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, SILENCE_GUIDANCE
from railctl.cli.commands.restore import (
    NO_ROLLBACK,
    RECOVERY,
    RESTORE_SCHEMA,
    SECONDS_PER_CV,
    SUPPRESSED_EVENT,
    WARNING_FILE_INCOMPLETE,
    WARNING_IDENTITY_DEGRADED,
    WARNING_IDENTITY_OVERRIDDEN,
    serial_token,
)
from railctl.cli.main import app
from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    LinkTimeout,
    ProtocolError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
    StationBusyError,
    TrackPowerError,
    TransportError,
)
from railctl.station import (
    INDEXED_CV_RANGE,
    PAGE_SELECTOR_CVS,
    Capabilities,
    CvEncoding,
    CvPage,
    CvReadOutcome,
    CvResult,
    ProgMode,
    Station,
)
from railctl.xbus.replies import StationVersion

runner = CliRunner()

SERVICE_CAPS = Capabilities(
    link_identity="serial:7010A0001194:3", pom_read=False, service_direct_cv=True
)

#: The reference decoder as the acceptance file records it: ZIMO (CV8 = 145),
#: an MS450 (CV250 = 6, so CV144 is the confirmation jingle and not a lock),
#: and the serial the bench measured - CV251=251, CV252=105, CV253=75 on three
#: consecutive backups (docs/probe-results.md, "CV251-253 answer after all").
MANUFACTURER_ID = 145
MS450_TYPE = 6
MX_TYPE = 217
SERIAL = [251, 105, 75]

#: What the fake decoder holds before a restore runs, unless a test says
#: otherwise. CV3 differs from the file on purpose: it is the one CV the
#: milestone's "a hand-changed CV is restored and verified" sentence is about.
LIVE_BEFORE = {3: 5}

#: What a CV nobody named answers.
DEFAULT_LIVE = 0

#: The CV31/CV32 index page the fixture file is taken on, and therefore the
#: one the decoder must already sit on for a restore to run at all.
FILE_PAGE = (0, 0)

#: A second page, used by the tests about the CVs above 256. It is not the
#: (0, 0) default, so a write that carried no page at all - or carried a
#: hard-coded default - cannot pass those assertions by coincidence.
INDEX_PAGE = (16, 1)

#: The curated CVs above 256 in the shipped catalog, every one of them
#: restorable and behind the CV31/CV32 index page. A restore that does not
#: pass a page cannot write a single one of them.
INDEXED_CURATED_CVS = (265, 266, 273, 274, 275, 276, 277, 287, 288, 313, 314, 395, 396, 397)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


# -- the file -----------------------------------------------------------------


def record(cv: int, value: int | None, *, name: str = "", status: ReadStatus = ReadStatus.OK):
    return CvRecord(cv=cv, name=name or f"cv{cv}", status=status, value=value)


#: A small curated set covering all four stages: A (3, 4, 5), B (28, 29),
#: C (1, 17, 18) and D (144).
def default_records() -> tuple[CvRecord, ...]:
    return (
        record(1, 3, name="primary_address"),
        record(3, 20, name="accel_rate"),
        record(4, 18, name="decel_rate"),
        record(5, 200, name="v_max"),
        record(17, 192, name="extended_address_high"),
        record(18, 40, name="extended_address_low"),
        record(28, 3, name="railcom_config"),
        record(29, 14, name="config_flags"),
        record(144, 0, name="confirm_jingle"),
    )


def backup_file(tmp_path: Path, **overrides: object) -> Path:
    """One well-formed `railctl/backup/v1` file, written through the real
    writer so every test starts from a file the real reader accepts."""
    fields: dict[str, object] = {
        "created_utc": "2026-08-13T09:15:00Z",
        "tool": "railctl 0.1.0",
        "note": None,
        "loco": {"address": 3, "kind": "short"},
        "catalog": {"family": "zimo-ms-mx", "schema": 1},
        "set_name": "curated",
        "mode": "service",
        "cv_encoding": "SERVICE_DIRECT",
        "page": FILE_PAGE,
        "speed_table_included": False,
        "sweep_range": None,
        "link": {
            "identity": "serial:7010A0001194:3",
            "protocol": "xpressnet",
            "protocol_version": "4.0",
            "command_station_id": 18,
        },
        "capabilities": {
            "pom_read": False,
            "pom_result_channel": None,
            "pom_echo_zero_based": None,
            "service_direct_cv": True,
            "service_ext_cv": None,
            "z21_cv_opcodes": None,
        },
        "decoder": {
            "manufacturer_id": MANUFACTURER_ID,
            "decoder_version": 5,
            "decoder_type": MS450_TYPE,
        },
        "cvs": default_records(),
    }
    fields.update(overrides)
    path = tmp_path / "loco-0003-curated.json"
    write_backup_to(BackupDocument(**fields), path)  # type: ignore[arg-type]
    return path


# -- the fake station ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Write:
    cv: int
    value: int
    mode: ProgMode
    verify: bool
    #: The index page the write named. `None` is what a caller that passed no
    #: `page=` looks like from here, and on a CV above 256 that is the bug
    #: `_require_page` below turns into the station's own refusal.
    page: CvPage | None = None


class FakeRestoreStation:
    """Answers the facade surface `restore` touches, and records the calls."""

    identity = "serial:7010A0001194:3"

    def __init__(
        self,
        *,
        values: dict[int, int] | None = None,
        capabilities: Capabilities | None = None,
        read_errors: dict[int, Exception] | None = None,
        batch_errors: dict[int, Exception] | None = None,
        write_errors: dict[int, Exception] | None = None,
        ignore_writes: dict[int, int] | None = None,
        interrupt_on_write: int | None = None,
        emit_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        self.capabilities = capabilities or SERVICE_CAPS
        self.values: dict[int, int] = {
            8: MANUFACTURER_ID,
            250: MS450_TYPE,
            251: SERIAL[0],
            252: SERIAL[1],
            253: SERIAL[2],
            31: 0,
            32: 0,
            144: 0,
            **LIVE_BEFORE,
            **(values or {}),
        }
        self.read_errors = read_errors or {}
        #: Failures for the LIVE PASS alone. `read_errors` fails every read of
        #: that CV, singleton and batch, which also fails the verification
        #: read-back; this one leaves the read-back working, so a test can be
        #: about a live value that did not answer and nothing else.
        self.batch_errors = batch_errors or {}
        self.write_errors = write_errors or {}
        #: CV -> how many writes to swallow before the value sticks. This is
        #: what an unverified write looks like from the CLI: the station
        #: accepts the telegram and the decoder keeps its old value.
        self.ignore_writes = dict(ignore_writes or {})
        self.interrupt_on_write = interrupt_on_write
        self.emit_events = emit_events or []
        self.on_event: object | None = None
        self.reads: list[int] = []
        #: Every singleton read as (cv, page), so a test can pin the page a
        #: verification read-back was made on and not only that it happened.
        self.read_pages: list[tuple[int, CvPage | None]] = []
        self.batches: list[list[int]] = []
        self.writes: list[Write] = []

    def _result(self, cv: int, mode: ProgMode, operation: str = "read") -> CvResult:
        return CvResult(
            cv=cv,
            value=self.values.get(cv, DEFAULT_LIVE),
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation=operation,  # type: ignore[arg-type]
            verified=None,
            elapsed=0.01,
        )

    def _require_page(self, cv: int, page: CvPage | None) -> None:
        """The station's own rule, mirrored so these tests can see it.

        `station/programming.py::_require_page` refuses an indexed CV that
        arrives with no page, having sent nothing. A fake that quietly
        accepted one would let every test about the CVs above 256 pass
        against a command that cannot write a single one of them on real
        hardware.
        """
        if cv in INDEXED_CV_RANGE and page is None:
            raise IndexPageRequiredError(f"CV{cv} is behind a ZIMO index page (CV31/CV32)", cv=cv)

    def cv_read(self, cv, *, address=None, mode=ProgMode.SERVICE, page=None):
        self.reads.append(cv)
        # Service READS ignore the page on this station and answer on the live
        # bank, which is why the real `service_read` never calls
        # `_require_page` - so this records the argument and refuses nothing.
        self.read_pages.append((cv, page))
        error = self.read_errors.get(cv)
        if error is not None:
            raise error
        return self._result(cv, mode)

    def cv_read_many(self, specs, *, address=None, mode=ProgMode.SERVICE, on_progress=None):
        self.batches.append([spec.cv for spec in specs])
        outcomes = []
        for spec in specs:
            error = self.read_errors.get(spec.cv) or self.batch_errors.get(spec.cv)
            if error is not None:
                outcomes.append(CvReadOutcome(spec=spec, result=None, error=error))
                continue
            outcomes.append(
                CvReadOutcome(spec=spec, result=self._result(spec.cv, mode), error=None)
            )
        return outcomes

    def cv_write(self, cv, value, *, address=None, mode=ProgMode.SERVICE, page=None, verify=True):
        if self.interrupt_on_write == cv:
            raise KeyboardInterrupt
        self._require_page(cv, page)
        self.writes.append(Write(cv=cv, value=value, mode=mode, verify=verify, page=page))
        error = self.write_errors.get(cv)
        if error is not None:
            raise error
        swallow = self.ignore_writes.get(cv, 0)
        if swallow:
            self.ignore_writes[cv] = swallow - 1
        else:
            self.values[cv] = value
        if not verify and self.on_event is not None:
            # The real `CvProgrammer.service_write` emits this for every
            # unverified write; the fake does too, because whether restore
            # publishes it is a test below.
            self.on_event(SUPPRESSED_EVENT, {"cv": cv, "value": value})
        return self._result(cv, mode, operation="write")

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def close(self) -> None:
        pass


def install(monkeypatch, fake: FakeRestoreStation) -> FakeRestoreStation:
    def fake_open(*_a, **kwargs):
        fake.on_event = kwargs.get("on_event")
        for name, payload in fake.emit_events:
            callback = kwargs.get("on_event")
            if callback is not None:
                callback(name, payload)
        return fake

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    return fake


def boom_open(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("the refusal must come before any port is touched")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))


def envelope(result) -> dict[str, object]:
    return json.loads(result.stderr.strip().splitlines()[-1])


def payload(result) -> dict[str, object]:
    return json.loads(result.stdout)["result"]


def ndjson_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.strip().splitlines()]


def published(code: int) -> bool:
    return code in command_meta("restore").exit_codes


def invoke(path: Path, *args: str, fmt: str = "json"):
    return runner.invoke(app, ["restore", str(path), "--format", fmt, *args])


def rows_by_cv(body: dict[str, object]) -> dict[int, dict[str, object]]:
    return {row["cv"]: row for row in body["cvs"]}  # type: ignore[index,union-attr]


# -- the metadata row ----------------------------------------------------------


def test_the_restore_row_publishes_its_safety_facts():
    meta = command_meta("restore")
    # The two fields an agent reads to decide whether this may run unattended.
    assert meta.mutates is True
    assert meta.confirms is True
    assert meta.schema == RESTORE_SCHEMA
    # 14 is the mismatch table and 15 a file value the catalog refuses; 16 is
    # absent because `restore` has no POM path that could reach it (D1).
    assert {2, 9, 14, 15, 17} <= set(meta.exit_codes)
    assert 16 not in meta.exit_codes


def test_the_track_option_publishes_prog_alone():
    # `main` is refused, so publishing it as an accepted value would be a
    # documented lie - the refusal names it instead.
    track = next(o for o in command_meta("restore").options if o.name == "--track")
    assert track.enum == ("prog",)


# -- M10 acceptance: the plan --------------------------------------------------


def test_a_dry_run_and_the_real_run_produce_the_same_plan(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    dry_station = install(monkeypatch, FakeRestoreStation())
    dry = invoke(path, "--dry-run")
    assert dry.exit_code == 0, dry.stderr
    install(monkeypatch, FakeRestoreStation())
    real = invoke(path, "--yes")
    assert real.exit_code == 0, real.stderr
    # One function builds both, so this is the whole property: same rows, same
    # order, same reasons.
    assert payload(dry)["cvs"] == payload(real)["cvs"]
    # And the dry run wrote nothing at all - not a CV, not a page selector.
    assert dry_station.writes == []


def test_a_changed_cv_is_restored_and_verified(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    body = payload(result)
    row = rows_by_cv(body)[3]
    assert (row["live_value"], row["new_value"], row["action"]) == (5, 20, "write")
    assert Write(cv=3, value=20, mode=ProgMode.SERVICE, verify=False, page=FILE_PAGE) in fake.writes
    assert (
        body["verified"]
        == body["written"]
        == len([r for r in body["cvs"] if r["action"] == "write"])
    )
    assert fake.values[3] == 20


def test_a_cv_the_decoder_already_holds_is_not_written(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={4: 18}))
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert rows_by_cv(payload(result))[4]["action"] == "unchanged"
    assert [write.cv for write in fake.writes if write.cv == 4] == []


def test_the_report_lists_the_address_cvs_as_skipped(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    rows = rows_by_cv(payload(result))
    assert [rows[cv]["action"] for cv in (1, 17, 18, 29)] == ["skip"] * 4
    # Skipped, and therefore never written - the whole point of the row.
    assert {write.cv for write in fake.writes}.isdisjoint({1, 17, 18, 29})
    assert "--with-address" in rows[1]["reason"]
    assert "--merge-cv29" in rows[29]["reason"]


def test_with_address_writes_the_address_cvs_last_in_stage_order(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", "--with-address")
    assert result.exit_code == 0, result.stderr
    # A ascending, then B (28, 29), then C in the fixed 17, 18, 1, then D.
    # CV144 already reads 0 so it is unchanged and never written.
    assert [write.cv for write in fake.writes] == [3, 4, 5, 28, 29, 17, 18, 1]


def test_a_forced_mismatch_exits_14_with_the_whole_table(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    # CV3 and CV5 swallow every write; CV4 takes its first one.
    fake = install(monkeypatch, FakeRestoreStation(ignore_writes={3: 99, 5: 99}))
    result = invoke(path, "--yes")
    assert result.exit_code == 14, result.stderr
    assert published(14)
    report = envelope(result)
    assert report["code"] == "cv_verify"
    details = report["details"]
    assert [row["cv"] for row in details["mismatches"]] == [3, 5]
    assert details["mismatches"][0] == {
        "cv": 3,
        "name": "accel_rate",
        "stage": "A",
        "intended": 20,
        "read": 5,
    }
    assert details["stage"] == "A"
    # One retry each, and no further loops: two writes for a CV that never took.
    assert [write.cv for write in fake.writes].count(3) == 2
    # And stage A's failure stopped the run - stage B was never written.
    assert 28 not in [write.cv for write in fake.writes]
    assert "Nothing was rolled back" in report["message"]
    assert "re-run" in report["hint"]


def test_a_write_that_takes_on_the_retry_is_verified_and_not_a_mismatch(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(ignore_writes={3: 1}))
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert [write.cv for write in fake.writes].count(3) == 2
    assert fake.values[3] == 20


# -- the CVs above 256 ---------------------------------------------------------


def indexed_records(*cvs: int) -> tuple[CvRecord, ...]:
    """One `ok` row per CV, each holding a value the fake decoder does not,
    so every one of them is planned as a write. The values stay inside the
    catalog's 0..255 for these CVs."""
    return tuple(record(cv, (cv % 200) + 1, name=f"indexed_{cv}") for cv in cvs)


def indexed_file(tmp_path: Path, *cvs: int) -> Path:
    return backup_file(tmp_path, page=INDEX_PAGE, cvs=indexed_records(*cvs))


def on_index_page(**kwargs: object) -> FakeRestoreStation:
    """A fake decoder already sitting on `INDEX_PAGE`, which is what
    precondition 5 requires before a restore of these CVs may run at all."""
    values = {31: INDEX_PAGE[0], 32: INDEX_PAGE[1], **(kwargs.pop("values", None) or {})}
    return FakeRestoreStation(values=values, **kwargs)  # type: ignore[arg-type]


def test_an_indexed_cv_is_written_on_the_page_the_file_names(monkeypatch, tmp_path):
    """The write carries the file's page, and it is the file's - not (0, 0),
    and not nothing. Without it the station refuses every CV above 256."""
    path = indexed_file(tmp_path, 265)
    fake = install(monkeypatch, on_index_page())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert [(w.cv, w.page) for w in fake.writes] == [(265, INDEX_PAGE)]


def test_the_verification_read_back_is_pinned_to_the_same_page_as_the_write(monkeypatch, tmp_path):
    """Service reads ignore the page today (they answer on the live bank),
    but a verification that is not pinned to the bank its write went to is
    only accidentally right - and issue #39 may make reads honour it."""
    path = indexed_file(tmp_path, 265)
    fake = install(monkeypatch, on_index_page())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert (265, INDEX_PAGE) in fake.read_pages


def test_every_curated_cv_above_256_is_written_in_one_run(monkeypatch, tmp_path):
    """The shipped catalog has fourteen restorable CVs above 256 and a bench
    backup carries a value for all of them. Before the page was passed, the
    first of them ended the run part-way through stage A - after the
    lower-numbered writes had gone out and before anything verified them."""
    path = indexed_file(tmp_path, *INDEXED_CURATED_CVS)
    fake = install(monkeypatch, on_index_page())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert [w.cv for w in fake.writes] == list(INDEXED_CURATED_CVS)
    body = payload(result)
    assert body["written"] == body["verified"] == len(INDEXED_CURATED_CVS)


def test_a_non_indexed_cv_carries_the_page_too_and_is_unaffected_by_it(monkeypatch, tmp_path):
    """`ensure_page` returns early below CV257, so the page is ignored there -
    which is why it is passed unconditionally rather than behind a branch on
    the CV number that would be one more thing to get wrong."""
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert {w.page for w in fake.writes} == {FILE_PAGE}


def test_a_dry_run_selects_no_page_and_writes_no_selector(monkeypatch, tmp_path):
    """A real restore now writes CV31/CV32 as part of selecting the page. A
    dry run must not: it only reads, and service reads never select."""

    class NoSelectionStation(FakeRestoreStation):
        def select_page(self, page, **kwargs):
            raise AssertionError("a dry run must select no page")

        def cv_write(self, cv, value, **kwargs):
            if cv in PAGE_SELECTOR_CVS:
                raise AssertionError(f"a dry run must not write CV{cv}")
            raise AssertionError(f"a dry run must not write CV{cv} either")

    path = indexed_file(tmp_path, *INDEXED_CURATED_CVS)
    fake = install(monkeypatch, NoSelectionStation(values={31: INDEX_PAGE[0], 32: INDEX_PAGE[1]}))
    result = invoke(path, "--dry-run")
    assert result.exit_code == 0, result.stderr
    assert payload(result)["counts"]["write"] == len(INDEXED_CURATED_CVS)
    assert fake.writes == []


# -- CV29 ----------------------------------------------------------------------


def test_merge_cv29_writes_the_masked_byte_and_verifies_against_it(monkeypatch, tmp_path):
    # File CV29 = 14 (bit 5 clear, short address); the decoder is on a long
    # address (bit 5 set) and must stay there.
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={29: 0b0010_1101}))
    result = invoke(path, "--yes", "--merge-cv29")
    assert result.exit_code == 0, result.stderr
    merged = 14 | 0b0010_0000
    assert rows_by_cv(payload(result))[29]["new_value"] == merged
    assert (
        Write(cv=29, value=merged, mode=ProgMode.SERVICE, verify=False, page=FILE_PAGE)
        in fake.writes
    )
    # Verification compared against the merged byte, not the file's 14. Written
    # exactly once: a read-back checked against the file value would have found
    # 46 where it wanted 14, retried, and then called the merge a mismatch.
    assert [write.cv for write in fake.writes].count(29) == 1
    assert fake.values[29] == merged


# -- the identity gate ---------------------------------------------------------


def test_a_manufacturer_mismatch_aborts_before_any_write(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={8: 99}))
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    assert published(9)
    report = envelope(result)
    assert report["code"] == "decoder_identity_mismatch"
    assert report["details"]["reason"] == "identity_mismatch"
    assert report["details"]["cv"] == 8
    assert fake.writes == []


def test_a_decoder_type_mismatch_aborts_too(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={250: 7}))
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    assert envelope(result)["details"]["cv"] == 250
    assert fake.writes == []


def test_the_hard_half_of_the_gate_is_not_overridable(monkeypatch, tmp_path):
    # Neither --yes (already passed above) nor --confirm reaches it: the
    # serial token answers the serial question and nothing else.
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(values={8: 99}))
    result = invoke(path, "--yes", "--confirm", serial_token(SERIAL))
    assert result.exit_code == 9, result.stderr
    assert envelope(result)["details"]["reason"] == "identity_mismatch"


def test_a_file_with_no_manufacturer_id_is_refused_rather_than_ungated(monkeypatch, tmp_path):
    path = backup_file(tmp_path, decoder={"decoder_type": MS450_TYPE})
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["details"] == {
        "reason": "identity_not_in_file",
        "field": "manufacturer_id",
        "cv": 8,
    }
    assert fake.writes == []


def test_a_serial_mismatch_names_the_token_that_would_confirm_it(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        decoder={
            "manufacturer_id": MANUFACTURER_ID,
            "decoder_type": MS450_TYPE,
            "serial_bytes": [1, 2, 3],
        },
    )
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["details"]["reason"] == "serial_mismatch"
    assert report["details"]["live"] == SERIAL
    assert report["details"]["confirm_token"] == "251.105.75"  # noqa: S105 - a serial, not a secret
    assert "--confirm=251.105.75" in report["hint"]
    assert fake.writes == []


def test_the_serial_confirmation_is_bound_to_the_serial_just_read(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        decoder={
            "manufacturer_id": MANUFACTURER_ID,
            "decoder_type": MS450_TYPE,
            "serial_bytes": [1, 2, 3],
        },
    )
    install(monkeypatch, FakeRestoreStation())
    # The FILE's serial is not the token: only the live one is.
    refused = invoke(path, "--yes", "--confirm", "1.2.3")
    assert refused.exit_code == 9, refused.stderr
    install(monkeypatch, FakeRestoreStation())
    accepted = invoke(path, "--yes", "--confirm", "251.105.75")
    assert accepted.exit_code == 0, accepted.stderr
    warnings = {w["name"] for w in json.loads(accepted.stdout)["warnings"]}
    assert WARNING_IDENTITY_OVERRIDDEN in warnings
    assert payload(accepted)["identity"]["serial_overridden"] is True


def test_a_matching_serial_is_checked_and_not_flagged(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        decoder={
            "manufacturer_id": MANUFACTURER_ID,
            "decoder_type": MS450_TYPE,
            "serial_bytes": SERIAL,
        },
    )
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    identity = payload(result)["identity"]
    assert identity["serial"] == SERIAL
    assert (identity["serial_checked"], identity["serial_overridden"]) == (True, False)
    assert json.loads(result.stdout)["warnings"] == []


def test_a_file_with_no_serial_degrades_to_a_warning_naming_what_matched(monkeypatch, tmp_path):
    path = backup_file(tmp_path)  # the default decoder block carries no serial
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    warning = next(
        w for w in json.loads(result.stdout)["warnings"] if w["name"] == WARNING_IDENTITY_DEGRADED
    )
    assert warning["details"] == {
        "manufacturer_id": MANUFACTURER_ID,
        "decoder_type": MS450_TYPE,
    }
    assert payload(result)["identity"]["serial_checked"] is False


def test_an_unreadable_identity_cv_aborts_with_the_placement_guidance(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(
        monkeypatch,
        FakeRestoreStation(
            read_errors={8: DecoderNotRespondingError("no answer after 3 attempts", cv=8)}
        ),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 13, result.stderr
    assert published(13)
    report = envelope(result)
    assert report["code"] == "decoder_not_responding"
    assert report["hint"] == SILENCE_GUIDANCE
    assert fake.writes == []


def test_the_gate_runs_on_a_dry_run_too(monkeypatch, tmp_path):
    # A dry run that skipped the gate would print a plan for the wrong
    # locomotive, which is worse than printing none.
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(values={8: 99}))
    result = invoke(path, "--dry-run")
    assert result.exit_code == 9, result.stderr
    assert envelope(result)["code"] == "decoder_identity_mismatch"


# -- the other preconditions ---------------------------------------------------


def test_a_page_the_file_was_not_taken_on_aborts_without_writing_the_selectors(
    monkeypatch, tmp_path
):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={31: 145, 32: 2}))
    result = invoke(path, "--yes")
    assert result.exit_code == 17, result.stderr
    assert published(17)
    report = envelope(result)
    assert report["code"] == "index_page_required"
    assert report["details"] == {"live": [145, 2], "file": [0, 0]}
    assert fake.writes == []


def test_cv144_is_a_lock_only_on_a_family_that_locks_on_it(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        decoder={"manufacturer_id": MANUFACTURER_ID, "decoder_type": MX_TYPE},
    )
    fake = install(monkeypatch, FakeRestoreStation(values={250: MX_TYPE, 144: 1}))
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["code"] == "programming_locked"
    assert report["details"] == {"cv": 144, "live": 1, "decoder_type": MX_TYPE}
    assert "railctl cv write 144 0" in report["hint"]
    assert fake.writes == []


def test_an_unlocked_mx_decoder_restores_without_re_reading_cv144(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        decoder={"manufacturer_id": MANUFACTURER_ID, "decoder_type": MX_TYPE},
    )
    fake = install(monkeypatch, FakeRestoreStation(values={250: MX_TYPE}))
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    # The lock check read CV144 as a singleton, so the live pass leaves it out
    # rather than paying 6 s to read the same CV twice.
    assert 144 not in fake.batches[0]
    assert fake.reads.count(144) == 1


def test_the_same_live_cv144_is_no_precondition_on_an_ms_decoder(monkeypatch, tmp_path):
    # Same non-zero CV144, MS family: it is the confirmation jingle, so it is
    # restored like any other CV rather than refused.
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation(values={144: 1}))
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    assert (
        Write(cv=144, value=0, mode=ProgMode.SERVICE, verify=False, page=FILE_PAGE) in fake.writes
    )


def test_a_value_the_catalog_refuses_aborts_before_any_write(monkeypatch, tmp_path):
    # CV56 takes 0..99 in the shipped catalog, and 200 is one file value the
    # decoder must never be asked to hold.
    path = backup_file(tmp_path, cvs=(record(56, 200, name="brake_distance"),))
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 15, result.stderr
    assert published(15)
    report = envelope(result)
    assert report["code"] == "cv_out_of_range"
    assert report["details"]["out_of_range"] == [{"cv": 56, "value": 200, "min": 0, "max": 99}]
    assert fake.writes == []


def test_with_address_needs_the_whole_address_set_in_the_file(monkeypatch, tmp_path):
    path = backup_file(tmp_path, cvs=(record(1, 3, name="primary_address"),))
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", "--with-address")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["code"] == "address_set_incomplete"
    assert report["details"]["missing"] == [17, 18, 29]
    assert fake.writes == []


def test_a_row_the_file_could_not_read_is_reported_and_never_written(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        cvs=(
            record(3, 20, name="accel_rate"),
            CvRecord(cv=4, name="decel_rate", status=ReadStatus.NO_RESPONSE, detail="no answer"),
        ),
    )
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", "--allow-incomplete")
    assert result.exit_code == 0, result.stderr
    assert rows_by_cv(payload(result))[4]["action"] == "unreadable"
    assert [write.cv for write in fake.writes] == [3]


# -- the refusals that cost no port --------------------------------------------


def test_track_main_is_refused_naming_both_reasons(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = backup_file(tmp_path)
    result = invoke(path, "--yes", "--track", "main")
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    report = envelope(result)
    assert report["code"] == "usage"
    assert report["details"]["reason"] == "restore_on_main_track"
    # Both reasons, in the message the operator reads.
    assert "identity gate" in report["message"]
    assert "verify" in report["message"]
    # The suggestion is the same command with the flag dropped - the default
    # track is the only one that runs.
    assert report["suggestions"] == [["railctl", "restore", str(path), "--format", "json", "--yes"]]


def test_an_unknown_track_word_is_a_usage_error(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = backup_file(tmp_path)
    result = invoke(path, "--track", "prg")
    assert result.exit_code == 2, result.stderr
    assert "--track must be one of" in envelope(result)["message"]


def test_with_address_and_merge_cv29_contradict_each_other(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = backup_file(tmp_path)
    result = invoke(path, "--with-address", "--merge-cv29", "--confirm", "1.2.3")
    assert result.exit_code == 2, result.stderr
    report = envelope(result)
    assert report["details"]["reason"] == "contradictory_cv29_flags"
    # One runnable argv per way out, each keeping everything else typed -
    # including the --confirm token, which neither flag has anything to do with.
    assert report["suggestions"] == [
        [
            "railctl",
            "restore",
            str(path),
            "--with-address",
            "--confirm",
            "1.2.3",
            "--format",
            "json",
        ],
        ["railctl", "restore", str(path), "--merge-cv29", "--confirm", "1.2.3", "--format", "json"],
    ]


def test_a_refusals_suggestion_carries_the_global_flags_typed_after_the_verb(monkeypatch, tmp_path):
    # No --format at all (so the human default stands), plus two globals typed
    # after the verb: a suggestion that dropped them would hand the operator a
    # command that produces different output from the one they ran.
    boom_open(monkeypatch)
    path = backup_file(tmp_path)
    result = runner.invoke(
        app, ["restore", str(path), "--track", "main", "--json", "--color", "never"]
    )
    assert result.exit_code == 2, result.stderr
    assert envelope(result)["suggestions"] == [
        ["railctl", "restore", str(path), "--json", "--color", "never"]
    ]


def test_a_missing_file_is_the_readers_own_error_before_any_port(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    result = invoke(tmp_path / "nothing-here.json")
    assert result.exit_code == 9, result.stderr
    assert envelope(result)["code"] == "backup_file"


def test_an_incomplete_file_is_refused_before_any_port(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = backup_file(
        tmp_path,
        cvs=(
            record(3, 20, name="accel_rate"),
            CvRecord(cv=4, name="decel_rate", status=ReadStatus.NO_RESPONSE, detail="no answer"),
        ),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["code"] == "restore_file_incomplete"
    assert report["details"]["no_response"] == 1
    assert "--allow-incomplete" in report["message"]


def test_allow_incomplete_runs_and_says_so(monkeypatch, tmp_path):
    path = backup_file(
        tmp_path,
        cvs=(
            record(3, 20, name="accel_rate"),
            CvRecord(cv=4, name="decel_rate", status=ReadStatus.NO_RESPONSE, detail="no answer"),
        ),
    )
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", "--allow-incomplete")
    assert result.exit_code == 0, result.stderr
    warning = next(
        w for w in json.loads(result.stdout)["warnings"] if w["name"] == WARNING_FILE_INCOMPLETE
    )
    assert warning["details"] == {"no_response": 1, "error": 0}


# -- confirmation --------------------------------------------------------------


def test_the_confirmation_names_the_file_the_loco_the_count_and_the_measured_cost(
    monkeypatch, tmp_path
):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path)
    assert result.exit_code == 2, result.stderr
    report = envelope(result)
    assert report["code"] == "confirmation_required"
    message = report["message"]
    assert str(path) in message
    assert "locomotive 3" in message
    assert f"{SECONDS_PER_CV} s per operation" in message
    assert "measured 2026-08-13" in message
    # The retry is the whole invocation with --yes appended, not a bare verb.
    assert report["suggestions"] == [["railctl", "restore", str(path), "--format", "json", "--yes"]]


def test_a_dry_run_asks_nothing(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--dry-run")
    assert result.exit_code == 0, result.stderr


# -- interruption --------------------------------------------------------------


def test_ctrl_c_mid_run_reports_what_was_written_and_rolls_nothing_back(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(interrupt_on_write=5))
    result = invoke(path, "--yes")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["code"] == "aborted"
    assert report["details"]["written"] == [3, 4]
    assert "nothing is rolled back" in report["message"]


# -- a station failure mid-run -------------------------------------------------


def test_a_write_that_fails_mid_stage_names_the_cvs_already_written(monkeypatch, tmp_path):
    """A station error out of `cv_write` used to propagate untouched: the
    operator of a half-written decoder got a one-CV verdict and learned
    nothing about the writes that had already gone out."""
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(write_errors={4: DecoderNoAckError("61 13", cv=4)}))
    result = invoke(path, "--yes")
    assert result.exit_code == 10, result.stderr
    report = envelope(result)
    # The station's verdict is still the verdict - only the details grew.
    assert report["code"] == "decoder_no_ack"
    assert report["details"]["written"] == [3]
    assert report["details"]["stage"] == "A"
    assert NO_ROLLBACK in report["message"]
    assert RECOVERY in report["hint"]


def test_a_verification_read_that_fails_names_what_was_written_and_verified(monkeypatch, tmp_path):
    """The other half of the same hole: the failure is in the read-back, so
    stage A's three writes are all out and only two of them are confirmed."""
    path = backup_file(tmp_path)
    install(
        monkeypatch,
        FakeRestoreStation(read_errors={5: DecoderNotRespondingError("no answer", cv=5)}),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 13, result.stderr
    report = envelope(result)
    assert report["code"] == "decoder_not_responding"
    assert (report["details"]["written"], report["details"]["verified"]) == ([3, 4, 5], [3, 4])
    # The station's own placement guidance keeps its place ahead of the
    # recovery: both are things the operator needs, and neither replaces
    # the other.
    assert report["hint"] == f"{SILENCE_GUIDANCE}; {RECOVERY}"


def test_a_failure_in_a_later_stage_names_the_stages_that_completed(monkeypatch, tmp_path):
    """Stage A wrote and verified; stage B is where it stopped. Without
    `stages_completed` a re-run's operator cannot tell those two apart."""
    path = backup_file(tmp_path)
    install(
        monkeypatch,
        FakeRestoreStation(write_errors={28: ShortCircuitError("short on the programming track")}),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 11, result.stderr
    details = envelope(result)["details"]
    assert (details["stages_completed"], details["stage"]) == (["A"], "B")
    assert details["verified"] == [3, 4, 5]


# -- the three renderings ------------------------------------------------------


def test_the_human_rendering_carries_the_same_facts_as_the_json_one(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", fmt="human")
    assert result.exit_code == 0, result.stderr
    assert "CV3 accel_rate: 5 -> 20 (stage A)" in result.stdout
    assert "CV1 primary_address: skip - " in result.stdout
    assert "written and verified: 4 of 4" in result.stdout
    # The programming-track notice belongs on stderr, never in the result.
    assert PROG_TRACK_NOTICE in result.stderr


def test_the_human_rendering_says_a_dry_run_wrote_nothing(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--dry-run", fmt="human")
    assert result.exit_code == 0, result.stderr
    assert "dry run: nothing was written" in result.stdout


def test_an_unread_live_value_is_written_and_says_why(monkeypatch, tmp_path):
    """Silence in the live pass is not `unchanged`. CV4 did not answer, so the
    planner cannot rule the write out as unnecessary - and the row says that
    rather than inventing a comparison against a value nobody read.

    `batch_errors` and not `read_errors`: this test is about a live value that
    did not answer, so the verification read-back has to keep working, or the
    run ends on the station's error and there is no rendered row to assert at
    all. That failing ending is a separate test, under F2's enrichment above.
    """
    path = backup_file(tmp_path)
    fake = install(
        monkeypatch,
        FakeRestoreStation(batch_errors={4: DecoderNotRespondingError("no answer", cv=4)}),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    row = rows_by_cv(payload(result))[4]
    assert (row["live_value"], row["action"], row["new_value"]) == (None, "write", 18)
    assert "did not read back" in row["reason"]
    assert 4 in [write.cv for write in fake.writes]


def test_the_json_envelope_carries_the_plan_the_counts_and_the_identity(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    body = payload(result)
    assert body["file"] == str(path)
    assert body["track"] == "prog"
    assert body["mode"] == "service"
    assert body["dry_run"] is False
    assert body["loco"] == {"address": 3, "kind": "short"}
    assert body["page"] == [0, 0]
    assert body["counts"] == {"write": 4, "unchanged": 1, "skip": 4, "unreadable": 0}
    assert body["planned"] == len(default_records())
    assert body["stages_completed"] == ["A", "B"]
    assert body["options"] == {
        "with_address": False,
        "merge_cv29": False,
        "include_sweep": False,
        "allow_incomplete": False,
    }
    assert {row["action"] for row in body["cvs"]} <= {"write", "unchanged", "skip", "unreadable"}


def test_the_ndjson_stream_starts_streams_and_ends_in_a_summary(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    lines = ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    start = lines[0]
    assert start["type"] == "start"
    assert start["schema"] == RESTORE_SCHEMA
    assert start["file"] == str(path)
    assert start["address"] == 3
    assert start["planned"] == len(default_records())
    assert start["writes"] == 4
    assert [line["cv"] for line in lines if line["type"] == "cv"] == [3, 4, 5, 28]
    stages = [line for line in lines if line["type"] == "stage"]
    assert [(s["stage"], s["written"], s["verified"]) for s in stages] == [("A", 3, 3), ("B", 1, 1)]
    assert all(s["mismatches"] == [] for s in stages)
    summary = lines[-1]
    assert summary["type"] == "summary"
    assert summary["exit_code"] == 0
    assert (summary["written"], summary["verified"], summary["mismatches"]) == (4, 4, 0)


def test_the_ndjson_stream_of_a_dry_run_carries_the_cvs_it_would_write(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--dry-run", fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    lines = ndjson_lines(result.stdout)
    assert lines[0]["dry_run"] is True
    assert [line["cv"] for line in lines if line["type"] == "cv"] == [3, 4, 5, 28]
    assert [line["type"] for line in lines if line["type"] == "stage"] == []
    assert lines[-1]["written"] == 0
    assert fake.writes == []


def test_the_ndjson_summary_is_last_even_on_a_mismatch(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(ignore_writes={3: 99}))
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 14, result.stderr
    lines = ndjson_lines(result.stdout)
    stage = next(line for line in lines if line["type"] == "stage")
    assert stage["mismatches"] == [{"cv": 3, "intended": 20, "read": 5}]
    summary = lines[-1]
    assert summary["type"] == "summary"
    assert summary["exit_code"] == 14
    assert summary["mismatches"] == 1
    assert envelope(result)["code"] == "cv_verify"


def test_the_ndjson_gate_refusal_still_owes_the_stream_its_summary(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(values={8: 99}))
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 9, result.stderr
    lines = ndjson_lines(result.stdout)
    # The station opened, so a summary is owed - and nothing was ever planned,
    # so it carries zeros. A consumer keys on `type`, never on position.
    assert [line["type"] for line in lines] == ["summary"]
    assert lines[0]["planned"] == 0
    assert lines[0]["exit_code"] == 9


def test_an_ndjson_usage_refusal_produces_no_stream_at_all(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = backup_file(tmp_path)
    result = invoke(path, "--track", "main", fmt="ndjson")
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    assert envelope(result)["code"] == "usage"


def test_an_ndjson_interrupt_before_the_station_opened_exits_9_quietly(monkeypatch, tmp_path):
    def interrupted_open(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(Station, "open", staticmethod(interrupted_open))
    path = backup_file(tmp_path)
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 9
    assert result.stdout == ""


def test_an_ndjson_interrupt_mid_run_still_ends_in_a_summary(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(interrupt_on_write=5))
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 9, result.stderr
    lines = ndjson_lines(result.stdout)
    assert lines[-1]["type"] == "summary"
    assert lines[-1]["written"] == 2
    assert envelope(result)["code"] == "aborted"


# -- station events -------------------------------------------------------------


def test_the_per_write_unverified_event_is_not_published_as_a_warning(monkeypatch, tmp_path):
    # Every write goes out with verify=False because the stage verifies them
    # together; publishing the station's per-write event would say nothing was
    # checked about writes that were all checked one stage later.
    path = backup_file(tmp_path)
    fake = install(monkeypatch, FakeRestoreStation())
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    # The calls the command actually made, never a literal built here: a
    # `Write` this test constructs proves nothing about the one it sent.
    assert fake.writes and all(write.verify is False for write in fake.writes)
    assert SUPPRESSED_EVENT not in {w["name"] for w in json.loads(result.stdout)["warnings"]}


def test_every_other_station_event_still_reaches_the_envelope(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(
        monkeypatch,
        FakeRestoreStation(emit_events=[("cv.stale_result", {"cv": 8, "echoed": 7})]),
    )
    result = invoke(path, "--yes")
    assert result.exit_code == 0, result.stderr
    warning = next(
        w for w in json.loads(result.stdout)["warnings"] if w["name"] == "cv.stale_result"
    )
    assert warning["details"] == {"cv": 8, "echoed": 7}


def test_the_ndjson_stream_carries_events_and_drops_the_same_one(monkeypatch, tmp_path):
    path = backup_file(tmp_path)
    install(
        monkeypatch,
        FakeRestoreStation(emit_events=[("cv.stale_result", {"cv": 8, "echoed": 7})]),
    )
    result = invoke(path, "--yes", fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    lines = ndjson_lines(result.stdout)
    events = [line for line in lines if line["type"] == "event"]
    assert [event["name"] for event in events] == ["cv.stale_result"]
    assert events[0]["details"] == {"cv": 8, "echoed": 7}


# -- reachability: every published exit code, driven ----------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransportError("the port vanished"), 3),
        (ProtocolError("unparseable telegram"), 4),
        (LinkTimeout("no reply"), 5),
        (DecoderNoAckError("61 13", cv=3), 10),
        (ShortCircuitError("short on the programming track"), 11),
        (StationBusyError("61 1F"), 12),
        (ServiceEncodingUnknownError("nothing probed yet"), 18),
        (TrackPowerError("track power is off"), 20),
    ],
    ids=lambda value: getattr(value, "__class__", type(value)).__name__,
)
def test_a_station_failure_mid_write_exits_with_a_code_restore_publishes(
    monkeypatch, tmp_path, error, expected
):
    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(write_errors={3: error}))
    result = invoke(path, "--yes")
    assert result.exit_code == expected, result.stderr
    assert published(expected)
    assert envelope(result)["exit_code"] == expected


def test_the_programming_base_code_is_reachable_too(monkeypatch, tmp_path):
    from railctl.errors import ProgrammingError

    path = backup_file(tmp_path)
    install(monkeypatch, FakeRestoreStation(write_errors={3: ProgrammingError("something else")}))
    result = invoke(path, "--yes")
    assert result.exit_code == 19, result.stderr
    assert published(19)
