---
name: chudware-daemon-crash-coredump-triage
description: Triage daemon connection loss and correlate it with crash evidence.
---

# chudware-daemon-crash-coredump-triage

- A closed daemon connection can indicate a crash, transport loss, shutdown, or restart. Correlate timestamps, process evidence, logs and available coredumps before classifying it.
- Match the executable and debug symbols to the failing process; inspect the stack and preceding request/events without exposing private data. Separate the first causal fault from downstream disconnects.
- Reproduce only with a private daemon and isolated data; do not interrupt existing sessions.

Verify: `scripts/testbox.py --suite platform`
