# tests/unit/test_exit_codes.py
"""One test per documented exit-code row, plus the whole-tree invariants.

The literals here are deliberate and must stay literals. This file is the SECOND
copy of the contract `railctl/exit_codes.py` declares, and its whole job is to make
an edit to the first copy show up in a diff as a contract change rather than a
refactor. `tests/test_exit_code_literals.py` excludes this file by name for that
reason - everywhere else in the suite a bare exit-code number is a defect.
"""

from __future__ import annotations

import inspect

import pytest

from railctl import errors
from railctl.cli._errors import default_suggestions
from railctl.cli.result import RETRYABLE_CODES, error_code
from railctl.errors import (
    EXIT_CODES,
    UNMAPPED_EXIT_CODE,
    AbortedError,
    AmbiguousPort,
    BackupFileError,
    BackupIncompleteError,
    CatalogError,
    ConfirmationRequiredError,
    CvOutOfRangeError,
    CvVerifyError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    LinkProtocolError,
    LinkTimeout,
    PomReadUnsupportedError,
    PortBusy,
    PortConfigError,
    PortNotFound,
    PortNotOpen,
    PortNotXpressNet,
    ProgrammingError,
    ProtocolError,
    RailctlError,
    ServiceEncodingUnknownError,
    ShortCircuitError,
    StationBusyError,
    StationError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
    UnsupportedFeatureError,
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    exit_code_for,
)
from railctl.exit_codes import (
    DOMAIN_FAILURE_EXIT_CODE,
    INTERRUPTED_EXIT_CODE,
    NOT_FOUND_EXIT_CODE,
    PUBLISHED_EXIT_CODES,
    RETRYABLE_EXIT_CODE,
    USAGE_EXIT_CODE,
)


