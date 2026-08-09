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
- A capability file written by one run is no longer erased by a narrower one. Every
  field is now merged individually, and `null` - "this run established nothing here" -
  never overwrites a value an earlier run measured. `pom_read_provenance` is also
  written to the file at all now: it was parsed on load and published in the envelope
  but silently dropped by the writer, so a save and a load erased the difference
  between "the station refused" and "nothing came back".
- A plain `railctl doctor` no longer releases a hold it found on the layout. Leaving
  service mode sends resume-operations, which clears an emergency stop, so a run on a
  layout left held by `railctl power on` released it and reported that it had changed
  nothing. Every service-mode session now puts back the hold it found, the run
  re-asserts and reads it back at the end, and the report says the layout is still
  held.
- `railctl doctor` no longer throws away a finished probe when it cannot write
  `capabilities.json`. The measurements are printed, `saved_to` is `null` and a
  `capabilities_not_saved` warning says why.
- The doctor reports the layout on every ending, including when the probe fails partway
  (the state travels in the error envelope's `details.layout`), and it no longer
  describes a track the station reports OFF as able to move. A locomotive that refused
  the speed-0 telegram is now warned about instead of exiting 0 in silence.
- `railctl monitor` now closes its ndjson stream with the same exit code the error
  envelope carries, instead of `0`, when the run ends in anything but Ctrl-C. Both
  streaming paths flush per line, so a piped monitor shows each broadcast as it
  arrives, and `--limit` below 1 is refused with a usage error rather than quietly
  emitting one event.
- A POM read the decoder never answers now suggests `railctl doctor` instead of
  suggesting nothing. Silence and a `61 82` refusal are different answers, but both are
  a failed POM read and both need the same next command.
