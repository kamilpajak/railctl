# railctl — design

## Purpose

`railctl` is a command-line tool for driving and configuring DCC model locomotives from macOS through a YaMoRC YD7010 command station over USB. It speaks XpressNet across the station's LI-USB-compatible CDC port, so it can read and write decoder CVs, back them up to a file, restore them, and run a locomotive (speed, direction, functions). The immediate user is the author, with one locomotive carrying a **ZIMO MS450P22** sound decoder (MS family, PluX22, 1.2 A continuous) on a bench layout; the immediate need is tuning that decoder without a Windows programmer. That decoder has RailCom enabled — **measured 2026-08-04: CV29 = 14 (bit 3 set) and CV28 = 3 (bits 0 and 1, channel 1 and channel 2)**. The ZIMO MS manual gives 3 as the correct value for this decoder class and 67 only for large scale decoders. An earlier draft of this document asserted 67 and used it to argue that POM should be the primary CV path; both were wrong, and the measurements replaced that framing entirely — see **CV operations by track** below. The design keeps a second transport (Z21 over LAN) in view: adding it must mean adding a transport and an envelope, nothing else. Everything the station cannot be proven to support is treated as unknown until a `doctor` run measures it on the real hardware.

## Scope

- Command station control: version, status, track power on/off, emergency stop (all locomotives or one).
- Driving: 128 speed steps, direction, functions **F0-F28**. The wire form used depends on what the `doctor` probe finds (single-function `E4 F8` preferred, function groups as fallback); F13-F28 are in scope for 0.1.0, not deferred.
- CV read and write, per the **CV operations by track** matrix below. There is no single "primary path": reads have exactly one option and writes have two, so any one sentence naming a primary path has to misdescribe one of them.

#### CV operations by track

Measured on the YD7010 with a ZIMO MS450P22, 2026-08-04. See `docs/probe-results.md`.

| Operation | Programming track (service mode) | Main track (POM) |
| --- | --- | --- |
| Read a CV | **yes — the only way** | **no**, returns only the interface ACK |
| Write a CV | **yes**, and it can be read back | **yes**, and nothing can confirm it |
| Backup / restore / diff | **yes** (they are built on reads) | **refused** |
| Drive, functions, track power | — | yes |

Inside service mode the default encoding is the Z21 16-bit pair `23 11` / `24 12`: it covers CV1..1024 in one form, and its result arrives **unsolicited**, which is the only delivery channel that cannot hand back a stale stored result. The Lenz opcodes stay implemented as fallbacks for other command stations; they require the `21 10` service-result request, which XpressNet section 2.2.8 mandates and which is not optional.

Why POM read fails here is **inferred, not measured**: the YD7010 generates the RailCom cut-out but its documentation describes reception through external modules only. The decoder is excluded by measurement. `pom_read` therefore stays `unknown`, never `false` — no `61 82` was ever observed, and `false` would assert a measurement that was not taken.

#### `--track prog|main`

The user-facing flag names **the track the locomotive stands on**, not the protocol. "Programming track" is a thing you can point at; "POM" is not. `--mode service|pom` remains as a hidden 1:1 alias for one release (`service` → `prog`, `pom` → `main`) and the JSON envelope carries both keys; nothing has shipped, so the rename is free now and never again.

Defaults: `cv read`, `cv write`, `backup`, `restore` and `diff` all default to `prog`. `--track main` must be typed, and is accepted only for `cv write`.

Three rules that follow from the matrix:

- A read requested on the main track fails with `read_needs_prog_track` (exit 16) and stdout empty. The message names the physical action first, then offers the live write as the alternative, and says plainly that the live write cannot be checked until the loco is back on the programming track.
- `--verify` together with `--track main` is a **usage error (exit 2)**, never a silent downgrade — nothing can confirm a POM write.
- Silence on the programming track must never be reported as "the decoder is not answering". An empty or badly contacted programming track is far more likely, and the guidance says so in that order, ending with `railctl cv read 8` as the placement test because a ZIMO decoder answers 145.

Every programming-track command prints once, on stderr: *"Programming track: this acts on whatever locomotive is standing on it. `--address` is not used to select it."*
- ZIMO indexed CVs via automatic CV31/CV32 page selection.
- Curated CV backup and restore (Part B).
- `railctl doctor`: probes and records what this station actually supports, before anything trusts it.
- Zero third-party runtime dependencies apart from Typer; stdlib `termios` serial I/O.

## Out of scope

- Consists, multi-unit and double-header operation (`E2` / `E5` loco-info forms raise `UnsupportedFeatureError`).
- Accessory and turnout decoders; feedback modules; LocoNet; the YD.Control telemetry port.
- Speed step modes other than 128 for commanding (other modes are reported, not commanded).
- Register and paged programming modes as a *usable* fallback (the replies are recognised and reported, never acted on).
- Interactive TUI, long-lived sessions, cancellation of an in-flight service-mode operation.
- Windows and Linux support (the `termios` layer is Darwin-specific by design).

## Architecture

Four layers plus one glue object. Each layer knows only about the one below it.

```
              +---------------------------------------------+
   cli        |  railctl.cli   (Typer command tree)          |
              |  parses args, prints, maps errors -> exit    |
              +----------------------+----------------------+
                                     | Station API, typed results
              +----------------------v----------------------+
   station    |  railctl.station                             |
              |  facade.py  programming.py  capabilities.py   |
              |  doctor.py  types.py  timing.py               |
              |  protocol-agnostic: no FF FE, no port names   |
              +----------------------+----------------------+
                                     | telegrams (bytes) + parsed replies
              +----------------------v----------------------+
   xbus       |  railctl.xbus                                |
              |  codec  address  speed  cv  dialect          |
              |  commands (cmd_*)   replies (parse)          |
              |  pure functions, no I/O                      |
              +----------------------+----------------------+
                                     | telegram in / telegram out
              +----------------------v----------------------+
   link       |  railctl.link.Link                           |
              |  one outstanding command, retry, timeouts    |
              +------+---------------------------+-----------+
                     |                           |
       +-------------v-----------+   +-----------v--------------+
   env |  railctl.envelope       |   |  railctl.transport       | tsp
       |  LiUsbEnvelope (FF FE)  |   |  SerialTransport (termios)|
       |  Z21Envelope (later)    |   |  UdpTransport (later)     |
       +-------------------------+   +--------------------------+
```

Rules that make the layering real, and are enforced by grep tests (`tests/test_layering.py`):

| Rule | Enforced by |
|---|---|
| No identifier in `station/` or `cli/` mentions `FF FE`, `FF FD`, `tty`, `cu.usbmodem`, `baud`, `termios`, `socket`. | grep guard |
| No CV-number arithmetic (`cv - 1`, `cv + 1`, `% 256`, `>> 8`, `<< 8`) outside `xbus/cv.py`. | grep guard over `station/`, `cli/`, `xbus/commands.py` |
| Every layer raises only from its own part of the exception tree. | review + `tests/test_exit_codes.py` |
| Connection targets are opaque strings parsed only by `transport.open_link()`. | grep guard |

### Data flow: one POM CV read, end to end

1. `railctl cv read 8 --address 3` reaches the Typer callback in `railctl.cli`. Argument parsing produces `cv=8`, `address=3`, `mode=ProgMode.AUTO`.
2. The CLI calls `Station.open(target)`. `railctl.transport.open_link(target)` resolves `"auto"` to the XpressNet CDC port, constructs `SerialTransport` + `LiUsbEnvelope` + `Link`, opens the port and runs the handshake (`21 21 00` must answer `63 21 ...`). `Capabilities.load(path, link.identity)` restores what the last `doctor` learned.
3. `Station.cv_read(8, address=3)` takes the station `RLock`, resolves `AUTO` to `ProgMode.POM` (`capabilities.pom_read` is `True` or `None`), refuses early if it is `False`, verifies `status().track_power`, and skips page selection because CV8 is not in `INDEXED_CV_RANGE`.
4. `cmd_pom_read_byte(3, 8, threshold=100)` asks `xbus.cv.pom_cv_fields(8)` for `(MM=0, LSB=0x07)` — the only place the zero-based conversion happens — then `codec.encode(0xE6, 0x30, 0x00, 0x03, 0xE4, 0x07, 0x00)` appends the XOR: `E6 30 00 03 E4 07 00 36`.
5. `Link.drain()` discards stale results, then `Link.request(telegram, timeout=5.0)` takes the link lock, calls `envelope.note_request()`, `envelope.wrap()` (which prepends `FF FE` and logs `TX`), and `transport.write()`.
6. `Link` loops on `transport.read()` -> `envelope.feed()` -> `envelope.pop()`. The first `Kind.SOLICITED` frame ends the wait; `envelope.note_reply(frame)` closes the lifecycle and `request()` returns the bare telegram `01 04 05`.
7. `xbus.replies.parse()` turns it into `GenericAck` — the interface confirming it forwarded the command, not a value. The CV programmer falls through to the wait loop.
8. `CvProgrammer._await_result()` drains pending unsolicited frames with `Link.poll(0.0)`, then sends `cmd_service_result_request()` (`21 10 31`) through `Link.request`. If the station answers `61 82` (not in service mode, so the poll is meaningless) polling is disabled for the rest of this attempt and the loop waits passively on `Link.poll(interval)`.
9. A frame arrives carrying `63 14 07 91`. `parse()` yields `CvValue(raw_cv=7, value=145, ident=0x14)`. `CvMatcher` accepts it because `7 in xbus.cv.echo_candidates(POM_ZERO_BASED, 8, zero_based=None) == {7, 8}`. The station records `pom_read=True`, `pom_result_channel="broadcast"`, and — because the echo was 7, not 8 — `pom_echo_zero_based=True`.
10. `cv_read` returns `CvResult(cv=8, value=145, mode=POM, encoding=POM_ZERO_BASED, operation="read", verified=None, elapsed=0.08)`. The CLI prints it, and `Station.close()` flushes the learned capabilities to `~/.config/railctl/capabilities.json`.

If step 9 never happens, three attempts of 2.0 s each are made (`link.drain()` before each), and the failure is `DecoderNoAckError` when `61 13` was seen or `DecoderNotRespondingError` otherwise — never a silent zero.

### Package layout

```
src/railctl/
  __init__.py  errors.py  link.py  py.typed
  transport/  __init__.py (Transport, open_link)  serial_posix.py  fake.py  udp.py(later)
  envelope/   __init__.py (Frame, Kind, Envelope, EnvelopeStats)  liusb.py  z21.py(later)
  xbus/       __init__.py  codec.py  address.py  speed.py  cv.py  dialect.py
              commands.py  replies.py
  station/    __init__.py  types.py  timing.py  facade.py  programming.py
              capabilities.py  doctor.py
  cli/        __init__.py  main.py  _errors.py  (command modules, Part B)
```

Console script: `railctl = "railctl.cli.main:app"`.

## Transport, envelope and X-Bus codec

### Exceptions (`railctl/errors.py`)

One module holds the whole tree, the exit-code map, and `exit_code_for()`. Nothing else defines an exception type.

```python
class RailctlError(Exception):
    def __init__(self, message: str, *, hint: str | None = None) -> None: ...
```

| Class | Parent | Raised when |
|---|---|---|
| `TransportError` | `RailctlError` | port vanished, write failed, LI interface status `01 01/02/03/05/06` |
| `PortNotFound` / `AmbiguousPort` / `PortBusy` / `PortConfigError` / `PortNotOpen` | `TransportError` | port discovery and `open()` failures |
| `PortNotXpressNet` | `TransportError` | port opened but the `21 21 00` handshake failed |
| `ProtocolError` | `RailctlError` | well-framed but unparseable or unexpected telegram |
| `XBusEncodeError` / `XBusDecodeError` | `ProtocolError` | internal telegram inconsistency |
| `XBusChecksumError` | `XBusDecodeError` | XOR mismatch |
| `LinkProtocolError` | `ProtocolError` | station rejected the telegram twice |
| `LinkTimeout` | `RailctlError` | no reply within the budget |
| `UnsupportedCommandError` | `RailctlError` | station answered `61 82` |
| `UnsupportedFeatureError` | `RailctlError` | outside this tool's scope (consists, F13+ unprobed) |
| `StationError` | `RailctlError` | facade-level base |
| `TrackPowerError` | `StationError` | operation needs power on, or power is in the wrong state |
| `ProgrammingError` | `StationError` | CV-operation base; carries `cv: int \| None` |
| `DecoderNoAckError` / `ShortCircuitError` / `StationBusyError` / `DecoderNotRespondingError` / `CvVerifyError` / `CvOutOfRangeError` / `ServiceEncodingUnknownError` / `PomReadUnsupportedError` / `IndexPageRequiredError` | `ProgrammingError` | see the station section |

Argument validation (address, speed, CV, value out of range) raises plain `ValueError`, which the CLI maps to the Typer usage code. The CLI catches `RailctlError` at the top, prints `str(exc)`, prints `Hint: {exc.hint}` when set, and exits with `exit_code_for(exc)`.

### Wire logging

`logging.getLogger("railctl.wire")` emits one DEBUG record per frame, **owned by the envelope in both directions**, always showing bytes as they appear on the wire. `Link` never logs wire bytes. `--verbose` enables it, and it is the primary instrument for every hardware probe.

```
TX FF FE 21 21 00
RX FF FE 63 21 40 12 10
RX! FF FD 61 01 60        (unsolicited)
RX? 12 34                 (discarded during resync)
```

### Transport

```python
class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, max_bytes: int, timeout: float) -> bytes: ...
    def flush_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def description(self) -> str: ...   # "xpressnet serial /dev/cu.usbmodem...3"
    @property
    def identity(self) -> str: ...      # USB serial, "host:port", or "unknown"

def open_link(target: str = "auto") -> Link: ...
#   "auto"                        -> autodetect, then handshake
#   "serial:/dev/cu.usbmodem...3" -> that port
#   "z21:192.168.0.111:21105"     -> future LAN transport
```

`read()` blocks up to `timeout`, returns as soon as one byte is available, returns `b""` on timeout, and never raises on timeout — framing is not its problem. `write()` writes everything or raises `TransportError`.

**Why no pyserial.** Its POSIX backend is `os.open` + `termios.tcsetattr` + `select`. We need 57600 8-N-1, which is a POSIX-standard rate whose Darwin constant is the literal value (`termios.B57600 == 57600`). The device is USB CDC-ACM, so the rate is forwarded as `SET_LINE_CODING` to a fixed-rate virtual UART and is essentially cosmetic; we set it because Lenz 23151 section 1.1 specifies it. No flow control, no DTR toggling. Result: no wheels, no ABI risk on Python 3.14. Portability note: on Linux the speed constants are small indices, so a Linux port needs a lookup table — this is the only platform assumption in the file.

`SerialTransport(SerialConfig(port, baudrate=57600))` with `open/close/read/write/flush_input`, context-manager support, and a private `_configure(fd)`. Settings that are load-bearing:

| Setting | Why |
|---|---|
| `/dev/cu.*`, never `/dev/tty.*` | `cu` is call-out and does not block in `open()` waiting for DCD |
| `O_NOCTTY` | otherwise a line BREAK delivers `SIGINT` to the process |
| `O_NONBLOCK`, `VMIN=0`, `VTIME=0` | `os.read` returns buffered bytes immediately; all waiting is done by `select` |
| `lflag = 0` | `ISIG` would turn an incoming `0x03` into `SIGINT`; `ICANON` would wait for `0x0A` |
| `iflag = 0` | `PARMRK` duplicates `0xFF`, `ISTRIP` clears bit 7, `ICRNL`/`INLCR` rewrite `0x0D`/`0x0A` — our framing uses `0xFF` twice per frame and payloads legitimately contain `0x0A`, `0x0D`, `0xFF` (speed 126 forward is exactly `0xFF`) |
| `oflag = 0` | no output post-processing |
| `cflag = CS8 \| CREAD \| CLOCAL` | 8-N-1, no `CRTSCTS`, no `HUPCL` so closing does not reset the adapter |
| read-back check after `tcsetattr` | some CDC drivers silently ignore parts of the request: `(got[2] & mask) != termios.CS8` raises `PortConfigError` |

`WRITE_SELECT_TIMEOUT = 1.0`, `READ_CHUNK = 256`, `BAUDRATE = 57600`.

**Port discovery and confirmation.** `find_xpressnet_port()` globs `/dev/cu.usbmodem*3` (the CDC interface index: 1 = LocoNet, 3 = XpressNet, 5 = YD.Control), raising `PortNotFound` or `AmbiguousPort`. The glob is a guess. Confirmation is mandatory and happens in `Link.open()`: send `21 21 00`, require a reply with header `0x63` and first data byte `0x21` within `HANDSHAKE_TIMEOUT = 2.0` s, else raise `PortNotXpressNet` naming the port and quoting up to 32 received bytes — so an ASCII telemetry line from the YD.Control port is visible in the error instead of a bare 5 s timeout. This applies to auto-detected and explicit targets alike.

`FakeTransport` (shipped, not test-only, so probes reuse it) adds `queue(data)`, `written`, `on_write: Callable[[bytes, FakeTransport], None]` and `max_write: int | None`. `on_write` lets a test script a station: the callback inspects the outgoing frame and queues the reply, reproducing real request/response ordering with no threads and no sleeps. `max_write` forces short writes so the partial-write loop, which never runs on real 10-byte frames, is still covered.

### Envelope

