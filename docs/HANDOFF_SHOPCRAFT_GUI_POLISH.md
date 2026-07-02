# HANDOFF — ShopCraft (formerly AI-CAD) GUI polish + pending rename (2026-07-02)

Durable copy of the live session handoff. The actionable items here are tracked as
work-system tasks: **T-G1** (land the uncommitted tree), **T-G2** (doc-tab input +
Copilot header), **T-G3 / T-G10** (ShopCraft rename tiers), **T-G4** (cleanliness pass).
See `work/tasks/` and `work/DAG.md`. Companion docs: `docs/HANDOFF_GUI_OVERHAUL.md`,
`docs/HANDOFF_GUI_GLASS_POLISH.md`, `docs/GUI_MIGRATION_DAG.md`.

Repo: `/home/hunter/Repositories/CAD` • Host GUI: Dear ImGui, C++ (`host/src`)
Build: `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"`
Screenshots: `AI_CAD_SHOT=<scn> AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png`
  scenarios: m1 (box+fillet+shell, edits fillet→PM active), sketch (enter/dim/solid),
  linear_dim, fillet, chamfer, cylinder, measure, plane, appearance. Per-step PNGs land in
  `scratch/<scn>_shots/`. Note: most scenarios EXIT sketch/PM before the final
  `--ui-screenshot` frame; use the `scratch/<scn>_shots/*_NN_*.png` step captures to see
  mid-edit state.
Project rule: run `graphify query "<q>"` before grepping; graphify indexes ui/abstract+core,
  NOT host/src — for host renderer, targeted grep/read is expected.

## STATE

All changes below are DONE + build-green but UNCOMMITTED (7 files). Nothing committed
that session. Working tree also has PRE-EXISTING unrelated edits from before that session
(the GUI-overhaul Commit A set — see `docs/HANDOFF_GUI_OVERHAUL.md` §6). Commit in the
logical pieces listed under "COMMIT PLAN" before more work → **T-G1**.

## COMPLETED THAT SESSION (verified via screenshot unless noted)

1. Input regression fix — FeatureManager took NO mouse input. Cause: commit 043e752 added
   `NoBringToFrontOnFocus` to the FM window. Removed it (`host/src/ui/layout.cpp`
   `drawLeftPane`). Also fixed the collapsed FM pill: removed its NoBringToFront + made it
   publish its column edge.
2. Transparent columns — removed the whole-column glass slab from FeatureManager
   (`layout.cpp`) and Copilot (`legacy_chat.cpp` `drawAIPanel`). Columns are now fully
   transparent (viewport shows through); only per-element pills carry glass. This also
   killed a Copilot "offset backdrop" refraction artifact. Adaptive-surface lookups kept
   for text contrast + edge-publish kept.
3. Columns hug the ribbon — top margin 12dp→4dp for FM (`layout.cpp`), Copilot
   (`legacy_chat.cpp`), and the doc-tab strip (`doc_tabs.cpp` `docY`).
4. Copilot collapse-to-pill — mirrors FM. Collapse chevron is now the LEADING item of the
   Copilot header row (`drawCopilotHeader`, `legacy_chat.cpp` ~1229) so it can't overlap
   the logo; `s_cpCollapsed` is file-scope; collapsed pill at the right anchor re-expands +
   publishes edge slot 1. NEEDS LIVE CLICK-TEST.
5. PropertyManager tab REMOVED (duplicated FeatureManager). PM command form now hosts
   INSIDE the FeatureManager tab body; header switches to "Property Manager" while a
   command is active; attention dot moved to the FeatureManager tab. Left-pane tabs now:
   Explorer / FeatureManager / Configurations. (`layout.cpp`)
6. Sketch DOF/status pill → top-CENTER "dynamic island" (was top-left); tool-hint stacks
   below it at y+52. (`sketch_mode.cpp` `drawOverlay`)
7. Sketch accept/deny — removed the floating in-viewport ✓/✗ corner + its 2 hover-exclusion
   zones. Finish (✓)/Cancel (✗) now render as a right-aligned SQUARE green/red commit
   cluster in the Sketch-tab ribbon, shown only while editing a sketch (`ribbon.cpp`; fires
   `sketch_exit` / the new `sketch_cancel` cmd id, handled in `sketch_mode.cpp`
   `onRibbonCommand`).
8. Fixed active-feature readout panel — top-right of viewport (left of Copilot, below the
   toolbar), read-only, lists the editing feature's dims (e.g. "Fillet1 / Radius: 8mm").
   Static over-geometry dim chips suppressed (drag arrows + live drag readout kept).
   (`viewport_panel.cpp`; uses `dimCandidates` + `overlayColumnEdge(1)`)
9. Sketch overlay dodges columns — sketch draw clip + HUD banners inset to the inter-column
   safe area via `overlayColumnEdge(0/1)`; imgMin/imgMax kept as true bounds for
   projection. Added `#include "layout.h"` to `sketch_mode.cpp`. NEEDS LIVE TEST with a
   large/panned sketch.
10. Viewport toolbar (`huBtn` zoom/pan row) now clamped to dodge BOTH columns (already
    dodged Copilot; added FeatureManager-edge left clamp). (`viewport_panel.cpp` ~2753)
    NEEDS LIVE CONFIRM.
