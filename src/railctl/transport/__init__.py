"""Byte pipes. A transport moves bytes and knows nothing about framing.

read() blocks up to `timeout`, returns as soon as at least one byte is
available, returns b"" on timeout, and never raises on timeout - framing is not
its problem. write() writes everything or raises TransportError.

diagnostic_hint is here rather than in link.py on purpose. When a handshake or an
exchange fails, the useful advice is about the CONNECTION, and only the transport
knows what kind of connection it is - which CDC function of a composite device the
serial port is, a network address for the future Z21 UDP transport. Spec line 583 requires the LAN
transport to land with no edit to link.py, so link.py must not hold either
sentence.
"""

from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, max_bytes: int, timeout: float) -> bytes: ...
    def flush_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def description(self) -> str: ...
    @property
    def identity(self) -> str: ...
    @property
    def diagnostic_hint(self) -> str: ...


import glob  # noqa: E402
import sys  # noqa: E402
from collections.abc import Callable, Mapping, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from types import MappingProxyType  # noqa: E402
from typing import TYPE_CHECKING, Final  # noqa: E402

from railctl.errors import (  # noqa: E402
    AmbiguousPort,
    PortNotFound,
    TransportError,
    UnsupportedFeatureError,
)
from railctl.transport.serial_posix import SerialConfig, SerialTransport  # noqa: E402

if TYPE_CHECKING:
    from railctl.envelope import Frame
    from railctl.link import Link


@dataclass(frozen=True, slots=True)
class Discovery:
    """One platform's auto-detection: the pattern, and the advice when it matches nothing.

    The two travel together because they are one measurement. A pattern without
    its own advice is how "no port matched" ends up telling a Linux operator to
    check a USB cable, when the answer on that machine is usually that the
    device was never attached to this kernel in the first place.
    """

    pattern: str
    nothing_found_hint: str


# Both patterns name the SAME physical CDC function and look nothing alike,
# because the two kernels number a composite device differently. macOS names a
# node after each function's CDC DATA interface (1 is LocoNet (silent), 3 is
# XpressNet, 5 is the YD.Control telemetry stream - hence "*3"); Linux's
# /dev/serial/by-id names it after the CONTROL interface (if00, if02, if04).
# XpressNet is the middle function on both, so macOS "...3" and Linux "-if02"
# are the same wire, and "-if03" - the number that looks right - matches nothing
# at all.
#
# Measured 2026-10-14 on the reference unit (03eb:245a, "YaMoRC YD700x MultiBus
# Command Station", USB serial 7010A0001194) forwarded into WSL2 by usbipd:
# -if00 -> ttyACM0 LocoNet, -if02 -> ttyACM1 XpressNet, -if04 -> ttyACM2
# telemetry, cross-checked against dmesg's cdc_acm 1-1:1.0 / 1-1:1.2 / 1-1:1.4.
# See docs/probe-results.md, section "Linux enumerates the same three CDC
# functions under different numbers".
#
# MappingProxyType rather than a bare dict: the table is a platform claim, and
# tests/unit/test_open_link.py is the only place its contents are checked, so a
# later PORT_DISCOVERY[...] = ... would pass that check by mutating what it
# compares against. EXIT_MEANINGS in exit_codes.py is sealed the same way.
#
# Either pattern is still a guess, exactly as the single one was: confirmation
# is Link.open()'s handshake and it is mandatory.
PORT_DISCOVERY: Final[Mapping[str, Discovery]] = MappingProxyType(
    {
        "darwin": Discovery(
            pattern="/dev/cu.usbmodem*3",
            nothing_found_hint="check the USB cable and that the station is powered",
        ),
        "linux": Discovery(
            pattern="/dev/serial/by-id/usb-YaMoRC_YD700x*-if02",
            nothing_found_hint=(
                "check the USB cable and that the station is powered; that "
                "directory is created by udev, so a system without it has "
                "nothing to glob, and under WSL2 the device must be attached "
                "with usbipd first"
            ),
        ),
    }
)
Z21_DEFAULT_PORT = 21105


