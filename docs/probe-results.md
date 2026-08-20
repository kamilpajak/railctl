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
| `80 80` **before** energising | does **nothing** — the loco resumes exactly as without it | 2026-08-09, control vs test |
| `80 80` **after** energising | **holds** the layout; caught the loco before it moved at steps 15 and 80 | 2026-08-09 |
| Emergency stop and the refresh buffer | **holds, never clears** — releasing resumes the stored speed | 2026-08-09, `loco_info` still read 80 |
| Decoder acceleration | **several seconds** to reach step 80 — why a 0.5 s window showed no motion | 2026-08-09, watched |
| `in_use_by_other` | set once **any** device has driven the loco, including an earlier railctl run | 2026-08-09, loco 3 vs loco 5 |

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

### The order is now a measured capability, not a constant

Issue #13. The reading above is still the reading, and it is still the DEFAULT the tool applies
when nothing has measured the attached station (`xbus/dialect.py`, `DEFAULT_STATUS_BIT_ORDER =
LENZ_23151`). What changed is that it is no longer a claim about XpressNet: the two documented
orders are named data, `capabilities.status_bit_order` records which one a station was measured
to use, and **`railctl doctor --measure-status-bits` (check D13) is the instrument** - it repeats
the experiment above, one `80 80` and one status read, and records the order whose emergency-stop
bit moved.

D13 is opt-in because the experiment holds the whole layout and then releases it, and the release
is when a locomotive with a stored speed starts moving (run 5, 2026-08-09). It also refuses to
measure unless the track is live with neither disputed bit already set, because a bit that was
already set is a bit the stop cannot be credited with. `null` in the capabilities file means
nobody ran it - never that the station uses the default.

#### The tool reproduces the hand measurement - ACCEPTANCE PASSED 2026-08-20

Run at the bench with the main track empty and the locomotive on the programming track, from
`0x04` (live, nothing held). `tests/hardware/test_issue13_acceptance.py`, two stages, both passed.

Stage 1, a plain `railctl doctor --no-programming-track`: D13 `skip`, `status_bit_order` stays
`null`, and the layout block reads `hold_applied: false, held: false, must_leave_held: false` -
the run did not touch the layout, and "nobody asked" is recorded as `null` rather than as the
default.

Stage 2, the same run with `--measure-status-bits`:

    62 22 04   before, live and released
    80 80      the hold
    62 22 05   after - bit 0 went from clear to set
    21 81      the release
    62 22 04   confirmed clear again

`status_bit_order: "lenz_23151"`, `hold_applied: true`, `held: false`, `must_leave_held: false`.
Bit 0 is emergency stop and bit 1 is emergency off, which is what 2026-08-05 established by hand
against the LED - now established by the tool, on its own, in 22 seconds including both gates.

The whole point of the exercise: this is the same answer arrived at through an instrument rather
than through a constant in the source. A station that answered `lenz_spec` here would be a finding
about that station, not a bug in the default.

##### The LED was checked separately, because D13 is too fast to watch

D13's held window is the gap between `80 80` and `21 81` - tens of milliseconds; the whole stage-2
run measured 73 ms end to end. Nobody can see a front-panel LED change and change back inside that,
so asking an operator to watch it DURING the check asks for an observation that cannot be made.

It was taken from a persistent hold instead, the same way 2026-08-05 did it, using the same
telegram the check sends (`railctl stop` with no address and `station.emergency_stop(address=None)`
are one path):

    62 22 04   live, released      Track Out GREEN STEADY
    80 80      hold, and leave it
    62 22 05   held                Track Out GREEN FLASHING - observed 2026-08-20
    21 81      release
    62 22 04   released again      Track Out GREEN STEADY

Green flashing is the load-bearing half. It says the track still has voltage while the layout is
held, which is what Lenz XpressNet 2.2.4 claims and what makes the bit that moved the EMERGENCY
STOP bit. Red there would have meant the voltage dropped, the moved bit was emergency OFF, and the
conclusion - and `DEFAULT_STATUS_BIT_ORDER` - were backwards. The status byte alone cannot tell
those two apart; only the LED can, which is why this observation is recorded and not assumed.

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
- ~~`CvOutOfRangeError` is raised when no encoding has been probed, naming the wrong cause (#16).~~
  Fixed: that case now raises `ServiceEncodingUnknownError` (exit 18), which names a state the
  operator clears by probing rather than a CV number they would have to retype.

### Confirmed by watching the locomotive

`80 80` sent to a locomotive running at step 30 stopped the wheels **instantly**, with the green
Track Out LED flashing throughout — the manual's "emergency stop has been triggered (track voltage
ON)". The status byte read `62 22 05` for ten seconds. This was predicted before the panel was
read, not explained afterwards, and it is the fifth independent confirmation of the bit order.

In the same moment `loco_info` reported `speed=30, emergency_stopped=False` for a locomotive that
was standing still under a global emergency stop. The per-locomotive view does not reflect the
station-wide state.

## Service mode needs no track power — SETTLED 2026-08-06

`doctor.py` refuses to run D5–D8 unless the main track is powered, and the comment on that gate
gives two reasons: that entering service mode cuts main power, and that leaving it re-energises
the main track even when the operator never authorised power. Neither had ever been measured;
both were reasoning written into a comment and then read back out as fact.

The two halves now stand on different footing, and the difference matters:

- **Settled.** A service-mode read works with the main track unpowered. Positive, reproduced,
  and independent of any instrument — the CV value came back.
- **Strongly supported, not settled.** Service mode does not disturb the main track. This is a
  negative established by a person watching a meter, so it can only rule out disturbances longer
  than that meter resolves. See the limits below.

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

**What the validation does and does not buy.** It establishes that this meter separates the two
states the station was commanded into, which is all a presence detector has to do. It assumes
nothing about the status byte being right — the byte is the thing under suspicion elsewhere in
this file — because both readings were taken while the station was told what to do, not asked.
Its temporal resolution was **not characterised**: a handheld meter typically refreshes a few
times a second, so a disturbance shorter than that is invisible to it and stays `unknown` here.

