"""Byte pipes. A transport moves bytes and knows nothing about framing.

read() blocks up to `timeout`, returns as soon as at least one byte is
available, returns b"" on timeout, and never raises on timeout - framing is not
its problem. write() writes everything or raises TransportError.

diagnostic_hint is here rather than in link.py on purpose. When a handshake or an
exchange fails, the useful advice is about the CONNECTION, and only the transport
knows what kind of connection it is - a CDC interface index for the serial port, a
network address for the future Z21 UDP transport. Spec line 583 requires the LAN
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
from collections.abc import Callable, Sequence  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

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

# The CDC interface index picks the bus on this hardware: 1 is LocoNet (silent),
# 3 is XpressNet, 5 is the YD.Control telemetry stream. The glob is a guess;
# confirmation is Link.open()'s handshake and it is mandatory.
PORT_GLOB = "/dev/cu.usbmodem*3"
Z21_DEFAULT_PORT = 21105


def list_candidate_ports() -> list[str]:
    return sorted(glob.glob(PORT_GLOB))


def find_xpressnet_port(candidates: Sequence[str] | None = None) -> str:
    found = list(list_candidate_ports() if candidates is None else candidates)
    if not found:
        raise PortNotFound(
            f"no XpressNet CDC port matching {PORT_GLOB}",
            hint="check the USB cable and that the station is powered",
        )
    if len(found) > 1:
        joined = ", ".join(found)
        raise AmbiguousPort(
            f"more than one XpressNet CDC port matches {PORT_GLOB}: {joined}",
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
                hint="use serial:/dev/cu.usbmodem... or auto",
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
            hint="use auto or serial:/dev/cu.usbmodem...",
        )
    raise TransportError(
        f"unknown connection target {target!r}",
        hint="expected auto, serial:/dev/cu.usbmodem..., or z21:HOST:PORT",
    )


def open_link(target: str = "auto", *, on_event: Callable[[Frame], None] | None = None) -> Link:
    """Resolve a target, build the link, open the port and run the handshake."""
    from railctl.envelope.liusb import LiUsbEnvelope
    from railctl.link import Link  # deferred: link.py type-hints Transport from here

    link = Link(transport_for(target), LiUsbEnvelope(), on_event=on_event)
    link.open()
    return link
