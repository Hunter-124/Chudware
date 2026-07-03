# ShopCraft (formerly AI-CAD)

A **SolidWorks-class parametric CAD application** — native C++ desktop app (ribbon UI,
FeatureManager tree, constraint-solver sketcher) on the **OpenCASCADE** B-rep kernel, with an
**AI orchestrator** and an **MCP server** so the same document can be driven by hand or by any
agent (the built-in copilot, Claude Code, Cursor, …). Mechanical CAD, assemblies, drawings,
sheet metal, CAM, ECAD, FEA/CFD, sourcing/BOM — one document model under all of it.

---

## 🤖 If you are an AI agent, start here

Five rules put you on the right path instantly — everything else hangs off them:

1. **Orient with graphify, not grep.** This repo has a knowledge graph. Run
   `graphify query "<your question>"` (or `graphify explain` / `graphify path`) before reading
   source — a repo hook **enforces** this. Read raw files only after the graph orients you.
2. **All work flows through the task board.** `python3 work/tools/work.py ready` shows what's
   claimable; `claim --owner <your-name>` is the *only* legitimate way to take a task (atomic,
   race-safe). Never work unclaimed, never merge to master, never set a task to `done` — humans
   merge. The full loop: [work/prompts/autopilot.md](work/prompts/autopilot.md).
3. **The workflow policy is [work/WORKFLOW.md](work/WORKFLOW.md).** Priorities, lanes
   (feature areas), `kind: fix|overhaul|new` (fixes claim first), `deferred: yes` plans you must
   not touch, the roadblock-skip protocol, and the MCP-freshness rule (ship a capability and its
   discoverability in the same branch).
4. **Verify before review.** `bash scripts/gate.sh` (build + tests) must show no new reds;
   run `graphify update .` after code changes. GUI work verifies via the headless screenshot
   harness (`AI_CAD_SHOT=<scenario>`).
5. **Coordinate, don't collide.** `work_board` / `issue_list` before touching anything;
   questions go in `work/questions/<task-id>.md`; progress notes via `note`. Prefer the
   `aicad-team` MCP tools (`whoami` once, then `claim`, `work_prompt`, `note`, `set_status`) —
   same system as the CLI.

New-to-a-subsystem? `section_context(section="core-cam")` (or the `/ctx-*` skills) loads a
maintained source map instead of you re-exploring it.

## 🧑‍🔧 If you are a human, start here

One-time, per-machine setup of the internal tooling (each contributor runs this locally;
nothing touches shared repo state):

```bash
# 1. Build the code knowledge graph for your checkout (gitignored, regenerable)
graphify .                               # once; `graphify hook install` keeps it fresh per-commit

# 2. Register the team-coordination MCP server (Claude Code CLI + Claude Desktop)
python3 work/tools/setup_team_tools.py   # then approve the one-time trust prompt in `claude`

# 2a. Tell the tooling who you are (once) — GitHub username, auto-filled from gh/git
python3 work/tools/work.py whoami --github <your-username>   # or whoami(github=...) via MCP
#   stored gitignored in work/sync/identity.json; becomes your default task owner + the
#   author of your board commits (via per-commit env, never `git config`) and powers
#   `work.py mine` / the my_work MCP tool ("what's waiting on me")

# 3. Register the ShopCraft CAD MCP server with your agent clients
python3 services/installer/setup_mcp_clients.py           # (Windows: mcp_setup.bat)
python3 services/installer/setup_mcp_clients.py --detect  # verify what's wired where

# 4. Open Mission Control — the team dashboard
work/run_version_control.sh              # (Windows: work\run_version_control.bat)
```

**Mission Control** (`localhost:8787`) is the master view: what needs *you* right now
(review-queue merges, stuck tasks, open questions — ordered by impact, with "safe to skip"
verdicts), per-feature progress lanes sorted by closest-to-shipping, and a pan/zoom dependency
map of every task. You steer from there too: priorities, feature areas, and pausing/unpausing
plans write straight back to the board. Optional extras: `work/workd.sh <name>` (presence
daemon) and `work/report.sh` (AI-guided bug/idea reporter → GitHub issue + backlog task).

