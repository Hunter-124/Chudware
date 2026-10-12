# SolidWorks Parity Specification (SW_PARITY_SPEC)

## Update 2026-06-14 — engine parity push (commits f725fe7..6128e9b)

10 engine commits, 660 tests green (0 failed). Summary of jumps:

- **Surfacing**: 0% → 47% — all 15 surface features implemented in engine (surf_planar / extrude / revolve / sweep / loft / offset / ruled / boundary / fill / knit / thicken / trim / untrim / delete_face / replace_face) plus the surface-body foundation (body `kind`, two-sided render, area mass). Host ribbon/PM forms not yet wired — all 18 changed rows are `partial` pending the host rework agent.
- **Part Features**: 46% → 51% — full SW option matrix added to engine (extrude end conditions + Dir2 + thin; revolve Dir2/mid/up-to-surface/thin/centerline; sweep guides/twist/orientation/thin; loft guides/centerline/tangency/thin; fillet face-fillet/conic/tangent-prop; chamfer angle×distance/vertex; shell multi-thickness; draft parting-line/step; rib explicit drop dir; hole ANSI/UNC/BSP/NPT tables + cosmetic-thread record + sketch-point positions; linear/circular pattern skip/vary/bidirectional; mirror bodies+merge; reference plane/axis/point extra methods; helix variable-pitch + flat spiral; non-planar split). All host PM forms pending — new rows stay `partial`.
- **Sketcher**: 80% → 81% — curve↔curve tangent constraint now covers all arc/spline/conic combinations (test_sketch_tangent.py); the P0 gap is closed.
- **Drawings**: 33% → 37% — radial, diameter, angular dimensions + center marks/lines added to engine (test_drawing_dims_ext.py) via arc extraction through HLR projection; host rendering annotation polish pending.
- **Sheet Metal**: 33% → 39% — cut→flat-pattern propagation fully done (sheetmetal_flat.py + export2d.py CUTS layer; test_sheetmetal_cuts.py) and corner relief geometry (rect/obround/tear) done (test_sheetmetal_cuts.py); both are engine-geometry DoD → `done`.
- **Weldments**: 33% → 36% — I/W-beam + T-section profiles + 28-size catalogue added; butt + cope trim implemented (test_weldment_ext.py); `partial` (notch/scribe remain).
- **Overall**: 54% → 57%.

---

**Status:** Canonical parity standard for Chudware. This is the contract the project
is built to reach: every capability keeps its SolidWorks acceptance condition, status,
and concrete evidence. The dated update above records the original engine push, not
current test results or current source paths.

**Scope:** Mechanical CAD parity is measured against SolidWorks 2020–2025. The ECAD /
Electronics area has no SolidWorks analog and is measured against a KiCad-class baseline;
every ECAD item is a beyond-SW differentiator by definition.

**Status vocabulary**
- **done** — the SW acceptance condition is met with concrete, named evidence (engine + host where a UI is implied).
- **partial** — the core path works but one or more acceptance sub-conditions are unmet.
- **missing** — not implemented (no engine path, no UI, or an explicit stub/disabled control).
- **beyond** — exceeds the SolidWorks baseline (architectural improvement or a capability SW lacks).

**Conservatism rule:** where evidence was ambiguous or a sub-condition unverified, the
capability is scored down. A capability is `done` only when its evidence is concrete.

**Evidence refresh (2026-10-04):** live pointers below name C++ compiler dispatch,
document ops, worker RPCs, ribbon commands and PropertyManager forms. Artifact presence
does **not** establish every acceptance sub-condition; unchanged statuses retain the
prior assessment unless a specific contradiction is identified. Prose without a
checkable pointer remains explicitly unverifiable in `scripts/parity_audit.py`.
Historical Python test names and measurements in the dated update are not live evidence.

**Parity %** per area is computed as `(done + beyond + 0.5·partial) / total`.

---

## Parity scorecard — historical 2026-06-14 snapshot

| Area | done | partial | missing | beyond | total | Parity % |
|------|-----:|--------:|--------:|-------:|------:|---------:|
| Sketcher (2D) | 44 | 4 | 9 | 0 | 57 | **81%** |
| Part Features (3D Solid) | 5 | 28 | 4 | 0 | 37 | **51%** |
| Assemblies | 20 | 14 | 16 | 1 | 51 | **55%** |
| Drawings | 17 | 12 | 33 | 0 | 62 | **37%** |
| Sheet Metal | 9 | 3 | 15 | 0 | 27 | **39%** |
| Weldments | 5 | 5 | 11 | 0 | 21 | **36%** |
| Surfacing | 0 | 18 | 1 | 0 | 19 | **47%** |
| Configurations & Design Tables | 15 | 2 | 7 | 0 | 24 | **67%** |
| Evaluate & Analysis | 6 | 9 | 11 | 1 | 27 | **43%** |
| Import / Export / Interop | 12 | 12 | 11 | 0 | 35 | **51%** |
| UI / UX / Interaction | 41 | 13 | 16 | 2 | 72 | **69%** |
| ECAD / Electronics (beyond-SW) | 30 | 6 | 8 | 3 | 47 | **77%** |
| **TOTAL** | **204** | **126** | **142** | **7** | **479** | **57%** |

The scorecard records the 2026-06-14 assessment; it is not a fresh measurement of
today's tree. `python3 scripts/parity_audit.py` computes status totals and live-pointer
coverage from the rows below. It does not run acceptance tests. The current C++ host
has surfacing ribbon/forms and extended feature controls; their existence alone does
not promote the inherited `partial` rows to full SolidWorks parity.

The implementation backlog derived from every `partial` and `missing` row below used to
live in `SW_PARITY_GAPS.md` (pruned 2026-07-23 in bb29af1c); the status rows in this
document are now the single source of truth.

---

## 1. Sketcher (2D)

