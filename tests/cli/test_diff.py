"""`railctl diff`: both forms, all three renderings, and the two promises the
command is judged on - the offline form opens no link, and neither form writes.

The fake answers the facade surface `diff` touches (`cv_read` singletons for
the CV31/CV32 selectors, `cv_read_many` for the live pass, `version`,
`capabilities`) and records every call, so each test is about the CLI contract
and never about wire bytes. `cv_write` is present only so a test can prove it
is never called: a fake that simply lacked the method would fail with an
`AttributeError` reported as an internal bug, which is a much weaker statement
than "the command made no write call".

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
from railctl.cli.commands.diff import (
    DIFF_SCHEMA,
    SOURCE_DECODER,
    SOURCE_FILE,
    WARNING_NOT_COMPARED,
    offline_capabilities,
)
from railctl.cli.main import app
from railctl.errors import DecoderNotRespondingError
from railctl.station import (
    Capabilities,
    CvEncoding,
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

MANUFACTURER_ID = 145
MS450_TYPE = 6

#: What the fake decoder holds before the comparison, unless a test says
#: otherwise. CV3 differs from the file on purpose - it is the one CV every
#: "a difference is reported" sentence below is about.
LIVE_BEFORE = {3: 5}

#: What a CV nobody named answers.
DEFAULT_LIVE = 0

#: The neutral index page both the fixture files and the fake decoder sit on.
NEUTRAL_PAGE = (0, 0)

#: CV29 bit 5, the long-address bit `--merge-cv29` keeps from the decoder.
CV29_LONG_ADDRESS_MASK = 32


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


# -- the files -----------------------------------------------------------------


def record(cv: int, value: int | None, *, name: str = "", status: ReadStatus = ReadStatus.OK):
    return CvRecord(cv=cv, name=name or f"cv{cv}", status=status, value=value)


def default_records() -> tuple[CvRecord, ...]:
    """A small curated set covering all four stages: A (3, 4, 5), B (28, 29),
    C (1, 17, 18) and D (144)."""
    return (
        record(1, 3, name="primary_address"),
        record(3, 20, name="accel_rate"),
        record(4, 18, name="decel_rate"),
        record(5, 200, name="v_max"),
        record(17, 192, name="ext_address_high"),
        record(18, 40, name="ext_address_low"),
        record(28, 3, name="railcom_config"),
        record(29, 14, name="config_flags"),
        record(144, 0, name="confirm_jingle"),
    )


def backup_file(path: Path, **overrides: object) -> Path:
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
        "page": NEUTRAL_PAGE,
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
    write_backup_to(BackupDocument(**fields), path)  # type: ignore[arg-type]
    return path


def file_a(tmp_path: Path, **overrides: object) -> Path:
    return backup_file(tmp_path / "loco-0003-a.json", **overrides)


def file_b(tmp_path: Path, **overrides: object) -> Path:
    return backup_file(tmp_path / "loco-0003-b.json", **overrides)


# -- the fake station -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Write:
    cv: int
    value: int


class FakeDiffStation:
    """Answers the facade surface `diff` touches, and records the calls."""

    identity = "serial:7010A0001194:3"

    def __init__(
        self,
        *,
        values: dict[int, int] | None = None,
        read_errors: dict[int, Exception] | None = None,
        batch_errors: dict[int, Exception] | None = None,
        interrupt_on_batch: bool = False,
        emit_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        self.capabilities = SERVICE_CAPS
        self.values: dict[int, int] = {31: 0, 32: 0, **LIVE_BEFORE, **(values or {})}
        self.read_errors = read_errors or {}
        self.batch_errors = batch_errors or {}
        self.interrupt_on_batch = interrupt_on_batch
        self.emit_events = emit_events or []
        self.reads: list[int] = []
        self.batches: list[list[int]] = []
        self.writes: list[Write] = []

    def _result(self, cv: int, mode: ProgMode) -> CvResult:
        return CvResult(
            cv=cv,
            value=self.values.get(cv, DEFAULT_LIVE),
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="read",
            verified=None,
            elapsed=0.01,
        )

    def cv_read(self, cv, *, address=None, mode=ProgMode.SERVICE, page=None):
        self.reads.append(cv)
        error = self.read_errors.get(cv)
        if error is not None:
            raise error
        return self._result(cv, mode)

    def cv_read_many(self, specs, *, address=None, mode=ProgMode.SERVICE, on_progress=None):
        if self.interrupt_on_batch:
            raise KeyboardInterrupt
        self.batches.append([spec.cv for spec in specs])
        outcomes = []
        for spec in specs:
            error = self.batch_errors.get(spec.cv)
            if error is not None:
                outcomes.append(CvReadOutcome(spec=spec, result=None, error=error))
                continue
            outcomes.append(
                CvReadOutcome(spec=spec, result=self._result(spec.cv, mode), error=None)
            )
        return outcomes

    def cv_write(self, cv, value, *, address=None, mode=ProgMode.SERVICE, page=None, verify=True):
        # Never called. Recorded rather than raising, so the assertion a test
        # makes is "no write happened" and not "something blew up".
        self.writes.append(Write(cv=cv, value=value))
        return self._result(cv, mode)

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def close(self) -> None:
        pass


def install(monkeypatch, fake: FakeDiffStation) -> FakeDiffStation:
    def fake_open(*_a, **kwargs):
        callback = kwargs.get("on_event")
        for name, payload in fake.emit_events:
            if callback is not None:
                callback(name, payload)
        return fake

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    return fake


def boom_open(monkeypatch) -> None:
    """A `Station.open` that raises. Installed by every offline test: the
    two-file form must not merely avoid using a link, it must never ask for
    one."""

    def _boom(*_a, **_k):
        raise AssertionError("diff must not open a link")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))


def envelope(result) -> dict[str, object]:
    return json.loads(result.stderr.strip().splitlines()[-1])


def payload(result) -> dict[str, object]:
    return json.loads(result.stdout)["result"]


def ndjson_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.strip().splitlines()]


def invoke(*args: str, fmt: str = "json"):
    return runner.invoke(app, ["diff", *args, "--format", fmt])


def rows_by_cv(body: dict[str, object]) -> dict[int, dict[str, object]]:
    return {row["cv"]: row for row in body["cvs"]}  # type: ignore[index,union-attr]


# -- the metadata row -----------------------------------------------------------


def test_the_diff_row_publishes_its_safety_facts():
    meta = command_meta("diff")
    # The two fields an agent reads to decide whether this may run unattended.
    assert meta.mutates is False
    assert meta.confirms is False
    assert meta.schema == DIFF_SCHEMA


def test_the_diff_row_drops_the_three_codes_nothing_here_can_reach():
    codes = set(command_meta("diff").exit_codes)
    # 8 needs a partial ending, 14 needs a write to verify, 16 needs a POM path.
    assert codes.isdisjoint({8, 14, 16})
    # And it keeps the ones the shared planner and the page check can produce.
    assert {0, 2, 9, 15, 17} <= codes


def test_the_second_file_is_an_optional_positional():
    arguments = command_meta("diff").arguments
    assert [(a.name, a.required) for a in arguments] == [("file", True), ("file2", False)]


def test_the_exit_code_table_says_zero_does_not_mean_identical():
    """The one sentence this command exists to state out loud - `diff(1)`'s
    convention would read a decoder differing in forty CVs as a clean match."""
    from railctl.cli._meta import _COMMAND_EXIT_MEANINGS

    assert "diff(1)" in _COMMAND_EXIT_MEANINGS["diff"][0]


# -- the offline form: no link, ever --------------------------------------------


def test_two_files_compare_with_a_station_open_that_raises(monkeypatch, tmp_path):
    """The proof, not the promise: `Station.open` is replaced by something that
    raises, and the comparison still answers."""
    boom_open(monkeypatch)
    left = file_a(tmp_path)
    right = file_b(tmp_path, cvs=(*default_records()[:1], record(3, 99, name="accel_rate")))
    result = invoke(str(left), str(right))
    assert result.exit_code == 0, result.stderr
    body = payload(result)
    assert body["source"] == SOURCE_FILE
    assert body["other"] == str(right)
    assert rows_by_cv(body)[3]["live_value"] == 99


def test_the_offline_form_reports_the_differing_cv(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    left = file_a(tmp_path)
    right = file_b(tmp_path, cvs=(record(3, 99, name="accel_rate"), record(4, 18)))
    body = payload(invoke(str(left), str(right)))
    row = rows_by_cv(body)[3]
    assert (row["live_value"], row["file_value"], row["action"]) == (99, 20, "write")
    assert row["new_value"] == 20


def test_a_hole_in_the_second_file_is_not_known_rather_than_zero(monkeypatch, tmp_path):
    """A `no_response` row in FILE2 carries no value, and `plan_restore` reads
    that as "the live value is not known" - never as a 0 that differs."""
    boom_open(monkeypatch)
    left = file_a(tmp_path)
    right = file_b(tmp_path, cvs=(record(3, None, status=ReadStatus.NO_RESPONSE),))
    row = rows_by_cv(payload(invoke(str(left), str(right))))[3]
    assert row["live_value"] is None
    assert row["action"] == "write"
    assert "did not read back" in row["reason"]


def test_the_offline_capabilities_are_unknown_not_the_files_own_block(tmp_path):
    """The file's `capabilities` block describes the station that took the
    BACKUP, possibly months ago. Replaying it would let this process act on a
    `false` no instrument of its own reported."""
    from railctl.backup import read_backup

    document = read_backup(file_a(tmp_path))
    assert document.capabilities["pom_read"] is False
    caps = offline_capabilities(document)
    assert caps.pom_read is None
    assert caps.service_direct_cv is None
    assert caps.link_identity == "serial:7010A0001194:3"


def test_two_files_taken_on_different_pages_are_refused(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    left = file_a(tmp_path)
    right = file_b(tmp_path, page=(145, 0))
    result = invoke(str(left), str(right))
    assert result.exit_code == 17, result.stderr
    report = envelope(result)
    assert report["code"] == "index_page_required"
    assert report["details"] == {"live": [145, 0], "file": [0, 0], "source": SOURCE_FILE}


# -- the online form ------------------------------------------------------------


def test_a_changed_cv_is_reported_and_the_exit_code_is_still_zero(monkeypatch, tmp_path):
    """The sentence from the design: `diff` exits 0 when it completed, and the
    answer is in the payload."""
    install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 0, result.stderr
    body = payload(result)
    assert body["differences"] >= 1
    assert body["source"] == SOURCE_DECODER
    assert body["other"] is None
    row = rows_by_cv(body)[3]
    assert (row["live_value"], row["file_value"], row["action"]) == (5, 20, "write")


def test_an_identical_decoder_reports_zero_differences(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(values={1: 3, 3: 20, 4: 18, 5: 200, 28: 3, 29: 14}))
    body = payload(invoke(str(file_a(tmp_path))))
    assert body["differences"] == 0
    assert body["counts"]["write"] == 0


def test_the_online_form_writes_nothing_at_all(monkeypatch, tmp_path):
    """Not a CV, and not the CV31/CV32 selectors: the page is READ and a
    disagreement is refused, never re-selected."""
    fake = install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 0, result.stderr
    assert fake.writes == []
    assert fake.reads == [31, 32]


def test_the_live_pass_skips_the_never_restored_cvs(monkeypatch, tmp_path):
    """`live_probe_cvs` is shared with `restore`, so the CVs read here are the
    ones the plan can say something about - CV8 is read-only identity data and
    is never among them."""
    fake = install(monkeypatch, FakeDiffStation())
    invoke(str(file_a(tmp_path, cvs=(*default_records(), record(8, 145, name="manufacturer_id")))))
    assert fake.batches == [[1, 3, 4, 5, 17, 18, 28, 29, 144]]


def test_a_decoder_on_another_page_is_refused_rather_than_re_selected(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(values={31: 145, 32: 0}))
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 17, result.stderr
    report = envelope(result)
    assert report["details"] == {"live": [145, 0], "file": [0, 0], "source": SOURCE_DECODER}
    assert "never writes the selectors" in report["message"]


def test_silence_on_the_selectors_carries_the_placement_guidance(monkeypatch, tmp_path):
    """Silence on the programming track is far likelier to be an empty track
    than a dead decoder, and `_with_guidance` is what says so."""
    install(
        monkeypatch,
        FakeDiffStation(read_errors={31: DecoderNotRespondingError("no answer", cv=31)}),
    )
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 13, result.stderr
    assert envelope(result)["hint"] == SILENCE_GUIDANCE


def test_a_live_cv_that_does_not_answer_is_a_difference_not_a_match(monkeypatch, tmp_path):
    install(
        monkeypatch,
        FakeDiffStation(batch_errors={4: DecoderNotRespondingError("no answer", cv=4)}),
    )
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 0, result.stderr
    row = rows_by_cv(payload(result))[4]
    assert row["live_value"] is None
    assert row["action"] == "write"


def test_the_programming_track_notice_goes_to_stderr(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)))
    assert PROG_TRACK_NOTICE in result.stderr
    assert PROG_TRACK_NOTICE not in result.stdout


def test_a_station_failure_after_the_open_still_closes_the_link(monkeypatch, tmp_path):
    """The `except BaseException: close_quietly` arm. A `RuntimeError` is used
    deliberately: it is not a `RailctlError`, so nothing but that arm can be
    what closed the station."""

    class Exploding(FakeDiffStation):
        closed = False

        def cv_read_many(self, specs, **kwargs):
            raise RuntimeError("boom")

        def close(self) -> None:
            type(self).closed = True

    fake = install(monkeypatch, Exploding())
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 1
    assert type(fake).closed is True


# -- what is compared, and what is only reported --------------------------------


def test_the_address_cvs_are_not_compared_by_default(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    body = payload(invoke(str(file_a(tmp_path))))
    rows = rows_by_cv(body)
    assert [rows[cv]["action"] for cv in (1, 17, 18, 29)] == ["skip"] * 4
    # Both values are still in the row - a diff that hid them would be hiding
    # the very fact the operator opened it for.
    assert rows[1]["file_value"] == 3
    assert rows[1]["live_value"] == DEFAULT_LIVE
    assert "--with-address" in rows[1]["reason"]


def test_the_uncompared_rows_are_counted_and_warned_about(monkeypatch, tmp_path):
    """A headline "0 differences" over four silently uncompared address CVs is
    the reading this command must not permit."""
    install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)))
    warnings = json.loads(result.stdout)["warnings"]
    warning = next(w for w in warnings if w["name"] == WARNING_NOT_COMPARED)
    assert warning["details"]["cvs"] == [29, 17, 18, 1]
    assert "--with-address" in warning["message"]


def test_with_address_brings_the_address_cvs_into_the_comparison(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    body = payload(invoke(str(file_a(tmp_path)), "--with-address"))
    rows = rows_by_cv(body)
    assert [rows[cv]["action"] for cv in (1, 17, 18)] == ["write"] * 3
    assert body["options"]["with_address"] is True


def test_with_address_on_a_partial_address_set_is_refused(monkeypatch, tmp_path):
    """`plan_restore`'s own refusal, let out rather than caught: without all
    four CVs there is no address set to compare."""
    install(monkeypatch, FakeDiffStation())
    records = tuple(r for r in default_records() if r.cv != 18)
    result = invoke(str(file_a(tmp_path, cvs=records)), "--with-address")
    assert result.exit_code == 9, result.stderr
    report = envelope(result)
    assert report["code"] == "address_set_incomplete"
    assert report["details"]["missing"] == [18]


def test_a_value_the_catalog_refuses_stops_the_comparison(monkeypatch, tmp_path):
    """CV1 takes 1..127, so a file recording 200 is a file no restore could
    write - and the shared planner is what decides that, for both commands."""
    install(monkeypatch, FakeDiffStation())
    records = tuple(
        record(1, 200, name="primary_address") if r.cv == 1 else r for r in default_records()
    )
    result = invoke(str(file_a(tmp_path, cvs=records)), "--with-address")
    assert result.exit_code == 15, result.stderr
    report = envelope(result)
    assert report["details"]["out_of_range"] == [{"cv": 1, "value": 200, "min": 1, "max": 127}]


def test_merge_cv29_keeps_the_decoders_own_long_address_bit(monkeypatch, tmp_path):
    """The file says short addressing, the decoder is long-addressed, and
    everything else agrees: with `--merge-cv29` that is not a difference."""
    live_cv29 = 14 | CV29_LONG_ADDRESS_MASK
    install(monkeypatch, FakeDiffStation(values={29: live_cv29}))
    row = rows_by_cv(payload(invoke(str(file_a(tmp_path)), "--merge-cv29")))[29]
    assert row["file_value"] == 14
    assert row["live_value"] == live_cv29
    assert row["action"] == "unchanged"


def test_merge_cv29_still_reports_a_difference_in_the_other_bits(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(values={29: 6 | CV29_LONG_ADDRESS_MASK}))
    row = rows_by_cv(payload(invoke(str(file_a(tmp_path)), "--merge-cv29")))[29]
    assert row["action"] == "write"
    assert row["new_value"] == 14 | CV29_LONG_ADDRESS_MASK


def test_include_sweep_compares_a_cv_the_catalog_does_not_name(monkeypatch, tmp_path):
    """No source produces an uncurated row yet (that is M11's `--all`), so the
    branch is driven with a synthetic record."""
    records = (*default_records(), record(199, 7, name="uncurated"))
    install(monkeypatch, FakeDiffStation(values={199: 7}))
    path = file_a(tmp_path, cvs=records)
    without = rows_by_cv(payload(invoke(str(path))))[199]
    assert without["action"] == "skip"
    with_sweep = rows_by_cv(payload(invoke(str(path), "--include-sweep")))[199]
    assert with_sweep["action"] == "unchanged"


# -- the refusals that cost no file and no port ---------------------------------


def test_the_two_cv29_flags_together_are_a_usage_error(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    result = invoke(str(file_a(tmp_path)), "--with-address", "--merge-cv29")
    assert result.exit_code == 2
    assert result.stdout == ""
    report = envelope(result)
    assert report["code"] == "usage"
    assert report["details"]["reason"] == "contradictory_cv29_flags"


def test_the_refusal_suggests_each_flag_on_its_own_as_runnable_argv(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    path = file_a(tmp_path)
    result = runner.invoke(
        app, ["diff", str(path), "--with-address", "--merge-cv29", "--json", "--yes"]
    )
    assert envelope(result)["suggestions"] == [
        ["railctl", "diff", str(path), "--with-address", "--json", "--yes"],
        ["railctl", "diff", str(path), "--merge-cv29", "--json", "--yes"],
    ]


def test_a_refusal_keeps_the_second_file_and_the_globals_typed_after_the_verb(
    monkeypatch, tmp_path
):
    boom_open(monkeypatch)
    left, right = file_a(tmp_path), file_b(tmp_path)
    result = runner.invoke(
        app,
        [
            "diff",
            str(left),
            str(right),
            "--with-address",
            "--merge-cv29",
            "--include-sweep",
            "--target",
            "auto",
            "--format",
            "json",
            "-vv",
            "--color",
            "never",
            "--non-interactive",
        ],
    )
    assert envelope(result)["suggestions"][0] == [
        "railctl",
        "diff",
        str(left),
        str(right),
        "--with-address",
        "--include-sweep",
        "--target",
        "auto",
        "--format",
        "json",
        "--verbose",
        "--verbose",
        "--color",
        "never",
        "--non-interactive",
    ]


def test_an_unreadable_file_is_the_readers_own_error(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = invoke(str(bad))
    assert result.exit_code == 9
    assert envelope(result)["code"] == "backup_file"


def test_an_unreadable_second_file_is_refused_before_any_link(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = invoke(str(file_a(tmp_path)), str(bad))
    assert result.exit_code == 9
    assert envelope(result)["code"] == "backup_file"


# -- the three renderings -------------------------------------------------------


def test_the_json_envelope_carries_the_documented_keys(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)))
    body = json.loads(result.stdout)
    assert body["schema"] == DIFF_SCHEMA
    assert body["command"] == "diff"
    assert set(body["result"]) == {
        "file",
        "other",
        "source",
        "loco",
        "decoder",
        "page",
        "options",
        "differences",
        "counts",
        "cvs",
    }
    assert body["result"]["page"] == list(NEUTRAL_PAGE)
    assert body["result"]["loco"] == {"address": 3, "kind": "short"}


def test_the_counts_and_the_rows_describe_the_same_table(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    body = payload(invoke(str(file_a(tmp_path))))
    counts = body["counts"]
    assert sum(counts.values()) == len(body["cvs"])
    assert counts["write"] == body["differences"]


def test_the_human_rendering_names_both_sides_and_the_totals(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    path = file_a(tmp_path)
    result = runner.invoke(app, ["diff", str(path)])
    assert result.exit_code == 0, result.stderr
    assert f"{path} against the decoder on the programming track" in result.stdout
    assert "CV3 accel_rate: 5 -> 20 (stage A)" in result.stdout
    assert "4 differ, 1 unchanged, 4 not compared" in result.stdout


def test_the_human_rendering_of_the_offline_form_names_the_second_file(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    left, right = file_a(tmp_path), file_b(tmp_path)
    result = runner.invoke(app, ["diff", str(left), str(right)])
    assert result.exit_code == 0, result.stderr
    assert f"{left} against {right}" in result.stdout


def test_ndjson_starts_with_start_and_ends_with_summary(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation())
    result = invoke(str(file_a(tmp_path)), fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    lines = ndjson_lines(result.stdout)
    assert [line["sequence"] for line in lines] == list(range(len(lines)))
    assert lines[0]["type"] == "start"
    assert lines[0]["schema"] == DIFF_SCHEMA
    assert lines[0]["source"] == SOURCE_DECODER
    assert [line["type"] for line in lines[1:-1]] == ["cv"] * (len(lines) - 2)
    assert lines[-1]["type"] == "summary"
    assert lines[-1]["exit_code"] == 0
    assert lines[-1]["differences"] == 4


def test_ndjson_streams_the_offline_form_without_a_link(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    result = invoke(str(file_a(tmp_path)), str(file_b(tmp_path)), fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    lines = ndjson_lines(result.stdout)
    assert lines[0]["source"] == SOURCE_FILE
    assert lines[-1]["type"] == "summary"


def test_ndjson_ends_with_a_summary_even_when_the_comparison_fails(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(values={31: 145, 32: 0}))
    result = invoke(str(file_a(tmp_path)), fmt="ndjson")
    assert result.exit_code == 17
    lines = ndjson_lines(result.stdout)
    assert lines[-1]["type"] == "summary"
    assert lines[-1]["exit_code"] == 17
    # All zeros: the run never produced a row.
    assert lines[-1]["differences"] == 0
    assert envelope(result)["code"] == "index_page_required"


def test_ndjson_writes_no_stream_at_all_for_a_refusal_before_the_start_line(monkeypatch, tmp_path):
    boom_open(monkeypatch)
    result = invoke(str(file_a(tmp_path)), "--with-address", "--merge-cv29", fmt="ndjson")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert envelope(result)["code"] == "usage"


def test_ndjson_ends_with_a_summary_on_ctrl_c(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(interrupt_on_batch=True))
    result = invoke(str(file_a(tmp_path)), fmt="ndjson")
    assert result.exit_code == 9
    lines = ndjson_lines(result.stdout)
    assert lines[-1]["type"] == "summary"
    assert lines[-1]["exit_code"] == 9


def test_a_buffered_ctrl_c_is_the_shared_aborted_ending(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(interrupt_on_batch=True))
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 9
    assert envelope(result)["code"] == "aborted"


# -- one planner, two commands --------------------------------------------------


def test_diff_and_a_restore_dry_run_agree_row_for_row(monkeypatch, tmp_path):
    """The property the "no second comparator" rule buys: `restore --dry-run`
    and `diff` are the same list, because one function built both."""
    from tests.cli.test_restore import FakeRestoreStation
    from tests.cli.test_restore import install as install_restore

    path = file_a(tmp_path)
    install_restore(monkeypatch, FakeRestoreStation())
    restore_rows = payload(
        runner.invoke(app, ["restore", str(path), "--format", "json", "--dry-run"])
    )["cvs"]
    install(monkeypatch, FakeDiffStation())
    diff_rows = payload(invoke(str(path)))["cvs"]
    assert diff_rows == restore_rows


# -- station events -------------------------------------------------------------

STALE_EVENT = ("cv.stale_result", {"cv": 3})


def test_a_station_event_becomes_an_envelope_warning(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(emit_events=[STALE_EVENT]))
    result = invoke(str(file_a(tmp_path)))
    assert result.exit_code == 0, result.stderr
    names = [w["name"] for w in json.loads(result.stdout)["warnings"]]
    assert STALE_EVENT[0] in names


def test_a_station_event_becomes_an_ndjson_event_line(monkeypatch, tmp_path):
    install(monkeypatch, FakeDiffStation(emit_events=[STALE_EVENT]))
    result = invoke(str(file_a(tmp_path)), fmt="ndjson")
    assert result.exit_code == 0, result.stderr
    events = [line for line in ndjson_lines(result.stdout) if line["type"] == "event"]
    assert events[0]["name"] == STALE_EVENT[0]
    assert events[0]["details"] == {"cv": 3}
