# C18–C23 — later-night cards (briefs)

Expand each into a full card (C01–C17 format) when its wave opens. Dependencies in
`docs/sessions/README.md`; detail sources: `docs/PLAN.md` §5 + the contracts.

## C18 — Sketch mode UI (opus) · needs C10, C17
Sketch-space camera lock + grid + snapping; entity tools (line/rects/circle/arcs/polygon/slot/
spline/ellipse/point) with rubber-banding + inference glyphs emitting auto-relations
(`sketch_add_entity.constraints`); DOF color coding from `sketch_solve` statuses; 60 Hz
`sketch_drag`/commit; Smart Dimension (click → place → DimField w/ expr); relation toolbar;
trim/extend/offset/convert/mirror/pattern tools (worker geometry helper ops per doc_ops.md);
worker `sketch_enter/solve/drag/drag_commit/exit` registration (C11 left stubs — wire them to
C10's solver). Verify: PLAN §5-M2 (test-engine-or-scripted dimensioned-rect → "Fully Defined" →
extrude; both-theme shots).

## C19 — Simulation + flow panels (sonnet) · needs C08, C07, C12, C17
Study tree UI (material/fixtures/loads/mesh/results), bottom study tabs, PM forms for
fixture/load/mesh nodes (SelectorWells on faces); sim WorkerClient #2 lifecycle (lazy spawn,
crash restart); progress events → status bar + live drag trace; results: SCLR/DISP rendering
(colormap LUT + legend + deformation scale slider + animate) and flow slices/streamlines
(LINS); qualitative banner on flow results; eval_* worker method registration from C12's
report. Verify: PLAN §5-M6/M7 GUI halves + cached-overlay reopen.

## C20 — Assembly (opus) · needs C06, C11, C17
`engine/doc/mate_solver.py` over nsolve (quat+trans per floating component; 8 mate residual
types; align flip; free-DOF drag solve); asm ops in `engine/doc/ops.py` (append) + worker;
Insert Components (file browse .step+/STEP), Mate PM (face-pair wells + type auto-suggest),
move/rotate drag with re-solve, component patterns, interference detection panel (C12 port),
exploded view slider, BOM table + CSV export; Mates folder in tree. Verify: PLAN §5-M4.

## C21 — Formats finalization (sonnet) · needs C11
IGES import/export via OCP, 3MF via lib3mf Mesher, STL import as frozen `import` feature;
`.step+` full (studies + results + ai/memory.json + thumbnail polish; unknown-entry
preservation test); Windows IFileDialog (`core/file_dialogs.cpp`); recent files; export
round-trip matrix test. Verify: PLAN §5-M8.

## C22 — AI orchestrator (opus; split into 22a engine / 22b UI when opened) · LAST
`agent/orchestrator/` per PLAN §3.1/§5-M9: DesignState; deterministic machine (INTAKE→PLAN→
MODEL→REVIEW→FIX→[SIM_SETUP]→MEMORY) with stage budgets + ported `_err_sig`/stagnation
detectors; role prompts `agent/prompts/*.md` + `%APPDATA%` overrides + GUI editor (diff/reset);
modeler emits doc-ops JSON (validate→dry-run→transactional apply→script escape hatch);
`agent_run_v2` + events.md hierarchy → Activity panel (live + events.jsonl replay); memory
into `.step+`. Legacy `agent_design` untouched. Verify: PLAN §5-M9 incl. live gemma run and
≤6k-token context assertion.

## C23 — Hardening + release (sonnet) · LAST
requirements.txt + wheelhouse rebuild WITH the sim pack (provision stamp auto-upgrades);
licenses manifest step in build_release; full sweep (every suite + both-theme shot gallery +
20-feature rebuild <2 s budget); ARCHITECTURE.md rewrite; installer smoke; tag.