The sketcher is at roughly SolidWorks 2020 parity on the core workflow: a full entity
suite over a Newton/LM solver with analytic Jacobians, the complete geometric-constraint
set (including the P0 curve↔curve tangent gap closed 2026-06-14), Smart Dimension with
the Modify box (driven dims, equation links, over-define pre-flight, double-click edit),
and the editing tools (trim/extend/offset/fillet/chamfer/mirror/pattern/convert/move).
Gaps include blocks, pierce, arc-corner fillets and non-primitive entity transforms.
Rotate Entities now has a dedicated selection/center/angle tool.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Sketch plane entry / exit / cancel | New Sketch locks camera normal to plane; Exit commits; cancel reverts to entry checkpoint with camera animation | done | `sketch_mode.cpp` enterNewOnSelection/createSketchOnPlane/exitSketch/cancelSketch; doc_checkpoint+doc_revert |
| Line + chaining | Click-click line re-arms a chain with coincident; H/V auto-relation within ~5° | done | `sketch_mode.cpp` SketchTool::Line, kHVDeg=5, chainPrevId_ |
| Circle (center+radius, perimeter/3-pt) | Center-point and 3-pick circumscribed circle | done | SketchTool::Circle/CirclePerimeter; circumcircle helper |
| Arc (center, 3-point, tangent) | Center/3-pt/tangent-to-prior arc | done | ArcCenter/Arc3pt/ArcTangent; C54 tangent source resolver |
| Rectangle (5 variants) | Corner/center/3-pt-corner/3-pt-center/parallelogram, 4 lines + relations | done | RectCorner..Parallelogram; addQuadLoop |
| Polygon (N-sided) | Regular inscribed/circumscribed polygon, equal edges | done | SketchTool::Polygon; addPolyLoop equalEdges |
| Slot (4 variants) | Straight endpoint/centerpoint, arc 3-pt/centerpoint | done | Slot/SlotCtrStr/SlotArcCtr/SlotArc3pt; arc_slot entity |
| Spline (through-pt, style) | Interpolating + control-pole spline, draggable | done | `host/src/ui/sketch_mode.cpp` SketchTool::Spline/StyleSpline; compiler `sketch`; `core/doc/document.cpp` spline edge construction |
| Ellipse (full + partial) | Center+axes, partial trims angles | done | Ellipse/EllipsePartial; ellipse/ellipse_arc entities |
| Conic (parabola, rho) | Parabola vertex/focus; rho conic apex/shoulder | done | Parabola/Conic; rational quadratic Bezier |
| Equation-driven curve | x(t)/y(t) + range evaluated to fixed geometry | done | SketchTool::EqCurve; eq_curve entity |
| Sketch text | String at anchor with cap height (reference geometry) | done | ribbon `sk_text`; `host/src/ui/sketch_mode.cpp` SketchTool::Text; compiler `sketch`; `core/doc/document.cpp` text entities |
| Sketch point | Standalone constrainable point | done | SketchTool::Point; point entity; dim pick handles it |
| Construction toggle | Any entity → construction, excluded from regions | done | sketch_toggle_construction; region detection excludes it |
| Inference snapping | Endpoint/mid/rim/alignment snap with guide lines, master toggle | done | snapPoint() kSnapPx=6, mid/rim prefixes, inferenceOn_ |
| Auto-relations on placement | Coincident/H/V/tangent/midpoint added on snap | done | kHVDeg auto-relation; tan/rim snap paths |
| Geometric relations popup | Full context set: coincident…symmetric, fix, point-on, coradial | done | ribbon `sk_relations`; op `sketch_add_constraint`; `core/sketch/sketch.cpp` add_constraint dispatch |
| Tangent constraint (line/arc) | All combinations: line-circle, arc-arc, line-conic, spline | done | rpc `sketch_solve`; `core/sketch/sketch.cpp` add_tangent_line_circle/add_tangent_circle_circle/add_tangent_line_conic/add_tangent_line_curve/add_tangent_curve_curve dispatch and mutual-closest-point numeric residual |
| Smart Dimension (all types) | Aligned/H/V/radius/diameter/angle, typed or =expr, draggable | done | SmartDim; dimCommit; distance/radius/diameter/angle constraints |
| Driven (reference) dims | Parenthesized, consumes no DOF | done | driven flag; dimLabel parenthesizes; solver skips driven |
| Equation-linked dims | =Var/=expr links to equation engine | done | op `sketch_set_dim`; `host/src/ui/sketch_mode.cpp` dimCommit; `core/sketch/sketch.cpp` resolve_dim |
| Dimension min/max | Near/far rim attachment to circle | done | cond field; condShift() anchor move |
| Over-define pre-flight + dialog | Probe before commit: Make Driven/Leave Driving/Cancel | done | sketch_probe RPC; overDefOpen_/relOverDefOpen_/editOverDefOpen_ |
| Fully-defined detection + DOF | Under/Fully/Over with DOF count, color-coded entities | done | _overall_status(); statusLine(); entity status field |
| Conflict-safe geometry freeze | Contradictory solve rejected → entities hold last-valid | done | _solve_active rejected→coord_map; solve_rejected flag |
| Constraint-exact drag | Real-time warm-start re-solve ≤10ms, commit on release | done | sketch_drag/_commit; nsolve _drag_solve KKT-Schur 6.4ms |
| Trim | Remove clicked span between intersections; <2-cross circle deleted | done | split_entity(); sketch_trim; hover highlight |
| Extend | Extend line/arc to first intersection | done | extend_entity(); sketch_extend |
| Offset (chain) | Connected chain offset with corner trim/extend | done | offset_chain(); sketch_offset; side_xy |
| Sketch fillet | Round corner of two lines (setback + tangent arc) | done | fillet_corner(); sketch_fillet; SkFillet (C52) |
| Sketch chamfer | Bevel corner of two lines | done | chamfer_corner(); sketch_chamfer; SkChamfer (C52) |
| Fillet/chamfer on arc corners | Allow at least one arc input | **missing** | _line_corner raises GeomError for non-line; solver lacks arc-arc tangency |
| Mirror entities | Mirror across construction-line axis with symmetric relations | done | sketch_mirror_entities; _symmetric_pairs; Mirror (C53) |
| Mirror of non-primitive entities | Mirror splines/slots/ellipses/text | **missing** | _MP_SUPPORTED = line/circle/arc only |
| Linear/circular entity patterns | Direction+spacing+count / center+angle+count | done | sketch_pattern_entities; LinPattern/CircPattern (C53) |
| Convert entities | Project model edges/faces into sketch with live link | **partial** | sketch_convert/sketch_project_ref work but link is static fix, not live parametric |
| Move/Copy entities | From/to or delta; copy adds translated copies | done | sketch_move (copy flag); Move (C54) |
| Move/Copy of non-primitive types | Move splines/slots/text/ellipses | **missing** | _MP_SUPPORTED guard raises unsupported_entity |
| Rotate Entities | Dedicated tool: select, center, angle | done | op `sketch_rotate`; ribbon `sk_rotate_entities`; `host/src/ui/sketch_mode.cpp` RotateEntities selection, draggable center, ring/typed angle and applyRotateOp; line/circle/arc inputs |
| Scale Entities | Uniform scale about a base point | **missing** | no scale op anywhere |
| Sketch Blocks | Create/insert/define/explode named reusable groups | **missing** | no block op/entity/RPC anywhere |
| Under-defined exit warning | Warn on exiting an under-defined sketch | **partial** | DOF shown in status bar; no exit gate dialog |
| Redundant-constraint detection | Report/highlight redundant relations (Over Defined) | done | redundant list in SketchSolution; status bar "N redundant" |
| Constraint glyph display | Relation icons (=, //, ⊥) on entity bodies, click-to-select | **partial** | cons_ tracked/selectable; SW-style glyph icons not confirmed drawn |
| Delete relation | Click glyph / RMB → Delete Relation | done | sketch_delete_constraint; selRelIds_; Delete dispatch |
| Smart Dimension placement drag | Drag annotation; placement stored per constraint | done | placement field; sketch_set_dim; dimPlaceU_/V_ |
| Double-click dim to edit | Modify box pre-filled with current value | done | dimEditCid_; applyDimEditOp |
| Ghost rendering of inactive sketches | Inactive sketches grey in viewport | done | drawGhosts(); doc_sketch_overview; ghost cache (C40) |
| Grid + grid snapping | Faint grid; optional snap-to-grid | **partial** | grid drawn (kGridStep=10); dedicated grid-snap toggle not found |
| Region detection + region picker | Even-odd closed regions; Selected Contours pick | done | doc_sketch_regions; drawRegionPicker; ear-clipping (C51) |
| Sketch origin | Virtual fixed (0,0) handle | done | snapPoint 'origin'; _resolve_handle zero-DOF |
| Spline on Surface | Spline constrained to a model face | **missing** | sk_spline_surface greyed (deliberately deferred) |
| Pierce constraint | Point at axis/edge ∩ sketch plane (for sweeps) | **missing** | no 'pierce' in solver/schema/popup |
| Along X / Along Y distance | H/V component variants of distance dim | done | axis='h'/'v'; dimAxis_ |
| Dimension to a datum plane | Reference dim to front/top/right plane | done | addPlaneDimRef(); sketch_project_ref |
| Offset with caps / bi-directional | End caps + two-sided offset | **missing** | offset_chain has no cap/bidirectional param |
| Instant3D sketch dim handle drag | Drive dim from 3D view (not in sketch) | done | doc_push_pull/doc_instant3d (W9) |

---

## 2. Part Features (3D Solid)

A cohesive feature set (extrude/revolve/sweep/loft, fillet/chamfer/shell/draft/rib, Hole
Wizard, pattern/mirror, reference geometry, helix, boolean, split, Instant3D, rollback,
suppression, equations). The dated engine push described the original option matrix.
Current engine evidence is `core/doc/document.cpp`, with types validated in
`core/doc/validate.cpp`; host controls live in `host/src/ui/pm_forms.cpp`. Existing forms
replace the historical "host UI pending" blanket claim but do not prove full acceptance.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Extrude — end conditions | Blind/Through-All/Mid/Up-To-Next/Surface/Body/Vertex/Offset, Dir1+Dir2, draft, thin, merge/new-body | **partial** | compiler `extrude`; pm_form `extrude`; `core/doc/document.cpp`; host exposes Direction 2 and reference end wells; complete end-condition matrix still needs acceptance verification |
| Extrude draft + thin feature | Draft per direction + thin-wall shell of profile | **partial** | compiler `extrude`; pm_form `extrude` draft_outward/draft, thin_thickness/thin_side; per-direction draft acceptance not established by form presence |
| Revolve — full feature set | Dir1+Dir2 angle, Mid/Up-To-Surface, thin, sketch centerline axis | **partial** | compiler `revolve`; pm_form `revolve` end/dir2/angle2/thin/axis controls; complete option behavior remains unverified |
| Sweep — core | Profile-along-path + guide curves + twist + orientation modes | **partial** | compiler `sweep`; pm_form `sweep` guides/twist_turns/twist_degrees/orientation; `core/doc/document.cpp`; complete guide/twist/orientation acceptance remains unverified |
| Loft — core | 2+ sections + guide curves + centerline + start/end tangency + matching | **partial** | compiler `loft`; pm_form `loft` sections/guides/centerline/tangency_start/tangency_end; matching and continuity acceptance remains unverified |
| Boundary Boss/Cut | Independent guide curves + tangency/curvature per direction | **missing** | no boundary type/compiler/ribbon |
| Fillet — constant + variable | Constant + variable + face + full-round + conic profiles + tangent prop | **partial** | compiler `fillet`; pm_form `fillet` constant/variable only; full-round/face/conic/tangent-propagation acceptance not established by the current controls |
| Chamfer — symmetric/asymmetric | Symmetric/asymmetric/angle×distance/vertex + tangent prop | **partial** | compiler `chamfer`; pm_form `chamfer` symmetric/asymmetric/distance_angle/vertex controls; tangent-propagation acceptance remains unverified |
| Shell | Uniform wall, remove faces, in/out, multi-thickness | **partial** | compiler `shell`; pm_form `shell` remove/thickness/outward; per-face thickness overrides not exposed by this form |
| Draft | Neutral-plane taper + parting-line + step + pull override | **partial** | compiler `draft`; pm_form `draft`; `core/doc/document.cpp`; full parting-line/step/pull matrix remains unverified |
| Rib | Thin wall from open contour, both/one side, draft, flip | **partial** | compiler `rib`; pm_form `rib`; `core/doc/document.cpp`; complete direction/thickening acceptance remains unverified |
| Wrap | Project sketch onto curved surface (emboss/deboss/scribe) | **missing** | not in FEATURE_TYPES/compiler/ribbon |
| Hole Wizard | Simple/cbore/csink/tap; ISO+ANSI+BSP+JIS; multi-position; cosmetic thread | **partial** | compiler `hole`; pm_form `hole`; `core/doc/document.cpp` hole sizing and positions; complete standards and cosmetic-thread display acceptance remains unverified |
| Linear Pattern | 1D/2D grid + skip + vary + curve/sketch/fill/table modes + body pattern | **partial** | compiler `linear_pattern`; pm_form `linear_pattern`; `core/doc/document.cpp`; curve/sketch/fill/table mode acceptance not established |
| Circular Pattern | Count/angle/equal-spacing + skip + bidirectional + body pattern | **partial** | compiler `circular_pattern`; pm_form `circular_pattern`; `core/doc/document.cpp`; complete skip/bidirectional/body behavior remains unverified |
| Mirror Feature | Mirror features OR bodies + geometry-pattern + merge | **partial** | compiler `mirror`; pm_form `mirror`; `core/doc/validate.cpp` features/bodies refs; complete merge/pattern behavior remains unverified |
| Sketch/Curve/Fill Pattern | Instances at sketch points / along curve / flood-fill a face | **missing** | no compiler/ribbon/type |
| Reference — Plane | Offset/angle/3-pt/mid/parallel-at-pt/normal-to-curve/on-surface | **partial** | compiler `plane`; pm_form `plane`; `core/doc/document.cpp`; complete reference-method acceptance remains unverified |
| Reference — Axis | Cylinder/2-plane/2-point/edge/on-surface | **partial** | compiler `axis`; pm_form `axis`; `core/doc/document.cpp`; complete reference-method acceptance remains unverified |
| Reference — Point | Vertex/center/on-face/on-curve/intersection/centroid/coordinate | **partial** | compiler `point`; pm_form `point`; `core/doc/document.cpp`; complete reference-method acceptance remains unverified |
| Reference — CSYS | Origin + X/Y dir, derived Z (right-hand) | done | pm_form csys; ribbon ref_csys; Gram-Schmidt; PM wells for all three |
| Helix / Spiral | Pitch/height/turns combos + taper + start angle + CW/CCW + variable pitch + flat spiral | **partial** | compiler `helix`; pm_form `helix`; `core/doc/document.cpp`; variable-pitch/spiral matrix and host editing remain unverified |
| Multi-body Boolean | Combine/Subtract/Intersect + keep tools, 2+ bodies | done | pm_form boolean; ribbon boolean_combine/boolean_subtract/boolean_intersect; keep_tools |
| Split | Planar split keep both/above/below, multi-body | **partial** | compiler `split`; pm_form `split`; `core/doc/document.cpp`; non-planar keep-side acceptance remains unverified |
| Instant3D — face drag | Drag any sketch-feature face to drive its dim, 1 undo, expr-protected | **partial** | extrude depth/box/cylinder/hole dia/datum offset; no revolve/fillet/chamfer/shell/rib/pattern drag |
| Rollback Bar | Suppress all features below bar; insert after bar; undo-aware | done | op `set_rollback`; `core/doc/session.cpp` and `core/doc/document.cpp` rollback_after; host rollback bar |
| Feature Suppression | Suppress/unsuppress, config-specific, greyed | done | op `set_suppressed`; `core/doc/session.cpp` configuration suppressions; host tree [S] rendering |
| Configurations (variants) | Per-config param overrides + suppression + design table | **partial** | overrides+suppression done; no design table, no per-config ref geometry |
| Equations / Design Variables | Global vars + dim refs + cycle detection + eval order | done | op `eq_set`/`eq_delete`; `core/doc/session.cpp`; expression resolution in `core/doc/document.cpp` |
| Feature Tree — rename/reorder/folder | Rename, dependency-checked reorder, folders, properties dialog | **partial** | rename/reorder/folder ops done; no Feature Properties dialog |
| Cosmetic Thread | Annotation marking a cylindrical face threaded | **missing** | thread_callout is BOM-only, not rendered; no standalone cosmetic-thread feature |
| Tangent Propagation (fillet/chamfer) | Auto-include tangent-connected edges (SW default) | **partial** | compiler `fillet`/`chamfer`; current forms do not expose tangent propagation; original Python implementation is not current acceptance evidence |
| Fillet — Face Fillet | Rolling-ball fillet between two non-adjacent faces | **partial** | compiler `fillet`; current pm_form `fillet` exposes edge constant/variable modes, not two face sets; rolling-ball face-fillet acceptance remains unverified |
| Extrude From-condition | Start from surface/face/plane/vertex/offset (not just sketch plane) | **partial** | compiler `extrude`; pm_form `extrude` start/start_ref/start_offset; complete From-condition acceptance remains unverified |
| Pattern — instance skip | Suppress individual instances in the grid | **partial** | compiler `linear_pattern`/`circular_pattern`; `core/doc/document.cpp`; host instance-skip editing and complete behavior remain unverified |
| Pattern — geometry vs feature | Re-solve seed per instance (follow faces) vs rigid copy | **partial** | always geometry pattern; no feature-pattern path |
| Extrude/Revolve — Selected Contours | Multi-select individual regions in a multi-region sketch (PM UI) | **partial** | engine indexed selection works; PM has single profile well only |

---

## 3. Assemblies

A solid P0 core: new assembly, insert-with-cache, auto-fix-first, the seven standard
mates over a Rodrigues-exp-map LM solver with analytic Jacobians, constrained drag,
component patterns, exploded-view slider, interference, BOM+CSV, undo/redo, and
mate redundancy/conflict classification. Gaps concentrate in mate-editing UI,
advanced/mechanical mates, mirror, flexible sub-assemblies, and in-context editing.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| New Assembly Document | Blank asm with 3 datum planes, auto-open Insert, first comp fixed | done | rpc `doc_new`; op `asm_insert_component`; `core/doc/session.cpp` assembly initialization; host drawAssemblyTree |
| Insert Component | File picker, place at click/drop, multi-instance, sub-asm | **partial** | text-path input (no browser), origin-only placement, sub-asm flat-loads but no child expansion |
| Component Suppression | Suppress removes from solve+tessellation; config-specific | done | op `asm_edit_component`; rpc `config_set_suppression`; `core/doc/session.cpp` and `core/doc/assembly.cpp`; host tree [S] |
| Fixed / Floating | First auto-fixed; RMB Float/Fix toggle | **partial** | op exists; no RMB context-menu toggle in UI |
| Mate: Coincident | Plane/axis/vertex coincident, aligned/anti, removes 3–4 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` coincident residual dispatch; `core/doc/assembly.cpp` geometry resolution |
| Mate: Parallel | Faces/edges parallel, aligned/anti, 2 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` parallel residual dispatch |
| Mate: Perpendicular | Axes/normals orthogonal, 1 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` perpendicular residual dispatch |
| Mate: Concentric | Coaxial cylinders, removes 4 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` concentric residual dispatch |
| Mate: Distance | Signed gap with flip, 1 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` distance residual dispatch |
| Mate: Angle | Angle between normals/edges with flip, 1 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` angle residual dispatch |
| Mate: Tangent | Cylinder-plane / cyl-cyl tangent, 1 DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` tangent residual dispatch |
| Mate: Lock | Freeze relative 6-DOF | done | op `asm_add_mate`; `core/mate/mate.cpp` lock residual dispatch |
| Advanced Mate: Symmetric | Mirror two comps about a plane, 3 DOF | **missing** | no symmetric mate type |
| Advanced Mate: Width | Center tab between groove faces, 1 DOF | **missing** | not implemented |
| Advanced Mate: Profile Center | Align profile centroids, 2–4 DOF | **missing** | not implemented |
| Advanced Mate: Path Mate | Point travels along a sketch path | **missing** | not implemented |
| Mechanical: Hinge | Concentric+coincident+angular limits | **missing** | not implemented |
| Mechanical: Gear | Ratio-coupled rotation | **missing** | requires velocity-level constraints |
| Mechanical: Cam/Follower | Follower tangent to cam profile | **missing** | not implemented |
| Mechanical: Screw | Translation/rotation pitch coupling | **missing** | not implemented |
| Mechanical: Linear Coupler | Ratio-coupled translation | **missing** | not implemented |
| Aligned / Anti-Aligned toggle | Seat/oppose normals, persists on edit | done | op `asm_edit_mate`; `core/mate/mate.cpp`; host mate alignment controls |
| Mate Redundancy / Over-Def detection | Redundant (orange) / conflicting (red), names offender | done | rpc `doc_solve_mates`; `core/mate/mate.cpp` rank/status classification; complete UI acceptance retains the prior assessment |
| DOF Summary | "X DOF remaining (N comps, M fixed)" or Fully constrained | done | rpc `doc_solve_mates`; `core/doc/assembly.cpp` and `core/mate/mate.cpp` solver DOF data; complete status rendering retains the prior assessment |
| Mate Suppress / Unsuppress | RMB Suppress grays mate, removes from solve | done | op `asm_edit_mate`; `core/doc/session.cpp`; host tree context menu |
| Mate Delete | RMB Delete; cascade on component delete | done | op `asm_delete_mate`/`asm_delete_component`; `core/doc/session.cpp` |
| Mate Edit | Double-click/RMB Edit reopens PM pre-filled | **missing** | asm_edit_mate op exists; no UI Edit path |
| Move Component (constrained drag) | Drag floating comp along free DOF, mates hold; rotate triad | **partial** | translation drag verified; no rotate triad |
| Linear Component Pattern | Direction (edge/axis/vector) + spacing + count, BOM rollup | **partial** | world datum axes only (no edge dir); no 2nd direction; instances view-only |
| Circular Component Pattern | Count + angle + equal-spacing + axis (edge/datum) | **partial** | world datum axes only; no partial-fill angle option |
| Mirror Component | Mirror comps across a plane, BOM listed | **missing** | no asm_mirror feature/engine/UI |
| Exploded View — manual steps | Drag comps with triad to author named steps; animate | **partial** | flat-list steps via op; no triad authoring, no per-step animate/reorder |
| Exploded View — factor slider | 0→1 interpolation, display-only | done | rpc `doc_set_explode_state`; `core/doc/session.cpp`; host explode slider |
| Exploded View — explode lines | Dashed guide rails from exploded to mated pos | **missing** | no line rendering/emission |
| Interference Detection | Pairwise overlap volume + min clearance, highlight | done | rpc `eval_interference`; `core/doc/session.cpp`; `host/src/ui/evaluate_dialogs.cpp` inventory/overlay path; complete clearance/highlight acceptance retains the prior assessment |
| BOM — assembly panel | Live item/name/qty/desc/mass, CSV/annotation | **partial** | Name/Source/Qty/Mass + CSV; no Description/custom props, no balloons, no sort/filter |
| BOM — drawing integration | Linked auto-updating BOM table + balloons on sheet | **partial** | drawing_bom data exists; no UI table insert, no balloons |
| Smart Fasteners | Auto-populate bolt/washer/nut stacks from Toolbox | **missing** | no fastener library/Toolbox |
| Sub-Assembly Rigid / Flexible | Rigid (one body) vs Flexible (internal mates in parent solve) | **missing** | all sub-asm rigid; no rigid_flexible field |
| In-Context Editing | Double-click comp → edit part with external references | **missing** | no in-context mode / cross-comp selector |
| Assembly Configurations | Per-config component suppression + mate-value overrides | **partial** | component suppression done; per-config mate values deferred |
| Assembly FeatureManager Tree | Components (f), Mates folder, expand to part tree, context menus | **partial** | components/(f)/[S]/Mates done; no component-subtree expand, no Float/Fix menu |
| Assembly Rollback Bar | Roll back to before mates/features | **missing** | asm docs have no feature ordering for rollback |
| Component Appearance Override | RMB Appearance per instance | **partial** | auto-color + op override done; no RMB Appearance menu for components |
| Drawing Views of Assembly | Ortho+iso+section of assembled model, auto-update | **partial** | whole-asm silhouette; no per-component HLR separation, no exploded views |
| Component Mass Properties (asm context) | Per-comp mass/CG/inertia in world frame | **partial** | rpc `eval_mass`; `core/doc/session.cpp` gathers world-placed assembly shapes; full per-component/material reporting acceptance remains unverified |
| Multiple Instances of Same Part | Independent transform/mates, shared cached geometry | done | op `asm_insert_component`; `core/doc/assembly.cpp` g_load_cache shared component geometry with distinct instance ids |
| Undo / Redo | Ctrl+Z/Y restores doc incl. solved transforms | done | rpc `doc_undo`/`doc_redo`; `core/doc/session.cpp` transactional history |
| Warm-Start Solve | Reuse last solution as init for fast convergence | done | rpc `doc_solve_mates`; `core/doc/session.cpp` asm_warm state; `core/mate/mate.cpp` warm-start solve |
| Antipodal / Flip Singularity Escape | Escape 180° rotation saddle | **beyond** | 6 random rotation perturbations when unconverged (SW handles implicitly) |
| Mate Geometry: Sphere | Sphere-plane / sphere-sphere coincident/tangent | **partial** | sphere MateGeom resolved but no sphere residual block (undefined behavior) |

---

## 4. Drawings

The OCCT HLR projection engine is strong (associative, cached, persistent: base/projected/
iso views, one-cut section, circular detail, linear/aligned dims, free notes, basic BOM,
DXF R12 + vector PDF). The annotation additions in the 2026-06-14 update are historical.
Current drawing sessions and annotations live in `core/doc/drawing_session.cpp`,
projection in `core/doc/drawing.cpp`, rendering in `host/src/ui/drawing_panel.cpp`.
Inherited statuses are not promoted solely from RPC presence: each full drafting
acceptance condition still requires its own end-to-end evidence.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Base orthographic views | 6 orientations, visible+hidden, associative | done | project_view(); _ORIENT; add_view base/projected |
| Projected (child) views | Aligned first/third-angle projection chains | **partial** | engine projects; no alignment enforcement, no angle toggle |
| Isometric view | SE iso, no hidden by default | done | _iso_axes(); add_view iso |
| Section view — full planar cut | Cutting plane + 45° hatch + arrows + letter label | done | project_section/_half_space_cut; hatchPolygon; commitSection |
| Half section view | Cut half of a symmetric part | **missing** | no half_section type |
| Aligned section view | Angled plane unfolded to a planar projection | **missing** | not implemented |
| Offset (stepped) section view | Multi-plane stepped cut | **missing** | _half_space_cut single plane only |
| Revolved section view | Cross-section revolved 90° onto parent | **missing** | not implemented |
| Broken-out section view | Spline-bounded cavity to a depth | **missing** | not implemented |
| Detail view (circular) | Circle region redrawn at higher scale, DETAIL A | done | project_detail; _clip_polyline_to_circle; commitDetail |
| Detail view — rect/sketch crop | Rectangular/polygon detail boundary | **missing** | circle clip only |
| Auxiliary view | View ⊥ to a chosen inclined edge/face | **missing** | type enum is base/projected/iso only |
| Crop / partial view | Clip a view to a sketch boundary | **missing** | no crop concept |
| Alternate position view | Phantom overlay of a second pose | **missing** | not implemented |
| View scale control | Per-view scale, label, true-length dims | done | add_view scale; dim value = sheet_dist/scale |
| Sheet size selection | ISO A0–A4 + ANSI A–E with border | done | SHEET_DIMS_MM; paper rect; default A3 |
| Multi-sheet drawings | Add/rename/reorder/delete sheets (tabs) | **missing** | one sheet 's1'; no sheet RPCs; host renders sheet[0] only |
| Title block / sheet format | Fill-in title block + custom formats | **missing** | only an 8mm trim border, no fields/templates |
| Sheet border + zones | Thick border + A1–D4 zone grid | **missing** | thin margin only |
| Linear dimension (H/V) | Extension lines + arrows + value | done | add_dim linear_dim; renderDimension |
| Aligned dimension | True point-to-point distance | done | add_dim aligned_dim; inclined dim line |
| Radial dimension | R-prefix leader from center to arc | **partial** | rpc `drawing_add_dim`; `core/doc/drawing_session.cpp` radial annotations; `host/src/ui/drawing_panel.cpp`; complete placement/rendering acceptance remains unverified |
| Diameter dimension | Ø with through-center line / leader | **partial** | rpc `drawing_add_dim`; `core/doc/drawing_session.cpp` diameter annotations; `host/src/ui/drawing_panel.cpp`; complete placement/rendering acceptance remains unverified |
| Angular dimension | Angle between two lines, arc dim line | **partial** | rpc `drawing_add_dim`; `core/doc/drawing_session.cpp` angular annotations; `host/src/ui/drawing_panel.cpp`; complete placement/rendering acceptance remains unverified |
| Ordinate (baseline) dims | Stacked distances from a baseline | **missing** | not in annotation system |
| Hole callout / smart annotation | Auto note from Hole Wizard data + leader | **missing** | no feature-data linkage |
| Free-text note with leader | Text anywhere + optional leader | **partial** | floating text placed; no leader geometry |
| Center mark | Crosshair at circular feature center | **partial** | `core/doc/drawing_session.cpp` center-mark annotations; `host/src/ui/drawing_panel.cpp`; full placement/rendering acceptance remains unverified |
| Centerline | Dash-dot line through cylindrical axis | **partial** | `core/doc/drawing_session.cpp` centerline annotations; `host/src/ui/drawing_panel.cpp`; full axis/linetype acceptance remains unverified |
| GD&T / Feature Control Frame | Symbol + tolerance + datum boxes | **missing** | not in _ANNOTATION_TYPES |
| Datum feature symbol | Triangle + letter for GD&T datums | **missing** | not implemented |
| Surface finish symbol | √ with Ra/Rz | **missing** | not implemented |
| Weld symbol | AWS/ISO weld symbol on a joint | **missing** | not implemented |
| Balloon / item bubble | Numbered circle + leader tied to BOM | **missing** | BOM data exists; no balloon type |
| BOM table on sheet | Structured table in sheet-mm coords, configurable | **partial** | screen-space overlay only (won't export); 3 fixed columns |
| Revision table | Rev/Description/Date/Approved history | **missing** | no revision concept |
| Hole table | Tabulated X/Y/dia/depth from a datum | **missing** | not implemented |
| Section hatch customization | Material-based angle/spacing/pattern | **missing** | hardcoded 45° @ 3mm |
| Show/hide tangent edges per view | Solid/phantom/hidden tangent edges | **partial** | always solid; no per-view Rg1 toggle |
| Show/hide hidden lines (HLR/HLV) | Per-view dashed-hidden toggle | done | add_view show_hidden; dashed rendering |
| Break view | Shorten long parts with break lines | **missing** | not implemented |
| View-to-view alignment locks | Projected view locked to parent axis | **missing** | views independently positioned |
| View drag-and-drop repositioning | Drag views on sheet, annotations follow | **missing** | no drag handler; set_view RPC only |
| Smart dim snap to geometry | Snap to endpoint/center/mid/tangent with magnet | **partial** | polyline-vertex snap only; no mid/center/tangent |
| Dim value override + prefix/suffix | REF/TYP prefix, ±tol suffix, custom text | **missing** | text overwrites whole value; no structured fields |
| Dual units on dims | mm and inch simultaneously | **missing** | one unit only |
| Tolerance annotation | Bilateral/limit/fit tolerances, ISO fits | **missing** | annotation schema has no tolerance fields |
| DXF export — drawing geometry | All views/layers incl. dims/text/hatch/title | **partial** | VISIBLE+HIDDEN polylines only; no dims/text/hatch/BOM/title; R12 |
| DXF export — native arcs/circles | ARC/CIRCLE entities for CAM | **missing** | all geometry discretized to polylines |
| PDF export — full output | Annotations + title block + BOM at sheet size | **partial** | visible+hidden polylines only; auto-fit to bbox (loses sheet size) |
| Flat pattern DXF export (sheet metal) | OUTLINE + BEND layers for cutting | done | export_flat_dxf; flat_pattern_layers; sm_export_flat_dxf |
| Drawing associativity | Views auto-update on source edit | done | _source_shapes live; HLR cache keyed on revision |
| Drawing persistence | Views/annotations/layout round-trip | done | serialize_drawing; register_restored_drawing |
| Make Drawing (4-view) | One-click Front/Top/Right/Iso on A3 | done | makeDrawing(); kStdViews; make_drawing cmd |
| Separate Drawing tab | Dedicated 2D canvas mode | done | DocTab::Drawing; drawDrawingSheet() |
| Section interactive cutting line | Two-click cut line drops a section view | done | sectionToolActive + commitSection |
| Detail interactive circle | Click center + drag radius | done | detailToolActive + commitDetail |
| Annotation selection + delete | Click selects, Delete removes | done | nearest-pos hit-test; deleteAnnotation |
| Annotation drag to reposition | Drag dim text, extension lines re-compute | **missing** | no drag handler; no update-annotation RPC |
| BOM column customization | Add/reorder part-no/desc/material/weight | **missing** | only item/name/qty/source |
| Print / plot output | Send sheet to printer/plotter | **missing** | DXF/PDF only |
| Drawing template creation | Save/reuse .drwdot template | **missing** | no template concept |

---

## 5. Sheet Metal

Base/edge flange, hem, jog and flat-pattern compilers are live in C++.
`core/geom/sheetmetal.cpp` develops bend strips; `core/doc/export2d.cpp` exports
OUTLINE/BEND and inner-wire CUTS. The dated engine push above is not proof that
the current C++ implementation preserves flange-wall cuts or generates corner relief.
Those acceptance claims are therefore no longer marked done solely from retired tests.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Base Flange / Tab | Closed-profile blank sets thickness/K/radius; reject open profile | done | compiler `sm_base_flange`; pm_form `sm_base_flange`; ribbon `sm_base_flange`; `core/geom/sheetmetal.cpp` (no separate Tab) |
| Edge Flange | Bent wall off edge: angle/length/radius/position/flip + bend record | done | compiler `sm_edge_flange`; pm_form `sm_edge_flange`; ribbon `sm_edge_flange`; `core/geom/sheetmetal.cpp` (single edge) |
| Miter Flange | Swept edge-chain with mitered corners | **missing** | not in schema/ribbon/PM (deferred in closeout) |
| Hem | Closed/open/teardrop fold + bend record | done | compiler `sm_hem`; pm_form `sm_hem`; ribbon `sm_hem`; `core/geom/sheetmetal.cpp` |
| Jog | Two opposing bends to a parallel offset | done | compiler `sm_jog`; pm_form `sm_jog`; ribbon `sm_jog`; `core/geom/sheetmetal.cpp` |
| Sketched Bend | Bend along a line on the broad face | **missing** | no sm_sketched_bend |
| Lofted Bend | Sheet lofted between two profiles | **missing** | no sm_lofted_bend |
| Flat Pattern / Unfold | BA-based developed length, bend lines, outline, area/vol | done | compiler `sm_flat_pattern`; ribbon `sm_flat_pattern`; `core/geom/sheetmetal.cpp` bend allowance development and bend_lines |
| Flat Pattern DXF/PDF Export | OUTLINE + BEND layers, requires Flatten | done | rpc `export_flat_dxf`; `core/doc/export2d.cpp` OUTLINE/BEND layers and vector PDF |
| Corner Treatments (Corner Relief) | Auto/round/rect/tear relief between adjacent flanges | **partial** | compiler `sm_edge_flange`; no relief generator found in current `core/geom/sheetmetal.cpp`; retired sheetmetal Python sources/tests do not establish this acceptance |
| Bend Relief (Auto Relief) | Notch at ends of partial-width flange | **missing** | no relief params/geometry |
| K-Factor + Bend Tables | K-factor + BA table + BD table + gauge table | **partial** | K-factor formula only; no BA/BD/gauge tables, no .btl import |
| Sheet Metal Gauge Tables | Material+gauge → thickness/radius/K lookup | **missing** | no gauge data/lookup |
| Convert to Sheet Metal | Solid → sheet metal via fixed face + rips | **missing** | no sm_convert |
| Tab Feature | Additive same-gauge bump on a sheet face | **missing** | no sm_tab |
| Forming Tool | Library punch (louver/dimple) stamp | **missing** | no forming library |
| Gusset (Sheet Metal) | Stiffening bead in a bend corner | **missing** | only weldment gusset exists |
| Flat Pattern in Drawing | Named flat-pattern view + bend annotations | **partial** | flat DXF standalone; not a drawing view; no bend notes |
| Sheet Metal Cut | Cut propagates to flat pattern (holes/slots/notches) | **partial** | compiler `sm_flat_pattern`; rpc `export_flat_dxf`; `core/geom/sheetmetal.cpp` re-extrudes broad face including base inner wires; `core/doc/export2d.cpp` emits CUTS; flange-wall cut development is not established by the current strip construction |
| Material/Thickness Propagation | Locked thickness inherited by all flanges | done | compiler `sm_base_flange`/`sm_edge_flange`; `core/doc/document.cpp` sheet context and bend records |
| Part-Level SM Properties Panel | Top-of-tree SM node (thickness/K/relief) + Folded/Unfolded | **missing** | context stored internally, not a tree node |
| SM Ribbon — Full SW Tab Parity | All ~18 SM commands | **partial** | 6 of 18+ commands (miter/tab/break-corner/sketched-bend/rip/unfold/fold… missing) |
| Unfold / Fold (individual bend) | Partial unfold of selected bends | **missing** | flat pattern unfolds all bends only |
| Rip | Slit a closed tube so it can develop flat | **missing** | no sm_rip |
| Insert Bends | Thin solid → sheet metal via face analysis | **missing** | no sm_insert_bends |
| Cross Break | X-pattern stiffening ridge | **missing** | not implemented |
| Break Corner | Fillet/chamfer flat-pattern corners (propagates to DXF) | **missing** | no sm_break_corner |

---

## 6. Weldments

Structural members, gussets, end caps and cut-list RPCs have live C++ evidence.
The profile catalogue and butt/cope claims in the 2026-06-14 update describe the
historical Python implementation; they are not current verification results.
Current profile and joint behavior is implemented in `core/geom/weldment.cpp` and
`core/doc/document.cpp`, with controls in `host/src/ui/pm_forms.cpp`.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Structural Member | Profile swept along sketch segments, one body per segment | done | compiler `weld_member`; pm_form `weld_member`; ribbon `weld_member`; `core/geom/weldment.cpp` and `core/doc/document.cpp` segment members |
| Profile library — standard sections | W/S/HSS/pipe/T/Z/angle/channel by designation | **partial** | compiler `weld_member`; `core/geom/weldment.cpp` profile construction; full designation catalogue acceptance is not established by the retired STANDARD_SIZES claim |
| Custom / user-defined profile | Import a sketch/DXF as cross-section | **missing** | hard-coded PROFILES dict; no profile_sketch_ref |
| Corner trim — mitre | Bisector cut at shared vertices | done | compiler `weld_member`; `core/geom/weldment.cpp` mitre trimming; `core/doc/document.cpp` member junction handling |
| Corner trim — butt/notch/cope/scribe | Trim/Extend joint types per member pair | **partial** | compiler `weld_member`; pm_form `weld_member`; `core/doc/document.cpp`; complete butt/notch/cope/scribe acceptance not established by the retired Python implementation |
| Gusset plate | Triangular/polygonal plate at an L-corner | done | compiler `weld_gusset`; pm_form `weld_gusset`; ribbon `weld_gusset`; `core/geom/weldment.cpp` |
| End cap / closure plate | Outer/inner plate on a hollow member end | done | compiler `weld_end_cap`; pm_form `weld_end_cap`; ribbon `weld_end_cap`; `core/geom/weldment.cpp` |
| Weld bead geometry | Cosmetic/solid bead along joint seam, in cut list | **missing** | no weld_bead type/compiler/UI |
| Cut list — basic grouping | Item/Profile/Description/Length/Qty/Total, live | done | rpc `weld_cut_list`; `core/doc/session.cpp` cut-list grouping; host CutListPanel |
| Cut list — mass/weight | Mass column from area×length×density | **missing** | rows have no mass/density (volume stored but not emitted) |
| Cut list — custom properties | Per-item Description/Material/Finish/Supplier | **missing** | fixed field set; no per-body props store |
| Cut list — Solid Bodies folder | Named per-member bodies, rename, exclude-from-cutlist | **missing** | weld_member is one tree node; no per-member access |
| Profile Locate — 9-point justification | 3×3 snap grid for cross-section anchor | **partial** | center + corner only (2 of 9) |
| Profile rotation + mirror | Angle spinner + H/V flip | **partial** | rotation done; no flip/mirror |
| Arc/curve segment members | Sweep arcs + trim at junctions | **partial** | arcs swept but butt at ends (not trimmed) |
| Trim/Extend tool | Interactive body trim against another body | **missing** | no weld_trim — T/X joints interpenetrate |
| Multiple path sketches per member | One feature spans multiple sketches | **missing** | single sketch per feature |
| Weldment Drawing — cut list table | Cut list table + balloons on drawing sheet | **missing** | cut list is a dockable panel only |
| Member material/appearance | Per-member material (mass) + color | **missing** | no material param; no Material column |
| Structural Member groups | Multiple profile groups in one feature | **missing** | single profile + single sketch |
| Weldment part document flag | Auto-flag part as weldment; Solid Bodies folder | **missing** | no weldment flag in doc schema |

---

## 7. Surfacing

The 2026-06-14 surfacing push and its test counts are historical. Current surf_*
types dispatch in `core/doc/document.cpp`; host forms are present in
`host/src/ui/pm_forms.cpp` and commands in `host/src/ui/ribbon_def.cpp`.
The inherited `partial` statuses remain conservative: current source presence is
not a fresh end-to-end acceptance test, especially for continuity and end conditions.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Surface Bodies folder / surface body concept | Open Shell/Face bodies coexist with solids; folder in tree | **partial** | `core/doc/document.cpp` body kind and surf_* shape construction; `host/src/ui/feature_tree.cpp` body grouping; complete surface-body display/folder acceptance remains unverified |
| Extruded Surface | Open/closed profile → open shell, all end conditions | **partial** | compiler `surf_extrude`; pm_form `surf_extrude`; ribbon `surf_extrude`; `core/doc/document.cpp`; full end-condition acceptance remains unverified |
| Revolved Surface | Open profile revolve → shell | **partial** | compiler `surf_revolve`; pm_form `surf_revolve`; ribbon `surf_revolve`; `core/doc/document.cpp`; end-to-end acceptance remains unverified |
| Swept Surface | Profile-along-path → shell + guide curves | **partial** | compiler `surf_sweep`; pm_form `surf_sweep`; ribbon `surf_sweep`; `core/doc/document.cpp`; complete guide behavior remains unverified |
| Lofted Surface | 2+ profiles → shell + tangency/guides | **partial** | compiler `surf_loft`; pm_form `surf_loft`; ribbon `surf_loft`; `core/doc/document.cpp`; full tangency/guide acceptance remains unverified |
| Boundary Surface | Two-direction curve net, G1/G2 continuity | **partial** | compiler `surf_boundary`; pm_form `surf_boundary`; ribbon `surf_boundary`; current form has dir1/dir2 wells, not continuity controls; full G1/G2 acceptance remains unverified |
| Planar Surface | Flat surface from a closed planar loop | **partial** | compiler `surf_planar`; pm_form `surf_planar`; ribbon `surf_planar`; `core/doc/document.cpp`; end-to-end acceptance remains unverified |
| Ruled Surface | Linear blend between two curves | **partial** | compiler `surf_ruled`; pm_form `surf_ruled`; ribbon `surf_ruled`; `core/doc/document.cpp`; end-to-end acceptance remains unverified |
| Offset Surface | Offset faces along normals | **partial** | compiler `surf_offset`; pm_form `surf_offset`; ribbon `surf_offset`; `core/doc/document.cpp`; end-to-end acceptance remains unverified |
| Fill Surface | Patch a bounded gap with G0/G1/G2 | **partial** | compiler `surf_fill`; pm_form `surf_fill`; ribbon `surf_fill`; current form has boundary well, not continuity controls; full G0/G1/G2 acceptance remains unverified |
| Trim Surface | Trim a surface with a tool, keep side | **partial** | compiler `surf_trim`; pm_form `surf_trim`; ribbon `surf_trim`; `core/doc/document.cpp`; keep-side acceptance remains unverified |
| Untrim Surface | Restore natural NURBS domain | **partial** | compiler `surf_untrim`; pm_form `surf_untrim`; ribbon `surf_untrim`; `core/doc/document.cpp`; natural-domain acceptance remains unverified |
| Knit Surface | Stitch surface bodies into shell/solid | **partial** | compiler `surf_knit`; pm_form `surf_knit`; ribbon `surf_knit`; `core/doc/document.cpp`; shell-to-solid acceptance remains unverified |
| Thicken Surface | Offset a surface into a solid | **partial** | compiler `surf_thicken`; pm_form `surf_thicken`; ribbon `surf_thicken`; `core/doc/document.cpp`; full thickening acceptance remains unverified |
| Replace Face | Swap solid faces with a surface | **partial** | compiler `surf_replace_face`; pm_form `surf_replace_face`; ribbon `surf_replace_face`; `core/doc/document.cpp`; topology/keep-side acceptance remains unverified |
| Delete Face | Remove faces, optional fill/patch | **partial** | compiler `surf_delete_face`; pm_form `surf_delete_face`; ribbon `surf_delete_face`; current form exposes face selection, not fill/patch; complete acceptance remains unverified |
| Extrude end: Up-To-Surface / Offset-From-Surface | Geometry-driven termination | **partial** | compiler `extrude`; pm_form `extrude` end/up_to/offset controls; `core/doc/document.cpp`; complete termination acceptance remains unverified |
| Extrude From: Surface/Face/Plane | Start offset from a surface | **partial** | compiler `extrude`; pm_form `extrude` start/start_ref/start_offset; `core/doc/document.cpp`; complete reference behavior remains unverified |
| Spline on Surface | 3D spline on a model face (UV-projected) | **missing** | sk_spline_surface greyed (deliberately deferred; requires 3D-sketch-on-face infrastructure) |

---

## 8. Configurations & Design Tables

The engine layer is production-quality: non-mutating compile overlay, cache keyed on
active config, full schema validation, six session ops, .step+ persistence, assembly
component suppression, and a Design Table UI with expression cells and a column-picker.
The main gap is the **Equations GUI dialog** (engine complete, no host dialog) plus
config-specific custom properties and config rename.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Named configurations | Add/Delete (last protected)/Activate, ≥1 always | done | rpc `config_add`/`config_delete`/`config_activate`; `core/doc/session.cpp`; host ConfigPanel |
| Config-specific param overrides | Independent per-config dim values, non-mutating | done | rpc `config_set_override`; `core/doc/session.cpp` copy/validate/commit; `core/doc/document.cpp` configuration overlay |
| Config-specific feature suppression | Suppress per config | done | rpc `config_set_suppression`; `core/doc/validate.cpp` validate_configurations |
| Compile cache keyed on active config | Round-trip to a prior config is a cache hit | done | `core/doc/document.cpp` CompileCache env/config/feature hash chain; cache-hit behavior retains the prior acceptance assessment |
| Config persistence in .step+ | Configs/overrides/suppressions/active pointer round-trip | done | rpc `doc_save`/`doc_open`; `core/doc/stepplus.cpp` document container persistence |
| Assembly-level config support | Component suppression + mate-value overrides | **partial** | component suppression done; per-config mate values deferred |
| Design Table — editable grid | Config rows × driven-param/suppress columns, live | done | design_table_panel.cpp full grid |
| Design Table — Add Column | Column picker enumerates live Dim fields | done | buildCandidates; pmFindForm enumeration |
| Design Table — expression cells | =expr stored in override.expr, re-evaluated | done | `host/src/ui/design_table_panel.cpp` expression editing; `core/doc/session.cpp` config_set_override; `core/doc/document.cpp` expression evaluation |
| Design Table — Add/Delete/Activate config | Toolbar buttons + double-click activate | done | Add/Delete/Activate buttons; copy_from seed |
| Equations — global variables | Named vars in dependency order + cycle detection | **partial** | engine complete (eq_set/eq_delete); **no host Equations dialog** — tree node is read-only |
| Equations — dim field expression entry | PM spinner accepts =expr, stored | done | validate_dim expr; compiler eval; pm_forms expr |
| Equations — dependency-aware rebuild | Change global → dependent dims recompute | done | C++ engine: eq env applied to dim.expr during rebuild; dependency-ordered eval |
| Equations — in-use guard | Block deleting a referenced equation | done | op `eq_delete`; `core/doc/session.cpp` equation deletion/reference handling; exact refusal behavior retains the prior assessment |
| Configuration rename | RMB rename, update all references | **missing** | no config_rename op/RPC/UI |
| Config-specific custom properties | Per-config Description/PartNo/Revision for BOM | **missing** | no properties field in schema |
| Display States | Appearance-only variants (color/hide) | **missing** | no display_state concept |
| Configure Feature context menu | Jump to config panel filtered to a feature | **missing** | no such menu entry |
| Per-config sketch suppression | Suppress individual sketch dims per config | **missing** | suppressions accept feature ids only |
| Equations — external spreadsheet link | Link Design Table to Excel | **missing** | no spreadsheet linkage |
| Equations — sensor-driven variables | Sensor value as a global variable | **missing** | no sensor concept |
| Config-panel override display | Compact per-config override/suppression view | done | ConfigPanel selected-row expansion |
| Config list reflects undo/redo | Panel refreshes on any edit | done | auto-invalidate on docId/revision change |
| Schema validation for configs | Reject dup names / unknown refs / bad active | done | rpc `doc_validate`; `core/doc/validate.cpp` validate_configurations; no current test count asserted |

---

## 9. Evaluate & Analysis

Geometric analysis is implemented in `core/doc/session.cpp`, with controls and overlays
in `host/src/ui/evaluate_dialogs.cpp`. Native FEA and flow RPCs dispatch through
`core/api/libchudware.cpp`; study controls live in `host/src/ui/sim_manager.cpp`.
The historical tet4-only/hardcoded-density description is obsolete. Existing solver
and UI paths below are evidence of implementation, not proof of every premium-study,
mesh-quality, probe or coordinate-system acceptance condition.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| Mass Properties | Volume/mass/CoM/inertia + density override + principal axes + CS | **partial** | rpc `eval_mass`; `core/doc/session.cpp` principal axes/moments; `host/src/ui/evaluate_dialogs.cpp` material/density override and IPS formatting; arbitrary CS acceptance remains unverified |
| Measure | Length/radius/distance/angle/area/vol, accumulate, delta-XYZ | **partial** | rpc `eval_measure`; `host/src/ui/evaluate_dialogs.cpp` diameter and recent-reading list; full delta-XYZ/arc-center/accumulation acceptance remains unverified |
| Section Properties | Pick plane → area/centroid/2nd-moments in a chosen CS | **partial** | rpc `eval_section_props`; `host/src/ui/evaluate_dialogs.cpp` three orthogonal draggable section planes; arbitrary datum/face/chosen-CS acceptance remains unverified |
| Interference Detection | Pairwise overlap volume + clearance, highlight, options | **partial** | rpc `eval_interference`; `host/src/ui/evaluate_dialogs.cpp` interference inventory and overlays; complete clearance/coincident/options acceptance remains unverified |
| Geometry Check | BRepCheck + free/short/sliver census | done | rpc `eval_check`; `core/doc/session.cpp`; color-coded host list |
| Draft Analysis | Pull direction + threshold color map | done | rpc `eval_draft`; `host/src/ui/evaluate_dialogs.cpp` Pull X/Y/Z/-Z and angle controls |
| Thickness Analysis | Per-vertex wall thickness color map | done | rpc `eval_thickness`; `core/doc/session.cpp`; host field overlay |
| Curvature Analysis | Mean/Gaussian/max curvature color map | done | rpc `eval_curvature`; `host/src/ui/evaluate_dialogs.cpp` mean/Gaussian/max/min controls |
| Zebra Stripes | Reflected stripes for G1/G2 inspection | done | setZebra; GLSL zebra (W23c) |
| Symmetry Check | (SW has none) mirror-and-diff symmetry test | **beyond** | rpc `eval_symmetry`; `core/doc/session.cpp`; `host/src/ui/evaluate_dialogs.cpp` Front/Top/Right plane picker |
| Dynamic Section View | Up to 3 planes, drag offset, hatch, save view | **partial** | `host/src/ui/section_tool.cpp` SectionTool; full multi-plane/drag/hatch/saved-view acceptance remains unverified |
| Linear Static FEA | Material/fixtures/loads/mesh + von-Mises/principal/disp/strain/FoS/reactions/probe | **partial** | rpc `sim_run_static`/`sim_run_adaptive`; `core/sim/fea_static.cpp`; tet4/tet10, material and reaction paths exist; full picked-node probe/UI acceptance remains unverified |
| Modal / Frequency | Fixtures + N modes → frequencies + animated mode shapes | **partial** | rpc `sim_run_modal`; `core/sim/fea_modal.cpp`; `host/src/ui/sim_manager.cpp`; full animation/export acceptance remains unverified |
| Thermal/Nonlinear/Fatigue/Buckling/Drop/PV | Premium study types | **partial** | rpc `sim_run_thermal`/`sim_run_fatigue`/`sim_run_buckling`; native `core/sim/fea_thermal.cpp`, `core/sim/fea_fatigue.cpp`, `core/sim/fea_buckling.cpp`; full nonlinear/drop/PV acceptance not established |
| FloXpress / Flow Simulation | External/internal CFD: velocity/pressure/temp/cut-plots | **partial** | rpc `sim_run_flow`; `core/sim/flow_run.cpp`; complete internal/thermal/cut-plot acceptance remains unverified |
| Assembly-level Mass Properties | Aggregate total mass/CoM/inertia of all components | **partial** | rpc `eval_mass`; `core/doc/session.cpp` gathers world-placed assembly export shapes; complete aggregation/material acceptance remains unverified |
| Smart Sensors | Watched value + threshold alert in tree | **missing** | no sensor concept |
| Design Study / Optimization | Parametric sweep/optimizer over sensors | **missing** | no framework |
| SimulationXpress | Single material/fixture/load static wizard, vM+disp | done | rpc `sim_run_static`; host static-study form matches the inherited SimulationXpress scope |
| FEA Custom Material Entry | Type custom E/nu/density/yield | **missing** | `host/src/ui/sim_manager.cpp` addCustomMaterial persists custom materials; complete typed-property entry UI acceptance remains unverified rather than inferred from storage alone |
| FEA Result Probe / List | Probe stress/disp at a picked node | **missing** | no probe interaction/RPC |
| FEA Reaction Forces | Reaction force per fixture + balance | **partial** | rpc `sim_run_static`; `core/sim/fea_static.h` support force/moment result fields and equilibrium_error_pct; complete per-fixture reporting/UI acceptance remains unverified |
| FEA Mesh Quality (Jacobian) | Jacobian-ratio check, highlight bad elements | **partial** | rpc `sim_mesh`; `core/sim/mesh_quality.cpp` and `core/sim/mesher.h` quality metrics; bad-element highlight acceptance remains unverified |
| FEA Local Mesh Controls | Per-face finer mesh | **partial** | rpc `sim_mesh`; `core/sim/mesher.h` MeshLocalRefine size-contract delivery; complete per-face GUI editing acceptance remains unverified |
| Mass Props Density UI / Material | Material field + override checkbox, doc material | **partial** | op `set_material`; rpc `eval_mass`; `host/src/ui/evaluate_dialogs.cpp` material picker and custom density override; exact checkbox interaction acceptance remains unverified |
| Measure — accumulate | Scrollable list of measurements + totals | **partial** | rpc `eval_measure`; `host/src/ui/evaluate_dialogs.cpp` recent readings and total_length/total_distance displays; totals across retained history not established |
| Section Props — plane picker | Pick any datum/face/section line | **partial** | rpc `eval_section_props`; `host/src/ui/evaluate_dialogs.cpp` orthogonal offset/drag controls; arbitrary datum/face/line picking acceptance remains unverified |

---

## 10. Import / Export / Interop

Neutral-format round-trips work at the engine layer (STEP/IGES/BREP/STL/3MF) with
mtime-keyed caching, the .step+ native container is robust, and 2D output (DXF/PDF, flat
DXF) plus ECAD interop (KiCad import, Gerber/BOM, board-to-STEP) are complete. The 51%
reflects that **geometry export has no UI** (File > Export items are stub-disabled), no
Parasolid, and no color/unit/assembly-hierarchy round-trip on STEP.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| STEP export (AP214) | Neutral export, assembly hierarchy preserved | **partial** | export_step works at RPC; File>Export>STEP disabled; assembly flattened to Compound |
| STEP import | Import bodies with product names + diagnostics | **partial** | _import_step works; Explorer routes to stepplus.open (wrong path); no diagnostics |
| IGES export | Legacy IGES output | **partial** | engine works; no UI/menu |
| IGES import | Sew + heal face-soup | **partial** | _import_iges sews+fixes; no GUI path; hardcoded tolerance |
| STL export | Mesh export, binary/ASCII, tolerance, count | **partial** | export_stl no quality params; File>Export>STL disabled |
| STL import | Mesh body, convert-to-solid | **partial** | imports as surface Face (no volume); no mesh→solid |
| 3MF export | Mesh + name + color for AM | **partial** | Mesher used; no color/quality; File>Export>3MF disabled |
| BREP export/import | OCC-native round-trip | done | rpc `export_file`/`import_file`; `core/doc/formats.cpp` BREP import/export |
| Native .step+ container | Atomic save/open, thumbnail, drawings, fallback | done | rpc `doc_save`/`doc_open`; `core/doc/stepplus.cpp`; native Save/Open dialogs |
| Frozen-geometry fallback | All-features-fail → embedded geometry as import | done | rpc `doc_open`; `core/doc/session.cpp` native container restoration; compiler `import`; inherited all-failure fallback acceptance is not proved by artifact presence alone |
| DXF export (drawing sheets) | Multi-layer 2D output | done | rpc `export_drawing`; `core/doc/export2d.cpp` and `core/doc/dxf.cpp`; Save As DXF dialog |
| DXF export (sheet-metal flat) | OUTLINE + BEND layers | done | rpc `export_flat_dxf`; `core/doc/export2d.cpp` OUTLINE/BEND layers |
| PDF export (drawing/flat) | Printable vector PDF | done | rpc `export_drawing`/`export_flat_dxf`; `core/doc/pdfvec.cpp`; format choice in panel |
| DWG export/import | AutoCAD binary format | done | rpc `export_file`/`import_file` + `export_drawing`/`export_flat_dxf` format `dwg`: native AutoCAD R2000 writer (`core/doc/dwg_write.cpp`) with solids as ACIS 3DSOLID/BODY; ACIS solids/surfaces of every version import exactly (R2013+ data storage via `core/doc/dwg_native.cpp`, `core/doc/acis_read.cpp`); 2D entities via subprocess LibreDWG `dwg2dxf` (`core/doc/dwg.cpp`, provisioned by `scripts/provision_libredwg.py`); not yet opened in AutoCAD itself (validated with ODA File Converter + re-read) |
| Parasolid (x_t/x_b) import | Native Siemens kernel format | **missing** | no Parasolid bridge (most-requested SW exchange format) |
| ACIS/SAT import/export | Inventor/Fusion kernel format | done | rpc `import_file`/`export_file` format `sat` (.sat/.sab in, ACIS 7.0 out); `core/doc/acis_read.cpp`, `core/doc/acis_write.cpp` |
| STEP AP242 + PMI/GD&T | MBD tolerances/datums in STEP | **missing** | only AP214; no PMI model |
| STEP color/appearance round-trip | Per-body RGB embedded/read | **missing** | bare export_step; no XCAF color |
| STEP assembly hierarchy export | Product/instance tree in STEP | **missing** | all bodies collapsed to one Compound |
| Import diagnostics | Auto geometry check + highlight on import | **partial** | eval_check exists but not auto-run on import_file |
| Geometry healing on import | Heal pipeline + report | **partial** | IGES auto-heals silently; STEP doesn't; no user control/report |
| Unit detection on import | Detect mm/inch, convert/prompt | **missing** | no unit inspection (25.4× scale risk) |
| Import into current document | Insert part with placement | **partial** | RPC 'into' works; no UI path; no placement transform |
| Export selected bodies only | Body filter on export | **missing** | always all bodies |
| Save a copy as | Export without changing session path | **missing** | save always updates path |
| Pack and Go | Self-contained archive with references | **partial** | .step+ embeds drawings/components; import source_path absolute (breaks off-machine) |
| ECAD native (.ecad+) | ZIP container save/open | done | rpc `ecad_save`/`ecad_open`; `core/ecad/container.cpp`; native ECAD Save/Open dialogs |
| KiCad .kicad_sch import | KiCad 6/7/8 schematic parse | done | import_kicad_sch; file filter |
| KiCad .kicad_pcb import | KiCad 6/7/8 board parse | done | import_kicad_pcb; ecad_open |
| Gerber / drill export | RS-274X + Excellon | done | ecad_export gerber/drill |
| BOM export (ECAD) | Reference/value/footprint/qty, DNP-filtered | done | ecad_export bom |
| Board to 3D (STEP) | PCB substrate + components → STEP into CAD | done | ecadTo3d; board_to_step; import_file |
| Assembly BOM (drawing) | Item/part-no/qty/desc table from assembly | **partial** | drawing_bom data exists; no sheet table UI; no Excel export |
| STL tessellation quality control | Deviation/angle sliders + preview | **missing** | defaults only; no UI |
| File > Export menu wired | Save dialog + format options per format | **missing** | all 4 Export items stub-disabled (largest export UI gap) |

---

## 11. UI / UX / Interaction

The SW-equivalent shell is substantially complete: the FeatureManager tree (suppress,
rollback, drag-reorder, rename, multi-select, assembly tree, appearance), the data-driven
PropertyManager with live preview, view manipulation (orbit/pan/zoom, presets, view cube,
normal-to), all three Instant3D drag modes, the 'S' bar, heads-up toolbar, mouse-gesture
wheel, toast notifications, units toggle, multi-doc tabs, and native dialogs. Gaps cluster
in selection (no 3D box/lasso/filter/tangent-chain), copy/paste, the PM two-column layout,
and high-DPI ribbon arithmetic.

| Capability | SW acceptance condition | Status | Evidence |
|---|---|---|---|
| FeatureManager — core structure | Root + fixed headers + history + icons | done | feature_tree.cpp header nodes, planes, Origin, featureIcon() |
| FeatureManager — suppress/unsuppress | Single + multi-select, dimmed [S] | done | set_suppressed single+multi; [S] + dim alpha |
| FeatureManager — inline rename | Slow-double-click editor, Enter/Esc | done | s_lastClickId arm; InputText commit/cancel |
| FeatureManager — drag-reorder | Drop indicator + dependency guard | done | FEATURE_ROW payload; dependency_exists toast |
| FeatureManager — rollback bar | Drag bar + Rollback-to-Here/Roll-to-End | done | drawRollbackBarLine; rollback ops; rolled-back marker |
| FeatureManager — folder grouping | Folders + Name&Group | **partial** | op `set_folder`; `host/src/ui/feature_tree.cpp` Features-header Add Folder groups selection or creates a session-only empty drop target; Name&Group is removed, so the combined acceptance is not done |
| FeatureManager — Ctrl+click multi + reorder | Toggle select + Ctrl+Up/Down | done | s_multiSel; ctrlClicked; Ctrl+Up/Down |
| FeatureManager — Shift+click range-select | Contiguous range select | **missing** | only plain + Ctrl click |
| FeatureManager — search/filter | Live name filter | **missing** | no search widget |
| FeatureManager — Properties/Config tabs | Three-tab rail switches left pane | **partial** | tab strip drawn; Properties/Config tabs non-functional stubs |
| FeatureManager — assembly tree | Components (f) + Mates folder + context menus | done | drawAssemblyTree |
| PropertyManager — data-driven forms | Grouped Combo/Dim/Check/Well, OK/Cancel | done | pm_forms.cpp all feature types; property_manager.cpp |
| PropertyManager — OK/Cancel + shortcuts | Check/X top-right, Enter/Esc | **partial** | icons fixed; layout inline not top-right; Enter=OK unconfirmed |
| PropertyManager — two-column grid | Label column aligned with input column | **missing** | all fields full-width stacked |
| PropertyManager — live preview | Per-keystroke debounced rebuild preview | done | debounced preview; checkpoint/revert envelope |
| PropertyManager — extrude end conditions | Blind/Mid/Through/Up-To-Next (+Surface/Body) | **partial** | 4 of 6 (no Up-To-Surface/Body) |
| PropertyManager — fillet tangent prop | Tangent propagation checkbox | **missing** | CheckSoon "coming soon", never emitted |
| PropertyManager — draft angle unit | [deg] on the draft field | **partial** | bug: Dim() shows [mm] on angle field |
| Selection — single-entity click + hover | Hover highlight, click select, Ctrl toggle, Esc clear | done | id-pass hover; Ctrl toggle; Esc clear |
| Selection — tangent-chain propagation | Loop/tangent select for fillet/chamfer | **missing** | no chain/loop/tangent select |
| Selection — 3D box select | Rubber-band window/crossing select | **missing** | only sketch has 2D box select |
| Selection — 3D lasso select | Freeform closed-curve select | **missing** | not implemented |
| Selection — selection filter | Global face/edge/vertex/body filter bar | **missing** | per-PM-well filtering only |
| Mouse gestures — RMB drag wheel | 8-segment radial view wheel | done | mouse_gestures.cpp kSegs |
| Mouse gestures — orbit | LMB/MMB orbit, Ctrl pan, Shift zoom | done | viewport_panel orbit/pan/zoom |
| Mouse gestures — scroll zoom to cursor | Wheel zooms toward cursor point | done | cursor-tracking pan compensation |
| Mouse gestures — double-MMB zoom-to-fit | Reset view to fit | done | double-click MMB fit |
| View — view cube | Click face/edge/corner to jump | done | view_selector.cpp truncated box (Space-activated) |
| View — standard presets + shortcuts | Ctrl+1–7 named views, animated | done | applyViewPreset; camera_anim |
| View — Zoom to Fit/Selection | F / Shift+F | done | ZoomFit; zoomToSelection |
| View — Normal To | Ctrl+8 + flip + sketch plane | done | normalToSelectionOrNearestAxis |
| View — zoom area | Rubber-band zoom | done | zoom-area drag; HUD [1] |
| View — display style | Shaded/Shaded+Edges/HLR/HLV/Wireframe | done | 5-mode dropdown |
| View — section view | Real-time cross-section, movable plane | **partial** | HUD toggle + SectionTool; interactive plane drag unverified |
| Heads-up toolbar — navigation bar | Floating nav bar above viewport | **partial** | 8-button pill; no Previous View / 3D Drawing View |
| Heads-up — 'S' shortcut bar | Context-aware quick commands | done | shortcut_bar.cpp 6 cmds/mode |
| Heads-up — customizable 'S' bar | Drag commands in/out | **missing** | hardcoded arrays |
| Command search | Search box jumps to commands | **missing** | no widget |
| Keyboard — standard shortcuts | F/Ctrl+1-8/S/Space/Ctrl+Z/Y/Del/Esc | done | viewport_panel + app.cpp + shortcut_bar |
| Keyboard — customizable bindings | Tools>Customize>Keyboard | **missing** | hardcoded |
| Rollback bar — keyboard Ctrl+Up/Down | Step rollback bar | **partial** | Ctrl+Up/Down reorders features, not rollback bar |
| Instant3D — face drag | Pull arrow drives feature dim, 1 undo, Esc cancel | done | PushPullDrag; checkpoint/revert/commit |
| Instant3D — dim handle drag | Draggable dim chips drive params, expr-protected | done | DimDrag; up to 4 handles |
| Instant3D — assembly component drag | Drag under-constrained comp, mates hold | done | AsmDrag; doc_solve_mates; verified |
| Copy / Paste features | Ctrl+C/V feature copy + cross-part | **missing** | menu items disabled; no engine op |
| Document tabs — multi-doc + dirty | Named tabs, '*' dirty, close prompt | done | doc_tabs.cpp |
| Units toggle — MMGS / IPS | Status-bar click reformats live | done | status_bar units token; persisted |
| Configurations manager | List/switch/add/delete | done | config_panel + design_table_panel |
| Themes — dark/light runtime | Switch without restart | done | applyThemeNow (DPI bug fixed) |
| DPI / HiDPI scaling | Clean at 100–200% + cross-monitor | **partial** | foundation sound; ribbon width bugs ≥2×, no cross-monitor handler |
| Right-click viewport menu | View orient/zoom/appearance/clear | done | viewport context menu |
| Sketch — plane lock + normal-to | Camera locked to sketch plane | done | cameraLock; Ctrl+8 sketch flip |
| Sketch — status line | Under/Fully/Over + DOF, color-coded | done | statusLine() role-colored |
| Sketch — inference snapping | Mid/rim/tangent/H-V inference | done | snapPoint prefixes; C49 tolerances |
| Sketch — box select | 2D rubber-band entity select | **partial** | exists; window-vs-crossing unclear |
| Explorer pane / file browser | Workspace CAD-file browser | done | drawExplorer() |
| Save / Open native dialogs | OS file pickers, Save-As on Shift | done | doSaveDoc/doOpenDoc |
| Evaluate — Measure | Distance/angle/radius readout | done | eval_measure; async |
| Evaluate — Mass Properties | Mass/volume/CG/inertia | done | eval_mass_props |
| Evaluate — Interference | Assembly clash detection | done | eval_interference |
| Appearance — color/metallic/roughness | RMB Appearance picker, live | done | appearanceColorPicker shared |
| PropertyManager — Coming Soon fields | (SW lacks) greyed not-yet fields for discoverability | **beyond** | CheckSoon/DimSoon |
| Ribbon — multiple tabs | Context tabs + split buttons | done | 8 tabs; split boss/cut |
| Ribbon — overflow at high DPI | Collapse to icons → chevron menu | **missing** | tab strip runs off-screen ≥2× |
| Equations dialog | Global-variable editor | **partial** | tree shows count; no editor dialog |
| Design Table — spreadsheet driver | Config×param grid | **partial** | native ImGui grid (not Excel embed) |
| Task pane — Design Library | Drag standard parts in | **missing** | not implemented |
| PropertyManager — selector well chips | Named chips + X-remove | **partial** | single-line button; long names truncate |
| View history — Previous/Next View | Step through camera history | **missing** | no history stack |
| Perspective / ortho toggle | HUD projection toggle | **missing** | perspective only |
| Ground shadow / AO toggles | Shadow/AO/reflection quality | **partial** | ground shadow + RealView; no AO |
| Toast notifications | (SW uses status bar) leveled popups | **beyond** | toast.cpp Info/Warn/Error |

---

## 12. ECAD / Electronics (beyond-SW differentiator)

SolidWorks has no native PCB capability, so **every item here is beyond-SW by definition**;
parity is measured against a KiCad-class baseline. The headless engine (schematic capture,
hierarchical sheets, netlist, ERC, PCB layout, DRC, zone fill, Gerber/BOM/drill, PCB→3D
bridge, native MNA SPICE) is done and verified across 27 test suites; the C++ GUI is built
and headlessly verified through Wave Z. The AI design orchestrator and 24-tool MCP API are
genuine differentiators KiCad lacks. Gaps: KiCad write-back export, cross-probe, variants.

| Capability | KiCad-class acceptance condition | Status | Evidence |
|---|---|---|---|
| Schematic Document Model | Symbols/wires/junctions/labels/buses/power/hier, version-locked | done | rpc `ecad_schema`; `core/ecad/schema.cpp` aicad.sch/1 validation |
| PCB Document Model | Stackup/outline/footprints/tracks/vias/zones/rules/nets, version-locked | done | rpc `ecad_schema`; `core/ecad/schema.cpp` aicad.pcb/1 validation |
| Schematic Capture | Place/wire/junction/label/power/no-connect/annotate, undo | done | op `sch_add_symbol`/`sch_add_wire`/`sch_add_junction`/`sch_add_label`/`sch_add_power`/`sch_add_no_connect`/`sch_annotate`; rpc `ecad_undo`; `core/ecad/ops.cpp` |
| Hierarchical Multi-Sheet | Sheet symbols + pins + hier bridge + ERC | done | sheets/sheet_symbols; netlist hierarchy bridge; erc |
| Netlist Extraction | Deterministic union-find, name priority, byte-stable | done | `core/ecad/netlist.cpp` extraction; byte stability remains a behavioral acceptance, not a file-presence check |
| Electrical Rules Check (ERC) | Full KiCad-class rule set, advisory | done | `core/ecad/erc.cpp` electrical rules; unchanged acceptance assessment, not proved by artifact presence alone |
| Symbol Library — KiCad Import | Parse .kicad_sym, multi-unit, body graphics | done | rpc `ecad_load_symbol_library`; `core/ecad/symlib.cpp` KiCad symbol parsing |
| Symbol Library — Clean-Room Starter | Built-in R/L/C/diode/…/IC with IPC-7351 | done | `core/ecad/library.cpp` starter library; inherited count/layout acceptance is not a fresh test result |
| Footprint Library — KiCad Import | Parse .kicad_mod / .pretty | done | `core/ecad/kicad.cpp` footprint parsing |
| Schematic→PCB Update | Idempotent footprint+net population, prune | done | op `pcb_update_from_sch`; rpc `ecad_update_from_sch`; `core/ecad/sync.cpp` |
| PCB — Footprint Placement | Place/rotate/flip/ref-value, grid snap | done | pcb_add/edit/delete_footprint; canvas tools |
| PCB — Manual Track Routing | Segment+arc, layer change via via, net widths | done | pcb_add_track/via; Route tool |
| PCB Autorouter | Maze/Lee + net classes + rip-up + diff-pair | done | rpc `ecad_autoroute`; `core/ecad/autoroute.cpp`; complete routing acceptance is not established by the RPC pointer alone |
| Copper Zone Fill | Flood + clearance + thermal relief + island removal | done | rpc `ecad_fill_zones`; `core/ecad/zonefill.cpp` |
| Design Rule Check (DRC) | Net-aware clearance + width/via/ring/hole/edge/unrouted | done | `core/ecad/drc.cpp`; full rule acceptance retains the prior assessment |
| Design for Manufacturing (DFM) | Silk/mask/acid-trap/courtyard/annular/drill | done | rpc `ecad_dfm`; `core/ecad/dfm.cpp` |
| Gerber / Fab Output | RS-274X + Excellon + BOM + CPL | done | gerber/excellon/bom/placement |
| PCB→3D STEP + CAD Bridge | Board substrate+components → STEP into CAD part | done | rpc `ecad_to_3d`; `core/api/libchudware.cpp` native board export and document import |
| Native SPICE — DC/Transient/AC | MNA solver, Newton, no external dep | done | rpc `sim_spice`; `core/sim/spice.cpp`; historical analytic gate count is not a current test result |
| Extended Simulation Analyses | Fourier/THD, Monte-Carlo, temp sweep, noise, .tf, .step | done | rpc `sim_spice`; `core/sim/spice.cpp`; complete analysis matrix retains the prior assessment |
| SPICE Netlist Import / Export | Round-trip R/L/C/V/I/D/BJT/MOS/OPAMP/.subckt | done | rpc `sim_netlist_parse`/`sim_netlist_to_circuit`; `core/sim/netlist.cpp`; full round-trip acceptance retains the prior assessment |
| Signal Integrity — Controlled Impedance | Microstrip/stripline/CPWG/diff-pair Z0 from stackup | done | rpc `ecad_impedance`; `core/ecad/impedance.cpp` |
| KiCad File Interoperability | Import + export .kicad_sch/.kicad_pcb | **partial** | import done; export (kicad_export) raises not_available |
| Native Container (.ecad+) | ZIP container, forward-compatible | done | rpc `ecad_save`/`ecad_open`; `core/ecad/container.cpp` |
| PCB Stackup Definition | Copper layers + dielectric + thickness/Er | done | schema stackup; pcb_set_stackup |
| Session Management | 100-deep undo/redo, transactional ops, multi-doc | done | rpc `ecad_apply_ops`/`ecad_undo`/`ecad_redo`/`ecad_new`; `core/ecad/session.cpp` |
| AI Schematic Design Orchestrator | NL → ERC-clean, sim-checked schematic | **beyond** | rpc `ecad_design`; `core/ecad/session.cpp`; orchestrated ERC/simulation acceptance is not implied by RPC presence |
| One-Click AI Board | NL → sch → PCB → route → 3D in one call | **beyond** | ecad_make_board; AI Board button |
| MCP / AI Tool API | 24 tools with structured state + next_actions | **beyond** | `services/mcp/ecad_tools.py` tool context; the historical tool count is not a current catalog measurement |
| PCB Net Classes / Design Rules | Per-class clearance/width/via, DRC+router aware | done | net_classes; pcb_set_rules |
| Library Search / Part Finder | Keyword search → ranked libids | done | library search(); ecad_find_part |
| Ratsnest Visualization | MST airwires per net, live | done | `core/ecad/drc.cpp` ratsnest extraction; host ECAD canvas |
| DNP Flag | Exclude from BOM/paste/CPL | done | dnp field; BOM/placement filtering |
| Zoom-to-Violation | Click ERC/DRC row → pan+zoom+flash | done | EcadCanvas::focusOn (Wave Z) |
| Simulation GUI — Waveform Plot | Run + plot V/I (transient) / Bode (AC) | **partial** | plotting done; click-wire-to-probe not in GUI |
| Board / Assembly Variants | Named variants, per-variant BOM/fab | **missing** | only single DNP bit |
| Bus Routing (Schematic) | Bus wire + bus-entry taps | **partial** | bus parsed; entry-tap objects + bus union-find missing |
| Blind/Buried/Micro Via (HDI) | Route with all via types | **partial** | schema supports all; autorouter places through-only |
| EMI/EMC Checks | Return-path/loop-area/decoupling advisor | **missing** | not implemented (beyond KiCad) |
| Power Integrity / IR Drop | Power-net resistance + voltage drop | **missing** | not implemented |
| Thermal Simulation of PCB | Component temperatures from power map | **missing** | temp sweep is device-param only |
| IBIS / S-Parameter | High-speed IO buffer / Touchstone models | **missing** | not implemented |
| KiCad Schematic Export | Write .kicad_sch back to KiCad | **missing** | kicad_export not present (import-only) |
| Diff-Pair Length Match (manual) | Tune existing routed pair to match | **partial** | available during autoroute only; no post-hoc op |
| Schematic Cross-Probe to PCB | Select in one view highlights the other | **missing** | no highlight sync |
| Component Rotation / Assembly Rules | Reflow-direction / edge-straddle checks | **missing** | not implemented |
| Netlist Export — Standard Formats | KiCad/OrCAD/PADS netlist | **partial** | JSON + SPICE done; standard EDA formats missing |

---

## Maintenance

This standard is maintained in this file. When a capability lands, update its row with
concrete live evidence. Use `scripts/parity_audit.py` for current status totals and
pointer coverage; retain the dated scorecard above as history.
A capability graduates to `done` only when its evidence is concrete and its full SW
acceptance condition is met — partial credit stays `partial`.
