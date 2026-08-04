"""Self-consistency of the golden vector table, and both directions across it.

Two tests run over EVERY row before any of it is used as an expectation:
xor(b[:-1]) == b[-1] and len(b) == (b[0] & 0x0F) + 2. A mistyped byte in the
table is then a failure of the table, not a silently wrong expectation that a
later encoder change gets blamed for.
"""

from __future__ import annotations

import pytest

from railctl.xbus.codec import xor
from tests.vectors import ALL_VECTORS, DECODE_VECTORS, ENCODE_VECTORS


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_carries_a_correct_xor(vector):
    assert xor(vector.telegram[:-1]) == vector.telegram[-1]


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_has_the_length_its_header_declares(vector):
    assert len(vector.telegram) == (vector.telegram[0] & 0x0F) + 2


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_says_why_it_exists(vector):
    """A row nobody can justify is a row nobody will dare to change."""
    assert vector.why.strip()


def test_the_table_is_not_silently_empty():
    assert len(ENCODE_VECTORS) >= 14
    assert len(DECODE_VECTORS) >= 5


from railctl.xbus.commands import cmd_drive_128  # noqa: E402
from railctl.xbus.replies import parse  # noqa: E402
from railctl.xbus.speed import Direction  # noqa: E402
from tests.vectors import XPRESSNET_THRESHOLD, Z21_THRESHOLD  # noqa: E402


@pytest.mark.parametrize("vector", ENCODE_VECTORS, ids=lambda v: v.name)
def test_each_encoder_produces_the_bytes_in_the_table(vector):
    assert vector.call() == vector.telegram


@pytest.mark.parametrize("address", [100, 110, 127])
def test_the_two_dialects_disagree_inside_the_divergence_band(address: int):
    """On XpressNet, addresses 100..127 go out as LONG DCC addresses. A decoder
    configured short in that range (CV1 = 100..127, CV29 bit 5 = 0) simply does
    nothing, with no error."""
    xn = cmd_drive_128(address, 1, Direction.FORWARD, threshold=XPRESSNET_THRESHOLD)
    z21 = cmd_drive_128(address, 1, Direction.FORWARD, threshold=Z21_THRESHOLD)
    assert xn != z21


@pytest.mark.parametrize("address", [1, 99, 128, 1234, 9999])
def test_the_two_dialects_agree_outside_the_divergence_band(address: int):
    xn = cmd_drive_128(address, 1, Direction.FORWARD, threshold=XPRESSNET_THRESHOLD)
    z21 = cmd_drive_128(address, 1, Direction.FORWARD, threshold=Z21_THRESHOLD)
    assert xn == z21


@pytest.mark.parametrize("vector", DECODE_VECTORS, ids=lambda v: v.name)
def test_each_decode_row_parses_to_the_whole_object_in_the_table(vector):
    """One comparison per row, over the ENTIRE dataclass.

    This is the clause of design line 1579 that says a decode vector "compares
    equal as a dataclass". Asserting a chosen list of fields instead - say
    (reply.raw_cv, reply.value, reply.ident, reply.z21_form) == (8, 8, 0x14,
    False) - still passes when the parser grows a field nobody looked at, stops
    setting a field it used to set, or renames one. A frozen dataclass compares
    on its whole field tuple, so `==` catches all three.

    The type assertion is not there to catch more: dataclass __eq__ already
    returns NotImplemented across classes, so a PagedCvValue would never compare
    equal to a CvValue with the same numbers. It is there so the failure reads
    "PagedCvValue is not CvValue" instead of a field diff between two objects
    that are not even the same kind of reply.
    """
    reply = parse(vector.telegram)
    assert type(reply) is type(vector.expected)
    assert reply == vector.expected


@pytest.mark.parametrize("vector", DECODE_VECTORS, ids=lambda v: v.name)
def test_no_decode_row_raises(vector):
    """Stated separately from the comparison above, which would also go red on an
    exception. `parse` is TOTAL - the row that must not raise is the unknown one,
    71 AA DB, and this is where the table says so in one line."""
    parse(vector.telegram)
