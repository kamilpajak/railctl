# src/railctl/cli/commands/backup.py
"""`railctl backup` - the curated-set CV backup (design C6, milestone M9).

The run order is the design's, verbatim: capabilities (already loaded by
`open_station`), the CV31/CV32 index selectors as singleton reads, CV29, the
identity CVs (CV7, CV8, CV250-253), then the rest of `curated_cvs` ascending
through `Station.cv_read_many`. Nothing read in an earlier step is re-read.

Three properties are load-bearing here rather than emergent:

* **a backup never writes the decoder.** The one CV pair a read path could
  write - the CV31/CV32 page selectors - is exactly what this command refuses
  to touch: a non-default pair read off the decoder aborts (exit 17) unless
  `--page` acknowledges it, and the file records the pair as READ, never as
  declared;
* **`--mode auto` never gambles on POM.** It resolves to the programming
  track unless `pom_read` is MEASURED working - on this station a silent POM
  attempt costs 6.7 s per CV (docs/probe-results.md R1), and 77 of those is
  not a backup, it is a nine-minute timeout;
* **the file is the product, the exit code is its label.** A hole
  (`no_response`/`error`) still delivers the document - the file (or the
  stdout document with `--out -`) plus the buffered envelope carrying it -
  and exits 9, the holes named in a `backup.incomplete` warning (buffered)
  or the `backup_incomplete` stderr envelope (ndjson, where the document
  already streamed); Ctrl-C writes the partial file with
  `"interrupted": true` and exits 9 as `aborted`; a `skipped` row is a
  recorded decision and never changes the exit code.

NDJSON is this command's streaming mode and bypasses `run()` the same way
`monitor` does: `start`, `cv` and `event` lines as the run progresses, and a
`summary` line as the LAST line even on error and on Ctrl-C - once the
station is open, no ending may leave the stream without one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import typer

from railctl.backup import (
    BACKUP_SCHEMA,
    NOT_ATTEMPTED_DETAIL,
    STDOUT_TARGET,
    BackupDocument,
    CvRecord,
    ReadStatus,
    backup_path,
    record_for,
    write_backup,
    write_backup_to,
)
from railctl.catalog import CATALOG_FAMILY, CATALOG_SCHEMA, curated_cvs, load_catalog
from railctl.cli._errors import OutputContext, report_for, run, usage_report
from railctl.cli._meta import (
    BACKUP_FORCE_OPT,
    BACKUP_MODE_OPT,
    BACKUP_NOTE_OPT,
    BACKUP_OUT_OPT,
    BACKUP_PAGE_OPT,
    command_meta,
    global_option,
    help_epilog,
    typer_option,
)
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, _with_guidance, parse_page
from railctl.cli.commands.throttle import preflight
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    StationEventLog,
    UsageProblem,
    _jsonable,
    check_choice,
    close_after,
    close_quietly,
    link_info,
    merged_output,
    open_station,
    require_address,
    station_info,
)
from railctl.cli.render import NdjsonStream
from railctl.cli.result import USAGE_EXIT_CODE, CommandResult, ErrorReport
from railctl.errors import (
    AbortedError,
    BackupIncompleteError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    exit_code_for,
)
from railctl.station import CvPage, CvReadOutcome, CvResult, CvSpec, ProgMode
from railctl.xbus.cv import MAX_CV_DIRECT, MAX_CV_EXT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from railctl.catalog import CatalogEntry
    from railctl.cli.deps import Settings
    from railctl.station import Capabilities, Station

_BACKUP_META = command_meta("backup")

#: The one set M9 backs up. `--all` and `--set` arrive with M10/M11; naming
#: only what exists keeps the default filename honest.
SET_NAME: Final[str] = "curated"

#: XpressNet's short/long address boundary: 1..99 ride in one byte.
SHORT_ADDRESS_MAX: Final[int] = 99

#: CV31/CV32 both zero - the ZIMO default page a backup expects to find.
DEFAULT_PAGE: Final[CvPage] = (0, 0)

#: The design's step 4, in read order: CV7/CV8 (version, manufacturer) and
#: CV250-253 (decoder type and the three serial bytes) fill the `decoder`
#: block. A failure here is a hole in that block, never an abort.
IDENTITY_CVS: Final[tuple[int, ...]] = (7, 8, 250, 251, 252, 253)

#: `decoder` block field -> the CV it is read from. `serial_bytes` is built
#: separately: it exists only when all three of CV251-253 answered.
DECODER_FIELD_CVS: Final[tuple[tuple[str, int], ...]] = (
    ("manufacturer_id", 8),
    ("decoder_version", 7),
    ("decoder_type", 250),
)
SERIAL_CVS: Final[tuple[int, ...]] = (251, 252, 253)

#: What an interrupted ndjson run exits with - read off the class, exactly as
#: `commands/monitor.py` does, so this file and `errors.py` cannot disagree.
_ABORTED_EXIT_CODE: Final[int] = exit_code_for(AbortedError.__new__(AbortedError))

#: What a delivered-but-incomplete buffered run exits with - read off
#: `BackupIncompleteError` the same way, because the buffered path marks its
#: outcome with this code instead of raising the class (see `_work`).
_INCOMPLETE_EXIT_CODE: Final[int] = exit_code_for(
    BackupIncompleteError.__new__(BackupIncompleteError)
)

#: The buffered envelope's warning name for a delivered-but-incomplete run.
_INCOMPLETE_WARNING: Final[str] = "backup.incomplete"

# Built once, at import time - the same B008 note as every command module.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_OUT_OPT = typer_option(BACKUP_OUT_OPT)
_NOTE_OPT = typer_option(BACKUP_NOTE_OPT)
_FORCE_OPT = typer_option(BACKUP_FORCE_OPT)
_MODE_OPT = typer_option(BACKUP_MODE_OPT)
_PAGE_OPT = typer_option(BACKUP_PAGE_OPT)


def utc_timestamp() -> str:
    """The `created_utc` the CLI stamps into the file. A module-level seam on
    purpose: the writer is a pure function of the document, so a test that
    pins this proves two consecutive backups byte-identical."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_backup_mode(mode_word: str, capabilities: Capabilities) -> ProgMode:
    """Backup's own AUTO rule - deliberately stricter than `station.resolve_mode`.

    `auto` resolves to POM only when `pom_read` is a MEASURED yes; `None`
    (unprobed) falls to the programming track, where `cv read`'s AUTO would
    try POM and record the outcome. One exploratory read is a fair probe; 77
    silent POM attempts at 6.7 s each is not a backup. An explicit
    `--mode pom` is refused only on a measured no (exit 16), naming both
    remedies - the re-probe, and the programming track.
    """
    if mode_word == ProgMode.POM.value:
        if capabilities.pom_read is False:
            raise PomReadUnsupportedError(
                "POM reading is recorded as unavailable on this command station, and a "
                "backup will not send 77 reads down a channel measured not to answer",
                hint=(
                    "rerun `railctl doctor` to re-probe POM reading, or put the locomotive "
                    "on the programming track and back up with --mode service"
                ),
            )
        return ProgMode.POM
    if mode_word == ProgMode.SERVICE.value:
        return ProgMode.SERVICE
    return ProgMode.POM if capabilities.pom_read is True else ProgMode.SERVICE


