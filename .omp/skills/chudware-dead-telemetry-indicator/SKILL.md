---
name: chudware-dead-telemetry-indicator
description: Diagnose missing or misleading agent telemetry indicators.
---

# chudware-dead-telemetry-indicator

- Trace the actual OMP-mapped events emitted by the affected session through transport, session ownership, consumer gates and UI rendering. Distinguish offline execution from online OMP.
- Preserve provenance and distinguish unknown or unavailable values from measured zero. Inspect catalog and event behavior using an isolated daemon and session, not a live socket.
- Cover both event emission and user-visible consumption with focused tests.

Verify: `scripts/testbox.py --suite agent`
