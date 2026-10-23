# AGENTS.md

Guidance for AI coding agents working in this repository. Human contributors should start
with [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md); everything here applies
to them too.

## Project overview

Chudware is a parametric CAD application: a native C++ desktop app on the OpenCASCADE
B-rep kernel, with a built-in AI copilot and an MCP server, so the same document can be
driven by hand or by an agent. One document model covers mechanical CAD, assemblies,
drawings, sheet metal, CAM, ECAD/firmware, FEA/CFD, and sourcing.

- The C++ kernel **`libchudware`** (`core/`) is the engine; there is no Python CAD engine.
- The GUI (`host/`) is the only GUI source tree. It builds natively on Linux, Windows, and
  macOS, and compiles to WASM for the experimental browser app (`web/`).
- The GUI talks to the shared **`chudware-hostd`** daemon (`core/hostd/`), which dlopens
  `libchudware`, over local IPC through the C++ seam in `services/worker/` and
  `services/ipc/`.
- hostd runs the persistent Python services worker (`services/worker_main.py`). Online
  agent turns run in the embedded Chudcode runtime (`chud --mode rpc`), hosted by
  `services/agent/chudcode_host.py`; `core/agent/run.cpp` is the offline-safe fallback.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for components and data flow,
[contracts/](contracts/README.md) for the frozen interface seams, and
[docs/dev/](docs/dev/README.md) for subsystem conventions and invariants.

## Repository map

| Path | Contents |
|------|----------|
| `core/` | C++ kernel → `libchudware` (geom, doc, sketch, solver, cam, sim, ecad, mate, parts, agent, util, api, tests) and `chudware-hostd` (`core/hostd/`) |
| `host/` | GUI (native + WASM): ribbon, feature tree, property panels, sketcher, copilot chat (`host/src/ui/`), OpenGL renderer (`host/src/render/`) |
| `services/` | C++ routing/IPC seam (`worker/`, `ipc/`, compiled into `host/`); Python: `worker_main.py`, `agent/` (Chudcode session host, host tools, roles/missions), `mcp/` (MCP server), `installer/`, domain helpers (`ecad/`, `sim/`, `structural/`, `piping/`, `formats/`, `plm/`, …). Imports use `services.*` names |
| `contracts/` | Frozen, append-only interface contracts |
| `corpora/`, `fixtures/`, `samples/` | Test fixtures (canonical `corpora/...` paths) and sample parts |
| `docs/` | Design docs, references, developer notes, generated manual (`docs/README.md` indexes it) |
| `scripts/`, `tools/` | Gates and audits (`check.py`, `gate.sh`), build and packaging helpers, dev tools |
| `packaging/` | Installers and release packaging |
| `web/` | Experimental self-hosted browser app (gateway, deployment) |
| `website/` | chudware.com promotional site (Astro) |
| `assets/`, `vendor/`, `extern/` | Icons and fonts, pinned vendored artifacts, vendored third-party code |
| `benchmarks/` | Performance and validity benchmarks |

Subsystem `AGENTS.md` files exist under `services/agent/`, `services/mcp/`,
`services/worker/`, and `services/installer/`; the deeper, more specific file wins.

## Build and test

```bash
PYTHON=python3.13 scripts/provision_venv.sh pytest              # .venv-mcp (extra args go to pip)
./build.sh all                                                   # core, GUI, daemon
./build.sh                                                       # GUI only → host/build-linux/bin/chudware
.venv-mcp/bin/python scripts/check.py                            # fast source/audit gate
CHUDWARE_GATE_PYTHON=.venv-mcp/bin/python bash scripts/gate.sh   # full Linux gate
.venv-mcp/bin/python -X utf8 -m pytest services/agent -q         # one Python suite
ctest --test-dir core/build-linux                                # C++ core tests (gate tree)
```

