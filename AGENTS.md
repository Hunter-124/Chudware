# AGENTS.md — Chudware (formerly Billet / AI-CAD) repo navigation + ownership

Canonical agent-navigation doc for the whole codebase. Read this first before
any work. It assumes you know nothing about the project.

## Project overview

Chudware is a **SolidWorks-class parametric CAD application** — a native C++
desktop app (ribbon UI, FeatureManager tree, PropertyManager, constraint-solver
sketcher) on the **OpenCASCADE** B-rep kernel, with an AI orchestrator and an
MCP server so the same document can be driven by hand or by any agent (the
built-in copilot, Claude Code, Cursor, …). Mechanical CAD, assemblies,
drawings, sheet metal, CAM, ECAD/firmware, FEA/CFD, sourcing/BOM — one document
model under all of it.

Naming (compat rules live in README.md): the product is **Chudware** (chudware.com). The
GUI target is `Chudware` and the binary is `chudware`. `CHUDWARE_*` env vars are the only
honored prefix (the legacy `AI_CAD_*`/`AICAD_*`/`BILLET_*`/`SHOPCRAFT_*` mapping was
removed). The MCP server id is `chudware`. Data dirs are unconditionally `Chudware/`
(appdata root + `~/Documents/Chudware` workspace, sessions in `Sessions/`); a pre-rename
`Billet`/`AI-CAD` install is copied forward once (copy-if-missing; old dirs never moved
or deleted). Back-compat that is NOT renamed (persisted on-disk / wire formats): PLM
vault dir `.aicadvault`, mesh format `.aicm`, git refs `refs/aicad/*`, and
`aicad.<x>/<n>` contract schema ids are kept for data compatibility.

Architecture in one paragraph: the C++ kernel `libchudware` (under `core/`) IS the
engine; there is no Python CAD engine. The C++ host under `host/` is the **sole GUI
source tree**: native builds ship from it on Linux, Windows, and macOS, and the same
tree compiles to WASM for the web app (`Webservice/README.md`). The host talks to the
shared `chudware-hostd` daemon (which dlopens `libchudware`) over local IPC, through the
C++ routing/IPC seam in `services/worker/` + `services/ipc/`. hostd runs the persistent
Python services worker (`services/worker_main.py`) for services-side methods. **Online
agent turns** run in the embedded Chudcode RPC runtime (`chud --mode rpc`), hosted by
`services/agent/chudcode_host.py`: Chudcode owns provider calls, auth, models, session
history, memory, and compaction; Chudware owns CAD tools, documents, roles, missions,
approvals, and guardrails. The native agent in `core/agent/run.cpp` is the
**offline-safe fallback** only (offline-safe local providers). Boundary details:
`.claude/skills/cad-chudcode-integration/SKILL.md`. The host builds its own OpenGL
pipeline under `host/src/render/`.

Canonical references:
- `ARCHITECTURE.md` — system architecture and locked decisions.
- `contracts/` — frozen interface contracts, the seams between components
  (append-only; see `contracts/README.md`).
- `docs/REFACTOR_LOG.md` — current handoff log (2026-08-14 onward); older entries
  are archived under `docs/refactor-log/`. Active plans live under `docs/plans/`.
- `docs/plans/2026-07-22-consolidation-refactor-dag.md` — historical record of the
  completed consolidation refactor (`T-RF1`..`T-RF23`); rationale, not pending work.

## Agent workflow on this repo (read before starting any task)

1. **Orient with graphify, not grep.** The repo has a knowledge graph at
   `graphify-out/`. Run `graphify query "<question>"` (or `graphify explain` /
   `graphify path`) before reading source — a repo hook enforces this. Read raw
   files only after the graph orients you. After modifying code, run
   `graphify update .` to keep it current (AST-only, no API cost).
