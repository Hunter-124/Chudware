# services/worker — engine routing seam

The routing seam between the host GUI and the engine: every host→engine call
funnels through here and is routed to in-process `libchudware` or the daemon
backend. The host compiles these sources directly (`host/CMakeLists.txt`).

## Sources (post-R0b: these four only)

- `worker_client.{h,cpp}` — JSON-RPC client and libchudware routing funnel
- `engine_backend.{h,cpp}` — backend seam: InProcCore vs daemon connection, IPC discovery
- `cpp_core.{h,cpp}` — in-process libchudware via flat C ABI
- `worker_router.{h,cpp}` — method/document routing policy

The stale duplicate copies of the host's live modules (async_rpc, doc_mirror,
jobs, sim_client, selection, doc_tabs, bootstrap, worker_types, doc_session,
pick_hit, worker_command_dispatcher) were deleted in R0b — the canonical
copies live in `host/src/core/*` and `host/src/bootstrap.cpp`.

## Host forwarding headers

- `host/src/worker_client.h` → `#include "../../services/worker/worker_client.h"`
- `host/src/core/cpp_core.h` → `#include "../../../services/worker/cpp_core.h"`
