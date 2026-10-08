# contracts/ — frozen interface seams

This directory holds the **frozen interface contracts** for Chudware. These are
THE seams between layers: the C++ `core/` (`libchudware`), the Python `engine/`
oracle, the `services/worker` routing layer, and the UI. They were promoted here
from `docs/contracts/` in refactor node N0c so they sit at the top level next to
the code they bind, not buried under `docs/`.

## Frozen-seam policy

These contracts are **append-only**. Any change to an existing seam requires
**integrator sign-off** and an explicit version bump. Leaf agents may read them
freely but must not edit them. One owner per round per append-only file (see
`AGENTS.md` swarm rules).

## Frozen contracts

- `worker_rpc.md` — flat C ABI / worker RPC wire format + method catalogue.
- `doc_ops.md` — document op vocabulary, envelope, transactionality, errors.
- `doc_schema.md` — document tree schema (entities, handles, constraints).
- `selectors.md` — selector normative algorithm (weights, thresholds, tiebreak).
- `mesh_format.md` — AICM v1 mesh byte-level format.
- `stepplus.md` — STEP+ persistence format (required entries, load order, save).
- `events.md` — async event payloads (`sim_progress`, etc.).
- `feature_params.md` — per-feature-type param/field tables (PM form order).
- `reveng.md` — Reverse Engineering workspace (`aicad.reveng_session/1`, MCP tools, DAG templates).
- `sim_handoff.md` — cross-sim handoff & verify envelopes (`aicad.simhandoff/1`): study
  provenance refs, handoff (geometry + BCs/loads with units + provenance) and verify
  (quantity/tolerance/verdict) shapes; executable form `services/mcp/simhandoff.py`.
- `cam.md` — CAM simulation frames, G-code audit, machine profiles, job diff
  (`aicad.cam_sim_frames/1`, `aicad.cam_audit/1`, `aicad.machines/1`, `aicad.cam_job_diff/1`).
- `box_mirror.md` — optional Box.com cloud mirror of saved project files
  (`aicad.box_mirror/1`): config ownership, spool/drain semantics, `box_*` tool
  and worker-RPC envelopes.

## Agent-layer contracts (Pillar 20 / design pipeline)

- `advisor.md` — advisory layer: design ledger (`aicad.ledger/1`), roles, triggers,
  councils, tiers, effort presets, cognition ladder (`aicad.ladder/1`).
- `design_pipeline.md` — guided design-thinking pipeline (`aicad.designpipe/1`, T-DP1):
  the six-stage IO schemas (`feature`/`geometry_spec`/`linkage`/`build_node`), the new
  ledger event kinds (**extends `aicad.ledger/1`**), the ledger-reading stage-gate
  definitions, the effort/auto-mode hooks, and the geometry-kind → trigger bindings.
  Refines (does not replace) `advisor.md` §2/§20/§22/§23 and `phases.py`.

## Frozen subdirectories /附属 files

- `ecad/` — ECAD sub-contracts (`ecad_rpc.md`, `ecad_ops.md`, `sch_schema.md`,
  `pcb_schema.md`, `netlist.md`, `library.md`, `fab_outputs.md`, `sim.md`).
- `fixtures/` — golden contract fixtures (`box.aicm`, `part_bracket.json`,
  `asm_clamp.json`, `mock_replies_v2.json`, `make_box_aicm.py`).
- `icons.md` — icon atlas inventory (names, sizes, style, JSON shape).
- `licenses.md` — third-party license posture / obligations per dependency.

## Index addendum (2026-10-04) — full inventory and current seams

Appended under integrator delegation. Nothing above is edited; the frozen contract files
themselves are unchanged. This section completes the index and records where the seams
named in the header now live.

### Current seams

The header's layer list predates the cutovers. As of 2026-10-04:

- **`core/` (`libchudware`)** implements the C ABI and doc-worker method table of
  `worker_rpc.md` (`core/api/libchudware.{h,cpp}`). The host reaches it in-process or through
  the `chudware-hostd` daemon (`core/hostd/`); the C++ routing/IPC seam is `services/worker/`
  (`worker_client`, `engine_backend`, `cpp_core`) and `services/ipc/`.
- **Python services** (`services/**`) serve the remaining registered method families. hostd
  forwards engine-unknown methods to the Python worker `services/worker_main.py`
  (`core/hostd/worker_bridge.cpp`; see `web_parity.md`), split into agent and general lanes as
  `worker_rpc.md` specifies.
- **The Python `engine/` oracle and `engine_oracle/` no longer exist** (deleted in T-RF21,
  2026-07-23). Producer paths under `engine/…` or `engine_oracle/…` inside older contract prose
  are historical; the implementations now live in `core/` and `services/`.
