"""Hardware I/O for the probe. This is the only module that opens a file descriptor."""

from __future__ import annotations

import glob
import os
import select
import termios
import time
from typing import Protocol

from tools.probe.frames import Frame, build, split_frames

BAUD = termios.B57600
PORT_GLOB = "/dev/cu.usbmodem7010*"
WRITE_TIMEOUT = 1.0  # seconds to write a command frame


class Link(Protocol):
    def exchange(self, payload: bytes, *, window: float) -> list[Frame]: ...
    def collect(self, *, window: float) -> list[Frame]: ...


def discover_ports() -> list[str]:
    """All candidate YD7010 CDC ports, in a stable order."""
    return sorted(glob.glob(PORT_GLOB))


class SerialLink:
    """Raw termios serial link. USB CDC ignores the baud rate, but we set it anyway
    because the DR5000/YD7010 convention is 57600 8-N-1 with no flow control."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: int | None = None
        self._buffer = b""

    def open(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        cc = list(attrs[6])
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(
            fd,
            termios.TCSANOW,
            [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0, BAUD, BAUD, cc],
        )
        termios.tcflush(fd, termios.TCIOFLUSH)
        self._fd = fd
        self._buffer = b""

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SerialLink:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError(f"link to {self.path} is not open")
        return self._fd

    def exchange(self, payload: bytes, *, window: float) -> list[Frame]:
        fd = self._require_fd()
        termios.tcflush(fd, termios.TCIOFLUSH)
        self._buffer = b""
        frame = build(payload)
        written = 0
        deadline = time.monotonic() + WRITE_TIMEOUT
        while written < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out writing frame to {self.path}")
            try:
                n = os.write(fd, frame[written:])
                if n == 0:
                    raise OSError(f"os.write returned 0 for {self.path}")
                written += n
            except BlockingIOError:
                # Kernel send buffer full; wait for writable before retrying
                select.select([], [fd], [], max(0.0, min(0.01, remaining)))
        return self.collect(window=window)

    def collect(self, *, window: float) -> list[Frame]:
        fd = self._require_fd()
        deadline = time.monotonic() + window
        collected: list[Frame] = []
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([fd], [], [], max(0.0, min(0.2, remaining)))
            if not readable:
                continue
            try:
                chunk = os.read(fd, 512)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            self._buffer += chunk
            frames, self._buffer = split_frames(self._buffer)
            collected.extend(frames)
        return collected