```python
class Kind(enum.Enum):
    SOLICITED = "solicited"      # FF FE -- reply to the command we sent
    UNSOLICITED = "unsolicited"  # FF FD -- broadcast / spontaneous

@dataclass(frozen=True, slots=True)
class Frame:
    kind: Kind
    payload: bytes    # complete X-Bus telegram, header..XOR, framing stripped, XOR verified

class Envelope(Protocol):
    def wrap(self, telegram: bytes) -> bytes: ...
    def feed(self, data: bytes) -> None: ...
    def pop(self) -> Frame | None: ...
    def note_request(self, telegram: bytes) -> None: ...
    def note_reply(self, frame: Frame) -> None: ...
    def note_abandoned(self) -> None: ...
    def reset(self) -> None: ...
    @property
    def expects_ack(self) -> bool: ...
    @property
    def stats(self) -> EnvelopeStats: ...   # frames_ok, bytes_dropped, bad_xor, stray_replies
```

`feed()`/`pop()` split keeps the envelope free of I/O and lets tests feed one byte at a time. **Exactly one of `note_reply` or `note_abandoned` ends every request started by `note_request`.** Forgetting `note_reply` on the success path silently breaks `stray_replies` and the future Z21 classification, so it has a named test.

`LiUsbEnvelope` constants: `PREFIX_SOLICITED = b"\xff\xfe"`, `PREFIX_UNSOLICITED = b"\xff\xfd"`, `MAX_BUFFER = 4096`, `expects_ack = True`. `wrap()` prepends two bytes and logs `TX`. Per Lenz 23151 section 1.3 those bytes are **not** part of the XOR, so the codec never sees them.

`pop()` algorithm, on `self._buf`:

1. Delete everything before the first `0xFF` (log `RX?`, add to `bytes_dropped`). No `0xFF` at all: drop the whole buffer, return `None`.
2. `len < 2` -> `None`.
3. `_buf[1]`: `0xFE` -> SOLICITED, `0xFD` -> UNSOLICITED, anything else (including a second `0xFF`) -> delete **one** byte, go to 1.
4. `len < 3` -> `None`.
5. `total = (_buf[2] & 0x0F) + 2`; `len < 2 + total` -> `None`, the frame is still arriving.
6. `telegram = _buf[2 : 2+total]`; require `xor(telegram) == 0`. Fail: `bad_xor += 1`, delete **one** byte (never the whole candidate — the true start may lie inside it), go to 1. Pass: consume `2 + total`, `frames_ok += 1`, log `RX`/`RX!`, return the `Frame`; if SOLICITED with no request outstanding, `stray_replies += 1` first.
7. `feed()` truncates the buffer to its last `MAX_BUFFER` bytes and counts the discard, bounding memory if the port ever streams unframed ASCII.

Why the header nibble and not a delimiter search: `FF FE` occurs inside legitimate payloads (`FF FE E4 13 00 03 FF 0B` is loco 3 at step 126 forward). The correct order is anchor once on the prefix, trust the nibble for the length, let the XOR confirm.

Interleaving: `pop()` returns frames in arrival order with the kind attached and `Link` decides — UNSOLICITED during a request goes to `on_event` and the wait continues; the first SOLICITED is the reply; a SOLICITED frame with nothing outstanding is a late reply, discarded and counted. There is never ambiguity about which command a `FF FE` frame answers because at most one command is outstanding. Note for RailCom: 23151 section 1.4 lists RailCom info among the *unsolicited* messages, so a POM read result is expected on `FF FD`.

`expects_ack` is the request/response policy, read off the envelope rather than hardcoded in `Link` — `True` for LI-USB (23151 section 1.3: every command is acknowledged), `False` for the future `Z21Envelope`. **It is unverified on the YD7010** for commands that return no data (power, drive, function, POM write); the first hardware probe settles it, and if the answer is negative those commands move to `Link.send_no_reply()`.

### Link

```python
DEFAULT_TIMEOUT = 5.0        # LI-USB normal-operation exchange budget
PROGRAMMING_TIMEOUT = 95.0   # LI-USB programming-mode exchange budget (1.5 min + margin)
HANDSHAKE_TIMEOUT = 2.0
SETTLE_TIME = 0.05           # send_no_reply only
MAX_RETRIES = 1
_READ_CHUNK = 256
_READ_SLICE = 0.2            # max blocking time per select(), keeps Ctrl-C responsive

class Link:
    def __init__(self, transport: Transport, envelope: Envelope, *,
                 default_timeout: float = DEFAULT_TIMEOUT,
                 on_event: Callable[[Frame], None] | None = None) -> None: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def request(self, telegram: bytes, *, timeout: float | None = None) -> bytes: ...
    def send(self, telegram: bytes, *, timeout: float | None = None) -> None: ...
    def send_no_reply(self, telegram: bytes) -> None: ...
    def await_frame(self, match: Callable[[Frame], bool], *, timeout: float) -> Frame: ...
    def poll(self, timeout: float = 0.0) -> list[Frame]: ...
    def drain(self) -> None: ...
    def stats(self) -> LinkStats: ...
    @property
    def description(self) -> str: ...
    @property
    def identity(self) -> str: ...
```

`LinkStats` carries `requests, retries, timeouts, frames_ok, bytes_dropped, bad_xor, stray_replies`.

- `open()` opens the transport, `flush_input()`, `envelope.reset()`, then the handshake.
- `request()` returns the reply **telegram** (framing stripped, XOR verified) or raises `LinkTimeout`.
- `send()` is `request()` with the reply discarded; it still waits, because `expects_ack` says the protocol requires it.
- `send_no_reply()` writes, waits `SETTLE_TIME`, drains and dispatches, returns. Used when `expects_ack` is false.
- `await_frame()` reads and dispatches **without writing**, under the same lock, until `match` succeeds. This is how asynchronous RailCom results are collected.
- `poll(timeout)` drains and returns unsolicited frames while idle; `poll(0.0)` is the non-blocking drain the station uses. `drain()` discards everything queued.
- `on_event` receives every unsolicited frame; events are also kept in a `deque(maxlen=256)`. An exception from `on_event` is logged at WARNING and swallowed so a bad callback cannot lose a reply.

`request()` internals: `drain(0.0)` first, `note_request`, `write(wrap(telegram))`, then loop `pop()` / `read(min(remaining, _READ_SLICE))` until a solicited frame arrives; unsolicited frames are dispatched and the wait continues; on the success path `note_reply(frame)` is mandatory. On expiry: `note_abandoned()`, `timeouts += 1`, raise `LinkTimeout` with the telegram, the budget and `stats()` embedded — a climbing `bytes_dropped` with `frames_ok == 0` is what distinguishes "wrong CDC port" from "dead port" without an extra flag. The receive buffer is **not** flushed on timeout; a late reply is caught and counted as a stray by the next `drain()`, because flushing risks cutting a frame in half.

Retry policy: `61 80` (station saw a bad XOR from us) and `01 0A` (station did not understand, retry) mean the telegram never arrived intact, so the command is resent once — safe because every command here is idempotent. Two rejections raise `LinkProtocolError`. `61 82` (not supported) is a real answer, returned to the caller, **never retried** — that is how the capability probes learn an opcode is unavailable.

Serialisation: `threading.RLock` held for the whole duration of every method that touches the transport. One outstanding command is therefore structural, not conventional. `RLock` because a station helper composing two link calls in one critical section (a POM read is exactly that) must not deadlock itself. The station implements no queueing of its own.

Timeout classes, decided by `xbus.commands.timeout_class(telegram) -> TimeoutClass` so the station never inspects opcode bytes:

| Class | Commands | Budget |
|---|---|---|
| `NORMAL` | version, status, power, drive, function, POM write, POM read phase 1 | 5.0 s |
| `PROGRAMMING` | `22 15`, `23 16`, `22 18..1B`, `23 1C..1F`, `21 10`, `23 11`, `24 12` | 95.0 s |

The long service-mode budget is a direct wait, not a poll loop: the reply can take a minute and arrives as the command reply. `21 10 31` re-fetches a result that already exists.

### Codec (`xbus/codec.py`)

```python
MAX_DATA_BYTES = 15; MAX_TELEGRAM_LEN = 17; MIN_TELEGRAM_LEN = 2

def xor(data: bytes) -> int: ...                     # xor(valid telegram) == 0
def telegram_length(header: int) -> int: ...         # (header & 0x0F) + 2
def encode(header: int, *data: int) -> bytes: ...    # header, data..., XOR
def decode(raw: bytes) -> tuple[int, bytes]: ...     # (header, data) without the XOR byte
```

`encode()` derives the expected data-byte count from the header's low nibble and raises `XBusEncodeError` on a mismatch, so an opcode whose argument list disagrees with its declared length cannot ship. `decode()` uses the `xor(whole) == 0` identity. Golden vectors come from the measured hardware: `encode(0x21, 0x21) == b"\x21\x21\x00"`, `decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x21\x40\x12")`.

### Dialect (`xbus/dialect.py`)

```python
@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    long_address_threshold: int
    service_cv_preference: tuple[CvEncoding, ...]

XPRESSNET = Dialect("xpressnet", 100,
                    (CvEncoding.SERVICE_DIRECT, CvEncoding.Z21_16BIT, CvEncoding.SERVICE_EXT))
Z21       = Dialect("z21", 128, (CvEncoding.Z21_16BIT,))
```

A data object, not a hierarchy. `long_address_threshold` is the *default*; `Capabilities.loco_address_threshold` overrides it once measured. `service_cv_preference` is the ordered list the station walks when choosing a service-mode encoding, filtered by capabilities.

### Loco address (`xbus/address.py`)

```python
LOCO_ADDR_MIN, LOCO_ADDR_MAX = 1, 9999    # station limit; the wire field holds 14 bits
LONG_ADDRESS_FLAG, LONG_ADDRESS_MASK = 0xC000, 0x3FFF

def encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]: ...
def decode_loco_address(adr_high: int, adr_low: int) -> int: ...
```

Below the threshold: `(0x00, address)`. At or above: `v = address | 0xC000`, `(v >> 8, v & 0xFF)`. One function covers both dialects because XpressNet's "add `0xC000` then split" and Z21's "`DB1 = 0xC0 | Adr_MSB`" produce identical bytes for every address in 1..16383 (asserted exhaustively as a test). The **only** difference is the threshold, 100 vs 128, carried as one integer.

Consequence: on XpressNet, addresses 100..127 go out as long DCC addresses. A decoder configured short in that range (CV1 = 100..127, CV29 bit 5 = 0) simply does nothing, with no error. The station warns once, naming CV1 and CV29 bit 5.

Vectors: `1/100 -> 00 01`; `99/100 -> 00 63`; `100/100 -> C0 64`; `127/100 -> C0 7F`; `128/100 -> C0 80`; `1000/100 -> C3 E8`; `1234/100 -> C4 D2`; `9999/100 -> E7 0F`; `100/128 -> 00 64`; `127/128 -> 00 7F`; `128/128 -> C0 80`. Plus round trip for every address and both thresholds, and `ValueError` at 0, -1, 10000.

### Speed (`xbus/speed.py`)

```python
SPEED_STEPS = 128; MAX_SPEED_STEP = 126; DRIVE_IDENT_128 = 0x13

class Direction(enum.IntEnum):
    REVERSE = 0
    FORWARD = 1

def encode_speed_128(step: int, direction: Direction) -> int: ...
def encode_emergency_stop_128(direction: Direction) -> int: ...
def decode_speed_128(byte: int) -> tuple[int, Direction, bool]: ...   # (step, direction, emergency)
```

Wire layout `RVVVVVVV`: 0 = braked stop, 1 = emergency stop, 2..127 = steps 1..126, bit 7 = forward. Direction is carried even on stop. Vectors: step 0 FWD `0x80`, step 0 REV `0x00`, step 1 FWD `0x82`, step 126 FWD `0xFF`, step 126 REV `0x7F`, e-stop FWD `0x81`, e-stop REV `0x01`; round trip for every pair; `ValueError` at -1 and 127. `Direction` is defined here once and re-exported by `railctl.station`; the CLI parses it as `Direction[value.upper()]`.

### CV number conversions (`xbus/cv.py`) — the single choke point

Every function takes a **1-based** user CV number; no function outside this module accepts or produces a wire CV address.

```python
class CvEncoding(enum.Enum):
    POM_ZERO_BASED = "pom"      # E6 30 ... (0xE4|MM) LSB          wire = cv - 1
    SERVICE_DIRECT = "direct"   # 22 15 C / 23 16 C V              wire = cv, 1..255
    SERVICE_EXT    = "ext"      # 22 18..1B / 23 1C..1F            page + (cv & 0xFF)
    Z21_16BIT      = "z21"      # 23 11 MSB LSB / 24 12 MSB LSB V  wire = cv - 1

POM_CV_MIN, MAX_CV_POM = 1, 1024
MAX_CV_DIRECT = 255            # see the CV256 note below
MAX_CV_EXT = 1024              # 1..1023 by page, plus CV1024 as page 0 / C = 0
MAX_CV_Z21 = 1024
EXT_READ_OPCODES  = (0x18, 0x19, 0x1A, 0x1B)
EXT_WRITE_OPCODES = (0x1C, 0x1D, 0x1E, 0x1F)
EXT_PAGE_SIZE = 256
CV_FOR_PAGE0_ZERO = 1024

def pom_cv_fields(cv: int) -> tuple[int, int]: ...        # (MM, LSB)
def direct_cv_byte(cv: int) -> int: ...                   # cv, 1..255
def ext_cv_fields(cv: int) -> tuple[int, int]: ...        # (page 0..3, C byte)
def z21_cv_fields(cv: int) -> tuple[int, int]: ...        # (MSB, LSB)
def join_cv_field(msb: int, lsb: int) -> int: ...         # for 64 14 replies
def decode_echo(encoding: CvEncoding, raw: int, *,   # page_index required for SERVICE_EXT,
                page_index: int | None = None,       # zero_based required for POM: neither
                zero_based: bool | None = None) -> int: ...   # may be guessed, both raise
def echo_candidates(encoding: CvEncoding, cv: int, *,
                    zero_based: bool | None = None) -> frozenset[int]: ...
def resolve_service_cv(reply_ident: int, c: int) -> int: ...   # 63 14..17 -> CV number
```

| Encoding | Wire formula | Valid CV | Inverse |
|---|---|---|---|
| `POM_ZERO_BASED` | `w = cv - 1`; `MM = w >> 8`; `LSB = w & 0xFF` | 1..1024 | `raw + 1` |
| `SERVICE_DIRECT` | `byte = cv` | 1..255 | `raw` (raw 0 rejected) |
| `SERVICE_EXT` | `idx = cv // 256`; `opcode = base + idx`; `byte = cv & 0xFF` | 1..1023, plus 1024 -> `(0, 0x00)` | `256 * page_index + raw`; `(0, 0)` -> 1024 |
| `Z21_16BIT` | `w = cv - 1`; `MSB = w >> 8`; `LSB = w & 0xFF` | 1..1024 | `raw + 1` |

The zero-based rule applies to `POM_ZERO_BASED` and `Z21_16BIT` **only**. Two resolutions worth stating explicitly:

- **CV256 is not encodable with the legacy direct opcodes.** 23151 sections 3.2.6 and 3.2.14 state that from station version 3.6 onward `C == 0` addresses CV1024, not CV256. The YD7010 reports 4.0, so sending `C == 0` would touch the wrong CV with no error. `MAX_CV_DIRECT` is therefore 255, and CV256 and above must use the extended or Z21 opcodes, or POM.
- **The `SERVICE_EXT` inverse is not `raw or 256`.** Page 0x18 covers CV1..255 (plus the CV1024 special case), and pages 0x19/0x1A/0x1B cover 256..511 / 512..767 / 768..1023 with `raw == 0` meaning the first CV of the page. Using the direct-mode fudge here would decode CV256 as 512, CV512 as 768 and CV768 as 1024 — three ZIMO-relevant CVs silently wrong. `page_index` is supplied by the caller from the request it issued.

`echo_candidates` exists so no comparison logic needs CV arithmetic elsewhere: for `POM_ZERO_BASED` it returns `{(cv - 1) & 0xFF}` when `zero_based is True`, `{cv & 0xFF}` when `False`, and both when `None`; for the other encodings, the single value the encoder produced.

`resolve_service_cv(ident, c)`: `63 14` with `c == 0` -> CV1024, `c == 1..255` -> CV `c`; `63 15/16/17` -> `256/512/768 + c` (23151 section 3.1.2.6).

`tests/test_cv_encoding.py` asserts the vector table, round trips over each encoding's own range, boundaries at CV255/256/257, 511/512, 767/768, 1023/1024, and the range guards (CV0 everywhere, CV256 on direct, CV1025 on POM).

### Command encoders (`xbus/commands.py`)

All encoders return a complete telegram with its XOR and **no framing prefix**. Address-bearing encoders take `threshold: int` as a keyword.

