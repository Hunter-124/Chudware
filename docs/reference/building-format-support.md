# Building Format Support — License-Clean Paths

> **Provenance:** folded in from the sibling clean-room project "Clean Engine"
> (`D:\Clean Engine\ODA\03-build-your-own.md`). General, license-aware guidance for
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
  **This repo already did this step**: DXF import via **ezdxf** (MIT),
  `engine/doc/formats.py` + `engine/doc/formats_dxf.py`.
- **DWG:** implement from the **ODA Open Design Specification for .dwg files**
  (public). Or use open-source:
  - **LibreDWG** (GNU LGPL/GPL — check which; affects linking) — reads/writes DWG.
  - **libdxfrw** (permissive) — DXF + basic DWG.
- **Reality check:** full DWG fidelity is a large effort (that's the ODA's whole
  business). This repo's documented plan (`docs/design/PLATFORM_ADR.md` §5,
  `work/backlog/T-BL02.md`) is **DWG via an ODA membership later**; LibreDWG would
  be **subprocess-convert only** (its GPL/LGPL license is incompatible with
  in-process linking under this repo's GPL-clean posture — see
  `contracts/licenses.md`). If production DWG fidelity is needed without that
  effort, the legitimate shortcut is licensing the ODA commercially — not
  decompiling anyone's DWG code.

## Goal: read/write DGN

- Implement from Bentley's public DGN documentation, or evaluate open readers.
  DGN's element-stream model is simpler to parse than DWG. Not on this repo's
  current interop surface.

## Goal: BIM (IFC)

- **Fully open.** Use **IfcOpenShell** (LGPL) or implement from **ISO 16739** +
  buildingSMART's public EXPRESS schema. No proprietary dependency at all. Not on
  this repo's current interop surface (ShopCraft is mechanical CAD, not BIM), but
  IfcOpenShell already appears in `contracts/licenses.md` for a different purpose
  (BOLTS/clean-room posture reference) — worth checking that entry before adding
  IFC support so the license posture stays consistent.

## Goal: STEP / IGES (MCAD exchange)

- **STEP:** **ISO 10303** is public; geometry kernels below can consume it.
  Open tooling: **OpenCASCADE** (LGPL-style) has robust STEP/IGES import/export.
  **This repo already implements both**: STEP (AP214) via
  `build123d.export_step`/`import_step`, IGES via `OCP.IGESControl` — both in
  `engine/doc/formats.py`, both riding on OCCT (see `contracts/licenses.md`, row
  `cadquery-ocp / OCCT`).
- **IGES:** **IGES 5.3** public; OpenCASCADE handles it too (same as above).

## Goal: a 3D geometry kernel (BREP, booleans, tessellation)

- **OpenCASCADE Technology (OCCT)** — mature open BREP kernel (LGPL-style).
  **This is this repo's kernel** (via `build123d`/`cadquery-ocp`, dynamically
  linked — see `contracts/licenses.md`).
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
  the C++ host's render backend (`host/src/render/aicm_loader`).

## Goal: export to PDF / SVG / STL / glTF

- All public specs: **ISO 32000** (PDF), **W3C SVG**, STL (public), **glTF**
  (Khronos). Many permissive libraries exist for each.
- **This repo already covers STL, glTF/GLB, OBJ, PLY, and 3MF** (mesh lane, via
  `build123d`/`trimesh`/`lib3mf` — `contracts/licenses.md`), plus PDF for
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
shape**: `engine/doc/formats.py`'s `FORMAT_REGISTRY`/`import_path` is the isolated
format-I/O layer; it feeds build123d/OCCT shapes into the document model
(`contracts/doc_schema.md`); tessellation to AICM (`contracts/mesh_format.md`) is
the Gi/Gs-equivalent boundary to the render backend. Adding a new format (per
`work/backlog/T-BL02.md`) means adding a module next to `formats_dxf.py` and a row
in `FORMAT_REGISTRY` — not touching the document model or renderer.

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
