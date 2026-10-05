# Guide: Cam

```text
CAM / MACHINING GUIDE — generate real CNC toolpaths + G-code from the part.

The CAM suite turns a part into a machining JOB: a tree of SETUPS (each a stock + work
coordinate system + machine/platform) holding OPERATIONS (toolpaths), driven by a tool
library and a material-aware feeds-&-speeds engine, posted to controller G-code.

TYPICAL LOOP
  1. cam_new_job("Bracket", post="grbl")            create the job (pick a post dialect).
  2. cam_add_tool(number=1, type="endmill_flat", diameter=6, flutes=3)   build a tool library.
  3. cam_extract(kind="all")  OR  cam_extract(kind="stock"|"holes"|"section"|"mesh")
     pull stock bounds, a mesh, planar regions, and drill points FROM THE CURRENT PART.
  4. cam_add_setup(name="Op1", machine="mill3", stock=<from extract>, material="aluminum").
  5. cam_add_operation(setup_id, type=..., tool_number=..., geometry=<from extract>, ...).
        2.5D mill: face, contour2d, pocket2d (islands), slot, chamfer2d, bore2d, engrave, adaptive2d
        holes:     drill (cycle=drill|peck|chipbreak|tap|ream|bore|spot), thread_mill
        3D mill:   parallel3d, contour3d (waterline), scallop3d, pencil3d, pocket3d, adaptive_clear3d, radial3d, spiral_finish3d
        turning:   turn_face, turn_rough, turn_finish, turn_groove, turn_thread, turn_part, turn_drill
        cutting:   cut2d (laser/plasma/waterjet — kerf, pierce, lead in/out); probe
     Feeds/speeds resolve automatically from the material + tool; override any of rpm/feed/
     plunge/stepover/stepdown on the op to pin it.
  6. cam_generate()    build the toolpaths (per setup / op).
  7. cam_simulate()    material-removal sim: removed/remaining volume, gouge + rapid-collision
                       checks, estimated cycle time.
  8. cam_post("fanuc") emit the NC program; cam_setup_sheet() for the operator sheet.
  9. Audit: cam_audit — modal parse, envelope, feeds, tool and round-trip checks; ship only ok:true.

FEEDS & SPEEDS ADVISOR (table-driven, aicad.calc/1 contract)
  Before cam_add_operation, call feeds_speeds() to get a recommended envelope:
    feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
    → {schema:"aicad.calc/1", answer:{rpm:{min,mid,max}, feed:{min,mid,max},
       stepdown:{min,mid,max}, stepover:{min,mid,max}, chipload, coolant, provenance}}
  After adding the op, call cam_check_feeds_envelope() to flag risky values:
    cam_check_feeds_envelope("alu_6061", "endmill_flat", 10.0, "roughing", rpm=50000)
    → {schema:"aicad.advisory/1", items:[{severity:"hint", candidate_fixes:[clamped_values]}]}
  For tap operations, get the drill size first:
    tap_drill("M6x1.0")  → {answer:{tap_drill_mm:5.0, pct_thread:75}}
    tap_drill("1/4-20")  → {answer:{tap_drill_mm:5.1, ...}}
  Supported tool_types: endmill_flat, endmill_ball, drill, tap, face_mill.
  Supported operations: roughing, finishing, slotting, drilling.
  Materials: alu_6061, steel_1018, steel_304_stainless, ti_6al4v, abs, pla,
             nylon_pa12, cast_iron_gray, inconel_718 (+ aliases from cam_materials()).

PLATFORMS: 3-axis mill, router, 4/5-axis (3+2), lathe / mill-turn, laser, plasma, waterjet.
POSTS: grbl, linuxcnc, fanuc, haas, mach3, marlin (laser), siemens (840D), centroid/tormach —
  fully data-driven (cam_post_dialects() lists them); per-controller canned cycles, arc IJK/R,
  work offsets, tool changes, coolant, laser power gating.
REFERENCE: cam_post_dialects(), cam_materials() (cutting-data DB), cam_tool_types(), cam_op_types(),
           feeds_speeds(), cam_check_feeds_envelope(), tap_drill().
Address geometry the same plain-English way as modeling — extract regions/holes/sections from the
part, don't hand-type coordinates unless you must.
```

