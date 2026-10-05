---
name: gui-theme-layout
description: Chudware theming and layout specialist — the Liquid Glass v2 visual system, dark/light palettes, glass window helpers, elevation levels, DPI-aware metrics, the shared ui::kit widget and ui::anim motion vocabularies, the dockspace/pinned-column layout, overlay band, and app-level modals (Settings, file dialog, equations, profiler). Use for any task about colors, styling, glass effects, panel docking/collapse, window layout, DPI scaling, or app dialogs. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Theme & Layout Specialist

Scope: how the app *looks and is arranged* — palette, glass, elevation, metrics, the shared
widget/motion vocabularies, docking, modals. What individual panels *contain* belongs to their
own specialist skills. This file is canonical for `ui::kit`, `ui::anim`, colors, DPI and the
`gui_lint` rules; dispatch, integrator-owned hot files, waves and the verification handoff live
in `.claude/skills/gui-orchestrator/SKILL.md`.

## File map

| File | Role |
|---|---|
| `host/src/ui_theme.cpp/.h` | Liquid Glass v2: palette (dark default + light), glass helpers, elevation. **The ONLY place colors are defined** |
| `host/src/ui/widgets/kit.h/.cpp` | `ui::kit` — the app-wide **widget** vocabulary, ONE implementation of every composite widget. Read its header comment before adding any painter |
| `host/src/ui/anim.h/.cpp` | `ui::anim` — the app-wide **motion** vocabulary: duration tokens, easings, per-id tween, frame pump |
| `docs/design/GLASS_DESIGN_SPEC.md` | Frozen visual-system spec — read before changing any glass behavior |
| `host/src/ui/layout.cpp/.h` | ImGui dockspace, pinned glass columns, overlay band, `resetOverlayColumns()` |
| `host/src/ui/metrics.h` | DPI-aware fonts/spacing — all sizes scale through this |
| `host/src/ui/settings_modal.cpp` | Settings dialog (theme toggle, AI provider, units, glass knobs) |
| `host/src/ui/file_dialog.cpp`, `equations_dialog.cpp`, `profiler_window.cpp` | App-level modals |
| `host/src/ui/toast.cpp`, `cursors.cpp` | Notifications, cursor set |
| `host/src/ui/provider_usage.h/.cpp` | THE subscription_accounts / subscription_usage poller — Settings Accounts and the composer usage rings both read it. Never add a second poller |
| `host/src/ui/account_panel.h` | `entitlementBannerHeight` / `drawEntitlementBanner` (reserved chrome, below) |
| `host/wasm/shell.html`, `host/src/app.cpp` | Browser mobile surface: safe-area/visualViewport wrapper, CSS-pixel UI scale, reserved 56px HUD, exclusive Files/Chat drawers, queued UTF-8/control-key IME bridge. GLFW owns DPR backing-store sizing; never multiply CSS metrics by DPR |

## Shared vocabulary — `ui::kit` and `ui::anim`

- `kit.h` is the single widget vocabulary: `cardBegin/cardEnd/cardHeaderRight`, `sectionHeader`,
  `row/rowBegin/rowEnd` + `RowStyle`, `chip/valueChip/toggleChip`,
  `primaryButton/secondaryButton/dangerButton/iconButton`, `segmented`,
  `popoverBegin/popoverEnd` (with an `activeCount` badge), `Status` + `statusGlyph/statusGlyphInline`,
  `inlineStatus`, `progressBar`, `spinner`, `emptyState/blockedState`, `dimText/keyValueRow/elide`,
  `rule/gapRow/gapSection`, `fmtBytes/fmtAgo/fmtCount/fmtDuration`, and `Tone` +
  `toneU32/toneVec/toneOn` (the meaning→color map, so "failed" looks identical everywhere).
- **Workspace kits are thin wrappers.** Per-workspace helpers (`ui/reveng/reveng_widgets.h`
  `ui::reveng::kit`, `ui/plm/plm_hub.h`, `ui/parts/parts_widgets.h`, `ui/copilot/ui_common.cpp`)
  keep their names and signatures — call sites spell them `plmEmptyState` / `awEmptyState` /
  `kit::EmptyState` — but their BODY forwards to `ui::kit`. Reference pattern: `plm_workspace.cpp`,
  `plm_pages.cpp`, `reveng_widgets.cpp`, `parts_widgets.cpp`, `parts_results.cpp`,
  `ai/{active_work,docs_panel,plan_board,ai_workspace}.cpp`, `copilot/ui_common.cpp`,
  `settings_modal.cpp`, `parts_filters.cpp`, `viewport_panel.cpp`, `firmware/fw_workspace.cpp`,
  `sketch_mode.cpp`, `sim_manager.cpp`. Panel agents consume those headers; a new primitive is a
  `kit.h/.cpp` change. A definition matching a kit primitive's name whose body never mentions
  `ui::kit` is a second implementation and fails `--rule dupe`. Sole exception:
  `property_manager.cpp` `pmSectionHeader` — a *disclosure control*, not a caption painter,
  marked `// gui-lint: allow`.