### What was measured

| Question | Result | Evidence |
| --- | --- | --- |
| Does a service read work with the main track unpowered? | **Yes** | `CV8=145`, 4 reads out of 4 |
| Does service mode cut a powered main track, for the duration of a read? | **No** | 13 operations watching the meter, then 3 more listening for relays; 16 V throughout both |
| Does leaving service mode energise an unpowered main track? | **No** | 16 V never appeared in any of the 4 reads |
| Does a relay switch during service mode? | **Yes, twice per read** | 6 audible clicks for 3 reads, in both power states |

**Method, because it decides what the negatives are worth.** The exit window was widened for the
last two questions: `service_exit_settle` was raised from its default 0.10 s to 1.5 s, so the gap
between resume-operations and the power-off that follows it was 15 times longer than in normal
operation. No telegram was added or removed. This makes the test **more** sensitive, not less —
a station that was going to energise the track had far longer to show it — but the default
0.10 s case was never watched directly, and a pulse confined to that shorter window remains
untested.

The two power-on runs asked the operator for different things: the first for the meter, the
second mainly for relay clicks with the meter reported alongside. Both reported 16 V unbroken.
Request and reply bytes were not captured for any of these reads; the evidence is the returned
value and the operator's reading.

The relay clicks in **both** power states, so it tracks service-mode entry and exit, not the power
commands. An earlier run appeared to contradict this; it did not — that run asked the operator to
*watch* the meter, and nobody was listening for clicks.

With the track unpowered, the meter alternates between 0.6 V and 0.0 V in step with the relay.
Both readings are "no track voltage". The likeliest reading is leakage following the relay
position — the track is empty and the meter's input is high impedance — but that is an
interpretation, not a measurement, and a real switching of the output cannot be ruled out from
these numbers. Separating the two would need a load or a scope. Either way the pair does not
survive the next `power_on`, which restores 16 V from both, and no conclusion here rests on the
difference.

### What another implementation does

In the `java/src/jmri/jmrix/lenz/` path — the one an XpressNet station goes through — JMRI does
not model any of it:

- `XNetTrafficController.enterProgMode()` returns **null**, with the comment "This method has to
  be available, even though it doesn't do anything on Lenz". Nothing is sent to enter service
  mode; the station enters it on the first programming command.
- `enterNormalMode()` returns `XNetMessage.getExitProgModeMsg()`, which is a bare
  `CS_REQUEST` + `RESUME_OPS` (`21 81`). No power state is saved and none is restored.
- `XNetProgrammer.java` contains **no reference to a PowerManager**. Across `jmrix/lenz/`,
  `PowerManager` appears in four files — `XNetInitializationManager`, `XNetSystemConnectionMemo`,
  `XNetPowerManager` itself and `hornbyelite/EliteAdapter` — and in no programmer.

Scope of that check: the XpressNet connection code only. JMRI's generic programming layer
(`jmrit/symbolicprog`) was not examined, so this says nothing about whether some higher level
warns a user about track power before programming.

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

## `power on`'s stop-all was in the wrong order — SETTLED 2026-08-09

`power on` sends `80 80` (emergency stop, all locomotives) **before** it energises the track,
on the reasoning that a locomotive resuming its stored speed unattended is the thing to
prevent. The design spec called that step inferred, never measured. It is now measured, and
the inference was wrong.

Setup: locomotive 3 (ZIMO MS450P22) on the rolling road, nothing else connected to the
station — checked, because a second throttle refreshing its own speed would invalidate every
row below.

### What was measured

The question needs a control. Had only the test run happened and the locomotive stayed put,
that would not distinguish "the prefix worked" from "this station never resumes".

| # | run | prefix `80 80` | locomotive after power returned |
| --- | --- | --- | --- |
| 1 | control — `station.power_on()` alone, CLI bypassed | no | **runs** |
| 2 | test — `railctl power on` | yes | **runs** |

So the prefix changes nothing. The station resumes a stored speed either way, which also
confirms what status bit 2 (automatic start mode) claims.

The prefix is not useless, though — it is misordered. `railctl stop` on a **live** track
works: status goes to `62 22 05`, emergency stop set, voltage kept. So the station either
ignores `80 80` while the track is dead, or the subsequent power-on clears it.

| # | run | locomotive |
| --- | --- | --- |
| 3 | energise, then `80 80` 0.51 s later, stored step **15** | **never moved** |
| 4 | same, stored step **80** | **never moved** |

### The 0.5 s window, and why it did not matter

That gap is railctl's own, not the station's: `power_on()` pays `power_settle` plus a status
round trip because the YD7010 never answers `21 81` with `61 01` (measured 2026-08-05).
Sending the stop straight after the power-on telegram, before the verify, would cut it to
milliseconds.

It did not matter here because **the decoder's acceleration curve is several seconds long** —
watched directly: released from a stored step 80, the locomotive spent several seconds
winding up. Half a second produces no visible motion at either speed tested. That is a fact
about this decoder's CV3, not about the window being inherently safe, and a decoder with a
short ramp would behave differently.

### The part that constrains the fix

Reordering alone is not enough, because of run 5:

| # | run | locomotive |
| --- | --- | --- |
| 5 | from emergency stop with step 80 stored, send `21 81` to release | **accelerates away** |

`loco_info` still reported `speed=80` after the emergency stop and after the release. **The
emergency stop holds the refresh buffer; it never clears it.** So `power on` cannot send a
stop-all and then quietly release it — releasing is exactly what run 5 did. Either the
command leaves the layout held and says how to release it, or it zeroes the stored speed of
every locomotive it knows about, and it only ever knows `--address`.

### Two more runs, which fix the order of steps inside the new `power on`

The chosen design energises, holds with `80 80`, then zeroes the addressed locomotive so a
later release cannot start it. That last telegram goes out while the layout is held, which
raises two questions neither of the runs above answers.

