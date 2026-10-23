# services/worker — host→engine routing seam

Single client-side dispatch point for every host→engine call. It routes each call either to
`libchudware` (through the `chudware-hostd` daemon by default, or in-process) or to the
services worker. Wire contract: `contracts/worker_rpc.md`. Ongoing guidance:
- Repo rules, build and gate: `docs/dev/engineering-notes.md`.
- Agent lane and Chudcode turns: `docs/dev/chudcode-integration.md`.

## Modules (these three only)

- `worker_client.{h,cpp}` — `WorkerClient`, the funnel for every panel RPC.
  - `WorkerClient::callImpl` applies the routing predicates `routeDocToCpp`,
    `routeAgentToCpp`, `routeSimToCpp`, `routeEcadToCpp` and `routeFirmwareToCpp`, plus the
    C++-only CAM block. Engine-bound methods go to `engine::call`.
  - Every other method goes over the engine channel in daemon/WASM mode; hostd's
    `WorkerBridge` bridges it to its services worker. Only the legacy in-process backend
    uses the local worker child that `WorkerClient::start` launches.
  - Also owns the long-run/control split: `longRunMtx_` vs `callMtx_`, `callViaSideClient`,
    and `worker_control_info` cancel/steer.
- `engine_backend.{h,cpp}` — namespace `engine`, the backend seam.
  - Default: the daemon `EngineClient` over local IPC to `chudware-hostd`, with bounded
    connect retry (`CHUDWARE_ENGINE_DAEMON_RETRY_MS`).
  - Fallback: `CHUDWARE_ENGINE_DAEMON=0`, or exhausted retries, gives the in-process
    `CppCore`. A `daemon_draining` hello refusal never falls back: the GUI stays in
    daemon mode and connects when the old daemon exits.
  - `RunLane` gives agent and sim runs their own connections.
- `cpp_core.{h,cpp}` — `CppCore`: runtime loader for `libchudware` over the flat C ABI
  (`core/api/libchudware.h`).

Compiled by:
- `host/CMakeLists.txt`: the `Chudware` target and `test_worker_long_run`.
- `core/CMakeLists.txt`: `cpp_core.cpp` into `chudware-hostd`.

GUI document/job/bootstrap state lives in `host/src/core/*` and
`host/src/bootstrap.cpp`; do not duplicate it in this transport seam.

## Change rules

- The routing predicates in `worker_client.cpp` are shared by every panel; change them
  deliberately and keep `contracts/worker_rpc.md` in step. A new stateful agent RPC family
  also needs hostd's agent-lane prefix list (`WorkerBridge::forMethod`,
  `core/hostd/worker_bridge.cpp`).
- Depends on `core/api/libchudware.h` and `services/ipc/*` (IPC transport/discovery).
- `worker_client.cpp` includes `bootstrap.h` from `host/src`, where runtime provisioning
  lives. Do not reintroduce a local copy.
- Must NOT include ImGui or UI types.
