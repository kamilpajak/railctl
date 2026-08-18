# src/railctl/cli/commands/restore.py
"""`railctl restore` - the four-stage CV restore (design C7, milestone M10).

The plan comes from `railctl.backup.plan_restore`, which is pure and takes no
station, so `--dry-run` prints exactly the list the executor then consumes:
one function, one order, nothing to drift. This module is the half that
touches the decoder - the identity gate, the preconditions, the staged writes
and the per-stage verification.

Four properties are load-bearing here rather than emergent:

* **restore runs on the programming track and nowhere else** (M10 decision
  D1). `--track` accepts `prog` alone; `--track main` is refused with both
  reasons - the identity gate needs reads, and on this station a POM CV read
  is measured silent (docs/probe-results.md R1), so a main-track restore could
  be neither gated nor verified. There is no `--mode` and no `--no-verify`:
  an unverified restore is the failure mode this command exists to prevent.
  One thing that refusal buys, and it is worth naming because a reader coming
  from the design will look for it: service mode addresses the TRACK, so
  after stage C writes CV17/CV18/CV1 there is no station re-targeting and no
  read-CV8-at-the-new-address diagnostic. Both are POM concerns and both are
  out of scope while there is no POM path;
* **the identity gate is real, and it is the only thing standing between a
  restore and the wrong locomotive.** Live CV8 and CV250 must equal the
  file's `manufacturer_id` and `decoder_type` - a hard abort, overridable by
  nothing. When the file carries `serial_bytes`, CV251-253 must match too,
  and only `--confirm=<the serial just read>` gets past a mismatch. Those
  three CVs answer, repeatably: CV251=251, CV252=105, CV253=75 on three
  consecutive backups (MEASURED 2026-08-13, docs/probe-results.md, "CV251-253
  answer after all"), which is what turned the design's printed warning into a
  gate. A file with no serial bytes is a legitimate hole and degrades to a
  warning naming what did match; an identity CV that cannot be READ aborts
  rather than guesses;
* **a real restore DOES write CV31/CV32, and this is the one command that
  does.** Every write goes out with the file's `page`, because on the service
  path a write to a CV in 257..512 is refused outright without one, and
  selecting a page is itself a pair of decoder writes. That is deliberate and
  design-sanctioned ("Restore uses the same grouping ... while the page is
  still selected"), and it is exactly what `backup` refuses to do - the
  difference is precondition 5: live CV31/CV32 must already equal the file's
  pair before anything is written, so the selection writes the two values the
  decoder is already holding and moves no bank. Reads are the asymmetric half:
  a service READ answers on the live bank and ignores the page, which is why
  `backup` can read CV265 without ever selecting anything. The verification
  read-backs carry the page all the same - a verify that is not pinned to the
  bank its write went to would only be accidentally right;
* **`--dry-run` writes nothing at all**, not even the CV31/CV32 selectors. It
  only reads, and a service read selects nothing; a test installs a fake that
  fails the run if `select_page` or a selector write is ever reached. It still
  runs the gate, because a dry run that skipped it would print a plan for the
  wrong locomotive;
* **nothing is ever rolled back.** A partial rollback can leave a state worse
  than the observed one, and the file plus the mismatch table already say
  which CVs disagree. Every failed ending says so and names the recovery:
  re-run `restore`, which is idempotent. Every ending, not the two that
  happened to be raised here - a station error escaping a stage is enriched
  in place with the CVs already written, the ones verified and the stages
  completed, keeping its own class, `code` and exit code, because the
  station's verdict on what went wrong is still the verdict.

Verification is per stage, against the INTENDED value (the masked byte for a
merged CV29, never the raw file value), with one retry and one re-read and no
further loops. The writes themselves go out with `verify=False` because this
command owns the verification: the station's own per-write read-back raises on
the first mismatch, and what the operator needs is the whole stage's table.
That is also why `cv.write_unverified` is the one station event this command
does not publish - it describes the moment between a write and the stage's
read-back, and reporting it as a warning would say nothing was checked when
the whole stage was.

NDJSON is this command's streaming mode and bypasses `run()` the same way
`backup` and `monitor` do: `start` once the plan exists, one `cv` line per CV
as it is written (or, in a dry run, per CV that would be), a `stage` line as
each stage finishes verifying, `event` lines, and a `summary` line LAST even
on error and on Ctrl-C. As in `backup`, a failure before the plan exists - a
gate refusal, an unreadable identity CV - produces a stream whose only line is
the summary, so a consumer must key on `type` and never on line position.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import typer

from railctl.backup import (
    BackupDocument,
    CvRecord,
    PlannedWrite,
    ReadStatus,
    never_write_cvs,
    plan_restore,
    read_backup,
)
from railctl.backup.plan import STAGES
from railctl.catalog import load_catalog
from railctl.cli._errors import OutputContext, report_for, run, usage_report
from railctl.cli._meta import (
    RESTORE_ALLOW_INCOMPLETE_OPT,
    RESTORE_CONFIRM_OPT,
    RESTORE_DRY_RUN_OPT,
    RESTORE_FILE_ARG,
    RESTORE_INCLUDE_SWEEP_OPT,
    RESTORE_MERGE_CV29_OPT,
    RESTORE_TRACK_OPT,
    RESTORE_WITH_ADDRESS_OPT,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.commands.backup import DECODER_FIELD_CVS, SERIAL_CVS
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, TRACK_MAIN, TRACK_PROG, _with_guidance
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    StationEventLog,
    UsageProblem,
    _jsonable,
    check_choice,
    close_after,
    close_quietly,
    confirm,
    link_info,
    merged_output,
    open_station,
    station_info,
)
from railctl.cli.render import NdjsonStream
from railctl.cli.result import USAGE_EXIT_CODE, CommandResult, ErrorReport
from railctl.errors import (
    AbortedError,
    CvVerifyError,
    DecoderIdentityMismatchError,
    IndexPageRequiredError,
    ProgrammingLockedError,
    RailctlError,
    RestoreFileIncompleteError,
    exit_code_for,
)
from railctl.station import (
    CV144,
    PAGE_SELECTOR_CVS,
    CvPage,
    CvSpec,
    ProgMode,
    treats_cv144_as_lock,
)

if TYPE_CHECKING:
    from railctl.catalog import CatalogEntry
    from railctl.cli.deps import Settings
    from railctl.station import Station

_RESTORE_META = command_meta("restore")

#: Read off the metadata row, never retyped: the manifest says what this
#: command emits and the envelope says what it emitted.
RESTORE_SCHEMA: Final[str] = _RESTORE_META.schema

#: The four `PlannedWrite.action` words, in report order. `backup.plan.Action`
#: is the source; this tuple is what turns them into counts, and
#: `tests/cli/test_restore.py` pins every planned row against it.
ACTIONS: Final[tuple[str, ...]] = ("write", "unchanged", "skip", "unreadable")
ACTION_WRITE: Final[str] = ACTIONS[0]

#: One CV operation costs this on this station - MEASURED 2026-08-13
#: (docs/probe-results.md, "A backup costs 6 s per CV"): a read is about 3 s
#: and the next one waits out the 3.0 s inter-session gap. The design's
#: "3-4 minutes" for a 77-CV restore predates that measurement and is wrong.
SECONDS_PER_CV: Final[float] = 6.0

#: What one restored CV costs: the write, then the stage's read-back. A
#: mismatch adds a second pair, which the estimate deliberately does not
#: include - the estimate is for the run that works.
OPERATIONS_PER_WRITE: Final[int] = 2

#: The `decoder` fields the gate is hard about, and the CVs they are read
#: from - filtered out of `backup`'s own field->CV table rather than retyped,
#: so the gate reads back exactly what the writer recorded. `decoder_version`
#: (CV7) is deliberately not among them: firmware changes on the same
#: physical decoder, and gating on it would refuse a legitimate restore.
_HARD_IDENTITY_FIELDS: Final[tuple[str, ...]] = ("manufacturer_id", "decoder_type")
IDENTITY_FIELD_CVS: Final[tuple[tuple[str, int], ...]] = tuple(
    (field, cv) for field, cv in DECODER_FIELD_CVS if field in _HARD_IDENTITY_FIELDS
)

#: The `decoder.serial_bytes` key, named once: the gate reads it and the
#: degraded warning names it.
SERIAL_FIELD: Final[str] = "serial_bytes"

#: The only value of CV144 a locking decoder family may show before a restore.
CV144_UNLOCKED: Final[int] = 0

#: The one station event this command drops instead of publishing. `restore`
#: writes with `verify=False` because it verifies per stage itself, so the
#: station emits this for every write; publishing it would tell the operator
#: nothing was checked about writes that were all checked one stage later.
SUPPRESSED_EVENT: Final[str] = "cv.write_unverified"

#: `details["reason"]` on the two `DecoderIdentityMismatchError` shapes, so a
#: caller branches on a string rather than on the prose of `message`.
REASON_IDENTITY_UNRECORDED: Final[str] = "identity_not_in_file"
REASON_IDENTITY_MISMATCH: Final[str] = "identity_mismatch"
REASON_SERIAL_MISMATCH: Final[str] = "serial_mismatch"

#: What every failed ending owes the operator, and the recovery that follows
#: it. Named once and shared by the verification table and by the enrichment
#: any other mid-stage failure gets, so a partially written decoder is
#: described the same way whichever error ended the run - the module docstring
#: promises "every failed ending says so", and two endings out of many is not
#: that promise.
NO_ROLLBACK: Final[str] = (
    "Nothing was rolled back: a partial rollback can leave a state worse than the one just "
    "measured, and the file plus this report already say which CVs were written"
)
RECOVERY: Final[str] = (
    "re-run the same `railctl restore` - it is idempotent and writes only what still differs"
)

WARNING_IDENTITY_DEGRADED: Final[str] = "restore.identity_degraded"
WARNING_IDENTITY_OVERRIDDEN: Final[str] = "restore.identity_overridden"
WARNING_FILE_INCOMPLETE: Final[str] = "restore.file_incomplete"

#: What an interrupted ndjson run exits with, read off the class exactly as
#: `commands/backup.py` does, so this file and `errors.py` cannot disagree.
_ABORTED_EXIT_CODE: Final[int] = exit_code_for(AbortedError.__new__(AbortedError))

# Built once, at import time - the same B008 note as every command module.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_FILE_ARG = typer_argument(RESTORE_FILE_ARG)
_DRY_RUN_OPT = typer_option(RESTORE_DRY_RUN_OPT)
_WITH_ADDRESS_OPT = typer_option(RESTORE_WITH_ADDRESS_OPT)
_MERGE_CV29_OPT = typer_option(RESTORE_MERGE_CV29_OPT)
_INCLUDE_SWEEP_OPT = typer_option(RESTORE_INCLUDE_SWEEP_OPT)
_ALLOW_INCOMPLETE_OPT = typer_option(RESTORE_ALLOW_INCOMPLETE_OPT)
_TRACK_OPT = typer_option(RESTORE_TRACK_OPT)
_CONFIRM_OPT = typer_option(RESTORE_CONFIRM_OPT)


@dataclass(frozen=True, slots=True)
class _Invocation:
    """The command line as typed, rebuildable as an argv array a suggestion
    can extend. Every refusal below hands back the whole invocation plus the
    one flag that would have let it through, so an agent gets a command that
    RUNS rather than a sentence to parse apart."""

    file: str
    dry_run: bool
    with_address: bool
    merge_cv29: bool
    include_sweep: bool
    allow_incomplete: bool
    track: str
    confirm_token: str | None
    typed_globals: tuple[str, ...]

    def argv(self, *extra: str) -> list[str]:
        # `--track` is deliberately never repeated: every suggestion this
        # command builds is for the programming track, which is the default
        # and the only accepted value, so the one invocation that typed the
        # flag gets it back dropped rather than echoed.
        argv = ["railctl", "restore", self.file]
        for option, given in (
            (RESTORE_DRY_RUN_OPT, self.dry_run),
            (RESTORE_WITH_ADDRESS_OPT, self.with_address),
            (RESTORE_MERGE_CV29_OPT, self.merge_cv29),
            (RESTORE_INCLUDE_SWEEP_OPT, self.include_sweep),
            (RESTORE_ALLOW_INCOMPLETE_OPT, self.allow_incomplete),
        ):
            if given:
                argv.append(option.name)
        if self.confirm_token is not None:
            argv += [RESTORE_CONFIRM_OPT.name, self.confirm_token]
        return [*argv, *self.typed_globals, *extra]


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything decided BEFORE the station opens - a refusal here costs no
    port, and both of this command's file-level refusals belong to it."""

    invocation: _Invocation
    path: Path
    document: BackupDocument

    @property
    def address(self) -> object:
        """The locomotive the FILE names. Never a read target: service mode
        acts on whatever stands on the programming track."""
        return self.document.loco.get("address")


