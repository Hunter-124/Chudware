# Refactor Log

Running handoff log for the CAD modular refactor + GUI abstraction. Each node
appends a one-paragraph handoff (files touched, deferred, gotchas). See
`AGENTS.md` and the plan file for context.

## P0 — foundation

- P0 started N0a–N0d (clean start, AGENTS.md, promote contracts, Linux gate).

### N0a — clean starting point
Committed the pre-existing dirty `host/src/ui/*` + `ui_theme.h` diff (opaque
doc-tab band, opaque glass-pane base, relocated context meter) as a WIP commit
so the swarm starts from a clean tree. `git status` clean after commit.

### N0b — AGENTS.md + REFACTOR_LOG scaffold
Added top-level `AGENTS.md` (project blurb, repo map of current on-disk dirs
with plan-target annotations, swarm operating rules copied from the plan,
build/test cadence, pointers to plan + contracts + ARCHITECTURE.md +
CPP_MIGRATION.md) and this `docs/REFACTOR_LOG.md` scaffold.

**Pre-existing gate failure (recorded, not blocking):** `scripts/check.py` is
RED on this box for reasons unrelated to P0 — `feature_surface_audit` reports
~56 missing handlers for the `dwg_*` (Drafting) and `cam_*` (Manufacture)
ribbon commands in `host/src/ui/ribbon_def.cpp`, and `parity_audit` reports 12
SW_PARITY_SPEC done/beyond rows naming absent compilers (assembly, config,
csys, boolean, sheet-metal, weldments). N0a did not touch any audited file, so
this is pre-existing drift, not introduced by the refactor. Per the plan,
`scripts/gate.sh` (N0d) records this as a non-fatal `check.py` finding and
still exits 0 so the swarm can proceed; the underlying ribbon/parity gaps are
real engine work for later phases, not P0 foundation.

### N0c — promote contracts
`git mv docs/contracts contracts` (history preserved). Updated `docs/contracts`
path references → `contracts/` in `docs/**/*.md`, the moved `contracts/**/*.md`
internal cross-refs, and root config (`README.md`, `ARCHITECTURE.md`,
`requirements.txt`). Added `contracts/README.md` listing the frozen seams
(`worker_rpc.md`, `doc_ops.md`, `doc_schema.md`, `selectors.md`,
`mesh_format.md`, `stepplus.md`, `events.md`, `feature_params.md`) plus the
`ecad/`, `fixtures/`, `icons.md`, `licenses.md` subdirs, with the append-only /
integrator-sign-off policy.

**Deferred (per P0 hard constraint — no source edits under
`core/`/`host/`/`app/`/`engine/`/`agent/`/`mcp_server/`):** the `docs/contracts`
path references that remain in code comments / one string literal inside those
trees (e.g. `host/src/ui/viewport_panel.cpp`, `host/src/render/test_aicm_cpp/test_aicm.cpp`
default `"docs/contracts/fixtures/box.aicm"`, `core/CMakeLists.txt`,
`core/doc/*.h`, `engine/**/*.py`, `host/assets/make_icons.py`) were intentionally
left stale. They are non-functional comments or a test fallback default not run
by the gate, and those trees move/reorganize in later phases; the path fixups
land with the per-subsystem moves. `AGENTS.md`'s "promoted from `docs/contracts/`"
line was preserved as historical prose. `scripts/parity_audit.py` references
`docs/SW_PARITY_SPEC.md` (not `docs/contracts`), so no script change was needed.

### N0d — Linux gate + CMake presets
Added `scripts/gate.sh` (executable, `set -euo pipefail`): runs `scripts/check.py`
(recorded; pre-existing red is non-fatal via `CHECK_PY_FATAL=0` until the
ribbon/parity gaps close), then the core CMake/Ninja build with
`-DAICAD_WITH_OCCT=ON` + `ctest` (SKIP if system OCCT cmake config not found),
then engine+agent pytest (SKIP if no venv/pytest). Prints a PASS/SKIP summary
and exits 0 on environment SKIPs. Added `CMakePresets.json` (`linux-core`,
`linux-app`, Ninja) plus a minimal root `CMakeLists.txt` dispatcher so the
presets can wrap the existing `core/` and `app/` builds (the real layered
top-level CMake lands in N1a; the gate does NOT use presets — it drives
`cmake -S core` directly). Added `/build/` to `.gitignore` for preset build dirs.

**Gate result on this box:** `bash scripts/gate.sh` exits 0 — `check.py`
FAIL-RECORDED (pre-existing), core build PASS (system OCCT 7.9.1 at
`/usr/lib64/cmake/opencascade`), ctest PASS (41/41), pytest SKIP (no runtime
venv / pytest installed).

**GUI cleanup — Feature Manager + Copilot (both shells):** Reworked the Copilot pane in `host/src/ui/legacy_chat.cpp` with a **Sessions** toggle (replaces New chat/Chats), per-session state (`Conversation.lastContextTokens`, `queuedPrompts`), a top bar for agent mode / permissions (offline + supervised + thoughts) / model-specific thinking effort, a single canonical model picker in the composer (footer provider chip removed), and `defaultContextWindowForModel()` for the context meter. Added `glassRowBackground()` in `host/src/ui_theme.*`, increased panel transparency (glass alpha 0.38), nested default planes under **Origin** in `host/src/ui/feature_tree.cpp`, and unified datum-plane icon mapping to `ref_plane` (feature tree, ribbon, PM forms, `surf_planar`). Mirrored the glass + Sessions UX in `app/src/ui/agent_panel.cpp`, `app/src/theme/theme.*`, and `app/src/ui/left_dock.cpp`. Regenerated procedural icon atlas via `make_icons.py` (refined `origin` + `ref_plane` glyphs; atlas PNGs updated). **Deferred:** true multi-worker concurrent agent runs (UI shows per-session running state but one worker job), Windows visual verify, full AI PNG icon set (`pack_icons.py` still 4/189 sources). **Gotcha:** `scripts/check.py` may FAIL-RECORD pre-existing ribbon/parity audits.

**FM + Copilot GUI rectification (integrator wave):** Completed row-only transparency across host + `ui/imgui/`: removed full-panel `windowBg`+`glassBackground` washes from `layout.cpp` and Copilot (`legacy_chat.cpp`); added `glassRowAtCursor()` / `fmRowPill()` row pills on FM toolbar, tree chrome (Origin/planes/bodies/assembly), Explorer file rows, left-pane title + tab rail, and Copilot composer prelude/toolbar rows. Dock overlap fix: transparent `DockingEmptyBg` in `app.cpp`. Agent 2 delivered composer prelude (Design|Permissions + context meter) and toolbar (model|thinking|gear|send) in host + `agent_panel.cpp`. Agent 3 refreshed 68 FM/ribbon glyphs via `FM_REFRESH_MANIFEST` in `make_icons.py`, regenerated atlas PNGs/JSON, deduped `featureIcon` datum aliases, and nested Origin→planes in `app_state.cpp`. Added `docs/GUI_FM_COPILOT_OBJECTIVES.md` (acceptance criteria A–E). **Gate:** `bash scripts/gate.sh` PASS (check.py FAIL-RECORDED pre-existing). **Build:** `cmake -S host -B build/host && cmake --build build/host` PASS → `build/host/bin/AI-CAD`. **Deferred:** manual visual screenshot checklist, AI Settings pane full-panel wash (sibling tab; not in scope A).

**FM + Copilot follow-up (viewport HUD + readability):** Replaced the viewport heads-up toolbar's single `glassBackground` bar with per-button `glassRowBackground` pills, moved it to the top-right, shrunk icons (22dp), and added idle auto-fade (ramps on hover / zoom-area / open popups) in `host/src/ui/viewport_panel.cpp`. Boosted row readability: `glassRowBackground` default wash 0.28→0.42 with a faint border, brighter dark-theme `textDim`/`proseText`/`assistantName`, and assistant chat body copy now sits on a frosted row pill in `legacy_chat.cpp`. Mirrored wash/border in `app/src/theme/theme.*` + `ui/imgui/src/theme/theme.*`. **Build:** host `AI-CAD` PASS. **Check:** `scripts/check.py gui_lint theme` PASS. **Deferred:** manual screenshot verify of fade + chat contrast on bright viewport bleed.

**FM glass regression fix (undo/redo + rail + tree pills):** Root cause of broken undo/redo: `glassRowAtCursor()` before the FM toolbar is draw-only and never advanced the layout cursor, so the full-width `##fm_filter` InputText was placed at the same Y and stole hit-tests from the toolbar (undo/redo last). Fixed in `host/src/ui/feature_tree.cpp` by reserving the toolbar row with explicit screen-space button placement + `Dummy` advance; filter row gets the same reserved-height pattern. `fmRowPill()` now uses `GetFrameHeightWithSpacing()` + explicit `glassRowBackground()` wash (0.26 idle / 0.42 selected) so tree icon rows read consistently. Toolbar icon buttons always show a subtle pill (stronger on hover). Left tab rail (`layout.cpp`) and `ui/imgui` activity rail now draw persistent frosted pills on every icon (0.20 idle, 0.32 hover, 0.40 selected). **Build:** `cmake --build build/host` PASS. **Check:** `gui_lint --rule theme` PASS.

### F3 — platform layer
Consolidated OS shims into `platform/`: `dialogs.h` + `dialogs_win32.cpp` / `dialogs_linux.cpp` (ported from `host/src/ui/file_dialog.*`, incl. `AI_CAD_NO_FILE_DIALOGS` stub + kdialog/zenity POSIX backend); `window.h` + `window_win32.cpp` / `window_linux.cpp` (borderless chrome from `host/src/win_chrome.*`, GLFW X11 init hint on Linux); `paths.h` + per-OS `paths_*.cpp` (`exeDir`, `userDataDir`, `appDataRoot`, `configPath`, `imguiIniPath`, `documentsDir`, `resourcesDir`); `dpi.h`/`dpi.cpp` (`contentScale` + `AI_CAD_SCALE`); `resources.h`/`resources.cpp` (`setWindowIcon` via stb). Replaced inline Win32 in `ui/imgui/src/platform/file_dialog.*` with thin CAD-filter wrappers delegating to `platform/dialogs.h` — zero `#ifdef _WIN32` outside `platform/` and `ui/imgui/src/platform/`. **Deferred:** CMake static lib + include paths (G1); `main.cpp`/`application.cpp` still duplicate `exeDir`/GLFW init/DPI until G1 wires `platform::*`. **Gotcha:** `ui/imgui` will not link until G1 adds `platform/*.cpp` to the target and `${REPO}` to `target_include_directories`.

### G0 — GUI unification integrator prep
Local baseline commit before Wave 1: added `ui/imgui/build/` to `.gitignore`; committed untracked `ui/viewport/**` scaffold (`renderer.h`, `mesh.*`, `camera_anim.*`, `render/{aicm_loader,id_pass,overlays}.*`, `vec_math.h`) plus MSVC 14.51 `/FI extern/fix_math.h` fix in `host/CMakeLists.txt` and incidental tools/docs path edits. Wrote monolithic swarm spec `docs/GUI_MIGRATION_DAG.md` (all nodes G0→X1 with write-sets, sources, verify commands) and `docs/GLASS_DESIGN_SPEC.md` (4 material levels, 8pt grid, `glassPanel`/`adaptiveSurface`, per-panel rules, P1 acceptance criteria). Captured 21/21 host `AI_CAD_SHOT` baselines into `scratch/gui_baseline/*.png` via `host/build-linux/bin/AI-CAD` (`flow` needs ~240s timeout for CFD frames; all others <120s). **Deferred:** Wave 1 leaves (F1–F4), `ui/viewport/renderer.cpp` + CMakeLists, Kawase blur. **Gotcha:** `ui/viewport/render/id_pass.*` and `overlays.*` still `#include` host `renderer.h` — F1 must break this first; `ui/imgui` still leeches `host/build-linux/_deps`.

### F1 — viewport library (`ui/viewport/**`)
Ported `host/src/renderer.cpp` (1618 LOC) to `ui/viewport/renderer.cpp`; added framework-neutral `worldToScreen` / `screenRay` (+ `ViewportScreen` / `ScreenPt`) on `renderer.h`. Fixed `render/id_pass.h` and `render/overlays.h` to include local `../renderer.h` (zero `host/src` includes under `ui/viewport/`). Added standalone `CMakeLists.txt` static lib `aicad_ui_viewport` (glad from `host/extern/glad`, OpenGL). Implemented `GlViewport` (`viewport_impl.*`) as `aicad::ui::IViewport` adapter over `Renderer` + `render::IdPass`. NEW: Kawase-blur backdrop FBO chain (¼-res downsample + 4 passes) on the resolve target, exported via `Renderer::backdrop()` / `GlViewport::backdrop()`; `averageLuminance(ViewportRect)` samples the resolve FBO (Rec.709, capped readback). **Verify:** `CCACHE_DISABLE=1 cmake -S ui/viewport -B build/viewport && cmake --build build/viewport` PASS → `libaicad_ui_viewport.a`. **Deferred:** G1 CMake rewire (`ui/imgui` link + glad FetchContent), `IViewport::backdrop()` on abstract seam (F2 consumes via `GlViewport`), runtime GL smoke. **Gotcha:** sandbox `ccache` may block builds — use `CCACHE_DISABLE=1`; glad path still references `host/extern/glad` until G1 vendors it under `ui/viewport/`.

### F3 — platform layer
Consolidated OS-specific GUI shims into `platform/` + thin AI-CAD2 wrappers. Added `platform/dialogs.h` + `dialogs_win32.cpp` / `dialogs_linux.cpp` (ported from `host/src/ui/file_dialog.*`, including `AI_CAD_NO_FILE_DIALOGS` headless stub), `platform/window.h` + `window_win32.cpp` / `window_linux.cpp` (from `host/src/win_chrome.*` + Linux X11 GLFW hints from `host/src/app.cpp`), `platform/paths.h` + `paths_win32.cpp` / `paths_linux.cpp` (`exeDir`, `appdataDir`/`appdataRoot`, `configPath`, `cacheDir`, `tempDir`, `imguiIniPath`, `documentsDir`, `resourcesDir`), `platform/resources.h` + `resources.cpp` (`setWindowIcon` from `host/src/core/app_state.cpp`), `platform/dpi.h` + `dpi.cpp` (GLFW content scale + `AI_CAD_SCALE`), and `platform/CMakeLists.txt` (`aicad_platform` static lib). Rewrote `ui/imgui/src/platform/file_dialog.*` as CAD filter wrappers with zero OS `#ifdef`. **Deferred (G1):** wire `add_subdirectory(platform)` + `target_link_libraries(AI-CAD2 aicad_platform glfw)` in `ui/imgui/CMakeLists.txt`; add `-I${REPO}/platform` include path; migrate `main.cpp`/`application.cpp` off local `exeDir`/`#ifdef _WIN32` to `paths.h`/`window.h`/`dpi.h`; host panels still call `host/src/ui/file_dialog.h` until shell migration (S6+). **Gotcha:** Linux file dialogs still require `kdialog` or `zenity` on PATH; macOS `exeDir` uses `_NSGetExecutablePath`.

