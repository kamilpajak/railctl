"""Pinning tests written to kill surviving mutants in `checks.py`.

Provenance: cosmic-ray run of 2026-08-04, 464 mutants, 261 killed by the prior
suite. See docs/test-hardening.md for the full triage and for the survivors that
no in-process test can reach.

The gap this file closes is structural rather than incidental. `FakeLink` answers
the same payload with the same bytes every time, so a station that replies only
after being asked a second time cannot be expressed with it - and the polling
loop in `_read_value` exists precisely for stations that behave that way. Every
mutant inside that loop therefore survived, including one that deletes the loop
outright.

That mutant is not academic. The missing `21 10` poll was the largest single
error of M1: it made the whole Lenz opcode family look unimplemented, invented a
4-5 second spacing requirement, and inflated a 2 minute backup into an estimated
6-7 minutes. Three conclusions, all wrong, all from one absent poll. `SequencedLink`
below is the seam that lets a test notice it coming back.
"""

from __future__ import annotations

import pytest

from tools.probe import checks, commands
from tools.probe.checks import (
    REFERENCE_CV,
    SERVICE_POLLS,
    check_address_band,
    check_identity,
    check_loco_info,
    check_pom_read,
    check_service_ext_cv,
    check_single_function,
    check_z21_opcodes,
)
from tools.probe.fake import FakeLink
from tools.probe.frames import Frame, build, split_frames

ADDRESS = 3
SERVICE_RESULT = commands.service_result()
Z21_REFERENCE = commands.z21_service_read(REFERENCE_CV)

CV8_IS_145 = b"\x63\x14\x08\x91"  # the ZIMO manufacturer id, the probe's anchor
READY = b"\x61\x11"


class SequencedLink:
    """A Link whose answer to the same payload changes from call to call.

    `FakeLink` maps a payload to one fixed reply, which cannot express the
    behaviour the service-mode poll exists for: a station that answers nothing
    to the read itself and produces the value only when asked with `21 10`.
    Each payload here owns a queue of replies, consumed one exchange at a time,
    and an exhausted queue means silence.
    """

    def __init__(self, script: dict[bytes, list[list[bytes]]]) -> None:
        self.script = {payload: list(turns) for payload, turns in script.items()}
        self.sent: list[bytes] = []

    def exchange(self, payload: bytes, *, window: float) -> list[Frame]:
        self.sent.append(payload)
        queue = self.script.get(payload)
        telegrams = queue.pop(0) if queue else []
        frames, _ = split_frames(b"".join(build(t) for t in telegrams))
        return frames

    def collect(self, *, window: float) -> list[Frame]:
        return []


def polls_made(link: SequencedLink) -> int:
    return link.sent.count(SERVICE_RESULT)


# ---------------------------------------------------------------------------
# The polling loop in _read_value
# ---------------------------------------------------------------------------


def test_a_value_that_only_arrives_after_a_poll_is_still_read():
    """The regression that cost M1 three wrong conclusions.

    Measured on the YD7010: `23 11` answers unsolicited, while `22 15`, `22 18`
    and `22 19` return nothing at all until asked with `21 10`, and then answer
    correctly every time. Without the poll the entire Lenz family reads as
    silent, and silence is how this probe records a missing capability.
    """
    link = SequencedLink({Z21_REFERENCE: [[]], SERVICE_RESULT: [[CV8_IS_145]]})
    result = check_z21_opcodes(link)
    assert result.value is True
    assert polls_made(link) == 1
    assert "145" in result.detail


def test_the_polled_frames_are_added_to_what_the_read_returned():
    """The poll's frames must join the read's, not replace them: the audit trail
    in the report has to show both halves of the exchange."""
    link = SequencedLink({Z21_REFERENCE: [[READY]], SERVICE_RESULT: [[CV8_IS_145]]})
    result = check_z21_opcodes(link)
    assert result.value is True
    assert result.frames == ["FE 61 11", "FE 63 14 08 91"]


def test_a_value_arriving_on_the_last_allowed_poll_is_not_dropped():
    """The classification after the loop is a real safety net, not dead code.

    A value delivered by the final poll arrives with no iteration left to notice
    it, so only the post-loop pass can see it. Without that pass the read is
    reported as silent - having just succeeded.
    """
    link = SequencedLink(
        {
            Z21_REFERENCE: [[]],
            SERVICE_RESULT: [[READY]] * (SERVICE_POLLS - 1) + [[CV8_IS_145]],
        }
    )
    result = check_z21_opcodes(link)
    assert result.value is True
    assert polls_made(link) == SERVICE_POLLS


def test_a_station_that_never_answers_is_given_up_on_after_four_polls():
    """The poll budget is bounded, and the bound is four.

    `SERVICE_POLL_WINDOW` is three seconds, so this is the difference between a
    dead CV costing twelve seconds and it costing forever. Backing up 77 CVs
    turns any per-read budget into minutes.
    """
    link = SequencedLink({Z21_REFERENCE: [[]], SERVICE_RESULT: [[READY]] * 20})
    result = check_z21_opcodes(link)
    assert result.value is None
    assert polls_made(link) == 4


