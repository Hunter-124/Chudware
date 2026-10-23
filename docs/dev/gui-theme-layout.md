# GUI theme, metrics and layout

How the desktop GUI looks and is arranged: palette, glass, elevation, DPI-aware metrics, the
shared widget (`ui::kit`) and motion (`ui::anim`) vocabularies, docking and app-level modals.
This document is canonical for colors, DPI and the `scripts/gui_lint.py` rules. What
individual panels contain is covered in [gui-architecture.md](gui-architecture.md) and
[gui-workspaces.md](gui-workspaces.md).

## File map

| File | Role |
|---|---|
| `host/src/ui_theme.cpp/.h` | Liquid Glass v2: palette (dark default, light), glass helpers, elevation. **The only place colors are defined** |
| `host/src/ui/widgets/kit.h/.cpp` | `ui::kit`: the app-wide widget vocabulary, one implementation of every composite widget. Read its header comment before adding a painter |
| `host/src/ui/anim.h/.cpp` | `ui::anim`: duration tokens, easings, per-id tweens, frame pump |
| `host/src/ui/metrics.h` | DPI-aware fonts and spacing; every GUI dimension scales through it |
| `host/src/ui/layout.cpp/.h` | Dockspace, pinned glass columns, overlay band, `resetOverlayColumns()` |
| `host/src/ui/settings_modal.cpp` | Settings dialog (theme toggle, AI provider, units, glass knobs) |
| `host/src/ui/file_dialog.cpp`, `equations_dialog.cpp`, `profiler_window.cpp` | App-level modals |
| `host/src/ui/toast.cpp`, `cursors.cpp` | Notifications, cursor set |
| `host/src/ui/provider_usage.h/.cpp` | The single AI-provider usage poller; Settings and the composer usage rings both read it. Never add a second poller |
| `host/wasm/shell.html`, `host/src/app.cpp` | Browser build surface: safe-area/visualViewport wrapper, CSS-pixel UI scale, reserved HUD, IME bridge. GLFW owns DPR backing-store sizing; never multiply CSS metrics by DPR |
| [`docs/design/GLASS_DESIGN_SPEC.md`](../design/GLASS_DESIGN_SPEC.md) | Frozen visual-system spec; read before changing glass behavior |

## Metrics and DPI

`host/src/ui/metrics.h` is the single place GUI dimensions are defined. Accessors return
logical sizes multiplied by `ui::uiScale`:

- `px(logical)` scales a logical-pixel value; `U(n)` is the 4-px base grid unit;
  `stroke(t)` and `rounding(r)` are hairline-safe (never below 1 px).
- Fonts are pre-scaled and `ImGuiStyle` goes through `ScaleAllSizes` once, so values derived
  from `CalcTextSize`, `GetFrameHeight` or `GetStyle()` are **already scaled**: do not wrap
  them in `px()`.
- `metrics.h` is append-only. Workspace token blocks (for example the `re*()` block the
  Reverse Engineering workspace sizes itself from) are called, never bypassed with literals.
- **Density, not metric families.** `kit::pushDensity/popDensity` with
  `Density{Compact, Cozy, Roomy}`: Compact for streaming chat, Cozy (default) for docked panes
  and menus, Roomy for wizard/workspace bodies. Push once around a pane; the kit's
  `padX/padY/rowGap/rowRound/chipRound/sectionGap/headGap/gutter` accessors resolve from it.
  Never pick a token family by hand or add fresh literals.

## Lint rules (`scripts/gui_lint.py`)

The linter enforces the sizing, color and widget rules above. Run one rule with `--rule`, or
all of them by default; accepted findings live in `scripts/gui_lint_baseline.json`, and a
legitimate case can be suppressed with a trailing `// gui-lint: allow <reason>` comment.

