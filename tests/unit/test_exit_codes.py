# tests/unit/test_exit_codes.py
"""One test per documented exit-code row, plus the whole-tree invariants."""

from __future__ import annotations

import inspect

import pytest

from railctl import errors
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


def _tree(root: type[RailctlError] = RailctlError) -> set[type[RailctlError]]:
    found = {root}
    for sub in root.__subclasses__():
        found |= _tree(sub)
    return found


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TransportError("x"), 3),
        (ProtocolError("x"), 4),
        (LinkTimeout("x"), 5),
        (UnsupportedCommandError("x"), 6),
        (UnsupportedFeatureError("x"), 7),
        (RailctlError("x"), 9),
        (DecoderNoAckError("x"), 10),
        (ShortCircuitError("x"), 11),
        (StationBusyError("x"), 12),
        (DecoderNotRespondingError("x"), 13),
        (CvVerifyError("x"), 14),
        (CvOutOfRangeError("x"), 15),
        (PomReadUnsupportedError("x"), 16),
        (IndexPageRequiredError("x"), 17),
        (ServiceEncodingUnknownError("x"), 18),
        (ProgrammingError("x"), 19),
        (TrackPowerError("x"), 20),
        (ConfirmationRequiredError("x"), 2),
    ],
)
def test_every_documented_exit_code_row(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PortNotFound("x"), 3),
        (AmbiguousPort("x"), 3),
        (PortBusy("x"), 3),
        (PortConfigError("x"), 3),
        (PortNotOpen("x"), 3),
        (PortNotXpressNet("x"), 3),
        (XBusEncodeError("x"), 4),
        (XBusDecodeError("x"), 4),
        (XBusChecksumError("x"), 4),
        (LinkProtocolError("x"), 4),
        (StationError("x"), 9),
        (AbortedError("x"), 9),
        (CatalogError("x"), 9),
        # M9's three backup exits share 9: BackupIncompleteError through its
        # StationError parent, BackupFileError straight off the base row.
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


def test_the_map_has_no_duplicate_codes():
    codes = list(EXIT_CODES.values())
    assert len(codes) == len(set(codes))


def test_nothing_maps_to_the_unmapped_code():
    assert UNMAPPED_EXIT_CODE not in EXIT_CODES.values()


def test_an_exception_from_outside_the_tree_gets_the_unmapped_code():
    assert exit_code_for(RuntimeError("boom")) == UNMAPPED_EXIT_CODE


def test_silence_a_refusal_and_out_of_scope_are_three_different_exit_codes():
    """M1's defining failure was silence read as "no". These three must never collapse.

    docs/probe-results.md records the POM read as unknown rather than false
    because the station answered 01 04 05 and then nothing - not 61 82. A caller
    reading only $? has to be able to tell those apart.
    """
    silence = exit_code_for(LinkTimeout("no reply in 5.0 s"))
    refusal = exit_code_for(UnsupportedCommandError("station answered 61 82"))
    out_of_scope = exit_code_for(UnsupportedFeatureError("consists are out of scope"))
    assert len({silence, refusal, out_of_scope}) == 3
    assert exit_code_for(RailctlError("x")) not in {silence, refusal, out_of_scope}


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


def test_an_unprobed_station_and_a_bad_cv_number_do_not_share_an_exit_code():
    """The two used to be one class, and a script could not tell them apart
    (issue #16). They ask the operator for different things: a range error is
    fixed by typing another CV number, an unprobed station by running
    `railctl doctor`. Sharing a code would leave a caller with no way to
    decide which, and the CLI contract forbids repurposing a code later.
    """
    assert exit_code_for(ServiceEncodingUnknownError("x")) != exit_code_for(CvOutOfRangeError("x"))
