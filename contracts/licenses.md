# Contract: Dependency License Ledger

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
>    was reimplemented on **Netgen / nglib (LGPL-2.1)** — see the Netgen row below. `libaicad.dll`
>    no longer imports gmsh (verified: `dumpbin /dependents libaicad.dll` shows `nglib.dll`, no
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
| olefile | BSD-2 | OLE2 probe for legacy SolidWorks .sldprt preview (experimental) | ok |
| pywin32 | PSF-2.0 | optional SolidWorks COM automation (Windows-only, feature-detected) | ok |
| numpy / scipy | BSD-3 | math, solvers | ok |
| matplotlib | PSF-based | colormap LUTs only | ok |
| lib3mf | BSD-2 | 3MF import + export | ok |
| anthropic / openai / mcp / httpx | MIT/Apache | providers | ok |
| llama.cpp (optional managed runtime) | MIT | local model server | ok; optional subprocess/server payload copied to `%APPDATA%\AI-CAD\llamacpp` when bundled |
| Dear ImGui | MIT | UI | ok |
| **ImGui Test Engine** | **dual: free for OSS/personal; paid for closed-source commercial** | dev-only GUI tests | **OWNER DECISION (2026-06-26): keep as-is, NO commercial seat purchased — accepted risk.** Dev-only, gated behind `AI_CAD_TESTS=ON`, NEVER shipped in dist (so the *shipped* product carries no Test Engine code). Not a distribution blocker; residual exposure is internal dev use only. |
| GLFW / glad / nlohmann_json / stb | zlib / MIT / MIT | host | ok |
| **Clipper2** | **Boost-1.0** | CAM 2D polygon offset / boolean (toolpath geometry) | ok (**static**, vendored via FetchContent `Clipper2_1.5.4`, populate-only — we compile `clipper.engine/offset/rectclip.cpp` into `aicad_clipper2`). Boost-1.0 is permissive + GPL-clean + commercial-safe; no attribution-in-binary obligation. Confined to `core/cam/`. No vendor CAM posts or GPL CAM source (FreeCAD Path / libarea / opencamlib) are copied — those dialect tables + algorithms are clean-room from public G-code references. |
| Rubik / JetBrains Mono | OFL-1.1 | fonts | ok |
| ~~gmsh~~ (M6) | ~~GPL-2+~~ | ~~FEA tet meshing~~ | **REMOVED (2026-06-26)** — replaced by Netgen (row below). No longer linked or shipped; `ProvisionGmsh.cmake` deleted. |
| **Netgen / nglib** | **LGPL-2.1** | FEA tet meshing (tet4/tet10) | ok (**dynamic** link). Built from source against our OCCT (`core/cmake/ProvisionNetgen.cmake`, tag v6.2.2504), confined to `core/sim/mesher.cpp`; `nglib.dll` + `ngcore.dll` shipped next to `libaicad.dll`. LGPL satisfied by dynamic linking + a written offer for the Netgen source. (One local patch — a `Mesh::DeleteMesh` double-free fix — applied at provision time.) |
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
| **BOLTS** (boltsparts) | **GPL-3.0** | standard-part spec reference | **NEVER vendored or linked** (T-B4). Same clean-room posture as KiCad: we learn the `.blt` *format* (documented → interop) and reimplement; no BOLTS data or code enters the tree. A user may point our loader at their own BOLTS checkout (their license, their choice). |
| clean-room ISO/DIN part tables (`engine_oracle/data/bolts/*.blt.yaml`) | project (dimensional data) | deterministic spec-sheet lookup (T-B4) | ok — published ISO/DIN **nominal dimensions are not copyrightable** (same posture as the IPC-7351 row below); transcribed from our own `agent/prompts/shared/cheatsheets/hardware.md` + `core/parts/parts_data.h`, NOT from BOLTS. GPL-clean. |
| **fluids** | **MIT** | piping module: ASME B36.10M/B36.19M pipe schedule tables (`fluids.piping.nearest_pipe`) for `engine/piping/catalog.py` | ok. Pure-Python (numpy/scipy dep already present); optional at runtime — `catalog.nearest_pipe` degrades contract-shaped when absent. Fitting geometry is our own CSV of public manufacturer dims — ASME standard text is **not** bundled (dimensions are facts, not copyrightable). |
| **fluids** | **MIT** | piping module: ASME B36.10M/B36.19M pipe schedule tables (`fluids.piping.nearest_pipe`) for `engine/piping/catalog.py`; Darcy/Colebrook friction + fitting K-factors for `engine/piping/solve.py` | ok. Pure-Python (numpy/scipy dep already present); optional at runtime — `catalog.nearest_pipe` degrades contract-shaped when absent. Fitting geometry is our own CSV of public manufacturer dims — ASME standard text is **not** bundled (dimensions are facts, not copyrightable). |
| **CoolProp** (optional backend) | **MIT** | piping module: thermophysical property backend (`engine/piping/properties.py`) — REFPROP-grade density/viscosity/vapour-pressure EOS | ok. **Optional, feature-detected, soft-fail** — `properties.fluid_properties` prefers CoolProp when importable and otherwise falls back to a cited built-in model (ideal-gas + Sutherland + tabulated IAPWS water). **NOT** added as a hard install dependency by T-E3; no CoolProp code is bundled. If a user installs it, it links in-process (MIT, GPL-clean). |
| **thermo** (optional backend) | **MIT** | piping module: second thermophysical property backend (`engine/piping/properties.py`), Caleb Bell family (same author as `fluids`/`chemicals`/`ht`) | ok. **Optional, feature-detected, soft-fail** — used only when importable, after CoolProp, before the cited built-in fallback. Pure-Python (numpy/scipy/`fluids` already present). **NOT** a hard install dependency of T-E3; no code bundled. |
| **fmpy** (optional backend) | **BSD-2-Clause** | hydraulic module: FMU execution backend (`engine/hydraulic/solve.fmu_simulate`) for OpenHydraulics Modelica circuits | ok. **Optional, feature-detected, soft-fail** — `fmu_simulate` returns `{"ok": False, "skipped": True}` when absent. NOT a hard install dependency; no FMU or Modelica source bundled. If a user installs it, it links in-process (BSD-2-Clause, GPL-clean). |
| **langgraph** (1.2.7) | MIT | DAG orchestrator state machine (services/dag/) — T-A1 | ok |
| **langgraph-checkpoint-sqlite** (3.1.0) | MIT | SQLite persistence for DAG run checkpoints (services/dag/) — T-A1 | ok |
| **aiosqlite** (0.22.1, transitive) | MIT | async SQLite backend for langgraph-checkpoint-sqlite | ok |
| **langchain-core** (1.4.8, transitive) | MIT | langgraph dependency | ok |
| **langsmith** (0.9.6, transitive) | MIT | langgraph tracing dep (no API key required) | ok |

Rules:
- Adding any dependency requires a row here in the same change.
- `installer/build_release.py` gains (M10) a license-manifest step that dumps wheel METADATA
  licenses into `dist/AI-CAD/licenses/` for shipped wheels.

## ECAD module posture (see `ECAD_PLAN.md §2.1`)

| item | license | role | posture |
|---|---|---|---|
| **KiCad source** | **GPL-3.0** | reference only | **NEVER copied or linked.** Clean-room: we learn concepts and reimplement; no KiCad source enters the tree. |
| KiCad file formats (`.kicad_sch/_pcb/_sym/_mod`, S-expr) | n/a (format) | import/export | ok — parsing a documented format is interop, not derivation. Our `library/sexpr.py` is clean-room. |
| KiCad official **libraries** (symbols/footprints/3D) | CC-BY-SA-4.0 + design-output exception | user-supplied content | **not vendored.** Users point at their own installed KiCad libs; the exception permits use in board outputs without imposing the license on the design. Attribution preserved on import. |
| **ngspice** (optional) | GPL-2/3 (mixed BSD) | alternate sim backend | **isolation policy** (same as gmsh): subprocess-only, NOT bundled by default; native `engine/sim/circuit.py` (numpy/scipy, BSD) is primary. Installs on demand with a notice if a user opts in. |
| our `engine/sim/circuit.py`, `engine/ecad/*` | project license | EDA engine + simulator | ok — original code, numpy/scipy/build123d/OCP only. |
| IPC-7351 footprint geometry | public standard | starter footprints | ok — dimensional standards are not copyrightable; geometry generated by our own script. |

Rule: the ECAD default distribution stays **GPL-clean**, identical to the CAD posture above.
