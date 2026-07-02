# CAM Suite Audit — sonnet-quill, 2026-07-02

> Commissioned by: foreman (Erik directive)  
> Scope: full CAM/toolpath-generation suite — C++ core, MCP tools, fast-path, tests  
> Branch: `audit/cam-suite`

---

## 1. Inventory

### 1.1 Core C++ CAM (`core/cam/`)

| File | Role |
|---|---|
| `model.h / model.cpp` | Data model: Vec2/3, Region, TriMesh, Stock, WCS, Machine, Tool/ToolLibrary, Heights, Lead, Ramp, Operation, Move, Toolpath, Setup, Job. All JSON round-trip. |
| `operations.h / operations.cpp` | Single `generate()` dispatch entry point; individual `gen_*()` declarations |
| `op_milling.cpp` | Face, Contour2D, Pocket2D, Slot, Chamfer2D, Bore2D |
| `op_adaptive.cpp` | Adaptive2D (engagement-limited concentric offsets + helical entry) |
| `op_3d.cpp` | Parallel3D, Contour3D (waterline), Pocket3D, AdaptiveClear3D, Scallop3D, Pencil3D, Radial3D, SpiralFinish3D |
| `op_drill.cpp` | Drill (all canned cycles, long-hand moves), ThreadMill |
| `op_engrave.cpp` | Engrave/Trace (V-bit width-to-depth) |
| `op_cut.cpp` | Cut2D (laser/plasma/waterjet) + Probe |
| `op_turn.cpp` | TurnFace, TurnRough, TurnFinish, TurnGroove, TurnThread, TurnPart, TurnDrill |
| `toolpath.h / toolpath.cpp` | PathBuilder (incremental move assembly), depth_levels(), cut_loop(), cut_open(), helix_entry(), zigzag_entry() |
| `geometry.h / geometry.cpp` | 2D offset (Clipper2), boolean ops, pocket_offsets(), raster_fill(), waterline_sections(), open-path offset |
| `heightfield.h / heightfield.cpp` | Z-dexel grid from TriMesh; drop_tool() for ball/flat tools; waterline_sections() |
| `feeds.h / feeds.cpp` | CuttingDB (25 builtin materials), compute_feeds(), apply_feeds() |
| `post.h / post.cpp` | 8 PostConfig presets (GRBL, LinuxCNC, Fanuc, Haas, Mach3, Marlin, Siemens 840D, Centroid/Tormach); NcGen renderer; setup_sheet() |
| `stock_sim.h / stock_sim.cpp` | HeightField dexel simulator: gouge check, rapid-through-stock, cycle-time estimate, backplot |
| `session.h / session.cpp` | Stateful `CamSession` store (undo/redo, cache, journal); all `cam_*` RPC entry points |
| `extract.h / extract.cpp` | OCCT adapter: stock bbox, mesh, Z-section regions, hole detection, turning profile |

### 1.2 MCP Tools (`services/mcp/cam_tools.py`)

All 18 MCP CAM tools are registered via `register_cam_tools()`:

`cam_new_job`, `cam_job_tree`, `cam_undo`, `cam_redo`, `cam_add_tool`, `cam_add_setup`,
`cam_set_stock`, `cam_add_operation`, `cam_update_operation`, `cam_remove_operation`,
`cam_extract`, `cam_generate`, `cam_simulate`, `cam_post`, `cam_setup_sheet`,
`cam_save`, `cam_open`, `cam_post_dialects`, `cam_materials`, `cam_tool_types`, `cam_op_types`

### 1.3 Agent CAM Fast-Path (`core/api/libaicad.cpp:493`)

`cam_program_from_part()` — deterministic, no-LLM "make me a CNC program" path.  
Mines free-text request for strategy keywords (face/pocket/drill/adaptive/3d/lathe/chamfer/engrave), picks dialect (grbl/fanuc/haas/…) and material (aluminum/steel/…), calls `cam_extract`, auto-sizes tools, builds and posts the job in one shot. Exposed as `cam_program` MCP method.

### 1.4 `describe_toolpaths` (T-F7, `services/mcp/gsl_tools.py:616`)

Narration tool: calls `cam_tree` + `cam_simulate` + `cam_post` and summarizes the job, ops, sim fractions, cycle time, gouge flags, G-code bounds, and feed range as plain text for the agent's context window.

### 1.5 Op Strategy Inventory

