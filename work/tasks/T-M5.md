---
id: T-M5
title: CAM overhaul: OCCT hole-extraction completeness (counterbore/countersink/tilt)
workstream: M
owner: unassigned
status: todo
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

- [ ] Coaxial-cylinder merge → counterbore records {dia_major,dia_minor,depth_major,depth_minor}
- [ ] GeomAbs_Cone branch → countersink records with tip angle + outer dia
- [ ] Tilt threshold relaxed to 0.95 with tilt angle emitted in the record
- [ ] Tests for kind="sections" and kind="profile" extraction modes
- [ ] core build + ctest + mcp-venv cam tool tests green; `graphify update .`

## Driver notes (Fable)

- Audit §T-M5 has exact detection rules. BRepAdaptor_Surface already imported — no new deps.
- Build a small test part with a counterbored + countersunk + tilted hole via the engine
  (add_features) as the fixture; assert the extract records, not just counts.
