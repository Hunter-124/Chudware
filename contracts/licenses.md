# Contract: Dependency License Ledger

Chudware is released under the MIT License (root [`LICENSE`](../LICENSE)). This ledger is the
authoritative record of every third-party component that the project links, bundles, fetches at
build time, invokes as a separate program, or downloads at run time, and of the posture under which
each may be used.

- [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) at the repository root is the distribution
  notice file derived from this ledger (component, version, SPDX license, how it is incorporated,
  upstream source).
- [`license_debt.md`](license_debt.md) tracks open licensing and compliance items: places where a
  component's use does not yet fully meet the policy below, or where an obligation is not yet met.
- [`docs/reference/README.md`](../docs/reference/README.md) collects the clean-room,
  public-specification CAD format reference material that the clean-room rules below rely on.

## Policy

Every component has one of the postures below. A row marked **exception** (used outside its
posture) or **conditional** (compliant only in some build configurations) has a matching open item
in `license_debt.md`.

| posture | licenses | meaning |
|---|---|---|
| **in-process** | permissive (MIT, BSD, ISC, Zlib, BSL-1.0, Apache-2.0, PSF-style, public domain); MPL-2.0 for unmodified files | May be compiled in, statically linked, imported, vendored or bundled. Copyright and license notices are preserved and listed in `THIRD_PARTY_NOTICES.md`. |
| **dynamic** | LGPL-2.1 / LGPL-3.0 (including the OCCT exception) | Shared library only, loaded at run time and replaceable by the user; never statically linked and never modified in-tree. Any distribution that ships the library carries its license text and a pointer to the corresponding upstream source. |
| **subprocess** | GPL, AGPL, MPL, proprietary freeware, or any license above | Separate executable that the user installs, or that a Chudware provisioning script installs into the user's own environment on request. Chudware communicates with it only through files, argv, stdio or a local socket; it is never linked, imported or bundled into Chudware artifacts. |
| **user-provisioned** | any, including use-restricted model licenses | Model weights, data or binaries that are downloaded only on explicit user action or supplied by the user. Never committed and never bundled. Use restrictions are surfaced to the user and recorded in `license_debt.md`. |
| **excluded** | GPL or AGPL code in a Chudware process; non-commercial or research-only code or weights in a Chudware process or artifact; unknown provenance | Must not enter the tree, a shipped artifact, or a Chudware process. Any current exception is an open item in `license_debt.md`. |

Rules:

- Adding, upgrading or removing a dependency updates its row here and its entry in
  `THIRD_PARTY_NOTICES.md` in the same change. Exact pins live in the build or provisioning file
  cited in each row (`core/CMakeLists.txt`, `host/CMakeLists.txt`, `core/cmake/Provision*.cmake`,
  `scripts/fetch_cef_*`, `requirements.txt`, `vendor/chudcode/CHUDCODE_ARTIFACT.lock`, the
  provisioning descriptors); the row records the pinned version.
- Copyleft code is never copied, ported, or reworded into the tree. Functionality also offered by
  GPL projects (KiCad, BOLTS, FreeCAD Path, CalculiX, LibreDWG, ...) is reimplemented clean-room
  from documented behaviour and public file formats only.
