# AI-CAD Overhaul — Master Plan

> Status: **APPROVED 2026-06-10** · M0 in progress · This file is the canonical execution reference.
> Contracts live in `docs/contracts/` and change only via explicit version bump.

## 1. Context

AI-CAD today is a working AI-driven CAD pipeline: a model writes build123d code → OpenCASCADE
solid → STEP/STL + renders fed back for visual convergence, wrapped in a C++ GLFW/ImGui host with
a chat-style UI, a JSON-RPC Python worker, an MCP server, and an offline-provisioning installer.
All five pillars are built and verified, but the product is a *chat that produces parts*.

The overhaul turns it into a **SolidWorks-class parametric CAD application** (the user supplied
SolidWorks 2025 screenshots as the structural spec) where every modeling capability exists twice:
as a **GUI command** (ribbon → PropertyManager → viewport picking) and as an **AI tool** (structured
document ops). On top: stress FEA, a qualitative CFD wind tunnel, richer formats including a native
`.step+` container with embedded AI memory, and a fully transparent orchestrator/subagent AI system
designed for limited local models (LM Studio gemma-class, 4–8k context), with user-editable prompts.

## 2. Locked decisions (user Q&A, 2026-06-10)

1. **UI**: clone the SolidWorks structure exactly — ribbon tabs, FeatureManager tree,
   PropertyManager, heads-up viewport toolbar, task pane, status bar, bottom doc tabs — with a
   **runtime theme toggle**: faithful SolidWorks-light AND the existing dark/teal brand.
2. **Sketcher**: **full constraint solver** (SolidWorks-style relations + driving dimensions,
   under/fully/over-defined states, auto-relations, drag-to-flex). Open-source adaptation blessed.
3. **Formats**: import STEP/IGES/BREP/STL; export STEP/STL/3MF/IGES/BREP. Native container is a
   ZIP literally named **`.step+`** (geometry.step + document.json + studies + ai/memory.json +
   thumbnail). `.sldprt` dropped (proprietary; SolidWorks opens our STEP fine).
4. **Order**: foundation → **features + simulations first** → formats → **AI orchestrator LAST**
   (built once against the final document model). The existing agent loop keeps working the whole
   time (its script becomes a `script` feature node).
5. **Execution**: structured for **parallel subagent workstreams** with frozen contracts, owned
   directories, and per-milestone verification gates.

## 3. Architecture (validated)

API probes were run against the live runtime venv (py 3.11.9, build123d 0.10.0, OCP 7.8.1):
per-face triangulation with stable in-rebuild face/edge index maps **works** (`BRepMesh_IncrementalMesh`
+ `BRep_Tool.Triangulation_s` + `TopExp.MapShapes_s`); edge polylines come from
`BRep_Tool.PolygonOnTriangulation_s` against an adjacent face's triangulation (NOT `Polygon3D` —
returns None) with `GCPnts_TangentialDeflection` fallback; normals via
`BRepLib_ToolTriangulatedShape.ComputeNormals_s` honoring face orientation; exact per-vertex
curvature via stored UV nodes + `BRepLProp_SLProps`. All required feature APIs import:
`BRepOffsetAPI_DraftAngle/MakeOffsetShape/MakePipeShell/ThruSections`, `BRepFilletAPI_*`,
`BRepAlgoAPI_*`, `BRepCheck_Analyzer`, `BRepGProp`, `STEPControl_*`, `IGESControl_*`. lib3mf,
matplotlib (colormap LUTs), trimesh voxelizer, scipy are already in the 94-wheel wheelhouse.

### 3.1 Core design

- **Document model in Python** (`engine/doc/`): JSON-serializable PartDocument (ordered feature
  history), Sketch (entities + constraints + dims), AssemblyDocument (components + mates),
  Equations (safe-AST expressions usable in any dimension). The **worker is the sole authority**;
  the C++ GUI holds a read-only mirror and submits ops (`doc_apply_ops`) with optimistic revisions.
- **Compiler** (`engine/doc/compiler.py`): sequential feature evaluation over build123d/OCP with
  per-feature solid caching (edit feature k → recompute from k), per-feature timing, per-feature
  error containment (broken feature gets an error icon; rest of tree still builds — SolidWorks
  behavior), and topology diffing to tag every face/edge with its creating feature.
