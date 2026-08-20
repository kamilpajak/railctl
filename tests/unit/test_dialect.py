# tests/unit/test_dialect.py
"""The XpressNet / Z21 split.

The YD7010 reports command station id 0x12 - the Z21 family - and its Z21
opcodes answer (docs/probe-results.md). The split still has to exist, because
the two dialects disagree about locomotive addresses 100..127, and that
disagreement is documented rather than measured on this hardware. A dialect is
data, not a class hierarchy: two integers and an ordered preference list.

The status byte's disputed bits are carried the same way, for the same reason
one layer down: two manuals disagree about bits 0 and 1, one order was measured
on this station, and the measurement is a default that a capability overrides -
not a claim about XpressNet.
"""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from railctl.xbus.dialect import (
    DEFAULT_STATUS_BIT_ORDER,
    DIALECTS,
    DIVERGENCE_BAND,
    LENZ_23151,
    LENZ_SPEC,
    STATUS_BIT_ORDERS,
    STATUS_DISPUTED_BITS,
    XPRESSNET,
    Z21,
    CvEncoding,
    Dialect,
    StatusBitOrder,
    StatusBitOrderName,
    status_bit_order_by_name,
)


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


# -- the status byte's disputed bits ------------------------------------------


def test_the_lenz_spec_order_puts_emergency_off_on_bit_0():
    """XpressNet 2.1.7, and what JMRI's XNetPowerManager implements with no
    per-station override: `0x01` is emergency off, `0x02` is emergency stop."""
    assert LENZ_SPEC.name == "lenz_spec"
    assert LENZ_SPEC.emergency_off_mask == 0x01
    assert LENZ_SPEC.emergency_stop_mask == 0x02


def test_the_23151_order_puts_emergency_stop_on_bit_0():
    """The German 23151 interface manual, and what the YD7010 was MEASURED to do
    on 2026-08-05 (docs/probe-results.md): `0x01` is emergency stop, `0x02` is
    emergency off."""
    assert LENZ_23151.name == "lenz_23151"
    assert LENZ_23151.emergency_stop_mask == 0x01
    assert LENZ_23151.emergency_off_mask == 0x02


def test_the_default_order_is_the_one_measured_on_this_hardware():
    """A regression pin, so changing the default is a deliberate act. The default
    is the only station this project has ever run against; it is not a claim about
    XpressNet, and `Capabilities.status_bit_order` overrides it once D13 measures
    one."""
    assert DEFAULT_STATUS_BIT_ORDER is LENZ_23151


def test_the_two_orders_are_the_same_two_bits_swapped():
    assert LENZ_SPEC.emergency_off_mask == LENZ_23151.emergency_stop_mask
    assert LENZ_SPEC.emergency_stop_mask == LENZ_23151.emergency_off_mask
    assert STATUS_DISPUTED_BITS == 0x03


def test_status_bit_orders_lists_both_and_nothing_else():
    assert STATUS_BIT_ORDERS == (LENZ_SPEC, LENZ_23151)
    assert all(isinstance(order, StatusBitOrder) for order in STATUS_BIT_ORDERS)


def test_every_order_name_is_one_of_the_declared_literals():
    """The `Literal` in the type and the instances are two copies of one fact -
    `Capabilities.status_bit_order` is typed off the first and deserialised into
    the second, so a name added to one and not the other loads as a value no
    lookup can resolve."""
    assert {order.name for order in STATUS_BIT_ORDERS} == set(get_args(StatusBitOrderName))


def test_an_order_is_looked_up_by_the_name_capabilities_stores():
    assert status_bit_order_by_name("lenz_spec") is LENZ_SPEC
    assert status_bit_order_by_name("lenz_23151") is LENZ_23151


def test_an_unknown_order_name_raises_rather_than_falling_back_to_the_default():
    """Silently defaulting would turn a capabilities file naming an order this
    build does not know into a station measured as something else."""
    with pytest.raises(ValueError, match="lenz_9999"):
        status_bit_order_by_name("lenz_9999")


def test_a_status_bit_order_cannot_be_edited_after_construction():
    with pytest.raises(dataclasses.FrozenInstanceError):
        LENZ_23151.emergency_stop_mask = 0x02  # type: ignore[misc]
