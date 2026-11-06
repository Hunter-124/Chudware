---
name: chudware-isolated-hostd-probe
description: Probe hostd document and client behavior without attaching to the user’s daemon.
---

# chudware-isolated-hostd-probe

Use `services/mcp/private_hostd.py` and current hostd tests as the starting point. The helper creates a fresh instance key and private socket; isolate HOME and XDG data/runtime roots as well. A socket override alone does not establish a distinct singleton identity. Copying the binary can additionally prevent rebuild races; production builds may not honor developer overrides, so fail rather than attach elsewhere if isolation cannot be established.

For a wire-level probe, connect only to the owned socket, send newline-delimited JSON-RPC `hello` first, and inspect readiness and response envelopes. Model client attach/detach lifetimes and run-in-flight state deliberately. Test both guarded and ordinary behavior, and clean up only owned resources.

Verify: `scripts/testbox.py --suite platform`