def plan_invocation(invocation: _Invocation) -> _Plan:
    """Validate the invocation and read the file, station untouched.

    The order is the cheap-refusal order: the two usage errors first (no file
    is even opened for them), then the file, then the completeness rule. A
    file that does not parse is `read_backup`'s own `BackupFileError` - M9's
    reader is already strict, and a second opinion here would be a second
    definition of what a valid file is.
    """
    if invocation.track == TRACK_MAIN:
        raise UsageProblem(
            f"restore runs on the programming track only, so --track {TRACK_MAIN} is refused "
            f"for two reasons rather than accepted as a fast path: the identity gate that "
            f"keeps a CV set out of the wrong locomotive needs reads, and on this station a "
            f"main-track (POM) CV read returns nothing at all, so nothing could verify a "
            f"write either - a blind restore is exactly the failure this command exists to "
            f"prevent",
            suggestions=[invocation.argv()],
            details={"reason": "restore_on_main_track", "track": invocation.track},
        )
    check_choice("track", invocation.track, RESTORE_TRACK_OPT.enum or ())
    if invocation.with_address and invocation.merge_cv29:
        raise UsageProblem(
            "--with-address and --merge-cv29 ask for opposite things with CV29 bit 5: "
            "--with-address writes the file's byte whole (the file decides the address), "
            "--merge-cv29 keeps the decoder's own long-address bit (the decoder does). "
            "Letting one win silently would move a locomotive's address on the strength "
            "of an argument order",
            suggestions=[
                _without(invocation, merge_cv29=False).argv(),
                _without(invocation, with_address=False).argv(),
            ],
            details={"reason": "contradictory_cv29_flags"},
        )
    path = Path(invocation.file)
    document = read_backup(path)
    if not document.summary["complete"] and not invocation.allow_incomplete:
        summary = document.summary
        raise RestoreFileIncompleteError(
            f"{path} is an incomplete backup: {summary['no_response']} CV(s) did not answer "
            f"and {summary['error']} failed, out of {summary['requested']}. Those rows carry "
            f"no value and are never written either way - pass --allow-incomplete to restore "
            f"the {summary['ok']} that do",
            hint="or back the decoder up again and restore from a file with no holes",
            details={
                "path": str(path),
                **{key: summary[key] for key in ("requested", "ok", "no_response", "error")},
            },
        )
    return _Plan(invocation=invocation, path=path, document=document)


