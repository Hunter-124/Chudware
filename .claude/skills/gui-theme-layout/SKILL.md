---
name: gui-theme-layout
description: Chudware (formerly Billet / AI-CAD) theming and layout specialist — the Liquid Glass v2 visual system, dark/light palettes, glass window helpers, elevation levels, DPI-aware metrics, the dockspace/pinned-column layout, overlay band, and app-level modals (Settings, file dialog, equations, profiler). Use for any task about colors, styling, glass effects, panel docking/collapse, window layout, DPI scaling, or app dialogs. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Theme & Layout Specialist

Scope: how the app *looks and is arranged* — palette, glass, elevation, metrics, docking,
modals. What individual panels *contain* belongs to their own specialist skills.

## File map

| File | Role |
|---|---|
| `host/src/ui_theme.cpp/.h` (~1.6k LOC) | Liquid Glass v2: palette (dark default + light), glass helpers, elevation. **The ONLY place colors are defined** |
| `contracts/GLASS_DESIGN_SPEC.md` | Frozen visual-system spec — read before changing any glass behavior |
| `host/src/ui/layout.cpp/.h` | ImGui dockspace, pinned glass columns, overlay band, `resetOverlayColumns()` |
| `host/src/ui/metrics.h` | DPI-aware fonts/spacing — all sizes scale through this |
| `host/src/ui/settings_modal.cpp` | Settings dialog (theme toggle, AI provider, units, glass knobs) |
| `host/src/ui/file_dialog.cpp`, `equations_dialog.cpp`, `profiler_window.cpp` | App-level modals |
| `host/src/ui/toast.cpp`, `cursors.cpp` | Notifications, cursor set |

## The theme system

- Modes: `"dark"` (default) and `"light"`, persisted to config.json on shutdown; theme toggle
  also calls `syncIconTheme()` (`ui/icons.cpp`) to swap the icon atlas.
- Glass helpers in `ui_theme.h`: `pushGlassWindow()`, `glassRowBackground()`,
  `adaptiveSurface()` — 4-level elevation (surface → container-high). Use these, don't
  hand-roll translucency.
- **Hard rule, lint-enforced:** no hardcoded colors outside `ui_theme.*`.
  `python3 scripts/gui_lint.py --rule theme` fails the gate on violations, and also catches
  the `applyTheme(., 1.0f)` HiDPI-collapse bug — never pass a literal scale.
- The `CHUDWARE_SHOT=appearance` scenario live-previews glass knobs headlessly.

## The layout system (layout.cpp)

- Single central ImGui dockspace node, no splits; panels aren't conventional docked windows.
- **Pinned overlay columns**: FeatureManager (left) and Copilot (right) draw OVER the
  viewport as glass columns. Collapse rolls the column UP to a header row + chevron (the
  T-G21 affordance) — not shrink-to-pill.
- **Overlay band**: screen-space rect between ribbon and status bar, published per-frame;
  floating dialogs (Measure, BOM, …) anchor inside it.
- "Reset layout" → `resetOverlayColumns()` restores default widths.
- Frame-loop draw order lives in `run_gui()` in `host/src/app.cpp` (integrator-owned):
  title bar → left pane tabs → viewport → console → status bar → ribbon dispatch → modals.

## Build & verify

```bash
./build.sh
python3 scripts/gui_lint.py --rule theme     # mandatory when touching this subsystem
CHUDWARE_SHOT=appearance ./host/build-linux/bin/Chudware --ui-screenshot scratch/theme_smoke.png
```
Smoke = crash check only. Never run git. Return files changed + a one-line user check, e.g.
"Settings > toggle light theme — glass columns re-tint, icons swap, nothing stays dark."
Theme/layout changes ripple everywhere, so the user check should name 2–3 distinct panels
to glance at.
