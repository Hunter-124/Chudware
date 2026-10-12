# CAD File Formats — Survey with Authoritative Public Specs

> **Provenance:** folded in from the sibling clean-room project "Clean Engine"
> (`D:\Clean Engine\ODA\01-file-formats.md`). That material is sourced *only* from
> public specifications, ISO/ASTM/ASPRS standards, and vendor-published developer
> docs — never from any proprietary binary or decompiled code (see the Clean Engine
> README's clean-room statement). Treat this file the same way: it documents
> **formats**, not any vendor's implementation of them, and cites the public spec
> to build from in each case. See `docs/reference/README.md` for how this fits
> into Chudware's own interop work, and `contracts/licenses.md` for this repo's
> license posture.

Scope: a survey of ~12 CAD/BIM/mesh format families, each with what it is, the
**authoritative public specification you'd build a reader/writer from**, and a
short structural summary drawn from that public spec.

---

## 1. DWG — AutoCAD native drawing

- **Owner/origin:** Autodesk (proprietary native format).
- **Authoritative public spec:** the **Open Design Specification for .dwg files**,
  published freely by the Open Design Alliance (ODA). This is the canonical public
  description of the binary container and object encodings, published precisely so
  third parties can read/write DWG without Autodesk's code.
- **Structure (from the public spec):** versioned binary container
  (`AC1015`=2000 … `AC1032`=2018). Header with version + section locator; sections
  for header variables, classes, objects (the entity/object data), the object map
  (handle→offset), and optional preview/summary/appinfo. Objects are handle-keyed
  records with bit-packed fields; entities reference symbol-table records (layer,
  linetype, style) by handle.
- **This repo:** DWG is **written** by Chudware's own AutoCAD R2000 (AC1015) encoder,
  implemented from the public spec (`core/doc/dwg_write.cpp`, no external tool): every
  table and control object AutoCAD expects, the named object dictionary with Model/Layout1,
  layers with colour and CONTINUOUS/DASHED/CENTER linetypes, LWPOLYLINE/ARC/CIRCLE/LINE/
  3DFACE (edge visibility)/SOLID/aligned TEXT, and 3DSOLID/BODY carrying SAT (ACIS 4.00)
  inline, with per-object CRCs, the object map and the second header. DWG is **read** in
  two parts. 3D: Chudware's own reader (`core/doc/dwg_native.cpp`, from the public spec)
  opens the R2004-family container of AC1027/AC1032 (2013–2018+) files, decompresses the
  `AcDb:AcDsPrototype_1b` data storage and returns each ACIS entity's SAB by handle with its
  type, owner block record and layer (LibreDWG drops this data and the SURFACE classes);
  older versions keep SAT inline. 2D: LibreDWG run as a subprocess (GPL-3.0: never linked,
  bundled or redistributed; `contracts/licenses.md`), found on `PATH`, else in the per-user
  managed install that `python3 scripts/provision_libredwg.py` builds from the SHA-pinned
  upstream release (`services/formats/libredwg.py` / `core/doc/dwg.cpp` resolve both in the
  same order): `dwg2dxf` converts to a private temp `.dxf` (literal argv, no shell) that the
  native DXF reader below imports, with the native ACIS data merged in by handle; when it is
  installed it also re-reads every written DWG, which must give every entity back. Without
  `dwg2dxf` a DWG still imports its model-space solids/surfaces, with a warning naming the
  provisioning script for the 2D entities. The Python helper `services/formats/formats_dwg.py`
  (parity oracle) prefers the ODA File Converter via `ezdxf.addons.odafc` before
  `dwg2dxf`; the C++ production path does not. The ODA-membership decision for full DWG
  fidelity is still open in `work/backlog/T-BL02.md`.

## 2. DXF — Drawing Exchange Format

- **Owner/origin:** Autodesk, **officially published**.
- **Authoritative public spec:** Autodesk's **DXF Reference** (public PDF, updated
  per release). A fully open spec — the cleanest possible source, and the
  human-readable mirror of the DWG object model (see `cad-data-model.md`).
- **Structure:** ASCII (or binary) tagged data as (group-code, value) pairs, in
  sections: `HEADER` (drawing variables), `CLASSES`, `TABLES` (symbol tables:
  layers, linetypes, text styles, views, UCS, viewports, dimstyles, block records),
  `BLOCKS`, `ENTITIES`, `OBJECTS` (dictionaries, non-graphical objects), `THUMBNAIL`.