def test_polling_stops_as_soon_as_the_station_goes_quiet():
    """An empty poll means there is nothing stored to collect, so asking three
    more times only spends the window again."""
    link = SequencedLink({Z21_REFERENCE: [[]], SERVICE_RESULT: []})
    assert check_z21_opcodes(link).value is None
    assert polls_made(link) == 1


# ---------------------------------------------------------------------------
# What a verdict is allowed to say
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (None, "(silent)"),
        (b"\x61\x1f", "(transient)"),
        (b"\x63\x10\x01\x05", "Register/Paged"),
    ],
    ids=["silent", "transient", "register-fallback"],
)
def test_an_unresolved_verdict_still_has_to_explain_itself_correctly(
    reply: bytes | None, fragment: str
):
    """These three all produce `None`, and are not interchangeable.

    Because the verdict is the same, a mutation that swaps one explanation for
    another changes nothing a test was checking - which is how "the station fell
    back to Register/Paged mode" survives as the stated reason for a station
    that simply said nothing. The verdict would be right and the sentence under
    it would be a fabrication about the hardware.
    """
    turns = [[]] if reply is None else [[reply]]
    link = SequencedLink({Z21_REFERENCE: turns, SERVICE_RESULT: []})
    result = check_z21_opcodes(link)
    assert result.value is None
    assert fragment in result.detail


def test_a_reply_for_a_lower_cv_than_requested_is_not_an_answer():
    """The reply must decode back to the CV that was asked for - not merely to
    something no larger than it.

    Band 0x15 starts at CV256, so a station answering in the wrong band echoes a
    CV well below the request. Comparing with `>` instead of `!=` accepts every
    such reply, publishing one CV's value under another CV's name.
    """
    link = SequencedLink(
        {
            commands.service_ext_read(REFERENCE_CV): [[CV8_IS_145]],
            commands.service_ext_read(500): [[b"\x63\x15\x00\x2a"]],  # decodes to CV256
            SERVICE_RESULT: [],
        }
    )
    result = check_service_ext_cv(link, high_cv=500)
    assert result.value["service_ext_cv"] is True
    assert result.value["service_ext_cv_high_band"] is None
    assert "CV256" in result.detail


def test_a_pom_echo_above_the_interned_integer_range_is_still_matched():
    """The echo comparison is a value test and must stay one.

    CPython caches small integers, so `==` and `is` behave identically for every
    CV the probe happens to use in its own tests - CV8, CV29. Above 256 the
    cache stops, `is` starts returning False for equal numbers, and a perfectly
    good read of a high CV is discarded as "not an answer to this request".
    The ZIMO CVs railctl needs to back up live at 265 and above.
    """
    high_cv = 300
    wire = commands.cv_wire(high_cv)
    echo = bytes([0x64, 0x14, (wire >> 8) & 0xFF, wire & 0xFF, 0x2A])
    link = FakeLink({commands.pom_read(ADDRESS, high_cv): [build(echo)]})
    result = check_pom_read(link, ADDRESS, high_cv, poll=False)
    assert result.value["pom_read"] is True
    assert result.value["pom_echo_zero_based"] is True
    assert result.value["pom_value"] == 0x2A


def test_the_pom_check_reads_cv8_when_no_cv_is_named():
    """CV8 is the default because its value is knowable independently: 145 on
    every ZIMO decoder. A different default would make a plausible-but-wrong
    reading undetectable."""
    link = FakeLink({})
    check_pom_read(link, ADDRESS, poll=False)
    assert link.sent == [build(commands.pom_read(ADDRESS, 8))]


# ---------------------------------------------------------------------------
# Commands that must not change the layout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("f0_is_on", "action"), [(True, 1), (False, 0)])
def test_the_function_check_re_asserts_f0_and_never_toggles_it(f0_is_on: bool, action: int):
    """R5 is only side-effect free because it commands F0 to the value it already
    holds. Action 2 is "toggle", and a probe that toggled would switch off the
    headlight of whatever locomotive it was pointed at - while reporting that
    single-function commands work.
    """
    link = FakeLink({})
    check_single_function(link, address=ADDRESS, f0_is_on=f0_is_on)
    assert link.sent == [build(commands.single_function(ADDRESS, 0, action))]
    assert link.sent[0][-2] & 0xC0 != 0x80  # never the toggle bit pattern


# ---------------------------------------------------------------------------
# Identity and status reporting
# ---------------------------------------------------------------------------


def identity_link(status_raw: int) -> FakeLink:
    return FakeLink(
        {
            commands.version(): [build(b"\x63\x21\x36\x12")],
            commands.status(): [build(bytes([0x62, 0x22, status_raw]))],
        }
    )


@pytest.mark.parametrize(
    ("raw", "flag"),
    [(0x01, "emergency_off"), (0x02, "emergency_stop"), (0x08, "service_mode")],
)
def test_each_status_flag_reaches_the_report(raw: int, flag: str):
    """The flags are published capabilities, so they have to be read from the
    status reply rather than defaulted away when one is present."""
    value = check_identity(identity_link(raw)).value
    assert value[flag] is True
    for other in ("emergency_off", "emergency_stop", "service_mode"):
        if other != flag:
            assert value[other] is False


