---
name: chudware-tool-gap-triage
description: Diagnose missing or denied tools across routing, advertisement, and dispatch.
---

# Tool-gap triage

Use the affected run's transcript and error envelopes to distinguish a routing problem from missing advertisement or an actual denial. Keep private transcripts and logs out of public artifacts.

1. Trace the actual online `agent_admin_turn` path in `services/agent/worker_rpc.py` into the OMP host and session; inspect the active role, instructions, and tool schemas.
2. For an unadvertised tool, inspect its registration, role policy and mode floors in `contracts/copilot_modes.json`, and the policy actually applied to the affected run.
3. For a denial, inspect `services/mcp/tool_registry.py` and the applicable family toggle, per-tool setting, and risk classification. Keep advertised and dispatch policies consistent.
4. For an error response from the run, investigate the worker connection and error rather than assuming the model lacked a tool. Switching providers alone does not grant tools.

When an isolated baseline is needed, use a source export and its independently built artifacts; never stash shared work.

Verify: `scripts/testbox.py --suite agent`