def reachable_bound(mode: ProgMode, capabilities: Capabilities) -> int:
    """The highest CV the resolved mode can reach WITHOUT writing the decoder.

    Curated CVs above 255 live behind the ZIMO CV31/CV32 index page, and
    selecting a page writes the selectors - the one thing a backup never
    does - so they are out of reach unless the extended service opcodes are a
    measured yes. POM shares the 255 bound for the same reason: its native
    range is not the problem, the page selection is.
    """
    if mode is ProgMode.SERVICE and capabilities.service_ext_cv is True:
        return MAX_CV_EXT
    return MAX_CV_DIRECT


def _bound_detail(cv: int, mode: ProgMode, capabilities: Capabilities) -> str:
    """The recorded reason a curated CV past the bound was never attempted -
    three honest variants, because "extended opcodes unavailable" is an
    absent-capability claim only a measured `false` may make (the repo's
    founding rule): an unprobed pair gets "not probed", and a POM run's
    bound is about the page write a backup refuses, not the opcodes.
    Service mode with `service_ext_cv is True` never arrives here - the
    bound is `MAX_CV_EXT` and nothing is skipped."""
    base = f"cv {cv} > MAX_CV_DIRECT {MAX_CV_DIRECT}; "
    if mode is ProgMode.POM:
        return base + "indexed CVs need a CV31/CV32 page write, which a backup never performs"
    if capabilities.service_ext_cv is False:
        return base + "extended opcodes unavailable"
    return base + "extended opcodes not probed (run railctl doctor)"


class _Plan:
    """Everything decided BEFORE the station opens - a refusal here costs no port."""

    __slots__ = ("address", "declared_page", "mode_word", "note", "path")

    def __init__(
        self,
        *,
        mode_word: str,
        declared_page: CvPage | None,
        address: int,
        path: Path | None,
        note: str | None,
    ) -> None:
        self.mode_word = mode_word
        self.declared_page = declared_page
        self.address = address
        self.path = path
        self.note = note


