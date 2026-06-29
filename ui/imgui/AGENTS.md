# ui/imgui — Dear ImGui concrete GUI backend

**Owner:** P3 nodes (N3a–N3d). First concrete backend over `ui/abstract/`.

Clean-room ImGui shell from `app/` (`app/DESIGN.md`). Renders the abstract
view-model; does NOT talk to the engine directly — goes through
`services/worker/` via `ICommandDispatcher`.

## Write-set rules
- May include ImGui, GLFW, OpenGL (via `ui/viewport/`).
- Must NOT duplicate command definitions — reads `ui/abstract/command_registry.*`.
- `host/` panels rehosted here workspace-by-workspace in P3 N3b.

## Migration source (`app/` → here, P3)
- Entire `app/src/*` tree re-homed under `ui/imgui/src/`.