- `scripts/gate.sh` is the authoritative Linux gate; its header lists the stages. A change
  must leave it green. Environment SKIPs (no OCCT, no `xvfb-run`, no emsdk) are not
  failures; `CHUDWARE_GATE_STRICT=1` makes them fatal.
- `build.sh` modes: `default|app|core|daemon|wasm|all|stage|clean` (see its header).
  Windows uses the `windows-*` CMake presets (`docs/reference/BUILD_WINDOWS.md`).
- CMake presets (`CMakePresets.json`) build into `build/<preset>`
  (`ctest --test-dir build/<preset>/core`). The gate, the GUI, and hostd consume
  `core/build-linux` and `core/build-occt-linux` instead; a preset tree is not read by them.
- Host only: `cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release &&
  ninja -C host/build-linux Chudware`. CEF defaults ON and needs the SDK under
  `extern/cef/` (`scripts/fetch_cef_posix.sh`); `-DCHUDWARE_ENABLE_CEF=OFF` builds without it.
- GUI verification: the headless screenshot harness, `CHUDWARE_SHOT=<scenario>` under
  `xvfb-run` (set `CHUDWARE_SHOT_HEADED=1` to see a real window).
- `requirements.txt` pins the Python runtime.

## Conventions

- English for code, comments, and docs. Match the surrounding file's naming, comment
  density, and idioms.
- C++17 baseline; the mesh, manufacturing, twin, and print layers (and a few tests) require
  C++20 via `cxx_std_20`. CUDA is an optional LBM backend (`CHUDWARE_WITH_CUDA`, default OFF).
  MSVC builds use `/utf-8 /MP`.
- Keep changes minimal and scoped; a bug fix does not clean up unrelated code. Build only
  what the change needs: no speculative options, single-implementation interfaces, or
  unused parameters. Delete dead code rather than keeping it "just in case".
- New engine functionality lands in `core/` behind the RPC surface.
  `scripts/check.py`'s `oracle_import_audit` fails on any `engine.*` / `engine_oracle.*`
  import in `services/`, `scripts/`, or `tools/`.
- Contracts are append-only; a frozen-section change needs a version bump
  (`contracts/README.md`). A new module ships its contract in the same change.
- The MCP tool surface stays consistent: a change to an agent-facing tool, prompt, or
  workflow updates the `services/mcp/server.py` instructions and the matching `cad_guide`
  topics in the same change (`services/mcp/test_mcp_freshness.py` checks coverage).
- GUI: form/property-panel schema lives in `host/src/ui/`. OS-specific code goes behind
  build conditionals in the existing seams (`host/src/ui/file_dialog.*`,
  `host/src/win_chrome.*`, `host/src/platform/`, `core/util/platform_paths.cpp`), never in a
  second GUI. Sizes scale through `host/src/ui/metrics.h`
  ([docs/dev/gui-theme-layout.md](docs/dev/gui-theme-layout.md)).
- Derived assets must be honest: screenshot-derived icons show real engine output (CAM
  icons render the real `cam_simulate` toolpath). If an operation cannot produce the output,
  fix the operation; never fake the asset.
- Before adding a dependency (vendored, pip, or CMake), check whether an existing in-repo
  capability can do the job; a new dependency needs a row in `contracts/licenses.md`.
- CMake 4.x and `core/`: doctest's old `cmake_minimum_required` is bypassed via
  `SOURCE_SUBDIR`; do not set `CMAKE_POLICY_VERSION_MINIMUM` (it poisons the cache; wipe the
  affected build tree if it gets in).

## Security

- Never commit secrets; configuration templates live in `config.example.json`. Do not read
  or transmit credential-bearing files such as `.env`.
- Dependencies must be license-compatible per `contracts/licenses.md`; copyleft tools stay
  out of process.
- CEF ships `chrome-sandbox` and related files under `host/build-linux/bin/`; packaging
  handles their permissions, so do not modify them by hand.
- Security issues are reported privately ([SECURITY.md](SECURITY.md)).