def _tree(root: type[RailctlError] = RailctlError) -> set[type[RailctlError]]:
    found = {root}
    for sub in root.__subclasses__():
        found |= _tree(sub)
    return found


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        # The three a caller may usefully retry, and the only distinction $? still draws.
        (LinkTimeout("x"), 7),
        (StationBusyError("x"), 7),
        (PortBusy("x"), 7),
        # A port that was named and is not there - the one "not found" this tool has.
        (PortNotFound("x"), 4),
        # Refused before anything ran, so the command line is what to fix.
        (ConfirmationRequiredError("x"), 2),
        # The operator stopped it.
        (AbortedError("x"), 130),
        # Everything else. Before #65 each of these owned a number of its own, which
        # is what made $? a second spelling of error.code; the detail lives there now.
        (RailctlError("x"), 9),
        (TransportError("x"), 9),
        (ProtocolError("x"), 9),
        (UnsupportedCommandError("x"), 9),
        (UnsupportedFeatureError("x"), 9),
        (DecoderNoAckError("x"), 9),
        (ShortCircuitError("x"), 9),
        (DecoderNotRespondingError("x"), 9),
        (CvVerifyError("x"), 9),
        (CvOutOfRangeError("x"), 9),
        (PomReadUnsupportedError("x"), 9),
        (IndexPageRequiredError("x"), 9),
        (ServiceEncodingUnknownError("x"), 9),
        (ProgrammingError("x"), 9),
        (TrackPowerError("x"), 9),
    ],
)
def test_every_documented_exit_code_row(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        # The transport leaves no longer share one code: two of them earned a place in
        # the small set on their own meaning, and the rest fall to the base.
        (AmbiguousPort("x"), 9),
        (PortConfigError("x"), 9),
        (PortNotOpen("x"), 9),
        (PortNotXpressNet("x"), 9),
        (XBusEncodeError("x"), 9),
        (XBusDecodeError("x"), 9),
        (XBusChecksumError("x"), 9),
        (LinkProtocolError("x"), 9),
        (StationError("x"), 9),
        (CatalogError("x"), 9),
        (BackupIncompleteError("x"), 9),
        (BackupFileError("x"), 9),
    ],
)
def test_subclasses_without_their_own_row_inherit_the_parent_code(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code


def test_every_class_in_the_tree_resolves_to_a_code_above_one():
    """Builds each class with k.__new__(k), not k("x").

    exit_code_for reads only type(exc).__mro__ and never touches instance state, so an
    uninitialised instance is safe here. This removes the coupling to constructor
    signatures: a future exception with a required keyword argument would otherwise raise
    TypeError inside this comprehension, and the failure would read as unrelated. Plain
    object.__new__(k) does not work here: BaseException defines its own __new__, and calling
    object.__new__ directly on a class that inherits it is refused as unsafe.
    """
    unresolved = sorted(
        k.__name__ for k in _tree() if exit_code_for(k.__new__(k)) == UNMAPPED_EXIT_CODE
    )
    assert unresolved == []


def test_no_entry_in_the_map_is_orphaned():
    assert set(EXIT_CODES) <= _tree()


def test_no_class_maps_to_a_code_outside_the_published_eight():
    """Replaces the old uniqueness test, which #65 made false ON PURPOSE.

    Sharing a code is now the design - some thirty classes land on 9 - so "no two
    classes share a number" had to go. What it really guarded was "a code cannot be
    claimed quietly", and that survives as this: a class may only resolve to a value
    the contract publishes.
    """
    assert {exit_code_for(k.__new__(k)) for k in _tree()} <= PUBLISHED_EXIT_CODES


def test_the_codes_classes_can_produce_are_exactly_these():
    """Frozen, so a code appearing AND a code going missing both show up."""
    assert {exit_code_for(k.__new__(k)) for k in _tree()} == {
        USAGE_EXIT_CODE,
        NOT_FOUND_EXIT_CODE,
        RETRYABLE_EXIT_CODE,
        DOMAIN_FAILURE_EXIT_CODE,
        INTERRUPTED_EXIT_CODE,
    }


def test_exactly_the_retryable_codes_exit_with_the_retryable_status():
    """The identity the naming was chosen for, checked in both directions.

    `RETRYABLE_EXIT_CODE` and `result.RETRYABLE_CODES` must describe one set. A class
    added to the map without being added to the string set makes a caller retry a
    permanent refusal - which is the exact defect #65 was raised to fix - and a class
    added to the string set without the map makes them give up on something transient.
    """
    retry_by_number = {
        error_code(k.__new__(k))
        for k in _tree()
        if exit_code_for(k.__new__(k)) == RETRYABLE_EXIT_CODE
    }
    assert retry_by_number == set(RETRYABLE_CODES)


def test_the_error_code_strings_are_still_one_per_class():
    """The uniqueness guarantee did not vanish; it moved.

    Exit codes stopped separating 39 classes, so `error.code` is now the only thing
    that does. Asserted here, beside the map that gave it up, rather than only in the
    schema tests - the relocation should be visible where the loss happened.
    """
    codes = [k.code for k in _tree()]
    assert len(codes) == len(set(codes))


def test_nothing_maps_to_the_unmapped_code():
    assert UNMAPPED_EXIT_CODE not in EXIT_CODES.values()


def test_an_exception_from_outside_the_tree_gets_the_unmapped_code():
    assert exit_code_for(RuntimeError("boom")) == UNMAPPED_EXIT_CODE


def test_silence_a_refusal_and_out_of_scope_stay_three_different_answers():
    """M1's defining failure was silence read as "no". These three must never collapse.

    docs/probe-results.md records the POM read as unknown rather than false
    because the station answered 01 04 05 and then nothing - not 61 82. A caller
    reading only $? has to be able to tell those apart.

    That last sentence stopped being true in 0.3.0 and this test is where it is
    recorded. The separation moved to `error.code`, which is now the only channel
    carrying it (#65, and CLAUDE.md's amended founding rule). What $? still says is
    the one thing a caller can act on without reading anything: silence is worth
    retrying and the two refusals are not.
    """
    silence = LinkTimeout("no reply in 5.0 s")
    refusal = UnsupportedCommandError("station answered 61 82")
    out_of_scope = UnsupportedFeatureError("consists are out of scope")

    assert len({error_code(silence), error_code(refusal), error_code(out_of_scope)}) == 3
    assert exit_code_for(silence) == RETRYABLE_EXIT_CODE
    assert exit_code_for(refusal) == exit_code_for(out_of_scope) == DOMAIN_FAILURE_EXIT_CODE


def test_the_base_carries_an_optional_hint():
    assert RailctlError("boom").hint is None
    assert RailctlError("boom", hint="try doctor").hint == "try doctor"
    assert str(RailctlError("boom", hint="try doctor")) == "boom"


def test_a_programming_error_carries_the_human_cv_number():
    assert ProgrammingError("bad").cv is None
    assert CvVerifyError("mismatch", cv=8, hint="re-read").cv == 8
    assert CvVerifyError("mismatch", cv=8).hint is None


def test_errors_is_the_only_module_defining_exception_types():
    """Only sees classes reachable through __subclasses__(), which only finds imported classes.

    A rogue exception class in a module nobody imports is invisible to this test.
    tests/test_layering.py RULE_3 is the other half: a text scan that catches an exception
    class outside errors.py whether or not anything ever imports it.
    """
    classes = [
        name
        for name, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, RailctlError)
    ]
    assert {obj.__module__ for obj in _tree()} == {"railctl.errors"}
    assert len(classes) == len(_tree())


