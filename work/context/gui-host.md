# host/src — sole GLFW+ImGui+OpenGL desktop and web GUI

Scope: host/src/**, host/CMakeLists.txt, host/build-linux
Contracts: contracts/worker_rpc.md (worker JSON-RPC), contracts/doc_ops.md (modeling commands)

## Entry points
- host/src/main.cpp:28 `main()` — GLFW window bootstrap, ImGui docking context init, worker stdio bridge spawning
- host/src/app.cpp:1 `AppState::runFrame()` — monolithic GUI frame loop: input dispatch → commands → UI panels → renderer calls
- host/src/renderer.cpp:450 `Renderer::render()` — OpenGL 3.3 core ImGui draw layer (VAO, shader, framebuffer blits for glass effects)
- host/src/ui_theme.cpp:1 `glassPanel()` / `glassRowBackground()` / `adaptiveSurface()` — liquid-glass v2 API: blur shaders, elevation levels, text-contrast lookups
- host/src/ui/scenarios.cpp:1 `loadScenario()` — screenshot harness: m1, sketch, appearance, fea, drawing, asm scenarios
- host/src/ui/layout.cpp — pinned FeatureManager (left) + Copilot (right) glass columns, doc tabs, ribbon
- host/CMakeLists.txt — deps: GLFW 3.4, Dear ImGui v1.91.6-docking, OpenGL/glad, stb, nlohmann/json

## How it connects
`host/` is the sole active GUI target. Pinned glass columns float over the full-width viewport, with adaptive text contrast and per-group tint settings. The same host UI tree compiles to WASM for the web app; the retired `ui/imgui` and `ui/abstract` shell has been deleted.

## Debugging playbook
- Build (serialized): `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"` (never run two builds against one dir concurrently)
- Screenshot harness: `CHUDWARE_SHOT=m1|sketch|appearance|fea|drawing|asm|multidoc CHUDWARE_REPO="$PWD" host/build-linux/bin/Chudware --ui-screenshot out.png` (per-step PNGs in scratch/<scn>_shots/). Binary renamed from Chudware; an `Chudware` compat symlink remains in bin/.
- Gotcha: FeatureManager input regression (commit 043e752 added NoBringToFrontOnFocus — fixed by removal); watch FM collapse pill edge-publish
- Gotcha: viewport HUD clip + sketch overlay dodge columns via `overlayColumnEdge(0/1)` (safe-area inset to inter-column bounds)
- Gotcha: PropertyManager tab removed — PM commands now nested inside FeatureManager tab (header text switches to "Property Manager" when active)

## Graphify starters
- graphify query "liquid glass adaptive surface backdrop"
- graphify explain "glass theme API"
Note: graphify-out does NOT index host/src directly — use targeted grep/read for renderer.cpp details, panel editors, input dispatch.

## Related tasks/docs
- work/tasks: grep T-G* (GUI workstream 1–10 tasks, landing liquid-glass, renaming to Chudware, migration DAG)
- docs/design/GLASS_DESIGN_SPEC.md (current design language; the former UI_PATTERN_LIBRARY.md was deleted with the retired ui/imgui shell — the live widget kit is host/src/ui/widgets/kit.{h,cpp})
- work/WORKFLOW.md §3c (sole-GUI ownership policy)