### F4 — worker / state seam
Ported `host/src/core/{doc_mirror,async_rpc,selection,jobs,sim_client}.*` + `host/src/bootstrap.*` into `services/worker/` with GL-free `pick_hit.h`, shared `worker_types.h` (JobState/AgentEvent/parseParts), `doc_session.h`, `doc_tabs.{h,cpp}`, and `worker_command_dispatcher.{h,cpp}` (`ICommandDispatcher` → WorkerClient+DocMirror). Fixed includes (`worker_client.h`, `cpp_core.h`, no `host/render/id_pass.h`). Replaced `ui/imgui` direct `cpp_core` shortcut with `app/doc_engine.{h,cpp}` — `eng::Engine` facade over WorkerClient (libaicad routing + Python-oracle fallback). `app_state.*` now owns WorkerClient, DocMirror, AsyncRpc, SimClient, JobState, Selection, core::DocTab sessions; wires `DocViewModel`/`ActionBus`/`WorkerCommandDispatcher`; `application.cpp` drains `rpc` each frame and shuts down worker on exit. **Deferred (G1):** CMake — add all `services/worker/*.cpp` + `app/doc_engine.cpp` to `AI-CAD2`, `-I${REPO}/services/worker`, drop duplicate `engine/engine.cpp` cpp_core path or keep as dead code until panel migration; `AI_CAD_SHOT=tree|m1` smoke. **Gotcha:** `ui/imgui/src/engine/engine.cpp` still exists and is still in the CMake glob — G1 should exclude it or delete to avoid duplicate `eng::Engine` symbols; panels still call `s.engine.*` via the new facade header included from `app_state.h`.

### F2 — theme / liquid glass
Ported host `ui_theme.*` glass system into `ui/imgui/src/theme/{tokens.h,theme.h,theme.cpp}` per `docs/GLASS_DESIGN_SPEC.md`. **tokens.h:** `MaterialLevel` (4 materials with tintAlpha + blurPasses), 8pt `Space` grid (`Metrics.grid=8`), SF-like `TypeScale`, `rowWash` constants (0.26/0.32/0.42), glass roles (`glassBg/Border/Highlight/Shadow`), vibrancy light/dark text tokens, `accentSoft`/`accentMid`, `roleU32` helpers. **theme.h/cpp:** 5-layer `glassBackground()` (+ alpha variant), `pushGlass{Tooltip,Window,Child}Style` pops, host-faithful `glassRowBackground`/`glassRowAtCursor`, `adaptiveSurface(luminance)` (thresholds 0.35/0.65), `glassPanel(level,rect)` (Kawase via `setViewportBackdrop` when valid, else layered fallback), `ViewportBackdrop` stub. `apply()` maps `PopupBg→glassBg`, `DockingEmptyBg` transparent, font sizes from type scale. **Verify:** `python3 scripts/gui_lint.py --rule theme ui/imgui/src` clean; `theme.cpp.o` builds. **Deferred:** F1 Kawase blur + `averageLuminance(rect)` wiring, ~40 host palette roles (sketch/console/diff/viewport-edge), panel consumption of `glassPanel`/`adaptiveSurface` (S*/C*/P1), `loadFonts` CADSymbols merge. **Gotcha:** `U()` is now 8px units (was 4px); default row wash 0.26 idle — explicit wash callers (rail 0.20/0.32/0.40) unchanged.

### S3 — ribbon + context toolbar
Ported `host/src/ui/widgets/ribbon.cpp` + `ribbon_def.cpp` into registry-driven `ui/imgui/src/ui/{ribbon,context_toolbar}.*` (~520 LOC). **No `ribbon_def`** — all verbs/flyouts come from `ui/abstract/command_registry.*` (Top + Flyout kinds; Variants palette-only). `workspace_bar.cpp` owns the old ribbon tab strip (workspaces); `ribbon::drawBody` renders SolidWorks-style grouped 52dp big buttons with split-arrow flyouts, last-used variant memory, sketch_exit↔sketch_new swap, group captions, priority overflow chevron, and `mat.thin` `glassPanel` background per GLASS_DESIGN_SPEC §6. `ribbon::applyShotEnv` maps host `AI_CAD_TAB` 0..12 → workspace/sketch/drawing doc-type for screenshot parity. **Coverage vs host `ribbon_def`:** 181/225 cmd_ids present in registry (80%); 44 host-only gaps — Manufacture/CAM tab (26 cmds), Appearance material presets + display styles (12), polygon flyout variants (6); registry adds 9 ids host ribbon lacked (`prim_*`, consolidated `appearance_edit`/`view_style`). **Deferred (G2/C6):** CMake source list for `ribbon.cpp`; `AI_CAD_SHOT=ribbon` scenario harness; small-button column layout + material swatches + rich tooltips; registry append for CAM/appearance cmds.

### S6 — files / settings / profiler
Ported `host/src/ui/file_explorer.*`, `settings_modal.*`, and `profiler_window.*` into `ui/imgui/src/ui/` with `theme::` glass row pills (Explorer), `mat.thick` settings sheet, and profiler flame graph. Explorer uses `platform::documentsDir`/`appdataRoot` for workspace default (no OS `#ifdef`); settings persist via `platform::configPath()` into module `AppSettings`/`settingsUi()` until G2 lifts flags onto `AppState`. File open dispatches through `takeExplorerOpenRequest()`; `left_dock.cpp` calls `drawFileExplorerBody` when `railTab == kFilesRailTab`. Profiler drives `core::cpp_core()` `prof_*` RPCs. AI settings tab uses a simplified provider picker (full `drawProviderModelPicker` + `model_registry.h` land with C3). **Deferred (G2):** wire `drawSettingsModal`/`drawProfilerWindow` in `application.cpp`, CMake source list, `AppState` fields (`openSettings`, `showAbout`, …), Ctrl+C/V explorer routing, viewport file-drop target (`kFileDragPayload`). **Gotcha:** `settings_modal.cpp` owns config.json read/write — merge with F4 if worker settings move to `services/worker/`.

### G1 — integrator: CMake rewire (Wave 1)
Integrated F1–F4 into AI-CAD2: root `CMakeLists.txt` now builds `glad` + `ui/viewport` + `ui/imgui` under `linux-app` preset; `ui/imgui/CMakeLists.txt` owns FetchContent for glfw/imgui/json/stb (no `host/build/_deps`), links `aicad_platform`, `aicad_ui_viewport`, and all `services/worker/*.cpp`; excludes duplicate `engine/engine{,_pick,_sketch}.cpp` (F4 `doc_engine.cpp` is the sole `eng::Engine`). `platform/CMakeLists.txt` wires glfw + stb includes + `${REPO}` for `#include <platform/...>`; fixed `window.h`/`dpi.h` API drift vs implementations. `main.cpp` migrated to `platform::{initGlfwPlatform,applyBorderlessWindowHints,contentScale,resourcesDir,installWindowChrome,tickWindowChrome}`; `application.cpp` owns `GlViewport` and feeds F2 `theme::setViewportBackdrop()` from Kawase backdrop each frame (legacy `viewport::Scene` still drives the ImGui texture until C4). `scripts/gate.sh` stage 5 builds `linux-app` + `AI-CAD2 --shot` (xvfb-run when headless) + `--selftest` when libaicad built. Bounce-fix: `PlanInfo`/`parsePlan` in `worker_types.*` for `jobs.cpp`; `theme.h` `<cstdint>` + `ImTextureID` cast; stb impl consolidated in `stb_impl.cpp`. **Verify:** `cmake --preset linux-app && cmake --build build/linux-app` PASS → `build/linux-app/bin/AI-CAD2`; `AI_CAD_NO_FILE_DIALOGS=1 AI-CAD2 --shot /tmp/aicad2-g1.png --frames 12` PASS; `--selftest` PASS (libaicad); `bash scripts/gate.sh` PASS (check.py FAIL-RECORDED pre-existing, pytest SKIP). **Deferred (Wave 2):** replace `viewport::Scene` with `GlViewport` in `viewport.cpp`; shell panels (S1–S6); glad still vendored from `host/extern/glad`; `AI_CAD_SHOT=*` scenario harness (C6); Windows preset (X1). **Gotcha:** headless shot needs `xvfb-run` or a real DISPLAY; standalone `cmake -S ui/imgui` works but reuses its own FetchContent cache.

### S5 — icons atlas + cursors
Ported host icon loader (`host/src/ui/icons.cpp`) into `ui/imgui/src/theme/icons.{h,cpp}`: `ui::loadIcons`/`getIcon`/`IconUV` with dual-theme atlases, `@32`/`@20` cell keys, size fallback, and `theme::ctx().dark` texture selection; retained `icons::` shell wrappers (`initAtlas`, `draw`, `inlineIcon`) with alias map + hand-drawn chrome glyphs + monogram fallback. Regenerated atlas via `host/assets/make_icons.py` (189 names × 2 sizes = 378 cells, 56×6426 px) and copied `atlas_dark.png`, `atlas_light.png`, `atlas.json` to `ui/imgui/resources/icons/` (parity with host manifest). Ported `host/src/ui/cursors.{h,cpp}` → `ui/imgui/src/ui/cursors.*` with module-local `requestCursor`/`applyCursorRequest` (host used `AppState` fields). **Deferred:** wire `initCursors`/`applyCursorRequest` in `application.cpp`; add `cursors.cpp` to CMake glob; `AppState::requestCursor` shim optional. **Gotcha:** `gui_lint icons` still reads `host/assets/icons/atlas.json`; runtime loads from `platform::resourcesDir()/icons/`.

### S2 — title bar + doc tabs
Ported `host/src/ui/title_bar.*` and `host/src/ui/doc_tabs.*` into `ui/imgui/src/ui/{title_bar,doc_tabs}.{h,cpp}`. **title_bar:** SolidWorks-style File/Edit/View/Insert/Help menus inside `BeginMenuBar` child (no doc tabs — those moved to viewport band per host latest); caption min/max/close with palette tokens; narrow-window Menu rollup; Search opens command palette. **doc_tabs:** Browser-style strip with deferred switch/close/save; `glassPanel(Thin)` full band over viewport, per-tab `Regular`/`UltraThin`/`Thin` pills with accent underline + group borders; "+" New chooser popup; unsaved-changes modal; `createNewDoc`/`saveDocTab`/`openNewDocChooser` bound to F4 `core::DocTab` + `AppState::{switchToTab,addDocTab,doSave}`. Updated `panels.h` exports. **Deferred (G2/C4):** wire `drawDocTabs` at top of `viewport.cpp` (hide dock tab bar via `ImGuiDockNodeFlags_NoTabBar`); `ui/imgui/CMakeLists.txt` add `doc_tabs.cpp`; Ctrl+N → `openNewDocChooser()` in `application.cpp`; `averageLuminance(rect)` per-tab adaptive tint when GlViewport replaces Scene; full host menu parity (Export/Recent/Save All, Insert/Tools/Window stubs). **Gotcha:** doc tabs removed from title bar — integrator must hook viewport before `AI_CAD_SHOT=multidoc` verify.

### S4 — status / toast / shortcut / palette
Ported `host/src/ui/{status_bar,toast,shortcut_bar,command_search}.*` into `ui/imgui/src/ui/` with Glass styling per `docs/GLASS_DESIGN_SPEC.md`. **status_bar.cpp:** solid `bg0` bar (not glass); pulsing status dot (good/warn/bad inferred from status text); agent live-phase + sim progress chips on `glassBackground` pills; SW-style right cluster (view style, MMGS/IPS toggle, editing mode, provider/model stub); sim chip click switches to `Simulate` workspace; `AI_CAD_SIMPROG` smoke hook. **toast.{h,cpp}:** module-local queue + `postToast()`/`drawToasts()`; `glassPanel(Regular)` cards with fade-out alpha + accent bar; click copies to clipboard. **shortcut_bar.{h,cpp}:** 'S' key floating toolbar (model vs sketch command tables); `glassPanel(Regular)` pill + per-button hover row pills; dispatches via `AppState::dispatch`. **command_search.{h,cpp}:** centered glass search popup with `icontains` filter over `cmd::palette()` + `kSettingsEntries`; `requestOpenSettings`/`takeOpenSettingsRequest` seam for S6. **command_palette.cpp:** extended Ctrl+K palette with host search patterns (label/id/workspace/group/keywords scoring + settings section), glass window + row pills, unified command+settings results. **Deferred (G2):** wire `drawToasts`, `drawCommandSearch`, `ShortcutBar` in `application.cpp`; move toast queue onto `AppState` for worker errorSink; `panels.h` exports; settings modal consumes `takeOpenSettingsRequest`; full provider/model from settings; sketch DOF status line when H5 lands. **Gotcha:** no CMake/build this node — new `.cpp` files need G2 glob; title-bar Search still opens palette not command_search until integrator switches it.

### S1 — layout / dock / rail
Ported `host/src/ui/layout.cpp` `buildDefaultLayout()` into `ui/imgui/src/ui/{layout.h,layout.cpp}` with pixel-clamped left/right fractions (`theme::S()` panel scale), `NoUndocking` on the left node, AI-CAD2 window names (`Browser###left`, `Viewport###viewport`, `Assistant###agent`), ECAD/CAM central pre-registration, and host task-pane dialog names in the lower-right slot. Added `ui::drawDockSplitters()` — recursive dock-node walk drawing 1px `palette.line` at 0.35 alpha (fixes invisible splitters, no accent/yellow ImGui separator bleed). **application.cpp:** replaced inline `buildDockLayout` with `buildDefaultLayout` + per-frame `drawDockSplitters`; transparent `DockingEmptyBg`/`WindowBg` plus zero-alpha `Separator*` during dock host; dock size passed as content width × `midH`. **Deferred (G2):** `AppState::resetLayout` + title-bar “Reset layout” → rebuild; port `drawLeftPane()` (tab strip + PM auto-switch) when C1/S6 panels land — activity rail glass pills remain in `left_dock.cpp` (0.20/0.32/0.40); merge external `##rail` with in-pane SW tab strip or keep dual chrome per integrator choice; `drawConfigurationsBody` stub. **Gotcha:** `layout.cpp` picked up by CMake GLOB — no list edit needed; `application.h` dropped dead `buildDockLayout` decl (minimal bounce); host `FeatureManager##LeftPane` name not used — G2 may alias Browser window if screenshot parity requires it.

