# services/worker — engine routing seam (libaicad vs Python oracle)

The routing seam between the host GUI and the engine: every host→engine call
funnels through here and is routed to in-process `libaicad` or the Python
oracle subprocess (`engine_oracle/` via JSON-RPC). The host compiles these
sources directly (`host/CMakeLists.txt`).

## Sources (post-R0b: these four only)

- `worker_client.{h,cpp}` — JSON-RPC to engine_oracle subprocess (+ libaicad routing)
- `engine_backend.{h,cpp}` — backend seam: InProcCore vs daemon conn, IPC discovery
- `cpp_core.{h,cpp}` — in-process libaicad via flat C ABI
- `worker_router.{h,cpp}` — method/doc routing policy (C++ vs Python)

The stale duplicate copies of the host's live modules (async_rpc, doc_mirror,
jobs, sim_client, selection, doc_tabs, bootstrap, worker_types, doc_session,
pick_hit, worker_command_dispatcher) were deleted in R0b — the canonical
copies live in `host/src/core/*` and `host/src/bootstrap.cpp`.

## Host forwarding headers

- `host/src/worker_client.h` → `#include "../../services/worker/worker_client.h"`
- `host/src/core/cpp_core.h` → `#include "../../../services/worker/cpp_core.h"`
