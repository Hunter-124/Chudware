# Chudware (formerly Billet / AI-CAD)

The product **displays as Chudware** everywhere users see it (window title, boot shell,
dialogs, wordmark, web landing). The GUI binary/target name stays `Billet` (formerly
`AI-CAD`) as an internal identifier — an `AI-CAD` symlink ships beside it for compatibility.

A **SolidWorks-class parametric CAD application** — native C++ desktop app (ribbon UI,
FeatureManager tree, constraint-solver sketcher) on the **OpenCASCADE** B-rep kernel, with an
**AI orchestrator** and an **MCP server** so the same document can be driven by hand or by any
agent (the built-in copilot, Claude Code, Cursor, …). Mechanical CAD, assemblies, drawings,
sheet metal, CAM, ECAD, FEA/CFD, sourcing/BOM — one document model under all of it.

---

## 🤖 If you are an AI agent, start here

Four rules put you on the right path instantly — everything else hangs off them:

1. **Orient with graphify, not grep.** This repo has a knowledge graph. Run
   `graphify query "<your question>"` (or `graphify explain` / `graphify path`) before reading
   source — a repo hook **enforces** this. Read raw files only after the graph orients you.
2. **Solo dev — use the task board as a personal backlog.** `python3 work/tools/work.py ready`
   shows what's unblocked; `work.py board` shows everything. No claim/coordination step is
   needed (one contributor), so just pick a task, do it, and mark it done yourself.
3. **The workflow policy is [work/WORKFLOW.md](work/WORKFLOW.md).** Priorities, lanes
   (feature areas), `kind: fix|overhaul|new` (fixes claim first), `deferred: yes` plans you must
   not touch, and the MCP-freshness rule (ship a capability and its discoverability in the same
   branch).
4. **Verify before review.** `bash scripts/gate.sh` (build + tests) must show no new reds;
   run `graphify update .` after code changes. GUI work verifies via the headless screenshot
   harness (`AI_CAD_SHOT=<scenario>`).

New-to-a-subsystem? `section_context(section="core-cam")` (or the `/ctx-*` skills) loads a
maintained source map instead of you re-exploring it.

## 🧑‍🔧 If you are a human, start here

One-time, per-machine setup:

```bash
# 1. Build the code knowledge graph for your checkout (gitignored, regenerable)
graphify .                               # once; `graphify hook install` keeps it fresh per-commit

# 2. Register the Billet CAD MCP server with your agent clients
python3 services/installer/setup_mcp_clients.py           # (Windows: mcp_setup.bat)
python3 services/installer/setup_mcp_clients.py --detect  # verify what's wired where
```

Optional extras: `work/report.sh` (AI-guided bug/idea reporter → GitHub issue + backlog task).

---

## What exists today

A working parametric modeler: sketches + constraint solver, the full common feature set
(extrude/revolve/sweep/loft/rib/shell/draft/fillet/chamfer/hole/pattern/mirror), assemblies +
mates, sheet metal, surfacing, weldments, drawings, FEA (static/modal), LBM CFD, SPICE, an
**ECAD** module (schematic/PCB/sim/Gerber), a full **CAM** suite (milling/turning/laser-plasma-
waterjet; G-code for GRBL/LinuxCNC/Fanuc/Haas/Marlin/Siemens), a federated standard-parts
library, and multi-format interop (STEP/IGES/BREP/STL/3MF/OBJ/PLY/glTF/3dm + SolidWorks COM +
Parasolid) around the native **`.step+`** document.

Three things distinguish the architecture:

- **GSL — the Geometry-Semantics Layer.** A deterministic state→text layer between the kernel
  and every agent surface (`describe_part/sketch/scene`, `render_part`, `cad_guide`), so an
  agent without vision still reads named, stable geometry; self-check RPCs
  (`check_feature_overlap`, `check_interference`, `eval_compare`) let the copilot verify its own
  work. See [docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md](docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md).
