# railctl M1 — Hardware Capability Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tested, standalone probe tool that measures exactly what the YaMoRC YD7010 supports over its USB XpressNet port, and record the results so every later milestone can stop guessing.

**Architecture:** A small `tools/probe` package, deliberately separate from the future `railctl` package. Pure frame-building and reply-parsing functions are unit tested against golden byte vectors. All hardware I/O sits behind a one-method `Link` protocol, so every capability check is tested in-process against a `FakeLink` that replays scripted exchanges. Only `SerialLink` touches `termios`, and it is the one module without unit tests.

**Tech Stack:** Python 3.11+, stdlib only at runtime (`os`, `termios`, `select`), pytest + ruff for development. No pyserial.

## Global Constraints

- Python 3.11+ (`requires-python = ">=3.11"`). Development happens on Python 3.14.
- Runtime dependencies: **none**. Dev dependencies: `pytest`, `ruff` only.
- Platform: macOS (Darwin) only. `termios` usage is not portable and is not required to be.
- Every command sent to the YD7010 XpressNet port MUST be prefixed with `FF FE`. Without it the port is silent.
- The `FF FE` / `FF FD` prefix bytes are NEVER included in the XOR checksum.
- XpressNet telegram length = `(header & 0x0F) + 2` bytes (header + N data + XOR).
- CV numbers are 1-based everywhere in this tool's API. Conversion to the zero-based wire value happens in exactly one function.
- The XpressNet port is `/dev/cu.usbmodem7010A00011943` on the reference unit, but the tool MUST auto-detect rather than hardcode it.
- LI-USB is strictly request/response: never send a second command before the first is answered or has timed out.
- The probe MUST NOT write any decoder CV. Reads and non-mutating commands only.
- Commit style: Conventional Commits (`type(scope): description`). Never mention AI assistance.
- Git identity in this repo is already configured as `Kamil Pająk <kamilpajak@users.noreply.github.com>`.

---

## Scope note — why this plan stops at M1

The approved spec defines milestones M1–M11. They are not one plan. M1 is the only milestone whose output changes the others: R1, R2, R4 and R5 decide which CV opcodes exist, whether POM works at all, and whether function shadow state is needed. Writing bite-sized TDD tasks for M5+ before M1 runs would encode guesses as test assertions.

Planned decomposition:

| Plan | Milestones | Deliverable |
|---|---|---|
| **1 (this one)** | M1 | Tested probe tool + `docs/probe-results.md` filled in from real hardware |
| 2 | M2–M4 | Package scaffolding, X-Bus codec with golden vectors, transport/envelope/link |
| 3 | M5–M6 | Station facade, CLI core, `doctor`, driving commands |
| 4 | M7–M8 | ZIMO catalog, `cv read` / `cv write` |
| 5 | M9–M11 | Backup, restore, diff, sweep, 0.1.0 release |

Plan 2 can be written immediately after this one — M2–M4 depend on the protocol, not on probe results. Plans 3–5 should wait for `docs/probe-results.md`.

---

## File Structure

```
pyproject.toml                    dev tooling only; no runtime deps, no package build
tools/__init__.py                 empty
tools/probe/__init__.py           empty
tools/probe/frames.py             pure: XOR, LI-USB wrap, frame splitting with resync
tools/probe/replies.py            pure: telegram -> typed reply object
tools/probe/link.py               Link protocol + SerialLink (termios) + port discovery
tools/probe/fake.py               FakeLink: scripted exchanges, used by tests AND by --dry-run
tools/probe/checks.py             R1, R2, R4, R5 and the secondary checks
tools/probe/report.py             CapabilityReport -> markdown + JSON
tools/probe/__main__.py           entry point: python -m tools.probe
tests/test_frames.py
tests/test_replies.py
tests/test_checks.py
tests/test_report.py
docs/probe-results.md             written by the hardware run in Task 9
```

Responsibilities are split so the untestable part is as small as possible: `link.py` is the only module that opens a file descriptor, and it contains no protocol logic.

---

### Task 1: Dev tooling and the frame layer

**Files:**
- Create: `pyproject.toml`
- Create: `tools/__init__.py`, `tools/probe/__init__.py`
- Create: `tools/probe/frames.py`
- Test: `tests/test_frames.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LI_COMMAND: bytes` = `b"\xff\xfe"`, `LI_BROADCAST: bytes` = `b"\xff\xfd"`
  - `xor(payload: bytes) -> int`
  - `build(payload: bytes) -> bytes` — payload is header+data without XOR; returns `FF FE` + payload + XOR
  - `telegram_length(header: int) -> int`
  - `@dataclass Frame(prefix: bytes, telegram: bytes)` with property `solicited: bool`
  - `split_frames(buffer: bytes) -> tuple[list[Frame], bytes]` — returns complete frames and the unconsumed remainder

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_frames.py
import pytest

from tools.probe.frames import (
    LI_BROADCAST,
    LI_COMMAND,
    Frame,
    build,
    split_frames,
    telegram_length,
    xor,
)


def test_xor_of_track_power_on_payload():
    assert xor(b"\x21\x81") == 0xA0


def test_xor_of_empty_payload_is_zero():
    assert xor(b"") == 0


def test_build_prefixes_ff_fe_and_appends_checksum():
    assert build(b"\x21\x81") == b"\xff\xfe\x21\x81\xa0"


def test_build_version_request_matches_the_bytes_measured_on_hardware():
    assert build(b"\x21\x21") == b"\xff\xfe\x21\x21\x00"


def test_telegram_length_is_low_nibble_plus_two():
    assert telegram_length(0x21) == 3
    assert telegram_length(0x62) == 4
    assert telegram_length(0x63) == 5
    assert telegram_length(0xE6) == 8


def test_split_frames_returns_one_frame_and_no_remainder():
    frames, rest = split_frames(b"\xff\xfe\x63\x21\x40\x12\x10")
    assert rest == b""
    assert len(frames) == 1
    assert frames[0].telegram == b"\x63\x21\x40\x12"
    assert frames[0].solicited is True


def test_split_frames_marks_ff_fd_as_unsolicited():
    frames, _ = split_frames(b"\xff\xfd\x61\x00\x61")
    assert frames[0].solicited is False


def test_split_frames_keeps_a_partial_frame_as_remainder():
    frames, rest = split_frames(b"\xff\xfe\x63\x21\x40")
    assert frames == []
    assert rest == b"\xff\xfe\x63\x21\x40"


def test_split_frames_handles_two_concatenated_frames():
    frames, rest = split_frames(b"\xff\xfe\x61\x01\x60\xff\xfd\x61\x00\x61")
    assert rest == b""
    assert [f.telegram for f in frames] == [b"\x61\x01", b"\x61\x00"]


def test_split_frames_resyncs_past_leading_garbage():
    frames, rest = split_frames(b"hello\xff\xfe\x21\x81\xa0")
    assert rest == b""
    assert [f.telegram for f in frames] == [b"\x21\x81"]


def test_split_frames_drops_a_frame_with_a_bad_checksum():
    frames, rest = split_frames(b"\xff\xfe\x21\x81\x00\xff\xfe\x21\x81\xa0")
    assert [f.telegram for f in frames] == [b"\x21\x81"]
    assert rest == b""


def test_frame_solicited_reflects_the_prefix():
    assert Frame(LI_COMMAND, b"\x61\x01").solicited is True
    assert Frame(LI_BROADCAST, b"\x61\x01").solicited is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_frames.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools'`

- [ ] **Step 3: Create the dev tooling**

