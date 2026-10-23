# Chudware

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Chudware is an open-source parametric CAD application: a native C++ desktop app with a
ribbon UI, a feature tree, property panels, and a constraint-solver sketcher, built on the
**OpenCASCADE** B-rep kernel. One document model sits under mechanical design, drawings,
CAM, ECAD, and simulation. A built-in AI copilot and an **MCP server** let the same document
be driven by hand, by the copilot, or by any external agent (Claude Code, Cursor, …).

Website: <https://chudware.com>

![Part modeling with the copilot panel](website/public/images/modeling.png)

| Simulation | Manufacturing |
|---|---|
| ![Static FEA results](website/public/images/fea.png) | ![CAM toolpaths](website/public/images/cam.png) |

## Features

- **Mechanical CAD:** sketches with a constraint solver; extrude, revolve, sweep, loft, rib,
  shell, draft, fillet, chamfer, hole wizard, thread, patterns, mirror; surfacing; multi-body
  operations; weldments; sheet metal.
- **Assemblies and drawings:** mates, standard-parts library, exploded views, BOM,
  interference checks; 2D drawings.
- **CAM:** milling, turning, and laser/plasma/waterjet processes with toolpath simulation;
  post-processors for GRBL, LinuxCNC, Fanuc, Haas, Mach3, Centroid, Marlin, and Siemens
  (sample output in [docs/cam_samples/](docs/cam_samples)).
- **ECAD and firmware:** schematic capture, PCB layout, SPICE simulation, Gerber output.
- **Simulation:** static, modal, thermal, and fatigue FEA; motion; LBM wind tunnel and
  OpenFOAM CFD; electromagnetics.
- **PLM:** built-in, git-backed version history for projects.
- **AI copilot:** chat-driven modeling and review inside the app, using your own model
  provider credentials or offline local models; every agent edit goes through the same
  document operations as the GUI.
- **MCP server:** the full modeling surface exposed over stdio JSON-RPC to external agents.
- **Interop:** STEP, IGES, BREP, STL, 3MF, OBJ, PLY, glTF, and 3DM import/export around the
  native `.step+` document; SolidWorks import through the SolidWorks COM API where it is
  installed.

## Project status

