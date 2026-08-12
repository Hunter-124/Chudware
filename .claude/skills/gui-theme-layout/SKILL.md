---
name: gui-theme-layout
description: Chudware theming and layout specialist — the Liquid Glass v2 visual system, dark/light palettes, glass window helpers, elevation levels, DPI-aware metrics, the dockspace/pinned-column layout, overlay band, and app-level modals (Settings, file dialog, equations, profiler). Use for any task about colors, styling, glass effects, panel docking/collapse, window layout, DPI scaling, or app dialogs. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Theme & Layout Specialist

Scope: how the app *looks and is arranged* — palette, glass, elevation, metrics, docking,
modals. What individual panels *contain* belongs to their own specialist skills.

## File map

| File | Role |
|---|---|
| `host/src/ui_theme.cpp/.h` (~1.6k LOC) | Liquid Glass v2: palette (dark default + light), glass helpers, elevation. **The ONLY place colors are defined** |
| `host/src/ui/widgets/kit.h/.cpp` (~1.4k LOC, 49 entry points) | `ui::kit` — the app-wide **widget** vocabulary. ONE implementation of every composite widget. Read its header comment before adding any painter |
| `host/src/ui/anim.h/.cpp` | `ui::anim` — the app-wide **motion** vocabulary: duration tokens, easings, the per-id tween, and the frame pump |
| `contracts/GLASS_DESIGN_SPEC.md` | Frozen visual-system spec — read before changing any glass behavior |
| `host/src/ui/layout.cpp/.h` | ImGui dockspace, pinned glass columns, overlay band, `resetOverlayColumns()` |
| `host/src/ui/metrics.h` | DPI-aware fonts/spacing — all sizes scale through this |
| `host/src/ui/settings_modal.cpp` | Settings dialog (theme toggle, AI provider, units, glass knobs) |
| `host/src/ui/file_dialog.cpp`, `equations_dialog.cpp`, `profiler_window.cpp` | App-level modals |
| `host/src/ui/toast.cpp`, `cursors.cpp` | Notifications, cursor set |
| `host/src/ui/provider_usage.h/.cpp` | THE subscription_accounts / subscription_usage poller — Settings Accounts and the composer usage rings both read it. Never add a second poller |

## The shared vocabulary — `ui::kit` and `ui::anim`

Chudware grew four parallel per-workspace widget toolkits (copilot `ui_common`, `plm_hub.h`,
`parts_widgets.h`, `reveng_widgets.h`) plus three private ones inside `ui/ai/`. An audit counted
EIGHT empty-state painters, SIX section headers (three literally named `sectionHeader` with three
different signatures), five chip painters, six spinner idioms and fifteen inline copies of the card
chrome. Every one used correct palette roles — **this was never a color bug, it was horizontal
duplication**, and it is why the product read differently tab to tab.

- `kit.h` is the single vocabulary: `cardBegin/cardEnd/cardHeaderRight`, `sectionHeader`,
  `row/rowBegin/rowEnd` + `RowStyle`, `chip/valueChip/toggleChip`,
  `primaryButton/secondaryButton/dangerButton/iconButton`, `segmented`,
  `popoverBegin/popoverEnd` (with an `activeCount` badge), `Status` + `statusGlyph/statusGlyphInline`,
  `inlineStatus`, `progressBar`, `spinner`, `emptyState/blockedState`, `dimText/keyValueRow/elide`,
  `rule/gapRow/gapSection`, `fmtBytes/fmtAgo/fmtCount/fmtDuration`, and `Tone` +
  `toneU32/toneVec/toneOn` (the meaning→color map, so "failed" looks identical everywhere).
- **THE RULE.** Per-workspace helpers keep their names and signatures — hundreds of call sites
  spell them `plmEmptyState` / `awEmptyState` / `kit::EmptyState` — but their BODY forwards to
  `ui::kit`. `plm_workspace.cpp`, `plm_pages.cpp`, `reveng_widgets.cpp`, `parts_widgets.cpp`,
  `parts_results.cpp`, `ai/{active_work,docs_panel,plan_board,ai_workspace}.cpp`,
  `copilot/ui_common.cpp`, `settings_modal.cpp`, `parts_filters.cpp`, `viewport_panel.cpp`,
  `firmware/fw_workspace.cpp`, `sketch_mode.cpp` and `sim_manager.cpp` are already converted and
  are the reference pattern. A definition matching a kit primitive's name whose body never mentions
  `ui::kit` is a second implementation and fails `--rule dupe`. The one deliberate exception is
  `property_manager.cpp`'s `pmSectionHeader` (`property_manager.cpp:2813`) — it is a *disclosure
  control*, not a caption painter, and carries `// gui-lint: allow`.