def _typed_argv(
    address: int | None,
    *,
    out: str | None,
    note: str | None,
    mode_word: str,
    page_token: str | None,
    typed_globals: list[str],
) -> list[str]:
    """The invocation as typed, rebuilt as an argv array a suggestion can
    extend: `--address` and backup's own options first, then the global
    flags the operator actually typed, in registration order. `None` for an
    address not yet resolved (a refusal ahead of `require_address`) simply
    omits the flag rather than inventing a value."""
    argv = ["railctl", "backup"]
    if address is not None:
        argv += ["--address", str(address)]
    if out is not None:
        argv += ["--out", out]
    if note is not None:
        argv += ["--note", note]
    if mode_word != "auto":
        argv += ["--mode", mode_word]
    if page_token is not None:
        argv += ["--page", page_token]
    return argv + typed_globals


def plan_backup(
    settings: Settings,
    *,
    mode_word: str,
    page_token: str | None,
    out: str | None,
    note: str | None,
    force: bool,
    typed_globals: list[str],
) -> _Plan:
    """Validate the invocation and resolve the target path, station untouched.

    The overwrite refusal lives here on purpose: `backup_path` only resolves
    the path, and refusing AFTER the station opened would cost the operator a
    77-read run to learn the file already existed.
    """
    prefix = ["railctl", "backup"]
    check_choice("mode", mode_word, BACKUP_MODE_OPT.enum or ())
    declared_page = parse_page(page_token, argv_hint=prefix)
    address = require_address(settings, argv_hint=prefix)
    path = backup_path(address, SET_NAME, out)
    if path is not None and not force and path.exists():
        raise UsageProblem(
            f"{path} already exists; a backup never overwrites silently - pass --force "
            f"to replace it",
            suggestions=[
                [
                    *_typed_argv(
                        address,
                        out=out,
                        note=note,
                        mode_word=mode_word,
                        page_token=page_token,
                        typed_globals=typed_globals,
                    ),
                    "--force",
                ]
            ],
            details={"reason": "backup_file_exists", "path": str(path)},
        )
    return _Plan(
        mode_word=mode_word, declared_page=declared_page, address=address, path=path, note=note
    )


class _Context:
    """The document fields that come from the session, not from CV reads."""

    __slots__ = ("address", "capabilities", "created_utc", "link", "note", "tool")

    def __init__(self, station: Station, *, note: str | None, address: int) -> None:
        info = station_info(station)
        caps = station.capabilities
        self.created_utc = utc_timestamp()
        self.tool = f"railctl {metadata.version('railctl')}"
        self.note = note
        self.address = address
        self.link: dict[str, object] = {
            "identity": station.identity,
            "protocol": info.protocol,
            "protocol_version": info.protocol_version,
            "command_station_id": info.command_station_id,
        }
        # The six published keys, values straight off `Capabilities` - three
        # valued, never coerced: an unprobed capability lands in the file as
        # `null`, exactly as the doctor left it.
        self.capabilities: dict[str, object] = {
            "pom_read": caps.pom_read,
            "pom_result_channel": caps.pom_result_channel,
            "pom_echo_zero_based": caps.pom_echo_zero_based,
            "service_direct_cv": caps.service_direct_cv,
            "service_ext_cv": caps.service_ext_cv,
            "z21_cv_opcodes": caps.z21_cv_opcodes,
        }


