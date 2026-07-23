---
id: T-M3
title: CAM overhaul: holder/shank collision simulation
workstream: M
owner: opus-iron
status: done
branch: task/T-M3-cam-overhaul
day: 3
priority: 3
depends_on: [T-M6]
lane: cam
kind: new
---

## Context

CAM overhaul wave 2 (after T-M6 lands — both touch stock_sim.cpp). `check_holder=true`
exists in SimOptions but is permanently unimplemented: deep-pocket jobs pass simulation
while the holder would crash. Audit: work/audits/CAM_AUDIT.md §3.2, §T-M3.

## Files / areas touched

- core/cam/stock_sim.{h,cpp}, core/cam/model.h (Tool already has holder fields),
  core/tests/test_cam.cpp, services/mcp/cam_tools.py (expose check_holder param)

## Definition of done

- [x] Shank/holder swept-cylinder check against remaining stock dexels (reuse the tip's
      segment-sweep, Z-extent flute_length..overall_length)
- [x] holder_collisions accumulated in SimResult + warning emitted
- [x] check_holder exposed through cam_simulate MCP tool
- [x] Deep-pocket test where the shank obviously hits the walls (asserts collisions > 0)
- [x] core build + ctest green; mcp-venv cam tool tests green; `graphify update .`

## DONE (opus-iron, 2026-07-02)

Commits `09d8614` (C++ core) + `1f08b94` (MCP surface) on `task/T-M3-cam-overhaul`, stacked
on T-M6 (merge order **M6 → M3** — this branch contains T-M6's commits; merge T-M6 first).

**Collision model (reuses the tip's `dist_point_seg` sweep — audit §T-M3).** `holder_hit_seg`
sweeps the non-cutting body per cutting (sub-)segment: within the shank radius, remaining
stock above `tip_z + flute_length` hits the shank; in the annulus out to the holder radius,
stock above `tip_z + shoulder_length` hits the holder. `tip_z` tracks the tip along the
segment so a plunge raises the whole envelope with it. Gated on `opt.check_holder`, so the
default sim path is byte-unchanged. `SimResult.holder_collisions` (per-move count) + a warning
+ `to_json` field. `session.cpp cam_simulate` forwards `check_holder` into `SimOptions`.

**MCP (subagent, sonnet).** `cam_simulate` MCP tool gains `check_holder: bool=False`, threaded
into the RPC `options` exactly like `check_rapid`; `holder_collisions` flows through the raw
passthrough result. Test added in `test_cam_tools.py`.

**Tests.** C++ (append-only): a 30 mm-deep 16×16 pocket (deeper than the 25 mm shoulder) →
`holder_collisions>0` + warning with the flag on, `0` with it off; a 4 mm shallow pocket stays
clear (no false positive). MCP: `check_holder=True accepted; holder_collisions surfaced as int`.

**Verified (real, not skipped).** `test_cam` 56/56 (3505 asserts); full core ctest 18/18 green;
built `libchudware.so` (OCCT) from this branch and ran the mcp-venv cam-tool test against it →
`ALL CAM SMOKE CHECKS PASSED` (the check_holder assertion executed live, not SKIP).
`graphify update .` run.

**Handoff:** the `sweep_seg`/`holder_hit_seg` split lives in `simulate_setup`; turning still
early-returns before it. `cam_tools.py` is also touched by T-M4/M5 — my addition is append-only.

## Driver notes (Fable)

- BRANCH FROM T-M6's branch if unmerged (record merge order M6 → M3); from master if merged.
- Audit §T-M3 names the exact reuse (dist_point_seg). cam_tools.py is also touched by
  T-M4/M5 — additions append-only, small commits, rebase early.

## Progress log
- 2026-07-02 22:34 UTC [opus-iron] CLAIM REASON (priority note): taking T-M3 (P3) ahead of the P2 ready tasks deliberately — T-M3 branches off T-M6 (merge order M6→M3) which I authored this session and is still in review/unmerged. I hold the stock_sim.cpp context warm and left a sweep_seg-reuse handoff in T-M6.md specifically for this task; a cold-start by another agent basing on master would miss my turning branch + sweep_seg lambda and conflict. Basing the T-M3 worktree on the task/T-M6-cam-overhaul branch, not fresh master. Flagged to Fable foreman.
