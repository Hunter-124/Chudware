---
name: chudware-headless-hostd-verify
description: Verify daemon lifecycle, cancellation and event behavior with a private hostd.
---

# chudware-headless-hostd-verify

- Identify the exact binary and engine artifacts used by the reproduction; a running process can retain an older executable after replacement. Do not restart or stop a daemon serving open user sessions.
- Use `services/mcp/private_hostd.py` for a fresh instance key and isolated data roots; overriding only the socket is insufficient. Inspect staleness logic in a disposable fixture rather than touching shared staged binaries.
- For cancellation, trace run identity, the cancel request and streamed completion; distinguish general RPC `inflight` from `daemon_status.engine_inflight` for actual engine work. Test empty or early run identifiers where applicable.
- Stop and clean up only the owned probe.

Verify: `scripts/testbox.py --suite platform`