class _Collection:
    """Design C6 steps 2-5, with every row recorded AS IT ARRIVES.

    Incremental on purpose: `cv_read_many`'s return value dies with a
    KeyboardInterrupt, so the rows a Ctrl-C partial file needs are collected
    through `on_progress` while the batch is still running, never from the
    list a completed batch would have returned.
    """

    def __init__(
        self,
        station: Station,
        catalog: Mapping[int, CatalogEntry],
        *,
        mode: ProgMode,
        address: int,
        declared_page: CvPage | None,
        on_start: Callable[[int, ProgMode], None] | None = None,
        on_cv: Callable[[CvRecord, CvResult | None], None] | None = None,
    ) -> None:
        self._station = station
        self._catalog = catalog
        self.mode = mode
        # Service mode acts on whatever stands on the programming track; the
        # address names the FILE there, never the read target.
        self._read_address = address if mode is ProgMode.POM else None
        self._declared_page = declared_page
        self._on_start = on_start
        self._on_cv = on_cv
        self.records: dict[int, CvRecord] = {}
        self.planned: tuple[int, ...] | None = None
        self.page: CvPage | None = None
        self.encoding: str | None = None
        #: The declared pair when it disagrees with what the decoder reads -
        #: the file records the measurement, and the disagreement is a warning.
        self.page_mismatch: CvPage | None = None
        self.speed_table_included = False

    def collect(self) -> None:
        selector_31 = self._singleton(31)
        selector_32 = self._singleton(32)
        page: CvPage = (selector_31.value, selector_32.value)
        if page != DEFAULT_PAGE and self._declared_page is None:
            raise IndexPageRequiredError(
                f"the decoder sits on index page CV31={page[0]} CV32={page[1]}, not the "
                f"default {DEFAULT_PAGE[0]}:{DEFAULT_PAGE[1]}; a backup never writes the "
                f"selectors, so rerun with --page {page[0]}:{page[1]} to acknowledge it",
                details={"page": list(page)},
            )
        if self._declared_page is not None and self._declared_page != page:
            self.page_mismatch = self._declared_page
        self.page = page
        config_29 = self._singleton(29)
        curated = curated_cvs(self._catalog, config_29.value)
        self.planned = tuple(curated)
        self.speed_table_included = any(self._catalog[cv].needs_speed_table for cv in curated)
        if self._on_start is not None:
            self._on_start(len(curated), self.mode)
        for result in (selector_31, selector_32, config_29):
            self._record_result(result)
        bound = reachable_bound(self.mode, self._station.capabilities)
        for cv in curated:
            if cv > bound:
                self._record(
                    CvRecord(
                        cv=cv,
                        name=self.name_for(cv),
                        status=ReadStatus.SKIPPED,
                        detail=_bound_detail(cv, self.mode, self._station.capabilities),
                    ),
                    None,
                )
        remaining = [cv for cv in curated if cv not in self.records]
        self._batch([cv for cv in IDENTITY_CVS if cv in remaining])
        self._batch([cv for cv in remaining if cv not in IDENTITY_CVS])

    def name_for(self, cv: int) -> str:
        # Every CV this command visits came out of the catalog, so the slug
        # always exists - a KeyError here is a bug, not a data question.
        return self._catalog[cv].slug

    def _singleton(self, cv: int) -> CvResult:
        """One `cv_read` whose failure ABORTS the run: without the page and
        CV29 there is no honest file to write. Service-mode silence carries
        the same placement guidance `cv read` attaches."""
        try:
            return self._station.cv_read(cv, address=self._read_address, mode=self.mode)
        except RailctlError as exc:
            raise _with_guidance(exc, mode=self.mode) from None

    def _record_result(self, result: CvResult) -> None:
        spec = CvSpec(cv=result.cv, name=self.name_for(result.cv))
        outcome = CvReadOutcome(spec=spec, result=result, error=None)
        self._record(record_for(outcome), result)

    def _record(self, record: CvRecord, result: CvResult | None) -> None:
        if result is not None and self.encoding is None:
            # `cv_encoding` is the encoding of the FIRST ok read (design C4).
            self.encoding = result.encoding.name
        self.records[record.cv] = record
        if self._on_cv is not None:
            self._on_cv(record, result)

    def _batch(self, cvs: list[int]) -> None:
        specs = [CvSpec(cv=cv, name=self.name_for(cv)) for cv in cvs]

        def progress(update: tuple[int, int, CvReadOutcome]) -> None:
            outcome = update[2]
            self._record(record_for(outcome), outcome.result)

        outcomes = self._station.cv_read_many(
            specs, address=self._read_address, mode=self.mode, on_progress=progress
        )
        # The real station reports every spec through `on_progress`, so this
        # loop records nothing new there; it is the fallback for a facade that
        # only returns the finished list.
        for outcome in outcomes:
            if outcome.spec.cv not in self.records:
                self._record(record_for(outcome), outcome.result)


def _decoder_block(records: Mapping[int, CvRecord]) -> dict[str, object]:
    """The `decoder` block, holes omitted: a field whose CV did not answer is
    absent, and `serial_bytes` exists only when all three bytes are `ok`."""
    block: dict[str, object] = {}
    for field, cv in DECODER_FIELD_CVS:
        record = records.get(cv)
        if record is not None and record.value is not None:
            block[field] = record.value
    serial = [records.get(cv) for cv in SERIAL_CVS]
    if all(record is not None and record.value is not None for record in serial):
        block["serial_bytes"] = [record.value for record in serial]  # type: ignore[union-attr]
    return block


