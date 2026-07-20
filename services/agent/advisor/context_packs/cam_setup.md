# CAM job setup context pack
# Tools: cam_new_job, cam_set_stock, cam_add_setup, cam_add_tool, cam_add_operation,
#        cam_generate, cam_simulate, cam_post, cam_setup_sheet, cam_materials, cam_op_types

## Forced order
1. cam_new_job → cam_set_stock (material + envelope) → cam_add_setup (WCS / fixture).
2. cam_add_tool from library / cam_tool_types — never invent diameters or flute counts.
3. cam_add_operation (facing → rough → finish → drill/chamfer) with stepover/stepdown from tool data.
4. cam_generate → cam_simulate (MUST be clean) → cam_post only after simulate is clean.
5. cam_setup_sheet for shop handoff.

## Discipline
- Feeds/speeds from cam_materials / tool tables — not memory.
- Climb vs conventional per material; leave finish stock on rough ops.
- Never post g-code without a clean simulate.