- **This repo:** DXF is **imported and exported** natively (no ezdxf, no third-party DXF
  library; `core/doc/`). Import: a hand-written ASCII tag parser plus OCCT edge
  builders; honours `$INSUNITS` (scaled to mm) and entity extrusion (OCS), expands INSERT
  block references, converts LINE / CIRCLE / ARC / LWPOLYLINE / POLYLINE (bulges → arcs) /
  SPLINE / ELLIPSE to edges (closed wires become planar faces, open wires stay reference
  wires), imports 3DFACE / POLYFACE meshes as mesh bodies grouped per layer, and imports
  ACIS 3DSOLID / BODY / REGION / SURFACE entities as exact B-rep bodies — inline SAT (R2000–
  R2010, decrypted 1/3 groups) or SAB in the R2013+ `ACDSDATA` section — through the native
  ACIS reader (`core/doc/acis_read.cpp`: SAT 1.x–2xxxx and SAB/ASM binary; analytic, NURBS and
  procedural surfaces via their stored approximations or rebuilt rolling-ball blends). Each
  ACIS entity it cannot read is named by type and handle in the import warnings. Text,
  dimensions and hatches are skipped. The drawing units are recorded on the import feature
  (`source_units`), and `export_file` dxf/dwg/sat default back to them.
  Export: `export_file` format `dxf` writes R12 ASCII for 2D-only content and AutoCAD R2000
  (AC1015, full tables/blocks/objects) when there are solids — solids as 3DSOLID and open
  shells as BODY with ACIS 4.00 SAT (`core/doc/acis_write.cpp`), 2D bodies as LINE / ARC /
  CIRCLE / POLYLINE; every export is re-read and must give back the same solids and volume.
  The 2D drawing / flat-pattern exporters write layered R12 sheets. The Python helper
  `services/formats/formats_dxf.py` (ezdxf, MIT) is an import-only parity oracle.
- **ACIS SAT / SAB** (`.sat`, `.sab`): imported with the same reader (header units applied);
  `export_file` format `sat` writes ACIS 7.0 text, one body per document body, which AutoCAD
  `ACISIN`/`IMPORT`, Inventor and SpaceClaim read.

## 3. DGN — MicroStation design file

- **Owner/origin:** Bentley Systems.
- **Authoritative public spec:** Bentley's published DGN/V8 documentation and the
  openly documented element structure (V7 element format is widely publicly
  documented; V8 is TCB + element records).
- **Structure:** sequence of variable-length **elements** (each with a type +
  level + range + geometry payload); a Terminal Control Block (TCB) holds
  file-wide settings; levels are DGN's analog of layers.
- **This repo:** not on the current interop surface; reference-only.

## 4. DWF / DWFx — Design Web Format

- **Owner/origin:** Autodesk.
- **Authoritative public spec:** the published **DWF spec**; **DWFx** is an
  OPC/XPS package (ECMA-376 OPC container + XPS fixed-page markup), both public.
- **Structure:** ZIP/OPC package of sections (2D graphics as W2D opcode streams,
  3D as graphics resources), metadata, and manifest.
- **This repo:** not on the current interop surface; reference-only.

## 5. IFC — Industry Foundation Classes (BIM)

- **Owner/origin:** buildingSMART, an **open ISO standard**.
- **Authoritative public spec:** **ISO 16739** + buildingSMART's freely published
  IFC EXPRESS schema and documentation.
- **Structure:** STEP physical file (ISO 10303-21) encoding of the IFC EXPRESS
  schema — an object graph of building elements (`IfcWall`, `IfcSpace`, …) with
  relationships, property sets, and geometry (BREP / swept solids / CSG).
- **This repo:** not on the current interop surface (Chudware is mechanical CAD,
  not BIM); reference-only. If BIM interop is ever wanted, `IfcOpenShell` (LGPL) is
  the open-source route (see `building-format-support.md`).

## 6. STEP — ISO 10303 product data

- **Owner/origin:** **ISO**, fully open.
- **Authoritative public spec:** **ISO 10303** (Part 21 physical file, Part 42
  geometry, and APs like AP203/AP214/AP242). EXPRESS schemas are public.
