---
id: T-G5
title: GUI migration wave 1 - foundation libs (F1-F4 + G1 gate)
workstream: G
owner: unassigned
status: todo
branch: task/T-G5-migration-wave1
day: 6
depends_on: [T-G1]
---

## Context

First wave of the host/ → ui/imgui modular rewrite. `docs/GUI_MIGRATION_DAG.md` is the
single source of truth for node specs — this task = its G0 prep + wave 1 leaves
(F1 viewport library, F2 theme/liquid glass, F3 platform layer, F4 worker/state seam)
+ the G1 CMake-rewire integrator gate. The whole point of the migration is modularity:
strict library boundaries (`ui/viewport`, `ui/imgui/src/theme`, `platform/`,
`services/worker`) replacing host/'s tangle, so run the leaves with the disjoint
write-sets the DAG assigns and let the integrator gate own shared files.

## Files / areas touched

- `ui/viewport/**` (F1), `ui/imgui/src/theme/**` (F2), `platform/**` +
  `ui/imgui/src/platform/*` (F3), `services/worker/**` + `ui/imgui/src/app/**` (F4)
- Integrator gate only: root `CMakeLists.txt`, `ui/imgui/CMakeLists.txt`,
  `ui/viewport/CMakeLists.txt`, `scripts/gate.sh`
- Read-only sources to port from: `host/src/**` per each node's Source row

## Steps

1. Read `docs/GUI_MIGRATION_DAG.md` G0 + F1-F4 + G1 in full; read
   `docs/GLASS_DESIGN_SPEC.md`.
2. G0 prep: capture host baseline screenshots into `scratch/gui_baseline/`.
3. Run F1-F4 as parallel subagents with EXACTLY the write-sets/verify commands the DAG
   specifies (include the graphify-first rule in every subagent prompt; graphify indexes
   ui/abstract+core, not host/src).
4. G1 gate: rewire CMake, build AI-CAD2, `--shot` smoke, `bash scripts/gate.sh`.
5. Append one paragraph per completed node to `docs/REFACTOR_LOG.md`.

## Definition of done

- [ ] `aicad_ui_viewport` compiles standalone, zero `#include` into `host/`
- [ ] Theme helpers compile; no literal colors outside `tokens.h`/`theme.*`
- [ ] Zero OS `#ifdef` outside `platform/`
- [ ] AI-CAD2 routes doc ops through `WorkerClient`
- [ ] `bash scripts/gate.sh` green (known pre-existing failures excepted); `graphify update .` run

## AI hints

- Never run two builds against one build dir concurrently — serialize with the flock
  pattern or dedicated build phases.
- The gate has a known pre-existing `PanelField::selMulti` break in ui/imgui (see agent
  memory gate-selmulti-blocker) — not your regression, but wave 1 may be the right
  moment to actually fix it since you own the seam files now.
