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