| # | run | result |
| --- | --- | --- |
| 6 | while held at `0x05`, send `drive(3, 0)`, then read `loco_info` | reports **speed 0** |
| 7 | same run, then read the STATUS again | still `0x05`, **hold intact** |

Run 6 says the zero may be sent after the hold rather than before it. Run 7 is the one that
matters for what the command is allowed to claim: a per-locomotive speed telegram does **not**
clear the station-wide emergency stop, so `power on` telling the operator the layout is held
is true at the moment it says so.

Run 7 was added after a review pointed out that run 6 alone proves the telegram arrives and
says nothing about whether the hold survives it. Nothing could move during either run — every
telegram involved is a stop, a zero, or a read.

### Also observed

- `drive 0` brakes along the decoder's deceleration curve; `80 80` cuts immediately. Watched:
  the same locomotive coasted to a stop under the first and stopped hard under the second.
- The pre-flight refusal works on hardware. With emergency stop active, `railctl drive 15
  --address 3` exits **20** with `code: track_power`, `condition: emergency_stop`, and the
  runnable suggestion `["railctl","power","on"]` — and its message distinguishes emergency
  stop (voltage present) from emergency off (no voltage), which is the distinction the swapped
  bit order exists to preserve.
- Status `0x06` was read while the operator measured **0.6 V** on the main track. Under the
  Lenz bit order `0x06` would mean emergency stop, which leaves the track energised, so
  railctl would have reported a dead track as powered. This is independent confirmation of the
  swapped order recorded on 2026-08-05, from a state that discriminates.

## `in_use_by_other` marks any past driver, including us — SETTLED 2026-08-09

The flag fired for locomotive 3 with **nothing but railctl connected to the station**, which
first looked like a confounded measurement. It is not: the flag tracks "some device has driven
this locomotive", and every railctl run connects as a new device.

| locomotive | ever driven | `raw_ident` | `in_use_by_other` |
| --- | --- | --- | --- |
| 3 | yes, by an earlier railctl run | `0x0C` | **True** |
| 5 | never | `0x04` | False |

Both report 128 speed steps (`0x04`); the difference is bit 3. It survives across connections
and does not clear on a re-read within one session.

Consequence for the CLI: a warning worded "another throttle holds loco 3" sends the operator
looking for a throttle that is not there, on the second and every later run against the same
locomotive. The fact belongs in the envelope; the wording has to say what the bit means.

## M6 acceptance run — PASSED 2026-08-10

Four stages, all passed, 19 minutes. First end-to-end exercise of the CLI against the real
station; everything before this ran against fakes.

**Bench:** locomotive 3 (ZIMO MS450P22) on the PROGRAMMING track. The rolling road is wired to
TRACK OUT and was empty. Nothing else connected.

**Locomotive 3 did not turn a wheel at any point, in any stage.** Watched by the owner.

**Final status byte: `0x07`** — track dead, hold still set under it, which is how stage 1 found
the bench.

### What each stage established

| stage | command | what it showed |
| --- | --- | --- |
| 1 | `doctor --address 3` | `pom_read: null` with the track dead — never asked, never answered |
| 2 | `doctor --address 3 --power-on` | the layout held, loco 3 idled, and `pom_read: false` carrying `pom_read_provenance: "silence"` |
| 3 | `monitor --limit 1 --format ndjson` | `61 00 61` decoded as `power.off`; two NDJSON lines ending in `summary` |
| 4 | `power off` on a dead track | `changed: false`, `completed: ["read_status_before"]`, no telegram sent |

Stage 1's `pom_read: null` is the founding rule running end to end on hardware for the first
time — station, dataclass, JSON envelope, file on disk. Stage 2's `"silence"` beside the `false`
is the other half: the one place this codebase allows a `false` without a `61 82`, with the
reason attached. Both had only ever been exercised against fakes.

Stage 2 also closes issue #14. D3 reported "track power turned on, then the whole layout was
held and loco 3 was sent speed 0; the hold is re-asserted and read back at the end of the run",
and `layout` came back `held: true, idled_address: 3, direction_preserved: true`. The hold is
read back from the status bit, not asserted from the fact that a telegram was sent.

### What this run does NOT establish

**D4's silence was trivial here.** The main track was empty, so the POM read had nothing to
answer it. That is consistent with R1 but does not confirm it: this run cannot separate "a
decoder is present and there is no RailCom detector" from "nothing was there". `capabilities.py`
already records that ambiguity — pointing the doctor at an address with no decoder produces the
same result. Settling it needs a second decoder on the rolling road while the first stays on the
programming track.

**Window 1 of stage 2 was not stressed.** Locomotive 3 entered the run with a stored speed of 0,
so the gap between energising and holding had nothing to resume. That window was measured
separately on 2026-08-09 at stored steps 15 and 80, with no movement either time.

### New: what the station's two buttons do

Undocumented until now, and worth knowing before designing any experiment around the front panel.
The buttons do not SET a state, they MOVE it:

- **green** — always jumps to green steady (voltage on, no hold), whatever the current state
- **red (STOP)** — degrades one step: green steady -> green flashing -> red

So the red button cuts power only from the flashing state. From green steady the same press
produces an emergency stop with the voltage still on. The acceptance document's stage-3 line
"That also cuts track power" happened to be true because stage 2 had left the layout in the
flashing state; it is not true in general.

This also gave an independent witness for stage 2's claim. Before stage 3 the panel showed green
FLASHING — emergency stop with voltage present — which is what the doctor's `layout` block had
claimed by reading the status bit. Panel and byte agreed.

### Two defects in the acceptance document itself, found by running it

- Stage 1's gate asks for "locomotive 3 on the rolling road" AND "readable on the programming
  track" in the same breath. Those are two places. The file was written for a bench with a
  decoder on each.
- Stage 1's gate says "safe, nothing is energised". It is not: `exit_service_mode` ends every
  service-mode session with `21 81`, which energises, before restoring the state it found. The
  track flickers several times during stage 1. It is safe here only because loco 3 has no stored
  speed.

Both are corrected in the file.

