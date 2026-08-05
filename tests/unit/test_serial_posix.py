# tests/unit/test_serial_posix.py
from __future__ import annotations

import os

import pytest

from railctl.errors import PortConfigError, TransportError
from railctl.transport.serial_posix import SerialConfig, SerialTransport


def test_open_on_a_non_serial_device_raises_port_config_error():
    """termios.error must not escape open() untyped. /dev/null exists, is not a
    FileNotFoundError and not a permission error, but termios.tcgetattr() on it
    raises termios.error - which is not a RailctlError and would otherwise print
    a bare traceback instead of a message plus hint.
    """
    transport = SerialTransport(SerialConfig("/dev/null"))

    with pytest.raises(PortConfigError, match="is not a serial device"):
        transport.open()

    assert transport.is_open is False


def test_read_after_the_peer_hangs_up_raises_instead_of_looking_idle():
    """A cable pulled between the write and the reply must not read as silence.

    On a pty, closing the master makes the slave end of file: select() reports
    readable and os.read() returns b"". Returning b"" here is indistinguishable
    from an idle port, so a LinkTimeout ("silence") would fire instead of the
    real fault, and it would only surface on the next write.
    """
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)

    transport = SerialTransport(SerialConfig(slave_path))
    transport.open()
    try:
        os.close(master_fd)
        with pytest.raises(TransportError, match="closed while reading"):
            transport.read(64, 1.0)
    finally:
        transport.close()
