# Contract: Dependency License Ledger

> See also: [docs/reference/README.md](../docs/reference/README.md) — clean-room, public-spec CAD
> format reference material (folded in from a sibling clean-room project) that documents the
> general format landscape and the clean-room-reimplementation posture this ledger enforces for
> specific dependencies (KiCad, BOLTS, etc. below).
>
> Policy: the default distribution stays GPL-clean except where isolated below. Re-audit at every
> wheelhouse rebuild (M10 gate). Today's posture: personal/local tool; revisit before any
> commercial distribution.
>
> **Owner sign-off (2026-06-13):** clean-room / GPL-clean posture CONFIRMED. KiCad source is never
> copied or reworded-then-disguised — features are reimplemented clean-room from documented behavior
> and file formats only. ngspice is permitted as an optional, unmodified, subprocess-only backend
> (mere aggregation — does not impose GPL on our code); the native MNA simulator remains primary.
>
> ⚠️ **AUDIT CORRECTION (2026-06-26):** two issues surfaced when scoping commercial distribution.
> 1. ✅ **RESOLVED (2026-06-26): gmsh removed.** The C++ FEA tet mesher (`core/sim/mesher.cpp`)
>    was reimplemented on **Netgen / nglib (LGPL-2.1)** — see the Netgen row below. `libchudware.dll`
>    no longer imports gmsh (verified: `dumpbin /dependents libchudware.dll` shows `nglib.dll`, no
>    `gmsh*.dll`); `ProvisionGmsh.cmake` and the `gmsh-4.15.dll` / gmsh wheel staging are gone.
>    Netgen is linked DYNAMICALLY (nglib.dll + ngcore.dll shipped, written source offer) which is
>    LGPL-clean. This removes the #1 commercialization blocker.
> 2. **STILL OPEN: `core/docs/kicad_reference.md` documents a "borrowing KiCad geometry is on the
>    table" stance** for internal use, which contradicts the clean-room sign-off above. Before
>    selling: audit `core/` for any KiCad `libs/kimath` (GPL) derived geometry that may have been
>    copied while the project was internal-only. Boost/MIT thirdparty (clipper2/delaunator/
>    tinyspline) listed there is vendorable; GPL kimath is not.
>
> Blocker #1 is cleared; the default distribution remains **NOT** sellable until #2 is audited.

