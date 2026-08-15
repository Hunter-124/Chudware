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
- **This repo:** DWG import is **not yet implemented**; Chudware's documented plan
  is DWG via an ODA membership (or a subprocess-isolated LibreDWG conversion —
  LibreDWG is GPL, so it stays out-of-process) — see `docs/design/PLATFORM_ADR.md` §5 and
  `work/backlog/T-BL02.md`. Reference-only until that lands.

## 2. DXF — Drawing Exchange Format

- **Owner/origin:** Autodesk, **officially published**.
- **Authoritative public spec:** Autodesk's **DXF Reference** (public PDF, updated
  per release). A fully open spec — the cleanest possible source, and the
  human-readable mirror of the DWG object model (see `cad-data-model.md`).
- **Structure:** ASCII (or binary) tagged data as (group-code, value) pairs, in
  sections: `HEADER` (drawing variables), `CLASSES`, `TABLES` (symbol tables:
  layers, linetypes, text styles, views, UCS, viewports, dimstyles, block records),
  `BLOCKS`, `ENTITIES`, `OBJECTS` (dictionaries, non-graphical objects), `THUMBNAIL`.
- **This repo:** DXF **import is implemented** — `services/formats/formats.py` +
  `services/formats/formats_dxf.py`, via **ezdxf** (MIT). Modelspace entities
  (LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE/ELLIPSE/SPLINE/INSERT) are converted to
  build123d planar B-rep (closed wires → faces, open wires kept as reference
  wires); text/dimensions/hatches/points/3D-mesh entities are skipped and counted.
  Import-only for now (2D-projection-from-3D DXF export is a separate follow-up).

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
- **This repo:** **STEP (AP214) is a first-class, bidirectional format** — see
  `services/formats/formats.py` (via `build123d.export_step` / `import_step`) and the
  `.step+` native container contract (`contracts/stepplus.md`), which embeds an
  AP214 `geometry.step` snapshot alongside the full parametric document.

## 7. IGES — Initial Graphics Exchange Specification

- **Owner/origin:** US PRO / ANSI, **public**.
- **Authoritative public spec:** **IGES 5.3** (publicly available).
- **Structure:** fixed 80-column records in five sections (Start, Global,
  Directory Entry, Parameter Data, Terminate); entities referenced by DE pointer.
- **This repo:** **implemented** — `services/formats/formats.py` via `OCP.IGESControl`
  (reader result is face-soup, re-sewn to a shell; volume computed from the
  closed shell).

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
- **This repo:** **STL, OBJ, PLY, glTF/GLB, and 3MF are all implemented**
  (mesh lane, `services/formats/formats.py`, via `build123d` mesh export/import,
  `trimesh`, and `lib3mf` — see `contracts/licenses.md`). Collada/Three.js
  JSON/SVG are not on the current surface; reference-only.

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
survey maps onto `services/formats/formats.py`'s actual `FORMAT_REGISTRY`.
