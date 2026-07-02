---
id: T-M3
title: CAM overhaul: holder/shank collision simulation
workstream: M
owner: opus-iron
status: doing
branch: task/T-M3-cam-overhaul
day: 3
priority: 3
depends_on: [T-M6]
---

## Context

CAM overhaul wave 2 (after T-M6 lands — both touch stock_sim.cpp). `check_holder=true`
exists in SimOptions but is permanently unimplemented: deep-pocket jobs pass simulation
while the holder would crash. Audit: work/audits/CAM_AUDIT.md §3.2, §T-M3.

## Files / areas touched

- core/cam/stock_sim.{h,cpp}, core/cam/model.h (Tool already has holder fields),
  core/tests/test_cam.cpp, services/mcp/cam_tools.py (expose check_holder param)

## Definition of done

- [ ] Shank/holder swept-cylinder check against remaining stock dexels (reuse the tip's
      segment-sweep, Z-extent flute_length..overall_length)
- [ ] holder_collisions accumulated in SimResult + warning emitted
- [ ] check_holder exposed through cam_simulate MCP tool
- [ ] Deep-pocket test where the shank obviously hits the walls (asserts collisions > 0)
- [ ] core build + ctest green; mcp-venv cam tool tests green; `graphify update .`

## Driver notes (Fable)

- BRANCH FROM T-M6's branch if unmerged (record merge order M6 → M3); from master if merged.
- Audit §T-M3 names the exact reuse (dist_point_seg). cam_tools.py is also touched by
  T-M4/M5 — additions append-only, small commits, rebase early.

## Progress log
- 2026-07-02 22:34 UTC [opus-iron] CLAIM REASON (priority note): taking T-M3 (P3) ahead of the P2 ready tasks deliberately — T-M3 branches off T-M6 (merge order M6→M3) which I authored this session and is still in review/unmerged. I hold the stock_sim.cpp context warm and left a sweep_seg-reuse handoff in T-M6.md specifically for this task; a cold-start by another agent basing on master would miss my turning branch + sweep_seg lambda and conflict. Basing the T-M3 worktree on the task/T-M6-cam-overhaul branch, not fresh master. Flagged to Fable foreman.
