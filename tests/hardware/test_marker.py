"""Canary for the `hardware` marker.

It must never run without an explicit `-m hardware`. Its job is to make the
"deselected" count in a default run non-zero, so a broken marker registration
shows up as `0 deselected` instead of as nothing at all.
"""

import pytest


@pytest.mark.hardware
def test_the_hardware_marker_selects_this_test():
    assert True
