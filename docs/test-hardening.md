# Test hardening: property testing and mutation testing

**Status:** LIVING. Records which modules are hardened, how to run the tools,
and every surviving mutant that was judged equivalent rather than fixed.

## Why both tools

They answer different questions, and neither answer substitutes for the other.

**Property testing (hypothesis)** asks whether the code obeys its own laws
across a wide range of inputs. The example tests in this repo were written from
telegrams observed on the wire, so they pin what the YD7010 actually did on one
afternoon. A property pins what must hold for every input, which is where the
gaps were: a CV encoding that is correct for CV8 and CV29 and wrong for CV256, a
parser that resyncs correctly when the noise is at the front of the buffer and
not when it is further in.

**Mutation testing (cosmic-ray)** asks whether the tests would notice if the
code changed. It edits the source, runs the suite, and reports every edit the
suite failed to catch. A surviving mutant is a line of code no test constrains.

Coverage answers neither question. Every module here was already at effectively
full line coverage before any of this work started.

## What this is guarding against

This project has one characteristic failure, and it has occurred repeatedly:

> a capability recorded as absent because of a defect in the instrument
> measuring it

The probe reports three answers - yes, no, and not established - and the
dangerous corruption is silent. A reply the parser does not recognise is
indistinguishable from no reply, and no reply is how the probe records "the
hardware cannot do this". Nothing in the output looks wrong. During M1 this
produced four confident, wrong conclusions that reached the design document.

So the properties are weighted towards that failure: a frame that arrived must
come back, a frame that did not arrive must never be invented, and only an
explicit `61 82` rejection may ever produce `False`.

## Running the tools

### Property tests

They are ordinary pytest tests and run with the rest of the suite:

```bash
.venv/bin/python -m pytest -q
```

Three hypothesis profiles are registered in `tests/conftest.py`, selected with
the `HYPOTHESIS_PROFILE` environment variable:

| Profile | Examples | Use |
|---|---|---|
| `default` | 100 | ordinary runs; applies when the variable is unset |
| `mutation` | 25 | mutation runs, where the suite executes once per mutant |
| `ci` | 500 | a deeper sweep than anyone wants to sit through locally |

### Mutation runs

One config per module in `mutation/`. Always run from the repo root.

```bash
.venv/bin/cosmic-ray baseline mutation/frames.toml
.venv/bin/cosmic-ray init     mutation/frames.toml /tmp/cr-frames.sqlite
HYPOTHESIS_PROFILE=mutation .venv/bin/cosmic-ray exec mutation/frames.toml /tmp/cr-frames.sqlite
.venv/bin/cr-rate   /tmp/cr-frames.sqlite    # survival percentage
.venv/bin/cr-report /tmp/cr-frames.sqlite    # per-mutant verdict and diff
```

This is a periodic audit, not a CI gate. A full run takes minutes per module and
leaves behind pinning tests, which are the durable part.

### Two rules that cost real time when broken

**Never run anything else against the working tree during `exec`.** cosmic-ray
mutates the source file IN PLACE and reverts it after each mutant. A `pytest`
started in parallel imports whatever happens to be on disk at that moment. It
cost one hung test run here, against a mutant that had removed a loop's
increment.

For the same reason, never run two mutation sessions at once, even on different
modules: the whole suite imports every module, so one session's mutant lands
inside the other session's measurement.

**Never kill `exec` with an external `timeout` or SIGTERM.** A hard kill
mid-mutant leaves the source mutated. Recover with:

```bash
git checkout -- tools/probe/<module>.py
```

To bound a run, narrow the config instead.

## Reading a survivor

Survivors fall into four kinds, and only the first is a test gap.

**Killable** - the suite genuinely does not constrain this line. Write a pinning
test. Verify it fails against the mutant and passes without it; a pinning test
that was never seen red is pinning nothing.

**Equivalent** - the mutated program cannot behave differently from the
original. No test can kill it, and writing one that appears to usually means
asserting an implementation detail. Document the argument.

**Unreachable** - the mutated line can differ in principle, but its callers
guarantee the difference never arises. Document the guarantee, because it is
exactly the kind of thing a later refactor breaks.

