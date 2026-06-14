# GUI Sizing Audit — Current State & Fix Backlog

> Date: 2026-06-13. Produced by a 5-agent read-only audit of the host GUI. Documents the **actual**
> sizing calculations per subsystem (with `file:line`) and a **prioritized backlog** of violations.
> Standards derived from this audit live in [GUI_STYLE_GUIDE.md](GUI_STYLE_GUIDE.md).
>
> **Legend:** ✅ fixed this pass · ⬜ open · severity High / Med / Low.

---

## 0. Root-cause summary

The GUI's scaling *foundation is sound* — fonts are pre-scaled and style metrics go through
`ScaleAllSizes` once. The defects are almost all the **same class**: panel code mixes raw 1.0× pixel
literals with already-scaled text metrics, or clamps a scaled value with fixed bounds. Plus an
icon-name contract that nothing validates (four wrong names shipped silently). See the style guide
for the standard that prevents recurrence.

---

## 1. Scale / Theme / Font foundation
Files: `host/src/ui_theme.{h,cpp}`, `host/src/app.cpp` (scale compute), `host/src/ui/settings_modal.cpp`.

**Current calc:** scale = `glfwGetWindowContentScale().x` (screenshot→1.0; `AI_CAD_SCALE` env override,
0.5–4.0) computed once at `app.cpp` startup, stored `st.scale`, copied to `ui::uiScale`
(`ui_theme.cpp:261`). Fonts loaded at `basePx*scale` (`ui_theme.cpp:404-418`); `io.FontGlobalScale`
never set (correct — no blur). `applyTheme` seeds base style then `ScaleAllSizes(scale)`
(`ui_theme.cpp:352`). Idempotent only if the same scale is always passed.

| # | Sev | Site | Problem | Fix |
|---|---|---|---|---|
| 1.1 | High | `settings_modal.cpp:100` | ✅ `applyTheme(next, 1.0f)` collapsed all padding to 100% on HiDPI + clobbered `ui::uiScale` when toggling theme from Settings | pass `st.scale` |
| 1.2 | Med | `app.cpp:518-520` | ⬜ no runtime DPI-change handler; cross-monitor DPI change leaves fonts/style at launch DPI | register `glfwSetWindowContentScaleCallback` → rebuild fonts + re-apply theme; or document limitation |
| 1.3 | Low | `app.cpp:520` | ⬜ scale uses `xs` only, ignores `ys` | `max(xs,ys)` |
| 1.4 | Low | `ui_theme.cpp` (`applyTheme`) | ⬜ re-apply correctness depends on caller always passing scale (fragile invariant 1.1 already broke) | drop the `=1.0f` default arg so callers must pass scale |

## 2. Ribbon / Bars / Dock layout
Files: `host/src/ui/widgets/ribbon.cpp`, `ribbon_def.cpp`, `title_bar.cpp`, `status_bar.cpp`.

**Current calc:** ribbon consts (1.0× px, `ribbon.cpp:79-90`): tabH 28, bigIcon 32, smIcon 20, bigBtnW
52..96, body 100, caption 22, smRow 22. Ribbon height `(28+100+8)*s`. Dock fractions
(`title_bar.cpp:73-76`): `leftFrac=258*sc/w` clamp [0.13,0.34], `rightFrac=300*sc/w` clamp
[0.15,0.36], `consFrac` computed then discarded. Title `FramePadding (14,12)*s`, caption
`barH*(1.35+0.15*(uiScale-1))`. Status height `30*scale` (set by caller `app.cpp:1707`).

