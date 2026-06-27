# Phase 1 Review — track: engine-new-mcp-agent

Branch: `feat/gui-rewrite` · Date: 2026-06-27  
Ownership: `engine/dfm/**`, `engine/intake/**`, `mcp_server/**`, `agent/**`

Summary: **7 fixes** (8 files) · **6 flags** · **2 tests added** · **no commits**

Fast unit tests re-run green after edits:
`engine.dfm.test_process_profiles` 42/42, `engine.intake.test_intake` 84/84,
MCP import chain (`cad_tools`/`gsl_tools`/`intake_tools`) clean.
`agent.test_loop_mock` / `agent.test_compaction` require `build123d` (not installed in the
review shell); logic-only paths verified by import. DLL-gated `mcp_server/test_cad_tools.py`
not run (no staged `libaicad.dll`).

---

## Fixed

### F1. `mcp_server/cad_tools.py` — `_LAST_VOLUME` seeded on all doc-creation paths
The GSL Stage-1a write-diff (`_write_diff`) reports a signed volume delta when
`_LAST_VOLUME` has a numeric baseline. Before seeding, the first `add_features` on a fresh
part degraded to `"volume now X mm^3"` instead of `"volume 0.0→X (+X)"`.

Baseline is now seeded on:
- `new_part`: `_LAST_VOLUME[did] = 0.0`
- `.step+` open (`import_file` native path): volume from `doc_open` when reported
- `import_file` (STEP/other): volume from importer when reported

Post-op update (`_LAST_VOLUME[d] = vol_after`) unchanged. Covered by
`test_new_part_seeds_volume_baseline_for_write_diff` (DLL-gated).

### F2. `engine/intake/intake.py` — `coupon_spec` dead fallback removed
`shaft_in_bore` used `(feat.uncert_mm and 0) or 5.0` — always `0`/`None`, never a real
diameter. Replaced with `_dim_value(feat.dims, "dia") or 5.0`.

### F3. `engine/intake/__init__.py` — package-level public API re-export
`from engine.intake import classify` previously raised `ImportError` (only the
`intake` submodule was exposed). Re-exported the full public surface from
`engine.intake.intake` with an explicit `__all__`. Added `test_package_reexports_public_api`
(84/84 intake tests pass).

### F4. `engine/intake/intake.py` + `mcp_server/intake_tools.py` — public `worst_tier`
`submit_part_photos` reached into private `_tier_index`. Added public `tier_index` and
`worst_tier`; MCP adapter now calls `ik.worst_tier`.

### F5. `engine/dfm/process_profiles.py` — `_strong_tie` simplified
Removed dead double-sort + unreachable `cnc_mill` swap. Single sort with explicit tie-break:
`key=lambda kv: (-kv[1], 0 if kv[0]=="cnc_mill" else 1)`. Ambiguity tests still pass.

### F6. `agent/loop.py` `PLAN_PROMPT` — FDM-default fit anchors
Replaced stale `"slip 0.1-0.3 / press ~0.05"` (textbook, unprintable on FDM) with the
load-bearing FDM diametral clearances from `process_profiles` (slip 0.40, location 0.20,
press 0.10, free 0.50). Peg/pin example updated to reference process clearance stacking.

### F7. `agent/prompts/planner.md` — same FDM anchor alignment
Planner prompt dimension anchors now match FDM defaults and reference the PROCESS BLOCK when
another process is bound.

---

## Reviewed — no change required

### `engine/dfm/process_profiles.py` + tests (42/42)
Solid stdlib-only module: frozen `PROFILES`, copy-isolated `get_profile`, FDM-default
resolver with ambiguity gate (within-margin rule (a), two-way ties (b), metal+FDM must-block
(c)), `modeled_dim` / `fit_clearance` / `process_block_text`. Test coverage matches plan §5.