- **Structure:** `DATA` section of `#id = ENTITY(args);` records; geometry via
  BREP topology (manifold solids, faces on surfaces, edges on curves).
- **This repo:** **STEP is a first-class, bidirectional format** — production
  read/write is OCCT `STEPControl_Reader`/`STEPControl_Writer` in `core/doc/formats.cpp`
  (the Python `services/formats/formats.py` reaches the same OCCT readers/writers via
  build123d as the parity oracle). The `.step+` native container contract
  (`contracts/stepplus.md`) embeds an AP214 `geometry.step` snapshot alongside the
  full parametric document.

## 7. IGES — Initial Graphics Exchange Specification

- **Owner/origin:** US PRO / ANSI, **public**.
- **Authoritative public spec:** **IGES 5.3** (publicly available).
- **Structure:** fixed 80-column records in five sections (Start, Global,
  Directory Entry, Parameter Data, Terminate); entities referenced by DE pointer.
- **This repo:** **implemented, import + export** — OCCT `IGESControl_Reader`/
  `IGESControl_Writer` in `core/doc/formats.cpp`. Import sews the face soup into
  shells and heals it; export sews bodies into a solid and writes B-rep mode so the
  file re-sews on re-import.

## 8. PRC — Product Representation Compact (3D in PDF)

- **Owner/origin:** **ISO 14739**, public.
- **Authoritative public spec:** **ISO 14739-1**.
- **Structure:** compressed 3D scene graph (BREP + tessellation + PMI), typically
  embedded in PDF (ISO 32000) 3D annotations.
- **This repo:** not on the current interop surface; reference-only.

## 9. LandXML — civil/survey data

- **Owner/origin:** LandXML.org, **open XML schema**.
- **Authoritative public spec:** the public **LandXML XSD**.
- **Structure:** XML: `Surfaces` (TIN Pnts/Faces), `Alignments`, `Parcels`,
  `CgPoints`, `Roadways`, survey data — all schema-validated.
- **This repo:** not on the current interop surface; reference-only.

## 10. STL / OBJ / Collada / glTF / Three.js JSON / SVG — mesh & web exchange

- All **public**: STL (de-facto public), OBJ (Wavefront, public), Collada
  (`.dae`, Khronos open), **glTF** (Khronos open spec), Three.js JSON (open),
  SVG (W3C). Easiest format family to implement — no proprietary encoding at all.
- **This repo:** **STL, OBJ, PLY, glTF/GLB, and 3MF are implemented** for import
  and export (mesh lane, `core/doc/formats.cpp`: OCCT `RWStl`/`RWObj`/`RWGltf` readers,
  `StlAPI_Writer`/`RWGltf_CafWriter`, in-file OBJ/PLY writers, and lib3mf for 3MF when
  the core is built with it; the Python oracle in `services/formats/formats.py` uses
  trimesh/lib3mf). Rhino `.3dm` is also on the
  mesh-level surface (openNURBS in C++, guarded by `CHUDWARE_WITH_OPENNURBS`;
  rhino3dm in the Python oracle). Collada/Three.js JSON/SVG are not on the current
  surface; reference-only.

## 11. PDF (Publish / export) — ISO 32000

- **Authoritative public spec:** **ISO 32000** (PDF), plus PDF/A, and the 3D-PDF
  path via PRC (ISO 14739). Fully open.
- **This repo:** used for **deliverables** (drawing/report PDF export via
  `reportlab` / `pypdfium2`, see `contracts/licenses.md`), not as a 3D-geometry
  interop format. 3D-PDF/PRC embedding is reference-only.

## 12. Point clouds

- **Formats:** LAS/LAZ (ASPRS public spec), E57 (ASTM E2807, public), PTS/PTX.
- **Authoritative public spec:** **ASPRS LAS**, **ASTM E57** — both public.
- **This repo:** not on the current interop surface; reference-only.

---

## Takeaway

Every format above has a **first-party or ISO public specification**. The
independent-implementation path is the same one Chudware already follows for
STEP/IGES/DXF/mesh formats: cite and build from the public spec (or an
appropriately-licensed open-source library — see `building-format-support.md`),
never from a proprietary binary. See `docs/reference/README.md` for how this
survey maps onto the production format matrix (`format_registry` in
`core/doc/formats.cpp`).
