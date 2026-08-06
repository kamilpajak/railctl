# YD7010 capability probe — hardware results

- Command station: YaMoRC YD7010, XpressNet **4.0**, command station id **0x12** (Z21 family)
- Port: `/dev/cu.usbmodem7010A00011943` (LI-USB `FF FE` framing required)
- Decoder: ZIMO MS450P22, address 3. **On the main track** for the R1 / POM section; moved
  to the **programming track** for the R2 / R4 service-mode section below.
- Run: 2026-08-04; power-state section added 2026-08-05

## Settled

| Question | Answer | Evidence |
| --- | --- | --- |
| Port map | `…41` LocoNet (silent), `…43` XpressNet, `…45` YD.Control telemetry | passive listen |
| Identity | XpressNet 4.0, id `0x12` | `63 21 40 12` |
| R5 — single function `E4 F8` | **works** | `E4 F8 00 03 40` accepted, headlight lit |
| Function groups 4 and 5 (F13–F28) | **work** | `E4 23` / `E4 28` accepted |
| Speed step mode | **128** | ident byte `0000 B100` |
| F13–F28 state readable | **yes**, `E3 09` → `E3 52 D1 D2` | closes the blind-clear side effect |
| Start mode | **automatic** — locos resume last speed on power-up | status bit 2 |
| Status byte bits 0 and 1 | **swapped vs Lenz** — bit 0 emergency stop, bit 1 emergency off | `62 22 05` after `80 80` |
| Service mode, all three encodings | **work** — direct, Z21 and extended | doctor D5/D6/D7, 2026-08-06 |
| Service mode with the track **unpowered** | **works** — the read needs no track power | 4 reads of CV8, 2026-08-06 |
| Decoder identity | ZIMO **MS**: `CV1=3 CV7=5 CV8=145 CV250=6 CV28=3 CV29=14` | doctor D9, 2026-08-06 |
| Decoder firmware | **5.15** — `CV7=5` main, `CV65=15` sub-version | service read, 2026-08-06 |
| RailCom **in the decoder** | **enabled** — `CV29` bit 3 set, `CV28=3` | doctor D8, 2026-08-06 |
| Broadcasts | `61 00`, `61 01`, `81 00` arrive **unsolicited**, three times each | `Station.events()`, 2026-08-06 |

The speed step question was one of the five left open in the design. It is answered:
this locomotive runs 128 steps.

## Status byte: bits 0 and 1 are the reverse of the Lenz spec — SETTLED 2026-08-05

The `62 22 S` status byte does not follow Lenz XpressNet 2.1.7 on this station. Measured with
the locomotive on the rollers and the owner reading the front-panel Track Out LED, whose three
states the YD7010 manual defines: green steady = voltage on, green **flashing** = emergency stop
*"(track voltage ON)"*, red steady = voltage off.

| sent | reply | bit set | LED | track voltage |
| --- | --- | --- | --- | --- |
| `21 81` | `62 22 04` | none | green steady | **on** |
| `80 80` | `62 22 05` | bit 0 | green **flashing** | **on** |
| `21 80` | `62 22 06` | bit 1 | red | **off** |
| `80 80` then `21 80` | `62 22 07` | both | red | off |

The state after plugging the USB in is **not a constant**. An earlier note here implied `62 22 07`
was the power-up value; measured on 2026-08-06, the station comes back in whatever state it was
left in — unplugged at `62 22 06`, it reappeared at `62 22 06`. The earlier `0x07` was a latched
emergency stop from before the disconnection, not a power-up default.

So **bit 0 is emergency stop and bit 1 is emergency off** — the order the German 23151 manual
gives, not the one in Lenz 2.1.7.

### Why the earlier runs could not have caught it

The two states this bench sits in almost all the time are `0x04` and `0x07`. Neither
distinguishes the orders: `0x04` has neither bit and `0x07` has both. Only a state with exactly
one of them decides, and the way to reach one on purpose is `80 80` — which the Lenz spec itself
makes decisive, because 2.2.4 states *"The DCC track power remains switched on"*. The command
whose effect the spec pins down is the one worth sending when two documents disagree.

### What it broke

`track_power` was `not emergency_off`, so `0x06` read as **powered**. Consequences:

- `power_off()` cut the voltage and then always raised `TrackPowerError`. The command worked;
  the check did not.