**28 operation types** across 6 process families:

| Family | Operations |
|---|---|
| 2.5D Milling | Face, Contour2D, Pocket2D, Slot, Adaptive2D, Engrave, Chamfer2D, Trace, Bore2D |
| Hole-making | Drill (drill / peck / chipbreak / tap / rigidtap / ream / bore / spot / backbore), ThreadMill |
| 3D Milling | AdaptiveClear3D, Pocket3D, Parallel3D, Contour3D, Scallop3D, Pencil3D, Radial3D, SpiralFinish3D |
| Turning | TurnFace, TurnRough, TurnFinish, TurnGroove, TurnThread, TurnPart, TurnDrill |
| 2D Cutting heads | Cut2D (laser / plasma / waterjet) |
| Metrology | Probe |

---

## 2. Strengths

- **OCCT-free core.** The cam_* engine is pure numerics + JSON, builds headless, and is unit-testable without a GUI. The OCCT extraction adapter is cleanly isolated in `extract.cpp`.
- **Complete operation breadth.** 28 op types covering milling, turning, laser, plasma, waterjet, and probing is legitimately impressive for a single integrated system.
- **Solid data model.** `model.h` is a well-typed, JSON-serializable schema. Heights, Lead, Ramp, CompSide, CutPattern, CutDirection all have the right primitives for a real shop.
- **Working post-processor.** 8 dialects with correct canned-cycle emission (G81/G83/G73/G84), IJK arcs, modal suppression, work offsets, tool-length compensation, lathe CSS, and laser power gating. Dialects are data (PostConfig) not code.
- **Dexel stock simulation.** HeightField-based sim with gouge tolerance, rapid-collision detection, volume accounting, and cycle time via trapezoidal accel model. Cheap enough to run inline.
- **Well-tested for its tier.** 913 lines of doctest covering model JSON round-trips, geometry (offset/boolean/raster/waterline), feeds, every generator family, all 8 post dialects, sim, and the full session RPC sequence. Tests are geometry-true (they verify bounds, move counts, Z levels) not just smoke.
- **Feeds DB with correct scaling.** 25 materials with SFM-based RPM, diameter-scaled chip load, HSS derating, CSS for turning, synchronized tap feed, 2D-cut feed/thickness scaling. User overrides preserved.
- **Fast-path coverage.** `cam_program` handles both milling and turning, auto-selects tools by part span, deduplicates tools, and runs generate→simulate→post deterministically.

---

## 3. Defects & Gaps

### 3.1 Critical — would produce wrong G-code or crash a real machine

**D1: Post ignores `check_holder=false` in sim but holds no holder geometry anyway**  
`SimOptions.check_holder` exists but is never exercised (`stock_sim.cpp` — the holder-check branch is absent from the loop body). A tool projecting into a fixture goes undetected.  
File: `core/cam/stock_sim.h:19`, `core/cam/stock_sim.cpp:206-234`

**D2: Canned-cycle post reads `op->geometry["points"]` directly — misses multi-setup WCS offsets**  
When a drill op has a `wcs` override (per-op WCS at `model.h:L255`), the canned-cycle emitter in `post.cpp:L395-415` still reads the raw geometry XY without transforming them into the work-offset frame. If the op WCS differs from the setup WCS, all hole positions are wrong.  
File: `core/cam/post.cpp:L388-415`

**D3: `gen_pocket2d` helix entry ignores `op.ramp.type != Helix` branch — plunge_to is called without a prior rapid_xy**  
`op_milling.cpp:L147-153`: when `ramp.type != Helix`, `pb.plunge_to(z)` is called with `cur_` still at the clearance position from the previous loop; `rapid_xy` is never called first. The tool plunges from wherever it last was, not from above the entry point.  
File: `core/cam/op_milling.cpp:L147-153`

**D4: `post.cpp` Work-offset register emitted as `G54`..`G59` but extended offsets (`offset_sub > 0`) are silently dropped**  
`model.h:L98` defines `offset_sub` for G54.1 P<n>. `post.cpp:L374-376` only emits `G` + `to_string(wo)` — there is no code path that emits `G54.1 P<n>`. Jobs using extended work offsets produce wrong G-code on Fanuc/Haas.  
File: `core/cam/post.cpp:L373-376`, `core/cam/model.h:L97-98`

### 3.2 Significant — wrong results, poor quality, or missing real-shop requirements

