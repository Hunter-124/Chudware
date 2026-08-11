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

Architecture in one paragraph: the pure C++/CUDA kernel `libchudware` (under
`core/`) IS the engine — the legacy Python engine (`engine_oracle/` + the root
`engine` symlink) was fully strangled out and deleted in T-RF21 (zero-Python
cutover, 2026-07-23); what Python remains lives under `services/` (MCP tools,
agent, worker seam, pure-helper modules with no C++ counterpart). The C++ host
under `host/` is the **sole GUI target** (~110k LOC under `host/src/ui/`); it
talks to the shared `chudware-hostd` daemon (which dlopens `libchudware`) over a unix
socket, with `python -m services.worker_main` as the stdio worker for the
services-side methods. The same `host/src/ui` tree compiles to WASM for the
web app (`Webservice/README.md`), so desktop and web stay at parity by
construction. The host builds its own OpenGL pipeline under `host/src/render/`.

Canonical references:
- `ARCHITECTURE.md` — system architecture and locked decisions.
- `contracts/` — frozen interface contracts, the seams between components
  (append-only; see `contracts/README.md`).
- `docs/REFACTOR_LOG.md` — refactor
  handoff log (each DAG node appends a paragraph: files touched, deferred,
  gotchas). Active plans live under `docs/plans/`.
- The active refactor plan (source of truth for the current consolidation
  effort — hygiene → agent harness → UI/UX → perf → zero-Python cutover):
  `docs/plans/2026-07-22-consolidation-refactor-dag.md`, tracked on the board
  as workstream `T-RF*`. (The older cursor-plan file was superseded
  2026-07-22 — see `docs/REFACTOR_LOG.md`.)

## Agent workflow on this repo (read before starting any task)

1. **Orient with graphify, not grep.** The repo has a knowledge graph at
   `graphify-out/`. Run `graphify query "<question>"` (or `graphify explain` /
   `graphify path`) before reading source — a repo hook enforces this. Read raw
   files only after the graph orients you. After modifying code, run
   `graphify update .` to keep it current (AST-only, no API cost).
2. **Solo-dev task board.** Work is tracked in `work/` (git-tracked markdown,
   no external tracker). `python3 work/tools/work.py ready` shows unblocked
   tasks; `work.py board` shows everything. No claim/coordination step — pick a
   task, do it, mark it done yourself. Policy (priorities, lanes,
   `kind: fix|overhaul|new`, `deferred: yes` plans you must not touch) is in
   `work/WORKFLOW.md`. Task lifecycle and DoD: `work/README.md` ("done" =
   DoD boxes checked, `scripts/gate.sh` green, `graphify update .` run).
3. **MCP freshness rule (binding).** Any task that adds/changes an agent-facing
   workflow, tool, prompt, or context pack must, in the same branch: update the
   MCP server instructions (`services/mcp/`), update `cad_guide` topics if
   modeling/CAM/ECAD-facing, refresh the touched `work/context/` section pack,
   and run `graphify update .`.
4. **Verify before review.** `bash scripts/gate.sh` must show no new reds. GUI
   work verifies via the headless screenshot harness (`CHUDWARE_SHOT=<scenario>`).

## Repo map (current on-disk reality)

