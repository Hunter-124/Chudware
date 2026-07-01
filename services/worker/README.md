# services/worker — engine routing seam (libaicad vs Python oracle)

Static library target for the worker client, C++ core loader, and routing policy.
The legacy host binary links these sources directly today; ui/imgui will link
this library once N4a / G1 integration completes.

## Sources
- `worker_client.{h,cpp}` — JSON-RPC to engine_oracle subprocess (+ libaicad routing)
- `cpp_core.{h,cpp}` — in-process libaicad via flat C ABI
- `worker_router.{h,cpp}` — method/doc routing policy (C++ vs Python)
- `doc_mirror.{h,cpp}` — worker-backed document mirror
- `async_rpc.{h,cpp}` — non-blocking worker calls for the UI thread
- `selection.{h,cpp}` + `pick_hit.h` — topology selection model (GL-free)
- `jobs.{h,cpp}` + `worker_types.{h,cpp}` — background agent/design jobs
- `sim_client.{h,cpp}` — FEA/CFD sim worker seam
- `doc_tabs.{h,cpp}` + `doc_session.h` — multi-document tab model
- `worker_command_dispatcher.{h,cpp}` — `ICommandDispatcher` implementation
- `bootstrap.{h,cpp}` — runtime/python provisioning

## Host forwarding headers
- `host/src/worker_client.h` → `#include "../../services/worker/worker_client.h"`
- `host/src/core/cpp_core.h` → `#include "../../../services/worker/cpp_core.h"`