- Doctor D3 would have read a dead track as "already on", skipped the power-up, and run D4 (POM
  read) and D10 (address band) on an unpowered track — and the doctor is what writes
  `capabilities.json`.

Three M5 acceptance runs missed it because the test only called `power_off()` when it found the
power already off. It found it on, took the "leave as found" branch, and the power-off path had
never executed on hardware. The test now calls it unconditionally and restores the found state
afterwards.

### What another implementation does

JMRI decodes this byte in the **Lenz order** — `java/src/jmri/jmrix/lenz/XNetPowerManager.java`
reads bit 0 as emergency off and bit 1 as emergency stop — with no per-station override anywhere.
On this hardware that inverts both states: at `62 22 06`, where the red LED says the track is
dead, JMRI would report `IDLE`, its state meaning "track power is ON, but all locomotives are
stopped"; at `62 22 05`, where the track is live, it would report `OFF`.

That is not evidence against the measurement — JMRI follows the same specification we followed,
and nothing suggests anyone has run it against a YD7010. It is worth recording for the opposite
reason: a reader who checks our claim against the best-known open implementation will find it
disagrees, and should know that was checked rather than missed.

### Second finding from the same run

`21 80` and `21 81` are both answered with the generic ack `01 04 05`, never `61 00` / `61 01`.
Those arrive as unsolicited broadcasts instead. The fast path in `Station._settle_power` therefore
never fires here, and both calls always pay `power_settle` plus a status round trip.

## Session of 2026-08-06 — what the bench added

Run through `Station` and `doctor`, not through the M1 probe script, so this is the first time
most of these paths reached real hardware.

### RailCom is enabled in the decoder — the missing piece is the detector

D8 read `CV29 = 14`, so bit 3 is set, and `CV28 = 3`, a valid channel selection. The decoder is
configured for RailCom and always was. This removes the last competing explanation for the silent
POM read: it is not decoder configuration, and it is not the decoder being asleep. It is that
nothing receives what the decoder transmits, exactly as the standards reading predicted.

Consequence for the code: the doctor's silence note ends with "Fix RailCom on the decoder and
re-run the doctor", which now sends the user to the one place that is already correct.

### POM read: silence is the rule, with one unexplained exception

28 controlled POM reads in this session, all silent, each costing **6.7 s** — three internal
attempts. That figure is the concrete cost behind the plan's "AUTO would retry POM for several
seconds forever"; it was assumed until now.

One doctor run returned `61 13` (no acknowledgement) instead. Four hypotheses were tested and all
four failed to reproduce it:

| hypothesis | attempts | result |
| --- | --- | --- |
| intermittent / random | 15 | silence |
| first read after the track is energised (1 s gap) | 6 | silence |
| cold decoder start (30 s with the track dark) | 4 | silence |
| freshly re-seated wheels after lifting the locomotive | 3 | silence |

It is recorded as a single observation with no explanation. Do not repair this gap with a story:
three explanations were proposed during the session and the measurement refuted each one.

What it does settle is the design question. `pom_read = false` from silence is well supported for
this hardware — but one contrary observation exists, so it is not certain, which is precisely what
`pom_read_provenance = "silence"` is for.

### The telemetry stream is not an instrument for track power

Interface 5 carries a `TV` field that looks like track voltage. Sampled with the track live
(`62 22 04`) and dead (`62 22 06`), it read **15.1 V in both**, alongside `TC 0mA` for a decoder
that is demonstrably drawing current and answering. Whatever `TV` measures, it is not the state of
the track output.

Recorded because the negative result is the useful part: anyone who sees `TV` in the output will
try this, and issue #13 needs an instrument independent of the status byte. This is not one.

Re-measured later the same day, with the toggle done under observation rather than between two
runs: `TV` held **15.1 V** across `power_off` → `power_on` → `power_off`, and `TC` held `0 mA`.
Same verdict, now with the transition itself watched. A second disqualifier surfaced: the `[CS0]`
line arrives about **once every 5 s**, so even a `TV` that did track the output would step over a
service-mode window of ~2 s more often than it landed in one.

**A multimeter on the main track supersedes the LED** as the independent instrument — see
"Service mode neither needs track power nor disturbs it" below for the validation and the
settings that matter.

### The programming-track output latches off after an overload, and nothing reports it

