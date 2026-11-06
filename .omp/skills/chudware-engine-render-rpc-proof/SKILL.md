---
name: chudware-engine-render-rpc-proof
description: Prove engine rendering RPC behavior with isolated positive and negative controls.
---

# chudware-engine-render-rpc-proof

- Trace render RPC handling to the engine's CPU rendering path in `core/doc/session.cpp`; inspect the configurable sparse-render threshold before assuming a fixed threshold.
- Capture a representative render and check the returned payload and image properties against the requested geometry. Use a negative control that demonstrates the check can detect a missing or sparse render.
- Run controls in a disposable isolated checkout or via an injected negative control; never mutate and restore shared source files. Even with `CHUDWARE_ENGINE_DAEMON=0`, use explicit private data roots.

Verify: `scripts/testbox.py --suite render`
