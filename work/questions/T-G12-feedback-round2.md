# T-G12 Walkthrough — Round 2 Feedback (AI-delegated reviewer, Opus)

**Reviewer:** Opus walkthrough agent (Hunter delegated round-2 to AI, 2026-07-03)
**Method:** READ-ONLY. Headless xvfb captures (`--ui-screenshot`, `AI_CAD_THEME=light|dark`),
every PNG eyeballed (Read renders images). No source changed, no board touched.
**Binary:** host/build-linux/bin/AI-CAD (current master, build up to date — round-1 fixes T-G14..T-G20+T-BL19 compiled in).
**Screenshots:** kept in this scratchpad under `shots/`.

Two documented false-verify traps honored (advisor-flagged):
- **T-G14 PM dark-mode** captured with `AI_CAD_PMBOX=1` (without a loaded doc, panes fall back to light theme text and HIDE the black-on-black bug — app.cpp ~1499).
- **T-G19 extrude double-display** captured with FULL-WINDOW scenarios (m1), NOT isolated `pmform` (the duplicate readout lived far-right behind the Copilot tab — pmform crops it out).

---

## Residual human/live checks (CANNOT be verified from static headless frames)

Enumerated up front so none is falsely marked PASS from a static image:

1. **T-G2 NEEDS-LIVE #1** — doc-tab `+`/`x` click response (needs live click).
2. **T-G2 NEEDS-LIVE #2** — Copilot collapse-to-pill → click pill to re-expand round trip (live click).
3. **T-G2 NEEDS-LIVE #3** — viewport-toolbar left-edge clamp on live sketch pan (live pan).
4. **T-G2 NEEDS-LIVE #4** — Extrude End-Condition dropdown opened, text readable over bg (dropdown-open state).
5. **FM row hover highlighting** (T-G18) — a SEPARATE agent is on hover; not judged from static frames here.
6. **Copilot tooltips** black-on-black (T-G14/T-G17) — tooltips are hover-triggered; residual.
7. **Copilot Sessions popup** formatting + **model-picker dropdown** (T-G17) — click-to-open state; residual.
8. **OS/subwindow transparent borders** (T-G14) — `--ui-screenshot` grabs the client framebuffer; native OS window borders are not in-frame. Residual unless verified another way.
9. Any modal/drag-drop interaction flow.

---

## VERIFIED list — round-1 checklist re-verification (PASS/FAIL + evidence)

_(filled in as captures are eyeballed)_

