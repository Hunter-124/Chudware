---
name: chudware-ai-session-settings
description: Change or debug AI session settings and their persistence across UI and runtime.
---

# chudware-ai-session-settings

- Trace each setting from the settings UI through persisted configuration to the owning session and its runtime; do not substitute the currently visible session for the owner.
- The supported memory backends are off, local, hindsight, mnemopi and sharpshooter; sharpshooter is the default. Follow the selected backend and preserve its behavior rather than assuming SQLite or a fallback.
- Inspect `host/src/ui/settings_modal.cpp` and `host/src/ui/copilot/panel.cpp` for settings and header behavior; rediscover the current quick-settings implementation before modifying it.
- Preserve explicit compaction choices and keep snapcompact opt-in. Exercise persistence with private configuration.

Verify: `scripts/testbox.py --suite agent`
