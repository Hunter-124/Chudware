# C13 — Theme roles + SolidWorks-light palette + runtime switch

Model: **sonnet** · Wave 2 · Depends: C04 (split landed) · Follow `docs/sessions/README.md`.
BUILD_DIR: `host/build` (you own it this wave unless told otherwise).

## Mission
Replace hardcoded colors with ~40 named palette roles, add a faithful SolidWorks-light palette
alongside the existing dark/teal one, runtime-switchable.

## Read first
1. `host/src/ui_theme.{h,cpp}` (current theme), every file under `host/src/ui/` (the split
   panels — find their hardcoded ImVec4/ImU32 colors)
2. `docs/PLAN.md` §2.1 + §3.1 (theme toggle decision), `contracts/icons.md` (palette refs)

## Owned paths
`host/src/ui_theme.{h,cpp}` · color-only edits inside `host/src/ui/*.cpp` and
`host/src/app.cpp` (replacing literals with roles — no structural changes) ·
`host/src/core/app_state.h` (add `theme` setting field only)

## Deliverables
1. `ui::Palette` → named roles (windowBg, panelBg, headerBg, text, textDim, accent, accentDim,
   border, button{,Hovered,Active}, ribbonBg, ribbonTabActive, treeRow{,Alt,Selected},
   viewportTop, viewportBottom, selectionHighlight, hoverHighlight, statusOk/Warn/Err,
   chipBg, codeBg, diffAdd/Del/Hunk, …extend as the panels demand). `applyTheme(Theme, scale)`
   fills ImGuiStyle + the Palette; panels read ONLY roles.
2. SolidWorks-light palette: near-white chrome (#F0F0F0 ribbon, #FFFFFF panels), slate text,
   blue-ish selection (SW uses #0078D7-ish), the existing viewport gradient swapped for the
   SW light-gray studio gradient. Dark palette = current look, byte-for-byte where possible.
3. Runtime switch: `Settings.theme` ("dark"|"light") persisted in config.json; View menu item
   + Settings toggle; switching calls applyTheme + swaps the icon atlas binding IF
   `host/assets/icons/` exists (C05 — guard on file presence, don't depend on its card).
4. Headless verification: support `AI_CAD_THEME=light` env override; capture
   `AI_CAD_THEME=light` + default screenshots of the main mode into `scratch/theme_shots/`.

## Constraints
- No layout/spacing changes in dark mode (compare against C04's `scratch/split_after/`
  screenshots — dark default should remain visually identical; tiny diffs from rounding are
  acceptable, justify them).
- Do not touch renderer internals beyond `setBackground` values per theme.

## Verification
Release build green; both-theme screenshots captured and described in your report; dark default
visually unchanged.