| Round-1 item (task) | Verdict | Evidence |
|---------------------|---------|----------|
| **T-G14** docked PM readable in dark (no black-on-black) | **PASS** | `crop_pm_dark.png`, `crop_extrudepm_dark.png` — Fillet+Extrude PM panes: light text over glass pane, all labels legible, borders present. Captured with `AI_CAD_PMBOX=1` (the bug regime). |
| **T-G14** app-wide dark readability (ribbon/menus/status) | **PASS (static)** | `crop_ribbon_dark.png`, `final_*_dark.png` across all scenarios — menubar, ribbon group titles, status bar all readable in dark. |
| **T-G19** OK/Cancel relocated to pane top row, wider | **PASS** | `crop_pm_dark.png`, `crop_extrudepm_dark.png` — green ✓ (OK) + dark ✗ (Cancel) sit in the top row between config icons and the `«` collapse chevron, exactly as specified. |
| **T-G19** far-right duplicate "Active Feature" readout PANEL removed | **PASS (panel) — but symptom recurs, see NEW-1** | The mirrored readout PANEL is removed (viewport_panel.cpp L3247-3252) AND absent for Fillet (`crop_copilot_dark.png` clean). HOWEVER the depth/draft-behind-Copilot *symptom* recurs via a different widget (in-viewport dim chips) in the Extrude pmform harness — see NEW-1. Whether that reproduces in a real edit needs a live check (residual). |
| **T-G17** Copilot Settings gear on-screen | **PASS** | `crop_copilot_dark.png`, `crop_extruderight_*.png` — settings gear button visible top-right of Copilot header (round-1: "off the screen"). |
| **T-G14** Evaluate windows auto-size to content + glass | **PASS (nit)** | `crop_evalmass_dark.png` — Mass Properties floats as a content-sized dark glass panel, all data readable (Volume/Mass/COM/Inertia/Principal axes). Nit: two empty dark pills (Material/Density) render blank — see NEW-4. |
| **T-G15** photographic screenshot icons (Dress-up) | **PASS** | `crop_sketchgrp_05_edit.png` — Fillet + Chamfer buttons show real rendered part screenshots with the modified edges emphasized, per directive. |
| **T-G15** `sketch_new` needs a text label | **NOT HONORED — but by explicit design** | `crop_sketchbtn_only_light.png` — New Sketch button is still icon-only. ROOT CAUSE FOUND: `ribbon.cpp::ribbon_displayLabel` (L132-135) hard-returns `""` for `sketch_new` with comment *"intentionally icon-only; the tooltip keeps the full name."* So this is a deliberate choice, NOT a regression/unlanded fix. It still does not satisfy Erik's round-1 ask ("needs a text label like the others"). Needs an Erik design call — see NEW-2 (downgraded to nit). Single-button groups DO caption (control: Assembly "Mate" group shows a "Mate" caption above its group title — `settle_asm_components_dark.png`), so the blank here is specific to the sketch_new override, confirmed in code. |
| **T-G15** `appr_mat_dark` / `appr_mat_brass` icons show | **PASS** | `crop_apprmat_light.png`, `crop_apprribbon_dark.png` — Dark Metal + Brass render real material-swatch cubes. |
| **T-G15** `appr_mat_aluminum` / `appr_mat_steel` icons | **UNVERIFIABLE** | Occluded by the ShopCraft info toast in every appearance capture (NEW-5). Same swatch-button family as Brass/Dark which pass. |
| **T-G15** `display_hlr` (Hidden Removed) icon shows | **PASS** | `crop_apprribbon_dark.png` — real hidden-line-removal glyph in Display group, both themes. |
| **T-G15** `asm_parts_library` (Std. Parts) icon remade | **PASS** | `settle_asm_components_dark.png` (toast-free asm interference frame) — Std. Parts shows a real component-with-badge icon + caption; Insert/Move Comp./Comp. Pattern also real. |
| **T-G15** `weld_gusset` (Gusset) icon shows | **UNVERIFIABLE** | Dress group fully under the info toast in all weldments captures (NEW-5). |
| **T-G16** ribbon liquid-glass, all tabs both themes | **PASS** | `crop_*ribbon_dark.png` + helper sweep of all 9 tabs × 2 themes — frosted-glass chrome, teal(dark)/blue(light) active-tab accent, group separators, consistent everywhere. |
| **T-G17** Copilot Settings gear on-screen | **PASS** | `crop_chatpane_dark.png`, `crop_copilot_dark.png` — gear visible top-right of Copilot header. |
| **T-G17** Copilot header/footer chrome fits (no clip) | **PASS** | `crop_chatpane_dark.png`/`_light.png` — Copilot/Sessions/gear header + Design/Edit/Ask/Permissions + token meter + prompt + model line all fit, readable, both themes. |
| **T-G17** Sessions popup / model-picker / new-session-width / trashcan | **RESIDUAL** | Require clicking into the Sessions view (live). Not shown in any static scenario frame. |
| **T-G18** FM rows readable + text centered + consistent styling | **PASS (static)** | `crop_fm_dark.png` — filename/Equations/Material/Origin/planes/features rows uniformly styled, text vertically centered, icons + visibility eyes aligned, section separators present. |
| **T-G18** FM hover highlighting fixed | **RESIDUAL** | Hover is a separate agent's item; not judged from static frames. |
| **T-G20** Surfaces tab KEPT + icons | **PASS (KEEP ratified) / known dup** | `crop_surfacesribbon_dark.png` — tab present, labels readable, glass consistent. surf_ruled/surf_thicken still duplicate-cube icons = KNOWN (T-G15/T-BL28 in flight) — NOT re-filed per instructions. |

