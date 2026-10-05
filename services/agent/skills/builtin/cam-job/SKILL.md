---
name: cam-job
description: Build a CAM job end-to-end — stock, setup, tools, ops, generate, simulate, then post only when clean.
inject: auto
triggers:
  keywords: [CAM job, CNC machining, machining toolpath, toolpath, g-code, gcode, feeds and speeds, milling operation, postprocessor]
  tools: [cam_new_job, cam_generate, cam_simulate, cam_post, cam_set_stock]
  phases: []
---

# CAM Job

## Sequence

1. **Orient** — `cam_op_types` / `cam_materials` if vocabulary is thin.
2. **Job** — `cam_new_job`; keep the returned job `doc_id`.
3. **Setup + stock** — `cam_add_setup(material=..., stock=..., wcs=...)` for the WCS,
   fixture and measured stock envelope. Keep `setup_id`; later stock changes use
   `cam_set_stock(setup_id, stock)`.
4. **Tools** — `cam_add_tool` from library; never invent diameters.
5. **Ops** — facing → rough → finish → drill/chamfer via `cam_add_operation`.
6. **Generate** — `cam_generate`.
7. **Simulate** — `cam_simulate` must be clean (no gouges/rapid collisions).
   Holder/shank checking needs geometry and `check_holder=True`; an unchecked
   holder is not collision-free.
8. **Post** — `cam_post` only after clean simulate; `cam_setup_sheet` for handoff.

## Rules

- Never post without a clean simulate.
- Feeds/speeds from tables (`cam_materials` / tool data), not memory.
- Ledger stock, tools, and post artifact paths.
