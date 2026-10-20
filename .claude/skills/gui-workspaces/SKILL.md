---
name: gui-workspaces
description: Chudware workspace-tab specialist — Parts Library, Assembly panels, CAM, ECAD, PLM, Reverse-Engineering, AI/Activity workspace, Drawing, Simulation manager, plus doc tabs, file explorer, console and status bar. Use for any task about one of these workspace tabs or auxiliary panels. Workers dispatched by gui-orchestrator read this file first, then work only in their own workspace's files.
---

# GUI Workspaces Specialist

Scope: the tabbed workspaces and auxiliary panels around the modeling view. **Work only in
your assigned workspace's files** — e.g. an icon-generation task never needs the Parts Library
section below, and vice versa. Shared plumbing (app.cpp dispatch, AppState seams) is
integrator-owned; dispatch, hot files and verification handoff live in
`.claude/skills/gui-orchestrator/SKILL.md`. Visual system (`ui::kit`, colors, metrics/DPI):
`.claude/skills/gui-theme-layout/SKILL.md`.

`AppState::Workspace` (`host/src/core/app_state.h`) has 10 values — Model, Drawing, Simulate,
Electronics, Manufacture, PLM, PartsLibrary, AI, ReverseEng, Firmware — and is append-only
(`status_bar` indexes by value). Each is drawn from the frame loop in `host/src/app.cpp`.
`CHUDWARE_SHOT=workspaces` (`ScenarioRunner::runWorkspaces`) cycles the first eight;
ReverseEng and Firmware have their own shots.

## Per-workspace file map

| Workspace | Files | Notes |
|---|---|---|
| **Parts Library** | `host/src/ui/parts/parts_workspace.cpp/.h`, `parts_browser_panel.cpp`, `parts_filters.cpp`, `parts_results.cpp`, `parts_taxonomy_pane.cpp`, `parts_vendors.cpp`, `parts_widgets.cpp/.h`, `parts_internal.h` | Local parametric standards sort first by relevance (`applyResultSort`); Printables live CAD results show file/license and download/import (`parts_results.cpp`); CEF-off vendor skips collapse to one notice while public CAD search remains available. Data: `contracts/parts.md`, `contracts/sourcing*.md`, `services/browser/adapters/printables.py` |
| **Assembly** | `host/src/ui/asm_panels.cpp/.h` | Mates, component insertion, BOM, exploded view |
| **CAM** | `host/src/ui/cam/cam_panel.cpp/.h`, `cam_mirror.cpp/.h` | Integrated Explorer / Job Manager / NC Output left rail; all stock, toolpath, tool and removal previews render in the native viewport |
| **ECAD** | `host/src/ui/ecad/ecad_panel.cpp/.h`, `ecad_canvas.cpp/.h`, `ecad_mirror.cpp/.h` | Schematic/PCB 2D canvas editor; schemas in `contracts/ecad/` |
| **PLM** | `host/src/ui/plm/plm_workspace.cpp`, `plm_pages.cpp`, `plm_hub.h`, `plm_state.cpp`, `plm_quickbar.cpp`; Explorer integration: `host/src/ui/file_explorer.cpp` | Versioning, releases, document control; `contracts/plm.md`. The separate left-rail PLM tab and `plm_overview.cpp` are retired: Explorer's bottom Changes strip (count badge, M/A/D/U rows, project-wide named checkpoint, PLM link) and its file-row badges (change code + rev; lifecycle in the tooltip) handle vault status and links into the full PLM workspace. Explicit revision-named siblings are visually grouped but remain individually openable physical files/items, never auto-merged. Shared item/status caches serve the hub; Files requests basic Git status/log, Git/Planning request full details. All hub pages enter `plmEnsureOverview` for one-shot outside-change recording; do not duplicate it in project selection. |
| **Reverse Eng.** | `host/src/ui/reveng/`: `reveng_workspace.cpp` (shell), `reveng_widgets.h/.cpp` (shared visual kit), `sources_panel.cpp`, `webcam_capture.cpp`, `stage_panels.cpp`, `dim_review.cpp`, `image_gallery.cpp`, `img_tex.cpp` | Native live/video Gaussian reconstruction plus the separate six-stage measured-CAD wizard (`contracts/reveng.md` §13). **Every panel draws with `ui::reveng::kit`** and uses `ui::metrics::re*()`. Camera seam: `host/src/platform/camera.h` v2. Native splat/point rendering reads `kit::previewSlotRect` and `previewSlotClip`. Full breakdown below. |
| **AI / Activity** | `host/src/ui/ai/ai_workspace.cpp/.h` shell/nav + `ai/agent_dag.cpp/.h` execution graph, `ai/current_run_view.cpp/.h` Current Run/Sub-agents rosters and docked inspector, `ai/{plan_board,docs_panel,active_work,metrics_view,activity_view,insights_view}.cpp`, plus `host/src/ui/copilot/` chat modules (reuse hooks in `copilot.h`) | Current Run / Sub-agents / Plan / Activity / Sessions / Insights / Options, project chip; project scoping via `convProjectDir(st)`; events per `contracts/agent_events.md` |
| **Drawing** | `host/src/ui/drawing_panel.cpp/.h` (+ `AppState::makeDrawing` in `core/doc_tabs.cpp` → one `drawing_make`) | Renders ONLY the core-compiled v2 draw contract (`sheet.frame`, view geometry, `view.draw`, annotation `draw`) via `SheetPen` with ISO 128 paper-space weights — never host-drawn borders/title blocks/markers; sheet-properties popup (paper/portrait/projection/scale/units/title block → `drawing_set_sheet`/`drawing_arrange`/`drawing_set_units`/`drawing_set_titleblock`); PDF/SVG/DXF export; `drawDrawingEmptyState` for the no-drawing workspace |
| **Simulation** | `host/src/ui/sim_manager.h` + `sim_manager.cpp` (core: study JSON, refresh/persist, CRUD, `handleCommand` routing incl. piping/hydraulic/EM/topology dispatch, materials, probes) and `host/src/ui/sim/`: `sim_manager_tree.cpp` (left-pane tree, panel header, study rows, new-study picker), `sim_manager_forms.cpp` (material/fixtures/loads/mesh/fatigue forms, checklist, `drawForm`), `sim_manager_run.cpp` (run dispatch, static/modal/thermal/contact/FRF prep, `tick`, `drainSimClientJob`), `sim_manager_results.cpp` (result overlays, view sync, result controls, advisor, provenance, advanced results), `sim_manager_viewport.cpp` (face picks, BC previews, mesh preview, indicator glyphs), `sim_manager_flow.cpp`, `sim_manager_motion.cpp`, `sim_manager_em.cpp`, `sim_manager_topology.cpp`, `sim_manager_electronics.cpp` (circuit/emulation/ECAD thermal hand-off); helpers shared by 2+ TUs live in `sim/sim_manager_internal.h` (`ui::simmgr`, `inline`), single-TU helpers stay file-local; `host/src/ui/evaluate_dialogs.cpp`; topology: `host/src/ui/sim_topology.cpp/.h`, `host/src/ui/sim_topology_bc.cpp/.h` | Study tree, fixtures/loads, results; Evaluate dialogs (measure, mass props, …). The **Topology** ribbon tab (Simulate only, `ribbon_def.cpp` `sim_new_topology`/`sim_topology_*`) drives topology studies; see "Simulation: Topology tab" below |
| **Simulation: Piping / Hydraulics** | `host/src/ui/piping_panel.cpp/.h`; `hydraulic_panel.cpp/.h` (shell/state/RPC/mutations/undo, owned by `SimManager`), `sim/hydraulic_forms.cpp` (forms), `sim/hydraulic_canvas.cpp` (schematic/plots/HUD), `sim/hydraulic_internal.h` (`ui::hydint` helpers), `sim/hydraulic_canvas_state.h` (`ui::hydcanvas::State`); `sim/sim_scene.h/.cpp` dispatcher, `sim/pipe_scene.cpp`, `pipe_mesh.*`, `pipe_sketch.*`, `hydraulic_scene.cpp` producers | `Piping` (tab 22) / `Hydraulics` (tab 23), Simulate only; `sim_piping_*` / `sim_hyd_*` route through `SimManager::handleCommand`. Piping Draw Pipe/Fittings author shaded networks; fitting editor previews native K(Re) and runs CFD-K. Hydraulics: Circuit · Design · Simulate · Results (Outcome · BOM · To Piping); Schematic/Plots own the central panel, 3D Layout uses the native viewport. Persist `piping_network`/`pipe_flow`/`vessel`/`duct_system`/`hydraulic_circuit` studies under "Piping, hydraulics & industrial"; hydraulic positions are `params.layout3d`, shared with MCP `hydraulic_layout_3d`. Engine `code` is a design-code string; a protocol error is `code` WITHOUT `ok`. Shots: `piping`, `pipedraw`, `hydraulics` (incl. `hydraulics_09_template.png`), `hydraulics3d`. |
| **Firmware** | `host/src/ui/firmware/fw_workspace.cpp/.h` | Project rail + sources, build console (parsed warnings), Emulate scenario editor + Engineering evidence, UART/GPIO viewer; same native `fw_sim_run` as agents (operational details below); fw_status-gated Flash/Debug; tab is progressively disclosed (`ui::firmwareTabVisible`); shot: `CHUDWARE_SHOT=firmware` |
| **Manufacture** | `host/src/ui/manufacture/{manufacture_panel,device_panel,run_panel,approval_dialog,attention_panel,mfg_mirror}.cpp/.h` | Capability-truth chips, gate-report rendering; rail/mode-strip layout in "CAM / Manufacture internals" below. Bodies draw with `ui::kit` — cards, tone chips, `keyValueRow`, `emptyState`/`blockedState`, `inlineStatus` — never bespoke text dumps. **Tone convention: Offline Simulation is the SAFE state and reads calm (`Tone::Ok` dot chip on neutral glass); LIVE MACHINE is the hazardous one and is the only state that gets the `statusErr` wash + danger button.** Shot: `CHUDWARE_SHOT=manufacture` (the rail opens on Attention for that shot only — `layout.cpp` resolves the shot tab ONCE from `CHUDWARE_SHOT`, so `cam` keeps the Job Manager default) |

