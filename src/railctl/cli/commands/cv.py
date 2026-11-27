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
  any telegram, `cv_out_of_range` - and only ADVISORY on read, where the decoder's own
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
  downgrade, because nothing can confirm a POM write on this hardware;
* an indexed CV (257-512) takes `--page CV31:CV32`, confirmed like any write
  to the selectors because that is what selecting it does, and is refused
  with `index_page_required` before the station opens when none is given.
  What the selection established is the station's to report
  (`CvResult.page`, a `PageSelection`), never inferred here. On the
  programming track the station reads the pair first and re-selects it as
  found after the write (`CvResult.page_restore`), as `cv read --page` does.
  Over POM the selection cannot be read back, the station's
  `page.unverified` event reaches the envelope as a warning beside a
  `verified: null` result, and the decoder is left on the selected page -
  nothing on that path is reported as checked;
* a `--page` that no requested CV can use (none in 257-512) is refused as a
  usage error, `page_not_applicable`, before any question is asked and
  before the station opens - a flag that would do nothing is not silently
  dropped.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Final

import typer

from railctl.catalog import CatalogEntry, load_catalog
from railctl.cli._errors import ABORTED_MESSAGE, PAGE_READ_ASSUME_YES, run
from railctl.cli._meta import (
    CV_PAGE_OPT,
    CV_READ_MODE_OPT,
    CV_SPEC_ARG,
    CV_WRITE_CONFIRM_OPT,
    CV_WRITE_CV_ARG,
    CV_WRITE_PAGE_OPT,
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
from railctl.cli.config import DEFAULT_TARGET, capabilities_path
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
from railctl.cli.result import CommandResult, error_code, tri_state
from railctl.errors import (
    REASON_VALUE_OUT_OF_RANGE,
    AbortedError,
    ConfirmationRequiredError,
    CvOutOfRangeError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    ServiceEncodingUnknownError,
)
from railctl.exit_codes import PARTIAL_EXIT_CODE
from railctl.station import (
    ADDRESS_CVS,
    CV144,
    INDEXED_CV_RANGE,
    PAGE_SELECTOR_CVS,
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    PageSelection,
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

#: The word `--confirm` must carry before CV8 is set to 8. Not a serial or any other token
#: bound to the decoder, and the difference from `restore`'s `--confirm=<live serial>` is
#: deliberate rather than an omission.
#:
#: `restore` is guarding against a MISMATCH - "this is not the decoder the file came from" -
#: so its token has to name the decoder in front of the operator, and it cannot be guessed
#: before the refusal prints it. A factory reset has nothing to mismatch against; the
#: question is only "did you mean this". The thing that must not answer it is `--yes`, which
#: a script carries because that is how anything is made non-interactive, not because its
#: author considered wiping a decoder. So the token names the OPERATION, and a caller has to
#: have written the word.
#:
#: A decoder-bound token would also be impossible on the main track: reading a serial needs
#: a CV read, and POM reading answers nothing on the reference station
#: (docs/probe-results.md R1). A gate that worked on one track and not the other would be
#: worse than one word that works on both.
FACTORY_RESET_TOKEN: Final[str] = "factory-reset"  # noqa: S105 - a confirmation word, not a secret

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
_CONFIRM_OPT = typer_option(CV_WRITE_CONFIRM_OPT)
_WRITE_PAGE_OPT = typer_option(CV_WRITE_PAGE_OPT)

#: The pair the reference MS450P22 rests on and refuses to leave, and on which
#: CV257-512 read as the normal CVs (docs/probe-results.md, "The ZIMO index bank
#: rests at 0:1 and will not leave it"). Named in the `index_page_required`
#: hint as the measured example - never applied without being typed, because
#: one decoder is one decoder and another may rest at 0:0.
RESTING_PAGE_EXAMPLE: Final[str] = "0:1"


#: What `cv write --page` does with CV31/CV32 after the write, per track - the
#: sentence the confirmation question carries, so the operator is told BEFORE
#: answering. The programming track mirrors `cv read --page`: the station reads
#: the pair first and re-selects it afterwards, because the ZIMO MS manual (EN,
#: p. 68) says to reset the CV page after programming and JMRI's ZIMO MS
#: definitions address CV257-512 by plain number without ever selecting a page
#: (xml/decoders/zimo/), so a page left behind sends the next tool into the
#: wrong bank. The main track cannot: no POM read answers on the reference
#: station (docs/probe-results.md R1), so there is no pair to put back.
_PAGE_AFTERWARDS: Final[dict[str, str]] = {
    TRACK_PROG: (
        "On the programming track the pair is read first and re-selected as found afterwards."
    ),
    TRACK_MAIN: (
        "On the main track nothing can read the pair, so the decoder is left on that "
        "page afterwards - the ZIMO MS manual (p. 68) says to reset CV31/CV32 after "
        "programming."
    ),
}


#: The example `cv read --page` suggests for a malformed token: the ZIMO audio
#: bank, harmless there because a read puts the pair back as found afterwards.
READ_PAGE_EXAMPLE: Final[str] = "145:0"


def parse_page(
    token: str | None,
    *,
    argv_hint: Sequence[str],
    example: str = READ_PAGE_EXAMPLE,
    without_page: Sequence[str] | None = None,
) -> CvPage | None:
    """`"145:0"` -> `(145, 0)`, the CV31/CV32 pair; `None` stays `None`.

    `example` is the page the usage suggestion names. It is the caller's to
    choose because the callers differ in what following it costs: `cv read`
    restores the pair afterwards, and so does `cv write` on the programming
    track, but a main-track `cv write` leaves the decoder on whatever page it
    selected - so `cv write` passes RESTING_PAGE_EXAMPLE, the page the bench
    already rests on, and never a bank that moves the decoder somewhere new.

    Whether the page applies to any requested CV is NOT decided here: `backup`
    shares this parser and has its own rules, so the `page_not_applicable`
    refusal lives in the command bodies (`_refuse_inapplicable_page`). But a
    command body that already knows no requested CV is indexed passes
    `without_page` - the argv its `page_not_applicable` refusal would suggest -
    and a malformed token then suggests that, because following an example
    page there only earns the second refusal.
    """
    if token is None:
        return None
    suggestion = list(without_page) if without_page is not None else [*argv_hint, "--page", example]
    parts = token.split(":")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise UsageProblem(
            f"--page takes the CV31 and CV32 values as two numbers separated by ':', got {token!r}",
            suggestions=[suggestion],
            details={"reason": "malformed_page", "page": token},
        )
    cv31, cv32 = (int(part) for part in parts)
    for name, value in (("CV31", cv31), ("CV32", cv32)):
        if not VALUE_MIN <= value <= VALUE_MAX:
            raise UsageProblem(
                f"--page {name} value {value} is outside {VALUE_MIN}..{VALUE_MAX}",
                suggestions=[suggestion],
                details={"reason": "page_value_not_a_byte", "field": name, "value": value},
            )
    return (cv31, cv32)


#: `details.reason` of the refusal below.
REASON_PAGE_NOT_APPLICABLE: Final[str] = "page_not_applicable"


def _refuse_inapplicable_page(
    page: CvPage | None, cvs: Sequence[int], *, command: str, retry: Sequence[str]
) -> None:
    """Refuse a `--page` that no CV in `cvs` can use, as a usage error.

    CV31/CV32 index CV257-512 and nothing else - NMRA S-9.2.2 Table 1 ("Primary
    / Secondary index for CV257-512") and the ZIMO MS manual (EN, p. 68) agree,
    and JMRI's `MultiIndexProgrammerFacade` writes the index only for an
    indexed CV id. A page given for CVs outside that window would select
    nothing and change nothing, and accepting it silently is how a caller who
    meant CV266 and typed CV26 learns nothing. A batch with at least one
    indexed CV keeps it: the page applies to that CV. `retry` is the caller's
    own argv without `--page`, which is the command that does what was asked.
    Raised before any confirmation and before the station opens.
    """
    if page is None or any(cv in INDEXED_CV_RANGE for cv in cvs):
        return
    named = ", ".join(f"CV{cv}" for cv in cvs) if len(cvs) <= 4 else f"{len(cvs)} CVs"
    raise UsageProblem(
        f"--page selects the ZIMO index page for CV257-512 only, and {command} was given "
        f"{named}, outside it; refused rather than ignored, because it would select nothing",
        suggestions=[list(retry)],
        details={"reason": REASON_PAGE_NOT_APPLICABLE, "page": _page_token(page), "cvs": list(cvs)},
    )


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


#: `details.page_selection.state` on a failed `cv write --page`. An OPEN set: a
#: consumer must treat a value it does not know as "unknown". Today `unknown`
#: is the only value - what the station did establish (a restore it confirmed,
#: the step that failed) rides beside it as `details.page_restore` and
#: `details.page_phase`, never folded into a state this layer did not measure.
PAGE_STATE_UNKNOWN: Final[str] = "unknown"

#: The read that settles which pair the decoder is on.
_READ_THE_PAIR: Final[str] = (
    "read the pair with `railctl cv read 31 32` on the programming track before trusting "
    "any CV257-512"
)


def _write_fact(write: object, *, cv: int) -> str:
    """The clause a restore-phase hint opens with: what the station recorded
    about the indexed CV (`details.write`, see `WriteState`) before the restore
    failed. Empty when nothing was recorded - then nothing is claimed."""
    if not isinstance(write, dict):
        return ""
    state = write.get("state")
    value = write.get("value")
    if state == "written":
        if write.get("verified") is True:
            return f"CV{cv} = {value} was written and confirmed by read-back, but "
        return f"CV{cv} = {value} was written but not confirmed by read-back, and "
    if state == "failed":
        return f"writing CV{cv} = {value} failed first ({write.get('error')}), and then "
    if state == "not_sent":
        return f"CV{cv} was not written (selecting the page failed first), and then "
    return ""


def _page_note(error: RailctlError, page: CvPage, *, cv: int, track: str) -> str:
    """The hint sentence for a failed `cv write --page`, from what the station
    recorded on the error - never from what this layer would like to be true.

    `details.page_phase` names the page step that failed (`pre_read`, `select`,
    `restore`; absent = the CV write itself, or an interrupt), and
    `details.page_restore` - present only on the programming track - is the
    pair the station put back after the failure and whether it read it back.
    """
    token = _page_token(page)
    phase = error.details.get("page_phase")
    restored = error.details.get("page_restore")
    if phase == "pre_read":
        # The station reads the pair BEFORE writing anything, and stops there.
        return (
            f"nothing was written: reading CV31/CV32 as found failed before --page {token} "
            f"was selected, so CV{cv} and the index page are as they were"
        )
    if phase == "restore":
        # This error replaced the write's own outcome, so the station put that
        # outcome on it (`details.write`); the hint says it before the page.
        return (
            f"{_write_fact(error.details.get('write'), cv=cv)}putting CV31/CV32 back to the "
            f"pair found before the write failed, so which index page the decoder is on is "
            f"unknown; {_READ_THE_PAIR}"
        )
    prefix = ""
    if phase == "select":
        # The selection runs before the indexed CV, so its failure is about
        # CV31/CV32 and never about CV{cv}: that write did not happen.
        prefix = f"CV{cv} was not written: selecting --page {token} failed before it; "
    if isinstance(restored, dict) and isinstance(restored.get("pair"), list):
        found = ":".join(str(v) for v in restored["pair"])
        if restored.get("verified") is True:
            return (
                f"{prefix}CV31/CV32 were put back to {found}, the pair found before the "
                f"write, and read back to confirm it"
            )
        return (
            f"{prefix}CV31/CV32 were written back to {found}, the pair found before the "
            f"write, but nothing read them back; {_READ_THE_PAIR}"
        )
    if track == TRACK_PROG:
        return (
            f"{prefix}--page {token} may already have written CV31/CV32 before this "
            f"failure, and whether the pair found before the write was put back is "
            f"unknown, so which index page the decoder is on is unknown; {_READ_THE_PAIR}"
        )
    return (
        f"{prefix}--page {token} may already have written CV31/CV32 before this failure, "
        f"and the main track never puts the pair back, so which index page the decoder "
        f"is on is unknown; {_READ_THE_PAIR}"
    )


def _note_page_left_unknown(
    error: RailctlError,
    page: CvPage,
    events: StationEventLog,
    *,
    cv: int,
    track: str,
    assume_yes: bool,
) -> None:
    """Record on a failed `cv write --page` what is known about the index page.

    The selection writes CV31/CV32 before the indexed CV. On the main track
    nothing puts the pair back, and a failure anywhere inside the write can
    leave the decoder on the selected page, on half of it (CV31 written, CV32
    not), or where it was; this layer cannot tell which. On the programming
    track the station read the pair first and puts it back after a failure as
    well, and says how that went on the error itself (`page_phase`,
    `page_restore`). The error envelope carries no warnings, so the fact goes
    into `details` and the hint, the two fields both output formats publish:
    `page_selection.state` is `"unknown"` (`PAGE_STATE_UNKNOWN`, an open set),
    never a guess, and the station events seen so far (a `page.unverified`
    among them) ride along instead of being dropped. A selection that read
    back as another pair keeps its own `reason: "page_selection_mismatch"`,
    `wrote` and `read_back` beside this record.
    """
    cv31, cv32 = page
    error.details = {
        **error.details,
        "page_selection": {
            "page": [cv31, cv32],
            "state": PAGE_STATE_UNKNOWN,
            "events": [name for name, _payload in events.events],
        },
    }
    note = _page_note(error, page, cv=cv, track=track)
    error.hint = f"{error.hint}; {note}" if error.hint else note
    # The suggested `cv read --page` writes CV31/CV32 and asks first, so it
    # carries `--yes` exactly when this write's question was answered by one;
    # without it a non-interactive caller is led to `confirmation_required`.
    # An attribute, not a `details` key: it shapes a suggestion, it is not a
    # fact about the station (`FunctionGroupUnreadableError.retry_argv` is the
    # same hand-off).
    setattr(error, PAGE_READ_ASSUME_YES, assume_yes)


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


def _page_token(page: CvPage) -> str:
    return f"{page[0]}:{page[1]}"


def _selection_json(selection: PageSelection | None) -> dict[str, object] | None:
    """`{"pair": [cv31, cv32], "verified": true|null}` - the same shape the
    station records as `details.page_restore` on a failed write."""
    if selection is None:
        return None
    return {"pair": list(selection.pair), "verified": selection.verified}


def _page_json(written: CvResult) -> dict[str, object] | None:
    """The result's `page`: `null` when no page was selected (a CV outside
    257-512), otherwise what the station reported about it -

    - `pair`: the CV31/CV32 values written before the CV;
    - `verified`: `true` when the pair was read back and matched, `null` when
      nothing could read it (POM) - never `false`, a mismatch is an error;
    - `source`: `"selected"`, this tool wrote the pair (an open set);
    - `restore`: `{pair, verified}` of the pair found before the write and put
      back after it (programming track), or `null` when nothing was put back -
      on the main track the decoder is left on `pair`.
    """
    if written.page is None:
        return None
    return {
        **(_selection_json(written.page) or {}),
        "source": written.page.source,
        "restore": _selection_json(written.page_restore),
    }


def _page_lines(written: CvResult) -> list[str]:
    """The human twin of `_page_json`, one fact per line."""
    selection = written.page
    if selection is None:
        return []
    token = _page_token(selection.pair)
    if selection.verified:
        lines = [f"index page CV31:CV32 = {token} selected and confirmed by read-back"]
    else:
        lines = [
            f"index page CV31:CV32 = {token} selected, not confirmed - nothing could read it back"
        ]
    restored = written.page_restore
    if restored is None:
        lines.append(f"the decoder is left on index page {token}")
    elif restored.verified:
        lines.append(
            f"index page put back to {_page_token(restored.pair)}, as found before the "
            f"write, and confirmed by read-back"
        )
    else:
        lines.append(
            f"index page written back to {_page_token(restored.pair)}, as found before the "
            f"write, not confirmed"
        )
    return lines


#: The station event `cv write` publishes itself instead of passing through.
#: The station emits it whenever a write returns `verified=None`, and
#: `build_cv_write` raises its own warning of the same name for exactly that
#: result - with a message the operator can act on. Passing both through gave
#: the envelope the same warning twice, the second time as the generic "the
#: station reported ..." sentence. `restore` drops the same event for its own
#: reason (`restore.SUPPRESSED_EVENT`).
WRITE_UNVERIFIED_EVENT: Final[str] = "cv.write_unverified"


class _WriteEventLog(StationEventLog):
    """`StationEventLog` that folds the station's `cv.write_unverified` into
    the command's own warning rather than publishing it a second time.

    Folded, not dropped: the event's `reason` is the one thing it says that the
    command's warning does not, so it is kept and `build_cv_write` puts it in
    that warning's details (JSON only - the human renderer prints a warning's
    message, not its details).

    Through `cv write` that reason is today always "verify=False: no read-back
    was attempted". A main-track write never verifies (`--verify` there is
    refused), and `Programming._blind_reason` answers `verify=False` before it
    looks at BLIND_WRITE_CVS or CV29, so the more specific reasons do not reach
    this command. The fold keeps whatever the station sends regardless.

    Suppression rests on the station emitting this event only for a write it
    returns with `verified=None` - the one result `build_cv_write` warns about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.unverified_reason: str | None = None

    def __call__(self, name: str, payload: dict[str, object]) -> None:
        if name == WRITE_UNVERIFIED_EVENT:
            reason = payload.get("reason")
            self.unverified_reason = reason if isinstance(reason, str) else None
            return
        super().__call__(name, payload)


def build_cv_write(
    written: CvResult,
    *,
    track: str,
    address: int | None,
    name: str,
    verify: bool,
    reason: str | None = None,
) -> CommandResult:
    """The one write, with `verified` carried honestly: `true` only when an
    independent read-back (or a decoder-level Ready) confirmed it, `null` when
    nothing measured the decoder - never `false`, which would claim a mismatch
    nobody measured (a real mismatch raises `CvVerifyError`) - and
    the reason it is not `true` said out loud.

    `page` comes from the station (`CvResult.page` and `.page_restore`), never
    from the `--page` token: what was selected, whether it was read back, and
    what was put back afterwards are the station's to say (`_page_json`). When
    the selection could not be read back - every POM selection on the
    reference station - `page.verified` is `null` and the `page.unverified`
    event says so beside this result. The top-level `verified` covers the CV
    that was written and nothing else.

    `reason` is the station's own account of why no read-back ran, taken from
    the `cv.write_unverified` event `_WriteEventLog` folds in; it rides in the
    warning's details when there is one, and is never invented here.
    """
    extra: dict[str, object] = {} if reason is None else {"reason": reason}
    result = CommandResult(schema=CV_WRITE_SCHEMA, command="cv write")
    label = _label(written.cv, name)
    result.result = {
        "cv": written.cv,
        "name": name,
        "value": written.value,
        "mode": written.mode.value,
        "track": track,
        "address": address,
        "page": _page_json(written),
        "verified": written.verified,
    }
    for line in _page_lines(written):
        result.say(line)
    result.say(
        f"{label} = {written.value} written ({written.mode.value} mode, "
        f"verified: {tri_state(written.verified)})"
    )
    if written.cv == FACTORY_RESET_CV and written.value == FACTORY_RESET_VALUE:
        # The gate makes the reset deliberate; this makes it legible afterwards. Without
        # it the run reports an ordinary CV write, and a log read months later says
        # `CV8 manufacturer_id = 8 written` - which only means anything to someone who
        # already knows what that value does. Both channels carry it: `factory_reset` in
        # the result for a script, this line for a person.
        result.result["factory_reset"] = True
        result.say("the decoder was FACTORY-RESET: every setting is back to its default")
    if written.verified:
        return result
    if track == TRACK_MAIN:
        # The read-back names the page because nothing confirmed the selection:
        # over POM, CV31/CV32 cannot be read back (`page.unverified`), so the
        # decoder may sit on another page. A service read without `--page` would
        # then read CV{n} from whichever page that is - it runs, and answers about
        # a different register. With `--page`, the read selects the bank the
        # write went to before it reads, and puts the pair back as it found it.
        read_back = f"railctl cv read {written.cv}"
        if written.page is not None:
            read_back += f" --page {_page_token(written.page.pair)}"
        result.warn(
            "cv.write_unverified",
            f"the write went out on the main track and cannot be checked there; put the "
            f"locomotive on the programming track and run `{read_back}` to verify it",
            cv=written.cv,
            **extra,
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
        **extra,
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
            # The caller's own argv without `--page`: every flag that changes
            # what is read or where rides along, resolved as `cv write` does,
            # and so does a `--yes` already given - a sweep over
            # SWEEP_CONFIRM_CVS asks again, and the suggestion must run as typed.
            page_retry = list(argv_hint)
            if mode != CV_READ_MODE_OPT.default:
                page_retry += ["--mode", mode]
            if settings.address is not None:
                page_retry += ["--address", str(settings.address)]
            if settings.target != DEFAULT_TARGET:
                page_retry += ["--target", settings.target]
            if settings.assume_yes:
                page_retry.append("--yes")
            indexed = any(cv in INDEXED_CV_RANGE for cv in cvs)
            declared_page = parse_page(
                page, argv_hint=argv_hint, without_page=None if indexed else page_retry
            )
            _refuse_inapplicable_page(declared_page, cvs, command="cv read", retry=page_retry)
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
        page: str | None = _WRITE_PAGE_OPT,
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
                # telegram, the bound named, `cv_out_of_range`.
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
            # The retry argv repeats what was actually typed (the raw CV token,
            # so a slug stays a slug) - `confirm` appends `--yes`.
            retry = [*prefix, cv, str(value)]
            if track == TRACK_MAIN:
                retry += ["--track", TRACK_MAIN]
            # Every flag that changes WHAT is written or WHERE rides along, so
            # the suggestion `confirm` publishes is the caller's own command and
            # not a different one: without `--address` the retry exits 2 on a
            # machine with no default address, or writes to the config's
            # locomotive instead of the one typed.
            # The RESOLVED address and target, not this verb's own copies of the
            # flags: `railctl --address 7 cv write ...` types the address BEFORE
            # the verb, where the root callback takes it and `address` here stays
            # `None`. Reading the parameter dropped it from the retry. A value from
            # the environment or config.toml rides along too, which names the
            # same locomotive and port the command was about to use.
            if verify is not None:
                retry.append("--verify" if verify else "--no-verify")
            if settings.address is not None:
                retry += ["--address", str(settings.address)]
            if settings.target != DEFAULT_TARGET:
                retry += ["--target", settings.target]
            # A `--page` on a CV outside 257-512 selects nothing: refused,
            # before any question and before the station opens. The suggestion
            # is this command without `--page`, keeping an answer already given
            # (`--yes`, `--confirm`) so it runs as typed.
            not_applicable_retry = list(retry)
            if confirm_token is not None:
                not_applicable_retry += ["--confirm", confirm_token]
            if settings.assume_yes:
                not_applicable_retry.append("--yes")
            # Parsed for every CV, so a malformed token is a usage error first -
            # the same order `cv read` checks in. The suggested page is the
            # resting one, never the read's example: a main-track write leaves
            # the decoder on whatever page it selected. For a CV no page
            # indexes, the suggestion drops `--page` instead.
            declared_page = parse_page(
                page,
                argv_hint=retry,
                example=RESTING_PAGE_EXAMPLE,
                without_page=None if number in INDEXED_CV_RANGE else not_applicable_retry,
            )
            _refuse_inapplicable_page(
                declared_page, [number], command="cv write", retry=not_applicable_retry
            )
            write_page: CvPage | None = None
            if number in INDEXED_CV_RANGE:
                if declared_page is None:
                    # Refused here, before the station opens, rather than by
                    # `ensure_page` after a port, a status read and (on main) a
                    # pre-flight - and with the flag this command now has named,
                    # instead of the station layer's generic wording.
                    raise IndexPageRequiredError(
                        f"CV{number} is behind a ZIMO index page (CV31/CV32), and cv write "
                        f"was given no --page; refused before any telegram went out",
                        hint=(
                            f"pass the decoder's index page as --page CV31:CV32, for example "
                            f"`{' '.join([*retry, '--page', RESTING_PAGE_EXAMPLE])}`. Read "
                            f"the live pair first with `railctl cv read 31 32` on the "
                            f"programming track: the reference MS450P22 rests at "
                            f"{RESTING_PAGE_EXAMPLE}, where CV257-512 are the normal CVs "
                            f"(docs/probe-results.md), but another decoder may not"
                        ),
                        cv=number,
                        details={"reason": "no_page_given"},
                    )
                # The one path through `cv write` that writes a CV the operator
                # did not name: the selection writes CV31/CV32, which are in
                # CONFIRM_CVS precisely because later indexed access lands where
                # they point. Same gate as `cv read --page`, and BEFORE the
                # station opens - a refusal must cost no port.
                cv31, cv32 = declared_page
                confirm(
                    f"--page selects the ZIMO index page by WRITING CV31={cv31} and "
                    f"CV32={cv32} before CV{number} - later indexed CV reads and writes "
                    f"land where they point. {_PAGE_AFTERWARDS[track]} Write CV31/CV32",
                    settings=settings,
                    stdin=sys.stdin,
                    stderr=output.stderr,
                    retry_argv=[*retry, "--page", str(page)],
                )
                write_page = declared_page
            if number in CONFIRM_CVS:
                if number == FACTORY_RESET_CV and value == FACTORY_RESET_VALUE:
                    # The one question `--yes` does not answer. See FACTORY_RESET_TOKEN for
                    # why the token names the operation rather than the decoder. Raised
                    # BEFORE `confirm`, so `--yes` cannot short-circuit past it, and before
                    # the station opens - a refusal must cost no port.
                    if confirm_token != FACTORY_RESET_TOKEN:
                        raise ConfirmationRequiredError(
                            f"writing {FACTORY_RESET_VALUE} to CV{FACTORY_RESET_CV} "
                            f"FACTORY-RESETS the decoder - every setting is wiped, the "
                            f"address included (vendor manual p.30). This one is not "
                            f"answered by --yes",
                            hint=(
                                f"rerun with --confirm={FACTORY_RESET_TOKEN} if this is "
                                f"deliberate; --yes does not answer this"
                            ),
                            retry_argv=[*retry, "--confirm", FACTORY_RESET_TOKEN],
                        )
                    # The token IS the confirmation, so no question follows it. Asking as
                    # well would mean a caller who named the operation still needed `--yes`,
                    # and two flags for one answer is how one of them stops being read.
                else:
                    if confirm_token is not None:
                        # `--confirm` answers exactly one question. Accepting it anywhere
                        # else turns it into a second `--yes`, which is what it exists not
                        # to be - and a caller who passes it defensively everywhere finds
                        # out here rather than believing it covered something.
                        raise UsageProblem(
                            f"--confirm answers only the CV{FACTORY_RESET_CV}="
                            f"{FACTORY_RESET_VALUE} factory reset, and this write is not "
                            f"one",
                            suggestions=[[*retry, "--yes"]],
                        )
                    confirm(
                        _confirm_question(number, value),
                        settings=settings,
                        stdin=sys.stdin,
                        stderr=output.stderr,
                        retry_argv=retry,
                    )
            wanted = MODE_FOR_TRACK[track]
            effective_verify = (track == TRACK_PROG) if verify is None else verify
            events = _WriteEventLog()
            station = open_station(settings, capabilities_path=capabilities_path(), on_event=events)
            try:
                resolved_address: int | None = None
                if wanted is ProgMode.POM:
                    address_hint = list(retry)
                    if write_page is not None:
                        address_hint += ["--page", str(page)]
                    if settings.assume_yes:
                        # The question was answered by --yes (typed before or after
                        # the verb, or from the environment); a suggestion without it
                        # would only lead back to confirmation_required.
                        address_hint.append("--yes")
                    resolved_address = require_address(settings, argv_hint=address_hint)
                    preflight(station, speed=None, action="a main-track (POM) CV write")
                else:
                    print(PROG_TRACK_NOTICE, file=output.stderr)
                try:
                    written = station.cv_write(
                        number,
                        value,
                        address=resolved_address,
                        mode=wanted,
                        page=write_page,
                        verify=effective_verify,
                        # Leave-as-found on the programming track only: over POM
                        # no read can tell what pair to put back.
                        restore_page=wanted is ProgMode.SERVICE,
                    )
                except RailctlError as exc:
                    if write_page is not None:
                        _note_page_left_unknown(
                            exc,
                            write_page,
                            events,
                            cv=number,
                            track=track,
                            assume_yes=settings.assume_yes,
                        )
                    raise
                except KeyboardInterrupt:
                    # Ctrl-C between CV31 and CV32 leaves the page just as unknown as
                    # a failure does, and `run()` would report a bare `aborted` with
                    # empty details. Raised as the same `AbortedError` `run()` builds
                    # (same code, message and exit code), carrying the page note -
                    # the route `restore` already takes for its own interrupt.
                    if write_page is None:
                        raise
                    aborted = AbortedError(ABORTED_MESSAGE)
                    _note_page_left_unknown(
                        aborted,
                        write_page,
                        events,
                        cv=number,
                        track=track,
                        assume_yes=settings.assume_yes,
                    )
                    raise aborted from None
                outcome = build_cv_write(
                    written,
                    track=track,
                    address=resolved_address,
                    name=name,
                    verify=effective_verify,
                    reason=events.unverified_reason,
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
