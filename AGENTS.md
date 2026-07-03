# AGENTS.md — ShopCraft (AI-CAD) repo navigation + ownership

This is the canonical agent-navigation and ownership doc for the whole ShopCraft
(formerly AI-CAD; see the T-G10 compatibility rules in README.md — `SHOPCRAFT_*`
env vars canonical with `AI_CAD_*` honored, `shopcraft` MCP id with `ai-cad`
preserved, legacy appdata dir honored) codebase. Read this first before any
refactor work.

## Project

ShopCraft is a parametric CAD application. The core geometry/document/solver/CAM
engine is a C++ library `libaicad` (under `core/`) that is strangling out a
legacy Python `engine/` oracle — the two are kept in lockstep by a per-subsystem
parity corpus until each Python module is flipped off and deleted. The desktop
GUI is an ImGui shell (under `app/`, with the older `host/` tree being migrated
into `app/` and then deleted). The active refactor plan lives at
`/home/hunter/.cursor/plans/cad_modular_refactor_+_gui_abstraction_6371dd9f.plan.md`
and is the source of truth for the target layered layout and the per-phase DAG.

## Repo map (current on-disk reality)

The target layout (see the plan's "Target layered layout" section) re-homes most
of these dirs; until a move node lands, the dirs below are where the code
physically lives today. "→ moves" marks dirs slated to move per the plan.

| Dir | Purpose (current) | Plan target |
|-----|-------------------|-------------|
| `core/` | Pure C++/CUDA kernel → `libaicad` (the zero-Python engine target): geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. | Stays `core/`. |
| `host/` | Legacy C++ ImGui desktop host: `src/ui/*` (panels/editors), `src/render/*` (GL viewport), `src/core/*` (app_state/doc_mirror/worker_client). **End-of-life** — migrated into `app/` + `ui/` + `services/worker/`, then deleted (N4b). | → `app/` + `ui/` + `services/worker/`; deleted N4b. |
| `app/` | New ImGui desktop GUI rewrite (`src/ui/*`, `src/platform/*`). The first concrete backend over the future `ui/abstract` view-model seam. | → `ui/imgui/` (+ `platform/`). |
| `engine_oracle/` | Python parity oracle (frozen while `core/` catches up). Root `engine` symlink preserves imports. Deleted LAST (N6b). | Deleted N6b. |
| `services/agent/` | Built-in agent loop (Python now → C++ later). Root `agent` symlink. | C++ at N5e. |
| `services/mcp/` | MCP server for external agents. Root `mcp_server` symlink. | C++ at N5e. |
| `services/installer/` | Provisioning, MCP registration, packaging. Root `installer` symlink. | Stays until N6. |
| `services/worker/` | Engine routing seam (scaffold; lands N4a from `host/src/core/*`). | N4a. |
| `ui/abstract/` | Framework-agnostic UI seam (view-model, commands, panel specs). **No ImGui.** | P2. |
| `ui/imgui/` | ImGui concrete backend (scaffold; `app/` lands here P3). | P3. |
| `ui/viewport/` | Shared OpenGL viewport pipeline (scaffold; `host/src/render/*` lands N2c). | N2c/P3. |
| `platform/` | Cross-OS shims (scaffold; `app/src/platform/*` lands P3). | P3. |
| `corpora/` | Consolidated test fixtures (`sketch/`, `render/`, `sw_parity/`, `sw_fixtures/`). Root symlinks preserve legacy paths. | Done (N1d). |
| `docs/` | Design docs, status notes, parity specs. | Stays `docs/`. |
| `contracts/` | Frozen interface contracts — THE seams. Append-only, integrator-owned. | Done (N0c). |
| `tools/` | One-off dev/analysis tools. | Stays. |
| `scripts/` | Repo gates + audits: `check.py`, `gate.sh`, etc. | Stays. |
| `third_party/` | Vendored third-party deps. | Stays. |
| `scratch/` | Throwaway scratch area. | Stays (non-blocking). |

## Swarm operating rules

Generalized from `docs/gui_rework_handoff.md` — these are binding for every
agent working in this repo during the refactor.

- **No worktree isolation.** Worktrees in this repo branch from a stale base and
  silently revert `master`. Run all subagents non-isolated in the main tree.
- **One integrator agent** owns all shared / append-only files (top-level
  CMake, `contracts/`, `AGENTS.md`, `scripts/gate.sh`, `core/api/libaicad.h`,
  `ui/abstract/command_registry.*`, `worker_rpc.md`) and does **all git
  commits**. Leaf subagents never run git.
- **Leaf subagents never touch files outside their assigned scope** and never
  run git. Each node lists its owned write-set.
- **Disjoint write-sets** for any parallel pair of nodes.
- **Append-only shared files** (`contracts/*`, `core/api/libaicad.h`,
  `ui/abstract/command_registry.*`, top-level CMake, `worker_rpc.md`) get
  exactly one owner per round; the integrator merges.
- **A node is done when `scripts/gate.sh` is green.** The integrator rebuilds
  once per wave; leaf nodes do **not** self-build if a parallel node shares the
  build graph.
- **Each node writes a one-paragraph handoff** to `docs/REFACTOR_LOG.md`
  (files touched, deferred, gotchas).

## Build / test cadence

- The Linux gate is `scripts/gate.sh` (created in N0d). It runs the stdlib
  `scripts/check.py` (always), then the C++ core CMake/Ninja build + ctest
  (skipped with a clear reason if OCCT is not installed), then the engine+agent
  pytest suites (skipped with a clear reason if the runtime venv / pytest is not
  provisioned). Every DAG node must leave `scripts/gate.sh` green.
- The engine + agent pytest suites (`<venv>/python -X utf8 -m pytest engine/doc agent -q`)
  need the runtime venv, which may be **absent on this Linux box** — that is a
  SKIP, not a failure.
- `scripts/check.py` is the stdlib-only fast gate (gui_lint icons/theme,
  feature_surface_audit, parity_audit) and always runs.

## Pointers

- Plan: `/home/hunter/.cursor/plans/cad_modular_refactor_+_gui_abstraction_6371dd9f.plan.md`
- Frozen contracts: `contracts/` (see `contracts/README.md`)
- Architecture: `ARCHITECTURE.md`
- C++ strangler status: `docs/CPP_MIGRATION.md`
- Refactor handoff log: `docs/REFACTOR_LOG.md`