Driving a locomotive on the programming track knocks that output offline until the load is
physically removed. No status bit, no telemetry field and no front-panel LED shows it. Established
by a controlled sequence with one variable, reproduced three times.

```
baseline           service read CV8 = 145, repeatedly
drive step 30      sent to the locomotive ON the programming track
                   (first occurrence: it moved for ~1 s and stopped by itself)
after              service read -> 61 13, three times in a row
```

What the station said while the fault was present: status byte `62 22 04` — **identical** to before,
telemetry `TC 0mA TV 15.1V` — identical, green Track Out LED steady, red LED off. Every indicator
we have describes the **main** output; there is none for the programming output.

### How it was pinned down

The decisive step was realising the decoder had no power at all, rather than a broken
acknowledgement:

1. Cutting main track power for 3 s and then 15 s did **not** clear the fault.
2. With the fault present, `function_set(3, 0, True)` produced **no light**, though the same
   locomotive had been driven from this output minutes earlier — so operating commands do reach it
   normally, and now nothing did.
3. The locomotive was lifted and put back, with **no command sent**. The light came on by itself,
   because the station still held `F0 = true` and the decoder finally had power to act on it.
4. Service reads worked again immediately: `CV8 = 145`.

Removing the load is what clears it. That is the behaviour a current-limited programming output is
designed to have — it refuses to keep driving into what looks like a fault — and it is invisible to
every channel this tool can read.

Stated precisely: removing the load **is** a way out, and cutting main track power is **not**. Both
were tested. What was not tested is whether anything else clears it — a full power cycle of the
command station itself, or some vendor command outside XpressNet. So read this as "the one recovery
we found", not "the only one that exists". The internal mechanism holding the latch is likewise not
established; what is established is the behaviour and which of the two interventions works.

### What this rules out

**Not a decoder state.** ZIMO's own figures settle it: stay-alive discharge is 1.2 s to 3.1 s at
75 mA (MS manual, technical data), so the decoder was fully unpowered long before the 15 s test
ended and would have restarted. And a decoder holding a latched motor cut-out would still light its
functions; this one lit nothing.

**Not contact resistance.** The locomotive was not touched between the working read and the failing
one. An earlier reading of this session blamed contact, on the grounds that lifting the locomotive
fixed it — but lifting removes the load as well, and the load is what matters.

### Why it matters here

This is what `doctor --power-on` walked into. With the station in automatic start mode and a stored
speed for the address, D3 energised the track, the locomotive drove on the programming track, the
programming output latched off, and every service-mode check then failed and would have been
recorded as "this station cannot do service mode". See #14 — the movement is the trigger, but the
latched output is the mechanism, and it explains why the run could not be rescued by retrying.

### Faults found in this tool, not in the station

