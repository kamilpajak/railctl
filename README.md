# railctl

Drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet. macOS and Linux
(Linux measured under WSL2 with usbipd; bare metal unverified).

## Install

```sh
uv sync
```

## Platforms

The serial layer is POSIX `termios` on both. What differs is how the station's three CDC
functions are named, so `auto` carries one pattern per platform: `/dev/cu.usbmodem*3` on
macOS and `/dev/serial/by-id/usb-YaMoRC_YD700x*-if02` on Linux. The numbers are not a typo
on either side — macOS names a node after each function's data interface (1, 3, 5) and
Linux's `by-id` after its control interface (`if00`, `if02`, `if04`), so macOS `3` and Linux
`-if02` are the same wire and `-if03` matches nothing. A platform with no measured pattern
is refused by name rather than reported as a missing port; `--target serial:<device path>`
needs no discovery and works anywhere `termios` does.

On Linux `auto` needs `/dev/serial/by-id`, which udev creates — without udev there is
nothing to glob. The node is usually owned by the `dialout` group, so a first run that says
*Permission denied* means the account is not in that group yet; `railctl` says so rather
than reporting the port as busy. The port path is also the key `capabilities.json` is stored
under, so a `railctl doctor` run on macOS does not carry over to Linux: the second platform
starts at `unknown`, never at `no`.

**What has been measured, and where.** macOS is the bench this tool was written on. The
Linux measurements were taken on 2026-10-14 under WSL2 with the station forwarded by
`usbipd`, where `railctl version` answers XpressNet 4.0 and command station id `0x12` over
the `-if02` path. **Bare-metal Linux has not been tested by anyone.** On that bench the
first exchange after each `usbipd attach` answered `61 80`, a Transfer Error; the handshake
now resends once, so it is invisible, but whether the cause is Linux, usbip or the station
is not established — `docs/probe-results.md` has the bytes and the reasoning. The automated
suite runs on Linux in CI with no station attached, so it measures the port-discovery logic
and not the port.

`tools/probe/` is frozen M1 output and still looks for `/dev/cu.usbmodem7010*`. On Linux it
reports the station as not connected whatever is attached, by design: its mutation baseline
was measured against that exact source and reformatting it would invalidate a measured
number. Use `railctl` itself there.

## Start here

```sh
railctl doctor --address 3          # probe the station and write capabilities.json
railctl schema --format json        # the machine-readable command tree
railctl --help                      # neither of the last two opens a port
```

`doctor` is the first command to run on a new bench. It measures what the station and
the decoder can do and writes the answers to `~/.config/railctl/capabilities.json`
(`$XDG_CONFIG_HOME/railctl/capabilities.json` when that is set), which every other
command then reads. `--no-save` runs the probe without writing anything.

## Commands

| Command | What it does |
| --- | --- |
| `railctl doctor` | Probe the station's capabilities and write `capabilities.json` |
| `railctl status` | Station status: the raw byte and the decoded bits |
| `railctl version` | XpressNet version and command station id |
| `railctl power on\|off\|resume` | Track power. `on` comes up HELD; `resume` is the release |
| `railctl stop` | Emergency stop: everything, or one locomotive with `--address` |
| `railctl drive SPEED` | Set speed step and direction |
| `railctl function F STATE` | Set F0-F28 on, off or toggle |
| `railctl monitor` | Decode broadcasts until Ctrl-C |
| `railctl schema` | The command tree as JSON, for an agent or a wrapper |

Every command takes the same eight global options on either side of the verb -
`railctl --address 3 doctor` and `railctl doctor --address 3` resolve identically - and
every one has `--help` with fixed `OUTPUT`, `EXIT CODES` and `EXAMPLES` sections.

## Output

`stdout` carries the result and nothing else; logs, progress notices, warnings and
errors go to `stderr`. `--format=human|json|ndjson`, with `--json` as an alias for
`--format=json`.

In `json` mode stdout holds exactly one JSON value. `monitor` is the one streaming
command: in `ndjson` mode it writes one compact object per line, numbered from 0, and
always finishes with a `summary` line - including when you interrupt it, so a consumer
can tell the run ended from the same stream it was reading.

Errors go to stderr as one `railctl/error/v1` object with a stable `code`, a
`retryable` flag and a `suggestions` array of runnable argv arrays. Exit codes are
documented per command in its `--help` and in `railctl schema`.

## Track power comes up held

`railctl power on` energises the track and leaves the whole layout held in emergency
stop. `railctl power resume` is the release, and it is a separate command on purpose:
an emergency stop holds the station's refresh buffer and never clears it, so the
release is the moment stored speeds start locomotives. Run it with the layout in view.

`railctl doctor --power-on` follows the same order and also leaves the layout held. Its
report says so, in both renderings.

## The rule this project enforces

A capability is never recorded as absent because the instrument measuring it was
broken. Three outcomes stay distinguishable everywhere: **true** (measured working),
**false** (the station said so), **unknown** (no answer, an unparsed answer, or never
tried). See `CLAUDE.md` for the one deliberate exception and how it carries its
provenance.

## Development

```sh
uv sync --frozen                  # what CI does
uv run pytest                     # the suite; never add -q
uv run ruff check .
uv run ruff format --check .
uv run pytest -m hardware -s      # needs the YD7010 attached; deselected by default
```

What the hardware actually answers is recorded in `docs/probe-results.md`. That file is
the authority; a documentation summary that contradicts it is wrong.
