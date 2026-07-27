---
name: edit-part
description: Build or edit a single solid part with editable features, verify after every mutation, and never declare done without verify_battery.
inject: auto
triggers:
  keywords: [part, solid, extrude, revolve, fillet, boss, pocket, shell, model a, design a, make a, make me a, build a, build me a, create a, create me a, model me a, design me a, 3d print a, machine a]
  tools: [new_part, add_features, render_part, describe_part]
  phases: [feature, part_verify]
---

# Edit Part

How to model one solid part in Chudware. Prefer standard editable features over freeform code.

## Sequence

1. **Orient** — `cad_guide()` if the op vocabulary is not already in context. Call `list_parts()` / `describe_part` when a doc may already exist (prefer the part name in narration; pass opaque `doc_id` when multi-doc or ambiguous).
2. **Open or create** — `new_part(name)` for a fresh body; when more than one part is open, pass the opaque `doc_id` on later tool calls; prefer the part **name** in user-facing text.
3. **Interface first** — if this part mates to another, model the **mating interface dims first** (bore, boss, hole pattern, flange face). Read dims from the existing mating part via `measure_part` / `query_geometry` — never parallel-guess both sides.
4. **Build features** — `add_features([...ops...])` with human features: sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell, holes, patterns, mirror. Address faces in plain words ("the top face", "the largest cylindrical face").
5. **Verify each mutation** — after every `add_features`:
   - `describe_part` or `measure_part` on the affected geometry
   - `check_feature_overlap` after boolean-heavy work
6. **Standard parts** — fasteners, bearings, inserts: `part_search(query)` then `part_place(part_id)`. Never approximate a catalog part.
7. **Done gate** — `verify_battery` must show no FAILs before you report done (pass `doc_id` when multi-doc). See `skill://edit-part` resources and `cad_guide('verify')`.

## Rules

- **Verify-before-done.** Mutations without a following describe/measure/check are incomplete.
- **Single source of truth for mates.** The part that already exists owns the interface numbers.
- **Process-aware fits.** Use `fit_calc` before sizing a bore/shaft; do not copy nominal diameters.
- **Rebuild failure** → `regen_diagnose` first; undo when the diagnosis says the approach is wrong.

## Deeper docs

- Op vocabulary: call `cad_guide()` / `cad_guide('review')`
- Verify battery: `cad_guide('verify')`
- Parts library phrasing: `skill://source-parts`
