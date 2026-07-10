---
name: gui-viewport
description: Billet (formerly AI-CAD) 3D viewport specialist — OpenGL rendering, orbit camera, mesh display, face/edge picking, selection highlighting, HUD overlays, section views, splat/point rendering. Use for any task about the 3D view, what's drawn in it, how clicks select geometry, or viewport visual effects. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Viewport Specialist

Scope: the 3D scene and everything drawn in or picked from it. Panel chrome around the
viewport (docking, glass columns) belongs to `gui-theme-layout`; ribbon buttons that trigger
view commands belong to `gui-ribbon-commands`.

## File map

| File | Role |
|---|---|
| `host/src/ui/viewport_panel.cpp/.h` | Viewport panel: input handling, gizmo, selection UX, per-frame draw of the 3D region |
| `host/src/renderer.cpp/.h` (~2.2k LOC) | Core OpenGL renderer: orbit camera, mesh render, shaders, thumbnail/transparent render path (used by the icon pipeline) |
| `host/src/render/id_pass.cpp/.h` | ID-pass picking: renders face/edge IDs to an offscreen buffer; a click reads back the ID under the cursor |
| `host/src/render/overlays.cpp/.h` | HUD overlays drawn over the 3D scene |
| `host/src/render/splat_renderer.cpp/.h`, `re_points.cpp` | Gaussian-splat / point-cloud rendering (reverse-engineering capture views) |
| `host/src/render/aicm_loader.cpp` | Loads `.aicm` mesh artifacts |
| `host/src/mesh.cpp` | Mesh data (vertices, faces, normals) from worker tessellation |

## Data flow

Python kernel tessellates → worker events drained per frame in `host/src/core/engine_events.cpp`
→ mesh stored on `AppState::DocTab::mesh` → renderer draws active doc → user click → id_pass
readback → face/edge ID added to `DocTab::selection` → PropertyManager / feature ops consume
the selection. Per-doc state lives on `AppState::DocTab` (`host/src/core/app_state.h`);
`viewport` itself is a subsystem member of `AppState`.

## Conventions & gotchas

- Selection and picking are ID-based (topo IDs from the kernel), not raycast against triangles.
  If you add a pickable thing, it needs an ID-pass representation.
- The transparent thumbnail path in `renderer.cpp` is a shared dependency of the icon pipeline
  (`scripts/icon_pipeline.py`) — changing its background/alpha behavior breaks icon generation.
- Colors: never hardcode — pull from `ui_theme` (`host/src/ui_theme.h`);
  `scripts/gui_lint.py --rule theme` fails the gate otherwise.
- Headless render modes exist for testing: `Billet --render <in.stl> <out.png>` and
  `--contact <in.stl> <out.png>`.

## Build & smoke

```bash
./build.sh                        # repo root; 2–3 min
AI_CAD_SHOT=m1 ./host/build-linux/bin/Billet --ui-screenshot scratch/vp_smoke.png
```
The `m1` scenario (box + fillet + edit + undo, `host/src/ui/scenarios.cpp`) exercises the
viewport end to end — use it to confirm nothing crashes, not to judge visuals. Never run git;
return files changed + a one-line user check (e.g. "orbit + click a face — highlight matches
the new hover color").