```toml
# pyproject.toml
[project]
name = "railctl-probe"
version = "0.0.0"
description = "Hardware capability probe for the YaMoRC YD7010 (railctl milestone M1)"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "ruff>=0.4.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

```python
# tools/__init__.py
```

```python
# tools/probe/__init__.py
```

- [ ] **Step 4: Implement the frame layer**

```python
# tools/probe/frames.py
"""LI-USB framing for the YD7010 XpressNet port.

Every command must carry the FF FE prefix; without it the port stays silent.
The prefix bytes are never part of the XOR checksum.
"""

from __future__ import annotations

from dataclasses import dataclass

LI_COMMAND = b"\xff\xfe"
LI_BROADCAST = b"\xff\xfd"
_PREFIXES = (LI_COMMAND, LI_BROADCAST)


def xor(payload: bytes) -> int:
    """XOR checksum over an X-Bus telegram body (header + data, no prefix)."""
    result = 0
    for byte in payload:
        result ^= byte
    return result


def build(payload: bytes) -> bytes:
    """Wrap header+data into a complete LI-USB command frame."""
    return LI_COMMAND + payload + bytes([xor(payload)])


def telegram_length(header: int) -> int:
    """Total telegram size: header + N data bytes + XOR, where N is the low nibble."""
    return (header & 0x0F) + 2


@dataclass(frozen=True)
class Frame:
    prefix: bytes
    telegram: bytes

    @property
    def solicited(self) -> bool:
        return self.prefix == LI_COMMAND


