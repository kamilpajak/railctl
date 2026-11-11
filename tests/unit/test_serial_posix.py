# tests/unit/test_serial_posix.py
from __future__ import annotations

import errno
import logging
import os

import pytest

from railctl.errors import PortBusy, PortConfigError, PortPermissionDenied, TransportError
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


def test_a_second_open_of_the_same_port_is_refused_instead_of_sharing_it():
    """Two opens of one port must not both succeed.

    Measured 2026-10-15 on the bench: they did. `os.open` on a CDC-ACM node is
    not exclusive, so a second railctl opened the port the first one was using,
    wrote its telegram, and had its reply consumed by the other process. It
    then reported `link_timeout` - "the station did not answer" - about a
    station that had answered, and `retryable: true` made the real cause
    (a monitor left running in another terminal) never surface.

    Both transports here live in ONE process, and that is deliberate rather
    than a convenience: a BSD flock belongs to the open file description, so
    two fds in one process exclude each other, while an fcntl record lock
    belongs to the process and would not. This test is therefore red if the
    implementation reaches for `fcntl.lockf` instead of `fcntl.flock` - which
    is the choice that would silently lose mutual exclusion with JMRI, since
    jSerialComm takes a BSD flock and the two lock spaces do not interoperate.
    """
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)

    held = SerialTransport(SerialConfig(slave_path))
    held.open()
    try:
        # The positive half of the same fact: this open DID take the lock.
        # Without it, a build where flock silently never fired would pass the
        # refuses-locking test and fail nothing here.
        assert held.lock_held is True
        intruder = SerialTransport(SerialConfig(slave_path))
        with pytest.raises(PortBusy, match="another process holds it"):
            intruder.open()
    finally:
        held.close()
        os.close(master_fd)


def test_the_port_is_released_by_close_so_the_next_process_gets_it():
    """The refusal must be transient, which is what `port_busy` publishes.

    `port_busy` is in RETRYABLE_CODES, so the claim "retry may work" has to be
    true. It is, because the lock belongs to the open file description and the
    kernel drops it when the fd closes - which is also why a crashed railctl
    cannot leave the port locked against the next one. Nothing calls LOCK_UN,
    and this is the test that would notice if that stopped being enough.

    Stated plainly, because this repo does not keep guards that measure
    nothing: no mutation of the current implementation makes this test red -
    all four tried, including removing the lock and swapping flock for lockf,
    were killed by the other tests and not by this one. It asserts a published
    contract (`port_busy` is in RETRYABLE_CODES, so "retry may work" has to be
    true end to end) rather than a mechanism, and it would catch a future
    design that keyed the lock on the port path instead of the fd. That is its
    whole value; it is not evidence that the lock works.
    """
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)

    first = SerialTransport(SerialConfig(slave_path))
    first.open()
    first.close()

    second = SerialTransport(SerialConfig(slave_path))
    second.open()
    try:
        assert second.is_open
    finally:
        second.close()
        os.close(master_fd)


def test_reopening_without_closing_says_so_instead_of_blaming_another_process():
    """A caller that re-opens an open transport must not be told a second
    process holds the port.

    Measured 2026-11-11 against the flock change before this guard existed:
    the first open took fd 4 and the second raised `PortBusy: another process
    holds it` - about itself. A BSD flock belongs to the open file description,
    so the second `os.open` creates a new one and it conflicts with the fd this
    object is already holding. That is the #68 misdiagnosis inverted: it sends
    the reader hunting for a second railctl, or a JMRI session, that was never
    running.

    `port_busy` is also in RETRYABLE_CODES, so an agent reading
    `retryable: true` would have retried a condition no retry can clear - the
    same trap PortPermissionDenied was split out of the blanket branch to
    avoid.

    Not reachable from today's CLI, where `link.open()` has one call site. But
    `Link.open()` is written for a transport that opened, closed and re-opens,
    so the lifecycle is live and the next caller added on that path lands here.
    """
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)

    transport = SerialTransport(SerialConfig(slave_path))
    transport.open()
    try:
        with pytest.raises(TransportError, match="already open") as caught:
            transport.open()
        # PortBusy IS a TransportError, so matching the base class is not
        # enough: this is the specific wrong answer the guard exists to stop.
        assert not isinstance(caught.value, PortBusy)
        # And the refusal must not have disturbed the connection the caller is
        # still using - it closes the fd it just opened, not the live one.
        assert transport.is_open
        assert transport.lock_held is True
    finally:
        transport.close()
        os.close(master_fd)


def test_a_port_that_refuses_locking_is_still_opened(caplog):
    """A device that rejects flock must not become unusable.

    Some filesystems and some character devices refuse flock outright. The lock
    is a diagnostic guarantee, not a prerequisite for talking to the station, so
    refusing to open would turn a working setup into a dead one over it. ENOLCK
    stands in for that class of device here.
    """
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)

    def refuse_to_lock(fd, operation):
        raise OSError(errno.ENOLCK, "no locks available")

    transport = SerialTransport(SerialConfig(slave_path))
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(serial_posix.fcntl, "flock", refuse_to_lock)
            with caplog.at_level(logging.WARNING, logger="railctl.transport"):
                transport.open()
        assert transport.is_open
        # Opening is right. Opening SILENTLY is not: with no trace, an
        # unlocked port is indistinguishable from a locked one, so the #68
        # misdiagnosis can come back with nothing anywhere recording that
        # the instrument was never armed. Both channels are asserted - the
        # field, for code that must not act on a guess, and the warning,
        # which reaches the operator at the default log level.
        assert transport.lock_held is False
        assert "unlocked" in caplog.text
        assert slave_path in caplog.text
    finally:
        transport.close()
        os.close(master_fd)