### Firmware: shared Emulate surface

- **One run path.** Firmware > Emulate selects the existing project, Auto/Renode/QEMU/
  simavr backend, scenario JSON (edit or load/save), duration/host timeout and assertions.
  Run emulation calls native `fw_sim_run`; agents use the MCP tool with the same inputs,
  optionally `elf_path` or `scenario_path` instead of inline JSON. UI-loaded files become
  inline JSON. See `services/mcp/firmware_tools.py::fw_sim_run` for the existing SI schema,
  not a GUI-only dialect. Forced backends refuse rather than silently falling back.
- **Bounded clocks/time.** The shared timed/electrical runtime supports STM32F4 Renode
  I2C (`i2c1–3`), TTL UART (`usart1/2`), master SPI (`spi1–3`) and bxCAN (`can1/2`).
  `fw_status` capabilities are authoritative; other families' functional support is not
  electrical support. Firmware registers set timing against declared `controller_clock_hz`;
  without links F4 assumes reset 16 MHz HSI clocks and an ideal medium. RCC/PLL/APB changes
  are not modeled. Renode `run_s` is a virtual horizon, `timeout_s` an independent host
  budget (UI/MCP 0 omits it; native default 30 s). A submitted frame may be unfinished at
  the horizon. QEMU has no peripheral scenario/precise virtual stop; simavr stops by wall time.
- **Peer precedence and timing.** CAN track `bitrate_hz` reconfigures the named peer even
  with a link, using sample point 0.875, SJW 1 and track `ack_enabled` (default true);
  conflicting settings are not rejected as a must-match rule. Inspect emitted effective
  timing, not just link JSON. SPI receive budget includes the clock path, peer output delay
  and MISO propagation/rise/load/threshold settling. The exercised 20 ns-edge/20 ns-output
  profile exceeded its 50 ns half-period and produced SPI_BAD with the same ELF; a healthy
  5 ns-edge profile is not a universal timing guarantee.
- **Evidence, not certification.** Engineering exposes native `bus_summary`/`bus_events`
  and Copy native result JSON. Preserve that result with the scenario; `trace_path` contains
  GPIO/UART, not the full bus packet. `passed` is the requested assertion verdict, possibly
  with no assertions. Check refusals, limitations and finite/truncated evidence. Resolved/
  unresolved samples and electrical causes are independent of hardware flags: UART ONEBIT
  retains unresolved evidence without inventing NF. Past/delayed receiver samples preserve
  voltage/validity history; quiet line ports still integrate travelling waves. Virtual
  nanosecond timestamps and the 0.1 ns integration floor are not subnanosecond accuracy
  promises. Renode 1.16.1 has final GPIO ODR evidence, not the observer for timestamped
  GPIO/toggle/period assertions. Linear RC/lossless lines exclude nonlinear/common-mode/
  EMC/crosstalk/dispersion behavior; this guidance supplies no full-flight, Windows/macOS
  or live-hardware proof. Keep all native limitations visible.

### Agent Hub / project ownership

- `ai_workspace.cpp` draws the shell (session header + New session above the chat; nav
  view persisted as `agent.ui_ai_view`); `agent_dag.cpp` owns per-conversation traces:
  an **agent tree** (main + one node per `subagent_id`, nested by spawn `parent_id`) and
  each agent's **step timeline** (`drawAgentTrace`, ListClipper, 2000-step cap, saved as
  `v:2`; rebuilt from persisted activity rows when a session has no live events). There is
  no pan/zoom node canvas any more. `current_run_view.cpp` owns one **docked** inspector
  (agent mode + step mode) and a roster that merges registry sub-agents (`subagent_list`,
  routed to the GENERAL worker lane so it answers during a run) with Chudcode `task`
  sub-agents from the trace (`agentTraceSubagents`; read-only, the main agent directs
  them). Do not restore floating sub-agent popups or auto-open an inspector.
