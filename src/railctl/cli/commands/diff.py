# src/railctl/cli/commands/diff.py
"""`railctl diff` - a backup file against the decoder, or against another file.

Two forms, one comparison. `railctl diff FILE` reads the live CVs and compares
them; `railctl diff FILE FILE2` puts the second file where the decoder would
have been and never opens a link at all. Both go through
`railctl.backup.plan_restore`, the same pure planner `restore --dry-run` uses -
there is deliberately no second comparator in this file, because two functions
answering "does the decoder match the file?" is how `diff` and `restore` come
to disagree about a CV while both look right on their own.

**`diff` exits 0 whenever the comparison completed, however many CVs differ.**
It deliberately does NOT mirror `diff(1)`'s exit 1 on a difference: every
non-zero code in this tool is an exception, and inventing an exception for "the
answer is yes" would make the exit table lie. The answer is `result.differences`
in the payload - beside `result.not_read`, which says how much of the decoder
never answered - and a caller branches on those.

Four more properties are load-bearing here rather than emergent:

* **the two-file form opens no link.** Not "does not usually" - the station is
  never constructed on that path, and `tests/cli/test_diff.py` installs a
  `Station.open` that raises to prove it. A file-to-file comparison that woke
  the port would be a diff that fails on a laptop with nothing plugged in,
  which is most of the times anyone wants one;
* **nothing is written, on either form.** Not a CV, and not the CV31/CV32
  index selectors - which is why a decoder sitting on a different page than the
  file was taken on is a refusal (exit 17) rather than a re-selection: the
  curated CVs above 256 do not name the same registers on two banks, so
  comparing them across a page boundary would report differences that are only
  a change of subject. The same check runs offline between the two files'
  recorded pages;
* **a CV that did not answer is not a difference.** `plan_restore` calls it a
  write, and for a restore that is right - a write cannot be ruled out
  unnecessary against a value nobody read. A diff is asked a different
  question, so those rows are counted under `not_read` and warned about
  separately. Counting them as differences would report silence as a measured
  disagreement, which is the same mistake as recording a capability as absent
  because nothing answered;
* **the identity gate does not run here.** It is `restore`'s, and it exists to
  keep a CV set out of the wrong locomotive - a question only a write can get
  wrong. A read compares whatever is on the track and says what it found, and
  the file's `decoder` block is in the payload for a caller who wants to judge
  that for themselves.

Like `restore` this reads on the programming track alone, and for the same
measured reason: on this station a POM CV read returns nothing at all
(docs/probe-results.md R1), so a main-track diff would report every CV as
"live value not known". There is no `--track` here to refuse - `cv read` is the
command that offers the main track, and it answers for it.

The plan-shaping flags are `restore`'s three, with `restore`'s meanings, so the
two commands cannot describe the same decoder differently: without
`--with-address` the four address CVs are reported as not compared, with both
values still in the row.

NDJSON streams the same way `backup` and `restore` do, bypassing `run()`:
`start` once the files are read, one `cv` line per row, and a `summary` line
LAST even on failure. A refusal before `start` - a contradictory flag pair, an
unreadable file - produces no stream at all, only the error envelope on stderr.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import typer

from railctl.backup import BackupDocument, PlannedWrite, plan_restore, read_backup
from railctl.catalog import load_catalog
from railctl.cli._errors import OutputContext, report_for, run, usage_report
from railctl.cli._meta import (
    DIFF_FILE_ARG,
    DIFF_INCLUDE_SWEEP_OPT,
    DIFF_MERGE_CV29_OPT,
    DIFF_OTHER_ARG,
    DIFF_WITH_ADDRESS_OPT,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.commands.cv import PROG_TRACK_NOTICE, _with_guidance
from railctl.cli.commands.restore import (
    ACTION_WRITE,
    counts_for,
    live_probe_cvs,
    row_json,
    row_line,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import (
    StationEventLog,
    UsageProblem,
    _jsonable,
    close_after,
    close_quietly,
    link_info,
    merged_output,
    open_station,
    station_info,
)
from railctl.cli.render import NdjsonStream
from railctl.cli.result import USAGE_EXIT_CODE, CommandResult, ErrorReport
from railctl.errors import (
    AbortedError,
    IndexPageRequiredError,
    RailctlError,
    exit_code_for,
)
from railctl.station import PAGE_SELECTOR_CVS, Capabilities, CvPage, CvSpec, ProgMode

if TYPE_CHECKING:
    from railctl.catalog import CatalogEntry
    from railctl.cli.deps import Settings
    from railctl.station import Station

_DIFF_META = command_meta("diff")

#: Read off the metadata row, never retyped: the manifest says what this
#: command emits and the envelope says what it emitted.
DIFF_SCHEMA: Final[str] = _DIFF_META.schema

#: What the file was compared against, published as `result.source` so a
#: consumer branches on a word instead of on whether `other` happens to be null.
SOURCE_DECODER: Final[str] = "decoder"
SOURCE_FILE: Final[str] = "file"

#: The two `PlannedWrite.action` words that mean "this row was never compared":
#: the planner declined to plan it at all, so neither "differs" nor "matches"
#: is a claim this command may make about it. Counted and printed separately
#: for exactly that reason - a headline "0 differences" over four silently
#: uncompared address CVs is the reading this command must not permit.
NOT_COMPARED_ACTIONS: Final[tuple[str, ...]] = ("skip", "unreadable")

WARNING_NOT_COMPARED: Final[str] = "diff.not_compared"
WARNING_NOT_READ: Final[str] = "diff.not_read"

#: What an interrupted ndjson run exits with, read off the class exactly as
#: `commands/backup.py` and `commands/restore.py` do, so this file and
#: `errors.py` cannot disagree. Reachable here because the online form reads
#: every curated CV and that costs about 6 s each (MEASURED 2026-08-13,
#: docs/probe-results.md, "A backup costs 6 s per CV") - long enough that
#: Ctrl-C is a normal way for this command to end.
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

_FILE_ARG = typer_argument(DIFF_FILE_ARG)
_OTHER_ARG = typer_argument(DIFF_OTHER_ARG)
_WITH_ADDRESS_OPT = typer_option(DIFF_WITH_ADDRESS_OPT)
_MERGE_CV29_OPT = typer_option(DIFF_MERGE_CV29_OPT)
_INCLUDE_SWEEP_OPT = typer_option(DIFF_INCLUDE_SWEEP_OPT)


@dataclass(frozen=True, slots=True)
class _Invocation:
    """The command line as typed, rebuildable as an argv array a suggestion can
    extend - the same shape `restore` carries, for the same reason: a refusal
    hands back the whole invocation with one flag changed, so an agent gets a
    command that RUNS rather than a sentence to parse apart."""

    file: str
    other: str | None
    with_address: bool
    merge_cv29: bool
    include_sweep: bool
    typed_globals: tuple[str, ...]

    def argv(self, *extra: str) -> list[str]:
        argv = ["railctl", "diff", self.file]
        if self.other is not None:
            argv.append(self.other)
        for option, given in (
            (DIFF_WITH_ADDRESS_OPT, self.with_address),
            (DIFF_MERGE_CV29_OPT, self.merge_cv29),
            (DIFF_INCLUDE_SWEEP_OPT, self.include_sweep),
        ):
            if given:
                argv.append(option.name)
        return [*argv, *self.typed_globals, *extra]


@dataclass(frozen=True, slots=True)
class _Comparison:
    """Both sides of the comparison, resolved before a link could be opened.

    `other is None` is the whole difference between the two forms, and it is
    read exactly once - in `_work` and `_run_ndjson`, to decide whether a
    station is constructed at all.
    """

    invocation: _Invocation
    path: Path
    document: BackupDocument
    other_path: Path | None
    other: BackupDocument | None

    @property
    def source(self) -> str:
        return SOURCE_DECODER if self.other is None else SOURCE_FILE


def plan_invocation(invocation: _Invocation) -> _Comparison:
    """Validate the invocation and read the file(s), station untouched.

    The usage error comes first, so a contradictory flag pair costs no file
    read and, on the online form, no port. A file that does not parse is
    `read_backup`'s own `BackupFileError`: M9's reader is already strict, and a
    second opinion here would be a second definition of a valid file.
    """
    if invocation.with_address and invocation.merge_cv29:
        raise UsageProblem(
            "--with-address and --merge-cv29 ask for opposite things with CV29 bit 5: "
            "--with-address compares the file's byte whole, --merge-cv29 compares it with "
            "the decoder's own long-address bit standing in for the file's. Letting one "
            "win silently would report CV29 as matching or differing on the strength of an "
            "argument order",
            suggestions=[
                _without(invocation, merge_cv29=False).argv(),
                _without(invocation, with_address=False).argv(),
            ],
            details={"reason": "contradictory_cv29_flags"},
        )
    path = Path(invocation.file)
    other_path = None if invocation.other is None else Path(invocation.other)
    return _Comparison(
        invocation=invocation,
        path=path,
        document=read_backup(path),
        other_path=other_path,
        other=None if other_path is None else read_backup(other_path),
    )


def _without(invocation: _Invocation, **changes: bool) -> _Invocation:
    """`invocation` with one flag turned off - the shape a suggestion needs."""
    fields = {
        "file": invocation.file,
        "other": invocation.other,
        "with_address": invocation.with_address,
        "merge_cv29": invocation.merge_cv29,
        "include_sweep": invocation.include_sweep,
        "typed_globals": invocation.typed_globals,
    }
    fields.update(changes)
    return _Invocation(**fields)  # type: ignore[arg-type]


# -- the comparison ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffResult:
    """One completed comparison: every row the planner produced, plus the page
    both sides were on."""

    comparison: _Comparison
    page: CvPage
    rows: tuple[PlannedWrite, ...]

    @property
    def differences(self) -> int:
        return differences_in(self.rows)

    @property
    def not_read(self) -> int:
        return len(unread_in(self.rows))

    @property
    def not_compared(self) -> int:
        return sum(1 for row in self.rows if row.action in NOT_COMPARED_ACTIONS)


def unread_in(rows: Iterable[PlannedWrite]) -> list[int]:
    """The CVs the planner would write and nothing could read, ascending in
    row order.

    `plan_restore` calls these `write` and is right to: a restore cannot rule
    out a write it has nothing to compare against. A diff is asked a different
    question, and for it silence is not an answer - which is this project's
    founding rule, one layer up from a capability: `null` is not `false`, and
    "no reply" is not "differs".
    """
    return [row.num for row in rows if row.action == ACTION_WRITE and row.live_value is None]


def differences_in(rows: Iterable[PlannedWrite]) -> int:
    """How many CVs answered, and answered with something other than the file's
    value.

    Read off the planner's own verdict rather than re-derived from
    `file_value != live_value`: `write` is exactly the word `plan_restore` uses
    for "what is there is not what the file says", and a second test here would
    be the second comparator this module exists without. On a merged CV29 the
    two would already disagree, because the file's long-address bit is
    deliberately not part of the question.

    The one place this command departs from that verdict is the row nothing
    could read. `plan_restore` calls it a write and this counts it under
    `not_read` instead - see `unread_in`.
    """
    return sum(1 for row in rows if row.action == ACTION_WRITE and row.live_value is not None)


def offline_live(document: BackupDocument) -> dict[int, int | None]:
    """The second file standing in for the decoder.

    A row the second file records as anything but `ok` carries no value, and
    `None` is precisely what `plan_restore` reads as "the live value is not
    known" - so a hole in FILE2 reports as an uncomparable row rather than as
    a difference against a zero nobody measured.
    """
    return {record.cv: record.value for record in document.cvs}


def offline_capabilities(document: BackupDocument) -> Capabilities:
    """Every capability `None`, under the identity the file recorded.

    `plan_restore` takes the capabilities the station measured, and on this
    path nothing has measured anything: the file's own `capabilities` block
    describes the station that took the BACKUP, on a day that may predate the
    doctor run. Replaying it here would let this process act on a `false`
    no instrument of its own reported, which is the one thing this project is
    organised against. The planner ignores capabilities in M10 in any case
    (`backup/plan.py`), so `unknown` costs nothing and claims nothing.
    """
    identity = document.link.get("identity")
    return Capabilities.unknown(identity if isinstance(identity, str) else "")


def compare_offline(
    comparison: _Comparison, other: BackupDocument, catalog: Mapping[int, CatalogEntry]
) -> DiffResult:
    """FILE against FILE2, with no station anywhere in the call.

    This function takes no `Station` and constructs none, which is what makes
    "the offline form never opens a link" a property of the code rather than of
    the order the branches happen to run in. `other` is passed in rather than
    read off `comparison`, so the caller's `is not None` check is the only
    place the two forms are told apart.
    """
    page = _require_same_page(comparison, tuple(other.page), source=SOURCE_FILE)
    return _plan(comparison, offline_live(other), offline_capabilities(other), catalog, page)


def compare_online(
    comparison: _Comparison, catalog: Mapping[int, CatalogEntry], station: Station
) -> DiffResult:
    """FILE against the decoder on the programming track. Reads only."""
    page = _require_same_page(
        comparison,
        tuple(_read_live(station, cv) for cv in PAGE_SELECTOR_CVS),
        source=SOURCE_DECODER,
    )
    wanted = live_probe_cvs(
        comparison.document.cvs, catalog, include_sweep=comparison.invocation.include_sweep
    )
    specs = [CvSpec(cv=cv, name=_name(catalog, cv)) for cv in wanted]
    live: dict[int, int | None] = {}
    for outcome in station.cv_read_many(specs, address=None, mode=ProgMode.SERVICE):
        live[outcome.spec.cv] = None if outcome.result is None else outcome.result.value
    return _plan(comparison, live, station.capabilities, catalog, page)


def _plan(
    comparison: _Comparison,
    live: Mapping[int, int | None],
    caps: Capabilities,
    catalog: Mapping[int, CatalogEntry],
    page: CvPage,
) -> DiffResult:
    """The one call both forms make. `plan_restore` raises
    `AddressSetIncompleteError` (9) and `CvOutOfRangeError` (15) on its own, and
    both are answers about the FILE that hold whether or not anyone restores
    it, so this command lets them out rather than reporting a comparison it
    could only half make."""
    invocation = comparison.invocation
    rows = plan_restore(
        comparison.document.cvs,
        live,
        catalog,
        caps,
        with_address=invocation.with_address,
        merge_cv29=invocation.merge_cv29,
        include_sweep=invocation.include_sweep,
    )
    return DiffResult(comparison=comparison, page=page, rows=tuple(rows))


def _read_live(station: Station, cv: int) -> int:
    """One live CV, or the station's own error with placement guidance."""
    try:
        return station.cv_read(cv, address=None, mode=ProgMode.SERVICE).value
    except RailctlError as exc:
        raise _with_guidance(exc, mode=ProgMode.SERVICE) from None


