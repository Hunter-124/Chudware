# Chudware

Chudware is a **SolidWorks-class parametric CAD application**: a native C++ desktop app
(ribbon UI, FeatureManager tree, PropertyManager, constraint-solver sketcher) on the
**OpenCASCADE** B-rep kernel, with a built-in AI copilot and an **MCP server** so the same
document can be driven by hand or by any external agent (Claude Code, Cursor, …). One
document model sits under every domain — and the same UI tree compiles to WASM for the web
app, so desktop and web share one GUI source tree.

Naming: the product is **Chudware** (chudware.com). The GUI target is `Chudware`; the binary
is `chudware`. `CHUDWARE_*` env vars are the only honored prefix (the legacy
`AI_CAD_*`/`AICAD_*`/`BILLET_*`/`SHOPCRAFT_*` mapping was removed). The MCP server id is
`chudware`. Data dirs are unconditionally `Chudware/`: the appdata root and the
`~/Documents/Chudware` workspace (sessions in `Sessions/`). A pre-rename `Billet`/`AI-CAD`
install is copied forward once (copy-if-missing, skipping venvs/payloads; the old dir is
never moved or deleted). Persisted wire formats are intentionally NOT renamed for data
compatibility: the PLM vault dir `.aicadvault`, mesh format `.aicm`, git refs
`refs/aicad/*`, and `aicad.<x>/<n>` contract schema ids are preserved.

## Features

- Mechanical CAD: sketches + constraint solver, extrude/revolve/sweep/loft/rib/shell/draft/
  fillet/chamfer/hole/pattern/mirror, surfacing, weldments
- Assemblies + mates; drawings; sheet metal
- CAM: milling/turning/laser-plasma-waterjet; G-code for GRBL/LinuxCNC/Fanuc/Haas/Marlin/Siemens
- ECAD/firmware: schematic, PCB, SPICE sim, Gerber output
- FEA/CFD: static/modal FEA, LBM wind tunnel, OpenFOAM, EM
- Sourcing/BOM; federated standard-parts library
- Interop: STEP/IGES/BREP/STL/3MF/OBJ/PLY/glTF/3dm + SolidWorks COM + Parasolid around the
  native `.step+` document

## Architecture snapshot

The C++ kernel **`libchudware`** ([core/](core)) **is the engine**; there is no Python CAD
engine. The C++ host ([host/](host)) is the sole GUI target; it talks to the shared
**`chudware-hostd`** daemon (which dlopens `libchudware`) over local IPC. The **same
`host/src/ui` tree compiles to WASM** for the web app
([Webservice/README.md](Webservice/README.md)); the hostd method set the WASM client relies
on is pinned by `contracts/web_parity.md` and checked by `scripts/web_parity_probe.py`. Python remains only
under [services/](services): the MCP server, the copilot's session host and CAD agent
policy, the services worker (`worker_main.py`), installer, and pure helpers with no C++
counterpart. See [ARCHITECTURE.md](ARCHITECTURE.md) for the online/offline agent runtime.

```
            ┌────────────────────────────────────────────────────┐
            │  Chudware GUI (host/, native C++ — same tree → WASM) │
 user ─────▶│  ribbon / sketcher / viewport (GL)   copilot chat  │
            └───────┬────────────────────────────────────────────┘
                    │ local IPC
                    ▼
            chudware-hostd ──dlopen──▶ libchudware (core C++ engine)
                    │ stdio  ▲
                    ▼        │
     services.worker_main    └──── external agent via MCP server
     (services-side methods,       (services/mcp, stdio JSON-RPC)
      copilot agent turns)
```

## Quickstart

```bash
./build.sh all                 # core → libchudware.so + chudware-hostd, then host GUI
./build.sh                     # host GUI only → host/build-linux/bin/chudware
./host/build-linux/bin/chudware  # run
bash scripts/gate.sh           # full Linux gate (check.py + builds + ctest + smokes)
python3 scripts/check.py       # fast source/audit gate (needs the pinned audit packages;
                               # see docs/reference/CI_RUNNERS.md)
```

