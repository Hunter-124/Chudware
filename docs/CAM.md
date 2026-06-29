# AI-CAD — CAM (Computer-Aided Machining)

A full CAM suite: stock + tool library + material-aware feeds & speeds → toolpaths for
2.5D/3D milling, turning, and 2D cutting → material-removal simulation → controller G-code.
Built in the **pure-C++ core** (`core/cam/`, namespace `aicad::cam`), exposed over the
`libaicad` C ABI, the MCP server (`cam_*` tools), and the desktop **Manufacture** workspace.

> Status (2026-06): engine + RPC + MCP + GUI complete and tested. `test_cam` = 48 cases /
> 3384 assertions green. G-code verified across all 8 post dialects (samples in
> `docs/cam_samples/`). The GUI host builds green; visual pass is done on Windows.

## Architecture

```
core/cam/                     OCCT-FREE pure-numeric + JSON engine (builds in the fast config)
  model.{h,cpp}               data model: Stock, WCS, Machine, Tool/ToolLibrary, Operation,
                              Move/Toolpath, Setup/Job — full JSON round-trip + enum tables
  geometry.{h,cpp}            2D machining geometry over Clipper2 (Boost): offset, boolean,
                              pocket-clearing offsets, raster fill, leads, measures
  feeds.{h,cpp}               feeds & speeds calculator + builtin material cutting-data DB (25 mats)
  toolpath.{h,cpp}            PathBuilder (classified moves, depth stepping, leads, helix/zigzag
                              ramp, holding tabs); depth_levels
  op_common.h                 shared op helpers (geometry readers, feed resolution, region points)
  operations.{h,cpp}          generate() dispatch over all 28 operation types
  op_milling.cpp              face, contour/profile, pocket (islands), slot, chamfer, helical bore
  op_drill.cpp                drilling canned cycles (drill/peck/chipbreak/tap/ream/bore/spot),
                              helical thread milling
  op_adaptive.cpp             adaptive / high-speed clearing (constant low radial engagement,
                              trochoidal helical entry, deep axial cut)
  op_engrave.cpp              engrave / trace (V-bit depth-from-width)
  heightfield.{h}/.cpp        top-down drop-cutter heightfield + waterline (Z-level) mesh sections
  op_3d.cpp                   parallel, waterline/contour finish, scallop, pencil, pocket/adaptive
                              roughing, radial, spiral finish — gouge-free flat/ball/bull drop cut
  op_turn.cpp                 turning: face, OD/ID rough, profile finish, groove, G76 thread,
                              part-off, center-drill (XZ plane, radius+axial)
  op_cut.cpp                  laser/plasma/waterjet (kerf comp, pierce, lead in/out, passes); probe
  post.{h,cpp}                data-driven post engine + 8 dialect presets + setup sheet
  stock_sim.{h,cpp}           Z-dexel material-removal sim, cycle-time estimate, backplot
  session.{h,cpp}             stateful job session (ops/undo/generate/simulate/post, .cam+ I/O)
  extract.{h}/.cpp            OCCT extraction adapter (in aicad_doc): live B-rep -> CAM geometry

mcp_server/cam_tools.py       cam_* MCP tools (register_cam_tools); cad_guide("cam")
host/src/ui/cam/              CAM Manufacture workspace (cam_mirror + cam_panel) + viewport overlay
```

The CAM **core is OCCT-free** (pure numerics + nlohmann::json), like `aicad_sim`/`aicad_ecad`,
so it builds in the standalone config and is unit-tested headless. Geometry to machine is supplied
as plain data (regions / section profiles / drill points / a triangle mesh); the OCCT
`cam_extract` adapter pulls that data out of a live document for the product. 2D polygon offset/
boolean uses **Clipper2** (Boost license — vendored via FetchContent; see
`contracts/licenses.md`). No GPL CAM source (FreeCAD Path / libarea / opencamlib) or vendor
post is copied — dialect tables + algorithms are clean-room from public G-code references.

## Platforms & operations