2. **Task board.** Work is tracked in `work/` (git-tracked markdown, no external
   tracker): `python3 work/tools/work.py ready` / `board`. Board policy is
   `work/WORKFLOW.md`; task fields, lifecycle, and "done" are `work/README.md`.
3. **MCP freshness rule (binding)** — `work/WORKFLOW.md` §3: a change to an
   agent-facing workflow, tool, prompt, or context pack updates the MCP server
   instructions, `cad_guide` topics, and the touched `work/context/` pack in the
   same change, then `graphify update .`.
4. **Verify before review.** `bash scripts/gate.sh` must show no new reds. GUI
   work verifies via the headless screenshot harness (`CHUDWARE_SHOT=<scenario>`).

## Repo map

| Dir | Purpose |
|-----|---------|
| `core/` | C++ kernel → `libchudware`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests, plus the `chudware-hostd` daemon (`core/hostd/`). **The engine.** |
| `host/` | **The sole GUI source tree.** Production C++ host, compiled natively on Linux (GCC/Clang), Windows (MSVC), and macOS (AppleClang), and to WASM/Emscripten for the web app. Binary: `host/build-linux/bin/chudware`. Own GL pipeline (including the `.aicm` loader `host/src/render/aicm_loader.*`, also compiled into core tests) under `host/src/render/`. |
| `services/` | C++: `worker/` (host-side engine routing seam: `worker_client`, `engine_backend`, `cpp_core`) and `ipc/` (hostd client/transport/discovery), both compiled into `host/`. Python: `worker_main.py` (the persistent services worker hostd spawns), `agent/` (Chudcode session host, host tools, CAD roles/missions/governance), `mcp/` (MCP server for external agents), `installer/` (provisioning, MCP registration, packaging), pure helpers with no C++ counterpart (`ecad/`, `sim/`, `structural/`, `piping/`, `formats/`, …), plus `dag/`, `deliverables/`, `plm/`, `browser/`, `serving/` (LoRA adapter routing — not the webservice; that is top-level `Webservice/`), `verify/`, `dist/`. Python imports use canonical `services.*` names. |
| `contracts/` | Frozen interface contracts — THE seams. Append-only, integrator-owned. |
| `corpora/` | Consolidated test fixtures, referenced directly by their canonical `corpora/...` paths. |
| `work/` | Git-tracked solo task board (`tasks/`, `backlog/`, `work/tools/work.py`, `WORKFLOW.md` policy). |
| `docs/` | Design docs, references, plans, handoff log (`docs/README.md` indexes it). |
| `Webservice/` | Web app: the `host/` WASM GUI backed by a shared hostd. |
| `tools/`, `scripts/`, `vendor/`, `assets/`, `packaging/`, `benchmarks/`, `training/` | Dev tools; repo gates + audits (`check.py`, `gate.sh`); vendored deps; icons/fonts; packaging; perf benches; ML eval/telemetry (training itself is a deferred plan). |
| `scratch/` | Throwaway area (non-blocking). |

## GUI ownership policy

- **`host/` is the sole GUI source tree.** There is no second GUI tree. Native Linux,
  Windows, and macOS builds and the web app's WASM build all compile this tree.
- **Form/PropertyManager schema lives with the host GUI** (`host/src/ui/`) —
  the single source every build reads. A form/PM fix (selector kinds, wells,
  gating) belongs there.
- **OS-specifics belong behind build conditionals in the existing seams**, not in a
  second GUI: host-side shims such as `host/src/ui/file_dialog.*` and
  `host/src/win_chrome.*`, host-only devices such as cameras under
  `host/src/platform/`, engine-side paths in `core/util/platform_paths.cpp`.
