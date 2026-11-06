---
name: chudware-imgui-overlay-hittest-debug
description: Diagnose ImGui controls that render but do not respond to hover or clicks.
---

# chudware-imgui-overlay-hittest-debug

First confirm input delivery using a known-working control. Compare item bounds, mouse coordinates, hover state, clipping, overlapping items, and window focus; an independent utility window behind its parent in hit-test order is one hypothesis, not the only cause. `host/src/ui/layout.cpp` has a parent-window positioned control to consult.

When appropriate, submit an `InvisibleButton` inside the existing parent at the desired screen position, restore the cursor afterward, and account for overlapping item submission order. Exercise both directions of toggles. Use a privately owned Xvfb/app pair with isolated HOME, XDG roots and daemon; retain PIDs and stop only those processes. Remove temporary instrumentation.

Verify: `scripts/testbox.py --suite gui`
