# services/worker — C++ engine routing seam (daemon client, libchudware loader, router)

Scope: services/worker/** (four compiled modules), contracts/worker_rpc.md
Contracts: contracts/worker_rpc.md (protocol 2, newline-delimited JSON-RPC), contracts/doc_ops.md (ops envelope), contracts/doc_schema.md (aicad.part/1 | aicad.asm/1)

> Purged 2026-07-22 (T-RF2): the stale duplicate modules (async_rpc/doc_mirror/jobs/
> sim_client/selection/doc_tabs/bootstrap/worker_command_dispatcher + worker_types/
> doc_session/pick_hit) lived here in NO build and were deleted. The live copies of those
> are compiled from `host/src/core/`. Only the four modules below remain.

## Entry points
- `services/worker/worker_client.{h,cpp}` — THE routing policy. Per-method/per-doc flags,
  all hardcoded `true` since T-RF21 closeout (the `CHUDWARE_CPP_*` escape hatches are
  removed): `cppDocsFlag()` (:111), `routeDocToCpp()` (:133, doc-id prefixes
  `doc-cpp`/`dwg-cpp`/`ecadcpp` commit a doc to C++ for its lifetime; also routes the
  T-RF18/19 ported sets — sourcing/BOM, hydraulic/piping/structural/tolerance/materials/dfm,
  parts catalog), `cppAgentFlag()`/`routeAgentToCpp()` (:312/:347),
  `cppSimFlag()`/`routeSimToCpp()` (:393/:399, FEA plus CFD `sim_run_flow`/`sim_cancel`),
  `cppEcadFlag()`/`routeEcadToCpp()` (:435/:442), `cppFirmwareFlag()`/`routeFirmwareToCpp()`
  (:481/:488, all 13 `fw_*` incl. the emulator leg), CAM block (:1440, C++-only, no Python
  fallback). Unported methods on a C++ doc fail loud; only minting calls declined with
  `code:"unsupported"` fall back to the Python services worker
  (`python -m services.worker_main` → `services/worker_main.py`).
- `services/worker/engine_backend.{h,cpp}` — daemon seam above cpp_core. `CHUDWARE_ENGINE_DAEMON`
  (default 1) routes via `EngineClient` over the chudware-hostd unix socket (`core/hostd/`,
  multi-client, push events, edit holds); `=0` = legacy in-proc dlopen; daemon failure falls
  back to in-proc. The WASM web client reaches the same daemon via gateway WS `/engine`.
- `services/worker/cpp_core.{h,cpp}` — runtime dlopen of libchudware. Resolve order:
  `$CHUDWARE_LIBCHUDWARE`/`$CHUDWARE_LIBCHUDWARE` → compile-time `CHUDWARE_DEFAULT_LIBCHUDWARE` → exe-adjacent
  → loader path. Flat C ABI: `chudware_call` (JSON in/out), `chudware_free`,
  `chudware_set_event_sink`, `chudware_abi_version`, `chudware_build_id`; nonzero return = unknown
  method → Python fallback. Method table lives in `core/api/libchudware.cpp` (~270 branches).
- `services/worker/worker_router.{h,cpp}` — thin policy mirror (`routeMethod`, `isCppDocId`).

## Current backend split (2026-08, post T-RF21 closeout)
- **C++ by default** (T-RF15..T-RF21 are all `:::done` in work/DAG.md): docs (`doc_*`,
  `config_*`, `export_*`, `weld_*`, `eval_*`, drawings), agent v2 (`agent_run_v2`/
  `agent_ask_v2`/`agent_clarify_v1`/`agent_pm_assist_v1`/`agent_simulate_v1`/
  `agent_mission_v1`/`agent_cancel`/`agent_steer`/`get_prompts_v2`) plus the C++-only
  provider/ASR seams (`agent_provider_*`, `agent_stt_*`, `agent_models_list`,
  `agent_cost_summary`, `agent_cap_raw`), FEA + CFD (`sim_mesh`,
  `sim_run_static/modal/fatigue/thermal/flow`, `sim_cancel` — T-RF16), CAM (`cam_*`),
  ECAD `ecad_*` (T-RF17 closed the autoroute doc-id form/`to_3d`/`fill_zones`/`simulate`/
  KiCad-import gaps), firmware `fw_*` (all 13, T-RF15), sourcing/BOM/cost/spec-match
  (`bom_rollup`/`bom_price`/`request_quote`/`match_part_number`/`vendor_part_fetch`/
  `spec_sheet_match`/`list_spec_templates`, T-RF18), hydraulic/piping/structural/tolerance/
  materials/dfm plus `part_place`/`part_import_file` (T-RF19), parts catalog
  (`parts_catalog_list`/`parts_make`), `doc_formats`, `vendor_part_insert`.
- **Python only** (services worker, `services/worker_main.py` — see
  work/context/engine-python.md): `run_model` (build123d via `services/kernel_exec.py`),
  `ecad_design` (an LLM state machine deferred to the agent seam; its deterministic recipe
  fallback is already C++), `sim_caps` and the unlisted sim methods, parts-hub FTS and
  vendor registry reads, deliverables, reveng, DAG steering, PLM, EM/multiphysics,
  intake/vision/research/render, agent GUI seams. `agent_design` was deleted at T-RF5 — the
  v2 run is the only agent path.
- Escape hatches: none left — the `CHUDWARE_CPP_DOCS`/`CHUDWARE_CPP_AGENT`/
  `CHUDWARE_CPP_SIM`/`CHUDWARE_CPP_ECAD`/`CHUDWARE_CPP_FIRMWARE` flags were all removed at
  T-RF21 closeout; every `cpp*Flag()` now returns `true` unconditionally.

## How it connects
GUI (host/src/core/{doc_mirror,async_rpc,jobs,...}) → `worker_client.call(method, params)` →
routing policy → `engine_backend` (hostd daemon → libchudware) or the Python services
worker (`services/worker_main.py`, spawned as `python -m services.worker_main`) stdio
JSON-RPC.
Ops responses (`{revision, tree, feature_status, owner_map, tess.aicm_path}`) update the
DocMirror on the GL thread; the AICM mesh is parsed from the path by
`host/src/render/aicm_loader.cpp` (a duplicate of the kept `ui/viewport/render/aicm_loader.cpp`
compiled into core tests/tools — merging them is deferred).
