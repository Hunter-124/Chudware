# core/cam — CAM (toolpath, post-processor, stock simulator)

Scope: core/cam/**, core/api/libaicad.cpp (C ABI for cam_* RPCs), core/tests/test_cam.cpp
Contracts: `contracts/cam.md` — aicad.cam_sim_frames/1, aicad.cam_audit/1, aicad.machines/1, aicad.cam_job_diff/1 (cam_new/cam_apply_ops/cam_generate/cam_post/etc live directly in session.h as the de facto RPC contract)

## Entry points
- core/cam/session.h:33-61 `cam_new` / `cam_apply_ops` / `cam_generate` / `cam_simulate` / `cam_post` — the stateful CAM job RPC surface (mirrored to ecad parity)
- core/cam/operations.h:36 `generate()` — dispatcher: turns one Operation (+ resolved geometry, tool, setup) into a Toolpath by calling gen_drill / gen_pocket2d / gen_contour3d / gen_adaptive2d / etc.
- core/cam/model.h — the data model: Job / Setup / Operation / Tool / Stock / WCS / Toolpath (all JSON-serializable)
- core/cam/post.h/.cpp:311 `post_setup()` — renders a Setup's toolpaths to G-code (dialects: GRBL/Fanuc/Haas/LinuxCNC/Mach3/Marlin/Siemens/Centroid)
- core/cam/stock_sim.h/.cpp:123 `simulate_setup()` — triangle-mesh stock model + envelope collision checks
- core/api/libaicad.cpp:493 `cam_program_from_part()` — AI copilot entry: parses a request string, auto-detects material/dialect, orchestrates cam_new + cam_apply_ops + cam_generate + cam_simulate + cam_post to produce a job tree + gcode

## How it connects
CAM is OCCT-free (pure numerics + JSON); geometry arrives inline as Regions/Path2/drill points/TriMesh from either tests/MCP (inline) or a separate geom-layer extractor (product). The session (cam_new/cam_apply_ops/cam_generate) is the state holder; core/cam/session.cpp:103 `generate_setup()` and :291 `cam_generate()` call core/cam/operations.cpp:12 `generate()` to build toolpaths. Feeds/speeds resolve from a cutting database (core/cam/feeds.h/.cpp). This subsystem is complete/parity with Python (engine/cam/ frozen); cam_program_from_part is the AI fast-path used by the MCP `cam_add_operation` / `cam_generate` / `cam_post` chain to automate CAM program generation from a live part.

## Debugging playbook
- Build: `cmake -S core -B core/build-standalone && cmake --build core/build-standalone` (CAM builds standalone; no OCCT required)
- Tests: `ctest --test-dir core/build-standalone -R cam --output-on-failure` (test_cam.cpp ~L39KB; exercises all generators, post dialects, stock sim)
- Known gotcha: feeds resolution falls back to aluminum defaults if material_id is unrecognized (core/cam/feeds.cpp) — match engine/cam cuts or tests fail
- Known gotcha: post_setup() escapes GCode comments; ensure dialect comment_open/close match public NC references (no vendor posts copied; clean-room authoring)
- Stock simulation uses exact triangle-mesh collision; hollow regions (islands) are supported; check SimResult.remaining_stock for leftover material

## Graphify starters
- graphify query "how does cam_generate build toolpaths end-to-end"
- graphify explain "Operation"
- graphify path "cam_program_from_part" "post_setup"

## Related tasks/docs
- work/tasks: T-B2, T-B3, T-F2, T-F12, T-F15 mention cam (CAM is part of the strangler parity story; see AGENTS.md)
- docs/cam_samples/ — reference CAM models for testing
- README.md §1–2 (CAM listed as "full suite: milling/turning/laser-plasma-waterjet, G-code for GRBL/LinuxCNC/Fanuc/Haas/Marlin/Siemens")
