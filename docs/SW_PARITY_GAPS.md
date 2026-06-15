# SolidWorks Parity — Prioritized Implementation Backlog (SW_PARITY_GAPS)

## Update 2026-06-14 — engine parity push (commits f725fe7..6128e9b)

660-test green suite. Changes to this backlog:
- **P0 items removed (engine done, graduating to host-UI backlog):** Extrude end conditions (up-to-surface/body/vertex engine done); sketch tangent (closed P0 → done); Sheet Metal Cut→flat propagation (done); Corner Relief (done); Weldment butt/cope trim (partial).
- **P0 items annotated ENGINE DONE — host UI pending:** Surfacing foundation, Extruded/Revolved/Swept/Lofted surfaces, Knit, Thicken (engine compilers registered + tested; host PM/ribbon forms remain open). Radial/diameter drawing dims (engine done; host rendering pending).
- **P1 items annotated ENGINE DONE — host UI pending:** Revolve full feature set; Sweep guides/twist; Loft guides/tangency; Fillet face/conic/tangent-prop; Chamfer angle×distance/vertex; Shell multi-thickness; Draft parting-line/step; Rib explicit drop dir; Hole ANSI/cosmetic/sketch-pts; Pattern skip/vary; Circular skip/bidirectional; Mirror bodies+merge; Ref plane/axis/point extra methods; Helix var-pitch/spiral; Non-planar split; Angular dim + center marks/lines; I/W-beam profiles + cope trim (weldments).
- **New partial rows (engine done, host pending):** Fillet—Face Fillet (was missing→partial), Tangent Propagation (was missing→partial), Pattern—instance skip (was missing→partial), Extrude From-condition (was missing→partial).
- **Surfacing P0/P1 items (all engine done — 15 compilers registered):** moved to host-UI backlog section below.

Overall backlog after this push: partial+missing went from 271 → **268** (26 rows removed or reclassified; 4 new partial rows added from missing; net reduction reflects 2 done + 7 partial-that-were-missing resolved to done/partial with real engine evidence).

**Backlog totals (partial + missing only) — post-push**

| Priority | Total | partial | missing |
|----------|------:|--------:|--------:|
| **P0** | 26 | 17 | 9 |
| **P1** | 101 | 55 | 46 |
| **P2** | 96 | 32 | 64 |
| **P3** | 45 | 5 | 40 |
| **TOTAL** | **268** | **109** | **159** |

## Update 2026-06-14 (host-exposure push) — commits 7f55296..916becd

The "ENGINE DONE — host UI pending" rows above are now **host-done** (PM forms + ribbon wired,
each visually verified via the `AI_CAD_SHOT=pmform`/`asm` headless harness). Closed this session:

- **P0 closed (host done):** Extrude end conditions (up-to-surface/body/vertex + offset + thin; new
  `refs.up_to` slot); all Surfacing host forms + a new **Surfaces ribbon tab** (15 `surf_*` commands);
  **File > Export wired** (STEP/IGES/STL/3MF + flat DXF — `export_file` RPC); **plain STEP/IGES/STL/3MF
  import** on open (doc_open routing fix); PropertyManager extrude end conditions.
- **P1 closed (host done):** Revolve full set (end/dir2/thin); Sweep guides/twist/orientation; Loft
  centerline/guides/tangency; Fillet **Face Fillet** + real tangent-propagation; Chamfer
  angle×distance + vertex; Draft **parting-line/step** (new `refs.parting` slot); Mirror bodies+merge.
- **Assemblies:** all **11 advanced/mechanical mates** registered in the schema (MATE_REF_COUNTS +
  MATE_COUPLING_SPEC value-model: gear ratio / screw pitch / limit min-max) AND exposed in the
  assembly mate form (Mechanical & Advanced section + 3rd ref well + type-aware params).
- **UI quality:** angle fields now display `deg` (were `mm`); unit-less dims display bare numbers;
  GUI sizing sweep cleared real chrome debt in 7 screen-space files (gui_lint scale 335 → 249, the
  remainder being world-space draw-list false-positives).
- **Still partial (deferred — real infra gaps):** Pattern instance-skip / vary-instance (needs a
  list/grid PMFieldKind); Hole Wizard ANSI inch sizes (needs dynamic standard→size combos); Drawings
  radial/diameter **host rendering** (engine done; `drawing_panel` rendering pending).

**How to read priority.** P0 = blocks a core professional workflow or is a correctness
defect (wrong geometry / silently-disabled UI). P1 = a daily-use capability a professional
expects. P2 = important but with a workaround. P3 = niche or premium-tier.

**ENGINE DONE — host UI pending** items below are annotated [ENGINE DONE]. The engine
compiler is registered and test-verified; what remains is host PM form / ribbon entry /
tree UI wiring (gated on the parallel host-rework agent).

---

## P0 — blockers and correctness defects (26)

### Sketcher (2D)

> *(Sketcher P0 gap closed 2026-06-14: curve↔curve tangent (arc/spline/conic combinations) fully implemented in `sketch_solver.py`; `test_sketch_tangent.py` green. Status → done. Removed from backlog.)*

### Part Features (3D Solid)
- **Extrude — end conditions** [ENGINE DONE] — *partial* — Engine: Up-To-Surface / Offset-From-Surface / Up-To-Body / Up-To-Vertex + Dir2 matrix + thin + New-Body in `extrude.py`; `test_extrude_ends.py`. DoD to close: host PM ref-pick wells + end-condition combo items → `pm_forms.cpp` extrude form.

### Assemblies
- **Insert Component** — *partial* — DoD: file-picker browser dialog + drag-to-place after insert + sub-assembly child expansion in tree. → `host/src/ui/asm_panels.cpp` drawInsert, `feature_tree.cpp` (.asmstep heuristic).
- **Move Component (constrained drag)** — *partial* — DoD: add rotation-triad manipulator handles re-solved per frame (translation already works). → `host/src/ui/viewport_panel.cpp` s_asmDrag, `mate_solver.py`.
- **Assembly FeatureManager Tree** — *partial* — DoD: expand a component to its internal feature tree; component RMB Float/Fix/Edit menu. → `host/src/ui/feature_tree.cpp` drawAssemblyTree.

### Drawings
- **Radial dimension** [ENGINE DONE] — *partial* — Engine: arc/circle extraction through HLR + radial annotation in `drawing.py`; `test_drawing_dims_ext.py`. DoD to close: host `drawing_panel.cpp` first-class radial rendering (stored as note+kind currently).
- **Diameter dimension** [ENGINE DONE] — *partial* — Engine: circle detection + diameter annotation in `drawing.py`; `test_drawing_dims_ext.py`. DoD to close: host `drawing_panel.cpp` diameter rendering with through-center line.
- **DXF export — complete drawing geometry** — *partial* — DoD: emit dimensions/text/center marks/centerlines/hatch/BOM/title-block layers (today only VISIBLE+HIDDEN). → `engine/doc/dxf.py`, `engine/doc/export2d.py`.
- **PDF export — complete drawing output** — *partial* — DoD: include annotations/title-block/BOM/hatch and use the sheet size (today geometry-only, auto-fit bbox). → `engine/doc/pdfvec.py`, `export2d.py`.