- **Topological references**: geometric-signature selectors (kind, surface type, centroid, axis,
  area, owner feature) re-resolved each rebuild by scored best-match with accept threshold +
  margin; failures surface as dangling-ref warnings in the tree. Honest limitation: geometric
  resolution, not persistent naming.
- **Tessellation/picking**: worker writes a chunked binary **AICM** file per rebuild (per-body
  verts/tris with per-FACE triangle ranges, per-EDGE polylines, B-rep VERTices, plus SCLR scalar /
  DISP displacement / LINS polyline chunks reused by Evaluate + FEA + CFD overlays). C++ renders an
  **R32UI ID buffer** (body|kind|topo-id encoding, RGBA8 fallback) for pixel-accurate hover/pick;
  picks round-trip via `doc_resolve_pick` into typed selector refs for feature params.
- **One numeric solver core** (`engine/doc/nsolve.py`): residual registry + analytic sparse
  Jacobian + damped Newton/LM + warm start + QR-rank DOF analysis + redundant-constraint
  identification. The 2D sketch solver and the 3D mate solver are thin parameterizations over it.
  In-house is primary (GPL-free, better diagnostics); python-solvespace is an optional backend
  probed by spike. Drag solves are warm-started, budget ≤10 ms at ~200 unknowns.
- **Undo/redo lives in the worker** (bounded doc-JSON snapshot stack + op log); GUI command stack
  is display-only. Every doc RPC carries `base_revision`; stale ops are rejected and the GUI
  resyncs. `ping` reports `protocol: 2`.
- **Two workers**: the existing doc worker (doc/sketch/eval/agent methods) and a separate on-demand
  **sim worker** process (`sim_*` methods) so a 15-minute CFD run can't block modeling and a
  gmsh/LBM crash can't kill the document session. Sim worker receives STEP/BREP path + study JSON.
- **Units**: canonical **mm/degrees/grams internally everywhere** (doc JSON included). IPS/MMGS is
  display + input parsing in C++ only. Expression trig takes degrees.
- **Host modularization**: `gui_app.cpp` (~1.9k lines) is mechanically split FIRST
  (behavior-preserving, screenshot-verified) into `app.cpp`, `ui/*` (legacy_chat, settings_modal,
  console, title_bar, status_bar), `core/*` (jobs, app_state); then new UI lands as `ui/widgets/
  ribbon`, `ui/feature_tree`, `ui/property_manager`, `ui/viewport_hud`, `ui/sketch_mode`,
  `ui/sim_panel`, `ui/flow_panel`, `ui/asm_panels`, `ui/equations_dialog`, `ui/activity_panel`,
  `ui/doc_tabs`, plus `render/aicm_loader`, `render/id_pass`, `render/overlays` and
  `core/selection`, `core/doc_mirror`. Theme = ~40 named palette roles, two palettes, runtime
  switch; every extracted panel uses roles, never hardcoded colors.
- **Icons**: ~90 PIL-generated icons (20/32 px, light+dark atlases) via `host/assets/make_icons.py`
  (same pattern as the proven `make_logo.py`); data-driven ribbon from a command table.
- **FEA**: gmsh (pip wheel; GPL — subprocess-isolated in the sim worker, netgen documented as
  swap) STEP→tet10 → scikit-fem (BSD, pure-pip) linear elasticity → scipy sparse (pyamg optional)
  → von Mises / displacement / FoS as SCLR+DISP chunks on the surface mesh; material library JSON
  (steel, 6061, ABS, PLA, PETG, nylon, Ti-6Al-4V). Modal (eigsh, 6–10 modes) is a cheap second
  study type. Study tree UI + bottom study tabs, colormap legend, deformation scale.
- **CFD**: numpy **D3Q19 LBM** voxel wind tunnel (trimesh voxelize; BGK + Smagorinsky LES,
  half-way bounce-back, Mach guard, NaN watchdog, presets 48³/64³/96³/128³) → drag/lift via
  momentum exchange + honest Cd, velocity slice planes, RK2 streamlines (LINS). Permanently
  labeled **qualitative**. OpenFOAM/CalculiX rejected: no maintainable local-first Windows story.
