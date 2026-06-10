# Landed-card integration notes (orchestrator log)

Terse API facts future dispatch prompts need. Updated as cards land. Full reports live in the
session transcripts; commit messages carry verification evidence.

## C01 compiler (committed 9024c8d)
- `from engine.doc.compiler import compile_document, CompileCache, BodyOut, CompileResult`
- Register features: `from engine.doc.features.base import register, CompileContext,
  CompileOutput, FeatureError`; append `from . import <mod>` to `engine/doc/features/__init__.py`
  (designated append-only). `ctx.dim(params,key,default)`, `ctx.datum(ref)`,
  return `CompileOutput(bodies=..., tool_body=...)`.
- `CompileResult`: bodies (BodyOut: body_id,name,shape,color), feature_status
  ({id,ok,ms,error,resolved,skipped,dangling_refs}), face_owners/edge_owners
  ({body_id: {1-based idx: feature_id}}, order = build123d .faces()/.edges()),
  tool_bodies, equations_env, cache_hits/misses. Never raises.
- engine/doc/__init__ stays OCP-free: import compiler/tessellate/selectors modules DIRECTLY.

## C02 tessellate (6fa0cad)
- `engine.doc.tessellate`: tessellate_body(...)->BodyTess; write_aicm(bodies, extra_chunks=[]);
  write_aicm_file; read_aicm(data)->dict(bodies: pos(n,3)f32, idx(n,3)u32, faces[{face_id,
  first_tri,tri_count,surf_type}], edges, edge_pts, verts); scalar_chunk(body_id,name,values,
  vmin,vmax)->bytes; disp_chunk(body_id,scale_hint,vectors); lines_chunk(name,polylines,scalars).
- OCP 7.8: no lowercase `topods` module — use TopoDS.Face_s/Edge_s/Vertex_s.

## C03 selectors (fa16389)
- `engine.doc.selectors`: TopoInfo, ResolveResult, make_selector, resolve, resolve_region,
  describe_faces/edges/vertices(shape, owners). owners keyed by id(ocp_obj) — valid ONLY in the
  same scope; build from compiler's index-keyed owners by enumerating .faces()/.edges() in-scope.
- dangling_refs string format: "<refslot>[i]: {reason}".