- Code adapted from permissively licensed projects keeps its attribution in the adapted file and is
  listed under [Code adapted into first-party files](#code-adapted-into-first-party-files).
- Tables transcribed from standards and references record facts (dimensions, material
  properties), not copied compilations; their sources are recorded under
  [Data and reference material](#data-and-reference-material).
- Re-audit this ledger whenever a pin, `requirements.txt`, the Chudcode lock or a provisioning
  descriptor changes.

## Native libraries (C/C++)

| dependency | license | role | posture |
|---|---|---|---|
| **Open CASCADE Technology (OCCT)** 7.9.3 | LGPL-2.1-only WITH OCCT-exception-1.0 | B-rep kernel: modelling, booleans, STEP/IGES/BREP exchange, tessellation | **dynamic**. Linux and macOS use the system package found by `core/cmake/ProvisionOCCT.cmake`; Windows uses the upstream `opencascade-7.9.3-vc14-64-combined.zip` release asset (SHA-256 pinned) with the toolkit DLLs staged beside the application. Unmodified upstream (tag `V7_9_3`). The Linux AppImage carries the system `libTK*` libraries through its dependency closure. |
| OCCT Windows third-party pack (`3rdparty-vc14-64` in the same archive): FreeImage, FFmpeg, oneTBB, FreeType, jemalloc, OpenVR | FreeImage: GPL-2.0-only OR GPL-3.0-only OR FreeImage (the FreeImage Public License is elected); FFmpeg: LGPL-2.1-or-later (upstream build configuration not verified); oneTBB: Apache-2.0; FreeType: FTL OR GPL-2.0-or-later (FTL elected); jemalloc: BSD-2-Clause; OpenVR: BSD-3-Clause | DLLs the OCCT toolkits import: jemalloc and TBB back TKernel; FreeImage, FFmpeg and OpenVR are import-table dependencies of TKService reached through the data-exchange toolkits | **dynamic**, Windows only, shipped unmodified beside the OCCT DLLs (`FreeImage.dll` is part of the Windows payload). See `license_debt.md` for the FFmpeg configuration check. `scripts/build_arm_sdk.sh` builds OCCT with `USE_FREEIMAGE/FFMPEG/OPENVR=OFF`, which removes them from that target. |
| **Eigen** 3.4.0 | MPL-2.0 | dense and sparse linear algebra (header-only) | in-process. Pinned commit fetched at build by `core/CMakeLists.txt`; unmodified; no `unsupported/` modules are included. |
| **Spectra** 1.0.1 | MPL-2.0 | sparse generalized eigensolver (modal, linear buckling) | in-process. Headers fetched at build (pinned commit), unmodified. |
| **nlohmann/json** 3.11.3 | MIT | JSON in core, host and IPC | in-process. Fetched at build (pinned commit). |
| **miniz** 3.0.2 | MIT | ZIP read/write (`.step+` containers, archives) | in-process. Fetched at build and compiled as static libraries (`chudware_miniz`, `chudware_host_miniz`). |
| **Clipper2** 1.5.4 | BSL-1.0 | 2D polygon offset and boolean for the CAM toolpath engine | in-process, static (`chudware_clipper2`; engine, offset and rectclip sources only). G-code post dialects and toolpath algorithms are clean-room from public G-code references; no FreeCAD Path, libarea or opencamlib code. |
| **doctest** 2.4.11 | MIT | C++ unit-test framework | in-process, test binaries only; not shipped. |
| **Polyhedral-FEA** (commit `a9fe080daf6e15187ab9d31ce7acedb93d6d7fe7`) | BSD-3-Clause | STEP/BREP volume mesher, quadratic boundary projection, adaptive refinement, learned mesh advisor | in-process, static. Public sibling project fetched at build into `third_party/polymesh` by `core/cmake/ProvisionPolymesh.cmake` (repository URL recorded there); shares Chudware's Eigen and OCCT targets. Its Geogram-based kernel is not built (`POLYMESH_WITH_GEOGRAM=OFF`). The advisor model package (`model.onnx` plus three JSON sidecars) is staged as `polymesh-advisor` beside the engine. |
| **ONNX Runtime** 1.28.0 | MIT | inference runtime for the Polyhedral-FEA mesh advisor | **dynamic**. Prebuilt upstream release archive pinned by Polyhedral-FEA (the Linux digest is supplied by `ProvisionPolymesh.cmake`); the shared library is staged beside the engine on Windows x64 and Linux x86-64 only. |
| **openNURBS** (rhino3dm 8.17.0; rhino3dm commit `1e0c80e6b9cf7ff3c1579d287e178b21634a2a6d`, openNURBS commit `6c91d229ab561fe21c92ffdd97934c77ff945e0a`) | LicenseRef-openNURBS (permissive; Robert McNeel & Associates) AND Zlib (bundled zlib) | native Rhino `.3dm` read/write (`core/doc/formats.cpp`) | in-process, static (`opennurbsStatic`). Built from pinned source by `core/cmake/ProvisionOpenNURBS.cmake`; `CHUDWARE_WITH_OPENNURBS` defaults ON on Windows or when a provisioned tree exists. Python bindings and WASM targets are never built. |
| **lib3mf** 2.5.0 | BSD-2-Clause | native 3MF read/write | **dynamic** (loaded at run time through the dynamic C++ binding). Upstream SDK archive SHA-256 pinned in `core/cmake/ProvisionLib3mf.cmake`; the shared library ships beside the engine. |
| **SuiteSparse CHOLMOD** (system) | LGPL-2.1-or-later (Core, Check, Cholesky, Partition, Utility modules) AND GPL-2.0-or-later (Supernodal, MatrixOps, Modify modules) | supernodal sparse Cholesky for the FEA solvers (static, modal, thermal, buckling) through Eigen `CholmodSupernodalLLT` | **conditional**. Optional system library behind `CHUDWARE_WITH_CHOLMOD` (default ON when found). The supernodal path runs GPL-2.0-or-later code in-process, so a binary that links it carries GPL-2.0-or-later obligations for the combined work (Chudware's MIT code is GPL-compatible). Windows and macOS releases build without it; the Linux AppImage bundles the system library through its dependency closure; tracked in `license_debt.md`. `-DCHUDWARE_WITH_CHOLMOD=OFF` uses Eigen's built-in sparse solvers instead. |
| **Dear ImGui** v1.91.6-docking | MIT | immediate-mode GUI | in-process. Fetched at build (pinned commit). |
| **GLFW** 3.4 | Zlib | native windowing and GL context | in-process. Fetched at build (pinned commit). WASM builds use Emscripten's GLFW port instead. |
| **stb** (commit `2c980bb59875b0d32144a71867fbdebb2f77cd20`) | MIT OR Unlicense | image decode and PNG write | in-process. Fetched at build. |
| **glad** 2.0.8 generated loader (GL 3.3 core) and `KHR/khrplatform.h` | (WTFPL OR CC0-1.0) AND Apache-2.0; Khronos MIT-style notice for `khrplatform.h` | OpenGL function loader | in-process, vendored in `extern/glad/`; license texts in `extern/glad/LICENSE`. |
| **earcut.hpp** (Mapbox) | ISC | hole-aware polygon triangulation for sketch regions (`host/src/ui/sketch_mode.cpp`) | in-process, vendored as `host/extern/mapbox/earcut.hpp`; license in `host/extern/mapbox/LICENSE`. The vendored file does not record its upstream version. |
| **TweetNaCl** | LicenseRef-PublicDomain | Ed25519 signature verification (`crypto_sign_open`) for signed release-payload manifests (`core/security/integrity.cpp`) and signed update manifests (`host/src/update/updater.cpp`) | in-process, vendored as `core/util/tweetnacl.{c,h}`; compiled into `chudware_util`, the update and uninstall helpers and the WASM client. Chosen over OpenSSL or libsodium to avoid a per-platform system dependency on a path every build needs. Verification only: no signing key is part of the repository or any artifact. Randomness comes from first-party `core/util/randombytes.cpp`. |
| **Chromium Embedded Framework (CEF)** 149.0.6+g0d0eeb6 (Chromium 149.0.7827.201) | BSD-3-Clause (CEF and Chromium) plus the licenses of Chromium's bundled third-party code, listed in the distribution's `CREDITS.html` | in-application off-screen browser (`host/src/ui/widgets/cef/`) | **dynamic**. SHA-256 pinned minimal binary distribution fetched by `scripts/fetch_cef_posix.sh` / `scripts/fetch_cef_windows.ps1` into `extern/cef/` (not committed); shipped with native desktop builds (`CHUDWARE_ENABLE_CEF`, default ON), never with WASM builds. The distribution's `LICENSE.txt` and `CREDITS.html` must accompany shipped binaries; tracked in `license_debt.md`. |
| libcurl (system) | curl | HTTP client for model-provider and update requests | **dynamic**, system library. |
| Platform runtime libraries bundled in the Linux AppImage dependency closure (for example libstdc++, libgcc, libgomp, X11/Wayland client libraries, zlib) | each under its distribution license (libstdc++/libgomp: GPL-3.0-or-later WITH GCC-exception-3.1) | C/C++ runtime, OpenMP threading, windowing | **dynamic**. `packaging/linux/build_appimage.sh` copies the `ldd` closure minus the AppImage excludelist; the exact set depends on the build host. Notices are covered by the open item in `license_debt.md`. |
| CUDA Toolkit (optional) | LicenseRef-NVIDIA-CUDA-EULA | CUDA lattice-Boltzmann backend | system, opt-in `CHUDWARE_WITH_CUDA` (default OFF); the CUDA runtime is not bundled by any packaging script. |
| Emscripten toolchain runtime (6.0.3 in the browser container; otherwise the developer's `emsdk`) | Emscripten: MIT OR NCSA; musl libc: MIT; libc++/libc++abi/compiler-rt: Apache-2.0 WITH LLVM-exception (per upstream) | runtime libraries linked into the experimental WASM web client | in-process (static in the WASM output); the container toolchain stage uses `emscripten/emsdk:6.0.3`, not the runtime service image. |

## Python runtime packages (`requirements.txt`)

Runtime packages are installed from wheels (or the SDK source described below) into the
Chudware Python runtime; none is vendored in the repository. The browser container
installs the root requirements except the optional `chudcode-rpc` source-archive line,
retaining non-AI Python-backed features. Optional packages that are feature-detected
at run time are listed in the next table.

| dependency | license | role | posture |
|---|---|---|---|
| build123d | Apache-2.0 | parametric modelling API over OCCT | in-process |
| cadquery-ocp-novtk (+ cadquery-ocp-proxy) | Apache-2.0 (bindings); bundled OCCT libraries: LGPL-2.1-only WITH OCCT-exception-1.0 | Python bindings for OCCT | in-process; OCCT inside the wheel is a dynamic library (same posture as the native OCCT row) |
| ocpsvg | Apache-2.0 (upstream repository; not stated in the package metadata) | SVG import/export over OCP | in-process |
| numpy / scipy | BSD-3-Clause (numpy wheels also carry 0BSD, MIT, Zlib, CC0-1.0 components and a bundled OpenBLAS/GCC Fortran runtime under their own licenses) | numerics and solvers | in-process |
| trimesh | MIT | mesh import/export lane (STL/OBJ/PLY/glTF), voxelization, ray casts | in-process |
| networkx | BSD-3-Clause | trimesh scene-graph dependency (multi-mesh OBJ/glTF import) | in-process |
| rhino3dm | MIT | Rhino `.3dm` mesh-level import/export | in-process |
| ezdxf | MIT | DXF import to planar wires and faces (`services/formats/formats_dxf.py`) | in-process |
| olefile | BSD-2-Clause | OLE2 probe for legacy SolidWorks part previews | in-process |
| pywin32 (Windows only) | PSF-2.0 | optional SolidWorks COM automation, feature-detected | in-process |
| pillow | MIT-CMU | image ingestion and resizing | in-process |
| pyvista / vtk | MIT / BSD-3-Clause | headless contact-sheet rendering | in-process |
| opencv-python-headless | Apache-2.0 (wheels bundle FFmpeg under LGPL-2.1-or-later, per upstream) | ChArUco/ArUco fiducial detection and pixel-to-millimetre homography; OpenCV core only, no contrib modules | in-process; optional at run time (vision tools degrade when absent) |
| pycolmap (Linux x86-64, Windows, macOS arm64) | BSD-3-Clause | sparse structure-from-motion for reverse engineering | in-process; also force-included in the compiled sidecars by `packaging/requirements-release.txt` |
| mcp | MIT | MCP server and tool schemas | in-process |
| chudcode-rpc 0.3.1 | MIT | typed RPC client for the optional native online-agent runtime | in-process; built from the SHA-256-pinned source archive at commit `2eaba2f4606984172f31f108459d9529d4ee5e38`, hosted in https://github.com/Hunter-124/Chudware/releases/tag/chudcode-runtime-v0.4.3; not published on PyPI. No private companion repository access is required. The browser container omits this optional SDK because online Copilot is unsupported there. |
| anthropic / openai / httpx | MIT / Apache-2.0 / BSD-3-Clause | model-provider SDKs and HTTP | in-process |
| scikit-fem / meshio / pyamg | BSD-3-Clause / MIT / MIT | Python-side FEA assembly, mesh I/O, AMG preconditioner | in-process |
| python-pptx / python-docx / openpyxl | MIT | `.pptx` / `.docx` / `.xlsx` deliverables and edits | in-process |
| matplotlib | LicenseRef-Matplotlib (PSF-based) | charts, colormaps, preview rasterization | in-process |
| reportlab | BSD-3-Clause | PDF reports | in-process |
| pypdfium2 | Apache-2.0 OR BSD-3-Clause (bundled PDFium: BSD-3-Clause and its dependencies) | PDF page rasterization for previews | in-process |
| pypdf | BSD-3-Clause | PDF metadata and page operations (`services/mcp/office_tools.py`) | in-process |
| office-oxide 0.1.8 | MIT OR Apache-2.0 | fast text extraction for Office formats; falls back to the libraries above | in-process |
| fluids | MIT | ASME B36.10M/B36.19M pipe schedule tables (`services/piping/catalog.py`); optional at run time | in-process. Fitting geometry is first-party CSV of public manufacturer dimensions; no ASME standard text is shipped. |
| PyNiteFEA | MIT | structural member forces and deflections | in-process |
| pyproj (bundles PROJ) | MIT / MIT | CRS transforms and grid/combined scale factors (`services/civil/crs.py`) | in-process. EPSG identifiers are used per the EPSG terms; no dataset is redistributed beyond the PROJ wheel. |
| dulwich | Apache-2.0 OR GPL-2.0-or-later (Apache-2.0 elected) | pure-Python git object store behind the PLM version vault (`services/plm/gitbind.py`) | in-process under the Apache-2.0 option. pygit2 was declined (GPL linking exception plus per-platform native wheels). |
| websockets | BSD-3-Clause | self-hosted PLM live relay and client (`services/plm/server.py`, `services/plm/live_client.py`) | in-process |
| anyio / pydantic | MIT / MIT | async transport and tool-argument schemas for the MCP server | in-process |
| langgraph / langgraph-checkpoint-sqlite (with transitive langchain-core, langsmith, aiosqlite) | MIT | DAG orchestration and SQLite checkpoints (`services/dag/`) | in-process; langchain-core and langsmith arrive as langgraph dependencies and are not configured by Chudware |
| qrcode | BSD (upstream package metadata) | QR code for the phone-capture hand-off URL | in-process |
| lib3mf (Python) | BSD-2-Clause | 3MF mesh I/O on the Python side | in-process |
| pyyaml | MIT | YAML configuration and catalog loading | in-process |
| playwright | Apache-2.0 | persistent-profile browser automation for user-authenticated fetches (`services/browser/session.py`) | in-process, lazily imported. The Chromium build it drives is a separate `playwright install chromium` download into Playwright's own cache; it is not vendored or bundled. What is fetched through it is tracked in `license_debt.md`. |
| PyJWT / cryptography / urllib3 | MIT / Apache-2.0 OR BSD-3-Clause / MIT | token verification, signatures, HTTP transport | in-process |
| pip / setuptools / Werkzeug | MIT / MIT / BSD-3-Clause | installer components and a developer dependency present in the runtime | in-process; Werkzeug is not used as a product server |

### Optional Python packages (feature-detected, not in `requirements.txt`)

| dependency | license | role | posture |
|---|---|---|---|
| pdfminer.six | MIT | datasheet PDF text extraction (`services/research/datasheet_fetch.py`) | in-process when the user installs it; absence returns `no_pdf_tool` |
| cairosvg | LGPL-3.0-or-later | best-effort SVG preview (`services/deliverables/engines.py`) | **dynamic**: an unmodified, separately installed package imported only if the user installed it; never bundled or required |
| CoolProp | MIT | full-range thermophysical properties for the piping solver | in-process when installed; otherwise a cited built-in model is used |
| open3d | MIT | mesh cleanup after photogrammetry | in-process when installed |
| docling | MIT | datasheet table and layout extraction for reverse engineering | in-process when installed (descriptor in `services/vision/reveng_provision.py`) |
| torch / transformers / huggingface_hub / pytorch3d | BSD-3-Clause / Apache-2.0 / Apache-2.0 / BSD-3-Clause (per upstream) | runtimes for the optional ML reverse-engineering backends (`services/vision/cad_recode.py`, `services/vision/ffwd_sfm.py`) | in-process when the user installs them; never bundled |
| vggt / mapanything Python packages | not verified | optional feed-forward structure-from-motion backends (`services/vision/ffwd_sfm.py`) | user-provisioned; see `license_debt.md` |
| openEMS / CSXCAD Python modules | GPL-3.0-or-later (per upstream) | full-wave EM solver backend (`services/sim/em/`) | **exception**: imported in the simulation worker process when the user has installed them; never bundled. Tracked in `license_debt.md`. |

## Build, packaging and web-deployment tooling

| dependency | license | role | posture |
|---|---|---|---|
| Nuitka | AGPL-3.0-or-later with the Nuitka Runtime Library Exception 1.0 (per upstream) | compiles the Python sidecars for releases (`packaging/requirements-release.txt`) | build tool. The compiler runs only on build machines and is never shipped. Its runtime library is linked into the compiled sidecars under the runtime exception, which lets the output be distributed under Chudware's own terms. The sidecars also contain the runtime packages listed above. |
| ordered-set | MIT | Nuitka dependency | build tool |
| zstandard / PyYAML / cryptography | BSD-3-Clause / MIT / Apache-2.0 OR BSD-3-Clause | release-build helpers and signing | build tool |
| patchelf | GPL-3.0-or-later | rewrites ELF run paths during Linux release builds | build tool only; never shipped |
| fastapi / uvicorn[standard] / websockets / httpx / python-multipart / anyio | MIT / BSD-3-Clause / BSD-3-Clause / BSD-3-Clause / Apache-2.0 / MIT | experimental self-hosted web gateway (`web/gateway/requirements.txt`) | in-process in the gateway service; not part of the desktop application |
| astro / @astrojs/mdx / pagefind / wrangler | MIT / MIT / MIT / MIT OR Apache-2.0 | static build and search index for the promotional website (`website/package.json`) | build tools; the generated site includes Pagefind's search UI (MIT) |
| sharp (npm override) with prebuilt libvips | Apache-2.0; libvips binaries LGPL-3.0-or-later | image processing during the website build | build tool only; nothing from it is served |
| `coi-serviceworker.js` 0.1.7 | MIT | sets COOP/COEP headers client-side so the WASM web client can use `SharedArrayBuffer` | in-process, vendored as `host/wasm/coi-serviceworker.js` (upstream `gzuidhof/coi-serviceworker`) |
| Docker Engine / Docker Compose v2 (Compose 2.24.4+ for the HTTPS override) | Apache-2.0 (Moby/Compose; Docker Desktop has separate product terms) | host-side build and orchestration for `web/compose.yaml` | user-installed tooling, not linked into Chudware or included in its application image. A compatible Podman/Compose provider is an alternative under its own license. |
| Fedora 44 container packages | per-package licenses (including LGPL libraries and GPL OS tools) | native build stage and Linux runtime environment in `web/deploy/container/Dockerfile` | operating-system packages, not relicensed by Chudware's MIT license. Preserve their package license files and metadata; redistribution/source obligations apply to each package. Exact installed versions are the image's RPM inventory, not a fixed whole-distribution license. |
| bubblewrap (Fedora package) | LGPL-2.0-or-later | isolated actor/worker subprocesses in the browser container and advanced systemd deployment | separate OS executable, never linked or imported into Chudware. Installed in the container runtime; user-installed for systemd. Preserve the package's license and corresponding-source information when redistributing the image. Upstream: https://github.com/containers/bubblewrap. |
| Caddy 2.10.2 (`caddy:2.10.2-alpine`) | Apache-2.0 (Caddy); image OS packages retain their own licenses | optional TLS and HTTP Basic authentication proxy in `web/compose.https.yaml` | separate upstream container image, not part of the Chudware process or desktop payload. Preserve upstream image/package notices when redistributing. Upstream: https://github.com/caddyserver/caddy. |

## Agent runtime

| dependency | license | role | posture |
|---|---|---|---|
| **Chudcode** 0.4.3 (commit `2eaba2f4606984172f31f108459d9529d4ee5e38`; engine 18.4.4; RPC protocol 2) | MIT (Chudcode modifies Oh My Pi, also MIT) | optional native online-agent runtime: providers, sessions, memory, compaction, browser/computer use, speech-to-text, host-tool orchestration | **subprocess** over RPC. Source and release artifacts are currently private; authorized native packaging/provisioning verifies available binaries against `vendor/chudcode/CHUDCODE_ARTIFACT.lock` (`macos-arm64` is not pinned). Chudware never imports or vendors the engine source. The binary embeds Bun and bundled packages/fonts, covered by the hash-locked `vendor/chudcode/CHUDCODE-NOTICE` and `vendor/chudcode/CHUDCODE-THIRD-PARTY-NOTICES.txt`; include them wherever that binary is shipped. The recommended browser container includes neither this runtime nor its SDK and does not provide online Copilot. |

## External tools (subprocess only)

These tools run as separate programs, never linked or imported into Chudware. Unless a
row says otherwise, they are user-installed, discovered on `PATH` or through an
environment variable, or provisioned into the user's environment on request; they
are not shipped in desktop artifacts. The browser container additionally installs
ngspice as a separate Fedora OS package, retaining its package notices and source
obligations. Absence of an optional tool degrades its feature with an actionable message.
The licenses below are those published by each upstream project.

| tool | license | role | posture |
|---|---|---|---|
| PrusaSlicer 2.9.6 (stock) | AGPL-3.0-or-later | FFF slicing for `slice_part` (`services/manufacturing/slicer/prusaslicer.py`); vendor profiles are read from the installed slicer, never copied into the repository | subprocess; user-installed (Flathub per-user install on Linux) |
| CalculiX (`ccx`) | GPL-2.0-or-later | thermal and contact FEA tools (`services/mcp/fea_advanced.py`) and the V&V cross-check (`scripts/vv_benchmark.py`) | subprocess; `PATH` or `CHUDWARE_CCX`. Input decks are generated from Chudware geometry in the public Abaqus/CalculiX syntax; no CalculiX decks or templates are copied. Linear buckling uses Chudware's in-process Spectra solver, not CalculiX. |
| OpenFOAM | GPL-3.0-or-later | accurate CFD and conjugate heat transfer backend (`services/sim/foam_runner.py`) | subprocess; user install, or `tools/provision_openfoam.py --install` creates a conda-forge environment for the user. Case files are plain OpenFOAM input dictionaries. |
| ngspice | BSD-3-Clause with GPL-licensed parts (per upstream) | optional circuit-simulation backend for vendor `.SUBCKT` models (`services/installer/ngspice_backend.py`) | separate executable; `PATH` or `CHUDWARE_NGSPICE`. User-installed for desktop; installed as a Fedora OS package in the browser container, with package license/source obligations preserved. The native MNA solver (`core/sim/spice.cpp`) is the default. |
| LibreDWG `dwg2dxf` 0.14 | GPL-3.0-or-later | DWG read and independent re-read of written DWG files | subprocess; `PATH`, else the per-user install that `scripts/provision_libredwg.py` builds from the SHA-256-pinned upstream release. DWG writing is Chudware's own encoder (`core/doc/dwg_write.cpp`). |
| ODA File Converter | proprietary freeware | DWG to DXF conversion for `services/formats/formats_dwg.py` via `ezdxf.addons.odafc` | subprocess; user-installed |
| pandoc | GPL-2.0-or-later | markup conversion for deliverables | subprocess; `PATH` |
| LibreOffice (headless) | MPL-2.0 | Office-to-PDF previews | subprocess; `PATH` |
| git | GPL-2.0-only | optional `gc`/`repack` accelerator for the PLM vault | subprocess; `PATH`; absence falls back to pure dulwich |
| Renode | MIT | firmware emulation | subprocess; `PATH`. The bundled platform descriptions are adapted from Renode (see [Code adapted into first-party files](#code-adapted-into-first-party-files)). |
| QEMU (`qemu-system-arm`) | GPL-2.0-only | firmware emulation fallback | subprocess; `PATH` |
| probe-rs | MIT OR Apache-2.0 | flash and debug | subprocess; `PATH` |
| esptool | GPL-2.0-or-later | ESP32 flashing | subprocess; `PATH` |
| arm-none-eabi GCC, ESP-IDF, arduino-cli, avrdude, OpenOCD | GPL-3.0-or-later WITH GCC-exception-3.1; Apache-2.0; GPL-3.0-only; GPL-2.0-or-later; GPL-2.0-or-later (per upstream) | firmware build, flash and debug toolchains (`core/firmware/provision.cpp`) | subprocess; user-installed |
| clangd | Apache-2.0 WITH LLVM-exception | language server for the firmware code panel | subprocess (JSON-RPC over stdio) |
| COLMAP | BSD-3-Clause | photogrammetry alternative (`COLMAP_BIN`) | subprocess; user-installed |
| Meshroom / AliceVision | MPL-2.0 | photogrammetry (`MESHROOM_BIN`) | subprocess; user-installed |
| OpenMVS | AGPL-3.0 | dense reconstruction (`OPENMVS_BIN`) | subprocess, discovery only: no download URL is recorded and nothing is fetched. Tracked in `license_debt.md`. |
| Brush v0.3.0 | Apache-2.0 | Gaussian-splat trainer for reverse engineering | subprocess; downloaded on request by `scripts/provision_reveng.py` from the upstream release (SHA-256 and source commit pinned in `services/vision/reveng_provision.py`) |
| llama.cpp `llama-server` | MIT | local OpenAI-compatible model server | subprocess or user-run server; `services/installer/llamacpp.py` never downloads binaries |
| LM Studio, Ollama, vLLM and other OpenAI-compatible servers | each under its own terms | user-run local model servers | external server reached over HTTP; no code incorporated |
| Gmsh + GetDP, Elmer, Meep | GPL-2.0-or-later | EM solver adapters (`services/sim/em/adapters.py`) | detection only; any execution path must follow the reviewed linkage mode recorded in `license_debt.md` |

## Model weights and ML data

| item | license | role | posture |
|---|---|---|---|
| Qwen3-4B GGUF (`Qwen/Qwen3-4B-GGUF`) | Apache-2.0 | default local model for fan-out roles (`services/agent/model_manager.py`) | user-provisioned: downloaded on request, never bundled |
| Gemma 3 4B QAT GGUF, Gemma 4 E4B GGUF | Gemma Terms of Use | local grammar-constrained worker model and comparison model | user-provisioned; tracked in `license_debt.md` |
| TripoSR, TRELLIS image-large | MIT | single-photo mesh reconstruction (`services/vision/reveng_provision.py`) | user-provisioned; tracked in `license_debt.md` until their SHA-256 values are pinned |
| CAD-Recode v1.5 (`filapro/cad-recode-v1.5`, base Qwen2-1.5B) | CC-BY-NC-4.0 (weights and upstream code) | ML parametric rebuild backend (`services/vision/cad_recode.py`) | **accepted exception** to the excluded posture: the ported model classes keep their CC-BY-NC-4.0 license (SPDX header in the file, listed in `THIRD_PARTY_NOTICES.md`); `license_debt.md` item 19 |
| VGGT, MapAnything weights | not verified | feed-forward multi-view geometry | user-supplied paths only (`VGGT_WEIGHTS`, `MAPANYTHING_WEIGHTS_DIR`); tracked in `license_debt.md` |
| Polyhedral-FEA mesh advisor (`model.onnx` and sidecars) | BSD-3-Clause | learned mesh-size advice | in-process data, shipped beside the engine (see the Polyhedral-FEA row) |
| Hosted model APIs (Anthropic, OpenAI, OpenRouter, and others) | provider terms of service | cloud inference with the user's own API key | external service; only the SDKs listed above are incorporated |

## Data and reference material

| item | license | role | posture |
|---|---|---|---|
| Rubik, JetBrains Mono (`assets/fonts/`) | OFL-1.1 | UI fonts | bundled; text in `assets/fonts/OFL.txt` |
| `assets/fonts/CADSymbols.ttf` (renamed subset of DejaVu Sans 2.35) | Bitstream Vera Fonts license AND Arev Fonts license; DejaVu changes public domain | CAD and GD&T symbol glyphs | bundled; text in `assets/fonts/LICENSE-DejaVu.txt` |
| civil V&V fixtures (`services/civil/data/`) | US-government public domain / MIT (EPANET) / BSD-3-Clause (WNTR, NumPy notices) | EPANET Net1 `.inp`, reference results from WNTR 1.5 and EPA SWMM 5.2 via pyswmm, NRCS NEH/TR-55 and FHWA HDS-5 coefficient tables, NOAA Atlas 14 sample | test data; notices retained in `epanet_net1.inp`. WNTR, pyswmm and IfcOpenShell (LGPL-3.0) were used outside the tree to generate or check fixtures and are not dependencies. |
| AISC section table (`services/structural/data/aisc_shapes/aisc_shapes_curated.json`) | factual data | W/S/C section properties for demand/capacity checks | Curated table of about 18 common shapes. Property values (A, Ix, Sx, Zx, ...) are physical facts restated from published engineering references (Salmon and Johnson, *Steel Structures*, 5th ed.; CISC Handbook), not a copy of the AISC Shapes Database compilation. Other shapes require a user-supplied AISC spreadsheet at run time; none is bundled. |
| Lumber sizes (`services/structural/data/lumber_table.json`) | factual data | dressed lumber dimensions and section properties | PS 20 dressed sizes and NDS Supplement Table 4A design values are standard facts; no NDS text is copied. |
| ISO/DIN part tables (`services/parts/data/bolts/*.blt.yaml`) | factual data | deterministic fastener and bearing lookup | Nominal dimensions from published standards, transcribed from first-party sources (`core/parts/parts_data.h`, the hardware cheat sheet); not from BOLTS. |
| IPC-7351 footprint geometry | factual data | starter footprints | generated by first-party code from the published standard's dimensions |

## Code adapted into first-party files

| source | license | where | posture |
|---|---|---|---|
| NumPy 2.x random (`SeedSequence`, PCG64, `standard_normal` with ziggurat tables derived from Julia) | BSD-3-Clause (NumPy); MIT (PCG64, Melissa O'Neill); MIT (Julia ziggurat) | `core/sim/numpy_rng.cpp` (bit-exact `default_rng(seed).standard_normal()`) | in-process; attribution in the file header, license texts in `THIRD_PARTY_NOTICES.md` |
| ezdxf SAB tag table and DXF SAT decryption rule | MIT | `core/doc/acis_read.cpp` | in-process; attribution in the file header, license text in `THIRD_PARTY_NOTICES.md` |
| Jeffrey Walton's SHA-NI SHA-256 (`sha256-x86.c`) | public domain | `core/util/sha256.h` | in-process; attribution in the file |
| Renode platform descriptions and STM32 bxCAN register conventions (Antmicro) | MIT | `core/firmware/templates_embedded.cpp` (emitted with its own `LICENSE`), `core/firmware/renode/TimedSTM32CAN.cs` | in-process; full MIT text kept in both files |

## ECAD module posture

| item | license | role | posture |
|---|---|---|---|
| **KiCad source** | GPL-3.0-or-later (application); GPL-2.0-or-later (`libs/kimath`) | reference only | **excluded**: never copied, linked, or ported. Concepts are reimplemented clean-room. `core/docs/kicad_reference.md` lists which KiCad third-party components are permissive. |
| KiCad file formats (`.kicad_sch`, `.kicad_pcb`, `.kicad_sym`, `.kicad_mod`, S-expressions) | n/a (format) | import/export | Parsing a documented format is interoperability; `services/ecad/library/sexpr.py` is clean-room. |
| KiCad official libraries (symbols, footprints, 3D models) | CC-BY-SA-4.0 with the KiCad design-output exception | user-supplied content | Not vendored. Users point Chudware at their own installed libraries; the exception lets designs use them without inheriting the license. Attribution is preserved on import. |
| Native circuit simulator and EDA engine (`core/sim/spice.cpp`, `core/ecad/`) | MIT (first-party) | EDA engine and MNA simulator | original code |

The ECAD module follows the same policy as the rest of the project: no GPL code in a Chudware
process.

## Excluded components

| item | license | reason |
|---|---|---|
| PyMuPDF / pymupdf4llm | AGPL-3.0 | AGPL in-process; `pdfminer.six` is used instead |
| BOLTS (boltsparts) data and code | GPL-3.0 | GPL; Chudware reads the documented `.blt` format, and a user may point the loader at their own BOLTS checkout |
| python-solvespace | GPL-3.0 | GPL in-process; the first-party sketch solver is used |
| Qwen2.5-VL-3B, Qwen2.5-3B, Moondream 3 | non-commercial or BUSL-1.1 model licenses | non-commercial or source-available terms |
