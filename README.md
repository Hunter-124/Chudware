# Chudware (formerly Billet / AI-CAD)

Chudware is a **SolidWorks-class parametric CAD application**: a native C++ desktop app
(ribbon UI, FeatureManager tree, PropertyManager, constraint-solver sketcher) on the
**OpenCASCADE** B-rep kernel, with a built-in AI copilot and an **MCP server** so the same
document can be driven by hand or by any external agent (Claude Code, Cursor, …). One
document model sits under every domain — and the same UI tree compiles to WASM for the web
app, so desktop and web stay at parity by construction.

Naming: the product is **Chudware** (chudware.com). The GUI target is `Chudware`; the binary
is `chudware`. `CHUDWARE_*` env vars are canonical; legacy `AI_CAD_*`/`AICAD_*`/`BILLET_*`/
`SHOPCRAFT_*` are still honored (mapped to `CHUDWARE_*` at startup). The MCP server id is
`chudware`. Persisted on-disk / wire formats are intentionally NOT renamed for data
compatibility: the appdata dir reads an existing `Billet`/`AI-CAD` install wholesale (fresh
installs use `Chudware/`, no migration); the PLM vault dir `.aicadvault`, mesh format
`.aicm`, git refs `refs/aicad/*`, and `aicad.<x>/<n>` contract schema ids are preserved.

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

The pure C++/CUDA kernel **`libchudware`** ([core/](core)) **is the engine** — the legacy
Python engine was fully strangled out and deleted in the zero-Python cutover (T-RF21,
2026-07-23). The C++ host ([host/](host), ~110k LOC under `host/src/ui/`) is the sole GUI
target; it talks to the shared **`chudware-hostd`** daemon (which dlopens `libchudware`) over a
unix socket. The **same `host/src/ui` tree compiles to WASM** for the web app
([Webservice/README.md](Webservice/README.md)) — desktop/web parity by construction,
enforced by `contracts/web_parity.md` + `scripts/web_parity_probe.py`. Python remains only
under [services/](services): the MCP server, the built-in agent loop, the stdio worker seam
(`worker_main.py`), installer, and pure helpers with no C++ counterpart.

```
            ┌────────────────────────────────────────────────────┐
            │  Chudware GUI (host/, native C++ — same tree → WASM) │
 user ─────▶│  ribbon / sketcher / viewport (GL)   copilot chat  │
            └───────┬───────────────────────────┬────────────────┘
                    │ unix socket               │ stdio
                    ▼                           ▼
            chudware-hostd ──dlopen──▶ libchudware   services.worker_main
            (daemon, core C++/CUDA engine)     (services-side methods)
                    ▲
 external agent ────┘ MCP server (services/mcp, stdio JSON-RPC)
```

## Quickstart

```bash
./build.sh                     # core → libchudware.so, then host GUI → host/build-linux/bin/Chudware
./host/build-linux/bin/Chudware  # run
bash scripts/gate.sh           # full Linux gate (check.py + builds + ctest + smokes)
python3 scripts/check.py       # fast stdlib-only gate
```

- Core only: presets `linux-core` / `windows-core` / `windows-occt` in
  [CMakePresets.json](CMakePresets.json); test with `ctest --test-dir core/build-linux`.
- Host only: `cmake -S host -B host/build-linux -G Ninja && ninja -C host/build-linux Chudware`
  (`-DCHUDWARE_ENABLE_CEF=OFF` for an SDK-less build). Windows: see docs/reference/BUILD_WINDOWS.md.
- Python suites: `~/.local/share/Chudware/mcp-venv/bin/python -m pytest <dir> -q`.
- GUI verification: headless `CHUDWARE_SHOT=<scenario>` under `xvfb-run`.
- C++17 throughout.

## MCP / agents

The MCP server ([services/mcp/](services/mcp), root `mcp_server` symlink) is a local
stdio/JSON-RPC harness exposing the full modeling surface; the `cad_guide` tool carries
modeling/CAM/ECAD guidance. Register it with your agent clients:

```bash
python3 services/installer/setup_mcp_clients.py           # (Windows: mcp_setup.bat)
python3 services/installer/setup_mcp_clients.py --detect  # verify
```

The built-in copilot lives in [services/agent/](services/agent) (root `agent` symlink) with
its chat UI in `host/src/ui/copilot/`.

**If you are an AI agent working on this repo**, read [AGENTS.md](AGENTS.md) first:
orient with `graphify query "<question>"` (a hook enforces this), pick work from
`python3 work/tools/work.py ready` (policy in [work/WORKFLOW.md](work/WORKFLOW.md)), and
verify with `bash scripts/gate.sh` before review.

## Repo map

| Dir | Purpose |
|-----|---------|
| [core/](core) | Pure C++/CUDA kernel → `libchudware` — **the engine** (zero Python) |
| [host/](host) | Sole GUI target (native + WASM); own GL pipeline under `host/src/render/` |
| [services/](services) | Python services: `mcp/`, `agent/`, `worker/` + `worker_main.py`, `installer/`, `ecad/`, `sim/`, `structural/`, `piping/`, `formats/`, `dag/`, `deliverables/`, `plm/`, … |
| [ui/viewport/](ui/viewport) | AICM mesh-format loader consumed by `core/` |
| [platform/](platform) | Cross-OS shims (`_linux`/`_win32`) |
| [contracts/](contracts) | Frozen interface contracts — the seams (append-only) |
| [corpora/](corpora) | Test fixtures (root `corpus*` symlinks preserved) |
| [work/](work) | Git-tracked solo task board (`tools/work.py`, `WORKFLOW.md`) |
| [docs/](docs) | Design docs, status notes, handoffs |
| tools/, scripts/, third_party/, assets/, packaging/, benchmarks/, training/ | Dev tools, repo gates, vendored deps, icons/fonts, packaging, perf benches, ML eval |

## Docs & process

- [AGENTS.md](AGENTS.md) — canonical agent-navigation doc (read first)
- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture
- [contracts/](contracts) — frozen interface contracts
- [docs/REFACTOR_LOG.md](docs/REFACTOR_LOG.md) — refactor handoff log (incl. the
  consolidation DAG T-RF1..T-RF23 and the zero-Python cutover)
- [work/](work) — task board and current follow-ups

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the rules (license guardrails, scoped write-sets,
gate policy). The product is commercial closed-source.
