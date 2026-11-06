---
name: chudware-lazy-gui-asset-pump-debug
description: Diagnose GUI assets that appear only after opening an unrelated panel.
---

# chudware-lazy-gui-asset-pump-debug

Trace the drawing/read path, request queue, per-frame pump, texture cache and latch state in `host/src/ui/` and `host/src/app.cpp`. Check whether pumping is conditional as well as whether a cache or one-time latch prevents retries. Preserve render-thread and initialization requirements when changing a pump.

Compare cold and warm runs with private HOME and XDG directories and a private daemon; never move the user's cache. Confirm actual pixels and, when applicable, that a cold run populates its private cache without visiting the unrelated panel. Inspect current appdata path resolution instead of assuming a legacy directory.

Verify: `scripts/testbox.py --suite render`
