# Chudware — Architecture

Post-cutover state (2026-07-23): the pure C++/CUDA kernel **`libchudware`** is the engine.
There is no Python CAD engine left. Canonical interface seams live in
[contracts/](contracts); the refactor handoff history is in
[docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md).

## Components

### `core/` → libchudware — the engine
Pure C++17/CUDA shared library: geom, doc, sketch, solver, cam, sim, ecad, mate, parts,
agent, util. Built with OCCT (`-DCHUDWARE_WITH_OCCT=ON`, preset `linux-core`). Public C ABI:
`core/api/libchudware.h`; RPC surface frozen in `contracts/worker_rpc.md`. All modeling,
solving, CAM, and sim execution happens here.

### `chudware-hostd` — engine-host daemon
Shared daemon that dlopens `libchudware` and serves it over a unix socket to any number of
clients (desktop GUI, WASM-side bridge, tools). Lifecycle, env vars, and troubleshooting are documented under `docs/`.

### `host/` — the sole GUI target (native + WASM)
~110k LOC under `host/src/ui/`: ribbon UI, FeatureManager tree, PropertyManager,
constraint-solver sketcher UX, copilot chat (`host/src/ui/copilot/`). Own OpenGL pipeline
under `host/src/render/`. Binary: `host/build-linux/bin/chudware`.
The **same `host/src/ui` tree compiles to WASM** under Emscripten for the web app
([Webservice/README.md](Webservice/README.md)); desktop/web parity is by construction and
enforced by `contracts/web_parity.md` + `scripts/web_parity_probe.py` (a gate stage that
probes a scratch hostd with the Python worker bridge disabled — what the WASM client faces).

### `services/` — the remaining Python
No engine code; Python lives here only where there is no C++ counterpart:
- `services/mcp/` — MCP server, stdio/JSON-RPC, so external agents (Claude Code,
  Cursor, …) drive the same document as the GUI. Server id `chudware` (`ai-cad`
  preserved). The `cad_guide` tool carries modeling/CAM/ECAD guidance.
- `services/agent/` — the built-in copilot loop, with provider adapters
  (Anthropic/OpenAI/local).
- `services/worker/` + `services/worker_main.py` — the stdio worker hostd spawns for
  services-side methods (worker_client / engine_backend / cpp_core / worker_router).
- `services/installer/` — provisioning, MCP registration, packaging.
- Pure-helper re-homes from the deleted Python engine: `services/ecad/`, `services/sim/`,
  `services/structural/`, `services/piping/`, `services/formats/`, `services/paths.py`,
  plus `dag/`, `deliverables/`, `plm/`, `browser/`, `ipc/`, `serving/`, `verify/`.

## RPC seams (frozen contracts)

- `contracts/worker_rpc.md` — the hostd/worker method surface (doc ops, features, sim, …).
- `contracts/ecad/ecad_rpc.md` — ECAD schematic/PCB/sim RPCs.
- `contracts/web_parity.md` — which methods the WASM client may rely on (hostd-only).
- `contracts/doc_schema.md` / `contracts/doc_ops.md` — the document model and its ops.

Append-only; integrator-owned (see AGENTS.md swarm rules).

## Data flow

**GUI doc edit:** user action in `host/src/ui/` → JSON-RPC over unix socket → `chudware-hostd`
→ `libchudware` (doc model mutated, features rebuilt) → tessellation/scene back to the host's
GL viewport.

**Agent edit:** external agent → MCP server (`services/mcp`, stdio JSON-RPC) →
core_bridge → hostd/libchudware; the built-in copilot (`services/agent`) drives the same
surface. Services-side methods that have no C++ counterpart are routed by hostd to the
stdio worker (`services.worker_main`), which dispatches into `services/worker/` and the
pure-helper modules.

```
GUI (host/, native or WASM) ──unix socket──▶ chudware-hostd ──dlopen──▶ libchudware (core/)
                                                │ stdio
external agent ──MCP/stdio──▶ services/mcp ─────┤
copilot (services/agent) ───────────────────────┤
                                                ▼
                                       services.worker_main
                                       (services-side methods)
```

## Migration history

The Python→C++ strangler migration is **complete** (T-RF21, 2026-07-23): `engine_oracle/`
and the root `engine` symlink are deleted, `scripts/oracle_import_allowlist.txt` is empty,
and `scripts/check.py`'s `oracle_import_audit` gate fails on any new `engine.*` /
`engine_oracle.*` import in `services/`. The earlier `ui/imgui`/`ui/abstract` GUI shell was
retired and deleted; `host/` is the single GUI target. Per-node handoffs (files touched,
deferred, gotchas): [docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md). Current follow-ups live
on the work board ([work/](work)).

## Build & test

- `./build.sh` — core → `libchudware.so`, then host GUI → `host/build-linux/bin/chudware`.
- `bash scripts/gate.sh` — full Linux gate: `scripts/check.py` (6 stdlib gates incl.
  `oracle_import_audit`), core build + ctest, host build + WASM smoke, hostd lifecycle +
  web-parity probe, Python suites, headless `CHUDWARE_SHOT=m1` screenshot smoke.
- Core presets (`CMakePresets.json`): `linux-core`, `windows-core`, `windows-occt`.
- `ctest --test-dir core/build-linux` for the C++ suites; Python suites via the mcp venv
  (`~/.local/share/Chudware/mcp-venv/bin/python -m pytest <dir> -q`).