| Rule | Flags |
|---|---|
| `icons` | Any icon name used in `host/src` (including `ribbon_def.cpp` rows) that is missing from `assets/icons/atlas.json` |
| `theme` | A literal scale in `applyTheme(..., 1.0f)` (collapses HiDPI padding) and any `io.FontGlobalScale` assignment (blurs fonts) |
| `scale` | Raw pixel literals in ImGui geometry calls not multiplied by a scale token or wrapped in `px()/U()/stroke()/rounding()` |
| `color` | `IM_COL32`/`ImVec4` literals in draw calls, and `GetStyleColorVec4(<anything but ImGuiCol_Text>)`. Reading `ImGuiCol_Text` is the adaptive-ink idiom and is allowed. Image tints and brand-mark files (`ui_theme.cpp`, `icons*.cpp`, `provider_logos.cpp`, `tesseract.cpp`) are excluded |
| `glyph` | Codepoints the UI font does not carry. The allowlist is parsed from `host/src/ui/glyph_ranges.h` (`kBody` + `kCadSymbols`), the same table font loading uses; extend the font there, never in the lint. The font carries `…` `—` `·` `×` `•` `°` but no arrows, checkmarks, crosses, circles, box-drawing or dingbats; those render as `?`. Use `kit::statusGlyph` or an atlas icon |
| `dupe` | A second implementation of a `ui::kit` primitive (see below) |

## Shared vocabulary: `ui::kit` and `ui::anim`

- `kit.h` is the single widget vocabulary: cards and section headers, rows (`RowStyle`), chips,
  primary/secondary/danger/icon buttons, `segmented`, popovers with an `activeCount` badge,
  `Status` with `statusGlyph`, `inlineStatus`, `progressBar`, `spinner`,
  `emptyState/blockedState`, `keyValueRow`, `elide`, spacing helpers, `fmtBytes/fmtAgo/
  fmtCount/fmtDuration`, and `Tone` with `toneU32/toneVec/toneOn` (the meaning-to-color map,
  so "failed" looks identical everywhere).
- Extended kit headers in the same namespace: `kit_form.h` (explicit-id fields, `FieldGroup`,
  pane-wide label column and unit lane, quantity formatters, tooltips), `kit_plot.h`
  (`linePlot/barPlot`, chip flows, `removableChip`, `metricTable`, tables), `kit_panel.h`
  (`panelBegin/BodyBegin/FooterBegin`, `panelHeader`, `formTabs`, `stepper`, `actionFooter`,
  `removableCard`, `canvasFrame`, `groupedPicker`). Every interactive primitive takes an
  explicit id; never rely on label text for ID uniqueness.
- **Workspace kits are thin wrappers.** Per-workspace helpers (`ui/reveng/reveng_widgets.h`,
  `ui/plm/plm_hub.h`, `ui/parts/parts_widgets.h`, `ui/copilot/ui_common.cpp`) keep their names
  and signatures, but their bodies forward to `ui::kit`. A new primitive is a `kit.h/.cpp`
  change. A definition matching a kit primitive's name whose body never mentions `ui::kit`
  fails `--rule dupe`; the one exception is `property_manager.cpp`'s `pmSectionHeader`
  (a disclosure control, marked `// gui-lint: allow`).
- **ID-conflict gate.** `IMGUI_ENABLE_TEST_ENGINE` is defined for the app's ImGui sources and
  `host/src/ui/ui_audit.cpp` implements the item hooks: `CHUDWARE_SHOT=simaudit` walks every
  simulation state and fails on duplicate IDs or clipped items; `CHUDWARE_IDAUDIT=1` adds the
  same check to any scenario (`idaudit: done conflicts=N`, non-zero exit on conflicts).
- `anim.h` is the motion vocabulary: duration tokens (`kInstant/kFast/kBase/kSlow/kStagger/
  kBreathe/kMarch`), easings (`easeOut/easeInOut/easeOutBack/easeSpring`), stateless per-id
  `toward(ImGuiID, target, seconds)`, `peek/forget`, ambient `pulse/phase`, `stagger`, and
  `dt()` (clamped to 0.05 s so a stall cannot teleport a tween). No bare duration literals or
  hand-rolled smoothsteps.
- **Frame pump.** The frame loop idles on `glfwWaitEventsTimeout(0.05)` (20 Hz floor) unless
  something is busy; `app.cpp` folds `ui::anim::consumeFrameRequest()` into the busy term. The
  kit's tweens request frames and stop once settled. **Any hand-rolled animation must call
  `ui::anim::requestFrames()` every frame it moves**, or it stutters at 20 Hz once the mouse
  stops.

## Theme system

- Modes are `"dark"` (default) and `"light"`, persisted to `config.json` on shutdown; the
  toggle also calls `syncIconTheme()` (`host/src/ui/icons.cpp`) to swap the icon atlas.
