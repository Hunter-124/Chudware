# Contributing to Chudware

Solo dev + AI agents, trunk-based, fast cycles. Read this (and AGENTS.md) before touching
anything.

## Prerequisites

- CMake + Ninja + a C++17 toolchain (GCC/Clang on Linux, MSVC/VS 2022 on Windows)
- Python 3.11 for services/agent/MCP work (managed venv:
  `~/.local/share/Chudware/mcp-venv/`)
- The repo knowledge graph: run `graphify update .` after any code change

---

## Workflow

### 1. Pick a task

```bash
python3 work/tools/work.py ready      # unblocked tasks, priority order
python3 work/tools/work.py board      # the full board
```

Tasks are YAML-front-matter files in `work/tasks/` + `work/backlog/` with `priority`,
`lane`, `kind` (`fix`|`overhaul`|`new`), `blocked_by`, and `status`. Policy:
[work/WORKFLOW.md](work/WORKFLOW.md). Solo dev — no claim step; pick a task, do it, mark
it done yourself.

### 2. Do the work, gate, close

```bash
bash scripts/gate.sh        # check.py + core build/ctest + host build + WASM smoke + smokes
graphify update .           # after code changes (AST-only, no API cost)
```

Do not merge work that fails the gate. Environment SKIPs (no OCCT, no venv, no xvfb) are
honest SKIPs, not failures. Fast stdlib-only gate: `python3 scripts/check.py`.

---

## Rules

**Contracts first.** Any new module ships a `contracts/*.md` the same day the module is
created. Contracts are append-only and integrator-owned.

**Scoped write-sets.** Each task's `file_list` defines what it may touch; parallel tasks
must have disjoint file lists. Extending a shared/append-only file (`contracts/*`,
top-level CMake, `core/api/libchudware.h`, `AGENTS.md`, `scripts/gate.sh`) is the
integrator's job — coordinate first.

**New engine functionality lands in `core/` (C++).** The Python engine is deleted
(T-RF21); `scripts/check.py`'s `oracle_import_audit` fails on any new `engine.*` /
`engine_oracle.*` import under `services/`.

**Log discovered work, don't absorb it.** Found something out of scope? File a task and
stop. Scope creep creates overlapping write-sets and merge conflicts.

**One gate, no exceptions.** `bash scripts/gate.sh` must be green before merging to master.

---

## License guardrails

The product is commercial closed-source. License discipline is non-negotiable; the full
policy is [contracts/licenses.md](contracts/licenses.md) — read it. The short version:

| OK to bundle | Subprocess only (must offer source in installer) | Never |
|---|---|---|
| LangGraph, PyNite, Renode, probe-rs, ngspice, Verilator, openNURBS, ezdxf, IfcOpenShell (LGPL, dynamic link) | CalculiX, OpenFOAM, GHDL, Icarus, esptool, Arduino CLI, LibreDWG | AGPL anything, GPL linking, GrabCAD scraping, retailer scraping, bundling IBC/NDS/Eurocode text |

When adding a dependency: check its SPDX identifier, add it to `contracts/licenses.md`,
and confirm bundle vs subprocess. If uncertain, ask before merging.

For format reverse-engineering: interoperability RE is protected under DMCA §1201(f) and
EU Software Directive Art. 6 **only if clean-room** — the person documenting a format and
the person writing the parser must never share competitor source, SDKs, or EULA-bound
tooling.

---

## AI-tooling

AI agents (Claude Code, the in-app copilot) are first-class contributors. Same rules apply.

**Start every task with a graph query:**
```bash
graphify query "<what you are about to build>"
graphify path "<ModuleA>" "<ModuleB>"   # cross-file relationships
graphify explain "<concept>"            # focused concept lookup
```

**Use the in-repo MCP server** to drive design work from an external agent:
`python3 services/installer/setup_mcp_clients.py` (Windows: `mcp_setup.bat`) registers it
with Claude Code, Cursor, VS Code, opencode.

**Work order format for agents:** paste the task file (YAML front matter + body) as the
work order; `file_list` and `end_condition` define the scope and the finish line.

**After code changes:** `graphify update .`.

---

## Getting help

- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) + [AGENTS.md](AGENTS.md)
- Frozen interface contracts: [contracts/](contracts/)
- Task system: [work/README.md](work/README.md)
