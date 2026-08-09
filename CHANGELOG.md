# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `railctl doctor` - probes the command station and writes
  `~/.config/railctl/capabilities.json`. Reports every capability as measured, refused
  or unknown, in the JSON (`true` / `false` / `null`), in the human text (`yes` / `no` /
  `unknown`) and in the exit code. `--power-on` energises a dead track, `--no-programming-track`
  skips the checks that need a decoder on the programming track, and `--no-save` writes
  nothing. (#15)
- `railctl monitor` - decodes broadcasts until Ctrl-C. `--format=human` prints each one
  as it arrives, `--format=ndjson` streams one JSON object per line and always ends with
  a `summary` line even when interrupted, and `--format=json` buffers and prints exactly
  one value. `--limit N` ends the run without an interrupt.
- The doctor's report now says what the run left the layout doing - whether it energised
  the track, whether the layout is held, and which locomotive was sent speed 0.

### Fixed

- `doctor --power-on` no longer leaves the layout free to move. It energises, holds the
  whole layout with an emergency stop, and sends speed 0 to the address it is about to
  probe, in that order - measured 2026-08-09, a stop sent before the track is energised
  does nothing. The hold is re-asserted and read back at the end of the run, because
  leaving service mode clears it. The doctor never releases the hold; run
  `railctl power resume` when you are watching the layout. (#14)
- A POM read the decoder never answers now suggests `railctl doctor` instead of
  suggesting nothing. Silence and a `61 82` refusal are different answers, but both are
  a failed POM read and both need the same next command.