**Low value** - killable, but only by pinning an arbitrary detail that no
consumer depends on. Record the decision rather than the test.

In this codebase one equivalent class dominates and is worth recognising on
sight: every module starts with `from __future__ import annotations`, so
annotations are never evaluated, and every mutation of a `|` inside one
(`tuple[Frame, int] | str` becoming `... + str`, `... & str`, and so on) is dead
code by construction. These account for most of the survivor count in every
module and none of the risk.

## What the run found

Two defects in shipped code, both invisible to the 139 tests that existed
before, and both of the family this project keeps producing.

**A capability value of `0` rendered as the word "no".** `report.py` chose the
Result column with `_WORDS.get(value)` on a table keyed `True`/`False`/`None`.
Python hashes `0` equal to `False` and `1` equal to `True`, so an integer 0 came
back out as a verdict. CV265 reads 0 on the decoder this probe was built for, so
the case is ordinary. The JSON report of the same run said `0` correctly, which
means the human and machine outputs disagreed about the same measurement. Found
by a property test; the example tests never handed a scalar to `to_markdown`.

**`POLL_INTERVAL = 0.25` was dead.** No code has ever read it. Its presence
implied the service-mode poll spaces its attempts, which it does not - and the
spacing of service-mode reads was one of the things M1 got wrong and had to
correct on hardware. A constant that states a false timing claim is worse than
no constant.

A third finding is a gap rather than a defect: `DECODER_TYPES` maps CV250 values
to ZIMO model names and has no consumer. Its 17 surviving mutants are all
"nothing depends on this". The design gives it one in M2, as part of the restore
identity gate, so it is left in place rather than deleted - but until then no
test can protect it.

## Completed runs

Measured 2026-08-04. "Adjusted" excludes the annotation `|` mutants, which are
unreachable by construction and which no test will ever kill; it is the number
that says something about the tests.

| Module | Mutants | Killed before | Killed after | Adjusted | Survivors left |
|---|---|---|---|---|---|
| `frames.py` | 294 | 82.0% | **83.3%** | 90.1% | 22 annotation + 27 |
| `commands.py` | 417 | 96.2% | **96.4%** | 96.9% | 2 annotation + 13 |
| `replies.py` | 583 | 88.0% | **96.2%** | 98.1% | 11 annotation + 11 |
| `report.py` | 55 | 85.5% | **89.1%** | 89.1% | 6 |
| `checks.py` | 462 | 56.2% | **66.9%** | 85.1% | 99 annotation + 54 |

`checks.py` is the module to read carefully. Its headline number is the worst of
the five and its adjusted number is respectable, because 99 of its 153 survivors
are annotations and another 24 are timing values this test seam cannot observe
at all (see below). The gap between 66.9% and 85.1% is not a rounding
convenience - it is the difference between counting mutants and counting risk.

### `frames.py`

The LI-USB framing layer: XOR checksums, telegram lengths, and the resync that
recovers the stream after noise. A frame lost here is a reply the probe never
sees, which is the difference between "unsupported" and "not established".

Run of 2026-08-04: **294 mutants, 241 killed, 53 survived (82.0% baseline)**.

Four survivors were killable, and all four are pinned in
`tests/test_frames_mutation_hardening.py`, each verified red against its mutant:

- `@dataclass(frozen=True)` on `Frame`. Frames are the audit trail hex-dumped
  into the report to justify a verdict; a mutable one lets a later stage rewrite
  the evidence for an earlier one.
- `self.prefix == LI_COMMAND` weakened to `>=` in `Frame.solicited`. Only two
  prefixes exist and they happen to be ordered so the two operators agree, so
  nothing in the suite noticed. Pinned with a third prefix value.
- `_salvage(buffer, pos + 1)` changed to `pos << 1`. **The most serious
  survivor.** Every existing test put its noise at the front of the buffer,
  where doubling a small offset lands close enough to still find the frame. With
  the stray prefix further in, the doubled offset jumps past the real frame and
  `split_frames` returns nothing at all - the exact regression this parser was
  rewritten to fix, reachable again through a different edit.
