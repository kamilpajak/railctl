# tests/unit/test_dialect.py
"""The XpressNet / Z21 split.

The YD7010 reports command station id 0x12 - the Z21 family - and its Z21
opcodes answer (docs/probe-results.md). The split still has to exist, because
the two dialects disagree about locomotive addresses 100..127, and that
disagreement is documented rather than measured on this hardware. A dialect is
data, not a class hierarchy: two integers and an ordered preference list.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.xbus.dialect import DIALECTS, DIVERGENCE_BAND, XPRESSNET, Z21, CvEncoding, Dialect


def test_xpressnet_switches_to_long_addresses_at_100():
    assert XPRESSNET.name == "xpressnet"
    assert XPRESSNET.long_address_threshold == 100


def test_z21_switches_to_long_addresses_at_128():
    assert Z21.name == "z21"
    assert Z21.long_address_threshold == 128


def test_the_divergence_band_is_exactly_100_to_127():
    assert list(DIVERGENCE_BAND) == list(range(100, 128))
    assert 99 not in DIVERGENCE_BAND
    assert 100 in DIVERGENCE_BAND
    assert 127 in DIVERGENCE_BAND
    assert 128 not in DIVERGENCE_BAND


def test_xpressnet_prefers_direct_then_z21_then_extended():
    assert XPRESSNET.service_cv_preference == (
        CvEncoding.SERVICE_DIRECT,
        CvEncoding.Z21_16BIT,
        CvEncoding.SERVICE_EXT,
    )


def test_z21_defaults_to_the_sixteen_bit_encoding_only():
    """Measured on the YD7010 (docs/probe-results.md, R2/R4): 22 15, 22 18 and
    22 19 all answer. This tuple is the default preference order, not a statement
    that the other encodings are unavailable; `Capabilities` re-adds them once
    `doctor` measures them.
    """
    assert Z21.service_cv_preference == (CvEncoding.Z21_16BIT,)


def test_a_dialect_cannot_be_edited_after_construction():
    with pytest.raises(dataclasses.FrozenInstanceError):
        XPRESSNET.long_address_threshold = 128  # type: ignore[misc]


def test_a_dialect_carries_no_instance_dict():
    assert not hasattr(XPRESSNET, "__dict__")


def test_dialects_lists_both_and_nothing_else():
    assert DIALECTS == (XPRESSNET, Z21)
    assert all(isinstance(d, Dialect) for d in DIALECTS)