## The session gap crosses invocations — SETTLED 2026-08-12

The M8 acceptance's stage 3 failed both its CV3 writes with `61 13`, while every read in the
same run succeeded — and the identical write, run by hand minutes later, succeeded with the
same `24 12` direct telegram the 2026-08-06 session had proven. Reproduced deterministically:

| sequence | result |
| --- | --- |
| `cv read 3` then `cv write 3 26` back-to-back (~1 s apart) | write fails `61 13` |
| the same `cv write 3 26` after minutes of idle | succeeds, `63 14` echo, verified |

This is the inter-session gap measured 2026-08-07 (sessions opened within ~1.5-1.75 s of the
previous close fail wholesale with `61 13`), crossing an invocation boundary. The guard,
`_await_session_gap`, keeps its memory on the programmer instance - and every CLI invocation
builds a fresh one, so "a session closed 800 ms ago" is forgotten exactly when it matters.
`_last_session_end = None` means UNKNOWN, and the code read it as "no session ever".

Also settled by the same failure: the `decoder_no_ack` hint blamed a 750 mA programming track
and said `retryable: false`. Both wrong here - the cause was the tool's own timing, and the
failure heals with a 3 s wait. CV3 was confirmed unchanged (26) after both failed writes:
nothing reached the decoder.

## The retry that reported itself and never ran — SETTLED 2026-08-12

