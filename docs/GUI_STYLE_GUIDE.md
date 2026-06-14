# AI-CAD GUI Style Guide — Sizing & Scaling Standards

> Engineering-grade UI standards for the C++ host (Dear ImGui + OpenGL). This is the **single
> source of truth** for how every pixel dimension in the GUI is computed. New panels and edits MUST
> follow these rules. Companion document: [GUI_AUDIT_FINDINGS.md](GUI_AUDIT_FINDINGS.md) — the
> current-state audit + the prioritized backlog of violations to fix.

---

## 1. The scaling model (read this first)

There is exactly **one** scale value: `ui::uiScale` (declared in `host/src/ui_theme.h`,
assigned in `ui_theme.cpp` from the `scale` computed once in `app.cpp` at startup from the monitor
content scale × the optional `AI_CAD_SCALE` env override, clamped 0.5–4.0).

Two facts that govern everything:

1. **Fonts are loaded PRE-SCALED.** `loadFonts(assets, scale)` rasterizes each font at
   `basePx * scale` (`ui_theme.cpp`). `io.FontGlobalScale` is **never** assigned (runtime font
   scaling blurs text — do not introduce it). Consequence: **`ImGui::CalcTextSize(...)`,
   `GetFontSize()`, `GetTextLineHeight*()`, and `GetFrameHeight()` already return DPI-correct
   (scaled) pixels.**

2. **Style metrics are scaled once, centrally.** `applyTheme(theme, scale)` seeds every
   `ImGuiStyle` field at its base (1.0×) value, then calls `style.ScaleAllSizes(scale)` exactly
   once. Consequence: **`GetStyle().FramePadding / ItemSpacing / ItemInnerSpacing / IndentSpacing /
   ScrollbarSize / *Rounding` are already scaled.**

### The one rule

> **Any raw pixel literal you pass to ImGui geometry MUST be multiplied by `ui::uiScale` — UNLESS
> it is already derived from a text metric or a `GetStyle()` field (which are pre-scaled).**

"ImGui geometry" = `ImVec2` sizes, `Image`/`AddImage*` sizes, `Indent`/`Unindent`,
`SameLine(offset)`, `SetNextItemWidth(px)`, `Dummy`, `AddLine` thickness, `AddRect*` rounding,
`SetCursorPos*` offsets, manual hit rects.

Corollary — **never mix scaled and unscaled in one expression.** A frequent bug here:
```cpp
// WRONG: text is already scaled, kPad is 1.0× logical, then *s scales the text AGAIN.
float w = (CalcTextSize(label).x + kPad) * uiScale;
// RIGHT: convert the metric to logical px first, mix with logical consts, scale once.
float w = (CalcTextSize(label).x / uiScale + kPad) * uiScale;   // or just: CalcTextSize + kPad*uiScale
```

### Theme / scale changes

`applyTheme(theme, scale)` is the **only** place style is (re)scaled. Always pass `st.scale` —
**never a literal `1.0f`** (doing so collapses padding to 100% while fonts stay large and clobbers
`ui::uiScale`). There is currently no runtime DPI-change handler; moving the window between
monitors of different DPI does not re-scale (documented limitation — see findings doc).

---

## 2. The base unit

Author all custom spacing in terms of a 4-px grid:

```cpp
const float U = 4.0f * ui::uiScale;   // base grid unit
```

Express gaps/insets as small multiples of `U` (`U`, `1.5U`, `2U`, `2.5U`). Prefer reusing a
pre-scaled style metric (`GetStyle().ItemSpacing.x`, `ItemInnerSpacing.x`, `IndentSpacing`) when one
already expresses the intent — it is automatically correct and theme-consistent.

---

## 3. Spacing & style scale (authored at 1.0×, scaled by `ScaleAllSizes`)

These live in `applyTheme` and are the global defaults. Do not re-scale them in panel code.

| Metric | 1.0× value | Notes |
|---|---|---|
| WindowPadding | 16, 12 | |
| FramePadding | 12, 7 | drives all framed-control heights |
| ItemSpacing | 10, 8 | vertical row gap |
| ItemInnerSpacing | 8, 6 | label↔widget gap |
| CellPadding | 8, 6 | tables |
| ScrollbarSize | 12 | |
| GrabMinSize | 11 | |
| Rounding (frame/grab) | 6 | "small" tier |
| Rounding (popup/tab) | 8 | "medium" tier |
| Rounding (window/child) | 9 | "large" tier |
| ScrollbarRounding | 10 | |
| Border sizes | 1 (on) / 0 (off) | |

Stroke hairlines drawn via the draw-list (`AddLine`/`AddRect`) should be
`max(1.0f, thicknessLogical * ui::uiScale)`; rounding radii `max(1.0f, rLogical * ui::uiScale)`.

---

## 4. Font tiers (px at 1.0×, multiplied by `uiScale` at load)

| Role | Font | 1.0× px |
|---|---|---|
| body | Rubik-Regular | 17.5 |
| label / medium | Rubik-Medium | 17.5 |
| section header (h2) | Rubik-SemiBold | 19 |
| title (h1) | Rubik-Bold | 30 |
| mono | JetBrains Mono | 15.5 |

Glyph coverage is limited to the ranges set in `uiGlyphRanges()` (`ui_theme.cpp`): Latin-1, a few
super/sub-scripts, and a small CAD-symbol set. **Do not use Unicode glyphs outside those ranges in
labels** (e.g. ✓ U+2713, ✗ U+2717 are NOT covered and render as a notdef box). Use an atlas icon or
ASCII instead.