When you sit down to work the board, follow the human protocol in
[work/WORKFLOW.md](work/WORKFLOW.md) §5 — check `work.py mine` for what's waiting on you,
merge the review queue top-down, answer the human-only questions, adjust priorities.
Everything else runs without you.

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
- **GUI migration (workstream G, nearly done).** The modular `ui/imgui` + `ui/viewport` app
  (liquid-glass overlay design — full-bleed viewport, transparent floating columns) has
  absorbed the legacy `host/` GUI wave-by-wave; `host/` is deleted at T-G11 after the final
  polish tasks land. New UI work goes in `ui/`, never `host/`. Rename compatibility (T-G3/T-G10):
  `SHOPCRAFT_*` env vars canonical (`AI_CAD_*` still honored), binary `AI-CAD2` + `ShopCraft`
  symlink, MCP id `shopcraft` (existing `ai-cad` preserved), existing `AI-CAD` appdata dirs
  keep working.

## How it works

```
            ┌────────────────────────────────────────────────────────────┐
            │  ShopCraft (native C++ — ribbon UI, feature tree, sketcher) │
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
work/            task board + team tooling (WORKFLOW.md, tasks/, tools/, Mission Control)
```

## How work gets organized (Workflow v2)

Tasks are git-tracked markdown files in `work/tasks/` + `work/backlog/` with frontmatter:
`priority` (1–5), `lane` (user-facing feature area — the dashboard swim-rows), `kind`
(`fix` | `overhaul` | `new`; fixes claim first within a priority), `deferred: yes` (parked
plans — currently ML training and SaaS hosting — invisible to claiming). Key commands:

```bash
python3 work/tools/work.py ready        # what's claimable, in claim order
python3 work/tools/work.py roadblocks   # what's waiting on a HUMAN, with impact verdicts
python3 work/tools/work.py lanes        # ship-readiness per feature area
python3 work/tools/work.py set T-XX --owner you --priority 2 --deferred no   # steer
```

Roles: **Sonnet drivers** claim and execute; **one Fable foreman** reviews, merges, triages
roadblocks (low-impact roadblocks get skipped/deferred, not queued); **humans** merge to done
and answer human-only questions. Canonical policy: [work/WORKFLOW.md](work/WORKFLOW.md).
Dependency graph: [work/DAG.md](work/DAG.md) (`work.py dag` regenerates).

## Build & run

**Linux gate (CI-equivalent):**
```bash
bash scripts/gate.sh    # check.py + core build/ctest + ui tests
```

**Windows desktop app** (VS 2022 + CMake):
```powershell
py -3.11 -m installer.provision --with-app-code   # once: runtime venv -> %APPDATA%\AI-CAD
build.bat                                          # compile + sync dist\AI-CAD\
dist\AI-CAD\AI-CAD.exe                             # run (first launch self-provisions)
```
`build.bat host` compiles the exe only; `build.bat app` re-syncs Python-only changes.
Headless smoke (no GUI): `AI-CAD.exe --design "a 30mm knurled hex standoff" out.png`.
Package a release: `py -3.11 -m installer.build_release --zip`.

**C++ core:**
```powershell
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure
```
Developed against the Python engine as a differential oracle — never weaken an assertion;
match Python unless Python is provably buggy. See [docs/CPP_MIGRATION.md](docs/CPP_MIGRATION.md).

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

ShopCraft is expanding into an **idea-to-production platform** — one prompt from concept
through mechanical design, electronics, firmware, structural checks, and priced BOM to a
ready-to-order package. Decision records, in reading order:

| Plan | What it covers |
|---|---|
| [docs/PLATFORM_ADR.md](docs/PLATFORM_ADR.md) | Platform pillars: DAG workflow orchestrator, sourcing/cost, spec-sheet matching, firmware/Renode, structural suite, format interop |
| [docs/AI_TOOLING_MASTER_PLAN.md](docs/AI_TOOLING_MASTER_PLAN.md) | Pillars 8–19: research tools, interaction, vision, narration, providers, translator, deliverables, flywheel, parts library, feature-UI productization, PLM |
| [docs/PLM_VERSION_CONTROL_PLAN.md](docs/PLM_VERSION_CONTROL_PLAN.md) | PLM version control for user documents (Pillar 19, workstream R): ease-of-use-first, hidden git vault, Project Hub |

Currently parked (per [work/WORKFLOW.md](work/WORKFLOW.md) §3): custom-model training and
SaaS/cloud hosting — capture/eval seams stay, build-out waits.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the rules (license guardrails, OSS-first, style);
[work/README.md](work/README.md) documents the task system;
[work/prompts/autopilot.md](work/prompts/autopilot.md) is the autonomous-driver loop.

**Model-author contract (v1 `script` features):** scripts run with the full `build123d`
namespace in scope and assign the final shape to `result`; printed output is captured.
Frozen interface contracts live in [contracts/](contracts).