def _without(invocation: _Invocation, **changes: bool) -> _Invocation:
    """`invocation` with one flag turned off - the shape a suggestion needs.
    `dataclasses.replace` would do, but this names what the callers mean."""
    fields = {
        "file": invocation.file,
        "dry_run": invocation.dry_run,
        "with_address": invocation.with_address,
        "merge_cv29": invocation.merge_cv29,
        "include_sweep": invocation.include_sweep,
        "allow_incomplete": invocation.allow_incomplete,
        "track": invocation.track,
        "confirm_token": invocation.confirm_token,
        "typed_globals": invocation.typed_globals,
    }
    fields.update(changes)
    return _Invocation(**fields)  # type: ignore[arg-type]


# -- the identity gate (design C7 stage 0, M10 decision D2) -------------------


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """What the gate proved about the decoder now on the programming track.

    `serial is None` is the DEGRADED case and the only one: the file carried
    no serial bytes, so there was nothing to compare and the gate says so out
    loud instead of inventing a check over data the file does not have.
    `overridden` is `--confirm` having been accepted for a real mismatch,
    which is a legitimate operation (a replacement decoder) and a warning
    either way.
    """

    manufacturer_id: int
    decoder_type: int
    serial: tuple[int, ...] | None
    overridden: bool = False

    @property
    def degraded(self) -> bool:
        return self.serial is None


def serial_token(serial: Iterable[int]) -> str:
    """The `--confirm` token for a live serial: the raw bytes, dot-separated.

    A token, not a comparison key - the gate compares the BYTES (design C7:
    "the raw serial bytes are compared, never a composed string"). This
    string exists so the refusal can print something the operator can type
    back, and it is bound to the decoder in front of them: it cannot be
    guessed ahead of the refusal that prints it, which is what makes it a
    confirmation rather than a second `--yes`.
    """
    return ".".join(str(byte) for byte in serial)


