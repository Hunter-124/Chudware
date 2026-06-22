# Overnight autonomous build plan — agent sim + assembly + local hardening

**Branch:** `feature/copilot-overhaul-sim-assembly` (session-1 checkpoint = 38ac03eb)
**Goal:** Move toward "the AI designs a model airplane with control surfaces, assembles it, and runs
a CFD sim — usable offline/local." Build the three missing capabilities; commit a checkpoint after
each verified increment. The local-model *quality* ceiling is accepted — build so a capable model
(cloud now, big local later) can drive the workflow, and so local degrades gracefully.

Update the STATUS box of each workstream as I go (this file is my anti-amnesia anchor across context
resets — also mirrored in memory `agent-feature-vocabulary-airfoil-multipart` + the task list).

---

## WS1 — Harden the local/micro path  [STATUS: DONE — commit a50f345c]
Smallest, improves everything else's testability. Done when a local model's *failed* build reports
failure (not "done"), and common local-model JSON malformations self-heal.
- [ ] 1a. Fix micro soft-fail: `run_micro_design` reports status="done" even when rev never advanced /
      no geometry built. Make it surface failure when no feature applied. (core/agent/micro.cpp)
- [ ] 1b. JSON repair for local-model output: normalize the common malformations seen live
      (double-wrapped arrays e.g. `points:[[[x,y],...]]`, trailing commas, single quotes) in the
      ops/array extractor. (core/agent/ops_emit.cpp / micro.cpp extract_json_array)
- [ ] 1c. Re-verify: local 4B simple part still ok; revolve attempt now either builds or fails honestly.

## WS2 — Agent CFD/sim tool  [STATUS: core DONE, verifying lift; then commit]
Done = the agent runs a wind-tunnel CFD on the current geometry from a NL request, returns
drag/lift/Cd. APPROACH (built): there was NO sim_run_flow + no voxelizer; added them.
- [x] 2a. Mapped: only a low-level `sim_lbm` kernel (needs a pre-voxelized mask). No voxelizer.
      lbm_run returns drag_n/lift_n/cd/frontal_area; DRAG=lattice X, LIFT=lattice Y (EQ[q][1]).
- [x] 2b. NEW `core/sim/flow_run.{h,cpp}` (in aicad_sim): triangle-soup X-scanline parity voxelizer
      + wind-tunnel domain + lattice-param derivation (mirrors lbm.py) -> lbm_run. Self-contained.
      `doc::doc_tessellate_bodies(doc_id)` (session.cpp) supplies triangles (factored from
      doc_render_png). Axis map CAD(x,y,z)->LBM(x,z,y) so lift comes out on CAD-Z (Z-up).
- [x] 2c. RPCs in libaicad.cpp: `sim_run_flow` {doc_id,velocity,aoa,fluid,cells,n_steps} and
      `agent_simulate_v1` {doc_id,request} (regex-parses "<n> m/s", "<n> deg" -> offline, no LLM).
- [x] 2d. VERIFIED sphere: frontal area 7811 vs exact pi*50^2=7854 mm^2 (<1%!), lift~0 (symmetric),
      Re 66667 exact, 4s. Cd 0.18 (coarse LBM underpredicts — directional). Wing-lift-vs-AoA check
      running (bst9198ug).
  TODO host hookup (a Simulate action / chat-intent routing to agent_simulate_v1) — note for later.

## WS3 — True assembly + part interfaces  [STATUS: DONE — core b9c3707b + host display wired]
HOST DISPLAY NOW WIRED: run_assembly_design returns the saved assembly's absolute `asm_path`;
launchAgentRunV2 (jobs.cpp) captures it into job.createdDocPath; the app.cpp v2-drain sets
st.requestOpenPath so the existing GL-thread open pump (featureTreeOpenPath -> docMirror.open ->
worker) opens it as a NEW tab — and since the C++ core rejects assembly opens with code
"unsupported", the worker routes that open to Python (the proven File>Open/asm-scenario path),
so the C++-created assembly displays. The active part doc is left untouched (drain skips its
rebuild for this case). VERIFIED headlessly (tools/asm_open_verify.py): the saved file is a valid
aicad.asm/1 with 2 components referencing the part .step+ files, AND C++ doc_open returns
"unsupported" (the route-to-Python trigger). The only un-headless-testable bit is the ImGui tab
itself rendering — it rides the existing File>Open path.

DONE: `run_assembly_design` (run.cpp) — plans, builds each part in its own child doc (run_design),
doc_saves each to a .step+, creates an ASSEMBLY doc, inserts positioned fixed components
(asm_insert_component, transform=placement). agent_run routes design path through it. VERIFIED:
deterministic 2-part plumbing + full mock-driven flow (Base@[0,0,0] + Lid@[0,0,40] as components).
REMAINING (host GUI — needs the running app + an open question to resolve):
- [ ] 3c. Host opens the returned assembly doc as a tab. result carries {created_doc, assembly,
      doc_id}. Wire jobs.cpp/app.cpp to store the new doc_id + open it (addDocTab(true) + DocMirror).
      ⚠ OPEN QUESTION: the C++ core currently REJECTS opening assembly docs ("unsupported",
      session.cpp ~5354) and the host routes assembly-open to the Python worker — so a C++-created
      assembly (doc-cpp id) may not display without either (a) teaching the C++ core to open/rebuild
      assemblies, or (b) routing the open through Python. Investigate before wiring.
- [ ] 3d. Expose the CFD tool in the UI too: route chat requests mentioning cfd/wind-tunnel/drag to
      agent_simulate_v1 (mirror Ask mode), or add a Simulate action. (host, low risk)
- Control-surface interfaces: deferred (positioned components cover separable parts; hinged mates
  would need asm_add_mate + a hinge-axis from the planner).
Heaviest. Done when a multi-part request yields separate part docs assembled into an assembly doc
with positioned components, opened in the app.
- [ ] 3a. Map: doc_save (.step+ path), doc_new("assembly"), asm_insert_component via doc_apply_ops,
      how host consumes agent_run result (jobs.cpp / worker_client) to open a doc/tab. (Explore)
- [ ] 3b. run.cpp: when plan.assembly==true, build each part in a child doc, doc_save to a temp
      .step+ under run_dir, then create an assembly doc + asm_insert_component (transform=placement,
      first fixed). Return assembly doc_id + a created_doc marker.
- [ ] 3c. Host: open the returned assembly doc as an Assembly tab (addDocTab / TabKind::Assembly).
- [ ] 3d. Control-surface interface hint: planner emits hinge-line / split info per surface (kept
      simple = positioned components; no live mate solve required).
- [ ] 3e. Verify: a 2-part assembly request -> 2 part files + assembly doc referencing them.

---

## Working rules (autonomous)
- Commit a checkpoint after each verified increment (explicit paths, this branch, never master/-A).
- Build core: `cmake --build core/build-occt --config Release --target aicad` (ABSOLUTE paths — the
  shell cwd drifts). Stage: `build.bat`. Keep `ctest` (25/25) green before each commit.
- Test live via the libaicad ctypes harness; local model = LM Studio @ localhost:1234 (gemma-4-e4b
  fast / -26b slow over-reasons), Gemini flash for capable-model checks (mind free-tier rate limits).
- Don't break the working cloud path. Fail-soft everything new.
- If blocked on a piece, log it here + move to the next; don't stall.