def _document(
    collection: _Collection, context: _Context, *, interrupted: bool = False
) -> BackupDocument:
    records = dict(collection.records)
    if interrupted:
        # The rows the interrupt cut off: `skipped` with the not-attempted
        # detail, so the partial file still answers for every planned CV and
        # `summary.requested` keeps meaning "the whole curated set".
        for cv in collection.planned or ():
            if cv not in records:
                records[cv] = CvRecord(
                    cv=cv,
                    name=collection.name_for(cv),
                    status=ReadStatus.SKIPPED,
                    detail=NOT_ATTEMPTED_DETAIL,
                )
    kind = "short" if context.address <= SHORT_ADDRESS_MAX else "long"
    return BackupDocument(
        created_utc=context.created_utc,
        tool=context.tool,
        note=context.note,
        loco={"address": context.address, "kind": kind},
        catalog={"family": CATALOG_FAMILY, "schema": CATALOG_SCHEMA},
        set_name=SET_NAME,
        mode=collection.mode.value,
        cv_encoding=collection.encoding,
        page=collection.page or DEFAULT_PAGE,
        speed_table_included=collection.speed_table_included,
        sweep_range=None,
        link=context.link,
        capabilities=context.capabilities,
        decoder=_decoder_block(records),
        cvs=tuple(records.values()),
        interrupted=interrupted,
    )


class _BackupRun:
    """One run's mutable state, shared by the buffered and the ndjson paths so
    a `finally` can still say what was collected and where it was written."""

    def __init__(
        self,
        plan: _Plan,
        catalog: Mapping[int, CatalogEntry],
        *,
        on_start: Callable[[int, ProgMode], None] | None = None,
        on_cv: Callable[[CvRecord, CvResult | None], None] | None = None,
    ) -> None:
        self.plan = plan
        self._catalog = catalog
        self._on_start = on_start
        self._on_cv = on_cv
        self.collection: _Collection | None = None
        self.document: BackupDocument | None = None
        self.text: str | None = None
        self.written: Path | None = None

    def execute(self, station: Station, output: OutputContext) -> None:
        plan = self.plan
        resolved = resolve_backup_mode(plan.mode_word, station.capabilities)
        if resolved is ProgMode.POM:
            # The same pre-flight every POM cv command runs (design L6): a
            # speed already sitting in the refresh buffer is exactly what a
            # POM telegram must not join.
            preflight(station, speed=None, action="a main-track (POM) CV backup")
        else:
            print(PROG_TRACK_NOTICE, file=output.stderr)
        context = _Context(station, note=plan.note, address=plan.address)
        collection = _Collection(
            station,
            self._catalog,
            mode=resolved,
            address=plan.address,
            declared_page=plan.declared_page,
            on_start=self._on_start,
            on_cv=self._on_cv,
        )
        self.collection = collection
        try:
            collection.collect()
        except KeyboardInterrupt:
            self._abort(context)
        self.document = _document(collection, context)
        self.text = write_backup(self.document)
        if plan.path is not None:
            write_backup_to(self.document, plan.path)
            self.written = plan.path

    def _abort(self, context: _Context) -> NoReturn:
        """Ctrl-C: write what was measured, then exit 9 as `aborted`.

        The partial file needs the page and the curated list to be honest
        about what it covers; an interrupt before CV29 answered has nothing
        worth keeping, and a stdout target has nowhere durable to keep it.
        """
        collection = self.collection
        if collection is None or collection.planned is None:
            raise AbortedError(
                "interrupted before the curated CV list was established; no backup file was written"
            ) from None
        if self.plan.path is None:
            raise AbortedError(
                "interrupted; the target was stdout (--out -), so no partial file was written"
            ) from None
        partial = _document(collection, context, interrupted=True)
        # Assigned BEFORE writing, so the ndjson `finally` reads its counts
        # and `complete` off the SAME document the file was written from -
        # two channels describing one run must not disagree about it.
        self.document = partial
        write_backup_to(partial, self.plan.path)
        self.written = self.plan.path
        raise AbortedError(
            f"interrupted; the partial backup was written to {self.plan.path} with "
            f'"interrupted": true',
            details={"path": str(self.plan.path)},
        ) from None


def _row_line(record: CvRecord) -> str:
    label = f"CV{record.cv} {record.name}"
    if record.status is ReadStatus.OK:
        return f"{label} = {record.value}"
    # Every non-ok row this command produces carries a detail: the station's
    # own text, the bound reason, or the not-attempted marker.
    return f"{label}: {record.status.value} ({record.detail})"


