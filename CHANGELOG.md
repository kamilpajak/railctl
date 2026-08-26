# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-26

### Changed

- **BREAKING: `railctl` now publishes eight exit codes instead of twenty-one.** A script
  that branches on the exit code needs updating; a script that reads `error.code` from the
  JSON envelope needs no change at all, and that was the point. Eleven of the retired codes
  (10-20) named one failure each and said nothing `error.code` did not already say, so an
  agent had to carry a 21-branch table where this project's own CLI rules promise six.
  Three of the numbers actively misled anyone following the convention: `4` meant a
  malformed reply rather than "not found", `7` meant a permanent refusal where the
  convention promises "transient, retry it", and `130` was documented nowhere and never
  emitted. (#65)

  The eight: `0` success, `1` a bug in railctl, `2` a malformed command line, `4` a named
  port that is not there, `7` transient - retry may work, `8` partial, `9` the operation
  failed for a real reason, `130` the operator interrupted the run.

  **What `$?` is for now:** the one decision you can make without reading anything. `7`
  means try again; every other non-zero value means it will not help. Which failure it was
  is `error.code` - one value per class, 39 of them, unchanged by this release, and
  `railctl schema` lists every one.

  **Migration.** Replace a test for a specific number with a test on `error.code`. For
  example `[ $? -eq 14 ]` becomes a check that `code` is `cv_verify`; `[ $? -eq 16 ]`
  becomes `pom_read_unsupported`; `[ $? -eq 20 ]` becomes `track_power`. A retry loop that
  keyed on `5`, `12` or `3` keys on `7` alone now, and that is a fix rather than a
  translation: the old `7` was `unsupported_feature`, a permanent refusal that a retry loop
  would have hammered forever.

- **A Ctrl-C now exits 130**, the shell's convention for a run ended by SIGINT, and still
  carries `code: aborted`. It exited 9 before, sharing a code with real failures. All three
  interrupt routes - during parsing, inside a command body, and an `EOFError` reaching
  typer - end the same way, with exactly one envelope on stderr. (#65)

- `railctl doctor` reports a failed probe as exit 9 rather than the old exit 3. What failed
  is in the report, which is this command's whole output. Success and "a capability came
  back unknown" are both still exit 0. (#65)

### Added

- Every command's row in `railctl schema` now carries `error_codes`: the `error.code`
  values that command can produce, alongside the `exit_codes` it can exit with. This is
  where the detail went. Asking "can `restore` hit an index-page refusal?" used to be a
  question about exit 17 and is now a question about `index_page_required` in this list;
  each code's row in the manifest's top-level `error_codes` table gives its exit code and
  whether it is worth retrying. (#65)

## [0.2.0] - 2026-08-22

### Added

- The status byte's bit order is now something the tool measures rather than something it
  asserts. Two manuals disagree about which of bits 0 and 1 is emergency stop and which is
  emergency off; both readings are carried as named data, the order measured on the YD7010
  stays the default, and `railctl doctor --measure-status-bits` (check D13) measures which
  one the attached station actually uses and records it as a capability. The flag is opt-in
  because the measurement holds the whole layout for one telegram and then releases it. No
  reading changes on a station that has not been measured. (#13)
- The `doctor` envelope's `layout` block gains `hold_applied`: whether the run sent an
  emergency stop of its own, so a hold present at the end can be told apart from one the run
  found already there. The human report now says when a run stopped the whole layout and
  released it again, and the "hold not confirmed" warning no longer tells the operator that
  such a run released a hold it found - the hold was its own. (#13)
- A swept backup document now says in the file what the sweep only said to the person
  running it: an optional top-level `caveats` array, each entry `{"code", "message"}`,
  carrying `zero_is_not_proof`. A row that answered `0` may be an implemented CV holding
  zero or a CV the decoder does not implement answering zero, and no read can settle it -
  so the file that is read months later now states that itself rather than leaving it in
  the run's console output. The key is absent from documents that have nothing to say, and
  `restore` and `diff` ignore it, so files written before this change still load and older
  readers still accept files that carry it. (#53)
- The catalog learned the ZIMO smoke generator characteristic: CV137, CV138 and CV139, in
  the `lights` group, named by the driving state the MS manual gives each one -
  `smoke_pwm_standstill`, `smoke_pwm_steady_speed` and `smoke_pwm_acceleration`. The
  CV127-132 effect entries now name the two effect codes (72 for steam, 80 for diesel) and
  the trap behind them: an effect code produces no smoke at all until CV137-139 are given
  values, and they default to 0. A curated backup therefore reads 80 CVs where it read 77,
  and 108 with the speed table.

### Fixed

- Every way of typing the command wrong now answers like everything else. An unknown command,
  an unknown option, a bad option value and a missing argument used to leave Click's own boxed
  text on stderr - escape codes included, even when stderr was a file - so a script doing
  `json.loads(stderr)` on a non-zero exit got a parse error and no `code` to branch on, for the
  two most common script bugs there are. They now publish the same `railctl/error/v1` envelope
  with `"code": "usage"`, and the one runnable suggestion names the level that refused the
  invocation: `railctl cv read --help`, not the root's. (#30)

- The sweep's `sweep.unexercised_range` warning no longer claims more than the bench
  established. It said no value above CV511 had been checked against a known quantity, which
  stopped being true once CV523 was corroborated across two encodings; and it blamed the high
  range for the zero-versus-unimplemented ambiguity, which applies at every CV number and which
  the run already states once. The text now names what was measured and through which opcode,
  and calls the range thinly measured rather than unmeasured. `reason` is the only published
  string that changed. (#52)

- An interrupt is now readable however early it lands. Pressing Ctrl-C while the arguments
  were still being parsed ended the run with nothing at all on stderr - no `code` for a
  script to branch on - while the same key press inside a command published the `aborted`
  envelope. All three routes now publish the same envelope with the same wording and the
  same exit code, so a caller asking "did the operator stop this?" always has a `code` to
  read. This also removes exit 130, a status typer produced that `railctl schema` never
  published. `railctl schema` now publishes exit 9 in its manifest row and on its `--help`
  page, with the meaning it has there: it opens no station, but it can still be stopped by
  the operator, and a published set that left out the one non-zero code it could really
  reach sent callers into their unknown-exit-code branch. (#50)
- `railctl cv read --page` no longer contradicts itself. It asks the operator to approve
  writing CV31/CV32, performs that selection, and then used to report `page.not_selected`
  for the very read the selection was made for. The warning now fires only when nothing
  selected the page it names - a page named for a read that no selection backs. (#39)
- The `61 13` hint no longer sends the operator to the track when the decoder has already
  answered. A CV that fails inside a service session where another CV answered cannot be
  a contact or a programming-current problem, and the hint now says so instead of
  suggesting the wheels be checked. The original advice is kept for the case it was
  written for: the first failure of a session, where nothing has been proven yet. (#46)
- `railctl restore` no longer reports the index page as unselected while verifying a write
  that selected it. Every read-back of a CV above 256 carried that warning - fourteen of
  them in a curated restore - saying the selection had not happened, in the run that made
  it.
- One selected CV31/CV32 page is now recorded under one key on the programming track.
  `PageKey` always documented that service mode is addressed by track and carries no
  locomotive address, but nothing enforced it and the callers disagreed, so the same
  physical selection could land under two keys and neither path saw the other's record.

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
