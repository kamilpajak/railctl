# src/railctl/transport/serial_posix.py
"""The only module in railctl that owns a file descriptor. No protocol logic here.

No pyserial: its POSIX backend is os.open + termios.tcsetattr + select, and
57600 8-N-1 is a POSIX-standard rate. The device is USB CDC-ACM, so the rate is
forwarded as SET_LINE_CODING to a fixed-rate virtual UART and is essentially
cosmetic; we set it because Lenz 23151 section 1.1 specifies it.

The rate goes through `getattr(termios, f"B{rate}")` rather than straight into
the termios list. On Darwin the constant IS the literal value (B57600 == 57600)
so the lookup changes nothing, but on Linux the speed constants are small
indices and passing 57600 makes tcsetattr fail with EINVAL. The literal was
first caught by CI, which runs on Linux and opens a real terminal through this
method in the pty test for read()'s end-of-file handling - it failed on every CI
run while passing locally. Since 2026-10-14 Linux is a supported platform rather
than only the CI host, so that lookup is production code on a real bench. A
capability the tests cannot reach on the machine that runs them is a capability
nobody is measuring.

Flags that are load-bearing: on macOS /dev/cu.* never /dev/tty.* (call-out, does
not block on DCD); Linux has no call-out twin, so there we open the ttyACM node
a /dev/serial/by-id symlink points at and O_NONBLOCK plus CLOCAL give the same
guarantee; O_NOCTTY (a line BREAK would otherwise deliver SIGINT); lflag 0
(ISIG turns an incoming 0x03 into SIGINT, ICANON waits for 0x0A); iflag 0
(PARMRK duplicates 0xFF, ISTRIP clears bit 7, ICRNL/INLCR rewrite 0x0D/0x0A -
and our payloads legitimately contain all of those); cflag CS8|CREAD|CLOCAL (no
CRTSCTS, no HUPCL so closing does not reset the adapter).
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import select
import termios
from dataclasses import dataclass

from railctl.errors import (
    PortBusy,
    PortConfigError,
    PortNotFound,
    PortNotOpen,
    PortPermissionDenied,
    TransportError,
)

# Structural facts about the path, not conditions that clear on a retry: a
# directory, a non-directory used as one, or a symlink chain too deep to follow.
# They say "this is not a serial device" exactly as a rejected tcgetattr does,
# and routing them to PortBusy published them as retryable.
_NOT_A_DEVICE_ERRNOS = frozenset({errno.EISDIR, errno.ENOTDIR, errno.ELOOP})

# What flock() reports when somebody else already holds the port. EWOULDBLOCK
# and EAGAIN are the same number on Linux and macOS; both are named because
# POSIX permits them to differ and a frozenset costs nothing.
_LOCK_HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})

BAUDRATE = 57600
# Suggested read size for callers; Link uses its own private _READ_CHUNK, a
# second constant with the same value that can drift without anything noticing.
READ_CHUNK = 256
WRITE_SELECT_TIMEOUT = 1.0
# Link quotes this whenever a handshake or an exchange fails on a serial port.
# It lives here and not in link.py because the future Z21 LAN transport must be
# a pure addition: spec line 583 allows no edit to link.py.
#
# It names the interface by its POSITION among the station's three CDC functions
# and then gives both numberings, because the two kernels number the same three
# differently: macOS after each function's data interface (1, 3, 5), Linux's
# /dev/serial/by-id after its control interface (if00, if02, if04). The previous
# wording said only "3 is XpressNet", which on Linux points at a node that
# cannot exist - "-if03" matches nothing - so this hint sent a Linux operator
# looking for hardware instead of at their own command. Measured 2026-10-14; see
# docs/probe-results.md, section "Linux enumerates the same three CDC functions
# under different numbers".
#
# The constant keeps its name: it is still the hint about which CDC function you
# are talking to, and tests/unit/test_open_link.py imports it.
CDC_INDEX_HINT = (
    "the station exposes three CDC functions and only the middle one speaks "
    "XpressNet: first LocoNet (silent), second XpressNet, third the YD.Control "
    "telemetry stream - macOS numbers them 1, 3, 5 and Linux /dev/serial/by-id "
    "names them -if00, -if02, -if04"
)


_log = logging.getLogger("railctl.transport")


@dataclass(frozen=True, slots=True)
class SerialConfig:
    port: str
    baudrate: int = BAUDRATE


class SerialTransport:
    def __init__(self, config: SerialConfig) -> None:
        self._config = config
        self._fd: int | None = None
        self._lock_held = False

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    @property
    def lock_held(self) -> bool:
        """Whether this open port is actually protected against a second opener.

        False means the device refused advisory locking and the port was
        opened anyway - a deliberate choice, see open() - so a second process
        CAN still take it and the #68 misdiagnosis is back in play. It is a
        field rather than only a log line because a guarantee that silently
        evaporates is exactly what this project forbids: whoever must not act
        on a guess reads this, not the fact that open() returned.
        """
        return self._lock_held

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
        if self._fd is not None:
            # NOT PortBusy, which is what this used to raise. A BSD flock
            # belongs to the open file description, so a second os.open makes
            # a new one that conflicts with the fd this object already holds:
            # re-opening reported "another process holds it" about ITSELF and
            # sent the reader hunting for a second railctl or a JMRI session
            # that was never running. Measured 2026-11-11 - the same
            # misdiagnosis #68 exists to remove, inverted.
            #
            # Raising rather than returning quietly: a caller that re-opens
            # usually wants a fresh connection, and silently keeping the old
            # fd would hand it a stale one after a replug. TransportError is
            # not in RETRYABLE_CODES, which is right - no retry fixes this.
            raise TransportError(
                f"{self._config.port} is already open in this process - "
                f"close() it before opening it again"
            )
        try:
            fd = os.open(self._config.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise PortNotFound(f"{self._config.port} does not exist") from exc
        except PermissionError as exc:
            # Split out of the blanket OSError below because the two share an
            # errno class and nothing else. On Linux the CDC node is owned by
            # the dialout group, so this is the ordinary first run of a new
            # account - and the blanket branch reported it under port_busy,
            # whose published meaning is "another process holds it, retry may
            # work". No retry clears a group membership, and an agent reading
            # retryable: true will try for ever.
            # "refused access", not "the device exists": EACCES also comes from
            # a parent directory this process cannot search, in which case the
            # node may not be there at all and `ls -l` on it fails the same way.
            raise PortPermissionDenied(
                f"cannot open {self._config.port}: {exc.strerror} - the OS "
                f"refused this process access to it",
                hint=(
                    "this is not another process holding the port and retrying "
                    "will not clear it; check the owner and group with "
                    f"ls -l {self._config.port}, then on Linux "
                    "sudo usermod -aG dialout $USER and log in again"
                ),
            ) from exc
        except OSError as exc:
            if exc.errno in _NOT_A_DEVICE_ERRNOS:
                # Same wording and same class as the tcgetattr failure below,
                # because it is the same fact about the path. Found by pointing
                # the tool at a directory: EISDIR was landing in the blanket
                # branch under port_busy, which publishes "transient, retry may
                # work" - and /tmp will not become a serial port on the second
                # attempt. /dev/null already reported this correctly, through
                # termios rather than open(), so the right bucket existed and
                # these errnos simply never reached it.
                raise PortConfigError(
                    f"{self._config.port} is not a serial device: {exc.strerror}"
                ) from exc
            raise PortBusy(f"cannot open {self._config.port}: {exc.strerror}") from exc
        locked = True
        try:
            # os.open on a CDC-ACM node is NOT exclusive, so without this two
            # railctl processes both succeed, both write their telegrams, and
            # either one can consume the reply addressed to the other. Measured
            # 2026-10-15: the second process reported link_timeout, exit 7,
            # retryable: true - "the station did not answer" - when the station
            # had answered and the other railctl had read it (#68).
            #
            # flock(), NOT lockf(). This is the one choice here that cannot be
            # made by taste. JMRI drives the same CDC nodes through jSerialComm,
            # whose POSIX backend takes flock(LOCK_EX | LOCK_NB) on open and
            # releases it on close, and which JMRI never disables. BSD flock
            # locks and fcntl record locks are separate, non-interoperating
            # spaces on Linux - measured: lockf() succeeds while a flock is
            # held - so lockf() here would still fix railctl against railctl
            # while silently losing mutual exclusion with JMRI. TIOCEXCL was
            # considered and rejected: it is kernel-wide, so it also refuses
            # passive readers that ask for no lock, and it buys nothing against
            # JMRI that flock does not already give.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _LOCK_HELD_ERRNOS:
                os.close(fd)
                raise PortBusy(
                    f"cannot open {self._config.port}: another process holds it",
                    hint=(
                        "this is the port being held, not a silent station: look "
                        "for another railctl still running, such as a monitor in "
                        "a second terminal, or for JMRI, which holds the same "
                        "lock while its connection is open. It clears when that "
                        "process exits, so retrying is the right response"
                    ),
                ) from exc
            # flock is refused outright on some filesystems and some exotic
            # character devices. Carry on unlocked rather than refuse to open:
            # the lock makes a misdiagnosis impossible, it is not a prerequisite
            # for talking to the station, and failing here would turn a working
            # setup into a dead one over a diagnostic.
            #
            # But SAY SO, and record it. Without this the guarantee vanished
            # in silence: an unlocked open was indistinguishable from a
            # locked one, so the #68 misdiagnosis could come back with
            # nothing anywhere saying the instrument had never been armed.
            # railctl's root logger sits at WARNING by default (cli/deps.py),
            # so this reaches the operator without -v.
            locked = False
            _log.warning(
                "%s refused advisory locking (%s); opening it unlocked, so a second "
                "process taking this port will not be detected and may look like a silent "
                "station",
                self._config.port,
                exc.strerror or exc,
            )
        # No LOCK_UN anywhere: the lock belongs to the open file description and
        # the kernel drops it when the fd closes, including on a crash. Releasing
        # it by hand would only create a window in which close() has not yet run.
        try:
            cc = list(termios.tcgetattr(fd)[6])
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            rate = getattr(termios, f"B{self._config.baudrate}", self._config.baudrate)
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
        self._lock_held = locked

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._lock_held = False

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
            # next write. Needs hardware validation: some macOS USB serial
            # drivers have been reported to briefly mark the descriptor
            # readable with zero bytes available around the device going to
            # sleep or waking up. If this ever misfires on a port that is
            # actually still alive, that is the scenario to look for first.
            raise TransportError(f"{self._config.port} closed while reading")
        return data
