# tests/hardware/test_m4_acceptance.py
"""M4 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly:  uv run pytest -m hardware -s
These are skipped by the default addopts, which already carries -q: adding a
second one raises the quiet level to 2 and suppresses the summary line you came
here to read.
"""

from __future__ import annotations

import os
import time

import pytest

from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import PortNotXpressNet
from railctl.link import HANDSHAKE_TIMEOUT, Link
from railctl.transport import find_xpressnet_port, open_link
from railctl.transport.serial_posix import SerialConfig, SerialTransport
from tests.hardware.ports import port_shape, telemetry_sibling

pytestmark = pytest.mark.hardware

VERSION_HEADER = 0x63
VERSION_MARKER = 0x21
# The stream is periodic and the period is MEASURED: 15.3084 s, from 19
# consecutive gaps over a 300 s raw listen on 2026-11-10 (docs/probe-results.md,
# "The telemetry stream is a repeated query at a measured 15.3 s period").
#
# This loop is bounded by a DEADLINE rather than by a count of attempts, and
# that correction has now been needed twice. The count was three, then five -
# and five 2-second attempts is a 10 s window against a 15.3 s period, which
# cannot pass reliably. That is arithmetic, not a thin margin. It did not fail
# randomly either: ten consecutive runs alternated pass and fail without a
# single exception, because a 10 s failure and a 2-6 s pass beat against the
# period and each run set the phase of the next. A verdict that depends on how
# long the PREVIOUS test took is not measuring the station.
#
# The deadline is DERIVED from the period rather than restated beside it, so
# that re-measuring the period moves the window with it: a second literal is a
# second thing to forget, and forgetting it is the whole history above. 2.6
# periods is about 40 s. It costs nothing while the stream is alive, because the
# loop stops at the first byte; the full window is only ever paid when the
# station is genuinely silent, which is exactly when waiting longer is right.
TELEMETRY_PERIOD_SECONDS = 15.3084
TELEMETRY_DEADLINE_PERIODS = 2.6
TELEMETRY_DEADLINE_SECONDS = TELEMETRY_DEADLINE_PERIODS * TELEMETRY_PERIOD_SECONDS


def test_auto_detection_finds_the_xpressnet_port():
    """The shape is the assertion, because a glob of one cannot certify itself.

    Comparing the result against list_candidate_ports() would be circular -
    find_xpressnet_port() only returns at all when that list held exactly one
    entry - and would pass for "-if03", for /dev/ttyACM*, or for "*1", the silent
    LocoNet interface. The prefix and suffix come from the per-platform table in
    tests/hardware/ports.py.
    """
    prefix, xpressnet, _telemetry = port_shape()
    port = find_xpressnet_port()
    print(f"\nXpressNet port: {port}")
    assert port.startswith(prefix)
    assert port.endswith(xpressnet)
    assert os.path.exists(port)


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
    continuous, and the period is what this loop must be sized against. The
    figures this docstring used to carry - 24 arrivals, 554 bytes over 30 s,
    mean gap 1.27 s, MAX GAP 4.83 s, attributed to 2026-08-05 - have no entry
    in docs/probe-results.md to audit them against, and they no longer describe
    this station: a 300 s raw listen on 2026-11-10 saw 20 arrivals of exactly 7
    bytes, one every 15.3084 s, and no firmware banner at all.

    `open()` gives up after HANDSHAKE_TIMEOUT (2.0 s) here - its resend path
    needs a reply in `_RETRY_PREFIXES`, and no amount of this ASCII stream ever
    assembles into a frame, which is the assertion below - so a single attempt
    can see zero bytes on a perfectly healthy station. The envelope's counters
    are per session and survive `reset()` (only the buffer is per connection),
    so consecutive attempts accumulate.

    The deadline replaced a count of attempts because the count was measuring
    the wrong thing. See TELEMETRY_DEADLINE_SECONDS: at five attempts this test
    was not flaky in the statistical sense but phase-locked, alternating pass
    and fail ten times out of ten, so repeating it sampled one phase rather
    than a probability. In CI the verdict would have turned on the duration of
    whatever happened to run before it.

    The irony is worth keeping in the file: this test exists to prove the
    counters can tell silence from noise, and it has now twice read a gap in
    the noise as silence - once by luck, and once by resonance.
    """
    # A FAILURE, not a skip. The XpressNet port was just found, and this station
    # exposes three CDC functions - so a missing third one is a finding about the
    # hardware or about the derivation, never a reason to stop measuring. The
    # skip this replaced is how the old port[:-1] + "5" hid itself on Linux: it
    # produced "-if05", which no station has, and the counters acceptance quietly
    # stopped running while -if04 sat there the whole time.
    telemetry = telemetry_sibling(find_xpressnet_port())
    assert os.path.exists(telemetry), (
        f"{telemetry} is not present even though the XpressNet port was found; "
        f"either the station is not exposing its telemetry function or the port "
        f"shape table in tests/hardware/ports.py is stale"
    )

    envelope = LiUsbEnvelope()
    link = Link(SerialTransport(SerialConfig(telemetry)), envelope)
    attempts = 0
    started = time.monotonic()
    deadline = started + TELEMETRY_DEADLINE_SECONDS
    while True:
        attempts += 1
        with pytest.raises(PortNotXpressNet) as caught:
            link.open()
        if envelope.stats.bytes_dropped > 0 or time.monotonic() >= deadline:
            break
    waited = time.monotonic() - started
    print(f"\n{caught.value}")
    stats = envelope.stats
    print(
        f"waited={waited:.1f}s attempts={attempts} "
        f"frames_ok={stats.frames_ok} bytes_dropped={stats.bytes_dropped}"
    )
    # frames_ok is the load-bearing half and holds however long we listen: no
    # amount of ASCII telemetry ever assembles into a valid XpressNet frame.
    assert stats.frames_ok == 0
    assert stats.bytes_dropped > 0, (
        f"the telemetry interface said nothing in {waited:.1f} s across {attempts} attempts "
        f"of {HANDSHAKE_TIMEOUT:.1f} s; the measured period is "
        f"{TELEMETRY_PERIOD_SECONDS:.4f} s (300 s raw listen, 2026-11-10), so a window this "
        f"long should have held about {waited / TELEMETRY_PERIOD_SECONDS:.1f} arrivals. Either "
        "the station is genuinely silent or its telemetry cadence has changed again - measure "
        "it and record it before raising this deadline, because raising the old attempt count "
        "is what hid this problem twice"
    )
