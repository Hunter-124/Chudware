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

Naming (compat rules live in README.md): the product **displays as Chudware**;
the GUI binary/target stays `Billet` with an `AI-CAD` compat symlink beside it.
`BILLET_*` env vars are canonical; `AI_CAD_*`/`AICAD_*` are still honored. The
MCP server id is `billet` with `ai-cad` preserved.

Architecture in one paragraph: the pure C++/CUDA kernel `libaicad` (under
`core/`) is strangling out the legacy Python engine (`engine_oracle/`, with the
root `engine` symlink preserving imports). The two are kept in lockstep by a
per-subsystem parity corpus until each Python module is flipped off and
deleted. The C++ host under `host/` is the **sole GUI target** (~52k LOC under
`host/src/ui/`); it talks to the Python engine worker over JSON-RPC on
stdio/local socket during the migration, and links `libaicad` increasingly
in-process. The shared OpenGL render pipeline lives in `ui/viewport/`.

Canonical references:
- `ARCHITECTURE.md` — system architecture and locked decisions.
- `contracts/` — frozen interface contracts, the seams between components
  (append-only; see `contracts/README.md`).
- `docs/PLAN.md` — design + milestones. `docs/REFACTOR_LOG.md` — refactor
  handoff log (each DAG node appends a paragraph: files touched, deferred,
  gotchas).
- The active modular-refactor plan (source of truth for the target layered
  layout and per-phase DAG):
  `/home/hunter/.cursor/plans/cad_modular_refactor_+_gui_abstraction_6371dd9f.plan.md`

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
   work verifies via the headless screenshot harness (`AI_CAD_SHOT=<scenario>`).

## Repo map (current on-disk reality)

| Dir | Purpose |
|-----|---------|
| `core/` | Pure C++/CUDA kernel → `libaicad`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. The zero-Python engine target. |
| `host/` | **The single GUI target.** Production C++ desktop host (~52k LOC under `host/src/ui/`), cross-platform (Linux GCC/Clang + Windows MSVC). Binary: `host/build-linux/bin/Billet` (`AI-CAD` compat symlink). |
| `engine_oracle/` | Frozen legacy Python engine, kept as differential parity oracle while `core/` catches up. Deleted LAST (node N6b). Root `engine` symlink preserves `python -m engine.*` imports. |
| `services/` | Python services: `agent/` (built-in agent loop; root `agent` symlink), `mcp/` (MCP server for external agents; root `mcp_server` symlink), `worker/` (engine routing seam: WorkerClient/DocMirror/bootstrap), `installer/` (provisioning, MCP registration, packaging), plus `dag/`, `deliverables/`, `plm/`, `browser/`, `ipc/`, `serving/`, `verify/`, `dist/`. |
| `ui/viewport/` | Shared OpenGL viewport pipeline: renderer/mesh/camera + render (AICM loader, id-pass picking, overlays), consumed by `host/`. **Only `ui/viewport` exists** — `ui/imgui`/`ui/abstract` (the AI-CAD2 shell) are retired and gone. |
| `platform/` | Cross-OS shims (dialogs, DPI, paths, resources, window) with `_linux`/`_win32` implementations. |
| `contracts/` | Frozen interface contracts — THE seams. Append-only, integrator-owned. |
| `corpora/` | Consolidated test fixtures (`sketch/`, `render/`, `sw_parity/`, `sw_fixtures/`). Root `corpus*` symlinks preserve legacy paths. |
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
  `xvfb-run` when available unless `AI_CAD_SHOT_HEADED=1` is set. Set that env
  var for interactive/walkthrough runs to see a real window.
- **Derived-asset honesty:** screenshot-derived icons must show REAL engine
  output. CAM/Manufacture icons must render the real `cam_simulate` toolpath
  over stock — each doubles as a smoke check of the op chain. If an op won't
  produce a renderable toolpath, file it as a CAM bug; never fake an icon.

## Build and test commands

