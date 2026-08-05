# tests/hardware/test_m4_acceptance.py
"""M4 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly:  .venv/bin/python -m pytest -m hardware -q -s
These are skipped by the default addopts.
"""

from __future__ import annotations

import os

import pytest

from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import PortNotXpressNet
from railctl.link import Link
from railctl.transport import find_xpressnet_port, open_link
from railctl.transport.serial_posix import SerialConfig, SerialTransport

pytestmark = pytest.mark.hardware

VERSION_HEADER = 0x63
VERSION_MARKER = 0x21


def test_auto_detection_finds_the_xpressnet_port():
    port = find_xpressnet_port()
    print(f"\nXpressNet port: {port}")
    assert port.startswith("/dev/cu.usbmodem")
    assert port.endswith("3")


def test_open_link_auto_completes_the_handshake():
    link = open_link("auto")
    try:
        telegram = link.version_telegram
        print(f"\n{link.description}  version telegram {telegram.hex(' ').upper()}")
        assert telegram[0] == VERSION_HEADER
        assert telegram[1] == VERSION_MARKER
        # Reference unit: 63 21 40 12 -> XpressNet 4.0, command station id 0x12.
        assert telegram[2] == 0x40
        assert telegram[3] == 0x12
        assert link.stats().frames_ok >= 1
        assert link.stats().timeouts == 0
    finally:
        link.close()


def test_the_telemetry_port_shows_bytes_dropped_climbing_with_no_frames():
    """The acceptance check for the stats counters.

    Interface 5 is the YD.Control telemetry stream: ASCII lines, no framing.
    Opening it instead of the XpressNet interface must fail loudly, and the two
    counters must say WHY - a climbing bytes_dropped with frames_ok stuck at 0
    is "wrong CDC interface", not "dead port". That distinction is the whole
    reason the counters exist.
    """
    telemetry = find_xpressnet_port()[:-1] + "5"
    if not os.path.exists(telemetry):
        pytest.skip(f"{telemetry} is not present")

    envelope = LiUsbEnvelope()
    link = Link(SerialTransport(SerialConfig(telemetry)), envelope)
    with pytest.raises(PortNotXpressNet) as caught:
        link.open()
    print(f"\n{caught.value}")
    stats = envelope.stats
    print(f"frames_ok={stats.frames_ok} bytes_dropped={stats.bytes_dropped}")
    assert stats.frames_ok == 0
    assert stats.bytes_dropped > 0
