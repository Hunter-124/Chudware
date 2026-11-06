---
name: chudware-daemon-lifecycle-audit
description: Audit host daemon idle exit, instance identity and lifecycle isolation.
---

# chudware-daemon-lifecycle-audit

- Trace single-instance identity, client attachment, idle exit and shutdown from the actual implementation. Developer overrides are conditional; inspect `services/ipc/ipc_discovery.cpp` before relying on them.
- Use the private-daemon pattern in `services/mcp/private_hostd.py` or a fresh random instance key and owned temporary roots. Never unlink sockets or locks that may belong to a live process.
- Exercise only an owned daemon and clean up only owned resources. Do not restart a service backing open user sessions.

Verify: `scripts/testbox.py --suite platform`