- **AI orchestrator (M9)**: deterministic Python state machine (code decides, never the model):
  INTAKE → PLAN → MODEL (ops per plan step) → REVIEW (vision) → FIX → [SIM_SETUP] → MEMORY.
  Role subagents each with an **editable markdown prompt** (`agent/prompts/*.md` shipped;
  `%APPDATA%\AI-CAD\prompts\` overrides; GUI editor with diff/reset). Modeler emits **doc ops JSON**
  (the same closed vocabulary as the GUI; few-shot sized for 4–8k contexts; schema-validated,
  dry-run compiled, transactional) with a raw-script escape hatch. Per-stage attempt budgets +
  the proven stagnation/fail-streak/error-signature detectors ported from `agent/loop.py`.
  **Compaction by construction**: every agent call gets a fresh bounded context assembled from a
  shared DesignState (overflow compaction kept as fallback). Full transparency: hierarchical events
  (run/stage/attempt with prompts, inputs, raw output, parsed result, durations) streamed to an
  Activity panel and persisted to `cache/runs/<id>/events.jsonl` for replay.

## 4. Contracts (`docs/contracts/`, frozen at M0)

| Contract | Contents |
|---|---|
| `doc_schema.md` | PartDocument / Feature / Sketch / AssemblyDocument / Equations JSON, units policy, expression grammar |
| `doc_ops.md` | Closed op vocabulary (envelope, transactionality, error codes) — also the M9 AI tool schema |
| `mesh_format.md` | AICM chunked binary (BODY/FACE/EDGE/VERT/SCLR/DISP/LINS) + golden fixture |
| `worker_rpc.md` | All RPC methods (doc/sketch/eval/import/export/sim/agent), protocol 2 handshake, error envelope |
| `selectors.md` | Selector fields, scoring algorithm, thresholds, owner-feature tagging, dangling behavior |
| `events.md` | Event envelope: doc_progress, sim_progress, orchestrator run/stage/agent_call/agent_output/stage_result |
| `stepplus.md` | `.step+` ZIP manifest, load order, fallback, size caps |
| `feature_params.md` | Per-feature params/refs/PM form/compiler entry — shared by Python compilers, C++ PM forms, AI modeler |
| `licenses.md` | Dependency license ledger + isolation policy (gmsh GPL, test-engine clause, solvespace GPLv3) |
| `icons.md` | Icon inventory, sizes, themes, atlas format |

Shared "merge-by-append" files (one integrator owns them per milestone): `engine/worker.py` method
registry, `host/CMakeLists.txt` source list, `host/src/ui/ribbon_def.cpp` command table.

## 5. Milestones

```
M0 contracts
 └─ M1 foundation (doc model + host split + ribbon shell + picking + minimal .step+)
     ├─ M2 sketcher + extrude/revolve ──┐
     │    └─ M3 features + equations ───┬─ M4 assembly ──┐
     ├─ M5 evaluate (after M1, overlaps)┘                ├─ M8 formats
     │                                  └─ M6 FEA ─ M7 CFD┘
     └─────────────────────────────────────────────────── M9 AI orchestrator (LAST)
                                                           └─ M10 hardening/release
```
**Regression gate at the end of every milestone:** the 5 legacy test suites + new suites all green;
`AI_CAD_SHOT` screenshot gallery rendered in BOTH themes; legacy agent loop + MCP server intact.

### M0 — Contracts & scaffolding (~1 wk) — single-threaded, no parallelism
All `docs/contracts/*`, fixtures (`part_bracket.json`, `asm_clamp.json`, golden `box.aicm` +
generator), `engine/doc/` schema validator + equations evaluator + tests, wheel-availability spike
(gmsh/scikit-fem/meshio/pyamg/python-solvespace), git repo init + baseline.
**Verify:** fixture validation suite green; golden AICM regenerates byte-identical; legacy suites green.

### M1 — Foundation (5–7 wks) — 3 parallel workstreams
- **WS-1A Python doc engine** (owns `engine/doc/`): schema dataclasses, equations, compiler with
  cache/timing/topo-diff/error containment, features: `script` (wraps the existing forgiving
  kernel exec — legacy agent output becomes a tree node), primitives, fillet/chamfer/shell on
  selectors; `selectors.py`, `tessellate.py` (AICM writer), `session.py` (multi-doc + undo),
  worker `doc_*` methods + `doc_resolve_pick`, minimal `stepplus.py` save/open.
  Tests: compile, selector torture (edit upstream → re-resolve; delete owner → dangling), AICM
  golden, step+ roundtrip, undo.
- **WS-1B Host core/render** (owns `host/src/core/`, `host/src/render/`): mechanical split of
  gui_app.cpp FIRST (gate for all C++ work, before/after screenshots byte-compared), then
  aicm_loader, id_pass (R32UI + fallback), overlays (hover/select tint, SCLR colormap scaffold),
  selection set + filters, doc_mirror with revision resync.
- **WS-1C Ribbon/theme/icons/tree/PM shells** (owns `host/src/ui/`, `host/assets/icons/`):
  make_icons.py + atlases, ribbon widget library + command table, palette-role theme rewrite
  (dark + SW-light, runtime switch), FeatureManager tree (status icons, context menus, rollback
  bar visual), PropertyManager host (OK/Cancel, collapsible groups, dim fields with expr,
  selector wells bound to selection), viewport HUD, doc tabs, status bar (units, defined-state).
  Builds against `docs/contracts/fixtures/` until WS-1A lands.
**Integration:** New Part → Box via PM → pick edges → Fillet → pick face → Shell → tree shows the
history → edit fillet radius → cached rebuild → save/open `.step+` → legacy chat designs into a
`script` feature in the same document.
**Verify:** WS-1A suites; AICM golden both sides; `AI_CAD_SHOT=ribbon|tree|light`; ImGui Test
Engine (vendored, `AI_CAD_TESTS=ON`, dev-only) script: box→fillet→tree count==3; legacy suites.

### M2 — Sketcher (4–6 wks) — engine/UI/features in parallel
`nsolve.py` core; `sketch_solver.py` (full relation set: coincident, horizontal, vertical,
parallel, perpendicular, tangent, equal, concentric, midpoint, symmetric, fix, point-on +
distance/angle/radius/diameter dims, driven dims); region detection → pickable profile faces;
worker `sketch_enter/solve/drag/drag_commit`. UI: sketch mode (camera lock, grid, snapping),
tools (line, rects, circle, arcs, polygon, slot, spline, ellipse, point), inference glyphs +
auto-relations, entity color by DOF state, ≤60 Hz drag, Smart Dimension with expr input,
relation add/delete, trim/extend/offset/convert/mirror/sketch patterns. Features: extrude
(boss/cut; blind/through-all/mid-plane; draft) + revolve, with PM forms.
**Verify:** per-constraint unit tests; DOF canon (rect + 2 dims + anchor = fully defined);
redundancy named on over-define; warm drag <10 ms @200 unknowns; test-engine: draw dimensioned
rect → "Fully Defined" → extrude; screenshot diffs both themes.

### M3 — Feature set + equations (4–5 wks) — parallel per feature family
Sweep/loft family; hole wizard (simple/cbore/csink/tapped-cosmetic, M2–M12 table); linear/circular
pattern + mirror (tool-body re-boolean semantics); reference geometry (plane/axis/point/CS);
helix; booleans; draft; rib (S); split (S). Equations dialog + dependency-aware rebuild.
**Verify:** per-feature volume-asserted tests; cache test (edit 1 of 10 → 9 recompiled); dangling
UX test; eval_perf per-feature ms.

### M4 — Assembly (3–4 wks)
Component graph + transforms; mate solver over nsolve (coincident/parallel/perpendicular/tangent/
concentric/distance/angle/lock, align flip; free-DOF drag); insert components (.step+/STEP);
move/rotate with re-solve; component patterns; interference detection (ported `_fit_report`);
exploded view + slider; BOM panel + CSV.
**Verify:** peg-in-hole DOF tests; interference fixture; test-engine 2-part mate flow; BOM shot.

### M5 — Evaluate (2–3 wks, overlaps M4)
Measure, mass properties, section properties, interference, draft/thickness/curvature overlays
(SCLR), zebra (shader), symmetry check, BRepCheck + geometry analysis, performance evaluation,
section view (clip shader, uncapped v1).
**Verify:** analytic fixtures (10 mm wall → thickness 10; r=5 cyl → κ=0.2); overlay shots both themes.

### M6 — FEA (3–4 wks)
Sim worker process; gmsh mesher (density slider → char length); scikit-fem static (tet10) +
modal; materials.json; results → npz + SCLR/DISP; study tree UI + study tabs + legend +
deformation scale + animate. Wheelhouse += gmsh/scikit-fem/meshio(/pyamg).
**Verify:** cantilever tip deflection vs Euler–Bernoulli ≤5%; plate modal f₁ ≤10%; kill -9 sim
worker mid-run → doc worker healthy; von Mises shot.

### M7 — CFD (2–3 wks)
`engine/sim/lbm.py` D3Q19 as specced; flow panel (direction/speed/preset, slice quads, streamlines,
**qualitative banner**, live drag trace).
**Verify:** sphere Cd ∈ [0.3, 0.7] @64³; plate>sphere; 2000 steps no NaN; streamline shot.

### M8 — Formats (1.5–2 wks)
Import STEP/BREP/STL (build123d) + IGES (OCP) → `import` feature; export STEP/STL/BREP/IGES/3MF;
`.step+` finalized (studies + results + ai/memory.json + thumbnail); Windows IFileDialog;
recent files; file association (S).
**Verify:** round-trip matrix (volume within mesh tol); SW-exported STEP smoke; study survives reopen.

### M9 — AI orchestrator (4–5 wks) — LAST, against the final doc model
`agent/orchestrator/`: DesignState, machine (stage budgets + ported convergence detectors),
roles + editable prompts (intake, planner, modeler, reviewer, fixer, sim_setup, memory +
ops cheatsheet), ops emission (validate → dry-run → transactional apply → escape hatch),
worker `agent_run_v2` streaming hierarchical events; Activity panel (live + jsonl replay);
prompt editor tab (per-role, diff vs built-in, reset); memory read/write into `.step+`.
Legacy `agent_design` kept verbatim.
**Verify:** mock-provider machine transcripts asserted; malformed-ops fuzz → bounded retry →
escape hatch; live gemma: "bracket with 4 M3 holes" yields a real feature tree ≥60% of runs;
per-call context ≤6k tokens asserted in tests.

### M10 — Hardening & release (2 wks)
Wheelhouse rebuild + pins (stamp-hash auto-reprovisions installs); full sweep (all suites,
test-engine, shot gallery both themes); rebuild budget: 20-feature part <2 s warm; ARCHITECTURE.md
rewrite; installer smoke on clean VM.

## 5.1 Wave C — Viewport & Interactive refinement (concurrent with M1–M3)

These waves are user-visible refinements landing in parallel with core feature development,
gated by the same regression suite. They do not block the critical path.

### C25 — SolidWorks-class viewport (1.5–2 wks)
Light studio viewport in both themes with retuned lighting + ground shadow; theme-aware silhouette
edges; rich display styles (shaded, shaded+edges, HLR, HLV, wireframe); animated camera with
smooth interpolation; MMB navigation (rotate/pan/zoom via middle-mouse gestures); S shortcut bar
(quick access to view/display/sketch tools); heads-up toolbar completion (align remaining command
icons); viewport context menu (section view, display toggle, camera presets); section view clipping
with uncapped v2 shader (see through cut); GUI audit pass (redundant widgets, orphaned state).
New icons: display_shaded_edges, display_hlr, display_hlv, display_wireframe, pan_view, rotate_view,
zoom_area, view_selector, normal_to.
**Verify:** interactive camera jitter <1 px; theme toggle preserves viewport state; context menu
closes on click-outside; section view clipping shots both themes.

## 6. Feature tiers (the scope contract — anything not V1/S is a no without re-planning)

- **Features tab V1**: extrude boss/cut, revolve, sweep, loft, fillet, chamfer, shell, draft,
  hole wizard, linear/circular pattern, mirror, ref plane/axis/point/CS, helix, combine/subtract/
  intersect. **S**: boundary, rib, split, variable fillet, wrap-as-emboss. **Skip**: Instant3D,
  advanced hole.
- **Sketch tab V1**: full entity + relation + dimension set listed in M2, construction toggle,
  trim/extend/offset/convert/mirror/patterns/move-copy. **S**: text, perimeter circle.
- **Assembly V1**: insert, 8 mate types, move/rotate, component patterns, interference, exploded
  view, BOM. **S**: smart fasteners, width/path/symmetric mates. **Skip v1**: in-context editing,
  motion study (tab greyed).
- **Evaluate V1**: measure, mass props, section props, interference, draft/thickness/curvature,
  zebra, symmetry, check, geometry analysis, performance eval. **Skip**: sensors, compare,
  parting line, undercut, DimXpert.
- **Simulation V1**: static, modal, LBM flow. **Skip**: thermal, buckling, nonlinear, fatigue.

## 7. Sizing

| Milestone | Py LOC | C++ LOC | Calendar (solo + subagents) |
|---|---|---|---|
| M0 | 0.4k | 0.1k | 1 wk |
| M1 | 3.0k | 4.5k | 5–7 wk |
| M2 | 2.2k | 2.5k | 4–6 wk |
| M3 | 2.5k | 2.0k | 4–5 wk |
| M4 | 1.8k | 1.6k | 3–4 wk |
| M5 | 1.4k | 1.0k | 2–3 wk (overlap) |
| M6 | 1.6k | 1.2k | 3–4 wk |
| M7 | 1.0k | 0.8k | 2–3 wk |
| M8 | 0.8k | 0.5k | 1.5–2 wk |
| M9 | 2.4k | 0.9k | 4–5 wk |
| M10 | 0.5k | 0.3k | 2 wk |
| **Total** | **~17.6k** | **~15.4k** | **~5–6 months with disciplined parallel agents** |

## 8. Risk register (top items)

| Risk | Mitigation |
|---|---|
| Selector drift (wrong face after edits) | Owner-feature tagging, accept threshold + margin, dangling-ref UI (never silent wrong pick), torture suite; documented honestly |
| Sketch solver feel | Warm start, analytic Jacobians, soft drag constraint, time budget + last-good fallback, slvs plug-in backend option, benchmark in CI |
| OCCT shell/draft/fillet robustness | Per-feature error containment + generalized auto-clamp retry (proven in `_wrap_edge_op`), guidance strings |
| Rebuild perf on long trees | Per-feature cache, tessellate changed bodies only, eval_perf visibility, <2 s budget test |
| GUI/worker state divergence | Worker-authoritative, revisions + stale resync, worker-side undo |
| gui_app split regression | Split lands alone, headless screenshots byte-compared before/after |
| Wheelhouse growth (gmsh ~50–90 MB) | Stamp-hash auto-reprovision exists; offline pip-resolve CI check; sim pack optional-extra if hostile |
| Licensing (gmsh GPL, slvs GPLv3, test-engine) | `licenses.md` ledger; subprocess isolation; in-house solver primary; test engine dev-only |
| 4–8k local models can't emit ops | Tiny closed vocabulary, few-shot per role, validation + bounded retry, script escape hatch keeps the proven floor |
| LBM instability/misleading results | Mach/tau guards, NaN watchdog, fixed qualitative banner, Cd band tests, honest presets |
| ID-buffer driver quirks | R32UI primary, RGBA8 fallback flag, 1-px readback |
| SW pixel-fidelity scope creep | §6 tiers are the contract |

## 9. Execution model (parallel subagents)

- **Ownership**: workstreams own disjoint directories (`engine/doc/` vs `host/src/render/` vs
  `host/src/ui/` vs `engine/sim/` vs `agent/orchestrator/`). The three merge-by-append files are
  integrator-owned. Contracts change only by version bump.
- **Git**: repo initialized at M0 with a baseline commit; each workstream runs in its own
  worktree/branch; integrator merges per milestone; milestone tags (`m0`, `m1`, …).
- **Gates**: a workstream is DONE only when its tests pass standalone AND the milestone
  integration scenario passes AND the regression gate (legacy suites + both-theme shot gallery)
  is green. Each milestone ends with a tagged, runnable app.
- **Continuation criteria** (for agent-driven work): fixed attempt budgets per work item;
  identical-failure detection (same error signature 3×) stops the attempt and escalates to the
  integrator instead of spinning — the same discipline the runtime orchestrator (M9) encodes.

## 10. Verification quick reference

- Python: per-package `test_*.py` plain runners (current convention), run with the venv python
  from repo root. New suites land next to their packages.
- C++: ImGui Test Engine scripts behind `AI_CAD_TESTS=ON` (dev-only); headless `AI_CAD_SHOT`
  screenshot gallery in both themes per milestone; golden AICM byte-compare in loader tests.
- Cross: integration scenarios named per milestone above; `build.bat` syncs dist; legacy
  `--build`/`--ensure-runtime` smokes stay green.
