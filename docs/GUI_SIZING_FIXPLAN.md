# GUI Sizing Fix Plan — curated from the lint triage

> Produced 2026-06-14 by a 6-agent read-only triage of every `gui_lint.py --rule scale` finding
> (395 after linter hardening). This is the **execution checklist** for the chrome-sizing burn-down:
> each entry is FIX (exact edit via `host/src/ui/metrics.h`), SUPPRESS (world/sheet-space), or
> FALSE-POS. **No code was changed in the triage pass** — apply this once the tree is stable.
> Standard: [GUI_STYLE_GUIDE.md](GUI_STYLE_GUIDE.md) · Tooling: [GUI_TOOLING.md](GUI_TOOLING.md).

## Status legend
✅ free file (ready to execute) · 🔒 owned by the in-flight layout rework (defer) · 🌍 world/sheet-space (exclude bulk, fix only chrome exceptions)

## Triage summary

| file | findings | real FIX | suppress/FP | class |
|---|---|---|---|---|
| `ui_theme.cpp` | 10 | **0** | 10 (base style values, by design) | ✅ FALSE-POS |
| `settings_modal.cpp` | 29 | 15 | 0 | ✅ |
| `legacy_chat.cpp` | 24 | 13 | 0 | ✅ |
| `view_selector.cpp` | 12 | 12 (+2 unflagged) | 0 | ✅ |
| `asm_panels.cpp` | 21 | 21 | 0 | ✅ |
| `widgets/ribbon.cpp` | 17 | 17 + **3 structural** | 0 | ✅ |
| `sim_panel.cpp` | 4 | 4 | 0 | ✅ |
| `activity_panel.cpp` | 4 | 4 | 0 | ✅ |
| `status_bar.cpp` | 2 | 2 | 0 | ✅ |
| `evaluate_dialogs.cpp` | 2 | 2 | 0 | ✅ |
| `legend.cpp` | 6 | 6 (+6 the linter missed) | 0 | ✅ |
| `ecad/ecad_panel.cpp` | 3 | 3 | 0 | ✅ |
| `ecad/ecad_canvas.cpp` | 37 | ~6 (text offsets, focus crosshair) | ~31 world | 🌍 |
| `viewport_panel.cpp` | 27 | ~20 (HUD/overlay chrome) | ~7 already `*sc`/`*ts` | 🌍 |
| `drawing_panel.cpp` | 21 | ~10 (pill pad/rounding) | rest color/sheet | 🌍 |
| `sketch_mode.cpp` | 157 | ~30 (item widths, pills, panels) | ~127 draw-list art | 🌍 exclude bulk |
| `section_tool.cpp` | 0 | 0 (all already `*s`) | — | ✅ done |
| `title_bar.cpp` | 8 | defer | — | 🔒 |
| `property_manager.cpp` | 5 | defer | — | 🔒 |
| `config_panel.cpp` | 2 | defer | — | 🔒 |
| `left_pane.cpp` | 8 | defer | — | 🔒 |

**~120 real chrome fixes** across the ✅ files + the 🌍 chrome exceptions. Layout-agent files (🔒) excluded.

---

## Step 1 — extend `metrics.h` with the high-reuse tokens

These values recur 3+ times across the triage. Add to `host/src/ui/metrics.h`:

```cpp
// overlay pills / chips (sketch_mode, viewport, legend, drawing_panel, view_selector)
inline float chipRound()  { return rounding(6.0f); }   // standard overlay pill corner
inline float chipRoundSm(){ return rounding(4.0f); }
inline float chipPadX()   { return px(12.0f); }
inline float chipPadY()   { return px(5.0f); }
// assembly panel forms
inline float asmBtnW()        { return px(90.0f); }    // Add/Close/Insert/Collapse buttons
inline float asmInputW()      { return px(120.0f); }   // mate/pattern numeric fields
inline float asmHeaderIconGap(){ return px(6.0f); }    // SameLine(0, gap) after a header icon
// ribbon chrome
inline float ribbonBtnRound()   { return rounding(3.0f); }  // button + tab fills
inline float ribbonSplitInset() { return px(4.0f); }        // big-button split divider inset
inline float ribbonSmSplitInset(){ return px(2.0f); }       // small-button + tab underline inset
// sketch canvas glyphs (screen-px draw-list art)
inline float sketchHandleR()   { return px(3.0f); }
inline float sketchHoverRingR(){ return px(6.0f); }
```
(Everything else uses the generic `px(n)` / `U(n)` / `stroke(t)` / `rounding(r)`.)