## C04 split (f3fac91)
- AppState aggregate: host/src/core/app_state.{h,cpp} (addLog/startSetup/tryStartWorker/
  showPartMesh methods). Panels: host/src/ui/*. Frame loop + AI_CAD_SHOT modes: host/src/app.cpp.
  Shared draw helpers defined in legacy_chat.cpp, declared in app_state.h.
- GOTCHA: never add host/src to include path (math.h shadows CRT <math.h> on MSVC).
- Exe: <BUILD_DIR>/bin/Release/AI-CAD.exe.

## C05 icons (b8efce1)
- host/assets/icons/{atlas_dark.png, atlas_light.png, atlas.json}; 123 names; single 56×4182
  vertical strip; key "name@32"/"name@20" -> {x,y,w,h}; theme switch = swap texture, UVs constant.

## C08 FEA (9391071)
- engine/sim: mesh_step(step_path,density)->MeshResult; run_static(mesh,material,fixtures,
  loads,on_progress)->StaticResult (npz save); run_modal(...); sim worker = `python -m
  engine.sim.worker` (ping {pong,protocol:2,sim:true}; sim_mesh/sim_run_static/sim_run_modal/
  sim_run_flow/sim_cancel/shutdown; sim_progress stages mesh/assemble/solve/post).
- Study JSON accepts mesh_path OR step_path (panel should cache mesh_path).
- tet4 ~15% stiff in bending; P2 upgrade path = skfem ElementTetP2 (later card).

## C06 nsolve (committed with this note)
- `engine.doc.nsolve`: System(); add_vars(n, init)->int64 indices; add_residual(rfunc, var_idx,
  jfunc, tag, weight=1.0, soft=False) — one block PER CONSTRAINT tagged with its constraint id;
  shapes probed at registration. solve(x0, tol, max_iter, time_budget_ms)->Result{x, converged,
  iterations, max_residual} (warm start = pass previous x; budget returns best-so-far).
  analyze(x)->Analysis{dof, redundant_tags (consistent dependents = warn), conflicting_tags
  (inconsistent = over-defined red), well_posed, rank}. dof>0 under; dof==0 & well_posed full.
- soft_target(idx, target, weight): CONSTRAINED drag (null-space projected) — hard constraints
  exact, point tracks cursor along the manifold; one soft_target per dragged coordinate.
- Analytic Jacobians only; sparsity pattern cached per solve; dense QR rank ok to ~600 vars.
- Worked residual templates (coincident/distance/point_on_line/fix/h/v) in test_nsolve.py.
- Perf: warm median ~8.8-9.0 ms @ 200 vars.

## C07 LBM wind tunnel
- `from engine.sim.lbm import run_wind_tunnel` (C08's sim worker already calls it).
- FlowResult: slices[3] (YZ/XZ/XY mid-plane vmag m/s float32 2D), slice_transforms[3] (4x4
  world mm), streamline_points[i] (N,3 mm) + streamline_speeds[i], trace [(step, drag_n)],
  reynolds_note (qualitative disclaimer string), diverged, frontal_area_mm2, cd, drag_n, lift_n.
  AICM packaging (SCLR slices/LINS streamlines) is C19's job.
- Runtime guidance: coarse full run ~23s; medium ~2min; fine ~16min, xfine ~73min — C19 must
  surface honest estimates + use max_steps caps for fine/xfine.
- LIMITATION for C19 UI: walls thinner than ~1 voxel leak (under-predict drag 5-15x) — warn
  when part min-thickness < voxel size. numba/vectorized streaming = M10 perf lever.

## C13 theme + C15 picking (committed together — interleaved edits in app.cpp/viewport_panel)
- ~65 palette roles in ui::Palette (ui_theme.h); applyTheme(Theme::Dark|Light, scale);
  Settings.theme persisted; AI_CAD_THEME env override; toU32(role) helper for draw lists.
  View menu + Settings checkbox switch live. Light = SW-style (#F0F0F0 chrome, #0078D7 accent).
- render/aicm_loader: DocMesh{bodies, scalars, displacements, lines}. render/id_pass: PickHit
  {body_id, kind, topo_id}, encodeId/decodeId (8b|2b|22b), R32UI + AI_CAD_IDBUF=rgba8 fallback.
  core/selection.h: Selection set (selectOne/toggle/clear/setObserver), SelItem.pending_resolve
  + .selector slots for C16's doc_resolve_pick round-trip.
- AppState::loadDocMesh(aicmPath, err) = the entry point C16 calls per rebuild; AppState::
  selection/hover/pickStatus/pickLabel() live; Renderer gained CameraXform camera() +
  beginOverlay/endOverlay. AI_CAD_SHOT=pick gate prints "pick: body=1 kind=face id=N".
- CMake: C15 sources in marked blocks; optional test_aicm.exe target (19 CPU checks).

## C09 features (dress/sketch/extrude/revolve)
- Registered: fillet/chamfer/shell (auto-clamp halve x3 floor 0.2mm; dangling -> feature error),
  sketch (NO-solve compile from stored coords -> regions; pseudo-body = tool_body compound of
  region faces, bodies list untouched), extrude (blind/mid_plane/through_all, boss/cut; draft
  param read but DEFERRED), revolve (reserved-datum or edge-selector axis; region-line deferred).
- Cross-feature state piggybacks ctx.datums sentinels: "_sketch_regions" Datum.obj =
  {sketch_fid: {faces, descs, plane, all_edges}}; "_topo_owners" Datum.obj = {fid: {"face":
  {1-based: owner}, "edge": {...}}} (last fid = current). Helpers _store/_get_topo_owners in
  extrude.py. GAP: primitives don't store owners -> selectors on primitive-only bodies resolve
  ownerless (all-candidates fallback). C11 should fix by storing topo maps after EVERY feature
  in the compiler/session layer, or accept the fallback.
- dress.py/sketch.py import private selectors._score_candidate for a hint_index tiebreak
  fallback (margin<=0.05 but best<0.01 — geometrically identical twins). Don't rename it.

## C14 ribbon
- ui/icons.{h,cpp}: loadIcons(assetsDir), getIcon(name, size)->IconUV, syncIconTheme() (swap
  texture, UVs constant), magenta placeholder. ui/widgets/ribbon: setRibbonCallback(fn(cmd_id)),
  drawRibbon(tab, show), ribbonHeight(). ribbon_def.cpp: 73 rows (Features 27/Sketch 25/
  Evaluate 11/Simulation 10), stretch rows disabled. AppState: showRibbon/ribbonTab/wantRibbon.
- C17 replaces the default printf callback with the PM dispatcher.
- ImGui 1.91.6: ImTextureID is ImU64 — cast (ImTextureID)0, not nullptr.
- POLISH DEBT (cosmetic, for C17 or a polish card): tab strip crowds the title bar; long labels
  overlap on narrow big-buttons and group captions ("Lin. Pattern"/"Coord. Sys" collide);
  needs min button width from label measure + tab strip vertical padding.

## C16 tree + doc mirror
- core::DocMirror (app_state: st.docMirror): newPart/applyOps(json ops)->OpsResponse{revision,
  tree, feature_status, aicm_path}/undo/redo/resolvePick(returns selector JSON string)/save/
  open/list; auto-resync on stale_revision; listener callback fires after every response
  (app.cpp feeds aicm_path to loadDocMesh). Doc calls are direct synchronous UI-thread calls
  (worker callMtx serializes; doc ops bypass the job system).
- ui/feature_tree: drawFeatureTree(st, editCallback) — pass the PM-opening lambda (C17).
  Fixed nodes + status badges + context menu + rollback strip + New Part/Undo/Redo.
- For C17: id_map not exposed standalone (parse from tree if needed); face-owner highlight
  deferred ("face-owner map not yet in C++ layer"); icon names used 'datum_plane'/'feature'
  are NOT in the atlas (use 'plane'/'history' or extend make_icons).
- AI_CAD_SHOT=tree needs AI_CAD_REPO set so the worker resolves; shot frame budget bumped for
  worker startup (wantTree ? 30 : 6).

## C10 sketch solver
- `from engine.doc.sketch_solver import solve_sketch, drag, SolveState, SketchSolution`.
- solve_sketch(sketch_params, equations_env, warm=None) -> SketchSolution{entities (solved param
  dicts, doc_schema §3.1 shape), statuses {eid: under|full|over}, dof, redundant, conflicting,
  converged, state}. Pass sol.state as warm= next call; DISCARD state on topology change.
- drag(state, handle, target_xy, sketch_params, equations_env) -> {entities, statuses} —
  transient, constraint-exact, ~10-11 ms; card signature extended (needs params+env to rebuild
  the System) — C18 must pass them.
- Parallel line-line distance: pass refs as point+line ("e1.p1","e2"); bare line-line treated
  as point_on path. Per-entity status via SVD nullspace inside sketch_solver (nsolve untouched).

## C11 sessions + worker doc_* + .step+ (the C16 contract)
- engine/worker.py now serves: 11 doc_* + 9 eval_* methods, ping protocol:2; legacy methods
  byte-identical (errors: NEW methods use {code,message,detail}; legacy keep plain strings).
- C16 contract transcript (test_worker_doc.py, real stdio): doc_new->doc_id+rev0;
  doc_apply_ops(add box)->rev1, id_map {$0:f1}, tess.aicm_path; doc_resolve_pick{revision,
  body_id, kind, topo_id(1-based TopExp == AICM ids)}->selector (stale revision -> code
  stale_revision + detail.current_revision); fillet via picked selector; undo/redo BUMP revision
  FORWARD and return full ops-response; doc_save/doc_open (.step+ ~5.6KB, doc_open adds doc_id
  + warnings); doc_list.
- .step+ thumbnail = flat placeholder (render path deferred); frozen-geometry open fallback
  deferred to C21. eval_measure/section/symmetry: last-body + reserved datums only (M5 refines).
- GOTCHA: `from engine.doc import evaluate` shadows -> use `import engine.doc.evaluate as ...`.

## C12 evaluate (this commit)
- engine.doc.evaluate: mass_props(shape,density), measure(infos), section_props(shape,plane),
  interference(bodies), check(shape), symmetry(shape,plane), draft_field/thickness_field/
  curvature_field(tess, shape, ...) each -> (values, vmin, vmax, chunk_bytes).
- Worker registration recommendations (for the card that wires eval_*): nine handlers
  eval_mass/measure/section_props/interference/check/symmetry/draft/thickness/curvature;
  field handlers: tessellate active body -> field fn -> write_aicm([tess], extra_chunks=[chunk])
  to temp path -> {aicm_path}.
- thickness uses Möller–Trumbore numpy fallback (venv lacks rtree); p25 = min-wall metric.
  curvature via ShapeAnalysis_Surface.ValueOfUV projection. check() reports numeric BRepCheck
  codes (no stable string labels in OCP 7.8.1).
