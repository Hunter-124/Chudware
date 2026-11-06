---
name: chudware-silent-rpc-failure-triage
description: Trace blank panels and silent RPC failures through GUI, worker, and daemon boundaries.
---

# chudware-silent-rpc-failure-triage

First establish that the panel and control render, then inspect the request shape, handler result keys and error envelopes. Available logs can identify a method and exception, but null params and import failures are hypotheses, not universal diagnoses. Distinguish a successful transport from an RPC error response.

Reproduce the exact request against an in-process handler, then worker stdio registration and response, then an owned private hostd and isolated UI. Use package-qualified imports such as `from services.mcp import core_bridge` when the module depends on package-relative imports. Inspect `async_rpc` queue/timeout and loading latch behavior if the handler succeeds but the UI stays busy. Use isolated HOME/XDG/workspace and daemon; never restart the user's app or replay into its socket. Add regression coverage for the failing boundary.

Verify: `scripts/testbox.py --suite platform`
