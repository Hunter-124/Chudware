# AI-CAD UI Walkthrough Checklist — Round 1

**Instructions for Erik:**
1. Launch the app with the scenario shown in the *launch* column.
2. For each row, mark `keep`, `fix`, or `kill` and add free-text notes.
3. Return the annotated file as `work/questions/T-G12-feedback-round1.md`.
4. Must-fix findings block sign-off (T-G9/T-G11) until resolved.

**Convergence rule:** the loop repeats until a full pass yields zero new must-fix findings.
Each round regenerates this file from code via `python3 scripts/ui_inventory.py`.
The `--diff prev.md` flag highlights NEW / CHANGED / REMOVED rows.

---

## PRIORITY: T-G2 NEEDS-LIVE Click-Tests

These four items were flagged by T-G2 as requiring a live interactive session.
**Test these first** — they are the immediate unresolved debt from the last round.

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `doc_tab_plus_x` | Doc Tabs | Doc-tab strip — '+' (new part) and 'x' (close tab) buttons | Live app — open a part, click '+' then 'x' on the tab | `[ ]` keep / `[ ]` fix / `[ ]` kill | **T-G2 NEEDS-LIVE #1**: code fix (NoBringToFrontOnFocus removed) in place; confirm click response |
| `copilot_collapse_pill` | Copilot Panel | Copilot header chevron → collapse to pill → click pill to re-expand | Live app — Copilot panel visible, click chevron then pill | `[ ]` keep / `[ ]` fix / `[ ]` kill | **T-G2 NEEDS-LIVE #2**: collapse-to-pill round trip requires live click |
| `viewport_toolbar_clamp` | Viewport Toolbar | Viewport toolbar left-edge clamp against FeatureManager column | AI_CAD_SHOT=sketch — open sketch, pan a large sketch; check toolbar doesn't overlap FM | `[ ]` keep / `[ ]` fix / `[ ]` kill | **T-G2 NEEDS-LIVE #3**: added in T-G1 polish; needs live sketch pan to confirm dodge |
| `extrude_end_condition_dropdown` | PM Form — Extrude | Extrude PM → End Condition dropdown (Direction 1) | AI_CAD_SHOT=m1 (PM active after fillet edit) — or launch live and run Extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | **T-G2 NEEDS-LIVE #4**: FrameBg fix applied; confirm dropdown text readable over light/dark |

---

## Ribbon — Features Tab