---

## 5. Icons

- **Atlas-based, two pre-rendered sizes (`@32`, `@20`).** `getIcon(name, size)` returns a UV rect;
  the caller scales the drawn quad. Display size tiers: **16 px tree/row icons, 18 px toolbar
  glyphs, 20 px PM/header, 32 px ribbon big buttons** — each `× ui::uiScale`.
- **Names are a contract.** The authoritative list is `CONTRACT_NAMES` in
  `host/assets/make_icons.py` and the cells in `host/assets/icons/atlas.json`. Reference an icon by
  one of those exact names. A miss renders a magenta placeholder (perceived as a broken "?").
- **OK / Cancel are `ok_check` / `cancel_x`** — not `ok`/`cancel` (that exact bug shipped).
- **New icon** → add a draw function + name in `make_icons.py`, regenerate the atlas, then
  reference it. Never invent a name in C++ that the atlas lacks.
- **Guardrail (recommended, see findings):** a startup `validateIconNames()` that asserts every
  name in the static tables (ribbon, shortcut bar, feature-icon map, HUD) exists in the atlas, so a
  bad name trips in dev instead of rendering magenta to a user.

---

## 6. Component formulas

All in terms of `U = 4*uiScale` and `S = ui::uiScale`. Heights that can derive from
`GetFrameHeight()` (already scaled) should.

### Ribbon
- Big button: height `19.5U` (≈78px), icon `8U` (32px) top-inset `1U`, label 2 lines below; width
  `clamp(13U, textLogical + 3U, 24U)*S + (hasDrop ? 3.5U : 0)`.
- Small button / row: **one** shared width formula for measure AND draw — `5U + textPx + 2.5U`; row
  height `5.5U` (22px). Degraded "big-as-small" rows advance `sy` by the **actual** row height.
- Group gutter `2.25U` each side; inter-button gap `0.5U`; separator inset `1U`.
- Tab strip height `7U` (28px), tab pad `5.5U`, gap `0.5U` — **must** handle overflow (shrink then
  `»`) so N tabs never run off.
- Reserve a `5U` chevron from `availW` whenever `Σ groupW > availW` before the collapse passes.

### Bars
- Title bar: `FramePadding (3.5U, 3U)`; `barH = GetFrameHeight()`; caption-button width via the
  single helper `captionButtonWidth(barH) = barH*(1.35 + 0.15*(uiScale-1))`, zone `= 3×`. Scale the
  menu-zone reserves (no bare `4`/`120`).
- Status bar: height `= GetFrameHeight()` (not a bare `30*scale`); padding `(3.5U, 1.25U)`;
  right-cluster margin `4U`.

### Dock (clamp in PIXELS so DPI growth survives)
```cpp
float leftPx  = clamp(258*S, 180*S, 0.34f*w);  float leftFrac  = leftPx/w;
float rightPx = clamp(300*S, 200*S, 0.36f*w);  float rightFrac = rightPx/w;
// right-bottom split 0.42 is a true fraction (DPI-independent) — keep.
```
Clamping the *fraction* with fixed bounds (the current bug) silently eats the DPI scale at ≥1.5×.

### FeatureManager tree
- Row height: derive from font — `GetTextLineHeightWithSpacing()`; optional touch-min
  `max(rowH, 22*S)` for toggle rows.
- Icon: **16*S uniformly** for every tree/header/folder/feature row (one aligned icon column).
- Icon↔label gap `1U`; indent step `IndentSpacing` (or `14*S`).
- Toolbar button `24*S` square, glyph `18*S`, inter-button gap `2*S` (this is the reference
  pattern — copy it).
- Rollback bar: band/hit `12*S`, line `3*S` hover / `2*S` idle, handle `28*S × 8*S`.

### PropertyManager forms
- **Two-column rows**: fixed label column `labelW = 96*S`, then `SameLine(labelW)` +
  `SetNextItemWidth(-1)` for the input (SolidWorks-style aligned `label : value`).
- Dim rows with `-`/`+` steppers: reserve the steppers —
  `SetNextItemWidth(-(stepBtnW*2 + gaps))`, `stepBtnW = CalcTextSize("+").x +
  FramePadding.x*2` — so they never overflow the pane.
- Row height = `GetFrameHeight()`; inter-row gap = global `ItemSpacing.y` (no ad-hoc `Dummy`).
- OK/Cancel on their **own row** above the separator, right-aligned: `btnW = 28*S`, gap `6*S`,
  image `btnW - 8*S`. Render via `getIcon("ok_check"/"cancel_x")`; ASCII `OK`/`X` fallback only.
- Angle fields use the `Angle()` field kind (unit suffix "[deg]"), never `Dim()`.

---

## 7. Review checklist (paste into PRs that touch the GUI)

- [ ] Every raw px literal in ImGui geometry is `* ui::uiScale` (or derived from a pre-scaled
      metric).
- [ ] No metric is scaled twice (no `CalcTextSize(...) * uiScale`).
- [ ] No `applyTheme(..., 1.0f)` — always `st.scale`.
- [ ] No Unicode glyph outside `uiGlyphRanges()` in any label.
- [ ] Every `getIcon("name")` uses a name present in `atlas.json`.
- [ ] Measure and draw of the same widget use the **same** width formula.
- [ ] Clamps on dock fractions are expressed in px, not fixed fraction bounds.