def discovery_for(platform: str | None = None) -> Discovery:
    """How `auto` looks for the station on `platform` - or a refusal to guess.

    sys.platform is read HERE and deliberately not captured as a default
    argument value: a default is evaluated once, at import, so
    `platform: str = sys.platform` would freeze whichever machine imported the
    module. Every `auto` run on the other platform would then glob a pattern
    that cannot match, and no test that patched sys.platform afterwards could
    tell - it would be reading the frozen constant back to itself.

    Resolution is lazy for a second reason: an unmeasured platform raises, and
    raising at import time would take `railctl --help` down with it.
    """
    name = sys.platform if platform is None else platform
    found = PORT_DISCOVERY.get(name)
    if found is None:
        # Not PortNotFound. Nobody looked: there is no measured pattern for this
        # platform, and "no port matched" would record the station as absent
        # because the instrument was never built for this machine. The port
        # itself may well work - name it with serial: and find out.
        raise UnsupportedFeatureError(
            f"auto-detection has not been measured on {name}; the measured "
            f"platforms are {', '.join(sorted(PORT_DISCOVERY))}",
            hint="name the device explicitly, for example serial:<device path>",
        )
    return found


def list_candidate_ports() -> list[str]:
    return sorted(glob.glob(discovery_for().pattern))


def find_xpressnet_port(candidates: Sequence[str] | None = None) -> str:
    # `globbed` is the whole reason this function stays usable on a platform the
    # table does not describe. Only the auto path has a pattern to quote, and
    # only it should answer for the platform: a caller who supplied candidates
    # touched no glob, no platform and no device path, so neither the message
    # nor the exception class may depend on which kernel is running. Resolving
    # the discovery unconditionally turned find_xpressnet_port([]) from
    # PortNotFound into UnsupportedFeatureError - the same question, a different
    # exit code - on every POSIX platform outside the table.
    globbed = candidates is None
    found = list(list_candidate_ports() if globbed else candidates)
    if not found:
        if globbed:
            discovery = discovery_for()
            raise PortNotFound(
                f"no XpressNet CDC port matching {discovery.pattern}",
                hint=discovery.nothing_found_hint,
            )
        raise PortNotFound(
            "no XpressNet CDC port among the candidates offered",
            hint="check the USB cable and that the station is powered",
        )
    if len(found) > 1:
        joined = ", ".join(found)
        matched = f" matching {discovery_for().pattern}" if globbed else ""
        raise AmbiguousPort(
            f"more than one XpressNet CDC port{matched}: {joined}",
            hint=f"name one, for example serial:{found[0]}",
        )
    return found[0]


def transport_for(target: str) -> Transport:
    """The one place a connection target is parsed. Nothing above this may split it."""
    if target == "auto":
        return SerialTransport(SerialConfig(find_xpressnet_port()))
    if target.startswith("serial:"):
        port = target[len("serial:") :]
        if not port:
            raise PortNotFound(
                "serial: target has no device path",
                hint="use serial:<device path>, or auto",
            )
        return SerialTransport(SerialConfig(port))
    if target.startswith("z21:"):
        host, separator, port = target[len("z21:") :].partition(":")
        if not host or (separator and not port.isdigit()):
            raise TransportError(
                f"malformed target {target!r}",
                hint=f"expected z21:HOST:PORT, for example z21:192.168.0.111:{Z21_DEFAULT_PORT}",
            )
        # Parsed, understood, and refused: the LAN transport is a pure addition
        # scheduled after this milestone, and a user whose address was correct
        # deserves to be told that rather than shown a parse error.
        where = f"{host}:{port or Z21_DEFAULT_PORT}"
        raise UnsupportedFeatureError(
            f"the Z21 LAN transport is not implemented yet ({where})",
            hint="use auto, or serial: with the device path",
        )
    raise TransportError(
        f"unknown connection target {target!r}",
        hint="expected auto, serial:<device path>, or z21:HOST:PORT",
    )


def open_link(target: str = "auto", *, on_event: Callable[[Frame], None] | None = None) -> Link:
    """Resolve a target, build the link, open the port and run the handshake."""
    from railctl.envelope.liusb import LiUsbEnvelope
    from railctl.link import Link  # deferred: link.py type-hints Transport from here

    link = Link(transport_for(target), LiUsbEnvelope(), on_event=on_event)
    link.open()
    return link
