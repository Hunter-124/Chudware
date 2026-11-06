# GUI architecture

The desktop GUI is a native C++ Dear ImGui application. All production GUI code lives in
`host/src/`, which is the sole GUI source tree (the same tree compiles to WASM for the
browser build). This document maps the subsystems, the shared files that need care, the
headless scenario harness, and the conventions for the command surface, viewport, sketcher
and icons. Visual-system rules (colors, metrics, `ui::kit`, lint) are in
[gui-theme-layout.md](gui-theme-layout.md); workspace tabs are in
[gui-workspaces.md](gui-workspaces.md); the Evaluate tools are in
[evaluate-tools.md](evaluate-tools.md).

## Subsystem map

| Change concerns | Primary files |
|---|---|
| Icons, atlas, icon generation | `assets/make_icons.py`, `scripts/icon_pipeline.py`, `host/src/ui/icons*.cpp` |
| 3D viewport, rendering, picking, camera, HUD | `host/src/ui/viewport_panel.cpp`, `host/src/renderer.cpp`, `host/src/render/` |
| Ribbon, feature tree, PropertyManager, command routing | `host/src/ui/ribbon_def.cpp`, `host/src/app_commands.cpp`, `host/src/ui/widgets/ribbon.cpp`, `host/src/ui/feature_tree.cpp`, `host/src/ui/property_manager.cpp`, `host/src/ui/pm_forms.cpp` |
| 2D and 3D sketcher | `host/src/ui/sketch_mode.cpp`, `host/src/ui/sketch3d_mode.cpp` |
| Workspace tabs (Parts, Assembly, CAM, ECAD, PLM, RevEng, AI, Drawing, Simulation, Firmware, Manufacture) | see [gui-workspaces.md](gui-workspaces.md) |
| Theme, layout, DPI, modals, status/title bar, toasts | `host/src/ui_theme.cpp`, `host/src/ui/layout.cpp`, `host/src/ui/metrics.h`, `host/src/ui/settings_modal.cpp` |
| Shared widget/motion vocabulary | `host/src/ui/widgets/kit.h/.cpp`, `host/src/ui/anim.h/.cpp` |

## Shared files

These files are shared by many features; change them deliberately and keep edits small:

- `host/src/app.cpp`: frame loop, panel dispatch, app-level command hooks.
- `host/src/app_commands.cpp`: the ordered command router; registration order is semantics.
- `host/src/core/app_state.h`: the central data model. Per-document UI state belongs on
  `AppState::DocTab`; add narrow seams rather than sprawling members.
- `host/src/ui/ribbon_def.cpp`: append-only command table.
- `host/src/ui/metrics.h`: append-only sizing tokens.
- `host/src/ui_theme.cpp/.h`, `host/src/ui/widgets/kit.h/.cpp`, `host/src/ui/anim.h/.cpp`: the
  app-wide color, widget and motion vocabularies. Panels consume them; a new primitive is its
  own change, landed before the panels that use it (the same ordering as a new icon before
  the ribbon button that uses it).
- `contracts/`: append-only.

## Shared panel patterns

- Panel shape: `class XxxPanel { void draw(AppState&); void drawBody(AppState&); }`; global
  dialogs are `void drawXxxDialog(AppState&)`.
- Engine data arrives through the per-frame drain in `host/src/core/engine_events.cpp`; panels
  render from `AppState` and never call the worker synchronously. RPCs go through
  `WorkerClient` (`host/src/worker_client.h`; methods in `contracts/worker_rpc.md`).
- Streaming logs stick to the bottom via `ui::chatLogStickToBottom("<region id>")` with a
  unique id per surface (two surfaces sharing an id fight over the flag).

## Account-optional startup

Desktop onboarding offers **Start without an account** as the immediate path to Model:
local CAD needs no account, API key or internet connection. Copilot provider sign-in and
testing are optional during setup and remain available through Settings → Providers.
Completing the wizard marks the current session complete immediately; its durable config
merge runs on a background worker. Completion writes are serialized, with the latest queued
workspace choice winning, and are polled every frame even after the modal closes. A failed
write warns the user without blocking CAD; onboarding may appear again on the next launch.
The config-corruption warning drain waits until that background write finishes because it
shares the config-writer lock. The public persistence helper remains synchronous for
callers that need to know when the write has completed.

Deferred viewport focus must wait while any popup is open: ImGui window focus can dismiss
the first-run or recovery modal. Verify a normal fresh-profile launch as well as the
`onboarding` capture scenario, which deliberately reopens its modal every frame.

