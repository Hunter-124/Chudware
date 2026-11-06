---
name: chudware-copilot-transcript-surface
description: Change or diagnose the Copilot transcript, streaming rows, and session presentation.
---

# chudware-copilot-transcript-surface

- Follow ordered stream rows in `host/src/ui/copilot/stream_rows.cpp` and the conversation owner through event mapping, rendering and persistence. Preserve event ordering, interjected images, and thought/cache context.
- Reuse shared session controls in the settings and AI workspace rather than creating a competing settings path.
- For visual probes, create synthetic v2 session data in an explicitly private `CHUDWARE_SESSIONS_DIR`; screenshot mode uses a private shot store by default. Use private HOME/XDG roots and a private daemon, and terminate only the owned probe.
- Test actual rendering and session switching where relevant.

Verify: `scripts/testbox.py --suite agent`
