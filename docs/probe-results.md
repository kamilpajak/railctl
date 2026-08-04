# YD7010 capability probe — hardware results

- Command station: YaMoRC YD7010, XpressNet **4.0**, command station id **0x12** (Z21 family)
- Port: `/dev/cu.usbmodem7010A00011943` (LI-USB `FF FE` framing required)
- Decoder: ZIMO MS450P22, address 3, on the **main track**
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

### Next step for R1

Read any CV through the YaMoRC tool's own **"Loco POM"** mode (Prog.Track CV Programming
→ Mode → Loco POM). That tool has a dedicated "No Reed — No read result" status, so the
vendor anticipates this failure.

- If the vendor tool **can** read via POM, the capability exists and the gap is in the
  XpressNet path used here — possibly it is only exposed over Z21 LAN.
- If the vendor tool **cannot**, POM read is unavailable on this hardware and the design
  must fall back to service mode on the programming track as the primary CV path.

## R2, R4 — service mode on the programming track: SETTLED

**Service-mode CV reading works, through the Z21 opcode `23 11` and only that one.**

| Opcode | Result |
| --- | --- |
| `23 11` — Z21 service read, 16-bit CV | **works, every time** |
| `22 15` — Lenz legacy direct read | silent |
| `22 18` — Lenz extended, band CV1–255 | silent |
| `22 19` — Lenz extended, band CV256–511 | silent |

Reproduced over three alternating rounds on the same CV: `22 15 01` returned only the
interface ACK each time, `23 11 00 00` returned `63 14 01 03` each time. This is
consistent with the station reporting command station id `0x12`, the Z21 family.

### CVs read from the ZIMO MS450P22

| CV | Request | Reply | Value | Cross-check |
| --- | --- | --- | --- | --- |
| 8 | `23 11 00 07` | `63 14 08 91` | **145** | ZIMO manufacturer id — the known constant |
| 29 | `23 11 00 1C` | `63 14 1D 0E` | **14** | bit 3 set, RailCom enabled at the decoder |
| 250 | `23 11 00 F9` | `63 14 FA 06` | **6** | decoder type 6 = MS450, matches the hardware |
| 265 | `23 11 01 08` | `63 15 09 00` | **0** | sound project |
| 266 | `23 11 01 09` | `63 15 0A 40` | **64** | master volume |

**CVs above 255 are reachable** — but through the 16-bit address of `23 11`, answered on
the `63 15` band, not through the Lenz extended opcodes the R2 check was written around.
CV265 and CV266 both read correctly.

The request is **zero-based** (`23 11 00 07` reads CV8) and the reply echo is
**one-based** (`63 14 08`). Band replies carry the offset from the band base: `63 15 09`
is CV256 + 9 = CV265.

### Consequence for the design

The design chose **POM on the main track as the primary CV path**, with service mode
secondary. The measurements invert that: POM returns nothing, service mode through
`23 11` returns everything asked of it, including the high CVs the ZIMO backup needs.
**The primary CV path should be service mode on the programming track using `23 11`.**

The R2 check must be rewritten: as written it probes `22 18` and `22 19`, which this
station does not answer, so it reports "unknown" for a capability the station has by
another route.

### Operational note

Each service-mode read makes the YD7010 switch its output between the main and the
programming track with an audible relay. Space reads out rather than firing them back to
back — consecutive reads without a pause returned silence where spaced reads succeeded.

## Measurement note

`TC` in the YD.Control telemetry read **0 mA** throughout, with track voltage at 15.1–15.2 V,
while the decoder was demonstrably powered and responding. **Do not use `TC` to decide
whether a locomotive is on the track** — it does not resolve a standing sound decoder's
current draw. That reading caused a wrong diagnosis during this session.
