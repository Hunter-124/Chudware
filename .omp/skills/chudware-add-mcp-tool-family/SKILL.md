---
name: chudware-add-mcp-tool-family
description: Add a Chudware tool family with policy gating and bounded outputs.
---

# chudware-add-mcp-tool-family

- Online OMP sessions receive Chudware host-tool bindings; native document operations remain an engine surface. Identify the intended surface before adding tools.
- Follow existing `services/mcp` tool modules and register through `_reg.view(mcp, family)` in `services/mcp/server.py` so catalog metadata and dispatch policy apply. Use `services.mcp` imports.
- Update the family catalog, policy and relevant UI/tests together. Defaults are policy-dependent; manufacture is default-off. Classify read/write risk correctly and return bounded, pageable output with actionable errors.
- Exercise real registration and dispatch.

Verify: `scripts/testbox.py --suite agent`
