import pytest

from tools.probe.fake import FakeLink
from tools.probe.frames import LI_BROADCAST, LI_COMMAND


def test_fake_link_returns_the_scripted_reply():
    link = FakeLink({b"\x21\x21": [b"\xff\xfe\x63\x21\x40\x12\x10"]})
    frames = link.exchange(b"\x21\x21", window=0.1)
    assert [f.telegram for f in frames] == [b"\x63\x21\x40\x12"]
    assert frames[0].prefix == LI_COMMAND


def test_fake_link_records_what_was_sent_including_the_checksum():
    link = FakeLink({b"\x21\x21": []})
    link.exchange(b"\x21\x21", window=0.1)
    assert link.sent == [b"\xff\xfe\x21\x21\x00"]


def test_fake_link_returns_nothing_for_an_unscripted_payload():
    link = FakeLink({})
    assert link.exchange(b"\x21\x24", window=0.1) == []


def test_fake_link_can_deliver_an_unsolicited_broadcast_after_the_reply():
    link = FakeLink(
        {b"\x21\x10": [b"\xff\xfe\x01\x04\x05"]},
        unsolicited={b"\x21\x10": [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    frames = link.exchange(b"\x21\x10", window=0.1)
    assert [f.solicited for f in frames] == [True, False]
    assert frames[1].prefix == LI_BROADCAST


def test_fake_link_collect_drains_queued_unsolicited_frames():
    link = FakeLink({}, unsolicited={b"": [b"\xff\xfd\x61\x00\x61"]})
    assert [f.telegram for f in link.collect(window=0.1)] == [b"\x61\x00"]


def test_fake_link_rejects_a_second_send_before_the_first_is_collected():
    link = FakeLink({b"\x21\x21": []}, strict_request_response=True)
    link.begin(b"\x21\x21")
    with pytest.raises(RuntimeError, match="outstanding"):
        link.begin(b"\x21\x24")
