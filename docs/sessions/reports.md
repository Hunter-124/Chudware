# Landed-card integration notes (orchestrator log)

## C24 (final UI polish pass)
- DEFAULT LAYOUT rebuilt (title_bar.cpp buildDefaultLayout) to the SolidWorks arrangement: LEFT =
  FeatureManager full-height (~280px; PM still swaps into the same dock slot), RIGHT = Task Pane
  (~340px) with Design + Agent Activity as tabs, BOTTOM = Console thin strip under the viewport,
  CENTER = Viewport. View menu gained a "Task Pane" toggle (drives showDesign+showActivityPanel).
  "Reset layout" reproduces exactly this. New AppState flags: showTaskPane, focusDesignTab.
- FLOATING-DIALOG DOCKING (C20b debt): buildDefaultLayout pre-registers the command dialogs
  (Simulation, Insert Component, Mate##asm, Exploded View, Interference Detection, Bill of
  Materials, and the evaluate-dialog titles Mass Properties/Measure/Section Properties/Geometry
  Check/Symmetry Check/Zebra Stripes/Draft angle/Wall thickness/Mean curvature) into a lower-right
  task-pane node via DockBuilderDockWindow, so they open as DOCKED TABS instead of floating behind
  the NoBringToFrontOnFocus central node. Verified: Mass Properties + Exploded View land docked.
- RIBBON polish (widgets/ribbon.cpp): per-button width = max(min, wrapped-label width) clamped to a
  cap (no overlap); two-line labels via ribbon_wrapLabel (splits on '\n' or last space; ribbon_def
  untouched — wrapping is render-side); group captions clipped to group width (no collision);
  reserved bottom caption strip (kCaptionH) so captions never overlap small-button rows; taller tab
  strip (kTabH 28) with top inset clear of the title bar; body taller (kBodyH 76). Horizontal
  overflow scroll retained.
- FEATURE TREE polish (feature_tree.cpp): featureIcon() now maps every type to a real atlas name
  (sketch->sketch_node, extrude->extrude_boss, revolve->revolve_boss, datum_plane->plane,
  feature/script->history, box/cylinder/sphere/cone/torus->part, hole->hole_wizard, etc.) — the two
  'datum_plane'/'feature' unknown-icon warnings are GONE (header planes use 'plane'). New top strip:
  SW-style tab row (tree active; properties/configurations stubs) + compact icon toolbar
  (New Part/New Assembly/Open/Save/Undo/Redo) replacing the old wide bottom button strip (only the
  Save/Open path field remains at the bottom). Rollback bar = thin accent line + centered grab
  handle. NOTE: the sketch-row context detection now matches "sketch_node".
- VIEWPORT (viewport_panel.cpp): centered translucent heads-up toolbar at the top (zoom-fit +
  Iso/Front/Right/Top presets + display-style/section toggles as stubs); orientation triad
  bottom-left projected from the live yaw/pitch. New palette roles axisX/axisY/axisZ added to BOTH
  dark + light (ui_theme.{h,cpp}). The old top-of-panel view buttons were removed.
- STATUS BAR (status_bar.cpp): right cluster now "MMGS | Editing Part/Editing Assembly | provider/
  model" (SW order; editing-mode reads the doc schema; whole cluster click opens Settings).
- DocMirror::resync() (doc_mirror.{h,cpp}): public doc_tree pull + listener notify; replaces the
  throwaway resolvePick(1,"body",1) in sketch_mode.cpp resyncMirror + the two in the app.cpp sketch
  scenario. (The C22b doc_rebuild-after-v2-run call is LEFT as-is: it also returns the aicm_path
  the viewport reload needs, which doc_tree does not.)