- **Sub-agents** (`agent.ui_ai_view=6`, existing view values unchanged) reuses the
  registry poller and inspector across all stored conversations in the active project.
  Flat / By parent and optional Current session only are persisted in
  `agent.ui_subagent_hub_by_parent` / `agent.ui_subagent_hub_session_only`.
  Selection keys include both conversation ID and agent ID; inspection never opens
  the owning chat. Scope changes clear selection, drafts and stale async results.
  Session-only fails closed when the open chat belongs to another project.
  Transcript, steer, pause and terminate requests carry project/owner-session scope;
  trace-only agents remain read-only. Usage not reported by the backend is labelled
  Not reported, never estimated.
- Activity (`active_work.cpp`, `docs_panel.cpp`) and Plan (`plan_board.cpp`) scope
  artifacts/links to `convProjectDir(st)`. Active Work cards render live previews offscreen
  (`render/doc_thumbnail.*`) from each tab's prewarmed `cachedDocMesh`, keyed by
  (docId, revision, aicm), ≤1 render/frame, 48-texture LRU; counts are only shown when known.
  Documents "Attach to next message" feeds `st.pendingAttachments`; Save never clobbers a
  file that changed on disk. `insights_view.cpp` header is "Model leaderboard · All
  projects"; `metrics_view.cpp`'s chip is "Include all projects".
- Sessions (`panel.cpp drawSessionsPanel`, shared by dock and workspace) list ONE project at
  a time via the header switcher (active project by default; empty key = "Unassigned" only).
  Titles come from `autoTitleConversation` (first prompt) unless renamed (`title_user`);
  numbering uses the index's monotonic `session_seq`.
  The default scope comes directly from `AppState::workspaceDir`, the top-left chip's
  sole authority, not the active document's folder. History load/reload validates
  `active_by_project` ownership before restoring the selected root's chat. The
  `projectchat` shot checks startup, root switches, parked runs and external documents.
- The Copilot header is compact, flat navigation: run-tinted tesseract, Chat / Tasks /
  Artifacts, settings and collapse. Measured text/icon widths keep hit boxes and paint aligned.
  Header glyphs use the existing SDF-crisp `drawIconGlass` path, not stretched 20px atlas cells.
  Counts accompany "Tasks" only when real tasks exist; never display an idle "0/0".
  Labelled Sessions / New chat share the row when everything fits, otherwise move onto a
  second row together. New chat parks the outgoing conversation, including during a run.
  Composer footer controls remain adjacent cells; New chat is a header action.
  `CHUDWARE_COPILOT_WIDTH` (shot-only, logical px) exercises resized headers without dragging.
- Question choices in `cards.cpp` are native full-width selectable rows with wrapped labels
  and compact selection glyphs; question/option IDs distinguish duplicate labels. Other's
  input is full-width below its selector. Dimension/unit and path/Browse pairs stack when
  measured space is insufficient; picked-geometry labels and actions wrap/stack. Pending
  and resolved status headers wrap as well. The source is enabled for test-engine semantic
  item annotations in `host/CMakeLists.txt`, including the native dimension combo.
- Jump to latest is a layout footer, never an overlay over question inputs. All three hosts
  (dock chat, AI workspace chat, agent trace timeline) reserve `chatJumpToLatestFooterHeight()`;
  the slot stays constant while the action hides at the tail, preserving scroll offsets.
  Short-window layouts budget the composer/footer first rather than enforcing a 120dp log.
  Smoke: `CHUDWARE_SHOT=copilot_ui`, isolated HOME/XDG paths, `CHUDWARE_ENGINE_DAEMON=0`;
  optional `CHUDWARE_COPILOT_UI_WORKSPACE=1`, `CHUDWARE_COPILOT_UI_OUT=<capture-directory>`,
  `CHUDWARE_SCALE` and `CHUDWARE_THEME`. Exercises real clicks/text, every question kind,
  header actions, and unclipped short-window layout; report/captures default to
  `scratch/copilot_ui_shots`.
  Parallel matrix runs MUST give each process a distinct `CHUDWARE_SESSIONS_DIR` and display:
  the default shot store is shared by scenario name and is cleared on startup.
- During a run every agent-lane worker call returns `worker_lane_busy`; UI detects it with
  `core::workerLaneBusy` and phrases refusals via `ui::userFacingRpcError` /
  `ui::kAvailableAfterRun`. Never show raw worker/lane/RPC text.
- `conversations.cpp::bindActiveConversationProject` restamps `Conversation.projectDir`
  to the workspace sent on **each turn**, not only at creation. `engine_events.cpp`
  stamps agent `DocTab::projectDir` from the uniquely owning running conversation;
  saved docs instead use their file path to establish project ownership. The first
  agent mirror may take focus from a pristine, empty active CAD tab
  (`activeOwnDocIsPristine`), not from user work.
- Image paste inserts `[Image #N]` at the live composer caret (replacing selection).
  `inline_image_refs.h` indexes exact paths; `pendingInlineImagePaths` keeps stable draft slots
  (`inline_images` in prompt drafts), while `consumeComposerAttachments` omits deleted refs,
  orders model images by first reference and compacts sent numbering. Queued prompts freeze
  attachment/context snapshots; staged edit/cancel restores the matching roster.
- Sent images render at their reference positions, not in a trailing gallery.
  `ChatMsg::imagePaths` is copied from the exact send/steer payload and persisted by
  `conversations.cpp` (`images` in compact messages, `imagePaths` in raw archives).
  `chat_log.cpp` caches text/image segmentation; `attachmentImagePreview` shares the bounded
  thumbnail cache and full-size viewer. Legacy attached-image markers consume recorded paths
  in order; missing paths stay text. Never resolve basenames across paste-cache days.
  Smoke: `CHUDWARE_SHOT=chat_inline`, `CHUDWARE_ENGINE_DAEMON=0`, isolated HOME/XDG paths,
  an isolated `XDG_RUNTIME_DIR`/invalid `WAYLAND_DISPLAY` under Xvfb and `xclip` on PATH.
  Supply portrait.png/landscape.png in `scratch/chat_inline_shots`; captures/report land there.
- Tool-returned visual evidence belongs at the tool's completion position in chat, outside
  its disclosure drawer. `activity_timeline.cpp` renders actual image paths at every tool
  detail level and preserves grouped-call order; expanding details never repeats thumbnails.
  `feed.cpp` stores the returned caption in the `CAP` trailer before `ART`; clipboard copy
  includes it, and compact tool rows retain complete bounded trailers. Restored Session files
  rows render only unmatched/legacy images. Respect the explicit Show renders inline preference.
  Smoke: `CHUDWARE_SHOT=chat_evidence CHUDWARE_CHAT_EVIDENCE=<mapped-event-array.json>`,
  optionally `CHUDWARE_CHAT_EVIDENCE_EXPANDED=1`; use an isolated, short HOME/appdata path
  (worker Unix-socket paths must fit AF_UNIX) and a tall Xvfb screen.