- `continue` changed to `break` after a salvaged frame, which drops every
  further telegram in the same read window. A probe run regularly collects a
  solicited reply and a broadcast together.

The remaining 49 survivors:

- **22 annotation mutants** on the `-> tuple[Frame, int] | str` and
  `-> tuple[Frame, int] | None` return types of `_frame_at` and `_salvage`. Dead
  under `from __future__ import annotations`.
- **8 mutants on the `_salvage` scan bound** `range(start, len(buffer) - 1)`.
  A frame needs a 2-byte prefix plus a 2-byte minimum telegram, so no frame can
  begin later than `len(buffer) - 4`. Widening the bound only adds iterations
  that read a short slice and reject it; narrowing it by one removes a position
  where no frame could have started. Equivalent in both directions.
- **8 of the 9 mutants on `_salvage(buffer, pos + 1)`** - every one except
  `pos << 1`. `pos` is a confirmed prefix start, so `buffer[pos + 1]` is `0xFE`
  or `0xFD`, and a prefix begins with `0xFF`. No frame can start at `pos + 1`,
  so starting the scan at `pos`, `pos + 1` or `pos + 2` finds the same frame.
- **3 mutants on `if pos + 2 >= len(buffer)`** (`>=` to `==`, `>=` to `is`, and
  `+ 2` to `+ 3`). Both callers bound `pos` so that `pos + 2 <= len(buffer)`,
  which makes `>=` and `==` coincide. Verified rather than argued: instrumenting
  `_frame_at` across 37116 calls over buffers built from real frames, stray
  prefixes and noise produced **zero** calls where `pos + 2 > len(buffer)`. The
  guard is a redundant early-out - when it does not fire, the `end >
  len(buffer)` check below returns `INCOMPLETE` anyway. Unreachable rather than
  equivalent: a caller that stopped bounding `pos` would make this live again.
- **`pos += 1` becoming `pos += 2` in the corrupt branch.** Same argument as
  `_salvage`: the skipped position starts with `0xFE` or `0xFD` and can never
  begin a prefix.
- **`pos = 0` becoming `pos = -1`.** The first iteration reads a slice that
  matches no prefix and advances to 0. One wasted iteration, same output.
- **2 identity mutants** (`found == CORRUPT` to `found is CORRUPT`, same for
  `INCOMPLETE`). Both constants are module-level strings returned by identity
  from `_frame_at`, so the operators agree. Arguably `is` is what the code means.
- **1 identity mutant** on `xor(telegram[:-1]) != telegram[-1]`. Both operands
  are ints in 0..255, which CPython interns, so `is not` and `!=` agree. This
  one is equivalent only because of an interpreter implementation detail, and it
  is the survivor in this list most worth revisiting if the checksum ever widens
  beyond a byte.
- **1 low-value mutant**: `while pos < len(buffer) - 1` becoming `- 2`. The loop
  stops one position earlier, where no frame could start, so no frame is lost.
  The only observable difference is that one extra byte of trailing noise stays
  in the remainder instead of being discarded, and the caller re-scans it on the
  next read. Not pinned: the amount of noise discarded is not a contract.

### `commands.py`

The payload builders, and the module whose own docstring calls the non-uniform
CV encoding "the single most dangerous detail". An off-by-one here reads the
wrong CV and reports the value under the right name.

Run of 2026-08-04: **417 mutants, 401 killed, 16 survived (96.2% baseline)** -
the strongest starting position of the five, because the property tests decode
every builder's output back through its own documented convention for all of
CV1-1024.

Four survivors were killable, all of them exact boundaries:

- `1 <= address <= 9999` weakened to `<= 10000`
- `0 <= index <= 28` weakened to `-1 <=`, and to `< 28`

The property tests already sweep those ranges. That is not the same as testing
the endpoint: asking for 25 draws from `integers(-100, 20000)` and hoping one is
exactly 10000 is hope, not coverage. Hypothesis finds the shape of a bug; it
does not promise to visit a named constant. `tests/test_commands_mutation_hardening.py`
names them instead, both sides of every range.

The remaining 12 are equivalent, and all for the same structural reason: **the
operands occupy disjoint bits**, so `+`, `|` and `^` compute the same number.

