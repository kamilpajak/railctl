# src/railctl/cli/commands/backup.py
"""`railctl backup` - the CV backup (design C6, milestones M9 and M11).

The run order is the design's, verbatim: capabilities (already loaded by
`open_station`), the CV31/CV32 index selectors as singleton reads, CV29, the
identity CVs (CV7, CV8, CV250-253), then the rest of the planned list
ascending through `Station.cv_read_many`. Nothing read in an earlier step is
re-read.

`--all` (M11) changes exactly one thing about that: the planned list is every
CV inside `_sweep.sweep_bound` instead of the 77 the catalog names, so a
decoder's undocumented settings land in the file too. Everything else holds -
the same order, the same page gate, the same three-valued rows - and the file
says which set it is (`"set": "all"`, `sweep_range`, `source: sweep` on the
rows no catalog entry names). A sweep NORMALLY exits 9: most CV numbers are
not implemented in any decoder, this hardware cannot tell that from silence,
and nothing here special-cases the sweep to hide it.

Three properties are load-bearing here rather than emergent:

* **a backup never writes the decoder.** The one CV pair a read path could
  write - the CV31/CV32 page selectors - is exactly what this command refuses
  to touch: a pair outside `NEUTRAL_PAGES` aborts (exit 17) unless `--page`
  acknowledges it, and the file records the pair as READ, never as declared.
  The reference decoder rests at 0:1 and cannot be moved off it, which is why
  neutral is a set and not the single pair 0:0 this once assumed;
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

The one stream shape that surprises: `start` carries the planned total, which
is not known until CV29 has been read, so a failure BEFORE that point (the
index-page refusal, a silent CV29) produces a stream whose only line is the
summary. A consumer must therefore key on `type`, never on line position.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, TextIO

import typer

from railctl.backup import (
    BACKUP_SCHEMA,
    NOT_ATTEMPTED_DETAIL,
    SOURCE_CATALOG,
    STDOUT_TARGET,
    SWEEP_CAVEATS,
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
    BACKUP_ALL_OPT,
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
from railctl.cli.commands._sweep import (
    CORROBORATED_HIGH_CV,
    HIGHEST_EXERCISED_CV,
    SWEEP_CONFIRM_SECONDS,
    SWEEP_ESTIMATE_AFTER,
    SWEEP_PROGRESS_EVERY,
    SWEEP_SECONDS_PER_CV,
    SWEEP_SET_NAME,
    estimate_seconds,
    format_duration,
    sweep_bound,
    sweep_name,
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
    confirm,
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
from railctl.xbus.cv import MAX_CV_DIRECT, MAX_CV_EXT, MAX_CV_Z21

if TYPE_CHECKING:
    from collections.abc import Mapping

    from railctl.catalog import CatalogEntry
    from railctl.cli.deps import Settings
    from railctl.station import Capabilities, Station

_BACKUP_META = command_meta("backup")

#: The set a backup takes without `--all`. The sweep's own word is
#: `SWEEP_SET_NAME`, and it reaches both the document's `set` key and the
#: default filename, so a sweep can never overwrite a curated backup.
SET_NAME: Final[str] = "curated"

#: The two sweep names the buffered envelope and the ndjson stream share, so
#: a consumer routes on one string whichever format it asked for.
_SWEEP_UNEXERCISED_EVENT: Final[str] = "sweep.unexercised_range"
_SWEEP_ESTIMATE_EVENT: Final[str] = "sweep.estimate"

#: What a sweep's human summary says about its own exit code. Nothing is
#: special-cased to make a sweep exit 0: silence and "this CV is not
#: implemented" are indistinguishable on this hardware, the design has no
#: status for the difference, and inventing one in the exit code would make
#: every other command's 9 mean less.
_SWEEP_EXIT_NOTE: Final[str] = (
    "a sweep normally exits 9: most CV numbers are not implemented in any decoder, and "
    "this hardware cannot tell that from silence, so they are recorded as no_response - "
    "the file is the product either way"
)

#: What passing `HIGHEST_EXERCISED_CV` does and does not mean. The claim is
#: about this bench's EVIDENCE, not about the decoder, so it moves whenever a
#: measurement lands - twice inside one day so far, and both moves shortened
#: it.
#:
#: 2026-08-19, the first full sweep: it got an answer for every CV from 512 to
#: 1024 through the Z21 opcode, so "never answered" stopped being true and
#: this text stopped saying it.
#:
#: Later the same day, the CV523 check (`CORROBORATED_HIGH_CV`): a value
#: written through one opcode family was read back through another, so "no
#: value read there has been checked against a known quantity" stopped being
#: true as well - and unlike the first move, this text did not follow. What
#: replaced that clause is thin rather than absent, one CV out of five
#: hundred, and that is what the text now says.
#:
#: Both claims stay in the past tense and named to this bench, and the opcode
#: is named with them. A sweep that reaches this warning may be about to use
#: the EXTENDED opcodes rather than the Z21 one - `sweep_bound` returns
#: `MAX_CV_EXT` when `service_ext_cv` is proven and `z21_cv_opcodes` is not -
#: and their reply bands `63 16` / `63 17` have never been seen on this bench
#: at all (see the `HIGHEST_EXERCISED_CV` comment in `_sweep.py`). A bare
#: "CV512 and up answer" would extend one opcode's measurement to an opcode
#: nothing has measured, in the warning whose whole subject is how far the
#: evidence goes.
#:
#: The same rewrite dropped "a zero cannot be told from a CV the decoder does
#: not implement". It is true, but docs/probe-results.md is explicit that it
#: applies at every CV number and is not a property of the high range, so
#: saying it here attributed a universal caveat to this range alone. That is
#: the whole reason it left, and it does not depend on the caveat being said
#: anywhere else.
#:
#: Where it IS said, exactly. On the human path, `_SWEEP_EXIT_NOTE`, on the
#: summary of a sweep that wrote a file - `build_backup` appends it to
#: `CommandResult.lines`, which only the `human` renderer prints, and returns
#: before it for `--out -`. On the machine path, the document's own `caveats`
#: key (`backup/types.py`, `SWEEP_CAVEATS`), written into every swept file and
#: therefore into `--format=json`'s `result`, which is the document plus the
#: path. Issue #53 added that second channel because the envelope has no
#: `lines` and the question is asked of the FILE long after the run. The
#: ndjson summary still carries counts, `complete`, the path and the exit code
#: only, so a streaming consumer reads the caveat off the file that line names.
#:
#: None of that belongs in this string: putting it back here would state a
#: property of CV1 inside a warning about CV512, which is the misattribution
#: above.
#:
#: `tests/cli/test_backup.py` pins these claims against the document's
#: section, and says in its own docstring which kind of drift that catches.
_UNEXERCISED_REASON: Final[str] = (
    f"CV{HIGHEST_EXERCISED_CV + 1} and up answered on this bench on 2026-08-19, through the Z21 "
    f"opcode: the first full sweep got an answer for every CV from {HIGHEST_EXERCISED_CV + 1} to "
    f"{MAX_CV_Z21}, and the extended opcodes have never been seen to reply above "
    f"CV{HIGHEST_EXERCISED_CV} - and one value up there is corroborated: "
    f"CV{CORROBORATED_HIGH_CV}, agreed on by two encodings with different field layouts and "
    f"matching a third implementation's bytes; that is one CV out of five hundred, so the range "
    f"is thinly measured rather than unmeasured, and a value read up there is not backed by "
    f"anything the way CV{CORROBORATED_HIGH_CV} is; that does not mean those CVs do not work"
)

#: How many holes the incomplete report names before it stops counting them
#: out. Twelve fits a terminal line; the full lists live in the report's
#: `details` and in the file, so this trims the prose and never the data.
INCOMPLETE_LIST_MAX: Final[int] = 12

#: XpressNet's short/long address boundary: 1..99 ride in one byte.
SHORT_ADDRESS_MAX: Final[int] = 99

#: The pair a backup reports when it has nothing measured to report.
DEFAULT_PAGE: Final[CvPage] = (0, 0)

#: The CV31/CV32 pairs that mean "no special CV page is selected", so the
#: curated CVs above 256 are the NORMAL ones the catalog names.
#:
#: Both come from the ZIMO MS/MN manual: (0, 0) is "CV page 0 (main page)",
#: and (0, 1) is what the manual calls "Resetting the CV bank" (p. 69). The
#: reference MS450P22 rests at (0, 1) and REFUSES to leave it - measured
#: 2026-08-13, `cv write 32 0` was accepted by the station and read back as 1,
#: which the write verification caught. Treating only (0, 0) as neutral
#: therefore aborted every backup of a decoder in its normal state.
#:
#: That (0, 1) really is neutral is measured, not inferred from the manual's
#: wording, which contradicts itself elsewhere: on that bank CV287, CV395,
#: CV396 and CV397 read 55, 80, 15 and 14. The last two carry a documented
#: range of 0-29 and landed on F15/F14, a coherent volume-down/volume-up
#: pair - see docs/probe-results.md, "The ZIMO index bank rests at 0:1".
#: Anything else (a real page such as 145/2, which holds the audio filters)
#: still stops a backup, because there the same CV numbers mean something
#: the catalog does not name.
NEUTRAL_PAGES: Final[frozenset[CvPage]] = frozenset({(0, 0), (0, 1)})

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
_ALL_OPT = typer_option(BACKUP_ALL_OPT)
_NOTE_OPT = typer_option(BACKUP_NOTE_OPT)
_FORCE_OPT = typer_option(BACKUP_FORCE_OPT)
_MODE_OPT = typer_option(BACKUP_MODE_OPT)
_PAGE_OPT = typer_option(BACKUP_PAGE_OPT)


def utc_timestamp() -> str:
    """The `created_utc` the CLI stamps into the file. A module-level seam on
    purpose: the writer is a pure function of the document, so a test that
    pins this proves two consecutive backups byte-identical."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def monotonic_seconds() -> float:
    """The clock a sweep's progress and revised estimate are measured on.

    A module-level seam for the same reason `utc_timestamp` is one: the
    rendered duration is part of the contract a test pins, and a test cannot
    pin a real elapsed time. Monotonic rather than wall clock - a duration
    that goes backwards because NTP stepped the clock would report a
    negative rate.
    """
    return time.monotonic()


