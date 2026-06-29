# ui/abstract — framework-agnostic UI seam

**Owner:** P2 refactor nodes (N2a–N2d). **No ImGui includes here.**

This module holds the presentation model, command registry, panel-as-data specs,
`ICommandDispatcher`, `IViewport` interface, and action bus. Every GUI backend
(`ui/imgui/`, future `ui/qt/`, etc.) renders from these types.

## Write-set rules
- Append-only: `command_registry.*` (one owner per round).
- Depends on: `services/worker/` (engine bridge), `contracts/worker_rpc.md`.
- Must NOT include GLFW, ImGui, or OpenGL headers.

## Key files (land in P2)
- `view_model.h` — DocViewModel, TreeModel, InspectorModel, SelectionState, WorkspaceState
- `command_registry.{h,cpp}` — moved from `app/src/ui/command_registry.*`
- `command_dispatcher.h` — ICommandDispatcher
- `panel_spec.h` — Panel/Field spec table for Inspector
- `viewport.h` — IViewport interface
- `action_bus.h` — input → Action → controller