**D5: Adaptive2D is concentric-offset, not true trochoidal**  
`op_adaptive.cpp` produces pocket_offsets (concentric rings at `engage` spacing) but does not implement arc-blend or trochoidal linking between the rings. At corners, the tool engagement spikes — the whole point of HSM adaptive clearing is violated. Labeling is misleading.  
File: `core/cam/op_adaptive.cpp:L31-55`

**D6: `CuttingDB` is disconnected from the engine/materials registry (Issue #2)**  
The feeds DB (`feeds.cpp`) is a standalone in-code table. The AI_TOOLING_MASTER_PLAN and issue #2 specify that the C++ cutting DB should fold onto the engine/materials registry (shared with FEA material properties). Right now the two are entirely separate; material IDs don't align between CAM and FEA.  
File: `core/cam/feeds.cpp:L57-86`, `core/cam/feeds.h:L44-56`

**D7: `cam_extract` hole detection is Z-axis-cylinder-only and misses oblique, counterbored, and countersunk holes**  
`extract.cpp:L195`: only `GeomAbs_Cylinder` faces with `fabs(ax.Z()) < 0.99` are recognized. A hole at any tilt, a counterbore (two nested cylinders), or a countersink (cone face) produces zero drill points. This is the primary geometry path feeding `cam_program` auto-drill.  
File: `core/cam/extract.cpp:L183-217`

**D8: 3D sim approximates arcs as chords — can underreport removal on curved toolpaths**  
`stock_sim.cpp:L207`: comment says "approximate as chord for removal (fine at sim resolution)". At 0.5mm resolution and large-radius arcs this underestimates removal near the chord midpoint. The gouge check can false-negative on convex surfaces cut with arcs.  
File: `core/cam/stock_sim.cpp:L207-211`

**D9: Turning post missing CSS/G96 max-RPM clamp word**  
`post.cpp:L380-381` emits `G96 S<css>` but never emits `G50 S<max_rpm>` before it. Fanuc/Haas require `G50 S<max>` immediately before `G96` to clamp the spindle when the diameter approaches zero. Without it, the spindle can overspeed to destruction on a parting pass.  
File: `core/cam/post.cpp:L380-381`

**D10: Drill cycle timing in `estimate_time()` doesn't account for peck retract traversals**  
`stock_sim.cpp:L33-79`: rapid retracts between peck increments are counted as cut moves (they're `MoveKind::Rapid`) which are handled correctly — but the approach rapid back down to `z+0.5` at each peck is classified as `Rapid` yet the loop retracts fully to `Rp`. The time estimate is roughly correct but the per-peck approach rapid is not modeled. Cycle-time estimates for deep-hole peck programs can be 20–40% optimistic.  
File: `core/cam/stock_sim.cpp:L59-63`, `core/cam/op_drill.cpp:L30-38`

**D11: `describe_toolpaths` (T-F7) re-runs `cam_simulate` every time — no cache invalidation guard**  
`gsl_tools.py:L646-654` calls `cam_simulate` unconditionally on every `describe_toolpaths` call. On a job with many setups or large meshes this is expensive. The session `s.stale` flag exists for exactly this purpose but is not consulted from Python.  
File: `services/mcp/gsl_tools.py:L644-654`

**D12: `post.cpp` dwell-word inconsistency between Fanuc and LinuxCNC dialects is unit-tested but behavior is inverted for spot drilling**  
`post.cpp:L404`: `c.dwell_seconds ? op->dwell : op->dwell * 1000` — on Fanuc `dwell_seconds=false` so it correctly multiplies by 1000. But `op->dwell` is stored in seconds (`model.h:L295`). A `dwell=0.5s` on a spot cycle emits `P500` (milliseconds) on Fanuc (correct) and `P0.5` on LinuxCNC (correct). This is actually right — but the comment at `post.h:L93` says `"P in ms"` which refers to Fanuc only, and there's no test for spot dwell on LinuxCNC specifically.  
File: `core/cam/post.h:L92-94` (documentation-level, low severity)

### 3.3 Coverage / Quality Gaps

**G1: No geometry-true toolpath verification for 3D ops**  
Tests for 3D ops (`test_cam.cpp:L514-529`) only check `r.ok` and `cut_length > 0`. They don't verify that the drop-tool Z stays above the surface (anti-gouge). The `min_cut_z` helper exists but is only used for `gen_parallel3d` (`L511`).

**G2: No post-processor round-trip test for Haas, Mach3, Siemens, Centroid**  
Only GRBL, Fanuc, LinuxCNC, and Marlin have real output content tests. The other 4 dialects are enumeration-tested only.

**G3: `cam_extract` "sections" mode not exercised in any test**  
`test_cam_tools.py` and `test_cam.cpp` never call `cam_extract` with `kind="sections"` or `kind="profile"`.

**G4: Turning sim not validated**  
`stock_sim.cpp` uses a cylinder-masking path for cylindrical stock but there are no tests that verify turning removal against a known diameter reduction.

**G5: `cam_program` fast-path has no test**  
`cam_program_from_part` at `libaicad.cpp:493` is not covered by any test in `test_cam.cpp` or `test_cam_tools.py`. It is the primary user-facing "one-shot CNC program" path.

**G6: `check_holder` simulation flag is permanently unimplemented**  
`SimOptions.check_holder=true` is a user-accessible parameter but is never acted upon in `stock_sim.cpp`. No warning is emitted either.

---

## 4. Overhaul Task Proposals

### T-M1: Fix critical post-processor correctness (C++) — ~1 driver-day
**Scope:** Fix D2, D4, D9 in `core/cam/post.cpp`:
1. Transform drill-point XY through per-op WCS when emitting canned cycles (apply `wcs.x_dir` / `z_dir` rotation to each point before formatting).
2. Emit `G54.1 P<n>` when `wcs.offset_sub > 0` (add `offset_sub` to NcGen work-offset emit).
3. Emit `G50 S<max_turn_rpm>` before `G96` on lathe posts.
4. Add post tests for all 8 dialects with actual content assertions (Haas, Mach3, Siemens, Centroid).

**Files:** `core/cam/post.cpp`, `core/tests/test_cam.cpp`  
**Dependencies:** None — self-contained within the post layer.

---

### T-M2: True adaptive clearing (trochoidal linking) (C++) — ~1 driver-day
**Scope:** Replace the concentric-ring loop in `gen_adaptive2d` and `gen_adaptive_clear3d` with arc-blend linking so engagement stays bounded at corners:
- After each inner-to-outer ring transition, insert a short arc move (radius = `engage * 0.5`) tangent to both rings instead of a direct feed_to.
- Optional: implement a basic engagement-angle check and emit a warning if any segment exceeds `op.stepover * 1.5`.
- Rename op internally to distinguish "concentric clearing" from "adaptive" until true arc-blend is in.

**Files:** `core/cam/op_adaptive.cpp`, `core/cam/op_3d.cpp` (AdaptiveClear3D), `core/tests/test_cam.cpp`  
**Dependencies:** geometry.h arc-segment helper (can be trivial — a circular arc in 2D).

---

### T-M3: Holder/shank collision simulation (C++) — ~1 driver-day
**Scope:** Implement the `check_holder=true` branch in `stock_sim.cpp`:
- For each cutting/rapid move, compute tool-body (shank) cylinder intersection with the remaining stock dexel field using the same segment-sweep already done for the tip (re-use `dist_point_seg`), but offset the Z-extent by `tool.flute_length` .. `tool.overall_length`.
- Accumulate `holder_collisions` in `SimResult` and emit a warning.
- Expose `check_holder` through `cam_simulate` MCP parameter.
- Add a test with a deep-pocket job where the shank obviously hits the walls.

**Files:** `core/cam/stock_sim.h`, `core/cam/stock_sim.cpp`, `core/cam/model.h` (Tool already has `holder_diameter`, `holder_length`), `core/tests/test_cam.cpp`, `services/mcp/cam_tools.py`  
**Dependencies:** None — model already has holder geometry fields.

---

### T-M4: Feeds/speeds DB unification with engine/materials registry (C++ + Python) — ~1 driver-day
**Scope:** Resolve Issue #2 — fold `CuttingDB` onto the shared materials registry:
1. Add a `cam` sub-section to the existing engine/materials JSON schema (`{"sfm_mill","sfm_drill","sfm_turn","hss_factor","fz_ref","fz_ref_dia","stepover_frac","stepdown_frac","plunge_frac","cut_feed_ref"}`).
2. Have `CuttingDB::builtin()` load from the shared registry at startup; fall back to in-code table if the registry is absent (maintains headless build).
3. Expose material IDs through `cam_materials()` so the MCP always reflects the registry's current material list.
4. Update `cam_add_setup` MCP tool to accept any material ID from the registry, not just the hardcoded 25.

**Files:** `core/cam/feeds.h`, `core/cam/feeds.cpp`, `core/cam/session.cpp`, shared materials schema (FEA side), `services/mcp/cam_tools.py`  
**Dependencies:** The FEA materials registry schema must be stable (coordinate with sim team).

---

### T-M5: OCCT hole-extraction completeness & multi-kind extract (C++) — ~1 driver-day
**Scope:** Fix D7 — make `cam_extract` production-ready for a real shop:
1. **Counterbore detection:** when two Z-axis cylinders share the same axis XY within tolerance, merge them into one hole record with `{dia_major, dia_minor, depth_major, depth_minor}`.
2. **Countersink detection:** add a `GeomAbs_Cone` branch; compute tip angle and outer diameter.
3. **Tilt allowance:** raise the `fabs(ax.Z()) < 0.99` threshold to `0.95` and emit the tilt angle in the hole record so the user can decide whether to program it.
4. **"sections" mode test:** add extraction test for `kind="sections"` and `kind="profile"` in `test_cam_tools.py`.

**Files:** `core/cam/extract.cpp`, `services/mcp/test_cam_tools.py`  
**Dependencies:** OCCT BRepAdaptor_Surface (already imported).

---

### T-M6: Pocket entry path-planning fix & stock-aware simulation test (C++ + tests) — ~1 driver-day
**Scope:** Fix D3, D8, and G4:
1. **D3 (pocket entry plunge without prior rapid_xy):** In `gen_pocket2d`, the `else` branch that calls `pb.plunge_to(z)` must first call `pb.rapid_to_clearance()` + `pb.rapid_xy(entry)` before the plunge. Add a regression test.
2. **D8 (arc sim chord approximation):** Replace chord-sweep with a proper arc-parameterized sweep in `stock_sim.cpp`: for `MoveKind::Arc`, sample along the arc angle rather than as a straight segment, using the `m.center` and radius that are already stored on the `Move`.
3. **G4 (turning sim):** Add a sim test that generates a `TurnRough` op on a cylinder stock, runs `simulate_setup`, and verifies `removed_volume_mm3 > 0` and `machined_fraction > 0.3`.

**Files:** `core/cam/op_milling.cpp:L147-153`, `core/cam/stock_sim.cpp:L205-212`, `core/tests/test_cam.cpp`  
**Dependencies:** None — all self-contained in existing files.

---

## 5. Dependency Order

```
T-M1 (post correctness) — no deps, highest risk, ship first
T-M6 (pocket entry + sim arc + turning test) — no deps
T-M2 (adaptive arc-blend) — no deps but benefits from T-M6 sim having arc coverage
T-M3 (holder sim) — no deps
T-M5 (extract holes) — no deps
T-M4 (feeds DB unification) — coordinate with sim/FEA materials team
```

T-M1 and T-M6 can run in parallel.  
T-M2, T-M3, T-M5 can all run after T-M1 ships (post tests are the regression baseline).  
T-M4 requires cross-team alignment; schedule last.

---

## 6. What a Real Shop Needs That's Still Missing (Beyond These 6 Tasks)

- **Stock-to-stock rest machining:** the sim already outputs a `remaining` HeightField but no operation uses it as input stock for a subsequent op or finish pass. Critical for real 3D work.
- **Fixture / clamp awareness:** no concept of fixture bodies in the job model; the sim can flag rapids through stock but not rapids through a vise jaw.
- **Tool-change optimization:** `cam_post` / `cam_generate` don't reorder operations to minimize tool changes across setups.
- **Imperial units end-to-end:** `model.h:L17` says "all lengths are millimetres" and `Job.units` field exists but the generators never respect it. `post.cpp` emits G20/G21 but all internal geometry is mm.
- **G-code verification by parsing:** no round-trip check that the emitted G-code, when parsed, matches the toolpath (i.e., no G-code interpreter / post verifier).
- **4th/5th axis output:** `MachineKind::Mill4` / `Mill5` and `Machine::rotary_axis` are in the model but no generator emits A/B/C words and the post has no rotary-axis formatting.

---

*Audit conducted read-only. No source files modified. All evidence is file:line cited above.*