- Core only: CMake presets in [CMakePresets.json](CMakePresets.json) build into
  `build/<preset>` — test with `ctest --test-dir build/<preset>/core`. The gate, GUI, and
  hostd consume `core/build-linux` / `core/build-occt-linux` instead
  (`ctest --test-dir core/build-linux`); a preset tree is not loaded by them.
- Host only: `cmake -S host -B host/build-linux -G Ninja && ninja -C host/build-linux Chudware`
  (`-DCHUDWARE_ENABLE_CEF=OFF` for an SDK-less build). Windows: see
  [docs/reference/BUILD_WINDOWS.md](docs/reference/BUILD_WINDOWS.md).
- Python suites: `~/.local/share/Chudware/mcp-venv/bin/python -m pytest <dir> -q`.
- GUI verification: headless `CHUDWARE_SHOT=<scenario>` under `xvfb-run`.
- C++17 baseline; the mesh/manufacturing layers require C++20; CUDA is an optional LBM
  backend (`CHUDWARE_WITH_CUDA`, default OFF).

## MCP / agents

The MCP server ([services/mcp/](services/mcp)) is a local stdio/JSON-RPC harness
exposing the full modeling surface; the `cad_guide` tool carries
modeling/CAM/ECAD guidance. Register it with your agent clients:

```bash
python3 services/installer/setup_mcp_clients.py           # (Windows: mcp_setup.bat)
python3 services/installer/setup_mcp_clients.py --detect  # verify
```

The built-in copilot's chat UI is `host/src/ui/copilot/`; its agent host and CAD
roles/tools live in [services/agent/](services/agent).

**If you are an AI agent working on this repo**, read [AGENTS.md](AGENTS.md) first:
orient with `graphify query "<question>"` (a hook enforces this), pick work from
`python3 work/tools/work.py ready` (policy in [work/WORKFLOW.md](work/WORKFLOW.md)), and
verify with `bash scripts/gate.sh` before review.

## Repo map

| Dir | Purpose |
|-----|---------|
| [core/](core) | C++ kernel → `libchudware` — **the engine** — plus the `chudware-hostd` daemon |
| [host/](host) | Sole GUI target (native + WASM); own GL pipeline under `host/src/render/` (its `.aicm` loader is reused by core round-trip tests) |
| [services/](services) | C++ host-side engine routing/IPC seam (`worker/`, `ipc/`); Python `worker_main.py`, `mcp/`, `agent/`, `installer/`, `ecad/`, `sim/`, `structural/`, `piping/`, `formats/`, `dag/`, `deliverables/`, `plm/`, … |
| [contracts/](contracts) | Frozen interface contracts — the seams (append-only) |
| [corpora/](corpora) | Test fixtures, referenced through canonical `corpora/...` paths |
| [work/](work) | Git-tracked solo task board (`work/tools/work.py`, `WORKFLOW.md`) |
| [docs/](docs) | Design docs, references, plans, handoff log ([docs/README.md](docs/README.md)) |
| [Webservice/](Webservice) | Web app: the `host/` WASM GUI backed by a shared hostd |
| tools/, scripts/, vendor/, assets/, packaging/, benchmarks/, training/ | Dev tools, repo gates, vendored deps, icons/fonts, packaging, perf benches, ML eval |

## Docs & process

- [AGENTS.md](AGENTS.md) — canonical agent-navigation doc (read first)
- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture
- [contracts/](contracts) — frozen interface contracts
- [docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md) — current handoff log (2026-08-14 onward);
  the consolidation refactor T-RF1..T-RF23 and earlier entries are archived in
  [docs/refactor-log/](docs/refactor-log)
- [work/](work) — task board and current follow-ups

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the rules (license guardrails, scoped write-sets,
gate policy). The product is commercial closed-source.
