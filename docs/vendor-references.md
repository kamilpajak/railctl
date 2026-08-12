# Vendor reference documents

Documents this project's data was checked against. None is committed to the repository:
they are the vendors' copyrighted manuals, freely downloadable but not redistributable, so
what is recorded here is the exact identity of the copy used - enough to fetch the same
document and to notice when the vendor replaces it.

## ZIMO MS/MN decoder instruction manual (English)

- URL: https://www.zimo.at/web2010/documents/MS-MN-Decoders_EN.pdf
- Retrieved: 2026-08-12
- SHA256: 9e63a0b607d47d3ee34078e5b7af6873a79bbabe2e5813d8e3656711e93725da
- Pages: 100. Covers the MS450P22 on this bench by name.
- Local copy: `~/Developer/Personal/reference/ZIMO-MS-MN-Decoders_EN.pdf` (not in git)

What the catalog was verified against, with pages:

| claim | page |
| --- | --- |
| CV17+18 pair range 1-10239, defaults 192/128 (CV18 full byte is correct) | 31 |
| CV265 selects the locomotive type from a sound collection | 28 |
| CV144: MS = confirmation jingle (bit 4); MN >= v5.7.0 flashes lights | 29 |
| CV29 bit table, incl. bit 4 gating CVs 67-94 | 29, 31 |
| CV7/65 version read-only; CV8 = 145, hard reset by writing 8 | 30 |
| CV250 decoder type, CV251-253 serial, read-only | 29 |
| CV10 on MS = Motorola subsequent addresses 0-3 (MX meaning differs) | 29 |
| CV9, CV56, CV147-149 regulation semantics | 34 |
| CV125-132 effect codes carry lighting, smoke (72/80) and uncoupler effects | 47 |
| Smoke system: CV133, CV137-139, CV351/352/355, CV353 | 47-49 |
