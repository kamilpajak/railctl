"""Property tests for the capability verdicts.

This is where the probe decides what to claim about the hardware, and the whole
design rests on one asymmetry: **only a contradiction may produce False.**

XpressNet section 2.2.23 and Lenz 23151 section 1.4 give non-support its own
signal, `61 82`. Everything else - silence, a busy station, a short circuit, a
reply form nobody parsed yet, a locomotive that is not on the track - is
indistinguishable from a capability that exists and did not answer this time.
Recording any of those as False is how this project produced four confidently
wrong conclusions before the probe was trusted.

The tests below state that asymmetry once, over generated reply sequences,
rather than one example at a time. `test_only_an_explicit_rejection_can_ever
_produce_false` is the one that matters: it holds every check to the rule
simultaneously, so a new check cannot quietly opt out of it.
"""

from __future__ import annotations

import itertools

from hypothesis import given
from hypothesis import strategies as st

from tools.probe import commands
from tools.probe.checks import (
    REFERENCE_CV,
    REFERENCE_VALUE,
    check_function_groups,
    check_pom_read,
    check_single_function,
    check_z21_opcodes,
)
from tools.probe.fake import FakeLink
from tools.probe.frames import build

ADDRESS = 3

# The reply vocabulary, grouped by what each telegram is allowed to establish.
# UNSUPPORTED sits alone on purpose: it is the only member of the alphabet that
# a check may turn into False.
UNSUPPORTED = b"\x61\x82"
TRANSIENTS = [
    b"\x61\x12",  # programming short circuit
    b"\x61\x08",  # track short circuit
    b"\x61\x1f",  # programming busy
    b"\x61\x81",  # command station busy
    b"\x61\x80",  # transfer error
]
INCONCLUSIVE = [
    b"\x01\x04",  # ACK
    b"\x61\x11",  # ready
    b"\x61\x13",  # no acknowledgement from decoder
    b"\x61\x02",  # entered programming mode
    b"\x61\x55",  # not in the 0x61 table at all: parses as Unknown
    b"\x63\x10\x01\x05",  # Register/Paged fallback - a register, never a CV
    b"\xe3\x52\x00\x00",  # F13-F28 state, irrelevant to these checks
]

# Every telegram that is not an explicit rejection.
NON_REJECTIONS = TRANSIENTS + INCONCLUSIVE

Z21_REFERENCE = commands.z21_service_read(REFERENCE_CV)
POM_REFERENCE = commands.pom_read(ADDRESS, REFERENCE_CV)
SINGLE_F0 = commands.single_function(ADDRESS, 0, 0)
GROUP4 = commands.function_group(ADDRESS, 0x23, 0)
GROUP5 = commands.function_group(ADDRESS, 0x28, 0)


def wire(telegrams: list[bytes]) -> list[bytes]:
    return [build(t) for t in telegrams]


def run_z21(telegrams: list[bytes]):
    return check_z21_opcodes(FakeLink({Z21_REFERENCE: wire(telegrams)})).value


def run_single_function(telegrams: list[bytes]):
    link = FakeLink({SINGLE_F0: wire(telegrams)})
    return check_single_function(link, address=ADDRESS, f0_is_on=False).value


def run_groups(group4: list[bytes], group5: list[bytes]):
    link = FakeLink({GROUP4: wire(group4), GROUP5: wire(group5)})
    return check_function_groups(link, address=ADDRESS, f13_f20=0, f21_f28=0).value


def run_pom(telegrams: list[bytes]):
    link = FakeLink({POM_REFERENCE: wire(telegrams)})
    return check_pom_read(link, ADDRESS, REFERENCE_CV, poll=False).value["pom_read"]


replies = st.lists(st.sampled_from(NON_REJECTIONS), max_size=4)


@given(replies, replies, replies, replies)
def test_only_an_explicit_rejection_can_ever_produce_false(
    z21: list[bytes], single: list[bytes], group4: list[bytes], pom: list[bytes]
):
    """Nothing short of `61 82` may be reported as a missing capability.

    The station has exactly one way to say "I do not implement that". Silence,
    a busy station, a short circuit and an unparsed reply all mean the same
    thing to this probe: not established.
    """
    assert run_z21(z21) is not False
    assert run_single_function(single) is not False
    assert run_groups(group4, group4) is not False
    assert run_pom(pom) is not False