- Glass helpers in `ui_theme.h`: `pushGlassWindowStyle/popGlassWindowStyle` around a floating
  `Begin`, `pushGlassChildStyle/popGlassChildStyle` around a `BeginChild` card, `glassPanel()`
  with a four-level `GlassLevel` (UltraThin, Thin, Regular, Thick), `glassRowBackground()`,
  `coloredRowWash()`, `adaptiveSurface()`. Do not hand-roll translucency.
- No hardcoded colors outside `ui_theme.*` (lint-enforced, above).
- `CHUDWARE_SHOT=appearance` live-previews glass knobs headlessly.

## Layout system (`layout.cpp`)

- One central ImGui dockspace node with no splits; panels are not conventional docked windows.
- **Pinned overlay columns:** the FeatureManager (left) and Copilot (right) draw over the
  viewport as glass columns. Collapse rolls a column up to a header row plus chevron.
- **Overlay band:** a screen-space rectangle between the ribbon and the status bar, published
  every frame; floating dialogs (Measure, BOM, ...) anchor inside it.
- `setNextCommandDialogPlacement(windowName, preferredSizePx, minimumSizePx)` takes
  already-scaled pixels and is called immediately before the matching `Begin`. It preserves
  user placement and size and recovers off-screen bounds after viewport or DPI changes.
  Analysis windows (Measure, Mass/Section Properties, BOM, Cut List, Design Table) are normal
  resizable windows: no `AlwaysAutoResize`, no double scaling.
- "Reset layout" calls `resetOverlayColumns()` to restore default widths.
- Frame-loop draw order lives in `run_gui()` in `host/src/app.cpp`: title bar, left pane tabs,
  viewport, console, status bar, ribbon dispatch, modals.
- **Reserved chrome, not overlays.** Strips that must never cover geometry (for example the
  Manufacture Offline/Live mode bar) report their height, which `app.cpp` folds into `dockTopY`
  before `setOverlayBand()`, and then paint at the reserved position.
- **Workspace crossfade.** A workspace change fades the central body in over `anim::kBase`
  with `PushStyleVar(ImGuiStyleVar_Alpha)` around the body span, popped unconditionally. Chrome
  stays fixed, and the fade snaps to 1 under `CHUDWARE_SHOT` so captures never show a
  half-faded panel.

## Shared scroll and gauge primitives

- **Pill scrollbar.** `ui::drawWindowPillScrollbarV(surf)` replaces ImGui's default bar; the
  child must carry `ImGuiWindowFlags_NoScrollbar`, and the call goes immediately before
  `EndChild()`. A default grey ImGui scrollbar anywhere is a bug.
- **Circular gauge.** `ui::drawFractionRing(dl, center, radius, thickness, frac, track, arc)` is
  the one "fraction of a budget" shape (context window, rate-limit rings, usage badges).
  Compact badges use `radius = GetFrameHeight() * 0.30-0.34f` and
  `thickness = metrics::stroke(2)`.

## Common traps

- **Logical dp vs pixels.** `glassPanel`, `glassRowBackground` and `coloredRowWash` take a
  logical dp radius and scale internally, while kit/metrics accessors already return pixels.
  Convert back (`token / uiScale`) or pass the bare dp number.
- **`ImGuiChildFlags_None` ignores WindowPadding.** Only children with
  `ImGuiChildFlags_AlwaysUseWindowPadding` or a border get `style.WindowPadding`.
- **Draw-list text is not clipped by its card.** `dl->AddText` of user strings (titles,
  snippets, artifact names) must be ellipsized against the row's real width.
- **Measure, don't eyeball.** Equal-width `segmented()` segments size from the widest label;
  `popoverBegin`'s count badge sits inside width reserved up front. Chrome that overhangs a
  frame or divides a width must be checked against rendered pixels.

## Verification

```bash
python3 scripts/gui_lint.py --rule theme     # required when touching this subsystem
python3 scripts/gui_lint.py --rule color     # any new painter
python3 scripts/gui_lint.py --rule glyph     # any new user-visible string
python3 scripts/gui_lint.py --rule dupe      # any new widget helper
CHUDWARE_SHOT=appearance xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/theme_smoke.png
```

Theme and layout changes ripple everywhere, so a manual check should cover two or three
distinct panels, for example: Settings, toggle the light theme; glass columns re-tint, icons
swap, and nothing stays dark.
