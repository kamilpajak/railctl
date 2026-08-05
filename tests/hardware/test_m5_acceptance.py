# tests/hardware/test_m5_acceptance.py
"""M5 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly:  uv run pytest -m hardware -s
Deselected by default (pyproject.toml's addopts carries -m 'not hardware').
"""

from __future__ import annotations

import pytest

from railctl.station.facade import Station
from railctl.xbus.speed import Direction

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = 3
ACCEPTANCE_SPEED = 30
ACCEPTANCE_PAUSE_SECONDS = 3.0


def test_power_drive_stop_power_off_restores_the_track_power_it_found():
    """The M5 verification sentence (CONTRACT.md): power on, drive 30
    forward, pause, STOP, power off - and leave track power exactly as it
    was found, since this test runs on a shared bench. "Stop" means
    `emergency_stop`, not a braked `drive(0)`: the sentence names the
    emergency-stop path specifically, because that is the one path this
    plan's Task 2 wires straight to `cmd_emergency_stop_loco` without going
    through the band-warning machinery `drive` and `loco_info` share - a
    safety broadcast has to work even when address validation would not.

    WHAT THIS TEST CANNOT PROVE, and no version of it ever will: that the
    locomotive moved. It passed on 2026-08-05 with the locomotive sitting on
    the PROGRAMMING track, where it did not turn a wheel; the motor made an
    audible hum instead. That the decoder was energised and made a sound while
    the drive command was in flight is observation. WHY it hummed rather than
    turned is not: the 750 mA ceiling on that output is real (YD7010 manual,
    "The programming track can supply a maximum of 750mA", and it is
    configurable), but no source connects a prog-track current limit to a hum,
    and the only documented overcurrent behaviour is the opposite - the output
    switches off. A stalled motor under PWM below Vstart, or the sound decoder's
    own speaker, would look the same from here. Do not repeat the current-limit
    story as if it were established.

    What the two runs settle is the part that matters: `loco_info` answered
    `speed=30` whether the wheels turned or not. XpressNet says only that the
    reply "provides the current speed and direction information for the
    decoder", and the station is answering from what it holds - it cannot be
    answering from the decoder, because a plain DCC decoder has no back channel
    at all. RailCom is a separate standard (NMRA S-9.3.2) and speed is an
    optional datagram there. Nothing on this hardware closes that loop: POM read
    returns nothing (docs/probe-results.md R1), and the telemetry stream's `TC`
    reads 0 mA for a decoder that is demonstrably powered and responding.

    So a green run means the command path reaches the station and the station
    answers as the protocol says it should. Movement is verified by a human
    watching the wheels, and by nothing else. That happened on 2026-08-05:
    the wheels turned at step 30 and stopped abruptly on the emergency stop.
    Abruptly is the tell that this was the emergency path: NMRA S-9.2 requires a
    decoder receiving emergency stop to "immediately stop delivering power to
    the motor", while a `drive(0)` decelerates on a configured ramp - CV4 in
    general, though on ZIMO the emergency-stop ramp has its own CV111, default 0
    for immediate, so the contrast is configurable rather than guaranteed.
    """
    station = Station.open()
    try:
        was_on = station.status().track_power
        print(f"\ntrack power before: {'on' if was_on else 'off'}")

        station.power_on()
        station.drive(ACCEPTANCE_ADDRESS, ACCEPTANCE_SPEED, Direction.FORWARD)
        print(f"driving loco {ACCEPTANCE_ADDRESS} at step {ACCEPTANCE_SPEED} forward")

        station.pause(ACCEPTANCE_PAUSE_SECONDS)

        info = station.loco_info(ACCEPTANCE_ADDRESS)
        print(f"loco_info before stop: speed={info.speed} direction={info.direction}")

        station.emergency_stop(ACCEPTANCE_ADDRESS)
        print(f"emergency-stopped loco {ACCEPTANCE_ADDRESS}")

        # power_off() runs UNCONDITIONALLY, then the found state is restored.
        # The old version only called it when it found the power already off,
        # which is why this path had never once run on hardware - and why the
        # swapped status bits (docs/probe-results.md R2) survived three
        # acceptance runs. A branch that skips the risky call whenever the
        # bench happens to be in the common state is not coverage.
        station.power_off()
        assert station.status().track_power is False, (
            "power_off() returned but the station still reports the track live"
        )
        print("power off verified")

        if was_on:
            station.power_on()
            print("track power restored to on, as found")
        else:
            print("track power left off, as found")
    finally:
        station.close()
