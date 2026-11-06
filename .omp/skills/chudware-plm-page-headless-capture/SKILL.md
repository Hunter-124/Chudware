---
name: chudware-plm-page-headless-capture
description: Capture a PLM hub page in an isolated headless GUI for visual diagnosis.
---

# chudware-plm-page-headless-capture

Use a small private fixture project, private HOME/XDG and an owned private daemon. The screenshot mode in `host/src/app.cpp` uses `CHUDWARE_SHOT=git`, `CHUDWARE_PLM_PAGE` to select a page, and `CHUDWARE_PLM_PROJECT` for the fixture. Launch the selected GUI build under private Xvfb with `--ui-screenshot` and write the capture to a unique temporary directory.

Inspect the image to ensure the intended workspace and page actually appeared. Check page busy signals and bounded capture waits if it hangs; graph readiness must not block unrelated pages. Read the current lint output rather than relying on historical baselines. Stop only owned processes.

Verify: `scripts/testbox.py --suite parts`
