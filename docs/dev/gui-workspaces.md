# GUI workspaces

The tabbed workspaces and auxiliary panels around the modeling view. General GUI structure is
in [gui-architecture.md](gui-architecture.md); visual rules (`ui::kit`, colors, metrics) are
in [gui-theme-layout.md](gui-theme-layout.md).

`AppState::Workspace` (`host/src/core/app_state.h`) has ten values (Model, Drawing, Simulate,
Electronics, Manufacture, PLM, PartsLibrary, AI, ReverseEng, Firmware) and is append-only
because the status bar indexes by value. Each workspace is drawn from the frame loop in
`host/src/app.cpp`. `CHUDWARE_SHOT=workspaces` cycles the first eight; ReverseEng and Firmware
have their own scenarios.

## File map

| Workspace | Files | Notes |
|---|---|---|
| Parts Library | `host/src/ui/parts/` | Local parametric standards sort first; live CAD search results show file and license. Data: `contracts/parts.md` |
| Assembly | `host/src/ui/asm_panels.cpp/.h` | Mates, component insertion, BOM, exploded view |
| CAM | `host/src/ui/cam/cam_panel.cpp/.h`, `cam_mirror.cpp/.h` | Explorer / Job Manager / NC Output rail; all previews render in the native viewport |
| ECAD | `host/src/ui/ecad/` | Schematic/PCB 2D canvas editor; schemas in `contracts/ecad/` |
| PLM | `host/src/ui/plm/`, `host/src/ui/file_explorer.cpp` | Versioning, releases, document control (`contracts/plm.md`); Explorer's Changes strip and row badges surface vault status |
| Reverse Engineering | `host/src/ui/reveng/` | Gaussian reconstruction plus the measured-CAD wizard (`contracts/reveng.md`) |
| AI / Activity | `host/src/ui/ai/`, `host/src/ui/copilot/` | Current Run, Sub-agents, Plan, Activity, Sessions, Insights; events per `contracts/agent_events.md` |
| Drawing | `host/src/ui/drawing_panel.cpp/.h` | Renders only the core-compiled drawing contract (sheet frame, views, annotations) with ISO 128 paper-space weights; never host-drawn borders or title blocks. PDF/SVG/DXF export |
| Simulation | `host/src/ui/sim_manager.cpp/.h`, `host/src/ui/sim/` | Study JSON, CRUD, command routing, forms, run dispatch, results, viewport picks |
| Piping / Hydraulics | `host/src/ui/piping_panel.cpp`, `host/src/ui/hydraulic_panel.cpp`, `host/src/ui/sim/` | Simulate-only tabs; commands route through `SimManager::handleCommand` |
| Firmware | `host/src/ui/firmware/fw_workspace.cpp/.h` | Sources, build console, Emulate scenario editor, UART/GPIO viewer |
| Manufacture | `host/src/ui/manufacture/` | Devices, runs, approvals, attention; capability-truth chips and gate reports |

## Shared rules

- Per-document state lives on `AppState::DocTab`; global state on `AppState`.
- Panels render from `AppState`; engine data arrives through the per-frame drain in
  `host/src/core/engine_events.cpp`.
- Workspace kits (`ui::reveng::kit`, `plm_hub.h`, `parts_widgets.h`) are thin wrappers over
  `ui::kit`; panels use them and size from `ui::metrics` density tokens.
- During an agent run every agent-lane worker call returns `worker_lane_busy`. The UI detects
  it with `core::workerLaneBusy` and phrases refusals via `ui::userFacingRpcError`; raw
  worker/lane/RPC text is never shown.
- Optional-backend absence keeps setup editable and blocks Run with an explanation; physics is
  never substituted.

## AI / Activity and Copilot chat

