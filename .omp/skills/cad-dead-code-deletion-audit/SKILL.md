---
name: cad-dead-code-deletion-audit
description: Audit proposed dead code across native, Python, configuration, and agent surfaces.
---

# cad-dead-code-deletion-audit

- Search the symbol, RPC name, and configuration keys across `core/`, `host/`, `services/`, contracts, prompts, and tests. Check dynamic registration and prose-directed agent calls as well as direct callers.
- Inspect native routing in `services/worker/worker_client.cpp`: a Python handler may remain the fallback when native dispatch is unavailable. Check C++ consumers such as `core/sourcing/providers.cpp` before deleting configuration written by the GUI.
- Confirm a genuinely obsolete surface, then remove its implementation, registration, exclusive tests and stale references together. Inspect and update affected contracts; coordinate ownership as appropriate.

Verify: `scripts/testbox.py --suite agent`; for deleted code outside agent/MCP, use the suite owning that area (`scripts/testbox.py --list-suites`).
