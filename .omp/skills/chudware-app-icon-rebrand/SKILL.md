---
name: chudware-app-icon-rebrand
description: Update Chudware application branding and icon assets consistently.
---

# chudware-app-icon-rebrand

- Inspect `assets/make_logo.py` and its `draw_mark` implementation, then trace generated assets and their consumers before changing the mark.
- Check desktop packaging and the actual current `web/gateway/` assets instead of assuming old deployment paths or page names. Preserve required formats, sizes and transparency.
- Use private disposable screenshot probes with isolated data roots and daemon; never attach to a user's active session.

Verify: `scripts/testbox.py --suite gui`
