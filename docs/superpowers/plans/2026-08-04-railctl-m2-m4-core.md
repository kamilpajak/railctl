# railctl M2–M4 — Package Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the repository from a probe tool into the `railctl` package: a real distribution with the full exception tree, a pure X-Bus codec verified against golden byte vectors, and a transport/envelope/link stack that talks to the YD7010 — with nothing above the link layer yet.

**Architecture:** Four layers, each knowing only about the one below it — `link` over `envelope` + `transport`, with `xbus` supplying pure encode/decode functions and `errors` supplying the one exception tree. This plan builds three of them (`xbus`, `envelope`/`transport`, `link`) and the scaffolding underneath; the `station` facade and the CLI are Plan 3. The layering is not a convention: four grep tests fail the build if a protocol byte, a port name, or a piece of CV arithmetic appears outside the module that owns it.

**Tech Stack:** Python 3.11+ (developed on 3.13), `typer` as the only runtime dependency, stdlib `os` + `termios` for the serial port, hatchling for the build, uv for dependency management, pytest + hypothesis + ruff for development. macOS only.

## Global Constraints

Every task's requirements implicitly include this section.

**Language and packaging**

- Python 3.11+ (`requires-python = ">=3.11"`). CI runs 3.11, 3.12, 3.13 and 3.14; development is on 3.13.
- Runtime dependencies: **`typer` only**. Everything else is stdlib. No `pyserial`.
- Dependency management is **uv**, the same as the owner's other Python projects (e.g. AlphaLens):
  dev tooling lives in PEP 735 `[dependency-groups]`, not `[project.optional-dependencies]`;
  `uv.lock` is committed to git and is the single source of truth for resolved versions;
  `.python-version` pins the local interpreter (`3.13`) that `uv sync` provisions and reuses. There
  is no `pip` inside `.venv` - uv manages the environment directly, and every install command in
  this plan goes through `uv sync` / `uv run`, never `python -m pip`.
- Dev dependencies: `pytest`, `pytest-cov`, `hypothesis`, `ruff`, in the `dev` dependency group.
  `cosmic-ray` is already installed for the probe's mutation configs and stays, in its own
  `mutation` group (see Task 1 Step 6 for why it is not folded into `dev`).
- Platform: macOS (Darwin) only. `termios` usage is not portable and is not required to be.
- The version string lives in exactly one place: `src/railctl/__init__.py`, exposed through hatchling's `dynamic = ["version"]`.
- Console script: `railctl = "railctl.cli.main:app"`.

**The wire**

- Every command telegram sent to the YD7010 XpressNet port MUST be prefixed with `FF FE`. Without it the port is silent.
- `FF FE` (solicited) and `FF FD` (unsolicited broadcast) prefix bytes are NEVER included in the XOR checksum.
- XpressNet telegram length = `(header & 0x0F) + 2` bytes: header + N data bytes + XOR.
- LI-USB is strictly request/response: never send a second command before the first is answered or has timed out. `FakeTransport` raises if this is violated, so the rule is a mechanical check rather than a review note.
- CV numbers are **1-based in every API**. Conversion to a wire value happens in exactly one module, `xbus/cv.py`. Four conventions live there and they disagree: POM (`E6 30`) and Z21 (`23 11`) are **zero-based**; Lenz direct (`22 15`) and Lenz extended (`22 18`–`1B`) are **one-based**.
- Locomotive addresses of 128 and above need the `0xC0` prefix on the high address byte.

**Measured on the hardware, 2026-08-04 — see `docs/probe-results.md`**

- Command station: YaMoRC YD7010, XpressNet **4.0**, command station id **0x12** (the Z21 family id). Handshake `21 21 00` → `63 21 40 12`.
- Ports on the reference unit: `…41` LocoNet (silent), `…43` XpressNet, `…45` YD.Control telemetry. The tool MUST auto-detect rather than hardcode.
- Z21 CV opcodes work; the Lenz direct and extended opcodes work; single-function `E4 F8` works; function groups 4 and 5 work; speed step mode is 128; start mode is automatic.
- **POM CV read returns nothing at all** — only the interface ACK `01 04 05`. Recorded as `unknown`, never `false`: the station never said `61 82`, so `false` would assert a measurement that was not taken. No module in this plan may treat that silence as proof of anything.

**The failure mode this project keeps committing**

A capability recorded as absent because of a defect in the instrument measuring it. It happened four times during M1. A reply form the parser does not recognise is indistinguishable from no reply at all, and the layer above reads silence as "the hardware cannot do this". Three outcomes — **true**, **false**, and **unknown** — must stay distinguishable end to end, and no parser may fail closed in a way that looks like a hardware verdict.

**Process**

- Commit style: Conventional Commits (`type(scope): description`). Never mention AI assistance in a commit message, body, or list.
- Test and lint run through the existing venv: `.venv/bin/python -m pytest …`, `.venv/bin/python -m ruff check …`.
- The 292 probe tests already in `tests/` must stay green through every task in this plan.
- Git identity in this repo is already configured as `Kamil Pająk <kamilpajak@users.noreply.github.com>`.

---

## Scope note — where this plan sits

The approved spec defines milestones M1–M11. Plan 1 delivered M1: a standalone capability probe in `tools/probe/`, 292 tests, and `docs/probe-results.md` filled in from the real station. This is Plan 2.

| Plan | Milestones | Deliverable |
|---|---|---|
| 1 (done) | M1 | Probe tool + `docs/probe-results.md` measured on hardware |
| **2 (this one)** | **M2–M4** | Package scaffolding, X-Bus codec with golden vectors, transport/envelope/link |
| 3 | M5–M6 | Station facade, CLI core, `doctor`, driving commands |
| 4 | M7–M8 | ZIMO catalog, `cv read` / `cv write` |
| 5 | M9–M11 | Backup, restore, diff, sweep, 0.1.0 release |

`tools/probe/` is **not** deleted and **not** imported by `railctl`. It stays as the instrument that produced the measurements, and its tests keep running. Where its code and this plan describe the same protocol behaviour, the probe is the proven one — it ran against the station and survived property and mutation testing.

Nothing in this plan needs the physical station except the three acceptance checks explicitly marked as hardware steps.

---

## Two places where this plan departs from the spec, and why

The spec is authoritative. These are the two points where following it literally would produce something wrong, so the plan does not.

**1. `decode()`'s worked example is arithmetically wrong.** Spec line 329 gives `decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x40\x12")`. Header `0x63` declares three data bytes — `telegram_length(0x63) = (0x63 & 0x0F) + 2 = 5` — so the data is `21 40 12` and the correct result is `(0x63, b"\x21\x40\x12")`. The example drops `db0`. Task 4 implements the correct behaviour and includes a step that fixes the sentence in the design document, as its own edit with the reason recorded. This is the only task in the plan that touches the spec.

**2. `open_link` lives in `transport`, not in `link`.** Spec line 1581 writes the M4 acceptance check as `railctl.link.open_link("auto")`, but the spec's own package layout (line 121) puts `open_link` in `transport/__init__.py`, and the layering rule at line 98 says connection targets are opaque strings parsed only by `transport.open_link()`. The plan follows the layout and the rule: it is `railctl.transport.open_link`. The acceptance sentence is the thing that is wrong, and the spec is left alone here because the layout and the layering rule already say it twice.

## A note on the test counts

Every task states the exact number of tests its step should report, and the running total for the suite. Those numbers were computed by reading the test code the tasks contain, not by executing it. Treat a small disagreement as an arithmetic slip in the plan, not as a broken implementation — check *which* tests ran before chasing it. The first execution should correct the numbers in place. A large disagreement, or a difference in *which* test failed, is a real signal.

---

## File Structure

Created by this plan:

```
pyproject.toml                     rewritten: railctl package, hatchling, typer, ruff, pytest, coverage
.github/workflows/ci.yml           ruff check, ruff format --check, pytest on 3.11–3.14, Linux, no serial port

src/railctl/__init__.py            __version__ — the single source of truth
src/railctl/errors.py              the whole exception tree, EXIT_CODES, exit_code_for()
src/railctl/link.py                Link: one command in flight, request/poll/drain, retries, LinkStats

src/railctl/xbus/codec.py          encode + XOR; no I/O
src/railctl/xbus/dialect.py        the XpressNet vs Z21 split
src/railctl/xbus/address.py        loco address wire form, the 100..127 divergence band, the 0xC0 prefix
src/railctl/xbus/speed.py          128-step encoding, direction, the reserved e-stop wire value
src/railctl/xbus/cv.py             THE single choke point: four CV conventions, echo_candidates, the bounds
src/railctl/xbus/commands.py       every cmd_* encoder
src/railctl/xbus/replies.py        parse() — total, and never claims more than the header entitles it to

src/railctl/envelope/__init__.py   Frame, Kind, the Envelope protocol, EnvelopeStats
src/railctl/envelope/liusb.py      LiUsbEnvelope: FF FE / FF FD, checksum, resync past a stray prefix
src/railctl/transport/__init__.py  the Transport protocol + open_link() — the only parser of target strings
src/railctl/transport/serial_posix.py  termios; the one module allowed a file descriptor
src/railctl/transport/fake.py      FakeTransport: scripted, sequenced, chunkable, fake-clocked

tests/…                            layout decided by Task 1; vectors.py carries the golden byte tables
```

Deliberately **not** created here: `station/`, `cli/`, `catalog/`, `backup/`, `envelope/z21.py`, `transport/udp.py`. The Z21 forms are designed for — `open_link` parses and cleanly rejects a `z21:` target, and the envelope fixture is parametrised over a one-element list — so that adding them later is an addition, not a refactor.

---

## Part M2 - scaffolding, the exception tree, and CI

M2 turns this repository from "a probe tool with a dev-tooling `pyproject.toml`" into "a real
`src/railctl` hatchling package that still builds, lints and tests the probe". No protocol code is
written here: `xbus/`, `envelope/`, `transport/`, `link.py`, `station/` and `cli/` belong to later
parts of this plan and to Plans 3-5.

The spec was written as if the repository were empty. It is not: 292 probe tests, a
setuptools-editable install of `railctl-probe`, five cosmic-ray configs and a flat `tests/`
directory are already here and must all stay green. Four conflicts are resolved below, each in the
task that owns it:

| Conflict | Resolved in | Resolution |
|---|---|---|
| `name = "railctl-probe"`, no build backend, installed editable | Task 1 | rename to `railctl`, add hatchling, uninstall the old distribution and reinstall editable; verify **both** `import railctl` and `import tools.probe` |
| `fail_under = 90` with `source = ["railctl"]` on a nearly empty package | Task 1 | coverage is **configured** in `pyproject.toml` but **never** in `addopts`; the gate becomes a CI step in M3 |
| new `addopts` vs the 292 probe tests and `mutation/*.toml` | Task 1 | new `addopts` adopted as written; `mutation/*.toml` need no edit and the baseline command is re-run to prove it |
| flat `tests/` with no `__init__.py` vs the spec's `tests/{unit,station,cli,hardware}` | Task 1 | probe tests move to `tests/probe/`; every test directory gets `__init__.py`; `pythonpath = ["src", "."]` keeps `tools.probe` importable |

Test-count checkpoints, each measured on this tree, not estimated:

| After | `.venv/bin/python -m pytest` |
|---|---|
| Task 1 | `299 passed, 1 deselected` |
| Task 2 | `335 passed, 1 deselected` |
| Task 3 | `342 passed, 1 deselected` |

All three tasks are committed on one branch, `feature/m2-scaffolding`, created in Task 1 Step 0 and
merged through a pull request in Task 3. Nothing in M2 is committed directly to `main`: the CI
workflow written in Task 3 is triggered by `pull_request` as well as by pushes to `main`, and the
PR-triggered half of it is only exercised if the work actually arrives as a PR. M1 landed the same
way (PR #2).

---

### Task 1: Package skeleton, pyproject migration and the test tree

**Files:**
- Create: branch `feature/m2-scaffolding` (Step 0; no task in M2 commits to `main`)
- Create: `src/railctl/__init__.py`
- Create: `src/railctl/py.typed`
- Create: `README.md`, `LICENSE`, `CHANGELOG.md`, `.python-version` (Step 5 - the three
  repository-skeleton files from design doc line 1420 that are not code, plus `.python-version`,
  which the design doc does not mention because it predates the decision to manage dependencies
  with uv)
- Create: `uv.lock` (Step 8, written by `uv sync --group mutation`; committed in Step 14)
- Create: `tests/__init__.py`, `tests/probe/__init__.py`, `tests/unit/__init__.py`,
  `tests/station/__init__.py`, `tests/cli/__init__.py`, `tests/hardware/__init__.py`
- Create: `tests/hardware/test_marker.py`
- Modify: `pyproject.toml` (whole file replaced, lines 1-29)
- Modify: `tests/test_commands_properties.py` line 191 (after the move:
  `tests/probe/test_commands_properties.py`), inside
  `test_the_long_address_marker_follows_the_threshold`
- Modify: `tests/test_frames_properties.py` line 113 (after the move:
  `tests/probe/test_frames_properties.py`)
- Move: `tests/test_*.py` (20 files) -> `tests/probe/`
- Reformatted by `ruff format` (whitespace only, AST unchanged): `tools/probe/checks.py`,
  `tests/probe/test_checks_opcodes.py`, `tests/probe/test_checks_properties.py`,
  `tests/probe/test_frames.py`, `tests/probe/test_replies.py`,
  `tests/probe/test_replies_mutation_hardening.py`,
  `tests/probe/test_report_mutation_hardening.py`, `tests/probe/test_report_properties.py`
- Test: `tests/unit/test_version.py`

**Interfaces:**
- Consumes: nothing from an earlier task of this plan. It consumes the existing
  `tools/probe/*` package and `tests/conftest.py` (the hypothesis profiles `default`, `mutation`,
  `ci`, selected by `HYPOTHESIS_PROFILE`), neither of which changes semantically.
- Produces:
  - `railctl.__version__: str` = `"0.1.0"` in `src/railctl/__init__.py` - the single source of
    truth; `[tool.hatch.version] path` reads this file, so the distribution version and the module
    attribute can never disagree.
  - The importable package root `railctl` (installed editable from `src/`), so every later task can
    write `from railctl.<module> import ...` and `from railctl.errors import ...`.
  - The pytest layout every later task writes into: `tests/unit/`, `tests/station/`, `tests/cli/`,
    `tests/hardware/`, all packages, plus `tests/probe/` holding the frozen M1 suite.
  - The `hardware` pytest marker, registered in `pyproject.toml` and deselected by default via
    `addopts = "-q --strict-markers --strict-config -m 'not hardware'"`.
  - Ruff settings every later task is linted under: `line-length = 100`, `target-version = "py311"`,
    `select = ["E","W","F","I","B","C4","UP","S","RUF"]`, `ignore = ["E501"]`,
    per-file-ignores `"tests/**/*.py" = ["S101"]` and `"tools/**/*.py" = ["C417"]`, and
    `[tool.ruff.lint.isort] known-first-party = ["railctl"]` - the one isort section this whole
    plan has, so that `railctl` and `tests` land in the same import block from the very first
    test file that imports both.

**Why this task exists at all.** The failure this project keeps hitting is *a capability recorded as
absent because of a defect in the instrument measuring it*. A build system is part of the
instrument: if `import railctl` silently resolves to a stale editable install of a differently named
distribution, every later test measures the wrong tree and reports green. Steps 8-10 make that
impossible to miss - `test_the_installed_distribution_agrees_with_the_module` fails loudly instead
of quietly reading the wrong package.

- [ ] **Step 0: Create the branch**

The repository is on `main` with `origin` at `https://github.com/kamilpajak/railctl.git`, and M1
landed through PR #2. All three M2 tasks commit to one branch, which Task 3 pushes and opens a pull
request for.

```bash
git switch -c feature/m2-scaffolding
```

Expected: `Switched to a new branch 'feature/m2-scaffolding'`

Confirm before going further:

```bash
git rev-parse --abbrev-ref HEAD
```

Expected: `feature/m2-scaffolding`

- [ ] **Step 1: Move the probe tests and lay out the test packages**

The 20 existing probe test files sit flat in `tests/` with no `tests/__init__.py`. The spec wants
`tests/{unit,station,cli,hardware}` each carrying `__init__.py`. Adding `__init__.py` changes how
pytest's default `prepend` import mode names modules (`tests.probe.test_frames` instead of
`test_frames`), so the probe tests must live inside a package too, not next to it. They go to
`tests/probe/` - a directory named after the tool they test, which keeps it obvious that they belong
to the throwaway M1 tool and not to the railctl package.

`tests/conftest.py` does **not** move. It registers the hypothesis profiles for the whole suite and
pytest loads a `conftest.py` by path, not by module name, so the added `tests/__init__.py` does not
disturb it.

```bash
mkdir -p tests/probe tests/unit tests/station tests/cli tests/hardware
git mv tests/test_*.py tests/probe/
touch tests/__init__.py tests/probe/__init__.py tests/unit/__init__.py \
      tests/station/__init__.py tests/cli/__init__.py tests/hardware/__init__.py
```

Then the marker canary, which is the only file `tests/hardware/` gets in M2:

```python
# tests/hardware/test_marker.py
"""Canary for the `hardware` marker.

It must never run without an explicit `-m hardware`. Its job is to make the
"deselected" count in a default run non-zero, so a broken marker registration
shows up as `0 deselected` instead of as nothing at all.
"""

import pytest


@pytest.mark.hardware
def test_the_hardware_marker_selects_this_test():
    assert True
```

- [ ] **Step 2: Write the failing version test**

```python
# tests/unit/test_version.py
"""The version string has exactly one home, and pyproject must agree with it."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

import railctl

PYPROJECT = tomllib.loads(
    Path(__file__).resolve().parents[2].joinpath("pyproject.toml").read_text(encoding="utf-8")
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?$")


def test_the_package_exposes_a_semver_string():
    assert SEMVER.match(railctl.__version__)


def test_pyproject_declares_the_version_dynamic_and_points_at_the_package():
    assert PYPROJECT["project"]["dynamic"] == ["version"]
    assert "version" not in PYPROJECT["project"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/railctl/__init__.py"


def test_the_installed_distribution_agrees_with_the_module():
    """Fails while the editable install is stale. The M2 rename is the reason."""
    assert installed_version("railctl") == railctl.__version__


def test_typer_is_the_only_runtime_dependency():
    assert [d.split(">")[0].strip() for d in PYPROJECT["project"]["dependencies"]] == ["typer"]


def test_the_console_script_is_declared():
    assert PYPROJECT["project"]["scripts"] == {"railctl": "railctl.cli.main:app"}


def test_the_hardware_marker_is_registered_and_deselected_by_default():
    ini = PYPROJECT["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("hardware:") for m in ini["markers"])
    assert "-m 'not hardware'" in ini["addopts"]
    assert "--strict-markers" in ini["addopts"]
    assert "--strict-config" in ini["addopts"]


def test_coverage_is_configured_but_not_wired_into_addopts():
    """M3 turns the gate on. At M2 the package is too small for 90% to mean anything."""
    assert PYPROJECT["tool"]["coverage"]["report"]["fail_under"] == 90
    assert "--cov" not in PYPROJECT["tool"]["pytest"]["ini_options"]["addopts"]
```

- [ ] **Step 3: Run it and see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_version.py`
Expected: collection error, ending in

```
E   ModuleNotFoundError: No module named 'railctl'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.04s
```

- [ ] **Step 4: Create the package root**

```python
# src/railctl/__init__.py
"""railctl - drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet.

`__version__` is the single source of truth. `[tool.hatch.version]` in
pyproject.toml reads this file, so the distribution version and this attribute
cannot drift apart.
"""

__version__ = "0.1.0"
```

```
# src/railctl/py.typed
```

`py.typed` is an empty marker file (PEP 561). Create it with `touch src/railctl/py.typed`.

Nothing else is created under `src/railctl/` in M2. In particular there is **no** `__main__.py` and
no empty `cli/`, `xbus/`, `transport/`, `envelope/` or `station/` package: an empty package would
make `railctl.cli.main` look importable to a reader while still failing at run time, which is the
same "looks fine, is not" shape this project is trying to eliminate.

- [ ] **Step 5: Add the four non-code repository-skeleton files**

Design doc line 1420 lists five things next to `src/`: `.github/workflows/ci.yml`, `pyproject.toml`,
`CHANGELOG.md`, `README.md` and `LICENSE (MIT)`. The workflow is Task 3 and `pyproject.toml` is the
next step; the remaining three are created here. None of them exists in the repository today
(`ls` shows only `docs/`, `mutation/`, `tests/`, `tools/` and `pyproject.toml`), and `README.md` has
to exist **before** step 8 runs `uv sync`, because step 6 adds `readme = "README.md"` to
`[project]` and hatchling fails the build if the file it names is missing.

A fourth file, `.python-version`, is added here too. The design doc does not mention it - it
predates the decision to manage dependencies with uv - but it is the same kind of file as the other
three: non-code, one line, read by tooling rather than imported. It pins the interpreter `uv sync`
provisions and reuses, the same way AlphaLens pins its own.

`LICENSE` - the MIT text, verbatim, copyright holder Kamil Pajak, year 2026:

```
MIT License

Copyright (c) 2026 Kamil Pajak

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`README.md` - the description sentence is the same string as `project.description` in step 6, so a
reader and the package metadata cannot disagree:

````markdown
# railctl

Drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet.

## Install

```sh
uv sync
```

## Status

M2 scaffolding, no protocol code yet. The package currently contains the version string, the
exception tree and the exit-code map. The `railctl` console script is declared but not runnable
until the CLI arrives in M6. What the hardware actually answers is recorded in
`docs/probe-results.md`.
````

`CHANGELOG.md` - the heading and an empty `## [Unreleased]` section only. The `## [0.1.0]` entry is
deliberately **not** written here: design doc line 1595 says the release notes are written by hand at
M11, and inventing them now would describe work that has not happened.

```markdown
# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
```

`.python-version` - one line, no trailing content beyond the newline. This is the interpreter
version measured on the machine that ran Plan 1 (`.venv`'s `pyvenv.cfg` records `version_info =
3.13.13`, created by uv 0.11.6), not the `requires-python` floor - `requires-python` stays
`>=3.11` so the package still supports 3.11-3.14, and CI still runs all four:

```
3.13
```

Confirm all four landed:

```bash
ls -1 .python-version README.md LICENSE CHANGELOG.md
```

Expected, four lines and no error (`ls` sorts its arguments, and a dotfile sorts before capital
letters in the C locale):

```
.python-version
CHANGELOG.md
LICENSE
README.md
```

- [ ] **Step 6: Replace pyproject.toml**

Replace the whole file (current lines 1-29) with:

```toml
[project]
name = "railctl"
dynamic = ["version"]
description = "Drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["typer>=0.12"]

[dependency-groups]
# typer is the only runtime dependency; the serial port is stdlib os + termios.
dev = ["pytest>=8", "pytest-cov>=5", "hypothesis>=6.100,<7", "ruff>=0.5"]
# cosmic-ray is deliberately NOT in `dev`. It drags in aiohttp and gitpython,
# it is only ever run by hand against mutation/*.toml, and CI must not have to
# resolve it on four Python versions to tell us whether the codec is correct.
# `[dependency-groups]` (PEP 735) rather than `[project.optional-dependencies]`:
# these are development-only groups, never installable by someone who `pip
# install`s the published distribution, which is exactly what a group is for
# and an extra is not.
mutation = ["cosmic-ray>=8.4.0"]

[project.scripts]
railctl = "railctl.cli.main:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.version]
path = "src/railctl/__init__.py"

[tool.hatch.build.targets.wheel]
packages = ["src/railctl"]

[tool.pytest.ini_options]
testpaths = ["tests"]
# "src" makes railctl importable without a reinstall; "." keeps `import tools.probe`
# working now that the probe is no longer covered by an editable install.
pythonpath = ["src", "."]
addopts = "-q --strict-markers --strict-config -m 'not hardware'"
markers = ["hardware: needs the physical YD7010; deselected by default"]

[tool.ruff]
target-version = "py311"
line-length = 100
# "." rather than the spec's "tests": `tools` and `tests` are both top-level
# import roots in this repo, and isort classifies them as third-party without it.
# It does NOT do the same job for `railctl` - see [tool.ruff.lint.isort] below.
src = ["src", "."]
# docs/ holds the design spec, the plans and the pre-M1 scratch script. ruff
# formats fenced Python inside Markdown, and reformatting the authoritative spec
# would be a silent edit to the document the plan is checked against.
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP", "S", "RUF"]
ignore = ["E501"]

# `src` above does not make `railctl` first-party; only this does. Measured, not
# assumed - see the paragraph under this block.
[tool.ruff.lint.isort]
known-first-party = ["railctl"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101"]
# tools/probe is frozen M1 output. Its cosmic-ray baseline in docs/test-hardening.md
# was measured against this exact AST; rewriting map()+lambda into a generator
# expression to satisfy a new lint rule would invalidate a measured number for a
# cosmetic gain.
"tools/**/*.py" = ["C417"]

[tool.coverage.run]
source = ["railctl"]
branch = true
omit = ["src/railctl/transport/serial_posix.py"]

[tool.coverage.report]
fail_under = 90
```

**Why `[tool.ruff.lint.isort]` is in this file and not in a later milestone.** Measured against
ruff 0.16.1 on a tree with `src/railctl/__init__.py` and `tests/__init__.py`: with `src = ["src",
"."]` as the only setting, a test file importing `os`, `typer`, `railctl.errors` and
`tests.vectors` is sorted into three blocks and `from railctl.errors import ...` ends up in the
**same block as `import typer`** - ruff files `railctl` as third-party and only `tests` (found
under `.`) as first-party. Adding `known-first-party = ["railctl"]` puts `railctl` and `tests`
into one block, which is the layout every test file in Plans 2-5 is written with, so the setting
has to exist **before** the first test file that imports both `railctl` and `tests` is committed -
otherwise every later `ruff check --select I` re-sorts files that earlier tasks already committed
as clean, and no task owns fixing them. Task 2 of this part is already such a file: its
`import pytest`, blank line, `from railctl import errors` layout only lints clean because of this
setting.

**Conflict 2, resolved and stated.** `fail_under = 90` is configured here but `--cov` is
deliberately absent from `addopts`, and no M2 step and no M2 CI job runs coverage. At the end of M2
`src/railctl` is `__init__.py` plus `errors.py`; a 90% gate over that measures nothing and would
fail the milestone it exists to protect. The gate arrives with **M3**, as an explicit CI step
(`uv run pytest --cov --cov-report=term-missing`) once `xbus/` gives it something to measure.
It stays out of `addopts` permanently, because `mutation/*.toml` run the suite once per mutant and
would otherwise pay the coverage tax on every one of the 462 `checks.py` mutants.

**Conflict 3, resolved and stated.** The new `addopts` are adopted exactly as the spec writes them.
`--strict-config` is safe because every ini key above is a real pytest key; `--strict-markers` is
safe because the probe suite uses only `@pytest.mark.parametrize`; `-m 'not hardware'` is parsed by
pytest's own shlex split and deselects exactly `tests/hardware/test_marker.py`. `mutation/*.toml`
need **no** edit: their `module-path` values point at `tools/probe/*.py`, which do not move, and
their `test-command` is `.venv/bin/python -m pytest -q -x`, a whole-suite run that picks up the new
`addopts` automatically. Step 13 proves it.

- [ ] **Step 7: Run the test again and watch exactly one assertion still fail**

Run: `.venv/bin/python -m pytest tests/unit/test_version.py`
Expected:

```
E           importlib.metadata.PackageNotFoundError: No package metadata was found for railctl
1 failed, 6 passed in 0.02s
```

Six of the seven now pass because `pythonpath = ["src", "."]` makes `import railctl` work from the
source tree. The seventh fails because the venv still has the old `railctl-probe` distribution
installed. That is the whole point of the test: it separates "the source tree is right" from "what
is installed is right".

- [ ] **Step 8: Reinstall the distribution with uv**

There is no `pip` inside `.venv` - it was created by uv 0.11.6 and uv never installs one, so every
`python -m pip …` command in an earlier draft of this step is unrunnable as written.
`railctl_probe.egg-info/` is build metadata generated by the old setuptools editable install; it is
matched by `*.egg-info/` in `.gitignore`, so it is not tracked, contains no source, and does not
need deleting by hand - uv prunes what no longer belongs.

One command:

```bash
uv sync --group mutation
```

This resolves the dependency tree from `pyproject.toml`, writes `uv.lock` (created if absent,
updated if the resolution changed), installs `railctl` editable from `src/` the way
`.[dev]` used to, installs the `dev` group, and additionally installs the `mutation` group so
`cosmic-ray` survives - `uv sync` on its own only installs the default groups, and `mutation` is not
one of them (Task 1 Step 6's `[dependency-groups]` comment explains why it is kept out of `dev`).
uv also **prunes** packages in `.venv` that no longer belong to the resolved environment, which is
what removes the stale `railctl-probe` editable install - there is no separate uninstall step.

Expected output shape (uv's, not pip's - do not look for pip's `Successfully installed` line):

```
Resolved N packages in …ms
Prepared N packages in …s
Installed N packages in …ms
 + cosmic-ray==8.4.0
 + railctl==0.1.0 (from file:///Users/jacoren/Developer/Personal/railctl)
 + typer==0.12.… (from file://…)
 …
 - railctl-probe==0.0.0 (from file:///Users/jacoren/Developer/Personal/railctl)
```

The exact package count and version pins depend on what `uv sync` resolves on the day this step
runs, so do not match this block wording-for-wording - confirm success with the verification
commands in Step 9 instead, which check the installed distribution and its version directly rather
than parsing this output.

Confirm the stale editable install left nothing behind:

```bash
ls .venv/lib/python3.13/site-packages/ | grep -i railctl_probe
```

Expected: no output (`grep` exits 1, so this command legitimately "fails" - that is the pass
condition). If either `__editable__.railctl_probe-*.pth` or a matching
`__editable___railctl_probe_*_finder.py` is still listed, uv did not prune it; run
`uv pip uninstall --python .venv/bin/python railctl-probe` and re-check.

`uv.lock` is now a tracked file. Step 14 commits it alongside `.python-version` and everything else
this task creates - a lockfile that is not committed does not lock anything for the next person who
clones the repository.

- [ ] **Step 9: Verify both packages import**

```bash
.venv/bin/python -c "import railctl, tools.probe; from importlib.metadata import version; print(version('railctl'), railctl.__version__, tools.probe.__file__)"
```

Expected, on one line:

```
0.1.0 0.1.0 /Users/jacoren/Developer/Personal/railctl/tools/probe/__init__.py
```

`railctl` resolves through the hatchling editable hook and works from any directory.
`tools.probe` resolves because the repository root is the current working directory; inside pytest
it resolves through `pythonpath = ["src", "."]`. The probe is deliberately **not** shipped:
`[tool.hatch.build.targets.wheel] packages = ["src/railctl"]` keeps `tools/` out of the wheel.

The console script is installed but not yet runnable - `.venv/bin/railctl` exits with
`ModuleNotFoundError: No module named 'railctl.cli'` until M6 writes `railctl/cli/main.py`. That is
expected and is why `test_the_console_script_is_declared` asserts the *declaration* and never
executes the script.

- [ ] **Step 10: Run the whole suite**

Run: `.venv/bin/python -m pytest`
Expected: `299 passed, 1 deselected in 4.4s`

292 probe tests + 7 version tests, with `tests/hardware/test_marker.py` deselected. Then confirm the
marker override works in the other direction:

Run: `.venv/bin/python -m pytest -m hardware`
Expected: `1 passed, 299 deselected in 0.05s`

- [ ] **Step 11: Fix the two lint findings the new ruff rules expose**

The wider `select` list finds exactly two real problems, both `RUF059` unused-unpacked-variable in
probe property tests. They are in test files, which cosmic-ray never mutates, so renaming to a
throwaway name changes no measured number. Confirm the exact locations first rather than trusting
the line numbers below:

```bash
.venv/bin/python -m ruff check . --select RUF059
```

Expected: two findings, at `tests/probe/test_commands_properties.py:191:11` and
`tests/probe/test_frames_properties.py:113:5`.

`tests/probe/test_commands_properties.py` line 191, the second line of
`test_the_long_address_marker_follows_the_threshold`:

```python
    high, _low = commands.loco_address_bytes(address)
```

**Do not touch line 185.** It is a character-for-character identical line
(`    high, low = commands.loco_address_bytes(address)`) inside
`test_a_locomotive_address_survives_the_round_trip`, where the very next line reads
`assert decode_address(high, low) == address`. Renaming `low` there produces
`NameError: name 'low' is not defined` and leaves the real RUF059 finding in place. Editing by
line number, or by grepping for the line text and taking the first hit, both land on the wrong one.
Line 191 is stable before and after `ruff format`.

`tests/probe/test_frames_properties.py` line 113:

```python
    _frames, rest = split_frames(buffer)
```

The other two findings, `C417` in `tools/probe/checks.py` lines 461 and 470, are suppressed by the
`"tools/**/*.py" = ["C417"]` per-file-ignore added in step 6 rather than rewritten. Reason: the
recorded cosmic-ray baseline for `checks.py` in `docs/test-hardening.md` (462 mutants, 66.9% killed,
85.1% adjusted) was measured against that exact AST.

- [ ] **Step 12: Format the tree and prove the probe's AST is unchanged**

Run: `.venv/bin/python -m ruff format .`
Expected: `8 files reformatted, 33 files left unchanged`

39 Python files in total: 30 exist today, and this task adds nine (`src/railctl/__init__.py`, six
`__init__.py` files under `tests/`, `tests/hardware/test_marker.py`, `tests/unit/test_version.py`).
`src/railctl/py.typed` is not a `.py` file, `LICENSE` has no extension, and `docs/` is excluded by
`extend-exclude`, so none of them is counted.

The other two are `README.md` and `CHANGELOG.md`. Measured on ruff 0.16.1: this version formats
Markdown as well as Python, so both files are counted and left unchanged, which is why the total
reads 33 rather than the 31 the Python-file arithmetic alone gives. A different ruff version may
report 31. What matters is the eight reformatted files below, not the total.

The eight are `tools/probe/checks.py` and seven files under `tests/probe/`; the repo has never been
run through `ruff format`, and CI runs `ruff format --check`, so this has to happen once.

Reformatting a cosmic-ray target needs a justification, and it is checkable rather than assumed -
`ruff format` is AST-preserving, so the mutant count and the operator list for `checks.py` do not
change; only line numbers shift. `docs/test-hardening.md` already states that comparing survivor
lists by line number is wrong after any edit and that the comparison key is the operator. Prove it:

```bash
.venv/bin/python - <<'PY'
import ast, pathlib, subprocess
new = pathlib.Path("tools/probe/checks.py").read_text()
old = subprocess.run(["git", "show", "HEAD:tools/probe/checks.py"],
                     capture_output=True, text=True, check=True).stdout
print("AST identical after ruff format:", ast.dump(ast.parse(new)) == ast.dump(ast.parse(old)))
PY
```

Expected: `AST identical after ruff format: True`

Then: `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .`
Expected: `All checks passed!` followed by `41 files already formatted` (39 Python files plus the
two Markdown files ruff 0.16.1 also formats)

- [ ] **Step 13: Prove the mutation runner still works under the new addopts**

`mutation/frames.toml`, `commands.toml`, `replies.toml`, `report.toml` and `checks.toml` all run
`test-command = ".venv/bin/python -m pytest -q -x"` from the repository root. Run that command
verbatim:

```bash
.venv/bin/python -m pytest -q -x; echo $?
```

Expected: **progress dots and nothing else, then `0`.** There is no `299 passed` line and no
summary at all. The `-q` is doubled - once from `addopts`, once from the command line - which
pytest accepts and which takes the quiet level to 2, and at that level pytest suppresses the summary
line entirely. `grep -c passed` over this output returns `0`. That is the correct result, not a
broken mutation runner: cosmic-ray only reads the exit status, which is why the doubled `-q` never
mattered to it. Do **not** start editing `mutation/*.toml` because no count appeared.

To see the count, drop the command-line `-q` and let the single one from `addopts` stand:

```bash
.venv/bin/python -m pytest -x
```

Expected: `299 passed, 1 deselected in 4.4s`.

Nothing in the five configs needs editing: no `module-path` moved and no config names a test path.

Sanity-check the hypothesis profile the mutation configs rely on, still selected by environment
variable through the unchanged `tests/conftest.py`:

```bash
HYPOTHESIS_PROFILE=mutation .venv/bin/python -m pytest -q -x; echo $?
```

Expected: again progress dots only, then `0`, in roughly 2 s (fewer examples, derandomised). The
count for this profile comes from the same command without the second `-q`:

```bash
HYPOTHESIS_PROFILE=mutation .venv/bin/python -m pytest -x
```

Expected: `299 passed, 1 deselected` in roughly 2 s.

- [ ] **Step 14: Commit**

Confirm the branch first, so the commit cannot land on `main`:

```bash
git rev-parse --abbrev-ref HEAD
```

Expected: `feature/m2-scaffolding`

`git add -A` picks up every path this task created or changed, including the two uv files that
have no earlier mention in the design doc: `uv.lock` (written by Step 8's `uv sync`) and
`.python-version` (written by Step 5). Confirm both are staged before committing:

```bash
git add -A
git status --porcelain | grep -E "^A  (uv\.lock|\.python-version)$"
```

Expected: two lines, `A  .python-version` and `A  uv.lock`.

```bash
git commit -m "build(pkg): migrate to the railctl hatchling package and the tests/ package tree"
```

---

### Task 2: The exception tree and exit codes

**Files:**
- Create: `src/railctl/errors.py`
- Test: `tests/unit/test_exit_codes.py`

**Interfaces:**
- Consumes from Task 1: the importable package root `railctl` and the `tests/unit/` package.
- Produces - the complete list of names every later task in Plans 2-5 imports from
  `railctl.errors`:
  - `RailctlError(Exception)` with `__init__(self, message: str, *, hint: str | None = None) -> None`
    and attribute `hint: str | None`.
  - `TransportError(RailctlError)`; `PortNotFound`, `AmbiguousPort`, `PortBusy`, `PortConfigError`,
    `PortNotOpen`, `PortNotXpressNet`, all `(TransportError)`.
  - `ProtocolError(RailctlError)`; `XBusEncodeError(ProtocolError)`, `XBusDecodeError(ProtocolError)`,
    `XBusChecksumError(XBusDecodeError)`, `LinkProtocolError(ProtocolError)`.
  - `LinkTimeout(RailctlError)`, `UnsupportedCommandError(RailctlError)`,
    `UnsupportedFeatureError(RailctlError)`.
  - `StationError(RailctlError)`; `TrackPowerError(StationError)`;
    `ProgrammingError(StationError)` with
    `__init__(self, message: str, *, hint: str | None = None, cv: int | None = None) -> None` and
    attribute `cv: int | None`.
  - `DecoderNoAckError`, `ShortCircuitError`, `StationBusyError`, `DecoderNotRespondingError`,
    `CvVerifyError`, `CvOutOfRangeError`, `PomReadUnsupportedError`, `IndexPageRequiredError`, all
    `(ProgrammingError)` and all accepting the `cv` keyword.
  - `EXIT_CODES: Final[dict[type[RailctlError], int]]`
  - `UNMAPPED_EXIT_CODE: Final[int]` = `1`
  - `exit_code_for(exc: BaseException) -> int`
  - One later addition, named here so the two sides agree: Task 4 of this plan appends
    `XBusIncompleteError(XBusDecodeError)` to this same file, with the same
    `(message: str, *, hint: str | None = None)` signature and **no** row of its own in
    `EXIT_CODES`. This task does not create it - `xbus/codec.py` does not exist yet, and the
    class only means something once `decode` can raise it. Every test in this task stays green
    when it lands: it resolves through `XBusDecodeError` to `ProtocolError`'s code 4, so
    `test_every_class_in_the_tree_resolves_to_a_code_above_one` and
    `test_errors_is_the_only_module_defining_exception_types` both still pass.

**Why this is one module.** The spec says nothing else in the package defines an exception type.
That is not tidiness: Task 3's rule 3 grep guard can only check the rule mechanically if there is
exactly one file to exempt. Every `class …Error` outside `src/railctl/errors.py` is a layering
violation the guard reports.

**Where this task meets the defining failure mode.** Three outcomes must stay distinguishable:
`LinkTimeout` (the station said *nothing* - unknown), `UnsupportedCommandError` (the station
answered `61 82` - a real "no"), and `UnsupportedFeatureError` (*we* decided it is out of scope -
never measured). `docs/probe-results.md` records the R1 POM read as `unknown`, **not** `false`,
for precisely this reason: the station acknowledged with `01 04 05` and then said nothing at all -
no `63 14`, no `61 13`, no `61 82`. Exit codes 5, 6 and 7 keep those three answers apart in a
script that only sees `$?`, and `test_silence_a_refusal_and_out_of_scope_are_three_different_exit_codes`
below is the test that stops anyone collapsing them.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_exit_codes.py
"""One test per documented exit-code row, plus the whole-tree invariants."""

from __future__ import annotations

import inspect

import pytest

from railctl import errors
from railctl.errors import (
    EXIT_CODES,
    UNMAPPED_EXIT_CODE,
    AmbiguousPort,
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


def _tree(root: type[RailctlError] = RailctlError) -> set[type[RailctlError]]:
    found = {root}
    for sub in root.__subclasses__():
        found |= _tree(sub)
    return found


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
    ],
)
def test_every_documented_exit_code_row(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code


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
    ],
)
def test_subclasses_without_their_own_row_inherit_the_parent_code(exc: RailctlError, code: int):
    assert exit_code_for(exc) == code


def test_every_class_in_the_tree_resolves_to_a_code_above_one():
    """Builds each class with k.__new__(k), not k("x").

    exit_code_for reads only type(exc).__mro__ and never touches instance state, so an
    uninitialised instance is safe here. This removes the coupling to constructor
    signatures: a future exception with a required keyword argument would otherwise raise
    TypeError inside this comprehension, and the failure would read as unrelated. Plain
    object.__new__(k) does not work here: BaseException defines its own __new__, and calling
    object.__new__ directly on a class that inherits it is refused as unsafe.
    """
    unresolved = sorted(
        k.__name__ for k in _tree() if exit_code_for(k.__new__(k)) == UNMAPPED_EXIT_CODE
    )
    assert unresolved == []


def test_no_entry_in_the_map_is_orphaned():
    assert set(EXIT_CODES) <= _tree()


def test_the_map_has_no_duplicate_codes():
    codes = list(EXIT_CODES.values())
    assert len(codes) == len(set(codes))


def test_nothing_maps_to_the_unmapped_code():
    assert UNMAPPED_EXIT_CODE not in EXIT_CODES.values()


def test_an_exception_from_outside_the_tree_gets_the_unmapped_code():
    assert exit_code_for(RuntimeError("boom")) == UNMAPPED_EXIT_CODE


def test_silence_a_refusal_and_out_of_scope_are_three_different_exit_codes():
    """M1's defining failure was silence read as "no". These three must never collapse.

    docs/probe-results.md records the POM read as unknown rather than false
    because the station answered 01 04 05 and then nothing - not 61 82. A caller
    reading only $? has to be able to tell those apart.
    """
    silence = exit_code_for(LinkTimeout("no reply in 5.0 s"))
    refusal = exit_code_for(UnsupportedCommandError("station answered 61 82"))
    out_of_scope = exit_code_for(UnsupportedFeatureError("consists are out of scope"))
    assert len({silence, refusal, out_of_scope}) == 3
    assert exit_code_for(RailctlError("x")) not in {silence, refusal, out_of_scope}


def test_the_base_carries_an_optional_hint():
    assert RailctlError("boom").hint is None
    assert RailctlError("boom", hint="try doctor").hint == "try doctor"
    assert str(RailctlError("boom", hint="try doctor")) == "boom"


def test_a_programming_error_carries_the_human_cv_number():
    assert ProgrammingError("bad").cv is None
    assert CvVerifyError("mismatch", cv=8, hint="re-read").cv == 8
    assert CvVerifyError("mismatch", cv=8).hint is None


def test_errors_is_the_only_module_defining_exception_types():
    """Only sees classes reachable through __subclasses__(), which only finds imported classes.

    A rogue exception class in a module nobody imports is invisible to this test.
    tests/test_layering.py RULE_3 is the other half: a text scan that catches an exception
    class outside errors.py whether or not anything ever imports it.
    """
    classes = [
        name
        for name, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, RailctlError)
    ]
    assert {obj.__module__ for obj in _tree()} == {"railctl.errors"}
    assert len(classes) == len(_tree())
```

- [ ] **Step 2: Run it and see it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_exit_codes.py`
Expected: collection error, ending in

```
    from railctl import errors
E   ImportError: cannot import name 'errors' from 'railctl' (/Users/jacoren/Developer/Personal/railctl/src/railctl/__init__.py)
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.04s
```

- [ ] **Step 3: Write the module**

```python
# src/railctl/errors.py
"""The whole railctl exception tree, the exit-code map, and `exit_code_for`.

Nothing else in the package defines an exception type. One module means one
place to look when a caller asks "what can this raise", and it is what makes
tests/test_layering.py able to check that rule mechanically.

The distinction this project exists to preserve is between three answers:

* `LinkTimeout`             - the station said **nothing**. Unknown, not "no".
* `UnsupportedCommandError` - the station said **no** (`61 82`). A real answer.
* `UnsupportedFeatureError` - **we** decided it is out of scope. Never measured.

They are three classes with three exit codes (5, 6, 7) because collapsing them
is exactly how milestone M1 recorded four capabilities as absent when the
instrument, not the hardware, was at fault.

These sixteen exit codes are a versioned public contract. Within a major version
no code may be renumbered, repurposed, or retired; a new error class claims an
unused code above 20 instead of reusing one of these. A future JSON envelope (M5
and later) can carry a stable machine-readable `error.code` string alongside the
process exit status, and that is where new domain detail belongs, not in a new
exit code.
"""

from __future__ import annotations

from typing import Final


class RailctlError(Exception):
    """Base for everything this package raises on purpose."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class TransportError(RailctlError):
    """Port vanished, write failed, or the LI reported an interface error."""


class PortNotFound(TransportError):
    """No candidate port matched the requested target."""


class AmbiguousPort(TransportError):
    """More than one port matched and none was preferred."""


class PortBusy(TransportError):
    """The port exists but another process holds it."""


class PortConfigError(TransportError):
    """The line settings were rejected."""


class PortNotOpen(TransportError):
    """A read or write was attempted before open()."""


class PortNotXpressNet(TransportError):
    """The port opened but the 21 21 00 handshake produced no 63 21 reply."""


class ProtocolError(RailctlError):
    """Well-framed but unparseable or unexpected telegram."""


class XBusEncodeError(ProtocolError):
    """A telegram could not be built from the given arguments."""


class XBusDecodeError(ProtocolError):
    """A telegram could not be decoded."""


class XBusChecksumError(XBusDecodeError):
    """The trailing XOR byte does not match the telegram body."""


class LinkProtocolError(ProtocolError):
    """The station rejected the same telegram twice."""


class LinkTimeout(RailctlError):
    """No reply arrived within the budget. Silence - never a negative answer."""


class UnsupportedCommandError(RailctlError):
    """The station answered 61 82: it understood, and it refuses."""


class UnsupportedFeatureError(RailctlError):
    """Outside this tool's declared scope (consists, unprobed F13+)."""


class StationError(RailctlError):
    """Facade-level base. Has no row in EXIT_CODES on purpose; it resolves to the base 9."""


class TrackPowerError(StationError):
    """Track power is off, or in the wrong state for this operation."""


class ProgrammingError(StationError):
    """Base for CV operations. Carries the human (1-based) CV number when known."""

    def __init__(self, message: str, *, hint: str | None = None, cv: int | None = None) -> None:
        super().__init__(message, hint=hint)
        self.cv = cv


class DecoderNoAckError(ProgrammingError):
    """The station reported 61 13: no acknowledgement from the decoder."""


class ShortCircuitError(ProgrammingError):
    """The station reported a short on the programming or main track."""


class StationBusyError(ProgrammingError):
    """The station reported 61 1F: a programming operation is already running."""


class DecoderNotRespondingError(ProgrammingError):
    """Nothing came back at all - neither a value nor a no-ack."""


class CvVerifyError(ProgrammingError):
    """A write completed but the read-back value differs."""


class CvOutOfRangeError(ProgrammingError):
    """The CV number is outside the bound the selected mode supports."""


class PomReadUnsupportedError(ProgrammingError):
    """POM reading is recorded as unavailable for this station."""


class IndexPageRequiredError(ProgrammingError):
    """The CV lives behind an index page that could not be selected."""


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
}

UNMAPPED_EXIT_CODE: Final[int] = 1


def exit_code_for(exc: BaseException) -> int:
    """Most specific mapped exit code for `exc`, or 1 when nothing matches.

    Walks `type(exc).__mro__`, so a new subclass inherits its parent's code
    until it is given one of its own. `StationError` has no row and resolves to
    the base 9 on purpose, exactly as the exit-code table states.
    """
    for klass in type(exc).__mro__:
        code = EXIT_CODES.get(klass)  # type: ignore[arg-type]
        if code is not None:
            return code
    return UNMAPPED_EXIT_CODE
```

Note on codes 0, 2 and 8: they are never in `EXIT_CODES`. 0 is success, 2 is a Typer usage error or
a plain `ValueError` from the facade, and 8 is partial success - all three are decided by
`cli/_errors.py` in M6, which is the single place `EXIT_CODES` is applied. Argument validation in
this package raises plain `ValueError`, never a `RailctlError`.

- [ ] **Step 4: Run the test and see it pass**

Run: `.venv/bin/python -m pytest tests/unit/test_exit_codes.py`
Expected: `36 passed in 0.02s`

Then the whole suite: `.venv/bin/python -m pytest`
Expected: `335 passed, 1 deselected in 4.4s`

- [ ] **Step 5: Lint**

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .`
Expected: `All checks passed!` then `43 files already formatted` (Task 1's 41 plus the two files
this task adds)

- [ ] **Step 6: Commit**

```bash
git add src/railctl/errors.py tests/unit/test_exit_codes.py
git commit -m "feat(errors): add the exception tree, EXIT_CODES and exit_code_for"
```

---

### Task 3: Layering guards and the CI workflow

**Files:**
- Create: `tests/test_layering.py`
- Create: `.github/workflows/ci.yml`
- Create then delete, never committed: `src/railctl/_layering_canary.py` (Step 5, removed in Step 7 -
  the planted violation that makes the rule-3 and rule-4 guards fail against the real package once)
- Pull request: branch `feature/m2-scaffolding` -> `main`, opened in Step 11

**Interfaces:**
- Consumes from Task 1: the `src/railctl` package root and the `tests/` package tree;
  `pyproject.toml` with the `dev` extra, the ruff configuration and `addopts`.
- Consumes from Task 2: `src/railctl/errors.py` - the one file rule 3 exempts.
- Produces:
  - `tests/test_layering.py` with module-level constants later parts of this plan extend rather than
    rewrite: `REPO_ROOT: Path`, `PACKAGE: Path`, `RULE_1_FORBIDDEN: tuple[str, ...]`,
    `RULE_1_PATTERNS: tuple[re.Pattern[str], ...]`,
    `RULE_2_PATTERNS: tuple[re.Pattern[str], ...]`, `RULE_3_PATTERN: re.Pattern[str]`,
    `RULE_4_PATTERNS: tuple[re.Pattern[str], ...]`, and the helpers
    `_python_files(*relative: str) -> list[Path]`,
    `_package_files(exclude: tuple[str, ...] = ()) -> list[Path]` and
    `_offenders(files: list[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]`.
  - `.github/workflows/ci.yml`, job `test`, matrix `python-version: ["3.11","3.12","3.13","3.14"]`
    on `ubuntu-latest`.

**The four rules, and how each is checked.** Rules 1, 2 and 4 are rules 1, 2 and 4 of the design
document's layering table (design doc lines 95, 96 and 98), token for token. The row labelled
"Rule 3" below is **not** the layering table's third rule - it is the separate requirement stated in
the `errors.py` section:

| Rule | Scanned | Forbidden |
|---|---|---|
| 1 (design doc line 95) | `station/`, `cli/` | `ff fe`, `ff fd`, `\xff\xfe`, `\xff\xfd`, `tty` (word-boundary), `cu.usbmodem`, `baud`, `termios`, `socket` (case-insensitive) |
| 2 (design doc line 96) | `station/`, `cli/`, `xbus/commands.py` | `cv - 1`, `cv + 1`, `% 256`, `>> 8`, `<< 8` - CV arithmetic belongs only in `xbus/cv.py` |
| 3 (design doc line 135, the `errors.py` section - **not** the layering table) | the whole package except `errors.py` | any `class …Error` / `…Exception` / `…Timeout` definition |
| 4 (design doc line 98) | the whole package except `transport/` | `/dev/`, `usbmodem` - connection targets are opaque strings |

The design doc's own layering rule 3, "every layer raises only from its own part of the exception
tree" (line 97), is enforced by review plus `test_exit_codes.py` and cannot be checked mechanically
until `station/` and `cli/` exist; it is not covered by M2. What the row above checks instead is
line 135, "Nothing else defines an exception type" - a necessary condition for the layering rule,
not the layering rule itself. Do not read this table as four mechanised layering rules.

They are text scans, not import checks, on purpose: an import check only fires once a module is
imported, and the rules must hold for code no test exercises.

**Why the guard is tested against a planted violation, and against the real tree.** At the end of M2
there is no `station/`, `cli/` or `xbus/`, so rules 1 and 2 scan zero files and pass trivially - and
a guard that can only pass is not a guard, it is the same blind instrument that made M1 record four
capabilities as absent (`docs/probe-results.md` lines 129-134). Three separate defences, because
each one covers a different way of being blind:

1. `_offenders` is proved against a `tmp_path` file that *does* violate the rules (steps 1-4). This
   shows the matcher can see a violation in a file it is handed. It shows nothing about which files
   are handed to it.
2. A canary file is planted **inside the real package** and the rule tests are watched failing
   against it (steps 5-7). Rules 3 and 4 fail; rules 1 and 2 do not, because they scan
   `station/` and `cli/`, which do not exist. This shows rules 3 and 4 scan `src/railctl` itself,
   not just a temporary directory.
3. `test_the_rule_1_and_2_targets_are_scanned_once_they_exist` (step 6) ties rules 1 and 2 to the
   tree: for each of `station`, `cli` and `xbus/commands.py`, either the path does not exist yet, or
   the scanner found files in it. It passes today because none of them exists. The moment Plan 3
   creates `station/` or `cli/`, or this plan creates `xbus/commands.py`, the two guards start
   measuring something, and if the path exists while the scanner still returns nothing this test
   fails and names the path. That is what turns rules 1 and 2 from decoration into measurement.

   One residual gap, stated rather than hidden: this test cannot catch a **rename**. If Plan 3
   calls the facade package `facade/` instead of `station/`, or puts the command builders in
   `xbus/build.py` instead of `xbus/commands.py`, then `PACKAGE / "station"` still does not exist,
   the test still passes, and rules 1 and 2 still scan nothing. No text scan can guess a name nobody
   has chosen yet. The obligation therefore belongs to Plan 3: whichever task creates the facade
   package or the command builders must update `_python_files(...)` in the two rule tests in the
   same commit, and prove it by planting a canary in the new directory exactly as step 5 does here.
   The docstring of `test_the_rule_1_and_2_targets_are_scanned_once_they_exist` carries that
   instruction, so it is read by whoever next touches the file rather than left to memory.

The two whole-package rules additionally assert their own file list is non-empty.

- [ ] **Step 1: Write the failing guard self-test**

Write the file with `_offenders` stubbed out to return nothing - which is exactly the blindness the
self-test exists to detect.

```python
# tests/test_layering.py
"""Mechanical guards for the four layering rules in the design document.

They are text scans, not import checks: an import check only fires once a module
is imported, and these rules must hold for code no test exercises.

Being line-oriented text scans, they cannot see a violation split across two lines,
or one assembled by string concatenation. Rule 2's regexes additionally match only
the literal spellings `cv - 1`, `% 256`, `>> 8`, and `<< 8`; the same arithmetic
under a different variable name passes. They narrow where a violation can hide, not
prove one is absent.

Every guard is written so it cannot pass by finding nothing. `_offenders` is
proved against a planted violation, and the whole-package rules assert that the
file list they scanned is non-empty. A guard that silently scans zero files is
the defect this project keeps hitting: an instrument that reports "clean" when
it is merely blind.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "railctl"

RULE_1_FORBIDDEN = (
    "ff fe",
    "ff fd",
    r"\xff\xfe",
    r"\xff\xfd",
    "cu.usbmodem",
    "baud",
    "termios",
    "socket",
)

# `tty` is deliberately NOT in the tuple above: as a bare substring it also matches
# `sys.stdout.isatty()`, which the CLI output contract requires (colour and progress
# only when that stream is a TTY, stdout and stderr tested separately), and `pretty`.
# A guard that fires on correct M6 code gets weakened or deleted, so it is anchored
# instead. `\btty` still catches `/dev/ttyUSB0` and `ttys001` - `/` and start-of-word
# are word boundaries - while `isatty` and `pretty` have no boundary before `tty`.
RULE_1_PATTERNS = (
    *(re.compile(re.escape(token), re.IGNORECASE) for token in RULE_1_FORBIDDEN),
    re.compile(r"\btty", re.IGNORECASE),
)

RULE_2_PATTERNS = (
    re.compile(r"\bcv\s*[-+]\s*1\b"),
    re.compile(r"%\s*256"),
    re.compile(r">>\s*8"),
    re.compile(r"<<\s*8"),
)

RULE_3_PATTERN = re.compile(r"^\s*class\s+\w*(?:Error|Exception|Timeout)\b", re.MULTILINE)

RULE_4_PATTERNS = (re.compile(r"/dev/"), re.compile(r"usbmodem"))


def _python_files(*relative: str) -> list[Path]:
    found: list[Path] = []
    for rel in relative:
        target = PACKAGE / rel
        if target.is_dir():
            found.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            found.append(target)
    return found


def _package_files(exclude: tuple[str, ...] = ()) -> list[Path]:
    excluded = {PACKAGE / name for name in exclude}
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if not any(path == item or item in path.parents for item in excluded)
    ]


def _offenders(files: list[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    return []


def test_the_scanner_reports_a_planted_violation(tmp_path: Path):
    planted = tmp_path / "leaky.py"
    planted.write_text(
        'PORT = "/dev/ttyUSB0"\n'
        'NAME = "cu.usbmodem7010A00011943"\n'
        "RAW = cv - 1\n"
        "COLOUR = sys.stdout.isatty()\n",
        encoding="utf-8",
    )
    assert len(_offenders([planted], RULE_4_PATTERNS)) == 2
    assert len(_offenders([planted], RULE_2_PATTERNS)) == 1
    assert len(_offenders([planted], (RULE_3_PATTERN,))) == 0
    # ttyUSB0 and cu.usbmodem are hits; isatty() is not. The CLI output contract
    # requires isatty(), so rule 1 must never fire on it.
    assert len(_offenders([planted], RULE_1_PATTERNS)) == 2


def test_the_scanner_reports_a_planted_exception_class(tmp_path: Path):
    planted = tmp_path / "rogue.py"
    planted.write_text("class RogueError(Exception):\n    pass\n", encoding="utf-8")
    assert len(_offenders([planted], (RULE_3_PATTERN,))) == 1
```

- [ ] **Step 2: Run it and see it fail**

Run: `.venv/bin/python -m pytest tests/test_layering.py`
Expected: two failures, the first reading

```
>       assert len(_offenders([planted], RULE_4_PATTERNS)) == 2
E       AssertionError: assert 0 == 2
E        +  where 0 = len([])
2 failed in 0.05s
```

- [ ] **Step 3: Implement the scanner**

Replace the `_offenders` stub with:

```python
def _offenders(files: list[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    hits: list[str] = []
    for path in files:
        label = os.path.relpath(path, REPO_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{label}:{number}: {line.strip()}")
    return hits
```

`os.path.relpath` rather than `Path.relative_to`, because the planted-violation tests pass a
`tmp_path` file that is not under `REPO_ROOT` and `relative_to` raises `ValueError` there.

- [ ] **Step 4: Run the self-tests and see them pass**

Run: `.venv/bin/python -m pytest tests/test_layering.py`
Expected: `2 passed in 0.01s`

- [ ] **Step 5: Plant a canary inside the real package**

The two planted-violation tests only prove that `_offenders` can see a violation in a file handed to
it explicitly. They prove nothing about whether the rule tests hand it the right files. Rules 3 and
4 do scan real files at this point - `src/railctl/__init__.py` and `src/railctl/errors.py` - so
watching them fail against the actual package costs one temporary file.

```python
# src/railctl/_layering_canary.py
class CanaryError(Exception):
    pass


PORT = "/dev/ttyUSB0"
```

Line 1 is a rule-3 violation (an exception class outside `errors.py`) and line 5 is a rule-4
violation (a device path outside `transport/`). The file is deleted again in step 7, before any lint
or full-suite run, so it never reaches a commit.

- [ ] **Step 6: Add the four rule tests and the target-coverage test, and watch two of them fail**

Append to `tests/test_layering.py`, above the two planted-violation tests:

```python
def test_rule_1_no_wire_vocabulary_in_station_or_cli():
    """station/ and cli/ speak in Station API terms, never in bytes or port names."""
    assert _offenders(_python_files("station", "cli"), RULE_1_PATTERNS) == []


def test_rule_2_no_cv_arithmetic_outside_xbus_cv():
    """CV numbers are 1-based in every API; xbus/cv.py is the only place they shift."""
    files = _python_files("station", "cli", "xbus/commands.py")
    assert _offenders(files, RULE_2_PATTERNS) == []


def test_rule_3_only_errors_defines_exception_types():
    files = _package_files(exclude=("errors.py",))
    assert files, "the guard scanned no files; the package layout moved"
    assert _offenders(files, (RULE_3_PATTERN,)) == []


def test_rule_4_connection_targets_are_opaque_outside_transport():
    files = _package_files(exclude=("transport",))
    assert files, "the guard scanned no files; the package layout moved"
    assert _offenders(files, RULE_4_PATTERNS) == []


def test_the_rule_1_and_2_targets_are_scanned_once_they_exist():
    """Rules 1 and 2 pass on an empty file list. This is what stops that being silent.

    Today none of these paths exists, so every branch is the `not target.exists()`
    one. Once station/, cli/ or xbus/commands.py lands, this test is what says
    whether the two guards are measuring anything or reporting green over nothing.

    It cannot catch a rename: if the facade package is called facade/ instead of
    station/, this passes and rules 1 and 2 still scan nothing. Whoever creates
    those directories under a different name must add them to the tuple below and
    to _python_files(...) in the two rule tests, in the same commit, and plant a
    canary in the new directory to watch the guard fail once.
    """
    for rel in ("station", "cli", "xbus/commands.py"):
        target = PACKAGE / rel
        assert not target.exists() or _python_files(rel), (
            f"{rel} exists but the scanner found no files in it"
        )
```

Run: `.venv/bin/python -m pytest tests/test_layering.py`
Expected: `2 failed, 5 passed in 0.10s`, the two failures being rules 3 and 4 against the canary:

```
FAILED tests/test_layering.py::test_rule_3_only_errors_defines_exception_types
FAILED tests/test_layering.py::test_rule_4_connection_targets_are_opaque_outside_transport
```

with these two lines in the diffs, naming the canary by file and line number:

```
E         Left contains one more item: 'src/railctl/_layering_canary.py:1: class CanaryError(Exception):'
E         Left contains one more item: 'src/railctl/_layering_canary.py:5: PORT = "/dev/ttyUSB0"'
```

If either failure names a different file, or if only one of the two fires, stop: the guard is not
scanning what this step says it scans.

Rules 1 and 2 pass here on an empty file list, and
`test_the_rule_1_and_2_targets_are_scanned_once_they_exist` passes because none of the three targets
exists yet - that is the expected reading of both, not a gap being waved through.

- [ ] **Step 7: Remove the canary and run the whole suite**

Explain before running: this deletes the one file created in step 5, which contains nothing but the
two planted violations and is not committed.

```bash
rm src/railctl/_layering_canary.py
```

Run: `.venv/bin/python -m pytest tests/test_layering.py`
Expected: `7 passed in 0.09s`

Run: `.venv/bin/python -m pytest`
Expected: `342 passed, 1 deselected in 4.4s`

Confirm the canary is really gone before committing:

```bash
git status --porcelain src/railctl/
```

Expected: no line mentioning `_layering_canary.py`.

- [ ] **Step 8: Write the CI workflow**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v9.0.0
        with:
          enable-cache: true
          python-version: ${{ matrix.python-version }}

      - name: Install the package and dev dependencies
        # --frozen refuses to re-resolve: if uv.lock is stale relative to
        # pyproject.toml, this step fails instead of silently installing a
        # resolution nobody committed and nobody can reproduce locally.
        run: uv sync --frozen

      - name: Assert no serial port is attached
        # The whole non-hardware suite must pass on a machine that has never seen
        # a YD7010. If a runner ever grew a serial device, a test could start
        # passing for the wrong reason and nothing would say so.
        run: |
          ! ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

      - name: Ruff check
        run: uv run ruff check .

      - name: Ruff format
        run: uv run ruff format --check .

      - name: Tests
        # The `ci` hypothesis profile in tests/conftest.py raises max_examples from
        # 100 to 500. It costs about 13 s locally and is the whole point of having
        # property tests run somewhere slower than a developer's patience.
        env:
          HYPOTHESIS_PROFILE: ci
        run: uv run pytest

      - name: The M1 probe still imports
        # tools/ is deliberately outside the wheel. This is the only thing that
        # would notice if the pytest pythonpath entry that keeps it importable
        # were removed.
        run: uv run python -c "import tools.probe; print(tools.probe.__file__)"
```

`astral-sh/setup-uv@v9.0.0` replaces `actions/setup-python@v5`: it installs uv itself, and uv then
provisions the interpreter named by `python-version` (mirroring `matrix.python-version`) the same
way `.python-version` does locally, so there is no separate Python setup step. `enable-cache: true`
caches the uv download cache between runs, keyed on `uv.lock`.

The exact release tag, not a moving major. `astral-sh/setup-uv` publishes moving major tags only up
to `v7`; from `v8` on there is no plain `v8` or `v9` ref, only exact releases (`v9.0.0` is the
newest as of 2026-08-04). Writing `@v9` fails the job before a line of test code runs, at `Set up
job`, with `Unable to resolve action astral-sh/setup-uv@v9, unable to find version v9`. Confirm the
tag still exists before trusting this line:

```bash
gh api repos/astral-sh/setup-uv/releases/latest --jq '.tag_name'
```

`uv sync --frozen` is used here rather than a bare `uv sync`, and that choice is deliberate: a bare
`uv sync` re-resolves and rewrites `uv.lock` if `pyproject.toml` changed since it was last
generated, which would let CI silently pass against a dependency set nobody reviewed. `--frozen`
makes that a hard failure instead - CI is telling the developer "regenerate the lockfile locally
and commit it," not doing that regeneration on their behalf. This is why `uv sync --frozen` is
preferred over the unqualified form everywhere in this workflow.

No coverage step: see the conflict-2 resolution in Task 1. M3 adds
`uv run pytest --cov --cov-report=term-missing` here once `xbus/` exists.

`on: pull_request` is why M2 arrives as a pull request rather than as three commits pushed to
`main`: a workflow whose PR trigger has never run once is a workflow nobody has tested.

- [ ] **Step 9: Confirm the CI commands pass locally before pushing**

Run each workflow command exactly as the workflow now runs it, in workflow order - through `uv
run`, not through `.venv/bin/python` directly, so this dress rehearsal exercises the same frozen
lockfile CI resolves against:

```bash
uv run ruff check . \
  && uv run ruff format --check . \
  && HYPOTHESIS_PROFILE=ci uv run pytest \
  && uv run python -c "import tools.probe; print(tools.probe.__file__)"
```

Expected: `All checks passed!`, `44 files already formatted`, `342 passed, 1 deselected in 13s`,
then `/Users/jacoren/Developer/Personal/railctl/tools/probe/__init__.py`.

42 Python files: Task 1 left 39, Task 2 added two (`src/railctl/errors.py`,
`tests/unit/test_exit_codes.py`) for 41, and this task adds one (`tests/test_layering.py`).
`.github/workflows/ci.yml` is YAML and is not counted, and `src/railctl/_layering_canary.py` was
deleted in step 7. The count reads 44 because ruff 0.16.1 also formats `README.md` and
`CHANGELOG.md`.

- [ ] **Step 10: Commit**

```bash
git add tests/test_layering.py .github/workflows/ci.yml
git commit -m "ci(build): add the layering guards and the 3.11-3.14 CI matrix"
```

- [ ] **Step 11: Push, open the pull request, and watch CI**

The branch is `feature/m2-scaffolding`, created in Task 1 Step 0, and it now carries all three M2
commits. Confirm that before pushing:

```bash
git rev-parse --abbrev-ref HEAD
git log --oneline -3
```

Expected: `feature/m2-scaffolding`, then the three M2 commits, newest first:

```
ci(build): add the layering guards and the 3.11-3.14 CI matrix
feat(errors): add the exception tree, EXIT_CODES and exit_code_for
build(pkg): migrate to the railctl hatchling package and the tests/ package tree
```

Then push the branch and open the PR. Nothing is pushed to `main`; `origin` is
`https://github.com/kamilpajak/railctl.git` and the merge happens through GitHub, as M1 did with
PR #2.

```bash
git push -u origin feature/m2-scaffolding
gh pr create --fill
```

Expected: `gh pr create` prints the new pull request URL, of the form
`https://github.com/kamilpajak/railctl/pull/3`.

Then watch the PR-triggered run:

```bash
RUN_ID=$(gh run list --branch feature/m2-scaffolding --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

Run `gh run watch` in the background rather than blocking the session. Expected: four green jobs,
one per Python version, each reporting `342 passed, 1 deselected`. This is the M2 acceptance
criterion from the spec's implementation order (design doc line 1577): ruff check, ruff format
--check and pytest all pass, with CI green on 3.11-3.14. Merge the PR once all four are green.

---

## Part M3a - the pure X-Bus primitives: codec, dialect, address, speed, cv

Five modules, no I/O, no framing, no logging. Everything here is a pure function over
integers and `bytes`, which is why it can be pinned byte for byte without a station
attached. Three properties of this part matter more than the code:

- **The `FF FE` prefix is not created here.** The envelope owns it (M4). `codec.encode`
  returns a bare telegram, and the XOR is computed over the bare telegram only. This is
  layering rule 1 ("no identifier in `station/` or `cli/` mentions `FF FE`") kept honest
  one layer lower: if `codec` emitted the prefix, every layer above would have to know
  about it.
- **All CV arithmetic lives in `xbus/cv.py`.** Layering rule 2 greps `station/`, `cli/`
  and `xbus/commands.py` for `cv - 1`, `cv + 1`, `% 256`, `>> 8` and `<< 8`. Task 6 is the
  one module allowed to contain them. `address.py` also shifts by 8, and is outside the
  grep set on purpose: it shifts a *locomotive address*, not a CV number.
- **Silence, unknown and false stay distinguishable.** `decode` raises three different
  exception *classes* for three different faults, so a caller separates them with an
  `except` clause and never by matching on message text:

  | Fault | Exception | What the link above should do |
  |---|---|---|
  | fewer than `MIN_TELEGRAM_LEN` bytes | `XBusIncompleteError` | keep reading; the reply has not finished arriving |
  | length disagrees with the header nibble | `XBusDecodeError` | resync the buffer; more bytes will not help |
  | checksum non-zero | `XBusChecksumError` | the frame is complete but damaged; retry the command |

  `XBusIncompleteError` and `XBusChecksumError` both subclass `XBusDecodeError`, so a
  caller that does not need the distinction still writes one `except XBusDecodeError`.
  A parser that collapses them turns a truncated reply into "the station said nothing",
  which is precisely how M1 recorded four capabilities as absent.

---

### Task 4: X-Bus codec and dialect

**Files:**
- Create: `src/railctl/xbus/__init__.py` (empty)
- Create: `src/railctl/xbus/codec.py`
- Create: `src/railctl/xbus/dialect.py`
- Modify: `src/railctl/errors.py` (add `XBusIncompleteError`, Step 5)
- Modify: `docs/superpowers/specs/2026-08-03-railctl-design.md` line 329 (Step 9)
- Test: `tests/unit/test_codec.py`, `tests/unit/test_dialect.py`

**Interfaces:**

- Consumes (from the M2 scaffolding tasks of this plan):
  - `railctl.errors.XBusEncodeError(message: str, *, hint: str | None = None)`
  - `railctl.errors.XBusDecodeError(message: str, *, hint: str | None = None)`
  - `railctl.errors.XBusChecksumError(message: str, *, hint: str | None = None)`, a
    subclass of `XBusDecodeError`
  - `railctl.errors.XBusIncompleteError(message: str, *, hint: str | None = None)`, a
    subclass of `XBusDecodeError`. **Task 2 deliberately does not define this class, so
    Step 5 of this task adds it** — it belongs to the same tree and the same file as its
    two siblings, and Task 2's Produces block says so. It gets no row of its own in
    `EXIT_CODES`: `exit_code_for` walks `type(exc).__mro__`, so it resolves through
    `XBusDecodeError` -> `ProtocolError` to exit code 4, the same code a length mismatch
    and a checksum mismatch already produce. The class exists so that callers can separate
    the three faults with `except`; the exit code the user sees is deliberately unchanged.
  - The test package tree from Task 1: `tests/unit/` already exists and already carries
    `__init__.py`, as does `tests/` itself. This task creates no `__init__.py`.
  - `pyproject.toml` with `[tool.pytest.ini_options] pythonpath = ["src", "."]`, so
    `import railctl` works from the repo root without an install step.
- Produces:
  - `railctl.xbus.codec.MAX_DATA_BYTES: int = 15`
  - `railctl.xbus.codec.MAX_TELEGRAM_LEN: int = 17`
  - `railctl.xbus.codec.MIN_TELEGRAM_LEN: int = 2`
  - `railctl.xbus.codec.LENGTH_NIBBLE_MASK: int = 0x0F`
  - `railctl.xbus.codec.LENGTH_OVERHEAD: int = 2`
  - `railctl.xbus.codec.BYTE_MIN: int = 0`, `railctl.xbus.codec.BYTE_MAX: int = 255`
  - `railctl.xbus.codec.xor(data: bytes) -> int`
  - `railctl.xbus.codec.telegram_length(header: int) -> int`
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes`
  - `railctl.xbus.codec.decode(raw: bytes) -> tuple[int, bytes]`
  - `railctl.xbus.dialect.CvEncoding` - `enum.Enum` with members `POM_ZERO_BASED = "pom"`,
    `SERVICE_DIRECT = "direct"`, `SERVICE_EXT = "ext"`, `Z21_16BIT = "z21"`. Task 6
    re-exports it from `railctl.xbus.cv`, and `railctl.xbus.cv.CvEncoding is
    railctl.xbus.dialect.CvEncoding` is asserted there.
  - `railctl.xbus.dialect.Dialect` - `@dataclass(frozen=True, slots=True)` with fields
    `name: str`, `long_address_threshold: int`,
    `service_cv_preference: tuple[CvEncoding, ...]`
  - `railctl.xbus.dialect.XPRESSNET: Dialect`, `railctl.xbus.dialect.Z21: Dialect`
  - `railctl.xbus.dialect.DIALECTS: tuple[Dialect, ...] = (XPRESSNET, Z21)`
  - `railctl.xbus.dialect.DIVERGENCE_BAND: range` = `range(100, 128)`
  - The vector self-consistency tests written by Task 9 call exactly one function from this
    task, `xor(telegram[:-1]) == telegram[-1]`, and spell the length rule out inline as
    `len(telegram) == (telegram[0] & 0x0F) + 2` rather than calling `telegram_length`, so a
    mistyped row cannot be excused by the same helper it is checking.

- [ ] **Step 1: Write the failing codec test**

```python
# tests/unit/test_codec.py
"""Byte-exact tests for the X-Bus codec.

Two rules from the hardware are pinned here and nowhere else:

* a telegram is `(header & 0x0F) + 2` bytes long, header and XOR included;
* the XOR covers the bare telegram and NEVER the `FF FE` framing prefix.

The golden rows are the T4 table from the design document. Every row is there
because it is a known top bug source, so they are written as literal bytes: a
row computed from the same helper it is testing proves nothing.
"""

from __future__ import annotations

import pytest

from railctl.errors import (
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    XBusIncompleteError,
)
from railctl.xbus.codec import (
    MAX_TELEGRAM_LEN,
    MIN_TELEGRAM_LEN,
    decode,
    encode,
    telegram_length,
    xor,
)

# (encode arguments, expected telegram). The first five rows are the drive
# telegrams of the T4 table, whose data bytes Task 5 produces; the CV rows are
# the ones whose data bytes Task 6 produces. Here they are literal on both
# sides, so a codec regression fails without any other module being involved.
GOLDEN_TELEGRAMS = [
    ((0x21, 0x21), b"\x21\x21\x00"),
    ((0x21, 0x24), b"\x21\x24\x05"),
    ((0x21, 0x81), b"\x21\x81\xa0"),
    ((0x21, 0x80), b"\x21\x80\xa1"),
    ((0x80,), b"\x80\x80"),
    ((0x92, 0x00, 0x03), b"\x92\x00\x03\x91"),
    ((0xE4, 0x13, 0x00, 0x63, 0x82), b"\xe4\x13\x00\x63\x82\x16"),
    ((0xE4, 0x13, 0xC0, 0x64, 0x82), b"\xe4\x13\xc0\x64\x82\xd1"),
    ((0xE4, 0x13, 0x00, 0x64, 0x82), b"\xe4\x13\x00\x64\x82\x11"),
    ((0xE4, 0x13, 0xC0, 0x7F, 0x82), b"\xe4\x13\xc0\x7f\x82\xca"),
    ((0xE4, 0x13, 0xC0, 0x80, 0x82), b"\xe4\x13\xc0\x80\x82\x35"),
    ((0x22, 0x15, 0x01), b"\x22\x15\x01\x36"),
    ((0x22, 0x15, 0xFF), b"\x22\x15\xff\xc8"),
    ((0x22, 0x18, 0x01), b"\x22\x18\x01\x3b"),
    ((0x22, 0x19, 0x00), b"\x22\x19\x00\x3b"),
    ((0x22, 0x18, 0x00), b"\x22\x18\x00\x3a"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE4, 0x00, 0x00), b"\xe6\x30\x00\x03\xe4\x00\x00\x31"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE4, 0x07, 0x00), b"\xe6\x30\x00\x03\xe4\x07\x00\x36"),
    ((0xE6, 0x30, 0x00, 0x03, 0xE5, 0x00, 0x00), b"\xe6\x30\x00\x03\xe5\x00\x00\x30"),
    ((0x23, 0x11, 0x00, 0x1C), b"\x23\x11\x00\x1c\x2e"),
]

# Replies captured on the YD7010 (docs/probe-results.md) plus the two forms the
# design names as decode rows worth keeping.
#
# CAUTION on the first row. The design document USED to write this example as
# `decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x40\x12")`, which contradicts its
# own length rule: the low nibble of 0x63 is 3, so the telegram carries THREE
# data bytes, `21 40 12`, and `10` is the checksum. The rule wins over the
# example - a two-byte answer here would mean `decode` had silently dropped the
# `21`, the byte that says which reply form this is. Step 9 of this task edits
# the design document itself so the two agree; leaving the wrong example in the
# authoritative file would invite the next reader to "fix" the code back.
GOLDEN_REPLIES = [
    (b"\x63\x21\x40\x12\x10", 0x63, b"\x21\x40\x12"),
    (b"\x62\x22\x07\x47", 0x62, b"\x22\x07"),
    (b"\x63\x14\x08\x08\x77", 0x63, b"\x14\x08\x08"),
    (b"\x61\x82\xe3", 0x61, b"\x82"),
    (b"\x71\xaa\xdb", 0x71, b"\xaa"),
]


def test_xor_of_a_complete_telegram_is_zero():
    """The identity decode() relies on: XOR over header+data+checksum cancels."""
    assert xor(b"\x63\x21\x40\x12\x10") == 0


def test_xor_of_an_empty_buffer_is_zero():
    assert xor(b"") == 0


def test_xor_never_covers_the_framing_prefix():
    """FF FE is added by the envelope and is not part of the checksum.

    Including it would change the checksum of every command by 0x01, and the
    station would answer nothing at all - the failure that reads as "this
    command is unsupported".
    """
    assert xor(b"\x21\x81") == 0xA0
    assert xor(b"\xff\xfe\x21\x81") != 0xA0


def test_telegram_length_is_the_low_nibble_plus_two():
    assert telegram_length(0x21) == 3
    assert telegram_length(0x62) == 4
    assert telegram_length(0x63) == 5
    assert telegram_length(0xE4) == 6
    assert telegram_length(0xE6) == 8


def test_telegram_length_spans_the_documented_extremes():
    assert telegram_length(0x80) == MIN_TELEGRAM_LEN
    assert telegram_length(0xEF) == MAX_TELEGRAM_LEN


@pytest.mark.parametrize(("args", "expected"), GOLDEN_TELEGRAMS)
def test_encode_matches_the_golden_telegram(args: tuple[int, ...], expected: bytes):
    assert encode(*args) == expected


def test_encode_returns_a_bare_telegram_with_no_framing_prefix():
    assert not encode(0x21, 0x21).startswith(b"\xff\xfe")
    assert not encode(0x21, 0x21).startswith(b"\xff\xfd")


def test_encode_rejects_too_few_data_bytes():
    with pytest.raises(XBusEncodeError, match="declares 1 data byte"):
        encode(0x21)


def test_encode_rejects_too_many_data_bytes():
    with pytest.raises(XBusEncodeError, match="declares 1 data byte"):
        encode(0x21, 0x21, 0x00)


def test_encode_rejects_a_data_byte_outside_a_byte():
    with pytest.raises(XBusEncodeError, match="data byte 0"):
        encode(0x21, 256)
    with pytest.raises(XBusEncodeError, match="data byte 0"):
        encode(0x21, -1)


def test_encode_rejects_a_header_outside_a_byte():
    with pytest.raises(XBusEncodeError, match="header"):
        encode(256, 0x00)


@pytest.mark.parametrize(("raw", "header", "data"), GOLDEN_REPLIES)
def test_decode_splits_header_and_data(raw: bytes, header: int, data: bytes):
    assert decode(raw) == (header, data)


def test_decode_round_trips_every_golden_telegram():
    for args, telegram in GOLDEN_TELEGRAMS:
        header, data = decode(telegram)
        assert (header, *data) == args


def test_decode_rejects_a_bad_checksum():
    with pytest.raises(XBusChecksumError):
        decode(b"\x21\x21\x01")


def test_a_length_mismatch_is_a_decode_error_and_not_a_checksum_error():
    """Truncation and corruption must not look like the same fault.

    A short read that is reported as a checksum error tells the layer above to
    retry the same command; a checksum error reported as truncation tells it to
    wait for more bytes that will never come. Both end as "no reply", which is
    how this project records a capability as absent.

    `type(...) is XBusDecodeError` is exact on purpose: it fails if this case
    ever starts raising the incomplete-buffer subclass, which is what keeps the
    two provably distinct rather than distinct-by-message-text.
    """
    with pytest.raises(XBusDecodeError) as excinfo:
        decode(b"\x63\x21\x40")
    assert type(excinfo.value) is XBusDecodeError
    assert not isinstance(excinfo.value, XBusIncompleteError)
    assert "declares 5 bytes" in str(excinfo.value)


@pytest.mark.parametrize("raw", [b"", b"\x21"])
def test_decode_rejects_a_buffer_below_the_minimum_length(raw: bytes):
    """A buffer too short to hold a telegram is its OWN exception class.

    The link layer must tell "the reply has not finished arriving" (keep
    reading) from "the reply arrived damaged" (resync or retry). If both are a
    bare XBusDecodeError, the only way to tell them apart is to match on message
    text, which no caller should ever do - so the difference becomes a class.
    """
    with pytest.raises(XBusIncompleteError, match="shorter than"):
        decode(raw)


def test_an_incomplete_buffer_is_still_caught_by_the_general_decode_error():
    """A caller that does not need the distinction writes one except clause."""
    assert issubclass(XBusIncompleteError, XBusDecodeError)
    assert issubclass(XBusChecksumError, XBusDecodeError)
    with pytest.raises(XBusDecodeError):
        decode(b"\x21")
```

- [ ] **Step 2: Write the failing dialect test**

```python
# tests/unit/test_dialect.py
"""The XpressNet / Z21 split.

The YD7010 reports command station id 0x12 - the Z21 family - and its Z21
opcodes answer (docs/probe-results.md). The split still has to exist, because
the two dialects disagree about locomotive addresses 100..127, and that
disagreement is documented rather than measured on this hardware. A dialect is
data, not a class hierarchy: two integers and an ordered preference list.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.xbus.dialect import DIALECTS, DIVERGENCE_BAND, XPRESSNET, Z21, CvEncoding, Dialect


def test_xpressnet_switches_to_long_addresses_at_100():
    assert XPRESSNET.name == "xpressnet"
    assert XPRESSNET.long_address_threshold == 100


def test_z21_switches_to_long_addresses_at_128():
    assert Z21.name == "z21"
    assert Z21.long_address_threshold == 128


def test_the_divergence_band_is_exactly_100_to_127():
    assert list(DIVERGENCE_BAND) == list(range(100, 128))
    assert 99 not in DIVERGENCE_BAND
    assert 100 in DIVERGENCE_BAND
    assert 127 in DIVERGENCE_BAND
    assert 128 not in DIVERGENCE_BAND


def test_xpressnet_prefers_direct_then_z21_then_extended():
    assert XPRESSNET.service_cv_preference == (
        CvEncoding.SERVICE_DIRECT,
        CvEncoding.Z21_16BIT,
        CvEncoding.SERVICE_EXT,
    )


def test_z21_defaults_to_the_sixteen_bit_encoding_only():
    """Measured on the YD7010 (docs/probe-results.md, R2/R4): 22 15, 22 18 and
    22 19 all answer. This tuple is the default preference order, not a statement
    that the other encodings are unavailable; `Capabilities` re-adds them once
    `doctor` measures them.
    """
    assert Z21.service_cv_preference == (CvEncoding.Z21_16BIT,)


def test_a_dialect_cannot_be_edited_after_construction():
    with pytest.raises(dataclasses.FrozenInstanceError):
        XPRESSNET.long_address_threshold = 128  # type: ignore[misc]


def test_a_dialect_carries_no_instance_dict():
    assert not hasattr(XPRESSNET, "__dict__")


def test_dialects_lists_both_and_nothing_else():
    assert DIALECTS == (XPRESSNET, Z21)
    assert all(isinstance(d, Dialect) for d in DIALECTS)
```

- [ ] **Step 3: Run both test files and watch them fail**

Run: `.venv/bin/python -m pytest tests/unit/test_codec.py tests/unit/test_dialect.py -v`

Expected: FAIL at collection —
`ModuleNotFoundError: No module named 'railctl.xbus'`.
Task 1 deliberately created no empty `xbus/` package ("an empty package would make
`railctl.cli.main` look importable to a reader while still failing at run time"), so this is
the message, not `No module named 'railctl.xbus.codec'`.

- [ ] **Step 4: Create the `xbus` package `__init__.py`**

```python
# src/railctl/xbus/__init__.py
```

It is empty. `tests/unit/__init__.py` is **not** created here: Task 1 already created
`tests/__init__.py` and `tests/unit/__init__.py`, which is what makes
`from tests.vectors import ...` work in Task 9 under pytest's default `prepend` import mode.

- [ ] **Step 5: Add `XBusIncompleteError` to the error tree**

Task 2 states in its Produces block that it does not define this class, so this step always
applies. Insert it in `src/railctl/errors.py` directly after
`class XBusChecksumError(XBusDecodeError):` and its docstring, so the three siblings sit
together:

```python
class XBusIncompleteError(XBusDecodeError):
    """The buffer holds fewer bytes than the shortest possible telegram.

    Separate from its parent because the caller's response is different: an
    incomplete buffer means keep reading, a length or checksum fault means
    resync or retry. Both would otherwise be one XBusDecodeError separable only
    by message text, and a link that waits for bytes that will never come ends
    as "no reply" - how this project has recorded working capabilities as
    absent.
    """
```

Add no row to `EXIT_CODES`. `exit_code_for` walks `type(exc).__mro__`, so this class
resolves through `XBusDecodeError` to `ProtocolError`'s exit code 4 - the same code a
length mismatch already produces. The new class changes what a caller can catch, not what
the user sees on the command line.

Verify both facts:

Run: `.venv/bin/python -c "from railctl.errors import XBusDecodeError, XBusIncompleteError, exit_code_for; print(issubclass(XBusIncompleteError, XBusDecodeError), exit_code_for(XBusIncompleteError('x')))"`

Expected: `True 4`

- [ ] **Step 6: Implement the codec**

```python
# src/railctl/xbus/codec.py
"""X-Bus telegram codec: bare telegrams in, bare telegrams out.

A telegram is a header, N data bytes and an XOR. Its length is `(header & 0x0F) + 2`:
the low nibble of the header counts the data bytes, and the +2 covers the header
itself and the checksum byte.

Two things this module deliberately does NOT do:

* it never prepends `FF FE` (or `FF FD`). That prefix belongs to the LI-USB
  envelope, and it is never part of the XOR. A checksum computed over the prefix
  is wrong by 0x01, the station answers nothing, and "no answer" is how this
  project has repeatedly recorded a working capability as missing.
* it never interprets a telegram. `decode` returns the header and the data
  bytes; deciding what they mean is `replies.py`.
"""

from __future__ import annotations

from railctl.errors import (
    XBusChecksumError,
    XBusDecodeError,
    XBusEncodeError,
    XBusIncompleteError,
)

MAX_DATA_BYTES = 15
MAX_TELEGRAM_LEN = 17
MIN_TELEGRAM_LEN = 2

LENGTH_NIBBLE_MASK = 0x0F
LENGTH_OVERHEAD = 2
BYTE_MIN = 0
BYTE_MAX = 255


def xor(data: bytes) -> int:
    """XOR checksum over a bare telegram body.

    `xor(complete_telegram) == 0` for every valid telegram, which is the
    identity `decode` checks with.
    """
    result = 0
    for byte in data:
        result ^= byte
    return result


def telegram_length(header: int) -> int:
    """Total telegram size in bytes: header + N data bytes + XOR."""
    return (header & LENGTH_NIBBLE_MASK) + LENGTH_OVERHEAD


def encode(header: int, *data: int) -> bytes:
    """Build a complete telegram: header, data, XOR. No framing prefix.

    The data-byte count is derived from the header's low nibble and checked
    against the arguments, so an opcode whose argument list disagrees with its
    own declared length cannot ship. A telegram that lies about its length is
    worse than a rejected one: the station reads the *next* telegram from the
    wrong offset, so every later reply on that link is lost too.
    """
    if not BYTE_MIN <= header <= BYTE_MAX:
        raise XBusEncodeError(f"header {header} is not a byte in {BYTE_MIN}..{BYTE_MAX}")
    expected = header & LENGTH_NIBBLE_MASK
    if len(data) != expected:
        raise XBusEncodeError(
            f"header 0x{header:02X} declares {expected} data byte(s), got {len(data)}"
        )
    for index, byte in enumerate(data):
        if not BYTE_MIN <= byte <= BYTE_MAX:
            raise XBusEncodeError(
                f"data byte {index} = {byte} is not a byte in {BYTE_MIN}..{BYTE_MAX}"
            )
    body = bytes([header, *data])
    return body + bytes([xor(body)])


def decode(raw: bytes) -> tuple[int, bytes]:
    """Split a complete telegram into `(header, data)`, checksum removed.

    Three distinct faults, three distinct exception CLASSES, on purpose:

    * shorter than `MIN_TELEGRAM_LEN`  -> XBusIncompleteError (keep reading)
    * length disagrees with the header -> XBusDecodeError     (resync; more bytes
                                                               will not help)
    * checksum non-zero                -> XBusChecksumError   (complete but damaged)

    The first and third are subclasses of the second, so `except XBusDecodeError`
    still catches all three when the caller does not care. What matters is that a
    caller who DOES care separates them with `except`, never by matching on the
    message text - message text is free to change, and a link that keeps waiting
    for bytes that will never come ends as "no reply", which is how this project
    records a working capability as absent.
    """
    if len(raw) < MIN_TELEGRAM_LEN:
        raise XBusIncompleteError(
            f"telegram of {len(raw)} byte(s) is shorter than the minimum {MIN_TELEGRAM_LEN}"
        )
    expected = telegram_length(raw[0])
    if len(raw) != expected:
        raise XBusDecodeError(f"header 0x{raw[0]:02X} declares {expected} bytes, got {len(raw)}")
    if xor(raw) != 0:
        raise XBusChecksumError(
            f"checksum mismatch in {raw.hex(' ')}: expected 0x{xor(raw[:-1]):02X}"
        )
    return raw[0], raw[1:-1]
```

- [ ] **Step 7: Implement the dialect table**

```python
# src/railctl/xbus/dialect.py
"""XpressNet and Z21: the two addressing and CV conventions this tool speaks.

The YD7010 reports command station id 0x12 - the Z21 family - and answers the
Z21 opcodes. It also answers the Lenz ones: `22 15`, `22 18` and `22 19` were all
verified three rounds each against known constants (docs/probe-results.md, R2/R4
"Settled"). `service_cv_preference` is therefore a DEFAULT ORDER, not a list of
what the station can do; an earlier document declared those opcodes absent
because the probe never sent the `21 10` result request, and that mistake must
not be re-frozen here as a design constant. The split is kept anyway,
because the two dialects disagree about locomotive addresses 100..127:
XpressNet sends them as long DCC addresses, Z21 sends them short. That band is
documented, not measured on this hardware, so it is carried as data and pinned
by tests rather than assumed away.

`CvEncoding` lives here rather than in `cv.py` because `Dialect` needs it at
class-definition time, while `cv.py` only needs it inside function bodies.
`cv.py` re-exports it, so `from railctl.xbus.cv import CvEncoding` is the same
object.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class CvEncoding(enum.Enum):
    """How a CV number is put on the wire. See `railctl.xbus.cv` for the rules."""

    POM_ZERO_BASED = "pom"  # E6 30 ... (0xE4|MM) LSB          wire = cv - 1
    SERVICE_DIRECT = "direct"  # 22 15 C / 23 16 C V           wire = cv, 1..255
    SERVICE_EXT = "ext"  # 22 18..1B / 23 1C..1F               page + (cv & 0xFF)
    Z21_16BIT = "z21"  # 23 11 MSB LSB / 24 12 MSB LSB V       wire = cv - 1


@dataclass(frozen=True, slots=True)
class Dialect:
    """A data object, not a hierarchy.

    `long_address_threshold` is the *default*. Once `doctor` measures the
    station's real threshold, `Capabilities.loco_address_threshold` overrides it,
    and it is one integer that changes - not a code path.

    `service_cv_preference` is the ordered list the station walks when choosing a
    service-mode encoding; the station filters it by what capabilities say works.
    """

    name: str
    long_address_threshold: int
    service_cv_preference: tuple[CvEncoding, ...]


XPRESSNET = Dialect(
    "xpressnet",
    100,
    (CvEncoding.SERVICE_DIRECT, CvEncoding.Z21_16BIT, CvEncoding.SERVICE_EXT),
)
# Measured on the YD7010 (docs/probe-results.md, R2/R4): 22 15, 22 18 and 22 19
# all answer. This tuple is the default preference order, not a statement that
# the other encodings are unavailable; `Capabilities` re-adds them once `doctor`
# measures them.
Z21 = Dialect("z21", 128, (CvEncoding.Z21_16BIT,))

DIALECTS: tuple[Dialect, ...] = (XPRESSNET, Z21)

# Addresses where the two dialects put different bytes on the wire: XpressNet
# marks them long, Z21 leaves them short. A decoder configured short in this
# range (CV1 = 100..127 with CV29 bit 5 clear) simply ignores the long form,
# with no error of any kind - which is why the station warns once, naming CV1
# and CV29 bit 5, instead of reporting a failure that never arrives.
DIVERGENCE_BAND = range(XPRESSNET.long_address_threshold, Z21.long_address_threshold)
```

Note: no default dialect is chosen here. Which dialect a given station speaks is a
station-layer decision (Plan 3) informed by the identity reply and by `Capabilities`;
picking one in this module would bury it where no capability can override it.

- [ ] **Step 8: Run the tests and see them pass**

Run: `.venv/bin/python -m pytest tests/unit/test_codec.py tests/unit/test_dialect.py -v`

Expected: PASS — 49 passed (41 in `test_codec.py`, 8 in `test_dialect.py`).

- [ ] **Step 9: Correct the decode example in the design document**

`docs/superpowers/specs/2026-08-03-railctl-design.md` line 329 currently ends with:

```
`decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x40\x12")`
```

That contradicts the length rule stated three lines above it in the same document: the low
nibble of `0x63` is 3, so the telegram carries three data bytes, `21 40 12`, and `10` is
the checksum. Replace it with:

```
`decode(b"\x63\x21\x40\x12\x10") == (0x63, b"\x21\x40\x12")`
```

The spec is the authoritative document for every later section and every later reader, so
leaving the wrong example there means the next person reconciling code against it "fixes"
`decode` back and silently drops the `21` byte - the byte that says which reply form this
is. Fixing the test comment alone would not prevent that.

Verify the edit landed and that nothing else in the document still shows the two-byte form:

Run: `grep -n 'decode(b"' docs/superpowers/specs/2026-08-03-railctl-design.md`

Expected: one line, number 329, containing `(0x63, b"\x21\x40\x12")` and no occurrence of
`(0x63, b"\x40\x12")`.

- [ ] **Step 10: Lint and format**

Run: `.venv/bin/python -m ruff check src/railctl/errors.py src/railctl/xbus tests/unit && .venv/bin/python -m ruff format --check src/railctl/errors.py src/railctl/xbus tests/unit`

Expected: `All checks passed!` and `5 files already formatted` style output with no
findings. Run `.venv/bin/python -m ruff format src/railctl/errors.py src/railctl/xbus tests/unit`
first if the check reports formatting.

- [ ] **Step 11: Commit**

Two commits, because the documentation correction is not part of the feature and must stay
readable as its own change:

```bash
git add docs/superpowers/specs/2026-08-03-railctl-design.md
git commit -m "fix(docs): correct the decode example to match the length rule"
git add src/railctl/errors.py src/railctl/xbus/__init__.py src/railctl/xbus/codec.py src/railctl/xbus/dialect.py tests/unit/test_codec.py tests/unit/test_dialect.py
git commit -m "feat(xbus): add the telegram codec and the dialect table"
```

---

### Task 5: Locomotive address and 128-step speed

**Files:**
- Create: `src/railctl/xbus/address.py`
- Create: `src/railctl/xbus/speed.py`
- Create: `tests/unit/test_address.py`, `tests/unit/test_speed.py`, `tests/unit/test_properties.py`
- Modify: `tests/conftest.py` line 44 (add `derandomize=True` to the `ci` hypothesis profile)

**Interfaces:**

- Consumes:
  - `railctl.xbus.codec.xor(data: bytes) -> int`
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes`
  - `railctl.xbus.codec.decode(raw: bytes) -> tuple[int, bytes]`
  - `railctl.errors.XBusDecodeError`, and its two subclasses
    `railctl.errors.XBusChecksumError` and `railctl.errors.XBusIncompleteError`.
    Property 5 catches the parent on purpose: a flipped bit lands either on the checksum
    identity or on the header's length nibble, and the law being stated is "no corrupted
    frame decodes", not "this particular fault fired". Which class fires for which fault
    is pinned in `test_codec.py`.
  - `railctl.xbus.dialect.DIALECTS: tuple[Dialect, ...]`,
    `railctl.xbus.dialect.DIVERGENCE_BAND: range`,
    `railctl.xbus.dialect.XPRESSNET`, `railctl.xbus.dialect.Z21`, each `Dialect` carrying
    `.name: str` and `.long_address_threshold: int`
- Produces:
  - `railctl.xbus.address.LOCO_ADDR_MIN: int = 1`, `railctl.xbus.address.LOCO_ADDR_MAX: int = 9999`
  - `railctl.xbus.address.LONG_ADDRESS_FLAG: int = 0xC000`
  - `railctl.xbus.address.LONG_ADDRESS_MASK: int = 0x3FFF`
  - `railctl.xbus.address.encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]`
    returning `(adr_high, adr_low)`
  - `railctl.xbus.address.decode_loco_address(adr_high: int, adr_low: int) -> int`
  - `railctl.xbus.speed.SPEED_STEPS: int = 128`
  - `railctl.xbus.speed.MAX_SPEED_STEP: int = 126`
  - `railctl.xbus.speed.DRIVE_IDENT_128: int = 0x13`
  - `railctl.xbus.speed.DIRECTION_BIT: int = 0x80`, `railctl.xbus.speed.SPEED_MASK: int = 0x7F`
  - `railctl.xbus.speed.STOP_WIRE: int = 0x00`, `railctl.xbus.speed.EMERGENCY_STOP_WIRE: int = 0x01`
  - `railctl.xbus.speed.WIRE_STEP_OFFSET: int = 1`
  - `railctl.xbus.speed.Direction` - `enum.IntEnum` with `REVERSE = 0`, `FORWARD = 1`.
    Defined here once; `railctl.station` re-exports it and the CLI parses it as
    `Direction[value.upper()]`.
  - `railctl.xbus.speed.encode_speed_128(step: int, direction: Direction) -> int`
  - `railctl.xbus.speed.encode_emergency_stop_128(direction: Direction) -> int`
  - `railctl.xbus.speed.decode_speed_128(byte: int) -> tuple[int, Direction, bool]`
    returning `(step, direction, emergency)`
  - `tests/unit/test_properties.py` - the only hypothesis file in the package, carrying
    all five properties. No other task adds hypothesis tests.
  - **Both modules raise plain `ValueError` on a bad argument, never a `RailctlError`.**
    The design document mandates it (spec lines 361 and 377: "`ValueError` at 0, -1,
    10000" and "`ValueError` at -1 and 127"), and M2 states the same rule: argument
    validation in this package raises `ValueError`, which `cli/_errors.py` turns into exit
    code 2 (usage), while a `RailctlError` goes through `EXIT_CODES`. Consequence the
    station layer must know: `xbus` raises **two** unrelated exception families -
    `ValueError` from `address.py` and `speed.py`, `RailctlError` subclasses from
    `codec.py` and (for CV range faults) `cv.py`. A station-layer `except` that names only
    one of them lets the other escape as an unhandled traceback with exit code 1.

- [ ] **Step 1: Write the failing address test**

```python
# tests/unit/test_address.py
"""Locomotive address wire form.

One function serves both dialects because XpressNet's "add 0xC000, then split"
and Z21's "DB1 = 0xC0 | Adr_MSB" produce identical bytes for every address the
station accepts. That claim is asserted exhaustively below rather than argued,
because the only real difference - the threshold at which an address becomes
long - is a single integer, and getting it wrong is silent: a decoder addressed
in the wrong form does nothing at all and reports nothing at all.
"""

from __future__ import annotations

import pytest

from railctl.xbus.address import (
    LOCO_ADDR_MAX,
    LOCO_ADDR_MIN,
    LONG_ADDRESS_FLAG,
    decode_loco_address,
    encode_loco_address,
)

# (address, threshold) -> (high, low). The 100..127 rows are the divergence band.
ADDRESS_VECTORS = [
    ((1, 100), (0x00, 0x01)),
    ((99, 100), (0x00, 0x63)),
    ((100, 100), (0xC0, 0x64)),
    ((127, 100), (0xC0, 0x7F)),
    ((128, 100), (0xC0, 0x80)),
    ((1000, 100), (0xC3, 0xE8)),
    ((1234, 100), (0xC4, 0xD2)),
    ((9999, 100), (0xE7, 0x0F)),
    ((100, 128), (0x00, 0x64)),
    ((127, 128), (0x00, 0x7F)),
    ((128, 128), (0xC0, 0x80)),
]


@pytest.mark.parametrize(("args", "expected"), ADDRESS_VECTORS)
def test_encode_matches_the_golden_bytes(args: tuple[int, int], expected: tuple[int, int]):
    address, threshold = args
    assert encode_loco_address(address, long_threshold=threshold) == expected


@pytest.mark.parametrize("address", [0, -1, 10000, 100000])
def test_encode_rejects_an_address_outside_the_station_range(address: int):
    with pytest.raises(ValueError, match="out of range"):
        encode_loco_address(address, long_threshold=100)


@pytest.mark.parametrize("high", [0x80, 0x40])
def test_decode_rejects_a_high_byte_with_only_one_marker_bit(high: int):
    """0xC0 means long. 0x80 or 0x40 alone is not a form any encoder produces.

    Returning a number for it would publish a locomotive address that nothing
    sent, so it is refused by name instead.
    """
    with pytest.raises(ValueError, match="not a locomotive address"):
        decode_loco_address(high, 0x64)


@pytest.mark.parametrize(("high", "low"), [(256, 0x00), (0x00, -1)])
def test_decode_rejects_a_wire_byte_outside_a_byte(high: int, low: int):
    with pytest.raises(ValueError, match="not a byte"):
        decode_loco_address(high, low)


def test_zero_decodes_to_zero_which_is_not_a_locomotive():
    """00 00 is the empty address slot, not loco 0. The caller decides."""
    assert decode_loco_address(0x00, 0x00) == 0
    assert LOCO_ADDR_MIN == 1


def test_every_address_survives_the_round_trip_under_both_thresholds():
    for threshold in (100, 128):
        for address in range(LOCO_ADDR_MIN, LOCO_ADDR_MAX + 1):
            high, low = encode_loco_address(address, long_threshold=threshold)
            assert decode_loco_address(high, low) == address


def test_the_two_dialect_formulas_produce_identical_bytes():
    """XpressNet: address | 0xC000, then split. Z21: DB1 = 0xC0 | Adr_MSB.

    Asserted for every long address the station accepts, so that "one function
    covers both dialects" is a measured claim rather than a convenience.
    """
    for address in range(100, LOCO_ADDR_MAX + 1):
        high, low = encode_loco_address(address, long_threshold=100)
        assert high == (0xC0 | ((address >> 8) & 0x3F))
        assert low == address & 0xFF


def test_the_long_marker_follows_the_threshold_and_nothing_else():
    for threshold in (100, 128):
        for address in range(LOCO_ADDR_MIN, LOCO_ADDR_MAX + 1):
            high, _ = encode_loco_address(address, long_threshold=threshold)
            is_long = (high << 8) & LONG_ADDRESS_FLAG == LONG_ADDRESS_FLAG
            assert is_long is (address >= threshold)
```

- [ ] **Step 2: Write the failing speed test**

```python
# tests/unit/test_speed.py
"""128-step speed byte.

Wire layout is RVVVVVVV: bit 7 is direction (1 = forward), the low seven bits
are 0 for a braked stop, 1 for emergency stop, and 2..127 for steps 1..126.
Wire value 1 is reserved, which is why an ordinary step is `step + 1` and never
collides with it. Direction is carried even on a stop, so a stop command must
not lose the direction the locomotive was travelling in.
"""

from __future__ import annotations

import pytest

from railctl.xbus.speed import (
    DRIVE_IDENT_128,
    EMERGENCY_STOP_WIRE,
    MAX_SPEED_STEP,
    SPEED_STEPS,
    Direction,
    decode_speed_128,
    encode_emergency_stop_128,
    encode_speed_128,
)

SPEED_VECTORS = [
    ((0, Direction.FORWARD), 0x80),
    ((0, Direction.REVERSE), 0x00),
    ((1, Direction.FORWARD), 0x82),
    ((60, Direction.FORWARD), 0xBD),
    ((63, Direction.FORWARD), 0xC0),
    ((126, Direction.FORWARD), 0xFF),
    ((126, Direction.REVERSE), 0x7F),
]

DECODE_VECTORS = [
    (0x80, (0, Direction.FORWARD, False)),
    (0x00, (0, Direction.REVERSE, False)),
    (0x82, (1, Direction.FORWARD, False)),
    (0xBD, (60, Direction.FORWARD, False)),
    (0xFF, (126, Direction.FORWARD, False)),
    (0x7F, (126, Direction.REVERSE, False)),
    (0x81, (0, Direction.FORWARD, True)),
    (0x01, (0, Direction.REVERSE, True)),
]


def test_the_constants_describe_128_step_mode():
    assert SPEED_STEPS == 128
    assert MAX_SPEED_STEP == 126
    assert DRIVE_IDENT_128 == 0x13


@pytest.mark.parametrize(("args", "expected"), SPEED_VECTORS)
def test_encode_matches_the_golden_byte(args: tuple[int, Direction], expected: int):
    step, direction = args
    assert encode_speed_128(step, direction) == expected


def test_emergency_stop_uses_the_reserved_wire_value_one():
    assert encode_emergency_stop_128(Direction.FORWARD) == 0x81
    assert encode_emergency_stop_128(Direction.REVERSE) == 0x01


@pytest.mark.parametrize(("byte", "expected"), DECODE_VECTORS)
def test_decode_matches_the_golden_triple(byte: int, expected: tuple[int, Direction, bool]):
    assert decode_speed_128(byte) == expected


@pytest.mark.parametrize("step", [-1, 127, 1000])
def test_encode_rejects_a_step_outside_zero_to_126(step: int):
    with pytest.raises(ValueError, match="out of range"):
        encode_speed_128(step, Direction.FORWARD)


@pytest.mark.parametrize("byte", [-1, 256])
def test_decode_rejects_a_wire_value_outside_a_byte(byte: int):
    with pytest.raises(ValueError, match="not a byte"):
        decode_speed_128(byte)


def test_direction_is_carried_even_on_a_stop():
    """A stop that forgets the direction makes the next start guess it."""
    assert decode_speed_128(encode_speed_128(0, Direction.FORWARD))[1] is Direction.FORWARD
    assert decode_speed_128(encode_speed_128(0, Direction.REVERSE))[1] is Direction.REVERSE


def test_every_step_round_trips_in_both_directions():
    for step in range(0, MAX_SPEED_STEP + 1):
        for direction in Direction:
            assert decode_speed_128(encode_speed_128(step, direction)) == (step, direction, False)


def test_no_ordinary_step_ever_lands_on_the_emergency_stop_value():
    for step in range(0, MAX_SPEED_STEP + 1):
        for direction in Direction:
            assert encode_speed_128(step, direction) & 0x7F != EMERGENCY_STOP_WIRE
```

- [ ] **Step 3: Write the failing property test file**

This is the only file in the package that uses hypothesis, and it carries exactly the
five properties the design names. The encoders are deliberately not property tested: a
property test for a drive telegram would reimplement the encoder and prove nothing.

```python
# tests/unit/test_properties.py
"""The five properties of the pure X-Bus layer.

Property tests state a law that must hold for every input. The example tests
next door pin the bytes one afternoon's hardware produced. Neither replaces the
other: hypothesis finds the shape of a bug, it does not promise to visit a named
constant, so boundaries stay in the example files.

Deliberately absent: any property over a command encoder. Rebuilding
`cmd_drive_128` inside its own test asserts that two copies of the same mistake
agree.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from railctl.errors import XBusDecodeError
from railctl.xbus.address import (
    LOCO_ADDR_MAX,
    LOCO_ADDR_MIN,
    decode_loco_address,
    encode_loco_address,
)
from railctl.xbus.codec import decode, encode, xor
from railctl.xbus.dialect import DIALECTS, DIVERGENCE_BAND, XPRESSNET, Z21, Dialect
from railctl.xbus.speed import (
    EMERGENCY_STOP_WIRE,
    MAX_SPEED_STEP,
    SPEED_MASK,
    Direction,
    decode_speed_128,
    encode_speed_128,
)

ADDRESSES = st.integers(min_value=LOCO_ADDR_MIN, max_value=LOCO_ADDR_MAX)
STEPS = st.integers(min_value=0, max_value=MAX_SPEED_STEP)
DIRECTIONS = st.sampled_from(list(Direction))


@st.composite
def telegrams(draw: st.DrawFn) -> bytes:
    """A valid telegram of any shape: header, matching data count, real XOR."""
    header = draw(st.integers(min_value=0, max_value=255))
    count = header & 0x0F
    data = draw(st.lists(st.integers(min_value=0, max_value=255), min_size=count, max_size=count))
    return encode(header, *data)


PROFILE_EXAMPLES = {"default": 100, "mutation": 25, "ci": 500}
DERANDOMISED_PROFILES = {"ci", "mutation"}


def test_the_ci_profile_is_derandomised():
    """A newly discovered example must not fail an unrelated CI run.

    Random draws also make any mutation score a sample rather than a
    measurement; two runs of identical code scored differently before this was
    set on the probe (docs/test-hardening.md).

    This checks the REGISTERED profile only. Registering it is not the same as
    running under it - see the next test.
    """
    assert settings.get_profile("ci").derandomize is True


def test_the_loaded_profile_matches_the_environment():
    """settings.default is the profile load_profile() actually installed.

    Without this, `HYPOTHESIS_PROFILE=ci pytest` looks identical to a plain run:
    both pass, and nothing anywhere states that the 500-example pass really
    happened. Reading `settings.default` rather than `get_profile("ci")` is the
    whole point - it is the only value that differs when conftest.py never saw
    the environment variable.

    An unknown profile name raises KeyError here on purpose: a typo in
    HYPOTHESIS_PROFILE must not silently fall back to 100 random examples.
    """
    name = os.environ.get("HYPOTHESIS_PROFILE", "default")
    assert settings.default.derandomize is (name in DERANDOMISED_PROFILES)
    assert settings.default.max_examples == PROFILE_EXAMPLES[name]


@given(ADDRESSES, st.sampled_from(DIALECTS))
def test_an_address_survives_the_round_trip_under_every_dialect(address: int, dialect: Dialect):
    """Property 1. Whatever the threshold, the address that went in comes out."""
    high, low = encode_loco_address(address, long_threshold=dialect.long_address_threshold)
    assert decode_loco_address(high, low) == address


@given(ADDRESSES.filter(lambda a: a not in DIVERGENCE_BAND))
def test_the_dialects_agree_outside_the_divergence_band(address: int):
    """Property 2. Only 100..127 is contested; everywhere else the bytes match."""
    assert encode_loco_address(
        address, long_threshold=XPRESSNET.long_address_threshold
    ) == encode_loco_address(address, long_threshold=Z21.long_address_threshold)


@given(st.sampled_from(list(DIVERGENCE_BAND)))
def test_the_dialects_differ_inside_the_divergence_band(address: int):
    """Property 3. XpressNet sends 100..127 long, Z21 sends them short.

    Stated as a law so that "simplifying" the two thresholds into one fails
    loudly. A decoder configured short in this range ignores the long form in
    silence, which is indistinguishable from a decoder that is not there.
    """
    xpressnet = encode_loco_address(address, long_threshold=XPRESSNET.long_address_threshold)
    z21 = encode_loco_address(address, long_threshold=Z21.long_address_threshold)
    assert xpressnet != z21
    assert xpressnet[0] == 0xC0
    assert z21[0] == 0x00


@given(STEPS, DIRECTIONS)
def test_a_speed_step_round_trips_and_never_collides_with_emergency_stop(
    step: int, direction: Direction
):
    """Property 4. Wire value 1 is reserved; no ordinary step may reach it."""
    wire = encode_speed_128(step, direction)
    assert wire & SPEED_MASK != EMERGENCY_STOP_WIRE
    assert decode_speed_128(wire) == (step, direction, False)


@given(telegrams(), st.data())
def test_a_single_flipped_bit_is_always_detected(telegram: bytes, data: st.DataObject):
    """Property 5. One corrupted bit must never decode as a valid telegram.

    A corrupted frame that slips through is worse than a lost one: it becomes a
    reply the station never sent. Flipping a bit inside the header changes the
    declared length, so that case surfaces as a length mismatch instead of a
    checksum mismatch. XBusIncompleteError cannot occur here - flipping a bit
    never shortens the buffer, and the shortest telegram is already
    MIN_TELEGRAM_LEN - but catching the parent keeps this law about "no corrupted
    frame decodes" rather than about which of the three faults fired. Which one
    fires is pinned in test_codec.py, by class and not by message text.
    """
    index = data.draw(st.integers(min_value=0, max_value=len(telegram) - 1))
    bit = data.draw(st.integers(min_value=0, max_value=7))
    corrupted = bytearray(telegram)
    corrupted[index] ^= 1 << bit
    if index > 0:
        assert xor(bytes(corrupted)) != 0
    with pytest.raises(XBusDecodeError):
        decode(bytes(corrupted))
```

- [ ] **Step 4: Run the three test files and watch them fail**

Run: `.venv/bin/python -m pytest tests/unit/test_address.py tests/unit/test_speed.py tests/unit/test_properties.py -v`

Expected: FAIL at collection —
`ModuleNotFoundError: No module named 'railctl.xbus.address'`

- [ ] **Step 5: Implement the address module**

```python
# src/railctl/xbus/address.py
"""Locomotive address wire form.

Below the threshold an address goes out as `(0x00, address)`. At or above it the
address is marked long: `value = address | 0xC000`, split into high and low
bytes. One function serves both dialects, because XpressNet's "add 0xC000 then
split" and Z21's "DB1 = 0xC0 | Adr_MSB" produce identical bytes for every
address in range - the ONLY difference between the dialects is the threshold,
100 or 128, and it arrives as one integer.

Measured on the YD7010: an address of 128 or above needs the 0xC0 marker on the
high byte. The 100..127 band, where the two dialects disagree, is documented
rather than measured on this hardware.

This module shifts an address by 8. That is not CV arithmetic, and the layering
grep for `>> 8` covers `station/`, `cli/` and `xbus/commands.py`, none of which
is this file.
"""

from __future__ import annotations

LOCO_ADDR_MIN = 1
LOCO_ADDR_MAX = 9999  # station limit; the wire field itself holds 14 bits

LONG_ADDRESS_FLAG = 0xC000
LONG_ADDRESS_MASK = 0x3FFF

_BYTE_MIN = 0
_BYTE_MAX = 255


def encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]:
    """Return `(adr_high, adr_low)` for a 1-based locomotive address."""
    if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
        raise ValueError(f"loco address {address} out of range {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}")
    value = address | LONG_ADDRESS_FLAG if address >= long_threshold else address
    return (value >> 8) & 0xFF, value & 0xFF


def decode_loco_address(adr_high: int, adr_low: int) -> int:
    """Recover a locomotive address from its two wire bytes.

    A high byte carrying exactly one of the two marker bits is refused rather
    than guessed at: no encoder produces that form, and turning it into a number
    would publish an address nothing ever sent.
    """
    for byte in (adr_high, adr_low):
        if not _BYTE_MIN <= byte <= _BYTE_MAX:
            raise ValueError(f"{byte} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")
    value = (adr_high << 8) | adr_low
    marker = value & LONG_ADDRESS_FLAG
    if marker == LONG_ADDRESS_FLAG:
        return value & LONG_ADDRESS_MASK
    if marker:
        raise ValueError(
            f"{adr_high:02X} {adr_low:02X} is not a locomotive address: the long marker "
            f"is 0x{LONG_ADDRESS_FLAG:04X}, both bits or neither"
        )
    return value
```

- [ ] **Step 6: Implement the speed module**

```python
# src/railctl/xbus/speed.py
"""128-step speed and direction.

Wire layout RVVVVVVV:

    0        braked stop
    1        emergency stop (reserved - no ordinary step may encode to it)
    2..127   steps 1..126
    bit 7    direction, 1 = forward

Because wire value 1 is reserved, an ordinary step is `step + 1`. Direction is
carried even on a stop, so stopping a locomotive does not erase which way it was
facing.

`Direction` is defined here once and re-exported by `railctl.station`; the CLI
parses it as `Direction[value.upper()]`. Defining it twice is how a REVERSE that
means 1 in one module and 0 in another gets shipped.
"""

from __future__ import annotations

import enum

SPEED_STEPS = 128
MAX_SPEED_STEP = 126
DRIVE_IDENT_128 = 0x13

DIRECTION_BIT = 0x80
SPEED_MASK = 0x7F
STOP_WIRE = 0x00
EMERGENCY_STOP_WIRE = 0x01
WIRE_STEP_OFFSET = 1

_BYTE_MIN = 0
_BYTE_MAX = 255


class Direction(enum.IntEnum):
    REVERSE = 0
    FORWARD = 1


def encode_speed_128(step: int, direction: Direction) -> int:
    """Encode a 0..126 speed step. Step 0 is a braked stop, not an emergency stop."""
    if not 0 <= step <= MAX_SPEED_STEP:
        raise ValueError(f"speed step {step} out of range 0..{MAX_SPEED_STEP}")
    wire = STOP_WIRE if step == 0 else step + WIRE_STEP_OFFSET
    return wire | (DIRECTION_BIT if direction is Direction.FORWARD else 0)


def encode_emergency_stop_128(direction: Direction) -> int:
    """The reserved wire value 1, with the direction bit still set correctly."""
    return EMERGENCY_STOP_WIRE | (DIRECTION_BIT if direction is Direction.FORWARD else 0)


def decode_speed_128(byte: int) -> tuple[int, Direction, bool]:
    """Return `(step, direction, emergency)` for a speed byte."""
    if not _BYTE_MIN <= byte <= _BYTE_MAX:
        raise ValueError(f"{byte} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")
    direction = Direction.FORWARD if byte & DIRECTION_BIT else Direction.REVERSE
    wire = byte & SPEED_MASK
    if wire == EMERGENCY_STOP_WIRE:
        return 0, direction, True
    if wire == STOP_WIRE:
        return 0, direction, False
    return wire - WIRE_STEP_OFFSET, direction, False
```

- [ ] **Step 7: Make the `ci` hypothesis profile reproducible**

`tests/conftest.py` line 44 currently reads:

```python
settings.register_profile("ci", max_examples=500, deadline=None, verbosity=Verbosity.normal)
```

Replace that single line with:

```python
# derandomize: a newly discovered example must not fail an unrelated CI run, and
# a mutation score computed from random draws is a sample rather than a
# measurement (docs/test-hardening.md).
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    derandomize=True,
    verbosity=Verbosity.normal,
)
```

No M2 task edits this line - Task 1 states that `tests/conftest.py` does not change
semantically - so this edit always applies. This is also the first task in the plan to touch
`tests/conftest.py`, so line 44 is still line 44. `test_the_ci_profile_is_derandomised` is the
check that the edit landed.

- [ ] **Step 8: Run the tests and see them pass**

Run: `.venv/bin/python -m pytest tests/unit/test_address.py tests/unit/test_speed.py tests/unit/test_properties.py -v`

Expected: PASS — 55 passed (23 in `test_address.py`, 25 in `test_speed.py`, 7 in
`test_properties.py`). The two exhaustive address tests walk 9999 addresses twice each
and still finish in well under a second.

- [ ] **Step 9: Run the property file under the CI profile**

Run: `HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest tests/unit/test_properties.py -q`

Expected: PASS — 7 passed, at 500 examples each with a fixed seed.
`test_the_loaded_profile_matches_the_environment` is the check that makes this step mean
something: it reads `settings.default`, the profile `load_profile()` actually installed,
so the run fails here if `tests/conftest.py` never loaded the `ci` profile. Comparing the
output of two runs would not have shown that - correct tests give identical output at 100
random examples and at 500 fixed ones, so both runs pass either way.

Then confirm the negative case, so the check is known to be able to fail:

Run: `.venv/bin/python -m pytest tests/unit/test_properties.py -q -k loaded_profile`

Expected: PASS — 1 passed. This is the same test under the `default` profile, where it now
asserts `derandomize is False` and `max_examples == 100`. If it passed identically under
both profiles, it would be reading the registered profile instead of the loaded one.

- [ ] **Step 10: Lint and format**

Run: `.venv/bin/python -m ruff check src/railctl/xbus tests/unit tests/conftest.py && .venv/bin/python -m ruff format --check src/railctl/xbus tests/unit tests/conftest.py`

Expected: `All checks passed!` with no findings.

- [ ] **Step 11: Commit**

```bash
git add src/railctl/xbus/address.py src/railctl/xbus/speed.py tests/unit/test_address.py tests/unit/test_speed.py tests/unit/test_properties.py tests/conftest.py
git commit -m "feat(xbus): add loco address and 128-step speed encoding"
```

---

### Task 6: CV number conversions - the single choke point

**Files:**
- Create: `src/railctl/xbus/cv.py`
- Test: `tests/unit/test_cv.py`

**Interfaces:**

- Consumes:
  - `railctl.xbus.dialect.CvEncoding` - `enum.Enum` with members `POM_ZERO_BASED`,
    `SERVICE_DIRECT`, `SERVICE_EXT`, `Z21_16BIT`
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` (tests only, to compose
    the golden telegrams from the fields this module produces)
  - `railctl.errors.CvOutOfRangeError(message: str, *, hint: str | None = None,
    cv: int | None = None)`, a subclass of `ProgrammingError`, mapped to exit code 15 in
    `EXIT_CODES`. M2 created the class and reserved the code; this task is the first and
    only place that raises it. Nothing else in the plan does, so if this module raised a
    bare `ValueError` instead, `railctl cv read 1025` would exit 1 with a Python traceback
    rather than the documented `railctl/error/v1` envelope with a stable `code`, and exit
    code 15 would never be reachable.
- Produces:
  - `railctl.xbus.cv.CvEncoding` - re-exported from `railctl.xbus.dialect`; the same
    object, so `railctl.xbus.cv.CvEncoding is railctl.xbus.dialect.CvEncoding`
  - `railctl.xbus.cv.CV_MIN: int = 1`, `railctl.xbus.cv.POM_CV_MIN: int = 1`
  - `railctl.xbus.cv.MAX_CV_POM: int = 1024`
  - `railctl.xbus.cv.MAX_CV_DIRECT: int = 255`
  - `railctl.xbus.cv.MAX_CV_EXT: int = 1024`
  - `railctl.xbus.cv.MAX_CV_Z21: int = 1024`
  - `railctl.xbus.cv.EXT_READ_OPCODES: tuple[int, ...] = (0x18, 0x19, 0x1A, 0x1B)`
  - `railctl.xbus.cv.EXT_WRITE_OPCODES: tuple[int, ...] = (0x1C, 0x1D, 0x1E, 0x1F)`
  - `railctl.xbus.cv.EXT_PAGE_SIZE: int = 256`
  - `railctl.xbus.cv.CV_FOR_PAGE0_ZERO: int = 1024`
  - `railctl.xbus.cv.SERVICE_RESULT_IDENTS: tuple[int, ...] = (0x14, 0x15, 0x16, 0x17)`
  - `railctl.xbus.cv.SERVICE_RESULT_IDENT_BASE: int = 0x14`
  - `railctl.xbus.cv.pom_cv_fields(cv: int) -> tuple[int, int]` returning `(MM, LSB)`
  - `railctl.xbus.cv.direct_cv_byte(cv: int) -> int`
  - `railctl.xbus.cv.ext_cv_fields(cv: int) -> tuple[int, int]` returning
    `(page_index 0..3, C byte)`; the opcode is `EXT_READ_OPCODES[page_index]` or
    `EXT_WRITE_OPCODES[page_index]`
  - `railctl.xbus.cv.z21_cv_fields(cv: int) -> tuple[int, int]` returning `(MSB, LSB)`
  - `railctl.xbus.cv.join_cv_field(msb: int, lsb: int) -> int`
  - `railctl.xbus.cv.decode_echo(encoding: CvEncoding, raw: int, *, page_index: int = 0) -> int`
  - `railctl.xbus.cv.echo_candidates(encoding: CvEncoding, cv: int, *, zero_based: bool | None = None) -> frozenset[int]`
  - `railctl.xbus.cv.resolve_service_cv(reply_ident: int, c: int) -> int`
  - Every function takes a **1-based** CV number and no function outside this module
    accepts or produces a wire CV address. Layering rule 2 - no `cv - 1`, `cv + 1`,
    `% 256`, `>> 8` or `<< 8` outside `xbus/cv.py` - is what makes that true, and this is
    the module the rule exempts.
  - **Two exception families, split by whose fault it is.** A bad *user CV number* -
    CV0, CV1025, CV256 on the direct opcodes - raises `CvOutOfRangeError`, because it
    reaches the CLI and needs the stable `code` and exit code 15. A bad *wire value* -
    a non-byte echo, an unknown reply ident, a page index outside 0..3 - raises plain
    `ValueError`, because it can only come from a caller inside this repo passing
    nonsense, and M2's rule is that internal argument validation is a `ValueError` mapped
    to exit code 2. Callers must be prepared for both.

- [ ] **Step 1: Write the failing test - the wire field tables**

```python
# tests/unit/test_cv.py
"""CV number conversions.

Four conventions live in one module, and they do not agree:

    POM      (E6 30)      ZERO-based   wire = cv - 1
    Z21      (23 11)      ZERO-based   wire = cv - 1
    direct   (22 15)      ONE-based    wire = cv, 1..255
    extended (22 18..1B)  ONE-based    band opcode + (cv & 0xFF)

Measured on the YD7010 (docs/probe-results.md): `23 11 00 07` reads CV8, and the
answer comes back as `63 14 08`. The request is zero-based and the echo is
one-based, on the same exchange. Routing a service-mode opcode through the
zero-based rule reads the CV next door and reports the value under the right
name - nothing in the output looks wrong.

No web summary states this correctly. These tables come from the hardware and
from Lenz 23151, and they are the reason this module exists at all.
"""

from __future__ import annotations

import pytest

from railctl.errors import CvOutOfRangeError, ProgrammingError, exit_code_for
from railctl.xbus import cv as cvmod
from railctl.xbus import dialect
from railctl.xbus.codec import encode
from railctl.xbus.cv import (
    CV_FOR_PAGE0_ZERO,
    EXT_READ_OPCODES,
    MAX_CV_DIRECT,
    MAX_CV_POM,
    MAX_CV_Z21,
    CvEncoding,
    decode_echo,
    direct_cv_byte,
    echo_candidates,
    ext_cv_fields,
    join_cv_field,
    pom_cv_fields,
    resolve_service_cv,
    z21_cv_fields,
)

POM_VECTORS = [
    (1, (0, 0x00)),
    (8, (0, 0x07)),
    (29, (0, 0x1C)),
    (255, (0, 0xFE)),
    (256, (0, 0xFF)),
    (257, (1, 0x00)),
    (265, (1, 0x08)),
    (1024, (3, 0xFF)),
]

DIRECT_VECTORS = [(1, 1), (8, 8), (29, 29), (255, 255)]

EXT_VECTORS = [
    (1, (0, 0x01)),
    (8, (0, 0x08)),
    (255, (0, 0xFF)),
    (256, (1, 0x00)),
    (257, (1, 0x01)),
    (265, (1, 0x09)),
    (511, (1, 0xFF)),
    (512, (2, 0x00)),
    (767, (2, 0xFF)),
    (768, (3, 0x00)),
    (1023, (3, 0xFF)),
    (1024, (0, 0x00)),
]

Z21_VECTORS = [
    (1, (0, 0x00)),
    (8, (0, 0x07)),
    (29, (0, 0x1C)),
    (256, (0, 0xFF)),
    (265, (1, 0x08)),
    (1024, (3, 0xFF)),
]


def test_the_cv_encoding_enum_is_the_dialect_one():
    """One enum, two import paths. Two enums would compare unequal in silence."""
    assert CvEncoding is dialect.CvEncoding


@pytest.mark.parametrize(("cv", "expected"), POM_VECTORS)
def test_pom_fields_are_zero_based(cv: int, expected: tuple[int, int]):
    assert pom_cv_fields(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), DIRECT_VECTORS)
def test_the_direct_byte_is_one_based(cv: int, expected: int):
    assert direct_cv_byte(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), EXT_VECTORS)
def test_extended_fields_are_one_based_and_band_relative(cv: int, expected: tuple[int, int]):
    assert ext_cv_fields(cv) == expected


@pytest.mark.parametrize(("cv", "expected"), Z21_VECTORS)
def test_z21_fields_are_zero_based_across_sixteen_bits(cv: int, expected: tuple[int, int]):
    assert z21_cv_fields(cv) == expected


def test_the_two_families_disagree_by_exactly_one_for_every_cv():
    """The property the probe pinned, here exhaustive rather than sampled.

    1024 iterations is cheaper than a hypothesis run and visits every boundary,
    including the two awkward ones: CV256, the first CV of band 1, and CV1024,
    which rides in band 0's vacant slot 0.
    """
    for cv in range(1, MAX_CV_POM + 1):
        zero_based = join_cv_field(*pom_cv_fields(cv))
        assert zero_based == join_cv_field(*z21_cv_fields(cv))
        assert zero_based == cv - 1
        page, c = ext_cv_fields(cv)
        one_based = CV_FOR_PAGE0_ZERO if (page, c) == (0, 0) else 256 * page + c
        assert one_based == cv
        assert one_based - zero_based == 1
        if cv <= MAX_CV_DIRECT:
            assert direct_cv_byte(cv) - zero_based == 1
```

- [ ] **Step 2: Write the failing test - range guards, echoes and golden telegrams**

Append to `tests/unit/test_cv.py`:

```python
@pytest.mark.parametrize(
    ("func", "cv"),
    [
        (pom_cv_fields, 0),
        (pom_cv_fields, -1),
        (pom_cv_fields, 1025),
        (direct_cv_byte, 0),
        (direct_cv_byte, 256),
        (direct_cv_byte, 1025),
        (ext_cv_fields, 0),
        (ext_cv_fields, -1),
        (ext_cv_fields, 1025),
        (z21_cv_fields, 0),
        (z21_cv_fields, -1),
        (z21_cv_fields, 1025),
    ],
)
def test_every_encoder_refuses_a_cv_outside_its_own_range(func, cv: int):
    """CvOutOfRangeError, not a bare ValueError.

    `railctl cv read 1025` has to exit with the documented `railctl/error/v1`
    envelope and exit code 15. A ValueError is not a RailctlError, so
    `exit_code_for` cannot map it and the command exits 1 with a traceback.
    """
    with pytest.raises(CvOutOfRangeError, match="CV") as excinfo:
        func(cv)
    assert excinfo.value.cv == cv


def test_a_cv_range_fault_carries_the_programming_error_exit_code():
    """The exit code reserved in M2 must actually be reachable from here."""
    with pytest.raises(CvOutOfRangeError) as excinfo:
        z21_cv_fields(1025)
    assert isinstance(excinfo.value, ProgrammingError)
    assert exit_code_for(excinfo.value) == 15


def test_cv256_is_refused_on_the_direct_opcode_and_says_why():
    """From station version 3.6 a bare C = 0 addresses CV1024, not CV256.

    The YD7010 reports 4.0, so sending C = 0 here would read a different CV and
    report it under the name of CV256. MAX_CV_DIRECT is 255 for that reason;
    CV256 and above go out on the extended or Z21 opcodes.
    """
    assert MAX_CV_DIRECT == 255
    with pytest.raises(CvOutOfRangeError, match="1024") as excinfo:
        direct_cv_byte(256)
    assert excinfo.value.cv == 256


@pytest.mark.parametrize(
    ("encoding", "raw", "page_index", "expected"),
    [
        (CvEncoding.POM_ZERO_BASED, 0, 0, 1),
        (CvEncoding.POM_ZERO_BASED, 7, 0, 8),
        (CvEncoding.POM_ZERO_BASED, 1023, 0, 1024),
        (CvEncoding.Z21_16BIT, 7, 0, 8),
        (CvEncoding.SERVICE_DIRECT, 1, 0, 1),
        (CvEncoding.SERVICE_DIRECT, 255, 0, 255),
        (CvEncoding.SERVICE_EXT, 0, 0, 1024),
        (CvEncoding.SERVICE_EXT, 1, 0, 1),
        (CvEncoding.SERVICE_EXT, 0, 1, 256),
        (CvEncoding.SERVICE_EXT, 9, 1, 265),
        (CvEncoding.SERVICE_EXT, 0, 2, 512),
        (CvEncoding.SERVICE_EXT, 0, 3, 768),
    ],
)
def test_decode_echo_inverts_each_encoding(
    encoding: CvEncoding, raw: int, page_index: int, expected: int
):
    """The extended inverse is NOT `raw or 256`.

    That fudge belongs to the legacy direct opcode. Used here it decodes CV256
    as 512, CV512 as 768 and CV768 as 1024 - three CVs a ZIMO backup touches,
    each silently wrong. `page_index` is supplied by the caller from the request
    it issued, because the reply alone cannot say which band it came from.
    """
    assert decode_echo(encoding, raw, page_index=page_index) == expected


def test_decode_echo_refuses_a_zero_on_the_direct_opcode():
    with pytest.raises(ValueError, match="raw 0"):
        decode_echo(CvEncoding.SERVICE_DIRECT, 0)


@pytest.mark.parametrize(
    ("encoding", "raw"),
    [
        (CvEncoding.POM_ZERO_BASED, 1024),
        (CvEncoding.POM_ZERO_BASED, 5000),
        (CvEncoding.Z21_16BIT, 1024),
        (CvEncoding.Z21_16BIT, 0xFFFF),
    ],
)
def test_decode_echo_refuses_a_wire_cv_past_the_encoding_maximum(
    encoding: CvEncoding, raw: int
):
    """The inverse is bounded by CV space, not by the width of the field.

    A 16-bit field holds 65536 values; POM and Z21 address 1024 CVs. Without
    this bound `decode_echo(POM_ZERO_BASED, 5000)` returns 5001 - a CV number
    outside every valid range, handed to the station layer as a legitimate
    result. Every other function in this module range-checks; this one must too,
    or it fabricates a plausible CV out of garbage, which is exactly the "wrong
    value under the right name" failure the module exists to prevent.
    """
    with pytest.raises(ValueError, match="not a wire CV"):
        decode_echo(encoding, raw)


def test_decode_echo_accepts_the_last_valid_wire_cv_of_each_encoding():
    """The bound is inclusive at 1023 -> CV1024, one below the field maximum."""
    assert decode_echo(CvEncoding.POM_ZERO_BASED, MAX_CV_POM - 1) == MAX_CV_POM
    assert decode_echo(CvEncoding.Z21_16BIT, MAX_CV_Z21 - 1) == MAX_CV_Z21


@pytest.mark.parametrize(
    ("reply_ident", "c", "expected"),
    [
        (0x14, 0, 1024),
        (0x14, 1, 1),
        (0x14, 8, 8),
        (0x14, 255, 255),
        (0x15, 0, 256),
        (0x15, 9, 265),
        (0x16, 0, 512),
        (0x17, 0, 768),
    ],
)
def test_resolve_service_cv_matches_the_measured_replies(reply_ident: int, c: int, expected: int):
    """`63 14 08` answered a read of CV8; `63 15 09` answered CV265.

    Lenz 23151 section 3.1.2.6: on `63 14`, C = 0 means CV1024 and C = 1..255
    means CV1..255. Not 0xFF for CV1024 - a plausible-sounding claim the document
    contradicts.
    """
    assert resolve_service_cv(reply_ident, c) == expected


def test_resolve_service_cv_refuses_an_unknown_ident_or_a_non_byte():
    with pytest.raises(ValueError, match="ident"):
        resolve_service_cv(0x13, 0)
    with pytest.raises(ValueError, match="not a byte"):
        resolve_service_cv(0x14, 256)


@pytest.mark.parametrize(
    ("encoding", "cv", "zero_based", "expected"),
    [
        (CvEncoding.POM_ZERO_BASED, 8, None, {7, 8}),
        (CvEncoding.POM_ZERO_BASED, 8, True, {7}),
        (CvEncoding.POM_ZERO_BASED, 8, False, {8}),
        (CvEncoding.POM_ZERO_BASED, 256, None, {255, 0}),
        (CvEncoding.SERVICE_DIRECT, 8, None, {8}),
        (CvEncoding.SERVICE_EXT, 265, None, {9}),
        (CvEncoding.SERVICE_EXT, 1024, None, {0}),
        (CvEncoding.Z21_16BIT, 8, None, {8}),
    ],
)
def test_echo_candidates_covers_the_forms_the_station_may_answer_with(
    encoding: CvEncoding, cv: int, zero_based: bool | None, expected: set[int]
):
    assert echo_candidates(encoding, cv, zero_based=zero_based) == frozenset(expected)


def test_a_z21_read_is_matched_against_the_one_based_echo_that_was_measured():
    """The request is zero-based, the echo is one-based. Both were measured.

    `23 11 00 07` -> `63 14 08` (CV8), `23 11 01 08` -> `63 15 09` (CV265).
    Matching a Z21 reply against the byte the *request* carried would reject
    every real answer, and a rejected answer is indistinguishable from silence -
    which is exactly how M1 concluded that the Lenz opcode family did not work.
    """
    assert echo_candidates(CvEncoding.Z21_16BIT, 8) == frozenset({8})
    assert echo_candidates(CvEncoding.Z21_16BIT, 29) == frozenset({29})
    assert echo_candidates(CvEncoding.Z21_16BIT, 250) == frozenset({250})
    assert echo_candidates(CvEncoding.Z21_16BIT, 265) == frozenset({9})
    assert echo_candidates(CvEncoding.Z21_16BIT, 266) == frozenset({10})
    # A `63 14..17` reply is resolved by band, never by decode_echo: the same
    # byte 8 means CV8 through resolve_service_cv and CV9 through the 16-bit
    # inverse, and only the first matches the hardware.
    assert resolve_service_cv(0x14, 8) == 8
    assert decode_echo(CvEncoding.Z21_16BIT, 8) == 9


def test_echo_candidates_alone_cannot_separate_two_cvs_in_different_bands():
    """The candidate byte narrows WITHIN a band. It does not identify the band.

    Pinned so that nobody reads `echo_candidates` as a complete matcher. The
    hardware separates these two exchanges only by the reply ident
    (docs/probe-results.md lines 148-152): `23 11 00 07` (CV8) is answered
    `63 14 08`, and `23 11 01 08` (CV265) is answered `63 15 09`. A matcher that
    compares the C byte alone accepts a `63 14 09` - which is CV9 - as the answer
    to a CV265 request and reports CV9's value under the name CV265. CV265 and
    CV266 are the ZIMO sound-project and master-volume CVs this tool backs up.
    """
    assert echo_candidates(CvEncoding.Z21_16BIT, 265) == echo_candidates(CvEncoding.Z21_16BIT, 9)
    assert echo_candidates(CvEncoding.POM_ZERO_BASED, 265) == echo_candidates(
        CvEncoding.POM_ZERO_BASED, 9
    )
    # The band is what separates them, and only resolve_service_cv reads it.
    assert resolve_service_cv(0x15, 9) == 265
    assert resolve_service_cv(0x14, 9) == 9


def test_join_cv_field_rebuilds_a_sixteen_bit_wire_value():
    assert join_cv_field(0x01, 0x08) == 264
    with pytest.raises(ValueError, match="not a byte"):
        join_cv_field(256, 0)


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\x22\x15\x01\x36"),
        (255, b"\x22\x15\xff\xc8"),
    ],
)
def test_the_direct_read_golden_telegrams(cv: int, telegram: bytes):
    assert encode(0x22, 0x15, direct_cv_byte(cv)) == telegram


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\x22\x18\x01\x3b"),
        (256, b"\x22\x19\x00\x3b"),
        (1024, b"\x22\x18\x00\x3a"),
    ],
)
def test_the_extended_read_golden_telegrams(cv: int, telegram: bytes):
    """`22 18 00` is CV1024, not CV256. `22 19 00` is CV256."""
    page, c = ext_cv_fields(cv)
    assert encode(0x22, EXT_READ_OPCODES[page], c) == telegram


@pytest.mark.parametrize(
    ("cv", "telegram"),
    [
        (1, b"\xe6\x30\x00\x03\xe4\x00\x00\x31"),
        (8, b"\xe6\x30\x00\x03\xe4\x07\x00\x36"),
        (257, b"\xe6\x30\x00\x03\xe5\x00\x00\x30"),
    ],
)
def test_the_pom_read_golden_telegrams(cv: int, telegram: bytes):
    """CV8 goes out as 07: POM is zero-based. CV257 pushes MM into the option byte."""
    mm, lsb = pom_cv_fields(cv)
    assert encode(0xE6, 0x30, 0x00, 0x03, 0xE4 | mm, lsb, 0x00) == telegram


def test_the_z21_read_golden_telegram():
    msb, lsb = z21_cv_fields(29)
    assert encode(0x23, 0x11, msb, lsb) == b"\x23\x11\x00\x1c\x2e"
    assert MAX_CV_Z21 == 1024
    assert cvmod.MAX_CV_POM == 1024
```

- [ ] **Step 3: Run the test and watch it fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cv.py -v`

Expected: FAIL at collection — `ModuleNotFoundError: No module named 'railctl.xbus.cv'`

- [ ] **Step 4: Implement the module head - constants and range guard**

```python
# src/railctl/xbus/cv.py
"""CV number conversions - the single choke point.

Every function here takes a 1-based user CV number. No function anywhere else in
railctl accepts or produces a wire CV address; the layering test greps
`station/`, `cli/` and `xbus/commands.py` for `cv - 1`, `cv + 1`, `% 256`,
`>> 8` and `<< 8`, and this module is the one place they are allowed.

Four conventions, measured on the YaMoRC YD7010 with a ZIMO MS450P22
(docs/probe-results.md):

    Encoding        Wire formula                              Valid CV
    POM_ZERO_BASED  w = cv - 1; MM = w >> 8; LSB = w & 0xFF   1..1024
    SERVICE_DIRECT  byte = cv                                 1..255
    SERVICE_EXT     page = cv // 256; C = cv & 0xFF           1..1023 (+1024 -> 0, 0)
    Z21_16BIT       w = cv - 1; MSB = w >> 8; LSB = w & 0xFF   1..1024

The zero-based rule applies to POM and Z21 ONLY. `23 11 00 07` reads CV8; its
answer comes back as `63 14 08`. The request is zero-based, the echo one-based,
on the same exchange, and no web summary of the protocol states this correctly.

Getting it wrong is silent. The wrong CV is read and its value is reported under
the right name; once a write path exists, the same mistake writes to the wrong
CV.

Two exception families live here, split by whose mistake it is:

* a bad 1-based USER CV number (CV0, CV1025, CV256 on the direct opcodes) raises
  `CvOutOfRangeError`. That value came from the command line, so it needs the
  stable `code` in the `railctl/error/v1` envelope and exit code 15;
* a bad WIRE value (a non-byte echo, an unknown reply ident, a page index
  outside 0..3) raises plain `ValueError`. Only code inside this repo can pass
  one, and M2's rule is that internal argument validation is a `ValueError`,
  which `cli/_errors.py` reports as usage exit code 2.
"""

from __future__ import annotations

from railctl.errors import CvOutOfRangeError
from railctl.xbus.dialect import CvEncoding

__all__ = [
    "CV_FOR_PAGE0_ZERO",
    "CV_MIN",
    "EXT_PAGE_SIZE",
    "EXT_READ_OPCODES",
    "EXT_WRITE_OPCODES",
    "MAX_CV_DIRECT",
    "MAX_CV_EXT",
    "MAX_CV_POM",
    "MAX_CV_Z21",
    "POM_CV_MIN",
    "SERVICE_RESULT_IDENTS",
    "SERVICE_RESULT_IDENT_BASE",
    "CvEncoding",
    "decode_echo",
    "direct_cv_byte",
    "echo_candidates",
    "ext_cv_fields",
    "join_cv_field",
    "pom_cv_fields",
    "resolve_service_cv",
    "z21_cv_fields",
]

CV_MIN = 1
POM_CV_MIN = CV_MIN  # the design names this one explicitly; same value

MAX_CV_POM = 1024
# 255, not 256. Lenz 23151 sections 3.2.6 and 3.2.14: from station version 3.6
# onward a C of 0 on the legacy direct opcodes addresses CV1024, not CV256. The
# YD7010 reports 4.0, so sending C = 0 would touch the wrong CV with no error.
MAX_CV_DIRECT = 255
MAX_CV_EXT = 1024
MAX_CV_Z21 = 1024

EXT_READ_OPCODES = (0x18, 0x19, 0x1A, 0x1B)
EXT_WRITE_OPCODES = (0x1C, 0x1D, 0x1E, 0x1F)
EXT_PAGE_SIZE = 256
CV_FOR_PAGE0_ZERO = 1024

# `63 14/15/16/17 C V` - the service-mode result bands (Lenz 23151, 3.1.2.6).
SERVICE_RESULT_IDENT_BASE = 0x14
SERVICE_RESULT_IDENTS = (0x14, 0x15, 0x16, 0x17)

_BYTE_MIN = 0
_BYTE_MAX = 255

# The largest 1-based CV each zero-based encoding can address, used to bound the
# INVERSE in decode_echo. The 16-bit wire field is not the bound: it holds 65536
# values while these encodings address 1024 CVs, and an unbounded inverse turns
# a garbage echo into a plausible-looking CV number.
_ZERO_BASED_MAXIMA = {
    CvEncoding.POM_ZERO_BASED: MAX_CV_POM,
    CvEncoding.Z21_16BIT: MAX_CV_Z21,
}


def _check_range(cv: int, maximum: int, what: str) -> None:
    """Guard a 1-based USER CV number.

    CvOutOfRangeError rather than ValueError: this value comes from the command
    line, and only a RailctlError reaches `exit_code_for`, which maps this class
    to exit code 15. A bare ValueError would exit 1 with a traceback instead of
    the documented `railctl/error/v1` envelope, and code 15 - reserved in M2 -
    would never be produced by anything.
    """
    if not CV_MIN <= cv <= maximum:
        raise CvOutOfRangeError(f"CV {cv} outside the {what} range {CV_MIN}..{maximum}", cv=cv)


def _check_byte(value: int, what: str) -> None:
    """Guard a WIRE byte. ValueError: only in-repo code can pass a non-byte."""
    if not _BYTE_MIN <= value <= _BYTE_MAX:
        raise ValueError(f"{what} {value} is not a byte in {_BYTE_MIN}..{_BYTE_MAX}")
```

- [ ] **Step 5: Implement the four field encoders**

Append to `src/railctl/xbus/cv.py`:

```python
def pom_cv_fields(cv: int) -> tuple[int, int]:
    """POM (E6 30) is ZERO-based: CV1 goes on the wire as 0.

    Returns `(MM, LSB)`. MM is the top two bits of the zero-based value and is
    OR-ed into the option byte by the caller: `0xE4 | MM`.
    """
    _check_range(cv, MAX_CV_POM, "POM")
    wire = cv - 1
    return (wire >> 8) & 0x03, wire & 0xFF


def direct_cv_byte(cv: int) -> int:
    """Legacy direct (22 15 / 23 16) is ONE-based: CV1 goes on the wire as 1.

    Routing this through the zero-based rule reads the CV next door. CV256 and
    above are refused: from station version 3.6 a C of 0 means CV1024 here.
    """
    if cv == MAX_CV_DIRECT + 1:
        raise CvOutOfRangeError(
            f"CV {cv} cannot be addressed with the direct opcodes: from station "
            f"version 3.6 a C of 0 means CV1024, not CV256. Use the extended or "
            f"Z21 opcodes.",
            cv=cv,
        )
    _check_range(cv, MAX_CV_DIRECT, "direct")
    return cv


def ext_cv_fields(cv: int) -> tuple[int, int]:
    """Extended (22 18..1B / 23 1C..1F) is ONE-based and band-relative.

    Returns `(page_index, C)`, where the opcode is `EXT_READ_OPCODES[page_index]`
    or `EXT_WRITE_OPCODES[page_index]`:

        page 0  CV1..255 at their own numbers, and CV1024 at C = 0
        page 1  CV256..511    page 2  CV512..767    page 3  CV768..1023

    Bands 1..3 are 256 wide and aligned, so `cv & 0xFF` is exactly
    `cv - 256 * page` for them, and the identity for band 0.
    """
    if cv == CV_FOR_PAGE0_ZERO:
        # CV1024 rides band 0's vacant slot 0, so it stays reachable even though
        # cv // 256 would put it past the last band.
        return 0, 0
    _check_range(cv, MAX_CV_EXT - 1, f"extended (CV{CV_FOR_PAGE0_ZERO} excepted)")
    return cv // EXT_PAGE_SIZE, cv & 0xFF


def z21_cv_fields(cv: int) -> tuple[int, int]:
    """Z21 (23 11 / 24 12) is ZERO-based across a full 16-bit CV field."""
    _check_range(cv, MAX_CV_Z21, "Z21")
    wire = cv - 1
    return (wire >> 8) & 0xFF, wire & 0xFF


def join_cv_field(msb: int, lsb: int) -> int:
    """Rebuild the 16-bit wire CV of a `64 14` reply from its two bytes."""
    _check_byte(msb, "CV MSB")
    _check_byte(lsb, "CV LSB")
    return (msb << 8) | lsb
```

- [ ] **Step 6: Implement the three inverse helpers**

Append to `src/railctl/xbus/cv.py`:

```python
def decode_echo(encoding: CvEncoding, raw: int, *, page_index: int = 0) -> int:
    """Turn the CV a reply echoed back into a 1-based CV number.

    `raw` is the 16-bit wire field for POM and Z21 (see `join_cv_field`) and the
    single C byte for the direct and extended opcodes.

    The extended inverse is NOT `raw or 256`. That fudge belongs to the legacy
    direct opcode; used here it decodes CV256 as 512, CV512 as 768 and CV768 as
    1024. `page_index` comes from the request the caller issued, because the
    reply on its own cannot say which band it belongs to.

    Every branch is bounded by the CV space of its own encoding, not by the width
    of the wire field. The 16-bit field holds 65536 values; POM and Z21 address
    1024 CVs. An inverse bounded only by the field would turn `raw = 5000` into
    CV5001 - a number outside every valid range - and hand it to the station
    layer as a legitimate result, which is the "wrong value under the right name"
    failure this whole module exists to prevent.
    """
    limit = _ZERO_BASED_MAXIMA.get(encoding)
    if limit is not None:
        if not 0 <= raw <= limit - 1:
            raise ValueError(f"echo {raw} is not a wire CV in 0..{limit - 1}")
        return raw + 1
    _check_byte(raw, "echo")
    if encoding is CvEncoding.SERVICE_DIRECT:
        if raw == 0:
            raise ValueError("raw 0 is not a direct-mode CV echo")
        return raw
    if not 0 <= page_index < len(EXT_READ_OPCODES):
        raise ValueError(f"page index {page_index} outside 0..{len(EXT_READ_OPCODES) - 1}")
    if page_index == 0 and raw == 0:
        return CV_FOR_PAGE0_ZERO
    return EXT_PAGE_SIZE * page_index + raw


def echo_candidates(
    encoding: CvEncoding, cv: int, *, zero_based: bool | None = None
) -> frozenset[int]:
    """Every echo byte that could legitimately answer a request for `cv`.

    This exists so that no comparison logic anywhere else has to do CV
    arithmetic. It is NOT a complete matcher, and the caller must not use it as
    one.

    **The returned byte narrows only WITHIN one band; it cannot separate bands.**
    Two CVs 256 apart share a candidate set: `echo_candidates(Z21_16BIT, 265)`
    and `echo_candidates(Z21_16BIT, 9)` are both `{9}`, and the POM pair is both
    `{8, 9}`. A `63 14..17` reply MUST therefore be resolved with
    `resolve_service_cv(reply_ident, c)` first, because the ident is the only
    thing that carries the band: measured on the hardware,
    `23 11 00 07` (CV8) is answered `63 14 08` and `23 11 01 08` (CV265) is
    answered `63 15 09` (docs/probe-results.md). A matcher that compares the C
    byte alone accepts a `63 14 09` - CV9 - as the answer to a CV265 request and
    reports CV9's value under the name CV265. CV265 and CV266 are the ZIMO
    sound-project and master-volume CVs this tool backs up.

    For POM the station's echo convention is not settled on this hardware - no
    POM result has ever come back (docs/probe-results.md, R1) - so `None` returns
    BOTH forms and lets `Capabilities.pom_echo_zero_based` narrow it once a real
    reply is seen. A matcher that guessed one form would drop the first genuine
    reply, and a dropped reply reads as silence, which reads as "unsupported".

    For the service-mode encodings the echo is the ONE-based band byte, measured:
    `23 11 00 07` (CV8) is answered `63 14 08`, and `23 11 01 08` (CV265) is
    answered `63 15 09`. That holds for Z21 requests too, even though the request
    itself is zero-based.
    """
    if encoding is CvEncoding.POM_ZERO_BASED:
        _check_range(cv, MAX_CV_POM, "POM")
        zero_form = (cv - 1) & 0xFF
        one_form = cv & 0xFF
        if zero_based is True:
            return frozenset({zero_form})
        if zero_based is False:
            return frozenset({one_form})
        return frozenset({zero_form, one_form})
    if encoding is CvEncoding.SERVICE_DIRECT:
        return frozenset({direct_cv_byte(cv)})
    return frozenset({ext_cv_fields(cv)[1]})


def resolve_service_cv(reply_ident: int, c: int) -> int:
    """`63 14..17 C V` -> the 1-based CV the station answered about.

    Lenz 23151 section 3.1.2.6: on `63 14`, C = 0 means CV1024 and C = 1..255
    means CV1..255; `63 15/16/17` carry 256/512/768 plus C. Measured: `63 15 09`
    is CV265, which is where the ZIMO CVs this tool backs up begin.
    """
    if reply_ident not in SERVICE_RESULT_IDENTS:
        raise ValueError(f"reply ident 0x{reply_ident:02X} is not a service-result ident")
    _check_byte(c, "C")
    page = reply_ident - SERVICE_RESULT_IDENT_BASE
    if page == 0 and c == 0:
        return CV_FOR_PAGE0_ZERO
    return EXT_PAGE_SIZE * page + c
```

- [ ] **Step 7: Run the test and see it pass**

Run: `.venv/bin/python -m pytest tests/unit/test_cv.py -v`

Expected: PASS — 93 passed.

- [ ] **Step 8: Run the whole unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q`

Expected: PASS — 240 passed. That is 197 from this part (49 from Task 4, 55 from Task 5,
93 from Task 6) on top of the 43 Task 1 and Task 2 already put in `tests/unit/`
(7 in `test_version.py`, 36 in `test_exit_codes.py`).

- [ ] **Step 9: Lint and format**

Run: `.venv/bin/python -m ruff check src/railctl/xbus tests/unit && .venv/bin/python -m ruff format --check src/railctl/xbus tests/unit`

Expected: `All checks passed!` with no findings.

- [ ] **Step 10: Commit**

```bash
git add src/railctl/xbus/cv.py tests/unit/test_cv.py
git commit -m "feat(xbus): add the CV number conversion choke point"
```

---

## Part M3b - command encoders, reply parsing, and the golden vector table

These three tasks finish the pure X-Bus layer. Nothing here opens a file descriptor, sleeps, or
knows what a port is; every function is a total function from arguments to bytes or from bytes to
a typed object.

Two rules from the design bind all three tasks and are stated here once so each task can refer to
them by name:

- **Layering rule 2** (spec line 95): no CV-number arithmetic — `cv - 1`, `cv + 1`, `% 256`,
  `>> 8`, `<< 8` — outside `src/railctl/xbus/cv.py`. `commands.py` is named explicitly in the grep
  guard. Every CV wire field in Task 7 therefore comes back from a `cv.py` function, and the one
  place `replies.py` needs to join two CV bytes (Task 8) calls `cv.join_cv_field` rather than
  shifting them itself.
- **The defining failure mode.** A reply form the parser does not recognise is indistinguishable
  from no reply at all, and the layer above reads silence as "the hardware cannot do this". During
  M1 that produced four confident, wrong conclusions. `parse` therefore never raises, never
  invents a typed reply from an unrelated telegram, and returns an `Other` — which is an *unknown*,
  carrying the bytes and a `reason` naming which kind of unknown, and is not the same thing as
  silence.

One scope note, so nobody has to guess whether it was forgotten: the `E3 09` request and its
`E3 52` reply, which carry the F13..F28 state, are **in this section**, in Tasks 7 and 8. Nothing
here is deferred. `LocoInfo` stops at F12, and the group commands `E4 23` / `E4 28` write all eight
bits of their group at once, so without this pair the station would have to seed zeros and blind
clear F13-F28 — the side effect `docs/probe-results.md` records as closed and spec line 1551 calls
the failure "most likely to bite in practice".

---

### Task 7: X-Bus command encoders (`xbus/commands.py`)

**Files:**
- Create: `src/railctl/xbus/commands.py`
- Test: `tests/unit/test_xbus_commands.py`
- Modify: none. This task adds no re-export to `src/railctl/xbus/__init__.py`; that file stays
  exactly as Task 4 left it. Every consumer imports `railctl.xbus.commands` by module path.

**Interfaces:**

- Consumes (all defined by Tasks 4–6, spec lines 318–427):
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` — appends the XOR and raises
    `XBusEncodeError` when the number of data bytes disagrees with `header & 0x0F`.
  - `railctl.xbus.address.encode_loco_address(address: int, *, long_threshold: int) -> tuple[int, int]`
    — returns `(adr_high, adr_low)`; raises `ValueError` outside 1..9999.
  - `railctl.xbus.speed.Direction` (`IntEnum`, `REVERSE = 0`, `FORWARD = 1`),
    `railctl.xbus.speed.encode_speed_128(step: int, direction: Direction) -> int`,
    `railctl.xbus.speed.DRIVE_IDENT_128` (`= 0x13`).
  - `railctl.xbus.cv.pom_cv_fields(cv: int) -> tuple[int, int]` — `(MM, LSB)`,
    `railctl.xbus.cv.direct_cv_byte(cv: int) -> int`,
    `railctl.xbus.cv.ext_cv_fields(cv: int) -> tuple[int, int]` — `(page 0..3, C byte)`,
    `railctl.xbus.cv.z21_cv_fields(cv: int) -> tuple[int, int]` — `(MSB, LSB)`,
    `railctl.xbus.cv.EXT_READ_OPCODES` (`= (0x18, 0x19, 0x1A, 0x1B)`),
    `railctl.xbus.cv.EXT_WRITE_OPCODES` (`= (0x1C, 0x1D, 0x1E, 0x1F)`).
  - `railctl.xbus.dialect.XPRESSNET` and `railctl.xbus.dialect.Z21`, each a frozen `Dialect` with
    `.long_address_threshold` (100 and 128).
- Produces:
  - `class FunctionGroup(enum.IntEnum)` with `G1 = 0x20`, `G2 = 0x21`, `G3 = 0x22`, `G4 = 0x23`,
    `G5 = 0x28`
  - `MAX_FUNCTION: int = 28`
  - `FUNCTION_BITS: dict[int, tuple[FunctionGroup, int]]` — function index to `(group, bit)`
  - `GROUP_FUNCTIONS: dict[FunctionGroup, tuple[int, ...]]`
  - `pack_function_bits(group: FunctionGroup, state: Mapping[int, bool]) -> int`
  - `cmd_station_version() -> bytes`, `cmd_station_status() -> bytes`,
    `cmd_track_power_on() -> bytes`, `cmd_track_power_off() -> bytes`,
    `cmd_emergency_stop_all() -> bytes`
  - `cmd_emergency_stop_loco(address: int, *, threshold: int) -> bytes`
  - `cmd_drive_128(address: int, step: int, direction: Direction, *, threshold: int) -> bytes`
  - `cmd_function_group(address: int, group: FunctionGroup, bits: int, *, threshold: int) -> bytes`
  - `cmd_loco_info(address: int, *, threshold: int) -> bytes`
  - `cmd_function_state_13_28(address: int, *, threshold: int) -> bytes` — the `E3 09` request whose
    `E3 52` reply Task 8 parses. Without it there is no reply form anywhere in this section from
    which an F13..F20 or F21..F28 state map can be built (`LocoInfo` stops at F12), and the station
    would be forced back to seeding zeros and blind-clearing F13-F28 — the side effect
    `docs/probe-results.md` records as closed and spec line 1551 calls the failure "most likely to
    bite in practice".
  - `cmd_service_direct_read(cv: int) -> bytes`, `cmd_service_direct_write(cv: int, value: int) -> bytes`
  - `cmd_service_ext_read(cv: int) -> bytes`, `cmd_service_ext_write(cv: int, value: int) -> bytes`
  - `cmd_z21_cv_read(cv: int) -> bytes`, `cmd_z21_cv_write(cv: int, value: int) -> bytes`
  - `cmd_service_result_request() -> bytes`
  - `cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes`
  - `cmd_pom_write_byte(address: int, cv: int, value: int, *, threshold: int) -> bytes`
  - `cmd_pom_write_bit(address: int, cv: int, bit: int, value: bool, *, threshold: int) -> bytes`
  - `class TimeoutClass(enum.Enum)` with `NORMAL = "normal"`, `PROGRAMMING = "programming"`
  - `timeout_class(telegram: bytes) -> TimeoutClass`
  - `PROGRAMMING_TELEGRAMS: frozenset[tuple[int, int]]`
  - `MAX_BIT_INDEX: int = 7`, `MIN_BYTE_VALUE: int = 0`, `MAX_BYTE_VALUE: int = 255`

Note on the test path: the design calls this file `tests/test_commands.py`. Task 1 fixed the test
layout for the whole plan - `tests/` is a package, the M1 probe suite moved to `tests/probe/`, and
`tests/{unit,station,cli,hardware}` all carry `__init__.py` - so railctl unit tests go in
`tests/unit/`, next to the ones Tasks 4-6 wrote. The `xbus_` in the name is not there to avoid a
basename clash (the `__init__.py` files already make `tests.probe.test_commands` and
`tests.unit.test_xbus_commands` distinct module names); it is there so a line of pytest output
says which suite failed without the reader having to check the directory.

- [ ] **Step 1: Write the failing tests for the function-bit tables**

```python
# tests/unit/test_xbus_commands.py
"""Golden byte vectors and behaviour tests for the X-Bus command encoders.

Every expected telegram in this file is a literal from the design document. An
encoder change that is intentional edits one row here; an encoder change that is
not, fails one row here.
"""

from __future__ import annotations

import pytest

from railctl.xbus.commands import (
    FUNCTION_BITS,
    GROUP_FUNCTIONS,
    MAX_FUNCTION,
    FunctionGroup,
    pack_function_bits,
)


def test_every_function_from_f0_to_f28_has_a_group_and_a_bit():
    assert sorted(FUNCTION_BITS) == list(range(0, MAX_FUNCTION + 1))


def test_f0_lives_in_bit_4_of_group_1_not_bit_0():
    """F0 is the headlight and it does NOT sit where F1 sits. The E4 20 byte is
    000 F0 F4 F3 F2 F1, so bit 4 is the headlight and bit 0 is F1."""
    assert FUNCTION_BITS[0] == (FunctionGroup.G1, 4)
    assert FUNCTION_BITS[1] == (FunctionGroup.G1, 0)


@pytest.mark.parametrize(
    ("group", "functions"),
    [
        (FunctionGroup.G1, (0, 1, 2, 3, 4)),
        (FunctionGroup.G2, (5, 6, 7, 8)),
        (FunctionGroup.G3, (9, 10, 11, 12)),
        (FunctionGroup.G4, (13, 14, 15, 16, 17, 18, 19, 20)),
        (FunctionGroup.G5, (21, 22, 23, 24, 25, 26, 27, 28)),
    ],
)
def test_each_group_owns_exactly_its_documented_functions(group, functions):
    assert GROUP_FUNCTIONS[group] == functions


def test_no_two_functions_in_one_group_share_a_bit():
    for group, functions in GROUP_FUNCTIONS.items():
        bits = [FUNCTION_BITS[f][1] for f in functions]
        assert len(set(bits)) == len(bits), group


@pytest.mark.parametrize(
    ("group", "state", "expected"),
    [
        (FunctionGroup.G1, {0: True, 1: False, 2: False, 3: False, 4: False}, 0x10),
        (FunctionGroup.G1, {0: False, 1: True, 2: False, 3: False, 4: False}, 0x01),
        (FunctionGroup.G1, {0: True, 1: True, 2: True, 3: True, 4: True}, 0x1F),
        (FunctionGroup.G2, {5: True, 6: False, 7: False, 8: False}, 0x01),
        (FunctionGroup.G3, {9: True, 10: False, 11: False, 12: False}, 0x01),
        (FunctionGroup.G4, dict.fromkeys(range(13, 21), False) | {13: True}, 0x01),
        (FunctionGroup.G5, dict.fromkeys(range(21, 29), False) | {21: True}, 0x01),
        (FunctionGroup.G5, dict.fromkeys(range(21, 29), True), 0xFF),
    ],
)
def test_pack_function_bits_matches_the_wire_layout(group, state, expected):
    assert pack_function_bits(group, state) == expected


def test_pack_function_bits_ignores_functions_belonging_to_other_groups():
    """The station holds one shadow map for all 29 functions and hands the whole
    map to each group in turn."""
    whole_shadow = dict.fromkeys(range(0, MAX_FUNCTION + 1), False) | {0: True, 5: True}
    assert pack_function_bits(FunctionGroup.G1, whole_shadow) == 0x10
    assert pack_function_bits(FunctionGroup.G2, whole_shadow) == 0x01


def test_pack_function_bits_refuses_a_state_missing_a_function_of_the_group():
    """E4 20 writes all five bits at once, so a caller that supplies only F0
    would silently switch F1-F4 off. Treating a missing key as False is the
    'absence read as a negative fact' failure this project keeps producing, so
    it is an error instead."""
    with pytest.raises(ValueError, match="missing"):
        pack_function_bits(FunctionGroup.G1, {0: True})


def test_pack_function_bits_refuses_a_function_index_that_does_not_exist():
    state = dict.fromkeys(range(0, 5), False) | {29: True}
    with pytest.raises(ValueError, match="out of range"):
        pack_function_bits(FunctionGroup.G1, state)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'railctl.xbus.commands'`

- [ ] **Step 3: Create the module with the headers, the function tables and `pack_function_bits`**

```python
# src/railctl/xbus/commands.py
"""X-Bus command encoders.

Every function returns a complete telegram - header, data bytes, XOR - with no
framing prefix. The FF FE prefix belongs to the envelope and is never part of
the XOR.

CV numbers are 1-based on the way in, for every function here, and this module
does NO CV arithmetic at all: every wire field comes back from `xbus.cv`, the
single place where a 1-based CV becomes a wire value. The layering test greps
this file for CV arithmetic: subtraction or addition of one against a CV,
eight-bit shifts, and modulo 256. That sentence deliberately names the patterns
instead of spelling them, because a docstring that spells them is itself a match
and would turn the guard red on correct code.

The wire conventions are not uniform, and that is the most dangerous detail in
the module:

- POM (E6 30) and the Z21 opcodes (23 11 / 24 12) are ZERO-BASED: CV1 goes out
  as 0.
- The legacy direct opcodes (22 15 / 23 16) and the extended opcodes
  (22 18..1B / 23 1C..1F) are ONE-BASED: CV1 goes out as 1.

`xbus.cv` owns both rules. The encoders below only say which one they want.
Routing a service-mode opcode through the POM rule reads the wrong CV off the
decoder and reports it under the right name.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

from railctl.xbus.address import encode_loco_address
from railctl.xbus.codec import encode
from railctl.xbus.cv import (
    EXT_READ_OPCODES,
    EXT_WRITE_OPCODES,
    direct_cv_byte,
    ext_cv_fields,
    pom_cv_fields,
    z21_cv_fields,
)
from railctl.xbus.speed import DRIVE_IDENT_128, Direction, encode_speed_128

# X-Bus request headers. The low nibble is the data-byte count, which is why one
# opcode family appears under three headers: 22 15 (read, two data bytes),
# 23 16 (write, three), 24 12 (Z21 write with a value, four).
REQ_1_DATA = 0x21
REQ_2_DATA = 0x22
REQ_3_DATA = 0x23
REQ_4_DATA = 0x24

DB_VERSION = 0x21
DB_STATION_STATUS = 0x24
DB_POWER_ON = 0x81
DB_POWER_OFF = 0x80
DB_SERVICE_RESULT = 0x10
DB_DIRECT_READ = 0x15
DB_DIRECT_WRITE = 0x16
DB_Z21_READ = 0x11
DB_Z21_WRITE = 0x12

OP_EMERGENCY_STOP_ALL = 0x80
OP_EMERGENCY_STOP_LOCO = 0x92
OP_LOCO_INFO = 0xE3
DB_LOCO_INFO = 0x00
# E3 09 AH AL X asks for the F13..F28 ON/OFF state (Lenz 23151 section 3.1.9.2).
# Measured 2026-08-04: the YD7010 answers E3 52 D1 D2. This is the only way to
# learn F13..F28; the E4 loco-info reply stops at F12.
DB_FUNCTION_STATE_13_28 = 0x09
OP_LOCO_DRIVE = 0xE4
OP_POM = 0xE6
DB_POM = 0x30

POM_READ_BYTE_BASE = 0xE4
POM_WRITE_BYTE_BASE = 0xEC
POM_WRITE_BIT_BASE = 0xE8
POM_UNUSED_BYTE = 0x00
POM_BIT_VALUE_SHIFT = 3

MAX_BIT_INDEX = 7
MIN_BYTE_VALUE = 0
MAX_BYTE_VALUE = 255
MAX_FUNCTION = 28


class FunctionGroup(enum.IntEnum):
    """The E4 sub-opcode that carries each block of functions.

    G4 and G5 need command station version 3.6 or later. The YD7010 reports 4.0
    and both were accepted on hardware (docs/probe-results.md, 2026-08-04), but
    that is a probed capability, not an assumption this module makes.
    """

    G1 = 0x20  # F0..F4
    G2 = 0x21  # F5..F8
    G3 = 0x22  # F9..F12
    G4 = 0x23  # F13..F20
    G5 = 0x28  # F21..F28


# Written out one entry at a time rather than generated, because the F0 row is
# the irregular one and a generator would hide it: F0 is bit 4 of the group 1
# byte, and F1 is bit 0. The byte is 000 F0 F4 F3 F2 F1.
FUNCTION_BITS: dict[int, tuple[FunctionGroup, int]] = {
    0: (FunctionGroup.G1, 4),
    1: (FunctionGroup.G1, 0),
    2: (FunctionGroup.G1, 1),
    3: (FunctionGroup.G1, 2),
    4: (FunctionGroup.G1, 3),
    5: (FunctionGroup.G2, 0),
    6: (FunctionGroup.G2, 1),
    7: (FunctionGroup.G2, 2),
    8: (FunctionGroup.G2, 3),
    9: (FunctionGroup.G3, 0),
    10: (FunctionGroup.G3, 1),
    11: (FunctionGroup.G3, 2),
    12: (FunctionGroup.G3, 3),
    13: (FunctionGroup.G4, 0),
    14: (FunctionGroup.G4, 1),
    15: (FunctionGroup.G4, 2),
    16: (FunctionGroup.G4, 3),
    17: (FunctionGroup.G4, 4),
    18: (FunctionGroup.G4, 5),
    19: (FunctionGroup.G4, 6),
    20: (FunctionGroup.G4, 7),
    21: (FunctionGroup.G5, 0),
    22: (FunctionGroup.G5, 1),
    23: (FunctionGroup.G5, 2),
    24: (FunctionGroup.G5, 3),
    25: (FunctionGroup.G5, 4),
    26: (FunctionGroup.G5, 5),
    27: (FunctionGroup.G5, 6),
    28: (FunctionGroup.G5, 7),
}

GROUP_FUNCTIONS: dict[FunctionGroup, tuple[int, ...]] = {
    group: tuple(f for f, (g, _) in FUNCTION_BITS.items() if g is group)
    for group in FunctionGroup
}


def pack_function_bits(group: FunctionGroup, state: Mapping[int, bool]) -> int:
    """Pack one group's byte. `state` must carry every function in the group.

    E4 20/21/22/23/28 sets EVERY function in its group in one telegram, so a
    caller that supplies only the function it wants to change switches the other
    four off. Defaulting a missing key to False would make that silent - the
    same shape as the failure this project keeps producing, an absence read as a
    negative fact - so a missing key raises instead and names what is missing.

    Functions belonging to other groups are ignored, so the station can hand its
    whole 29-entry shadow map to each group in turn.
    """
    unknown = sorted(f for f in state if f not in FUNCTION_BITS)
    if unknown:
        raise ValueError(f"function index out of range 0..{MAX_FUNCTION}: {unknown}")
    missing = [f for f in GROUP_FUNCTIONS[group] if f not in state]
    if missing:
        raise ValueError(f"{group.name} sets all its functions at once; state missing {missing}")
    bits = 0
    for function in GROUP_FUNCTIONS[group]:
        if state[function]:
            bits |= 1 << FUNCTION_BITS[function][1]
    return bits
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: PASS, 19 tests

- [ ] **Step 5: Add the failing golden-vector tests for every encoder**

Append to `tests/unit/test_xbus_commands.py`:

```python
from railctl.xbus.commands import (  # noqa: E402
    cmd_drive_128,
    cmd_emergency_stop_all,
    cmd_emergency_stop_loco,
    cmd_function_group,
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
from railctl.xbus.dialect import XPRESSNET  # noqa: E402
from railctl.xbus.speed import Direction  # noqa: E402

XN = XPRESSNET.long_address_threshold  # 100


def hexbytes(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize(
    ("telegram", "expected"),
    [
        (cmd_station_version(), "21 21 00"),
        (cmd_station_status(), "21 24 05"),
        (cmd_track_power_on(), "21 81 A0"),
        (cmd_track_power_off(), "21 80 A1"),
        (cmd_emergency_stop_all(), "80 80"),
        (cmd_emergency_stop_loco(3, threshold=XN), "92 00 03 91"),
        (cmd_emergency_stop_loco(1234, threshold=XN), "92 C4 D2 84"),
        (cmd_drive_128(3, 1, Direction.FORWARD, threshold=XN), "E4 13 00 03 82 76"),
        (cmd_drive_128(3, 60, Direction.FORWARD, threshold=XN), "E4 13 00 03 BD 49"),
        (cmd_drive_128(3, 126, Direction.FORWARD, threshold=XN), "E4 13 00 03 FF 0B"),
        (cmd_drive_128(1000, 63, Direction.FORWARD, threshold=XN), "E4 13 C3 E8 C0 1C"),
        (cmd_function_group(3, FunctionGroup.G1, 0x10, threshold=XN), "E4 20 00 03 10 D7"),
        (cmd_function_group(3, FunctionGroup.G2, 0x01, threshold=XN), "E4 21 00 03 01 C7"),
        (cmd_function_group(3, FunctionGroup.G3, 0x01, threshold=XN), "E4 22 00 03 01 C4"),
        (cmd_function_group(3, FunctionGroup.G4, 0x01, threshold=XN), "E4 23 00 03 01 C5"),
        (cmd_function_group(3, FunctionGroup.G5, 0x01, threshold=XN), "E4 28 00 03 01 CE"),
        (cmd_loco_info(3, threshold=XN), "E3 00 00 03 E0"),
        (cmd_loco_info(1234, threshold=XN), "E3 00 C4 D2 F5"),
        (cmd_function_state_13_28(3, threshold=XN), "E3 09 00 03 E9"),
        (cmd_service_direct_read(8), "22 15 08 3F"),
        (cmd_service_direct_write(144, 0), "23 16 90 00 A5"),
        (cmd_service_ext_read(8), "22 18 08 32"),
        (cmd_service_ext_read(256), "22 19 00 3B"),
        (cmd_service_ext_read(257), "22 19 01 3A"),
        (cmd_service_ext_read(512), "22 1A 00 38"),
        (cmd_service_ext_read(1023), "22 1B FF C6"),
        (cmd_service_ext_read(1024), "22 18 00 3A"),
        (cmd_service_ext_write(257, 5), "23 1D 01 05 3A"),
        (cmd_z21_cv_read(1), "23 11 00 00 32"),
        (cmd_z21_cv_read(8), "23 11 00 07 35"),
        (cmd_z21_cv_read(257), "23 11 01 00 33"),
        (cmd_z21_cv_read(1024), "23 11 03 FF CE"),
        (cmd_z21_cv_write(8, 12), "24 12 00 07 0C 3D"),
        (cmd_service_result_request(), "21 10 31"),
        (cmd_pom_read_byte(3, 8, threshold=XN), "E6 30 00 03 E4 07 00 36"),
        (cmd_pom_read_byte(3, 256, threshold=XN), "E6 30 00 03 E4 FF 00 CE"),
        (cmd_pom_read_byte(3, 257, threshold=XN), "E6 30 00 03 E5 00 00 30"),
        (cmd_pom_read_byte(3, 1024, threshold=XN), "E6 30 00 03 E7 FF 00 CD"),
        (cmd_pom_read_byte(1234, 300, threshold=XN), "E6 30 C4 D2 E5 2B 00 0E"),
        (cmd_pom_write_byte(3, 8, 12, threshold=XN), "E6 30 00 03 EC 07 0C 32"),
        (cmd_pom_write_byte(3, 31, 0, threshold=XN), "E6 30 00 03 EC 1E 00 27"),
        (cmd_pom_write_byte(3, 32, 0, threshold=XN), "E6 30 00 03 EC 1F 00 26"),
        (cmd_pom_write_bit(3, 29, 3, True, threshold=XN), "E6 30 00 03 E8 1C 0B 2A"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_encoder_golden_vectors(telegram: bytes, expected: str):
    assert telegram == hexbytes(expected)


def test_the_step_126_telegram_contains_a_literal_ff_byte():
    """E4 13 00 03 FF 0B is loco 3 at full speed forward and it carries FF in its
    payload. This is why the envelope must anchor on the FF FE prefix once and
    then trust the header nibble for the length, instead of searching for a
    delimiter."""
    assert b"\xff" in cmd_drive_128(3, 126, Direction.FORWARD, threshold=XN)


def test_a_pom_write_bit_index_above_seven_is_refused():
    with pytest.raises(ValueError, match="bit 8 out of range 0..7"):
        cmd_pom_write_bit(3, 29, 8, True, threshold=XN)


@pytest.mark.parametrize("value", [-1, 256])
def test_a_cv_value_outside_a_byte_is_refused(value: int):
    with pytest.raises(ValueError, match="value"):
        cmd_service_direct_write(8, value)
    with pytest.raises(ValueError, match="value"):
        cmd_z21_cv_write(8, value)
    with pytest.raises(ValueError, match="value"):
        cmd_pom_write_byte(3, 8, value, threshold=XN)


def test_function_bits_outside_a_byte_are_refused():
    with pytest.raises(ValueError, match="function bits"):
        cmd_function_group(3, FunctionGroup.G1, 256, threshold=XN)


def test_the_emergency_stop_for_one_loco_is_the_dedicated_92_instruction():
    """Not E4 13 with wire speed 1. The 92 instruction carries no direction bit,
    so a safety path never has to make a loco_info round trip first to find out
    which way the locomotive was facing."""
    assert cmd_emergency_stop_loco(3, threshold=XN)[0] == 0x92
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: collection ERROR — `ImportError: cannot import name 'cmd_drive_128' from 'railctl.xbus.commands'`

- [ ] **Step 7: Implement the encoders**

Append to `src/railctl/xbus/commands.py`:

```python
def _check_byte(name: str, value: int) -> int:
    if not MIN_BYTE_VALUE <= value <= MAX_BYTE_VALUE:
        raise ValueError(f"{name} {value} out of range {MIN_BYTE_VALUE}..{MAX_BYTE_VALUE}")
    return value


def cmd_station_version() -> bytes:
    """21 21 00 - measured; the YD7010 answers 63 21 40 12 10 (XpressNet 4.0, id 0x12)."""
    return encode(REQ_1_DATA, DB_VERSION)


def cmd_station_status() -> bytes:
    """21 24 05 - measured; the YD7010 answered 62 22 07 47 on an unpowered track."""
    return encode(REQ_1_DATA, DB_STATION_STATUS)


def cmd_track_power_on() -> bytes:
    return encode(REQ_1_DATA, DB_POWER_ON)


def cmd_track_power_off() -> bytes:
    return encode(REQ_1_DATA, DB_POWER_OFF)


def cmd_emergency_stop_all() -> bytes:
    """80 80 - the only telegram here with no data byte at all."""
    return encode(OP_EMERGENCY_STOP_ALL)


def cmd_emergency_stop_loco(address: int, *, threshold: int) -> bytes:
    """92 AH AL X (XpressNet 2.2.5.2).

    The dedicated per-locomotive stop, NOT E4 13 with wire speed 1: it carries
    no direction bit, so a safety path never has to make a loco_info round trip
    first to learn which way the locomotive was facing.
    """
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_EMERGENCY_STOP_LOCO, high, low)


def cmd_drive_128(address: int, step: int, direction: Direction, *, threshold: int) -> bytes:
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_DRIVE, DRIVE_IDENT_128, high, low, encode_speed_128(step, direction))


def cmd_function_group(address: int, group: FunctionGroup, bits: int, *, threshold: int) -> bytes:
    """E4 20/21/22/23/28 AH AL BITS X - sets every function in the group at once."""
    _check_byte("function bits", bits)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_DRIVE, int(group), high, low, bits)


def cmd_loco_info(address: int, *, threshold: int) -> bytes:
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_INFO, DB_LOCO_INFO, high, low)


def cmd_function_state_13_28(address: int, *, threshold: int) -> bytes:
    """E3 09 AH AL X - ask for the ON/OFF state of F13..F28.

    Measured 2026-08-04 (docs/probe-results.md, "Settled"): the YD7010 answers
    E3 52 D1 D2, which Task 8 parses as FunctionState13To28.

    This encoder exists because the E4 loco-info reply carries F0..F12 and
    nothing above. Without it the station has no way to READ F13..F28, and the
    group path (E4 23 / E4 28) writes all eight bits of its group at once - so a
    station with no state to start from seeds zeros and blind-clears every
    function in the group. probe-results.md records that side effect as closed
    precisely because this request answers; dropping the encoder would reopen it.
    """
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_LOCO_INFO, DB_FUNCTION_STATE_13_28, high, low)


def cmd_service_direct_read(cv: int) -> bytes:
    """22 15 C X - legacy direct read, ONE-based, CV1..255.

    `direct_cv_byte` refuses CV256 and above: from station version 3.6 a C of 0
    addresses CV1024, not CV256 (23151 sections 3.2.6 and 3.2.14), and the
    YD7010 reports 4.0, so a bare 0 here would touch the wrong CV with no error.
    Measured: this opcode answers only after a 21 10 poll.
    """
    return encode(REQ_2_DATA, DB_DIRECT_READ, direct_cv_byte(cv))


def cmd_service_direct_write(cv: int, value: int) -> bytes:
    """23 16 C V X - legacy direct write, ONE-based, CV1..255."""
    return encode(REQ_3_DATA, DB_DIRECT_WRITE, direct_cv_byte(cv), _check_byte("value", value))


def cmd_service_ext_read(cv: int) -> bytes:
    """22 18..1B C X - extended read, ONE-based within a 256-wide page.

    CV1024 is page 0 with C = 0, so 22 18 00 is CV1024 and NOT CV256. CV256 is
    22 19 00.
    """
    page, c = ext_cv_fields(cv)
    return encode(REQ_2_DATA, EXT_READ_OPCODES[page], c)


def cmd_service_ext_write(cv: int, value: int) -> bytes:
    """23 1C..1F C V X - extended write, same page scheme as the read."""
    page, c = ext_cv_fields(cv)
    return encode(REQ_3_DATA, EXT_WRITE_OPCODES[page], c, _check_byte("value", value))


def cmd_z21_cv_read(cv: int) -> bytes:
    """23 11 MSB LSB X - 16-bit, ZERO-based.

    Measured 2026-08-04: the only opcode family on this station that pushes its
    result without a 21 10 poll, and the one that reached CV265 and CV266.
    """
    msb, lsb = z21_cv_fields(cv)
    return encode(REQ_3_DATA, DB_Z21_READ, msb, lsb)


def cmd_z21_cv_write(cv: int, value: int) -> bytes:
    """24 12 MSB LSB V X - 16-bit, ZERO-based."""
    msb, lsb = z21_cv_fields(cv)
    return encode(REQ_4_DATA, DB_Z21_WRITE, msb, lsb, _check_byte("value", value))


def cmd_service_result_request() -> bytes:
    """21 10 31 - "Request for Service Mode results".

    XpressNet 2.2.8, verbatim: "The read instruction does not require an answer
    by the command station! A result must be specifically requested." M1
    recorded the whole Lenz opcode family as unimplemented because the probe
    never sent this telegram. It is the protocol, not a workaround.
    """
    return encode(REQ_1_DATA, DB_SERVICE_RESULT)


def cmd_pom_read_byte(address: int, cv: int, *, threshold: int) -> bytes:
    """E6 30 AH AL (E4|MM) LSB 00 X - Operations Mode read, ZERO-based.

    Measured 2026-08-04: the YD7010 answers this with the interface ACK
    01 04 05 and nothing else - no 63 14, no 64 14, no 61 13, no 61 82, no
    broadcast, over an 8 s window and a 30 s raw capture. That is recorded as
    pom_read UNKNOWN, never False, and the judgement belongs to the station
    layer. This encoder claims nothing either way; it only guarantees the
    telegram is the documented one.
    """
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_POM, DB_POM, high, low, POM_READ_BYTE_BASE | mm, lsb, POM_UNUSED_BYTE)


def cmd_pom_write_byte(address: int, cv: int, value: int, *, threshold: int) -> bytes:
    """E6 30 AH AL (EC|MM) LSB V X - Operations Mode byte write, ZERO-based.

    Measured to work on this hardware even though the read does not: a write
    needs no return path from the decoder, while a read needs RailCom channel 2
    to come back. There is no confirmation channel, so the station verifies by
    reading the CV back in service mode, never by assuming success.
    """
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    return encode(OP_POM, DB_POM, high, low, POM_WRITE_BYTE_BASE | mm, lsb,
                  _check_byte("value", value))


def cmd_pom_write_bit(address: int, cv: int, bit: int, value: bool, *, threshold: int) -> bytes:
    """E6 30 AH AL (E8|MM) LSB (D<<3|BBB) X - Operations Mode bit write."""
    if not 0 <= bit <= MAX_BIT_INDEX:
        raise ValueError(f"bit {bit} out of range 0..{MAX_BIT_INDEX}")
    mm, lsb = pom_cv_fields(cv)
    high, low = encode_loco_address(address, long_threshold=threshold)
    payload = (int(value) << POM_BIT_VALUE_SHIFT) | bit
    return encode(OP_POM, DB_POM, high, low, POM_WRITE_BIT_BASE | mm, lsb, payload)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: PASS, 68 tests

- [ ] **Step 9: Add the failing tests for `timeout_class`**

Append to `tests/unit/test_xbus_commands.py`:

```python
from railctl.xbus.commands import TimeoutClass, timeout_class  # noqa: E402


@pytest.mark.parametrize(
    "telegram",
    [
        cmd_service_direct_read(8),
        cmd_service_direct_write(8, 1),
        cmd_service_ext_read(8),
        cmd_service_ext_read(256),
        cmd_service_ext_read(512),
        cmd_service_ext_read(768),
        cmd_service_ext_write(8, 1),
        cmd_service_ext_write(257, 1),
        cmd_z21_cv_read(8),
        cmd_z21_cv_write(8, 1),
        cmd_service_result_request(),
    ],
    ids=lambda t: t.hex(" "),
)
def test_service_mode_telegrams_get_the_long_budget(telegram: bytes):
    assert timeout_class(telegram) is TimeoutClass.PROGRAMMING


@pytest.mark.parametrize(
    "telegram",
    [
        cmd_station_version(),
        cmd_station_status(),
        cmd_track_power_on(),
        cmd_track_power_off(),
        cmd_emergency_stop_all(),
        cmd_emergency_stop_loco(3, threshold=XN),
        cmd_drive_128(3, 1, Direction.FORWARD, threshold=XN),
        cmd_function_group(3, FunctionGroup.G1, 0x10, threshold=XN),
        cmd_loco_info(3, threshold=XN),
        cmd_function_state_13_28(3, threshold=XN),
        cmd_pom_read_byte(3, 8, threshold=XN),
        cmd_pom_write_byte(3, 8, 1, threshold=XN),
        cmd_pom_write_bit(3, 29, 3, True, threshold=XN),
    ],
    ids=lambda t: t.hex(" "),
)
def test_normal_operation_telegrams_get_the_short_budget(telegram: bytes):
    """POM is NORMAL on purpose: its command reply is the interface ACK, which
    comes back immediately. The long wait for a POM result, if one ever arrives,
    is a separate await_frame in the station layer, not this budget."""
    assert timeout_class(telegram) is TimeoutClass.NORMAL


@pytest.mark.parametrize("telegram", [b"", b"\x21"])
def test_a_telegram_too_short_to_classify_is_refused(telegram: bytes):
    """Every telegram reaching this function was produced by an encoder in this
    module and is at least two bytes long. Something shorter is a caller bug, and
    handing it the short budget would hide that bug behind a plausible answer -
    the same shape as reading an absence as a fact."""
    with pytest.raises(ValueError, match="too short"):
        timeout_class(telegram)


def test_a_drive_telegram_that_happens_to_start_with_a_programming_pair_is_not_promoted():
    """22 15 as the FIRST TWO bytes is the direct read; the same two values
    further into a payload are not. Classification looks at position 0 and 1
    only."""
    assert timeout_class(b"\xe4\x13\x22\x15\x82\x00") is TimeoutClass.NORMAL


def test_every_service_mode_encoder_is_in_the_programming_table():
    """Derive the list instead of restating it.

    PROGRAMMING_TELEGRAMS is hand written, and the parametrize above is a second
    hand-written copy of the same calls. Nothing ties either to the set of
    encoders that actually exist. Add a cmd_service_* encoder later, forget the
    table, and its telegram gets the 5.0 s budget instead of 95.0 s (spec line
    314) - so the reply arrives after the window closes and the station records
    the opcode as unsupported. That is the M1 failure exactly: a capability
    recorded absent because the instrument measuring it was mis-set.
    """
    import railctl.xbus.commands as c

    for name in dir(c):
        if name.startswith(("cmd_service_", "cmd_z21_cv_")):
            fn = getattr(c, name)
            args = (8, 1) if "write" in name else ((8,) if "cv" in name or "read" in name else ())
            assert c.timeout_class(fn(*args)) is c.TimeoutClass.PROGRAMMING, name
```

- [ ] **Step 10: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: collection ERROR — `ImportError: cannot import name 'TimeoutClass' from 'railctl.xbus.commands'`

- [ ] **Step 11: Implement `TimeoutClass` and `timeout_class`**

Append to `src/railctl/xbus/commands.py`:

```python
class TimeoutClass(enum.Enum):
    """Which Link budget a telegram needs: 5.0 s or 95.0 s."""

    NORMAL = "normal"
    PROGRAMMING = "programming"


# Service-mode exchanges can take a minute and the reply arrives as the command
# reply, so the wait is direct rather than a poll loop. Everything else - power,
# drive, function, POM in both directions - answers immediately.
PROGRAMMING_TELEGRAMS: frozenset[tuple[int, int]] = (
    frozenset(
        {
            (REQ_1_DATA, DB_SERVICE_RESULT),  # 21 10
            (REQ_2_DATA, DB_DIRECT_READ),  # 22 15
            (REQ_3_DATA, DB_DIRECT_WRITE),  # 23 16
            (REQ_3_DATA, DB_Z21_READ),  # 23 11
            (REQ_4_DATA, DB_Z21_WRITE),  # 24 12
        }
    )
    | frozenset((REQ_2_DATA, opcode) for opcode in EXT_READ_OPCODES)  # 22 18..1B
    | frozenset((REQ_3_DATA, opcode) for opcode in EXT_WRITE_OPCODES)  # 23 1C..1F
)


def timeout_class(telegram: bytes) -> TimeoutClass:
    """Classify a telegram so the station layer never inspects opcode bytes.

    Every telegram reaching this function was produced by an encoder above and is
    at least two bytes long, so anything shorter is a caller bug and is refused
    rather than given a plausible-looking NORMAL. Silently classifying an
    unclassifiable telegram is how a wrong budget gets chosen without anyone
    noticing.
    """
    if len(telegram) < 2:
        raise ValueError(f"telegram too short to classify: {telegram.hex(' ')!r}")
    if (telegram[0], telegram[1]) in PROGRAMMING_TELEGRAMS:
        return TimeoutClass.PROGRAMMING
    return TimeoutClass.NORMAL
```

- [ ] **Step 12: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_commands.py -q`
Expected: PASS, 96 tests

- [ ] **Step 13: Prove the module obeys layering rule 2 (no CV arithmetic outside `cv.py`)**

Run: `grep -nE 'cv *- *1|cv *\+ *1|>> *8|<< *8|% *256' src/railctl/xbus/commands.py`
Expected: no output, exit status 1. Every CV wire field came out of `xbus.cv`; the only shifts in
the file are `1 << FUNCTION_BITS[function][1]` and `int(value) << POM_BIT_VALUE_SHIFT`, neither of which
touches a CV number.

If grep prints a line, it is real CV arithmetic — do not relax the pattern. The regex is the whole
of layering rule 2's enforcement; weakening it to make a match go away removes the guard
permanently and silently. Move the arithmetic into `xbus/cv.py` instead. Note that the module
docstring in Step 3 describes these patterns in prose for exactly this reason: writing them out
literally there would make the file match itself.

- [ ] **Step 14: Check lint and formatting**

Run: `.venv/bin/python -m ruff check src/railctl/xbus/commands.py tests/unit/test_xbus_commands.py`
Expected: `All checks passed!`

- [ ] **Step 15: Commit**

```bash
git add src/railctl/xbus/commands.py tests/unit/test_xbus_commands.py
git commit -m "feat(xbus): add command encoders with golden byte vectors"
```

---

### Task 8: X-Bus reply parsing (`xbus/replies.py`)

**Files:**
- Create: `src/railctl/xbus/replies.py`
- Test: `tests/unit/test_xbus_replies.py`
- Modify: none. `src/railctl/xbus/__init__.py` stays as Task 4 left it; consumers import
  `railctl.xbus.replies` by module path.

**Interfaces:**

- Consumes:
  - `railctl.errors.ProtocolError` and `railctl.errors.XBusChecksumError` (Task 2). The tree is
    `ProtocolError` -> `XBusDecodeError` -> `XBusChecksumError` (spec lines 148-149), and both are
    imported here because `parse` must tell a bad XOR from a bad length: the checksum branch is
    caught first, since catching the base class first would swallow it.
  - `railctl.xbus.codec.decode(raw: bytes) -> tuple[int, bytes]` — returns `(header, data)` with
    the XOR byte removed; raises an `XBusDecodeError`/`XBusChecksumError` when the length does not
    match `(header & 0x0F) + 2` or the XOR identity fails.
  - `railctl.xbus.codec.xor(data: bytes) -> int` (used by the test helper only).
  - `railctl.xbus.codec.encode(header: int, *data: int) -> bytes` (test helper only).
  - `railctl.xbus.cv.join_cv_field(msb: int, lsb: int) -> int` — the ONLY way this module is
    allowed to combine the two address bytes of a `64 14` reply (layering rule 2).
  - `railctl.xbus.speed.Direction`, `railctl.xbus.speed.SPEED_STEPS` (`= 128`),
    `railctl.xbus.speed.decode_speed_128(byte: int) -> tuple[int, Direction, bool]`.
  - `railctl.xbus.commands.FUNCTION_BITS` and `railctl.xbus.commands.FunctionGroup` (Task 7) —
    used by one cross-check test, not by the module.
- Produces:
  - `@dataclass(frozen=True, slots=True) class GenericAck` — no fields
  - `class InterfaceStatus(code: int)`
  - `class StationVersion(raw: int, station_id: int)` with `.version -> str` and `.family -> str`
  - `class StationStatus(raw, emergency_off, emergency_stop, auto_start_mode, service_mode,
    powering_up, ram_error)`, classmethod `StationStatus.from_raw(raw: int) -> StationStatus`,
    property `.track_power -> bool`
  - `class CvValue(raw_cv: int, value: int, ident: int, z21_form: bool)`
  - `class PagedCvValue(raw_register: int, value: int)`
  - `class Ready`, `Busy`, `NoAck`, `ShortCircuit`, `TrackShortCircuit`, `Unsupported`,
    `TransferError`, `StationBusy`, `ServiceModeEntry`, `EmergencyStopBroadcast` — all field-less
  - `class PowerState(on: bool)`
  - `class LocoInfo(raw_ident: int, raw_speed: int, speed_steps: int | None,
    in_use_by_other: bool, function_bits: tuple[bool, ...], speed: int | None = None,
    direction: Direction | None = None, emergency_stopped: bool | None = None,
    address: int | None = None)` — this is the declaration order verbatim, because a caller that
    builds one positionally from a reordered summary puts the address into `raw_ident` with no type
    error, both being ints. **Construct by keyword only.** `address` is always `None` from `parse`
    and is attached later with `dataclasses.replace`.
  - `class FunctionState13To28(f13_f20: int, f21_f28: int)` — the `E3 52` reply to `E3 09`
  - `class Other(telegram: bytes, reason: str = "unknown_form")` — `reason` is one of
    `"unknown_form"`, `"checksum"`, `"length"`, `"empty"`. The default keeps the spec's positional
    `Other(telegram)` shape constructible.
  - `Reply` — the union type of all of the above
  - `parse(telegram: bytes) -> Reply`
  - `HEADER_61_REPLIES: dict[int, Reply]`, `SPEED_STEP_MODES: dict[int, int]`,
    `STATION_FAMILIES: dict[int, str]`, `CV_RESULT_IDENTS: tuple[int, ...]`,
    `LOCO_INFO_FUNCTION_BITS: tuple[tuple[int, int], ...]`, `FUNCTIONS_IN_LOCO_INFO: int`
  - `TRANSIENT_REPLIES: frozenset[Reply]` — the replies that are not an answer either way
  - Singletons `GENERIC_ACK`, `READY`, `BUSY`, `NO_ACK`, `SHORT_CIRCUIT`, `TRACK_SHORT_CIRCUIT`,
    `UNSUPPORTED`, `TRANSFER_ERROR`, `STATION_BUSY`, `SERVICE_MODE_ENTRY`, `POWER_ON`,
    `POWER_OFF`, `EMERGENCY_STOP_BROADCAST`

- [ ] **Step 1: Write the failing example tests, including every measured frame**

```python
# tests/unit/test_xbus_replies.py
"""Reply parsing tests.

`parse` is where this project's characteristic failure is manufactured: a reply
form the parser does not recognise is indistinguishable from no reply at all,
and the layer above reads silence as "the hardware cannot do this". That is not
a hypothetical: in M1 the whole Lenz opcode family was recorded as unimplemented
because the probe's `_read_value` never sent the `21 10` poll, so results the
station was holding were never collected (docs/probe-results.md, the R2/R4
correction). A capability was declared absent because of a defect in the
instrument measuring it.

Three things therefore have to hold at once:

- parse never raises, whatever arrives on a port shared with a telemetry stream;
- parse never claims MORE than the header entitles it to;
- an unrecognised telegram becomes Other(telegram), which carries the bytes and
  is an UNKNOWN - not the same thing as silence, and never the same thing as a
  negative answer.
"""

from __future__ import annotations

import dataclasses

import pytest

from railctl.xbus.codec import encode
from railctl.xbus.replies import (
    HEADER_61_REPLIES,
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
from railctl.xbus.speed import Direction


def tg(text: str) -> bytes:
    return bytes.fromhex(text)


def test_the_measured_version_reply_is_xpressnet_40_on_a_z21_family_station():
    reply = parse(tg("63 21 40 12 10"))
    assert isinstance(reply, StationVersion)
    assert reply.raw == 0x40
    assert reply.station_id == 0x12
    assert reply.version == "4.0"
    assert reply.family == "Z21"


def test_an_unlisted_station_id_reports_its_family_as_unknown_not_as_a_guess():
    reply = parse(tg("63 21 40 7F 7D"))
    assert isinstance(reply, StationVersion)
    assert reply.family == "unknown"


def test_the_measured_status_reply_on_an_unpowered_track():
    """62 22 07 47 was measured on 2026-08-04. Bit 0 emergency off, bit 1
    emergency stop, bit 2 automatic start mode. XpressNet defines no
    short-circuit bit, and the earlier "short circuit" reading was dropped."""
    reply = parse(tg("62 22 07 47"))
    assert isinstance(reply, StationStatus)
    assert reply.raw == 0x07
    assert reply.emergency_off is True
    assert reply.emergency_stop is True
    assert reply.auto_start_mode is True
    assert reply.service_mode is False
    assert reply.powering_up is False
    assert reply.ram_error is False
    assert reply.track_power is False


def test_every_status_bit_owns_exactly_one_flag():
    """Flipping one bit must move one flag. Two flags sharing a mask would make a
    station in one state indistinguishable from a station in another. All 256
    raw bytes, not a sample."""
    flags = {
        "emergency_off": 0x01,
        "emergency_stop": 0x02,
        "auto_start_mode": 0x04,
        "service_mode": 0x08,
        "powering_up": 0x40,
        "ram_error": 0x80,
    }
    for raw in range(256):
        status = StationStatus.from_raw(raw)
        for name, mask in flags.items():
            assert getattr(status, name) is bool(raw & mask), (raw, name)
            flipped = StationStatus.from_raw(raw ^ mask)
            moved = {
                other for other in flags if getattr(status, other) != getattr(flipped, other)
            }
            assert moved == {name}, (raw, name, moved)


def test_track_power_is_the_inverse_of_emergency_off():
    assert StationStatus.from_raw(0x00).track_power is True
    assert StationStatus.from_raw(0x01).track_power is False


def test_the_lenz_cv_result_carries_the_raw_field_and_names_no_cv():
    """63 14 08 08 77. The encoding is NOT inferred here: 63 14 carries both
    service-mode and POM results whose conventions differ, and the caller knows
    which request it issued. cv.resolve_service_cv does that job."""
    reply = parse(tg("63 14 08 08 77"))
    assert isinstance(reply, CvValue)
    assert reply.raw_cv == 0x08
    assert reply.value == 0x08
    assert reply.ident == 0x14
    assert reply.z21_form is False


@pytest.mark.parametrize(
    ("telegram", "ident"),
    [("63 15 09 00 7F", 0x15), ("63 16 0A 40 3F", 0x16), ("63 17 FF 00 8B", 0x17)],
)
def test_each_extended_cv_result_band_is_parsed_and_keeps_its_ident(telegram: str, ident: int):
    reply = parse(tg(telegram))
    assert isinstance(reply, CvValue)
    assert reply.ident == ident
    assert reply.z21_form is False


def test_the_z21_cv_result_joins_both_address_bytes():
    """64 14 MSB LSB VAL is DOC ONLY - spec line 573.

    It has never been seen on this station. probe-results.md line 34 records that
    a POM read returned no 64 14 at all, and every CV read the probe measured came
    back as 63 14 or 63 15 (probe-results.md lines 148-152). It is parsed now so
    that if the Z21 LAN transport or a firmware update ever emits it, the value is
    not lost the way the missing 21 10 poll lost the Lenz results in M1.

    Whether raw_cv 7 names CV7 or CV8 is cv.resolve_service_cv's job and is
    UNMEASURED for this form, so nothing here asserts a CV number.
    """
    reply = parse(tg("64 14 00 07 91 E6"))
    assert isinstance(reply, CvValue)
    assert reply.raw_cv == 7
    assert reply.value == 145
    assert reply.ident == 0x14
    assert reply.z21_form is True


def test_a_register_result_is_a_valid_answer_and_never_a_cv_value():
    """63 10 01 03 71 means the station fell back to register or paged mode
    because the decoder did not answer a direct-mode read (23151 3.1.2.6). The
    number is a REGISTER, not a CV, so reading it as one publishes a value the
    decoder never sent. It is a valid answer, not an error."""
    reply = parse(tg("63 10 01 03 71"))
    assert isinstance(reply, PagedCvValue)
    assert not isinstance(reply, CvValue)
    assert reply.raw_register == 1
    assert reply.value == 3


def test_the_station_saying_it_could_not_process_that_is_parsed():
    """61 82 E3. In M1 this was recorded as "I support that" because the header
    pair was not in the table."""
    assert isinstance(parse(tg("61 82 E3")), Unsupported)


@pytest.mark.parametrize(
    ("telegram", "type_name"),
    [
        ("61 00 61", "PowerState"),
        ("61 01 60", "PowerState"),
        ("61 02 63", "ServiceModeEntry"),
        ("61 08 69", "TrackShortCircuit"),
        ("61 11 70", "Ready"),
        ("61 12 73", "ShortCircuit"),
        ("61 13 72", "NoAck"),
        ("61 1F 7E", "Busy"),
        ("61 80 E1", "TransferError"),
        ("61 81 E0", "StationBusy"),
        ("61 82 E3", "Unsupported"),
    ],
)
def test_every_header_61_form_is_pinned(telegram: str, type_name: str):
    assert type(parse(tg(telegram))).__name__ == type_name


def test_power_off_and_power_on_are_distinguishable():
    assert parse(tg("61 00 61")) == PowerState(on=False)
    assert parse(tg("61 01 60")) == PowerState(on=True)


def test_the_generic_ack_and_the_other_interface_frames_are_distinguishable():
    """01 04 05 means the interface forwarded the command - it is NOT a value.
    Every other 01 XX frame carries its code verbatim so the transport layer can
    map it without this module claiming to know what it means."""
    assert isinstance(parse(tg("01 04 05")), GenericAck)
    late = parse(tg("01 0A 0B"))
    assert isinstance(late, InterfaceStatus)
    assert late.code == 0x0A


def test_the_emergency_stop_broadcast_is_parsed():
    assert isinstance(parse(tg("81 00 81")), EmergencyStopBroadcast)


def test_a_locomotive_info_reply_in_128_step_mode():
    """E4 04 BD 10 00 4D: ident 0x04 is 128 speed steps and not busy, BD is step
    60 forward, FA 0x10 is F0 on with F1-F4 off."""
    reply = parse(tg("E4 04 BD 10 00 4D"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps == 128
    assert reply.speed == 60
    assert reply.direction is Direction.FORWARD
    assert reply.emergency_stopped is False
    assert reply.in_use_by_other is False
    assert reply.raw_speed == 0xBD
    assert reply.function_bits == (True, False, False, False, False,
                                   False, False, False, False,
                                   False, False, False, False)


def test_a_locomotive_info_reply_never_invents_the_address_it_was_not_sent():
    """The E4 reply carries no address field at all. The station knows which
    locomotive it asked about and fills this in with dataclasses.replace; parse
    must not guess."""
    assert parse(tg("E4 04 BD 10 00 4D")).address is None


def test_all_functions_on_and_another_device_in_control():
    reply = parse(tg("E4 0C BD 1F FF B5"))
    assert isinstance(reply, LocoInfo)
    assert reply.in_use_by_other is True
    assert reply.function_bits == tuple([True] * 13)


def test_a_locomotive_held_at_emergency_stop_is_not_reported_as_stopped_normally():
    """Wire speed 1 is emergency stop, wire speed 0 is a normal stop. Reporting
    the first as the second tells an operator the track is safe when it is not.

    This is the only positive case for emergency_stopped anywhere in the section.
    Without it an implementation that hardcodes emergency_stopped=False on the
    128-step path passes every other test, including the exhaustive sweep, and
    the one safety-relevant tri-state in LocoInfo goes unpinned.
    """
    reply = parse(tg("E4 04 81 00 00 61"))
    assert isinstance(reply, LocoInfo)
    assert reply.emergency_stopped is True
    assert reply.speed == 0
    assert reply.direction is Direction.FORWARD


def test_direction_is_carried_even_when_the_locomotive_is_stopped():
    reply = parse(tg("E4 04 00 00 00 E0"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed == 0
    assert reply.direction is Direction.REVERSE


def test_a_speed_step_mode_this_module_cannot_decode_reports_unknown_not_zero():
    """E4 02 BD 10 00 4B is a 28-step locomotive. speed.py defines only the
    128-step wire layout, so the speed is UNKNOWN, not 60 and not 0. Guessing
    would publish a speed the decoder never had."""
    reply = parse(tg("E4 02 BD 10 00 4B"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps == 28
    assert reply.speed is None
    assert reply.direction is None
    assert reply.emergency_stopped is None
    assert reply.raw_speed == 0xBD


def test_a_reserved_speed_step_pattern_reports_unknown():
    reply = parse(tg("E4 07 BD 10 00 4E"))
    assert isinstance(reply, LocoInfo)
    assert reply.speed_steps is None
    assert reply.speed is None


def test_an_e4_command_echoed_back_is_not_read_as_a_locomotive_info_reply():
    """E4 F8 is a COMMAND, not a reply - the single-function command the spec
    prefers (line 694). 0xE4 is the request header for drive and for functions as
    well as the loco-info reply header, and the reply is identified by an
    identification byte of the form 0000 BFFF, so only db0 0x00..0x0F is one.

    Parsing E4 F8 00 03 40 as LocoInfo would produce raw_ident 0xF8, and
    0xF8 & 0x07 == 0 means the parser would then claim 14 speed steps and
    in_use_by_other True for a locomotive nobody asked about. That is the exact
    thing the module docstring forbids: a reply invented from an unrelated
    telegram, which the station would treat as a measurement.
    """
    assert isinstance(parse(tg("E4 F8 00 03 40 5F")), Other)
    assert isinstance(parse(tg("E4 13 00 03 82 76")), Other)  # a drive command


def test_the_f13_to_f28_state_reply_is_parsed_so_the_station_never_blind_clears():
    """E3 52 D1 D2 answers E3 09 (Lenz 23151 section 3.1.9.2) and is the ONLY
    reply form carrying F13..F28; LocoInfo stops at F12.

    docs/probe-results.md lists this under Settled: "F13-F28 state readable |
    yes, E3 09 -> E3 52 D1 D2 | closes the blind-clear side effect". Leaving it
    unparsed forces the station to seed zeros before an E4 23 or E4 28 write,
    which switches off every function in the group it did not read - the failure
    spec line 1551 calls most likely to bite in practice.
    """
    reply = parse(tg("E3 52 01 80 30"))
    assert isinstance(reply, FunctionState13To28)
    assert reply.f13_f20 == 0x01
    assert reply.f21_f28 == 0x80


def test_the_replies_that_answer_nothing_either_way_are_named_in_one_place():
    """A Busy or a StationBusy says nothing about whether an opcode exists.

    Unsupported is the ONLY reply that entitles anything above to record a
    capability as False. Without one named set every consumer re-derives that
    list by hand, and the one that forgets StationBusy records a busy station as
    an absent capability - the exact M1 failure.
    """
    assert UNSUPPORTED not in TRANSIENT_REPLIES
    assert TRANSIENT_REPLIES <= set(HEADER_61_REPLIES.values())


def test_an_unrecognised_but_well_formed_telegram_becomes_other_without_raising():
    """71 AA DB. Other is an UNKNOWN carrying the bytes, so the layer above can
    print hex and a human can extend the table. It is not silence, and it is
    never a negative answer."""
    reply = parse(tg("71 AA DB"))
    assert isinstance(reply, Other)
    assert reply.telegram == tg("71 AA DB")
    assert reply.reason == "unknown_form"


def test_a_telegram_with_a_broken_xor_says_so_instead_of_just_being_unknown():
    """Three different causes must stay distinguishable, because the remedies are
    opposite: a corrupt XOR means the LINK is damaging bytes, a truncated frame
    means the read window closed early, and a well-formed telegram in a form
    nobody listed means the REPLY TABLE is incomplete. Collapsing all three into
    one value leaves the station unable to tell "the cable is bad" from "the
    station answered in a form we do not know", and both then look like an
    unresolved capability. Link.stats() counts bad_xor separately (spec line 293)
    for exactly this reason.
    """
    reply = parse(tg("62 22 07 48"))
    assert isinstance(reply, Other)
    assert reply.reason == "checksum"


def test_a_telegram_whose_length_disagrees_with_its_header_says_length():
    """A short 63 14 is not a CV read of value zero; it is a frame that did not
    arrive. Truncation is never filled in with defaults."""
    reply = parse(tg("63 14"))
    assert isinstance(reply, Other)
    assert reply.reason == "length"


def test_a_well_formed_telegram_with_no_data_bytes_is_reported_as_empty():
    """80 80 decodes cleanly - its header nibble declares zero data bytes - but
    no reply form in the index table has a zero-length body, so there is no db0
    to dispatch on. That is a third cause again, and it is not a checksum fault
    and not a truncation."""
    reply = parse(tg("80 80"))
    assert isinstance(reply, Other)
    assert reply.reason == "empty"


@pytest.mark.parametrize(
    "reply",
    [
        parse(tg("63 21 40 12 10")),
        parse(tg("62 22 07 47")),
        parse(tg("63 14 08 08 77")),
        parse(tg("63 10 01 03 71")),
        parse(tg("E4 04 BD 10 00 4D")),
        parse(tg("01 04 05")),
        parse(tg("01 0A 0B")),
        parse(tg("61 82 E3")),
        parse(tg("71 AA DB")),
    ],
    ids=lambda r: type(r).__name__,
)
def test_every_parsed_reply_is_frozen(reply: object):
    """Parsed replies are the evidence a verdict rests on and are hex-dumped as
    an audit trail. One that could be edited after parsing would let a later
    stage rewrite what an earlier one saw."""
    fields = dataclasses.fields(reply)
    if not fields:
        pytest.skip("no fields to mutate")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(reply, fields[0].name, None)


def test_a_frozen_field_less_reply_still_refuses_a_new_attribute():
    """GenericAck has no fields, so the test above skips it, but it must still be
    impossible to hang an attribute on one.

    The exception type is not FrozenInstanceError here. On CPython 3.13 a
    frozen+slots dataclass raises TypeError from its __setattr__ when the name is
    not a declared field, and AttributeError on some versions, so all three are
    accepted. What is being pinned is the refusal, not the class of the refusal.
    """
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
        parse(tg("01 04 05")).forged = 1


def test_the_header_nibble_is_the_only_length_guard_parse_needs():
    """codec.decode has already enforced len == (header & 0x0F) + 2, so every
    branch below can index its data bytes without a second check. This test
    fixes that contract: build every reply form at its declared length and read
    the last byte each branch touches."""
    assert parse(encode(0x64, 0x14, 0x03, 0xFF, 0x2A)).value == 0x2A
    assert parse(encode(0xE4, 0x04, 0x00, 0x00, 0x80)).function_bits[12] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'railctl.xbus.replies'`

- [ ] **Step 3: Write the reply dataclasses and `parse`**

```python
# src/railctl/xbus/replies.py
"""Typed views over X-Bus reply telegrams (framing already stripped).

`parse` is TOTAL. It never raises, for any byte string, because this port is
shared with a telemetry stream and because a parser that raises turns a frame we
did not understand into an exception in a layer that was measuring something
else. An unrecognised telegram becomes `Other(telegram)`, which carries the
bytes: that is an UNKNOWN, distinct from silence and never a negative answer.

`parse` also never claims MORE than the header entitles it to. The dangerous
direction is not a missed reply - that shows up as `Other` and the station
treats it as unresolved - it is a reply invented from an unrelated telegram,
which the station would treat as a measurement.

Length handling: `codec.decode` has already enforced
`len(telegram) == (header & 0x0F) + 2`, so a header of 0x63 guarantees exactly
three data bytes and a header of 0xE4 exactly four. No branch below needs its
own length guard, and none has one.
"""

from __future__ import annotations

from dataclasses import dataclass

from railctl.errors import ProtocolError, XBusChecksumError
from railctl.xbus import codec
from railctl.xbus.cv import join_cv_field
from railctl.xbus.speed import SPEED_STEPS, Direction, decode_speed_128

HDR_INTERFACE = 0x01
HDR_PROGRAMMING = 0x61
HDR_STATUS = 0x62
HDR_RESULT_5 = 0x63
HDR_RESULT_6 = 0x64
HDR_BROADCAST = 0x81
HDR_FUNCTION_STATE = 0xE3
HDR_LOCO_INFO = 0xE4

DB_FUNCTION_STATE_13_28 = 0x52

DB_GENERIC_ACK = 0x04
DB_STATUS = 0x22
DB_PAGED_RESULT = 0x10
DB_VERSION = 0x21
DB_EMERGENCY_STOP = 0x00
DB_Z21_CV_RESULT = 0x14

# 63 14 / 15 / 16 / 17 (Lenz 23151 sections 3.1.2.6 to 3.1.2.9). Which CV each
# one names depends on the request that was issued, so the number is resolved by
# cv.resolve_service_cv and NOT here.
CV_RESULT_IDENTS = (0x14, 0x15, 0x16, 0x17)

# Identification byte of a loco info reply is 0000 BFFF: bit 3 is "another
# device is in control", bits 0-2 are the speed step mode, and the HIGH NIBBLE IS
# ZERO. That last part is load bearing. 0xE4 is also the request header for drive
# (E4 13) and for functions (E4 20..28, E4 F8), so a stray or echoed command
# reaches this parser under the same header as a reply. Only db0 0x00..0x0F can
# be a reply; anything else is a command and becomes Other.
SPEED_STEP_MODES: dict[int, int] = {0b000: 14, 0b001: 27, 0b010: 28, 0b100: 128}
IDENT_BUSY_MASK = 0x08
IDENT_SPEED_STEPS_MASK = 0x07
IDENT_RESERVED_MASK = 0xF0

# Lenz 23151 lists 0x00 LZ100, 0x01 LH200, 0x02 DPC and 0x03 Control Plus.
# 0x12 is the Z21 family and is the ONLY one measured here (probe-results.md,
# 2026-08-04). An id outside the table reports "unknown" rather than a guess.
STATION_FAMILIES: dict[int, str] = {
    0x00: "LZ100",
    0x01: "LH200",
    0x02: "DPC",
    0x03: "Control Plus",
    0x12: "Z21",
}
UNKNOWN_FAMILY = "unknown"

STATUS_EMERGENCY_OFF = 0x01
STATUS_EMERGENCY_STOP = 0x02
STATUS_AUTO_START = 0x04
STATUS_SERVICE_MODE = 0x08
STATUS_POWERING_UP = 0x40
STATUS_RAM_ERROR = 0x80

# (byte index within (FA, FB), mask), indexed by function number. F0 is bit 4 of
# FA and F1 is bit 0 of FA - the same irregular layout the E4 20 command byte
# uses, which is what tests/unit/test_xbus_replies.py cross-checks against
# commands.FUNCTION_BITS.
LOCO_INFO_FUNCTION_BITS: tuple[tuple[int, int], ...] = (
    (0, 0x10),  # F0
    (0, 0x01),  # F1
    (0, 0x02),  # F2
    (0, 0x04),  # F3
    (0, 0x08),  # F4
    (1, 0x01),  # F5
    (1, 0x02),  # F6
    (1, 0x04),  # F7
    (1, 0x08),  # F8
    (1, 0x10),  # F9
    (1, 0x20),  # F10
    (1, 0x40),  # F11
    (1, 0x80),  # F12
)
FUNCTIONS_IN_LOCO_INFO = len(LOCO_INFO_FUNCTION_BITS)


@dataclass(frozen=True, slots=True)
class GenericAck:
    """01 04 05 - the interface forwarded the command. NOT a value."""


@dataclass(frozen=True, slots=True)
class InterfaceStatus:
    """Any other 01 XX frame. The code is carried verbatim; mapping it to an
    exception is the transport layer's job, not this module's."""

    code: int


@dataclass(frozen=True, slots=True)
class StationVersion:
    raw: int
    station_id: int

    @property
    def version(self) -> str:
        return f"{self.raw >> 4}.{self.raw & 0x0F}"

    @property
    def family(self) -> str:
        return STATION_FAMILIES.get(self.station_id, UNKNOWN_FAMILY)


@dataclass(frozen=True, slots=True)
class StationStatus:
    """62 22 S. Bit meanings are the Lenz XpressNet ones (section 2.1.7).

    The German 23151 manual swaps bits 0 and 1, and neither document defines any
    bit as "short circuit". `raw` is always preserved so the interpretation can
    be revised without touching the parser.
    """

    raw: int
    emergency_off: bool
    emergency_stop: bool
    auto_start_mode: bool
    service_mode: bool
    powering_up: bool
    ram_error: bool

    @classmethod
    def from_raw(cls, raw: int) -> StationStatus:
        return cls(
            raw=raw,
            emergency_off=bool(raw & STATUS_EMERGENCY_OFF),
            emergency_stop=bool(raw & STATUS_EMERGENCY_STOP),
            auto_start_mode=bool(raw & STATUS_AUTO_START),
            service_mode=bool(raw & STATUS_SERVICE_MODE),
            powering_up=bool(raw & STATUS_POWERING_UP),
            ram_error=bool(raw & STATUS_RAM_ERROR),
        )

    @property
    def track_power(self) -> bool:
        return not self.emergency_off


@dataclass(frozen=True, slots=True)
class CvValue:
    """A CV read result. `raw_cv` is the field exactly as received.

    The encoding is NOT inferred here. 63 14 carries both service-mode and POM
    results whose conventions differ, and the caller knows which request it
    issued; `cv.resolve_service_cv` and `cv.echo_candidates` turn `raw_cv` into a
    CV number. For the 64 14 form the two address bytes are combined by
    `cv.join_cv_field`, so no CV arithmetic escapes that module.
    """

    raw_cv: int
    value: int
    ident: int
    z21_form: bool


@dataclass(frozen=True, slots=True)
class PagedCvValue:
    """63 10 REG VAL - register or paged mode.

    23151 section 3.1.2.6: the station has determined the decoder does not
    support direct mode and has fallen back. This is a VALID answer, not an
    error. The number is a register, not a CV.
    """

    raw_register: int
    value: int


@dataclass(frozen=True, slots=True)
class Ready:
    """61 11 - service mode ready."""


@dataclass(frozen=True, slots=True)
class Busy:
    """61 1F - programming busy."""


@dataclass(frozen=True, slots=True)
class NoAck:
    """61 13 - the decoder did not acknowledge."""


@dataclass(frozen=True, slots=True)
class ShortCircuit:
    """61 12 - short circuit on the PROGRAMMING track."""


@dataclass(frozen=True, slots=True)
class TrackShortCircuit:
    """61 08 - short circuit on the MAIN track. Distinct from 61 12."""


@dataclass(frozen=True, slots=True)
class Unsupported:
    """61 82 - the station cannot process that instruction.

    This is the ONLY reply that entitles anything above to record a capability
    as False. Silence does not, and Other does not.
    """


@dataclass(frozen=True, slots=True)
class TransferError:
    """61 80 - the station saw a bad XOR from us. Resend once."""


@dataclass(frozen=True, slots=True)
class StationBusy:
    """61 81 - the station cannot act right now. Says nothing about support."""


@dataclass(frozen=True, slots=True)
class ServiceModeEntry:
    """61 02 - the station has entered service mode. Observed on the YD7010 on
    2026-08-04 as the first reply to a service-mode read."""


@dataclass(frozen=True, slots=True)
class PowerState:
    """61 00 / 61 01 - track power off / on."""

    on: bool


@dataclass(frozen=True, slots=True)
class EmergencyStopBroadcast:
    """81 00 81."""


@dataclass(frozen=True, slots=True)
class LocoInfo:
    """E4 IDENT SPD FA FB.

    `address` is always None from `parse`: the reply carries no address field at
    all. The station knows which locomotive it asked about and attaches it with
    `dataclasses.replace`; inventing it here would publish one locomotive's
    speed under another's number.

    `speed`, `direction` and `emergency_stopped` are None unless the ident byte
    says 128 speed steps, because `speed.py` defines only the 128-step wire
    layout. A 14/27/28-step reply keeps its `raw_speed` and reports the rest as
    UNKNOWN rather than decoding it with the wrong layout.

    `function_bits` has exactly FUNCTIONS_IN_LOCO_INFO entries, F0..F12. F13..F28
    are not carried by this reply and are absent rather than defaulted to False.
    """

    raw_ident: int
    raw_speed: int
    speed_steps: int | None
    in_use_by_other: bool
    function_bits: tuple[bool, ...]
    speed: int | None = None
    direction: Direction | None = None
    emergency_stopped: bool | None = None
    address: int | None = None


@dataclass(frozen=True, slots=True)
class FunctionState13To28:
    """E3 52 D1 D2 - the ON/OFF state of F13..F28 (Lenz 23151 section 3.1.9.2).

    Answers the E3 09 request. Measured 2026-08-04 (docs/probe-results.md,
    "Settled": "F13-F28 state readable | yes, E3 09 -> E3 52 D1 D2 | closes the
    blind-clear side effect").

    This is the ONLY reply form that carries F13..F28. LocoInfo stops at F12, and
    E4 23 / E4 28 write all eight bits of their group at once, so a station with
    nothing to read from would have to seed zeros and switch off every function
    in the group it never saw.
    """

    f13_f20: int
    f21_f28: int


@dataclass(frozen=True, slots=True)
class Other:
    """Anything this module does not turn into a typed reply, bytes preserved.

    `reason` keeps three different causes apart, because their remedies are
    opposite:

    - "checksum" - the XOR did not hold. The LINK is damaging bytes; check the
      cable, the port and Link.stats().bad_xor.
    - "length"   - the frame did not match the length its header declares. The
      read window closed early, or this is not a telegram at all.
    - "empty"    - decoded cleanly but carries no data byte to dispatch on. No
      reply form in the index table has a zero-length body.
    - "unknown_form" - well formed, correct length, good XOR, and in a form
      nobody has listed. The REPLY TABLE is incomplete; this is the one that
      wants a new row.

    Collapsing these into one value leaves the station unable to tell a bad cable
    from a reply form we do not know, and both then read as an unresolved
    capability. The default keeps `Other(telegram)` constructible positionally,
    which is the shape the design document uses (spec line 538).
    """

    telegram: bytes
    reason: str = "unknown_form"


Reply = (
    GenericAck
    | InterfaceStatus
    | StationVersion
    | StationStatus
    | CvValue
    | PagedCvValue
    | Ready
    | Busy
    | NoAck
    | ShortCircuit
    | TrackShortCircuit
    | Unsupported
    | TransferError
    | StationBusy
    | ServiceModeEntry
    | PowerState
    | EmergencyStopBroadcast
    | LocoInfo
    | FunctionState13To28
    | Other
)

GENERIC_ACK = GenericAck()
READY = Ready()
BUSY = Busy()
NO_ACK = NoAck()
SHORT_CIRCUIT = ShortCircuit()
TRACK_SHORT_CIRCUIT = TrackShortCircuit()
UNSUPPORTED = Unsupported()
TRANSFER_ERROR = TransferError()
STATION_BUSY = StationBusy()
SERVICE_MODE_ENTRY = ServiceModeEntry()
POWER_ON = PowerState(on=True)
POWER_OFF = PowerState(on=False)
EMERGENCY_STOP_BROADCAST = EmergencyStopBroadcast()

# Not an answer either way. None of these say anything about whether an opcode
# is implemented, so every capability verdict must treat them as unresolved.
#
# Naming the set once is the point. Unsupported is the ONLY reply that entitles
# anything above to record a capability as False; if each consumer re-derives
# "which ones mean nothing" by hand, the one that forgets StationBusy records a
# busy station as an absent capability - the M1 failure again.
TRANSIENT_REPLIES: frozenset[Reply] = frozenset(
    {SHORT_CIRCUIT, TRACK_SHORT_CIRCUIT, BUSY, STATION_BUSY, TRANSFER_ERROR}
)

# Every reply that shares header 0x61. The two at 0x80 and 0x81 are easy to
# forget because they are not programming replies, but leaving one unparsed is
# how a station saying "I could not process that" gets recorded as one saying
# "I support that".
HEADER_61_REPLIES: dict[int, Reply] = {
    0x00: POWER_OFF,
    0x01: POWER_ON,
    0x02: SERVICE_MODE_ENTRY,
    0x08: TRACK_SHORT_CIRCUIT,
    0x11: READY,
    0x12: SHORT_CIRCUIT,
    0x13: NO_ACK,
    0x1F: BUSY,
    0x80: TRANSFER_ERROR,
    0x81: STATION_BUSY,
    0x82: UNSUPPORTED,
}


def _loco_info(data: bytes) -> LocoInfo:
    ident, raw_speed, fa, fb = data[0], data[1], data[2], data[3]
    speed_steps = SPEED_STEP_MODES.get(ident & IDENT_SPEED_STEPS_MASK)
    function_bytes = (fa, fb)
    function_bits = tuple(
        bool(function_bytes[index] & mask) for index, mask in LOCO_INFO_FUNCTION_BITS
    )
    if speed_steps != SPEED_STEPS:
        return LocoInfo(
            raw_ident=ident,
            raw_speed=raw_speed,
            speed_steps=speed_steps,
            in_use_by_other=bool(ident & IDENT_BUSY_MASK),
            function_bits=function_bits,
        )
    step, direction, emergency = decode_speed_128(raw_speed)
    return LocoInfo(
        raw_ident=ident,
        raw_speed=raw_speed,
        speed_steps=speed_steps,
        in_use_by_other=bool(ident & IDENT_BUSY_MASK),
        function_bits=function_bits,
        speed=step,
        direction=direction,
        emergency_stopped=emergency,
    )


def parse(telegram: bytes) -> Reply:
    """Turn one bare telegram into a typed reply. Never raises.

    The three failure causes are kept apart in Other.reason - see Other. The
    XBusChecksumError branch must come first: it is a subclass of
    XBusDecodeError, so catching ProtocolError first would swallow it and every
    corrupt link would look like a truncated frame.
    """
    try:
        header, data = codec.decode(telegram)
    except XBusChecksumError:
        return Other(telegram=telegram, reason="checksum")
    except ProtocolError:
        return Other(telegram=telegram, reason="length")
    if not data:
        return Other(telegram=telegram, reason="empty")
    db0 = data[0]

    if header == HDR_INTERFACE:
        return GENERIC_ACK if db0 == DB_GENERIC_ACK else InterfaceStatus(code=db0)
    if header == HDR_PROGRAMMING and db0 in HEADER_61_REPLIES:
        return HEADER_61_REPLIES[db0]
    if header == HDR_STATUS and db0 == DB_STATUS:
        return StationStatus.from_raw(data[1])
    if header == HDR_RESULT_5 and db0 == DB_PAGED_RESULT:
        return PagedCvValue(raw_register=data[1], value=data[2])
    if header == HDR_RESULT_5 and db0 in CV_RESULT_IDENTS:
        return CvValue(raw_cv=data[1], value=data[2], ident=db0, z21_form=False)
    if header == HDR_RESULT_5 and db0 == DB_VERSION:
        return StationVersion(raw=data[1], station_id=data[2])
    if header == HDR_RESULT_6 and db0 == DB_Z21_CV_RESULT:
        return CvValue(
            raw_cv=join_cv_field(data[1], data[2]),
            value=data[3],
            ident=db0,
            z21_form=True,
        )
    if header == HDR_BROADCAST and db0 == DB_EMERGENCY_STOP:
        return EMERGENCY_STOP_BROADCAST
    if header == HDR_FUNCTION_STATE and db0 == DB_FUNCTION_STATE_13_28:
        return FunctionState13To28(f13_f20=data[1], f21_f28=data[2])
    # The identification-byte guard, not just the header. See IDENT_RESERVED_MASK:
    # E4 F8 and E4 13 are COMMANDS under the same header, and without this test
    # E4 F8 00 03 40 would come back as LocoInfo(raw_ident=0xF8) claiming 14
    # speed steps and in_use_by_other for a locomotive nobody asked about.
    if header == HDR_LOCO_INFO and not data[0] & IDENT_RESERVED_MASK:
        return _loco_info(data)
    return Other(telegram=telegram, reason="unknown_form")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q`
Expected: PASS, 51 tests

- [ ] **Step 5: Add the exhaustive dispatch test and its converse**

Append to `tests/unit/test_xbus_replies.py`:

```python
from railctl.xbus.commands import FUNCTION_BITS, FunctionGroup  # noqa: E402
from railctl.xbus.replies import (  # noqa: E402
    Busy,
    NoAck,
    Ready,
    ServiceModeEntry,
    ShortCircuit,
    StationBusy,
    TrackShortCircuit,
    TransferError,
)

# Written from the protocol documents, NOT from the parser, so a disagreement is
# a disagreement about the protocol rather than about control flow.
EXPECTED_61 = {
    0x00: PowerState,
    0x01: PowerState,
    0x02: ServiceModeEntry,
    0x08: TrackShortCircuit,
    0x11: Ready,
    0x12: ShortCircuit,
    0x13: NoAck,
    0x1F: Busy,
    0x80: TransferError,
    0x81: StationBusy,
    0x82: Unsupported,
}


def valid_telegram(header: int, db0: int) -> bytes:
    """A telegram of exactly the length its header declares, with a good XOR.

    Data bytes after the first are zero. Headers whose low nibble is 0 carry no
    data at all, so db0 is dropped for them - which is itself the right answer:
    no reply form in the index table has a zero-length body.
    """
    want = header & 0x0F
    data = ([db0] + [0x00] * want)[:want]
    return encode(header, *data)


def expected_type(header: int, db0: int) -> type:
    if header & 0x0F == 0:
        return Other
    if header == 0x01:
        return GenericAck if db0 == 0x04 else InterfaceStatus
    if header == 0x61 and db0 in EXPECTED_61:
        return EXPECTED_61[db0]
    if (header, db0) == (0x62, 0x22):
        return StationStatus
    if (header, db0) == (0x63, 0x10):
        return PagedCvValue
    if header == 0x63 and db0 in (0x14, 0x15, 0x16, 0x17):
        return CvValue
    if (header, db0) == (0x63, 0x21):
        return StationVersion
    if (header, db0) == (0x64, 0x14):
        return CvValue
    if (header, db0) == (0x81, 0x00):
        return EmergencyStopBroadcast
    if (header, db0) == (0xE3, 0x52):
        return FunctionState13To28
    if header == 0xE4 and db0 <= 0x0F:
        # The identification byte is 0000 BFFF, so only the low nibble is a
        # reply. E4 F8 and E4 13 are commands sharing the header.
        return LocoInfo
    return Other


def test_the_dispatch_table_matches_the_protocol_documents():
    """All 65536 header/db0 pairs, not a sample of them.

    Exhaustiveness is the point. The mutants that survived sampling in M1 were
    header comparisons weakened from == to >=, and each misbehaves for only a
    handful of specific byte pairs. A generated-input test can sweep the space
    but cannot promise to visit (0x62, 0x22) - which is exactly where "equal to"
    and "at least" stop agreeing.
    """
    wrong = []
    for header in range(256):
        for db0 in range(256):
            got = type(parse(valid_telegram(header, db0)))
            want = expected_type(header, db0)
            if got is not want:
                wrong.append((hex(header), hex(db0), want.__name__, got.__name__))
    assert not wrong, f"{len(wrong)} misparsed pairs, first few: {wrong[:6]}"


def test_no_header_pair_outside_the_table_produces_a_typed_reply():
    """The converse. Everything undocumented must land on Other.

    A parser that widens silently is how a station's "I could not process that"
    became "I support that": the reply had a header nobody had listed, and an
    unlisted reply that borrows a neighbour's meaning is worse than one that is
    not understood at all.
    """
    for header in range(256):
        for db0 in range(256):
            if expected_type(header, db0) is Other:
                assert isinstance(parse(valid_telegram(header, db0)), Other), (header, db0)


def test_parse_never_raises_on_anything_that_can_arrive_on_a_shared_port():
    corpus = [
        b"",
        b"\xff",
        b"\xff\xfe",
        b"\xff\xfe\x63\x21",
        b"TC=0 U=15.1 I=0\r\n",
        bytes(range(256)),
    ]
    for header in range(256):
        for length in range(0, 10):
            corpus.append(bytes([header]) * length)
            corpus.append(bytes([header] + [0xAA] * length))
    for header in range(256):
        for db0 in range(256):
            corpus.append(bytes([header, db0]))
    for telegram in corpus:
        parse(telegram)


# Every typed reply and the header pairs that are allowed to produce it. `Other`
# is the ONE deliberate exemption: it is the catch-all unknown and may come from
# any header pair at all, which is the whole reason it exists.
#
# The header-0x61 rows are derived from EXPECTED_61 rather than retyped, so the
# table cannot drift away from it. Unsupported is the one type this test most
# needs to constrain - it is the ONLY reply that entitles anything above to
# record a capability as False - and a hand-written table that happened to omit
# it would let a parser return UNSUPPORTED from an unrelated header untouched.
ALLOWED_HEADERS = {
    StationVersion: {(0x63, 0x21)},
    StationStatus: {(0x62, 0x22)},
    PagedCvValue: {(0x63, 0x10)},
    CvValue: {(0x63, 0x14), (0x63, 0x15), (0x63, 0x16), (0x63, 0x17), (0x64, 0x14)},
    FunctionState13To28: {(0xE3, 0x52)},
    LocoInfo: {(0xE4, db0) for db0 in range(0x10)},
    EmergencyStopBroadcast: {(0x81, 0x00)},
    GenericAck: {(0x01, 0x04)},
    InterfaceStatus: {(0x01, db0) for db0 in range(256) if db0 != 0x04},
} | {
    cls: {(0x61, db0) for db0, want in EXPECTED_61.items() if want is cls}
    for cls in set(EXPECTED_61.values())
}


def test_parse_only_claims_what_the_header_entitles_it_to():
    """A typed reply may only come from the headers that define it. Inventing a
    CvValue from an unrelated telegram would report a decoder value that no
    decoder ever sent."""
    assert set(ALLOWED_HEADERS) >= set(EXPECTED_61.values())
    for header in range(256):
        for db0 in range(256):
            telegram = valid_telegram(header, db0)
            reply = parse(telegram)
            for reply_type, headers in ALLOWED_HEADERS.items():
                if type(reply) is reply_type:
                    assert len(telegram) >= 2
                    assert (telegram[0], telegram[1]) in headers, telegram.hex(" ")


def test_the_header_61_singletons_are_distinct_objects():
    """The station compares some of these by identity, so two conditions sharing
    an object would make them indistinguishable."""
    from railctl.xbus.replies import HEADER_61_REPLIES

    assert len({id(reply) for reply in HEADER_61_REPLIES.values()}) == len(HEADER_61_REPLIES)


def test_the_loco_info_function_layout_agrees_with_the_command_byte_layout():
    """The reply's FA byte and the E4 20 command byte are the same layout, and
    FB packs group 2 into its low nibble and group 3 into its high nibble. If
    these ever disagree, the station re-asserts a function state it never read."""
    from railctl.xbus.replies import LOCO_INFO_FUNCTION_BITS

    for function in range(0, 5):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G1
        assert LOCO_INFO_FUNCTION_BITS[function] == (0, 1 << bit)
    for function in range(5, 9):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G2
        assert LOCO_INFO_FUNCTION_BITS[function] == (1, 1 << bit)
    for function in range(9, 13):
        group, bit = FUNCTION_BITS[function]
        assert group is FunctionGroup.G3
        assert LOCO_INFO_FUNCTION_BITS[function] == (1, 1 << (bit + 4))
```

- [ ] **Step 6: Run the new tests and see them pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q`
Expected: PASS, 57 tests

- [ ] **Step 7: Prove the exhaustive test can fail — weaken one header comparison**

A pinning test that was never seen red pins nothing, and the survivors this test exists to kill are
exactly header comparisons weakened from `==` to `>=` or `<=`. Edit
`src/railctl/xbus/replies.py`, changing exactly one line in `parse`:

```python
    if header == HDR_RESULT_5 and db0 <= DB_PAGED_RESULT:
```

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q -k dispatch_table`
Expected: FAIL —
`AssertionError: 16 misparsed pairs, first few: [('0x63', '0x0', 'Other', 'PagedCvValue'), ('0x63', '0x1', 'Other', 'PagedCvValue'), ...]`
— the sixteen db0 values 0x00..0x0F that `<=` swallows and `==` does not.

Then revert the line to `if header == HDR_RESULT_5 and db0 == DB_PAGED_RESULT:` and re-run:

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q`
Expected: PASS, 57 tests

- [ ] **Step 8: Prove the converse test can fail — add a header nobody documented**

Edit `src/railctl/xbus/replies.py` and insert this line immediately before the
final `return Other(telegram=telegram, reason="unknown_form")` in `parse`:

```python
    if header == 0x71:
        return CvValue(raw_cv=data[0], value=0, ident=0x71, z21_form=False)
```

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q -k "outside_the_table or entitles"`
Expected: FAIL, 2 tests — `test_no_header_pair_outside_the_table_produces_a_typed_reply` and
`test_parse_only_claims_what_the_header_entitles_it_to`, the second reporting `71 aa`.

Then delete those two lines and re-run:

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_replies.py -q`
Expected: PASS, 57 tests

- [ ] **Step 9: Prove no CV arithmetic escaped into the parser**

Run: `grep -nE 'cv *- *1|cv *\+ *1|>> *8|<< *8|% *256' src/railctl/xbus/replies.py`
Expected: no output, exit status 1. The `64 14` address bytes are joined by
`cv.join_cv_field`, and the version nibbles use `>> 4`, which is not a CV.

- [ ] **Step 10: Check lint and formatting**

Run: `.venv/bin/python -m ruff check src/railctl/xbus/replies.py tests/unit/test_xbus_replies.py`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add src/railctl/xbus/replies.py tests/unit/test_xbus_replies.py
git commit -m "feat(xbus): add total reply parser with exhaustive dispatch pinning"
```

---

### Task 9: The golden vector table (`tests/vectors.py`)

**Files:**
- Create: `tests/vectors.py`
- Test: `tests/unit/test_xbus_vectors.py`
- Modify: `.github/workflows/ci.yml` — add the coverage step Task 1 and Task 3 of the M2 part
  deferred to M3 (Step 11 below). Nothing else outside `tests/` is touched; in particular this
  task makes **no** edit to `pyproject.toml`, because Task 1 of the M2 part already wrote the one
  `[tool.ruff.lint.isort]` section this plan has and a second header is a TOML parse error.

`tests/vectors.py` is a helper module, not a test module — pytest will not collect it (the name
does not start with `test_`), and the test file imports it as `from tests.vectors import
ALL_VECTORS`. That import works because Task 1 made `tests/` a package with an `__init__.py` and
set `pythonpath = ["src", "."]`, so the repository root is on `sys.path` and `tests.vectors` is a
real module path. A bare `from vectors import ...` would **not** work under that layout, which is
why the table lives at `tests/vectors.py` and is imported by its package path.

Why this file exists on top of the per-encoder tests in Tasks 7 and 8: these rows are the ones the
design names as the top bug sources, and keeping them in one named-constant table means an
intentional encoder change is **one reviewed edit**, not a dozen scattered literals. The two
self-consistency tests then run over every row, so a mistyped byte in the table itself fails
before it can pin a wrong expectation.

**Interfaces:**

- Consumes:
  - `railctl.xbus.codec.xor(data: bytes) -> int` (Task 4)
  - `railctl.xbus.commands` (Task 7): `cmd_drive_128`, `cmd_service_direct_read`,
    `cmd_service_ext_read`, `cmd_pom_read_byte`, `cmd_z21_cv_read`
  - `railctl.xbus.dialect.XPRESSNET`, `railctl.xbus.dialect.Z21` (Task 4) —
    `.long_address_threshold` is 100 and 128
  - `railctl.xbus.speed.Direction` (Task 5)
  - `railctl.xbus.replies` (Task 8): `parse`, the `Reply` union, and the five reply classes the
    decode rows construct their expected object from — `StationStatus(raw, emergency_off,
    emergency_stop, auto_start_mode, service_mode, powering_up, ram_error)`,
    `CvValue(raw_cv, value, ident, z21_form)`, `PagedCvValue(raw_register, value)`,
    `Unsupported()` (no fields) and `Other(telegram, reason="unknown_form")`. The field names and
    the `Other` default are Task 8's, verbatim; a row that constructs a reply with a field Task 8
    does not declare fails at import with
    `TypeError: CvValue.__init__() got an unexpected keyword argument 'cv'`, before a test runs.
  - `.github/workflows/ci.yml` with the `test` job (Task 3), whose steps Step 11 appends to
- Produces (importable from `tests.vectors`):
  - `@dataclass(frozen=True) class EncodeVector(name: str, call: Callable[[], bytes], telegram: bytes, why: str)`
    — the `call` field is not optional and is second, because
    `test_each_encoder_produces_the_bytes_in_the_table` does `vector.call()`. A three-field version
    built from a summary makes every `ENCODE_VECTORS` literal fail with a `TypeError` on an
    unexpected positional argument.
  - `@dataclass(frozen=True) class DecodeVector(name: str, telegram: bytes, expected: Reply, why: str)`
    — `expected` is the CONSTRUCTED reply object `parse(telegram)` must return, not a tuple of the
    fields somebody thought worth checking. It sits **third**: input second, expectation third,
    reason last, the same order `EncodeVector` reads in. There is no default and every row supplies
    one, so a three-argument `DecodeVector(name, telegram, why)` copied from an older summary fails
    with `TypeError: DecodeVector.__init__() missing 1 required positional argument: 'why'` instead
    of quietly binding the reason string to `expected`.
  - `ENCODE_VECTORS: tuple[EncodeVector, ...]`
  - `DECODE_VECTORS: tuple[DecodeVector, ...]`
  - `ALL_VECTORS: tuple[EncodeVector | DecodeVector, ...]`
  - `XPRESSNET_THRESHOLD: int`, `Z21_THRESHOLD: int`
  - `UNKNOWN_TELEGRAM: bytes` — `71 AA DB`, named because that row is the one whose telegram is
    also part of its expected object: `parse` echoes the bytes back inside `Other`, so writing the
    literal twice would let the input and the expectation drift apart.

- [ ] **Step 1: Confirm the import sorter files `tests` and `railctl` in one block**

No `pyproject.toml` edit is needed here, and adding one would collide with the
`[tool.ruff.lint.isort]` section Task 1 of the M2 part already wrote. Two settings do that job
together, and both come from Task 1: `src = ["src", "."]` files `tests` as first-party (it is
found under `.`), and `known-first-party = ["railctl"]` files `railctl` as first-party. `src`
alone is **not** enough — measured against ruff 0.16.1, with only `src` set, `from railctl...` is
sorted into the third-party block next to `import typer`, and `tests` sits in a separate block
below it. With both settings in place `railctl` and `tests` belong in the **same** import block,
sorted alphabetically, with no blank line between them — which is how Steps 2 and 7 below are
written.

Confirm both settings are in place before writing any imports against them:

```bash
.venv/bin/python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['ruff']; print(d['src'], d['lint']['isort'])"
```

Expected: `['src', '.'] {'known-first-party': ['railctl']}`

Putting a blank line between `from railctl...` and `from tests.vectors...`, or ordering the two
the other way round, brings back `I001 Import block is un-sorted or un-formatted` and fails the
lint gate at the end of this task.

- [ ] **Step 2: Write the failing self-consistency tests**

```python
# tests/unit/test_xbus_vectors.py
"""Self-consistency of the golden vector table, and both directions across it.

Two tests run over EVERY row before any of it is used as an expectation:
xor(b[:-1]) == b[-1] and len(b) == (b[0] & 0x0F) + 2. A mistyped byte in the
table is then a failure of the table, not a silently wrong expectation that a
later encoder change gets blamed for.
"""

from __future__ import annotations

import pytest

from railctl.xbus.codec import xor
from tests.vectors import ALL_VECTORS, DECODE_VECTORS, ENCODE_VECTORS


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_carries_a_correct_xor(vector):
    assert xor(vector.telegram[:-1]) == vector.telegram[-1]


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_has_the_length_its_header_declares(vector):
    assert len(vector.telegram) == (vector.telegram[0] & 0x0F) + 2


@pytest.mark.parametrize("vector", ALL_VECTORS, ids=lambda v: v.name)
def test_every_vector_says_why_it_exists(vector):
    """A row nobody can justify is a row nobody will dare to change."""
    assert vector.why.strip()


def test_the_table_is_not_silently_empty():
    assert len(ENCODE_VECTORS) >= 14
    assert len(DECODE_VECTORS) >= 5
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'tests.vectors'`

The module name in that message carries the `tests.` prefix because Task 1 made `tests/` a package. A bare `No module named 'vectors'` would mean the import in Step 2 was written as `from vectors import ...`, which cannot work under this layout — go back and fix the import rather than the expectation.

- [ ] **Step 4: Write the vector table**

```python
# tests/vectors.py
"""Golden byte vectors, as named constants.

Every row here is copied verbatim from the design document, with the one-line
reason that document gives for why the row exists. These are the top bug sources
of this protocol: the dialect divergence band, the three different CV encodings,
and the four reply forms that must stay distinguishable.

Keeping them in one table means an intentional encoder change is one reviewed
edit rather than a dozen scattered literals - and the two self-consistency tests
in tests/unit/test_xbus_vectors.py check the table itself before anything uses it.

An encode row carries the bytes its call must produce. A decode row carries the
whole reply OBJECT its telegram must parse to, built here by keyword from the
classes in railctl.xbus.replies, so the comparison in the test is one `==` over
the entire dataclass rather than a hand-picked list of fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from railctl.xbus.commands import (
    cmd_drive_128,
    cmd_pom_read_byte,
    cmd_service_direct_read,
    cmd_service_ext_read,
    cmd_z21_cv_read,
)
from railctl.xbus.dialect import XPRESSNET, Z21
from railctl.xbus.replies import (
    CvValue,
    Other,
    PagedCvValue,
    Reply,
    StationStatus,
    Unsupported,
)
from railctl.xbus.speed import Direction

XPRESSNET_THRESHOLD = XPRESSNET.long_address_threshold  # 100
Z21_THRESHOLD = Z21.long_address_threshold  # 128


@dataclass(frozen=True)
class EncodeVector:
    """One encoder call and the bytes it must produce.

    `call` is annotated `Callable[[], bytes]`, not `object`: the tests below
    invoke it, and `object` is not callable to any type checker, which this
    repo's conventions require to pass.
    """

    name: str
    call: Callable[[], bytes]
    telegram: bytes
    why: str


@dataclass(frozen=True)
class DecodeVector:
    """One reply telegram and the WHOLE object `parse` must return for it.

    `expected` is a constructed reply instance, and it sits third - input
    second, expectation third, reason last - so this dataclass reads the same
    way round as EncodeVector.

    It replaces a field-by-field assertion, which is weaker in exactly the
    direction this protocol keeps producing bugs. `(reply.raw_cv, reply.value,
    reply.ident, reply.z21_form) == (8, 8, 0x14, False)` keeps passing after the
    parser grows a field, stops setting a field it used to set, or renames one.
    A frozen dataclass compares on its whole field tuple AND on its class, so
    `parse(telegram) == expected` fails on all three.
    """

    name: str
    telegram: bytes
    expected: Reply
    why: str


def _b(text: str) -> bytes:
    return bytes.fromhex(text)


FWD = Direction.FORWARD

ENCODE_VECTORS: tuple[EncodeVector, ...] = (
    EncodeVector(
        "drive_128(99, fwd, 1) xpressnet",
        lambda: cmd_drive_128(99, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 00 63 82 16"),
        "below the XpressNet threshold",
    ),
    EncodeVector(
        "drive_128(100, fwd, 1) xpressnet",
        lambda: cmd_drive_128(100, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 64 82 D1"),
        "at the threshold",
    ),
    EncodeVector(
        "drive_128(100, fwd, 1) z21",
        lambda: cmd_drive_128(100, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 00 64 82 11"),
        "dialects disagree in 100..127",
    ),
    EncodeVector(
        "drive_128(127, fwd, 1) xpressnet",
        lambda: cmd_drive_128(127, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 7F 82 CA"),
        "top of the divergence band",
    ),
    EncodeVector(
        "drive_128(127, fwd, 1) z21",
        lambda: cmd_drive_128(127, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 00 7F 82 0A"),
        "top of the divergence band, the short form the other dialect does not send",
    ),
    EncodeVector(
        "drive_128(128, fwd, 1) xpressnet",
        lambda: cmd_drive_128(128, 1, FWD, threshold=XPRESSNET_THRESHOLD),
        _b("E4 13 C0 80 82 35"),
        "dialects agree again",
    ),
    EncodeVector(
        "drive_128(128, fwd, 1) z21",
        lambda: cmd_drive_128(128, 1, FWD, threshold=Z21_THRESHOLD),
        _b("E4 13 C0 80 82 35"),
        "dialects agree again",
    ),
    EncodeVector(
        "service_direct_read(1)",
        lambda: cmd_service_direct_read(1),
        _b("22 15 01 36"),
        "direct CV is NOT zero-based",
    ),
    EncodeVector(
        "service_direct_read(255)",
        lambda: cmd_service_direct_read(255),
        _b("22 15 FF C8"),
        "MAX_CV_DIRECT",
    ),
    EncodeVector(
        "service_ext_read(1)",
        lambda: cmd_service_ext_read(1),
        _b("22 18 01 3B"),
        "band 0",
    ),
    EncodeVector(
        "service_ext_read(256)",
        lambda: cmd_service_ext_read(256),
        _b("22 19 00 3B"),
        "22 18 00 is NOT CV256",
    ),
    EncodeVector(
        "service_ext_read(1024)",
        lambda: cmd_service_ext_read(1024),
        _b("22 18 00 3A"),
        "CV1024 is page 0 with C = 0",
    ),
    EncodeVector(
        "pom_read_byte(3, 1)",
        lambda: cmd_pom_read_byte(3, 1, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E4 00 00 31"),
        "POM is zero-based",
    ),
    EncodeVector(
        "pom_read_byte(3, 8)",
        lambda: cmd_pom_read_byte(3, 8, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E4 07 00 36"),
        "the probe telegram",
    ),
    EncodeVector(
        "pom_read_byte(3, 257)",
        lambda: cmd_pom_read_byte(3, 257, threshold=XPRESSNET_THRESHOLD),
        _b("E6 30 00 03 E5 00 00 30"),
        "crosses into MM=1",
    ),
    EncodeVector(
        "z21_cv_read(29)",
        lambda: cmd_z21_cv_read(29),
        _b("23 11 00 1C 2E"),
        "16-bit zero-based",
    ),
)

# The unknown row's telegram appears twice - once as the input, once inside the
# Other the parser echoes it back in - so it is named rather than typed twice.
UNKNOWN_TELEGRAM = _b("71 AA DB")

DECODE_VECTORS: tuple[DecodeVector, ...] = (
    DecodeVector(
        "station status 62 22 07",
        _b("62 22 07 47"),
        # Written out field by field on purpose, NOT as StationStatus.from_raw(0x07):
        # from_raw is the parser's own bit-mask code, so calling it here would make
        # the expectation agree with the parser by construction and the row would
        # stop being evidence of anything.
        StationStatus(
            raw=0x07,
            emergency_off=True,
            emergency_stop=True,
            auto_start_mode=True,
            service_mode=False,
            powering_up=False,
            ram_error=False,
        ),
        "measured: emergency off + emergency stop + automatic start mode",
    ),
    DecodeVector(
        "lenz cv result 63 14 08 08",
        _b("63 14 08 08 77"),
        # ident is kept because 63 14/15/16/17 are four different result bands, and
        # z21_form False is what tells cv.resolve_service_cv this is the 8-bit form.
        CvValue(raw_cv=0x08, value=0x08, ident=0x14, z21_form=False),
        "a CV result whose CV number only the caller can resolve",
    ),
    DecodeVector(
        "z21 cv result 64 14 00 07 91",
        _b("64 14 00 07 91 E6"),
        # raw_cv 7 is the two address bytes joined by cv.join_cv_field, and it names
        # no CV number: whether it means CV7 or CV8 is cv.resolve_service_cv's job
        # and is UNMEASURED for this form. What the row pins is that the field
        # arrives whole and that z21_form is True, which is how the caller knows
        # which resolution rule to apply.
        CvValue(raw_cv=7, value=145, ident=0x14, z21_form=True),
        "doc only (spec line 573); never seen on this station, parsed so a Z21 LAN "
        "transport or a firmware update cannot lose the value",
    ),
    DecodeVector(
        "paged result 63 10 01 03",
        _b("63 10 01 03 71"),
        # A PagedCvValue and not a CvValue. The number is a REGISTER; reading it as
        # a CV publishes a value the decoder never sent. The class is half of what
        # the comparison checks, which is why the test asserts the type as well.
        PagedCvValue(raw_register=1, value=3),
        "a VALID answer meaning register-mode fallback, not an error",
    ),
    DecodeVector(
        "not supported 61 82",
        _b("61 82 E3"),
        # parse returns the UNSUPPORTED singleton; a freshly constructed Unsupported
        # compares equal to it, because a frozen dataclass compares on its class and
        # its field tuple and not on identity. Constructing one keeps the row
        # independent of which object the module happens to hand back.
        Unsupported(),
        "the only reply that entitles anything above to record a capability as False",
    ),
    DecodeVector(
        "unknown 71 AA",
        UNKNOWN_TELEGRAM,
        # reason is spelled out rather than left to the default, because the whole
        # point of Other is that "unknown_form", "checksum", "length" and "empty"
        # stay apart: a well-formed telegram in a form nobody listed must not come
        # back looking like a damaged one.
        Other(telegram=UNKNOWN_TELEGRAM, reason="unknown_form"),
        "must produce Other with no exception",
    ),
)

ALL_VECTORS: tuple[EncodeVector | DecodeVector, ...] = ENCODE_VECTORS + DECODE_VECTORS
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: PASS, 67 tests

- [ ] **Step 6: Prove the self-consistency tests can fail**

Edit `tests/vectors.py`, changing the `service_ext_read(1024)` row from `22 18 00 3A` to
`22 18 00 3B`.

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q -k xor`
Expected: FAIL, 1 test — `test_every_vector_carries_a_correct_xor[service_ext_read(1024)]`,
`assert 58 == 59`.

Restore `3A` and re-run:

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: PASS, 67 tests

- [ ] **Step 7: Write the failing encode and decode tests that consume the table**

Append to `tests/unit/test_xbus_vectors.py`:

```python
from railctl.xbus.commands import cmd_drive_128  # noqa: E402
from railctl.xbus.replies import parse  # noqa: E402
from railctl.xbus.speed import Direction  # noqa: E402
from tests.vectors import XPRESSNET_THRESHOLD, Z21_THRESHOLD  # noqa: E402


@pytest.mark.parametrize("vector", ENCODE_VECTORS, ids=lambda v: v.name)
def test_each_encoder_produces_the_bytes_in_the_table(vector):
    assert vector.call() == vector.telegram


@pytest.mark.parametrize("address", [100, 110, 127])
def test_the_two_dialects_disagree_inside_the_divergence_band(address: int):
    """On XpressNet, addresses 100..127 go out as LONG DCC addresses. A decoder
    configured short in that range (CV1 = 100..127, CV29 bit 5 = 0) simply does
    nothing, with no error."""
    xn = cmd_drive_128(address, 1, Direction.FORWARD, threshold=XPRESSNET_THRESHOLD)
    z21 = cmd_drive_128(address, 1, Direction.FORWARD, threshold=Z21_THRESHOLD)
    assert xn != z21


@pytest.mark.parametrize("address", [1, 99, 128, 1234, 9999])
def test_the_two_dialects_agree_outside_the_divergence_band(address: int):
    xn = cmd_drive_128(address, 1, Direction.FORWARD, threshold=XPRESSNET_THRESHOLD)
    z21 = cmd_drive_128(address, 1, Direction.FORWARD, threshold=Z21_THRESHOLD)
    assert xn == z21


@pytest.mark.parametrize("vector", DECODE_VECTORS, ids=lambda v: v.name)
def test_each_decode_row_parses_to_the_whole_object_in_the_table(vector):
    """One comparison per row, over the ENTIRE dataclass.

    This is the clause of design line 1579 that says a decode vector "compares
    equal as a dataclass". Asserting a chosen list of fields instead - say
    (reply.raw_cv, reply.value, reply.ident, reply.z21_form) == (8, 8, 0x14,
    False) - still passes when the parser grows a field nobody looked at, stops
    setting a field it used to set, or renames one. A frozen dataclass compares
    on its whole field tuple, so `==` catches all three.

    The type assertion is not there to catch more: dataclass __eq__ already
    returns NotImplemented across classes, so a PagedCvValue would never compare
    equal to a CvValue with the same numbers. It is there so the failure reads
    "PagedCvValue is not CvValue" instead of a field diff between two objects
    that are not even the same kind of reply.
    """
    reply = parse(vector.telegram)
    assert type(reply) is type(vector.expected)
    assert reply == vector.expected


@pytest.mark.parametrize("vector", DECODE_VECTORS, ids=lambda v: v.name)
def test_no_decode_row_raises(vector):
    """Stated separately from the comparison above, which would also go red on an
    exception. `parse` is TOTAL - the row that must not raise is the unknown one,
    71 AA DB, and this is where the table says so in one line."""
    parse(vector.telegram)
```

- [ ] **Step 8: Run the tests and see them pass**

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: PASS, 103 tests

These tests cannot be seen red by writing them first: the encoders and the parser they consume
already exist and are already correct, so there is nothing to make fail. Step 9 is what proves they
are capable of failing, and it is not optional — a pinning test that was never seen red pins
nothing.

- [ ] **Step 9: Prove the encode and the decode tests can fail**

Both directions, one mutation each, each reverted before the next.

**The encode direction.** Edit `src/railctl/xbus/commands.py` and change `cmd_service_ext_read` to
use `EXT_READ_OPCODES[0]` unconditionally instead of `EXT_READ_OPCODES[page]`.

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q -k service_ext_read`
Expected: FAIL, 1 test — `test_each_encoder_produces_the_bytes_in_the_table[service_ext_read(256)]`,
showing `22 18 00` where `22 19 00` was expected. This is the exact confusion the row exists for:
`22 18 00` is CV1024, not CV256.

Revert the edit and re-run:

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: PASS, 103 tests

**The decode direction.** Edit `src/railctl/xbus/replies.py` and change the final line of `parse`
from `return Other(telegram=telegram, reason="unknown_form")` to
`return Other(telegram=telegram, reason="length")` — a parser that files a well-formed telegram it
does not recognise under the label meaning "the frame did not arrive".

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q -k decode_row`
Expected: `1 failed, 11 passed` —
`test_each_decode_row_parses_to_the_whole_object_in_the_table[unknown 71 AA]`, with
`AssertionError: assert Other(telegram=b'q\xaa\xdb', reason='length') == Other(telegram=b'q\xaa\xdb', reason='unknown_form')`.

This is the mutation that makes the whole-object comparison worth having. A row-by-row test that
asserted `isinstance(reply, Other)` and `reply.telegram == vector.telegram` — the obvious pair of
fields to check by hand — stays green under it, because `reason` is the field nobody thought to
name. Comparing the object leaves nothing to think of. (Task 8's suite catches this one too; the
point here is what **this** file can see on its own, which is why the run is scoped to the vectors
test module.)

Revert the line and re-run:

Run: `.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q`
Expected: PASS, 103 tests

- [ ] **Step 10: Run the whole suite so nothing in the probe collides**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the 292 M1 probe tests, now under `tests/probe/`, plus every railctl test added
by Tasks 1–9, with no `import file mismatch` error. Task 1 put an `__init__.py` in every test
directory, so `tests.probe.test_commands` and `tests.unit.test_xbus_commands` are distinct module
names and a shared basename could not have collided anyway.

- [ ] **Step 11: Add the coverage gate to CI**

Task 1 configured `[tool.coverage]` with `fail_under = 90` and deliberately kept `--cov` out of
`addopts`, and Task 3 left the CI workflow with no coverage step, both saying the gate arrives
with M3 "once `xbus/` gives it something to measure". `xbus/` now exists in full, so this is where
that promise is kept. Nothing else in the plan adds it.

In `.github/workflows/ci.yml`, insert this step in the `test` job immediately after the `Tests`
step and before `The M1 probe still imports`:

```yaml
      - name: Coverage
        # Configured in pyproject.toml since M2 (fail_under = 90, branch = true,
        # serial_posix.py omitted) but only switched on here: at the end of M2 the
        # package was __init__.py plus errors.py, and a 90% gate over that measures
        # nothing. It stays out of addopts permanently, because mutation/*.toml run
        # the suite once per mutant.
        run: uv run pytest --cov --cov-report=term-missing
```

`uv run`, not `python -m pytest`: every other command in this workflow goes through `uv run` since
Task 3, and the job never puts a bare `python` on the path — `astral-sh/setup-uv` provisions the
interpreter for uv, not for the shell. A bare `python -m pytest` here would fail the job on a
missing interpreter, or, worse, find a system Python and measure coverage against a different
environment than the one the tests just ran in.

Confirm it passes locally before pushing, through the same entry point CI uses:

```bash
uv run pytest --cov --cov-report=term-missing
```

Expected: the coverage table, then `Required test coverage of 90% reached.` and `0 failed`. If it
reports a figure under 90, do **not** lower `fail_under`: name the uncovered lines from the
`term-missing` column and add the tests, or state in the commit message why a line is
unreachable.

- [ ] **Step 12: Check lint and formatting**

Run: `.venv/bin/python -m ruff check tests/vectors.py tests/unit/test_xbus_vectors.py`
Expected: `All checks passed!` — this is the step Step 1 exists for. If `railctl` and
`tests.vectors` end up in two blocks with a blank line between them, ruff reports
`I001 Import block is un-sorted or un-formatted` and exits 1.

- [ ] **Step 13: Confirm the M3 acceptance criterion, with the exact command**

Design doc line 1579 states what must be true when M3 is done: "every encode vector matches byte
for byte, every decode vector compares equal as a dataclass, and both self-consistency tests pass
over the whole table. No hardware needed." One command covers all three clauses, and this is the
last task of M3, so it is checked here:

```bash
.venv/bin/python -m pytest tests/unit/test_xbus_vectors.py -q
```

Expected: `103 passed`. Read the three clauses off that run:

| Clause of line 1579 | Test that proves it |
|---|---|
| every encode vector matches byte for byte | `test_each_encoder_produces_the_bytes_in_the_table` (one case per `ENCODE_VECTORS` row) |
| every decode vector compares equal as a dataclass | `test_each_decode_row_parses_to_the_whole_object_in_the_table` (one case per `DECODE_VECTORS` row), asserting `parse(row.telegram) == row.expected` — one `==` over the entire frozen dataclass, plus `type(...) is type(...)` so a mismatch of class reads as one; and `test_no_decode_row_raises` over every row |
| both self-consistency tests pass over the whole table | `test_every_vector_carries_a_correct_xor` and `test_every_vector_has_the_length_its_header_declares`, both parametrised over `ALL_VECTORS` |

The middle row is the clause worth reading literally. "Compares equal as a dataclass" means the
object, not a selection of its fields: `DecodeVector.expected` holds the reply `parse` must return,
and Step 9 shows the comparison going red on a change a field-by-field assertion let through.

If any parametrised id is missing from the run, the table shrank; compare the id list against
`ENCODE_VECTORS` and `DECODE_VECTORS` before accepting a green run.

- [ ] **Step 14: Commit**

```bash
git add tests/vectors.py tests/unit/test_xbus_vectors.py .github/workflows/ci.yml
git commit -m "test(xbus): add named golden vector table with self-consistency checks"
```

---

---

## Part M4 - transport, envelope, and the link

Four tasks. They are ordered so that nothing is written before the thing it needs exists:
the envelope first (pure, no I/O), then the test double that stands in for a station, then
`Link` (which needs both), then the real serial port and `open_link` (which needs `Link`).

The whole part exists because of one M1 defect. The probe's `FakeLink` answered a payload
with the same bytes every single time, so a station that returns **nothing** to a read and
produces the value only when asked **again** could not be expressed at all. That is exactly
the station this hardware turns out to be (`docs/probe-results.md`: `22 15`, `22 18` and
`22 19` deliver their result "only after `21 10` is sent"), and every mutant inside the
service-result poll loop survived the suite, including the one that deletes the loop.
Task 11 builds the fake so that the same request can be answered differently each time, and
contains the test that fails if that property is lost.

---

### Task 10: The envelope layer - `Frame`, `Kind`, `EnvelopeStats`, `LiUsbEnvelope`

**Files:**
- Create: `src/railctl/envelope/__init__.py`
- Create: `src/railctl/envelope/liusb.py`
- Create: `tests/unit/test_envelope_liusb.py` (the only file in the link and transport suites allowed to spell the framing bytes out)
- Create: `tests/unit/test_envelope_isolation.py`
- Modify: `tests/conftest.py` - append the `envelope_factory` fixture at the end of the file, after the `settings.load_profile(...)` call. Do not go by line number: Task 5 replaced the one-line `ci` profile registration with a seven-line call, so everything below it has moved.
- Not touched, and named here so nobody adds it back: `pyproject.toml` is **not** edited. The one `[tool.ruff.lint.isort] known-first-party = ["railctl"]` section this plan has was written by Task 1 of the M2 part, because Tasks 4-9 already commit test files that import both `railctl` and `tests`; adding a second `[tool.ruff.lint.isort]` header here is a TOML parse error and pytest would refuse to start. Step 1 checks it instead of writing it.

**Interfaces:**
- Consumes:
  - Nothing from `railctl` itself. `railctl` must already be importable from `src/` (Task 1).
  - The test package tree from Task 1: `tests/unit/` already exists and already carries `__init__.py`. This task creates no `__init__.py`.
  - `tests/conftest.py` as Task 5 left it (the `ci` hypothesis profile with `derandomize=True`).
  - The five files from Tasks 4, 7 and 8 that legitimately name the framing prefix: `src/railctl/xbus/codec.py`, `src/railctl/xbus/commands.py`, `tests/unit/test_codec.py`, `tests/unit/test_xbus_commands.py` and `tests/unit/test_xbus_replies.py`. `test_envelope_isolation.py` asserts the offender set is **equal** to its allow-list, so a missing file fails it just as an extra one does. If this task is ever run before M3, that is the failure to expect, and the fix is to run M3 first - not to shorten the list.
- Produces:
  - `railctl.envelope.Kind` - `enum.Enum` with members `SOLICITED = "solicited"` and `UNSOLICITED = "unsolicited"`
  - `railctl.envelope.Frame` - `@dataclass(frozen=True, slots=True)` with fields `kind: Kind`, `payload: bytes`
  - `railctl.envelope.EnvelopeStats` - `@dataclass` with `frames_ok: int = 0`, `bytes_dropped: int = 0`, `bad_xor: int = 0`, `stray_replies: int = 0`, `resyncs: int = 0`
  - `railctl.envelope.Envelope` - `Protocol` with `wrap(telegram: bytes) -> bytes`, `frame(kind: Kind, telegram: bytes) -> bytes`, `feed(data: bytes) -> None`, `pop() -> Frame | None`, `note_request(telegram: bytes) -> None`, `note_reply(frame: Frame) -> None`, `note_abandoned() -> None`, `reset() -> None`, and the properties `expects_ack: bool` and `stats: EnvelopeStats`
  - `railctl.envelope.hex_bytes(data: bytes) -> str` - uppercase, space separated
  - `railctl.envelope.liusb.LiUsbEnvelope` - class with the above methods, `expects_ack` always `True`
  - `railctl.envelope.liusb.MAX_BUFFER: int = 4096`
  - `tests/conftest.py` fixture `envelope_factory` - parametrised over `[LiUsbEnvelope]`, yields the **class**; call it to get an instance

**Layering note.** `liusb.py` computes its own XOR instead of importing `railctl.xbus.codec.xor`.
`xbus` sits *above* `link`, and the envelope sits *below* it; importing upward would invert the
layer diagram in the spec and make the `Z21Envelope` addition drag `xbus` behind it. The two
implementations are pinned against each other by a test in Task 12, which is allowed to import
both layers.

- [ ] **Step 1: Confirm ruff already files `railctl` as first-party, before writing any code**

Every file this part creates separates `import pytest` (or `import glob`) from the
`from railctl...` block with a blank line, which is the layout ruff's formatter and every
other file in the repo already use. `[tool.ruff.lint]` selects `I`, so the classification of
`railctl` decides whether that blank line is right or wrong.

`src = ["src", "."]` on its own does **not** decide it. Measured against ruff 0.16.1: with only
that setting, `from railctl.errors import ...` is sorted into the same block as `import typer`,
which is the third-party block. The setting that moves `railctl` next to `tests` is
`[tool.ruff.lint.isort] known-first-party = ["railctl"]`, and **Task 1 of the M2 part** writes it
into `pyproject.toml`, because Tasks 4-9 already commit test files that import both `railctl` and
`tests` and those files would be re-sorted the moment the setting appeared. This task therefore
makes no `pyproject.toml` edit at all: a second `[tool.ruff.lint.isort]` header anywhere in the
file is a TOML parse error and pytest would refuse to start.

Check the setting is present before writing any imports against it:

```bash
.venv/bin/python -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['ruff']['lint']['isort'])"
```

Expected: `{'known-first-party': ['railctl']}`

Then confirm the tree is clean before this task adds to it:

Run: `.venv/bin/python -m ruff check .`
Expected: `All checks passed!`

If the first command ends in `KeyError: 'isort'`, stop: the M2 pyproject did not land as written,
and the lint gate at the end of every one of the four tasks in this part reports `Found N errors`
instead. Fix it in the M2 pyproject block, not here.

- [ ] **Step 2: Write the failing envelope tests**

```python
# tests/unit/test_envelope_liusb.py
"""The one file in the link and transport suites allowed to contain the literal
LI-USB prefix bytes.

The link, station and CLI suites hold bare telegrams and take the envelope as a
fixture parameter, so adding Z21Envelope later re-runs them against new framing
with zero test edits. tests/unit/test_envelope_isolation.py carries the full
allow-list and fails if any file outside it starts spelling these bytes out.
"""

from __future__ import annotations

import itertools
import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st

from railctl.envelope import EnvelopeStats, Frame, Kind, hex_bytes
from railctl.envelope.liusb import MAX_BUFFER, LiUsbEnvelope

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"      # measured: XpressNet 4.0, station id 0x12
ACK = b"\x01\x04\x05"                        # measured: the LI interface ack
BROADCAST = b"\x61\x01\x60"                  # track power on, unsolicited
DRIVE_126 = b"\xe4\x13\x00\x03\xff\x0b"      # loco 3, step 126 forward: payload holds 0xFF
TELEMETRY = b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C\r\n"


@pytest.fixture
def env() -> LiUsbEnvelope:
    return LiUsbEnvelope()


def test_wrap_prepends_the_solicited_prefix_and_changes_nothing_else(env):
    assert env.wrap(VERSION_REQUEST) == b"\xff\xfe\x21\x21\x00"


def test_frame_renders_both_kinds(env):
    assert env.frame(Kind.SOLICITED, ACK) == b"\xff\xfe" + ACK
    assert env.frame(Kind.UNSOLICITED, BROADCAST) == b"\xff\xfd" + BROADCAST


def test_a_solicited_frame_comes_back_whole(env):
    env.note_request(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(kind=Kind.SOLICITED, payload=VERSION_REPLY)
    assert env.pop() is None
    assert env.stats.frames_ok == 1
    assert env.stats.bytes_dropped == 0
    assert env.stats.stray_replies == 0


def test_ff_fd_is_classified_as_unsolicited(env):
    env.feed(b"\xff\xfd" + BROADCAST)
    assert env.pop() == Frame(kind=Kind.UNSOLICITED, payload=BROADCAST)


def test_a_payload_containing_ff_is_not_mistaken_for_a_prefix(env):
    env.note_request(DRIVE_126)
    env.feed(b"\xff\xfe" + DRIVE_126)
    assert env.pop() == Frame(kind=Kind.SOLICITED, payload=DRIVE_126)
    assert env.stats.bytes_dropped == 0


def test_two_frames_in_one_chunk_come_back_in_arrival_order(env):
    env.feed(b"\xff\xfe" + ACK + b"\xff\xfd" + BROADCAST)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.pop() == Frame(Kind.UNSOLICITED, BROADCAST)
    assert env.pop() is None


def test_byte_at_a_time_feeding_yields_the_same_frame(env):
    stream = b"\xff\xfe" + DRIVE_126
    got = []
    for index in range(len(stream)):
        env.feed(stream[index : index + 1])
        frame = env.pop()
        if frame is not None:
            got.append(frame)
    assert got == [Frame(Kind.SOLICITED, DRIVE_126)]


def test_leading_noise_is_dropped_counted_and_the_frame_still_arrives(env):
    env.feed(b"hello" + b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.bytes_dropped == 5
    assert env.stats.resyncs == 1


def test_a_buffer_with_no_ff_at_all_is_discarded_whole(env):
    env.feed(TELEMETRY)
    assert env.pop() is None
    assert env.stats.frames_ok == 0
    assert env.stats.bytes_dropped == len(TELEMETRY)


def test_the_telemetry_port_produces_only_dropped_bytes(env):
    """The software half of the M4 hardware acceptance check."""
    for _ in range(4):
        env.feed(TELEMETRY)
        assert env.pop() is None
    assert env.stats.frames_ok == 0
    assert env.stats.bytes_dropped == 4 * len(TELEMETRY)


def test_a_stray_prefix_in_front_of_a_real_frame_does_not_swallow_it(env):
    """The exact regression tools/probe/frames.py was rewritten to fix.

    The stray prefix's header byte is 0xFF, so the length it implies is 17 and
    the candidate looks incomplete for ever. Trusting that reading loses the
    real frame behind it - a reply recorded as silence, which in this project is
    the difference between "unsupported" and "not established".
    """
    env.feed(b"\xff\xfe" + b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)
    assert env.stats.bytes_dropped == 2
    assert env.stats.resyncs == 1


def test_a_stray_prefix_further_into_the_buffer_still_resyncs(env):
    """Mutation pinning. docs/test-hardening.md records the most serious survivor
    in frames.py as the salvage scan starting at pos << 1 instead of pos + 1:
    with the noise at the FRONT the doubled offset still lands near the frame,
    and every test passed. With the noise further in it jumps past the frame and
    nothing comes back at all.
    """
    env.feed(b"\xff\xfe" + ACK + b"\xff\xfe" + b"\xff\xfe" + VERSION_REPLY)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)
    assert env.pop() is None


def test_an_incomplete_frame_with_nothing_behind_it_is_waited_for(env):
    env.feed(b"\xff\xfe\x63\x21\x40")
    assert env.pop() is None
    assert env.stats.bytes_dropped == 0
    env.feed(b"\x12\x10")
    assert env.pop() == Frame(Kind.SOLICITED, VERSION_REPLY)


def test_a_bad_checksum_costs_one_byte_and_the_next_good_frame_arrives(env):
    env.feed(b"\xff\xfe\x21\x81\x00" + b"\xff\xfe\x21\x81\xa0")
    assert env.pop() == Frame(Kind.SOLICITED, b"\x21\x81\xa0")
    assert env.stats.bad_xor == 1
    assert env.stats.frames_ok == 1
    assert env.stats.bytes_dropped == 5   # one byte for the bad frame, four resyncing
    assert env.stats.resyncs == 2


def test_a_second_ff_after_a_prefix_costs_exactly_one_byte(env):
    env.feed(b"\xff\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.bytes_dropped == 1


def test_the_buffer_is_bounded_and_the_discard_is_counted(env):
    env.feed(b"\x00" * (MAX_BUFFER + 500))
    assert env.stats.bytes_dropped == 500
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)


def test_a_solicited_frame_with_no_request_outstanding_is_a_stray(env):
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)
    assert env.stats.stray_replies == 1


def test_note_reply_closes_the_lifecycle_so_the_next_reply_is_the_stray(env):
    """Named test. Forgetting note_reply on the success path silently breaks
    stray_replies and the future Z21 classification, and nothing else would
    notice.
    """
    env.note_request(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    first = env.pop()
    assert env.stats.stray_replies == 0
    env.note_reply(first)
    env.feed(b"\xff\xfe" + ACK)
    env.pop()
    assert env.stats.stray_replies == 1


def test_note_abandoned_also_closes_the_lifecycle(env):
    env.note_request(VERSION_REQUEST)
    env.note_abandoned()
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    env.pop()
    assert env.stats.stray_replies == 1


def test_an_unsolicited_frame_is_never_a_stray(env):
    env.feed(b"\xff\xfd" + BROADCAST)
    env.pop()
    assert env.stats.stray_replies == 0


def test_reset_clears_the_buffer_and_keeps_the_counters(env):
    env.feed(b"garbage")
    env.pop()
    env.reset()
    assert env.stats.bytes_dropped == 7
    env.feed(b"\xff\xfe" + ACK)
    assert env.pop() == Frame(Kind.SOLICITED, ACK)


def test_stats_is_a_snapshot_the_caller_cannot_edit(env):
    before = env.stats
    env.feed(b"junk")
    env.pop()
    assert before.bytes_dropped == 0
    assert env.stats.bytes_dropped == 4
    before.bytes_dropped = 999
    assert env.stats.bytes_dropped == 4
    assert isinstance(before, EnvelopeStats)


def test_expects_ack_is_true_for_li_usb(env):
    assert env.expects_ack is True


def test_the_wire_log_shows_bytes_as_they_appear_on_the_wire(env, caplog):
    caplog.set_level(logging.DEBUG, logger="railctl.wire")
    env.note_request(VERSION_REQUEST)
    env.wrap(VERSION_REQUEST)
    env.feed(b"\xff\xfe" + VERSION_REPLY)
    env.pop()
    env.feed(b"\xff\xfd" + BROADCAST)
    env.pop()
    env.feed(b"\x12\x34")
    env.pop()
    assert [record.getMessage() for record in caplog.records] == [
        "TX FF FE 21 21 00",
        "RX FF FE 63 21 40 12 10",
        "RX! FF FD 61 01 60",
        "RX? 12 34",
    ]


def test_hex_bytes_is_the_one_wire_rendering():
    assert hex_bytes(b"\x01\x04\x05") == "01 04 05"
    assert hex_bytes(b"") == ""


STREAM = b"\xff\xfe" + VERSION_REPLY + b"\xff\xfd" + BROADCAST + b"\xff\xfe" + ACK
EXPECTED = [
    Frame(Kind.SOLICITED, VERSION_REPLY),
    Frame(Kind.UNSOLICITED, BROADCAST),
    Frame(Kind.SOLICITED, ACK),
]


@given(sizes=st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=20))
def test_arbitrary_chunking_yields_identical_frames(sizes):
    """USB CDC splits wherever it likes. Where the split falls must not change
    which frames come out, or a reply becomes silence for timing reasons alone.
    """
    env = LiUsbEnvelope()
    got: list[Frame] = []
    index = 0
    for size in itertools.cycle(sizes):
        if index >= len(STREAM):
            break
        env.feed(STREAM[index : index + size])
        index += size
        while (frame := env.pop()) is not None:
            got.append(frame)
    assert got == EXPECTED
```

```python
# tests/unit/test_envelope_isolation.py
"""The framing bytes may appear only in the files listed below, and this test is why.

If a link, station or CLI file ever spells the prefix out, the envelope has
leaked upward and adding Z21Envelope stops being a zero-edit change. The needles
are assembled at run time so this file is not its own counter-example.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/railctl", "tests/unit", "tests/station", "tests/cli", "tests/hardware")
# Seven files, each for a stated reason. This is an allow-list, not a waiver
# list: a file goes in only when naming the prefix is the point of the file.
#
#   envelope/liusb.py            owns the prefix; it is the implementation.
#   test_envelope_liusb.py       the envelope's own tests; the ONE file in the
#                                link and transport suites allowed to spell it.
#   xbus/codec.py                its docstring states that the codec never
#                                prepends the prefix and never checksums it -
#                                the layering rule this whole part rests on.
#   xbus/commands.py             same statement, one module up.
#   test_codec.py (Task 4)       asserts encode() does NOT start with either
#                                prefix and that the XOR changes if the prefix
#                                is included. Deleting that assertion to satisfy
#                                this guard would remove the check that keeps
#                                the framing out of the checksum.
#   test_xbus_commands.py (Task 7) explains, in the step-126 test, why a payload
#                                byte of FF means the envelope must anchor on
#                                the prefix rather than search for a delimiter.
#   test_xbus_replies.py (Task 8) feeds prefixed bytes to parse() to prove it
#                                never raises on framing that reached it by
#                                mistake.
#
# Everything else - link.py, transport/, the station and CLI suites - must hold
# bare telegrams and render them through the envelope under test.
ALLOWED = {
    "src/railctl/envelope/liusb.py",
    "src/railctl/xbus/codec.py",
    "src/railctl/xbus/commands.py",
    "tests/unit/test_codec.py",
    "tests/unit/test_envelope_liusb.py",
    "tests/unit/test_xbus_commands.py",
    "tests/unit/test_xbus_replies.py",
}
# tools/, tests/probe/ and tests/test_layering.py are out of scope by SCANNED:
# the M1 probe is a separate throwaway tool that keeps its own copy of the
# framing, and the layering guard has to name the same bytes to grep for them.

_PAIRS = (("ff", "fe"), ("ff", "fd"))
_NEEDLES = tuple(f"\\x{a}\\x{b}" for a, b in _PAIRS) + tuple(f"{a} {b}" for a, b in _PAIRS)


def _offenders() -> set[str]:
    found: set[str] = set()
    for area in SCANNED:
        base = ROOT / area
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8").lower()
            if any(needle in text for needle in _NEEDLES):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_the_framing_bytes_appear_only_where_they_are_allowed():
    assert _offenders() == ALLOWED
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_envelope_liusb.py -q`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.envelope'`

If it says `No module named 'railctl'` instead, the M2 scaffolding task has not been run;
stop and run it first, because nothing here is importable without it.

- [ ] **Step 4: Write the envelope package interface**

```python
# src/railctl/envelope/__init__.py
"""Frame classification, checksum validation and the wire log.

The envelope owns the bytes that surround an X-Bus telegram, in both directions,
and it is the only layer that logs them. Link never logs wire bytes: with two
loggers the same frame appears twice or, worse, once with the framing and once
without, and the wire log is the primary instrument for every hardware probe.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class Kind(enum.Enum):
    SOLICITED = "solicited"      # a reply to the command we sent
    UNSOLICITED = "unsolicited"  # broadcast or spontaneous


@dataclass(frozen=True, slots=True)
class Frame:
    """A complete X-Bus telegram, header..XOR, framing stripped, XOR verified.

    Frozen because a frame is evidence: it is what a capability verdict rests on
    and what the wire log prints. A mutable one lets a later stage rewrite what
    an earlier stage saw.
    """

    kind: Kind
    payload: bytes


@dataclass
class EnvelopeStats:
    """Counters that make silence diagnosable.

    frames_ok stuck at 0 while bytes_dropped climbs is what distinguishes "the
    wrong CDC interface" from "a dead port", with no extra flag anywhere.
    """

    frames_ok: int = 0
    bytes_dropped: int = 0
    bad_xor: int = 0
    stray_replies: int = 0
    resyncs: int = 0


def hex_bytes(data: bytes) -> str:
    """Wire bytes as the log and every error message render them: 21 21 00."""
    return " ".join(f"{byte:02X}" for byte in data)


class Envelope(Protocol):
    def wrap(self, telegram: bytes) -> bytes: ...
    def frame(self, kind: Kind, telegram: bytes) -> bytes: ...
    def feed(self, data: bytes) -> None: ...
    def pop(self) -> Frame | None: ...
    def note_request(self, telegram: bytes) -> None: ...
    def note_reply(self, frame: Frame) -> None: ...
    def note_abandoned(self) -> None: ...
    def reset(self) -> None: ...
    @property
    def expects_ack(self) -> bool: ...
    @property
    def stats(self) -> EnvelopeStats: ...
```

`frame()` is not decoration. It is how a scripted test builds inbound bytes without knowing
the framing: the script holds a bare telegram and asks the envelope under test to render it.
`wrap()` is `frame(Kind.SOLICITED, ...)` plus the TX log.

- [ ] **Step 5: Implement `LiUsbEnvelope`**

```python
# src/railctl/envelope/liusb.py
"""LI-USB framing for the YD7010 XpressNet port (Lenz 23151 section 1.3).

Every command carries the two-byte solicited prefix; without it the port stays
silent. Broadcasts carry the unsolicited prefix. Neither prefix is part of the
XOR, so the codec never sees them.

Why the header nibble and not a delimiter search: the prefix bytes occur inside
legitimate payloads - ff fe e4 13 00 03 ff 0b is loco 3 at step 126 forward. The
correct order is anchor once on the prefix, trust the low nibble for the length,
let the XOR confirm.

The XOR here is deliberately not imported from railctl.xbus.codec. xbus sits
above link and this module sits below it; importing upward would invert the
layering and drag xbus behind the future Z21Envelope. tests/unit/test_link.py
pins the two implementations against each other.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from railctl.envelope import EnvelopeStats, Frame, Kind, hex_bytes

PREFIX_SOLICITED = b"\xff\xfe"
PREFIX_UNSOLICITED = b"\xff\xfd"
MAX_BUFFER = 4096
_MARKER = 0xFF
_PREFIXES = {Kind.SOLICITED: PREFIX_SOLICITED, Kind.UNSOLICITED: PREFIX_UNSOLICITED}
_KIND_BY_SECOND_BYTE = {0xFE: Kind.SOLICITED, 0xFD: Kind.UNSOLICITED}
_ALL_PREFIXES = (PREFIX_SOLICITED, PREFIX_UNSOLICITED)
_MIN_TELEGRAM = 2

_wire = logging.getLogger("railctl.wire")


def _xor(telegram: bytes) -> int:
    result = 0
    for byte in telegram:
        result ^= byte
    return result


class LiUsbEnvelope:
    expects_ack = True  # 23151 section 1.3: every command is acknowledged

    def __init__(self) -> None:
        self._buf = bytearray()
        self._counters = EnvelopeStats()
        self._outstanding: bytes | None = None

    @property
    def stats(self) -> EnvelopeStats:
        # A copy: a snapshot taken before an operation must stay valid, and no
        # caller gets to edit the counters a hardware verdict is read from.
        return replace(self._counters)

    def frame(self, kind: Kind, telegram: bytes) -> bytes:
        """The exact bytes this envelope puts on the wire for a frame of `kind`."""
        return _PREFIXES[kind] + telegram

    def wrap(self, telegram: bytes) -> bytes:
        framed = self.frame(Kind.SOLICITED, telegram)
        if _wire.isEnabledFor(logging.DEBUG):
            _wire.debug("TX %s", hex_bytes(framed))
        return framed

    def note_request(self, telegram: bytes) -> None:
        self._outstanding = bytes(telegram)

    def note_reply(self, frame: Frame) -> None:
        self._outstanding = None

    def note_abandoned(self) -> None:
        self._outstanding = None

    def reset(self) -> None:
        # The buffer is per-connection, the counters are per-session: the M4
        # acceptance check reads bytes_dropped after a failed open.
        self._buf.clear()
        self._outstanding = None

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf += data
        excess = len(self._buf) - MAX_BUFFER
        if excess > 0:
            self._discard(excess)

    def pop(self) -> Frame | None:
        while True:
            start = self._buf.find(_MARKER)
            if start == -1:
                if self._buf:
                    self._discard(len(self._buf))
                return None
            if start:
                self._discard(start)
            if len(self._buf) < 2:
                return None
            kind = _KIND_BY_SECOND_BYTE.get(self._buf[1])
            if kind is None:
                self._discard(1)
                continue
            if len(self._buf) < 3:
                return None
            total = (self._buf[2] & 0x0F) + _MIN_TELEGRAM
            if len(self._buf) < 2 + total:
                rescue = self._salvage_start()
                if rescue is None:
                    return None
                self._discard(rescue)
                continue
            telegram = bytes(self._buf[2 : 2 + total])
            if _xor(telegram) != 0:
                self._counters.bad_xor += 1
                # One byte, never the whole candidate: the true start may lie
                # inside it.
                self._discard(1)
                continue
            del self._buf[: 2 + total]
            self._counters.frames_ok += 1
            if kind is Kind.SOLICITED and self._outstanding is None:
                self._counters.stray_replies += 1
            if _wire.isEnabledFor(logging.DEBUG):
                mark = "RX" if kind is Kind.SOLICITED else "RX!"
                _wire.debug("%s %s", mark, hex_bytes(_PREFIXES[kind] + telegram))
            return Frame(kind=kind, payload=telegram)

    def _discard(self, count: int) -> None:
        if _wire.isEnabledFor(logging.DEBUG):
            _wire.debug("RX? %s", hex_bytes(bytes(self._buf[:count])))
        self._counters.bytes_dropped += count
        self._counters.resyncs += 1
        del self._buf[:count]

    def _salvage_start(self) -> int | None:
        """Offset of the first complete, checksum-valid frame after position 0.

        An incomplete candidate is not trusted on its own. A stray prefix in
        front of a real frame reads its header from the stray's offset, and the
        length that implies overruns the buffer for ever - so the frame behind it
        was lost, and a lost reply is recorded one layer up as "the hardware
        cannot do this". If a checksum-valid frame exists further along, that is
        strong evidence the candidate was noise. Only when none exists do we
        wait, because then the candidate may genuinely still be arriving.
        """
        for pos in range(1, len(self._buf) - 1):
            if self._complete_at(pos):
                return pos
        return None

    def _complete_at(self, pos: int) -> bool:
        if bytes(self._buf[pos : pos + 2]) not in _ALL_PREFIXES:
            return False
        if pos + 3 > len(self._buf):
            return False
        end = pos + 2 + (self._buf[pos + 2] & 0x0F) + _MIN_TELEGRAM
        if end > len(self._buf):
            return False
        return _xor(bytes(self._buf[pos + 2 : end])) == 0
```

This deviates from the spec on one point and the deviation is deliberate. Spec step 5 says an
incomplete candidate returns `None` and waits. Applied to a stray prefix followed by a real
frame, that waits for bytes that never come and loses the frame - the exact bug
`tools/probe/frames.py` was rewritten to fix, written up in its docstring and pinned in
`tests/test_frames_mutation_hardening.py`. The measured behaviour wins: salvage first, wait
only when there is nothing to salvage.

- [ ] **Step 6: Add the envelope fixture to the shared conftest**

Append to `tests/conftest.py` at the end of the file, below the `settings.load_profile(...)` call. **Do not go by line number.** The file is 46 lines today, but Task 5 of the M3a part replaces the one-line `ci` profile registration with a seven-line call, so by the time this step runs `load_profile` has moved to line 52 and line 46 is in the middle of an open `settings.register_profile(` call. Inserting there is a syntax error that takes every one of the 292 probe tests down with it.

```python

# --- railctl fixtures -------------------------------------------------------
# The station and CLI suites take the envelope as a parameter and hold bare
# telegrams, so adding Z21Envelope re-runs every one of them against new framing
# with zero test edits. The list has one element today; retrofitting the
# parametrisation later is what fails.
import pytest  # noqa: E402

from railctl.envelope.liusb import LiUsbEnvelope  # noqa: E402

ENVELOPES = [LiUsbEnvelope]


@pytest.fixture(params=ENVELOPES, ids=lambda cls: cls.__name__)
def envelope_factory(request):
    """The envelope CLASS under test. Call it to get a fresh instance."""
    return request.param
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_envelope_liusb.py tests/unit/test_envelope_isolation.py -q`
Expected: PASS, 27 passed

- [ ] **Step 8: Run the whole suite so the probe tests are not disturbed**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, the 292 M1 tests plus the new ones, 0 failed

- [ ] **Step 9: Check formatting and lint**

Run: `.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 10: Check the coverage gate before committing**

Task 9 of the M3 part added a `Coverage` step to `.github/workflows/ci.yml` running
`python -m pytest --cov --cov-report=term-missing` over `source = ["railctl"]`, `branch = true`,
with `src/railctl/transport/serial_posix.py` omitted and `fail_under = 90`. That gate has been
live on every push and every pull request since the end of M3, so the code this task adds is
measured by it whether or not anybody looks. Run the same pytest invocation locally, through the
venv interpreter - CI has no `.venv/`, so the interpreter path differs and nothing else does,
which is what keeps the local check and CI from diverging on a flag - before the commit leaves
the machine:

```bash
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

Expected: the coverage table, now with `src/railctl/envelope/__init__.py` and
`src/railctl/envelope/liusb.py` in it, then `Required test coverage of 90% reached.` and
`0 failed`. No percentage is written here on purpose: the first run of this step is what records
the real figure. What is fixed is the gate - **at least 90**.

If the total comes in under 90, this task owns the fix. Read the uncovered lines out of the
`term-missing` column, and add the tests to `tests/unit/test_envelope_liusb.py`. Lowering
`fail_under` is not an option, and neither is leaving it: Task 11 would then start from a red
build caused by this task.

- [ ] **Step 11: Commit**

`pyproject.toml` is deliberately not in the `git add`: Step 1 checked it and changed nothing.

```bash
git add src/railctl/envelope \
        tests/unit/test_envelope_liusb.py tests/unit/test_envelope_isolation.py \
        tests/conftest.py
git commit -m "feat(envelope): add LI-USB framing with resync, stats and the wire log"
```

---

### Task 11: The `Transport` protocol and `FakeTransport`

**Files:**
- Create: `src/railctl/transport/__init__.py` (the `Transport` protocol only; Task 13 appends discovery and `open_link`)
- Create: `src/railctl/transport/fake.py`
- Create: `tests/unit/test_fake_transport.py`
- Modify: `tests/conftest.py` - append the `chunk_size` fixture at the end of the file, below the `envelope_factory` fixture

**Interfaces:**
- Consumes:
  - `railctl.errors.PortNotOpen` (from the M2 errors task) - `RailctlError` subclass; `RailctlError(message: str, *, hint: str | None = None)`, where `str(exc)` is the message **only** and `exc.hint` is the hint or `None`
  - `railctl.envelope.hex_bytes(data: bytes) -> str` - uppercase, space separated, e.g. `hex_bytes(b"\x01\x04\x05") == "01 04 05"`
  - `railctl.envelope.Kind` - `enum.Enum` with members `SOLICITED` and `UNSOLICITED`; `railctl.envelope.liusb.LiUsbEnvelope()` - no-argument constructor, method `frame(kind: Kind, telegram: bytes) -> bytes` returning the exact bytes that envelope puts on the wire (tests only, to render framed script bytes without spelling the framing out)
  - `pyproject.toml` already sets `[tool.ruff.lint.isort] known-first-party = ["railctl"]` (M2 part, Task 1, Step 6 - not Task 10, which only checks it). Verified necessary, not optional: measured against ruff 0.16.1, `src = ["src", "."]` alone leaves `railctl` in the third-party block and this task's new test file fails `I001`.
- Produces:
  - `railctl.transport.Transport` - `Protocol` with `open() -> None`, `close() -> None`, `write(data: bytes) -> None`, `read(max_bytes: int, timeout: float) -> bytes`, `flush_input() -> None`, and the properties `is_open: bool`, `description: str`, `identity: str`, `diagnostic_hint: str`
  - `railctl.transport.fake.FakeClock` - `__init__(start: float = 0.0)`, `monotonic() -> float`, `sleep(seconds: float) -> None`, `advance(seconds: float) -> None`
  - `railctl.transport.fake.Exchange` - `@dataclass(frozen=True, slots=True)` with `request: bytes`, `reply: bytes = b""`
  - `railctl.transport.fake.FakeTransport` - `__init__(*, clock: FakeClock | None = None, chunk_size: int | None = None, max_write: int | None = None, on_write: Callable[[bytes, FakeTransport], None] | None = None, description: str = "fake xpressnet", identity: str = "fake", diagnostic_hint: str = "check the station is reachable on the network")`; methods `expect(request: bytes, *, reply: bytes = b"") -> FakeTransport`, `queue(data: bytes) -> None`, plus the whole `Transport` protocol; attributes `clock`, `written: list[bytes]` (whole requests, one entry per completed exchange), `write_chunks: list[bytes]` (the pieces each `write()` call was split into), `flushes: int`, `script_pending: list[Exchange]`
  - `tests/conftest.py` fixture `chunk_size` - parametrised over `[None, 1]` with ids `whole-frame` / `byte-at-a-time`

**Layering note.** `fake.py` imports `hex_bytes` from `railctl.envelope` so every layer renders
the wire the same way. `envelope` never imports `transport`, so this edge cannot cycle. The fake
still knows nothing about framing: every byte string it handles is exactly what would appear on
the wire, and callers render bare telegrams through the envelope under test.

- [ ] **Step 1: Write the failing fake-transport tests**

```python
# tests/unit/test_fake_transport.py
from __future__ import annotations

import pytest

from railctl.envelope import Kind, hex_bytes
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import PortNotOpen
from railctl.transport.fake import FakeClock, FakeTransport

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"
POLL = b"\x21\x10\x31"                    # 21 10: request for service mode results
CV8_RESULT = b"\x63\x14\x08\x91\xee"      # measured: CV8 = 145 on the ZIMO MS450P22
ACK = b"\x01\x04\x05"


@pytest.fixture
def env() -> LiUsbEnvelope:
    return LiUsbEnvelope()


def _open(**kwargs) -> FakeTransport:
    transport = FakeTransport(**kwargs)
    transport.open()
    return transport


def test_the_same_request_is_answered_first_with_silence_and_then_with_the_value(env):
    """The single reason this test double exists.

    The M1 probe's FakeLink answered a payload with the same bytes every time,
    so a station that returns nothing to a read and produces the value only when
    asked AGAIN could not be expressed. That is the station docs/probe-results.md
    measured: 22 15, 22 18 and 22 19 deliver their result only after 21 10 is
    sent. Every mutant inside the poll loop survived the M1 suite, including the
    one that deletes the loop outright - and a missing poll makes the whole Lenz
    opcode family read as silent, which is two capabilities wrongly recorded as
    absent.

    If this test can be deleted without another one failing, the fake has
    regressed to the M1 shape.
    """
    solicited = lambda telegram: env.frame(Kind.SOLICITED, telegram)  # noqa: E731
    transport = _open()
    transport.expect(solicited(POLL), reply=b"")
    transport.expect(solicited(POLL), reply=solicited(CV8_RESULT))

    transport.write(solicited(POLL))
    assert transport.read(256, 1.0) == b""

    transport.write(solicited(POLL))
    assert transport.read(256, 1.0) == solicited(CV8_RESULT)


def test_a_second_command_while_a_reply_is_outstanding_raises(env):
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST),
                     reply=env.frame(Kind.SOLICITED, VERSION_REPLY))
    transport.expect(env.frame(Kind.SOLICITED, POLL))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    with pytest.raises(AssertionError, match="exactly one command in flight"):
        transport.write(env.frame(Kind.SOLICITED, POLL))


def test_a_second_command_after_the_reply_was_read_is_fine(env):
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST),
                     reply=env.frame(Kind.SOLICITED, VERSION_REPLY))
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=env.frame(Kind.SOLICITED, ACK))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, VERSION_REPLY)
    transport.write(env.frame(Kind.SOLICITED, POLL))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, ACK)


def test_a_silent_exchange_releases_the_one_command_rule(env):
    """After the station says nothing there is nothing to wait for, so the next
    command - a retry, or the 21 10 poll - must be allowed through.
    """
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=b"")
    transport.expect(env.frame(Kind.SOLICITED, POLL), reply=b"")
    transport.write(env.frame(Kind.SOLICITED, POLL))
    assert transport.read(256, 0.5) == b""
    transport.write(env.frame(Kind.SOLICITED, POLL))


def test_the_exact_request_telegram_is_asserted(env):
    """The expected bytes are rendered through hex_bytes, never typed out.

    Typing them would put the framing prefix into a second test file and
    tests/unit/test_envelope_isolation.py would fail: that guard lower-cases every
    scanned file and looks for the prefix as text as well as as an escape.
    """
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    with pytest.raises(AssertionError, match="unexpected request") as caught:
        transport.write(env.frame(Kind.SOLICITED, b"\x21\x24\x05"))
    assert hex_bytes(env.frame(Kind.SOLICITED, VERSION_REQUEST)) in str(caught.value)


def test_a_write_with_an_exhausted_script_raises(env):
    transport = _open()
    with pytest.raises(AssertionError, match="script is exhausted"):
        transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))


def test_a_read_that_finds_nothing_advances_the_fake_clock_by_its_timeout():
    """Without this a Link waiting on monotonic() spins for ever against frozen
    time and the timeout path cannot be tested at all.
    """
    clock = FakeClock()
    transport = _open(clock=clock)
    transport.queue(b"")
    assert transport.read(256, 2.5) == b""
    assert clock.monotonic() == pytest.approx(2.5)


def test_a_read_that_finds_bytes_does_not_advance_the_clock(env):
    clock = FakeClock()
    transport = _open(clock=clock)
    transport.queue(env.frame(Kind.UNSOLICITED, b"\x61\x01\x60"))
    assert transport.read(256, 2.5) != b""
    assert clock.monotonic() == 0.0


def test_chunk_size_one_replays_worst_case_usb_fragmentation(env):
    transport = _open(chunk_size=1)
    framed = env.frame(Kind.SOLICITED, VERSION_REPLY)
    transport.queue(framed)
    got = b""
    for _ in range(len(framed)):
        got += transport.read(256, 0.1)
    assert got == framed


def test_max_write_splits_the_write_but_delivers_everything(env):
    transport = _open(max_write=3)
    framed = env.frame(Kind.SOLICITED, VERSION_REQUEST)
    transport.expect(framed, reply=env.frame(Kind.SOLICITED, ACK))
    transport.write(framed)
    assert transport.written == [framed]
    assert transport.write_chunks == [framed[0:3], framed[3:5]]


def test_on_write_lets_a_test_script_a_station_with_no_queue(env):
    def station(request: bytes, transport: FakeTransport) -> None:
        if request.endswith(VERSION_REQUEST):
            transport.queue(env.frame(Kind.SOLICITED, VERSION_REPLY))

    transport = _open(on_write=station)
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.read(256, 1.0) == env.frame(Kind.SOLICITED, VERSION_REPLY)


def test_flush_input_drops_queued_bytes_and_is_counted(env):
    transport = _open()
    transport.queue(env.frame(Kind.SOLICITED, ACK))
    transport.flush_input()
    assert transport.read(256, 0.1) == b""
    assert transport.flushes == 1


def test_reading_or_writing_a_closed_transport_raises_port_not_open(env):
    transport = FakeTransport()
    with pytest.raises(PortNotOpen):
        transport.read(256, 0.1)
    with pytest.raises(PortNotOpen):
        transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert transport.is_open is False


def test_description_identity_and_diagnostic_hint_are_reported():
    """diagnostic_hint keeps connection-specific advice behind the transport.

    Spec line 583 requires the Z21 LAN transport to land with no edit to link.py,
    so link.py may not hold a sentence about CDC interface indices. Each transport
    supplies its own.
    """
    transport = FakeTransport(description="fake xpressnet", identity="fake")
    assert transport.description == "fake xpressnet"
    assert transport.identity == "fake"
    assert transport.diagnostic_hint == "check the station is reachable on the network"


def test_script_pending_shows_what_was_never_sent(env):
    transport = _open()
    transport.expect(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    transport.expect(env.frame(Kind.SOLICITED, POLL))
    transport.write(env.frame(Kind.SOLICITED, VERSION_REQUEST))
    assert [exchange.request for exchange in transport.script_pending] == [
        env.frame(Kind.SOLICITED, POLL)
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_fake_transport.py -q`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.transport'`

- [ ] **Step 3: Write the `Transport` protocol**

```python
# src/railctl/transport/__init__.py
"""Byte pipes. A transport moves bytes and knows nothing about framing.

read() blocks up to `timeout`, returns as soon as at least one byte is
available, returns b"" on timeout, and never raises on timeout - framing is not
its problem. write() writes everything or raises TransportError.

diagnostic_hint is here rather than in link.py on purpose. When a handshake or an
exchange fails, the useful advice is about the CONNECTION, and only the transport
knows what kind of connection it is - a CDC interface index for the serial port, a
network address for the future Z21 UDP transport. Spec line 583 requires the LAN
transport to land with no edit to link.py, so link.py must not hold either
sentence.
"""

from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, max_bytes: int, timeout: float) -> bytes: ...
    def flush_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def description(self) -> str: ...
    @property
    def identity(self) -> str: ...
    @property
    def diagnostic_hint(self) -> str: ...
```

- [ ] **Step 4: Implement `FakeClock` and `FakeTransport`**

```python
# src/railctl/transport/fake.py
"""A Transport that replays a scripted station: no threads, no sleeps, no real clock.

Shipped rather than test-only, so a hardware probe can dry-run against it.

Four properties earn this file its place, and each exists because the M1 probe's
FakeLink lacked it:

1. The script is an ORDERED queue, so the same request may be answered
   differently each time it is sent. The probe's fake answered a payload with the
   same bytes for ever, which made a station that returns nothing until asked
   again with 21 10 inexpressible - so every mutant inside the service-result
   poll loop survived, including the one that deletes the loop. That deletion is
   the largest error of M1: no poll means the whole Lenz opcode family reads as
   silent, and two further capabilities get recorded as absent.
2. Writing a second command while the reply to the first is still queued raises.
   The LI-USB one-command rule becomes a mechanical check, not a review note.
3. A read that finds nothing advances the fake clock by its own timeout.
   Without it a Link waiting on monotonic() spins for ever against frozen time
   and the timeout path is untestable.
4. chunk_size=1 replays worst-case USB CDC fragmentation, so the whole suite can
   run twice: whole-frame and byte-at-a-time.

It knows nothing about framing. Every byte string here is exactly what would
appear on the wire; callers render bare telegrams through the envelope under
test, which is what keeps framing bytes out of the station and CLI suites.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from railctl.envelope import hex_bytes
from railctl.errors import PortNotOpen


class FakeClock:
    """monotonic()/sleep() with no real time. Duck-typed against railctl.link.Clock."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


@dataclass(frozen=True, slots=True)
class Exchange:
    request: bytes
    reply: bytes = b""


class FakeTransport:
    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        chunk_size: int | None = None,
        max_write: int | None = None,
        on_write: Callable[[bytes, FakeTransport], None] | None = None,
        description: str = "fake xpressnet",
        identity: str = "fake",
        diagnostic_hint: str = "check the station is reachable on the network",
    ) -> None:
        self.clock = FakeClock() if clock is None else clock
        self.chunk_size = chunk_size
        self.max_write = max_write
        self.on_write = on_write
        self.written: list[bytes] = []
        self.write_chunks: list[bytes] = []
        self.flushes = 0
        self._description = description
        self._identity = identity
        self._diagnostic_hint = diagnostic_hint
        self._script: deque[Exchange] = deque()
        self._rx = bytearray()
        self._partial = bytearray()
        self._open = False
        self._in_flight = False

    # -- scripting ---------------------------------------------------------
    def expect(self, request: bytes, *, reply: bytes = b"") -> FakeTransport:
        """Queue one exchange. reply=b"" scripts a station that says nothing."""
        self._script.append(Exchange(bytes(request), bytes(reply)))
        return self

    def queue(self, data: bytes) -> None:
        """Push bytes with no request behind them - a broadcast."""
        self._rx += data

    @property
    def script_pending(self) -> list[Exchange]:
        return list(self._script)

    # -- Transport ---------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return self._description

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def diagnostic_hint(self) -> str:
        # Link quotes this verbatim when a handshake or an exchange fails, so no
        # connection-specific advice has to live in link.py.
        return self._diagnostic_hint

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def flush_input(self) -> None:
        self._require_open()
        self._rx.clear()
        self.flushes += 1

    def write(self, data: bytes) -> None:
        self._require_open()
        limit = len(data) if self.max_write is None else max(1, self.max_write)
        offset = 0
        while offset < len(data):
            piece = data[offset : offset + limit]
            offset += len(piece)
            self.write_chunks.append(piece)
            self._accept(piece)
        if self._partial and not self._script:
            self._finish_callback_request()

    def read(self, max_bytes: int, timeout: float) -> bytes:
        self._require_open()
        if not self._rx:
            # The station said nothing. Burn the budget the caller was prepared
            # to wait, exactly as a real select() would.
            self.clock.sleep(timeout)
            self._in_flight = False
            return b""
        size = min(max_bytes, len(self._rx))
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        out = bytes(self._rx[:size])
        del self._rx[:size]
        if not self._rx:
            self._in_flight = False
        return out

    # -- internals ---------------------------------------------------------
    def _require_open(self) -> None:
        if not self._open:
            raise PortNotOpen("fake transport is not open")

    def _accept(self, piece: bytes) -> None:
        if self._in_flight:
            raise AssertionError(
                f"second command written while the reply to "
                f"{hex_bytes(self.written[-1])} was still outstanding; "
                f"LI-USB allows exactly one command in flight"
            )
        self._partial += piece
        if not self._script:
            return  # scriptless mode: on_write sees the whole write() call
        expected = self._script[0].request
        if not expected.startswith(bytes(self._partial)):
            raise AssertionError(
                "unexpected request\n"
                f"  expected {hex_bytes(expected)}\n"
                f"  got      {hex_bytes(bytes(self._partial))}"
            )
        if len(self._partial) < len(expected):
            return
        exchange = self._script.popleft()
        self._complete(exchange.request, exchange.reply)

    def _finish_callback_request(self) -> None:
        if self.on_write is None:
            raise AssertionError(
                f"unexpected request {hex_bytes(bytes(self._partial))}; the script is exhausted"
            )
        request = bytes(self._partial)
        self._complete(request, b"")
        self.on_write(request, self)

    def _complete(self, request: bytes, reply: bytes) -> None:
        self.written.append(request)
        self._partial.clear()
        self._rx += reply
        self._in_flight = True
```

- [ ] **Step 5: Add the chunk-size fixture to the shared conftest**

Append at the end of `tests/conftest.py`, below `envelope_factory`:

```python


# Every scripted suite runs twice: whole-frame, then one byte at a time. The
# byte-at-a-time run is the worst case a USB CDC port actually produces.
@pytest.fixture(params=[None, 1], ids=["whole-frame", "byte-at-a-time"])
def chunk_size(request):
    return request.param
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_fake_transport.py -q`
Expected: PASS, 15 passed

- [ ] **Step 7: Check the framing guard still holds and lint**

Run: `.venv/bin/python -m pytest tests/unit/test_envelope_isolation.py -q && .venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .`
Expected: `1 passed`, then `All checks passed!`

`1 passed` here is the point of Step 1's `hex_bytes` rendering. If it reports `1 failed`
with `test_fake_transport.py` in the difference, the framing bytes have been typed into
this task's test file. Fix the test - do **not** add the file to `ALLOWED`, which would
switch the guard off for every future edit to it.

- [ ] **Step 8: Run the whole suite so the shared conftest change is not disturbing the probe tests**

`tests/conftest.py` is loaded by all 292 existing M1 tests, and Step 5 appended to it. A
syntax error, a fixture name collision or an import that fails on a half-installed
scaffolding breaks every one of them, and no gate so far has looked.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, the 292 M1 tests plus the new ones, 0 failed

- [ ] **Step 9: Check the coverage gate before committing**

The gate Task 9 of the M3 part wired into `.github/workflows/ci.yml` is the same one here:
`python -m pytest --cov --cov-report=term-missing` over `source = ["railctl"]`, `branch = true`,
`src/railctl/transport/serial_posix.py` omitted, `fail_under = 90`. `src/railctl/transport/`
arrives in this task, so this is the first run in which the transport package is measured at all.
Run the CI command locally before committing:

```bash
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

Expected: the coverage table, now with `src/railctl/transport/__init__.py` and
`src/railctl/transport/fake.py` in it, then `Required test coverage of 90% reached.` and
`0 failed`. The first execution of this step records the real figure; the number this plan fixes
is the gate, **at least 90**.

Note which file the omit covers and which it does not: only `transport/serial_posix.py` is
omitted, and Task 13 writes it. `transport/fake.py` is shipped code and is fully measured, so
every branch of the script queue, the one-command-in-flight check and the chunking paths need a
test in `tests/unit/test_fake_transport.py`.

If the total comes in under 90, this task owns the fix - the uncovered lines in the
`term-missing` column are this task's own new module. Lowering `fail_under` is not an option, and
handing a red build to Task 12 is not one either.

- [ ] **Step 10: Commit**

```bash
git add src/railctl/transport tests/unit/test_fake_transport.py tests/conftest.py
git commit -m "feat(transport): add the Transport protocol and a sequenced fake station"
```

---

### Task 12: `Link` - one command in flight, retries, timeouts, `LinkStats`

**Files:**
- Create: `src/railctl/link.py`
- Create: `tests/unit/test_link.py`

**Interfaces:**
- Consumes:
  - `railctl.errors.LinkTimeout`, `railctl.errors.LinkProtocolError`, `railctl.errors.PortNotXpressNet` (M2 errors task) - all subclass `RailctlError(message: str, *, hint: str | None = None)`, where `str(exc)` is the message **only** and `exc.hint` is the hint or `None`
  - `railctl.envelope.Envelope` (Task 10) - `Protocol` with `wrap(telegram: bytes) -> bytes`, `frame(kind: Kind, telegram: bytes) -> bytes`, `feed(data: bytes) -> None`, `pop() -> Frame | None`, `note_request(telegram: bytes) -> None`, `note_reply(frame: Frame) -> None`, `note_abandoned() -> None`, `reset() -> None`, and the **properties** (not methods) `expects_ack: bool` and `stats: EnvelopeStats`
  - `railctl.envelope.Frame` (Task 10) - `@dataclass(frozen=True, slots=True)` with `kind: Kind`, `payload: bytes`
  - `railctl.envelope.Kind` (Task 10) - `enum.Enum` with members `SOLICITED` and `UNSOLICITED`
  - `railctl.envelope.EnvelopeStats` (Task 10) - `@dataclass` with the fields `frames_ok`, `bytes_dropped`, `bad_xor`, `stray_replies`, `resyncs`, all `int`, all defaulting to `0`. `Link.stats()` reads the first four.
  - `railctl.envelope.hex_bytes(data: bytes) -> str` (Task 10) - uppercase, space separated
  - `railctl.envelope.liusb.LiUsbEnvelope()` (Task 10) - no-argument constructor, `expects_ack = True` as a class attribute so a test subclass can override it
  - `railctl.transport.Transport` (Task 11) - `Protocol` with `open() -> None`, `close() -> None`, `write(data: bytes) -> None`, `read(max_bytes: int, timeout: float) -> bytes`, `flush_input() -> None`, and the properties `is_open: bool`, `description: str`, `identity: str`, `diagnostic_hint: str`
  - `railctl.transport.fake.FakeClock` (Task 11, tests only) - `__init__(start: float = 0.0)`, `monotonic() -> float`, `sleep(seconds: float) -> None`, `advance(seconds: float) -> None`
  - `railctl.transport.fake.FakeTransport` (Task 11, tests only) - `__init__(*, clock=None, chunk_size=None, max_write=None, on_write=None, description="fake xpressnet", identity="fake", diagnostic_hint="check the station is reachable on the network")`; methods `expect(request: bytes, *, reply: bytes = b"") -> FakeTransport` and `queue(data: bytes) -> None`; attributes `written: list[bytes]`, `write_chunks: list[bytes]`, `flushes: int`, `script_pending: list[Exchange]`, `is_open: bool`
  - `tests/conftest.py` already provides the fixture `chunk_size`, parametrised over `[None, 1]` with ids `whole-frame` / `byte-at-a-time` (Task 11). `tests/unit/test_link.py` takes it through the `station` fixture; without it every test in this task errors with `fixture 'chunk_size' not found`.
  - `railctl.xbus.commands.cmd_station_version() -> bytes` and `railctl.xbus.codec.xor(data: bytes) -> int` (M3 codec tasks; used **only** in `tests/unit/test_link.py`, never by `link.py`)
  - `pyproject.toml` already sets `[tool.ruff.lint.isort] known-first-party = ["railctl"]` (M2 part, Task 1, Step 6 - not Task 10, which only checks it). Verified necessary, not optional: measured against ruff 0.16.1, `src = ["src", "."]` alone leaves `railctl` in the third-party block and `tests/unit/test_link.py` fails `I001`.
- Produces:
  - `railctl.link.Clock` - `Protocol` with `monotonic() -> float` and `sleep(seconds: float) -> None`
  - `railctl.link.LinkStats` - `@dataclass(frozen=True, slots=True)` with `requests: int`, `retries: int`, `timeouts: int`, `frames_ok: int`, `bytes_dropped: int`, `bad_xor: int`, `stray_replies: int`
  - `railctl.link.Link` - `__init__(transport: Transport, envelope: Envelope, *, default_timeout: float = DEFAULT_TIMEOUT, on_event: Callable[[Frame], None] | None = None, clock: Clock = time)`; methods `open() -> None`, `close() -> None`, `request(telegram: bytes, *, timeout: float | None = None) -> bytes`, `send(telegram: bytes, *, timeout: float | None = None) -> None`, `send_no_reply(telegram: bytes) -> None`, `await_frame(match: Callable[[Frame], bool], *, timeout: float) -> Frame`, `poll(timeout: float = 0.0) -> list[Frame]`, `drain() -> None`, `stats() -> LinkStats`, `recent_events() -> list[Frame]`, `recent_late_replies() -> list[Frame]`; properties `description: str`, `identity: str`, `version_telegram: bytes | None`
  - `railctl.link.DEFAULT_TIMEOUT = 5.0`, `PROGRAMMING_TIMEOUT = 95.0`, `HANDSHAKE_TIMEOUT = 2.0`, `SETTLE_TIME = 0.05`, `MAX_RETRIES = 1`, `_MAX_DRAIN_BYTES = 4096`

**Layering note.** `link.py` holds the three handshake bytes literally, because `xbus` sits
above it and it cannot call `cmd_station_version()`. The duplication is pinned by a test that
imports both layers, which a test is allowed to do. `link.py` contains no framing bytes at all
and logs no wire bytes - `tests/unit/test_envelope_isolation.py` enforces the first and a named
test below enforces the second.

`link.py` also holds **no connection-specific advice**. Every `hint=` it produces is
`self._transport.diagnostic_hint`. Spec line 583 requires the Z21 LAN transport to land with
no edit to `link.py`; a sentence about CDC interface indices in here would have to be edited
the moment a handshake fails over `z21:192.168.0.111:21105`, which is exactly the edit the
spec forbids. A named test below pins it.

- [ ] **Step 1: Write the failing link tests**

```python
# tests/unit/test_link.py
from __future__ import annotations

import logging

import pytest

from railctl.envelope import Frame, Kind
from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import LinkProtocolError, LinkTimeout, PortNotXpressNet
from railctl.link import (
    DEFAULT_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    MAX_RETRIES,
    PROGRAMMING_TIMEOUT,
    SETTLE_TIME,
    Link,
    LinkStats,
)
from railctl.transport.fake import FakeClock, FakeTransport
from railctl.xbus.codec import xor
from railctl.xbus.commands import cmd_station_version

VERSION_REQUEST = b"\x21\x21\x00"
VERSION_REPLY = b"\x63\x21\x40\x12\x10"
STATUS_REQUEST = b"\x21\x24\x05"
STATUS_REPLY = b"\x62\x22\x07\x47"
POLL = b"\x21\x10\x31"
CV8_RESULT = b"\x63\x14\x08\x91\xee"
ACK = b"\x01\x04\x05"
POWER_ON_BROADCAST = b"\x61\x01\x60"
UNSUPPORTED = b"\x61\x82\xe3"
BAD_XOR_REJECT = b"\x61\x80\xe1"
NOT_UNDERSTOOD = b"\x01\x0a\x0b"


class Fixture:
    """A Link over a scripted station, with the envelope doing all the framing."""

    def __init__(self, chunk_size=None, on_event=None):
        self.envelope = LiUsbEnvelope()
        self.clock = FakeClock()
        self.transport = FakeTransport(clock=self.clock, chunk_size=chunk_size)
        self.link = Link(self.transport, self.envelope, on_event=on_event, clock=self.clock)

    def expect(self, request: bytes, *replies: tuple[Kind, bytes]):
        reply = b"".join(self.envelope.frame(kind, tel) for kind, tel in replies)
        self.transport.expect(self.envelope.frame(Kind.SOLICITED, request), reply=reply)
        return self

    def push(self, kind: Kind, telegram: bytes):
        self.transport.queue(self.envelope.frame(kind, telegram))
        return self

    def open(self):
        self.expect(VERSION_REQUEST, (Kind.SOLICITED, VERSION_REPLY))
        self.link.open()
        return self


@pytest.fixture
def station(chunk_size) -> Fixture:
    return Fixture(chunk_size=chunk_size)


def test_the_handshake_bytes_agree_with_the_xbus_encoder():
    """link.py cannot import xbus - xbus sits above it - so the handshake is
    duplicated on purpose. A test may import both layers and pin them together.
    """
    from railctl.link import _HANDSHAKE_TELEGRAM

    # The encoder call goes on the left: ruff's SIM300 reads a SCREAMING_CASE
    # name on the left of == as a Yoda condition and fails the lint gate.
    assert cmd_station_version() == _HANDSHAKE_TELEGRAM
    assert xor(_HANDSHAKE_TELEGRAM) == 0


def test_open_runs_the_handshake_and_records_the_version(station):
    station.open()
    assert station.link.version_telegram == VERSION_REPLY
    assert station.transport.is_open is True
    assert station.transport.flushes == 1


def test_open_on_a_silent_port_raises_port_not_xpressnet_and_closes(station):
    station.expect(VERSION_REQUEST)
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "(none)" in str(caught.value)
    assert station.transport.is_open is False
    assert station.clock.monotonic() == pytest.approx(HANDSHAKE_TIMEOUT)


def test_open_on_a_port_that_answers_the_wrong_thing_says_so(station):
    """A prompt answer that is not a version reply must not be reported as silence.

    This project exists because a reply recorded as silence reads one layer up as
    "the hardware cannot do this". Reproducing that failure inside Link would be
    the same mistake: the station DID answer, and the operator needs the bytes.
    """
    station.expect(VERSION_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "62 22 07 47" in str(caught.value)
    assert "did not answer" not in str(caught.value)


def test_the_handshake_failure_hint_comes_from_the_transport(station):
    """Spec line 583: the Z21 LAN transport lands with no edit to link.py.

    A hardcoded sentence about CDC interface indices would have to be edited the
    first time a handshake fails over the network, so the advice is read off the
    transport instead.
    """
    station.expect(VERSION_REQUEST)
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert caught.value.hint == station.transport.diagnostic_hint


def test_open_on_the_telemetry_port_quotes_what_it_saw(station):
    # Queued as the REPLY, not up front: open() calls flush_input() before the
    # handshake, so anything queued earlier is gone by the time it writes.
    station.transport.expect(
        station.envelope.frame(Kind.SOLICITED, VERSION_REQUEST),
        reply=b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C CT 35.7'C CA 26 CB 08\r\n",
    )
    with pytest.raises(PortNotXpressNet) as caught:
        station.link.open()
    assert "5B 43 53 30" in str(caught.value)   # "[CS0"
    assert station.envelope.stats.bytes_dropped > 0
    assert station.envelope.stats.frames_ok == 0


def test_request_returns_the_bare_telegram(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY


def test_a_request_that_is_never_answered_raises_link_timeout_with_the_stats(station):
    station.open().expect(STATUS_REQUEST)
    with pytest.raises(LinkTimeout) as caught:
        station.link.request(STATUS_REQUEST, timeout=3.0)
    message = str(caught.value)
    assert "21 24 05" in message
    assert "3.0" in message
    assert "frames_ok=1" in message
    assert station.link.stats().timeouts == 1


def test_a_timeout_does_not_flush_the_receive_buffer(station):
    """Flushing risks cutting a frame in half. A late reply is caught, counted as
    a stray by the next drain(), and KEPT.

    docs/probe-results.md investigation R1 is a station that acknowledges a
    request and returns no result. The question there is whether something
    arrived late and what it was, so a counter alone is not enough: "one stray
    reply happened" cannot be told apart from "a 63 14 08 91 EE arrived 3 s after
    the budget", and that is the difference between "POM read unsupported" and
    "POM read is slower than the budget".
    """
    station.open().expect(STATUS_REQUEST)
    with pytest.raises(LinkTimeout):
        station.link.request(STATUS_REQUEST, timeout=1.0)
    station.push(Kind.SOLICITED, STATUS_REPLY)
    station.link.drain()
    assert station.link.stats().stray_replies == 1
    assert station.link.recent_late_replies() == [Frame(Kind.SOLICITED, STATUS_REPLY)]


def test_the_same_request_answered_first_with_silence_then_with_the_value(station):
    """The service-result poll loop in one test. If the fake ever loses its
    sequencing this fails, and with it the whole reason M4 is a milestone.
    """
    station.open()
    station.expect(POLL)
    station.expect(POLL, (Kind.SOLICITED, CV8_RESULT))
    with pytest.raises(LinkTimeout):
        station.link.request(POLL, timeout=1.0)
    assert station.link.request(POLL, timeout=1.0) == CV8_RESULT


def test_an_unsolicited_frame_during_a_request_is_dispatched_and_the_wait_continues():
    seen: list[Frame] = []
    fixture = Fixture(on_event=seen.append)
    fixture.open()
    fixture.expect(
        STATUS_REQUEST,
        (Kind.UNSOLICITED, POWER_ON_BROADCAST),
        (Kind.SOLICITED, STATUS_REPLY),
    )
    assert fixture.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert seen == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]
    assert fixture.link.recent_events() == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]


def test_an_on_event_callback_that_raises_cannot_lose_the_reply(caplog):
    def explode(frame: Frame) -> None:
        raise RuntimeError("bad callback")

    fixture = Fixture(on_event=explode)
    fixture.open()
    fixture.expect(
        STATUS_REQUEST,
        (Kind.UNSOLICITED, POWER_ON_BROADCAST),
        (Kind.SOLICITED, STATUS_REPLY),
    )
    with caplog.at_level(logging.WARNING, logger="railctl.link"):
        assert fixture.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert "bad callback" in caplog.text


def test_a_bad_xor_rejection_is_retried_exactly_once(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY
    assert station.link.stats().retries == 1


def test_a_not_understood_rejection_is_retried_exactly_once(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, NOT_UNDERSTOOD))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    assert station.link.request(STATUS_REQUEST) == STATUS_REPLY


def test_two_rejections_raise_link_protocol_error(station):
    station.open()
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    station.expect(STATUS_REQUEST, (Kind.SOLICITED, BAD_XOR_REJECT))
    with pytest.raises(LinkProtocolError, match="twice"):
        station.link.request(STATUS_REQUEST)
    assert station.link.stats().retries == MAX_RETRIES


def test_unsupported_is_a_real_answer_and_is_never_retried(station):
    """61 82 is how a capability probe learns an opcode is unavailable. Retrying
    it, or turning it into an exception here, is how a capability gets recorded
    as absent for the wrong reason.
    """
    station.open().expect(POLL, (Kind.SOLICITED, UNSUPPORTED))
    assert station.link.request(POLL) == UNSUPPORTED
    assert station.link.stats().retries == 0


def test_send_waits_for_the_ack_when_the_envelope_expects_one(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, ACK))
    station.link.send(STATUS_REQUEST)
    assert station.link.stats().requests == 2


def test_send_uses_send_no_reply_when_the_envelope_does_not_expect_an_ack():
    class NoAckEnvelope(LiUsbEnvelope):
        expects_ack = False

    envelope = NoAckEnvelope()
    clock = FakeClock()
    transport = FakeTransport(clock=clock)
    link = Link(transport, envelope, clock=clock)
    transport.open()
    transport.expect(envelope.frame(Kind.SOLICITED, STATUS_REQUEST))
    link.send(STATUS_REQUEST)
    assert clock.monotonic() >= SETTLE_TIME


def test_poll_returns_unsolicited_frames_and_files_late_replies_separately(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.push(Kind.SOLICITED, STATUS_REPLY)
    assert station.link.poll(0.0) == [Frame(Kind.UNSOLICITED, POWER_ON_BROADCAST)]
    assert station.link.stats().stray_replies == 1
    assert station.link.recent_late_replies() == [Frame(Kind.SOLICITED, STATUS_REPLY)]


class EndlessTelemetry(FakeTransport):
    """Interface ...45: the YD.Control stream never goes quiet.

    docs/probe-results.md, port map: ...41 is LocoNet (silent), ...43 is
    XpressNet, ...45 streams ASCII telemetry continuously at 57600 baud. read()
    on that port always has bytes ready.
    """

    LINE = b"[CS0] M: TC 0mA LC 0mA TV 15.2V TT 25.3'C\r\n"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0

    def read(self, max_bytes: int, timeout: float) -> bytes:
        self.reads += 1
        if self.reads > 1000:
            raise AssertionError("poll(0.0) never stopped draining")
        return self.LINE[:max_bytes]


def test_poll_gives_up_on_a_port_that_never_goes_quiet():
    """poll(0.0) runs at the top of every request(), so an unbounded drain hangs
    railctl with no timeout and no error - the one outcome worse than a wrong
    answer. The bound turns it back into the wrong-interface diagnosis the
    counters were built for.
    """
    envelope = LiUsbEnvelope()
    clock = FakeClock()
    transport = EndlessTelemetry(clock=clock)
    transport.open()
    link = Link(transport, envelope, clock=clock)

    assert link.poll(0.0) == []

    assert transport.reads < 200            # 4096 / 43 bytes per line is about 96
    assert link.stats().frames_ok == 0
    assert link.stats().bytes_dropped > 4000


def test_drain_discards_everything_queued(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.link.drain()
    assert station.link.poll(0.0) == []


def test_await_frame_reads_without_writing(station):
    station.open()
    station.push(Kind.UNSOLICITED, POWER_ON_BROADCAST)
    station.push(Kind.UNSOLICITED, CV8_RESULT)
    frame = station.link.await_frame(lambda f: f.payload[:2] == b"\x63\x14", timeout=1.0)
    assert frame.payload == CV8_RESULT
    assert station.transport.written == [station.envelope.frame(Kind.SOLICITED, VERSION_REQUEST)]


def test_await_frame_that_never_matches_raises_link_timeout(station):
    station.open()
    with pytest.raises(LinkTimeout):
        station.link.await_frame(lambda f: False, timeout=1.0)


def test_link_never_logs_wire_bytes(station, caplog):
    """The envelope owns the wire log in both directions. Two loggers means the
    same frame printed twice, or once with the framing and once without.
    """
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    with caplog.at_level(logging.DEBUG):
        station.link.request(STATUS_REQUEST)
    assert {record.name for record in caplog.records} == {"railctl.wire"}


def test_stats_carries_both_halves(station):
    station.open().expect(STATUS_REQUEST, (Kind.SOLICITED, STATUS_REPLY))
    station.link.request(STATUS_REQUEST)
    assert station.link.stats() == LinkStats(
        requests=2, retries=0, timeouts=0, frames_ok=2,
        bytes_dropped=0, bad_xor=0, stray_replies=0,
    )


def test_description_and_identity_come_from_the_transport(station):
    station.open()
    assert station.link.description == "fake xpressnet"
    assert station.link.identity == "fake"


def test_the_budgets_are_the_documented_ones():
    assert DEFAULT_TIMEOUT == 5.0
    assert PROGRAMMING_TIMEOUT == 95.0
    assert HANDSHAKE_TIMEOUT == 2.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_link.py -q`
Expected: FAIL - `ModuleNotFoundError: No module named 'railctl.link'`

- [ ] **Step 3: Implement `Link`**

```python
# src/railctl/link.py
"""One command in flight, retries, timeouts, and the frame pump.

Serialisation is a threading.RLock held for the whole duration of every method
that touches the transport, so the LI-USB one-command rule is structural rather
than conventional. RLock and not Lock because a station helper composing two
link calls in one critical section - a CV read is exactly that - must not
deadlock itself.

This module logs no wire bytes. The envelope owns the wire log in both
directions; see railctl/envelope/liusb.py.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from railctl.envelope import Envelope, Frame, Kind, hex_bytes
from railctl.errors import LinkProtocolError, LinkTimeout, PortNotXpressNet

if TYPE_CHECKING:
    from railctl.transport import Transport

DEFAULT_TIMEOUT = 5.0        # LI-USB normal-operation exchange budget
PROGRAMMING_TIMEOUT = 95.0   # service-mode budget: 1.5 min plus margin
HANDSHAKE_TIMEOUT = 2.0
SETTLE_TIME = 0.05           # send_no_reply only
MAX_RETRIES = 1
_READ_CHUNK = 256
_READ_SLICE = 0.2            # max blocking time per read, keeps Ctrl-C responsive
_EVENT_BUFFER = 256
_ERROR_SAMPLE = 32
# One MAX_BUFFER's worth. A port that never goes quiet is the YD.Control
# telemetry interface, which streams ASCII for ever; without this bound the
# non-blocking drain at the top of every request() never returns and railctl
# hangs with no timeout and no error instead of reporting the wrong interface.
_MAX_DRAIN_BYTES = 4096

# xbus sits ABOVE this layer, so cmd_station_version() is unreachable from here.
# tests/unit/test_link.py imports both and pins them together.
_HANDSHAKE_TELEGRAM = b"\x21\x21\x00"
_VERSION_HEADER = 0x63
_VERSION_MARKER = 0x21

# Both mean the telegram never arrived intact, so resending is safe: every
# command this tool sends is idempotent. 61 82 (not supported) is a real answer
# and is never retried - that is how a capability probe learns an opcode is
# unavailable.
_RETRY_PREFIXES = (b"\x61\x80", b"\x01\x0a")

_log = logging.getLogger("railctl.link")


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class LinkStats:
    requests: int
    retries: int
    timeouts: int
    frames_ok: int
    bytes_dropped: int
    bad_xor: int
    stray_replies: int


class Link:
    def __init__(
        self,
        transport: Transport,
        envelope: Envelope,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
        on_event: Callable[[Frame], None] | None = None,
        clock: Clock = time,
    ) -> None:
        self._transport = transport
        self._envelope = envelope
        self._default_timeout = default_timeout
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.RLock()
        self._events: deque[Frame] = deque(maxlen=_EVENT_BUFFER)
        # Late replies are kept, not just counted: see recent_late_replies().
        self._late: deque[Frame] = deque(maxlen=_EVENT_BUFFER)
        self._requests = 0
        self._retries = 0
        self._timeouts = 0
        self._version_telegram: bytes | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            self._transport.open()
            self._transport.flush_input()
            self._envelope.reset()
            seen = bytearray()
            try:
                self._requests += 1
                self._envelope.note_request(_HANDSHAKE_TELEGRAM)
                self._transport.write(self._envelope.wrap(_HANDSHAKE_TELEGRAM))
                deadline = self._clock.monotonic() + HANDSHAKE_TIMEOUT
                frame = self._pump(deadline, seen)
                if frame is None:
                    self._envelope.note_abandoned()
                    self._timeouts += 1
                    raise self._not_xpressnet(seen)
                self._envelope.note_reply(frame)
                if (
                    len(frame.payload) < 2
                    or frame.payload[0] != _VERSION_HEADER
                    or frame.payload[1] != _VERSION_MARKER
                ):
                    # A different failure from silence, and it gets a different
                    # message: the station answered, and the bytes it answered
                    # with are the evidence.
                    raise self._wrong_version_reply(frame)
                self._version_telegram = frame.payload
            except BaseException:
                self._transport.close()
                raise

    def _not_xpressnet(self, seen: bytes) -> PortNotXpressNet:
        """Nothing came back at all within the handshake budget."""
        sample = hex_bytes(bytes(seen[:_ERROR_SAMPLE])) or "(none)"
        return PortNotXpressNet(
            f"{self._transport.description} did not answer the XpressNet version "
            f"request {hex_bytes(_HANDSHAKE_TELEGRAM)} within {HANDSHAKE_TIMEOUT} s; "
            f"first bytes received: {sample}",
            hint=self._transport.diagnostic_hint,
        )

    def _wrong_version_reply(self, frame: Frame) -> PortNotXpressNet:
        """Something came back promptly, but it is not a version reply."""
        return PortNotXpressNet(
            f"{self._transport.description} answered {hex_bytes(frame.payload)}, "
            f"which is not an XpressNet version reply "
            f"(expected header {_VERSION_HEADER:02X} and first data byte "
            f"{_VERSION_MARKER:02X})",
            hint=self._transport.diagnostic_hint,
        )

    def close(self) -> None:
        with self._lock:
            self._transport.close()

    # -- exchanges ---------------------------------------------------------
    def request(self, telegram: bytes, *, timeout: float | None = None) -> bytes:
        budget = self._default_timeout if timeout is None else timeout
        with self._lock:
            attempts = 0
            while True:
                self.drain()
                self._requests += 1
                self._envelope.note_request(telegram)
                self._transport.write(self._envelope.wrap(telegram))
                frame = self._pump(self._clock.monotonic() + budget)
                if frame is None:
                    self._envelope.note_abandoned()
                    self._timeouts += 1
                    # The receive buffer is deliberately NOT flushed: flushing
                    # risks cutting a frame in half, and a late reply is caught
                    # and counted as a stray by the next drain(). The embedded
                    # stats are the diagnosis - bytes_dropped climbing with
                    # frames_ok at 0 is the wrong CDC interface, not a dead port.
                    raise LinkTimeout(
                        f"no reply to {hex_bytes(telegram)} within {budget} s; "
                        f"{self.stats()}",
                        # No connection-specific advice in this module: the
                        # transport supplies it, so the Z21 LAN transport lands
                        # without editing link.py (spec line 583).
                        hint=self._transport.diagnostic_hint,
                    )
                self._envelope.note_reply(frame)
                if frame.payload[:2] in _RETRY_PREFIXES:
                    if attempts >= MAX_RETRIES:
                        raise LinkProtocolError(
                            f"the station rejected {hex_bytes(telegram)} twice "
                            f"(last reply {hex_bytes(frame.payload)})"
                        )
                    attempts += 1
                    self._retries += 1
                    continue
                return frame.payload

    def send(self, telegram: bytes, *, timeout: float | None = None) -> None:
        # The request/response policy is read off the envelope, not hardcoded:
        # LI-USB acknowledges every command, the future Z21Envelope will not.
        if self._envelope.expects_ack:
            self.request(telegram, timeout=timeout)
        else:
            self.send_no_reply(telegram)

    def send_no_reply(self, telegram: bytes) -> None:
        with self._lock:
            self.drain()
            self._requests += 1
            self._envelope.note_request(telegram)
            self._transport.write(self._envelope.wrap(telegram))
            self._clock.sleep(SETTLE_TIME)
            # note_abandoned BEFORE the drain on purpose: if an ack does arrive
            # for a command declared reply-less, it lands in stray_replies, and
            # that counter is the evidence that expects_ack was wrong.
            self._envelope.note_abandoned()
            self.drain()

    def await_frame(self, match: Callable[[Frame], bool], *, timeout: float) -> Frame:
        with self._lock:
            deadline = self._clock.monotonic() + timeout
            while True:
                frame = self._envelope.pop()
                if frame is None:
                    remaining = deadline - self._clock.monotonic()
                    if remaining <= 0:
                        self._timeouts += 1
                        raise LinkTimeout(
                            f"no matching frame within {timeout} s; {self.stats()}"
                        )
                    self._envelope.feed(
                        self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE))
                    )
                    continue
                if frame.kind is Kind.UNSOLICITED:
                    self._dispatch(frame)
                if match(frame):
                    return frame

    def poll(self, timeout: float = 0.0) -> list[Frame]:
        with self._lock:
            deadline = self._clock.monotonic() + timeout
            events: list[Frame] = []
            drained = 0
            while True:
                frame = self._envelope.pop()
                if frame is not None:
                    if frame.kind is Kind.UNSOLICITED:
                        self._dispatch(frame)
                        events.append(frame)
                    else:
                        # A solicited frame with nothing outstanding is a late
                        # reply. The envelope has already counted it, but the
                        # counter alone cannot tell "one stray reply happened"
                        # from "a 63 14 08 91 EE arrived after the budget", and
                        # that distinction is the whole R1 investigation in
                        # docs/probe-results.md. Keep the bytes.
                        self._late.append(frame)
                    continue
                # Non-blocking reads until the port is empty, so poll(0.0) is a
                # real drain even when the port hands over one byte at a time.
                chunk = self._transport.read(_READ_CHUNK, 0.0)
                if chunk:
                    self._envelope.feed(chunk)
                    drained += len(chunk)
                    if drained < _MAX_DRAIN_BYTES:
                        continue
                    # The port has not gone quiet in a whole buffer's worth of
                    # bytes, so it is not going to. Returning is what lets the
                    # wrong-interface diagnosis happen instead of a hang.
                    return events
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    return events
                self._envelope.feed(
                    self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE))
                )

    def drain(self) -> None:
        with self._lock:
            self.poll(0.0)

    # -- reporting ---------------------------------------------------------
    def stats(self) -> LinkStats:
        counters = self._envelope.stats
        return LinkStats(
            requests=self._requests,
            retries=self._retries,
            timeouts=self._timeouts,
            frames_ok=counters.frames_ok,
            bytes_dropped=counters.bytes_dropped,
            bad_xor=counters.bad_xor,
            stray_replies=counters.stray_replies,
        )

    def recent_events(self) -> list[Frame]:
        return list(self._events)

    def recent_late_replies(self) -> list[Frame]:
        """Solicited frames that arrived with nothing outstanding, in order.

        `railctl doctor` reports these alongside stray_replies. "POM read
        unsupported" and "POM read is slower than the budget" look identical in
        the counter and different here.
        """
        return list(self._late)

    @property
    def description(self) -> str:
        return self._transport.description

    @property
    def identity(self) -> str:
        return self._transport.identity

    @property
    def version_telegram(self) -> bytes | None:
        return self._version_telegram

    # -- internals ---------------------------------------------------------
    def _pump(self, deadline: float, seen: bytearray | None = None) -> Frame | None:
        """Read and dispatch until a solicited frame arrives or the deadline passes.

        pop() is tried before the deadline check, so a frame already sitting in
        the envelope buffer is returned even on an expired budget: throwing away
        a frame we already hold is how a reply becomes silence.
        """
        while True:
            frame = self._envelope.pop()
            if frame is not None:
                if frame.kind is Kind.SOLICITED:
                    return frame
                self._dispatch(frame)
                continue
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return None
            chunk = self._transport.read(_READ_CHUNK, min(remaining, _READ_SLICE))
            if seen is not None:
                seen += chunk
            self._envelope.feed(chunk)

    def _dispatch(self, frame: Frame) -> None:
        self._events.append(frame)
        if self._on_event is None:
            return
        try:
            self._on_event(frame)
        except Exception:  # noqa: BLE001 - a bad callback must not lose a reply
            _log.warning("on_event callback raised for %r", frame, exc_info=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_link.py -q`
Expected: PASS, 48 passed. The file defines 27 test functions: 21 take the `station` fixture
and so run twice, whole-frame and byte-at-a-time (42), and 6 take no fixture parameter, so
21 * 2 + 6 = 48.

- [ ] **Step 5: Run the whole suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .`
Expected: `0 failed`, then `All checks passed!`

- [ ] **Step 6: Check the coverage gate before committing**

`link.py` is the largest single module this part adds and none of it is omitted from coverage
(`omit` names only `src/railctl/transport/serial_posix.py`). Run the pytest invocation Task 9 of
the M3 part put into `.github/workflows/ci.yml` — same flags, venv interpreter instead of the
runner's bare `python`, so the two cannot disagree on what is measured:

```bash
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

Expected: the coverage table with `src/railctl/link.py` in it, then
`Required test coverage of 90% reached.` and `0 failed`. The figure itself is recorded by the
first run of this step, not predicted here; the gate is **at least 90**.

The paths most likely to be missed are the ones the tests in Step 1 were written for: the retry
branch, the `LinkProtocolError` branch after two rejections, the `_MAX_DRAIN_BYTES` early return
in `poll`, and the `except Exception` arm of `_dispatch`. If the total is under 90, this task
adds the missing tests to `tests/unit/test_link.py`. Lowering `fail_under` is not an option, and
Task 13 must not inherit a red build from this task.

- [ ] **Step 7: Commit**

```bash
git add src/railctl/link.py tests/unit/test_link.py
git commit -m "feat(link): add the one-command link with retries, timeouts and stats"
```

---

### Task 13: `SerialTransport`, port discovery, `open_link`, and the M4 hardware acceptance

**Files:**
- Create: `src/railctl/transport/serial_posix.py`
- Modify: `src/railctl/transport/__init__.py` - append below the `Transport` protocol block (end of file)
- Create: `tests/unit/test_open_link.py`
- Create: `tests/hardware/test_m4_acceptance.py`
- Not touched, and named here so nobody adds them back: `pyproject.toml` is **not** edited (Task 1 already registered the `hardware` marker and put `-m 'not hardware'` in `addopts`; a second `addopts` or `markers` key is a TOML parse error), and `tests/hardware/__init__.py` is **not** created (Task 1 created it). Step 7 verifies both instead of writing them a second time.

**Interfaces:**
- Consumes:
  - From Task 1: `tests/hardware/` as a package with `__init__.py`, the registered marker `hardware`, and `addopts = "-q --strict-markers --strict-config -m 'not hardware'"`. `--strict-markers` means an unregistered marker is an error, so `pytestmark = pytest.mark.hardware` below only works because Task 1 registered it. Task 1 also left one canary in `tests/hardware/test_marker.py`, which is why the deselected count in Step 9 is four and not three.
  - `railctl.errors.RailctlError(message: str, *, hint: str | None = None)` (M2 errors task) - the base of every error below. **`str(exc)` is the message only; `exc.hint` is the hint or `None`.** The two are separate because spec line 159 has the CLI print `str(exc)` and then `Hint: {exc.hint}` when set - folding the hint into `__str__` would print it twice. A test that wants to assert on hint text must read `exc.hint`, never `str(exc)` and never `pytest.raises(..., match=...)`.
  - `railctl.errors.PortNotFound`, `AmbiguousPort`, `PortBusy`, `PortConfigError`, `PortNotOpen`, `PortNotXpressNet`, `TransportError`, `UnsupportedFeatureError` (M2 errors task) - all `RailctlError` subclasses with that same signature
  - `railctl.link.Link`, `railctl.link.HANDSHAKE_TIMEOUT`
  - `railctl.envelope.liusb.LiUsbEnvelope`
  - `railctl.transport.Transport` (Task 11) - including the `diagnostic_hint: str` property, which `SerialTransport` must implement
  - `pyproject.toml` already sets `[tool.ruff.lint.isort] known-first-party = ["railctl"]` (M2 part, Task 1, Step 6 - not Task 10, which only checks it). Verified necessary, not optional: measured against ruff 0.16.1, `src = ["src", "."]` alone leaves `railctl` in the third-party block and `tests/unit/test_open_link.py` fails `I001`.
- Produces:
  - `railctl.transport.serial_posix.SerialConfig` - `@dataclass(frozen=True, slots=True)` with `port: str`, `baudrate: int = BAUDRATE`
  - `railctl.transport.serial_posix.SerialTransport` - `__init__(config: SerialConfig)`, the whole `Transport` protocol including `diagnostic_hint`, plus `__enter__`/`__exit__`
  - `railctl.transport.serial_posix.BAUDRATE = 57600`, `READ_CHUNK = 256`, `WRITE_SELECT_TIMEOUT = 1.0`, `CDC_INDEX_HINT: str` (the sentence `SerialTransport.diagnostic_hint` returns)
  - `railctl.transport.PORT_GLOB = "/dev/cu.usbmodem*3"`
  - `railctl.transport.list_candidate_ports() -> list[str]`
  - `railctl.transport.find_xpressnet_port(candidates: Sequence[str] | None = None) -> str`
  - `railctl.transport.transport_for(target: str) -> Transport`
  - `railctl.transport.open_link(target: str = "auto", *, on_event: Callable[[Frame], None] | None = None) -> Link`

**Layering notes.** Rule 4 - connection targets are opaque strings parsed only by
`transport.open_link()` - is satisfied because `transport_for()` is the single place that
looks inside the string, and nothing above `transport` may split it. `serial_posix.py` is the
only module in `railctl` that owns a file descriptor and is omitted from coverage because it
has no logic. The spec puts the budget at "~60 lines"; the file Step 3 writes measures **88**
code lines by the count in Step 6 (non-blank, non-comment, docstring excluded), because the
spec's estimate predates the error handling, the three properties and the context manager.
**Step 6 fails above 95** - past that, logic has leaked into the one module coverage does not
watch and belongs in `Link` or the envelope instead. `open_link` imports `Link` inside the function body: `link.py`
type-hints `Transport` under `TYPE_CHECKING`, so a module-level import here would be the only
edge closing the cycle.

- [ ] **Step 1: Write the failing target-grammar tests**

```python
# tests/unit/test_open_link.py
from __future__ import annotations

import pytest

from railctl.errors import AmbiguousPort, PortNotFound, TransportError, UnsupportedFeatureError
from railctl.transport import find_xpressnet_port, transport_for
from railctl.transport.serial_posix import BAUDRATE, CDC_INDEX_HINT, SerialTransport

PORT_43 = "/dev/cu.usbmodem7010A00011943"
PORT_OTHER = "/dev/cu.usbmodemAAAA3"

# str(exc) is the message alone and exc.hint is the hint, because the CLI prints
# them on separate lines (spec line 159). Anything asserted about advice is read
# off .hint; pytest.raises(match=...) only ever sees the message.


def test_a_single_candidate_is_the_xpressnet_port():
    assert find_xpressnet_port([PORT_43]) == PORT_43


def test_no_candidate_raises_port_not_found():
    with pytest.raises(PortNotFound, match="no XpressNet"):
        find_xpressnet_port([])


def test_two_candidates_raise_ambiguous_port_naming_both():
    with pytest.raises(AmbiguousPort) as caught:
        find_xpressnet_port([PORT_43, PORT_OTHER])
    assert PORT_43 in str(caught.value)
    assert PORT_OTHER in str(caught.value)
    assert "serial:" in caught.value.hint


def test_an_explicit_serial_target_is_used_verbatim():
    transport = transport_for(f"serial:{PORT_43}")
    assert isinstance(transport, SerialTransport)
    assert transport.description == f"xpressnet serial {PORT_43}"
    assert transport.identity == PORT_43
    # The advice link.py quotes on a failed handshake belongs to the transport,
    # so the Z21 LAN transport lands without editing link.py (spec line 583).
    assert transport.diagnostic_hint == CDC_INDEX_HINT


def test_a_serial_target_with_no_path_is_rejected():
    with pytest.raises(PortNotFound, match="serial:"):
        transport_for("serial:")


def test_a_well_formed_z21_target_parses_and_is_refused_cleanly():
    """The future LAN transport must not crash the parser today. Parsing it and
    then refusing it is what tells a user their address was understood.
    """
    with pytest.raises(UnsupportedFeatureError) as caught:
        transport_for("z21:192.168.0.111:21105")
    assert "192.168.0.111:21105" in str(caught.value)


def test_a_malformed_z21_target_is_a_transport_error_not_a_crash():
    with pytest.raises(TransportError) as caught:
        transport_for("z21:192.168.0.111:not-a-port")
    assert "192.168.0.111:not-a-port" in str(caught.value)
    assert "z21:HOST:PORT" in caught.value.hint


def test_an_unknown_target_names_the_forms_that_work():
    with pytest.raises(TransportError) as caught:
        transport_for("http://station.local")
    assert "http://station.local" in str(caught.value)
    message = caught.value.hint
    assert "auto" in message
    assert "serial:" in message
    assert "z21:" in message


def test_the_baudrate_is_the_one_lenz_23151_specifies():
    assert BAUDRATE == 57600
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_open_link.py -q`
Expected: FAIL - `ImportError: cannot import name 'find_xpressnet_port' from 'railctl.transport'`

- [ ] **Step 3: Implement the serial transport**

```python
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
            return os.read(fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise TransportError(f"read from {self._config.port} failed: {exc}") from exc
```

- [ ] **Step 4: Implement discovery and `open_link`**

Append to `src/railctl/transport/__init__.py`, below the `Transport` protocol:

```python


import glob  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

from railctl.errors import (  # noqa: E402
    AmbiguousPort,
    PortNotFound,
    TransportError,
    UnsupportedFeatureError,
)
from railctl.transport.serial_posix import SerialConfig, SerialTransport  # noqa: E402

if TYPE_CHECKING:
    from railctl.envelope import Frame
    from railctl.link import Link

# The CDC interface index picks the bus on this hardware: 1 is LocoNet (silent),
# 3 is XpressNet, 5 is the YD.Control telemetry stream. The glob is a guess;
# confirmation is Link.open()'s handshake and it is mandatory.
PORT_GLOB = "/dev/cu.usbmodem*3"
Z21_DEFAULT_PORT = 21105


def list_candidate_ports() -> list[str]:
    return sorted(glob.glob(PORT_GLOB))


def find_xpressnet_port(candidates: Sequence[str] | None = None) -> str:
    found = list(list_candidate_ports() if candidates is None else candidates)
    if not found:
        raise PortNotFound(
            f"no XpressNet CDC port matching {PORT_GLOB}",
            hint="check the USB cable and that the station is powered",
        )
    if len(found) > 1:
        joined = ", ".join(found)
        raise AmbiguousPort(
            f"more than one XpressNet CDC port matches {PORT_GLOB}: {joined}",
            hint=f"name one, for example serial:{found[0]}",
        )
    return found[0]


def transport_for(target: str) -> Transport:
    """The one place a connection target is parsed. Nothing above this may split it."""
    if target == "auto":
        return SerialTransport(SerialConfig(find_xpressnet_port()))
    if target.startswith("serial:"):
        port = target[len("serial:") :]
        if not port:
            raise PortNotFound(
                "serial: target has no device path",
                hint="use serial:/dev/cu.usbmodem... or auto",
            )
        return SerialTransport(SerialConfig(port))
    if target.startswith("z21:"):
        host, separator, port = target[len("z21:") :].partition(":")
        if not host or (separator and not port.isdigit()):
            raise TransportError(
                f"malformed target {target!r}",
                hint=f"expected z21:HOST:PORT, for example z21:192.168.0.111:{Z21_DEFAULT_PORT}",
            )
        # Parsed, understood, and refused: the LAN transport is a pure addition
        # scheduled after this milestone, and a user whose address was correct
        # deserves to be told that rather than shown a parse error.
        where = f"{host}:{port or Z21_DEFAULT_PORT}"
        raise UnsupportedFeatureError(
            f"the Z21 LAN transport is not implemented yet ({where})",
            hint="use auto or serial:/dev/cu.usbmodem...",
        )
    raise TransportError(
        f"unknown connection target {target!r}",
        hint="expected auto, serial:/dev/cu.usbmodem..., or z21:HOST:PORT",
    )


def open_link(target: str = "auto", *, on_event: Callable[[Frame], None] | None = None) -> Link:
    """Resolve a target, build the link, open the port and run the handshake."""
    from railctl.envelope.liusb import LiUsbEnvelope
    from railctl.link import Link  # deferred: link.py type-hints Transport from here

    link = Link(transport_for(target), LiUsbEnvelope(), on_event=on_event)
    link.open()
    return link
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_open_link.py -q`
Expected: PASS, 9 passed

- [ ] **Step 6: Check `serial_posix.py` has not grown logic**

`grep -vc '^\s*$'` counts the module docstring and the comments, which are the bulk of this
file and are exactly what should be encouraged in it, so measure the code alone: non-blank,
non-comment lines minus the docstring.

Run:

```bash
.venv/bin/python -c "import ast,pathlib; s=pathlib.Path('src/railctl/transport/serial_posix.py').read_text(); print(len([l for l in s.splitlines() if l.strip() and not l.strip().startswith('#')]) - len(ast.get_docstring(ast.parse(s)).splitlines()) - 1)"
```

Expected: `88` for the file Step 3 writes. **Fail the step above 95.** If it climbs past that,
protocol logic has leaked into the one module coverage does not watch - move it into `Link`
or the envelope. Nothing currently in the file is removable: it is `open`, `close`, `read`,
`write`, `flush_input`, three properties, the context manager and the `SerialConfig`
dataclass, with no branch that is not an error path.

- [ ] **Step 7: Verify the `hardware` marker is registered - do not register it again**

Task 1 already wrote both lines into `[tool.pytest.ini_options]`:

```toml
addopts = "-q --strict-markers --strict-config -m 'not hardware'"
markers = ["hardware: needs the physical YD7010; deselected by default"]
```

Writing them a second time is a duplicate TOML key and pytest would refuse to start, so this
step **checks** rather than edits:

```bash
.venv/bin/python -m pytest --markers | grep hardware
```

Expected: a line reading
`@pytest.mark.hardware: needs the physical YD7010; deselected by default`

If that prints nothing, stop: Task 1 did not land, Step 9 would run the three hardware tests on
a machine with no station attached, `find_xpressnet_port()` would raise `PortNotFound`, and the
deselected count would never appear.

- [ ] **Step 8: Write the hardware acceptance suite**

```python
# tests/hardware/test_m4_acceptance.py
"""M4 acceptance. NEEDS THE PHYSICAL YD7010 ATTACHED.

Run explicitly:  .venv/bin/python -m pytest -m hardware -q -s
These are skipped by the default addopts.
"""

from __future__ import annotations

import os

import pytest

from railctl.envelope.liusb import LiUsbEnvelope
from railctl.errors import PortNotXpressNet
from railctl.link import Link
from railctl.transport import find_xpressnet_port, open_link
from railctl.transport.serial_posix import SerialConfig, SerialTransport

pytestmark = pytest.mark.hardware

VERSION_HEADER = 0x63
VERSION_MARKER = 0x21


def test_auto_detection_finds_the_xpressnet_port():
    port = find_xpressnet_port()
    print(f"\nXpressNet port: {port}")
    assert port.startswith("/dev/cu.usbmodem")
    assert port.endswith("3")


def test_open_link_auto_completes_the_handshake():
    link = open_link("auto")
    try:
        telegram = link.version_telegram
        print(f"\n{link.description}  version telegram {telegram.hex(' ').upper()}")
        assert telegram[0] == VERSION_HEADER
        assert telegram[1] == VERSION_MARKER
        # Reference unit: 63 21 40 12 -> XpressNet 4.0, command station id 0x12.
        assert telegram[2] == 0x40
        assert telegram[3] == 0x12
        assert link.stats().frames_ok >= 1
        assert link.stats().timeouts == 0
    finally:
        link.close()


def test_the_telemetry_port_shows_bytes_dropped_climbing_with_no_frames():
    """The acceptance check for the stats counters.

    Interface 5 is the YD.Control telemetry stream: ASCII lines, no framing.
    Opening it instead of the XpressNet interface must fail loudly, and the two
    counters must say WHY - a climbing bytes_dropped with frames_ok stuck at 0
    is "wrong CDC interface", not "dead port". That distinction is the whole
    reason the counters exist.
    """
    telemetry = find_xpressnet_port()[:-1] + "5"
    if not os.path.exists(telemetry):
        pytest.skip(f"{telemetry} is not present")

    envelope = LiUsbEnvelope()
    link = Link(SerialTransport(SerialConfig(telemetry)), envelope)
    with pytest.raises(PortNotXpressNet) as caught:
        link.open()
    print(f"\n{caught.value}")
    stats = envelope.stats
    print(f"frames_ok={stats.frames_ok} bytes_dropped={stats.bytes_dropped}")
    assert stats.frames_ok == 0
    assert stats.bytes_dropped > 0
```

- [ ] **Step 9: Run the software suite and confirm the hardware tests are excluded**

Run: `.venv/bin/python -m pytest -q`
Expected: `0 failed`, and the collection report shows **4 deselected** - the three written in
Step 8 plus the `tests/hardware/test_marker.py` canary Task 1 left there. If it shows
`0 deselected` and failures naming `PortNotFound`, the marker registration from Task 1 is
missing; go back to Step 7.

- [ ] **Step 10: The M4 acceptance run, with the station attached**

Connect the YD7010 over USB, then run in order:

```bash
.venv/bin/python -c "from railctl.transport import find_xpressnet_port; print(find_xpressnet_port())"
```
Expected on the reference unit: `/dev/cu.usbmodem7010A00011943`

```bash
.venv/bin/python -m pytest -m hardware -q -s
```
Expected: `4 passed` - the three written in Step 8 plus Task 1's `tests/hardware/test_marker.py`
canary, which asserts nothing about the hardware and exists only so that the deselected count in
a default run is never zero. Printed lines resembling

```
XpressNet port: /dev/cu.usbmodem7010A00011943
xpressnet serial /dev/cu.usbmodem7010A00011943  version telegram 63 21 40 12 10
xpressnet serial /dev/cu.usbmodem7010A00011945 did not answer the XpressNet version
request 21 21 00 within 2.0 s; first bytes received: 5B 43 53 30 5D 20 4D 3A ...
frames_ok=0 bytes_dropped=248
```

The exact `bytes_dropped` count varies with how much telemetry arrives in two seconds; what
must hold is `frames_ok == 0` and `bytes_dropped > 0`.

- [ ] **Step 11: Confirm the M4 acceptance criterion, with the exact commands**

Design doc line 1581 lists four things that must be true when M4 is done. Three of them are
proved by tests written in Tasks 10 and 11, and this is the last task of M4, so they are re-run
here as one gate rather than left to whoever remembers which task they were in. Run all four in
order:

```bash
.venv/bin/python -m pytest tests/unit/test_envelope_liusb.py -q
.venv/bin/python -m pytest tests/unit/test_fake_transport.py -q -k "second_command_while_a_reply_is_outstanding"
.venv/bin/python -c "from railctl.transport import find_xpressnet_port; print(find_xpressnet_port())"
.venv/bin/python -m pytest -m hardware -q -s
```

| Clause of line 1581 | What proves it |
|---|---|
| the envelope test file passes including byte-at-a-time feeding | `test_byte_at_a_time_feeding_yields_the_same_frame` and `test_arbitrary_chunking_yields_identical_frames`, in the first command |
| ...and the checksum-resync case | `test_a_bad_checksum_costs_one_byte_and_the_next_good_frame_arrives`, same command |
| `FakeTransport` raises on a pipelined write | `test_a_second_command_while_a_reply_is_outstanding_raises`, the second command - expected `1 passed` |
| `open_link("auto")` finds and identifies the real port, and the telemetry port shows `bytes_dropped` climbing with `frames_ok` stuck at 0 | the third and fourth commands, which are Step 10's run |

The first two commands need no hardware; the last two need the YD7010 attached. If the station is
not available, record which of the four clauses is unverified rather than marking M4 done - a
milestone recorded as met on evidence nobody collected is the failure this whole project exists
to stop.

- [ ] **Step 12: Check the coverage gate before committing**

This is the last task of M4 and the one with the most code that no unit test drives directly, so
it is also the one most likely to push the total down. Run the pytest invocation Task 9 of the M3
part put into `.github/workflows/ci.yml` — same flags, venv interpreter:

```bash
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

Expected: the coverage table, then `Required test coverage of 90% reached.` and `0 failed`. No
percentage is stated here - the first execution of this step is what records it. The gate is
**at least 90**, and it is the same `source = ["railctl"]`, `branch = true`, `fail_under = 90`
configuration Task 1 of the M2 part wrote.

Read the table with the omit rule in mind. `src/railctl/transport/serial_posix.py` is omitted, so
Step 3's module contributes nothing either way - that is what Step 6's line-count check exists to
protect. Everything Step 4 appended to `src/railctl/transport/__init__.py` **is** measured:
`list_candidate_ports`, `find_xpressnet_port`, `transport_for` and `open_link`. The tests in Step
1 cover every branch of `transport_for` and both error branches of `find_xpressnet_port`; the
`target == "auto"` branch and `open_link` itself reach real hardware, so if they show as missing
lines, cover them with a test that passes an explicit candidate list or a `FakeTransport` rather
than by widening `omit`.

The command above needs no extra flag to match CI: the default `addopts` already carry
`-m 'not hardware'`, so the suite Step 8 wrote is deselected in both runs and none of its lines
counts as covered on either side.

If the total comes in under 90, this task owns the missing tests. Lowering `fail_under` is not an
option, and a milestone must not be closed on a red build: M4 is the last chance to fix it before
Plan 3 starts on top of it.

- [ ] **Step 13: Lint and commit**

```bash
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
git add src/railctl/transport tests/unit/test_open_link.py tests/hardware/test_m4_acceptance.py
git commit -m "feat(transport): add the POSIX serial transport, port discovery and open_link"
```

`pyproject.toml` is not in the `git add`: Step 7 verified it and changed nothing.

Expected from the lint pair: `All checks passed!`

---
