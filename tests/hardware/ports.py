# tests/hardware/ports.py
"""What the station's port names are SHAPED like, per platform.

Separate from the discovery table in railctl/transport/__init__.py on purpose:
that table says WHICH node is XpressNet, this says what that node looks like,
and they are two different claims. A pattern typo that still matched one node
would satisfy the first and fail the second, which is the whole value of the
acceptance assertion.

It lives in a plain module rather than inside the acceptance file because the
arithmetic is pure string work that needs no station, and
tests/unit/test_hardware_port_shapes.py measures it on every CI run - on both
platforms, from whichever one happens to be running. tests/hardware/ is
deselected by default, so anything kept only in there is exercised on exactly
one bench at a time.

Measured 2026-08-04 (macOS) and 2026-10-14 (Linux, /dev/serial/by-id); see
docs/probe-results.md.
"""

from __future__ import annotations

import sys
from typing import Final

#: platform -> (prefix, XpressNet suffix, YD.Control telemetry suffix)
PORT_SHAPES: Final[dict[str, tuple[str, str, str]]] = {
    "darwin": ("/dev/cu.usbmodem", "3", "5"),
    "linux": ("/dev/serial/by-id/usb-YaMoRC_YD700x", "-if02", "-if04"),
}


def port_shape(platform: str | None = None) -> tuple[str, str, str]:
    """This platform's shape, or a KeyError naming it.

    sys.platform is read here and not captured as a default argument value, for
    the same reason transport.discovery_for() gives: a default is evaluated once,
    at import, and freezes whichever machine imported the module.
    """
    name = sys.platform if platform is None else platform
    try:
        return PORT_SHAPES[name]
    except KeyError:
        raise KeyError(f"no acceptance port shape has been measured on {name}") from None


def telemetry_sibling(port: str, platform: str | None = None) -> str:
    """The YD.Control node beside `port`, in either platform's naming.

    `port[:-1] + "5"` was the whole of this, and it is why this function exists:
    on a Linux by-id path that produces "...-if05", which no station has ever
    had, and the acceptance test's os.path.exists guard then SKIPPED. A silent
    skip of the counters acceptance is the broken-instrument failure this project
    is organised against - the telemetry interface was present the whole time, as
    -if04.

    A port that does not fit the table raises rather than returning a guess, so
    a stale table cannot turn into a skip either.

    BOTH ends are checked. Checking only the suffix was close to vacuous on
    macOS, where the suffix is the single character "3": every path ending in a
    3 satisfied it, including a *1 LocoNet node from a station whose serial ends
    in 3.

    The arithmetic is a bounded slice, not str.replace, and that is the case the
    unit test pins: replace() rewrites the suffix wherever it occurs, so a
    darwin serial containing the suffix character ("...0003194 3") or a by-id
    path where "-if02" appears twice both come out wrong, silently.
    """
    prefix, xpressnet, telemetry = port_shape(platform)
    if not (port.startswith(prefix) and port.endswith(xpressnet)):
        raise AssertionError(
            f"{port} is not {prefix!r}...{xpressnet!r}; the port shape table is stale"
        )
    return port[: -len(xpressnet)] + telemetry
