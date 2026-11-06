---
name: chudware-doc-lock-contention-triage
description: Investigate document lock contention and slow daemon document reads.
---

# chudware-doc-lock-contention-triage

- Trace the operation, lock duration, competing requests and revision handling before attributing a stall to contention. Inspect the existing `doc_snapshot` path in `core/hostd/doc_registry.h` and `core/hostd/server.cpp`; do not add a duplicate snapshot mechanism.
- Reproduce with a private daemon using `services/mcp/private_hostd.py`: socket override alone is insufficient isolation, so use a fresh key and private data roots.
- Preserve snapshot consistency and document revision semantics; collect timings only as measurements from the actual reproduction.

Verify: `scripts/testbox.py --suite platform`
