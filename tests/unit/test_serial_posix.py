# tests/unit/test_serial_posix.py
from __future__ import annotations

import errno
import os

import pytest

from railctl.errors import PortConfigError, PortPermissionDenied, TransportError
from railctl.transport import serial_posix
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


def test_a_permission_denial_says_so_instead_of_blaming_another_process(monkeypatch):
    """The ordinary first run on Linux: the CDC node is owned by dialout and the
    account is not in the group yet.

    It used to be reported in the words of the one port failure it is not -
    PortBusy, "another process holds it", whose published meaning is that
    retrying may work. It is the one failure a retry can never clear, so an agent
    reading retryable: true would try for ever and a human would go looking for a
    process that was never there.
    """

    def deny(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(serial_posix.os, "open", deny)
    transport = SerialTransport(SerialConfig("/dev/does-not-matter"))

    with pytest.raises(PortPermissionDenied) as caught:
        transport.open()

    assert "refused this process access" in str(caught.value)
    # NOT "the device exists": EACCES also comes from an unsearchable parent
    # directory, where the node may not be there at all.
    assert "exists" not in str(caught.value)
    assert "dialout" in caught.value.hint
    assert "retrying will not clear it" in caught.value.hint
    assert transport.is_open is False


def test_a_directory_is_not_a_serial_device_and_is_not_retryable(tmp_path):
    """Found by exploratory testing: pointing the tool at a directory reported
    `port_busy`, whose published meaning is "transient, retry may work". A
    directory will not become a serial port on the second attempt.

    /dev/null already reported this correctly - through termios rather than
    open() - so the right class existed and EISDIR simply never reached it. Both
    paths now say the same thing in the same words.

    tmp_path rather than a hardcoded /tmp: ruff's S108 fires on the literal, and
    the fixture is a real directory, which is the only property this needs.
    """
    transport = SerialTransport(SerialConfig(str(tmp_path)))

    with pytest.raises(PortConfigError, match="is not a serial device"):
        transport.open()

    assert transport.is_open is False


def test_the_dev_directory_itself_is_reported_the_same_way():
    """The case an operator actually reaches by fumbling a path: /dev instead of
    a node under it. Same errno, same verdict, and not a temp path - so this one
    also proves the branch is not an artefact of the fixture.
    """
    transport = SerialTransport(SerialConfig("/dev"))

    with pytest.raises(PortConfigError, match="is not a serial device"):
        transport.open()


def test_a_path_component_that_is_not_a_directory_is_reported_the_same_way(tmp_path):
    """ENOTDIR, the other structural way a path cannot be a device: a regular
    file used as a directory. Same verdict, because it is the same kind of fact.
    """
    regular = tmp_path / "file"
    regular.write_text("not a directory", encoding="utf-8")
    transport = SerialTransport(SerialConfig(str(regular / "port")))

    with pytest.raises(PortConfigError, match="is not a serial device"):
        transport.open()


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