- **Density resolves the near-identical metrics token families.** `pushDensity/popDensity` with
  `Density{Compact,Cozy,Roomy}`: Compact → `cpChat*` (the streaming-chat rhythm),
  Cozy → `cpMenu*` (docked panes and menus — the default), Roomy → `re*` (wizard/workspace bodies).
  The host pushes a density once around a pane; the kit's `padX/padY/rowGap/rowRound/chipRound/
  sectionGap/headGap/gutter` accessors resolve from it (`kit.cpp:360-425`). That is what replaced
  four copies of every widget.
- `anim.h` is the motion vocabulary: `kInstant/kFast/kBase/kSlow/kStagger/kBreathe/kMarch`,
  `easeOut/easeInOut/easeOutBack/easeSpring`, the stateless per-id tween
  `toward(ImGuiID, target, seconds)`, `peek/forget`, ambient `pulse/phase`, `stagger`, and `dt()`
  (clamped to 0.05 so a debugger stall cannot teleport a tween to its end state). A bare duration
  literal or a hand-rolled `t*t*(3-2t)` is the thing this replaced.
- **THE STRUCTURAL FACT.** `app.cpp`'s frame loop idles on `glfwWaitEventsTimeout(0.05)` — a 20 Hz
  floor — unless something declares itself busy (`app.cpp:3783-3793`). Before `anim.h` the ONLY
  such flag was `AppState::animActive`, which belongs to the 3D CAD viewport's camera tween and is
  cleared on doc-tab switches — so every *other* animation in the program ran at 20 Hz the instant
  the mouse stopped. `app.cpp` now folds `ui::anim::consumeFrameRequest()` into that `busy` term.
  `toward/pulse/phase/stagger` call `requestFrames()` for you AND stop once settled, so a static UI
  still idles. **Any hand-rolled animation MUST call `ui::anim::requestFrames()` every frame it is
  moving or it stutters at 20 Hz** — that was a live bug in the PLM graph's blast halo.

## The theme system

- Modes: `"dark"` (default) and `"light"`, persisted to config.json on shutdown; theme toggle
  also calls `syncIconTheme()` (`ui/icons.cpp`) to swap the icon atlas.
- Glass helpers in `ui_theme.h`: `pushGlassWindow()`, `glassRowBackground()`,
  `adaptiveSurface()` — 4-level elevation (surface → container-high). Use these, don't
  hand-roll translucency.
- **Hard rule, lint-enforced:** no hardcoded colors outside `ui_theme.*`. Six rules live in
  `scripts/gui_lint.py` — `icons`, `theme`, `scale`, `color`, `glyph`, `dupe`:
  - `--rule theme` catches the `applyTheme(., 1.0f)` HiDPI-collapse bug (never pass a literal
    scale) and any `io.FontGlobalScale` assignment.
  - `--rule color` catches what `theme` never did: `IM_COL32`/`ImVec4` literals inside draw calls,
    and `GetStyleColorVec4(<anything but ImGuiCol_Text>)`. `ImGuiCol_Text` is EXEMPT — reading the
    caller's ink is the documented adaptive idiom (it is how a widget stays legible inside a glass
    bubble, a docked panel and a popup). `AddImage*` tints and the brand-mark files
    (`ui_theme.cpp`, `icons*.cpp`, `provider_logos.cpp`, `tesseract.cpp`) are excluded.
  - `--rule glyph` is an ALLOWLIST transcribed from `ui_theme.cpp:662` (`uiGlyphRanges`) and
    `ui_theme.cpp:675` (`cadSymbolRanges`) — not a guessed blocklist. The font DOES carry `…`
    U+2026, `—` U+2014, `·` U+00B7, `×` U+00D7, `•` U+2022, `°` U+00B0 and the app ships them; the
    old blanket "no non-ASCII" rule was over-applied. What it does NOT carry is arrows
    (0x2190-0x21FF), checkmarks, crosses, filled/hollow circles, box-drawing, block elements and
    dingbats — those render as a literal `?`. Status marks belong in `kit::statusGlyph` (vector) or
    the icon atlas, never in a string.
  - `--rule dupe` catches a second implementation of a kit primitive (see above).
  - `--rule scale` catches unscaled pixel literals in ImGui geometry calls.
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
- **The read-only entitlement notice is RESERVED CHROME, not an overlay.**
  `ui::entitlementBannerHeight(st)` (0 when the account is in good standing) is measured into
  `dockTopY` before `setOverlayBand`, and `ui::drawEntitlementBanner(st, x, y, w, h)` paints the
  strip — `app.cpp:5994-6027`, `ui/account_panel.h:14-28`. Every earlier refinement of the floating
  version just moved which panel it covered next: an overlay cannot be positioned correctly for
  nine workspaces that disagree about what lives at the top-left. Only the transient grace chip is
  still a corner overlay (`ui::drawEntitlementGraceChip`, `app.cpp:6094`).
- **Workspace crossfade.** A workspace change fades the central BODY in over `anim::kBase` while
  the chrome (title bar, ribbon, status bar) stays fixed, so the chrome reads as a frame around
  content that changed — `app.cpp:6228-6265`. It is a `PushStyleVar(ImGuiStyleVar_Alpha)` around
  the body span, popped unconditionally after it (no early return between the two), and snapped to
  1 when `shot` is set so a headless capture can never photograph a half-faded panel. Alpha only,
  deliberately no vertical lift — each full-body block positions its own window from the overlay
  band, so a lift would have to be threaded through five separate `SetNextWindowPos` calls.

## Shared scroll + gauge primitives (do not re-roll these)

- **Pill scrollbar.** `ui::drawWindowPillScrollbarV(surf)` (ui_theme) replaces ImGui's default
  bar for the current child. The child MUST carry `ImGuiWindowFlags_NoScrollbar`, and the call
  goes immediately before `EndChild()`. Every scrolling child in the copilot column, the AI
  workspace and the FeatureManager uses it — a default grey ImGui bar anywhere is a bug.
- **Circular gauge.** `ui::drawFractionRing(dl, center, radius, thickness, frac, track, arc)` is
  the one "how much of a budget is used" shape: composer context window, composer provider
  rate-limit rings (5H/7D), Settings Accounts usage, plan/metrics badges. Compact badges use
  `radius = GetFrameHeight() * 0.30–0.34f`, `thickness = metrics::stroke(2)`.
- **Density tokens, not fresh literals.** `ui::metrics::cpMenu*` (gutter / rowPadX / rowPadY /
  rowGap / rowRound / chipRound / sectionGap / headGap / labelGap / ctlGap), `cpChat*` and `re*`
  are the three rhythms, and `ui::kit`'s density accessors are how you reach them — push a
  `Density` around the pane rather than picking a family by hand. The
  `cpMenuHeader/cpMenuSection/cpMenuLabel/cpMenuHint` chrome in `ui/copilot/ui_common.cpp` now
  forwards to the kit. New literals there are what made the five header views read as five
  unrelated screens.
- **Trap — logical dp vs pixels.** `glassPanel`, `glassRowBackground` and `coloredRowWash` take
  a LOGICAL dp radius and scale it internally; `metrics::cpMenuRowRound()` already returns
  PIXELS. Passing the token straight in double-scales it. Convert back (`token / uiScale`) or
  pass the bare dp number.
- **Trap — `ImGuiChildFlags_None` kills WindowPadding.** imgui.cpp only applies
  `style.WindowPadding` to a child that has `ImGuiChildFlags_AlwaysUseWindowPadding` or a
  border. A pushed `WindowPadding` on a plain child is silently a no-op — which is how the
  Settings tab bodies ended up flush with each section inventing its own indent.
- **Trap — draw-list text is not clipped by its card.** `dl->AddText` for user-supplied strings
  (session titles, snippets, artifact names) must be ellipsized against the row's real width or
  it runs out past the panel edge.
- **Trap — measure, don't eyeball.** Two kit bugs shipped looking plausible and were only found by
  pixel-inspecting the render. `segmented()` summed all label widths and divided by count, giving
  each segment the MEAN width — so any set with one long and two short labels elided the long one
  (`{"Links","All","Assembly"}` rendered as `"Asse..."`); equal-width segments size from the WIDEST
  label (`kit.cpp:881-896`). And `popoverBegin`'s count badge hung off the button's OUTER edge,
  putting it above the host row's clip rect and slicing its top half off; a corner badge must be
  fully inside the frame, in width reserved up front (`kit.cpp:974-1016`). If you add chrome that
  overhangs a frame or divides a width, render it and look at the pixels.

## Build & verify

```bash
./build.sh
python3 scripts/gui_lint.py --rule theme     # mandatory when touching this subsystem
python3 scripts/gui_lint.py --rule color     # any new painter
python3 scripts/gui_lint.py --rule glyph     # any new user-visible string
python3 scripts/gui_lint.py --rule dupe      # any new widget helper
CHUDWARE_SHOT=appearance ./host/build-linux/bin/Chudware --ui-screenshot scratch/theme_smoke.png
```
Smoke = crash check only. Never run git. Return files changed + a one-line user check, e.g.
"Settings > toggle light theme — glass columns re-tint, icons swap, nothing stays dark."
Theme/layout changes ripple everywhere, so the user check should name 2–3 distinct panels
to glance at.