class _SweepReporter:
    """A sweep's stderr progress and its one revised estimate.

    Never stdout, in any format: the JSON document and the ndjson stream own
    that stream, and a progress line in either is a parse error at the other
    end. `progress_lines` is false for ndjson, where the stream already
    carries one line per CV and repeating it on stderr is noise; the revised
    estimate is reported in every format, because it is the only correction
    to a number the operator agreed to.
    """

    def __init__(
        self,
        total: int,
        *,
        stderr: TextIO,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
        progress_lines: bool = True,
    ) -> None:
        self._total = total
        self._stderr = stderr
        self._on_event = on_event
        self._progress_lines = progress_lines
        self._started = monotonic_seconds()
        self._done = 0

    def observed(self) -> None:
        """One more CV reported. Called for every row the collection records,
        the CV31/CV32/CV29 singletons included - they cost the same seconds
        as any other read, and an estimate that pretended otherwise would be
        wrong by exactly what it left out."""
        self._done += 1
        if self._done == SWEEP_ESTIMATE_AFTER and self._done < self._total:
            self._revise()
        if self._progress_lines and self._done % SWEEP_PROGRESS_EVERY == 0:
            if self._done < self._total:
                self._say(
                    f"sweep: {self._done} of {self._total} CVs, "
                    f"about {format_duration(self._remaining())} left"
                )

    def finished(self) -> None:
        """The closing line of a sweep that ran to the end. An interrupted run
        gets no line here: the abort's own message says where the partial
        file went, and a "done" line above it would contradict it."""
        if self._progress_lines:
            elapsed = monotonic_seconds() - self._started
            self._say(
                f"sweep: {self._done} of {self._total} CVs read in {format_duration(elapsed)}"
            )

    def _revise(self) -> None:
        """The up-front estimate replaced by the observed rate, once.

        It never re-prompts. The operator already agreed to the sweep, and a
        question arriving mid-run on a stream nobody is watching is a hang,
        not a safeguard.
        """
        rate = self._rate()
        remaining = estimate_seconds(self._total - self._done, rate)
        self._say(
            f"sweep: revised estimate after {self._done} CVs - {rate:.2f} s per CV, "
            f"{format_duration(estimate_seconds(self._total, rate))} for all "
            f"{self._total} CVs, about {format_duration(remaining)} left"
        )
        if self._on_event is not None:
            self._on_event(
                _SWEEP_ESTIMATE_EVENT,
                {
                    "observed": self._done,
                    "total": self._total,
                    "seconds_per_cv": round(rate, 2),
                    "remaining_seconds": round(remaining),
                },
            )

    def _rate(self) -> float:
        return (monotonic_seconds() - self._started) / self._done

    def _remaining(self) -> float:
        return estimate_seconds(self._total - self._done, self._rate())

    def _say(self, line: str) -> None:
        print(line, file=self._stderr)


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
                "backup will not send a whole set of reads down a channel measured not "
                "to answer",
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
    does. What gets past that is an encoding whose READ needs no selection,
    and there are two: the extended opcodes, and the Z21 16-bit opcode, which
    carries CV1..1024 in one field and is the FIRST thing
    `service_read_telegram` picks when it is proven. Either one, measured
    yes, lifts the bound.

    Checking only `service_ext_cv` was a defect until 2026-08-19: a station
    proving the Z21 opcode and nothing else recorded every curated CV above
    255 as skipped, with the detail "extended opcodes not probed" - true
    about the extended opcodes and false about the CV, which `cv read` would
    have read on that same station. It stayed invisible on this bench, where
    the doctor proves both. Found by the M11 review, because `sweep_bound`
    and this function then disagreed about the same CV.

    POM keeps the 255 bound: its native range is not the problem, the page
    selection is.
    """
    if mode is ProgMode.SERVICE and (
        capabilities.z21_cv_opcodes is True or capabilities.service_ext_cv is True
    ):
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

    __slots__ = ("address", "argv", "declared_page", "mode_word", "note", "path", "set_name")

    def __init__(
        self,
        *,
        mode_word: str,
        declared_page: CvPage | None,
        address: int,
        path: Path | None,
        note: str | None,
        set_name: str,
        argv: list[str],
    ) -> None:
        self.mode_word = mode_word
        self.declared_page = declared_page
        self.address = address
        self.path = path
        self.note = note
        #: `"curated"` or `SWEEP_SET_NAME` - one word, decided once, reaching
        #: both the filename and the document's `set` key.
        self.set_name = set_name
        #: The invocation as typed, for any suggestion this run has to make
        #: after the plan exists - the sweep's confirmation among them.
        self.argv = argv

    @property
    def sweep(self) -> bool:
        return self.set_name == SWEEP_SET_NAME


def _typed_argv(
    address: int | None,
    *,
    out: str | None,
    note: str | None,
    sweep: bool,
    force: bool,
    mode_word: str,
    page_token: str | None,
    typed_globals: list[str],
) -> list[str]:
    """The invocation as typed, rebuilt as an argv array a suggestion can
    extend: `--address` and backup's own options first (in the order
    `_meta` declares them), then the global flags the operator actually
    typed, in registration order. `None` for an address not yet resolved (a
    refusal ahead of `require_address`) simply omits the flag rather than
    inventing a value.

    `force` is carried like every other flag because this argv is what the
    sweep's confirmation republishes: an operator who typed `--force` did so
    because the target file already exists, and a retry that drops the flag
    is refused by the overwrite check instead of running."""
    argv = ["railctl", "backup"]
    if address is not None:
        argv += ["--address", str(address)]
    if out is not None:
        argv += ["--out", out]
    if note is not None:
        argv += ["--note", note]
    if force:
        argv.append(BACKUP_FORCE_OPT.name)
    if sweep:
        argv.append(BACKUP_ALL_OPT.name)
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
    sweep: bool,
    force: bool,
    typed_globals: list[str],
) -> _Plan:
    """Validate the invocation and resolve the target path, station untouched.

    The overwrite refusal lives here on purpose: `backup_path` only resolves
    the path, and refusing AFTER the station opened would cost the operator a
    77-read run - or a sweep's half hour - to learn the file already existed.
    The set name is decided here for the same reason: it is what names the
    file, so `--all` writes `loco-0003-all.json` and can never land on top of
    a curated backup.
    """
    prefix = ["railctl", "backup"]
    check_choice("mode", mode_word, BACKUP_MODE_OPT.enum or ())
    declared_page = parse_page(page_token, argv_hint=prefix)
    address = require_address(settings, argv_hint=prefix)
    set_name = SWEEP_SET_NAME if sweep else SET_NAME
    argv = _typed_argv(
        address,
        out=out,
        note=note,
        sweep=sweep,
        force=force,
        mode_word=mode_word,
        page_token=page_token,
        typed_globals=typed_globals,
    )
    path = backup_path(address, set_name, out)
    # `not force` guards this branch, so `argv` cannot already carry the flag
    # and appending it here produces one `--force`, never two.
    if path is not None and not force and path.exists():
        raise UsageProblem(
            f"{path} already exists; a backup never overwrites silently - pass --force "
            f"to replace it",
            suggestions=[[*argv, "--force"]],
            details={"reason": "backup_file_exists", "path": str(path)},
        )
    return _Plan(
        mode_word=mode_word,
        declared_page=declared_page,
        address=address,
        path=path,
        note=note,
        set_name=set_name,
        argv=argv,
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
        sweep_to: int | None = None,
        on_start: Callable[[int, ProgMode], None] | None = None,
        on_cv: Callable[[CvRecord, CvResult | None], None] | None = None,
    ) -> None:
        self._station = station
        self._catalog = catalog
        self.mode = mode
        #: The sweep's bound, or `None` for a curated run. Decided before this
        #: object exists, because it needs measured capabilities and an
        #: operator's agreement, and both of those happen before the reads.
        self._sweep_to = sweep_to
        self.sweep_range: tuple[int, int] | None = None if sweep_to is None else (1, sweep_to)
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
        if page not in NEUTRAL_PAGES and self._declared_page is None:
            neutral = ", ".join(f"{high}:{low}" for high, low in sorted(NEUTRAL_PAGES))
            raise IndexPageRequiredError(
                f"the decoder sits on CV page CV31={page[0]} CV32={page[1]}, not on a "
                f"neutral bank ({neutral}); the curated CVs above 256 would not mean "
                f"what the catalog names them there, and a backup never writes the "
                f"selectors, so rerun with --page {page[0]}:{page[1]} to acknowledge it",
                details={
                    "page": list(page),
                    "neutral_pages": [list(p) for p in sorted(NEUTRAL_PAGES)],
                },
            )
        if self._declared_page is not None and self._declared_page != page:
            self.page_mismatch = self._declared_page
        self.page = page
        config_29 = self._singleton(29)
        planned = self._planned_cvs(config_29.value)
        self.planned = tuple(planned)
        # For a sweep this is a fact about the RANGE, not about CV29 bit 4:
        # the speed-table CVs are inside every bound a sweep can take, so the
        # file carries them whether or not the decoder has the table selected.
        self.speed_table_included = any(
            entry.needs_speed_table
            for entry in (self._catalog.get(cv) for cv in planned)
            if entry is not None
        )
        if self._on_start is not None:
            self._on_start(len(planned), self.mode)
        for result in (selector_31, selector_32, config_29):
            self._record_result(result)
        if self._sweep_to is None:
            # A sweep asks only for CVs inside its own bound, so nothing is
            # ever out of range there and `_bound_detail` has nothing to
            # explain; the curated list is the one that can name a CV the
            # resolved mode cannot reach.
            bound = reachable_bound(self.mode, self._station.capabilities)
            for cv in planned:
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
        remaining = [cv for cv in planned if cv not in self.records]
        self._batch([cv for cv in IDENTITY_CVS if cv in remaining])
        self._batch([cv for cv in remaining if cv not in IDENTITY_CVS])

    def _planned_cvs(self, cv29: int) -> list[int]:
        """Every CV this run answers for, ascending.

        The whole swept range for `--all`, the curated set CV29 selects
        otherwise. CV31 and CV32 stay IN the swept range - they are read as
        singletons above, and the `remaining` filter is what keeps them out
        of a batch payload, which `cv_read_many` refuses. Holding the full
        range here is what makes `summary.requested`, the stream's `total`
        and the rows an interrupt fills in all mean the same set.
        """
        if self._sweep_to is not None:
            return list(range(1, self._sweep_to + 1))
        return curated_cvs(self._catalog, cv29)

    def name_source(self, cv: int) -> tuple[str, str]:
        """The `name` and `source` one CV's row carries.

        A sweep keeps the catalog's slug where there is one - so its file
        diffs against a curated backup by name - and calls the rest
        `cv0617`, marked `sweep` so `restore` can refuse to write back a
        value nothing documents. A curated run only ever visits catalog CVs,
        so a KeyError there is a bug, not a data question.
        """
        if self._sweep_to is not None:
            return sweep_name(cv, self._catalog)
        return self._catalog[cv].slug, SOURCE_CATALOG

    def name_for(self, cv: int) -> str:
        return self.name_source(cv)[0]

    def _singleton(self, cv: int) -> CvResult:
        """One `cv_read` whose failure ABORTS the run: without the page and
        CV29 there is no honest file to write. Service-mode silence carries
        the same placement guidance `cv read` attaches."""
        try:
            return self._station.cv_read(cv, address=self._read_address, mode=self.mode)
        except RailctlError as exc:
            raise _with_guidance(exc, mode=self.mode) from None

    def _record_result(self, result: CvResult) -> None:
        name, source = self.name_source(result.cv)
        spec = CvSpec(cv=result.cv, name=name)
        outcome = CvReadOutcome(spec=spec, result=result, error=None)
        self._record(record_for(outcome, source=source), result)

    def _record(self, record: CvRecord, result: CvResult | None) -> None:
        if result is not None and self.encoding is None:
            # `cv_encoding` is the encoding of the FIRST ok read (design C4).
            self.encoding = result.encoding.name
        self.records[record.cv] = record
        if self._on_cv is not None:
            self._on_cv(record, result)

    def _batch(self, cvs: list[int]) -> None:
        named = {cv: self.name_source(cv) for cv in cvs}
        specs = [CvSpec(cv=cv, name=named[cv][0]) for cv in cvs]

        def record(outcome: CvReadOutcome) -> None:
            self._record(record_for(outcome, source=named[outcome.spec.cv][1]), outcome.result)

        def progress(update: tuple[int, int, CvReadOutcome]) -> None:
            record(update[2])

        outcomes = self._station.cv_read_many(
            specs, address=self._read_address, mode=self.mode, on_progress=progress
        )
        # The real station reports every spec through `on_progress`, so this
        # loop records nothing new there; it is the fallback for a facade that
        # only returns the finished list.
        for outcome in outcomes:
            if outcome.spec.cv not in self.records:
                record(outcome)


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
    collection: _Collection, context: _Context, *, set_name: str, interrupted: bool = False
) -> BackupDocument:
    records = dict(collection.records)
    if interrupted:
        # The rows the interrupt cut off: `skipped` with the not-attempted
        # detail, so the partial file still answers for every planned CV and
        # `summary.requested` keeps meaning "the whole set that was planned" -
        # the curated list, or the swept range.
        for cv in collection.planned or ():
            if cv not in records:
                name, source = collection.name_source(cv)
                records[cv] = CvRecord(
                    cv=cv,
                    name=name,
                    status=ReadStatus.SKIPPED,
                    source=source,
                    detail=NOT_ATTEMPTED_DETAIL,
                )
    kind = "short" if context.address <= SHORT_ADDRESS_MAX else "long"
    return BackupDocument(
        created_utc=context.created_utc,
        tool=context.tool,
        note=context.note,
        loco={"address": context.address, "kind": kind},
        catalog={"family": CATALOG_FAMILY, "schema": CATALOG_SCHEMA},
        set_name=set_name,
        mode=collection.mode.value,
        cv_encoding=collection.encoding,
        page=collection.page or DEFAULT_PAGE,
        speed_table_included=collection.speed_table_included,
        sweep_range=collection.sweep_range,
        link=context.link,
        capabilities=context.capabilities,
        decoder=_decoder_block(records),
        cvs=tuple(records.values()),
        interrupted=interrupted,
        # Off the SET, not off the rows. A sweep's zeroes are the reason the
        # caveat exists, but a sweep that happened to read no zero at all is
        # still a document whose zeroes would have been unprovable - and a
        # key that came and went with the values would leave a consumer
        # unable to tell a sweep without zeroes from a file written before
        # the key existed.
        caveats=SWEEP_CAVEATS if set_name == SWEEP_SET_NAME else (),
    )


class _BackupRun:
    """One run's mutable state, shared by the buffered and the ndjson paths so
    a `finally` can still say what was collected and where it was written."""

    def __init__(
        self,
        plan: _Plan,
        catalog: Mapping[int, CatalogEntry],
        *,
        settings: Settings,
        on_start: Callable[[int, ProgMode], None] | None = None,
        on_cv: Callable[[CvRecord, CvResult | None], None] | None = None,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.plan = plan
        self._catalog = catalog
        self._settings = settings
        self._on_start = on_start
        self._on_cv = on_cv
        #: How this run publishes a named event AS IT HAPPENS - the ndjson
        #: stream's `event` line. The buffered formats have no such channel,
        #: so what they must carry is stashed for the envelope instead.
        self._on_event = on_event
        self._reporter: _SweepReporter | None = None
        self.collection: _Collection | None = None
        self.document: BackupDocument | None = None
        self.text: str | None = None
        self.written: Path | None = None
        #: The `sweep.unexercised_range` warning's message and details, for a
        #: buffered envelope to publish after the run.
        self.unexercised: tuple[str, dict[str, object]] | None = None

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
        sweep_to = self._plan_sweep(station, output, resolved) if plan.sweep else None
        context = _Context(station, note=plan.note, address=plan.address)
        collection = _Collection(
            station,
            self._catalog,
            mode=resolved,
            address=plan.address,
            declared_page=plan.declared_page,
            sweep_to=sweep_to,
            on_start=self._on_start,
            on_cv=self._cv_callback(),
        )
        self.collection = collection
        try:
            collection.collect()
        except KeyboardInterrupt:
            self._abort(context)
        if self._reporter is not None:
            self._reporter.finished()
        self.document = _document(collection, context, set_name=plan.set_name)
        self.text = write_backup(self.document)
        if plan.path is not None:
            write_backup_to(self.document, plan.path)
            self.written = plan.path

    def _plan_sweep(self, station: Station, output: OutputContext, mode: ProgMode) -> int:
        """The bound, the warning and the question - in that order, and all
        three before the first read.

        None of it can happen earlier than this. The bound comes from
        MEASURED capabilities, and those are only known once the port is
        open, so a sweep planned at the desk would be a guess about how many
        CVs to read. The warning goes out ahead of the question on purpose:
        an operator agreeing to half an hour of reads should already know
        which part of the range rests on a single corroborated value.
        """
        bound = sweep_bound(mode, station.capabilities)
        if bound > HIGHEST_EXERCISED_CV:
            self._warn_unexercised(bound, output)
        self._confirm_sweep(bound, output)
        self._reporter = _SweepReporter(
            bound,
            stderr=output.stderr,
            on_event=self._on_event,
            # The ndjson stream already carries one line per CV on stdout.
            progress_lines=output.fmt != "ndjson",
        )
        return bound

    def _warn_unexercised(self, bound: int, output: OutputContext) -> None:
        """Said ONCE, on every channel this run has, before the reads start."""
        details: dict[str, object] = {
            "from": HIGHEST_EXERCISED_CV + 1,
            "to": bound,
            "reason": _UNEXERCISED_REASON,
        }
        message = (
            f"the sweep reaches CV{bound}, past CV{HIGHEST_EXERCISED_CV}: {_UNEXERCISED_REASON}"
        )
        self.unexercised = (message, details)
        print(f"sweep: {message}", file=output.stderr)
        if self._on_event is not None:
            self._on_event(_SWEEP_UNEXERCISED_EVENT, dict(details))

    def _confirm_sweep(self, bound: int, output: OutputContext) -> None:
        """Ask before a long sweep (design L6). `confirm` returns at once with
        `--yes`, and raises rather than blocking when stdin is not a
        terminal, so this never hangs a scripted run."""
        seconds = estimate_seconds(bound)
        if seconds <= SWEEP_CONFIRM_SECONDS:
            return
        confirm(
            f"sweep {bound} CVs (CV1..CV{bound}) off the decoder - about "
            f"{format_duration(seconds)} at the measured {SWEEP_SECONDS_PER_CV} s per CV "
            f"(docs/probe-results.md, 2026-08-19). Nothing is written to the decoder, and "
            f"the run normally ends at exit 9 because most CV numbers answer nothing. "
            f"Proceed",
            settings=self._settings,
            stdin=sys.stdin,
            stderr=output.stderr,
            retry_argv=self.plan.argv,
        )

    def _cv_callback(self) -> Callable[[CvRecord, CvResult | None], None] | None:
        """The collection's one per-row callback, with the sweep's counter
        folded in behind the format's own: the ndjson `cv` line is written
        first, so a revised estimate that follows the tenth CV appears after
        it rather than in the middle of it."""
        reporter = self._reporter
        inner = self._on_cv
        if reporter is None:
            return inner

        def report(record: CvRecord, result: CvResult | None) -> None:
            if inner is not None:
                inner(record, result)
            reporter.observed()

        return report

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
        partial = _document(collection, context, set_name=self.plan.set_name, interrupted=True)
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
    if document.set_name == SWEEP_SET_NAME:
        result.say(_SWEEP_EXIT_NOTE)
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
    # Capped, because `--all` changed the size of this list: a curated run has
    # a handful of holes and naming them all is the report, while a 1024 CV
    # sweep has hundreds - most CV numbers are not implemented in any decoder -
    # and inlining those is a multi-kilobyte line on stderr and inside the JSON
    # envelope. Nothing is lost by capping: `details` below still carries every
    # number, machine-readable, and so does the file.
    listed = ", ".join(f"CV{r.cv} ({r.status.value})" for r in non_ok[:INCOMPLETE_LIST_MAX])
    if len(non_ok) > INCOMPLETE_LIST_MAX:
        listed += f", and {len(non_ok) - INCOMPLETE_LIST_MAX} more"
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
    sweep: bool,
    force: bool,
    typed_globals: list[str],
) -> CommandResult:
    plan = plan_backup(
        settings,
        mode_word=mode,
        page_token=page,
        out=out,
        note=note,
        sweep=sweep,
        force=force,
        typed_globals=typed_globals,
    )
    catalog = load_catalog()
    events = StationEventLog()
    station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
    try:
        backup_run = _BackupRun(plan, catalog, settings=settings)
        backup_run.execute(station, output)
        outcome = build_backup(backup_run.document, path=plan.path, text=backup_run.text)
        collection = backup_run.collection
        if collection is not None and collection.page_mismatch is not None:
            outcome.warn(
                _PAGE_MISMATCH_EVENT, _PAGE_MISMATCH_MESSAGE, **_mismatch_details(collection)
            )
        if backup_run.unexercised is not None:
            message, details = backup_run.unexercised
            outcome.warn(_SWEEP_UNEXERCISED_EVENT, message, **details)
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
    sweep: bool,
    force: bool,
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
        sweep=sweep,
        force=force,
        mode_word=mode_word,
        page_token=page_token,
        typed_globals=typed_globals,
    )
    keep_stdout = _typed_argv(
        address,
        out=STDOUT_TARGET,
        note=note,
        sweep=sweep,
        force=force,
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
    sweep: bool,
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
                sweep=sweep,
                force=force,
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
            sweep=sweep,
            force=force,
            typed_globals=typed_globals,
        )
        catalog = load_catalog()
        backup_run = _BackupRun(
            plan,
            catalog,
            settings=settings,
            on_start=on_start,
            on_cv=on_cv,
            # The sweep's own events ride the same `event` line the station's
            # do - one shape for a consumer to route on.
            on_event=on_event,
        )
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
        sweep: bool = _ALL_OPT,
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
                sweep=sweep,
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
                sweep=sweep,
                force=force,
                typed_globals=typed_globals,
            ),
        )