- Scripted GUI launches (the gate's host shot stage) use `xvfb-run` when available
  unless `CHUDWARE_SHOT_HEADED=1` is set. Set that env var for interactive/walkthrough
  runs to see a real window.
- **Derived-asset honesty:** screenshot-derived icons must show REAL engine
  output. CAM/Manufacture icons must render the real `cam_simulate` toolpath
  over stock — each doubles as a smoke check of the op chain. If an op won't
  produce a renderable toolpath, file it as a CAM bug; never fake an icon.

## Build and test commands

The Linux gate is **`bash scripts/gate.sh`** — every change must leave it green. Its
header comment is the authoritative stage list. In outline: `scripts/check.py`
(source/audit gate), core build into `core/build-linux` (`cmake -S core` with
`-DCHUDWARE_WITH_OCCT=ON`, SKIP when system OCCT is absent) + ctest, host GUI build
(`host/build-linux`) + WASM build smoke (FATAL when emsdk is provisioned and the web
build breaks), `chudware-hostd` lifecycle smoke and the web parity probe
(`scripts/web_parity_probe.py` enforcing `contracts/web_parity.md` over a scratch hostd
with the Python worker bridge disabled — what the WASM client faces), Python suites in
the service venv, content/demo smokes, and the `CHUDWARE_SHOT=m1` headless screenshot
smoke, then a PASS/SKIP summary. Environment SKIPs are not failures;
`CHUDWARE_GATE_STRICT=1` makes them fatal (CI).

Other entry points:

- `./build.sh` — thin wrapper. Bare `./build.sh` (= `app`) builds **only** the host GUI
  → `host/build-linux/bin/chudware`. `./build.sh all` builds core (`libchudware.so` +
  `chudware-hostd` in the consumed core trees), then the host GUI, then the daemon.
  Modes: `default|app|core|daemon|wasm|all|stage|clean` (usage header in `build.sh`).
  Windows: `build.bat` / CMake presets.
- Two kinds of core tree: CMake presets (`CMakePresets.json`; see
  `docs/reference/BUILD_WINDOWS.md`) build into `build/<preset>` and test with
  `ctest --test-dir build/<preset>/core`; the trees the gate, GUI, and hostd consume are
  `core/build-linux` (gate) and `core/build-occt-linux` (`./build.sh` daemon/stage).
  A preset tree is NOT read by `gate.sh`, the GUI, or `CHUDWARE_LIBCHUDWARE`.
- Host GUI has its own CMakeLists: `cmake -S host -B host/build-linux
  -G Ninja -DCMAKE_BUILD_TYPE=Release && ninja -C host/build-linux Chudware`.
  CEF (embedded vendor sign-in browser) defaults ON and needs the SDK under
  `extern/cef/` (`-DCHUDWARE_ENABLE_CEF=OFF` for an SDK-less build).
- Python runtime pins: `requirements.txt` is the source of truth for the managed
  runtime (`%APPDATA%\Chudware\runtime` on Windows).

## Multi-agent operating rules (binding)

- **No worktree isolation.** Worktrees here branch from a stale base and
  silently revert `master`. Run all subagents non-isolated in the main tree.
- **One integrator agent** owns all shared / append-only files (top-level
  CMake, `contracts/`, `AGENTS.md`, `scripts/gate.sh`, `core/api/libchudware.h`,
  `contracts/worker_rpc.md`) and does **all git commits**. Leaf subagents
  never run git.
- **Leaf subagents never touch files outside their assigned scope**; each node
  lists its owned write-set; parallel nodes have disjoint write-sets.
- **Append-only shared files** get exactly one owner per round; the integrator
  merges.
- **A node is done when `scripts/gate.sh` is green.** The integrator rebuilds
  once per wave; leaf nodes do not self-build if a parallel node shares the
  build graph.
- **Each node appends a one-paragraph handoff** to `docs/REFACTOR_LOG.md`
  (files touched, deferred, gotchas).

## Code style and conventions

- Language of code, comments, and docs: **English**.
- C++17 baseline (`CMAKE_CXX_STANDARD 17` in the top-level, core, and host CMake);
  the mesh, manufacturing, twin, and print layers (and a few tests) require C++20 via
  `cxx_std_20`; CUDA is an optional LBM backend (`CHUDWARE_WITH_CUDA`, default OFF).
  MSVC builds use `/utf-8 /MP`.
- Minimal, scoped changes: a bug fix does not clean up surrounding code; match
  the surrounding file's naming, comment density, and idioms. No speculative
  generality.
- **YAGNI is binding while we refine toward shipping.** Build only what a
  current task, contract, or shipping requirement actually needs:
  - No features, options, config knobs, extension points, or abstractions
    "for later" — no interfaces with one implementation, no parameters
    nothing passes, no generic utilities with one caller.
  - Dead code gets deleted, not kept "just in case": unused functions,
    unreachable branches, stale `#if 0` blocks, orphaned files, and compat
    shims whose legacy consumer is gone. Git history is the archive.
  - Deferred work belongs in `work/` as a task (or a `deferred: yes` plan),
    not as half-built scaffolding in the tree.
  - When touching code, prefer the smallest design that satisfies the
    current contract; refactor to generality only when a second real use
    exists.
  - Before adding a dependency (vendored, pip, or CMake), prove an existing
    in-repo capability can't do the job.
- New engine functionality lands in `core/` (C++) behind the RPC surface.
  `scripts/check.py`'s `oracle_import_audit` fails on any `engine.*` /
  `engine_oracle.*` import in `services/`, `scripts/`, or `tools/`
  (`scripts/oracle_import_allowlist.txt` is empty). Services-side Python is for MCP
  tools, the agent host/policy, and pure helpers with no C++ counterpart.
- Subsystem-local `AGENTS.md` files exist (for example, `services/agent/AGENTS.md`
  and `services/mcp/AGENTS.md`); the deeper, more specific one wins. Keep them current
  when you change the conventions they describe.
- CMake 4.x note for `core/`: doctest's old `cmake_minimum_required` is
  bypassed via `SOURCE_SUBDIR`; do **not** set
  `CMAKE_POLICY_VERSION_MINIMUM` (it poisons the cache — wipe the affected core
  build tree if it ever gets in).

## Testing instructions

- Full gate: `bash scripts/gate.sh` (see above). Environment SKIPs (no OCCT, no venv,
  no xvfb) are honest SKIPs, not failures.
- Fast source/audit gate only: `python3 scripts/check.py`. It is not stdlib-only: some
  audits import the MCP tool catalog, so the interpreter needs the pinned audit
  packages (`CHECK_PYTHON` overrides; see `docs/reference/CI_RUNNERS.md`).
- Python suites: `<venv>/python -X utf8 -m pytest services/agent -q`
  (`-X utf8` matters on Windows). The gate resolves the venv (override with
  `CHUDWARE_GATE_PYTHON`). A root `conftest.py` exists.
- C++ core: `ctest --test-dir core/build-linux` (gate tree) or
  `ctest --test-dir build/<preset>/core` (preset tree); GUI-adjacent unit tests build
  into `host/build-linux/bin/` (`test_camera_backend`, `test_splat_renderer`, …).
- GUI verification: headless screenshot harness `CHUDWARE_SHOT=<scenario>`; use
  `xvfb-run` unless `CHUDWARE_SHOT_HEADED=1`.
- Test fixtures: `corpora/` (canonical); SW-parity fixtures are under
  `corpora/sw_parity/`.

## Security considerations

- Never commit secrets; config templates live in `config.example.json`. Do not
  read or transmit credential-bearing files.
- License cleanliness is gated: vendored and pip deps must be license-clean per
  `contracts/licenses.md` (e.g. OpenCV core only, no opencv-contrib).
- CEF ships `chrome-sandbox` etc. under `host/build-linux/bin/` — packaging
  handles the sandbox bits; don't hand-modify them.
- The MCP server is a local stdio/JSON-RPC harness; any change to its tool
  surface must follow the MCP freshness rule above (server instructions +
  `cad_guide` + context packs updated in the same change).