| # | Sev | Site | Problem | Fix |
|---|---|---|---|---|
| 2.1 | High | `ribbon.cpp:124,127` | ⬜ `bigBtnW` mixes scaled `CalcTextSize` with 1.0× `kBigLblPad/kBigBtnW/kBigBtnMaxW` then `*s` → wide labels scale text twice, breach the `96*s` cap at ≥2× | compute label width in logical px, clamp logical, `*s` once |
| 2.2 | High | `ribbon.cpp:135` vs `:746` | ⬜ small-button measured width (`text+38*s`) ≠ drawn/hit width (`text+30*s`) — packer over-reserves, mis-centers separators | shared `ribbon_smBtnW` helper for both |
| 2.3 | High | `ribbon.cpp:534,1105` | ⬜ degraded big-as-small row is `26*s` tall but layout advances `sy` by `22*s` → rows overlap ~4*s | advance by actual row height |
| 2.4 | Med | `ribbon.cpp:968,1257` | ⬜ overflow chevron width (`22*s`) computed but never subtracted from `availW` → chevron can land off-edge | reserve chevron when `totalW>availW` |
| 2.5 | Med | `ribbon.cpp:886-925` | ⬜ tab strip has no overflow/wrap; 8 tabs (growing) run off-screen at high DPI / narrow window | add tab overflow (shrink-pad then `»`) |
| 2.6 | Med | `title_bar.cpp:74-76` | ⬜ dock clamps fight scale: `leftFrac` ceiling 0.34 eats DPI growth at ≥1.5× (the W4-K fix is silently clipped) | clamp in px not fraction (see style guide §6) |
| 2.7 | Med | `title_bar.cpp:139,157` | ⬜ menu-zone reserves `4.0f`/`120.0f` unscaled → wordmark fit wrong at high DPI | `*s` |
| 2.8 | Low | `title_bar.cpp:78` | ⬜ dead `consFrac` math (computed, `(void)`'d) | delete |
| 2.9 | Low | `ribbon.cpp:446,573,928,1116` | ⬜ split-button divider insets + all `AddLine(...,1.0f)` hairlines unscaled | `*s` / `max(1,1*s)` |
| 2.10 | Low | `ribbon.cpp:647,1165,1210` | ⬜ flyout/overflow row metrics (28/20/8/6) duplicated inline in 3 places | hoist to named consts |
| 2.11 | Low | `title_bar.cpp:17,135` | ⬜ caption-width DPI ramp duplicated verbatim, must hand-sync | one `captionButtonWidth(barH)` helper |
| 2.12 | Low | `status_bar.cpp` + `app.cpp:1707` | ⬜ status height is a bare `30*scale` magic number unrelated to font metrics (~2.5*s slack at high DPI) | derive from `GetFrameHeight()` |

## 3. PropertyManager + forms
Files: `host/src/ui/property_manager.cpp`, `property_manager.h`, `pm_forms.cpp`.

**Current calc:** controls inherit global `FramePadding (12,7)` → height `~fontH+14`, all scaled. Header
icon `getIcon(icon,20)` drawn `18*uiScale`. OK/Cancel `btnW=28*uiScale`. Fields stack
label-above-input, each `SetNextItemWidth(-1)` (full pane); no label/input column grid. Extrude form
= `pm_forms.cpp:128-146` (10 fields).

| # | Sev | Site | Problem | Fix |
|---|---|---|---|---|
| 3.1 | High | `property_manager.cpp:1302,1323` | ✅ **the reported "?"** — `getIcon("ok"/"cancel")` don't exist (atlas has `ok_check`/`cancel_x`); text fallback `✓`/`✗` also outside font ranges | use `ok_check`/`cancel_x`; ASCII fallback |
| 3.2 | High | `property_manager.cpp:1383,1398` | ✅ Dim `SetNextItemWidth(-1)` then `-`/`+` steppers `SameLine` → steppers overflow/clip the pane (most visible Extrude defect) | reserve stepper width via `SetNextItemWidth(-(stepBtnW*2+gaps))` |
| 3.3 | Med | `property_manager.cpp:1284-1293` | ⬜ OK/Cancel right-aligned onto the description line (not top-right); narrow dock dumps them far-left over the title | draw on their own row above the separator |
| 3.4 | Med | whole form | ⬜ no label/input column grid — every field full-width stacked; tall, unaligned vs SolidWorks | fixed `labelW=96*S` + `SameLine` + input |
| 3.5 | Med | `property_manager.cpp:1266,1285,1315` | ✅ (partial) header/button gap literals `6`/`4` unscaled | `*uiScale` (done for the button pair; header icon gap `:1266` still ⬜) |
| 3.6 | Low | `pm_forms.cpp:138` | ⬜ Extrude `draft` declared `Dim()` not `Angle()` → shows "[mm]" on an angle field | use `Angle()` |
| 3.7 | Low | `property_manager.cpp:1510,1644` | ⬜ SelectorWell chip is a single-line `Button` → long placeholders truncate | bordered child w/ wrapped text, or shorter placeholder |

## 4. FeatureManager tree
File: `host/src/ui/feature_tree.cpp`.

**Current calc:** rows = ImGui `TreeNodeEx` (height `GetTextLineHeightWithSpacing()`, metric-derived,
correct). Per-row icons drawn at literal **16/20 px, NOT scaled** (`treeIcon` default 16; feature rows
20). Indent `Indent(14.0f)` unscaled. Toolbar button `24*uiScale` glyph `18*uiScale` (correct —
reference pattern). Rollback band `9*uiScale`. Badges = font glyphs.

| # | Sev | Site | Problem | Fix |
|---|---|---|---|---|
| 4.1 | High | `feature_tree.cpp:54,57` + ~10 call sites | ⬜ tree icons never `*uiScale` → glyphs stay 16/20px while rows grow at HiDPI (tiny, misaligned) | bake `*uiScale` into `treeIcon` |
| 4.2 | Med | `feature_tree.cpp:54-57` vs `403-407` | ⬜ header rows draw icons at 16, feature rows at 20 → ragged icon column | one tier (16*S) for all rows |
| 4.3 | Med | `feature_tree.cpp:404,443,618` | ⬜ nested-sketch `Indent/Unindent(14.0f)` unscaled → flattened hierarchy at HiDPI | `14*S` or `IndentSpacing` |
| 4.4 | Low | `feature_tree.cpp:1306,1320,1758` | ⬜ Solid Bodies / folders borrow `mates_folder` icon though `body_folder` exists | use `body_folder` |
| 4.5 | Low | many | ⬜ icon↔label `SameLine(0,4)` and draw-list thickness/rounding (`2/2.5/3`) unscaled | `*S` |
| 4.6 | Low | `feature_tree.cpp:1030,1043` | ⬜ rollback bar thin/subtle vs SW (ergonomic) | band `12*S`, line `3*S`, handle `28×8*S` |

## 5. Icon system
Files: `host/src/ui/icons.{cpp,h}`, `host/assets/icons/atlas.json`, `host/assets/make_icons.py`.

**Current calc:** one pre-rendered atlas, cells `name@32`/`name@20` (`make_icons.py` → `atlas.json`).
`getIcon(name,size)` builds `"name@size"`, retries the other size, else returns the magenta
placeholder + logs `[icons] unknown icon` once. Nothing cross-checks C++ name strings vs the atlas →
4 wrong names shipped.

| # | Sev | Site | Problem | Fix |
|---|---|---|---|---|
| 5.1 | High | `property_manager.cpp:1302,1323` | ✅ `ok`/`cancel` → `ok_check`/`cancel_x` | (done) |
| 5.2 | Med | `asm_panels.cpp:460` | ✅ `asmIcon("pattern")` → `pattern_component` | (done) |
| 5.3 | Med | `ribbon_def.cpp:265` | ✅ `"ai_design"` → `ai_panel` (AI Design ribbon button) | (done) |
| 5.4 | Med | `icons.cpp` / startup | ⬜ no validation of referenced names → silent magenta in front of users | add `ui::hasIcon()` + startup `validateIconNames()` over the static tables (style guide §5) |

---

## Recommended fix order

1. ✅ **Done this pass (committed):** 5.1–5.3 (4 broken icons), 1.1 (Settings theme-scale bug),
   3.1/3.2 (PM OK/Cancel icons + Dim stepper overflow) — the user-visible breakage.
2. **Next (mechanical, low-risk, High sev):** 4.1 tree-icon scaling, 2.6 dock-clamp px, 2.1–2.3
   ribbon width/overlap math. One commit each, headless-verified.
3. **Then (Med, contained):** 5.4 icon-name validation guardrail, 3.3/3.4 PM button row + two-column
   grid, 2.4/2.5 ribbon + tab overflow, 1.2 runtime DPI handler.
4. **Polish (Low):** the remaining unscaled literals, dead code, duplicated helpers.