def _read_identity(station: Station, cv: int, *, page: CvPage | None = None) -> int:
    """One live CV, or the station's own error with placement guidance.

    An identity CV that cannot be READ aborts the run rather than being
    guessed at - a gate that passes when the instrument is broken is the
    failure this whole project is organised against.

    `page` is `None` for every read that runs before the page is known - the
    identity CVs, the CV31/CV32 pair itself, CV144 - and the file's page for
    the verification read-backs. A service read ignores it today (see the
    module docstring on the read/write asymmetry), but a read-back that is
    not pinned to the bank its write went to would only be accidentally
    right, and issue #39 may make reads honour it.
    """
    try:
        return station.cv_read(cv, address=None, mode=ProgMode.SERVICE, page=page).value
    except RailctlError as exc:
        raise _with_guidance(exc, mode=ProgMode.SERVICE) from None


def check_identity(
    station: Station, document: BackupDocument, *, confirm_token: str | None
) -> IdentityCheck:
    """Refuse to write this file into anything but the decoder it came from.

    Runs once per command, before any write and before a `--dry-run` prints
    anything: a dry run that skipped the gate would report a plan for the
    wrong locomotive, which is worse than no plan at all.
    """
    decoder = document.decoder
    values: dict[str, int] = {}
    for field, cv in IDENTITY_FIELD_CVS:
        expected = decoder.get(field)
        if expected is None:
            raise DecoderIdentityMismatchError(
                f"the file records no decoder.{field}, so nothing can check that the "
                f"decoder on the programming track is the one it came from; a restore is "
                f"not run on an unidentified decoder",
                hint="back the decoder up again - a backup with holes in its decoder block "
                "identifies nothing",
                details={"reason": REASON_IDENTITY_UNRECORDED, "field": field, "cv": cv},
            )
        live = _read_identity(station, cv)
        if live != expected:
            raise DecoderIdentityMismatchError(
                f"CV{cv} reads {live} and the file records decoder.{field} = {expected}: this "
                f"is not the decoder the file was taken from. Service mode writes whatever "
                f"stands on the programming track, and no flag overrides this check",
                hint="put the locomotive the file names on the programming track",
                details={
                    "reason": REASON_IDENTITY_MISMATCH,
                    "field": field,
                    "cv": cv,
                    "file": expected,
                    "live": live,
                },
            )
        values[field] = live
    return _check_serial(station, decoder, values, confirm_token=confirm_token)


def _check_serial(
    station: Station,
    decoder: Mapping[str, object],
    values: Mapping[str, int],
    *,
    confirm_token: str | None,
) -> IdentityCheck:
    """The overridable half of the gate.

    A file with no `serial_bytes` is a legitimate hole - the block omits what
    did not answer - so the gate degrades to a warning rather than inventing
    a check over data that is not there. Where the file does carry them, the
    three live bytes must match; `--confirm=<the serial just read>` is the
    only way past a mismatch, and `--yes` deliberately is not.
    """
    recorded = decoder.get(SERIAL_FIELD)
    if recorded is None:
        return IdentityCheck(
            manufacturer_id=values["manufacturer_id"],
            decoder_type=values["decoder_type"],
            serial=None,
        )
    # The reader has already held this to three bytes; `file.py` refuses a
    # `serial_bytes` of any other shape, so nothing re-validates it here.
    expected: tuple[int, ...] = tuple(recorded)  # type: ignore[call-overload]
    live = tuple(_read_identity(station, cv) for cv in SERIAL_CVS)
    token = serial_token(live)
    if live == expected or confirm_token == token:
        return IdentityCheck(
            manufacturer_id=values["manufacturer_id"],
            decoder_type=values["decoder_type"],
            serial=live,
            overridden=live != expected,
        )
    raise DecoderIdentityMismatchError(
        f"the decoder serial reads {token} (CV251-253) and the file records "
        f"{serial_token(expected)}: same manufacturer and same decoder type, different "
        f"decoder. Copying a file onto a replacement decoder is a real thing to want, so "
        f"this one can be confirmed - but only by naming the serial just read",
        hint=f"rerun with --confirm={token} if this is deliberate; --yes does not answer this",
        details={
            "reason": REASON_SERIAL_MISMATCH,
            "file": list(expected),
            "live": list(live),
            "confirm_token": token,
        },
    )


# -- the run ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One CV that did not read back what its stage intended, after the retry."""

    row: PlannedWrite
    read: int


def live_probe_cvs(
    records: Iterable[CvRecord], catalog: Mapping[int, CatalogEntry], *, include_sweep: bool
) -> list[int]:
    """The CVs whose live value is read before the plan is built, ascending.

    A superset of what the plan strictly needs, on purpose: the address CVs
    and CV29 are read even on a run that will skip them, so the report can
    say what the decoder holds for the CVs it is NOT writing - which is the
    whole value of the skipped rows in the table. What is left out is what
    could never be written whatever the flags: a row that did not read `ok`
    in the file (no value to compare against), and a CV the catalog marks
    unrestorable. The never-write set is read off the catalog through
    `never_write_cvs`, never listed a second time here.
    """
    never_write = never_write_cvs(catalog)
    return sorted(
        record.cv
        for record in records
        if record.status is ReadStatus.OK
        and record.cv not in never_write
        and (record.cv in catalog or include_sweep)
    )