```python
class FunctionGroup(enum.IntEnum):
    G1 = 0x20   # F0..F4     G2 = 0x21   # F5..F8     G3 = 0x22   # F9..F12
    G4 = 0x23   # F13..F20 (needs station V3.6+)      G5 = 0x28   # F21..F28 (V3.6+)

FUNCTION_BITS: dict[int, tuple[FunctionGroup, int]]   # F0 -> (G1, 4); F1..F4 -> (G1, 0..3); ...
MAX_FUNCTION = 28

def pack_function_bits(group: FunctionGroup, state: Mapping[int, bool]) -> int: ...

def cmd_station_version() -> bytes
def cmd_station_status() -> bytes
def cmd_track_power_on() -> bytes
def cmd_track_power_off() -> bytes
def cmd_emergency_stop_all() -> bytes
def cmd_emergency_stop_loco(address: int, *, threshold: int) -> bytes
def cmd_drive_128(address: int, step: int, direction: Direction, *, threshold: int) -> bytes
def cmd_function_group(address: int, group: FunctionGroup, bits: int, *, threshold: int) -> bytes
def cmd_loco_info(address: int, *, threshold: int) -> bytes

def cmd_service_direct_read(cv: int) -> bytes
def cmd_service_direct_write(cv: int, value: int) -> bytes
def cmd_service_ext_read(cv: int) -> bytes
def cmd_service_ext_write(cv: int, value: int) -> bytes
def cmd_z21_cv_read(cv: int) -> bytes
def cmd_z21_cv_write(cv: int, value: int) -> bytes
def cmd_service_result_request() -> bytes

def cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes
def cmd_pom_write_byte(address: int, cv: int, value: int, *, threshold: int) -> bytes
def cmd_pom_write_bit(address: int, cv: int, bit: int, value: bool, *, threshold: int) -> bytes

class TimeoutClass(enum.Enum):
    NORMAL = "normal"
    PROGRAMMING = "programming"

def timeout_class(telegram: bytes) -> TimeoutClass: ...
```

Per-locomotive emergency stop uses the dedicated instruction `92 AH AL XOR` (XpressNet section 2.2.5.2), not `E4 13` with wire value 1: it needs no direction bit, so a safety path never has to make a `loco_info` round trip first. `encode_emergency_stop_128` stays in `speed.py` for decode symmetry.

Golden vectors (`tests/test_commands.py`, one parametrize case each; add `FF FE` in front for the wire):

| Call | Telegram |
|---|---|
| `cmd_station_version()` | `21 21 00` (measured; replies `63 21 40 12 10`) |
| `cmd_station_status()` | `21 24 05` (measured; replies `62 22 07 47`) |
| `cmd_track_power_on()` | `21 81 A0` |
| `cmd_track_power_off()` | `21 80 A1` |
| `cmd_emergency_stop_all()` | `80 80` |
| `cmd_emergency_stop_loco(3)` | `92 00 03 91` |
| `cmd_emergency_stop_loco(1234)` | `92 C4 D2 84` |
| `cmd_drive_128(3, 1, FORWARD)` | `E4 13 00 03 82 76` |
| `cmd_drive_128(3, 60, FORWARD)` | `E4 13 00 03 BD 49` |
| `cmd_drive_128(3, 126, FORWARD)` | `E4 13 00 03 FF 0B` (payload contains `FF`) |
| `cmd_drive_128(1000, 63, FORWARD)` | `E4 13 C3 E8 C0 1C` |
| `cmd_function_group(3, G1, 0x10)` | `E4 20 00 03 10 D7` (F0 on) |
| `cmd_function_group(3, G2, 0x01)` | `E4 21 00 03 01 C7` (F5 on) |
| `cmd_function_group(3, G3, 0x01)` | `E4 22 00 03 01 C4` (F9 on) |
| `cmd_function_group(3, G4, 0x01)` | `E4 23 00 03 01 C5` (F13 on) |
| `cmd_function_group(3, G5, 0x01)` | `E4 28 00 03 01 CE` (F21 on) |
| `cmd_loco_info(3)` | `E3 00 00 03 E0` |
| `cmd_loco_info(1234)` | `E3 00 C4 D2 F5` |
| `cmd_service_direct_read(8)` | `22 15 08 3F` |
| `cmd_service_direct_write(144, 0)` | `23 16 90 00 A5` |
| `cmd_service_ext_read(8)` | `22 18 08 32` |
| `cmd_service_ext_read(256)` | `22 19 00 3B` |
| `cmd_service_ext_read(257)` | `22 19 01 3A` |
| `cmd_service_ext_read(512)` | `22 1A 00 38` |
| `cmd_service_ext_read(1023)` | `22 1B FF C6` |
| `cmd_service_ext_read(1024)` | `22 18 00 3A` |
| `cmd_service_ext_write(257, 5)` | `23 1D 01 05 3A` |
| `cmd_z21_cv_read(1)` | `23 11 00 00 32` |
| `cmd_z21_cv_read(8)` | `23 11 00 07 35` |
| `cmd_z21_cv_read(257)` | `23 11 01 00 33` |
| `cmd_z21_cv_read(1024)` | `23 11 03 FF CE` |
| `cmd_z21_cv_write(8, 12)` | `24 12 00 07 0C 3D` |
| `cmd_service_result_request()` | `21 10 31` |
| `cmd_pom_read_byte(3, 8)` | `E6 30 00 03 E4 07 00 36` |
| `cmd_pom_read_byte(3, 256)` | `E6 30 00 03 E4 FF 00 CE` |
| `cmd_pom_read_byte(3, 257)` | `E6 30 00 03 E5 00 00 30` |
| `cmd_pom_read_byte(3, 1024)` | `E6 30 00 03 E7 FF 00 CD` |
| `cmd_pom_read_byte(1234, 300)` | `E6 30 C4 D2 E5 2B 00 0E` |
| `cmd_pom_write_byte(3, 8, 12)` | `E6 30 00 03 EC 07 0C 32` |
| `cmd_pom_write_byte(3, 31, 0)` | `E6 30 00 03 EC 1E 00 27` |
| `cmd_pom_write_byte(3, 32, 0)` | `E6 30 00 03 EC 1F 00 26` |
| `cmd_pom_write_bit(3, 29, 3, True)` | `E6 30 00 03 E8 1C 0B 2A` |

Constraint the wire format imposes upward: `E4 20/21/22/23/28` set **every** function in the group at once, so "F2 on" also asserts F0, F1, F3, F4. The station must keep a per-locomotive function shadow. Groups G4 and G5 need station version 3.6 or later; the YD7010 reports 4.0, so they are expected to work, but that is a probed capability, not an assumption.

### Reply parsing (`xbus/replies.py`)

`parse(telegram) -> Reply` validates with `codec.decode()` (which is redundant after the envelope, and is what makes `parse` safe on hand-assembled test bytes) and dispatches on `(header, data[0])`. It never raises for an unrecognised telegram; it returns `Other(telegram)` so probes can print raw hex.

```python
GenericAck()                       # 01 04 05
InterfaceStatus(code: int)         # other 01 XX frames
StationVersion(raw, station_id)    # 63 21 VER ID; .version "4.0"; .family "Z21" for 0x12
StationStatus(raw, emergency_off, emergency_stop, auto_start_mode, service_mode,
              powering_up, ram_error)                     # 62 22 S; .track_power
CvValue(raw_cv: int, value: int, ident: int, z21_form: bool)   # 63 14..17, 64 14
PagedCvValue(raw_register: int, value: int)                    # 63 10
Ready() Busy() NoAck() ShortCircuit() Unsupported() TransferError()   # 61 11/1F/13/12/82/80
ServiceModeEntry() PowerState(on: bool) EmergencyStopBroadcast()      # 61 02, 61 01/00, 81 00
LocoInfo(address, speed, direction, speed_steps, emergency_stopped,
         in_use_by_other, function_bits, raw_speed)              # E4 <ident> SPD FA FB
Other(telegram: bytes)
```

`CvValue.raw_cv` is the field exactly as received; for `64 14 MSB LSB D` the two bytes are combined by `cv.join_cv_field`, so no arithmetic escapes the choke point. The encoding is *not* inferred here — the caller knows which request it issued and supplies it to the matcher.

`PagedCvValue` is not scope creep: 23151 section 3.1.2.6 warns that a decoder may not answer a CV-mode read at all and the station then falls back to register or paged mode. Implementing that fallback is out of scope, but the reply must be recognised so the user gets a sentence instead of hex.

LI-USB interface frames (header `0x01`, 23151 section 1.5):

| Telegram | Meaning | Mapped to |
|---|---|---|
| `01 01 00` | header byte count did not match | `TransportError` |
| `01 02 03` | timeout interface -> command station | `TransportError` |
| `01 03 02` | unknown error, station asked for an ack | `TransportError` |
| `01 04 05` | command forwarded (generic ack) | `GenericAck` |
| `01 05 04` | station no longer addressing the interface | `TransportError` |
| `01 06 07` | interface input buffer overflow | `TransportError` |
| `01 07 06` | station addressing the interface again | informational event |
| `01 08 09` | commands cannot be sent right now | `StationBusyError` |
| `01 09 08` | parameter error, for example a bad loco address | `ValueError` (exit 2) |
| `01 0A 0B` | station did not deliver the expected reply | `LinkProtocolError`, retried once |

Reply header index (length is always `(header & 0x0F) + 2`):

| Header + first data byte | Len | Meaning | Verified |
|---|---|---|---|
| `01 04 05` | 3 | generic ack | doc only |
| `61 00 / 61 01 / 61 02` | 3 | track off / on / service mode entry (usually `FF FD`) | doc only |
| `61 11 / 61 12 / 61 13 / 61 1F` | 3 | ready / short circuit / no ACK / busy | doc only |
| `61 80 / 61 82` | 3 | transfer error / instruction not supported | doc only |
| `62 22 S` | 4 | command station status | **measured** |
| `63 10 REG VAL` | 5 | register or paged result | doc only |
| `63 14 C VAL` | 5 | CV result page 0 (C = 0 means CV1024) | doc only |
| `63 15 / 16 / 17` | 5 | extended CV result, CV256..511 / 512..767 / 768..1023 | doc only |
| `63 21 VER ID` | 5 | software version | **measured** |
| `64 14 MSB LSB VAL` | 6 | Z21-style CV result, usually a broadcast | doc only |
| `81 00 81` | 3 | emergency stop broadcast | doc only |
| `E3 4x ... / E4 ...` | var | loco information | doc only |

Only the two measured rows are facts. Everything else is a hypothesis until `railctl doctor` runs; `Other` plus the wire log is how a wrong row is detected.

**Status bit reading.** The Lenz *XpressNet Protocol Description V2* section 2.1.7 defines bit 0 = emergency off (track power removed), bit 1 = emergency stop, bit 2 = start mode (0 manual, 1 automatic), bit 3 = service mode, bit 6 = powering up, bit 7 = RAM check error; bits 4 and 5 are reserved. The German 23151 manual swaps bits 0 and 1, and neither document defines any bit as "short circuit". The measured `62 22 07` on an unpowered track therefore reads as emergency off + emergency stop + automatic start mode, and the earlier "short circuit" reading is dropped. `raw` is always preserved, so the interpretation can be revised without touching the parser.

### What the Z21 LAN transport will add

No edits expected in `errors.py`, `link.py`, `codec.py`, `address.py`, `speed.py`, `cv.py`, `dialect.py`, the whole `station` layer, or any existing test. Additive only: `transport/udp.py` (`UdpConfig(host, port=21105)`, same `Transport` protocol, one datagram per `read`), `envelope/z21.py` (`Z21Envelope`, `expects_ack = False`, `wrap` = `<DataLen u16le><0x0040 u16le> + telegram`, `pop` slices `DataLen - 4` payload bytes and skips non-`0x0040` packets), new branches in `replies.py` for `64 14` and `EF`, and a transport selector in the CLI. Z21 has no `FF FE` / `FF FD` marker, so `Z21Envelope` classifies frames using the `note_request` / `note_reply` / `note_abandoned` lifecycle that `LiUsbEnvelope` already uses for stray detection: it records the outgoing header, maps it to the reply headers that answer it, labels the first match `SOLICITED` and everything else `UNSOLICITED`. `Link` does not change by one line. The Z21 60-second keepalive is deliberately not designed: the CLI is one-shot.

## Station facade and CV operations

### Position and contract

`station` talks to exactly one object satisfying the `Link` protocol and builds telegrams only through `xbus`. It never sees framing bytes, port names or sockets. `Station` wraps every public method in a `threading.RLock`, so a future TUI cannot interleave two operations; a service-mode operation holds that lock for up to 95 s, and cancellation is out of scope.

Two facts from the LI-USB documentation shape everything below:

- **Exactly one solicited reply per command** — the generic ack, an interface status frame, or the data. There is never an ack followed by data, so "first solicited frame" is always the right frame.
- **Broadcasts are buffered while a command is outstanding** and delivered after the command reply. A passive wait only observes pushes when nothing is in flight.

### Types (`station/types.py`)

`Direction`, `StationVersion`, `StationStatus`, `LocoInfo` and `CvEncoding` are defined in `xbus` and re-exported here. Defined here:

```python
Address = int      # 1..9999
CvNumber = int     # 1..1024, user-facing, ALWAYS 1-based
CvPage = tuple[int, int]     # (CV31 value, CV32 value)

class ProgMode(enum.Enum):
    AUTO = "auto"; POM = "pom"; SERVICE = "service"

@dataclass(frozen=True, slots=True)
class CvSpec:
    cv: CvNumber
    name: str = ""
    page: CvPage | None = None

@dataclass(frozen=True, slots=True)
class CvResult:
    cv: CvNumber
    value: int
    mode: ProgMode              # resolved, never AUTO
    encoding: CvEncoding
    operation: Literal["read", "write"]
    verified: bool | None       # write: read-back confirmed / not attempted; read: None
    elapsed: float

@dataclass(frozen=True, slots=True)
class CvReadOutcome:
    spec: CvSpec
    result: CvResult | None
    error: RailctlError | None

ADDRESS_CVS: Final = frozenset({1, 17, 18, 29})
BLIND_WRITE_CVS: Final = frozenset({1, 8, 17, 18})
CV29_LONG_ADDRESS_BIT: Final = 5
PAGE_SELECTOR_CVS: Final = (31, 32)
INDEXED_CV_RANGE: Final = range(257, 513)
CV144: Final = 144            # meaning depends on the decoder family, see below
DECODER_TYPE_CV: Final = 250  # ZIMO MS family: 6 = MS450, 7 = MS990, 12 = MS491, …
```

**CV144 is family-dependent and must not be treated as a programming lock on MS decoders.** On the older ZIMO **MX** family CV144 is the programming/update lock, and a non-zero value blocks writes. ZIMO **dropped that lock in the MS family** (change log entry 2021-05-12: *"CV #144 (Programm./Update lock): dropped, no longer necessary in new decoders"*), and the CV was later reused: on MS decoders **CV144 bit 4 = 1 activates a confirmation jingle when a CV is programmed** (change log 2024-05-31). The target decoder for 0.1.0 is an **MS450P22**, so on this hardware CV144 is a sound setting, not a lock. Family is decided by reading `DECODER_TYPE_CV`; lock semantics apply only when the decoder is not in the MS family, and `null` (unread) is treated as MS, because guessing "locked" would abort restores that are perfectly safe.

`LocoInfo.speed` is decoded only for the 128-step layout (the only mode `drive()` commands); for 14/27/28 steps it is `None` and `raw_speed` carries the byte. `direction`, `emergency_stopped` and the function bits are valid in all modes.

`BLIND_WRITE_CVS` excludes CV29 deliberately: CV29 only changes the answering address when bit 5 changes, and the commonest reason to write it is enabling RailCom via bit 3, which must be verifiable.

### Facade API (`station/facade.py`)

```python
class Station:
    def __init__(self, link: Link, capabilities: Capabilities, *,
                 default_address: int | None = None,
                 capabilities_path: Path | None = None,
                 timing: Timing = TIMING,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 on_event: Callable[[str, dict[str, object]], None] | None = None) -> None: ...
    @classmethod
    def open(cls, target: str = "auto", *, default_address: int | None = None,
             capabilities_path: Path | None = None, timing: Timing = TIMING) -> "Station": ...

    default_address: int | None
    def close(self) -> None            # flushes capabilities when a path is set; never cuts power
    def __enter__/__exit__

    def version(self) -> StationVersion         # 21 21 00, cached for the object's lifetime
    def status(self) -> StationStatus           # 21 24 05, never cached
    def power_on(self) -> None
    def power_off(self) -> None
    def emergency_stop(self, address: int | None = None) -> None

    def drive(self, address: int, speed: int, direction: Direction) -> None
    def loco_info(self, address: int) -> LocoInfo
    def function_set(self, address: int, function: int, on: bool) -> None
    def function_toggle(self, address: int, function: int) -> bool

    def cv_read(self, cv, *, address=None, mode=ProgMode.AUTO, page=None) -> CvResult
    def cv_write(self, cv, value, *, address=None, mode=ProgMode.AUTO,
                 page=None, verify=True) -> CvResult
    def cv_read_many(self, specs, *, address=None, mode=ProgMode.AUTO,
                     on_progress=None) -> list[CvReadOutcome]
    def select_page(self, page, *, address=None, mode=ProgMode.AUTO, force=False) -> None

    def probe(self, *, address=None, allow_power_on=False,
              use_programming_track=True) -> DoctorReport
    @property
    def capabilities(self) -> Capabilities
```

Semantics that are not obvious:

- **Power.** The LI documentation says a command that produces a broadcast is answered, for the originating device, with that broadcast rather than with `01 04 05`, and that a failed power-on answers track-power-off. Both methods therefore read the solicited reply directly: `61 01` means on, `61 00` means off. A disagreeing reply triggers one `status()` re-read after `TIMING.power_settle`; if it still disagrees, `TrackPowerError`. No unconditional status round trip.
- **Emergency stop.** `None` sends `80 80` (all locomotives); an address sends `92 AH AL XOR`. Track power stays on so the rest of the layout keeps running.
- **Drive.** `speed` 0..126 (0 = braked stop); out-of-range speed or address raises `ValueError`. The drive instruction produces no command station answer of its own; the expected reply is the generic ack.
- **Functions.** Two paths, chosen by capability:
  - **Preferred — single-function command (R5).** When `capabilities.single_function_cmd is True`, `function_set` sends `E4 F8 AdrMSB AdrLSB TTNNNNNN`, where `TT` is `00` off / `01` on / `10` toggle and `NNNNNN` is the function index F0..F28. This touches exactly one function, so no shadow state and no read-modify-write is involved, and a stale shadow can never switch off a function another throttle turned on. `E4 F8` is a Z21 extension rather than classic XpressNet V2, but this station reports command station ID `0x12` (Z21 family), so it is probed as D12 rather than assumed.
  - **Fallback — group commands.** When `single_function_cmd` is `False`, the wire form is `E4 20/21/22/23/28`, which sets **every** function in the group at once. The facade then keeps a per-address shadow of F0..F28, refreshed from a successful `loco_info()` and seeded to all-zeros when that fails (a locomotive the station has never addressed is not an error). This path can clobber functions set by another device between our read and our write; that is inherent to the wire format, not a defect in the design.
  - While `single_function_cmd` is `None`, the group path is used, because it is the one classic XpressNet guarantees.
  - F13..F28 additionally require `capabilities.function_groups_4_5 is True` on the group path; on the single-function path they need only `single_function_cmd`. Otherwise `UnsupportedFeatureError`.
  - `in_use_by_other` does not block anything; it emits `loco.in_use_by_other`.
- **Address encoding** goes exclusively through `encode_loco_address(address, long_threshold=...)`, never inlined. The effective threshold is `capabilities.loco_address_threshold` when set, otherwise `XPRESSNET.long_address_threshold` (100).
- **Address band 100..127 is unresolved.** Classic XpressNet switches to the long form at 100, Z21 at 128, and this station reports command station ID `0x12` (Z21 family). Until the doctor establishes it, addresses in that band use 100 and emit `address.band_unverified`. Addresses below 100 and above 127 are unaffected.
- `address=None` in the CV methods means `default_address`. POM requires an address; service mode ignores it. `mode=POM` with no address anywhere raises `ValueError`.
- `E5` and `E2` loco-info forms raise `UnsupportedFeatureError`.

**`63 10` handling.** XpressNet section 2.1.5.5 says data byte 2 is the register number *or* the CV number if paged mode was used, and that receiving this reply to a direct-mode request means the decoder does not support direct mode. So: record `service_direct_cv = False` in every case; for **CV1..CV8** raise `DecoderNotRespondingError` with the detail "station fell back to register mode; register numbers 1-8 are indistinguishable from these CV numbers, so the value is not usable" (registers are numbered 1..8, so the collision is guaranteed, and the doctor reads CV1 and CV8); for **CV > 8** accept the value only when `raw_register` is in `cv.echo_candidates(SERVICE_DIRECT, cv)`, otherwise raise the same error without the register wording.

### POM CV read

Preconditions, all checked before any telegram is sent: `capabilities.pom_read is False` -> `PomReadUnsupportedError` with no traffic; `status().track_power` false -> `TrackPowerError` ("POM needs the main track powered; run `railctl power on`"); address resolvable; `_ensure_page()` when the CV is indexed.

```
for attempt in 1..TIMING.pom_read_attempts (3):
    link.drain()                                     # kill stale results from a previous CV
    reply = parse(link.request(cmd_pom_read_byte(...), timeout=TIMING.li_ack_normal))
      Unsupported        -> learn pom_read=False, raise PomReadUnsupportedError
      InterfaceStatus    -> map per the table, raise, no retry
      CvValue and match  -> return                   # station answered inline
      otherwise          -> fall through
    outcome = _await_result(matcher, timeout=TIMING.pom_result, first_delay=0.0,
                            interval=TIMING.pom_poll_interval,
                            exchange_timeout=TIMING.li_ack_normal,
                            allow_poll=True, ready_means_done=False, context="pom")
      CvValue      -> CvResult
      ShortCircuit -> ShortCircuitError, no retry
      NoAck        -> record, sleep TIMING.pom_retry_delay, next attempt
      TimedOut     -> record, sleep TIMING.pom_retry_delay, next attempt
after the loop: DecoderNoAckError if any NoAck was seen, else DecoderNotRespondingError
```

`_await_result(matcher, *, timeout, first_delay, interval, exchange_timeout, allow_poll, ready_means_done, context) -> Reply | TimedOut` is the single wait loop shared by POM and service mode. Each pass: drain `link.poll(0.0)` and test every frame with `_consider()`; if polling is enabled, send `cmd_service_result_request()` through `link.request` and dispatch the answer; otherwise wait passively on `link.poll(min(interval, remaining))` and test what comes back. Four details are load-bearing:

- **The passive branch must bind and test its frame.** That branch is only reached after `61 82` disabled polling — precisely the case where the station delivers results as pushes. Dropping the frame there would break POM reads on exactly the behaviour the design exists to support.
- **Every inner exchange is clamped** to `max(TIMING.min_exchange, min(exchange_timeout, remaining))`, and a `LinkTimeout` from an inner poll ends the attempt rather than escaping. Without the clamp one slow poll issued at 5 s overruns a 2 s attempt by 3 s, and three attempts take 15 s.
- **Polling is conditional.** `21 10 31` sent while the station is not in service mode is answered `61 82`, and POM never enters service mode. A station that only pushes POM results answers the first poll with `61 82`; the loop then switches to passive waiting instead of hammering the port. That single branch makes the code correct under both possible YD7010 behaviours.
- **`Ready` is counted, not trusted.** `ready_streak >= TIMING.service_ready_limit` (8) ends the wait with `NoAck()` instead of burning the whole budget, unless `ready_means_done` is set.

`_learn_result_channel(context, channel)` records `pom_result_channel` only when `context == "pom"`. A `61 82` answer to a poll during a POM read is the *expected* response and is never recorded as a durable fact.

**Matching (`CvMatcher`).** A `CvValue` matches when `reply.raw_cv in cv.echo_candidates(encoding, requested_cv, zero_based=capabilities.pom_echo_zero_based)` and the encoding is the one requested. While `pom_echo_zero_based is None` the candidate set has two members; the doctor pins it down by reading CV8, whose two candidate echoes (7 vs 8) are distinguishable. Draining before every attempt is what keeps a two-member candidate set safe; it also discards a late-but-valid answer to the previous attempt, which is the accepted trade.

POM timeouts are short on purpose: a RailCom answer arrives inside one cutout, tens of milliseconds after the packet. 2.0 s per attempt times 3 covers loss on the RailCom channel without making a failing read feel hung. The 2.0 s figure is an estimate from how RailCom works, not a measurement, and is a single constant in `Timing` to be re-measured on the first doctor run.

### POM CV write

POM writes have no feedback in either protocol, and the LI documentation goes further: neither the PC nor the interface can determine whether a command reached the track. The generic ack means only "handed to the command station". Every guarantee comes from reading back.

1. Resolve mode and address; validate `cv` and `value`.
2. `verify` and `capabilities.pom_read is False` -> refuse before sending anything: `PomReadUnsupportedError`, hint "cannot verify POM writes on this station; re-run with `--no-verify` or use `--mode service`". Silently writing unverified data is how a restore corrupts a decoder unnoticed.
3. `verify` and `capabilities.pom_read is None` -> POM-read the target CV **first**, both to establish the capability and to capture the pre-write value. If that read fails, refuse as in step 2, still before writing.
4. `cv == 29` and `verify`: if `(old ^ value) & (1 << CV29_LONG_ADDRESS_BIT)` is non-zero the write changes the long/short address selection, so it is treated as blind; otherwise CV29 is verified normally.
5. `_ensure_page()` when the CV is indexed.
6. `link.request(cmd_pom_write_byte(...), timeout=TIMING.li_ack_normal)`; `61 82` -> `UnsupportedCommandError`; interface status frames mapped per the table.
7. `sleep(TIMING.pom_write_settle)` = 0.5 s. The station repeats the DCC packet an unknown number of times and nothing reports delivery, so this is a floor, not a guarantee.
8. If verifiable: `cv_read(cv, mode=POM)` and compare. On a first mismatch, wait `pom_write_settle` again and re-read once before raising `CvVerifyError(cv, expected, actual)` — an early read is the likeliest false failure.
9. Blind writes (`cv in BLIND_WRITE_CVS`, the CV29 bit 5 case, or `verify=False`) return `CvResult(verified=False)` and emit `cv.write_unverified`.
10. A write to CV8 or to any CV in `ADDRESS_CVS` invalidates the page cache and the cached `LocoInfo` / function shadow for that address.

### Service mode

Differences from POM: addressed by track, not by locomotive, so the `address` argument is ignored; the station enters service mode on receipt, broadcasts `61 02 63`, cuts main track power, and stays there until it receives resume-operations; results come back through the `21 10 31` poll; the 750 mA programming-track limit applies, and a sound decoder that cannot raise a valid ACK pulse yields `61 13` -> `DecoderNoAckError` with the hint "decoder did not acknowledge; sound decoders often fail on a 750 mA programming track — use POM instead".

**Timeout regime.** From the moment a service-mode telegram is sent until `_exit_service_mode()` completes, **every exchange uses `TIMING.li_ack_programming` (95.0 s), not `li_ack_normal`.** The LI rule is that a programming-mode reply may take a minute and no further command may be sent until the previous one is acknowledged. Running a service-mode read at 5 s and polling every 0.5 s would send a new command while the previous one is unacknowledged and desynchronise the link. `service_poll_interval` is a *minimum gap between polls*, never a reply deadline. In practice a service-mode read issues one or two long exchanges, not two hundred short ones. POM is unaffected.

```
power_before = self.status().track_power
telegram, encoding, page_idx = self._service_read_telegram(cv)
try:
    reply = parse(link.request(telegram, timeout=TIMING.li_ack_programming))
    if reply is Unsupported: raise UnsupportedCommandError(...)
    outcome = _await_result(matcher, timeout=TIMING.service_result,
                            first_delay=TIMING.service_first_poll_delay,
                            interval=TIMING.service_poll_interval,
                            exchange_timeout=TIMING.li_ack_programming,
                            allow_poll=True, ready_means_done=False, context="service")
finally:
    self._exit_service_mode(restore_power=power_before)
```

The write sequence is identical with `ready_means_done=True`: after a write `61 11` means the write finished, after a read it only means "no result waiting".

`_exit_service_mode(restore_power)` always runs in a `finally`: send `21 81 A0` (resume operations, which leaves service mode **and energises the main track**); sleep `TIMING.service_exit_settle` and re-read `status()`, retry once if bit 3 is still set, then `StationBusyError`; **if `restore_power` is false, send `21 80 A1`** — not optional, because the measured state of this hardware is an unpowered track, and without this step every service-mode read starts the locomotives moving; finally clear the page cache, because the decoder on the programming track is not necessarily the one on the main track.

Service-mode operations are **never retried automatically**: they already take up to 95 s and the station retries internally. A `Busy` state surviving to the deadline raises `StationBusyError`.

**Mode resolution.** `ProgMode.AUTO` resolves to POM whenever `capabilities.pom_read` is `True` or `None` (unknown: POM is tried and the outcome recorded), and to SERVICE only when `pom_read is False` **and** `service_direct_cv is True`. The facade never switches mid-operation, because that would silently require moving the locomotive. When AUTO cannot proceed the message names the fix: "POM reads are unsupported on this command station; put the loco on the programming track and use `--mode service`". The resolved mode is always reported in `CvResult.mode`.

### CVs above 256

POM natively covers CV1..1024 (10 bits split across `0xE4|MM` and the LSB), but only for writing — POM cannot read here, so nothing in backup, restore or diff uses it. Service mode picks in this order (`_service_read_telegram(cv) -> (telegram, encoding, page_index)`, write side identical):

1. `capabilities.z21_cv_opcodes is True` -> `cmd_z21_cv_read(cv)`, `Z21_16BIT`, page 0. **First, not third.** Measured on the reference station: it covers CV1..1024 in one unambiguous 16-bit field, it is the encoding the LAN transport will use anyway, and its result arrives unsolicited — the only channel that cannot return a stale stored result. The previous order put `service_direct` first, which on this station is the one encoding that answers nothing until separately polled.
2. `cv <= 255` and `capabilities.service_direct_cv is True` -> `cmd_service_direct_read(cv)`, `SERVICE_DIRECT`, page 0. Requires the `21 10` result request.
3. `capabilities.service_ext_cv is True` and `cv <= MAX_CV_EXT` -> `cmd_service_ext_read(cv)`, `SERVICE_EXT`, page `cv // 256`. Also requires the `21 10` request.
4. Otherwise `CvOutOfRangeError`: "CV{cv} is not reachable in service mode on this command station (no extended or Z21 CV opcodes); use `--mode pom`".

Every step requires its capability to be explicitly `True`, never `None`: an unprobed station never sends an opcode that has not been observed to work. Step 2 previously accepted `is not False`, which let an unprobed station lead with the encoding measured silent here.

**Bands above CV511 are documented but never exercised.** Only reply bands `0x63 0x14` (CV1..255) and `0x63 0x15` (CV256..511) have been answered on real hardware. `0x63 0x16` and `0x63 0x17` come from the Lenz document alone. `--all` may still sweep to CV1024, but the JSON output and `doctor` must label those ranges *not exercised on this station* rather than implying they were verified. The extended opcodes are not in the extracted Lenz V2 document at all — they come from XpressNet 3.6, and the page boundaries used here are from a secondary summary. If both turn out negative, high CVs are reachable **only** over POM; the backup profile then marks entries above CV256 as `pom_only` so a service-mode backup reports them skipped rather than failing the run.

### ZIMO indexed CVs (CV31 / CV32)

CV31 and CV32 select which page the CV257..CV512 window maps to. Reading CV265 without selecting the page reads a different setting, silently. `_ensure_page(address, mode, cv, page)`:

- `cv not in INDEXED_CV_RANGE` -> return; a `page` argument is ignored.
- `page is None` -> `IndexPageRequiredError(cv)`.
- Cache hit on `(address, mode)` with the same page and age below `TIMING.page_cache_ttl` -> return.
- Otherwise write CV31 and CV32 through `_raw_cv_write`, which bypasses `_ensure_page` entirely (CV31/CV32 are outside `INDEXED_CV_RANGE`, so no recursion is possible), then — the first time a given page is selected in a session and only when `_reads_available(mode)` — read both back and raise `CvVerifyError(31, ..., detail="index page selection did not stick")` on a mismatch. That is one extra round trip per page group, not per CV. When reads are unavailable the selection cannot be verified and `page.unverified` is emitted.

`TIMING.page_cache_ttl = 10.0 s`, short on purpose: another throttle or the station's own UI can move the cursor, and a stale page yields wrong data with no error. The cache is invalidated on any `RailctlError` from a CV operation, a write to CV8 or to any CV in `ADDRESS_CVS`, `power_off()`, `_exit_service_mode()` and `close()`.

Batch interaction: `cv_read_many` sorts specs by `(spec.page or (0, 0), spec.cv)`, calls `select_page(page, force=True)` at the head of each group and lets the TTL absorb the rest. Restore uses the same grouping and verifies each write **before** moving to the next group, while the page is still selected. CV31 and CV32 are never part of a backup or restore payload — they are a cursor, not a setting — and `PAGE_SELECTOR_CVS` is excluded by the profile loader in addition to the address CVs.

### Capabilities (`station/capabilities.py`)

```python
@dataclass(frozen=True, slots=True)
class Capabilities:
    link_identity: str
    probed_at: str | None                  # ISO-8601 UTC
    xpressnet_version: str | None          # "4.0"
    command_station_id: int | None         # 18 (0x12)
    pom_read: bool | None
    pom_result_channel: str | None         # "broadcast" | "poll" | "none"
    pom_echo_zero_based: bool | None
    loco_address_threshold: int | None     # 100 or 128
    service_direct_cv: bool | None
    service_ext_cv: bool | None
    z21_cv_opcodes: bool | None
    function_groups_4_5: bool | None
    single_function_cmd: bool | None
    notes: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, identity: str) -> "Capabilities": ...
    @classmethod
    def load(cls, path: Path, identity: str) -> "Capabilities": ...
    def save(self, path: Path) -> None: ...
    def with_learned(self, **updates: object) -> "Capabilities": ...
```

`None` means "not established". The file is `~/.config/railctl/capabilities.json`, shape `{"version": 1, "links": {"<identity>": {…}}}`, keyed by `Link.identity` and **not** by a USB serial number: the serial `7010A0001194` comes from the USB descriptor, not from any telegram, and a LAN transport has none. The serial transport returns the USB serial, the LAN transport `host:port`, and a transport with no stable identity returns `"unknown"`, whose capabilities are held in memory only and never written.

Runtime learning is limited to four fields a normal operation can establish without risk — `pom_read`, `pom_result_channel`, `pom_echo_zero_based`, and `service_direct_cv` when a `63 10` reply proves direct mode failed. They are updated in memory and flushed on `close()` when a `capabilities_path` is set. Everything else needs an explicit `railctl doctor` run, because establishing it means sending opcodes a normal operation would never send.