### H5 — sketch mode (wave 2 milestone; ~96% logic port)
Split `host/src/ui/sketch_mode.cpp` (8216 LOC) + `sketch_mode.h` + `airfoil.*` into `ui/imgui/src/ui/sketch/{sketch_mode.h,sketch_mode.cpp,sketch_tools.cpp,sketch_constraints.cpp,sketch_overlay.cpp,sketch_edit.cpp,sketch_internal.*,sketch_seam.*,sketch_palette.h,airfoil.*}` (~7913 LOC implementation + seam). **Camera:** removed host-local `worldToScreen`/`screenRay`/`invert4x4`; all projection goes through `ui/viewport/renderer.h` via `sketch_internal.h` ImVec2 wrappers (`::worldToScreen` / `::screenRay` on `Renderer::CameraXform` + `ViewportScreen`). **F4 seam:** `ui::sketch::SketchHost` bridges `app::AppState` + `WorkerClient`/`DocMirror` + `Renderer*` (toast→status, `loadDocMesh`→`scene::loadAicm`, clipboard/camera/pick fields local until AppState grows them); `SketchPmSeam` stub until C2 PropertyManager. **Core tools:** enter/exit/solve/drag, ribbon dispatch, line/rect/circle/arc/slot/smart-dim overlay path, constraints/dims, ghosts/region pickers — all present in split TUs. **Deferred (G2/G4):** wire `SketchMode::drawOverlay` in `application.cpp` + pass F1 `Renderer*` from viewport; full `pmParseDim` expr parser; `camera_anim` restore; id-buffer hover for convert/smart-dim; `selectionItems()` from `core::Selection`; IPS units; CMake integrator only; compile bounce-fixes expected. **Gotcha:** `port_h5.py` retained for re-sync from host; no build this node per DAG.

### C6 — scenarios + screenshot harness
Ported `host/src/ui/scenarios.{h,cpp}` + `AI_CAD_SHOT` wiring into AI-CAD2. **New:** `ui/imgui/src/ui/scenarios.{h,cpp}` — `ShotConfig` (all 21 scenario flags + frame budgets), `configureShot()` (reads `AI_CAD_SHOT`, seeds static UI for settings/chat/plan/assembly/pick/ribbon/explorer/pmform, calls `ribbon::applyShotEnv` for tab selection), `ScenarioRunner` step machines for m1/sketch/asm/fea/flow/cam/drawing/multidoc/activity/evalmass + worker one-shots for tree/equations/ecad. **main.cpp:** after init, `configureShot` + `setScenarioContext`; shot loop calls `endFrame` (per-step PNG mirrors + scenario Break/Continue) then generic capture at `ShotConfig::captureFrameBudget()` (tree/equations/ecad/cam timing; **flow cap 14400 frames ≈ 240 s @ 60 Hz**). **application.cpp:** `scenarioTick()` before UI each frame. **Deferred (G3/P2):** full host parity for asm mates/explode/interference, sketch rect+drag+dim chain, SimManager-colormap overlay, ecad mirror, PM `AI_CAD_PMSET`, property-manager edit forms on m1/multidoc; integrator screenshot battery vs `scratch/gui_baseline/*.png`. **Gotcha:** no build this node; complex scenarios use DocMirror/worker/simClient directly until C1–C5/H1–H4 panels land; `AI_CAD_REPO` required for worker-driven shots.

### C1 — feature tree
Ported `host/src/ui/feature_tree.*` (~3k LOC) into `ui/imgui/src/ui/feature_tree.{h,cpp}` with Glass `fmRowPill()` row pills (`glassRowBackground` + `GetFrameHeightWithSpacing`), FM toolbar explicit row reservation (undo/redo hit-test fix), Origin→nested Front/Top/Right planes per host latest, deferred `applyTreeOps` queue, feature history from F4 `DocMirror`/`WorkerClient`, status badges, rollback bar, sketch nesting, copy/cut/paste, and bottom save/open strip. **FmState adapter** (`detail::FmState`) bridges lean `app::AppState` until G3 lifts `selectedFeatureId`, plane visibility, body slots, highlight faces, and clipboard onto AppState; routes doc ops through `docMirror.applyOps` + `scene::loadAicm`. Inlined `appearance_common` picker/material tables. **left_dock.cpp:** rail tab 0 calls `drawFeatureTreeBody` (demo `drawNode` removed). Sketch enter / PM plane-path picks / `consumePlanePick` deferred until H5/C2 wire `SketchHost` + PropertyManager. **Deferred (G2/G3):** `ui/imgui/CMakeLists.txt` already GLOBs new TU; lift FmState fields to `AppState`; `AI_CAD_SHOT=tree` verify; sketch+PM tree interactions; `pollAiCadHandoff` full tab lifecycle; body visibility sync from mesh load. **Gotcha:** no build this node — integrator compile bounce expected on `fmPalette()`/`FmState` seams and any remaining host-only `AppState` helpers.

### G2 — integrator: wire Wave 2 shell chrome + sketch scaffold
Integrated S1–S6 + H5 into AI-CAD2 build and frame loop. **CMake:** `GLOB_RECURSE` already picks up all new `ui/imgui/src/ui/*` TUs (layout, doc_tabs, ribbon, toast, shortcut_bar, command_search, cursors, file_explorer, settings_modal, profiler_window, `ui/sketch/*.cpp` except `port_h5.py`). **application.cpp:** S1 `buildDefaultLayout` + `drawDockSplitters` verified; S2 Ctrl+N → `openNewDocChooser`; S4 end-of-frame `drawToasts`/`drawCommandSearch` + palette; S5 `initCursors`/`applyCursorRequest`; S6 `drawSettingsModal`/`drawProfilerWindow` + `takeOpenSettingsRequest`/`takeExplorerOpenRequest` dispatch; S4 settings search seam honored. **viewport.cpp:** doc tab strip at top of central node (`NoTabBar`), `ShortcutBar`, H5 `SketchMode::drawGhosts`/`drawOverlay` via `setViewportPipeline(Renderer*, IdPass*)`. **panels.h:** exports for new draw entry points. **Bounce-fixes (integrator + seam):** `command_search.h` deduped `SettingsEntry` vs `settings_modal.h`; `profiler_window.cpp` `pushText` rgba fix; H5 seam expanded (`SketchHost::rpc()`, `docUndo`/`docRedo`, `hiddenFeatures`, `IdPass*`, `SketchPmSeam` PM stubs); `sketch_internal` shared helpers (`parseEntities`, geometry, `fmtLen`/`fmtAng`/`arrowHead`, `toWorkerPick`); missing `entIndexById`/`hitTestEntity`/`addProjectedDimRef` added to `sketch_mode.cpp`; `smartDimPicking` syntax fix. **Verify:** `cmake --preset linux-app && cmake --build build/linux-app` PASS; `bash scripts/gate.sh` PASS (check.py FAIL-RECORDED pre-existing, pytest SKIP); `AI-CAD2 --shot /tmp/aicad2-g2.png --frames 12` + `--selftest` PASS. **Deferred (Wave 3):** `workspace_bar`→`ribbon::drawBody`; PropertyManager/C1 panels; replace `viewport::Scene` with full `GlViewport` render path; `AppState` lift of settings/toast/cursor flags; sketch `selectionItems()` + camera_anim; Ctrl+C/V explorer routing; `AI_CAD_SHOT=*` scenario harness (C6). **Gotcha:** sketch overlay compiles with PM/route stubs (no-op until C2); unknown icon `grp_pattern` in viewport toolbar (registry gap); headless shot needs DISPLAY or xvfb-run.

### C3 — copilot + activity + console
Ported `host/src/ui/{legacy_chat,activity_panel,console_panel,model_registry.h}` into `ui/imgui/src/ui/{agent_panel,activity_panel,console_panel,model_registry.h}`. **agent_panel:** expanded from demo stub to host-faithful Copilot — slim header (logo + Sessions toggle + settings gear + status dot), Chat/Console/Activity tab bar, per-doc session list with running indicators + delete, glass user bubbles (`pushGlassChildStyle` + accent border) and assistant body on `glassRowAtCursor`, action cards on frosted child pills, composer prelude row (Design|Edit|Ask + Permissions offline/supervised/thoughts + context meter), toolbar row (model dropdown from `model_registry` + reasoning-effort + send/stop icons) on `glassRowBackground` pills, message queue while busy, F4 seam via `tryStartWorker`/`job.running`/`startDemoAgent`/`engineReady`, routes `run_started` to `activityPanel()`. **activity_panel:** full run→stage→attempt tree from orchestrator events + `%APPDATA%/AI-CAD/cache/runs/*/events.jsonl` replay combo (`platform::appdataDir`), verdict chips, prompt-editor jump to Settings Agents tab. **console_panel:** module-local log with `[tag]` color coding via theme tokens; `appendConsoleLog`/`drawConsoleBody`. **model_registry.h:** header-only catalog (providers, builtin/cloud/local presets, reasoning-effort + context-window helpers). **Verify:** `python3 scripts/check.py gui_lint theme` PASS (zero literal colors in new panel files). **Deferred (G3/C6):** full `ChatMsg`/`Conversation` persistence to `chat_history.json`, `refreshModelAvailability` worker probe, `submitTurn`→`agent_run_v2`/`agent_mission_v1` (still `startDemoAgent` + engine facade), `drawProviderModelPicker` shared with settings, `pollChatOpenFoam`, activity `onEvent` drain from `JobState::events`, `AI_CAD_SHOT=chat|activity` scenario wiring. **Gotcha:** no build this node; CMake GLOB picks up new TUs; integrator should wire `activityPanel().onEvent` in job drain when agent RPC lands.

### C4 — viewport panel + HUD + tools
Ported `host/src/ui/{viewport_panel HUD/tools,view_selector,section_tool,legend,mouse_gestures,camera_anim}` into split `ui/imgui/src/ui/{viewport,view_selector,section_tool,legend,mouse_gestures,camera_anim}.*`. **Render path:** `viewport::Scene` replaced by `GlViewport` as primary — `setViewportPipeline(GlViewport*)`, per-frame `renderViewport()` loads AICM via `render::loadAicmFile` + `GlViewport::setDoc/render`, displays `colorTextureId()`, picking via `GlViewport::pick()`. Module-local `ViewportState` holds yaw/pitch/zoom/pan, animation tween, display style, legend, HUD flags until AppState lift. **HUD (GLASS_DESIGN_SPEC §6):** top-right per-button `glassPanel(UltraThin)` pills with `averageLuminance` adaptive tint + idle fade (0.20→1.0 on hover/popup); groups zoom-fit/area/pan/rotate, view-orientation ▾, display-style ▾, hide/show ▾, section toggle. **Tools:** `camera_anim` smoothstep tweens; `mouse_gestures` 8-segment RMB wheel; `ViewSelector` glass-box (Space) with hover-preview thumbnail; `SectionTool` multi-plane clip + PM panel + `AI_CAD_SECTION` env; `drawLegend` jet ramp. **application.cpp:** `setViewportPipeline(glViewport_)`, `updateViewportBackdrop` → `ui::refreshViewportBackdrop`. **Deferred (G3/C6):** full host viewport_panel parity (push-pull, dim-drag, asm drag, pick readout, normal-to/zoom-to-selection, appearance picker, lens-shift full-bleed, overlay/id-pass selection highlights, `AppState` camera/legend/renderer fields); CMake integrator only; `AI_CAD_SHOT=pick` scenario. **Gotcha:** no build this node; legacy `viewport::Scene` still owned by Application but unused for display; sketch `requestCameraAnimation` in seam still snaps (wire to `ui::requestCameraAnimation` in G3).

### C5 — asm / config / tables / equations / evaluate
Ported `host/src/ui/{asm_panels,config_panel,design_table_panel,equations_dialog,cutlist_panel,appearance_common,evaluate_dialogs}.*` (~3.5k LOC) into `ui/imgui/src/ui/` with `theme::` glass (`pushGlassWindowStyle` on floating asm/evaluate windows) and `ui::c5::*` seam in `appearance_common.{h,cpp}` bridging lean `app::AppState` (toast→`postToast`, `loadDocMesh`→`scene::loadAicm`, `ips`/`activeTabIsDrawing`/`busy`, simplified `pmParseDim`/`pmFormatDim`, module `showEquationsDialog`/`showDesignTablePanel`/`showCutListPanel` flags, `EvaluateDialogs` legend/colormap via `setEvaluateRenderer`). **asm_panels:** full mate/pattern/explode/interference/BOM/parts-library flow through `DocMirror` + async `rpc`. **config_panel:** read-only ConfigurationManager embedded body. **design_table_panel:** editable config grid (override columns from cache; add-column candidates stub until C2 `pmFindForm`). **equations_dialog:** global-variable editor (`eq_set`/`eq_delete`). **cutlist_panel:** weldment cut list. **evaluate_dialogs:** mass/measure/section/check/interference/symmetry + field overlays (draft/thickness/curvature) + zebra. **Deferred (G3):** wire `AsmPanels`/`EvaluateDialogs`/`ConfigPanel` instances in `application.cpp` + ribbon dispatch; lift visibility flags onto `AppState`; `setEvaluateRenderer` from viewport pipeline; full `pmFindForm` for design-table add-column; `AI_CAD_SHOT=equations|evalmass|asm` verify. **Gotcha:** no build this node; evaluate_dialogs still has a few host literal status colors (P1 sweep); integrator compile bounce expected on `c5::` seam + `PMFieldKind` bridge.

### C2 — property manager + 44 forms
Expanded `ui/abstract/panel_spec.{h,cpp}` from 4 stub forms to all **44** feature tables ported from `host/src/ui/pm_forms.cpp` (`PanelField`/`PanelSpec`/`SelectorKinds`, `panelFormForCommand` ribbon routing). Ported `host/src/ui/property_manager.cpp` logic into `ui/imgui/src/ui/property_manager.{h,cpp}` — data-driven `drawBody` (grouped glass cards, dim parse/format, selector wells, live debounced preview via `docMirror.applyOps`, checkpoint/revert cancel, `presetFieldForShot` for `AI_CAD_PMSET`). **left_dock.cpp:** replaced hardcoded inspector with `propertyManager().openForCommand` + `drawBody` inside `mat.regular` glass card; appearance editor unchanged. **sketch_seam.{h,cpp}:** `SketchPmSeam` delegates PM hooks (`wantsRegionPicker`, `routeDatumPlane/Axis`, `setProfilePicks`, …) to `ui::propertyManager()`. **Shot env:** `pmApplyShotEnv` reads `AI_CAD_PMCMD`/`AI_CAD_PMSET` on first inspector draw (pmform scenario). **Deferred (G3):** integrator CMake already GLOBs new TU; viewport pick routing into `routePick`, callout overlays, Select-with-AI, left-pane PM tab auto-switch (S1 layout), `prim_*` commands without panel_spec entries; compile bounce on theme token renames (`txt0`/`bg3` vs host palette). **Gotcha:** no build this node; AI assist stripped; sketch exit on PM open clears `inSketch` only (no `SketchMode::exitSketch` yet).


## Liquid-glass v2 restyle (AI-CAD2) — 2026-07-01

