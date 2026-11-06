---
name: chudware-gui-pixel-proof
description: Measure GUI visual defects and verify fixes in an isolated screenshot probe.
---

# chudware-gui-pixel-proof

- Capture a representative `CHUDWARE_SHOT` scenario with a separately owned application on private Xvfb, private HOME/XDG/session roots and a private daemon, or `CHUDWARE_ENGINE_DAEMON=0` when supported. Stop only owned processes.
- Check executable provenance and whether a historical banner or stale binary explains an apparent recurrence before attributing it to current drawing code.
- Measure before/after pixels using candidate palette colors and tolerances; add slice offsets to image coordinates. No matching long run means only no run at that color and tolerance: consider blending, gradients, clipping and other colors.
- Inspect ImGui title-bar versus window borders, nested child borders, and accumulated padding for inset scrollbars. Record actual measurements.

Verify: `scripts/testbox.py --suite render,gui`
