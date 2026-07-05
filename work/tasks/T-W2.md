---
id: T-W2
title: Sketch completion suite - auto-coincident pass, constraint advisor, sketch_health
workstream: W
owner: opus-reed
status: done
branch: task/T-W2-sketch-completion
depends_on: []
lane: ai-copilot
kind: new
priority: 2
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.6). Hunter directive: endpoints that touch
must become coincident automatically, and agents (and the GUI later) need a narrated,
actionable read of sketch constraint state instead of guess-and-retry. The C++ solver
already exposes everything needed: `System::analyze()` returns dof, redundant_tags,
conflicting_tags (core/solver/nsolve.h L105-114); per-entity under/full/over status
exists in the sketch SolveState; `doc_sketch_regions()` (core/doc/session.h L443)
enumerates closed regions. Missing: auto-constraint inference and narration.

## Files / areas touched

- `core/doc/session.h/.cpp` — new RPCs: `doc_sketch_auto_constrain`, `doc_sketch_health`
- `core/api/libaicad.cpp` — dispatch entries for both
- `core/sketch/` — helper for endpoint-proximity pair finding (read-only scan)
- `services/mcp/gsl_tools.py` (or new `sketch_tools.py`) — `sketch_health` MCP tool;
  extend `check_sketch` to include the advisor output
- tests: `core/doc/test_sketch_autoconstrain.cpp` (mirror test_interference.cpp style),
  python smoke via MCP

## Steps

1. Read docs/ADVISOR_ARCHITECTURE.md §2.6 and contracts/advisor.md §3 (advisory item
   shape — sketch_health findings must be emittable as advisory items).
2. `doc_sketch_auto_constrain(doc_id, sketch_id, {tol_mm=1e-4, apply=true|false})`:
   scan entity endpoints (lines, arcs, splines, slots ends); pair points within tol
   that are not already coincident-constrained; when apply=false return proposals
   `[{a: {entity, handle}, b: {...}, gap_mm}]`; when apply=true add the coincident
   constraints via the existing constraint-add path, re-solve, and report
   `{applied, skipped_conflicting, regions_before, regions_after}` using
   doc_sketch_regions to show closure gained. Never apply a proposal whose addition
   makes the system conflicting (add, analyze, roll back on conflict).
3. `doc_sketch_health(doc_id, sketch_id)`: one JSON with — closure (regions, open
   chains with the gap endpoints named), dof + free-variable narration in plain words
   ("circle C1 radius; line L4 end x/y" — map var indices back through var_map),
   per-entity status (under/full/over), redundant/conflicting constraint tags with
   removal suggestions, and a proposed constraint schedule to reach fully-constrained
   (geometric constraints first, then driving dimensions; heuristic order is fine,
   document it).
4. MCP: expose `sketch_health` returning the narrated block + raw JSON; fold the
   auto-constrain call into the sketch edit path behind config
   `sketch.auto_coincident=true` (default ON per Hunter directive) so it runs after
   sketch-modifying ops; also expose explicit `sketch_auto_constrain` tool.
5. Tests (C++): open-rectangle-with-touching-corners gains 4 coincidents and closes;
   near-miss beyond tol untouched; conflicting proposal rolled back; health narration
   names the correct free variables on a known under-constrained sketch.
6. MCP freshness: update server instructions (sketch loop: edit → sketch_health →
   constrain), cad_guide sketch topic; `graphify update .`.

## Definition of done

- [x] Auto-coincident closes touching-endpoint profiles automatically (default on),
      with rollback safety on conflicts
- [x] `sketch_health` narrates closure, DOF in plain words, per-entity status,
      redundant/conflicting tags, and an ordered constraint schedule
- [x] Both exposed via MCP; check_sketch enriched; server instructions + cad_guide updated
- [x] C++ tests pass in the build (see linux-build memory: host/build vs build-linux)
- [x] Output shapes emittable as aicad.advisory/1 items (severity, finding,
      candidate_fixes, refs)

## AI hints

- `graphify query "sketch constraint solver analyze dof"` and
  `graphify explain "doc_sketch_regions"` first.
- Constraint dispatch lives around core/sketch/sketch.cpp L1892-1978; add_* helpers
  above it. var_map for narration: SolveState in sketch.h L50-60.
- ui/imgui sketch entry is NOT wired (known blocker, separate task) — verify via
  headless RPC tests, not the GUI.

## Progress log
- 2026-07-05 01:00 UTC [opus-reed] Sketch completion suite landed on task/T-W2-sketch-completion (5 commits, pushed).

WHAT: (1) auto-coincident pass — proximity-pairs entity endpoints within tol (default 1e-4mm), adds coincident constraints with per-proposal add->analyze->rollback-on-CONFLICT safety (redundant coincidents KEPT, only conflicting skipped); fires automatically on every sketch-mutating batch (add_feature + sketch_* ops) via maybe_auto_constrain in apply_ops_locked, gated on doc meta sketch.auto_coincident (default ON). (2) doc_sketch_health — narrates closure (open-chain endpoints named), DOF free variables in plain words (SVD null-space + per-entity-type name table mirroring allocate_entity), per-entity under/full/over status, redundant/conflicting tags with removal suggestions, heuristic geometric-then-dimension schedule, and the same findings as frozen aicad.advisory/1 items. (3) MCP: new sketch_health + sketch_auto_constrain tools (gsl_tools.py); check_sketch now routes through the read-only sketch_health RPC (also fixes a pre-existing dispatch bug where it called the nonexistent doc_sketch_solve) and folds in open_endpoints/dof_narration/schedule/advisory; server.py instructions + _GSL_GUIDE document the edit->sketch_health->constrain loop; manual regenerated (clean).

VERIFIED: core ctest 44/44 (new test_sketch_autoconstrain 8/8: 4-corner rectangle gains 4 coincidents + closes; beyond-tol near-miss untouched; conflicting proposal rolled back (skipped=1); redundant kept; on-edit auto-fire proven by PERSISTED coincident count 0->4 via add_feature, not geometry; default-OFF = 0; id-collision mints c5+ on a pre-constrained sketch; health names free vars + advisory/1 shape). MCP: gsl_tools 26/26 + freshness 5/5 with libaicad staged; live end-to-end sketch_health through the ctypes bridge verified. bash scripts/gate.sh => RESULT: PASS.

PRE-EXISTING (not from this task, empirically confirmed): services/mcp pytest = 8 failed/241 passed/34 errors on BOTH master baseline and this branch (identical counts) — FEA/CFD (drag_n KeyError etc.), ECAD collection errors, regen seam, and the 2 check_sketch tests. The check_sketch tests fail because their _rect_sketch fixture builds a legacy-FLAT sketch (entities/plane at feature top level) the validator no longer accepts (needs params/refs nesting) AND hardcodes id "s1" add_features never mints — spawned a follow-up task to migrate that fixture. Auto-coincident only pins points already within 1e-4mm so it cannot move any FEA/CFD geometry number.

CAVEAT for next driver: auto-coincident fires from apply_ops_locked (add_feature + sketch_* ops). doc_sketch_exit persists dragged coords by mutating the feature directly (not through apply_ops_locked), so auto-coincident won't fire on interactive exit-after-drag — moot today since the ui/imgui sketch entry is unwired, but worth wiring when it lands.
