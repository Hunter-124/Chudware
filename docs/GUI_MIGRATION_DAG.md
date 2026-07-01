# GUI Migration DAG — monolithic node spec

Single source of truth for GUI-unification swarm leaf prompts. Each leaf reads **only**
its node section plus `docs/GLASS_DESIGN_SPEC.md`. Never edit this file unless you are
the integrator (G0/G1/G2/G3/G4/G5).

**Ground truth inventory (2026-07-01):**

| Tree | LOC / files | Notes |
|------|-------------|-------|
| `host/src/ui/` | ~46.6k / 90 | Source-of-truth panels + chrome |
| `host/src/render/` + `renderer.*` | ~3.1k | GL viewport pipeline |
| `host/src/core/` | ~4.8k | AppState, DocMirror, jobs, sim |
| `host/src/ui_theme.*` | ~850 | Glass + palette |
| `ui/imgui/src/` | ~4k partial | Shell builds; 4/44 PM forms |
| `ui/viewport/` | scaffold | No `renderer.cpp`, no CMakeLists |
| `platform/` | scaffold | AGENTS.md only |
| `services/worker/` | partial | Router + cpp_core; no DocMirror |

**Headless verify:** `AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=<scenario> ./host/build-linux/bin/AI-CAD --ui-screenshot scratch/gui_baseline/<scenario>.png` (host baselines in G0). New shell: `AI-CAD2 --shot <path>` after C6.

**Swarm rules:** non-isolated main tree, no git, no build if parallel node shares build graph, append one paragraph to `docs/REFACTOR_LOG.md` on completion.

---

## G0 — integrator prep

| Field | Value |
|-------|-------|
| **Model** | integrator (parent) |
| **Write-set** | `.gitignore`, `docs/GUI_MIGRATION_DAG.md`, `docs/GLASS_DESIGN_SPEC.md`, `docs/REFACTOR_LOG.md`, `ui/viewport/**` (scaffold commit), `host/CMakeLists.txt` (MSVC `/FI`), incidental WIP in dirty tree |
| **Depends on** | — |
| **Done** | Local baseline commit; host PNGs in `scratch/gui_baseline/`; both docs written |
| **Verify** | `git status` clean (except ignored `ui/imgui/build/`); `ls scratch/gui_baseline/*.png` |

---

## Wave 1 — foundation (parallel leaves)

