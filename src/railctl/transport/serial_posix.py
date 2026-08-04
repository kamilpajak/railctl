# src/railctl/transport/serial_posix.py
"""The only module in railctl that owns a file descriptor. No protocol logic here.

No pyserial: its POSIX backend is os.open + termios.tcsetattr + select, and
57600 8-N-1 is a POSIX-standard rate whose Darwin constant is the literal value
(termios.B57600 == 57600). The device is USB CDC-ACM, so the rate is forwarded
as SET_LINE_CODING to a fixed-rate virtual UART and is essentially cosmetic; we
set it because Lenz 23151 section 1.1 specifies it. Portability note: on Linux
the speed constants are small indices, so a Linux port needs a lookup table.
This is the only platform assumption in railctl.

Flags that are load-bearing: /dev/cu.* never /dev/tty.* (call-out, does not
block on DCD); O_NOCTTY (a line BREAK would otherwise deliver SIGINT); lflag 0
(ISIG turns an incoming 0x03 into SIGINT, ICANON waits for 0x0A); iflag 0
(PARMRK duplicates 0xFF, ISTRIP clears bit 7, ICRNL/INLCR rewrite 0x0D/0x0A -
and our payloads legitimately contain all of those); cflag CS8|CREAD|CLOCAL (no
CRTSCTS, no HUPCL so closing does not reset the adapter).
"""

from __future__ import annotations

import os
import select
import termios
from dataclasses import dataclass

from railctl.errors import PortBusy, PortConfigError, PortNotFound, PortNotOpen, TransportError

BAUDRATE = 57600
# Suggested read size for callers; Link uses its own private _READ_CHUNK, a
# second constant with the same value that can drift without anything noticing.
READ_CHUNK = 256
WRITE_SELECT_TIMEOUT = 1.0
# Link quotes this whenever a handshake or an exchange fails on a serial port.
# It lives here and not in link.py because the future Z21 LAN transport must be
# a pure addition: spec line 583 allows no edit to link.py.
CDC_INDEX_HINT = (
    "on this hardware the CDC interface index picks the bus: 1 is LocoNet, "
    "3 is XpressNet, 5 is the YD.Control telemetry stream"
)


@dataclass(frozen=True, slots=True)
class SerialConfig:
    port: str
    baudrate: int = BAUDRATE


class SerialTransport:
    def __init__(self, config: SerialConfig) -> None:
        self._config = config
        self._fd: int | None = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    @property
    def description(self) -> str:
        return f"xpressnet serial {self._config.port}"

    @property
    def identity(self) -> str:
        return self._config.port

    @property
    def diagnostic_hint(self) -> str:
        return CDC_INDEX_HINT

    def open(self) -> None:
        try:
            fd = os.open(self._config.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise PortNotFound(f"{self._config.port} does not exist") from exc
        except OSError as exc:
            raise PortBusy(f"cannot open {self._config.port}: {exc.strerror}") from exc
        try:
            cc = list(termios.tcgetattr(fd)[6])
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            rate = self._config.baudrate
            cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
            termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, rate, rate, cc])
            if (termios.tcgetattr(fd)[2] & termios.CSIZE) != termios.CS8:
                raise PortConfigError(f"{self._config.port} silently rejected 8-N-1")
            termios.tcflush(fd, termios.TCIOFLUSH)
        except termios.error as exc:
            os.close(fd)
            raise PortConfigError(f"{self._config.port} is not a serial device: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SerialTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _fileno(self) -> int:
        if self._fd is None:
            raise PortNotOpen(f"{self._config.port} is not open")
        return self._fd

    def flush_input(self) -> None:
        termios.tcflush(self._fileno(), termios.TCIFLUSH)

    def write(self, data: bytes) -> None:
        fd = self._fileno()
        sent = 0
        while sent < len(data):
            try:
                sent += os.write(fd, data[sent:])
            except BlockingIOError:
                if not select.select([], [fd], [], WRITE_SELECT_TIMEOUT)[1]:
                    raise TransportError(f"timed out writing to {self._config.port}") from None
            except OSError as exc:
                raise TransportError(f"write to {self._config.port} failed: {exc}") from exc

    def read(self, max_bytes: int, timeout: float) -> bytes:
        fd = self._fileno()
        if not select.select([fd], [], [], max(0.0, timeout))[0]:
            return b""
        try:
            data = os.read(fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise TransportError(f"read from {self._config.port} failed: {exc}") from exc
        if not data:
            # select() said readable, then os.read() returned nothing: that is
            # end of file, not an idle port. Returning b"" here would read as
            # silence one layer up and the real fault would only surface on the
            # next write.
            raise TransportError(f"{self._config.port} closed while reading")
        return data
