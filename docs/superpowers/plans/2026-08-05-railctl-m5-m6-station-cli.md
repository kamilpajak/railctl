# railctl M5-M6 — Station Facade and CLI Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a working tool in the operator's hands. M5 builds the `station` facade — the only layer that turns "what the user asked for" into telegrams, and the only layer allowed to decide what a reply *means*. M6 wraps it in a CLI that is a versioned API with a human renderer bolted on top, not the other way round: `railctl status`, `version`, `power`, `stop`, `drive`, `function`, `monitor`, `doctor` and `schema`, each producing the same facts in human, JSON and NDJSON form, with a documented exit code and a machine-readable error object on stderr.

**Architecture:** Two new layers on top of the four M2–M4 delivered. `station/` talks to exactly one object satisfying the `Link` protocol and builds telegrams only through `xbus`; it never sees framing bytes, port names or sockets. `cli/` talks only to `station/` plus rendering plus exception-to-exit-code mapping; it holds no opcode, no device path and no CV arithmetic. Both rules are enforced mechanically by `tests/test_layering.py`, which greps `station/` and `cli/` on every run — M5 and M6 are the first code those two guards have ever measured, and `test_the_rule_1_and_2_targets_are_scanned_once_they_exist` starts reporting real coverage the moment `src/railctl/station/` appears. Inside `station/`, `facade.py` owns the lock, the link and the session; `programming.py` owns every CV path and the single shared wait loop; `capabilities.py` owns the tri-state record of what this station was measured to do; `doctor.py` owns the procedure that establishes those facts. Inside `cli/`, exactly one internal result object (`railctl.cli.result.CommandResult`) is built per command and rendered three ways, so the human and the JSON output cannot drift apart, and one metadata table (`railctl.cli._meta.COMMANDS`) feeds both the Typer parser and `railctl schema`.

**Tech Stack:** Python 3.11+ (developed on 3.13, CI on 3.11–3.14), `typer` as the only runtime dependency, stdlib `tomllib`/`json`/`logging`/`threading` for everything else, hatchling for the build, uv for dependency management, pytest + hypothesis + ruff for development. macOS only. No `rich`, no `pyserial`, no `click` beyond what typer brings.

## Global Constraints

Every task's requirements implicitly include this section.

**Toolchain**

- Dependencies are managed by **uv**. Every command in this plan is `uv run pytest …`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run railctl …`. Never `python -m pip`, never a bare `pytest`.
- `pyproject.toml`'s `addopts` already carries `-q --strict-markers --strict-config -m 'not hardware'`. **Never add `-q` to a pytest command in this plan**: a second `-q` takes the quiet level to 2, at which pytest prints no summary line, and the `N passed` every step tells you to compare against would never appear.
- Runtime dependencies: **`typer` only**. Everything else is stdlib.
- Python 3.11+; `tomllib` is stdlib from 3.11 and is what reads `config.toml`. No `tomli` fallback, no `toml` package.
- Platform: macOS only.

**State of the tree when this plan starts**

- `main` at `2c9d3a9`, milestones M2–M4 merged and green: **920 tests, coverage 99.31 %**. Every task must leave the suite green and coverage at or above the gate.
- Coverage gate: `fail_under = 90`, `branch = true`, `source = ["railctl"]`, `omit = ["src/railctl/transport/serial_posix.py"]`. Nothing in M5 or M6 is omitted, so every branch of `station/` and `cli/` needs a test. Lowering `fail_under` is not an option; neither is handing a red build to the next task.
- On disk and authoritative — quote these, never invent a variant: `railctl.link.Link` (`open/close/request/send/send_no_reply/await_frame/poll/drain/stats/recent_events/recent_late_replies`, properties `description/identity/version_telegram`, module constants `DEFAULT_TIMEOUT = 5.0`, `PROGRAMMING_TIMEOUT = 95.0`, `HANDSHAKE_TIMEOUT = 2.0`), `railctl.transport.open_link/transport_for/find_xpressnet_port`, the whole `railctl.errors` tree with `EXIT_CODES` and `exit_code_for`, and `railctl.xbus.{codec,address,speed,cv,dialect,commands,replies}`.

**The layering rules — `tests/test_layering.py`, read it before writing a line**

1. No `ff fe`, `ff fd`, `\xff\xfe`, `\xff\xfd`, `cu.usbmodem`, `baud`, `termios`, `socket`, or `\btty` (case-insensitive, word start) anywhere under `station/` or `cli/`. **The word "TTY" is forbidden in `cli/` even inside a comment or docstring** — `\btty` matches it. Write "terminal" in prose and `stream.isatty()` in code; `isatty` and `pretty` have no word boundary before `tty` and are safe.
2. No `cv - 1`, `cv + 1`, `% 256`, `>> 8`, `<< 8` under `station/`, `cli/` or `xbus/commands.py`. All CV and band arithmetic lives in `xbus/cv.py`. If a task needs a new conversion, it adds a function to `xbus/cv.py`; it does not inline the arithmetic.
3. Only `errors.py` may define a class whose name ends in `Error`, `Exception` or `Timeout` **and declares a base**. A frozen dataclass with no base is not a hit, whatever it is called.
4. No `/dev/` and no `usbmodem` outside `transport/`.

**The failure mode this project keeps committing**

A capability recorded as absent because of a defect in the instrument measuring it. Three outcomes — **true**, **false** and **unknown** — must stay distinguishable end to end, in the dataclass, in the JSON (`true` / `false` / `null`), in the human text (`yes` / `no` / `unknown`) and in the exit code. On this hardware a POM CV read returns **nothing at all** — only the interface ACK `01 04 05`, never `61 13`, never `61 82` (`docs/probe-results.md`, R1). That is `unknown`. Only `Unsupported` (`61 82`) entitles any layer to write `false`, with the one deliberate exception the doctor makes in D4, which is recorded together with a note saying the conclusion came from silence.

**Measured facts that constrain M5 (`docs/probe-results.md`, 2026-08-04)**

- Command station YaMoRC YD7010, XpressNet **4.0**, id **0x12** (Z21 family). Start mode is **automatic**: locomotives resume their last speed when power returns.
- Service mode on the programming track **works**. `23 11` (Z21, 16-bit, zero-based request, one-based echo) delivers its result **unsolicited**; `22 15`, `22 18` and `22 19` deliver only after `21 10 31` is sent. One service read takes about **1.7 s**.
- POM **write** works; POM **read** returns nothing.
- `E4 F8` single-function works, function groups 4 and 5 work, `E3 09 → E3 52 D1 D2` reads F13–F28.
- Reply bands `63 14` (CV1–255) and `63 15` (CV256–511) are the only ones ever answered. `63 16` and `63 17` come from a document alone and must be labelled *not exercised on this station*.

**Timing constants — the M5 table governs**

`Timing` carries exactly these values: `li_ack_normal = 5.0`, `li_ack_programming = 95.0`, `min_exchange = 0.05`, `power_settle = 0.5`, `pom_result = 2.0`, `pom_poll_interval = 0.10`, `pom_read_attempts = 3`, `pom_retry_delay = 0.25`, `pom_write_settle = 0.5`, `service_result = 95.0`, `service_first_poll_delay = 0.20`, `service_poll_interval = 0.50`, `service_ready_limit = 8`, `service_exit_settle = 0.10`, `page_cache_ttl = 10.0`. See "departures".

**The CLI output contract (`~/Developer/CLAUDE.md`, binding, not in the spec)**

- `stdout` carries the result only. Logs, progress, warnings and errors go to `stderr`, in every format mode.
- In `json` mode `stdout` holds **exactly one JSON value** — no preamble, no trailing line, no ANSI. In `ndjson` mode, one compact object per line, each with `type` and a monotonic `sequence` starting at 0, always ending in a `summary`.
- Every envelope carries a `schema` field. Within a major version only optional fields may be added; never rename a field, change a type or a unit, or repurpose an error code.
- Errors: one JSON object on stderr with `schema`, `code` (machine-readable, never renamed), `message` (free to change), `retryable`, `exit_code`, `details`, `suggestions`. **Every suggestion is an argv array, never a shell string.**
- Colour, spinners and progress only when *that* stream is a terminal; `stdout` and `stderr` are tested separately. `NO_COLOR` (any non-empty value) and `TERM=dumb` force plain text; `--color=always` still wins over `NO_COLOR`.
- When `stdin` is not interactive, act non-interactive automatically: never read `/dev/tty`, never block, fail fast with an actionable error carrying a `--yes` suggestion.
- Exit-code set is small, documented and fixed: `0` ok, `2` usage, `3` transport, `4` protocol, `5` timeout, `6` refused, `7` out of scope, `9` base, `10`–`20` domain. Domain detail belongs in `error.code`, never in a new exit code.

**Process**

- Conventional Commits (`type(scope): description`). **Never mention AI or assistance** in a commit message, body or list.
- Test-first, always: write the failing test, RUN it and see it fail for the named reason, write the implementation, run it and see it pass, run `uv run ruff check .` and `uv run ruff format --check .`, commit.
- Each task states the test count its steps should report and the running suite total. Those numbers are computed by reading the test code, not by executing it — a small disagreement is an arithmetic slip in the plan and the first execution corrects it in place; a *different failing test* is a real signal.
- Hardware tests carry `pytestmark = pytest.mark.hardware` and are deselected by default. They are run by hand with `uv run pytest -m hardware -s`.

---

## Scope note — where this plan sits

Plan 1 delivered M1, the standalone capability probe. Plan 2 delivered M2-M4: the package, the X-Bus codec with its golden vectors, and the transport, envelope and link layers. This is Plan 3.

| Plan | Milestones | Deliverable |
|---|---|---|
| 1 (done) | M1 | Probe tool + `docs/probe-results.md` measured on hardware |
| 2 (done) | M2-M4 | Package scaffolding, X-Bus codec, transport/envelope/link |
| **3 (this one)** | **M5-M6** | Station facade, CV programmer, doctor, and the CLI core |
| 4 | M7-M8 | ZIMO catalog, `cv read` / `cv write` |
| 5 | M9-M11 | Backup, restore, diff, release |

The tree this plan starts from is `main` at `2c9d3a9`: 920 tests, coverage 99.31%, CI green on Python 3.11 through 3.14, and the M4 acceptance measured on the real YD7010 on 2026-08-05.

## Where this plan departs from the spec, and why

Eleven, each recorded here so no task author re-opens it.

1. **Timing constants: the M5 table (spec lines 892-912) governs, not the L3 summary (lines 1287-1296).** The two disagree on ten of fifteen fields (`li_ack_programming` 95 vs 90, `service_result` 95 vs 90, `service_ready_limit` 8 vs 20, `pom_write_settle` 0.5 vs 0.2, `min_exchange` 0.05 vs 0.02, `power_settle` 0.5 vs 0.3, `pom_poll_interval` 0.10 vs 0.25, `pom_retry_delay` 0.25 vs 0.2, `service_first_poll_delay` 0.20 vs 0.5, `service_exit_settle` 0.10 vs 0.3). The M5 table is the normative one, its prose names `service_ready_limit` as 8 inline, and its 95.0 s matches `link.PROGRAMMING_TIMEOUT = 95.0` already on disk. A test pins `Timing().li_ack_programming == link.PROGRAMMING_TIMEOUT`.

2. **`cli/` gains modules beyond the spec's `{__init__, main, _errors, deps}`.** `result.py`, `render.py`, `config.py`, `_meta.py` and `commands/` exist because `~/Developer/CLAUDE.md` requires one internal result object rendered two ways and one metadata source feeding both parser and manifest. Putting nine commands, the config reader, the renderers and the manifest generator in `main.py` would make that single source unreviewable.

3. **Two functions are added to the `xbus` layer during M5.** `cmd_function_single` (E4 F8) does not exist on disk and both the preferred function path and doctor check D12 need it; `result_ident_for(cv, encoding)` is added to `xbus/cv.py` because the CV matcher must check the reply *band* and layering rule 2 forbids band arithmetic in `station/`. Both are additions to the layer that already owns opcodes and CV arithmetic, not a widening of `station/`.

4. **The function shadow never seeds zeros blindly.** Spec line 697 says the F0..F28 shadow is "seeded to all-zeros when `loco_info` fails". The hardware measurement that came after the spec (`E3 09 → E3 52 D1 D2`, `docs/probe-results.md`, "Settled": "closes the blind-clear side effect") makes that unnecessary. Functions whose state was never read are **absent from the state map, not False**, and the group path refuses to write a group containing an unknown function unless `force_group=True`. A defaulted False here would switch off a function another throttle turned on, which is the project's failure mode wearing a different hat.

5. **`Station.events()` is added to the facade.** The spec's facade list omits it but M6 requires a `monitor` command, and layering rule 1 forbids the CLI from touching frames. `events()` is the station-level API that turns polled frames into decoded `StationEvent` objects.

6. **`AbortedError` and `ConfirmationRequiredError` are added to `errors.py`, and `KeyboardInterrupt` is not added to `EXIT_CODES`.** Spec line 1358 says `KeyboardInterrupt` "needs its own entry", but `EXIT_CODES` is typed `dict[type[RailctlError], int]` and `tests/unit/test_exit_codes.py::test_no_entry_in_the_map_is_orphaned` asserts `set(EXIT_CODES) <= _tree()`, so a `BaseException` row would fail a merged test. The CLI catches `KeyboardInterrupt` and converts it to `AbortedError(RailctlError)`, which resolves to 9 exactly as spec line 1362 requires. `ConfirmationRequiredError` gets the one new row, `2`, because a refused confirmation is a usage error.

7. **`--non-interactive` is added to the global options.** The spec's table has only `--yes`. `~/Developer/CLAUDE.md` requires an explicit non-interactive switch that is *not* an implicit yes; `--yes` answers every confirmation yes, `--non-interactive` refuses to prompt and fails with a `--yes` suggestion. When `stdin` is not interactive, `--non-interactive` is implied.

8. **`pom_result_channel` records `"broadcast"` when the station answers the POM read telegram inline.** The field has three documented values and the meaning that matters downstream is "did we have to poll for it"; an inline answer did not.

9. **`Capabilities.notes` is serialised as a JSON list of strings.** The spec's example file shows `"notes": "…"`, a string, while the dataclass field is `tuple[str, ...]`. The list is the shape that round-trips; a reader that finds a bare string accepts it as a one-element list so a hand-written file still loads.

10. **Doctor check D0 does not re-open the link.** The spec writes D0 as `open_link(target)`, but `run_probe` receives an already-open `Station`. D0 records `description` and `identity` from the live link and calls `link.drain()`. Re-opening would drop the session the caller owns.

11. **`ProgMode.AUTO` resolves the same way for writes as for reads.** The spec states the rule only for reads. An AUTO write lands where it can be verified; a user who wants an unverifiable main-track write asks for `--mode pom --no-verify` explicitly, which is a deliberate act rather than a silent one.

Where the spec and `~/Developer/CLAUDE.md` overlap they agree, and the spec's L1-L6 is essentially the convention restated with this tool's command tree filled in. **The spec governs** the command tree, option names, payload field names and the `railctl/<command>/v1` schema strings. **`~/Developer/CLAUDE.md` governs** stream separation, the argv-array form of suggestions, the exit-code discipline, colour and non-interactive behaviour, and the requirement that one result object produce both renderings. Its `--limit`/`--fields` guidance has no target in M6 (no command returns a list large enough to page) and is deferred to the backup and diff work in Plan 5.

## File Structure

Created by this plan:

```
src/railctl/station/__init__.py      the station package's public surface: Station, the types, Capabilities,
                                     Timing, and the xbus re-exports (Direction, StationVersion,
                                     StationStatus, LocoInfo, CvEncoding)
src/railctl/station/types.py         Address/CvNumber/CvPage, ProgMode, CvSpec, CvResult, CvReadOutcome,
                                     StationEvent, Check, DoctorReport, and the CV constant sets
                                     (ADDRESS_CVS, BLIND_WRITE_CVS, PAGE_SELECTOR_CVS, INDEXED_CV_RANGE,
                                     CV144, DECODER_TYPE_CV) plus decoder_family/treats_cv144_as_lock
src/railctl/station/timing.py        the Timing dataclass and the TIMING singleton; every budget in one place
src/railctl/station/capabilities.py  the tri-state capability record, its JSON file, load/save/with_learned
src/railctl/station/facade.py        Station: the RLock, the session, power/status/version/emergency stop,
                                     drive/loco_info/functions, the event iterator, and the delegations
src/railctl/station/programming.py   CvProgrammer: the one wait loop, the CV matcher, POM read/write,
                                     service-mode read/write, mode resolution, the index-page cache
src/railctl/station/doctor.py        run_probe(): checks D0-D12, the interpretation rules, verdict_lines()

src/railctl/cli/__init__.py          empty package marker; holds no logic
src/railctl/cli/result.py            CommandResult, ResultWarning, LinkInfo, StationInfo, ErrorReport - THE
                                     one internal result object both renderings are built from
src/railctl/cli/render.py            human / json / ndjson renderers, the colour decision, NdjsonStream
src/railctl/cli/_errors.py           exception -> ErrorReport -> exit code; the one place EXIT_CODES is applied
src/railctl/cli/config.py            ~/.config/railctl paths, config.toml reader, the per-key precedence rule
src/railctl/cli/deps.py              Settings, logging setup, open_station(), address resolution, confirm()
src/railctl/cli/_meta.py             the ONE command metadata table: Option, CommandMeta, COMMANDS, manifest()
src/railctl/cli/main.py              the Typer app, the global callback, command registration, `app`
src/railctl/cli/commands/__init__.py command package marker
src/railctl/cli/commands/basics.py   version, status
src/railctl/cli/commands/schema.py   schema [COMMAND]
src/railctl/cli/commands/power.py    power on|off, stop
src/railctl/cli/commands/throttle.py drive, function
src/railctl/cli/commands/doctor.py   doctor
src/railctl/cli/commands/monitor.py  monitor
src/railctl/__main__.py              `python -m railctl` entry point

tests/station/conftest.py            StationFixture + the `bench` fixture every station test uses
tests/station/test_types.py          the constant sets, the tri-state helpers, the dataclass contracts
tests/station/test_capabilities.py   tri-state round trip, file merging, the unknown identity, corruption
tests/station/test_power_and_status.py  lifecycle, version, status, power, emergency stop, events()
tests/station/test_drive.py          drive, loco_info, function_set/toggle, both function paths
tests/station/test_cv_pom.py         the wait loop, the matcher, POM read, mode resolution, learning
tests/station/test_cv_service_mode.py  the encoding ladder, the 95 s regime, 63 10, the exit path
tests/station/test_cv_write.py       POM/service writes, verification, blind writes, page selection
tests/station/test_doctor.py         D0-D12, the interpretation rules, report.ok, verdict lines
tests/cli/test_format_modes.py       one JSON value on stdout, error object on stderr, NO_COLOR, non-interactive
tests/cli/test_errors.py             error codes, retryable, argv suggestions, exit codes
tests/cli/test_config.py             precedence per key, bad config file, path resolution
tests/cli/test_wiring.py             global options, deps, version/status end to end
tests/cli/test_schema.py             the manifest, the Typer drift test, `schema cv read`
tests/cli/test_throttle.py           power, stop, drive, function, the safety pre-flights
tests/cli/test_doctor.py             doctor rendering, capabilities.json writing, --no-save
tests/cli/test_monitor.py            monitor streaming, Ctrl-C, the summary line
tests/hardware/test_m5_acceptance.py power on / drive 30 / stop / power off on the bench
tests/hardware/test_m6_acceptance.py `railctl doctor --address 3` writes capabilities.json matching M1
```

Modified by this plan:

```
src/railctl/errors.py                + AbortedError, + ConfirmationRequiredError, + the EXIT_CODES row for 2
src/railctl/xbus/commands.py         + FunctionAction and cmd_function_single (E4 F8) - Task 3
src/railctl/xbus/cv.py               + result_ident_for(cv, encoding) - Task 4
tests/unit/test_xbus_commands.py     + the E4 F8 vectors - Task 3
tests/unit/test_cv.py                + result_ident_for bands - Task 4
tests/unit/test_exit_codes.py        + the two new classes' rows - Task 8
tests/vectors.py                     + the E4 F8 golden vector - Task 3
```

Deliberately **not** created here: `catalog/`, `backup/`, `cv read` / `cv write` / `backup` / `restore` / `diff` commands (Plans 4 and 5). `_meta.COMMANDS` therefore describes only the nine commands M6 registers, and the drift test is bidirectional so a later plan cannot add a command without adding its metadata row.

---

## Tasks

Eighteen sections. Tasks 6 and 7 are lettered rather than renumbered because later text refers to tasks by number: 6, 6b and 6c are one milestone's worth of CV writing split at its natural seams, and 7 through 7e are the thirteen doctor checks, which do not fit under one heading. Execute them in the order printed.

---

### Task 1: Station data layer: types, timing and the tri-state Capabilities file

**Files:**
- Create: `src/railctl/station/__init__.py`, `src/railctl/station/types.py`, `src/railctl/station/timing.py`, `src/railctl/station/capabilities.py`
- Test: `tests/station/test_types.py`, `tests/station/test_capabilities.py`

**Interfaces:**

- Consumes, exactly as merged on disk (M2-M4):
  - `railctl.errors.RailctlError(message: str, *, hint: str | None = None)` - `str(exc)` is the message only, `exc.hint` is the hint or `None`
  - `railctl.errors.CvOutOfRangeError(message: str, *, hint: str | None = None, cv: int | None = None)` - subclasses `ProgrammingError(StationError(RailctlError))`, carries `.cv`
  - `railctl.xbus.dialect.CvEncoding` - `enum.Enum`, members `POM_ZERO_BASED = "pom"`, `SERVICE_DIRECT = "direct"`, `SERVICE_EXT = "ext"`, `Z21_16BIT = "z21"`
  - `railctl.xbus.speed.Direction` - `enum.IntEnum`, `REVERSE = 0`, `FORWARD = 1`
  - `railctl.xbus.replies.StationVersion` - `@dataclass(frozen=True, slots=True)`, fields `raw: int`, `station_id: int`, properties `.version -> str`, `.family -> str`
  - `railctl.xbus.replies.StationStatus` - `@dataclass(frozen=True, slots=True)`, fields `raw, emergency_off, emergency_stop, auto_start_mode, service_mode, powering_up, ram_error`, `@classmethod from_raw(raw: int) -> StationStatus`, property `.track_power -> bool`
  - `railctl.xbus.replies.LocoInfo` - `@dataclass(frozen=True, slots=True)`, fields `raw_ident, raw_speed, speed_steps, in_use_by_other, function_bits, speed=None, direction=None, emergency_stopped=None, address=None`
  - `railctl.link.DEFAULT_TIMEOUT == 5.0`, `railctl.link.PROGRAMMING_TIMEOUT == 95.0`

- Produces (later tasks depend on these exact names and signatures):
  - `railctl.station.types`: `Address`, `CvNumber`, `CvPage`, `ProgMode`, `CvSpec`, `CvResult`, `CvReadOutcome`, `StationEvent`, `Check`, `DoctorReport` (with `.ok` and `.check(check_id)`), the constants `ADDRESS_CVS`, `BLIND_WRITE_CVS`, `CV29_LONG_ADDRESS_BIT`, `PAGE_SELECTOR_CVS`, `INDEXED_CV_RANGE`, `CV144`, `DECODER_TYPE_CV`, `MS_DECODER_TYPES`, `EVENT_NAMES` (twelve names - see below), and the functions `decoder_family(decoder_type: int | None) -> Literal["ms", "other", "unknown"]`, `treats_cv144_as_lock(decoder_type: int | None) -> bool`
  - `railctl.station.timing`: `Timing` (fifteen fields, all defaulted) and `TIMING: Final[Timing]`
  - `railctl.station.capabilities`: `ResultChannel`, `CAPABILITIES_VERSION`, `UNKNOWN_IDENTITY`, `LEARNABLE_FIELDS`, `Capabilities` with `.unknown(identity)`, `.load(path, identity)`, `.save(path) -> bool`, `.with_learned(**updates) -> Capabilities`, `.with_note(note) -> Capabilities`, `.as_json() -> dict`, `.probed -> bool`
  - `railctl.station` (the package): re-exports every name above plus `Direction`, `StationVersion`, `StationStatus`, `LocoInfo`, `CvEncoding`, with `__all__`

**Notes the implementer must not re-derive:**

- Import direction is one-way: `types.py` imports `Capabilities` from `capabilities.py` (for `DoctorReport.capabilities`); `capabilities.py` imports nothing from `types.py`. Build `capabilities.py` first. `timing.py` has no dependency on either and is built between them only because `tests/station/test_types.py` imports the three station submodules in isort order (`capabilities`, `timing`, `types`) and the failing-test steps below walk that import chain one `ModuleNotFoundError` at a time - it is not a sign `types.py` needs `Timing`.
- `StationEvent` lives in `types.py`, not in `timing.py`, and `at` is a required field with no default. A later task's `facade.py` builds every `StationEvent` with `at=self.now()` and imports the class as `from railctl.station.types import StationEvent` - never `from railctl.station.timing import StationEvent`, which is not where it lives.
- There is no `default_capabilities_path()` anywhere in this package. Resolving `~/.config/railctl/capabilities.json` is the CLI's job in a later task; `Capabilities.load`/`.save` only ever take a `path` argument that something else computed. Two modules computing that default is how they drift apart.
- `Capabilities.save(path) -> bool` returns `False`, and writes nothing, when nothing was written (the unknown identity). A later CLI task branches on that return value (`if caps.save(path):`) to decide whether to report that a probe's findings were persisted, so `save` returning `None` would make that branch always falsy.
- The file is exactly `{"version": 1, "links": {"<identity>": {...}}}`, keyed by `Link.identity`, never by a USB serial number - the serial comes from the USB descriptor, not from any telegram, and a LAN transport has none.
- `station/` is scanned by `tests/test_layering.py` the moment this task creates the directory (rule 1: no framing bytes or port names; rule 2: no CV arithmetic - `INDEXED_CV_RANGE` is a `range` object, never `cv - 1` / `% 256` / `>> 8` / `<< 8`; rule 3: no class here may end in `Error`, `Exception` or `Timeout` with a declared base). None of the code below needs any of that vocabulary, but a stray docstring word is enough to fail that suite.
- `CV144` is family-dependent: the older ZIMO MX family used it as a programming/update lock; ZIMO dropped that lock for the MS family (change log 2021-05-12) and reused the CV so that on MS decoders bit 4 turns on a confirmation jingle (change log 2024-05-31). The target decoder is an MS450P22 (MS family), so on this hardware CV144 is a sound setting, not a lock - and `treats_cv144_as_lock(None)` must be `False`, because a decoder type nobody has read yet is not evidence of anything, and guessing "locked" would abort a restore that is perfectly safe.
- `BLIND_WRITE_CVS` leaves out CV29 on purpose: CV29 only changes the answering address when bit 5 changes, and the commonest reason to write it at all is enabling RailCom via bit 3, which has to be verified by reading the CV back, not written blind.
- `notes` is a JSON list on disk. A hand-edited file whose `notes` is a bare string loads as a one-element tuple, not as an error and not as an iteration over characters.

---

- [ ] **Step 1: Write the failing `Capabilities` tests**

```python
# tests/station/test_capabilities.py
"""The tri-state capability file: None means "not established", never "no".

Every test here exists because collapsing "never measured" into "false" is
the recorded failure mode this project is built around - a POM CV read that
returns nothing at all must stay `None` forever, not become `False` the
moment it touches a JSON file.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from railctl.errors import RailctlError
from railctl.station.capabilities import (
    CAPABILITIES_VERSION,
    LEARNABLE_FIELDS,
    UNKNOWN_IDENTITY,
    Capabilities,
)

IDENTITY = "7010A0001194"


def test_unknown_has_every_capability_field_none_and_is_not_probed():
    caps = Capabilities.unknown(IDENTITY)
    assert caps.link_identity == IDENTITY
    assert caps.probed_at is None
    assert caps.probed is False
    for field in dataclasses.fields(caps):
        if field.name in ("link_identity", "notes"):
            continue
        assert getattr(caps, field.name) is None, field.name
    assert caps.notes == ()


def test_load_on_a_missing_file_returns_unknown_and_creates_nothing(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    assert Capabilities.load(path, IDENTITY) == Capabilities.unknown(IDENTITY)
    assert not path.exists()


def test_save_on_the_unknown_identity_writes_nothing(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    caps = Capabilities.unknown(UNKNOWN_IDENTITY)
    assert caps.save(path) is False
    assert not path.exists()


def test_the_tri_state_survives_the_file(tmp_path: Path):
    """A saved None reloads as None, a saved False reloads as False, and a key
    that was never written AT ALL loads as None too - never as False.

    Cases 1 and 2 alone would not catch a loader written as
    `entry.get("pom_read", False)` instead of `entry.get("pom_read")`, because
    `as_json()` always writes every field, including the nulls. Case 3 hand-
    writes an entry with the key missing outright, which is the only way to
    tell the two loader implementations apart.
    """
    path = tmp_path / "capabilities.json"

    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=None)
    assert caps.save(path) is True
    assert Capabilities.load(path, IDENTITY).pom_read is None

    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=False)
    assert caps.save(path) is True
    assert Capabilities.load(path, IDENTITY).pom_read is False

    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: {}}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).pom_read is None


def test_save_merges_with_an_existing_entry_for_another_station(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    other = Capabilities.unknown("other-station").with_learned(pom_read=True)
    assert other.save(path) is True

    mine = Capabilities.unknown(IDENTITY).with_learned(pom_read=False)
    assert mine.save(path) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == CAPABILITIES_VERSION
    assert raw["links"]["other-station"]["pom_read"] is True
    assert raw["links"][IDENTITY]["pom_read"] is False


def test_save_is_atomic_no_stray_temp_file_survives(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    Capabilities.unknown(IDENTITY).with_learned(pom_read=True).save(path)

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []
    # A half-written file would fail this parse, not the leftover check above.
    json.loads(path.read_text(encoding="utf-8"))


def test_load_on_corrupt_json_raises_railctl_error_naming_the_path(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert str(path) in str(caught.value)
    assert "doctor" in caught.value.hint


def test_load_on_the_wrong_version_raises_railctl_error(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"version": 2, "links": {}}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert str(path) in str(caught.value)
    assert "doctor" in caught.value.hint


def test_load_ignores_an_unrecognised_key_in_a_link_entry(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    entry = {"pom_read": True, "a_field_from_a_future_railctl": "x"}
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).pom_read is True


def test_load_raises_on_a_recognised_field_with_the_wrong_type(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    entry = {"pom_read": "yes"}
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert "pom_read" in str(caught.value)


def test_load_reads_a_bare_string_note_as_a_one_element_tuple(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    entry = {"notes": "hand-written note"}
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).notes == ("hand-written note",)


def test_with_learned_returns_a_new_object_and_leaves_the_original_unchanged():
    original = Capabilities.unknown(IDENTITY)
    learned = original.with_learned(pom_read=True)
    assert learned is not original
    assert learned.pom_read is True
    assert original.pom_read is None


def test_with_learned_raises_value_error_naming_an_unknown_field():
    caps = Capabilities.unknown(IDENTITY)
    with pytest.raises(ValueError, match="bogus_field"):
        caps.with_learned(bogus_field=True)


def test_with_learned_can_set_z21_cv_opcodes_though_it_is_not_a_learnable_field():
    """with_learned enforces only "is this a real field" - LEARNABLE_FIELDS is
    the FACADE's restriction, checked one layer up, because the doctor probe
    (a later task) needs to set fields outside it, z21_cv_opcodes among them.
    """
    assert "z21_cv_opcodes" not in LEARNABLE_FIELDS
    caps = Capabilities.unknown(IDENTITY).with_learned(z21_cv_opcodes=True)
    assert caps.z21_cv_opcodes is True


def test_with_note_appends_and_does_not_duplicate_an_identical_note():
    caps = Capabilities.unknown(IDENTITY).with_note("D4 concluded false from silence")
    again = caps.with_note("D4 concluded false from silence")
    assert caps.notes == ("D4 concluded false from silence",)
    assert again is caps


def test_as_json_carries_no_identity_key():
    payload = Capabilities.unknown(IDENTITY).with_learned(pom_read=True).as_json()
    assert "link_identity" not in payload
    assert payload["pom_read"] is True


def test_capabilities_is_frozen():
    caps = Capabilities.unknown(IDENTITY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.pom_read = True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/station/test_capabilities.py`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.station'` (the directory does not exist yet)

- [ ] **Step 3: Implement `Capabilities`**

```python
# src/railctl/station/capabilities.py
"""What this station has been measured to do, kept as three-valued facts.

`None` means "not established" - never "no". A capability becomes `False`
only when the station gave a real negative answer (`61 82`, Unsupported) or a
`railctl doctor` check exhausted every alternative; everything else stays
`None` until something measures it. Collapsing "never asked" into "no" is the
recorded failure mode this whole package exists to avoid, and it is why every
field below defaults to `None`, not `False`.

The file is shaped `{"version": 1, "links": {"<identity>": {...}}}`, one entry
per `Link.identity`. The key is never a hardware serial number: the identity
that names a serial link comes from the USB descriptor, not from any
telegram, and a network link has no descriptor to read one from at all.
`UNKNOWN_IDENTITY` is what a transport reports when it cannot produce a
stable identity of its own, and `save()` refuses to persist it - see below.

Path resolution is deliberately NOT this module's job. `Station.open` takes
an explicit `capabilities_path`, and the CLI computes the default one; a
second place computing that default is how the two drift apart the first
time either one changes.

`notes` is a JSON list on disk. A hand-edited file that holds a bare string
instead loads as a one-element tuple rather than being rejected or walked
character by character - see `Capabilities._notes_from`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Final, Literal

from railctl.errors import RailctlError

ResultChannel = Literal["broadcast", "poll", "none"]

CAPABILITIES_VERSION: Final[int] = 1
UNKNOWN_IDENTITY: Final[str] = "unknown"

# The only fields a normal operation - never a `doctor` probe - is allowed to
# learn on its own, because establishing anything else means sending an
# opcode a normal operation would never send. `with_learned` itself does not
# enforce this; it is the facade's job, and this set is what the facade
# checks against before calling `with_learned`.
LEARNABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"pom_read", "pom_result_channel", "pom_echo_zero_based", "service_direct_cv"}
)

_BOOL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "pom_read",
        "pom_echo_zero_based",
        "service_direct_cv",
        "service_ext_cv",
        "z21_cv_opcodes",
        "function_groups_4_5",
        "single_function_cmd",
    }
)
_INT_FIELDS: Final[frozenset[str]] = frozenset({"command_station_id", "loco_address_threshold"})
_STR_FIELDS: Final[frozenset[str]] = frozenset({"xpressnet_version", "probed_at"})
_RESULT_CHANNELS: Final[frozenset[str]] = frozenset({"broadcast", "poll", "none"})

_DELETE_AND_RERUN_HINT: Final[str] = "delete {path} and run `railctl doctor` again"


def _malformed(path: Path, message: str) -> RailctlError:
    return RailctlError(message, hint=_DELETE_AND_RERUN_HINT.format(path=path))


@dataclass(frozen=True, slots=True)
class Capabilities:
    """One station's measured capabilities. Every field but `link_identity`
    and `notes` is a tri-state: `True`, `False`, or `None` for "not
    established"."""

    link_identity: str
    probed_at: str | None = None
    xpressnet_version: str | None = None
    command_station_id: int | None = None
    pom_read: bool | None = None
    pom_result_channel: ResultChannel | None = None
    pom_echo_zero_based: bool | None = None
    loco_address_threshold: int | None = None
    service_direct_cv: bool | None = None
    service_ext_cv: bool | None = None
    z21_cv_opcodes: bool | None = None
    function_groups_4_5: bool | None = None
    single_function_cmd: bool | None = None
    notes: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, identity: str) -> Capabilities:
        """Every capability `None`, nothing probed - the starting point for a
        station that has never been measured, and the only shape `save()`
        refuses to persist."""
        return cls(link_identity=identity)

    @classmethod
    def load(cls, path: Path, identity: str) -> Capabilities:
        """Read `identity`'s entry from `path`, or `unknown(identity)` if the
        file or the entry is absent. Raises `RailctlError` on anything that
        looks wrong rather than guessing - a silently discarded measurement
        is exactly the failure mode this file format exists to prevent."""
        if not path.exists():
            return cls.unknown(identity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise _malformed(path, f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != CAPABILITIES_VERSION:
            raise _malformed(
                path, f"{path} is not a version {CAPABILITIES_VERSION} capabilities file"
            )
        links = raw.get("links")
        if not isinstance(links, dict):
            raise _malformed(path, f'{path} is missing its "links" table')
        entry = links.get(identity)
        if entry is None:
            return cls.unknown(identity)
        if not isinstance(entry, dict):
            raise _malformed(path, f"{path}: the entry for {identity!r} is not an object")
        return cls(link_identity=identity, **cls._fields_from(entry, identity, path))

    @classmethod
    def _fields_from(
        cls, entry: dict[str, object], identity: str, path: Path
    ) -> dict[str, object]:
        """Recognised keys only. An unrecognised key is ignored, so a newer
        railctl reading an older file - or the reverse - never fails on that
        alone. A recognised key with the wrong type DOES fail: silently
        coercing `"pom_read": "yes"` to a boolean is the measurement
        corruption this module exists to catch."""
        kwargs: dict[str, object] = {}
        for name in _BOOL_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, bool):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be a boolean or null, got {value!r}"
                )
            kwargs[name] = value
        for name in _INT_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, int):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be an integer or null, got {value!r}"
                )
            kwargs[name] = value
        for name in _STR_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, str):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be a string or null, got {value!r}"
                )
            kwargs[name] = value
        if "pom_result_channel" in entry:
            value = entry["pom_result_channel"]
            if value is not None and value not in _RESULT_CHANNELS:
                raise _malformed(
                    path,
                    f"{path}: {identity!r}.pom_result_channel must be one of "
                    f"{sorted(_RESULT_CHANNELS)} or null, got {value!r}",
                )
            kwargs["pom_result_channel"] = value
        if "notes" in entry:
            kwargs["notes"] = cls._notes_from(entry["notes"], identity, path)
        return kwargs

    @staticmethod
    def _notes_from(value: object, identity: str, path: Path) -> tuple[str, ...]:
        if isinstance(value, str):
            # A hand-written file holding one bare string, not a list - see
            # the module docstring. One note, never split into characters.
            return (value,)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        raise _malformed(path, f"{path}: {identity!r}.notes must be a string or a list of strings")

    def save(self, path: Path) -> bool:
        """Write this station's entry into `path`, merged with whatever else
        is already there, atomically. Returns `False` and touches nothing
        when `link_identity` is `UNKNOWN_IDENTITY`: an identity with no
        stable name has nowhere safe to persist to, and inventing a key
        would silently merge two different stations' facts together."""
        if self.link_identity == UNKNOWN_IDENTITY:
            return False
        payload = {"version": CAPABILITIES_VERSION, "links": self._merged_links(path)}
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            # os.replace is atomic on the same filesystem: a reader never
            # sees a half-written file, and a process killed mid-write
            # leaves only the abandoned temp file behind, never a truncated
            # capabilities.json.
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return True

    def _merged_links(self, path: Path) -> dict[str, object]:
        links: dict[str, object] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("links"), dict):
                links = dict(raw["links"])
        links[self.link_identity] = self.as_json()
        return links

    def with_learned(self, **updates: object) -> Capabilities:
        """Return a new `Capabilities` with `updates` applied. Accepts any
        real field name; `LEARNABLE_FIELDS` is a narrower set the FACADE
        enforces before calling this, not a restriction this method applies
        itself - the doctor probe needs to set fields outside that set,
        `z21_cv_opcodes` among them."""
        valid = {f.name for f in fields(self)} - {"link_identity"}
        unknown = set(updates) - valid
        if unknown:
            raise ValueError(f"unknown capability field: {sorted(unknown)[0]!r}")
        return replace(self, **updates)

    def with_note(self, note: str) -> Capabilities:
        """Append `note`, unless it already is the exact text of an existing
        one - repeating the same probe should not grow the file forever."""
        if note in self.notes:
            return self
        return replace(self, notes=(*self.notes, note))

    def as_json(self) -> dict[str, object]:
        """This station's entry as written to disk - no `link_identity` key,
        because that name is the dict key one level up in the file."""
        return {
            "probed_at": self.probed_at,
            "xpressnet_version": self.xpressnet_version,
            "command_station_id": self.command_station_id,
            "pom_read": self.pom_read,
            "pom_result_channel": self.pom_result_channel,
            "pom_echo_zero_based": self.pom_echo_zero_based,
            "loco_address_threshold": self.loco_address_threshold,
            "service_direct_cv": self.service_direct_cv,
            "service_ext_cv": self.service_ext_cv,
            "z21_cv_opcodes": self.z21_cv_opcodes,
            "function_groups_4_5": self.function_groups_4_5,
            "single_function_cmd": self.single_function_cmd,
            "notes": list(self.notes),
        }

    @property
    def probed(self) -> bool:
        return self.probed_at is not None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/station/test_capabilities.py`
Expected: PASS, `17 passed`

- [ ] **Step 5: Write the failing `types`/`Timing` tests**

```python
# tests/station/test_types.py
"""The CV/result dataclasses, the doctor report shape, the CV addressing
constants, and the injectable `Timing` table.

`decoder_family` and `treats_cv144_as_lock` are tested side by side on
purpose: both read the same `None` (decoder type never read) and answer
differently, and that disagreement is the point, not a bug - see the module
docstring in `railctl.station.types`.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.errors import CvOutOfRangeError
from railctl.station.capabilities import Capabilities
from railctl.station.timing import TIMING, Timing
from railctl.station.types import (
    ADDRESS_CVS,
    BLIND_WRITE_CVS,
    CV144,
    CV29_LONG_ADDRESS_BIT,
    DECODER_TYPE_CV,
    EVENT_NAMES,
    INDEXED_CV_RANGE,
    MS_DECODER_TYPES,
    PAGE_SELECTOR_CVS,
    Check,
    CvReadOutcome,
    CvResult,
    CvSpec,
    DoctorReport,
    ProgMode,
    StationEvent,
    decoder_family,
    treats_cv144_as_lock,
)
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import LocoInfo, StationStatus, StationVersion
from railctl.xbus.speed import Direction

ALL_DATACLASSES = (CvSpec, CvResult, CvReadOutcome, StationEvent, Check, DoctorReport, Timing, Capabilities)


def _check(check_id: str, status: str) -> Check:
    return Check(id=check_id, title=check_id, status=status, detail="")


def test_prog_mode_has_exactly_the_three_documented_members():
    assert {mode.value for mode in ProgMode} == {"auto", "pom", "service"}
    assert ProgMode.AUTO.value == "auto"


def test_cv_spec_defaults_to_no_name_and_no_page():
    spec = CvSpec(cv=8)
    assert spec.name == ""
    assert spec.page is None


def test_cv_spec_can_carry_a_name_and_an_index_page():
    spec = CvSpec(cv=257, name="index page CV", page=(31, 2))
    assert spec.name == "index page CV"
    assert spec.page == (31, 2)


def test_cv_result_carries_the_resolved_mode_never_auto():
    result = CvResult(
        cv=8,
        value=3,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="read",
        verified=None,
        elapsed=1.7,
    )
    assert result.mode is ProgMode.SERVICE
    assert result.operation == "read"
    assert result.verified is None


def test_cv_read_outcome_can_carry_a_result():
    result = CvResult(
        cv=8,
        value=3,
        mode=ProgMode.SERVICE,
        encoding=CvEncoding.SERVICE_DIRECT,
        operation="read",
        verified=None,
        elapsed=1.7,
    )
    outcome = CvReadOutcome(spec=CvSpec(cv=8), result=result, error=None)
    assert outcome.result is result
    assert outcome.error is None


def test_cv_read_outcome_can_carry_an_error():
    error = CvOutOfRangeError("cv 2000 out of range", cv=2000)
    outcome = CvReadOutcome(spec=CvSpec(cv=2000), result=None, error=error)
    assert outcome.result is None
    assert outcome.error is error


def test_cv_read_outcome_allows_neither_result_nor_error_as_a_distinct_state():
    """The spec never says a read must produce one or the other. A batch
    that stops after CV1 fails leaves CV17's outcome with both fields None -
    not attempted, not resolved, not failed. A consumer that branches on
    `error is not None` would read that as success; the contract this
    dataclass pins is that the caller must branch on `result is None`
    instead.
    """
    outcome = CvReadOutcome(spec=CvSpec(cv=17), result=None, error=None)
    assert outcome.result is None
    assert outcome.error is None


def test_station_event_carries_a_free_form_payload():
    event = StationEvent(
        at=12.5, name="cv.stale_result", detail="CV8 read after retry", payload={"cv": 8, "attempt": 2}
    )
    assert event.name in EVENT_NAMES
    assert event.payload == {"cv": 8, "attempt": 2}


def test_doctor_report_ok_requires_d0_through_d2_ok_and_d3_not_fail():
    report = DoctorReport(
        checks=(_check("D0", "ok"), _check("D1", "ok"), _check("D2", "ok"), _check("D3", "unknown")),
        capabilities=Capabilities.unknown("test"),
    )
    assert report.ok is True

    d3_fail = dataclasses.replace(
        report,
        checks=(_check("D0", "ok"), _check("D1", "ok"), _check("D2", "ok"), _check("D3", "fail")),
    )
    assert d3_fail.ok is False

    d0_fail = dataclasses.replace(
        report,
        checks=(_check("D0", "fail"), _check("D1", "ok"), _check("D2", "ok"), _check("D3", "skip")),
    )
    assert d0_fail.ok is False

    missing_d1 = dataclasses.replace(report, checks=(_check("D0", "ok"), _check("D2", "ok")))
    assert missing_d1.ok is False


def test_doctor_report_check_looks_up_by_id_and_returns_none_when_absent():
    report = DoctorReport(checks=(_check("D0", "ok"),), capabilities=Capabilities.unknown("test"))
    assert report.check("D0").status == "ok"
    assert report.check("D9") is None


def test_the_cv_addressing_constants_match_the_design_spec():
    assert ADDRESS_CVS == frozenset({1, 17, 18, 29})
    assert BLIND_WRITE_CVS == frozenset({1, 8, 17, 18})
    assert CV29_LONG_ADDRESS_BIT == 5
    assert PAGE_SELECTOR_CVS == (31, 32)
    assert list(INDEXED_CV_RANGE) == list(range(257, 513))
    assert CV144 == 144
    assert DECODER_TYPE_CV == 250
    assert MS_DECODER_TYPES == frozenset({6, 7, 12})


def test_blind_write_cvs_excludes_cv29():
    """CV29 only changes the answering address when bit 5 changes, and the
    commonest reason to write it is enabling RailCom (bit 3), which must be
    verifiable - so CV29 stays out of the blind-write set even though it is
    an address CV like CV1/17/18.
    """
    assert 29 not in BLIND_WRITE_CVS
    assert 29 in ADDRESS_CVS


def test_event_names_are_exactly_the_twelve_defined_events():
    """Twelve names from this task onward, not five: `cv.unexercised_band` is
    emitted by a later CV-programming task and `function.group_seeded` by a
    later drive/function task; `power.on`, `power.off`, `loco.emergency_stop`,
    `service.entered` and `reply.unknown` are emitted by the facade (Task 2)
    and rendered by `monitor`. A later CLI task pins that every name in this
    tuple has a rendering - so the tuple has to be complete here, before any
    emitter exists, or that later task has nothing to render against.
    """
    assert EVENT_NAMES == (
        "cv.stale_result",
        "cv.write_unverified",
        "cv.unexercised_band",
        "page.unverified",
        "loco.in_use_by_other",
        "address.band_unverified",
        "function.group_seeded",
        "power.on",
        "power.off",
        "loco.emergency_stop",
        "service.entered",
        "reply.unknown",
    )


def test_decoder_family_is_ms_for_every_known_ms_decoder_type():
    for decoder_type in sorted(MS_DECODER_TYPES):
        assert decoder_family(decoder_type) == "ms"


def test_decoder_family_is_other_for_a_non_ms_decoder_type():
    assert decoder_family(5) == "other"  # e.g. an older ZIMO MX-family decoder


def test_decoder_family_is_unknown_when_the_type_was_never_read():
    assert decoder_family(None) == "unknown"


def test_treats_cv144_as_lock_disagrees_with_decoder_family_about_none():
    """decoder_family(None) is "unknown" - a report may not claim a family it
    never measured. treats_cv144_as_lock(None) is False for the opposite
    reason: the restore path must not refuse a safe write just because it
    never read the decoder type. Same None, two different correct answers.
    """
    assert decoder_family(None) == "unknown"
    assert treats_cv144_as_lock(None) is False


def test_treats_cv144_as_lock_is_false_for_the_ms_family():
    assert treats_cv144_as_lock(6) is False  # MS450P22 family: CV144 is a sound setting


def test_treats_cv144_as_lock_is_true_outside_the_ms_family():
    assert treats_cv144_as_lock(5) is True  # e.g. an older MX-family decoder


def test_timing_matches_the_m5_table():
    timing = Timing()
    assert timing.li_ack_normal == 5.0
    assert timing.li_ack_programming == 95.0
    assert timing.min_exchange == 0.05
    assert timing.power_settle == 0.5
    assert timing.pom_result == 2.0
    assert timing.pom_poll_interval == 0.10
    assert timing.pom_read_attempts == 3
    assert timing.pom_retry_delay == 0.25
    assert timing.pom_write_settle == 0.5
    assert timing.service_result == 95.0
    assert timing.service_first_poll_delay == 0.20
    assert timing.service_poll_interval == 0.50
    assert timing.service_ready_limit == 8
    assert timing.service_exit_settle == 0.10
    assert timing.page_cache_ttl == 10.0


def test_timing_singleton_is_the_default_timing():
    assert TIMING == Timing()


def test_li_ack_timings_agree_with_the_link_module_constants():
    """Timing.li_ack_normal and li_ack_programming restate link.DEFAULT_TIMEOUT
    and link.PROGRAMMING_TIMEOUT as data the station can inject a fake clock
    against; importing both modules here is what stops them drifting apart
    if link.py's constants ever change without station/timing.py following.
    """
    from railctl.link import DEFAULT_TIMEOUT, PROGRAMMING_TIMEOUT

    assert Timing().li_ack_normal == DEFAULT_TIMEOUT
    assert Timing().li_ack_programming == PROGRAMMING_TIMEOUT


def test_every_dataclass_in_the_station_data_layer_is_frozen_and_uses_slots():
    for cls in ALL_DATACLASSES:
        params = cls.__dataclass_params__
        assert params.frozen is True, cls.__name__
        assert "__slots__" in cls.__dict__, cls.__name__


def test_a_frozen_dataclass_refuses_mutation():
    spec = CvSpec(cv=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.cv = 2
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/station/test_types.py`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.station.timing'` (`railctl.station.capabilities` now imports fine from Step 3; `timing` is the next import in the file and does not exist yet)

- [ ] **Step 7: Implement `Timing`**

```python
# src/railctl/station/timing.py
"""Every timing budget the station facade uses, gathered in one injectable
place so a unit test can run the 95 s service-mode path in microseconds
against a fake clock instead of a real one.

`li_ack_normal` and `li_ack_programming` restate `link.DEFAULT_TIMEOUT` and
`link.PROGRAMMING_TIMEOUT` as data. The station always passes an explicit
timeout to `Link`, so in practice this table is authoritative; `link.py`'s
constants exist for callers with no station layer above them, and a test
pins the two pairs equal so they cannot drift apart unnoticed.

Every value below is measured against the YD7010, not guessed
(docs/probe-results.md, 2026-08-04): one service-mode read takes about 1.7 s,
comfortably inside `service_result`'s 95 s whole-operation budget, and the
per-attempt POM budget is well above the reply time of the opcodes that
answer at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Timing:
    li_ack_normal: float = 5.0  # per-exchange budget in normal operation
    li_ack_programming: float = 95.0  # per-exchange budget once in service mode
    min_exchange: float = 0.05  # floor when clamping to whatever budget remains
    power_settle: float = 0.5
    pom_result: float = 2.0  # per attempt
    pom_poll_interval: float = 0.10
    pom_read_attempts: int = 3
    pom_retry_delay: float = 0.25
    pom_write_settle: float = 0.5  # floor only - nothing reports track delivery
    service_result: float = 95.0  # whole-operation budget
    service_first_poll_delay: float = 0.20
    service_poll_interval: float = 0.50  # minimum gap between polls, not a deadline
    service_ready_limit: int = 8
    service_exit_settle: float = 0.10
    page_cache_ttl: float = 10.0


TIMING: Final[Timing] = Timing()
```

- [ ] **Step 8: Run the tests to verify the next failure**

Run: `uv run pytest tests/station/test_types.py`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.station.types'` (`timing` now imports; `types` is next and does not exist yet)

- [ ] **Step 9: Implement `types`**

```python
# src/railctl/station/types.py
"""The station facade's shared vocabulary: CV addressing rules, results, and
the doctor's report shape.

`Direction`, `StationVersion`, `StationStatus`, `LocoInfo` and `CvEncoding`
are defined in `xbus` and re-exported by `railctl.station.__init__`, not by
this module - importing `xbus` here as well as there would be exactly the
two-places-to-change split the design forbids for `CvEncoding`.

CV144 is family-dependent and must never be treated as a universal
programming lock. On the older ZIMO MX family CV144 is the programming/
update lock, and a non-zero value there blocks writes. ZIMO dropped that
lock for the MS family (change log 2021-05-12: "CV #144 (Programm./Update
lock): dropped, no longer necessary in new decoders") and later reused the
CV: on MS decoders CV144 bit 4 set to 1 turns on a confirmation jingle when a
CV is programmed (change log 2024-05-31). The decoder this tool targets for
0.1.0 is an MS450P22 - MS family - so on this hardware CV144 is a sound
setting, not a lock. `decoder_family` decides the family from
`DECODER_TYPE_CV`, and `treats_cv144_as_lock` answers `False` when the
family is unread (`None`) as well as when it is MS: guessing "locked" for a
decoder type nobody has read yet would abort restores that are perfectly
safe - the false negative this project exists to stop.

`BLIND_WRITE_CVS` excludes CV29 on purpose. CV29 only changes the address a
decoder answers to when bit 5 changes, and the most common reason to write
it at all is enabling RailCom by setting bit 3 - a change that must be
verified by reading the CV back, never written and trusted.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Literal

from railctl.errors import RailctlError
from railctl.station.capabilities import Capabilities
from railctl.xbus.dialect import CvEncoding

Address = int  # a locomotive address, 1..9999, always as the operator sees it
CvNumber = int  # a CV number, 1..1024, user-facing and always 1-based
CvPage = tuple[int, int]  # (CV31 value, CV32 value) for the extended index


class ProgMode(enum.Enum):
    """Which track and protocol a CV operation used - never AUTO once resolved."""

    AUTO = "auto"
    POM = "pom"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class CvSpec:
    """What the caller asked for: a CV number, and optionally a name for
    reporting and the index page it lives behind."""

    cv: CvNumber
    name: str = ""
    page: CvPage | None = None


@dataclass(frozen=True, slots=True)
class CvResult:
    """One resolved CV read or write."""

    cv: CvNumber
    value: int
    mode: ProgMode
    encoding: CvEncoding
    operation: Literal["read", "write"]
    verified: bool | None  # write: read back and confirmed, or not attempted; read: always None
    elapsed: float


@dataclass(frozen=True, slots=True)
class CvReadOutcome:
    """One CV's outcome inside a batch. `result` and `error` are independent:
    a CV skipped because an earlier one in the same batch raised leaves BOTH
    `None` - not attempted, not resolved, not failed - and callers branch on
    `result is None`, never on `error is not None`, or a skip reads as
    success.
    """

    spec: CvSpec
    result: CvResult | None
    error: RailctlError | None


@dataclass(frozen=True, slots=True)
class StationEvent:
    """A notable moment worth surfacing to the operator, not an exception -
    the operation that raised it still completed. `name` is one of
    `EVENT_NAMES`; `payload` carries whatever detail the emitting code has."""

    at: float
    name: str
    detail: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class Check:
    """One row of a `railctl doctor` report."""

    id: str
    title: str
    status: Literal["ok", "fail", "skip", "unknown"]
    detail: str


# Doctor checks D0-D2 establish the basics (link, track power, station
# identity); D3 probes a feature that may legitimately be absent, so an
# "unknown" or "skip" verdict there does not fail the report on its own -
# only D0-D2 failing, or D3 outright failing, does.
_REQUIRED_OK: Final[tuple[str, ...]] = ("D0", "D1", "D2")
_MUST_NOT_FAIL: Final[str] = "D3"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...]
    capabilities: Capabilities

    @property
    def ok(self) -> bool:
        by_id = {check.id: check for check in self.checks}
        for check_id in _REQUIRED_OK:
            check = by_id.get(check_id)
            if check is None or check.status != "ok":
                return False
        must_not_fail = by_id.get(_MUST_NOT_FAIL)
        return must_not_fail is None or must_not_fail.status != "fail"

    def check(self, check_id: str) -> Check | None:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None


ADDRESS_CVS: Final[frozenset[int]] = frozenset({1, 17, 18, 29})
BLIND_WRITE_CVS: Final[frozenset[int]] = frozenset({1, 8, 17, 18})
CV29_LONG_ADDRESS_BIT: Final[int] = 5
PAGE_SELECTOR_CVS: Final[tuple[int, int]] = (31, 32)
INDEXED_CV_RANGE: Final[range] = range(257, 513)
CV144: Final[int] = 144  # meaning depends on the decoder family - see module docstring
DECODER_TYPE_CV: Final[int] = 250
MS_DECODER_TYPES: Final[frozenset[int]] = frozenset({6, 7, 12})  # MS450, MS990, MS491

EVENT_NAMES: Final[tuple[str, ...]] = (
    # diagnostic events - something the operator may need to act on
    "cv.stale_result",
    "cv.write_unverified",
    "cv.unexercised_band",  # emitted by the CV-programming layer
    "page.unverified",
    "loco.in_use_by_other",
    "address.band_unverified",
    "function.group_seeded",  # emitted by the drive/function layer
    # station-state events - emitted by the facade, rendered by `monitor`
    "power.on",
    "power.off",
    "loco.emergency_stop",
    "service.entered",
    "reply.unknown",
)


def decoder_family(decoder_type: int | None) -> Literal["ms", "other", "unknown"]:
    """`None` means `DECODER_TYPE_CV` was never read - the family is unknown,
    never guessed. A report may not claim a family it never measured."""
    if decoder_type is None:
        return "unknown"
    return "ms" if decoder_type in MS_DECODER_TYPES else "other"


def treats_cv144_as_lock(decoder_type: int | None) -> bool:
    """`False` for `None` as well as for the MS family - see the module
    docstring. The restore path must not refuse a safe write just because
    the decoder type was never read; only a CONFIRMED non-MS family locks
    CV144."""
    return decoder_family(decoder_type) == "other"
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `uv run pytest tests/station/test_types.py`
Expected: PASS, `24 passed`

- [ ] **Step 11: Append the package re-export tests**

Append to the end of `tests/station/test_types.py`:

```python
def test_the_station_package_reexports_the_xbus_types():
    import railctl.station as station

    assert station.Direction is Direction
    assert station.StationVersion is StationVersion
    assert station.StationStatus is StationStatus
    assert station.LocoInfo is LocoInfo
    assert station.CvEncoding is CvEncoding


def test_every_name_in_station_all_is_actually_importable():
    """A canary against a typo in `__all__`: a name listed there that does not
    exist on the module would otherwise only be caught by whoever first tries
    `from railctl.station import <that name>` - a later task, not this one.
    """
    import railctl.station as station

    for name in station.__all__:
        assert hasattr(station, name), name
```

- [ ] **Step 12: Run the tests to verify they fail**

Run: `uv run pytest tests/station/test_types.py`
Expected: FAIL - `AttributeError: module 'railctl.station' has no attribute 'Direction'` (`railctl/station/` has no `__init__.py` yet, so Python treats it as an empty namespace package)

- [ ] **Step 13: Implement the package `__init__.py`**

```python
# src/railctl/station/__init__.py
"""The station facade's shared vocabulary, gathered from three siblings.

`types.py`, `timing.py` and `capabilities.py` each stay import-clean on
their own (`types.py` depends on `capabilities.py`; `capabilities.py`
depends on nothing in this package). This module is the one place a caller
reaches for all of it, plus the wire-level types the station and CLI layers
also need: `Direction`, `StationVersion`, `StationStatus`, `LocoInfo` and
`CvEncoding`, defined once in `xbus` and re-exported here rather than a
second time.
"""

from __future__ import annotations

from railctl.station.capabilities import (
    CAPABILITIES_VERSION,
    LEARNABLE_FIELDS,
    UNKNOWN_IDENTITY,
    Capabilities,
    ResultChannel,
)
from railctl.station.timing import TIMING, Timing
from railctl.station.types import (
    ADDRESS_CVS,
    BLIND_WRITE_CVS,
    CV144,
    CV29_LONG_ADDRESS_BIT,
    DECODER_TYPE_CV,
    EVENT_NAMES,
    INDEXED_CV_RANGE,
    MS_DECODER_TYPES,
    PAGE_SELECTOR_CVS,
    Address,
    Check,
    CvNumber,
    CvPage,
    CvReadOutcome,
    CvResult,
    CvSpec,
    DoctorReport,
    ProgMode,
    StationEvent,
    decoder_family,
    treats_cv144_as_lock,
)
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import LocoInfo, StationStatus, StationVersion
from railctl.xbus.speed import Direction

__all__ = [
    "ADDRESS_CVS",
    "BLIND_WRITE_CVS",
    "CAPABILITIES_VERSION",
    "CV144",
    "CV29_LONG_ADDRESS_BIT",
    "DECODER_TYPE_CV",
    "EVENT_NAMES",
    "INDEXED_CV_RANGE",
    "LEARNABLE_FIELDS",
    "MS_DECODER_TYPES",
    "PAGE_SELECTOR_CVS",
    "TIMING",
    "UNKNOWN_IDENTITY",
    "Address",
    "Capabilities",
    "Check",
    "CvEncoding",
    "CvNumber",
    "CvPage",
    "CvReadOutcome",
    "CvResult",
    "CvSpec",
    "Direction",
    "DoctorReport",
    "LocoInfo",
    "ProgMode",
    "ResultChannel",
    "StationEvent",
    "StationStatus",
    "StationVersion",
    "Timing",
    "decoder_family",
    "treats_cv144_as_lock",
]
```

- [ ] **Step 14: Run the whole station suite**

Run: `uv run pytest tests/station/`
Expected: PASS, `43 passed`

- [ ] **Step 15: Run the full suite**

Run: `uv run pytest`
Expected: PASS, `963 passed` (the 920 already on `main` plus the 43 added here)

- [ ] **Step 16: Check the coverage gate**

Run: `uv run pytest --cov --cov-report=term-missing`
Expected: the coverage table, now listing `src/railctl/station/__init__.py`, `src/railctl/station/types.py`, `src/railctl/station/timing.py` and `src/railctl/station/capabilities.py`, then `Required test coverage of 90% reached.` and `963 passed`. `src/railctl/station/` is not in `omit`, so every branch this task adds - every field-type check and the `notes` string/list split in `capabilities.py`, every arm of `DoctorReport.ok` in `types.py` - needs to be the reason a line here is covered, not an accident of some other test. If the total comes in under 90%, the uncovered lines in the `term-missing` column belong to this task's own new modules; fix the test, not the gate.

- [ ] **Step 17: Lint and format check**

Run: `uv run ruff check src/railctl/station tests/station && uv run ruff format --check src/railctl/station tests/station`
Expected: no output, exit code 0. If `ruff format --check` fails, run `uv run ruff format src/railctl/station tests/station` and re-run both commands before continuing.

- [ ] **Step 18: Commit**

```bash
git add src/railctl/station tests/station
git commit -m "feat(station): add CV/timing types and the tri-state capabilities file"
```

---

### Task 2: Station facade: session, version, status, power, emergency stop and the event iterator

**Design specification:** `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 587-597 (position and
contract, the two LI-USB facts), 649-694 (facade API and semantics), 916-924 (the `on_event`
table). Read those before writing a line - every value below traces back to one of them.

**Files:**
- Create: `src/railctl/station/facade.py`
- Create: `tests/station/conftest.py`
- Create: `tests/station/test_power_and_status.py`
- Modify: `src/railctl/station/__init__.py` (export `Station`)

**Interfaces:**

- Consumes:
  - Task 1's `station/` package: `railctl.station.timing.Timing`, `.TIMING` (fifteen fields -
    `li_ack_normal=5.0`, `li_ack_programming=95.0`, `min_exchange=0.05`, `power_settle=0.5`,
    `pom_result=2.0`, `pom_poll_interval=0.10`, `pom_read_attempts=3`, `pom_retry_delay=0.25`,
    `pom_write_settle=0.5`, `service_result=95.0`, `service_first_poll_delay=0.20`,
    `service_poll_interval=0.50`, `service_ready_limit=8`, `service_exit_settle=0.10`,
    `page_cache_ttl=10.0`); and, from `railctl.station.types` - **not** from `timing.py` -
    `.StationEvent` (`@dataclass(frozen=True, slots=True)` with `at: float` **required, no
    default**, `name: str`, `detail: str`, `payload: dict[str, object]`). Importing `StationEvent`
    from `railctl.station.timing` is an `ImportError` on the first line of `facade.py`: `timing.py`
    holds only `Timing`/`TIMING`. Also `railctl.station.capabilities.Capabilities`
    (`@dataclass(frozen=True, slots=True)` with `link_identity`, `probed_at`, `xpressnet_version`,
    `command_station_id`, `pom_read`, `pom_result_channel`, `pom_echo_zero_based`,
    `loco_address_threshold`, `service_direct_cv`, `service_ext_cv`, `z21_cv_opcodes`,
    `function_groups_4_5`, `single_function_cmd`, `notes: tuple[str, ...] = ()`; classmethods
    `unknown(cls, identity: str) -> Capabilities` and `load(cls, path: Path, identity: str) ->
    Capabilities`; methods `save(self, path: Path) -> bool` (`False` when nothing was written -
    an unknown link identity - `True` otherwise; Task 12's `if caps.save(path):` depends on this
    return value, so nothing here may call it as if it returned `None`) and `with_learned(self,
    **updates: object) -> Capabilities`), `.LEARNABLE_FIELDS` (`frozenset[str]` of the four names
    `pom_read`, `pom_result_channel`, `pom_echo_zero_based`, `service_direct_cv` - spec line 844)
    and `.UNKNOWN_IDENTITY` (`"unknown"` - the string a transport with no stable identity
    returns, spec line 842).
  - `railctl.link.Link` (on disk) - `open() -> None`, `close() -> None`, `request(telegram:
    bytes, *, timeout: float | None = None) -> bytes`, `send`, `send_no_reply`, `await_frame`,
    `poll(timeout: float = 0.0) -> list[Frame]`, `drain`, `stats`, `recent_events`,
    `recent_late_replies`; properties `description: str`, `identity: str`, `version_telegram:
    bytes | None`. `Link` takes its OWN clock shape (`clock: Clock` - an object with
    `.monotonic()` and `.sleep()`); `Station` below takes two plain callables instead. Do not
    confuse the two - see "Decisions already made".
  - `railctl.transport.open_link(target: str = "auto", *, on_event: Callable[[Frame], None] |
    None = None) -> Link` (on disk).
  - `railctl.xbus.commands` (on disk): `cmd_station_version() -> bytes` (`21 21 00`),
    `cmd_station_status() -> bytes` (`21 24 05`), `cmd_track_power_on() -> bytes` (`21 81`, XOR
    `A0`), `cmd_track_power_off() -> bytes` (`21 80`, XOR `A1`), `cmd_emergency_stop_all() ->
    bytes` (`80 80`), `cmd_emergency_stop_loco(address: int, *, threshold: int) -> bytes` (`92 AH
    AL X`).
  - `railctl.xbus.replies` (on disk): `parse(telegram: bytes) -> Reply` (total, never raises);
    reply types `InterfaceStatus(code: int)`, `StationVersion(raw: int, station_id: int)`,
    `StationStatus` (`.track_power: bool` property), `EmergencyStopBroadcast`, `ServiceModeEntry`,
    `StationBusy` (`61 81` - "the station cannot act right now, says nothing about support"),
    `TransferError` (`61 80` - Link already retries this once via `_RETRY_PREFIXES` and raises
    `LinkProtocolError` itself on a second one, so a bare `TransferError` can never actually reach
    `exchange()` through `link.request()`), `Other(telegram: bytes, reason: Reason)` whose
    `.reason` is one of `REASON_CHECKSUM`, `REASON_LENGTH`, `REASON_EMPTY`, `REASON_UNKNOWN_FORM`;
    the singletons `GENERIC_ACK`, `POWER_ON = PowerState(on=True)`, `POWER_OFF = PowerState(on=False)`,
    `UNSUPPORTED`; and `TRANSIENT_REPLIES: frozenset[Reply]` (`{SHORT_CIRCUIT, TRACK_SHORT_CIRCUIT,
    BUSY, STATION_BUSY, TRANSFER_ERROR}` - none of the five says anything about whether an opcode
    is implemented (that module's own docstring on `TRANSIENT_REPLIES`), so `exchange()` returns
    every one of them unchanged, `StationBusy` included, even though it is the one member that can
    follow ANY command; a later caller that has the CV number or retry policy this method has no
    way to know decides what a `Busy` or `StationBusy` reply means).
  - `railctl.xbus.dialect.XPRESSNET` (`Dialect(name="xpressnet", long_address_threshold=100,
    service_cv_preference=(SERVICE_DIRECT, Z21_16BIT, SERVICE_EXT))`) and `DIVERGENCE_BAND =
    range(100, 128)`.
  - `railctl.errors` (on disk): `RailctlError(message, *, hint=None)`, `TransportError`,
    `ProtocolError`, `UnsupportedCommandError`, `TrackPowerError`. Nothing in this task raises
    `StationBusyError`: `exchange()` returns `StationBusy` (`61 81`) unchanged, exactly like every
    other `TRANSIENT_REPLIES` member - see must-pin 5 below and the reply-mapping table this task
    owns.
  - `railctl.envelope` (on disk): `Frame(kind: Kind, payload: bytes)`, `Kind.SOLICITED` /
    `Kind.UNSOLICITED`, `hex_bytes(data: bytes) -> str`.
  - `railctl.transport.fake.FakeTransport` (on disk) - `__init__(*, clock=None,
    chunk_size=None, max_write=None, on_write=None, description="fake xpressnet",
    identity="fake", diagnostic_hint="check the station is reachable on the network")`;
    `expect(request: bytes, *, reply: bytes = b"") -> FakeTransport`, `queue(data: bytes) ->
    None`; attributes `written: list[bytes]`, `write_chunks`, `flushes`, `script_pending`,
    `is_open`. `FakeClock(start: float = 0.0)` - `monotonic()`, `sleep(seconds)`,
    `advance(seconds)`.
  - `tests/conftest.py` already provides `chunk_size`, parametrised `[None, 1]` with ids
    `whole-frame` / `byte-at-a-time`, and `envelope_factory`, parametrised over `ENVELOPES`
    (`[LiUsbEnvelope]` today) with ids being the envelope class name. `ENVELOPES` has one member
    today, so no count in this file changes because of it - but it is what makes adding
    `Z21Envelope` a zero-test-edit change later, which is the whole point of taking it as a
    fixture instead of hardcoding `LiUsbEnvelope()`.

- Produces (Tasks 3-7 depend on these EXACT signatures - do not rename, do not re-type):

```python
# src/railctl/station/facade.py
INTERFACE_STATUS_USAGE: Final[int] = 0x09          # 01 09 08 -> ValueError -> exit 2
PROTOCOL_NAME: Final[str] = "xpressnet"

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
             capabilities_path: Path | None = None, timing: Timing = TIMING) -> Station: ...

    link: Link
    timing: Timing
    default_address: int | None
    @property
    def capabilities(self) -> Capabilities: ...
    @property
    def threshold(self) -> int: ...
    @property
    def description(self) -> str: ...
    @property
    def identity(self) -> str: ...

    def now(self) -> float: ...
    def pause(self, seconds: float) -> None: ...
    def emit(self, name: str, payload: dict[str, object]) -> None: ...
    def learn(self, **updates: object) -> None: ...
    def record(self, **updates: object) -> None: ...
    def exchange(self, telegram: bytes, *, timeout: float) -> Reply: ...
    def resolve_address(self, address: int | None) -> int | None: ...
    def register_cache(self, clear: Callable[[], None]) -> None: ...
    def invalidate_caches(self) -> None: ...

    def close(self) -> None: ...
    def __enter__(self) -> Station: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...

    def version(self) -> StationVersion: ...
    def status(self) -> StationStatus: ...
    def power_on(self) -> None: ...
    def power_off(self) -> None: ...
    def emergency_stop(self, address: int | None = None) -> None: ...
    def events(self, *, interval: float = 0.25) -> Iterator[StationEvent]: ...
```

This task's `__init__` does not assign `self.programmer`: Task 4 extends this same method to add
`self.programmer = CvProgrammer(self)` - a plain public attribute, never `self._programmer` and
never a property - once `CvProgrammer` exists. Nothing here stubs it out early. Likewise,
`resolve_address` above is **only** `self.default_address if address is None else address` - no
range check, no event, no validation. Task 3 adds a *different*, separately-named method,
`_validate_address(address: int) -> None`, for the range-check-and-band-warning behaviour; the two
must never be merged, and this task's `resolve_address` must never be renamed to imply it validates.
`link: Link` above is a plain attribute (`self.link = link` in `__init__`), never a `@property` -
nothing here should wrap it in one "for consistency" with `capabilities`.

```python
# tests/station/conftest.py
class Bench:
    def __init__(self, *, chunk_size: int | None = None,
                 envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
                 capabilities: Capabilities | None = None,
                 default_address: int | None = BENCH_DEFAULT_ADDRESS,
                 capabilities_path: Path | None = None,
                 timing: Timing = TIMING,
                 **capability_overrides: object) -> None: ...
    envelope: LiUsbEnvelope
    clock: FakeClock
    transport: FakeTransport
    link: Link
    station: Station
    events: list[tuple[str, dict[str, object]]]
    on_event_hook: Callable[[str, dict[str, object]], None] | None
    def expect(self, request: bytes, reply: bytes | tuple[bytes, ...] = b"", *,
               broadcast: bytes | tuple[bytes, ...] = ()) -> Bench: ...
    def push(self, telegram: bytes) -> Bench: ...
    def reply(self, telegram: bytes) -> Bench: ...
    def open(self) -> Bench: ...
    @property
    def sent(self) -> list[bytes]: ...
    def unframe(self, framed: bytes) -> bytes: ...
    def event_names(self) -> list[str]: ...

@pytest.fixture
def bench_factory(chunk_size, envelope_factory) -> Callable[..., Bench]: ...
@pytest.fixture
def bench(bench_factory) -> Bench: ...      # already opened
```

This is the full surface. Every keyword and method above is final for the whole plan: Task 2
writes `tests/station/conftest.py` once, in Step 1 below, and no later task edits it again
(ADDENDUM §A.0) - a task that needs one branch in isolation monkeypatches the single method on
`bench.station.programmer` instead of adding a fixture keyword.

**What the tests must pin** (twelve behaviours; every one is a place a plausible-looking
"simplification" breaks something this project has already broken once):

1. `power_on()` sends exactly `21 81 A0` and returns on `61 01` with no second exchange; same for
   `power_off()` / `61 00`.
2. A disagreeing power reply triggers exactly one `status()` re-read after `TIMING.power_settle`,
   then `TrackPowerError` - never a loop, never a second re-read.
3. `version()` is cached for the object's lifetime (two calls, one exchange); `status()` never is
   (two calls, two exchanges) - both pinned in the same test so neither regresses unnoticed.
4. `emergency_stop(None)` sends `80 80`, `emergency_stop(3)` sends `92 03 XOR`, and **neither
   writes a power telegram**.
5. `exchange()` never invents a capability verdict: `InterfaceStatus(0x09)` is a `ValueError`
   (usage), every other `InterfaceStatus` is `TransportError`, `Unsupported` is
   `UnsupportedCommandError`, `Other` with reason `checksum` or `length` is `ProtocolError`
   (exit 4 - the LINK damaged the reply, never the station), `Other` with reason `empty` or
   `unknown_form` stays on the base `RailctlError` (exit 9 - the REPLY TABLE is incomplete, never
   the station) - and every `TRANSIENT_REPLIES` member (`ShortCircuit`, `TrackShortCircuit`,
   `Busy`, `StationBusy`, `TransferError`) comes back as a plain `Reply` object, not an exception,
   `StationBusy` included, because `exchange()` has no CV number to attach to a `ProgrammingError`
   and a later task's caller does.
6. `learn()` raises `ValueError` outside `LEARNABLE_FIELDS`; `record()` accepts anything.
7. `close()` flushes capabilities only when a path is set **and** something changed, never writes
   when nothing changed, and **never sends a power telegram**.
8. `close()` and `power_off()` call every callback registered through `register_cache`;
   `version()` and `status()` do not.
9. `threshold` is `capabilities.loco_address_threshold` when set, else `XPRESSNET
   .long_address_threshold` (100) - never 128, even though this station's family id is Z21's.
10. Every public method takes the same `threading.RLock`; calling `status()` from inside an
    `on_event` callback must not deadlock.
11. `events()` does not hold the lock while suspended at a `yield` - pinned by calling
    `station.status()` between two `next()` calls.
12. The whole module runs under both `chunk_size` ids.

**Decisions already made - do not re-open:**

- **Two clock shapes.** `Link` takes `clock: Clock` (an object). `Station` takes two callables,
  `clock: Callable[[], float]` and `sleep: Callable[[float], None]`. In the fixture, `Link` gets
  `clock=self.clock` and `Station` gets `clock=self.clock.monotonic, sleep=self.clock.sleep`.
- **`Station.open` sequence**: `open_link(target)` -> read `link.identity` -> `Capabilities.load
  (capabilities_path, identity)` when a path is given, else `Capabilities.unknown(identity)` ->
  construct. The identity is not knowable before the link opens, so this order is forced.
- **`exchange()` is the only place in `station/` that calls `link.request` and `replies.parse`.**
  Every later task goes through it - which is why its mapping table is pinned here, once, rather
  than reinvented in each caller.
- **The module docstring carries the two LI-USB facts**: exactly one solicited reply per command
  (never an ack *followed by* data, so "first solicited frame" is always the right one), and
  broadcasts are buffered while a command is outstanding, so a passive wait only observes pushes
  when nothing is in flight.
- **`StationEvent.name` is a dotted name** (`power.on`, `power.off`, `loco.emergency_stop`,
  `service.entered`, `reply.unknown`); `detail` is human text; `payload` is JSON-safe, with the
  frame's raw bytes going in as `payload["telegram"]` via `hex_bytes()` - a runtime string, never
  a byte literal, so layering rule 1 stays green.
- **`events()` must not swallow `KeyboardInterrupt`** - the future `monitor` CLI command depends on
  it propagating. Do not wrap the generator body in a bare `except Exception`.
- **This file says "port" and "terminal", never the words test_layering.py's rule 1 forbids** (no
  "socket", no word starting "tty" - `\btty` is case-insensitive and anchored at a word start, so
  "identity" and "battery" are fine but "tty" or "ttyUSB0" are not).
- **`Bench` is the interface every later station task builds on, complete as written here.** The
  whole surface - `expect`/`push`/`reply`/`open`/`.sent`/`unframe`/`event_names`, and the
  `chunk_size`, `envelope_cls`, `capabilities`, `default_address`, `capabilities_path`, `timing`
  and `**capability_overrides` keywords - is written once, in this task's Step 1, and no other
  task edits `tests/station/conftest.py` again (ADDENDUM §A.0): the fixture the sheet ordered
  every station test onto has to be specified once by the one task that writes it, or each
  consumer re-imagines it and none of them agree. `Bench.__init__` takes `envelope_cls` (default
  `LiUsbEnvelope`) for the same reason `bench_factory` depends on `envelope_factory` as well as
  `chunk_size`: the day a second envelope (Z21 LAN) lands, this fixture is parametrised over it
  with zero test edits, which is the whole point of the M5 acceptance sentence running under "both
  envelope parameters, both chunk sizes". Tasks 4-7 build every one of their tests on `bench` /
  `bench_factory` and must not define their own `make_open_station`, `FakeStation`, or `FakeCtx`
  helper that constructs a `Station` a different way - a test that needs one branch in isolation
  monkeypatches the single method on `bench.station.programmer` instead.

---

- [ ] **Step 1: Write the Station test fixture**

`Bench` mirrors `tests/unit/test_link.py`'s `Fixture` one layer up: `expect`/`push` script the
wire exactly as the envelope would frame it, and every test talks to `station`, never to bytes.
Framing lives entirely inside `Bench` - `expect()` and `push()` take bare telegrams in and frame
them going out; `.sent` unframes what came back - so no test in `tests/station/` ever spells the
`FF FE` prefix, which is what `tests/unit/test_envelope_isolation.py` enforces by scanning this
whole directory, and what lets a future `Z21Envelope` run this suite with no edit here
(ADDENDUM §A.0). `on_event_hook` is the one addition beyond the `Fixture` pattern: it lets a
single test run code from *inside* the `on_event` callback, which is the only way to prove the
facade's lock is reentrant (Step 7 uses it).

```python
# tests/station/conftest.py
"""The one test double every tests/station/ test is built on.

Bare telegrams in, bare telegrams out. The envelope under test does all the framing, so nothing
in tests/station/ ever names the framing prefix - which tests/unit/test_envelope_isolation.py
enforces (tests/station is in its SCANNED list and this file is not on its ALLOWED list), and
which is what lets a future Z21Envelope run this whole suite with no edit here.

Two axes come from tests/conftest.py and apply to every test that takes `bench` or
`bench_factory`: `chunk_size` (whole-frame, then one byte at a time) and `envelope_factory` (the
envelope class under test). Neither is ever passed by a test.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.link import Link
from railctl.station.capabilities import Capabilities
from railctl.station.facade import Station
from railctl.station.timing import TIMING, Timing
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.commands import cmd_station_version

BENCH_IDENTITY = "bench"
BENCH_DEFAULT_ADDRESS = 3
# 21 21 00 - the telegram Link.open() sends itself. Taken from the encoder rather than typed, so
# this file cannot drift from link.py's _HANDSHAKE_TELEGRAM (tests/unit/test_link.py pins those two
# against each other already).
_HANDSHAKE_REQUEST = cmd_station_version()
_HANDSHAKE_REPLY = b"\x63\x21\x40\x12\x10"


class Bench:
    """An open Station over a scripted FakeTransport, speaking bare telegrams."""

    def __init__(
        self,
        *,
        chunk_size: int | None = None,
        envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
        capabilities: Capabilities | None = None,
        default_address: int | None = BENCH_DEFAULT_ADDRESS,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
        **capability_overrides: object,
    ) -> None:
        self._envelope_cls = envelope_cls
        self.envelope = envelope_cls()
        self.clock = FakeClock()
        self.transport = FakeTransport(clock=self.clock, chunk_size=chunk_size)
        self.link = Link(self.transport, self.envelope, clock=self.clock)
        self.events: list[tuple[str, dict[str, object]]] = []
        self.on_event_hook: Callable[[str, dict[str, object]], None] | None = None
        self._handshake_writes = 0
        caps = Capabilities.unknown(BENCH_IDENTITY) if capabilities is None else capabilities
        if capability_overrides:
            # with_learned accepts any real field name and raises ValueError naming a wrong one,
            # so bench_factory(loco_address_threshold=128) is checked rather than swallowed.
            caps = caps.with_learned(**capability_overrides)
        self.station = Station(
            self.link,
            caps,
            default_address=default_address,
            capabilities_path=capabilities_path,
            timing=timing,
            clock=self.clock.monotonic,
            sleep=self.clock.sleep,
            on_event=self._on_event,
        )

    # -- scripting ---------------------------------------------------------
    def expect(
        self,
        request: bytes,
        reply: bytes | tuple[bytes, ...] = b"",
        *,
        broadcast: bytes | tuple[bytes, ...] = (),
    ) -> Bench:
        """Queue exactly one exchange, in bare telegrams.

        `request` is the next telegram the station must send; a mismatch fails the test from
        inside FakeTransport, naming both telegrams in hex.

        `reply` is one telegram, or a tuple of telegrams delivered as one burst of solicited
        frames. `reply=b""` scripts a station that ACCEPTS the request and then says nothing, so
        the exchange ends in LinkTimeout.

        SILENCE IS ALWAYS SCRIPTED, NEVER IMPLIED. An unscripted request does not time out - it
        makes FakeTransport raise AssertionError("the script is exhausted"). A test that expects a
        timeout must still call expect(), and must write `reply=b""` explicitly rather than
        leaving the argument off, so the intent is greppable.

        `broadcast` appends unsolicited frames after the solicited ones, for the rare case where a
        broadcast must arrive inside one exchange. A broadcast that merely has to be seen belongs
        in push() before the call.
        """
        self.transport.expect(
            self.envelope.frame(Kind.SOLICITED, request),
            reply=self._frames(Kind.SOLICITED, reply) + self._frames(Kind.UNSOLICITED, broadcast),
        )
        return self

    def push(self, telegram: bytes) -> Bench:
        """An UNSOLICITED frame with no request behind it - a broadcast.

        Solicited frames answer a command and are consumed by the exchange that asked for them;
        an unsolicited one is dispatched to Station's on_event as it passes and never satisfies a
        pending request. This is the only way a test produces a broadcast.
        """
        self.transport.queue(self.envelope.frame(Kind.UNSOLICITED, telegram))
        return self

    def reply(self, telegram: bytes) -> Bench:
        """One solicited frame, queued directly. For an on_write responder answering the request
        it was just handed; tests use expect() instead."""
        self.transport.queue(self.envelope.frame(Kind.SOLICITED, telegram))
        return self

    def open(self) -> Bench:
        self.expect(_HANDSHAKE_REQUEST, _HANDSHAKE_REPLY)
        self.link.open()
        self._handshake_writes = len(self.transport.written)
        return self

    # -- what was sent -----------------------------------------------------
    @property
    def sent(self) -> list[bytes]:
        """Every telegram written since open(), BARE and in order.

        transport.written holds what Link handed the transport, which is framed. Comparing that
        against a bare telegram silently never matches, so no test reads it: they read this.
        The handshake is excluded - it is fixture ceremony, not part of any test's subject - so
        `len(bench.sent)` counts the operation's own telegrams, and `bench.sent.count(...)` of
        cmd_station_version() counts version() calls without the open() handshake in the tally.

        Decoded through a FRESH envelope instance so the one under test keeps its buffer and its
        counters untouched.
        """
        decoder = self._envelope_cls()
        decoder.feed(b"".join(self.transport.written[self._handshake_writes :]))
        out: list[bytes] = []
        while (frame := decoder.pop()) is not None:
            out.append(frame.payload)
        return out

    def unframe(self, framed: bytes) -> bytes:
        """One framed request (as an on_write responder is handed) back to its bare telegram."""
        decoder = self._envelope_cls()
        decoder.feed(framed)
        frame = decoder.pop()
        assert frame is not None, f"not one complete frame: {len(framed)} bytes"
        return frame.payload

    # -- events ------------------------------------------------------------
    def _on_event(self, name: str, payload: dict[str, object]) -> None:
        self.events.append((name, payload))
        if self.on_event_hook is not None:
            self.on_event_hook(name, payload)

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    # -- internals ---------------------------------------------------------
    def _frames(self, kind: Kind, telegrams: bytes | tuple[bytes, ...]) -> bytes:
        if isinstance(telegrams, bytes):
            telegrams = (telegrams,)
        return b"".join(self.envelope.frame(kind, one) for one in telegrams if one)


@pytest.fixture
def bench_factory(chunk_size, envelope_factory) -> Callable[..., Bench]:
    """Build and OPEN a fresh Bench. One handshake exchange is already spent.

    Depends on both `chunk_size` and `envelope_factory` (tests/conftest.py), so every test built
    on bench/bench_factory runs under both axes, not just the byte-delivery one.
    """

    def make(**kwargs: object) -> Bench:
        return Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs).open()

    return make


@pytest.fixture
def bench(bench_factory: Callable[..., Bench]) -> Bench:
    return bench_factory()
```

This step produces no test to run yet - `conftest.py` alone has nothing to collect, and it
imports `railctl.station.facade`, which does not exist until Step 4. Step 3 below is the first
point a command runs, and its collection error covers this file too.

- [ ] **Step 2: Write the failing Station tests**

```python
# tests/station/test_power_and_status.py
"""Session, version, status, power, emergency stop and the event iterator.

The two LI-USB facts that shape every test here: exactly one solicited reply per command (never
an ack followed by data), and broadcasts are buffered while a command is outstanding.
"""

from __future__ import annotations

import logging

import pytest

# Kind is still used directly below: test_close_never_writes_capabilities_for_an_unknown_link_
# identity builds its own raw Link (never bench) because Bench has no way to ask for an unknown
# transport identity, so that one test still frames its own handshake by hand. Every other
# expect()/push() call in this file goes through Bench and never spells Kind.
from railctl.envelope import Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import (
    ProtocolError,
    RailctlError,
    TrackPowerError,
    TransportError,
    exit_code_for,
)
from railctl.link import Link
from railctl.station.capabilities import UNKNOWN_IDENTITY, Capabilities
from railctl.station.facade import Station
from railctl.station.timing import TIMING
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
)
from railctl.xbus.dialect import XPRESSNET
from railctl.xbus.replies import GENERIC_ACK, TRANSIENT_REPLIES

CMD_STATION_VERSION = cmd_station_version()  # 21 21 00
VERSION_REPLY = b"\x63\x21\x40\x12\x10"  # measured: XpressNet 4.0, station id 0x12
CMD_STATION_STATUS = cmd_station_status()  # 21 24 05
STATUS_UNPOWERED = b"\x62\x22\x07\x47"  # measured: track power off
STATUS_POWERED = b"\x62\x22\x04\x44"  # auto_start_mode only: track power on
CMD_TRACK_POWER_ON = cmd_track_power_on()  # 21 81 A0
CMD_TRACK_POWER_OFF = cmd_track_power_off()  # 21 80 A1
POWER_ON_REPLY = b"\x61\x01\x60"
POWER_OFF_REPLY = b"\x61\x00\x61"
CMD_EMERGENCY_STOP_ALL = cmd_emergency_stop_all()  # 80 80
CMD_EMERGENCY_STOP_LOCO_3 = cmd_emergency_stop_loco(3, threshold=100)
CMD_EMERGENCY_STOP_LOCO_105 = cmd_emergency_stop_loco(105, threshold=100)
ACK = b"\x01\x04\x05"
INTERFACE_STATUS_USAGE_REPLY = b"\x01\x09\x08"
# Must NOT start 01 0A: link.py's _RETRY_PREFIXES retries any reply whose first two bytes are
# 01 0A, so a scripted single reply of 01 0A xx would be retried once, the FakeTransport script
# would run out, and the test would fail with "unexpected request ...; the script is exhausted"
# instead of exercising this branch at all. 01 0B (XOR 0A) is not a retry prefix.
INTERFACE_STATUS_OTHER_REPLY = b"\x01\x0b\x0a"
UNSUPPORTED_REPLY = b"\x61\x82\xe3"
SHORT_CIRCUIT_REPLY = b"\x61\x12\x73"
TRACK_SHORT_CIRCUIT_REPLY = b"\x61\x08\x69"
BUSY_REPLY = b"\x61\x1f\x7e"
STATION_BUSY_REPLY = b"\x61\x81\xe0"
UNKNOWN_FORM_REPLY = b"\x71\x00\x71"
# Same header and data byte as UNKNOWN_FORM_REPLY (71 00 71), with the trailing XOR corrupted:
# xor(71 00) is 71, not 00, so replies.parse() raises XBusChecksumError and returns
# Other(reason=REASON_CHECKSUM) before it ever reaches the "no row for this form" fallback.
# REASON_CHECKSUM and REASON_UNKNOWN_FORM are two different byte strings here, not just two
# different labels hung on the same one.
BAD_CHECKSUM_REPLY = b"\x71\x00\x00"
EMERGENCY_STOP_BROADCAST_BYTES = b"\x81\x00\x81"


# -- power and status --------------------------------------------------------


def test_power_on_reads_the_solicited_reply_in_one_exchange(bench):
    bench.expect(CMD_TRACK_POWER_ON, POWER_ON_REPLY)
    bench.station.power_on()
    assert bench.transport.script_pending == []
    assert bench.sent.count(CMD_TRACK_POWER_ON) == 1


def test_power_off_reads_the_solicited_reply_in_one_exchange(bench):
    bench.expect(CMD_TRACK_POWER_OFF, POWER_OFF_REPLY)
    bench.station.power_off()
    assert bench.transport.script_pending == []
    assert bench.sent.count(CMD_TRACK_POWER_OFF) == 1


def test_power_on_disagreement_re_reads_once_then_raises(bench):
    bench.expect(CMD_TRACK_POWER_ON, POWER_OFF_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_UNPOWERED)
    with pytest.raises(TrackPowerError):
        bench.station.power_on()
    assert bench.clock.monotonic() == pytest.approx(TIMING.power_settle)
    assert bench.transport.script_pending == []


def test_power_off_disagreement_re_reads_once_then_raises(bench):
    bench.expect(CMD_TRACK_POWER_OFF, POWER_ON_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    with pytest.raises(TrackPowerError):
        bench.station.power_off()
    assert bench.clock.monotonic() == pytest.approx(TIMING.power_settle)
    assert bench.transport.script_pending == []


def test_power_off_invalidates_caches_even_when_it_ultimately_raises(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_TRACK_POWER_OFF, POWER_ON_REPLY)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    with pytest.raises(TrackPowerError):
        bench.station.power_off()
    assert calls == ["clear"]


def test_version_is_cached_but_status_is_never_cached(bench):
    bench.expect(CMD_STATION_VERSION, VERSION_REPLY)
    first = bench.station.version()
    second = bench.station.version()
    assert first is second
    assert first.version == "4.0"
    assert first.family == "Z21"
    assert bench.sent.count(CMD_STATION_VERSION) == 1

    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.station.status()
    bench.station.status()
    assert bench.sent.count(CMD_STATION_STATUS) == 2


# -- emergency stop -----------------------------------------------------------
# Bench now opens with `default_address=BENCH_DEFAULT_ADDRESS` (3), not None. That default is
# invisible to every test below: `emergency_stop(None)` branches on `address is None` and sends
# `80 80` directly (see `emergency_stop`'s body above) - it never calls `resolve_address`, so
# whatever `bench.station.default_address` happens to be plays no part in what gets sent.


def test_emergency_stop_all_sends_80_80_and_no_power_telegram(bench):
    bench.expect(CMD_EMERGENCY_STOP_ALL, ACK)
    bench.station.emergency_stop()
    assert bench.sent[-1] == CMD_EMERGENCY_STOP_ALL
    assert CMD_TRACK_POWER_ON not in bench.sent
    assert CMD_TRACK_POWER_OFF not in bench.sent


def test_emergency_stop_address_sends_92_and_no_power_telegram(bench):
    bench.expect(CMD_EMERGENCY_STOP_LOCO_3, ACK)
    bench.station.emergency_stop(3)
    assert bench.sent[-1] == CMD_EMERGENCY_STOP_LOCO_3
    assert CMD_TRACK_POWER_ON not in bench.sent
    assert CMD_TRACK_POWER_OFF not in bench.sent
    assert bench.event_names() == []  # address 3 is well below the divergence band


def test_emergency_stop_in_the_divergence_band_emits_address_band_unverified(bench):
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    bench.station.emergency_stop(105)
    assert bench.events == [("address.band_unverified", {"address": 105, "threshold": 100})]


def test_status_call_from_inside_on_event_does_not_deadlock(bench):
    """emit() runs while emergency_stop() still holds the lock; RLock (not Lock) is what lets
    status() re-enter from inside that same callback without hanging the test forever."""
    reentered: list[bool] = []

    def reenter(name: str, payload: dict[str, object]) -> None:
        if name == "address.band_unverified":
            bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
            result = bench.station.status()
            reentered.append(result.track_power)

    bench.on_event_hook = reenter
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    bench.station.emergency_stop(105)
    assert reentered == [True]


def test_a_bad_on_event_callback_cannot_break_the_operation(bench, caplog):
    def explode(name: str, payload: dict[str, object]) -> None:
        raise RuntimeError("bad callback")

    bench.on_event_hook = explode
    bench.expect(CMD_EMERGENCY_STOP_LOCO_105, ACK)
    with caplog.at_level(logging.WARNING, logger="railctl.station"):
        bench.station.emergency_stop(105)  # must not raise
    assert "bad callback" in caplog.text


# -- exchange() mapping table -------------------------------------------------


def test_exchange_returns_generic_ack_for_a_plain_command(bench):
    bench.expect(CMD_EMERGENCY_STOP_ALL, ACK)
    reply = bench.station.exchange(CMD_EMERGENCY_STOP_ALL, timeout=TIMING.li_ack_normal)
    assert reply == GENERIC_ACK


def test_exchange_maps_interface_status_09_to_value_error(bench):
    bench.expect(CMD_STATION_VERSION, INTERFACE_STATUS_USAGE_REPLY)
    with pytest.raises(ValueError):
        bench.station.version()


def test_exchange_maps_any_other_interface_status_to_transport_error(bench):
    bench.expect(CMD_STATION_VERSION, INTERFACE_STATUS_OTHER_REPLY)
    with pytest.raises(TransportError) as caught:
        bench.station.version()
    assert "0B" in str(caught.value)  # INTERFACE_STATUS_OTHER_REPLY's code byte is 0x0B


def test_exchange_maps_unsupported_to_unsupported_command_error(bench):
    from railctl.errors import UnsupportedCommandError

    bench.expect(CMD_STATION_VERSION, UNSUPPORTED_REPLY)
    with pytest.raises(UnsupportedCommandError):
        bench.station.version()


def test_exchange_maps_an_unknown_form_to_railctl_error_carrying_the_bytes(bench):
    bench.expect(CMD_STATION_VERSION, UNKNOWN_FORM_REPLY)
    with pytest.raises(RailctlError) as caught:
        bench.station.version()
    assert "71 00 71" in str(caught.value)


def test_exchange_keeps_a_bad_cable_and_an_unknown_reply_form_apart(bench):
    """xbus/replies.py's own docstring on `Other.reason`: "Collapsing these into one value
    leaves the station unable to tell a bad cable from a reply form we do not know." A
    REASON_CHECKSUM/REASON_LENGTH `Other` is the LINK damaging bytes and gets `ProtocolError`
    (exit 4); a REASON_EMPTY/REASON_UNKNOWN_FORM `Other` is an incomplete reply table and stays
    on the base `RailctlError` (exit 9). This is the one test that pins the two exit codes
    apart from each other - folding either mapping into the other makes this go red."""
    bench.expect(CMD_STATION_VERSION, BAD_CHECKSUM_REPLY)
    with pytest.raises(ProtocolError) as bad_cable:
        bench.station.version()
    assert exit_code_for(bad_cable.value) == 4

    bench.expect(CMD_STATION_VERSION, UNKNOWN_FORM_REPLY)
    with pytest.raises(RailctlError) as unknown_form:
        bench.station.version()
    assert exit_code_for(unknown_form.value) == 9
    assert exit_code_for(bad_cable.value) != exit_code_for(unknown_form.value)


@pytest.mark.parametrize(
    "reply_bytes",
    [SHORT_CIRCUIT_REPLY, TRACK_SHORT_CIRCUIT_REPLY, BUSY_REPLY, STATION_BUSY_REPLY],
    ids=["short_circuit", "track_short_circuit", "busy", "station_busy"],
)
def test_exchange_returns_transient_replies_unchanged(bench, reply_bytes):
    """None of TRANSIENT_REPLIES' five members says anything about whether an opcode is
    implemented (xbus/replies.py's own docstring on TRANSIENT_REPLIES), so exchange() must not
    turn any of them into an exception - StationBusy included, even though it is the one member
    that can follow ANY command. A later CV task needs the raw reply back so it can attach the
    CV number ProgrammingError carries, which exchange() has no way to know. The fifth member,
    TransferError, is not exercised here: Link retries a 61 80 reply once and raises
    LinkProtocolError itself on a second one (_RETRY_PREFIXES), so a bare TransferError can
    never actually reach exchange() through link.request() to be scripted as a single reply."""
    bench.expect(CMD_STATION_VERSION, reply_bytes)
    reply = bench.station.exchange(CMD_STATION_VERSION, timeout=TIMING.li_ack_normal)
    assert reply in TRANSIENT_REPLIES


# -- capability learning -------------------------------------------------------


def test_learn_refuses_a_field_outside_learnable_fields(bench):
    with pytest.raises(ValueError):
        bench.station.learn(z21_cv_opcodes=True)


def test_learn_accepts_every_learnable_field(bench):
    bench.station.learn(pom_read=True, pom_result_channel="broadcast")
    assert bench.station.capabilities.pom_read is True
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_record_accepts_any_field_including_non_learnable_ones(bench):
    bench.station.record(z21_cv_opcodes=True, function_groups_4_5=False)
    assert bench.station.capabilities.z21_cv_opcodes is True
    assert bench.station.capabilities.function_groups_4_5 is False


# -- session lifecycle ---------------------------------------------------------


def test_close_flushes_learned_capabilities_when_a_path_is_set(bench_factory, tmp_path):
    path = tmp_path / "capabilities.json"
    fixture = bench_factory(capabilities_path=path)
    fixture.station.learn(pom_read=True)
    fixture.station.close()
    assert path.exists()
    # "bench", not "fake": Bench seeds its Capabilities with BENCH_IDENTITY ("bench"), a label
    # chosen independently of FakeTransport's own identity ("fake") - see ADDENDUM §A.2, "no test
    # may assert those two are equal". Loading under "fake" here would read no entry at all and
    # silently leave reloaded.pom_read at None, which is a false pass, not a real one.
    reloaded = Capabilities.load(path, "bench")
    assert reloaded.pom_read is True
    assert CMD_TRACK_POWER_ON not in fixture.sent
    assert CMD_TRACK_POWER_OFF not in fixture.sent


def test_close_does_not_write_capabilities_when_nothing_was_learned(bench_factory, tmp_path):
    path = tmp_path / "capabilities.json"
    fixture = bench_factory(capabilities_path=path)
    fixture.station.close()
    assert not path.exists()


def test_close_never_writes_capabilities_for_an_unknown_link_identity(tmp_path):
    """A transport with no stable identity must never grow a `capabilities.json` entry keyed
    "unknown" - two unrelated stations would then silently share one profile."""
    envelope = LiUsbEnvelope()
    clock = FakeClock()
    transport = FakeTransport(clock=clock, identity=UNKNOWN_IDENTITY)
    link = Link(transport, envelope, clock=clock)
    transport.expect(
        envelope.frame(Kind.SOLICITED, CMD_STATION_VERSION),
        reply=envelope.frame(Kind.SOLICITED, VERSION_REPLY),
    )
    link.open()
    path = tmp_path / "capabilities.json"
    station = Station(
        link,
        Capabilities.unknown(UNKNOWN_IDENTITY),
        capabilities_path=path,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    station.learn(pom_read=True)
    station.close()
    assert not path.exists()


def test_close_and_power_off_invalidate_registered_caches(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_TRACK_POWER_OFF, POWER_OFF_REPLY)
    bench.station.power_off()
    assert calls == ["clear"]
    bench.station.close()
    assert calls == ["clear", "clear"]


def test_version_and_status_do_not_invalidate_registered_caches(bench):
    calls: list[str] = []
    bench.station.register_cache(lambda: calls.append("clear"))
    bench.expect(CMD_STATION_VERSION, VERSION_REPLY)
    bench.station.version()
    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    bench.station.status()
    assert calls == []


def test_threshold_defaults_to_xpressnet_when_capabilities_unset(bench):
    assert bench.station.threshold == XPRESSNET.long_address_threshold == 100


def test_threshold_uses_capabilities_when_measured(bench_factory):
    caps = Capabilities.unknown("fake").with_learned(loco_address_threshold=128)
    fixture = bench_factory(capabilities=caps)
    assert fixture.station.threshold == 128


# -- events() ------------------------------------------------------------------


def test_events_do_not_hold_the_lock_across_a_yield(bench):
    bench.push(EMERGENCY_STOP_BROADCAST_BYTES)
    bench.push(POWER_ON_REPLY)
    iterator = bench.station.events(interval=0.0)

    first = next(iterator)
    assert first.name == "loco.emergency_stop"

    bench.expect(CMD_STATION_STATUS, STATUS_POWERED)
    # This call would deadlock on the same RLock if events() held it across the yield above.
    assert bench.station.status().track_power is True

    second = next(iterator)
    assert second.name == "power.on"
    assert second.payload["telegram"] == "61 01 60"
```

- [ ] **Step 3: Run the tests and see them fail for the right reason**

```bash
uv run pytest tests/station/
```

Expected: **collection error**, not a test failure -
`ModuleNotFoundError: No module named 'railctl.station.facade'` - raised while collecting
`tests/station/conftest.py`, because `Station` does not exist yet. Confirm the traceback names
`railctl.station.facade`, not something in Task 1's modules: if it names `railctl.station.timing`
or `railctl.station.capabilities` instead, Task 1 did not merge cleanly and this task must stop
and say so rather than paper over it.

- [ ] **Step 4: Implement the facade**

```python
# src/railctl/station/facade.py
"""The one object the CLI and a future TUI talk to: session, telemetry, power, and CV
operations built in later tasks. `station/` never sees framing bytes, port names, or CV
arithmetic - it speaks only in Station API terms, built through `xbus`.

Two facts from the LI documentation shape everything below:

* Exactly one solicited reply per command - the generic ack, an interface status frame, or the
  data. There is never an ack followed by data, so "first solicited frame" is always the right
  one.
* Broadcasts are buffered while a command is outstanding and delivered after the command reply.
  A passive wait (`events()`) only observes pushes when nothing else is in flight.

Every public method holds one `threading.RLock` for its whole body. RLock, not Lock: a callback
registered through `on_event` can call back into `status()` while `emergency_stop()` still holds
the lock (see `_warn_if_unverified_band`), and a plain Lock would make that deadlock instead of
reenter.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from railctl.envelope import Frame, hex_bytes
from railctl.errors import (
    ProtocolError,
    RailctlError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
)
from railctl.link import Link
from railctl.station.capabilities import LEARNABLE_FIELDS, UNKNOWN_IDENTITY, Capabilities
from railctl.station.timing import TIMING, Timing
from railctl.station.types import StationEvent
from railctl.transport import open_link
from railctl.xbus import replies
from railctl.xbus.commands import (
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
)
from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET
from railctl.xbus.replies import (
    POWER_OFF,
    POWER_ON,
    REASON_CHECKSUM,
    REASON_LENGTH,
    UNSUPPORTED,
    EmergencyStopBroadcast,
    InterfaceStatus,
    Other,
    Reply,
    ServiceModeEntry,
    StationStatus,
    StationVersion,
)

# 01 09 08 measured during D10 (docs/probe-results.md): the interface's answer when a request
# was malformed on the way OUT, never a fact about the decoder or the station's support for an
# opcode. A bare ValueError (not a RailctlError) is deliberate: cli/_errors.py maps ValueError to
# exit code 2 (usage), and this is always a railctl bug, never something the operator caused.
INTERFACE_STATUS_USAGE: Final[int] = 0x09
# Named once so an error message can say which dialect produced an unrecognised reply, ahead of
# the day a second dialect (Z21 LAN) exists to be confused with this one.
PROTOCOL_NAME: Final[str] = "xpressnet"

_log = logging.getLogger("railctl.station")


class Station:
    def __init__(
        self,
        link: Link,
        capabilities: Capabilities,
        *,
        default_address: int | None = None,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.link = link
        self.timing = timing
        self.default_address = default_address
        self._capabilities = capabilities
        self._capabilities_path = capabilities_path
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event
        self._lock = threading.RLock()
        self._version_cache: StationVersion | None = None
        self._dirty = False
        self._cache_clears: list[Callable[[], None]] = []

    @classmethod
    def open(
        cls,
        target: str = "auto",
        *,
        default_address: int | None = None,
        capabilities_path: Path | None = None,
        timing: Timing = TIMING,
    ) -> Station:
        """Resolve `target`, open the link, then load capabilities by its identity.

        The identity is not knowable before the link opens - it comes from the transport, which
        `open_link` has already asked - so this order (link, then capabilities) is forced, not a
        style choice.
        """
        link = open_link(target)
        capabilities = (
            Capabilities.load(capabilities_path, link.identity)
            if capabilities_path is not None
            else Capabilities.unknown(link.identity)
        )
        return cls(
            link,
            capabilities,
            default_address=default_address,
            capabilities_path=capabilities_path,
            timing=timing,
        )

    # -- read-only surface ---------------------------------------------------
    @property
    def capabilities(self) -> Capabilities:
        with self._lock:
            return self._capabilities

    @property
    def threshold(self) -> int:
        """The long-address cutoff for `encode_loco_address`: measured once the doctor runs,
        `XPRESSNET.long_address_threshold` (100) until then - never 128, even though this
        station's family id is Z21's. Z21's threshold applies only once D10 confirms it."""
        with self._lock:
            measured = self._capabilities.loco_address_threshold
        return measured if measured is not None else XPRESSNET.long_address_threshold

    @property
    def description(self) -> str:
        return self.link.description

    @property
    def identity(self) -> str:
        return self.link.identity

    # -- collaborator surface (CvProgrammer and doctor use exactly these) -----
    def now(self) -> float:
        return self._clock()

    def pause(self, seconds: float) -> None:
        self._sleep(seconds)

    def emit(self, name: str, payload: dict[str, object]) -> None:
        """Call the on_event callback, if any. A raising callback must not lose the operation
        that triggered it - the same reasoning as Link._dispatch for wire events."""
        if self._on_event is None:
            return
        try:
            self._on_event(name, payload)
        except Exception:
            _log.warning("on_event callback raised for %s", name, exc_info=True)

    def learn(self, **updates: object) -> None:
        """Update a capability a normal operation can establish without risk.

        Restricted to LEARNABLE_FIELDS (spec line 844): everything else needs an explicit
        `railctl doctor` run, because establishing it means sending an opcode a normal operation
        never sends. `record()` below is the doctor-only escape hatch with no such restriction.
        """
        with self._lock:
            unknown = sorted(set(updates) - LEARNABLE_FIELDS)
            if unknown:
                raise ValueError(
                    f"not learnable outside `railctl doctor`: {unknown}; "
                    f"learnable fields are {sorted(LEARNABLE_FIELDS)}"
                )
            self._capabilities = self._capabilities.with_learned(**updates)
            self._dirty = True

    def record(self, **updates: object) -> None:
        """Doctor-only: update any capability field, learnable or not."""
        with self._lock:
            self._capabilities = self._capabilities.with_learned(**updates)
            self._dirty = True

    def exchange(self, telegram: bytes, *, timeout: float) -> Reply:
        """Send one telegram, parse its solicited reply, and raise for every reply that is not
        an answer at all - never for one that merely disagrees with what the caller hoped for.

        This is the ONE place in `station/` that calls `link.request` and `replies.parse`, so
        this mapping table is written once here rather than reinvented per caller:

        * `InterfaceStatus(0x09)` -> `ValueError` - a malformed request, a railctl bug.
        * any other `InterfaceStatus` -> `TransportError` - the interface had a problem; this is
          never a capability verdict.
        * `Unsupported` (61 82) -> `UnsupportedCommandError` - the one reply that IS a real "no".
        * `Other` with reason `checksum` or `length` -> `ProtocolError` (exit 4): the LINK
          damaged or truncated the reply. Collapsing this into the row below would make a bad
          cable and an incomplete reply table indistinguishable at the exit code.
        * `Other` with reason `empty` or `unknown_form` -> the base `RailctlError` (exit 9): the
          reply arrived intact, but this REPLY TABLE has no row for it yet - the station is not
          at fault.
        * everything else - GenericAck, StationVersion, StationStatus, PowerState,
          EmergencyStopBroadcast, ServiceModeEntry, every CV reply Tasks 4-6 add, and every
          `TRANSIENT_REPLIES` member (ShortCircuit, TrackShortCircuit, Busy, StationBusy,
          TransferError) - is returned untouched. None of `TRANSIENT_REPLIES`' five members says
          anything about whether an opcode is implemented (that module's own docstring), so this
          method must not turn any of them into an exception, `StationBusy` included, even though
          it is the one member that can follow ANY command: a later CV caller attaches the CV
          number `ProgrammingError` carries, or applies its own retry policy, which this method
          has no way to know.
        """
        with self._lock:
            reply = replies.parse(self.link.request(telegram, timeout=timeout))
            if isinstance(reply, InterfaceStatus):
                if reply.code == INTERFACE_STATUS_USAGE:
                    raise ValueError(
                        f"{hex_bytes(telegram)} was rejected as malformed (interface status "
                        f"{INTERFACE_STATUS_USAGE:02X}); this is a railctl bug, not a station "
                        f"limit"
                    )
                raise TransportError(
                    f"the interface reported status {reply.code:02X} answering "
                    f"{hex_bytes(telegram)}; this is the interface having a problem, not a "
                    f"station capability verdict"
                )
            if reply == UNSUPPORTED:
                raise UnsupportedCommandError(
                    f"the station answered 61 82 to {hex_bytes(telegram)}: not supported"
                )
            if isinstance(reply, Other):
                if reply.reason in (REASON_CHECKSUM, REASON_LENGTH):
                    raise ProtocolError(
                        f"damaged reply to {hex_bytes(telegram)} ({reply.reason}): "
                        f"{hex_bytes(reply.telegram)}; this is the LINK, not the station or the "
                        f"decoder - check the cable, the port, and link.stats()"
                    )
                raise RailctlError(
                    f"unrecognised {PROTOCOL_NAME} reply to {hex_bytes(telegram)} "
                    f"({reply.reason}): {hex_bytes(reply.telegram)}"
                )
            return reply

    def resolve_address(self, address: int | None) -> int | None:
        with self._lock:
            return self.default_address if address is None else address

    def register_cache(self, clear: Callable[[], None]) -> None:
        with self._lock:
            self._cache_clears.append(clear)

    def invalidate_caches(self) -> None:
        with self._lock:
            for clear in self._cache_clears:
                clear()

    # -- session ---------------------------------------------------------
    def close(self) -> None:
        """Flush learned capabilities, then close the link. Never touches track power - a
        session ending is not a reason to stop the layout."""
        with self._lock:
            self.invalidate_caches()
            if (
                self._capabilities_path is not None
                and self._dirty
                and self.link.identity != UNKNOWN_IDENTITY
            ):
                self._capabilities.save(self._capabilities_path)
                self._dirty = False
            self.link.close()

    def __enter__(self) -> Station:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- operations ---------------------------------------------------------
    def version(self) -> StationVersion:
        with self._lock:
            if self._version_cache is None:
                reply = self.exchange(cmd_station_version(), timeout=self.timing.li_ack_normal)
                if not isinstance(reply, StationVersion):
                    raise RailctlError(
                        f"expected a station version reply, got {reply!r}"
                    )
                self._version_cache = reply
            return self._version_cache

    def status(self) -> StationStatus:
        with self._lock:
            reply = self.exchange(cmd_station_status(), timeout=self.timing.li_ack_normal)
            if not isinstance(reply, StationStatus):
                raise RailctlError(f"expected a station status reply, got {reply!r}")
            return reply

    def power_on(self) -> None:
        with self._lock:
            reply = self.exchange(cmd_track_power_on(), timeout=self.timing.li_ack_normal)
            self._settle_power(reply, expected=True)

    def power_off(self) -> None:
        with self._lock:
            reply = self.exchange(cmd_track_power_off(), timeout=self.timing.li_ack_normal)
            self.invalidate_caches()
            self._settle_power(reply, expected=False)

    def _settle_power(self, reply: Reply, *, expected: bool) -> None:
        """`61 01` means on and `61 00` means off, read directly off the command's own reply -
        no unconditional status round trip. A disagreeing reply gets exactly one status()
        re-read after `power_settle`; if it still disagrees, TrackPowerError. Never a loop."""
        wanted = POWER_ON if expected else POWER_OFF
        if reply == wanted:
            return
        self.pause(self.timing.power_settle)
        second = self.status()
        if second.track_power != expected:
            state = "on" if expected else "off"
            seen = "on" if second.track_power else "off"
            raise TrackPowerError(
                f"commanded track power {state} but the station still reports {seen} "
                f"after {self.timing.power_settle}s"
            )

    def emergency_stop(self, address: int | None = None) -> None:
        """`None` sends 80 80 (all locomotives); an address sends 92 AH AL X. Track power stays
        on either way, so the rest of the layout keeps running."""
        with self._lock:
            if address is None:
                self.exchange(cmd_emergency_stop_all(), timeout=self.timing.li_ack_normal)
                return
            self._warn_if_unverified_band(address)
            telegram = cmd_emergency_stop_loco(address, threshold=self.threshold)
            self.exchange(telegram, timeout=self.timing.li_ack_normal)

    def _warn_if_unverified_band(self, address: int) -> None:
        """Addresses 100..127 are where XpressNet and Z21 disagree about the wire form. Until
        the doctor's D10 measures which one this station uses, `threshold` defaults to 100 and
        this emits a warning event rather than silently guessing right or wrong."""
        if self._capabilities.loco_address_threshold is None and address in DIVERGENCE_BAND:
            self.emit("address.band_unverified", {"address": address, "threshold": self.threshold})

    def events(self, *, interval: float = 0.25) -> Iterator[StationEvent]:
        """Decode broadcasts from `link.poll(interval)` forever. The lock is held only around
        each `poll()` call, never across a `yield` - a caller that calls `status()` between two
        `next()` calls must not deadlock. `KeyboardInterrupt` is not caught here on purpose: the
        future `monitor` CLI command needs it to propagate.
        """
        while True:
            with self._lock:
                frames = self.link.poll(interval)
            for frame in frames:
                yield self._station_event(frame)

    def _station_event(self, frame: Frame) -> StationEvent:
        """`at` is required on `StationEvent` (no default), so every branch below stamps it with
        `self.now()` - the same clock `pause()` reads, never `time.monotonic()` directly, so a
        `FakeClock` in tests governs this timestamp too."""
        reply = replies.parse(frame.payload)
        telegram_hex = hex_bytes(frame.payload)
        if reply == POWER_ON:
            return StationEvent(
                at=self.now(), name="power.on", detail="track power turned on",
                payload={"telegram": telegram_hex},
            )
        if reply == POWER_OFF:
            return StationEvent(
                at=self.now(), name="power.off", detail="track power turned off",
                payload={"telegram": telegram_hex},
            )
        if isinstance(reply, EmergencyStopBroadcast):
            return StationEvent(
                at=self.now(), name="loco.emergency_stop", detail="emergency stop broadcast",
                payload={"telegram": telegram_hex},
            )
        if isinstance(reply, ServiceModeEntry):
            return StationEvent(
                at=self.now(), name="service.entered",
                detail="another device entered service mode",
                payload={"telegram": telegram_hex},
            )
        return StationEvent(
            at=self.now(), name="reply.unknown", detail=f"undecoded broadcast: {telegram_hex}",
            payload={"telegram": telegram_hex},
        )
```

- [ ] **Step 5: Run the session and capability-learning tests**

```bash
uv run pytest tests/station/test_power_and_status.py -k "learn or record or close or threshold or invalidate"
```

Expected: `PASS`, 11 test functions collected (`test_learn_refuses_a_field_outside_learnable_fields`,
`test_learn_accepts_every_learnable_field`, `test_record_accepts_any_field_including_non_learnable_ones`,
`test_close_flushes_learned_capabilities_when_a_path_is_set`,
`test_close_does_not_write_capabilities_when_nothing_was_learned`,
`test_close_never_writes_capabilities_for_an_unknown_link_identity`,
`test_close_and_power_off_invalidate_registered_caches`,
`test_version_and_status_do_not_invalidate_registered_caches`,
`test_threshold_defaults_to_xpressnet_when_capabilities_unset`,
`test_threshold_uses_capabilities_when_measured`,
`test_power_off_invalidates_caches_even_when_it_ultimately_raises`), 21 passed (all but the
identity test double under `chunk_size`).

- [ ] **Step 6: Run the power, version and status tests**

`-k "power_on or power_off"` would also catch `test_close_and_power_off_invalidate_registered_
caches` from Step 5 (it contains the substring `power_off`), so this step selects by node id
instead of by keyword:

```bash
uv run pytest \
  "tests/station/test_power_and_status.py::test_power_on_reads_the_solicited_reply_in_one_exchange" \
  "tests/station/test_power_and_status.py::test_power_off_reads_the_solicited_reply_in_one_exchange" \
  "tests/station/test_power_and_status.py::test_power_on_disagreement_re_reads_once_then_raises" \
  "tests/station/test_power_and_status.py::test_power_off_disagreement_re_reads_once_then_raises" \
  "tests/station/test_power_and_status.py::test_version_is_cached_but_status_is_never_cached"
```

Expected: `PASS`, 5 test functions × 2 `chunk_size` ids = `10 passed`.

- [ ] **Step 7: Run the emergency-stop and reentrancy tests**

```bash
uv run pytest tests/station/test_power_and_status.py -k "emergency or on_event"
```

Expected: `PASS`, 5 test functions × 2 = 10 passed. This is the run that exercises the lock
reentrancy: `test_status_call_from_inside_on_event_does_not_deadlock` calls `station.status()`
from inside the `on_event` callback while `emergency_stop()` still holds the `RLock`; if a
`threading.Lock` had been used by mistake instead, this step hangs rather than failing cleanly -
if it does, interrupt it, re-check `self._lock = threading.RLock()`, and re-run.

- [ ] **Step 8: Run the exchange() mapping tests**

`-k "exchange"` would also catch `test_power_on_reads_the_solicited_reply_in_one_exchange` and
its `power_off` twin (both names end in `..._in_one_exchange`), so this step selects by node id:

```bash
uv run pytest \
  "tests/station/test_power_and_status.py::test_exchange_returns_generic_ack_for_a_plain_command" \
  "tests/station/test_power_and_status.py::test_exchange_maps_interface_status_09_to_value_error" \
  "tests/station/test_power_and_status.py::test_exchange_maps_any_other_interface_status_to_transport_error" \
  "tests/station/test_power_and_status.py::test_exchange_maps_unsupported_to_unsupported_command_error" \
  "tests/station/test_power_and_status.py::test_exchange_maps_an_unknown_form_to_railctl_error_carrying_the_bytes" \
  "tests/station/test_power_and_status.py::test_exchange_keeps_a_bad_cable_and_an_unknown_reply_form_apart" \
  "tests/station/test_power_and_status.py::test_exchange_returns_transient_replies_unchanged"
```

Expected: `PASS`, 7 test functions -> 6 plain ones × `chunk_size` (2 ids) plus the 4-way
parametrized one × 2 = `6 * 2 + 4 * 2 = 20` passed.

- [ ] **Step 9: Run the events() iterator test**

```bash
uv run pytest tests/station/test_power_and_status.py -k "events_do_not_hold"
```

Expected: `PASS`, 2 passed (`whole-frame` and `byte-at-a-time`).

- [ ] **Step 10: Run the whole file**

```bash
uv run pytest tests/station/
```

Expected: `PASS`, `2 * 27 + 2 * 4 + 1 = 63 passed` (29 test functions: 27 plain ones parametrised
by `chunk_size` alone, one - `test_exchange_returns_transient_replies_unchanged` - parametrised
four ways [`short_circuit`, `track_short_circuit`, `busy`, `station_busy`] on top of `chunk_size`,
and one - the unknown-identity test - not parametrised by `chunk_size` at all, since it builds its
own `Link` and `Station` directly). `envelope_factory` (2.31) does not add a further multiplier
today: `ENVELOPES` has one member, so every count above already accounts for it; the day a second
envelope lands, every one of these formulas doubles again without a single test edit.

- [ ] **Step 11: Export `Station`**

Open `src/railctl/station/__init__.py` (Task 1 already populates it with `Capabilities`,
`LEARNABLE_FIELDS`, `UNKNOWN_IDENTITY`, `Timing`, `TIMING`, `StationEvent` and the `station/
types.py` re-exports). Add the facade import and list it in `__all__` alongside what is already
there - do not replace the file, append to it:

```python
from railctl.station.facade import Station
```

and add `"Station"` to the existing `__all__` list, keeping it alphabetically sorted with the
names Task 1 already added.

```bash
uv run python -c "from railctl.station import Station; print(Station.__name__)"
```

Expected: `Station`

- [ ] **Step 12: Run the full suite and the coverage gate**

```bash
uv run pytest
```

Expected: `PASS`, `963 + 63 = 1026 passed` - `963` is Task 1's own full-suite total
(task-1.md Step 15: the `920` already on `main` plus Task 1's `43`); `63` is this task's own total
from Step 10. Only Tasks 1 and 2 have landed at this point in the plan's execution order, so this
is the sum of exactly those two files' own counts, not a guessed number - if the actual run
disagrees by a small amount, treat it as an arithmetic slip in one of the two files' step counts
and correct it in place; if a *different* test fails, that is a real signal and this step must
stop, not paper over it.

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: `src/railctl/station/facade.py` now appears in the coverage table at 100% or very close
to it - `Required test coverage of 90% reached.` and `0 failed`. `exchange()` no longer special-
cases `StationBusy` or `TransferError` - both fall through to the generic `return reply` at the
bottom, already exercised by every test that gets an ordinary reply back - so there is no
defensive branch left to mark `# pragma: no cover` for either of them. If any line or branch in
`facade.py` is uncovered, it is almost always the `UNKNOWN_IDENTITY` guard in `close()` (covered by
`test_close_never_writes_capabilities_for_an_unknown_link_identity` - if that shows red, the test
is not actually reaching the branch it claims to).

- [ ] **Step 13: Layering and lint**

```bash
uv run pytest tests/test_layering.py
```

Expected: `PASS`, `8 passed` (unchanged from before this task - this run is what proves
`facade.py` and the new tests introduced no framing bytes, no CV arithmetic, and no rogue
exception class).

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both `All checks passed!` and no diff. If `ruff check` flags `F401` on a name such as
`ServiceModeEntry` or `EmergencyStopBroadcast`, look inside `_station_event` before deleting the
import: those two are used only inside an `isinstance` check nested three levels deep in that
method, which is easy to miss on a skim of the import block alone.

- [ ] **Step 14: Commit**

```bash
git add src/railctl/station/facade.py src/railctl/station/__init__.py tests/station/conftest.py tests/station/test_power_and_status.py
git commit -m "feat(station): add the Station facade with session, power and event support"
```

---

### Task 3: Drive, loco_info and functions - including the E4 F8 single-function encoder

**Files:**
- Create: `tests/station/test_drive.py`, `tests/hardware/test_m5_acceptance.py`
- Modify: `src/railctl/station/facade.py` (append `drive`/`loco_info`/`function_state`/`function_set`/`function_toggle`/`forget_loco` to `Station`, plus their private helpers, plus the `EXTENDED_LOCO_INFO_HEADERS` branch in `exchange`)
- Modify: `src/railctl/xbus/commands.py` (add `DB_FUNCTION_SINGLE`, `FUNCTION_ACTION_SHIFT`, `FunctionAction`, `cmd_function_single` - the class and function go immediately after `cmd_function_group`, which ends at line 217 on `main`; the two constants join the existing constant block, immediately after `MAX_FUNCTION = 28` at line 84)
- Modify: `tests/unit/test_xbus_commands.py` (import and exercise the new encoder)
- Modify: `tests/vectors.py` (the measured E4 F8 golden vector)
- Modify: `src/railctl/xbus/replies.py` (add `EXTENDED_LOCO_INFO_HEADERS`, spec line 704: an `E5` or `E2` loco-info form is a feature this station has not been probed for, not an unrecognised reply)
- Modify: `tests/unit/test_xbus_replies.py` (one unit test for `EXTENDED_LOCO_INFO_HEADERS`; **not** `tests/unit/test_replies.py` - that file does not exist on disk, the real one is `test_xbus_replies.py`, matching `test_xbus_commands.py`'s naming)

**Interfaces:**

- Consumes, exactly as merged on disk (M2-M4) or produced by Task 1/Task 2 of this plan:
  - `railctl.xbus.commands.cmd_drive_128(address: int, step: int, direction: Direction, *, threshold: int) -> bytes`, `cmd_loco_info(address: int, *, threshold: int) -> bytes`, `cmd_function_state_13_28(address: int, *, threshold: int) -> bytes`, `cmd_function_group(address: int, group: FunctionGroup, bits: int, *, threshold: int) -> bytes`, `pack_function_bits(group: FunctionGroup, state: Mapping[int, bool]) -> int` (raises `ValueError` naming the missing functions when `state` does not cover the whole group), `FunctionGroup` (`G1=0x20` F0..F4, `G2=0x21` F5..F8, `G3=0x22` F9..F12, `G4=0x23` F13..F20, `G5=0x28` F21..F28), `FUNCTION_BITS: dict[int, tuple[FunctionGroup, int]]` (second element is a bit-shift 0..7, e.g. `FUNCTION_BITS[0] == (FunctionGroup.G1, 4)`), `GROUP_FUNCTIONS: dict[FunctionGroup, tuple[int, ...]]` (each group's members in ascending order), `MAX_FUNCTION = 28`, `OP_LOCO_DRIVE = 0xE4`, `_check_byte(name: str, value: int) -> int`, `encode(header: int, *data: int) -> bytes` (this last one re-exported from `railctl.xbus.codec`, already imported into `commands.py`) - all on disk today, read at `src/railctl/xbus/commands.py`.
  - `railctl.xbus.replies.LocoInfo` - `@dataclass(frozen=True, slots=True)`, fields `raw_ident, raw_speed, speed_steps, in_use_by_other, function_bits` (exactly 13 entries, F0..F12) and `speed=None, direction=None, emergency_stopped=None, address=None` - the last four are `None` unless `speed_steps == 128`, because `speed.py` only defines the 128-step layout. `railctl.xbus.replies.FunctionState13To28(f13_f20: int, f21_f28: int)` - answers `E3 09`, the only reply form carrying F13..F28. `railctl.xbus.replies.GenericAck` and `.Unsupported` - both `@dataclass(frozen=True, slots=True)` with no fields; `Unsupported` is the only reply that entitles anything to record a capability `False`.
  - `railctl.xbus.speed.Direction` (`enum.IntEnum`, `REVERSE=0`, `FORWARD=1`), `MAX_SPEED_STEP = 126`.
  - `railctl.xbus.address.encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]` (raises `ValueError` outside `LOCO_ADDR_MIN..LOCO_ADDR_MAX`), `LOCO_ADDR_MIN = 1`, `LOCO_ADDR_MAX = 9999`.
  - `railctl.xbus.dialect.DIVERGENCE_BAND = range(100, 128)` - the address band where XpressNet and Z21 disagree about the long-address marker.
  - `railctl.xbus.replies.Other(telegram: bytes, reason: Reason = REASON_UNKNOWN_FORM)` and `railctl.envelope.hex_bytes(data: bytes) -> str` - both already imported into `facade.py` by Task 2, for `exchange`'s existing reason-based `Other` dispatch. This task's `EXTENDED_LOCO_INFO_HEADERS` branch reuses both names; it does not import either a second time.
  - `railctl.errors.UnsupportedFeatureError`, `StationError`, `UnsupportedCommandError`, `LinkTimeout` - all subclass `RailctlError(message: str, *, hint: str | None = None)`; `StationError` has no row in `EXIT_CODES` and resolves to the base exit code 9.
  - `railctl.station.capabilities.Capabilities` (Task 1 of this plan, merged before this task runs) - `@dataclass(frozen=True, slots=True)` with, among others, `loco_address_threshold: int | None`, `function_groups_4_5: bool | None`, `single_function_cmd: bool | None`; `Capabilities.unknown(identity: str) -> Capabilities` gives every field `None`; `.with_learned(**updates: object) -> Capabilities` returns a new instance with named fields overridden and raises `ValueError` naming any field that does not exist.
  - Task 2's `Station` collaborator surface, already on `Station` before this task's methods are appended: `self.link`, `self.timing: Timing`, `self.capabilities: Capabilities` (property), `self.threshold: int` (property - `capabilities.loco_address_threshold` when set, otherwise `XPRESSNET.long_address_threshold` = 100), `self.exchange(telegram: bytes, *, timeout: float) -> Reply` (sends via `self.link.request` and parses the bare reply bytes; a `LinkTimeout` from the link propagates uncaught), `self.emit(name: str, payload: dict[str, object]) -> None`, `self.resolve_address(address: int | None) -> int | None` (Task 2's own method, returning `self.default_address if address is None else address` - every CV path Tasks 4-6 add depends on that return value, so this task must not touch it), `self.register_cache(clear: Callable[[], None]) -> None` (appends `clear` to a list `invalidate_caches()` walks), `self.invalidate_caches() -> None` (calls every registered `clear`; `power_off()`, written in Task 2, already calls this), `self.now() -> float`, `self.pause(seconds: float) -> None`.

    `resolve_address` is Task 2's to write and keep, not this task's. `drive`, `loco_info` and the function methods below all take a required `address: int`, never an optional one, so none of them has a default to resolve and none of them calls `resolve_address` at all. What this task DOES add is a differently-named method, `_validate_address(self, address: int) -> None`: it raises `ValueError` outside `LOCO_ADDR_MIN..LOCO_ADDR_MAX` and emits `address.band_unverified` once per operation while `capabilities.loco_address_threshold is None` and the address sits in `DIVERGENCE_BAND`. Before this task, nothing on `Station` needs that validation-plus-warning - Task 2's own methods (`version`, `status`, `power_on/off`, `emergency_stop`) either take no address or, for `emergency_stop`, use `commands.cmd_emergency_stop_loco` directly without the band warning (a safety broadcast is not the moment to worry about the 100..127 ambiguity). This task adds `_validate_address` to `Station` in the same edit that adds `drive`, because `drive` is its first caller.
  - `railctl.xbus.commands` as a module (`from railctl.xbus import commands`) - Task 2 already needs it for `cmd_track_power_on`/`cmd_track_power_off`/`cmd_emergency_stop_all`/`cmd_emergency_stop_loco`/`cmd_station_version`/`cmd_station_status`, called as `commands.cmd_track_power_on()` and so on. This task's new code reaches every encoder it needs the same way (`commands.cmd_drive_128(...)`, `commands.cmd_loco_info(...)`, etc.) through that one existing import - never add a second `from railctl.xbus import commands` line.
  - Task 2's `tests/station/conftest.py` fixtures `bench` and `bench_factory`, and the `Bench` class they return - on disk before this task runs (see ADDENDUM.md §A.1; that file is the source of truth, this bullet only restates it):
    - `bench_factory` is a pytest fixture yielding a callable `make(**kwargs: object) -> Bench`. It builds `Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs)` and calls `.open()` on it before returning it - `chunk_size` and `envelope_factory` come from `tests/conftest.py` and are never passed by a test. Any keyword that is not `capabilities`, `default_address`, `capabilities_path` or `timing` is forwarded as `**capability_overrides` and applied with `Capabilities.with_learned(**capability_overrides)`, which raises `ValueError` naming any field that does not exist - so `bench_factory(single_function_cmd=True)` and `bench_factory(loco_address_threshold=128)` are checked, not swallowed.
    - `bench` is `bench_factory()` with no overrides - every capability `None`, so `Station.threshold` falls back to the XpressNet default, 100. `default_address` defaults to `3` in both.
    - `Bench.station: Station` - the object under test.
    - `Bench.expect(self, request: bytes, reply: bytes | tuple[bytes, ...] = b"", *, broadcast: bytes | tuple[bytes, ...] = ()) -> Bench` - queues exactly one exchange, in bare telegrams: the next telegram `Station.exchange()` sends must equal `request` byte-for-byte (a mismatch fails the test from inside the fixture, naming both telegrams in hex), and the reply is `reply` (or, for a burst, every entry of the tuple, delivered as one solicited frame each). `reply=b""` scripts a station that accepts the request and then says nothing, so the exchange ends in `LinkTimeout` - silence is always scripted, never implied, so a test that expects a timeout still calls `expect()` and still writes `reply=b""` explicitly. Returns `self`, so calls chain. `broadcast=` appends unsolicited frames inside the same exchange; this task's tests never need it.
    - `Bench.sent: list[bytes]` - every telegram `Station.exchange()` has sent since `open()`, BARE and in order, with the `open()` handshake itself excluded. This is what a test checks to prove a refusal sent nothing, or that the single-function path sent exactly one telegram with no read first.
    - `Bench.events: list[tuple[str, dict[str, object]]]` - every `(name, payload)` pair the `Station` under test has called `self.emit(...)` with, in order. `Bench.event_names() -> list[str]` is a METHOD, not an attribute - it returns the same list with only the names, so a call site reads `bench.event_names()`, never `bench.event_names`.

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

```python
# src/railctl/xbus/commands.py
DB_FUNCTION_SINGLE: Final[int] = 0xF8
FUNCTION_ACTION_SHIFT: Final[int] = 6

class FunctionAction(enum.IntEnum):
    OFF = 0b00
    ON = 0b01
    TOGGLE = 0b10

def cmd_function_single(
    address: int, function: int, action: FunctionAction, *, threshold: int
) -> bytes: ...
    # E4 F8 AdrMSB AdrLSB TTNNNNNN X; measured: E4 F8 00 03 40 5F lit the
    # headlight of loco 3 (docs/probe-results.md, D12).
```

```python
# src/railctl/xbus/replies.py
EXTENDED_LOCO_INFO_HEADERS: Final[frozenset[int]] = frozenset({0xE5, 0xE2})
```

```python
# src/railctl/station/facade.py, added to Station
def drive(self, address: int, speed: int, direction: Direction) -> None: ...
def loco_info(self, address: int) -> LocoInfo: ...          # .address filled in by the facade
def function_state(self, address: int, *, refresh: bool = False) -> dict[int, bool]: ...
def function_set(self, address: int, function: int, on: bool,
                 *, force_group: bool = False) -> None: ...
def function_toggle(self, address: int, function: int,
                    *, force_group: bool = False) -> bool: ...
def forget_loco(self, address: int) -> None: ...             # drops the shadow for one address
```

**Layering and measurement notes:**

- `cmd_function_single` lives in `xbus/commands.py`, sharing `OP_LOCO_DRIVE` with `cmd_drive_128` and `cmd_function_group` - the opcode belongs to the layer that owns opcodes, not to `station/`. `station/facade.py` never touches a framing byte, a port name, or CV arithmetic; it only calls encoders and inspects typed replies. `tests/test_layering.py` greps `station/` for exactly that, so a docstring that spells out a byte pattern (`E4 F8`, `TT`, `<< 8`, and so on) belongs in `xbus/commands.py`'s docstrings, never in `facade.py`'s.
- `E4 F8` is a Z21 extension rather than classic XpressNet V2. It is used because this station reports command station id `0x12` **and** because D12 (a later task's doctor probe) measured it, never because the id was assumed - which is why `facade.py` consults `capabilities.single_function_cmd` and never branches on `command_station_id` itself.
- The single-function path touches exactly one function, so there is no shadow and no read-modify-write: a stale shadow can never switch off a function another throttle turned on. The group path (`E4 20/21/22/23/28`) sets every function in its group at once and therefore needs a full, freshly-read picture of the group before it can safely change one bit of it; it can still clobber a function set by another device between the read and the write, and that is inherent to the wire format, not a defect to "fix" with a lock later.
- `pack_function_bits` raises when the state map does not cover the whole group. This task never catches that and substitutes `False` for a missing function - the exception is left to surface, because it is the mechanical half of the same rule `function_set`'s group path enforces at a higher level (refuse an incomplete group unless `force_group=True` says otherwise).
- The station's own event vocabulary is `railctl.station.types.EVENT_NAMES`, a TWELVE-entry tuple Task 1 pins from the start: the seven diagnostic names this and earlier/later tasks emit - `cv.stale_result`, `cv.write_unverified`, `cv.unexercised_band` (Task 5 emits it), `page.unverified`, `loco.in_use_by_other`, `address.band_unverified` and `function.group_seeded` (the last one is THIS task's event, already in the tuple from Task 1 onward) - plus five station-state names the facade and `monitor` also rely on: `power.on`, `power.off`, `loco.emergency_stop`, `service.entered` and `reply.unknown`. `Station.emit(name, payload)` does not validate `name` against that tuple at the call site; the tuple exists so Task 8 can pin that every name in it has a CLI rendering, which is why a name emitted later than the tuple was written would break that test. Nothing in this task's Files list touches `station/types.py`, because Task 1 already put `function.group_seeded` there.
- **Departure 4** (recorded in `CONTRACT.md`): spec line 700 has the group path seed all-zeros and proceed when `loco_info()` fails, treating an unaddressed locomotive as nothing special. This task instead REFUSES the write with `StationError` unless `--force-group` is given - a locomotive whose functions have never been read is a locomotive this station cannot safely blind-write, and the safer behaviour is worth contradicting that one sentence of the spec for. The CLI consequence: `railctl function f14 on` against a never-addressed locomotive exits 9 with a `--force-group` suggestion (Task 11's CLI layer must-pins this).
- The `EXTENDED_LOCO_INFO_HEADERS` branch (spec line 704) belongs in `Station.exchange`, not in `loco_info` or `_expect_ack`: `exchange` is the ONE place in `station/` that calls `replies.parse` (Task 2's docstring on `exchange` says so), so it is the only place that can turn `Other(telegram, reason=REASON_UNKNOWN_FORM)` with an `E5` or `E2` header into `UnsupportedFeatureError` before the reason-based `Other` dispatch (checksum/length/empty/unknown_form) gets a chance to call it a bare `RailctlError` instead. `E5`/`E2` are extended loco-info reply forms `parse` does not have a dedicated dataclass for (spec line 704), so they fall through to `Other` by default - the same way `Other`'s own docstring says an unlisted header should - and this branch is what turns "a reply form we do not know" into "a feature we have decided is out of scope" for these two headers specifically, rather than leaving them as an unresolved capability.
- Nothing in this task writes a CV. If you find yourself reaching for `xbus.cv`, you are in the wrong task.

---

- [ ] **Step 1: Write the failing tests for `cmd_function_single`**

Open `tests/unit/test_xbus_commands.py`. In the import block at the top of the file (the one that also imports `FUNCTION_BITS`, `GROUP_FUNCTIONS`, `MAX_FUNCTION`, `FunctionGroup`, `pack_function_bits`), add `FunctionAction`:

```python
from railctl.xbus.commands import (
    FUNCTION_BITS,
    GROUP_FUNCTIONS,
    MAX_FUNCTION,
    FunctionAction,
    FunctionGroup,
    pack_function_bits,
)
```

In the second import block (the one carrying the `# noqa: E402` comment, which also imports `cmd_function_group` and `cmd_function_state_13_28`), add `cmd_function_single` between them:

```python
from railctl.xbus.commands import (  # noqa: E402
    cmd_drive_128,
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_function_group,
    cmd_function_single,
    cmd_function_state_13_28,
    cmd_loco_info,
    cmd_pom_read_byte,
    cmd_pom_write_bit,
    cmd_pom_write_byte,
    cmd_service_direct_read,
    cmd_service_direct_write,
    cmd_service_ext_read,
    cmd_service_ext_write,
    cmd_service_result_request,
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_off,
    cmd_track_power_on,
    cmd_z21_cv_read,
    cmd_z21_cv_write,
)
```

In `test_encoder_golden_vectors`'s parametrize list, add one row right after the five `cmd_function_group` rows and before the `cmd_loco_info` rows:

```python
        (cmd_function_single(3, 0, FunctionAction.ON, threshold=XN), "E4 F8 00 03 40 5F"),
```

In `test_normal_operation_telegrams_get_the_short_budget`'s parametrize list, add one entry right after the `cmd_function_group(...)` line and before `cmd_loco_info(...)`:

```python
        cmd_function_single(3, 0, FunctionAction.ON, threshold=XN),
```

Finally, append three new test functions right after `test_the_emergency_stop_for_one_loco_is_the_dedicated_92_instruction` and before the `from railctl.xbus.commands import TimeoutClass, timeout_class` import:

```python
def test_function_single_refuses_a_function_index_above_28():
    with pytest.raises(ValueError, match="out of range"):
        cmd_function_single(3, 29, FunctionAction.ON, threshold=XN)


def test_function_single_refuses_an_action_outside_the_enum():
    with pytest.raises(ValueError, match="not a valid FunctionAction"):
        cmd_function_single(3, 0, 3, threshold=XN)  # type: ignore[arg-type]


def test_function_single_toggle_on_f28_packs_the_action_into_the_top_two_bits():
    """TT sits in bits 7-6 and NNNNNN in bits 5-0: TOGGLE (0b10) on F28
    (0b011100) is 0b10011100, not two encodings that happen to collide."""
    telegram = cmd_function_single(3, 28, FunctionAction.TOGGLE, threshold=XN)
    assert telegram[4] == 0b10011100
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/test_xbus_commands.py
```

Expected: FAIL - `ImportError: cannot import name 'FunctionAction' from 'railctl.xbus.commands'` (nothing below has been written yet, so the very first new import breaks collection of the whole file).

- [ ] **Step 3: Implement `FunctionAction` and `cmd_function_single`**

Open `src/railctl/xbus/commands.py`. Add `Final` to the `typing` import - there is currently no `typing` import in this file, so add a new line right after the `collections.abc` import:

```python
from typing import Final
```

In the constant block, immediately after `MAX_FUNCTION = 28` (line 84), add:

```python
DB_FUNCTION_SINGLE: Final[int] = 0xF8
FUNCTION_ACTION_SHIFT: Final[int] = 6
```

Immediately after `cmd_function_group` (the function ending `return encode(OP_LOCO_DRIVE, int(group), high, low, bits)` at line 217) and before `def cmd_loco_info`, add:

```python
class FunctionAction(enum.IntEnum):
    """The TT bits of E4 F8's payload.

    TOGGLE exists on the wire and is never sent by station/facade.py: a
    toggle whose prior state is unknown would return a guess, not a fact,
    and that is the one thing this project's whole error model exists to
    keep out of a return value. See the design decision recorded there.
    """

    OFF = 0b00
    ON = 0b01
    TOGGLE = 0b10


def cmd_function_single(
    address: int, function: int, action: FunctionAction, *, threshold: int
) -> bytes:
    """E4 F8 AdrMSB AdrLSB TTNNNNNN X - one function, one telegram.

    Measured 2026-08-04 (docs/probe-results.md, D12): E4 F8 00 03 40 5F lit
    the headlight of loco 3. This is a Z21 extension, not classic XpressNet
    V2 - the station is probed for it (single_function_cmd) rather than
    assumed to have it just because it reports command station id 0x12.

    `FunctionAction(action)` validates the action: an int that is not 0, 1
    or 2 raises ValueError from the enum constructor itself
    ("3 is not a valid FunctionAction"), so this function does not spell
    the range out by hand a second time.
    """
    if not 0 <= function <= MAX_FUNCTION:
        raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
    action = FunctionAction(action)
    high, low = encode_loco_address(address, long_threshold=threshold)
    payload = _check_byte(
        "function single payload", (int(action) << FUNCTION_ACTION_SHIFT) | function
    )
    return encode(OP_LOCO_DRIVE, DB_FUNCTION_SINGLE, high, low, payload)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/test_xbus_commands.py
```

Expected: PASS, `5 new tests among the file's total; 0 failed` - the file had no failures before this step, and this step adds exactly 5 test nodes (1 golden-vector row, 1 budget-table row, 3 new functions), so compare the printed count against the file's previous total plus 5.

- [ ] **Step 5: Add the measured E4 F8 vector to the golden table**

Open `tests/vectors.py`. In the `railctl.xbus.commands` import, add `cmd_function_single` and `FunctionAction`:

```python
from railctl.xbus.commands import (
    FunctionAction,
    cmd_drive_128,
    cmd_function_single,
    cmd_pom_read_byte,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_z21_cv_read,
)
```

Add one row to `ENCODE_VECTORS`, right after the last `cmd_z21_cv_read` row and before the closing `)` of the tuple:

```python
    EncodeVector(
        "function_single(3, F0, ON)",
        lambda: cmd_function_single(3, 0, FunctionAction.ON, threshold=XPRESSNET_THRESHOLD),
        _b("E4 F8 00 03 40 5F"),
        "measured: lit the headlight of loco 3 (docs/probe-results.md, D12)",
    ),
```

Run the self-consistency suite, which parametrizes over every row in the table without you touching it:

```bash
uv run pytest tests/unit/test_xbus_vectors.py
```

Expected: PASS. The new row adds one node to each of `test_every_vector_carries_a_correct_xor`, `test_every_vector_has_the_length_its_header_declares`, `test_every_vector_says_why_it_exists` and `test_each_encoder_produces_the_bytes_in_the_table` - 4 new nodes in this file, all passing.

- [ ] **Step 6: Lint, format and commit the command-layer change**

```bash
uv run ruff check src/railctl/xbus/commands.py tests/unit/test_xbus_commands.py tests/vectors.py
uv run ruff format --check src/railctl/xbus/commands.py tests/unit/test_xbus_commands.py tests/vectors.py
```

Expected: no output, exit code 0 from both. If format check fails, run `uv run ruff format <the same three paths>` and re-check both commands before committing.

```bash
git add src/railctl/xbus/commands.py tests/unit/test_xbus_commands.py tests/vectors.py
git commit -m "feat(xbus): add the E4 F8 single-function command encoder"
```

- [ ] **Step 7: Write the failing test for `EXTENDED_LOCO_INFO_HEADERS`**

Open `tests/unit/test_xbus_replies.py`. Add `EXTENDED_LOCO_INFO_HEADERS` to the existing `from railctl.xbus.replies import (...)` block, alphabetically first (before `HEADER_61_REPLIES`):

```python
from railctl.xbus.replies import (
    EXTENDED_LOCO_INFO_HEADERS,
    HEADER_61_REPLIES,
    REASON_CHECKSUM,
    REASON_EMPTY,
    REASON_LENGTH,
    REASON_UNKNOWN_FORM,
    TRANSIENT_REPLIES,
    UNSUPPORTED,
    CvValue,
    EmergencyStopBroadcast,
    FunctionState13To28,
    GenericAck,
    InterfaceStatus,
    LocoInfo,
    Other,
    PagedCvValue,
    PowerState,
    StationStatus,
    StationVersion,
    Unsupported,
    parse,
)
```

Append this test right after `test_an_e4_command_echoed_back_is_not_read_as_a_locomotive_info_reply`:

```python
def test_an_e5_or_e2_header_is_named_in_extended_loco_info_headers():
    """E5 and E2 are the two extended loco-info forms spec line 704 calls
    out - this module does not decode them (there is no dataclass for
    either), it only names them so Station.exchange, one layer up, can
    turn the resulting Other into UnsupportedFeatureError instead of a
    bare RailctlError."""
    assert EXTENDED_LOCO_INFO_HEADERS == frozenset({0xE5, 0xE2})
    # Each header's own low nibble sets how many data bytes encode() wants:
    # 0xE5 & 0x0F == 5, 0xE2 & 0x0F == 2. The data bytes' values do not
    # matter, only the header does - so a mismatched byte count here would
    # raise XBusEncodeError before parse() is ever reached.
    for header in EXTENDED_LOCO_INFO_HEADERS:
        data = (0,) * (header & 0x0F)
        reply = parse(encode(header, *data))
        assert isinstance(reply, Other)
        assert reply.telegram[0] == header
```

No new import is needed for this test: `encode` is already imported at the top of the file.

- [ ] **Step 8: Run the test to verify it fails**

```bash
uv run pytest tests/unit/test_xbus_replies.py
```

Expected: FAIL - `ImportError: cannot import name 'EXTENDED_LOCO_INFO_HEADERS' from 'railctl.xbus.replies'` (the new import breaks collection of the whole file, same shape as Step 2's failure for `FunctionAction`).

- [ ] **Step 9: Implement `EXTENDED_LOCO_INFO_HEADERS`, run, lint, format and commit**

Open `src/railctl/xbus/replies.py`. Add `Final` to the existing `typing` import: `from typing import Final, Literal`. In the constant block, immediately after the four `REASON_*` constants, add:

```python
# Spec line 704: E5 and E2 are extended loco-info reply forms this module
# does not decode - there is no dataclass for either, so they fall through
# to Other(telegram, reason=REASON_UNKNOWN_FORM) like any other unlisted
# header. Naming them here lets Station.exchange (station/facade.py) treat
# that specific Other as "a feature we have not probed for" rather than
# "a reply form nobody has ever seen" - the two have different remedies,
# and collapsing them back into one throws that distinction away.
EXTENDED_LOCO_INFO_HEADERS: Final[frozenset[int]] = frozenset({0xE5, 0xE2})
```

```bash
uv run pytest tests/unit/test_xbus_replies.py
```

Expected: PASS. This adds one test node to the file; compare the printed count against the file's previous total plus 1.

```bash
uv run ruff check src/railctl/xbus/replies.py tests/unit/test_xbus_replies.py
uv run ruff format --check src/railctl/xbus/replies.py tests/unit/test_xbus_replies.py
```

Expected: no output, exit code 0 from both.

```bash
git add src/railctl/xbus/replies.py tests/unit/test_xbus_replies.py
git commit -m "feat(xbus): name the E5/E2 extended loco-info headers"
```

- [ ] **Step 10: Write the failing station-facade tests**

Create `tests/station/test_drive.py`:

```python
# tests/station/test_drive.py
"""Station.drive, .loco_info and the function family, including the E4 F8
single-function path.

Every telegram here is a literal byte string, computed by hand from the
wire layouts in xbus/commands.py and xbus/replies.py, not by calling the
encoders under test a second time: a station-level test that built its own
expectations by calling cmd_drive_128 would go green even if cmd_drive_128
and Station.drive agreed on a WRONG byte, because both sides would be
wrong the same way. tests/unit/test_xbus_commands.py is where the encoders
themselves are pinned against the design document; this file is where the
FACADE's choices - which encoder, which capability gate, which telegram
gets sent at all - are pinned.
"""

from __future__ import annotations

import pytest

from railctl.errors import StationError, UnsupportedCommandError, UnsupportedFeatureError
from railctl.xbus.speed import Direction

ACK = b"\x01\x04\x05"
UNSUPPORTED = b"\x61\x82\xe3"
STATUS_REPLY = b"\x62\x22\x07\x47"  # a real reply, but not the one any method here expects
# E5 00 00 00 00 00 <xor>: a well-formed, correctly-checksummed telegram
# under a header nobody has listed - spec line 704's "extended loco info"
# form. XOR of E5 with five zero data bytes is E5 itself, so the xor byte
# is E5 too. parse() has no branch for 0xE5, so this becomes
# Other(telegram, reason=REASON_UNKNOWN_FORM) - exactly the shape the
# EXTENDED_LOCO_INFO_HEADERS branch in exchange() exists to catch before
# the reason-based dispatch calls it a bare RailctlError.
EXTENDED_LOCO_INFO_REPLY_E5 = b"\xe5\x00\x00\x00\x00\x00\xe5"

# drive(3, 30, FORWARD) at the default (XpressNet) threshold: wire step is
# 30 + 1 = 31 = 0x1F, with the direction bit (0x80) set for FORWARD.
DRIVE_30_FWD_REQUEST = b"\xe4\x13\x00\x03\x9f\x6b"

LOCO_INFO_REQUEST = b"\xe3\x00\x00\x03\xe0"
# 128-step mode (ident low 3 bits = 0b100), not busy, stopped, forward, no
# functions on: ident=0x04, raw_speed=0x80 (STOP_WIRE with the direction bit).
LOCO_INFO_REPLY_IDLE = b"\xe4\x04\x80\x00\x00\x60"
# Same, but bit 3 of ident (0x08) set: another device holds this locomotive.
LOCO_INFO_REPLY_BUSY = b"\xe4\x0c\x80\x00\x00\x68"
# ident low 3 bits = 0b000: 14-step mode. raw_speed is deliberately NOT a
# valid 128-step byte's worth of anything meaningful - it must never be
# decoded, only preserved.
LOCO_INFO_REPLY_14_STEP = b"\xe4\x00\x05\x00\x00\xe1"

FUNCTION_STATE_REQUEST = b"\xe3\x09\x00\x03\xe9"
# E3 52 D1 D2: D1 bit 0 is F13, so F13 on and F14..F28 off.
FUNCTION_STATE_REPLY_F13_ON = b"\xe3\x52\x01\x00\xb0"

# E4 F8 00 03 <payload> <xor>: F0 is bit 4 of the G1 byte, so its single-
# function index is 0; ON is TT=01.
FUNCTION_SINGLE_F0_ON = b"\xe4\xf8\x00\x03\x40\x5f"
# F14's index is 14; ON is TT=01: payload = (1 << 6) | 14 = 0x4E.
FUNCTION_SINGLE_F14_ON = b"\xe4\xf8\x00\x03\x4e\x51"

# E4 20 AH AL BITS X: G1 bits, F2 on (bit 1) and F0/F1/F3/F4 off.
FUNCTION_GROUP_G1_F2_ON = b"\xe4\x20\x00\x03\x02\xc5"
# G1 bits, F0 on (bit 4) and F1..F4 off - the group write a fresh loco_info()
# (all functions off) plus an unreadable F13..F28 read still allows, because
# G1 is always fully known.
FUNCTION_GROUP_G1_F0_ON = b"\xe4\x20\x00\x03\x10\xd7"
# E4 23 AH AL BITS X: G4 bits, F14 on (bit 1) and F13/F15..F20 seeded False.
FUNCTION_GROUP_G4_F14_ON_SEEDED = b"\xe4\x23\x00\x03\x02\xc6"

# drive(address, 0, FORWARD) - wire speed byte is always 0x80 (STOP_WIRE
# with the direction bit) regardless of address - at addresses that straddle
# DIVERGENCE_BAND = range(100, 128), default threshold 100.
DRIVE_STOP_FWD_BY_ADDRESS = {
    99: b"\xe4\x13\x00\x63\x80\x14",
    100: b"\xe4\x13\xc0\x64\x80\xd3",
    127: b"\xe4\x13\xc0\x7f\x80\xc8",
    128: b"\xe4\x13\xc0\x80\x80\x37",
}

# loco_info(100) with capabilities.loco_address_threshold overridden to 128:
# 100 < 128, so the address goes out SHORT even though it sits inside
# DIVERGENCE_BAND.
LOCO_INFO_REQUEST_ADDR_100_THR_128 = b"\xe3\x00\x00\x64\x87"


# -- drive() ----------------------------------------------------------------


def test_drive_sends_the_128_step_telegram_and_expects_the_ack(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, ACK)
    bench.station.drive(3, 30, Direction.FORWARD)
    assert bench.sent == [DRIVE_30_FWD_REQUEST]


@pytest.mark.parametrize("speed", [-1, 127])
def test_drive_rejects_out_of_range_speed_before_sending_anything(bench, speed):
    with pytest.raises(ValueError, match="speed"):
        bench.station.drive(3, speed, Direction.FORWARD)
    assert bench.sent == []


@pytest.mark.parametrize("address", [0, 10000])
def test_drive_rejects_out_of_range_address_before_sending_anything(bench, address):
    with pytest.raises(ValueError, match="loco address"):
        bench.station.drive(address, 30, Direction.FORWARD)
    assert bench.sent == []


def test_drive_treats_a_refusal_as_unsupported_command(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        bench.station.drive(3, 30, Direction.FORWARD)


def test_drive_raises_station_error_on_an_unrecognized_reply(bench):
    bench.expect(DRIVE_30_FWD_REQUEST, STATUS_REPLY)
    with pytest.raises(StationError):
        bench.station.drive(3, 30, Direction.FORWARD)


# -- loco_info() --------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "busy"),
    [(LOCO_INFO_REPLY_IDLE, False), (LOCO_INFO_REPLY_BUSY, True)],
)
def test_loco_info_fills_the_address_and_flags_in_use_by_other(bench, reply, busy):
    bench.expect(LOCO_INFO_REQUEST, reply)
    info = bench.station.loco_info(3)
    assert info.address == 3
    assert info.in_use_by_other is busy
    if busy:
        assert bench.event_names() == ["loco.in_use_by_other"]
        assert bench.events[0][1] == {"address": 3}
    else:
        assert bench.event_names() == []


def test_loco_info_in_14_step_mode_leaves_speed_none_and_keeps_raw_speed(bench):
    """The facade must not decode a non-128-step reply with the 128-step
    layout - it only ever passes through what replies.py already decided."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_14_STEP)
    info = bench.station.loco_info(3)
    assert info.speed_steps == 14
    assert info.speed is None
    assert info.direction is None
    assert info.emergency_stopped is None
    assert info.raw_speed == 0x05


def test_loco_info_raises_station_error_on_an_unrecognized_reply(bench):
    bench.expect(LOCO_INFO_REQUEST, STATUS_REPLY)
    with pytest.raises(StationError):
        bench.station.loco_info(3)


def test_loco_info_raises_unsupported_feature_for_an_extended_e5_reply(bench):
    """E5/E2 are extended loco-info forms this station has not been probed
    for (spec line 704) - a feature this project has decided is out of
    scope, not an unrecognised reply. exchange() must raise
    UnsupportedFeatureError here, distinct from the bare StationError the
    test above gets for a form that really is unknown."""
    bench.expect(LOCO_INFO_REQUEST, EXTENDED_LOCO_INFO_REPLY_E5)
    with pytest.raises(UnsupportedFeatureError):
        bench.station.loco_info(3)


# -- address resolution -------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "band"),
    [(99, False), (100, True), (127, True), (128, False)],
)
def test_address_in_the_divergence_band_emits_once_and_the_edges_emit_nothing(
    bench, address, band
):
    bench.expect(DRIVE_STOP_FWD_BY_ADDRESS[address], ACK)
    bench.station.drive(address, 0, Direction.FORWARD)
    if band:
        assert bench.events == [("address.band_unverified", {"address": address, "threshold": 100})]
    else:
        assert bench.events == []


def test_effective_threshold_comes_from_capabilities_not_the_default(bench_factory):
    """capabilities.loco_address_threshold overrides the XpressNet default:
    with it set to 128, address 100 - inside DIVERGENCE_BAND - goes out
    SHORT, and no band-unverified event fires, because the ambiguity this
    event exists to flag has been resolved."""
    fixture = bench_factory(loco_address_threshold=128)
    fixture.expect(LOCO_INFO_REQUEST_ADDR_100_THR_128, LOCO_INFO_REPLY_IDLE)
    fixture.station.loco_info(100)
    assert fixture.event_names() == []


# -- function_state() ----------------------------------------------------------


def test_function_state_leaves_f13_28_absent_when_the_request_is_unsupported(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    state = bench.station.function_state(3)
    assert len(state) == 13
    assert 13 not in state
    assert all(value is False for value in state.values())


def test_function_state_leaves_f13_28_absent_when_the_request_times_out(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, reply=b"")  # no reply queued -> LinkTimeout
    state = bench.station.function_state(3)
    assert len(state) == 13
    assert 13 not in state


def test_function_state_reads_f13_28_when_the_request_succeeds(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    state = bench.station.function_state(3)
    expected = dict.fromkeys(range(29), False)
    expected[13] = True
    assert state == expected


# -- function_set(): tri-state dispatch ----------------------------------------


def test_function_set_prefers_single_function_when_capability_is_true(bench_factory):
    """The preferred path is chosen ONLY on single_function_cmd is True, and
    it sends exactly one telegram - no loco_info, no E3 09, no shadow."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    fixture.station.function_set(3, 0, True)
    assert fixture.sent == [FUNCTION_SINGLE_F0_ON]


def test_function_set_uses_the_group_path_when_capability_is_false(bench_factory):
    fixture = bench_factory(single_function_cmd=False)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G1_F0_ON, ACK)
    fixture.station.function_set(3, 0, True)
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_GROUP_G1_F0_ON]


def test_function_set_uses_the_group_path_when_capability_is_none(bench):
    """None is the default XpressNet cannot promise the single-function
    command exists, so the fall-through from True/False alone would hide a
    bug here - this is the third, separate case the design calls out."""
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    bench.expect(FUNCTION_GROUP_G1_F0_ON, ACK)
    bench.station.function_set(3, 0, True)
    assert bench.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_GROUP_G1_F0_ON]


# -- function_set(): the group path never guesses ------------------------------


def test_function_set_on_a_fully_known_group_needs_no_force_group(bench_factory):
    """F2 lives in G1, and G1 is always fully known from loco_info() alone -
    an unreadable F13..F28 must not block a write to a group that never
    depended on that read."""
    fixture = bench_factory(single_function_cmd=False)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G1_F2_ON, ACK)
    fixture.station.function_set(3, 2, True)


def test_function_set_on_an_unknown_group_member_refuses_and_sends_no_group_write(
    bench_factory,
):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    with pytest.raises(StationError) as caught:
        fixture.station.function_set(3, 14, True)
    assert caught.value.hint == "--force-group"
    # The read happened - it has to, to discover the group is incomplete -
    # but the write (E4 23) must never be among these two.
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST]


def test_function_set_with_force_group_seeds_false_and_emits_an_event(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_GROUP_G4_F14_ON_SEEDED, ACK)
    fixture.station.function_set(3, 14, True, force_group=True)
    assert fixture.sent == [
        LOCO_INFO_REQUEST,
        FUNCTION_STATE_REQUEST,
        FUNCTION_GROUP_G4_F14_ON_SEEDED,
    ]
    assert fixture.event_names() == ["function.group_seeded"]
    name, payload = fixture.events[0]
    assert payload["address"] == 3
    assert payload["group"] == "G4"
    assert set(payload["functions"]) == {13, 15, 16, 17, 18, 19, 20}


# -- function_toggle() ---------------------------------------------------------


def test_function_toggle_reads_first_and_sends_an_explicit_action(bench_factory):
    """F0 is off in LOCO_INFO_REPLY_IDLE, so the toggle must send ON - never
    the TOGGLE wire value - and return True, a fact read back from the
    telegram it built, not a guess."""
    fixture = bench_factory(single_function_cmd=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    fixture.expect(FUNCTION_SINGLE_F0_ON, ACK)
    assert fixture.station.function_toggle(3, 0) is True
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST, FUNCTION_SINGLE_F0_ON]


def test_function_toggle_raises_when_state_is_unknown_and_sends_nothing(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=True)
    fixture.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    fixture.expect(FUNCTION_STATE_REQUEST, UNSUPPORTED)
    with pytest.raises(StationError) as caught:
        fixture.station.function_toggle(3, 14)
    assert caught.value.hint == "--force-group"
    assert fixture.sent == [LOCO_INFO_REQUEST, FUNCTION_STATE_REQUEST]


# -- F13..F28 capability gating, one path each ---------------------------------


def test_f13_28_on_the_group_path_needs_function_groups_4_5(bench_factory):
    fixture = bench_factory(single_function_cmd=False, function_groups_4_5=False)
    with pytest.raises(UnsupportedFeatureError):
        fixture.station.function_set(3, 14, True)
    assert fixture.sent == []


def test_f13_28_on_the_single_function_path_only_needs_single_function_cmd(bench_factory):
    """function_groups_4_5 is False here on purpose: the single-function
    command reaches F13..F28 by a completely different wire form and does
    not depend on the group capability at all."""
    fixture = bench_factory(single_function_cmd=True, function_groups_4_5=False)
    fixture.expect(FUNCTION_SINGLE_F14_ON, ACK)
    fixture.station.function_set(3, 14, True)
    assert fixture.sent == [FUNCTION_SINGLE_F14_ON]


# -- shadow invalidation --------------------------------------------------------


def test_forget_loco_drops_the_shadow(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    # Cached: a second refresh=False call sends nothing more.
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    bench.station.forget_loco(3)
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 4


def test_invalidate_caches_drops_the_shadow(bench):
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 2

    bench.station.invalidate_caches()
    bench.expect(LOCO_INFO_REQUEST, LOCO_INFO_REPLY_IDLE)
    bench.expect(FUNCTION_STATE_REQUEST, FUNCTION_STATE_REPLY_F13_ON)
    bench.station.function_state(3)
    assert len(bench.sent) == 4
```

- [ ] **Step 11: Run the tests to verify they fail**

```bash
uv run pytest tests/station/test_drive.py
```

Expected: FAIL - the first test raises `AttributeError: 'Station' object has no attribute 'drive'`. Every other test in the file fails too, on the same missing-method shape (`drive`, `loco_info`, `function_state`, `function_set`, `function_toggle` or `forget_loco`, depending on the test) - including `test_loco_info_raises_unsupported_feature_for_an_extended_e5_reply`, which fails the same way as every other `loco_info` test here (`AttributeError: 'Station' object has no attribute 'loco_info'`), because `loco_info` and the `exchange` branch it depends on do not exist until Step 12. None of it is a fixture problem, because `bench`/`bench_factory` were already exercised by Task 2's own tests.

- [ ] **Step 12: Implement `_validate_address`, `drive`, `loco_info` and the `EXTENDED_LOCO_INFO_HEADERS` branch of `exchange`**

Open `src/railctl/station/facade.py`. This task's finished imports, across all three implementation steps (this one, Step 14 and Step 16), are:

```python
import dataclasses

from railctl.errors import (
    LinkTimeout,
    StationError,
    UnsupportedFeatureError,
)
from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN
from railctl.xbus.commands import (
    FUNCTION_BITS,
    GROUP_FUNCTIONS,
    MAX_FUNCTION,
    FunctionAction,
    FunctionGroup,
    pack_function_bits,
)
from railctl.xbus.replies import EXTENDED_LOCO_INFO_HEADERS, FunctionState13To28, GenericAck, LocoInfo
from railctl.xbus.speed import MAX_SPEED_STEP, Direction
```

Two names in that list are already available and must NOT be imported a second time: `UnsupportedCommandError` (Task 2's own `from railctl.errors import (...)` line already carries it, for `exchange`'s `61 82` handling) and `DIVERGENCE_BAND` (Task 2's `from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET` already carries it). This task USES both names in the code below without importing either again - a second import line for the same name is exactly what `ruff check`'s redefinition rule catches, the same way the duplicate `commands/__init__.py` mistake elsewhere in this plan got caught.

Add only what this step needs right now - `dataclasses`, `StationError`, `LOCO_ADDR_MAX`, `LOCO_ADDR_MIN`, `GenericAck`, `LocoInfo`, `MAX_SPEED_STEP` - next to whatever Task 2 already imports at the top of the file. `railctl.xbus.commands` as a module and `Capabilities`/`Timing` are already there from Task 2; check whether `railctl.xbus.speed.Direction` is too (Task 2's `emergency_stop` does not need it, so it may not be) - add it here if it is missing, alongside `MAX_SPEED_STEP`, in one `from railctl.xbus.speed import ...` line rather than two. `EXTENDED_LOCO_INFO_HEADERS` is new from this task's own Step 9 addition to `xbus/replies.py`, so it needs a fresh `from railctl.xbus.replies import ...` line here - there is nothing from Task 2 to extend for that one. Steps 14 and 16 below each extend these same import lines with a few more names, ending at the full block shown above - `ruff check` at Step 19 is what catches a name added twice or a line left unsorted, so do not worry about getting the running total exactly right at every intermediate step, only the final one.

First, extend `exchange`'s `Other` handling (Task 2 wrote the method; this is the one place in it this task touches). Find the `if isinstance(reply, Other):` block Task 2's own fixer wrote there (it dispatches on `reply.reason` to choose between `ProtocolError` and `RailctlError`) and add this branch immediately BEFORE it - an `E5` or `E2` loco-info form parses as `Other(telegram, reason=REASON_UNKNOWN_FORM)` by default, and that reason-based dispatch would otherwise call it a bare `RailctlError` instead of the `UnsupportedFeatureError` spec line 704 requires:

```python
            if isinstance(reply, Other) and reply.telegram[0] in EXTENDED_LOCO_INFO_HEADERS:
                raise UnsupportedFeatureError(
                    f"{hex_bytes(telegram)} answered with an extended loco-info form "
                    f"({reply.telegram[0]:02X}) this station has not been probed for"
                )
```

`Other` and `hex_bytes` are already imported by Task 2 - nothing new to add for this branch beyond `EXTENDED_LOCO_INFO_HEADERS` itself and `UnsupportedFeatureError`, both already in the block above.

Second, append these methods to the `Station` class, after everything Task 2 wrote:

```python
    def _validate_address(self, address: int) -> None:
        """Validate `address` and, once per call, warn about the one band
        where XpressNet and Z21 disagree.

        This is NOT `resolve_address` - Task 2 already owns that name, and
        it does something unrelated (substitute `default_address` for a
        `None`). Every address this task's methods take is required, so
        there is never a `None` to resolve here, only a value to check.

        DIVERGENCE_BAND is a FIXED range - the two dialects always disagree
        there - but the warning only fires while the threshold is still
        unmeasured (`capabilities.loco_address_threshold is None`). Once a
        doctor run has established it, the ambiguity this event exists to
        flag is resolved, and repeating the warning would just be noise.
        """
        if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
            raise ValueError(
                f"loco address {address} out of range {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}"
            )
        if self.capabilities.loco_address_threshold is None and address in DIVERGENCE_BAND:
            self.emit("address.band_unverified", {"address": address, "threshold": self.threshold})

    def drive(self, address: int, speed: int, direction: Direction) -> None:
        """speed 0..126 (0 is a braked stop); the drive telegram gets no
        answer of its own, so the expected reply is the generic ack."""
        if not 0 <= speed <= MAX_SPEED_STEP:
            raise ValueError(f"speed {speed} out of range 0..{MAX_SPEED_STEP}")
        self._validate_address(address)
        telegram = commands.cmd_drive_128(address, speed, direction, threshold=self.threshold)
        reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
        self._expect_ack(reply)

    def loco_info(self, address: int) -> LocoInfo:
        """Never raises for `in_use_by_other` - another device holding this
        locomotive blocks nothing here, it only gets reported."""
        self._validate_address(address)
        telegram = commands.cmd_loco_info(address, threshold=self.threshold)
        reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
        if not isinstance(reply, LocoInfo):
            raise StationError(f"unexpected reply to loco info: {reply!r}")
        info = dataclasses.replace(reply, address=address)
        if info.in_use_by_other:
            self.emit("loco.in_use_by_other", {"address": address})
        return info

    def _expect_ack(self, reply: object) -> None:
        """The `Unsupported` (61 82) case is NOT handled here on purpose:
        `exchange` already turned it into `UnsupportedCommandError` before
        returning (Task 2's reply mapping), so by the time a reply reaches
        this method it can never BE `Unsupported` - an `isinstance` branch
        for it here is dead code that would show up as an uncovered branch
        at the coverage gate. `test_drive_treats_a_refusal_as_unsupported_command`
        still pins the refusal end to end; it just does so through
        `exchange`, one layer below this method, which is where the refusal
        is actually detected.
        """
        if isinstance(reply, GenericAck):
            return
        raise StationError(f"expected the generic ack, got {reply!r}")
```

- [ ] **Step 13: Run the drive and loco_info tests**

```bash
uv run pytest tests/station/test_drive.py -k "drive or loco_info or address or threshold"
```

Expected: PASS, `2 * 17 = 34 passed`. Every test in this file takes `bench` or `bench_factory`, which - per Task 2's fixture - is parametrised over `chunk_size` (2 ids) as well as `envelope_factory` (1 id today), so every node below counts twice; do not compare against a bare node count without that factor. The 17 comes from: 1 drive-send + 2 speed-range + 2 address-range + 1 refusal + 1 unrecognized-reply (drive) + 2 busy-flag + 1 fourteen-step + 1 unrecognized-reply (loco_info) + 1 extended-E5-reply (loco_info) + 4 divergence-band + 1 effective-threshold = 17 test-function/parametrize-case combinations, none of which calls `function_state`, `function_set` or `function_toggle`. The `-k` expression is broad enough to catch `test_address_in_the_divergence_band_...` and `test_effective_threshold_...`, whose names do not literally contain "drive" or "loco_info" even though they call `drive()`/`loco_info()` under the hood; nothing in the rest of the file matches "address" or "threshold", so the function-family tests stay excluded and still fail at this point, which is expected.

- [ ] **Step 14: Implement `function_state` and the shadow attribute**

Extend two of the import lines Step 12 added - do not add new, separate `from railctl.errors import ...` / `from railctl.xbus.replies import ...` lines, or `ruff check`'s import-sort rule will flag the duplicate block at Step 19:

- `from railctl.errors import ...` gains `LinkTimeout`, alphabetically first: `from railctl.errors import LinkTimeout, StationError`. `UnsupportedCommandError`, which the `except` clause below also needs, is already imported by Task 2 - it is used here, not imported again.
- Add a new `from railctl.xbus.commands import FUNCTION_BITS, GROUP_FUNCTIONS, FunctionGroup` line (this module was previously only reached as `commands.something()`; these three names are used bare below, so they need their own import).
- `from railctl.xbus.replies import ...` gains `FunctionState13To28`, alphabetically first: `from railctl.xbus.replies import EXTENDED_LOCO_INFO_HEADERS, FunctionState13To28, GenericAck, LocoInfo`.

In `Station.__init__`, add these two lines at the very end of the constructor body, after every assignment Task 2 wrote:

```python
        self._function_shadow: dict[int, dict[int, bool]] = {}
        self.register_cache(self._function_shadow.clear)
```

Append `function_state` and `forget_loco` to the `Station` class:

```python
    def function_state(self, address: int, *, refresh: bool = False) -> dict[int, bool]:
        """F0..F12 from loco_info(); F13..F28 from E3 09, best-effort.

        A refused (61 82, which `exchange` has already turned into
        `UnsupportedCommandError` by the time it reaches here) or silent
        (`LinkTimeout`) E3 09 both leave keys 13..28 ABSENT from the result -
        never False. Absence read as a negative fact is the exact failure
        mode this project exists to stop, and it would happen here first:
        the group write path below trusts this dict completely, so a
        wrongly-defaulted False would blind-clear a function nobody ever
        measured. The two exceptions are read the same way here - "we could
        not read F13..F28" - even though one is a real answer and the other
        is silence; what they share is that neither entitles this method to
        invent a value.
        """
        if not refresh and address in self._function_shadow:
            return dict(self._function_shadow[address])
        info = self.loco_info(address)
        state: dict[int, bool] = dict(enumerate(info.function_bits))
        telegram = commands.cmd_function_state_13_28(address, threshold=self.threshold)
        try:
            reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
        except (LinkTimeout, UnsupportedCommandError):
            reply = None
        if isinstance(reply, FunctionState13To28):
            for function in GROUP_FUNCTIONS[FunctionGroup.G4]:
                state[function] = bool(reply.f13_f20 & (1 << FUNCTION_BITS[function][1]))
            for function in GROUP_FUNCTIONS[FunctionGroup.G5]:
                state[function] = bool(reply.f21_f28 & (1 << FUNCTION_BITS[function][1]))
        self._function_shadow[address] = dict(state)
        return dict(state)

    def forget_loco(self, address: int) -> None:
        """Drop one address's function shadow - the next read starts fresh."""
        self._function_shadow.pop(address, None)
```

- [ ] **Step 15: Run the function_state tests**

```bash
uv run pytest tests/station/test_drive.py -k function_state
```

Expected: PASS, `2 * 3 = 6 passed` - 3 test functions, each doubled by the `bench` fixture's `chunk_size` parametrisation, same as Step 13.

- [ ] **Step 16: Implement `function_set`, `function_toggle` and their private helpers**

Extend the last two import lines, reaching the final state shown at the top of Step 12:

- `from railctl.errors import ...` gains `UnsupportedFeatureError` at the end, giving the parenthesized three-name block shown at the top of Step 12 (`ruff format` at Step 19 is the authority on whether it stays multi-line or collapses to one - do not hand-wrap it, run the formatter and keep whatever it produces):

  ```python
  from railctl.errors import (
      LinkTimeout,
      StationError,
      UnsupportedFeatureError,
  )
  ```

  `UnsupportedCommandError` is NOT in this block: Task 2 already imports it in `facade.py`'s own `from railctl.errors import (...)` line for `exchange`'s `61 82` handling, and Step 14's `function_state` uses that same, already-imported name. Adding a second import of it here is exactly the kind of duplicate `ruff check` flags at Step 19.

- The `from railctl.xbus.commands import FUNCTION_BITS, GROUP_FUNCTIONS, FunctionGroup` line from Step 14 gains `MAX_FUNCTION` (after `GROUP_FUNCTIONS`, still uppercase) and `FunctionAction` (before `FunctionGroup`, capitalised names sort `FunctionAction` < `FunctionGroup`) and `pack_function_bits` at the end. This one also stops fitting on one line - it becomes the parenthesized block already shown at the top of Step 12 (`FUNCTION_BITS`, `GROUP_FUNCTIONS`, `MAX_FUNCTION`, `FunctionAction`, `FunctionGroup`, `pack_function_bits`).

Append these methods to `Station`:

```python
    def _require_function_capability(self, function: int, *, single_function_path: bool) -> None:
        group = FUNCTION_BITS[function][0]
        if group not in (FunctionGroup.G4, FunctionGroup.G5):
            return
        if single_function_path:
            # single_function_cmd is already True to have reached this path,
            # and E4 F8 needs nothing more for F13..F28 - unlike the group
            # path, it never touches the other seven functions in the group.
            return
        if self.capabilities.function_groups_4_5 is not True:
            raise UnsupportedFeatureError(
                f"F{function} (group {group.name}) needs function_groups_4_5, "
                "which this station has not confirmed"
            )

    def _function_set_group_path(
        self, address: int, function: int, on: bool, *, force_group: bool
    ) -> None:
        group = FUNCTION_BITS[function][0]
        state = self.function_state(address, refresh=True)
        missing = [f for f in GROUP_FUNCTIONS[group] if f not in state]
        if missing and not force_group:
            raise StationError(
                f"F{function} shares group {group.name} with F{missing}, whose state "
                "has not been read; a blind write would clobber them",
                hint="--force-group",
            )
        if missing:
            for f in missing:
                state[f] = False
            self.emit(
                "function.group_seeded",
                {"address": address, "group": group.name, "functions": tuple(missing)},
            )
        state[function] = on
        bits = pack_function_bits(group, state)
        telegram = commands.cmd_function_group(address, group, bits, threshold=self.threshold)
        reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
        self._expect_ack(reply)
        self._function_shadow[address] = state

    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None:
        if not 0 <= function <= MAX_FUNCTION:
            raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
        single = self.capabilities.single_function_cmd is True
        self._require_function_capability(function, single_function_path=single)
        if single:
            # No shadow, no read-modify-write: this touches exactly one
            # function, so a stale shadow can never switch off a function
            # another throttle turned on.
            action = FunctionAction.ON if on else FunctionAction.OFF
            telegram = commands.cmd_function_single(
                address, function, action, threshold=self.threshold
            )
            reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
            self._expect_ack(reply)
            return
        self._function_set_group_path(address, function, on, force_group=force_group)

    def function_toggle(
        self, address: int, function: int, *, force_group: bool = False
    ) -> bool:
        """Read the current value, send an explicit ON or OFF - never the
        TOGGLE wire action - and return the new value as a fact, not a
        guess. Raises StationError, sending nothing, when the state cannot
        be read at all."""
        if not 0 <= function <= MAX_FUNCTION:
            raise ValueError(f"function {function} out of range 0..{MAX_FUNCTION}")
        single = self.capabilities.single_function_cmd is True
        self._require_function_capability(function, single_function_path=single)
        state = self.function_state(address, refresh=True)
        if function not in state:
            raise StationError(
                f"F{function} state is unknown; a toggle cannot guess it",
                hint="--force-group",
            )
        new_value = not state[function]
        if single:
            action = FunctionAction.ON if new_value else FunctionAction.OFF
            telegram = commands.cmd_function_single(
                address, function, action, threshold=self.threshold
            )
            reply = self.exchange(telegram, timeout=self.timing.li_ack_normal)
            self._expect_ack(reply)
            return new_value
        self._function_set_group_path(address, function, new_value, force_group=force_group)
        return new_value
```

- [ ] **Step 17: Run the whole station-facade test file**

```bash
uv run pytest tests/station/test_drive.py
```

Expected: PASS, `2 * 32 = 64 passed`. This file has 32 raw test-function/parametrize-case combinations (17 from Step 13's drive/loco_info group, 3 from Step 15's `function_state` group, plus 12 more across `function_set`'s tri-state dispatch, the group-path safety tests, `function_toggle`, the F13-28 capability split, and the two shadow-invalidation tests), and every one of them takes `bench` or `bench_factory`, so `Station`'s `chunk_size` parametrisation doubles the whole file, not just this task's own additions.

- [ ] **Step 18: Run the full suite and the coverage gate**

```bash
uv run pytest
```

Expected: PASS, `0 failed`. Do not compare against a bare number - compare this task's own contribution: `5` (`tests/unit/test_xbus_commands.py`) `+ 4` (flow-through nodes in `tests/unit/test_xbus_vectors.py`) `+ 1` (`tests/unit/test_xbus_replies.py`) `+ 64` (`tests/station/test_drive.py`, Step 17's `2 * 32`) `= 74` new test nodes from this task, layered on top of whatever total Task 1 and Task 2 already brought the suite to (`920` was the total before this plan's own Tasks 1 and 2 landed; add their own additions first). `tests/hardware/test_m5_acceptance.py` does not exist until Step 20, so it is not part of this count yet.

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table, now covering the new lines in `src/railctl/xbus/commands.py` and `src/railctl/station/facade.py`, then `Required test coverage of 90% reached.` with `0 failed`. If a branch in `_function_set_group_path`, `_require_function_capability` or `function_state`'s `LinkTimeout` handling shows up uncovered, the fix is a missing test in `tests/station/test_drive.py`, not a smaller gate.

- [ ] **Step 19: Lint and format check**

```bash
uv run ruff check src/railctl/station/facade.py src/railctl/xbus/commands.py tests/station/test_drive.py
uv run ruff format --check src/railctl/station/facade.py src/railctl/xbus/commands.py tests/station/test_drive.py
```

Expected: no output, exit code 0 from both. If `ruff format --check` fails, run `uv run ruff format` over the same three paths and re-run both checks before continuing.

- [ ] **Step 20: Write the M5 hardware acceptance test**

Create `tests/hardware/test_m5_acceptance.py`:

```python
# tests/hardware/test_m5_acceptance.py
"""M5 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly:  uv run pytest -m hardware -s
Deselected by default (pyproject.toml's addopts carries -m 'not hardware').
"""

from __future__ import annotations

import pytest

from railctl.station.facade import Station
from railctl.xbus.speed import Direction

pytestmark = pytest.mark.hardware

ACCEPTANCE_ADDRESS = 3
ACCEPTANCE_SPEED = 30
ACCEPTANCE_PAUSE_SECONDS = 3.0


def test_power_drive_stop_power_off_restores_the_track_power_it_found():
    """The M5 verification sentence (CONTRACT.md): power on, drive 30
    forward, pause, STOP, power off - and leave track power exactly as it
    was found, since this test runs on a shared bench. "Stop" means
    `emergency_stop`, not a braked `drive(0)`: the sentence names the
    emergency-stop path specifically, because that is the one path this
    plan's Task 2 wires straight to `cmd_emergency_stop_loco` without going
    through the band-warning machinery `drive` and `loco_info` share - a
    safety broadcast has to work even when address validation would not.
    """
    station = Station.open()
    try:
        was_on = station.status().track_power
        print(f"\ntrack power before: {'on' if was_on else 'off'}")

        station.power_on()
        station.drive(ACCEPTANCE_ADDRESS, ACCEPTANCE_SPEED, Direction.FORWARD)
        print(f"driving loco {ACCEPTANCE_ADDRESS} at step {ACCEPTANCE_SPEED} forward")

        station.pause(ACCEPTANCE_PAUSE_SECONDS)

        info = station.loco_info(ACCEPTANCE_ADDRESS)
        print(f"loco_info before stop: speed={info.speed} direction={info.direction}")

        station.emergency_stop(ACCEPTANCE_ADDRESS)
        print(f"emergency-stopped loco {ACCEPTANCE_ADDRESS}")

        if not was_on:
            station.power_off()
            print("track power restored to off")
        else:
            print("track power left on, as found")
    finally:
        station.close()
```

- [ ] **Step 21: Verify the hardware test is collected but deselected by default**

```bash
uv run pytest --collect-only tests/hardware/test_m5_acceptance.py
```

Expected: the test node is listed (`tests/hardware/test_m5_acceptance.py::test_power_drive_stop_power_off_restores_the_track_power_it_found`) together with a `1 deselected` (or similar) line - `pyproject.toml`'s `addopts` carries `-m 'not hardware'`, so `pytestmark = pytest.mark.hardware` at module scope is what keeps this test out of every ordinary run. This is the only verification this step gets: the test is never executed against real hardware as part of this plan.

```bash
uv run pytest
```

Expected: the same total as Step 18 - `tests/hardware/test_m5_acceptance.py`'s one test is deselected, not run, so it does not change the passing count.

- [ ] **Step 22: Lint and format the hardware test, then commit the whole station-facade change**

```bash
uv run ruff check tests/hardware/test_m5_acceptance.py
uv run ruff format --check tests/hardware/test_m5_acceptance.py
```

Expected: no output, exit code 0.

```bash
git add src/railctl/station/facade.py tests/station/test_drive.py tests/hardware/test_m5_acceptance.py
git commit -m "feat(station): add drive, loco_info and the function family to Station"
```

---

### Task 4: The shared wait loop, the CV matcher, POM read, and mode resolution

**Files:**
- Create: `src/railctl/station/programming.py`, `tests/station/test_cv_pom.py`
- Modify: `src/railctl/xbus/cv.py` (add `result_ident_for` next to `resolve_service_cv`, around line 349), `tests/unit/test_cv.py` (bands for the new function), `src/railctl/station/facade.py` (wire `self.programmer` and `self.register_cache(...)` into `Station.__init__`), `src/railctl/errors.py` (add an optional `details` keyword to `RailctlError.__init__`, threaded through `ProgrammingError.__init__` alongside `cv` - `pom_read`'s own failure paths need it now, in Step 13, and this task is the first one that does), `tests/unit/test_exit_codes.py` (pin the new keyword's default and round-trip)

**Interfaces:**

- Consumes:
  - Task 2's `Station` collaborator surface, exactly as named to this task: `link` (a `railctl.link.Link`), `timing` (a `railctl.station.timing.Timing`), `capabilities` (property, current `Capabilities`, replaced wholesale on every `learn`), `threshold` (property, `int`, the effective long-address threshold), `exchange(telegram: bytes, *, timeout: float) -> Reply` (sends through `link.request`, parses with `railctl.xbus.replies.parse`, and raises the mapped exception when the parsed reply is an `InterfaceStatus`; every other parsed form - `GenericAck`, `CvValue`, `Unsupported`, `NoAck`, `Ready`, `Busy`, `ShortCircuit`, `TrackShortCircuit`, `StationBusy`, `TransferError`, `PagedCvValue`, `Other` - passes through untouched, which is why this task never has to import `InterfaceStatus` at all), `emit(name: str, payload: dict[str, object]) -> None` (forwards to the `on_event` callback given to `Station.__init__`), `learn(**updates: object) -> None` (replaces `capabilities` with `capabilities.with_learned(**updates)`), `now() -> float` (the injected clock), `pause(seconds: float) -> None` (the injected sleep), `status() -> StationStatus`, `resolve_address(address: int | None) -> int | None` (returns `address` if given, else `default_address`, else `None`), `register_cache(clear: Callable[[], None]) -> None` (keeps `clear` in a list `Station` calls at its own cache-invalidation points - none of which this task adds; it only registers the hook).
  - Task 1's `ProgMode`, `CvResult` (`railctl.station.types`), `Timing` (`railctl.station.timing`, constant `TIMING`), `Capabilities` (`railctl.station.capabilities`) with fields `pom_read: bool | None`, `pom_result_channel: str | None`, `pom_echo_zero_based: bool | None`, `service_direct_cv: bool | None`, classmethod `Capabilities.unknown(identity: str) -> Capabilities`, method `with_learned(**updates: object) -> Capabilities`.
  - Task 2's `bench` / `bench_factory` pytest fixtures, exposed through `tests/station/conftest.py` with the following shape - stated here in full because this task's implementer sees only this file (per ADDENDUM §A.1, the source of truth):
    ```python
    class Bench:
        def __init__(
            self,
            *,
            chunk_size: int | None = None,
            envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
            capabilities: Capabilities | None = None,
            default_address: int | None = BENCH_DEFAULT_ADDRESS,
            capabilities_path: Path | None = None,
            timing: Timing = TIMING,
            **capability_overrides: object,
        ) -> None: ...
        # station, link, transport, clock, events and on_event_hook are plain attributes set here.

        def expect(
            self,
            request: bytes,
            reply: bytes | tuple[bytes, ...] = b"",
            *,
            broadcast: bytes | tuple[bytes, ...] = (),
        ) -> Bench: ...
        # bare X-Bus bytes in and out - Kind.SOLICITED framing happens inside.
        # Repeated calls with the same `request` queue successive answers, in order.
        def push(self, telegram: bytes) -> Bench: ...
        # queues `telegram` as an unsolicited (Kind.UNSOLICITED) frame.

        @property
        def sent(self) -> list[bytes]: ...
        # every telegram written since open(), BARE and in order, handshake excluded.

    def bench_factory(**kwargs: object) -> Bench: ...
    # builds Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs).open();
    # chunk_size/envelope_factory come from tests/conftest.py and are never passed by a test.

    @pytest.fixture
    def bench(bench_factory) -> Bench:
        return bench_factory()
    ```
    `chunk_size` and `envelope_cls` are injected by `bench_factory` and never passed by a test.
    `BENCH_DEFAULT_ADDRESS` is `3`. `bench.station`, `bench.link`, `bench.transport`, `bench.clock`
    and `bench.events` are plain attributes this task reads directly. `bench.transport.written` holds
    FRAMED bytes - `Link.request` writes `envelope.wrap(telegram)` and `FakeTransport` appends exactly
    that - so no test in this task reads it; every assertion on what was sent reads `bench.sent`, which
    is bare telegrams in order, with the `open()` handshake already excluded.
    `bench_factory`'s default `capabilities` is `Capabilities.unknown("bench")`. Every test below that needs a different capability state reaches it through `bench.station.learn(...)` after construction, never through the factory - `Station.learn` only ever touches `LEARNABLE_FIELDS` (`pom_read`, `pom_result_channel`, `pom_echo_zero_based`, `service_direct_cv`), which is enough for every test but two. The first exception is `default_address=None`, needed once, for the "no address anywhere" precondition. The second is `probed_at` and `notes`, neither of which `Station.learn` can set (they are not in `LEARNABLE_FIELDS`) - Step 11's provenance test builds its `Capabilities` directly with `Capabilities.unknown("bench").with_learned(pom_read=False, probed_at=...).with_note(...)` and passes it to `bench_factory(capabilities=...)`, because `with_learned` on the `Capabilities` object itself (unlike `Station.learn`) accepts any real field name.
  - `railctl.link.Link.poll(timeout: float = 0.0) -> list[Frame]`, `drain() -> None` (defined as `poll(0.0)` with the return value discarded - see `link.py`), `request(telegram: bytes, *, timeout: float | None = None) -> bytes`.
  - `railctl.xbus.commands.cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes`, `cmd_service_result_request() -> bytes` (`21 10 31`), `cmd_station_status() -> bytes` (`21 24 05`, used only by this task's tests, to script the track-power precondition).
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` (test-only, to build scripted replies without hand-computing XOR bytes).
  - `railctl.xbus.cv.echo_candidates(encoding: CvEncoding, cv: int, *, zero_based: bool | None = None) -> frozenset[int]`, `decode_echo(encoding, raw, *, page_index=None, zero_based=None) -> int`, `CvEncoding` (re-exported from `railctl.xbus.dialect`).
  - `railctl.xbus.replies.CvValue(raw_cv: int, value: int, ident: int, z21_form: bool)`, `Ready()`, `NoAck()`, `ShortCircuit()`, `TrackShortCircuit()`, `Unsupported()`, `Reply` (the union type), `TRANSIENT_REPLIES: frozenset[Reply]` (`{ShortCircuit(), TrackShortCircuit(), Busy(), StationBusy(), TransferError()}`), `parse(telegram: bytes) -> Reply`.
  - `railctl.errors.PomReadUnsupportedError`, `DecoderNoAckError`, `DecoderNotRespondingError`, `ShortCircuitError` (all `ProgrammingError(message, *, hint=None, cv=None)` subclasses - `cv` is accepted), `TrackPowerError` (a bare `StationError(message, *, hint=None)` - **no `cv` keyword**, unlike its siblings above), `LinkTimeout` (a bare `RailctlError(message, *, hint=None)` - **also no `cv` keyword**). Quoted from `src/railctl/errors.py` as it stands on disk; do not pass `cv=` to `TrackPowerError` or `LinkTimeout`, only to the four `ProgrammingError` subclasses.

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

  `src/railctl/xbus/cv.py`:
  ```python
  def result_ident_for(cv: int, encoding: CvEncoding) -> int: ...
      # the 63 14..17 ident the station uses when answering about `cv`:
      # SERVICE_RESULT_IDENT_BASE + ext_cv_fields(cv)[0]. Measured: CV8 -> 0x14, CV265 -> 0x15.
  ```

  `src/railctl/station/programming.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class TimedOut:
      polls: int; ready_streak: int; saw_no_ack: bool

  WaitOutcome = Reply | TimedOut
  ResultChannelSeen = Literal["broadcast", "poll"]

  class CvMatcher:
      def __init__(self, encoding: CvEncoding, cv: int, *,
                   zero_based: bool | None = None, page_index: int | None = None) -> None: ...
      cv: int
      encoding: CvEncoding
      def __call__(self, reply: Reply) -> bool: ...
      def value_of(self, reply: CvValue) -> int: ...
      def echo_says_zero_based(self, reply: CvValue) -> bool | None: ...

  def resolve_mode(mode: ProgMode, capabilities: Capabilities, *,
                   operation: Literal["read", "write"]) -> ProgMode: ...

  class CvProgrammer:
      def __init__(self, station: "Station") -> None: ...
      def invalidate_pages(self) -> None: ...
      def await_result(self, matcher: CvMatcher, *, timeout: float, first_delay: float,
                       interval: float, exchange_timeout: float, allow_poll: bool,
                       ready_means_done: bool, context: Literal["pom", "service"]
                       ) -> WaitOutcome: ...
      def pom_read(self, cv: int, *, address: int | None = None) -> CvResult: ...
  ```
  One deliberate deviation from the sketch this task was handed: `pom_read`'s `address` is typed `int | None = None`, not the bare `int` a terse summary of this interface might suggest. Design line 703 states "`address=None` in the CV methods means `default_address` ... `mode=POM` with no address anywhere raises `ValueError`", and the must-pin list below requires exactly that `ValueError` from this method. A non-optional `int` parameter could never reach that branch, so the optional form is what actually satisfies both the design text and the tests this task must write.

  `cv` is `pom_read`'s only positional parameter. There is no call form `pom_read(address, cv)` anywhere in this task or any later one - every call site, in this file's own tests and in every task that calls through `station.programmer` from here on, is `programmer.pom_read(PROBE_CV, address=address)`, `cv` first and bare, `address` second and named.

  The `page: CvPage | None = None` keyword is **not** this task's to add. It arrives on `pom_read` (and `service_read`) in Task 6, in the same step that implements `ensure_page` and calls `self.ensure_page(...)` at the top of each - reading an indexed CV through `pom_read` without first selecting its page is exactly the silent-wrong-value failure design line 802 warns about, and Task 6 is where the page cache this task's `invalidate_pages` stub only clears (Step 8, below) actually gets populated. Nothing in this task's `pom_read` signature or body mentions `page`.

  `src/railctl/station/facade.py` gains, at the end of `Station.__init__` (after every other collaborator attribute - `link`, `timing`, `capabilities`, `default_address`, `on_event` wiring - is already assigned):
  ```python
  from railctl.station.programming import CvProgrammer
  ...
  self.programmer = CvProgrammer(self)
  self.register_cache(self.programmer.invalidate_pages)
  ```

**Notes the implementer needs before writing any code:**

- **Two independent checks decide a `CvMatcher` match, and either one alone is wrong.** `echo_candidates` narrows the echo BYTE within a band (CV265 and CV9 share `{9}`), and `result_ident_for` supplies the BAND the ident carries (`63 14` vs `63 15`). A matcher that checks only the byte accepts a `63 14 09` (CV9's value) as the answer to a CV265 request, under CV265's name. Both checks live in `CvMatcher.__call__`; neither is optional.
- **`result_ident_for` takes `encoding` to apply that encoding's own CV bound before the shared band arithmetic**, not because the arithmetic itself differs by encoding - `SERVICE_DIRECT` tops out at CV255, and routing `result_ident_for(300, SERVICE_DIRECT)` through `ext_cv_fields`'s own bound (which reaches 1023) would silently promise a band the direct opcode family can never produce. Every other encoding's own maximum already equals `ext_cv_fields`'s, so only `SERVICE_DIRECT` needs the extra check.
- **The `64 14` (`z21_form=True`) branch of `CvMatcher` is documented, never measured** on this hardware (docs/probe-results.md, R1: no POM reply has ever come back at all). It is kept general rather than rejected, because a real (if unmeasured) answer through that form is still an answer, and dropping it would turn a genuine reply into silence - the exact failure this project exists to catch. Its test says so in its name.
- **`_learn_result_channel` only records `pom_result_channel` when `context == "pom"`.** A `61 82` answer to a service-mode poll is the expected reply from a station that only pushes results, and recording it would misfile an ordinary moment as a durable POM capability.
- **Channel naming:** `"poll"` when the CV value arrived as the direct answer to `21 10 31`; `"broadcast"` for every other arrival - an unsolicited push, or the value arriving inline as the answer to the POM telegram itself.
- **POM's 2.0 s-per-attempt budget is an estimate, not a measurement.** A RailCom answer, if it comes at all, arrives inside one cutout, tens of milliseconds after the packet; 2.0 s times 3 attempts covers loss on that channel without a failing read feeling hung. Keep that sentence next to `TIMING.pom_result` wherever it is quoted - it says the constant is re-measurable, not sacred.
- **No `cv - 1`, `>> 8`, `% 256` in this module.** `result_ident_for`, `echo_candidates` and `decode_echo` exist precisely so `station/programming.py` never needs any of that; `tests/test_layering.py` greps this file for it.
- **`CvProgrammer` reads `station.capabilities` fresh at the top of every call, never caches it in `__init__`.** `learn()` replaces the `Capabilities` object wholesale; a cached reference from construction time would never see a capability this same session just learned. Concretely: `CvProgrammer.__init__` sets exactly one collaborator attribute, `self._station`. There is no `self.station`, no `self.capabilities`, no `self.timing`, no `self.clock`, no `self.sleep`, no `self._ctx`, no `self._capabilities`, no `self._timing`, no `self._threshold` - every one of those names belongs to a different, earlier draft of this class and none of them survives into the file this task actually writes. Every later call reaches the station as `self._station.capabilities`, `self._station.timing`, `self._station.now()`, `self._station.pause(...)`, `self._station.threshold`.

- **This task's own `from typing import TYPE_CHECKING, Literal` line stays exactly that - two names, nothing unused.** Task 4 has no module-level `Final`-typed constant of its own, so adding `Final` here now would sit unused and fail `uv run ruff check .` at this task's own Step 14, before anything downstream needs it. Task 5 is the first task that actually uses `Final` (`SERVICE_ENCODING_ORDER`, `UNEXERCISED_BANDS`, `_REGISTER_COLLISION_MAX`), and that task's own fix widens this exact line to `from typing import TYPE_CHECKING, Final, Literal` when it adds them - leave the widening to the task that needs the name.

- **What tests must pin** (paraphrased from this task's contract, mapped to the step that writes each one): silence never becomes "unsupported" (Steps 11-12); `Unsupported` learns `pom_read=False` (Step 12); `NoAck` produces `DecoderNoAckError` and leaves `pom_read` at `None` (Step 12); a successful read learns `pom_read`, `pom_result_channel`, and `pom_echo_zero_based` in both directions (Steps 3, 12); the passive branch binds and tests its own polled frame (Step 9); polling is conditional and a poll's `61 82` is never durable (Step 9); every inner exchange is clamped to the remaining budget (Step 9); `ready_streak >= service_ready_limit` ends the wait as `NoAck()` unless `ready_means_done` (Step 9); `ShortCircuit` ends the whole operation with no retry (Step 12); `link.drain()`-equivalent clearing before every attempt, with `cv.stale_result` emitted (Step 12); `CvMatcher` rejects a right-byte/wrong-band reply (Step 3); the two-candidate set while `pom_echo_zero_based` is `None` narrows to one once learned (Step 3); preconditions are checked before any telegram is sent (Step 11); `resolve_mode`'s three-way AUTO resolution, all nine crossings of `pom_read` x `service_direct_cv` (Step 5).

---

- [ ] **Step 1: Write the failing `result_ident_for` band tests**

Open `tests/unit/test_cv.py`. Add `result_ident_for` to the import block from `railctl.xbus.cv` (alphabetically, between `resolve_service_cv` and `z21_cv_fields`), and append these two tests at the end of the file:

```python
@pytest.mark.parametrize(
    ("cv", "encoding", "expected"),
    [
        (8, CvEncoding.POM_ZERO_BASED, 0x14),
        (265, CvEncoding.POM_ZERO_BASED, 0x15),
        (511, CvEncoding.POM_ZERO_BASED, 0x15),
        (512, CvEncoding.POM_ZERO_BASED, 0x16),
        (1023, CvEncoding.POM_ZERO_BASED, 0x17),
        (1024, CvEncoding.POM_ZERO_BASED, 0x14),
        (8, CvEncoding.SERVICE_DIRECT, 0x14),
        (255, CvEncoding.SERVICE_DIRECT, 0x14),
        (8, CvEncoding.Z21_16BIT, 0x14),
        (265, CvEncoding.SERVICE_EXT, 0x15),
    ],
)
def test_result_ident_for_matches_the_measured_bands(
    cv: int, encoding: CvEncoding, expected: int
):
    """`63 14 08` answered CV8; `63 15 09` answered CV265 (docs/probe-results.md).

    The band an ident carries is a property of the CV's own page, not of which
    opcode asked for it - CV8 comes back on `63 14` whether it was requested
    through POM, the legacy direct opcode, or the extended opcodes.
    """
    assert result_ident_for(cv, encoding) == expected


def test_result_ident_for_refuses_a_cv_the_direct_opcode_cannot_reach():
    """SERVICE_DIRECT tops out at CV255. `ext_cv_fields` alone reaches 1023 and
    would silently promise a band the direct opcode family can never produce,
    so this function checks the caller's own bound first."""
    with pytest.raises(CvOutOfRangeError, match="direct") as excinfo:
        result_ident_for(300, CvEncoding.SERVICE_DIRECT)
    assert excinfo.value.cv == 300
```

Run:

```bash
uv run pytest tests/unit/test_cv.py
```

Expected: `FAIL - ImportError: cannot import name 'result_ident_for' from 'railctl.xbus.cv'`. This is the named reason - the function does not exist yet.

- [ ] **Step 2: Implement `result_ident_for` and commit**

Open `src/railctl/xbus/cv.py`. Add `"result_ident_for"` to `__all__`, between `"resolve_service_cv"` and `"z21_cv_fields"`. Insert the function immediately before `def resolve_service_cv(...)` (currently line 349):

```python
def result_ident_for(cv: int, encoding: CvEncoding) -> int:
    """The `63 14..17` ident the station uses when it answers about `cv`.

    `SERVICE_RESULT_IDENT_BASE + ext_cv_fields(cv)[0]`. Measured: CV8 -> 0x14,
    CV265 -> 0x15 (docs/probe-results.md). The band a CV answers on is a
    property of the CV's own page, not of which opcode requested it - the
    station answers CV8 on `63 14` whether it was asked for through POM, the
    legacy direct opcode, or the extended opcodes - so `encoding` plays no part
    in the arithmetic. It IS used to apply the bound the caller's own encoding
    actually supports before doing that arithmetic: `SERVICE_DIRECT` tops out
    at CV255, and routing a CV300 request through here without that check would
    accept it via `ext_cv_fields`'s wider bound (which reaches 1023), silently
    promising a band the direct opcode family can never produce.

    Exists so that `station/` never has to add `SERVICE_RESULT_IDENT_BASE` to a
    band index itself - the layering test forbids CV arithmetic there, and this
    function is the whole reason `CvMatcher` can check the ident band without
    it.
    """
    if encoding is CvEncoding.SERVICE_DIRECT:
        direct_cv_byte(cv)  # validates 1..255 with the CV256 message; result unused
    return SERVICE_RESULT_IDENT_BASE + ext_cv_fields(cv)[0]
```

Run:

```bash
uv run pytest tests/unit/test_cv.py
```

Expected: `10 passed` for the new parametrized cases plus one for the range refusal, all previously-passing tests in the file still green, `0 failed`.

```bash
uv run pytest
```

Expected: `N passed, 0 failed`, where `N` is `920` (the M2-M4 baseline this whole plan starts from) `+ (Task 1's own reported running total) + (Task 2's) + (Task 3's) + 11` (the 10 parametrized cases plus the one range-refusal test added just above). Nobody has measured what Tasks 1-3 add from this file's vantage point, so this step does not hardcode that figure - compare the real total against Task 3's own final step, which is the one place that number is actually reported, plus these 11.

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both silent, exit 0.

```bash
git add src/railctl/xbus/cv.py tests/unit/test_cv.py
git commit -m "feat(xbus): add result_ident_for for the shared service-result band"
```

- [ ] **Step 3: Write the failing `CvMatcher` tests**

Create `tests/station/test_cv_pom.py`:

```python
"""The shared CV wait loop, the CV matcher, POM read, and AUTO mode resolution.

`Task 12` in the M2-M4 core plan is the model for these fixtures' shape:
`bench` and `bench_factory` (`tests/station/conftest.py`, Task 2) wrap a real
`Station` over a `FakeTransport`-backed `Link`, already past the version
handshake, so every test below scripts bare X-Bus telegrams and never touches
framing itself.
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    PomReadUnsupportedError,
    ShortCircuitError,
    TrackPowerError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import CvMatcher, CvProgrammer, TimedOut, resolve_mode
from railctl.station.timing import TIMING
from railctl.station.types import ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import cmd_pom_read_byte, cmd_service_result_request, cmd_station_status
from railctl.xbus.cv import CvEncoding
from railctl.xbus.replies import CvValue, NoAck, Ready

STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x01)
POLL = cmd_service_result_request()
ACK = encode(0x01, 0x04)
UNSUPPORTED = encode(0x61, 0x82)
NO_ACK_BYTES = encode(0x61, 0x13)
SHORT_CIRCUIT_BYTES = encode(0x61, 0x12)
ZIMO_CV8 = 145  # the MS450P22's known CV8 value - also why the doctor reads CV8 for D4


def cv_value(ident: int, c: int, value: int) -> bytes:
    return encode(0x63, ident, c, value)


def test_matcher_accepts_either_echo_form_while_pom_echo_zero_based_is_unknown():
    """CV8's two candidate echoes, 7 and 8, both have to match until the doctor
    has read one and pinned the convention down (docs/probe-results.md, R1)."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=None)
    assert matcher(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert matcher(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))


def test_matcher_narrows_to_one_form_once_zero_based_is_learned():
    zero_based = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=True)
    assert zero_based(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert not zero_based(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))

    one_based = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    assert one_based(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False))
    assert not one_based(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False))


def test_matcher_rejects_a_right_byte_wrong_band_reply():
    """A request for CV265 must not accept `63 14 09`, which is CV9.

    `echo_candidates` narrows only WITHIN a band; `result_ident_for` is what
    supplies the band. Skip either check and CV9's value comes back reported
    under CV265's name - CV265 is a ZIMO sound-project CV this tool backs up.
    """
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 265, zero_based=False)
    right_band = CvValue(raw_cv=9, value=7, ident=0x15, z21_form=False)
    wrong_band = CvValue(raw_cv=9, value=7, ident=0x14, z21_form=False)
    assert matcher(right_band)
    assert not matcher(wrong_band)


def test_matcher_decodes_the_documented_but_never_measured_z21_form_branch():
    """No `64 14` reply to a POM request has ever been observed on this
    hardware (docs/probe-results.md, R1). Kept general rather than rejected: a
    station that answered this way would still be answering, and dropping the
    reply would turn a real (if unmeasured) answer into silence - which reads
    as "unsupported" one layer up, the exact failure this project exists to
    catch.
    """
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    # CV8 zero-based wire value is 7, joined as the 16-bit field 0x0007.
    assert matcher(CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=True))
    # raw_cv=8 decodes (Z21's own rule) to CV9, not CV8.
    assert not matcher(CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=True))


def test_matcher_ignores_non_cv_replies():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    assert not matcher(NoAck())
    assert not matcher(Ready())


def test_value_of_reads_the_plain_byte():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    reply = CvValue(raw_cv=8, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.value_of(reply) == ZIMO_CV8


@pytest.mark.parametrize(("raw", "expected"), [(7, True), (8, False)])
def test_echo_says_zero_based_reads_cv8_either_way(raw: int, expected: bool):
    """CV8 is the doctor's probe CV precisely because 7 and 8 are distinguishable."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=None)
    reply = CvValue(raw_cv=raw, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.echo_says_zero_based(reply) is expected


def test_echo_says_zero_based_is_none_once_the_convention_is_already_fixed():
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=True)
    reply = CvValue(raw_cv=7, value=ZIMO_CV8, ident=0x14, z21_form=False)
    assert matcher.echo_says_zero_based(reply) is None
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `FAIL - ModuleNotFoundError: No module named 'railctl.station.programming'`. Named reason: the module does not exist yet.

- [ ] **Step 4: Implement `TimedOut`, `WaitOutcome`, `ResultChannelSeen`, `CvMatcher`**

Create `src/railctl/station/programming.py` with this much of it (the rest arrives in later steps of this same task):

```python
"""The shared CV wait loop, the echo matcher, POM reads, and AUTO mode
resolution (design doc lines 708-743, 786-787, 916-924).

`station/` may hold no framing bytes, no port names, and no CV arithmetic
(`tests/test_layering.py`, rules 1 and 2) - `xbus.cv`'s `echo_candidates`,
`decode_echo` and `result_ident_for` are what let this module compare CV
numbers and reply idents without ever touching a wire byte itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    ShortCircuitError,
    TrackPowerError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.types import CvResult, ProgMode
from railctl.xbus.commands import cmd_pom_read_byte, cmd_service_result_request
from railctl.xbus.cv import CvEncoding, decode_echo, echo_candidates, result_ident_for
from railctl.xbus.replies import (
    TRANSIENT_REPLIES,
    CvValue,
    NoAck,
    PagedCvValue,
    Ready,
    Reply,
    ShortCircuit,
    TrackShortCircuit,
    Unsupported,
    parse,
)

if TYPE_CHECKING:
    from railctl.station.facade import Station

__all__ = [
    "CvMatcher",
    "CvProgrammer",
    "ResultChannelSeen",
    "TimedOut",
    "WaitOutcome",
    "resolve_mode",
]


@dataclass(frozen=True, slots=True)
class TimedOut:
    """The wait ran out with no answer. Distinct from `NoAck`: the station may
    simply never have replied, which is UNKNOWN, never a negative answer."""

    polls: int
    ready_streak: int
    saw_no_ack: bool


WaitOutcome = Reply | TimedOut
ResultChannelSeen = Literal["broadcast", "poll"]


class CvMatcher:
    """Does a reply answer a request for `cv` under `encoding`?

    Two independent checks both have to hold for the measured `63 14..17`
    form, and mixing them up is the "right value, wrong CV name" failure this
    project exists to catch: `echo_candidates` narrows the C byte WITHIN a
    band, but two CVs 256 apart share a candidate set
    (`echo_candidates(POM_ZERO_BASED, 265) == echo_candidates(POM_ZERO_BASED,
    9)`), so the byte alone cannot tell CV265 from CV9. `result_ident_for`
    supplies the band the ident carries. Either check alone accepts a
    same-numbered CV from the wrong page.
    """

    def __init__(
        self,
        encoding: CvEncoding,
        cv: int,
        *,
        zero_based: bool | None = None,
        page_index: int | None = None,
    ) -> None:
        self.encoding = encoding
        self.cv = cv
        self._zero_based = zero_based
        # Consumed only when `encoding` is SERVICE_EXT (Task 5's service-mode
        # reads). POM's own matching needs only `cv` and `encoding`, because
        # `result_ident_for` already carries the band the SERVICE_EXT echo
        # byte cannot.
        self._page_index = page_index

    def __call__(self, reply: Reply) -> bool:
        if not isinstance(reply, CvValue):
            return False
        if reply.z21_form:
            # Documented, never measured on this hardware (docs/probe-results.md,
            # R1): no POM reply has ever been seen at all, let alone in this
            # form. Kept general anyway - see the module docstring on why a
            # real answer must never be treated as silence.
            try:
                return decode_echo(CvEncoding.Z21_16BIT, reply.raw_cv) == self.cv
            except ValueError:
                return False
        if reply.ident != result_ident_for(self.cv, self.encoding):
            return False
        return reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=self._zero_based)

    def value_of(self, reply: CvValue) -> int:
        """The decoded byte value. Trivial on its own - `CvValue.value` is
        already the plain 0..255 byte for every form `parse` produces - but
        kept as a method so a caller never reads `.value` off a reply it has
        not first confirmed matches, and so a future encoding with its own
        value convention has exactly one place to change."""
        return reply.value

    def echo_says_zero_based(self, reply: CvValue) -> bool | None:
        """Which POM echo convention `reply` demonstrates, or `None` if it
        does not settle the question.

        Only `POM_ZERO_BASED` has an unmeasured convention; every other
        encoding's echo rule is already fixed, so this always returns `None`
        for them. Delegates the byte comparison to `echo_candidates` rather
        than computing `cv - 1` or `cv & 0xFF` here, which would be CV
        arithmetic in `station/` - forbidden by `tests/test_layering.py`.
        """
        if self.encoding is not CvEncoding.POM_ZERO_BASED or reply.z21_form:
            return None
        if reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=True):
            return True
        if reply.raw_cv in echo_candidates(self.encoding, self.cv, zero_based=False):
            return False
        return None
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `FAIL - ImportError: cannot import name 'resolve_mode' from 'railctl.station.programming'` (the test file's own import line already names it). This is expected at this point - `resolve_mode` and `CvProgrammer` are not written yet, but everything above them (`CvMatcher` and its tests) is already exercised by pytest's collection; the import error blocks the whole file. Comment out the `resolve_mode` and `CvProgrammer` names from the `from railctl.station.programming import ...` line temporarily, rerun, confirm the nine `CvMatcher`/`value_of`/`echo_says_zero_based` tests above pass, then put the import line back before continuing to Step 5 - the module gains both names in the next two steps and the file will import cleanly again once they exist.

- [ ] **Step 5: Write the failing `resolve_mode` tests**

Append to `tests/station/test_cv_pom.py`:

```python
@pytest.mark.parametrize(
    ("pom_read", "service_direct_cv", "expected"),
    [
        (True, True, ProgMode.POM),
        (True, False, ProgMode.POM),
        (True, None, ProgMode.POM),
        (None, True, ProgMode.POM),
        (None, False, ProgMode.POM),
        (None, None, ProgMode.POM),
        (False, True, ProgMode.SERVICE),
    ],
)
def test_resolve_mode_auto_picks_pom_whenever_it_might_still_work(
    pom_read: bool | None, service_direct_cv: bool | None, expected: ProgMode
):
    """`pom_read is not False` covers both a measured `True` and an unprobed
    `None` - an unknown capability is tried, per the design's own rule, never
    refused pre-emptively."""
    capabilities = Capabilities.unknown("bench").with_learned(
        pom_read=pom_read, service_direct_cv=service_direct_cv
    )
    assert resolve_mode(ProgMode.AUTO, capabilities, operation="read") == expected


@pytest.mark.parametrize("service_direct_cv", [False, None])
def test_resolve_mode_auto_refuses_when_pom_is_measured_false_and_service_is_not(
    service_direct_cv: bool | None,
):
    """SERVICE is the fallback only when POM is a MEASURED no and service mode
    is a MEASURED yes. An unprobed `service_direct_cv is None` cannot receive
    silent fallback traffic any more than an unprobed POM path could be
    assumed to work."""
    capabilities = Capabilities.unknown("bench").with_learned(
        pom_read=False, service_direct_cv=service_direct_cv
    )
    with pytest.raises(PomReadUnsupportedError, match="--mode service") as excinfo:
        resolve_mode(ProgMode.AUTO, capabilities, operation="write")
    assert "--mode service" in excinfo.value.hint


def test_resolve_mode_never_returns_auto_and_leaves_an_explicit_mode_untouched():
    capabilities = Capabilities.unknown("bench")
    assert resolve_mode(ProgMode.POM, capabilities, operation="read") is ProgMode.POM
    assert resolve_mode(ProgMode.SERVICE, capabilities, operation="write") is ProgMode.SERVICE
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py -k resolve_mode
```

Expected: `FAIL - ImportError: cannot import name 'resolve_mode'`.

- [ ] **Step 6: Implement `resolve_mode` and commit**

Append to `src/railctl/station/programming.py`, after `class CvMatcher` and before the (not yet written) `class CvProgrammer`:

```python
def resolve_mode(
    mode: ProgMode, capabilities: Capabilities, *, operation: Literal["read", "write"]
) -> ProgMode:
    """AUTO never reaches the caller; every explicit mode passes through
    unchanged. `pom_read is not False` covers both `True` (measured working)
    and `None` (unknown - POM is tried and the outcome recorded). SERVICE is
    the fallback only when POM is a measured no AND service mode is a measured
    yes; nothing here is inferred from an unprobed capability.
    """
    if mode is not ProgMode.AUTO:
        return mode
    if capabilities.pom_read is not False:
        return ProgMode.POM
    if capabilities.service_direct_cv is True:
        return ProgMode.SERVICE
    raise PomReadUnsupportedError(
        f"POM is unsupported on this command station for a CV {operation}; put "
        f"the loco on the programming track and use `--mode service`",
        hint="put the loco on the programming track and use `--mode service`",
    )
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `19 passed, 0 failed` (9 `CvMatcher`/related tests from Steps 3-4 plus 10 `resolve_mode` cases from Step 5).

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: both silent.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_pom.py
git commit -m "feat(station): add CvMatcher and AUTO mode resolution"
```

- [ ] **Step 7: Write the failing `CvProgrammer` construction and cache-hook tests**

Append to `tests/station/test_cv_pom.py`:

```python
def test_station_wires_a_cv_programmer(bench):
    """Renamed from `..._with_a_page_cache_hook`: that name promised the cache
    hook was registered, but this body only ever checked the type. The hook
    itself is `test_closing_the_station_runs_the_registered_cache_hook`,
    below - `Station.__init__` could drop its `register_cache(...)` line and
    this test alone would stay green."""
    programmer = bench.station.programmer
    assert isinstance(programmer, CvProgrammer)


def test_invalidate_pages_clears_the_stub_cache_directly():
    """Task 6 fills `_ensure_page`'s real cache in here; today it is empty, but
    the hook has to exist now so `Station.__init__` is not touched twice."""
    programmer = CvProgrammer(station=object())  # no station method is called
    programmer._pages["probe"] = object()
    programmer.invalidate_pages()
    assert programmer._pages == {}


def test_closing_the_station_runs_the_registered_cache_hook(bench):
    bench.station.programmer._pages["probe"] = object()
    bench.station.close()
    assert bench.station.programmer._pages == {}
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py -k "programmer or invalidate or closing"
```

Expected: `FAIL - AttributeError: 'Station' object has no attribute 'programmer'` (or an equivalent error naming the missing attribute). `CvProgrammer` itself already exists as a class name imported at the top of the file, but `Station` has not been wired to construct one yet, and `__init__` has no `_pages` attribute or `invalidate_pages` method.

- [ ] **Step 8: Implement `CvProgrammer.__init__`/`invalidate_pages`, wire `facade.py`**

Append to `src/railctl/station/programming.py`, after `resolve_mode`:

```python
class CvProgrammer:
    """POM reads (this task) and, from Task 5, service-mode reads and writes,
    sharing one wait loop.

    Takes the whole `Station` rather than a bag of collaborators, and reads
    `station.capabilities` fresh on every call rather than caching it here:
    `learn()` replaces the `Capabilities` object wholesale, so a `CvProgrammer`
    that cached it at construction time would never see a capability this same
    session just learned.
    """

    def __init__(self, station: "Station") -> None:
        self._station = station
        self._pages: dict[object, object] = {}

    def invalidate_pages(self) -> None:
        """Registered with `station.register_cache` in `Station.__init__`. A
        no-op stub until Task 6 populates `_ensure_page`'s cache - there is
        nothing to clear yet, but the hook has to exist now."""
        self._pages.clear()
```

Open `src/railctl/station/facade.py`. Add `from railctl.station.programming import CvProgrammer` to the imports, and at the very end of `Station.__init__` - after every other collaborator attribute is already assigned - add:

```python
        self.programmer = CvProgrammer(self)
        self.register_cache(self.programmer.invalidate_pages)
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `22 passed, 0 failed` (19 from Step 6 plus the 3 new ones above).

- [ ] **Step 9: Write the failing shared-wait-loop tests**

Append to `tests/station/test_cv_pom.py`:

```python
def test_the_passive_branch_binds_and_tests_the_frame_it_polls(bench):
    """A passive branch that polls for a frame and discards it makes POM
    reads fail on exactly the behaviour they exist to support: a station that
    only pushes its result once polling has been switched off."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=UNSUPPORTED)
    bench.push(cv_value(0x14, 8, ZIMO_CV8))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, CvValue)
    assert outcome.value == ZIMO_CV8


def test_a_61_82_answer_to_the_poll_never_becomes_a_durable_capability_fact(bench):
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=UNSUPPORTED)
    bench.push(cv_value(0x14, 8, ZIMO_CV8))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    assert isinstance(outcome, CvValue)
    # The eventual match arrived as an unsolicited push, not as the poll's own
    # answer, so learning must say "broadcast" - never a trace of the 61 82.
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_every_inner_exchange_is_clamped_to_the_remaining_attempt_budget(bench):
    """Without the clamp, a poll issued at `li_ack_normal` (5.0 s) would
    overrun a 2.0 s attempt by 3 s. `FakeTransport` asserts on an unscripted
    request rather than timing out on its own, so silence has to be scripted
    explicitly - one `expect(..., reply=b"")` per poll the loop is expected to
    issue. At `timeout=2.0` the very first exchange's own clamped budget
    already consumes the whole attempt (`min(exchange_timeout=5.0,
    remaining=2.0)` is 2.0, not 5.0), so exactly one silent poll is scripted;
    the fake clock is what proves it was allowed only the 2.0 s attempt
    budget, never the passed `exchange_timeout`."""
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=b"")
    started = bench.clock.monotonic()
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=2.0,
        first_delay=0.0,
        interval=0.10,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="pom",
    )
    elapsed = bench.clock.monotonic() - started
    assert isinstance(outcome, TimedOut)
    assert elapsed == pytest.approx(2.0, abs=0.2)


def test_ready_streak_at_the_limit_ends_the_wait_as_no_ack(bench):
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    ready_bytes = encode(0x61, 0x11)
    for _ in range(TIMING.service_ready_limit):
        bench.expect(POLL, reply=ready_bytes)
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=5.0,
        first_delay=0.0,
        interval=0.0,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=False,
        context="service",
    )
    assert isinstance(outcome, NoAck)


def test_ready_means_done_ends_the_wait_on_the_first_ready(bench):
    matcher = CvMatcher(CvEncoding.POM_ZERO_BASED, 8, zero_based=False)
    bench.expect(POLL, reply=encode(0x61, 0x11))
    outcome = bench.station.programmer.await_result(
        matcher,
        timeout=5.0,
        first_delay=0.0,
        interval=0.0,
        exchange_timeout=5.0,
        allow_poll=True,
        ready_means_done=True,
        context="service",
    )
    assert isinstance(outcome, Ready)
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py -k "passive or 61_82 or clamped or ready"
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'await_result'`.

- [ ] **Step 10: Implement `_consider` and `await_result`**

Append to `class CvProgrammer` in `src/railctl/station/programming.py` (after `invalidate_pages`):

```python
    def _learn_result_channel(
        self, context: Literal["pom", "service"], channel: ResultChannelSeen
    ) -> None:
        """Only a POM read's channel is a durable fact.

        A `61 82` answer to a service-mode `21 10` poll is the *expected*
        reply from a station that pushes its result instead of answering the
        poll directly - recording it here would misfile an ordinary
        service-mode moment as a POM capability. Called only on an actual
        match, never on `61 82` itself, so the conditional-polling branch in
        `await_result` never has to touch capabilities at all.
        """
        if context == "pom":
            self._station.learn(pom_result_channel=channel)

    def _consider(
        self,
        reply: Reply,
        matcher: CvMatcher,
        *,
        context: Literal["pom", "service"],
        channel: ResultChannelSeen,
    ) -> Reply | None:
        """One reply, turned into an outcome, or `None` meaning "not an
        answer, keep waiting". `TRANSIENT_REPLIES` (`Busy`, `StationBusy`,
        `ShortCircuit`, `TrackShortCircuit`, `TransferError`) says nothing
        about support one way or the other - except `ShortCircuit` and
        `TrackShortCircuit`, which end the whole operation, so those two are
        pulled out first and handled as terminal. `PagedCvValue` is also
        terminal, unconditionally and with no matcher check: it is a real
        answer - the `63 10` paged form Task 5's service-mode reads use - and
        this loop is the only place either mode ever gets to see one.
        Swallowing it here (returning `None` and letting the deadline turn it
        into `TimedOut`) would make Task 5's whole paged-read branch
        unreachable while looking, from the outside, like an ordinary
        timeout - the exact "real answer read as no answer" failure this
        project exists to catch. `GenericAck` and `Other` fall through to the
        final `None`: neither settles the question, and the caller's own
        deadline is what eventually turns silence into `TimedOut`, not this
        function.
        """
        if isinstance(reply, CvValue):
            if matcher(reply):
                self._learn_result_channel(context, channel)
                return reply
            self._station.emit(
                "cv.stale_result",
                {"cv": matcher.cv, "raw_cv": reply.raw_cv, "encoding": matcher.encoding.name},
            )
            return None
        if isinstance(reply, PagedCvValue):
            return reply
        if isinstance(reply, (ShortCircuit, TrackShortCircuit, Ready, NoAck)):
            return reply
        if reply in TRANSIENT_REPLIES:
            return None
        return None

    def await_result(
        self,
        matcher: CvMatcher,
        *,
        timeout: float,
        first_delay: float,
        interval: float,
        exchange_timeout: float,
        allow_poll: bool,
        ready_means_done: bool,
        context: Literal["pom", "service"],
    ) -> WaitOutcome:
        """The wait loop POM (this task) and service mode (Task 5) share.

        Each pass drains whatever is already sitting on the port
        (`link.poll(0.0)`) before deciding whether to poll again. Polling is
        conditional: `21 10 31` answered `61 82` means the station only pushes
        results, so `polling` is switched off for the rest of THIS attempt and
        the loop falls back to a passive `link.poll(...)` wait, which is what
        lets a push-only station's answer still be caught. Every exchange this
        function issues is clamped to `max(timing.min_exchange,
        min(exchange_timeout, remaining))`, and a `LinkTimeout` from one ends
        that pass rather than escaping - the next loop iteration's own
        `remaining <= 0` check is what turns it into `TimedOut`.
        """
        timing = self._station.timing
        deadline = self._station.now() + timeout
        if first_delay:
            self._station.pause(first_delay)
        polling = allow_poll
        polls = 0
        ready_streak = 0
        saw_no_ack = False

        def settle(reply: Reply, *, channel: ResultChannelSeen) -> Reply | None:
            nonlocal ready_streak, saw_no_ack
            outcome = self._consider(reply, matcher, context=context, channel=channel)
            if outcome is None:
                return None
            if isinstance(outcome, NoAck):
                saw_no_ack = True
                return outcome
            if isinstance(outcome, Ready):
                if ready_means_done:
                    return outcome
                ready_streak += 1
                if ready_streak >= timing.service_ready_limit:
                    saw_no_ack = True
                    return NoAck()
                return None
            return outcome

        while True:
            for frame in self._station.link.poll(0.0):
                settled = settle(parse(frame.payload), channel="broadcast")
                if settled is not None:
                    return settled
            remaining = deadline - self._station.now()
            if remaining <= 0:
                return TimedOut(polls=polls, ready_streak=ready_streak, saw_no_ack=saw_no_ack)
            budget = max(timing.min_exchange, min(exchange_timeout, remaining))
            if polling:
                polls += 1
                try:
                    reply = self._station.exchange(cmd_service_result_request(), timeout=budget)
                except LinkTimeout:
                    continue
                if isinstance(reply, Unsupported):
                    polling = False
                    continue
                settled = settle(reply, channel="poll")
                if settled is not None:
                    return settled
                self._station.pause(interval)
            else:
                remaining = deadline - self._station.now()
                if remaining <= 0:
                    return TimedOut(polls=polls, ready_streak=ready_streak, saw_no_ack=saw_no_ack)
                for frame in self._station.link.poll(min(interval, remaining)):
                    settled = settle(parse(frame.payload), channel="broadcast")
                    if settled is not None:
                        return settled
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `27 passed, 0 failed` (22 from Step 8 plus the 5 new tests from Step 9).

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: both silent.

- [ ] **Step 11: Write the failing `pom_read` precondition tests**

Append to `tests/station/test_cv_pom.py`:

```python
def test_pom_read_refuses_before_sending_anything_when_pom_read_is_known_false(bench):
    bench.station.learn(pom_read=False)
    before = list(bench.sent)
    with pytest.raises(PomReadUnsupportedError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.sent == before


def test_pom_read_refuses_and_names_its_capabilities_provenance(bench_factory):
    """The precondition refusal's message has to say WHERE the "unsupported"
    fact came from - `capabilities.json`, when it was probed, and the note
    recorded at the time - or it reads as a bug in this tool rather than a
    fact the station already taught it in an earlier session. This is the
    provenance requirement moved here from the CLI layer's own tests (a
    message-content assertion belongs where the message is built, not where
    it is only ever copied through)."""
    capabilities = (
        Capabilities.unknown("bench")
        .with_learned(pom_read=False, probed_at="2026-01-01T00:00:00+00:00")
        .with_note("the command station answered `61 82` to a POM read of CV8")
    )
    bench = bench_factory(capabilities=capabilities)
    with pytest.raises(PomReadUnsupportedError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    message = str(excinfo.value)
    assert "capabilities.json" in message
    assert "2026-01-01T00:00:00+00:00" in message
    assert "61 82" in message


def test_pom_read_refuses_when_track_power_is_off(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)
    with pytest.raises(TrackPowerError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    assert excinfo.value.hint is not None
    assert "railctl power on" in excinfo.value.hint


def test_pom_read_refuses_with_no_address_anywhere(bench_factory):
    bench = bench_factory(default_address=None)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    with pytest.raises(ValueError, match="address"):
        bench.station.programmer.pom_read(8, address=None)
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py -k "precondition or refuses"
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'pom_read'`. The `bench_factory` fixture is injected by name as the test parameter - no import is needed.

- [ ] **Step 12: Write the failing `pom_read` behaviour tests**

Append to `tests/station/test_cv_pom.py`:

```python
def test_three_silent_attempts_raise_decoder_not_responding_never_unsupported(bench):
    """The single most important assertion in M5: silence is measured
    behaviour on this hardware (docs/probe-results.md, R1), and this project
    exists because an earlier instrument recorded it as `False` instead."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    for _ in range(TIMING.pom_read_attempts):
        bench.expect(pom8, reply=ACK)
        # `FakeTransport` asserts on an unscripted request rather than timing
        # out on its own, so this attempt's own silence has to be scripted
        # too - one empty-reply poll per attempt. `pom_result`'s 2.0 s budget
        # clamps the inner exchange the same way as the clamp test above, so
        # one silent poll is exactly what each attempt consumes.
        bench.expect(POLL, reply=b"")
    started = bench.clock.monotonic()
    with pytest.raises(DecoderNotRespondingError) as excinfo:
        bench.station.programmer.pom_read(8, address=3)
    elapsed = bench.clock.monotonic() - started
    message = str(excinfo.value).lower()
    assert "unsupported" not in message
    assert "not supported" not in message
    assert bench.station.capabilities.pom_read is None
    assert elapsed < 15.0
    # A script has no other way to tell "gave up after one attempt" from
    # "gave up after three" - the exit code and message text are identical
    # either way. `attempts` is what a caller (or a retry policy one layer up)
    # actually reads to make that distinction.
    assert excinfo.value.details == {
        "cv": 8,
        "address": 3,
        "mode": "pom",
        "attempts": TIMING.pom_read_attempts,
        "attempt_timeout_s": TIMING.pom_result,
    }


def test_unsupported_to_the_pom_telegram_raises_and_learns_pom_read_false(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=UNSUPPORTED)
    with pytest.raises(PomReadUnsupportedError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.station.capabilities.pom_read is False


def test_no_ack_seen_on_any_attempt_raises_decoder_no_ack_and_leaves_pom_read_unknown(bench):
    """`saw_no_ack` from attempt 1 has to survive attempts 2 and 3 coming back
    silent, so the final exception is still `DecoderNoAckError`, not
    `DecoderNotRespondingError`. Attempts 2 and 3's silence is scripted
    explicitly - `FakeTransport` asserts on an unscripted request rather than
    timing out on its own, so leaving `POLL` unscripted for those attempts
    would fail with `unexpected request`, not with the timeout this test
    means to exercise."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=ACK)
    bench.expect(POLL, reply=NO_ACK_BYTES)
    bench.expect(pom8, reply=ACK)  # attempt 2: silence
    bench.expect(POLL, reply=b"")
    bench.expect(pom8, reply=ACK)  # attempt 3: silence
    bench.expect(POLL, reply=b"")
    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.pom_read(8, address=3)
    assert bench.station.capabilities.pom_read is None


def test_a_successful_read_learns_zero_based_true_from_echo_seven(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 7, ZIMO_CV8))
    result = bench.station.programmer.pom_read(8, address=3)
    assert result.value == ZIMO_CV8
    assert result.mode is ProgMode.POM
    assert result.operation == "read"
    assert result.verified is None
    assert bench.station.capabilities.pom_read is True
    assert bench.station.capabilities.pom_echo_zero_based is True
    assert bench.station.capabilities.pom_result_channel == "broadcast"


def test_a_successful_read_learns_zero_based_false_from_echo_eight(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 8, ZIMO_CV8))
    result = bench.station.programmer.pom_read(8, address=3)
    assert result.value == ZIMO_CV8
    assert bench.station.capabilities.pom_echo_zero_based is False


def test_short_circuit_ends_the_read_immediately_with_no_retry(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=ACK)
    bench.expect(POLL, reply=SHORT_CIRCUIT_BYTES)
    with pytest.raises(ShortCircuitError):
        bench.station.programmer.pom_read(8, address=3)
    # handshake + status() + the one POM telegram + the one poll - no second
    # or third attempt. `Link.open()` counts the version handshake as request
    # 1 before this test ever calls anything (`link.py` L111), so the count
    # a bare "three things happened" reading of this test would expect is off
    # by exactly that one request.
    assert bench.station.link.stats().requests == 4


def test_a_stale_result_from_an_earlier_read_is_discarded_and_reported(bench):
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom7 = cmd_pom_read_byte(3, 7, threshold=bench.station.threshold)
    bench.expect(pom7, reply=cv_value(0x14, 7, 200))
    first = bench.station.programmer.pom_read(7, address=3)
    assert first.value == 200

    # A late reply belonging to that already-finished CV7 read is still
    # sitting on the port - exactly what the per-attempt drain exists to
    # clear before CV8's own telegram goes out.
    bench.push(cv_value(0x14, 7, 201))
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    pom8 = cmd_pom_read_byte(3, 8, threshold=bench.station.threshold)
    bench.expect(pom8, reply=cv_value(0x14, 8, ZIMO_CV8))
    second = bench.station.programmer.pom_read(8, address=3)
    assert second.value == ZIMO_CV8

    stale = [payload for name, payload in bench.events if name == "cv.stale_result"]
    assert stale == [{"cv": 8, "raw_cv": 7, "encoding": "POM_ZERO_BASED"}]
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py -k "silent or unsupported_to or no_ack_seen or learns_zero or short_circuit or stale"
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'pom_read'`.

- [ ] **Step 13: Give `RailctlError` an optional `details` dict, then implement `pom_read` and `_drain_stale`**

`pom_read`'s failure paths below need to hand a script something more specific than a message string - "one attempt" and "three attempts" produce the same exit code and the same wording otherwise. That structured payload has to live on the exception base, `RailctlError`, because every later task that renders an error (Task 8's `report_for`) reads it from there, not from a subclass. This task is the first one that needs it, so it is where it is added; nothing later re-adds it, only reads it.

Open `tests/unit/test_exit_codes.py`. Both `RailctlError` and `ProgrammingError` are already imported from `railctl.errors` at the top of the file - no import line changes here. Append this test at the end of the file:

```python
def test_railctl_error_details_defaults_to_empty_and_round_trips():
    """`RailctlError` itself carries `details` - `ProgrammingError` only adds
    `cv` alongside it, it does not introduce the field."""
    bare = RailctlError("x")
    assert bare.details == {}
    carrying = RailctlError("x", details={"cv": 8, "attempts": 3})
    assert carrying.details == {"cv": 8, "attempts": 3}
    programming = ProgrammingError("x", cv=8, details={"attempts": 3})
    assert programming.cv == 8
    assert programming.details == {"attempts": 3}
```

Run:

```bash
uv run pytest tests/unit/test_exit_codes.py
```

Expected: `FAIL - TypeError: RailctlError.__init__() got an unexpected keyword argument 'details'`. Named reason: the base class does not accept it yet.

Open `src/railctl/errors.py`. Change `RailctlError.__init__` from:

```python
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
```

to:

```python
    def __init__(
        self, message: str, *, hint: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.details = details or {}
```

and `ProgrammingError.__init__` from:

```python
    def __init__(self, message: str, *, hint: str | None = None, cv: int | None = None) -> None:
        super().__init__(message, hint=hint)
        self.cv = cv
```

to:

```python
    def __init__(
        self, message: str, *, hint: str | None = None, cv: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, hint=hint, details=details)
        self.cv = cv
```

Every other subclass in the file (`TrackPowerError`, `DecoderNoAckError`, `ShortCircuitError`, `DecoderNotRespondingError`, `PomReadUnsupportedError`, and the rest) defines no `__init__` of its own, so all of them pick up `details` for free through whichever parent they already inherit from - nothing else in this file changes.

Run:

```bash
uv run pytest tests/unit/test_exit_codes.py
```

Expected: all tests in the file pass, including the new one.

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: both silent.

Now append to `class CvProgrammer` in `src/railctl/station/programming.py` (after `await_result`):

```python
    def _drain_stale(self, matcher: CvMatcher) -> None:
        """Clear whatever is sitting on the port before this attempt's own
        telegram goes out.

        `Link.drain()` is exactly `poll(0.0)` with the return value thrown
        away (see link.py); this needs the frames back, because a `CvValue`
        left over from an earlier request must be reported through
        `cv.stale_result`, not silently swallowed. Anything else pending (an
        old ack, a broadcast) is already logged in `Link.stats()` and needs no
        further action.
        """
        for frame in self._station.link.poll(0.0):
            reply = parse(frame.payload)
            if isinstance(reply, CvValue):
                self._station.emit(
                    "cv.stale_result",
                    {"cv": matcher.cv, "raw_cv": reply.raw_cv, "encoding": matcher.encoding.name},
                )

    def pom_read(self, cv: int, *, address: int | None = None) -> CvResult:
        started = self._station.now()
        capabilities = self._station.capabilities
        if capabilities.pom_read is False:
            # Naming *where* this refusal comes from matters: "POM does not
            # work" read on its own looks like a bug report against this
            # tool, not a fact this same station already taught it. Naming
            # the file, when it was probed, and the note recorded at the time
            # is what turns the message into something the operator can go
            # verify or clear (`railctl doctor` re-probes and overwrites it).
            probed = capabilities.probed_at or "an unknown time"
            note = f"; {capabilities.notes[-1]}" if capabilities.notes else ""
            raise PomReadUnsupportedError(
                f"POM reads are recorded as unsupported for this station in "
                f"capabilities.json (probed {probed}{note}) - CV{cv}",
                hint="put the loco on the programming track and use `--mode service`",
                cv=cv,
            )
        if not self._station.status().track_power:
            raise TrackPowerError("POM needs the main track powered; run `railctl power on`")
        resolved = self._station.resolve_address(address)
        if resolved is None:
            raise ValueError(
                f"POM read of CV{cv} needs a locomotive address: pass address= "
                f"or set a default address"
            )

        timing = self._station.timing
        matcher = CvMatcher(
            CvEncoding.POM_ZERO_BASED, cv, zero_based=capabilities.pom_echo_zero_based
        )
        saw_no_ack = False
        for attempt in range(1, timing.pom_read_attempts + 1):
            self._drain_stale(matcher)
            telegram = cmd_pom_read_byte(resolved, cv, threshold=self._station.threshold)
            reply = self._station.exchange(telegram, timeout=timing.li_ack_normal)
            if isinstance(reply, Unsupported):
                self._station.learn(pom_read=False)
                raise PomReadUnsupportedError(
                    f"the command station answered `61 82` to a POM read of CV{cv}",
                    hint="put the loco on the programming track and use `--mode service`",
                    cv=cv,
                    details={
                        "cv": cv,
                        "address": resolved,
                        "mode": "pom",
                        "attempts": attempt,
                        "attempt_timeout_s": timing.pom_result,
                    },
                )
            settled = self._consider(reply, matcher, context="pom", channel="broadcast")
            outcome: WaitOutcome = (
                settled
                if settled is not None
                else self.await_result(
                    matcher,
                    timeout=timing.pom_result,
                    first_delay=0.0,
                    interval=timing.pom_poll_interval,
                    exchange_timeout=timing.li_ack_normal,
                    allow_poll=True,
                    ready_means_done=False,
                    context="pom",
                )
            )
            if isinstance(outcome, CvValue):
                learned: dict[str, object] = {"pom_read": True}
                if capabilities.pom_echo_zero_based is None:
                    zero_based = matcher.echo_says_zero_based(outcome)
                    if zero_based is not None:
                        learned["pom_echo_zero_based"] = zero_based
                self._station.learn(**learned)
                return CvResult(
                    cv=cv,
                    value=matcher.value_of(outcome),
                    mode=ProgMode.POM,
                    encoding=CvEncoding.POM_ZERO_BASED,
                    operation="read",
                    verified=None,
                    elapsed=self._station.now() - started,
                )
            if isinstance(outcome, (ShortCircuit, TrackShortCircuit)):
                raise ShortCircuitError(
                    f"short circuit reading CV{cv} over POM",
                    cv=cv,
                    details={
                        "cv": cv,
                        "address": resolved,
                        "mode": "pom",
                        "attempts": attempt,
                        "attempt_timeout_s": timing.pom_result,
                    },
                )
            if isinstance(outcome, NoAck) or (isinstance(outcome, TimedOut) and outcome.saw_no_ack):
                saw_no_ack = True
            if attempt < timing.pom_read_attempts:
                self._station.pause(timing.pom_retry_delay)
        # These are the two failure paths that ran out the clock rather than
        # getting a definite refusal - a script's only way to tell "gave up
        # after one attempt" from "gave up after three" is `details["attempts"]`,
        # since the message text and exit code are otherwise identical either
        # way.
        failure_details = {
            "cv": cv,
            "address": resolved,
            "mode": "pom",
            "attempts": timing.pom_read_attempts,
            "attempt_timeout_s": timing.pom_result,
        }
        if saw_no_ack:
            raise DecoderNoAckError(
                f"the decoder did not acknowledge the POM read of CV{cv}",
                cv=cv,
                details=failure_details,
            )
        raise DecoderNotRespondingError(
            f"CV{cv} produced no result over POM after {timing.pom_read_attempts} "
            f"attempts (interface ack only; docs/probe-results.md, R1)",
            cv=cv,
            details=failure_details,
        )
```

Run:

```bash
uv run pytest tests/station/test_cv_pom.py
```

Expected: `41 passed, 0 failed` (27 from Step 10 plus 4 precondition tests from Step 11 plus 7 behaviour tests from Step 12 plus the two-line assertion added to one of them - the assertion additions do not change the count, only the provenance test in Step 11 does).

```bash
uv run pytest tests/station/
```

Expected: same `41 passed, 0 failed` - this is currently the whole `tests/station/` package's contribution.

- [ ] **Step 14: Full suite, coverage, lint, commit**

```bash
uv run pytest
```

Expected: `N passed, 0 failed`, where `N` is this step's own running total, built the same way every other step in this plan builds one: `920` (the M2-M4 baseline) `+ (Task 1's own reported total) + (Task 2's) + (Task 3's) + 11` (10 from Step 2's `result_ident_for` cases, plus the 1 `details` test added to `tests/unit/test_exit_codes.py` in this step) `+ 41` (this task's own `tests/station/test_cv_pom.py`, per Step 13's own count above). Nobody upstream of this task has measured Tasks 1-3's contributions, so do not paste a number here - compare the printed total against that formula, and treat a difference from the formula as the signal, not a difference from any number written in this file.

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: `src/railctl/station/programming.py` and `src/railctl/station/facade.py` now appear in the coverage table; `Required test coverage of 90% reached.`, `0 failed`. Two things can show as missed, and both are this task's own, expected, branches:
- the `TRANSIENT_REPLIES` fallthrough in `_consider` - `Busy`/`StationBusy`/`TransferError` never appear in any scripted reply above; add one `bench.expect(POLL, reply=encode(0x61, 0x81))` (`StationBusy`) inside `test_the_passive_branch_binds_and_tests_the_frame_it_polls`'s script, before the `UNSUPPORTED` reply, to exercise it without changing that test's assertions.
- the `PagedCvValue` branch in `_consider` - no test in this file ever scripts a `63 10` reply, because the only caller that can produce one is Task 5's service-mode read, which does not exist yet. Leave it uncovered here; Task 5's own un-stubbed test (added per that task's fix for the same issue) is what exercises it, and a coverage gap on a branch with no caller yet is not a defect in this task.

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both silent, exit 0.

```bash
git add src/railctl/station/programming.py src/railctl/station/facade.py src/railctl/errors.py tests/station/test_cv_pom.py tests/unit/test_exit_codes.py
git commit -m "feat(station): add the POM read path, wait loop, and mode resolution"
```

---

### Task 5: Service-mode read: the encoding ladder, the 95 s regime, the exit path and `63 10`

**Files:**
- Create: `tests/station/test_cv_service_mode.py`
- Modify: `src/railctl/station/programming.py` (add the service read, the telegram ladder, the exit
  path, the public `cv_read`)
- Modify: `src/railctl/station/facade.py` (add `Station.cv_read` delegating to the programmer)

**Interfaces:**
- Consumes:
  - Task 4's `CvMatcher(encoding: CvEncoding, cv: int, *, zero_based: bool | None = None, page_index: int | None = None)`, already imported in `programming.py`
  - Task 4's `TimedOut` - `@dataclass(frozen=True, slots=True)` with `polls: int`, `ready_streak: int`, `saw_no_ack: bool`, already defined in `programming.py`
  - Task 4's `resolve_mode(mode: ProgMode, capabilities: Capabilities, *, operation: Literal["read", "write"]) -> ProgMode`, module-level in `programming.py`
  - Task 4's `CvProgrammer.await_result(self, matcher: CvMatcher, *, timeout: float, first_delay: float, interval: float, exchange_timeout: float, allow_poll: bool, ready_means_done: bool, context: Literal["pom", "service"]) -> WaitOutcome` (`WaitOutcome = Reply | TimedOut`), already a method on the class this task extends
  - Task 4's `CvProgrammer.pom_read(self, cv: int, *, address: int | None = None) -> CvResult`, already a method on the class this task extends
  - Task 4's `CvProgrammer.__init__(self, station: "Station") -> None` - the **only** collaborator attribute is `self._station`; nothing is cached. Capabilities, timing, the clock and the sleep function are all read fresh off the station on every call - `self._station.capabilities`, `self._station.timing`, `self._station.now()`, `self._station.pause(...)` - because `Station.learn(...)` replaces `capabilities` wholesale, and a value cached at construction time would never see a fact this same session just learned. There is **no** `self.capabilities`, `self.timing`, `self.clock`, `self.sleep`, `self._capabilities` or `self._timing` anywhere on `CvProgrammer`. Task 4 already follows this for the facts it learns (`pom_read`, `pom_result_channel`, `pom_echo_zero_based`) by calling `self._station.learn(...)`; this task's new code calls `self._station.learn(service_direct_cv=...)` the same way - never a rebind of an attribute that does not exist.
  - Task 4's `Station.programmer` - the `CvProgrammer` instance, a **plain public attribute** Task 4 assigns in `Station.__init__` (`self.programmer = CvProgrammer(self)`), never `self._programmer`. `Station.cv_read` (this task's own addition to `facade.py`) delegates to `self.programmer`, not to a private name.
  - `railctl.xbus.cv.result_ident_for(cv: int, encoding: CvEncoding) -> int` (Task 4) - used inside `CvMatcher`, not called directly by this task
  - Task 2's `Station` collaborator surface: `exchange(telegram: bytes, *, timeout: float) -> Reply`, `status() -> StationStatus`, `pause(seconds: float) -> None`, `invalidate_caches() -> None` (no parameters), `now() -> float`, `learn(**updates: object) -> None` (`service_direct_cv` is in `LEARNABLE_FIELDS`, so this task's own learned fact goes through it, never a rebind of a nonexistent `self.capabilities`), and `emit(name: str, payload: dict[str, object]) -> None` (forwards to the `on_event` callback given to `Station.__init__` - a positional `dict`, never `**payload`). `EVENT_NAMES` (`railctl.station.types`) already holds all **twelve** names from Task 1 onward - `cv.stale_result`, `cv.write_unverified`, `page.unverified`, `loco.in_use_by_other`, `address.band_unverified`, `function.group_seeded`, `power.on`, `power.off`, `loco.emergency_stop`, `service.entered` and `reply.unknown` are already spoken for by earlier tasks; this task adds the twelfth, `cv.unexercised_band`.
  - Task 2's `Station.__init__(self, link: Link, capabilities: Capabilities, *, default_address: int | None = None, capabilities_path: Path | None = None, timing: Timing = TIMING, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, on_event: Callable[[str, dict[str, object]], None] | None = None) -> None`, wrapping every public method in a `threading.RLock`
  - Task 1's `Timing` fields `li_ack_programming = 95.0`, `service_result = 95.0`, `service_first_poll_delay = 0.20`, `service_poll_interval = 0.50`, `service_ready_limit = 8`, `service_exit_settle = 0.10`, and the module singleton `TIMING: Final[Timing] = Timing()` - `Timing` has **fifteen** fields in total; this task only touches the six above
  - `railctl.station.capabilities.Capabilities` - frozen dataclass with `z21_cv_opcodes: bool | None`, `service_direct_cv: bool | None`, `service_ext_cv: bool | None`, `pom_read: bool | None`, plus `classmethod unknown(cls, identity: str) -> Capabilities` and `with_learned(self, **updates: object) -> Capabilities`. `with_learned` accepts **any** real field name - it is not gated by `LEARNABLE_FIELDS`, that gate belongs to `Station.learn` alone - so test setup below builds arbitrary True/False/None combinations through it and hands the result straight to `bench_factory(capabilities=...)`.
  - `railctl.station.types.ProgMode` (enum: `AUTO`, `POM`, `SERVICE`), `CvPage = tuple[int, int]`, `CvResult` - frozen dataclass with `cv: int`, `value: int`, `mode: ProgMode`, `encoding: CvEncoding`, `operation: Literal["read", "write"]`, `verified: bool | None`, `elapsed: float`
  - `railctl.xbus.commands.cmd_service_direct_read(cv: int) -> bytes` (on disk, `22 15 C X`), `cmd_service_ext_read(cv: int) -> bytes` (on disk, `22 18..1B C X`), `cmd_z21_cv_read(cv: int) -> bytes` (on disk, `23 11 MSB LSB X`), `cmd_service_result_request() -> bytes` (on disk, `21 10 31`), `cmd_track_power_on() -> bytes` (on disk, `21 81 A0`), `cmd_track_power_off() -> bytes` (on disk, `21 80 A1`), `cmd_station_status() -> bytes` (on disk, `21 24 05`, used only by this task's tests to script the track-power precondition)
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` (test-only, to build scripted replies without hand-computing XOR bytes)
  - `railctl.xbus.cv.CV_MIN = 1`, `MAX_CV_DIRECT = 255`, `MAX_CV_EXT = 1024`, `MAX_CV_Z21 = 1024` (all on disk), `ext_cv_fields(cv: int) -> tuple[int, int]` (on disk, returns `(page_index, C)`), `echo_candidates(encoding: CvEncoding, cv: int, *, zero_based: bool | None = None) -> frozenset[int]` (on disk)
  - `railctl.xbus.dialect.CvEncoding` (on disk: `POM_ZERO_BASED`, `SERVICE_DIRECT`, `SERVICE_EXT`, `Z21_16BIT`)
  - `railctl.xbus.replies.CvValue(raw_cv: int, value: int, ident: int, z21_form: bool)`, `PagedCvValue(raw_register: int, value: int)`, `Ready`, `Busy`, `NoAck`, `ShortCircuit`, `Unsupported`, `GenericAck`/`GENERIC_ACK`, `StationStatus(raw: int, emergency_off: bool, emergency_stop: bool, auto_start_mode: bool, service_mode: bool, powering_up: bool, ram_error: bool)` with `track_power` property (all on disk, all frozen dataclasses with no fields except where shown)
  - `railctl.errors.CvOutOfRangeError`, `DecoderNotRespondingError`, `DecoderNoAckError`, `StationBusyError`, `UnsupportedCommandError`, `ShortCircuitError`, `LinkTimeout` (all on disk; every `ProgrammingError` subclass takes `(message, *, hint=None, cv=None)`, `UnsupportedCommandError` and `LinkTimeout` are direct `RailctlError` subclasses and take only `(message, *, hint=None)`, no `cv`)
  - Task 2's `bench` / `bench_factory` pytest fixtures, exposed through `tests/station/conftest.py` with the following shape - stated here in full because this task's implementer sees only this file (per ADDENDUM §A.1, the source of truth):
    ```python
    class Bench:
        def __init__(
            self,
            *,
            chunk_size: int | None = None,
            envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
            capabilities: Capabilities | None = None,
            default_address: int | None = BENCH_DEFAULT_ADDRESS,
            capabilities_path: Path | None = None,
            timing: Timing = TIMING,
            **capability_overrides: object,
        ) -> None: ...
        # station, link, transport, clock, events and on_event_hook are plain attributes set here.

        def expect(
            self,
            request: bytes,
            reply: bytes | tuple[bytes, ...] = b"",
            *,
            broadcast: bytes | tuple[bytes, ...] = (),
        ) -> Bench: ...
        # bare X-Bus bytes in and out - Kind.SOLICITED framing happens inside.
        # Repeated calls with the same `request` queue successive answers, in order.
        def push(self, telegram: bytes) -> Bench: ...
        # queues `telegram` as an unsolicited (Kind.UNSOLICITED) frame.

        @property
        def sent(self) -> list[bytes]: ...
        # every telegram written since open(), BARE and in order, handshake excluded.

    def bench_factory(**kwargs: object) -> Bench: ...
    # builds Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs).open();
    # chunk_size/envelope_factory come from tests/conftest.py and are never passed by a test.

    @pytest.fixture
    def bench(bench_factory) -> Bench:
        return bench_factory()
    ```
    `chunk_size` and `envelope_cls` are injected by `bench_factory` and never passed by a test.
    `BENCH_DEFAULT_ADDRESS` is `3`. `bench.transport.written` holds FRAMED bytes - `Link.request`
    writes `envelope.wrap(telegram)` and `FakeTransport` appends exactly that - so no test in this
    task reads it; every assertion on what was sent reads `bench.sent`, which is bare telegrams in
    order, with the `open()` handshake already excluded.
    `bench_factory`'s default `capabilities` is `Capabilities.unknown("bench")`. This task needs
    capability combinations `Station.learn` cannot build at all - `z21_cv_opcodes` and
    `service_ext_cv` are not in `LEARNABLE_FIELDS` - so every test below passes a `Capabilities`
    object built through `with_learned` straight into `bench_factory(capabilities=...)`, never
    through `bench.station.learn(...)` after construction. **Every module under `tests/station/`
    gets its `Station` from `bench` / `bench_factory`; this file builds no fixture of its own.**
    Where one test needs to isolate a single branch of `service_read`, it monkeypatches the one
    method on `bench.station.programmer` that branch depends on (`await_result`, `pom_read`,
    `service_read`) rather than substituting a fake collaborator for the whole `Station`.

- Produces:

```python
# src/railctl/station/programming.py

SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] = (
    ("z21_cv_opcodes", CvEncoding.Z21_16BIT),
    ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT),
)
UNEXERCISED_BANDS: Final[frozenset[int]] = frozenset({2, 3})   # 63 16 / 63 17, never answered here

class CvProgrammer:
    def service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]: ...
    def service_read(self, cv: int) -> CvResult: ...
    def exit_service_mode(self, *, restore_power: bool) -> None: ...
    def cv_read(self, cv: int, *, address: int | None = None,
                mode: ProgMode = ProgMode.AUTO, page: CvPage | None = None) -> CvResult: ...
```

```python
# src/railctl/station/facade.py

class Station:
    def cv_read(self, cv: int, *, address: int | None = None,
                mode: ProgMode = ProgMode.AUTO, page: CvPage | None = None) -> CvResult: ...
```

`service_read_telegram` and `service_read` above do **not** take a `page` keyword. Task 6 adds it
- together with `ensure_page` and the `self.ensure_page(...)` call at the top of both `pom_read` and
`service_read` - in the same step that implements `ensure_page` (1.5, **[overrides CONTRACT]**:
spec line 709 lists `_ensure_page()` among POM read's own preconditions, and reading CV265 through
`cv_read` without it reads whatever index page happens to already be selected on the station - the
silent-wrong-value failure spec line 802 warns about). `Station.cv_read` and
`CvProgrammer.cv_read` above already carry `page` for signature symmetry with the write side; this
task's own `cv_read` body does not forward it anywhere yet, because there is nothing behind it to
forward it to until Task 6 lands.

**Layering note.** Every wire opcode this task touches goes through a `cmd_*` encoder or an
`xbus.cv` constant - never a raw byte literal - and every CV bound comparison (`cv <= MAX_CV_DIRECT`,
`cv <= MAX_CV_EXT`) is a comparison against an imported constant, never arithmetic on `cv` itself.
`tests/test_layering.py` greps `station/` for `cv - 1`, `cv + 1`, `% 256`, `>> 8`, `<< 8`; the only
place this task computes a band from a CV number is the already-existing `xbus.cv.ext_cv_fields`
call.

**On `_finish_service_read` and outcome types.** `await_result` returns one member of
`xbus.replies.Reply` or a `TimedOut`. For POM (Task 4's own read loop) only four shapes ever come
back - `CvValue`, `ShortCircuit`, `NoAck`, `TimedOut` - because POM never enters service mode and
never sees a register fallback. Service mode adds two more real, terminal shapes: `PagedCvValue`
(the `63 10` register fallback, spec line 706) and `Busy` (`61 1F`, "a programming operation is
already running" - a definitive signal, not a "not yet" signal like `Ready`, which `await_result`
already absorbs internally per the design's "Ready is counted, not trusted"). `TimedOut.saw_no_ack`
is what tells apart true silence from a `NoAck` that arrived mid-wait but was not the very last
frame before the deadline; both still mean "the decoder did not acknowledge", so both raise
`DecoderNoAckError`, not `DecoderNotRespondingError`. `_consider` (Task 4's own dispatcher inside
`await_result`) must treat `PagedCvValue` as terminal the same way it treats `CvValue` - it is a
real answer, not silence - or the whole `63 10` branch below is unreachable and every read that
should end in a register fallback times out as `TimedOut` instead (2.24 / 5h; pinned by this task's
own un-stubbed test at the end of Step 10's block).

- [ ] **Step 1: Write the failing tests for the encoding ladder**

Create `tests/station/test_cv_service_mode.py` with the shared fixtures and the tests for
`service_read_telegram` and the two constants. This file is self-contained: it builds no fake
station of its own and takes every `Station` it needs from Task 2's `bench` / `bench_factory`
fixtures (`tests/station/conftest.py`), because the implementer of this task sees only this file.

```python
# tests/station/test_cv_service_mode.py
"""CvProgrammer's service-mode read: the encoding ladder, the 95 s timeout regime,
exit_service_mode, the 63 10 register fallback, and Station.cv_read.

Every test here goes through `bench` / `bench_factory` (tests/station/conftest.py,
Task 2), which wrap a real Station over a FakeTransport-backed Link, already past
the version handshake, driven by a fake clock. A service-mode read costs up to
TIMING.service_result (95.0 s) against a real clock; under the fake clock every
test in this file runs in microseconds. If a test in this file takes real
seconds, a real time.sleep leaked in through Station(sleep=...).
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    CvOutOfRangeError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    LinkTimeout,
    ShortCircuitError,
    StationBusyError,
    UnsupportedCommandError,
)
from railctl.station.capabilities import Capabilities
from railctl.station.programming import (
    SERVICE_ENCODING_ORDER,
    UNEXERCISED_BANDS,
    CvProgrammer,
    TimedOut,
)
from railctl.station.timing import TIMING
from railctl.station.types import ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_service_result_request,
    cmd_station_status,
    cmd_track_power_on,
    cmd_z21_cv_read,
)
from railctl.xbus.cv import MAX_CV_DIRECT
from railctl.xbus.dialect import CvEncoding
from railctl.xbus.replies import GENERIC_ACK, Busy, CvValue, NoAck, PagedCvValue, ShortCircuit

STATUS_REQUEST = cmd_station_status()
STATUS_POWER_ON = encode(0x62, 0x22, 0x00)
STATUS_POWER_OFF = encode(0x62, 0x22, 0x01)
STATUS_SERVICE_MODE = encode(0x62, 0x22, 0x08)
POLL = cmd_service_result_request()
ACK = encode(0x01, 0x04)
UNSUPPORTED = encode(0x61, 0x82)


def make_capabilities(**overrides: bool | None) -> Capabilities:
    """`Capabilities.unknown` with a hand-picked True/False/None combination,
    passed straight into `bench_factory(capabilities=...)`.

    Goes through `with_learned` rather than `bench.station.learn(...)` on
    purpose: most of the fields this file sets (`z21_cv_opcodes`,
    `service_ext_cv`) are not in `LEARNABLE_FIELDS` at all, and only
    `Station.learn` enforces that list - `with_learned` accepts any real
    field name (1.3).
    """
    return Capabilities.unknown("bench").with_learned(**overrides)


def test_service_encoding_order_is_z21_then_direct_then_extended():
    """Pins the order itself: the earlier design put service_direct first,
    which on this station is the one encoding that answers nothing until
    separately polled. Z21 covers CV1..1024 in one field and its result
    arrives unsolicited - the only channel that cannot return a stale
    stored result. Task 6 iterates this tuple as
    `for field_name, encoding in SERVICE_ENCODING_ORDER` and gates each step
    on `getattr(capabilities, field_name) is True` - a bare-enum form
    crashes, which is exactly why the tuple carries the field name alongside
    the encoding rather than the encoding alone."""
    assert [name for name, _ in SERVICE_ENCODING_ORDER] == [
        "z21_cv_opcodes",
        "service_direct_cv",
        "service_ext_cv",
    ]
    assert [encoding for _, encoding in SERVICE_ENCODING_ORDER] == [
        CvEncoding.Z21_16BIT,
        CvEncoding.SERVICE_DIRECT,
        CvEncoding.SERVICE_EXT,
    ]


def test_unexercised_bands_are_the_two_high_pages_never_answered_here():
    """Only 63 14 (CV1-255) and 63 15 (CV256-511) have ever been answered on
    real hardware; 63 16 and 63 17 come from the Lenz document alone."""
    assert UNEXERCISED_BANDS == frozenset({2, 3})


def test_service_read_telegram_prefers_z21_when_every_capability_is_true(bench_factory):
    capabilities = make_capabilities(
        z21_cv_opcodes=True, service_direct_cv=True, service_ext_cv=True
    )
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(8)

    assert telegram == cmd_z21_cv_read(8)
    assert encoding is CvEncoding.Z21_16BIT
    assert page == 0


def test_service_read_telegram_falls_back_to_direct_when_z21_is_unavailable(bench_factory):
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(8)

    assert telegram == cmd_service_direct_read(8)
    assert encoding is CvEncoding.SERVICE_DIRECT
    assert page == 0


def test_service_read_telegram_never_sends_direct_opcode_above_cv255(bench_factory):
    """cv <= 255 gates step 2 even when service_direct_cv is True: CV265 has
    no valid direct-mode wire form at all (direct_cv_byte refuses it)."""
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "pom" in caught.value.hint.lower()


def test_service_read_telegram_uses_extended_opcode_for_cv265_with_only_that_capability(
    bench_factory,
):
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)

    telegram, encoding, page = bench.station.programmer.service_read_telegram(265)

    assert telegram.hex(" ").upper().startswith("22 19 09")
    assert telegram == cmd_service_ext_read(265)
    assert encoding is CvEncoding.SERVICE_EXT
    assert page == 1


def test_service_read_telegram_with_no_encoding_probed_names_the_bound_and_suggests_doctor(
    bench_factory,
):
    """An unprobed station never sends an opcode that has not been observed
    to work: None is not enough, only True is."""
    capabilities = make_capabilities(
        z21_cv_opcodes=None, service_direct_cv=None, service_ext_cv=None
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(8)
    assert str(MAX_CV_DIRECT) in str(caught.value)
    assert "doctor" in caught.value.hint


def test_service_read_telegram_when_the_cv_exceeds_every_available_encoding_suggests_pom(
    bench_factory,
):
    capabilities = make_capabilities(
        z21_cv_opcodes=False, service_direct_cv=True, service_ext_cv=False
    )
    bench = bench_factory(capabilities=capabilities)

    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_read_telegram(265)
    assert "pom" in caught.value.hint.lower()
```

- [ ] **Step 2: Run it and see it fail**

```bash
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: FAIL at collection - `ImportError: cannot import name 'SERVICE_ENCODING_ORDER' from
'railctl.station.programming'` (or `UNEXERCISED_BANDS`, whichever name Python's import machinery
reports first). Neither constant nor `service_read_telegram` exists yet.

- [ ] **Step 3: Implement the encoding ladder**

Add `Final` to the existing `from typing import TYPE_CHECKING, Literal` line at the top of
`programming.py`, making it `from typing import TYPE_CHECKING, Final, Literal` - `SERVICE_ENCODING_ORDER`
and `UNEXERCISED_BANDS` below both need it, and Task 4's own import line never added it (2.12).

Then add to `src/railctl/station/programming.py`, alongside the existing imports (these four names
are new; everything else this step touches - `CvEncoding`, `CvOutOfRangeError`, `cmd_z21_cv_read`,
`cmd_service_direct_read`, `cmd_service_ext_read`, `MAX_CV_DIRECT`, `MAX_CV_EXT`, `MAX_CV_Z21`,
`CV_MIN`, `ext_cv_fields` - is already imported by Task 4):

```python
from railctl.xbus.cv import (
    CV_MIN,
    MAX_CV_DIRECT,
    MAX_CV_EXT,
    MAX_CV_Z21,
    ext_cv_fields,
)
```

Then the ladder itself:

```python
SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] = (
    ("z21_cv_opcodes", CvEncoding.Z21_16BIT),
    ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT),
)
UNEXERCISED_BANDS: Final[frozenset[int]] = frozenset({2, 3})   # 63 16 / 63 17, never answered here


class CvProgrammer:
    # ... existing Task 4 members above ...

    def service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]:
        """Choose the wire encoding for a service-mode CV read.

        Z21 first, not third: measured on the reference station, the Z21
        16-bit opcode covers CV1..1024 in one unambiguous field and its
        result arrives unsolicited - the only channel here that cannot
        return a stale stored result. `service_direct` answers nothing at
        all until separately polled with `21 10 31`; leading with it (the
        earlier order) means every read pays for a poll round trip the Z21
        opcode never needs.

        Every step requires its capability to be exactly True. `None` means
        "not established" - docs/probe-results.md distinguishes true, false
        and unknown throughout - and an unprobed station never sends an
        opcode that has not been observed to work.
        """
        capabilities = self._station.capabilities
        if capabilities.z21_cv_opcodes is True and cv <= MAX_CV_Z21:
            return cmd_z21_cv_read(cv), CvEncoding.Z21_16BIT, 0
        if capabilities.service_direct_cv is True and cv <= MAX_CV_DIRECT:
            return cmd_service_direct_read(cv), CvEncoding.SERVICE_DIRECT, 0
        if capabilities.service_ext_cv is True and cv <= MAX_CV_EXT:
            page, _ = ext_cv_fields(cv)
            return cmd_service_ext_read(cv), CvEncoding.SERVICE_EXT, page
        if all(getattr(capabilities, name) is None for name, _ in SERVICE_ENCODING_ORDER):
            raise CvOutOfRangeError(
                f"CV{cv} is not reachable in service mode: no encoding has been "
                f"probed on this command station (Z21 covers CV{CV_MIN}..{MAX_CV_Z21}, "
                f"extended CV{CV_MIN}..{MAX_CV_EXT}, direct CV{CV_MIN}..{MAX_CV_DIRECT}, "
                f"all unknown)",
                hint="run `railctl doctor` to probe the service-mode encodings",
                cv=cv,
            )
        raise CvOutOfRangeError(
            f"CV{cv} is not reachable in service mode on this command station "
            f"(no extended or Z21 CV opcodes; direct opcodes only cover "
            f"CV{CV_MIN}..{MAX_CV_DIRECT})",
            hint="use `--mode pom`",
            cv=cv,
        )
```

- [ ] **Step 4: Run it and see it pass**

```bash
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: PASS - `2 * 6 + 2 = 14 passed` (six ladder tests, each running twice through
`bench_factory`'s `chunk_size` parametrisation, plus the two plain constant pins that take no
fixture and so run once each).

- [ ] **Step 5: Write the failing tests for `exit_service_mode`**

Append to `tests/station/test_cv_service_mode.py`:

```python
def test_exit_service_mode_retries_once_then_raises_station_busy_error(bench):
    bench.expect(cmd_track_power_on(), reply=ACK)             # attempt 1
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)             # attempt 2 (the retry)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)

    with pytest.raises(StationBusyError):
        bench.station.programmer.exit_service_mode(restore_power=True)
    assert bench.transport.script_pending == []


def test_exit_service_mode_succeeds_on_the_second_attempt(bench):
    """The retry itself, not just its two ends: an implementation that gives
    up after the FIRST resume-operations telegram would leave the second
    scripted exchange unconsumed, which `script_pending` below catches -
    a fails-after-two-attempts test alone cannot tell a real retry from a
    single attempt that happens to time out the same way."""
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True)

    assert bench.transport.script_pending == []


def test_exit_service_mode_does_not_power_off_when_track_was_powered_before(bench):
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True)

    # No track_power_off exchange is scripted: if the implementation sent one
    # anyway, FakeTransport would raise its own AssertionError ("unexpected
    # request") before this line runs.
    assert bench.transport.script_pending == []


def test_exit_service_mode_powers_off_when_track_was_unpowered_before(bench):
    """The measured state of this hardware is an unpowered bench track
    (docs/probe-results.md): resume-operations always re-energises the main
    track, and the station's start mode is automatic, so skipping this
    would start every locomotive moving at its last speed."""
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_OFF)
    bench.expect(cmd_track_power_on(), reply=ACK)  # 21 80 A1 shares the encoded frame's shape here

    bench.station.programmer.exit_service_mode(restore_power=False)

    assert bench.transport.script_pending == []


def test_exit_service_mode_every_exchange_uses_the_programming_timeout(bench):
    """Nothing is scripted for the resume-operations reply, so the exchange
    times out - the fake clock is what proves the BUDGET it was given, not a
    fixed reply. At `li_ack_normal` (5.0 s) this would time out nineteen
    times sooner than the measured 95 s regime."""
    bench.expect(cmd_track_power_on(), reply=b"")  # silent on purpose

    started = bench.clock.monotonic()
    with pytest.raises(LinkTimeout):
        bench.station.programmer.exit_service_mode(restore_power=False)
    elapsed = bench.clock.monotonic() - started

    assert elapsed == pytest.approx(TIMING.li_ack_programming, abs=0.5)


def test_exit_service_mode_always_invalidates_the_page_cache(bench, monkeypatch):
    """The decoder on the programming track is not necessarily the one on
    the main track."""
    calls = []
    monkeypatch.setattr(bench.station, "invalidate_caches", lambda: calls.append(None))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    bench.station.programmer.exit_service_mode(restore_power=True)

    assert len(calls) == 1


def test_exit_service_mode_invalidates_the_cache_even_when_it_raises(bench, monkeypatch):
    calls = []
    monkeypatch.setattr(bench.station, "invalidate_caches", lambda: calls.append(None))
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_SERVICE_MODE)

    with pytest.raises(StationBusyError):
        bench.station.programmer.exit_service_mode(restore_power=True)
    assert len(calls) == 1
```

- [ ] **Step 6: Run it and see it fail**

```bash
uv run pytest tests/station/test_cv_service_mode.py -k exit_service_mode
```

Expected: FAIL - `AttributeError: 'CvProgrammer' object has no attribute 'exit_service_mode'`, on
all `2 * 7 = 14` new test instances (each of the seven test functions above runs once per
`chunk_size` id).

- [ ] **Step 7: Implement `exit_service_mode`**

`TIMING.li_ack_normal` is not imported for this comparison anywhere in this file's tests - it is the
value `test_exit_service_mode_every_exchange_uses_the_programming_timeout` proves is ABSENT from
every exchange by timing it out at the programming budget instead, so leave it unimported in the
implementation too.

```python
    def exit_service_mode(self, *, restore_power: bool) -> None:
        """Leave service mode and restore the pre-operation track power state.

        Always called from a `finally` by every service-mode caller: a
        `DecoderNoAckError` raised mid-read must still send resume-operations,
        because that is what re-energises the main track, and skipping it
        here would leave the layout dead until the next unrelated command
        happens to touch power.

        Every exchange in this method uses `TIMING.li_ack_programming`, the
        same 95 s budget as the read itself, through completion: the LI-USB
        rule is that no new command may be sent until the previous one is
        acknowledged, and the station may still be finishing the read's
        internal retries when this runs.
        """
        try:
            left_service_mode = False
            for _ in range(2):
                self._station.exchange(
                    cmd_track_power_on(), timeout=self._station.timing.li_ack_programming
                )
                self._station.pause(self._station.timing.service_exit_settle)
                if not self._station.status().service_mode:
                    left_service_mode = True
                    break
            if not left_service_mode:
                raise StationBusyError(
                    "the command station is still reporting service mode after "
                    "resume-operations was sent twice"
                )
            if not restore_power:
                # Not optional: the measured state of this hardware is an
                # unpowered bench track, and the station's start mode is
                # automatic, so every service read would otherwise start the
                # locomotives moving.
                self._station.exchange(
                    cmd_track_power_off(), timeout=self._station.timing.li_ack_programming
                )
        finally:
            self._station.invalidate_caches()
```

Add the two new imports this needs at the top of `programming.py`:

```python
from railctl.errors import StationBusyError
from railctl.xbus.commands import cmd_track_power_off, cmd_track_power_on
```

(`StationBusyError` may already be imported by Task 4 for a different branch - if `ruff` reports a
duplicate import in Step 19 below, delete this line instead of the existing one.)

- [ ] **Step 8: Run it and see it pass**

```bash
uv run pytest tests/station/test_cv_service_mode.py -k exit_service_mode
```

Expected: PASS - `2 * 7 = 14 passed`.

- [ ] **Step 9: Run the whole file so far**

```bash
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: PASS - `28 passed` (`2 * 6 + 2 = 14` from Step 4 plus `2 * 7 = 14` from Step 8).

- [ ] **Step 10: Write the failing tests for `service_read`**

Append to `tests/station/test_cv_service_mode.py`. Every outcome test below stubs `await_result`
directly (`monkeypatch.setattr(bench.station.programmer, "await_result", ...)`) rather than driving
it through a real polling loop: `await_result` is Task 4's own well-tested method, and this task
tests `service_read`'s USE of it - the telegram it sends first, the timeout budget, and how each
possible outcome maps to a `CvResult` or an exception - not `await_result`'s internal wait mechanics
again. The one exception is the last test in this block, which drives a real `63 10` reply through
the un-stubbed loop on purpose (2.24 / 5h).

```python
def _script_read_and_clean_exit(bench, read_telegram: bytes, *, read_reply: bytes = ACK) -> None:
    """Every `service_read` test below needs the same shape around the one
    call whose outcome differs: the power-before status check, the read
    telegram itself, and `exit_service_mode`'s own resume-operations
    exchange and status check (service_mode already False, track already
    powered, so the exit path in every one of these tests takes exactly one
    attempt and sends no power-off). Factored out so each test scripts only
    what makes it different - the read telegram's own reply, or the stubbed
    `await_result` outcome."""
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(read_telegram, reply=read_reply)
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)


def test_the_service_window_uses_the_programming_timeout_on_the_telegram_and_every_poll(
    bench_factory, monkeypatch
):
    """Running a service read at 5 s and polling every 0.5 s sends a new
    command while the previous one is unacknowledged and desynchronises the
    link. service_poll_interval is a minimum GAP between polls, never a
    reply deadline - that gap is await_result's job, not re-measured here;
    this test only pins the BUDGET every call into await_result gets. The
    read telegram's own timeout is pinned separately, below, because a
    stubbed await_result never exercises it."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    recorded: dict[str, object] = {}

    def fake_await_result(matcher, **kwargs):
        recorded.update(kwargs)
        return CvValue(raw_cv=8, value=42, ident=0x14, z21_form=True)

    monkeypatch.setattr(bench.station.programmer, "await_result", fake_await_result)

    bench.station.programmer.service_read(8)

    assert recorded["timeout"] == TIMING.service_result
    assert recorded["exchange_timeout"] == TIMING.li_ack_programming
    assert recorded["first_delay"] == TIMING.service_first_poll_delay
    assert recorded["interval"] == TIMING.service_poll_interval
    assert recorded["allow_poll"] is True
    assert recorded["ready_means_done"] is False
    assert recorded["context"] == "service"


def test_the_read_telegram_itself_uses_the_programming_timeout(bench_factory):
    """Nothing is scripted for the read telegram's own reply, so this
    exchange times out - proving the BUDGET it was given, not a fixed reply.
    `exit_service_mode` still runs from the `finally` afterwards, so its own
    two exchanges are scripted too."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_z21_cv_read(8), reply=b"")  # silent on purpose
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    started = bench.clock.monotonic()
    with pytest.raises(LinkTimeout):
        bench.station.programmer.service_read(8)
    elapsed = bench.clock.monotonic() - started

    assert elapsed == pytest.approx(TIMING.li_ack_programming, abs=0.5)


def test_the_station_rejecting_the_read_opcode_raises_unsupported_command_error(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8), read_reply=UNSUPPORTED)
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda *a, **k: pytest.fail("must not be reached")
    )

    with pytest.raises(UnsupportedCommandError):
        bench.station.programmer.service_read(8)


def test_a_no_ack_outcome_raises_decoder_no_ack_and_still_runs_exit_once(bench_factory, monkeypatch):
    """Two must-pins share one script. exit_service_mode always runs, even
    when the read raises - proved by `script_pending` being empty, which it
    would not be if the exit exchanges were left unconsumed. And a service
    read is never retried automatically: they already cost up to 95 s and
    the station retries internally, so exactly one read telegram is
    scripted - a second identical request would not match the next scripted
    exchange (the exit-mode resume-operations telegram), and FakeTransport
    would raise its own AssertionError."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: NoAck())

    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.service_read(8)
    assert bench.transport.script_pending == []


def test_a_no_ack_reply_names_pom_as_the_alternative(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: NoAck())

    with pytest.raises(DecoderNoAckError) as caught:
        bench.station.programmer.service_read(8)
    assert "pom" in caught.value.hint.lower()


def test_a_short_circuit_raises_short_circuit_error(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer, "await_result", lambda matcher, **kw: ShortCircuit()
    )

    with pytest.raises(ShortCircuitError):
        bench.station.programmer.service_read(8)


def test_a_busy_state_raises_station_busy_error_not_decoder_not_responding(bench_factory, monkeypatch):
    """61 1F means a programming operation is already running - a real,
    definitive signal, not silence, so it must not be reported the same way
    as no answer at all."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: Busy())

    with pytest.raises(StationBusyError):
        bench.station.programmer.service_read(8)


def test_a_timed_out_result_that_saw_a_no_ack_raises_decoder_no_ack(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: TimedOut(polls=6, ready_streak=0, saw_no_ack=True),
    )

    with pytest.raises(DecoderNoAckError):
        bench.station.programmer.service_read(8)


def test_a_timed_out_result_with_nothing_seen_raises_decoder_not_responding(bench_factory, monkeypatch):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: TimedOut(polls=6, ready_streak=8, saw_no_ack=False),
    )

    with pytest.raises(DecoderNotRespondingError):
        bench.station.programmer.service_read(8)


def test_an_unexpected_reply_from_await_result_raises_decoder_not_responding(bench_factory, monkeypatch):
    """Totality: any reply shape service_read does not recognise is treated
    as unresolved, never silently accepted as success."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(bench.station.programmer, "await_result", lambda matcher, **kw: GENERIC_ACK)

    with pytest.raises(DecoderNotRespondingError):
        bench.station.programmer.service_read(8)


def test_paged_cv_value_for_cv1_to_8_raises_decoder_not_responding_with_register_wording(
    bench_factory, monkeypatch
):
    """23151 3.1.2.6: 63 10 means the station fell back to register mode.
    Register numbers 1-8 are indistinguishable from CV numbers 1-8, so the
    value is never usable there, whatever the register byte says."""
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=8, value=5),
    )

    with pytest.raises(DecoderNotRespondingError) as caught:
        bench.station.programmer.service_read(8)
    assert "register" in str(caught.value)
    assert bench.station.capabilities.service_direct_cv is False


def test_paged_cv_value_above_cv8_is_accepted_when_the_register_echoes_the_cv(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(9))
    # CV9's only direct-mode echo candidate is 9 (xbus.cv.echo_candidates).
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=9, value=200),
    )

    result = bench.station.programmer.service_read(9)

    assert result.value == 200
    assert result.encoding is CvEncoding.SERVICE_DIRECT
    assert bench.station.capabilities.service_direct_cv is False


def test_paged_cv_value_above_cv8_with_a_mismatched_register_raises_without_register_wording(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(9))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: PagedCvValue(raw_register=200, value=1),
    )

    with pytest.raises(DecoderNotRespondingError) as caught:
        bench.station.programmer.service_read(9)
    assert "register" not in str(caught.value)


def test_a_successful_read_reports_service_mode_the_encoding_and_no_verification(
    bench_factory, monkeypatch
):
    capabilities = make_capabilities(z21_cv_opcodes=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_z21_cv_read(8))

    def fake_await_result(matcher, **kw):
        bench.clock.advance(1.7)  # measured: about 1.7 s for one real service read
        return CvValue(raw_cv=8, value=145, ident=0x14, z21_form=True)

    monkeypatch.setattr(bench.station.programmer, "await_result", fake_await_result)

    result = bench.station.programmer.service_read(8)

    assert result.cv == 8
    assert result.value == 145
    assert result.mode is ProgMode.SERVICE
    assert result.encoding is CvEncoding.Z21_16BIT
    assert result.verified is None
    assert result.elapsed == pytest.approx(1.7)


def test_a_read_in_an_unexercised_band_emits_a_note(bench_factory, monkeypatch):
    """CV600 is band 2 (63 16) - documented but never answered on this
    station. The read still succeeds mechanically; the note is what stops
    the JSON output implying it was verified."""
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_service_ext_read(600))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=88, value=1, ident=0x16, z21_form=False),
    )

    bench.station.programmer.service_read(600)

    assert bench.events == [("cv.unexercised_band", {"cv": 600, "page": 2})]


def test_a_read_in_an_exercised_band_emits_no_note(bench_factory, monkeypatch):
    """CV265 is band 1 (63 15) - measured and answered on this hardware."""
    capabilities = make_capabilities(service_ext_cv=True)
    bench = bench_factory(capabilities=capabilities)
    _script_read_and_clean_exit(bench, cmd_service_ext_read(265))
    monkeypatch.setattr(
        bench.station.programmer,
        "await_result",
        lambda matcher, **kw: CvValue(raw_cv=9, value=1, ident=0x15, z21_form=False),
    )

    bench.station.programmer.service_read(265)

    assert bench.events == []


def test_a_real_63_10_reply_drives_paged_cv_value_through_the_unstubbed_loop(bench_factory):
    """Every outcome test above stubs `await_result` and never drives a real
    `PagedCvValue` through the wait loop. Task 4's own `_consider` must
    return `PagedCvValue` as a TERMINAL outcome for this branch to ever
    complete (2.24 / 5h) - without that fix this test times out as
    `TimedOut` instead of returning a result, because `_consider` silently
    discards `PagedCvValue` the same way it discards `GenericAck` and
    `Other`."""
    capabilities = make_capabilities(service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)
    bench.expect(cmd_service_direct_read(9), reply=ACK)
    bench.expect(POLL, reply=UNSUPPORTED)  # a poll may still answer 61 82
    bench.push(encode(0x63, 0x10, 9, 200))  # the real answer arrives unsolicited
    bench.expect(cmd_track_power_on(), reply=ACK)
    bench.expect(STATUS_REQUEST, reply=STATUS_POWER_ON)

    result = bench.station.programmer.service_read(9)

    assert result.value == 200
    assert result.encoding is CvEncoding.SERVICE_DIRECT
    assert bench.station.capabilities.service_direct_cv is False
```

- [ ] **Step 11: Run it and see it fail**

```bash
uv run pytest tests/station/test_cv_service_mode.py -k "service_read or paged_cv or timed_out or busy_state or unexpected_reply or short_circuit or no_ack or programming_timeout"
```

Expected: FAIL - `AttributeError: 'CvProgrammer' object has no attribute 'service_read'`, on all
`2 * 17 = 34` new test instances (seventeen test functions above, two `chunk_size` ids each).

- [ ] **Step 12: Implement `service_read` and its outcome mapping**

```python
    def service_read(self, cv: int) -> CvResult:
        """Read one CV over the programming track in service mode.

        Service mode is addressed by TRACK, not by locomotive: there is no
        `address` parameter here at all, and nothing in this method sends
        one. `Station.cv_read` still accepts an address for the POM path
        and warns when one is given alongside `mode=SERVICE`.

        Never retried automatically, unlike POM's three attempts: a service
        read already costs up to `TIMING.service_result` (95 s) and the
        command station retries the decoder handshake internally. One
        failing read here is exactly one telegram plus its polls.

        Takes no `page` keyword yet - Task 6 adds it, together with
        `ensure_page`'s call at the top of this method, in the step that
        implements `ensure_page` (1.5).
        """
        telegram, encoding, page_index = self.service_read_telegram(cv)
        power_before = self._station.status().track_power
        start = self._station.now()
        try:
            reply = self._station.exchange(
                telegram, timeout=self._station.timing.li_ack_programming
            )
            if isinstance(reply, Unsupported):
                raise UnsupportedCommandError(
                    f"the command station rejected the service-mode read opcode for CV{cv}"
                )
            matcher = CvMatcher(
                encoding,
                cv,
                page_index=page_index if encoding is CvEncoding.SERVICE_EXT else None,
            )
            outcome = self.await_result(
                matcher,
                timeout=self._station.timing.service_result,
                first_delay=self._station.timing.service_first_poll_delay,
                interval=self._station.timing.service_poll_interval,
                exchange_timeout=self._station.timing.li_ack_programming,
                allow_poll=True,
                ready_means_done=False,
                context="service",
            )
            return self._finish_service_read(cv, encoding, page_index, outcome, start)
        finally:
            self.exit_service_mode(restore_power=power_before)

    def _finish_service_read(
        self,
        cv: int,
        encoding: CvEncoding,
        page_index: int,
        outcome: object,
        start: float,
    ) -> CvResult:
        no_ack_hint = (
            "decoder did not acknowledge; sound decoders often fail on a 750 mA "
            "programming track - use POM instead"
        )
        if isinstance(outcome, CvValue):
            if encoding is CvEncoding.SERVICE_EXT and page_index in UNEXERCISED_BANDS:
                self._station.emit("cv.unexercised_band", {"cv": cv, "page": page_index})
            return CvResult(
                cv=cv,
                value=outcome.value,
                mode=ProgMode.SERVICE,
                encoding=encoding,
                operation="read",
                verified=None,
                elapsed=self._station.now() - start,
            )
        if isinstance(outcome, PagedCvValue):
            self._station.learn(service_direct_cv=False)
            if cv <= _REGISTER_COLLISION_MAX:
                raise DecoderNotRespondingError(
                    f"the station fell back to register mode for CV{cv}; register "
                    f"numbers 1-8 are indistinguishable from these CV numbers, so "
                    f"the value is not usable",
                    cv=cv,
                )
            if outcome.raw_register in echo_candidates(CvEncoding.SERVICE_DIRECT, cv):
                return CvResult(
                    cv=cv,
                    value=outcome.value,
                    mode=ProgMode.SERVICE,
                    encoding=CvEncoding.SERVICE_DIRECT,
                    operation="read",
                    verified=None,
                    elapsed=self._station.now() - start,
                )
            raise DecoderNotRespondingError(
                f"the station fell back to register mode for CV{cv} and register "
                f"{outcome.raw_register} does not match",
                cv=cv,
            )
        if isinstance(outcome, NoAck):
            raise DecoderNoAckError(
                f"CV{cv}: no acknowledgement from the decoder (61 13)",
                hint=no_ack_hint,
                cv=cv,
            )
        if isinstance(outcome, ShortCircuit):
            raise ShortCircuitError(
                f"short circuit on the programming track reading CV{cv}", cv=cv
            )
        if isinstance(outcome, Busy):
            raise StationBusyError(
                f"a programming operation was already running; CV{cv} read did not start",
                cv=cv,
            )
        if isinstance(outcome, TimedOut):
            if outcome.saw_no_ack:
                raise DecoderNoAckError(
                    f"CV{cv}: no acknowledgement from the decoder (61 13)",
                    hint=no_ack_hint,
                    cv=cv,
                )
            raise DecoderNotRespondingError(
                f"no result arrived for CV{cv} within {self._station.timing.service_result} s",
                cv=cv,
            )
        raise DecoderNotRespondingError(
            f"unexpected reply reading CV{cv} in service mode: {outcome!r}", cv=cv
        )
```

`_finish_service_read`'s only new learned fact goes through `self._station.learn(service_direct_cv=False)`
(2.18) - never a rebind of `self.capabilities`, which does not exist on `CvProgrammer` (1a). Add
`bench.station.capabilities.service_direct_cv is False` assertions after the call in the tests
above rather than `bench.station.programmer.capabilities...` - the point of `learn()` is that the
**station's** object changed, and every later read on this same `bench` sees it.

Add the module-level private constant to the constant block already started in Step 3, directly
under `UNEXERCISED_BANDS` - it is not new syntax, just placed where the other module constants live
rather than jammed between two import statements (2.12):

```python
_REGISTER_COLLISION_MAX: Final[int] = 8  # registers 1..8 collide with CV1..8
```

Add the remaining new imports to the *existing* import block at the top of `programming.py`
(alphabetically inside their group, not as a second `from railctl.errors import (...)` or a second
`from railctl.xbus.cv import (...)` - `ruff`'s `I001` merges same-module imports into one statement
and will reorder these on `ruff check --fix` if the ordering below is not exact):

```python
from railctl.errors import (
    DecoderNoAckError,
    DecoderNotRespondingError,
    ShortCircuitError,
    UnsupportedCommandError,
)
from railctl.xbus.cv import echo_candidates
from railctl.xbus.replies import Busy, CvValue, NoAck, PagedCvValue, ShortCircuit, Unsupported
```

- [ ] **Step 13: Run it and see it pass**

```bash
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: PASS - `62 passed` (`28` from Step 9 plus `2 * 17 = 34` new).

- [ ] **Step 14: Write the failing tests for `cv_read` and `Station.cv_read`**

Append to `tests/station/test_cv_service_mode.py`:

```python
def test_cv_read_uses_pom_when_the_resolved_mode_is_pom(bench_factory, monkeypatch):
    capabilities = make_capabilities(pom_read=True)
    bench = bench_factory(capabilities=capabilities)
    monkeypatch.setattr(
        bench.station.programmer, "pom_read", lambda cv, *, address: "POM-RESULT"
    )
    monkeypatch.setattr(
        bench.station.programmer, "service_read", lambda cv: pytest.fail("must not be reached")
    )

    assert bench.station.programmer.cv_read(8, address=3) == "POM-RESULT"


def test_cv_read_uses_service_mode_when_the_resolved_mode_is_service(bench_factory, monkeypatch):
    capabilities = make_capabilities(pom_read=False, service_direct_cv=True)
    bench = bench_factory(capabilities=capabilities)
    monkeypatch.setattr(
        bench.station.programmer,
        "pom_read",
        lambda cv, *, address: pytest.fail("must not be reached"),
    )
    monkeypatch.setattr(bench.station.programmer, "service_read", lambda cv: "SERVICE-RESULT")

    assert bench.station.programmer.cv_read(8) == "SERVICE-RESULT"


def test_station_cv_read_delegates_to_the_programmer(bench, monkeypatch):
    recorded: dict[str, object] = {}

    def fake_cv_read(self, cv, *, address=None, mode=ProgMode.AUTO, page=None):
        recorded.update(cv=cv, address=address, mode=mode, page=page)
        return "DELEGATED"

    monkeypatch.setattr(CvProgrammer, "cv_read", fake_cv_read)

    result = bench.station.cv_read(265, address=3, mode=ProgMode.SERVICE, page=(1, 2))

    assert result == "DELEGATED"
    assert recorded == {
        "cv": 265,
        "address": 3,
        "mode": ProgMode.SERVICE,
        "page": (1, 2),
    }
```

- [ ] **Step 15: Run it and see it fail**

```bash
uv run pytest tests/station/test_cv_service_mode.py -k "cv_read_uses or delegates"
```

Expected: FAIL - `AttributeError: 'CvProgrammer' object has no attribute 'cv_read'` on both
`chunk_size` instances of the first two test functions; `AttributeError: 'Station' object has no
attribute 'cv_read'` on both instances of the third.

- [ ] **Step 16: Implement `CvProgrammer.cv_read` and `Station.cv_read`**

In `src/railctl/station/programming.py`:

```python
    def cv_read(
        self,
        cv: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
    ) -> CvResult:
        """Read one CV, choosing POM or service mode through `resolve_mode`.

        `address` matters only on the POM path; service mode is addressed
        by track and never sends one. `page` is accepted for signature
        symmetry with the write side - selecting the CV257..512 index page
        is `ensure_page`'s job, added in Task 6, not this one.
        """
        resolved = resolve_mode(mode, self._station.capabilities, operation="read")
        if resolved is ProgMode.POM:
            return self.pom_read(cv, address=address)
        return self.service_read(cv)
```

`CvPage` needs importing if it is not already:

```python
from railctl.station.types import CvPage
```

In `src/railctl/station/facade.py`, inside `class Station`, next to the other CV methods (this is
the first thing this task adds to `facade.py`, so add the whole method plus whatever import of
`CvPage`/`CvResult`/`ProgMode` is missing):

```python
    def cv_read(
        self,
        cv: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
    ) -> CvResult:
        with self._lock:
            return self.programmer.cv_read(cv, address=address, mode=mode, page=page)
```

`self.programmer`, never `self._programmer` (1i) - Task 4 assigns it as a plain public attribute in
`Station.__init__`, and Task 6 and Task 12 read it by the same name.

- [ ] **Step 17: Run it and see it pass**

```bash
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: PASS - `68 passed` (`62` from Step 13 plus `2 * 3 = 6` new).

- [ ] **Step 18: Run the whole unit suite and check coverage**

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table shows `src/railctl/station/programming.py` and
`src/railctl/station/facade.py` both fully covered on the lines this task touched, `Required test
coverage of 90% reached.`, and `0 failed`. If any branch in `service_read_telegram`,
`_finish_service_read` or `exit_service_mode` shows up under `Missing`, the fix is a missing test
in this file, not a lowered gate - every `if`/`elif` added in Steps 3, 7 and 12 has a dedicated
test above that takes the opposite branch.

- [ ] **Step 19: Lint and format**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: `All checks passed!` from both. If `ruff check` reports `F401` on a duplicate
`StationBusyError` import, merge the two `from railctl.errors import (...)` blocks added in Steps 7
and 12 into one; if it reports `I001` on the import ordering added in Step 12, accept its own
reordering rather than hand-fixing it.

- [ ] **Step 20: Commit**

```bash
git add src/railctl/station/programming.py src/railctl/station/facade.py \
  tests/station/test_cv_service_mode.py
git commit -m "feat(station): add service-mode CV read with the encoding ladder and 63 10 handling"
```

---

### Task 6: Page-cache scaffolding, the shared write-and-confirm helper, index pages

Design specification: `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 744-758 (POM CV write),
801-812 (ZIMO indexed CVs), 759-786 (service mode, for the write-side mirror), 890-912 (`Timing`),
814-844 (`Capabilities`).

This is the first of three tasks that used to be one (`task-6`, `task-6b`, `task-6c`); together they
add CV writes, index-page selection and `cv_read_many` to `CvProgrammer`. This task builds the
page cache itself, the one piece of wire-level machinery every write path shares
(`_write_and_confirm`/`_raise_for_write_reply`/`raw_cv_write`), and `ensure_page`/`select_page`. Task
6b adds `pom_write`, the service write ladder and `service_write`, plus `cv_write`. Task 6c adds
`cv_read_many` and the facade delegations, then runs the whole file, the full suite, coverage, lint
and the layering guard once for all three tasks' work together.

**Files:**
- Create: `tests/station/test_cv_write.py`
- Modify: `src/railctl/station/programming.py` — new: `PageKey`, `reads_available`, the real body of
  `invalidate_pages` (Task 4 left it as a stub that only clears a placeholder dict),
  `_write_and_confirm`, `_raise_for_write_reply`, `raw_cv_write`, `ensure_page`, `select_page`.
  Also modifies two methods Tasks 4/5 already wrote: `pom_read` and `service_read` each gain a
  `page: CvPage | None = None` keyword and an `ensure_page(...)` call at the top (spec line 709);
  `cv_read` (Task 5) gains one line forwarding its own `page` argument to whichever of the two it
  calls.

**Interfaces:**

- Consumes:
  - `CvProgrammer(station)` with exactly one collaborator attribute, `self._station` — no
    `self.station`, no `self.capabilities`, no `self.timing`, no `self.clock`/`self.sleep`, no `ctx`
    of any kind. Capabilities are read fresh as `self._station.capabilities` at the top of every
    call; timing is `self._station.timing`; the clock is `self._station.now()`; the sleep is
    `self._station.pause(seconds)`; the threshold is `self._station.threshold`. `learn()` replaces
    `Capabilities` wholesale, so a cached copy from construction time would go stale the moment this
    same session learns something; a private rebind on a collaborator (rather than a call through
    `self._station`) could never reach `Station.close()`'s flush either. There is no
    `ProgrammerContext` protocol anywhere in this plan — Task 2 never defines one, and every
    CV-level operation reaches the wire and the event/cache machinery through the real `Station`
    Task 2 built: `exchange(telegram: bytes, *, timeout: float) -> Reply` (already parses and
    already raises the mapped exception — see the table below; never re-`parse()` its return value),
    `emit(name: str, payload: dict[str, object]) -> None` (one positional dict, never `**payload`),
    `invalidate_caches() -> None` (no parameters — it is a blanket clear, not a per-address one),
    `register_cache(clear: Callable[[], None]) -> None`, `now() -> float`, `pause(seconds: float) ->
    None`, `capabilities` (property), `timing` (property), `threshold` (property).
  - `Station.exchange`'s reply mapping, so this task never has to re-derive which parsed forms are
    exceptions and which are plain return values:

    | parsed reply | `station.exchange(...)` |
    |---|---|
    | `Unsupported` (`61 82`) | raises `UnsupportedCommandError` |
    | `Other` with reason in `{checksum, length}` | raises `ProtocolError` |
    | `Other` with reason in `{empty, unknown_form}` | raises `RailctlError` |
    | any `InterfaceStatus` | raises `ValueError` or `TransportError` |
    | `GenericAck`, `CvValue`, `PagedCvValue`, `Ready`, `Busy`, `NoAck`, `ShortCircuit`, `TrackShortCircuit`, `StationBusy`, `TransferError`, `StationStatus`, … | returned unchanged |

    Concretely: a write telegram that the station refuses with `61 82` never reaches this task's own
    code as an `Unsupported` instance to inspect — `station.exchange(...)` has already raised
    `UnsupportedCommandError` by the time control returns. `_raise_for_write_reply` (Step 2) only
    ever has to handle the forms that come back *unchanged* — `ShortCircuit`, `TrackShortCircuit`,
    `Busy`, `StationBusy` — none of the other rows in the table above are reachable inside it.
  - Tasks 1, 4, 5 (already on disk when this task starts): `ADDRESS_CVS`, `BLIND_WRITE_CVS`,
    `CV29_LONG_ADDRESS_BIT`, `PAGE_SELECTOR_CVS`, `INDEXED_CV_RANGE`, `CvSpec`, `CvReadOutcome`,
    `CvResult`, `CvPage = tuple[int, int]`, `ProgMode`, `Timing.pom_write_settle = 0.5`,
    `Timing.page_cache_ttl = 10.0`; `CvMatcher(encoding: CvEncoding, cv: int, *, zero_based: bool |
    None = None, page_index: int | None = None)` (`encoding` first, `cv` second — not the reverse);
    `resolve_mode(mode: ProgMode, capabilities: Capabilities, *, operation: Literal["read", "write"])
    -> ProgMode`; `CvProgrammer.await_result(self, matcher, *, timeout, first_delay, interval,
    exchange_timeout, allow_poll, ready_means_done, context: Literal["pom", "service"]) -> Reply |
    TimedOut`; `CvProgrammer.pom_read(self, cv: int, *, address: int | None = None) -> CvResult`
    (before this task adds `page`); `CvProgrammer.service_read(self, cv: int) -> CvResult` (before
    this task adds `page`); `CvProgrammer.service_read_telegram(self, cv: int) -> tuple[bytes,
    CvEncoding, int]`; `CvProgrammer.exit_service_mode(self, *, restore_power: bool) -> None`;
    `CvProgrammer.cv_read(self, cv: int, *, address: int | None = None, mode: ProgMode =
    ProgMode.AUTO, page: CvPage | None = None) -> CvResult` (before this task adds the forward);
    `SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] =
    (("z21_cv_opcodes", CvEncoding.Z21_16BIT), ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT))` — pairs of `(capability_field_name, CvEncoding)`,
    iterated as `for field_name, encoding in SERVICE_ENCODING_ORDER`, never as bare enums (Task 6b
    is the task that actually walks this tuple; it is listed here only because `test_reads_available`
    below reads the same three capability field names).
  - `bench` / `bench_factory` (`tests/station/conftest.py`, Task 2, per the addendum's §A.1 — the
    shape every station task from here on builds on). `Bench` is a plain class, not a dataclass; the
    constructor and the members this task uses, quoted from that file:
    ```python
    class Bench:
        def __init__(
            self,
            *,
            chunk_size: int | None = None,
            envelope_cls: type[LiUsbEnvelope] = LiUsbEnvelope,
            capabilities: Capabilities | None = None,
            default_address: int | None = BENCH_DEFAULT_ADDRESS,
            capabilities_path: Path | None = None,
            timing: Timing = TIMING,
            **capability_overrides: object,
        ) -> None: ...

        def expect(
            self,
            request: bytes,
            reply: bytes | tuple[bytes, ...] = b"",
            *,
            broadcast: bytes | tuple[bytes, ...] = (),
        ) -> Bench: ...

        def push(self, telegram: bytes) -> Bench: ...

        @property
        def sent(self) -> list[bytes]: ...
    ```
    `chunk_size` and `envelope_cls` are injected by `bench_factory` and never passed by a test.
    `BENCH_DEFAULT_ADDRESS` is `3`. `bench.station`, `bench.link`, `bench.transport`, `bench.clock`
    and `bench.events` are plain attributes this task reads directly. `bench.transport.written` holds
    FRAMED bytes — `Link.request` writes `envelope.wrap(telegram)` and `FakeTransport` appends exactly
    that — so no test in this task reads it; every assertion on what was sent reads `bench.sent`,
    which is bare telegrams in order, with the `open()` handshake already excluded. This task DOES
    read one other transport attribute: `bench.transport.script_pending`, the list of scripted
    exchanges not yet consumed (`src/railctl/transport/fake.py`). Five tests assert it is empty, which
    is how they prove a read that raised still consumed everything it was supposed to send — a leftover
    scripted exchange means the code under test gave up a step early. Every module under
    `tests/station/` gets its `Station` from `bench`/`bench_factory`, never from a hand-rolled double
    — this task does not define a `ProgrammerContext` stand-in, a `FakeStation`, or a `FakeCtx`. Where
    one test needs one collaborator method to behave a particular way in isolation, it monkeypatches
    that single method on `bench.station.programmer`, never the whole object. `bench_factory`'s
    default `capabilities` is `Capabilities.unknown("bench")`; a test that needs a `LEARNABLE_FIELDS`
    value (`pom_read`, `pom_result_channel`, `pom_echo_zero_based`, `service_direct_cv`) reaches it
    with `bench.station.learn(...)` after construction, and a test that needs any other field
    (`z21_cv_opcodes`, `service_direct_cv`, `service_ext_cv`, `loco_address_threshold`, …) either
    passes it as a `bench_factory(...)` keyword — absorbed by `**capability_overrides` and applied
    with `with_learned`, which accepts any real field name — or builds
    `Capabilities.unknown("bench").with_learned(...)` directly and passes it to
    `bench_factory(capabilities=...)`.
  - `railctl.xbus.commands`: `cmd_pom_write_byte(address: int, cv: int, value: int, *, threshold:
    int) -> bytes`, `cmd_station_status() -> bytes` — on disk, verified against
    `src/railctl/xbus/commands.py`.
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` (test-only, to build scripted
    replies without hand-computing XOR bytes).
  - `railctl.xbus.replies`: `parse(telegram: bytes) -> Reply` (used only by `Station.exchange`
    itself, never inside `programming.py`), the reply dataclasses `ShortCircuit`, `TrackShortCircuit`,
    `Busy`, `StationBusy`, `Reply` — on disk, verified against `src/railctl/xbus/replies.py`.
  - `railctl.errors`: `RailctlError`, `ProgrammingError(message: str, *, hint: str | None = None,
    cv: int | None = None)`, `CvVerifyError`, `IndexPageRequiredError`, `ShortCircuitError`,
    `StationBusyError` — all `ProgrammingError` subclasses, all on disk in `src/railctl/errors.py`.

- Produces (`src/railctl/station/programming.py`):

```python
@dataclass(frozen=True, slots=True)
class PageKey:
    address: int | None
    mode: ProgMode

class CvProgrammer:
    def reads_available(self, mode: ProgMode) -> bool: ...
    def invalidate_pages(self) -> None: ...              # replaces Task 4's stub
    def _raise_for_write_reply(self, reply: Reply, cv: int) -> None: ...
    def _write_and_confirm(self, cv: int, value: int, *, address: int | None,
                          mode: ProgMode) -> CvEncoding: ...
    def raw_cv_write(self, cv: int, value: int, *, address: int | None,
                     mode: ProgMode) -> None: ...        # bypasses ensure_page entirely
    def ensure_page(self, address: int | None, mode: ProgMode, cv: int,
                    page: CvPage | None) -> None: ...
    def select_page(self, page: CvPage, *, address: int | None = None,
                    mode: ProgMode = ProgMode.AUTO, force: bool = False) -> None: ...
```

Plus the two modifications to existing methods: `pom_read`/`service_read` each gain `page: CvPage |
None = None` and an `ensure_page(...)` call, and `cv_read` gains one line forwarding `page`.

**Layering note.** Every write telegram in this task is built through `xbus.commands`
(`cmd_pom_write_byte`); every CV number stays 1-based end to end. This task touches no `xbus.cv`
arithmetic at all — that is Task 6b's `_service_encoding_for`, not this one.

**A note on what this task assumes versus what it builds.** `CvProgrammer` and its constructor,
`CvMatcher`, `await_result`, `pom_read`, `service_read`, `service_read_telegram` and `cv_read`
already exist on disk when this task starts (Tasks 1-5 ran first). Every test below that exercises
a pre-existing collaborator does so through `monkeypatch.setattr` on `bench.station.programmer`,
never by re-executing that collaborator's real wire behaviour — those methods have their own tests
in Tasks 4/5, and re-deriving their exact exchange sequence here would make this task's tests fail
the moment an unrelated detail of someone else's implementation changed. What this task tests
directly is the code it writes, plus the two call sites it adds to `pom_read`/`service_read`/
`cv_read`.

---

- [ ] **Step 1: `reads_available` and the page-cache scaffolding**

Write the failing test first, in a new file:

```python
# tests/station/test_cv_write.py
from __future__ import annotations

import pytest

from railctl.errors import CvVerifyError, IndexPageRequiredError, UnsupportedCommandError
from railctl.station.capabilities import Capabilities
from railctl.station.types import CvResult, ProgMode
from railctl.xbus.codec import encode
from railctl.xbus.commands import cmd_pom_write_byte
from railctl.xbus.dialect import CvEncoding

ADDRESS = 3
THRESHOLD = 100

ACK = encode(0x01, 0x04)


def make_capabilities(**overrides: object) -> Capabilities:
    return Capabilities.unknown("bench").with_learned(**overrides)


def make_cv_result(
    *,
    cv: int = 0,
    value: int,
    mode: ProgMode = ProgMode.POM,
    encoding: CvEncoding = CvEncoding.POM_ZERO_BASED,
    operation: str = "read",
    verified: bool | None = None,
) -> CvResult:
    return CvResult(
        cv=cv, value=value, mode=mode, encoding=encoding,
        operation=operation, verified=verified, elapsed=0.0,
    )


def watch_invalidations(station) -> list[None]:
    """`Station.invalidate_caches()` takes no address and unconditionally calls every
    registered callback - a spy is the direct way to count calls without depending on
    what else those callbacks clear.
    """
    calls: list[None] = []
    station.register_cache(lambda: calls.append(None))
    return calls


# -- reads_available -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "overrides", "expected"),
    [
        (ProgMode.POM, {"pom_read": True}, True),
        (ProgMode.POM, {"pom_read": False}, False),
        (ProgMode.POM, {"pom_read": None}, False),
        (ProgMode.SERVICE, {"z21_cv_opcodes": True}, True),
        (ProgMode.SERVICE, {"service_direct_cv": True}, True),
        (ProgMode.SERVICE, {"service_ext_cv": True}, True),
        (ProgMode.SERVICE, {}, False),
    ],
)
def test_reads_available(mode, overrides, expected, bench_factory):
    bench = bench_factory(capabilities=make_capabilities(**overrides))
    assert bench.station.programmer.reads_available(mode) is expected
```

Run only the isolated test:

```bash
uv run pytest tests/station/test_cv_write.py -k reads_available
```

Expected: collection succeeds; every one of the 7 parametrized cases fails with
`AttributeError: 'CvProgrammer' object has no attribute 'reads_available'`.

Add the page-cache scaffolding and `reads_available` itself. `_pages` already exists from Task 4 as
`dict[object, object]`, holding nothing yet — this step gives it its real key/value types and adds
the verification-tracking set next to it:

```python
# src/railctl/station/programming.py - inside CvProgrammer.__init__, replacing Task 4's
# `self._pages: dict[object, object] = {}` line:
        self._pages: dict[PageKey, tuple[CvPage, float]] = {}
        self._verified_pages: set[PageKey] = set()

# new method on CvProgrammer:
    def reads_available(self, mode: ProgMode) -> bool:
        """Whether ANY read path is confirmed working for `mode`.

        Never `None`: an unprobed capability does not entitle a read attempt,
        the same rule Task 6b's write ladder uses. For POM this is
        deliberately narrower than "not False" - on THIS hardware `pom_read`
        is measured False (POM read returns nothing at all), so page
        selection over POM can never be verified here, and `select_page`
        (Step 3) must emit `page.unverified` rather than pretend to check.
        """
        caps = self._station.capabilities
        if mode is ProgMode.POM:
            return caps.pom_read is True
        return (
            caps.z21_cv_opcodes is True
            or caps.service_direct_cv is True
            or caps.service_ext_cv is True
        )
```

Extend Task 4's `invalidate_pages` stub to clear both:

```python
# src/railctl/station/programming.py - replaces Task 4's invalidate_pages body
    def invalidate_pages(self) -> None:
        """Registered with `station.register_cache` in Task 4's `Station.__init__` -
        that registration does not change here, only what this method clears.

        Takes no argument on purpose: the page cache is keyed by `(address, mode)`,
        but `power_off()`, `close()` and `exit_service_mode()` (which all call
        `station.invalidate_caches()`) mean "the track state is no longer
        trustworthy for ANYONE" - narrowing this clear to one address would buy
        nothing but a bug the day two locomotives share a session.
        """
        self._pages.clear()
        self._verified_pages.clear()
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k reads_available
```

Expected: `2 * 7 = 14 passed` (7 parametrized cases, each run once per `chunk_size` id — this file's
tests build a real `Station` through `bench`/`bench_factory`, so every test in it is parametrized by
`chunk_size` the same way every other `tests/station/` module is; `envelope_factory` has one member
today so it does not change the count).

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add the CV-write page-cache scaffolding"
```

- [ ] **Step 2: `_raise_for_write_reply`, `_write_and_confirm`, `raw_cv_write`**

This is the one piece of wire-level machinery every write path shares. Its POM branch is fully
exercised by this step's test; its SERVICE branch calls `self.service_write_telegram(...)` and
`self._track_power()`, which do not exist until Task 6b — that is fine, Python resolves the
attribute at call time, and no test in this task reaches that branch.

`_raise_for_write_reply` only ever sees a reply that `station.exchange(...)` already let through
unchanged. `Unsupported` is not one of those — `station.exchange` raises `UnsupportedCommandError`
for it before this method is ever called — so this method has no `Unsupported` branch to write; a
branch checking for a form the caller can never observe is dead code that would show up as an
uncovered line at this task's own coverage gate, at the end of Step 3.

```python
# tests/station/test_cv_write.py - append

def test_raw_cv_write_bypasses_ensure_page(bench, monkeypatch):
    programmer = bench.station.programmer

    def guard(*args, **kwargs):
        raise AssertionError("raw_cv_write must not call ensure_page")

    monkeypatch.setattr(programmer, "ensure_page", guard)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    programmer.raw_cv_write(31, 10, address=ADDRESS, mode=ProgMode.POM)
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k raw_cv_write
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'raw_cv_write'`.

```python
# src/railctl/station/programming.py - new methods on CvProgrammer

    def _raise_for_write_reply(self, reply: Reply, cv: int) -> None:
        """Maps the `61 xx` replies a write can get back, once `station.exchange`
        has already turned `61 82` into `UnsupportedCommandError` and every
        `InterfaceStatus`/damaged-reply case into its own exception. Everything
        that reaches this method is one of `station.exchange`'s pass-through
        forms - `GenericAck` (POM's `01 04 05`) and anything not named below
        fall through as accepted, exactly as the LI documentation says the
        generic ack means only "handed to the command station".
        """
        if isinstance(reply, ShortCircuit):
            raise ShortCircuitError(
                f"short circuit on the programming track writing CV{cv}", cv=cv
            )
        if isinstance(reply, TrackShortCircuit):
            raise ShortCircuitError(
                f"short circuit on the main track writing CV{cv}", cv=cv
            )
        if isinstance(reply, (Busy, StationBusy)):
            raise StationBusyError(f"station busy while writing CV{cv}", cv=cv)

    def _write_and_confirm(
        self, cv: int, value: int, *, address: int | None, mode: ProgMode
    ) -> CvEncoding:
        """Puts one CV write on the wire and confirms the station accepted it.

        POM: the interface ack IS the confirmation - there is no other channel
        (module docstring: "neither the PC nor the interface can determine
        whether a command reached the track"). Service (Task 6b): the same
        wait loop `service_read` uses, with `ready_means_done=True`, because
        after a WRITE `61 11` means the write finished, not "no result
        waiting" (spec line 780).

        Returns the encoding actually used, so a caller building a `CvResult`
        does not have to re-derive it.
        """
        if mode is ProgMode.POM:
            if address is None:
                raise ValueError("POM CV write needs a locomotive address")
            telegram = cmd_pom_write_byte(
                address, cv, value, threshold=self._station.threshold
            )
            reply = self._station.exchange(telegram, timeout=self._station.timing.li_ack_normal)
            self._raise_for_write_reply(reply, cv)
            self._station.pause(self._station.timing.pom_write_settle)
            return CvEncoding.POM_ZERO_BASED
        telegram, encoding, page_index = self.service_write_telegram(cv, value)
        power_before = self._track_power()
        try:
            reply = self._station.exchange(
                telegram, timeout=self._station.timing.li_ack_programming
            )
            self._raise_for_write_reply(reply, cv)
            matcher = CvMatcher(
                encoding,
                cv,
                page_index=page_index if encoding is CvEncoding.SERVICE_EXT else None,
            )
            outcome = self.await_result(
                matcher,
                timeout=self._station.timing.service_result,
                first_delay=self._station.timing.service_first_poll_delay,
                interval=self._station.timing.service_poll_interval,
                exchange_timeout=self._station.timing.li_ack_programming,
                allow_poll=True,
                ready_means_done=True,
                context="service",
            )
            if isinstance(outcome, NoAck):
                raise DecoderNoAckError(
                    f"CV{cv} service-mode write: decoder did not acknowledge",
                    hint=(
                        "decoder did not acknowledge; sound decoders often fail "
                        "on a 750 mA programming track - use POM instead"
                    ),
                    cv=cv,
                )
            if isinstance(outcome, ShortCircuit):
                raise ShortCircuitError(
                    f"short circuit on the programming track writing CV{cv}", cv=cv
                )
        finally:
            self.exit_service_mode(restore_power=power_before)
        return encoding

    def raw_cv_write(self, cv: int, value: int, *, address: int | None, mode: ProgMode) -> None:
        """CV31 and CV32 route through here, never through `ensure_page`.

        Both sit outside `INDEXED_CV_RANGE`, so even a buggy call back into
        `ensure_page` would return immediately rather than loop - but going
        through it at all would be backwards: this IS the mechanism
        `select_page` uses to change the page, not something `ensure_page`
        should gate.
        """
        self._write_and_confirm(cv, value, address=address, mode=mode)
```

`_write_and_confirm`'s SERVICE branch calls two Task 6b methods (`service_write_telegram`,
`_track_power`) and one Task 6b import (`DecoderNoAckError`) that do not exist until that task.
Python resolves attribute lookups and name lookups at call time, not at class-definition time, so
this compiles and the POM branch runs correctly now; `DecoderNoAckError` needs importing here
anyway since this method's source is already committed with the reference.

Add the imports this step needs:

```python
from railctl.errors import DecoderNoAckError, ShortCircuitError, StationBusyError
from railctl.xbus.replies import Busy, NoAck, Reply, ShortCircuit, StationBusy, TrackShortCircuit
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k raw_cv_write
```

Expected: `2 * 1 = 2 passed`.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add the shared write-and-confirm helper and raw_cv_write"
```

- [ ] **Step 3: `ensure_page`, `select_page`, and wiring `page` into the read paths**

```python
# tests/station/test_cv_write.py - append

def test_ensure_page_is_a_no_op_outside_the_indexed_range(bench):
    programmer = bench.station.programmer
    before = list(bench.sent)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 8, page=(9, 9))
    assert bench.sent == before


def test_ensure_page_requires_a_page_for_an_indexed_cv(bench):
    programmer = bench.station.programmer
    before = list(bench.sent)
    with pytest.raises(IndexPageRequiredError) as caught:
        programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=None)
    assert bench.sent == before
    assert caught.value.cv == 265


def test_ensure_page_cache_hit_sends_nothing(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    before = list(bench.sent)
    bench.clock.advance(5.0)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert bench.sent == before


def test_ensure_page_reselects_after_the_ttl_expires(bench):
    from railctl.station.timing import TIMING

    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    bench.clock.advance(TIMING.page_cache_ttl + 0.1)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert len(bench.sent) == 4


def test_select_page_verifies_the_first_time_when_reads_are_available(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(
            cv=cv, value={31: 10, 32: 2}[cv], mode=mode
        ),
    )
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert bench.events == []


def test_select_page_raises_cv_verify_error_when_the_page_did_not_stick(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=0, mode=mode),
    )
    with pytest.raises(CvVerifyError) as caught:
        programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert caught.value.cv == 31
    assert "did not stick" in str(caught.value)


def test_select_page_emits_unverified_when_reads_are_not_available(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert bench.events == [("page.unverified", {"page": (10, 2), "mode": ProgMode.POM})]


def test_select_page_force_reselects_even_within_the_ttl(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM, force=True)
    assert len(bench.sent) == 4


def test_select_page_invalidates_the_cache_when_the_page_write_fails(bench):
    """The `RailctlError` branch inside `select_page`'s own `raw_cv_write` pair -
    rule 12's "any `RailctlError` from a CV operation" clause. Decided unconditionally
    rather than left as a coverage-gap maybe: `select_page` writes CV31/CV32 through
    `raw_cv_write`, and a station that refuses one of those writes has left the page
    cache pointing at a page that was never actually selected.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    invalidated = watch_invalidations(bench.station)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=encode(0x61, 0x82))
    with pytest.raises(UnsupportedCommandError):
        programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert len(invalidated) == 1


def test_the_page_cache_is_cleared_by_the_shared_invalidation_hook(bench):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=False)
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.select_page((10, 2), address=ADDRESS, mode=ProgMode.POM)
    assert programmer._pages != {}
    bench.station.invalidate_caches()  # what power_off(), close() and exit_service_mode() all call
    assert programmer._pages == {}
    bench.expect(cmd_pom_write_byte(ADDRESS, 31, 10, threshold=THRESHOLD), reply=ACK)
    bench.expect(cmd_pom_write_byte(ADDRESS, 32, 2, threshold=THRESHOLD), reply=ACK)
    programmer.ensure_page(ADDRESS, ProgMode.POM, 265, page=(10, 2))
    assert len(bench.sent) == 4


# -- ensure_page wired into the read paths (spec line 709) ----------------------


def test_reading_an_indexed_cv_without_a_page_raises_before_any_telegram(bench):
    before = list(bench.sent)
    with pytest.raises(IndexPageRequiredError) as caught:
        bench.station.programmer.cv_read(265, address=ADDRESS, mode=ProgMode.POM, page=None)
    assert bench.sent == before
    assert caught.value.cv == 265


def test_reading_an_indexed_cv_with_a_page_selects_it_first(bench, monkeypatch):
    programmer = bench.station.programmer
    select_calls = []
    monkeypatch.setattr(
        programmer, "select_page",
        lambda page, *, address, mode, force: select_calls.append((page, address, mode, force)),
    )
    monkeypatch.setattr(
        programmer, "pom_read",
        lambda cv, *, address, page=None: make_cv_result(cv=cv, value=7),
    )
    result = programmer.cv_read(265, address=ADDRESS, mode=ProgMode.POM, page=(10, 2))
    assert select_calls == [((10, 2), ADDRESS, ProgMode.POM, False)]
    assert result.value == 7
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "ensure_page or select_page or invalidation_hook or indexed_cv"
```

Expected: `FAIL` — every `ensure_page`/`select_page` case with `AttributeError`, and the two
`indexed_cv` cases with a `TypeError` (`cv_read` does not accept a `page` argument yet — those two
still fail after this task's implementation lands until Step 3's `ensure_page` wiring below runs;
they are written first, house style, and turn green together with `ensure_page`/`select_page`).

```python
# src/railctl/station/programming.py - new methods on CvProgrammer

    def ensure_page(
        self, address: int | None, mode: ProgMode, cv: int, page: CvPage | None
    ) -> None:
        if cv not in INDEXED_CV_RANGE:
            return
        if page is None:
            raise IndexPageRequiredError(
                f"CV{cv} is behind a ZIMO index page (CV31/CV32); pass --page "
                f"or a CvSpec that carries one",
                cv=cv,
            )
        self.select_page(page, address=address, mode=mode, force=False)

    def select_page(
        self,
        page: CvPage,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        force: bool = False,
    ) -> None:
        resolved_mode = resolve_mode(mode, self._station.capabilities, operation="write")
        key = PageKey(address=address, mode=resolved_mode)
        cached = self._pages.get(key)
        now = self._station.now()
        if (
            not force
            and cached is not None
            and cached[0] == page
            and (now - cached[1]) < self._station.timing.page_cache_ttl
        ):
            return
        cv31, cv32 = page
        try:
            self.raw_cv_write(31, cv31, address=address, mode=resolved_mode)
            self.raw_cv_write(32, cv32, address=address, mode=resolved_mode)
        except RailctlError:
            self._station.invalidate_caches()
            raise
        first_selection = key not in self._verified_pages
        if first_selection:
            if self.reads_available(resolved_mode):
                read31 = self.cv_read(31, address=address, mode=resolved_mode)
                read32 = self.cv_read(32, address=address, mode=resolved_mode)
                if (read31.value, read32.value) != page:
                    raise CvVerifyError(
                        f"CV31/CV32 index page selection did not stick: wrote "
                        f"{page}, read back {(read31.value, read32.value)}",
                        cv=31,
                    )
                self._verified_pages.add(key)
            else:
                self._station.emit("page.unverified", {"page": page, "mode": resolved_mode})
        self._pages[key] = (page, now)
```

Wire `page` into `pom_read` and `service_read` (both already exist; add the keyword and the
`ensure_page` call at the top, after their existing preconditions and before the first telegram),
and forward `page` from `cv_read`:

```python
# src/railctl/station/programming.py - pom_read's signature and top gain one line each
    def pom_read(self, cv: int, *, address: int | None = None,
                page: CvPage | None = None) -> CvResult:
        resolved = self._station.resolve_address(address)
        # ... pom_read's existing preconditions (Task 4) stay exactly where they are ...
        self.ensure_page(resolved, ProgMode.POM, cv, page)
        # ... the rest of Task 4's pom_read body, unchanged ...

# service_read's signature and top gain one line each
    def service_read(self, cv: int, *, page: CvPage | None = None) -> CvResult:
        self.ensure_page(None, ProgMode.SERVICE, cv, page)
        # ... the rest of Task 5's service_read body, unchanged ...
```

`cv_read` (Task 5) already dispatches to `pom_read`/`service_read` based on `resolve_mode`; add the
one-line forward so its own `page` argument reaches whichever of the two it calls:

```python
# src/railctl/station/programming.py - cv_read, one line added at each of its two call sites:
# `self.pom_read(cv, address=resolved)` becomes
#   `self.pom_read(cv, address=resolved, page=page)`
# `self.service_read(cv)` becomes
#   `self.service_read(cv, page=page)`
```

Add `CvVerifyError`, `IndexPageRequiredError` and `RailctlError` to `programming.py`'s own
`railctl.errors` import (Task 4/5 need only `PomReadUnsupportedError`, `DecoderNoAckError`,
`DecoderNotRespondingError`, `ShortCircuitError`; `CvVerifyError` is new here, in `select_page`, and
`IndexPageRequiredError`/`RailctlError` are new in `ensure_page`/`select_page`'s own `except` clause),
and `INDEXED_CV_RANGE` to its `railctl.station.types` import (`ensure_page`'s own range check).

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "ensure_page or select_page or invalidation_hook or indexed_cv"
```

Expected: `2 * 11 = 22 passed` (11 test functions in this group: `ensure_page` no-op, `ensure_page`
requires a page, cache hit, TTL reselect, `select_page` verifies/raises/emits-unverified/force, the
failing-write invalidation test, the shared-hook test, and the two read-path tests).

```bash
uv run pytest tests/station/test_cv_write.py
```

Expected: `2 * (7 + 1 + 11) = 38 passed` — this task's whole file, so far: `reads_available` (Step 1),
`raw_cv_write` (Step 2), and this step's 11. Coverage, `ruff check`/`ruff format`, the layering guard
and the full suite are Task 6c's gates, run once for Tasks 6, 6b and 6c together (Part 3.1) — not
repeated here.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add ensure_page, select_page and index-page cache"
```

---

### Task 6b: `pom_write`, the service write ladder, `service_write`, `cv_write`

Design specification: `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 744-758 (POM CV write),
759-786 (service mode write), 890-912 (`Timing`), 810 (page-cache invalidation on any failing CV
operation).

Second of three tasks that used to be one `task-6`. Task 6 (run before this one) added the page
cache, `_write_and_confirm`/`_raise_for_write_reply`/`raw_cv_write`, and `ensure_page`/`select_page`.
This task adds `pom_write`, factors the service-mode write ladder to share its capability walk with
the read ladder Task 5 already wrote, adds `_track_power`/`service_write`, and adds `cv_write` — the
first method in this file that dispatches between the two modes. Task 6c (after this one) adds
`cv_read_many` and the facade delegations, then runs the whole file, the full suite, coverage, lint
and the layering guard once for Tasks 6, 6b and 6c together — do not run those gates yet.

**Files:**
- Modify: `src/railctl/station/programming.py` (`pom_write`, `_service_encoding_for`,
  `service_write_telegram`, refactor `service_read_telegram`, `_track_power`, `service_write`,
  `cv_write`; also re-wraps `cv_read`, from Task 5/6, so any `RailctlError` it raises also
  invalidates the page cache)
- Modify: `tests/station/test_cv_write.py` (append)

**Interfaces:**

- Consumes:
  - Everything Task 6 built in this same file: `PageKey`, `reads_available`, `invalidate_pages`,
    `_write_and_confirm`, `_raise_for_write_reply`, `raw_cv_write`, `ensure_page`, `select_page`, and
    `pom_read`/`service_read`/`cv_read` with their `page` keyword already wired in.
  - `CvProgrammer(station)`, `self._station` only — see Task 6's file for the full reasoning; this
    task never adds a second collaborator attribute.
  - `SERVICE_ENCODING_ORDER: Final[tuple[tuple[str, CvEncoding], ...]] =
    (("z21_cv_opcodes", CvEncoding.Z21_16BIT), ("service_direct_cv", CvEncoding.SERVICE_DIRECT),
    ("service_ext_cv", CvEncoding.SERVICE_EXT))` (Task 5) — pairs of `(capability_field_name,
    CvEncoding)`. `service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]` already
    exists (Task 5) with its own capability walk and two `CvOutOfRangeError` branches (one hinting
    `railctl doctor` when no encoding has ever been probed, one hinting `--mode pom` when the CV
    exceeds every available encoding's bound); this task factors that walk into a shared method and
    both branches move with it — Task 5's own tests
    (`test_service_read_telegram_with_no_encoding_probed_names_the_bound_and_suggests_doctor`,
    `test_service_read_telegram_never_sends_direct_opcode_above_cv255`) must keep passing unchanged
    against the shared walk, or the refactor has silently rewritten `service_read_telegram`'s
    contract.
  - `railctl.xbus.cv`: `CV_MIN = 1`, `MAX_CV_DIRECT = 255`, `MAX_CV_EXT = 1024`, `MAX_CV_Z21 = 1024`
    (on disk), `ext_cv_fields(cv: int) -> tuple[int, int]` (on disk, returns `(page_index, C)`).
  - `railctl.xbus.commands`: `cmd_service_direct_write`, `cmd_service_ext_write`,
    `cmd_z21_cv_write`, `cmd_station_status` — all `(cv: int, value: int) -> bytes` except
    `cmd_station_status() -> bytes` — on disk, verified against `src/railctl/xbus/commands.py`.
  - `railctl.xbus.replies`: `StationStatus` (with a `track_power: bool` field), `Ready`, `NoAck` — on
    disk.
  - `railctl.errors`: `PomReadUnsupportedError`, `DecoderNoAckError`, `ProtocolError`,
    `CvOutOfRangeError` — all on disk in `src/railctl/errors.py`.
  - `bench`/`bench_factory` exactly as Task 6's file states them; `z21_cv_opcodes`, `service_direct_cv`
    and `service_ext_cv` are not in `LEARNABLE_FIELDS`, so every test in this task that needs one of
    them builds `Capabilities.unknown("bench").with_learned(...)` and passes it to
    `bench_factory(capabilities=...)` — `bench.station.learn(...)` cannot set them.

- Produces (`src/railctl/station/programming.py`):

```python
class CvProgrammer:
    def pom_write(self, cv: int, value: int, *, address: int, verify: bool) -> CvResult: ...
    def _service_encoding_for(self, cv: int) -> tuple[CvEncoding, int]: ...
    def service_write_telegram(self, cv: int, value: int) -> tuple[bytes, CvEncoding, int]: ...
    def _track_power(self) -> bool: ...
    def service_write(self, cv: int, value: int, *, verify: bool) -> CvResult: ...
    def cv_write(self, cv: int, value: int, *, address: int | None = None,
                 mode: ProgMode = ProgMode.AUTO, page: CvPage | None = None,
                 verify: bool = True) -> CvResult: ...
```

Plus the modification to `service_read_telegram` (rewritten to call `_service_encoding_for`) and to
`cv_read` (wrapped so any `RailctlError` it raises also calls `self.invalidate_pages()`).

**Layering note.** `_service_encoding_for` calls `xbus.cv.ext_cv_fields(cv)` to get the extended-opcode
page index rather than computing `cv // 256` here — that division is not literally one of
`tests/test_layering.py`'s four banned patterns, but the project rule is "use the choke point", not
"dodge the regex", and this is the one place in this task that would have been tempted to inline it.

---

- [ ] **Step 0: confirm Task 6 is green**

```bash
uv run pytest tests/station/test_cv_write.py
```

Expected: `PASS` — this task adds to the same file; if it is not green before this task starts, stop
and fix Task 6 first.

- [ ] **Step 1: `pom_write`**

```python
# tests/station/test_cv_write.py - append to the existing import block:
from railctl.errors import DecoderNotRespondingError, PomReadUnsupportedError
from railctl.station.timing import TIMING
from railctl.station.types import ADDRESS_CVS, CV29_LONG_ADDRESS_BIT

UNSUPPORTED = encode(0x61, 0x82)


# -- pom_write: guards and verification --------------------------------------


def test_pom_write_refuses_before_sending_when_pom_read_is_known_false(bench):
    bench.station.learn(pom_read=False)
    before = list(bench.sent)
    with pytest.raises(PomReadUnsupportedError) as caught:
        bench.station.programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert bench.sent == before
    assert caught.value.hint == (
        "cannot verify POM writes on this station; re-run with `--no-verify` "
        "or use `--mode service`"
    )


def test_pom_write_probes_pom_read_first_when_unknown_and_refuses_if_that_fails(bench, monkeypatch):
    programmer = bench.station.programmer
    calls: list[tuple[int, int]] = []

    def fake_pom_read(cv, *, address, page=None):
        calls.append((cv, address))
        raise DecoderNotRespondingError("nothing came back", cv=cv)

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(PomReadUnsupportedError) as caught:
        programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert calls == [(5, ADDRESS)]
    assert caught.value.hint == (
        "cannot verify POM writes on this station; re-run with `--no-verify` "
        "or use `--mode service`"
    )


def test_pom_write_to_cv29_that_flips_bit_5_is_treated_as_blind(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    old_value = 0b00000010
    new_value = old_value | (1 << CV29_LONG_ADDRESS_BIT)
    monkeypatch.setattr(
        programmer, "pom_read",
        lambda cv, *, address, page=None: make_cv_result(cv=cv, value=old_value),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 29, new_value, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(29, new_value, address=ADDRESS, verify=True)
    assert result.verified is False
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert set(payload) == {"cv", "value", "reason"}


def test_pom_write_to_cv29_that_does_not_flip_bit_5_is_verified_normally(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    old_value = 0b00100010  # bit 5 (0x20) set
    new_value = 0b00101010  # bit 5 still set - only an unrelated bit changed
    monkeypatch.setattr(
        programmer, "pom_read",
        lambda cv, *, address, page=None: make_cv_result(cv=cv, value=old_value),
    )
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=new_value, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 29, new_value, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(29, new_value, address=ADDRESS, verify=True)
    assert result.verified is True


def test_pom_write_reread_once_on_mismatch_then_succeeds(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    reads = [make_cv_result(cv=5, value=99), make_cv_result(cv=5, value=10)]
    seen_at: list[float] = []

    def fake_cv_read(cv, *, address, mode, page=None):
        seen_at.append(bench.station.now())
        return reads.pop(0)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)
    result = programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert result.verified is True
    assert len(seen_at) == 2
    assert seen_at[1] - seen_at[0] == pytest.approx(TIMING.pom_write_settle)


def test_pom_write_raises_cv_verify_error_after_second_mismatch(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=99),
    )
    with pytest.raises(CvVerifyError) as caught:
        programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert caught.value.cv == 5


def test_pom_write_verified_is_false_while_a_read_stays_none(bench):
    """CV1 is in BLIND_WRITE_CVS: no verify read at all, so `verified` is `False`
    even though nothing ever read `None` back. Goes red if `pom_write` stops
    treating CV1 as blind - the scripted reply list has no second exchange for a
    verify read, so `bench` raises `AssertionError: the script is exhausted`
    instead of the assertion below even firing.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    bench.expect(cmd_pom_write_byte(ADDRESS, 1, 10, threshold=THRESHOLD), reply=ACK)
    write_result = programmer.pom_write(1, 10, address=ADDRESS, verify=True)
    assert write_result.verified is False


def test_pom_write_with_verify_false_is_blind(bench):
    programmer = bench.station.programmer
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    result = programmer.pom_write(5, 10, address=ADDRESS, verify=False)
    assert result.verified is False
    assert bench.events[-1][0] == "cv.write_unverified"


@pytest.mark.parametrize("cv", sorted(ADDRESS_CVS) + [8])
def test_pom_write_to_cv8_or_an_address_cv_invalidates_the_cache(cv, bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    invalidated = watch_invalidations(bench.station)
    monkeypatch.setattr(
        programmer, "pom_read", lambda c, *, address, page=None: make_cv_result(cv=c, value=0)
    )
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda c, *, address, mode, page=None: make_cv_result(cv=c, value=0, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, cv, 0, threshold=THRESHOLD), reply=ACK)
    programmer.pom_write(cv, 0, address=ADDRESS, verify=True)
    assert len(invalidated) == 1


def test_pom_write_to_an_unrelated_cv_does_not_invalidate_the_cache(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    invalidated = watch_invalidations(bench.station)
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: make_cv_result(cv=cv, value=10, mode=mode),
    )
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=ACK)
    programmer.pom_write(5, 10, address=ADDRESS, verify=True)
    assert invalidated == []


def test_pom_write_raises_unsupported_command_error_on_61_82(bench):
    programmer = bench.station.programmer
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        programmer.pom_write(5, 10, address=ADDRESS, verify=False)
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k pom_write
```

Expected: `FAIL` — collection succeeds, every one of these fails with
`AttributeError: 'CvProgrammer' object has no attribute 'pom_write'`.

```python
# src/railctl/station/programming.py - new methods on CvProgrammer

    @staticmethod
    def _blind_reason(cv: int, verify: bool) -> str:
        if not verify:
            return "verify=False: no read-back was attempted"
        if cv in BLIND_WRITE_CVS:
            return f"CV{cv} has no reliable read-back on this station"
        return (
            "CV29 bit 5 changes the answering address; a read-back would "
            "ask the wrong locomotive"
        )

    def pom_write(self, cv: int, value: int, *, address: int, verify: bool) -> CvResult:
        started = self._station.now()
        blind = not verify or cv in BLIND_WRITE_CVS
        old_value: int | None = None
        if verify and not blind:
            capabilities = self._station.capabilities
            if capabilities.pom_read is False:
                raise PomReadUnsupportedError(
                    f"CV{cv} POM write cannot be verified",
                    hint=(
                        "cannot verify POM writes on this station; re-run with "
                        "`--no-verify` or use `--mode service`"
                    ),
                    cv=cv,
                )
            # CV29 needs the pre-write value even when pom_read is already
            # known True, to detect a bit-5 (long/short address) flip; every
            # other CV only needs this read when pom_read is unestablished.
            if capabilities.pom_read is None or cv == 29:
                try:
                    old_value = self.pom_read(cv, address=address).value
                except RailctlError:
                    if self._station.capabilities.pom_read is True:
                        # A known-working capability failing once is a real
                        # fault (e.g. DecoderNotRespondingError), not grounds
                        # to claim POM verification is unsupported.
                        raise
                    raise PomReadUnsupportedError(
                        f"CV{cv} POM write cannot be verified",
                        hint=(
                            "cannot verify POM writes on this station; re-run "
                            "with `--no-verify` or use `--mode service`"
                        ),
                        cv=cv,
                    ) from None
            if cv == 29 and old_value is not None:
                if (old_value ^ value) & (1 << CV29_LONG_ADDRESS_BIT):
                    blind = True
        try:
            encoding = self._write_and_confirm(cv, value, address=address, mode=ProgMode.POM)
        except RailctlError:
            self._station.invalidate_caches()
            raise
        if cv == 8 or cv in ADDRESS_CVS:
            self._station.invalidate_caches()
        if blind:
            self._station.emit(
                "cv.write_unverified",
                {"cv": cv, "value": value, "reason": self._blind_reason(cv, verify)},
            )
            return CvResult(
                cv=cv, value=value, mode=ProgMode.POM, encoding=encoding,
                operation="write", verified=False, elapsed=self._station.now() - started,
            )
        read = self.cv_read(cv, address=address, mode=ProgMode.POM)
        if read.value != value:
            self._station.pause(self._station.timing.pom_write_settle)
            read = self.cv_read(cv, address=address, mode=ProgMode.POM)
            if read.value != value:
                raise CvVerifyError(
                    f"CV{cv} write verification failed twice: expected {value}, "
                    f"read back {read.value}",
                    cv=cv,
                )
        return CvResult(
            cv=cv, value=value, mode=ProgMode.POM, encoding=encoding,
            operation="write", verified=True, elapsed=self._station.now() - started,
        )
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k pom_write
```

Expected: `2 * 15 = 30 passed` (10 single-case functions plus the 5-case parametrized CV8/address
test).

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add pom_write with verification and blind-write handling"
```

- [ ] **Step 2: the shared service-mode encoding walk, and `service_write_telegram`**

The design is explicit that the write ladder must reuse the same walk the read ladder already does
(spec: "the service write ladder mirrors the read ladder exactly ... do not duplicate the ladder
logic; factor it"). `service_read_telegram` already exists from Task 5, with its own capability walk
and its two `CvOutOfRangeError` branches — one when no encoding has ever been probed (hint naming
`railctl doctor`), one when the CV exceeds every available encoding's own bound (hint naming
`--mode pom`). This step pulls that walk into a shared private method, keeping both branches intact,
and rewrites both telegram builders to call it, so a station that answers `23 11` for reads answers
`24 12` for writes through the identical gate.

```python
# tests/station/test_cv_write.py - append to the import block:
from railctl.errors import CvOutOfRangeError
from railctl.xbus.commands import cmd_service_direct_write, cmd_service_ext_write, cmd_z21_cv_write


# -- service_write_telegram: the write ladder mirrors the read ladder --------


def test_service_write_telegram_prefers_z21_when_available(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(z21_cv_opcodes=True, service_direct_cv=True)
    )
    telegram, encoding, page = bench.station.programmer.service_write_telegram(8, 145)
    assert telegram == cmd_z21_cv_write(8, 145)
    assert encoding is CvEncoding.Z21_16BIT
    assert page == 0


def test_service_write_telegram_falls_back_to_direct_for_low_cvs(bench_factory):
    bench = bench_factory(capabilities=make_capabilities(service_direct_cv=True))
    telegram, encoding, page = bench.station.programmer.service_write_telegram(8, 145)
    assert telegram == cmd_service_direct_write(8, 145)
    assert encoding is CvEncoding.SERVICE_DIRECT
    assert page == 0


def test_service_write_telegram_falls_back_to_extended_for_high_cvs(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=False, service_direct_cv=False, service_ext_cv=True
        )
    )
    telegram, encoding, page = bench.station.programmer.service_write_telegram(265, 7)
    assert telegram == cmd_service_ext_write(265, 7)
    assert encoding is CvEncoding.SERVICE_EXT
    assert page == 1


def test_service_write_telegram_raises_cv_out_of_range_when_nothing_is_available(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=False, service_direct_cv=False, service_ext_cv=False
        )
    )
    with pytest.raises(CvOutOfRangeError) as caught:
        bench.station.programmer.service_write_telegram(8, 1)
    assert caught.value.cv == 8


def test_service_write_telegram_never_uses_an_unprobed_capability(bench_factory):
    bench = bench_factory(
        capabilities=make_capabilities(
            z21_cv_opcodes=None, service_direct_cv=None, service_ext_cv=True
        )
    )
    _telegram, encoding, _page = bench.station.programmer.service_write_telegram(8, 1)
    assert encoding is CvEncoding.SERVICE_EXT
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k service_write_telegram
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'service_write_telegram'`.

```python
# src/railctl/station/programming.py - new private method on CvProgrammer

    def _service_encoding_for(self, cv: int) -> tuple[CvEncoding, int]:
        """First encoding `SERVICE_ENCODING_ORDER` allows for `cv`, with its page
        index (0 unless the encoding is `SERVICE_EXT`). Shared by
        `service_read_telegram` and `service_write_telegram`, so a station that
        answers `23 11` for reads answers `24 12` for writes through the
        identical gate.

        Every step requires its capability to be exactly `True` - `None` means
        "not established", and an unprobed station never sends an opcode that
        has not been observed to work.
        """
        caps = self._station.capabilities
        for field_name, encoding in SERVICE_ENCODING_ORDER:
            if getattr(caps, field_name) is not True:
                continue
            if encoding is CvEncoding.Z21_16BIT and cv <= MAX_CV_Z21:
                return encoding, 0
            if encoding is CvEncoding.SERVICE_DIRECT and cv <= MAX_CV_DIRECT:
                return encoding, 0
            if encoding is CvEncoding.SERVICE_EXT and cv <= MAX_CV_EXT:
                page_index, _c = ext_cv_fields(cv)
                return encoding, page_index
        if all(getattr(caps, field_name) is None for field_name, _ in SERVICE_ENCODING_ORDER):
            raise CvOutOfRangeError(
                f"CV{cv} is not reachable in service mode: no encoding has been "
                f"probed on this command station (Z21 covers CV{CV_MIN}..{MAX_CV_Z21}, "
                f"extended CV{CV_MIN}..{MAX_CV_EXT}, direct CV{CV_MIN}..{MAX_CV_DIRECT}, "
                f"all unknown)",
                hint="run `railctl doctor` to probe the service-mode encodings",
                cv=cv,
            )
        raise CvOutOfRangeError(
            f"CV{cv} is not reachable in service mode on this command station "
            f"(no extended or Z21 CV opcodes; direct opcodes only cover "
            f"CV{CV_MIN}..{MAX_CV_DIRECT})",
            hint="use `--mode pom`",
            cv=cv,
        )

    def service_write_telegram(self, cv: int, value: int) -> tuple[bytes, CvEncoding, int]:
        encoding, page_index = self._service_encoding_for(cv)
        if encoding is CvEncoding.Z21_16BIT:
            return cmd_z21_cv_write(cv, value), encoding, page_index
        if encoding is CvEncoding.SERVICE_DIRECT:
            return cmd_service_direct_write(cv, value), encoding, page_index
        return cmd_service_ext_write(cv, value), encoding, page_index
```

Replace the body of the existing `service_read_telegram` (Task 5) with the equivalent read-side walk
over the same helper — `cmd_service_direct_read`/`cmd_z21_cv_read`/`cmd_service_ext_read` are already
imported by Task 5:

```python
# src/railctl/station/programming.py - replaces Task 5's service_read_telegram body

    def service_read_telegram(self, cv: int) -> tuple[bytes, CvEncoding, int]:
        encoding, page_index = self._service_encoding_for(cv)
        if encoding is CvEncoding.Z21_16BIT:
            return cmd_z21_cv_read(cv), encoding, page_index
        if encoding is CvEncoding.SERVICE_DIRECT:
            return cmd_service_direct_read(cv), encoding, page_index
        return cmd_service_ext_read(cv), encoding, page_index
```

Add `cmd_service_direct_write`, `cmd_service_ext_write`, `cmd_z21_cv_write`, `MAX_CV_DIRECT`,
`MAX_CV_EXT`, `MAX_CV_Z21`, `CV_MIN` and `ext_cv_fields` to the imports (`MAX_CV_DIRECT`, `CV_MIN`
and `ext_cv_fields` are already imported by Task 5; add only the names Task 5 did not need).

Run the new ladder tests, then Task 5's existing read-ladder tests, to confirm the refactor changed
nothing observable:

```bash
uv run pytest tests/station/test_cv_write.py -k service_write_telegram
uv run pytest tests/station/test_cv_service_mode.py
```

Expected: `2 * 5 = 10 passed` for the first command. The second command reports whatever count Task
5 already established for that file — `0 failed` is what this refactor promises; any *change* in
that file's own count is a real regression from this step, not something this task's plan can
predict from here.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "refactor(station): share the service-mode encoding walk between read and write"
```

- [ ] **Step 3: `_track_power` and `service_write`**

```python
# tests/station/test_cv_write.py - append to the import block:
from railctl.errors import DecoderNoAckError
from railctl.xbus.commands import cmd_station_status

STATUS_POWERED = encode(0x62, 0x22, 0x00)  # HDR_STATUS, DB_STATUS, track_power bit clear -> True


# -- service_write ------------------------------------------------------------


def test_service_write_succeeds_when_the_wait_loop_reports_ready(bench_factory, monkeypatch):
    from railctl.xbus.replies import Ready

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: Ready())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda *, restore_power: None)
    result = programmer.service_write(8, 145, verify=True)
    assert result.verified is True
    assert result.mode is ProgMode.SERVICE


def test_service_write_raises_decoder_no_ack_when_the_wait_loop_reports_no_ack(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import NoAck

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: NoAck())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda *, restore_power: None)
    with pytest.raises(DecoderNoAckError):
        programmer.service_write(8, 145, verify=True)


def test_service_write_calls_exit_service_mode_and_invalidates_cache_on_failure(
    bench_factory, monkeypatch
):
    from railctl.xbus.replies import NoAck

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    invalidated = watch_invalidations(bench.station)
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    exit_calls: list[bool] = []
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: NoAck())
    monkeypatch.setattr(
        programmer, "exit_service_mode",
        lambda *, restore_power: exit_calls.append(restore_power),
    )
    with pytest.raises(DecoderNoAckError):
        programmer.service_write(8, 145, verify=True)
    assert exit_calls == [True]
    assert len(invalidated) == 1


def test_service_write_with_verify_false_is_blind(bench_factory, monkeypatch):
    from railctl.xbus.replies import Ready

    bench = bench_factory(capabilities=make_capabilities(z21_cv_opcodes=True))
    programmer = bench.station.programmer
    bench.expect(cmd_station_status(), reply=STATUS_POWERED)
    bench.expect(cmd_z21_cv_write(8, 145), reply=ACK)
    monkeypatch.setattr(programmer, "await_result", lambda *a, **k: Ready())
    monkeypatch.setattr(programmer, "exit_service_mode", lambda *, restore_power: None)
    result = programmer.service_write(8, 145, verify=False)
    assert result.verified is False
    name, payload = bench.events[-1]
    assert name == "cv.write_unverified"
    assert set(payload) == {"cv", "value", "reason"}
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "service_write and not telegram"
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'service_write'` (or
`'_track_power'`, whichever name Python's attribute lookup reports first).

```python
# src/railctl/station/programming.py - new methods on CvProgrammer

    def _track_power(self) -> bool:
        """Read `21 24 05` once, self-contained: `service_write` needs this
        BEFORE entering service mode (to know what to restore afterwards, spec
        line 782), and it is simple enough not to need `await_result`'s poll
        machinery - a status reply is immediate, never paged.
        """
        reply = self._station.exchange(
            cmd_station_status(), timeout=self._station.timing.li_ack_normal
        )
        if not isinstance(reply, StationStatus):
            raise ProtocolError(f"expected a station status reply, got {reply!r}")
        return reply.track_power

    def service_write(self, cv: int, value: int, *, verify: bool) -> CvResult:
        started = self._station.now()
        blind = not verify or cv in BLIND_WRITE_CVS
        try:
            encoding = self._write_and_confirm(cv, value, address=None, mode=ProgMode.SERVICE)
        except RailctlError:
            self._station.invalidate_caches()
            raise
        if cv == 8 or cv in ADDRESS_CVS:
            # Service mode ignores address, and the decoder on the programming
            # track is not necessarily the one on the main track (spec line
            # 782) - there is no address to narrow to, so the whole cache goes.
            self._station.invalidate_caches()
        if blind:
            self._station.emit(
                "cv.write_unverified",
                {"cv": cv, "value": value, "reason": self._blind_reason(cv, verify)},
            )
            return CvResult(
                cv=cv, value=value, mode=ProgMode.SERVICE, encoding=encoding,
                operation="write", verified=False, elapsed=self._station.now() - started,
            )
        return CvResult(
            cv=cv, value=value, mode=ProgMode.SERVICE, encoding=encoding,
            operation="write", verified=True, elapsed=self._station.now() - started,
        )
```

Add `cmd_station_status`, `StationStatus` and `ProtocolError` to the imports.

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "service_write and not telegram"
```

Expected: `2 * 4 = 8 passed`.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add service_write and the track-power precondition"
```

- [ ] **Step 4: `cv_write`, and the page-cache invalidation wrap on `cv_read`/`cv_write`**

Spec line 810 lists "any `RailctlError` from a CV operation" among what invalidates the page cache.
`pom_write`/`service_write` already call `self._station.invalidate_caches()` on failure, which
clears the page cache too (Task 4 registered `invalidate_pages` as one of `invalidate_caches`'s
callbacks) — but `cv_read` (Task 5) has no such wrap at all today, and a failing read currently
leaves a stale page selection in place. This step adds the direct, narrower
`self.invalidate_pages()` wrap to both `cv_read` and `cv_write`, so the guarantee holds even if the
indirect path through `invalidate_caches()` ever changes.

```python
# tests/station/test_cv_write.py - add to the existing import block:
from railctl.station.programming import PageKey


# -- cv_write: mode dispatch ------------------------------------------------------


def test_cv_write_dispatches_to_pom_when_capabilities_allow(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    calls: list[tuple[int, int, int, bool]] = []

    def fake_pom_write(cv, value, *, address, verify):
        calls.append((cv, value, address, verify))
        return make_cv_result(cv=cv, value=value, operation="write", verified=verify)

    monkeypatch.setattr(programmer, "pom_write", fake_pom_write)
    result = programmer.cv_write(5, 10, address=ADDRESS)
    assert calls == [(5, 10, ADDRESS, True)]
    assert result.value == 10


def test_cv_write_dispatches_to_service_when_pom_is_unavailable(bench_factory, monkeypatch):
    bench = bench_factory(capabilities=make_capabilities(pom_read=False, service_direct_cv=True))
    programmer = bench.station.programmer
    calls: list[tuple[int, int, bool]] = []

    def fake_service_write(cv, value, *, verify):
        calls.append((cv, value, verify))
        return make_cv_result(
            cv=cv, value=value, mode=ProgMode.SERVICE, operation="write", verified=verify
        )

    monkeypatch.setattr(programmer, "service_write", fake_service_write)
    programmer.cv_write(5, 10)
    assert calls == [(5, 10, True)]


def test_cv_write_requires_an_address_for_pom(bench):
    bench.station.learn(pom_read=True)
    with pytest.raises(ValueError):
        bench.station.programmer.cv_write(5, 10, address=None)


def test_cv_write_ensures_the_page_for_an_indexed_cv(bench, monkeypatch):
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    ensure_calls: list[tuple[int | None, ProgMode, int, tuple[int, int] | None]] = []
    monkeypatch.setattr(
        programmer, "ensure_page",
        lambda address, mode, cv, page: ensure_calls.append((address, mode, cv, page)),
    )
    monkeypatch.setattr(
        programmer, "pom_write",
        lambda cv, value, *, address, verify: make_cv_result(
            cv=cv, value=value, operation="write", verified=verify
        ),
    )
    programmer.cv_write(265, 7, address=ADDRESS, page=(10, 2))
    assert ensure_calls == [(ADDRESS, ProgMode.POM, 265, (10, 2))]


def test_cv_write_invalidates_the_page_cache_on_any_failure(bench):
    """`pom_write` already calls `station.invalidate_caches()` on failure, which
    clears the page cache indirectly (Task 4 registered `invalidate_pages` as one
    of its callbacks) - this pins the direct wrap on `cv_write` itself, which
    stays correct even if that indirect path ever changes. Goes red if
    `cv_write`'s own `try/except` is removed, since nothing else in this
    specific scenario would clear `_pages`.
    """
    programmer = bench.station.programmer
    bench.station.learn(pom_read=True)
    programmer._pages[PageKey(address=ADDRESS, mode=ProgMode.POM)] = ((0, 0), 0.0)
    bench.expect(cmd_pom_write_byte(ADDRESS, 5, 10, threshold=THRESHOLD), reply=UNSUPPORTED)
    with pytest.raises(UnsupportedCommandError):
        programmer.cv_write(5, 10, address=ADDRESS)
    assert programmer._pages == {}


def test_cv_read_invalidates_the_page_cache_on_any_failing_read(bench, monkeypatch):
    programmer = bench.station.programmer
    programmer._pages[PageKey(address=ADDRESS, mode=ProgMode.POM)] = ((0, 0), 0.0)

    def fake_pom_read(cv, *, address, page=None):
        raise DecoderNotRespondingError("no ack", cv=cv)

    monkeypatch.setattr(programmer, "pom_read", fake_pom_read)
    with pytest.raises(DecoderNotRespondingError):
        programmer.cv_read(8, address=ADDRESS, mode=ProgMode.POM)
    assert programmer._pages == {}
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "cv_write or cv_read_invalidates"
```

Expected: `FAIL` — the four dispatch tests with `AttributeError: 'CvProgrammer' object has no
attribute 'cv_write'`; the two invalidation tests currently pass by accident for `cv_write` and fail
for `cv_read` (`cv_read` exists from Task 5 but has no `invalidate_pages` wrap yet, so `_pages`
stays populated after the failing read) — after this step both must fail for the *stated* reason,
so run this once now to see the honest failure, then implement.

```python
# src/railctl/station/programming.py - new method on CvProgrammer

    def cv_write(
        self,
        cv: int,
        value: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
        verify: bool = True,
    ) -> CvResult:
        try:
            resolved_mode = resolve_mode(mode, self._station.capabilities, operation="write")
            if resolved_mode is ProgMode.POM:
                if address is None:
                    raise ValueError(
                        "POM CV write needs a locomotive address: pass --address "
                        "or set a default"
                    )
                self.ensure_page(address, resolved_mode, cv, page)
                return self.pom_write(cv, value, address=address, verify=verify)
            self.ensure_page(None, resolved_mode, cv, page)
            return self.service_write(cv, value, verify=verify)
        except RailctlError:
            self.invalidate_pages()
            raise
```

Wrap the existing `cv_read` (Task 5, already carrying Task 6's `page` forward) the same way: indent
every line of its current body one level under a `try:`, and add the matching `except` at the same
indentation as that `try:`:

```python
# src/railctl/station/programming.py - cv_read gains a wrapping try/except;
# every line of Task 5's existing dispatch body moves one indent level in,
# under this `try:`, and this `except` is appended at the end, unindented
# to match:
    def cv_read(
        self, cv: int, *, address: int | None = None,
        mode: ProgMode = ProgMode.AUTO, page: CvPage | None = None,
    ) -> CvResult:
        try:
            ...  # Task 5's existing dispatch body, plus Task 6's `page` forward - unchanged
        except RailctlError:
            self.invalidate_pages()
            raise
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k "cv_write or cv_read_invalidates"
```

Expected: `2 * 7 = 14 passed` (this step's 6 new functions, plus `raw_cv_write`, already green since
Task 6, which also matches the `cv_write` substring filter).

```bash
uv run pytest tests/station/test_cv_write.py
```

Expected: `2 * 49 = 98 passed` (Task 6's `19` plus this task's `pom_write` `15`, `service_write_telegram`
`5`, `service_write` `4`, `cv_write`-and-`cv_read` `6` — `19 + 15 + 5 + 4 + 6 = 49`). Coverage, `ruff
check`/`ruff format`, the layering guard and the full suite are Task 6c's gates, run once for Tasks
6, 6b and 6c together (Part 3.1) — not repeated here.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add cv_write and invalidate the page cache on any CV failure"
```

---

### Task 6c: `cv_read_many`, facade delegation, and the whole-plan-so-far gates

Design specification: `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 801-812 (ZIMO
indexed CVs / `cv_read_many`).

Third of three tasks that used to be one `task-6`. Tasks 6 and 6b (run before this one) built the
page cache, `_write_and_confirm`/`raw_cv_write`, `ensure_page`/`select_page`, `pom_write`, the
service write ladder, `service_write` and `cv_write`. This task adds the last new method,
`cv_read_many`, wires all three write-side methods into `Station` as thin delegations, then runs the
whole `test_cv_write.py` file, the full suite, coverage, lint and the layering guard once for Tasks
6, 6b and 6c's combined work.

**Files:**
- Modify: `src/railctl/station/programming.py` (`cv_read_many`)
- Modify: `src/railctl/station/facade.py` (add `cv_write`, `select_page`, `cv_read_many`)
- Modify: `tests/station/test_cv_write.py` (append)

**Interfaces:**

- Consumes:
  - Everything Tasks 6 and 6b added to `CvProgrammer` in this same file: `select_page`, `cv_read`
    (with its page-cache invalidation wrap), `PageKey`.
  - `Station.programmer` is a **public** attribute (`self.programmer`, never `self._programmer` —
    Tasks 5, 6 and 12 must not write the underscored name; the facade methods below read it as
    `self.programmer`, matching every other collaborator access in this file), assigned once in
    `Station.__init__` (Task 4) and never rebound.
  - `Station._lock: threading.RLock` — every public `Station` method, including the three this task
    adds, takes it (Task 2 must-pin 10).
  - `bench`/`bench_factory` exactly as Tasks 6/6b state them.

- Produces (`src/railctl/station/programming.py`):

```python
class CvProgrammer:
    def cv_read_many(self, specs: Sequence[CvSpec], *, address: int | None = None,
                     mode: ProgMode = ProgMode.AUTO,
                     on_progress: Callable[[int, int, CvReadOutcome], None] | None = None
                     ) -> list[CvReadOutcome]: ...
```

- Produces (`src/railctl/station/facade.py`): thin delegations with the same signatures as
  `cv_write`, `cv_read_many` and `select_page` above, substituting `self.default_address` for a
  `None` address, and reaching the collaborator as `self.programmer` — not `self._programmer`, which
  is not this class's attribute name (see the note above).

---

- [ ] **Step 0: confirm Task 6b is green**

```bash
uv run pytest tests/station/test_cv_write.py
```

Expected: `PASS` — this task adds to the same file; if it is not green before this task starts, stop
and fix Task 6b first.

- [ ] **Step 1: `cv_read_many`**

```python
# tests/station/test_cv_write.py - append


# -- cv_read_many -----------------------------------------------------------------


def test_cv_read_many_rejects_page_selector_cvs_in_the_payload(bench):
    with pytest.raises(ValueError, match="cursor"):
        bench.station.programmer.cv_read_many([CvSpec(cv=31)], address=ADDRESS, mode=ProgMode.POM)


def test_cv_read_many_selects_each_page_once_and_reads_in_sorted_order(bench, monkeypatch):
    programmer = bench.station.programmer
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        programmer, "select_page",
        lambda page, *, address, mode, force: calls.append(("select", page, force)),
    )
    monkeypatch.setattr(
        programmer, "cv_read",
        lambda cv, *, address, mode, page=None: (
            calls.append(("read", cv)),
            make_cv_result(cv=cv, value=cv, mode=mode),
        )[1],
    )
    specs = [
        CvSpec(cv=269, page=(10, 2)),
        CvSpec(cv=266, page=(10, 2)),
        CvSpec(cv=8),
        CvSpec(cv=265, page=(10, 2)),
        CvSpec(cv=267, page=(10, 2)),
        CvSpec(cv=268, page=(10, 2)),
    ]
    outcomes = programmer.cv_read_many(specs, address=ADDRESS, mode=ProgMode.POM)
    assert [o.spec.cv for o in outcomes] == [8, 265, 266, 267, 268, 269]
    assert calls == [
        ("read", 8),
        ("select", (10, 2), True),
        ("read", 265),
        ("read", 266),
        ("read", 267),
        ("read", 268),
        ("read", 269),
    ]


def test_cv_read_many_calls_on_progress_once_per_spec_and_captures_failures(bench, monkeypatch):
    programmer = bench.station.programmer

    def fake_cv_read(cv, *, address, mode, page=None):
        if cv == 6:
            raise DecoderNotRespondingError("no ack", cv=cv)
        return make_cv_result(cv=cv, value=cv, mode=mode)

    monkeypatch.setattr(programmer, "cv_read", fake_cv_read)
    progress: list[tuple[int, int, CvReadOutcome]] = []
    specs = [CvSpec(cv=5), CvSpec(cv=6), CvSpec(cv=7)]
    outcomes = programmer.cv_read_many(
        specs, address=ADDRESS, mode=ProgMode.POM, on_progress=progress.append
    )
    assert [o.error is None for o in outcomes] == [True, False, True]
    assert isinstance(outcomes[1].error, DecoderNotRespondingError)
    assert [(index, total) for index, total, _outcome in progress] == [(0, 3), (1, 3), (2, 3)]
```

Add `CvReadOutcome` and `CvSpec` to the `railctl.station.types` import (currently `CvResult,
ProgMode`; neither `test_cv_write.py` file section before this one constructs a `CvSpec`).

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k cv_read_many
```

Expected: `FAIL - AttributeError: 'CvProgrammer' object has no attribute 'cv_read_many'`.

```python
# src/railctl/station/programming.py - new method on CvProgrammer

    def cv_read_many(
        self,
        specs: Sequence[CvSpec],
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        on_progress: Callable[[int, int, CvReadOutcome], None] | None = None,
    ) -> list[CvReadOutcome]:
        for spec in specs:
            if spec.cv in PAGE_SELECTOR_CVS:
                raise ValueError(
                    f"CV{spec.cv} is a ZIMO page cursor (CV31/CV32), not a "
                    f"setting; cv_read_many refuses to read it as part of a "
                    f"payload"
                )
        ordered = sorted(specs, key=lambda spec: (spec.page or (0, 0), spec.cv))
        total = len(ordered)
        outcomes: list[CvReadOutcome] = []
        current_page: CvPage | None = None
        for index, spec in enumerate(ordered):
            try:
                if spec.page != current_page:
                    if spec.page is not None:
                        self.select_page(spec.page, address=address, mode=mode, force=True)
                    current_page = spec.page
                result = self.cv_read(spec.cv, address=address, mode=mode, page=spec.page)
                outcome = CvReadOutcome(spec=spec, result=result, error=None)
            except RailctlError as exc:
                outcome = CvReadOutcome(spec=spec, result=None, error=exc)
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress(index, total, outcome)
        return outcomes
```

Add `PAGE_SELECTOR_CVS` and `Sequence` (from `collections.abc`) to the imports if not already
present.

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k cv_read_many
```

Expected: `2 * 3 = 6 passed`.

```bash
git add src/railctl/station/programming.py tests/station/test_cv_write.py
git commit -m "feat(station): add cv_read_many"
```

- [ ] **Step 2: facade delegation**

```python
# tests/station/test_cv_write.py - append


# -- facade delegation --------------------------------------------------------------


class _ProgrammerStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def cv_write(self, cv, value, *, address, mode, page, verify):
        self.calls.append(("cv_write", cv, value, address, mode, page, verify))
        return make_cv_result(cv=cv, value=value, operation="write", verified=verify)

    def cv_read_many(self, specs, *, address, mode, on_progress):
        self.calls.append(("cv_read_many", tuple(specs), address, mode, on_progress))
        return []

    def select_page(self, page, *, address, mode, force):
        self.calls.append(("select_page", page, address, mode, force))


def test_facade_cv_write_substitutes_the_default_address_and_delegates(bench, monkeypatch):
    """`bench_factory`'s default `default_address` is `ADDRESS` (3); substituting
    the whole `programmer` collaborator on a real, already-open `bench.station`
    is how Task 2's own rule ("monkeypatch the single method on
    `bench.station.programmer`") extends to a test whose whole point is the
    facade method's own delegation, not any one `CvProgrammer` method - there is
    no smaller surface to patch here.
    """
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.cv_write(5, 10)
    assert stub.calls == [("cv_write", 5, 10, ADDRESS, ProgMode.AUTO, None, True)]


def test_facade_cv_write_keeps_an_explicit_address(bench, monkeypatch):
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.cv_write(5, 10, address=99)
    assert stub.calls == [("cv_write", 5, 10, 99, ProgMode.AUTO, None, True)]


def test_facade_select_page_and_cv_read_many_delegate(bench, monkeypatch):
    stub = _ProgrammerStub()
    monkeypatch.setattr(bench.station, "programmer", stub)
    bench.station.select_page((10, 2), force=True)
    bench.station.cv_read_many([CvSpec(cv=8)])
    assert stub.calls == [
        ("select_page", (10, 2), ADDRESS, ProgMode.AUTO, True),
        ("cv_read_many", (CvSpec(cv=8),), ADDRESS, ProgMode.AUTO, None),
    ]
```

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k facade
```

Expected: `FAIL - AttributeError: 'Station' object has no attribute 'cv_write'` (or `'select_page'`/
`'cv_read_many'`, whichever name Python's attribute lookup reports first).

```python
# src/railctl/station/facade.py - add to Station, alongside the existing cv_read.
# Reached as `self.programmer`, matching every other collaborator access on this
# class - never `self._programmer`, which is not this class's attribute name
# (Task 4 assigns the public `self.programmer` once, in `__init__`).

    def cv_write(
        self,
        cv: int,
        value: int,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        page: CvPage | None = None,
        verify: bool = True,
    ) -> CvResult:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            return self.programmer.cv_write(
                cv, value, address=resolved_address, mode=mode, page=page, verify=verify
            )

    def cv_read_many(
        self,
        specs: Sequence[CvSpec],
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        on_progress: Callable[[int, int, CvReadOutcome], None] | None = None,
    ) -> list[CvReadOutcome]:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            return self.programmer.cv_read_many(
                specs, address=resolved_address, mode=mode, on_progress=on_progress
            )

    def select_page(
        self,
        page: CvPage,
        *,
        address: int | None = None,
        mode: ProgMode = ProgMode.AUTO,
        force: bool = False,
    ) -> None:
        with self._lock:
            resolved_address = address if address is not None else self.default_address
            self.programmer.select_page(page, address=resolved_address, mode=mode, force=force)
```

Add `from collections.abc import Sequence` to `facade.py`'s imports if not already present —
`cv_read_many` is the first method on this class to take one.

Run:

```bash
uv run pytest tests/station/test_cv_write.py -k facade
```

Expected: `2 * 3 = 6 passed`.

```bash
git add src/railctl/station/facade.py tests/station/test_cv_write.py
git commit -m "feat(station): wire cv_write, cv_read_many and select_page onto Station"
```

- [ ] **Step 3: run the whole new test file**

```bash
uv run pytest tests/station/test_cv_write.py
```

Expected: `2 * 55 = 110 passed` — the running total across Tasks 6, 6b and 6c: `19` (Task 6:
`reads_available` 7 + `raw_cv_write` 1 + `ensure_page`/`select_page` group 11) `+ 30` (Task 6b:
`pom_write` 15 + `service_write_telegram` 5 + `service_write` 4 + `cv_write`-and-`cv_read` 6) `+ 6`
(Task 6c: `cv_read_many` 3 + facade 3) `= 55` logical cases, each run once per `chunk_size` id.

- [ ] **Step 4: run the full suite**

```bash
uv run pytest
```

Expected: `PASS`, `0 failed` — every test from M2 through M4 (920) plus every test Tasks 1-5 of this
plan added, plus this file's 110: `920 + Σ(tests added by Tasks 1-5) + 110`. Tasks 1-5 run before
this one and their own counts are not visible from here; a discrepancy against `920 + 110` alone is
not a regression, a *new* failure anywhere outside `tests/station/test_cv_write.py` is.

- [ ] **Step 5: coverage**

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table, `Required test coverage of 90% reached.` (the project's own
`fail_under = 90`, `pyproject.toml`), `0 failed`. If any line inside `pom_write`, `service_write`,
`_write_and_confirm`, `ensure_page`, `select_page` or `cv_read_many` shows up in the `Missing`
column, add the one test that would exercise it — per the plan's global rule, this group of three
tasks owns its own coverage gaps rather than handing them to Task 7.

- [ ] **Step 6: lint and format**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: `All checks passed!` for the first command, no diff reported by the second.

- [ ] **Step 7: layering guard**

```bash
uv run pytest tests/test_layering.py
```

Expected: `PASS`, `8 passed` (Task 2 Step 13 already establishes this file has eight test functions;
Tasks 6/6b/6c added no framing bytes, no port names and no CV arithmetic to `station/` —
`_service_encoding_for`'s `ext_cv_fields(cv)` call, Task 6b's own choke point, is the one place that
would have been tempted to inline `cv // 256`, and it does not).

- [ ] **Step 8: commit**

```bash
git add src/railctl/station/programming.py src/railctl/station/facade.py tests/station/test_cv_write.py
git commit -m "test(station): confirm CV writes, index pages and cv_read_many pass the full suite"
```

---

### Task 7: `Station.probe()` and the doctor report, part 1: doctor.py skeleton, D0-D3

This is the first of five files covering what was originally "Task 7": `task-7.md` (this file, D0-D3),
`task-7b.md` (D4), `task-7c.md` (D5-D8), `task-7d.md` (D9-D12), `task-7e.md` (the verdict block,
`Station.probe()`, package exports, and the final gate). Each later file starts by confirming this
one's tests are green and ends with its own commit. The full `run_probe`/`verdict_lines`/
`exit_code_for_report` contract does not exist until `task-7e.md` lands; this file only builds the
skeleton and the first four checks.

**Files:**
- Create: `src/railctl/station/doctor.py`, `tests/station/test_doctor.py`

**Interfaces:**

- Consumes, exactly as merged on disk (Task 1, `src/railctl/station/types.py` and
  `src/railctl/station/capabilities.py` - read both files before writing a line, not just this
  summary):
  - `railctl.station.types.Check` - `@dataclass(frozen=True, slots=True)`, fields `id: str`,
    `title: str`, `status: Literal["ok", "fail", "skip", "unknown"]`, `detail: str`
  - `railctl.station.types.DoctorReport` - `@dataclass(frozen=True, slots=True)`, fields
    `checks: tuple[Check, ...]`, `capabilities: Capabilities`; `@property ok -> bool` (`True` iff
    `D0`, `D1`, `D2` are all present and `status == "ok"`, **and** `D3` is either absent or not
    `"fail"`); `def check(self, check_id: str) -> Check | None`
  - `railctl.station.timing.{Timing, TIMING}` - **fifteen** fields as merged (Task 1), not
    fourteen; this file reads `li_ack_normal` (D3's power-on retry budget is `Station.power_on()`'s
    concern, not read here directly)
  - `railctl.xbus.replies.{StationVersion, StationStatus}` and the `Reply` union - all read from
    disk above; `parse` is never called directly here, only through `Station.exchange`
  - `railctl.xbus.commands.{cmd_station_version, cmd_station_status, cmd_track_power_on}` - all read
    from disk above
  - `railctl.station.facade.Station` (built across Tasks 1-6; the parts this file touches):
    `def exchange(self, telegram: bytes, *, timeout: float) -> Reply` - one `link.request` wrapped
    in `parse`, no retry logic of its own (that lives in `Link`); `def record(self, **updates:
    object) -> None` - `self._capabilities = self._capabilities.with_learned(**updates)`, in memory
    only, exactly why `run_probe` calls this and never `Capabilities.save` itself (persisting the
    file is the CLI's job in a later task); `def status(self) -> StationStatus` and `def version(self)
    -> StationVersion` (already in the Task 6 facade contract, `version()` cached, `status()` never
    cached); `def power_on(self) -> None` - cuts and re-reads with its own settle-and-reread dance
    (`TIMING.power_settle`), raising `TrackPowerError` on a disagreeing re-read; `link: Link` (**a
    plain attribute**, assigned in `__init__` - not a property, per Part 1.4 of the normalisation
    sheet - needed here only for `.drain()`); `capabilities: Capabilities` (already public); `threshold:
    int` (a read-only property: `self.capabilities.loco_address_threshold` when set, else
    `XPRESSNET.long_address_threshold`); `default_address: int | None` (already public per the
    facade's constructor)
  - `railctl.errors.RailctlError` - the base every check catches to turn a raised exception into a
    `"fail"` `Check` rather than letting `run_probe` itself blow up partway through
  - Task 2's `Bench` class and `bench` / `bench_factory` fixtures (`tests/station/conftest.py` -
    normalisation ADDENDUM.md §A.1 is the file in full; read it before writing a line, not just this
    summary). `Bench` wraps an open `Station` over a `FakeTransport` and speaks bare telegrams only:
    `.expect(request, reply=b"", *, broadcast=())` scripts one exchange, `.push(telegram)` queues an
    unsolicited broadcast, `.reply(telegram)` queues one solicited frame directly, `.sent` is every
    bare telegram written since `open()` with the handshake excluded, and `.unframe(framed) -> bytes`
    turns one framed request (what an `on_write` responder is handed) back into its bare telegram.
    `bench_factory` is a fixture depending on `chunk_size` and `envelope_factory`
    (`tests/conftest.py`) that returns a callable `make(**kwargs) -> Bench` building
    `Bench(chunk_size=chunk_size, envelope_cls=envelope_factory, **kwargs).open()`, so `bench =
    bench_factory()` is the zero-argument fixture and is already open. `Bench.__init__` also accepts
    `capabilities`, `default_address` (default `3`), `capabilities_path`, `timing` (default `TIMING`),
    and any real `Capabilities` field as a keyword, applied through `with_learned` (which raises
    `ValueError` naming a field that does not exist). `Bench` exposes `.station: Station`,
    `.transport: FakeTransport`, `.link: Link`, `.clock: FakeClock`, `.envelope: LiUsbEnvelope`.
    `open()` spends `Bench`'s own one `.expect()` call on the handshake, so by the time a test runs,
    `bench.transport`'s script queue is already empty and every further write falls through to
    `on_write` - the transport is "scriptless" only because that one handshake exchange has already
    been popped, not because it was ever built without a script. This file's tests attach their own
    `on_write` responder (below) rather than scripting more `.expect()` calls: `run_probe`'s exact
    exchange count and order across thirteen checks is Tasks 1-6's implementation detail, and a test
    built on the ordered-queue mode would fail the moment an unrelated internal refactor changed how
    many times `status()` is called, which is exactly the kind of test this project's own review
    criteria call out as coupled to the wrong thing.

- Produces so far (the full contract lands in `task-7e.md`; D4-D12 are still the `"skip"`
  placeholder loop this file's `run_probe` ends with):
```python
PROBE_CV: Final[int] = 8            # ZIMO manufacturer id, known constant 145
PROBE_CV_VALUE: Final[int] = 145
IDENTITY_CVS: Final[tuple[int, ...]] = (7, 8, 250, 1, 17, 18, 28, 29, 144)
RAILCOM_CVS: Final[tuple[int, int]] = (29, 28)
CHECK_IDS: Final[tuple[str, ...]] = ("D0", "D1", "D2", "D3", "D4", "D5", "D6",
                                     "D7", "D8", "D9", "D10", "D11", "D12")
CHECK_TITLES: Final[dict[str, str]]

def run_probe(station: "Station", *, address: int | None = None,
              allow_power_on: bool = False,
              use_programming_track: bool = True,
              now_utc: Callable[[], str] | None = None) -> DoctorReport: ...

def exit_code_for_report(report: DoctorReport) -> int: ...   # 0 when report.ok, else 3
```
`verdict_lines` is written as a four-empty-string placeholder in this file and gets its real body in
`task-7e.md`.

**Layering note.** `doctor.py` lives under `station/`, so `tests/test_layering.py` scans it under all
four rules the moment this file lands. Rule 1 (no framing bytes, no `tty`) rules out writing the
literal hex the design document quotes anywhere in this file, including a comment - name the opcode
instead (`cmd_track_power_on`, D3) and let the test pin the bytes.

**Decisions already made - do not re-open, do not contradict:**
- D0 does not re-open the link: `run_probe` receives an already-open `Station`, records
  `station.link.description` / `station.link.identity` in the check detail, and calls
  `station.link.drain()`.
- `probed_at` is set once, at the very end of `run_probe`, as an ISO-8601 UTC string with a trailing
  `Z`, through an injectable `now_utc: Callable[[], str] | None = None` keyword-only parameter -
  never a frozen global clock. `Station.probe` (added in `task-7e.md`) does not forward a `now_utc`
  parameter to its caller; it always lets `run_probe` use the real clock. Tests pass a fixed
  `now_utc` so the assertion on `capabilities.probed_at` is not a moving target.
- D3 turning the track on is `Station.power_on()`'s job, not a raw `station.exchange(cmd_track_power_on(),
  ...)` call re-implemented here: `power_on()` already performs the settle-and-reread dance and
  raises `TrackPowerError` on a disagreeing reply (spec line 692), so `_check_d3` only needs to catch
  it. There is no separate sleep call to make in this file.

---

- [ ] **Step 1: Write the failing D0/D1/D2 tests**

`tests/station/test_doctor.py` needs one shared piece of test infrastructure before any check can be
exercised: a responder that answers a `FakeTransport` running in scriptless mode. Every test in this
file builds on it, so it goes in first, alongside the three simplest checks.

```python
# tests/station/test_doctor.py
"""railctl doctor: checks D0-D12 and the verdict block.

Every scripted scenario here uses a `Responder` keyed by exact request bytes,
never `FakeTransport.expect()`'s ordered queue: `run_probe` sends a variable
number of exchanges depending on which capability path each check takes, and
pinning that count would break the moment an unrelated internal refactor
changed it. `Responder` distinguishes two kinds of answer: a PERSISTENT one
(the same reply every time a request recurs - `station.status()` is called
more than once in a single `run_probe`, and both calls must see the same
track-power bit unless a test explicitly changes it) and a QUEUED one (answer
this exact request once, then fall back to whatever is persistent or
default - later checks in this project send the identical `21 10 31` poll
telegram for several different CVs in a row, and each must get its own answer
in turn).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from railctl.station.capabilities import Capabilities
from railctl.station.doctor import (
    CHECK_IDS,
    CHECK_TITLES,
    PROBE_CV,
    PROBE_CV_VALUE,
    exit_code_for_report,
    run_probe,
    verdict_lines,
)
from railctl.station.facade import Station
from railctl.transport.fake import FakeTransport
from railctl.xbus.codec import encode
from railctl.xbus.commands import (
    cmd_station_status,
    cmd_station_version,
    cmd_track_power_on,
)
from railctl.xbus.dialect import XPRESSNET, Z21

if TYPE_CHECKING:
    from tests.station.conftest import Bench

VERSION_REPLY = encode(0x63, 0x21, 0x40, 0x12)  # XpressNet 4.0, station id 0x12 (Z21 family)
STATUS_REPLY_POWERED = encode(0x62, 0x22, 0x04)  # auto-start bit only: track_power True
STATUS_REPLY_UNPOWERED = encode(0x62, 0x22, 0x07)  # measured (docs/probe-results.md)
GENERIC_ACK = encode(0x01, 0x04)
UNSUPPORTED_REPLY = encode(0x61, 0x82)
NOACK_REPLY = encode(0x61, 0x13)


def cv_reply(ident: int, echo: int, value: int) -> bytes:
    """A `63 <ident> <echo> <value>` service-result telegram - the one reply
    shape every read opcode in this file answers through (cv.py, xbus/replies.py):
    POM, direct, extended and Z21 reads all come back via `63 14..17` on this
    hardware, never the alternate 6-byte Z21 form. Not exercised by D0-D3, but
    defined here since every later split of this test file imports it from
    here rather than redefining it."""
    return encode(0x63, ident, echo, value)


def loco_info_reply(*, busy: bool = False) -> bytes:
    ident = 0x08 if busy else 0x00  # bit 3 = in_use_by_other; high nibble stays 0
    return encode(0xE4, ident, 0, 0, 0)


class Responder:
    """`on_write` for a scriptless FakeTransport. See the module docstring.

    on_write is handed the FRAMED request, and every table in this class is keyed on bare
    telegrams, so unframing happens here, once. Keying the table on framed bytes instead would
    make every lookup miss and every probe answer GENERIC_ACK - a doctor that measures nothing
    and reports success.

    D4 through D8 all poll the identical `21 10` service-result telegram inside one run_probe, and
    the station answers a given CV on the same ident+echo bytes regardless of which opcode asked
    for it (`docs/probe-results.md`: CV8 always comes back on `63 14 08`, whether POM, a direct
    service read, or an extended service read asked) - so a queue keyed on the poll telegram
    alone cannot tell one check's poll from another's, and cannot even fall back to matching on
    the reply's own bytes, since a reply that would satisfy one check's matcher can equally
    satisfy a different check's matcher for the same CV. `queue_once_for` scopes a reply to the
    request that must immediately precede the poll it answers - tracked here as `_last_probe`,
    the most recent DIFFERENT request this responder has seen - so a reply queued for one check's
    poll stays invisible to every other check's.
    """

    def __init__(self, bench: Bench) -> None:
        self._bench = bench
        self._persistent: dict[bytes, bytes | None] = {
            cmd_station_version(): VERSION_REPLY,
            cmd_station_status(): STATUS_REPLY_POWERED,
        }
        self._queues: dict[bytes, deque[bytes | None]] = {}
        self._scoped_queues: dict[bytes, deque[bytes | None]] = {}
        self._previous_key: bytes = b""
        self._last_probe: bytes = b""

    def set(self, request: bytes, reply: bytes | None) -> None:
        """Override every future answer to `request`."""
        self._persistent[bytes(request)] = reply

    def queue_once(self, request: bytes, reply: bytes | None) -> None:
        """Answer the next occurrence of `request`, then fall through."""
        self._queues.setdefault(bytes(request), deque()).append(reply)

    def queue_once_for(self, probe: bytes, reply: bytes | None) -> None:
        """Answer the next poll that comes immediately after a write of `probe`, then fall
        through. `probe` is the check's own request - `cmd_z21_cv_read(1)`,
        `cmd_service_ext_read(257)`, `cmd_service_direct_read(29)` - never the shared `21 10`
        poll telegram itself, and never the reply's own bytes: this scopes by what the station
        was just SENT, which is unique per check, not by what a reply looks like, which is not.
        """
        self._scoped_queues.setdefault(bytes(probe), deque()).append(reply)

    def __call__(self, framed: bytes, transport: FakeTransport) -> None:
        key = self._bench.unframe(framed)
        if key != self._previous_key:
            self._last_probe = self._previous_key
        self._previous_key = key
        scoped = self._scoped_queues.get(self._last_probe)
        if scoped:
            reply = scoped.popleft()
        else:
            queue = self._queues.get(key)
            reply = queue.popleft() if queue else self._persistent.get(key, GENERIC_ACK)
        if reply is not None:
            self._bench.reply(reply)
```
A plain per-telegram queue is not enough because D4-D8 share one poll telegram AND, for a CV more
than one check probes (CV8 through POM/direct/extended alike), share the reply bytes too - so
neither the request nor the reply is unique per check, and only what was written immediately
before the poll (`_last_probe`) is.

```python
@pytest.fixture
def doctor_bench(bench_factory):
    """A `Bench` with no default address, built directly from `bench_factory` rather than from
    `bench`: `bench`'s own default (`BENCH_DEFAULT_ADDRESS = 3`, ADDENDUM.md part A.2) would make
    "no address given" untestable here, since `_resolved_address` falls back to
    `station.default_address` whenever a check's own `address=` argument is `None` -
    `test_d4_is_skipped_with_no_address_even_if_the_track_is_powered` (task-7b.md) and
    `test_d11_and_d12_are_skipped_with_no_address` (task-7d.md) both call `run_probe` with no
    `address=` and depend on that fallback being absent.

    One check reads the address a different way and is unaffected either way: `_check_d9`'s
    `_best_effort_read` (task-7d.md) reads `station.default_address` directly, never through
    `_resolved_address`, so with no default address it always skips its POM path and falls
    straight to `service_read` - which is what task-7d.md's D9 tests already assume, address or
    not.
    """
    bench = bench_factory(default_address=None)
    bench.transport.on_write = Responder(bench)
    return bench


def test_check_ids_are_thirteen_and_unique():
    assert len(CHECK_IDS) == 13
    assert len(set(CHECK_IDS)) == 13
    assert set(CHECK_TITLES) == set(CHECK_IDS)


def test_d0_records_link_description_and_identity_and_drains(doctor_bench):
    doctor_bench.push(encode(0x81, 0x00))
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d0 = report.check("D0")
    assert d0.status == "ok"
    assert doctor_bench.station.link.description in d0.detail
    assert doctor_bench.station.link.identity in d0.detail
    # The queued broadcast must be gone: drain() is what D0 promises.
    assert doctor_bench.link.poll(0.0) == []


def test_d1_records_xpressnet_version_and_station_id(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D1").status == "ok"
    assert report.capabilities.xpressnet_version == "4.0"
    assert report.capabilities.command_station_id == 0x12


def test_d2_decodes_the_status_bits(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d2 = report.check("D2")
    assert d2.status == "ok"
    assert "track power on" in d2.detail.lower() or "powered" in d2.detail.lower()
```

Run: `uv run pytest tests/station/test_doctor.py`
Expected output: `ModuleNotFoundError: No module named 'railctl.station.doctor'` - `doctor.py` does not
exist yet. `bench`/`bench_factory` come from Task 2's `tests/station/conftest.py`, already merged; if
that error instead reads `fixture 'bench' not found`, Task 2 has not landed on this branch yet and
must be merged first.

- [ ] **Step 2: Create `doctor.py` with the constants and D0-D2, and a `run_probe` stub for D3-D12**

```python
# src/railctl/station/doctor.py
"""`Station.probe()`: checks D0-D12 and the human verdict block.

Every check here is READ-ONLY against the decoder - see the design document,
"the doctor never writes a decoder CV" - and every service-mode check restores
the track power state it found before it ran. Three outcomes stay
distinguishable end to end: a capability is `True` (the station proved it),
`False` (the station said `61 82`, or - the one deliberate exception, D4 - the
POM read produced no result at all after three attempts and neither a value
nor `61 13` was ever seen), or `None` (nothing conclusive happened, or the
check did not run). No branch here ever writes `False` for any other reason.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final

from railctl.errors import RailctlError
from railctl.station.types import CV144, DECODER_TYPE_CV, Check, DoctorReport, decoder_family
from railctl.xbus.commands import cmd_station_status, cmd_station_version
from railctl.xbus.replies import StationStatus, StationVersion, Unsupported

if TYPE_CHECKING:
    from railctl.station.facade import Station

PROBE_CV: Final[int] = 8  # ZIMO manufacturer id, known constant 145
PROBE_CV_VALUE: Final[int] = 145
IDENTITY_CVS: Final[tuple[int, ...]] = (7, 8, 250, 1, 17, 18, 28, 29, 144)
RAILCOM_CVS: Final[tuple[int, int]] = (29, 28)

CHECK_IDS: Final[tuple[str, ...]] = (
    "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12",
)

CHECK_TITLES: Final[dict[str, str]] = {
    "D0": "link",
    "D1": "link alive",
    "D2": "station status",
    "D3": "track power",
    "D4": "POM read",
    "D5": "service direct read",
    "D6": "Z21 CV opcodes",
    "D7": "extended CV opcodes",
    "D8": "RailCom sanity",
    "D9": "decoder identity",
    "D10": "address band",
    "D11": "function groups 4/5",
    "D12": "single-function command",
}

_PLACEHOLDER_DETAIL: Final[str] = "not implemented yet"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolved_address(station: "Station", address: int | None) -> int | None:
    return address if address is not None else station.default_address


def _check_d0(station: "Station") -> Check:
    station.link.drain()
    detail = f"{station.link.description} ({station.link.identity})"
    return Check("D0", CHECK_TITLES["D0"], "ok", detail)


def _check_d1(station: "Station") -> Check:
    try:
        version: StationVersion = station.version()
    except RailctlError as exc:
        return Check("D1", CHECK_TITLES["D1"], "fail", str(exc))
    station.record(xpressnet_version=version.version, command_station_id=version.station_id)
    detail = f"XpressNet {version.version}, station id 0x{version.station_id:02X} ({version.family})"
    return Check("D1", CHECK_TITLES["D1"], "ok", detail)


def _check_d2(station: "Station") -> tuple[Check, StationStatus | None]:
    try:
        status = station.status()
    except RailctlError as exc:
        return Check("D2", CHECK_TITLES["D2"], "fail", str(exc)), None
    power = "track power on" if status.track_power else "track power off"
    detail = (
        f"{power}, emergency_stop={status.emergency_stop}, "
        f"auto_start_mode={status.auto_start_mode}, service_mode={status.service_mode}"
    )
    return Check("D2", CHECK_TITLES["D2"], "ok", detail), status


def _check_d3(
    station: "Station", status: StationStatus | None, *, allow_power_on: bool
) -> tuple[Check, bool]:
    if status is None:
        return Check("D3", CHECK_TITLES["D3"], "fail", "D2 did not produce a status"), False
    if status.track_power:
        return Check("D3", CHECK_TITLES["D3"], "ok", "track power already on"), True
    if not allow_power_on:
        detail = "track power is off; re-run with --power-on to verify D4 and D10"
        return Check("D3", CHECK_TITLES["D3"], "unknown", detail), False
    try:
        station.power_on()
    except RailctlError as exc:
        return Check("D3", CHECK_TITLES["D3"], "fail", str(exc)), False
    return Check("D3", CHECK_TITLES["D3"], "ok", "track power turned on"), True


def run_probe(
    station: "Station",
    *,
    address: int | None = None,
    allow_power_on: bool = False,
    use_programming_track: bool = True,
    now_utc: Callable[[], str] | None = None,
) -> DoctorReport:
    checks: list[Check] = [_check_d0(station), _check_d1(station)]
    d2_check, status = _check_d2(station)
    checks.append(d2_check)
    d3_check, track_powered = _check_d3(station, status, allow_power_on=allow_power_on)
    checks.append(d3_check)
    for check_id in CHECK_IDS[4:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities)


def verdict_lines(report: DoctorReport) -> list[str]:  # placeholder, task-7e implements it
    return ["", "", "", ""]


def exit_code_for_report(report: DoctorReport) -> int:
    return 0 if report.ok else 3
```

`station.power_on()` already performs the settle-and-reread dance the design document describes
(spec line 692) and raises `TrackPowerError` (a `RailctlError` subclass) on a disagreeing reply, so
`_check_d3`'s `except RailctlError` catches it without any extra work in this file. `track_powered`
above is unused by this file's stub loop, but is read starting in `task-7b.md`.

- [ ] **Step 3: Run the D0-D2 tests**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `7 passed` (`test_check_ids_are_thirteen_and_unique`, plus `test_d0_...`, `test_d1_...` and
`test_d2_...` each counted twice - every one of the last three takes `doctor_bench`, which is built
from `bench_factory(chunk_size)` and therefore runs once per `chunk_size` id (`whole-frame`,
`byte-at-a-time`): `2 * 3 + 1 = 7`.

- [ ] **Step 4: Write the failing D3 tests**

```python
def test_d3_reports_ok_when_track_already_powered(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D3").status == "ok"


def test_d3_unpowered_without_power_on_is_unknown_not_a_failure(doctor_bench):
    """Pinned: an unpowered bench with no --power-on must not read as a link
    failure. report.ok stays True and the exit code stays 0 - this is the
    ordinary state of a bench with the layout switched off, not a defect."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D3").status == "unknown"
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_d3_powers_on_when_allowed_and_the_reread_confirms_it(doctor_bench):
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    doctor_bench.transport.on_write.set(cmd_track_power_on(), encode(0x61, 0x01))
    report = run_probe(
        doctor_bench.station, allow_power_on=True, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.check("D3").status == "ok"
    assert "turned on" in report.check("D3").detail
```

`cmd_track_power_on` needs importing in the test file too - add it to the `railctl.xbus.commands`
import list from Step 1 (already listed there above).

Run: `uv run pytest tests/station/test_doctor.py -k test_d3`
Expected: `6 passed` - all three tests pass immediately, 2 `chunk_size` ids each. This is not a
red-then-green step: `_check_d3` already calls `station.power_on()` in its final, correct form
(Step 2), and `power_on()` reads the solicited `61 01`/`61 00` reply to `cmd_track_power_on()`
directly (spec line 692, "`61 01` means on... No unconditional status round trip") rather than
always re-reading `status()` - a disagreeing reply is the only thing that triggers the extra
`TIMING.power_settle` re-read, and `encode(0x61, 0x01)` here is not disagreeing. These three tests
exist to pin the behaviour, not to drive it: there is no wrong intermediate version of `_check_d3` to
correct in a later step, unlike the plan this file replaces, which shipped `station.exchange(...,
timeout=station.threshold and 0 or 0)` and a fictitious `station.clock_sleep_for_power_settle()`
call as knowingly-wrong placeholders and asked the implementer to fix them one step later. A plan
that commits a wrong line on purpose teaches the implementer to skim; this file ships `_check_d3`
correct the first and only time it is written.

- [ ] **Step 5: Run the whole file and commit the D0-D3 milestone**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `13 passed` - `2 * 6` bench-parametrised tests (`d0`, `d1`, `d2`, and the three `d3` tests)
`+ 1` plain test (`test_check_ids_are_thirteen_and_unique`, which takes no fixture and therefore
does not double).

```bash
uv run ruff check .
uv run ruff format --check .
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor checks D0-D3"
```

This closes the D0-D3 slice. `task-7b.md` continues from here with D4 (POM read).

---

### Task 7b: the doctor report, part 2: D4 (POM read, all four outcomes)

Continues `task-7.md`. `doctor.py` and `tests/station/test_doctor.py` already exist with D0-D3
implemented and the responder/fixture infrastructure in place; this file only adds D4.

**Files:**
- Modify: `src/railctl/station/doctor.py`, `tests/station/test_doctor.py`

**Interfaces:**

- Consumes, in addition to `task-7.md`'s list:
  - `railctl.station.programming.CvProgrammer` (Tasks 4-6, merged into `src/railctl/station/facade.py`
    as the public, no-underscore `Station.programmer` attribute - **not** constructed here). The
    method this file calls: `def pom_read(self, cv: int, *, address: int | None = None, page:
    CvPage | None = None) -> CvResult: ...` - `cv` is the only positional argument; every call site
    is `station.programmer.pom_read(PROBE_CV, address=address)`, never `pom_read(address, cv)`. It
    raises `PomReadUnsupportedError` on `61 82` (recording `pom_read=False` itself before raising -
    the ordinary runtime-learning path spec line 844 describes), `DecoderNoAckError` when any
    attempt saw `61 13` (recording nothing), `DecoderNotRespondingError` when three attempts saw
    neither a value nor `61 13` (recording nothing either - the plain-`cv_read` behaviour this
    file's second test below requires D4 to override).
  - `railctl.errors.{DecoderNoAckError, DecoderNotRespondingError, PomReadUnsupportedError}` - all
    `ProgrammingError` subclasses, all already on disk in `src/railctl/errors.py`.
  - `railctl.xbus.commands.cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes` and
    `cmd_service_result_request() -> bytes` - both already on disk.

- Produces so far (unchanged from `task-7.md` except that D4 is now real rather than a stub entry in
  the `CHECK_IDS[4:]` loop):
```python
def run_probe(station: "Station", *, address: int | None = None,
              allow_power_on: bool = False,
              use_programming_track: bool = True,
              now_utc: Callable[[], str] | None = None) -> DoctorReport: ...
```

**Decisions already made - do not re-open, do not contradict:**
- D4 needs the **main** track powered and a resolvable address; when either is missing it does not
  run at all. The two "did not run" reasons are reported differently, matching spec line 855
  ("if off without the flag, D4 and D10 are skipped as `unknown`"): the track being off without
  `--power-on` is `"unknown"` (a fact that genuinely could not be established), while having no
  address at all is `"skip"` (an opt-out - there was nothing to probe against in the first place).
- **D4 silence sets `pom_read = False` and `pom_result_channel = "none"`, together with a note
  saying the conclusion came from silence.** This is the one place `False` may be written without a
  `61 82`, and the note is the price: leaving it `None` would make every later `AUTO` operation retry
  POM for several seconds, forever. The same silence during a plain `cv_read` (Task 4) leaves the
  capability `None` - that is a deliberate difference, not an inconsistency, because only the doctor
  can afford to spend the time proving silence is really silence.
- `61 13` from D4 keeps `pom_read` at `None`, not `False`: a misconfigured decoder's RailCom must not
  permanently poison the capabilities file.

---

- [ ] **Step 1: Confirm `task-7.md` is green**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `13 passed` (`task-7.md`'s own final count: `2 * 6 + 1`). If this is not green, stop and
fix `task-7.md`'s slice before adding anything here.

- [ ] **Step 2: Write the failing D4 tests (POM read, all four outcomes)**

```python
from railctl.errors import DecoderNoAckError, DecoderNotRespondingError, PomReadUnsupportedError


def test_d4_success_records_pom_read_true_and_the_echo_convention(doctor_bench):
    """CV8 echoed as 7 (zero-based) fixes pom_echo_zero_based True."""
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 7, PROBE_CV_VALUE)
    )
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    d4 = report.check("D4")
    assert d4.status == "ok"
    assert report.capabilities.pom_read is True
    assert report.capabilities.pom_echo_zero_based is True
    assert "145" in d4.detail
    assert "expected" not in d4.detail  # value matched - no mismatch note


def test_d4_success_with_one_based_echo_and_a_mismatched_value_adds_a_note(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 8, 3)
    )
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.pom_read is True
    assert report.capabilities.pom_echo_zero_based is False
    d4 = report.check("D4")
    assert d4.status == "ok"
    assert "3" in d4.detail and "145" in d4.detail  # a note, not a silent pass


def test_d4_unsupported_sets_pom_read_false_with_no_silence_note(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, UNSUPPORTED_REPLY)
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.pom_read is False
    assert report.capabilities.notes == ()
    assert report.check("D4").status == "ok"


def test_d4_noack_keeps_pom_read_unknown_and_points_at_railcom(doctor_bench):
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    for _ in range(3):  # TIMING.pom_read_attempts
        doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), NOACK_REPLY)
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.pom_read is None
    d4 = report.check("D4")
    assert d4.status == "unknown"
    assert "railcom" in d4.detail.lower()


def test_d4_total_silence_sets_pom_read_false_with_a_silence_note(doctor_bench):
    """Pinned: this is the ONE place False is written without a 61 82, and the
    note naming it is the price. A plain cv_read hitting the same silence
    (Task 4/5's own tests) must leave pom_read at None - only the doctor makes
    this call, because AUTO would otherwise retry POM for 6s forever."""
    telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(telegram, GENERIC_ACK)
    # No queued reply for 21 10 31 at all: every poll gets the persistent
    # default (a generic ack, never a value or a 61 13) until the budget runs out.
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.pom_read is False
    assert report.capabilities.pom_result_channel == "none"
    d4 = report.check("D4")
    note = next(n for n in report.capabilities.notes if "silence" in n.lower())
    assert "silence" in note.lower()
    assert "re-run" in note.lower() and "doctor" in note.lower()
    assert note in d4.detail


def test_d4_is_unknown_when_the_track_is_unpowered(doctor_bench):
    """D4 (like D10) is 'unknown', not 'skip', when the reason it did not run
    is that the track is off without --power-on: spec line 855, "D4 and D10
    are skipped as unknown". 'skip' is reserved for a genuine opt-out (no
    address given)."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.check("D4").status == "unknown"
    assert report.capabilities.pom_read is None


def test_d4_is_skipped_with_no_address_even_if_the_track_is_powered(doctor_bench):
    """Distinct from the case above: track power is fine, there is simply
    nothing to address a POM read at - that is an opt-out, 'skip'."""
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D4").status == "skip"
    assert report.capabilities.pom_read is None
```

`cmd_pom_read_byte` and `cmd_service_result_request` need importing in the test file - add them to
the `railctl.xbus.commands` import list from `task-7.md`'s Step 1.

Run: `uv run pytest tests/station/test_doctor.py -k test_d4`
Expected: `14 failed` (7 tests x 2 `chunk_size` ids) - `run_probe` still treats `D4` as an unconditional
`"skip"` stub for every scenario, including the two that now expect `"unknown"` and `"skip"` to
differ by reason rather than always reading `"skip"`.

- [ ] **Step 3: Implement D4 using `station.programmer.pom_read`**

```python
from railctl.errors import DecoderNoAckError, DecoderNotRespondingError, PomReadUnsupportedError
```

Add this import, then append after `_check_d3`:

```python
_SILENCE_NOTE: Final[str] = (
    "POM read produced no result at all (neither 61 13 nor 61 82) after "
    f"{TIMING.pom_read_attempts} attempts; recorded as unsupported from silence "
    "rather than left unknown, or every AUTO operation would retry POM for "
    "several seconds forever. Fix RailCom on the decoder and re-run the doctor."
)


def _check_d4(station: "Station", *, address: int) -> tuple[Check, bool]:
    try:
        result = station.programmer.pom_read(PROBE_CV, address=address)
    except PomReadUnsupportedError:
        return Check("D4", CHECK_TITLES["D4"], "ok", "POM read unsupported (61 82)"), False
    except DecoderNoAckError:
        detail = (
            "decoder answered 61 13 (no acknowledgement) on the operations track; "
            "check RailCom wiring/configuration on the decoder and re-run the doctor"
        )
        return Check("D4", CHECK_TITLES["D4"], "unknown", detail), True
    except DecoderNotRespondingError:
        station.record(pom_read=False, pom_result_channel="none")
        capabilities = station.capabilities.with_note(_SILENCE_NOTE)
        station.record(notes=capabilities.notes)
        return Check("D4", CHECK_TITLES["D4"], "ok", _SILENCE_NOTE), False
    if result.value == PROBE_CV_VALUE:
        detail = f"POM read confirmed (CV{PROBE_CV}={result.value})"
    else:
        detail = (
            f"POM read confirmed, but CV{PROBE_CV}={result.value}, expected the ZIMO "
            f"manufacturer id {PROBE_CV_VALUE} - verify this is a ZIMO decoder"
        )
    return Check("D4", CHECK_TITLES["D4"], "ok", detail), False
```

`_check_d4` returns a `(Check, bool)` pair from the start here, not a bare `Check`: the second value
(`d4_noack`) records whether this run's D4 specifically saw `61 13`, which `task-7c.md`'s D8 needs to
decide whether it may run at all. Writing the tuple return once, now, avoids reshaping every call
site a second time later.

Wire it into `run_probe`, replacing the `D4` entry the loop over `CHECK_IDS[4:]` used to produce -
splitting `"unknown"` (track off) from `"skip"` (no address) per spec line 855:

```python
def run_probe(
    station: "Station",
    *,
    address: int | None = None,
    allow_power_on: bool = False,
    use_programming_track: bool = True,
    now_utc: Callable[[], str] | None = None,
) -> DoctorReport:
    checks: list[Check] = [_check_d0(station), _check_d1(station)]
    d2_check, status = _check_d2(station)
    checks.append(d2_check)
    d3_check, track_powered = _check_d3(station, status, allow_power_on=allow_power_on)
    checks.append(d3_check)

    resolved_address = _resolved_address(station, address)
    d4_noack = False
    if track_powered and resolved_address is not None:
        d4_check, d4_noack = _check_d4(station, address=resolved_address)
    elif not track_powered:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "unknown", "track power is off; re-run with --power-on"
        )
    else:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "skip", "no locomotive address given; pass --address"
        )
    checks.append(d4_check)

    for check_id in CHECK_IDS[5:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
    clock = now_utc or _iso_utc_now
    station.record(probed_at=clock())
    return DoctorReport(checks=tuple(checks), capabilities=station.capabilities)
```

`TIMING` needs importing for `_SILENCE_NOTE`'s f-string - add `from railctl.station.timing import
TIMING` to `doctor.py`'s imports if `task-7.md` did not already need it (it did not: D0-D3 never
referenced `TIMING` directly, since D3 delegates its timing entirely to `station.power_on()`).

Run: `uv run pytest tests/station/test_doctor.py -k test_d4`
Expected: `14 passed` (7 tests x 2 `chunk_size` ids).

- [ ] **Step 4: Run the whole file and commit D4**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `27 passed` - `2 * (6 + 7) + 1`: `task-7.md`'s 6 bench-parametrised tests (`d0`, `d1`, `d2`,
three `d3` tests) plus this file's 7 bench-parametrised D4 tests, each doubled for the two
`chunk_size` ids, plus the 1 plain `test_check_ids_are_thirteen_and_unique` which does not double.
Treat this as a formula the first execution measures, not a fixed number: if the printed count
disagrees, a *different failing test* is the real signal, not a mismatched total.

```bash
uv run ruff check .
uv run ruff format --check .
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D4 (POM read)"
```

This closes the D4 slice. `task-7c.md` continues from here with the D5-D8 service-mode batch.

---

### Task 7c: the doctor report, part 3: D5-D8 (the service-mode batch, one `try/finally`)

Continues `task-7b.md`. `doctor.py` and `tests/station/test_doctor.py` already have D0-D4 implemented;
this file adds D5 (service direct read), D6 (Z21 CV opcode), D7 (extended CV opcodes) and D8
(RailCom sanity), all inside one service-mode session.

**Files:**
- Modify: `src/railctl/station/doctor.py`, `tests/station/test_doctor.py`

**Interfaces:**

- Consumes, in addition to `task-7b.md`'s list - all against `railctl.station.programming`, the only
  module Tasks 4-6 actually created (there is no `station/cv_programmer.py`, `station/service.py` or
  `station/wait.py`; a doctor.py that imports any of those three names is importing a module that
  does not exist):
  - `railctl.station.programming.CvMatcher` - a **plain class**, not a frozen dataclass:
    `__init__(self, encoding: CvEncoding, cv: int, *, zero_based: bool | None = None, page_index:
    int | None = None)`, `__call__(self, reply: Reply) -> bool`, `value_of(self, reply: CvValue) ->
    int`, `echo_says_zero_based(self, reply: CvValue) -> bool | None`. Positional order is
    `(encoding, cv)`.
  - `Station.programmer.await_result` - a **method**, not a module-level function taking a
    `station` argument: `def await_result(self, matcher: CvMatcher, *, timeout: float, first_delay:
    float, interval: float, exchange_timeout: float, allow_poll: bool, ready_means_done: bool,
    context: Literal["pom", "service"]) -> Reply | TimedOut: ...`. `context` is a two-value literal,
    `"pom"` or `"service"` - **not** an arbitrary string like `"doctor"`; every probe in this file
    passes `context="service"`, because that is what it is.
  - `Station.programmer.exit_service_mode` - also a method: `def exit_service_mode(self, *,
    restore_power: bool) -> None`. This file calls it directly, exactly once, around the whole
    D5-D8 batch - never through `Station.programmer.service_read`, which enters and exits service
    mode once per call and would re-energise and re-cut the programming track four times in a row
    for no reason, and would make D8's "D5 just passed" precondition impossible to observe from
    inside a fresh call.
  - `railctl.xbus.commands.{cmd_service_direct_read, cmd_service_ext_read, cmd_z21_cv_read,
    cmd_service_result_request}` - all already on disk (M4).
  - `railctl.xbus.commands.{REQ_4_DATA, DB_Z21_WRITE}` - already-exported names in
    `railctl.xbus.commands` (M4): `REQ_4_DATA = 0x24`, `DB_Z21_WRITE = 0x12`, so `24 12` (the Z21
    *write* opcode this file's D6 test must never send) is `(REQ_4_DATA, DB_Z21_WRITE)`.
  - `railctl.xbus.cv.CvEncoding` (re-exported from `railctl.xbus.dialect`, where it is actually
    defined) - `SERVICE_DIRECT`, `Z21_16BIT`, `SERVICE_EXT`.
  - `railctl.xbus.replies.{CvValue, NoAck, Unsupported}` - already imported from `task-7.md`'s Step 1
    list except `CvValue` and `NoAck`, added here.
  - `railctl.xbus.replies.UNSUPPORTED` - the module-level singleton `Unsupported()` instance, already
    exported (M4, verified on disk: `src/railctl/xbus/replies.py` line 374). New in this file.
  - `railctl.errors.UnsupportedCommandError` - already on disk (M2): `Station.exchange` raises it on a
    `61 82` reply (`"the station answered 61 82: it understood, and it refuses"`). New in this file -
    neither `task-7.md` nor `task-7b.md` imports it, because `_check_d0`-`_check_d4` never call the
    helper below.

- Produces so far: unchanged shape from `task-7b.md`; D5-D8 are now real checks rather than stub
  entries.

**Layering note.** Rule 2 (no CV arithmetic under `station/`) matters most in this file: D7's high-band
probe CV is the literal `257`, a named constant documented as page 1's first CV - never written as
`256 * 1 + 1`. Nothing in this file's D5-D8 block calls `ext_cv_fields` or does band arithmetic of its
own; `CvMatcher` and `cmd_service_ext_read` already own that.

**Decisions already made - do not re-open, do not contradict:**
- Every service-mode check (D5-D8) restores the pre-batch track power state through **one**
  `station.programmer.exit_service_mode(restore_power=...)` call, inside **one** `try/finally` around
  the whole D5-D8 batch - not one `exit_service_mode` per check.
- Ordering trap: D4 needs the **main** track powered; D5-D8 need the **programming** track, which
  service mode reaches by cutting main power. `task-7b.md`'s D4 already ran to completion before this
  batch starts, so `power_before = station.status().track_power` captured here reflects the state D4
  left behind, not a track the doctor itself has already de-energised.
- **D5-D8 are recorded `unknown` (capability stays `None`), never `False`, under
  `--no-programming-track`.** No service-mode telegram is sent at all in that case - not even an
  entry attempt - and every one of D5-D8 reports `status="skip"` (an opt-out, spec-consistent with
  `--no-programming-track` being a flag the operator set on purpose) while the capability stays
  `None`.
- **D6 probes the read opcode `23 11` only, never `24 12`.** `23 11` has no meaning in classic
  XpressNet, so a station that lacks it answers `61 82` and nothing happens; probing `24 12` (the
  write opcode) could modify a CV.
- **D7 probes both a low and a high CV and sets `service_ext_cv = True` only when both succeed** - a
  station could accept the family and refuse pages above the first. The tri-state rule below is the
  corrected version of an earlier draft of this check that wrote `False` too eagerly; the corrected
  wording is authoritative.
- **D8 runs only when D4 specifically saw a `61 13` (`NoAck`) and D5 passed.** It does not run when
  D4 was silent (a different, unrelated capability judgment) or when D4 was unsupported or succeeded
  outright.

---

- [ ] **Step 1: Confirm `task-7b.md` is green**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `27 passed` (`task-7b.md`'s own final count: `2 * 13 + 1`). If this is not green, stop and
fix `task-7b.md`'s slice before adding anything here.

- [ ] **Step 2: Add `_exchange`, the shared service-probe helper, `_check_d5`, and wire the gated
  batch into `run_probe`**

The D5-through-D8 batch has one behaviour that has to exist before any single check's logic can be
tested meaningfully: it must run only under `use_programming_track=True`, and under `False` it must
not send a single service-mode telegram. That gate is written here, in production code, first -
there is no way to make a test for "no telegram was sent under the disabled flag" go red honestly
before this exists, since a stub that always skips D5-D8 would pass that assertion too, for the
wrong reason. Writing the implementation first and pinning it immediately afterward (Step 3) is more
honest than writing the test first, watching it pass by accident, and being told to hand-edit the
flag and revert the edit to "prove" the pass was meaningless - which is what an earlier draft of this
file did.

`Station.exchange` RAISES `UnsupportedCommandError` on a `61 82` - it does not return an `Unsupported`
value. Left alone, every `isinstance(reply, Unsupported)` branch below would be unreachable, and the
`except RailctlError` above it would turn the one reply entitled to write a capability `False` into a
`"fail"` check with the fact lost - the exact M1 failure this project exists to prevent, running
backwards. `_exchange` turns that raise back into the value the existing branches already test for,
so no check's structure changes:

```python
from railctl.errors import UnsupportedCommandError
from railctl.station.programming import CvMatcher
from railctl.xbus.commands import cmd_service_direct_read
from railctl.xbus.cv import CvEncoding
from railctl.xbus.replies import UNSUPPORTED, CvValue, NoAck, Reply
```

`Reply` is added here because `_exchange` below is annotated `-> Reply`: neither `task-7.md`'s own
`railctl.xbus.replies` import line (`StationVersion, StationStatus, Unsupported`) nor this one
otherwise brings the name in, and ruff's `F821` (undefined name) fires under `select = ["F", ...]`
even with `from __future__ import annotations` deferring the annotation's evaluation - the name
still has to resolve at lint time.

Add these imports (merge with the existing `railctl.errors`/`railctl.xbus.commands`/
`railctl.xbus.replies` import lines rather than duplicating them), then add:

```python
def _exchange(station: "Station", telegram: bytes, *, timeout: float) -> Reply:
    """station.exchange, with the one refusal that is a real answer turned back into a value.

    61 82 is the ONLY reply entitled to write a capability False, and Station.exchange raises it.
    A check that let that raise reach its own `except RailctlError` would record "fail" and leave
    the capability at None - the one reply that CAN say "no" turned into "unknown", which is the
    exact M1 failure this project exists to prevent, running backwards.
    """
    try:
        return station.exchange(telegram, timeout=timeout)
    except UnsupportedCommandError:
        return UNSUPPORTED


def _service_probe(
    station: "Station", telegram: bytes, cv: int, encoding: CvEncoding
) -> CvValue | Unsupported | NoAck | None:
    """One service-mode read, already inside a service-mode session the caller
    entered. Returns the value, a definitive Unsupported/NoAck, or None for
    'nothing conclusive' - never raises for a plain timeout, because the
    caller (D5-D8) needs to keep going to the next check either way."""
    reply = _exchange(station, telegram, timeout=TIMING.li_ack_programming)
    if isinstance(reply, Unsupported):
        return reply
    matcher = CvMatcher(encoding, cv)
    try:
        outcome = station.programmer.await_result(
            matcher,
            timeout=TIMING.service_result,
            first_delay=TIMING.service_first_poll_delay,
            interval=TIMING.service_poll_interval,
            exchange_timeout=TIMING.li_ack_programming,
            allow_poll=True,
            ready_means_done=False,
            context="service",
        )
    except UnsupportedCommandError:
        # A 61 82 to the 21 10 result poll is the same refusal as a 61 82 to
        # the read itself - the station can reject either half of the
        # exchange, and both mean the same thing here.
        return UNSUPPORTED
    if isinstance(outcome, (CvValue, NoAck)):
        return outcome
    return None  # a stray reply or TimedOut: inconclusive, not a capability verdict


def _check_d5(station: "Station") -> Check:
    try:
        outcome = _service_probe(
            station, cmd_service_direct_read(PROBE_CV), PROBE_CV, CvEncoding.SERVICE_DIRECT
        )
    except RailctlError as exc:
        return Check("D5", CHECK_TITLES["D5"], "fail", str(exc))
    if isinstance(outcome, Unsupported):
        station.record(service_direct_cv=False)
        return Check("D5", CHECK_TITLES["D5"], "ok", "service direct read unsupported (61 82)")
    if isinstance(outcome, CvValue):
        station.record(service_direct_cv=True)
        detail = f"service direct read confirmed (CV{PROBE_CV}={outcome.value})"
        return Check("D5", CHECK_TITLES["D5"], "ok", detail)
    if isinstance(outcome, NoAck):
        detail = "decoder answered 61 13 on the programming track"
        return Check("D5", CHECK_TITLES["D5"], "unknown", detail)
    return Check("D5", CHECK_TITLES["D5"], "unknown", "no result within the service-mode budget")
```

`context="service"` above, not `context="doctor"`: `await_result`'s `context` parameter is
`Literal["pom", "service"]`, and every probe this batch runs is a service-mode one regardless of
which check it belongs to.

Wire the batch into `run_probe`, replacing the tail of the `CHECK_IDS[5:]` stub loop with the real
gate (D6-D8 stay stubs for two more steps, appended as placeholders inside the same `try`):

```python
    if use_programming_track:
        power_before = station.status().track_power
        try:
            checks.append(_check_d5(station))
            for check_id in ("D6", "D7", "D8"):
                checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
        finally:
            station.programmer.exit_service_mode(restore_power=power_before)
    else:
        skip_detail = "programming track disabled (--no-programming-track)"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", skip_detail))

    for check_id in CHECK_IDS[9:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
```

This replaces the `for check_id in CHECK_IDS[5:]: ...` line from `task-7b.md` - delete it, this block
and the one below it (`CHECK_IDS[9:]`) together cover the same range.

No test run in this step: it is pure implementation, and Step 3 is where the first test against it
lands.

- [ ] **Step 3: Write and pass the D5-through-D8 gating test**

```python
def test_d5_through_d8_are_skipped_under_no_programming_track(doctor_bench):
    report = run_probe(
        doctor_bench.station,
        address=50,
        use_programming_track=False,
        now_utc=lambda: "2026-08-05T00:00:00Z",
    )
    for check_id in ("D5", "D6", "D7", "D8"):
        check = report.check(check_id)
        assert check.status == "skip"
    assert report.capabilities.service_direct_cv is None
    assert report.capabilities.z21_cv_opcodes is None
    assert report.capabilities.service_ext_cv is None
    # No service-mode telegram was sent at all - not even an entry attempt.
    # doctor_bench.sent, not .transport.written: the latter is framed (an
    # LI-USB header the envelope adds), so request[:1] would compare against
    # the frame prefix instead of the telegram's own first byte.
    assert not any(
        request.startswith(b"\x21\x10") or request[:1] == b"\x22" or request[:1] == b"\x23"
        for request in doctor_bench.sent
    )
```

Run: `uv run pytest tests/station/test_doctor.py -k d5_through_d8`
Expected: `2 passed` (1 test x 2 `chunk_size` ids) - it passes by construction, the same way
`test_the_doctor_never_writes_a_decoder_cv` will later in this file: Step 2's `if
use_programming_track: ... else: ...` split already sends nothing under the disabled flag. This test
exists to catch a *future* regression (a refactor that starts sending a probe telegram before
checking the flag), not to drive today's implementation - there is nothing to "prove was red first"
about a mechanical, always-true-by-construction assertion, which is exactly why it belongs here,
right after the code it pins, rather than staged earlier with a hand-edited flag to fake a red run.

- [ ] **Step 4: Write and pass the D5 read/unsupported/noack tests, then commit**

```python
def test_d5_success_records_service_direct_cv_true(doctor_bench):
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_direct_cv is True
    assert report.check("D5").status == "ok"


def test_d5_unsupported_records_service_direct_cv_false(doctor_bench):
    doctor_bench.transport.on_write.set(cmd_service_direct_read(PROBE_CV), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_direct_cv is False
    assert report.check("D5").status == "ok"
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d5`
Expected: `4 passed` (2 tests x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `33 passed` - `2 * (13 + 1 + 2) + 1`: `task-7b.md`'s 13 bench tests, plus the gate test,
plus these 2 D5 tests, doubled, plus the 1 plain test.

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D5 (service direct read)"
```

- [ ] **Step 5: Write the failing D6 test, pinning the exact telegram bytes**

```python
from railctl.xbus.commands import REQ_4_DATA, DB_Z21_WRITE


def test_d6_probes_only_the_z21_read_opcode_never_the_write_one(doctor_bench):
    """Pinned: 23 11 only. 23 11 has no meaning in classic XpressNet, so a
    station that lacks it answers 61 82 and nothing happens; probing 24 12
    (the write opcode) could modify a CV instead."""
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 1, 5)
    )
    run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    # doctor_bench.sent holds bare telegrams, so req[0] is the real X-Bus
    # header byte; .transport.written is framed and req[0] would be the
    # envelope's own first byte on every request, making this assertion
    # vacuously true no matter what doctor.py sends.
    written = doctor_bench.sent
    assert cmd_z21_cv_read(1) in written
    assert not any(req[0] == REQ_4_DATA and req[1] == DB_Z21_WRITE for req in written)


def test_d6_success_records_z21_cv_opcodes_true(doctor_bench):
    # queue_once_for, not queue_once: D5 polls the identical 21 10 telegram
    # before D6 does, and CV1's reply bytes would satisfy D5's own matcher
    # too (both are low-band reads) - queue_once would let D5 drain it first.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_z21_cv_read(1), cv_reply(0x14, 1, 5)
    )
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.z21_cv_opcodes is True


def test_d6_unsupported_records_z21_cv_opcodes_false(doctor_bench):
    doctor_bench.transport.on_write.set(cmd_z21_cv_read(1), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.z21_cv_opcodes is False
```

`REQ_4_DATA` and `DB_Z21_WRITE` are already exported names in `railctl.xbus.commands` (M4): verified
on disk, `REQ_4_DATA = 0x24` and `DB_Z21_WRITE = 0x12`, so `24 12` is `(REQ_4_DATA, DB_Z21_WRITE)`.
Add them, and `cmd_z21_cv_read`, to the test file's import line. `cmd_z21_cv_read(1)` is the probe
telegram this task uses - CV1, matching the design document's own literal example (`23 11 00 00`) - a
named constant is not required by the pinned behaviour list, so the raw `1` stays inline here and in
`doctor.py`, same as the design text.

`test_d6_success_records_z21_cv_opcodes_true` goes red exactly as before this rewrite: if `_check_d6`
ever fails to call `station.record(z21_cv_opcodes=True)` on a matching `CvValue`. The rewrite changes
only how the test double routes its scripted reply, not what production behaviour the assertion pins.

Run: `uv run pytest tests/station/test_doctor.py -k test_d6`
Expected: `6 failed` (3 tests x 2 `chunk_size` ids) - D6 is still a stub.

- [ ] **Step 6: Implement D6**

```python
def _check_d6(station: "Station") -> Check:
    z21_probe_cv = 1  # spec's own literal example: 23 11 00 00
    try:
        outcome = _service_probe(
            station, cmd_z21_cv_read(z21_probe_cv), z21_probe_cv, CvEncoding.Z21_16BIT
        )
    except RailctlError as exc:
        return Check("D6", CHECK_TITLES["D6"], "fail", str(exc))
    if isinstance(outcome, Unsupported):
        station.record(z21_cv_opcodes=False)
        return Check("D6", CHECK_TITLES["D6"], "ok", "Z21 CV opcode 23 11 unsupported (61 82)")
    if isinstance(outcome, CvValue):
        station.record(z21_cv_opcodes=True)
        detail = f"Z21 CV opcode confirmed (CV{z21_probe_cv}={outcome.value})"
        return Check("D6", CHECK_TITLES["D6"], "ok", detail)
    if isinstance(outcome, NoAck):
        return Check("D6", CHECK_TITLES["D6"], "unknown", "decoder answered 61 13")
    return Check("D6", CHECK_TITLES["D6"], "unknown", "no result within the service-mode budget")
```

Add `from railctl.xbus.commands import cmd_z21_cv_read` to `doctor.py`'s imports (merge with the
existing block), then replace the `D6` placeholder line inside the `try:` block from Step 2 with
`checks.append(_check_d6(station))`.

Run: `uv run pytest tests/station/test_doctor.py -k "test_d6 or d5_through_d8"`
Expected: `8 passed` (3 D6 tests + the 1 gate test, x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `39 passed` - `2 * (16 + 3) + 1`.

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D6 (Z21 CV opcode)"
```

- [ ] **Step 7: Write the failing D7 tests (low and high CV, both agreement cases)**

```python
EXT_HIGH_PROBE_CV = 257  # first CV of page 1: 22 19 01, the design's own example


def test_d7_both_bands_succeed_records_service_ext_cv_true(doctor_bench):
    # queue_once_for, not queue_once: D5 and D6 poll the identical 21 10
    # telegram before D7's low band does, and CV8's reply would satisfy D5's
    # own matcher too - a plain queue_once would let D5 (and, for the second
    # reply, D6) drain both replies before D7 ever gets a turn.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(EXT_HIGH_PROBE_CV), cv_reply(0x15, 1, 7)
    )
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is True
    d7 = report.check("D7")
    assert str(PROBE_CV) in d7.detail and str(EXT_HIGH_PROBE_CV) in d7.detail


def test_d7_high_band_rejected_records_false_and_names_the_band(doctor_bench):
    """Pinned: a station could accept the low page and refuse the high one -
    service_ext_cv is True only when BOTH succeed, and the failing band is
    named so a user knows CV257+ is unreachable in service mode here. A 61 82
    is the only reply this project ever lets a check record False from."""
    # queue_once_for, not queue_once, for the same reason as the test above:
    # D5 polls the identical 21 10 telegram first and CV8's reply satisfies its
    # matcher too, so a plain queue_once is drained by D5 and D7's low band gets
    # nothing. The assertions below would still pass - the detail names the high
    # band whenever the high band is refused, whatever happened to the low one -
    # so this test would go on reporting success while measuring a case its own
    # docstring does not describe: accepts the low page, refuses the high one.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.set(cmd_service_ext_read(EXT_HIGH_PROBE_CV), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is False
    assert str(EXT_HIGH_PROBE_CV) in report.check("D7").detail
    # A refused band is a real, measured fact about this station, not a
    # failure of the probe itself - "fail" would print FAIL for a classic
    # XpressNet station that is merely different, and D5/D6 render the
    # identical 61 82 fact as "ok" too.
    assert report.check("D7").status == "ok"


def test_d7_one_band_noack_leaves_service_ext_cv_unknown_not_false(doctor_bench):
    """Pinned regression: a decoder that fails to acknowledge on ONE band (a
    decoder fact) must not be recorded as 'this station lacks extended
    opcodes' (a station fact) in capabilities.json - that is the exact M1
    failure this project exists to avoid. Only an actual 61 82 may write
    False; a NoAck disagreement between the two bands leaves the capability
    None and the check 'unknown', naming which band was inconclusive."""
    # queue_once_for, not queue_once: without scoping to D7's own low-band
    # probe, D5 (which polls first and shares CV8's exact reply bytes with
    # D7's low band) drains this before D7 gets a turn, and D7's low band
    # ends up as inconclusive as its high band - naming CV8, not CV257, and
    # failing the assertion below for a reason the test is not about.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_ext_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    doctor_bench.transport.on_write.set(cmd_service_ext_read(EXT_HIGH_PROBE_CV), NOACK_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.capabilities.service_ext_cv is None
    d7 = report.check("D7")
    assert d7.status == "unknown"
    assert str(EXT_HIGH_PROBE_CV) in d7.detail
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d7`
Expected: `6 failed` (3 tests x 2 `chunk_size` ids) - D7 is still a stub.

`test_d7_high_band_rejected_records_false_and_names_the_band`'s new `status == "ok"` line goes red
if `_check_d7`'s Unsupported branch below returns `"fail"` instead of `"ok"` - the mistake sheet 2.17
made and B.2 overrules.

`test_d7_both_bands_succeed_records_service_ext_cv_true` and
`test_d7_one_band_noack_leaves_service_ext_cv_unknown_not_false` go red for the same production
reasons they always did - `_check_d7` failing to record `service_ext_cv=True` on two matching
`CvValue`s, and failing to leave it `None` on a low/high disagreement that never saw a `61 82` -
not for any reason introduced by scoping their scripted replies with `queue_once_for`.

- [ ] **Step 8: Implement D7, with the corrected tri-state rule**

```python
def _check_d7(station: "Station") -> Check:
    high_cv = 257  # first CV of page 1 - 22 19 01, the design's own example
    try:
        low = _service_probe(station, cmd_service_ext_read(PROBE_CV), PROBE_CV, CvEncoding.SERVICE_EXT)
        high = _service_probe(station, cmd_service_ext_read(high_cv), high_cv, CvEncoding.SERVICE_EXT)
    except RailctlError as exc:
        return Check("D7", CHECK_TITLES["D7"], "fail", str(exc))
    low_ok, high_ok = isinstance(low, CvValue), isinstance(high, CvValue)
    if low_ok and high_ok:
        detail = f"extended read confirmed on CV{PROBE_CV} and CV{high_cv}"
        station.record(service_ext_cv=True)
        return Check("D7", CHECK_TITLES["D7"], "ok", detail)
    low_unsupported, high_unsupported = isinstance(low, Unsupported), isinstance(high, Unsupported)
    if low_unsupported or high_unsupported:
        failed_cv = PROBE_CV if low_unsupported else high_cv
        station.record(service_ext_cv=False)
        detail = f"extended opcodes rejected for CV{failed_cv}'s band (61 82)"
        return Check("D7", CHECK_TITLES["D7"], "ok", detail)
    # Neither band was definitively rejected, yet they disagree (one silent,
    # one NoAck, or both inconclusive): a decoder-side non-answer is not a
    # station capability. Leave service_ext_cv at None rather than guessing.
    inconclusive_cv = PROBE_CV if not low_ok else high_cv
    detail = f"CV{inconclusive_cv}'s band gave no conclusive result within the service-mode budget"
    return Check("D7", CHECK_TITLES["D7"], "unknown", detail)
```

Add `from railctl.xbus.commands import cmd_service_ext_read` to `doctor.py`'s imports, then replace
the `D7` placeholder line inside the `try:` block with `checks.append(_check_d7(station))`.

Run: `uv run pytest tests/station/test_doctor.py -k test_d7`
Expected: `6 passed` (3 tests x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `45 passed` - `2 * (19 + 3) + 1`.

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D7 (extended CV opcodes)"
```

- [ ] **Step 9: Write the failing D8 tests (gating, then the read itself)**

```python
def test_d8_does_not_run_when_d4_was_silent_not_noack(doctor_bench):
    """Pinned: D8 runs only after D4 answers 61 13, never after D4's silence
    branch (a different, unrelated capability judgment)."""
    pom_telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(pom_telegram, GENERIC_ACK)
    # D4 sees total silence (no 61 13 ever) - falls into the "silence" branch.
    # queue_once_for, not queue_once: D4 polls the identical 21 10 telegram
    # first and would otherwise steal this on its very first (silent) poll -
    # and CV8's reply would match D4's own POM matcher too, turning D4's
    # intended silence into a false success before D4 ever exhausts its
    # attempts.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(PROBE_CV), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D5's own poll answer, so D5 passes and only D4's branch is under test
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.pom_read is False  # D4's silence override, not NoAck
    assert report.check("D8").status == "skip"


def test_d8_runs_after_d4_noack_and_d5_pass_and_reports_cv29_cv28(doctor_bench):
    pom_telegram = cmd_pom_read_byte(50, PROBE_CV, threshold=doctor_bench.station.threshold)
    doctor_bench.transport.on_write.set(pom_telegram, GENERIC_ACK)
    for _ in range(3):
        # D4 is always the first check to poll, so it drains these 3 in order
        # regardless of what runs after it - a plain queue_once is safe here.
        doctor_bench.transport.on_write.queue_once(cmd_service_result_request(), NOACK_REPLY)
    # D5 (service direct on CV8) must PASS for D8 to run. Also safe unscoped:
    # D5 is next in line once D4's 3 NoAcks are spent, and nothing before it
    # can steal a 4th item that was never queued for it under a different key.
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )
    # D8 itself: CV29 then CV28. queue_once_for, not queue_once - D6 and D7
    # poll the identical 21 10 telegram before D8 does, and each mismatch
    # they see (neither replies below matches CV1 or CV257/CV8's band) makes
    # them poll again rather than stop, draining a plain queue before D8 ever
    # gets a turn.
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(29), cv_reply(0x14, 29, 0x08)
    )
    doctor_bench.transport.on_write.queue_once_for(
        cmd_service_direct_read(28), cv_reply(0x14, 28, 0x03)
    )
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    d8 = report.check("D8")
    assert d8.status == "ok"
    assert "CV29" in d8.detail and "CV28" in d8.detail
    assert "bit 3" in d8.detail
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d8`
Expected: `4 failed` (2 tests x 2 `chunk_size` ids) - D8 is still a stub, so
`report.check("D8").status == "skip"` already holds in the first test (it passes for the wrong
reason - the second test is the one that actually fails, on `d8.status == "ok"`).

Both tests go red for the same production reasons they always did:
`test_d8_does_not_run_when_d4_was_silent_not_noack` if `_check_d8` ever runs without a genuine D4
NoAck, and `test_d8_runs_after_d4_noack_and_d5_pass_and_reports_cv29_cv28` if `_check_d8` fails to
read and report CV29/CV28 once its two preconditions hold. `queue_once_for` only fixes which reply
each poll actually receives; neither test's assertions changed.

- [ ] **Step 10: Implement D8, and thread the D4-NoAck / D5-passed flags through**

```python
def _check_d8(station: "Station", *, d4_noack: bool, d5_passed: bool) -> Check:
    if not (d4_noack and d5_passed):
        detail = "runs only after D4 answers 61 13 with D5 already confirmed"
        return Check("D8", CHECK_TITLES["D8"], "skip", detail)
    cv29, cv28 = RAILCOM_CVS
    try:
        result_29 = _service_probe(
            station, cmd_service_direct_read(cv29), cv29, CvEncoding.SERVICE_DIRECT
        )
        result_28 = _service_probe(
            station, cmd_service_direct_read(cv28), cv28, CvEncoding.SERVICE_DIRECT
        )
    except RailctlError as exc:
        return Check("D8", CHECK_TITLES["D8"], "fail", str(exc))
    if isinstance(result_29, CvValue) and isinstance(result_28, CvValue):
        bit3 = "set" if result_29.value & 0x08 else "clear"
        channel = result_28.value & 0x03
        detail = (
            f"CV{cv29}={result_29.value} (bit 3 {bit3}), CV{cv28}={result_28.value} "
            f"(bits 0-1 = {channel:02b}) - RailCom needs CV{cv29} bit 3 set and a "
            f"valid CV{cv28} channel selection"
        )
        return Check("D8", CHECK_TITLES["D8"], "ok", detail)
    return Check("D8", CHECK_TITLES["D8"], "unknown", "CV29/CV28 not readable in service mode")
```

`_check_d4` (`task-7b.md`) already returns a `(Check, bool)` pair, so no reshaping is needed there.
Update `run_probe`'s D4 and D8 wiring together - this replaces both the `D4` if/elif/else block from
`task-7b.md`'s Step 3 and the `D8` placeholder line inside the `try:` block from Step 2 above, delete
both, this is their combined replacement:

```python
    resolved_address = _resolved_address(station, address)
    d4_noack = False
    if track_powered and resolved_address is not None:
        d4_check, d4_noack = _check_d4(station, address=resolved_address)
    elif not track_powered:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "unknown", "track power is off; re-run with --power-on"
        )
    else:
        d4_check = Check(
            "D4", CHECK_TITLES["D4"], "skip", "no locomotive address given; pass --address"
        )
    checks.append(d4_check)

    if use_programming_track:
        power_before = station.status().track_power
        try:
            checks.append(_check_d5(station))
            checks.append(_check_d6(station))
            checks.append(_check_d7(station))
            d5_passed = station.capabilities.service_direct_cv is True
            checks.append(_check_d8(station, d4_noack=d4_noack, d5_passed=d5_passed))
        finally:
            station.programmer.exit_service_mode(restore_power=power_before)
    else:
        skip_detail = "programming track disabled (--no-programming-track)"
        for check_id in ("D5", "D6", "D7", "D8"):
            checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", skip_detail))
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d8`
Expected: `4 passed` (2 tests x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `49 passed` - `2 * (22 + 2) + 1`.

```bash
uv run ruff check .
uv run ruff format --check .
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D8 (RailCom sanity)"
```

This closes the D5-D8 slice. `task-7d.md` continues from here with D9-D12.

---

### Task 7d: the doctor report, part 4: D9 (identity), D10 (address band), D11/D12 (function commands)

Continues `task-7c.md`. `doctor.py` and `tests/station/test_doctor.py` already have D0-D8 implemented;
this file adds the four remaining checks.

**Files:**
- Modify: `src/railctl/station/doctor.py`, `tests/station/test_doctor.py`

**Interfaces:**

- Consumes, in addition to `task-7c.md`'s list:
  - `Station.programmer.pom_read` and `Station.programmer.service_read` - both already consumed
    (`task-7b.md`, `task-7c.md`). `service_read`'s real signature is `def service_read(self, cv:
    int, *, page: CvPage | None = None) -> CvResult: ...` - a **high-level** method that already
    walks `SERVICE_ENCODING_ORDER` internally, gated by whichever capabilities are proven `True`,
    and raises the appropriate `ProgrammingError` subclass when none apply or the CV is out of
    bounds for every proven encoding. It does **not** take a telegram or a `CvMatcher` argument -
    there is no `service_read(station, telegram, matcher)` free-function form anywhere in this
    project. D9's best-effort read below calls it directly, with no page/band arithmetic of its own,
    which is also why D9 needs no `CvMatcher` or `ext_cv_fields` import at all.
  - `railctl.xbus.commands.{cmd_loco_info, cmd_function_group, cmd_function_single}`,
    `railctl.xbus.commands.{FunctionAction, FunctionGroup}` - `cmd_loco_info` and `FunctionGroup`
    already on disk (M4); `cmd_function_single`/`FunctionAction` were added to
    `railctl.xbus.commands` by an earlier M5 task (spec lines 696, 864):
    `class FunctionAction(enum.IntEnum): OFF = 0b00; ON = 0b01; TOGGLE = 0b10` and `def
    cmd_function_single(address: int, function: int, action: FunctionAction, *, threshold: int) ->
    bytes` encoding `E4 F8 AH AL TTNNNNNN X`. This file calls it only with `function=0,
    action=FunctionAction.OFF` (D12's "F0 off", the least disruptive telegram the opcode can carry).
  - `railctl.xbus.address.encode_loco_address(address, *, long_threshold)` (used internally by
    `cmd_loco_info`/`cmd_function_group`/`cmd_function_single`, not called directly here).
  - `railctl.xbus.dialect.{XPRESSNET, Z21, DIVERGENCE_BAND}` (`DIVERGENCE_BAND = range(100, 128)`).
  - `railctl.xbus.replies.LocoInfo`.
  - `_exchange` - the module-private helper `task-7c.md` adds to `doctor.py` alongside
    `_service_probe`: `station.exchange`, with `UnsupportedCommandError` caught and turned back into
    the `UNSUPPORTED` value. D11 and D12 are the other two checks a real `61 82` must reach as a value
    rather than a raise (B.2 in the addendum), so both call `_exchange(...)` wherever they currently
    call `station.exchange(...)` directly. D10 is not on that list: it never branches on
    `isinstance(reply, Unsupported)` - it branches on `isinstance(reply, LocoInfo)`, and a genuine `61
    82` from either encoding is correctly a `"fail"` there, not a capability verdict - so D10's two
    `station.exchange(...)` calls stay exactly as written.

- Produces so far: unchanged shape; D9-D12 are now real checks rather than stub entries, and
  `CHECK_IDS[9:]`'s stub loop from `task-7c.md` disappears entirely - this file's last step removes
  it.

**Decisions already made - do not re-open, do not contradict:**
- D9 reads `IDENTITY_CVS` through whichever path D4-D7 already proved works, independently per CV -
  one CV's failure must not stop the rest from being read. `decoder_family` (Task 1) turns CV250's
  value into `"ms"`/`"other"`/`"unknown"`, and an unread CV250 must render `"unknown"`, never `"ms"`.
- **D10 is gated on track power exactly like D4** (spec line 855, "if off without the flag, D4 and
  D10 are skipped as `unknown`"): with the track off and no `--power-on`, D10 is `"unknown"`, not
  `"skip"`. Only when the track *is* powered does the existing "no address in 100..127" reason apply,
  and that one stays `"skip"` - a genuine opt-out, since there is nothing to compare in that case
  regardless of power. These are two different reasons for not running, and the four-value `Check`
  status exists precisely so they render differently.
- D10 compares both address encodings; identical replies leave `loco_address_threshold` at `None`
  with a note, and `01 09 08` identifies the rejected form immediately.
- D11 and D12 use all-bits-zero telegrams on the probe address - the least disruptive possible
  probes - and are `"skip"` (an opt-out) with no address, regardless of track power: unlike D4/D10,
  nothing in the spec ties D11/D12 to the unpowered-unknown distinction.

---

- [ ] **Step 1: Confirm `task-7c.md` is green**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `49 passed` (`task-7c.md`'s own final count: `2 * 24 + 1`). If this is not green, stop and
fix `task-7c.md`'s slice before adding anything here.

- [ ] **Step 2: Write the failing D9 test (decoder_family, unread CV250)**

```python
def test_d9_with_no_established_read_path_reports_family_unknown_never_ms(doctor_bench):
    """Pinned: an unread CV250 must render as 'unknown', never as 'ms' - the
    same guard decoder_family() itself enforces (Task 1), exercised here
    through the doctor's own aggregation over IDENTITY_CVS."""
    doctor_bench.transport.on_write.set(cmd_service_direct_read(PROBE_CV), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_z21_cv_read(1), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_service_ext_read(PROBE_CV), UNSUPPORTED_REPLY)
    doctor_bench.transport.on_write.set(cmd_service_ext_read(257), UNSUPPORTED_REPLY)
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    d9 = report.check("D9")
    assert "unknown" in d9.detail
    assert "ms" not in d9.detail.lower().replace("unknown", "")
    assert d9.status == "skip"
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d9`
Expected: `2 failed` (1 test x 2 `chunk_size` ids) - D9 is still a stub (`"not implemented yet"` has no
`"unknown"` substring).

- [ ] **Step 3: Implement D9**

```python
def _best_effort_read(station: "Station", cv: int) -> int | None:
    """Read one CV through whichever path D4-D7 already proved works: POM
    first if `pom_read` is proven True and an address is resolvable, then a
    single high-level `service_read` call, which already walks
    SERVICE_ENCODING_ORDER (Z21, then direct, then extended) internally and
    raises when none of those is proven. This file does no band or page
    arithmetic of its own - that stays inside `service_read`, exactly as Rule
    2 under `station/` requires."""
    caps = station.capabilities
    address = station.default_address
    if caps.pom_read is True and address is not None:
        try:
            return station.programmer.pom_read(cv, address=address).value
        except RailctlError:
            pass
    try:
        return station.programmer.service_read(cv).value
    except RailctlError:
        return None


def _check_d9(station: "Station") -> Check:
    values = {cv: _best_effort_read(station, cv) for cv in IDENTITY_CVS}
    family = decoder_family(values[DECODER_TYPE_CV])
    read_count = sum(1 for value in values.values() if value is not None)
    rendered = ", ".join(
        f"CV{cv}={values[cv]}" if values[cv] is not None else f"CV{cv}=?" for cv in IDENTITY_CVS
    )
    status = "ok" if read_count else "skip"
    return Check("D9", CHECK_TITLES["D9"], status, f"decoder family: {family}; {rendered}")
```

Replace the `for check_id in CHECK_IDS[9:]: ...` stub loop (from `task-7c.md`'s Step 2) with:

```python
    checks.append(_check_d9(station))
    for check_id in CHECK_IDS[10:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d9`
Expected: `2 passed` (1 test x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `53 passed` - `2 * (24 + 1) + 1`.

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D9 (decoder identity)"
```

- [ ] **Step 4: Write the failing D10 tests (address band, and the track-power gate)**

```python
from railctl.xbus.replies import LocoInfo


def test_d10_is_unknown_when_the_track_is_unpowered_without_power_on(doctor_bench):
    """Distinct from the no-address skip below - spec line 855, 'D4 and D10
    are skipped as unknown'. An operator who forgot --power-on must not read
    this as 'nothing to probe', but as 'this genuinely could not be
    established'."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(
        doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.check("D10").status == "unknown"
    assert report.capabilities.loco_address_threshold is None


def test_d10_is_skipped_with_no_address_in_the_divergence_band(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D10").status == "skip"
    assert report.capabilities.loco_address_threshold is None


def test_d10_identical_replies_leave_the_threshold_unresolved(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), loco_info_reply()
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(
        doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.loco_address_threshold is None
    assert report.check("D10").status == "ok"


def test_d10_only_the_z21_form_answers_records_threshold_128(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), encode(0x01, 0x09)
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    report = run_probe(
        doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.loco_address_threshold == 128
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d10`
Expected: `8 collected` (4 tests x 2 `chunk_size` ids): the 2 collected
`test_d10_is_skipped_with_no_address_in_the_divergence_band` ids PASS already (the stub from
`task-7c.md`'s Step 2 is an unconditional `"skip"`, which happens to match this one scenario); the
other 6 FAIL - including the new unpowered-track test, since the stub has no notion of track power at
all yet.

- [ ] **Step 5: Implement D10, gated on track power**

```python
from railctl.xbus.commands import cmd_loco_info
from railctl.xbus.dialect import DIVERGENCE_BAND, XPRESSNET, Z21
from railctl.xbus.replies import LocoInfo
```

Add these imports, then:

```python
def _check_d10(station: "Station", *, address: int | None, track_powered: bool) -> Check:
    if not track_powered:
        detail = "track power is off; re-run with --power-on to verify D10"
        return Check("D10", CHECK_TITLES["D10"], "unknown", detail)
    resolved = _resolved_address(station, address)
    if resolved is None or resolved not in DIVERGENCE_BAND:
        detail = "no address in 100..127 given; pass --address in that range"
        return Check("D10", CHECK_TITLES["D10"], "skip", detail)
    try:
        xpressnet_reply = station.exchange(
            cmd_loco_info(resolved, threshold=XPRESSNET.long_address_threshold),
            timeout=TIMING.li_ack_normal,
        )
        z21_reply = station.exchange(
            cmd_loco_info(resolved, threshold=Z21.long_address_threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D10", CHECK_TITLES["D10"], "fail", str(exc))
    xpressnet_ok, z21_ok = isinstance(xpressnet_reply, LocoInfo), isinstance(z21_reply, LocoInfo)
    if xpressnet_ok == z21_ok:
        detail = f"address {resolved} answers identically under both encodings; threshold unresolved"
        return Check("D10", CHECK_TITLES["D10"], "ok", detail)
    threshold = XPRESSNET.long_address_threshold if xpressnet_ok else Z21.long_address_threshold
    station.record(loco_address_threshold=threshold)
    form = "XpressNet (long from 100)" if xpressnet_ok else "Z21 (long from 128)"
    detail = f"address {resolved} answers only the {form} form"
    return Check("D10", CHECK_TITLES["D10"], "ok", detail)
```

`_check_d10` now takes `track_powered` (already in scope inside `run_probe`, set by `_check_d3` in
`task-7.md`). Replace the `for check_id in CHECK_IDS[10:]: ...` stub loop (from Step 3 above) with:

```python
    checks.append(_check_d10(station, address=address, track_powered=track_powered))
    for check_id in CHECK_IDS[11:]:
        checks.append(Check(check_id, CHECK_TITLES[check_id], "skip", _PLACEHOLDER_DETAIL))
```

Run: `uv run pytest tests/station/test_doctor.py -k test_d10`
Expected: `8 passed` (4 tests x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `61 passed` - `2 * (25 + 4) + 1`.

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor check D10 (address band)"
```

- [ ] **Step 6: Write the failing D11/D12 tests (all-bits-zero telegrams)**

```python
def test_d11_sends_all_zero_bits_on_groups_4_and_5(doctor_bench):
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    threshold = doctor_bench.station.threshold
    # doctor_bench.sent, not .transport.written: the latter is framed, so
    # neither cmd_function_group() call would ever appear inside it verbatim.
    written = doctor_bench.sent
    assert cmd_function_group(50, FunctionGroup.G4, 0, threshold=threshold) in written
    assert cmd_function_group(50, FunctionGroup.G5, 0, threshold=threshold) in written
    assert report.capabilities.function_groups_4_5 is True


def test_d11_unsupported_records_false(doctor_bench):
    doctor_bench.transport.on_write.set(
        cmd_function_group(50, FunctionGroup.G4, 0, threshold=doctor_bench.station.threshold),
        UNSUPPORTED_REPLY,
    )
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert report.capabilities.function_groups_4_5 is False


def test_d12_sends_the_f0_off_single_function_telegram(doctor_bench):
    report = run_probe(
        doctor_bench.station, address=50, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    threshold = doctor_bench.station.threshold
    expected = cmd_function_single(50, 0, FunctionAction.OFF, threshold=threshold)
    assert expected in doctor_bench.sent
    assert report.capabilities.single_function_cmd is True


def test_d11_and_d12_are_skipped_with_no_address(doctor_bench):
    report = run_probe(doctor_bench.station, now_utc=lambda: "2026-08-05T00:00:00Z")
    assert report.check("D11").status == "skip"
    assert report.check("D12").status == "skip"
```

Add `FunctionAction`, `FunctionGroup` and `cmd_function_group`, `cmd_function_single` to the test
file's `railctl.xbus.commands` import line.

`test_d11_sends_all_zero_bits_on_groups_4_and_5` and `test_d12_sends_the_f0_off_single_function_telegram`
go red if D11/D12 ever send a telegram other than the all-bits-zero probe the test names - reading
`.sent` rather than `.transport.written` is what lets a wrong telegram be caught at all instead of
comparing against framed bytes neither probe's bare encoding can ever equal.

Run: `uv run pytest tests/station/test_doctor.py -k "test_d11 or test_d12"`
Expected: `8 collected` (4 tests x 2 `chunk_size` ids); the 2 collected
`test_d11_and_d12_are_skipped_with_no_address` ids pass already; the other 6 FAIL.

- [ ] **Step 7: Implement D11 and D12, then run the whole file and commit**

```python
from railctl.xbus.commands import FunctionAction, FunctionGroup, cmd_function_group, cmd_function_single
```

Add this import, then - both checks call `_exchange`, not `station.exchange`, directly. `_exchange` is
already module-level in `doctor.py` since `task-7c.md`'s Step 2 (it wraps `station.exchange` and turns
the `UnsupportedCommandError` raise on a `61 82` back into the `UNSUPPORTED` value the `isinstance(...,
Unsupported)` branch below expects), so no new import is needed for it here:

```python
def _check_d11(station: "Station", *, address: int | None) -> Check:
    resolved = _resolved_address(station, address)
    if resolved is None:
        return Check("D11", CHECK_TITLES["D11"], "skip", "no locomotive address given; pass --address")
    threshold = station.threshold
    try:
        g4 = _exchange(
            station,
            cmd_function_group(resolved, FunctionGroup.G4, 0, threshold=threshold),
            timeout=TIMING.li_ack_normal,
        )
        g5 = _exchange(
            station,
            cmd_function_group(resolved, FunctionGroup.G5, 0, threshold=threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D11", CHECK_TITLES["D11"], "fail", str(exc))
    if isinstance(g4, Unsupported) or isinstance(g5, Unsupported):
        station.record(function_groups_4_5=False)
        return Check("D11", CHECK_TITLES["D11"], "ok", "function groups 4/5 unsupported (61 82)")
    station.record(function_groups_4_5=True)
    return Check("D11", CHECK_TITLES["D11"], "ok", "function groups 4/5 accepted (F13-F28 off)")


def _check_d12(station: "Station", *, address: int | None) -> Check:
    resolved = _resolved_address(station, address)
    if resolved is None:
        return Check("D12", CHECK_TITLES["D12"], "skip", "no locomotive address given; pass --address")
    try:
        reply = _exchange(
            station,
            cmd_function_single(resolved, 0, FunctionAction.OFF, threshold=station.threshold),
            timeout=TIMING.li_ack_normal,
        )
    except RailctlError as exc:
        return Check("D12", CHECK_TITLES["D12"], "fail", str(exc))
    if isinstance(reply, Unsupported):
        station.record(single_function_cmd=False)
        return Check("D12", CHECK_TITLES["D12"], "ok", "single-function command unsupported (61 82)")
    station.record(single_function_cmd=True)
    return Check("D12", CHECK_TITLES["D12"], "ok", "single-function command accepted (F0 off)")
```

Replace the `for check_id in CHECK_IDS[11:]: ...` stub loop (from Step 5) - the last placeholder loop
in `run_probe` - with:

```python
    checks.append(_check_d11(station, address=address))
    checks.append(_check_d12(station, address=address))
```

`_PLACEHOLDER_DETAIL` is now unused - remove its definition along with the last stub loop;
`CHECK_IDS[4:]`/`[5:]`/`[9:]`/`[10:]`/`[11:]` slicing was scaffolding for incremental steps across
four files, not a shape to keep.

Run: `uv run pytest tests/station/test_doctor.py -k "test_d11 or test_d12"`
Expected: `8 passed` (4 tests x 2 `chunk_size` ids).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `67 passed` - `2 * (29 + 4) + 1`.

```bash
uv run ruff check .
uv run ruff format --check .
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add doctor checks D11-D12 (function commands)"
```

This closes the D9-D12 slice, and every entry in `CHECK_IDS` now has a real check behind it.
`task-7e.md` continues from here with the verdict block, exit codes, `Station.probe()` and the
package exports.

---

### Task 7e: the doctor report, part 5: the verdict block, exit codes, `Station.probe()`, package exports

Continues `task-7d.md`. `doctor.py` and `tests/station/test_doctor.py` already implement every check
D0-D12; this file adds the mechanical "never writes a decoder CV" and `CHECK_IDS`-completeness pins,
the human verdict block, wires `Station.probe()`, exports `run_probe`/`verdict_lines`/
`exit_code_for_report` from the `station` package, and runs the M5 gate. This closes Task 7 and, with
it, M5.

**Files:**
- Modify: `src/railctl/station/doctor.py`, `src/railctl/station/facade.py`,
  `src/railctl/station/__init__.py`, `tests/station/test_doctor.py`

**Interfaces:**

- Consumes, in addition to `task-7d.md`'s list:
  - `railctl.xbus.commands.{POM_WRITE_BYTE_BASE, POM_WRITE_BIT_BASE, DB_DIRECT_WRITE,
    DB_Z21_WRITE}` and `railctl.xbus.cv.EXT_WRITE_OPCODES` - all already exported (M4).

- Produces (this file completes the contract `task-7.md` opened):
```python
def verdict_lines(report: DoctorReport) -> list[str]: ...
    # "Primary CV path: …", "Fallback: …", "CV > 255: …", "Loco addresses: …"
```
`src/railctl/station/facade.py`:
```python
    def probe(self, *, address: int | None = None, allow_power_on: bool = False,
              use_programming_track: bool = True) -> DoctorReport: ...
```
`src/railctl/station/__init__.py` gains `run_probe`, `verdict_lines`, `exit_code_for_report` in its
`__all__`-plus-imports block, alongside `Check` and `DoctorReport` (already re-exported since Task 1
- `task-7d.md` and earlier never had reason to touch that file). Per the normalisation sheet, Task 12
imports `Check`/`DoctorReport`/`run_probe`/`verdict_lines`/`exit_code_for_report` all from
`railctl.station`, never from `railctl.station.facade` or `railctl.station.doctor` directly - keep
that in mind while wiring the exports below, even though this task does not touch Task 12's file.

**Decisions already made - do not re-open, do not contradict:**
- The doctor never writes a decoder CV. Every telegram this file's tests exercise comes from a
  `cmd_..._read` function or a function/drive telegram; not one write-opcode encoder is imported in
  `doctor.py`.
- The verdict block is the doctor's human output and lives here, not in the CLI (Task 12 only prints
  the lines it is given), so it is unit-testable without Typer.
- `Station.probe()` never forwards a `now_utc` parameter; it always lets `run_probe` use the real
  clock, exactly as `task-7.md`'s Decisions section already established.

---

- [ ] **Step 1: Confirm `task-7d.md` is green**

Run: `uv run pytest tests/station/test_doctor.py`
Expected: `67 passed` (`task-7d.md`'s own final count: `2 * 33 + 1`). If this is not green, stop and
fix `task-7d.md`'s slice before adding anything here.

- [ ] **Step 2: Write and pass the "never writes a decoder CV" test**

```python
from railctl.xbus.commands import (
    POM_WRITE_BIT_BASE,
    POM_WRITE_BYTE_BASE,
    DB_DIRECT_WRITE,
    DB_Z21_WRITE,
)
from railctl.xbus.cv import EXT_WRITE_OPCODES


def test_the_doctor_never_writes_a_decoder_cv(doctor_bench):
    """The mechanical version of the design's central promise. Every read
    scenario answers successfully so every check actually runs, then every
    telegram this run sent is checked against every CV-write encoding:
    E6 30 .. EC|MM / E8|MM (POM byte/bit write), 23 16 (direct write),
    23 1C..1F (extended write), 24 12 (Z21 write)."""
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 7, PROBE_CV_VALUE)
    )  # D4 (POM), zero-based echo
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D5 (service direct)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, 1, 5)
    )  # D6 (Z21)
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x14, PROBE_CV, PROBE_CV_VALUE)
    )  # D7 low
    doctor_bench.transport.on_write.queue_once(
        cmd_service_result_request(), cv_reply(0x15, 1, 7)
    )  # D7 high
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=XPRESSNET.long_address_threshold), encode(0x01, 0x09)
    )
    doctor_bench.transport.on_write.set(
        cmd_loco_info(105, threshold=Z21.long_address_threshold), loco_info_reply()
    )
    run_probe(doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z")

    # doctor_bench.sent, not .transport.written: the latter is framed, so
    # telegram[0]/telegram[1] would read the envelope's own prefix bytes on
    # every request rather than the X-Bus header and DB0 this check needs -
    # every `assert not (...)` below would hold vacuously no matter what
    # doctor.py sent, which is not a test, it is a tautology with a docstring.
    for telegram in doctor_bench.sent:
        header, db0 = telegram[0], telegram[1]
        assert not (header == 0xE6 and db0 == 0x30 and telegram[4] & 0xFC == POM_WRITE_BYTE_BASE)
        assert not (header == 0xE6 and db0 == 0x30 and telegram[4] & 0xFC == POM_WRITE_BIT_BASE)
        assert not (header == 0x23 and db0 == DB_DIRECT_WRITE)
        assert not (header == 0x23 and db0 in EXT_WRITE_OPCODES)
        assert not (header == 0x24 and db0 == DB_Z21_WRITE)
```

`POM_WRITE_BYTE_BASE`, `POM_WRITE_BIT_BASE`, `DB_DIRECT_WRITE`, `DB_Z21_WRITE` and
`EXT_WRITE_OPCODES` are already exported from `railctl.xbus.commands`/`railctl.xbus.cv` (M4) - add
them to the test file's imports.

Run: `uv run pytest tests/station/test_doctor.py -k never_writes_a_decoder_cv`
Expected: `2 passed` (1 test x 2 `chunk_size` ids) - this test should already pass by construction,
since `doctor.py` never imports a write-opcode encoder. If it fails, the failure is in `doctor.py`'s
imports, not its logic: grep the file for `cmd_pom_write`, `cmd_service_direct_write`,
`cmd_service_ext_write` or `cmd_z21_cv_write` and remove whichever crept in. This test now goes red
for a real reason if one does: reading `doctor_bench.sent` means `header`/`db0` are the actual X-Bus
bytes, not the envelope's `FF FE` prefix, so a write encoder slipping into `doctor.py` would trip one
of the five `assert not (...)` lines instead of being silently invisible to all of them.

- [ ] **Step 3: Write and pass the `CHECK_IDS` coverage/ordering test**

```python
def test_every_check_id_appears_exactly_once_in_order(doctor_bench):
    report = run_probe(
        doctor_bench.station, address=105, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert tuple(check.id for check in report.checks) == CHECK_IDS


def test_every_check_id_appears_exactly_once_when_gated_paths_are_taken(doctor_bench):
    """Same pin, walking the OTHER branch of every gate this task added:
    unpowered track (D4/D10 read 'unknown' rather than the '--power-on'-given
    path) and --no-programming-track (skips D5-D8). CHECK_IDS must still be
    complete."""
    doctor_bench.transport.on_write.set(cmd_station_status(), STATUS_REPLY_UNPOWERED)
    report = run_probe(
        doctor_bench.station, use_programming_track=False, now_utc=lambda: "2026-08-05T00:00:00Z"
    )
    assert tuple(check.id for check in report.checks) == CHECK_IDS
```

Run: `uv run pytest tests/station/test_doctor.py -k check_id`
Expected: `4 passed` (2 tests x 2 `chunk_size` ids).

- [ ] **Step 4: Write the failing `verdict_lines` tests and implement it**

```python
def test_verdict_lines_are_exactly_four():
    caps = Capabilities.unknown("test")
    report = DoctorReport(checks=(), capabilities=caps)
    assert len(verdict_lines(report)) == 4


def test_verdict_lines_on_an_all_unknown_capability_set_say_unknown_never_bare_no():
    """Pinned: every line must say 'unknown', and none may contain the bare
    word 'no' - a naive substring check would flag 'unknown' itself (it
    contains 'no' as consecutive letters), so this asserts a WORD boundary."""
    import re

    caps = Capabilities.unknown("test")
    report = DoctorReport(checks=(), capabilities=caps)
    lines = verdict_lines(report)
    assert len(lines) == 4
    for line in lines:
        assert line.strip() != ""
        assert "unknown" in line.lower()
        assert re.search(r"\bno\b", line.lower()) is None
```

`DoctorReport` needs importing in the test file: add `from railctl.station.types import
DoctorReport` to the import block.

Run: `uv run pytest tests/station/test_doctor.py -k verdict`
Expected: `2 failed` - the `task-7.md` Step 2 placeholder returns four empty strings, which fails both
`line.strip() != ""` and `"unknown" in line`. Neither test takes `doctor_bench`, so there is no
`chunk_size` doubling here.

Replace the placeholder `verdict_lines` in `doctor.py`:

```python
def _primary_cv_path(caps: "Capabilities") -> str:
    if caps.pom_read is True:
        channel = caps.pom_result_channel or "unknown"
        return f"POM (results arrive via {channel})"
    if caps.pom_read is False:
        return "POM unavailable (61 82); see Fallback"
    return "unknown (re-run the doctor to establish this)"


def _fallback(caps: "Capabilities") -> str:
    if caps.service_direct_cv is True:
        return "service mode, direct opcodes, CV1-255 only"
    if caps.z21_cv_opcodes is True:
        return "service mode, Z21 opcodes, CV1-1024"
    if caps.service_ext_cv is True:
        return "service mode, extended opcodes"
    if caps.service_direct_cv is None and caps.z21_cv_opcodes is None and caps.service_ext_cv is None:
        return "unknown (re-run the doctor)"
    return "unavailable - service-mode opcodes unconfirmed"


def _cv_above_255(caps: "Capabilities") -> str:
    if caps.z21_cv_opcodes is True:
        return "POM (write) + Z21 opcodes (read), CV1-1024"
    if caps.service_ext_cv is True:
        return "POM (write) + extended opcodes (read)"
    if caps.z21_cv_opcodes is False and caps.service_ext_cv is False:
        return "POM only (extended opcodes rejected: 61 82)"
    return "unknown (re-run the doctor with an address to establish this)"


def _loco_addresses(caps: "Capabilities") -> str:
    if caps.loco_address_threshold == 100:
        return "1-99 short, 100+ long (XpressNet form confirmed)"
    if caps.loco_address_threshold == 128:
        return "1-127 short, 128+ long (Z21 form confirmed)"
    return "100..127 unknown (re-run with --address in that range)"


def verdict_lines(report: DoctorReport) -> list[str]:
    caps = report.capabilities
    return [
        f"Primary CV path: {_primary_cv_path(caps)}",
        f"Fallback:        {_fallback(caps)}",
        f"CV > 255:        {_cv_above_255(caps)}",
        f"Loco addresses:  {_loco_addresses(caps)}",
    ]
```

This needs `Capabilities` importable for the type hint - add `from railctl.station.capabilities
import Capabilities` to `doctor.py`'s imports (it is currently only imported under
`TYPE_CHECKING` via `Station`; add a direct runtime import here since `verdict_lines`/the four
helpers use it as an ordinary parameter annotation evaluated at definition time only under `from
__future__ import annotations`, so a plain top-level import is correct and does not create a
cycle - `capabilities.py` imports nothing from `station.doctor`).

Run: `uv run pytest tests/station/test_doctor.py -k verdict`
Expected: `2 passed`.

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `75 passed` - `2 * (33 + 1 + 2) + 3`: `task-7d.md`'s 33 bench tests plus this file's
never-writes-a-decoder-cv and 2 `CHECK_IDS` tests (all bench, doubled), plus 3 plain tests
(`test_check_ids_are_thirteen_and_unique` from `task-7.md` and these 2 new `verdict_lines` tests,
none of which take `doctor_bench`).

```bash
git add src/railctl/station/doctor.py tests/station/test_doctor.py
git commit -m "feat(station): add the doctor verdict block"
```

- [ ] **Step 5: Write and pass the exit-code tests**

```python
def test_exit_code_for_report_is_zero_when_ok():
    caps = Capabilities.unknown("test")
    checks = tuple(Check(cid, CHECK_TITLES[cid], "ok", "") for cid in ("D0", "D1", "D2"))
    report = DoctorReport(checks=checks, capabilities=caps)
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_exit_code_for_report_is_three_when_d1_failed():
    caps = Capabilities.unknown("test")
    checks = (
        Check("D0", CHECK_TITLES["D0"], "ok", ""),
        Check("D1", CHECK_TITLES["D1"], "fail", "no reply"),
        Check("D2", CHECK_TITLES["D2"], "ok", ""),
    )
    report = DoctorReport(checks=checks, capabilities=caps)
    assert report.ok is False
    assert exit_code_for_report(report) == 3
```

`Check` needs importing in the test file: add `Check` to the `railctl.station.types` import line
alongside `DoctorReport`.

Run: `uv run pytest tests/station/test_doctor.py -k exit_code`
Expected: `2 passed` already - `exit_code_for_report` was written in full back in `task-7.md`'s Step 2
and never changed since. This step exists to pin it explicitly rather than leave it covered only
incidentally by `test_d3_unpowered_without_power_on_is_unknown_not_a_failure`.

- [ ] **Step 6: Wire `Station.probe()` and the package exports**

Read `src/railctl/station/facade.py` before editing it - Task 2 has by this point added `version`,
`status`, `exchange`, `record`, `link`, `capabilities`, `threshold` and `default_address`. Add the
`probe` method after `capabilities` (or wherever the facade's method ordering convention from
earlier tasks places public operations):

```python
    def probe(
        self,
        *,
        address: int | None = None,
        allow_power_on: bool = False,
        use_programming_track: bool = True,
    ) -> DoctorReport:
        # Imported here, not at module level: doctor.py imports Station only
        # under TYPE_CHECKING, but facade.py importing doctor.py at module
        # level would need doctor.py to import facade.py for a real (not
        # type-only) Station reference somewhere else first - keeping this
        # one import lazy avoids finding out the hard way which of the two
        # modules a future refactor makes load first.
        from railctl.station.doctor import run_probe

        return run_probe(
            self,
            address=address,
            allow_power_on=allow_power_on,
            use_programming_track=use_programming_track,
        )
```

`DoctorReport` needs importing in `facade.py` for the return type annotation - add `from
railctl.station.types import DoctorReport` to its existing `station.types` import line (it already
imports several names from there for `cv_read`/`cv_write`'s annotations).

Update `src/railctl/station/__init__.py`'s existing `__all__`-plus-imports block (Task 1 already
established the pattern of one import line and one `__all__` entry per re-exported name):

```python
from railctl.station.doctor import exit_code_for_report, run_probe, verdict_lines
```

Add `"exit_code_for_report"`, `"run_probe"` and `"verdict_lines"` to `__all__`, keeping it sorted
alongside Task 1's existing entries. `Check` and `DoctorReport` are already re-exported from
`__init__.py` since Task 1 - do not add them a second time.

```python
def test_station_probe_delegates_to_run_probe(doctor_bench):
    report = doctor_bench.station.probe(address=50)
    assert isinstance(report, DoctorReport)
    assert report.check("D0") is not None


def test_run_probe_verdict_lines_and_exit_code_for_report_are_exported_from_the_station_package():
    from railctl.station import (
        exit_code_for_report as exported_exit_code_for_report,
        run_probe as exported_run_probe,
        verdict_lines as exported_verdict_lines,
    )

    assert exported_run_probe is run_probe
    assert exported_verdict_lines is verdict_lines
    assert exported_exit_code_for_report is exit_code_for_report
```

Run: `uv run pytest tests/station/test_doctor.py -k "station_probe or exported_from"`
Expected: `3 passed` (`test_station_probe_delegates_to_run_probe` counted twice for `chunk_size`, plus
the plain export-identity test counted once).

Run the whole file: `uv run pytest tests/station/test_doctor.py`
Expected: `80 passed` - `2 * 37 + 6`: 37 bench-parametrised test functions (`task-7.md` through this
step) doubled for the two `chunk_size` ids, plus 6 plain ones
(`test_check_ids_are_thirteen_and_unique`, the two `verdict_lines` tests, the two exit-code tests,
and the export-identity test here).

- [ ] **Step 7: Run the whole `tests/station/` suite and the coverage gate**

Run: `uv run pytest tests/station/`
Expected: PASS across both `chunk_size` ids (`whole-frame`, `byte-at-a-time`) and the one
`envelope_factory` id `tests/station/conftest.py` currently defines. This file's own contribution is
`2 * 37 + 6 = 80`; add whatever Tasks 1-6 already contributed on `main` and treat any smaller total as
a real signal, not an off-by-one in this plan - a *different failing test* is the signal that matters,
never a bare number by itself.

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: `Required test coverage of 90% reached.` `src/railctl/station/doctor.py` should show at or
near 100% - every `if`/`except` branch across all five files of this task has a dedicated test. If any
line is uncovered, it is one of this task's own new lines (Tasks 1-6 are already green on `main`); add
the missing scenario rather than lowering the gate.

- [ ] **Step 8: Ruff**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both report no issues. If `ruff format --check` fails, run `uv run ruff format .` and
re-review the diff before committing - do not blind-commit a formatter's rewrite of a docstring you
have not re-read.

- [ ] **Step 9: Commit**

```bash
git add src/railctl/station/doctor.py src/railctl/station/facade.py src/railctl/station/__init__.py tests/station/test_doctor.py
git commit -m "feat(station): wire Station.probe() and export the doctor verdict helpers"
```

This closes Task 7 (`task-7.md` through `task-7e.md`) and M5. M5's verification sentence - the whole
`tests/station/` suite passing under both `chunk_size` ids and both envelope parameters, and
`tests/hardware/test_m5_acceptance.py` moving and stopping one locomotive - depends on the CLI task
(Task 12) still to come, which prints `verdict_lines(report)` and maps
`exit_code_for_report(report)` to the process exit status; this task's job ends at
`Station.probe()` returning a correct, fully-populated `DoctorReport`.

---

### Task 8: The CLI output contract: one result object, three renderings, one error object

**Design specification:** `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 1221-1230
(L1, output mode / colour / non-interactive rules), 1300-1348 (L4, the JSON envelope, the error
object, NDJSON), 1356-1362 (L6, the exit-code table and its consequences). Read those before
writing a line here - every literal value below is quoted from them, not invented.

**Files:**
- Create: `src/railctl/cli/__init__.py`, `src/railctl/cli/result.py`, `src/railctl/cli/render.py`,
  `src/railctl/cli/_errors.py`, `tests/cli/test_format_modes.py`, `tests/cli/test_errors.py`
- Modify: `src/railctl/errors.py` (insert two new exception classes, `AbortedError` and
  `ConfirmationRequiredError`, immediately before the `EXIT_CODES` literal - anchored on the
  literal's own text, never a line number, because Task 4 already edited this file once to add
  the `details` keyword to `RailctlError.__init__` and `ProgrammingError.__init__` - and add one
  row to that literal for `ConfirmationRequiredError`), `tests/unit/test_exit_codes.py` (add two
  imports at lines 11-42 and two parametrize rows at lines 52-71 and 77-92)

**Interfaces:**

- Consumes:
  - `railctl.errors.RailctlError(message: str, *, hint: str | None = None, details:
    dict[str, object] | None = None)` - `.hint` is the optional hint, `.details` defaults to `{}`
    (never `None`), `str(exc)` is the message alone. `ProgrammingError.__init__` carries the same
    `details` keyword, ordered LAST after `hint` and `cv`. Task 4 added both - it is the first
    task that needs `details` (its three `pom_read` failure paths pass `details={...}`) - so this
    task only consumes the keyword, in Step 12's `DecoderNotRespondingError(..., details=...)`
    test, which is what goes red if it is missing, because a script needs `{"cv": 8, "address": 3,
    "mode": "pom", "attempts": 3}` in the error envelope to tell a first silent attempt from a
    third.
  - `railctl.errors.EXIT_CODES: Final[dict[type[RailctlError], int]]` and
    `exit_code_for(exc: BaseException) -> int` (walks `type(exc).__mro__`, returns
    `UNMAPPED_EXIT_CODE` for anything unmatched) and `UNMAPPED_EXIT_CODE: Final[int] = 1`
    (all verified on disk, lines 160-193)
  - The whole exception tree as it stands on disk today: `TransportError`(3), `PortBusy(TransportError)`,
    `ProtocolError`(4), `LinkTimeout`(5), `UnsupportedCommandError`(6), `UnsupportedFeatureError`(7),
    `StationError(RailctlError)` (no row, resolves to 9), `TrackPowerError(StationError)`(20),
    `ProgrammingError(StationError)`(19, carries `.cv: int | None`), `DecoderNoAckError`(10),
    `ShortCircuitError`(11), `StationBusyError`(12), `DecoderNotRespondingError`(13),
    `CvVerifyError`(14), `CvOutOfRangeError`(15), `PomReadUnsupportedError`(16),
    `IndexPageRequiredError`(17)
  - `railctl.station.capabilities.Capabilities` (tri-state fields such as `.pom_read: bool | None`,
    `.service_direct_cv: bool | None`) and `Check` / `DoctorReport`, imported as
    `from railctl.station import Check, DoctorReport` - **never** `railctl.station.facade`, which
    does not export them, and never `railctl.station.types`, which is the inside view. Both are
    re-exported from the `station` package's `__init__.py` by Task 1. Built by an earlier task in
    this plan; used here only to prove `tri_state` and the envelope generalise to real domain data,
    not a synthetic bool
  - `railctl.station.types.EVENT_NAMES` - twelve station event names as a tuple: seven diagnostic
    names (five from Task 1, plus `cv.unexercised_band` from Task 5 and `function.group_seeded`
    from Task 3) and five station-state names the facade emits and `monitor` renders (`power.on`,
    `power.off`, `loco.emergency_stop`, `service.entered`, `reply.unknown`), all present from
    Task 1 onward per the authoritative tuple; the test file imports this rather than retyping the
    twelve strings, so it cannot drift from the station module
  - `typer` - only `typer.Exit(code: int)`, which carries `.exit_code`. No Typer app, no `typer.echo`,
    no command registration happens in this task.

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

  `src/railctl/errors.py` (additions only - `RailctlError.__init__` and `ProgrammingError.__init__`
  already carry `details`, ordered last; Task 4 added both):
  ```python
  class AbortedError(RailctlError):
      """The operator interrupted the run. Cleanup ran; exit 9."""

  class ConfirmationRequiredError(RailctlError):
      """A confirmation was needed and could not be asked for."""

  # ConfirmationRequiredError: 2 is written as a row inside the EXIT_CODES dict literal itself,
  # never via EXIT_CODES[ConfirmationRequiredError] = 2 after the fact.
  ```

  `src/railctl/cli/result.py`:
  ```python
  Format = Literal["human", "json", "ndjson"]
  ERROR_SCHEMA: Final[str] = "railctl/error/v1"
  RETRYABLE_CODES: Final[frozenset[str]] = frozenset({"link_timeout", "station_busy", "port_busy"})
  USAGE_EXIT_CODE: Final[int] = 2
  INTERNAL_EXIT_CODE: Final[int] = 1

  @dataclass(frozen=True, slots=True)
  class ResultWarning:
      name: str; message: str; details: dict[str, object] = field(default_factory=dict)

  @dataclass(frozen=True, slots=True)
  class LinkInfo:
      identity: str; target: str

  @dataclass(frozen=True, slots=True)
  class StationInfo:
      protocol: str; protocol_version: str | None; command_station_id: int | None

  @dataclass
  class CommandResult:
      schema: str
      command: str
      ok: bool = True
      exit_code: int = 0
      elapsed_ms: int = 0
      link: LinkInfo | None = None
      station: StationInfo | None = None
      warnings: list[ResultWarning] = field(default_factory=list)
      result: dict[str, object] = field(default_factory=dict)
      lines: list[str] = field(default_factory=list)
      def warn(self, name: str, message: str, **details: object) -> None: ...
      def say(self, line: str) -> None: ...
      def envelope(self) -> dict[str, object]: ...

  @dataclass(frozen=True, slots=True)
  class ErrorReport:
      code: str; message: str; retryable: bool; exit_code: int
      details: dict[str, object] = field(default_factory=dict)
      suggestions: list[list[str]] = field(default_factory=list)
      hint: str | None = None
      def envelope(self) -> dict[str, object]: ...

  def error_code(exc: BaseException) -> str: ...      # CamelCase -> snake_case, trailing _error dropped
  def tri_state(value: bool | None) -> Literal["yes", "no", "unknown"]: ...
  ```

  `src/railctl/cli/render.py`:
  ```python
  def want_color(choice: str, stream: TextIO, env: Mapping[str, str]) -> bool: ...
  def render(result: CommandResult, *, fmt: Format, stdout: TextIO, color: bool) -> None: ...
  def render_error(report: ErrorReport, *, stderr: TextIO, fmt: Format, color: bool) -> None: ...

  class NdjsonStream:
      def __init__(self, stream: TextIO) -> None: ...
      sequence: int
      def event(self, type_: str, **fields: object) -> None: ...
      def summary(self, **fields: object) -> None: ...
  ```

  `src/railctl/cli/_errors.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class OutputContext:
      fmt: Format; color: bool; stdout: TextIO; stderr: TextIO

  def report_for(exc: BaseException, *, command: str,
                 details: dict[str, object] | None = None,
                 suggestions: list[list[str]] | None = None) -> ErrorReport: ...
  def default_suggestions(exc: BaseException, *, command: str,
                          address: int | None = None, cv: int | None = None) -> list[list[str]]: ...
  def run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn: ...
  ```

**Notes the implementer must not re-derive:**

- **No "TTY" anywhere under `cli/`.** `tests/test_layering.py` rule 1 matches `\btty` case-insensitively,
  including inside a comment or docstring. Write "terminal" in prose and `stream.isatty()` in code -
  `isatty` and `pretty` have no word boundary before `tty` and are safe. This is the mistake that bites
  in a docstring, not in code, so re-read every comment you write in `render.py` before moving on.
- **This task is the first to create `src/railctl/cli/`.** `tests/test_layering.py::test_the_rule_1_and_2_targets_are_scanned_once_they_exist`
  currently passes only because `cli/` does not exist; the moment these four files land, that test's
  `not target.exists() or _python_files(rel)` branch flips, and rule 1 / rule 2 start scanning real
  content for the first time. Step 16 below runs the whole file and is not optional.
- **Almost everything in this task is a pure function of its arguments.** `error_code`, `tri_state`,
  `want_color` and `NdjsonStream` touch no clock and read no environment variable; `run()`'s one
  documented exception is the `RAILCTL_VERBOSE` check inside `_verbose()`, and that is deliberately
  the only place in this package that reads `os.environ` directly.
- **Colour is applied by `render()` alone, never baked into `CommandResult.lines`.** The same
  `CommandResult` renders identically with `color=True` or `color=False` once you strip the escape
  codes - Step 10's implementation writes ANSI only around the human-format status/warning lines, and
  the JSON and NDJSON branches never call the painting helper at all, colour argument or not. Step 11
  pins this by rendering the SAME `CommandResult` to `json` with `color=True` and asserting no escape
  byte appears - the naive bug this catches is a `render()` that paints unconditionally.
  `stdout` carries the result only; `stderr` carries every diagnostic. No command may write to
  `stdout` directly - only `render()` does, with exactly one exception: Task 12's `monitor` command
  is the single one allowed to write to `ctx.stdout` outside `render()`, because it is the only
  streaming command in this plan - every event it prints as it arrives is one line `render()` never
  gets a chance to buffer. Every other command's stdout goes through `render()` alone. Every later
  command module (Tasks 9-12) exposes a pure `build_<command>(...) -> CommandResult` taking only
  facade objects and plain arguments; the Typer function calls it, then calls `render()`, and does
  no other I/O. Say this in `result.py`'s module docstring, because Tasks 9-12 each need the
  reminder and none of them see this task's file.
- **`ok` is not what scripts branch on.** `ok` means the command did what it was asked; a script
  branches on `exit_code`. Both live in the envelope on purpose - do not collapse one into the other.
- **`ConfirmationRequiredError` takes no `suggestions` parameter.** It stays a bare
  `RailctlError` subclass with only the inherited `(message, *, hint=None, details=None)` - do not
  give it its own `__init__` with a `suggestions` keyword. Task 9's `confirm()` raises it with only
  a message; `default_suggestions` is what supplies `[["railctl", *command.split(), "--yes"]]`
  (Step 14 below), so the argv never needs to be threaded through the exception's constructor.
- **`EXIT_CODES` stays a module-level literal.** Add the `ConfirmationRequiredError: 2` row inside the
  dict literal itself, not via `EXIT_CODES[ConfirmationRequiredError] = 2` after the fact - a later
  test reads the literal's source, not a mutated runtime object, and mutating it after definition would
  make `test_no_entry_in_the_map_is_orphaned` pass for the wrong reason if the literal were ever copied.
  `AbortedError` gets **no** row: it inherits the base 9 from `RailctlError`, exactly like `StationError`
  does today - do not add `EXIT_CODES[AbortedError] = 9`, that would be a correct-looking duplicate the
  next reader has to reconcile against the "no code is duplicated" rule.
- **`KeyboardInterrupt` is never added to `EXIT_CODES`.** The dict is typed to `type[RailctlError]`,
  and `run()` converts a `KeyboardInterrupt` to an `AbortedError` instance before it ever reaches
  `report_for` - the interrupt itself never touches the map.

- [ ] **Step 1: Write the failing tests for the two new exit-code rows**

Edit `tests/unit/test_exit_codes.py`. Add the two new names to the import block (alphabetical, matching
the existing style):

```python
from railctl.errors import (
    EXIT_CODES,
    UNMAPPED_EXIT_CODE,
    AbortedError,
    AmbiguousPort,
    ConfirmationRequiredError,
    CvOutOfRangeError,
    CvVerifyError,
    DecoderNoAckError,
    DecoderNotRespondingError,
    IndexPageRequiredError,
    LinkProtocolError,
    LinkTimeout,
    PomReadUnsupportedError,
    PortBusy,
    PortConfigError,
    PortNotFound,
    PortNotOpen,
    PortNotXpressNet,
    ProgrammingError,
    ProtocolError,
    RailctlError,
    ShortCircuitError,
    StationBusyError,
    StationError,
    TrackPowerError,
    TransportError,
    UnsupportedCommandError,
    UnsupportedFeatureError,
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    exit_code_for,
)
```

Add `(ConfirmationRequiredError("x"), 2)` to the end of the `test_every_documented_exit_code_row`
parametrize list (it has its own row, like `TrackPowerError`):

```python
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TransportError("x"), 3),
        (ProtocolError("x"), 4),
        (LinkTimeout("x"), 5),
        (UnsupportedCommandError("x"), 6),
        (UnsupportedFeatureError("x"), 7),
        (RailctlError("x"), 9),
        (DecoderNoAckError("x"), 10),
        (ShortCircuitError("x"), 11),
        (StationBusyError("x"), 12),
        (DecoderNotRespondingError("x"), 13),
        (CvVerifyError("x"), 14),
        (CvOutOfRangeError("x"), 15),
        (PomReadUnsupportedError("x"), 16),
        (IndexPageRequiredError("x"), 17),
        (ProgrammingError("x"), 19),
        (TrackPowerError("x"), 20),
        (ConfirmationRequiredError("x"), 2),
    ],
)
def test_every_documented_exit_code_row(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code
```

Add `(AbortedError("x"), 9)` to the end of the "inherits the parent code" parametrize list - it has
no row of its own, exactly like `StationError`:

```python
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PortNotFound("x"), 3),
        (AmbiguousPort("x"), 3),
        (PortBusy("x"), 3),
        (PortConfigError("x"), 3),
        (PortNotOpen("x"), 3),
        (PortNotXpressNet("x"), 3),
        (XBusEncodeError("x"), 4),
        (XBusDecodeError("x"), 4),
        (XBusChecksumError("x"), 4),
        (LinkProtocolError("x"), 4),
        (StationError("x"), 9),
        (AbortedError("x"), 9),
    ],
)
def test_subclasses_without_their_own_row_inherit_the_parent_code(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code
```

No other test in the file needs editing: `test_no_entry_in_the_map_is_orphaned`,
`test_the_map_has_no_duplicate_codes`, `test_every_class_in_the_tree_resolves_to_a_code_above_one` and
`test_errors_is_the_only_module_defining_exception_types` all walk `RailctlError.__subclasses__()` at
runtime, so they pick up the two new classes automatically once they exist.

- [ ] **Step 2: Run the test file and read the failure**

Run: `uv run pytest tests/unit/test_exit_codes.py`

Expected: FAIL - `ImportError: cannot import name 'AbortedError' from 'railctl.errors'`. This is a
collection error, not a test failure, so nothing runs; that is the correct red for a missing name.

- [ ] **Step 3: Add `AbortedError`, `ConfirmationRequiredError` and the new `EXIT_CODES` row to
      `errors.py`**

`RailctlError.__init__` and `ProgrammingError.__init__` already carry the `details` keyword by
this point - Task 4 added it, being the first task that needs it (its three `pom_read` failure
paths pass `details={...}`), with `details` ordered LAST on both signatures. This step does not
touch either `__init__` again; it adds only the two new exception classes and the exit-code row,
none of which depends on anything but `RailctlError` itself. No test in this task's own
`test_exit_codes.py` exercises `.details` in isolation (that file only walks the exit-code map);
Step 12's `run()` tests are where a missing keyword would go red, with a `TypeError`, not a
`ModuleNotFoundError` - and Task 4's own tests are what actually pin the keyword.

Anchor the edit on the `EXIT_CODES` literal's own text - `EXIT_CODES: Final[dict[type[RailctlError], int]] = {`
through its closing `}`, immediately after `class IndexPageRequiredError` - never on a line
number: Task 4 already edited this file once, so any line number quoted here would already be
stale by the time this task runs.

Replace that block with:

```python
class AbortedError(RailctlError):
    """The operator interrupted the run. Cleanup ran; exit 9."""


class ConfirmationRequiredError(RailctlError):
    """A confirmation was needed and could not be asked for."""


EXIT_CODES: Final[dict[type[RailctlError], int]] = {
    TransportError: 3,
    ProtocolError: 4,
    LinkTimeout: 5,
    UnsupportedCommandError: 6,
    UnsupportedFeatureError: 7,
    RailctlError: 9,
    DecoderNoAckError: 10,
    ShortCircuitError: 11,
    StationBusyError: 12,
    DecoderNotRespondingError: 13,
    CvVerifyError: 14,
    CvOutOfRangeError: 15,
    PomReadUnsupportedError: 16,
    IndexPageRequiredError: 17,
    ProgrammingError: 19,
    TrackPowerError: 20,
    ConfirmationRequiredError: 2,
}
```

`AbortedError` is placed above the dict, not inside it, on purpose: it has no row, so it reads next
to its sibling classes rather than next to a comment explaining an absence. `ConfirmationRequiredError`
gets 2 because both cases it is raised for - a confirmation the operator cannot be asked for, and a
malformed CLI argument (`ValueError` in `run()`, Step 14) - are the same category from a script's
point of view: fix the invocation, do not retry.

- [ ] **Step 4: Run the test file and see it pass**

Run: `uv run pytest tests/unit/test_exit_codes.py`

Expected: PASS, 39 passed, 0 failed (37 before this task: 16 + 11 + 9 on disk, plus Task 4's
`details` round-trip test; +1 row in each of the two parametrize lists brings it to
17 + 13 + 9 = 39).

- [ ] **Step 5: Create the `cli` package**

```python
# src/railctl/cli/__init__.py
"""railctl.cli - Typer commands, output rendering, and exception-to-exit-code mapping.

Nothing under this package touches an X-Bus opcode, a framing byte, a port name or a network
address - `tests/test_layering.py` rule 1 enforces that mechanically. Every command talks to a `Station`
facade object and to the modules in this package: `result` for the one shared envelope type,
`render` for turning it into bytes, `_errors` for the exception-to-exit-code decorator that every
command function is wrapped in.
"""

from __future__ import annotations
```

- [ ] **Step 6: Write the failing tests for `CommandResult`, `ErrorReport`, `error_code` and `tri_state`**

```python
# tests/cli/test_format_modes.py
"""Pins the CLI output contract: one CommandResult, three renderings, one shared shape.

Every assertion here answers a question a script author or another agent would ask before
trusting this tool's output: does `json.loads` succeed with nothing else on the stream, does a
fact I put in `result` also show up in the text a human reads, does `None` ever get silently
turned into "no", does colour ever leak into a format meant to be machine-parsed.
"""

from __future__ import annotations

import io
import json

import pytest

from railctl.cli.render import NdjsonStream, render, want_color
from railctl.cli.result import (
    CommandResult,
    ErrorReport,
    LinkInfo,
    ResultWarning,
    StationInfo,
    error_code,
    tri_state,
)
from railctl.station import Check, DoctorReport
from railctl.station.capabilities import Capabilities
from railctl.station.types import EVENT_NAMES


def test_tri_state_never_renders_none_as_empty_or_dash_or_no():
    """The whole project exists to keep "unknown" from collapsing into "no". This is the
    one-line version of that rule, for the tri-state helper every capability rendering uses.
    """
    assert tri_state(None) == "unknown"
    assert tri_state(False) == "no"
    assert tri_state(True) == "yes"


def test_tri_state_tracks_a_real_capability_field_not_just_a_synthetic_bool():
    caps = Capabilities.unknown("serial:test:0")
    assert tri_state(caps.pom_read) == "unknown"
    learned_true = caps.with_learned(pom_read=True)
    learned_false = caps.with_learned(pom_read=False)
    assert tri_state(learned_true.pom_read) == "yes"
    assert tri_state(learned_false.pom_read) == "no"


def test_a_doctor_capability_keeps_its_tri_state_in_json_and_uses_the_helper_only_for_text():
    """The value that goes in `result` (and so into JSON) is the raw bool | None, never the
    tri_state() string - `tri_state()` is for the human line only. Storing "unknown" as the
    JSON value would be exactly the M1 failure mode: a capability gap that a script cannot
    distinguish from the literal string "unknown" some other field might legitimately hold.
    """
    caps = Capabilities.unknown("serial:test:0")
    report = DoctorReport(
        checks=(Check(id="D0", title="link", status="ok", detail="opened"),),
        capabilities=caps,
    )
    result = CommandResult(schema="railctl/doctor/v1", command="doctor")
    result.result["pom_read"] = report.capabilities.pom_read
    result.say(f"POM read: {tri_state(report.capabilities.pom_read)}")
    body = result.envelope()["result"]
    assert body["pom_read"] is None
    assert result.lines == ["POM read: unknown"]


def test_error_code_is_reexported_from_result():
    # error_code has its own dedicated coverage in tests/cli/test_errors.py; this only
    # confirms result.py re-exports it, since render.py and _errors.py both import it from here.
    # Breaks if error_code is moved out of result.py without updating this import, or if the
    # function stops handling a plain (non-RailctlError) exception the same way.
    class Boom(Exception):
        pass

    assert error_code(Boom("x")) == "boom"


def test_warn_appends_a_result_warning_with_its_details():
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.warn("cv.stale_result", "a stale reply arrived", cv=254, echoed=7)
    assert result.warnings == [
        ResultWarning(
            name="cv.stale_result",
            message="a stale reply arrived",
            details={"cv": 254, "echoed": 7},
        )
    ]


def test_say_appends_a_human_readable_line():
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.say("track power: on")
    assert result.lines == ["track power: on"]


def test_envelope_key_order_and_omits_link_and_station_when_none():
    result = CommandResult(schema="railctl/status/v1", command="status")
    body = result.envelope()
    assert list(body.keys()) == [
        "schema", "ok", "command", "exit_code", "elapsed_ms", "warnings", "result",
    ]
    assert "link" not in body
    assert "station" not in body


def test_envelope_includes_link_and_station_in_order_when_present():
    result = CommandResult(
        schema="railctl/status/v1",
        command="status",
        link=LinkInfo(identity="serial:7010A0001194:3", target="serial:/dev/cu.usbmodem7010A00011943"),
        station=StationInfo(protocol="xpressnet", protocol_version="4.0", command_station_id=18),
    )
    body = result.envelope()
    assert list(body.keys()) == [
        "schema", "ok", "command", "exit_code", "elapsed_ms",
        "link", "station", "warnings", "result",
    ]
    assert body["link"] == {
        "identity": "serial:7010A0001194:3",
        "target": "serial:/dev/cu.usbmodem7010A00011943",
    }
    assert body["station"] == {
        "protocol": "xpressnet", "protocol_version": "4.0", "command_station_id": 18,
    }


def test_json_mode_stdout_holds_exactly_one_json_value():
    """color=True on purpose: JSON must never carry an escape code regardless of the flag -
    colour is a human-format-only concern, and this is the naive bug that would slip through
    if render() painted before checking the format.
    """
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.result["track_power"] = True
    result.say("track power: on")
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=True)
    text = out.getvalue()
    parsed = json.loads(text)  # succeeds only if stdout holds nothing but the one value
    assert parsed["schema"] == "railctl/status/v1"
    assert parsed["result"]["track_power"] is True
    assert "\x1b" not in text


def test_human_and_json_carry_the_same_facts():
    """No command may put a fact in one rendering only. render() is the single place both
    are produced from one CommandResult, and this is the test that would fail if a command
    module wrote a number into `.say()` without also putting it in `.result`, or vice versa.
    """
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.result["value"] = 131
    result.say("CV 1 = 131")
    human_out = io.StringIO()
    render(result, fmt="human", stdout=human_out, color=False)
    json_out = io.StringIO()
    render(result, fmt="json", stdout=json_out, color=False)
    assert "131" in human_out.getvalue()
    assert json.loads(json_out.getvalue())["result"]["value"] == 131


@pytest.mark.parametrize("name", EVENT_NAMES)
def test_every_station_event_name_renders_in_both_formats(name: str):
    """Imports EVENT_NAMES rather than retyping the twelve strings, so a thirteenth event added
    to station/types.py in a later milestone is exercised here with no edit to this file.
    """
    result = CommandResult(schema="railctl/cv-read/v1", command="cv read")
    result.warn(name, f"test message for {name}")
    human_out = io.StringIO()
    render(result, fmt="human", stdout=human_out, color=False)
    json_out = io.StringIO()
    render(result, fmt="json", stdout=json_out, color=False)
    assert name in human_out.getvalue()
    assert json.loads(json_out.getvalue())["warnings"][0]["name"] == name


class _Stream(io.StringIO):
    def __init__(self, *, terminal: bool) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


def test_want_color_false_when_the_stream_is_not_a_terminal():
    assert want_color("auto", _Stream(terminal=False), {}) is False


def test_want_color_false_when_no_color_is_set_to_any_non_empty_value():
    assert want_color("auto", _Stream(terminal=True), {"NO_COLOR": "1"}) is False


def test_want_color_false_when_term_is_dumb():
    assert want_color("auto", _Stream(terminal=True), {"TERM": "dumb"}) is False


def test_want_color_always_wins_over_no_color():
    assert want_color("always", _Stream(terminal=False), {"NO_COLOR": "1"}) is True


def test_want_color_decides_stdout_and_stderr_independently():
    stdout_decision = want_color("auto", _Stream(terminal=True), {})
    stderr_decision = want_color("auto", _Stream(terminal=False), {})
    assert stdout_decision is True
    assert stderr_decision is False


def test_ndjson_stream_numbers_from_zero_and_writes_compact_lines():
    buf = io.StringIO()
    stream = NdjsonStream(buf)
    stream.event("start", total=3)
    stream.event("cv", cv=1, value=131)
    stream.summary(requested=3, ok=1)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3
    first, second, third = (json.loads(line) for line in lines)
    assert (first["type"], first["sequence"]) == ("start", 0)
    assert second["sequence"] == 1
    assert (third["type"], third["sequence"]) == ("summary", 2)
    assert ", " not in lines[0] and ": " not in lines[0]  # compact separators, no spaces


def test_ndjson_summary_is_always_the_last_line_even_after_a_raised_exception():
    buf = io.StringIO()
    stream = NdjsonStream(buf)
    try:
        stream.event("start", total=1)
        raise RuntimeError("mid-run failure")
    except RuntimeError:
        pass
    finally:
        stream.summary(complete=False, exit_code=9)
    last = json.loads(buf.getvalue().splitlines()[-1])
    assert last["type"] == "summary"


def test_ndjson_mode_render_emits_exactly_one_summary_line_for_a_plain_result():
    result = CommandResult(schema="railctl/status/v1", command="status")
    result.result["track_power"] = True
    out = io.StringIO()
    render(result, fmt="ndjson", stdout=out, color=False)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["type"] == "summary"
    assert body["sequence"] == 0
    assert body["result"]["track_power"] is True
```

- [ ] **Step 7: Run and see it fail on the missing `result` module**

Run: `uv run pytest tests/cli/test_format_modes.py`

Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.cli.result'`

- [ ] **Step 8: Implement `src/railctl/cli/result.py`**

```python
# src/railctl/cli/result.py
"""The one result object every command builds, and nothing else writes to stdout with.

`CommandResult` is the object the review criterion "one result object, three renderings"
points at. `railctl.cli.render.render()` is the only function that turns it into bytes - no
command writes to `stdout` directly. Every command module from Task 9 onward exposes a pure
`build_<command>(...) -> CommandResult` that takes only facade objects (a `Station`, a
`Capabilities`, plain arguments) and returns a `CommandResult`; it opens no file, prints
nothing, reads no environment variable. The Typer function's only job is: parse argv, call
`build_*`, hand the result to `railctl.cli._errors.run()`. Splitting the object from its
renderings is what lets a test build one `CommandResult` and assert the same fact appears in
both the human text and the JSON body (design L4) - a fact recorded in only one of the two is
exactly the kind of drift this split exists to make structurally impossible.

`ERROR_SCHEMA` and every command's own `railctl/<command>/v1` string are a versioned public
contract (design L4): within a major version only optional fields may be added to an envelope;
removing a field, renaming one, or changing its type or unit needs a new `v2` schema string -
never a silent edit to what `v1` means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

Format = Literal["human", "json", "ndjson"]

ERROR_SCHEMA: Final[str] = "railctl/error/v1"

# Exactly these three: a script that retries on any other code is retrying a real answer
# (UnsupportedCommandError) or a bug (everything else), neither of which gets better on retry.
RETRYABLE_CODES: Final[frozenset[str]] = frozenset({"link_timeout", "station_busy", "port_busy"})

USAGE_EXIT_CODE: Final[int] = 2
INTERNAL_EXIT_CODE: Final[int] = 1

_TRAILING_ERROR = re.compile(r"Error$")
_WORD_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def error_code(exc: BaseException) -> str:
    """`PomReadUnsupportedError` -> `"pom_read_unsupported"`; `LinkTimeout` -> `"link_timeout"`.

    Only a trailing "Error" is stripped, not "Timeout": the exit-code table's own class names
    are the source of truth here, and `LinkTimeout` reads as "link_timeout" everywhere else in
    this project's docs, never "link". Word boundaries are inserted before every capital that
    is not the first character, then the whole name is lowercased - this is what turns
    `CvOutOfRangeError` into `cv_out_of_range` and `PortBusy` (no "Error" suffix at all) into
    `port_busy` with the same one rule, rather than a lookup table that silently misses a class.
    """
    name = _TRAILING_ERROR.sub("", type(exc).__name__)
    return _WORD_BOUNDARY.sub("_", name).lower()


def tri_state(value: bool | None) -> Literal["yes", "no", "unknown"]:
    """`None` -> `"unknown"`, never `""`, `"-"` or `"no"`.

    This is the one-line version of the failure mode the whole project exists to avoid: a
    capability the doctor never probed must never render the same way as one it probed and
    found absent.
    """
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@dataclass(frozen=True, slots=True)
class ResultWarning:
    name: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LinkInfo:
    identity: str
    target: str


@dataclass(frozen=True, slots=True)
class StationInfo:
    protocol: str
    protocol_version: str | None
    command_station_id: int | None


@dataclass
class CommandResult:
    schema: str
    command: str
    ok: bool = True
    exit_code: int = 0
    elapsed_ms: int = 0
    link: LinkInfo | None = None
    station: StationInfo | None = None
    warnings: list[ResultWarning] = field(default_factory=list)
    result: dict[str, object] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def warn(self, name: str, message: str, **details: object) -> None:
        self.warnings.append(ResultWarning(name=name, message=message, details=dict(details)))

    def say(self, line: str) -> None:
        self.lines.append(line)

    def envelope(self) -> dict[str, object]:
        """The JSON body, in the documented key order. `link` and `station` are OMITTED, not
        `null`, when no link was opened - a doctor failure on D0 has no station to describe,
        and an absent key is a smaller claim than a null one.
        """
        body: dict[str, object] = {
            "schema": self.schema,
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.link is not None:
            body["link"] = {"identity": self.link.identity, "target": self.link.target}
        if self.station is not None:
            body["station"] = {
                "protocol": self.station.protocol,
                "protocol_version": self.station.protocol_version,
                "command_station_id": self.station.command_station_id,
            }
        body["warnings"] = [
            {"name": w.name, "message": w.message, "details": w.details} for w in self.warnings
        ]
        body["result"] = self.result
        return body


@dataclass(frozen=True, slots=True)
class ErrorReport:
    code: str
    message: str
    retryable: bool
    exit_code: int
    details: dict[str, object] = field(default_factory=dict)
    suggestions: list[list[str]] = field(default_factory=list)
    hint: str | None = None

    def envelope(self) -> dict[str, object]:
        """5d: `hint` sits between `message` and `retryable`, and is `None` rather than omitted
        when there is none - the same optional-field rule `CommandResult.envelope()` follows,
        because a script and a human must be able to read the same fact off the same object.
        Dropping this key here is exactly how the human rendering (which already prints
        `report.hint` in `render.py`) and the JSON rendering drift apart.
        """
        return {
            "schema": ERROR_SCHEMA,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "exit_code": self.exit_code,
            "details": self.details,
            "suggestions": [list(s) for s in self.suggestions],
        }
```

- [ ] **Step 9: Run again and confirm the remaining gap has moved to `render.py`**

Run: `uv run pytest tests/cli/test_format_modes.py`

Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.cli.render'`. The failure reason
changed from Step 7 (it no longer complains about `result`), which is what proves `result.py`
landed cleanly and the only remaining gap is the renderer.

- [ ] **Step 10: Implement `src/railctl/cli/render.py`**

```python
# src/railctl/cli/render.py
"""Turns one CommandResult or ErrorReport into bytes on a stream. Nothing else in this package
writes to stdout or stderr directly - see result.py's module docstring for why that split matters.

Colour is decided and applied entirely in this module. `CommandResult.lines` never contains an
escape code; `render()` paints a copy of the text at write time, so the same result object
renders identically whether `color` is True or False, and the JSON and NDJSON branches never
call the painting helper at all - JSON output must never carry an escape code regardless of the
`color` argument, because a consumer piping `--format=json` through `jq` is not a terminal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TextIO

from railctl.cli.result import CommandResult, ErrorReport, Format

_RESET = "\x1b[0m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"

_JSON_SEPARATORS = (",", ":")  # compact: no space after "," or ":", in every JSON/NDJSON line


def want_color(choice: str, stream: TextIO, env: Mapping[str, str]) -> bool:
    """`choice` is the resolved `--color` value: `"always"`, `"never"` or `"auto"`.

    `"always"` wins even when `NO_COLOR` is set - an operator who explicitly asked for colour on
    a redirected stream gets it, because the explicit flag is a stronger signal than the
    environment convention it overrides. `NO_COLOR` counts only when it is a non-empty string -
    an unset variable and an empty one must decide the same way, "not set".
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False
    return stream.isatty()


def _paint(text: str, code: str, *, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _render_human(result: CommandResult, *, stdout: TextIO, color: bool) -> None:
    status = "ok" if result.ok else "failed"
    tone = _GREEN if result.ok else _RED
    stdout.write(_paint(f"{result.command}: {status}", tone, color=color) + "\n")
    for warning in result.warnings:
        line = f"warning: {warning.name}: {warning.message}"
        stdout.write(_paint(line, _YELLOW, color=color) + "\n")
    for line in result.lines:
        stdout.write(line + "\n")


def render(result: CommandResult, *, fmt: Format, stdout: TextIO, color: bool) -> None:
    if fmt == "human":
        _render_human(result, stdout=stdout, color=color)
        return
    if fmt == "json":
        stdout.write(json.dumps(result.envelope(), separators=_JSON_SEPARATORS))
        stdout.write("\n")
        return
    # ndjson: one summary line carries the whole envelope. A streaming command (backup,
    # restore, diff) builds its own NdjsonStream directly and never calls this branch; this
    # exists so a non-streaming command can still be asked for --format=ndjson and produce
    # something a line-oriented consumer can parse the same way.
    NdjsonStream(stdout).summary(**result.envelope())


def _render_error_human(report: ErrorReport, *, stderr: TextIO, color: bool) -> None:
    stderr.write(_paint(f"error: {report.message}", _RED, color=color) + "\n")
    if report.hint:
        stderr.write(f"hint: {report.hint}\n")
    for suggestion in report.suggestions:
        stderr.write("try: " + " ".join(suggestion) + "\n")


def render_error(report: ErrorReport, *, stderr: TextIO, fmt: Format, color: bool) -> None:
    """Errors are one JSON object on stderr in EVERY format mode but human - `ndjson` does not
    get its own error shape, because the error object is never part of the ndjson data stream:
    it is a diagnostic, and diagnostics are stderr-only regardless of what stdout is carrying.
    """
    if fmt == "human":
        _render_error_human(report, stderr=stderr, color=color)
        return
    stderr.write(json.dumps(report.envelope(), separators=_JSON_SEPARATORS))
    stderr.write("\n")


class NdjsonStream:
    """One compact JSON object per line, numbered from 0, always ending in a `summary` line -
    even when the caller's `finally` block is the only thing that runs, because a consumer
    that dies mid-run must be able to tell the run ended from the same stream it was reading.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.sequence = 0

    def event(self, type_: str, **fields: object) -> None:
        body: dict[str, object] = {"type": type_, "sequence": self.sequence, **fields}
        self._stream.write(json.dumps(body, separators=_JSON_SEPARATORS))
        self._stream.write("\n")
        self.sequence += 1

    def summary(self, **fields: object) -> None:
        self.event("summary", **fields)
```

- [ ] **Step 11: Run the format-modes test file and see it pass**

Run: `uv run pytest tests/cli/test_format_modes.py`

Expected: PASS, 30 passed, 0 failed (19 test functions; `test_every_station_event_name_renders_in_both_formats`
is parametrized over the twelve names in `EVENT_NAMES` (B.5's authoritative tuple), so it collects
as 18 + 12 = 30).

- [ ] **Step 12: Write the failing tests for `error_code` mapping, suggestions and `run()`**

```python
# tests/cli/test_errors.py
"""Pins the exception-to-exit-code-and-JSON pipeline: railctl.errors -> ErrorReport -> stderr.

The five run() tests are the ones that matter most: they are the only place in this task that
proves KeyboardInterrupt, a bad CLI argument, a domain RailctlError and an honest-to-goodness
bug in this tool all end up with DIFFERENT exit codes and DIFFERENT `code` strings, because a
script reading only `$?` has no other way to tell "I pressed Ctrl-C" from "railctl has a bug".
"""

from __future__ import annotations

import io
import json

import pytest
import typer

from railctl.cli._errors import OutputContext, default_suggestions, report_for, run
from railctl.cli.render import render_error
from railctl.cli.result import CommandResult, ErrorReport, error_code
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    CvVerifyError,
    DecoderNotRespondingError,
    LinkTimeout,
    PomReadUnsupportedError,
    PortBusy,
    RailctlError,
    StationBusyError,
    TrackPowerError,
    UnsupportedCommandError,
)


def _tree(root: type[RailctlError] = RailctlError) -> set[type[RailctlError]]:
    found = {root}
    for sub in root.__subclasses__():
        found |= _tree(sub)
    return found


@pytest.mark.parametrize(
    ("exc_cls", "code"),
    [
        (LinkTimeout, "link_timeout"),
        (UnsupportedCommandError, "unsupported_command"),
        (PomReadUnsupportedError, "pom_read_unsupported"),
        (CvVerifyError, "cv_verify"),
        (TrackPowerError, "track_power"),
        (StationBusyError, "station_busy"),
        (PortBusy, "port_busy"),
        (AbortedError, "aborted"),
    ],
)
def test_error_code_maps_the_documented_names(exc_cls: type[RailctlError], code: str):
    assert error_code(exc_cls.__new__(exc_cls)) == code


def test_every_class_in_the_error_tree_gets_a_unique_code():
    """A whole-tree test, not just the eight pinned above: two exceptions sharing a code would
    let a script mistake one domain failure for another with no way to notice.
    """
    codes = [error_code(k.__new__(k)) for k in _tree()]
    assert len(codes) == len(set(codes)), codes


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (LinkTimeout("x"), True),
        (StationBusyError("x"), True),
        (PortBusy("x"), True),
        (DecoderNotRespondingError("x"), False),
        (CvVerifyError("x"), False),
        (TrackPowerError("x"), False),
        (UnsupportedCommandError("x"), False),
    ],
)
def test_retryable_is_true_for_exactly_three_classes(exc: RailctlError, expected: bool):
    """DecoderNotRespondingError is retryable in feel - retrying a service-mode read is a
    normal thing to do - but not in fact: the station said nothing, and asking again with no
    change of track power or wiring gets the same silence. Only a station that reported BUSY
    (retry will resolve on its own) or a link that timed out or a port that was busy are true.
    """
    assert report_for(exc, command="cv read").retryable is expected


def test_retryable_true_set_is_exactly_link_timeout_station_busy_and_port_busy():
    retryable = {
        k for k in _tree() if report_for(k.__new__(k), command="x").retryable
    }
    assert retryable == {LinkTimeout, StationBusyError, PortBusy}


def test_pom_read_failure_always_suggests_doctor_first():
    """The usual cause of a failed POM read is RailCom off or the track unpowered - both are
    what `doctor` reports first - so `doctor` always leads, even when a same-CV service-mode
    retry is also offered.
    """
    with_cv = report_for(PomReadUnsupportedError("no result for CV 8 on loco 3", cv=8), command="cv read")
    assert with_cv.suggestions[0] == ["railctl", "doctor"]
    assert with_cv.suggestions[1] == ["railctl", "cv", "read", "8", "--mode", "service"]

    without_cv = report_for(PomReadUnsupportedError("pom reads are unsupported here"), command="cv read")
    assert without_cv.suggestions == [["railctl", "doctor"]]


def test_suggestions_are_argv_arrays_never_shell_strings():
    """An agent must be able to subprocess.run(suggestion) with no shell - a single string
    element containing a space would be a shell command line smuggled into a JSON array.
    """
    report = report_for(PomReadUnsupportedError("no result for CV 8", cv=8), command="cv read")
    assert report.suggestions, "fixture exception produced no suggestions to check"
    for suggestion in report.suggestions:
        assert isinstance(suggestion, list)
        for arg in suggestion:
            assert isinstance(arg, str)
            assert " " not in arg


def test_report_for_reads_the_hint_off_the_exception():
    exc = TrackPowerError("track power is off", hint="run `railctl power on`")
    report = report_for(exc, command="drive")
    assert report.hint == "run `railctl power on`"
    assert report.code == "track_power"
    assert report.exit_code == 20


def test_default_suggestions_is_empty_for_an_exception_with_no_known_fix():
    assert default_suggestions(TrackPowerError("track power is off"), command="drive") == []


def test_default_suggestions_offers_yes_for_a_blocked_confirmation():
    exc = ConfirmationRequiredError("restore needs --force on a decoder with pending writes")
    assert default_suggestions(exc, command="restore backup.json") == [
        ["railctl", "restore", "backup.json", "--yes"]
    ]


@pytest.mark.parametrize("fmt", ["human", "json", "ndjson"])
def test_errors_go_to_stderr_in_every_format_mode(fmt: str):
    report = report_for(LinkTimeout("no reply to 21 24 05 within 5.0 s"), command="status")
    err = io.StringIO()
    render_error(report, stderr=err, fmt=fmt, color=False)
    assert err.getvalue() != ""
    if fmt != "human":
        body = json.loads(err.getvalue())
        assert body["code"] == "link_timeout"
        assert body["retryable"] is True


def test_the_json_error_envelope_carries_the_same_hint_the_human_rendering_prints():
    """5d: `_render_error_human` has always printed `report.hint` when set. Before this task's
    fix to `ErrorReport.envelope()`, the JSON branch below dropped it silently - the hint that
    distinguishes "POM is recorded unsupported, use `--mode service`" from a bare refusal would
    reach a human and vanish for a script. Breaks if `envelope()` stops including the `hint` key.
    """
    report = ErrorReport(
        code="track_power",
        message="track power is off",
        retryable=False,
        exit_code=20,
        hint="run `railctl power on`",
    )
    human_out = io.StringIO()
    render_error(report, stderr=human_out, fmt="human", color=False)
    json_out = io.StringIO()
    render_error(report, stderr=json_out, fmt="json", color=False)
    assert "run `railctl power on`" in human_out.getvalue()
    assert json.loads(json_out.getvalue())["hint"] == "run `railctl power on`"


def _ctx(fmt: str = "json") -> OutputContext:
    return OutputContext(fmt=fmt, color=False, stdout=io.StringIO(), stderr=io.StringIO())


def test_run_converts_keyboard_interrupt_to_aborted_exit_9():
    ctx = _ctx()

    def work() -> CommandResult:
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as caught:
        run("stop", ctx, work)
    assert caught.value.exit_code == 9
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body["code"] == "aborted"


def test_run_reports_a_value_error_as_usage_exit_2():
    ctx = _ctx()

    def work() -> CommandResult:
        raise ValueError("speed must be 0..126")

    with pytest.raises(typer.Exit) as caught:
        run("drive", ctx, work)
    assert caught.value.exit_code == 2
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body == {
        "schema": "railctl/error/v1",
        "code": "usage",
        "message": "speed must be 0..126",
        "hint": None,
        "retryable": False,
        "exit_code": 2,
        "details": {},
        "suggestions": [],
    }


def test_run_maps_a_railctl_error_through_exit_code_for():
    ctx = _ctx()

    def work() -> CommandResult:
        raise CvVerifyError("mismatch", cv=8)

    with pytest.raises(typer.Exit) as caught:
        run("cv write", ctx, work)
    assert caught.value.exit_code == 14
    assert ctx.stdout.getvalue() == ""
    body = json.loads(ctx.stderr.getvalue())
    assert body["code"] == "cv_verify"


def test_run_reports_an_unmapped_exception_as_internal_exit_1_without_a_traceback(monkeypatch):
    monkeypatch.delenv("RAILCTL_VERBOSE", raising=False)
    ctx = _ctx()

    def work() -> CommandResult:
        raise RuntimeError("unreachable branch hit")

    with pytest.raises(typer.Exit) as caught:
        run("status", ctx, work)
    assert caught.value.exit_code == 1
    assert ctx.stdout.getvalue() == ""
    stderr_text = ctx.stderr.getvalue()
    assert json.loads(stderr_text)["code"] == "internal"
    assert "Traceback" not in stderr_text


def test_run_prints_the_traceback_only_in_verbose_mode(monkeypatch):
    monkeypatch.setenv("RAILCTL_VERBOSE", "1")
    ctx = _ctx()

    def work() -> CommandResult:
        raise RuntimeError("unreachable branch hit")

    with pytest.raises(typer.Exit):
        run("status", ctx, work)
    assert "Traceback" in ctx.stderr.getvalue()


def test_run_renders_the_result_and_exits_with_its_code():
    ctx = _ctx()

    def work() -> CommandResult:
        result = CommandResult(schema="railctl/status/v1", command="status")
        result.result["track_power"] = True
        return result

    with pytest.raises(typer.Exit) as caught:
        run("status", ctx, work)
    assert caught.value.exit_code == 0
    assert ctx.stderr.getvalue() == ""
    body = json.loads(ctx.stdout.getvalue())
    assert body["result"]["track_power"] is True


@pytest.mark.parametrize("fmt", ["human", "json", "ndjson"])
def test_run_sends_every_format_error_to_stderr_only(fmt: str):
    ctx = _ctx(fmt=fmt)

    def work() -> CommandResult:
        raise TrackPowerError("track power is off")

    with pytest.raises(typer.Exit) as caught:
        run("power on", ctx, work)
    assert caught.value.exit_code == 20
    assert ctx.stdout.getvalue() == ""
    assert ctx.stderr.getvalue() != ""


def test_run_times_the_work_and_reports_a_non_zero_elapsed_ms(monkeypatch):
    """5g: before this task's fix, `run()` never touched `time.monotonic()` at all, so
    `elapsed_ms` stayed at the dataclass default of 0 for every command, forever - a script
    comparing a fast `status` call against a POM read that took three 2s attempts had no way
    to tell them apart. Patches the module's own `time.monotonic`, not the stdlib one, so the
    fixture is exact rather than racing a real clock.
    """
    ticks = iter([100.0, 100.037])
    monkeypatch.setattr("railctl.cli._errors.time.monotonic", lambda: next(ticks))
    ctx = _ctx()

    def work() -> CommandResult:
        return CommandResult(schema="railctl/status/v1", command="status")

    with pytest.raises(typer.Exit):
        run("status", ctx, work)
    body = json.loads(ctx.stdout.getvalue())
    assert body["elapsed_ms"] == 37


def test_a_decoder_not_responding_error_carrying_details_surfaces_them_in_the_envelope():
    """5g: pins the merge order `report_for` documents - `exc.details` first, then `{"cv":
    exc.cv}`, then the caller's own `details=` argument. Breaks if `report_for` stops reading
    `exc.details`, or if it lets an explicit `cv` entry inside `exc.details` silently win over
    the `cv` keyword instead of the reverse.
    """
    ctx = _ctx()
    exc = DecoderNotRespondingError(
        "no result for CV 8 after 3 attempts",
        cv=8,
        details={"address": 3, "mode": "pom", "attempts": 3, "attempt_timeout_s": 2.0},
    )

    def work() -> CommandResult:
        raise exc

    with pytest.raises(typer.Exit) as caught:
        run("cv read", ctx, work)
    assert caught.value.exit_code == 13
    body = json.loads(ctx.stderr.getvalue())
    assert body["details"] == {
        "address": 3,
        "mode": "pom",
        "attempts": 3,
        "attempt_timeout_s": 2.0,
        "cv": 8,
    }
```

- [ ] **Step 13: Run and see it fail on the missing `_errors` module**

Run: `uv run pytest tests/cli/test_errors.py`

Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.cli._errors'`

- [ ] **Step 14: Implement `src/railctl/cli/_errors.py`**

```python
# src/railctl/cli/_errors.py
"""Exception-to-exit-code-and-JSON, in one place, so every command wraps its work the same way.

`run()` is the only function in this package allowed to catch an exception and decide an exit
code from it - a command module that catches RailctlError itself and picks its own exit code
would fork the mapping errors.py already owns.
"""

from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, TextIO

import typer

from railctl.cli.render import render, render_error
from railctl.cli.result import (
    INTERNAL_EXIT_CODE,
    RETRYABLE_CODES,
    USAGE_EXIT_CODE,
    CommandResult,
    ErrorReport,
    Format,
    error_code,
)
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    exit_code_for,
)


@dataclass(frozen=True, slots=True)
class OutputContext:
    fmt: Format
    color: bool
    stdout: TextIO
    stderr: TextIO


def default_suggestions(
    exc: BaseException, *, command: str, address: int | None = None, cv: int | None = None
) -> list[list[str]]:
    """The two suggestions this project has actually needed (docs/probe-results.md R1: a POM
    read the station never answers, and a confirmation nothing can ask for on a non-interactive
    stdin). Everything else defaults to no suggestion rather than a guess that reads as
    authoritative advice it is not.
    """
    if isinstance(exc, PomReadUnsupportedError):
        suggestions = [["railctl", "doctor"]]
        if cv is not None:
            suggestions.append(["railctl", "cv", "read", str(cv), "--mode", "service"])
        return suggestions
    if isinstance(exc, ConfirmationRequiredError):
        return [["railctl", *command.split(), "--yes"]]
    return []


def report_for(
    exc: BaseException,
    *,
    command: str,
    details: dict[str, object] | None = None,
    suggestions: list[list[str]] | None = None,
) -> ErrorReport:
    """5g: `details` is a three-way merge, in this fixed order - `exc.details` (whatever the
    station layer already recorded, e.g. `{"address": 3, "mode": "pom", "attempts": 3}`), then
    `{"cv": exc.cv}` when the exception carries one, then the caller's own `details=` argument
    last, so an explicit call-site value always wins over what the exception recorded. Merging
    in the other order would let a stale `cv` inside `exc.details` silently shadow the one this
    function itself resolves from `exc.cv`.
    """
    code = error_code(exc)
    cv = getattr(exc, "cv", None)
    merged_details: dict[str, object] = dict(getattr(exc, "details", None) or {})
    if cv is not None:
        merged_details["cv"] = cv
    if details:
        merged_details.update(details)
    return ErrorReport(
        code=code,
        message=str(exc),
        retryable=code in RETRYABLE_CODES,
        exit_code=exit_code_for(exc),
        details=merged_details,
        suggestions=(
            suggestions
            if suggestions is not None
            else default_suggestions(exc, command=command, cv=cv)
        ),
        hint=getattr(exc, "hint", None),
    )


def _verbose() -> bool:
    # The one place this package reads an environment variable directly: RAILCTL_VERBOSE is
    # the global --verbose flag's env fallback (design L2), and Task 9's Typer wiring sets it
    # before calling run() rather than every command re-deriving verbosity on its own.
    return os.environ.get("RAILCTL_VERBOSE", "") not in ("", "0")


def run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn:
    start = time.monotonic()
    try:
        result = work()
    except KeyboardInterrupt:
        report = report_for(AbortedError("interrupted by the operator"), command=command)
    except ValueError as exc:
        report = ErrorReport(
            code="usage",
            message=str(exc),
            retryable=False,
            exit_code=USAGE_EXIT_CODE,
            details={},
            suggestions=[],
            hint=None,
        )
    except RailctlError as exc:
        report = report_for(exc, command=command)
    except Exception as exc:  # the safety net: anything else is a bug, never a domain answer
        if _verbose():
            traceback.print_exc(file=ctx.stderr)
        report = ErrorReport(
            code="internal",
            message=str(exc),
            retryable=False,
            exit_code=INTERNAL_EXIT_CODE,
            details={},
            suggestions=[],
            hint=None,
        )
    else:
        # 5g: timed here, not inside build_<command> - every command gets this for free, and a
        # command that only measured its own body would miss the argv-parsing and station-open
        # time a script comparing two invocations actually cares about.
        result.elapsed_ms = int(round((time.monotonic() - start) * 1000))
        render(result, fmt=ctx.fmt, stdout=ctx.stdout, color=ctx.color)
        raise typer.Exit(code=result.exit_code)

    render_error(report, stderr=ctx.stderr, fmt=ctx.fmt, color=ctx.color)
    raise typer.Exit(code=report.exit_code)
```

`RailctlError` is caught AFTER `ValueError` and BEFORE the bare `Exception`. `ValueError` is never
a `RailctlError` subclass (the whole tree hangs off `RailctlError` alone, verified against
`errors.py` above; Python's own `ValueError` is unrelated to it), so the order between those two
branches never changes which one fires - but `RailctlError` must still come before `Exception`,
or every domain exception would fall into the generic branch and be reported as `"internal"`,
which is the M1 failure mode one layer up: a real, specific answer recorded as an unexplained bug.

- [ ] **Step 15: Run the errors test file and see it pass**

Run: `uv run pytest tests/cli/test_errors.py`

Expected: PASS, 37 passed, 0 failed (20 test functions; four are parametrized -
`test_error_code_maps_the_documented_names` x8, `test_retryable_is_true_for_exactly_three_classes` x7,
`test_errors_go_to_stderr_in_every_format_mode` x3, `test_run_sends_every_format_error_to_stderr_only`
x3 - so 16 plain + 8 + 7 + 3 + 3 = 37; the three new plain functions are the hint-parity test and
the two `run()` tests for `elapsed_ms` and `details`).

- [ ] **Step 16: Confirm the layering guard is now genuinely scanning `cli/`**

This is the step that matters most in this task: `src/railctl/cli/` did not exist before Step 5,
so `tests/test_layering.py::test_the_rule_1_and_2_targets_are_scanned_once_they_exist` was passing
on the `not target.exists()` branch alone. From this step onward it must pass on the other branch -
`_python_files("cli")` finding real files - or the two content-scanning rules are silently blind to
everything just written.

Run: `uv run pytest tests/test_layering.py`

Expected: PASS, 8 passed, 0 failed (`tests/test_layering.py` has eight test functions, not seven -
see the house-wide test-count rule). If `test_rule_1_no_wire_vocabulary_in_station_or_cli` or
`test_rule_2_no_cv_arithmetic_outside_xbus_cv` fails here, read the offending line it prints -
the likely cause is the literal word "tty" surviving in a docstring (Step 10's `render.py` module
docstring and `want_color`'s docstring both discuss the concept in prose using "terminal" for this
reason) rather than an actual bug.

- [ ] **Step 17: Run the whole `cli` test package together with the exit-code tests**

Run: `uv run pytest tests/cli tests/unit/test_exit_codes.py`

Expected: PASS, 106 passed, 0 failed (30 + 37 + 39, confirming nothing in one file's fixtures
leaks state into another - `RAILCTL_VERBOSE` is set via `monkeypatch` in Step 12's two tests
specifically so it cannot).

- [ ] **Step 18: Lint and format**

Run: `uv run ruff check src/railctl/errors.py src/railctl/cli tests/unit/test_exit_codes.py tests/cli && uv run ruff format --check src/railctl/errors.py src/railctl/cli tests/unit/test_exit_codes.py tests/cli`

Expected: `All checks passed!` and every file reported already formatted. Run
`uv run ruff format src/railctl/errors.py src/railctl/cli tests/unit/test_exit_codes.py tests/cli`
first if the format check reports a diff, then re-run both commands.

- [ ] **Step 19: Check the coverage gate before committing**

Run: `uv run pytest --cov --cov-report=term-missing`

Expected: the coverage table now includes `src/railctl/cli/__init__.py`, `src/railctl/cli/result.py`,
`src/railctl/cli/render.py` and `src/railctl/cli/_errors.py`, each at 100% or with only the
`except Exception` safety-net line's traceback-print branch needing its own test (Step 12 already
covers both the verbose and non-verbose sides of that branch, so it should show no gap); then
`Required test coverage of 90% reached.` and `0 failed`. `src/railctl/cli/` is not in the coverage
omit list - only `src/railctl/transport/serial_posix.py` is - so every branch written in this task
needs a test, and if the table shows an uncovered line here, this task owns the fix.

- [ ] **Step 20: Commit**

```bash
git add src/railctl/errors.py src/railctl/cli tests/unit/test_exit_codes.py tests/cli/test_format_modes.py tests/cli/test_errors.py
git commit -m "feat(cli): add the output contract - CommandResult, render, and exception-to-exit-code mapping"
```

---

### Task 9: Configuration, global options, dependency resolution, and the first two commands

**Files:**
- Create: `src/railctl/cli/config.py`, `src/railctl/cli/deps.py`, `src/railctl/cli/main.py`, `src/railctl/cli/commands/__init__.py`, `src/railctl/cli/commands/basics.py`, `src/railctl/__main__.py`
- Test: `tests/cli/test_config.py`, `tests/cli/test_wiring.py`
- Modify: `pyproject.toml` (`[project.scripts]`'s `railctl = "railctl.cli.main:app"` becomes `railctl = "railctl.cli.main:main"` - see Step 15; the installed console script and `python -m railctl` must take the same path, or the pre-command JSON-error envelope this task builds only ever runs under `python -m`)

**Interfaces:**

- Consumes, exactly as merged on disk (M2-M4):
  - `railctl.errors.RailctlError(message: str, *, hint: str | None = None)` - `str(exc)` is the message only
  - `railctl.errors.TransportError(RailctlError)`, `railctl.errors.exit_code_for(exc: BaseException) -> int`, `railctl.errors.EXIT_CODES`
  - `railctl.xbus.address.LOCO_ADDR_MIN = 1`, `LOCO_ADDR_MAX = 9999`
  - `railctl.xbus.replies.StationVersion` - `raw: int`, `station_id: int`, properties `.version -> str` (`f"{raw>>4}.{raw&0x0F}"`), `.family -> str`
  - `railctl.xbus.replies.StationStatus` - `raw, emergency_off, emergency_stop, auto_start_mode, service_mode, powering_up, ram_error`, classmethod `from_raw(raw: int) -> StationStatus`, property `.track_power -> bool` (`not emergency_off`)
  - `railctl.envelope.liusb` logs to `logging.getLogger("railctl.wire")` - the only wire log in the package
  - `railctl.__version__ = "0.1.0"`
  - stdlib `tomllib`, `logging`, `os`, `dataclasses`; `typer` (the only runtime dependency)

- Consumes from Task 8 (`src/railctl/cli/result.py`, `src/railctl/cli/render.py`, `src/railctl/cli/_errors.py`, all already on disk by the time this task runs - **not** `railctl.cli.output`, which no task in this plan creates):
  - `railctl.cli.result.Format` - `Literal["human", "json", "ndjson"]`. Write the strings `"human"` / `"json"` / `"ndjson"` directly; there is no `Format.human` to write instead, because `Format` is a type alias, not an enum class.
  - `railctl.cli.result.LinkInfo` - `@dataclass(frozen=True, slots=True)`, `identity: str`, `target: str`
  - `railctl.cli.result.StationInfo` - `@dataclass(frozen=True, slots=True)`, `protocol: str`, `protocol_version: str | None`, `command_station_id: int | None`
  - `railctl.cli.result.ResultWarning` - `@dataclass(frozen=True, slots=True)`, `name: str`, `message: str`, `details: dict[str, object]`
  - `railctl.cli.result.CommandResult` - a **mutable** `@dataclass` (no `frozen`, no `slots`): `schema: str`, `command: str`, `ok: bool = True`, `exit_code: int = 0`, `elapsed_ms: int = 0`, `link: LinkInfo | None = None`, `station: StationInfo | None = None`, `warnings: list[ResultWarning] = field(default_factory=list)`, `result: dict[str, object] = field(default_factory=dict)`, `lines: list[str] = field(default_factory=list)`, plus `.warn(name, message, **details)` and `.say(line)` methods. Every builder in this task constructs one with `link`/`station` left `None`, then assigns them directly (`result.link = ...`, `result.station = ...`) once the station is open - mutability is what lets `build_version`/`build_status` themselves stay pure and testable with no Typer runner at all, and it also means this task's command bodies never call `dataclasses.replace` on a `CommandResult`.
  - `railctl.cli._errors.OutputContext` - `@dataclass(frozen=True, slots=True)`, `fmt: Format`, `color: bool`, `stdout: TextIO`, `stderr: TextIO`
  - `railctl.cli._errors.run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn` - calls `work()`, times it, renders the result (or, on `RailctlError`/plain `ValueError`/`KeyboardInterrupt`, writes one `railctl/error/v1` JSON object to `ctx.stderr`), and always ends by raising `typer.Exit` with the right code itself. Every command in this task's body ends with a bare call to `run(...)` - **never** `raise typer.Exit(code=run(...))`, since `run` never returns a value to wrap.
  - `railctl.cli.render.render(result: CommandResult, *, fmt: Format, stdout: TextIO, color: bool) -> None`
  - `railctl.cli.render.want_color(choice: Literal["auto", "always", "never"], stream: TextIO, env: Mapping[str, str]) -> bool`
  - `railctl.cli._errors.report_for(exc: BaseException, *, command: str, details: dict[str, object] | None = None, suggestions: list[list[str]] | None = None) -> ErrorReport` - `command` is required, and the return value is an `ErrorReport`, never a dict. Used directly once in this task, by `main()`, for a failure that happens before any command's own `run()` gets a chance to catch it - `main()` must call `.envelope()` on the result before handing it to `json.dumps`.
  - `railctl.cli.result.tri_state(value: bool | None) -> str` - available, not needed by either command in this task (every `StationStatus` field is a plain `bool`; the tri-state fields live in `Capabilities`, first read by `doctor` in Task 12)

- Consumes from Task 8 (`src/railctl/errors.py`, extended by that task): `errors.ConfirmationRequiredError(message: str, *, hint: str | None = None, details: dict[str, object] | None = None)` and `errors.AbortedError(message: str, *, hint: str | None = None)`, both subclassing `RailctlError`. `ConfirmationRequiredError` is mapped to exit code 2 in `EXIT_CODES`; `AbortedError` inherits the base 9. Neither class takes a `suggestions` keyword: `confirm()` below must not pass one, and the runnable `["railctl", <command>, "--yes"]` array a script sees in the eventual JSON envelope is assembled later, by `_errors.py`'s `default_suggestions`, from the exception's type alone.

- Consumes from Tasks 1-7 (`src/railctl/station/`, already on disk by the time this task runs):
  - `railctl.station.Station.open(target: str = "auto", *, default_address: int | None = None, capabilities_path: Path | None = None, timing: Timing = TIMING) -> Station`
  - `Station.close() -> None`, `Station.version() -> StationVersion`, `Station.status() -> StationStatus`, properties `.description`, `.identity`, `.capabilities`
  - `railctl.station.TIMING` - the module-level `Timing` default

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

```python
# src/railctl/cli/config.py
CONFIG_KEYS: Final[tuple[str, ...]] = ("target", "address", "verbose")
DEFAULT_TARGET: Final[str] = "auto"

def config_dir(env: Mapping[str, str] | None = None) -> Path: ...
def config_path(env: Mapping[str, str] | None = None) -> Path: ...
def capabilities_path(env: Mapping[str, str] | None = None) -> Path: ...

@dataclass(frozen=True, slots=True)
class Config:
    target: str = DEFAULT_TARGET
    address: int | None = None
    verbose: int = 0

def load_config(path: Path) -> Config: ...
def pick(flag: object | None, env_value: str | None, config_value: object | None,
         default: object, *, name: str, cast: Callable[[str], object]) -> object: ...
```

```python
# src/railctl/cli/deps.py
@dataclass(frozen=True, slots=True)
class Settings:
    target: str
    address: int | None
    fmt: Format
    verbose: int
    color: Literal["auto", "always", "never"]
    assume_yes: bool
    interactive: bool

def build_settings(*, target: str | None, address: int | None, fmt: str | None,
                   json_flag: bool, verbose: int | None, color: str, yes: bool,
                   non_interactive: bool, env: Mapping[str, str],
                   config: Config, stdin: TextIO) -> Settings: ...
def merge_settings(base: Settings, *, target: str | None = None, address: int | None = None,
                   fmt: str | None = None, json_flag: bool = False, verbose: int = 0,
                   color: str | None = None, yes: bool = False,
                   non_interactive: bool = False) -> Settings: ...
def configure_logging(verbose: int, stderr: TextIO) -> None: ...
def open_station(settings: Settings, *, capabilities_path: Path | None) -> Station: ...
def link_info(station: Station, settings: Settings) -> LinkInfo: ...
def station_info(station: Station) -> StationInfo: ...
def require_address(settings: Settings, *, argv_hint: list[str]) -> int: ...
def confirm(question: str, *, settings: Settings, stdin: TextIO, stderr: TextIO) -> None: ...
```

```python
# src/railctl/cli/commands/basics.py
VERSION_SCHEMA: Final[str] = "railctl/version/v1"
STATUS_SCHEMA: Final[str] = "railctl/status/v1"

def build_version(version: StationVersion, *, tool_version: str) -> CommandResult: ...
def build_status(status: StationStatus) -> CommandResult: ...
def register(app: typer.Typer) -> None: ...
```

```python
# src/railctl/cli/main.py
app: typer.Typer                       # typer.Typer(add_completion=False, no_args_is_help=True,
                                       #   context_settings={"max_content_width": 100})
def context_for(settings: Settings, *, stdout: TextIO, stderr: TextIO) -> OutputContext: ...
def main() -> None: ...                # used by src/railctl/__main__.py AND by pyproject.toml's
                                       #   [project.scripts] entry (Step 15) - both must run the
                                       #   same code path, so `app` itself is never the entry point
```

**Notes the implementer must not re-derive:**

- `context_for` lives in `main.py`, not in `commands/basics.py` - `main.py` imports `commands.basics` to call `register(app)`, so `basics.py` importing `context_for` back from `main.py` would be a circular import. Instead, `main.py`'s own `@app.callback()` builds an `OutputContext` once per invocation and stores it, together with `Settings`, in a small frozen `CliContext` (also defined in `main.py`) on `ctx.obj`. Every command in `commands/basics.py` reads `ctx.obj.settings` and `ctx.obj.output` and never imports anything from `railctl.cli.main`.
- `UsageProblem`, defined in `deps.py`, is a `ValueError` subclass that carries a structured `suggestions: list[list[str]]`. It is named "Problem", not "...Error": `tests/test_layering.py` rule 3 reserves class names ending in `Error`/`Exception`/`Timeout`, with a declared base, for `errors.py` alone. `UsageProblem` still IS a `ValueError` underneath, so `railctl.cli._errors.report_for` maps it to exit code 2 through the same generic "any `ValueError` is exit 2" rule the design spec states (L6) - this class adds only a place to hang a real argv array.
- `typer.Option(None, "--target")`-style defaults (a function call used as a parameter default) look like they should trip Ruff's `B008` (flake8-bugbear, "function call in default argument"), which is in this project's selected rule set (`select = [..., "B", ...]`). They do not: Ruff's bugbear implementation has a built-in allowlist for exactly this pattern (`typer.Option`/`typer.Argument`, along with FastAPI's `Depends`) - verified against the version `uv` resolves in this repo; run `uv run ruff --version` if the behaviour below does not match. No `# noqa` and no `pyproject.toml` edit for lint config is needed (this task's own, unrelated `pyproject.toml` edit is the console-script entry point in Step 15).
- `--verbose`/`-v` uses `typer.Option(None, "-v", "--verbose", count=True)`. Passing `None` as the default of a `count=True` option is what makes an absent flag resolve to `None` (so `pick()` can fall through to environment/config/default) while a present flag still resolves to the real count (`1` for `-v`, `2` for `-vv`) - verified with `typer.testing.CliRunner` against the version `uv` resolves in this repo; run `uv run python -c 'import typer; print(typer.__version__)'` if the behaviour below does not match, since Click's own count-option default without an explicit sentinel would otherwise always be `0`, indistinguishable from an explicit `-v` count that happened to also be zero.
- `merge_settings` is produced here but not called by either command in this task: it exists so that Tasks 10-12, whose commands must each declare all eight global options a second time to work around Click's group-options-before-subcommand parsing (spec's own usage puts them after - see the design spec's global-option table), have one place to layer a per-command override onto `ctx.obj.settings` instead of five copies of the same `if x is not None` block. It is exercised directly in this task's own tests so its sentinel rule (`None`/`False`/`0` means "not typed at the command level, keep the base value") is pinned before anything downstream depends on it.
- `--color`'s only environment input is `NO_COLOR` (design spec table), not `RAILCTL_COLOR`, and it is not resolved through `pick()`: `build_settings` passes the literal `--color` flag value straight through to `Settings.color`, and `NO_COLOR`/`TERM=dumb` are folded in later, at render time, by `want_color` (Task 8). Treating `color` as a fourth `pick()`-managed key would be wrong twice over - it has no config-file key, and `NO_COLOR` is a force-plain-text override, not a value in the same precedence chain as `target`/`address`/`verbose`.
- `station_info(station)` calls `station.version()` again - a second real XpressNet query. `version_command` avoids that duplicate query by building its own `StationInfo` inline from the `StationVersion` it already fetched for `build_version`'s own result; `status_command` has no `StationVersion` lying around, so it uses the shared `station_info()` helper. Both are correct; they differ only in whether a command already happens to hold the fact `station_info` would otherwise re-derive.
- `Station.open`'s signature takes `target` positionally with a default, which is exactly what a monkeypatched replacement in this task's tests must still accept: patch it with a plain function (`monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: ...))`), not a bound method, so the patched call shape matches regardless of whether the real Task 1-7 implementation is a `classmethod` or a `staticmethod`.
- Every test that exercises `main()`, `app` or `config_dir`/`config_path`/`capabilities_path` sets `XDG_CONFIG_HOME` to a `tmp_path` first. Nothing in this task's tests may read or write a developer's real `~/.config/railctl`.
- Testing an error raised **inside** the Typer callback (`build_settings`/`load_config` failures - address out of range, a broken config file) requires calling `main()` directly inside `pytest.raises(SystemExit)`, not `typer.testing.CliRunner`. Verified directly: `CliRunner.invoke()` catches an uncaught exception from a Typer callback and reports it as `exit_code == 1` with the raw exception attached to `result.exception` - it does NOT run this task's `main()` wrapper, so it cannot observe the JSON-on-stderr, exit-2 behaviour this task builds for that case. Once a command's own body starts running, its `work()` failures go through Task 8's `run()`, which already converts them to `typer.Exit(code=...)` before they would reach `main()`'s wrapper - `CliRunner` observes those correctly, because `typer.Exit` is exactly what `CliRunner` is built to catch.
- `CliRunner` on the version of Typer `uv` resolves in this repo separates `result.stdout` and `result.stderr`; there is no `mix_stderr` constructor argument to set - run `uv run python -c 'import typer; print(typer.__version__)'` if the behaviour below does not match. Verified directly - a command that prints to both streams and exits non-zero shows up as two distinct, non-empty `result.stdout`/`result.stderr` strings.

---

- [ ] **Step 1: Write the failing config tests**

```python
# tests/cli/test_config.py
"""`~/.config/railctl/config.toml`: three keys, and the CLI-flag/env/file/default
precedence primitive every global option is resolved through.

A missing file is not an error - most runs have none. A file that IS there and
IS wrong always names the file, the line and the key: "invalid config" alone
is a support ticket six months later, not something anyone can fix on sight.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from railctl.cli.config import (
    CONFIG_KEYS,
    Config,
    capabilities_path,
    config_dir,
    config_path,
    load_config,
    pick,
)


def test_config_dir_honours_xdg_config_home(tmp_path: Path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert config_dir(env) == tmp_path / "railctl"


def test_config_dir_falls_back_to_dot_config_when_xdg_unset(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config_dir({}) == tmp_path / ".config" / "railctl"


def test_config_path_and_capabilities_path_are_under_config_dir(tmp_path: Path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert config_path(env) == tmp_path / "railctl" / "config.toml"
    assert capabilities_path(env) == tmp_path / "railctl" / "capabilities.json"


def test_config_keys_are_exactly_target_address_verbose():
    assert CONFIG_KEYS == ("target", "address", "verbose")


def test_default_config_has_auto_target_and_no_address():
    config = Config()
    assert config.target == "auto"
    assert config.address is None
    assert config.verbose == 0


def test_load_config_on_a_missing_file_returns_defaults_and_creates_nothing(tmp_path: Path):
    path = tmp_path / "config.toml"
    assert load_config(path) == Config()
    assert not path.exists()


def test_load_config_reads_all_three_keys(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "serial:auto"\naddress = 3\nverbose = 2\n', encoding="utf-8")
    assert load_config(path) == Config(target="serial:auto", address=3, verbose=2)


def test_load_config_rejects_an_unknown_key_naming_file_line_and_key(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "auto"\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 2" in message
    assert "bogus" in message


def test_load_config_rejects_bad_toml_syntax_naming_file_and_line(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "auto"\naddress = \n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 2" in message


def test_load_config_rejects_an_out_of_range_address_naming_the_bound(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("address = 99999\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 1" in message
    assert "address" in message
    assert "9999" in message


def test_load_config_rejects_a_negative_verbose_count(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("verbose = -1\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    assert "verbose" in str(caught.value)


def test_pick_prefers_the_flag_over_everything():
    result = pick("cli-value", "env-value", "config-value", "default", name="target", cast=str)
    assert result == "cli-value"


def test_pick_prefers_env_over_config_and_default():
    result = pick(None, "7", "3", 0, name="address", cast=int)
    assert result == 7


def test_pick_prefers_config_over_the_default():
    result = pick(None, None, 3, 0, name="address", cast=int)
    assert result == 3


def test_pick_falls_back_to_the_built_in_default():
    result = pick(None, None, None, 0, name="address", cast=int)
    assert result == 0


def test_pick_applies_cast_to_the_environment_value_only():
    # config_value=3 (an int, already the right type) must be returned as-is;
    # cast is for parsing the environment STRING, never for re-typing a value
    # that came from a source that is already typed.
    result = pick(None, "9", 3, 0, name="address", cast=int)
    assert result == 9
    assert isinstance(result, int)


def test_pick_wraps_a_cast_failure_in_value_error():
    with pytest.raises(ValueError) as caught:
        pick(None, "not-a-number", None, 0, name="address", cast=int)
    assert "RAILCTL_ADDRESS" in str(caught.value)
```

- [ ] **Step 2: Run the config tests to see them fail**

```bash
uv run pytest tests/cli/test_config.py
```

Expected: a collection error, `ModuleNotFoundError: No module named 'railctl.cli.config'` - `src/railctl/cli/config.py` does not exist yet.

- [ ] **Step 3: Implement `src/railctl/cli/config.py`**

```python
# src/railctl/cli/config.py
"""Config file loading and the generic CLI-flag/env/file/default precedence.

`~/.config/railctl/config.toml` carries three keys and nothing else - target,
address, verbose (design spec L3). A missing file is not an error: most
invocations have none. A file that IS there and IS wrong - bad TOML, an
unknown key, a value outside its bound - is exit 2, and the message always
names the file, the line and the key.

`pick()` is the one place the four-level precedence (CLI flag > environment >
config file > built-in default) is decided, generically, so `cli/deps.py`
calls it once per key instead of writing the same if/elif/else four times
over.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN

CONFIG_KEYS: Final[tuple[str, ...]] = ("target", "address", "verbose")
DEFAULT_TARGET: Final[str] = "auto"

_APP_DIRNAME: Final[str] = "railctl"
_XDG_FALLBACK: Final[str] = ".config"
_CONFIG_FILENAME: Final[str] = "config.toml"
_CAPABILITIES_FILENAME: Final[str] = "capabilities.json"

_TOML_LINE_RE: Final[re.Pattern[str]] = re.compile(r"line (\d+)")


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/railctl`, or `~/.config/railctl` when unset.

    `env` defaults to the real process environment so the CLI's own entry
    point needs no special case; every test in this task passes a mapping
    with `XDG_CONFIG_HOME` already pointing at a `tmp_path`, which is what
    keeps the suite from ever touching a real home directory.
    """
    mapping = os.environ if env is None else env
    xdg = mapping.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / _XDG_FALLBACK
    return base / _APP_DIRNAME


def config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / _CONFIG_FILENAME


def capabilities_path(env: Mapping[str, str] | None = None) -> Path:
    # The ONLY definition of this function in the whole plan. Task 12's
    # `doctor` command imports it from here (`from railctl.cli.config import
    # capabilities_path`) rather than defining its own - a second copy that
    # drifts to a different XDG fallback is exactly how two commands would end
    # up reading two different `capabilities.json` files on the same machine.
    return config_dir(env) / _CAPABILITIES_FILENAME


@dataclass(frozen=True, slots=True)
class Config:
    target: str = DEFAULT_TARGET
    address: int | None = None
    verbose: int = 0


def _line_for_key(text: str, key: str) -> int:
    """First line whose left-hand side is `key`, or line 1 when not found.

    "Not found" only happens for a key tomllib never actually accepted (a
    syntax error before the key was even parsed), so line 1 is the closest
    honest answer, not a guess dressed up as one.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return number
    return 1


def _config_error(path: Path, *, line: int, key: str, detail: str) -> ValueError:
    return ValueError(f"{path}: line {line}: key {key!r}: {detail}")


def load_config(path: Path) -> Config:
    """Missing file -> defaults. Bad file -> `ValueError` naming file, line, key."""
    if not path.exists():
        return Config()
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        match = _TOML_LINE_RE.search(str(exc))
        line = int(match.group(1)) if match else 1
        lines = text.splitlines()
        offending = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        # A syntax error still names a "key": the text left of the first `=`
        # on the offending line, or the whole stripped line when there is no
        # `=` at all (a stray bracket, a missing quote). Either way the file,
        # the line and something to grep for all land in one message.
        key = offending.split("=", 1)[0].strip() or offending or "<syntax>"
        raise _config_error(path, line=line, key=key, detail=f"invalid TOML ({exc})") from exc

    for key in raw:
        if key not in CONFIG_KEYS:
            raise _config_error(
                path,
                line=_line_for_key(text, key),
                key=key,
                detail=f"not a recognised config key (expected one of {CONFIG_KEYS})",
            )

    target = raw.get("target", DEFAULT_TARGET)
    if not isinstance(target, str):
        raise _config_error(
            path,
            line=_line_for_key(text, "target"),
            key="target",
            detail=f"must be a string, got {target!r}",
        )

    address = raw.get("address")
    if address is not None:
        if not isinstance(address, int) or isinstance(address, bool):
            raise _config_error(
                path,
                line=_line_for_key(text, "address"),
                key="address",
                detail=f"must be an integer, got {address!r}",
            )
        if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
            raise _config_error(
                path,
                line=_line_for_key(text, "address"),
                key="address",
                detail=f"{address} is outside {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}",
            )

    verbose = raw.get("verbose", 0)
    if not isinstance(verbose, int) or isinstance(verbose, bool) or verbose < 0:
        raise _config_error(
            path,
            line=_line_for_key(text, "verbose"),
            key="verbose",
            detail=f"must be a non-negative integer, got {verbose!r}",
        )

    return Config(target=target, address=address, verbose=verbose)


def pick(
    flag: object | None,
    env_value: str | None,
    config_value: object | None,
    default: object,
    *,
    name: str,
    cast: Callable[[str], object],
) -> object:
    """CLI flag > environment > config file > built-in default, for one key.

    The four levels are separate arguments, not a single merged mapping, so a
    caller cannot let one key's environment value leak into another key's
    decision - the quiet cross-talk between two measurements that were
    supposed to stay separate is exactly the failure mode this project exists
    to catch.
    """
    if flag is not None:
        return flag
    if env_value is not None:
        try:
            return cast(env_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"environment variable RAILCTL_{name.upper()}={env_value!r} is invalid: {exc}"
            ) from exc
    if config_value is not None:
        return config_value
    return default
```

- [ ] **Step 4: Run the config tests to see them pass**

```bash
uv run pytest tests/cli/test_config.py
```

Expected: `17 passed`.

- [ ] **Step 5: Write the failing dependency-resolution tests**

```python
# tests/cli/test_wiring.py
"""Global-option resolution, logging levels, confirmation, and the two
commands every railctl session starts with.

Split by the module under test with a comment banner per section, because
this is the one test file this task's contract allows for `deps.py`,
`commands/basics.py` and `main.py` together.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from typer.testing import CliRunner

from railctl.cli.config import Config
from railctl.cli.deps import (
    Settings,
    UsageProblem,
    build_settings,
    confirm,
    configure_logging,
    link_info,
    merge_settings,
    open_station,
    require_address,
    station_info,
)
from railctl.errors import AbortedError, ConfirmationRequiredError, TransportError
from railctl.station import TIMING, Station
from railctl.xbus.replies import StationVersion


def _config(**overrides) -> Config:
    return Config(**overrides)


def _settings(**overrides) -> Settings:
    base = dict(
        target="auto",
        address=None,
        fmt="human",
        verbose=0,
        color="auto",
        assume_yes=False,
        interactive=False,
    )
    base.update(overrides)
    return Settings(**base)


# -- Settings / build_settings precedence -----------------------------------


def test_target_precedence_cli_over_env_over_config_over_default():
    common = dict(
        address=None, fmt=None, json_flag=False, verbose=None, color="auto",
        yes=False, non_interactive=True, stdin=io.StringIO(),
    )
    all_four = dict(
        target="cli-target", env={"RAILCTL_TARGET": "env-target"},
        config=_config(target="config-target"), **common,
    )
    assert build_settings(**all_four).target == "cli-target"
    assert build_settings(**{**all_four, "target": None}).target == "env-target"
    assert build_settings(
        **{**all_four, "target": None, "env": {}}
    ).target == "config-target"
    assert build_settings(
        **{**all_four, "target": None, "env": {}, "config": _config()}
    ).target == "auto"


def test_address_precedence_cli_over_env_over_config_over_default():
    common = dict(
        target=None, fmt=None, json_flag=False, verbose=None, color="auto",
        yes=False, non_interactive=True, stdin=io.StringIO(),
    )
    all_four = dict(
        address=1, env={"RAILCTL_ADDRESS": "2"}, config=_config(address=3), **common,
    )
    assert build_settings(**all_four).address == 1
    assert build_settings(**{**all_four, "address": None}).address == 2
    assert build_settings(
        **{**all_four, "address": None, "env": {}}
    ).address == 3
    assert build_settings(
        **{**all_four, "address": None, "env": {}, "config": _config()}
    ).address is None


def test_verbose_precedence_cli_over_env_over_config_over_default():
    common = dict(
        target=None, address=None, fmt=None, json_flag=False, color="auto",
        yes=False, non_interactive=True, stdin=io.StringIO(),
    )
    all_four = dict(
        verbose=2, env={"RAILCTL_VERBOSE": "1"}, config=_config(verbose=1), **common,
    )
    assert build_settings(**all_four).verbose == 2
    assert build_settings(**{**all_four, "verbose": None}).verbose == 1
    assert build_settings(
        **{**all_four, "verbose": None, "env": {}}
    ).verbose == 1
    assert build_settings(
        **{**all_four, "verbose": None, "env": {}, "config": _config(verbose=0)}
    ).verbose == 0


def test_format_precedence_cli_over_env_over_default():
    # `format` has no config-file key at all (design spec L3): only three
    # keys ever live in config.toml, and format is not one of them.
    common = dict(
        target=None, address=None, verbose=None, color="auto",
        yes=False, non_interactive=True, stdin=io.StringIO(), config=_config(),
    )
    # Format is `Literal["human", "json", "ndjson"]`, not an enum class - there
    # is nothing to call it with, so the comparison is against the plain string.
    assert build_settings(
        fmt="json", json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common
    ).fmt == "json"
    assert build_settings(
        fmt=None, json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common
    ).fmt == "ndjson"
    assert build_settings(fmt=None, json_flag=False, env={}, **common).fmt == "human"


def test_precedence_is_decided_independently_per_key():
    # A config file supplying `address` and an environment variable supplying
    # `target` must BOTH take effect in the same run - proof that build_settings
    # walks each key's own four levels rather than picking one winning SOURCE
    # for the whole call.
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_TARGET": "z21:192.168.0.111:21105"},
        config=_config(address=7),
        stdin=io.StringIO(),
    )
    assert settings.target == "z21:192.168.0.111:21105"
    assert settings.address == 7


def test_json_flag_is_an_alias_for_format_json():
    settings = build_settings(
        target=None, address=None, fmt=None, json_flag=True, verbose=None,
        color="auto", yes=False, non_interactive=True, env={}, config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.fmt == "json"


def test_json_flag_conflicts_with_an_explicit_non_json_format():
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None, address=None, fmt="ndjson", json_flag=True, verbose=None,
            color="auto", yes=False, non_interactive=True, env={}, config=_config(),
            stdin=io.StringIO(),
        )
    assert "--json" in str(caught.value)
    assert "ndjson" in str(caught.value)


def test_address_outside_bounds_is_rejected_before_any_link_is_opened(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("Station.open must not run when --address is out of range")

    monkeypatch.setattr(Station, "open", staticmethod(fail_if_called))
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None, address=20000, fmt=None, json_flag=False, verbose=None,
            color="auto", yes=False, non_interactive=True, env={}, config=_config(),
            stdin=io.StringIO(),
        )
    assert "9999" in str(caught.value)


def test_railctl_port_env_var_has_no_effect():
    with_port = build_settings(
        target=None, address=None, fmt=None, json_flag=False, verbose=None,
        color="auto", yes=False, non_interactive=True,
        env={"RAILCTL_PORT": "/dev/whatever-this-would-be"}, config=_config(),
        stdin=io.StringIO(),
    )
    without_port = build_settings(
        target=None, address=None, fmt=None, json_flag=False, verbose=None,
        color="auto", yes=False, non_interactive=True, env={}, config=_config(),
        stdin=io.StringIO(),
    )
    assert with_port == without_port


def test_interactive_is_decided_by_stdin_isatty():
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    settings = build_settings(
        target=None, address=None, fmt=None, json_flag=False, verbose=None,
        color="auto", yes=False, non_interactive=False, env={}, config=_config(),
        stdin=_Tty(),
    )
    assert settings.interactive is True

    settings = build_settings(
        target=None, address=None, fmt=None, json_flag=False, verbose=None,
        color="auto", yes=False, non_interactive=False, env={}, config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.interactive is False


# -- merge_settings -----------------------------------------------------
#
# Tasks 10-12 give every registered command its own copy of all eight global
# options (Click parses group options before the subcommand name, so a bare
# `railctl doctor --address 3` would otherwise be a usage error) and layer
# them over `ctx.obj.settings` with this function. It is pinned here, where
# `Settings` and the sentinel rule are defined, rather than left for the first
# downstream task to invent its own version.


def test_merge_settings_overrides_only_the_typed_fields():
    base = _settings(target="auto", address=None, fmt="human", color="auto")
    merged = merge_settings(base, address=7, fmt="json")
    assert merged.address == 7
    assert merged.fmt == "json"
    # Untyped fields (the sentinel: None/False/0) pass `base`'s value through
    # unchanged - this is what lets a command declare all eight options and
    # merge unconditionally, without first checking which ones the operator
    # actually passed on this particular invocation.
    assert merged.target == "auto"
    assert merged.color == "auto"
    assert merged.assume_yes is False


def test_merge_settings_json_flag_is_an_alias_for_format_json():
    merged = merge_settings(_settings(fmt="human"), json_flag=True)
    assert merged.fmt == "json"


def test_merge_settings_leaves_base_untouched_when_nothing_is_typed():
    base = _settings()
    assert merge_settings(base) == base


# -- require_address ----------------------------------------------------


def test_require_address_returns_the_configured_address():
    assert require_address(_settings(address=3), argv_hint=["railctl", "drive", "40"]) == 3


def test_require_address_raises_a_usage_problem_with_the_documented_suggestion():
    with pytest.raises(UsageProblem) as caught:
        require_address(_settings(address=None), argv_hint=["railctl", "drive", "40"])
    assert caught.value.suggestions == [["railctl", "drive", "40", "--address", "3"]]


# -- confirm --------------------------------------------------------------


def test_confirm_with_yes_returns_immediately_without_reading_stdin():
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must not read stdin when --yes was given")

    confirm(
        "really restore?", settings=_settings(assume_yes=True), stdin=_NeverRead(),
        stderr=io.StringIO(),
    )


def test_confirm_noninteractive_raises_confirmation_required_naming_yes():
    # `ConfirmationRequiredError` (Task 8) takes only `hint`/`details`, no
    # `suggestions` - the runnable `[..., "--yes"]` array a script sees in the
    # JSON envelope is assembled later, by `_errors.py`'s `default_suggestions`,
    # from the exception's type. All this layer can pin is that `confirm()`
    # itself still tells a human reader how to get past the prompt: this test
    # goes red the moment `confirm()`'s message stops mentioning `--yes`.
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must never block on a non-interactive stdin")

    with pytest.raises(ConfirmationRequiredError) as caught:
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=False),
            stdin=_NeverRead(),
            stderr=io.StringIO(),
        )
    assert "--yes" in str(caught.value)


def test_confirm_interactive_proceeds_on_y():
    confirm(
        "really restore?",
        settings=_settings(assume_yes=False, interactive=True),
        stdin=io.StringIO("y\n"),
        stderr=io.StringIO(),
    )


def test_confirm_interactive_aborts_on_anything_else():
    with pytest.raises(AbortedError):
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=True),
            stdin=io.StringIO("n\n"),
            stderr=io.StringIO(),
        )


# -- configure_logging ------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_loggers():
    yield
    for name in ("railctl", "railctl.wire"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_configure_logging_verbose_1_enables_decoded_diagnostics_and_keeps_wire_quiet(capsys):
    stderr = io.StringIO()
    configure_logging(1, stderr)
    logging.getLogger("railctl.station").info("decoded frame: status")
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    output = stderr.getvalue()
    assert "decoded frame: status" in output
    assert "TX 21 24 05" not in output
    assert capsys.readouterr().out == ""


def test_configure_logging_verbose_2_enables_wire_debug(capsys):
    stderr = io.StringIO()
    configure_logging(2, stderr)
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    assert "TX 21 24 05" in stderr.getvalue()
    assert capsys.readouterr().out == ""


# -- open_station / link_info / station_info --------------------------------


class _FakeStation:
    def __init__(self, *, identity="serial:7010A0001194:3", raw_version=0x40, station_id=0x12):
        self.identity = identity
        self._version = StationVersion(raw=raw_version, station_id=station_id)
        self.closed = False

    def version(self) -> StationVersion:
        return self._version

    def close(self) -> None:
        self.closed = True


def test_open_station_forwards_target_address_capabilities_path_and_timing(monkeypatch, tmp_path):
    calls = []

    def fake_open(target, *, default_address, capabilities_path, timing):
        calls.append((target, default_address, capabilities_path, timing))
        return _FakeStation()

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    caps = tmp_path / "capabilities.json"
    settings = _settings(target="serial:auto", address=3)
    open_station(settings, capabilities_path=caps)
    assert calls == [("serial:auto", 3, caps, TIMING)]


def test_open_station_lets_transport_error_propagate(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    with pytest.raises(TransportError):
        open_station(_settings(), capabilities_path=None)


def test_link_info_reads_identity_and_target():
    station = _FakeStation(identity="serial:7010A0001194:3")
    info = link_info(station, _settings(target="serial:auto"))
    assert info.identity == "serial:7010A0001194:3"
    assert info.target == "serial:auto"


def test_station_info_reads_protocol_facts_from_version():
    station = _FakeStation(raw_version=0x40, station_id=0x12)
    info = station_info(station)
    assert info.protocol == "xpressnet"
    assert info.protocol_version == "4.0"
    assert info.command_station_id == 18
```

- [ ] **Step 6: Run the wiring tests to see them fail**

```bash
uv run pytest tests/cli/test_wiring.py
```

Expected: a collection error, `ModuleNotFoundError: No module named 'railctl.cli.deps'` - `src/railctl/cli/deps.py` does not exist yet.

- [ ] **Step 7: Implement `src/railctl/cli/deps.py`**

```python
# src/railctl/cli/deps.py
"""Global-option resolution, logging levels, and the two things every command
needs before it can do anything: an open `Station` and a confirmation gate.

Every function here is a thin, testable seam between Typer's parsed argv and
the rest of the CLI. None of it touches a wire byte or a port name - that is
`station.Station`'s job - which is what keeps this module inside
`tests/test_layering.py` rule 1.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TextIO

from railctl import errors
from railctl.cli.config import Config, pick
from railctl.cli.result import Format, LinkInfo, StationInfo
from railctl.station import TIMING, Station
from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN

_ALLOWED_FORMATS: Final[tuple[str, ...]] = ("human", "json", "ndjson")

# A fixed, illustrative loco number - never derived from any real address - so
# every "missing --address" message is reproducible and greppable. Design spec
# L2 spells out exactly this suggestion for `railctl drive 40`.
_EXAMPLE_ADDRESS: Final[str] = "3"


class UsageProblem(ValueError):
    """An exit-2 usage failure carrying a structured argv suggestion.

    Named "Problem", not "...Error": `tests/test_layering.py` rule 3 reserves
    class names ending in Error/Exception/Timeout, with a declared base, for
    `errors.py` alone. This is still a plain `ValueError` underneath -
    `railctl.cli._errors.report_for` already maps any `ValueError` to exit code
    2 - so the only thing this class adds is somewhere to hang a real argv
    array instead of a sentence an agent would have to parse back apart.
    """

    def __init__(self, message: str, *, suggestions: list[list[str]]) -> None:
        super().__init__(message)
        self.suggestions = suggestions


@dataclass(frozen=True, slots=True)
class Settings:
    target: str
    address: int | None
    fmt: Format
    verbose: int
    color: Literal["auto", "always", "never"]
    assume_yes: bool
    interactive: bool


def build_settings(
    *,
    target: str | None,
    address: int | None,
    fmt: str | None,
    json_flag: bool,
    verbose: int | None,
    color: str,
    yes: bool,
    non_interactive: bool,
    env: Mapping[str, str],
    config: Config,
    stdin: TextIO,
) -> Settings:
    """Resolve every global option, independently, per key.

    `RAILCTL_PORT` is never read here on purpose (design spec L3): it exists
    only so the hardware test suite can point at a device, and giving it any
    effect on `target` would make the shipped tool answer to a variable it
    never documents.
    """
    resolved_target = pick(
        target, env.get("RAILCTL_TARGET"), config.target, "auto", name="target", cast=str
    )

    resolved_address = pick(
        address, env.get("RAILCTL_ADDRESS"), config.address, None, name="address", cast=int
    )
    if resolved_address is not None and not (LOCO_ADDR_MIN <= resolved_address <= LOCO_ADDR_MAX):
        raise ValueError(
            f"--address {resolved_address} is outside {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}"
        )

    resolved_verbose = pick(
        verbose, env.get("RAILCTL_VERBOSE"), config.verbose, 0, name="verbose", cast=int
    )

    # `--json` is an alias for `--format=json`, so it is folded into the same
    # CLI-flag slot pick() sees - not a second, competing source. Passing both
    # `--format=ndjson` and `--json` is a real conflict, not "last flag wins":
    # on a CLI that drives a running train, silently picking one of two
    # contradictory instructions is worse than refusing to guess.
    if json_flag and fmt is not None and fmt != "json":
        raise ValueError(f"--json conflicts with --format={fmt}; pass only one")
    format_flag = "json" if json_flag else fmt
    resolved_format = pick(
        format_flag, env.get("RAILCTL_FORMAT"), None, "human", name="format", cast=str
    )
    if resolved_format not in _ALLOWED_FORMATS:
        raise ValueError(f"--format must be one of {_ALLOWED_FORMATS}, got {resolved_format!r}")

    return Settings(
        target=resolved_target,
        address=resolved_address,
        # `Format` is `Literal["human", "json", "ndjson"]`, not an enum class -
        # there is nothing to construct. `resolved_format` was already checked
        # against `_ALLOWED_FORMATS` above, so the plain string IS the value.
        fmt=resolved_format,
        verbose=resolved_verbose,
        color=color,
        assume_yes=yes,
        # stdin.isatty() is the ONLY thing that decides "interactive" - never
        # a literal path open. --non-interactive forces the non-interactive
        # branch even against a real terminal, for scripted use over a pty.
        interactive=stdin.isatty() and not non_interactive,
    )


def merge_settings(
    base: Settings,
    *,
    target: str | None = None,
    address: int | None = None,
    fmt: str | None = None,
    json_flag: bool = False,
    verbose: int = 0,
    color: str | None = None,
    yes: bool = False,
    non_interactive: bool = False,
) -> Settings:
    """Layer one command's own copy of the global options over `base`.

    Every parameter defaults to the sentinel for "not typed at the command
    level" - `None` for `target`/`address`/`fmt`/`color`, `False` for the three
    booleans, `0` for `verbose` - so a command that redeclares all eight global
    options (Tasks 10-12, worked around Click's group-options-before-subcommand
    parsing) can hand every one of them straight through and get `base`
    unchanged back when none of them were actually given on this invocation.
    """
    updates: dict[str, object] = {}
    if target is not None:
        updates["target"] = target
    if address is not None:
        updates["address"] = address
    resolved_fmt = "json" if json_flag else fmt
    if resolved_fmt is not None:
        updates["fmt"] = resolved_fmt
    if verbose > 0:
        updates["verbose"] = verbose
    if color is not None:
        updates["color"] = color
    if yes:
        updates["assume_yes"] = True
    if non_interactive:
        updates["interactive"] = False
    if not updates:
        return base
    return dataclasses.replace(base, **updates)


def configure_logging(verbose: int, stderr: TextIO) -> None:
    """Set logger levels only. `-v` is decoded diagnostics, `-vv` is raw bytes.

    This function never touches a `Frame` or a byte - it sets levels on
    `logging.getLogger("railctl")` and `logging.getLogger("railctl.wire")` by
    name. Layering rule 1 forbids wire vocabulary in `cli/`, and the envelope
    module already owns `railctl.wire` (the only wire log in the package).
    """
    handler = logging.StreamHandler(stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger("railctl")
    root.handlers = [handler]
    root.propagate = False
    root.setLevel(logging.INFO if verbose >= 1 else logging.WARNING)

    # Set explicitly rather than relying on inheritance from `root`: at
    # verbose=1 the wire logger must stay quiet even though `root` is now
    # INFO, and a logger's effective level is resolved from itself before any
    # walk up the tree happens.
    wire = logging.getLogger("railctl.wire")
    wire.handlers = [handler]
    wire.propagate = False
    wire.setLevel(logging.DEBUG if verbose >= 2 else logging.WARNING)


def open_station(settings: Settings, *, capabilities_path: Path | None) -> Station:
    """Open a `Station` for `settings.target`.

    Raises straight through on failure and adds no `try`/`except` of its own:
    `Station.open` owns closing whatever it partially opened, and swallowing
    or wrapping that here would just be a second place for the same cleanup
    to drift out of sync.
    """
    return Station.open(
        settings.target,
        default_address=settings.address,
        capabilities_path=capabilities_path,
        timing=TIMING,
    )


def link_info(station: Station, settings: Settings) -> LinkInfo:
    return LinkInfo(identity=station.identity, target=settings.target)


def station_info(station: Station) -> StationInfo:
    version = station.version()
    return StationInfo(
        protocol="xpressnet",
        protocol_version=version.version,
        command_station_id=version.station_id,
    )


def require_address(settings: Settings, *, argv_hint: list[str]) -> int:
    """`settings.address`, or an exit-2 `UsageProblem` with a runnable suggestion.

    No command in this tool takes the locomotive address positionally:
    `railctl drive 3 40` and `railctl drive 40 3` are indistinguishable to a
    human holding a running train, so the fix is always to append
    `--address <n>`, never to guess which bare number was meant.
    """
    if settings.address is not None:
        return settings.address
    raise UsageProblem(
        "no locomotive address given (neither --address, RAILCTL_ADDRESS, nor "
        "config.toml's address key); this command always needs one",
        suggestions=[[*argv_hint, "--address", _EXAMPLE_ADDRESS]],
    )


def confirm(question: str, *, settings: Settings, stdin: TextIO, stderr: TextIO) -> None:
    """Ask `question`, unless `--yes` already answered it.

    When `stdin` is not interactive this never blocks: it raises immediately,
    mentioning `--yes` in the message itself, and never reads a byte from
    `stdin`. Blocking here is how a `restore` launched from a cron job with no
    terminal at all would hang forever waiting for an answer that can never
    come. The exception carries no `suggestions` of its own -
    `ConfirmationRequiredError` only takes `hint`/`details` (Task 8) - the
    runnable `["railctl", <command>, "--yes"]` array a script sees in the JSON
    envelope is assembled later, by `_errors.py`'s `default_suggestions`, from
    the exception's type alone.
    """
    if settings.assume_yes:
        return
    if not settings.interactive:
        raise errors.ConfirmationRequiredError(
            f"{question} (refusing to guess: stdin is not interactive; rerun with --yes)"
        )
    print(f"{question} [y/N] ", end="", file=stderr, flush=True)
    answer = stdin.readline().strip().lower()
    if answer not in ("y", "yes"):
        raise errors.AbortedError(f"{question}: not confirmed")
```

- [ ] **Step 8: Run the wiring tests to see the deps section pass**

```bash
uv run pytest tests/cli/test_wiring.py
```

Expected: `25 passed` (10 `Settings`/`build_settings` precedence tests, 3 `merge_settings` tests, 2 `require_address` tests, 4 `confirm` tests, 2 `configure_logging` tests, 4 `open_station`/`link_info`/`station_info` tests). Steps 9 and 13 below append two more sections to this same file, importing `commands.basics` and `cli.main` respectively - neither exists yet, so this is the only point at which the whole file collects and runs cleanly on its own.

- [ ] **Step 9: Write the failing `commands/basics.py` tests**

First, add three names to the existing `from railctl.xbus.replies import StationVersion` line
near the top of `tests/cli/test_wiring.py` so every import stays together in one block instead of
scattering `# noqa: E402`-suppressed imports through the middle of the file:

```python
from railctl.xbus.replies import StationStatus, StationVersion
```

and add one more import line directly after the `from railctl.cli.deps import (...)` block:

```python
from railctl.cli.commands.basics import STATUS_SCHEMA, VERSION_SCHEMA, build_status, build_version
```

Then append the new tests to the end of the file:

```python
# -- commands/basics.py: build_version / build_status ------------------------


def test_build_version_schema_and_command_fields():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    assert result.schema == VERSION_SCHEMA
    assert result.command == "version"


def test_build_version_result_and_lines_carry_the_same_three_facts():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    joined = " ".join(result.lines)
    for fact in ("4.0", "18", "0.1.0"):
        assert fact in json.dumps(result.result)
        assert fact in joined


def test_build_status_result_and_lines_carry_the_raw_byte_and_decoded_names():
    status = StationStatus.from_raw(0x04)  # bit 2 only: auto_start_mode
    result = build_status(status)
    assert result.schema == STATUS_SCHEMA
    assert result.result["raw"] == 0x04
    assert result.result["raw_hex"] == "0x04"
    assert result.result["auto_start_mode"] is True
    assert any("0x04" in line for line in result.lines)
    assert any("start mode" in line for line in result.lines)


def test_build_status_never_calls_bit_2_short_circuit():
    status = StationStatus.from_raw(0x04)
    result = build_status(status)
    assert "short" not in json.dumps(result.result).lower()
    assert "short" not in " ".join(result.lines).lower()


def test_build_status_track_power_is_false_when_emergency_off_is_set():
    status = StationStatus.from_raw(0x01)  # bit 0: emergency_off
    result = build_status(status)
    assert result.result["track_power"] is False
    assert "track power: off" in result.lines
```

- [ ] **Step 10: Run the wiring tests to see the new section fail**

```bash
uv run pytest tests/cli/test_wiring.py -k "build_version or build_status"
```

Expected: `ModuleNotFoundError: No module named 'railctl.cli.commands'` - neither `commands/__init__.py` nor `commands/basics.py` exists yet.

- [ ] **Step 11: Implement `commands/__init__.py` and `commands/basics.py`**

```python
# src/railctl/cli/commands/__init__.py
"""Typer command modules.

Each module exposes `register(app: typer.Typer) -> None`. `cli/main.py` calls
every module's `register` once, in the order commands should appear in
`--help`.
"""

from __future__ import annotations
```

```python
# src/railctl/cli/commands/basics.py
"""The two commands every session starts with: `version` and `status`.

Both builder functions are pure: given the facade's own reply objects, they
return a `CommandResult` with no I/O of their own. That is what lets
`tests/cli/test_wiring.py` pin the human/JSON parity rule - the same facts
appearing in `.lines` and in `.result` - without going through Typer at all,
and it is the shape every later command (Tasks 10-12) repeats.
"""

from __future__ import annotations

from typing import Final

import typer

from railctl import __version__
from railctl.cli._errors import run
from railctl.cli.config import capabilities_path
from railctl.cli.deps import link_info, open_station, station_info
from railctl.cli.result import CommandResult, StationInfo
from railctl.xbus.replies import StationStatus, StationVersion

VERSION_SCHEMA: Final[str] = "railctl/version/v1"
STATUS_SCHEMA: Final[str] = "railctl/status/v1"


def build_version(version: StationVersion, *, tool_version: str) -> CommandResult:
    outcome = CommandResult(schema=VERSION_SCHEMA, command="version")
    outcome.result = {
        "protocol": "xpressnet",
        "protocol_version": version.version,
        "command_station_id": version.station_id,
        "family": version.family,
        "tool_version": tool_version,
    }
    outcome.say(f"XpressNet {version.version} ({version.family})")
    outcome.say(f"command station id: {version.station_id}")
    outcome.say(f"railctl {tool_version}")
    return outcome


def build_status(status: StationStatus) -> CommandResult:
    # auto_start_mode is bit 2 - never printed or named "short circuit"
    # anywhere: neither the Lenz nor the German 23151 document defines any
    # status bit that way, and that mislabel is the exact trap the design
    # spec calls out by name.
    outcome = CommandResult(schema=STATUS_SCHEMA, command="status")
    outcome.result = {
        "raw": status.raw,
        "raw_hex": f"0x{status.raw:02X}",
        "track_power": status.track_power,
        "emergency_off": status.emergency_off,
        "emergency_stop": status.emergency_stop,
        "auto_start_mode": status.auto_start_mode,
        "service_mode": status.service_mode,
        "powering_up": status.powering_up,
        "ram_error": status.ram_error,
    }
    start_mode = "automatic" if status.auto_start_mode else "manual"
    outcome.say(f"raw status byte: 0x{status.raw:02X}")
    outcome.say(f"track power: {'on' if status.track_power else 'off'}")
    outcome.say(f"emergency off: {status.emergency_off}")
    outcome.say(f"emergency stop: {status.emergency_stop}")
    outcome.say(f"start mode: {start_mode} (bit 2)")
    outcome.say(f"service mode: {status.service_mode}")
    outcome.say(f"powering up: {status.powering_up}")
    outcome.say(f"ram error: {status.ram_error}")
    return outcome


def register(app: typer.Typer) -> None:
    """Wire `version` and `status` onto `app`.

    Both open a `Station`, build a `CommandResult`, and close the station in
    `finally` - even when building the result raises - so a spy on
    `Station.close` always sees exactly one call regardless of how `work()`
    ends. `run()` never returns (`NoReturn`): it renders the result (or an
    error) and raises `typer.Exit` itself, so neither command body wraps the
    call in its own `raise typer.Exit(code=...)`.
    """

    @app.command("version")
    def version_command(ctx: typer.Context) -> None:
        cli_ctx = ctx.obj
        settings = cli_ctx.settings

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                version = station.version()
                # Built inline from the SAME StationVersion already fetched
                # above, not via station_info(station) - that helper exists
                # for commands (like `status`, below) that have no
                # StationVersion of their own and would otherwise have to
                # query it a second time just for the envelope's station block.
                outcome = build_version(version, tool_version=__version__)
                outcome.link = link_info(station, settings)
                outcome.station = StationInfo(
                    protocol="xpressnet",
                    protocol_version=version.version,
                    command_station_id=version.station_id,
                )
            finally:
                station.close()
            return outcome

        run("version", cli_ctx.output, work)

    @app.command("status")
    def status_command(ctx: typer.Context) -> None:
        cli_ctx = ctx.obj
        settings = cli_ctx.settings

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                outcome = build_status(station.status())
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            finally:
                station.close()
            return outcome

        run("status", cli_ctx.output, work)
```

- [ ] **Step 12: Run the wiring tests to see the basics section pass**

```bash
uv run pytest tests/cli/test_wiring.py -k "build_version or build_status"
```

Expected: `5 passed`.

- [ ] **Step 13: Write the failing `main.py` wiring tests**

First add two more import lines to the same top-of-file import block in `tests/cli/test_wiring.py`
(next to `import sys`, and next to the `railctl.cli.deps` import respectively):

```python
import runpy
```

```python
import railctl.cli.main as cli_main
```

Then append the new tests to the end of the file:

```python
# -- main.py: app wiring, error paths, __main__ -------------------------------


@pytest.fixture(autouse=True)
def _isolated_config_dir(monkeypatch, tmp_path):
    # Every test below either builds `app`/`main()` for real or imports a
    # module that resolves `config_path()` at call time - none of them may
    # ever touch a developer's real ~/.config/railctl.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        monkeypatch.delenv(key, raising=False)


def _patch_station(monkeypatch, station):
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: station))


def test_version_command_json_output_is_one_value_with_station_facts(monkeypatch):
    _patch_station(monkeypatch, _FakeStation(raw_version=0x40, station_id=0x12))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--format", "json", "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["station"]["protocol_version"] == "4.0"
    assert payload["station"]["command_station_id"] == 18
    assert payload["result"]["tool_version"] == "0.1.0"


def test_version_command_human_output_contains_the_same_facts(monkeypatch):
    _patch_station(monkeypatch, _FakeStation(raw_version=0x40, station_id=0x12))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 0
    for fact in ("4.0", "18", "0.1.0"):
        assert fact in result.stdout


def test_status_command_json_carries_raw_byte_and_decoded_names(monkeypatch):
    class _StatusStation(_FakeStation):
        def status(self):
            return StationStatus.from_raw(0x04)

    _patch_station(monkeypatch, _StatusStation())
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--format", "json", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["raw_hex"] == "0x04"
    assert payload["result"]["auto_start_mode"] is True
    assert "short" not in json.dumps(payload).lower()


def test_status_command_human_carries_raw_byte_and_decoded_names(monkeypatch):
    class _StatusStation(_FakeStation):
        def status(self):
            return StationStatus.from_raw(0x04)

    _patch_station(monkeypatch, _StatusStation())
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["status"])
    assert result.exit_code == 0
    assert "0x04" in result.stdout
    assert "start mode" in result.stdout
    assert "short" not in result.stdout.lower()


def test_open_station_failure_exits_3_with_empty_stdout_and_json_stderr(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("the port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["exit_code"] == 3


def test_station_is_closed_even_when_the_command_body_raises(monkeypatch):
    class _FailingStation(_FakeStation):
        def version(self):
            raise TransportError("station went away mid-read")

    station = _FailingStation()
    _patch_station(monkeypatch, station)
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert station.closed is True
    assert result.exit_code == 3


def test_address_out_of_range_exits_2_before_any_command_runs(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["railctl", "--address", "20000", "status"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2


def test_address_out_of_range_writes_json_error_and_empty_stdout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["railctl", "--address", "20000", "status"])
    with pytest.raises(SystemExit):
        cli_main.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["exit_code"] == 2


def test_bad_config_file_exits_2_naming_file_line_and_key(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "railctl" / "config.toml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("bogus = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["railctl", "status"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    message = json.loads(capsys.readouterr().err)["message"]
    assert str(bad) in message
    assert "bogus" in message


def test_dunder_main_module_calls_main(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_main, "main", lambda: calls.append(True))
    runpy.run_module("railctl.__main__", run_name="__main__")
    assert calls == [True]
```

- [ ] **Step 14: Run the wiring tests to see the `main.py` section fail**

```bash
uv run pytest tests/cli/test_wiring.py -k "command or dunder_main"
```

Expected: `ModuleNotFoundError: No module named 'railctl.cli.main'` - `src/railctl/cli/main.py` and `src/railctl/__main__.py` do not exist yet.

- [ ] **Step 15: Implement `src/railctl/cli/main.py` and `src/railctl/__main__.py`**

```python
# src/railctl/cli/main.py
"""The Typer app: global options, `ctx.obj` wiring, and the process entry point.

Later tasks add commands by writing their own `commands/*.py` module with a
`register(app)` function and adding one import + one call at the bottom of
this file - they never need to edit `global_options` or `CliContext`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import TextIO

import typer

from railctl.cli._errors import OutputContext, report_for
from railctl.cli.commands import basics
from railctl.cli.config import config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging
from railctl.cli.render import want_color
from railctl.errors import RailctlError, exit_code_for

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"max_content_width": 100},
)


@dataclass(frozen=True, slots=True)
class CliContext:
    """Resolved once per invocation, read by every command via `ctx.obj`."""

    settings: Settings
    output: OutputContext


def context_for(settings: Settings, *, stdout: TextIO, stderr: TextIO) -> OutputContext:
    return OutputContext(
        fmt=settings.fmt,
        color=want_color(settings.color, stdout, os.environ),
        stdout=stdout,
        stderr=stderr,
    )


@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = typer.Option(None, "--target", help="auto, serial:<path>, or z21:<host>:<port>"),
    address: int = typer.Option(None, "--address", "-a", help="locomotive address, 1..9999"),
    format_: str = typer.Option(None, "--format", help="human, json, or ndjson"),
    json_flag: bool = typer.Option(False, "--json", help="alias for --format=json"),
    verbose: int = typer.Option(
        None, "-v", "--verbose", count=True, help="repeatable: -v decoded frames, -vv raw bytes"
    ),
    color: str = typer.Option("auto", "--color", help="auto, always, or never"),
    yes: bool = typer.Option(False, "--yes", "-y", help="answer every confirmation yes"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="never prompt, even on a real terminal"
    ),
) -> None:
    config = load_config(config_path())
    settings = build_settings(
        target=target,
        address=address,
        fmt=format_,
        json_flag=json_flag,
        verbose=verbose,
        color=color,
        yes=yes,
        non_interactive=non_interactive,
        env=os.environ,
        config=config,
        stdin=sys.stdin,
    )
    configure_logging(settings.verbose, sys.stderr)
    ctx.obj = CliContext(
        settings=settings,
        output=context_for(settings, stdout=sys.stdout, stderr=sys.stderr),
    )


basics.register(app)


def main() -> None:
    """Process entry point. Catches only what can be raised BEFORE a command's
    own `run()` gets a chance to: a bad `config.toml`, or an invalid global
    option (an out-of-range --address, a --json/--format conflict). Once a
    command body starts, `run()` (Task 8) already converts its failures to
    `typer.Exit`, which Typer's own dispatch handles without reaching here.
    """
    try:
        app()
    except (RailctlError, ValueError) as exc:
        code = exit_code_for(exc) if isinstance(exc, RailctlError) else 2
        print(json.dumps(report_for(exc, command="railctl").envelope()), file=sys.stderr)
        raise SystemExit(code) from None
```

```python
# src/railctl/__main__.py
from railctl.cli.main import main

if __name__ == "__main__":
    main()
```

Also change `pyproject.toml`'s console-script entry point so the installed
`railctl` command runs `main()` - not the bare `app` object - and therefore
takes exactly the same path as `python -m railctl`. Without this the JSON-error
handling `main()` just added for a bad `config.toml` or an out-of-range
`--address` only ever runs under `python -m`; the installed script would still
call the Typer app directly and let the exception propagate as a raw
traceback instead:

```diff
 [project.scripts]
-railctl = "railctl.cli.main:app"
+railctl = "railctl.cli.main:main"
```

- [ ] **Step 16: Run the full wiring test file to see it pass**

```bash
uv run pytest tests/cli/test_wiring.py
```

Expected: `40 passed` (25 from the deps section, 5 from the basics section, 10 from the main section).

- [ ] **Step 17: Run the whole `tests/cli` directory**

```bash
uv run pytest tests/cli
```

Expected: `57 passed` (17 from `test_config.py` plus 40 from `test_wiring.py`; Task 8's own `tests/cli/test_format_modes.py` and `tests/cli/test_errors.py`, if collected here too, add to this number - compare only that the run reports `0 failed`).

- [ ] **Step 18: Run the full suite**

```bash
uv run pytest
```

Expected: `0 failed`. The total is whatever Tasks 1-8 left the suite at, plus the 57 tests this task adds - if that arithmetic does not match what actually prints, treat it as this plan's estimate being off by a small, explainable amount, not as a real signal; a *different* test failing is the real signal.

- [ ] **Step 19: Check the coverage gate**

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table now includes `src/railctl/cli/config.py`, `src/railctl/cli/deps.py`, `src/railctl/cli/main.py`, `src/railctl/cli/commands/__init__.py`, `src/railctl/cli/commands/basics.py` and `src/railctl/__main__.py`, then `Required test coverage of 90% reached.` and `0 failed`. `src/railctl/cli/main.py`'s `global_options` callback has a branch through every one of `build_settings`'s failure paths exercised in Step 13 (`--address` out of range, the `--json`/`--format` conflict, a bad config file) plus its own success path (every command test in Step 13 goes through it) - if any branch in `config.py` or `deps.py` is missing from `term-missing`, it is a gap in this task's own tests, not a pre-existing shortfall to defer.

- [ ] **Step 20: Lint and format check**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both report no issues. `ruff check .` in particular must NOT flag `typer.Option(None, ...)` under `B008` - if it does, re-read the note above about Ruff's bugbear allowlist before reaching for a `# noqa`, since a real hit here would mean something about this task's code does not match the verified pattern (for example, wrapping the option call in another expression).

- [ ] **Step 21: Commit**

```bash
git add src/railctl/cli/config.py src/railctl/cli/deps.py src/railctl/cli/main.py \
        src/railctl/cli/commands/__init__.py src/railctl/cli/commands/basics.py \
        src/railctl/__main__.py tests/cli/test_config.py tests/cli/test_wiring.py
git commit -m "feat(cli): add config resolution, global options, and version/status commands"
```

---

### Task 10: The command metadata table and `railctl schema`

**Design specification:** `docs/superpowers/specs/2026-08-03-railctl-design.md` lines 1231-1266
(L2, the command tree and the global option table), 1350-1354 (L5, `railctl schema`), 1356-1373
(L6, the exit-code meanings - already the single source of truth in `errors.py`'s own docstrings,
which `help_epilog` below reads rather than re-typing). Read those before writing a line here -
every literal value below is quoted from them, not invented.

**Files:**
- Create: `src/railctl/cli/_meta.py`, `src/railctl/cli/commands/schema.py`, `tests/cli/test_schema.py`
- Modify: `src/railctl/cli/main.py` (the `global_options` callback's eight parameter defaults, and
  the registration block at the bottom), `src/railctl/cli/commands/basics.py` (both commands'
  decorators gain `help=`/`epilog=` from the table, both commands' signatures gain the eight
  per-command global options, and both bodies gain a `merge_settings` call and rebuild their
  `OutputContext` from it - see must-pin 4 below; this is a bigger edit to this file than "add
  `help=`", because 2.32's fix for `railctl doctor --address 3` has to start somewhere, and `status`/
  `version` are the only registered commands old enough to prove it works before `doctor` exists)

**What is really on disk when this task starts - read before writing a line of code:**

Task 9 registers exactly two commands, `version` and `status`, both taking **no parameters beyond
`ctx: typer.Context`** - `commands/basics.py`'s `register(app)` is:

```python
@app.command("version")
def version_command(ctx: typer.Context) -> None:
    cli_ctx = ctx.obj
    settings = cli_ctx.settings

    def work() -> CommandResult:
        station = open_station(settings, capabilities_path=capabilities_path())
        try:
            version = station.version()
            outcome = build_version(version, tool_version=__version__)
            outcome.link = link_info(station, settings)
            outcome.station = StationInfo(
                protocol="xpressnet",
                protocol_version=version.version,
                command_station_id=version.station_id,
            )
        finally:
            station.close()
        return outcome

    run("version", cli_ctx.output, work)

@app.command("status")
def status_command(ctx: typer.Context) -> None:
    cli_ctx = ctx.obj
    settings = cli_ctx.settings

    def work() -> CommandResult:
        station = open_station(settings, capabilities_path=capabilities_path())
        try:
            outcome = build_status(station.status())
            outcome.link = link_info(station, settings)
            outcome.station = station_info(station)
        finally:
            station.close()
        return outcome

    run("status", cli_ctx.output, work)
```

`main.py`'s `global_options` callback is the only place any CLI parameter is declared today:

```python
@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = typer.Option(None, "--target", help="auto, serial:<path>, or z21:<host>:<port>"),
    address: int = typer.Option(None, "--address", "-a", help="locomotive address, 1..9999"),
    format_: str = typer.Option(None, "--format", help="human, json, or ndjson"),
    json_flag: bool = typer.Option(False, "--json", help="alias for --format=json"),
    verbose: int = typer.Option(
        None, "-v", "--verbose", count=True, help="repeatable: -v decoded frames, -vv raw bytes"
    ),
    color: str = typer.Option("auto", "--color", help="auto, always, or never"),
    yes: bool = typer.Option(False, "--yes", "-y", help="answer every confirmation yes"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="never prompt, even on a real terminal"
    ),
) -> None:
```

`doctor`, `power on`, `power off`, `stop`, `drive` and `function` do not exist as Typer commands
yet. `Station.probe()` (the capability probe `doctor` prints) is finished on disk (M5, Task 7), but
that same task's own text says in so many words: *"M5's verification sentence ... depends on the
CLI task (Task 12) still to come, which prints `verdict_lines(report)` ... this task's job ends at
`Station.probe()` returning a correct, fully-populated `DoctorReport`."* `Station.power_on`,
`.power_off`, `.emergency_stop`, `.drive`, `.function_set` also exist on disk (M5, Tasks 2-3), but
no CLI task has wired them yet either.

**This changes what `COMMANDS` can honestly contain today.** The design's full command tree has
nine M6 leaves, but a `CommandMeta` row for `power on` while no `power on` Typer command exists is
exactly the "stale row" the bidirectional drift test (`what tests must pin #1` below) exists to
fail on - and it is precisely the failure mode this whole project is built around: a capability
recorded as present (or absent) for a reason that has nothing to do with whether it is true.
`COMMANDS` in this task therefore holds one row per command **actually registered by this commit**:
`status`, `version` (already on disk, Task 9) and `schema` (this task). The type stays exactly
`Final[tuple[CommandMeta, ...]]` and every later task that adds a command - the movement commands
(`power on`, `power off`, `stop`, `drive`, `function`) and `doctor` - extends this same literal with
its own row **in the same commit that registers its command**, importing `command_meta`,
`typer_option` and `typer_argument` from this module rather than hand-rolling a `typer.Option`. That
is the one-metadata-source rule applied to a table that is still growing, not a table already
closed; three real, provable rows are worth more than nine the drift test cannot check.

Tasks 11 and 12 extend this exact literal - `path: str`, a tuple container, `schema` required on
every row, `command_meta` staying a pure lookup, `COMMANDS` rebuilt rather than appended to. (Three
other designs were floated for this table across the plan - one with `path` as a tuple, one with
`COMMANDS` as a dict, one where `command_meta` *appends* a row instead of looking one up - all
wrong; this task's shape is the one every test below is written against.) The full nine-path tree
order is `doctor, status, version, power, stop, drive, function, monitor, schema` (design spec L2,
minus the `cv`/`backup`/`restore`/`diff` commands that belong to a later plan). Notice that `doctor`
sits **first** in that order even though `commands/doctor.py` is the *last* of the six command
modules to land, in Task 12: each later task inserts its row where the tree says it belongs, not at
the end of the tuple - which is exactly why `COMMANDS` is a literal every task rebuilds in full,
never a list any task appends to at import time.

**Interfaces:**

- Consumes, exactly as merged on disk:
  - `railctl.cli.result.Format = Literal["human", "json", "ndjson"]` - **not** an `enum.Enum`; there
    is no `Format.human` to write anywhere in this task
  - `railctl.cli.result.CommandResult` - mutable `@dataclass`, `schema: str`, `command: str`,
    `ok: bool = True`, `exit_code: int = 0`, `elapsed_ms: int = 0`, `link: LinkInfo | None = None`,
    `station: StationInfo | None = None`, `warnings: list[ResultWarning]`,
    `result: dict[str, object]`, `lines: list[str]`, `.say(line)`, `.warn(name, message, **details)`,
    `.envelope()`
  - `railctl.cli._errors.OutputContext` - `@dataclass(frozen=True, slots=True)`, `fmt: Format`,
    `color: bool`, `stdout: TextIO`, `stderr: TextIO`
  - `railctl.cli._errors.run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) -> NoReturn` -
    calls `work()`, times it, renders the result, and on `RailctlError` **or plain `ValueError`**
    writes one `railctl/error/v1` object to `ctx.stderr`. **`run` never returns a value - it raises
    `typer.Exit` itself.** Every command below ends with a **bare** call, `run(...)`, never
    `raise typer.Exit(code=run(...))`; wrapping it that way was written against a `run() -> int` that
    is not the real signature, and `schema.py`'s `register()` is the corrected form.
  - `railctl.cli.render.render(result, *, fmt, stdout, color) -> None` - called by `run()` only, never
    by a command module directly - and
    `railctl.cli.render.want_color(choice: str, stream: TextIO, env: Mapping[str, str]) -> bool` -
    what a command calls itself when it needs to rebuild its own `OutputContext` after a per-command
    `--format`/`--color` override (must-pin 4 below)
  - `railctl.cli.deps.Settings` and
    `railctl.cli.deps.merge_settings(base: Settings, *, target: str | None = None, address: int | None = None, fmt: str | None = None, json_flag: bool = False, verbose: int = 0, color: str | None = None, yes: bool = False, non_interactive: bool = False) -> Settings` -
    the sentinel rule: an argument overrides `base` only when it is "typed" - not `None` for the
    string/int fields, `True` for the three booleans, `> 0` for `verbose`. Calling it with every
    keyword every time (as below) is exactly what the sentinel rule is for: nothing typed at the
    command level leaves `base` untouched.
  - `railctl.cli.main.app: typer.Typer`, `railctl.cli.main.CliContext` (frozen, `.settings`, `.output`,
    stored on `ctx.obj` by `global_options`)
  - `railctl.cli.commands.basics.register(app) -> None`, `VERSION_SCHEMA = "railctl/version/v1"`,
    `STATUS_SCHEMA = "railctl/status/v1"`
  - `railctl.cli.config.CONFIG_KEYS: Final[tuple[str, ...]] = ("target", "address", "verbose")`
  - `railctl.errors.EXIT_CODES: Final[dict[type[RailctlError], int]]` (verified on disk, reproduced
    at the top of this file's read) and `UNMAPPED_EXIT_CODE = 1`. `help_epilog` (below) reverses this
    same map to print each exit code's meaning straight from the exception class's own docstring -
    the one place that meaning is already written down - rather than typing a second, driftable copy
    of it into `_meta.py`.
  - `railctl.station.Station` (only `.open` is ever monkeypatched, exactly the way
    `tests/cli/test_wiring.py` already does it) and `railctl.xbus.replies.StationStatus`,
    `railctl.xbus.replies.StationVersion` - used only inside this task's own test file, for the fake
    station behind the two `status`-based invocation-order tests (must-pin 4)
  - `typer` (`typer.Typer`, `typer.Option`, `typer.Argument`, `typer.Context`, `typer.Exit`,
    `typer.BadParameter`), `typer.testing.CliRunner`, and `typer.main.get_command(app)` - the only
    way this task touches `click`, and only inside the test file, only for the drift and
    option-name-matching tests

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

```python
# src/railctl/cli/_meta.py
SCHEMA_SCHEMA: Final[str] = "railctl/schema/v1"
OptionType = Literal["string", "integer", "boolean", "enum"]

@dataclass(frozen=True, slots=True)
class Option:
    name: str; help: str; type: OptionType = "string"; short: str | None = None
    default: object = None; enum: tuple[str, ...] | None = None; required: bool = False
    env: str | None = None; repeatable: bool = False

@dataclass(frozen=True, slots=True)
class Argument:
    name: str; help: str; type: OptionType = "string"; required: bool = True
    enum: tuple[str, ...] | None = None

@dataclass(frozen=True, slots=True)
class CommandMeta:
    path: str; help: str; schema: str; mutates: bool; exit_codes: tuple[int, ...]
    arguments: tuple[Argument, ...] = (); options: tuple[Option, ...] = (); confirms: bool = False

GLOBAL_OPTIONS: Final[tuple[Option, ...]]      # --target, --address/-a, --format, --json,
                                                # --verbose/-v, --color, --yes/-y, --non-interactive
COMMANDS: Final[tuple[CommandMeta, ...]]       # status, version, schema - the commands this
                                                # commit actually registers, in tree order
BASE_EXIT_CODES: Final[tuple[int, ...]] = (0, 1, 2)

def command_meta(path: str) -> CommandMeta: ...          # KeyError -> ValueError naming near misses
def manifest(paths: Sequence[str] | None = None) -> dict[str, object]: ...
def typer_option(option: Option) -> Any: ...              # envvar only for a RAILCTL_*-prefixed row
def typer_argument(argument: Argument) -> Any: ...
def global_option(name: str) -> Any: ...                  # per-command copy: bare default, no envvar
def help_epilog(meta: CommandMeta) -> str: ...             # OUTPUT / EXIT CODES / EXAMPLES sections
```

```python
# src/railctl/cli/commands/schema.py
def build_schema(paths: Sequence[str] | None) -> CommandResult: ...
def register(app: typer.Typer) -> None: ...
```

**Notes the implementer must not re-derive:**

- **The enum guard must let the unset default through.** `typer_option` attaches a validating
  `callback` to any option that declares `enum=`. Click calls that callback for **every** resolution
  of the parameter, including when nothing was typed and the value is the option's own default. Both
  `--format` and its sibling `--color` are `enum` options, and `--format`'s default is `None` (so
  `build_settings` can tell "not given" from "given as human") - a callback that rejects anything not
  literally in the tuple would reject the default itself, and `railctl status` with no flags at all
  would exit 2 before ever reaching a command. The callback below is written `value is not None and
  value not in choices`, and Step 1's test is what catches a "simplification" that drops the
  `is not None` half.
- **`typer_option` must not pass `envvar=` for every row that declares `env=`.** `--color`'s row
  documents `env="NO_COLOR"` (the design's own global-option table names it), but `NO_COLOR` is a
  force-plain-text override read later, at render time, by `want_color` - it is not a value in the
  same CLI-flag/env/config/default precedence chain as `--target`/`--address`/`--format`/`--verbose`.
  Passing `envvar="NO_COLOR"` straight to `typer.Option` makes Click resolve `--color` to `"1"` for
  anyone with `NO_COLOR` set, and the enum guard above then rejects `"1"` as not one of
  `auto`/`always`/`never` - `railctl status` would exit 2 before touching a port for every developer
  who sets `NO_COLOR` globally. The row keeps `env="NO_COLOR"` as documentation (must-pin 9 below
  still asserts it is in the manifest); `typer_option` passes `envvar=option.env` to Click **only
  when `option.env` starts with `RAILCTL_`**. Step 9's `NO_COLOR` test is what catches a
  simplification that passes `envvar=option.env` unconditionally again.
- **`global_option(name)` never repeats the environment variable either, for a different reason.**
  Every registered command declares all eight global options a second time (must-pin 4) because
  Click parses a Typer group's own options only *before* the subcommand name - without a per-command
  copy, `railctl doctor --address 3` is a usage error before `doctor` ever runs, even though the
  design's own examples put the flag after the verb. The root `global_options` callback (`main.py`)
  is still the *only* place the environment and `config.toml` are actually resolved, once per
  invocation; a per-command copy that also read `RAILCTL_ADDRESS` would let Click resolve the same
  variable a second time at the subcommand level, and that second resolution would silently win over
  whatever `build_settings` already folded in from the config file. `global_option` therefore always
  passes `env=None` to `typer_option`, regardless of what the `GLOBAL_OPTIONS` row itself declares,
  and gives the option a "nothing typed here" default instead of the row's real one - `None` for a
  string/int field, `False` for a boolean, `0` for the repeatable `--verbose` counter - so
  `merge_settings` (Task 9) can tell "the command itself said nothing" from "the command repeated
  the root's own choice."
- **`help_epilog` reads its exit-code meanings from `errors.py`, never from a second table.** Each
  meaning is that exception class's own docstring, first line - `errors.EXIT_CODES` reversed gives
  the class for a mapped code, and codes 0/1/2 (success, unhandled internal error, usage error) have
  no exception class at all, so those three are the only literal strings this module owns. A rewrite
  that hard-codes a parallel `{5: "timeout", ...}` dict here would be exactly the kind of second,
  driftable source of truth the whole `_meta.py` module exists to prevent one layer up.
- **Build every `typer.Option`/`typer.Argument` at import time, into a module-level name, not inline
  as a parameter default.** `typer_option(row)` (or `global_option(name)`) called directly in a
  `def foo(x = typer_option(row))` signature is a function call in a default argument - Ruff's B008
  (bugbear) is in this project's `select` list, and its built-in allowlist covers a literal
  `typer.Option(...)`/`typer.Argument(...)` call, not an arbitrary wrapper function that returns one.
  `main.py`, `commands/schema.py` and `commands/basics.py` all compute their parameters once, right
  after the import, and reference the names in the signature - the same "build it once" shape
  `GLOBAL_OPTIONS` itself already has.
- **`schema.py` never calls `typer.echo` or raises `typer.Exit` for an unknown path.**
  `command_meta`/`manifest` raise a plain `ValueError`; `run()` (Task 8/9, already on disk) already
  maps any plain `ValueError` to exit 2 with a `railctl/error/v1` object on stderr - that is the
  generic "any `ValueError` is exit 2" rule `cli/deps.py`'s usage-error paths also rely on. Catching
  the `ValueError` here and hand-rolling a second exit-2 path would be the exact kind of duplicate
  machinery the design's "one metadata source" principle exists to prevent one layer up.
- **`railctl schema` opens no `Station` and needs no port**, even though it now declares
  `--address`/`--target` like every other command (must-pin 4) - those eight parameters are only
  ever forwarded into `merge_settings`, never into `open_station`. Step 9's test proves this by
  making `open_station` raise if it is ever called and then running `railctl schema status` to
  completion.
- **No "TTY" anywhere under `cli/`, including in a comment.** `tests/test_layering.py` rule 1 matches
  `\btty` case-insensitively. Nothing in this task needs the word; "terminal" and `stream.isatty()`
  (used only inside `render.py`, not here) are the safe spellings.
- **`--for SECONDS` on `drive` and `function` is deliberately absent from this milestone.** It
  belongs with the operation-resource work planned for a later plan, not with the metadata table
  itself; `_meta.py`'s module docstring (Step 3) records this once so nobody re-derives it while
  writing Task 11's `drive`/`function` rows.
- **`CliRunner()` takes no `mix_stderr` argument on the pinned Typer/Click version** -
  `result.stdout` and `result.stderr` are already separate streams on the plain constructor, and
  passing `mix_stderr=False` raises `TypeError` here. This was verified against the version `uv`
  resolves in this repo, not against a specific upstream release number that this repo's
  `pyproject.toml` does not itself pin - run
  `uv run python -c 'import typer; print(typer.__version__)'` if the behaviour below does not match.

---

- [ ] **Step 1: Write the failing structural tests for the metadata dataclasses and lookup**

```python
# tests/cli/test_schema.py
"""Pins the command metadata table and `railctl schema`.

`COMMANDS` holds one row per command this commit actually registers - see the
note at the top of this task in the plan. `status`, `version` and `schema`
are real; the movement commands and `doctor` extend this same tuple in their
own later commits.
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from railctl.cli import config
from railctl.cli._meta import (
    BASE_EXIT_CODES,
    COMMANDS,
    GLOBAL_OPTIONS,
    Argument,
    CommandMeta,
    Option,
    command_meta,
    global_option,
    help_epilog,
    manifest,
    typer_argument,
    typer_option,
)
from railctl.errors import EXIT_CODES

KNOWN_CODES = set(BASE_EXIT_CODES) | set(EXIT_CODES.values())


def test_command_meta_returns_the_row_for_a_known_path():
    assert command_meta("status").path == "status"
    assert command_meta("version").schema == "railctl/version/v1"
    assert command_meta("schema").mutates is False


def test_command_meta_unknown_path_orders_the_closest_match_first():
    with pytest.raises(ValueError, match="statuz") as caught:
        command_meta("statuz")
    message = str(caught.value)
    suggestions = message.split("closest known paths:")[1].strip()
    names = [n.strip() for n in suggestions.split(",")]
    assert names[0] == "status"  # the true near miss, ranked ahead of the other two


def test_global_options_cover_the_eight_design_flags():
    names = {o.name for o in GLOBAL_OPTIONS}
    assert names == {
        "--target", "--address", "--format", "--json",
        "--verbose", "--color", "--yes", "--non-interactive",
    }
    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--address"].short == "-a"
    assert by_name["--verbose"].short == "-v"
    assert by_name["--verbose"].repeatable is True
    assert by_name["--yes"].short == "-y"
    # None/False defaults are what let build_settings (Task 9) tell "not given"
    # from "given the human default" - a "helpful" non-None default here would
    # silently break every precedence test in tests/cli/test_config.py.
    assert by_name["--target"].default is None
    assert by_name["--format"].default is None
    assert by_name["--verbose"].default is None
    assert by_name["--color"].default == "auto"


def test_config_backed_global_options_match_config_keys():
    flag_names = {o.name.lstrip("-") for o in GLOBAL_OPTIONS}
    assert set(config.CONFIG_KEYS) <= flag_names


def test_every_real_manifest_exit_code_is_a_known_code():
    for meta in COMMANDS:
        assert set(meta.exit_codes) <= KNOWN_CODES, meta.path


def test_typer_option_enum_guard_allows_the_unset_default_through():
    option = Option(name="--format", help="x", type="enum", enum=("human", "json"), default=None)
    built = typer_option(option)
    assert built.callback(None) is None  # the default itself must never be rejected


def test_typer_option_enum_guard_rejects_an_unknown_choice():
    option = Option(name="--format", help="x", type="enum", enum=("human", "json"), default=None)
    built = typer_option(option)
    with pytest.raises(typer.BadParameter):
        built.callback("xml")


def test_typer_option_builds_the_repeatable_count_flag_for_verbose():
    verbose = next(o for o in GLOBAL_OPTIONS if o.name == "--verbose")
    built = typer_option(verbose)
    assert built.count is True
    assert built.default is None


def test_typer_argument_required_uses_ellipsis_and_optional_uses_none():
    required = typer_argument(Argument(name="cv", help="x"))
    optional = typer_argument(Argument(name="path", help="x", required=False))
    assert required.default is ...
    assert optional.default is None


def test_manifest_with_no_path_returns_the_tree_shape():
    payload = manifest(None)
    assert payload["schema"] == "railctl/schema/v1"
    assert [c["path"] for c in payload["commands"]] == ["status", "version", "schema"]
    assert {o["name"] for o in payload["global_options"]} == {o.name for o in GLOBAL_OPTIONS}


def test_manifest_for_a_single_path_matches_the_tree_entry_shape():
    tree = manifest(None)
    single = manifest(["status"])
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert single["command"] == tree_entry


def test_manifest_for_an_unknown_path_raises_value_error():
    with pytest.raises(ValueError, match="power on"):
        manifest(["power", "on"])


def test_global_option_builds_a_bare_copy_with_no_envvar():
    # Exercises all three `_bare_default` branches: a plain string/int field,
    # the repeatable counter, and a boolean flag.
    address = global_option("--address")
    assert address.envvar is None  # the root callback already resolved RAILCTL_ADDRESS once
    assert address.default is None
    verbose = global_option("--verbose")
    assert verbose.default == 0  # a per-command --verbose is a bare counter, never "not given"
    assert verbose.count is True  # the enum/count machinery still comes from typer_option
    yes = global_option("--yes")
    assert yes.default is False


def test_help_epilog_includes_headings_and_meanings_for_every_exit_code():
    epilog = help_epilog(command_meta("status"))
    assert "OUTPUT" in epilog
    assert "EXIT CODES" in epilog
    assert "EXAMPLES" in epilog
    assert "railctl/status/v1" in epilog
    # Code 5 is LinkTimeout - pin the actual meaning, not just "a line exists",
    # so a rewrite that hard-codes a placeholder string here goes red too.
    assert "5: No reply arrived within the budget" in epilog
    # Codes 0/1/2 have no exception class; this is the branch that does not
    # go through errors.EXIT_CODES at all.
    assert "2: usage error" in epilog
```

- [ ] **Step 2: Run the new tests to see them fail for a named reason**

```bash
uv run pytest tests/cli/test_schema.py
```

Expected: `ModuleNotFoundError: No module named 'railctl.cli._meta'` - nothing in this file exists yet.

- [ ] **Step 3: Implement `src/railctl/cli/_meta.py`**

```python
# src/railctl/cli/_meta.py
"""The single source of every CLI parameter: `main.py`'s global options and
every command's own options and arguments are built from the rows below by
`typer_option`/`typer_argument`/`global_option`, and `railctl schema`'s
manifest is generated from the exact same rows. A flag name, default or help
string edited in only one of the two places is the drift
`tests/cli/test_schema.py` exists to catch.

`COMMANDS` holds one row per command this commit registers, not the whole
design's nine-leaf tree - see this task's note in the plan for why a row with
no matching Typer command would be the wrong kind of "documented".

`--for SECONDS` on `drive` and `function` is deliberately absent from this
milestone's tree. It belongs with the operation-resource work planned for a
later plan, not with this table.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

import typer

from railctl import errors

SCHEMA_SCHEMA: Final[str] = "railctl/schema/v1"

OptionType = Literal["string", "integer", "boolean", "enum"]


@dataclass(frozen=True, slots=True)
class Option:
    name: str
    help: str
    type: OptionType = "string"
    short: str | None = None
    default: object = None
    enum: tuple[str, ...] | None = None
    required: bool = False
    env: str | None = None
    repeatable: bool = False


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    help: str
    type: OptionType = "string"
    required: bool = True
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CommandMeta:
    path: str
    help: str
    schema: str
    mutates: bool
    exit_codes: tuple[int, ...]
    arguments: tuple[Argument, ...] = ()
    options: tuple[Option, ...] = ()
    confirms: bool = False


# Order and defaults are quoted from the design spec's global option table
# (L2). `--target`/`--format`/`--verbose` default to `None`, not their eventual
# human-readable default, because `build_settings` (Task 9) uses `None` as
# "nothing was typed" to fall through to the environment, the config file and
# only then the built-in default - a non-None default here would make every
# flag look "given" on every invocation.
GLOBAL_OPTIONS: Final[tuple[Option, ...]] = (
    Option(
        name="--target", help="auto, serial:<path>, or z21:<host>:<port>",
        type="string", default=None, env="RAILCTL_TARGET",
    ),
    Option(
        name="--address", help="locomotive address, 1..9999", type="integer",
        short="-a", default=None, env="RAILCTL_ADDRESS",
    ),
    Option(
        name="--format", help="human, json, or ndjson", type="enum",
        enum=("human", "json", "ndjson"), default=None, env="RAILCTL_FORMAT",
    ),
    Option(name="--json", help="alias for --format=json", type="boolean", default=False),
    Option(
        name="--verbose", help="repeatable: -v decoded frames, -vv raw bytes",
        type="integer", short="-v", default=None, env="RAILCTL_VERBOSE", repeatable=True,
    ),
    # `--color`'s only environment input is NO_COLOR (design spec table), read
    # later at render time - `NO_COLOR`/`TERM=dumb` are a force-plain-text
    # override, not a value in the same precedence chain as the three above.
    # Kept here as documentation (must-pin 9 asserts it is in the manifest);
    # `typer_option` below never lets a non-`RAILCTL_*` `env` reach Click.
    Option(
        name="--color", help="auto, always, or never", type="enum",
        enum=("auto", "always", "never"), default="auto", env="NO_COLOR",
    ),
    Option(name="--yes", help="answer every confirmation yes", type="boolean",
           short="-y", default=False),
    Option(name="--non-interactive", help="never prompt, even on a real terminal",
           type="boolean", default=False),
)

BASE_EXIT_CODES: Final[tuple[int, ...]] = (0, 1, 2)

_STATUS = CommandMeta(
    path="status",
    help="Command station status: raw byte and decoded bits",
    schema="railctl/status/v1",
    mutates=False,
    exit_codes=(0, 1, 2, 3, 4, 5, 9),
)
_VERSION = CommandMeta(
    path="version",
    help="XpressNet version and command station id",
    schema="railctl/version/v1",
    mutates=False,
    exit_codes=(0, 1, 2, 3, 4, 5, 9),
)
_SCHEMA = CommandMeta(
    path="schema",
    help="Machine-readable manifest of the command tree",
    schema=SCHEMA_SCHEMA,
    mutates=False,
    exit_codes=(0, 1, 2),
    arguments=(
        Argument(
            name="path", help="command path words, for example: status",
            type="string", required=False,
        ),
    ),
)

# Tree order per the design spec's L2 ASCII listing: status, version, ...,
# schema last. Each later task that adds a command rebuilds this literal in
# full, its own row inserted where the nine-path tree order puts it - never
# appended to the end - so `doctor`, added last (Task 12), still lands first.
COMMANDS: Final[tuple[CommandMeta, ...]] = (_STATUS, _VERSION, _SCHEMA)

_BY_PATH: Final[dict[str, CommandMeta]] = {c.path: c for c in COMMANDS}


def command_meta(path: str) -> CommandMeta:
    try:
        return _BY_PATH[path]
    except KeyError:
        near = difflib.get_close_matches(path, _BY_PATH, n=3, cutoff=0.0)
        raise ValueError(
            f"no such command {path!r}; closest known paths: {', '.join(near)}"
        ) from None


def _enum_guard(choices: tuple[str, ...]) -> Callable[[str | None], str | None]:
    def _validate(value: str | None) -> str | None:
        # `value is None` is the option's own unset default (see the note in
        # the plan) - only a value someone actually typed is checked.
        if value is not None and value not in choices:
            raise typer.BadParameter(f"must be one of: {', '.join(choices)}")
        return value

    return _validate


def typer_option(option: Option) -> Any:
    """The one place a `typer.Option` is built, from one metadata row."""
    names: list[str] = [option.name]
    if option.short is not None:
        names.append(option.short)
    # Only a RAILCTL_*-prefixed row is a real environment input in the same
    # precedence chain `build_settings` resolves. `--color`'s `env="NO_COLOR"`
    # is documentation, not a Click envvar: NO_COLOR is a later, render-time
    # override, and letting Click resolve it here as well would make the enum
    # guard below reject "1" and exit every command at 2 for anyone who sets
    # NO_COLOR globally.
    envvar = option.env if option.env is not None and option.env.startswith("RAILCTL_") else None
    return typer.Option(
        option.default,
        *names,
        help=option.help,
        envvar=envvar,
        count=option.repeatable,
        callback=_enum_guard(option.enum) if option.enum is not None else None,
    )


def typer_argument(argument: Argument) -> Any:
    """The one place a `typer.Argument` is built, from one metadata row."""
    return typer.Argument(
        ... if argument.required else None,
        help=argument.help,
        callback=_enum_guard(argument.enum) if argument.enum is not None else None,
    )


def _bare_default(option: Option) -> object:
    """The "nothing typed at the command level" default for `global_option`'s
    per-command copy - never the row's own real default, which belongs to the
    root callback alone."""
    if option.type == "boolean":
        return False
    if option.repeatable:
        return 0
    return None


def global_option(name: str) -> Any:
    """A per-command copy of a `GLOBAL_OPTIONS` row: same flags, help and enum
    guard, but a bare "nothing typed here" default and no `envvar`.

    Every registered command declares all eight of these because Click parses
    a Typer group's own options only *before* the subcommand name - without a
    per-command copy, `railctl doctor --address 3` is a usage error before
    `doctor` ever runs, even though the design's own examples put the flag
    after the verb. The root `global_options` callback (`main.py`) still owns
    the one real resolution of the environment and the config file; repeating
    `envvar=` here would let Click read the same variable a second time at the
    subcommand level, and that second read would silently win over whatever
    `build_settings` already folded in from `config.toml`.
    """
    row = next(o for o in GLOBAL_OPTIONS if o.name == name)
    bare = replace(row, default=_bare_default(row), env=None)
    return typer_option(bare)


_BASE_EXIT_MEANINGS: Final[dict[int, str]] = {
    0: "success",
    1: "unhandled internal error",
    2: "usage error - a bad flag, value, or missing argument",
}


def help_epilog(meta: CommandMeta) -> str:
    """The fixed `OUTPUT` / `EXIT CODES` / `EXAMPLES` sections appended as
    this command's Typer `epilog`. Click supplies `Usage:` and `Options:` on
    its own. Built from `meta` and `errors.EXIT_CODES` alone, never a clock or
    a terminal size, so two runs of the same command produce byte-identical
    text - `app`'s `context_settings={"max_content_width": 100}` (Task 9)
    makes that true regardless of whether the stream is a terminal too.
    """
    by_code = {code: klass for klass, code in errors.EXIT_CODES.items()}
    lines = [
        "OUTPUT",
        f"  schema: {meta.schema}",
        "  formats: human, json, ndjson",
        "",
        "EXIT CODES",
    ]
    for code in meta.exit_codes:
        meaning = _BASE_EXIT_MEANINGS.get(code)
        if meaning is None:
            doc = by_code[code].__doc__ or ""
            meaning = doc.strip().splitlines()[0]
        lines.append(f"  {code}: {meaning}")
    required_args = " ".join(f"<{a.name}>" for a in meta.arguments if a.required)
    example = " ".join(w for w in (f"railctl {meta.path}", required_args, "--format json") if w)
    lines += ["", "EXAMPLES", f"  {example}"]
    return "\n".join(lines)


def _option_dict(option: Option) -> dict[str, object]:
    return {
        "name": option.name,
        "short": option.short,
        "help": option.help,
        "type": option.type,
        "default": option.default,
        "enum": list(option.enum) if option.enum is not None else None,
        "required": option.required,
        "env": option.env,
        "repeatable": option.repeatable,
    }


def _argument_dict(argument: Argument) -> dict[str, object]:
    return {
        "name": argument.name,
        "help": argument.help,
        "type": argument.type,
        "enum": list(argument.enum) if argument.enum is not None else None,
        "required": argument.required,
    }


def _command_dict(meta: CommandMeta) -> dict[str, object]:
    return {
        "path": meta.path,
        "help": meta.help,
        "schema": meta.schema,
        "mutates": meta.mutates,
        "confirms": meta.confirms,
        "exit_codes": list(meta.exit_codes),
        "arguments": [_argument_dict(a) for a in meta.arguments],
        "options": [_option_dict(o) for o in meta.options],
    }


def manifest(paths: Sequence[str] | None = None) -> dict[str, object]:
    """The `railctl/schema/v1` payload: the whole tree, or one command.

    `paths` is `None`/empty for the whole tree; otherwise its words are
    joined with a single space and looked up as one `CommandMeta.path` -
    `command_meta`'s `ValueError` on a miss is left to propagate, uncaught,
    so `run()` (Task 8/9) turns it into the standard exit-2 error envelope.
    """
    options = [_option_dict(o) for o in GLOBAL_OPTIONS]
    if not paths:
        return {
            "schema": SCHEMA_SCHEMA,
            "global_options": options,
            "commands": [_command_dict(c) for c in COMMANDS],
        }
    meta = command_meta(" ".join(paths))
    return {"schema": SCHEMA_SCHEMA, "global_options": options, "command": _command_dict(meta)}
```

- [ ] **Step 4: Run the tests written so far**

```bash
uv run pytest tests/cli/test_schema.py
```

Expected: `14 passed` (the original 13, minus the invented-exit-code test deleted below at Step 9's
sibling fix, plus the two new tests for `global_option` and `help_epilog` above - 12 + 2).

- [ ] **Step 5: Write the failing tests for `build_schema`**

```python
# tests/cli/test_schema.py (continued)
from railctl.cli.commands.schema import build_schema  # noqa: E402


def test_build_schema_returns_the_tree_when_no_path_is_given():
    result = build_schema(None)
    assert result.schema == "railctl/schema/v1"
    assert result.command == "schema"
    assert [c["path"] for c in result.result["commands"]] == ["status", "version", "schema"]


def test_build_schema_raises_value_error_for_an_unknown_path():
    with pytest.raises(ValueError, match="power on"):
        build_schema(["power", "on"])
```

- [ ] **Step 6: Run the tests to see them fail**

```bash
uv run pytest tests/cli/test_schema.py -k build_schema
```

Expected: `ModuleNotFoundError: No module named 'railctl.cli.commands.schema'`.

- [ ] **Step 7: Implement `src/railctl/cli/commands/schema.py`**

```python
# src/railctl/cli/commands/schema.py
"""`railctl schema`: the manifest generated from `railctl.cli._meta`'s table.

Opens no `Station` and touches no port - the one command an agent can use to
discover the whole CLI with the layout unplugged. Its only failure mode is an
unresolved command path, and that is a plain `ValueError` from `_meta.manifest`
left to propagate: `run()` (Task 8/9) already turns any `ValueError` into the
standard `railctl/error/v1` exit-2 envelope, so this module raises nothing of
its own and calls no rendering function directly.

Declares all eight global options a second time (`global_option`, `_meta.py`)
because Click parses a Typer group's own options only *before* the subcommand
name - without this, `railctl schema --format json` (flag after the verb, the
form every design-spec example uses) is a usage error. They are otherwise
inert here: `schema` never opens a `Station`, so `--address`/`--target` do
nothing beyond what `merge_settings` lets `--format`/`--color` do to this
command's own rendering.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import typer

from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import command_meta, global_option, manifest, typer_argument
from railctl.cli.deps import merge_settings
from railctl.cli.render import want_color
from railctl.cli.result import CommandResult

_PATH_ARGUMENT = typer_argument(command_meta("schema").arguments[0])
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")


def build_schema(paths: Sequence[str] | None) -> CommandResult:
    data = manifest(paths)
    return CommandResult(schema="railctl/schema/v1", command="schema", result=data)


def register(app: typer.Typer) -> None:
    @app.command(
        "schema",
        help=command_meta("schema").help,
        epilog=help_epilog(command_meta("schema")),
    )
    def schema_command(
        ctx: typer.Context,
        path: list[str] = _PATH_ARGUMENT,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings = merge_settings(
            cli_ctx.settings,
            target=target, address=address, fmt=format_, json_flag=json_flag,
            verbose=verbose, color=color, yes=yes, non_interactive=non_interactive,
        )
        # Rebuilt unconditionally, not only "when fmt or color changed": when
        # nothing was typed at the command level `merge_settings` returns the
        # same values `cli_ctx.settings` already had, so this only costs one
        # more `OutputContext` with identical fields, never a second branch to
        # get wrong.
        output = OutputContext(
            fmt=settings.fmt,
            color=want_color(settings.color, cli_ctx.output.stdout, os.environ),
            stdout=cli_ctx.output.stdout,
            stderr=cli_ctx.output.stderr,
        )

        def work() -> CommandResult:
            return build_schema(path or None)

        run("schema", output, work)
```

Note: `help_epilog` is imported alongside `command_meta`/`manifest`/`typer_argument` - the import
line above already lists it, this line only calls out that `register()` uses all four.

- [ ] **Step 8: Run the `build_schema` tests**

```bash
uv run pytest tests/cli/test_schema.py -k build_schema
```

Expected: `2 passed`.

- [ ] **Step 9: Write the failing CliRunner-level tests: drift, option names, JSON shape, help,
  global-option position**

Append to `tests/cli/test_schema.py`:

```python
import click  # noqa: E402  # only for isinstance(param, click.Option) - the drift test's one use

from railctl.cli.main import app  # noqa: E402
from railctl.station import Station  # noqa: E402
from railctl.xbus.replies import StationStatus, StationVersion  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config_dir(monkeypatch, tmp_path):
    # Every test below invokes the real `app`, whose root `global_options`
    # callback always calls `load_config(config_path())` before a subcommand
    # runs - without this, any test here would read whatever real
    # ~/.config/railctl/config.toml happens to exist on the machine running
    # the suite (the same isolation tests/cli/test_wiring.py already applies).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


class _FakeStatusStation:
    """Bare stand-in for the two `status`-based invocation-order tests below.
    `status` is the pinned example (not `schema`), because it is the one
    command in this file whose commands actually open a `Station` - proving
    the --format-position parity on a command that never touches a port would
    not catch a per-command global-option block that forgot to route through
    the real `open_station`/`run()` plumbing.
    """

    identity = "serial:7010A0001194:3"

    def status(self) -> StationStatus:
        return StationStatus.from_raw(0x00)

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def close(self) -> None:
        pass


def registered_paths(app: typer.Typer) -> set[str]:
    """Every leaf command path Typer actually routes, at any nesting depth -
    there is no nesting yet, but the walk costs nothing and later tasks add
    `power on`/`power off` under a `power` group without this helper changing.
    """
    click_app = typer.main.get_command(app)
    paths: set[str] = set()

    def _walk(group, prefix: str) -> None:
        for name, cmd in group.commands.items():
            path = f"{prefix} {name}".strip()
            if hasattr(cmd, "commands"):
                _walk(cmd, path)
            else:
                paths.add(path)

    _walk(click_app, "")
    return paths


def _leaf_command(path: str):
    click_app = typer.main.get_command(app)
    for word in path.split(" "):
        click_app = click_app.commands[word]
    return click_app


def _long_option_names(command) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        if isinstance(param, click.Option):
            names.update(opt for opt in param.opts if opt.startswith("--"))
    return names


def test_every_registered_command_has_a_metadata_row_and_vice_versa():
    assert registered_paths(app) == {c.path for c in COMMANDS}


def test_a_missing_registration_would_fail_the_drift_check():
    # A fresh app, deliberately NOT the shared `app` above, carrying one
    # command with no metadata row at all. Goes red if `registered_paths`
    # stops walking the Click tree for real - for example, a rewrite that
    # only reads `click_app.commands` without the recursive `_walk` would
    # still "find" a flat command, but a version that returned `set()`
    # unconditionally, or a hard-coded literal, would fail this immediately.
    copy = typer.Typer(add_completion=False)

    @copy.command("throwaway")
    def _throwaway() -> None:  # pragma: no cover - never invoked
        ...

    assert registered_paths(copy) - {c.path for c in COMMANDS} == {"throwaway"}


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_option_names_match_between_typer_and_metadata(meta: CommandMeta):
    # Every registered command declares its own metadata options PLUS the
    # eight global ones a second time (see the per-command global-option
    # note above) - a command that forgot that block would otherwise look
    # complete against only its own (possibly empty) option list, so the
    # union with GLOBAL_OPTIONS is what makes an omission fail here.
    typer_names = _long_option_names(_leaf_command(meta.path)) - {"--help"}
    assert typer_names == {o.name for o in meta.options} | {o.name for o in GLOBAL_OPTIONS}


def test_global_options_match_the_root_group():
    root = typer.main.get_command(app)
    typer_names = _long_option_names(root) - {"--help"}
    assert typer_names == {o.name for o in GLOBAL_OPTIONS}


def test_an_invalid_format_value_is_rejected_like_an_invalid_choice():
    result = runner.invoke(app, ["--format", "xml", "status"])
    assert result.exit_code == 2


def test_schema_json_prints_one_envelope_with_the_registered_paths_in_tree_order():
    result = runner.invoke(app, ["--format", "json", "schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "railctl/schema/v1"
    assert [c["path"] for c in payload["commands"]] == ["status", "version", "schema"]


def test_schema_for_a_not_yet_implemented_command_is_exit_2_with_near_misses():
    result = runner.invoke(app, ["schema", "power", "on"])
    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert "power on" in payload["message"]
    assert "status" in payload["message"]  # one of the three known paths, named


def test_schema_for_a_single_command_matches_the_tree_entry_shape():
    tree = json.loads(runner.invoke(app, ["--format", "json", "schema"]).stdout)
    single = json.loads(runner.invoke(app, ["--format", "json", "schema", "status"]).stdout)
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert single["command"] == tree_entry
    assert set(single["command"]) == set(tree_entry)


def test_help_is_deterministic_offline_and_unwrapped():
    first = runner.invoke(app, ["schema", "--help"])
    second = runner.invoke(app, ["schema", "--help"])
    assert first.exit_code == 0
    assert first.stdout == second.stdout  # two consecutive runs, byte-identical
    assert "schema" in first.stdout
    assert all(heading in first.stdout for heading in ("OUTPUT", "EXIT CODES", "EXAMPLES"))


@pytest.mark.parametrize("path,expected", [("status", False), ("version", False), ("schema", False)])
def test_mutates_flags_for_the_registered_commands(path: str, expected: bool):
    assert command_meta(path).mutates is expected


def test_global_options_carry_their_env_vars_and_no_color_on_color():
    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--target"].env == "RAILCTL_TARGET"
    assert by_name["--address"].env == "RAILCTL_ADDRESS"
    assert by_name["--format"].env == "RAILCTL_FORMAT"
    assert by_name["--verbose"].env == "RAILCTL_VERBOSE"
    assert by_name["--color"].env == "NO_COLOR"


def test_schema_never_opens_a_station(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("schema must never open a station")

    monkeypatch.setattr("railctl.cli.deps.open_station", _boom)
    result = runner.invoke(app, ["schema", "status"])
    assert result.exit_code == 0


def test_no_fuzzy_abbreviation_for_status():
    result = runner.invoke(app, ["st"])
    assert result.exit_code == 2
    assert "track power" not in result.stdout.lower()


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_command_help_text_matches_the_metadata_row(meta: CommandMeta):
    # This is what makes Step 13 a real test-first step rather than a
    # cosmetic tidy-up: `version`/`status` carry no `help=` today, so this
    # is red for both until basics.py starts reading it from the same row
    # the manifest reads.
    assert _leaf_command(meta.path).help == meta.help


def test_a_command_still_runs_with_no_color_set_in_the_environment():
    # `--color`'s row keeps `env="NO_COLOR"` as documentation (asserted
    # above) but `typer_option` must never forward it to Click - see the
    # note in the plan for the exit-2-for-everyone failure this guards
    # against.
    result = runner.invoke(app, ["schema"], env={"NO_COLOR": "1"})
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout


def test_format_after_the_subcommand_name_is_accepted(monkeypatch):
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation()))
    result = runner.invoke(app, ["status", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == "railctl/status/v1"


def test_format_before_or_after_the_subcommand_produces_identical_stdout(monkeypatch):
    # M6's acceptance sentence in miniature: `railctl status --format json`
    # and `railctl --format json status` must be indistinguishable to a
    # script. task-11.md and task-12.md pin the same property for `drive`
    # and `doctor` once those commands exist.
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation()))
    before = runner.invoke(app, ["--format", "json", "status"])
    after = runner.invoke(app, ["status", "--format", "json"])
    assert before.exit_code == after.exit_code == 0
    assert before.stdout == after.stdout
```

- [ ] **Step 10: Run the whole file to see the new tests fail**

```bash
uv run pytest tests/cli/test_schema.py
```

Expected: failures in every test that invokes `app` with `schema` (`schema.register(app)` has not
been called yet - Step 11 does that), in `test_option_names_match_between_typer_and_metadata[schema]`
and `test_command_help_text_matches_the_metadata_row[schema]` (same reason), and in
`test_option_names_match_between_typer_and_metadata[status]`/`[version]` and
`test_command_help_text_matches_the_metadata_row[status]`/`[version]` (`status_command`/
`version_command` still take only `ctx` - no `help=`, no options at all - until Step 13).
`test_every_registered_command_has_a_metadata_row_and_vice_versa` fails the same way:
`registered_paths(app)` returns `{"status", "version"}`, one short of the three-row table.
`test_format_after_the_subcommand_name_is_accepted` and
`test_format_before_or_after_the_subcommand_produces_identical_stdout` fail too - `status` does not
accept `--format` at the command level yet, so Click reports "No such option" for the per-command
parse. `test_a_command_still_runs_with_no_color_set_in_the_environment` fails because `schema` is
not registered. `test_a_missing_registration_would_fail_the_drift_check` already passes - it builds
its own throwaway `typer.Typer()` and never touches `app`.
`test_an_invalid_format_value_is_rejected_like_an_invalid_choice` already passes too:
`build_settings` (Task 9) already rejects an unknown `--format` value with a `ValueError`
independently of this task's own enum guard.

- [ ] **Step 11: Modify `src/railctl/cli/main.py` - source the global options from the table, register `schema`**

Replace the import block and `app`'s construction:

```python
# before
import json
import os
import sys
from dataclasses import dataclass
from typing import TextIO

import typer

from railctl.cli._errors import OutputContext, report_for
from railctl.cli.commands import basics
from railctl.cli.config import config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging
from railctl.cli.render import want_color
from railctl.errors import RailctlError, exit_code_for

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"max_content_width": 100},
)
```

```python
# after
import json
import os
import sys
from dataclasses import dataclass
from typing import TextIO

import typer

from railctl.cli._errors import OutputContext, report_for
from railctl.cli._meta import GLOBAL_OPTIONS, typer_option
from railctl.cli.commands import basics, schema
from railctl.cli.config import config_path, load_config
from railctl.cli.deps import Settings, build_settings, configure_logging
from railctl.cli.render import want_color
from railctl.errors import RailctlError, exit_code_for

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"max_content_width": 100},
)

# Built once, at import time, into names the callback below references - a
# call to `typer_option(...)` written directly as a parameter default would
# trip Ruff's B008 (function call in a default argument); its built-in
# allowlist covers a literal `typer.Option(...)` call, not a wrapper around one.
(
    _TARGET, _ADDRESS, _FORMAT, _JSON, _VERBOSE, _COLOR, _YES, _NON_INTERACTIVE,
) = (typer_option(option) for option in GLOBAL_OPTIONS)
```

Replace the `global_options` signature (the body is unchanged):

```python
# before
@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = typer.Option(None, "--target", help="auto, serial:<path>, or z21:<host>:<port>"),
    address: int = typer.Option(None, "--address", "-a", help="locomotive address, 1..9999"),
    format_: str = typer.Option(None, "--format", help="human, json, or ndjson"),
    json_flag: bool = typer.Option(False, "--json", help="alias for --format=json"),
    verbose: int = typer.Option(
        None, "-v", "--verbose", count=True, help="repeatable: -v decoded frames, -vv raw bytes"
    ),
    color: str = typer.Option("auto", "--color", help="auto, always, or never"),
    yes: bool = typer.Option(False, "--yes", "-y", help="answer every confirmation yes"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="never prompt, even on a real terminal"
    ),
) -> None:
```

```python
# after
@app.callback()
def global_options(
    ctx: typer.Context,
    target: str = _TARGET,
    address: int = _ADDRESS,
    format_: str = _FORMAT,
    json_flag: bool = _JSON,
    verbose: int = _VERBOSE,
    color: str = _COLOR,
    yes: bool = _YES,
    non_interactive: bool = _NON_INTERACTIVE,
) -> None:
```

The function body below this line (the `load_config`/`build_settings`/`configure_logging`/`ctx.obj`
assignment) is untouched - only where each parameter's default comes from changes.

Replace the registration line at the bottom of the file:

```python
# before
basics.register(app)
```

```python
# after
basics.register(app)
schema.register(app)
```

- [ ] **Step 12: Run the tests to see most of the remaining ones pass**

```bash
uv run pytest tests/cli/test_schema.py
```

Expected: every test passes except `test_command_help_text_matches_the_metadata_row[status]`/
`[version]`, `test_option_names_match_between_typer_and_metadata[status]`/`[version]`, and the two
`--format`-after-`status` invocation tests - all five still red for the same reason: `basics.py` has
not been touched yet, so `status`/`version` carry no `help=`, declare none of the eight per-command
global options, and do not accept `--format` at the command level. Everything else Step 11 alone
already fixes: `schema` is now registered with the right option names (its own metadata options are
empty, so the expected set is exactly the eight globals), the right help text and epilog (Step 7
already built both from `command_meta("schema")`), and `--color`'s `NO_COLOR` row no longer reaches
Click as an `envvar` (Step 3's fix) - `test_a_command_still_runs_with_no_color_set_in_the_environment`
passes too.

- [ ] **Step 13: Modify `src/railctl/cli/commands/basics.py` - source `help=`/`epilog=` from the
  table, add the eight per-command global options**

Replace the import block:

```python
# before
from __future__ import annotations

from typing import Final

import typer

from railctl import __version__
from railctl.cli._errors import run
from railctl.cli.config import capabilities_path
from railctl.cli.deps import link_info, open_station, station_info
from railctl.cli.result import CommandResult, StationInfo
from railctl.xbus.replies import StationStatus, StationVersion
```

```python
# after
from __future__ import annotations

import os
from typing import Final

import typer

from railctl import __version__
from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import command_meta, global_option, help_epilog
from railctl.cli.config import capabilities_path
from railctl.cli.deps import Settings, link_info, merge_settings, open_station, station_info
from railctl.cli.render import want_color
from railctl.cli.result import CommandResult, StationInfo
from railctl.xbus.replies import StationStatus, StationVersion

# Built once, at import time - see the same B008 note in main.py. Every
# registered command builds this identical eight-tuple; see the plan's note
# on `global_option` for why the duplication across command modules is the
# accepted shape rather than a shared constant imported from `_meta.py`.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")


def _merged_output(ctx: typer.Context, *, target, address, format_, json_flag, verbose,
                    color, yes, non_interactive) -> tuple[Settings, OutputContext]:
    """Shared by both commands below: merge the per-command global options
    onto `ctx.obj.settings` and rebuild the `OutputContext` from the result.
    A free function here, not a method on anything, because both bodies need
    exactly the same eight-keyword call and nothing about it is command
    specific. Returns the merged `Settings` (for `open_station`/`link_info`)
    alongside the rebuilt `OutputContext` (for `run()`) - never `ctx` itself,
    which the caller already has."""
    cli_ctx = ctx.obj
    settings = merge_settings(
        cli_ctx.settings,
        target=target, address=address, fmt=format_, json_flag=json_flag,
        verbose=verbose, color=color, yes=yes, non_interactive=non_interactive,
    )
    output = OutputContext(
        fmt=settings.fmt,
        color=want_color(settings.color, cli_ctx.output.stdout, os.environ),
        stdout=cli_ctx.output.stdout,
        stderr=cli_ctx.output.stderr,
    )
    return settings, output
```

Replace the two commands:

```python
# before
def register(app: typer.Typer) -> None:
    @app.command("version")
    def version_command(ctx: typer.Context) -> None:
        cli_ctx = ctx.obj
        settings = cli_ctx.settings

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                version = station.version()
                outcome = build_version(version, tool_version=__version__)
                outcome.link = link_info(station, settings)
                outcome.station = StationInfo(
                    protocol="xpressnet",
                    protocol_version=version.version,
                    command_station_id=version.station_id,
                )
            finally:
                station.close()
            return outcome

        run("version", cli_ctx.output, work)

    @app.command("status")
    def status_command(ctx: typer.Context) -> None:
        cli_ctx = ctx.obj
        settings = cli_ctx.settings

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                outcome = build_status(station.status())
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            finally:
                station.close()
            return outcome

        run("status", cli_ctx.output, work)
```

```python
# after
def register(app: typer.Typer) -> None:
    @app.command(
        "version",
        help=command_meta("version").help,
        epilog=help_epilog(command_meta("version")),
    )
    def version_command(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        settings, output = _merged_output(
            ctx, target=target, address=address, format_=format_, json_flag=json_flag,
            verbose=verbose, color=color, yes=yes, non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                version = station.version()
                # Built inline from the SAME StationVersion already fetched
                # above, not via station_info(station) - that helper exists
                # for commands (like `status`, below) that have no
                # StationVersion of their own.
                outcome = build_version(version, tool_version=__version__)
                outcome.link = link_info(station, settings)
                outcome.station = StationInfo(
                    protocol="xpressnet",
                    protocol_version=version.version,
                    command_station_id=version.station_id,
                )
            finally:
                station.close()
            return outcome

        run("version", output, work)

    @app.command(
        "status",
        help=command_meta("status").help,
        epilog=help_epilog(command_meta("status")),
    )
    def status_command(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        settings, output = _merged_output(
            ctx, target=target, address=address, format_=format_, json_flag=json_flag,
            verbose=verbose, color=color, yes=yes, non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                outcome = build_status(station.status())
                outcome.link = link_info(station, settings)
                outcome.station = station_info(station)
            finally:
                station.close()
            return outcome

        run("status", output, work)
```

- [ ] **Step 14: Run `tests/cli/test_schema.py` again**

```bash
uv run pytest tests/cli/test_schema.py
```

Expected: `39 passed` (14 from Step 4, 2 from Step 8, 23 from Step 9's block - the original 21 minus
the two deleted fake drift tests, plus the canary, the `NO_COLOR` test and the two invocation-order
tests: 21 - 2 + 1 + 1 + 2 = 23).

- [ ] **Step 15: Run the whole `tests/cli` directory to confirm no regression in Tasks 8-9's own suites**

```bash
uv run pytest tests/cli
```

Expected: `0 failed`. `tests/cli/test_wiring.py`'s `version`/`status` CliRunner tests still pass
unchanged - every invocation there passes no per-command global option at all, so `merge_settings`
resolves to exactly `cli_ctx.settings` and `_merged_output`'s rebuilt `OutputContext` carries the
same `fmt`/`color` the root callback already picked. The `--format`/`--color` enum guard added in
`_meta.py` only rejects a value nobody in Task 8's or Task 9's own tests ever passes.

- [ ] **Step 16: Run the full suite**

```bash
uv run pytest
```

Expected: `0 failed`. The total is whatever Tasks 1-9 left the suite at, plus the 39 tests this task
adds - if the arithmetic printed does not match that estimate exactly, treat it as this plan's own
small miscount, not a signal; a *different* test failing is the real signal.

- [ ] **Step 17: Check the coverage gate**

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table now includes `src/railctl/cli/_meta.py` and
`src/railctl/cli/commands/schema.py`, then `Required test coverage of 90% reached.` and `0 failed`.
Every branch this task adds has a direct test above: the enum guard's `is not None` branch (Step 1),
`command_meta`'s hit and miss branches (Step 1), `manifest`'s tree-shape and single-command-shape
branches (Step 1), `typer_argument`'s required/optional branches (Step 1),
`typer_option`'s `RAILCTL_*`/non-`RAILCTL_*` `envvar` branches (Step 9's `NO_COLOR` test plus the
existing env-var test), `global_option`'s three `_bare_default` branches (Step 1), and
`help_epilog`'s base-code/exception-docstring branches (Step 1, against `command_meta("status")`,
whose `exit_codes` include both kinds of code). If `term-missing` shows an uncovered line in
`_meta.py` or `commands/schema.py`, it is a gap in this task's own tests to close, not a
pre-existing shortfall to defer.

- [ ] **Step 18: Lint and format**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both report no issues. If Ruff flags a line in `main.py`, `commands/schema.py` or
`commands/basics.py` as B008, the parameter default is still a direct function call somewhere -
check that each file's module-level tuple/name assignment landed, not an inline
`typer_option(...)`/`global_option(...)` call in the signature itself.

- [ ] **Step 19: Commit**

```bash
git add src/railctl/cli/_meta.py src/railctl/cli/commands/schema.py tests/cli/test_schema.py \
        src/railctl/cli/main.py src/railctl/cli/commands/basics.py
git commit -m "feat(cli): add the command metadata table and railctl schema"
```

---

### Task 11: The throttle commands: power, stop, drive, function - and their safety pre-flights

**Files:**
- Create: `src/railctl/cli/commands/power.py`
- Create: `src/railctl/cli/commands/throttle.py`
- Create: `tests/cli/test_throttle.py`
- Modify: `src/railctl/cli/_meta.py` (append rows for `power`, `stop`, `drive`, `function`; rebuild `COMMANDS`)
- Modify: `src/railctl/cli/main.py` (register the two new command modules)
- Modify: `src/railctl/errors.py` (one new exception class, `FunctionGroupUnreadableError` - see
  Step 3)
- Modify: `src/railctl/cli/_errors.py` (one `isinstance` branch in `default_suggestions` - a small,
  deliberate addition to a file outside this task's original file list, not a rewrite; Task 12 later
  adds a branch of its own to this same function for its own domain need, so this is not the only
  task that touches it)

**Interfaces:**

Consumes - Task 8 (`railctl.cli.result` / `railctl.cli._errors` / `railctl.cli.render`), exact
shapes this task relies on, quoted from the modules those names actually live in - there is no
`railctl.cli.output` module anywhere in this plan:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TextIO

@dataclass(frozen=True, slots=True)
class ResultWarning:
    name: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

@dataclass                                    # MUTABLE, no slots - a command body
class CommandResult:                          # keeps calling .say()/.warn() as it runs
    schema: str
    command: str
    ok: bool = True
    exit_code: int = 0
    elapsed_ms: int = 0
    link: "LinkInfo | None" = None
    station: "StationInfo | None" = None
    warnings: list[ResultWarning] = field(default_factory=list)
    result: dict[str, object] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)   # the human text - there is no `human` field
    def warn(self, name: str, message: str, **details: object) -> None: ...
    def say(self, line: str) -> None: ...
    def envelope(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class OutputContext:
    fmt: Literal["human", "json", "ndjson"]
    color: bool
    stdout: TextIO
    stderr: TextIO

def run(
    command: str, ctx: OutputContext, work: "Callable[[], CommandResult]"
) -> "NoReturn":
    """Call `work()`. On a normal return, render the envelope to `ctx.stdout`
    and raise `typer.Exit(result.exit_code)`. On `KeyboardInterrupt`, a bare
    `ValueError` (exit 2, a usage problem), a `RailctlError` (via
    `report_for`), or anything else (`"internal"`, exit 1), render the error
    envelope to `ctx.stderr` and raise `typer.Exit(report.exit_code)`.

    `run` takes exactly these three positional/keyword-only arguments - there
    is no `suggest=` callback. A command that needs to attach an argv the
    generic exception-type table in `default_suggestions` could not invent on
    its own (this task's `function` does) has to get that argv onto the
    exception itself before raising it, which is exactly what Step 3 below
    adds `FunctionGroupUnreadableError.retry_argv` for.
    """

def render(result: CommandResult, *, fmt: Literal["human", "json", "ndjson"],
          stdout: TextIO, color: bool) -> None: ...

def want_color(choice: str, stream: TextIO, env: "Mapping[str, str]") -> bool: ...

def tri_state(value: bool | None) -> Literal["yes", "no", "unknown"]:
    """`True` -> `"yes"`, `False` -> `"no"`, `None` -> `"unknown"`. Human text
    only - the JSON payload keeps the bool/None as-is, which is what keeps
    the three outcomes distinguishable in both renderings from one value."""
```

`report_for`/`default_suggestions` (also Task 8, `railctl.cli._errors`) are not called directly by
this task's own code - `run` calls them internally - but Step 3 extends `default_suggestions` with
one more branch, so their shape matters here too:

```python
def report_for(exc: BaseException, *, command: str,
               details: dict[str, object] | None = None,
               suggestions: list[list[str]] | None = None) -> "ErrorReport": ...
def default_suggestions(exc: BaseException, *, command: str,
                        address: int | None = None, cv: int | None = None) -> list[list[str]]: ...
```

Consumes - Task 9 (`railctl.cli.deps` and `railctl.cli.config` - there is no `railctl.cli.settings`
and no `railctl.cli._deps` anywhere in this plan):

```python
from __future__ import annotations

from pathlib import Path
from typing import Literal, TextIO

from railctl.station import Station

@dataclass(frozen=True, slots=True)
class Settings:
    target: str
    address: int | None
    fmt: Literal["human", "json", "ndjson"]
    verbose: int
    color: Literal["auto", "always", "never"]
    assume_yes: bool
    interactive: bool

def open_station(settings: Settings, *, capabilities_path: Path | None) -> Station:
    """A PLAIN function, not a context manager. Every command below opens one
    inside a `try`/`finally: station.close()` itself - there is no
    `with open_station(...) as station:` form."""

def link_info(station: Station, settings: Settings) -> "LinkInfo": ...
def station_info(station: Station) -> "StationInfo": ...

def require_address(settings: Settings, *, argv_hint: list[str]) -> int:
    """`settings.address` if set, else raise a bare `ValueError` naming
    `argv_hint + ["--address", "<n>"]` as the fix - never `argv_prefix`,
    never a prompt. Address is resolved from the CLI flag, `RAILCTL_ADDRESS`,
    or the config file, in that order, by the time `Settings` exists."""

def confirm(question: str, *, settings: Settings, stdin: TextIO, stderr: TextIO) -> None: ...
    # Never called anywhere in this task - see must-pin 5.

def merge_settings(base: Settings, *, target: str | None = None, address: int | None = None,
                   fmt: str | None = None, json_flag: bool = False, verbose: int = 0,
                   color: str | None = None, yes: bool = False,
                   non_interactive: bool = False) -> Settings:
    """Layers one command's OWN copy of the global options over `base`.
    Every parameter defaults to the sentinel for "not typed at the command
    level" (`None`/`False`/`0`), so a command that redeclares the eight
    global options (this task, worked around Click's group-options-before-
    subcommand parsing - see the note above `register` in each module below)
    can hand every one of them straight through and get `base` unchanged
    back when none of them were actually given on this invocation."""

def capabilities_path(env: "Mapping[str, str] | None" = None) -> Path: ...
    # Lives in railctl.cli.config, not railctl.cli.deps - imported from there.
```

Consumes - Task 10 (`railctl.cli._meta`):

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OptionType = Literal["string", "integer", "boolean", "enum"]

@dataclass(frozen=True, slots=True)
class Option:
    name: str
    help: str
    type: OptionType = "string"
    short: str | None = None
    default: object = None
    enum: tuple[str, ...] | None = None
    required: bool = False
    env: str | None = None
    repeatable: bool = False

@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    help: str
    type: OptionType = "string"
    required: bool = True
    enum: tuple[str, ...] | None = None
    # NO `default` field - `typer_argument` below builds Typer's own default
    # (`...` when required, `None` otherwise) from `required` alone.

@dataclass(frozen=True, slots=True)
class CommandMeta:
    path: str                        # a STRING ("drive", "function") - never a tuple
    help: str
    schema: str                      # REQUIRED on every row, or `manifest()` breaks
    mutates: bool
    exit_codes: tuple[int, ...]
    arguments: tuple[Argument, ...] = ()
    options: tuple[Option, ...] = ()
    confirms: bool = False

COMMANDS: "Final[tuple[CommandMeta, ...]]"    # a TUPLE - each task rebuilds the literal,
                                              # it is never mutated in place

def command_meta(path: str) -> CommandMeta:
    """A LOOKUP by path - `ValueError` naming near misses when `path` is not
    registered. This task never calls it to append a row; `_DRIVE`, `_STOP`
    etc. below are assigned directly and folded into the `COMMANDS` tuple by
    hand, exactly as Task 10's own `_STATUS`/`_VERSION`/`_SCHEMA` rows are."""

def typer_option(option: Option) -> "Any": ...
def typer_argument(argument: Argument) -> "Any": ...

def global_option(name: str) -> "Any":
    """A per-command copy of the `GLOBAL_OPTIONS` row named `name`
    (`"--target"`, `"--address"`, `"--format"`, `"--json"`, `"--verbose"`,
    `"--color"`, `"--yes"`, or `"--non-interactive"`), built with a
    `None`/`False`/`0` sentinel default and NO envvar - the root callback in
    `main.py` already resolved the environment once for the group-level
    flags, and re-reading `RAILCTL_ADDRESS` a second time at the command
    level would just double-apply the same environment variable. Every
    command in both modules below calls this eight times (`stop` calls it
    seven - see the note above `register` in `power.py`) so that Click can
    parse `--address`/`--format`/etc. AFTER the subcommand name, matching
    every worked example in the design spec (`railctl drive 30 --address 3`,
    spec line 1386) - Click parses a `@app.callback()` group's own options
    only BEFORE the subcommand name, never after it, and no
    `context_settings` flag changes that."""
```

Consumes - Tasks 2, 3 (`railctl.station.Station`, already fixed, quoted verbatim from
this task's contract - do not add, drop or retype a parameter):

```python
class Station:
    def power_on(self) -> None: ...
    def power_off(self) -> None: ...
    def emergency_stop(self, address: int | None = None) -> None: ...
    def drive(self, address: int, speed: int, direction: Direction) -> None: ...
    def loco_info(self, address: int) -> LocoInfo: ...
    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None: ...
    def function_toggle(
        self, address: int, function: int, *, force_group: bool = False
    ) -> bool: ...
    def function_state(
        self, address: int, *, refresh: bool = False
    ) -> dict[int, bool]: ...
    def status(self) -> StationStatus: ...
```

Consumes - already on disk, quoted from the real files:

- `railctl.xbus.speed.Direction` (`REVERSE = 0`, `FORWARD = 1`), `MAX_SPEED_STEP = 126`.
- `railctl.xbus.replies.LocoInfo` - `@dataclass(frozen=True, slots=True)`: `raw_ident`, `raw_speed`,
  `speed_steps`, `in_use_by_other`, `function_bits` are required; `speed: int | None = None`,
  `direction: Direction | None = None`, `emergency_stopped: bool | None = None`,
  `address: int | None = None` all default. `speed`/`direction` are `None` together whenever
  `speed_steps != 128` (`_loco_info` in `replies.py` never sets one without the other). **`slots=True`
  means `LocoInfo` has no `__dict__`** - build a modified copy with `dataclasses.replace`, never
  `LocoInfo(**{**LOCO_128.__dict__, ...})` (that raises `AttributeError`).
- `railctl.xbus.replies.StationStatus` - `raw`, `emergency_off`, `emergency_stop`,
  `auto_start_mode`, `service_mode`, `powering_up`, `ram_error`, property `track_power`
  (`not emergency_off`), classmethod `from_raw(raw: int) -> StationStatus`. **Bit 2 is
  `auto_start_mode`** (`STATUS_AUTO_START = 0x04`); this task never re-derives that name or that bit
  position, it imports `status.auto_start_mode` and prints it, so a later fix to the bit mapping in
  `replies.py` cannot leave `build_power`'s wording out of sync with `build_status`'s (a different
  task, not this one, but they read the same field).
- `railctl.errors.RailctlError(message: str, *, hint: str | None = None)`,
  `TrackPowerError(StationError)` (exit 20), `StationBusyError(ProgrammingError)` (exit 12),
  `StationError(RailctlError)` (exit 9, no row of its own in `EXIT_CODES`),
  `UnsupportedFeatureError(RailctlError)` (exit 7).

Produces (later tasks depend on these EXACT signatures):

`src/railctl/cli/commands/power.py`:
```python
POWER_SCHEMA: Final[str] = "railctl/power/v1"
STOP_SCHEMA: Final[str] = "railctl/stop/v1"

def build_power(state: str, status: StationStatus, *, changed: bool,
                idled_address: int | None) -> CommandResult: ...
def build_stop(address: int | None) -> CommandResult: ...
def register(app: typer.Typer) -> None: ...
```

`src/railctl/cli/commands/throttle.py`:
```python
DRIVE_SCHEMA: Final[str] = "railctl/drive/v1"
FUNCTION_SCHEMA: Final[str] = "railctl/function/v1"
FUNCTION_ALIASES: Final[dict[str, int]] = {"light": 0, "lights": 0, "headlight": 0}
RUNNING_NOTICE: Final[str] = "loco {address} is running at step {speed} {direction}; it keeps running after this command exits"

def parse_function(token: str) -> int: ...         # "f2" | "2" | "light" -> 0..28
def parse_state(token: str | None) -> Literal["on", "off", "toggle"]: ...
def build_drive(address: int, speed: int, direction: Direction,
                *, was: LocoInfo | None) -> CommandResult: ...
def build_function(address: int, function: int, state: str, *, now_on: bool) -> CommandResult: ...
def preflight(station: Station, *, speed: int | None) -> StationStatus: ...
def register(app: typer.Typer) -> None: ...
```

`src/railctl/errors.py` gains one class this task owns:
```python
class FunctionGroupUnreadableError(StationError):
    def __init__(self, message: str, *, hint: str | None = None,
                retry_argv: list[str]) -> None: ...
    retry_argv: list[str]
```

**Layering notes for this task specifically**

- No opcode, framing byte or CV arithmetic appears anywhere below: every mutation goes through a
  `Station` facade method, and `Direction` / function numbers are the only wire-adjacent vocabulary
  either module names, which the design explicitly allows (`tests/test_layering.py` rule 1 only
  forbids raw bytes and port words, not the `Direction` enum or an integer function number).
- `station.status()` is read, never a raw status byte compared against a literal mask - `bit 2` is
  named once, in `replies.py`, as `auto_start_mode`, and this file imports that name.
- `sys.stdout.isatty()` / `stream.isatty()` is fine to write in these files if a later task adds
  colour; the word "TTY" itself is not, even in a comment - write "terminal".

**Decisions already made here - do not re-open:**

- `stop`'s `--address` is a **command-local** option, independent of the global `--address` /
  `RAILCTL_ADDRESS` / config default that `drive` and `function` read through `settings.address`.
  If `stop` fell back to a configured default address the way `drive` does, a user with
  `address = 3` in their config who types the panic button `railctl stop` meaning "stop
  everything" would instead stop only loco 3 - the one case where inheriting the convenient
  default is the dangerous choice. `stop` therefore ignores `settings.address` entirely and only
  ever narrows to one locomotive when `--address` is typed on the `stop` invocation itself. That is
  also why `stop_cmd` (in `power.py`) is the one command in this file that redeclares only SEVEN of
  the eight global options: its own `STOP_ADDRESS_OPT` already claims the `--address`/`-a` flag
  names for that command-scoped meaning, and a second `global_option("--address")` under the same
  flag names would either collide in Click or quietly reintroduce the exact fallback this note
  argues against.
- `power on` always executes its full sequence (stop-all, power-on, status, idle) regardless of
  the track's prior state, so it always reports `changed=True`. Checking "was it already on" first
  would require a status read BEFORE the stop-all, which is the one call this task's own test
  proves is first; `power off` is the one of the two that checks first, because skipping its only
  mutation when nothing needs doing is exactly what `changed: false` is for.
- `--for SECONDS` is not implemented here. It needs a timed-revert path that survives Ctrl-C and
  belongs with the operation-resource work of a later plan.
- Every command in both modules redeclares all eight global options in its own signature (`stop`
  redeclares seven, see above), using `global_option(name)` and `merge_settings`. This is not
  optional decoration: Click parses a Typer group's own `@app.callback()` options only BEFORE the
  subcommand name, and this design's own worked session types them after it
  (`railctl drive 30 --address 3`). Without the per-command redeclaration, that exact invocation
  exits 2 with "no such option" before it ever reaches `drive`'s body.
- `function`'s failure to read a group's current state is surfaced as
  `errors.FunctionGroupUnreadableError`, a new leaf under `StationError` this task adds, carrying
  the exact retry command (`retry_argv`) the operator should type with `--force-group` appended.
  `default_suggestions` (Task 8's `_errors.py`) is keyed only by exception type plus an optional
  `address`/`cv`, so it has no way to reconstruct the function token (`"f2"`) or state token
  (`"on"`) the operator actually typed; carrying the finished argv on the exception itself, and
  reading it back with one `isinstance` branch, is the smallest fix that does not require
  `default_suggestions` to somehow guess at command-specific argv shapes.

- [ ] **Step 1: Write every failing test in `tests/cli/test_throttle.py`**

This is one large file covering all four commands, because the plan's own file list has exactly
one test file for both `power.py` and `throttle.py`. It imports both modules, which do not exist
yet, so the whole file fails to collect - that is the expected red.

```python
# tests/cli/test_throttle.py
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from railctl.cli import deps
from railctl.cli._errors import OutputContext
from railctl.cli.commands import power, throttle
from railctl.cli.deps import Settings
from railctl.errors import StationBusyError, StationError, TrackPowerError
from railctl.xbus.replies import LocoInfo, StationStatus
from railctl.xbus.speed import Direction

runner = CliRunner()
# No mix_stderr=False: the pinned Typer/Click raises TypeError for that
# argument. Tasks 9, 10 and 12 already run CliRunner() plain; this is the
# fourth file to do the same, not the first.

# Raw status bytes are fine in a test file - test_layering.py only scans
# src/railctl, not tests/. STATUS_EMERGENCY_OFF=0x01, STATUS_EMERGENCY_STOP=0x02,
# STATUS_SERVICE_MODE=0x08, STATUS_AUTO_START=0x04 (railctl/xbus/replies.py).
CLEAR_STATUS = StationStatus.from_raw(0x00)
AUTO_START_STATUS = StationStatus.from_raw(0x04)
EMERGENCY_OFF_STATUS = StationStatus.from_raw(0x01)
EMERGENCY_STOP_STATUS = StationStatus.from_raw(0x02)
SERVICE_MODE_STATUS = StationStatus.from_raw(0x08)

LOCO_128 = LocoInfo(
    raw_ident=0b10000100,
    raw_speed=0x8F,
    speed_steps=128,
    in_use_by_other=False,
    function_bits=(False,) * 13,
    speed=14,
    direction=Direction.FORWARD,
    emergency_stopped=False,
)
LOCO_14_STEP = LocoInfo(
    raw_ident=0b00000000,
    raw_speed=0x07,
    speed_steps=14,
    in_use_by_other=False,
    function_bits=(False,) * 13,
)


class FakeStation:
    """A stand-in for `railctl.station.Station`. Records every call so a test
    can assert both the return value and the ORDER calls happened in - the
    power-on test below depends on order, not just on which methods ran."""

    def __init__(
        self,
        *,
        status: StationStatus = CLEAR_STATUS,
        loco_info: LocoInfo | None = LOCO_128,
        function_toggle_result: bool = True,
        function_raises: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._status = status
        self._loco_info = loco_info
        self._function_toggle_result = function_toggle_result
        self._function_raises = function_raises

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    @property
    def call_names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]

    def status(self) -> StationStatus:
        self._record("status")
        return self._status

    def power_on(self) -> None:
        self._record("power_on")

    def power_off(self) -> None:
        self._record("power_off")

    def emergency_stop(self, address: int | None = None) -> None:
        self._record("emergency_stop", address=address)

    def drive(self, address: int, speed: int, direction: Direction) -> None:
        self._record("drive", address, speed, direction)

    def loco_info(self, address: int) -> LocoInfo:
        self._record("loco_info", address)
        if self._loco_info is None:
            raise StationError(f"no loco info available for {address}")
        return self._loco_info

    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None:
        self._record("function_set", address, function, on, force_group=force_group)
        if self._function_raises and not force_group:
            raise StationError("could not read the current function group")

    def function_toggle(
        self, address: int, function: int, *, force_group: bool = False
    ) -> bool:
        self._record("function_toggle", address, function, force_group=force_group)
        if self._function_raises and not force_group:
            raise StationError("could not read the current function group")
        return self._function_toggle_result

    def close(self) -> None:
        self._record("close")


def _settings(*, address: int | None = 3, fmt: str = "human", yes: bool = False) -> Settings:
    return Settings(
        target="fake:test", address=address, fmt=fmt, verbose=0,
        color="auto", assume_yes=yes, interactive=True,
    )


@dataclass(frozen=True, slots=True)
class _FakeCliContext:
    """Stands in for `railctl.cli.main.CliContext` - importing the real class
    would import `railctl.cli.main`, which imports `power` and `throttle`
    back to call `register(app)`, and this test module importing its own
    subject modules' importer is the same cycle `context_for`'s own note (in
    `power.py`/`throttle.py`) already rules out."""

    settings: Settings
    output: OutputContext


def _app(
    station: FakeStation, monkeypatch: pytest.MonkeyPatch, *,
    address: int | None = 3, fmt: str = "human",
) -> typer.Typer:
    def _open(_settings: Settings, *, capabilities_path: Any = None) -> FakeStation:
        return station

    monkeypatch.setattr(power, "open_station", _open)
    monkeypatch.setattr(throttle, "open_station", _open)
    monkeypatch.setattr(power, "capabilities_path", lambda: None)
    monkeypatch.setattr(throttle, "capabilities_path", lambda: None)

    app = typer.Typer()
    settings = _settings(address=address, fmt=fmt)

    @app.callback()
    def _root(ctx: typer.Context) -> None:
        # sys.stdout/sys.stderr are read HERE, inside the callback body, not
        # captured in the enclosing _app() scope above - CliRunner.invoke()
        # swaps them in only once app() actually dispatches, which happens
        # after _app() has already returned. A reference captured earlier
        # would point at the real, un-swapped streams and every stderr/stdout
        # assertion below would silently see an empty buffer for the wrong
        # reason - exactly the trap the real main.py's own callback avoids by
        # calling context_for(settings, stdout=sys.stdout, stderr=sys.stderr)
        # from inside itself.
        ctx.obj = _FakeCliContext(
            settings=settings,
            output=OutputContext(fmt=settings.fmt, color=False, stdout=sys.stdout, stderr=sys.stderr),
        )

    power.register(app)
    throttle.register(app)
    return app


# --- preflight: three refusal conditions, one pass-through ---

def test_preflight_returns_status_when_track_is_clear():
    station = FakeStation(status=CLEAR_STATUS)
    assert throttle.preflight(station, speed=30) is CLEAR_STATUS


def test_preflight_refuses_when_emergency_off():
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    with pytest.raises(TrackPowerError):
        throttle.preflight(station, speed=30)


def test_preflight_refuses_when_emergency_stop():
    station = FakeStation(status=EMERGENCY_STOP_STATUS)
    with pytest.raises(TrackPowerError):
        throttle.preflight(station, speed=30)


def test_preflight_refuses_when_service_mode_active():
    station = FakeStation(status=SERVICE_MODE_STATUS)
    with pytest.raises(StationBusyError):
        throttle.preflight(station, speed=None)


# --- parse_function / parse_state ---

def test_parse_function_accepts_f_prefixed_bare_number_and_light_alias():
    assert throttle.parse_function("f2") == 2
    assert throttle.parse_function("2") == 2
    assert throttle.parse_function("light") == 0


def test_parse_function_rejects_out_of_range_and_non_numeric():
    for token in ("29", "f29", "xyz"):
        with pytest.raises(ValueError, match="0..28"):
            throttle.parse_function(token)


def test_parse_state_defaults_to_on_when_omitted():
    assert throttle.parse_state(None) == "on"


def test_parse_state_accepts_and_rejects():
    assert throttle.parse_state("off") == "off"
    assert throttle.parse_state("toggle") == "toggle"
    with pytest.raises(ValueError):
        throttle.parse_state("sideways")


# --- build_drive ---

def test_build_drive_direction_is_a_word_never_a_wire_value():
    result = throttle.build_drive(3, 30, Direction.FORWARD, was=LOCO_128)
    assert result.result["direction"] == "forward"
    result = throttle.build_drive(3, 20, Direction.REVERSE, was=LOCO_128)
    assert result.result["direction"] == "reverse"


def test_build_drive_changed_true_when_speed_or_direction_differs():
    was = replace(LOCO_128, speed=10, direction=Direction.FORWARD)
    result = throttle.build_drive(3, 30, Direction.FORWARD, was=was)
    assert result.result["changed"] is True


def test_build_drive_changed_false_when_nothing_differs():
    was = replace(LOCO_128, speed=30, direction=Direction.FORWARD)
    result = throttle.build_drive(3, 30, Direction.FORWARD, was=was)
    assert result.result["changed"] is False


def test_build_drive_changed_unknown_when_prior_state_unavailable():
    result = throttle.build_drive(3, 30, Direction.FORWARD, was=None)
    assert result.result["changed"] is None
    assert result.result["previous_speed_decoded"] is None


def test_build_drive_changed_unknown_when_prior_step_mode_not_decoded():
    """LOCO_14_STEP.speed is None because speed.py only defines the 128-step
    layout - replies.py leaves it undecoded rather than guess. Reporting
    `changed` here would mean comparing a number against a layout railctl
    never decoded, which is exactly the "recorded absent by a defective
    instrument" failure this project exists to avoid."""
    result = throttle.build_drive(3, 30, Direction.FORWARD, was=LOCO_14_STEP)
    assert result.result["changed"] is None
    assert result.result["previous_speed_decoded"] is False
    assert any("not decoded" in line for line in result.lines)


# --- build_function ---

def test_build_function_reports_requested_state_and_resulting_bit():
    result = throttle.build_function(3, 2, "on", now_on=True)
    assert result.result == {"address": 3, "function": 2, "requested": "on", "now_on": True}
    assert result.schema == throttle.FUNCTION_SCHEMA
    assert result.command == "function"


# --- drive: direction default, preflight wiring, notice, global options ---

def test_drive_keeps_current_direction_when_reverse_not_given(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, direction=Direction.REVERSE))
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    assert station.calls[-1] == ("drive", (3, 30, Direction.REVERSE), {})


def test_drive_reverse_flag_overrides_current_direction(monkeypatch):
    station = FakeStation(loco_info=LOCO_128)  # currently forward
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "20", "--reverse"])
    assert result.exit_code == 0
    assert station.calls[-1] == ("drive", (3, 20, Direction.REVERSE), {})


def test_drive_positive_speed_refuses_on_emergency_off(monkeypatch):
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 20
    assert "drive" not in station.call_names


def test_drive_positive_speed_refuses_on_service_mode(monkeypatch):
    station = FakeStation(status=SERVICE_MODE_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 12
    assert "drive" not in station.call_names


def test_drive_zero_skips_preflight_and_is_always_sent(monkeypatch):
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["drive", "0"])
    assert result.exit_code == 0
    assert "status" not in station.call_names
    assert station.calls[-1] == ("drive", (3, 0, Direction.FORWARD), {})


def test_drive_prints_running_notice_on_stderr_only_for_nonzero_speed(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    moving = runner.invoke(app, ["drive", "30"])
    assert moving.exit_code == 0
    assert "loco 3 is running at step 30 forward" in moving.stderr
    assert "loco 3 is running" not in moving.output

    stopped = runner.invoke(app, ["drive", "0"])
    assert stopped.exit_code == 0
    assert "is running" not in stopped.stderr


def test_drive_prints_running_notice_on_stderr_in_json_mode(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    assert result.exit_code == 0
    assert "loco 3 is running at step 30 forward" in result.stderr
    payload = json.loads(result.output)
    assert "is running" not in json.dumps(payload)


def test_drive_accepts_the_global_address_option_after_the_subcommand(monkeypatch):
    """The global-option-position pin (spec's own worked example, spec line
    1386): Click only parses a Typer group's callback options BEFORE the
    subcommand name, so this only works because drive_cmd redeclares
    --address itself via global_option("--address") and merges it with
    merge_settings - without that, this invocation would exit 2."""
    station = FakeStation()
    app = _app(station, monkeypatch, address=None)  # no address configured globally
    result = runner.invoke(app, ["drive", "30", "--address", "3"])
    assert result.exit_code == 0
    assert station.calls[-1] == ("drive", (3, 30, Direction.FORWARD), {})


def test_drive_format_option_after_the_subcommand_overrides_the_configured_default(monkeypatch):
    """Exercises the OTHER half of the same mechanism: --format typed on
    drive itself must rebuild the OutputContext used for THIS invocation
    rather than the one main.py's callback built from the group-level
    default, or drive --format json here would print human text."""
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="human")
    result = runner.invoke(app, ["drive", "0", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["result"]["speed"] == 0


# --- function: facade choice, preflight, force-group suggestion ---

def test_function_toggle_uses_function_toggle_facade(monkeypatch):
    station = FakeStation(function_toggle_result=True)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "toggle"])
    assert result.exit_code == 0
    assert ("function_toggle", (3, 2), {"force_group": False}) in station.calls
    assert not any(name == "function_set" for name in station.call_names)


def test_function_on_off_uses_function_set_facade(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "light", "off"])
    assert result.exit_code == 0
    assert ("function_set", (3, 0, False), {"force_group": False}) in station.calls


def test_function_refuses_on_service_mode(monkeypatch):
    station = FakeStation(status=SERVICE_MODE_STATUS)
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 12
    assert not any(name in ("function_set", "function_toggle") for name in station.call_names)


def test_function_suggests_force_group_when_state_cannot_be_read(monkeypatch):
    station = FakeStation(function_raises=True)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "on"])
    assert result.exit_code == 9
    error = json.loads(result.stderr)
    assert error["suggestions"][0] == [
        "railctl", "function", "f2", "on", "--address", "3", "--force-group",
    ]


# --- human/json parity, one per command ---

def test_drive_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(loco_info=replace(LOCO_128, speed=10, direction=Direction.FORWARD))
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["drive", "30"])
    payload = json.loads(result.output)
    assert payload["result"]["address"] == 3
    assert payload["result"]["speed"] == 30
    assert payload["result"]["direction"] == "forward"
    assert payload["result"]["changed"] is True

    station_human = FakeStation(loco_info=replace(LOCO_128, speed=10, direction=Direction.FORWARD))
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["drive", "30"])
    joined = "\n".join([human.output])
    assert "3" in joined and "30" in joined and "forward" in joined


def test_function_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(function_toggle_result=True)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["function", "f2", "toggle"])
    payload = json.loads(result.output)
    assert payload["result"] == {"address": 3, "function": 2, "requested": "toggle", "now_on": True}

    station_human = FakeStation(function_toggle_result=True)
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["function", "f2", "toggle"])
    assert "3" in human.output and "F2" in human.output and "on" in human.output


# --- power ---

def test_power_on_runs_stop_all_then_power_on_then_status_then_idles_address(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.call_names == ["emergency_stop", "power_on", "status", "drive", "close"]
    assert station.calls[0] == ("emergency_stop", (), {"address": None})
    assert station.calls[-2] == ("drive", (3, 0, Direction.FORWARD), {})


def test_power_on_does_not_idle_when_no_address_is_configured(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=None)
    result = runner.invoke(app, ["power", "on"])
    assert result.exit_code == 0
    assert station.call_names == ["emergency_stop", "power_on", "status", "close"]


def test_power_off_reports_changed_false_when_already_off(monkeypatch):
    station = FakeStation(status=EMERGENCY_OFF_STATUS)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["power", "off"])
    payload = json.loads(result.output)
    assert payload["result"]["changed"] is False
    assert "power_off" not in station.call_names


def test_power_off_reports_changed_true_when_it_was_on(monkeypatch):
    station = FakeStation(status=CLEAR_STATUS)
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["power", "off"])
    payload = json.loads(result.output)
    assert payload["result"]["changed"] is True
    assert "power_off" in station.call_names


def test_power_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3, fmt="json")
    result = runner.invoke(app, ["power", "on"])
    payload = json.loads(result.output)
    assert payload["result"]["state"] == "on"
    assert payload["result"]["auto_start_mode"] is True
    assert payload["result"]["idled_address"] == 3

    station_human = FakeStation(status=AUTO_START_STATUS)
    app_human = _app(station_human, monkeypatch, address=3, fmt="human")
    human = runner.invoke(app_human, ["power", "on"])
    assert "on" in human.output and "3" in human.output and "automatic" in human.output


# --- stop ---

def test_stop_uses_emergency_stop_facade_with_address(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch)
    result = runner.invoke(app, ["stop", "--address", "7"])
    assert result.exit_code == 0
    assert station.calls == [("emergency_stop", (), {"address": 7}), ("close", (), {})]


def test_stop_uses_emergency_stop_facade_for_all_locomotives_when_no_address(monkeypatch):
    station = FakeStation()
    # settings.address is 3, but stop's own --address is not given, so this
    # must stop everything, not narrow to the configured default.
    app = _app(station, monkeypatch, address=3)
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert station.calls == [("emergency_stop", (), {"address": None}), ("close", (), {})]


def test_stop_human_and_json_report_the_same_facts(monkeypatch):
    station = FakeStation()
    app = _app(station, monkeypatch, fmt="json")
    result = runner.invoke(app, ["stop", "--address", "7"])
    payload = json.loads(result.output)
    assert payload["result"] == {"address": 7, "scope": "single"}

    station_human = FakeStation()
    app_human = _app(station_human, monkeypatch, fmt="human")
    human = runner.invoke(app_human, ["stop", "--address", "7"])
    assert "7" in human.output


# --- never confirmed ---

def test_none_of_the_four_commands_ever_calls_confirm(monkeypatch):
    def _confirm_must_not_be_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("confirm called")

    monkeypatch.setattr(deps, "confirm", _confirm_must_not_be_called)
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    for args in (["power", "on"], ["power", "off"], ["stop"], ["drive", "10"], ["function", "f2", "on"]):
        result = runner.invoke(app, args, input="")
        assert result.exit_code == 0, (args, result.output, result.stderr)


def test_all_five_invocations_succeed_without_yes_and_without_a_prompt(monkeypatch):
    station = FakeStation(status=AUTO_START_STATUS)
    app = _app(station, monkeypatch, address=3)
    for args in (["power", "on"], ["power", "off"], ["stop"], ["drive", "10"], ["function", "f2", "on"]):
        result = runner.invoke(app, args, input="")
        assert result.exit_code == 0, (args, result.output, result.stderr)
        assert "[y/N]" not in result.stderr
```

Run it and read the failure - every test errors at collection because `railctl.cli.commands.power`
does not exist yet (it is imported before `throttle` in the same statement, so it is the name
Python reports):

```bash
uv run pytest tests/cli/test_throttle.py
```

Expected: `ERROR tests/cli/test_throttle.py` with `ModuleNotFoundError: No module named
'railctl.cli.commands.power'`, 0 collected.

- [ ] **Step 2: Add the `power`, `stop`, `drive` and `function` rows to `_meta.py`**

Doing this before either command module exists is deliberate: these are plain data classes with no
import of `commands/`, so nothing here can be blocked on `power.py`/`throttle.py`, and writing them
first means every later step in this file can import them immediately rather than importing names
that do not exist yet (which is exactly the ordering bug this rewrite is fixing - see the note at
the end of this task about what changed and why).

Add `from railctl.xbus.speed import MAX_SPEED_STEP` to `_meta.py`'s existing top-of-file import
block (alphabetically within its group - Task 5's own fix elsewhere in this plan exists precisely
because an import added below a constant is an `E402` violation). Then insert these seven
definitions right after `_SCHEMA`'s own definition and right before the `COMMANDS = (...)` line:

```python
DRIVE_SPEED_ARG = Argument(
    name="speed", help=f"speed step 0-{MAX_SPEED_STEP}", type="integer",
)
DRIVE_REVERSE_OPT = Option(
    name="--reverse", help="run in reverse; omit to keep the locomotive's current direction",
    type="boolean", default=None,
)
_DRIVE = CommandMeta(
    path="drive",
    help="set speed step and direction",
    schema="railctl/drive/v1",
    mutates=True,
    exit_codes=(0, 2, 3, 4, 5, 9, 12, 20),
    arguments=(DRIVE_SPEED_ARG,),
    options=(DRIVE_REVERSE_OPT,),
)

FUNCTION_FUNC_ARG = Argument(
    name="function", help="f0-f28, a bare number, or an alias such as 'light'", type="string",
)
FUNCTION_STATE_ARG = Argument(
    name="state", help="on, off or toggle - defaults to on", type="string", required=False,
)
FUNCTION_FORCE_GROUP_OPT = Option(
    name="--force-group", help="skip reading the current function group; clears the rest of the group",
    type="boolean", default=False,
)
_FUNCTION = CommandMeta(
    path="function",
    help="set F0-F28 on|off|toggle",
    schema="railctl/function/v1",
    mutates=True,
    exit_codes=(0, 2, 3, 4, 5, 9, 12, 20),
    arguments=(FUNCTION_FUNC_ARG, FUNCTION_STATE_ARG),
    options=(FUNCTION_FORCE_GROUP_OPT,),
)

POWER_STATE_ARG = Argument(name="state", help="on or off", type="enum", enum=("on", "off"))
_POWER = CommandMeta(
    path="power",
    help="track power on or off",
    schema="railctl/power/v1",
    mutates=True,
    exit_codes=(0, 2, 3, 4, 5),
    arguments=(POWER_STATE_ARG,),
)

STOP_ADDRESS_OPT = Option(
    name="--address", help="stop only this locomotive; omitted means every locomotive",
    type="integer", short="-a", default=None,
)
_STOP = CommandMeta(
    path="stop",
    help="emergency stop: all locomotives, or one with --address",
    schema="railctl/stop/v1",
    mutates=True,
    exit_codes=(0, 2, 3, 4, 5),
    options=(STOP_ADDRESS_OPT,),
)
```

Then replace the existing `COMMANDS = (_STATUS, _VERSION, _SCHEMA)` line with the tree order from
the design spec's L2 listing, minus `doctor` and `monitor`, which do not exist until Task 12:

```python
COMMANDS: Final[tuple[CommandMeta, ...]] = (
    _STATUS, _VERSION, _POWER, _STOP, _DRIVE, _FUNCTION, _SCHEMA,
)
```

`_BY_PATH` below it does not need touching - it is already built as `{c.path: c for c in COMMANDS}`,
so it picks up the four new rows automatically.

- [ ] **Step 3: Add `FunctionGroupUnreadableError` to `errors.py` and one branch to `default_suggestions`**

`function`'s must-pin 7 (a `--force-group` suggestion whose argv names the exact function and state
tokens the operator typed) cannot be built from `default_suggestions`'s existing shape -
`default_suggestions(exc, *, command, address=None, cv=None)` is keyed by exception type plus at
most an address or a CV number, and neither carries a function token like `"f2"` or a state token
like `"on"`. The fix carries the finished argv on a dedicated exception instead of trying to make
the generic table reconstruct it.

Add to `src/railctl/errors.py`, next to `StationBusyError` (both are `StationError` leaves that
carry one extra piece of structured data - `cv` there, `retry_argv` here):

```python
class FunctionGroupUnreadableError(StationError):
    """`function` could not read a group's current state before flipping one
    bit (departure 4, CONTRACT.md: `Station.function_set`/`function_toggle`
    refuse to blind-write a group rather than seed it all-zeros, the choice
    spec line 700 itself does not make). Carries the CLI's own retry
    command - the function and state tokens the operator actually typed,
    plus --force-group - since no exception-type-keyed table could
    reconstruct that argv from the exception alone.
    """

    def __init__(self, message: str, *, hint: str | None = None, retry_argv: list[str]) -> None:
        super().__init__(message, hint=hint)
        self.retry_argv = retry_argv
```

No new `EXIT_CODES` row: it falls back to `StationError`'s own base 9, exactly matching must-pin
7's "exits 9".

Add to `src/railctl/cli/_errors.py`: import `FunctionGroupUnreadableError` alongside the other
`railctl.errors` names already imported there, and add one branch inside `default_suggestions`,
ahead of the final `return []`:

```python
    if isinstance(exc, FunctionGroupUnreadableError):
        return [exc.retry_argv]
```

- [ ] **Step 4: Create the non-`register` half of `throttle.py` and `power.py`**

Writing both files now - even though neither has `register()` yet - is what lets
`tests/cli/test_throttle.py`'s own `from railctl.cli.commands import power, throttle` succeed for
the first time. `register` is added to each module in a later step below; nothing here is a
placeholder or a deliberately wrong line, it is simply the part of each file that does not need
`typer` or the metadata rows yet.

```python
# src/railctl/cli/commands/throttle.py
"""The `drive` and `function` commands, and the `preflight` guard a later
task's `cv` commands also import.

Nothing here holds an opcode, a framing byte or CV arithmetic: every mutation
goes through a `Station` facade method, and the only wire-adjacent names are
`Direction` and a function number, both of which the design allows through
`cli/` (spec line 1223; tests/test_layering.py rule 1).
"""

from __future__ import annotations

from typing import Final, Literal

from railctl.cli.result import CommandResult, tri_state
from railctl.errors import StationBusyError, StationError, TrackPowerError
from railctl.station import Station
from railctl.xbus.replies import LocoInfo, StationStatus
from railctl.xbus.speed import Direction

DRIVE_SCHEMA: Final[str] = "railctl/drive/v1"
FUNCTION_SCHEMA: Final[str] = "railctl/function/v1"
FUNCTION_ALIASES: Final[dict[str, int]] = {"light": 0, "lights": 0, "headlight": 0}
RUNNING_NOTICE: Final[str] = (
    "loco {address} is running at step {speed} {direction}; "
    "it keeps running after this command exits"
)

MAX_FUNCTION: Final[int] = 28
_DIRECTION_TEXT: Final[dict[Direction, str]] = {
    Direction.FORWARD: "forward",
    Direction.REVERSE: "reverse",
}


def preflight(station: Station, *, speed: int | None) -> StationStatus:
    """Guard for anything that could start or continue motion: `drive
    SPEED>0`, `function`, and (from a later task) every POM `cv` command.

    Never called for `drive 0` - the caller below skips it outright, because
    a stop must never be refused by a status this check dislikes. `speed` is
    only used to phrase the refusal; it is None for function and cv, where
    there is no single speed value to name.
    """
    status = station.status()
    if status.emergency_off or status.emergency_stop:
        target = f"speed {speed}" if speed is not None else "this command"
        raise TrackPowerError(
            f"track power is off or the layout is in emergency stop; refusing to send "
            f"{target}. Run `railctl power on` first."
        )
    if status.service_mode:
        raise StationBusyError(
            "a service-mode programming session is active on this station; it must "
            "finish or be cancelled before a throttle command can run"
        )
    return status


def parse_function(token: str) -> int:
    """`"f2"`, `"2"` or an alias in `FUNCTION_ALIASES` -> 0..28."""
    lowered = token.strip().lower()
    if lowered in FUNCTION_ALIASES:
        return FUNCTION_ALIASES[lowered]
    digits = lowered[1:] if lowered.startswith("f") else lowered
    if not digits.isdigit():
        raise ValueError(
            f"'{token}' is not a function: use f0..f{MAX_FUNCTION}, a bare number in "
            f"0..{MAX_FUNCTION}, or one of {sorted(FUNCTION_ALIASES)}"
        )
    value = int(digits)
    if not 0 <= value <= MAX_FUNCTION:
        raise ValueError(f"function {value} is out of range 0..{MAX_FUNCTION}")
    return value


def parse_state(token: str | None) -> Literal["on", "off", "toggle"]:
    """`None` (the argument was omitted) defaults to `"on"`."""
    if token is None:
        return "on"
    lowered = token.strip().lower()
    if lowered not in ("on", "off", "toggle"):
        raise ValueError(f"'{token}' is not a state: use on, off or toggle")
    return lowered  # type: ignore[return-value]
```

```python
# src/railctl/cli/commands/power.py
"""The `power` and `stop` commands.

`stop`'s own `--address` is deliberately NOT the same value as the global
`--address` / `RAILCTL_ADDRESS` / config default that `drive` and `function`
read through `settings.address` - see the note above `register` below.
"""

from __future__ import annotations

from typing import Final

from railctl.cli.result import CommandResult
from railctl.xbus.replies import StationStatus

POWER_SCHEMA: Final[str] = "railctl/power/v1"
STOP_SCHEMA: Final[str] = "railctl/stop/v1"


def build_power(
    state: str, status: StationStatus, *, changed: bool, idled_address: int | None
) -> CommandResult:
    outcome = CommandResult(schema=POWER_SCHEMA, command="power")
    outcome.result = {
        "state": state,
        "track_power": status.track_power,
        "auto_start_mode": status.auto_start_mode,
        "changed": changed,
        "idled_address": idled_address,
    }
    outcome.say(
        f"track power is {'on' if status.track_power else 'off'} "
        f"({'changed' if changed else 'no change'})"
    )
    # bit 2 is auto_start_mode (railctl.xbus.replies.StationStatus), never
    # short circuit - this wording must agree with build_status's, so both
    # read status.auto_start_mode rather than either retyping the bit.
    if status.auto_start_mode:
        outcome.say(
            "start mode is automatic: locomotives resume their last speed as soon "
            "as power returns"
        )
    else:
        outcome.say("start mode is manual: locomotives stay stopped until driven")
    if idled_address is not None:
        outcome.say(f"loco {idled_address} was set to speed 0 so it does not move on its own")
    return outcome


def build_stop(address: int | None) -> CommandResult:
    outcome = CommandResult(schema=STOP_SCHEMA, command="stop")
    outcome.result = {"address": address, "scope": "single" if address is not None else "all"}
    outcome.say(f"loco {address} stopped" if address is not None else "all locomotives stopped")
    return outcome
```

- [ ] **Step 5: Run the preflight and parsing tests**

```bash
uv run pytest tests/cli/test_throttle.py -k "preflight or parse_function or parse_state"
```

Expected: `8 passed`. Every other test in the file collects fine now (both command modules import
successfully) but still fails when it actually runs, since `build_drive`, `build_function` and
`register` do not exist yet in `throttle.py`, and `register` does not exist yet in `power.py` -
this `-k` selection is what proves the eight already written are real.

- [ ] **Step 6: Add `build_drive` and `build_function` to `throttle.py`**

Append to the same file:

```python
def build_drive(
    address: int, speed: int, direction: Direction, *, was: LocoInfo | None
) -> CommandResult:
    if was is None:
        changed: bool | None = None
        previous_speed_decoded: bool | None = None
    elif was.speed is None:
        # was.speed is None exactly when was.speed_steps != 128 - speed.py only
        # defines the 128-step layout, and replies.py leaves the rest UNKNOWN
        # rather than decode it wrong. Reporting a number here would be the
        # capability-recorded-as-absent failure this project exists to avoid,
        # just aimed at "changed" instead of a CV read.
        changed = None
        previous_speed_decoded = False
    else:
        changed = (was.speed, was.direction) != (speed, direction)
        previous_speed_decoded = True

    direction_text = _DIRECTION_TEXT[direction]
    outcome = CommandResult(schema=DRIVE_SCHEMA, command="drive")
    outcome.result = {
        "address": address,
        "speed": speed,
        "direction": direction_text,
        "changed": changed,
        "previous_speed_decoded": previous_speed_decoded,
    }
    outcome.say(
        f"loco {address} set to speed {speed} {direction_text} ({tri_state(changed)} changed)"
    )
    if previous_speed_decoded is False:
        outcome.say(
            "the locomotive's previous speed step mode is not 128-step and was not "
            "decoded, so whether this changed its speed is unknown"
        )
    elif previous_speed_decoded is None:
        outcome.say("the locomotive's previous state could not be read")
    return outcome


def build_function(address: int, function: int, state: str, *, now_on: bool) -> CommandResult:
    outcome = CommandResult(schema=FUNCTION_SCHEMA, command="function")
    outcome.result = {
        "address": address,
        "function": function,
        "requested": state,
        "now_on": now_on,
    }
    outcome.say(
        f"loco {address} F{function} is now {'on' if now_on else 'off'} (requested {state})"
    )
    return outcome
```

- [ ] **Step 7: Run the build_drive and build_function tests**

```bash
uv run pytest tests/cli/test_throttle.py -k "build_drive or build_function"
```

Expected: `6 passed`.

- [ ] **Step 8: Add `register` (the `drive` and `function` commands) to `throttle.py`**

Replace the file's import block with its final form (this adds `os`, `typer`, the `_meta.py`,
`_errors.py`, `config.py`, `deps.py` and `render.py` names, and the new `errors.py` class):

```python
from __future__ import annotations

import os
from typing import Final, Literal

import typer

from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import (
    DRIVE_REVERSE_OPT,
    DRIVE_SPEED_ARG,
    FUNCTION_FORCE_GROUP_OPT,
    FUNCTION_FUNC_ARG,
    FUNCTION_STATE_ARG,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import Settings, merge_settings, open_station, require_address
from railctl.cli.render import want_color
from railctl.cli.result import CommandResult, tri_state
from railctl.errors import (
    FunctionGroupUnreadableError,
    StationBusyError,
    StationError,
    TrackPowerError,
)
from railctl.station import Station
from railctl.xbus.replies import LocoInfo, StationStatus
from railctl.xbus.speed import Direction
```

Then append:

```python
# Built once, at import time, into names the signatures below reference - a
# call to `global_option(...)`/`typer_option(...)`/`typer_argument(...)`
# written directly as a parameter default would trip Ruff's B008 (function
# call in a default argument); its built-in allowlist covers a literal
# `typer.Option(...)`/`typer.Argument(...)` call, not a wrapper around one.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_SPEED_ARG = typer_argument(DRIVE_SPEED_ARG)
_REVERSE = typer_option(DRIVE_REVERSE_OPT)
_FUNC_ARG = typer_argument(FUNCTION_FUNC_ARG)
_STATE_OPT_ARG = typer_argument(FUNCTION_STATE_ARG)
_FORCE_GROUP = typer_option(FUNCTION_FORCE_GROUP_OPT)


def _output_for(cli_ctx: "Any", settings: Settings) -> OutputContext:
    """Rebuild the `OutputContext` only when this command's own `--format`/
    `--color` (redeclared below, per the module note about global-option
    position) actually changed the merged settings. This cannot call
    `railctl.cli.main.context_for` instead: `main.py` imports this module to
    call `register(app)`, so this module importing `main.py` back would be
    the same cycle `commands/basics.py`'s own note about `context_for`
    already rules out.
    """
    base = cli_ctx.output
    if settings.fmt == cli_ctx.settings.fmt and settings.color == cli_ctx.settings.color:
        return base
    return OutputContext(
        fmt=settings.fmt,
        color=want_color(settings.color, base.stdout, os.environ),
        stdout=base.stdout,
        stderr=base.stderr,
    )


def register(app: typer.Typer) -> None:
    """Attach `drive` and `function` to `app`.

    Both redeclare all eight global options (`target` through
    `non-interactive`) alongside their own arguments - Click parses a
    subcommand's own options anywhere after its name, but a `@app.callback()`
    group option only before it, and the spec's own worked session types
    `--address` after `drive` (spec line 1386). `global_option` (Task 10)
    builds each one with a `None`/`False`/`0` sentinel default and no envvar
    (the root callback already resolved the environment), and
    `merge_settings` (Task 9) layers only the ones actually typed here over
    `ctx.obj.settings`.
    """

    @app.command(
        "drive",
        help=command_meta("drive").help,
        epilog=help_epilog(command_meta("drive")),
    )
    def drive_cmd(
        ctx: typer.Context,
        speed: int = _SPEED_ARG,
        reverse: bool | None = _REVERSE,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj  # a CliContext built by main.py's own callback
        settings = merge_settings(
            cli_ctx.settings, target=target, address=address, fmt=format_,
            json_flag=json_flag, verbose=verbose, color=color, yes=yes,
            non_interactive=non_interactive,
        )
        output = _output_for(cli_ctx, settings)

        def action() -> CommandResult:
            resolved = require_address(settings, argv_hint=["railctl", "drive", str(speed)])
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                try:
                    was = station.loco_info(resolved)
                except StationError:
                    was = None
                if reverse is None:
                    # No --reverse/--no-reverse typed: keep whatever direction
                    # the locomotive is already running, per the worked
                    # session (spec line 1384) - "railctl drive 30 --address 3
                    # # keeps current direction". Only fall back to forward
                    # when there is no prior direction to keep at all.
                    direction = (
                        was.direction
                        if was is not None and was.direction is not None
                        else Direction.FORWARD
                    )
                else:
                    direction = Direction.REVERSE if reverse else Direction.FORWARD
                if speed > 0:
                    preflight(station, speed=speed)
                station.drive(resolved, speed, direction)
                if speed:
                    print(
                        RUNNING_NOTICE.format(
                            address=resolved, speed=speed, direction=_DIRECTION_TEXT[direction]
                        ),
                        file=output.stderr,
                    )
                return build_drive(resolved, speed, direction, was=was)
            finally:
                station.close()

        run("drive", output, action)

    @app.command(
        "function",
        help=command_meta("function").help,
        epilog=help_epilog(command_meta("function")),
    )
    def function_cmd(
        ctx: typer.Context,
        function: str = _FUNC_ARG,
        state: str | None = _STATE_OPT_ARG,
        force_group: bool = _FORCE_GROUP,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings = merge_settings(
            cli_ctx.settings, target=target, address=address, fmt=format_,
            json_flag=json_flag, verbose=verbose, color=color, yes=yes,
            non_interactive=non_interactive,
        )
        output = _output_for(cli_ctx, settings)

        def action() -> CommandResult:
            func_num = parse_function(function)
            state_token = parse_state(state)
            resolved = require_address(
                settings, argv_hint=["railctl", "function", function, state_token]
            )
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                preflight(station, speed=None)
                try:
                    if state_token == "toggle":
                        now_on = station.function_toggle(
                            resolved, func_num, force_group=force_group
                        )
                    else:
                        on = state_token == "on"
                        station.function_set(resolved, func_num, on, force_group=force_group)
                        now_on = on
                except StationError as exc:
                    # function_set/function_toggle read the current group
                    # before flipping one bit (spec line 1369) - a group
                    # command carries every bit of its group, so setting one
                    # function without reading the rest would silently clear
                    # them. The station raises a bare StationError with no
                    # structured retry information (departure 4, CONTRACT.md);
                    # this CLI layer is what attaches the --force-group escape
                    # as a machine-readable suggestion, because
                    # default_suggestions (railctl.cli._errors) is keyed only
                    # by exception type and never sees the function/state
                    # tokens the operator actually typed.
                    raise FunctionGroupUnreadableError(
                        f"could not read the current state of F{func_num} on loco "
                        f"{resolved}: {exc}",
                        retry_argv=[
                            "railctl", "function", function, state_token,
                            "--address", str(resolved), "--force-group",
                        ],
                    ) from exc
                result = build_function(resolved, func_num, state_token, now_on=now_on)
                try:
                    info = station.loco_info(resolved)
                except StationError:
                    info = None
                if info is not None and info.speed:
                    direction_text = (
                        _DIRECTION_TEXT[info.direction]
                        if info.direction is not None
                        else "unknown direction"
                    )
                    print(
                        RUNNING_NOTICE.format(
                            address=resolved, speed=info.speed, direction=direction_text
                        ),
                        file=output.stderr,
                    )
                return result
            finally:
                station.close()

        run("function", output, action)
```

- [ ] **Step 9: Run every drive and function test**

```bash
uv run pytest tests/cli/test_throttle.py -k "drive or function"
```

Expected: `23 passed` - this filter matches every test whose name contains "drive" or "function",
which includes the two `parse_function` tests and all six `build_drive`/`build_function` tests
already green from Steps 5 and 7, plus the nine new `drive`/`function` command tests, plus the two
parity tests. Power and stop tests are not selected by this filter (their names contain neither
word), but they already pass too by this point, since `power.py`'s pure half exists from Step 4 -
this run only re-confirms the subset most relevant to what changed in this step.

- [ ] **Step 10: Add `register` (the `power` and `stop` commands) to `power.py`**

Replace the file's import block with its final form:

```python
from __future__ import annotations

import os
from typing import Final

import typer

from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import (
    POWER_STATE_ARG,
    STOP_ADDRESS_OPT,
    command_meta,
    global_option,
    help_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import Settings, merge_settings, open_station
from railctl.cli.render import want_color
from railctl.cli.result import CommandResult
from railctl.xbus.replies import StationStatus
from railctl.xbus.speed import Direction
```

Then append:

```python
# Built once, at import time - see the identical B008 note in throttle.py.
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")

_STATE_ARG = typer_argument(POWER_STATE_ARG)
_STOP_ADDRESS = typer_option(STOP_ADDRESS_OPT)


def _output_for(cli_ctx: "Any", settings: Settings) -> OutputContext:
    """See the identical helper in `throttle.py` - duplicated rather than
    imported across the two command modules, since neither is allowed to
    import from `railctl.cli.main` (the cycle `context_for`'s own note
    describes) and this project's file structure has no third
    `cli/commands/_shared.py` module to hold one shared copy."""
    base = cli_ctx.output
    if settings.fmt == cli_ctx.settings.fmt and settings.color == cli_ctx.settings.color:
        return base
    return OutputContext(
        fmt=settings.fmt,
        color=want_color(settings.color, base.stdout, os.environ),
        stdout=base.stdout,
        stderr=base.stderr,
    )


def register(app: typer.Typer) -> None:
    """Attach `power` and `stop` to `app`.

    `power` redeclares all eight global options, same as every command in
    `throttle.py`. `stop` redeclares only SEVEN of them: its own
    `STOP_ADDRESS_OPT` already claims the `--address`/`-a` flag names for a
    command-scoped meaning ("only this locomotive, omit for all of them")
    that must never fall back to `settings.address` (see the module
    docstring) - declaring `global_option("--address")` a second time under
    the same flag names would either collide in Click or quietly reintroduce
    the exact fallback that decision argues against.
    """

    @app.command(
        "power",
        help=command_meta("power").help,
        epilog=help_epilog(command_meta("power")),
    )
    def power_cmd(
        ctx: typer.Context,
        state: str = _STATE_ARG,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings = merge_settings(
            cli_ctx.settings, target=target, address=address, fmt=format_,
            json_flag=json_flag, verbose=verbose, color=color, yes=yes,
            non_interactive=non_interactive,
        )
        output = _output_for(cli_ctx, settings)

        def action() -> CommandResult:
            # POWER_STATE_ARG is type="enum", enum=("on", "off") (Task 10's
            # _enum_guard) - Click rejects anything else before this function
            # body ever runs, so there is no "if state not in (...)" check
            # here left to go stale and unreachable, the way _expect_ack's
            # dead Unsupported branch did elsewhere in this plan.
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                if state == "on":
                    # Only the last step (speed 0 to the resolved address) is
                    # proven. The stop-first prefix rests on an inference
                    # about the station's refresh buffer, not a measurement:
                    # docs/probe-results.md never captured what that buffer
                    # holds across a power cycle. This station's start mode
                    # is automatic (same doc), so skipping the stop-all risks
                    # a speed left over from before power was cut resuming
                    # the instant power_on() returns.
                    station.emergency_stop(address=None)
                    station.power_on()
                    status = station.status()
                    idled_address = settings.address
                    if idled_address is not None:
                        station.drive(idled_address, 0, Direction.FORWARD)
                    return build_power("on", status, changed=True, idled_address=idled_address)
                before = station.status()
                was_on = before.track_power
                if was_on:
                    station.power_off()
                    after = station.status()
                else:
                    after = before
                return build_power("off", after, changed=was_on, idled_address=None)
            finally:
                station.close()

        run("power", output, action)

    @app.command(
        "stop",
        help=command_meta("stop").help,
        epilog=help_epilog(command_meta("stop")),
    )
    def stop_cmd(
        ctx: typer.Context,
        address: int | None = _STOP_ADDRESS,
        target: str | None = _TARGET,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        settings = merge_settings(
            cli_ctx.settings, target=target, fmt=format_,
            json_flag=json_flag, verbose=verbose, color=color, yes=yes,
            non_interactive=non_interactive,
        )
        output = _output_for(cli_ctx, settings)

        def action() -> CommandResult:
            station = open_station(settings, capabilities_path=capabilities_path())
            try:
                # 92 AH AL XOR through the facade, never E4 13 with wire
                # value 1 - that opcode is an ordinary speed-zero command,
                # not an emergency stop (spec line 1368).
                station.emergency_stop(address=address)
                return build_stop(address)
            finally:
                station.close()

        run("stop", output, action)
```

- [ ] **Step 11: Run the whole test file**

```bash
uv run pytest tests/cli/test_throttle.py
```

Expected: `39 passed`.

- [ ] **Step 12: Wire `power.py` and `throttle.py` into `main.py`**

Open `src/railctl/cli/main.py`. Alongside its existing `<module>.register(app)` calls for the
commands earlier tasks added, add:

```python
from railctl.cli.commands import power, throttle

power.register(app)
throttle.register(app)
```

- [ ] **Step 13: Confirm `railctl` actually runs the new commands end to end**

```bash
uv run railctl --help
```

Expected: `power`, `stop`, `drive` and `function` appear in the command list alongside whatever
earlier tasks already registered.

- [ ] **Step 14: Run the full test suite and the coverage gate**

```bash
uv run pytest
```

Expected: `920 + <everything Tasks 1-10 added> + 39 passed`, `0 failed`. This task adds exactly 39
tests, all in `tests/cli/test_throttle.py`; nothing under this task's own files is parametrised by
`bench`/`bench_factory`, so 39 is the flat count, not a formula with a multiplier. The running total
across the whole suite is stated as a sum, not a number, because nobody has measured what Tasks
1-10 actually added - a small mismatch there is an arithmetic slip the first execution corrects in
place, while a different failing test is a real signal.

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table with `src/railctl/cli/commands/power.py` and
`src/railctl/cli/commands/throttle.py` now listed, `Required test coverage of 90% reached.` If any
line is missing, it is almost always the `except StationError` branch in `function_cmd`'s facade
call, the `was is None` branch of `build_drive`, or the `_output_for` rebuild branch - all three are
exercised by tests above (`test_function_suggests_force_group_when_state_cannot_be_read`,
`test_build_drive_changed_unknown_when_prior_state_unavailable`, and
`test_drive_format_option_after_the_subcommand_overrides_the_configured_default`), so a gap here
means one of those three did not reach the line it was meant to and is this task's own bug to fix,
not a reason to relax the gate.

- [ ] **Step 15: Lint and format**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both report no issues.

- [ ] **Step 16: Run the layering guard directly**

```bash
uv run pytest tests/test_layering.py
```

Expected: `8 passed`, including `test_the_rule_1_and_2_targets_are_scanned_once_they_exist`, which
now sees `cli/` non-empty for the first time in this plan's execution and would fail loudly if
`power.py` or `throttle.py` contained anything the guard forbids.

- [ ] **Step 17: Commit**

```bash
git add src/railctl/cli/commands/power.py src/railctl/cli/commands/throttle.py \
        src/railctl/cli/_meta.py src/railctl/cli/main.py src/railctl/errors.py \
        src/railctl/cli/_errors.py tests/cli/test_throttle.py
git commit -m "feat(cli): add power, stop, drive and function commands

Adds the four throttle commands and their safety pre-flights: power on
sends emergency-stop-all before track power on and idles the resolved
address afterward, because this station's start mode is automatic and a
cached speed would otherwise resume the instant power returns. drive and
function refuse on emergency-off, emergency stop or an active service-mode
session; drive 0 is exempt so a stop is never refused. function flips
exactly one bit through function_set/function_toggle and raises a
FunctionGroupUnreadableError carrying a --force-group retry command when
the current state cannot be read. None of the four commands ever asks for
confirmation.

Every command redeclares the eight global options so Click can parse them
after the subcommand name, matching the spec's own worked examples
(railctl drive 30 --address 3); stop redeclares seven of the eight, since
its own --address option is deliberately not the configured default one.

--for SECONDS is out of scope here; it needs a timed-revert path that
survives Ctrl-C and belongs with the operation-resource work of a later
plan."
```

---

### Task 12: `doctor` and `monitor`, and the M6 acceptance run

**Files:**
- Create: `src/railctl/cli/commands/doctor.py`, `src/railctl/cli/commands/monitor.py`,
  `tests/cli/test_doctor.py`, `tests/cli/test_monitor.py`, `tests/hardware/test_m6_acceptance.py`
- Modify: `src/railctl/cli/_meta.py` (add the `doctor` and `monitor` rows), `src/railctl/cli/main.py`
  (register both commands), `src/railctl/cli/_errors.py` (one extra `isinstance` branch in
  `default_suggestions` - see the note below, this is a small, deliberate addition to a file
  outside this task's original file list, not a rewrite), `CHANGELOG.md`, `README.md`

**Interfaces:**

- Consumes, exactly as merged on disk:
  - `railctl.cli.result.CommandResult` - plain (not frozen) `@dataclass`: `schema: str`,
    `command: str`, `ok: bool = True`, `exit_code: int = 0`, `elapsed_ms: int = 0`,
    `link: LinkInfo | None = None`, `station: StationInfo | None = None`,
    `warnings: list[ResultWarning] = field(default_factory=list)`,
    `result: dict[str, object] = field(default_factory=dict)`,
    `lines: list[str] = field(default_factory=list)`; methods `warn(name, message, **details)`,
    `say(line)`, `envelope() -> dict[str, object]` (key order `schema, ok, command, exit_code,
    elapsed_ms, [link, station], warnings, result`; `link`/`station` keys are omitted entirely when
    `None`, never emitted as `null`)
  - `railctl.cli.result.LinkInfo` - `@dataclass(frozen=True, slots=True)`, `identity: str`,
    `target: str`
  - `railctl.cli.result.StationInfo` - `@dataclass(frozen=True, slots=True)`, `protocol: str`,
    `protocol_version: str | None`, `command_station_id: int | None`
  - `railctl.cli.result.tri_state(value: bool | None) -> Literal["yes", "no", "unknown"]`.
    Bool-typed only: the four non-boolean tri-state `Capabilities` fields
    (`xpressnet_version`, `command_station_id`, `loco_address_threshold`, `pom_result_channel`,
    `probed_at`) are rendered by this task's own `_text()` helper below, never passed to
    `tri_state()`, which would reject a non-`bool` argument's type at the call site conceptually
    even though Python will not stop you - passing `"4.0"` through it would be a silent type
    confusion, not a caught bug.
  - `railctl.cli.render.render(result, *, fmt, stdout, color) -> None`,
    `railctl.cli.render.render_error(report, *, stderr, fmt, color) -> None`,
    `railctl.cli.render.NdjsonStream(stream)` - `.sequence: int`, `.event(type_, **fields) -> None`,
    `.summary(**fields) -> None`, both writing one compact (`separators=(",", ":")`) JSON line
  - `railctl.cli._errors.OutputContext` - `@dataclass(frozen=True, slots=True)`: `fmt: Format`,
    `color: bool`, `stdout: TextIO`, `stderr: TextIO`
  - `railctl.cli._errors.run(command: str, ctx: OutputContext, work: Callable[[], CommandResult]) ->
    NoReturn` - calls `work()`; on a normal return renders it and raises `typer.Exit(result.exit_code)`;
    on `KeyboardInterrupt` reports `AbortedError` (exit 9); on `RailctlError` reports it via
    `report_for`; on anything else reports `"internal"` (exit 1). Every branch ends in
    `raise typer.Exit(...)` - `run()` never returns.
  - `railctl.cli._errors.report_for(exc, *, command, details=None, suggestions=None) -> ErrorReport`
    and `railctl.cli._errors.default_suggestions(exc, *, command, address=None, cv=None) ->
    list[list[str]]` - as merged on disk **before** this task's Step 3 (see below): the one
    `isinstance` branch inside `default_suggestions` currently reads
    `if isinstance(exc, PomReadUnsupportedError): suggestions = [["railctl", "doctor"]]; ...`.
  - `railctl.errors.{DecoderNotRespondingError, PomReadUnsupportedError, RailctlError}` - both
    subclass `ProgrammingError(StationError(RailctlError))` and carry `.cv: int | None`; already on
    disk, exit codes 13 and 16
  - `railctl.cli.result.ErrorReport` - `@dataclass(frozen=True, slots=True)`: `code: str`,
    `message: str`, `retryable: bool`, `exit_code: int`, `details: dict[str, object] = {}`,
    `suggestions: list[list[str]] = []`, `hint: str | None = None`; `envelope() -> dict[str,
    object]` (key order `schema, code, message, hint, retryable, exit_code, details, suggestions`,
    `"hint": None` when there is none) - normalisation sheet 1.9/5d, unchanged by the addendum.
    `report_for(...).envelope()` is how any caller gets the JSON shape of an error; `_run_ndjson`
    (Step 23) is this task's own such caller, the first one in this plan that is not `run()` itself.
  - `railctl.errors.exit_code_for(exc: BaseException) -> int` - the same lookup `report_for` uses
    to fill `ErrorReport.exit_code`; `_run_ndjson` calls it directly because it renders the error
    by hand instead of going through `run()`.
  - `railctl.cli._meta.command_meta(path: str) -> CommandMeta` and
    `railctl.cli._meta.help_epilog(meta: CommandMeta) -> str` - Task 10's shape (ADDENDUM Part
    B.1/B.6, which wins over this task's own earlier drafting). `command_meta` is a pure lookup by
    path; its `ValueError` on an unknown path propagates uncaught. `help_epilog` builds the fixed
    `OUTPUT`/`EXIT CODES`/`EXAMPLES` sections from a `CommandMeta` row plus `errors.EXIT_CODES`.
    Every registered command - `doctor` and `monitor` included - imports both and uses them at
    registration time: `help=command_meta(path).help, epilog=help_epilog(command_meta(path))`. No
    command module ever imports a `CommandMeta` row constant by name; the rows in `_meta.py`
    (`_DOCTOR`, `_MONITOR`, and the seven already on disk) stay module-private for exactly that
    reason.
  - `railctl.station.{Capabilities, Check, DoctorReport, StationEvent}` (`station/__init__.py`'s
    re-export, Task 1/Task 7) - `Capabilities` is the frozen 13-field-plus-`notes` dataclass;
    `Capabilities.save(path: Path) -> bool` returns `False` and writes nothing when
    `link_identity == UNKNOWN_IDENTITY`, otherwise merges this station's entry into whatever is
    already at `path` and writes atomically; `Check(id, title, status, detail)`,
    `status: Literal["ok", "fail", "skip", "unknown"]`; `DoctorReport(checks, capabilities)` with
    `@property ok` (`True` iff D0/D1/D2 are `"ok"` and D3 is not `"fail"`); `StationEvent(at, name,
    detail, payload)` - all four fields required, no defaults
  - `railctl.station.verdict_lines(report: DoctorReport) -> list[str]` and
    `railctl.station.exit_code_for_report(report: DoctorReport) -> int` (0 when `report.ok`,
    else 3) - **re-exported from `railctl.station.__init__`** (Task 7e); import both from
    `railctl.station`, never from `railctl.station.doctor` directly (normalisation sheet 1r
    settles what Task 7's own prose left ambiguous)
  - `railctl.station.facade.Station` (type-checking only here) - `probe(self, *, address:
    int | None = None, allow_power_on: bool = False, use_programming_track: bool = True) ->
    DoctorReport`, `events(self, *, interval: float = 0.25) -> Iterator[StationEvent]`,
    `close(self) -> None`, `capabilities: Capabilities` (read-only property)
  - This task's own authoring completes two contracts that no earlier task in this plan pins to an
    exact shape, because they are needed here for the first time and no later task changes them:
    - `railctl.cli._meta.{Option, CommandMeta, COMMANDS, global_option, typer_option}` - **not**
      this task's own invention: Task 10 shipped these shapes and Task 11 already extends them
      (normalisation sheet Part 1.14, which wins over this task's own earlier assumptions). `Option`
      carries `name` (the flag, e.g. `"--power-on"`), `type: OptionType` (the literal `"string"` /
      `"integer"` / `"boolean"` / `"enum"` - a manifest-friendly string, never a Python `type`
      object and never the bare words `"bool"` / `"int"` / `"str"`, because `railctl schema
      --format=json` must be able to serialise it directly), plus `short`, `default`, `help`,
      `enum`, `required`, `env`, `repeatable`. There is no `dest` field - the Python parameter name
      in the command function's own signature already carries that role. `CommandMeta` carries
      `path: str` (never a tuple, e.g. `"doctor"`), `help: str`, `schema: str` (**required** on
      every row, or `manifest()` breaks the moment this task's rows land), `mutates: bool`,
      `exit_codes: tuple[int, ...]`, `arguments`, `options`, `confirms`. `COMMANDS: Final[tuple[
      CommandMeta, ...]]` is a **tuple**, never a dict - this task rebuilds the whole literal
      rather than mutating it in place, ending in the nine-path tree order `doctor, status,
      version, power, stop, drive, function, monitor, schema`, and it already holds rows for
      `status`, `version`, `power`, `stop`, `drive`, `function` and `schema` before this task
      starts. `typer_option(opt: Option) -> typer.models.OptionInfo` builds the actual
      `typer.Option(...)` default a command function's signature uses, so the manifest and the
      parser can never drift apart.
    - `railctl.cli._meta.global_option(name: str) -> typer.models.OptionInfo` builds a
      *per-command* copy of a `GLOBAL_OPTIONS` row - same flags, help and enum guard, but
      `default=None` (`False` for a boolean, `0` for `--verbose`) and no `envvar`, because the
      callback already resolved the environment. It exists because Click parses a group's own
      options only **before** the subcommand name: `railctl doctor --address 3` hands `--address`
      to `doctor_cmd`, never to `@app.callback()`, and there is no framework switch that changes
      this (`allow_interspersed_args` governs arguments inside one command, not group options after
      a subcommand name). So every registered command - `doctor` and `monitor` included - declares
      all eight global options in its own signature via `global_option`, one short line each, and
      rebuilds its effective `Settings` with `merge_settings` before doing anything else. This is
      also the fix for M6's own acceptance sentence: `railctl doctor --address 3` has to parse, not
      just `railctl --address 3 doctor`.
    - `railctl.cli.deps.{Settings, merge_settings, open_station, link_info, station_info}` - the
      module is `deps.py`, never `_deps.py`. `Settings` is the fully-resolved global-option bundle:
      `target: str`, `address: int | None`, `fmt: Format` (the field is `fmt`, never `format`),
      `verbose: int`, `color: Literal["auto", "always", "never"]` (still the raw CLI choice here,
      not yet resolved to a bool), `assume_yes: bool`, `interactive: bool`. It is built once by
      `main.py`'s Typer callback and stored on `ctx.obj` as part of a `CliContext(settings, output)`
      - **not** as a bare `Settings` - so a command function reads `ctx.obj.settings`, never
      `ctx.obj` directly. `merge_settings(base: Settings, *, target=None, address=None, fmt=None,
      json_flag=False, verbose=0, color=None, yes=False, non_interactive=False) -> Settings`
      overlays a command's own per-command global options onto the callback's `base` settings - an
      argument only takes effect when it is "typed" (not `None` for the string/int options, `True`
      for the three booleans, `> 0` for `verbose`). `open_station(settings: Settings, *,
      capabilities_path: Path | None) -> Station` resolves `settings.target`, opens the link,
      loads `Capabilities.load(capabilities_path, link.identity)` when `capabilities_path` is given
      or starts from `Capabilities.unknown(link.identity)` when it is `None`, and constructs the
      `Station` with that same `capabilities_path` as its flush target. `link_info(station:
      Station, settings: Settings) -> LinkInfo` (**two** arguments - `settings.target` supplies
      `LinkInfo.target`, since the station's own `link` object never stores the string the operator
      typed) and `station_info(station: Station) -> StationInfo` read `station.link.identity` /
      `station.capabilities.{xpressnet_version, command_station_id}`.
    - `railctl.cli.config.capabilities_path(env: Mapping[str, str] | None = None) -> Path` - lives
      in `config.py`, **not** `deps.py`, and is XDG-based: `$XDG_CONFIG_HOME/railctl/
      capabilities.json`, falling back to `~/.config/railctl/capabilities.json` when
      `XDG_CONFIG_HOME` is unset. Every test that writes the file monkeypatches `XDG_CONFIG_HOME`
      (and `HOME`, belt for the fallback branch) - a `HOME`-only patch leaks onto the developer's
      real config directory on any machine that has `XDG_CONFIG_HOME` set.

- Produces (later tasks depend on these EXACT signatures - do not rename, do not re-type):

`src/railctl/cli/commands/doctor.py`:
```python
DOCTOR_SCHEMA: Final[str] = "railctl/doctor/v1"

def build_doctor(report: DoctorReport, *, saved_to: Path | None) -> CommandResult: ...
def register(app: typer.Typer) -> None: ...
```
`src/railctl/cli/commands/monitor.py`:
```python
MONITOR_SCHEMA: Final[str] = "railctl/monitor/v1"

def build_monitor(seen: Sequence[StationEvent], *, complete: bool, streamed: bool) -> CommandResult: ...
def stream_monitor(station: Station, *, ndjson: NdjsonStream,
                   limit: int | None = None) -> int: ...      # returns the event count
def register(app: typer.Typer) -> None: ...
```

`build_monitor` gained `streamed` over this task's first draft: no later task in this plan depends
on the old two-keyword shape, so widening it here costs nothing and fixes the double-print bug
described below.

**A gap this task closes in `_errors.py`, not invented here.** `default_suggestions` already routes
`PomReadUnsupportedError` to `[["railctl", "doctor"], ...]` (Task 8). Design line 1335 - "A failed
POM read always suggests `["railctl","doctor"]` first" - is written about POM reads failing in
general, and `DecoderNotRespondingError` (three attempts, no result at all: R1) is the other shape a
failed POM read takes. Left unhandled, `default_suggestions(DecoderNotRespondingError(...), ...)`
falls through to the final `return []`, and this task's own pinned test #1 below would fail against
it. Step 3 adds `DecoderNotRespondingError` to the existing `isinstance` check - one word inside an
existing tuple, not a new branch, not a new file.

**Why `build_doctor` and `build_monitor` never take a `Station` or a `Link`.** Both signatures are
pure functions of data already produced elsewhere - a `DoctorReport` (which carries
`capabilities.link_identity`, `xpressnet_version`, `command_station_id`) and a plain sequence of
`StationEvent`. Populating `CommandResult.link` / `.station` needs `link_info(station, settings)` /
`station_info(station)`, which need the live `Station` object (and, for `link_info`, the resolved
`Settings`) these functions do not receive - so `register()`'s own
`work()` closures set `result.link` / `result.station` themselves, immediately after calling
`build_doctor`/`build_monitor`. This keeps `build_doctor`/`build_monitor` testable with no fake
transport at all, which is exactly what lets tests #2-#8 and #11 below construct a `DoctorReport`
or a list of `StationEvent` by hand and never touch a `Station`.

**Two commands, three formats, and one deliberate exception to "only `render()` writes to
stdout".** `status`/`version`/`power`/etc. (Tasks 9-11) are point-in-time: one `CommandResult`,
one call to `render()`, done. `monitor` is not - "`human` prints one line per event as it arrives
and `json` buffers until the run ends" is the Decision this plan already made, and a command that
must show output *while it is still running* cannot wait for a single end-of-run `render()` call
to do it. `monitor`'s `human` and `ndjson` paths write directly to `ctx.stdout` from inside the
event loop, exactly as `backup`/`restore`/`diff` (a later plan) are already documented to do for
`ndjson` (Task 8's `render.py` docstring: "A streaming command ... builds its own NdjsonStream
directly and never calls this branch"). `monitor`'s `json` path is the ordinary one: it buffers
`seen` and returns one `CommandResult` for `run()` to render once, which is exactly test #10 below.
Because `human` writes each event to stdout itself, `build_monitor`'s own `streamed` keyword tells
it not to *also* put those lines in `CommandResult.lines` - `render()` would print them a second
time otherwise, once as they streamed past and once when it renders the final result.

**Layering note.** `cli/commands/doctor.py` and `cli/commands/monitor.py` live under `cli/`, so
`tests/test_layering.py` scans every line of both. Neither file touches a CV number, a framing
byte, a port name or the word "tty" in any casing - `doctor` and `monitor` talk to `Station`,
`DoctorReport`, `StationEvent` and the `cli.result`/`cli.render` types only, never to `xbus` or
`transport` directly. Watch the docstrings you write, not just the code: a stray "TTY" in a comment
fails rule 1 exactly as a stray `cv - 1` would fail rule 2.

**Decisions already made - do not re-open, do not contradict:**
- `report.capabilities.save(capabilities_path(os.environ))` is called explicitly by `doctor`'s own
  `work()` closure, importing `capabilities_path` from `railctl.cli.config` (it is not in
  `deps.py`); `open_station` is called with `capabilities_path=None`, so `Station.close()` flushes
  nothing and the doctor's full record is written exactly once.
- Every registered command, `doctor` and `monitor` included, declares all eight global options in
  its own signature via `_meta.global_option` and rebuilds its effective `Settings` with
  `merge_settings(ctx.obj.settings, ...)` before doing anything else - not because this task
  invents that pattern, but because without it `railctl doctor --address 3` and `railctl monitor
  --format ndjson` cannot parse at all: Click hands options written after the subcommand name to
  the subcommand, never back to `@app.callback()`. This is also why the `OutputContext` each command
  builds takes its `fmt` and `color` from the *merged* `settings`, never from `ctx.obj.output.fmt`
  - the callback's own `OutputContext` was built before this command's own `--format`/`--color`
  were known. `Settings` never carries a stream, though, so `stdout`/`stderr` are the one pair of
  fields that *does* come straight from `cli_ctx.output` (`cli_ctx.output.stdout` /
  `cli_ctx.output.stderr`), never from a module-global `sys.stdout`/`sys.stderr` - `OutputContext`
  exists precisely so no command body reaches for one (ADDENDUM Part B.5).
- Test #7 below ("the human output ends with the four-line verdict block") settles an apparent
  tension in this task's own brief between "ends with the verdict" and "lead with the verdict, then
  the checks, then the notes": this plan follows the literal pinned assertion. The checks and
  capabilities are the raw evidence: the verdict is the synthesised takeaway, and closing the
  report with it - after the reader has seen the evidence it is built from - is what "ends with"
  means here. `render()` still prints its own automatic `"doctor: ok"` / `"doctor: failed"` headline
  first (Task 8), so the report is never headline-free at the top; it is the checks/capabilities/
  notes detail that comes before the verdict, not the overall status.
- `Station.events()` is never wrapped in a blanket `except Exception` or `except BaseException` by
  this task's code. `stream_monitor` (the `ndjson` streaming path) catches only `KeyboardInterrupt`,
  writes the `summary` line in a `finally` block so the stream on stdout always ends the same way,
  and then **re-raises** - the interrupt still reaches `_run_ndjson`'s own `except KeyboardInterrupt`
  (Step 15), which is what decides the process's exit code. The buffering paths (`human`/`json`,
  `_work` in Step 9) catch `KeyboardInterrupt` themselves and return a normal, partial
  `CommandResult(complete=False)` instead - there is no line-per-event stdout contract to protect
  there, and `run()` needs a normal return to render it as one JSON/human value rather than as an
  error object.
- `--limit` exists so tests can end the loop without an interrupt at all; it is a `monitor`-only
  `Option` in `_meta.py`, not a global one.

---

- [ ] **Step 1: Write the failing `report_for` contrast tests**

```python
# tests/cli/test_doctor.py
"""Pins the `doctor` command's rendering contract and, in the first test, the
`report_for` contract that a future `cv read --mode pom` (Plan 4) depends on: a station
that answered nothing at all must never read as "not supported". The contrasting case -
a `pom_read=false` conclusion naming where it came from - is Plan 4's own test (a station
that answers "no" must say so from a message *built* in the station layer, not from a
message this test writes for itself and then asserts against): normalisation sheet 4b.
"""

from __future__ import annotations

from railctl.cli._errors import report_for
from railctl.errors import DecoderNotRespondingError
from railctl.station import Capabilities


def test_decoder_not_responding_never_says_unsupported():
    """R1 (docs/probe-results.md): the station ACKs a POM read and returns nothing at
    all - no `61 13`, no `61 82`, no value. That is UNKNOWN, never a negative answer,
    and this is the end-to-end assertion Plan 4's `cv read --mode pom` will rely on.
    """
    exc = DecoderNotRespondingError(
        "CV8 produced no result over POM after 3 attempts "
        "(interface ack only; docs/probe-results.md, R1)",
        cv=8,
    )
    report = report_for(exc, command="cv read")
    assert report.code == "decoder_not_responding"
    assert report.exit_code == 13
    assert "unsupported" not in report.message.lower()
    assert "not supported" not in report.message.lower()
    assert report.suggestions[0] == ["railctl", "doctor"]
```

The dropped test built a `PomReadUnsupportedError` message itself, then asserted `report.message`
contains four substrings of that same message - `report_for` copies `str(exc)` verbatim, so this
was `assert x in x` and could never go red for the reason its own docstring claimed. The real
requirement - "a `pom_read=false` conclusion must name its provenance" - belongs where that message
is *built*, in the station layer (Plan 4's `cv read`), not in this CLI-only rendering test.

- [ ] **Step 2: Run and see the first test fail for the documented reason**

Run: `uv run pytest tests/cli/test_doctor.py`

Expected: FAIL - 1 failed. `test_decoder_not_responding_never_says_unsupported` fails on
`assert report.suggestions[0] == ["railctl", "doctor"]` with `IndexError: list index out of range`
(`default_suggestions` returns `[]` for `DecoderNotRespondingError` today - it only recognises
`PomReadUnsupportedError`).

- [ ] **Step 3: Add `DecoderNotRespondingError` to `default_suggestions`**

Open `src/railctl/cli/_errors.py`. Add `DecoderNotRespondingError` to the existing import from
`railctl.errors`, and widen the one `isinstance` check inside `default_suggestions`:

```python
# before, in the existing import block:
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    PomReadUnsupportedError,
    RailctlError,
    exit_code_for,
)

# after:
from railctl.errors import (
    AbortedError,
    ConfirmationRequiredError,
    DecoderNotRespondingError,
    PomReadUnsupportedError,
    RailctlError,
    exit_code_for,
)
```

```python
# before, inside default_suggestions:
    if isinstance(exc, PomReadUnsupportedError):
        suggestions = [["railctl", "doctor"]]
        if cv is not None:
            suggestions.append(["railctl", "cv", "read", str(cv), "--mode", "service"])
        return suggestions

# after:
    if isinstance(exc, (PomReadUnsupportedError, DecoderNotRespondingError)):
        suggestions = [["railctl", "doctor"]]
        if cv is not None:
            suggestions.append(["railctl", "cv", "read", str(cv), "--mode", "service"])
        return suggestions
```

`DecoderNotRespondingError` is a `ProgrammingError` subclass and already carries `.cv`, so
`report_for`'s existing `cv = getattr(exc, "cv", None)` line needs no change - the widened branch
picks it up for free. Nothing about `PomReadUnsupportedError`'s own behaviour changes: the tuple
grew, the body did not.

- [ ] **Step 4: Run and see the test pass, then the whole errors suite**

Run: `uv run pytest tests/cli/test_doctor.py tests/cli/test_errors.py`

Expected: PASS, 38 passed, 0 failed (1 new here, 37 already in `test_errors.py` from Task 8 -
adding `DecoderNotRespondingError` to an existing tuple changes no other test's outcome, since
every other row in `test_errors.py`'s parametrised suggestion tests still only exercises
`PomReadUnsupportedError` / `ConfirmationRequiredError`).

- [ ] **Step 5: Write the failing `build_doctor` rendering tests**

Add the imports below to the top of `tests/cli/test_doctor.py`'s existing import block (ruff's
`E402` rule fails on a module-level import that is not at the top of the file, so the `import`
lines go up there even though the test functions below join the bottom of the file):

```python
import io
import json

from railctl.cli.commands.doctor import DOCTOR_SCHEMA, build_doctor
from railctl.cli.render import render
from railctl.station import Check, DoctorReport, verdict_lines
```

`verdict_lines` is re-exported from `railctl.station.__init__` (Task 7e), so it is imported from
there, never from `railctl.station.doctor` directly.

Then append the test functions themselves at the end of the file:

```python
def _bench_report(*, powered: bool) -> DoctorReport:
    """Track unpowered, `--power-on` not given: D3 is `unknown`, not `fail` - the
    expected state of a bench setup, per design line 881. D4/D10 skip because they
    need main/programming-track power respectively.
    """
    d3_status = "ok" if powered else "unknown"
    d3_detail = "track power on" if powered else "track power off; re-run with --power-on"
    checks = (
        Check(id="D0", title="link", status="ok", detail="link opened"),
        Check(id="D1", title="link alive", status="ok", detail="XpressNet 4.0, id 0x12"),
        Check(id="D2", title="status", status="ok", detail="raw status byte decoded"),
        Check(id="D3", title="track power", status=d3_status, detail=d3_detail),
        Check(id="D4", title="POM read", status="skip", detail="main track unpowered"),
        Check(id="D10", title="address band", status="skip", detail="no --address in 100..127"),
    )
    caps = Capabilities(
        link_identity="serial:BENCH:3",
        probed_at="2026-08-04T12:00:00Z",
        xpressnet_version="4.0",
        command_station_id=18,
        notes=(
            "z21_cv_opcodes reply bands 63 16 and 63 17 are documented (Lenz "
            "secondary summary) but not exercised on this station",
        ),
    )
    return DoctorReport(checks=checks, capabilities=caps)


def test_pom_read_none_renders_null_in_json_and_unknown_in_human_never_no():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    assert result.result["capabilities"]["pom_read"] is None
    assert "  POM read: unknown" in result.lines
    assert not any(line.strip() == "POM read: no" for line in result.lines)


def test_json_rendering_keeps_pom_read_null_through_the_real_render_pipeline():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=False)
    body = json.loads(out.getvalue())
    assert body["result"]["capabilities"]["pom_read"] is None


def test_four_check_statuses_render_four_different_labels_in_human_and_json():
    checks = (
        Check(id="D0", title="link", status="ok", detail="opened"),
        Check(id="D3", title="track power", status="fail", detail="still off after power-on"),
        Check(id="D5", title="service direct", status="skip", detail="--no-programming-track"),
        Check(id="D4", title="POM read", status="unknown", detail="no result on either channel"),
    )
    report = DoctorReport(checks=checks, capabilities=Capabilities.unknown("serial:test:0"))
    result = build_doctor(report, saved_to=None)
    labels = {line.split("]")[0] for line in result.lines if line.startswith("[")}
    assert labels == {"[OK", "[FAIL", "[SKIP", "[UNKNOWN"}
    assert [c["status"] for c in result.result["checks"]] == ["ok", "fail", "skip", "unknown"]


def test_bench_scenario_track_unpowered_no_power_on_exits_zero():
    """The exact case pinned test #4 names: a missing capability is information, not
    a failure.
    """
    report = _bench_report(powered=False)
    assert report.ok is True
    result = build_doctor(report, saved_to=None)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.result["checks"][3]["status"] == "unknown"  # D3
    assert result.result["checks"][4]["status"] == "skip"  # D4


def test_a_failed_link_check_exits_three():
    checks = (Check(id="D0", title="link", status="fail", detail="port not found"),)
    report = DoctorReport(checks=checks, capabilities=Capabilities.unknown("unknown"))
    assert report.ok is False
    result = build_doctor(report, saved_to=None)
    assert result.ok is False
    assert result.exit_code == 3


def test_human_output_ends_with_the_four_line_verdict_and_json_carries_the_same_lines():
    report = _bench_report(powered=False)
    result = build_doctor(report, saved_to=None)
    verdict = list(verdict_lines(report))
    assert len(verdict) == 4
    assert result.lines[-len(verdict):] == verdict
    assert result.result["verdict"] == verdict


def test_notes_labelling_ranges_never_exercised_appear_in_both_renderings():
    report = _bench_report(powered=False)
    note = report.capabilities.notes[0]
    assert "63 16" in note and "not exercised" in note
    result = build_doctor(report, saved_to=None)
    assert note in result.result["notes"]
    assert any(note in line for line in result.lines)
```

- [ ] **Step 6: Run and see it fail on the missing module**

Run: `uv run pytest tests/cli/test_doctor.py`

Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.cli.commands.doctor'`. The
`railctl.cli.commands` *package* already exists - Task 9 created `commands/__init__.py` once, with
its own docstring - so this task never re-creates it; only the `doctor` submodule is missing.

- [ ] **Step 7: Implement `build_doctor`**

```python
# src/railctl/cli/commands/doctor.py
"""`railctl doctor` - runs the capability probe and saves capabilities.json.

The doctor is the first command a new user runs. Its human output leads with the raw
evidence - the check table, then the tri-state capabilities, then any notes - and closes
with the four-line verdict block `railctl.station.doctor.verdict_lines` already builds:
the checks are what the probe measured, the verdict is what that means for the CLI's
other commands, and putting the takeaway last is what "ends with the verdict" (this
task's pinned assertion) means in practice.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

import typer

from railctl.cli._errors import OutputContext, run
from railctl.cli._meta import (
    DOCTOR_NO_PROGRAMMING_TRACK,
    DOCTOR_NO_SAVE,
    DOCTOR_POWER_ON,
    command_meta,
    global_option,
    help_epilog,
    typer_option,
)
from railctl.cli.config import capabilities_path
from railctl.cli.deps import link_info, merge_settings, open_station, station_info
from railctl.cli.render import want_color
from railctl.cli.result import CommandResult, tri_state
from railctl.station import Capabilities, Check, DoctorReport, exit_code_for_report, verdict_lines

if TYPE_CHECKING:
    from railctl.cli.deps import Settings
    from railctl.cli.main import CliContext

DOCTOR_SCHEMA: Final[str] = "railctl/doctor/v1"

_LABEL_WIDTH: Final[int] = len("UNKNOWN")

# (attribute, human title, kind). "bool" renders through tri_state(); "text" renders
# through _text() - the four non-boolean tri-state fields (a version string, two
# integers, a result-channel enum string) must never be handed to tri_state(), which
# only ever means yes/no/unknown for an actual bool | None.
CAPABILITY_FIELDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("xpressnet_version", "XpressNet version", "text"),
    ("command_station_id", "Command station id", "text"),
    ("pom_read", "POM read", "bool"),
    ("pom_result_channel", "POM result channel", "text"),
    ("pom_echo_zero_based", "POM echo zero-based", "bool"),
    ("loco_address_threshold", "Long-address threshold", "text"),
    ("service_direct_cv", "Service mode: direct CV", "bool"),
    ("service_ext_cv", "Service mode: extended CV", "bool"),
    ("z21_cv_opcodes", "Z21 16-bit CV opcodes", "bool"),
    ("function_groups_4_5", "Function groups 4/5 (F13-F28)", "bool"),
    ("single_function_cmd", "Single-function command (E4 F8)", "bool"),
)


def _text(value: object) -> str:
    """None reads as "unknown", never blank and never "no" - a capability nobody has
    probed yet is not evidence it is absent."""
    return "unknown" if value is None else str(value)


def _capabilities_payload(caps: Capabilities) -> dict[str, object]:
    """The raw tri-state values, untouched, for JSON - true/false/null, never the
    tri_state() word. Storing "unknown" as the JSON value would be the exact M1
    failure mode one layer up: a script could no longer tell a real gap from the
    literal string some other field might legitimately hold.
    """
    return {name: getattr(caps, name) for name, _, _ in CAPABILITY_FIELDS}


def _capability_lines(caps: Capabilities) -> list[str]:
    lines: list[str] = []
    for name, title, kind in CAPABILITY_FIELDS:
        value = getattr(caps, name)
        rendered = tri_state(value) if kind == "bool" else _text(value)
        lines.append(f"  {title}: {rendered}")
    return lines


def _check_line(check: Check) -> str:
    label = check.status.upper()
    return f"[{label:<{_LABEL_WIDTH}}] {check.id} {check.title}: {check.detail}"


def build_doctor(report: DoctorReport, *, saved_to: Path | None) -> CommandResult:
    caps = report.capabilities
    result = CommandResult(
        schema=DOCTOR_SCHEMA,
        command="doctor",
        ok=report.ok,
        exit_code=exit_code_for_report(report),
    )
    result.result["checks"] = [
        {"id": c.id, "title": c.title, "status": c.status, "detail": c.detail}
        for c in report.checks
    ]
    result.result["capabilities"] = _capabilities_payload(caps)
    result.result["notes"] = list(caps.notes)
    verdict = list(verdict_lines(report))
    result.result["verdict"] = verdict
    result.result["saved_to"] = str(saved_to) if saved_to is not None else None

    result.say("Checks:")
    for check in report.checks:
        result.say(_check_line(check))
    result.say("")
    result.say("Capabilities:")
    for line in _capability_lines(caps):
        result.say(line)
    if caps.notes:
        result.say("")
        result.say("Notes:")
        for note in caps.notes:
            result.say(f"  - {note}")
    result.say("")
    for line in verdict:
        result.say(line)
    if saved_to is not None:
        result.say("")
        result.say(f"Capabilities saved to {saved_to}")
    return result
```

- [ ] **Step 8: Run and see the rendering tests pass**

Run: `uv run pytest tests/cli/test_doctor.py`

Expected: PASS, 8 passed, 0 failed (1 from Step 1 plus 7 from Step 5).

- [ ] **Step 9: Write the failing CLI-wiring tests: flags reaching `Station.probe`, save, merge, `--no-save`, unknown identity**

Add these imports to the top of `tests/cli/test_doctor.py`'s import block:

```python
from typer.testing import CliRunner

from railctl.cli.commands import doctor
from railctl.cli.main import app as real_app
from railctl.cli.result import LinkInfo, StationInfo
```

Then append the rest at the end of the file:

```python
class _FakeStation:
    def __init__(self, report: DoctorReport) -> None:
        self._report = report
        self.calls: dict[str, object] = {}
        self.closed = False

    def probe(self, *, address=None, allow_power_on=False, use_programming_track=True):
        self.calls = {
            "address": address,
            "allow_power_on": allow_power_on,
            "use_programming_track": use_programming_track,
        }
        return self._report

    def close(self) -> None:
        self.closed = True


def _wire(monkeypatch, tmp_path, report: DoctorReport, *, identity="serial:FAKE:3"):
    """Invokes the REAL nine-command `railctl.cli.main.app` through `CliRunner` - a
    throwaway app has no callback declaring the eight global options, so `--address`
    after `doctor` would be "No such option" before `doctor_cmd` ever runs. Only
    `open_station`/`link_info`/`station_info` are monkeypatched, onto the names
    `doctor.py` imported them under, and `$HOME`/`$XDG_CONFIG_HOME` are redirected so
    `capabilities_path()` never touches the real user's config.
    """
    fake_station = _FakeStation(report)
    monkeypatch.setattr(doctor, "open_station", lambda settings, *, capabilities_path: fake_station)
    monkeypatch.setattr(
        doctor, "link_info",
        lambda station, settings: LinkInfo(identity=identity, target="serial:/dev/cu.usbmodemFAKE3"),
    )
    monkeypatch.setattr(
        doctor, "station_info",
        lambda station: StationInfo(protocol="xpressnet", protocol_version="4.0", command_station_id=18),
    )
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return real_app, fake_station


def test_power_on_and_no_programming_track_reach_station_probe(monkeypatch, tmp_path):
    report = _bench_report(powered=False)
    app, fake_station = _wire(monkeypatch, tmp_path, report)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["doctor", "--power-on", "--no-programming-track", "--no-save", "--address", "3"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert fake_station.calls == {
        "address": 3,
        "allow_power_on": True,
        "use_programming_track": False,
    }
    assert fake_station.closed is True


def test_doctor_saves_capabilities_json_merged_with_an_existing_station(monkeypatch, tmp_path):
    report = _bench_report(powered=False)
    app, _ = _wire(monkeypatch, tmp_path, report)
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    cap_path.parent.mkdir(parents=True)
    cap_path.write_text(
        json.dumps({"version": 1, "links": {"serial:OTHER:9": {"probed_at": None}}}),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    saved = json.loads(cap_path.read_text(encoding="utf-8"))
    assert set(saved["links"]) == {"serial:OTHER:9", "serial:FAKE:3"}
    assert json.loads(result.stdout)["result"]["saved_to"] == str(cap_path)


def test_no_save_touches_nothing_and_says_so_on_stderr(monkeypatch, tmp_path):
    report = _bench_report(powered=False)
    app, _ = _wire(monkeypatch, tmp_path, report)
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--no-save", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    assert not cap_path.exists()
    assert "--no-save" in result.stderr
    assert json.loads(result.stdout)["result"]["saved_to"] is None


def test_unknown_identity_writes_nothing(monkeypatch, tmp_path):
    report = _bench_report(powered=False)
    unknown_caps = Capabilities(link_identity="unknown", xpressnet_version="4.0",
                                 command_station_id=18)
    report = DoctorReport(checks=report.checks, capabilities=unknown_caps)
    app, _ = _wire(monkeypatch, tmp_path, report, identity="unknown")
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    assert not cap_path.exists()
    assert "unknown" in result.stderr
    assert json.loads(result.stdout)["result"]["saved_to"] is None
```

Dropping `obj=_Settings(...)` and invoking the real `app` is the fix for two things at once: a
throwaway app has no callback to build a `CliContext` at all (so `settings.address` was always
`None` regardless of what the test passed), and `--address`/`--format` written *after* `doctor` on
the command line only parse because `doctor_cmd` now declares all eight global options itself
(`register()`, Step 12 below) - exactly the mechanism `railctl doctor --address 3` (this task's own
M6 acceptance sentence, Step 29) depends on.

- [ ] **Step 10: Run and see it fail on the missing rows and `register`**

Run: `uv run pytest tests/cli/test_doctor.py`

Expected: FAIL - `ImportError: cannot import name 'DOCTOR_NO_PROGRAMMING_TRACK' from
'railctl.cli._meta'` - the first name in `doctor.py`'s own import statement that Step 11 has not
written yet; `command_meta` and `help_epilog` already exist on disk (Task 10), only the `doctor`-
specific names are missing.

- [ ] **Step 11: Add the `doctor` rows to `_meta.py`**

Open `src/railctl/cli/_meta.py`. It already holds one `Option`/`CommandMeta` pair per existing
command (`status`, `version`, `power`, `stop`, `drive`, `function`, `schema`), each `CommandMeta` row
keyed by a module-private `_<PATH_IN_CAPS>` constant with no `_META` suffix - `_STATUS`, `_VERSION`,
`_POWER`, `_STOP`, `_DRIVE`, `_FUNCTION`, `_SCHEMA` (ADDENDUM Part B.1, which is the ruling on this;
no command module ever imports one of these rows by name, it calls `command_meta("<path>")`
instead, which is exactly why the rows are free to stay private) - and a `COMMANDS` tuple assembling
those rows directly (Task 10's shape - `Option` has no `dest` field, `type` is the `OptionType`
literal, `CommandMeta.path` is a plain `str`, every row supplies `schema`, and `COMMANDS` is a tuple
rebuilt whole, never mutated in place). Append the following after the last existing pair, then
rebuild `COMMANDS` to insert `_DOCTOR` at the front - `doctor` leads the nine-path tree order. The
`Option` value objects (`DOCTOR_POWER_ON`, `DOCTOR_NO_PROGRAMMING_TRACK`, `DOCTOR_NO_SAVE`) keep
their public, unprefixed names - command modules do import those, so only the `CommandMeta` row
itself is private:

```python
DOCTOR_POWER_ON: Final[Option] = Option(
    name="--power-on",
    type="boolean",
    default=False,
    help="turn track power on before D4/D10 if it is currently off",
)
DOCTOR_NO_PROGRAMMING_TRACK: Final[Option] = Option(
    name="--no-programming-track",
    type="boolean",
    default=False,
    help="skip D5-D8 (need a decoder on the programming track); "
    "records their capabilities as unknown, never as false",
)
DOCTOR_NO_SAVE: Final[Option] = Option(
    name="--no-save",
    type="boolean",
    default=False,
    help="do not write ~/.config/railctl/capabilities.json",
)
_DOCTOR: Final[CommandMeta] = CommandMeta(
    path="doctor",
    help="probe the command station's capabilities and save capabilities.json",
    # matches commands/doctor.py's own DOCTOR_SCHEMA constant. _meta.py never imports a
    # command module (that would risk a circular import the other way), so the string is
    # duplicated here by convention, exactly as every other row's schema already is.
    schema="railctl/doctor/v1",
    mutates=True,  # --power-on can turn track power on
    exit_codes=(0, 3),
    options=(DOCTOR_POWER_ON, DOCTOR_NO_PROGRAMMING_TRACK, DOCTOR_NO_SAVE),
)

# before, the tuple Tasks 10 and 11 left behind:
COMMANDS: Final[tuple[CommandMeta, ...]] = (
    _STATUS, _VERSION, _POWER, _STOP, _DRIVE, _FUNCTION, _SCHEMA,
)

# after - COMMANDS is rebuilt whole, never appended to in place (ADDENDUM Part B.1); doctor
# leads the tree order:
COMMANDS: Final[tuple[CommandMeta, ...]] = (
    _DOCTOR, _STATUS, _VERSION, _POWER, _STOP, _DRIVE, _FUNCTION, _SCHEMA,
)
```

- [ ] **Step 12: Implement `capabilities` saving and `register()`**

Append to `src/railctl/cli/commands/doctor.py`. `doctor_cmd` declares all eight global options
itself, via `global_option`, because Click hands an option written *after* the subcommand name to
the subcommand, never back to `@app.callback()` - `railctl doctor --address 3` (this task's own
acceptance sentence, Step 29) would be "No such option" without this. Every one of those eleven
defaults - the eight global ones plus the three `doctor`-specific ones - is a module-level constant,
never a call inline in the signature: Ruff's B008 allowlist covers a literal `typer.Option(...)`
call, not a wrapper function like `global_option`/`typer_option`, and both this task and Task 11 run
`uv run ruff check .` at their own gate (ADDENDUM Part B.3):

```python
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")
_POWER_ON = typer_option(DOCTOR_POWER_ON)
_NO_PROGRAMMING_TRACK = typer_option(DOCTOR_NO_PROGRAMMING_TRACK)
_NO_SAVE = typer_option(DOCTOR_NO_SAVE)


def _save_capabilities(caps: Capabilities, *, no_save: bool, stderr: TextIO) -> Path | None:
    if no_save:
        print("capabilities not saved (--no-save)", file=stderr)
        return None
    path = capabilities_path(os.environ)
    if caps.save(path):
        return path
    print(
        f"capabilities not saved: link identity {caps.link_identity!r} is unknown",
        file=stderr,
    )
    return None


def register(app: typer.Typer) -> None:
    @app.command(
        "doctor",
        help=command_meta("doctor").help,
        epilog=help_epilog(command_meta("doctor")),
    )
    def doctor_cmd(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        fmt: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
        power_on: bool = _POWER_ON,
        no_programming_track: bool = _NO_PROGRAMMING_TRACK,
        no_save: bool = _NO_SAVE,
    ) -> None:
        cli_ctx: CliContext = ctx.obj
        settings = merge_settings(
            cli_ctx.settings,
            target=target,
            address=address,
            fmt=fmt,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )
        output = OutputContext(
            fmt=settings.fmt,
            color=want_color(settings.color, cli_ctx.output.stdout, os.environ),
            stdout=cli_ctx.output.stdout,
            stderr=cli_ctx.output.stderr,
        )

        def work() -> CommandResult:
            station = open_station(settings, capabilities_path=None)
            try:
                report = station.probe(
                    address=settings.address,
                    allow_power_on=power_on,
                    use_programming_track=not no_programming_track,
                )
                saved_to = _save_capabilities(
                    report.capabilities, no_save=no_save, stderr=output.stderr
                )
                result = build_doctor(report, saved_to=saved_to)
                result.link = link_info(station, settings)
                result.station = station_info(station)
                return result
            finally:
                station.close()

        run("doctor", output, work)
```

`ctx.obj` is a `CliContext(settings, output)` built by `main.py`'s callback (Task 9), never a bare
`Settings` - reading `ctx.obj.settings` and merging it with this command's own per-command globals
is what makes `railctl --address 3 doctor` and `railctl doctor --address 3` resolve to the same
`Settings.address`, regardless of which side of the subcommand name the flag was written on. The
same is true of the streams: `want_color` reads `cli_ctx.output.stdout`, and `OutputContext.stdout`/
`.stderr` are built from `cli_ctx.output.stdout`/`.stderr`, never `sys.stdout`/`sys.stderr` -
`OutputContext` exists precisely so no command body ever reaches for a module-global stream, and a
call site that read `sys.stdout` directly would reintroduce the one thing it was built to remove
(ADDENDUM Part B.5).

- [ ] **Step 13: Run and see the whole doctor test file pass**

Run: `uv run pytest tests/cli/test_doctor.py`

Expected: PASS, 12 passed, 0 failed (8 from Step 8 plus 4 from Step 9).

- [ ] **Step 14: Lint and format the doctor files**

Run: `uv run ruff check src/railctl/cli/_errors.py src/railctl/cli/_meta.py src/railctl/cli/commands tests/cli/test_doctor.py && uv run ruff format --check src/railctl/cli/_errors.py src/railctl/cli/_meta.py src/railctl/cli/commands tests/cli/test_doctor.py`

Expected: `All checks passed!` and every file already formatted. If the format check reports a diff,
run `uv run ruff format src/railctl/cli/_errors.py src/railctl/cli/_meta.py src/railctl/cli/commands
tests/cli/test_doctor.py` once, then re-run both commands.

- [ ] **Step 15: Commit the `doctor` command**

```bash
git add src/railctl/cli/_errors.py src/railctl/cli/_meta.py src/railctl/cli/commands \
        tests/cli/test_doctor.py
git commit -m "feat(cli): add the doctor command"
```

- [ ] **Step 16: Write the failing `stream_monitor` and `build_monitor` unit tests**

```python
# tests/cli/test_monitor.py
"""Pins `monitor`'s decoding, its ndjson streaming contract (contiguous sequence
numbers, always ending in a summary - even on Ctrl-C), and the split between what goes
to stdout and what goes to stderr.
"""

from __future__ import annotations

import io
import json

import pytest

from railctl.cli.commands.monitor import MONITOR_SCHEMA, build_monitor, stream_monitor
from railctl.cli.render import NdjsonStream, render
from railctl.station import StationEvent


class _EventStation:
    """A fake `Station` exposing only `events()` and `close()` - `stream_monitor` and
    `build_monitor` never touch anything else, which is exactly what lets this fake
    stay this small.
    """

    def __init__(self, events: list[StationEvent], *, interrupt_after: int | None = None) -> None:
        self._events = events
        self._interrupt_after = interrupt_after
        self.closed = False

    def events(self, *, interval: float = 0.25):
        for index, event in enumerate(self._events):
            yield event
            if self._interrupt_after is not None and (index + 1) == self._interrupt_after:
                raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


def test_stream_monitor_emits_contiguous_sequence_ending_in_a_summary():
    events = [
        StationEvent(at=1.0, name="power.on", detail="track power on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="track power off", payload={}),
    ]
    station = _EventStation(events)
    buf = io.StringIO()
    count = stream_monitor(station, ndjson=NdjsonStream(buf), limit=2)
    assert count == 2
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [line["sequence"] for line in lines] == [0, 1, 2]
    assert [line["type"] for line in lines] == ["event", "event", "summary"]
    assert lines[-1] == {
        "type": "summary", "sequence": 2, "count": 2, "complete": True, "exit_code": 0,
    }


def test_stream_monitor_writes_three_events_then_a_summary_on_keyboard_interrupt():
    """Pinned test #9: the fake station's event iterator raises KeyboardInterrupt after
    three events. `stream_monitor` must not swallow it - the interrupt still has to
    reach the caller (Step 24) - but the ndjson stream on stdout must already carry its
    ending summary line by the time it does.
    """
    events = [
        StationEvent(at=1.0, name="power.on", detail="d1", payload={}),
        StationEvent(at=2.0, name="power.off", detail="d2", payload={}),
        StationEvent(at=3.0, name="loco.emergency_stop", detail="d3", payload={}),
    ]
    station = _EventStation(events, interrupt_after=3)
    buf = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        stream_monitor(station, ndjson=NdjsonStream(buf))
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [line["type"] for line in lines] == ["event", "event", "event", "summary"]
    assert [line["sequence"] for line in lines] == [0, 1, 2, 3]
    assert lines[-1] == {
        "type": "summary", "sequence": 3, "count": 3, "complete": False, "exit_code": 9,
    }


def test_unknown_telegram_is_reported_not_dropped():
    """Pinned test #11: an unrecognised broadcast is `reply.unknown`, bytes preserved -
    never silently discarded, which is the instrument defect this project exists to
    avoid.
    """
    events = [
        StationEvent(
            at=1.0, name="reply.unknown", detail="undecoded broadcast: 63 FF FF",
            payload={"telegram": "63 FF FF"},
        ),
    ]
    # streamed=False: this is the buffered (json) contract, where build_monitor is the
    # only place these events are ever turned into lines at all.
    result = build_monitor(events, complete=True, streamed=False)
    assert result.result["events"] == [
        {
            "name": "reply.unknown",
            "detail": "undecoded broadcast: 63 FF FF",
            "payload": {"telegram": "63 FF FF"},
        }
    ]
    assert any("reply.unknown" in line for line in result.lines)
    out = io.StringIO()
    render(result, fmt="json", stdout=out, color=False)
    body = json.loads(out.getvalue())
    assert body["schema"] == MONITOR_SCHEMA
    assert body["result"]["events"][0]["payload"]["telegram"] == "63 FF FF"


def test_build_monitor_marks_an_interrupted_run_incomplete_with_exit_nine():
    result = build_monitor([], complete=False, streamed=False)
    assert result.ok is False
    assert result.exit_code == 9


def test_build_monitor_marks_a_completed_run_ok_with_exit_zero():
    result = build_monitor([], complete=True, streamed=False)
    assert result.ok is True
    assert result.exit_code == 0


def test_streamed_result_never_repeats_the_lines_the_caller_already_wrote():
    """Pins the fix for the human-mode double-print (normalisation sheet 2.22/5f):
    `_work` (Step 23) already wrote each event to stdout itself before calling
    `build_monitor(..., streamed=True)`, so the summary is all `.lines` may add - one
    per-event line here would be one extra copy on the operator's terminal.
    """
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    result = build_monitor(events, complete=True, streamed=True)
    assert not any("power.on" in line for line in result.lines)
```

- [ ] **Step 17: Run and see it fail on the missing module**

Run: `uv run pytest tests/cli/test_monitor.py`

Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.cli.commands.monitor'`

- [ ] **Step 18: Implement `build_monitor` and `stream_monitor`**

```python
# src/railctl/cli/commands/monitor.py
"""`railctl monitor` - decodes broadcasts and prints them until Ctrl-C.

Inherently a streaming command, so `ndjson` is its natural mode: `stream_monitor` writes
one `event` line per broadcast and always finishes with a `summary` line, even when the
operator interrupts it, because a consumer reading the stream must be able to tell the
run ended the same way whether it ended by running out of events or by Ctrl-C. `human`
prints the same information as it arrives; `json` buffers and renders exactly once, so a
script parsing `--format=json` never has to handle more than one value on stdout.

`monitor` is the one command in this plan allowed to write to stdout outside `render()` -
every other command's stdout goes through `render()` alone. `build_monitor`'s own
`streamed` keyword exists because of that: when the caller already wrote each event to
stdout as it arrived (`human`, and `ndjson` via `NdjsonStream` directly), the returned
`CommandResult.lines` must not repeat them, or `render()` would print every broadcast a
second time on the one surface the operator is watching.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

import typer

from railctl.cli._errors import OutputContext, report_for, run
from railctl.cli._meta import MONITOR_LIMIT, command_meta, global_option, help_epilog, typer_option
from railctl.cli.deps import merge_settings, open_station
from railctl.cli.render import NdjsonStream, want_color
from railctl.cli.result import CommandResult
from railctl.errors import RailctlError, exit_code_for

if TYPE_CHECKING:
    from railctl.cli.deps import Settings
    from railctl.cli.main import CliContext
    from railctl.station import StationEvent
    from railctl.station.facade import Station

MONITOR_SCHEMA: Final[str] = "railctl/monitor/v1"

_START_NOTICE: Final[str] = "monitoring broadcasts; press Ctrl-C to stop\n"


def _event_row(event: StationEvent) -> dict[str, object]:
    return {"name": event.name, "detail": event.detail, "payload": event.payload}


def build_monitor(seen: Sequence[StationEvent], *, complete: bool, streamed: bool) -> CommandResult:
    result = CommandResult(
        schema=MONITOR_SCHEMA,
        command="monitor",
        ok=complete,
        exit_code=0 if complete else 9,
    )
    result.result["events"] = [_event_row(event) for event in seen]
    result.result["count"] = len(seen)
    result.result["complete"] = complete
    if not seen:
        result.say("no broadcasts seen")
    elif not streamed:
        # streamed=True means the caller already wrote one line per event to stdout
        # itself (`_work`'s human path, or `stream_monitor`'s ndjson lines) - repeating
        # them here would have `render()` print every broadcast a second time.
        for event in seen:
            result.say(f"{event.name}: {event.detail}")
    if not complete:
        result.say("interrupted")
    return result


def stream_monitor(
    station: Station, *, ndjson: NdjsonStream, limit: int | None = None
) -> int:
    """Streams `station.events()` as ndjson `event` lines and always finishes with one
    `summary` line - even on Ctrl-C.

    `KeyboardInterrupt` is caught here only to record `complete=False, exit_code=9` for
    that closing line; the `finally` block writes it, and then the interrupt is
    RE-RAISED rather than swallowed. Catching it and returning normally instead would
    give stdout its summary line while leaving the caller with no way to know the run
    was cut short - and this project exists precisely to keep "the run ended early" and
    "the run ended cleanly" from becoming indistinguishable one layer up.
    """
    count = 0
    complete = False
    exit_code = 0
    try:
        for event in station.events():
            ndjson.event("event", name=event.name, detail=event.detail, payload=event.payload)
            count += 1
            if limit is not None and count >= limit:
                break
        complete = True
        return count
    except KeyboardInterrupt:
        exit_code = 9
        raise
    finally:
        ndjson.summary(count=count, complete=complete, exit_code=exit_code)
```

- [ ] **Step 19: Run and see the unit tests pass**

Run: `uv run pytest tests/cli/test_monitor.py`

Expected: PASS, 6 passed, 0 failed.

- [ ] **Step 20: Write the failing CLI-wiring tests for `monitor`**

Add these imports to the top of `tests/cli/test_monitor.py`'s import block:

```python
from typer.testing import CliRunner

from railctl.cli._errors import report_for
from railctl.cli.commands import monitor
from railctl.cli.main import app as real_app
from railctl.errors import DecoderNotRespondingError
```

Then append the rest at the end of the file:

```python
def _wire(monkeypatch, station: _EventStation):
    """Invokes the real `railctl.cli.main.app`, exactly as `tests/cli/test_doctor.py`'s
    own `_wire` does and for the same reason: a throwaway app has no callback building a
    `CliContext`, and `monitor_cmd` needs its own per-command global options (`--limit`
    plus the eight, see `commands/monitor.py`'s `register()`) to parse flags written after
    the subcommand name at all.
    """
    monkeypatch.setattr(monitor, "open_station", lambda settings, *, capabilities_path: station)
    return real_app


def test_json_format_writes_exactly_one_json_value_events_only_on_stdout(monkeypatch):
    events = [StationEvent(at=1.0, name="power.on", detail="on", payload={})]
    station = _EventStation(events)
    app = _wire(monkeypatch, station)
    runner = CliRunner()
    result = runner.invoke(app, ["monitor", "--limit", "1", "--format", "json"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)  # succeeds only if stdout holds nothing else
    assert body["result"]["count"] == 1
    assert "monitoring broadcasts" in result.stderr
    assert station.closed is True


def test_human_format_prints_each_event_as_it_arrives(monkeypatch):
    events = [
        StationEvent(at=1.0, name="power.on", detail="track power on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="track power off", payload={}),
    ]
    station = _EventStation(events)
    app = _wire(monkeypatch, station)
    runner = CliRunner()
    result = runner.invoke(app, ["monitor", "--limit", "2", "--format", "human"])
    assert result.exit_code == 0, result.stderr
    # count(), not `in` - two substring checks pass whether `_work` streams the line to
    # stdout once (the real contract) or `build_monitor` ALSO puts it in `.lines` for
    # `render()` to print a second time (normalisation sheet 2.16/4i, 2.22/5f).
    assert result.stdout.count("power.on: track power on") == 1
    assert result.stdout.count("power.off: track power off") == 1


def test_ndjson_format_end_to_end_via_the_registered_command(monkeypatch):
    events = [
        StationEvent(at=1.0, name="power.on", detail="on", payload={}),
        StationEvent(at=2.0, name="power.off", detail="off", payload={}),
    ]
    station = _EventStation(events)
    app = _wire(monkeypatch, station)
    runner = CliRunner()
    result = runner.invoke(app, ["monitor", "--limit", "2", "--format", "ndjson"])
    assert result.exit_code == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["event", "event", "summary"]
    assert lines[-1]["exit_code"] == 0
    assert station.closed is True


def test_ndjson_format_reports_a_railctlerror_with_the_same_envelope_and_exit_code_run_would(
    monkeypatch,
):
    """Pins ADDENDUM Part B.5's fix for RECHECK 1.7/9.4.82: `_run_ndjson` bypasses
    `_errors.run()` to avoid a second summary line, but a `RailctlError` raised while
    opening the station must still leave stderr and the exit code indistinguishable from
    what `run()` renders for every other command's failure - never `main()`'s catch-all
    envelope, and never exit code 1.

    Goes red if `_run_ndjson` stops catching `RailctlError` around `open_station`/
    `stream_monitor`, or writes anything other than `report_for(exc,
    command="monitor").envelope()` as one compact JSON line on stderr.
    """
    exc = DecoderNotRespondingError("CV8 produced no result over POM after 3 attempts", cv=8)

    def _raise(settings, *, capabilities_path):
        raise exc

    monkeypatch.setattr(monitor, "open_station", _raise)
    runner = CliRunner()
    result = runner.invoke(real_app, ["monitor", "--format", "ndjson"])
    report = report_for(exc, command="monitor")
    assert result.exit_code == report.exit_code
    assert json.loads(result.stderr.strip().splitlines()[-1]) == report.envelope()
```

- [ ] **Step 21: Run and see it fail on the missing rows and `register`**

Run: `uv run pytest tests/cli/test_monitor.py`

Expected: FAIL - `ImportError: cannot import name 'MONITOR_LIMIT' from 'railctl.cli._meta'`

- [ ] **Step 22: Add the `monitor` row to `_meta.py`**

Append to `src/railctl/cli/_meta.py`, after the `doctor` rows added in Step 11, then rebuild
`COMMANDS` again to insert `_MONITOR` second-to-last, immediately before `schema`. `MONITOR_LIMIT`
stays a public, unprefixed name - `commands/monitor.py` imports it directly - while the
`CommandMeta` row itself is private, exactly as `_DOCTOR` was in Step 11 (ADDENDUM Part B.1):

```python
MONITOR_LIMIT: Final[Option] = Option(
    name="--limit",
    type="integer",
    default=None,
    help="stop after N events instead of running until Ctrl-C",
)
_MONITOR: Final[CommandMeta] = CommandMeta(
    path="monitor",
    help="decode broadcasts and own traffic until Ctrl-C",
    schema="railctl/monitor/v1",  # matches commands/monitor.py's own MONITOR_SCHEMA constant
    mutates=False,
    exit_codes=(0, 9),
    options=(MONITOR_LIMIT,),
)

# before (as Step 11 left it):
COMMANDS: Final[tuple[CommandMeta, ...]] = (
    _DOCTOR, _STATUS, _VERSION, _POWER, _STOP, _DRIVE, _FUNCTION, _SCHEMA,
)

# after - monitor sits second-to-last, in the nine-path tree order:
COMMANDS: Final[tuple[CommandMeta, ...]] = (
    _DOCTOR, _STATUS, _VERSION, _POWER, _STOP, _DRIVE, _FUNCTION, _MONITOR, _SCHEMA,
)
```

- [ ] **Step 23: Implement `register()`, including the `ndjson` path's own exit handling**

Append to `src/railctl/cli/commands/monitor.py`. Like `doctor_cmd`, `monitor_cmd` declares all
eight global options itself via `global_option`, for the same reason: `railctl monitor --format
ndjson` writes `--format` after the subcommand name, which `@app.callback()` never sees. All nine
defaults - the eight global ones plus `--limit` - are module-level constants, never inline calls in
the signature, for the same B008 reason `doctor_cmd` already gave (ADDENDUM Part B.3):

```python
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")
_LIMIT = typer_option(MONITOR_LIMIT)


def register(app: typer.Typer) -> None:
    @app.command(
        "monitor",
        help=command_meta("monitor").help,
        epilog=help_epilog(command_meta("monitor")),
    )
    def monitor_cmd(
        ctx: typer.Context,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        fmt: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
        limit: int | None = _LIMIT,
    ) -> None:
        cli_ctx: CliContext = ctx.obj
        settings = merge_settings(
            cli_ctx.settings,
            target=target,
            address=address,
            fmt=fmt,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )
        output = OutputContext(
            fmt=settings.fmt,
            color=want_color(settings.color, cli_ctx.output.stdout, os.environ),
            stdout=cli_ctx.output.stdout,
            stderr=cli_ctx.output.stderr,
        )
        if output.fmt == "ndjson":
            _run_ndjson(settings, output, limit)
            return
        run("monitor", output, lambda: _work(settings, output, limit))


def _run_ndjson(settings: Settings, output: OutputContext, limit: int | None) -> None:
    """Bypasses `_errors.run()` entirely: `stream_monitor` has already written the
    ndjson `summary` line to stdout by the time it re-raises `KeyboardInterrupt`, and
    letting `run()` also render a `CommandResult` here would print a SECOND,
    sequence-reset summary line after the real one.

    That bypass is also why a `RailctlError` needs its own catch here: nothing else on
    this path calls `report_for`/`render_error`, so without it a failure in
    `open_station` or `stream_monitor` would escape to `main()`'s catch-all and print a
    different envelope, with a different exit code, than every other command's errors
    do (ADDENDUM Part B.5). The `except RailctlError` block below renders the identical
    `railctl/error/v1` object `run()` would have rendered, by hand, because `run()`
    itself is exactly what this function exists to not call.
    """
    station: Station | None = None
    try:
        station = open_station(settings, capabilities_path=None)
        print(_START_NOTICE, end="", file=output.stderr)
        ndjson = NdjsonStream(output.stdout)
        stream_monitor(station, ndjson=ndjson, limit=limit)
    except KeyboardInterrupt:
        raise typer.Exit(code=9) from None
    except RailctlError as exc:
        envelope = report_for(exc, command="monitor").envelope()
        output.stderr.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        raise typer.Exit(code=exit_code_for(exc)) from exc
    else:
        raise typer.Exit(code=0)
    finally:
        if station is not None:
            station.close()


def _work(settings: Settings, output: OutputContext, limit: int | None) -> CommandResult:
    streamed = output.fmt == "human"
    station = open_station(settings, capabilities_path=None)
    print(_START_NOTICE, end="", file=output.stderr)
    try:
        seen: list[StationEvent] = []
        try:
            for event in station.events():
                if streamed:
                    output.stdout.write(f"{event.name}: {event.detail}\n")
                seen.append(event)
                if limit is not None and len(seen) >= limit:
                    break
            return build_monitor(seen, complete=True, streamed=streamed)
        except KeyboardInterrupt:
            return build_monitor(seen, complete=False, streamed=streamed)
    finally:
        station.close()
```

`_work`'s direct write to `output.stdout` inside the loop (`human` format only) is the one place
this task departs from "only `render()` writes to stdout" - documented above under "Decisions
already made" and inside `monitor.py`'s own module docstring, for exactly the reason Task 8's
`render.py` already anticipates for `backup`/`restore`/`diff`. `streamed` is what keeps that write
from being repeated: `build_monitor(seen, complete=…, streamed=True)` leaves the per-event lines
out of `.lines`, so `render()` only ever adds the summary (and `"interrupted"` when incomplete).
The `json` path never sets `streamed`, so `build_monitor` includes the per-event lines there - they
end up in `.lines`, which the `json` renderer ignores in favour of `.result`, so no double-print is
possible on that path either way, but the enumeration stays honest either way.

- [ ] **Step 24: Run and see the whole monitor test file pass**

Run: `uv run pytest tests/cli/test_monitor.py`

Expected: PASS, 10 passed, 0 failed (6 from Step 19 plus 4 from Step 20 - the three CLI-wiring
tests plus the ndjson `RailctlError` test).

- [ ] **Step 25: Lint and format the monitor files**

Run: `uv run ruff check src/railctl/cli/_meta.py src/railctl/cli/commands tests/cli/test_monitor.py && uv run ruff format --check src/railctl/cli/_meta.py src/railctl/cli/commands tests/cli/test_monitor.py`

Expected: `All checks passed!` and every file already formatted.

- [ ] **Step 26: Commit the `monitor` command**

```bash
git add src/railctl/cli/_meta.py src/railctl/cli/commands/monitor.py tests/cli/test_monitor.py
git commit -m "feat(cli): add the monitor command"
```

- [ ] **Step 27: Register both commands in `main.py`**

Open `src/railctl/cli/main.py`. There are six command *modules*, not nine - `basics.py` (Task 9)
registers `status` and `version`; `power.py` (Task 11) registers `power` and `stop`; `throttle.py`
(Task 11) registers `drive` and `function`; `schema.py` (Task 10) registers `schema`. There is no
`drive.py`, `function.py`, `status.py`, `stop.py` or `version.py` module - importing those names
would be an `ImportError` against files this plan never creates. `main.py` already imports
`basics, power, schema, throttle` and calls each one's `register(app)`. Add `doctor` and `monitor`
to both the import line and the registration block, keeping the same alphabetical order as the
import statement itself:

```python
# before:
from railctl.cli.commands import basics, power, schema, throttle

basics.register(app)
power.register(app)
throttle.register(app)
schema.register(app)

# after:
from railctl.cli.commands import basics, doctor, monitor, power, schema, throttle

basics.register(app)
doctor.register(app)
monitor.register(app)
power.register(app)
schema.register(app)
throttle.register(app)
```

Registration order has no functional effect here: `_meta.py`'s `COMMANDS` rows are populated at
import time, independent of which `register()` call runs when, and `railctl schema`'s own command
order comes from `COMMANDS` (Part 1.14's nine-path tree order), not from this list. This block just
follows the import statement's own alphabetical order rather than inventing a second ordering to
keep in sync with it.

**The `--help` contract canary (ADDENDUM Part B.6) belongs here, not in an earlier step:** it walks
every path in `COMMANDS` through the real `app`, and `app` only carries all nine registrations once
this step's edit to `main.py` has landed - `doctor` and `monitor` included. Add this import to the
top of `tests/cli/test_monitor.py`'s import block, alongside the ones Step 20 already put there:

```python
from railctl.cli._meta import COMMANDS
```

Then append the test itself at the end of the file:

```python
@pytest.mark.parametrize("path", [meta.path for meta in COMMANDS])
def test_every_command_ships_the_three_fixed_help_sections(path: str):
    """A command registered without epilog=help_epilog(...) ships --help with no OUTPUT, no EXIT
    CODES and no EXAMPLES, and nothing else notices."""
    result = CliRunner().invoke(real_app, [path, "--help"])
    assert result.exit_code == 0
    for heading in ("OUTPUT", "EXIT CODES", "EXAMPLES"):
        assert heading in result.stdout
```

`pytest` needs `import pytest` for the parametrize decorator; add it to the top-of-file import
block if `tests/cli/test_monitor.py` does not already have it (Step 16's `from __future__ import
annotations` block already imports `pytest` for `pytest.raises`, so this is very likely a no-op).
This one test function collects as nine parametrized cases, one per registered command - it goes
red on any single path missing `epilog=help_epilog(...)`, not just on `doctor` and `monitor`'s own,
which is exactly why it belongs after every command in this plan is registered rather than inside
Task 10's, 11's or this task's own earlier steps.

Run: `uv run pytest tests/cli/test_monitor.py`

Expected: PASS, 19 passed, 0 failed (10 from Step 24 plus the 9 parametrized cases this canary
collects - `status`, `version`, `power`, `stop`, `drive`, `function` and `schema` pass already,
since Tasks 10 and 11 already attach `epilog=help_epilog(...)`; `doctor` and `monitor` pass because
Step 11's and Step 22's `@app.command(...)` decorators, rewritten above, now do the same).

```bash
git add src/railctl/cli/main.py tests/cli/test_monitor.py
git commit -m "feat(cli): register doctor and monitor, and pin the --help contract for all nine commands"
```

- [ ] **Step 28: Confirm the layering guard still passes over the two new command modules**

Run: `uv run pytest tests/test_layering.py`

Expected: PASS, 8 passed, 0 failed. `tests/test_layering.py` has eight test functions in total
(fixed count, normalisation sheet Part 4.0 - several earlier tasks' plans mis-stated it as 4 or 7).
`cli/` was already being scanned from Task 8 onward - this
step is the check that `doctor.py` and `monitor.py` did not introduce a stray "tty", CV arithmetic,
framing byte or rogue exception class. If it fails, read the offending line the guard prints - a
docstring word is the likely cause, not a real bug.

- [ ] **Step 29: Write the hardware acceptance test**

```python
# tests/hardware/test_m6_acceptance.py
"""M6 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly: uv run pytest -m hardware -s
Deselected by default (pyproject.toml addopts).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from railctl.cli.main import app

pytestmark = pytest.mark.hardware


def test_doctor_writes_capabilities_matching_probe_results(tmp_path, monkeypatch):
    """docs/probe-results.md is the source of every value pinned here. `pom_read is
    False` is the one capability D4 is allowed to set to `false` from silence alone
    (design line 866) - and it must carry a note saying so, never just the bare
    boolean, or a later `railctl cv read --mode pom` reads as a station that was
    asked and said no, rather than one that was asked and never answered.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--address", "3", "--format", "json"])
    print(f"\n{result.stdout}\n{result.stderr}")
    assert result.exit_code == 0, result.stderr

    cap_path = tmp_path / ".config" / "railctl" / "capabilities.json"
    saved = json.loads(cap_path.read_text(encoding="utf-8"))
    entry = next(iter(saved["links"].values()))

    assert entry["xpressnet_version"] == "4.0"
    assert entry["command_station_id"] == 18
    assert entry["z21_cv_opcodes"] is True
    assert entry["service_direct_cv"] is True
    assert entry["service_ext_cv"] is True
    assert entry["single_function_cmd"] is True
    assert entry["function_groups_4_5"] is True
    assert entry["pom_read"] is False
    assert any(
        "silence" in note or "no result" in note for note in entry["notes"]
    ), entry["notes"]
```

This test is never run by CI or by the coverage gate - `-m 'not hardware'` in `addopts` deselects
it, exactly like `tests/hardware/test_m4_acceptance.py`. Running it by hand right now (with the
YD7010 attached, on the programming track for the decoder used in R2/R4) is this task's own final
verification that the whole `doctor` stack agrees with the measurements `docs/probe-results.md`
already recorded - not a step whose output this plan can print in advance.

- [ ] **Step 30: Update `CHANGELOG.md`**

Open `CHANGELOG.md`. Replace the empty `## [Unreleased]` section with:

```markdown
## [Unreleased]

### Added

- `railctl doctor` - probes the command station's capabilities (POM support, service-mode
  CV encodings, function-command style, long-address threshold) and saves the result to
  `~/.config/railctl/capabilities.json`.
- `railctl status` - prints track power and the command station's status bits.
- `railctl version` - prints the XpressNet version and command station id.
- `railctl power on` / `railctl power off` - turns track power on or off.
- `railctl stop` - emergency stop, for one locomotive or the whole layout.
- `railctl drive` - sets a locomotive's speed and direction.
- `railctl function` - turns a locomotive function on or off, or toggles it.
- `railctl monitor` - decodes broadcasts and prints them until Ctrl-C.
- `railctl schema` - prints a machine-readable description of every command, for scripts
  and other tools to consume.
- Every command supports `--format human|json|ndjson` (`--json` as a shortcut for
  `--format json`), so the same output can be read by a person or parsed by a script.
```

- [ ] **Step 31: Update `README.md`**

Open `README.md`. Replace the `## Status` section's body:

```markdown
# before:
## Status

M2 scaffolding, no protocol code yet. The package currently contains the version string, the
exception tree and the exit-code map. The `railctl` console script is declared but not runnable
until the CLI arrives in M6. What the hardware actually answers is recorded in
`docs/probe-results.md`.

# after:
## Status

M6: a working CLI over a real command station - `status`, `version`, `power`, `stop`, `drive`,
`function`, `doctor` and `monitor`, plus `schema` for scripts. CV reading and writing (`cv read`,
`cv write`, `backup`, `restore`, `diff`) land in a later milestone. What the hardware actually
answers is recorded in `docs/probe-results.md`.
```

Then add, after the `## Status` section:

`````markdown
## Usage

```sh
railctl status                              # target auto-resolves; raw byte + decoded bits
railctl power on
railctl doctor --address 3                  # answers what cv read/write will need; writes
                                             # capabilities.json
railctl schema --format=json | jq '.commands[].path'
```
`````

- [ ] **Step 32: Run the whole suite**

Run: `uv run pytest`

Expected: PASS. `12 + 19 = 31` new tests in this task's two CLI files (`test_doctor.py`: 1 from
Step 1 + 7 from Step 5 + 4 from Step 9 = 12, unchanged; `test_monitor.py`: 6 from Step 16 + 4 from
Step 20 + 9 from Step 27's `--help` canary = 19 - ADDENDUM Part B.6 describes the canary and the
ndjson `RailctlError` test together as "+2" to the old `12 + 9 = 21` formula, counting the canary as
one test *function*; this plan states the number `pytest`'s own summary line will actually print,
which counts each of its nine parametrized cases separately, matching Part B.6's own worked example
for `EVENT_NAMES` a few paragraphs earlier), `1` new (deselected) hardware test, plus
`920 + Σ(tests added by tasks 1..11)` from every earlier task in this plan - do not compare against
an absolute number nobody in this plan has measured; compare against this formula and treat a
mismatch as a slip to correct in place, a *different* failing test as a real signal. `0 failed`.

- [ ] **Step 33: Check the coverage gate**

Run: `uv run pytest --cov --cov-report=term-missing`

Expected: the coverage table includes `src/railctl/cli/commands/doctor.py` and
`src/railctl/cli/commands/monitor.py`, then `Required test coverage of 90% reached.` and `0
failed`. `_run_ndjson`'s `except KeyboardInterrupt`, `except RailctlError` and `else` branches and
`_work`'s two format branches are each exercised by name in Step 20's tests (the `RailctlError`
branch by the ndjson-error test added there); `build_monitor`'s `streamed` branch is exercised by
Step 20's human-format test (`streamed=True`) and by Step 16's json-shaped unit tests
(`streamed=False`). If
the table shows a gap here, it is one of those seven branches missing a test, not a reason to lower
the gate.

- [ ] **Step 34: Lint and format the whole repository**

Run: `uv run ruff check . && uv run ruff format --check .`

Expected: `All checks passed!` and every file already formatted.

- [ ] **Step 35: Confirm the schema manifest lists all nine commands**

Run: `uv run railctl schema --format=json | jq '.commands[].path'`

Expected: nine lines, one **string** each - `path` is a plain `str` (Part 1.14), never a tuple, so
`jq` prints a quoted string per line, not a one-element array:

```
"doctor"
"status"
"version"
"power"
"stop"
"drive"
"function"
"monitor"
"schema"
```

This is `COMMANDS`' own order in `_meta.py` (the nine-path tree order), not `main.py`'s
registration order from Step 27 - the two orders differ (registration follows the import
statement's alphabetical order) and only `COMMANDS`' order is what `railctl schema` reports. If any
command is missing, its `register()` was never added to `main.py`, or its `CommandMeta` was never
added to the rebuilt `COMMANDS` tuple in `_meta.py`.

- [ ] **Step 36: Commit the milestone close-out**

```bash
git add CHANGELOG.md README.md tests/hardware/test_m6_acceptance.py
git commit -m "$(cat <<'EOF'
docs: close out M6 - changelog, readme usage, hardware acceptance test

uv run pytest: all green
uv run pytest --cov --cov-report=term-missing: >= 90%, branch coverage on
uv run ruff check . && uv run ruff format --check .: clean
uv run railctl schema --format=json | jq '.commands[].path': nine command paths
EOF
)"
```
