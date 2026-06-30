# Feature Manager + Copilot GUI Objectives

Permanent acceptance criteria for the FM + Copilot GUI rectification pass. Verify on a live **host `AI-CAD`** build.

## A. Transparency (Feature Manager + Copilot + overlaps)

1. Left pane (`FeatureManager##LeftPane`), Explorer tab, Copilot pane, and File Explorer rows are **`NoBackground`** with **no full-panel `windowBg` or `glassBackground` wash**.
2. Viewport / dock background is visible through panel gaps; panels do **not** read as solid gray blocks.
3. **Frosted row pills** (`glassRowBackground` / `glassRowAtCursor`) behind every readable control row:
   - FM vertical tab rail buttons (hover/selected)
   - FM title header row
   - FM document toolbar strip (`drawFeatureManagerTopStrip`)
   - FM tree nodes (Origin, planes, folders, features, assembly)
   - FM bottom Save/Save As/Open row
   - Explorer file rows (`file_explorer.cpp`)
   - Copilot composer control rows (section B)
4. Overlap fixes: left tab rail separator, FM body scrollbar gutter, and docked panel edges must not show **double opaque stacks** or muddy bleed from the viewport compositor (`host/src/app.cpp` `##DockHost`).

## B. Copilot control layout

1. **Remove** `drawCopilotTopBar` from the chat flow; Design/Permissions/Thinking must **not** appear below the header.
2. **Composer prelude row** (directly above prompt):
   - Left: **Design** mode popup + **Permissions** popup
   - Right: **Context window meter** (`drawContextMeter`, model-aware via `defaultContextWindowForModel`)
3. **Composer toolbar row** (below prompt):
   - Left: **Model picker** (visible pill — `drawModelDropdown` popup, searchable)
   - Next: **Thinking effort** (provider-native levels from `reasoningEffortOptions`; disabled when unsupported)
   - Right: settings gear + send/stop
4. **One** in-chat model picker (composer). Settings modal picker (`drawProviderModelPicker`) remains for configuration — not a duplicate.
5. Status bar model summary (`status_bar.cpp`) stays **read-only** (not a picker).
6. **Sessions** button stays in header; sessions list behavior preserved.

## C. Feature Manager icons (~60–80 glyphs)

1. Audit icon usage across FM tree, left rail tabs, FM toolbar, and ribbon groups visible alongside FM (Features tab commands per `ribbon_def.cpp`).
2. Regenerate targeted glyphs via `host/assets/make_icons.py` — production path per `docs/ICONS_AI_PROMPTS.md`.
3. Rebuild `atlas_dark.png`, `atlas_light.png`, `atlas.json`; `gui_lint --rule icons` clean.
4. Deduplicate mappings: one canonical icon per concept (e.g. `ref_plane` for all datum planes; distinct `origin`; no `plane`/`ref_plane` drift).
5. Remove nonsensical FM duplicates (Origin→nested planes); fix **app shell** demo tree in `ui/imgui/src/app/app_state.cpp` to match host nesting.

## D. Shell parity

1. Mirror layout + styling in **`ui/imgui/`** (app symlink) — not stub controls.
2. App agent panel must wire real Settings / model registry patterns (or shared helpers extracted if trivial).

## E. Verification gate

1. `scripts/check.py` gui_lint icons + theme PASS
2. `bash scripts/gate.sh` PASS
3. Host `AI-CAD` binary builds and links
4. Manual visual checklist against screenshot areas (left FM, right Copilot, viewport bleed-through)
