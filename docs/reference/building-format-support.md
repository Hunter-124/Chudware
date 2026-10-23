# Building Format Support — License-Clean Paths

> **Provenance:** folded in from the separate clean-room research project "Clean Engine"
> (its "build your own" guide). General, license-aware guidance for
> implementing CAD format readers/writers from public specs and open-source
> building blocks — not specific to any one codebase. See
> `docs/reference/README.md` for how it maps onto this repo's actual interop
> surface, and `contracts/licenses.md` for this repo's binding license ledger
> (the authority on what's actually shipped and how).

You don't need any vendor's internals. Below are practical, defensible routes per
goal, using public specs and open-source building blocks. Check each library's
license against the target product (permissive vs. copyleft) before shipping —
in this repo that check is `contracts/licenses.md`, not this file.

## Goal: read/write DWG & DXF

- **DXF (start here):** implement directly from Autodesk's public **DXF
  Reference**. It's a tagged (group-code, value) format — a weekend to a basic
  reader, and it teaches the whole data model (see `cad-data-model.md`).
  **This repo already did this step**: production DXF import and export are hand-written
  in `core/doc/` (no ezdxf, no third-party DXF library; INSERT block references are
  expanded, 3DFACE/POLYFACE meshes import as mesh bodies, ACIS solids import as exact
  B-rep, and `export_file` writes R12 ASCII, or R2000 with ACIS 3DSOLIDs when there are
  solids). The Python helper `services/formats/formats_dxf.py` (ezdxf, MIT) is the
  import-only parity oracle.
- **DWG:** implement from the **ODA Open Design Specification for .dwg files**
  (public). Or use open-source:
  - **LibreDWG** (GPL-3.0 per its `contracts/licenses.md` row — copyleft, so
    subprocess-only here) — reads/writes DWG.
  - **libdxfrw** (GPL-2.0-or-later — copyleft; no ledger row) — DXF + basic DWG.
- **Reality check:** full DWG fidelity is a large effort (that's the ODA's whole
  business). Export (`export_file`, `export_drawing`, `export_flat_dxf` with format
  `dwg`) is Chudware's own AutoCAD R2000 (AC1015) encoder written from the public ODA
  specification (`core/doc/dwg_write.cpp`, no external tool). The 3D solids of R2013+ files
  are read natively from the DWG data storage (`core/doc/dwg_native.cpp`); import of a DWG's 2D
  entities runs LibreDWG `dwg2dxf` and reuses the DXF importer; it is found on `PATH`,
  else in the per-user install that
  `python3 scripts/provision_libredwg.py` builds from the SHA-pinned upstream release.
  LibreDWG is GPL-3.0, so it is subprocess-only, never linked or bundled
  (`contracts/licenses.md`). Whether to pursue an ODA membership for higher-fidelity DWG
  is an open decision. If production DWG fidelity is needed
  beyond that, the legitimate shortcut is licensing the ODA commercially — not
  decompiling anyone's DWG code.

## Goal: read/write DGN

- Implement from Bentley's public DGN documentation, or evaluate open readers.
  DGN's element-stream model is simpler to parse than DWG. Not on this repo's
  current interop surface.

## Goal: BIM (IFC)

- **Fully open.** Use **IfcOpenShell** (LGPL) or implement from **ISO 16739** +
  buildingSMART's public EXPRESS schema. No proprietary dependency at all. Not on
  this repo's current interop surface (Chudware is mechanical CAD, not BIM).
  `contracts/licenses.md` has no IfcOpenShell row; one must be added (per that
  file's rules) before IFC support uses it.

## Goal: STEP / IGES (MCAD exchange)

- **STEP:** **ISO 10303** is public; geometry kernels below can consume it.
  Open tooling: **OpenCASCADE** (LGPL-style) has robust STEP/IGES import/export.
  **This repo already implements both**: OCCT `STEPControl_*` and `IGESControl_*`
  readers/writers in `core/doc/formats.cpp` (the Python oracle
  `services/formats/formats.py` reaches the same OCCT code via build123d/OCP; ledger
  row `cadquery-ocp / OCCT` in `contracts/licenses.md`).
- **IGES:** **IGES 5.3** public; OpenCASCADE handles it too (same as above).

## Goal: a 3D geometry kernel (BREP, booleans, tessellation)

- **OpenCASCADE Technology (OCCT)** — mature open BREP kernel (LGPL-style).
  **This is this repo's kernel** — used directly by the C++ core and via
  `build123d`/`cadquery-ocp` in Python helpers (see `contracts/licenses.md`).
- **CGAL** — computational geometry (GPL/commercial dual). Not used here.
- Or build a focused geometry library from *The NURBS Book* (Piegl & Tiller) and
  standard CAGD literature — background only; not a build target for this repo.

## Goal: rendering / a viewer

- Emit abstract primitives from entities (a `Gi`-style pattern, see
  `cad-data-model.md`), render via any open stack: OpenGL/Vulkan/WebGL/three.js.
  For web viewers, **glTF** (Khronos open) is the ideal interchange target — and
  is already one of this repo's supported mesh formats.
- **This repo's actual render path:** the AICM tessellation format
  (`contracts/mesh_format.md`) plays the "abstract primitive stream" role, fed to
  the C++ host's render backend (`host/src/render/aicm_loader.cpp`).

## Goal: export to PDF / SVG / STL / glTF

- All public specs: **ISO 32000** (PDF), **W3C SVG**, STL (public), **glTF**
  (Khronos). Many permissive libraries exist for each.
- **This repo already covers STL, glTF/GLB, OBJ, PLY, and 3MF** (mesh lane in
  `core/doc/formats.cpp`: OCCT mesh readers/writers plus lib3mf for 3MF; the Python
  oracle uses trimesh/lib3mf — `contracts/licenses.md`), plus PDF for
  deliverables/reports (`reportlab`, `pypdfium2`). SVG export is not currently
  implemented; `cairosvg` appears in `contracts/licenses.md` only as an
  optional, import-only, feature-detected SVG *preview* path (LGPL, never
  bundled), not a general SVG export/import format handler.

## Goal: point clouds

- **LAS/LAZ** (ASPRS public; **LASlib/LASzip**, PDAL), **E57** (ASTM; **libE57**).
  Not on this repo's current interop surface.

## Architecture recommendation (clean & maintainable)

```
[ Format I/O layer ]  DXF/DWG/DGN/IFC/STEP readers+writers  (public specs)
        │  produces/consumes
[ Document model ]    database + symbol tables + dictionaries + handle graph
        │  entities expose draw()
[ Geometry (Ge) ]     OCCT or your NURBS/CAGD lib
        │  emits primitives
[ Graphics (Gi/Gs) ]  abstract primitive stream → OpenGL/Vulkan/glTF/PDF backends
```

Keep the format layer isolated so adding a format never touches the document
model, and keep geometry/graphics swappable. **This repo already follows this
shape**: `core/doc/formats.cpp` (`format_registry()` + the per-format import/export
dispatch) is the isolated format-I/O layer; it feeds OCCT shapes into the document
model (`contracts/doc_schema.md`); tessellation to AICM (`contracts/mesh_format.md`) is
the Gi/Gs-equivalent boundary to the render backend. Adding a new format
means adding its importer and a `format_registry()` row in
`core/doc/formats.cpp` with fixture round-trip tests — not touching the document
model or renderer.

## The one rule that keeps this clean

Build from the **public specifications** cited in `cad-file-formats.md` and the
open-source libraries above, and record every new dependency in
`contracts/licenses.md` in the same change (existing repo rule — see that file's
"Rules" section). Do **not** decompile any vendor's protected binaries and code
from what you see there — that produces a derivative work and breaches the
license. Public spec in → independent implementation out. This is exactly the
posture already recorded in `contracts/licenses.md` for KiCad (ECAD) and BOLTS
(hardware data): learn the documented format/behavior, reimplement clean-room,
never copy or reverse-derive from proprietary source.
