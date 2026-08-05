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
    the PROGRAMMING track, where it did not turn a wheel - the motor only
    buzzed, because that output is current limited (750 mA per the YD7010
    manual) and will not turn a sound decoder's motor. Note what that buzz
    proves: the drive telegram DID reach the decoder there. The two runs
    differed in whether the locomotive moved, and `loco_info` answered
    `speed=30` in both, because it reads the command station's own refresh
    buffer - the station reports back what we put there and the decoder has no
    say in the answer. Nothing on this hardware closes the loop: POM read
    returns nothing (docs/probe-results.md R1), and the telemetry stream's
    `TC` reads 0 mA for a decoder that is demonstrably powered and responding.

    So a green run means the command path reaches the station and the station
    answers as the protocol says it should. Movement is verified by a human
    watching the wheels, and by nothing else. That happened on 2026-08-05:
    the wheels turned at step 30 and stopped abruptly on the emergency stop -
    abruptly being the tell that this is the emergency path and not a braked
    `drive(0)`, which would decelerate on the decoder's CV4 ramp.
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

        if not was_on:
            station.power_off()
            print("track power restored to off")
        else:
            print("track power left on, as found")
    finally:
        station.close()