- **Online agent runtime:** `agent_admin_turn` (`worker_rpc.md`) runs in
  `services/agent/worker_rpc.py`, which drives Chudcode through protocol-v2 RPC
  (`services/agent/chudcode_host.py`, binary pinned by `vendor/chudcode/CHUDCODE_ARTIFACT.lock`,
  posture in `licenses.md`). The Chudcode protocol is external and is not a contract in this
  directory. Online provider execution, sessions, memory, compaction and speech-to-text belong to
  that runtime (`docs/omp-integration/01-DECISIONS.md`, 2026-08-31 section); references to the
  deleted bun OMP bridge (`vendor/omp/…`) in older contract prose are superseded. Native C++
  providers (`providers.md`) remain for the explicit offline fallback.
- **UI:** the sole GUI is `host/` (desktop and WASM, see `web_parity.md`).
- **`docs/contracts/`** now holds only legacy fixture data kept as provenance; it is not a contract
  set and no current code reads it. The contracts live here.

### Complete inventory

Every contract file in this directory. Entries already described above are marked "(above)".

Core document, wire and event seams:

- `worker_rpc.md` (above) · `doc_ops.md` (above) · `doc_schema.md` (above) ·
  `selectors.md` (above) · `mesh_format.md` (above) · `stepplus.md` (above) ·
  `events.md` (above) · `feature_params.md` (above)
- `sketch3d_gui_contract.md` — 3D sketch (`sketch3d`) GUI/RPC surface, v1.
- `web_parity.md` — web/desktop parity split (`aicad.webparity/1`): which methods hostd serves
  from `libchudware` versus the Python worker fallback.
- `geom_labels.md` — canonical per-face geometry labels (`chudware.geomlabel/1`).
- `gsl_ser.md` — canonical GSL text serialization (`gsl-ser/v1`).

Agent layer:

- `advisor.md` (above) · `design_pipeline.md` (above)
- `agent_events.md` + `agent_events.json` — agent event kinds (`aicad.agent_events/2`); the JSON
  is the machine-readable kind list checked by `scripts/agent_events_audit.py`.
- `copilot_modes.md` + `copilot_modes.json` — copilot mode tool gating
  (`aicad.copilot_modes/1`); the JSON is read by `services/agent/copilot_mode_tools.py` and
  `core/agent/copilot_modes.cpp`.
- `interaction.md` — `ask_user` question/answer envelope (`aicad.interaction/1`).
- `providers.md` — provider and session layer, v1.
- `skills.md` — copilot skills, v1.
- `dag_schema.md` — DAG orchestrator schema (`aicad.dag/1`, `aicad.dag/2`).
- `ai_vc.md` — silent agent version control (AI-VC), v1.
- `requirements.md` — requirements layer (`aicad.req/1`).
- `research_tools.md` — `research_*` look-it-up tool answer envelope.
- `narration.md` — narration style guide for text-narration tools.
- `telemetry.md` — AI-artifact envelope and consent tiers, v1.
- `leaderboard.md` — model leaderboard data seam (`aicad.leaderboard/1`).

Domain seams:

- `reveng.md` (above) · `sim_handoff.md` (above) · `cam.md` (above) · `box_mirror.md` (above)
- `manufacturing_gateway.md` — manufacturing gateway wire and durable-state contract, v1.
- `firmware_rpc.md` — firmware toolchain RPC, v1.
- `structural_schema.md` — structural documents (`aicad.structure/1`).
- `hydraulic_schema.md` — ISO 1219 hydraulic circuits (`aicad.hydraulic/1`).
- `piping_schema.md` — piping documents (`aicad.piping/1`).
- `civil_schema.md` — civil / site / utility infrastructure model (`aicad.civil/1`):
  terrain, alignments, water + gravity networks, hydrology, structures, plant tie-ins.
- `deliverables.md` — spec-driven deliverable authoring (`deliverables/1`).
- `office.md` — reading and editing existing office files (`office/1`).
- `parts.md` — unified parts library (`aicad.parts/1`).
- `sourcing.md` — part sourcing fan-out (`aicad.part_source/1`).
- `sourcing_schema.md` — sourcing schema (`aicad.sourcing/1`).
- `fabrication_services.md` — quote-your-own-geometry fabrication services (`aicad.fab/1`).
- `plm.md` — PLM / version control (`aicad.plm/1`, `aicad.plm/2`).
- `plm_live.md` — PLM live-session protocol (`aicad.plm-live/1`).
- `plm_planning.md` — PLM planning (`aicad.planning/1`, `aicad.planning/2`).
- `cad_graph.md` — CAD project graph, v1.

Subdirectories, policy and assets:

- `ecad/` (above) — also contains `boardphys.md` (board physics sidecar and component
  engineering metadata, `aicad.boardphys/1`).
- `fixtures/` (above) — also contains `sim2_cantilever_result.aicm`, regenerated by hand with
  `core/tests/gen_sim2_result_fixture.cpp`.
- `icons.md` (above) · `licenses.md` (above)
- `license_debt.md` — license-debt register: shortcuts that must be resolved before any SaaS,
  commercial or centralized deployment.
