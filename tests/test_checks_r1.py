from tools.probe.checks import check_pom_read
from tools.probe.fake import FakeLink
from tools.probe.frames import build

POM_CV8_AT_3 = b"\xe6\x30\x00\x03\xe4\x07\x00"
POLL = b"\x21\x10"


def test_result_arriving_as_a_broadcast_sets_channel_broadcast():
    link = FakeLink(
        {POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"]},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is True
    assert result.value["pom_result_channel"] == "broadcast"
    assert result.value["value"] == 0x91


def test_echo_of_the_zero_based_cv_sets_the_echo_flag():
    link = FakeLink(
        {POM_CV8_AT_3: []},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_echo_zero_based"] is True


def test_echo_of_the_one_based_cv_clears_the_echo_flag():
    link = FakeLink(
        {POM_CV8_AT_3: []},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x08\x91\xee"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_echo_zero_based"] is False


def test_result_arriving_only_after_a_poll_sets_channel_poll():
    link = FakeLink(
        {
            POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"],
            POLL: [b"\xff\xfe\x63\x14\x07\x91\xe1"],
        }
    )
    result = check_pom_read(link, address=3, cv=8, poll=True)
    assert result.value["pom_read"] is True
    assert result.value["pom_result_channel"] == "poll"


def test_no_ack_leaves_pom_read_unknown_and_names_railcom():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x13\x72"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is None
    assert "RailCom" in result.detail


def test_unsupported_reply_sets_pom_read_false():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is False


def test_total_silence_leaves_pom_read_unknown_and_says_what_to_check():
    # Silence is not the specified signature of non-support: XpressNet 2.2.23
    # says an unsupported station answers 61 82, and Lenz 23151 section 1.4 says
    # that reply is always coupled to the command that caused it. Silence is
    # equally produced by an unparsed result form, a missing RailCom receiver,
    # or no locomotive on the track, so it must not become a definite "no".
    link = FakeLink({})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is None
    assert result.value["pom_result_channel"] == "none"
    assert "nothing established" in result.detail
    assert "RailCom" in result.detail


def test_a_z21_form_cv_result_is_recognised_and_not_mistaken_for_silence():
    # Z21 LAN Protocol 6.5: LAN_X_CV_RESULT is 64 14 with a 16-bit CV address.
    # The YD7010 reports command station id 0x12, the Z21 family, so this form
    # is plausible on its XpressNet port. Unparsed, it looked like silence and
    # a successful read was recorded as "POM read not supported".
    link = FakeLink({POM_CV8_AT_3: [build(b"\x64\x14\x00\x07\x91")]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is True
    assert result.value["value"] == 0x91
    assert result.value["pom_echo_zero_based"] is True


def test_the_probe_never_sends_a_write_opcode():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"]})
    check_pom_read(link, address=3, cv=8, poll=True)
    for frame in link.sent:
        assert frame[2] != 0x23, "0x23 is a write opcode; the probe must never write"
        if frame[2] == 0xE6:
            assert frame[6] & 0xFC == 0xE4, "POM option byte must be the read form 0xE4|MM"


def test_short_circuit_reply_leaves_pom_read_unknown():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x12\x73"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is None
    assert "short_circuit" in result.detail


def test_busy_reply_leaves_pom_read_unknown():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x1f\x7e"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is None
    assert "busy" in result.detail


def test_unrecognised_echo_leaves_echo_flag_unknown():
    link = FakeLink(
        {POM_CV8_AT_3: []},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x09\x91\xef"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is True
    assert result.value["pom_echo_zero_based"] is None


def test_real_cvvalue_reply_wins_over_no_ack_marker():
    link = FakeLink(
        {POM_CV8_AT_3: [b"\xff\xfe\x61\x13\x72"]},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is True


def test_with_broadcast_result_poll_command_is_never_sent():
    link = FakeLink(
        {POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"]},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    check_pom_read(link, address=3, cv=8, poll=True)
    assert len(link.sent) == 1
