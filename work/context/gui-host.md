# host/src — sole GLFW+ImGui+OpenGL desktop and web GUI

Scope: host/src/**, host/tests/**, host/CMakeLists.txt, host/build-linux
Contracts:
- contracts/worker_rpc.md: worker JSON-RPC.
- contracts/doc_ops.md: modeling commands.
- contracts/icons.md: icon names.
- contracts/interaction.md: interaction rules.
Canonical guidance:
- `.claude/skills/gui-orchestrator/SKILL.md`: dispatch, hot files, verification handoff.
- The `gui-*` specialist skills it links.

## Entry points
- `host/src/main.cpp` `main()` — CLI modes (`--ui-screenshot`, `--render`, `--contact`, …).
  Desktop calls `run_gui()`; WASM uses the Emscripten boot path.
- `host/src/app.cpp` `run_gui()` — window/context bootstrap and the per-frame loop: input →
  commands → panels → renderer. It is integrator-owned.
- `host/src/core/app_state.h` `AppState` — the central data model. Its `Workspace` enum is
  append-only.
- `host/src/core/{doc_mirror,async_rpc,jobs,sim_client,doc_tabs,selection}.*` — the GUI
  side of the engine seam. Calls go through `WorkerClient` (work/context/worker-bridge.md).
- `host/src/renderer.cpp` `Renderer` — OpenGL 3.3 / WebGL2 viewport: `drawCurrent`, the
  section caps, the thumbnail path, and `backdrop()`/`averageLuminance` for liquid glass.
- `host/src/ui_theme.cpp` — the liquid-glass theme API: `glassPanel`, `glassRowBackground`,
  `adaptiveSurface`. Colors come only from here.
- `host/src/ui/widgets/kit.{h,cpp}` — the `ui::kit` widget vocabulary that panels compose.
- `host/src/ui/layout.cpp` — the pinned FeatureManager (left) and Copilot (right) glass
  columns, doc tabs and ribbon. `overlayColumnEdge(slot)` publishes the column edges.
- `host/src/ui/scenarios.cpp` `ScenarioRunner::tick` — the screenshot harness. Scenarios
  include m1, sketch, sketch3d, asm, fea, cam, evalmass, multidoc, tabperf, drawing,
  appearance, onboarding, iconpipe, reveng_splat, workspaces, motion, topology and
  manufacture.
- `host/src/render/aicm_loader.*` — the one live AICM mesh parser; core tests reuse it.
- `host/CMakeLists.txt` — FetchContent dependencies (GLFW 3.4, Dear ImGui v1.91.6-docking,
  stb, nlohmann/json), vendored glad. The target is `Chudware` and the output binary is
  `bin/chudware`.

## How it connects
`host/` is the sole GUI target, and the same tree compiles to WASM for the web app. Pinned
glass columns float over the full-width viewport, with adaptive text contrast and per-group
tint settings. Engine calls go through `WorkerClient` to hostd/libchudware.

## Debugging playbook
- Build: `./build.sh` (host/build-linux, ninja target `Chudware`). Never run two builds
  against one build dir concurrently.
- Run: `./host/build-linux/bin/chudware`.
- Screenshot harness: `CHUDWARE_SHOT=<scenario> CHUDWARE_REPO="$PWD" ./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png`.
  - Per-step PNGs go to `scratch/<scn>_shots/`. Scripted launches use `xvfb-run -a`
    when available unless `CHUDWARE_SHOT_HEADED=1`; the binary itself does not add xvfb.
  - It is a functional crash/behavior check; the user judges visuals.
- Host unit tests build into `host/build-linux/bin/test_*` (e.g. `test_switch_hold`,
  `test_worker_long_run`) and run via ctest in `host/build-linux`.
- Lint: `python3 scripts/gui_lint.py --rule theme`, `--rule icons` or `--rule dupe`.
- Gotcha: viewport HUD clip and sketch overlays dodge the columns via
  `ui::overlayColumnEdge(0/1)`. The edge is frame-stamped, so a workspace that skips
  `drawLeftPane` publishes a stale edge.
- Gotcha: there is no separate PropertyManager tab. PM forms render inside the
  FeatureManager tab, and an activating command auto-switches to it (`layout.cpp`).

## Graphify starters
- graphify query "liquid glass adaptive surface backdrop"
- graphify explain "ScenarioRunner"
- graphify path "app.cpp" "renderer.cpp"

## Related tasks/docs
- work/WORKFLOW.md — task policy; archived GUI cards are implementation history, not a claim queue.
- docs/design/GLASS_DESIGN_SPEC.md (current design language; the live widget kit is `host/src/ui/widgets/kit.{h,cpp}`)
- work/WORKFLOW.md §3c (sole-GUI ownership policy)
