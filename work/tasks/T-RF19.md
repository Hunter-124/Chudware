---
id: T-RF19
title: RF stream-Z long-tail oracle modules cutover (node R4e)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF19-longtail-cpp
depends_on: [T-RF15, T-RF16, T-RF17, T-RF18]
lane: modeling-core
kind: overhaul
priority: 3
---

## Context

After R4a–d, the remaining engine_oracle/ modules are the long tail: hydraulic, piping,
intake, vision, research, render (headless PyVista screenshots), structural, tolerance,
vendor_parts leftovers, and engine_oracle/doc format/export remnants (formats,
tessellate, op_registry). ~25 modules total before this stream started.

## Scope

Sub-split at dispatch into per-module leaves (each small enough for one subagent):
port to the matching core/ subsystem, corpus parity, flip routing, delete the Python
module in the same commit. Modules with no live consumer get deleted, not ported
(YAGNI — prove the consumer first via the MCP tool surface and GUI call sites).

Also evaluate (from the 2026-07-16 gui-session Deferred section): item 9 "full nested
history for imported parts" needs an engine RPC `doc_import_feature_tree` — plain
STEP/mesh imports have no parametric history; assess whether native .step+ sources
can expose it and whether it's in scope for the doc subsystem or genuinely new work.

## Definition of done

- [x] Every remaining oracle module either ported+deleted or proven dead+deleted.
- [ ] engine_oracle/ reduced to worker.py shell + __init__ shims for T-RF20.
- [ ] gate.sh green per leaf; REFACTOR_LOG handoffs; web_parity inventory ~empty.

## Sub-split plan (2026-07-22, integrator recon)

Worker-registry seams (engine_oracle/worker.py): doc.formats, vendor_parts, ecad.rpc,
sim/em.worker_rpc, parts_hub.worker_rpc, reveng_rpc, sourcing (DONE — T-RF18), kernel.
MCP-direct-import modules (no worker RPC): hydraulic, piping, structural, tolerance,
intake, vision, research, dfm, requirements, mechanical, materials.

Leaf split at dispatch (each = one subagent):
1. doc/formats.py + tessellate/op_registry remnants (4,165 ln in doc/) → core/doc
2. parts_hub stack (7,441 ln: part_search/get/place, gui_rpc, FTS index, source,
   taxonomy) → core/parts or core/sourcing (match R4d's pattern)
3. reveng_rpc.py (501 ln) → evaluate consumers first (MCP reveng tools just split in
   T-RF8 — port or prove thin)
4. hydraulic (684) + piping (2,984) → core/ (shared fluid code; one leaf)
5. structural (2,457, PyNite) → core/structural (subprocess-orchestrate like firmware)
6. tolerance (258) + dfm (719) + mechanical (369) + materials (261) + requirements
   (744) — small modules, one leaf, port-or-prove-dead each
7. intake (1,424) → evaluate: likely services-side (agent intake), maybe NOT engine
8. vision (6,046) + research (5,087) — biggest; evaluate carefully: vision is
   photogrammetry (heavy deps — cv2/trimesh; a C++ port may be a subprocess-orchestration
   port or a "keep as services-side tool" verdict); research is web_search/fetch
   (agent tooling — candidate for services-side re-home, NOT core)
9. kernel.py (657, build123d run_model legacy) — the T-RF20 decision point; do NOT
   port (needs a Python interpreter); delete-or-thin-wrap per the T-RF20 analysis

HOLD-BACK: sim/em/* — the EM solver family is a DEFERRED plan (T-SIM11, deferred: yes).
Do not port or delete sim/em in this stream; it is the documented closeout exception
until T-SIM11 un-parks. Also hold: openfoam* (R4b's follow-up node, dispatch separately).
