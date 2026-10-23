# Clean-room standard-part specification tables (`services/parts/data/bolts/`)

These YAML tables are the deterministic spec-sheet lookup source for the
standard-part matcher in `core/parts/`. They map a standard designation
(ISO 4762 M3, deep-groove bearing 608, …) to nominal dimensions and to a
`core/parts` catalog **family + size**, so a normalized manufacturer part number can
resolve to real, editable geometry via the `parts_make` RPC.

## Provenance & license (important)

**These tables are NOT copied from the BOLTS project.** BOLTS
(github.com/boltsparts/BOLTS) is **GPL-3.0**, and the project's clean-room /
GPL-clean posture (`contracts/licenses.md`) forbids vendoring GPL data. Bundling
any GPL data would require an explicit licensing review first.

Instead these values are transcribed from the project's own single source of truth
for hardware dimensions — `services/agent/prompts/shared/cheatsheets/hardware.md` and the
C++ catalog tables in `core/parts/parts_data.h` — which carry published **ISO/DIN
nominal dimensions**. Dimensional standards are not copyrightable (same posture as
the IPC-7351 footprint geometry row in `contracts/licenses.md`), so this table is
clean-room original data, GPL-clean.

## Format (BOLTS-blt-compatible, documented → interop)

The schema is a small, documented subset chosen to interoperate with BOLTS `.blt`
concepts (a *collection* of *standards*, each a *table* of size → parameters). The
loader (`services/parts/bolts_loader.py`) parses this format; a user who points it at
their own installed BOLTS checkout (their license, their choice) can be supported by
the same parser without us redistributing GPL data.

```yaml
collection: <id>
standards:
  - id: "ISO 4762"          # governing standard designation
    name: "Socket Head Cap Screw"
    family: shcs             # core/parts catalog family key (parts_make)
    kind: fastener           # fastener | bearing
    needs_length: true       # geometry needs a length_mm
    aliases: ["DIN 912"]     # alternate designations that map here
    sizes:                   # designation -> nominal params (mm)
      "M3": {d: 3.0, pitch: 0.5, head_d: 5.5, head_h: 3.0, hex_key: 2.5}
```

All lengths are millimetres. `null`/absent means *not specified*, never `0`.
