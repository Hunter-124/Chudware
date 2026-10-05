# services/worker — host→engine routing seam (WorkerClient, engine backend, libchudware loader)

Scope: services/worker/** (three compiled modules), services/ipc/**, core/hostd/worker_bridge.*
Contracts:
- contracts/worker_rpc.md: protocol 2, newline-delimited JSON-RPC.
- contracts/doc_ops.md: ops envelope.
- contracts/doc_schema.md: aicad.part/1 | aicad.asm/1.
- contracts/web_parity.md: methods the web client needs served by libchudware.
Canonical guidance:
- services/worker/AGENTS.md: module ownership.
- .claude/skills/chudware-dev/SKILL.md: daemon and worker-lane invariants.
- .claude/skills/cad-chudcode-integration/SKILL.md: `agent_admin_turn`, cancel and steer.

## Entry points
- `services/worker/worker_client.{h,cpp}` — `WorkerClient`, the funnel for every GUI RPC.
  - `WorkerClient::callImpl` routes by predicate: `routeDocToCpp`, `routeAgentToCpp` (also
    translates host agent RPC names to core names), `routeSimToCpp` +
    `isNativeOnlySimMethod`, `routeEcadToCpp`, `routeFirmwareToCpp`, and the C++-only CAM
    block. Matching methods go to `engine::call`.
  - Core failures surface to the caller as a structured error in `result`, or via the
    error sink. There is no fallback from the core to the services worker.
  - All other methods go over the engine channel in daemon/WASM mode, and hostd bridges
    them to its services worker. Only the legacy in-process backend uses a local worker
    child launched by `WorkerClient::start` (`python -m services.worker_main`; packaged
    builds run the adjacent compiled worker with `--module worker`).
- `services/worker/engine_backend.{h,cpp}` — the `engine::` seam.
  - Default: the daemon `EngineClient` to `chudware-hostd` (`core/hostd/`, multi-client,
    push events, edit holds), with bounded connect retry
    (`CHUDWARE_ENGINE_DAEMON_RETRY_MS`).
  - Fallback: `CHUDWARE_ENGINE_DAEMON=0`, or exhausted retries, gives the in-process
    `CppCore`.
  - `RunLane` (`Agent`/`Sim`) gives long runs their own connection.
  - `DaemonConnState maintainConnection()` reports the link state.
  - The WASM client reaches the same daemon through the gateway WebSocket `/engine`
    (`Webservice/gateway/engine_bridge.py`).
- `services/worker/cpp_core.{h,cpp}` — `CppCore`, the runtime `libchudware` loader.
  - Developer builds accept explicit, build-tree and loader-search candidates.
  - Production loads only the canonical signed-manifest payload.
  - Flat C ABI (`core/api/libchudware.h`): `chudware_call`, `chudware_free`,
    `chudware_set_event_sink`, `chudware_set_doc_publish_sink`, `chudware_abi_version`,
    `chudware_build_id`. The method dispatch lives in `core/api/libchudware.cpp`.
- `services/ipc/ipc_client.{h,cpp}` — `EngineClient`: connect/hello, calls, queued
  pushes and `reconnectGeneration` (clients must re-subscribe after reconnect).
- `services/ipc/ipc_transport*` — POSIX sockets, Windows pipes and WASM WebSocket
  transport; `ipc_discovery*` owns binary/instance discovery and single-instance locks.
  Developer overrides never weaken the packaged signed-payload lookup.
- `core/hostd/server.cpp`, `doc_registry.*` — shared native engine lifetime,
  per-client document attachment, events, autosave, work-based idle/drain and doc GC.
- `core/hostd/worker_bridge.cpp` `WorkerBridge::forMethod` — hostd's two services-worker
  lanes. The agent families prefix-match onto the agent lane (`agent_`, `chudcode_`,
  `session_`, `subagent_`, `dag_`, …); everything else goes to the general lane.
- `services/worker_main.py` `_METHODS` + `_register(...)` — the services-worker dispatch
  table. The `agent_gui` (`services.agent.worker_rpc`) and `subagent` tables are REQUIRED,
  and a failed import fails worker startup. The others degrade with a warning: vendor
  parts, deliverables, parts hub, reveng, DAG, PLM, EM, multiphysics and mfg printers.

## How it connects
GUI (`host/src/core/{doc_mirror,async_rpc,jobs,...}`) → `WorkerClient::call` → route
predicates.
- Engine-bound calls: `engine::call` → hostd → libchudware.
- Everything else: hostd `WorkerBridge` → `services/worker_main.py`.

Ops responses (`{revision, tree, feature_status, owner_map, tess.aicm_path}`) update the
DocMirror on the GL thread. The AICM mesh at that path is parsed by
`host/src/render/aicm_loader.cpp`, the one live AICM parser; core tests and tools reuse it.

## Debugging playbook
- Locks: `callMtx_` serializes ordinary calls. In daemon mode, long runs
  (`agent_admin_turn`/`agent_run_v2`/`agent_mission_v1`) hold `longRunMtx_` instead, so no
  GUI call waits on a turn client-side.
- Lane contention: an engine-bound call whose lane is busy takes `callViaSideClient`.
  Minting calls never take the side connection, because hostd attaches the minted doc to
  the minting connection.
- Cancel/steer during `agent_admin_turn`: these use the authenticated worker control
  endpoint from `worker_control_info` (`workerControlMtx_`, `callAgentCancel`,
  `callAgentSteer`), never the blocked stdio lane.
- hostd fails a non-turn call behind a running turn at once with `worker_lane_busy`.
  A new stateful agent RPC family must be added to `WorkerBridge::forMethod`'s agent
  prefixes, or it lands on the general lane (a different process).
- Never stop a daemon serving the user's GUI or agent children. Stale-binary drain
  is soft; clean shutdown is not project save. Mission terminal persistence saves
  touched agent documents and never closes or overwrites the user's open documents.
- `CHUDWARE_RPC_TRACE=<file>` appends a redacted per-call trace from `WorkerClient::call`.
- Tests: host `test_worker_long_run` (fake hostd, developer builds), `test_worker_control`,
  `test_async_rpc`; hostd lifecycle `scripts/hostd_smoke.py`; web parity
  `scripts/web_parity_probe.py`.

## Graphify starters
- graphify query "WorkerClient callImpl routeDocToCpp engine_backend daemon"
- graphify explain "WorkerBridge"
- graphify path "worker_client.cpp" "worker_main.py"

## Related tasks/docs
- docs/reference/HOSTD.md (daemon design, drain/replace, instance keys)
- work/context/engine-python.md (services worker side), work/context/gui-host.md (GUI callers)