| Dir | Purpose |
|-----|---------|
| `core/` | Pure C++/CUDA kernel → `libchudware`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. **The engine** (zero-Python since T-RF21). |
| `host/` | **The single GUI target.** Production C++ desktop host (~110k LOC under `host/src/ui/`), cross-platform (Linux GCC/Clang + Windows MSVC); the same tree compiles to WASM for the web app. Binary: `host/build-linux/bin/Chudware` (`Chudware` compat symlink). Own GL pipeline under `host/src/render/`. |
| `services/` | Python services: `agent/` (built-in agent loop), `mcp/` (MCP server for external agents), `worker/` (C++ engine routing seam, four modules: worker_client / engine_backend / cpp_core / worker_router), `worker_main.py` (the stdio worker hostd spawns), `installer/` (provisioning, MCP registration, packaging), `ecad/`+`sim/`+`structural/`+`piping/`+`formats/` (pure-helper re-homes from the deleted Python engine), plus `dag/`, `deliverables/`, `plm/`, `browser/`, `ipc/`, `serving/` (LoRA adapter routing — not the webservice; the webservice is top-level `Webservice/`), `verify/`, `dist/`. All Python imports use canonical `services.*` names. |
| `ui/viewport/` | AICM mesh-format loader (`render/aicm_loader.*`) consumed by `core/` (libchudware tests/tools). The rest of the former "shared renderer" extraction was orphaned (host builds its own pipeline in `host/src/render/`) and was purged in T-RF2. `ui/imgui`/`ui/abstract` (the AI-CAD2 shell) are retired and gone. |
| `platform/` | Cross-OS shims (dialogs, DPI, paths, resources, window) with `_linux`/`_win32` implementations. |
| `contracts/` | Frozen interface contracts — THE seams. Append-only, integrator-owned. |
| `corpora/` | Consolidated test fixtures (`sketch/`, `render/`, `sw_parity/`, `sw_fixtures/`), referenced directly by their canonical paths. |
| `work/` | Git-tracked solo task board (`tasks/`, `backlog/`, `tools/work.py`, `WORKFLOW.md` policy). |
| `docs/` | Design docs, status notes, parity specs, handoffs, perf audits. |
| `tools/`, `scripts/`, `third_party/`, `assets/`, `packaging/`, `benchmarks/`, `training/` | Dev tools; repo gates + audits (`check.py`, `gate.sh`); vendored deps; icons/fonts; packaging; perf benches; ML eval/telemetry (training itself is a deferred plan). |
| `scratch/` | Throwaway area (non-blocking). |

## GUI ownership policy

- **`host/` is the SINGLE GUI target.** There is no second GUI tree; the
  `ui/imgui` + `ui/abstract` (AI-CAD2) shell is retired. (Historical: T-G11
  briefly deleted `host/`; that was reversed.)
- **Form/PropertyManager schema lives with the host GUI** (`host/src/ui/`) —
  the single source the app reads. A form/PM fix (selector kinds, wells,
  gating) belongs there.
- **OS-specifics belong in build files / the `platform/` seam**, not duplicated
  source. Per-OS bits (subprocess launch, file dialogs, GL loader) live behind
  CMake conditionals and the `platform/*_{linux,win32}.cpp` split.
- Scripted GUI launches (gate.sh, tools/gui_parity/run_all.py) always use
  `xvfb-run` when available unless `CHUDWARE_SHOT_HEADED=1` is set. Set that env
  var for interactive/walkthrough runs to see a real window.
- **Derived-asset honesty:** screenshot-derived icons must show REAL engine
  output. CAM/Manufacture icons must render the real `cam_simulate` toolpath
  over stock — each doubles as a smoke check of the op chain. If an op won't
  produce a renderable toolpath, file it as a CAM bug; never fake an icon.

## Build and test commands

The Linux gate is **`bash scripts/gate.sh`** — every DAG node must leave it
green. It runs, in order:

1. `python3 scripts/check.py` — stdlib-only fast gate (always runs, FATAL on red
   since T-RF3): `gui_lint` (icons exist in the atlas; no HiDPI-collapse theme bug),
   `feature_surface_audit` (every ribbon command routes to a real handler),
   `parity_audit` (SW_PARITY_SPEC claims resolve to real code).
2. Core CMake/Ninja build — `libchudware` with `-DCHUDWARE_WITH_OCCT=ON` (Linux
   `linux-core` preset). SKIPPED with a clear reason if system OCCT is absent —
   a SKIP is **not** a failure.
