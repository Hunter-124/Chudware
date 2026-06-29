# ui/viewport — shared OpenGL viewport pipeline

**Owner:** P2 node N2c, then P3. Framework-shared (not ImGui-specific).

Reused OpenGL renderer from `host/src/render/*` and viewport GL parts from
`host/src/ui/viewport_panel.cpp` / `host/src/ui/camera_anim.*`. Implements
`ui/abstract/viewport.h` (`IViewport`).

## Write-set rules
- Owns: `renderer.*`, `mesh.*`, `render/*`, `camera_anim.*`, FBO compositing.
- Must NOT depend on ImGui (only on abstract viewport interface + GL).
- Preserves id-pass picking, AICM mesh format, overlay VBO batching.

## Migration source (host/ → here, N2c)
- `host/src/renderer.h`, `host/src/render/*`
- `host/src/ui/camera_anim.*`
