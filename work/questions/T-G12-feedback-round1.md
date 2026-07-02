# T-G12 Walkthrough — Round 1 Feedback

**Status:** AWAITING ERIK  
**Instructions:** Fill in the `keep/fix/kill` column and add free-text notes for every row.  
Return this file committed to the branch or sent to the foreman when done.  
Must-fix findings block T-G9/T-G11 sign-off until the fix is merged and a re-run pass confirms zero new must-fix items.

---

## How to run the app for each scenario

```bash
# Build (if stale):
flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"

# Run a scenario (headless screenshot):
AI_CAD_SHOT=m1      AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=sketch  AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=asm     AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=fea     AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=cam     AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=drawing AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png
AI_CAD_SHOT=appearance AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png

# Run live (interactive):
host/build-linux/bin/AI-CAD

# Regenerate the checklist after code changes (for round 2+):
python3 scripts/ui_inventory.py --repo . --out work/questions/T-G12-feedback-round<N>.md
# With diff vs previous round:
python3 scripts/ui_inventory.py --repo . \
    --diff work/questions/T-G12-feedback-round1.md \
    --out work/questions/T-G12-feedback-round2.md

# Command palette: press Ctrl+Shift+P (or Cmd+K on Mac) inside the live app
```

---

## Checklist

_The checklist below was generated from source code by `scripts/ui_inventory.py`._
_After annotating, commit this file and post a note to the foreman._

<!-- PASTE THE OUTPUT OF scripts/ui_inventory.py --out ... HERE FOR ROUND 1 -->
<!-- (Generated separately; see scripts/ui_inventory.py) -->

**To generate and view the full round-1 checklist:**
```bash
python3 scripts/ui_inventory.py --repo . --out /tmp/round1_checklist.md
# Then open /tmp/round1_checklist.md in your editor
```

Or to write it directly into the repo:
```bash
python3 scripts/ui_inventory.py --repo . >> work/questions/T-G12-feedback-round1.md
```

---

## Erik's annotation area

_Fill in findings below after walking the app.  Use the format:_

```
## Must-fix
- [cmd_id] description of issue

## Nice-to-fix
- [cmd_id] description

## Kill
- [cmd_id] reason

## Keep (confirmed good)
- [cmd_id]
```

### Must-fix findings (blocks T-G9/T-G11)

<!-- Erik fills in here -->

### Nice-to-fix / polish

<!-- Erik fills in here -->

### Kill / remove

<!-- Erik fills in here -->

### Confirmed good

<!-- Erik fills in here -->

---

## Convergence tracking

| Round | Date | Must-fix count | Status |
|-------|------|----------------|--------|
| 1 | (pending) | (pending) | AWAITING ERIK |

_Converged = a round yields zero new must-fix findings.  Record the converged pass here._
