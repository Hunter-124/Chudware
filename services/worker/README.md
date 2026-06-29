# services/worker — engine routing seam (libaicad vs Python oracle)

Static library target for the worker client, C++ core loader, and routing policy.
The legacy host binary links these sources directly today; ui/imgui will link
this library once N4a integration completes.

## Sources
- `worker_client.{h,cpp}` — JSON-RPC to engine_oracle subprocess
- `cpp_core.{h,cpp}` — in-process libaicad via flat C ABI
- `worker_router.{h,cpp}` — method/doc routing policy (C++ vs Python)

## Host forwarding headers
- `host/src/worker_client.h` → `#include "../../services/worker/worker_client.h"`
- `host/src/core/cpp_core.h` → `#include "../../../services/worker/cpp_core.h"`
