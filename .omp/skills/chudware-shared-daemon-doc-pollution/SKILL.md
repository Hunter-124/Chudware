---
name: chudware-shared-daemon-doc-pollution
description: Triage phantom documents, missing agent parts, and unexpected recovery tabs without mutating user state.
---

# chudware-shared-daemon-doc-pollution

Separate daemon document-pool sharing, GUI/backend tab lifetime, test leakage and recovery-only internal artifacts; similar symptoms do not imply the same cause. Inspect existing user evidence read-only, without closing documents or restarting any user process. Reproduce on private HOME/XDG roots and an owned daemon using `chudware-isolated-hostd-probe`; a socket override alone is not enough to isolate a singleton.

Compare document IDs, origin kinds and revisions with GUI tabs and artifact selection in the private fixture. For recovery-only `_pf` artifacts use `chudware-recovery-tier-doc-probe`; for an unresponsive existing tab investigate lock contention separately. Assert both isolation and legitimate document visibility.

Verify: `scripts/testbox.py --suite platform`