class _RestoreRun:
    """One run's state, shared by the buffered and the ndjson paths so a
    `finally` can still say what was planned, written and verified."""

    def __init__(
        self,
        plan: _Plan,
        catalog: Mapping[int, CatalogEntry],
        *,
        on_start: Callable[[_RestoreRun], None] | None = None,
        on_cv: Callable[[PlannedWrite], None] | None = None,
        on_stage: Callable[[str, Sequence[PlannedWrite], Sequence[Mismatch]], None] | None = None,
    ) -> None:
        self.plan = plan
        self._catalog = catalog
        self._on_start = on_start
        self._on_cv = on_cv
        self._on_stage = on_stage
        self.identity: IdentityCheck | None = None
        self.page: CvPage | None = None
        self.rows: list[PlannedWrite] = []
        self.written: list[PlannedWrite] = []
        self.verified: list[PlannedWrite] = []
        self.mismatches: list[Mismatch] = []
        self.stages_completed: list[str] = []

    # -- the plan -------------------------------------------------------------
    @property
    def writes(self) -> list[PlannedWrite]:
        return [row for row in self.rows if row.action == ACTION_WRITE]

    def execute(self, station: Station, output: OutputContext, settings: Settings) -> None:
        print(PROG_TRACK_NOTICE, file=output.stderr)
        self.identity = check_identity(
            station, self.plan.document, confirm_token=self.plan.invocation.confirm_token
        )
        self._require_page(station)
        prior = self._require_unlocked(station, self.identity.decoder_type)
        live = self._read_live(station, prior)
        # Raises `AddressSetIncompleteError` (9) and `CvOutOfRangeError` (15)
        # on its own, both before the caller has written anything.
        self.rows = plan_restore(
            self.plan.document.cvs,
            live,
            self._catalog,
            station.capabilities,
            with_address=self.plan.invocation.with_address,
            merge_cv29=self.plan.invocation.merge_cv29,
            include_sweep=self.plan.invocation.include_sweep,
        )
        if self._on_start is not None:
            self._on_start(self)
        if self.plan.invocation.dry_run:
            # No writes at all - not the CVs, and not the CV31/CV32
            # selectors a real run writes to select the file's page: this
            # path only reads, and a service read selects nothing.
            for row in self.writes:
                self._emit(row)
            return
        self._confirm(settings, output)
        try:
            self._execute_stages(station)
        except KeyboardInterrupt:
            raise self._aborted() from None

    def _require_page(self, station: Station) -> None:
        """Live CV31/CV32 must equal the pair the file was taken on.

        No write is performed to REACH that state (design C7 precondition 5):
        a restore that silently moved the index bank would write every CV
        above 256 into a different set of registers than the ones it read.
        What the stages then do write is the same pair the decoder is already
        holding - the station selects the page before each indexed write, and
        this check is what makes that selection a no-op in decoder terms
        rather than a bank change nobody asked for.
        """
        live = tuple(_read_identity(station, cv) for cv in PAGE_SELECTOR_CVS)
        recorded = tuple(self.plan.document.page)
        if live != recorded:
            raise IndexPageRequiredError(
                f"the decoder sits on CV page CV31={live[0]} CV32={live[1]} and the file was "
                f"taken on CV31={recorded[0]} CV32={recorded[1]}; the curated CVs above 256 "
                f"do not name the same registers on the two banks, and a restore never MOVES "
                f"the bank - it writes only the page the decoder already holds",
                hint=(
                    "put the decoder back on the page the file names, or back it up again "
                    "on the bank it is on now"
                ),
                details={"live": list(live), "file": list(recorded)},
            )
        self.page = (live[0], live[1])

    def _require_unlocked(self, station: Station, decoder_type: int) -> dict[int, int]:
        """CV144 must read 0 - but only on a family that reads it as the lock.

        Returns the live values this check already read, so the probe pass
        below does not pay for CV144 twice. On the MS family (and on an
        unread decoder type, which cannot happen here because the gate above
        just read one) CV144 is the confirmation jingle and no precondition
        at all - `station.treats_cv144_as_lock` owns that decision, and
        `decoder_type` comes from the gate rather than being re-read.
        """
        if not treats_cv144_as_lock(decoder_type):
            return {}
        value = _read_identity(station, CV144)
        if value != CV144_UNLOCKED:
            raise ProgrammingLockedError(
                f"CV144 reads {value} on a decoder of type {decoder_type}, where CV144 is "
                f"the programming lock: every write in this run would be refused by the "
                f"decoder, verification included. The lock is cleared by whoever set it, "
                f"not by this tool",
                hint=f"clear it with `railctl cv write 144 {CV144_UNLOCKED}` and rerun",
                details={"cv": CV144, "live": value, "decoder_type": decoder_type},
            )
        return {CV144: value}

    def _read_live(self, station: Station, prior: Mapping[int, int]) -> dict[int, int | None]:
        """Every live value the plan compares against, in one batch.

        A CV that does not answer stays `None`, and `plan_restore` reads that
        as "not known" - which makes it a write with the reason saying so,
        never an `unchanged` inferred from silence.
        """
        wanted = live_probe_cvs(
            self.plan.document.cvs,
            self._catalog,
            include_sweep=self.plan.invocation.include_sweep,
        )
        specs = [CvSpec(cv=cv, name=self._name(cv)) for cv in wanted if cv not in prior]
        live: dict[int, int | None] = dict(prior)
        for outcome in station.cv_read_many(specs, address=None, mode=ProgMode.SERVICE):
            live[outcome.spec.cv] = None if outcome.result is None else outcome.result.value
        return live

    def _name(self, cv: int) -> str:
        entry = self._catalog.get(cv)
        return entry.slug if entry is not None else ""

    def _confirm(self, settings: Settings, output: OutputContext) -> None:
        writes = self.writes
        seconds = int(len(writes) * OPERATIONS_PER_WRITE * SECONDS_PER_CV)
        confirm(
            f"restore {len(writes)} CV(s) from {self.plan.path} onto the locomotive standing "
            f"on the programming track; the file was taken from locomotive "
            f"{self.plan.address}. About {seconds} s: each CV costs a write and a read-back, "
            f"at {SECONDS_PER_CV} s per operation (measured 2026-08-13, "
            f"docs/probe-results.md). Nothing is ever rolled back. Proceed",
            settings=settings,
            stdin=sys.stdin,
            stderr=output.stderr,
            retry_argv=self.plan.invocation.argv(),
        )

    # -- the stages -----------------------------------------------------------
    def _execute_stages(self, station: Station) -> None:
        """The four stages in order, each fully verified before the next.

        A stage with a surviving mismatch ends the run: a decoder that did
        not retain stage A is not one to write stage B into, and the table
        the operator needs is the one that says where it stopped.
        """
        for stage in STAGES:
            rows = [row for row in self.writes if row.stage == stage]
            if not rows:
                continue
            try:
                for row in rows:
                    self._write(station, row)
                    self.written.append(row)
                    self._emit(row)
                mismatches = self._verify(station, rows)
            except RailctlError as exc:
                # Every OTHER way a stage can end. The verification table
                # below is raised outside this block on purpose: it already
                # carries the report, and enriching it twice would append the
                # same sentence to its own message.
                raise self._partial_failure(exc, stage) from None
            if self._on_stage is not None:
                self._on_stage(stage, rows, mismatches)
            if mismatches:
                self.mismatches = mismatches
                raise self._verify_failed(stage, rows)
            self.stages_completed.append(stage)

    def _write(self, station: Station, row: PlannedWrite) -> None:
        # `verify=False`: this command verifies the whole stage against the
        # INTENDED value, with one retry, and reports one table. The
        # station's own read-back raises on the first CV instead.
        #
        # `page=self.page` unconditionally, never behind a test on the CV
        # number: below CV257 `ensure_page` returns before it looks at the
        # argument, and above it a write with no page is refused outright by
        # `IndexPageRequiredError` - which is what used to end a restore
        # part-way through stage A on the fourteen curated CVs above 256.
        # `_require_page` has already run, so `self.page` is the file's pair
        # and the decoder is already holding it.
        station.cv_write(
            row.num,
            row.new_value,
            address=None,
            mode=ProgMode.SERVICE,
            page=self.page,
            verify=False,
        )

    def _verify(self, station: Station, rows: Sequence[PlannedWrite]) -> list[Mismatch]:
        """Re-read every CV this stage wrote, compare against `new_value`.

        `new_value`, never `file_value`: a merged CV29 deliberately did not
        copy the file's byte, and comparing against the file would report the
        merge as a failure.

        One retry and one re-read, then stop. A decoder that ignored the same
        write twice will not take it on the third attempt, and a loop here is
        how a restore spends an hour failing.
        """
        mismatches: list[Mismatch] = []
        for row in rows:
            read = _read_identity(station, row.num, page=self.page)
            if read != row.new_value:
                self._write(station, row)
                read = _read_identity(station, row.num, page=self.page)
            if read == row.new_value:
                self.verified.append(row)
            else:
                mismatches.append(Mismatch(row=row, read=read))
        return mismatches

    def _progress(self, stage: str) -> dict[str, object]:
        """What the executor knows and a station error cannot: which CVs went
        out, which read back, and how far the run got before it stopped."""
        return {
            "stage": stage,
            "written": [row.num for row in self.written],
            "verified": [row.num for row in self.verified],
            "stages_completed": list(self.stages_completed),
        }

    def _partial_failure(self, exc: RailctlError, stage: str) -> RailctlError:
        """Any other station failure, carrying the partial-write report.

        `CvVerifyError` and `AbortedError` said what had already been written
        and nothing else did, so a `RailctlError` out of a write or out of a
        verification read left the operator of a half-written decoder with a
        single-CV verdict and no idea what had gone out before it.

        Enriched in place rather than re-raised as something new: the class,
        the `code` and therefore the exit code stay the station's - its
        verdict on WHAT went wrong is still the verdict, and only the details
        grow. For the same reason the message is extended rather than
        replaced, and the station's own hint (the programming-track placement
        guidance, for one) keeps its place ahead of the recovery.
        """
        exc.args = (
            f"{exc}. Stage {stage} of the restore stopped here: {len(self.written)} CV(s) had "
            f"already been written and {len(self.verified)} verified. {NO_ROLLBACK}",
        )
        exc.details = {**exc.details, **self._progress(stage)}
        exc.hint = RECOVERY if exc.hint is None else f"{exc.hint}; {RECOVERY}"
        return exc

    def _verify_failed(self, stage: str, rows: Sequence[PlannedWrite]) -> CvVerifyError:
        listed = "; ".join(
            f"CV{m.row.num} {m.row.name} intended {m.row.new_value}, reads {m.read}"
            for m in self.mismatches
        )
        return CvVerifyError(
            f"stage {stage}: {len(self.mismatches)} of {len(rows)} CV(s) did not read back "
            f"what was written, each after one retry - {listed}. {NO_ROLLBACK}",
            hint=(
                f"{RECOVERY}; a CV that fails twice is a decoder or contact problem, not a "
                f"file problem"
            ),
            details={
                **self._progress(stage),
                "mismatches": [
                    {
                        "cv": m.row.num,
                        "name": m.row.name,
                        "stage": m.row.stage,
                        "intended": m.row.new_value,
                        "read": m.read,
                    }
                    for m in self.mismatches
                ],
            },
        )

    def _aborted(self) -> AbortedError:
        return AbortedError(
            f"interrupted after {len(self.written)} CV(s) were written and "
            f"{len(self.verified)} verified; nothing is rolled back - re-run the same "
            f"`railctl restore` to finish it",
            details={
                "written": [row.num for row in self.written],
                "verified": [row.num for row in self.verified],
                "stages_completed": list(self.stages_completed),
            },
        )

    def _emit(self, row: PlannedWrite) -> None:
        if self._on_cv is not None:
            self._on_cv(row)


