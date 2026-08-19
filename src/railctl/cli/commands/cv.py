# src/railctl/cli/commands/cv.py
"""The `cv read` and `cv write` commands - the first two-word command paths.

Everything wire-shaped is the station facade's business: this module parses
argv, chooses a `ProgMode`, and renders `CvResult`/`CvReadOutcome` objects.
No CV arithmetic, no opcodes (tests/test_layering.py rules 1 and 2).

The three-valued rule at this layer (ADDENDUM A4): a read that got silence is
`status: "no_response"` with NO `value` key - never value 0, never dropped
from the list. A read the mode could not reach is `skipped` with a reason.
Silence is not a value.

`cv write` is the first command whose job is to CHANGE the decoder, and every
safety property is explicit here rather than emergent:

* the catalog's min/max are ENFORCING on write - the refusal goes out before
  any telegram, exit 15 - and only ADVISORY on read, where the decoder's own
  value is the measurement and the catalog is the opinion;

* writes to `CONFIRM_CVS` ({1, 8, 17, 18, 29, 31, 32, 144}: the address CVs,
  the factory-reset trigger, the index selectors, CV144) are confirmed, with
  CV8's own wording, and `--yes` bypasses;
* a `--track main` (POM) write runs the same `preflight` the throttle
  commands use - a speed already sitting in the refresh buffer is exactly
  what a POM telegram must not join - while a programming-track write does
  not pre-flight track power, because service mode needs none on this
  station (measured 2026-08-06);
* verify-after-write defaults ON; `--no-verify` turns it off; and an
  explicit `--verify` with `--track main` is a usage error, never a silent
  downgrade, because nothing can confirm a POM write on this hardware.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Final

import typer

from railctl.catalog import CatalogEntry, load_catalog
from railctl.cli._errors import run
from railctl.cli._meta import (
    CV_PAGE_OPT,
    CV_READ_MODE_OPT,
    CV_SPEC_ARG,
    CV_WRITE_CV_ARG,
    CV_WRITE_TRACK_OPT,
    CV_WRITE_VALUE_ARG,
    CV_WRITE_VERIFY_OPT,
    command_meta,
    global_option,
    group_epilog,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli._parse_context import ParseContextTyper
from railctl.cli.commands.throttle import preflight
from railctl.cli.config import capabilities_path
from railctl.cli.cvspec import parse_cv_spec
from railctl.cli.deps import (
    StationEventLog,
    UsageProblem,
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
from railctl.cli.result import PARTIAL_EXIT_CODE, CommandResult, error_code, tri_state
from railctl.errors import (
    REASON_VALUE_OUT_OF_RANGE,
    CvOutOfRangeError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    ServiceEncodingUnknownError,
)
from railctl.station import (
    ADDRESS_CVS,
    CV144,
    INDEXED_CV_RANGE,
    PAGE_SELECTOR_CVS,
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    ProgMode,
    resolve_mode,
)

_CV_READ_META = command_meta("cv read")
_CV_WRITE_META = command_meta("cv write")

# Read off the metadata rows, never retyped - the manifest says what a command
# emits and the envelope says what it emitted, and two literals drift.
CV_READ_SCHEMA: Final[str] = _CV_READ_META.schema
CV_WRITE_SCHEMA: Final[str] = _CV_WRITE_META.schema

CV_GROUP_HELP: Final[str] = "Read and write decoder CVs"

#: The track words, quoted from the metadata row's enum so the manifest and
#: this module cannot disagree; a test pins the two maps below against it.
TRACK_PROG: Final[str] = "prog"
TRACK_MAIN: Final[str] = "main"
MODE_FOR_TRACK: Final[dict[str, ProgMode]] = {
    TRACK_PROG: ProgMode.SERVICE,
    TRACK_MAIN: ProgMode.POM,
}
#: The envelope carries BOTH keys (design: the `--track` rename note), so a
#: consumer keyed on either vocabulary reads the same fact.
TRACK_FOR_MODE: Final[dict[ProgMode, str]] = {
    ProgMode.SERVICE: TRACK_PROG,
    ProgMode.POM: TRACK_MAIN,
}

#: A CV holds one byte; the catalog narrows further per CV.
VALUE_MIN: Final[int] = 0
VALUE_MAX: Final[int] = 255

#: Writing this value to CV8 factory-resets a ZIMO decoder (vendor manual
#: p.30, docs/vendor-references.md).
FACTORY_RESET_CV: Final[int] = 8
FACTORY_RESET_VALUE: Final[int] = 8

#: One service-mode CV read costs about this on the YD7010 - measured 2026-08-04
#: (docs/probe-results.md). A silent POM attempt costs 6.7 s, so this is a floor.
SERVICE_READ_SECONDS: Final[float] = 1.7
#: Batch size above which `cv read` asks first: the design's 60 s confirmation
#: threshold divided by the measured per-read cost, rounded down.
SWEEP_CONFIRM_CVS: Final[int] = int(60 / SERVICE_READ_SECONDS)  # = 35

#: The confirmation set, derived from the station layer's own constants
#: rather than retyped: the address CVs {1, 17, 18, 29}, the factory-reset
#: trigger CV8, the ZIMO index selectors {31, 32}, and CV144. A test pins the
#: union against the design's literal {1, 8, 17, 18, 29, 31, 32, 144}.
CONFIRM_CVS: Final[frozenset[int]] = frozenset(
    {*ADDRESS_CVS, FACTORY_RESET_CV, *PAGE_SELECTOR_CVS, CV144}
)

#: Why each confirmed CV deserves a prompt, in words an operator can act on.
#: CV8 is handled separately - see `_confirm_question`.
_CONFIRM_REASONS: Final[dict[int, str]] = {
    1: "the primary address - the locomotive answers on a different address afterwards",
    17: "the high byte of the extended address pair",
    18: "the low byte of the extended address pair",
    29: "the configuration register - address mode, direction and RailCom hang on its bits",
    31: "a ZIMO index-page selector - later indexed CV reads and writes land where it points",
    32: "a ZIMO index-page selector - later indexed CV reads and writes land where it points",
    144: "the ZIMO MX programming lock (a confirmation-jingle setting on MS decoders)",
}

#: Printed once, on stderr, by every programming-track command (design L2).
PROG_TRACK_NOTICE: Final[str] = (
    "programming track: this acts on whatever locomotive is standing on it; "
    "--address is not used to select it"
)

#: The order is the point (design: silence on the programming track "must
#: never be reported as 'the decoder is not answering'"): the likelier cause
#: first, the decoder last, ending with the placement test.
SILENCE_GUIDANCE: Final[str] = (
    "an empty or badly contacted programming track is far more likely than a decoder "
    "that is not answering - check the locomotive is standing on the programming track "
    "and its wheels make contact, and only then suspect the decoder; `railctl cv read 8` "
    "is the placement test, because a ZIMO decoder answers 145"
)

STATUS_OK: Final[str] = "ok"
STATUS_NO_RESPONSE: Final[str] = "no_response"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"

#: Errors that mean "this mode cannot reach this CV" - a skip with a reason,
#: not a decoder that failed to answer (ADDENDUM A4).
_SKIP_ERRORS: Final[tuple[type[RailctlError], ...]] = (
    CvOutOfRangeError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    ServiceEncodingUnknownError,
)

# Built once, at import time - the same B008 note as every command module.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_CVSPEC_ARG = typer_argument(CV_SPEC_ARG)
_MODE_OPT = typer_option(CV_READ_MODE_OPT)
_PAGE_OPT = typer_option(CV_PAGE_OPT)
_CV_ARG = typer_argument(CV_WRITE_CV_ARG)
_VALUE_ARG = typer_argument(CV_WRITE_VALUE_ARG)
_VERIFY_OPT = typer_option(CV_WRITE_VERIFY_OPT)
_TRACK_OPT = typer_option(CV_WRITE_TRACK_OPT)


def parse_page(token: str | None, *, argv_hint: Sequence[str]) -> CvPage | None:
    """`"145:0"` -> `(145, 0)`, the CV31/CV32 pair; `None` stays `None`."""
    if token is None:
        return None
    parts = token.split(":")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise UsageProblem(
            f"--page takes the CV31 and CV32 values as two numbers separated by ':', got {token!r}",
            suggestions=[[*argv_hint, "--page", "145:0"]],
            details={"reason": "malformed_page", "page": token},
        )
    cv31, cv32 = (int(part) for part in parts)
    for name, value in (("CV31", cv31), ("CV32", cv32)):
        if not VALUE_MIN <= value <= VALUE_MAX:
            raise UsageProblem(
                f"--page {name} value {value} is outside {VALUE_MIN}..{VALUE_MAX}",
                suggestions=[[*argv_hint, "--page", "145:0"]],
                details={"reason": "page_value_not_a_byte", "field": name, "value": value},
            )
    return (cv31, cv32)


def _catalog_name(catalog: Mapping[int, CatalogEntry], cv: int) -> str:
    """The catalog slug, or `""` for a CV the catalog does not curate - an
    absent name is a smaller claim than an invented one."""
    entry = catalog.get(cv)
    return entry.slug if entry is not None else ""


def _label(cv: int, name: str) -> str:
    return f"CV{cv} {name}" if name else f"CV{cv}"


def _confirm_question(cv: int, value: int) -> str:
    """CV8 gets its own wording (ADDENDUM A3): writing 8 to it factory-resets
    the decoder, and that sentence must be in front of the operator BEFORE
    they answer, not in a manual they have not opened."""
    if cv == FACTORY_RESET_CV:
        if value == FACTORY_RESET_VALUE:
            return (
                f"writing {FACTORY_RESET_VALUE} to CV8 FACTORY-RESETS the decoder - every "
                f"setting is wiped (vendor manual p.30, docs/vendor-references.md). Reset it"
            )
        return (
            f"CV8 is the manufacturer ID and writing it acts on the whole decoder - "
            f"writing 8 factory-resets it (vendor manual p.30). Write {value} to CV8"
        )
    return f"CV{cv} is {_CONFIRM_REASONS[cv]}. Write {value} to CV{cv}"


def _all_failed(outcomes: Sequence[CvReadOutcome]) -> bool:
    for outcome in outcomes:
        if outcome.result is not None:
            return False
    return True


def _with_guidance(error: RailctlError, *, mode: ProgMode) -> RailctlError:
    """Attach the programming-track placement guidance to total silence.

    Only for service mode: the guidance names the programming track, and POM
    silence on this station is a different, already-documented story
    (docs/probe-results.md R1) whose message the station layer owns.
    """
    if isinstance(error, DecoderNotRespondingError) and mode is ProgMode.SERVICE:
        return DecoderNotRespondingError(
            str(error),
            hint=SILENCE_GUIDANCE,
            cv=error.cv,
            details=dict(error.details),
        )
    return error


def build_cv_read(
    outcomes: Sequence[CvReadOutcome],
    *,
    mode: ProgMode,
    address: int | None,
    catalog: Mapping[int, CatalogEntry],
) -> CommandResult:
    """One row per requested CV, in request order, statuses per ADDENDUM A4.

    `ok`/`failed` count rows, and `failed` is everything that produced no
    value - a skip included, because "not read" is not "read". A batch with
    both kinds is a partial result (exit 8, `ok: false`): what completed is
    reported, what did not is named, and neither hides the other.
    """
    result = CommandResult(schema=CV_READ_SCHEMA, command="cv read")
    rows: list[dict[str, object]] = []
    ok_count = 0
    silent: list[int] = []
    for outcome in outcomes:
        cv = outcome.spec.cv
        entry = catalog.get(cv)
        name = entry.slug if entry is not None else ""
        label = _label(cv, name)
        row: dict[str, object] = {"cv": cv, "name": name}
        if outcome.result is not None:
            value = outcome.result.value
            row["status"] = STATUS_OK
            row["value"] = value
            ok_count += 1
            result.say(f"{label} = {value}")
            if entry is not None and not entry.min <= value <= entry.max:
                # ADVISORY on read (ADDENDUM A3): the decoder's value is the
                # measurement and the catalog is the opinion, so the row stays
                # `ok` and the disagreement is a note, never a failure.
                row["note"] = f"outside the catalog's {entry.min}..{entry.max}"
                result.warn(
                    "cv.value_outside_catalog_range",
                    f"CV{cv} reads {value}, outside the catalog's {entry.min}..{entry.max} "
                    f"for {name}; the value stands - the catalog is advisory on read",
                    cv=cv,
                    value=value,
                    min=entry.min,
                    max=entry.max,
                )
        elif outcome.error is None:
            # `CvReadOutcome`'s contract: both None is "not attempted".
            row["status"] = STATUS_SKIPPED
            row["reason"] = "not attempted"
            result.say(f"{label}: skipped (not attempted)")
        elif isinstance(outcome.error, DecoderNotRespondingError):
            # Silence. NO value key - never zero, never omitted from the list.
            row["status"] = STATUS_NO_RESPONSE
            row["error"] = error_code(outcome.error)
            silent.append(cv)
            result.say(f"{label}: no response")
        elif isinstance(outcome.error, _SKIP_ERRORS):
            row["status"] = STATUS_SKIPPED
            row["error"] = error_code(outcome.error)
            row["reason"] = str(outcome.error)
            result.say(f"{label}: skipped ({error_code(outcome.error)})")
        else:
            row["status"] = STATUS_FAILED
            row["error"] = error_code(outcome.error)
            row["reason"] = str(outcome.error)
            result.say(f"{label}: failed ({error_code(outcome.error)})")
        rows.append(row)
    failed = len(rows) - ok_count
    result.result = {
        "mode": mode.value,
        "track": TRACK_FOR_MODE[mode],
        "address": address,
        "requested": len(rows),
        "ok": ok_count,
        "failed": failed,
        "cvs": rows,
    }
    if silent and mode is ProgMode.SERVICE:
        result.warn("cv.no_response", SILENCE_GUIDANCE, cvs=list(silent))
    if failed:
        result.ok = False
        result.exit_code = PARTIAL_EXIT_CODE
    return result


def build_cv_write(
    written: CvResult, *, track: str, address: int | None, name: str, verify: bool
) -> CommandResult:
    """The one write, with `verified` carried honestly: `true` only when an
    independent read-back (or a decoder-level Ready) confirmed it, `null` when
    nothing measured the decoder - never `false`, which would claim a mismatch
    nobody measured (a real mismatch raises `CvVerifyError`, exit 14) - and
    the reason it is not `true` said out loud."""
    result = CommandResult(schema=CV_WRITE_SCHEMA, command="cv write")
    label = _label(written.cv, name)
    result.result = {
        "cv": written.cv,
        "name": name,
        "value": written.value,
        "mode": written.mode.value,
        "track": track,
        "address": address,
        "verified": written.verified,
    }
    result.say(
        f"{label} = {written.value} written ({written.mode.value} mode, "
        f"verified: {tri_state(written.verified)})"
    )
    if written.verified:
        return result
    if track == TRACK_MAIN:
        result.warn(
            "cv.write_unverified",
            f"the write went out on the main track and cannot be checked there; put the "
            f"locomotive on the programming track and run `railctl cv read {written.cv}` "
            f"to verify it",
            cv=written.cv,
        )
        return result
    if not verify:
        result.say("not read back (--no-verify)")
    result.warn(
        "cv.write_unverified",
        f"the station's own result echo matched {written.value}, but no independent "
        f"read-back confirmed it; the echo shows what the station produced, not what "
        f"the decoder retained",
        cv=written.cv,
    )
    return result


def register(app: typer.Typer) -> None:
    """Attach the `cv` group - `cv read`, then `cv write`, in tree order.

    Both leaves redeclare the eight global options, like every registered
    command (Click parses a group's options only before the subcommand name).
    The group itself carries the fixed help headings via `group_epilog` and
    is added between `monitor` and `schema`, which is where `_meta.COMMANDS`
    puts its leaves.
    """
    cv_app = ParseContextTyper(add_completion=False, context_settings={"max_content_width": 100})

    @cv_app.command("read", help=_CV_READ_META.help, epilog=help_epilog(_CV_READ_META))
    def cv_read_command(
        ctx: typer.Context,
        cvspec: list[str] = _CVSPEC_ARG,
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

        def work() -> CommandResult:
            prefix = ["railctl", "cv", "read"]
            argv_hint = [*prefix, *cvspec]
            catalog = load_catalog()
            cvs = parse_cv_spec(cvspec, catalog, argv_prefix=prefix)
            check_choice("mode", mode, CV_READ_MODE_OPT.enum or ())
            declared_page = parse_page(page, argv_hint=argv_hint)
            if declared_page is not None and any(cv in INDEXED_CV_RANGE for cv in cvs):
                # The one path through a read that WRITES the decoder: the
                # page selection writes CV31/CV32, which are in CONFIRM_CVS
                # precisely because later indexed access lands where they
                # point. Same gate as `cv write`, and BEFORE the station
                # opens - a refusal must cost no port.
                cv31, cv32 = declared_page
                confirm(
                    f"--page selects the ZIMO index page by WRITING CV31={cv31} and "
                    f"CV32={cv32} - later indexed CV reads and writes land where they "
                    f"point. The pair is read first and re-selected as found afterwards. "
                    f"Write CV31/CV32",
                    settings=settings,
                    stdin=sys.stdin,
                    stderr=output.stderr,
                    retry_argv=[*argv_hint, "--page", str(page)],
                )
            if len(cvs) > SWEEP_CONFIRM_CVS:
                # Design L6: "any sweep estimated over 60 s" is confirmed. The grammar makes
                # `cv read 1-1024` typable today, and on this station one service read costs
                # about SERVICE_READ_SECONDS (measured 2026-08-04, docs/probe-results.md) -
                # so 1024 CVs is half an hour of bus traffic nobody was asked about. The
                # figure is a floor: a silent POM attempt costs 6.7 s, so a POM-resolved
                # sweep runs LONGER than this estimate, never shorter.
                estimate = int(len(cvs) * SERVICE_READ_SECONDS)
                confirm(
                    f"reading {len(cvs)} CVs takes roughly {estimate} s on this station "
                    f"(about {SERVICE_READ_SECONDS} s per service-mode read, measured; "
                    f"POM is slower). Proceed",
                    settings=settings,
                    stdin=sys.stdin,
                    stderr=output.stderr,
                    retry_argv=argv_hint,
                )
            events = StationEventLog()
            station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
            try:
                resolved = resolve_mode(ProgMode(mode), station.capabilities, operation="read")
                if resolved is ProgMode.POM:
                    # Usage first (cheap, runnable answer), then the status
                    # pre-flight every POM cv command runs (design L6).
                    resolved_address: int | None = require_address(settings, argv_hint=argv_hint)
                    preflight(station, speed=None, action="a main-track (POM) CV read")
                else:
                    resolved_address = None
                    print(PROG_TRACK_NOTICE, file=output.stderr)
                specs = [
                    CvSpec(
                        cv=cv,
                        name=_catalog_name(catalog, cv),
                        page=declared_page if cv in INDEXED_CV_RANGE else None,
                    )
                    for cv in cvs
                ]
                # CV31/CV32 are read as singleton `cv_read` calls: the station
                # batch refuses them wholesale (page selection interleaving -
                # that guard stands), but the published grammar accepts
                # 1..1024, so the split lives here. The batch may come back in
                # the station's efficient wire order; the envelope's contract
                # is "first-appearance order is kept", so rows are reassembled
                # in request order.
                by_cv: dict[int, CvReadOutcome] = {}
                for spec in specs:
                    if spec.cv not in PAGE_SELECTOR_CVS:
                        continue
                    try:
                        singleton = station.cv_read(
                            spec.cv, address=resolved_address, mode=resolved
                        )
                        by_cv[spec.cv] = CvReadOutcome(spec=spec, result=singleton, error=None)
                    except RailctlError as exc:
                        by_cv[spec.cv] = CvReadOutcome(spec=spec, result=None, error=exc)
                batched = [spec for spec in specs if spec.cv not in PAGE_SELECTOR_CVS]
                if batched:
                    for batch_outcome in station.cv_read_many(
                        batched, address=resolved_address, mode=resolved
                    ):
                        by_cv[batch_outcome.spec.cv] = batch_outcome
                outcomes = [by_cv[cv] for cv in cvs]
                if outcomes[0].error is not None and _all_failed(outcomes):
                    # Nothing answered: the caller gets the real error with
                    # its own code and exit, not a partial result with no
                    # partial in it.
                    raise _with_guidance(outcomes[0].error, mode=resolved)
                outcome = build_cv_read(
                    outcomes, mode=resolved, address=resolved_address, catalog=catalog
                )
                events.attach_to(outcome)
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            except BaseException:
                close_quietly(station)
                raise
            return close_after(station, outcome)

        run("cv read", output, work)

    @cv_app.command("write", help=_CV_WRITE_META.help, epilog=help_epilog(_CV_WRITE_META))
    def cv_write_command(
        ctx: typer.Context,
        cv: str = _CV_ARG,
        value: int = _VALUE_ARG,
        verify: bool | None = _VERIFY_OPT,
        track: str = _TRACK_OPT,
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

        def work() -> CommandResult:
            prefix = ["railctl", "cv", "write"]
            catalog = load_catalog()
            check_choice("track", track, CV_WRITE_TRACK_OPT.enum or ())
            cvs = parse_cv_spec([cv], catalog, argv_prefix=prefix)
            if len(cvs) != 1:
                raise UsageProblem(
                    f"cv write takes exactly one CV, got {len(cvs)} from {cv!r}",
                    suggestions=[[*prefix, str(number), str(value)] for number in cvs],
                    details={"reason": "multiple_cvs", "cvs": cvs},
                )
            number = cvs[0]
            if not VALUE_MIN <= value <= VALUE_MAX:
                raise UsageProblem(
                    f"value {value} is outside {VALUE_MIN}..{VALUE_MAX}; a CV holds one byte",
                    suggestions=[],
                    details={"reason": "value_not_a_byte", "value": value},
                )
            if track == TRACK_MAIN and verify is True:
                # Never a silent downgrade (design): a typed flag that cannot
                # be honoured is refused, and the message says plainly what
                # cannot happen and when it can.
                raise UsageProblem(
                    "--verify together with --track main: nothing can confirm a write on "
                    "the main track, and the live write cannot be checked until the "
                    "locomotive is back on the programming track",
                    suggestions=[
                        [*prefix, cv, str(value), "--track", "main", "--no-verify"],
                        [*prefix, cv, str(value)],
                    ],
                    details={"reason": "verify_on_main"},
                )
            entry = catalog.get(number)
            name = entry.slug if entry is not None else ""
            if entry is not None and not entry.min <= value <= entry.max:
                # ENFORCING on write (ADDENDUM A3): refused before any
                # telegram, the bound named, exit 15.
                raise CvOutOfRangeError(
                    f"CV{number} ({entry.slug}) takes {entry.min}..{entry.max}, got "
                    f"{value}; refused before any telegram went out - the catalog is "
                    f"enforcing on write",
                    cv=number,
                    details={
                        "reason": REASON_VALUE_OUT_OF_RANGE,
                        "value": value,
                        "min": entry.min,
                        "max": entry.max,
                    },
                )
            if number in CONFIRM_CVS:
                # The retry argv repeats what was actually typed (the raw CV
                # token, so a slug stays a slug) - `confirm` appends `--yes`.
                retry = [*prefix, cv, str(value)]
                if track == TRACK_MAIN:
                    retry += ["--track", TRACK_MAIN]
                confirm(
                    _confirm_question(number, value),
                    settings=settings,
                    stdin=sys.stdin,
                    stderr=output.stderr,
                    retry_argv=retry,
                )
            wanted = MODE_FOR_TRACK[track]
            effective_verify = (track == TRACK_PROG) if verify is None else verify
            events = StationEventLog()
            station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
            try:
                resolved_address: int | None = None
                if wanted is ProgMode.POM:
                    resolved_address = require_address(
                        settings,
                        argv_hint=[*prefix, cv, str(value), "--track", "main"],
                    )
                    preflight(station, speed=None, action="a main-track (POM) CV write")
                else:
                    print(PROG_TRACK_NOTICE, file=output.stderr)
                written = station.cv_write(
                    number,
                    value,
                    address=resolved_address,
                    mode=wanted,
                    verify=effective_verify,
                )
                outcome = build_cv_write(
                    written,
                    track=track,
                    address=resolved_address,
                    name=name,
                    verify=effective_verify,
                )
                events.attach_to(outcome)
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            except BaseException:
                close_quietly(station)
                raise
            return close_after(station, outcome)

        run("cv write", output, work)

    app.add_typer(cv_app, name="cv", help=CV_GROUP_HELP, epilog=group_epilog("cv"))