- `address + 0xC000` - an address is at most 9999, which fits in 14 bits, and
  the marker occupies the top 2.
- `0xE4 | ((wire >> 8) & 0x03)` - the option byte's low 2 bits are clear.
- `0x18 + (cv >> 8)` - the band index is 0..3 and the opcode's low 2 bits are clear.
- `(action << 6) | index` - the index is 0..28, below the action's shift.

Two more are unreachable rather than equivalent, and both are worth knowing
about because a refactor could make them live:

- `if cv == 256:` weakened to `>=`, inside `service_direct_read`. The guard two
  lines above has already rejected everything past 256.
- `if not 1 <= cv < MAX_CV:` weakened to `<=`, inside `service_ext_read`. CV1024
  returns earlier, so control never reaches this line with that value.

The last is `(value >> 8) & 0xFF` becoming `% 0xFF`. The high byte of a
locomotive address never reaches 255, so the two agree. Note that the *same*
mutation on the low byte of the same line WAS killed, because a low byte of 255
distinguishes them immediately.

Two mutants replace the keyword-only marker `*` in a signature with `/`. Callers
that pass keywords behave identically, and every caller does. Not pinned.

### `replies.py`

The parser, and the module where this project's characteristic failure is
manufactured: a reply form that is not recognised is indistinguishable from no
reply, and no reply is how the probe records a missing capability.

Run of 2026-08-04: **583 mutants, 513 killed, 70 survived (88.0% baseline)**.

The killable survivors fell into three groups.

**Header dispatch (about 25 mutants).** Comparisons like `header == 0x62`
weakened to `>=` or `<=`, so that a telegram with an unrelated header would be
parsed as a `Status`, a `Version`, or a marker. Each misbehaves for only a few
specific byte pairs, which is why a property test drawing random binary never
caught them: it can sweep the space but cannot promise to visit `(0x62, 0x22)`.
`test_the_dispatch_table_matches_the_protocol_documents` walks **all 65536**
header/db0 combinations against a table written from the protocol documents
rather than from the parser, and its converse asserts that everything
undocumented lands on `Unknown`. Exhaustiveness is affordable here and removes
the question entirely.

**Nine frozen dataclasses.** `frozen=True` flipped to `False` on every reply
type. Parsed replies are the evidence a verdict rests on and are hex-dumped into
the report as its audit trail; one that can be edited after parsing lets a later
stage rewrite what an earlier one saw.

**The F0 mask.** `telegram[3] & 0x10` widened to `& 0x11`, which makes F1 read as
the headlight. The probe re-asserts F0 to the value it just read, so this one
ends with the probe changing a layout it promised to leave alone. Pinned with the
byte pair that separates them (`0x01`: bit 0 set, bit 4 clear).

The equivalent survivors:

- **11 annotation mutants** on `-> int | None`, dead under
  `from __future__ import annotations`.
- **`bool(self.raw & 0x80)` becoming `// 0x80`.** For a byte, `raw // 128` is 1
  exactly when bit 7 is set. Genuinely the same function.
- **`(telegram[2] << 8) | telegram[3]` and `base + telegram[2]`** becoming `+`,
  `|` or `^` - disjoint bits again. The band bases 256, 512 and 768 all have a
  clear low byte.
- **The length guards** (`len(telegram) >= 4` becoming `>= 5`, `<= 4`, `== 5`).
  Every telegram reaching `parse` came from `split_frames`, which sizes it from
  the header's low nibble, so the length is always exactly what the form
  requires. These can only differ on input the real path cannot produce.

### `report.py`

Run of 2026-08-04: **55 mutants, 47 killed, 8 survived (85.5% baseline)**.

One killable survivor, and it is the most instructive result of the whole
exercise: **`if value is True:` weakened to `if value == True:`**.

That edit is the bug this run had already found and the fix had already
corrected. A property test written to prevent its return draws integers from
-1000..1000 and asserts none of them render as a verdict word. The mutant
survived it, because the edit misbehaves for exactly two values in that range -
0 and 1 - and no sample of a thousand-wide range is obliged to contain them.