PropertyManager field parsing, validation, hydration and serialization live in
`ui/property_manager_fields.inl`, included once by the desktop implementation and once by
its headless regression executable. Keep desktop lifecycle/worker/rendering dependencies
out of this fragment; the regression must link on MSVC without relying on ELF section
garbage collection or desktop-symbol stubs.

Tab-switch timings go to `AppState::addLog` and the in-app console, not unconditionally to
stderr on the GUI thread. `CHUDWARE_LOG_FILE` can opt into the existing file tee when
diagnosing an issue; that tee itself performs file I/O.

## Verification: lint and the scenario harness

```bash
./build.sh                                   # or: ninja -C host/build-linux Chudware
python3 scripts/gui_lint.py                  # all rules; see gui-theme-layout.md
CHUDWARE_SHOT=<scenario> xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/<scenario>.png
```

The ninja target is `Chudware`; the binary is lowercase `chudware`. `CHUDWARE_SHOT` runs a
scripted native scenario from `host/src/ui/scenarios.cpp` headlessly through the real engine and
writes a capture; drop `xvfb-run -a` and set `CHUDWARE_SHOT_HEADED=1` for a visible window.
The browser build does not include this harness: its entry point skips the desktop screenshot
CLI and launches the ordinary GUI. Browser checks exercise the web client separately.
Common scenarios: `m1` (part build, edit, undo: viewport, tree, PropertyManager), `sketch`,
`sketch3d`, `appearance` (theme/glass), `workspaces`, `manufacture`, `topology`, `iconpipe`,
`reveng_splat`, `presentation`, `settings_routes`, `model_settings`, `text_contrast`.
`CHUDWARE_IDAUDIT=1` adds the ImGui duplicate-ID/clipping audit to any scenario.

A scenario is the functional gate: it drives the changed path end to end. A capture's
existence is not visual proof; inspect the changed surface. Self-checking scenarios (for
example `topology`, `pipedraw`, `simaudit`) write reports and exit non-zero on failure. When
a workspace gains a major new flow, add a scenario for it.

`text_contrast` places real selectable Copilot prose across the edge of a shaded mesh,
checks dark/light portions of a single glyph, header labels and model metadata against
same-frame text-free/font-coverage references, then moves the camera and exercises native
selection and clipboard copying. Run with private HOME/XDG paths and
`CHUDWARE_ENGINE_DAEMON=0`; `CHUDWARE_THEME=light|dark` and `CHUDWARE_SCALE=1|2` cover the
theme/DPI matrix. `CHUDWARE_TEXT_CONTRAST_OUT` selects the report/capture directory (default
`scratch/text_contrast_shots`); an explicit `CHUDWARE_SESSIONS_DIR` must live inside it.

`presentation` uses a real daemon and a second MCP seat to exercise hidden, explicitly
presented and user-opened live documents; use an isolated daemon instance and private
paths. `settings_routes` uses private HOME/XDG paths and `CHUDWARE_ENGINE_DAEMON=0`;
it exercises Quick Settings → Prompts and keyboard/click search navigation across the
eight AI sections, reporting to `scratch/settings_routes_shots/report.json`.

`asm_refresh` exercises unsaved component edits through the actual active/background
assembly meshes, selection preservation and editor deferral. Run both
`CHUDWARE_ENGINE_DAEMON=1` and `0` with private HOME/XDG/session paths and a private daemon
socket. `CHUDWARE_ASM_REFRESH_OUT` selects its captures and `report.json`.

