# The CAD Data Model (DWG/DXF-family)

> **Provenance:** folded in from the separate clean-room research project "Clean Engine"
> (its CAD data-model note). This describes the object model
> shared by AutoCAD-lineage tools, as **publicly documented** by the DXF Reference
> (DXF is the readable serialization of exactly these objects) and mirrored by the
> Open Design Alliance's public `Db*/Ge/Gi/Gs` API naming convention. Nothing here
> comes from proprietary internals — it's the standard model in the DXF spec, the
> ODA public .dwg spec, and vendor developer docs. See `docs/reference/README.md`
> for how this cross-links to Chudware's own document model.

## The four subsystems (public naming convention)

| Prefix | Subsystem | Responsibility |
|--------|-----------|----------------|
| `Db`   | Database  | Persistent document model: the objects/entities that get saved to DWG/DXF and their containers. |
| `Ge`   | Geometry  | Math primitives — points, vectors, matrices, curves, surfaces. Pure geometry, no persistence. |
| `Gi`   | Graphics Interface | How an object *describes itself for drawing* (emits geometry primitives to a renderer). |
| `Gs`   | Graphics System | The display pipeline — viewports, view transforms, and rendering to a device. |

Separation of concerns: **Db** = what's stored, **Ge** = math, **Gi** = "draw me",
**Gs** = "put pixels on screen". This is a clean architectural reference point —
not a prescription that Chudware's own document model (`contracts/doc_schema.md`,
`contracts/doc_ops.md`) must mirror it, though the separation of concerns rhymes:
Chudware's own document/geometry/tessellation split (`.step+` document JSON →
OCCT B-rep rebuilt by the C++ core → AICM tessellation, see `contracts/stepplus.md` and
`contracts/mesh_format.md`) follows the same logic for different reasons (a
parametric feature-history CAD kernel rather than a drawing-database editor).

## The database (Db) structure

A DWG/DXF drawing database is a graph of **handle-identified objects**:

```
Database
├── Header variables        (units, extents, current layer, versions, …)
├── Symbol Tables           (named, fixed set of record types)
│   ├── Block table         → BlockTableRecords (ModelSpace, PaperSpace, blocks)
│   ├── Layer table         → LayerTableRecords
│   ├── Linetype table      → LinetypeTableRecords
│   ├── TextStyle table     → TextStyleRecords
│   ├── DimStyle, View, UCS, Viewport, RegApp tables
├── Named Object Dictionary (extensible key→object maps: groups, layouts,
│                            materials, plot settings, xrecords, …)
└── Entities                (live inside Block records; the drawable content)
```

Key ideas (all from the public DXF/DWG specs):

- **Handles**: every object has a unique, stable 64-bit handle. References between
  objects are by handle. This is the backbone of the file's object map.
- **Symbol tables**: a *fixed* set of tables for named resources. Entities point at
  these by handle (e.g., an entity's layer is a handle into the Layer table).
- **Block table records** are containers of entities. `*Model_Space` and
  `*Paper_Space` are special block records; user blocks enable instancing via
  INSERT entities.
- **Dictionaries**: the extensible, general-purpose key→value object store for
  everything that isn't a fixed symbol table (layouts, groups, dictionaries of
  materials, custom app data, etc.).
- **Objects vs. Entities**: *entities* are drawable (LINE, CIRCLE, ARC, LWPOLYLINE,
  TEXT, MTEXT, INSERT, HATCH, DIMENSION, SPLINE, 3DSOLID, …). Non-graphical
  *objects* (dictionaries, layouts, groups, xrecords) hold data but don't draw.

## Common entities (from the public DXF entity reference)

- **Curves:** LINE, RAY, XLINE, CIRCLE, ARC, ELLIPSE, LWPOLYLINE, POLYLINE, SPLINE.
- **Annotation:** TEXT, MTEXT, DIMENSION (+ dimension styles), LEADER, MLEADER,
  TABLE.
- **Fills/regions:** HATCH (boundary loops + pattern), SOLID, REGION.
- **References:** INSERT (block instance with scale/rotation), attributes.
- **3D:** 3DFACE, MESH, SURFACE, 3DSOLID (ACIS/BREP payload), BODY.
- **Raster/underlay:** IMAGE, PDFUNDERLAY, DWFUNDERLAY, DGNUNDERLAY.

Each entity carries: owner handle, layer, linetype, color, lineweight,
transparency, and its geometry-defining group codes (documented per-entity in the
DXF reference).

**Relevance to this repo's DXF importer:** the production importer in
`core/doc/formats.cpp` is a hand-written ASCII DXF tag parser (no ezdxf, no
third-party DXF library). It reads the `ENTITIES` section, honours `$INSUNITS`, and
converts a subset of this entity catalog — LINE, CIRCLE, ARC, LWPOLYLINE/POLYLINE
(bulge → true arc), SPLINE and ELLIPSE — into OCCT edges, then planar faces (closed
wires) or reference wires (open). INSERT, TEXT/MTEXT, DIMENSION, HATCH, POINT and 3D
mesh entities are skipped and counted, not converted. The Python helper
`services/formats/formats_dxf.py` (ezdxf, MIT) is the parity oracle and additionally
explodes INSERT block references; production does not. See `cad-file-formats.md` §2
for the format-level summary and `README.md` for planned format work.

## Geometry (Ge) — the math layer

Standard computational-geometry primitives, independent of persistence:

- Points/vectors (2D/3D), matrices (3D transforms), quaternions.
- Curves: line, ray, circular/elliptical arc, NURBS curve, polyline.
- Surfaces: plane, cone, sphere, torus, NURBS surface.
- Operations: intersection, projection, parameterization, bounding volumes.

This is textbook CAGD (Computer-Aided Geometric Design). Sources: any NURBS/CAGD
text (e.g., Piegl & Tiller, *The NURBS Book*); no vendor code required. Chudware's
own geometry layer is OpenCASCADE (OCCT, LGPL-2.1 per `contracts/licenses.md`),
used directly by the C++ core (`core/geom/`, `core/doc/`) and through
`cadquery-ocp`/`build123d` by the Python oracle — it already implements this layer;
this section is background for understanding *why* it's shaped the way it is, not a
build target.

## Graphics (Gi → Gs) — the render pipeline

- **Gi (worldDraw/viewportDraw)**: an entity implements a "draw myself" method that
  emits primitives (polylines, meshes, text, shells) to an abstract graphics
  *interface* — decoupling *what* to draw from *how/where*.
- **Gs**: manages **views/viewports**, the world→screen transform, visual styles,
  and hands primitives to a concrete device (OpenGL/DirectX/Vulkan/software/PDF).

Clean-room design lesson: keep entities emitting abstract primitives (a `Gi`-style
boundary), and swap render backends behind a `Gs`-style boundary. This is standard
retained/immediate-mode graphics architecture, widely documented. Chudware's own
AICM tessellation format (`contracts/mesh_format.md`) plays a comparable role: a
single binary format the C++ host renders from (B-rep tessellation + picking
metadata + scalar/displacement overlays), decoupling entity geometry from the
render backend.

## Reading order for interop work

1. Read `cad-file-formats.md` for the per-format survey and public specs.
2. Read this file for the shared DWG/DXF-family object model (handle graph,
   symbol tables, entity catalog) that any DXF/DWG reader has to reconstruct.
3. Read `building-format-support.md` for license-aware implementation guidance
   per format and the recommended layering.