The first fix for the cross-invocation gap (writes pay `_await_session_gap`, plus a retry-once
when the instance's session history is unknown) failed its own acceptance rerun: stage 3 died
`61 13` again, with the new hint claiming "the session-gap retry already ran". It had not run.

Bracketing on the bench, same decoder, same `cv write 3 20`, each preceded by a `cv read 3`:

| gap after the read's session close | result |
| --- | --- |
| ~3.2 s | write succeeds, verified |
| ~5 s | write succeeds, verified |
| ~10 s | write succeeds, verified |
| minutes idle | write succeeds, verified |

So the 3.0 s retry gap is sufficient — the retry itself never fired. The defect:
`_retry_once_for_unknown_gap` read `_session_history_unknown` at **catch time**, but the failed
attempt's own `finally` (`exit_service_mode`) had already stamped the session end and set the
flag to `False`. By the time the exception arrived, history always looked "known" and the retry
was skipped. The unit test passed because its `exit_service_mode` stub was a no-op that skipped
exactly the state transition under test — the mock hid the bug.

Fix: snapshot the flag **before** the attempt; test stubs now stamp `_last_session_end` and
flip the flag like the real exit does. Mutation-proved: reverting the snapshot turns 4 tests red.

Left open by the bracketing (all measured gaps followed a *read's* close): whether 3.0 s after a
FAILED write's close also suffices — the retry path exercises exactly that. Answered by the
acceptance run below: yes. Four commands in that run hit `61 13` on their first session, retried
3.0 s after the failed attempt's own close, and every one succeeded on the second attempt.

## M8 acceptance run — PASSED 2026-08-12

Four stages, run as one invocation of `tests/hardware/test_m8_acceptance.py` against the
MS450P22 (loco 3) on the programming track, isolated config dir, 2 min 18 s total:

| stage | observable | result |
| --- | --- | --- |
| 1 | doctor, no `--power-on` | D5/D6/D7 ok, D3/D4/D10 honest `unknown` (track power off), capabilities saved |
| 2 | batch read CV1,3,8,29 | all ok: CV1=3, CV3=26, CV8=145, CV29=14 |
| 3 | verified write, restored | CV3: 26 → 20 → 26, every step `verified: true` by independent read-back |
| 4 | CV1025 refusal | exit 15 `cv_out_of_range`, `1..1024` named, `railctl doctor` suggested, no telegram |

The retry-once fix was visible working, not idle: stage 3's four back-to-back commands (write,
read-back, restore, final read) each opened their first session moments after the previous
invocation's close, each got `61 13`, and each healed on the one retry — reported honestly as a
`service.session_retried` warning in the envelope, never silently. Stage 2's batch and stage 3's
pre-read carried no warning: their previous session had closed while the operator sat at a gate,
so the first attempt simply succeeded. Cost of the unknown-history retry: ~3 s per back-to-back
invocation, paid only when the gap is actually hit.

## Station firmware updated (KLUG) — re-probed 2026-08-12

The YD7010's firmware was updated after the M8 acceptance run, **with the station disconnected
from mains for the update and reconnected afterwards** — so the post-update probes start from a
cold boot, with every piece of station-side state cleared (refresh-buffer entries, and the
emergency-stop flag the morning run's D2 had shown as `True`; the post-update `False` is the
power cycle, not a button press). The pre-update `capabilities.json` was set aside as
`capabilities.pre-klug.json` before re-probing, so nothing measured on the old firmware could
masquerade as a measurement of the new one.

Two doctor runs on the new firmware, same day:

| run | bench | result |
| --- | --- | --- |
| no `--power-on`, loco on the prog track | service-mode checks | D5/D6/D7/D9 all ok; identity CVs byte-identical to the morning run (CV7=5, CV8=145, CV250=6, CV1=3, CV29=14) — the station flash left the decoder alone |
| `--power-on`, loco on the rollers (main) | D3/D4 | D3 fully green (energised, held, loco 3 idled, hold re-asserted, direction preserved); D4 total silence after 3 attempts — neither `61 13` nor `61 82` |

So the firmware update changes nothing this tool measures: XpressNet 4.0, station id 0x12,
every service-mode and Z21 encoding as before, and **R1 stands — POM read is still silence on
the new firmware**. `pom_read=false` with provenance `silence`, `pom_result_channel: none`.
The missing piece remains a RailCom detector on the layout, not the station's firmware.

The second run also exercised the capability merge as designed: its service checks came back
`unknown` (`61 13` — nothing was standing on the prog track), and the merge preserved the first
run's measured `true` values instead of letting `unknown` overwrite them.

## The ZIMO index bank rests at 0:1 and will not leave it — SETTLED 2026-08-13

The first `railctl backup` run against the MS450P22 refused with exit 17: the decoder's index
selectors read **CV31=0, CV32=1**, and the command treated anything but 0:0 as "parked on a CV
page". The refusal itself was correct behaviour - a backup never writes the selectors - but the
premise was wrong, and this section is what replaced it.

**The bank cannot be moved.** `cv write 32 0` was accepted by the station and the independent
read-back returned 1, so the write did not stick (exit 14 `cv_verify`, which is the verification
doing its job). A `select_page` to 0:1 in the same session verified fine, so this is not
session volatility: the decoder accepts CV32=1 and refuses CV32=0.

| operation | result |
| --- | --- |
| `cv read 31 32 --mode service` | CV31=0, CV32=1 |
| `cv write 32 0` (service, verified) | station accepted, read-back 1 → `cv_verify`, exit 14 |
| `select_page((0,1))` inside `cv read 265 --page 0:1` | wrote and verified, decoder unchanged |

**On that bank the CVs above 256 read as the NORMAL ones.** Read with no page selection at all
(`cv read 287 395 396 397 --mode service`), against the ZIMO MS/MN manual's own CV table:

| CV | slug | read | manual |
| --- | --- | --- | --- |
| 287 | brake_squeal_threshold | 55 | default 50, range 0-255 |
| 395 | volume_limit | 80 | default 64, range 0-255 |
| 396 | volume_down_key | 15 | range **0-29** |
| 397 | volume_up_key | 14 | range **0-29** |

CV396 and CV397 carry a documented range of 0-29 and landed on 15 and 14 - F15 and F14, a
coherent volume-down/volume-up pair. A byte read from the wrong bank lands inside 0-29 with
probability 30/256; both doing so, adjacent and in the sensible order, is not what a wrong bank
produces. The doctor's D7 has also been reading CV257 successfully on this bank since the first
probe, with no page selection anywhere in its path.

The manual contradicts itself here and the measurement settles it: p. 70 says the normal CVs
"#257 … #512 can only be addressed if CVs #31 and #32 = 0", while p. 69 calls **0:1** the
result of "Resetting the CV bank". The decoder behaves as though 0:1 is neutral, and refuses
the state the first sentence demands.

Consequence in the tool: `backup` now treats `NEUTRAL_PAGES = {(0,0), (0,1)}` as "no page
selected" and only refuses on a real page such as 145/2 (the audio filters), where the same CV
numbers mean something the catalog does not name. The measured pair is always recorded in the
file, so a later reader knows which bank the values came from.

**Still unmeasured:** whether a decoder parked on 145/x can be read at all here, and whether
other ZIMO decoders rest at 0:1 or at 0:0. One decoder is one decoder.

## `cv read --page` does not select a page in service mode — FOUND 2026-08-13

Discovered while measuring the bank above. `cv read 265 --page 0:1 --mode service` prints a
`page.not_selected` warning: `service_read` cannot honour a page itself, so the value comes
from whatever bank is live. The CLI's confirmation text, meanwhile, promises that "--page
selects the ZIMO index page by WRITING CV31/CV32 ... The pair is read first and re-selected as
found afterwards" - and the batch path (`cv_read_many`) does exactly that, including the
restore. So the write does happen for a batch, and the extra warning comes from the per-CV read
that cannot repeat the selection.

What is wrong is the pairing: the operator is asked to approve a write, and then a warning says
the page was not selected. Both are true of different layers, and together they read as a
contradiction. Worth an issue against the M8 surface, not a blocker for M9.

## M9 acceptance run — PASSED 2026-08-13

Four stages against the MS450P22 on the programming track, one invocation of
`tests/hardware/test_m9_acceptance.py`, 1 h 25 min including the gate waits.

| stage | observable | result |
| --- | --- | --- |
| 1 | doctor probe | service encodings proven, `service_ext_cv` true |
| 2 | a real backup file | **77 of 77 ok**, no holes, `complete: true`, validates against `read_backup` |
| 3 | the same backup again | **byte-identical apart from `created_utc`** (11:27:42Z → 11:52:42Z) |
| 4 | the NDJSON stream | 79 lines, sequence 0..78 contiguous, `start` first, `summary` last, exit 0 |

Stage 3 is the milestone's acceptance sentence and the only part the unit tests could not make:
they prove the writer is deterministic given identical reads; only the decoder can prove it
answers identically twice. It does. The run also opened its first session moments after stage 2
closed its last, which is the case that used to fail `61 13`, and it passed without a retry.

Recorded in the file: `page: [0, 1]` (the resting bank), `cv_encoding: Z21_16BIT` - the reads
resolved to the Z21 opcodes, not the direct or extended ones - and the decoder block
`manufacturer_id 145, decoder_version 5, decoder_type 6, serial_bytes [251, 105, 75]`.

### CV251-253 answer after all

The design says of the serial bytes that they "have never been read on the reference hardware",
and both the restore identity gate and this acceptance file were built around that. **They
answered on all three runs, with the same values: CV251=251, CV252=105, CV253=75.** Whether
they were silent before this firmware or simply never attempted is not established by our own
record - what is established is that they answer now, repeatably.

Consequence for M10: the serial part of the restore identity gate can be a real gate rather
than the printed warning the design settled for. That decision should be re-made against this
measurement, not inherited.

### A backup costs 6 s per CV, and half of it is the session gap

Each backup took 466 s for 77 CVs - **6.05 s per CV**, not the 1.7 s a service read was measured
at. The NDJSON stream shows why: each `cv` line carries `elapsed_ms` of about 3.0 s (the read
plus its own service session), and the wall clock is twice that, because every read closes its
session and the next one waits out `Timing.service_session_gap` (3.0 s) before opening again.

So the cost is structural, not decoder speed: `cv_read_many` gives each CV its own session.
`CvProgrammer.service_read_many` already exists and reads several CVs inside ONE session -
written for exactly this - but the batch path does not use it. Using it would remove both the
per-CV session setup and the gap.

This matters for M11: a `--all` sweep of 1024 CVs at 6 s each is **1 h 42 min**. The >60 s
confirmation would state it honestly, but the number itself is a tooling choice, not a hardware
limit.

## M10 acceptance run — PASSED 2026-08-18

Five gated stages against the MS450P22 on the programming track. The tool passed every
stage; the test FILE's own assertions were wrong twice and had to be corrected, which is
recorded here because it cost 57 minutes of bench time and produced two real fixes.

| stage | observable | result |
| --- | --- | --- |
| 1 | doctor probe | service encodings proven |
| 2 | a full backup | 77 of 77 ok, `complete: true`, 466 s (identical to 2026-08-13) |
| 3 | `diff` of an unchanged decoder | `differences: 0`, five rows all `unchanged` |
| 4 | a hand-changed CV3, found and restored | see below |
| 5 | offline file-to-file `diff` | `differences: 0` in 4 ms, no link opened |

Stage 4 is the milestone's sentence, and every part of it held:

- CV3 written to 20 by hand, verified.
- `diff` reported `differences: 1` and named CV3 with `file_value 26, live_value 20`. The
  other four rows stayed `unchanged`. No CV was miscounted.
- `restore --dry-run` planned `counts.write: 1` for CV3 and reported `written: 0,
  verified: 0, stages_completed: []` - **the dry run wrote nothing**.
- `restore` reported `written: 1, verified: 1, stages_completed: ["A"]`, and an independent
  `cv read 3` immediately afterwards returned **26**. The value came from the file, through
  the plan, onto the decoder, and back out through a separate session.
- The decoder ended the run exactly as it entered it.

### Two defects the bench found that no test had

**A plain CV read-back was decorated with `page.not_selected`.** The M10 fix that pins every
verification read to the file's page - so a read-back cannot land on a different bank from
its write - passed the page for EVERY CV, including CV3. `service_read` emitted
`page.not_selected` for it, and an operator reading that envelope cannot tell it from a page
that genuinely did not take. Below CV257 the page argument is ignored by every layer that
handles it, so the warning reported a non-event. The emit is now conditional on the CV being
inside `INDEXED_CV_RANGE`, and the station tests that pinned the old behaviour were moved
onto an indexed CV, which is what their own docstrings had always described.

**The success envelope could not say WHICH CVs were written.** `written` and `verified` were
counts in the result envelope but lists of CV numbers in the failure details - the same names
carrying different types depending on the ending. After a command that changes a device,
"which" is the question, and `counts` already answers "how many". Both are lists now.

### The acceptance file's own bug, twice

`differences`, `written` and `verified` were all read as lists when the first two runs were
written; `differences` was a count and the other two became lists only in this session's fix.
Both failures aborted the stage AFTER the tool had already done the right thing, so nothing
was ever at risk - but the second run spent eight minutes re-reading a decoder for want of an
environment variable, and the milestone's central claim went unasserted through two runs.

The lesson is cheap to state and was expensive to learn: an acceptance assertion is code
against a contract, and the contract is readable offline - in the command's unit tests, in
`railctl schema`, or in the envelope builder itself. Guessing its shape from the prose of a
brief is not the same as reading it.

### The confirmation run — 2026-08-18, 6 min 37 s

Both fixes above were made AFTER the acceptance run that found them, so the envelope they
produce had never been seen by the hardware. A short re-run with `RAILCTL_M10_FILES` pointing
at the saved files (stage 2 skipped, 4 passed 1 skipped) settled that:

- `restore` reported `written: [3]`, `verified: [3]`, `stages_completed: ["A"]` - the CV
  numbers, not a tally. `--dry-run` reported `written: []`, `verified: []`,
  `stages_completed: []` on the same file and the same decoder.
- **No `page.not_selected` warning anywhere in the run.** Before the fix it decorated the
  CV3 read-back of every restore; CV3 needs no page, and now says nothing about one. The
  warning still fires for an indexed CV, which is what the station tests pin.
- The acceptance file passed in full for the first time. Its assertions had been wrong twice
  while the tool was right both times.

CV3 went 26 -> 20 -> 26 again, and the closing `diff` reported zero differences.

## Grouped service reads — issue #38 acceptance PASSED 2026-08-19

Three gated stages against the MS450P22 on the programming track, ten minutes end to end.
Reading eight CVs inside one service-mode session instead of one session per CV.

| stage | observable | result |
| --- | --- | --- |
| 1 | doctor probe | service encodings proven, decoder family `ms` |
| 2 | a full curated backup, timed | 77 of 77 ok, `complete: true`, **185 s** |
| 3 | the same backup as NDJSON | 79 lines, contiguous, 0 events, no silent CV |

**2.40 s per CV, down from 6.05 s.** The same 77 CVs, 466 s before and 185 s after, on the
same bench and the same decoder — 2.5 times faster.

**The file did not change.** Stage 2 compares its output against
`~/railctl-backups/loco-0003-curated.json`, the keeper backup taken on 2026-08-13 before this
change, ignoring only `created_utc` and `note`. Every other field matched, including all 77
values, all 77 statuses, the capabilities block and the encoding. This is the assertion worth
the bench time: a faster backup that reads something else is not a faster backup, and no unit
test can tell those apart.

### The rhythm is visible in the stream, and it is exactly eight

`elapsed_ms` in the NDJSON run separates into two populations with nothing in between:

| position in the group | `elapsed_ms` |
| --- | --- |
| first CV of a session | 2947 – 3063 |
| every other CV | 1481 – 1788 |

The expensive reads land at sequence 10, 18, 26, 34, 42, 50, 58, 66 and 74 — every eighth
line, without exception, which is `SERVICE_BATCH_SIZE`. Sequences 1 to 3 (CV31, CV32, CV29) are
also ~3.0 s because `backup` reads them as singletons before it can plan, and sequence 4 opens
the identity group.

So **a service read costs about 1.7 s and the session costs about 1.3 s on top of it**. The
2026-08-13 note above modelled the 6.05 s as "3.0 s read plus 3.0 s gap"; the `elapsed_ms` of
3.0 s it read that from was the read WITH its session, not the read alone. The corrected split
is what the two populations show directly.

### A session of eight answered to its last CV

Two full backups, 77 CVs each, and not one hole: `no_response: 0`, `error: 0`, and no
`service.session_close_failed` event. Before this run, four CVs in one session was the largest
number this hardware had ever been asked for (issue #22). Eight is now measured. Sixteen is
still a guess, and raising the group would save about 10 s on a 77 CV backup — not worth
another unproven session length without a reason.

### What this means for M11

A 1024 CV sweep at 1.86 s per CV (the in-group rate) is about **32 minutes**, against the
1 h 42 min the per-CV path would have cost. The >60 s confirmation still fires, and now it
quotes a number an operator can wait out.

## The first full CV sweep — M11 acceptance PASSED 2026-08-19

Three gated stages against the MS450P22 on the programming track, 41 min 41 s end to end.

| stage | observable | result |
| --- | --- | --- |
| 1 | doctor probe | all three service encodings proven, decoder family `ms` |
| 2 | the refusal | exit 2 `confirmation_required`, no file written, retry argv published |
| 3 | CV1..CV1024 swept | **1023 ok, 1 no_response**, 39 min, 2.29 s per CV |

### Everything above CV511 answered

This is the run's real finding, and it reverses an assumption the tool shipped with.
`0x63 0x16` and `0x63 0x17` — the extended reply bands for CV512..1024 — have still never
been seen, because the sweep never used the extended opcodes: with `z21_cv_opcodes` proven,
`service_read_telegram` picks the **Z21 16-bit opcode**, which carries the CV in one field
and has no band byte at all. Through that opcode every CV from 512 to 1024 produced an
answer.

Most of those answers are 0. Some are not, and they fall in blocks:

| CVs | values |
| --- | --- |
| 508–512 | 248, 248, 248, 248, 248 |
| 516, 519, 522, 525 | 202, 201, 185, 203 |
| 543–549 | 195, 128, 8, 194, 181, 8, 181 |
| 570–583 | 193, 32, 72, 197, 46, …, 196, 64, …, 214, 181, 198 |
| 744–761 | 191, 128, 8, 190, 128, 8, 194, 128, 8, 189, 181, 72, 192, 128, 8, 215, 0, 72 |
| 769–780 | 1, 127, 127, 127, 127, 1, 42, 26, 60, 30, 60, 30 |

The repeating `x, 128, 8` and `x, 181, 8` triples look like ZIMO sound-sample assignments,
which is where the MS decoder's manual puts CV744 and up. That is a resemblance, not a
verification.

**What is NOT established: that any of those values is the CV it is labelled with.** A read
of a CV the decoder does not implement can return 0 just as easily, and 500-odd of these are
0. Nothing above CV511 has been checked against a known quantity — the way CV396/CV397 were
checked against a documented 0-29 range when the index bank was settled. So
`HIGHEST_EXERCISED_CV` stays at 511 and the sweep still warns, with its text corrected: the
claim is no longer "nothing has ever answered up there", it is "nothing up there has been
corroborated".

The way to settle it is to write a known value to one high CV through POM (POM covers
CV1..1024 for writes) and read it back over the Z21 opcode. That needs a decoder we are
willing to write to.

### CV100 did not answer, and only CV100

One `61 13` in 1024 reads, at CV100, with 1023 neighbours answering — including CV99 and
CV101. Not yet reproduced; it may be a genuine non-acknowledgement for that CV or a single
transient. Worth one targeted re-read before it is called either.

### Timing

39 minutes for 1024 CVs is **2.29 s per CV**, against the 2.4 s the up-front estimate quoted
and the 2.40 s measured over the 77-CV curated backup. The constant is well calibrated for a
long run.

The estimate revised after the first ten CVs said **3.56 s per CV, 1 h 1 min** — 56 % over
the truth. Those first ten rows are the three singleton reads (CV31, CV32, CV29, one session
each) plus the identity group, so they carry all of the run's fixed cost and none of the
amortisation. The periodic progress lines, which re-estimate from the running rate every 32
CVs, converged instead: 43 min at CV32, 35 min at CV128, and 39 min actual. The early
revision is honest about what it measured; it is just measuring the least representative
part of the run.

### The curated values did not move

All 77 CVs from `~/railctl-backups/loco-0003-curated.json` appear in the sweep with the same
name and the same value. The wider net changed nothing about what the known CVs say, which
is what tells a working sweep from one reading off by an index.

### What JMRI says about the sweep — 2026-08-19, documentary, not measured

Everything in this subsection comes from reading JMRI's source and decoder definitions
(`~/Developer/Personal/reference/JMRI`), not from this bench. It is recorded here because
it bears directly on the two open questions the sweep raised, but it is a second opinion
from a document, and this file's authority is measurement.

**CV100 is a live measurement, which is why it does not answer.** `xml/decoders/zimo/
CV7-CV102_MS-MN-FS.xml`, included by the MS450's definition, has CV100 as `Current
asymmetry voltage`, `readOnly="yes"`, "the asymmetry the decoder measures right now, in
tenths of a volt. Read on the main to set the ABC threshold in CV #134." A service-mode
read verifies a value with acknowledgement pulses and can only succeed if the value holds
still; a CV holding whatever the decoder is measuring at that instant has no reason to,
and on the programming track there is no asymmetry to measure at all. The `61 13` is the
honest answer, not a fault. Prediction, unverified: it fails identically on every attempt.

**The high CVs decompose correctly under JMRI's definitions.** Three checks, in
increasing order of what they rule out:

1. `Z21XNetMessage.getZ21ReadDirectCVMsg` builds the identical telegram to
   `cmd_z21_cv_read`, offset and 16-bit split included.
2. `Z21XNetProgrammer.message` rebuilds the echoed CV from both bytes and discards a
   reply that does not match, exactly as `CvMatcher` does. Its `getCanRead` returns true
   for any CV in direct mode - "z21 allows us to specify the CV in 16 bits."
3. The values themselves land where the definitions say. CV508-512 (Swiss Dimming 1-5,
   low three bits mode and high five bits brightness) all read 248 = mode `normal`,
   brightness 31 of 31. CV516/519/522/525 are the F2-F5 sound samples and hold sample
   numbers. **CV746, 749, 752, 755, 758 and 761 are flag bytes whose only documented bits
   are 3 (`Stand`) and 6 (`Drive`), and every one of them read 8 or 72** - those two bits
   and nothing else, six times over. CV527 does the same one block earlier.

The third check is the one that tests addressing rather than encoding: a read landing on
the wrong CV would have to preserve a three-CV repeating structure and still put only
documented bits in only the flag positions.

What this does NOT establish: that a CV which answered `0` is implemented at all. About
500 of them read `0`, and an unimplemented CV returning `0` is indistinguishable from one
holding `0`. `HIGHEST_EXERCISED_CV` stays at 511 and `backup --all` keeps warning. The
definitive test is still a known value written over POM and read back over the Z21 opcode
(issue #43).

**Control, below the boundary:** JMRI gives CV134 `Asymmetrical DCC Threshold` a default
of 106, and the sweep read 106.

## CV100 does not acknowledge, reproducibly — SETTLED 2026-08-19

Four runs against the MS450P22 on the programming track, immediately after the sweep
first found it.

| run | command | CV99 | CV100 | CV101 |
| --- | --- | --- | --- | --- |
| 1-3 | `cv read 99-101 --mode service` | ok, 0 | **61 13** | ok, 0 |
| 4 | `cv read 100 --mode service` | - | **61 13** | - |

Runs 1 to 3 read all three CVs **inside one service-mode session**, which is what makes
them worth more than three separate reads: the neighbours answering in the same session
rules out the session, the gap, the wheel contact and the track. The silence belongs to
CV100 and to nothing else around it. Run 4 removes the last variable - CV100 was second
in the group each time - by reading it alone: same `61 13`, after the session-gap retry
had already run.

So the answer is stable and CV-specific. It is not a transient, and the sweep's single
hole was not a flake.

**Why**, from JMRI's ZIMO definition rather than from this bench (see the documentary
subsection above): CV100 is `Current asymmetry voltage`, read-only, "the asymmetry the
decoder measures right now, in tenths of a volt". A service-mode read is a sequence of
verifications the decoder answers with current pulses, and it can only succeed if the
value holds still throughout; a live measurement has no reason to, and on the programming
track there is no asymmetry to measure at all. That mechanism is consistent with
everything measured here and is not itself proven here - what is proven is that CV100
never answers and its neighbours always do.

**What the tool does with it is already right.** `no_response` in a backup, exit 10 as a
single read. The decoder genuinely did not acknowledge; recording anything else would
invent a value.

One wart, worth its own issue: the `61 13` hint says to check the wheel contact and warns
about the 750 mA programming output. In run 1 to 3 two other CVs answered in that same
session, so contact and current cannot be the cause, and the advice sends an operator to
look at the track for a decoder that is talking to them.

## A CV above 511 checked against a known value — SETTLED 2026-08-19

The sweep established that every CV from 512 to 1024 answers. It did not establish that
the answers belong to the CVs they are labelled with, and roughly 500 of them are zeroes
that an out-of-range read would also produce. This settles that, for one CV, by putting a
known value there through a **different opcode family** than the one that reads it.

The locomotive has no speaker fitted, so the planned listening test (CV523 is "Volume on
Key F4" in JMRI's ZIMO definition) could not run. The cross-encoding half does not need
one.

| step | track | what | result |
| --- | --- | --- | --- |
| 1 | prog | `cv read 513-530 --mode service` | baseline, 18 of 18 ok, CV523 = 128 |
| 2 | main | `cv write 523 20 --track main --address 3 --no-verify` | sent blind; POM has no feedback |
| 3 | prog | `cv read 513-530 --mode service` | **exactly one row changed: CV523, 128 -> 20** |
| 4 | prog | `cv write 523 128 --track prog --verify` | `verified: true` |
| 5 | prog | `cv read 513-530 --mode service` | identical to the baseline, no differences |

The write went out as POM (`E6 30`, ten-bit, zero-based) and the read came back as the Z21
opcode (`23 11`, sixteen-bit, zero-based). Two encodings with different field layouts agree
on which register is CV523, and CV523 is above the boundary the sweep's warning is about.

**Reading a RANGE rather than the single CV is what makes step 3 evidence.** A blind write
that landed somewhere else would show up as a change at the wrong number; a check of CV523
alone would have missed it. Nothing outside the target moved.

### The same two encoders agree with JMRI

The bench cannot rule out both of our encoders sharing a compensating error. A third
implementation can. JMRI's `XNetMessage.getWriteOpsModeCVMsg` and
`Z21XNetMessage.getZ21ReadDirectCVMsg` produce the same bytes as ours - for CV523 the POM
address bytes are `EE 0A` from both, and the Z21 read is byte for byte identical
including the zero-based offset. JMRI reaches the POM bytes by a different route
(`((cv - 1) & 0x0300) / 0x00FF` for the high bits, `(cv & 0xFF) - 1` for the low byte),
and the frames still match at every value tested, boundaries included. For a shared error
to explain the bench result, the same compensating error would have to exist in JMRI's two
encoders as well.

One difference is real but harmless: at CV256, 512, 768 and 1024 JMRI's low-byte
expression yields `-1`, which its own `setElement` stores unmasked. It reaches the wire
correctly anyway - the traffic controller casts to `byte`, and the parity XOR is masked -
but the monitor displays those writes as "CV 0". Reported upstream as JMRI issue #15399.
Our `pom_cv_fields` computes `(cv - 1) & 0xFF` and has no such intermediate.

### What is still open

That a CV which answered `0` is implemented at all. That is the ordinary silence-versus-zero
ambiguity and it applies at every CV number, not only above 511 - it is not a property of
the high range and no read can settle it.