### `Station.probe()` and `railctl doctor`

The first command implemented and the first one the user runs. Every check is read-only against the decoder — **the doctor never writes a decoder CV** — and every service-mode check restores the pre-check track power state.

| # | Check | How | Records |
|---|---|---|---|
| D0 | link | `open_link(target)`, `link.drain()` | `description`, `identity` |
| D1 | link alive | `21 21 00` -> `63 21 40 12` | `xpressnet_version`, `command_station_id` |
| D2 | status | `21 24 05` -> `62 22 S` | decoded bits |
| D3 | track power | from D2; if off and `--power-on` given, `21 81 A0` and re-read; if off without the flag, D4 and D10 are skipped as `unknown` | |
| D4 | POM read | `cmd_pom_read_byte(address, 8)` + the standard wait loop | `pom_read`, `pom_result_channel`, `pom_echo_zero_based` |
| D5 | service direct | `22 15 08 3F` + poll loop, programming track | `service_direct_cv` |
| D6 | Z21 opcodes | `23 11 00 00 32` + poll loop | `z21_cv_opcodes` |
| D7 | extended opcodes | `22 18 08 32`, then `22 19 01 3A` | `service_ext_cv` |
| D8 | RailCom sanity | only when D4 failed with `NoAck` **and** D5 passed: read CV29 and CV28 over service mode | note naming CV29 bit 3 and CV28 bits 0 and 1 set |
| D9 | decoder identity | reads CV7, CV8, CV250, CV1, CV17, CV18, CV28, CV29, CV144 over the best path from D4/D5 | report lines, `decoder_family` |
| D10 | address band | only with an address in 100..127: `E3 00` under both encodings, compare | `loco_address_threshold` |
| D11 | function groups 4/5 | `E4 23 …` and `E4 28 …` with all bits zero on the probe address | `function_groups_4_5` |
| D12 | single-function command (R5) | `E4 F8 AdrMSB AdrLSB 00` — "F0 off", the least disruptive possible single-function telegram | `single_function_cmd` |

Interpretation rules: CV8 is used for D4 because its ZIMO value is a known constant (145), so a plausible-but-wrong value is detectable, and the echoed CV byte (7 vs 8) fixes `pom_echo_zero_based`. `61 13` from D4 keeps `pom_read` at `None` and points at RailCom configuration — a misconfigured decoder must not permanently poison the file. `61 82` to the POM telegram itself sets `pom_read = False`, because that is a property of the station. **No result at all on either channel, with neither `61 13` nor `61 82`, sets `pom_read = False` and `pom_result_channel = "none"` with a note recording that the conclusion came from silence** — leaving it `None` would make every later AUTO operation retry POM for 6 s forever; the note tells the user to re-run the doctor after fixing RailCom. D6 probes the **read** opcode `23 11` only, never `24 12`: `23 11` has no meaning in XpressNet V2, so a station that does not implement it answers `61 82` and nothing happens, whereas probing a write opcode could modify a CV. D7 probes both a low and a high CV because a station could accept the family and refuse pages above the first; `service_ext_cv` is `True` only when both succeed. D5-D8 need a decoder on the programming track and are recorded `None` (unknown), never `False`, under `--no-programming-track`. D10 compares both encodings; `01 09 08` identifies the rejected form immediately, identical replies leave the capability `None` with a note.

```python
@dataclass(frozen=True, slots=True)
class Check:
    id: str; title: str; status: Literal["ok", "fail", "skip", "unknown"]; detail: str

@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...]
    capabilities: Capabilities
    @property
    def ok(self) -> bool: ...     # D0..D2 are "ok" and D3 is not "fail"
```

Doctor exit codes: `0` when `report.ok`, regardless of capability gaps (a missing capability is information, not a failure); `3` when D0 or D1 fails. D3 reporting `unknown` because the track is unpowered without `--power-on` is the expected state of a bench setup and must not map onto the link-failure code. The human output ends with a verdict block, for example:

```
Primary CV path: POM (results arrive as broadcasts)
Fallback:        service mode, direct opcodes, CV1-255 only
CV > 255:        POM only (extended opcodes rejected: 61 82)
Loco addresses:  100..127 unverified (re-run with --address in that range)
```

### Timing and events (`station/timing.py`)

```python
@dataclass(frozen=True, slots=True)
class Timing:
    li_ack_normal: float = 5.0            # per-exchange budget in normal operation
    li_ack_programming: float = 95.0      # per-exchange budget once in service mode
    min_exchange: float = 0.05            # floor when clamping to the remaining budget
    power_settle: float = 0.5
    pom_result: float = 2.0               # per attempt
    pom_poll_interval: float = 0.10
    pom_read_attempts: int = 3
    pom_retry_delay: float = 0.25
    pom_write_settle: float = 0.5         # floor only; nothing reports track delivery
    service_result: float = 95.0          # whole-operation budget
    service_first_poll_delay: float = 0.20
    service_poll_interval: float = 0.50   # minimum gap between polls, not a deadline
    service_ready_limit: int = 8
    service_exit_settle: float = 0.10
    page_cache_ttl: float = 10.0

TIMING: Final[Timing] = Timing()
```

`li_ack_normal` and `li_ack_programming` are the same numbers as `link.DEFAULT_TIMEOUT` and `link.PROGRAMMING_TIMEOUT`; the station always passes an explicit timeout, so `Timing` is authoritative in practice. All constants, plus `clock` and `sleep`, are injected through `Station(...)`, so the unit tests run the 95 s service-mode path in microseconds against a fake link.

Events via `on_event(name, payload)`:

| Event | Payload keys | Meaning |
|---|---|---|
| `cv.stale_result` | `cv`, `raw_cv`, `encoding` | a result belonging to an earlier request arrived |
| `cv.write_unverified` | `cv`, `value`, `reason` | a blind write completed with no read-back |
| `page.unverified` | `page`, `mode` | a page was selected but could not be read back |
| `page.not_selected` | `cv`, `page`, `mode` | a caller-supplied page could not be honoured (service-mode reads cannot select one yet) |
| `loco.in_use_by_other` | `address` | another device controls this locomotive |
| `address.band_unverified` | `address`, `threshold` | address is in 100..127 and D10 has not run |

### Exit codes (`railctl/errors.py`)

`EXIT_CODES: Final[dict[type[RailctlError], int]]` plus `exit_code_for(exc)`, which walks `type(exc).__mro__` and returns 1 for anything unmapped. The map is applied once, by the decorator in `cli/_errors.py` that wraps every command.

| Exit | Condition |
|---|---|
| 0 | success |
| 1 | unhandled internal error (a bug) |
| 2 | CLI usage error (Typer default, `ValueError` from the facade, LI status `01 09 08`) |
| 3 | `TransportError` |
| 4 | `ProtocolError` |
| 5 | `LinkTimeout` |
| 6 | `UnsupportedCommandError` |
| 7 | `UnsupportedFeatureError` |
| 9 | `RailctlError` (base; also covers `StationError` and anything not otherwise mapped) |
| 10 | `DecoderNoAckError` |
| 11 | `ShortCircuitError` |
| 12 | `StationBusyError` |
| 13 | `DecoderNotRespondingError` |
| 14 | `CvVerifyError` |
| 15 | `CvOutOfRangeError` |
| 16 | `PomReadUnsupportedError` |
| 17 | `IndexPageRequiredError` |
| 18 | `ServiceEncodingUnknownError` |
| 19 | `ProgrammingError` (base) |
| 20 | `TrackPowerError` |

A new subclass inherits its parent's code until it is given its own. `tests/test_exit_codes.py` asserts that every concrete subclass resolves to a code, that no two entries share a code, and that nothing falls through to 1.


## Curated ZIMO CV catalog and backup format

### C1. Placement and the one-conversion rule

Two new modules sit **above** `railctl.station`: `railctl.catalog` (curated CV reference data + loader) and `railctl.backup` (file read/write, restore planning). Neither may import `railctl.transport`, `railctl.envelope` or `railctl.xbus`; both talk only to `Station` and to plain integers. A CI test scans their source for those imports and for the literals `FF FE`, `FF FD`, `/dev/`, `0x21`.

Every CV number in the catalog, in the backup file, in the CLI and in config is the **human CV number** (CV1 = 1). All wire conversion happens once, in `railctl.xbus.cv` (`CvEncoding`, `pom_cv_fields`, `direct_cv_byte`, `ext_cv_fields`, `z21_cv_fields`). Likewise loco addresses are plain decimals here; `encode_loco_address(address, *, long_threshold)` owns the XpressNet `>= 100` / Z21 `>= 128` split.

### C2. Catalog location and schema

```
src/railctl/catalog/__init__.py    # loader: CatalogEntry, load_catalog, curated_cvs
src/railctl/catalog/zimo.toml      # the data, shipped as package data
```

Loaded with `importlib.resources.files("railctl.catalog").joinpath("zimo.toml")`. TOML because `tomllib` is stdlib on 3.11+ and because the descriptions are reference data the user must be able to correct against the sheet that came with the decoder — a wrong description is a documentation bug, not a code change. `[tool.hatch.build]` must include `*.toml`, and one test loads the catalog from the installed wheel so a packaging mistake fails CI.

```python
@dataclass(frozen=True, slots=True)
class CatalogEntry:
    num: int; slug: str; desc: str; group: str
    min: int = 0; max: int = 255
    address: bool = False; restorable: bool = True; needs_speed_table: bool = False

CATALOG_FAMILY = "zimo-ms-mx"
CATALOG_SCHEMA = 1

def load_catalog(path: Path | None = None) -> dict[int, CatalogEntry]: ...
def curated_cvs(cat: Mapping[int, CatalogEntry], cv29: int) -> list[int]: ...
```

The file has `[[cv]]` entries and `[[range]]` blocks (`first`, `last`, `index_start`, `slug_template`, `desc_template`, both `str.format` patterns over `i` and `cv`). A `[[cv]]` always wins over a `[[range]]` covering the same number; duplicate `num` or duplicate slug is a load error.

`restorable = false` means exactly one thing: **`restore` never writes this CV from a file.** It makes no claim about whether the decoder accepts a write. `address = true` marks `ADDRESS_CVS` = {1, 17, 18, 29}.

`curated_cvs` requires `cv29`, it is not optional: speed-table entries (`needs_speed_table`) are included only when CV29 bit 4 is set, and the same value drives `loco.kind` and the restore mask. If CV29 cannot be read, the run aborts.

`tests/unit/test_catalog.py` asserts: file parses; `1 <= num <= 1024`; `0 <= min <= max <= 255`; slugs unique and matching `^[a-z][a-z0-9_]*$`; `desc` non-empty, single line, no `"` and no `\`; `address = true` exactly on {1,17,18,29}; `restorable = false` exactly on {7,8,31,32,250,251,252,253}; range templates expand without collision; at least 60 entries (decision 4 says "~60-80", a floor with no ceiling).

### C3. The curated set

59 `[[cv]]` + 3 `[[range]]` blocks. Expanded: **77 CVs by default**, **105** when CV29 bit 4 selects the 28-point speed table.

| CV | slug | description | range |
|---|---|---|---|
| 1 | `primary_address` | Short DCC address, used when CV29 bit 5 = 0 | 1–127 |
| 2 | `v_start` | Starting voltage / lowest motor step | |
| 3 | `accel_rate` | Acceleration rate; higher = slower pickup | |
| 4 | `decel_rate` | Deceleration rate; higher = longer coast | |
| 5 | `v_max` | Maximum voltage at top speed step | |
| 6 | `v_mid` | Mid-point voltage of the 3-point curve | |
| 7 | `decoder_version` | Firmware version, read-only, never restored | |
| 8 | `manufacturer_id` | 145 = ZIMO. Writing 8 factory-resets. Never restored | |
| 9 | `motor_pwm_period` | Motor drive period / EMF sampling; 0 = default | |
| 10 | `reg_rolloff_step` | Speed step where regulation influence drops | |
| 17 | `ext_address_high` | Long address, high byte | 192–231 |
| 18 | `ext_address_low` | Long address, low byte | |
| 19 | `consist_address` | Consist address; bit 7 = reversed in consist | |
| 21 | `consist_fn_f1_f8` | Which of F1..F8 follow the consist address | |
| 22 | `consist_fn_f0` | Which F0 directions follow the consist address | |
| 27 | `dc_brake_config` | Braking on DC / asymmetric DCC (ABC) | |
| 28 | `railcom_config` | RailCom channels; POM readback needs value 3 | |
| 29 | `config_flags` | bit0 dir, bit1 28/128, bit2 analog, bit3 RailCom, bit4 speed table, bit5 long address | |
| 31 | `index_page_high` | Indexed page selector, high. Never written | |
| 32 | `index_page_low` | Indexed page selector, low. Never written | |
| 33 | `fn_map_f0_fwd` | Output mapping for F0 forward | |
| 34 | `fn_map_f0_rev` | Output mapping for F0 reverse | |
| 35–46 | `fn_map_f{i:02d}` | *range*, i from 1: output mapping for F{i} | |
| 56 | `reg_pi` | Regulation: tens digit = P, ones digit = I | 0–99 |
| 57 | `reg_reference` | Regulation reference voltage, 0.1 V units; 0 = auto | |
| 58 | `reg_influence` | Regulation strength, 0 = off | |
| 60 | `output_dim_pwm` | Overall brightness reduction for function outputs | |
| 61 | `fn_map_mode` | ZIMO extended function-mapping selector | |
| 65 | `kick_start` | Extra starting kick pulse | |
| 67–94 | `speed_table_{i:02d}` | *range*, i from 1, `needs_speed_table`: 28-point table | |
| 105 | `user_id_1` | Free user identification byte 1 | |
| 106 | `user_id_2` | Free user identification byte 2 | |
| 121 | `accel_curve_exp` | Exponential shaping of the acceleration curve | |
| 122 | `decel_curve_exp` | Exponential shaping of the deceleration curve | |
| 123 | `adaptive_accel_decel` | Adaptive acceleration / braking | |
| 125 | `effect_headlight_fwd` | Light effect, forward headlight output | |
| 126 | `effect_headlight_rev` | Light effect, reverse headlight output | |
| 127–132 | `effect_fa{i}` | *range*, i from 1: light effect for output FA{i} | |
| 144 | `confirm_jingle` | MS family: bit 4 = 1 plays a confirmation jingle when a CV is programmed. MX family: programming/update lock, non-zero blocks writes and is never itself locked | |
| 250 | `decoder_type` | Decoder family/type id. 6 = MS450, 7 = MS990, 8 = MS590, 12 = MS491, 14 = MS540. Read-only, never restored | |
| 147 | `reg_i_min` | Experimental regulation: minimum I contribution | |
| 148 | `reg_d` | Experimental regulation: D value | |
| 149 | `reg_p_fixed` | Experimental regulation: fixed P contribution | |
| 190 | `fade_in_time` | Light fade-in time | |
| 191 | `fade_out_time` | Light fade-out time | |
| 250 | `decoder_type` | ZIMO decoder type ID, read-only, never restored | |
| 251–253 | `serial_byte_1..3` | Decoder serial bytes, read-only, never restored | |
| 265 | `sound_project_select` | Sound project / load code selection | |
| 266 | `volume_master` | Master sound volume | |
| 273 | `volume_accel` | Volume while accelerating | |
| 274 | `accel_threshold` | Acceleration rate above which `volume_accel` applies | |
| 275 | `volume_constant` | Volume at constant speed | |
| 276 | `volume_decel` | Volume while decelerating | |
| 277 | `decel_threshold` | Deceleration rate above which `volume_decel` applies | |
| 287 | `brake_squeal_threshold` | Deceleration rate above which brake squeal starts | |
| 288 | `brake_squeal_min_speed` | Lowest speed step at which brake squeal plays | |
| 313 | `mute_function_key` | Function key that mutes the sound | |
| 314 | `mute_fade_time` | Mute fade in / out time | |
| 395 | `volume_limit` | Upper bound honoured by the volume up/down keys | |
| 396 | `volume_down_key` | Function key for volume down | |
| 397 | `volume_up_key` | Function key for volume up | |

Unspecified `range` is 0–255. `min`/`max` are **advisory on read** (a decoder value outside the range is stored as `ok` with a note; the decoder is the truth) and **enforcing on write** (`restore` refuses out-of-range file values and aborts before any write, exit 15 `CvOutOfRangeError`).

The 14 curated CVs above 256 (265, 266, 273–277, 287, 288, 313, 314, 395–397) are reachable only through POM (`MAX_CV_POM = 1024`), the extended opcodes (R2) or the Z21 opcodes (R4). `MAX_CV_DIRECT = 255`, so plain service mode cannot reach them; they are emitted as `skipped`. Per-sound volume CVs beyond the named block are deliberately unnamed and left to `--all`.

The catalog covers indexed page (0, 0) only. `INDEXED_CV_RANGE = range(257, 513)` is the band where `PAGE_SELECTOR_CVS = (31, 32)` change what a CV number means; access inside it raises `IndexPageRequiredError` (exit 17) unless the page is known and selected.

### C4. Backup file format

**Decision: JSON, `railctl/backup/v1`.** This reverses the TOML proposal in one source section. The reason is consistency with the house CLI standard: the file is byte-identical to the `result` object of `railctl backup --format=json`, so `--out -` and `--format=json` produce the same document and there is exactly one writer and one schema to test. The catalog stays TOML because it is hand-edited reference data; the backup is generated.

Default path `<backup dir>/loco-0003-curated.json`, i.e. `loco-<address:04d>-<set>.json` where `<set>` is `curated` or `all`. The set is part of the name so a curated run cannot overwrite a full sweep. `--out PATH` overrides; `-` means stdout; a path that exists as a directory gets the generated name appended. The written path is always reported.

```json
{
  "schema": "railctl/backup/v1",
  "created_utc": "2026-08-03T18:42:11Z",
  "tool": "railctl 0.1.0",
  "note": "stock settings",
  "loco": {"address": 3, "kind": "short"},
  "catalog": {"family": "zimo-ms-mx", "schema": 1},
  "set": "curated",
  "mode": "pom",
  "cv_encoding": "POM_ZERO_BASED",
  "page": [0, 0],
  "speed_table_included": false,
  "sweep_range": null,
  "link": {"identity": "serial:7010A0001194:3", "protocol": "xpressnet",
           "protocol_version": "4.0", "command_station_id": 18},
  "capabilities": {"pom_read": true, "pom_result_channel": "poll",
                   "pom_echo_zero_based": true, "service_direct_cv": true,
                   "service_ext_cv": false, "z21_cv_opcodes": false},
  "decoder": {"manufacturer_id": 145, "decoder_version": 34, "decoder_type": 217,
              "serial_bytes": [10, 27, 44]},
  "summary": {"requested": 77, "ok": 76, "no_response": 1, "error": 0,
              "skipped": 0, "complete": false},
  "cvs": [
    {"cv": 1, "name": "primary_address", "status": "ok", "value": 3, "source": "catalog"},
    {"cv": 253, "name": "serial_byte_3", "status": "no_response", "source": "catalog",
     "detail": "no answer after 3 attempts (pom)"},
    {"cv": 397, "name": "volume_up_key", "status": "skipped", "source": "catalog",
     "detail": "cv 397 > MAX_CV_DIRECT 255; extended opcodes unavailable"}
  ]
}
```

Writer rules: fixed key order as above; `cvs` sorted ascending by `cv`; `indent=2`; LF endings; trailing newline; `ensure_ascii=False`; no key omitted from the top level, no `value` key present unless `status == "ok"`. Deterministic output means two runs on an unchanged decoder produce byte-identical files and `git diff` shows one line per changed CV.

An `--all` sweep sets `"set": "all"` and `"sweep_range": [1, 1024]`, keeps catalog slugs for curated numbers, and names everything else `cv0617` with `"source": "sweep"`.

### C5. Status values

```python
class ReadStatus(StrEnum):
    OK = "ok"; NO_RESPONSE = "no_response"; ERROR = "error"; SKIPPED = "skipped"
