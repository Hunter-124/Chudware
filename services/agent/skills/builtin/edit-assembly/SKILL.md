---
name: edit-assembly
description: Build assemblies with interface-first mating — model the mating part first, mate with plain-word faces, then interference + interface_check before done.
inject: auto
triggers:
  keywords: [assembly, mate, mates, component, subassembly, fit together, put together, assemble, bolt pattern]
  tools: [new_assembly, asm_add_component, asm_mate, solve_mates, check_interference, interface_check]
  phases: [assembly]
---

# Edit Assembly

## Interface-first mating (mandatory)

1. Identify every mating interface (hole pattern, coaxial bore/shaft, flush faces, fastener stack).
2. **Model or measure the first side completely** — dims live in that part. Never invent both sides in parallel.
3. Build/import the second part **from those measured dims** (with `fit_calc` clearances where needed).
4. Only then place and mate.

## Sequence

1. `new_assembly(name)` → keep `doc_id`.
2. Place components: `asm_add_component(path)` (or place from open parts). Prefer `part_search` + `part_place` for catalog hardware.
3. Constrain with plain-word queries: `asm_mate(type, a, b)` — concentric + coincident is the classic bolt-in-hole seat. Face language: "the top face of bracket", "the shaft outer face".
4. Seat geometry: `solve_mates()` after mates are defined.
5. Validate:
   - `describe_assembly(doc_id)`
   - `check_interference(doc_id)` — hard clash
   - `interface_check(doc_id)` — hole patterns, coaxiality, flush contact, fastener pass-through (catches errors interference misses)
6. Motion (if needed): `simulate_motion(mate_id, start, stop)` and re-check interference across the sweep.
7. Done: `verify_battery(doc_id)` with no FAILs.

## Rules

- **Verify-before-done** after every mate batch and after motion.
- **Do not guess hole centers** on the second part — measure the first.
- Fasteners: search the parts library (`M6 socket head cap screw`, not "hex bolt thing").

## Deeper docs

- `cad_guide('assembly')`, `cad_guide('motion')`, `cad_guide('verify')`
- Sourcing phrasing: `cad_skill("source-parts")`
