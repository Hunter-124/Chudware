# AI-CAD

AI-driven **parametric CAD**. A model writes [build123d](https://build123d.readthedocs.io/)
Python code → the engine builds a precise **OpenCASCADE** B-rep solid → it's exported to
STEP/STL and rendered to clean multi-view screenshots that the model inspects to verify the
result against the request and iterate.

> Status: **all five pillars built and verified** — CAD engine, headless renderer, MCP
> server, built-in agent loop, native C++ desktop host (GLFW + Dear ImGui + OpenGL
> viewport, Python worker IPC), and the installer (runtime provisioning + Claude MCP
> self-registration + offline payload). See [ARCHITECTURE.md](ARCHITECTURE.md) for the
> full design.

## Layout
```
engine/          Python CAD kernel: execute build123d, export STEP/STL, mass props
  kernel.py        run code -> ModelResult (subprocess-isolated variant available)
  render.py        headless multi-view PNGs (PyVista/VTK, clean feature edges)
  run_model.py     CLI: build a sample/.py and render it
  samples/         feature-rich reference models (mounting_plate, ...)
mcp_server/      MCP server exposing the engine as tools (run_model, get_screenshot, ...)
agent/           built-in agent loop: prompt -> code -> render -> visual self-check
  loop.py          the orchestration + prompts
  providers/       provider adapters (anthropic [default], openai, mock)
  config.py        resolve provider/key from config.json or env
  run_agent.py     CLI: design from a natural-language request
host/            native C++ app: GLFW + Dear ImGui + OpenGL viewport, worker IPC
  src/renderer.*   offscreen GL mesh renderer (smooth shading, supersampled screenshots)
  src/mesh.*       binary STL loader with normal welding
  src/worker_client.* spawn engine.worker, JSON-RPC over pipes
  src/gui_app.cpp  the desktop GUI (viewport + code/prompt panels)
  CMakeLists.txt   MSVC build (fetches GLFW/ImGui/json; glad vendored in extern/)
installer/       provisioning + Claude MCP self-registration + release packaging
  provision.py     create/verify %APPDATA%\AI-CAD runtime (idempotent, stamped)
  register_mcp.py  add/remove the ai-cad MCP server in Claude Desktop + Claude Code
  build_release.py assemble dist/ payload (exe + app + offline wheelhouse + scripts)
  install.ps1 / uninstall.ps1
requirements.txt managed-runtime pins (provisioned into %APPDATA%\AI-CAD\runtime)
```

## Run it (dev)
The managed runtime lives at `%APPDATA%\AI-CAD\runtime\venv`. Build + render a sample:
```powershell
$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
& $py -m engine.run_model mounting_plate
# -> STEP/STL + PNGs in %APPDATA%\AI-CAD\cache\mounting_plate
```
MCP roundtrip self-test:
```powershell
& $py -m mcp_server.test_roundtrip
```
Run the MCP server (stdio) for an external agent:
```powershell
& $py -m mcp_server.server
```
Drive the full built-in agent loop from a text prompt (needs an API key):
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or copy config.example.json to %APPDATA%\AI-CAD\config.json
& $py -m agent.run_agent "a 60x40x12mm bracket with two M5 clearance holes and a filleted back edge"
# generate -> render -> Claude inspects the screenshot -> iterate; outputs in %APPDATA%\AI-CAD\cache\agent_model
```
Verify the loop logic with no API key (mock provider):
```powershell
& $py -m agent.test_loop_mock
```

## Build the desktop app
Needs Visual Studio 2022 (MSVC), CMake, and a provisioned runtime.
```powershell
# 1. provision the runtime (once)
py -3.11 -m installer.provision --with-app-code
# 2. build the native host
cmake -S host -B host/build -G "Visual Studio 17 2022" -A x64
cmake --build host/build --config Release
# 3a. run the GUI (live viewport + code/prompt panels)
host\build\bin\Release\AI-CAD.exe
# 3b. or render headlessly (verifies the whole pipeline)
host\build\bin\Release\AI-CAD.exe --build engine\samples\mounting_plate.py out.png
host\build\bin\Release\AI-CAD.exe --design "a 30mm knurled hex standoff" out.png
```
Package a distributable (native exe + app code + 200 MB offline wheelhouse + installer):
```powershell
py -3.11 -m installer.build_release --zip
# then on a target machine: powershell -ExecutionPolicy Bypass -File install.ps1
```
On first launch `AI-CAD.exe` self-provisions `%APPDATA%\AI-CAD` from the payload beside it if
the runtime is absent (offline, from the wheelhouse). Force/inspect it with
`AI-CAD.exe --ensure-runtime`.

## Model-author contract
Scripts run with the full `build123d` namespace in scope and assign the final shape to
**`result`** (a `Part`/`Solid`/`Compound`, or a `BuildPart`/`BuildSketch` context — the
kernel unwraps it). Anything printed is captured and returned to the model.