```

Mapping from the station's `CvReadOutcome` — the backup layer matches on this enum only, never on telegram bytes:

| `CvReadOutcome` | file `status` |
|---|---|
| value returned | `ok` (`value` present) |
| `NoAck` / `DecoderNotRespondingError` after all attempts | `no_response` |
| `Busy` | not surfaced; the station retries, then reports `error` |
| `Unsupported` | `error`, and the capability is recorded once, not per CV |
| short circuit, malformed reply, `LinkTimeout` | `error`, `detail` carries the station's opaque text |
| never attempted (out of reach for the mode, excluded by options) | `skipped` |

**There is no "does not exist" status.** Neither POM-with-RailCom nor service mode has a "no such CV" reply; a missing acknowledgement means an unimplemented CV *or* a decoder that failed to draw enough current for the ACK pulse — exactly what a 750 mA programming track makes likely on a sound decoder. Recording a guess in a file that later drives writes is how a decoder gets corrupted.

Holes are never silent: `value` is absent (not `null`, not `0`) whenever `status != "ok"`; the reader rejects a file where `value` coexists with a non-`ok` status or where `ok` has no `value`; `summary.complete` is `no_response == 0 and error == 0`. **`skipped` does not make a file incomplete** — skips are recorded decisions, and counting them would mark every service-mode backup incomplete purely because 14 sound CVs are out of reach.

### C6. Backup semantics

Order of a run:

1. `station.probe()` → `Capabilities`, or the cached `~/.config/railctl/capabilities.json` entry for this link identity. This is the only place that decides what the hardware can do.
2. Read CV31 and CV32. If either is non-zero the run aborts with `IndexPageRequiredError` (exit 17) unless `--page` was given, which continues and records the page. **The tool never writes CV31/CV32 during a backup or a dry run** — a backup that changes decoder state is not a backup, and on POM the write would be blind with nothing to restore the previous page from.
3. Read CV29. Not `ok` → abort (exit 13). The speed-table decision, `loco.kind` and the restore mask all depend on it.
4. Read CV7, CV8, CV250–253 into `decoder`. A failure here is a hole, not an abort; the field is omitted.
5. Read the rest of `curated_cvs(cat, cv29)` ascending, one at a time, through `Station.cv_read_many`.

If `pom_read` is false, `--mode pom` aborts with `PomReadUnsupportedError` (exit 16) and names the two remedies: enable RailCom on the decoder (CV29 bit 3 = 1, CV28 bits 0 and 1 set), or use `--mode service` with the loco on the programming track. There is no silent fallback: falling back to service mode would read a *different* locomotive if two are on the layout. **`--mode auto` never resolves to the main track for any CV operation.** It resolves to `service` unless `pom_read` has been measured `True`, which on the reference station it has not been. The previous rule resolved to `pom` when `pom_read` was true *or unprobed*, so the default silently selected the one path that returns nothing — and would keep doing so on any station that has never been probed.

Backup exits 9 when `complete` is false and lists the non-`ok` CVs; skips are listed but do not change the exit code. Ctrl-C writes the partial file with `"interrupted": true` and exits 9.

### C7. Restore

#### Stage 0 — identity gate (runs before anything is written)

**Service mode addresses the track, not a locomotive.** Whatever stands on the programming track receives the writes. A restore aimed at the wrong locomotive would write a full CV set into it and report complete success, and the curated set deliberately skips CV1/17/18/29 — removing the one symptom that would have shown up later as a changed address. Nothing in the protocol prevents this; only this gate does.

Before the first write of a mutating programming-track command, read and require:

- **CV8 == the file's `decoder.manufacturer_id`** and **CV250 == the file's `decoder.decoder_type`**. A mismatch is a hard abort with no `--force`.
- **CV251..253 (serial) only when the file carries serial bytes and all three read back live.** Otherwise this degrades to a printed warning naming what did match, plus `--yes`. It cannot be an unskippable gate: those three CVs have **never been read on the reference hardware**, the backup example in this document shows CV253 as `no_response`, and an unsatisfiable safety gate is a broken tool. **Measure them before building restore** (see the open questions) and drop them from the gate if they do not answer.
- A serial mismatch is overridable **only** by `--confirm=<the serial just read>`, never by `--force`. Restoring onto a replacement decoder and copying settings to a second locomotive are legitimate; a slip of the hand is not.

The gate runs once per session, cached, and only for mutating commands — a plain `cv read` must not pay 10 s for it.

#### Verifying a write

The reply that follows a write is `63 14`, which is the **direct-CV read-result** format, not a documented write echo. It shows what the command station produced, not that the decoder retained the value. Worse, `21 10` returns the station's **stored** result: after a write, that store already holds the value the verification read is looking for, echoed against the same CV. A verification that polls can therefore confirm itself.

The rule is provenance, not value comparison:

- Verify **only** through `23 11`, whose result arrives unsolicited. If no unsolicited result arrives, the write is `unverified` — full stop. The verification path never sends `21 10`.
- Before each verification read, read the reference CV (CV8, expect 145) so any late frame from the write is consumed by a read that is not the one being trusted.
- Every read requires the echoed CV to decode back to the CV requested; a mismatch is `cv.stale_result`, discarded, retried once, then failed. Three consecutive stale replies abort the run.
- Fail on the **first** unverified write, not the seventieth.
- `restore --track main` is refused outright — no identity gate and no verification is possible there. `--no-verify` is removed from `restore` and kept only on a single `cv write`.
- `tcflush` drains the host file descriptor only and gives no protection against any of this.

Timing, stated honestly: restore reads every CV first (~2.2 min for 77), writes the few that differ, then verifies each with two reads. **3–4 minutes.** Write latency has never been measured.

Preconditions, all checked before any write:

1. `schema` is `railctl/backup/v1`.
2. Live CV8 equals `decoder.manufacturer_id`, and CV250 plus the three serial bytes match. Any mismatch aborts (exit 9) unless `--force`, which downgrades the serial mismatch to a warning. An unreadable identity aborts rather than guesses. The raw serial bytes are compared, never a composed string.
3. `summary.complete == false` aborts (exit 9) unless `--allow-incomplete`. Non-`ok` entries are never written either way.
4. **Only when `DECODER_TYPE_CV` says the decoder is not MS-family:** live CV144 must read 0, because on MX decoders it is the programming lock. Non-zero aborts (exit 9); the user clears it, the tool does not. On MS decoders (including the MS450P22) CV144 is the confirmation jingle and is not a precondition at all.
5. Live CV31/CV32 equal `page`. No write is performed to reach that state.
6. With `--mode pom` and `pom_read` false, nothing can be verified: abort (exit 16) unless `--no-verify` is passed explicitly, which acknowledges a blind restore.
7. Every value to be written is inside the catalog `min`/`max` (exit 15 with the list otherwise).
8. With `--with-address`: CV1, CV17, CV18 and CV29 must all be `ok` in the file (exit 9 otherwise). A partial address set produces an unreachable locomotive.

Never-write set: {7, 8, 31, 32, 250, 251, 252, 253}. `source == "sweep"` entries are skipped unless `--include-sweep`, which warns that ZIMO uses several CVs as command triggers rather than stored settings.

**Stages**, each fully verified before the next, because a later stage can destroy the ability to verify an earlier one:

- **A — ordinary CVs.** All `ok`, restorable, non-address CVs except 28, 29, 144, ascending.
- **B — CV28 then CV29.** These can switch off the readback path itself. The test is on **bits, not on a whole-byte value**: RailCom is live when CV28 bits 0 and 1 are set and CV29 bit 3 is set. **Measured on the reference decoder: CV28 = 3 and CV29 = 14.** An earlier draft asserted CV28 = 67 and warned that comparing against the literal 3 would wrongly flag a factory-default decoder; that had it backwards, and as written this stage would have aborted a restore on a correctly configured decoder. 67 belongs to large scale decoders (ZIMO MS manual: "CV #28 = 3 (or = 67, if large scale decoder)"). Test the bits and never a whole-byte literal, in either direction. If the target CV28 has bit 0 or bit 1 clear, or the target CV29 has bit 3 clear, and the mode is POM, abort before stage A (exit 9) unless `--allow-railcom-off`; with that flag they are written last in the stage and the remaining CVs are reported as unverifiable rather than as mismatches. CV29 is **skipped by default** (decision 5). `--merge-cv29` opts into a masked write preserving the live long-address bit: `new = (file & ~(1 << CV29_LONG_ADDRESS_BIT)) | (live & (1 << CV29_LONG_ADDRESS_BIT))`. With `--with-address` CV29 is written whole and `--merge-cv29` is refused as contradictory.
- **C — address CVs, only with `--with-address`: CV17, CV18, then CV1.** Last among settings because on POM every later command is addressed by loco number. Afterwards the station re-targets: new address = `((cv17 - 192) << 8) | cv18` when CV29 bit 5 is set, else `cv1`. A long address below 100 aborts *before* stage C: the XpressNet threshold is purely numeric, so a long address in 1..99 cannot be addressed distinctly on this link. Because the address writes were blind (`BLIND_WRITE_CVS` = {1, 8, 17, 18}), failure is diagnosed, not guessed: read CV8 at the new address, then at the old one. An answer at the old address means the write did not take; no answer at either means communication was lost. Both report and exit 14. Nothing is retried at a third address.
- **D — CV144, last of all.** Kept last for the MX case, where a lock written earlier would block every subsequent write including verification retries. On MS decoders the value only controls the confirmation jingle, so the ordering is harmless rather than load-bearing. CV144 is verified by read-back only, and the report says that no retry was possible.

**Verification (R3).** POM writes never have feedback in either protocol, so a write pass alone proves nothing. At the end of each stage: re-read every CV written in that stage, compare against the **intended** value (the masked value for CV29, not the raw file value), retry once on mismatch and re-read once, no further loops. Remaining mismatches are collected and reported as one table; the command raises `CvVerifyError`, exit 14. `--no-verify` skips the pass entirely, emits `cv.write_unverified` for every write, and exits 0.

Nothing is rolled back. A partial rollback can leave a state worse than the observed one; the file plus the mismatch table already say which CVs disagree. Recovery is re-running `restore` (it is idempotent) or CV8 = 8 followed by a full restore.

`--dry-run` performs **no writes at all**, not even to CV31/CV32. It reads every live value and prints a diff. `railctl diff FILE` is the same comparison without the restore framing, and also accepts a second file for an offline file-to-file comparison that never opens a link.

```python
# railctl/backup/plan.py
@dataclass(frozen=True, slots=True)
class PlannedWrite:
    num: int; name: str; stage: Literal["A","B","C","D"]
    file_value: int | None; live_value: int | None; new_value: int | None
    action: Literal["write","unchanged","skip","unreadable"]; reason: str

def plan_restore(records, live, catalog, caps, *,
                 with_address: bool, merge_cv29: bool,
                 include_sweep: bool) -> list[PlannedWrite]: ...
```

`plan_restore` is pure and takes no `Station`, so the dry-run table and the real execution order come from one function and cannot drift. The executor consumes exactly the `action == "write"` entries, in list order, stage by stage.

---

## CLI surface

### L1. Rules the whole surface obeys

The CLI is protocol-free: no module under `src/railctl/cli/` may contain an X-Bus opcode, `FF FE`/`FF FD`, a device path, a baud rate or a UDP port. A CI test greps for them. Everything is a `Station` facade call plus rendering plus exception-to-exit-code mapping in `railctl.cli._errors`.

**Output mode is `--format=human|json|ndjson`**; `--json` is an alias for `--format=json`. In `json` mode **stdout carries exactly one JSON value** — no preamble, no trailing line, no ANSI. In `ndjson` mode stdout carries one compact JSON object per line. All diagnostics, progress, warnings and the `--verbose` frame log go to **stderr**, in every mode.

Colour, spinners and progress bars appear only when the target stream is a TTY. `stdout` and `stderr` are tested separately, so `railctl backup --format=json > f.json` on a terminal still shows a progress bar on stderr while producing clean JSON. `NO_COLOR` (any non-empty value) and `TERM=dumb` force plain text; an explicit `--color=always` still wins over `NO_COLOR`.

**No command ever blocks on an interactive prompt when stdin is not a TTY.** It fails fast with the error object below and a `["...","--yes"]` suggestion.

### L2. Command tree and options

```
railctl [GLOBAL] COMMAND ...

  doctor                    run the capability probe, save ~/.config/railctl/capabilities.json
  status                    command station status (raw byte + decoded bits)
  version                   XpressNet version and command station id
  power on|off              track power
  stop                      emergency stop: all locomotives, or one with --address
  drive SPEED               set speed step 0-126 and direction
  function FUNC [STATE]     set F0-F28 (on|off|toggle)
  monitor                   decode broadcasts and own traffic until Ctrl-C
  cv read CVSPEC...         read CVs
  cv write CV VALUE         write one CV
  backup                    read a CV set, write a backup file
  restore FILE              write CVs from a backup file
  diff FILE [FILE2]         compare a backup against the decoder, or two backups
  schema [COMMAND]          machine-readable manifest of the command tree
```

| Global option | Env | Default | Meaning |
|---|---|---|---|
| `--target` | `RAILCTL_TARGET` | `auto` | `auto`, `serial:<path>`, `z21:<host>:<port>` |
| `--address` / `-a` | `RAILCTL_ADDRESS` | config `address` | locomotive, `LOCO_ADDR_MIN..MAX` = 1..9999 |
| `--format` | `RAILCTL_FORMAT` | `human` | `human`, `json`, `ndjson` |
| `--json` | — | — | alias for `--format=json` |
| `--verbose` / `-v` | `RAILCTL_VERBOSE` | 0 | repeatable: `-v` decoded frames, `-vv` raw bytes, both on stderr |
| `--color` | `NO_COLOR` | `auto` | `auto`, `always`, `never` |
| `--yes` / `-y` | — | false | answer every confirmation yes |

`cv read`, `cv write`, `backup`, `restore` and `diff` take `--mode auto|pom|service`, `--page H:L` (declare the live indexed page instead of aborting) and `--verify` / `--no-verify`. `backup` adds `--all`, `--out`, `--set`, `--note`, `--force`. `restore` adds `--with-address`, `--merge-cv29`, `--include-sweep`, `--allow-incomplete`, `--allow-railcom-off`, `--dry-run`, `--force`. `doctor` adds `--power-on`, `--no-programming-track`, `--no-save`.

**No command takes the locomotive address positionally.** `railctl drive 3 40` and `railctl drive 40 3` are indistinguishable to a human holding a running train; `railctl drive 40 -a 3` is not. Missing address is exit 2 with a suggestion of `["railctl","drive","40","--address","3"]`.

`cv read` and `--only`/`--range` share one grammar, `parse_cv_spec`: `29`; `3-8`; `1,3,29`; a catalog slug such as `accel_rate`. Tokens concatenate, duplicates collapse, first-appearance order is kept. An unknown slug is exit 2 listing the three closest catalog names. A CV above the bound for the resolved mode is `CvOutOfRangeError`, exit 15, naming the bound and suggesting `["railctl","doctor"]`.

### L3. Configuration

`~/.config/railctl/config.toml`, three keys only: `target`, `address`, `verbose`. Precedence per key: **CLI flag > environment > config file > built-in default**. A missing file is not an error; an unparsable file, an unknown key or an out-of-range value is exit 2 naming the file, the line and the key. `RAILCTL_PORT` exists solely to point the hardware test suite at a device and is not read by the CLI.

`~/.config/railctl/capabilities.json` is written only by `doctor` (unless `--no-save`):

```json
{"version": 1, "links": {"serial:7010A0001194:3": {
  "probed_at": "2026-08-04T19:02:00Z", "xpressnet_version": "4.0",
  "command_station_id": 18, "pom_read": null, "pom_result_channel": null,
  "pom_echo_zero_based": null, "loco_address_threshold": null,
  "service_direct_cv": true, "service_ext_cv": true, "z21_cv_opcodes": true,
  "function_groups_4_5": true, "notes": "…"}}}