11. Extrude "black on black" — PM form field frames used ImGui's opaque-dark FrameBg while
    text is adaptive-dark (over the now-transparent column on a light viewport). Fixed in
    `property_manager.cpp` `drawBody`: pushed translucent-glass FrameBg/Hovered/Active from
    `surf.tint` (0.16/0.26/0.30 alpha, balanced `PopStyleColor(3)` on all return paths) +
    pushed light text (`pal.text`) inside both BeginCombo popups (`##combo`, `##datum`).
    NEEDS LIVE CONFIRM on the actual Extrude menu incl. opening the End-Condition dropdown.

## PENDING (diagnosed, NOT yet applied — do these first next session) → T-G2

A. Doc-tab strip input broken — clicking "+" (new part) / "x" (close) does nothing. Same
   regression class as #1: `##DocTabsOverlay` window carries
   `ImGuiWindowFlags_NoBringToFrontOnFocus` (`host/src/ui/doc_tabs.cpp:327`, in `dtFlags`).
   FIX: remove that flag (exactly like the FM fix). If input still dead, add a temporary
   `GImGui->HoveredWindow->Name` foreground-drawlist readout to see what window is stealing
   the click (that diagnostic found the FM cause). Verify by clicking the "+" and a tab "x"
   in the live app.
B. Copilot header "AI-CAD Copilot" is too wide — make it horizontally smaller. In
   `drawCopilotHeader` (`legacy_chat.cpp` ~1238) it uses `st.fonts.h2`; drop to a smaller
   font (`st.fonts.medium` or a dedicated smaller size) and/or shrink the logo image
   (h = `GetTextLineHeight()`). Text becomes "ShopCraft Copilot" after the rename.

## PENDING BIG TASK: rename AI-CAD → ShopCraft → T-G3 (branding) + T-G10 (build + deep)

Scope was being decided; user leaning "entire project". Surface: ~88 files contain
"AI-CAD", ~46 "AI_CAD", 8 "ai-cad". Do it in tiers and rebuild/re-register between:

- **Branding** (T-G3): window title (`main.cpp:164`, `app.cpp:763`), "AI-CAD Copilot" →
  "ShopCraft Copilot" (`legacy_chat.cpp:1240`), About/splash/ribbon, any on-screen "AI-CAD".
- **Build/binary** (T-G10): CMake `project(AI_CAD_Host)` (`host/CMakeLists.txt:2`) +
  `add_executable(AI-CAD ...)` (`:41` and ~10 `target_*` refs) → ShopCraft; update
  run/build scripts + docs that call `bin/AI-CAD`. NOTE: build dir is `host/build-linux`;
  a target rename changes `bin/<name>`.
- **Deep** (T-G10; breaks tooling until re-registered): env vars `AI_CAD_SHOT` /
  `AI_CAD_REPO` (`scenarios.cpp` + any harness) → `SHOPCRAFT_*`; MCP server id "ai-cad"
  (renaming breaks the connected MCP tools until re-added); appdata dir "AI-CAD"
  (`bootstrap.cpp:316-319` + handoff path `~/.claude/.../AI-CAD/handoff/live.json`) →
  needs a migration/move.

Confirm the tier before executing; a blind global sed WILL break the build/MCP/paths.
The build/deep tiers are deliberately sequenced AFTER the ui/imgui cutover (T-G9) so the
rename happens once, in the surviving tree, and `host/` is deleted un-renamed.

## COMMIT PLAN → T-G1

Suggested pieces; `git add <exact files>`, never `-A`; end msgs with
`Co-Authored-By: Claude <noreply@anthropic.com>`:

1. input fix: `layout.cpp` (FM + pill NoBringToFront) — "fix(gui): restore FeatureManager mouse input"
2. transparent columns: `layout.cpp`, `legacy_chat.cpp` — "style(gui): hide column bounding box, pills only"
3. columns hug ribbon: `layout.cpp`, `legacy_chat.cpp`, `doc_tabs.cpp`
4. sketch HUD + ribbon accept/deny: `sketch_mode.cpp`, `ribbon.cpp`
5. feature readout panel + overlay dodge: `viewport_panel.cpp`, `sketch_mode.cpp`
6. PM tab removal + black-on-black: `layout.cpp`, `property_manager.cpp`

(Some files span pieces — commit by hunks or fold sensibly.) The working tree ALSO holds
the earlier GUI-overhaul Commit A set (`host/src/**` per `HANDOFF_GUI_OVERHAUL.md` §6) and
the AI-experience Commit B set — T-G1/T-S2 cover landing those. NEVER scoop
`ui/abstract/panel_spec.*` (separate in-flight lane), `services/mcp/cad_tools.py`, or
`docs/REFACTOR_LOG.md`.

## STILL OPEN / WATCH

- "Select with AI" button in the PM form is dark-text-on-dark (`ImGuiCol_Button`, NOT
  covered by the FrameBg fix) — confirm with user whether it's part of the black-on-black
  complaint. Folded into T-G2.
- Locked design memory updated:
  `~/.claude/projects/-home-hunter-Repositories-CAD/memory/gui-overlay-design-decisions.md`
  (transparent columns now OVERRIDE the old "pinned glass columns" intent).
- Companion repo docs: `docs/HANDOFF_GUI_GLASS_POLISH.md`, `docs/HANDOFF_GUI_OVERHAUL.md`.