# -- the three renderings -----------------------------------------------------


def row_json(row: PlannedWrite) -> dict[str, object]:
    """One plan row as JSON. Public because `commands/diff.py` renders the same
    rows from the same planner: two spellings of one table would let a consumer
    key on `cv` in one envelope and on `num` in the other."""
    return {
        "cv": row.num,
        "name": row.name,
        "stage": row.stage,
        "file_value": row.file_value,
        "live_value": row.live_value,
        "new_value": row.new_value,
        "action": row.action,
        "reason": row.reason,
    }


def row_line(row: PlannedWrite) -> str:
    """The human form of one plan row, shared with `diff` for the same reason
    `row_json` is: the arrow reads `what is there -> what the file says` in
    both commands, because one planner decided both."""
    label = f"CV{row.num} {row.name}" if row.name else f"CV{row.num}"
    if row.action == ACTION_WRITE:
        live = "unread" if row.live_value is None else str(row.live_value)
        return f"{label}: {live} -> {row.new_value} (stage {row.stage})"
    if row.action == "unchanged":
        return f"{label}: {row.live_value} unchanged"
    return f"{label}: {row.action} - {row.reason}"


def _identity_json(identity: IdentityCheck) -> dict[str, object]:
    """Not optional: a result exists only for a run whose gate passed, and
    `execute` sets `identity` before anything else in a run can fail."""
    return {
        "manufacturer_id": identity.manufacturer_id,
        "decoder_type": identity.decoder_type,
        "serial": None if identity.serial is None else list(identity.serial),
        "serial_checked": not identity.degraded,
        "serial_overridden": identity.overridden,
    }