```

Every capability is tri-state: `true`, `false` (probed, does not work), `null` (not probed or not testable). Keyed by `Link.identity` so two command stations do not share one answer.

The example above carries the values this reference station actually produced (`docs/probe-results.md`), not invented ones. It was written before M1 ran and showed `pom_read: true` with the service and Z21 opcodes `false` — the reverse of the measurement in every one of those four fields. An example is read as a statement about the hardware whether or not it was meant as one, and this one would have taught a reader that POM read works here and that the opcodes reaching CV1024 do not. Note in particular that `pom_read` is `null` and not `false`: the station returned nothing, which is not the same as saying no.

Timing defaults live in `railctl.station.timing` as the `Timing` dataclass `TIMING`; none of them is a CLI flag in v1. **The values are defined once, in "Timing and events" above** — see that section for the table.

They were printed here a second time, and the two copies disagreed on **ten of the fifteen fields**: `li_ack_programming` (95 s against 90), `min_exchange` (0.05 against 0.02), `power_settle` (0.5 against 0.3), `pom_poll_interval` (0.10 against 0.25), `pom_retry_delay` (0.25 against 0.2), `pom_write_settle` (0.5 against 0.2), `service_result` (95 s against 90), `service_first_poll_delay` (0.20 against 0.5), `service_ready_limit` (8 polls against 20) and `service_exit_settle` (0.10 against 0.3). A reader who found this copy first would have called the implementation wrong. The duplicate is removed rather than reconciled: two copies of one table drift again the moment either is edited.

On what the numbers rest, now that M1 has run (`docs/probe-results.md`): the service-mode figures are measured — one service read takes about 1.7 s on this station, which is what `service_result`'s 95 s ceiling and the poll intervals are sized around. The `pom_*` figures are still estimates and will stay estimates here, because POM read on this hardware returns nothing at all to measure. They are not dead settings: POM write works, and a station with a RailCom detector would exercise the read path.

### L4. JSON envelope, NDJSON stream, errors

Every `--format=json` command prints exactly this on stdout:

```json
{
  "schema": "railctl/cv-read/v1",
  "ok": true,
  "command": "cv read",
  "exit_code": 0,
  "elapsed_ms": 412,
  "link": {"identity": "serial:7010A0001194:3", "target": "serial:/dev/cu.usbmodem7010A00011943"},
  "station": {"protocol": "xpressnet", "protocol_version": "4.0", "command_station_id": 18},
  "warnings": [],
  "result": {"mode": "pom", "address": 3, "requested": 2, "ok": 2, "failed": 0,
             "cvs": [{"cv": 1, "name": "primary_address", "value": 3, "status": "ok",
                      "attempts": 1, "elapsed_ms": 131}]}
}
```

`schema` is per command (`railctl/cv-read/v1`, `railctl/backup/v1`, `railctl/doctor/v1`, `railctl/schema/v1`, …). Within a major version only optional fields may be added; a removed or retyped field is `v2`. `ok` means the command did what it was asked; scripts branch on `exit_code`. `link`/`station` are omitted when no link was opened. `warnings` entries are `{"name": "cv.stale_result", "message": "...", "details": {...}}` using the fixed event names `cv.stale_result`, `cv.write_unverified`, `page.unverified`, `page.not_selected`, `loco.in_use_by_other`, `address.band_unverified`.

**Errors** are one JSON object on **stderr**, in every format mode:

```json
{"schema": "railctl/error/v1",
 "code": "pom_read_unsupported",
 "message": "The command station did not return a POM read result for CV 8 on loco 3.",
 "retryable": false,
 "exit_code": 16,
 "details": {"cv": 8, "address": 3, "mode": "pom", "attempts": 3, "attempt_timeout_s": 2.0},
 "suggestions": [["railctl", "doctor"],
                 ["railctl", "cv", "read", "8", "--mode", "service"]]}
```

`code` is the snake_case exception class name. `retryable` is true only for `LinkTimeout`, `StationBusyError` and `PortBusy`. **Every suggestion is an argv array, never a shell string**, so an agent can execute it without a shell. A failed POM read always suggests `["railctl","doctor"]` first, because the usual cause is RailCom off or the track unpowered.

**NDJSON** is mandatory for `backup`, `restore`, `diff` and any `--all` sweep, and is what makes a thousand sequential reads observable and resumable. One compact object per line on stdout, every line carrying `type` and a monotonic `sequence` starting at 0:

```
{"type":"start","sequence":0,"schema":"railctl/backup/v1","address":3,"mode":"pom","total":77}
{"type":"cv","sequence":1,"cv":1,"name":"primary_address","status":"ok","value":3,"elapsed_ms":131}
{"type":"cv","sequence":54,"cv":253,"name":"serial_byte_3","status":"no_response","attempts":3}
{"type":"event","sequence":55,"name":"cv.stale_result","cv":254,"details":{"echoed":7}}
{"type":"summary","sequence":78,"requested":77,"ok":76,"no_response":1,"error":0,"skipped":0,
 "complete":false,"path":"/Users/…/loco-0003-curated.json","exit_code":9}
```

A consumer that dies mid-run knows the last completed CV from the last `cv` line and can restart from it. The `summary` event is always the last line, even on error and on Ctrl-C.

### L5. `railctl schema`

`railctl schema --format=json` prints one `railctl/schema/v1` object: for every command, its path, help text, whether it `mutates` state, its exit codes, and every option with `type`, `default`, `enum` and `required`. `railctl schema cv read` prints the same shape for a single command so an agent need not load the tree.

The manifest is **generated from the same metadata that builds the Typer parser** — one declarative table per command feeds both — so help text and manifest cannot drift. A test asserts every registered Typer command appears in the manifest with matching option names, and that every exit code named in the manifest exists in `EXIT_CODES`.

### L6. Exit codes and safety

The exception-to-code mapping is `railctl.errors.EXIT_CODES` with `exit_code_for(exc)` walking the MRO, so a new subclass inherits its base's code with no table edit. `KeyboardInterrupt` derives from `BaseException` and needs its own entry.

| 0 success | 1 unhandled internal error | 2 usage / `ValueError` / LI `01 09 08` | 3 `TransportError` | 4 `ProtocolError` | 5 `LinkTimeout` | 6 `UnsupportedCommandError` | 7 `UnsupportedFeatureError` | 9 `RailctlError` base (covers `StationError`) | 10 `DecoderNoAckError` | 11 `ShortCircuitError` | 12 `StationBusyError` | 13 `DecoderNotRespondingError` | 14 `CvVerifyError` | 15 `CvOutOfRangeError` | 16 `PomReadUnsupportedError` | 17 `IndexPageRequiredError` | 19 `ProgrammingError` base | 20 `TrackPowerError` |

Consequences: a partial backup exits **9** (`BackupIncompleteError(StationError)`); a restore whose read-back disagrees exits **14**; `diff` exits **0** when it completed and reports `differences` in the payload — it does **not** mirror `diff(1)`, because every non-zero code in this tool is an exception and inventing an exception for "the answer is yes" makes the table lie. Ctrl-C runs cleanup and exits 9 via `AbortedError(RailctlError)`.

Safety rules:

- `power on` sends `cmd_emergency_stop_all` (`80 80`), then `cmd_track_power_on`, then reads status, then sends speed 0 to `--address` when one is resolvable. Only the last step is proven; the stop-first prefix rests on an inference about the station's refresh buffer and is listed in the verification plan. Status **bit 2 is start mode** (0 manual, 1 automatic), never short circuit; `status` prints the raw byte alongside the decoded names.
- `drive SPEED>0`, `function` and every POM `cv` command run a status pre-flight and refuse on emergency-off (`TrackPowerError`, 20), emergency stop (20) or an active service-mode session (12). Without the power check the speed would sit in the refresh buffer and start the train when power returns. `speed 0` skips the pre-flight and is always sent.
- Per-locomotive stop is `92 AH AL XOR`, not `E4 13` with wire value 1.
- `function` reads current state via `cmd_loco_info` and flips one bit, because a group command carries every bit of its group. If loco info fails, exit 9 with a `--force-group` suggestion, which clears the rest of the group.
- Whenever a command exits with a non-zero speed, stderr carries `loco 3 is running at step 30 forward; it keeps running after this command exits`.
- Service-mode commands run their exit path in `finally`: emergency-stop-all, then `power_on` (which is what terminates service mode), then `power_off` if the track was off before. They also print, once, on stderr: `service mode acts on whatever decoder is on the programming track, not on --address`.
- Confirmation is required for `restore`, for `cv write` on {1, 8, 17, 18, 29, 31, 32, 144}, and for any sweep estimated over 60 s. `power on/off`, `drive`, `stop` and `function` are never confirmed — a prompt on every throttle change trains the user to type `-y` reflexively.

### L7. Worked session

```bash
# first contact
railctl status                                  # target auto-resolves, prints raw byte + bits
railctl power on
railctl doctor --address 3                      # answers R1/R2/R4, writes capabilities.json
railctl schema --format=json | jq '.commands[].path'

# driving
railctl drive 30 --address 3                    # keeps current direction, prints running reminder
railctl function light on --address 3           # F0 merged with the live F1..F4 bits
railctl function f2 on --for 1.5 --address 3    # horn on, 1.5 s, off (reverts on Ctrl-C too)
railctl drive 20 --reverse --address 3
railctl drive 0 --address 3

# backup before touching anything
railctl backup --address 3 --note "stock settings" --format=ndjson | tee stock.ndjson
# -> ~/railctl-backups/loco-0003-curated.json, 76/77, exit 9

# tuning
railctl cv read 3 4 29 --address 3
railctl cv write 3 20 --address 3               # verified by read-back
railctl cv write 4 14 --address 3
railctl drive 30 --for 12 --address 3           # test run, auto-stops
railctl diff ~/railctl-backups/loco-0003-curated.json --address 3 --format=json \
  | jq '.result.differences'

# back to stock
railctl restore ~/railctl-backups/loco-0003-curated.json --address 3 --allow-incomplete
railctl stop --address 3
railctl power off
```

Scripted single value, proving both modes carry the same facts:

```bash
railctl cv read accel_rate --address 3 --json | jq -r '.result.cvs[0].value'   # -> 20
```

---

## Testing strategy and project scaffolding

### T1. Repository skeleton

```
railctl/
├── .github/workflows/ci.yml   pyproject.toml  CHANGELOG.md  README.md  LICENSE (MIT)
├── docs/probe-results.md            filled in by milestone M1, committed
├── docs/manual-test-checklist.md    the only tests a human runs
├── src/railctl/
│   ├── __init__.py  __version__ = "0.1.0"   (single source of truth)
│   ├── __main__.py  errors.py  link.py
│   ├── transport/{__init__,serial_posix,fake}.py
│   ├── envelope/{__init__,liusb}.py
│   ├── xbus/{__init__,codec,address,speed,cv,dialect,commands,replies}.py
│   ├── station/{__init__,types,timing,facade,programming,capabilities,doctor}.py
│   ├── catalog/{__init__.py,zimo.toml}
│   ├── backup/{__init__,file,plan,run}.py
│   └── cli/{__init__,main,_errors,deps}.py
└── tests/
    ├── __init__.py  conftest.py  fakes.py  vectors.py
    ├── unit/      codec, address, speed, cv, commands, replies, envelope_liusb,
    │              properties (hypothesis), catalog, backup_file, version
    ├── station/   power_and_status, drive, cv_service_mode, cv_pom, restore, link_errors
    ├── cli/       power, drive, cv, backup_restore, schema, format_modes, wiring
    └── hardware/  conftest.py, test_probe.py  (marker `hardware`, deselected by default)