def _name(catalog: Mapping[int, CatalogEntry], cv: int) -> str:
    entry = catalog.get(cv)
    return entry.slug if entry is not None else ""


def _require_same_page(comparison: _Comparison, live: tuple[int, ...], *, source: str) -> CvPage:
    """Both sides must be on the CV31/CV32 index page the file was taken on.

    A diff never writes the selectors, so a disagreement is a refusal and not a
    re-selection: the curated CVs above 256 (CV265, CV273, CV395 and the rest)
    map to different registers on different banks, and comparing them across
    that boundary would report differences that are only a change of subject.
    """
    recorded = tuple(comparison.document.page)
    if live != recorded:
        subject = (
            "the decoder sits" if source == SOURCE_DECODER else f"{comparison.other_path} was taken"
        )
        raise IndexPageRequiredError(
            f"{subject} on CV page CV31={live[0]} CV32={live[1]} and {comparison.path} was "
            f"taken on CV31={recorded[0]} CV32={recorded[1]}; the curated CVs above 256 do "
            f"not name the same registers on the two banks, and a diff never writes the "
            f"selectors",
            hint=(
                "compare files taken on the same page, or put the decoder back on the page "
                "the file names"
            ),
            details={"live": list(live), "file": list(recorded), "source": source},
        )
    return (recorded[0], recorded[1])


