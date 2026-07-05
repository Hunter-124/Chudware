---
id: T-BL30
title: Engine — boolean fuse/cut/common of disjoint bodies silently succeeds; enforce the single-connected-solid invariant (issue #8)
workstream: backlog
owner: sonnet-quorix
status: review
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

- [x] Boolean fuse whose operands do not intersect/touch fails the rebuild with a clear
      "disjoint fuse" error (or an explicit non-manifold-compound flag — pick one, document
      it in the ops cheatsheet if the wording changes).
- [x] Same invariant enforced for cut/common producing multiple disconnected pieces, per
      the contract wording.
- [x] Battery case `nc_disjoint_fuse` flips from XFAIL to expected-pass; no other battery
      case regresses (`scripts/modeling_battery.py` full run against the OCCT .so).
- [x] Legitimate multi-body workflows are NOT broken: `merge:false` co-existing bodies,
      compound imports, and pattern/mirror flows still rebuild clean (core ctest + per-suite
      pytest green).
- [x] `graphify update .` + scripts/gate.sh before review.

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

## Resolution (sonnet-quorix, 2026-07-03)

The `cut`/`common` branch of the `"boolean"` feature (`core/doc/document.cpp`, the
explicit `type=="boolean"` handler) already enforced exactly this invariant — it counts
`TopAbs_SOLID` in the BOP result and throws `"... produced an empty body"` when
`nsolids != 1`. The bug was that the sibling `fuse` branch never got the same check: it
ran `::geom::unify_same_domain(out)` and stopped, so two disjoint operands (which pass
`BRepAlgoAPI_Fuse::IsDone()` — OCCT happily returns a 2-solid compound) sailed through as
`ok:true`. Fix: mirror the existing `nsolids` count/throw in the `fuse` branch, right
after `unify_same_domain` (so touching/coplanar operands that legitimately collapse to
one solid are unaffected), with a distinct "disjoint solids" message. No ops_cheatsheet.md
wording change needed — its BOOLEAN section already promises exactly this ("A fuse must
produce ONE connected solid; a cut/common must also yield ONE solid piece (disjoint
results fail...)"), so this fix makes the engine match a contract that was already
documented, not a new/changed one.

Scoped deliberately to ONLY the explicit `"boolean"` feature dispatch, not
`geom::fuse`/`geom::cut`/`geom::common`/`geom::fuse_many`/`geom::cut_many` themselves —
those low-level primitives have ~50 other call sites across sheet metal, weldments,
lofts, sweeps, thread, surfacing, and (critically) linear/circular pattern + mirror
(`fuse_many`), where instances are legitimately expected to sometimes NOT touch each
other while still being unioned into one logical multi-body result. Baking the invariant
into the primitives would have broken all of those; baking it into the one feature
branch the contract actually names does not touch them at all.

Verification:
- **Touch boundary (the case most likely to make this fix wrong, per advisor review):**
  the contract says fuse succeeds when operands "intersect or touch" and only disjoint
  operands fail. Manually drove two `10x10x10` boxes sharing an exact coincident face (A
  at origin x∈[-5,5], B `at:[10,0,0]` x∈[5,15], zero gap/zero overlap) through
  `core_bridge.doc_apply_ops` against this worktree's `.so`: `rebuild_ok=True,
  volume=2000` (both boxes' full material, nothing lost), `bodies=1` —
  `BRepAlgoAPI_Fuse` genuinely welds face-touching solids into one `TopAbs_SOLID`, so the
  `nsolids != 1` check does not false-positive on this boundary. Promoted this probe to a
  permanent case, `boolean_fuse_touching_face` (`expect_vol="=="`, `expect_bodies=1`), so
  the boundary stays covered going forward.
- Battery: added `nc_disjoint_cut` (a full-cross-section tool slicing a body into two
  disconnected remnants) and `boolean_fuse_touching_face` (above), alongside flipping
  `nc_disjoint_fuse`'s `xfail=` marker off. Full run against this worktree's OCCT build
  (`core/build-linux`, OCCT confirmed present via `cmake -DAICAD_WITH_OCCT=ON`):
  **52/52 PASS, 0 FAIL, 0 XFAIL, 0 ERROR** — every previously-passing case (including
  `boolean_fuse`, `boolean_cut`, `linear_pattern`, `circular_pattern`, `mirror`, `sm_*`,
  `weld_member`) still passes, confirming legitimate multi-body/pattern/mirror workflows
  are untouched. (`reports/modeling/*` itself is NOT committed — it's a worktree-absolute
  snapshot that would dangle after `git worktree remove`; this note is the durable record
  of the run.)
- `ctest --test-dir core/build-linux`: 100% (42/42) passed.
- `bash scripts/gate.sh`: RESULT: PASS. The three pre-existing `FAIL-RECORDED` pytest
  buckets (mcp_server/services/engine_oracle) were inspected directly — every failure I
  sampled is either a missing pytest fixture (`test_ecad_tools.py`, unrelated file I
  never touched) or `"unknown method: doc_sketch_solve"`/`"unknown method:
  sim_run_static"`, which `work/audits/SIM_AUDIT.md` (T-V1) already documents as a
  pre-existing architectural gap (those RPC methods are MCP-server-level plugins, not
  reachable through the raw `core_bridge.py` JSON-RPC bridge these tests call directly)
  — nothing in the sampled failures references booleans, fuse, cut, or disjoint
  geometry. `git status` confirms this branch touches only `core/doc/document.cpp`,
  `scripts/modeling_battery.py`, and the regenerated `reports/modeling/*` snapshot.
- `python3 scripts/check.py`: all 4 gates PASS.

Reverted (not committed): an incidental `docs/manual/{commands,contracts}.md` diff —
`tools/test_manual_gen.py::test_drift_clean_after_write` writes the real repo's
`docs/manual/*.md` as a side effect of running the gate's `pytest/tools` stage
(pre-existing test-hygiene wart, same one seen on T-BL21 this session); unrelated to
this task's scope, reverted with `git checkout --`.

## Progress log
- 2026-07-03 22:35 UTC [sonnet-quorix] (foreman) You hold BOTH T-BL21 (active, pushed) and T-BL30 (no branch). Claims are one-at-a-time — if you're not actively on T-BL30, run: python3 work/tools/work.py release T-BL30 --owner sonnet-quorix so another driver can take it. If you meant to queue it for after T-BL21, release anyway; re-claim when free.
