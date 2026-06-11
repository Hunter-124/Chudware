# AI-CAD — Architecture

> **2026-06 overhaul:** AI-CAD is now a SolidWorks-class parametric CAD application — ribbon UI,
> FeatureManager tree, PropertyManager, constraint-solver sketcher, assemblies with mates,
> FEA (static/modal) + LBM flow simulation, Evaluate tools, STEP/IGES/BREP/STL/3MF + native
> `.step+`, and a deterministic AI orchestrator with editable role prompts and a transparent
> Activity panel. **Canonical references:** [docs/PLAN.md](docs/PLAN.md) (design + milestones),
> [docs/contracts/](docs/contracts) (frozen interface contracts),
> [docs/sessions/reports.md](docs/sessions/reports.md) (per-component integration notes).
> The description below covers the original v1 pipeline, which still works unchanged
> (a script becomes a `script` feature in the document tree).

AI-driven **parametric CAD**. An AI model (built-in agent, or any external agent over
MCP) writes [build123d](https://build123d.readthedocs.io/) Python code; the engine
executes it on the OpenCASCADE B-rep kernel to produce a **precise, feature-rich solid**;
the solid is rendered to screenshots that are fed back to the model so it can verify the
result against the request and iterate.

```
                ┌──────────────────────────────────────────────────────────┐
                │  AI-CAD.exe   (native C++ single binary — the product)     │
                │                                                            │
  user prompt → │  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐  │
                │  │ Chat / Agent │──▶│ 3D Viewport  │   │  MCP server    │◀─┼── external agent
                │  │ panel (loop) │   │ (GLFW+ImGui+ │   │ (HTTP/SSE)     │  │   (Claude Code, …)
                │  └──────┬───────┘   │  OpenGL)     │   └───────┬────────┘  │
                │         │           └──────▲───────┘           │           │
                │         │   JSON-RPC over stdio / local socket │           │
                │         ▼                  │                   ▼           │
                │  ┌───────────────────────────────────────────────────┐    │
                │  │   Python kernel worker  (runs in managed runtime)  │    │
                │  │   build123d → OpenCASCADE → tessellate + export     │    │
                │  └───────────────────────────────────────────────────┘    │
                └──────────────────────────────────────────────────────────┘
                          runtime + cache live in %APPDATA%\AI-CAD
```

## Locked decisions
| Area | Choice |
|---|---|
| CAD kernel | **build123d** (Python) on the **OpenCASCADE** B-rep kernel — precise solids, fillets/chamfers/threads/assemblies, clean STEP export |
| App shell | Native **C++** single launcher binary (GLFW + Dear ImGui + OpenGL viewport) |
| Runtime delivery | **Embed & extract**: the heavy runtime (isolated Python + build123d + OCCT) ships as a payload (`app/` + offline `wheelhouse/`) beside the launcher. On first launch `AI-CAD.exe` runs `ensureRuntime()` → provisions `%APPDATA%\AI-CAD` from the payload if absent. `install.ps1` does the same at install time. |
| Interfaces | **Built-in agent loop** *and* an **MCP server** so any external agent/provider can drive the same engine |
| Self-registration | On first launch the app registers itself as an MCP server in the machine's Claude install (Claude Code + Claude Desktop) |
| v1 milestone | Full agent loop end-to-end on one real example |

## Components

### 1. Python CAD kernel (`engine/`) — built first, testable now
The brain of the modeling. Pure Python, runs inside the managed runtime.
- `kernel.py` — execute AI-generated build123d code in a controlled namespace; locate the
  resulting solid; compute mass properties (volume, area, bbox, center); export **STEP**
  (precise) + **STL/GLB** (mesh); tessellate for the viewer. Each job runs in a fresh
  subprocess so an OCCT crash can't take down the app.
- `render.py` — headless multi-view PNG screenshots (PyVista/VTK off-screen). Used by
  MCP-headless mode and tests. In the desktop app the live OpenGL viewport is the primary
  screenshot source (what the user sees == what the model checks).
- `protocol.py` — the result schema shared across kernel, worker, MCP, and host.
- `worker.py` — newline-delimited JSON-RPC loop over stdio; this is what the C++ host
  spawns and talks to.
- `samples/` — feature-rich reference models used as smoke tests.

### 2. MCP server (`mcp/`)
HTTP/SSE MCP server hosted by the running app so an external agent drives the **live**
model (changes appear in the viewport). Tools:
`run_model(code)`, `get_screenshot(views)`, `get_model_info()`, `export(format)`,
`reset()` for single parts; `run_assembly(code)`, `get_fit_report()`,
`get_assembly_screenshot()`, `export_part(name, fmt)` for multi-part assemblies. Registered
into Claude config on first launch.

### 3. Built-in agent loop (`agent/`)
`generate → run_model → screenshot → vision self-check vs. request → iterate`.
Provider-adapter layer (`providers/`: anthropic, openai, openai-compatible/local) so
"any API provider" works; default Anthropic (vision + codegen). Keys stored in
`%APPDATA%\AI-CAD\config.json`.

### 4. Native host (`host/`) — C++
GLFW window, Dear ImGui docking UI (chat, viewport, code, log, settings), OpenGL mesh
renderer with orbit camera, framebuffer capture → PNG. Spawns + supervises the Python
worker. Hosts the MCP server. Embeds the runtime payload and runs first-launch bootstrap.

### 5. Installer / bootstrap (`installer/`, `scripts/`)
Embed-and-extract of the runtime payload; version-stamped provisioning into
`%APPDATA%\AI-CAD`; Claude MCP self-registration; Windows installer script.

## Model-author contract
AI-generated scripts run with the full `build123d` namespace in scope and must assign the
final shape to **`result`** (a `Part`/`Solid`/`Compound`, or a `BuildPart`/`BuildSketch`
context — the kernel unwraps `.part`/`.sketch`/`.line`). Everything printed is captured and
returned to the model.

**Multi-part / assembly** (optional, in addition to `result`): a script may instead define
**`parts = {"base": solid_a, "lid": solid_b, ...}`** — named bodies positioned in a shared
coordinate space with `Pos(x,y,z) *` / `Rot(rx,ry,rz) *` — and/or **`assembly = Compound(...)`**.
The kernel (`run_code_multi`) then exports each part (`{name}_{part}.stl/.step`) plus a combined,
non-fused assembly (`{name}_assembly.stl/.step`) and computes a **fit report**: pairwise
interference (overlap volume via `(a & b).volume`) and clearances (min distance via OCCT
`BRepExtrema_DistShapeShape`), with per-part bounding boxes. A color-coded contact sheet renders
the bodies together so the model can verify the fit. Single-`result` scripts behave exactly as
before (`run_code` is unchanged; `run_code_multi` falls back to the single-part path).

## Build order (all five pillars built & verified)
1. **Kernel + headless render** — model → STEP/STL + multi-view PNGs. ✅
2. **MCP server** over the kernel; drivable by any external agent. ✅
3. **Built-in agent loop** with provider adapters (Anthropic/OpenAI/local/mock). ✅
4. **C++ host**: GLFW + ImGui + OpenGL viewport, JSON-RPC worker IPC, offscreen
   screenshots (`--render`/`--contact`/`--build`/`--design`). ✅
5. **Installer**: `provision.py` (idempotent runtime), `register_mcp.py` (Claude
   self-registration), `build_release.py` (exe + app + offline wheelhouse + scripts). ✅

Remaining productization polish: embed the wheelhouse/portable-python payload *inside* the
launcher exe (currently shipped alongside it and extracted by install.ps1); CAD feature-edge
overlay in the GL viewport; streaming agent progress into the GUI.

Engine and MCP are developed against the real product runtime at
`%APPDATA%\AI-CAD\runtime` so dev and prod converge; packaging later pre-bundles the same
wheels instead of pip-downloading them.
