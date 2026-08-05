# tests/unit/test_open_link.py
from __future__ import annotations

import pytest

import railctl.transport as transport_module
from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import AmbiguousPort, PortNotFound, TransportError, UnsupportedFeatureError
from railctl.transport import find_xpressnet_port, list_candidate_ports, open_link, transport_for
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.transport.serial_posix import BAUDRATE, CDC_INDEX_HINT, SerialTransport

PORT_43 = "/dev/cu.usbmodem7010A00011943"
PORT_OTHER = "/dev/cu.usbmodemAAAA3"
VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"

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


def test_list_candidate_ports_globs_the_xpressnet_pattern_and_sorts(monkeypatch):
    """PORT_GLOB picks the CDC interface railctl talks to. Get the pattern wrong
    - for example "*1", the silent LocoNet interface - and this is the only test
    that would notice. sorted() is what decides which port AmbiguousPort names
    first in its hint, so an unsorted glob.glob() result must come back sorted.
    """
    seen_patterns = []
    unsorted = [PORT_OTHER, PORT_43]

    def fake_glob(pattern):
        seen_patterns.append(pattern)
        return unsorted

    monkeypatch.setattr(transport_module.glob, "glob", fake_glob)

    result = list_candidate_ports()

    assert seen_patterns == ["/dev/cu.usbmodem*3"]
    assert result == sorted(unsorted)


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