### `engine/intake/intake.py` + tests (84/84)
Complete intake brain: §2 schemas, taxonomy, ledger gate, confidence scorer, photo builder,
FitReport/coupon, never-block policy. Pure, no I/O. Parity with `process_profiles` verified.

### `mcp_server/cad_tools.py` — `_face_hist` / `_write_diff` / exception handling
- `_face_hist`: best-effort `doc_query` face histogram; empty `{}` on RPC failure →
  write-diff degrades to volume-only (documented).
- `_write_diff`: semantic summary (face/bore deltas + signed volume); explicit
  `"no geometry change"` when op is a no-op; `volume_delta_mm3` only when baseline known.
- `add_features` exception policy is intentional: checkpoint/revert and write-diff failures
  are swallowed with structured fallbacks (`rolled_back`, `"write-diff unavailable"`) so a
  partial telemetry failure never aborts a successful build. Outer `except` returns `_err(e)`
  for transport-level failures.

### `agent/loop.py` — orchestration
Well-structured loop: plan-first option, convergence (`confirmed` / `stabilized` / cap),
stagnation + same-error limits, noop-change detection, context-overflow auto-compaction,
session persistence across turns, assembly fit-report feedback. Offline guard present.

### `agent/prompts/intake.md`, `agent/providers/*`, `agent/config.py`
Intake prompt is geometry-free normalization (appropriate). Providers follow base contract.

---

## Flagged (not fixed — product/architecture decisions)

### B1. Planner PROCESS BLOCK not wired into in-app C++ orchestrator
MCP path (`intake_tools.set_process`) persists process + can emit `process_block_text`;
in-app `core/agent/**` planner does not yet inject the block. F6/F7 align static anchors to
FDM; full per-process binding still needs orchestrator wiring (out of lane).

### B2. `mcp_server/intake_tools.py` — `dfm_check` false-positive when engine unavailable
Both `eval_thickness` / `eval_draft` wrapped in `try/except: pass`; empty `issues` →
`dfm_pass: True`. Recommend `checks_run` count or explicit "no checks executed".

### B3. `mcp_server/intake_tools.py` — `set_process` material key mixing
`prof.setdefault("material", {})["default"] = material` nests user material inside the
process default object. Harmless today; recommend `user_material` field.

### B4. `mcp_server/cad_tools.py` — `_LAST_VOLUME` never pruned
Module dict grows with doc ids; bounded per session. Add cleanup on doc close for long-lived
servers.

### B5. `engine/intake/intake.py` — bolt-pattern coupon 3-hole layout off-center
Spacing correct; symmetric `[-s,0,+s]` would look nicer. Cosmetic.

### B6. `agent/prompts/shared/cheatsheets/hardware.md` still has generic fit tables
Cross-process reference doc; not contradicted by FDM-specific planner anchors but could
note FDM uses diametral clearances from `process_profiles`.

---

## Tests added

| Test | File | Notes |
|------|------|-------|
| `test_new_part_seeds_volume_baseline_for_write_diff` | `mcp_server/test_cad_tools.py` | DLL-gated; covers F1 |
| `test_package_reexports_public_api` | `engine/intake/test_intake.py` | covers F3/F4 |

---

## Files touched (this track)

| File | Change |
|------|--------|
| `mcp_server/cad_tools.py` | F1: `_LAST_VOLUME` seeding (prior) |
| `mcp_server/test_cad_tools.py` | F1 test (prior) |
| `engine/intake/intake.py` | F2 coupon fix; F4 `tier_index`/`worst_tier` |
| `engine/intake/__init__.py` | F3 public re-exports |
| `engine/intake/test_intake.py` | F3/F4 test |
| `engine/dfm/process_profiles.py` | F5 `_strong_tie` |
| `mcp_server/intake_tools.py` | F4 `worst_tier` |
| `agent/loop.py` | F6 PLAN_PROMPT anchors |
| `agent/prompts/planner.md` | F7 planner anchors |
