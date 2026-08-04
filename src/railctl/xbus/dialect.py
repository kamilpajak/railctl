# src/railctl/xbus/dialect.py
"""XpressNet and Z21: the two addressing and CV conventions this tool speaks.

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

`CvEncoding` lives here rather than in `cv.py` because `Dialect` needs it at
class-definition time, while `cv.py` only needs it inside function bodies.
`cv.py` re-exports it, so `from railctl.xbus.cv import CvEncoding` is the same
object.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


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