The two integers are the entire bug. They are now named, not sampled for, in
`tests/test_report_mutation_hardening.py`. The lesson generalises: a property
test states the law, and a pinning test names the point where the law is
load-bearing. Neither replaces the other.

`sort_keys=False` flipped to `True` is now pinned as well - the capability order
follows the order the checks ran, which is the order the report reads in.

The rest: two keyword-only marker mutants, and four `indent=2` variations. JSON
indentation is not a contract; not pinned.

### `checks.py`

The verdict logic. Run of 2026-08-04: **464 mutants, 261 killed, 203 survived
(56.3% baseline)** - much the weakest of the five, and for one structural reason
worth stating plainly.

**`FakeLink` answers a payload with the same bytes every time.** A station that
returns nothing to a read and produces the value only when asked again with
`21 10` cannot be expressed with it. That is precisely the behaviour the polling
loop in `_read_value` exists for, so every mutant inside the loop survived -
including `ZeroIterationForLoop`, which deletes the loop outright.

That mutant is the largest error of M1 restored. The absent `21 10` poll made
the whole Lenz opcode family read as silent, which was written up as "this
station does not implement them", and produced two further wrong conclusions on
top: an invented 4-5 second spacing requirement and a 6-7 minute backup estimate
for work that takes 2 minutes. The suite could not have noticed it coming back.

`SequencedLink` in `tests/test_checks_mutation_hardening.py` gives each payload a
queue of replies consumed one exchange at a time. With it, the poll loop is now
pinned on five axes: a value that arrives only after a poll is read; polled
frames are appended to the read's rather than replacing them; a value arriving
on the last allowed poll is still caught by the classification after the loop;
a station that never answers is given up on after exactly four polls; and
polling stops as soon as the station goes quiet.

Other killable survivors now pinned:

- **`action = 1 if f0_is_on else 0` with the 1 mutated.** Action 2 is *toggle*.
  A probe that toggled would switch off the headlight of whatever locomotive it
  was pointed at, while reporting that single-function commands work. R5 is only
  side-effect free because it re-asserts the value it just read.
- **`reply.raw_cv == wire` weakened to `is`.** CPython caches small integers, so
  `==` and `is` agree for every CV the tests happened to use. Both operands are
  computed at runtime - `wire` from `cv - 1`, `raw_cv` from `(hi << 8) | lo` -
  so once past the cache they are distinct objects and `is` returns False for
  equal numbers. Measured, with runtime values rather than literals (the
  compiler folds those and hides the effect):

  | CV | wire | `==` | `is` |
  |---|---|---|---|
  | 8 | 7 | True | True |
  | 256 | 255 | True | True |
  | **265** | **264** | **True** | **False** |
  | 1024 | 1023 | True | False |

  The break is at CV258, and `HIGH_BAND_CV` - the CV this probe reads to decide
  whether the bands above 255 work at all - is **265**. So the mutant does not
  fail on some exotic CV: it fails on the first high CV the probe touches, and
  the ZIMO CVs railctl must back up live at 265 and above.
- **`outcome.reply_cv != cv` weakened to `>`.** Accepts every reply that decodes
  to a *lower* CV than the one requested - publishing one CV's value under
  another CV's name.
- **The three unresolved explanations.** Silence, a transient condition and a
  Register/Paged fallback all produce `None`, so mutations that swap one
  explanation for another changed nothing any test asserted. The verdict would
  stay right while the sentence under it became a fabrication about the
  hardware. Now pinned separately.
- **The status flags and the automatic-start warning.** `status.emergency_off if
  status else None` with the condition negated silently reports every flag as
  unknown; `if status and status.auto_start_mode` weakened to `or` warns about
  automatic start mode when the bit is clear, which teaches the operator to
  ignore the warning.
- **The address divergence band.** Both endpoints (100 and 127), both sides of
  it (99 and 128), the mirror case where only the short form answers, both
  threshold values, and the requirement that the two telegrams actually differ.

Equivalent or unreachable, in bulk:

- **99 annotation mutants** across eight signatures.
- **About 24 timing mutants** - `POM_WINDOW`, `SERVICE_WINDOW`,
  `SERVICE_POLL_WINDOW` and every literal `window=` argument. `FakeLink` and
  `SequencedLink` both ignore the window entirely, so **no in-process test can
  constrain these values at all.** They were measured on hardware and are
  protected by hardware runs, not by this suite. This is a real limit of the
  seam, stated here rather than hidden inside a survival percentage.
- **The `_verdict` status comparisons** (`==` to `>=`, `!=` to `>`). The five
  status strings happen to sort so that these operators agree with equality.
  This is the most fragile equivalence in the codebase: adding a status like
  `"aborted"` would silently make several of them live. Worth an enum if the set
  ever grows.
- **17 mutants inside `DECODER_TYPES`**, which has no consumer yet (see above).
- **`if g4 is False or g5 is False:` with `is` weakened to `==`.** The operands
  are `bool | None` and never integers, so the two agree.

## Two things that made the measurement itself unreliable

Both cost real time, and both are the same mistake this project keeps making in
a new costume: trusting an instrument without checking it.

**Random property tests make mutation scores non-reproducible.** Whether a
marginal mutant dies depends on whether that run happened to draw the input
exposing it. Two `frames.py` mutants (`_salvage(buffer, pos % 1)` and `pos & 1`)
were killed in the first run, survived the second, and were killed again in the
third - with the source unchanged between the last two. Consecutive runs of
identical code produced different scores, so "the number went up" meant nothing.
The `mutation` profile now sets `derandomize=True`. The default profile stays
random, because there the randomness is the whole point.

**Comparing survivor lists by line number is wrong after any edit.** Deleting
`POLL_INTERVAL` shifted every later line in `checks.py` by one, and a naive
before/after diff then reported dozens of spurious kills and resurrections -
including "kills" of `SERVICE_WINDOW` mutants that no test can possibly observe.
That impossibility is what exposed the error. Compare survivors by operator and
diff text, never by position.

Two of the pinned mutants are killed by **hanging** rather than by failing:
`pos % 1` and `pos & 1` make the salvage scan restart forever. cosmic-ray's
60-second timeout catches them, so those two mutants cost a minute each. A
`pytest` run against them never returns, which is worth knowing before verifying
one by hand.

## Where the numbers stand

The percentages are honest but should be read with the equivalence classes in
mind. A third of most modules' mutant population is annotation `|` swaps that no
test can ever kill, and in `checks.py` a further 24 are timing values this seam
cannot see. A module at 83% with its whole equivalent-mutant population read and
argued is in better shape than one at 95% whose survivors nobody opened.

## What is deliberately not covered

Stated plainly, because a survival percentage invites the reader to assume the
rest is safe:

- **Every timing value.** `POM_WINDOW`, `SERVICE_WINDOW`, `SERVICE_POLL_WINDOW`
  and each literal `window=` argument. Both test doubles ignore the window, so
  these are protected by hardware runs alone. Changing one and running the suite
  proves nothing.
- **`link.py`.** It opens a file descriptor and talks to a serial port; there is
  no mutation config for it. Its known weakness - `collect` waits out the whole
  window instead of returning once a reply has arrived, costing roughly six
  times on a full backup - is recorded against M2, not here.
- **`DECODER_TYPES`.** A data table with no consumer until the M2 identity gate.
- **JSON indentation** and the exact quantity of noise `split_frames` discards.
  Neither is a contract.
- **Markdown escaping in `_word`.** Its fallback is `str(value)`, so a capability
  value containing `|` or a newline would break the table row it is rendered in.
  Nothing reachable today produces one - the only scalars the checks publish are
  integers, booleans and `None` - so no escaping was added rather than guess at
  a rule for a case that does not exist. The property test restricts its
  alphabet for the same reason and says so. A check that starts publishing free
  text needs this revisited.

## When to run this again

Before relying on a module for something new, and after any significant refactor
of one. Two specific triggers are already known:

- **Adding a status string to `_verdict`.** Several survivors there are
  equivalent only because the five current strings happen to sort so that `>=`
  agrees with `==`. A sixth would silently make them live.
- **Widening the XOR checksum past one byte.** One survivor is equivalent only
  because CPython interns integers up to 256.
