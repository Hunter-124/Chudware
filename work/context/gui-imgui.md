# GUI (ImGui desktop rewrite + viewport + framework-agnostic seam)

> ⚠️ **STALE — the `ui/imgui` / `ui/abstract` (AI-CAD2) target below is RETIRED.** Per
> `build.sh`, `host/` is the single cross-platform GUI target; `ui/imgui` no longer exists on
> disk (verified 2026-07-10). Do NOT send agents to `ui/imgui`. For any real GUI change use the
> **gui-orchestrator** skill (`.claude/skills/gui-orchestrator/SKILL.md`) and its dispatch
> table. Real entry points now:
> - Title bar / OS-border chips + workspace tabs: `host/src/ui/title_bar.cpp` (`drawTitleBar`,
>   `drawChipAt`; procedural tab glyphs in `drawTbIcon`/`TbIcon`; workspace tabs also blit
>   raster atlas tiles via a `rasterCell` field on `kTabs` — `ui::getIcon(cell,32)` +
>   `AddImage`, glyph fallback).
> - Ribbon tabs/buttons/groups: `host/src/ui/widgets/ribbon.cpp` (`drawRibbon`); command table
>   `host/src/ui/ribbon_def.cpp`.
> - Theme / liquid-glass helpers: `host/src/ui_theme.{h,cpp}` — `glassRowBackground`,
>   `coloredRowWash`, `glassSelectionGlow`, `glassBackground` (title bar + ribbon share these
>   for the gradient-glass + accent-wash look).
> - Icons: procedural line-art `assets/make_icons.py`; engine-rendered raster tiles
>   `scripts/icon_pipeline.py` (RECIPES) + `scripts/cam_toolpath_icons.py` (toolpaths) +
>   `scripts/fea_result_icons.py` (deformed-FEA tile); atlas `assets/icons/atlas*.{png,json}`
>   (`raster_only`); C++ loader `host/src/ui/icons.cpp` (`getIcon`). Ribbon/tree/tab icons all
>   resolve by name, so replacing a glyph cell with a raster tile under the same name needs no
>   C++ change.
> The rest of this file describes the abandoned migration and is kept only for historical DAG
> context.

Scope: ui/imgui/**, ui/abstract/**, ui/viewport/** (modular GUI rewrite: framework-agnostic view-model/commands/panel-specs seam, ImGui desktop app, shared OpenGL pipeline scaffold)
Contracts: docs/GUI_MIGRATION_DAG.md, docs/HANDOFF_GUI_OVERHAUL.md, docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md

## Entry points
- ui/abstract/panel_spec.h:44-46 `findPanelSpec`, `panelFormForCommand`, `allPanelSpecs` — panel metadata registry (feature type → form fields)
- ui/abstract/variant.h:15-21 `ModelDiff` — command result struct (ok/error/newRevision/invalidateDocId/payload)
- ui/abstract/command_dispatcher.h:10-15 `ICommandDispatcher::dispatch` — stable routing interface (cmdId + params → ModelDiff)
- services/worker/worker_command_dispatcher.h:11-25 `WorkerCommandDispatcher` — dispatch implementation (routes to WorkerClient + DocMirror)
- ui/imgui/src/main.cpp:46 `main()` — GLFW + ImGui + OpenGL bootstrap entry point
- ui/imgui/src/app/application.h:11-24 `Application` — frame state + viewport management (init/frame/shutdown)
- ui/imgui/src/viewport/scene.h:12-50 `Scene` — OpenGL 3.3 renderer (AICM loader, orbit camera, picking, selection)
- ui/imgui/src/ui/agent_inspector_panel.h / watch_grid_panel.h / agent_settings_panel.h — T-W17 advisor-transparency observer panels (glass scaffold, view-models in ui/abstract/agent_inspector_view.h · watch_grid_view.h · agent_settings_view.h). Replay from run-dir fixtures (docs/contracts/fixtures/runs/, docs/contracts/fixtures/routing/) with NO live backend. Settings writes route through the services/agent/routing.py CLI (get/set), never raw YAML.

## How it connects
ui/abstract is the framework-agnostic seam decoupling panel specs, command dispatch, and variant results from any GUI framework. The migration ports host/ (~46.6k LOC, source-of-truth panels) wave-by-wave into ui/imgui (ImGui shell + app logic), ui/viewport (shared OpenGL pipeline scaffold), platform/ (dialogs/paths/DPI), and services/worker (command routing to libaicad + Python oracle). ui/imgui/src/app/application.cpp calls WorkerCommandDispatcher::dispatch() to execute commands; the ModelDiff result flows back to AppState. ui/viewport/Scene renders the tessellated AICM mesh. After the port completes (G0–G9 DAG in docs/GUI_MIGRATION_DAG.md), host/ will be deleted per host/DEPRECATED.md criteria.

## Debugging playbook
- Build: `cmake -S ui/imgui -B ui/imgui/build` (CMakeLists.txt regenerates ui/abstract + ui/viewport + platform subdirs)
- Test/verify: `ui/imgui/test/test_command_registry.cpp` (command palette smoke test)
- Run: `AI_CAD_NO_FILE_DIALOGS=1 ./ui/imgui/build/bin/AI-CAD2 --shot /tmp/screenshot.png` (headless screenshot for smoke)
- T-W17 shots: `AI_CAD_SHOT=agent_inspector|agent_inspector_empty|watch_grid|agent_settings|agent_settings_error` seed from committed fixtures (findFixturesDir walks up to docs/contracts/fixtures); build+run under build/linux-app (`cmake --preset linux-app`).
- **IN-PROGRESS:** ui/abstract/panel_spec.cpp/.h have active modifications (838 insertions, 53 deletions per `git diff --stat`); other agents should read but not edit these files during in-flight work
- Gotcha: ui/viewport/CMakeLists.txt does not yet exist (F1/G1 deliverables per GUI_MIGRATION_DAG.md L40–50); viewport currently static-linked as object files
- Gotcha: host/src/ui/ remains source-of-truth for all panel specs; ui/imgui/src has only 4/44 PM forms so far (G5 onward per DAG)

## Graphify starters
- graphify query "ui/abstract panel_spec command dispatcher variant ModelDiff"
- graphify explain "ICommandDispatcher"
- graphify path "ui/abstract/command_dispatcher.h" "services/worker/worker_command_dispatcher.h"

## Related tasks/docs
- work/tasks: grep `T-G` (GUI migration workstream; T-G1 done, T-G5..T-G9 swarm-integrator migration-wave tasks)
- docs/GUI_MIGRATION_DAG.md (node-level leaf plan with F1–F4/G0–G9 phases)
- docs/HANDOFF_GUI_OVERHAUL.md (locked UI decisions: full-bleed viewport, transparent glass columns, adaptive overlay)
- docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md (polish + branding plan)
- host/DEPRECATED.md (sunset criteria for host/)
