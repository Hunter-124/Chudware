# AI-CAD

A **SolidWorks-class parametric CAD application** — native C++ desktop app with a ribbon UI,
FeatureManager tree, PropertyManager, and a constraint-solver sketcher — built on the
**OpenCASCADE** B-rep kernel (via [build123d](https://build123d.readthedocs.io/)), with an
**AI orchestrator** and an **MCP server** so the same document can be driven by hand or by an
agent (the built-in chat, or any external agent such as Claude Code).

> **Status:** a working parametric modeler with sketches + constraint solver, the full common
> feature set (extrude/revolve/sweep/loft/rib/shell/draft/fillet/chamfer/hole/pattern/mirror),
> assemblies + mates, sheet metal, surfacing, weldments, drawings, FEA (static/modal), LBM CFD,
> SPICE simulation, an **ECAD** module (schematic/PCB/native sim/Gerber), a full **CAM** suite
> (milling/turning/laser-plasma-waterjet, G-code for GRBL/LinuxCNC/Fanuc/Haas/Marlin/Siemens),
> a standard-parts library, and multi-format import/export (STEP/IGES/BREP/STL/3MF/OBJ/PLY/
> glTF/3dm + SolidWorks COM + Parasolid) plus the native **`.step+`** document.
>
> **AI-experience layer (GSL):** a deterministic **Geometry-Semantics Layer** sits between the
> kernel and every agent-facing surface (`describe_part`/`describe_sketch`/`describe_scene`,
> `render_part`/`contact_sheet` image+caption pairs, `cad_guide`) so an agent without vision can
> still read named, stable geometry — see
> [docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md](docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md).
> Self-check RPCs (`check_feature_overlap`, `check_interference`, `eval_compare`,
> `doc_face_wires`) let the copilot verify its own work. A gated GNN face-recognition scaffold
> lives at `tools/gsl_gnn/` — deferred until deterministic recognition is measured failing.
>
> **C++/CUDA core rewrite (in progress):** the Python engine is being replaced subsystem by
> subsystem with a pure-C++ core (`libaicad.dll`). It runs today as a strangler: **24/24**
> geometric-feature parity, **42/42** stateful doc-session parity, FEA static/modal (4/4), and a
> completed SPICE epic (35/35 device parity, 10/10 netlist parity). Still Python-only: CFD,
> ECAD, formats, assemblies, and the agent/MCP layer. **Settings → General → Engine** shows which
> engine is live.
>
> **GUI (in transition — workstream G in [work/](work)):** the shipping desktop GUI is
> `host/` with the adaptive **liquid-glass v2** overlay design (full-bleed viewport,
> transparent floating columns, per-element glass pills — locked decisions in
> [docs/HANDOFF_GUI_OVERHAUL.md](docs/HANDOFF_GUI_OVERHAUL.md) §1 and
> [docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md](docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md)). It is being
> ported wave-by-wave into the modular `ui/imgui` + `ui/viewport` + `platform/` +
> `services/worker` split — node-level plan in
> [docs/GUI_MIGRATION_DAG.md](docs/GUI_MIGRATION_DAG.md) — after which `host/` is deleted
> (criteria in [host/DEPRECATED.md](host/DEPRECATED.md)). A tiered rename **AI-CAD →
> ShopCraft** is queued alongside (branding first; build targets/env vars/MCP id/appdata
> after the cutover, so tooling breaks once, not thrice).

## How it works

A native C++ binary is the product. The desktop app (GLFW + Dear ImGui + OpenGL viewport) talks
to a **Python kernel worker** over newline-delimited JSON-RPC. The worker holds the parametric
document — a feature tree compiled to OCCT solids, tessellated for the viewport, and exported to
STEP/STL/etc. The same engine is exposed over an **MCP server** for external agents, and a
built-in **agent loop** can design from a natural-language prompt.

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

## Layout (post-refactor — see [AGENTS.md](AGENTS.md))
```
contracts/       frozen interface seams (worker RPC, doc schema, selectors, mesh, …)
core/            pure C++/CUDA kernel → libaicad (zero-Python target)
engine_oracle/   Python parity oracle (frozen; deleted last) — root `engine` symlink
services/
  worker/        JSON-RPC client + libaicad loader + routing policy
  agent/         built-in agent loop — root `agent` symlink
  mcp/           MCP server — root `mcp_server` symlink
  installer/     provisioning + packaging — root `installer` symlink
ui/
  abstract/      framework-agnostic UI seam (view-model, commands, panel specs)
  imgui/         ImGui desktop GUI rewrite — root `app` symlink
  viewport/      shared OpenGL pipeline (scaffold)
host/            today's production GUI; EOL after the ui/imgui port (host/DEPRECATED.md)
corpora/         test fixtures (sketch, render, sw_parity, sw_fixtures)
docs/ tools/ scripts/ third_party/
work/            git-tracked task system (see work/README.md)
```

## Where this is going

AI-CAD is expanding into an **idea-to-production platform** — one prompt carries a project from
concept through mechanical design, electronics, firmware, structural checks, and priced BOM to a
ready-to-order package. The full decision record and rationale is in
[docs/PLATFORM_ADR.md](docs/PLATFORM_ADR.md); the 5-day walking-skeleton sprint is in
[docs/SPRINT_5DAY_PLAN.md](docs/SPRINT_5DAY_PLAN.md).

**DAG workflow orchestrator** — projects as checkpointed graphs (LangGraph + SQLite); limited-
context models execute nodes; explicit end conditions so loops terminate; human approval gates
per node; autonomy dial from "AI does everything" to "AI as advisor."

**AI tooling & infrastructure overhaul** — the layer under every pillar: agent web-research
tools (materials/datasheets/standards with provenance), a unified ask-user interaction
contract, image + camera intake, state→text narration for every domain (CAM, FEA/CFD, ECAD,
assemblies — GSL everywhere), a converged provider/session layer (MCP-first, subscription
session auth, BYO key, local models, company-hosted relay), and a trainable ≤4B local
translator track — see [docs/AI_TOOLING_MASTER_PLAN.md](docs/AI_TOOLING_MASTER_PLAN.md).
Cut as **workstream F** in [work/](work).

**Deliverables & transparent agents** — agents produce everything an engineer ships
(design-review decks, calc packages, BOM/cost workbooks, test reports, manuals) through
open-source format engines (python-pptx/docx/openpyxl, LibreOffice + pandoc as
subprocesses — we never write format parsers), with in-app previews and artifact cards;
and every agent runs as a glass box — live plan/thought/action stream, steer-anytime
(pause, answer, inject, replan), an autonomy dial from advisor to autonomous, and an
editable design brief so intent is agreed before the run — master plan Pillars 14–15.

**Sourcing + cost estimation** — priced BOMs with supplier lookups (Nexar/Digi-Key/Mouser),
machining cost from CAM cycle-time, fabrication quotes (Xometry/JLCPCB), and a material-cost
model with user-overridable rate tables.

**Spec-sheet → model matching** — pair purchased parts (no CAD file) to generated or library
models using datasheet extraction; standard fasteners/bearings from BOLTS blt tables; AI path
extracts a dimension dict from a pasted listing and fills a parametric template.

**Firmware/embedded environment** — compile firmware targeting the MCU seeded from the ECAD
schematic BOM (pin-map header generated from the netlist), emulate in Renode (MIT) so the agent
can test without hardware, then flash via probe-rs or esptool.

**Civil/structural/carpentry suite** — structural elements (beam/column/post/joist/stud-wall/
footing) compiled to B-rep, analyzed with PyNite (MIT), AISC + dimensional lumber section
libraries, D/C ratio checks (labeled preliminary), cost takeoff × ENR-seeded rate tables.

**Format interop / reverse-engineering track** — openNURBS/.3dm (MIT, no reversing needed),
Parasolid payload extraction from NX/Solid Edge, then clean-room parsers for CATIA V5, Inventor,
Altium, and DXF/DWG; goal is to break vendor lock-in under the DMCA interop exemption.

## Linux gate
```bash
bash scripts/gate.sh    # check.py + core build/ctest + ui/abstract test
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
On first launch `AI-CAD.exe` self-provisions `%APPDATA%\AI-CAD` from the payload (offline wheelhouse).
Force/inspect: `AI-CAD.exe --ensure-runtime`. Package: `py -3.11 -m installer.build_release --zip`.

## The C++/CUDA core (in progress)
The pure-C++ core lives in `core/` and builds `libaicad.dll`. Developed against the Python
engine as a differential oracle — never weaken an assertion, match Python unless Python is
provably buggy.
```powershell
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure   # C++ core doctest suite (parity proof, incl. the libaicad load probe)
# Legacy Python-vs-C++ differential harnesses (cpp_parity.py, cpp_session_parity.py) are documented
# in docs/CPP_MIGRATION.md but not present in this tree — use ctest + core/tests/ until restored.
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

## Wire the MCP server into external agents
Register the AI-CAD MCP server with every supported client (Claude Code, Cursor, VS Code,
opencode, Antigravity) in one shot:
```powershell
mcp_setup.bat            # portable wrapper around installer/setup_mcp_clients.py
```

## Contributing & task tracking

See [CONTRIBUTING.md](CONTRIBUTING.md) for workflow, rules, and AI-tooling guidance.

Tasks are git-tracked YAML files in `work/` (see [work/README.md](work/README.md)). List
unblocked work with:
```bash
python3 work/tools/work.py ready
```
Each task lives on a branch named `task/<id>-<slug>`. The dependency graph is
[work/DAG.md](work/DAG.md) (regenerate with `python3 work/tools/work.py dag`). Autonomous
agents pick up tasks via [work/prompts/autopilot.md](work/prompts/autopilot.md); workstream
G (GUI polish → ShopCraft rename → `ui/imgui` migration → `host/` deletion) has extra
build/verify rules noted there.

## Model-author contract (v1 `script` features)
Scripts run with the full `build123d` namespace in scope and assign the final shape to
**`result`** (a `Part`/`Solid`/`Compound`, or a `BuildPart`/`BuildSketch` context — the kernel
unwraps it). Anything printed is captured and returned. See
[contracts/](contracts) for the frozen interface contracts (doc schema, ops, RPC,
selectors, mesh/`.step+` formats).
