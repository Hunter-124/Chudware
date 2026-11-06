---
name: cad-omp-bridge
description: Debug or extend Chudware’s official OMP integration, provider setup, and session lifecycle.
---

# cad-omp-bridge

- Trace GUI → worker `agent_admin_turn` → `OmpSessionHost` → official OMP authentication, model and session APIs → provider; use a private session and inspect redacted events without extracting credentials.
- Read `docs/dev/omp-integration.md` and `services/agent/AGENTS.md`: agent turns use persistent official OMP RPC. Inspect `services/agent/omp_tools.py` and the trusted extension for host-tool binding and dispatch.
- OMP owns provider authentication and journals; Chudware owns CAD tools, policy, roles, and event presentation. Preserve opaque session history, thought traces, and cache lineage.
- Preserve the user's exact compaction selection; snapcompact is experimental and must not be enabled by default. Never restart an active user session to apply settings.
- Consult `scripts/pin_omp.py` and the integration documentation for artifact pinning.

Verify: `scripts/testbox.py --suite agent`
