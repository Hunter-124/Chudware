# host/src — GLFW+ImGui+OpenGL production desktop GUI (deprecated, porting to ui/imgui/)

Scope: host/src/**, host/CMakeLists.txt, host/build-linux
Contracts: ui/abstract/command_registry (ICommandDispatcher::dispatch routed from app.cpp), contracts/worker_rpc.md (worker JSON-RPC for doc_apply_ops)

## Entry points
- host/src/main.cpp:28 `main()` — GLFW window bootstrap, ImGui docking context init, worker stdio bridge spawning
- host/src/app.cpp:1 `AppState::runFrame()` — monolithic GUI frame loop: input dispatch → commands → UI panels → renderer calls
- host/src/renderer.cpp:450 `Renderer::render()` — OpenGL 3.3 core ImGui draw layer (VAO, shader, framebuffer blits for glass effects)
- host/src/ui_theme.cpp:1 `glassPanel()` / `glassRowBackground()` / `adaptiveSurface()` — liquid-glass v2 API: blur shaders, elevation levels, text-contrast lookups
- host/src/ui/scenarios.cpp:1 `loadScenario()` — screenshot harness: m1, sketch, appearance, fea, drawing, asm scenarios
- host/src/ui/layout.cpp — pinned FeatureManager (left) + Copilot (right) glass columns, doc tabs, ribbon
- host/src/ui/legacy_chat.cpp — Copilot panel (collapse-to-pill, glass rows, command form integration)
- host/CMakeLists.txt — deps: GLFW 3.4, Dear ImGui v1.91.6-docking, OpenGL/glad, stb, nlohmann/json

## How it connects
Liquid-glass v2 overlay design (docs/GLASS_DESIGN_SPEC.md, HANDOFF_GUI_OVERHAUL.md): pinned glass columns float over full-width viewport; adaptive text contrast + per-group tint settings (AppearanceSettings). Porting to ui/imgui/ in progress; host/ will be deleted once ui/imgui passes deletion criteria (host/DEPRECATED.md: standalone build, feature-surface parity, headless screenshot smoke, feature_surface_audit.py retarget). graphify does NOT index host/src (only ui/abstract + core) — direct grep/read expected and allowed here.

## Debugging playbook
- Build (serialized): `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"` (never run two builds against one dir concurrently)
- Screenshot harness: `AI_CAD_SHOT=m1|sketch|appearance|fea|drawing|asm|multidoc AI_CAD_REPO="$PWD" host/build-linux/bin/AI-CAD --ui-screenshot out.png` (per-step PNGs in scratch/<scn>_shots/)
- Gotcha: FeatureManager input regression (commit 043e752 added NoBringToFrontOnFocus — fixed by removal); watch FM collapse pill edge-publish
- Gotcha: viewport HUD clip + sketch overlay dodge columns via `overlayColumnEdge(0/1)` (safe-area inset to inter-column bounds)
- Gotcha: PropertyManager tab removed — PM commands now nested inside FeatureManager tab (header text switches to "Property Manager" when active)

## Graphify starters
- graphify query "liquid glass adaptive surface backdrop" (crosses into ui/abstract appearance settings)
- graphify explain "glass theme API" (GLASS_DESIGN_SPEC material levels + elevation)
Note: graphify-out does NOT index host/src directly — use targeted grep/read for renderer.cpp details, panel editors, input dispatch.

## Related tasks/docs
- work/tasks: grep T-G* (GUI workstream 1–10 tasks, landing liquid-glass, renaming to ShopCraft, migration DAG)
- docs/HANDOFF_GUI_OVERHAUL.md (glass core API + styling wave audit), docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md (FM input fix, transparent columns, Copilot header, sketch HUD dodge)
- docs/GUI_MIGRATION_DAG.md (node specs, write-sets, verify commands for porting to ui/imgui/)
- host/DEPRECATED.md (deletion gate criteria: ui/imgui standalone build, feature-surface parity, headless screenshot smoke)
- docs/GLASS_DESIGN_SPEC.md (design principles, 4 material levels, spacing grid, per-panel rules)
