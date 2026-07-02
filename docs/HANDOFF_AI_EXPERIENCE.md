# HANDOFF — AI-Experience DAG (session 2026-07-01/02)

For the agent merging this lane into its current workflow. Everything below is
committed on `master` unless marked **TODO**. Full execution spec (all designs
pre-decided): the plan sections quoted inline here are self-sufficient; the
original plan lived in a session-local file, so THIS document is the surviving
source of truth. Repo swarm rules in `AGENTS.md` apply (disjoint write-sets,
handoff paragraphs to `docs/REFACTOR_LOG.md`, no worktrees).

## What this lane is

Three goals: (1) translate geometry/sketch/feature placement into coherent
TEXT for non-vision LLMs; (2) intra-part FEATURE-OVERLAP detection (analog of
assembly interference, which already existed); (3) REVERSE-ENGINEERING:
STEP/mesh import → editable parametric feature tree, verified against the
source solid by boolean comparison. Plus: dataset factory + gated GNN adapter
scaffold, scraped knowledge cheatsheets, in-app AI hooks.
Binding prior art: `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` §3.4/§3.6
(deterministic first; learned model only after a measured residual; heuristics
never suppress the raw face list).

## DONE and committed (commits `6059dc3`, `dcedf4b`, `c1eda94` + integrator appends)

- **Core (C++, libaicad)**
  - `CompileResult::tool_recs` — per-feature pre-boolean tool solids captured
    at 16 callsites in `core/doc/document.cpp`.
  - RPC `eval_feature_overlap` `{doc_id, features?, min_volume_mm3?}` →
    `{ok, summary, pairs:[{a,b,a_name,b_name,kind,volume_mm3}], dead_features}`;
    kinds `boss_overlap|cut_overlap|cut_into_boss|boss_swallowed` (only
    boss_swallowed ⇒ ok:false). `core/doc/session.cpp`.
  - `eval_interference` new params `include_coincident` (→ `contacts` array),
    `min_clearance_mm` (→ `warning:"below min clearance"`).
  - `core/doc/recon.{h,cpp}`: RPC `eval_compare` `{doc_a,doc_b,body_a?,body_b?}`
    → match_pct / common/missing/extra mm³ / residual_regions / degraded mode
    (fuzzy 1e-4); RPC `doc_face_wires` `{doc_id, face:{sid}|selector}` →
    planar face wires as doc-schema sketch entities in the face plane frame.
  - Tests: `core/tests/test_interference.cpp` (ctest target `interference`,
    8 cases / 47 asserts, green).
  - `core/agent/run.cpp`: `reverse_engineer` mission capability (injects
    eval_compare feedback into reviewer context); `services/worker/
    worker_client.cpp` routes `eval_compare` (doc_a/doc_b sniff) to C++.
- **MCP surface (`services/mcp/`)**
  - `gsl_tools.py`: `describe_sketch` (plane/entities/constraints/regions/DOF
    text), `describe_scene` (per-view visible-face text render; occlusion
    disclaimer), `_scene_caption(doc_id, views)` helper, feature-PLACEMENT
    narrative in `describe_part` (+ "owns no surviving faces" dead-feature
    line).
  - `cad_tools.py`: `check_feature_overlap` tool; `add_features` now attaches
    `overlap_warnings` (batch-scoped, best-effort); `check_interference(
    include_coincident, min_clearance_mm)`; `render_part`/`contact_sheet`
    return `[Image, caption]` (mcp 1.28.1 flattens lists); cad_guide topics
    `gsl` + `reverse` (reverse lazy-imports `recon_tools._RE_GUIDE`,
    fallback string until recon_tools lands).
  - `server.py` instructions mention the non-vision path.
- **Prompts/knowledge**: `reviewer.md` non-vision review procedure;
  `loop.py` `_no_vision_note`; cheatsheets `fits/threads/bearings/materials.md`
  (auto-discovered by cad_guide); `tools/scrape_knowledge.py` (Firecrawl REST
  iff `FIRECRAWL_API_KEY`, else urllib; drafts to /tmp, never overwrites).
