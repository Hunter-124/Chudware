# services/worker — host→engine routing seam

The client-side routing seam between the host GUI (desktop and WASM) and the engine. Every
host→engine call funnels through `WorkerClient`. It goes either to `libchudware` (through
the `chudware-hostd` daemon, or in-process) or to the services worker that hostd bridges.
The host compiles these sources directly (`host/CMakeLists.txt`). Contract:
`contracts/worker_rpc.md`. Rules and ownership: `AGENTS.md` in this directory.

## Sources (these three only)

- `worker_client.{h,cpp}` — `WorkerClient` and its routing predicates
  (`routeDocToCpp`/`routeAgentToCpp`/`routeSimToCpp`/`routeEcadToCpp`/`routeFirmwareToCpp`)
- `engine_backend.{h,cpp}` — `engine::` backend seam: daemon `EngineClient` (default) vs
  in-process `CppCore` (`CHUDWARE_ENGINE_DAEMON=0` or daemon unreachable), plus per-run lanes
- `cpp_core.{h,cpp}` — `CppCore`, the runtime `libchudware` loader over the flat C ABI

GUI document/job/bootstrap state is owned by `host/src/core/*` and
`host/src/bootstrap.cpp`; this directory owns routing, backend and loading only.

## Host forwarding headers

- `host/src/worker_client.h` → `#include "../../services/worker/worker_client.h"`
- `host/src/core/cpp_core.h` → `#include "../../../services/worker/cpp_core.h"`
