# railctl

Drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet. macOS only.

## Install

```sh
uv sync
```

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
