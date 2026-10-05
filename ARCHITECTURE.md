# Chudware — Architecture

The C++ kernel **`libchudware`** is the engine; there is no Python CAD engine. Canonical
interface seams live in [contracts/](contracts); the current handoff log is
[docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md) (older entries under
[docs/refactor-log/](docs/refactor-log)).

## Components

### `core/` → libchudware — the engine
C++ shared library: geom, doc, sketch, solver, cam, sim, ecad, mate, parts, agent, util.
C++17 baseline; the mesh, manufacturing, twin, and print layers require C++20; an
optional CUDA LBM backend builds with `CHUDWARE_WITH_CUDA=ON` (default OFF). Built with
OCCT (`-DCHUDWARE_WITH_OCCT=ON`). Public C ABI: `core/api/libchudware.h`; RPC surface
frozen in `contracts/worker_rpc.md`. All modeling, solving, CAM, and sim execution
happens here.

### `chudware-hostd` — engine-host daemon
Shared daemon (`core/hostd/`) that dlopens `libchudware` and serves it over local IPC to
any number of clients (desktop GUI, WASM-side bridge, tools). Lifecycle, env vars, and
troubleshooting: [docs/reference/HOSTD.md](docs/reference/HOSTD.md).

### `host/` — the sole GUI target (native + WASM)
Ribbon UI, FeatureManager tree, PropertyManager, constraint-solver sketcher UX, copilot
chat (`host/src/ui/copilot/`). Own OpenGL pipeline under `host/src/render/` (including
the `.aicm` mesh loader `host/src/render/aicm_loader.*`, which core tests compile too).
Binary: `host/build-linux/bin/chudware`. The host reaches the engine through the C++
routing/IPC seam it compiles from `services/worker/` (`worker_client`, `engine_backend`,
`cpp_core`: in-process libchudware or hostd) and `services/ipc/` (client, transport,
discovery). The **same `host/src/ui` tree compiles to WASM** under Emscripten for the web
app ([Webservice/README.md](Webservice/README.md)). The hostd method set the WASM client
may rely on is pinned by `contracts/web_parity.md` and checked by
`scripts/web_parity_probe.py` (a gate stage that probes a scratch hostd with the Python
worker bridge disabled — what the WASM client faces).

### `services/` — Python services
No engine code; Python lives here only where there is no C++ counterpart:
- `services/mcp/` — MCP server, stdio/JSON-RPC, so external agents (Claude Code,
  Cursor, …) drive the same document as the GUI. Server id `chudware` (registration
  leaves legacy `ai-cad` client entries in place). The `cad_guide` tool carries
  modeling/CAM/ECAD guidance.
- `services/worker_main.py` — the persistent services worker hostd spawns for
  services-side methods and online agent turns.
- `services/agent/` — the Chudcode session host (`chudcode_host.py`), host-tool
  projection/dispatch, event mapping, and Chudware-owned roles, missions, delegation
  governance, and guardrails.
- `services/installer/` — provisioning, MCP registration, packaging.
- Pure helpers with no C++ counterpart: `services/ecad/`, `services/sim/`,
  `services/structural/`, `services/piping/`, `services/formats/`, `services/paths.py`,
  plus `dag/`, `deliverables/`, `plm/`, `browser/`, `serving/`, `verify/`.

### Agent runtime
- **Online:** the embedded Chudcode RPC runtime (`chud --mode rpc`, pinned by
  `vendor/chudcode/CHUDCODE_ARTIFACT.lock`) owns every online provider call, auth, model
  catalog, session history, memory, and compaction. Chudware owns CAD tools and schemas,
  documents/workspaces, roles, missions, approvals, guardrails, and event presentation.
- **Offline:** `core/agent/run.cpp` runs the native fallback with offline-safe local
  providers only.
- Boundary and lifecycle rules: `.claude/skills/cad-chudcode-integration/SKILL.md`.

## RPC seams (frozen contracts)

- `contracts/worker_rpc.md` — the hostd/worker method surface (doc ops, features, sim, …).
- `contracts/ecad/ecad_rpc.md` — ECAD schematic/PCB/sim RPCs.
- `contracts/web_parity.md` — which methods the WASM client may rely on (hostd-only).
- `contracts/doc_schema.md` / `contracts/doc_ops.md` — the document model and its ops.

Append-only; integrator-owned (see the multi-agent operating rules in AGENTS.md).

## Data flow

**GUI doc edit:** user action in `host/src/ui/` → `services/worker` routing seam →
JSON-RPC over local IPC → `chudware-hostd` → `libchudware` (doc model mutated, features
rebuilt) → tessellation/scene back to the host's GL viewport.

**Agent edit:** external agent → MCP server (`services/mcp`, stdio JSON-RPC) →
core_bridge → hostd/libchudware. A copilot turn goes host → hostd → `core/agent/run.cpp`
→ online: services worker (`services.worker_main`) → `services/agent` Chudcode session
host → `chud --mode rpc`, whose CAD host tools call back into the same document surface;
offline: the native fallback in `core/agent/run.cpp`. Other services-side methods with no
C++ counterpart are routed by hostd to the services worker and the pure-helper modules.

```
GUI (host/, native or WASM) ──local IPC──▶ chudware-hostd ──dlopen──▶ libchudware (core/)
                                                │ stdio            (offline agent fallback:
external agent ──MCP/stdio──▶ services/mcp      │                   core/agent/run.cpp)
                                                ▼
                                       services.worker_main
                                       (services-side methods;
                                        services/agent ──RPC──▶ chud --mode rpc)
```

## History

The Python engine (`engine_oracle/`) is deleted; `scripts/check.py`'s
`oracle_import_audit` fails on any new `engine.*` / `engine_oracle.*` import in
`services/`, `scripts/`, or `tools/`. The consolidation refactor (`T-RF1`..`T-RF23`,
including the zero-Python cutover T-RF21) is recorded in
[docs/refactor-log/2026-07-and-earlier.md](docs/refactor-log/2026-07-and-earlier.md); its
plan is [docs/plans/2026-07-22-consolidation-refactor-dag.md](docs/plans/2026-07-22-consolidation-refactor-dag.md).
Current follow-ups live on the work board ([work/](work)).

## Build & test

- `./build.sh` — host GUI only → `host/build-linux/bin/chudware`. `./build.sh all` —
  core (`libchudware.so` + `chudware-hostd`), then host GUI, then the daemon.
- `bash scripts/gate.sh` — full Linux gate (stage list in its header): `scripts/check.py`
  source/audit gate, core build + ctest, host build + WASM smoke, hostd lifecycle +
  web-parity probe, Python suites, headless `CHUDWARE_SHOT=m1` screenshot smoke.
- Core trees: CMake presets ([CMakePresets.json](CMakePresets.json)) build into
  `build/<preset>` (`ctest --test-dir build/<preset>/core`); the gate, GUI, and hostd
  consume `core/build-linux` / `core/build-occt-linux` (`ctest --test-dir core/build-linux`).
- Python suites via the service venv the gate resolves (default
  `~/.local/share/Chudware/mcp-venv/bin/python -m pytest <dir> -q`; override with
  `CHUDWARE_GATE_PYTHON`).