def counts_for(rows: Iterable[PlannedWrite]) -> dict[str, int]:
    """One count per `ACTIONS` word, zeros included - a caller reads four keys
    off every restore result, whatever the file happened to contain."""
    planned = list(rows)
    return {action: sum(1 for row in planned if row.action == action) for action in ACTIONS}


def build_restore(state: _RestoreRun) -> CommandResult:
    """One result, three renderings: the plan is the result, and a real run
    adds what was written and verified. Skipped rows are IN the table - a skip
    nobody can see reads as a CV nobody considered, and "CV1/CV17/CV18/CV29
    are skipped" is the sentence this command is judged on."""
    plan = state.plan
    invocation = plan.invocation
    counts = counts_for(state.rows)
    result = CommandResult(schema=RESTORE_SCHEMA, command="restore")
    result.result = {
        "file": str(plan.path),
        "dry_run": invocation.dry_run,
        "track": TRACK_PROG,
        "mode": ProgMode.SERVICE.value,
        "loco": dict(plan.document.loco),
        "page": None if state.page is None else list(state.page),
        "identity": _identity_json(state.identity),
        "options": {
            "with_address": invocation.with_address,
            "merge_cv29": invocation.merge_cv29,
            "include_sweep": invocation.include_sweep,
            "allow_incomplete": invocation.allow_incomplete,
        },
        "planned": len(state.rows),
        "counts": counts,
        "written": len(state.written),
        "verified": len(state.verified),
        "stages_completed": list(state.stages_completed),
        "cvs": [row_json(row) for row in state.rows],
    }
    for row in state.rows:
        result.say(row_line(row))
    result.say(
        f"{counts['write']} to write, {counts['unchanged']} unchanged, "
        f"{counts['skip']} skipped, {counts['unreadable']} unreadable"
    )
    if invocation.dry_run:
        result.say("dry run: nothing was written")
    else:
        result.say(f"written and verified: {len(state.verified)} of {len(state.written)}")
    return result


def attach_warnings(outcome: CommandResult, state: _RestoreRun) -> None:
    """The three things a successful restore still has to say out loud."""
    # `execute` sets `identity` before anything else in a run can fail, and
    # warnings are attached only to a run that finished, so this is never None.
    identity = state.identity
    if identity.degraded:
        outcome.warn(
            WARNING_IDENTITY_DEGRADED,
            f"the file carries no {SERIAL_FIELD}, so the serial half of the identity gate "
            f"did not run; CV8 = {identity.manufacturer_id} and CV250 = "
            f"{identity.decoder_type} did match",
            manufacturer_id=identity.manufacturer_id,
            decoder_type=identity.decoder_type,
        )
    if identity.overridden:
        outcome.warn(
            WARNING_IDENTITY_OVERRIDDEN,
            f"--confirm accepted a serial mismatch: this decoder's serial is "
            f"{serial_token(identity.serial or ())} and the file was taken from another "
            f"decoder of the same type",
            serial=list(identity.serial or ()),
        )
    if state.plan.invocation.allow_incomplete:
        summary = state.plan.document.summary
        outcome.warn(
            WARNING_FILE_INCOMPLETE,
            f"--allow-incomplete: {summary['no_response']} CV(s) in this file did not answer "
            f"and {summary['error']} failed when it was taken, and carry no value to write",
            no_response=summary["no_response"],
            error=summary["error"],
        )


