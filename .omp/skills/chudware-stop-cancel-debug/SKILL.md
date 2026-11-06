---
name: chudware-stop-cancel-debug
description: Diagnose agent Stop freezes, incomplete cancellation, and completion-path deadlocks.
---

# chudware-stop-cancel-debug

Trace cancellation from GUI action through hostd run control, provider/worker process and completion handling. Distinguish a missing run registration, nested run control replacement, a blocked shared client, an unresponsive provider or worker, and a recursively acquired completion mutex. Use stack and log evidence from owned processes rather than assuming one failure explains all symptoms.

Start a separately owned private daemon with fresh instance key and private roots using `chudware-isolated-hostd-probe`; a socket override alone is insufficient. Never restart the user's daemon. Exercise early and late cancellation, verify the response, run termination, engine inflight drain and cleanup of owned worker descendants. Measure latency rather than treating a historical threshold as a universal guarantee.

Verify: `scripts/testbox.py --suite agent,platform`
