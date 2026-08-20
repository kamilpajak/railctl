# src/railctl/xbus/dialect.py
"""XpressNet and Z21: the addressing, CV and status conventions this tool speaks.

The YD7010 reports command station id 0x12 - the Z21 family - and answers the
Z21 opcodes. It also answers the Lenz ones: `22 15`, `22 18` and `22 19` were all
verified three rounds each against known constants (docs/probe-results.md, R2/R4
"Settled"). `service_cv_preference` is therefore a DEFAULT ORDER, not a list of
what the station can do; an earlier document declared those opcodes absent
because the probe never sent the `21 10` result request, and that mistake must
not be re-frozen here as a design constant. The split is kept anyway,
because the two dialects disagree about locomotive addresses 100..127:
XpressNet sends them as long DCC addresses, Z21 sends them short. That band is
documented, not measured on this hardware, so it is carried as data and pinned
by tests rather than assumed away.

`StatusBitOrder` carries the same argument about a different field. Two manuals
disagree about bits 0 and 1 of the `62 22` status byte, and the disagreement is
not academic - reading them the wrong way round makes a dead track report as
powered. So the two documented orders are named data here, one of them is the
DEFAULT, and `Capabilities.status_bit_order` overrides it once `railctl doctor`
D13 has measured which one the attached station uses. Exactly the shape
`long_address_threshold` already has: one value that changes, never a code path.

`CvEncoding` lives here rather than in `cv.py` because `Dialect` needs it at
class-definition time, while `cv.py` only needs it inside function bodies.
`cv.py` re-exports it, so `from railctl.xbus.cv import CvEncoding` is the same
object.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Literal


class CvEncoding(enum.Enum):
    """How a CV number is put on the wire. See `railctl.xbus.cv` for the rules."""

    POM_ZERO_BASED = "pom"  # E6 30 ... (0xE4|MM) LSB          wire = cv - 1
    SERVICE_DIRECT = "direct"  # 22 15 C / 23 16 C V           wire = cv, 1..255
    SERVICE_EXT = "ext"  # 22 18..1B / 23 1C..1F               page + (cv & 0xFF)
    Z21_16BIT = "z21"  # 23 11 MSB LSB / 24 12 MSB LSB V       wire = cv - 1


@dataclass(frozen=True, slots=True)
class Dialect:
    """A data object, not a hierarchy.

    `long_address_threshold` is the *default*. Once `doctor` measures the
    station's real threshold, `Capabilities.loco_address_threshold` overrides it,
    and it is one integer that changes - not a code path.

    `service_cv_preference` is the ordered list the station walks when choosing a
    service-mode encoding; the station filters it by what capabilities say works.
    """

    name: str
    long_address_threshold: int
    service_cv_preference: tuple[CvEncoding, ...]


XPRESSNET = Dialect(
    "xpressnet",
    100,
    (CvEncoding.SERVICE_DIRECT, CvEncoding.Z21_16BIT, CvEncoding.SERVICE_EXT),
)
# Measured on the YD7010 (docs/probe-results.md, R2/R4): 22 15, 22 18 and 22 19
# all answer. This tuple is the default preference order, not a statement that
# the other encodings are unavailable; `Capabilities` re-adds them once `doctor`
# measures them.
Z21 = Dialect("z21", 128, (CvEncoding.Z21_16BIT,))

DIALECTS: tuple[Dialect, ...] = (XPRESSNET, Z21)

# Addresses where the two dialects put different bytes on the wire: XpressNet
# marks them long, Z21 leaves them short. A decoder configured short in this
# range (CV1 = 100..127 with CV29 bit 5 clear) simply ignores the long form,
# with no error of any kind - which is why the station warns once, naming CV1
# and CV29 bit 5, instead of reporting a failure that never arrives.
DIVERGENCE_BAND = range(XPRESSNET.long_address_threshold, Z21.long_address_threshold)


#: The name a `StatusBitOrder` is stored under in `capabilities.json`, and the
#: only two values `Capabilities.status_bit_order` accepts. Declared here rather
#: than in `station/capabilities.py` so the type and the instances below cannot
#: drift apart; `tests/unit/test_dialect.py` pins them against each other.
StatusBitOrderName = Literal["lenz_spec", "lenz_23151"]


@dataclass(frozen=True, slots=True)
class StatusBitOrder:
    """Which of bits 0 and 1 of the `62 22` status byte means what.

    Data, not a code path - see the module docstring. The two masks are always
    `0x01` and `0x02` in one arrangement or the other; nothing else about the
    status byte is in dispute, and bits 2, 3, 6 and 7 stay module constants in
    `xbus/replies.py` where they have never needed a second reading.
    """

    name: StatusBitOrderName
    emergency_stop_mask: int
    emergency_off_mask: int


#: Lenz XpressNet 2.1.7: bit 0 is emergency OFF, bit 1 is emergency STOP. This is
#: what the specification says and what JMRI implements - its
#: `java/src/jmri/jmrix/lenz/XNetPowerManager.java` reads `statusByte & 0x01` as
#: "Command station is in Emergency Off Mode" and `& 0x02` as "Emergency Stop
#: Mode", with no per-station override anywhere. No station here has been
#: measured to use it; it is carried because a second implementation reading the
#: same byte the other way is not hypothetical.
LENZ_SPEC: Final[StatusBitOrder] = StatusBitOrder(
    "lenz_spec", emergency_stop_mask=0x02, emergency_off_mask=0x01
)
#: The German Lenz 23151 interface manual: bit 0 is emergency STOP, bit 1 is
#: emergency OFF - the reverse of the order above. MEASURED on the YD7010,
#: 2026-08-05, against the front-panel Track Out LED (docs/probe-results.md,
#: "Status byte: bits 0 and 1 are the reverse of the Lenz spec").
LENZ_23151: Final[StatusBitOrder] = StatusBitOrder(
    "lenz_23151", emergency_stop_mask=0x01, emergency_off_mask=0x02
)

STATUS_BIT_ORDERS: Final[tuple[StatusBitOrder, ...]] = (LENZ_SPEC, LENZ_23151)

#: Both disputed bits, whichever order is in force. A status byte with neither of
#: them set reads the same under both orders, which is what makes it the only
#: safe starting state for the D13 measurement.
STATUS_DISPUTED_BITS: Final[int] = LENZ_SPEC.emergency_stop_mask | LENZ_SPEC.emergency_off_mask

#: The order applied when nothing has measured this station. It is the order the
#: only station this project has ever run against was measured to use - a DEFAULT
#: taken from named data, not a claim that every XpressNet station orders these
#: bits this way. `Capabilities.status_bit_order` overrides it the moment
#: `railctl doctor` D13 establishes one, and until then no reading changes: this
#: is the same value the constants in `xbus/replies.py` used to hardcode.
DEFAULT_STATUS_BIT_ORDER: Final[StatusBitOrder] = LENZ_23151


def status_bit_order_by_name(name: str) -> StatusBitOrder:
    """The order `name` names, for deserialising `Capabilities.status_bit_order`.

    Raises rather than falling back to the default: a capabilities file naming an
    order this build does not know is a file whose station was measured as
    something this build cannot reproduce, and quietly reading it as the default
    would publish that guess as the measurement.
    """
    for order in STATUS_BIT_ORDERS:
        if order.name == name:
            return order
    known = ", ".join(order.name for order in STATUS_BIT_ORDERS)
    raise ValueError(f"unknown status bit order {name!r}; known orders are {known}")
