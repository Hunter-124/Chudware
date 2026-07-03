---
id: T-BL30
title: Engine — boolean fuse/cut/common of disjoint bodies silently succeeds; enforce the single-connected-solid invariant (issue #8)
workstream: backlog
owner: sonnet-quorix
status: doing
priority: 3
lane: modeling-core
kind: fix
depends_on: []
branch: task/T-BL30-disjoint-boolean-invariant
---

## Context

Found by the T-BL19 modeling battery (case `nc_disjoint_fuse`, marked XFAIL against this
bug). GitHub issue #8. Severity ruled low-medium by the foreman (Hunter-delegated,
2026-07-03): valid solids are produced, but a contract-promised invariant is unenforced.

**Contract** (`services/agent/prompts/shared/ops_cheatsheet.md`, BOOLEAN section): a fuse
must produce ONE connected solid; a cut/common must also yield ONE solid piece — disjoint
results fail.

**Actual:** `box A@origin; box B@[100,0,0] (merge:false); boolean{op:"fuse",a:"f1",b:"f2"}`
returns `feature_status.ok == true`, `eval_mass.body_count == 2`, `eval_check.issues == []`.
The fuse silently no-ops; no error is raised.

**Repro:** `AI_CAD_LIBAICAD=$PWD/core/build-occt-linux/bin/libaicad.so PYTHONPATH=$PWD
python3 scripts/modeling_battery.py` — case `nc_disjoint_fuse`. NOTE: only the OCCT build
has the real geometry kernel; `core/build-linux` all-skips the battery.

## Definition of done

- [ ] Boolean fuse whose operands do not intersect/touch fails the rebuild with a clear
      "disjoint fuse" error (or an explicit non-manifold-compound flag — pick one, document
      it in the ops cheatsheet if the wording changes).
- [ ] Same invariant enforced for cut/common producing multiple disconnected pieces, per
      the contract wording.
- [ ] Battery case `nc_disjoint_fuse` flips from XFAIL to expected-pass; no other battery
      case regresses (`scripts/modeling_battery.py` full run against the OCCT .so).
- [ ] Legitimate multi-body workflows are NOT broken: `merge:false` co-existing bodies,
      compound imports, and pattern/mirror flows still rebuild clean (core ctest + per-suite
      pytest green).
- [ ] `graphify update .` + scripts/gate.sh before review.

## Driver notes

- graphify-first: `graphify query "boolean fuse feature implementation"` to locate the
  boolean feature execution path in core/ before reading raw source.
- The check likely belongs where the boolean result shape is accepted — count resulting
  solids/connected components after the BOP and reject per-contract rather than patching
  callers.
- Update `services/agent/prompts/shared/ops_cheatsheet.md` only if the error wording or
  semantics you land differ from what it promises (MCP freshness rule, WORKFLOW.md §6).
- Opus-leaning if the OCCT BOP result-inspection gets gnarly; a Sonnet driver hitting depth
  should stop at repro + failing test + question file per standard protocol.

## Progress log