def build_backup(document: BackupDocument, *, path: Path | None, text: str) -> CommandResult:
    """One result, three renderings: the envelope `result` is the file
    document itself plus the one fact the file cannot carry - where it went.
    The reader tolerates unknown top-level keys, so `result` piped to a file
    still validates."""
    result = CommandResult(schema=_BACKUP_META.schema, command="backup")
    result.result = {**json.loads(text), "path": None if path is None else str(path)}
    if path is None:
        # `--out -`: the document text IS the human result.
        result.lines = text.splitlines()
        return result
    summary = document.summary
    for record in sorted(document.cvs, key=lambda r: r.cv):
        result.say(_row_line(record))
    completeness = "yes" if summary["complete"] else "no"
    result.say(f"{summary['ok']} of {summary['requested']} CVs read; complete: {completeness}")
    result.say(f"written to {path}")
    return result


def incomplete_report(
    document: BackupDocument, path: Path | None
) -> tuple[str, dict[str, object]] | None:
    """The one composition of "this file has holes" - message and details -
    with two consumers: `_work` marks its outcome with them (the document is
    delivered; exit 9 is its label), and `require_complete` raises them as
    `BackupIncompleteError` for the ndjson path. `None` for a complete
    document."""
    summary = document.summary
    if summary["complete"]:
        return None
    non_ok = sorted(
        (r for r in document.cvs if r.status in (ReadStatus.NO_RESPONSE, ReadStatus.ERROR)),
        key=lambda r: r.cv,
    )
    listed = ", ".join(f"CV{r.cv} ({r.status.value})" for r in non_ok)
    message = (
        f"backup incomplete: {len(non_ok)} of {summary['requested']} CVs produced no "
        f"value - {listed}"
    )
    details: dict[str, object] = {
        "path": None if path is None else str(path),
        "no_response": [r.cv for r in non_ok if r.status is ReadStatus.NO_RESPONSE],
        "error": [r.cv for r in non_ok if r.status is ReadStatus.ERROR],
        "skipped": sorted(r.cv for r in document.cvs if r.status is ReadStatus.SKIPPED),
    }
    return message, details


def require_complete(document: BackupDocument, path: Path | None) -> None:
    """Exit 9 when the file has holes - the NDJSON path's consumer of
    `incomplete_report`, raised AFTER the file is written and the rows
    streamed: there the document already reached its consumer, and the
    stderr envelope is the design's exit-9 report. The buffered path never
    raises this - it delivers the document and marks the outcome instead
    (see `_work`)."""
    report = incomplete_report(document, path)
    if report is None:
        return
    message, details = report
    raise BackupIncompleteError(message, details=details)


def _mismatch_details(collection: _Collection) -> dict[str, object]:
    declared = collection.page_mismatch or DEFAULT_PAGE
    read = collection.page or DEFAULT_PAGE
    return {"declared": list(declared), "read": list(read)}


_PAGE_MISMATCH_EVENT: Final[str] = "backup.page_mismatch"
_PAGE_MISMATCH_MESSAGE: Final[str] = (
    "--page declared a CV31:CV32 pair the decoder does not read back; the file records "
    "what was read, not what was declared"
)


def _work(
    settings: Settings,
    output: OutputContext,
    *,
    mode: str,
    page: str | None,
    out: str | None,
    note: str | None,
    force: bool,
    typed_globals: list[str],
) -> CommandResult:
    plan = plan_backup(
        settings,
        mode_word=mode,
        page_token=page,
        out=out,
        note=note,
        force=force,
        typed_globals=typed_globals,
    )
    catalog = load_catalog()
    events = StationEventLog()
    station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
    try:
        backup_run = _BackupRun(plan, catalog)
        backup_run.execute(station, output)
        outcome = build_backup(backup_run.document, path=plan.path, text=backup_run.text)
        collection = backup_run.collection
        if collection is not None and collection.page_mismatch is not None:
            outcome.warn(
                _PAGE_MISMATCH_EVENT, _PAGE_MISMATCH_MESSAGE, **_mismatch_details(collection)
            )
        events.attach_to(outcome)
        outcome.link = link_info(station, settings)
        outcome.station = station_info(station)
        # An incomplete document is still the product: deliver it and mark
        # the outcome, never discard it for an error envelope. Exit 9 is the
        # label on the file, and the holes ride as a warning beside it.
        incomplete = incomplete_report(backup_run.document, plan.path)
        if incomplete is not None:
            message, details = incomplete
            outcome.ok = False
            outcome.exit_code = _INCOMPLETE_EXIT_CODE
            outcome.warn(_INCOMPLETE_WARNING, message, **details)
    except BaseException:
        close_quietly(station)
        raise
    return close_after(station, outcome)