def test_the_automatic_start_warning_appears_only_when_that_bit_is_set():
    """Automatic start mode means every locomotive resumes its last speed the
    moment track power returns. Warning about it when the bit is clear teaches
    the operator to ignore the warning."""
    warning = "start mode is AUTOMATIC"
    assert warning in check_identity(identity_link(0x04)).detail
    assert warning not in check_identity(identity_link(0x00)).detail


def test_a_broadcast_frame_is_marked_as_one_in_the_audit_trail():
    """FE and FD distinguish a reply to our command from an unsolicited
    broadcast, which is the difference between evidence about the command that
    was sent and evidence about something else entirely."""
    link = FakeLink({commands.single_function(ADDRESS, 0, 0): [b"\xff\xfd\x61\x01\x60"]})
    result = check_single_function(link, address=ADDRESS, f0_is_on=False)
    assert result.frames == ["FD 61 01"]


@pytest.mark.parametrize(
    ("ident", "fragment"),
    [(0x04, "128 speed steps"), (0x03, "reserved bit pattern")],
)
def test_the_speed_step_mode_is_reported_in_words(ident: int, fragment: str):
    link = FakeLink({commands.loco_info(ADDRESS): [build(bytes([0xE4, ident, 0, 0x10, 0]))]})
    result, _, _ = check_loco_info(link, ADDRESS)
    assert fragment in result.detail


@pytest.mark.parametrize(("fa", "word"), [(0x10, "F0 on"), (0x00, "F0 off")])
def test_the_headlight_state_is_reported_as_it_was_read(fa: int, word: str):
    link = FakeLink({commands.loco_info(ADDRESS): [build(bytes([0xE4, 0x04, 0, fa, 0]))]})
    result, _, _ = check_loco_info(link, ADDRESS)
    assert word in result.detail


# ---------------------------------------------------------------------------
# The address divergence band
# ---------------------------------------------------------------------------

LOCO_INFO_REPLY = build(b"\xe4\x04\x00\x10\x00")


def band_link(*, short_answers: bool, long_answers: bool, address: int) -> FakeLink:
    short_high, short_low = commands.loco_address_bytes(address, threshold=128)
    long_high, long_low = commands.loco_address_bytes(address, threshold=100)
    script = {}
    if short_answers:
        script[bytes([0xE3, 0x00, short_high, short_low])] = [LOCO_INFO_REPLY]
    if long_answers:
        script[bytes([0xE3, 0x00, long_high, long_low])] = [LOCO_INFO_REPLY]
    return FakeLink(script)


@pytest.mark.parametrize("address", [100, 127])
def test_the_divergence_band_includes_both_of_its_endpoints(address: int):
    link = band_link(short_answers=False, long_answers=True, address=address)
    assert check_address_band(link, address).value == 100


@pytest.mark.parametrize("address", [99, 128])
def test_an_address_outside_the_divergence_band_is_declined(address: int):
    result = check_address_band(FakeLink({}), address)
    assert result.value is None
    assert "outside" in result.detail


def test_only_the_long_form_answering_puts_the_threshold_at_100():
    link = band_link(short_answers=False, long_answers=True, address=110)
    result = check_address_band(link, 110)
    assert result.value == 100
    assert "long" in result.detail


def test_only_the_short_form_answering_puts_the_threshold_at_128():
    """The mirror case. Testing one direction alone lets a comparison collapse
    into one that agrees with `==` on exactly the outcome that was tested."""
    link = band_link(short_answers=True, long_answers=False, address=110)
    result = check_address_band(link, 110)
    assert result.value == 128
    assert "short" in result.detail


def test_the_band_check_asks_the_same_address_in_two_genuinely_different_forms():
    """The whole check rests on the two telegrams differing. If both thresholds
    produced the same bytes the station would be asked the same question twice
    and the answers could never diverge."""
    link = band_link(short_answers=False, long_answers=False, address=110)
    check_address_band(link, 110)
    assert len(link.sent) == 2
    assert link.sent[0] != link.sent[1]
    # sent frames are FF FE | E3 00 | high low | xor, so the address is at 4:6
    assert link.sent[0][4:6] == b"\x00\x6e"  # short: plain 110
    assert link.sent[1][4:6] == b"\xc0\x6e"  # long: 110 with the long-address marker


@pytest.mark.parametrize(
    ("short_answers", "long_answers"),
    [(True, True), (False, False)],
)
def test_both_forms_agreeing_establishes_nothing(short_answers: bool, long_answers: bool):
    link = band_link(short_answers=short_answers, long_answers=long_answers, address=110)
    result = check_address_band(link, 110)
    assert result.value is None
    assert "threshold not established" in result.detail


def test_the_hexdump_helper_is_reachable_for_the_entry_point():
    """`__main__` calls `checks._hexdump` directly when it has to build a skipped
    result, so the name is part of that module's contract with this one."""
    assert checks._hexdump([]) == []