| platform | operations |
|---|---|
| 3-axis mill / router | face, contour/profile, pocket (+islands), slot, chamfer, engrave, bore, adaptive clearing |
| hole-making | drill, peck/deep, chip-break, tap, rigid-tap, ream, bore, spot, thread-mill |
| 3D milling | parallel/raster, waterline (Z-level), scallop, pencil/corner, pocket+adaptive roughing, radial, spiral |
| 4/5-axis | 3+2 positional (WCS orientation per setup); rotary fields on the machine |
| turning / mill-turn | face, OD/ID rough, finish, groove, thread (G76), part-off, center-drill |
| laser / plasma / waterjet | profile cut with kerf comp, pierce, lead in/out, multi-pass |
| metrology | probing (G38 approach/retract, XY edge probe) |

## Feeds & speeds

`compute_feeds(machine, tool, op, material)` resolves rpm / feed / plunge / lead / ramp /
stepover / stepdown, branching by tool class (mill / drill / tap / turning / laser-plasma-waterjet),
HSS-vs-carbide, ball/chamfer chip-thinning, CSS for turning. Any field the user pins on the op is
preserved; only unset (0) fields are computed; everything clamps to the machine envelope. The
builtin `CuttingDB` ships 25 material groups with clean-room handbook cutting data; users merge or
override per material and per tool.

## Post-processors

Data-driven `PostConfig` dialect model + an NC generator (modal motion/feed suppression, sequence/
program numbers, `%` wrap, work offsets G54.., tool change + G43, spindle/coolant, IJK or R arcs,
drilling canned-cycle emission vs long-hand, laser/plasma beam gating, lathe diameter mode + G96
CSS, configurable comments/dwell). Builtin dialects: **GRBL, LinuxCNC, Fanuc, Haas, Mach3/4,
Marlin (laser), Siemens 840D, Centroid/Tormach**. Plus an HTML+text operator setup sheet.

## Using it

### MCP / agent
```
cad_guide("cam")                          # the workflow
cam_new_job(name, post="grbl")
cam_add_tool(number=1, type="endmill_flat", diameter=6, flutes=3)
cam_extract(kind="all")                    # stock + mesh + holes from the current part
cam_add_setup(name="Op1", machine="mill3", material="aluminum", stock=<extract.stock.box>)
cam_add_operation(type="pocket2d", tool_number=1, geometry={regions:[...]})
cam_generate(); cam_simulate(); cam_post("fanuc"); cam_setup_sheet()
```

### Desktop GUI
Open the **Manufacture** ribbon tab (or any `cam_*` command) → the **CAM** panel: a toolbar
(New Job / Extract-from-part / Add Setup+Tool / operation buttons / Generate / Simulate / Post),
a setups → operations → tools tree, a 2D toolpath backplot, a material-removal sim summary
(removed volume / cycle time / gouge + rapid-collision flags), and a G-code pane (dialect picker +
viewer, writes `.nc`). Toolpaths also render over the part in the 3D viewport (classified colors:
rapid red, lead green, plunge cyan, cut blue, arc violet).

## Verification

- `ctest --test-dir core/build-occt -R cam` (or `core/build-linux/bin/test_cam`): 48 cases /
  3384 assertions — model JSON, Clipper2 geometry, feeds, every operation generator, all 8 post
  dialects, the dexel sim, the session lifecycle (+ a dialect-override regression).
- `mcp_server/test_cam_tools.py`: drives the `cam_*` RPCs through the C++ core (extract on a real
  STEP, generate, simulate, post) — `ALL CAM SMOKE CHECKS PASSED`.
- `docs/cam_samples/`: conformant G-code for all 8 controllers on a pocket+contour+peck-drill job
  + the setup sheet. GRBL/Marlin emit long-hand drilling and no ATC; Fanuc/Haas emit O-numbers,
  N-lines, `%`, M6 tool changes, and G83 peck cycles.
- The C ABI roundtrip + `cam_extract` were exercised on a real exported STEP (50×50×10 box with a
  Ø6 through-hole): correct stock bbox, mesh, the detected hole, and a section region with the hole
  as an island.
