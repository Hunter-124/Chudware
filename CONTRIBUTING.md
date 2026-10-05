# Contributing to Chudware

Solo dev + AI agents, trunk-based, fast cycles. Read this (and AGENTS.md) before touching
anything.

## Prerequisites

- CMake + Ninja + a C++ toolchain with C++20 support (C++17 baseline; the mesh,
  manufacturing, twin, and print layers need C++20) — GCC/Clang on Linux, MSVC/VS 2022 on
  Windows. CUDA is optional (`CHUDWARE_WITH_CUDA`, default OFF).
- Python 3.11 for services/agent/MCP work. The gate resolves the service venv (default
  `~/.local/share/Chudware/mcp-venv/`; override with `CHUDWARE_GATE_PYTHON`).
- The repo knowledge graph (`graphify`; see AGENTS.md workflow item 1).

---

## Workflow

### 1. Pick a task

```bash
python3 work/tools/work.py ready      # unblocked tasks, priority order
python3 work/tools/work.py board      # the full board
```

Tasks are markdown files with flat YAML front matter in `work/tasks/` + `work/backlog/`
(`status`, `priority`, `lane`, `kind`, `depends_on`, `deferred`, …) and body sections
including "Files / areas touched" and "Definition of done". Board policy:
[work/WORKFLOW.md](work/WORKFLOW.md); task format and lifecycle:
[work/README.md](work/README.md).

### 2. Do the work, gate, close

```bash
bash scripts/gate.sh        # check.py + core build/ctest + host build + WASM smoke + smokes
graphify update .           # after code changes (AST-only, no API cost)
```

Do not merge work that fails the gate. Environment SKIPs (no OCCT, no venv, no xvfb) are
honest SKIPs, not failures. Fast source/audit gate: `python3 scripts/check.py` — not
stdlib-only; the interpreter needs the pinned audit packages (`CHECK_PYTHON` overrides;
see [docs/reference/CI_RUNNERS.md](docs/reference/CI_RUNNERS.md)).

---

## Rules

**Contracts first.** Any new module ships a `contracts/*.md` the same day the module is
created. Contracts are append-only and integrator-owned: hand the draft to the integrator.

**Scoped write-sets.** A task's "Files / areas touched" section defines what it may touch;
parallel tasks must have disjoint write-sets. Extending a shared/append-only file
(`contracts/*`, top-level CMake, `core/api/libchudware.h`, `AGENTS.md`, `scripts/gate.sh`)
is the integrator's job — coordinate first.

**New engine functionality lands in `core/` (C++).** There is no Python engine;
`scripts/check.py`'s `oracle_import_audit` fails on any `engine.*` / `engine_oracle.*`
import under `services/`, `scripts/`, or `tools/`.

**Log discovered work, don't absorb it.** Found something out of scope? File a task and
stop. Scope creep creates overlapping write-sets and merge conflicts.

**One gate, no exceptions.** `bash scripts/gate.sh` must be green before merging to master.

---

## License guardrails

The product is commercial closed-source. License discipline is non-negotiable. The
authoritative ledger is [contracts/licenses.md](contracts/licenses.md): its row for a
dependency decides that dependency's posture, and its header records the current
distribution posture and open audit items. Open license debts and pending legal reviews are
tracked in [contracts/license_debt.md](contracts/license_debt.md). The ledger's rules:

- **Ledger row before use.** Every dependency — vendored, pip, CMake, external binary, or
  model weights — gets a row (SPDX identifier, role, posture) before it lands. No row means
  no classification: do not infer one.
- **In-process only when license-clean.** Linked or imported dependencies are permissive,
  or LGPL linked dynamically (as the ledger records for OCCT and Netgen).
- **Copyleft tools are isolated.** GPL tools are subprocess-only: user-installed or
  PATH-discovered, never linked, imported in-process, vendored, or bundled; a missing tool
  degrades gracefully instead of breaking unrelated features.
- **Excluded rows stay excluded.** A dependency the ledger marks EXCLUDED or RULED OUT
  (for example AGPL in-process libraries or non-commercial model licenses) is not used.

If a dependency's posture is uncertain, ask before merging.

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

**Work order format for agents:** `python3 work/tools/work.py prompt <ID>` emits the task as
a self-contained work order; its "Files / areas touched" section is the scope and its
"Definition of done" checklist is the finish line.

---

## Getting help

- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) + [AGENTS.md](AGENTS.md)
- Frozen interface contracts: [contracts/](contracts/)
- Task system: [work/README.md](work/README.md)
