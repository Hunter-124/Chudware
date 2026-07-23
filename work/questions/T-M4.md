# Notes / design — T-M4 (feeds/speeds DB unification with the engine materials registry, Issue #2)

All items **non-blocking** (NOTE, not QUESTION-blocking): the additive `cam` schema is pre-authorized
by the Driver notes, the runtime-load mechanism is DoD-forced, and no material data is fabricated.
Recording the architecture + the one cross-team touch for supervisor/sim-team awareness.

## Architecture (ratified with advisor)

**Seed-then-merge, in-code table is the fallback + the byte-identical baseline.**

- `CuttingDB()` (plain constructor) stays a **pure in-code table** (`builtin_table()`, 25 rows).
  This is the headless fallback and the diff-test ground truth.
- `CuttingDB::builtin()` (shared singleton) seeds in-code, then **locates + reads
  `registry.json` at runtime and merges the `cam` fields on top**. Absent/unparseable registry ⇒
  in-code stands (headless build intact). This is what makes the DoD's *"loads from the registry
  with in-code fallback"* and *"cam_materials reflects the registry's live list"* literally true —
  pure transcription would satisfy neither (it never loads, never reflects live edits).
- Registry gains an **additive `cam` sub-section on the 12 alloys that correspond to a CuttingDB
  machining group** (aluminum_6061_t6↔aluminum, steel_1018↔steel_mild, …). Each section carries the
  10 feeds fields + a linking `id` (the CuttingDB group id) + per-row `provenance`.
- **The 13 CuttingDB-only groups stay in-code** (aluminum_cast, bronze, steel_alloy, steel_tool,
  stainless_316, hardened_steel, acetal, hdpe, wood_hard, wood_soft, mdf, foam, g10). They have no
  FEA alloy in the registry; adding registry rows for them would mean **fabricating mechanical/FEA
  data** (violates the T-F2 "unseeded > invented" precedent). Seed-then-merge means they simply
  remain in the in-code base — no fabricated rows, no cross-team FEA expansion.
- **CuttingDB ids unchanged** (aluminum, steel_mild, …): byte-identical + unbroken `.cam+` refs.
  The registry↔cam link is explicit (`cam.id`), not a hardcoded map.

## Byte-identical guarantee (DoD)

The 12 registry `cam` sections were **transcribed byte-exact from the built core's `cam_materials`
RPC** (not hand-typed). Two tests pin it:
- Python (`materials/test_materials_registry.py::test_cam_values_byte_identical_to_core_table`):
  each registry cam field == the live core value, all 10 fields × 12 materials.
- C++ ctest (`test_cam.cpp`): `CuttingDB::builtin()` (merged with the real registry) == `CuttingDB()`
  (pure in-code) across all 25 × 14 fields — a defaulted-field mismatch cannot slip through.

## Runtime registry location

Core reads (in order): env `CHUDWARE_MATERIALS_REGISTRY` → `appdata_root()/materials/registry.json`
→ (none) in-code fallback. Mirrors `core/agent/paths.cpp` / `config.cpp`. The env override makes the
runtime-load path testable from the repo tree (ctest + mcp-venv point it at
`engine_oracle/materials/registry.json`) and keeps installed/headless working when unset. A future
installer step provisioning registry.json into the data dir is out of this task's scope (noted).

## cam_add_setup accepting any registry material ID

`cam_add_setup` now resolves the material through the registry: a registry id/alias that maps to a
cam-annotated material is translated to the CuttingDB group id (via `registry.cam_material_id`) so
the stored setup uses the id the feeds calculator resolves; a bare CuttingDB id passes through
unchanged; an unknown id is accepted but annotated (feeds fall back — existing behaviour, no error).

## Cross-team touch (sim/FEA — one line)

The registry schema is co-owned with the sim/FEA team. This change is **additive only**: a new
optional `cam` sub-section on 12 materials; **zero** `mechanical`/FEA/`condition` fields changed.
`export_sim_json` output is unaffected (its anti-drift mirror test still passes — cam is not read by
the FEA path). No version bump needed (registry §0: additive optional fields).

## server.py / cam_tools.py

`cam_tools.py` shared with M3/M5 — edits are small + append-only (cam_add_setup validation +
cam_materials enrichment). `services/mcp/server.py` untouched.
