# Third-Party Notices

Chudware is licensed under the MIT License (see [`LICENSE`](LICENSE)). It is built with, links
to, fetches, or can download the third-party components listed here, each under its own license.
The MIT License that covers Chudware grants no rights in these components.

This file is derived from [`contracts/licenses.md`](contracts/licenses.md), the authoritative
dependency ledger, which also sets the policy for what may be linked, bundled or invoked. Open
compliance items, including notice obligations that release packaging does not yet meet, are
tracked in [`contracts/license_debt.md`](contracts/license_debt.md).

## How components are incorporated

| Term | Meaning |
|---|---|
| vendored | Source committed in this repository. |
| fetched at build | Source downloaded by the build at a pinned commit and compiled into Chudware binaries. |
| prebuilt, fetched at build | Upstream binary archive downloaded at build time (SHA-256 pinned) and shipped beside Chudware binaries. |
| system library | Provided by the operating system or package manager. The Linux AppImage may carry it in its bundled dependency closure. |
| Python runtime package | Wheel installed into the Chudware Python runtime from `requirements.txt`; release builds compile these into the Python sidecars. |
| downloaded at runtime | Fetched from upstream only when the user requests the feature. |
| subprocess-only optional tool | Separate program that the user installs (or that a provisioning script installs into the user's own environment on request). Never distributed with Chudware. |
| model weights downloaded on demand | Fetched only on explicit user request, or supplied by the user. Never bundled. |

## License files in this repository

| Path | Covers |
|---|---|
| [`LICENSE`](LICENSE) | Chudware (MIT) |
| [`assets/fonts/OFL.txt`](assets/fonts/OFL.txt) | Rubik and JetBrains Mono (also the copies in `website/public/fonts/`) |
| [`assets/fonts/LICENSE-DejaVu.txt`](assets/fonts/LICENSE-DejaVu.txt) | `assets/fonts/CADSymbols.ttf` (DejaVu, Bitstream Vera and Arev notices) |
| [`extern/glad/LICENSE`](extern/glad/LICENSE) | glad-generated OpenGL loader and `KHR/khrplatform.h` |
| [`host/extern/mapbox/LICENSE`](host/extern/mapbox/LICENSE) | `earcut.hpp` |
| [`vendor/chudcode/CHUDCODE-NOTICE`](vendor/chudcode/CHUDCODE-NOTICE), [`vendor/chudcode/CHUDCODE-THIRD-PARTY-NOTICES.txt`](vendor/chudcode/CHUDCODE-THIRD-PARTY-NOTICES.txt) | The Chudcode agent runtime binary and everything it embeds (Bun runtime, bundled packages, fonts, native GUI and speech components) |
| `core/firmware/renode/TimedSTM32CAN.cs`, `core/firmware/templates_embedded.cpp` | Renode-derived material (MIT text inline) |
| `services/civil/data/epanet_net1.inp` | EPANET, WNTR and NumPy notices (inline) |
| [License texts for adapted code](#license-texts-for-adapted-code) (below) | NumPy, PCG64, Julia and ezdxf code adapted into first-party files |

## 1. Native components

| Component | Version | License (SPDX) | How incorporated | Upstream |
|---|---|---|---|---|
| Open CASCADE Technology (OCCT) | 7.9.3 | LGPL-2.1-only WITH OCCT-exception-1.0 | system library (Linux, macOS); prebuilt, fetched at build (Windows); dynamically linked | https://github.com/Open-Cascade-SAS/OCCT |
| FreeImage (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | FreeImage (FreeImage Public License 1.0, elected from GPL-2.0-only OR GPL-3.0-only OR FreeImage) | prebuilt, fetched at build; Windows only | https://freeimage.sourceforge.io/ |
| FFmpeg (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | LGPL-2.1-or-later (build configuration not verified; see `license_debt.md` item 24) | prebuilt, fetched at build; Windows only | https://ffmpeg.org/ |
| oneTBB (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | Apache-2.0 | prebuilt, fetched at build; Windows only | https://github.com/uxlfoundation/oneTBB |
| FreeType (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | FTL (elected from FTL OR GPL-2.0-or-later) | prebuilt, fetched at build; Windows only | https://freetype.org/ |
| jemalloc (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | BSD-2-Clause | prebuilt, fetched at build; Windows only | https://github.com/jemalloc/jemalloc |
| OpenVR (OCCT Windows third-party pack) | as shipped in OCCT 7.9.3 `3rdparty-vc14-64` | BSD-3-Clause | prebuilt, fetched at build; Windows only | https://github.com/ValveSoftware/openvr |
| Eigen | 3.4.0 | MPL-2.0 | fetched at build (header-only, unmodified) | https://gitlab.com/libeigen/eigen |
| Spectra | 1.0.1 | MPL-2.0 | fetched at build (header-only, unmodified) | https://github.com/yixuan/spectra |
| nlohmann/json | 3.11.3 | MIT | fetched at build | https://github.com/nlohmann/json |
| miniz | 3.0.2 | MIT | fetched at build | https://github.com/richgel999/miniz |
| Clipper2 | 1.5.4 | BSL-1.0 | fetched at build | https://github.com/AngusJohnson/Clipper2 |
| doctest | 2.4.11 | MIT | fetched at build; test binaries only, not shipped | https://github.com/doctest/doctest |
| Polyhedral-FEA (mesher and mesh advisor model) | commit `a9fe080daf6e15187ab9d31ce7acedb93d6d7fe7` | BSD-3-Clause | fetched at build and statically linked; advisor model files shipped beside the engine | repository URL recorded in `core/cmake/ProvisionPolymesh.cmake` |
| ONNX Runtime | 1.28.0 | MIT | prebuilt, fetched at build; Windows x64 and Linux x86-64 only | https://github.com/microsoft/onnxruntime |
| openNURBS (from rhino3dm 8.17.0) | commit `6c91d229ab561fe21c92ffdd97934c77ff945e0a` | LicenseRef-openNURBS AND Zlib | fetched at build and statically linked | https://github.com/mcneel/opennurbs |
| lib3mf | 2.5.0 | BSD-2-Clause | prebuilt, fetched at build; loaded at run time | https://github.com/3MFConsortium/lib3mf |
| SuiteSparse CHOLMOD | build-host version | LGPL-2.1-or-later AND GPL-2.0-or-later (the Supernodal module used by Chudware is GPL-2.0-or-later; see `license_debt.md` item 22) | system library, optional (`CHUDWARE_WITH_CHOLMOD`); bundled in the Linux AppImage when enabled | https://github.com/DrTimothyAldenDavis/SuiteSparse |
| Dear ImGui | v1.91.6-docking | MIT | fetched at build | https://github.com/ocornut/imgui |
| GLFW | 3.4 | Zlib | fetched at build (native builds) | https://github.com/glfw/glfw |
| stb | commit `2c980bb59875b0d32144a71867fbdebb2f77cd20` | MIT OR Unlicense | fetched at build | https://github.com/nothings/stb |
| glad (generated OpenGL 3.3 core loader) | glad 2.0.8 | (WTFPL OR CC0-1.0) AND Apache-2.0 | vendored (`extern/glad/`) | https://github.com/Dav1dde/glad |
| Khronos `KHR/khrplatform.h` | as emitted by glad 2.0.8 | Khronos MIT-style license (text in `extern/glad/LICENSE`) | vendored (`extern/glad/include/KHR/`) | https://github.com/KhronosGroup/EGL-Registry |
| earcut.hpp | not recorded in the vendored file | ISC | vendored (`host/extern/mapbox/`) | https://github.com/mapbox/earcut.hpp |
| TweetNaCl | not recorded in the vendored file | LicenseRef-PublicDomain | vendored (`core/util/tweetnacl.{c,h}`) | https://tweetnacl.cr.yp.to/ |
| Chromium Embedded Framework (CEF) | 149.0.6+g0d0eeb6 | BSD-3-Clause | prebuilt, fetched at build; native desktop builds only | https://bitbucket.org/chromiumembedded/cef |
| Chromium (inside CEF) | 149.0.7827.201 | BSD-3-Clause, plus the licenses listed in the CEF distribution's `CREDITS.html` | prebuilt, fetched at build with CEF | https://www.chromium.org/ |
| coi-serviceworker | 0.1.7 | MIT | vendored (`host/wasm/coi-serviceworker.js`) | https://github.com/gzuidhof/coi-serviceworker |
| libcurl | build-host version | curl | system library | https://curl.se/ |
| Linux runtime libraries (libstdc++, libgcc, libgomp, X11/Wayland client libraries, zlib, and the rest of the dependency closure) | build-host versions | per distribution package (libstdc++, libgcc, libgomp: GPL-3.0-or-later WITH GCC-exception-3.1) | system libraries bundled in the Linux AppImage | the build host's distribution |
| Emscripten runtime (musl libc, libc++, libc++abi, compiler-rt) | 6.0.3 in the browser container; otherwise the building developer's `emsdk` | MIT OR NCSA (Emscripten); MIT (musl); Apache-2.0 WITH LLVM-exception (LLVM runtimes) | linked into the experimental WASM web client; toolchain image `emscripten/emsdk:6.0.3` is build-only | https://emscripten.org/ |

## 2. Agent runtime (Chudcode)

The optional native online-agent runtime is MIT-licensed and runs as a separate
process. Its pinned source archive and native binary assets are hosted in this
repository's [runtime release](https://github.com/Hunter-124/Chudware/releases/tag/chudcode-runtime-v0.4.4).
The notices are hash-locked by `vendor/chudcode/CHUDCODE_ARTIFACT.lock` and must
accompany any payload that includes the runtime. The recommended browser container
ships neither this binary nor the `chudcode-rpc` SDK because online Copilot is unsupported there.

Runtime source and contribution instructions are available at
[`Hunter-124/chudcode`](https://github.com/Hunter-124/chudcode). Public source
availability does not change the mirrored artifact URLs or notice/hash locks.

| Component | Version | License (SPDX) | How incorporated | Upstream |
|---|---|---|---|---|
| Chudcode (`chud` binary) | 0.4.4, commit `8f70d216879be0cfe6bfeb5520f2370521224330` | MIT | optional native prebuilt binary, obtained from this repository's runtime release at packaging/provisioning time and SHA-256 verified; subprocess over RPC; not in the recommended browser container | repository and release URLs recorded in `vendor/chudcode/CHUDCODE_ARTIFACT.lock` (`macos-arm64` is not pinned) |
| Oh My Pi (modified by Chudcode) | as included in Chudcode 0.4.4 | MIT | inside the Chudcode binary | https://github.com/can1357/oh-my-pi |
| Bun runtime, bundled packages and fonts | as included in Chudcode 0.4.4 | see `vendor/chudcode/CHUDCODE-THIRD-PARTY-NOTICES.txt` | inside the Chudcode binary | see the notices file |
| chudcode-rpc | 0.3.2 | MIT | optional online-agent Python SDK, built from the SHA-256-pinned source archive at the locked commit in this repository's runtime release; required by native provisioning, omitted from the browser container | https://github.com/Hunter-124/Chudware/releases/tag/chudcode-runtime-v0.4.4 |

## 3. Python runtime packages (`requirements.txt`)

Versions are the pins in `requirements.txt`. The **Basis** column says where the license was
confirmed: *metadata* = read from the installed package's metadata in a development environment
(the inspected version may differ slightly from the pin); *PyPI* = the PyPI metadata for the pinned
version; *repository* = the upstream source repository; *requirements* = only stated in a
`requirements.txt` comment and not otherwise verified.

The browser container installs these requirements except the optional
`chudcode-rpc` source-archive dependency. Their presence does not imply that optional external
programs, separately installed models or browser-incompatible features are provided.

| Package | Version | License (SPDX) | Basis | Notes |
|---|---|---|---|---|
| build123d | 0.11.1 | Apache-2.0 | metadata | |
| cadquery-ocp-novtk | 7.9.3.1.1 | Apache-2.0 | metadata | Wheel bundles OCCT libraries under LGPL-2.1-only WITH OCCT-exception-1.0 (per upstream) |
| ocpsvg | 0.6.0 | Apache-2.0 | repository | |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | metadata | Binary wheels also bundle OpenBLAS and the GCC Fortran runtime under their own licenses (per upstream) |
| scipy | 1.17.1 | BSD-3-Clause | PyPI | Binary wheels bundle OpenBLAS and the GCC Fortran runtime (per upstream) |
| trimesh | 4.12.2 | MIT | metadata | |
| networkx | 3.4.2 | BSD-3-Clause | metadata | |
| rhino3dm | 8.17.0 | MIT | repository | |
| ezdxf | 1.4.4 | MIT | metadata | |
| olefile | 0.47 | BSD-2-Clause | PyPI (BSD) | |
| pywin32 (Windows only) | 312 | PSF-2.0 | PyPI | |
| pillow | 12.3.0 | MIT-CMU | metadata | |
| pyvista | 0.48.4 | MIT | metadata | |
| vtk | 9.6.2 | BSD-3-Clause | metadata | |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 | metadata | Wheels bundle FFmpeg under LGPL-2.1-or-later (per upstream) |
| pycolmap | 3.13.0 | BSD-3-Clause | PyPI | |
| mcp | 1.28.1 | MIT | metadata | |
| chudcode-rpc | 0.3.2 | MIT | requirements | SHA-256-pinned source archive; see section 2 |
| anthropic | 0.107.1 | MIT | PyPI | |
| openai | 2.41.0 | Apache-2.0 | metadata | |
| httpx | 0.28.1 | BSD-3-Clause | metadata | |
| scikit-fem | 12.0.2 | BSD-3-Clause | repository | |
| meshio | 5.3.5 | MIT | metadata | |
| pyamg | 5.3.0 | MIT | PyPI | |
| python-pptx | 1.0.2 | MIT | metadata | |
| python-docx | 1.2.0 | MIT | metadata | |
| openpyxl | 3.1.5 | MIT | metadata | |
| matplotlib | 3.11.0 | LicenseRef-Matplotlib (PSF-based) | metadata | |
| reportlab | 5.0.0 | BSD-3-Clause | PyPI (BSD) | |
| pypdfium2 | 5.11.0 | Apache-2.0 OR BSD-3-Clause, plus PDFium and its dependencies | metadata | |
| pypdf | 6.19.0 | BSD-3-Clause | PyPI | |
| office-oxide | 0.1.8 | MIT OR Apache-2.0 | PyPI | |
| fluids | 1.3.1 | MIT | metadata | |
| PyNiteFEA | 1.0.1 | MIT | repository | |
| pyproj | 3.7.2 | MIT | PyPI | Wheels bundle PROJ (MIT) and its data |
| dulwich | 1.2.12 | Apache-2.0 OR GPL-2.0-or-later | metadata | Used under the Apache-2.0 option |
| websockets | 16.1 | BSD-3-Clause | metadata | |
| anyio | 4.14.2 | MIT | metadata | |
| langgraph | 1.x | MIT | metadata | Pulls in langchain-core, langsmith (MIT, per upstream) |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT | metadata | Pulls in aiosqlite (MIT, per upstream) |
| pydantic | 2.x | MIT | metadata | |
| qrcode | 7.x or later | BSD | PyPI | `requirements.txt` comment states MIT; PyPI metadata states BSD |
| lib3mf | 2.5.x | BSD-2-Clause | PyPI (BSD) | |
| pyyaml | 6.0.3 | MIT | metadata | |
| playwright | 1.61.0 | Apache-2.0 | metadata | Its Chromium build is downloaded at runtime into Playwright's cache (`playwright install chromium`); Chromium is BSD-3-Clause plus its bundled third-party licenses |
| PyJWT | 2.15.0 | MIT | metadata | |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause | metadata | |
| urllib3 | 2.8.0 | MIT | metadata | |
| pip | 26.2.0 | MIT | metadata | |
| setuptools | 83.0.0 | MIT | PyPI | |
| Werkzeug | 3.1.9 | BSD-3-Clause | PyPI | |

Optional packages that Chudware imports only when the user has installed them (pdfminer.six,
cairosvg, CoolProp, open3d, docling, torch, transformers, huggingface_hub, pytorch3d, the vggt
and mapanything packages, and the openEMS/CSXCAD modules) are listed with their postures in
`contracts/licenses.md`; they are not installed or distributed by Chudware.

## 4. Build, packaging and deployment tooling

| Component | Version | License (SPDX) | How incorporated | Upstream |
|---|---|---|---|---|
| Nuitka | 4.1.3 | AGPL-3.0-or-later with the Nuitka Runtime Library Exception 1.0 | release-build tool (`packaging/requirements-release.txt`). The compiler is never shipped; its runtime library is linked into the compiled sidecars under the runtime exception | https://github.com/Nuitka/Nuitka |
| ordered-set | 4.1.0 | MIT | release-build tool | https://github.com/rspeer/ordered-set |
| zstandard | 0.25.0 | BSD-3-Clause | release-build tool | https://github.com/indygreg/python-zstandard |
| PyYAML, cryptography | 6.0.3, 50.0.0 | MIT; Apache-2.0 OR BSD-3-Clause | release-build tools | see section 3 |
| patchelf | 0.17.2.4 | GPL-3.0-or-later | Linux release-build tool; never shipped | https://github.com/NixOS/patchelf |
| fastapi, uvicorn[standard], websockets, httpx, python-multipart, anyio | per `web/gateway/requirements.txt` | MIT; BSD-3-Clause; BSD-3-Clause; BSD-3-Clause; Apache-2.0; MIT | Python packages of the experimental self-hosted web gateway (`web/gateway/`); not part of the desktop application | PyPI |
| astro, @astrojs/mdx | 7.3.8, 7.0.5 | MIT | website build (`website/package-lock.json`) | https://astro.build/ |
| pagefind | 1.5.2 | MIT | website build; its search UI is part of the generated site | https://pagefind.app/ |
| wrangler | 4.149.0 | MIT OR Apache-2.0 | website deployment tool | https://github.com/cloudflare/workers-sdk |
| sharp, @img/sharp-libvips | 0.35.5, 1.3.4 | Apache-2.0; LGPL-3.0-or-later | website build-time image processing; nothing from it is served | https://sharp.pixelplumbing.com/ |
| Docker Engine (Moby), Docker Compose v2 | user-installed; Compose 2.24.4+ for the HTTPS override | Apache-2.0; Docker Desktop has separate product terms | host-side tooling for the self-hosted container build/orchestration; not part of the Chudware application image | https://github.com/moby/moby ; https://github.com/docker/compose |
| Fedora container OS packages | Fedora 44; installed versions in the image RPM inventory | per-package licenses, including LGPL libraries and GPL OS tools | build/runtime environment for `web/deploy/container/Dockerfile`; package metadata and `/usr/share/licenses` retain upstream notices; image redistribution must meet the packages' own source/license obligations | https://packages.fedoraproject.org/ |
| bubblewrap | Fedora package version in the image | LGPL-2.0-or-later | separate OS executable for actor/worker isolation, installed in the browser container; never linked or imported into Chudware; user-installed for the advanced systemd path | https://github.com/containers/bubblewrap ; https://github.com/containers/bubblewrap/blob/main/COPYING |
| Caddy | 2.10.2 (`caddy:2.10.2-alpine`) | Apache-2.0; Alpine OS packages retain their own licenses | optional separate upstream TLS/authentication proxy image selected by `web/compose.https.yaml`; not part of the Chudware or desktop process | https://github.com/caddyserver/caddy ; https://github.com/caddyserver/caddy-docker |
| AppArmor parser | host-installed 4.x | GPL-2.0-only; host dependencies retain their own licenses | optional host-side subprocess loading the named container profile on Ubuntu 24.04+ and other namespace-restricting AppArmor hosts; not linked, imported or bundled in the Chudware image | https://gitlab.com/apparmor/apparmor |

## 5. Fonts and data

| Component | Version | License (SPDX) | How incorporated | Upstream |
|---|---|---|---|---|
| Rubik | as committed | OFL-1.1 | vendored (`assets/fonts/`, `website/public/fonts/`); text in `assets/fonts/OFL.txt` | https://github.com/googlefonts/rubik |
| JetBrains Mono | as committed | OFL-1.1 | vendored (`assets/fonts/`, `website/public/fonts/`); text in `assets/fonts/OFL.txt` | https://github.com/JetBrains/JetBrainsMono |
| `CADSymbols.ttf` (renamed nine-glyph subset of DejaVu Sans) | DejaVu Sans 2.35 | Bitstream-Vera; Arev Fonts license; DejaVu changes public domain | vendored (`assets/fonts/`); text in `assets/fonts/LICENSE-DejaVu.txt` | https://dejavu-fonts.github.io/ |
| EPANET Net1 example network | EPANET 2.2 | MIT | vendored test data (`services/civil/data/epanet_net1.inp`, notice inline) | https://github.com/OpenWaterAnalytics/EPANET |
| WNTR-generated reference results | WNTR 1.5 | BSD-3-Clause (notice inline, with the NumPy notice) | vendored test data (`services/civil/data/`) | https://github.com/USEPA/WNTR |
| NRCS NEH/TR-55 and FHWA HDS-5 coefficient tables, NOAA Atlas 14 sample | — | US-government public domain | vendored test data (`services/civil/data/`) | the issuing agencies |

## 6. Code adapted into first-party source files

These files are part of Chudware's source and are distributed under the MIT License together with
the notices below, which the originals require. The full texts are in
[License texts for adapted code](#license-texts-for-adapted-code).

| Origin | License (SPDX) | Adapted into |
|---|---|---|
| NumPy 2.x random: `SeedSequence`, `standard_normal` and ziggurat tables | BSD-3-Clause (NumPy); MIT (ziggurat method derived from Julia) | `core/sim/numpy_rng.cpp` |
| PCG64 generator as shipped in NumPy | MIT | `core/sim/numpy_rng.cpp` |
| ezdxf SAB tag table and DXF SAT decryption rule | MIT | `core/doc/acis_read.cpp` |
| Jeffrey Walton's SHA-NI SHA-256 (`sha256-x86.c`) | public domain | `core/util/sha256.h` |
| Renode platform descriptions and STM32 bxCAN register conventions (Antmicro) | MIT | `core/firmware/templates_embedded.cpp`, `core/firmware/renode/TimedSTM32CAN.cs` (MIT text inline in both) |

### Code under a non-MIT license

One first-party file contains code that is **not** covered by Chudware's MIT License:

| Origin | License (SPDX) | Where | Terms |
|---|---|---|---|
| `FourierPointEncoder` and `CADRecode` model classes from [filaPro/cad-recode](https://github.com/filaPro/cad-recode) (`demo.ipynb`) | CC-BY-NC-4.0 | `services/vision/cad_recode.py` (file header: `MIT AND CC-BY-NC-4.0`) | The ported classes remain under [CC-BY-NC-4.0](https://creativecommons.org/licenses/by-nc/4.0/legalcode) (attribution required, non-commercial use only); the rest of the file is MIT. They are used only by the optional `strategy="ml"` reverse-engineering backend, which also needs the CC-BY-NC-4.0 weights listed in section 8. Anyone redistributing or using that backend commercially must remove it or obtain permission from the upstream authors. Documented exception: `license_debt.md` item 19. |

## 7. Subprocess-only optional tools

Chudware drives the following separate programs through files, command-line arguments,
standard streams or local sockets; it never links or imports them into its processes.
They are user-installed unless a row explicitly notes a container OS package. The
browser image installs ngspice as a separate Fedora executable, not in a Chudware
binary. Each program retains its own license; copyleft obligations (GPL source offers,
the AGPL network clause) apply to whoever distributes or hosts that program. Licenses
below are as published upstream.

| Tool | License (SPDX) | How it is obtained |
|---|---|---|
| PrusaSlicer 2.9.6 | AGPL-3.0-or-later | user install (Flathub per-user install on Linux); vendor profiles are read from the installed copy |
| OpenMVS | AGPL-3.0 | user install, path through `OPENMVS_BIN`; never downloaded by Chudware |
| CalculiX (`ccx`) | GPL-2.0-or-later | user install, `PATH` or `CHUDWARE_CCX` |
| OpenFOAM | GPL-3.0-or-later | user install, or `tools/provision_openfoam.py --install` creates a conda-forge environment for the user |
| LibreDWG `dwg2dxf` | GPL-3.0-or-later | `PATH`, or `scripts/provision_libredwg.py` builds the SHA-256-pinned 0.14 release on the user's machine |
| ngspice | BSD-3-Clause with GPL-licensed parts | user install for desktop (`PATH` or `CHUDWARE_NGSPICE`); separate Fedora OS package in the browser image, with its package notices and source obligations |
| pandoc | GPL-2.0-or-later | user install |
| LibreOffice (headless) | MPL-2.0 | user install |
| ODA File Converter | proprietary freeware | user install |
| git | GPL-2.0-only | user install; optional accelerator for the PLM vault |
| QEMU (`qemu-system-arm`) | GPL-2.0-only | user install |
| Renode | MIT | user install |
| probe-rs | MIT OR Apache-2.0 | user install |
| esptool | GPL-2.0-or-later | user install |
| arm-none-eabi GCC | GPL-3.0-or-later WITH GCC-exception-3.1 | user install |
| ESP-IDF | Apache-2.0 | user install |
| arduino-cli | GPL-3.0-only | user install |
| avrdude | GPL-2.0-or-later | user install |
| OpenOCD | GPL-2.0-or-later | user install |
| clangd | Apache-2.0 WITH LLVM-exception | user install |
| COLMAP | BSD-3-Clause | user install, `COLMAP_BIN` |
| Meshroom / AliceVision | MPL-2.0 | user install, `MESHROOM_BIN` |
| Brush v0.3.0 | Apache-2.0 | downloaded at runtime by `scripts/provision_reveng.py` from the upstream release (SHA-256 and source commit pinned) |
| llama.cpp `llama-server` | MIT | user install or user-provided runtime folder; Chudware does not download it |
| Gmsh + GetDP, Elmer, Meep | GPL-2.0-or-later | detected only; no execution path exists yet |

The openEMS and CSXCAD Python modules (GPL-3.0-or-later) are the one optional GPL component that
Chudware imports in-process when the user has installed it; that exception is tracked as
`license_debt.md` item 9.

## 8. Model weights downloaded on demand

Model weights are never committed to this repository or bundled in Chudware artifacts. They are
downloaded only when the user requests a feature that needs them, or the user points Chudware at
weights they already have. Some of these licenses restrict use, not just redistribution; users are
bound by them when they download the weights.

| Model | License | How it is obtained | Caveat |
|---|---|---|---|
| Qwen3-4B GGUF (`Qwen/Qwen3-4B-GGUF`) | Apache-2.0 | downloaded on demand by `services/agent/model_manager.py` | none |
| Gemma 3 4B QAT GGUF (`bartowski/google_gemma-3-4b-it-qat-GGUF`), Gemma 4 E4B GGUF (`unsloth/gemma-4-E4B-it-GGUF`) | Gemma Terms of Use | downloaded on demand by `services/agent/model_manager.py` | Use-restricted, not an open-source license; redistribution passes the terms on (`license_debt.md` item 2) |
| TripoSR (`stabilityai/TripoSR`) | MIT | download descriptor in `services/vision/reveng_provision.py` | Download refused until a SHA-256 is pinned (`license_debt.md` item 13) |
| TRELLIS image-large (`microsoft/TRELLIS-image-large`) | MIT | download descriptor in `services/vision/reveng_provision.py` | As for TripoSR |
| CAD-Recode v1.5 (`filapro/cad-recode-v1.5`, base Qwen2-1.5B; upstream [filaPro/cad-recode](https://github.com/filaPro/cad-recode)) | CC-BY-NC-4.0 | downloaded on demand through `huggingface_hub`, or `CAD_RECODE_WEIGHTS` | Non-commercial use only; not covered by Chudware's MIT License; see `license_debt.md` item 19 |
| VGGT, MapAnything | not verified | user-supplied paths only (`VGGT_WEIGHTS`, `MAPANYTHING_WEIGHTS_DIR`) | License unverified; see `license_debt.md` item 12 |

Hosted model APIs (Anthropic, OpenAI, OpenRouter and others) are used with the user's own API key
under each provider's terms of service; only the client SDKs listed in section 3 are part of
Chudware.

## License texts for adapted code

### NumPy (`core/sim/numpy_rng.cpp`)

```
Copyright (c) 2005-2025, NumPy Developers.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

    * Redistributions of source code must retain the above copyright
       notice, this list of conditions and the following disclaimer.

    * Redistributions in binary form must reproduce the above
       copyright notice, this list of conditions and the following
       disclaimer in the documentation and/or other materials provided
       with the distribution.

    * Neither the name of the NumPy Developers nor the names of any
       contributors may be used to endorse or promote products derived
       from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### PCG64 (`core/sim/numpy_rng.cpp`)

```
PCG Random Number Generation for C.

Copyright 2014 Melissa O'Neill <oneill@pcg-random.org>

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### Julia ziggurat method (`core/sim/numpy_rng.cpp`, via NumPy)

```
Copyright (c) 2009-2019: Jeff Bezanson, Stefan Karpinski, Viral B. Shah,
and other contributors:

https://github.com/JuliaLang/julia/contributors

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### ezdxf (`core/doc/acis_read.cpp`)

```
MIT License

Copyright (c) 2020 Manfred Moitzi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