- **Dataset/ML groundwork**: `tools/gsl_dataset_gen.py` (7 families,
  owner_map face labels; 350/350 stress-verified; `corpora/gsl_dataset/`
  gitignored, README documents layout); `tools/gsl_gnn/` scaffold
  (GraphSAGE, lazy imports, hard go/no-go gate in README — do NOT train
  before `docs/RECOGNITION_RESIDUAL.md` exists and flags a class).
- **Contracts/registry (integrator)**: `contracts/worker_rpc.md` §Recognition
  & overlap; `contracts/feature_params.md` import.embedded_brep;
  `ui/abstract/command_registry.cpp` rows `eval_feature_overlap`,
  `ai_reverse_engineer`, `ai_explain_part`.
- Staged dist refreshed: `services/dist/AI-CAD/libaicad.so` (core_bridge's
  FIRST candidate — restage after any core rebuild or new RPCs vanish).

## TODO — remaining nodes (specs are decision-complete; execute as written)

### 1. RECON-TOOLS (highest value; agent died mid-flight, NO file landed)
New `services/mcp/recon_tools.py` + register in `server.py` (after GSL block)
+ new `services/agent/prompts/shared/cheatsheets/reverse_engineer.md`.
Must export `_RE_GUIDE` (cad_guide('reverse') already lazy-imports that name).
Tools (copy `check_interference`'s `_doc/_dumps/_err` pattern from cad_tools):
- `_recognize(ref_doc_id)` — 5 deterministic passes over `doc_query` faces
  (sid/surf/side/normal/concavity/radius/centroid/area) + `doc_adjacency` +
  `doc_bbox`: (1) prismatic base: largest planar face + antiparallel partner
  at distance d, laterals ⊥/∥ explain ≥80% remaining area → profile from
  `doc_face_wires`, sketch + extrude d; (2) turned body: all curved faces
  coaxial → ANALYTIC revolve profile from face params (no sectioning);
  (3) holes: bore-concavity cylinders coaxial → hole {d=2r, depth|through};
  (4) patterns: `gsl_tools._axis_order` on same-radius groups → seed +
  circular/linear pattern (import, don't copy); (5) fillet/chamfer
  CANDIDATES (convex-edge cylinders/tori r ≤ 20% min bbox dim; 45°±2°
  bridges) — apply only if a plain-word edge query resolves on the REBUILT
  body, else report under `candidates`. Unclaimed faces always reported.
- `reverse_engineer(source, name=None)` — source = path or doc_id; import →
  recognize → new part → apply batches (base → holes → patterns → fillets)
  with the checkpoint/revert pattern add_features uses; after each batch
  `eval_compare(new, ref)`; revert on match_pct regression → `rejected`.
  Return `{ok, doc_id, ref_doc_id, match_pct, features, rejected, candidates,
  unexplained:{missing_mm3, extra_mm3, regions}, next_steps}`. Mesh input →
  "facet geometry: expect low deterministic yield" note. No solids → error.
- `compare_parts(doc_a, doc_b, body_a=None, body_b=None)` — thin
  eval_compare wrapper (docstring: world-frame assumption, no ICP).
- `convert_face_to_sketch(face, doc_id=None, sketch_name=None)` —
  doc_face_wires → one sketch feature on that face (refs.plane sel(face)).
Acceptance: plate 100×60×8 + 4×⌀6 bolt-circle + r3 fillet → STEP →
reverse_engineer ≥97% match, hole/pattern recovered, fillet ≥ candidates;
edit recovered hole ⌀6→8 → recompiles, match drops (editability proof);
turned shaft ≥95%; STL smoke completes honestly.

### 2. TESTS-MCP
New `services/mcp/test_gsl_tools.py`, `services/mcp/test_recon_tools.py`,
append to `services/mcp/test_cad_tools.py` (pattern: existing `_call` asyncio
wrapper). Cover: describe_part/hover/query_adjacency/describe_sketch/
describe_scene happy paths (asserted substrings: entity ids + DOF verdict;
bore sid under top view + occlusion disclaimer; placement line);
check_feature_overlap clean + boss_swallowed; add_features overlap_warnings;
include_coincident contacts; render_part → [ImageContent, TextContent];
the RE round-trip above as `test_recon_tools.py`.
Run: `python3 -m pytest services/mcp/test_gsl_tools.py services/mcp/test_recon_tools.py -q`
and `-k "overlap or coincident or caption"` on test_cad_tools.py.

### 3. EVAL (after RECON-TOOLS)
New `tools/gsl_eval.py` → `docs/RECOGNITION_RESIDUAL.md`: for each
`corpora/gsl_dataset` sample: export STEP, re-import (tree stripped), run the
recon pipeline, score {match_pct, pct_faces_claimed, features_recovered}.
Plus `corpora/sw_fixtures/*.step` if present. Verdict per family with the
gate: adapter training justified only where median match_pct < 90% or
faces-claimed < 70%. This report is the GNN go/no-go input.

### 4. E3 in-app UI hooks — NOW YOURS (GUI session)
`app/src/...` turned out to be symlinks into `ui/imgui/src/...` (your tree),
so this lane backed off; a partial `doc_engine.h` edit was REVERTED.
Wire, in your own style (registry rows + agent capability already exist):
- Import summary: after a foreign-format open (`Engine::open`,
  `ui/imgui/src/app/doc_engine.cpp` ~L115), toast/agent-block: body count +
  bbox + "Ask the copilot to reverse-engineer this into editable features."
- Copilot quick-action chip "Reverse-engineer import" in `agent_panel.cpp`,
  visible when the tree has an `import` feature; fires the startAgent path
  (`app_state.cpp:391-408`) with request "Rebuild the imported body as an
  editable parametric feature tree; verify with eval_compare; report match %."
  (Optionally pass capability `reverse_engineer` — run.cpp handles it.)
- Commands `ai_reverse_engineer` / `ai_explain_part` (registry rows exist,
  Group::AI) → dispatch to the same canned agent requests.
- Also relayed from the user (GUI-polish lane, see REFACTOR_LOG "RELAY →
  GUI session"): pill backgrounds for all viewport text/icons; sliders for
  bounded PM params (angles, draft, fillet radius, pattern count); cleanup of
  viewport indicators (sim setup glyphs, previews, drag arrows).

### 5. Final integrator pass
`bash scripts/gate.sh` (NOTE: `feature_surface_audit` + `parity_audit`
failures are PRE-EXISTING baseline — verified by stash-diff — not this lane);
`ctest --test-dir core/build-linux -R interference`; MCP pytest above;
re-stage `services/dist/AI-CAD/libaicad.so` after core rebuilds; commit scoped
(never scoop the other lane's dirty files); append a status note to
`docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` header (Stages 0–2a were already
shipped; this lane added write-warnings, compare, face-wires, RE pipeline).

## Gotchas discovered (save yourself the hour)

- `core_bridge` lib resolution prefers `services/dist/AI-CAD/libaicad.so` —
  ALWAYS restage after rebuilding core, or new RPCs report "unknown method".
- `doc_query` face matches NEVER populate `owner_feature` on the plain query
  path — join `doc_tree.owner_map` `[body_id, topo_id]` pairs against match
  `hint_index+1` (same describe_faces enumeration order).
- Sketch RPCs take the id under JSON key `"sketch"` (not `sketch_fid`).
- Box features require `length/width/height` params (`cb.feat`/`cb.dim`).
- mcp 1.28.1 `_convert_to_content` flattens list returns → [Image, text] works.
- ctest parallel harness has 3 pre-existing flaky tests (appearance,
  drawing_gdt, hole) — they pass standalone.

## Kickoff

Read this file, claim the TODOs in order (RECON-TOOLS → TESTS-MCP → EVAL;
E3 whenever it fits your GUI queue), keep write-sets disjoint from any other
running lane, append handoff paragraphs to `docs/REFACTOR_LOG.md`, commit
scoped. Everything already committed is verified live — build on it, don't
re-audit.