- GALLERY: every AI_CAD_SHOT mode (default/ribbon/tree/pick/m1/sketch/asm/activity/evalmass) shot in
  dark AND light into scratch/c24_gallery/ — all exit 0; m1 cache-assert + tree-intact green, asm
  z=15 PASS both themes, no icon warnings. test_aicm.exe 19/19. Tab-default caveat: the right pane
  may show Agent Activity selected on the very first frame of a fast shot (SelectedTabId set in
  DockBuilder + SetNextWindowFocus, but ImGui's runtime tab bar can override on appearing frame);
  settles to Design by ~frame 10 and is correct in normal interactive use. Both tabs always present.

## C20b + C22b (wave C, combined commit)
- AsmPanels: Insert Component, Mate PM (auto-suggest by picked surf pair, Aligned/Anti flip w/
  "seat-on-face = Anti" hint, DimField for distance/angle), Exploded slider (view-only state,
  reload SAME aicm path), Interference window, BOM (qty rollup by source, CSV). DocMirror gained
  newAssembly() + resolvePickAsm() (returns {selector, component}). Assembly ribbon tab appended
  at index 4 (existing tab indices stable). Assembly tree: (f) prefix + Mates folder w/ context.
- ActivityPanel: live run->stage->attempt tree from agent_run_v2 events (routed through jobs
  event queue with "orch_" prefix) + events.jsonl replay combo; attempt nodes expose role,
  prompt_file (click -> opens Agents editor), context size, duration, verdict chips, raw output.
  Settings "Agents" tab: per-role editor, Save->override_path, Reset, char-count diff badge.
  Design panel "Use orchestrator (v2)" checkbox (agent_v2 setting; needs an open doc); after a
  v2 run, doc_rebuild refreshes tree+mesh. Fixture contracts/fixtures/mock_replies_v2.json.
- AI_CAD_SHOT now also: asm (asserts |peg z-15|<0.5), activity. ALWAYS run shots with
  AI_CAD_PROVIDER=mock.
- C24 DEBTS (from both agents): dock the floating asm dialogs (dockspace NoBringToFrontOnFocus
  hides floating windows behind the central node — dock them or use the PM slot), viewport-drag
  move-component, asm pattern op (engine gap), DocMirror::resync() convenience, prompt-editor
  full diff, run-list human timestamps.

## C20-engine + C22a (wave B engine; both agents died hung — orchestrator salvaged + fixed)
- ROOT CAUSE of the hangs + test flakes: agent/config.py resolved provider as
  config.json BEFORE the AI_CAD_PROVIDER env var, so "mock" subprocess tests silently called
  the user's LIVE LM Studio (gemma) — nondeterministic replies, multi-hour stalls. FIXED:
  env var now outranks config.json (it's a per-process override). Plus: roles.load_prompt
  raises on unreadable builtin (was silent ""), run.py prepends "ROLE: <role>" line to every
  system prompt (deterministic mock routing; harmless for real models), RoleScriptedProvider
  returns a loud MOCK-MISSING-ROLE sentinel. agent.test_orchestrator now 6/6 stable.
- C22a surface: agent/orchestrator/{state,machine,roles,ops_emit,run,events}.py +
  agent/prompts/*.md (+shared/ops_cheatsheet.md); worker agent_run_v2/agent_cancel/
  get_prompts_v2; events per events.md §3 + %APPDATA%/cache/runs/<id>/events.jsonl + sidecars;
  memory staged as session.extra_entries["ai/memory.json"]. Escape hatch verified (garbage ops
  -> script feature). Context cap: max 911 est tokens across calls (<=6000 asserted).
- C20-engine surface: engine/doc/mate_solver.py (8 mate types over nsolve, align aligned/anti
  — SW semantics: seat-on-face = ANTI; warm per-component (t,quat)), assembly.py (component
  loading .step+/.STEP w/ mtime cache, selector->MateGeom in component-local frame),
  session.py asm state (asm_result/asm_warm/explode_active+factor; set_explode_state is
  VIEW-ONLY: no revision bump, re-tessellates IN PLACE — compare content not paths),
  worker doc_solve_mates + doc_set_explode_state; component default name = file stem
  (orchestrator fix in ops.py). VERIFIED end-to-end by orchestrator-authored
  test_worker_asm.py: insert x2 -> concentric+coincident(anti) -> (0,0,15) exact, designed
  0.1mm clearance reported, explode +40mm, undo. C++ half NOT built (agent died) -> C20b.

## C18 sketch UI + C19 sim/eval UI + C21 formats (wave A, combined commit)
- Worker now also serves: sketch_enter/solve/drag/drag_commit/exit (C18; session.py holds
  _SketchState w/ warm solver state; drag transient, commit = one undoable batch; exit persists
  solved coords + rebuilds; cache-clear hook _batch_consumes_sketch_region in apply_ops so
  region-consuming features see a cache-hit sketch's regions), doc_export_step + doc_face_points
  (C19, engine/sim/bridge.py), import_file/export_file (C21 via _METHODS.update).
- ui/sketch_mode: ImGui draw-list overlay projected via CameraXform (no new GL); tools
  line/rect/circle/center-arc/slot + Smart Dimension (pmParseDim, =expr) + auto-relations
  (coincident snap 6px, h/v 5deg); status bar shows defined-state + DOF; AI_CAD_SHOT=sketch.
  Stubs left for C24: trim/extend/offset/convert/mirror/pattern tools, polygon/spline/ellipse.
  DocMirror resync after sketch commits uses a throwaway resolvePick — C24 should add resync().
- Renderer scalar pass (C19): PartMesh gained scalar/disp arrays; setColormapMode/setDeformScale/
  scalarRange; 256x1 jet LUT, aScalar attr; legend widget ui/legend.cpp. core/sim_client = 2nd
  WorkerClient(module="engine.sim.worker") lazy+restart. ui/sim_panel (Static/Modal/Flow studies,
  material combo from materials.json, SelectorWell fixtures/loads -> doc_face_points, run ->
  progress -> overlay_aicm -> colormap+legend+summary); ui/evaluate_dialogs (mass/measure/
  interference/draft/thickness/curvature/check/section). engine/sim/overlay.py packages results
  (nearest-node volume->surface SCLR/DISP; turbo slice PNGs; LINS streamlines).
  NOT wired: textured slice quads in viewport, eval_perf, zebra (deferred notes in dialogs).
- AI_CAD_SHOT modes now: sketch, fea, evalmass, m1, tree, pick, ribbon (+legacy). All exit 0.
- WorkerClient::start(err, module="engine.worker") — second param new (C19).

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

## C17 PropertyManager + M1 close (agent died at session limit; orchestrator finished it)
- ui/property_manager.{h,cpp} + pm_forms.cpp: PM swaps over the FeatureManager dock while a
  command is active (SW behavior); data-driven forms (box/cylinder/extrude/revolve/fillet/
  chamfer/shell); DimField (mm/in/fraction suffixes + =expr); SelectorWell consumes resolved
  selectors from core::Selection filtered by kind; live preview via debounced apply + Cancel =
  N worker undos; ribbon callback dispatches to PM (replaces C14 printf).
- ORCHESTRATOR FIX (the crash that killed the night's first m1 run): worker_client.cpp call()
  did j.value("error","...") -> get<string> on protocol-2 OBJECT error envelopes -> nlohmann
  type_error -> std::terminate (0xC0000409). Fixed: object errors land in `result` (mirror
  checks result.contains("code")) with call() returning true; string errors keep legacy false
  path; transport failures false. ALSO doc_new now sends {kind:"part"} per contract (was
  {schema:...}, worker defaulted correctly).
- AI_CAD_SHOT=m1 = the M1 integration scenario (needs AI_CAD_REPO): new part -> box -> pick 2
  vertical edges -> fillet -> shell(top) -> edit fillet r=8 (CACHE ASSERT: box ms unchanged,
  fillet recompiled) -> save .step+ -> reopen tree intact(3). Exit 0; shots in scratch/m1_shots.
- Gate at m1 tag: 17/17 suites green (test_lbm ran green separately, 90s).

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
