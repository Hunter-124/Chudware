# Overnight autonomous build plan — agent sim + assembly + local hardening

**Branch:** `feature/copilot-overhaul-sim-assembly` (session-1 checkpoint = 38ac03eb)
**Goal:** Move toward "the AI designs a model airplane with control surfaces, assembles it, and runs
a CFD sim — usable offline/local." Build the three missing capabilities; commit a checkpoint after
each verified increment. The local-model *quality* ceiling is accepted — build so a capable model
(cloud now, big local later) can drive the workflow, and so local degrades gracefully.

Update the STATUS box of each workstream as I go (this file is my anti-amnesia anchor across context
resets — also mirrored in memory `agent-feature-vocabulary-airfoil-multipart` + the task list).

---

## WS1 — Harden the local/micro path  [STATUS: in progress]
Smallest, improves everything else's testability. Done when a local model's *failed* build reports
failure (not "done"), and common local-model JSON malformations self-heal.
- [ ] 1a. Fix micro soft-fail: `run_micro_design` reports status="done" even when rev never advanced /
      no geometry built. Make it surface failure when no feature applied. (core/agent/micro.cpp)
- [ ] 1b. JSON repair for local-model output: normalize the common malformations seen live
      (double-wrapped arrays e.g. `points:[[[x,y],...]]`, trailing commas, single quotes) in the
      ops/array extractor. (core/agent/ops_emit.cpp / micro.cpp extract_json_array)
- [ ] 1c. Re-verify: local 4B simple part still ok; revolve attempt now either builds or fails honestly.

## WS2 — Agent CFD/sim tool  [STATUS: not started]
The headline missing capability. Done when the agent can take the current geometry, set up a flow
study (inlet velocity, AoA), run it, and report lift/drag — driven from a natural-language request.
- [ ] 2a. Map sim infra: sim_caps, sim_run_flow / LBM / FoamRunner, the `studies` schema in
      document.json, how results (forces/lift/drag) come back. (Explore)
- [ ] 2b. Design the agent seam: a `sim_*` capability the agent loop can invoke (new core fn +
      libaicad RPC, callable from run.cpp like doc_* are) that builds a flow study from
      {velocity, aoa, fluid} and runs it, returning a summary.
- [ ] 2c. Add an "ask to simulate" path: detect a sim request (or a dedicated mode) and route to 2b;
      surface results in the run result + events.
- [ ] 2d. Verify: run a flow sim on a simple solid (sphere/wing) via the agent; sane drag number.

## WS3 — True assembly + part interfaces  [STATUS: not started]
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