```

Every test directory carries `__init__.py`; without it `from tests.vectors import …` fails under pytest's default `prepend` import mode.

### T2. pyproject essentials

```toml
[project]
name = "railctl"
dynamic = ["version"]
requires-python = ">=3.11"
dependencies = ["typer>=0.12"]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5", "hypothesis>=6.100,<7", "ruff>=0.5"]
[project.scripts]
railctl = "railctl.cli.main:app"
[build-system]
requires = ["hatchling"]; build-backend = "hatchling.build"
[tool.hatch.version]
path = "src/railctl/__init__.py"
[tool.pytest.ini_options]
testpaths = ["tests"]; pythonpath = ["src", "."]
addopts = "-q --strict-markers --strict-config -m 'not hardware'"
markers = ["hardware: needs the physical YD7010; deselected by default"]
[tool.ruff]
target-version = "py311"; line-length = 100; src = ["src", "tests"]
[tool.ruff.lint]
select = ["E","W","F","I","B","C4","UP","S","RUF"]; ignore = ["E501"]
[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101"]
[tool.coverage.run]
source = ["railctl"]; branch = true
omit = ["src/railctl/transport/serial_posix.py"]
[tool.coverage.report]
fail_under = 90
```

`typer` is the only runtime dependency; the serial port is stdlib `os` + `termios`. `dynamic` version means one copy of the version string. `-m 'not hardware'` in `addopts` is overridden by an explicit `pytest -m hardware`. `serial_posix.py` is omitted from coverage because it has no logic; if it grows past ~60 lines, logic has leaked into it. CI runs ruff check, ruff format --check and pytest on Python 3.11–3.14 on Linux with no serial port attached — every non-hardware test must pass on a machine that has never seen a YD7010.

### T3. Layered strategy

| Layer | How it is tested | Hardware |
|---|---|---|
| `transport.serial_posix` | not unit tested; hardware smoke + manual checklist only | yes |
| `envelope.liusb` | byte-in/frame-out tables + a hypothesis test that arbitrary chunking yields identical frames | no |
| `xbus.*` | exact byte comparison against `tests/vectors.py`, both directions | no |
| `link` | `FakeTransport` + fake clock: one-command-in-flight, ack timeout, unsolicited routing | no |
| `station` | scripted exchanges holding **bare telegrams**; the envelope is a fixture parameter | no |
| `cli` | `typer.testing.CliRunner` with `railctl.cli.deps.open_link` monkeypatched | no |

**Only `serial_posix.py` may touch a file descriptor and only the system clock may touch real time.** Everything above takes transport and clock by constructor injection.

The envelope must not leak upward, and tests enforce it rather than hoping: station and CLI scripts never contain `FF FE`; `make_station` is parametrised over the envelope list (`[LiUsbEnvelope]` today), so adding `Z21Envelope` re-runs the whole station and CLI suite against new framing **with zero test edits**. If any of them needs changing at that point, the layering was violated. Literal `FF FE`/`FF FD` appear in exactly one test file.

`FakeTransport` properties that earn their keep: it asserts the exact request telegram (so an encoder regression fails the driving test without a separate assertion); it knows nothing about framing; it **raises if a second command is written while a reply is outstanding**, making the LI-USB one-command rule a mechanical check rather than a review note; and `chunk_size=1` replays the worst-case USB CDC fragmentation. The whole station suite runs twice, whole-frame and byte-at-a-time. A fake `read()` that returns nothing advances the fake clock by its own timeout, otherwise a link waiting on `monotonic()` spins forever against frozen time and the timeout path is untestable.

CLI binding rule: every command module does `from railctl.cli import deps` and calls `deps.open_link(...)`. A module doing `from .deps import open_link` binds at import time, `monkeypatch.setattr` silently does nothing, and the test opens a real serial port and passes by accident. `tests/cli/test_wiring.py` scans for that import form and fails.

### T4. Golden vectors that carry the load

Full tables live in `tests/vectors.py` as named constants (so an intentional encoder change is one edit, not dozens). Two self-consistency tests run over every row: `xor(b[:-1]) == b[-1]` and `len(b) == (b[0] & 0x0F) + 2`. The rows that exist specifically because they are the top bug sources:

| Case | Bytes | Why |
|---|---|---|
| `cmd_drive_128(99, fwd, 1)` | `E4 13 00 63 82 16` | below the XpressNet threshold |
| `cmd_drive_128(100, fwd, 1)` XpressNet | `E4 13 C0 64 82 D1` | at the threshold |
| `cmd_drive_128(100, fwd, 1)` Z21 | `E4 13 00 64 82 11` | dialects disagree in 100..127 |
| `cmd_drive_128(127, fwd, 1)` XpressNet | `E4 13 C0 7F 82 CA` | top of the divergence band |
| `cmd_drive_128(128, fwd, 1)` both | `E4 13 C0 80 82 35` | dialects agree again |
| `cmd_service_direct_read(1)` | `22 15 01 36` | direct CV is **not** zero-based |
| `cmd_service_direct_read(255)` | `22 15 FF C8` | `MAX_CV_DIRECT` |
| `cmd_service_ext_read(1)` | `22 18 01 3B` | band 0 |
| `cmd_service_ext_read(256)` | `22 19 00 3B` | `22 18 00` is **not** CV256 |
| `cmd_service_ext_read(1024)` | `22 18 00 3A` | CV1024 is page 0 with C = 0 |
| `cmd_pom_read_byte(3, 1)` | `E6 30 00 03 E4 00 00 31` | POM is zero-based |
| `cmd_pom_read_byte(3, 8)` | `E6 30 00 03 E4 07 00 36` | the probe telegram |
| `cmd_pom_read_byte(3, 257)` | `E6 30 00 03 E5 00 00 30` | crosses into MM=1 |
| `cmd_z21_cv_read(29)` | `23 11 00 1C 2E` | 16-bit zero-based |

Decode rows worth naming: `62 22 07 47` → `StationStatus(raw=0x07, emergency_off, emergency_stop, start_mode_auto)`; `63 14 08 08 77` → `CvValue`; `63 10 01 03 71` → `PagedCvValue` (a **valid** answer meaning register-mode fallback, not an error); `61 82 E3` → `Unsupported`; `71 AA DB` → `Other`, no exception.

Hypothesis is used in one file, five properties, `derandomize=True` under `HYPOTHESIS_PROFILE=ci` so a newly discovered example cannot fail an unrelated run: address round-trip per dialect; dialects agree outside 100..127; dialects differ inside 100..127; speed round-trip never colliding with the e-stop wire value 1; single-bit corruption always detected by `xor`. The encoders themselves are **not** property-tested — a property test for `cmd_drive_128` would reimplement it and prove nothing.

### T5. Deterministic POM polling and CLI format tests

The POM read loop is the only asynchronous thing in the tool and must never call `time.sleep`. Its deadline comes from `clock.monotonic()`, never from an iteration count, and its reply `match` has an explicit default that raises. Tests assert the exact sleep sequence, that a transient `NoAck` is tolerated but a persistent one raises `DecoderNotRespondingError`, that `Unsupported` raises `PomReadUnsupportedError`, that a result pushed as an unsolicited broadcast before the first poll is accepted (`pom_result_channel = "broadcast"`), and that a result echoing an unrelated CV emits `cv.stale_result` and is discarded.

CLI format tests are the house-standard enforcement: for every command, `--format=json` produces stdout parsing as exactly one JSON value with a `schema` field and an empty stderr-free stdout; `--format=ndjson` produces N lines whose `sequence` values are `0..N-1` and whose last line is `type: "summary"`; every error path writes one `railctl/error/v1` object to **stderr** with a `code` present in `EXIT_CODES` and suggestions that are lists of strings; `NO_COLOR=1` and a non-TTY stdout both produce zero ANSI escapes; and a confirmation-requiring command with a non-TTY stdin exits without reading stdin. One test per exit-code row. A golden-file test freezes `railctl schema --format=json` so an option rename shows up as a review diff.

### T6. What genuinely cannot be tested in software

Serial port setup (termios flags, DTR/RTS, settle delay after open); R1 in all its parts (does a POM result come back at all, by poll or broadcast, and does the echo carry the CV or the zero-based CVAdr); R2; R4; whether a blind POM write actually took effect; RailCom working end to end; service mode on a 750 mA track with a sound decoder; the real timing tolerances; status bit 2; and anything physical. `tests/hardware/test_probe.py` automates these as a marked suite that **records rather than asserts** — while a capability is unresolved, the test `xfail`s with the reason, so an unproven risk cannot masquerade as a regression. Once `docs/probe-results.md` records a capability as present, the `xfail` becomes a hard assertion in the same commit.

`docs/manual-test-checklist.md`, run before tagging a release, in order: CV250 identifies the decoder family (6 = MS450 on the reference hardware); CV29 bit 3 set and CV28 bits 0 and 1 set (measured values 14 and 3 on the reference decoder); `railctl version` prints XpressNet 4.0 / id 0x12; power on, drive, stop, power off; record the raw status byte on a healthy powered track; drive at addresses 3, 100, 127 and 1234 (100 and 127 are the divergence band and need a decoder set to a long address to answer); F0, F8, F12 and F13/F21 if groups 4/5 probe positive; **service-mode** read of CV8 returns 145 and of CV250 returns 6; write CV3, read it back, restore it; full backup and restore including the skipped address CVs; and two failure drills — loco off the track, and USB unplugged — each checked for a clear message and the mapped exit code.

---

## Risks and verification plan

**R1, R2, R4 and R5 are unproven. Nothing in this spec may assume any of them.** Every code path that depends on one of them is guarded by a `Capabilities` field that starts as `null`, and a `null` capability never becomes a silent assumption: it either takes the conservative branch or raises with a `["railctl","doctor"]` suggestion. The verification below is milestone M1 and runs before any package code is frozen. Results go into `docs/probe-results.md` with the date and the firmware version reported by `21 21 00`, and into `~/.config/railctl/capabilities.json`.

**R1 — does the YD7010 return POM read results over XpressNet?** Procedure, extending `scratchpad/probe_yd7010.py`: track powered, one ZIMO loco at address 3 with CV29 bit 3 = 1 and CV28 bits 0 and 1 set. (a) Send `FF FE E6 30 00 03 E4 07 00 36` (POM read of CV8) and log every byte for 5 s without polling — this answers whether the result arrives as an `FF FD` broadcast. (b) Repeat, this time polling `FF FE 21 10 31` every 250 ms for 5 s — this answers whether it arrives by poll. (c) Note which CV byte the `63 14` result carries: `07` means zero-based, `08` means one-based. That single byte sets `pom_echo_zero_based` and decides whether `CvMatcher` can validate the echo at all. (d) Repeat for CV265 and CV266, two CVs above 256 with known different values, and check that the returned values are not those of CV9 and CV10 — the result telegram carries only 8 bits of CV address, so a station that truncates would return a neighbour in the same 256-block with no error. Outcomes: `pom_read`, `pom_result_channel` (`broadcast`/`poll`/`none`), `pom_echo_zero_based`, plus a measured value for `pom_result`. If (a) and (b) both fail, `pom_read = false`, POM becomes unavailable, `--mode auto` resolves to `service`, and every POM path raises `PomReadUnsupportedError` (exit 16) with the RailCom and programming-track remedies. The product still works; every CV touch then needs the programming track.

**R2 — are the extended CV opcodes implemented?** Loco on the programming track. Send `FF FE 22 18 01 3B` (extended read of CV1) and compare with `FF FE 22 15 01 36` (direct read of CV1). Equal values → `service_ext_cv = true`. `61 82 E3` → `false`. Then confirm the band split with `22 19 00 3B` (CV256) and `22 19 2C 17` (CV300). If false, service mode is capped at `MAX_CV_DIRECT = 255`, the 14 curated sound CVs above 256 are emitted as `skipped` in every service-mode backup, and `cmd_service_ext_read/write` raise `UnsupportedCommandError` before touching the wire.

**R4 — does the station accept Z21-style CV opcodes over XpressNet?** Same setup. Send `FF FE 23 11 00 1C 2E` (Z21 service read of CV29, 16-bit zero-based) and compare with the direct read of CV29. A matching value → `z21_cv_opcodes = true`, which raises the service-mode ceiling to `MAX_CV_Z21 = 1024` and gives a full 16-bit echo that closes the high-CV validation gap R1(d) leaves open. `61 82 E3` → `false`. This is the probe most worth running: the station reports command station ID 0x12, the Z21 family id, so a positive result simplifies the dialect split considerably.

**R5 — does the station accept the single-function command `E4 F8` over XpressNet?** Track powered, one locomotive addressed. `E4 F8 AdrMSB AdrLSB TTNNNNNN` sets exactly one function, where `TT` is `00` off / `01` on / `10` toggle and `NNNNNN` is the index F0..F28. It is documented for Z21 (spec V1.13 §4.3.1) but has no meaning in classic XpressNet V2, so a station that does not implement it answers `61 82 E3` and nothing reaches the track.

Probe procedure, designed so a negative result changes nothing on the layout: call `loco_info()` first, read the current state of F0, then send `E4 F8` commanding F0 to **the value it already has**. For address 3 with F0 currently off that is `FF FE E4 F8 00 03 00 1F` (XOR `E4^F8^00^03^00 = 1F`). Any reply other than `61 82 E3` → `single_function_cmd = true`; `61 82 E3` → `false`; silence leaves it `null`.

A positive result is worth more than it looks. On the group path (`E4 20/21/22/23/28`) every function command asserts all eight bits of its group, so the facade must maintain shadow state and any drift between the shadow and reality silently switches a function off — the failure the section review flagged as most likely to bite in practice. `E4 F8` removes the shadow, the read-modify-write, and that entire failure mode. It also makes F13..F28 reachable without depending on `function_groups_4_5`.

**R3 — POM writes never have feedback, in either protocol.** This is not a risk to be resolved, it is a permanent property, so it is mitigated by design rather than probed. Restore writes blind and verifies by read-back, per stage, against the *intended* value, with one retry per mismatch. `--no-verify` is the only way to run a POM restore when `pom_read` is false; it emits `cv.write_unverified` for every write and says in the summary that nothing was checked. `doctor --allow-write` records `pom_write` as `null`, never `false`, when `pom_read` is not `true`, because an unobservable write cannot be proven to have failed. The write probe changes the value first (`original ^ 1`) and only then restores it: writing a CV back to the value it already holds and reading the same value proves nothing, since a decoder that ignores the write entirely produces an identical reading.

**Partial mitigation available on this specific decoder.** ZIMO MS decoders implement a *confirmation jingle*: with **CV144 bit 4 = 1** the decoder plays a short sound whenever a CV is programmed. On the MS450P22 that turns an unobservable POM write into an audible one. It is not a protocol acknowledgement — nothing reaches the tool and nothing can be asserted in a test — so it does not change any code path, and read-back verification remains the mechanism the tool relies on. It is worth surfacing in the troubleshooting docs, because "I heard nothing when restore ran" is a fast human diagnosis that the writes are not landing at all. `railctl doctor` reports the current CV144 bit 4 state so the user knows whether to expect the sound.

Secondary items to settle during M1, each with a one-line consequence: **status bit 2** is start mode per the Lenz spec, not short circuit — read the raw byte on a healthy powered track, and if bit 2 is set there, the label is right and the `power on` stop-first rationale loses its strongest justification; **`power on` refresh-buffer behaviour** — loco at step 30, `power off`, `power on`, observe whether it moves, which decides whether the `80 80` prefix is a real guard or decoration; **the 100..127 address band** — a decoder set to a short address in that range will silently not respond, so `address.band_unverified` is emitted once per session for addresses in it until the band is confirmed on hardware; **`function_groups_4_5`** — probe `E4 23` and `E4 28` and set `MAX_FUNCTION` behaviour accordingly, since `61 82 E3` there means F13–F28 raise `UnsupportedFeatureError` (exit 7).

---

## Open questions

1. **ZIMO sound-CV descriptions — partly resolved.** The official ZIMO MS manual confirms **CV265 = sound project / loco type selection** (used with sound collections, e.g. 1 = BR50, 2 = BR78, 101 = BR211) and **CV266 = master volume, default 60, typical working range 40–90**. It also confirms **CV147/148/149 are PID settings** that replace CV56 when any of them is non-zero — so CV56's description must say "only effective while CV147 = CV148 = CV149 = 0". Still open: CV273–277, CV287, CV288, CV313, CV314 and CV395–397 are specific to the *sound project* loaded on the decoder, not to the decoder model. Can you supply the CV sheet for the sound project actually installed on your MS450P22? Without it those descriptions ship as best-effort. The numbers are stable regardless, so this affects labels only.
2. **Speed step modes other than 128.** `drive` always sends the 128-step telegram, which also forces the station into 128-step mode for that locomotive. Does any locomotive on your layout need 14, 27 or 28 steps? If not, this stays out permanently.
3. **Telemetry port.** `/dev/cu.usbmodem…5` streams `[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C CT 35.7'C CA 26 CB 08`. Parsing it would put real track current, voltage and temperature into `railctl status` and make short-circuit diagnosis obvious. Worth a second transport in 0.1.0, or leave `monitor` printing the raw lines?
4. **Serial number byte order.** `serial = CV251<<16 | CV252<<8 | CV253` is an assumption. The raw bytes are stored too and identity checks compare the raw bytes, so a wrong composition is cosmetic — but can you confirm it against the sticker on the decoder?
5. **Additional curated sets.** Only `curated` (the 77-CV set above) is defined; the `--set` mechanism supports more. Which extra sets are worth curating depends on what you actually tune — motor-only and sound-only are the obvious candidates.

---

## Implementation order

Each milestone is independently verifiable and leaves the tree green.

**M1 — hardware capability probe (no package code).** Extend `scratchpad/probe_yd7010.py` with the R1/R2/R4/R5 procedures plus the status-bit, refresh-buffer and address-band checks. Verify: `docs/probe-results.md` committed, with the firmware version, the date, and a definite `true`/`false`/`null` for `pom_read`, `pom_result_channel`, `pom_echo_zero_based`, `service_direct_cv`, `service_ext_cv`, `z21_cv_opcodes`, `function_groups_4_5`, `single_function_cmd`, and measured values replacing the `pom_result` and `pom_poll_interval` estimates.

**M2 — scaffolding.** Repo, `pyproject.toml`, ruff config, CI matrix, `errors.py` with the full exception tree and `EXIT_CODES`, `tests/` skeleton with `__init__.py` everywhere. Verify: `ruff check`, `ruff format --check` and `pytest` all pass on an empty suite plus `test_version.py`; CI green on 3.11–3.14.

**M3 — xbus codec and vectors.** `codec`, `address`, `speed`, `cv`, `dialect`, `commands`, `replies`, and `tests/vectors.py` populated from the tables in T4. Verify: every encode vector matches byte for byte, every decode vector compares equal as a dataclass, and both self-consistency tests pass over the whole table. No hardware needed.

**M4 — transport, envelope, link.** `serial_posix`, `fake`, `liusb`, `Link` with the one-command-in-flight rule, `LinkStats`, resync counters. Verify: the envelope test file passes including byte-at-a-time feeding and the checksum-resync case; `FakeTransport` raises on a pipelined write; `railctl.link.open_link("auto")` finds and identifies the real port by hand (`find_xpressnet_port()`), and opening the telemetry port instead shows `bytes_dropped` climbing with `frames_ok` stuck at 0.

**M5 — station facade.** `Station.open`, power, status, version, drive, function, loco_info, `CvProgrammer` with both POM and service paths wired to whatever M1 found, `Capabilities`, `probe()`, `doctor`, `Timing`. Verify: the whole `tests/station/` suite passes under both chunk sizes and both envelope parameters; on hardware, power on / drive 30 / stop / power off moves and stops one locomotive.

**M6 — CLI core with the house output contract.** `main`, `_errors`, `deps`, plus `doctor`, `status`, `version`, `power`, `stop`, `drive`, `function`, `monitor`, and `schema` generated from the shared command metadata. Verify: the format-mode test suite passes (one JSON value on stdout, error object on stderr, NO_COLOR, non-TTY stdin); `railctl schema --format=json` round-trips against the registered Typer tree; `railctl doctor --address 3` writes `capabilities.json` matching M1 by hand.

**M7 — catalog.** `zimo.toml` with the 77-CV curated set, `load_catalog`, `curated_cvs`, and the validation tests including the installed-wheel load. Verify: `tests/unit/test_catalog.py` green; the entry count is at least 60; address and non-restorable sets match exactly.

**M8 — `cv read` / `cv write`.** `parse_cv_spec`, mode resolution, page handling, verify-after-write, CV-out-of-range errors with the bound named. Verify: reading CV1, CV3, CV8 and CV29 on the bench returns plausible values; writing CV3 and reading it back agrees; a CV above the mode's bound exits 15 with a `doctor` suggestion.

**M9 — backup.** `railctl/backup/file.py` writer and reader, the `railctl/backup/v1` schema, NDJSON event stream, partial-file-on-Ctrl-C. Verify: two consecutive backups of an unchanged decoder are byte-identical; a run with a deliberately unreadable CV produces `status: "no_response"` with no `value` key, `complete: false` and exit 9; the NDJSON stream's sequence numbers are contiguous and end in a `summary`.

**M10 — restore and diff.** `plan_restore` (pure), the four-stage executor, per-stage verification, `diff` in both online and offline forms. Verify: `restore --dry-run` and the real run produce the same plan; a hand-changed CV3 is restored and verified; the report lists CV1/CV17/CV18/CV29 as skipped; `--with-address` writes them last and re-targets; a forced mismatch exits 14 with the mismatch table.

**M11 — `--all` sweep and release.** Sweep bounds from `Capabilities`, the >60 s confirmation with re-estimation after the first 10 reads, progress on stderr only. Verify: a full sweep completes with a recorded wall-clock time and unreadable count; the manual checklist passes end to end; CHANGELOG `## [0.1.0]` written by hand, `chore(release): v0.1.0`, tag `v0.1.0`.