Chudware is under active development and has not reached 1.0; file formats and APIs can
still change between releases. The desktop app builds on Linux, Windows, and macOS.
Prebuilt installers are published on
[GitHub Releases](https://github.com/Hunter-124/Chudware/releases), and the in-app updater
installs new releases after verifying their signed update manifest. No account is required.
On Linux, the release install script downloads and verifies the right package for your
distribution:

```bash
curl -fsSL https://github.com/Hunter-124/Chudware/releases/latest/download/install.sh | sh
```

It accepts `--version <V>` and `--format deb|rpm|appimage`. The browser build of the app is
experimental (see [Self-hosted web app](#self-hosted-web-app-experimental)).

## Building from source

Requirements:

- a C++20 toolchain (GCC or Clang on Linux, AppleClang on macOS, Visual Studio 2022 on
  Windows), CMake 3.24+, and Ninja;
- OCCT 7.9 (Linux: the distro development package, for example Fedora `opencascade-devel`;
  macOS: Homebrew `opencascade`; Windows: downloaded by CMake), plus SuiteSparse, libcurl,
  and X11/Wayland/OpenGL development headers on Linux;
- Python 3.13 (3.12 also works) and `git` on `PATH` for the Python services: the MCP
  server, the copilot's agent host, and the test suites.

On Fedora, `sudo .github/scripts/install-fedora-deps.sh` installs the same packages CI uses
(add `gate` for the extra tools `scripts/gate.sh` runs).

Other C++ dependencies (Eigen, GLFW, Dear ImGui, nlohmann/json, Clipper2, …) are fetched at
pinned versions by CMake. The embedded CEF browser is optional: fetch its SDK with
`scripts/fetch_cef_posix.sh`, or configure the host with `-DCHUDWARE_ENABLE_CEF=OFF`.

```bash
git clone https://github.com/Hunter-124/Chudware.git
cd Chudware

# Python service environment (.venv-mcp); extra arguments go to pip, e.g. `pytest`
PYTHON=python3.13 scripts/provision_venv.sh pytest

# Native build: core (libchudware + chudware-hostd), then the GUI, then the daemon
./build.sh all
```

`scripts/provision_venv.sh` creates `.venv-mcp`, installs `requirements.txt` from PyPI
(`chudcode-rpc` is built from the pinned Chudcode commit, hence `git`), and runs the runtime
dependency audit; `python3.13 -m venv .venv-mcp && .venv-mcp/bin/python -m pip install -r
requirements.txt` is the manual equivalent. `./build.sh` is a thin wrapper over CMake; its
header lists every mode (`app|core|daemon|wasm|all|stage|clean`), and bare `./build.sh`
rebuilds only the GUI.
Core-only builds can also use the CMake presets in [CMakePresets.json](CMakePresets.json)
(`cmake --preset linux-core`). Windows builds use the `windows-*` presets from a Visual
Studio 2022 developer prompt; see [docs/reference/BUILD_WINDOWS.md](docs/reference/BUILD_WINDOWS.md).
On macOS the build tree is `host/build-macos-<arch>/`.

### Running

```bash
CHUDWARE_PYTHON="$PWD/.venv-mcp/bin/python" CHUDWARE_REPO="$PWD" ./host/build-linux/bin/chudware
```

The GUI starts the shared engine daemon `chudware-hostd` on demand and attaches to it over
local IPC; see [docs/reference/HOSTD.md](docs/reference/HOSTD.md) for its lifecycle.

The copilot's online agent runtime, [Chudcode](https://github.com/Hunter-124/chudcode), is
a separate binary (`chud`) pinned by `vendor/chudcode/CHUDCODE_ARTIFACT.lock`; release
packages ship it beside the bundled Python runtime. For a source build, download and verify
the pinned release and point Chudware at it:

```bash
.venv-mcp/bin/python scripts/pin_chudcode.py ensure --target linux-x86_64 \
    --cache-dir ~/.cache/chudware/chudcode     # prints JSON with the verified path
export CHUDWARE_CHUDCODE_BIN=<path printed above>
```

Targets are `linux-x86_64`, `macos-x86_64`, and `windows-x86_64` (`macos-arm64` is not
pinned yet). Without the runtime, online copilot turns are unavailable; the offline
local-model fallback still works.

### Testing

```bash
.venv-mcp/bin/python scripts/check.py                       # fast source and audit checks
CHUDWARE_GATE_PYTHON=.venv-mcp/bin/python bash scripts/gate.sh   # full Linux gate
.venv-mcp/bin/python -m pytest services/mcp -q              # one Python suite
```

The gate runs `check.py`, the core and GUI builds, ctest, the Python suites, and headless
smoke tests. Environment-dependent stages (no OCCT, no `xvfb-run`, no emsdk) report SKIP
rather than failing. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

## MCP server

The MCP server ([services/mcp/](services/mcp)) is a local stdio JSON-RPC server that exposes
the full modeling surface; its `cad_guide` tool carries modeling, CAM, and ECAD guidance.
Register it with your agent clients (Claude Code, Cursor, VS Code, opencode, …):

```bash
python3 services/installer/setup_mcp_clients.py            # Windows: mcp_setup.bat
python3 services/installer/setup_mcp_clients.py --detect   # verify the registration
```

The server id is `chudware`. Agent edits made over MCP appear live in an open GUI because
both attach to the same `chudware-hostd` engine.

## Architecture

The C++ kernel **`libchudware`** ([core/](core)) is the engine. The C++ GUI ([host/](host))
talks to the shared **`chudware-hostd`** daemon, which loads `libchudware`, over local IPC.
Python under [services/](services) provides the MCP server, the copilot's agent host
(online turns run in the embedded [Chudcode](https://github.com/Hunter-124/chudcode)
runtime), the services worker, the installer, and helpers with no C++ counterpart.

```
            ┌──────────────────────────────────────────────────────┐
            │  Chudware GUI (host/, native C++; same tree → WASM)  │
 user ─────▶│  ribbon / sketcher / viewport (GL)    copilot chat   │
            └───────┬──────────────────────────────────────────────┘
                    │ local IPC
                    ▼
            chudware-hostd ──dlopen──▶ libchudware (core C++ engine)
                    │ stdio  ▲
                    ▼        │
     services.worker_main    └──── external agent via MCP server
     (services-side methods,       (services/mcp, stdio JSON-RPC)
      copilot agent turns)
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes the components, data flow, and RPC contracts;
[contracts/](contracts/README.md) holds the frozen interface contracts.

## Self-hosted web app (experimental)

The same `host/src/ui` tree compiles to WebAssembly. The browser build is served by a small
gateway that connects each session to a `chudware-hostd` tenant cell. It is intended to
become self-hostable; today it is experimental and not feature-complete. See
[web/README.md](web/README.md).

## Repository layout

| Path | Contents |
|------|----------|
| [core/](core) | C++ kernel → `libchudware` (geometry, document, sketch, solver, CAM, simulation, ECAD, agent), plus the `chudware-hostd` daemon |
| [host/](host) | The GUI (native and WASM) and its OpenGL renderer |
| [services/](services) | Host-side C++ routing/IPC seam (`worker/`, `ipc/`) and the Python services: MCP server, agent host, services worker, installer, domain helpers |
| [contracts/](contracts/README.md) | Frozen interface contracts between components |
| [corpora/](corpora/README.md), [fixtures/](fixtures), [samples/](samples) | Test fixtures and sample parts |
| [docs/](docs/README.md) | Design docs, references, and developer notes |
| [packaging/](packaging) | Installers and release packaging for Linux, Windows, and macOS |
| [scripts/](scripts), [tools/](tools) | Build, gate, audit, and developer tools |
| [web/](web/README.md) | Experimental self-hosted browser app (gateway and deployment) |
| [website/](website) | Source of the chudware.com promotional site |
| [assets/](assets), [vendor/](vendor), [extern/](extern) | Icons and fonts, pinned vendored artifacts, vendored third-party code |
| [benchmarks/](benchmarks) | Performance and validity benchmarks |

Some persisted identifiers keep the project's earlier `aicad` prefix for data compatibility:
the PLM vault directory `.aicadvault`, the `.aicm` mesh format, `refs/aicad/*` git refs, and
`aicad.<x>/<n>` schema ids.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, the
checks a change must pass, and the dependency license rules, and follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security issues privately as described in
[SECURITY.md](SECURITY.md).

## License

Chudware is released under the [MIT License](LICENSE). Third-party components keep their
own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the dependency
ledger in [contracts/licenses.md](contracts/licenses.md). One exception lives in the tree:
the model classes in `services/vision/cad_recode.py`, used by the optional machine-learning
reverse-engineering backend, are derived from
[filaPro/cad-recode](https://github.com/filaPro/cad-recode) and remain under CC-BY-NC-4.0
(non-commercial use only), as stated in that file's header.