Parallel 7-node wave + integrator, per the frozen contract in the session's glass_design doc.
**N1 (ui/viewport/)** — async luminance pipeline: `averageLuminance()` is now a pure-CPU read of a
≤64×40 Rec.709 grid filled by a 3-slot PBO ring + `glFenceSync` polling off the Kawase chain
(killed the per-pill synchronous `glReadPixels`-per-row stalls); `GlassTimings` GPU timers
(GL_TIME_ELAPSED, double-buffered) around scene/blur/luma; `setBackdropEnabled()`.
**N2 (theme/)** — theme-independent adaptive contrast: vibrancy tokens unified across palettes,
`adaptivePanel()` (hysteresis 0.42/0.58 + smoothing + per-frame idempotence, `panelId()` ids),
`glassPanel` per-rect backdrop-UV projection with clamped edge bleed, contrast scrim, new edge
effects (specular top, inner rim, bottom shadow), `glassText` halo helper, `Palette::onAccent`.
**N3 (icons)** — runtime SDF derivation from the atlas alpha (chroma-gated monochrome allowlist,
8SSEDT, R8 atlas) + size-exact re-raster cache + `icons::drawGlass()` contrast halo; gui_lint icons
rule now also covers ui/imgui/src and icons::draw/inlineIcon/drawGlass; ICONS.md rewritten.
**N4–N7 (panels)** — FM/Explorer/left rail, Copilot (glass bubbles via measure-then-fill draw-list
channels, per-doc session parking in syncChatToDoc), doc tabs, toasts, HUD/overlays on adaptive
surfaces; chrome stays theme-colored; single scene render/frame (window-sized backdrop pre-render
deleted); backdrop invalidated when the Viewport tab is hidden.
**N8/N9 (perf)** — `AI_CAD_SHOT=glassperf` scenario (orbit + chat seeds + fixture mesh) prints
frame/GPU metrics and captures a sampling-profiler report; new `prof_target_push/pop` RPCs let the
GUI thread register with the libaicad sampler (2891 samples @997 Hz over the 3 s window). Measured
on the RTX 3080 Ti: glass GPU cost ≈ 0.07–0.17 ms (blur 0.02–0.06, luma 0.05–0.11), frames
vsync-locked (avg 16.9 ms, p99 17.9).
**Integrator extras** — `ui::viewportLoadAicm()` harness + the missing shaded-mesh upload path
(`meshFromBody`/`uploadDocMeshes` ported from host `loadDocMesh`; AI-CAD2 previously fed only the
id-pass, so no scenario ever displayed a mesh); `pick` now renders `contracts/fixtures/box.aicm`.
**Known-deferred** — PM body still uses explicit palette colors under an adaptive ambient push;
FM "Model Tree" heading + dim reference-plane rows can read washy over a very bright floor in
light mode (rail vs pane sampling bands); profiler symbolization of the stripped GUI binary is
module-level (`-rdynamic` would fix); worker tessellation on this box returns no aicm (pre-existing
— scenario docs build but don't display without the fixture path).
**Gates** — build/linux-app green; gui_lint icons/theme clean; feature_surface_audit +
parity_audit failures are pre-existing and untouched.

### PROMPTS — non-vision review path (AI-experience wave, WS-A/A4)
Write-set: `services/agent/prompts/reviewer.md`, `services/agent/loop.py`. In
`reviewer.md`, replaced the one-line "(or, if no image is available, the mass properties
and feature tree)" fallback with an explicit "NO IMAGE AVAILABLE (non-vision model)"
paragraph directing the reviewer through `describe_part()` (named outline: bbox, feature
groups, symmetry, placement), `describe_scene()` (per-view visible-face text, occlusion
disclaimed), `check_feature_overlap()` (intra-part boss/cut overlap + swallowed-boss),
`check_interference()` (assembly clearance/clash), and `measure_part()`, plus an explicit
instruction to confirm every requested feature's placement against the outline before
approving; the rest of the file (render-reading cues, concrete checklist, JSON reply
contract) is untouched. In `loop.py`, added `_no_vision_note(res)` (new helper, right
after `_feedback_ok`) and wired it into the non-vision branch of `run_turn`'s per-iteration
feedback (~L987) so a non-vision provider gets 3–4 extra sentences pointing it at the
face histogram, bbox/fill %, and (for assemblies) the FIT REPORT already present in
`_feedback_ok`'s digest — no new engine/MCP calls, since `ModelResult.faces` only carries
`{total,planar,cylindrical}` (no per-face or per-body breakdown) so a "largest faces per
body" summary was not available from data the loop already holds and was correctly
omitted rather than faked. The vision branch (`_feedback_ok(res)` + image + prompt_text)
is untouched — verified byte-identical by inspection since only the `else` branch changed.
**Verify:** `python3 -m py_compile services/agent/loop.py` — clean.

### KNOWLEDGE — scraped-knowledge cheatsheets + scraper script (AI-experience wave, WS-D/D4)
Write-set: `tools/scrape_knowledge.py` (new), `services/agent/prompts/shared/cheatsheets/{fits,threads,bearings,materials}.md`
(new). Added a stdlib-only draft generator (`tools/scrape_knowledge.py`): built-in
`TOPIC_URLS` dict (fits/threads/bearings/materials → public reference URLs), fetches each
via the Firecrawl REST v1 `/scrape` endpoint (`Bearer $FIRECRAWL_API_KEY`,
`formats:["markdown"]`) when the env var is set, else `urllib.request` with a browser
User-Agent + a crude tag-strip fallback (verified live against Wikipedia in this session —
112 KB draft produced for the `materials` topic with no `FIRECRAWL_API_KEY` set); writes
`<topic>.md` drafts under `--out` (default `/tmp/knowledge_drafts`), one `## <url>` /
`Source: <url> (retrieved YYYY-MM-DD)` section per page. The script refuses to write into
`services/agent/prompts/shared/cheatsheets/` (explicit path-equality guard) so it can never
clobber a committed cheatsheet — it only ever produces scratch drafts for a human curator.
Wrote the four committed cheatsheets by hand (table-first, phrased for the modeling agent,
each ≤60 lines, well under the 120-line cap): `fits.md` (ISO 286 H7/g6·H7/k6·H7/p6·H11/c11
clearance/interference ranges for 3–50 mm plus FDM-clearance guidance — press fits routed to
heat-set inserts/fasteners rather than interference, matching
`docs/INTAKE_MEASUREMENT_DFM_PLAN.md`'s process-profile table), `threads.md` (ISO metric
coarse M2–M12 pitch/tap-drill/minor-Ø/clearance + UNC #4–3/8" equivalents + a "model ≥ M6 or
use an insert" printed-thread rule), `bearings.md` (608/6000-series/625/688 bore×OD×width +
rotating-vs-stationary-ring shaft-k6/housing-H7 fit table + a printed-housing section warning
against relying on plastic interference to retain a bearing), `materials.md` (density/E/yield
for mild steel, 6061-T6, 304 SS, brass, ABS, PLA, PETG, nylon, one selection line each, noting
the FEA-material-entry and Z-axis-anisotropy caveats for printed polymers). No dispatch code
was touched: `cad_guide_text()` in `services/mcp/cad_tools.py` already resolves an unknown
topic via `_read_prompt(f"shared/cheatsheets/{topic}.md")` (confirmed by reading the existing
source), so `cad_guide("fits")` etc. auto-discover the new files by filename alone, sitting
beside `fillet.md`/`hardware.md`/etc. **Verify:** `python3 -m py_compile
tools/scrape_knowledge.py` — clean; all four cheatsheets end with a `Sources:` line citing
public references (ISO 286-2, ISO 261/262, ANSI/ASME B1.1, SKF bearing/fit tables, MatWeb
typical values) and are 39–60 lines each.
## Liquid-glass v2 → HOST port (production binary) — 2026-07-01

Follow-up to the AI-CAD2 restyle: the user launches `./build.sh` → host/build-linux/bin/AI-CAD
(host/DEPRECATED.md: "host/ remains the production binary"), so the full glass system was ported
to host/src via a second 7-node wave against local://glass_host.md (H1 renderer backdrop/Kawase/
async PBO luminance grid/GL timers; H2 ui_theme AdaptiveSurface/adaptivePanel/panelId/GlassLevel/
glassPanel-UV/glassText/glassIconHalo with the SAME theme-independent vibrancy constants as
ui/imgui; H3 icons_sdf port + ui::drawIconGlass; H4 left pane one UltraThin surface
("host_left_glass") + adaptive text; H5 legacy_chat translucent glass bubbles — deliberately
REVERSES the old opaque-dark card decision per the restyle brief — measure-then-fill channel
split; H6a viewport_panel backdrop publish + sampler (15% inset band) + adaptive HUD pills;
H6b doc-tab band adaptive Thin glass with the opaque panelBg fill kept as backdrop-invalid
fallback + adaptive toasts). Integrator: host/CMakeLists.txt gained src/ui/icons_sdf.cpp;
glassIconHalo hoisted into ui_theme (was duplicated in doc_tabs/viewport_panel).
AI-CAD2 extra: empty scenes now render the GL studio backdrop (viewport.cpp renderViewport no
longer early-outs without a doc), so the glass look is live from first launch in both apps —
previously an empty scene fell back to a flat draw-list gradient and the restyle was invisible
until a mesh loaded (the "I don't see any changes" report). gui_lint icons/theme clean; scale
findings unchanged (pre-existing); host tree/chat scenario shots verified visually.

### RELAY → GUI session (from AI-experience DAG integrator, 2026-07-02)
User request for the GUI-polish lane (not handled by the AI-experience DAG; deferred per user):
(1) every text/icon overlay drawn over the 3D viewport needs a proper glass background pill
(`glassRowBackground`/`glassRowAtCursor` pattern); (2) PM feature forms should get SLIDERS for
bounded numeric params (angles, draft, fillet radius within edge limits, pattern count) alongside
the text fields; (3) viewport indicators need a cleanup pass — simulation-setup glyphs (fixtures/
loads), feature preview rendering, and the drag arrows/handles for hand-tuning features should be
visually consistent (consistent palette, occlusion-aware sizing, pill-backed labels). User: "a lot
of it's messy, and it's up to you to clear it up."


