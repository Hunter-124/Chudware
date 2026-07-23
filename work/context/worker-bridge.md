# services/worker — C++ engine routing seam (daemon client, libchudware loader, router)

Scope: services/worker/** (four compiled modules), contracts/worker_rpc.md
Contracts: contracts/worker_rpc.md (protocol 2, newline-delimited JSON-RPC), contracts/doc_ops.md (ops envelope), contracts/doc_schema.md (aicad.part/1 | aicad.asm/1)

> Purged 2026-07-22 (T-RF2): the stale duplicate modules (async_rpc/doc_mirror/jobs/
> sim_client/selection/doc_tabs/bootstrap/worker_command_dispatcher + worker_types/
> doc_session/pick_hit) lived here in NO build and were deleted. The live copies of those
> are compiled from `host/src/core/`. Only the four modules below remain.

## Entry points
- `services/worker/worker_client.{h,cpp}` — THE routing policy. Per-method/per-doc flags:
  `cppDocsFlag()` (:101, env `CHUDWARE_CPP_DOCS`, default 1), `routeDocToCpp()` (:120, doc-id
  prefixes `doc-cpp`/`dwg-cpp`/`ecadcpp` commit a doc to C++ for its lifetime),
  `cppAgentFlag()`/`routeAgentToCpp()` (:190/:222), `cppSimFlag()`/`routeSimToCpp()`
  (:257/:263), `cppEcadFlag()`/`routeEcadToCpp()` (:295/:305), CAM block (:988, C++-only,
  no Python fallback). Unported methods on a C++ doc fail loud; only minting calls declined
  with `code:"unsupported"` fall back to the Python oracle subprocess (`python -m engine.worker`).
- `services/worker/engine_backend.{h,cpp}` — daemon seam above cpp_core. `CHUDWARE_ENGINE_DAEMON`
  (default 1) routes via `EngineClient` over the chudware-hostd unix socket (`core/hostd/`,
  multi-client, push events, edit holds); `=0` = legacy in-proc dlopen; daemon failure falls
  back to in-proc. The WASM web client reaches the same daemon via gateway WS `/engine`.
- `services/worker/cpp_core.{h,cpp}` — runtime dlopen of libchudware. Resolve order:
  `$CHUDWARE_LIBCHUDWARE`/`$CHUDWARE_LIBCHUDWARE` → compile-time `CHUDWARE_DEFAULT_LIBCHUDWARE` → exe-adjacent
  → loader path. Flat C ABI: `chudware_call` (JSON in/out), `chudware_free`,
  `chudware_set_event_sink`, `chudware_abi_version`, `chudware_build_id`; nonzero return = unknown
  method → Python fallback. Method table lives in `core/api/libchudware.cpp` (~248 branches).
- `services/worker/worker_router.{h,cpp}` — thin policy mirror (`routeMethod`, `isCppDocId`).

## Current backend split (2026-07)
- **C++ by default**: docs (`doc_*`, `config_*`, `export_*`, `weld_*`, `eval_*`), agent v2
  (`agent_run_v2`/`agent_ask_v2`/`agent_clarify_v1`/`agent_pm_assist_v1`/`agent_mission_v1`/
  cancel/steer/`get_prompts_v2`), FEA (`sim_mesh`, `sim_run_static/modal/fatigue/thermal`),
  CAM (`cam_*`), parts catalog, ECAD (Linux/macOS; Windows default off).
- **Python only** (until the T-RF15+ zero-Python stream lands): CFD (`sim_run_flow`,
  `sim_cancel`), firmware (`fw_*`), ECAD gaps (autoroute doc-id form/`to_3d`/`fill_zones`/
  `simulate`/`design`/kicad-import), BOM/sourcing/parts-hub, hydraulic/piping/intake/vision/
  research/render/structural/tolerance/vendor_parts, legacy `agent_design`.
- Escape hatches: `CHUDWARE_CPP_DOCS=0`, `CHUDWARE_CPP_AGENT=0`, `CHUDWARE_CPP_SIM=0`,
  `CHUDWARE_CPP_ECAD=0` (all removed at T-RF21 closeout).

## How it connects
GUI (host/src/core/{doc_mirror,async_rpc,jobs,...}) → `worker_client.call(method, params)` →
routing policy → `engine_backend` (hostd daemon → libchudware) or Python oracle stdio JSON-RPC.
Ops responses (`{revision, tree, feature_status, owner_map, tess.aicm_path}`) update the
DocMirror on the GL thread; the AICM mesh is parsed from the path by
`host/src/render/aicm_loader.cpp` (a duplicate of the kept `ui/viewport/render/aicm_loader.cpp`
compiled into core tests/tools — merging them is deferred).
