# contracts/ — frozen interface seams

This directory holds the **frozen interface contracts** for Chudware: the seams between the
C++ engine (`core/`, `libchudware`), the engine daemon (`chudware-hostd`), the host-side
routing layer (`services/worker/`, `services/ipc/`), the Python services (`services/`), and
the GUI (`host/`, desktop and WASM).

## Change policy

Contracts are **append-only**. A change to an existing frozen section requires an explicit
version bump of the schema or contract it belongs to (for example `aicad.plm/1` →
`aicad.plm/2`) and a note describing the change; additive amendments are recorded in the
contract itself. A new module ships its contract in the same change that introduces it.
Machine-readable files (`*.json`) are checked by the audits and tests that consume them, so
update the producer, the consumers, and the contract together.

Schema ids keep the historical `aicad.` prefix for data compatibility.

## Current seams

- **`core/` (`libchudware`)** implements the C ABI and doc-worker method table of
  `worker_rpc.md` (`core/api/libchudware.{h,cpp}`). The host reaches it in-process or through
  the `chudware-hostd` daemon (`core/hostd/`); the C++ routing/IPC seam is `services/worker/`
  (`worker_client`, `engine_backend`, `cpp_core`) and `services/ipc/`.
- **Python services** (`services/**`) serve the remaining registered method families. hostd
  forwards engine-unknown methods to the Python worker `services/worker_main.py`
  (`core/hostd/worker_bridge.cpp`; see `web_parity.md`), split into agent and general lanes as
  `worker_rpc.md` specifies.
- **No Python engine.** The former Python `engine/` oracle and `engine_oracle/` packages no
  longer exist; producer paths under `engine/…` or `engine_oracle/…` in older contract prose
  are historical, and the implementations now live in `core/` and `services/`.
- **Online agent runtime:** `agent_admin_turn` (`worker_rpc.md`) runs in
  `services/agent/worker_rpc.py`, which drives the [Chudcode](https://github.com/Hunter-124/chudcode)
  runtime through its RPC protocol (`services/agent/chudcode_host.py`, binary pinned by
  `vendor/chudcode/CHUDCODE_ARTIFACT.lock`, license row in `licenses.md`). The Chudcode
  protocol is external and is not a contract in this directory; online provider execution,
  sessions, memory, compaction, and speech-to-text belong to that runtime. References to the
  former bun OMP bridge (`vendor/omp/…`) in older contract prose are superseded. Native C++
  providers (`providers.md`) remain for the explicit offline fallback. Boundary notes:
  [docs/dev/chudcode-integration.md](../docs/dev/chudcode-integration.md).
- **UI:** the sole GUI is `host/` (desktop and WASM, see `web_parity.md`).

## Inventory

Core document, wire, and event seams:

- `worker_rpc.md` — flat C ABI / worker RPC wire format and method catalogue.
- `doc_ops.md` — document op vocabulary, envelope, transactionality, errors.
- `doc_schema.md` — document tree schema (entities, handles, constraints).
- `selectors.md` — selector normative algorithm (weights, thresholds, tiebreak).
- `mesh_format.md` — AICM v1 mesh byte-level format.
- `stepplus.md` — STEP+ persistence format (required entries, load order, save).
- `events.md` — async event payloads (`sim_progress`, etc.).
- `feature_params.md` — per-feature-type param/field tables (property-panel form order).
- `sketch3d_gui_contract.md` — 3D sketch (`sketch3d`) GUI/RPC surface, v1.
- `web_parity.md` — web/desktop parity split (`aicad.webparity/1`): which methods hostd serves
  from `libchudware` versus the Python worker fallback.
- `cell_methods.json` — the method set a `chudware-hostd --cell` tenant cell admits for the
  browser app.
- `gsl_ser.md` — canonical GSL text serialization (`gsl-ser/v1`).

Agent layer:

- `advisor.md` — advisory layer: design ledger (`aicad.ledger/1`), roles, triggers,
  councils, tiers, effort presets, cognition ladder (`aicad.ladder/1`).
- `design_pipeline.md` — guided design-thinking pipeline (`aicad.designpipe/1`): the
  six-stage IO schemas (`feature`/`geometry_spec`/`linkage`/`build_node`), the ledger event
  kinds it adds to `aicad.ledger/1`, the stage-gate definitions, the effort/auto-mode hooks,
  and the geometry-kind → trigger bindings. Refines `advisor.md` and
  `services/agent/pipeline/phases.py`.
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

- `reveng.md` — Reverse Engineering workspace (`aicad.reveng_session/1`, MCP tools, DAG
  templates).
- `sim_handoff.md` — cross-sim handoff and verify envelopes (`aicad.simhandoff/1`): study
  provenance refs, handoff (geometry + BCs/loads with units + provenance) and verify
  (quantity/tolerance/verdict) shapes; executable form `services/mcp/simhandoff.py`.
- `cam.md` — CAM simulation frames, G-code audit, machine profiles, job diff
  (`aicad.cam_sim_frames/1`, `aicad.cam_audit/1`, `aicad.machines/1`, `aicad.cam_job_diff/1`).
- `box_mirror.md` — optional Box.com cloud mirror of saved project files
  (`aicad.box_mirror/1`): config ownership, spool/drain semantics, `box_*` tool and
  worker-RPC envelopes.
- `manufacturing_gateway.md` — manufacturing gateway wire and durable-state contract, v1.
- `firmware_rpc.md` — firmware toolchain RPC, v1.
- `structural_schema.md` — structural documents (`aicad.structure/1`).
- `hydraulic_schema.md` — ISO 1219 hydraulic circuits (`aicad.hydraulic/1`).
- `piping_schema.md` — piping documents (`aicad.piping/1`).
- `drives_schema.md` — hydraulic motor sizing and pump drive selection RPCs
  (`hydraulic_size_motor`, `pump_drive_size`).
- `industrial_schema.md` — industrial process sizing RPCs (`vessel_size`, `duct_size`).
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

Subdirectories, policy, and assets:

- `ecad/` — ECAD sub-contracts (`ecad_rpc.md`, `ecad_ops.md`, `sch_schema.md`,
  `pcb_schema.md`, `netlist.md`, `library.md`, `fab_outputs.md`, `sim.md`, and
  `boardphys.md` for the board physics sidecar and component engineering metadata,
  `aicad.boardphys/1`).
- `fixtures/` — golden contract fixtures (`box.aicm`, `part_bracket.json`, `asm_clamp.json`,
  `mock_replies_v2.json`, `make_box_aicm.py`, and `sim2_cantilever_result.aicm`, regenerated
  with `core/tests/gen_sim2_result_fixture.cpp`).
- `icons.md` — icon atlas inventory (names, sizes, style, JSON shape).
- `licenses.md` — dependency license ledger: license, role, and posture per dependency.
- `license_debt.md` — open licensing and compliance items that must be resolved before the
  affected component ships in a distributed build.