def _write_error(report: ErrorReport, output: OutputContext) -> None:
    """One JSON object on stderr - the shape `render_error` gives every other
    command, written here because the ndjson path deliberately never calls
    `run()` (same reasoning as `commands/monitor.py`)."""
    output.stderr.write(json.dumps(report.envelope(), separators=(",", ":")) + "\n")


#: The summary line's five count keys, in the file's own order.
_COUNT_KEYS: Final[tuple[str, ...]] = ("requested", "ok", "no_response", "error", "skipped")


def _summary_counts(run: _BackupRun | None) -> dict[str, object]:
    """The stream summary's counts, read off the run's document when one was
    built - the SAME rows the file was written from, so the counts sum to
    their own `requested` - and off the incremental collection only when no
    document exists (an abort or refusal before one could be assembled)."""
    if run is not None and run.document is not None:
        summary = run.document.summary
        return {key: summary[key] for key in _COUNT_KEYS}
    collection = run.collection if run is not None else None
    if collection is None:
        return dict.fromkeys(_COUNT_KEYS, 0)
    counts = dict.fromkeys(ReadStatus, 0)
    for record in collection.records.values():
        counts[record.status] += 1
    requested = (
        len(collection.planned) if collection.planned is not None else len(collection.records)
    )
    return {
        "requested": requested,
        "ok": counts[ReadStatus.OK],
        "no_response": counts[ReadStatus.NO_RESPONSE],
        "error": counts[ReadStatus.ERROR],
        "skipped": counts[ReadStatus.SKIPPED],
    }


def _json_format_globals(typed_globals: list[str]) -> list[str]:
    """`typed_globals` with any typed `--format <value>` replaced by a
    trailing `--format json` - the suggestion must not carry the stream
    format and its replacement side by side, and the explicit flag also
    outranks a RAILCTL_FORMAT that picked ndjson without being typed."""
    swapped: list[str] = []
    skip_value = False
    for word in typed_globals:
        if skip_value:
            skip_value = False
            continue
        if word == "--format":
            skip_value = True
            continue
        swapped.append(word)
    return [*swapped, "--format", "json"]


def _refuse_stdout_stream(
    settings: Settings,
    *,
    note: str | None,
    mode_word: str,
    page_token: str | None,
    typed_globals: list[str],
) -> NoReturn:
    """`--format ndjson` + `--out -`: the stream owns stdout, so the document
    would be produced NOWHERE. Refused at the top of the ndjson path, before
    `plan_backup` and before any port opens - like every usage refusal it
    costs no stream at all - with both remedies as runnable argvs: keep the
    stream and write the file, or keep stdout and buffer as json."""
    address = settings.address
    keep_stream = _typed_argv(
        address,
        out=None,
        note=note,
        mode_word=mode_word,
        page_token=page_token,
        typed_globals=typed_globals,
    )
    keep_stdout = _typed_argv(
        address,
        out=STDOUT_TARGET,
        note=note,
        mode_word=mode_word,
        page_token=page_token,
        typed_globals=_json_format_globals(typed_globals),
    )
    raise UsageProblem(
        "--format ndjson streams events to stdout and --out - writes the document there "
        "too; the stream and the document cannot share stdout, so the document would be "
        "delivered nowhere - write the file to a path, or buffer with --format json",
        suggestions=[keep_stream, keep_stdout],
        details={"reason": "ndjson_stdout_conflict"},
    )