@given(replies, replies, st.integers(min_value=0, max_value=4))
def test_a_rejection_anywhere_in_the_replies_is_still_a_rejection(
    before: list[bytes], after: list[bytes], _seed: int
):
    """`61 82` must not be masked by whatever else shares the window with it."""
    telegrams = [*before, UNSUPPORTED, *after]
    assert run_z21(telegrams) is False
    assert run_single_function(telegrams) is False
    assert run_pom(telegrams) is False


@given(st.lists(st.sampled_from(TRANSIENTS), min_size=1, max_size=4))
def test_a_transient_station_condition_establishes_nothing(telegrams: list[bytes]):
    """Busy and short-circuit replies say the station could not act right now.

    Reading one as acceptance is what happened while `61 80` and `61 81` went
    unparsed: they fell through to the "some frame came back, so it must have
    been accepted" branch and recorded an unsupported command as supported.
    """
    assert run_z21(telegrams) is None
    assert run_single_function(telegrams) is None
    assert run_groups(telegrams, telegrams) is None


def test_silence_establishes_nothing():
    assert run_z21([]) is None
    assert run_single_function([]) is None
    assert run_groups([], []) is None
    assert run_pom([]) is None


@given(st.lists(st.sampled_from(INCONCLUSIVE), min_size=1, max_size=4))
def test_an_ordinary_reply_is_acceptance_for_a_fire_and_forget_command(
    telegrams: list[bytes],
):
    """The function commands produce no result telegram of their own, so any
    reply that is neither a rejection nor a transient condition means the
    station took the command."""
    assert run_single_function(telegrams) is True
    assert run_groups(telegrams, telegrams) is True


# Kleene three-valued AND. A confirmed rejection of either group settles the
# pair even when the other group never answered, so False must beat None.
KLEENE_AND = {
    (True, True): True,
    (True, None): None,
    (True, False): False,
    (None, True): None,
    (None, None): None,
    (None, False): False,
    (False, True): False,
    (False, None): False,
    (False, False): False,
}
OUTCOME_REPLIES = {True: [b"\x01\x04"], False: [UNSUPPORTED], None: []}


def test_the_function_groups_combine_by_three_valued_and():
    """Testing for None before False would discard knowledge we already have and
    report "unknown" for a pair we know is unusable."""
    for group4, group5 in itertools.product([True, False, None], repeat=2):
        expected = KLEENE_AND[(group4, group5)]
        actual = run_groups(OUTCOME_REPLIES[group4], OUTCOME_REPLIES[group5])
        assert actual is expected, f"g4={group4} g5={group5} gave {actual}, want {expected}"


@given(st.integers(min_value=0, max_value=255))
def test_a_reference_read_is_only_believed_when_the_constant_matches(value: int):
    """CV8 holds the NMRA manufacturer id, 145 on every ZIMO decoder.

    A different number means the read went somewhere else - the wrong CV, a
    stale result, another decoder - so it must not be reported as a working
    opcode. It must not be reported as a broken one either: the opcode plainly
    answered. The only honest verdict is unresolved.
    """
    verdict = run_z21([bytes([0x63, 0x14, REFERENCE_CV, value])])
    assert verdict is (True if value == REFERENCE_VALUE else None)
    assert verdict is not False


@given(st.integers(min_value=0, max_value=255), st.integers(min_value=0, max_value=255))
def test_a_pom_result_for_another_cv_is_not_an_answer_to_this_request(
    echo: int, value: int
):
    """The echoed CV number decides whether the reply belongs to this request.

    Accepting a mismatched echo would publish another CV's value under this
    CV's name - a number that looks entirely ordinary and is from the wrong
    place. The two accepted echoes are the CV itself and its zero-based form,
    because which one the station uses is precisely what this check measures.
    """
    result = run_pom([bytes([0x63, 0x14, echo, value])])
    if echo in (REFERENCE_CV, commands.cv_wire(REFERENCE_CV)):
        assert result is True
    else:
        assert result is None