- Desktop chat autosave (`conversations.cpp`, `chat_persistence.h`) uses exact immutable
  message/session snapshots and one ordered, coalescing writer. Unchanged session files
  are not rewritten. Explicit save/reload calls remain barriers; routine debounce never
  waits for fsync. Failed writes retain pending state; index publication follows transcript
  durability, then owned-session pruning. Shutdown drains agent control jobs before
  `flushChatPersistence`, while AppState and its worker remain alive.
- Tool-row layout (`activity_timeline.cpp`) caches exact parsed inputs and stable measured
  heights, windowing settled offscreen rows and group members. Invalidate on row edits,
  grouping, width, theme/font/detail preferences and session/anchor changes. Never clip an
  animating row using a stale height or retain an unbounded trace cache.
- **Copilot chat conventions.** Magic keywords (Chudcode's `ultrathink`,
  `orchestrate`, `workflowz`, `jevify`, `ultragoon`, `autobot`, `tokenmaxxing`; table mirrors
  `omp/packages/coding-agent/src/modes/magic-keywords.ts`) are matched ONLY by
  `ui/copilot/magic_keywords.h` (standalone prose, never in code spans/fences/tags;
  `test_magic_keywords`) and painted with `chatPaintMagicKeywords`: shimmering gradient in the
  focused composer, static in sent bubbles. Chudcode applies their meaning server-side; the GUI
  only presents them. `ask_user` batches are `ChatMsg::asks` (`ui/copilot/ask_answers.*`,
  `drawAskCards`); an open batch replaces the run status with "Waiting for your answers".
  User messages are right-aligned mode-tinted bubbles; assistant prose has no box.

### CAM / Manufacture internals

- There is **no CAM dock window or private preview canvas**. `buildDefaultLayout()` keeps one
  uninterrupted Viewport/ECAD central node. Manufacture's pinned left rail hosts Explorer,
  `drawCamTreeBody()` (job/setup/tool tree, serialized-value operation inspector, simulation
  controls) and `drawCamOutputBody()` (post flavor, G-code, audit).
- **No floating operator drawer, no floating mode banner.** The Manufacture rail in
  `drawLeftPane` has SIX tabs: Explorer, Job Manager, NC Output, Devices, Run, Attention.
  The last three are `ui::drawManufacture{Devices,Run,Attention}Body(st)` — they draw straight
  into the rail's body child (no `Begin`/`BeginChild` of their own, same contract as
  `drawCamTreeBody`), and are written for the ~360dp column, not a wide drawer. The Attention
  tab shows its attention dot from `ui::manufactureCriticalCount(st) > 0`. The column's `minW`
  is raised to 360dp on this workspace so six rail buttons always fit.
- **The Offline/Live mode strip is real chrome, not an overlay.** `app.cpp` calls
  `ui::manufactureModeBarHeight(st)` right after the ribbon, folds it into `dockTopY` BEFORE
  `setOverlayBand()`, and draws it with `ui::drawManufactureModeBar(st, x, y, w, h)`. So the
  dockspace, the doc-tab strip and both pinned columns start beneath it and it can never cover
  geometry. `drawManufacturePanel(st)` is only bootstrap RPCs + the approval dialog.
- `CamMirror` owns the typed job mirror plus render-ready world-space batches. Every path class
  carries global segment start/end times and cached direction triangles; stock-box edges are
  cached too. Tree revision changes invalidate generated paths so stale geometry cannot remain
  authoritative after an edit.
- `viewport_panel.cpp` is the only geometric presentation path: it draws classified paths with
  `Overlays::drawCamSegments`, direction glyphs, stock, tool/spindle marker, and remnant-stock
  frames inside the existing renderer color FBO. `camDrawViewportHud()` adds controls/telemetry
  over that same view; it is not a second geometry surface.
- `camDrawViewportHud()` publishes its card rect through `publishViewportHudRect()` /
  `viewportHudRect()` (declared in `cam/cam_panel.h`, frame-stamped like
  `publishOverlayColumnHeight`). The orientation triad and the model-dimensions readout in
  `viewport_panel.cpp` READ it and lift above the card, so the bottom-centre HUD and the
  bottom-left triad never overlap on a 360dp-rail workspace. Any new bottom-anchored
  viewport card should publish the same way instead of hoping for empty space.
- CAM UI state is one process-level active job. Extraction pins the source **Part doc id** instead
  of following whichever tab becomes active later. Mutations use the mirrored revision, reject
  overlap with long RPCs, and clear derived simulation/post/audit state.
- Focused CPU contract: `host/tests/test_cam_mirror.cpp`. End-to-end smoke:
  `CHUDWARE_SHOT=cam ./host/build-linux/bin/chudware --ui-screenshot scratch/cam.png`.

### Reverse Engineering internals

Read `reveng_widgets.h` before touching any RE TU.

- `reveng_workspace.cpp` / `.h` — the shell. Three columns: session rail (rich cards with
  thumbnails, status/progress, substring filter, New-session card), centre column with the
  source-lane `SegmentedControl` and **the stage stepper pinned at the TOP** (clickable
  `kit::StepTrack`, locked steps explained by tooltip) above the active stage body, and a
  properties column with three tabs — **Details / Capabilities / Provenance** — fed by an async
  `reveng_status()` capability probe (`loadCapabilities`). Also owns the Reconstruct stage and
  its **capability pre-flight gate** (`ReconGate`: a missing COLMAP/VGGT backend disables Run and
  names the env var instead of failing after the click), the `reveng_*` ribbon command router
  (`app.cpp` `dispatchCmd` forwards every `reveng_` id here; Source entry points set a one-shot
  `SourceFocus`), and `ui::revengWorkspaceTeardown()` — closes the camera + rail thumbnails,
  called from app.cpp on leaving the tab and at shutdown (the workspace also self-heals on
  re-entry after a frame gap). **Responsive columns:** minimums are `reRailW` / `rePropW` /
  `rePreviewMinW`; a narrower body folds Auto side columns to their strips (Properties first,
  then Sessions) via `foldColumns()` every frame. User intent is `RevEngState::railIntent` /
  `propsIntent` (`ColumnIntent` Auto/Collapsed/Expanded) and is never written by the fold; any
  "show the rail" action goes through `revealColumn()`, which pins Expanded only when the layout
  is folding it. Chip rows flow with `statusChipW`/`valueChipW` + `flowSameLine`
  (`reveng_internal.h`); rail labels ellipsize via `ui::kit::elide`.