---

## Step 2 — ribbon STRUCTURAL fixes (audit §2.1–2.3) — do these carefully (visual verify)

**§2.1 `ribbon_bigBtnW` double-scales the label (`ribbon.cpp:121-129`).** `CalcTextSize` is already
screen-px; it's compared to 1.0× consts then `* s` again. Rewrite all-in-screen-px:
```cpp
float lblWScreen = ribbon_wrapLabel(label, l1, l2) + kBigLblPad * 2.0f * s;
float base = kBigBtnW * s;
if (lblWScreen > base) base = lblWScreen;
if (base > kBigBtnMaxW * s) base = kBigBtnMaxW * s;
return base + (hasDrop ? kDropArrowW * s : 0.0f);
```
**§2.2 measure ≠ draw width.** `ribbon_smBtnW` (`:135`) returns `text + 38*s`; `ribbon_smBtn`
(`:746`) draws `text + 30*s`. Make the draw side call the shared helper:
`float totalW = ribbon_smBtnW(label);`
**§2.3 degraded row overlap.** `ribbon_bigBtnAsSmall` row is `26*s` tall but `sy += kSmRowH*s`
(`:1105`) advances `22*s`. Add a helper and advance by it:
```cpp
static float ribbon_bigBtnAsSmallRowH() {
    float s = ui::uiScale;
    return ImMax(ribbon_snap((kBodyH - kCaptionH) * s / 3.0f), kSmRowH * s);
}
// call site (~:1105):  sy += (idx < nBig) ? ribbon_bigBtnAsSmallRowH() : kSmRowH * s;
```

## Step 3 — ribbon literal fixes (`ribbon.cpp`)
`:429 :442 :465 :558 :909` rounding `3.0f` → `ribbonBtnRound()` · `:446 :1116 :1117` inset `4` →
`ribbonSplitInset()` · `:573 :913-914` inset `2` → `ribbonSmSplitInset()` (the `-1` tab nudge → `px(1)`).

## Step 4 — assembly panels (`asm_panels.cpp`, all 21)
`:139 :261 :460 :596 :660 :779` `SameLine(0, 6)` → `SameLine(0, asmHeaderIconGap())` ·
`:161 :167 :345 :358 :543 :559 :607 :609` `ImVec2(90,0)` → `ImVec2(asmBtnW(),0)` ·
`:331 :499 :502 :506 :509` `SetNextItemWidth(120)` → `SetNextItemWidth(asmInputW())` ·
`:822` `SetNextItemWidth(-90)` → `-asmBtnW()` · `:825` `ImVec2(80,0)` → `ImVec2(px(80),0)`.

## Step 5 — settings / chat / view_selector
- `settings_modal.cpp`: every `Dummy(ImVec2(0, N))` → `U()` multiples (2→`U(0.5f)`, 4→`U(1)`,
  6→`U(1.5f)`, 8→`U(2)`) at `:149 :171 :181 :186 :190 :195 :220 :237 :324 :334 :346 :348 :415 :418`;
  `:340 SetNextItemWidth(140)` → `px(140)`.
- `legacy_chat.cpp`: `Dummy(0,N)` gaps at `:46 :181 :209 :244 :377 :621 :667 :675 :729 :734` → `U()`;
  `:185 SameLine(0,7)`→`px(7)`, `:415 SameLine(0,6)`→`px(6)`, `:184 ColorButton ImVec2(10,10)`→`px(10)`.