- **Density, not metric families.** `pushDensity/popDensity` with `Density{Compact,Cozy,Roomy}`:
  Compact → `cpChat*` (streaming chat), Cozy → `cpMenu*` (docked panes/menus, default),
  Roomy → `re*` (wizard/workspace bodies). Push once around a pane; the kit's `padX/padY/rowGap/
  rowRound/chipRound/sectionGap/headGap/gutter` accessors resolve from it. Never pick a family by
  hand or add fresh literals (`cpMenuHeader/cpMenuSection/cpMenuLabel/cpMenuHint` in
  `copilot/ui_common.cpp` forward to the kit).
- `anim.h` is the motion vocabulary: `kInstant/kFast/kBase/kSlow/kStagger/kBreathe/kMarch`,
  `easeOut/easeInOut/easeOutBack/easeSpring`, stateless per-id `toward(ImGuiID, target, seconds)`,
  `peek/forget`, ambient `pulse/phase`, `stagger`, and `dt()` (clamped to 0.05 so a stall cannot
  teleport a tween). No bare duration literals or hand-rolled smoothsteps.
- **Frame pump.** The frame loop idles on `glfwWaitEventsTimeout(0.05)` (20 Hz floor) unless
  something is busy; `app.cpp` folds `ui::anim::consumeFrameRequest()` (consumed unconditionally,
  only there) into the `busy` term. `AppState::animActive` belongs to the viewport camera tween
  only. `toward/pulse/phase/stagger` call `requestFrames()` and stop once settled. **Any
  hand-rolled animation MUST call `ui::anim::requestFrames()` every frame it is moving** or it
  stutters at 20 Hz once the mouse stops.

## Theme system

- Modes: `"dark"` (default) and `"light"`, persisted to config.json on shutdown; the toggle also
  calls `syncIconTheme()` (`ui/icons.cpp`) to swap the icon atlas.
- Glass helpers in `ui_theme.h`: `pushGlassWindowStyle/popGlassWindowStyle` (around a floating
  `Begin`), `pushGlassChildStyle/popGlassChildStyle` (around a `BeginChild` card), `glassPanel()`
  with a 4-level `GlassLevel` (UltraThin/Thin/Regular/Thick), `glassRowBackground()`,
  `coloredRowWash()`, `adaptiveSurface()`. Don't hand-roll translucency.
- **No hardcoded colors outside `ui_theme.*`; lint-enforced.** `scripts/gui_lint.py` rules
  `icons`, `theme`, `scale`, `color`, `glyph`, `dupe`:
  - `theme`: literal scale in `applyTheme(., 1.0f)` (HiDPI collapse) and any `io.FontGlobalScale` assignment.
  - `color`: `IM_COL32`/`ImVec4` literals inside draw calls and `GetStyleColorVec4(<anything but
    ImGuiCol_Text>)`. `ImGuiCol_Text` is exempt — reading the caller's ink is the adaptive idiom.
    `AddImage*` tints and brand-mark files (`ui_theme.cpp`, `icons*.cpp`, `provider_logos.cpp`,
    `tesseract.cpp`) are excluded.
  - `glyph`: ALLOWLIST read from `host/src/ui/glyph_ranges.h` (`ui::glyphs::kBody` +
    `kCadSymbols`), the same table font loading and the copilot text fold use; extend the font
    there, never in the lint. The font carries `…` `—` `·` `×` `•` `°`; it does NOT carry arrows
    (0x2190-0x21FF), checkmarks, crosses, filled/hollow circles, box-drawing, block elements or
    dingbats — those render as `?`. Status marks use `kit::statusGlyph` (vector) or the atlas.
  - `dupe`: a second implementation of a kit primitive (above).
  - `scale`: unscaled pixel literals in ImGui geometry calls.
- `CHUDWARE_SHOT=appearance` live-previews glass knobs headlessly.

## Layout system (`layout.cpp`)

- Single central ImGui dockspace node, no splits; panels aren't conventional docked windows.
- **Pinned overlay columns**: FeatureManager (left) and Copilot (right) draw OVER the viewport as
  glass columns. Collapse rolls the column UP to a header row + chevron — not shrink-to-pill.
- **Overlay band**: screen-space rect between ribbon and status bar, published per-frame;
  floating dialogs (Measure, BOM, …) anchor inside it.
- `setNextCommandDialogPlacement(windowName, preferredSizePx, minimumSizePx)` takes already
  metric-scaled pixels; call immediately before the matching `Begin`. It preserves user
  placement/size and recovers off-screen bounds after viewport/DPI changes; published columns
  guide the initial anchor, not a permanent exclusion zone. Measure, Mass/Section Properties, BOM,
  Cut List and Design Table are normal resizable windows: no `AlwaysAutoResize`, no double scaling.
- "Reset layout" → `resetOverlayColumns()` restores default widths.
- Frame-loop draw order lives in `run_gui()` in `host/src/app.cpp` (integrator-owned):
  title bar → left pane tabs → viewport → console → status bar → ribbon dispatch → modals.
- **Entitlement notice is RESERVED CHROME, not an overlay.** `ui::entitlementBannerHeight(st)`
  (0 in good standing) is folded into `dockTopY` before `setOverlayBand`, and
  `ui::drawEntitlementBanner(st, x, y, w, h)` paints the strip — an overlay cannot sit correctly
  across workspaces that disagree about the top-left. No offline-grace notice: customer-facing
  entitlement text never names grace windows, countdowns or clock checks; only the read-only strip
  shows, with generic wording.
- **Workspace crossfade.** A workspace change fades the central BODY in over `anim::kBase`;
  chrome (title bar, ribbon, status bar) stays fixed. It is a `PushStyleVar(ImGuiStyleVar_Alpha)`
  around the body span, popped unconditionally after it (no early return between), snapped to 1
  when `shot` is set so captures never show a half-faded panel, and excludes the Web overlay.
  Alpha only — no vertical lift (each full-body block positions its own window from the band).

## Shared scroll + gauge primitives (do not re-roll)

- **Pill scrollbar.** `ui::drawWindowPillScrollbarV(surf)` replaces ImGui's default bar; the
  child MUST carry `ImGuiWindowFlags_NoScrollbar` and the call goes immediately before
  `EndChild()`. A default grey ImGui bar anywhere is a bug.
- **Circular gauge.** `ui::drawFractionRing(dl, center, radius, thickness, frac, track, arc)` is
  the one "fraction of a budget" shape (composer context window, 5H/7D rate-limit rings, Settings
  Accounts usage, plan/metrics badges). Compact badges: `radius = GetFrameHeight() * 0.30–0.34f`,
  `thickness = metrics::stroke(2)`.
- **Trap — logical dp vs pixels.** `glassPanel`, `glassRowBackground` and `coloredRowWash` take a
  LOGICAL dp radius and scale internally; `metrics::cpMenuRowRound()` already returns PIXELS.
  Convert back (`token / uiScale`) or pass the bare dp number.
- **Trap — `ImGuiChildFlags_None` ignores WindowPadding.** Only children with
  `ImGuiChildFlags_AlwaysUseWindowPadding` or a border get `style.WindowPadding`.
- **Trap — draw-list text is not clipped by its card.** `dl->AddText` of user strings (session
  titles, snippets, artifact names) must be ellipsized against the row's real width.
- **Trap — measure, don't eyeball.** Equal-width `segmented()` segments size from the WIDEST
  label, not the mean; `popoverBegin`'s count badge sits fully inside the frame in width reserved
  up front. Chrome that overhangs a frame or divides a width must be checked against rendered pixels.

## Verification

Workers return files changed plus lint/scenario commands; the integrator builds and runs them per
gui-orchestrator. Requirements for this subsystem:

```bash
python3 scripts/gui_lint.py --rule theme     # mandatory when touching this subsystem
python3 scripts/gui_lint.py --rule color     # any new painter
python3 scripts/gui_lint.py --rule glyph     # any new user-visible string
python3 scripts/gui_lint.py --rule dupe      # any new widget helper
CHUDWARE_SHOT=appearance ./host/build-linux/bin/chudware --ui-screenshot scratch/theme_smoke.png
```

The `appearance` scenario is the automated gate for glass knobs; overhang/width-division chrome
needs a pixel check of the render (above). Theme/layout changes ripple everywhere, so the user
check names 2–3 distinct panels, e.g. "Settings > toggle light theme — glass columns re-tint,
icons swap, nothing stays dark."