# -- the three renderings -------------------------------------------------------


def build_diff(diff: DiffResult) -> CommandResult:
    """One result, three renderings.

    Every row is in the table, whatever its action - a `skip` nobody can see
    reads as a CV nobody considered, and the four address CVs are skipped by
    default. `differences` is the headline; `counts` and the `not_compared`
    warning are what stop it being read as "identical".
    """
    comparison = diff.comparison
    invocation = comparison.invocation
    counts = counts_for(diff.rows)
    result = CommandResult(schema=DIFF_SCHEMA, command="diff")
    result.result = {
        "file": str(comparison.path),
        "other": None if comparison.other_path is None else str(comparison.other_path),
        "source": comparison.source,
        "loco": dict(comparison.document.loco),
        "decoder": dict(comparison.document.decoder),
        "page": list(diff.page),
        "options": {
            "with_address": invocation.with_address,
            "merge_cv29": invocation.merge_cv29,
            "include_sweep": invocation.include_sweep,
        },
        "differences": diff.differences,
        "not_read": diff.not_read,
        "counts": counts,
        "cvs": [row_json(row) for row in diff.rows],
    }
    against = (
        "the decoder on the programming track"
        if comparison.other is None
        else str(comparison.other_path)
    )
    result.say(f"{comparison.path} against {against}")
    for row in diff.rows:
        result.say(row_line(row))
    result.say(
        f"{diff.differences} differ, {counts['unchanged']} unchanged, "
        f"{diff.not_read} not read, {diff.not_compared} not compared"
    )
    if diff.not_read:
        result.warn(
            WARNING_NOT_READ,
            f"{diff.not_read} CV(s) did not answer, so this file and the decoder were not "
            f"compared on them at all; they are not counted as differences, because "
            f"silence is not a value that disagrees with anything",
            cvs=unread_in(diff.rows),
        )
    if diff.not_compared:
        result.warn(
            WARNING_NOT_COMPARED,
            f"{diff.not_compared} row(s) were not compared at all, so the difference count "
            f"is not a whole-file verdict; each row's reason says why, and --with-address "
            f"is what brings CV1, CV17, CV18 and CV29 into the comparison",
            cvs=[row.num for row in diff.rows if row.action in NOT_COMPARED_ACTIONS],
        )
    return result


