# AI-CAD

A **SolidWorks-class parametric CAD application** — native C++ desktop app with a ribbon UI,
FeatureManager tree, PropertyManager, and a constraint-solver sketcher — built on the
**OpenCASCADE** B-rep kernel (via [build123d](https://build123d.readthedocs.io/)), with an
**AI orchestrator** and an **MCP server** so the same document can be driven by hand or by an
agent (the built-in chat, or any external agent such as Claude Code).

> **Status:** a working parametric modeler. Sketches + relations/dimensions solver, the full
> common feature set (extrude/revolve/sweep/loft/rib/shell/draft/fillet/chamfer/hole/pattern/
> mirror), assemblies + mates, sheet metal, surfacing, weldments, drawings, FEA (static/modal)
> + LBM flow + SPICE simulation, an **ECAD** module (schematic / PCB / native sim / Gerber), and
> multi-format import/export (STEP/IGES/BREP/STL/3MF/OBJ/PLY/glTF/3dm + SolidWorks COM +
> Parasolid) plus the native **`.step+`** document. See [ARCHITECTURE.md](ARCHITECTURE.md) and
> [docs/PLAN.md](docs/PLAN.md) for the full design.
>
> **In progress — the C++/CUDA core rewrite ([docs/CPP_MIGRATION.md](docs/CPP_MIGRATION.md)):**
> the Python engine is being replaced, subsystem by subsystem, with a pure-C++ core shipped as
> `libaicad.dll`, with the goal of collapsing to a single process with **zero Python**. It runs
> today as a *strangler*: Python is the oracle, the C++ core is gated behind a differential
> parity harness (currently **24/24** geometric parity, **7/7** stateful-session parity). The
> production path still routes document ops to the Python worker; **Settings → General → Engine**
> shows which engine is live.

## How it works

A native C++ binary is the product. The desktop app (GLFW + Dear ImGui + OpenGL viewport) talks
to a **Python kernel worker** over newline-delimited JSON-RPC. The worker holds the parametric
**document** — a feature tree compiled to OCCT solids, tessellated for the viewport, and exported
to STEP/STL/etc. The same engine is exposed over an **MCP server** for external agents, and a
built-in **agent loop** can design from a natural-language prompt. The legacy v1 pipeline (an AI
writes build123d code) still works — that script becomes a `script` feature in the document tree.

```
            ┌────────────────────────────────────────────────────────────┐
            │  AI-CAD.exe  (native C++ — ribbon UI, feature tree, sketcher)│
 user / AI →│   viewport (GLFW+ImGui+GL)   chat/agent   MCP server ◀───────┼── external agent
            │                    │ JSON-RPC over stdio                     │
            │                    ▼                                         │
            │   Python kernel worker  →  engine/doc feature compiler       │
            │   build123d → OpenCASCADE → tessellate + export              │
            └────────────────────────────────────────────────────────────┘
        in progress:  core/  (pure C++/CUDA)  →  libaicad.dll  replaces the worker
                      runtime + cache live in %APPDATA%\AI-CAD
```

## Layout
```
engine/          Python CAD engine (runs in the managed runtime)
  doc/             the parametric document: feature compiler, sketch constraint solver
                   (nsolve), session/ops/undo, selectors, tessellate, formats, drawing,
                   stepplus (.step+ I/O), 110+ test_*.py suites
  ecad/            KiCad-class EDA: schematic, PCB, netlist, native MNA circuit sim, fab out
  sim/             simulation: FEA (static/modal), LBM CFD, SPICE
  kernel.py        v1: run build123d code -> ModelResult (subprocess-isolated)
  worker.py        JSON-RPC worker the C++ host spawns; render.py headless PNGs
core/            C++/CUDA core rewrite (the Python-replacement, in progress)
  geom/ doc/ sketch/ solver/   geometry kernel, document compiler, SW sketch solver
  api/             libaicad.{h,cpp} flat C ABI (aicad_call) + DLL load probe
  tests/           doctest suites (ctest); built with Eigen + OCCT 7.9.3
host/            native C++ desktop app (GLFW + Dear ImGui + OpenGL)
  src/ui/          ribbon, FeatureManager tree, PropertyManager, sketcher, drawing, ECAD,
                   settings, viewport panel, status bar (~79 units)
  src/core/        app/doc state, DocMirror, worker client, cpp_core (loads libaicad)
  src/render/      offscreen GL renderer, id-pass picking, overlays
  CMakeLists.txt   MSVC build (fetches GLFW/ImGui/json)
mcp_server/      MCP server exposing the engine as tools (run_model, doc ops, screenshots)
agent/           built-in agent loop: prompt -> ops/code -> render -> visual self-check
installer/       runtime provisioning + Claude MCP self-registration + release packaging
docs/            PLAN.md, ARCHITECTURE, CPP_MIGRATION, ECAD_*, FORMATS_*, contracts/, sessions/
tools/           dev/diagnostic scripts (cpp_parity.py, cpp_session_parity.py, ...)
build.bat        one-shot host build + dist sync (see below)
```

## Build & run the desktop app
Needs Visual Studio 2022 (MSVC), CMake, and a provisioned runtime.
```powershell
# 1. provision the managed runtime (once) -> %APPDATA%\AI-CAD\runtime\venv
py -3.11 -m installer.provision --with-app-code

# 2. build the native host (Release). build.bat is the normal entry point:
build.bat            # compile host + sync into dist\AI-CAD\ (use after C++ or .py edits)
build.bat host       # compile the exe only (does not touch dist\)
build.bat app        # only re-sync Python app code into dist\ (fastest for .py-only changes)

# 3. run it
dist\AI-CAD\AI-CAD.exe            # full build (dist synced)
host\build\bin\Release\AI-CAD.exe # a `build.bat host` build runs from here (dist NOT synced)
```
Headless render / design (verifies the whole pipeline without the GUI):
```powershell
host\build\bin\Release\AI-CAD.exe --build engine\samples\mounting_plate.py out.png
host\build\bin\Release\AI-CAD.exe --design "a 30mm knurled hex standoff" out.png
```
On first launch `AI-CAD.exe` self-provisions `%APPDATA%\AI-CAD` from the payload beside it if the
runtime is absent (offline, from the wheelhouse). Force/inspect it with `AI-CAD.exe --ensure-runtime`.

Package a distributable (native exe + app code + offline wheelhouse + installer):
```powershell
py -3.11 -m installer.build_release --zip
# then on a target machine: powershell -ExecutionPolicy Bypass -File install.ps1
```

## The C++/CUDA core (in progress)
The pure-C++ core lives in `core/` and builds `libaicad.dll`. It is developed against the Python
engine as a differential oracle — never weaken an assertion, match Python unless Python is
provably buggy.
```powershell
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure   # incl. the libaicad load probe

$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
& $py tools\cpp_parity.py            # stateless feature parity dashboard (exact BRep volume gate)
& $py tools\cpp_session_parity.py    # stateful session (doc_new/apply_ops/undo/redo/resolve_pick)
```

## Engine dev & tests
The managed runtime lives at `%APPDATA%\AI-CAD\runtime\venv`. Engine suites are plain
`main()`-runner test modules (house style):
```powershell
$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
& $py -m engine.run_model mounting_plate         # v1: build + render a sample -> %APPDATA%\AI-CAD\cache
& $py -m engine.doc.test_sketch_solver           # run one engine test suite
& $py -m mcp_server.test_roundtrip               # MCP roundtrip self-test
& $py -m mcp_server.server                        # run the MCP server (stdio) for an external agent
```
Drive the built-in agent loop from a prompt (needs an API key, or use the mock provider):
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or copy config.example.json to %APPDATA%\AI-CAD\config.json
& $py -m agent.run_agent "a 60x40x12mm bracket with two M5 clearance holes and a filleted back edge"
& $py -m agent.test_loop_mock           # verify the loop logic with no API key
```

## Model-author contract (v1 `script` features)
Scripts run with the full `build123d` namespace in scope and assign the final shape to
**`result`** (a `Part`/`Solid`/`Compound`, or a `BuildPart`/`BuildSketch` context — the kernel
unwraps it). Anything printed is captured and returned. See
[docs/contracts/](docs/contracts) for the frozen interface contracts (doc schema, ops, RPC,
selectors, mesh/`.step+` formats).
