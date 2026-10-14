# tests/unit/test_hardware_port_shapes.py
"""The hardware acceptance file's port arithmetic, measured without hardware.

tests/hardware/ is deselected by default and runs on one bench at a time, so its
macOS arithmetic has not executed since the Linux bench appeared and its Linux
arithmetic has never executed on the Mac. The arithmetic is pure string work.
This is where it gets measured, and the case it exists for is the number that
looks right: "-if03" is the data-interface number macOS uses for XpressNet and
matches no Linux node, and "-if05" is what the old port[:-1] + "5" produced.
"""

from __future__ import annotations

import pytest

from tests.hardware.ports import PORT_SHAPES, port_shape, telemetry_sibling

DARWIN_XPRESSNET = "/dev/cu.usbmodem7010A00011943"
LINUX_XPRESSNET = "/dev/serial/by-id/usb-YaMoRC_YD700x_MultiBus_Command_Station_7010A0001194-if02"


def test_the_telemetry_sibling_is_the_third_function_on_both_platforms():
    assert telemetry_sibling(DARWIN_XPRESSNET, "darwin") == "/dev/cu.usbmodem7010A00011945"
    assert telemetry_sibling(LINUX_XPRESSNET, "linux") == (
        "/dev/serial/by-id/usb-YaMoRC_YD700x_MultiBus_Command_Station_7010A0001194-if04"
    )


def test_the_linux_sibling_is_neither_if03_nor_if05():
    """The two wrong answers, named. "-if03" is the macOS data-interface number
    for XpressNet and exists on no Linux system; "-if05" is what deriving the
    sibling by last-character substitution produced, and it made the counters
    acceptance skip instead of run.
    """
    sibling = telemetry_sibling(LINUX_XPRESSNET, "linux")
    assert "-if03" not in sibling
    assert "-if05" not in sibling


def test_a_port_that_does_not_fit_the_table_is_an_error_not_a_guess():
    """Deriving a sibling from a port the table does not describe would hand the
    acceptance test a path to os.path.exists, which would skip - the failure this
    helper replaced.
    """
    with pytest.raises(AssertionError, match="port shape table is stale"):
        telemetry_sibling("/dev/cu.usbmodem7010A00011941", "darwin")


def test_an_unmeasured_platform_is_named_rather_than_guessed_at():
    with pytest.raises(KeyError, match="win32"):
        port_shape("win32")


def test_the_shape_table_covers_the_platforms_the_readme_claims():
    assert sorted(PORT_SHAPES) == ["darwin", "linux"]