- The doctor can start a locomotive moving, including on the programming track, where it then
  fails its own service-mode measurements (#14).
- Nothing the doctor measures is persisted, so a later process finds every encoding "unknown" and
  is told to run the doctor (#15).
- `CvOutOfRangeError` is raised when no encoding has been probed, naming the wrong cause (#16).

### Confirmed by watching the locomotive

`80 80` sent to a locomotive running at step 30 stopped the wheels **instantly**, with the green
Track Out LED flashing throughout — the manual's "emergency stop has been triggered (track voltage
ON)". The status byte read `62 22 05` for ten seconds. This was predicted before the panel was
read, not explained afterwards, and it is the fifth independent confirmation of the bit order.

In the same moment `loco_info` reported `speed=30, emergency_stopped=False` for a locomotive that
was standing still under a global emergency stop. The per-locomotive view does not reflect the
station-wide state.

## Service mode neither needs track power nor disturbs it — SETTLED 2026-08-06

`doctor.py` refuses to run D5–D8 unless the main track is powered, and the comment on that gate
gives two reasons: that entering service mode cuts main power, and that leaving it re-energises
the main track even when the operator never authorised power. **Both are contradicted by
measurement on this station.** Neither had ever been measured; both were reasoning written into a
comment and then read back out as fact.

### The instrument, validated first

A multimeter across the main track rails, on **AC volts**. Validated against a transition whose
state the station reports, before being used to judge anything:

| Station reports | Meter |
| --- | --- |
| `track_power=False` | **0.6 V** |
| `track_power=True` | **16 V** |

Three settings decide whether it answers or misleads:

- **AC, never DC.** DCC is a symmetric bipolar square wave, so its mean is about zero. On DC the
  meter reads ~0 V whether the track is live or dead — a confident wrong answer.
- **Never the current range.** An ammeter across the rails is a short.
- **Presence/absence only.** DCC runs at 5–8 kHz and a typical meter is calibrated for a 50 Hz
  sine, so the number is not the track voltage. The 16 V against 0.6 V is what carries meaning.

### What was measured

| Question | Result | Evidence |
| --- | --- | --- |
| Does a service read work with the main track unpowered? | **Yes** | `CV8=145`, 4 reads out of 4 |
| Does service mode cut a powered main track? | **No** | 16 service operations over two runs, meter never left 16 V |
| Does leaving service mode energise an unpowered main track? | **No** | 16 V never appeared in any of the 4 reads |
| Does a relay switch during service mode? | **Yes, twice per read** | 6 audible clicks for 3 reads, in both power states |

The relay clicks in **both** power states, so it tracks service-mode entry and exit, not the power
commands. An earlier run appeared to contradict this; it did not — that run asked the operator to
*watch* the meter, and nobody was listening for clicks.

With the track unpowered, the meter alternates between 0.6 V and 0.0 V in step with the relay.
Both readings are "no track voltage": the track is empty and the meter's input is high impedance,
so this is leakage following the relay position, not a state of the track. It does not survive
the next `power_on`, which restores 16 V from either reading. Separating the two would need a
load or a scope, and no conclusion here rests on the difference.

### What another implementation does

JMRI does not model any of it (`java/src/jmri/jmrix/lenz/`):

- `XNetTrafficController.enterProgMode()` returns **null**, with the comment "This method has to
  be available, even though it doesn't do anything on Lenz". Nothing is sent to enter service
  mode; the station enters it on the first programming command.
- `enterNormalMode()` returns `XNetMessage.getExitProgModeMsg()`, which is a bare
  `CS_REQUEST` + `RESUME_OPS` (`21 81`). No power state is saved and none is restored.
- `XNetProgrammer.java` contains **no reference to a PowerManager**. There is no gate on track
  power anywhere in the programming path.

### What this costs us today

Reading `CV65` needed the main track energised for no reason other than our own gate — with a
locomotive standing on the main track, that is a real hazard accepted in exchange for nothing.
The gate also makes every fresh process pay for a full probe before it may read one CV, because
capabilities are not persisted (issue #15).

The gate itself is issue #20.

Left open: why the station's relay switches at all, and what the 0.6 V / 0.0 V pair physically
represents. Neither affects any decision about this code.

## R1 — POM CV read: NOT established

> **Added 2026-08-05, from the specification rather than the bench, and it outranks every
> hypothesis below.** The XpressNet specification marks POM CV **read** as a *(future feature)*
> and specifies only byte-mode and bit-mode POM **writes**. So a station that acknowledges the
> request and then says nothing is not necessarily broken or incomplete — it is answering a
> request the protocol never finished defining. This explains the measured asymmetry (write works,
> read is silent) more simply than the RailCom-detector inference further down, and it was missed
> for two days because the investigation started at the hardware instead of at the document.
>
> A capability the protocol does not define is not a capability the station refused. That reasoning
> still stands, and it is why the value written here is not an ordinary `false`.
>
> **Superseded in part, 2026-08-06.** This paragraph originally read "`pom_read` stays **unknown**,
> never `false`", and that is no longer what the code does. Doctor D4 records `pom_read = false`
> after total silence, as the one deliberate exception in the codebase, because leaving it `None`
> makes every `AUTO` operation retry POM — measured at **6.7 s per call** — on every call, forever.
> The exception carries its own provenance: `pom_read_provenance` is `"unsupported"` for a real
> `61 82` and `"silence"` for this case, so the distinction the sentence was protecting survives in
> the type rather than in prose. Anything that must not act on a guess reads the provenance, not
> `pom_read`.

The station **acknowledges** the request and returns **no result at all**.

```
sent      FF FE E6 30 00 03 E4 07 00 36     POM read CV8 @ addr 3
received  FF FE 01 04 05                    interface ACK, nothing else
```

Repeated for CV1, CV8 and CV29, each with an 8 s window plus a `21 10` poll, and once
with a 30 s raw capture. Only the ACK, every time. No `63 14`, no `64 14`, no `61 13`
(no-ack), no `61 82` (not supported), no broadcast.

The decoder was **proven live** during these attempts: `E4 F8 00 03 40` switched the
headlight on, confirmed visually, and `E4 F8 00 03 00` switched it back off.

This is recorded as `unknown`, not `false`. XpressNet section 2.2.23 specifies that a
station which does not support Operations Mode Programming answers `61 82`, and Lenz
23151 section 1.4 specifies that this reply is always coupled to the command that
caused it. The station is not saying "not supported" — it is saying nothing.

### The decoder is ruled out

Read back in service mode on 2026-08-04:

- **CV29 = 14** — bit 3 set, RailCom enabled.
- **CV28 = 3** — bit 0 channel 1, **bit 1 channel 2**. Channel 2 is the one a POM read
  needs. The ZIMO MS manual states "CV #29, Bit 3 = 1 AND CV #28 = 3 (or = 67, if large
  scale decoder)", so 3 is the correct value for this PluX22 decoder.

The decoder is therefore configured exactly as POM reading requires, and it was proven
live on the main track. The cause is on the command station side.

Remaining candidate causes:

1. **RailCom cut-out generation disabled** in the YD7010 (Track Out → DCC Properties →
   Track → "Generate the RailCom cut-out"). Without the cut-out no decoder can reply.
2. **No global RailCom detector in the YD7010.** The manual states the YD7010 generates
   the cut-out and describes reception through external modules (YD6016LN-RC, YD7432,
   YD7652, DR5088RC, DR5052, DR5013). It nowhere promises an onboard global detector.
   Generating a cut-out and receiving channel 2 are separate capabilities.
3. **Result delivered on a channel not covered here.** Lenz 23151 lists a
   `Broadcast "Railcom-Info"` among the unsolicited broadcasts and never defines its
   format — one mention in 64 pages. The raw captures show no unparsed frames, so this
   is currently unsupported by evidence, but it cannot be ruled out over other transports.
4. ~~Decoder RailCom off~~ — **excluded by measurement**, see above.

**Most likely explanation:** the YD7010 generates the RailCom cut-out but has no global
RailCom detector, so nothing in the command station can receive the decoder's channel 2
reply. The manual describes RailCom reception through external modules (YD6016LN-RC,
YD7432, YD7652, DR5088RC, DR5052, DR5013) and states that RailCom information is shown
only when such a module is fitted. Its predecessor, the Digikeijs DR5000, is explicitly
documented to require a DR5088RC for POM reading. If that holds, POM CV read cannot work
on this setup without adding a RailCom feedback module, and no software change will
alter that.

This remains an inference: YaMoRC does not publish a hardware block diagram, and nothing
found so far states outright that the YD7010 lacks a global detector.

### POM WRITE works, even though POM read does not

Measured 2026-08-04. CV3 was written over POM with the locomotive on the main track, then
the locomotive was moved to the programming track and the CV read back in service mode:

```
main track   E6 30 00 03 EC 02 24   POM write CV3 = 36, only an interface ACK
prog track   read CV3 = 36          the write landed
             restored CV3 = 26      original value back
```

This asymmetry is not surprising: a POM write needs no return path from the decoder,
while a POM read needs RailCom channel 2 to come back to the command station. It is the
read half that has no receiver.

**Consequence:** tuning by ear on the main track is possible. Volume and sound CVs can be
changed while the locomotive runs. What cannot be done on the main track is reading a
value back — verification requires the programming track.

### Next step for R1

Read any CV through the YaMoRC tool's own **"Loco POM"** mode (Prog.Track CV Programming
→ Mode → Loco POM). That tool has a dedicated "No Reed — No read result" status, so the
vendor anticipates this failure.

- If the vendor tool **can** read via POM, the capability exists and the gap is in the
  XpressNet path used here — possibly it is only exposed over Z21 LAN.
- If the vendor tool **cannot**, POM read is unavailable on this hardware and the design
  must fall back to service mode on the programming track as the primary CV path.

## R2, R4 — service mode on the programming track: SETTLED

**Service-mode CV reading works. Every opcode family tried works** — but they differ in
how the result is delivered.

| Opcode | Result delivery |
| --- | --- |
| `23 11` — Z21 service read, 16-bit CV | **unsolicited**, arrives on its own |
| `22 15` — Lenz legacy direct read | **only after `21 10`** is sent |
| `22 18` — Lenz extended, band CV1–255 | **only after `21 10`** |
| `22 19` — Lenz extended, band CV256–511 | **only after `21 10`** |

Verified with the poll, three rounds each, all correct against known constants:
`22 15 01` → CV1 = 3, `22 15 08` → CV8 = 145, `22 18 01` → CV1 = 3, `22 18 08` → CV8 = 145,
`22 19 09` → CV265 = 0, `22 19 0A` → CV266 = 64.

> **Correction.** An earlier version of this document stated that the Lenz opcodes were
> silent and that the station implemented only the Z21 family. That was wrong. The probe's
> `_read_value` never sent the `21 10` service-result request, so it never collected results
> the station was holding for it. The capability was declared absent because of a defect in
> the instrument measuring it — the precise failure this probe exists to prevent. Fixed;
> `_read_value` now polls.
>
> The poll is not a workaround for this station's quirk. It is the documented protocol.
> XpressNet section 2.2.8, verbatim: *"The read instruction does not require an answer by
> the command station! A result must be specifically requested with the 'Request for
> Service Mode results' request. Only after receiving the response to programming results
> request can it be determined whether the read instruction was successful or not."*
> The answer was in a document already on disk. The Z21 opcode pushing its result without
> being asked is the exception here, not the rule.

### CVs read from the ZIMO MS450P22

| CV | Request | Reply | Value | Cross-check |
| --- | --- | --- | --- | --- |
| 8 | `23 11 00 07` | `63 14 08 91` | **145** | ZIMO manufacturer id — the known constant |
| 29 | `23 11 00 1C` | `63 14 1D 0E` | **14** | bit 3 set, RailCom enabled at the decoder |
| 250 | `23 11 00 F9` | `63 14 FA 06` | **6** | decoder type 6 = MS450, matches the hardware |
| 251 | `23 11 00 FA` | `63 14 FB FB` | **251** | serial byte, low |
| 252 | `23 11 00 FB` | `63 14 FC 69` | **105** | serial byte, middle |
| 253 | `23 11 00 FC` | `63 14 FD 4B` | **75** | serial byte, high |
| 265 | `23 11 01 08` | `63 15 09 00` | **0** | sound project |
| 266 | `23 11 01 09` | `63 15 0A 40` | **64** | master volume |

`CV7 = 5` and `CV65 = 15` were read the same way on 2026-08-06: ZIMO puts the main software
version in CV7 and the sub-version in CV65, so this decoder runs firmware **5.15**. A sub-version
below 100 is a normal release (JMRI, `Zimo_MS_large_v5.xml` and the CV65 tooltip in
`Zimo_MX69MX690.xml`: 0–99 normal, 100–199 beta, 200+ special). The request and reply bytes were
not captured in that run.

**CVs above 255 are reachable** — by both routes. `22 19` reaches them through the Lenz
band scheme, and `23 11` through its 16-bit address; both are answered on the `63 15`
reply band. CV265 and CV266 read correctly either way.

With `_read_value` fixed, the probe's own checks now report:

```
service_ext_cv        True   high_band True   (CV265 read back on band 0x19)
z21_cv_opcodes        True                     (Z21 and direct reads of CV29 both 14)
```

The request is **zero-based** (`23 11 00 07` reads CV8) and the reply echo is
**one-based** (`63 14 08`). Band replies carry the offset from the band base: `63 15 09`
is CV256 + 9 = CV265.

For M2, the `63 14` band maps as Lenz 23151 section 3.1.2.6 states: **C = 0 means CV1024**,
C = 1..255 means CV1..255. Not 0xFF for CV1024 — a plausible-sounding claim that the
document contradicts.

### CV251–253 — the decoder serial, and what it does not yet prove

Measured 2026-08-04. The restore path planned for M9 is meant to refuse to write a backup
taken from one decoder into a different one, and that gate needs something that identifies
the unit. These three CVs are the candidate.

`CV251` reads back **251**, the same number as the CV itself. That is the shape of a parser
returning the CV number instead of the value, so it was not accepted until three separate
checks ruled the artefact out:

- **The anchor.** CV8 read back 145 through the same code path in the same session. 145 is
  not 8, so the path returns values, not numbers.
- **The pairing.** The direct-opcode reply carries no CV echo, so nothing in the frame says
  which CV it answers; the attribution rests on the link keeping one command in flight. The
  four CVs were re-read in reverse order, interleaved with CV8. Every value stayed with its
  own request and every CV8 returned 145. A reply arriving one request late would have moved
  the 145 onto a subject CV.
- **Two opcode families.** `22 15` is one-based and `23 11` is zero-based. Both return the
  same four values, so an off-by-one in either would have shown up as a disagreement.

Stable across two passes in one session.

What this does **not** establish: that the three bytes differ between two MS450P22 decoders.
Only a second decoder answers that, and until one is read, a value identical on both would
mean these CVs identify the model rather than the unit — which would leave the M9 gate with
nothing to compare. The byte order of the composite is also unknown: nothing here says
whether ZIMO means 251·2^0 + 105·2^8 + 75·2^16 or the reverse, and no reason to guess has
come up yet, because the gate only needs equality, not the number.

### Service-mode WRITE works

Tested on CV3 (acceleration rate), read first, changed, verified, restored:

```
read  CV3            = 26          original
write 24 12 00 02 24 → 63 14 03 24 station echoes CV3 = 36
read  CV3            = 36          change confirmed
write 24 12 00 02 1A → restored
read  CV3            = 26          back to the original value
```

The write is followed by a `63 14` result carrying the written value. Two cautions before
treating that as proof:

- `63 14` is the **direct-CV read-result** format (Lenz 23151 section 3.1.2.6), not a
  documented "write echo". It shows the command station produced that value; it does not
  by itself prove the decoder accepted and retained it. What proves that here is the
  independent read afterwards, which is why the test above reads CV3 back rather than
  trusting the reply.
- **Stale-result hazard.** `21 10` asks for the *stored* result. If an operation fails
  quietly, a poll can return the result of a previous one. In the test above the value
  changed from 26 to 36, so the result was demonstrably fresh — but `railctl` must not
  rely on the poll alone to confirm a write. Read back with the value expected to differ,
  or the confirmation is circular.

Even so, service mode is strictly better placed than POM here: it has *a* verification
channel, where a POM write has none at all by design.

`restore` therefore has a proven path. It is no longer gated.

### Timing: the 4–5 s spacing claim was wrong

Also an artefact of the missing poll. With the poll in place, nine consecutive reads at
gaps of 0.2 s, 1.0 s and 2.5 s were all correct (CV8 = 145 every time). Spacing does not
matter.

Real hardware latency for one service read is **about 1.7 s**. A 77 CV backup is therefore
roughly **2.2 minutes**, not the 6–7 minutes estimated earlier.

**Open performance issue for M2:** `SerialLink.collect` always waits the full window even
after the reply has arrived, so an 8 s `SERVICE_WINDOW` turns a 1.7 s read into an 8 s one
and a backup into ten minutes of waiting. `collect` should return as soon as a satisfying
reply is parsed.

### Consequence for the design

The design chose **POM on the main track as the primary CV path**, with service mode
secondary. The measurements invert that: POM returns nothing, service mode through
`23 11` returns everything asked of it, including the high CVs the ZIMO backup needs.
**The primary CV path should be service mode on the programming track using `23 11`.**

The R2 check did not need rewriting onto a different opcode family after all — it needed
its instrument fixed. It asks the right question and now answers it.

### Operational note

Each service-mode read makes the YD7010 switch its output between the main and the
programming track with an audible relay. A long backup therefore cycles that relay once
per CV. There is no protocol reason to space the reads out — see the timing section above,
which measured nine consecutive reads at 0.2 s gaps all correct. Do not overlap
operations: complete read → poll → result before starting the next one.

Because the relay re-energises the main track on every cycle and the station's start mode
is **automatic**, a long run repeatedly restores power to locomotives that will resume
their last speed. Send an emergency stop before a backup, not only after.

## Measurement note

`TC` in the YD.Control telemetry read **0 mA** throughout, with track voltage at 15.1–15.2 V,
while the decoder was demonstrably powered and responding. **Do not use `TC` to decide
whether a locomotive is on the track** — it does not resolve a standing sound decoder's
current draw. That reading caused a wrong diagnosis during this session.
