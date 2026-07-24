---
name: gui-workspaces
description: Chudware (formerly Billet / AI-CAD) workspace-tab specialist — Parts Library, Assembly panels, CAM, ECAD, PLM, Reverse-Engineering, AI/Activity workspace, Drawing, Simulation manager, plus doc tabs, file explorer, console and status bar. Use for any task about one of these workspace tabs or auxiliary panels. Workers dispatched by gui-orchestrator read this file first, then work only in their own workspace's files.
---

# GUI Workspaces Specialist

Scope: the tabbed workspaces and auxiliary panels around the modeling view. **Work only in
your assigned workspace's files** — e.g. an icon-generation task never needs the Parts Library
section below, and vice versa. Shared plumbing (app.cpp dispatch, AppState seams) is
integrator-owned.

The app has 8 workspaces cycled by the `CHUDWARE_SHOT=workspaces` scenario; each is drawn from
the frame loop in `host/src/app.cpp`.

## Per-workspace file map

| Workspace | Files | Notes |
|---|---|---|
| **Parts Library** | `host/src/ui/parts/parts_workspace.cpp/.h`, `parts_browser_panel.cpp`, `parts_filters.cpp`, `parts_results.cpp`, `parts_taxonomy_pane.cpp`, `parts_vendors.cpp`, `parts_widgets.cpp/.h`, `parts_internal.h` | Vendor search, taxonomy tree, thumbnails, streaming results. Data contracts: `contracts/parts.md`, `contracts/sourcing*.md` |
| **Assembly** | `host/src/ui/asm_panels.cpp/.h` | Mates, component insertion, BOM, exploded view |
| **CAM** | `host/src/ui/cam/cam_panel.cpp/.h`, `cam_mirror.cpp/.h` | Job tree, setups/ops/tools, simulation; mirror syncs engine CAM state |
| **ECAD** | `host/src/ui/ecad/ecad_panel.cpp/.h`, `ecad_canvas.cpp/.h`, `ecad_mirror.cpp/.h` | Schematic/PCB 2D canvas editor; schemas in `contracts/ecad/` |
| **PLM** | `host/src/ui/plm/plm_workspace.cpp`, `plm_pages.cpp`, `plm_hub.h` | Versioning, releases, document control; `contracts/plm.md` |
| **Reverse Eng.** | `host/src/ui/reveng/`: `reveng_workspace.cpp` (shell), `reveng_widgets.h/.cpp` (shared visual kit), `sources_panel.cpp`, `webcam_capture.cpp`, `stage_panels.cpp`, `dim_review.cpp`, `image_gallery.cpp`, `img_tex.cpp` | Six-stage scan → mesh → AI → parametric wizard (`contracts/reveng.md`). **Every panel draws with `ui::reveng::kit`** (`reveng_widgets.h`) and sizes itself from `ui::metrics::re*()` — no bespoke cards, no pixel literals. Camera seam: `host/src/platform/camera.h` v2. Splat/point-cloud rendering lives in `host/src/render/` (gui-viewport), fed by `kit::previewSlotRect`. Full breakdown below. |
| **AI / Activity** | `host/src/ui/ai/ai_workspace.cpp/.h` + `ai/{plan_board,docs_panel,active_work,metrics_view}.cpp`, `host/src/ui/activity_panel.cpp`, and `host/src/ui/copilot/` (the chat impl the workspace embeds via the reuse hooks at the bottom of `copilot/copilot.h`; the old `legacy_chat.cpp` monolith is GONE — see the module map in `copilot/copilot_internal.h`) | Agent run tree, role-prompt editor, live progress; Context Injection tab edits `aicad.trigger/1` via `triggers_*` RPCs; events per `contracts/agent_events.md` |
| **Drawing** | `host/src/ui/drawing_panel.cpp` | Sheet layout, views, DXF export |
| **Simulation** | `host/src/ui/sim_manager.cpp`, `host/src/ui/evaluate_dialogs.cpp` | Study tree, fixtures/loads, results; Evaluate dialogs (measure, mass props, …) |
| **Firmware** | `host/src/ui/firmware/fw_workspace.cpp/.h` | T-FIN5: project rail + sources, build console (parsed warnings), emulation + UART console, GPIO trace viewer (`contracts/firmware_rpc.md` §7/§8); fw_status-gated Flash/Debug; tab is progressively disclosed (`ui::firmwareTabVisible`); shot: `CHUDWARE_SHOT=firmware` |