Launch: `AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `sketch_new` | Ribbon/Features | Features → Sketch → New Sketch (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `extrude_boss` | Ribbon/Features | Features → 3D Body → Extrude (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `extrude_cut` | Ribbon/Features | Features → 3D Body → Extrude Cut (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `revolve_boss` | Ribbon/Features | Features → 3D Body → Revolve (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `revolve_cut` | Ribbon/Features | Features → 3D Body → Revolve Cut | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `revolve_boss` |
| `sweep_boss` | Ribbon/Features | Features → 3D Body → Sweep (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sweep_cut` | Ribbon/Features | Features → 3D Body → Sweep Cut | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sweep_boss` |
| `loft_boss` | Ribbon/Features | Features → 3D Body → Loft (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `loft_cut` | Ribbon/Features | Features → 3D Body → Loft Cut | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `loft_boss` |
| `fillet` | Ribbon/Features | Features → Dress-up → Fillet (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `chamfer` | Ribbon/Features | Features → Dress-up → Chamfer (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `shell` | Ribbon/Features | Features → Dress-up → Shell (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `draft` | Ribbon/Features | Features → Dress-up → Draft | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `rib` | Ribbon/Features | Features → Dress-up → Rib | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `hole_wizard` | Ribbon/Features | Features → Hole → Hole Wizard (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `linear_pattern` | Ribbon/Features | Features → Pattern → Lin. Pattern (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `circular_pattern` | Ribbon/Features | Features → Pattern → Circ. Pattern (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `mirror_feature` | Ribbon/Features | Features → Pattern → Mirror (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ref_plane` | Ribbon/Features | Features → Reference → Ref. Plane (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ref_axis` | Ribbon/Features | Features → Reference → Ref. Axis | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ref_point` | Ribbon/Features | Features → Reference → Ref. Point | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ref_csys` | Ribbon/Features | Features → Reference → Coord. Sys. | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `boolean_combine` | Ribbon/Features | Features → Multi-body → Combine (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `boolean_subtract` | Ribbon/Features | Features → Multi-body → Subtract | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `boolean_intersect` | Ribbon/Features | Features → Multi-body → Intersect | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `curve_helix` | Ribbon/Features | Features → Curves → Helix (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `split` | Ribbon/Features | Features → Curves → Split | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Sketch Tab

Launch: `AI_CAD_SHOT=sketch — enter sketch, draw, add dim, exit to solid`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `sketch_exit` | Ribbon/Sketch | Sketch → Sketch → Exit Sketch (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_line` | Ribbon/Sketch | Sketch → Draw → Line (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_rect_corner` | Ribbon/Sketch | Sketch → Draw → Rectangle (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_rect_center` | Ribbon/Sketch | Sketch → Draw → Ctr. Rect. | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_rect_corner` |
| `sk_circle` | Ribbon/Sketch | Sketch → Draw → Circle (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_arc_3pt` | Ribbon/Sketch | Sketch → Draw → 3-Pt Arc (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_arc_center` | Ribbon/Sketch | Sketch → Draw → Ctr. Arc | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_arc_3pt` |
| `sk_arc_tangent` | Ribbon/Sketch | Sketch → Draw → Tang. Arc | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_arc_3pt` |
| `sk_polygon` | Ribbon/Sketch | Sketch → Draw → Polygon (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_slot` | Ribbon/Sketch | Sketch → Draw → Slot (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_spline` | Ribbon/Sketch | Sketch → Draw → Spline (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_ellipse` | Ribbon/Sketch | Sketch → Draw → Ellipse | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_point` | Ribbon/Sketch | Sketch → Draw → Point | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_text` | Ribbon/Sketch | Sketch → Draw → Text | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_rect_3pt_corner` | Ribbon/Sketch | Sketch → Draw → 3Pt Cor. Rect | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_rect_corner` |
| `sk_rect_3pt_center` | Ribbon/Sketch | Sketch → Draw → 3Pt Ctr. Rect | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_rect_corner` |
| `sk_parallelogram` | Ribbon/Sketch | Sketch → Draw → Parallelogram | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_rect_corner` |
| `sk_circle_perimeter` | Ribbon/Sketch | Sketch → Draw → Perim. Circle | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_circle` |
| `sk_slot_ctr_str` | Ribbon/Sketch | Sketch → Draw → Ctr. Str. Slot | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_slot` |
| `sk_slot_arc_3pt` | Ribbon/Sketch | Sketch → Draw → 3Pt Arc Slot | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_slot` |
| `sk_slot_arc_ctr` | Ribbon/Sketch | Sketch → Draw → Ctr. Arc Slot | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_slot` |
| `sk_spline_style` | Ribbon/Sketch | Sketch → Draw → Style Spline | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_spline` |
| `sk_spline_surface` | Ribbon/Sketch | Sketch → Draw → Spline on Surf. | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_spline`; ⚠ stretch=disabled (Coming Soon) |
| `sk_spline_equation` | Ribbon/Sketch | Sketch → Draw → Eq. Driven Crv. | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_spline` |
| `sk_airfoil` | Ribbon/Sketch | Sketch → Draw → Airfoil (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_ellipse_big` | Ribbon/Sketch | Sketch → Draw → Ellipse | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_ellipse_partial` | Ribbon/Sketch | Sketch → Draw → Partial Ellipse | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_ellipse_big` |
| `sk_parabola` | Ribbon/Sketch | Sketch → Draw → Parabola | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_ellipse_big` |
| `sk_conic` | Ribbon/Sketch | Sketch → Draw → Conic | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_ellipse_big` |
| `sk_poly_tri` | Ribbon/Sketch | Sketch → Draw → Triangle | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `sk_poly_quad` | Ribbon/Sketch | Sketch → Draw → Square | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `sk_poly_pent` | Ribbon/Sketch | Sketch → Draw → Pentagon | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `sk_poly_hex` | Ribbon/Sketch | Sketch → Draw → Hexagon | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `sk_poly_oct` | Ribbon/Sketch | Sketch → Draw → Octagon | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `sk_poly_star` | Ribbon/Sketch | Sketch → Draw → Star | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill sub-button of `sk_polygon` |
| `smart_dim` | Ribbon/Sketch | Sketch → Dimension → Smart Dim. (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_trim` | Ribbon/Sketch | Sketch → Sketch Tools → Trim (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_extend` | Ribbon/Sketch | Sketch → Sketch Tools → Extend (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_offset_ent` | Ribbon/Sketch | Sketch → Sketch Tools → Offset (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_fillet` | Ribbon/Sketch | Sketch → Sketch Tools → Sk. Fillet | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_chamfer` | Ribbon/Sketch | Sketch → Sketch Tools → Sk. Chamfer | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_convert_ent` | Ribbon/Sketch | Sketch → Sketch Tools → Convert (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_construction` | Ribbon/Sketch | Sketch → Sketch Tools → Construction | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_mirror` | Ribbon/Sketch | Sketch → Sketch Tools → Mirror | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_lin_pattern` | Ribbon/Sketch | Sketch → Sketch Tools → Lin. Pattern | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_circ_pattern` | Ribbon/Sketch | Sketch → Sketch Tools → Circ. Pattern | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_move` | Ribbon/Sketch | Sketch → Sketch Tools → Move | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_relations` | Ribbon/Sketch | Sketch → Relations → Relations (big button) | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sk_fix` | Ribbon/Sketch | Sketch → Relations → Fix | `sketch` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Evaluate Tab

Launch: `AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `eval_measure` | Ribbon/Evaluate | Evaluate → Measure → Measure (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_mass_props` | Ribbon/Evaluate | Evaluate → Measure → Mass Props. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_section_props` | Ribbon/Evaluate | Evaluate → Measure → Section Props. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_draft` | Ribbon/Evaluate | Evaluate → Geometry → Draft Anlys. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_thickness` | Ribbon/Evaluate | Evaluate → Geometry → Thickness (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_curvature` | Ribbon/Evaluate | Evaluate → Geometry → Curvature (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_zebra` | Ribbon/Evaluate | Evaluate → Geometry → Zebra | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_symmetry` | Ribbon/Evaluate | Evaluate → Geometry → Symmetry | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_check` | Ribbon/Evaluate | Evaluate → Geometry Check → Check (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_interference` | Ribbon/Evaluate | Evaluate → Geometry Check → Interference (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `eval_geometry` | Ribbon/Evaluate | Evaluate → Geometry Check → Geometry Anlys. | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `make_drawing` | Ribbon/Evaluate | Evaluate → Drawing → Make Drawing (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `prof_toggle` | Ribbon/Evaluate | Evaluate → Performance → Profiler (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Simulation Tab

Launch: `AI_CAD_SHOT=fea — static FEA study`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `sim_new_static` | Ribbon/Simulation | Simulation → Study → Static Study (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_new_modal` | Ribbon/Simulation | Simulation → Study → Modal Study (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_new_flow` | Ribbon/Simulation | Simulation → Study → Flow Study (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_new_thermal` | Ribbon/Simulation | Simulation → Study → Thermal Study (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_new_fatigue` | Ribbon/Simulation | Simulation → Study → Fatigue Study (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_material` | Ribbon/Simulation | Simulation → Materials → Material (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_fixture` | Ribbon/Simulation | Simulation → Fixtures → Fixture (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_load_force` | Ribbon/Simulation | Simulation → Loads → Force (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_load_pressure` | Ribbon/Simulation | Simulation → Loads → Pressure (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_mesh` | Ribbon/Simulation | Simulation → Mesh → Mesh (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_run` | Ribbon/Simulation | Simulation → Run → Run (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sim_results` | Ribbon/Simulation | Simulation → Run → Results (big button) | `fea` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Assembly Tab

Launch: `AI_CAD_SHOT=asm — assembly with mate`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `asm_insert` | Ribbon/Assembly | Assembly → Components → Insert Comp. (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_parts_library` | Ribbon/Assembly | Assembly → Components → Std. Parts (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_move` | Ribbon/Assembly | Assembly → Components → Move Comp. (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_pattern` | Ribbon/Assembly | Assembly → Components → Comp. Pattern (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_mate` | Ribbon/Assembly | Assembly → Mate → Mate (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_interference` | Ribbon/Assembly | Assembly → Evaluate → Interference (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_bom` | Ribbon/Assembly | Assembly → Evaluate → BOM (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `asm_explode` | Ribbon/Assembly | Assembly → Display → Exploded View (big button) | `asm` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Sheet Metal Tab

Launch: `AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `sm_base_flange` | Ribbon/Sheet Metal | Sheet Metal → Create → Base Flange (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sm_edge_flange` | Ribbon/Sheet Metal | Sheet Metal → Create → Edge Flange (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sm_hem` | Ribbon/Sheet Metal | Sheet Metal → Create → Hem (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sm_jog` | Ribbon/Sheet Metal | Sheet Metal → Create → Jog (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sm_flat_pattern` | Ribbon/Sheet Metal | Sheet Metal → Flat Pattern → Flatten (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sm_export_flat_dxf` | Ribbon/Sheet Metal | Sheet Metal → Flat Pattern → Export Flat DXF (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Weldments Tab

Launch: `AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `weld_member` | Ribbon/Weldments | Weldments → Structural → Structural Member (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `weld_gusset` | Ribbon/Weldments | Weldments → Dress → Gusset (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `weld_end_cap` | Ribbon/Weldments | Weldments → Dress → End Cap (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — PCB Tab

Launch: `Live app`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `ecad_ai_design` | Ribbon/PCB | PCB → New → AI Design (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_new_sch` | Ribbon/PCB | PCB → New → Schematic (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_new_pcb` | Ribbon/PCB | PCB → New → PCB Layout (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_open` | Ribbon/PCB | PCB → New → Open | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_save` | Ribbon/PCB | PCB → New → Save | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sch_add_symbol` | Ribbon/PCB | PCB → Schematic → Place Symbol (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sch_add_wire` | Ribbon/PCB | PCB → Schematic → Wire | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sch_add_label` | Ribbon/PCB | PCB → Schematic → Net Label | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sch_add_power` | Ribbon/PCB | PCB → Schematic → Power Port | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `sch_annotate` | Ribbon/PCB | PCB → Schematic → Annotate | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_run_erc` | Ribbon/PCB | PCB → Schematic → Run ERC | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `pcb_update_from_sch` | Ribbon/PCB | PCB → Board → Update Board (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `pcb_add_footprint` | Ribbon/PCB | PCB → Board → Footprint | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `pcb_add_track` | Ribbon/PCB | PCB → Board → Route | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `pcb_add_via` | Ribbon/PCB | PCB → Board → Via | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `pcb_add_zone` | Ribbon/PCB | PCB → Board → Copper Zone | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_autoroute` | Ribbon/PCB | PCB → Board → Autoroute (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_fill_zones` | Ribbon/PCB | PCB → Board → Fill Zones | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_run_drc` | Ribbon/PCB | PCB → Board → Run DRC | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_simulate` | Ribbon/PCB | PCB → Analyze → Simulate (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_to_3d` | Ribbon/PCB | PCB → Outputs → Board to 3D (big button) | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_export_gerber` | Ribbon/PCB | PCB → Outputs → Gerbers | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_export_drill` | Ribbon/PCB | PCB → Outputs → Drill | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `ecad_export_bom` | Ribbon/PCB | PCB → Outputs → BOM | `live` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Surfaces Tab

Launch: `AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `surf_extrude` | Ribbon/Surfaces | Surfaces → Create → Extruded Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_revolve` | Ribbon/Surfaces | Surfaces → Create → Revolved Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_sweep` | Ribbon/Surfaces | Surfaces → Create → Swept Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_loft` | Ribbon/Surfaces | Surfaces → Create → Lofted Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_planar` | Ribbon/Surfaces | Surfaces → Create → Planar Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_offset` | Ribbon/Surfaces | Surfaces → Curves & Patches → Offset Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_ruled` | Ribbon/Surfaces | Surfaces → Curves & Patches → Ruled Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_boundary` | Ribbon/Surfaces | Surfaces → Curves & Patches → Boundary Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_fill` | Ribbon/Surfaces | Surfaces → Curves & Patches → Filled Surf. | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_knit` | Ribbon/Surfaces | Surfaces → Edit → Knit Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_thicken` | Ribbon/Surfaces | Surfaces → Edit → Thicken (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_trim` | Ribbon/Surfaces | Surfaces → Edit → Trim Surf. (big button) | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_untrim` | Ribbon/Surfaces | Surfaces → Edit → Untrim Surf. | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_delete_face` | Ribbon/Surfaces | Surfaces → Edit → Delete Face | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `surf_replace_face` | Ribbon/Surfaces | Surfaces → Edit → Replace Face | `m1` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — View Layout Tab

Launch: `AI_CAD_SHOT=drawing — engineering drawing sheet with views`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `dwg_view_model` | Ribbon/View Layout | View Layout → Views → Model View (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_view_proj` | Ribbon/View Layout | View Layout → Views → Projected (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_view_std3` | Ribbon/View Layout | View Layout → Views → Standard 3 (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_view_section` | Ribbon/View Layout | View Layout → Views → Section (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_view_detail` | Ribbon/View Layout | View Layout → Views → Detail (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_sheet_props` | Ribbon/View Layout | View Layout → Sheet → Sheet Props (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_rebuild` | Ribbon/View Layout | View Layout → Sheet → Rebuild | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_fit` | Ribbon/View Layout | View Layout → Sheet → Fit | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_export_dxf` | Ribbon/View Layout | View Layout → Sheet → Export DXF | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_export_pdf` | Ribbon/View Layout | View Layout → Sheet → Export PDF | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_section_half` | Ribbon/View Layout | View Layout → Section → Half Section | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_section_reverse` | Ribbon/View Layout | View Layout → Section → Flip Side | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_scale_1_1` | Ribbon/View Layout | View Layout → Scale → 1:1 | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_scale_1_2` | Ribbon/View Layout | View Layout → Scale → 1:2 | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_scale_2_1` | Ribbon/View Layout | View Layout → Scale → 2:1 | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Annotation Tab

Launch: `AI_CAD_SHOT=drawing — engineering drawing sheet with views`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `dwg_smart_dim` | Ribbon/Annotation | Annotation → Dimensions → Smart Dim (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_model_items` | Ribbon/Annotation | Annotation → Dimensions → Model Items (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_hole_callout` | Ribbon/Annotation | Annotation → Dimensions → Hole Callout (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_gdt` | Ribbon/Annotation | Annotation → GD&T → Geo Tol (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_datum` | Ribbon/Annotation | Annotation → GD&T → Datum | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_datum_target` | Ribbon/Annotation | Annotation → GD&T → Datum Tgt | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_surface_finish` | Ribbon/Annotation | Annotation → GD&T → Surf Finish | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_weld` | Ribbon/Annotation | Annotation → GD&T → Weld | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_center_mark` | Ribbon/Annotation | Annotation → Annotations → Center Mark (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_center_line` | Ribbon/Annotation | Annotation → Annotations → Center Line (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_note` | Ribbon/Annotation | Annotation → Annotations → Note (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_balloon` | Ribbon/Annotation | Annotation → Annotations → Balloon | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_auto_balloon` | Ribbon/Annotation | Annotation → Annotations → Auto Balloon | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_title_block` | Ribbon/Annotation | Annotation → Tables → Title Block (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `dwg_bom_table` | Ribbon/Annotation | Annotation → Tables → Bill of Mat. (big button) | `drawing` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Appearance Tab

Launch: `AI_CAD_SHOT=appearance — appearance/material scenario`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `appr_color` | Ribbon/Appearance | Appearance → Material → Appearance (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_mat_plastic` | Ribbon/Appearance | Appearance → Material → Plastic (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_mat_aluminum` | Ribbon/Appearance | Appearance → Material → Aluminum (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_mat_steel` | Ribbon/Appearance | Appearance → Material → Steel (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_mat_brass` | Ribbon/Appearance | Appearance → Material → Brass (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_mat_dark` | Ribbon/Appearance | Appearance → Material → Dark Metal (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `appr_clear` | Ribbon/Appearance | Appearance → Material → Clear | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `display_shaded` | Ribbon/Appearance | Appearance → Display → Shaded (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `display_shaded_edges` | Ribbon/Appearance | Appearance → Display → Shaded Edges (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `display_hlr` | Ribbon/Appearance | Appearance → Display → Hidden Removed (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `display_hlv` | Ribbon/Appearance | Appearance → Display → Hidden Visible | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `display_wireframe` | Ribbon/Appearance | Appearance → Display → Wireframe (big button) | `appearance` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

## Ribbon — Manufacture Tab

Launch: `AI_CAD_SHOT=cam — CAM job fast-path`

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `cam_new_job` | Ribbon/Manufacture | Manufacture → Job → New Job (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_extract` | Ribbon/Manufacture | Manufacture → Job → From Part (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_add_setup` | Ribbon/Manufacture | Manufacture → Job → Setup | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_add_tool` | Ribbon/Manufacture | Manufacture → Job → Tool | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_face` | Ribbon/Manufacture | Manufacture → 2.5D → Face (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_contour` | Ribbon/Manufacture | Manufacture → 2.5D → Contour (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_pocket` | Ribbon/Manufacture | Manufacture → 2.5D → Pocket (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_adaptive` | Ribbon/Manufacture | Manufacture → 2.5D → Adaptive | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_slot` | Ribbon/Manufacture | Manufacture → 2.5D → Slot | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_chamfer` | Ribbon/Manufacture | Manufacture → 2.5D → Chamfer | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_engrave` | Ribbon/Manufacture | Manufacture → 2.5D → Engrave | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_drill` | Ribbon/Manufacture | Manufacture → Holes → Drill (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_thread` | Ribbon/Manufacture | Manufacture → Holes → Thread Mill | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_parallel3d` | Ribbon/Manufacture | Manufacture → 3D → Parallel (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_waterline3d` | Ribbon/Manufacture | Manufacture → 3D → Waterline (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_scallop3d` | Ribbon/Manufacture | Manufacture → 3D → Scallop | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_pocket3d` | Ribbon/Manufacture | Manufacture → 3D → 3D Pocket | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_turn_rough` | Ribbon/Manufacture | Manufacture → Turning → Rough (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_turn_finish` | Ribbon/Manufacture | Manufacture → Turning → Finish | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_turn_face` | Ribbon/Manufacture | Manufacture → Turning → Face | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_turn_part` | Ribbon/Manufacture | Manufacture → Turning → Part Off | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_laser` | Ribbon/Manufacture | Manufacture → Cutting → Laser/Plasma (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_op_probe` | Ribbon/Manufacture | Manufacture → Cutting → Probe | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_generate` | Ribbon/Manufacture | Manufacture → Output → Generate (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_simulate` | Ribbon/Manufacture | Manufacture → Output → Simulate (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `cam_post` | Ribbon/Manufacture | Manufacture → Output → Post G-code (big button) | `cam` | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

---

## Command Palette (⌘K) — All Commands

Searchable via ⌘K in the running app.  Top = primary button; Flyout = dropdown child; Variant = op variant.

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `palette/sketch_new` | Palette/Model | Model › Sketch › New Sketch (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: draw 2d profile |
| `palette/prim_box` | Palette/Model | Model › Create › Box (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: primitive block cube cuboid |
| `palette/prim_cylinder` | Palette/Model | Model › Create › Cylinder (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `prim_box`; kw: primitive rod disc |
| `palette/prim_sphere` | Palette/Model | Model › Create › Sphere (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `prim_box`; kw: primitive ball |
| `palette/prim_cone` | Palette/Model | Model › Create › Cone (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `prim_box`; kw: primitive taper frustum |
| `palette/extrude_boss` | Palette/Model | Model › Create › Extrude (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: boss pad protrusion |
| `palette/extrude_cut` | Palette/Model | Model › Create › Extrude Cut (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `extrude_boss`; kw: pocket remove |
| `palette/revolve_boss` | Palette/Model | Model › Create › Revolve (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: lathe turn |
| `palette/revolve_cut` | Palette/Model | Model › Create › Revolve Cut (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `revolve_boss`; kw: groove |
| `palette/sweep_boss` | Palette/Model | Model › Create › Sweep (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: profile path |
| `palette/sweep_cut` | Palette/Model | Model › Create › Sweep Cut (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sweep_boss` |
| `palette/loft_boss` | Palette/Model | Model › Create › Loft (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: blend sections |
| `palette/loft_cut` | Palette/Model | Model › Create › Loft Cut (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `loft_boss` |
| `palette/rib` | Palette/Model | Model › Create › Rib (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: web stiffener |
| `palette/hole_wizard` | Palette/Model | Model › Create › Hole (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: drill bore counterbore tap |
| `palette/curve_helix` | Palette/Model | Model › Create › Coil (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: helix spring thread |
| `palette/fillet` | Palette/Model | Model › Modify › Fillet (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: round edge blend |
| `palette/chamfer` | Palette/Model | Model › Modify › Chamfer (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: bevel edge |
| `palette/shell` | Palette/Model | Model › Modify › Shell (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: hollow thin wall |
| `palette/draft` | Palette/Model | Model › Modify › Draft (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: taper mold angle |
| `palette/split` | Palette/Model | Model › Modify › Split (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: divide body |
| `palette/boolean_combine` | Palette/Model | Model › Modify › Combine (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: boolean union add |
| `palette/boolean_subtract` | Palette/Model | Model › Modify › Subtract (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `boolean_combine`; kw: boolean difference |
| `palette/boolean_intersect` | Palette/Model | Model › Modify › Intersect (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `boolean_combine`; kw: boolean common |
| `palette/linear_pattern` | Palette/Model | Model › Pattern › Rect. Pattern (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: array grid repeat |
| `palette/circular_pattern` | Palette/Model | Model › Pattern › Circ. Pattern (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: radial array repeat |
| `palette/mirror_feature` | Palette/Model | Model › Pattern › Mirror (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: reflect symmetry |
| `palette/ref_plane` | Palette/Model | Model › Construct › Plane (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: datum reference |
| `palette/ref_axis` | Palette/Model | Model › Construct › Axis (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: datum line |
| `palette/ref_point` | Palette/Model | Model › Construct › Point (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: datum vertex |
| `palette/ref_csys` | Palette/Model | Model › Construct › Coord Sys (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: csys triad origin |
| `palette/eval_measure` | Palette/Model | Model › Inspect › Measure (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: distance angle dimension |
| `palette/eval_mass_props` | Palette/Model | Model › Inspect › Mass Props (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: volume density center |
| `palette/eval_section_props` | Palette/Model | Model › Inspect › Section Props (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_mass_props`; kw: moment inertia |
| `palette/eval_thickness` | Palette/Model | Model › Inspect › Thickness (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: wall min |
| `palette/eval_draft` | Palette/Model | Model › Inspect › Draft Anlys (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: draft angle mold |
| `palette/eval_curvature` | Palette/Model | Model › Inspect › Curvature (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_draft`; kw: surface |
| `palette/eval_zebra` | Palette/Model | Model › Inspect › Zebra (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_draft`; kw: surface continuity |
| `palette/eval_symmetry` | Palette/Model | Model › Inspect › Symmetry (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_draft`; kw: mirror |
| `palette/eval_check` | Palette/Model | Model › Inspect › Check (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: geometry validity |
| `palette/eval_interference` | Palette/Model | Model › Inspect › Interference (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_check`; kw: clash |
| `palette/eval_geometry` | Palette/Model | Model › Inspect › Geometry Anlys (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_check` |
| `palette/eval_feature_overlap` | Palette/Model | Model › Inspect › Feature Overlap (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `eval_check`; kw: feature overlap boss cut swallowed clash |
| `palette/prof_toggle` | Palette/Model | Model › Inspect › Profiler (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: performance sampling |
| `palette/ai_reverse_engineer` | Palette/Model | Model › AI › Reverse Engineer (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: reverse engineer import parametric rebuild editable |
| `palette/ai_explain_part` | Palette/Model | Model › AI › Explain Part (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: describe explain geometry text |
| `palette/appearance_edit` | Palette/Model | Model › Appearance › Appearance (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: color material finish texture realview render swatch |
| `palette/view_style` | Palette/Model | Model › Appearance › Display (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: shaded wireframe edges display style hidden |
| `palette/view_style_shaded` | Palette/Model | Model › Appearance › Shaded (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `view_style` |
| `palette/view_style_edges` | Palette/Model | Model › Appearance › Shaded + Edges (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `view_style` |
| `palette/view_style_wire` | Palette/Model | Model › Appearance › Wireframe (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `view_style` |
| `palette/make_drawing` | Palette/Model | Model › Output › Make Drawing (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: 2d sheet draft |
| `palette/sk_line` | Palette/Sketch | Sketch › Draw › Line (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: segment polyline |
| `palette/sk_rect_corner` | Palette/Sketch | Sketch › Draw › Rectangle (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: box |
| `palette/sk_rect_center` | Palette/Sketch | Sketch › Draw › Center Rect (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_rect_corner` |
| `palette/sk_rect_3pt_corner` | Palette/Sketch | Sketch › Draw › 3-Pt Corner Rect (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_rect_corner` |
| `palette/sk_rect_3pt_center` | Palette/Sketch | Sketch › Draw › 3-Pt Center Rect (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_rect_corner` |
| `palette/sk_parallelogram` | Palette/Sketch | Sketch › Draw › Parallelogram (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_rect_corner` |
| `palette/sk_circle` | Palette/Sketch | Sketch › Draw › Circle (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/sk_circle_perimeter` | Palette/Sketch | Sketch › Draw › Perimeter Circle (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_circle` |
| `palette/sk_arc_3pt` | Palette/Sketch | Sketch › Draw › Arc (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: 3 point |
| `palette/sk_arc_center` | Palette/Sketch | Sketch › Draw › Center Arc (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_arc_3pt` |
| `palette/sk_arc_tangent` | Palette/Sketch | Sketch › Draw › Tangent Arc (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_arc_3pt` |
| `palette/sk_polygon` | Palette/Sketch | Sketch › Draw › Polygon (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: hexagon ngon |
| `palette/sk_slot` | Palette/Sketch | Sketch › Draw › Slot (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: capsule |
| `palette/sk_slot_ctr_str` | Palette/Sketch | Sketch › Draw › Center Slot (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_slot` |
| `palette/sk_slot_arc_3pt` | Palette/Sketch | Sketch › Draw › 3-Pt Arc Slot (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_slot` |
| `palette/sk_slot_arc_ctr` | Palette/Sketch | Sketch › Draw › Center Arc Slot (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_slot` |
| `palette/sk_spline` | Palette/Sketch | Sketch › Draw › Spline (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: curve bezier |
| `palette/sk_spline_style` | Palette/Sketch | Sketch › Draw › Style Spline (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_spline` |
| `palette/sk_spline_equation` | Palette/Sketch | Sketch › Draw › Equation Curve (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_spline` |
| `palette/sk_airfoil` | Palette/Sketch | Sketch › Draw › Airfoil (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_spline`; kw: naca wing |
| `palette/sk_spline_surface` | Palette/Sketch | Sketch › Draw › Spline on Surface (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_spline` |
| `palette/sk_ellipse_big` | Palette/Sketch | Sketch › Draw › Ellipse (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: oval conic |
| `palette/sk_ellipse` | Palette/Sketch | Sketch › Draw › Ellipse (Variant) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_ellipse_big` |
| `palette/sk_ellipse_partial` | Palette/Sketch | Sketch › Draw › Partial Ellipse (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_ellipse_big` |
| `palette/sk_parabola` | Palette/Sketch | Sketch › Draw › Parabola (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_ellipse_big`; kw: conic |
| `palette/sk_conic` | Palette/Sketch | Sketch › Draw › Conic (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_ellipse_big` |
| `palette/sk_point` | Palette/Sketch | Sketch › Draw › Point (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/sk_text` | Palette/Sketch | Sketch › Draw › Text (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: label |
| `palette/smart_dim` | Palette/Sketch | Sketch › Dimension › Smart Dimension (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: constrain measure |
| `palette/sk_trim` | Palette/Sketch | Sketch › Modify › Trim (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: cut entity |
| `palette/sk_extend` | Palette/Sketch | Sketch › Modify › Extend (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/sk_offset_ent` | Palette/Sketch | Sketch › Modify › Offset (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: parallel |
| `palette/sk_fillet` | Palette/Sketch | Sketch › Modify › Fillet (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: round corner |
| `palette/sk_chamfer` | Palette/Sketch | Sketch › Modify › Chamfer (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_fillet`; kw: bevel corner |
| `palette/sk_convert_ent` | Palette/Sketch | Sketch › Modify › Convert (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: project edge |
| `palette/sk_mirror` | Palette/Sketch | Sketch › Modify › Mirror (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: reflect |
| `palette/sk_lin_pattern` | Palette/Sketch | Sketch › Modify › Pattern (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: array repeat |
| `palette/sk_circ_pattern` | Palette/Sketch | Sketch › Modify › Circular Pattern (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_lin_pattern` |
| `palette/sk_move` | Palette/Sketch | Sketch › Modify › Move / Copy (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: translate |
| `palette/sk_relations` | Palette/Sketch | Sketch › Relations › Add Relation (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: constraint coincident parallel |
| `palette/sk_fix` | Palette/Sketch | Sketch › Relations › Fix (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sk_relations`; kw: ground anchor |
| `palette/sk_construction` | Palette/Sketch | Sketch › Relations › Construction (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: reference geometry |
| `palette/sketch_exit` | Palette/Sketch | Sketch › Finish › Exit Sketch (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: done close |
| `palette/sm_base_flange` | Palette/Sheet Metal | Sheet Metal › Create › Base Flange (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: tab sheet |
| `palette/sm_edge_flange` | Palette/Sheet Metal | Sheet Metal › Create › Edge Flange (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: bend wall |
| `palette/sm_hem` | Palette/Sheet Metal | Sheet Metal › Create › Hem (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: fold edge |
| `palette/sm_jog` | Palette/Sheet Metal | Sheet Metal › Create › Jog (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: offset step |
| `palette/sm_flat_pattern` | Palette/Sheet Metal | Sheet Metal › Modify › Flatten (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: unfold flat pattern |
| `palette/sm_export_flat_dxf` | Palette/Sheet Metal | Sheet Metal › Output › Export Flat DXF (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: laser cut |
| `palette/weld_member` | Palette/Weldment | Weldment › Create › Structural Member (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: beam profile frame |
| `palette/weld_gusset` | Palette/Weldment | Weldment › Modify › Gusset (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: corner plate |
| `palette/weld_end_cap` | Palette/Weldment | Weldment › Modify › End Cap (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: tube close |
| `palette/surf_extrude` | Palette/Surface | Surface › Create › Extrude (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: surface |
| `palette/surf_revolve` | Palette/Surface | Surface › Create › Revolve (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: surface |
| `palette/surf_sweep` | Palette/Surface | Surface › Create › Sweep (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: surface |
| `palette/surf_loft` | Palette/Surface | Surface › Create › Loft (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: surface |
| `palette/surf_planar` | Palette/Surface | Surface › Create › Planar (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: fill region |
| `palette/surf_offset` | Palette/Surface | Surface › Modify › Offset (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/surf_ruled` | Palette/Surface | Surface › Modify › Ruled (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/surf_boundary` | Palette/Surface | Surface › Modify › Boundary (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: patch |
| `palette/surf_fill` | Palette/Surface | Surface › Modify › Fill (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `surf_boundary` |
| `palette/surf_knit` | Palette/Surface | Surface › Modify › Knit (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: sew stitch |
| `palette/surf_thicken` | Palette/Surface | Surface › Modify › Thicken (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: solidify |
| `palette/surf_trim` | Palette/Surface | Surface › Modify › Trim (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/surf_untrim` | Palette/Surface | Surface › Modify › Untrim (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `surf_trim` |
| `palette/surf_delete_face` | Palette/Surface | Surface › Modify › Delete Face (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/surf_replace_face` | Palette/Surface | Surface › Modify › Replace Face (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `surf_delete_face` |
| `palette/sim_new_static` | Palette/Simulate | Simulate › Study › New Study (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: fea stress |
| `palette/sim_new_modal` | Palette/Simulate | Simulate › Study › Modal Study (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sim_new_static`; kw: frequency vibration |
| `palette/sim_new_flow` | Palette/Simulate | Simulate › Study › Flow Study (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sim_new_static`; kw: cfd aero |
| `palette/sim_new_thermal` | Palette/Simulate | Simulate › Study › Thermal Study (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sim_new_static`; kw: heat |
| `palette/sim_new_fatigue` | Palette/Simulate | Simulate › Study › Fatigue Study (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sim_new_static`; kw: cyclic life |
| `palette/sim_material` | Palette/Simulate | Simulate › Setup › Material (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: assign |
| `palette/sim_fixture` | Palette/Simulate | Simulate › Setup › Fixtures (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: restraint support |
| `palette/sim_load_force` | Palette/Simulate | Simulate › Setup › Loads (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: force |
| `palette/sim_load_pressure` | Palette/Simulate | Simulate › Setup › Pressure (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `sim_load_force` |
| `palette/sim_mesh` | Palette/Simulate | Simulate › Setup › Mesh (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: discretize |
| `palette/sim_run` | Palette/Simulate | Simulate › Run › Run (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: solve |
| `palette/sim_results` | Palette/Simulate | Simulate › Run › Results (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: plot |
| `palette/asm_insert` | Palette/Assemble | Assemble › Insert › Insert Component (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: add part |
| `palette/asm_parts_library` | Palette/Assemble | Assemble › Insert › Standard Parts (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `asm_insert`; kw: fastener toolbox |
| `palette/asm_move` | Palette/Assemble | Assemble › Position › Move (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: drag rotate |
| `palette/asm_mate` | Palette/Assemble | Assemble › Position › Mate (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: constrain align |
| `palette/asm_pattern` | Palette/Assemble | Assemble › Pattern › Component Pattern (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: array repeat |
| `palette/asm_interference` | Palette/Assemble | Assemble › Inspect › Interference (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: clash |
| `palette/asm_bom` | Palette/Assemble | Assemble › Inspect › BOM (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: bill materials |
| `palette/asm_explode` | Palette/Assemble | Assemble › Display › Exploded View (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_view_model` | Palette/Drafting | Drafting › Views › Model View (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_view_proj` | Palette/Drafting | Drafting › Views › Projected (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_view_std3` | Palette/Drafting | Drafting › Views › Standard 3 (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: three |
| `palette/dwg_view_section` | Palette/Drafting | Drafting › Views › Section (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: cut |
| `palette/dwg_section_half` | Palette/Drafting | Drafting › Views › Half Section (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_view_section` |
| `palette/dwg_section_reverse` | Palette/Drafting | Drafting › Views › Flip Side (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_view_section` |
| `palette/dwg_view_detail` | Palette/Drafting | Drafting › Views › Detail (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: zoom |
| `palette/dwg_smart_dim` | Palette/Drafting | Drafting › Dimension › Smart Dim (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_model_items` | Palette/Drafting | Drafting › Dimension › Model Items (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: import dims |
| `palette/dwg_hole_callout` | Palette/Drafting | Drafting › Dimension › Hole Callout (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_gdt` | Palette/Drafting | Drafting › Annotate › Geo Tol (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: gd&t tolerance |
| `palette/dwg_datum` | Palette/Drafting | Drafting › Annotate › Datum (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_gdt` |
| `palette/dwg_datum_target` | Palette/Drafting | Drafting › Annotate › Datum Target (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_gdt` |
| `palette/dwg_surface_finish` | Palette/Drafting | Drafting › Annotate › Surface Finish (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_gdt` |
| `palette/dwg_weld` | Palette/Drafting | Drafting › Annotate › Weld (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_gdt` |
| `palette/dwg_center_mark` | Palette/Drafting | Drafting › Annotate › Center Mark (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_center_line` | Palette/Drafting | Drafting › Annotate › Center Line (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_center_mark` |
| `palette/dwg_note` | Palette/Drafting | Drafting › Annotate › Note (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: text |
| `palette/dwg_balloon` | Palette/Drafting | Drafting › Annotate › Balloon (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: callout |
| `palette/dwg_auto_balloon` | Palette/Drafting | Drafting › Annotate › Auto Balloon (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_balloon` |
| `palette/dwg_title_block` | Palette/Drafting | Drafting › Tables › Title Block (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_bom_table` | Palette/Drafting | Drafting › Tables › Bill of Materials (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: bom |
| `palette/dwg_sheet_props` | Palette/Drafting | Drafting › Sheet › Sheet Props (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_rebuild` | Palette/Drafting | Drafting › Sheet › Rebuild (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_fit` | Palette/Drafting | Drafting › Sheet › Fit (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_scale_1_1` | Palette/Drafting | Drafting › Sheet › Scale 1:1 (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_scale_1_2` | Palette/Drafting | Drafting › Sheet › Scale 1:2 (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_scale_1_1` |
| `palette/dwg_scale_2_1` | Palette/Drafting | Drafting › Sheet › Scale 2:1 (Flyout) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill parent: `dwg_scale_1_1` |
| `palette/dwg_export_dxf` | Palette/Drafting | Drafting › Output › Export DXF (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/dwg_export_pdf` | Palette/Drafting | Drafting › Output › Export PDF (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/ecad_new_sch` | Palette/Schematic | Schematic › New › New Schematic (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/ecad_open` | Palette/Schematic | Schematic › New › Open (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/ecad_save` | Palette/Schematic | Schematic › New › Save (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/sch_add_symbol` | Palette/Schematic | Schematic › Place › Place Symbol (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: component part |
| `palette/sch_add_wire` | Palette/Schematic | Schematic › Place › Wire (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: net connect |
| `palette/sch_add_label` | Palette/Schematic | Schematic › Place › Net Label (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/sch_add_power` | Palette/Schematic | Schematic › Place › Power Port (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: vcc gnd |
| `palette/sch_annotate` | Palette/Schematic | Schematic › Verify › Annotate (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: reference designators |
| `palette/ecad_run_erc` | Palette/Schematic | Schematic › Verify › Run ERC (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: electrical rule check |
| `palette/ecad_ai_design` | Palette/Schematic | Schematic › AI › AI Design (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: generate circuit |
| `palette/ecad_new_pcb` | Palette/Board | Board › New › New PCB (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: layout |
| `palette/pcb_update_from_sch` | Palette/Board | Board › New › Update from Sch (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: netlist |
| `palette/pcb_add_footprint` | Palette/Board | Board › Place › Footprint (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: land pattern |
| `palette/pcb_add_track` | Palette/Board | Board › Place › Route (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: trace copper |
| `palette/pcb_add_via` | Palette/Board | Board › Place › Via (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/pcb_add_zone` | Palette/Board | Board › Place › Copper Zone (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: pour fill plane |
| `palette/ecad_autoroute` | Palette/Board | Board › Auto › Autoroute (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |
| `palette/ecad_fill_zones` | Palette/Board | Board › Auto › Fill Zones (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: pour |
| `palette/ecad_run_drc` | Palette/Board | Board › Auto › Run DRC (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: design rule check |
| `palette/ecad_simulate` | Palette/Board | Board › Analyze › Simulate (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: spice |
| `palette/ecad_to_3d` | Palette/Board | Board › Output › Board to 3D (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: model |
| `palette/ecad_export_gerber` | Palette/Board | Board › Output › Gerbers (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: fabrication |
| `palette/ecad_export_drill` | Palette/Board | Board › Output › Drill (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill kw: nc |
| `palette/ecad_export_bom` | Palette/Board | Board › Output › BOM (Top) | `⌘K` search | `[ ]` keep / `[ ]` fix / `[ ]` kill  |

---

## Property Manager (PM) Forms

Total: 44 forms, 230 fields.  Each form opens when its ribbon command is clicked.


### PM Form: Box (`box`)

*Launch:* ribbon command mapped to `box` | 4 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/box` | PM/box | PM form — Box (header/title/OK-Cancel) | Ribbon → box | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/box/length` | PM/box | PM/Box → Box → Length (Dim) | Ribbon → box | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/box/width` | PM/box | PM/Box → Box → Width (Dim) | Ribbon → box | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/box/height` | PM/box | PM/Box → Box → Height (Dim) | Ribbon → box | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/box/centered` | PM/box | PM/Box → Box → Centered (Check) | Ribbon → box | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Cylinder (`cylinder`)

*Launch:* ribbon command mapped to `cylinder` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/cylinder` | PM/cylinder | PM form — Cylinder (header/title/OK-Cancel) | Ribbon → cylinder | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/cylinder/radius` | PM/cylinder | PM/Cylinder → Cylinder → Radius (Dim) | Ribbon → cylinder | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/cylinder/height` | PM/cylinder | PM/Cylinder → Cylinder → Height (Dim) | Ribbon → cylinder | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/cylinder/centered` | PM/cylinder | PM/Cylinder → Cylinder → Centered (Check) | Ribbon → cylinder | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Extrude (`extrude`)

*Launch:* ribbon command mapped to `extrude` | 24 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/extrude` | PM/extrude | PM form — Extrude (header/title/OK-Cancel) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/profile` | PM/extrude | PM/Extrude → From → Profile (Well) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/start` | PM/extrude | PM/Extrude → From → Start Condition (ComboL) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/start_ref` | PM/extrude | PM/Extrude → From → Start Reference (WellIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/start_offset` | PM/extrude | PM/Extrude → From → Start Offset (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/mode` | PM/extrude | PM/Extrude → Direction 1 → Type (ComboL) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end` | PM/extrude | PM/Extrude → Direction 1 → End Condition (ComboL) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/depth` | PM/extrude | PM/Extrude → Direction 1 → Depth (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/up_to` | PM/extrude | PM/Extrude → Direction 1 → Up To Reference (WellIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/offset` | PM/extrude | PM/Extrude → Direction 1 → Offset (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end_ref` | PM/extrude | PM/Extrude → Direction 1 → End Reference (WellIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end_offset` | PM/extrude | PM/Extrude → Direction 1 → End Offset (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/flip` | PM/extrude | PM/Extrude → Direction 1 → Reverse direction (Check) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/draft` | PM/extrude | PM/Extrude → Direction 1 → Draft angle (AngleIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/draft_outward` | PM/extrude | PM/Extrude → Direction 1 → Draft outward (CheckIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end2` | PM/extrude | PM/Extrude → Direction 2 → End Condition (ComboL) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/up_to2` | PM/extrude | PM/Extrude → Direction 2 → Up To Reference (WellIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/offset2` | PM/extrude | PM/Extrude → Direction 2 → Offset (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end2_ref` | PM/extrude | PM/Extrude → Direction 2 → End Reference (WellIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/end2_offset` | PM/extrude | PM/Extrude → Direction 2 → End Offset (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/depth2` | PM/extrude | PM/Extrude → Direction 2 → Depth (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/thin` | PM/extrude | PM/Extrude → Thin Feature → Thin Feature (Check) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/thin_thickness` | PM/extrude | PM/Extrude → Thin Feature → Wall thickness (DimIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/thin_side` | PM/extrude | PM/Extrude → Thin Feature → Wall side (ComboLIf) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/extrude/merge` | PM/extrude | PM/Extrude → Options → Merge result (Check) | Ribbon → extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Revolve (`revolve`)

*Launch:* ribbon command mapped to `revolve` | 12 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/revolve` | PM/revolve | PM form — Revolve (header/title/OK-Cancel) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/profile` | PM/revolve | PM/Revolve → From → Profile (Well) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/axis` | PM/revolve | PM/Revolve → From → Axis (Well) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/mode` | PM/revolve | PM/Revolve → Direction 1 → Type (ComboL) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/end` | PM/revolve | PM/Revolve → Direction 1 → End Condition (ComboL) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/angle` | PM/revolve | PM/Revolve → Direction 1 → Angle (AngleIf) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/flip` | PM/revolve | PM/Revolve → Direction 1 → Reverse direction (Check) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/dir2` | PM/revolve | PM/Revolve → Direction 2 → Direction 2 (Check) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/angle2` | PM/revolve | PM/Revolve → Direction 2 → Angle (AngleIf) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/thin` | PM/revolve | PM/Revolve → Thin Feature → Thin Feature (Check) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/thin_thickness` | PM/revolve | PM/Revolve → Thin Feature → Wall thickness (DimIf) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/thin_side` | PM/revolve | PM/Revolve → Thin Feature → Wall side (ComboLIf) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/revolve/merge` | PM/revolve | PM/Revolve → Options → Merge result (Check) | Ribbon → revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Sweep (`sweep`)

*Launch:* ribbon command mapped to `sweep` | 8 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/sweep` | PM/sweep | PM form — Sweep (header/title/OK-Cancel) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/profile` | PM/sweep | PM/Sweep → Profile and Path → Profile (Well) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/path` | PM/sweep | PM/Sweep → Profile and Path → Path (Well) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/guides` | PM/sweep | PM/Sweep → Guide Curves → Guide Curves (Well) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/orientation` | PM/sweep | PM/Sweep → Options → Profile Orientation (ComboL) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/twist_turns` | PM/sweep | PM/Sweep → Options → Twist (turns) (DimU) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/twist_degrees` | PM/sweep | PM/Sweep → Options → Twist (degrees) (Angle) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/mode` | PM/sweep | PM/Sweep → Options → Type (ComboL) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sweep/merge` | PM/sweep | PM/Sweep → Options → Merge result (Check) | Ribbon → sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Loft (`loft`)

*Launch:* ribbon command mapped to `loft` | 8 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/loft` | PM/loft | PM form — Loft (header/title/OK-Cancel) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/sections` | PM/loft | PM/Loft → Profiles → Profiles (Well) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/centerline` | PM/loft | PM/Loft → Centerline → Centerline (Well) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/guides` | PM/loft | PM/Loft → Guide Curves → Guide Curves (Well) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/tangency_start` | PM/loft | PM/Loft → Tangency → Start Constraint (ComboL) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/tangency_end` | PM/loft | PM/Loft → Tangency → End Constraint (ComboL) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/mode` | PM/loft | PM/Loft → Options → Type (ComboL) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/ruled` | PM/loft | PM/Loft → Options → Ruled (straight transitions) (Check) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/loft/merge` | PM/loft | PM/Loft → Options → Merge result (Check) | Ribbon → loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Fillet (`fillet`)

*Launch:* ribbon command mapped to `fillet` | 8 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/fillet` | PM/fillet | PM form — Fillet (header/title/OK-Cancel) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/edges` | PM/fillet | PM/Fillet → Items to Fillet → Edges (WellIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/middle_face` | PM/fillet | PM/Fillet → Items to Fillet → Middle Face (WellIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/face_set_a` | PM/fillet | PM/Fillet → Items to Fillet → Face Set 1 (WellIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/face_set_b` | PM/fillet | PM/Fillet → Items to Fillet → Face Set 2 (WellIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/type` | PM/fillet | PM/Fillet → Fillet Parameters → Fillet Type (ComboL) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/radius` | PM/fillet | PM/Fillet → Fillet Parameters → Radius (DimIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/radius2` | PM/fillet | PM/Fillet → Fillet Parameters → End Radius (DimIf) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/fillet/tangent_propagation` | PM/fillet | PM/Fillet → Fillet Parameters → Tangent propagation (Check) | Ribbon → fillet | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Chamfer (`chamfer`)

*Launch:* ribbon command mapped to `chamfer` | 7 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/chamfer` | PM/chamfer | PM form — Chamfer (header/title/OK-Cancel) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/edges` | PM/chamfer | PM/Chamfer → Items to Chamfer → Edges (WellIf) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/vertices` | PM/chamfer | PM/Chamfer → Items to Chamfer → Vertices (WellIf) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/type` | PM/chamfer | PM/Chamfer → Chamfer Parameters → Chamfer Type (ComboL) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/length` | PM/chamfer | PM/Chamfer → Chamfer Parameters → Distance (Dim) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/length2` | PM/chamfer | PM/Chamfer → Chamfer Parameters → Distance 2 (DimIf) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/angle` | PM/chamfer | PM/Chamfer → Chamfer Parameters → Angle (AngleIf) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/chamfer/reference_face` | PM/chamfer | PM/Chamfer → Chamfer Parameters → Reference Face (WellIf) | Ribbon → chamfer | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Shell (`shell`)

*Launch:* ribbon command mapped to `shell` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/shell` | PM/shell | PM form — Shell (header/title/OK-Cancel) | Ribbon → shell | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/shell/remove` | PM/shell | PM/Shell → Parameters → Faces to Remove (Well) | Ribbon → shell | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/shell/thickness` | PM/shell | PM/Shell → Parameters → Thickness (Dim) | Ribbon → shell | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/shell/outward` | PM/shell | PM/Shell → Parameters → Shell outward (Check) | Ribbon → shell | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Hole Wizard (`hole`)

*Launch:* ribbon command mapped to `hole` | 12 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/hole` | PM/hole | PM form — Hole Wizard (header/title/OK-Cancel) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/face` | PM/hole | PM/Hole Wizard → Hole Position → Face (Well) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/style` | PM/hole | PM/Hole Wizard → Hole Type → Type (ComboL) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/standard` | PM/hole | PM/Hole Wizard → Hole Type → Size (ComboL) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/diameter` | PM/hole | PM/Hole Wizard → Hole Type → Diameter (DimIf) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/csink_angle` | PM/hole | PM/Hole Wizard → Hole Type → C'Sink Angle (AngleIf) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/end` | PM/hole | PM/Hole Wizard → End Condition → End Condition (ComboL) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/depth` | PM/hole | PM/Hole Wizard → End Condition → Depth (DimIf) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/positions` | PM/hole | PM/Hole Wizard → Positions → Position Vertices (Well) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/nx` | PM/hole | PM/Hole Wizard → Positions → Count X (Int) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/ny` | PM/hole | PM/Hole Wizard → Positions → Count Y (Int) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/dx` | PM/hole | PM/Hole Wizard → Positions → Pitch X (Dim) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/hole/dy` | PM/hole | PM/Hole Wizard → Positions → Pitch Y (Dim) | Ribbon → hole | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Mirror (`mirror`)

*Launch:* ribbon command mapped to `mirror` | 5 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/mirror` | PM/mirror | PM form — Mirror (header/title/OK-Cancel) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/mirror/plane` | PM/mirror | PM/Mirror → Mirror Plane → Mirror Face/Plane (DatumRef) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/mirror/plane` | PM/mirror | PM/Mirror → Mirror Plane → …or click a face/plane (Well) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/mirror/features` | PM/mirror | PM/Mirror → Features to Mirror → Features to Mirror (FeatWell) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/mirror/bodies` | PM/mirror | PM/Mirror → Bodies to Mirror → Bodies to Mirror (Well) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/mirror/merge` | PM/mirror | PM/Mirror → Options → Merge result (Check) | Ribbon → mirror | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Linear Pattern (`linear_pattern`)

*Launch:* ribbon command mapped to `linear_pattern` | 10 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/linear_pattern` | PM/linear_pattern | PM form — Linear Pattern (header/title/OK-Cancel) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/dir1` | PM/linear_pattern | PM/Linear Pattern → Direction 1 → Direction 1 (DatumRef) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/dir1` | PM/linear_pattern | PM/Linear Pattern → Direction 1 → …or pick an edge (Well) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/count1` | PM/linear_pattern | PM/Linear Pattern → Direction 1 → Instances (Int) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/spacing1` | PM/linear_pattern | PM/Linear Pattern → Direction 1 → Spacing (Dim) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/dir2` | PM/linear_pattern | PM/Linear Pattern → Direction 2 → Direction 2 (DatumRef) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/dir2` | PM/linear_pattern | PM/Linear Pattern → Direction 2 → …or pick an edge (Well) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/count2` | PM/linear_pattern | PM/Linear Pattern → Direction 2 → Instances (Int) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/spacing2` | PM/linear_pattern | PM/Linear Pattern → Direction 2 → Spacing (Dim) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/features` | PM/linear_pattern | PM/Linear Pattern → Features to Pattern → Features to Pattern (FeatWell) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/linear_pattern/skip` | PM/linear_pattern | PM/Linear Pattern → Instances to Skip → Instances to skip (Text) | Ribbon → linear_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Circular Pattern (`circular_pattern`)

*Launch:* ribbon command mapped to `circular_pattern` | 7 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/circular_pattern` | PM/circular_pattern | PM form — Circular Pattern (header/title/OK-Cancel) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/axis` | PM/circular_pattern | PM/Circular Pattern → Parameters → Pattern Axis (DatumRef) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/axis` | PM/circular_pattern | PM/Circular Pattern → Parameters → …or pick edge/cyl face/datum axis (Well) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/count` | PM/circular_pattern | PM/Circular Pattern → Parameters → Instances (Int) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/angle` | PM/circular_pattern | PM/Circular Pattern → Parameters → Angle (Angle) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/equal_spacing` | PM/circular_pattern | PM/Circular Pattern → Parameters → Equal spacing (Check) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/features` | PM/circular_pattern | PM/Circular Pattern → Features to Pattern → Features to Pattern (FeatWell) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/circular_pattern/skip` | PM/circular_pattern | PM/Circular Pattern → Instances to Skip → Instances to skip (Text) | Ribbon → circular_pattern | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Draft (`draft`)

*Launch:* ribbon command mapped to `draft` | 6 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/draft` | PM/draft | PM form — Draft (header/title/OK-Cancel) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/mode` | PM/draft | PM/Draft → Draft → Draft Type (ComboL) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/angle` | PM/draft | PM/Draft → Draft → Draft Angle (Angle) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/neutral` | PM/draft | PM/Draft → Draft → Neutral Plane (DatumRef) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/neutral` | PM/draft | PM/Draft → Draft → …or click a face/plane (Well) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/faces` | PM/draft | PM/Draft → Draft → Faces to Draft (Well) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/draft/parting` | PM/draft | PM/Draft → Draft → Parting Line (edges) (WellIf) | Ribbon → draft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Helix / Spiral (`helix`)

*Launch:* ribbon command mapped to `helix` | 8 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/helix` | PM/helix | PM form — Helix / Spiral (header/title/OK-Cancel) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/base` | PM/helix | PM/Helix / Spiral → Helix → Axis Plane (DatumRef) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/base` | PM/helix | PM/Helix / Spiral → Helix → …or click a face/plane (Well) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/radius` | PM/helix | PM/Helix / Spiral → Helix → Radius (Dim) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/pitch` | PM/helix | PM/Helix / Spiral → Helix → Pitch (Dim) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/height` | PM/helix | PM/Helix / Spiral → Helix → Height (Dim) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/taper` | PM/helix | PM/Helix / Spiral → Helix → Taper Angle (Angle) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/start_angle` | PM/helix | PM/Helix / Spiral → Helix → Start Angle (Angle) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/helix/ccw` | PM/helix | PM/Helix / Spiral → Helix → Counterclockwise (Check) | Ribbon → helix | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Plane (`plane`)

*Launch:* ribbon command mapped to `plane` | 8 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/plane` | PM/plane | PM form — Plane (header/title/OK-Cancel) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/method` | PM/plane | PM/Plane → First Reference → Type (ComboL) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/base` | PM/plane | PM/Plane → First Reference → Reference (WellIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/offset` | PM/plane | PM/Plane → Parameters → Offset Distance (DimIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/angle` | PM/plane | PM/Plane → Parameters → Angle (AngleIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/second` | PM/plane | PM/Plane → Second Reference → Hinge (edge/axis) (WellIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/second` | PM/plane | PM/Plane → Second Reference → Second Plane (WellIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/points` | PM/plane | PM/Plane → Points → Three Vertices (WellIf) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/plane/flip` | PM/plane | PM/Plane → Parameters → Flip (Check) | Ribbon → plane | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Axis (`axis`)

*Launch:* ribbon command mapped to `axis` | 5 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/axis` | PM/axis | PM form — Axis (header/title/OK-Cancel) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/axis/method` | PM/axis | PM/Axis → Reference → Type (ComboL) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/axis/a` | PM/axis | PM/Axis → Reference → Reference 1 (WellIf) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/axis/a` | PM/axis | PM/Axis → Reference → Plane 1 (WellIf) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/axis/b` | PM/axis | PM/Axis → Reference → Reference 2 (WellIf) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/axis/b` | PM/axis | PM/Axis → Reference → Plane 2 (WellIf) | Ribbon → axis | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Point (`point`)

*Launch:* ribbon command mapped to `point` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/point` | PM/point | PM form — Point (header/title/OK-Cancel) | Ribbon → point | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/point/method` | PM/point | PM/Point → Reference → Type (ComboL) | Ribbon → point | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/point/target` | PM/point | PM/Point → Reference → Reference (Well) | Ribbon → point | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Coordinate System (`csys`)

*Launch:* ribbon command mapped to `csys` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/csys` | PM/csys | PM form — Coordinate System (header/title/OK-Cancel) | Ribbon → csys | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/csys/origin` | PM/csys | PM/Coordinate System → Coordinate System → Origin (Well) | Ribbon → csys | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/csys/x_dir` | PM/csys | PM/Coordinate System → Coordinate System → X Axis (edge/axis) (Well) | Ribbon → csys | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/csys/y_dir` | PM/csys | PM/Coordinate System → Coordinate System → Y Axis (edge/axis) (Well) | Ribbon → csys | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Combine (`boolean`)

*Launch:* ribbon command mapped to `boolean` | 4 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/boolean` | PM/boolean | PM form — Combine (header/title/OK-Cancel) | Ribbon → boolean | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/boolean/op` | PM/boolean | PM/Combine → Operation → Operation (ComboL) | Ribbon → boolean | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/boolean/targets` | PM/boolean | PM/Combine → Bodies → Main / Target Bodies (Well) | Ribbon → boolean | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/boolean/tools` | PM/boolean | PM/Combine → Bodies → Tool Bodies (Well) | Ribbon → boolean | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/boolean/keep_tools` | PM/boolean | PM/Combine → Options → Keep tool bodies (Check) | Ribbon → boolean | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Base Flange (`sm_base_flange`)

*Launch:* ribbon command mapped to `sm_base_flange` | 4 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/sm_base_flange` | PM/sm_base_flange | PM form — Base Flange (header/title/OK-Cancel) | Ribbon → sm_base_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_base_flange/profile` | PM/sm_base_flange | PM/Base Flange → Profile → Profile (Well) | Ribbon → sm_base_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_base_flange/thickness` | PM/sm_base_flange | PM/Base Flange → Sheet Metal Gauges → Thickness (Dim) | Ribbon → sm_base_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_base_flange/k_factor` | PM/sm_base_flange | PM/Base Flange → Sheet Metal Gauges → K-Factor (DimU) | Ribbon → sm_base_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_base_flange/default_bend_radius` | PM/sm_base_flange | PM/Base Flange → Sheet Metal Gauges → Bend Radius (Dim) | Ribbon → sm_base_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Edge Flange (`sm_edge_flange`)

*Launch:* ribbon command mapped to `sm_edge_flange` | 6 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/sm_edge_flange` | PM/sm_edge_flange | PM form — Edge Flange (header/title/OK-Cancel) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/edge` | PM/sm_edge_flange | PM/Edge Flange → Flange → Bend Edge (Well) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/angle` | PM/sm_edge_flange | PM/Edge Flange → Flange → Angle (Angle) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/length` | PM/sm_edge_flange | PM/Edge Flange → Flange → Length (Dim) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/radius` | PM/sm_edge_flange | PM/Edge Flange → Flange → Bend Radius (Dim) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/position` | PM/sm_edge_flange | PM/Edge Flange → Flange → Flange Position (ComboL) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_edge_flange/flip` | PM/sm_edge_flange | PM/Edge Flange → Flange → Reverse direction (Check) | Ribbon → sm_edge_flange | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Hem (`sm_hem`)

*Launch:* ribbon command mapped to `sm_hem` | 5 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/sm_hem` | PM/sm_hem | PM form — Hem (header/title/OK-Cancel) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_hem/edge` | PM/sm_hem | PM/Hem → Hem → Bend Edge (Well) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_hem/type` | PM/sm_hem | PM/Hem → Hem → Hem Type (ComboL) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_hem/length` | PM/sm_hem | PM/Hem → Hem → Length (Dim) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_hem/radius` | PM/sm_hem | PM/Hem → Hem → Bend Radius (Dim) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_hem/flip` | PM/sm_hem | PM/Hem → Hem → Reverse direction (Check) | Ribbon → sm_hem | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Jog (`sm_jog`)

*Launch:* ribbon command mapped to `sm_jog` | 7 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/sm_jog` | PM/sm_jog | PM form — Jog (header/title/OK-Cancel) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/edge` | PM/sm_jog | PM/Jog → Jog → Bend Edge (Well) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/angle` | PM/sm_jog | PM/Jog → Jog → Angle (Angle) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/offset` | PM/sm_jog | PM/Jog → Jog → Offset (Dim) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/length` | PM/sm_jog | PM/Jog → Jog → Length (Dim) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/radius` | PM/sm_jog | PM/Jog → Jog → Bend Radius (Dim) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/position` | PM/sm_jog | PM/Jog → Jog → Flange Position (ComboL) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/sm_jog/flip` | PM/sm_jog | PM/Jog → Jog → Reverse direction (Check) | Ribbon → sm_jog | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Structural Member (`weld_member`)

*Launch:* ribbon command mapped to `weld_member` | 11 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/weld_member` | PM/weld_member | PM form — Structural Member (header/title/OK-Cancel) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/sketch` | PM/weld_member | PM/Structural Member → Selections → Path Sketch (FeatWell1) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/profile` | PM/weld_member | PM/Structural Member → Profile → Profile (ComboL) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/size` | PM/weld_member | PM/Structural Member → Profile → Size (DimIf) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/w` | PM/weld_member | PM/Structural Member → Profile → Width (DimIf) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/h` | PM/weld_member | PM/Structural Member → Profile → Height (DimIf) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/od` | PM/weld_member | PM/Structural Member → Profile → Outer Dia. (DimIf) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/leg` | PM/weld_member | PM/Structural Member → Profile → Leg (DimIf) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/thickness` | PM/weld_member | PM/Structural Member → Profile → Thickness (Dim) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/angle` | PM/weld_member | PM/Structural Member → Settings → Rotation (Angle) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/justify` | PM/weld_member | PM/Structural Member → Settings → Justify (ComboL) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_member/trim_corners` | PM/weld_member | PM/Structural Member → Settings → Trim corners (Check) | Ribbon → weld_member | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Gusset (`weld_gusset`)

*Launch:* ribbon command mapped to `weld_gusset` | 7 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/weld_gusset` | PM/weld_gusset | PM form — Gusset (header/title/OK-Cancel) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/faces` | PM/weld_gusset | PM/Gusset → Selections → Supporting Faces (Well) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/thickness` | PM/weld_gusset | PM/Gusset → Gusset → Thickness (Dim) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/d1` | PM/weld_gusset | PM/Gusset → Gusset → Leg 1 (d1) (Dim) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/d2` | PM/weld_gusset | PM/Gusset → Gusset → Leg 2 (d2) (Dim) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/type` | PM/weld_gusset | PM/Gusset → Gusset → Profile (ComboL) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/location` | PM/weld_gusset | PM/Gusset → Settings → Location (ComboL) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_gusset/offset` | PM/weld_gusset | PM/Gusset → Settings → Offset (Dim) | Ribbon → weld_gusset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: End Cap (`weld_end_cap`)

*Launch:* ribbon command mapped to `weld_end_cap` | 5 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/weld_end_cap` | PM/weld_end_cap | PM form — End Cap (header/title/OK-Cancel) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_end_cap/face` | PM/weld_end_cap | PM/End Cap → Selections → End Face (Well) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_end_cap/thickness` | PM/weld_end_cap | PM/End Cap → End Cap → Thickness (Dim) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_end_cap/type` | PM/weld_end_cap | PM/End Cap → End Cap → Type (ComboL) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_end_cap/overhang` | PM/weld_end_cap | PM/End Cap → End Cap → Overhang (DimU) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/weld_end_cap/offset` | PM/weld_end_cap | PM/End Cap → End Cap → Offset (Dim) | Ribbon → weld_end_cap | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Rib (`rib`)

*Launch:* ribbon command mapped to `rib` | 5 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/rib` | PM/rib | PM form — Rib (header/title/OK-Cancel) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/rib/profile` | PM/rib | PM/Rib → Profile → Profile (open contour) (Well) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/rib/thickness` | PM/rib | PM/Rib → Rib → Thickness (Dim) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/rib/side` | PM/rib | PM/Rib → Rib → Thickness Side (ComboL) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/rib/draft` | PM/rib | PM/Rib → Rib → Draft Angle (Angle) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/rib/flip` | PM/rib | PM/Rib → Rib → Reverse direction (Check) | Ribbon → rib | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Split (`split`)

*Launch:* ribbon command mapped to `split` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/split` | PM/split | PM form — Split (header/title/OK-Cancel) | Ribbon → split | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/split/tool` | PM/split | PM/Split → Trim/Split Tool → Tool Plane (DatumRef) | Ribbon → split | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/split/tool` | PM/split | PM/Split → Trim/Split Tool → …or pick a planar face (Well) | Ribbon → split | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/split/keep` | PM/split | PM/Split → Resulting Bodies → Resulting Bodies (ComboL) | Ribbon → split | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Extruded Surface (`surf_extrude`)

*Launch:* ribbon command mapped to `surf_extrude` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_extrude` | PM/surf_extrude | PM form — Extruded Surface (header/title/OK-Cancel) | Ribbon → surf_extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_extrude/profile` | PM/surf_extrude | PM/Extruded Surface → Profile → Profile (Well) | Ribbon → surf_extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_extrude/end` | PM/surf_extrude | PM/Extruded Surface → Direction → End Condition (ComboL) | Ribbon → surf_extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_extrude/depth` | PM/surf_extrude | PM/Extruded Surface → Direction → Distance (DimIf) | Ribbon → surf_extrude | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Revolved Surface (`surf_revolve`)

*Launch:* ribbon command mapped to `surf_revolve` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_revolve` | PM/surf_revolve | PM form — Revolved Surface (header/title/OK-Cancel) | Ribbon → surf_revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_revolve/profile` | PM/surf_revolve | PM/Revolved Surface → Revolve → Profile (Well) | Ribbon → surf_revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_revolve/axis` | PM/surf_revolve | PM/Revolved Surface → Revolve → Axis (Well) | Ribbon → surf_revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_revolve/angle` | PM/surf_revolve | PM/Revolved Surface → Revolve → Angle (Angle) | Ribbon → surf_revolve | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Swept Surface (`surf_sweep`)

*Launch:* ribbon command mapped to `surf_sweep` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_sweep` | PM/surf_sweep | PM form — Swept Surface (header/title/OK-Cancel) | Ribbon → surf_sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_sweep/profile` | PM/surf_sweep | PM/Swept Surface → Profile and Path → Profile (Well) | Ribbon → surf_sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_sweep/path` | PM/surf_sweep | PM/Swept Surface → Profile and Path → Path (Well) | Ribbon → surf_sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_sweep/guides` | PM/surf_sweep | PM/Swept Surface → Guide Curves → Guide Curves (Well) | Ribbon → surf_sweep | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Lofted Surface (`surf_loft`)

*Launch:* ribbon command mapped to `surf_loft` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_loft` | PM/surf_loft | PM form — Lofted Surface (header/title/OK-Cancel) | Ribbon → surf_loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_loft/sections` | PM/surf_loft | PM/Lofted Surface → Profiles → Profiles (Well) | Ribbon → surf_loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_loft/guides` | PM/surf_loft | PM/Lofted Surface → Guide Curves → Guide Curves (Well) | Ribbon → surf_loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_loft/centerline` | PM/surf_loft | PM/Lofted Surface → Centerline → Centerline (Well) | Ribbon → surf_loft | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Planar Surface (`surf_planar`)

*Launch:* ribbon command mapped to `surf_planar` | 1 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_planar` | PM/surf_planar | PM form — Planar Surface (header/title/OK-Cancel) | Ribbon → surf_planar | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_planar/profile` | PM/surf_planar | PM/Planar Surface → Planar Surface → Bounding Entities (Well) | Ribbon → surf_planar | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Offset Surface (`surf_offset`)

*Launch:* ribbon command mapped to `surf_offset` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_offset` | PM/surf_offset | PM form — Offset Surface (header/title/OK-Cancel) | Ribbon → surf_offset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_offset/faces` | PM/surf_offset | PM/Offset Surface → Offset Parameters → Faces to Offset (Well) | Ribbon → surf_offset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_offset/distance` | PM/surf_offset | PM/Offset Surface → Offset Parameters → Offset Distance (Dim) | Ribbon → surf_offset | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Ruled Surface (`surf_ruled`)

*Launch:* ribbon command mapped to `surf_ruled` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_ruled` | PM/surf_ruled | PM form — Ruled Surface (header/title/OK-Cancel) | Ribbon → surf_ruled | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_ruled/edge_a` | PM/surf_ruled | PM/Ruled Surface → Edge Selection → Edge 1 (Well) | Ribbon → surf_ruled | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_ruled/edge_b` | PM/surf_ruled | PM/Ruled Surface → Edge Selection → Edge 2 (Well) | Ribbon → surf_ruled | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Boundary Surface (`surf_boundary`)

*Launch:* ribbon command mapped to `surf_boundary` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_boundary` | PM/surf_boundary | PM form — Boundary Surface (header/title/OK-Cancel) | Ribbon → surf_boundary | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_boundary/dir1` | PM/surf_boundary | PM/Boundary Surface → Direction 1 → Direction 1 Curves (Well) | Ribbon → surf_boundary | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_boundary/dir2` | PM/surf_boundary | PM/Boundary Surface → Direction 2 → Direction 2 Curves (Well) | Ribbon → surf_boundary | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Filled Surface (`surf_fill`)

*Launch:* ribbon command mapped to `surf_fill` | 1 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_fill` | PM/surf_fill | PM form — Filled Surface (header/title/OK-Cancel) | Ribbon → surf_fill | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_fill/edges` | PM/surf_fill | PM/Filled Surface → Fill Boundary → Patch Boundary (Well) | Ribbon → surf_fill | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Knit Surface (`surf_knit`)

*Launch:* ribbon command mapped to `surf_knit` | 1 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_knit` | PM/surf_knit | PM form — Knit Surface (header/title/OK-Cancel) | Ribbon → surf_knit | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_knit/surfaces` | PM/surf_knit | PM/Knit Surface → Selections → Surfaces to Knit (Well) | Ribbon → surf_knit | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Thicken (`surf_thicken`)

*Launch:* ribbon command mapped to `surf_thicken` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_thicken` | PM/surf_thicken | PM form — Thicken (header/title/OK-Cancel) | Ribbon → surf_thicken | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_thicken/surface` | PM/surf_thicken | PM/Thicken → Thicken Parameters → Surface (Well) | Ribbon → surf_thicken | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_thicken/thickness` | PM/surf_thicken | PM/Thicken → Thicken Parameters → Thickness (Dim) | Ribbon → surf_thicken | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Trim Surface (`surf_trim`)

*Launch:* ribbon command mapped to `surf_trim` | 3 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_trim` | PM/surf_trim | PM form — Trim Surface (header/title/OK-Cancel) | Ribbon → surf_trim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_trim/surface` | PM/surf_trim | PM/Trim Surface → Trim → Surface to Trim (Well) | Ribbon → surf_trim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_trim/tool` | PM/surf_trim | PM/Trim Surface → Trim → Trim Tool (Well) | Ribbon → surf_trim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_trim/keep` | PM/surf_trim | PM/Trim Surface → Trim → Keep Selection (ComboL) | Ribbon → surf_trim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Untrim Surface (`surf_untrim`)

*Launch:* ribbon command mapped to `surf_untrim` | 1 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_untrim` | PM/surf_untrim | PM form — Untrim Surface (header/title/OK-Cancel) | Ribbon → surf_untrim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_untrim/faces` | PM/surf_untrim | PM/Untrim Surface → Untrim → Faces to Untrim (Well) | Ribbon → surf_untrim | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Delete Face (`surf_delete_face`)

*Launch:* ribbon command mapped to `surf_delete_face` | 1 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_delete_face` | PM/surf_delete_face | PM form — Delete Face (header/title/OK-Cancel) | Ribbon → surf_delete_face | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_delete_face/faces` | PM/surf_delete_face | PM/Delete Face → Selections → Faces to Delete (Well) | Ribbon → surf_delete_face | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

### PM Form: Replace Face (`surf_replace_face`)

*Launch:* ribbon command mapped to `surf_replace_face` | 2 fields

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `pm/surf_replace_face` | PM/surf_replace_face | PM form — Replace Face (header/title/OK-Cancel) | Ribbon → surf_replace_face | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_replace_face/faces` | PM/surf_replace_face | PM/Replace Face → Replace Face → Faces to Replace (Well) | Ribbon → surf_replace_face | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `pm/surf_replace_face/surface` | PM/surf_replace_face | PM/Replace Face → Replace Face → Replacement Surface (Well) | Ribbon → surf_replace_face | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

---

## Settings — Appearance Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/appearance/theme` | Settings/Appearance | Settings → Appearance → Theme | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/light_theme` | Settings/Appearance | Settings → Appearance → Light theme | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/dark_theme` | Settings/Appearance | Settings → Appearance → Dark theme | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/liquid_glass` | Settings/Appearance | Settings → Appearance → Liquid glass | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/glass_tint` | Settings/Appearance | Settings → Appearance → Glass tint | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/tint_strength` | Settings/Appearance | Settings → Appearance → Tint strength | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/glass_level` | Settings/Appearance | Settings → Appearance → Glass level | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/glass_opacity` | Settings/Appearance | Settings → Appearance → Glass opacity | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/corner_rounding` | Settings/Appearance | Settings → Appearance → Corner rounding | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/text_contrast` | Settings/Appearance | Settings → Appearance → Text contrast | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/appearance/dynamic_color` | Settings/Appearance | Settings → Appearance → Dynamic color | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

## Settings — General Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/general/units` | Settings/General | Settings → General → Units | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/mmgs_units` | Settings/General | Settings → General → MMGS units | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/ips_units` | Settings/General | Settings → General → IPS units | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/workspace_folder` | Settings/General | Settings → General → Workspace folder | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/engine_routing` | Settings/General | Settings → General → Engine routing | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/c++_core` | Settings/General | Settings → General → C++ core | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/performance_profiler` | Settings/General | Settings → General → Performance profiler | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/general/profiler` | Settings/General | Settings → General → Profiler | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

## Settings — AI Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/ai/ai_provider` | Settings/AI | Settings → AI → AI provider | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/model` | Settings/AI | Settings → AI → Model | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/api_key` | Settings/AI | Settings → AI → API key | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/gemini_key` | Settings/AI | Settings → AI → Gemini key | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/anthropic_key` | Settings/AI | Settings → AI → Anthropic key | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/openai_key` | Settings/AI | Settings → AI → OpenAI key | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/cloud_api_key` | Settings/AI | Settings → AI → Cloud API key | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/vision` | Settings/AI | Settings → AI → Vision | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/offline_mode` | Settings/AI | Settings → AI → Offline mode | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/reasoning_effort` | Settings/AI | Settings → AI → Reasoning effort | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/max_response_tokens` | Settings/AI | Settings → AI → Max response tokens | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/model_context_window` | Settings/AI | Settings → AI → Model context window | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/local_endpoint` | Settings/AI | Settings → AI → Local endpoint | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/manage_models` | Settings/AI | Settings → AI → Manage models | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/ai/hidden_models` | Settings/AI | Settings → AI → Hidden models | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

## Settings — Prompts Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/prompts/role_prompts` | Settings/Prompts | Settings → Prompts → Role prompts | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/prompts/agent_prompts` | Settings/Prompts | Settings → Prompts → Agent prompts | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/prompts/prompt_editor` | Settings/Prompts | Settings → Prompts → Prompt editor | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/prompts/system_prompt` | Settings/Prompts | Settings → Prompts → System prompt | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/prompts/system_prompt_override` | Settings/Prompts | Settings → Prompts → System prompt override | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

## Settings — Agent Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/agent/plan_before_building` | Settings/Agent | Settings → Agent → Plan before building | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/agent/plan_mode` | Settings/Agent | Settings → Agent → Plan mode | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/agent/refine-pass_cap` | Settings/Agent | Settings → Agent → Refine-pass cap | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/agent/max_iters` | Settings/Agent | Settings → Agent → Max iters | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/agent/iteration_budget` | Settings/Agent | Settings → Agent → Iteration budget | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

## Settings — Experimental Tab

Launch: ⌘K → 'Settings' or via top-menu gear icon

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `settings/experimental/vision_review_model` | Settings/Experimental | Settings → Experimental → Vision review model | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/experimental/model_routing` | Settings/Experimental | Settings → Experimental → Model routing | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/experimental/seek-cad_split` | Settings/Experimental | Settings → Experimental → Seek-CAD split | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/experimental/parametric_sequence_mode` | Settings/Experimental | Settings → Experimental → Parametric sequence mode | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/experimental/cad_sequence_mode` | Settings/Experimental | Settings → Experimental → CAD sequence mode | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `settings/experimental/feature_sequence` | Settings/Experimental | Settings → Experimental → Feature sequence | ⌘K 'Settings' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

---

## Viewport & App Chrome

Elements visible in the 3D viewport and surrounding chrome.

| id | area | location | launch | keep/fix/kill | notes |
|----|------|----------|--------|---------------|-------|
| `chrome/title_bar` | Title Bar | App title bar (window drag, OS controls) | Any scenario | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/status_bar` | Status Bar | Bottom status bar (messages, units, progress) | Any scenario | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/ribbon_bar` | Ribbon Bar | Tab strip + command groups at top | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/fm_panel` | Feature Manager | Left column: Feature tree / PropertyManager / Mate panel | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/copilot_panel` | Copilot Panel | Right column: AI chat, composer, timeline | activity | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/doc_tabs` | Doc Tabs | Doc-tab strip (part tabs, '+' new, 'x' close) | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/command_palette` | Command Palette | ⌘K overlay — fuzzy search all commands + settings | Any (press ⌘K) | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/viewport_triad` | Viewport Triad | XYZ orientation triad in viewport corner | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/view_cube` | View Cube | ViewCube / orientation cube (if present) | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/context_toolbar` | Context Toolbar | Floating toolbar above viewport (zoom, pan, orbit, section) | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/hud_pill` | HUD Pill | Model info pill (material, bounding box dims) in viewport HUD | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/section_tool` | Section Tool | Cross-section plane widget in viewport | m1 | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/sketch_overlay` | Sketch Overlay | Sketch entities + constraint overlay (inside sketch mode) | sketch | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/part_appearance` | Part Appearance | Per-body swatch/glass colour overlay on viewport | appearance | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/drawing_sheet` | Drawing Sheet | Drawing sheet canvas (views, borders, title block) | drawing | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/cam_panel` | CAM Panel | CAM job tree + toolpath preview | cam | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/sim_panel` | Simulation Panel | FEA study tree + results overlay | fea | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/equations_dialog` | Equations Dialog | Equations / global variables dialog | Live app — ⌘K 'Equations' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/cutlist_panel` | Cut List Panel | Weldment cut list (structural members) | Live app — weld_member then cut list | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/config_panel` | Configurations | Configuration manager panel | Live app — ⌘K 'Configurations' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |
| `chrome/design_table` | Design Table | Design table (Excel-like config editor) | Live app — ⌘K 'Design Table' | `[ ]` keep / `[ ]` fix / `[ ]` kill | |

---

## Inventory Summary

| Category | Count |
|----------|-------|
| Ribbon commands (all tabs) | 225 |
| Palette commands (deduplicated) | 193 |
| PM forms | 44 |
| PM fields (total across all forms) | 230 |
| Settings options | 50 |
| App chrome / viewport elements | 21 |
| T-G2 NEEDS-LIVE seed items | 4 |

*Generated from source by `scripts/ui_inventory.py`. Re-run after code changes to refresh.*
