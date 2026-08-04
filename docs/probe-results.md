# YD7010 capability probe — hardware results

- Command station: YaMoRC YD7010, XpressNet **4.0**, command station id **0x12** (Z21 family)
- Port: `/dev/cu.usbmodem7010A00011943` (LI-USB `FF FE` framing required)
- Decoder: ZIMO MS450P22, address 3. **On the main track** for the R1 / POM section; moved
  to the **programming track** for the R2 / R4 service-mode section below.
- Run: 2026-08-04

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

The speed step question was one of the five left open in the design. It is answered:
this locomotive runs 128 steps.

## R1 — POM CV read: NOT established

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
| 265 | `23 11 01 08` | `63 15 09 00` | **0** | sound project |
| 266 | `23 11 01 09` | `63 15 0A 40` | **64** | master volume |

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