### Reverse Engineering internals (rebuilt RE-40)

Eight TUs behind one tab. Read `reveng_widgets.h` before touching any of them.

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
  re-entry after a frame gap).
- `reveng_widgets.h` / `.cpp` — **the shared visual kit, namespace `ui::reveng::kit`. Every RE
  panel must draw with it**; this is the single most important convention in this workspace.
  It provides: `Level` + `levelColor`, `CardBegin`/`CardEnd`/`CardHeaderRight`/`SectionHeader`,
  `StatusChip`/`ValueChip`/**`HonestyTag`** (the approximate/photo-derived tier badge),
  `InlineStatus`/`HelpMarker`, form rows (`FieldLabel`, `KeyValueRow`),
  `PrimaryButton`/`SecondaryButton`/`DangerButton`, `SegmentedControl`, `ProgressBar`,
  `StepTrack`, `HeroPreview`, `ThumbTile`, `ThumbHoverPreview`, `Viewport3DPreview` +
  `previewSlotRect`/`resetPreviewSlots` (the seam app.cpp's post-ImGui GL pass reads for the
  `Splat` / `SparseCloud` slots), `EmptyState`, `BlockedState`, `CapabilityRow`, `Rule`,
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
  viewer. Thumbnails come from `reveng::cachedThumbTex()` — `img_tex` now has a **bounded
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
`services/reveng/worker_rpc.py`, which is now a *thin adapter* over the reveng MCP tool registry
(`services/mcp/reveng_session_tools.py`, `reveng_build_tools.py`, `recon_tools.py`): adding a
reveng tool exposes it to BOTH the agent and the GUI, and there is no second implementation to
keep in sync. Bridging one is a line in `_BRIDGED_TOOLS`. Only `reveng_start_from_product` is
still hand-written. Failure envelopes are always `{ok:false, reason, hint}` — surface `reason`
and `hint`, never a bare "failed".

**Headless shots** (`CHUDWARE_SHOT=<name> ./host/build-linux/bin/Chudware --ui-screenshot …`):
`reveng` (empty shell), `reveng_capture` (Source stage, canned session), `reveng_scale`,
`reveng_recon` (mid-run at 45 %), `reveng_review` (canned dims + deviation), `reveng_model`,
`reveng_deliver`. All but the first seed a canned session via `seedRevengCanned` in `app.cpp`.

## Auxiliary panels

`host/src/ui/doc_tabs.cpp` (multi-document tab strip) · `file_explorer.cpp` (workspace
browser, recents) · `console_panel.cpp` (worker/debug log) · `status_bar.cpp` (job status,
spinner, sticky "Engine daemon unreachable — retrying" chip while `AppState::daemonUnreachable`;
tabs re-bootstrap via `engine_events` after hostd reconnect) · `title_bar.cpp` (window chrome + File/Edit/View/Help menus) · `config_panel.cpp`,
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
- Scrolling children use the app's pill scrollbar, never ImGui's default: give the child
  `ImGuiWindowFlags_NoScrollbar` and call `ui::drawWindowPillScrollbarV(surf)` right before
  `EndChild()`. Streaming logs stick to the bottom via `ui::chatLogStickToBottom("<region id>")`
  (a unique id per surface — two surfaces sharing one id fight over the flag).
- Row/card gutters, corners and gaps come from `ui::metrics::cpMenu*`; compact
  "fraction of a budget" readouts use `ui::drawFractionRing`. See gui-theme-layout for both,
  including the logical-dp-vs-pixels double-scale trap.
- Contracts are append-only; schema changes are orchestrator-level decisions.

## Build & verify

`./build.sh` from repo root must compile. Headless smoke per workspace where one exists:
`CHUDWARE_SHOT=<asm|fea|flow|drawing|activity|workspaces|motion|firmware|reveng[_capture|_scale|_recon|_review|_model|_deliver]> ./host/build-linux/bin/Chudware
--ui-screenshot scratch/ws_smoke.png` — crash check only. Never run git. Return files changed
+ a one-line user check, e.g. "Parts Library > type in search — results stream while typing,
filters stay responsive."