### Sheet Metal

> *(Both Sheet Metal P0 gaps closed 2026-06-14 — removed from backlog:)*
> - *Corner Treatments (Corner Relief): rectangular/obround/tear relief geometry now generated in `sheetmetal.py`; `test_sheetmetal_cuts.py`. Status → done.*
> - *Sheet Metal Cut→flat propagation: base-region + flange-wall holes develop correctly in `sheetmetal_flat.py`; CUTS layer in flat DXF via `export2d.py`; `test_sheetmetal_cuts.py`. Status → done.*

### Weldments
- **Profile library — standard structural sections** [ENGINE DONE — partial] — *partial* — Engine: I/W-beam + T-section + 28-size STANDARD_SIZES catalogue (HSS/pipe/channel/angle/W/T) in `weld_profiles.py`; `test_weldment_ext.py`. DoD to close remaining gap: Z-section + user-defined profile sketch ref.
- **Corner trim — butt/notch/cope/scribe** [ENGINE DONE — partial] — *partial* — Engine: butt + cope (fish-mouth saddle cut, true boolean, any profile) in `weldment.py`; `test_weldment_ext.py`. DoD to close remaining gap: notch/scribe trim variants + host PM joint-type picker.
- **Trim/Extend tool (interactive body trim)** — *missing* — DoD: interactive post-hoc select body-to-trim + bodies-to-trim-against; directional boolean. → `engine/doc/features/weldment.py` + `pm_forms.cpp` + `ribbon_def.cpp`.

### Surfacing

> *(All P0 Surfacing engine gaps closed 2026-06-14 — see ENGINE DONE notes below. What remains is host ribbon tab + PM forms, which are gated on the parallel host-rework agent.)*

- **Surface Bodies folder / surface body concept** [ENGINE DONE] — *partial* — Engine: body `kind` tag in `compiler.py`/`tessellate.py` (two-sided)/`evaluate.py`/`formats.py`; `test_surface_bodykind.py`. DoD to close: Surface Bodies tree folder in `feature_tree.cpp`.
- **Extruded Surface** [ENGINE DONE] — *partial* — Engine: `surf_extrude` compiler registered; `test_surf_extrude.py`. DoD to close: Surfaces ribbon tab + PM form in `ribbon_def.cpp` / `pm_forms.cpp`.
- **Revolved Surface** [ENGINE DONE] — *partial* — Engine: `surf_revolve` compiler registered; `test_surf_revolve.py`. DoD to close: ribbon + PM.
- **Swept Surface** [ENGINE DONE] — *partial* — Engine: `surf_sweep` compiler registered; `test_surf_sweep.py`. DoD to close: ribbon + PM.
- **Lofted Surface** [ENGINE DONE] — *partial* — Engine: `surf_loft` compiler registered; `test_surf_loft.py`. DoD to close: ribbon + PM.
- **Knit Surface** [ENGINE DONE] — *partial* — Engine: `surf_knit` compiler registered; BRepBuilderAPI_Sewing; closed shell flips to solid kind; `test_surf_knit.py`. DoD to close: ribbon + PM.
- **Thicken Surface** [ENGINE DONE] — *partial* — Engine: `surf_thicken` compiler registered; BRepOffset thickening; `test_surf_thicken.py`. DoD to close: ribbon + PM.

### Configurations & Design Tables
- **Equations — global variables** — *partial* — DoD: host Equations dialog to add/edit/delete global vars (engine eq_set/eq_delete already complete). → new `host/src/ui/equations_dialog`; wire `feature_tree.cpp` Equations node.

### Evaluate & Analysis
- **Mass Properties** — *partial* — DoD: density/material field + override checkbox + principal axes/moments + IPS reformat. → `host/src/ui/evaluate_dialogs.cpp`, `engine/doc/evaluate.py` mass_props.
- **Measure** — *partial* — DoD: accumulate multiple measurements, delta-XYZ, diameter, arc-center/midpoint. → `engine/doc/evaluate.py` measure(), `evaluate_dialogs.cpp`.
- **Interference Detection** — *partial* — DoD: multi-body single-part check + viewport highlight of interfering volume + coincident-as-interference toggle. → `engine/doc/evaluate.py` interference(), `asm_panels.cpp` drawInterference.
- **Simulation: Linear Static FEA** — *partial* — DoD: tet10 + local mesh + principal/shear/directional/strain results + roller/pin fixtures + torque/gravity loads + reactions + probe + material entry. → `engine/sim/fea_static.py`, `mesher.py`, `sim_panel.cpp`.

### Import / Export / Interop
- **STEP export (AP214)** — *partial* — DoD: wire File>Save-As-STEP dialog; preserve assembly hierarchy + per-body names; AP203 option. → `host/src/ui/title_bar.cpp` (enable menu), `engine/doc/formats.py`.
- **STEP import** — *partial* — DoD: route plain `.step` through import_file RPC (not stepplus.open); surface diagnostics; into-current-doc option. → `host/src/ui/left_pane.cpp` openCadFile, `engine/doc/formats.py`.
- **Parasolid (x_t/x_b) import** — *missing* — DoD: open Siemens-kernel Parasolid files (most-requested SW exchange). → import bridge in `engine/doc/formats.py` `_EXT_TO_FORMAT` (commercial OCC plug-in or transient STEP conversion).
- **File > Export menu wired** — *missing* — DoD: each Export item opens a Save dialog + calls export_file RPC + toasts (engine RPC ready). → `host/src/ui/title_bar.cpp` (4 stub-disabled items).

### UI / UX / Interaction
- **PropertyManager — OK/Cancel + shortcuts** — *partial* — DoD: OK/Cancel on a dedicated top-right row; Enter=OK/Esc=Cancel wired. → `host/src/ui/property_manager.cpp` (GUI_AUDIT 3.3).
- **PropertyManager — extrude end conditions** — *partial* — DoD: add Up-To-Surface / Up-To-Body to the PM combo (tracks the engine gap). → `host/src/ui/pm_forms.cpp` extrude form.
- **DPI / HiDPI scaling** — *partial* — DoD: fix ribbon width/overlap arithmetic ≥2×; add cross-monitor DPI-change handler. → ribbon sizing in host UI (GUI_AUDIT 1.2, 2.1–2.3, 2.5).

---

## P1 — daily-use professional capabilities (101)