- **C++/CUDA core rewrite (strangler, in progress).** `core/` builds `libaicad`, replacing the
  Python engine subsystem-by-subsystem: 24/24 feature parity, 42/42 doc-session parity, FEA
  4/4, SPICE 35/35. Still Python: CFD, ECAD, formats, assemblies, agent/MCP layer.
  Settings → General → Engine shows which is live.
- **Single GUI, two targets.** The production GUI is `host/src/ui/` (liquid-glass overlay
  design — full-bleed viewport, transparent floating columns), built native for the desktop
  and compiled to WASM for the web app (`Webservice/`) — one tree, desktop/web parity by
  construction. (The earlier `ui/imgui` migration, workstream G, was reversed and its shell
  deleted.) Rename compatibility (product displays as **Chudware**, binary stays `Billet`):
  `BILLET_*` env vars canonical (`AI_CAD_*`/`AICAD_*` still honored), binary `Billet` + `AI-CAD`
  symlink, MCP id `billet` (existing `ai-cad` preserved), existing `AI-CAD` appdata dirs
  keep working.

## How it works

```
            ┌────────────────────────────────────────────────────────────┐
            │  Billet (native C++ — ribbon UI, feature tree, sketcher)    │
 user / AI →│   viewport (GLFW+ImGui+GL)   copilot   MCP server ◀─────────┼── external agent
            │                    │ JSON-RPC over stdio                    │
            │                    ▼                                        │
            │   Python kernel worker  →  engine/doc feature compiler      │
            │   build123d → OpenCASCADE → tessellate + export             │
            └────────────────────────────────────────────────────────────┘
        in progress:  core/ (pure C++/CUDA) → libaicad replaces the worker
                      runtime + cache live in %APPDATA%\AI-CAD
```

## Layout

```
contracts/       frozen interface seams (worker RPC, doc schema, doc ops, parts, plm, …)
core/            pure C++/CUDA kernel → libaicad (zero-Python target)
engine_oracle/   Python parity oracle (frozen; deleted last) — root `engine` symlink
services/
  worker/        JSON-RPC client + libaicad loader + routing policy
  agent/         built-in agent loop — root `agent` symlink
  mcp/           MCP server — root `mcp_server` symlink
  installer/     provisioning + packaging — root `installer` symlink
ui/
  abstract/      framework-agnostic UI seam (view-model, commands, panel specs)
  imgui/         the desktop GUI — root `app` symlink
  viewport/      shared OpenGL pipeline
host/            legacy GUI (deprecated — deleted at T-G11; don't add code here)
corpora/         test fixtures (sketch, render, sw_parity, sw_fixtures)
docs/ tools/ scripts/ third_party/
work/            task board (WORKFLOW.md, tasks/, tools/)
```

## How work gets organized

Tasks are git-tracked markdown files in `work/tasks/` + `work/backlog/` with frontmatter:
`priority` (1–5), `lane` (user-facing feature area), `kind` (`fix` | `overhaul` | `new`;
fixes go first within a priority), `deferred: yes` (parked plans — currently ML training and
SaaS hosting). Key commands:

```bash
python3 work/tools/work.py ready        # what's unblocked, in priority order
python3 work/tools/work.py roadblocks   # what's stuck / needs a decision
python3 work/tools/work.py lanes        # ship-readiness per feature area
python3 work/tools/work.py set T-XX --priority 2 --deferred no   # steer
```

Solo dev — main WIP happens on this machine, remote is mainly a backup. Canonical policy:
[work/WORKFLOW.md](work/WORKFLOW.md). Dependency graph: [work/DAG.md](work/DAG.md)
(`work.py dag` regenerates).

## Build & run

**Linux gate (CI-equivalent):**
```bash
bash scripts/gate.sh    # check.py + core build/ctest + ui tests
```