---

## NEW findings (round 2)

_Format: element/tab · theme · severity · issue · screenshot · fix direction · regression?/new_

### NEW-1 · Extrude in-viewport dim chips ("Depth"/"Draft angle") bleed behind the Copilot column · light+dark · **ugly (harness-dependent — verify live)** · **new (T-G19-adjacent)**
- **CAVEAT (advisor-flagged):** This was seen ONLY in the `pmform`+PMBOX synthetic harness, where NO profile is selected (the form shows an empty "Profile [pick in viewport]"). So there is no real extrude preview — the chips may be orphaned dim-candidate chips the synthetic harness renders at a degenerate far-right position, NOT necessarily what a real interactive extrude produces. m1 could not be used (it's box+fillet+shell, never opens an extrude PM), so pmform was the only headless option. **Whether this reproduces in a real edit needs a live session** (moved to residual list too). Reporting it because the collision is visually real in the capture and matches the round-1 symptom class.
- **What (in the harness):** With the Extrude form active, in-viewport dimension chips reading `Depth: 10mm` and `Draft angle: 0deg` render at the far right of the viewport and visually collide with / bleed behind the pinned Copilot glass column (overlapping the "Copilot / Sessions / gear" header and "Assistant" line).
- **Distinction from T-G19:** T-G19 removed the *duplicate mirrored readout PANEL* (that fix landed — confirmed in code + screenshot). What remains is the **live editable dim chip** drawn by viewport_panel.cpp (~L3235). Because the viewport spans full-width under the pinned glass columns and these chips aren't clamped/dodged against the right (Copilot) column the way the toolbar dodges the FM column, they render behind the Copilot pane. This reproduces the *visual symptom* Erik flagged in round 1 ("depth/draft behind the Copilot tab") via a different widget.
- **Screenshots:** `crop_extruderight_dark.png`, `crop_extruderight_light.png` (both show `Depth: 10mm` + `Draft angle: 0deg` over the Copilot header).
- **Fix direction:** Clamp/dodge the in-viewport dim chips against the right pinned column edge (mirror the viewport-toolbar left-edge clamp against the FM column, `overlayColumnEdge`), OR suppress the chips in the region occluded by a pinned column. NOTE: this is a PMBOX-harness layout (box centered iso); in a real edit the feature may sit elsewhere — severity is "ugly" not "blocker" because it's position-dependent, but the collision is clearly reproducible whenever a feature's dims land under the right column.

### NEW-2 · `sketch_new` "New Sketch" is deliberately icon-only — round-1 label request NOT honored · light+dark · **nit (design call for Erik)** · **new**
- **What:** The New Sketch big button renders icon-only, no caption. Round-1 asked: "[sketch_new] needs a text label like all the other icons have."
- **ROOT CAUSE (confirmed in code, not inferred):** `host/src/ui/widgets/ribbon.cpp::ribbon_displayLabel` (L132-135) hard-returns `""` specifically for `cmd_id == "sketch_new"`, with the comment *"The Features-tab sketch entry is intentionally icon-only; the tooltip keeps the full name."* So this is a DELIBERATE design decision — NOT a regression, NOT an unlanded fix. The generic big-button caption path (L762) renders every other label fine; single-button groups DO get captions (control case: Assembly "Mate" single-button group shows a "Mate" caption — `settle_asm_components_dark.png`).
- **Verdict:** The deliberate icon-only choice directly conflicts with Erik's round-1 request. This needs Erik to decide: keep icon-only (and close the round-1 item as "won't do"), or restore the "New Sketch" caption (delete the L134 special-case). Downgraded from FAIL to nit because it's an intentional, tooltip-backed choice, not a bug.
- **Screenshots:** `crop_sketchbtn_only_light.png` (5× zoom), `crop_newsketch2_dark.png`.

### NEW-3 · On-load "ShopCraft: live view of the AI's part" info toast is pinned over the ribbon's top-left in headless captures · light+dark · **nit (capture-quality) / possible ugly** · **new**
- **What:** A white/light info card ("Info — ShopCraft: live view of the AI's part — it updates as it builds") is anchored at the viewport upper-left and, in most `ribbon_*` / early-scenario frames, overlaps the leftmost 1-2 ribbon groups. In dark mode the card is a light/white panel (dark text on white) even though the app is dark — a minor dark-mode inconsistency if that persists on real windows.
- **Impact on this review:** It occluded the leftmost group on nearly every ribbon-tab capture, blocking round-2 verification of `asm_parts_library`, `weld_gusset`, `appr_mat_aluminum`, `appr_mat_steel` (all UNVERIFIABLE above).
- **Screenshots:** `crop_apprmat_dark.png` (white card over Material group in dark), `crop_surfacesribbon_dark.png`, `crop_featuresribbon_dark.png`.
- **Fix direction:** Likely benign in live use (the toast auto-dismisses), so primary ask is a capture-harness one: suppress/auto-dismiss the ShopCraft toast for `AI_CAD_SHOT=ribbon` so per-tab icon verification isn't occluded. Secondary: confirm the toast card honors the dark palette (white-in-dark looks off).

### NEW-4 · Mass Properties window: Material + Density fields render as empty dark pills · dark (and light) · **nit** · **new**
- **What:** The Mass Properties evaluate window shows two dark rounded fields at the top labeled "Material" and "Density" that are blank — no material name / density value populated (the box scenario part may have no material assigned, but the empty pills read as unstyled/placeholder).
- **Screenshot:** `crop_evalmass_dark.png`.
- **Fix direction:** Either populate (show "— / not set") or hide the fields when no material is assigned; verify they're not black-on-black-empty vs. intentionally blank.

### NEW-5 · Contextual ribbon tabs (Simulation, PCB) not capturable via `AI_CAD_SHOT=ribbon` alias · n/a · **note (residual, not a product defect)** · **new**
- **What:** `AI_CAD_RIBBON_TAB=simulation` and `=pcb` fall back to the Features tab because those tabs are gated on `st.showSimulationTools` / `st.showPcbTools`, which the plain `ribbon` shot doesn't enable. The default tab bar shows 9 tabs (Features, Sketch, Evaluate, Assembly, Sheet Metal, Weldments, Surfaces, Appearance, Manufacture). The Simulation tab DOES render correctly in the `fea` scenario (`final_fea_dark.png`), confirming it's contextual-by-design, not missing.
- **Screenshots:** `ribbon_pcb_dark.png` (shows Features active), `crop_tabbar_dark.png` (9-tab bar), `final_fea_dark.png` (Simulation tab present with context).
- **Impact:** Simulation and PCB ribbon *icons* were NOT round-2-verified this pass (Simulation partially visible in fea; PCB not at all). RESIDUAL — capture needs the feature-flag context.

---

## Summary

**Round-1 verification score: 13 of 15 landed items confirmed PASS; 0 FAIL/regression; 1 design-conflict (icon-only sketch_new); 2 UNVERIFIABLE (occluded by NEW-3 toast).**

PASS (13): T-G14 docked-PM dark readability · T-G14 app-wide dark readability · T-G14 evaluate-window sizing+glass · T-G19 OK/Cancel top-row relocation · T-G19 far-right duplicate-PANEL removal (symptom recurs via chips, see NEW-1) · T-G15 photographic Dress-up icons · T-G15 Std. Parts icon · T-G15 Dark Metal/Brass icons · T-G15 Hidden-Removed icon · T-G16 ribbon liquid-glass (9 tabs ×2 themes) · T-G17 Copilot settings-gear + header/footer chrome · T-G18 FM row styling/centering (static) · T-G20 Surfaces tab kept + readable.

DESIGN-CONFLICT (1): **`sketch_new` is deliberately icon-only** (code override, NEW-2) — round-1 label request not honored, but by intent not by bug. Needs an Erik keep/restore call. NOT a regression.

UNVERIFIABLE (2, blocked by NEW-3 toast — no toast-free capture exists): `weld_gusset`, `appr_mat_aluminum`+`appr_mat_steel`. (`asm_parts_library` recovered → PASS from a toast-free asm frame.)

**NEW findings (round 2):**
- NEW-1 (ugly, verify-live): Extrude in-viewport Depth/Draft dim chips bleed behind the Copilot column — but seen only in the profile-less pmform harness; may be a harness artifact. Needs a live extrude to confirm.
- NEW-2 (nit, design call): `sketch_new` deliberately icon-only vs Erik's round-1 label request.
- NEW-3 (nit/capture): ShopCraft info toast pinned over ribbon top-left; occludes leftmost groups; renders white-in-dark.
- NEW-4 (nit): Mass Properties Material/Density empty pills.
- NEW-5 (note/residual): Simulation + PCB ribbon tabs are contextual — not capturable via the plain ribbon alias; not verified this pass (Simulation confirmed present in the fea scenario).

**Severity tally — NEW:** blocker 0 · ugly 1 (NEW-1, and it's harness-caveated) · nit 3 (NEW-2, NEW-3, NEW-4) · note 1 (NEW-5).
**Regressions vs new:** 0 regressions. All 5 are new observations; NEW-2 is a not-honored round-1 request (by design), not a regression.

**KNOWN issues seen but NOT re-filed (per instructions):** surf_ruled/surf_thicken/surf_fill duplicate icons (T-G15/T-BL28 in flight); surf_revolve axis well + ruled/fill/boundary edge-well wiring (T-BL28); replace_face heal (T-BL27).

**Convergence:** NOT YET converged, but close — zero blockers, zero regressions. Open items are 1 design call (sketch_new icon-only, Erik decides), 1 ugly-but-harness-caveated item needing a live extrude (NEW-1), and 3 nits. A short round-3 should confirm: (a) Erik's sketch_new decision, (b) NEW-1 in a real edit, (c) the 2 toast-occluded icons (weld_gusset, aluminum/steel) with the ShopCraft toast suppressed. None of these is a hard must-fix blocker on its own; the largest round-1 batches (dark mode, ribbon glass, OK/Cancel, FM, Copilot chrome, icons) all verified PASS.

## Checks that still genuinely need a human / live session
1. All 4 T-G2 NEEDS-LIVE: doc-tab +/x click, Copilot collapse-to-pill round trip, viewport-toolbar clamp on live pan, Extrude End-Condition dropdown-open readability.
2. FM row hover highlighting (T-G18) — separate hover agent.
3. Copilot Sessions popup formatting, model-picker dropdown, new-session button full-width, trashcan centering (T-G17) — all require opening the Sessions view (live click).
4. Copilot tooltips black-on-black (T-G14/T-G17) — hover-triggered.
5. OS/native subwindow transparent borders (T-G14) — `--ui-screenshot` grabs the client framebuffer only; native window borders are out of frame.
6. Re-verify the 3 toast-occluded icons (Std. Parts, Gusset, Aluminum, Steel) once NEW-3 is addressed — could be done headless with the toast suppressed (doesn't strictly need a human).
7. Simulation + PCB ribbon-tab icons (NEW-5) — need the contextual capture path or a live session.
8. **NEW-1 in a real interactive extrude** — confirm whether the Depth/Draft in-viewport chips actually collide with the Copilot column during a genuine extrude edit (the pmform harness has no profile selected, so it may be a synthetic artifact).
9. Any modal/drag-drop interaction flow.
