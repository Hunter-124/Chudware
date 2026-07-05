---
id: T-W2
title: Sketch completion suite - auto-coincident pass, constraint advisor, sketch_health
workstream: W
owner: opus-reed
status: review
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

- [ ] Auto-coincident closes touching-endpoint profiles automatically (default on),
      with rollback safety on conflicts
- [ ] `sketch_health` narrates closure, DOF in plain words, per-entity status,
      redundant/conflicting tags, and an ordered constraint schedule
- [ ] Both exposed via MCP; check_sketch enriched; server instructions + cad_guide updated
- [ ] C++ tests pass in the build (see linux-build memory: host/build vs build-linux)
- [ ] Output shapes emittable as aicad.advisory/1 items (severity, finding,
      candidate_fixes, refs)

## AI hints

- `graphify query "sketch constraint solver analyze dof"` and
  `graphify explain "doc_sketch_regions"` first.
- Constraint dispatch lives around core/sketch/sketch.cpp L1892-1978; add_* helpers
  above it. var_map for narration: SolveState in sketch.h L50-60.
- ui/imgui sketch entry is NOT wired (known blocker, separate task) — verify via
  headless RPC tests, not the GUI.
