# tests/unit/test_open_link.py
from __future__ import annotations

import pytest

import railctl.transport as transport_module
from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import AmbiguousPort, PortNotFound, TransportError, UnsupportedFeatureError
from railctl.transport import (
    PORT_DISCOVERY,
    discovery_for,
    find_xpressnet_port,
    list_candidate_ports,
    open_link,
    transport_for,
)
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.transport.serial_posix import BAUDRATE, CDC_INDEX_HINT, SerialTransport

PORT_43 = "/dev/cu.usbmodem7010A00011943"
PORT_OTHER = "/dev/cu.usbmodemAAAA3"
VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"

#: The two patterns auto-detection is allowed to use, spelled out a second time
#: here because this is still the only test that would notice a wrong one - and a
#: wrong one is not hypothetical. macOS names a CDC node after each function's
#: DATA interface (1, 3, 5); Linux's /dev/serial/by-id names it after the CONTROL
#: interface (if00, if02, if04). XpressNet is the middle function on both, so
#: macOS "3" and Linux "-if02" are one interface and "-if03" - the number that
#: looks right - matches nothing. Measured 2026-10-14 on 03eb:245a, serial
#: 7010A0001194; docs/probe-results.md, section "Linux enumerates the same three
#: CDC functions under different numbers".
EXPECTED_GLOBS = {
    "darwin": "/dev/cu.usbmodem*3",
    "linux": "/dev/serial/by-id/usb-YaMoRC_YD700x*-if02",
}

# str(exc) is the message alone and exc.hint is the hint, because the CLI prints
# them on separate lines (spec line 159). Anything asserted about advice is read
# off .hint; pytest.raises(match=...) only ever sees the message.


def test_a_single_candidate_is_the_xpressnet_port():
    assert find_xpressnet_port([PORT_43]) == PORT_43


def test_no_candidate_raises_port_not_found():
    with pytest.raises(PortNotFound, match="no XpressNet"):
        find_xpressnet_port([])


def test_two_candidates_raise_ambiguous_port_naming_both():
    with pytest.raises(AmbiguousPort) as caught:
        find_xpressnet_port([PORT_43, PORT_OTHER])
    assert PORT_43 in str(caught.value)
    assert PORT_OTHER in str(caught.value)
    assert "serial:" in caught.value.hint


def test_an_explicit_serial_target_is_used_verbatim():
    transport = transport_for(f"serial:{PORT_43}")
    assert isinstance(transport, SerialTransport)
    assert transport.description == f"xpressnet serial {PORT_43}"
    assert transport.identity == PORT_43
    # The advice link.py quotes on a failed handshake belongs to the transport,
    # so the Z21 LAN transport lands without editing link.py (spec line 583).
    assert transport.diagnostic_hint == CDC_INDEX_HINT


def test_a_serial_target_with_no_path_is_rejected():
    with pytest.raises(PortNotFound, match="serial:"):
        transport_for("serial:")


def test_a_well_formed_z21_target_parses_and_is_refused_cleanly():
    """The future LAN transport must not crash the parser today. Parsing it and
    then refusing it is what tells a user their address was understood.
    """
    with pytest.raises(UnsupportedFeatureError) as caught:
        transport_for("z21:192.168.0.111:21105")
    assert "192.168.0.111:21105" in str(caught.value)


def test_a_malformed_z21_target_is_a_transport_error_not_a_crash():
    with pytest.raises(TransportError) as caught:
        transport_for("z21:192.168.0.111:not-a-port")
    assert "192.168.0.111:not-a-port" in str(caught.value)
    assert "z21:HOST:PORT" in caught.value.hint


def test_an_unknown_target_names_the_forms_that_work():
    with pytest.raises(TransportError) as caught:
        transport_for("http://station.local")
    assert "http://station.local" in str(caught.value)
    message = caught.value.hint
    assert "auto" in message
    assert "serial:" in message
    assert "z21:" in message


def test_the_baudrate_is_the_one_lenz_23151_specifies():
    assert BAUDRATE == 57600


def test_the_pattern_names_the_xpressnet_interface_on_both_platforms():
    """These two patterns are what pick the CDC function railctl talks to. Get
    one wrong - "*1", the silent LocoNet interface, or "-if03", which exists on
    no Linux system - and this is the only test that would notice.

    They are deliberately NOT the same interface number, and the parameter is
    what makes both of them checkable from whichever machine happens to be
    running the suite: CI is Linux and the bench that owns the station is macOS,
    so neither could reach the other's branch on its own.
    """
    assert discovery_for("darwin").pattern == EXPECTED_GLOBS["darwin"]
    assert discovery_for("linux").pattern == EXPECTED_GLOBS["linux"]


def test_the_discovery_table_holds_exactly_the_two_measured_platforms():
    """A third entry in that table is a platform claim, and the README, CLAUDE.md
    and the design spec all name the platforms this tool supports. Nothing else
    in the suite compares those documents against the code that does the
    detecting; this is the one place the count is pinned.
    """
    assert {name: entry.pattern for name, entry in PORT_DISCOVERY.items()} == EXPECTED_GLOBS


