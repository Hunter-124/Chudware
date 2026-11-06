---
name: chudware-config-json-writer-audit
description: Audit concurrent configuration writers without disturbing user settings.
---

# chudware-config-json-writer-audit

- Trace each writer and reader, including GUI and native code. `services/agent/config.py` implements a shared advisory lock; audit whether each writer takes it and account for platform limitations.
- Preserve unrelated configuration and provider credentials; never print secrets. Check atomic replacement, read-modify-write ordering, caching, and behavior on malformed input.
- Reproduce using a private configuration root. Any recovery of an actual user configuration requires explicit authorization and coordination; do not rewrite the live file as a diagnostic.

Verify: `scripts/testbox.py --suite agent,platform`