def _run_ndjson(
    settings: Settings,
    output: OutputContext,
    *,
    mode: str,
    page: str | None,
    out: str | None,
    note: str | None,
    force: bool,
    typed_globals: list[str],
) -> NoReturn:
    """The streaming path, bypassing `run()` exactly as `monitor` does: once
    the station is open, EVERY ending - success, a hole, an error, Ctrl-C -
    finishes the stream with one `summary` line carrying the same exit code
    the process leaves with. A refusal before the station opens produces no
    stream at all, only the error envelope on stderr."""
    stream = NdjsonStream(output.stdout)

    def on_event(name: str, payload: dict[str, object]) -> None:
        stream.event("event", name=name, details=_jsonable(dict(payload)))

    def on_start(total: int, resolved: ProgMode) -> None:
        # `backup_run` is assigned below, before any collection can call this.
        stream.event(
            "start",
            schema=BACKUP_SCHEMA,
            address=backup_run.plan.address,
            mode=resolved.value,
            total=total,
        )

    def on_cv(record: CvRecord, result: CvResult | None) -> None:
        fields: dict[str, object] = {
            "cv": record.cv,
            "name": record.name,
            "status": record.status.value,
        }
        if record.value is not None:
            fields["value"] = record.value
        if record.detail is not None:
            fields["detail"] = record.detail
        if record.attempts is not None:
            fields["attempts"] = record.attempts
        if result is not None:
            fields["elapsed_ms"] = round(result.elapsed * 1000)
        stream.event("cv", **fields)

    station = None
    backup_run: _BackupRun | None = None
    exit_code = 0
    try:
        if out == STDOUT_TARGET:
            _refuse_stdout_stream(
                settings,
                note=note,
                mode_word=mode,
                page_token=page,
                typed_globals=typed_globals,
            )
        plan = plan_backup(
            settings,
            mode_word=mode,
            page_token=page,
            out=out,
            note=note,
            force=force,
            typed_globals=typed_globals,
        )
        catalog = load_catalog()
        backup_run = _BackupRun(plan, catalog, on_start=on_start, on_cv=on_cv)
        station = open_station(settings, capabilities_path=capabilities_path(), on_event=on_event)
        backup_run.execute(station, output)
        collection = backup_run.collection
        if collection is not None and collection.page_mismatch is not None:
            stream.event("event", name=_PAGE_MISMATCH_EVENT, details=_mismatch_details(collection))
        require_complete(backup_run.document, plan.path)
    except KeyboardInterrupt:
        # An interrupt the collection never saw - during open or the POM
        # pre-flight. Nothing was measured, so there is no envelope to write;
        # the summary (owed once the station opened) and the exit code are
        # the whole report, the same ending `monitor` gives one.
        exit_code = _ABORTED_EXIT_CODE
    except ValueError as exc:
        exit_code = USAGE_EXIT_CODE
        _write_error(usage_report(exc), output)
    except RailctlError as exc:
        exit_code = exit_code_for(exc)
        _write_error(report_for(exc, command="backup"), output)
    finally:
        if station is not None:
            # `backup_run` is always built before the station opens, so once a
            # summary is owed there is a run to read it off.
            written = backup_run.written if backup_run is not None else None
            document = backup_run.document if backup_run is not None else None
            complete = bool(document.summary["complete"]) if document is not None else False
            counts = _summary_counts(backup_run)
            stream.summary(
                **counts,
                complete=complete,
                path=None if written is None else str(written),
                exit_code=exit_code,
            )
            close_quietly(station)
    raise typer.Exit(code=exit_code)


def register(app: typer.Typer) -> None:
    """Attach `backup` between the `cv` group and `schema`, where
    `_meta.COMMANDS` puts it. Declares the eight global options a second time
    like every registered command (Click parses a group's options only before
    the subcommand name)."""

    @app.command("backup", help=_BACKUP_META.help, epilog=help_epilog(_BACKUP_META))
    def backup_command(
        ctx: typer.Context,
        out: str | None = _OUT_OPT,
        note: str | None = _NOTE_OPT,
        force: bool = _FORCE_OPT,
        mode: str = _MODE_OPT,
        page: str | None = _PAGE_OPT,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings, output = merged_output(
            cli_ctx.settings,
            cli_ctx.output,
            target=target,
            address=address,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )
        # The global flags actually typed AFTER the verb, rebuilt in
        # registration order. Each parameter is its "nothing typed" sentinel
        # (None/False/0) when absent - exactly the values `merged_output`
        # receives above - so a refusal's suggested argv keeps the full
        # invocation and invents nothing the operator did not type.
        typed_globals: list[str] = []
        if target is not None:
            typed_globals += ["--target", target]
        if format_ is not None:
            typed_globals += ["--format", format_]
        if json_flag:
            typed_globals.append("--json")
        typed_globals += ["--verbose"] * verbose
        if color is not None:
            typed_globals += ["--color", color]
        if yes:
            typed_globals.append("--yes")
        if non_interactive:
            typed_globals.append("--non-interactive")
        if output.fmt == "ndjson":
            _run_ndjson(
                settings,
                output,
                mode=mode,
                page=page,
                out=out,
                note=note,
                force=force,
                typed_globals=typed_globals,
            )
        run(
            "backup",
            output,
            lambda: _work(
                settings,
                output,
                mode=mode,
                page=page,
                out=out,
                note=note,
                force=force,
                typed_globals=typed_globals,
            ),
        )
