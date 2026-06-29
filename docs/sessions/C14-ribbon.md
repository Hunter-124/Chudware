# C14 — Ribbon widgets + command table + icon loader

Model: **sonnet** · Wave 2 · Depends: C04, C05, C13 · Follow `docs/sessions/README.md`.
BUILD_DIR: `host/build2` (C15 may run in parallel using `host/build`).

## Mission
The SolidWorks ribbon: tab strip + grouped big/small icon buttons, data-driven from one command
table, rendered above the dockspace. Commands fire callbacks (stubs log to console until C17
wires PropertyManager flows).

## Read first
1. The user's target layout is SolidWorks 2025: tabs (Part: Features | Sketch | Evaluate |
   Simulation), groups of large icon-over-label buttons with dropdown splits, group separators.
   See `docs/PLAN.md` §2.1/§3.1 and `contracts/icons.md` for command names.
2. `host/src/app.cpp` + `host/src/ui/` (post-split structure), `host/src/ui_theme.h` (C13 roles)
3. `host/assets/icons/atlas.json` (C05 output format)

## Owned paths
`host/src/ui/widgets/ribbon.{h,cpp}` · `host/src/ui/icons.{h,cpp}` ·
`host/src/ui/ribbon_def.cpp` (+`.h`) · minimal integration hooks in `host/src/app.cpp`
(ribbon draw call + height reservation) · `host/CMakeLists.txt` (append your sources)

## Deliverables
1. `icons.cpp`: load both atlases + atlas.json at startup (stb_image is vendored);
   `ui::Icon(name, size)` → {ImTextureID, uv0, uv1}; theme switch swaps texture, UVs constant;
   missing name → magenta placeholder + one console warning.
2. `ribbon.cpp` widgets: `RibbonBegin/End`, tab strip (active tab persists), `RibbonGroup
   (label)` with bottom-aligned group caption, `RibbonBig(icon, label, enabled)` (icon 32 over
   label, SW-style hover frame), `RibbonSmall` rows, dropdown-split buttons (arrow zone opens
   an ImGui popup of sub-commands), disabled state, horizontal overflow scroll.
3. `ribbon_def.cpp`: ONE static table `{cmd_id, label, icon, tab, group, big, sub_of}` covering
   the v1 commands of all four Part tabs per `contracts/feature_params.md` and PLAN §6
   tiers (stretch commands present but disabled). Dispatch via a registered
   `std::function<void(const char* cmd_id)>` callback; default logs "[ribbon] <cmd>".
4. Integration: ribbon spans the window under the title bar; dockspace shifts down; View menu
   gets "Ribbon" visibility toggle; works in BOTH themes (roles only).
5. `AI_CAD_SHOT=ribbon` headless mode: main window with ribbon on the Features tab; capture
   dark + light (`AI_CAD_THEME=light`) into `scratch/ribbon_shots/`.

## Constraints
- Pure ImGui drawing — no new dependencies. Keep per-frame allocations out of the draw path.
- `ribbon_def.cpp` is a merge-by-append shared file going forward: keep the table sorted by
  tab/group so later cards append cleanly.

## Verification
Release build green (BUILD_DIR host/build2); both-theme ribbon screenshots; clicking commands
logs to console (verify via the shot mode's console or describe the wiring).