- `view_selector.cpp` (`:355-357` tooltip box): pad `+12`→`chipPadX()`, `+8`→`px(8)`,
  rounding `4.0`→`chipRoundSm()`, text offset `+6/+4`→`px(6)/px(4)`; also fix the `:353-354` clamp
  guards (`+12`,`+8`) to match (linter didn't flag them).

## Step 6 — small chrome
- `sim_panel.cpp` `:417 :445 :473` `ImVec2(120,0)`→`px(120)`, `:479` `ImVec2(0,60)`→`px(60)`.
- `activity_panel.cpp` `:488 :542` `Dummy(0,4)`→`U()`.
- `status_bar.cpp` `:41` `+8`→`px(8)`.
- `evaluate_dialogs.cpp` `:155` `SetNextWindowSize(ImVec2(340,…))`→`px(340)`.
- `ecad_panel.cpp` `:443` Dummy `+6`→`px(6)` & swatch rounding `2.0f`→`rounding(2)`, `:666` swatch
  rounding `2.0f`→`rounding(2)`.

## Step 7 — legend (`legend.cpp`) — includes 6 the linter MISSED (const-float assignments)
`:59 barW=18.0f`→`px(18)` · `:60 barH=200.0f`→`px(200)` · `:61 pad=8.0f`→`px(8)` ·
`:62 labelW=64.0f`→`px(64)` · `:65 +18.0f`→`px(18)` · `:66 -12.0f`→`px(12)` ·
`:69 :70` rounding `6.0f`→`chipRound()` · `:96` tick `+4`→`px(4)` · `:97` label `+7/-7`→`px(7)`.
> Linter follow-up: the `const float barW = 18.0f;` pattern is a false-NEGATIVE class (size stored
> in a local then used downstream). Note in GUI_TOOLING; not worth dataflow in the linter.

## Step 8 — 🌍 world/sheet files: fix only the chrome exceptions, suppress the bulk
- `viewport_panel.cpp` HUD/overlay FIXES: `:967-968` color-btn `ImVec2(12,12)`+`SameLine(0,6)` →
  `px(12)`/`px(6)`; `:1835` hover rounding→`rounding(3)`; `:1848` tick insets `3`→`px(3)`;
  `:2059 :2064 :2068-2069 :2078-2080 :2305 :2348 :2364-2365 :2403-2407` progress pill / pick-readout
  chip / triad label chip / dims pill / plane-pick chip → `px()`/`chipPad*()`/`chipRound*()`.
  SUPPRESS (already `*sc`/`*ts`): `:1891 :1956 :1967-1968 :1999 :2224 :2318`.
- `ecad_canvas.cpp` FIXES: `:279 :281 :288 :299 :427` text offsets → `px()`; `:519-520` focus
  crosshair thickness→`stroke(2.5f)`, arms `5`→`px(5)`. SUPPRESS the ~31 `pxPerMm_`-driven world radii.
- `drawing_panel.cpp` FIXES (~10): `:440 :737-738 :862-863 :1035-1036 :1051 :1085-1086` pill
  pad/rounding/offsets → `px()`/`rounding()`. SUPPRESS the IM_COL32 channels (mostly auto-handled now).
- `sketch_mode.cpp` FIXES (~30): `SetNextItemWidth` widths at `:1101 :1105 :1164-1171 :6430 :6456
  :6479-6488` → `px()`; confirmation button `:6047` `sz=30/pad=12` → `px()`; overlay pills
  `:3597-3599 :6243-6245 :6259-6261 :6275` → `chipRound()`/`chipPad*()`; selected handle `:3901` and
  hover rings `:4255 :4730 :4742` → `px()`. SUPPRESS the bulk of endpoint-dot/ring/grid draw-list art
  (entity glyphs) — recommend a **file-level exclude** of `sketch_mode.cpp` from the `scale` rule
  after the ~30 real fixes land, since the remaining ~127 are screen-glyph radii that floor at 1px.

## Step 9 — FALSE-POS (no change)
`ui_theme.cpp` `:281-285` (WindowPadding/FramePadding/ItemSpacing/ItemInnerSpacing/CellPadding) are
the **authored 1.0× base style values** that `ScaleAllSizes(scale)` multiplies once — wrapping them
would double-scale. Leave as-is; exclude `ui_theme.cpp` from the `scale` rule (or add the 5 lines to
the baseline).

---

## Linter follow-ups (cheap, raise precision further)
1. Exclude-file list for `scale`: `ui_theme.cpp` (base values) and `sketch_mode.cpp` (draw-list art,
   after its ~30 chrome fixes land).
2. Skip the **thickness/segment** trailing arg of `AddCircle*` (the hardening did integer segment
   counts; the float thickness arg is still flagged in a few spots — usually a real fix, keep).
3. Known false-negative: sizes stored in `const float x = 18.0f;` then used downstream (legend) —
   document, don't chase with dataflow.

## Execution order (once the tree is stable / layout agent has committed)
1. metrics.h tokens (Step 1) — one commit.
2. Mechanical ✅ chrome (Steps 4–7) via sonnet/haiku subagents, one file per agent, **re-run the
   linter per file to confirm findings drop**, build once at the end, commit.
3. ribbon structural (Step 2) — separate commit, **visual verify** (the double-scale fix changes
   button widths at HiDPI).
4. 🌍 chrome exceptions (Step 8) + the exclude-list (Step 9, linter).
5. Generate the committed baseline from whatever remains; wire `--rule icons --rule theme` into CI.
