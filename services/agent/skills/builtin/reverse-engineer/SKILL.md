---
name: reverse-engineer
description: Rebuild scanned/imported geometry into editable parametric features with a compare loop and verify_battery.
inject: auto
triggers:
  keywords: [reverse engineer, reverse-engineer, scan, mesh, point cloud, photogrammetry, STEP import]
  tools: [reverse_engineer, compare_parts, measure_from_photo, convert_face_to_sketch, import_part]
  phases: [reveng, feature]
---

# Reverse Engineer

## Sequence

1. **Capture/import** — STEP/mesh via `import_part`, or photo path (`start_capture_session` → `submit_part_photos`).
2. **Rebuild** — `reverse_engineer` into parametric features (not freehand over mesh).
3. **Compare loop** — `compare_parts` until deviation is acceptable; `measure_from_photo` / datasheet for critical dims.
4. **Editable faces** — `convert_face_to_sketch` / `project_into_sketch` when needed.
5. **Done gate** — `verify_battery`; ledger source artifact + deviation summary.

## Rules

- Never declare done on mesh-only bodies when parametric rebuild is in scope.
- Poor fit → more views/photos or tighter region seeds, then compare again.