class _EventLog(StationEventLog):
    """`StationEventLog` minus the one event this command owns the answer to.

    See `SUPPRESSED_EVENT`: every write goes out with `verify=False` because
    the stage verifies them together, so publishing the station's per-write
    "nothing checked this" would contradict the read-back that ran one stage
    later.
    """

    def __call__(self, name: str, payload: dict[str, object]) -> None:
        if name == SUPPRESSED_EVENT:
            return
        super().__call__(name, payload)


def _work(settings: Settings, output: OutputContext, invocation: _Invocation) -> CommandResult:
    plan = plan_invocation(invocation)
    catalog = load_catalog()
    events = _EventLog()
    station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
    try:
        state = _RestoreRun(plan, catalog)
        state.execute(station, output, settings)
        outcome = build_restore(state)
        attach_warnings(outcome, state)
        events.attach_to(outcome)
        outcome.link = link_info(station, settings)
        outcome.station = station_info(station)
    except BaseException:
        close_quietly(station)
        raise
    return close_after(station, outcome)


def _write_error(report: ErrorReport, output: OutputContext) -> None:
    """One JSON object on stderr - the shape `render_error` gives every other
    command, written here because the ndjson path deliberately never calls
    `run()` (same reasoning as `commands/backup.py`)."""
    output.stderr.write(json.dumps(report.envelope(), separators=(",", ":")) + "\n")


def _summary_fields(state: _RestoreRun) -> dict[str, object]:
    """The stream summary's counts, read off the run - all zeros when the
    station opened and nothing was ever planned, because the run object is
    built before the station and simply has empty lists then."""
    return {
        "planned": len(state.rows),
        **counts_for(state.rows),
        "written": len(state.written),
        "verified": len(state.verified),
        "mismatches": len(state.mismatches),
    }


def _run_ndjson(settings: Settings, output: OutputContext, invocation: _Invocation) -> NoReturn:
    """The streaming path, bypassing `run()` exactly as `backup` does: once
    the station is open, EVERY ending finishes the stream with one `summary`
    line carrying the same exit code the process leaves with. A refusal before
    the station opens - both usage errors, an unreadable file, an incomplete
    one - produces no stream at all, only the error envelope on stderr."""
    stream = NdjsonStream(output.stdout)

    def on_event(name: str, payload: dict[str, object]) -> None:
        if name == SUPPRESSED_EVENT:
            return
        stream.event("event", name=name, details=_jsonable(dict(payload)))

    def on_start(state: _RestoreRun) -> None:
        stream.event(
            "start",
            schema=RESTORE_SCHEMA,
            file=str(state.plan.path),
            address=state.plan.address,
            dry_run=state.plan.invocation.dry_run,
            planned=len(state.rows),
            writes=len(state.writes),
        )

    def on_cv(row: PlannedWrite) -> None:
        stream.event("cv", **row_json(row))

    def on_stage(stage: str, rows: Sequence[PlannedWrite], mismatches: Sequence[Mismatch]) -> None:
        stream.event(
            "stage",
            stage=stage,
            written=len(rows),
            verified=len(rows) - len(mismatches),
            mismatches=[
                {"cv": m.row.num, "intended": m.row.new_value, "read": m.read} for m in mismatches
            ],
        )

    station = None
    state: _RestoreRun | None = None
    exit_code = 0
    try:
        plan = plan_invocation(invocation)
        catalog = load_catalog()
        state = _RestoreRun(plan, catalog, on_start=on_start, on_cv=on_cv, on_stage=on_stage)
        station = open_station(settings, capabilities_path=capabilities_path(), on_event=on_event)
        state.execute(settings=settings, station=station, output=output)
    except KeyboardInterrupt:
        # An interrupt the executor never saw - during open, or before the
        # first write. `execute` turns the ones it does see into AbortedError,
        # which arrives below with the same exit code.
        exit_code = _ABORTED_EXIT_CODE
    except ValueError as exc:
        exit_code = USAGE_EXIT_CODE
        _write_error(usage_report(exc), output)
    except RailctlError as exc:
        exit_code = exit_code_for(exc)
        _write_error(report_for(exc, command="restore"), output)
    finally:
        # `state` is built before the station opens, so once a summary is
        # owed there is always a run to read it off.
        if station is not None and state is not None:
            stream.summary(**_summary_fields(state), exit_code=exit_code)
            close_quietly(station)
    raise typer.Exit(code=exit_code)


def register(app: typer.Typer) -> None:
    """Attach `restore` between `backup` and `schema`, where `_meta.COMMANDS`
    puts it. Declares the eight global options a second time like every
    registered command (Click parses a group's options only before the
    subcommand name)."""

    @app.command("restore", help=_RESTORE_META.help, epilog=help_epilog(_RESTORE_META))
    def restore_command(
        ctx: typer.Context,
        file: str = _FILE_ARG,
        dry_run: bool = _DRY_RUN_OPT,
        with_address: bool = _WITH_ADDRESS_OPT,
        merge_cv29: bool = _MERGE_CV29_OPT,
        include_sweep: bool = _INCLUDE_SWEEP_OPT,
        allow_incomplete: bool = _ALLOW_INCOMPLETE_OPT,
        track: str = _TRACK_OPT,
        confirm_token: str | None = _CONFIRM_OPT,
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
        # registration order - the same block every command with argv
        # suggestions carries, so a refusal's suggestion keeps the whole
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
        invocation = _Invocation(
            file=file,
            dry_run=dry_run,
            with_address=with_address,
            merge_cv29=merge_cv29,
            include_sweep=include_sweep,
            allow_incomplete=allow_incomplete,
            track=track,
            confirm_token=confirm_token,
            typed_globals=tuple(typed_globals),
        )
        if output.fmt == "ndjson":
            _run_ndjson(settings, output, invocation)
        run("restore", output, lambda: _work(settings, output, invocation))
