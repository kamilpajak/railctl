"""Pinning tests written to kill surviving mutants in `frames.py`.

Provenance: cosmic-ray run of 2026-08-04, 294 mutants, 241 killed by the prior
suite. Each test below closes a specific gap that run exposed. The full triage,
including the survivors judged equivalent and why, is in docs/test-hardening.md.

These are deliberately example-based rather than generated. A property test
explores a distribution; a pinning test has to fail on one exact edit, every
run, or it is not pinning anything.
"""

from __future__ import annotations

import dataclasses

import pytest

from tools.probe.frames import LI_BROADCAST, LI_COMMAND, Frame, build, split_frames

# Non-0xFF filler. 0xFF is excluded so the noise cannot start a prefix itself,
# which would change what these buffers are testing.
NOISE = b"[CS0] M: TC 0mA"


def test_a_frame_is_immutable_once_parsed():
    """`Frame` is frozen on purpose.

    Frames are handed to every check and hex-dumped into the report as the audit
    trail for a verdict. A frame that could be edited after parsing would let a
    later stage rewrite the evidence for an earlier one.
    """
    frame = Frame(LI_COMMAND, b"\x61\x01")
    assert dataclasses.fields(frame) is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.telegram = b"\x61\x82"  # type: ignore[misc]


def test_solicited_means_equal_to_the_command_prefix_not_at_least_it():
    """`solicited` is an equality test, and ordering must not stand in for it.

    Only two prefixes ever come off this wire, and they happen to be ordered so
    that `>=` gives the same answer as `==` for both - which is why replacing
    one with the other survived the whole suite. The distinction is still the
    one the field means, so it is pinned here rather than left to luck.
    """
    assert Frame(LI_COMMAND, b"\x61\x01").solicited is True
    assert Frame(LI_BROADCAST, b"\x61\x01").solicited is False
    assert Frame(b"\xff\xff", b"\x61\x01").solicited is False


def test_a_stray_prefix_late_in_the_buffer_does_not_hide_the_frame_after_it():
    """Salvage must resume just past the stray prefix, not at a scaled offset.

    The mutant that motivated this test replaced `_salvage(buffer, pos + 1)`
    with `pos << 1`. With the stray prefix at the very start of the buffer that
    is nearly harmless, because doubling a small offset lands close by - so the
    existing tests, which all put their noise at the front, never noticed. Move
    the stray prefix far enough in and the doubled offset jumps clean past the
    real frame, and `split_frames` returns NOTHING.

    Silence is the one outcome this project cannot afford to get wrong: it is
    how the probe records a capability the hardware does not have.
    """
    payload = b"\x63\x14\x08\x91"  # CV8 reads back 145, the ZIMO manufacturer id
    buffer = NOISE + LI_COMMAND + build(payload)
    frames, rest = split_frames(buffer)
    assert [f.telegram for f in frames] == [payload]
    assert rest == b""


def test_salvage_continues_scanning_after_it_rescues_a_frame():
    """One rescued frame must not end the scan.

    A single read regularly carries several telegrams - a probe run collects the
    solicited reply and any broadcast that arrived in the same window. Stopping
    after the first rescued frame silently drops the rest of that window.
    """
    first = b"\x63\x14\x08\x91"
    second = b"\x61\x01"
    frames, rest = split_frames(LI_COMMAND + build(first) + build(second))
    assert [f.telegram for f in frames] == [first, second]
    assert rest == b""


def test_salvage_after_a_late_stray_prefix_still_returns_every_frame():
    """The two cases above, together: a far-in stray prefix and several frames."""
    payloads = [b"\x63\x14\x08\x91", b"\x61\x01", b"\x62\x22\x00"]
    buffer = NOISE + LI_COMMAND + b"".join(build(p) for p in payloads)
    frames, rest = split_frames(buffer)
    assert [f.telegram for f in frames] == payloads
    assert rest == b""