| dependency | license | role | posture |
|---|---|---|---|
| build123d | Apache-2.0 | kernel | ok |
| cadquery-ocp / OCCT | LGPL-2.1 (OCCT excepted) | B-rep kernel | ok (dynamic) |
| pyvista / VTK | MIT / BSD-3 | contact-sheet renders | ok |
| trimesh | MIT | voxelize, ray casts, mesh import/export lane (STL/OBJ/PLY/glTF) | ok |
| networkx | BSD-3 | trimesh scene-graph dep (multi-mesh OBJ/glTF import) | ok |
| rhino3dm | MIT | Rhino .3dm import/export (mesh-level) | ok |
| ezdxf | MIT | DXF (2D vector) import → planar B-rep wires/faces (engine.doc.formats_dxf) | ok (in-process; MIT permissive) |
| ODA File Converter (optional) | proprietary (free EULA) | DWG→DXF conversion for `engine.doc.formats_dwg` (via `ezdxf.addons.odafc`) | **subprocess-only**, user-installed, PATH/registry-discovered, never bundled (mere aggregation) — absence degrades to an actionable error |
| LibreDWG `dwg2dxf` (optional) | GPL-3.0 | DWG read (`dwg2dxf` DWG→DXF of a DWG's 2D entities for the native importer and `services.formats.formats_dwg`; also re-reads a just-written DWG as an independent check when present). DWG **writing** is Chudware's own R2000 encoder (`core/doc/dwg_write.cpp`) and uses no LibreDWG code or tool | **subprocess-only**; found on PATH, else in the per-user managed install (`<appdata>/Chudware/tools/libredwg`) that `scripts/provision_libredwg.py` builds on the user's machine from the SHA-256-pinned upstream release (0.14). Never linked, bundled or redistributed by Chudware (GPL isolation, same policy as pandoc/ngspice); absence degrades to an error naming the provisioning script |
| olefile | BSD-2 | OLE2 probe for legacy SolidWorks .sldprt preview (experimental) | ok |
| pywin32 | PSF-2.0 | optional SolidWorks COM automation (Windows-only, feature-detected) | ok |
| numpy / scipy | BSD-3 | math, solvers | ok |
| PyNiteFEA | MIT | structural FEM: member forces + deflections (T-D2 `engine.structural.analysis`) | ok (in-process; MIT permissive, GPL-clean, commercial-safe) |
| matplotlib | PSF-based | colormap LUTs only | ok |
| lib3mf | BSD-2 | 3MF import + export | ok |
| anthropic / openai / mcp / httpx | MIT/Apache | providers | ok |
| llama.cpp (optional managed runtime) | MIT | local model server | ok; optional subprocess/server payload copied to `%APPDATA%\Chudware\llamacpp` when bundled |
| Dear ImGui | MIT | UI | ok |
| **ImGui Test Engine** | **dual: free for OSS/personal; paid for closed-source commercial** | dev-only GUI tests | **OWNER DECISION (2026-06-26): keep as-is, NO commercial seat purchased — accepted risk.** Dev-only, gated behind `CHUDWARE_TESTS=ON`, NEVER shipped in dist (so the *shipped* product carries no Test Engine code). Not a distribution blocker; residual exposure is internal dev use only. |
| GLFW / glad / nlohmann_json / stb | zlib / MIT / MIT | host | ok |
| **Clipper2** | **Boost-1.0** | CAM 2D polygon offset / boolean (toolpath geometry) | ok (**static**, vendored via FetchContent `Clipper2_1.5.4`, populate-only — we compile `clipper.engine/offset/rectclip.cpp` into `chudware_clipper2`). Boost-1.0 is permissive + GPL-clean + commercial-safe; no attribution-in-binary obligation. Confined to `core/cam/`. No vendor CAM posts or GPL CAM source (FreeCAD Path / libarea / opencamlib) are copied — those dialect tables + algorithms are clean-room from public G-code references. |
| **TweetNaCl** | **public domain** | Ed25519 signature verification for the desktop entitlement token (`core/util/tweetnacl.c`, `core/util/entitlement.cpp`) | ok (**static**, vendored — a single ~800-line public-domain C file, byte-identical on Linux, Windows and macOS). Chosen over linking OpenSSL/libsodium specifically to avoid a new system dependency and a per-platform linkage question in a code path that must work on every desktop build. Public domain imposes no attribution or copyleft obligation, so it is commercial-safe and GPL-clean. Only `crypto_sign_open` is used: the client verifies, and never signs — the Ed25519 private key exists solely on the gateway at `~/chudware-web-data/entitlement_ed25519.key` (0600) and is never shipped. |
| Rubik / JetBrains Mono | OFL-1.1 | fonts | ok |
| ~~gmsh~~ (M6) | ~~GPL-2+~~ | ~~FEA tet meshing~~ | **REMOVED (2026-06-26)** — replaced by Netgen (row below). No longer linked or shipped; `ProvisionGmsh.cmake` deleted. |
| **Netgen / nglib** | **LGPL-2.1** | FEA tet meshing (tet4/tet10) | ok (**dynamic** link). Built from source against our OCCT (`core/cmake/ProvisionNetgen.cmake`, tag v6.2.2504), confined to `core/sim/mesher.cpp`; `nglib.dll` + `ngcore.dll` shipped next to `libchudware.dll`. LGPL satisfied by dynamic linking + a written offer for the Netgen source. (One local patch — a `Mesh::DeleteMesh` double-free fix — applied at provision time.) |
| scikit-fem (M6) | BSD-3 | FEA assembly | ok |
| meshio (M6) | MIT | mesh I/O | ok |
| pyamg (M6, optional) | MIT | AMG preconditioner | ok |
| **python-solvespace** (optional backend) | **GPL-3** | sketch solver alternative | **NOT bundled by default**; in-house `nsolve` is primary. If a user enables it, it installs into their runtime venv on demand with a license notice |
| numba (optional, M7 perf) | BSD-2 | LBM JIT | ok if added |
| opencv-python-headless (T-F5) | Apache-2.0 | ChArUco/ArUco fiducial detection + px→mm homography | ok |
| Pillow (T-F5 direct dep) | MIT-CMU | photo ingestion downscale pipeline (engine.vision.ingest) | ok |
| **pdfminer.six** (optional, T-F2 Pillar 8) | **MIT** | `engine/research/datasheet_fetch` PDF->markdown extraction | ok (**in-process, permissive**). **Optional + feature-detected** — NOT a hard dependency (not in `requirements.txt`); absence degrades to `{ok:false, reason:"no_pdf_tool"}`, CI stays green with no new dep. |
| ~~PyMuPDF / pymupdf4llm~~ | ~~AGPL-3~~ | ~~PDF->markdown~~ | **EXCLUDED (T-F2)** — AGPL, never imported in-process. `datasheet_fetch` uses only the MIT extractor above. |
| python-pptx (T-F14) | MIT | .pptx deliverables | ok |
| python-docx (T-F14) | MIT | .docx deliverables | ok |
| openpyxl (T-F14) | MIT | .xlsx deliverables (live formulas) | ok |
| reportlab (T-F14) | BSD-3 | PDF reports + preview rendering | ok |
| pypdfium2 (T-F14) | BSD-3 / Apache-2.0 | PDF → PNG preview raster | ok |
| cairosvg (T-F14, optional) | LGPL-3.0 | best-effort SVG preview | ok (**import-only**, feature-detected, never bundled or required — LGPL dynamic posture per the Netgen precedent) |
| pandoc (T-F14, optional) | GPL-2+ | markup conversion | **subprocess-only**, PATH-discovered, never bundled (mere aggregation — same isolation policy as ngspice) |
| LibreOffice headless (T-F14, optional) | MPL-2.0 | office → PDF for `render_document` previews | **subprocess-only**, PATH-discovered, never bundled |
| pypdf (T-office) | BSD-3 | PDF metadata + page ops (read/rotate/delete/merge, `services/mcp/office_tools.py` office_edit_pdf) | ok. In-process, pure-Python, permissive — same posture as pypdfium2 above. |
| pyproj / PROJ (civil) | MIT / MIT | CRS transforms + grid/combined scale factors (`services/civil/crs.py`) | ok. In-process, permissive. EPSG dataset use per its terms of use (identifiers only; no dataset redistribution beyond the PROJ wheel). |
| civil V&V fixtures (`services/civil/data/`) | US-government public domain / MIT | EPA EPANET Net1 `.inp` (EPANET 2.2, MIT; notices retained in-file), reference results generated with WNTR 1.5 (BSD-3) + EPA SWMM 5.2 via pyswmm, NRCS NEH/TR-55 and FHWA HDS-5 coefficient tables, NOAA Atlas 14 PFDS sample | ok — test data only. WNTR / pyswmm / IfcOpenShell (LGPL-3) were used **out of tree** to generate or check fixtures and are **not** dependencies, imported, or bundled. |
| **BOLTS** (boltsparts) | **GPL-3.0** | standard-part spec reference | **NEVER vendored or linked** (T-B4). Same clean-room posture as KiCad: we learn the `.blt` *format* (documented → interop) and reimplement; no BOLTS data or code enters the tree. A user may point our loader at their own BOLTS checkout (their license, their choice). |
| clean-room ISO/DIN part tables (`engine_oracle/data/bolts/*.blt.yaml`) | project (dimensional data) | deterministic spec-sheet lookup (T-B4) | ok — published ISO/DIN **nominal dimensions are not copyrightable** (same posture as the IPC-7351 row below); transcribed from our own `services/agent/prompts/shared/cheatsheets/hardware.md` + `core/parts/parts_data.h`, NOT from BOLTS. GPL-clean. |
| **fluids** | **MIT** | piping module: ASME B36.10M/B36.19M pipe schedule tables (`fluids.piping.nearest_pipe`) for `services/piping/catalog.py` | ok. Pure-Python (numpy/scipy dep already present); optional at runtime — `catalog.nearest_pipe` degrades contract-shaped when absent. Fitting geometry is our own CSV of public manufacturer dims — ASME standard text is **not** bundled (dimensions are facts, not copyrightable). |
| **probe-rs** (T-C4) | **MIT / Apache-2.0** | hardware flash/debug path: `core/firmware/hardware.cpp` `ProbeRsFlasher` + `ProbeRsDebugger` — `probe-rs download` / `probe-rs reset` / `probe-rs read` subcommands | ok. **subprocess-only**, PATH-discovered, never vendored or linked. Dual MIT/Apache-2.0 — both permissive. Absent binary degrades gracefully (`success=False`). |
| **esptool** | **GPL-2.0+** | ESP32 flash path: `core/firmware/hardware.cpp` `EsptoolFlasher` — `esptool.py write_flash` subprocess | ok. **subprocess-only** (GPL-2.0+ isolation — same policy as QEMU/arduino-cli/ngspice). PATH-discovered, never vendored, never imported in-process. Absent binary degrades gracefully (`success=False`). |
| **clangd** (T-C4) | **Apache-2.0 WITH LLVM-exception** | firmware code panel LSP — spawns `clangd --query-driver='**/arm-none-eabi-*'` for completions/diagnostics | ok. **subprocess-only** (Popen, JSON-RPC over stdio). Apache-2.0-WITH-LLVM-exception is permissive and GPL-clean. Absent binary degrades gracefully (returns `None` / empty diagnostics). |
| **fluids** | **MIT** | piping module: ASME B36.10M/B36.19M pipe schedule tables (`fluids.piping.nearest_pipe`) for `services/piping/catalog.py`; Darcy/Colebrook friction + fitting K-factors for `core/piping/solve.cpp` | ok. Pure-Python (numpy/scipy dep already present); optional at runtime — `catalog.nearest_pipe` degrades contract-shaped when absent. Fitting geometry is our own CSV of public manufacturer dims — ASME standard text is **not** bundled (dimensions are facts, not copyrightable). |
| **CoolProp** (optional backend) | **MIT** | piping module: thermophysical property backend (`core/piping/fluid_properties.cpp`) — REFPROP-grade density/viscosity/vapour-pressure EOS | ok. **Optional, feature-detected, soft-fail** — `properties.fluid_properties` prefers CoolProp when importable and otherwise falls back to a cited built-in model (ideal-gas + Sutherland + tabulated IAPWS water). **NOT** added as a hard install dependency by T-E3; no CoolProp code is bundled. If a user installs it, it links in-process (MIT, GPL-clean). |
| **thermo** (optional backend) | **MIT** | piping module: second thermophysical property backend (`core/piping/fluid_properties.cpp`), Caleb Bell family (same author as `fluids`/`chemicals`/`ht`) | ok. **Optional, feature-detected, soft-fail** — used only when importable, after CoolProp, before the cited built-in fallback. Pure-Python (numpy/scipy/`fluids` already present). **NOT** a hard install dependency of T-E3; no code bundled. |
| **fmpy** (optional backend) | **BSD-2-Clause** | hydraulic module: FMU execution backend (`engine/hydraulic/solve.fmu_simulate`) for OpenHydraulics Modelica circuits | ok. **Optional, feature-detected, soft-fail** — `fmu_simulate` returns `{"ok": False, "skipped": True}` when absent. NOT a hard install dependency; no FMU or Modelica source bundled. If a user installs it, it links in-process (BSD-2-Clause, GPL-clean). |
| **anyio** (>=4.0.0) | MIT | async I/O transport used by services/mcp/server.py (MCP server transport layer); transitive dep of mcp package — T-V3 declaration | ok — in-process, permissive |
| **pydantic** (>=2.0.0) | MIT | tool arg schema validation via FastMCP (indirect dep of mcp package); also used by langgraph and services/ — T-V3 declaration | ok — in-process, permissive |
| **langgraph** (1.2.7) | MIT | DAG orchestrator state machine (services/dag/) — T-A1 | ok |
| **langgraph-checkpoint-sqlite** (3.1.0) | MIT | SQLite persistence for DAG run checkpoints (services/dag/) — T-A1 | ok |
| **aiosqlite** (0.22.1, transitive) | MIT | async SQLite backend for langgraph-checkpoint-sqlite | ok |
| **Renode** (optional, T-C3) | **MIT** | firmware emulation: ELF boot + UART capture + GPIO state on Renode platform descriptions (`renode --disable-xwt`, subprocess-only) | ok. **Subprocess-only**, PATH-discovered, never vendored or linked. Bundled `.repl` platform description files (in `engine_oracle/firmware/templates/renode_platforms/`) are adapted from Renode's open-source platform infrastructure (MIT) with attribution in the bundled `LICENSE` file. |
| **QEMU** (optional fallback, T-C3) | **GPL-2.0+** | firmware emulation fallback: generic Cortex-M via `qemu-system-arm -M mps2-an385`, semihosting stdout capture | ok. **Subprocess-only**, PATH-discovered, never bundled or linked (same aggregation isolation as ngspice). |
| **langchain-core** (1.4.8, transitive) | MIT | langgraph dependency | ok |
| **langsmith** (0.9.6, transitive) | MIT | langgraph tracing dep (no API key required) | ok |
| qrcode (T-F6, optional) | MIT | QR code generation for phone handoff URL | ok |
| open3d (T-F6, optional) | MIT | mesh cleanup + scale normalization post-photogrammetry | ok |
| Meshroom/AliceVision (T-F6, subprocess only) | MPL-2.0 | photogrammetry reconstruction — NEVER imported, subprocess via MESHROOM_BIN | ok (subprocess) |
| COLMAP (T-F6, subprocess alternate) | BSD-3 | photogrammetry alternate — NEVER imported, subprocess via COLMAP_BIN | ok (subprocess) |
| **CalculiX** (`ccx`, optional, T-BL20 / T-BL04) | **GPL-2.0** | (T-BL20) FEA V&V cross-validation: `scripts/vv_benchmark.py` `bench_calculix()` — an independent structural "second opinion" solver run on a clean-room-generated cantilever `.inp`, cross-checked against Roark and our own C++ FEA. (T-BL04) EXPANDED to first-class simulation MCP tools: `services/mcp/fea_advanced.py` `run_ccx()` drives `run_fea_thermal` (`*HEAT TRANSFER`), `run_fea_buckling` (`*BUCKLE`), and `run_fea_contact` (`*STATIC,NLGEOM` + `*CONTACT PAIR`) on clean-room `.inp` box proxies. | ok. **Subprocess-only**, PATH-discovered (`shutil.which("ccx")`, or `CHUDWARE_CCX` env), never bundled or linked (same aggregation isolation as ngspice/Renode/QEMU). Every ccx-backed tool degrades gracefully (`{ok:false, skipped:true}`) when absent — not installed in the dev sandbox these tasks shipped from; the `.inp` generators + `.dat` parsers are covered by pure-Python unit tests that need no binary. NO CalculiX source, example decks, or `.inp` templates are copied — the input decks are synthesized clean-room from our own geometry/loads in the public Abaqus/CalculiX input syntax. |

Rules:
- Adding any dependency requires a row here in the same change.
- `services/installer/build_release.py` gains (M10) a license-manifest step that dumps wheel METADATA
  licenses into `dist/Chudware/licenses/` for shipped wheels.

## ECAD module posture (see `ECAD_PLAN.md §2.1`)

| item | license | role | posture |
|---|---|---|---|
| **KiCad source** | **GPL-3.0** | reference only | **NEVER copied or linked.** Clean-room: we learn concepts and reimplement; no KiCad source enters the tree. |
| KiCad file formats (`.kicad_sch/_pcb/_sym/_mod`, S-expr) | n/a (format) | import/export | ok — parsing a documented format is interop, not derivation. Our `services/ecad/library/sexpr.py` is clean-room. |
| KiCad official **libraries** (symbols/footprints/3D) | CC-BY-SA-4.0 + design-output exception | user-supplied content | **not vendored.** Users point at their own installed KiCad libs; the exception permits use in board outputs without imposing the license on the design. Attribution preserved on import. |
| **ngspice** (optional) | GPL-2/3 (mixed BSD) | alternate sim backend | **isolation policy** (same as gmsh): subprocess-only, NOT bundled by default; native `core/sim/spice.cpp` (numpy/scipy, BSD) is primary. Installs on demand with a notice if a user opts in. |
| our `core/sim/spice.cpp`, `engine/ecad/*` | project license | EDA engine + simulator | ok — original code, numpy/scipy/build123d/OCP only. |
| IPC-7351 footprint geometry | public standard | starter footprints | ok — dimensional standards are not copyrightable; geometry generated by our own script. |

Rule: the ECAD default distribution stays **GPL-clean**, identical to the CAD posture above.

## Flywheel / training-data pipeline (T-H5)

| package | license | role | posture |
|---|---|---|---|
| **dvc** | **Apache-2.0** | training-data versioning + push/pull to local/NAS/S3 remote | ok — in-process via `python -m dvc` subprocess calls (GPL-safe isolation); Apache-2.0 permits in-process use too. Never shipped in the desktop installer. Dev/internal tooling only. |
| **argilla** (client) | **Apache-2.0** | Python client for the self-hosted Argilla annotation server | ok — in-process, dev/internal only; optional import with graceful degradation (`ArgillaUnavailable` if not installed). Never shipped in the desktop installer. |
| **Argilla server** (Docker) | **Apache-2.0** | Self-hosted human-annotation UI + API (runs as a Docker container on hunter-pc/localhost) | ok — subprocess/Docker isolation; Apache-2.0. Not bundled with the desktop app. |
| **pandas / pyarrow** | BSD-3 / Apache-2.0 | Argilla client transitive deps | ok — permissive; not shipped in desktop installer. |
| **huggingface_hub / datasets** | Apache-2.0 | Argilla client transitive deps | ok — Apache-2.0; not shipped in desktop installer. |

## Fleet training runner (T-H6)

| package | license | role | posture |
|---|---|---|---|
| **mlflow** (client) | **Apache-2.0** | experiment tracking client — logs params, metrics, artifacts per training run | ok — in-process, dev/internal only; optional import with graceful degradation (logs to ./mlruns if server unreachable). Never shipped in the desktop installer. |
| **MLflow server** (Docker) | **Apache-2.0** | Self-hosted experiment tracking UI + artifact store (runs as Docker container on hunter-pc/localhost) | ok — subprocess/Docker isolation; Apache-2.0. Not bundled with the desktop app. |
| **trl** | **Apache-2.0** | HuggingFace TRL — SFTTrainer, DPOTrainer, GRPOTrainer for fine-tuning | ok — in-process, dev/internal only; lazy import (only when training runs). Never shipped in the desktop installer. |
| **peft** | **Apache-2.0** | HuggingFace PEFT — LoRA adapter training | ok — in-process, dev/internal only; lazy import. Never shipped in the desktop installer. |
| **Unsloth** | **Apache-2.0** | Fast LoRA training via custom CUDA kernels | ok — in-process, dev/internal only; lazy import with fallback to plain TRL. Never shipped in the desktop installer. |
| **PyYAML** | **MIT** | YAML recipe file parsing | ok — in-process; already a common transitive dep. Never shipped in the desktop installer. |
| **vLLM (server)** (T-H9) | **Apache-2.0** | per-task LoRA serving runtime — external OpenAI-compatible inference server launched with `--enable-lora` (`docs/reference/serving_runbook.md`); Chudware talks to it over HTTP via the `vllm` provider (`services/agent/providers/vllm_provider.py`), NEVER imports it | ok — external server, same posture as the llama.cpp managed runtime / LM Studio: subprocess/HTTP isolation, Apache-2.0 permissive, GPU/dev-internal only, never bundled in the desktop installer. No `requirements.txt` line (never imported in-process — an HTTP client, not a Python dependency). |
| **Offline VLM weights** (T-H10): Qwen3-VL-4B-Instruct, granite-vision-4.1-4b, Gemma 4 E4B-it (+ their GGUF/mmproj) | **Apache-2.0** (all three) | `experimental.vision_review` render self-review / drawing reading; the offline vision track candidates | ok — **bundleable in a paid, ITAR-safe offline app** (all Apache-2.0). Served **subprocess-only** via the llama.cpp MIT runtime row above (`llama-server --mmproj`, loopback). NOT vendored in the repo — downloaded out-of-band (`tools/flywheel/vlm/candidates.py:provision_instructions`). RULED OUT: Qwen2.5-VL-3B (Qwen Research License — non-commercial) and Moondream 3 (BUSL-1.1). |
| **Translator base weights** (T-F11): Qwen3-4B-Instruct-2507; Gemma 3 4B-it (+ their Q4_K_M GGUF) | Qwen3 = **Apache-2.0**; Gemma 3 = **Gemma ToU** (commercial OK, restrictions) | Pillar-13 learned narration translator (state→text); served locally | ok — served **subprocess-only** via the llama.cpp MIT runtime row above (drop-in for the existing local-model default). NOT vendored — downloaded out-of-band (`tools/translator/candidates.py:provision_instructions`). Qwen3-4B is the license-clean PRIMARY; Gemma 3 4B is the parallel candidate (verify Gemma ToU restrictions before shipping it as default). RULED OUT: Qwen2.5-3B (non-commercial research license). |
## Structural section data (T-D3)

| item | license | role | posture |
|---|---|---|---|
| **AISC shapes curated table** (`services/structural/data/aisc_shapes/aisc_shapes_curated.json`) | n/a (dimensional facts) | AISC W/S/C section properties for D/C ratio checks | **Clean-room curated table of ~18 common shapes.** Property values (A, Ix, Sx, Zx, …) are dimensional facts re-stated from widely published engineering references (Salmon & Johnson *Steel Structures* 5th ed, NSAI, CISC Handbook) — *not* a bulk copy of the AISC Shapes Database compilation (whose license is not clearly public-domain). The numbers are physical facts of steel geometry; the AISC compilation's selection/layout/commentary is what is proprietary. Posture: same as `analysis.py` `_MATERIALS` block. See `work/questions/archive/T-D3.md` Q1. For unlisted shapes, T-D4/T-D5 can accept a user-supplied AISC XLSX path at runtime (not bundled). |
| **ANSI/AWC NDS Supplement lumber sizes** (`services/structural/data/lumber_table.json`) | n/a (public standard) | Dressed (S4S) lumber actual dimensions + section properties | **Public standard facts.** PS 20 (American Softwood Lumber Standard) dressed sizes are a federal/industry standard; dimensional facts are not copyrightable. Fb/Fc allowable-stress values used in `checks.py` are the same NDS Supplement Table 4A numbers reproduced in open structural textbooks. No NDS document text is copied. |

## PLM vault (Pillar 19, Workstream R — T-R2)

Git binding chosen in `docs/review/PLM_OSS_SURVEY.md` and frozen in `contracts/plm.md` §7.
Added by T-R2 (first code task importing dulwich) per the "adding a dependency needs a
row in the same change" rule.

| package | license | role | posture |
|---|---|---|---|
| **dulwich** (`services/plm/gitbind.py`) | **Apache-2.0 OR GPL-2.0-or-later** (dual; SPDX `Apache-2.0 OR GPL-2.0-or-later`) | pure-Python git implementation — the object store + commit/tree writer behind the hidden `.aicadvault/` version-control vault | ok — **in-process, take the Apache-2.0 arm** (permissive, GPL-clean, commercial-safe under the guardrails). A hard `requirements.txt` dep of `services/plm/`. Chosen for zero-provisioning cross-platform parity (no compiled wheel assumption, no binary-on-PATH); `pip install`-only, identical on Fedora + Windows. pygit2 was declined (GPL-linking-exception ambiguity + per-platform native wheel). |
| **git** (optional accelerator for `services/plm/`) | GPL-2.0+ | system `git` binary, used ONLY for `gc`/`repack` acceleration when present on PATH | ok — **subprocess-only, PATH-discovered, never bundled, feature-detected**; its absence degrades transparently to the guaranteed pure-dulwich path (`GitBinding.repack()`). Same mere-aggregation isolation as ngspice/QEMU/pandoc/esptool. No `requirements.txt` line (never imported in-process — spawned as a subprocess). |
| **websockets** (`services/plm/server.py`, `services/plm/live_client.py`) | **BSD-3-Clause** | `aicad.plm-live/1` self-hosted authoritative WebSocket relay + Python client seam | ok — in-process, permissive, commercial-safe. Hard-pinned in `requirements.txt`; no SaaS dependency and no CRDT/OT code vendored. The same library is already used by the Webservice gateway. |

## Browser session service (T-X1, workstream X)

| package | license | role | posture |
|---|---|---|---|
| **playwright** | **Apache-2.0** | `services/browser/session.py` — drives a persistent-profile Chromium context so a user can sign into a parts/document source site once and have the copilot reuse that session for headless fetches | ok — in-process, permissive. OSS-first pick over Selenium (weaker native async/persistent-context support) and Puppeteer (Node-only, would require a second runtime). The Chromium **binary** itself (`playwright install chromium`) is a separate one-time download into Playwright's own cache dir, not vendored in this repo and not part of `requirements.txt`. See `services/browser/README.md`. This is the mechanism, not the policy — per-source TOS/licensing posture for what gets fetched through it is tracked separately in `contracts/license_debt.md`, not here. |

## FEA mesher cutover (2026-08-30)

| package | license | role | posture |
|---|---|---|---|
| **Polyhedral-FEA** (`Hunter-124/Polyhedral-FEA`, pinned commit `955457901e5fe9ba7d46c4cafee710000df9f4bd`) | **BSD-3-Clause** | sole in-process STEP/BREP volume mesher, quadratic boundary projection, adaptive ZZ recovery, optional learned mesh advice | ok — statically linked from pinned source, shares Chudware's Eigen/OpenCASCADE targets, permissive attribution posture |
| ~~Netgen / nglib~~ | ~~LGPL-2.1~~ | ~~legacy FEA tet mesher~~ | **REMOVED** — no source provisioner, link dependency, runtime DLL staging, backend selector, or production callsite remains |

## Chudcode embedded agent runtime (2026-08-31)

| package | license | role | posture |
|---|---|---|---|
| **Chudcode** (`Hunter-124/chudcode`, artifact-locked commit `e3d8790f50d33a86ef7b295eeeda504fd6399835`) | **MIT** | compiled protocol-v2 online agent runtime for providers, sessions, auth, memory, compaction, browser/computer, STT, and host-tool orchestration | ok — **subprocess/RPC-only** behind `vendor/chudcode/CHUDCODE_ARTIFACT.lock`; Chudware never imports or vendors the Chudcode engine source. The compiled binary's Bun-embedded runtime and all bundled third-party packages, fonts, stb/native-GUI code, and ASR components are covered by the shipped `CHUDCODE-NOTICE` and `CHUDCODE-THIRD-PARTY-NOTICES.txt`; their SHA-256 values are lock-validated and both files are required in every signed desktop/web payload. No OMP source/runtime boundary remains in Chudware. |

## Native linear buckling cutover (2026-09-07)

The historical CalculiX buckling role above is superseded. `run_fea_buckling` and
`sim_run_buckling` use Chudware's in-process static prestress, geometric stiffness,
and shared Spectra generalized eigensolver, on the real tet4/tet10 CAD mesh.
No CalculiX executable, deck generator, or parser is used for buckling. This adds
no dependency: the existing Eigen/Spectra and optional CHOLMOD solver stack is
reused. CalculiX retains its existing subprocess-only posture for remaining
non-buckling tools and validation scenarios.

**2026-10 — hydraulic circuit simulation is native.** `hydraulic_simulate`
(`core/hydraulic/sim.cpp`, contracts/hydraulic_schema.md §8) replaced the
`hydraulic_fmu_simulate` stub. The **fmpy** row above is retired: no FMU runtime is
loaded or probed, and the native solver adds no dependency.
