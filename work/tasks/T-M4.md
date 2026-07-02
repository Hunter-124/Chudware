---
id: T-M4
title: CAM overhaul: feeds/speeds DB unification with engine materials registry
workstream: M
owner: opus-vane
status: done
branch: task/T-M4-cam-overhaul
day: 4
depends_on: []
---

## Context

CAM overhaul wave 1. Resolves GitHub issue #2: CuttingDB's hardcoded 25-material table is
disconnected from the shared engine/materials registry (T-F2) — feeds/speeds and FEA use
different material truths. Audit: work/audits/CAM_AUDIT.md §3.2, §T-M4.

## Files / areas touched

- core/cam/feeds.{h,cpp}, core/cam/session.cpp, the engine/materials registry schema
  (additive `cam` sub-section), services/mcp/cam_tools.py (cam_materials, cam_add_setup)

## Definition of done

- [x] Materials registry schema gains an additive `cam` sub-section (sfm_mill/sfm_drill/
      sfm_turn/hss_factor/fz_ref/fz_ref_dia/stepover_frac/stepdown_frac/plunge_frac/cut_feed_ref)
      — added to the 12 registry alloys that correspond to a CuttingDB machining group (each with
      a linked cutting-group `id` + per-row `provenance`). registry.py gains `cam_data`/
      `cam_material_id`/`cam_materials`. Additive-only: zero mechanical/FEA fields touched.
- [x] CuttingDB::builtin() loads from the registry with in-code fallback (headless build intact)
      — `CuttingDB::merge_cam()` + seed-then-merge `builtin()` reads registry.json (env
      `AICAD_MATERIALS_REGISTRY`, else appdata `materials/registry.json`); absent/unparseable ⇒
      in-code table stands. Proven both directions in test_cam_tools (set ⇒ modified value 4321
      flows through; unset ⇒ in-code 900).
- [x] cam_materials() reflects the registry's live material list; cam_add_setup accepts any
      registry material ID — cam_tools wires the core at the bundled registry (setdefault, never
      overriding an explicit path); `cam_add_setup` translates a registry id/alias to its cutting
      group via `registry.cam_material_id` (bare group ids pass through; cam-less registry
      materials accepted + noted).
- [x] Values for the existing 25 materials preserved byte-identically in the migration
      (no silent feeds changes — diff-tested) — the 12 fold-in sections were transcribed
      byte-exact from the built core's `cam_materials` RPC; the 13 CuttingDB-only groups stay
      in-code (no fabricated FEA rows). Two diff-tests: C++ ctest asserts merged==in-code across
      all 25×14 fields against the source registry; Python asserts registry cam == live core.
- [x] core build + ctest + mcp-venv cam tool tests green; `graphify update .`; close issue #2 ref
      — core build (OCCT ON) links; test_cam 50/50; materials pytest 23/23; cam_tools smoke ALL
      PASS. graphify updated at closeout. **Closes GitHub issue #2** (CuttingDB ↔ engine/materials
      registry unification).

## Closeout (opus-vane, 2026-07-02)

- **Design + cross-team note**: `work/questions/T-M4.md` — seed-then-merge architecture (in-code =
  byte-identical fallback + diff-test baseline; registry folds cam fields on top at runtime), the
  no-fabrication ruling (13 CuttingDB-only groups stay in-code, no invented FEA rows), and the one
  additive schema touch for the sim/FEA co-owners (zero mechanical fields changed).
- **Gate** (`bash scripts/gate.sh`): see the REVIEW check-in line for the verdict. Byte-identical
  + runtime-load verified against the real engine (`AI_CAD_LIBAICAD` set); cam ctest is inside the
  50/50 core ctest stage. Known pre-existing gate reds (check.py audits, pytest 0-collected venv
  gap) are not mine.
- **Scope**: `core/cam/session.cpp` was listed in the task but needed no change — `cam_materials`
  already returns `CuttingDB::builtin().to_json()`, so the singleton fold-in flows through
  unchanged. `services/mcp/server.py` untouched. cam_tools.py edits are small/append-only (M3/M5
  share the file).

## Driver notes (Fable)

- The registry (T-F2, merged) is REGISTRY-AUTHORITATIVE with provenance + condition fields —
  your cam sub-section must carry provenance too (cite the CuttingDB's original source).
  NEVER fabricate feeds data (T-F2's machinability_pct precedent: unseeded > invented).
- Registry schema is a CONTRACT-ADJACENT change: additive only; if anything non-additive
  seems needed, work/questions/T-M4.md first.
- cam_tools.py shared with M3/M5 — append-only, small commits.