- `ai_workspace.cpp` draws the shell; `agent_dag.cpp` owns per-conversation traces (an agent
  tree plus each agent's step timeline, capped at 2000 steps). `current_run_view.cpp` owns a
  single docked inspector and a roster that merges registry sub-agents with Chudcode `task`
  sub-agents (read-only).
- Activity, Plan and Sessions scope to the active project (`convProjectDir(st)`), whose
  authority is `AppState::workspaceDir`. Usage the backend does not report is labelled
  "Not reported", never estimated.
- `conversations.cpp::bindActiveConversationProject` restamps a conversation's project on each
  turn. Chat autosave uses immutable snapshots and one ordered, coalescing writer; index
  publication follows transcript durability.
- Pasted images insert `[Image #N]` at the caret; sent images render at their reference
  positions. Tool-returned images render at the tool's completion position in the chat.
- Magic keywords are matched only by `ui/copilot/magic_keywords.h` (standalone prose, never in
  code spans); Chudcode applies their meaning, the GUI only presents them.
- `ask_user` batches (`ui/copilot/ask_answers.*`) render as cards at the point they were
  asked. "Jump to latest" is a layout footer with a constant reserved height, never an overlay.
- Scenarios: `copilot_ui`, `chat_inline`, `chat_evidence`, `projectchat`, `steerstatus`. Run
  them with `CHUDWARE_ENGINE_DAEMON=0` and isolated `HOME`/XDG paths; parallel runs need
  distinct `CHUDWARE_SESSIONS_DIR` values and displays.

## CAM and Manufacture

- There is no CAM dock window or private preview canvas. Manufacture's pinned left rail has six
  tabs: Explorer, Job Manager, NC Output, Devices, Run, Attention. Rail bodies draw directly
  into the rail's child window and are designed for the ~360 dp column.
- The Offline/Live mode strip is reserved chrome: `ui::manufactureModeBarHeight(st)` is folded
  into `dockTopY` before `setOverlayBand()`, so it never covers geometry.
- Tone convention: Offline Simulation is the safe state and reads calm; Live Machine is the
  hazardous state and is the only one with the error wash and a danger button.
- `CamMirror` owns the typed job mirror plus render-ready world-space batches with global
  timing; tree revision changes invalidate generated paths. `viewport_panel.cpp` is the only
  geometric presentation path; `camDrawViewportHud()` adds controls over the same view.
- CAM UI state is one process-level active job pinned to its source part document; mutations
  use the mirrored revision and clear derived simulation/post/audit state.
- Tests: `host/tests/test_cam_mirror.cpp`; scenarios `cam` and `manufacture`.

## Reverse Engineering

- `reveng_workspace.cpp` is the shell: session rail, centre column with the source-lane
  selector and the stage stepper pinned at the top, and a Details / Capabilities / Provenance
  column fed by an async `reveng_status()` probe. Missing COLMAP/VGGT backends disable Run and
  name the required setting before the click. Columns fold responsively; user intent
  (`railIntent`/`propsIntent`) is never overwritten by the fold.
- **Every RE panel draws with `ui::reveng::kit`** (`reveng_widgets.h`), sizes from
  `ui::metrics::re*()`, shows Busy/Ok/Warn/Error for every async action, and shows every
  produced artifact. `HonestyTag` marks approximate, photo-derived values.
- `webcam_capture.cpp` drives the `host/src/platform/camera.h` v2 backend (V4L2 on Linux,
  Media Foundation on Windows, a synthetic fallback). `grabFrame()` returning `again` is the
  normal steady state. Synthetic frames are never persisted as captures; the refusal lives in
  the shutter path, not only in the UI.
- Gaussian reconstruction lives in the Source lane, independent of the measured-CAD stepper.
  A PLY and its `view_camera` belong to the same immutable SfM batch and are published
  together; asynchronous loads are fenced by session, path and revision. Live reconstruction
  means successive frozen batches, not video-rate SLAM; Gaussian units are reconstruction units,
  and measured CAD remains the separate scale/dimension-gated workflow.
- Backends: `services/reveng/gaussian_jobs.py` (supervisors, GPU reservation, cancellation),
  `services/vision/gaussian_pipeline.py` (COLMAP and Brush). Setup:
  `.venv-mcp/bin/python scripts/provision_reveng.py`.
- Transport: the RE worker RPC table in `services/reveng/worker_rpc.py` is a thin adapter over
  the reveng MCP tool registry, so a new reveng tool reaches both agents and the GUI. Failure
  envelopes are `{ok:false, reason, hint}`; surface both fields.
- Scenarios: `reveng`, `reveng_capture`, `reveng_scale`, `reveng_recon`, `reveng_review`,
  `reveng_model`, `reveng_deliver`, `reveng_splat`. Demo rows are gated on `CHUDWARE_SHOT` so
  they can never reach a real session.

## Simulation

- **Pane anatomy.** `kit::panelBegin`, `panelHeader`, a `stepper` (Setup, Run, Results) or
  `formTabs`, the body, then an `actionFooter` with one primary action, its blocker inline, and
  progress plus Cancel while running. The body never adds a second Run button.
- Run state comes from `SimManager::runUi()/runBlocker()/requestCancel()`; stage text from
  `simStageLabel/simStageFraction`, never raw engine stage ids.
- Content uses `kit_form` fields with explicit ids, face wells with removable chips, results as
  an outcome card followed by key metrics and collapsed details, and plots via
  `kit::linePlot/barPlot`. Labels are human-readable; raw tokens go in tooltips.
- `core::AsyncRpc::interactionBusy()` excludes only global metadata/status queries; document
  reads, mutations and solves still block interaction.
- Full result overlays load with `cacheAsModel=false`, keeping the editable tessellation and
  the parked GPU model intact; geometry and pick caches are restored together.
- **Topology.** `sim_topology.*` owns the `topology{}` editor, run gating and results;
  `sim_topology_bc.*` owns boundary conditions and the run envelope; `SimManager` performs all
  RPC and file work. The study is shared with the model tools
  (`services/mcp/topology_tools.py`), so editors change only the keys they display. Offered
  methods and physics come from the native capability matrix. Results show `converged`,
  `feasible` and `termination` separately; the panel never promotes topology output to
  print-ready (only a passing `slice_part` G-code check does).
- **Piping/hydraulics.** Atomic edits and loss coefficients live in `core/piping/`
  (`contracts/piping_schema.md`); MCP tools in `services/mcp/piping_tools.py` and
  `hydraulic_tools.py`. `hydraulic_panel.cpp` owns circuit mutations and bounded undo/redo.
- **Gates.** `CHUDWARE_SHOT=simaudit` must complete every registered state with zero ID
  conflicts and zero overflows; `siminteractive` exercises real mouse/keyboard input, atomic
  Apply/Revert and stale/busy fences. `pipedraw`, `hydraulics3d` and `piping` are real-input or
  pixel-checking scenarios. `demoreview` with `CHUDWARE_SHOT_DOC=<file.step+>` opens a copy of a
  saved part and captures every saved study, proving the file reopens intact.

## Firmware

Firmware > Emulate uses the same native `fw_sim_run` as the agent tool
(`services/mcp/firmware_tools.py`), with the same scenario schema. Forced backends refuse
rather than silently falling back. `fw_status` capabilities are authoritative; virtual time
horizons and host timeouts are separate budgets. Results are evidence, not certification:
keep refusals, limitations and truncated evidence visible. Scenario: `firmware`.

## Auxiliary panels

`host/src/ui/doc_tabs.cpp` (document tabs), `file_explorer.cpp` (working-folder browser with
filter, recents and vault changes), `console_panel.cpp`, `status_bar.cpp` (job status and the
sticky "Engine daemon unreachable" chip), `title_bar.cpp`, `config_panel.cpp`,
`design_table_panel.cpp`, `cutlist_panel.cpp`, `toast.cpp`, and `scenarios.cpp` (the scenario
harness).

Explorer rendering uses non-blocking `FsBackend::pollDirectory/pollStat/pollDefaultRoot`, never
blocking calls (required on the web build). A non-empty filter runs a bounded incremental
search (4000 entries, depth 12). Scenario: `explorer`.

## Verification

```bash
CHUDWARE_SHOT=<asm|fea|flow|drawing|cam|manufacture|topology|workspaces|motion|firmware|reveng> \
  xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/ws_smoke.png
```

Self-checking scenarios are functional evidence; a capture alone proves only that the flow
ran. CPU tests: `host/tests/test_cam_mirror.cpp`, `test_camera_backend.cpp`,
`test_sim_topology.cpp`, `test_magic_keywords.cpp`, `test_ask_answers.cpp`,
`test_usage_rings.cpp`.