3. Host GUI build: `cmake -S host -B host/build-linux -G Ninja` + `ninja Chudware`;
   then a WASM build smoke (`./build.sh wasm` into a scratch dir — FATAL when
   emsdk is provisioned and the web build breaks).
4. `ctest` on the core build, plus the `chudware-hostd` lifecycle smoke, plus the
   **web parity probe** (`scripts/web_parity_probe.py` enforcing
   `contracts/web_parity.md` over a scratch hostd with the Python worker bridge
   disabled — what the WASM client faces).
5. Engine + agent pytest suites: `<venv>/python -X utf8 -m pytest engine/doc services/agent -q`.
   SKIPPED if the runtime venv/pytest is not provisioned on the box — again a
   SKIP, not a failure.
6. `CHUDWARE_SHOT=m1` headless screenshot smoke via `xvfb-run`, plus the
   sensor-node golden demo, then a PASS/SKIP summary.

Other entry points:

- `./build.sh` — thin wrapper: builds core → `libchudware.so`, then the host GUI
  → `host/build-linux/bin/Chudware`. (`build.bat` on Windows.)
- Core presets (`CMakePresets.json`): `linux-core`, `windows-core` (MSVC,
  OCCT off, self-contained), `windows-occt` (auto-downloads OCCT 7.9.3 +
  Netgen, cached under `third_party/`). See `docs/reference/BUILD_WINDOWS.md`.
- Host GUI has its own CMakeLists: `cmake -S host -B host/build-linux
  -G Ninja -DCMAKE_BUILD_TYPE=Release && ninja -C host/build-linux Chudware`.
  CEF (embedded vendor sign-in browser) defaults ON and needs the SDK under
  `extern/cef/` (`-DCHUDWARE_ENABLE_CEF=OFF` for an SDK-less build).
- Python runtime pins: `requirements.txt` (build123d, cadquery-ocp-novtk,
  numpy, scipy, pyvista/vtk, opencv-headless, …) — provisioned into the
  managed runtime (`%APPDATA%\Chudware\runtime` on Windows).

## Swarm operating rules (binding during the refactor)

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
- C++17 (`CMAKE_CXX_STANDARD 17` in both the top-level and host CMake); MSVC
  builds use `/utf-8 /MP`.
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
- The Python→C++ strangler migration is COMPLETE (T-RF21, 2026-07-23):
  `engine_oracle/` is deleted, `scripts/oracle_import_allowlist.txt` is empty,
  and `scripts/check.py`'s `oracle_import_audit` gate fails on any new
  `engine.*`/`engine_oracle.*` import in `services/`. New engine functionality
  lands in `core/` (C++) behind the RPC surface; services-side Python is for
  MCP tools, the agent loop, and pure helpers with no C++ counterpart.
- Subsystem-local `AGENTS.md` files exist (e.g. `engine/AGENTS.md`,
  `platform/AGENTS.md`); the deeper, more specific one wins. Keep them current
  when you change the conventions they describe.
- CMake 4.x note for `core/`: doctest's old `cmake_minimum_required` is
  bypassed via `SOURCE_SUBDIR`; do **not** set
  `CMAKE_POLICY_VERSION_MINIMUM` (it poisons the cache — wipe `core/build` if
  it ever gets in).

## Testing instructions

- Full gate: `bash scripts/gate.sh` (see stage list above). Environment SKIPs
  (no OCCT, no venv, no xvfb) are honest SKIPs, not failures.
- Fast stdlib gate only: `python3 scripts/check.py`.
- Python suites: `<venv>/python -X utf8 -m pytest services/agent -q`
  (`-X utf8` matters on Windows). A root `conftest.py` exists.
- C++ core: `ctest --test-dir core/build-linux` (or the preset's build dir);
  GUI-adjacent unit tests build into `host/build-linux/bin/` (`test_aicm`,
  `test_camera_backend`, `test_splat_renderer`, …).
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
  `cad_guide` + context packs updated in the same branch).