def test_railctl_error_details_defaults_to_empty_and_round_trips():
    """`RailctlError` itself carries `details` - `ProgrammingError` only adds
    `cv` alongside it, it does not introduce the field."""
    bare = RailctlError("x")
    assert bare.details == {}
    carrying = RailctlError("x", details={"cv": 8, "attempts": 3})
    assert carrying.details == {"cv": 8, "attempts": 3}
    programming = ProgrammingError("x", cv=8, details={"attempts": 3})
    assert programming.cv == 8
    assert programming.details == {"attempts": 3}


def test_an_unprobed_station_and_a_bad_cv_number_stay_tellable_apart():
    """The two used to be one class, and a script could not tell them apart
    (issue #16). They ask the operator for different things: a range error is
    fixed by typing another CV number, an unprobed station by running
    `railctl doctor`. Sharing a code would leave a caller with no way to
    decide which, and the CLI contract forbids repurposing a code later.

    They share exit 9 since 0.3.0, because the exit code stopped naming failures at
    all. `error.code` keeps them apart, and so does the suggestion each one carries -
    which is the difference the issue was actually about.
    """
    unprobed, bad_number = ServiceEncodingUnknownError("x"), CvOutOfRangeError("x")

    assert error_code(unprobed) != error_code(bad_number)
    assert default_suggestions(unprobed, command="cv read") != default_suggestions(
        bad_number, command="cv read"
    )


def test_the_two_numbers_railctl_shares_with_its_dependencies_still_coincide():
    """Two of the eight were chosen to match a number typer and Click already use.

    `USAGE_EXIT_CODE` equals `ClickUsageError.exit_code` so that a refusal Click answers
    by itself and one railctl answers through `main()` leave the same status - a caller
    cannot tell which layer refused, and should not have to. `INTERRUPTED_EXIT_CODE`
    equals `main.TYPER_INTERRUPT_EXIT_CODE`, the number typer invents for a Ctrl-C it
    catches while parsing, so all three interrupt routes end alike.

    Both are coincidences that the code depends on, which is why they are asserted
    rather than assumed - and why the two names are never merged. `tests/conftest.py`'s
    canary shifts railctl's constants and leaves typer's and Click's alone, so it would
    turn a merge into a green run with a broken contract; this file is the one the canary
    skips, which makes it the only place the equality can be stated.
    """
    from railctl.cli._click_errors import ClickUsageError
    from railctl.cli.main import TYPER_INTERRUPT_EXIT_CODE

    assert USAGE_EXIT_CODE == ClickUsageError.exit_code == 2
    assert INTERRUPTED_EXIT_CODE == TYPER_INTERRUPT_EXIT_CODE == 130