def split_frames(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Consume as many complete, checksum-valid frames as possible.

    Unrecognised bytes are skipped one at a time so the stream can resync after
    noise — for example after accidentally opening the YD.Control telemetry port.
    """
    frames: list[Frame] = []
    pos = 0
    while pos < len(buffer):
        prefix = buffer[pos : pos + 2]
        if len(prefix) < 2:
            break
        if prefix not in _PREFIXES:
            pos += 1
            continue
        if pos + 2 >= len(buffer):
            break
        header = buffer[pos + 2]
        size = telegram_length(header)
        end = pos + 2 + size
        if end > len(buffer):
            break
        telegram = buffer[pos + 2 : end]
        if xor(telegram[:-1]) == telegram[-1]:
            frames.append(Frame(prefix, telegram[:-1]))
            pos = end
        else:
            pos += 1
    return frames, buffer[pos:]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_frames.py -v`
Expected: PASS, 11 tests

- [ ] **Step 6: Check formatting and lint**

Run: `python -m ruff check . && python -m ruff format --check .`
Expected: no findings. Run `python -m ruff format .` first if it reports formatting.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tools/__init__.py tools/probe/__init__.py tools/probe/frames.py tests/test_frames.py
git commit -m "feat(probe): add LI-USB frame building and splitting"
```

---

### Task 2: Reply parsing

**Files:**
- Create: `tools/probe/replies.py`
- Test: `tests/test_replies.py`

**Interfaces:**
- Consumes: nothing from Task 1 at runtime; tests use raw telegrams.
- Produces:
  - `@dataclass Version(xpressnet_major: int, xpressnet_minor: int, command_station_id: int)`
  - `@dataclass Status(raw: int)` with properties `emergency_off`, `emergency_stop`, `auto_start_mode`, `service_mode`, `powering_up`, `ram_error`
  - `@dataclass CvValue(raw_cv: int, value: int, ident: int)` — `ident` is `0x14` (direct) or `0x10` (register/paged)
  - Singletons `ACK`, `READY`, `SHORT_CIRCUIT`, `NO_ACK`, `BUSY`, `UNSUPPORTED` as instances of frozen marker dataclasses
  - `@dataclass Unknown(telegram: bytes)`
  - `parse(telegram: bytes) -> Reply` where `Reply` is the union of the above

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replies.py
from tools.probe.replies import (
    ACK,
    BUSY,
    NO_ACK,
    READY,
    SHORT_CIRCUIT,
    UNSUPPORTED,
    CvValue,
    Status,
    Unknown,
    Version,
    parse,
)


def test_parses_the_version_reply_measured_on_the_yd7010():
    assert parse(b"\x63\x21\x40\x12") == Version(
        xpressnet_major=4, xpressnet_minor=0, command_station_id=0x12
    )


def test_parses_a_status_reply():
    assert parse(b"\x62\x22\x07") == Status(raw=0x07)


def test_status_decodes_bit_2_as_start_mode_not_short_circuit():
    status = Status(raw=0x07)
    assert status.emergency_off is True
    assert status.emergency_stop is True
    assert status.auto_start_mode is True
    assert status.service_mode is False


def test_status_service_mode_is_bit_3():
    assert Status(raw=0x08).service_mode is True


def test_parses_a_direct_cv_result():
    assert parse(b"\x63\x14\x07\x91") == CvValue(raw_cv=0x07, value=0x91, ident=0x14)


def test_parses_a_register_or_paged_result():
    assert parse(b"\x63\x10\x01\x03") == CvValue(raw_cv=0x01, value=0x03, ident=0x10)


def test_parses_the_generic_interface_acknowledgement():
    assert parse(b"\x01\x04") is ACK


def test_parses_the_programming_status_replies():
    assert parse(b"\x61\x11") is READY
    assert parse(b"\x61\x12") is SHORT_CIRCUIT
    assert parse(b"\x61\x13") is NO_ACK
    assert parse(b"\x61\x1f") is BUSY


def test_parses_instruction_not_supported():
    assert parse(b"\x61\x82") is UNSUPPORTED


def test_unrecognised_telegram_becomes_unknown():
    assert parse(b"\x55\xaa") == Unknown(telegram=b"\x55\xaa")


def test_empty_telegram_becomes_unknown():
    assert parse(b"") == Unknown(telegram=b"")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_replies.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.probe.replies'`

- [ ] **Step 3: Implement reply parsing**

```python
# tools/probe/replies.py
"""Typed views over XpressNet reply telegrams (prefix and XOR already stripped)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    xpressnet_major: int
    xpressnet_minor: int
    command_station_id: int


@dataclass(frozen=True)
class Status:
    raw: int

    # Bit meanings are the XpressNet ones (Lenz section 2.1.7), NOT the Z21 ones.
    # In particular bit 2 is start mode, and XpressNet defines no short-circuit bit.
    @property
    def emergency_off(self) -> bool:
        return bool(self.raw & 0x01)

    @property
    def emergency_stop(self) -> bool:
        return bool(self.raw & 0x02)

    @property
    def auto_start_mode(self) -> bool:
        """True = every loco resumes its last speed when the station powers up."""
        return bool(self.raw & 0x04)

    @property
    def service_mode(self) -> bool:
        return bool(self.raw & 0x08)

    @property
    def powering_up(self) -> bool:
        return bool(self.raw & 0x40)

    @property
    def ram_error(self) -> bool:
        return bool(self.raw & 0x80)


@dataclass(frozen=True)
class CvValue:
    raw_cv: int
    value: int
    ident: int


@dataclass(frozen=True)
class Marker:
    name: str


ACK = Marker("ack")
READY = Marker("ready")
SHORT_CIRCUIT = Marker("short_circuit")
NO_ACK = Marker("no_ack")
BUSY = Marker("busy")
UNSUPPORTED = Marker("unsupported")

_PROGRAMMING_MARKERS = {
    0x11: READY,
    0x12: SHORT_CIRCUIT,
    0x13: NO_ACK,
    0x1F: BUSY,
    0x82: UNSUPPORTED,
}


@dataclass(frozen=True)
class Unknown:
    telegram: bytes


Reply = Version | Status | CvValue | Marker | Unknown


def parse(telegram: bytes) -> Reply:
    if len(telegram) < 2:
        return Unknown(telegram=telegram)
    header, db0 = telegram[0], telegram[1]

    if header == 0x63 and db0 == 0x21 and len(telegram) >= 4:
        version = telegram[2]
        return Version(
            xpressnet_major=version >> 4,
            xpressnet_minor=version & 0x0F,
            command_station_id=telegram[3],
        )
    if header == 0x62 and db0 == 0x22 and len(telegram) >= 3:
        return Status(raw=telegram[2])
    if header == 0x63 and db0 in (0x10, 0x14) and len(telegram) >= 4:
        return CvValue(raw_cv=telegram[2], value=telegram[3], ident=db0)
    if header == 0x61 and db0 in _PROGRAMMING_MARKERS:
        return _PROGRAMMING_MARKERS[db0]
    if header == 0x01 and db0 == 0x04:
        return ACK
    return Unknown(telegram=telegram)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_replies.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add tools/probe/replies.py tests/test_replies.py
git commit -m "feat(probe): parse XpressNet reply telegrams into typed objects"
```

---

### Task 3: The Link seam — serial transport, port discovery, and a fake

**Files:**
- Create: `tools/probe/link.py`
- Create: `tools/probe/fake.py`
- Test: `tests/test_fake_link.py`

**Interfaces:**
- Consumes: `tools.probe.frames.build`, `split_frames`, `Frame`.
- Produces:
  - `class Link(Protocol)` with `exchange(payload: bytes, *, window: float) -> list[Frame]` and `collect(window: float) -> list[Frame]`
  - `class SerialLink` implementing it over `termios`, plus `open()` / `close()`
  - `def discover_ports() -> list[str]` — sorted `/dev/cu.usbmodem7010*`
  - `class FakeLink` with constructor `FakeLink(script: dict[bytes, list[bytes]], *, unsolicited: dict[bytes, list[bytes]] | None = None)` and attribute `sent: list[bytes]`

`exchange` sends one payload (wrapping it with `build`) and collects every frame that arrives inside `window` seconds. It returns all of them rather than the first, because a POM result may arrive as an unsolicited `FF FD` broadcast after the solicited acknowledgement.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fake_link.py
import pytest

from tools.probe.fake import FakeLink
from tools.probe.frames import LI_BROADCAST, LI_COMMAND


def test_fake_link_returns_the_scripted_reply():
    link = FakeLink({b"\x21\x21": [b"\xff\xfe\x63\x21\x40\x12\x10"]})
    frames = link.exchange(b"\x21\x21", window=0.1)
    assert [f.telegram for f in frames] == [b"\x63\x21\x40\x12"]
    assert frames[0].prefix == LI_COMMAND


def test_fake_link_records_what_was_sent_including_the_checksum():
    link = FakeLink({b"\x21\x21": []})
    link.exchange(b"\x21\x21", window=0.1)
    assert link.sent == [b"\xff\xfe\x21\x21\x00"]


def test_fake_link_returns_nothing_for_an_unscripted_payload():
    link = FakeLink({})
    assert link.exchange(b"\x21\x24", window=0.1) == []


def test_fake_link_can_deliver_an_unsolicited_broadcast_after_the_reply():
    link = FakeLink(
        {b"\x21\x10": [b"\xff\xfe\x01\x04\x05"]},
        unsolicited={b"\x21\x10": [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    frames = link.exchange(b"\x21\x10", window=0.1)
    assert [f.solicited for f in frames] == [True, False]
    assert frames[1].prefix == LI_BROADCAST


def test_fake_link_collect_drains_queued_unsolicited_frames():
    link = FakeLink({}, unsolicited={b"": [b"\xff\xfd\x61\x00\x61"]})
    assert [f.telegram for f in link.collect(window=0.1)] == [b"\x61\x00"]


def test_fake_link_rejects_a_second_send_before_the_first_is_collected():
    link = FakeLink({b"\x21\x21": []}, strict_request_response=True)
    link.begin(b"\x21\x21")
    with pytest.raises(RuntimeError, match="outstanding"):
        link.begin(b"\x21\x24")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_fake_link.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.probe.fake'`

- [ ] **Step 3: Implement the Link protocol and SerialLink**

```python
# tools/probe/link.py
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
        while written < len(frame):
            written += os.write(fd, frame[written:])
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
```

- [ ] **Step 4: Implement FakeLink**

```python
# tools/probe/fake.py
"""In-process Link replacement driven by a script of payload -> raw reply bytes."""

from __future__ import annotations

from tools.probe.frames import Frame, build, split_frames


class FakeLink:
    def __init__(
        self,
        script: dict[bytes, list[bytes]],
        *,
        unsolicited: dict[bytes, list[bytes]] | None = None,
        strict_request_response: bool = False,
    ) -> None:
        self.script = script
        self.unsolicited = unsolicited or {}
        self.strict = strict_request_response
        self.sent: list[bytes] = []
        self._outstanding: bytes | None = None
        self._pending = b"".join(self.unsolicited.get(b"", []))

    def begin(self, payload: bytes) -> None:
        if self.strict and self._outstanding is not None:
            raise RuntimeError(f"outstanding command {self._outstanding!r} not yet collected")
        self.sent.append(build(payload))
        self._outstanding = payload
        raw = b"".join(self.script.get(payload, []))
        raw += b"".join(self.unsolicited.get(payload, []))
        self._pending += raw

    def exchange(self, payload: bytes, *, window: float) -> list[Frame]:
        self.begin(payload)
        return self.collect(window=window)

    def collect(self, *, window: float) -> list[Frame]:
        frames, self._pending = split_frames(self._pending)
        self._outstanding = None
        return frames
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_fake_link.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add tools/probe/link.py tools/probe/fake.py tests/test_fake_link.py
git commit -m "feat(probe): add serial link, port discovery and a scripted fake link"
```

---

### Task 4: Command builders and CV address encoding

**Files:**
- Create: `tools/probe/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: nothing (returns payloads without prefix or XOR; `build` adds those).
- Produces:
  - `cv_wire(cv: int) -> int` — the zero-based wire value, `cv - 1`; raises `ValueError` outside 1..1024
  - `loco_address_bytes(address: int, *, threshold: int = 100) -> tuple[int, int]`
  - `version() -> bytes`, `status() -> bytes`, `service_result() -> bytes`
  - `pom_read(address: int, cv: int) -> bytes`
  - `service_direct_read(cv: int) -> bytes`
  - `service_ext_read(cv: int) -> bytes`
  - `z21_service_read(cv: int) -> bytes`
  - `function_group(address: int, group: int, bits: int) -> bytes`
  - `single_function(address: int, index: int, action: int) -> bytes`

Every expected byte string below is copied from the approved spec, and each XOR has been recomputed by hand.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commands.py
import pytest

from tools.probe.commands import (
    cv_wire,
    function_group,
    loco_address_bytes,
    pom_read,
    service_direct_read,
    service_ext_read,
    service_result,
    single_function,
    status,
    version,
    z21_service_read,
)
from tools.probe.frames import build


def test_cv_wire_is_zero_based():
    assert cv_wire(1) == 0
    assert cv_wire(8) == 7
    assert cv_wire(29) == 28
    assert cv_wire(256) == 255
    assert cv_wire(1024) == 1023


@pytest.mark.parametrize("bad", [0, -1, 1025])
def test_cv_wire_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        cv_wire(bad)


def test_short_address_is_sent_as_is():
    assert loco_address_bytes(3) == (0x00, 0x03)
    assert loco_address_bytes(99) == (0x00, 0x63)


def test_address_at_or_above_the_threshold_gets_the_c000_offset():
    assert loco_address_bytes(100) == (0xC0, 0x64)
    assert loco_address_bytes(1234) == (0xC4, 0xD2)


def test_threshold_is_configurable_for_the_z21_convention():
    assert loco_address_bytes(100, threshold=128) == (0x00, 0x64)
    assert loco_address_bytes(128, threshold=128) == (0xC0, 0x80)


def test_simple_system_commands():
    assert build(version()) == b"\xff\xfe\x21\x21\x00"
    assert build(status()) == b"\xff\xfe\x21\x24\x05"
    assert build(service_result()) == b"\xff\xfe\x21\x10\x31"


def test_pom_read_of_cv8_at_address_3_matches_the_spec():
    assert build(pom_read(3, 8)) == b"\xff\xfe\xe6\x30\x00\x03\xe4\x07\x00\x36"


def test_pom_read_puts_the_high_cv_bits_into_the_option_byte():
    # CV300 -> wire 299 = 0x12B -> MM = 1, LSB = 0x2B
    payload = pom_read(3, 300)
    assert payload[4] == 0xE5
    assert payload[5] == 0x2B


def test_service_direct_read_matches_the_spec():
    assert build(service_direct_read(1)) == b"\xff\xfe\x22\x15\x01\x36"
    assert build(service_direct_read(29)) == b"\xff\xfe\x22\x15\x1c\x2b"


def test_service_direct_read_refuses_cv_above_256():
    with pytest.raises(ValueError, match="256"):
        service_direct_read(257)


def test_service_direct_read_refuses_cv256_because_wire_zero_is_ambiguous():
    with pytest.raises(ValueError, match="ambiguous"):
        service_direct_read(256)


def test_extended_read_picks_the_right_band_opcode():
    assert build(service_ext_read(1)) == b"\xff\xfe\x22\x18\x01\x3b"
    assert build(service_ext_read(256)) == b"\xff\xfe\x22\x19\x00\x3b"
    assert build(service_ext_read(300)) == b"\xff\xfe\x22\x19\x2c\x17"


def test_z21_service_read_uses_16_bit_zero_based_addressing():
    assert build(z21_service_read(29)) == b"\xff\xfe\x23\x11\x00\x1c\x2e"


def test_function_group_probe_telegrams():
    assert build(function_group(3, 0x23, 0x00)) == b"\xff\xfe\xe4\x23\x00\x03\x00\xc4"
    assert build(function_group(3, 0x28, 0x00)) == b"\xff\xfe\xe4\x28\x00\x03\x00\xcf"


def test_single_function_off_for_f0_at_address_3():
    assert build(single_function(3, 0, action=0)) == b"\xff\xfe\xe4\xf8\x00\x03\x00\x1f"


def test_single_function_encodes_action_in_the_top_two_bits():
    assert single_function(3, 5, action=1)[4] == 0b01_000101


@pytest.mark.parametrize("bad", [-1, 29])
def test_single_function_rejects_an_out_of_range_index(bad):
    with pytest.raises(ValueError):
        single_function(3, bad, action=0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.probe.commands'`

- [ ] **Step 3: Implement the command builders**

```python
# tools/probe/commands.py
"""X-Bus payload builders (header + data, no LI prefix and no XOR).

CV numbers are 1-based on the way in. cv_wire() is the ONLY place the
zero-based conversion happens, so it cannot be applied twice.
"""

from __future__ import annotations

MAX_CV = 1024
XPRESSNET_LONG_ADDRESS_THRESHOLD = 100


def cv_wire(cv: int) -> int:
    if not 1 <= cv <= MAX_CV:
        raise ValueError(f"CV {cv} out of range 1..{MAX_CV}")
    return cv - 1


def loco_address_bytes(address: int, *, threshold: int = XPRESSNET_LONG_ADDRESS_THRESHOLD) -> tuple[int, int]:
    if not 1 <= address <= 9999:
        raise ValueError(f"loco address {address} out of range 1..9999")
    value = address + 0xC000 if address >= threshold else address
    return (value >> 8) & 0xFF, value & 0xFF


def version() -> bytes:
    return b"\x21\x21"


def status() -> bytes:
    return b"\x21\x24"


def service_result() -> bytes:
    return b"\x21\x10"


def pom_read(address: int, cv: int) -> bytes:
    wire = cv_wire(cv)
    high, low = loco_address_bytes(address)
    option = 0xE4 | ((wire >> 8) & 0x03)
    return bytes([0xE6, 0x30, high, low, option, wire & 0xFF, 0x00])


def service_direct_read(cv: int) -> bytes:
    """Legacy direct read. Only CV1..255 — wire value 0 is ambiguous between
    CV256 and CV1024 across the two Lenz documents, so it is refused."""
    wire = cv_wire(cv)
    if wire == 0xFF + 1 or cv > 256:
        raise ValueError(f"CV {cv} exceeds the 256 CV limit of the legacy direct read")
    if wire == 0:
        raise ValueError("wire value 0 is ambiguous in the legacy direct read; use the extended opcodes")
    if cv == 256:
        raise ValueError("CV256 encodes as wire 0, which is ambiguous; use the extended opcodes")
    return bytes([0x22, 0x15, wire])


def service_ext_read(cv: int) -> bytes:
    wire = cv_wire(cv)
    band = wire >> 8
    if band > 3:
        raise ValueError(f"CV {cv} outside the extended opcode range 1..1024")
    return bytes([0x22, 0x18 + band, wire & 0xFF])


def z21_service_read(cv: int) -> bytes:
    wire = cv_wire(cv)
    return bytes([0x23, 0x11, (wire >> 8) & 0xFF, wire & 0xFF])


def function_group(address: int, group: int, bits: int) -> bytes:
    high, low = loco_address_bytes(address)
    return bytes([0xE4, group, high, low, bits & 0xFF])


def single_function(address: int, index: int, action: int) -> bytes:
    """action: 0 = off, 1 = on, 2 = toggle. index: F0..F28."""
    if not 0 <= index <= 28:
        raise ValueError(f"function index {index} out of range 0..28")
    if action not in (0, 1, 2):
        raise ValueError(f"action {action} must be 0 (off), 1 (on) or 2 (toggle)")
    high, low = loco_address_bytes(address)
    return bytes([0xE4, 0xF8, high, low, (action << 6) | index])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -v`
Expected: PASS. If `test_service_direct_read_refuses_cv_above_256` fails, simplify the guard in `service_direct_read` to `if cv > 255: raise ValueError(...)` plus the explicit CV256 branch — the first condition in the draft is redundant.

- [ ] **Step 5: Commit**

```bash
git add tools/probe/commands.py tests/test_commands.py
git commit -m "feat(probe): add X-Bus command builders with one-place CV conversion"
```

---

### Task 5: R1 — does a POM read result come back?

**Files:**
- Create: `tools/probe/checks.py`
- Test: `tests/test_checks_r1.py`

**Interfaces:**
- Consumes: `Link`, `commands.*`, `replies.parse`.
- Produces:
  - `@dataclass CheckResult(name: str, value: object | None, detail: str, frames: list[str])`
  - `def check_pom_read(link: Link, address: int, cv: int = 8, *, poll: bool) -> CheckResult`

`value` is a dict for R1: `{"pom_read": bool | None, "pom_result_channel": str, "pom_echo_zero_based": bool | None, "value": int | None}`. `None` always means "not established", never "false".

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checks_r1.py
from tools.probe.checks import check_pom_read
from tools.probe.fake import FakeLink

POM_CV8_AT_3 = b"\xe6\x30\x00\x03\xe4\x07\x00"
POLL = b"\x21\x10"


def test_result_arriving_as_a_broadcast_sets_channel_broadcast():
    link = FakeLink(
        {POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"]},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is True
    assert result.value["pom_result_channel"] == "broadcast"
    assert result.value["value"] == 0x91


def test_echo_of_the_zero_based_cv_sets_the_echo_flag():
    link = FakeLink(
        {POM_CV8_AT_3: []},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x07\x91\xe1"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_echo_zero_based"] is True


def test_echo_of_the_one_based_cv_clears_the_echo_flag():
    link = FakeLink(
        {POM_CV8_AT_3: []},
        unsolicited={POM_CV8_AT_3: [b"\xff\xfd\x63\x14\x08\x91\xee"]},
    )
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_echo_zero_based"] is False


def test_result_arriving_only_after_a_poll_sets_channel_poll():
    link = FakeLink(
        {
            POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"],
            POLL: [b"\xff\xfe\x63\x14\x07\x91\xe1"],
        }
    )
    result = check_pom_read(link, address=3, cv=8, poll=True)
    assert result.value["pom_read"] is True
    assert result.value["pom_result_channel"] == "poll"


def test_no_ack_leaves_pom_read_unknown_and_names_railcom():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x13\x72"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is None
    assert "RailCom" in result.detail


def test_unsupported_reply_sets_pom_read_false():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is False


def test_total_silence_sets_pom_read_false_and_records_why():
    link = FakeLink({})
    result = check_pom_read(link, address=3, cv=8, poll=False)
    assert result.value["pom_read"] is False
    assert result.value["pom_result_channel"] == "none"
    assert "silence" in result.detail


def test_the_probe_never_sends_a_write_opcode():
    link = FakeLink({POM_CV8_AT_3: [b"\xff\xfe\x01\x04\x05"]})
    check_pom_read(link, address=3, cv=8, poll=True)
    for frame in link.sent:
        assert frame[2] != 0x23, "0x23 is a write opcode; the probe must never write"
        if frame[2] == 0xE6:
            assert frame[6] & 0xFC == 0xE4, "POM option byte must be the read form 0xE4|MM"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_checks_r1.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.probe.checks'`

- [ ] **Step 3: Implement the R1 check**

```python
# tools/probe/checks.py
"""Capability checks. Every check is read-only: no decoder CV is ever written."""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.probe import commands
from tools.probe.link import Link
from tools.probe.replies import (
    BUSY,
    NO_ACK,
    SHORT_CIRCUIT,
    UNSUPPORTED,
    CvValue,
    parse,
)

POM_WINDOW = 5.0
POLL_INTERVAL = 0.25


def _hexdump(frames) -> list[str]:
    return [f"{'FE' if f.solicited else 'FD'} {f.telegram.hex(' ').upper()}" for f in frames]


@dataclass
class CheckResult:
    name: str
    value: object | None
    detail: str
    frames: list[str] = field(default_factory=list)


def check_pom_read(link: Link, address: int, cv: int = 8, *, poll: bool) -> CheckResult:
    """R1. CV8 is used by default because its ZIMO value is a known constant (145),
    so a plausible-but-wrong reading is detectable."""
    wire = commands.cv_wire(cv)
    frames = link.exchange(commands.pom_read(address, cv), window=POM_WINDOW)
    channel = "broadcast"

    if poll and not any(isinstance(parse(f.telegram), CvValue) for f in frames):
        polled = link.exchange(commands.service_result(), window=POM_WINDOW)
        if any(isinstance(parse(polled_frame.telegram), CvValue) for polled_frame in polled):
            channel = "poll"
        frames = frames + polled

    seen = [parse(f.telegram) for f in frames]
    dump = _hexdump(frames)

    for reply in seen:
        if isinstance(reply, CvValue):
            echo_zero_based = None
            if reply.raw_cv == wire:
                echo_zero_based = True
            elif reply.raw_cv == cv:
                echo_zero_based = False
            return CheckResult(
                "pom_read",
                {
                    "pom_read": True,
                    "pom_result_channel": channel,
                    "pom_echo_zero_based": echo_zero_based,
                    "value": reply.value,
                },
                f"POM read of CV{cv} returned {reply.value} via {channel}",
                dump,
            )

    if UNSUPPORTED in seen:
        return CheckResult(
            "pom_read",
            {"pom_read": False, "pom_result_channel": "none", "pom_echo_zero_based": None, "value": None},
            "station answered 61 82 (instruction not supported): POM read is not implemented",
            dump,
        )
    if NO_ACK in seen:
        return CheckResult(
            "pom_read",
            {"pom_read": None, "pom_result_channel": "none", "pom_echo_zero_based": None, "value": None},
            "decoder did not acknowledge: check RailCom (CV29 bit 3 = 1, CV28 bits 0 and 1 set) and retry",
            dump,
        )
    if SHORT_CIRCUIT in seen:
        return CheckResult("pom_read", None, "short circuit reported; fix the track and retry", dump)
    if BUSY in seen:
        return CheckResult("pom_read", None, "station busy; retry", dump)

    return CheckResult(
        "pom_read",
        {"pom_read": False, "pom_result_channel": "none", "pom_echo_zero_based": None, "value": None},
        "no result on either channel and neither 61 13 nor 61 82: concluded from silence",
        dump,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_checks_r1.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add tools/probe/checks.py tests/test_checks_r1.py
git commit -m "feat(probe): add R1 POM read capability check"
```

---

### Task 6: R2, R4 and R5 — opcode and function-command support

**Files:**
- Modify: `tools/probe/checks.py`
- Test: `tests/test_checks_opcodes.py`

**Interfaces:**
- Consumes: everything from Task 5.
- Produces:
  - `def check_service_ext_cv(link: Link) -> CheckResult` — R2
  - `def check_z21_opcodes(link: Link) -> CheckResult` — R4
  - `def check_single_function(link: Link, address: int) -> CheckResult` — R5
  - `def check_function_groups(link: Link, address: int) -> CheckResult`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checks_opcodes.py
from tools.probe.checks import (
    check_function_groups,
    check_service_ext_cv,
    check_single_function,
    check_z21_opcodes,
)
from tools.probe.fake import FakeLink

EXT_CV1 = b"\x22\x18\x01"
DIRECT_CV1 = b"\x22\x15\x01"
Z21_CV29 = b"\x23\x11\x00\x1c"
DIRECT_CV29 = b"\x22\x15\x1c"
SINGLE_F0_AT_3 = b"\xe4\xf8\x00\x03\x00"
GROUP4_AT_3 = b"\xe4\x23\x00\x03\x00"
GROUP5_AT_3 = b"\xe4\x28\x00\x03\x00"


def test_extended_and_direct_reads_agreeing_sets_service_ext_cv_true():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
        }
    )
    assert check_service_ext_cv(link).value is True


def test_unsupported_extended_opcode_sets_service_ext_cv_false():
    link = FakeLink({EXT_CV1: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_service_ext_cv(link).value is False


def test_disagreeing_values_leave_service_ext_cv_unknown():
    link = FakeLink(
        {
            EXT_CV1: [b"\xff\xfe\x63\x14\x00\x03\x74"],
            DIRECT_CV1: [b"\xff\xfe\x63\x14\x00\x09\x7e"],
        }
    )
    result = check_service_ext_cv(link)
    assert result.value is None
    assert "disagree" in result.detail


def test_z21_opcode_matching_the_direct_read_sets_the_flag():
    link = FakeLink(
        {
            Z21_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"],
            DIRECT_CV29: [b"\xff\xfe\x63\x14\x1c\x0e\x65"],
        }
    )
    assert check_z21_opcodes(link).value is True


def test_z21_opcode_rejected_sets_the_flag_false():
    link = FakeLink({Z21_CV29: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_z21_opcodes(link).value is False


def test_single_function_accepted_when_the_station_does_not_reject_it():
    link = FakeLink({SINGLE_F0_AT_3: [b"\xff\xfe\x01\x04\x05"]})
    assert check_single_function(link, address=3).value is True


def test_single_function_rejected_sets_it_false():
    link = FakeLink({SINGLE_F0_AT_3: [b"\xff\xfe\x61\x82\xe3"]})
    assert check_single_function(link, address=3).value is False


def test_single_function_silence_leaves_it_unknown():
    link = FakeLink({})
    assert check_single_function(link, address=3).value is None


def test_function_groups_need_both_group_4_and_group_5():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x61\x82\xe3"],
        }
    )
    assert check_function_groups(link, address=3).value is False


def test_function_groups_true_when_both_are_accepted():
    link = FakeLink(
        {
            GROUP4_AT_3: [b"\xff\xfe\x01\x04\x05"],
            GROUP5_AT_3: [b"\xff\xfe\x01\x04\x05"],
        }
    )
    assert check_function_groups(link, address=3).value is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_checks_opcodes.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_service_ext_cv'`

- [ ] **Step 3: Append the four checks to `tools/probe/checks.py`**

```python
SERVICE_WINDOW = 8.0


def _read_value(link: Link, payload: bytes) -> tuple[int | None, list, bool]:
    """Returns (value, replies, was_rejected)."""
    frames = link.exchange(payload, window=SERVICE_WINDOW)
    replies = [parse(f.telegram) for f in frames]
    for reply in replies:
        if isinstance(reply, CvValue):
            return reply.value, frames, False
    return None, frames, UNSUPPORTED in replies


def check_service_ext_cv(link: Link) -> CheckResult:
    """R2. Compare the extended read of CV1 against the legacy direct read of CV1."""
    ext_value, ext_frames, rejected = _read_value(link, commands.service_ext_read(1))
    if rejected:
        return CheckResult(
            "service_ext_cv", False, "station answered 61 82 to 22 18: extended opcodes absent",
            _hexdump(ext_frames),
        )
    direct_value, direct_frames, _ = _read_value(link, commands.service_direct_read(1))
    dump = _hexdump(ext_frames) + _hexdump(direct_frames)
    if ext_value is None or direct_value is None:
        return CheckResult("service_ext_cv", None, "no value from one of the two reads", dump)
    if ext_value != direct_value:
        return CheckResult(
            "service_ext_cv", None,
            f"reads disagree: extended {ext_value}, direct {direct_value}", dump,
        )
    return CheckResult(
        "service_ext_cv", True, f"extended and direct reads of CV1 both returned {ext_value}", dump
    )


def check_z21_opcodes(link: Link) -> CheckResult:
    """R4. Only the READ opcode 23 11 is probed. Never 24 12, which would write."""
    z21_value, z21_frames, rejected = _read_value(link, commands.z21_service_read(29))
    if rejected:
        return CheckResult(
            "z21_cv_opcodes", False, "station answered 61 82 to 23 11: Z21 CV opcodes absent",
            _hexdump(z21_frames),
        )
    direct_value, direct_frames, _ = _read_value(link, commands.service_direct_read(29))
    dump = _hexdump(z21_frames) + _hexdump(direct_frames)
    if z21_value is None or direct_value is None:
        return CheckResult("z21_cv_opcodes", None, "no value from one of the two reads", dump)
    if z21_value != direct_value:
        return CheckResult(
            "z21_cv_opcodes", None,
            f"reads disagree: Z21 {z21_value}, direct {direct_value}", dump,
        )
    return CheckResult(
        "z21_cv_opcodes", True, f"Z21 and direct reads of CV29 both returned {z21_value}", dump
    )


def _accepted(link: Link, payload: bytes, window: float = 2.0) -> tuple[bool | None, list]:
    frames = link.exchange(payload, window=window)
    replies = [parse(f.telegram) for f in frames]
    if UNSUPPORTED in replies:
        return False, frames
    if not frames:
        return None, frames
    return True, frames


def check_single_function(link: Link, address: int) -> CheckResult:
    """R5. Commands F0 to the value it already holds, so a negative result changes nothing.

    The caller is responsible for reading the current F0 state first and passing an
    address whose F0 is off; the probe entry point does that.
    """
    accepted, frames = _accepted(link, commands.single_function(address, 0, action=0))
    detail = {
        True: "station accepted E4 F8: single-function commands work, no shadow state needed",
        False: "station answered 61 82 to E4 F8: fall back to function group commands",
        None: "no reply to E4 F8; capability not established",
    }[accepted]
    return CheckResult("single_function_cmd", accepted, detail, _hexdump(frames))


def check_function_groups(link: Link, address: int) -> CheckResult:
    """Groups 4 (F13-F20) and F21-F28 (group 5). All bits zero, so nothing switches on."""
    g4, g4_frames = _accepted(link, commands.function_group(address, 0x23, 0x00))
    g5, g5_frames = _accepted(link, commands.function_group(address, 0x28, 0x00))
    dump = _hexdump(g4_frames) + _hexdump(g5_frames)
    if g4 is None or g5 is None:
        return CheckResult("function_groups_4_5", None, "no reply to E4 23 or E4 28", dump)
    value = bool(g4 and g5)
    detail = "groups 4 and 5 accepted: F13-F28 reachable" if value else "at least one group rejected: F13-F28 unavailable on the group path"
    return CheckResult("function_groups_4_5", value, detail, dump)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_checks_opcodes.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -v`
Expected: PASS, all tests from Tasks 1-6

- [ ] **Step 6: Commit**

```bash
git add tools/probe/checks.py tests/test_checks_opcodes.py
git commit -m "feat(probe): add R2, R4 and R5 opcode capability checks"
```

---

### Task 7: Identity, status and the address band

**Files:**
- Modify: `tools/probe/checks.py`
- Test: `tests/test_checks_identity.py`

**Interfaces:**
- Consumes: everything from Tasks 5-6.
- Produces:
  - `def check_identity(link: Link) -> CheckResult` — version + status in one result
  - `def check_address_band(link: Link, address: int) -> CheckResult`
  - `DECODER_TYPES: dict[int, str]` mapping CV250 values to ZIMO model names

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checks_identity.py
from tools.probe.checks import DECODER_TYPES, check_address_band, check_identity
from tools.probe.fake import FakeLink

VERSION = b"\x21\x21"
STATUS = b"\x21\x24"


def test_identity_reports_the_version_and_decoded_status():
    link = FakeLink(
        {
            VERSION: [b"\xff\xfe\x63\x21\x40\x12\x10"],
            STATUS: [b"\xff\xfe\x62\x22\x07\x47"],
        }
    )
    result = check_identity(link)
    assert result.value["xpressnet"] == "4.0"
    assert result.value["command_station_id"] == 0x12
    assert result.value["status_raw"] == 0x07
    assert result.value["auto_start_mode"] is True


def test_identity_warns_when_the_station_resumes_speeds_on_power_up():
    link = FakeLink(
        {
            VERSION: [b"\xff\xfe\x63\x21\x40\x12\x10"],
            STATUS: [b"\xff\xfe\x62\x22\x07\x47"],
        }
    )
    assert "last known speed" in check_identity(link).detail


def test_identity_without_a_version_reply_is_unknown():
    link = FakeLink({})
    assert check_identity(link).value is None


def test_ms450_is_a_known_decoder_type():
    assert DECODER_TYPES[6] == "MS450"


def test_address_band_confirms_the_threshold_when_only_one_form_answers():
    short_form = b"\xe3\x00\x00\x64"
    long_form = b"\xe3\x00\xc0\x64"
    link = FakeLink({long_form: [b"\xff\xfe\xe4\x04\x00\x00\x00\xe0"], short_form: []})
    result = check_address_band(link, address=100)
    assert result.value == 100


def test_address_band_is_unknown_when_both_forms_answer():
    short_form = b"\xe3\x00\x00\x64"
    long_form = b"\xe3\x00\xc0\x64"
    reply = b"\xff\xfe\xe4\x04\x00\x00\x00\xe0"
    link = FakeLink({long_form: [reply], short_form: [reply]})
    assert check_address_band(link, address=100).value is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_checks_identity.py -v`
Expected: FAIL — `ImportError: cannot import name 'DECODER_TYPES'`

- [ ] **Step 3: Append to `tools/probe/checks.py`**

```python
from tools.probe.replies import Status, Version

DECODER_TYPES = {
    6: "MS450", 7: "MS990", 8: "MS590", 9: "MS950", 10: "MS560",
    11: "MS001", 12: "MS491", 13: "MS581", 14: "MS540", 15: "MS591",
}


def check_identity(link: Link) -> CheckResult:
    version_frames = link.exchange(commands.version(), window=2.0)
    version = next(
        (r for r in map(lambda f: parse(f.telegram), version_frames) if isinstance(r, Version)),
        None,
    )
    if version is None:
        return CheckResult("identity", None, "no version reply; is this the XpressNet port?",
                           _hexdump(version_frames))

    status_frames = link.exchange(commands.status(), window=2.0)
    status = next(
        (r for r in map(lambda f: parse(f.telegram), status_frames) if isinstance(r, Status)),
        None,
    )
    dump = _hexdump(version_frames) + _hexdump(status_frames)
    value = {
        "xpressnet": f"{version.xpressnet_major}.{version.xpressnet_minor}",
        "command_station_id": version.command_station_id,
        "status_raw": status.raw if status else None,
        "auto_start_mode": status.auto_start_mode if status else None,
        "emergency_off": status.emergency_off if status else None,
        "emergency_stop": status.emergency_stop if status else None,
        "service_mode": status.service_mode if status else None,
    }
    detail = f"XpressNet {value['xpressnet']}, command station id 0x{version.command_station_id:02X}"
    if status and status.auto_start_mode:
        detail += (
            "; start mode is AUTOMATIC, so every locomotive resumes its last known speed "
            "when the station powers up - send an emergency stop before restoring track power"
        )
    return CheckResult("identity", value, detail, dump)


def check_address_band(link: Link, address: int) -> CheckResult:
    """Addresses 100..127 are the XpressNet/Z21 divergence band."""
    if not 100 <= address <= 127:
        return CheckResult("loco_address_threshold", None,
                           f"address {address} is outside the 100..127 divergence band", [])
    short_high, short_low = commands.loco_address_bytes(address, threshold=128)
    long_high, long_low = commands.loco_address_bytes(address, threshold=100)
    short_frames = link.exchange(bytes([0xE3, 0x00, short_high, short_low]), window=2.0)
    long_frames = link.exchange(bytes([0xE3, 0x00, long_high, long_low]), window=2.0)
    dump = _hexdump(short_frames) + _hexdump(long_frames)
    if bool(short_frames) == bool(long_frames):
        return CheckResult("loco_address_threshold", None,
                           "both encodings behaved identically; threshold not established", dump)
    threshold = 100 if long_frames else 128
    return CheckResult("loco_address_threshold", threshold,
                       f"only the {'long' if long_frames else 'short'} form answered; threshold is {threshold}",
                       dump)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_checks_identity.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add tools/probe/checks.py tests/test_checks_identity.py
git commit -m "feat(probe): add identity, status and address band checks"
```

---

### Task 8: Report rendering and the CLI entry point

**Files:**
- Create: `tools/probe/report.py`
- Create: `tools/probe/__main__.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `CheckResult` from `checks.py`.
- Produces:
  - `def to_json(results: list[CheckResult], *, port: str, run_at: str) -> str`
  - `def to_markdown(results: list[CheckResult], *, port: str, run_at: str) -> str`
  - `python -m tools.probe [--port PATH] [--address N] [--no-programming-track] [--format human|json|markdown]`

The renderer must distinguish three states, because "unknown" collapsing into "no" is the failure mode the whole milestone exists to prevent.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_report.py
import json

from tools.probe.checks import CheckResult
from tools.probe.report import to_json, to_markdown

RESULTS = [
    CheckResult("service_ext_cv", True, "both reads agreed", ["FE 63 14 00 03"]),
    CheckResult("z21_cv_opcodes", False, "station answered 61 82", []),
    CheckResult("single_function_cmd", None, "no reply", []),
]


def test_json_keeps_true_false_and_null_distinct():
    payload = json.loads(to_json(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z"))
    assert payload["capabilities"]["service_ext_cv"] is True
    assert payload["capabilities"]["z21_cv_opcodes"] is False
    assert payload["capabilities"]["single_function_cmd"] is None


def test_json_records_the_port_and_the_run_time():
    payload = json.loads(to_json(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z"))
    assert payload["port"] == "/dev/cu.usbmodem0"
    assert payload["run_at"] == "2026-08-03T20:00:00Z"


def test_markdown_renders_unknown_as_a_word_not_as_no():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "| `single_function_cmd` | unknown |" in text
    assert "| `z21_cv_opcodes` | no |" in text
    assert "| `service_ext_cv` | yes |" in text


def test_markdown_includes_the_detail_text_for_each_check():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "both reads agreed" in text


def test_markdown_includes_the_raw_frames_for_auditing():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "FE 63 14 00 03" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.probe.report'`

- [ ] **Step 3: Implement the report renderer**

```python
# tools/probe/report.py
"""Render capability results. true / false / null must never be conflated."""

from __future__ import annotations

import json

from tools.probe.checks import CheckResult

_WORDS = {True: "yes", False: "no", None: "unknown"}


def _flatten(results: list[CheckResult]) -> dict[str, object]:
    capabilities: dict[str, object] = {}
    for result in results:
        if isinstance(result.value, dict):
            capabilities.update(result.value)
        else:
            capabilities[result.name] = result.value
    return capabilities


def to_json(results: list[CheckResult], *, port: str, run_at: str) -> str:
    return json.dumps(
        {
            "schema": "railctl/probe-results/v1",
            "port": port,
            "run_at": run_at,
            "capabilities": _flatten(results),
            "checks": [
                {"name": r.name, "value": r.value, "detail": r.detail, "frames": r.frames}
                for r in results
            ],
        },
        indent=2,
        sort_keys=False,
    )


def to_markdown(results: list[CheckResult], *, port: str, run_at: str) -> str:
    lines = [
        "# YD7010 probe results",
        "",
        f"- Port: `{port}`",
        f"- Run at: {run_at}",
        "",
        "| Capability | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        word = _WORDS.get(result.value, "see below") if not isinstance(result.value, dict) else "see below"
        lines.append(f"| `{result.name}` | {word} | {result.detail} |")

    lines += ["", "## Flattened capabilities", "", "```json",
              json.dumps(_flatten(results), indent=2), "```", "", "## Raw frames", ""]
    for result in results:
        lines.append(f"### {result.name}")
        lines.append("")
        if result.frames:
            lines += ["```", *result.frames, "```", ""]
        else:
            lines += ["(no frames received)", ""]
    return "\n".join(lines)
```

- [ ] **Step 4: Implement the entry point**

```python
# tools/probe/__main__.py
"""Run the YD7010 capability probe.

Read-only: no decoder CV is ever written. Function checks command a function to
the value it already holds, so nothing on the layout changes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys

from tools.probe import checks, report
from tools.probe.commands import version as version_cmd
from tools.probe.link import SerialLink, discover_ports
from tools.probe.replies import Version, parse


def find_xpressnet_port() -> str:
    """The XpressNet port is the one that answers the version request."""
    candidates = discover_ports()
    if not candidates:
        raise SystemExit("no /dev/cu.usbmodem7010* ports found; is the YD7010 connected?")
    for path in candidates:
        try:
            with SerialLink(path) as link:
                frames = link.exchange(version_cmd(), window=1.5)
        except OSError:
            continue
        if any(isinstance(parse(f.telegram), Version) for f in frames):
            return path
    raise SystemExit(
        "none of these ports answered a version request: " + ", ".join(candidates)
        + "\nIs the YaMoRC tool holding the port open?"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.probe")
    parser.add_argument("--port", help="serial port; auto-detected when omitted")
    parser.add_argument("--address", type=int, default=3, help="locomotive address (default 3)")
    parser.add_argument("--band-address", type=int, help="an address in 100..127 to test the band")
    parser.add_argument("--no-programming-track", action="store_true",
                        help="skip the service-mode checks R2 and R4")
    parser.add_argument("--format", choices=("human", "json", "markdown"), default="human")
    args = parser.parse_args(argv)

    port = args.port or find_xpressnet_port()
    run_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    results = []
    with SerialLink(port) as link:
        results.append(checks.check_identity(link))
        results.append(checks.check_pom_read(link, args.address, poll=True))
        results.append(checks.check_single_function(link, args.address))
        results.append(checks.check_function_groups(link, args.address))
        if args.band_address:
            results.append(checks.check_address_band(link, args.band_address))
        if not args.no_programming_track:
            results.append(checks.check_service_ext_cv(link))
            results.append(checks.check_z21_opcodes(link))

    if args.format == "json":
        print(report.to_json(results, port=port, run_at=run_at))
    elif args.format == "markdown":
        print(report.to_markdown(results, port=port, run_at=run_at))
    else:
        print(f"port {port}")
        for result in results:
            print(f"  {result.name:24} {result.value!r}")
            print(f"  {'':24} {result.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_report.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Verify the whole suite and the lint gate**

Run: `python -m pytest -v && python -m ruff check . && python -m ruff format --check .`
Expected: all green

- [ ] **Step 7: Commit**

```bash
git add tools/probe/report.py tools/probe/__main__.py tests/test_report.py
git commit -m "feat(probe): add capability report rendering and CLI entry point"
```

---

### Task 9: Run against the hardware and record the results

This task cannot be test-driven. It is the point of the milestone: replacing eight `null`s with measurements.

**Files:**
- Create: `docs/probe-results.md`
- Modify: `docs/superpowers/specs/2026-08-03-railctl-design.md` (only if a measurement contradicts it)

**Preconditions — confirm each before starting:**
1. YD7010 connected by USB, powered, and the YaMoRC tool **closed** so the port is free.
2. The ZIMO MS450P22 locomotive on the **main track**, track power on, at a known address (default 3).
3. Decoder RailCom confirmed on: CV29 bit 3 = 1 and CV28 bits 0 and 1 set. Factory values are CV29 = 14 and CV28 = 67, so an untouched decoder already satisfies this.
4. For the R2/R4 checks: the locomotive moved to the **programming track**. Skip with `--no-programming-track` and record those two as `unknown` rather than guessing.

- [ ] **Step 1: Confirm the port is detected**

Run: `python -m tools.probe --port "" 2>&1 | head -3` then, on failure, `ls /dev/cu.usbmodem7010*`
Expected: auto-detection names `/dev/cu.usbmodem7010A00011943` on the reference unit. If every port is reported as silent, the YaMoRC tool is probably still holding it open.

- [ ] **Step 2: Run the main-track probe**

Run: `python -m tools.probe --address 3 --no-programming-track --format markdown > docs/probe-results.md`
Expected: a markdown file with identity, `pom_read`, `single_function_cmd` and `function_groups_4_5` resolved.

- [ ] **Step 3: Record the `power on` refresh-buffer behaviour by hand**

This one needs a human watching the locomotive. Drive it to step 30 using any throttle, then send track power off and on:

```bash
python - <<'PY'
from tools.probe.link import SerialLink
from tools.probe.commands import status
with SerialLink("/dev/cu.usbmodem7010A00011943") as link:
    for payload in (b"\x21\x80", b"\x21\x81"):
        link.exchange(payload, window=1.0)
    print([f.telegram.hex(" ") for f in link.exchange(status(), window=1.0)])
PY
```

Watch the locomotive. Append to `docs/probe-results.md` under a `## Refresh buffer` heading whether it moved. If it moved, the station restores buffered speeds and the `80 80` emergency stop before `power on` is a real safety guard, not decoration.

- [ ] **Step 4: Move the locomotive to the programming track and run the service-mode checks**

Run: `python -m tools.probe --address 3 --format markdown > /tmp/probe-service.md`
Expected: `service_ext_cv` and `z21_cv_opcodes` resolved. Merge those two sections into `docs/probe-results.md`.

Note: the programming track output is 750 mA and this is a sound decoder. If the checks report no acknowledgement, record that as the finding — it is exactly the constraint that made POM the primary path.

- [ ] **Step 5: Reconcile the results against the spec**

For each measurement, confirm the spec still holds:

| Measurement | If it contradicts the spec |
|---|---|
| `pom_read` false | POM is unavailable; `--mode auto` must resolve to `service`. Update the spec's CV section. |
| `pom_echo_zero_based` false | `CvMatcher` accepts the one-based echo; note it in the spec. |
| status bit 2 clear on a healthy powered track | The "automatic start mode" label is confirmed as the correct reading. |
| `single_function_cmd` false | The function shadow state in the facade is mandatory, not a fallback. |
| `loco_address_threshold` 128 | The 100..127 warning can be dropped. |

Edit the spec only where a measurement actually contradicts it. Record every measurement in `docs/probe-results.md` regardless.

- [ ] **Step 6: Commit**

```bash
git add docs/probe-results.md docs/superpowers/specs/2026-08-03-railctl-design.md
git commit -m "docs(probe): record YD7010 capability measurements from hardware"
```

---

## Self-Review

**Spec coverage.** M1 in the spec requires the R1/R2/R4/R5 procedures plus the status-bit, refresh-buffer and address-band checks, and a committed `docs/probe-results.md` with definite values for `pom_read`, `pom_result_channel`, `pom_echo_zero_based`, `service_direct_cv`, `service_ext_cv`, `z21_cv_opcodes`, `function_groups_4_5` and `single_function_cmd`.

Mapping: R1 → Task 5. R2, R4, R5, function groups → Task 6. Status bit and address band → Task 7. Refresh buffer → Task 9 Step 3. `probe-results.md` → Task 8 renderer plus Task 9.

One gap found and accepted: `service_direct_cv` has no dedicated check. It is established implicitly, because both `check_service_ext_cv` and `check_z21_opcodes` perform a legacy direct read as their comparison baseline — a value there proves direct mode works. Task 9 Step 5 records it from those frames. A separate check would send a third identical telegram for no new information.

Two measured values the spec asks for — `pom_result` and `pom_poll_interval` timings — are not asserted anywhere, because the constants live in the future package, not in the probe. Task 9 Step 2's markdown output carries the raw frames, from which the timings can be read. Plan 2 (M2–M4) turns them into constants.

**Placeholder scan.** No TBD, no "add error handling", no "similar to Task N". Every code step contains runnable code. Task 4 Step 4 flags a known redundancy in the draft guard rather than leaving it to be discovered.

**Type consistency.** `CheckResult` is defined once in Task 5 and imported unchanged in Tasks 6, 7 and 8. `Link.exchange(payload, *, window)` has the same signature in `SerialLink` (Task 3), `FakeLink` (Task 3) and every call site. `cv_wire` is defined in Task 4 and used in Task 5 only. `parse` returns the union defined in Task 2 and is narrowed with `isinstance` everywhere. `_hexdump` and `_accepted` are private helpers defined in Tasks 5 and 6 respectively, before first use.

One naming note: `check_pom_read` returns a dict in `CheckResult.value` while the other checks return a scalar. `report._flatten` handles both explicitly, and `to_markdown` prints "see below" for dict-valued results rather than a misleading yes/no.
