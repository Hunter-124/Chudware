# T-G12 Walkthrough — Round 1 Feedback

**Status:** ROUND 1 RECEIVED (Erik, 2026-07-03) — triaged into T-G14…T-G20 + T-BL19  
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

_Erik's round-1 pass (2026-07-03, after rebuilding local). Broad-pass feedback rather
than row-by-row annotation; grouped by fix task._

**Dark mode + window correctness (→ T-G14, P0):**
- [global] Every popup window breaks in dark mode: black-on-black text. The whole
  layout looks really good in light mode; in dark mode readability falls off hard.
- [pm_form/*] Property manager in the viewport (dark mode): the text background
  becomes black instead of the window background → black-on-black text, plus the
  border goes transparent (light mode is correct).
- [pm_form/3D-body tools] PM tool menus: formatting wrong — text overlapping UI
  controls; black-on-black text in dark mode.
- [copilot] Copilot-tab tooltips are black-on-black in dark mode.
- [global] All subwindow OS borders are fully transparent — fix.
- [eval_measure, eval_mass_props, eval_section_props, …] Evaluate tools don't open at
  the correct window size to show the properties of the selection — windows should
  auto-scale to content and carry the liquid-glass effect.

**Icons — broken/missing + screenshot pipeline (→ T-G15):**
- [sketch_new] needs a text label like all the other icons have.
- [asm_parts_library] Std. Parts icon needs to be remade.
- [weld_gusset] icon isn't showing.
- [appr_mat_aluminum] [appr_mat_steel] [appr_mat_brass] [appr_mat_dark] icons aren't
  showing; remake them from screenshots of actual renders using each preset.
- [display_hlr] Hidden Removed icon (Appearance → Display) isn't showing.
- Directive: most icons regenerated. Biggest change — these groups get icons that are
  actual styled viewport screenshots of parts showcasing what the tool does, with the
  modified faces emphasized (colored surfaces, like the current highlight treatment):
  Features → 3D Body / Dress-up / Pattern / Multi-body / Curves; Sheet Metal → Create;
  Appearance → Material + Display; Manufacture → 2.5D / Holes / 3D / Turning / Cutting
  (Manufacture may use CAM-simulator screenshots). Approach ratified 2026-07-03:
  **automated pipeline** (regenerable script), not hand-curated one-offs.

**Ribbon restyle (→ T-G16):**
- The toolbar ribbon should be restyled to look like the rest of the program — modern
  iOS-style liquid glass, matching the tooltips and the Copilot tab.

**Copilot tab (→ T-G17):**
- Settings button is off the screen.
- "Manage Models" doesn't fit in the tab dropdown: full management UI moves to the
  Advanced settings window ONLY; keep a slim quick model *picker* in the tab (ratified
  2026-07-03).
- Sessions popup isn't formatted correctly.
- Trashcan button clips off the screen and isn't centered with each row's text.
- Sessions button should turn into the "back to chat" button.
- New-session button can span the whole Copilot tab width.
- General: make the whole thing look better.

**Feature manager restyle (→ T-G18):**
- Text isn't centered in the text backgrounds.
- Hover highlighting on filename/equations/material/solid bodies/origin/default planes
  is terrible — style those rows like the features are.
- Some text/icons not centered or missing proper margins within their backgrounds.
- Regenerate icons: undo/redo (in the FM), equations, material (screenshot-based, like
  the materials-tab icons), and an origin icon.

**3D-feature PM chrome + double display (→ T-G19):**
- OK/Cancel icons regenerated for ALL 3D features; make the buttons wider; display
  them above the property manager, between the configurations and the collapse button.
- Extrude: depth/draft angle double-displays (similar 3D features likely affected —
  audit them all). The duplicate readout at the far right, inside/behind the Copilot
  tab, must be removed.

**Surfaces tab (→ T-G20):**
- [surf_*] The Surfaces tab is broken, and the icons don't reflect what the actual
  operations do. Erik: "I'm not a surface modeler — figure it out and do the testing
  yourself, or we can scrap it if you think it's not useful." → AI-driven audit task.

### Nice-to-fix / polish

- (none separated this round — everything above was accepted as must-fix)

### Kill / remove

- Candidate only: Surfaces tab, IF the T-G20 audit concludes it isn't salvageable/
  useful. Erik explicitly open to scrapping it.

### Confirmed good

- [sk_*] All Sketch-tab icons look pretty good.
- [eval_*] Evaluate-tab icons are fine.
- Overall layout in light mode looks really good.

### Process note (not a UI finding)

Erik can't manually test every modeling feature/tool combination — AI testing needed
for that coverage. Ratified 2026-07-03: engine-level combination battery first
(→ T-BL19); a GUI click-through battery can follow once the icon/style work lands.

---

## Convergence tracking

| Round | Date | Must-fix count | Status |
|-------|------|----------------|--------|
| 1 | 2026-07-03 | 7 batches (T-G14…T-G20) + T-BL19 process task | TRIAGED — fixes in flight; round 2 after they merge |

_Converged = a round yields zero new must-fix findings.  Record the converged pass here._
