---
name: chudware-token-meter-accuracy
description: Diagnose incorrect token, context-window, and cost readings using cache-aware usage accounting.
---

# chudware-token-meter-accuracy

For blank or stale indicators consult `chudware-dead-telemetry-indicator`; this playbook concerns populated but inaccurate values. Trace supplied context events from provider/OMP usage through `services/agent/providers/token_usage.py`, `services/agent/omp_events.py`, and the host meter before changing the widget. Separate billed non-cached input from cache-aware context occupancy; account for missing cache fields.

Compare reported occupancy with provider/OMP usage and consider compaction or context replacement before interpreting a drop. Check model-window provenance and do not replace explicit user or authoritative metadata with a heuristic. Exercise recorded or mocked usage and compaction offline; a paid provider call is not the only proof.

Verify: `scripts/testbox.py --suite agent`