def _work(settings: Settings, output: OutputContext, invocation: _Invocation) -> CommandResult:
    comparison = plan_invocation(invocation)
    catalog = load_catalog()
    other = comparison.other
    if other is not None:
        # The offline form, and the whole proof that it opens no link: there is
        # no `open_station` call on this branch to reach.
        return build_diff(compare_offline(comparison, other, catalog))
    events = StationEventLog()
    station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
    try:
        # After the open, never before: a target nothing can serve fails here,
        # and a notice printed first would leave the error envelope as the
        # SECOND thing on stderr, where nothing can parse it as one object.
        print(PROG_TRACK_NOTICE, file=output.stderr)
        outcome = build_diff(compare_online(comparison, catalog, station))
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


def _summary_fields(rows: Iterable[PlannedWrite]) -> dict[str, object]:
    """The stream summary's counts - all zeros when the comparison never got
    far enough to produce a row, because `rows` is then still the empty tuple
    the run started with."""
    planned = list(rows)
    return {
        "differences": differences_in(planned),
        "not_read": len(unread_in(planned)),
        **counts_for(planned),
    }


def _run_ndjson(settings: Settings, output: OutputContext, invocation: _Invocation) -> NoReturn:
    """The streaming path, bypassing `run()` exactly as `backup` and `restore`
    do. `start` goes out as soon as both sides are readable, and from that
    moment EVERY ending finishes the stream with one `summary` line carrying
    the same exit code the process leaves with. A refusal before that - the
    contradictory flag pair, an unreadable file - produces no stream at all,
    only the error envelope on stderr, so a consumer must key on `type` and
    never on line position."""
    stream = NdjsonStream(output.stdout)

    def on_event(name: str, payload: dict[str, object]) -> None:
        stream.event("event", name=name, details=_jsonable(dict(payload)))

    station = None
    started = False
    rows: tuple[PlannedWrite, ...] = ()
    exit_code = 0
    try:
        comparison = plan_invocation(invocation)
        catalog = load_catalog()
        stream.event(
            "start",
            schema=DIFF_SCHEMA,
            file=str(comparison.path),
            other=None if comparison.other_path is None else str(comparison.other_path),
            source=comparison.source,
        )
        started = True
        other = comparison.other
        if other is not None:
            rows = compare_offline(comparison, other, catalog).rows
        else:
            station = open_station(
                settings, capabilities_path=capabilities_path(), on_event=on_event
            )
            print(PROG_TRACK_NOTICE, file=output.stderr)
            rows = compare_online(comparison, catalog, station).rows
        for row in rows:
            stream.event("cv", **row_json(row))
    except KeyboardInterrupt:
        exit_code = _ABORTED_EXIT_CODE
    except ValueError as exc:
        exit_code = USAGE_EXIT_CODE
        _write_error(usage_report(exc), output)
    except RailctlError as exc:
        exit_code = exit_code_for(exc)
        _write_error(report_for(exc, command="diff"), output)
    finally:
        if started:
            stream.summary(**_summary_fields(rows), exit_code=exit_code)
        if station is not None:
            close_quietly(station)
    raise typer.Exit(code=exit_code)


def register(app: typer.Typer) -> None:
    """Attach `diff` between `restore` and `schema`, where `_meta.COMMANDS`
    puts it. Declares the eight global options a second time like every
    registered command (Click parses a group's options only before the
    subcommand name)."""

    @app.command("diff", help=_DIFF_META.help, epilog=help_epilog(_DIFF_META))
    def diff_command(
        ctx: typer.Context,
        file: str = _FILE_ARG,
        file2: str | None = _OTHER_ARG,
        with_address: bool = _WITH_ADDRESS_OPT,
        merge_cv29: bool = _MERGE_CV29_OPT,
        include_sweep: bool = _INCLUDE_SWEEP_OPT,
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
            other=file2,
            with_address=with_address,
            merge_cv29=merge_cv29,
            include_sweep=include_sweep,
            typed_globals=tuple(typed_globals),
        )
        if output.fmt == "ndjson":
            _run_ndjson(settings, output, invocation)
        run("diff", output, lambda: _work(settings, output, invocation))
