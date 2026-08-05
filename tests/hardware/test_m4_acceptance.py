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
from railctl.link import HANDSHAKE_TIMEOUT, Link
from railctl.transport import find_xpressnet_port, open_link
from railctl.transport.serial_posix import SerialConfig, SerialTransport

pytestmark = pytest.mark.hardware

VERSION_HEADER = 0x63
VERSION_MARKER = 0x21
# 5 x HANDSHAKE_TIMEOUT = 10 s. The measured maximum gap is 4.83 s, but the
# first run of this loop needed three attempts - two consecutive empty windows -
# so 6 s was already grazing the edge. Ten seconds costs a few seconds on a
# suite that only runs by hand, and buys a test that is evidence rather than a
# coin toss.
TELEMETRY_ATTEMPTS = 5


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

    Why this opens the port more than once. The stream is periodic, not
    continuous: measured on 2026-08-05 over 30 s, 24 arrivals totalling 554
    bytes, mean gap 1.27 s and MAX GAP 4.83 s. `open()` gives up after
    HANDSHAKE_TIMEOUT (2.0 s), so a single attempt can legitimately see zero
    bytes on a perfectly healthy station - which is exactly what happened on
    that date, after this test had been passing on luck since M4. The envelope's
    counters are per session and survive `reset()` (only the buffer is per
    connection), so consecutive attempts accumulate. The first run of this loop
    needed three of them, so the count is five: two empty windows in a row have
    been observed, and a margin that has already been grazed is not a margin.

    The irony is worth keeping in the file: this test exists to prove the
    counters can tell silence from noise, and it was itself reading a gap in
    the noise as silence.
    """
    telemetry = find_xpressnet_port()[:-1] + "5"
    if not os.path.exists(telemetry):
        pytest.skip(f"{telemetry} is not present")

    envelope = LiUsbEnvelope()
    link = Link(SerialTransport(SerialConfig(telemetry)), envelope)
    attempts = 0
    while attempts < TELEMETRY_ATTEMPTS and envelope.stats.bytes_dropped == 0:
        attempts += 1
        with pytest.raises(PortNotXpressNet) as caught:
            link.open()
    print(f"\n{caught.value}")
    stats = envelope.stats
    print(f"attempts={attempts} frames_ok={stats.frames_ok} bytes_dropped={stats.bytes_dropped}")
    # frames_ok is the load-bearing half and holds however long we listen: no
    # amount of ASCII telemetry ever assembles into a valid XpressNet frame.
    assert stats.frames_ok == 0
    assert stats.bytes_dropped > 0, (
        f"the telemetry interface said nothing across {attempts} attempts "
        f"({attempts * HANDSHAKE_TIMEOUT:.0f} s); the measured maximum gap is 4.83 s, so "
        "either the station is genuinely silent or the stream's timing has changed"
    )