### F1 — viewport library

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/viewport/**` (sole owner) |
| **Source (port from)** | `host/src/renderer.cpp`, `host/src/renderer.h`, `host/src/render/id_pass.*`, `host/src/render/overlays.*`, `host/src/render/aicm_loader.*`, `host/src/mesh.*`, `host/src/vec_math.h`, `host/src/ui/camera_anim.*`; camera math currently duplicated in `host/src/ui/sketch_mode.cpp` (`worldToScreen`, `screenRay`) → public API in `ui/viewport/renderer.h` |
| **Target** | `ui/viewport/renderer.cpp`, `ui/viewport/CMakeLists.txt` (static lib `aicad_ui_viewport`), fix `ui/viewport/render/id_pass.*` + `overlays.*` to include local `renderer.h` not `host/`; implement `ui/abstract/viewport.h` (`IViewport`); **NEW:** Kawase-blur backdrop FBO + `averageLuminance(ImRect)` |
| **Depends on** | G0 |
| **Done** | `aicad_ui_viewport` compiles standalone; no `#include` into `host/`; blur + luminance hooks exported |
| **Verify** | `cmake -S ui/viewport -B build/viewport && cmake --build build/viewport` (after CMakeLists lands); `grep -r 'host/src' ui/viewport` → empty |

### F2 — theme / liquid glass

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/theme/**` |
| **Source** | `host/src/ui_theme.h`, `host/src/ui_theme.cpp` (`glassBackground`, `pushGlassWindow/Tooltip/Child`, `glassRowBackground`, `glassRowAtCursor`, palette roles) |
| **Target** | Expand `ui/imgui/src/theme/tokens.h` (4 material levels, 8pt grid, SF-like type scale); `theme.h`/`theme.cpp`: `glassPanel()`, `adaptiveSurface()`, row pills, vibrancy text colors per `docs/GLASS_DESIGN_SPEC.md` |
| **Depends on** | G0 (F1 blur texture ID is stub OK until G1) |
| **Done** | All theme helpers compile; no literal colors outside `tokens.h`/`theme.*` |
| **Verify** | `python3 scripts/check.py gui_lint theme` |

### F3 — platform layer

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `platform/**`, `ui/imgui/src/platform/*` |
| **Source** | `host/src/ui/file_dialog.*`, `host/src/win_chrome.*`, `app/src/platform/file_dialog.*`, Linux GLFW paths from `host/src/app.cpp` |
| **Target** | `platform/dialogs.h` + `platform/dialogs_linux.cpp` + `platform/dialogs_win32.cpp`; `platform/window.h`, `platform/paths.h`, `platform/dpi.h`; thin wrappers in `ui/imgui/src/platform/file_dialog.*` |
| **Depends on** | G0 |
| **Done** | Zero OS `#ifdef` outside `platform/`; file open/save works headless stub when `AI_CAD_NO_FILE_DIALOGS=1` |
| **Verify** | `grep -r '#ifdef _WIN32' ui/imgui/src/ui` → empty (panels); build `ui/imgui` after G1 |

### F4 — worker / state seam

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `services/worker/**`, `ui/imgui/src/app/**` |
| **Source** | `host/src/core/app_state.*`, `host/src/core/doc_mirror.*`, `host/src/core/doc_tabs.*`, `host/src/core/jobs.*`, `host/src/core/sim_client.*`, `host/src/core/async_rpc.*`, `host/src/core/selection.*`, `host/src/worker_client.h` |
| **Target** | Rich `AppState` + `DocMirror` + doc-tab sessions + `jobs` + `sim_client` in `services/worker/`; `ui/imgui/src/app/app_state.*` binds to `WorkerClient` (libaicad + Python-oracle fallback); drop direct-only `cpp_core` shortcut |
| **Depends on** | G0 |
| **Done** | AI-CAD2 routes doc ops through `WorkerClient`; parity with host tree/m1 smoke |
| **Verify** | Integrator runs `AI_CAD_SHOT=tree` on AI-CAD2 after G1; `bash scripts/gate.sh` pytest SKIP ok |

### G1 — integrator: CMake rewire

| Field | Value |
|-------|-------|
| **Model** | integrator |
| **Write-set** | root `CMakeLists.txt`, `ui/imgui/CMakeLists.txt`, `ui/viewport/CMakeLists.txt`, `scripts/gate.sh` |
| **Depends on** | F1, F2, F3, F4 |
| **Done** | `ui/imgui` owns FetchContent + glad (no `host/build/_deps`); gate builds AI-CAD2 + `--shot` smoke; wave compiles; local commit |
| **Verify** | `cmake -S ui/imgui -B ui/imgui/build && cmake --build ui/imgui/build`; `AI_CAD_NO_FILE_DIALOGS=1 ./ui/imgui/build/bin/AI-CAD2 --shot scratch/smoke.png`; `bash scripts/gate.sh` |

---

## Wave 2 — shell chrome (parallel leaves; H5 starts)

### S1 — layout / dock / rail

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `ui/imgui/src/ui/layout.cpp`, `ui/imgui/src/ui/layout.h` (create), `ui/imgui/src/ui/left_dock.cpp` (dock integration only) |
| **Source** | `host/src/ui/layout.cpp` (415 LOC), `host/src/ui/layout.h` |
| **Target** | One file per element; glass row pills on tab rail per spec |
| **Depends on** | G1 |
| **Done** | Default dock layout matches host; transparent `DockingEmptyBg` |
| **Verify** | `AI_CAD_SHOT=explorer` screenshot |

### S2 — title bar + doc tabs

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `ui/imgui/src/ui/title_bar.cpp`, `ui/imgui/src/ui/title_bar.h`, `ui/imgui/src/ui/doc_tabs.cpp`, `ui/imgui/src/ui/doc_tabs.h` (create) |
| **Source** | `host/src/ui/title_bar.*`, `host/src/ui/doc_tabs.*` |
| **Target** | Glass tab groups; doc-tab band rules per GLASS_DESIGN_SPEC |
| **Depends on** | G1, F2 |
| **Done** | Multi-doc strip renders inside viewport band |
| **Verify** | `AI_CAD_SHOT=multidoc` |

### S3 — ribbon + context toolbar

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/ui/context_toolbar.cpp`, `ui/imgui/src/ui/ribbon.cpp`, `ui/imgui/src/ui/ribbon.h`, `ui/imgui/src/ui/ribbon_def.cpp`, `ui/imgui/src/ui/ribbon_def.h` (create) |
| **Source** | `host/src/ui/widgets/ribbon.*`, `host/src/ui/ribbon_def.*`, merge with existing `context_toolbar.cpp` |
| **Target** | ~2,052 LOC ribbon system restyled |
| **Depends on** | G1, F2, S5 (icons) |
| **Done** | All ribbon tabs render; `AI_CAD_TAB` env honored |
| **Verify** | `AI_CAD_SHOT=ribbon`; `AI_CAD_SHOT=ribbon AI_CAD_TAB=1` (sketch tab) |

### S4 — status / toast / shortcut / palette

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `ui/imgui/src/ui/status_bar.cpp`, `ui/imgui/src/ui/toast.cpp`, `ui/imgui/src/ui/toast.h`, `ui/imgui/src/ui/shortcut_bar.cpp`, `ui/imgui/src/ui/shortcut_bar.h`, `ui/imgui/src/ui/command_palette.cpp`, `ui/imgui/src/ui/command_search.cpp`, `ui/imgui/src/ui/command_search.h` (create as needed) |
| **Source** | `host/src/ui/status_bar.*`, `host/src/ui/toast.*`, `host/src/ui/shortcut_bar.*`, `host/src/ui/command_search.*`, existing `command_palette.cpp` |
| **Depends on** | G1, F2 |
| **Done** | Status bar + toast + Ctrl+K palette |
| **Verify** | `AI_CAD_SHOT=settings` |

### S5 — icons atlas + cursors

| Field | Value |
|-------|-------|
| **Model** | `gemini-3.5-flash` |
| **Write-set** | `ui/imgui/src/theme/icons.cpp`, `ui/imgui/src/theme/icons.h`, `ui/imgui/src/ui/cursors.cpp`, `ui/imgui/src/ui/cursors.h`, `host/assets/icons_atlas.png` → copy to `ui/imgui/assets/` (create) |
| **Source** | `host/src/ui/icons.*`, `host/src/ui/cursors.*`, `host/assets/make_icons.py` output |
| **Depends on** | G1 |
| **Done** | Atlas loads; FM/ribbon glyphs match host manifest |
| **Verify** | `python3 scripts/check.py gui_lint icons` |

### S6 — files / settings / profiler

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `ui/imgui/src/ui/file_explorer.cpp`, `ui/imgui/src/ui/file_explorer.h`, `ui/imgui/src/ui/settings_modal.cpp`, `ui/imgui/src/ui/settings_modal.h`, `ui/imgui/src/ui/profiler_window.cpp`, `ui/imgui/src/ui/profiler_window.h`, `ui/imgui/src/platform/file_dialog.cpp` (wire to F3) |
| **Source** | `host/src/ui/file_explorer.*`, `host/src/ui/settings_modal.*`, `host/src/ui/profiler_window.*`, `host/src/ui/file_dialog.*` |
| **Depends on** | G1, F3 |
| **Done** | Settings modal + explorer tab |
| **Verify** | `AI_CAD_SHOT=settings`; `AI_CAD_SHOT=explorer` |

### H5 — sketch mode (long pole; spans waves 2–4)

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/sketch_mode.cpp`, `sketch_tools.cpp`, `sketch_constraints.cpp`, `sketch_overlay.cpp`, `sketch_edit.cpp` + headers (create split) |
| **Source** | `host/src/ui/sketch_mode.cpp` (9,305 LOC), `host/src/ui/sketch_mode.h` |
| **Target** | Split files; use F1 `worldToScreen`/`screenRay`; no host camera coupling |
| **Depends on** | G1, F1 (camera API) |
| **Done (wave 2 milestone)** | Compiles + sketch overlay draws; full done-gate at G4 |
| **Verify (interim)** | `AI_CAD_SHOT=sketch` layout parity; **final:** solver op-batch diff vs host |

---

## G2 — integrator: shell wave

| Field | Value |
|-------|-------|
| **Model** | integrator |
| **Write-set** | `ui/imgui/src/app/application.cpp` (wire new panels), `ui/imgui/CMakeLists.txt` (source list), bounce-fixes only |
| **Depends on** | S1–S6, H5 (compile milestone) |
| **Done** | AI-CAD2 shell chrome screenshots review vs `scratch/gui_baseline/{explorer,ribbon,multidoc,settings}.png`; local commit |
| **Verify** | `bash scripts/gate.sh`; integrator screenshot diff review |

---

## Wave 3 — core panels

### C1 — feature tree

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/ui/feature_tree.cpp`, `ui/imgui/src/ui/feature_tree.h` |
| **Source** | `host/src/ui/feature_tree.*` (3,050 LOC) |
| **Depends on** | G2, S5 |
| **Done** | FM toolbar undo/redo hit-test correct; row pills |
| **Verify** | `AI_CAD_SHOT=tree` |

### C2 — property manager + 44 forms

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/property_manager.cpp`, `ui/imgui/src/ui/property_manager.h`, `ui/imgui/src/ui/pm_forms.cpp`, `ui/abstract/panel_spec.cpp`, `ui/abstract/panel_spec.h` (**sole append owner this round**) |
| **Source** | `host/src/ui/property_manager.*`, `host/src/ui/pm_forms.cpp` (5,661 LOC total) |
| **Depends on** | G2 |
| **Done** | All 44 `panel_spec` forms; data-driven inspector; `AI_CAD_PMCMD` / `AI_CAD_PMSET` |
| **Verify** | `AI_CAD_SHOT=pmform AI_CAD_PMCMD=extrude` |

### C3 — copilot + activity + console

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/ui/agent_panel.cpp`, `ui/imgui/src/ui/activity_panel.cpp`, `ui/imgui/src/ui/activity_panel.h`, `ui/imgui/src/ui/console_panel.cpp`, `ui/imgui/src/ui/console_panel.h`, `ui/imgui/src/ui/model_registry.h` |
| **Source** | `host/src/ui/legacy_chat.*`, `host/src/ui/activity_panel.*`, `host/src/ui/console_panel.*`, `host/src/ui/model_registry.h` |
| **Depends on** | G2, F2 |
| **Done** | Sessions UX, composer prelude, activity stream |
| **Verify** | `AI_CAD_SHOT=chat`; `AI_CAD_SHOT=activity` |

### C4 — viewport panel + HUD + tools

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/viewport.cpp`, `ui/imgui/src/ui/viewport_panel.cpp`, `ui/imgui/src/ui/viewport_panel.h`, `ui/imgui/src/ui/view_selector.cpp`, `ui/imgui/src/ui/section_tool.cpp`, `ui/imgui/src/ui/legend.cpp`, `ui/imgui/src/ui/mouse_gestures.cpp` + headers |
| **Source** | `host/src/ui/viewport_panel.*`, `view_selector.*`, `section_tool.*`, `legend.*`, `mouse_gestures.*`, `camera_anim.*` |
| **Depends on** | G2, F1 |
| **Done** | HUD pills top-right, idle fade, view cube, section plane |
| **Verify** | `AI_CAD_SHOT=pick`; manual HUD hover fade check |

### C5 — asm / config / tables / equations

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/ui/asm_panels.*`, `config_panel.*`, `design_table_panel.*`, `equations_dialog.*`, `cutlist_panel.*`, `appearance_common.*`, `evaluate_dialogs.*` |
| **Source** | matching `host/src/ui/*` (3,987 LOC) |
| **Depends on** | G2 |
| **Done** | Equations editor + evaluate dialogs |
| **Verify** | `AI_CAD_SHOT=equations`; `AI_CAD_SHOT=evalmass` |

### C6 — scenarios + screenshot harness

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `ui/imgui/src/ui/scenarios.cpp`, `ui/imgui/src/ui/scenarios.h`, `ui/imgui/src/main.cpp`, `ui/imgui/src/app/application.cpp` (`AI_CAD_SHOT` env) |
| **Source** | `host/src/ui/scenarios.*`, `host/src/app.cpp` shot wiring |
| **Depends on** | G2, C1–C5 (scenarios reference panels) |
| **Done** | All 21 `AI_CAD_SHOT` values work on AI-CAD2 |
| **Verify** | Loop all scenarios → `scratch/gui_baseline2/<s>.png` |

---

## G3 — integrator: panels wave

| Field | Value |
|-------|-------|
| **Model** | integrator |
| **Depends on** | C1–C6 |
| **Done** | Full panel screenshot battery vs baselines; local commit |
| **Verify** | `bash scripts/gate.sh`; compare `scratch/gui_baseline/*.png` vs AI-CAD2 shots |

---

## Wave 4 — hard subsystems

### H1 — CAM

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/ui/cam/cam_panel.*`, `cam_mirror.*` |
| **Source** | `host/src/ui/cam/*` (718 LOC) |
| **Depends on** | G3 |
| **Done** | G-code byte-diff vs host for all 8 post dialects on same job |
| **Verify** | `AI_CAD_SHOT=cam`; `tools/` CAM diff script (integrator) |

### H2 — ECAD

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/ecad/ecad_panel.*`, `ecad_canvas.*`, `ecad_mirror.*` |
| **Source** | `host/src/ui/ecad/*` (2,501 LOC) |
| **Depends on** | G3 |
| **Done** | Netlist JSON diff + ERC identical |
| **Verify** | `AI_CAD_SHOT=ecad` |

### H3 — drawing sheets

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/drawing_panel.*` |
| **Source** | `host/src/ui/drawing_panel.*` (2,116 LOC) |
| **Depends on** | G3 |
| **Done** | DXF/PDF export byte-diff vs host |
| **Verify** | `AI_CAD_SHOT=drawing` |

### H4 — simulation

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | `ui/imgui/src/ui/sim_manager.*` |
| **Source** | `host/src/ui/sim_manager.*` (3,831 LOC) |
| **Depends on** | G3, F4 (`sim_client`) |
| **Done** | Study JSON + result-summary diff |
| **Verify** | `AI_CAD_SHOT=fea`; `AI_CAD_SHOT=flow` (allow 240s timeout) |

### H5 — sketch (completion)

| Field | Value |
|-------|-------|
| **Model** | `glm-5.2-high` |
| **Write-set** | (continues) `ui/imgui/src/ui/sketch_*.cpp` |
| **Depends on** | G3, F1 |
| **Done** | Sketch scenario + solver op-batch diff vs host |
| **Verify** | `AI_CAD_SHOT=sketch`; `AI_CAD_SHOT=asm` |

---

## G4 — integrator: domain wave

| Field | Value |
|-------|-------|
| **Model** | integrator |
| **Depends on** | H1–H4, H5 complete |
| **Done** | Domain diffs green; `AI_CAD_SHOT=asm` screenshot; local commit |
| **Verify** | `bash scripts/gate.sh`; G-code/netlist/DXF/study diff suite |

---

## Wave 5 — polish + parity

### P1 — adaptive-glass sweep

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5` |
| **Write-set** | `ui/imgui/src/theme/**`, panel files flagged in `docs/GLASS_DESIGN_SPEC.md` § shortcomings |
| **Depends on** | G4 |
| **Done** | Every over-viewport surface uses `glassPanel()`/`adaptiveSurface()`; shortcomings list cleared |
| **Verify** | `python3 scripts/check.py gui_lint theme`; integrator visual review |

### P2 — full screenshot battery

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` + integrator review |
| **Write-set** | `scratch/gui_baseline2/` (output only, gitignored) |
| **Depends on** | G4, C6 |
| **Done** | All 21 scenarios on both binaries; integrator signs off each pair |
| **Verify** | Side-by-side diff script or manual review checklist |

### G5 — cutover + gate

| Field | Value |
|-------|-------|
| **Model** | integrator |
| **Write-set** | `scripts/check.py`, `scripts/feature_surface_audit.py`, `scripts/parity_audit.py` (retarget paths to `ui/imgui` + `ui/abstract`), `AGENTS.md` (host EOL note) |
| **Depends on** | P1, P2 |
| **Done** | `bash scripts/gate.sh` green; audits scan new tree; local commit |
| **Verify** | `bash scripts/gate.sh` |

---

## Wave 6 — Windows (host deletion deferred)

### X1 — Windows preset + docs

| Field | Value |
|-------|-------|
| **Model** | `composer-2.5-fast` |
| **Write-set** | `CMakePresets.json`, `docs/BUILD_WINDOWS.md`, `ui/imgui/CMakeLists.txt` (MSVC `/FI` parity with host) |
| **Depends on** | G5 |
| **Done** | One-command VS build documented; friend verifies on Windows |
| **Verify** | Friend reports AI-CAD2 runs; **then** integrator may delete `host/` (NOT before) |

---

## AI_CAD_SHOT scenario index

| Scenario | Primary node | Notes |
|----------|--------------|-------|
| `settings` | S4, S6 | Settings modal |
| `chat` | C3 | Copilot pane |
| `assembly` | C5 | Assembly workspace chrome |
| `plan` | C3 | Plan mode (light) |
| `pick` | C4 | ID-buffer pick + highlight |
| `ribbon` | S3 | `AI_CAD_TAB` selects tab |
| `tree` | C1 | FM doc smoke |
| `explorer` | S6 | File explorer tab |
| `equations` | C5 | Equations dialog |
| `m1` | F4, C1 | Integration scenario |
| `multidoc` | S2, F4 | Multi-doc sessions |
| `ecad` | H2 | PCB editor |
| `sketch` | H5 | Sketch mode |
| `fea` | H4 | Static study |
| `cam` | H1 | CAM panel (frame 60) |
| `flow` | H4 | CFD external flow (~76s) |
| `drawing` | H3 | Engineering drawing |
| `evalmass` | C5 | Mass properties |
| `activity` | C3 | Agent activity panel |
| `asm` | C5, H5 | Assembly mate flow |
| `pmform` | C2 | `AI_CAD_PMCMD`, `AI_CAD_PMSET` |
