# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-19

First release. `railctl` drives a YaMoRC YD7010 command station over XpressNet on a USB
serial port, and reads and writes CVs on ZIMO decoders. macOS, Python 3.11+, `typer` as
the only runtime dependency.

Every claim this tool makes about the hardware was measured on a real station and a real
decoder; what was not measured is reported as unknown rather than guessed. That rule is
the reason the project exists, and it is visible in the output: a capability is `true`
only when it was seen to work, `false` only when the station refused it in so many words,
and `null` when nobody has established either.

### Added

**Finding out what you have**

- `railctl doctor` probes the command station and writes
  `~/.config/railctl/capabilities.json`, which every later command resolves against. It
  reports each capability as measured, refused or unknown - in the JSON (`true` / `false`
  / `null`), in the human text (`yes` / `no` / `unknown`) and in the exit code. `--power-on`
  energises a dead track, `--no-programming-track` skips the checks that need a decoder in
  place, `--no-save` writes nothing. The report says what the run left the layout doing:
  whether it energised the track, whether the layout is held, and which locomotive was
  sent speed 0. A probe it cannot save is still printed, with `saved_to: null` and a
  warning saying why. A capability file written by one run is never erased by a narrower
  one: `null` - "this run established nothing here" - does not overwrite what an earlier
  run measured.
- `railctl status`, `railctl version` and `railctl schema`. The last one is the
  machine-readable manifest of the whole command tree: paths, options, types, defaults,
  exit codes and risk metadata, so an agent can discover the CLI without loading its help.

**Running trains**

- `railctl power on | resume | off`. `power on` comes up **held**: it energises, stops
  everything, and sends speed 0 to the address it can resolve - in that order, because a
  stop sent before the track is live does nothing. `power resume` is the release, and it
  is the command to run while you are watching the layout.
- `railctl drive`, `railctl function` (F0-F28) and `railctl stop`. Anything that could
  start a train refuses on emergency stop, emergency off or an open service-mode session;
  `speed 0` is always allowed. A command that exits with a locomotive still moving says so
  on stderr.
- `railctl monitor` decodes broadcasts until Ctrl-C. `--format=ndjson` streams one object
  per line and always closes with a `summary`, even when interrupted.

**Reading and writing CVs**

- `railctl cv read` and `railctl cv write`, on the main track (POM) or the programming
  track (service mode), with `--mode auto` choosing from measured capabilities. A write is
  read back by default: a station's acknowledgement proves the station produced a value,
  not that the decoder kept it. The ZIMO CV31/CV32 index page is handled explicitly rather
  than assumed.
- `railctl backup` writes the curated ZIMO CV set to a `railctl/backup/v1` file. Reads
  only - it never writes a CV, not even the index selectors. Two backups of an unchanged
  decoder are byte-identical, so a file in git shows one line per changed CV.
- `railctl backup --all` sweeps every CV the resolved mode can reach. The range comes from
  measured capabilities alone, an encoding nobody probed never widens it, and the file
  records the range it covered. CVs the catalog names keep their names; the rest are
  `cv0617` with `source: sweep`. A sweep estimated over a minute asks first and revises its
  estimate as it goes, with progress on stderr and never on stdout. Past CV511 it says that
  no value up there has been checked against a known quantity. A full sweep normally ends
  at exit 9 with many `no_response` rows: most CV numbers are not implemented in any
  decoder, and this hardware cannot tell that from silence.
- `railctl restore` writes a file back in four stages, ordinary settings first and the
  dangerous ones last: the RailCom and configuration bytes, then the address CVs only if
  you ask for them, then CV144 - which on the older ZIMO MX family is the programming
  lock, so writing it any earlier would block the rest of the run including the checks.
  Each stage is verified by reading every CV it wrote back against the value that was
  intended, with one retry on a mismatch. The decoder's identity is checked before
  anything is written at all, and a serial that does not match the file is refused
  outright unless you name it. A stage that fails is reported with what it wrote and what
  it verified; nothing is silently rolled back.
- `railctl diff` compares a file against the decoder, or two files against each other with
  no station attached.

**How every command behaves**

- `--format=human | json | ndjson`. In JSON mode stdout carries exactly one value; logs,
  progress and warnings go to stderr. Errors carry a stable `code`, a `retryable` flag and
  suggestions as runnable argument lists rather than sentences.
- A small, documented exit-code set, published per command in `railctl schema`.
- Confirmation is required where it is earned - a restore, a write to an address or
  configuration CV, a long sweep - and never for throttle commands. `--yes` answers them;
  with no terminal a command that needs an answer refuses immediately instead of blocking.
