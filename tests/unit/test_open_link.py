# tests/unit/test_open_link.py
from __future__ import annotations

import pytest

from railctl.errors import AmbiguousPort, PortNotFound, TransportError, UnsupportedFeatureError
from railctl.transport import find_xpressnet_port, transport_for
from railctl.transport.serial_posix import BAUDRATE, CDC_INDEX_HINT, SerialTransport

PORT_43 = "/dev/cu.usbmodem7010A00011943"
PORT_OTHER = "/dev/cu.usbmodemAAAA3"

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
