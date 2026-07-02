---
id: T-M5
title: CAM overhaul: OCCT hole-extraction completeness (counterbore/countersink/tilt)
workstream: M
owner: opus-flint
status: done
branch: task/T-M5-cam-overhaul
day: 5
depends_on: []
---

## Context

CAM overhaul wave 1. `cam_extract` hole detection misses counterbores, countersinks, and
tilted holes — a real shop's bread and butter. Audit: work/audits/CAM_AUDIT.md §3.2, §T-M5.

## Files / areas touched

- core/cam/extract.cpp, services/mcp/test_cam_tools.py

## Definition of done

- [x] Coaxial-cylinder merge → counterbore records {dia_major,dia_minor,depth_major,depth_minor}.
      `holes_json` groups coaxial cylinders by a stable axis-XY key; a group with >1 distinct
      radius emits `kind:"counterbore"` + those four fields. VERIFIED on real engine geometry:
      a Ø6 bore + Ø12×5-deep cbore → {dia_major:12, dia_minor:6, depth_major:~5, depth_minor:~15}.
- [x] GeomAbs_Cone branch → countersink records with tip angle + outer dia. Cone faces (~Z axis)
      → `csink_angle_deg` (= 2·SemiAngle, the included/tip angle) + `csink_outer_dia` (widest ring),
      attached to a coaxial hole (or standalone). VERIFIED: a 90° csink → {csink_angle_deg:~89.5,
      csink_outer_dia:~11.9, kind:"countersink"}.
- [x] Tilt threshold relaxed to 0.95 with tilt angle emitted in the record. `AXIS_TOL` 0.99→0.95
      (accepts holes tilted up to ~18°); every record carries `tilt_deg = acos(|axis.Z|)`. VERIFIED
      with a ~10° tilted hole (cos10°≈0.985): dropped by the OLD 0.99 threshold, now detected with
      tilt_deg=10.0. BONUS correctness: `depth`/`depth_major`/`depth_minor` now report AXIAL length
      (z_extent/cos(tilt)) rather than the Z-projection — meaningful for tilted bores, a no-op for
      vertical holes (cos0=1, so the counterbore depths above are unchanged).
- [x] Tests for kind="sections" and kind="profile" extraction modes. `sections` (z_levels) → 3
      levels each with non-empty regions; `profile` → non-empty [z,x] silhouette. Both asserted.
- [x] core build + ctest + cam tool tests green; `graphify update .` run (23737 nodes).
      core build PASS; ctest 100% (42/42); cam smoke test 41 checks / 0 failures (15 pre-existing +
      26 new, run via `AI_CAD_LIBAICAD=…/libaicad.so python -m mcp_server.test_cam_tools`).

## Closeout / verification (opus-flint, 2026-07-03)

Scope: `core/cam/extract.cpp` `holes_json` rewrite (audit D7) + `services/mcp/test_cam_tools.py`
(new `_check_hole_extraction`). No new deps (gp_Cone.hxx; BRepAdaptor_Surface already imported).

TEST INVOCATION (the cam suite is a plain main() runner needing the engine, NOT collected by the
gate's `pytest engine/doc agent` stage):
  `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m mcp_server.test_cam_tools`
  → ALL CAM SMOKE CHECKS PASSED (41 checks, 0 failures; deterministic across reruns).

Fixtures built via the engine (add_features → STEP → cam_extract), asserting RECORDS not counts:
an 80×80×20 box with cbore/csink/simple holes; a ~10° tilted hole via a two-distance chamfer that
consumes the box top into a sloped face, drilled normal to it (documented inline).

NOTE on the tilted fixture's `depth` (~0.9mm): investigated — the extractor faithfully reports the
one cylinder face present; the wedge geometry genuinely leaves a short bore wall (a fixture quirk,
NOT an extractor bug). The tilted-case test asserts only the exact/stable `dia`+`tilt_deg`, per the
"don't over-assert" rule.

GATE (`scripts/gate.sh`): core build PASS · ctest 100% PASS · (check.py + pytest-stage reds are the
documented pre-existing set, unrelated to this core-only change — see the T-H9 closeout / issue #6).

## Driver notes (Fable)

- Audit §T-M5 has exact detection rules. BRepAdaptor_Surface already imported — no new deps.
- Build a small test part with a counterbored + countersunk + tilted hole via the engine
  (add_features) as the fixture; assert the extract records, not just counts.
