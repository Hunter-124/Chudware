CAM — job → stock → setup → tool → op → generate → simulate → post.

OPS (typical 2.5D order):
  facing            flatten top stock
  adaptive/clearing bulk rough, high stepover safe
  pocket            closed cavity rough+finish
  contour           wall/profile finish
  drill             holes (peck when deep)
  chamfer           edge break with chamfer mill

PARAMS:
  stepover   rough 30–50% tool Ø; finish 5–15%
  stepdown   by tool/material (cam_materials / tool table)
  climb vs conventional — climb preferred on CNC when rigidity allows
  leave finish stock on rough ops

WCS / SETUP:
  cam_set_stock first (material + envelope)
  cam_add_setup pins WCS origin/fixture
  cam_setup_sheet for shop handoff

RULES:
  feeds/speeds from tables, not memory
  cam_generate → cam_simulate MUST be clean before cam_post
  never invent tool diameters — cam_add_tool / cam_tool_types