The Linux gate is **`bash scripts/gate.sh`** — every DAG node must leave it
green. It runs, in order:

1. `python3 scripts/check.py` — stdlib-only fast gate (always runs):
   `gui_lint` (icons exist in the atlas; no HiDPI-collapse theme bug),
   `feature_surface_audit` (every ribbon command routes to a real handler),
   `parity_audit` (SW_PARITY_SPEC claims resolve to real code).
   Currently RECORDED non-fatal (pre-existing red); `CHECK_PY_FATAL=1` flips it
   back to hard-gating once the gaps are fixed.
2. Core CMake/Ninja build — `libaicad` with `-DAICAD_WITH_OCCT=ON` (Linux
   `linux-core` preset). SKIPPED with a clear reason if system OCCT is absent —
   a SKIP is **not** a failure.
3. `ctest` on the core build (best-effort).
4. Host GUI build: `cmake -S host -B host/build-linux -G Ninja` + `ninja Billet`.
5. Engine + agent pytest suites: `<venv>/python -X utf8 -m pytest engine/doc agent -q`.
   SKIPPED if the runtime venv/pytest is not provisioned on the box — again a
   SKIP, not a failure.
6. `AI_CAD_SHOT=m1` headless screenshot smoke via `xvfb-run`, plus the
   sensor-node golden demo, then a PASS/SKIP summary.

Other entry points:

- `./build.sh` — thin wrapper: builds core → `libaicad.so`, then the host GUI
  → `host/build-linux/bin/Billet`. (`build.bat` on Windows.)
- Core presets (`CMakePresets.json`): `linux-core`, `windows-core` (MSVC,
  OCCT off, self-contained), `windows-occt` (auto-downloads OCCT 7.9.3 +
  Netgen, cached under `third_party/`). See `docs/BUILD_WINDOWS.md`.
- Host GUI has its own CMakeLists: `cmake -S host -B host/build-linux
  -G Ninja -DCMAKE_BUILD_TYPE=Release && ninja -C host/build-linux Billet`.
  CEF (embedded vendor sign-in browser) defaults ON and needs the SDK under
  `extern/cef/` (`-DAI_CAD_ENABLE_CEF=OFF` for an SDK-less build).
- Python runtime pins: `requirements.txt` (build123d, cadquery-ocp-novtk,
  numpy, scipy, pyvista/vtk, opencv-headless, …) — provisioned into the
  managed runtime (`%APPDATA%\AI-CAD\runtime` on Windows).

## Swarm operating rules (binding during the refactor)

- **No worktree isolation.** Worktrees here branch from a stale base and
  silently revert `master`. Run all subagents non-isolated in the main tree.
- **One integrator agent** owns all shared / append-only files (top-level
  CMake, `contracts/`, `AGENTS.md`, `scripts/gate.sh`, `core/api/libaicad.h`,
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
- During the Python→C++ strangler migration, ported subsystems flip routing in
  `services/worker/` and delete their Python module in `engine_oracle/` in the
  same commit; `core/tests/` mirrors the Python suites (e.g. `test_sketch.cpp`
  mirrors `engine/doc/test_sketch_solver.py`) and parity is proven with diff
  harnesses (`tools/solver_diff.py`) before the switch.
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
- Python suites: `<venv>/python -X utf8 -m pytest engine/doc agent -q`
  (`-X utf8` matters on Windows). A root `conftest.py` exists.
- C++ core: `ctest --test-dir core/build-linux` (or the preset's build dir);
  GUI-adjacent unit tests build into `host/build-linux/bin/` (`test_aicm`,
  `test_camera_backend`, `test_splat_renderer`, …).
- GUI verification: headless screenshot harness `AI_CAD_SHOT=<scenario>`; use
  `xvfb-run` unless `AI_CAD_SHOT_HEADED=1`.
- Test fixtures: `corpora/` (canonical) with root `corpus*` symlinks;
  SW-parity fixtures under `corpus_sw_parity/`.

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