### Sketcher (2D)
- **Convert entities** — *partial* — DoD: live parametric link that re-projects on model rebuild (today static fix). → `engine/doc/ops.py` sketch_convert, `session.py` sketch_project_ref.
- **Under-defined exit warning** — *partial* — DoD: warn on exiting an under-defined sketch (configurable gate). → `host/src/ui/sketch_mode.cpp` exitSketch().
- **Constraint glyph display** — *partial* — DoD: render SW-style relation glyphs (⊥ // =) on entity bodies. → `sketch_mode.cpp` drawOverlay.

### Part Features (3D Solid)
- **Extrude draft + thin feature** [ENGINE DONE] — *partial* — Engine: thin via concentric Wire.offset_2d rings in `extrude.py`; `test_extrude_ends.py`. DoD to close: host PM thin-wall group in `pm_forms.cpp`.
- **Revolve — full feature set** [ENGINE DONE] — *partial* — Engine: Dir2, Mid-Plane, Up-To-Surface, thin, construction-centerline axis in `revolve.py`; `test_revolve.py`. DoD to close: host PM form in `pm_forms.cpp`.
- **Sweep — core** [ENGINE DONE] — *partial* — Engine: guide curves + twist + orientation modes (follow-path/frenet/keep-normal) + thin in `sweep.py`; `test_sweep.py`. DoD to close: host PM guide/twist wells in `pm_forms.cpp`.
- **Loft — core** [ENGINE DONE] — *partial* — Engine: start/end tangency, centerline + guide curves, thin in `loft.py`; `test_loft.py`. DoD to close: host PM guide/tangency wells in `pm_forms.cpp`.
- **Fillet — constant + variable** [ENGINE DONE] — *partial* — Engine: face fillet, conic (rho), tangent propagation (BFS G1-edges) in `dress.py`; `test_dress_ext.py`. DoD to close: host PM face-set wells + tangent-prop checkbox in `pm_forms.cpp`.
- **Chamfer — symmetric/asymmetric** [ENGINE DONE] — *partial* — Engine: angle×distance (AddDA) + vertex chamfer in `dress.py`; `test_dress_ext.py`. DoD to close: host PM form in `pm_forms.cpp`.
- **Hole Wizard** [ENGINE DONE] — *partial* — Engine: ANSI/UNC/UNF + BSP/NPT tables, cosmetic-thread annotation record, sketch-point positions in `hole.py`; `test_hole_ext.py`. DoD to close: host PM ANSI combo + thread-render display in `pm_forms.cpp`.
- **Linear Pattern** [ENGINE DONE] — *partial* — Engine: instance skip + vary-instance + geometry_pattern in `pattern.py`; `test_pattern_ext.py`. DoD to close: host PM skip-well; curve/sketch/fill/table modes still missing from engine too.
- **Circular Pattern** [ENGINE DONE] — *partial* — Engine: instance skip + bidirectional in `pattern.py`; `test_pattern_ext.py`. DoD to close: host PM skip-well; body pattern mode still needed.
- **Instant3D — face drag** — *partial* — DoD: extend drag to revolve angle / fillet radius / chamfer / shell / rib / pattern spacing. → `engine/doc/instant3d.py`, `viewport_panel.cpp`.
- **Equations / Design Variables (file-linked)** — *partial* (engine done) — DoD: only the file-linked/external-equation extension remains. → `engine/doc/equations.py`.
- **Tangent Propagation on Fillet/Chamfer** [ENGINE DONE] — *partial* — Engine: BFS over G1-continuous edges in `dress.py`; `test_dress_ext.py`. DoD to close: host PM checkbox emitting tangent_prop (currently CheckSoon) in `pm_forms.cpp`.
- **Extrude From-condition** [ENGINE DONE] — *partial* — Engine: from_face ref resolves surface/face/plane/vertex in `extrude.py`; `test_extrude_ends.py`. DoD to close: host PM From group in `pm_forms.cpp`.

### Assemblies
- **Fixed / Floating Component** — *partial* — DoD: RMB Float/Fix context-menu toggle. → `host/src/ui/feature_tree.cpp` (op exists).
- **Linear Component Pattern** — *partial* — DoD: model-edge direction ref + second direction (2D grid). → `engine/doc/assembly.py` `_world_axis_from_ref`.
- **Circular Component Pattern** — *partial* — DoD: model-edge/datum axis + partial-fill angle. → `engine/doc/assembly.py` `_compile_component_patterns`.
- **Exploded View — manual steps** — *partial* — DoD: interactive triad drag to author/reorder/animate steps. → `host/src/ui/asm_panels.cpp` drawExplode.
- **BOM — assembly panel** — *partial* — DoD: Description + custom-property columns + balloons + sort/filter. → `host/src/ui/asm_panels.cpp` drawBom.
- **Drawing Views of Assembly** — *partial* — DoD: per-component HLR separation + exploded drawing views + per-component section colors. → `engine/doc/drawing.py` `_source_shapes`.
- **Component Mass Properties (asm context)** — *partial* — DoD: transform CG/inertia to world frame + total-assembly aggregate query. → `engine/worker.py` `_eval_active_shape`, `evaluate.py`.
- **Mate Edit** — *missing* — DoD: double-click/RMB Edit reopens the Mate PM pre-filled. → `host/src/ui/feature_tree.cpp` (asm_edit_mate op exists).

### Drawings
- **Projected (child) views** — *partial* — DoD: automatic ortho alignment to parent + first/third-angle toggle. → `engine/doc/drawing.py` add_view, `drawing_panel.cpp`.
- **Auxiliary view** — *missing* — DoD: view ⊥ to a picked inclined edge/face. → `engine/doc/drawing.py` (custom gp_Ax2 projector + 'auxiliary' type).
- **Multi-sheet drawings** — *missing* — DoD: add/rename/reorder/delete sheets; host renders the active sheet. → `engine/doc/drawing.py` (sheet RPCs), `drawing_panel.cpp`.
- **Title block / sheet format** — *missing* — DoD: static ISO title block with editable fields. → `drawing_panel.cpp` + drawing schema.
- **Angular dimension** [ENGINE DONE] — *partial* — Engine: angular annotation in `drawing.py`; `test_drawing_dims_ext.py`. DoD to close: host `drawing_panel.cpp` first-class angular rendering.
- **Hole callout / smart annotation** — *missing* — DoD: auto note from Hole feature data + leader. → `engine/doc/drawing.py` (link projected circle → Hole feature).
- **Free-text note with leader** — *partial* — DoD: add leader-line geometry from note to feature. → `engine/doc/drawing.py` add_dim note, `drawing_panel.cpp`.
- **Center mark** [ENGINE DONE] — *partial* — Engine: center mark annotation in `drawing.py`; `test_drawing_dims_ext.py`. DoD to close: host `drawing_panel.cpp` crosshair rendering.
- **Centerline** [ENGINE DONE] — *partial* — Engine: centerline annotation in `drawing.py`; `test_drawing_dims_ext.py`. DoD to close: host CENTER linetype + dash-dot rendering in `drawing_panel.cpp`.
- **GD&T / Feature Control Frame** — *missing* — DoD: framed symbol + tolerance + datum boxes. → `engine/doc/drawing.py` `gdt_frame` type, `drawing_panel.cpp`.
- **Balloon / item bubble** — *missing* — DoD: numbered circle + leader tied to BOM rows. → `engine/doc/drawing.py` balloon type (BOM data exists).
- **BOM table on sheet** — *partial* — DoD: place BOM in sheet-mm coords so it exports; configurable columns. → `engine/doc/drawing.py` drawing_bom, `drawing_panel.cpp`.
- **DXF export — native arcs/circles** — *missing* — DoD: emit ARC/CIRCLE entities for CAM. → `engine/doc/dxf.py` `_edge_to_polyline` (detect GeomAbs_Circle).
- **View-to-view alignment locks** — *missing* — DoD: projected view locked to parent's projection axis. → drawing doc schema + `drawing_panel.cpp`.
- **View drag-and-drop repositioning** — *missing* — DoD: drag views on the sheet, annotations follow. → `drawing_panel.cpp` (view drag handler).
- **Smart dim snap to geometry** — *partial* — DoD: midpoint/arc-center/intersection snap (today vertex only). → `drawing_panel.cpp` snapToVertex().
- **Dim value override + prefix/suffix** — *missing* — DoD: structured prefix/suffix/tolerance fields. → drawing annotation schema.
- **Tolerance annotation** — *missing* — DoD: bilateral/limit/fit + ISO H7/h6 auto-populate. → drawing annotation schema.
- **Annotation drag to reposition** — *missing* — DoD: drag dim text; extension lines re-compute. → `drawing_panel.cpp` + drawing_update_annotation RPC.

### Sheet Metal
- **Edge Flange** — *partial* — DoD: multi-edge selection per feature + custom flange profile sketch. → `engine/doc/features/sheetmetal.py` compile_sm_edge_flange (edges[0] only).
- **Miter Flange** — *missing* — DoD: swept edge-chain with mitered corners. → new `sm_miter_flange` in `sheetmetal.py` + schema + ribbon + PM.
- **Sketched Bend** — *missing* — DoD: bend along a line drawn on the broad face. → new `sm_sketched_bend` (face-split + bend about line).
- **K-Factor + Bend Tables** — *partial* — DoD: BA-table and BD-table lookup + .btl import. → `engine/doc/features/sheetmetal.py` bend_allowance.
- **Sheet Metal Gauge Tables** — *missing* — DoD: material+gauge → thickness/radius/K JSON lookup + Base-Flange combo. → new gauge table data + `pm_forms.cpp`.
- **Part-Level SM Properties Panel** — *missing* — DoD: top-of-tree SM node (thickness/K/relief) + Folded/Unfolded toggle. → feature tree + surface ctx as a node.
- **SM Ribbon — Full SW Tab Parity** — *partial* — DoD: add miter/tab/break-corner/sketched-bend/rip/unfold/fold/insert-bends/cross-break ribbon entries. → `host/src/ui/ribbon_def.cpp`.

### Weldments
- **Custom / user-defined profile** — *missing* — DoD: import a sketch/DXF as a member cross-section. → `engine/doc/weld_profiles.py` (profile_sketch_ref), schema.
- **Weld bead geometry** — *missing* — DoD: cosmetic/solid bead along a joint seam, listed in cut list. → new `weld_bead` feature + schema + ribbon + PM.
- **Cut list — mass/weight** — *missing* — DoD: mass column = volume×density (volume already stored). → `engine/doc/cutlist.py` build_cut_list + weld_member density param.
- **Cut list — Solid Bodies folder** — *missing* — DoD: named per-member bodies + rename + exclude-from-cutlist. → BodyOut metadata + `feature_tree.cpp`.
- **Weldment Drawing — cut list table** — *missing* — DoD: cut list table + balloons on a drawing sheet. → `engine/doc/drawing.py` cut_list_table annotation.

### Surfacing
- **Boundary Surface** [ENGINE DONE] — *partial* — Engine: `surf_boundary` compiler; BRepFill_Filling + ThruSections fallback; `test_surf_boundary.py`. DoD to close: ribbon + PM in `ribbon_def.cpp`/`pm_forms.cpp`.
- **Planar Surface** [ENGINE DONE] — *partial* — Engine: `surf_planar` compiler; `test_surface_bodykind.py`. DoD to close: ribbon + PM.
- **Offset Surface** [ENGINE DONE] — *partial* — Engine: `surf_offset` compiler; BRepOffsetAPI_MakeOffsetShape; `test_surf_offset.py`. DoD to close: ribbon + PM.
- **Fill Surface** [ENGINE DONE] — *partial* — Engine: `surf_fill` compiler; BRepFill_Filling g0/g1/g2; `test_surf_fill.py`. DoD to close: ribbon + PM.
- **Trim Surface** [ENGINE DONE] — *partial* — Engine: `surf_trim` + `surf_untrim` compilers; BRepAlgoAPI_Splitter + ShapeFix_Face; `test_surf_trim.py`. DoD to close: ribbon + PM.
- **Extrude end: Up-To-Surface / Offset** [ENGINE DONE] — *partial* — Engine: `extrude.py` `_VALID_ENDS` + face ref; `test_extrude_ends.py`. DoD to close: host PM ref-pick well.
- **Extrude From: Surface/Face/Plane** [ENGINE DONE] — *partial* — Engine: from_face ref in `extrude.py`; `test_extrude_ends.py`. DoD to close: host PM From group.

### Configurations & Design Tables
- **Assembly-level config support** — *partial* — DoD: per-config mate distance/angle overrides. → `engine/doc/session.py` `_rebuild_assembly` (mate overlay).
- **Config-specific custom document properties** — *missing* — DoD: per-config Description/PartNo/Revision for BOM. → doc schema `properties` + ops.py set_property + UI.

### Evaluate & Analysis
- **Section Properties** — *partial* — DoD: pick any datum/face plane + section outline + chosen CS. → `engine/doc/evaluate.py` section_props, `evaluate_dialogs.cpp`.
- **Dynamic Section View** — *partial* — DoD: cut-face hatch + interactive plane drag + save named view. → `host/src/ui/section_tool.cpp`.
- **Simulation: Modal / Frequency** — *partial* — DoD: animate each mode + pre-stress modal + CSV export. → `engine/sim/fea_modal.py`, `sim_panel.cpp`.
- **FloXpress / Flow Simulation** — *partial* — DoD: internal flow + surface-pressure plot + temperature + velocity cut-plot + water fluid. → `engine/sim/lbm.py`, `sim_panel.cpp`.
- **Assembly-level Mass Properties** — *partial* — DoD: aggregate total mass/CoM/inertia of all components. → `engine/worker.py` `_eval_active_shape`.
- **FEA Custom Material Entry** — *missing* — DoD: type custom E/nu/density/yield in the study form. → `host/src/ui/sim_panel.cpp` drawStaticForm.
- **FEA Result Probe / List** — *missing* — DoD: probe stress/disp at a picked node + table. → probe RPC over StaticResult.npz + `sim_panel.cpp`.
- **FEA Reaction Forces** — *missing* — DoD: reaction force per fixture + net balance. → `engine/sim/fea_static.py` (f = K·u on constrained DOF).
- **Mass Props Density UI / Material** — *missing* — DoD: density/material field + override + part-document material. → `evaluate_dialogs.cpp`, `engine/doc/schema.py`.

### Import / Export / Interop
- **IGES export** — *partial* — DoD: UI dialog + menu item (engine works). → `host/src/ui` + `engine/doc/formats.py`.
- **IGES import** — *partial* — DoD: GUI path + user sew-tolerance + diagnostics. → `host/src/ui/left_pane.cpp`, `formats.py` `_import_iges`.
- **STL export** — *partial* — DoD: UI dialog + binary/ASCII + tolerance + triangle-count preview. → `engine/doc/formats.py` export_stl, host export dialog.
- **DWG export/import** — *missing* — DoD: DWG read/write (or DWG-compatible DXF R2013+). → `engine/doc/formats.py` (ODA/Teigha or ezdxf).
- **ACIS/SAT import** — *missing* — DoD: open .sat/.sab (Inventor/Fusion). → `engine/doc/formats.py` `_EXT_TO_FORMAT`.
- **STEP assembly hierarchy export** — *missing* — DoD: product/instance tree via XDE STEPCAFControl_Writer. → `engine/doc/formats.py` `_shapes_to_export_list`.
- **Import diagnostics on import** — *partial* — DoD: auto-run check() in import_file and return diagnostics. → `engine/doc/formats.py` import_file + `evaluate.py` check().
- **Geometry healing on import** — *partial* — DoD: user-invokable heal RPC + ShapeUpgrade + report. → `engine/doc/formats.py` (ShapeFix/ShapeUpgrade).
- **Unit detection on import** — *missing* — DoD: read STEP LENGTH_UNIT, convert/prompt (avoid 25.4× error). → `engine/doc/formats.py` `_import_step`.
- **Import into current document** — *partial* — DoD: UI insert-into-doc path + placement transform + body selection. → `host/src/ui` + `engine/doc/features/import_feature.py`.
- **Assembly BOM (drawing)** — *partial* — DoD: render BOM as a sheet table + Excel/CSV export. → `engine/doc/drawing.py` drawing_bom, drawing panel.
- **KiCad File Interoperability** — *partial* — DoD: implement `engine.ecad.kicad_export` for .kicad_sch/.kicad_pcb write. → `engine/ecad/formats.py`, `rpc.py` `_EXPORT_KINDS`.

### UI / UX / Interaction
- **FeatureManager — Properties/Config tabs** — *partial* — DoD: wire Properties + Configurations tabs to real panes. → `host/src/ui/feature_tree.cpp` tab strip.
- **PropertyManager — two-column grid** — *missing* — DoD: fixed label column aligned with input column. → `host/src/ui/property_manager.cpp` (GUI_AUDIT 3.4).
- **PropertyManager — fillet tangent propagation** — *missing* — DoD: working checkbox emitting tangent_prop (tracks engine gap). → `pm_forms.cpp` fillet form.
- **Selection — tangent-chain propagation** — *missing* — DoD: loop/tangent select for fillet/chamfer edge picks. → `viewport_panel.cpp` + engine face-chain topology.
- **Selection — 3D box select** — *missing* — DoD: rubber-band window/crossing select in the model viewport. → `host/src/ui/viewport_panel.cpp`.
- **Selection — selection filter** — *missing* — DoD: global face/edge/vertex/body filter bar. → `host/src/ui` new SelectionFilter.
- **View — section view** — *partial* — DoD: confirm/finish interactive plane drag. → `host/src/ui/section_tool.cpp`.
- **Heads-up toolbar — navigation bar** — *partial* — DoD: add Previous View + 3D Drawing View buttons. → `host/src/ui/viewport_panel.cpp` HUD.
- **Copy / Paste features** — *missing* — DoD: Ctrl+C/V feature copy (enable disabled menu items) + engine op. → `host/src/ui/title_bar.cpp` + worker op.
- **Ribbon — overflow at high DPI** — *missing* — DoD: collapse to icons → chevron menu; tab-strip wrap. → ribbon layout (GUI_AUDIT 2.4–2.5).
- **Equations dialog** — *partial* — DoD: global-variable editor dialog (same item as Configurations area). → new equations dialog.
- **PropertyManager — selector well chips** — *partial* — DoD: bordered wrapped chips + per-chip X-remove (no truncation). → `property_manager.cpp` (GUI_AUDIT 3.7).
- **Sketch — box select** — *partial* — DoD: confirm window-vs-crossing modes. → `host/src/ui/sketch_mode.cpp`.

### ECAD
- **Simulation GUI — Waveform Plot** — *partial* — DoD: click-a-wire-to-probe + node-voltage annotations on schematic. → `host/src/ui/ecad/ecad_panel.cpp`.
- **KiCad Schematic Export (.kicad_sch write-back)** — *missing* — DoD: write native schematic back to KiCad format. → `engine/ecad/formats.py` write_kicad_sch.

---

## P2 — important, with a workaround (94)

### Sketcher (2D)
- **Fillet/chamfer on arc corners** — *missing* — DoD: fillet/chamfer where at least one entity is an arc. → `engine/doc/sketch_geom.py` `_line_corner` + solver arc-arc tangency.
- **Mirror of non-primitive entities** — *missing* — DoD: mirror splines/slots/ellipses/text. → `engine/doc/ops.py` `_MP_SUPPORTED`.
- **Move/Copy of non-primitive types** — *missing* — DoD: move/copy splines/slots/text/ellipses. → `engine/doc/ops.py` sketch_move guard.
- **Rotate Entities** — *missing* — DoD: dedicated select/center/angle rotate tool. → new rotate op + `SketchTool::Rotate` + ribbon.
- **Sketch Blocks** — *missing* — DoD: create/insert/define/explode named reusable groups. → new block op/entity/RPC across solver, ops, UI.
- **Pierce constraint** — *missing* — DoD: point at axis/edge ∩ sketch plane (for helix sweeps). → `engine/doc/sketch_solver.py` `_add_constraint`.
- **Grid + grid snapping** — *partial* — DoD: snap-to-grid toggle quantizing cursor to grid. → `host/src/ui/sketch_mode.cpp`.

### Part Features (3D Solid)
- **Boundary Boss/Cut** — *missing* — DoD: solid boundary feature with per-direction guides/tangency. → new `boundary` solid compiler.
- **Shell** [ENGINE DONE] — *partial* — Engine: multi-thickness (per-face offset overrides) in `dress.py`; `test_dress_ext.py`. DoD to close: host PM per-face thickness well.
- **Draft** [ENGINE DONE] — *partial* — Engine: parting-line draft (split + opposite-sign DraftAngle per side) + step draft in `draft.py`; `test_ribdraft_ext.py`. DoD to close: host PM pull-dir well.
- **Rib** [ENGINE DONE] — *partial* — Engine: explicit drop direction + normal-vs-parallel thicken in `rib.py`; `test_ribdraft_ext.py`. DoD to close: host PM direction-pick well.
- **Wrap** — *missing* — DoD: project sketch onto a curved surface (emboss/deboss/scribe). → new `wrap` feature.
- **Mirror Feature** [ENGINE DONE] — *partial* — Engine: refs.bodies + merge toggle (union vs separate body) in `pattern.py`; `test_pattern_ext.py`. DoD to close: host PM bodies well + merge checkbox.
- **Sketch/Curve/Fill Pattern** — *missing* — DoD: instances at sketch points / along a curve / flood-fill. → new pattern compilers + ribbon.
- **Reference — Plane** [ENGINE DONE] — *partial* — Engine: parallel-through-point / normal-to-curve / on-surface in `datum.py`; `test_datum_ext.py`. DoD to close: host PM method picker.
- **Helix / Spiral** [ENGINE DONE] — *partial* — Engine: variable-pitch (per-region pitch/dia table, B-spline) + flat Archimedean spiral in `curves.py`; `test_helix_ext.py`. DoD to close: host PM variable-pitch table well.
- **Split** [ENGINE DONE] — *partial* — Engine: non-planar/surface split via BRepAlgoAPI_Splitter + centroid keep-side in `split.py`; `test_split_ext.py`. DoD to close: host PM surface-tool well.
- **Configurations (Design Variants)** — *partial* — DoD: Design Table + per-config reference geometry. → `engine/doc/compiler.py` + design table.
- **Cosmetic Thread** — *missing* — DoD: render thread_callout as annotation + standalone cosmetic-thread feature (Hole Wizard now stores annotation record; standalone feature + visual render still missing). → `engine/doc/features/hole.py` + tessellation.
- **Pattern — instance skip** [ENGINE DONE] — *partial* — Engine: per-instance skip set + vary-instance in `pattern.py`; `test_pattern_ext.py`. DoD to close: host PM skip-instance well.
- **Extrude/Revolve — Selected Contours UI** — *partial* — DoD: per-region multi-select picker in the PM (engine supports indices). → `pm_forms.cpp`.

### Assemblies
- **Advanced Mate: Symmetric** — *missing* — DoD: mirror two comps about a plane (3 DOF). → `engine/doc/mate_solver.py` builder dispatch + PM.
- **Standard Mates: Perpendicular** — *partial(UX)* — DoD: add to PM auto-suggest list. → `host/src/ui/asm_panels.cpp` suggestedMate.
- **Standard Mates: Tangent / Angle / Lock** — *partial(UX)* — DoD: add tangent/angle/lock to PM auto-suggest list. → `asm_panels.cpp`.
- **Mirror Component** — *missing* — DoD: mirror components across a plane, BOM-listed. → new `asm_mirror` feature + UI.
- **Exploded View — explode lines** — *missing* — DoD: dashed guide rails from exploded to mated position. → `engine/doc/assembly.py` + viewport.
- **BOM — drawing integration** — *partial* — DoD: insert BOM table on the drawing + balloons. → `engine/doc/drawing.py` drawing_bom + drawing panel.
- **Sub-Assembly Rigid / Flexible** — *missing* — DoD: Flexible mode where internal mates join the parent solve. → `engine/doc/assembly.py` + recursive solve + schema field.
- **In-Context Editing** — *missing* — DoD: edit a part in assembly with external references. → cross-component selector resolution.
- **Assembly Configurations** — *partial* — DoD: config-specific mate values + per-config BOM. → `engine/doc/session.py` config overlay.
- **Component Appearance Override** — *partial* — DoD: RMB Appearance menu for components. → `host/src/ui/feature_tree.cpp`.
- **Mate Geometry: Sphere** — *partial* — DoD: sphere-plane / sphere-sphere tangent residual block. → `engine/doc/mate_solver.py` `_MateBuilder`.

### Drawings
- **Half / Aligned / Offset section views** — *missing* — DoD: half-space, unfolded-aligned, and stepped (compound half-space) section types. → `engine/doc/drawing.py` `_half_space_cut`.
- **Broken-out section view** — *missing* — DoD: spline-bounded cavity to a depth. → `engine/doc/drawing.py` (arbitrary solid clip).
- **Crop / partial view** — *missing* — DoD: clip a view to a sketch boundary. → drawing view schema + `_compound_polylines`.
- **Drawing sheet border + zones** — *missing* — DoD: thick border + A1–D4 zone grid. → `drawing_panel.cpp`.
- **Ordinate (baseline) dimensions** — *missing* — DoD: baseline origin + stacked ordinate dims. → `engine/doc/drawing.py` annotation system.
- **Datum feature symbol** — *missing* — DoD: triangle + datum letter (companion to GD&T). → drawing annotation type.
- **Surface finish symbol** — *missing* — DoD: √ with Ra/Rz (ISO 1302). → drawing annotation type.
- **Weld symbol annotation** — *missing* — DoD: AWS/ISO weld symbol on a joint. → drawing annotation type.
- **Revision table** — *missing* — DoD: Rev/Description/Date/Approved history table. → drawing schema.
- **Hole table** — *missing* — DoD: tabulated X/Y/dia/depth from a datum. → `engine/doc/drawing.py` (hole detection + coordinates).
- **Section hatch customization** — *missing* — DoD: per-material angle/spacing/pattern. → `drawing_panel.cpp` hatchPolygon.
- **Show/hide tangent edges per view** — *partial* — DoD: solid/phantom/hidden tangent-edge style toggle. → `engine/doc/drawing.py` (Rg1 toggle).
- **Break view** — *missing* — DoD: shorten long parts with break-line symbols. → `engine/doc/drawing.py`.
- **BOM column customization** — *missing* — DoD: add/reorder part-no/desc/material/weight columns. → `engine/doc/drawing.py` drawing_bom.
- **Print / plot output** — *missing* — DoD: send a sheet to a system printer. → host print integration.
- **Drawing template creation** — *missing* — DoD: save/reuse a drawing template. → serializable template format.

### Sheet Metal
- **Bend Relief (Auto Relief)** — *missing* — DoD: notch at the ends of a partial-width flange. → `engine/doc/features/sheetmetal.py` (relief params + geometry).
- **Tab Feature** — *missing* — DoD: additive same-gauge bump co-planar with the sheet, flat-pattern-aware. → new `sm_tab`.
- **Flat Pattern in Drawing** — *partial* — DoD: named flat-pattern drawing view + bend (UP/DOWN/angle) annotations. → `engine/doc/drawing.py` flat-pattern view type.
- **Unfold / Fold (individual bend)** — *missing* — DoD: partial unfold of selected bends with a fixed face. → new `sm_unfold`/`sm_fold`.
- **Break Corner** — *missing* — DoD: fillet/chamfer flat-pattern corners (propagates to DXF). → new `sm_break_corner`.

### Weldments
- **Corner trim — mitre (arc)** — *partial* — DoD: trim arc-segment members at junctions (today butt at ends; straight-segment cope trim now done). → `engine/doc/features/weldment.py` `_mitre_trim`.
- **Cut list — custom properties** — *missing* — DoD: per-item Description/Material/Finish/Supplier. → properties map on weld_member/BodyOut + UI.
- **Profile Locate — 9-point justification** — *partial* — DoD: full 3×3 snap grid (today center+corner). → `engine/doc/weld_profiles.py` build_profile_face.
- **Profile rotation + mirror** — *partial* — DoD: horizontal/vertical flip params. → `engine/doc/features/weldment.py` + `pm_forms.cpp`.
- **Member material / appearance** — *missing* — DoD: per-member material (mass) + color. → weld_member param + BodyOut.color.
- **Weldment part document flag** — *missing* — DoD: auto-flag part as weldment + Solid Bodies folder. → doc schema `weldment` flag.

### Surfacing
- **Ruled Surface** [ENGINE DONE] — *partial* — Engine: `surf_ruled` compiler; BRepFill.Face_s + ThruSections(ruled); `test_surf_ruled.py`. DoD to close: ribbon + PM.
- **Replace Face** [ENGINE DONE] — *partial* — Engine: `surf_replace_face` compiler; BOP-split + keep-retained-side; `test_surf_replace_face.py`. DoD to close: ribbon + PM.
- **Delete Face** [ENGINE DONE] — *partial* — Engine: `surf_delete_face` compiler; drop faces → open shell + kind flip; `test_surf_delete_face.py`. DoD to close: ribbon + PM.
- **Spline on Surface** — *missing* — DoD: 3D spline on a model face (UV-projected). → 3D-sketch-on-face infra (shared with Sketcher item); deliberately deferred.

### Configurations & Design Tables
- **Configuration rename** — *missing* — DoD: rename a config, update all references. → `engine/doc/ops.py` config_rename + RPC + UI.
- **Configure Feature context menu** — *missing* — DoD: jump to config panel filtered to a feature. → `host/src/ui/feature_tree.cpp` + config panel filter.

### Evaluate & Analysis
- **Smart Sensors** — *missing* — DoD: watched value + threshold alert in tree. → new sensor record + post-rebuild eval + tree UI.
- **FEA Mesh Quality (Jacobian)** — *missing* — DoD: Jacobian-ratio check + highlight bad elements. → `engine/sim/mesher.py`.
- **FEA Local Mesh Controls** — *missing* — DoD: per-face finer mesh density. → `sim_panel.cpp` + `mesher.py` sizing fields.
- **Measure — accumulate** — *missing* — DoD: scrollable list of measurements + totals. → `host/src/ui/evaluate_dialogs.cpp` (client-side list).
- **Section Props — plane picker** — *missing* — DoD: pick any datum/face/section line. → `evaluate_dialogs.cpp` selector well.

### Import / Export / Interop
- **STL import** — *partial* — DoD: import as a usable body + mesh-to-solid conversion + binary/ASCII. → `engine/doc/formats.py` `_import_stl`.
- **3MF export** — *partial* — DoD: per-body color + quality control + working 3MF import. → `engine/doc/formats.py` (Mesher color), import_feature.
- **STEP AP242 + PMI/GD&T** — *missing* — DoD: MBD tolerances/datums via STEPCAFControl_Writer. → `engine/doc/formats.py` + GD&T model.
- **STEP color/appearance round-trip** — *missing* — DoD: per-body RGB embedded/read via XCAFDoc_ColorTool. → `engine/doc/formats.py`.
- **Export selected bodies only** — *missing* — DoD: body_ids filter on export + UI checkboxes. → `engine/doc/formats.py` export_file.
- **Save a copy as** — *missing* — DoD: export without changing session path. → `engine/worker.py` save_copy_as RPC.
- **Pack and Go** — *partial* — DoD: inline imported geometry bytes (or copy+relativize references). → `engine/doc/stepplus.py` + import_feature.
- **STL tessellation quality control** — *missing* — DoD: deviation/angle sliders + preview. → `engine/doc/formats.py` export_stl params + UI.

### UI / UX / Interaction
- **FeatureManager — Shift+click range-select** — *missing* — DoD: contiguous range selection. → `host/src/ui/feature_tree.cpp`.
- **FeatureManager — search/filter** — *missing* — DoD: live name filter. → `feature_tree.cpp`.
- **PropertyManager — draft angle unit bug** — *partial* — DoD: Angle() instead of Dim() so the draft field shows [deg]. → `pm_forms.cpp` extrude form (one-line fix, GUI_AUDIT 3.6).
- **Selection — lasso select in 3D** — *missing* — DoD: freeform closed-curve select. → `viewport_panel.cpp`.
- **Command search** — *missing* — DoD: search box jumps to commands. → ribbon-level search input.
- **Keyboard — customizable bindings** — *missing* — DoD: Tools>Customize>Keyboard editor. → settings + dispatch.
- **Rollback bar — keyboard Ctrl+Up/Down** — *partial* — DoD: keyboard rollback stepping (today reorders features). → `feature_tree.cpp`.
- **Design Table — spreadsheet driver** — *partial* — DoD: (native grid acceptable; optional Excel embed). → `design_table_panel.cpp`.
- **View history — Previous/Next View** — *missing* — DoD: camera-history ring buffer. → `viewport_panel.cpp` / `camera_anim.cpp`.
- **Perspective / ortho toggle** — *missing* — DoD: HUD projection toggle. → `viewport_panel.cpp` + renderer.

### ECAD
- **Signal Integrity (impedance)** — *(done; no gap)* — *(listed P2 in catalog but status done — excluded.)*
- **DNP / Zoom-to-Violation** — *(done; no gap)* — *(excluded.)*
- **Board / Assembly Variants** — *missing* — DoD: named variants with per-variant BOM/fab. → `engine/ecad/schema.py` + fab outputs.
- **Bus Routing (Schematic)** — *partial* — DoD: bus-entry tap objects + bus union-find merges. → `engine/ecad/ops.py` + `netlist.py`.
- **Blind/Buried/Micro Via (HDI)** — *partial* — DoD: autorouter places blind/buried/micro vias. → `engine/ecad/autoroute.py`.
- **Diff-Pair Length Match (manual)** — *partial* — DoD: standalone tune-length op for routed tracks. → `engine/ecad/autoroute.py` / new pcb_tune_length op.
- **Schematic Cross-Probe to PCB** — *missing* — DoD: select in one editor highlights the other. → `sch_canvas` / `pcb_canvas` highlight sync.
- **Netlist Export — Standard Formats** — *partial* — DoD: KiCad/OrCAD/PADS netlist export. → `engine/ecad/rpc.py` `_ecad_export`.

---

## P3 — niche or premium-tier (46)

### Sketcher (2D)
- **Scale Entities** — *missing* — DoD: uniform scale about a base point. → new scale op + `SketchTool::Scale`.
- **Spline on Surface** — *missing* — DoD: spline on a model face (deferred; needs 3D-on-face infra). → ribbon `sk_spline_surface` + projection infra.
- **Offset with caps / bi-directional** — *missing* — DoD: end caps + two-sided offset. → `engine/doc/sketch_geom.py` offset_chain.

### Part Features (3D Solid)
- **Reference — Axis (on-surface)** [ENGINE DONE] — *partial* — Engine: on-surface axis (normal at picked point) in `datum.py`; `test_datum_ext.py`. DoD to close: host PM method picker.
- **Reference — Point (extra methods)** [ENGINE DONE] — *partial* — Engine: on-curve / intersection / centroid (GProp) / explicit coordinate in `datum.py`; `test_datum_ext.py`. DoD to close: host PM method picker.
- **Feature Tree — properties dialog** — *partial* — DoD: Feature Properties (material/description) dialog. → host feature tree.
- **Fillet — Face Fillet** [ENGINE DONE] — *partial* — Engine: face fillet (blend chain of edges between two face sets) in `dress.py`; `test_dress_ext.py`. DoD to close: host PM face-set wells in `pm_forms.cpp`.
- **Pattern — geometry vs feature** — *partial* — DoD: feature-pattern path (re-solve seed per instance). → `engine/doc/features/pattern.py`.

### Assemblies
- **Advanced Mate: Width** — *missing* — DoD: center a tab between groove faces. → `engine/doc/mate_solver.py`.
- **Advanced Mate: Profile Center** — *missing* — DoD: align profile centroids. → `mate_solver.py`.
- **Advanced Mate: Path Mate** — *missing* — DoD: point travels along a sketch path. → `mate_solver.py` (path parameterization).
- **Mechanical Mates: Hinge / Gear / Cam / Screw / Linear Coupler** — *missing* — DoD: the five mechanical mate types (need velocity-level constraints). → `engine/doc/mate_solver.py`.
- **Smart Fasteners** — *missing* — DoD: auto bolt/washer/nut stacks from a fastener library. → new fastener catalog + auto-mate.
- **Assembly Rollback Bar** — *missing* — DoD: roll back to before mates/features. → assembly doc ordering + UI.

### Drawings
- **Revolved section view** — *missing* — DoD: cross-section revolved 90° onto parent. → `engine/doc/drawing.py`.
- **Detail view — rect/sketch crop** — *missing* — DoD: rectangular/polygon detail boundary. → `engine/doc/drawing.py` `_clip_polyline_to_rect`.
- **Alternate position view** — *missing* — DoD: phantom overlay of a second pose. → `engine/doc/drawing.py`.
- **Dual units on dimensions** — *missing* — DoD: mm and inch simultaneously. → `engine/doc/drawing.py` `_format_dim_value`.

### Sheet Metal
- **Lofted Bend** — *missing* — DoD: sheet lofted between two profiles with approximate unfold. → new `sm_lofted_bend`.
- **Convert to Sheet Metal** — *missing* — DoD: solid → sheet metal via fixed face + rips. → new `sm_convert`.
- **Forming Tool** — *missing* — DoD: library punch (louver/dimple) stamp. → forming-tool library + feature.
- **Gusset (Sheet Metal)** — *missing* — DoD: stiffening bead in a bend corner. → new `sm_gusset`.
- **Rip** — *missing* — DoD: slit a closed tube so it can develop flat. → new `sm_rip`.
- **Insert Bends** — *missing* — DoD: thin solid → sheet metal via face analysis. → new `sm_insert_bends`.
- **Cross Break** — *missing* — DoD: X-pattern stiffening ridge. → new `sm_cross_break`.

### Weldments
- **Multiple path sketches per member** — *missing* — DoD: one feature spans multiple sketches. → `engine/doc/schema.py` weld_member refs.
- **Structural Member groups** — *missing* — DoD: multiple profile groups in one feature. → weld_member schema (group array).

### Surfacing
- **Untrim Surface** [ENGINE DONE] — *partial* — Engine: `surf_untrim` compiler (in `surf_trim.py`); BRepLib + ShapeFix_Face regrows face to natural UV domain; `test_surf_trim.py`. DoD to close: ribbon + PM.

### Configurations & Design Tables
- **Display States** — *missing* — DoD: appearance-only variants (color/hide). → new display_state concept.
- **Per-configuration sketch suppression** — *missing* — DoD: suppress individual sketch dims per config. → `engine/doc/schema.py` suppressions.
- **Equations — external spreadsheet link** — *missing* — DoD: link Design Table to Excel. → spreadsheet integration.
- **Equations — sensor-driven variables** — *missing* — DoD: sensor value as a global variable. → sensor concept + equations env.

### Evaluate & Analysis
- **Simulation: Thermal/Nonlinear/Fatigue/Buckling/Drop/PV** — *missing* — DoD: premium study types. → `engine/sim/` new solvers.
- **Design Study / Optimization** — *missing* — DoD: parametric sweep/optimizer over sensors. → new study framework (depends on sensors).

### Import / Export / Interop
- *(No pure-P3 gaps; BREP round-trip is `done`.)*

### UI / UX / Interaction
- **Heads-up — customizable 'S' bar** — *missing* — DoD: drag commands in/out of the bar. → `host/src/ui/shortcut_bar.cpp`.
- **Task pane — Design Library** — *missing* — DoD: drag standard parts into an assembly. → `host/src/ui/left_pane.cpp`.
- **Ground shadow / AO quality** — *partial* — DoD: ambient-occlusion toggle (shadow/RealView exist). → `viewport_panel.cpp` + renderer.

### ECAD
- **EMI/EMC Checks** — *missing* — DoD: return-path/loop-area/decoupling advisor. → new `engine/ecad/` analysis.
- **Power Integrity / IR Drop** — *missing* — DoD: power-net resistance + voltage drop. → new analysis (copper as distributed R).
- **Thermal Simulation of PCB** — *missing* — DoD: component temperatures from a power map. → new PCB thermal solver.
- **IBIS / S-Parameter** — *missing* — DoD: high-speed IO buffer / Touchstone models. → new model support.
- **Component Rotation / Assembly Rules** — *missing* — DoD: reflow-direction / edge-straddle checks. → `engine/ecad/dfm.py`.

---

## Suggested implementation sequencing

The P0 block is the parity floor; within it, the highest-leverage clusters are:

1. **Wire the export UI** — every File>Export item is stub-disabled while the engine RPC is
   ready (`title_bar.cpp`). A few lines of C++ per format converts ~6 partial Import/Export
   rows toward done.
2. **Surface-body concept** — one gating prerequisite (`compiler.py` bodies-kind tag) unlocks
   the entire Surfacing area; the four generators + knit + thicken follow mechanically.
3. **Sheet Metal / Weldment correctness** — corner relief, sheet-metal-cut-to-flat,
   butt/cope Trim-Extend, and the standard profile library are the four rows that make
   fabricated parts actually correct rather than merely renderable.
4. **Drawing annotation engine** — radial/diameter dims (P0) plus the P1 annotation cluster
   (angular, center mark/line, hole callout, GD&T, balloons, title block) require one shared
   investment: arc-entity extraction from the HLR pass. Land that first.
5. **Extrude end/from conditions** — touches both Part Features and Surfacing and is the most
   common professional modeling need across the catalog.

Items graduate out of this backlog into [SW_PARITY_SPEC.md](SW_PARITY_SPEC.md) only when
their full definition-of-done is met with concrete evidence.