**Linux desktop app** (host GUI):
```bash
./build.sh                                   # host/build-linux/bin/Billet
scripts/install_desktop.sh                   # once: ~/.local/share applications + hicolor icons
./host/build-linux/bin/Billet                # run
```
`install_desktop.sh` writes `Billet.desktop` (`Name=Chudware`, `StartupWMClass=Billet`,
`Icon=Billet`) and resizes `assets/logo_icon.png` into the user hicolor theme — no root.
The running window sets `WM_CLASS=Billet` (and Wayland `app_id`) so the taskbar / alt-tab /
task manager bind to that entry. Verify: `xprop WM_CLASS` on the window should report
`"Billet", "Billet"`. Re-pin any old AI-CAD launcher after the first install.

**Windows desktop app** (VS 2022 + CMake):
```powershell
py -3.11 -m installer.provision --with-app-code   # once: runtime venv -> %APPDATA%\AI-CAD
build.bat                                          # compile + sync dist\AI-CAD\
dist\AI-CAD\AI-CAD.exe                             # run (first launch self-provisions)
```
`build.bat host` compiles the exe only; `build.bat app` re-syncs Python-only changes.
Headless smoke (no GUI): `AI-CAD.exe --design "a 30mm knurled hex standoff" out.png`.
Package a release: `py -3.11 -m installer.build_release --zip` (Windows walkthrough:
`docs/PACKAGING_WINDOWS.md`; Linux AppImage: `packaging/linux/build_appimage.sh`).
Both installers bundle the shared engine-host daemon — see `docs/HOSTD.md` for
its lifecycle, env vars, and troubleshooting.

**C++ core:**
```powershell
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure
```
Developed against the Python engine as a differential oracle — never weaken an assertion;
match Python unless Python is provably buggy. Cutover state lives in
[docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md) and the `T-RF*` board stream.

**Engine dev & tests** (managed venv at `%APPDATA%\AI-CAD\runtime\venv`; suites are plain
`main()`-runner modules):
```powershell
$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
& $py -m engine.doc.test_sketch_solver      # one engine suite
& $py -m mcp_server.test_roundtrip          # MCP roundtrip self-test
& $py -m mcp_server.server                  # run the MCP server (stdio)
& $py -m agent.run_agent "a 60x40x12mm bracket with two M5 clearance holes"   # needs API key
& $py -m agent.test_loop_mock               # agent loop, no key needed
```

## Where this is going

Billet is expanding into an **idea-to-production platform** — one prompt from concept
through mechanical design, electronics, firmware, structural checks, and priced BOM to a
ready-to-order package. Decision records, in reading order:

| Plan | What it covers |
|---|---|
| [docs/PLATFORM_ADR.md](docs/PLATFORM_ADR.md) | Platform pillars: DAG workflow orchestrator, sourcing/cost, spec-sheet matching, firmware/Renode, structural suite, format interop |
| [docs/AI_TOOLING_MASTER_PLAN.md](docs/AI_TOOLING_MASTER_PLAN.md) | Pillars 8–19: research tools, interaction, vision, narration, providers, translator, deliverables, flywheel, parts library, feature-UI productization, PLM |
| [docs/PLM_VERSION_CONTROL_PLAN.md](docs/PLM_VERSION_CONTROL_PLAN.md) | PLM version control for user documents (Pillar 19, workstream R): ease-of-use-first, hidden git vault, Project Hub |
| [docs/reference/README.md](docs/reference/README.md) | Clean-room, public-spec CAD format & data-model reference (folded in from a sibling clean-room project) for format-interop work — see `work/backlog/T-BL02.md` |

Currently parked (per [work/WORKFLOW.md](work/WORKFLOW.md) §3): custom-model training and
SaaS/cloud hosting — capture/eval seams stay, build-out waits.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the rules (license guardrails, OSS-first, style);
[work/README.md](work/README.md) documents the task system.

**Model-author contract (v1 `script` features):** scripts run with the full `build123d`
namespace in scope and assign the final shape to `result`; printed output is captured.
Frozen interface contracts live in [contracts/](contracts).