def test_list_candidate_ports_globs_the_pattern_once_and_sorts(monkeypatch):
    """sorted() is what decides which port AmbiguousPort names first in its hint,
    so an unsorted glob.glob() result must come back sorted.

    Which pattern is right is pinned by the two tests above; what this one holds
    is that exactly one glob call happens and that it uses the table's pattern
    rather than a second literal spelled out again here. glob.glob is faked, so
    no default-selected test ever reads the real /dev - which matters now that
    the bench running this suite has a station attached.
    """
    seen_patterns = []
    unsorted = [PORT_OTHER, PORT_43]

    def fake_glob(pattern):
        seen_patterns.append(pattern)
        return unsorted

    monkeypatch.setattr(transport_module.glob, "glob", fake_glob)

    result = list_candidate_ports()

    assert seen_patterns == [discovery_for().pattern]
    assert result == sorted(unsorted)


@pytest.mark.parametrize("platform", sorted(EXPECTED_GLOBS))
def test_discovery_defaults_to_the_platform_it_is_running_on(platform, monkeypatch):
    """The platform parameter exists for the tests above; production passes
    nothing. Two ways to get that wrong and have every other test here still
    pass: default the parameter to one platform's name, or write it as
    `platform: str = sys.platform`, which Python evaluates once at import and
    freezes. Either one makes every run on the other platform glob a pattern
    that cannot match and report the station as missing.

    Parametrized over BOTH platforms on purpose. Patching sys.platform to the
    value the host already has is a no-op, so a single-platform version of this
    test passes while both of those mutations survive - measured, not assumed.
    """
    seen_patterns = []
    monkeypatch.setattr(transport_module.sys, "platform", platform)
    monkeypatch.setattr(
        transport_module.glob, "glob", lambda pattern: seen_patterns.append(pattern) or []
    )

    list_candidate_ports()

    assert seen_patterns == [EXPECTED_GLOBS[platform]]


def test_a_platform_nobody_measured_is_refused_rather_than_reported_empty(monkeypatch):
    """PortNotFound here would read as "the station is not plugged in" - a
    capability recorded as absent because the instrument was never built for
    this machine. The refusal names the platform and still offers serial:, which
    needs no discovery at all.
    """
    monkeypatch.setattr(transport_module.sys, "platform", "win32")
    monkeypatch.setattr(
        transport_module.glob,
        "glob",
        lambda pattern: pytest.fail(f"globbed {pattern!r} on an unmeasured platform"),
    )

    with pytest.raises(UnsupportedFeatureError) as caught:
        list_candidate_ports()

    assert "win32" in str(caught.value)
    assert "serial:" in caught.value.hint


def test_the_nothing_found_hint_belongs_to_its_platform(monkeypatch):
    """The hint travels with the pattern because they are one measurement. On
    Linux "check the USB cable" is the wrong advice: the usual cause is that
    udev never made the by-id directory, or that the device was never attached
    to this kernel. Without this test the two platforms' hints can be swapped,
    or the udev sentence deleted, and the whole suite stays green.

    The second half pins the WIRING, not the strings: the hint PortNotFound
    carries has to be this platform's, not whichever one the code reached for.
    glob.glob is faked empty so the raise is deterministic - the bench running
    this suite has a station attached, and an unfaked glob here would find it.
    """
    linux_hint = discovery_for("linux").nothing_found_hint
    darwin_hint = discovery_for("darwin").nothing_found_hint

    # Both platforms keep the physical check, which is right on either. Only
    # Linux names udev and usbipd, and asserting their ABSENCE on macOS is not
    # enough on its own: every string that is not the Linux hint satisfies that,
    # so the macOS hint has to be pinned for what it does say. Measured by
    # mutation - replacing it passed the whole file before this line existed.
    assert "cable" in linux_hint
    assert "cable" in darwin_hint
    assert "udev" in linux_hint
    assert "usbipd" in linux_hint
    assert "udev" not in darwin_hint
    assert "usbipd" not in darwin_hint

    monkeypatch.setattr(transport_module.glob, "glob", lambda pattern: [])

    with pytest.raises(PortNotFound) as caught:
        find_xpressnet_port()

    assert caught.value.hint == discovery_for().nothing_found_hint
    assert discovery_for().pattern in str(caught.value)


def test_z21_host_with_no_port_is_unsupported_not_malformed():
    """z21:HOST with no explicit port is understood and refused, never a parse
    error - Z21_DEFAULT_PORT must be filled in for the message.
    """
    with pytest.raises(UnsupportedFeatureError) as caught:
        transport_for("z21:192.168.0.111")
    assert "192.168.0.111:21105" in str(caught.value)


def test_z21_host_with_trailing_colon_and_no_port_is_malformed():
    with pytest.raises(TransportError):
        transport_for("z21:192.168.0.111:")


def test_transport_for_auto_uses_the_discovered_port(monkeypatch):
    monkeypatch.setattr(transport_module, "list_candidate_ports", lambda: [PORT_43])

    transport = transport_for("auto")

    assert transport.identity == PORT_43


def test_open_link_completes_the_handshake_through_a_fake_transport(monkeypatch):
    envelope = LiUsbEnvelope()
    fake = FakeTransport(clock=FakeClock())
    fake.expect(
        envelope.frame(Kind.SOLICITED, VERSION_REQUEST),
        reply=envelope.frame(Kind.SOLICITED, VERSION_REPLY),
    )
    monkeypatch.setattr(transport_module, "transport_for", lambda target: fake)

    link = open_link("auto")
    try:
        assert link.version_telegram == VERSION_REPLY
    finally:
        link.close()