The Simple, Advanced and composer model pickers share the runtime catalog and
authenticated provider state. Catalog presence alone does not enable a provider; hidden
models, disabled providers and unloaded local models are excluded. Claude older than 5.1 and
OpenAI models older than GPT-6 (including codex variants and the o-series) are never offered
on any provider route (`ui::isLegacyGenerationModel`, `host/src/ui/model_registry.h`, applied
in `bridgePickerModelUsable`, `providerPickerModels` and Settings -> Providers -> Models); a
saved selection of one still displays as the current value. An existing fixed
assignment remains visible by provider/model ID if it becomes unavailable, and is not silently
replaced. Online role assignments preserve the runtime provider ID even when multiple
providers share a composer slot. Legacy native-only engineering-firm model assignments
have no online consumer and are not exposed as GUI model controls.
Model assignment controls remain usable with the last confirmed state during a background
refresh. A save supersedes any in-flight refresh response; an initial load, an unavailable
assignment service, or an unsaved/in-flight write still protects the controls from edits.
Failed saves retain their draft until retried or discarded.
Provider and model discovery (`omp_provider_list`, `omp_models_list`), sign-in/out and key
save, `ai_roles_*`, `subscription_*` and `skills_*` are served by hostd's own service worker, so
they answer live during an agent run. A refusal with `worker_lane_busy` can still happen — an
interactive sign-in holds that worker, or the daemon is an older build whose agent lane takes
them — and it is a deferral, not a failure: no error state is set, the last data
stays, and the 5 s poll asks again until the lane is free. A complete live read of the catalog
(`model_catalog_cache.json`) and of the provider list (`provider_list_cache.json`, beside
`config.json`) is kept on disk; only while a run defers the live read does the next launch
show those saved copies (labelled "last session", `modelPickerDeferred()`), so the picker and
Settings -> Providers are not blank. Otherwise availability still needs a live confirmation.
Provider detail (`omp_auth_state`) retries every 3 s until it loads, then every 60 s; a busy
lane keeps the loaded detail instead of replacing it with an empty one. Model assignments
(`ai_roles_get` / `ai_roles_set`) are agent-lane calls too: a busy lane keeps the loaded
assignments (a status line says they refresh after the run), and a refused save keeps its draft
for Retry / Discard. Every reply must carry an explicit `ok: true` to count as a read — a
refusal envelope has no `ok`, and reading that as success used to blank the assignments. The
catalog lists only signed-in providers, so it is re-read within 5 s when the signed-in set
changes (sign-in, key, logout from any surface), not on the 5-minute timer.
Model popups are one compact popover shared by the chat model, the Small / Medium / Large
tiers, per-role overrides and Copilot Quick settings: search on top, a list bounded to about
ten rows that scrolls inside the popover (tier choices first, then models grouped under
provider headers, the current choice highlighted and scrolled into view), and
refresh/provider actions outside the list, with an opaque background and keyboard navigation.
Each tier also carries a thinking effort (Auto, or a level the tier's model reports) in both
surfaces. Per-role overrides are off by default (Settings -> AI -> Models -> Advanced
overrides -> "Use per-role overrides"): while off every role dispatches through its tier and
saved overrides are kept but ignored; an override picks a tier (Small / Medium / Large) or a
concrete model, never "follow the chat model". Generic local endpoint probes are
offline choices, not invented online OMP provider identities.

`model_settings` exercises Simple/Advanced switching, authenticated catalog model choices,
sign-out removal, disabled/hidden models, exact provider/model assignment round trips, the
Large model picker, scrolling/filtering, keyboard selection, provider navigation, and the
composer choice between a manual pin and following the chat assignment. Its isolated
process uses synthetic provider-auth state and explicitly synthetic
catalog rows for Claude, Codex, ZenMux and other providers. These fixtures verify UI filtering
and exact provider/model round trips, not real model availability; validate runtime catalog
identities separately using credential-free metadata or an isolated runtime profile.
It checks RPC, reloaded state, the actual turn payload, and save-failure retry/discard through
the private config lock's real permissions. It restores the original lock permissions before
exit and requires an unprivileged user whose mode restrictions actually deny writes. It also
captures both themes. The Default assignment follows the last manual chat model; a manually
chosen model remains an explicit override. Run it with an isolated data directory and daemon:

```bash
APPDATA="$PWD/scratch/model_settings_private" HOME="$PWD/scratch/model_settings_private/home" \
XDG_DATA_HOME="$PWD/scratch/model_settings_private/xdg" \
CHUDWARE_SESSIONS_DIR="$PWD/scratch/model_settings_private/sessions" \
CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=model_settings \
env -u CHUDWARE_THEME xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/model_settings.png
```


Settings deep-link routes 8–15 are Harness, Roles, Prompts, Skills, Rules, Benchmarks,
Tools and Dev Preview, respectively (`host/src/ui/settings_modal.h`).

Run build commands one at a time; parallel `./build.sh`/`ninja` runs race on the Ninja lock.

### GUI frame statistics

`host/src/core/frame_stats.*` records a fixed 1024-frame history of steady-clock wall,
event-wait, CPU-work and swap times. CPU work excludes both the idle event wait and
`glfwSwapBuffers`; these are CPU-side timings, not GPU timer queries. Sections are exclusive
(nested scopes do not double-count), with unattributed work reported as `other`.
The Performance window shows presented FPS over the last second, work-time p50/p99,
the idle-throttle state, a work-time plot and a section table alongside the sampling profiler.

Set `CHUDWARE_FRAMESTATS=/absolute/path/report.json` to export full-run and per-phase
percentiles on exit, plus the last 64 work-time spikes and their top three sections.
The normal closed-profiler path uses fixed storage without per-frame allocations; optional
full-run export retains the additional samples needed for exact whole-run percentiles.
A spike exceeds both twice the preceding rolling work-time median and 8 ms.

`workspace_transition` separates disposable-starter cleanup from panel drawing. Entering
Electronics presents the KiCad project hub without creating a document. Tab timing breadcrumbs use `AppState::addLog` (the in-app console and
explicit `CHUDWARE_LOG_FILE` tee), not unconditional stderr writes/flushes on the GUI thread:
slow redirected output can stall a Windows tab change even when its cached mesh bind is fast.

`CHUDWARE_SHOT=frameperf` builds a real box with four corner fillets and nine through-holes,
waits for idle, then measures 300 orbit frames, 300 mouse-hover/picking frames, 300 frames
with a 160-message Copilot transcript and 40 frames in each of the eight standard workspace
routes. Setup and final capture have separate phase labels. It needs no AI provider.
By default its JSON is written to `CHUDWARE_REPO/scratch/frameperf/framestats.json`;
`CHUDWARE_FRAMESTATS` overrides that path. Isolate both application state and the working
directory, as `scripts/gate.sh` does:

```bash
REPO="$PWD"
STATE="$(mktemp -d /tmp/chudware-frameperf.XXXXXX)"
mkdir -p "$STATE/home" "$STATE/work"
(
  cd "$STATE/work"
  xvfb-run -a env HOME="$STATE/home" APPDATA="$STATE/data" XDG_DATA_HOME="$STATE/data" \
    CHUDWARE_NO_FILE_DIALOGS=1 CHUDWARE_INSTANCE_KEY="frameperf-$$" CHUDWARE_REPO="$REPO" \
    CHUDWARE_SHOT=frameperf CHUDWARE_FRAMESTATS="$REPO/scratch/frameperf/framestats.json" \
    "$REPO/host/build-linux/bin/chudware" --ui-screenshot /tmp/x.png
)
```

For automated tests, use the isolated suites described in
[Running tests](../../CONTRIBUTING.md#running-tests).

As with the gate, set `CHUDWARE_PYTHON` and `CHUDWARE_LIBCHUDWARE` when the runtime or
libchudware build cannot be discovered automatically.

The JSON root is a summary plus `phases`, an object mapping phase names to the same summary
shape (without another `phases` field). A summary contains `frame_count`, `busy_frames`,
`idle_frames`, `spike_count`, `frame_ms`, `work_ms`, `wait_ms`, `swap_ms`, `sections` and
`spikes`. Every `*_ms` distribution contains numeric `p50`, `p95`, `p99`, `max` and `mean`
in milliseconds. `sections` maps static section names to those same distribution fields
plus `frame_count` (frames entering that section, summing repeated invocations per frame).
`spikes` retains up to the last 64 entries in chronological order, each with zero-based
`frame_idx`, numeric `work_ms`, string `phase` and up to three `top_sections` objects
containing `name` and `ms`. Per-phase summaries retain their own last 64 spikes.

Viewport sub-sections distinguish target allocation, background, faces, edges, datums,
shadow, resolve, backdrop blur and luminance readback. These are CPU submission timings:
first-use driver compilation may be charged to a draw, not to shader linking. Native
startup warms draw-state/sampler variants in an offscreen target and warms the ImGui
backend without swapping; neither warm-up geometry nor luminance reaches the UI.

### Frame pacing and redraw rules

The loop renders on demand. It idles in `glfwWaitEventsTimeout` (50 ms floor, 250 ms while
iconified/hidden) unless something declares work; presented FPS therefore varies with
activity by design. Judge performance by work-time ms, not by FPS.

- Interactive tweens call `ui::anim::requestFrames()` while moving. Decorative motion (brand
  marks, spinners, shimmer, pulses, fades) calls `ui::anim::requestFramesAt(30)`, which bounds
  the wait without pinning the loop to vsync. Never animate from `ImGui::GetTime()` without one
  of the two: the motion then steps at whatever cadence other work happens to produce.
- Input queues add three settle frames so popups, tooltips and auto-sized windows finish
  their measuring frames immediately.
- Worker threads (engine connection/bootstrap, AsyncRpc completion, mesh loader, SimClient,
  CEF, background file and image loaders) call the thread-safe `ui::anim::wakeLoop()` when
  results land; nothing polls for them.
- The busy gate uses `AsyncRpc::interactionBusy()`. Background/metadata polls are classified in
  `host/src/core/async_rpc.cpp`; add a new periodic poll's method there, or it holds the loop at
  vsync while in flight. `callPrimary` keeps primary-connection identity (attach/subscribe/
  detach) on the FIFO; `callCoalesced` replaces a queued same-key call in place (debounce is
  capped at twice its value so it cannot stall the queue).
- GL-thread code never makes a blocking engine call per frame, per drag step or per event; use
  `st.rpc`. `EngineClient::call` holds the connection mutex for the whole round trip.
  Native `engine::GuiConnection` owns initial discovery/spawn/hello and proactive reconnect
  on a persistent worker, retaining the final daemon-vs-inproc verdict, drain refusal and
  reconnect/respawn backoff. Until that verdict, GUI `daemonMode()` reports daemon intent
  and `client()` is null; background callers still wait for the final backend choice.
  Synchronous GUI RPCs fail promptly with `engine_starting` until publication; daemon
  intent is never proof that a client exists. Shutdown cancels startup/reconnect waits
  and aborts their transport before joining. The version probe and drain RPC are bounded,
  and cancellation terminates only an owned probe/spawn helper, never the shared daemon.
  Managed-worker launch waits for that choice so its backend pin cannot diverge, then
  spawn/hello and fallback core loading run asynchronously too. Bootstrap RPC completions
  remain on the GUI callback drain; initial connection and startup never block a frame.
  When daemon mode wins, a missing managed worker does not dlopen a redundant private
  OCCT core: the shared engine already serves C++ RPCs, and the dynamic-loader lock can
  otherwise block unrelated GUI thread creation.
  Core availability release/acquire-publishes immutable metadata after loading. Runtime
  repair is disabled/rejected while worker boot is pending, so it cannot stop or replace
  a child still being started. Readiness-dependent shot actions run once at or after their
  earliest frame, rather than requiring boot to finish on an exact frame number.
- Viewport frame reuse: `viewport_panel.cpp` skips the scene, overlays, resolve and backdrop
  when `Renderer::contentEpoch()` and its overlay-input signature are unchanged. Renderer
  setters must bump the epoch only on an actual change. A new overlay input drawn between
  `beginOverlay`/`endOverlay` must be added to that signature, or the state must be excluded
  from reuse; otherwise the viewport shows a stale frame.
- Config writes (`saveSettings`, `updateConfigJson`) run on a background writer with
  read-your-writes via `readConfigJson`. Call `flushConfigWrites()` before an RPC or process
  launch that reads the file; it returns immediately when nothing is pending.
- Chat persistence serializes and fsyncs on the writer thread; only shutdown and history load
  wait for it. The raw JSONL archive keeps full thinking rows but coalesces streaming
  checkpoints until their detail has doubled; the completed row is always written. A crash
  can therefore leave the archive at an earlier checkpoint, while the more frequent bounded
  session snapshot remains the normal reload source.

## Ribbon, feature tree and command routing

| File | Role |
|---|---|
| `host/src/ui/ribbon_def.cpp/.h` | Append-only `RibbonCmd` table plus `ribbonCommandCapability()` |
| `host/src/ui/widgets/ribbon.cpp/.h` | Ribbon widget: tabs, groups, big/small buttons, flyouts |
| `host/src/ui/feature_tree.cpp/.h` | FeatureManager tree: hierarchy, suppress/reorder, rollback bar, badges, context menus, viewport-to-tree sync |
| `host/src/ui/property_manager.cpp` | PropertyManager shell (per-doc form state); `PropertyManager::openForCommand` |
| `host/src/ui/pm_forms.cpp` | Per-feature form schemas |
| `host/src/ui/command_search.cpp` | Command palette over the ribbon table |
| `host/src/app_commands.cpp/.h` | `buildRibbonCommandRouter`: ordered rules mapping ids/prefixes to modules; `CommandRouter::authorize` |

`RibbonCmd` fields: `cmd_id` (unique snake_case), `label` (12 characters fit a big button),
`icon` (bare name from `contracts/icons.md`), `tab`, `group`, `big` (32 px icon over label vs
20 px beside it), `sub_of` (parent id for flyouts), `stretch` (reserved, renders disabled), and
`capability` (`Mutate`, `Inspect`, `Export`). Append rows; never edit or reorder existing
ones. `capability` is fail-closed: unclassified ids are `Mutate`, which read-only mode blocks.
Mark a row `Inspect` or `Export` only when it strictly never mutates the open document.

**Command flow.** Ribbon click (or shortcut bar) → `dispatchCmd` in `app.cpp`
→ `CommandRouter::authorize` → `CommandRouter::dispatch` walks rules in order until a handler
consumes the id (a module entry point such as `SketchMode::onRibbonCommand`,
`PropertyManager::openForCommand`, `AsmPanels::handleCommand`, or an app-level hook) → engine
RPC over `WorkerClient` → `chudware-hostd` executes in libchudware → `doc.changed` events
drained per frame → admitted `AppState`/`DocTab` update → tree and viewport repaint.
Shared engine documents are not automatically tabs: admission is limited to GUI-origin,
intentional user-open/recovery, or explicitly presented documents. `show_in_app` requests
`doc_present`, without saving; a native user-open reuses the live document and keeps its
selection and updates. Dismissed tabs remain dismissed. See
[`HOSTD.md`](../reference/HOSTD.md#live-documents-and-presentation).

**Adding a command.**

1. Ensure the icon exists in `contracts/icons.md` (otherwise add it first; see Icons).
2. Append a `RibbonCmd` row (tab, group, `sub_of` for flyouts, capability only if genuinely
   Inspect/Export).
3. Route it. Most ids reach their module through an existing prefix rule (`sk_`, `dwg_`, `asm_`,
   `cam_`, `reveng_`, `sim_`, `eval_`); a new family needs a rule in `buildRibbonCommandRouter`
   with the fall-through cases audited.
4. A parameter form goes in `pm_forms.cpp`; op fields must match `contracts/feature_params.md`
   and `contracts/doc_ops.md` (both append-only).
5. Tree nodes render from the document's feature list automatically once the op lands.

**Assembly component tree** (`feature_tree.cpp` `drawAssemblyTree`). Each component expands
into a nested, read-only feature tree (a Reference folder plus history features with sketches
nested under their consuming feature), fed by `ui::componentFeatures()`
(`component_features.*`). Only native `.step+` components populate it. Component context menu:
**Edit** (placement/mates) and **Edit Model** (in-context edit via
`AppState::enterComponentEdit`).

**Viewport-to-tree sync.** `ui::featureTreeRevealPick` is called from the selection observer
as soon as a pick is added, never gated on the `doc_resolve_pick` reply. Part documents select
the owner-map feature; assemblies (which have no engine owner map) use the resolve reply's
`owner_feature` through `featureTreeNoteAssemblyPick`.

## Document edits, saves and recovery

Dirty state is keyed by document identity, not the selected tab or the document's
creator. `AppState::markHumanEdit(docId, revision)` records a human edit;
`markSaved` clears it only when the saved revision covers the human edit, and
`applyHumanDirty` incorporates hostd's document-scoped attribution. Metadata has
its own revision so an older save or notification cannot erase a newer human
metadata edit. AI/system-only edits do not create a human save prompt. Drawing
and ECAD tabs use their own document identities as well.

Human mutations maintain per-document pending barriers (`beginHumanMutation` /
`endHumanMutation`). Close, quit, save and conflict handling must not treat an
unsettled human request as safely saved or discardable. Saves complete
asynchronously against the captured document id and revision, not whichever tab
is active when the response arrives; later edits remain dirty. Agent automatic
saves skip human-dirty documents. In-process daemon-disabled fallback cannot
atomically guard agent saves against concurrent human edits.

For an external disk change hostd reloads a clean document; a human-dirty tab
instead shows **File changed on disk** with **Reload (discard my edits)** and
**Keep mine**. Reload waits for pending human mutations; Keep mine authorizes a
subsequent overwrite only against the observed stamp, which is checked again at
publication. Recovery prompts list human work; AI-only recovery is handled by
hostd when its original file still matches the snapshot's base stamp. Legacy
snapshots without human-dirty provenance are treated as human work.

Document events must refresh active and parked tabs without opening or focusing
unrequested documents. A same-revision `doc.cache_refreshed` can carry a new
mesh and must not be discarded merely because the revision is unchanged.
Invalidate stale parked mesh identities and update dependent assemblies as
source geometry changes; a tab switch must not restore an older mesh or tree.

## Viewport

| File | Role |
|---|---|
| `host/src/ui/viewport_panel.cpp/.h` | Input, gizmo, selection UX, per-frame 3D draw |
| `host/src/renderer.cpp/.h` | OpenGL renderer: orbit camera, mesh render, shaders, transparent thumbnail path |
| `host/src/render/id_pass.cpp/.h` | ID-pass picking: face/edge IDs to an offscreen buffer, read back under the cursor |
| `host/src/render/overlays.cpp/.h` | Native GL overlays: selection washes, simulation indicators, CAM path batches |
| `host/src/ui/sim/sim_scene.h/.cpp` | Viewport-to-simulation scene dispatcher (`ui::simscene`) |
| `host/src/render/splat_renderer.cpp/.h`, `re_points.cpp` | Gaussian splat and point-cloud rendering |
| `host/src/render/aicm_loader.cpp`, `host/src/mesh.cpp` | `.aicm` mesh loading and host mesh data |

**Data flow.** libchudware tessellates the document into an AICM file → `doc.changed` →
`AppState::loadDocMesh`/`loadDocMeshAsync` feed the renderer, ID pass and overlays → a click
reads back an ID → a `render::PickHit` joins `AppState::selection` → PropertyManager and
feature ops consume it. Background tabs keep their parsed mesh on `DocSession::cachedDocMesh`.

- **Picking is ID-based** (topology IDs from libchudware's tessellation), never a raycast
  against triangles. Anything pickable needs an ID-pass representation.
  Thin targets use depth-tested single-pixel geometry plus a screen-space search radius; the
  search rejects rear components behind the opaque surface under the cursor. Hidden bodies are
  skipped. Active mate references are drawn translucent and picked behind opaque components,
  while their unobstructed geometry remains selectable.
- The transparent thumbnail path in `renderer.cpp` is used by the icon pipeline; changing its
  background/alpha behavior breaks icon generation.
- **Framing.** The 3D image is full-bleed behind the glass panes. `viewport_panel.cpp` computes
  the clear region between the overlay columns each frame and calls
  `Renderer::setFramingRegion(shiftX, shiftY, scale)` (a lens shift; picking and overlays
  follow). Wheel/area zoom pivot on that region's centre.
- **Focus and popups.** A deferred viewport focus request remains pending while any popup is
  open at any level; applying it earlier can dismiss first-run or recovery modals.
- Pane controls that stand for a face call `ui::claimViewportHoverFace(body, face)` every
  hovered frame; UI code never writes `AppState::hover` directly.
- **CAM overlays** are drawn by `viewport_panel.cpp` through `Overlays` inside the renderer's
  colour FBO: Manufacture-only, excluded from the ID pass, section-clipped and depth-aware.
- **Simulation scene seam.** `ui::simscene` dispatches to the open panel's piping or hydraulics
  scene; the viewport never reaches into those panels. Collection and drawing run without a
  document mesh (empty parts are valid network canvases). Armed tools consume only clicks below
  ImGui's drag threshold; drags, MMB and wheel remain navigation. Scene pointer hover uses
  `ImGuiHoveredFlags_NoNavOverride`. Auxiliary scene bounds enlarge framing without moving the
  orbit centre; zoom clamps use `maxSceneZoom`, not a hard-coded limit.
- **Bottom-anchored HUD cards** publish their rectangle with `publishViewportHudRect()` so the
  orientation triad and dimensions readout lift above them.
- **In-context component edit.** Edit Model makes the component's part document active and draws
  the assembly as a translucent ghost (`captureCurrentAsGhost()` reuses the uploaded GL
  buffers; `setGhostTransform` receives the inverse component placement). The ghost is
  excluded from picking. Part edits push new assembly snapshots without saving. Exit saves
  the component asynchronously and returns to the assembly without forcing a second rebuild.
- **Gaussian splats.** The loader accepts validated PLY (log scales, wxyz quaternions, opacity
  logits, SH degree 0-3), projects full anisotropic covariance, and caps at 2 000 000 Gaussians
  and 2 GiB input. Camera motion updates only sorted indices. Image-plane NDC is clamped to
  ±1.3 before differentiating, matching Brush. Worker threads parse files but never touch GL.
  Real-GL smoke: `CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=reveng_splat CHUDWARE_REPO="$PWD"
  ./host/build-linux/bin/chudware --ui-screenshot scratch/re.png`; CPU contract:
  `host/tests/test_splat_renderer.cpp`.
- Headless render modes for testing: `chudware --render <in.stl> <out.png>` and
  `--contact <in.stl> <out.png>`.

## Sketcher

| File | Role |
|---|---|
| `host/src/ui/sketch_mode.cpp/.h` | The 2D sketcher: tool state machine, gestures, entity drawing, relation glyphs, smart dimensions, trim/extend/offset/convert/mirror/pattern |
| `host/src/ui/sketch3d_mode.cpp/.h` | 3D sketcher: free-space curves in world coordinates |
| `host/src/ui/sketch_relations_panel.cpp/.h` | Display/Delete Relations panel over a plain `RelationRow` model |
| [`contracts/interaction.md`](../../contracts/interaction.md) | The gesture language (click/drag/hover semantics) |
| [`contracts/sketch3d_gui_contract.md`](../../contracts/sketch3d_gui_contract.md) | Frozen 3D-sketcher UX spec |

- The sketcher sends ops to `chudware-hostd`, where libchudware owns the constraint solver;
  solved geometry returns through the event drain. Touching endpoints are auto-coincident on
  every edit, so UI flows do not add redundant coincident constraints.
- Each tool is a state-machine branch keyed by its ribbon command id; the tool set mirrors the
  Sketch sections of `contracts/icons.md`. New gestures need an appended entry in
  `contracts/interaction.md`.
- Relations to model geometry read resolved selections from `st.selection` (reuse
  `item.selector`), project them with one associative `sketch_convert` (construction, fixed),
  and add the relation in the same batch. World axes parallel to the sketch u/v map to the
  solver's virtual `axis.h`/`axis.v`.
- Performance: batch picked refs into one op, never block on `resolvePick` when the selection
  already carries a selector, and keep per-frame work dirty-flagged.
- Inactive sketches ghost in the viewport unless a built feature consumes them. Consumption has
  one definition, `core::consumedSketches` (`host/src/core/sketch_consumers.h`), shared with the
  FeatureManager's sketch nesting. Ghost frames come from the engine's `doc_sketch_overview`,
  which already applies the sketch's `params.offset`; never add it again in the host.
- `CHUDWARE_SHOT=sketch` prints solved coordinates; headless captures frame sketches too
  closely, so judge overlays from the printed numbers plus popup/panel captures.

## Icons

**Procedural line-art icons** come from `assets/make_icons.py` (PIL, deterministic, so
regeneration is diffable): drawn at 8x supersample into `assets/icons/atlas_dark.png`,
`assets/icons/atlas_light.png` and `assets/icons/atlas.json`, at 32 px (big buttons) and 20 px
(small buttons, tree rows), named `name@32`/`name@20`. **Both atlases share one pixel layout**,
so a theme switch swaps the texture while UVs stay constant. Helpers: `assets/pack_icons.py`,
`assets/test_icons.py`, `assets/test_pack_icons.py`.

**Screenshot icons** come from `scripts/icon_pipeline.py`: a recipe drives the real engine
headlessly (`CHUDWARE_SHOT=iconpipe` with `CHUDWARE_ICON_RECIPES=<json>`), renders through the
transparent thumbnail path, and fits each render into its cells in both atlases, leaving every
other cell byte-identical. Run `python3 scripts/icon_pipeline.py --render` (`--only a,b`,
`--list`, `--inject-only`). Icons carry no baked background. CAM tiles
(`scripts/cam_toolpath_icons.py`) render real `cam_generate` toolpaths gated by `cam_simulate`;
an icon is never faked. A failed build step yields an empty tile on
`scratch/icon_contact_sheet.png`, which should be regenerated for review.

**Runtime.** `host/src/ui/icons.cpp` loads the atlas (`getIcon(name, size)`), `syncIconTheme()`
swaps it on theme toggle, and `icons_sdf.cpp` rasterizes line art crisply at any DPI. Consumers
use bare names; size is chosen by context. The build copies the atlas to
`host/build-linux/bin/assets/icons/`.

**Adding an icon.** Append the name to [`contracts/icons.md`](../../contracts/icons.md) (names
never change or disappear), add a draw function or recipe, regenerate, confirm both `@32` and
`@20` cells exist in both atlases, reference it by name, and run
`python3 scripts/gui_lint.py --rule icons`.
