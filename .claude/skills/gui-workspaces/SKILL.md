---
name: gui-workspaces
description: AI-CAD workspace-tab specialist — Parts Library, Assembly panels, CAM, ECAD, PLM, Reverse-Engineering, AI/Activity workspace, Drawing, Simulation manager, plus doc tabs, file explorer, console and status bar. Use for any task about one of these workspace tabs or auxiliary panels. Workers dispatched by gui-orchestrator read this file first, then work only in their own workspace's files.
---

# GUI Workspaces Specialist

Scope: the tabbed workspaces and auxiliary panels around the modeling view. **Work only in
your assigned workspace's files** — e.g. an icon-generation task never needs the Parts Library
section below, and vice versa. Shared plumbing (app.cpp dispatch, AppState seams) is
integrator-owned.

The app has 8 workspaces cycled by the `AI_CAD_SHOT=workspaces` scenario; each is drawn from
the frame loop in `host/src/app.cpp`.

## Per-workspace file map

| Workspace | Files | Notes |
|---|---|---|
| **Parts Library** | `host/src/ui/parts/parts_workspace.cpp/.h`, `parts_browser_panel.cpp`, `parts_filters.cpp`, `parts_results.cpp`, `parts_taxonomy_pane.cpp`, `parts_vendors.cpp`, `parts_widgets.cpp/.h`, `parts_internal.h` | Vendor search, taxonomy tree, thumbnails, streaming results. Data contracts: `contracts/parts.md`, `contracts/sourcing*.md` |
| **Assembly** | `host/src/ui/asm_panels.cpp/.h` | Mates, component insertion, BOM, exploded view |
| **CAM** | `host/src/ui/cam/cam_panel.cpp/.h`, `cam_mirror.cpp/.h` | Job tree, setups/ops/tools, simulation; mirror syncs engine CAM state |
| **ECAD** | `host/src/ui/ecad/ecad_panel.cpp/.h`, `ecad_canvas.cpp/.h`, `ecad_mirror.cpp/.h` | Schematic/PCB 2D canvas editor; schemas in `contracts/ecad/` |
| **PLM** | `host/src/ui/plm/plm_workspace.cpp`, `plm_pages.cpp`, `plm_hub.h` | Versioning, releases, document control; `contracts/plm.md` |
| **Reverse Eng.** | `host/src/ui/reveng/reveng_workspace.cpp`, `webcam_capture.cpp`, `dim_review.cpp` | Photo capture, dimension confirm, reconstruction; `contracts/reveng.md`; splat view lives in `host/src/render/` (gui-viewport) |
| **AI / Activity** | `host/src/ui/ai/ai_workspace.cpp/.h`, `host/src/ui/activity_panel.cpp` | Agent run tree, role-prompt editor, live progress; events per `contracts/agent_events.md` |
| **Drawing** | `host/src/ui/drawing_panel.cpp` | Sheet layout, views, DXF export |
| **Simulation** | `host/src/ui/sim_manager.cpp`, `host/src/ui/evaluate_dialogs.cpp` | Study tree, fixtures/loads, results; Evaluate dialogs (measure, mass props, …) |

## Auxiliary panels

`host/src/ui/doc_tabs.cpp` (multi-document tab strip) · `file_explorer.cpp` (workspace
browser, recents) · `console_panel.cpp` (worker/debug log) · `status_bar.cpp` (job status,
spinner) · `title_bar.cpp` (window chrome + File/Edit/View/Help menus) · `config_panel.cpp`,
`design_table_panel.cpp`, `cutlist_panel.cpp` (configurations & tables) · `toast.cpp`
(notifications) · `scenarios.cpp` (scripted test harness — add a scenario if your workspace
gains a major new flow).

## Shared patterns

- Panel shape: `class XxxPanel { void draw(AppState&); void drawBody(AppState&); }`; global
  dialogs are `void drawXxxDialog(AppState&)`.
- Per-doc state → `AppState::DocTab`; global/singleton state → `AppState` members
  (`host/src/core/app_state.h`, integrator-owned — request seam additions, don't sprawl).
- Engine data arrives via the per-frame drain in `host/src/core/engine_events.cpp`; panels
  render from AppState, they don't call the worker synchronously. RPC via
  `WorkerClient::sendRpc()` (`contracts/worker_rpc.md`).
- Colors via `ui_theme` only; icons from `contracts/icons.md`; glass containers via the
  helpers in `host/src/ui_theme.h` (see gui-theme-layout for the visual system).
- Contracts are append-only; schema changes are orchestrator-level decisions.

## Build & verify

`./build.sh` from repo root must compile. Headless smoke per workspace where one exists:
`AI_CAD_SHOT=<asm|fea|flow|drawing|activity|workspaces|motion> ./host/build-linux/bin/AI-CAD
--ui-screenshot scratch/ws_smoke.png` — crash check only. Never run git. Return files changed
+ a one-line user check, e.g. "Parts Library > type in search — results stream while typing,
filters stay responsive."