- `reveng_widgets.h` / `.cpp` — **the shared visual kit, namespace `ui::reveng::kit`. Every RE
  panel must draw with it**; this is the single most important convention in this workspace.
  It provides: `Level` + `levelColor`, `CardBegin`/`CardEnd`/`CardHeaderRight`/`SectionHeader`,
  `StatusChip`/`ValueChip`/**`HonestyTag`** (the approximate/photo-derived tier badge),
  `InlineStatus`/`HelpMarker`, form rows (`FieldLabel`, `KeyValueRow`),
  `PrimaryButton`/`SecondaryButton`/`DangerButton`, `SegmentedControl`, `ProgressBar`,
  `StepTrack`, `HeroPreview`, `ThumbTile`, `ThumbHoverPreview`, `Viewport3DPreview` +
  `previewSlotRect`/`previewSlotClip`/`resetPreviewSlots` (app.cpp's post-ImGui GL seam:
  full rectangle for projection, visible clip intersection for scissoring), `EmptyState`,
  `BlockedState`, `CapabilityRow`, `Rule`,
  `GapCards`/`GapSection`/`GapControl`, and the `fmtMm`/`fmtPct`/`fmtDuration` formatters.
  Hard rules it encodes: sizes from `ui::metrics::re*()`, colours from `ui::theme().palette`,
  every async action shows Busy/Ok/Warn/Error, every stage that produces an artifact shows it.
- `sources_panel.cpp` — the four source lanes selected by the shell's source-mode control:
  **Scan** (webcam console, file picker, video ingest, phone-QR capture block, plus the gallery),
  **Datasheet** (→ `reveng_ingest_datasheet`), **Web+Part#** (→ `reveng_research_source`),
  **From Library** (seeded by the Parts Library, routed mode shown read-only).
- `webcam_capture.cpp` — live capture console over the **`camera.h` v2** backend: device combo +
  Rescan, resolution/mode combo from `listModes` with the negotiated `activeMode()` chip,
  focus / exposure / white-balance / gain / zoom / brightness controls, `lockForCapture()` as the
  photogrammetry pre-flight, a coverage dome fed by real `get_reveng_coverage` (never a local
  counter), live variance-of-Laplacian sharpness shown before the shutter, and a **hard block on
  persisting synthetic frames** (refused in `shutterFire`, not merely greyed out).
- Gaussian reconstruction lives in the **Source** lane, independent of the measured-CAD
  stepper. `reveng_workspace.cpp` owns `GaussianState`, capability/status polling, async
  validated PLY loads, object crop/reset, and calibrated orbit/pan/zoom. `sources_panel.cpp`
  starts imported-video jobs; `webcam_capture.cpp` supplies continuous native keyframes.
  Both share per-session `GaussianRunControl`: finish/cancel requested while start or ingest
  is pending must be delivered after that request completes, not silently discarded.
- Live capture binds session ID and generation; changing rail selection never retargets an
  active camera stream. Persist accepted frames through canonical ingest, not directly into
  the gallery. The rail uses the optional Gaussian summary, not unrelated CAD stage status,
  and preserves selected/gallery **session IDs** across list replacement and reordering.
- Backend map: `services/reveng/gaussian_jobs.py` owns Linux supervisors, atomic sidecars,
  GPU reservation and cancellation; `services/vision/gaussian_pipeline.py` runs real
  COLMAP registration/undistortion and Brush; `dense_backends.py` owns the CLI adapter.
  Exact tool names are `reveng_start_splat`, `reveng_splat_status`,
  `reveng_finish_splat`, `reveng_cancel_splat`, and `reveng_crop_splat`.
  Source and compiled children both dispatch through the accepted modules in
  `services/compiled_entry.py`.
- Backend setup: `.venv-mcp/bin/python scripts/provision_reveng.py` installs checksum-pinned
  Apache-2.0 Brush v0.3.0. `--source-checkout PATH` gets its editable pinned source;
  `BRUSH_BIN` selects a locally modified build. The worker needs `pycolmap==3.13.0`;
  launch a development build with `CHUDWARE_PYTHON="$PWD/.venv-mcp/bin/python"`.
  Do not launch Brush's external viewer or restore the obsolete Brush-to-mesh adapter.
- Honesty invariants: live means successive frozen batches while capture continues,
  not video-rate SLAM or optimizer resume. Object mode is a stationary subject plus
  explicit 3D crop, not automatic segmentation. Gaussian units are reconstruction units;
  measured CAD remains the separate OpenMVS + scale/dimension-gate workflow.
- A PLY and its `view_camera` must belong to the same immutable SfM batch. Publish them
  together and fence asynchronous loads by session/path/revision. Use a registered capture
  camera for the initial view; **Capture view** restores calibration and **Frame splat
  bounds** is explicit. Do not reset user navigation on every export of the same batch.
  Claim mouse-wheel ownership inside the preview so zoom cannot also scroll its parent.
- `stage_panels.cpp` — Scale (`drawScalePanel`: aruco_mat / two_point / known_dim →
  `reveng_set_scale`; `reason:"scale_needs_sparse"` renders as a `BlockedState` that jumps to
  Reconstruct), Model (`drawModelPanel`: `reveng_fit_primitives` cards, `reveng_build_parametric`
  gated on the approximate honesty flag), Deliver (`drawDeliverPanel`: trust hero,
  `reveng_attach_result`, `export_reveng_bundle`, provenance summary).
- `dim_review.cpp` — the Review stage. **`drawReviewStage()` is the entry point** the shell calls;
  it owns the split between `drawDimReviewPanel` (dimension ledger, `list_reveng_dims` →
  `confirm_dimensions`, fit-critical rows dominant) and `drawDeviationPanel`
  (`get_reveng_deviation` error map + bucket histogram + verdict). `seedCannedDimPayload()` is
  `$CHUDWARE_SHOT`-gated so demo rows can never reach a real session.
- `image_gallery.cpp` + `img_tex.cpp` — the contact sheet: responsive tile grid with hover
  metadata previews, quality/synthetic badges, a filter + count summary, culling and full-size
  viewer. Thumbnails come from `reveng::cachedThumbTex()` — `img_tex` has a **bounded
  downscaled thumbnail decode** (`loadThumbTexFromFile`) plus an **LRU texture cache**
  (`setThumbCacheLimit`, `thumbCacheOwns`, `thumbCacheStillMaps`, `thumbCacheEvictionEpoch`,
  `thumbCacheClear`). `freeTexture()` is a documented **no-op for cache-owned ids**, which is what
  keeps `galleryFreeTextures()` from double-freeing a texture the cache still hands out.

**Platform seam — `host/src/platform/camera.h` (v2 `CameraBackend`).** `listDevices`,
`listModes(device_id)`, `open(id, OpenRequest)`, `activeMode()`, `grabFrame() -> GrabResult`
(`ok|again|device_lost|decode_failed|not_open`; `again` is the normal steady state, never a
reopen trigger), `getControl`/`setControl`/`setControlAuto` over
`CameraControl::{Focus,Exposure,WhiteBalance,Gain,Zoom,Brightness}`, `lockForCapture()`, and the
honesty flag `CameraFrame::synthetic` (a synthetic frame must never be persisted as a capture).
`frameSharpness(frame)` + `kSharpnessBlurThreshold` mirror `services/vision/frames.py`
`blur_score()`. Implementations: `camera_v4l2.cpp` (Linux, mmap streaming + MJPEG),
`camera_mf.cpp` (Windows Media Foundation), `camera_stub.cpp` (synthetic fallback, the
cross-platform `CameraBackend::create()` factory and the `frameSharpness` definition).

**Transport.** The RE GUI reaches Python through the worker RPC table in
`services/reveng/worker_rpc.py`, a *thin adapter* over the reveng MCP tool registry
(`services/mcp/reveng_session_tools.py`, `reveng_build_tools.py`, `recon_tools.py`): adding a
reveng tool exposes it to BOTH the agent and the GUI, and there is no second implementation to
keep in sync. Bridging one is a line in `_BRIDGED_TOOLS`. Only `reveng_start_from_product` is
hand-written. Failure envelopes are always `{ok:false, reason, hint}` — surface `reason`
and `hint`, never a bare "failed".

**Headless shots** (`CHUDWARE_SHOT=<name> ./host/build-linux/bin/chudware --ui-screenshot …`):
`reveng` (empty shell), `reveng_capture` (Source stage, canned session), `reveng_scale`,
`reveng_recon` (mid-run at 45 %), `reveng_review` (canned dims + deviation), `reveng_model`,
`reveng_deliver`. All but the first seed a canned session via `seedRevengCanned` in `app.cpp`.

### Simulation: UI standard (every sim surface)

- **Pane anatomy.** `kit::panelBegin` → `panelHeader` (via `simPanelHeader`/`drawPanelHeader`;
  close label "Done"; title = record name only) → `stepper` Setup → Run → Results (studies; setup
  sub-chips from `setupSteps()`, the single readiness model also used by tree rows) or `formTabs`
  (Piping/Hydraulics/EM; labels match the ribbon) → body → `actionFooter` with the one primary
  action, its blocker inline (≤ 2 lines, action-first), progress + Cancel while running. A Results
  step without results keeps "Run <type>" as primary; the body never adds a second Run button.
- **Run state** comes from `SimManager::runUi()/runBlocker()/requestCancel()`; stage text from
  `simStageLabel/simStageFraction` (never raw engine stage ids). Ribbon setup commands route through
  `openSetup(node, focus)` + `requestFocus/takeFocus` so the matching card scrolls into view.
- **Hydraulics workflow.** Circuit's Start card reads native `hydraulic_templates` and adopts
  `hydraulic_new(template)` (power unit, cylinder circuit, motor circuit or blank, with editable
  simulation setup). Canvas palette supports drag/drop and click placement; clicking two ports
  or dragging between them calls `connectPorts` immediately, including tee extension at an
  occupied port. No footer staging; Connect manually has its own button. Ribbon Simulate runs
  when `simulateBlocker()` is empty, otherwise reveals setup/blockers; completion opens
  Results → Outcome. Simulate is setup-only; Results has Outcome · BOM · To Piping.
  The 3D layout card (plane/offset/grid/snap/frame/placement) appears only in 3D view;
  the selected component editor retains its 3D position fields and Place in 3D button.
- **Content.** kit_form fields with explicit ids (pane-wide label column, unit lane), face wells
  via `faceWellControls` + `kit::removableChip` (hover → `ui::claimViewportHoverFace`), repeated
  items as `removableCard`, results as outcome card (chips once) → key metrics → display controls →
  collapsed details; plots via `kit::linePlot/barPlot`; no editable multi-column tables in the side
  pane; human labels only (raw tokens in tooltips). Shared non-UI helpers: `sim/sim_common.h`
  (JSON/RPC/`StudyRecordStore`), pump drive form `sim/sim_pump_drive.h`.
- **Interactive setup.** `sim/sim_interaction.h` owns document/revision-scoped face/tool
  payloads. `sim_manager_viewport.cpp` routes compatible delivered tools; face wells consume
  delivered faces additively and deduplicate the same boundary role. `viewport_panel.cpp`
  retains blank-click orbit/navigation and ordinary picking; simulation face-source/tool-drop
  reads use `IdPass::render(camera, true)`. The GPU face-only mode skips edge/vertex IDs and
  participates in the frame-cache key, so normal picks cannot reuse that buffer.
- **3D network seam.** `sim/sim_scene.h/.cpp` (`ui::simscene`) is the only viewport
  dispatcher: `active/collect/toolArmed/handleViewport/onPlaneChip` forward to the open
  panel's `ui::pipescene` or `ui::hydscene`. Cached lit/line batches use keys
  `0x1000..0x1FFF` / `0x2000..0x2FFF`, revisions change only with buffer content.
  Collect/render even WITHOUT `st.hasDocMesh`: empty parts are valid network canvases.
  Armed tools own plain left clicks and Esc/Enter/Backspace/Tab, not navigation drags;
  reference-plane chips select the working plane. Pointer hover must use
  `ImGuiHoveredFlags_NoNavOverride` so keyboard navigation cannot hide it. Renderer
  auxiliary bounds frame networks; see gui-viewport for the camera/render contract.
  Transient batches (ghost, rubber band, plane patch, grid) set `bounds=false` — otherwise
  mouse motion rescales zoom. Pipe planes: `DrawSettings::autoPlane` (camera-facing plane
  through the last point) vs `plane` (last explicit pick); Tab cycles XY→XZ→YZ→Auto.
  Pipe sketch finish queues through a pending save, retaining cancellation; its
  centred hint publishes `viewportHudRect` so the triad/dimensions avoid it.
- **Network owners/tools.** `pipe_mesh.*` builds shaded bends/fitting glyphs and solved
  pressure/velocity colour; `pipe_sketch.*` owns planes, risers, snaps and typed lengths.
  Atomic edits and deterministic K live in `core/piping/edit.cpp` / `solve.cpp`
  (`piping_edit`, `piping_fitting_k`, `contracts/piping_schema.md` §9). MCP:
  `services/mcp/piping_tools.py`; CFD-K: `services/piping/fitting_cfd.py`,
  `worker_rpc.py` (`piping_fitting_k_cfd_start/_status/_cancel`, agent tool
  `piping_fitting_k_cfd`; poll that tool, NOT `get_job`). Hydraulic glyphs/pressure
  hoses/placement live in `hydraulic_scene.cpp`; MCP layout in `hydraulic_tools.py`.
  `hydraulic_panel.cpp` owns `commitCircuitMutation` and bounded circuit/layout undo/redo;
  forms, schematic drag release and 3D placement use this seam. Header/canvas buttons and
  owner-aware Delete/Ctrl+Z/Ctrl+Shift+Z/Ctrl+Y edit circuit history. `app.cpp` skips global CAD
  Ctrl+Z/Ctrl+Y while Simulate is active and the Hydraulics panel is open.
- **Native customization.** `SimManager::drawParameterEditor/configureActiveStudy` preserve
  canonical definitions plus typed baselines. Pristine drafts refresh by generation; dirty
  drafts retain their exact base and refuse stale Apply. Apply is atomic, Revert reloads current
  data, and single-edited mesh aliases synchronize without accepting genuinely conflicting
  pairs. Per-load and mate-ID baselines prevent reorder from moving unrelated native metadata.
  `sim_manager_run.cpp` uses an immutable merged request; managed identities/governor fields
  remain protected. Pure `worker_normalize_face_query` precedes native document resolution.
  Compound face sets keep terminal geometry separate through native facet/node selection,
  including explicit thermal exterior facets; one authored force is not multiplied by children.
  EM/Electronics typed controls remain primary; raw customization is collapsed under
  "Advanced (native parameters)", with outcome cards and kit plots, not JSON result dumps.
  Flow fidelity in `sim_manager_flow.cpp` writes native `flow.cells/n_steps`:
  Quick preview 58/667 (new-study default in `sim_manager.cpp`), Standard 96/2000,
  Accurate 144/3000; show ETA and face-normal inlet direction. Accurate is still LBM,
  not OpenFOAM. Native flow display packaging is `core/sim/flow_run.cpp`:
  `pressure_cp`/`velocity`, `streamlines`/`slices`, optional `overlay:false`.
- **Family owners.** Structural/mesh cards: `sim_manager_forms.cpp`; flow controls:
  `sim_manager_flow.cpp`; mate drivers: `sim_manager_motion.cpp`; EM:
  `sim_manager_em.cpp`; circuit/board/heatsink/emulation: `sim_manager_electronics.cpp`.
  `sim/sim_fw_scenario.h` is the shared native scenario/event editor used there and by
  `firmware/fw_workspace.cpp`. Hydraulic forms/native editors live in `sim/hydraulic_forms.cpp`,
  canvas palette/gestures in `sim/hydraulic_canvas.cpp`, metadata/ports/mutations in
  `hydraulic_panel.cpp`, shared helpers in `sim/hydraulic_internal.h` (`ui::hydint`);
  piping endpoints/ports/presets and native definitions remain in `piping_panel.*`.
  Capture payload context once at drag start; source/study/panel generations fence delivery.
  Optional-backend absence keeps setup editable and blocks Run honestly; never substitute physics.
- **Queue scope.** `core::AsyncRpc::interactionBusy()` excludes only exact global metadata
  discovery/status methods. Document reads, mutations, solves and unknown methods still block.
  Simulation gesture/edit/run guards use it; scheduling, refresh and teardown retain `busy()`
  (all queued/executing/undrained work). Preserve both counters through callbacks and shutdown.
- **Result/model boundary.** Full simulation overlays load with `cacheAsModel=false`;
  keep the session's editable tessellation and parked GPU model intact. Same-document
  setup invalidation/deletion calls `restoreModelMesh` before dropping result visibility.
  Restore geometry and face-pick/overlay caches together; binding shaded GPU buffers
  alone leaves FEA meshes unpickable. A cached model bind cancels pending visible loads,
  and skips rebuilding/copying shaded mesh arrays already bound on the GPU.
- **Circuit palette seam.** Compiled schematic conversion is a native core diagnostic,
  not a services-worker method. Keep its explicit native-only route in
  `services/worker/worker_client.cpp`, and expose engine conversion errors in the palette.
- **Gates.** `CHUDWARE_SHOT=simaudit` must complete every registered state with 0 ID conflicts
  and 0 overflows, including real disclosure clicks and fully framed native editors.
  `CHUDWARE_SHOT=siminteractive` exercises actual mouse/keyboard drag/drop, atomic Apply/Revert,
  stale/busy/document fences and native parameter-to-solver fidelity. These are input-driven
  proofs, not injected payloads or command-only setup checks. `CHUDWARE_IDAUDIT=1` with
  `fea|flow|motion|topology|piping|hydraulics` covers the corresponding result surfaces
  (fea/flow print PASS and exit non-zero on failure). Backend-specific run coverage must be
  reported separately from setup/render coverage.
  Opt-in item snapshots retain `native_label` beside the human semantic label: footer
  actions have stable `###kit_af_primary` IDs even when a page tab has the same caption.
  Resolve that action identity rather than clicking the first matching tab. Read actual
  disclosure state offscreen and visit pinned input tabs once per navigation phase;
  repeated tab clicks reset the wheel search for long native editors.
  `pipedraw` and `hydraulics3d` are real-input self-checking scenarios registered in
  `app.cpp`'s `pointerShot` and `ui_audit.cpp`'s observed-item path. The `piping` shot
  also pixel-checks routed geometry visibility; do not replace it with a route JSON check.
  `CHUDWARE_SHOT=demoreview CHUDWARE_SHOT_DOC=<file.step+>` opens a COPY of a saved part,
  adopts every saved circuit/network/vessel/duct by name (`openStudyNamed`, the row-click
  path) and captures Schematic/Plots/3D and Solve/Spec/BOM/vessel/duct results into
  `scratch/demoreview_shots`; use it to prove a saved file reopens with all its studies.
  The scenario selects the source file's working directory, opens its scratch copy
  outside that directory and verifies the root stays unchanged. It enters Simulate
  with the Model column collapsed and waits for the normal tree draw to list studies
  (no direct refresh injection). Simulate, Manufacture and Electronics rails ignore
  Model's saved roll-up state; their bodies have no Model collapse control.


### Simulation: Topology tab

- **Ownership split.** `sim_topology.*` owns the scalar `topology{}` editor (method, physics,
  objective, scalar keys), run gating (`validateTopology`), and the results/compare panel
  (`drawTopologyResults` → `ResultsAction` ShowRun / ExportArtifact / ContinueInCad / ApplyRunSettings).
  `sim_topology_bc.*` owns everything outside `topology{}`: face wells (fixtures, loads,
  load cases, preserve/exclude faces or boxes, thermal fixtures/flux/power/convection,
  flow inlets/outlets/walls), the material and mesh checks (`validateBoundaries`), and
  `buildRunEnvelope`. Neither TU issues an RPC or touches files. `SimManager` handles the
  async `topology_capabilities` probe, one STEP export plus `doc_face_points` per unique
  selector, the `sim_run_topology` launch through SimClient (no timeout), retained runs,
  artifact missing/stale checks, the off-thread export copy, and viewport overlays.
- **One canonical study, shared with the model tools** (`services/mcp/topology_tools.py`,
  `services/sim/topology.py`). Editors change only the keys they display. Agent-authored
  keys, `runs`, `last_job` and sections the current physics does not use are kept, and a
  cleared field erases its key. Selectors carry viewport identity under `_pick`, which is
  stripped before resolution. Do not add a GUI-only dialect.
- **Capability truth.** Every method/physics/objective/limit/parameter offered comes from
  the native matrix. Unsupported pairs show the native `reason` and cannot run. Do not
  hard-code a pair list (SIMP/RAMP × 7 physics; BESO/level set × structural/thermal,
  volume only).
- **Results honesty.** Show `ok`/`converged`/`feasible`/`termination` separately. Surface
  exports (stl/3mf/obj) are offered only when `manufacturing.geometry_valid`. Runs on other
  geometry are `source_stale`; model-tool runs without the editor's geometry digest are
  `source_unverified`. A finished geometry-valid GUI run queues ONE async `topology_dfam`
  worker call (gui-background socket) and merges its `dfam` into that run record only;
  agent-run records and invalid geometry are never queued, and an unavailable check reads
  "DFAM check could not run", never passed. The panel never promotes topology output to
  print-ready, including legacy records claiming it: only `slice_part`'s passing G-code
  check establishes that. It never rewrites a retained run otherwise.
- **3D printing setup.** The Additive manufacturing card (Mfg. Rules focuses it) applies a
  `services/sim/topology_print_processes.json` preset (Apply overwrites offered keys; null
  erases; sets `print_process`) and holds the Bed face well (`build_plate`, preview in the
  preserve colour, forwarded by `buildRunEnvelope`). Overhang settings need a build
  direction OR a bed face; the solver derives the direction from the face. The print
  surface (`surface_print_stl`) is listed first and preferred by Continue in CAD; the
  overlay field `overhang` is labelled "Needs support".
- **Return to editable CAD.** Results → Result geometry and export → Continue in CAD
  asynchronously imports the valid, available, non-stale `surface_print_stl` (else
  `surface_stl`) as a separate
  solid, activates its document tab, clears the result overlay/legend, and enters Model /
  Feature Manager. Source geometry and retained studies are not mutated. Save the new
  part as `.step+`; the base remains faceted, not reconstructed sketch/NURBS history.
  Closed single-shell mesh import shares edges and merges coplanar faces, so planar pads
  can be selected as whole faces instead of individual cap triangles. Invalid geometry
  blocks continuation independently of convergence/feasibility; DFAM is not a CAD gate.
- **Shot:** `CHUDWARE_SHOT=topology` (`scenarios.cpp` `runTopology`) builds a 120×40×40 mm
  domain (PLA, `fdm_0.4`, min-Z bed face) and runs the study for real, asserting the print
  surface, the `overhang` overlay field and a checked DFAM for geometry-valid runs, writing
  `scratch/topology_shots/topology_report.json` plus captures (`topology_06b_print_simp.png`
  is a tall results capture with the DFAM rows).

## Auxiliary panels

`host/src/ui/doc_tabs.cpp` (multi-document tab strip) · `file_explorer.cpp` (working-folder
browser with filter, recents and vault changes/checkpoint strips) · `console_panel.cpp` (worker/debug log) · `status_bar.cpp` (job status,
spinner, sticky "Engine daemon unreachable — retrying" chip while `AppState::daemonUnreachable`;
tabs re-bootstrap via `engine_events` after hostd reconnect) · `title_bar.cpp` (window chrome + File/Edit/View/Help menus) · `config_panel.cpp`,
`design_table_panel.cpp`, `cutlist_panel.cpp` (configurations & tables) · `toast.cpp`
(notifications) · `scenarios.cpp` (scripted test harness — add a scenario if your workspace
gains a major new flow).

Explorer rendering uses `FsBackend::pollDirectory/pollStat/pollDefaultRoot`, never
blocking `list/stat/defaultRoot` or `mkdir` on the web. Snapshot state distinguishes
loading, errors, and confirmed absence. Directory requests coalesce; mutation and
workspace invalidation fence old callbacks. Keep filtered rows alive across nested
mutations, which can invalidate the browser cache during tree traversal.

Explorer layout, top to bottom: one header line (working-folder name, tooltip = full path,
right-click = folder actions; branch chip inline), a filter field (Ctrl+F; Esc clears) beside
New folder / Refresh / Collapse all / Reveal active document, the tree (all remaining height;
the workspace's children at top level, folders first, natural numeric-aware order, every
non-dot file with non-documents dimmed), then collapsible Recent and Changes strips pinned at
the bottom, both collapsed by default. Expansion lives in the Explorer's own set (Collapse
all / Reveal edit it); a click anywhere on a folder row toggles it. A non-empty filter shows a
flat result list from a bounded incremental `pollDirectory` BFS (4000 entries, depth 12).
Rows carry an open-in-tab dot, the active document's accent highlight, and change code + rev
at the right. Shot: `CHUDWARE_SHOT=explorer` with optional `CHUDWARE_EXPLORER_SHOT_EXPAND`,
`_SELECT`, `_QUERY`, `_STRIPS` (see `applyShotStateOnce`).

## Shared patterns

- Panel shape: `class XxxPanel { void draw(AppState&); void drawBody(AppState&); }`; global
  dialogs are `void drawXxxDialog(AppState&)`.
- Per-doc state → `AppState::DocTab`; global/singleton state → `AppState` members
  (`host/src/core/app_state.h`, integrator-owned — request seam additions, don't sprawl).
- Engine data arrives via the per-frame drain in `host/src/core/engine_events.cpp`; panels
  render from AppState, they don't call the worker synchronously. RPC via
  `WorkerClient::sendRpc()` (`contracts/worker_rpc.md`).
- Visual system per gui-theme-layout: colors via `ui_theme` only, widgets via `ui::kit` (or the
  workspace's thin-wrapper kit), sizes via `ui::metrics` density tokens, pill scrollbar
  (`ui::drawWindowPillScrollbarV`) and `ui::drawFractionRing` instead of re-rolls; icons from
  `contracts/icons.md`.
- Streaming logs stick to the bottom via `ui::chatLogStickToBottom("<region id>")` — a unique id
  per surface (two surfaces sharing one id fight over the flag).
- Contracts are append-only; schema changes are orchestrator-level decisions.

## Verification

Workers return files changed and the shots/tests below; the integrator builds and runs them per
gui-orchestrator. Headless scenario per workspace where one exists:
`CHUDWARE_SHOT=<asm|fea|flow|drawing|cam|manufacture|topology|workspaces|motion|firmware|reveng[_capture|_scale|_recon|_review|_model|_deliver]> ./host/build-linux/bin/chudware --ui-screenshot scratch/ws_smoke.png`.
Self-checking scenarios (e.g. `topology` writes `ok` into its report) are functional evidence;
a capture alone proves the flow ran — visual judgment stays with the user check. CPU pins:
`host/tests/test_cam_mirror.cpp`, `test_camera_backend.cpp`, `test_sim_topology.cpp`,
`test_magic_keywords.cpp`, `test_ask_answers.cpp`, `test_usage_rings.cpp`. User check example:
"Parts Library > type in search — results stream while typing, filters stay responsive."