### DATASET — synthetic GSL dataset factory (AI-experience wave, WS-D/D1)
Write-set: `tools/gsl_dataset_gen.py` (new), `corpora/gsl_dataset/README.md` (new). Added a
stdlib-only generator (`json`, `argparse`, `random`, `pathlib`, `sys` only) with seven family
templates (`plate_bolt_circle`, `pocketed_block`, `turned_shaft`, `ribbed_bracket` — 50/50
rib-gusset-or-second-boss, `shelled_enclosure`, `bladed_disk`, `swept_pipe`), each grounded
directly against `services/agent/prompts/shared/ops_cheatsheet.md` +
`services/agent/prompts/shared/cheatsheets/sweep.md` and built with `core_bridge.dim`/`feat`.
Every op batch was smoke-tested live against the engine (`core/build-linux/bin/libaicad.so`)
before being committed to the script; **all 7 families built 100% of samples on this engine
build — zero failures** across a 2-sample acceptance run and a 50-sample-per-family stress run
(350/350). No family needed the "report and continue" degraded path, so there is nothing to
list as broken; the per-sample try/except (logs `[family] sample seed=N FAILED: <error>` and
continues) and the `--families` filter remain in place for when a future engine change
regresses one. Per sample: `document.json` (`doc_tree`), `faces.json` (`doc_query` face matches
labelled with the owning feature's TYPE, resolved by joining `doc_tree`'s `owner_map`
`(body_id, face_id)` pairs against each match's `hint_index + 1` — `doc_query`'s own
`owner_feature` field is always null since that RPC never threads the owner map through, a gap
discovered and worked around here rather than filed), `adjacency.json` (`doc_adjacency`),
`outline.txt` (best-effort `mcp_server.gsl_tools._outline_text(_digest(doc_id))`, imported
lazily per-sample and caught so a concurrent edit to `gsl_tools.py` degrades to a placeholder
string instead of crashing the run), and `meta.json` (bbox, `eval_mass` volume, ops count,
generator version). `doc_close` is called after every sample (confirmed live as a real RPC,
same as `cad_tools.py`'s usage) so a large `--count` run doesn't accumulate open doc sessions.
**Gitignore gap (flagged, not fixed — outside this task's write-set):** neither the top-level
`.gitignore` nor a `corpora/.gitignore` excludes `corpora/gsl_dataset/*` sample output; README
documents this explicitly and asks the integrator to add one before samples get generated at
scale. **Verify:** `python3 tools/gsl_dataset_gen.py --count 2` from repo root → 14/14 samples,
7/7 families, all five files present per sample (tree listing + a sample `meta.json`/`faces.json`
inspected and confirmed correctly labelled, e.g. `plate_bolt_circle` faces split `box`(4) vs
`bolt_circle`(7) with the bore faces correctly attributed to the hole feature, not the plate).

### CORE-OVERLAP — intra-part feature-overlap detection (AI-experience wave, WS-B/B1+B2+B4)
Write-set: `core/doc/document.h`, `core/doc/document.cpp`, `core/doc/session.h`,
`core/doc/session.cpp`, `core/api/libaicad.cpp`. **B1** — added `ToolRec{feature_id, role,
tool}` + `CompileResult::tool_recs` (`document.h`) and captured the actual pre-boolean tool
solid (+ "add"/"cut" role) at every boolean callsite in `compile_document`: both primitive
boolean branches, extrude (cut and boss/base), revolve, sweep, rib, the explicit `boolean`
feature (fuse/cut only — `common` is skipped, it fits neither role), linear/circular pattern,
both mirror paths (feature-mirror and whole-body), hole, and bolt_circle — 16 capture sites via
one `record_tool` lambda. Deliberately kept **independent** of the pre-existing
`tool_body_of`/`feature_op_of` maps: those store NESTING-oriented shapes for pattern/mirror
REPLAY (e.g. an additive pattern's `tool_body_of` is its whole grown body, so an outer pattern
can re-fuse it wholesale), whereas a `ToolRec` always holds the real incremental geometry a
feature added or removed, which is what a pairwise `BRepAlgoAPI_Common` needs to measure
genuine overlap. For additive linear/circular patterns this meant building a fresh
seed+copies union (mirroring the cut branch's existing `pattern_tool` logic) rather than reusing
`res` (the whole grown body). Purely additive — no existing behavior/return shape changed.
**B2** — new `doc_eval_feature_overlap(doc_id, params)` (`session.h`/`.cpp`) pairs every
unordered combination of `tool_recs` (via `ensure_compiled`'s cache, unlike
`doc_eval_interference` which always recompiles), classifies `boss_overlap`/`cut_overlap`/
`cut_into_boss`/`boss_swallowed` (add+cut where common volume >= 99% of the additive tool's
volume), and reports `dead_features` (ok, non-suppressed features that made a tool but own no
surviving face/edge per the owner map — fully consumed downstream). `ok` is false only for a
`boss_swallowed` pair. `params.features` restricts pairing to combinations touching at least one
listed id (bounds add_features' future latency); `params.min_volume_mm3` floors reportable
overlap (default 1e-3, mirrors `INTERFERENCE_EPS`). Dispatched as `eval_feature_overlap` in
`libaicad.cpp` beside `eval_interference`. **B4** — `interference_report`/`doc_eval_interference`
gained `include_coincident` (near-zero-clearance pairs, distance <= 1e-6, go to a new `contacts`
array instead of `clearances`) and `min_clearance_mm` (tags a `clearances` entry below it with
`"warning":"below min clearance"`); `contacts` is always present (empty by default) so the
return shape is stable either way. `doc_eval_interference` took a `params` json param (default
`{}`, backward compatible) threaded from the `eval_interference` dispatch. **Deferred/out of
scope** (per the assignment's write-set): MCP surface (`check_feature_overlap`,
`check_interference` params, `add_features` auto-warning — WS-B/B3, a different node's
write-set) and core tests (`core/tests/test_interference.cpp` — WS-B/B5, also a different
node). **Verify:** `cmake --build core/build-linux -j12` — clean, no new warnings in touched
files (pre-existing `ctest` flakiness on `appearance`/`drawing_gdt`/`hole` under the parallel
harness is unrelated — all three pass 100% run standalone). Live smoke via
`core/build-linux/bin/libaicad.so` + Python `ctypes`: a 40mm cube + a d10h10 boss fused on top +
a 20mm cube cut centered on the boss → `eval_feature_overlap` returns exactly the two expected
pairs (`Base`<->`SwallowCut` = `cut_into_boss` 2000 mm³, `Boss`<->`SwallowCut` = `boss_swallowed`
785.4 mm³, `ok:false`, `dead_features:["f2"]` since the boss's geometry is fully swallowed); a
cube + a disjoint far-away boss (`merge:false`) → `pairs:[]`, `ok:true`. Also spot-checked the
`features`/`min_volume_mm3` filters, an assembly doc (degrades gracefully to an empty report —
`compile_document` tolerates the missing `features` key), and B4's `include_coincident`/
`min_clearance_mm` on a flush two-box part (touching pair correctly moves from `clearances` to
`contacts`) and a far-apart pair (correctly tagged `"warning":"below min clearance"`).

### GSL-TEXT — non-vision geometry/sketch text channel (AI-experience wave, WS-A/A1+A2+A3)
Write-set: `services/mcp/gsl_tools.py` only. **A3** extends `_digest`/`_outline_text`
(signatures unchanged) with a per-feature placement narrative: mean centroid + dominant
principal side + offsets from the part bbox on the two perpendicular axes + `touches` (other
feature ids bridged by an adjacency edge). Ownership join required a workaround discovered at
runtime: `doc_query`'s per-face `owner_feature` field is **always null**
(`core/doc/picks.cpp::resolve_geom_query` hardcodes `ti.owner = std::nullopt`), so ownership is
instead joined from `doc_tree`'s `owner_map` `[body_id, topo_id]` pairs against `doc_query`'s
0-based match order (`topo_id == index + 1`, verified empirically on a box+hole: owner_map's
4/3 face split for the box/hole features lined up exactly with doc_query's face order). A feature
owning zero faces prints `"feature <id> owns no surviving faces (consumed by later ops)"`
(verified live: a boss fully swallowed by a later cut correctly falls into this branch — doubles
as WS-B's dead-feature signal on the read path). **A1** adds `describe_sketch(sketch, doc_id)`
composing `doc_sketch_overview` + `doc_sketch_regions` + the authored sketch feature from
`doc_tree` into plane/entity-roster/constraint-roster/regions/DOF-verdict text. Two RPC param
gotchas found only by running them: both `doc_sketch_overview` and `doc_sketch_regions` take the
sketch feature id under the JSON key **`sketch`**, not `sketch_fid` (that's only the C++
parameter name; `libaicad.cpp`'s dispatch reads `params.value("sketch", ...)`) — the plan's RPC
shorthand was misleading here. `doc_sketch_overview` carries no `constraints`/`measured` field
(only `entities`/`statuses`/`dof`/`plane_frame`/`status`/`skipped_constraints`), so driving-
dimension values are read from the authored params as the plan's fallback anticipated, and the
DOF verdict's "conflicting" detail degrades to listing entity ids with `status:"over"` (no
`conflicting` constraint-id list is available from this RPC). **A2** adds `describe_scene(views,
doc_id)` (per-view visible-face listing from signed `normal` dot camera-dir > 0.15, explicit
occlusion disclaimer, part-level bbox + A3 placement narrative up top) and the standalone,
dependency-free `_scene_caption(doc_id, views)` helper (one paragraph; exported for the Wave-2
`cad_tools` node's `render_part`/`contact_sheet` caption). View directions match render_part's
documented convention exactly (front=-Y, back=+Y, right=+X, left=-X, top=+Z, bottom=-Z,
iso=normalize(1,1,0.8)) — cross-checked against `core/doc/session.cpp::resolve_view_spec`.
**Verify:** `python3 -m py_compile services/mcp/gsl_tools.py` clean; live smoke against
`libaicad.so` (box 40x30x20 + a d8 through-hole): `describe_part` outline correctly reports
"f1 Base [box] (4 face(s)): on right face of f2, centered 15 mm from -Y edge / 10 mm from -Z
edge" and "f2 Bore [hole] (3 face(s)): on bottom face of f1, centered 20 mm from -X edge / 15 mm
from -Y edge"; `describe_scene` iso view lists the 3 faces whose normals actually face
`(0.62,0.62,0.49)`; a rect+circle sketch with coincident/H/V relations + 2 driving dimensions +
1 radius dim reports "Under-defined: 2 degree(s) of freedom remain" (circle center unconstrained)
with both regions (outer 40x20 material, inner void) and their areas/bboxes correctly split.
Unknown-sketch id returns the plan's exact `_err`-style hint. No changes needed to
`_digest`/`_outline_text` call sites elsewhere — `server.py` only imports `register_gsl_tools`.

### MCP-SURFACE — check_feature_overlap + auto-warnings + render captions + guide topics (AI-experience wave, WS-B B3 / WS-A A2/A4)
Write-set: `services/mcp/cad_tools.py`, `services/mcp/server.py` (instructions string only).
Depends on CORE-OVERLAP's `eval_feature_overlap`/`eval_interference` RPCs and GSL-TEXT's
`describe_sketch`/`describe_scene`/`_scene_caption`, both confirmed present. Added
`check_feature_overlap(doc_id, features)` right after `check_interference`, following its exact
`cb.call` + `_dumps({"ok": res.get("ok", True), "doc_id": d, **res})` pattern; docstring explains
boss_overlap/cut_overlap/boss_swallowed/dead_features and when to reach for it vs
check_interference. `add_features`'s success path now derives the batch's own new feature ids
from `r["id_map"]["$<op-index>"]` (only for `add_feature` ops), best-effort calls
`eval_feature_overlap({"doc_id","features":added_ids})`, and attaches `overlap_warnings`
{summary, pairs, dead_features} when non-empty — never fails the batch, and is skipped
entirely when the batch added no features. `check_interference` gained `include_coincident`/
`min_clearance_mm` params threaded straight through to `eval_interference`, documented alongside
the new `contacts` array and the "below min clearance" warning. `render_part`/`contact_sheet`
now return `-> list`: a new `_captioned_render(png, doc_id, views)` helper lazy-imports
`mcp_server.gsl_tools._scene_caption` inside a try/except and returns `[Image(path=png),
caption]`, degrading to the bare `Image` on any failure (missing gsl_tools, bad doc_id); every
existing return point in both tools was routed through it. Guide discoverability: `_GSL_GUIDE`
now documents `describe_sketch`/`describe_scene` alongside `describe_part`/`hover`/
`query_adjacency`; `cad_guide_text` gained a `("reverse","re","recognize","recon")` branch that
lazy-imports `mcp_server.recon_tools._RE_GUIDE` and falls back to a one-line pointer via
`except ImportError` (recon_tools.py doesn't exist yet — this future-proofs the wave-3 node
with zero coupling); the no-match fallback string and the `cad_guide` tool docstring both now
list gsl/reverse alongside overview/simulation/review/intake/cam. `server.py`'s FastMCP
instructions string gained one sentence pointing at the non-vision describe_* path and at
check_feature_overlap (intra-part) vs check_interference (assemblies) — the ECAD closing
sentence was preserved. **Verify:** `python3 -m py_compile services/mcp/cad_tools.py
services/mcp/server.py` clean. Live smoke against a freshly built `libaicad.so`
(`core/build-linux/bin`, staged via `AI_CAD_LIBAICAD` since `core_bridge`'s default Linux
candidate `core/build-occt-linux/bin` was stale and lacked the new RPC — a real repo issue for
whoever restages the default candidate list, not touched here since `core_bridge.py` is outside
this node's write-set): built a 40³ box + a d10h10 top boss (deliberately half-embedded, so
`add_features` also surfaced a `boss_overlap` warning on that batch) + a 20³ cut engulfing the
boss — the boss-adding batch's own `overlap_warnings` and the cut batch's `overlap_warnings`
both appeared automatically, the latter naming the exact `boss_swallowed` pair (Boss<->
SwallowCut, ~785 mm³) plus `dead_features:["f2"]`; a direct `check_feature_overlap` call on the
full tree returned `ok:false` with all three pairs (boss_overlap, cut_into_boss, boss_swallowed)
sorted by volume descending, matching the core's own ordering. `check_interference` with
`include_coincident=True` on two flush-stacked duplicate boxes returned a `contacts` entry
`{a:"b1",b:"b2",distance:0.0}`; with `min_clearance_mm=5.0` on two boxes with a 1 mm gap it
tagged the clearance `"warning":"below min clearance"`. `await mcp.call_tool("render_part", {})`
returned exactly `[ImageContent, TextContent]` with a real one-paragraph caption (and likewise
for `contact_sheet`, four-view caption). `cad_guide("gsl")` mentions both `describe_sketch` and
`describe_scene`; `cad_guide("reverse")` returns the exact wave-3 fallback pointer string. No
other files touched; `gsl_tools.py` was read-only (imported, never edited).

### CORE-RECON — eval_compare + doc_face_wires (AI-experience wave, WS-C C1+C2)
Write-set: new `core/doc/recon.h`/`core/doc/recon.cpp`; appended wrappers in
`core/doc/session.h`/`core/doc/session.cpp`; appended dispatch lines in
`core/api/libaicad.cpp` beside `eval_feature_overlap`; `core/CMakeLists.txt` (added
`doc/recon.cpp` to the `aicad_doc` source list only). `recon.{h,cpp}` are Session-free — pure
OCCT geometry (`compare_shapes(TopoDS_Shape,TopoDS_Shape)`, `face_wires(TopoDS_Face)`) — so the
doc-resolving glue (require_session, body/face selector resolution) stays in session.cpp's
`doc_eval_compare`/`doc_face_wires` wrappers, dispatched as RPC methods `eval_compare` and
`doc_face_wires`. `eval_compare`: `{doc_a,doc_b,body_a?,body_b?}`, default body = `geom::fuse_many`
of every surviving solid (mirrors `doc_eval_interference`'s member collection); named body_x uses
the same `"b1".."bN"` convention. Returns volume_a/b, common/missing/extra_mm3, match_pct, up to
10 residual regions (side/volume/center/size, exploded via `TopExp_Explorer(TopAbs_SOLID)`,
sorted desc, <1e-3 mm³ dropped), and a one-line summary; every boolean uses `SetFuzzyValue(1e-4)`
and a null/failed boolean degrades to `{degraded:true, match_pct:null, volume_ratio, bbox_a/b,
centroid_dist_mm}` instead of throwing. `doc_face_wires`: `{doc_id,{face:<selector-or-geom-query
or {"sid":...}>}}` resolves against the last body's faces (sid matched via `make_selector`'s sid,
else the same `eval_resolve_ref` predicate path `doc_eval_measure` uses), converts a planar
face's wires into doc-schema §3.1 entities in the face's own plane frame (built via
`aicad::geom::SketchPlane::from(origin,normal)` — byte-identical convention to
`resolve_sketch_plane`'s face-sketch branch): line/circle/arc (ccw resolved from an actual
traversal-midpoint sample)/ellipse/ellipse_arc, anything else sampled to a 24-segment polyline
with `approx:true`. Non-planar face throws `OpError("invalid_op", "convert entities requires a
planar face")`. **Verify:** `cmake --build core/build-linux -j12` clean. Live ctypes smoke
(`AI_CAD_LIBAICAD` pointed at the fresh `core/build-linux/bin/libaicad.so` — same stale-default
candidate-list gotcha MCP-SURFACE already flagged): two independently-built 40×30×20 box docs ->
`eval_compare` match_pct 100.0 (0 residual regions); a third box translated +10mm in X ->
match_pct 75.0 with exactly one 6000 mm³ `missing` region at `[-15,0,0]` and one 6000 mm³
`extra` region at `[25,0,0]` (both against a 24000 mm³ box — arithmetically exact). `body_a`/
`body_b` `"b1"` naming round-trips; an unknown body name (`"b2"` on a single-body doc) raises
`eval_compare: unknown body 'b2' in document '<id>'`. `doc_face_wires` on a bare box's top face
returned exactly 4 `line` entities in one `outer:true` wire; the same face on a box with an
8mm-diameter through hole returned the same 4-line outer wire plus a `radius:4.0` `circle` in a
second `outer:false` wire — both by the `{"of":"face","side":"top"}` geom-query form and by
`{"sid":<doc_query's sid>}`. The `ok:false`-vs-`degraded` split is intentional: an EMPTY doc (no
bodies at all) raises `OpError("invalid_op", "...has no bodies")` from the wrapper before ever
reaching `compare_shapes` — a clearer error than a soft "degraded" match — so the degraded branch
is unreachable from the RPC with an empty-vs-box pair as literally worded in the ask; verified the
branch itself directly instead with a standalone harness (recon.cpp + geom/plane.cpp linked
against system OCCT 7.9.1 in `/tmp`, no repo files touched) calling `compare_shapes` with a
genuine null `TopoDS_Shape`: returned `{degraded:true, match_pct:null, volume_ratio:0.0,
bbox_a:null, bbox_b:{...}, centroid_dist_mm:-1.0}` as designed, and `compare_shapes(box,box)` from
the same binary still returned the normal 100%-match shape — confirming both branches compile and
run correctly. **Tool-issue note:** the `edit` tool repeatedly rejected a `SWAP` on
`core/CMakeLists.txt` claiming a stale hash while `read`/`grep` on the same file consistently
returned the tag the edit claimed was current-invalid, with disk content verified byte-identical
via `sed`/`md5sum`; worked around by patching the file directly through the `eval` `write()`
helper (see the `report_tool_issue` call this session). No other files touched.

### GNN-SCAFFOLD — gated GNN adapter scaffold, no training run (AI-experience wave, WS-D D3)
Write-set: new `tools/gsl_gnn/{dataset.py,model.py,train.py,README.md,requirements.txt}` only.
`dataset.py` walks `corpora/gsl_dataset/<family>/<seed>/{faces.json,adjacency.json,meta.json}`
(layout confirmed against the already-generated corpus: `faces.json["matches"][i]` and
`adjacency.json["faces"][i]` share the same `describe_faces()` enumeration order, so `edges[*]
.between` indices join directly, no re-keying needed) and builds one PyTorch-Geometric `Data`
per sample: 18-dim node features (`surf` one-hot over
plane/cylinder/cone/sphere/torus/bspline/other, `area_or_len/diag²`, `log1p(radius or 0)`,
`concavity` one-hot bore/boss/none, unit normal xyz, `centroid/diag` xyz; `diag` = bbox
diagonal from `meta.json.bbox.size`), edges inserted both directions with `edge_attr =
[convex, length/diag]`, and `y` from a deterministic sorted label vocab built across every
`faces.json` under the root (`build_label_vocab`) — verified against the live corpus: 9
classes (`bolt_circle, box, circular_pattern, cylinder, extrude, revolve, rib, shell, sweep`).
`model.py` exposes `build_model(in_channels, num_classes, hidden_channels=128, dropout=0.2)`:
3-layer GraphSAGE + ReLU + dropout + linear per-node head. `train.py` is a CLI
(`--data/--epochs/--lr/--device/--out/--hidden/--dropout/--batch-size/--val-frac/--seed`)
doing a family-stratified 80/20 split, cross-entropy, per-epoch train/val loss+accuracy
printed to stdout, and writes `best.pt` (state_dict + label vocab + hyperparams) plus
`confusion_matrix.{json,txt}` (no plotting dependency) under `--out`. Both `dataset.py` and
`model.py` import `torch`/`torch_geometric` lazily inside functions (never at module scope),
so all three modules `py_compile` clean and are importable with neither package installed;
calling into any torch-requiring function without the deps raises a clear `ImportError`
pointing at `requirements.txt` — verified live in this session (no torch/torch_geometric
installed on this box): module import, `build_label_vocab`, and the pure-Python
`_node_features`/`_bbox_diag` helpers all ran correctly against the real corpus (18-dim
feature vectors, correct 9-class vocab), `train.py --help` printed normally, and both
`load_samples(...)` and `build_model(...)` raised the intended `ImportError` instead of a
bare `ModuleNotFoundError`. `requirements.txt` pins `torch>=2.2`/`torch-geometric>=2.5` with a
comment noting CUDA wheel choice is user-side. README.md states the hard gate verbatim-in-
spirit (train only for classes `docs/RECOGNITION_RESIDUAL.md` flags as median match_pct <
90% or faces-claimed < 70%; output feeds a templated caption head only, never free-form
numbers; never suppresses/replaces the raw face list; no soft-token/vision adapter per
`GEOMETRY_LLM_REPRESENTATION_REVIEW.md` §3.6; engine-inference wiring is a separate later
decision), plus setup/run commands and the `tools/gsl_dataset_gen.py --count 50` corpus
regeneration pointer. No training run performed, no packages installed, no other files
touched, git untouched.

### TESTS-CORE — ctest coverage for eval_feature_overlap/eval_compare/interference params (AI-experience wave, WS-B B5-core)
Write-set: new `core/tests/test_interference.cpp`; appended one test-target block to
`core/CMakeLists.txt` (after `test_mirror`, inside the existing `if(AICAD_WITH_OCCT)` region,
registering `add_test(NAME interference ...)`). Unlike `test_document.cpp`/`test_mirror.cpp`
this file never touches the host AICM loader — every case calls `doc_new`/`doc_apply_ops`/
`doc_eval_interference`/`doc_eval_feature_overlap`/`doc_eval_compare` (`core/doc/session.h`)
directly and asserts on the returned JSON, so the target links only `aicad_doc`/
`aicad_geom_occt`/`aicad_sketch`/`aicad_geom` with no OCCT include dir or render sources.
Geometry is built entirely from primitive `box`/`cylinder` features placed via
`params.at`+`params.centered:false` (corner/base-center convention, confirmed by reading
`document.cpp`'s primitive branch) so every expected volume is closed-form exact, not a
tolerance guess. 8 `TEST_CASE`s / 47 assertions, all real (verified with `--success`, no
vacuous empty-loop checks): (1) a `copy:true` transform of a box gives two disjoint-body
overlap of exactly 4000 mm³, asserted at 1e-6 relative tolerance via `eval_interference`;
(2) base+boss+engulfing-cut → `eval_feature_overlap` reports `ok:false` with a
`boss_swallowed(boss,cut)` pair and the boss id in `dead_features` (also incidentally
surfaces a `boss_overlap(base,boss)` and `cut_into_boss(base,cut)` pair — expected, not
asserted against); (3) two overlapping cylinder cuts into one plate → a `cut_overlap` pair
with positive volume; (4) two 100mm-apart boxes (`merge:false`) → zero pairs, `ok:true`;
(5) `eval_compare` on identical geometry in two independently-built docs → match_pct ≥
99.5% (actual 100.0), empty `residual_regions`; a 10mm-translated copy of the same box →
match_pct 66.67% (< 90), with ≥1 `missing` and ≥1 `extra` residual region, both positive
volume; (6) two flush-stacked boxes (`merge:false`, zero gap) show no `contacts` entry by
default but one at distance ~0 with `include_coincident:true`, and separately a 0.05mm gap
is tagged `"warning":"below min clearance"` under `min_clearance_mm:0.1`. Feature ids used
in assertions are read back from `doc_apply_ops`'s `rebuild.feature_status[i].id` (never
assumed/hardcoded — `add_feature` ids are engine-assigned `f1,f2,...` regardless of any
client-supplied `id`). Verified: `cmake --build core/build-linux -j12` clean (incremental
no-op on the second run, zero errors), `ctest --test-dir core/build-linux -R interference
--output-on-failure` → `1/1 Test #36: interference ... Passed` (100% tests passed). Full
suite (`ctest` unfiltered) intentionally NOT run per the assignment's scope. No files
outside the stated write-set touched; no git commands run.

### APP — in-app AI hooks: E2 worker routing, E4 reverse_engineer mission capability (AI-experience wave, WS-E)
Assigned write-set: `app/src/app/doc_engine.{cpp,h}`, `app/src/ui/agent_panel.cpp`,
`app/src/app/app_state.cpp`, `core/agent/run.cpp`, `services/worker/worker_client.cpp`.
Delivered E2 + E4 in full; E3 (import-summary toast + Copilot chip) was descoped after
discovering mid-task that `app/src/app/*` and `app/src/ui/agent_panel.*` are now SYMLINKS
into `ui/imgui/src/...` (the G1 host->ImGui port landed after this plan was written) —
editing them means editing the exact tree the concurrent GUI session owns exclusively.
Flagged to the integrator over IRC; confirmed decision was to keep E2/E4, revert a
trial `doc_engine.h` header change (two new method decls with no `.cpp` body would have
broken the app link), and hand E3 to the GUI session via `docs/HANDOFF_AI_EXPERIENCE.md`.
**E2**: traced `services/worker/worker_client.cpp`'s `routeDocToCpp` — it sniffs `params`
for a `doc_id`/`into` key with a `doc-cpp`/`dwg-cpp` prefix to decide C++-core vs. Python
routing, method-name-agnostic. `eval_interference`/`eval_feature_overlap`/`doc_face_wires`
all key on `doc_id` and were already covered. `eval_compare` uniquely keys on `doc_a`/
`doc_b` (two documents, no `doc_id` field at all) and was silently falling through to the
(nonexistent, for this RPC) Python path — a real bug, not a hypothetical. Fixed by adding
`doc_a`/`doc_b` to the sniffed-key list (7-line diff, `routeDocToCpp`, ~L86-92). **E4**: added
a `reverse_engineer` capability branch to `run_mission` (`core/agent/run.cpp`) at the
"recognized but not yet wired" stub. Also added a `options.capability` override that lets a
caller route straight to one capability, bypassing the `mission_planner` LLM call entirely
— necessary because the mission_planner prompt template (outside this node's write-set)
has no idea "reverse_engineer" exists and would never emit it unprompted; this makes the
future Copilot chip's canned request deterministic once wired, independent of any prompt
update. The capability itself: creates a fresh doc via `doc::doc_new`, keeps the import doc
as a read-only reference, runs the normal `run_assembly_design` build loop into the new doc
with an appended instruction not to touch the reference, then calls the real
`doc::doc_eval_compare(new, ref)` RPC (in-process, no WorkerClient hop) and folds
`match_pct`/`degraded` into both the task summary text and a new `reverse_engineer_compare`
event so the result surfaces in `agent_run_v2`'s event stream. Took the plan's PRE-DECIDED
FALLBACK rather than the maximal path (threading compare feedback into the reviewer/fixer
`BuiltContext` at every modeler/review/fix iteration): that would mean editing the ~500-line
stage loop inside `run_design` that `design`/`assembly` also depend on, plus the
reviewer/fixer prompt templates (`services/agent/prompts/`, not in this write-set) to teach
them to read an injected compare block — disproportionate blast radius for one capability
branch. The delivered version still does a REAL comparison (never a canned number), just
once per mission rather than once per stage. Verified: `cmake --build core/build-linux -j12
--target aicad_agent` clean (both incrementally and from the edit point forward). No
`app/build*`/`build/linux-app` build attempted for E3 since no E3 code was written under
this node — the app was left untouched. No git commands run by this node (the integrator
owns commits per `AGENTS.md`).

### AI-EXPERIENCE — final verification pass (§TODO-5), lane consolidated (2026-07-01)
Verification node; no git run (orchestrator commits). **Gate**: `bash scripts/gate.sh` →
RESULT: PASS — only `check.py` FAIL-RECORDED (`feature_surface_audit` + `parity_audit`),
which is the documented PRE-EXISTING baseline; core build (OCCT ON), ui/abstract,
core ctest (100%), ui/imgui build+shot+selftest all PASS; gate's engine/agent pytest
step SKIPs (no runtime venv) as usual. **ctest**: `-R interference` 1/1 Passed.
**MCP pytest** (`~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest`, mcp 1.27.2 +
pytest 9.1.1): `test_gsl_tools.py + test_recon_tools.py` 13 passed;
`test_cad_tools.py -k "overlap or coincident or caption"` 6 passed — run BOTH before and
after restaging. **Restage**: `core/build-linux/bin/libaicad.so` (relinked 21:50 after the
E4 `core/agent/run.cpp` capability work) was newer than `services/dist/AI-CAD/libaicad.so`
(21:19) → recopied; all 19 lane tests re-green against the staged lib. No breakage found
in this lane's new files, nothing fixed. **Lane consolidated — files landed across the
TODO nodes**: `services/mcp/recon_tools.py` (`_recognize` 5-pass + `reverse_engineer` /
`compare_parts` / `convert_face_to_sketch`), `services/mcp/server.py` (register after GSL
block), `services/agent/prompts/shared/cheatsheets/reverse_engineer.md` (`_RE_GUIDE`,
served by `cad_guide('reverse')`), `services/mcp/test_gsl_tools.py` (9 tests),
`services/mcp/test_recon_tools.py` (4), append-only tests in
`services/mcp/test_cad_tools.py` (5, before the `__main__` runner), `tools/gsl_eval.py`,
`docs/RECOGNITION_RESIDUAL.md` (full 14-sample run), plus this node's doc touches
(`docs/REFACTOR_LOG.md`, status note atop `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md`,
restaged `services/dist/AI-CAD/libaicad.so`). **Deferred/open**: E3 in-app hooks belong to
the GUI session (HANDOFF §TODO-4); GNN adapter training remains gated — RECOGNITION_RESIDUAL
says GO only for ribbed_bracket / shelled_enclosure / swept_pipe, NO-GO elsewhere;
`corpora/sw_fixtures` has no `*.step` yet (eval path dormant until files appear);
pre-existing (NOT this lane): `test_cad_tools.py::test_check_sketch_{fully,under}_defined`
author the legacy flat sketch shape the staged schema validation rejects. **Gotchas worth
keeping**: pytest under `services/mcp/` imports the dir as package `mcp`, shadowing the
mcp SDK — each test file carries a no-op-when-unshadowed reload shim; recon hole placement
uses a 2-position checkpointed probe because hole positions live in the face's `gp_Pln`
parameter axes; fillet-candidate bound is 20% of MEDIAN bbox dim (documented deviation —
the spec's own r3-on-8mm acceptance part violates the min-dim rule); ALWAYS restage
`services/dist/AI-CAD/libaicad.so` after a core rebuild or new RPCs report
"unknown method".
---

**Glass-v2 audit — drawing-sheet mode + Section View tool (host/src/ui/drawing_panel.cpp,
section_tool.cpp).** Brought the two remaining raw-ImGui surfaces in this lane up to the
liquid-glass v2 standard. `section_tool.cpp`: the `"Section View##panel"` floating window
previously used `SetNextWindowBgAlpha(0.94f)` (flat opaque fill); now suppresses the native
fill (`BgAlpha 0`), splits the window draw list (channel 1 = widgets, channel 0 = bg), and
after the auto-resized rect is known paints a frosted `ui::adaptivePanel(panelId(
"vp_section_panel"), rect, GlassGroup::Hud)` + `ui::glassPanel(Regular, WindowRounding/uiScale)`
behind the content, then merges — matching the local convention already used by the collapsed
`vp_section_chip` pill. Preset/Flip/OK/X/Add/Remove buttons were left as standard themed ImGui
widgets (same choice settings_modal makes for real widgets inside a frosted window; their
palette button roles already read correctly on glass). `drawing_panel.cpp`: added a local
`GlassPopupScope`/`beginGlassPopup()` helper (anon file scope) and applied it to all three
floating dialogs — Edit Dimension (`drw_dimedit_popup`), GD&T editor (`drw_gdtedit_popup`),
Sheet Properties (`drw_sheetprops_popup`): each now `SetNextWindowBgAlpha(0)` before its
`BeginPopup`, splits/merges the draw list, and frosts with `adaptivePanel`+`glassPanel`
(Regular, `PopupRounding/uiScale`) so they join the app glass family instead of the flat
legacy `PopupBg=glassBg` fill. The drawing CANVAS itself (the light "paper" sheet + view
boxes + dimension/GD&T/title-block ink) was deliberately left untouched — it is a
document/paper representation (like the 3D viewport), not chrome, and its dark-on-white ink
must stay per the sketchInk contract. All `ImVec2` math done via explicit constructors (not
`operator+`) to avoid the `IMGUI_DEFINE_MATH_OPERATORS` trap. **Verify:** both files compile
cleanly under `flock /tmp/host-build-linux.lock` (section_tool + drawing_panel objects build
with zero errors); full link + `AI_CAD_SHOT=drawing` screenshot are BLOCKED only by concurrent
sibling agents' in-flight `ImVec2 operator+` errors in cutlist_panel.cpp / equations_dialog.cpp
/ design_table_panel.cpp / evaluate_dialogs.cpp (all outside this write-set) — retry the link +
shot once those land. **Touched:** host/src/ui/section_tool.cpp, host/src/ui/drawing_panel.cpp.

**Liquid-glass v2 — PropertyManager forms (pm_forms.cpp / property_manager.cpp).** Brought the
~44 feature-editing PM forms (extrude/revolve/fillet/chamfer/shell/hole-wizard/pattern/mirror/
draft/sheet-metal/weldments/surfacing/…) up to the liquid-glass v2 standard. Key finding:
`pm_forms.cpp` is a PURE DATA TABLE (form/field definitions) with ZERO render code — it needed no
edits; ALL rendering lives in `property_manager.cpp`'s `drawBody()` + its helpers. The forms
render EMBEDDED inside the pinned FeatureManager glass column (layout.cpp drawLeftPane -> drawBody,
which pushes `ImGuiCol_Text = surf.textPrimary` around the call), so per the host contract they must
REUSE that shared surface — not paint their own `glassPanel` (double-paint). Added an fmSurf-style
local `pmSurf()` helper (verbatim pattern from feature_tree.cpp: same `panelId("host_left_glass")`
+ `GlassGroup::FeatureManager`, id-cached/idempotent) and switched every SECONDARY/dim text site
from theme-static `pal.text`/`pal.textDim` to adaptive `surf.textPrimary`/`surf.textSecondary`
(form description, section captions, selector-well labels + chip text, FeatureWell label, "="
toggle glyph, idle segmented-pill text, live-preview status hint). Interactive rows now get
translucent glass pills via `ui::glassRowBackground()` instead of opaque fills: `pmSectionHeader`
(pill drawn behind the CollapsingHeader, transparent Header* colors), the selector-well fill
(neutral frosted wash idle/hover, semantic accent selected-pill when active — accent/dangling
BORDER semantics preserved). The "=" dim toggle and Boss/Cut-style segmented pills kept their
ImGui::Button geometry but idle fills dropped to ~0.40 alpha so the column glass bleeds through.
DELIBERATELY LEFT ON PALETTE (semantic, not chrome — per the feature_tree adaptive/semantic split):
accent (title, active-well label, active-segment/toggle), statusOk (OK button), statusErr/statusWarn
(errors/dangling/warnings), accentSoft/accentMid (selection/segment washes). LEFT AS-IS: the group
`BeginChild` cards (`ChildBg=glassBg`, `Border=glassBorder`) — already translucent sub-region
containers, not the "opaque plain ImGui widget" the report flagged; repainting them with glassPanel
would be the double-paint the contract warns against. `PropertyManager::draw()` and its
`##PMFloating` fallback (which paints its own `WindowBg=glassBg`) were left UNTOUCHED because that
path is DEAD in the current liquid-glass layout (grep confirms only `drawBody()` is called, from
layout.cpp:534 — `draw()` has no callers); if a future layout revives it, it needs the
standalone-window treatment (own `adaptivePanel`+`glassPanel`, like settings_modal). All `ImVec2`
math uses explicit constructors, never `operator+` (avoids the `IMGUI_DEFINE_MATH_OPERATORS` trap
that broke sibling files). **Verify:** property_manager.cpp compiles cleanly under
`flock /tmp/host-build-linux.lock`; full link succeeded after concurrent sibling `ImVec2 operator+`
errors cleared, and I captured `scratch/gui-overhaul-shots/pmforms_extrude.png` (Extrude, with
up_to_surface + thin revealed), `pmforms_fillet.png` (Fillet, variable-size), and `pmforms_m1.png`
(m1 scenario, ran to completion, no crash/assertion). Screenshots confirm section-header glass
pills, accent-washed active selector wells, segmented pills, and adaptive-contrast labels rendering
over the live frosted column. **Input-frame widgets left opaque — DELIBERATE, matches app
convention:** the Dim (Slider/Drag), Int (DragInt), Combo/DatumRef (BeginCombo), Check (Checkbox)
and Text/`=`-mode (InputText) widgets keep the global opaque `ImGuiCol_FrameBg` (`pal.frameBg`
alpha 1.0). Verified against the already-modernized panels: `settings_modal.cpp`, `sim_manager.cpp`,
and `asm_panels.cpp` all render bare `ImGui::SliderFloat`/`Checkbox`/`BeginCombo`/`InputText` over
their frosted glass with NO FrameBg override — opaque input frames are the established house style
(they preserve value legibility; only chrome/rows/text/section-headers go glass). The only file that
pushes a translucent FrameBg (feature_tree.cpp:848) is a special-case accent-tinted inline-rename
editor, not a general convention. Making the PM input frames translucent would DEVIATE from the
modernized panels, so they were intentionally left as themed opaque frames. **Touched:**
host/src/ui/property_manager.cpp.

**GUI visual-polish pass — 4 layout fixes (m1 blank-part fresh-screenshot audit, host/ only).**
Closed the dead-gap / mislaid-chrome issues Erik flagged on the running app. **(1) Left icon rail
→ horizontal.** `host/src/ui/layout.cpp` `drawLeftPane()`: the `##lp_tabstrip` child (Explorer/
FeatureManager/PropertyManager/Configurations) was a VERTICAL strip down the column's left edge; it
is now a HORIZONTAL icon row across the column's TOP (child sized `railW × railRowH`, buttons via
`SameLine(0,gap)`, btn 30dp/glyph 18dp), with a horizontal separator under it and the tab body
flowing BELOW at full pane width (the old full-height vertical separator + `SameLine(0,0)` beside
the rail are gone) — kills the left gutter dead space (HANDOFF_GUI_OVERHAUL §5 item 1). **(2) Doc
tab strip → floats over the viewport.** `host/src/ui/doc_tabs.cpp` `drawDocTabs()`: was drawn INLINE
in the central document window and reserved its height with a trailing `Dummy(availW,barH)` (which
pinned it above the FeatureManager column AND pushed the whole viewport down). It now opens its OWN
pinned borderless floating window (same flag set as the FM/Copilot columns:
`NoTitleBar|NoResize|NoMove|NoDocking|NoBackground|NoScrollbar|NoSavedSettings|NoBringToFront`),
positioned at `x = (overlayColumnEdge(0)≥0 ? that : band.minX) + 12dp`, `y = band.minY + 12dp` (flush
under the ribbon, just right of the FM column), and the height-reservation `Dummy` is removed. The
window is sized to HUG its content (`winW = min(contentW+6, copilotLeftEdge - docX)`, the `contentW`
tab-measure loop hoisted above the `Begin`) — a full-width window is `NoBackground` but still
input-blocking, so its empty right half would eat clicks on the viewport toolbar + Copilot header
that share the same top row to the right (they render clean but go dead — screenshots can't catch it;
verified with a temporary `AddRectFilled` debug footprint that the rect stops after the "+" button in
both m1 and multidoc). The
band bottom is still published and `docTabStripHeight()` still returns 26 (truthiness gate at both
call sites — viewport_panel.cpp:330 + the out-of-write-set ecad_panel.cpp:28 — preserved). Falls back
to inline draw only on the pre-band-publish first frame. Corresponding `colY` in
`layout.cpp` drawLeftPane (was `max(band.minY+docTabStripHeight, docTabBandBottom)+margin`) and
`legacy_chat.cpp:~3192` (Copilot column) both collapsed to `band.minY + margin`, so both pinned
columns start flush at the viewport top. **(3) Viewport toolbar + Copilot top flush.** The viewport
zoom/pan toolbar (`viewport_panel.cpp` `by0 = cr0.y + 6`, `cr0 = GetCursorScreenPos()` after the
strip) rose to band-top AUTOMATICALLY once the strip stopped reserving height — no edit needed there;
the Copilot top was raised by the `colY` edit above. **(4) Collapse chevron.** `layout.cpp`
`##lp_collapse` block: was hover-only background (read as a stray mark); now a persistent glass chip
(`glassRowBackground` at 0.22 idle / 0.42 hover, 26dp square) parked at the column's top-right corner
and reserved out of the horizontal rail's width (`chevRsv`) so it never collides with the relocated
icon row. **Verify:** built green under `flock /tmp/host-build-linux.lock`; re-shot `AI_CAD_SHOT=
m1|sketch|fea|appearance|multidoc` → `scratch/gui-overhaul-shots/fix4_*.png`, all clean (dead gap
gone, doc tab + chevron coexist, icon row full-width bodies, FEA legend + Settings + multi-tab strip
unregressed); `grep -rc panelsOverlay host/src/` still all-zero (docked layout stays deleted, locked
decision §1). **Touched:** host/src/ui/layout.cpp, host/src/ui/doc_tabs.cpp,
host/src/ui/legacy_chat.cpp (host/src/ui/viewport_panel.cpp needed NO edit — fix 3a was automatic;
host/src/ui/layout.h unchanged).

## Liquid-glass v2 — Evaluate / Equations / Design Table / Cut List dialogs

Brought four standalone floating dialogs onto the v2 adaptive-glass standard; they had been left as
plain opaque ImGui windows (or, more precisely, the older static `pushGlassWindowStyle()` fill, which
is non-adaptive and near-opaque). **Files:** `host/src/ui/evaluate_dialogs.cpp`,
`host/src/ui/equations_dialog.cpp`, `host/src/ui/design_table_panel.cpp`,
`host/src/ui/cutlist_panel.cpp` (all `ImGui::Begin` floating windows over the viewport; no `.h`
signature change needed). **Pattern (identical in all four):** dropped `push/popGlassWindowStyle`; push
a transparent `ImGuiCol_WindowBg` so the adaptive `glassPanel` is the only fill (mirrors the tooltip
"native rect must not compete with the custom glass" contract). Because the title bar renders DURING
`Begin` (before any post-Begin draw — an `ImDrawListSplitter` can't reorder already-emitted commands,
so full-rect glass filmed over the title text + close X; caught in the first `equations` shot), each
window now scopes `ImGuiCol_TitleBg/Active/Collapsed = glassBg` around `Begin` (theme sets these
transparent globally) and frosts only the CONTENT region: `winRect` starts at
`GetCursorScreenPos().y - WindowPadding.y` (title-bar bottom), so the title strip stays crisp and the
seam reads as a header divider. After Begin: `adaptivePanel(panelId("host_<dlg>_glass"), winRect,
GlassGroup::Global)` → `glassPanel(dl, winRect, GlassLevel::Regular, 12.0f, surf)`; body text uses
`surf.textPrimary` (default ink push) / `surf.textSecondary` (the old `pal.textDim` dim role) so
contrast tracks the live backdrop, while semantic colors (statusErr/Warn/Ok, accent header) stay
as-is. PopStyleColor accounting: 3 title colors popped right after Begin; WindowBg (+ default text ink
on content paths) popped 1 (Begin-fail) / 2 (every content exit). **Deliberately left alone:** the
read-only cutlist / design-table / equations grids use ImGui-default `TableRowBg` (~0 alpha), so the
frost already shows through the grid body — row pills would add nothing and clash with the cell
borders; only the design-table SELECTED config-name row got a cue: a translucent `selectionHighlight`
wash via `TableSetBgColor(ImGuiTableBgTarget_RowBg0, ...)` (grid-honoring, not a rounded pill) with the
`Selectable`'s own opaque highlight suppressed (passed `selected=false`). Standard `ImGui::Button`s
left themed (consistent with settings_modal — only custom icon-buttons get pills). **Verify:** built
green under `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"`; re-shot
`AI_CAD_SHOT=equations` and `AI_CAD_SHOT=evalmass` → `scratch/gui-overhaul-shots/dlg_equations.png` +
`dlg_evalmass.png` (zoom crops confirm crisp title/X on the glass strip + legible frosted content).
Design Table and Cut List have NO `AI_CAD_SHOT` scenario, so they are build-verified only (they share
the identical, screenshot-proven pattern). **Touched:** host/src/ui/evaluate_dialogs.cpp,
host/src/ui/equations_dialog.cpp, host/src/ui/design_table_panel.cpp, host/src/ui/cutlist_panel.cpp.

---

**Liquid-glass v2 for Simulation + Assembly panels (host/src/ui/sim_manager.{cpp,h}, asm_panels.cpp).**
These two files still used only v1 theme-static glass (`glassBackground` / `palette.glassBg` fills +
`maxContrastTextOn(glassBg)` text) — zero v2 adaptive API — so they read as flat dark panels over the
light viewport instead of frosted backdrop-tracking glass. Split by nature:
*sim_manager.cpp is EMBEDDED* in the FeatureManager column's tab body (layout.cpp `drawLeftPane` calls
`SimManager::drawTreeBody`), so it must NOT repaint a pane background — it reuses the ONE surface
layout already painted under `panelId("host_left_glass")` + `GlassGroup::FeatureManager`. Added
`simSurf()` (mirror of feature_tree.cpp's `fmSurf`), a pane-level `PushStyleColor(ImGuiCol_Text,
simSurf().textPrimary)` at the top of `drawTreeBody` (RAII pop; only over a live backdrop) so all pane
text + the `pushDimText`-derived dims become adaptive, `simRowPill()` (`glassRowBackground` pill,
mirror of `fmRowPill`) behind every study node + leaf row in `drawStudyRow` (accent card when
selected), and a `beginCheckCard/endCheckCard` pair that replaced the 5 checklist cards' old
`pushGlassChildStyle`+`pushCardText` (theme-static dark child fill) with a transparent auto-resize
child fronted by a frosted `glassRowBackground` pill (no double-frost of the already-glass pane, no
per-card text re-pair). `drawStudyTypeBanner` migrated `glassBackground`→`glassRowBackground`. NOTE:
`glassCardText()`/`pushCardText()` are now used ONLY by the two floating popups (New Study picker +
custom-material modal) which fill with the OPAQUE `glassBg` PopupBg regardless of backdrop, so
`glassCardText` stays `maxContrastTextOn(glassBg)` (light ink) — substituting the pane's dark adaptive
ink there would land dark-on-dark = invisible (contrast trap, caught in review before shipping).
*asm_panels.cpp is 7 independent floating windows* (Insert / Parts Library / Mate / Pattern / Explode /
Interference / BOM), so each paints its OWN frosted backdrop (evaluate_dialogs.cpp recipe): `pushGlassWindow`
now pushes a transparent `ImGuiCol_WindowBg`; after each `Begin` succeeds, `paintGlassWindow("asm_<name>_glass")`
does `adaptivePanel(unique panelId, winRect, GlassGroup::Global)` → `glassPanel(dl, winRect,
GlassLevel::Regular, 12.0f, surf)` and pushes `surf.textPrimary`; `endGlassWindow()` pops it before each
`End` (incl. the 2 early-return paths in Parts Library + Pattern). To make the file's ~40
`pushText(pal.text)`/`pushText(pal.textDim)` call sites backdrop-adaptive with zero per-site edits,
`pushText` now pointer-compares its arg against the `palette.text`/`palette.textDim` role arrays and
remaps them to `surf.textPrimary`/`textSecondary` when a frosted window surface is active
(`g_asmSurf`); semantic roles (accent/status*) pass through unchanged. (Full-rect frost incl. the title
bar — title text bleeds through the translucent glass legibly, per the original evaluate_dialogs
pattern; a sibling agent later refined dialogs to frost only the content region below the title, but
the asm windows keep the simpler verified full-rect approach.) **Verify:** built green under
`flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"`; `AI_CAD_SHOT=asm`
→ `scratch/gui-overhaul-shots/asm_interference_after.png` (zoom crop confirms the Interference window
is frosted with crisp dark-on-light content text over the light model); `AI_CAD_SHOT=fea` runs the sim
path (`studyCount>0` forces `drawTreeBody`; `newStudy` sets `sel_>=0` so `drawForm`→`drawStudyChecklist`
→`beginCheckCard` all execute) to clean completion with no ImGui stack assertion — the study tree +
checklist render just below the headless left-pane fold, using the identical screenshot-proven
`host_left_glass` surface + `glassRowBackground` mechanism the visible feature-tree rows already use.
No `.h` signature change was needed (all new helpers are file-static in the .cpp; sim_manager.h's dirty
diff in the tree is a PRE-EXISTING unrelated arrowhead-triangulation change, not this glass work).
**Touched (this glass work):** host/src/ui/sim_manager.cpp, host/src/ui/asm_panels.cpp.

**2026-07-02 — GUI overhaul Commit A + glass-polish landing (T-G1).** Landed the two
sessions of verified-but-uncommitted host GUI work from the master working tree in two
scoped commits: (1) the overhaul core set — adaptive colored-glass API + AppearanceSettings
(`ui_theme.*`, `core/app_state.*`), full-bleed overlay layout support files, contrast/widget
modernization across `feature_tree.*`, `settings_modal.*`, `title_bar/toast/legend/
view_selector/shortcut_bar/icons_sdf`, renderer/overlay support (`renderer.*`,
`render/overlays.*`), scenario harness updates, plus pre-existing header diffs
(`layout.h`, `legacy_chat.h`, `property_manager.h`, `sim_manager.h`, `metrics.h`); (2) the
2026-07-02 polish set on the seven files from `docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` —
FM input fix (NoBringToFrontOnFocus removal), fully transparent columns (pills only),
columns hugging the ribbon, Copilot collapse-to-pill, PropertyManager-inside-FeatureManager,
sketch top-center DOF island + ribbon accept/deny cluster, feature readout panel, overlay
column-dodging, PM FrameBg translucency fix. **Verify:** build green (flock'd ninja,
exit 0); `AI_CAD_SHOT=m1` + `AI_CAD_SHOT=sketch` re-shot and eyeballed
(`scratch/land_verify/`). **Known open item:** the m1 scenario's reopen step reports
`rev=0 features=0 (TREE MISMATCH)` — save/reopen feature-tree restore, suspected
pre-existing (kernel-worker side, untouched by this work); filed for investigation.
Remaining NEEDS-LIVE click-tests are tracked as T-G2.

### G5 — Wave 1 verification (opus-vale, 2026-07-02)
Resumed T-G5 (claimed via `357860e`, no branch had been created). Established that Wave 1
(F1 viewport, F2 theme, F3 platform, F4 worker seam) + the G1 CMake rewire are **already
landed on master** (via `9d0d445` G1-integrate + `0c20c37` liquid-glass v2), so T-G5 is a
verify-and-close, not a from-scratch port. **Verified DoD:** `aicad_ui_viewport` builds +
links standalone (`cmake -S ui/viewport -B build/viewport && cmake --build build/viewport`,
exit 0; `grep -r host/src ui/viewport` → only comments/AGENTS.md, no `#include`); F1 Kawase
blur + `averageLuminance()` present (`renderer.h`/`viewport_impl.cpp`); zero OS `#ifdef` in
`ui/imgui/src/ui`; F4 routes doc ops through `WorkerClient` (`doc_engine.cpp` ctor
`Engine(WorkerClient&, DocMirror&)`, app_state "Engine live (WorkerClient)"). **G1 gate
smoke — blocked on a half-landed C2 commit, then proven green once patched:** clean master
does not build AI-CAD2 because `property_manager.cpp` (committed) references
`PanelField::selMulti`/`label` but the matching `ui/abstract/panel_spec.{h,cpp}` rewrite is
**uncommitted in the main checkout**. Ephemerally applying that staged panel_spec (reverted
after, not this branch's write-set) → AI-CAD2 links clean (exit 0) and
`AI_CAD_NO_FILE_DIALOGS=1 AI-CAD2 --shot scratch/smoke.png` renders the full shell (ribbon +
Model Tree + liquid-glass + Copilot, 1600×1000). **Blocker filed:** `work/questions/T-G5.md`
— recommend committing the staged `panel_spec.{cpp,h}` to unbreak every AI-CAD2 build
(team-wide, not T-G5-specific). **Gotcha:** headless `--shot` works without xvfb on this box
(offscreen GL); the "known selMulti gate break" is not a seam-file gap — it is C2's
`property_manager.cpp` having shipped without its `panel_spec` counterpart.

### T-G6 — wave 2 shell chrome verify-and-close (G2 gate) — 2026-07-03 (opus-gale)
Verify-and-close of the Wave-2 shell chrome (S1–S6 + H5 milestone + G2 gate). The prior
overhaul workflow had already ported all of S1–S6/H5/G2 to master (paragraphs above); this
task changed **zero code** (branch = `origin/master` + claim commit) and instead built
AI-CAD2 from master and eyeball-verified each DAG verify scenario against the AI hints.
**Build:** `cmake --preset linux-app` → `bin/AI-CAD2` links clean; `--selftest` OK.
**Renders (scratch/tg6-shots/, gitignored):** `ribbon` (full Model ribbon, all cmds+glyphs,
group labels), `ribbon`+`AI_CAD_TAB=1` (sketch-context ribbon Line/Rect/…/Exit Sketch,
"Sketch1"/"Fully Defined"), `explorer` (Files tab: Recent/Workspace tree), `settings`
(Settings modal General/AI/Prompts/Advanced/Experimental), `tree` (real worker-built
Base/Fillet/Shell FM tree). All show glass transparent columns w/ per-element pills,
doc-tab band inside viewport, ShopCraft branding, "Engine live (WorkerClient, rev 3)".
**S1** dock+rail (Model Tree↔Files switch) ✓; **S2** doc-tab band ✓ (multi-tab population
blocked by deadlock below); **S3** ribbon + `AI_CAD_TAB` ✓✓; **S4** status bar + toast +
palette + settings search seam ✓; **S5** atlas glyphs render (real, no fallback boxes) ✓;
**S6** explorer + settings ✓; **H5** compiles ✓ + sketch chrome/DOF solver live ✓ (2D
entity-overlay shot blocked by deadlock). **Systemic pre-existing finding:** ScenarioRunner
*step-machine* scenarios (`sketch`/`multidoc`/`m1`/`fea`/`cam`/`flow`/`drawing`/`asm`/
`activity`/`evalmass`) deadlock offscreen on a mid-loop `DocMirror::newPart(worker)`; the
shot loop ignores `--frames` for these and waits for scenario completion → `timeout` exit 124,
no PNG. Worker **one-shots** (`tree`/`equations`/`ecad`) capture fine. F4/C6 worker-seam
scope, filed in `work/questions/T-G6.md`; does not block wave-2 shell chrome. **Gate:** core/
ui-abstract/ctest/ui-imgui pass; check.py `feature_surface_audit`+`parity_audit` FAIL are the
known pre-existing reds (host-era ribbon manifest / absent SW_PARITY_SPEC). **Touched:** none
(code); docs/task/question files only.

### T-G7 — wave 3 core panels verify-and-close + C2 pmform fix (G3 gate) — 2026-07-03 (opus-gale)
Chained off task/T-G6-migration-wave2 (merge order T-G6 → T-G7). C1–C6 already ported to
master by the prior overhaul run (§§ C1–C6); this task verified they render on AI-CAD2 and
fixed the C2 PropertyManager-form path. **Verified render** (scratch/tg7-shots/): C1 `tree`
(real worker-built Base/Fillet/Shell), C3 `chat` (glass bubbles + action card + ShopCraft
branding), C4 `pick`, C5 `equations`/`assembly`/`plan`, one-shot `ecad`. **C2 FIX (real
bug):** `AI_CAD_SHOT=pmform AI_CAD_PMCMD=extrude` rendered the Model Tree, not the Extrude
form. Root cause: the pmform scenario `st.dispatch(cmd)`'d the command — which routes to
`actionBus.submit()` and *executes* it (sets status, returns) without setting `activeTool` —
so `drawInspector` (only called when `activeTool` non-empty, application.cpp:159; and host of
`pmApplyShotEnv`) never ran and the PM form never opened. Fix (2 hunks, in-lane): (1)
`ui/imgui/src/ui/scenarios.cpp` pmform sets `st.activeTool = cmd` (opens the data-driven form
instead of executing); (2) `ui/abstract/panel_spec.cpp` `panelFormForCommand` now falls back to
a bare spec NAME (explicit ribbon-command mappings still win), so the DoD's literal
`AI_CAD_PMCMD=extrude` resolves the same form `extrude_boss` does. Reverified: the full Extrude
PM form renders inside the FeatureManager tab (From/Direction1/Direction2/Thin Feature/Options,
selector wells, dropdowns, depth+draft steppers, checkboxes, green✓/red✗ accept-deny), no
separate PM tab — per AI hint. **G3-gate BLOCKER:** the "all 21 scenarios" DoD is gated on
issue #7 — 10/21 are ScenarioRunner step machines that deadlock offscreen on mid-loop
`DocMirror::newPart(worker)` (F4/C6 scope). ~11/21 (one-shots + static) capture fine. G3-gate
re-scope decision requested from the foreman in work/questions/T-G7.md. **Touched:**
ui/imgui/src/ui/scenarios.cpp, ui/abstract/panel_spec.cpp (+docs/task/question files).

## 2026-07-06 — sim perf root cause: libaicad was built -O0 (FEA ~16x slow)

**Symptom:** simple FEA simulations "extremely long" on Linux despite powerful hardware.
**Root cause:** the core library (`libaicad.so`) was compiled **unoptimized (-O0)**. All
`core/build-*` dirs had an EMPTY `CMAKE_BUILD_TYPE`; the `-O3 -DNDEBUG` flags live only in
`CMAKE_CXX_FLAGS_RELEASE`, applied solely for a Release build type. `scripts/gate.sh`
configured `core/build-linux` without `-DCMAKE_BUILD_TYPE=Release`, and its
`cmake --build ... --config Release` / `ctest -C Release` are **no-ops on single-config
Ninja** — so the gate produced an -O0 core while printing "PASS (Release)".
**Measured:** FEA/solver ctests (sim+modal+tet10+thermal) 34.1s (-O0) -> 2.08s (-O3) = **~16x**
(modal 68x, tet10 13.6x, sim 13.3x). The Eigen assembly/solve hot loops are extremely
-O0-sensitive (lost inlining/vectorization).
**Fix:** (1) `scripts/gate.sh` now configures with `-DCMAKE_BUILD_TYPE=Release`. (2) Reconfigured
+ rebuilt the in-use dirs `core/build-linux` AND `core/build-occt-linux` (the GUI's in-process
`.so` via `cpp_core.cpp` -> baked `AICAD_DEFAULT_LIBAICAD`) at Release. **Note:** the stale
`services/dist/AI-CAD/libaicad.so` (2026-07-02, -O0, predates `sim_run_static`) still shadows
`core/build-linux` in `core_bridge` resolution order — refresh or remove it so tool/worker
paths don't load stale -O0 code. Secondary levers (not yet done): single-threaded Netgen
mesher, no mesh caching (re-meshes every solve), double STEP parse, tet10-by-default.

---

**2026-07-10 — CEF daemon is now the sole owner of the warm profile (Phase 2b).**
The interactive CEF panel (`cef_web_view.cpp`, app process) and the headless
`cef_daemon` (sibling process) both opened a `CefRequestContext` on the SAME
`_warm_profile` dir — two Chromium processes writing one cookie/leveldb store,
which corrupts it, and a login in the panel did not carry into headless fetch
(OSCrypt is per-process, so raw dir-sharing can't move a session). Fix: the panel
now uses its OWN persistent dir `panel_profile_dir()` = `_warm_profile`→`_panel_profile`
(env `AI_CAD_CEF_PANEL_PROFILE_DIR`); the daemon stays sole owner of `_warm_profile`.
Login carry rides the existing `exportSession → browser_session_import` seam: the
worker handler (`engine_oracle/embed_rpc.py`) now also pushes the exported jar to a
new daemon `import_session` RPC (`cef_client.import_session` → `headless_import_cookies`
in `cef_headless.cpp`, `CefCookieManager::SetCookie` + `FlushStore` into `_warm_profile`),
off-thread so a cold daemon spawn never stalls the login chip. Files: `cef_headless.{h,cpp}`,
`cef_daemon.cpp`, `cef_web_view.cpp`, `services/browser/cef_client.py`, `embed_rpc.py`.
Verified: CEF build (`ninja -C host/build-cef AI-CAD`) links; `cef_client` ping + a
login-carry smoke (inject cookie via `import_session`, `fetch` a local echo server,
cookie carried) PASS; embed pytest 39 passed/1 skip. **Deferred fast-follow** (only if
in-panel challenge rates warrant it): bidirectional seed — on panel open, pull the
daemon's `_warm_profile` cookies (new daemon `export_session` + host-side `SetCookie`,
spawn=False) so the panel benefits from the daemon's independently-accumulated
Google/CF/reCAPTCHA trust, not just its own. Push-only today means the two persistent
jars warm in parallel and don't share third-party trust cookies. Pre-existing unrelated
gate debt: `scripts/check.py` `feature_surface_audit` fails on Sketch3D/Reverse-Eng
ribbon handlers (untouched here).

## 2026-07-10 — T-SIM5: OpenFOAM accurate-CFD live path (branch task/T-SIM5-openfoam)
run_cfd_accurate is now production-real on Linux: new `LinuxRunner` in
engine_oracle/sim/foam_runner.py (sourced-env / AICAD_OPENFOAM / system / user-space
conda detection, strict subprocess exec — GPL isolation, license_debt.md row 18);
tools/provision_openfoam.py gained a no-sudo conda-forge install path (installed live:
OpenFOAM v2412); foam_cht.py grew the T-FIN19 multi-solid seam
(build_cht_case(solids=[{name, stl_path, kappa_w_mk, power_w}]) — per-region cellZones,
heat sources, coupled BCs; openfoam.py passes thermal["solids"], foam_post aggregates
per-region temperatures). Live-verified: foam_validation sphere/cube Cd PASS in-band,
2-solid board+chip CHT end-to-end. sim_audit.py gained an opt-in live validation leg
(AICAD_SIM_AUDIT_LIVE_FOAM=1). Gotchas: OpenFOAM case files must stay ASCII (`_w` writes
ascii); conda env bashrc lives at <env>/etc/bashrc and MUST be sourced (bare exe fails on
etc/controlDict lookup); mcp test suite legs needing libaicad were skipped here (worktree
has no staged dist). Deferred: MCP-level exposure of the CHT variant (T-FIN19 will wire
board solids + heat sources into thermal["solids"]).
