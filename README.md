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

## Contributor setup: graphify + the team-coordination tools

Two pieces of **internal dev tooling** (not part of the shipped product) make working on
this codebase in parallel with a team, human or AI, much cheaper:

**graphify** — a persistent knowledge graph of this codebase (god nodes, community
structure, cross-file relationships) that Claude Code queries instead of repeated raw
grep/read loops. `.claude/settings.json` wires a repo-wide hook that *requires* Claude to
run `graphify query`/`explain`/`path` before grepping or reading source once the graph
exists, so build it once per clone:

```
graphify .                 # one-time: build graphify-out/graph.json for this checkout
graphify hook install      # optional: auto-rebuild graph.json after every git commit
```

`graphify-out/` is a local, per-machine build artifact (gitignored — it bakes in absolute
paths and is fully regenerable) — every contributor builds their own, it is never pushed.
The `graphify claude install` CLAUDE.md wiring is already done project-wide; you only need
the one-time build above. See [.claude/skills/graphify/SKILL.md](.claude/skills/graphify/SKILL.md)
for the full command set.

**Team coordination (`work/`)** — file-based task tracking plus a live sync layer for
multiple contributors (human or AI) working the [work/](work) task board at once, in
parallel Claude Code CLI / Claude Desktop sessions, without overlapping fixes:

```
python3 work/tools/setup_team_tools.py   # one-time: registers the aicad-team MCP server
                                          # with Claude Code CLI + Claude Desktop
work/workd.sh <your-name>                # (or work\workd.bat) — presence/sync daemon,
                                          # leave running while you work
work/report.sh                           # (or work\report.bat) — AI-guided issue reporter
                                          # GUI: draft a bug/idea -> linked GitHub issue +
                                          # work/backlog task in one step
```

Once registered, `/work-board`, `/work-pick`, `/work-sync`, `/work-issues`, `/work-msg`,
`/work-report`, and thirteen per-section `/ctx-*` commands (pre-mapped source maps —
`work/context/*.md` — so agents don't burn tokens re-exploring a subsystem) are available
in every Claude Code session in this repo. This is entirely per-machine setup (each
contributor runs it locally on Windows/macOS/Linux — nothing here touches shared repo
state) so any number of people can register and run it concurrently without conflict.
Two things to expect on first run, either platform:
- **Claude Code CLI** will show a one-time prompt to approve the new project MCP server
  the first time you run `claude` in this repo after registering — that's the normal
  per-contributor trust check, say yes.
- **Claude Desktop**: fully quit and reopen (not just close the window) to pick up the
  change. Newer Desktop versions may source their MCP/tool list from account-synced
  Connectors settings instead of the classic config file `setup_team_tools.py` writes —
  if `aicad-team` doesn't show up after restarting, add it manually via
  Settings → Connectors (or MCP Servers): Command `python3` (or `py -3` on Windows),
  Args `work/tools/team_mcp.py`'s full path.

**The desired path to contributing** — once registered, an agent (or human driving one)
has everything needed in the `aicad-team` MCP tools; no raw `git`/`gh` juggling required:

```
work_board                              # situational awareness: full task board + who's
                                         # online + open issues — call this first, always
work_claim(owner="you")                 # atomically claim the next ready task (or a
                                         # specific task_id) — the ONLY legitimate way in
work_prompt(task_id="T-XX")             # emit the self-contained work order for it
  ...do the work, on the branch the work order names...
work_note(task_id, owner, text)         # log progress so parallel sessions/humans stay
                                         # current — use at real milestones
work_status(task_id, owner,             # doing -> review when pushed and awaiting human
            status="review")           # merge (status can never be set to "done" here —
                                         # that's a human merge-time action)

issue_list()                            # before fixing any bug: check nobody else has it
issue_claim(number)                     # claim it on GitHub so fixes never overlap
graphify_query / graphify_explain /     # scoped subgraph answers instead of repeated
  graphify_path                         # grep/read loops — use before raw file search
section_context(section="core-cam")     # pre-mapped source map for a subsystem — see
                                         # available sections with no args
team_status / team_send                 # who else is active right now, and message them
```

Same rules either way (MCP tools or the `/work-*` slash commands are two doors to the
same system): claim before starting, note progress, never flip anything to `done`
yourself, and check `issue_list`/`work_board` before touching something someone else
might already be on. Full protocol: [work/README.md](work/README.md)
and [work/prompts/autopilot.md](work/prompts/autopilot.md).

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
