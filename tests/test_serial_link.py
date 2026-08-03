import pytest

from tools.probe.link import SerialLink


def test_serial_link_exchange_retries_on_blocking_io_error(monkeypatch):
    """Verify that exchange retries os.write after BlockingIOError."""
    link = SerialLink("/dev/null")
    link._fd = 999  # Fake open fd

    # Mock os.write to fail once with BlockingIOError, then succeed
    write_calls = []

    def mock_write(fd, data):
        write_calls.append((fd, data))
        if len(write_calls) == 1:
            raise BlockingIOError("kernel send buffer full")
        # On second call, return the number of bytes written
        return len(data)

    def mock_select(rlist, wlist, xlist, timeout):
        # Simulate select returning writable
        return ([], [999] if wlist else [], [])

    monkeypatch.setattr("os.write", mock_write)
    monkeypatch.setattr("select.select", mock_select)
    monkeypatch.setattr("termios.tcflush", lambda fd, flags: None)

    # This should not raise; it should retry after BlockingIOError
    link.exchange(b"\x21\x21", window=0.1)

    # Verify os.write was called twice (first failed, second succeeded)
    assert len(write_calls) == 2


def test_serial_link_exchange_raises_on_zero_write(monkeypatch):
    """Verify that os.write returning 0 is treated as an error."""
    link = SerialLink("/dev/null")
    link._fd = 999

    def mock_write(fd, data):
        return 0  # Zero bytes written; this should trigger an error

    monkeypatch.setattr("os.write", mock_write)
    monkeypatch.setattr("termios.tcflush", lambda fd, flags: None)

    with pytest.raises(OSError, match="returned 0"):
        link.exchange(b"\x21\x21", window=0.1)


def test_serial_link_exchange_raises_on_write_timeout(monkeypatch):
    """Verify that a persistent BlockingIOError eventually times out."""
    link = SerialLink("/dev/null")
    link._fd = 999

    def mock_write(fd, data):
        raise BlockingIOError("always blocked")

    def mock_select(rlist, wlist, xlist, timeout):
        # select says not writable; the write will keep failing
        return ([], [], [])

    monkeypatch.setattr("os.write", mock_write)
    monkeypatch.setattr("select.select", mock_select)
    monkeypatch.setattr("termios.tcflush", lambda fd, flags: None)

    with pytest.raises(TimeoutError, match="timed out writing"):
        link.exchange(b"\x21\x21", window=0.1)
