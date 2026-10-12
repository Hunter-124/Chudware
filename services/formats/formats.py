"""Python geometry import/export helpers for ECAD and other service callers.

Formats supported
-----------------
* **STEP** (AP214) — via ``build123d.export_step`` / ``import_step``.
* **IGES** — via ``OCP.IGESControl`` (OCP IGES writer/reader; reader result is face-soup,
  re-sewn to a shell and volume is computed from the closed shell).
* **BREP** — via ``build123d.export_brep`` / ``import_brep``.
* **STL** — via ``build123d.export_stl`` / ``import_stl`` (mesh only; no parametrics).
* **3MF** — via ``build123d.Mesher`` / ``lib3mf`` (mesh only).
* **DXF** — via ``ezdxf`` (``services.formats.formats_dxf``); 2D vector drawings → planar B-rep
  wires/faces. See that module for the entity coverage matrix. This helper only reads DXF;
  DXF writing is the native engine's ``export_file`` (format ``dxf``).
* **DWG** — via an external DWG→DXF converter + the DXF pipeline (``services.formats.formats_dwg``;
  ODA File Converter, or LibreDWG ``dwg2dxf`` on PATH / provisioned by
  ``scripts/provision_libredwg.py``). This helper only reads DWG; DWG writing is the native
  engine's ``export_file`` (format ``dwg``, Chudware's own R2000 encoder).

Public API
----------
``export_document(session_bodies, path, format)``
    Export a list of ``(name, build123d_shape)`` bodies to *path* in the requested format.

``import_path(path, format=None) -> list[(name, shape)]``
    Import geometry from *path*, auto-detecting format when omitted.  Returns a list of
    ``(name, shape)`` pairs.  Names come from STEP product labels when available, fall back to
    "Body1", "Body2", …

Native document RPCs
-------------------
``doc_formats``, ``import_file`` and ``export_file`` run in the C++ core;
``core/doc/formats.cpp`` owns the format matrix and native document I/O.
The Python ``import_path()`` / ``export_document()`` helpers above operate on
Python shapes and do not register document RPC handlers.
"""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path
from typing import Any, Callable

# ---- lazy imports to keep this module OCP-free at import time ----------------
# (imported inside functions as needed)


# --------------------------------------------------------------------------- helpers

_EXT_TO_FORMAT: dict[str, str] = {
    ".step": "step",
    ".stp": "step",
    ".igs": "iges",
    ".iges": "iges",
    ".brep": "brep",
    ".stl": "stl",
    ".3mf": "3mf",
    ".obj": "obj",
    ".ply": "ply",
    ".gltf": "gltf",
    ".glb": "glb",
    ".3dm": "3dm",
    ".dxf": "dxf",
    ".dwg": "dwg",
    ".sldprt": "sldprt",
    ".sldasm": "sldasm",
    ".x_t": "x_t",
    ".x_b": "x_b",
}


# --------------------------------------------------------------------------- format registry (D4)
# Single source of truth for the format matrix. The host derives its Open/Import/Export filters
# from this via the ``doc_formats`` RPC, so the UI never drifts from what the engine can do.
#   lane:  "brep" (OCCT solids) | "mesh" (triangle soup → MeshBody) | "native" (.step+)
class FormatInfo:
    __slots__ = ("fmt", "label", "exts", "lane", "can_import", "can_export")

    def __init__(self, fmt, label, exts, lane, can_import=True, can_export=True):
        self.fmt = fmt
        self.label = label
        self.exts = exts            # list of extensions incl. leading dot, primary first
        self.lane = lane
        self.can_import = can_import
        self.can_export = can_export

    def as_dict(self) -> dict:
        return {"fmt": self.fmt, "label": self.label, "exts": list(self.exts), "lane": self.lane,
                "can_import": self.can_import, "can_export": self.can_export}


# FORMAT_REGISTRY removed 2026-07-22 (T-RF19) — C++ owns the matrix
# (core/doc/formats.cpp). _detect_format/_EXT_TO_FORMAT stay for the import/export helpers.


def list_formats() -> list[dict]:
    """The import/export format matrix, from the C++ core's ``doc_formats`` RPC."""
    from services.mcp import core_bridge as _cb
    return list(_cb.call("doc_formats", {}).get("formats", []))


def _detect_format(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lower()
    fmt = _EXT_TO_FORMAT.get(ext)
    if fmt is None:
        raise ValueError(f"cannot detect format from extension '{ext}' for {path!r}")
    return fmt


def _shapes_to_export_list(session_bodies: list[tuple[str, Any]]) -> Any:
    """Merge several named shapes into a single build123d Compound for export."""
    from build123d import Compound
    shapes = [s for _, s in session_bodies]
    if not shapes:
        raise ValueError("no bodies to export")
    if len(shapes) == 1:
        return shapes[0]
    return Compound(children=list(shapes))


def _is_solid_shape(shape) -> bool:
    """True iff *shape* contains at least one SOLID — the 3MF/lib3mf mesher needs a closed solid.

    Open surface bodies (lone FACE / open SHELL) contain no SOLID and return False. Robust to OCP
    errors (defaults to True so a non-surface body is never wrongly skipped)."""
    raw = shape.wrapped if hasattr(shape, "wrapped") else shape
    try:
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedMapOfShape
        solids = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(raw, TopAbs_SOLID, solids)
        return solids.Extent() > 0
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------- export


def export_document(
    session_bodies: list[tuple[str, Any]],
    path: str | os.PathLike,
    format: str | None = None,  # noqa: A002
) -> str:
    """Export *session_bodies* (list of ``(name, shape)``) to *path*.

    Parameters
    ----------
    session_bodies:
        List of ``(body_name, build123d_shape)`` produced by the compiler layer.
    path:
        Destination file path.
    format:
        One of ``"step"`` / ``"iges"`` / ``"brep"`` / ``"stl"`` / ``"3mf"``.  Detected from
        the file extension when ``None``.

    Returns
    -------
    str
        The resolved absolute path that was written.
    """
    from services.formats.mesh_body import MeshBody

    path = Path(path)
    fmt = format or _detect_format(path)
    fmt = fmt.lower()
    path.parent.mkdir(parents=True, exist_ok=True)

    has_mesh = any(isinstance(s, MeshBody) for _, s in session_bodies)

    # --- B-rep formats (OCCT) — cannot carry a topology-free mesh body ----------------------
    if fmt in ("step", "iges", "brep"):
        if has_mesh:
            raise ValueError(
                f"a mesh body (imported STL/OBJ/PLY/3MF/glTF) cannot be exported to {fmt.upper()} — "
                "it has no B-rep topology. Export STL, OBJ, PLY, 3MF, or glTF instead.")
        shape = _shapes_to_export_list(session_bodies)
        if fmt == "step":
            from build123d import export_step
            export_step(shape, str(path))
        elif fmt == "iges":
            from OCP.IGESControl import IGESControl_Writer
            w = IGESControl_Writer()
            w.AddShape(shape.wrapped)
            w.ComputeModel()
            if not w.Write(str(path)):
                raise RuntimeError("IGES writer returned failure")
        else:  # brep
            from build123d import export_brep
            export_brep(shape, str(path))
        return str(path)

    # --- mesh formats — MeshBody goes straight out; B-rep solids get tessellated -----------
    if fmt in ("stl", "obj", "ply", "gltf", "glb"):
        # Pure-solid STL keeps the proven build123d path (byte-identical to before); any mesh body
        # (or the F1 mesh-only formats) route through the unified trimesh writer.
        if fmt == "stl" and not has_mesh:
            from build123d import export_stl
            export_stl(_shapes_to_export_list(session_bodies), str(path))
            return str(path)
        return _export_mesh_trimesh(session_bodies, path, fmt)

    if fmt == "3mf":
        if not has_mesh:
            from build123d import Mesher
            m = Mesher()
            added = 0
            for _name, s in session_bodies:
                # 3MF (via lib3mf) is a solid-mesh format — an open surface body has no enclosed
                # volume to mesh, so skip it rather than emit a degenerate mesh.
                if not _is_solid_shape(s):
                    continue
                m.add_shape(s)
                added += 1
            if added == 0:
                raise ValueError(
                    "3MF export needs at least one solid body; all bodies are open surfaces "
                    "(export STEP/IGES/STL for surface bodies)")
            m.write(str(path))
            return str(path)
        return _export_3mf_lib3mf(session_bodies, path)

    if fmt == "3dm":
        return _export_3dm(session_bodies, path)

    if fmt in ("dxf", "dwg"):
        raise ValueError(
            f"this Python helper does not write {fmt.upper()}. {fmt.upper()} export runs in the "
            f"native engine: the export_file RPC with format '{fmt}' (MCP export_part "
            f"fmt=\"{fmt}\"; File > Export in the app).")

    if fmt in ("sldprt", "sldasm", "x_t", "x_b"):
        raise ValueError(
            f"writing {fmt.upper()} is not supported (proprietary closed format). Export STEP (.step) "
            "instead — SolidWorks / NX / Parasolid tools import STEP losslessly.")

    raise ValueError(f"unsupported export format '{fmt}'")


# --------------------------------------------------------------------------- import


def import_path(
    path: str | os.PathLike,
    format: str | None = None,  # noqa: A002
) -> list[tuple[str, Any]]:
    """Import geometry from *path* → list of ``(name, shape)`` pairs.

    STEP files yield product names when the XCAF label carries a non-trivial name; all others
    return generic "Body1", "Body2", … names.

    Returns
    -------
    list of (name, shape)
        *shape* is always a build123d Shape subclass with a ``.volume`` attribute (Solid,
        Compound, or Shell for IGES).  STL imports are Face objects (mesh surface only — volume
        is not meaningful).
    """
    path = Path(path)
    fmt = format or _detect_format(path)
    fmt = fmt.lower()

    if fmt == "step":
        return _import_step(path)
    elif fmt == "iges":
        return _import_iges(path)
    elif fmt == "brep":
        return _import_brep(path)
    elif fmt == "stl":
        return _import_stl(path)
    elif fmt == "3mf":
        return _import_3mf(path)
    elif fmt in ("obj", "ply", "gltf", "glb"):
        return _load_mesh_trimesh(path, fmt)
    elif fmt == "3dm":
        return _import_3dm(path)
    elif fmt == "dxf":
        from services.formats.formats_dxf import import_dxf
        return import_dxf(path)
    elif fmt == "dwg":
        from services.formats.formats_dwg import import_dwg
        return import_dwg(path)
    elif fmt in ("sldprt", "sldasm"):
        from services.formats.formats_solidworks import import_solidworks
        return import_solidworks(path)
    elif fmt in ("x_t", "x_b"):
        from services.formats.formats_solidworks import import_parasolid
        return import_parasolid(path)
    else:
        raise ValueError(f"unsupported import format '{fmt}'")


# -- per-format importers ------------------------------------------------------


def _import_step(path: Path) -> list[tuple[str, Any]]:
    from build123d import import_step
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDF import TDF_LabelSequence
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    # Build123d import for the actual shapes
    shape = import_step(str(path))

    # XDE pass to extract product names
    names: list[str] = []
    try:
        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
        app.NewDocument(TCollection_ExtendedString("XmlXCAF"), doc)
        reader = STEPCAFControl_Reader()
        reader.ReadFile(str(path))
        reader.Transfer(doc)
        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        labels = TDF_LabelSequence()
        tool.GetFreeShapes(labels)
        for i in range(1, labels.Size() + 1):
            lbl = labels.Value(i)
            attr = TDataStd_Name()
            if lbl.FindAttribute(TDataStd_Name.GetID_s(), attr):
                raw_name = attr.Get().ToExtString()
                # Filter out generic "COMPOUND"/"SOLID"/"SHAPE" placeholders
                if raw_name and raw_name.upper() not in ("COMPOUND", "SOLID", "SHAPE", ""):
                    names.append(raw_name)
    except Exception:  # noqa: BLE001
        pass

    # Normalise to a list of shapes. A multi-solid STEP comes back as a single Compound; split it
    # into one body PER SOLID (SolidWorks-parity multi-body import) and align XDE product names by
    # index. A single-solid STEP keeps the whole shape as one body (byte-identical to before).
    if hasattr(shape, "wrapped"):
        solids = []
        try:
            solids = list(shape.solids())
        except Exception:  # noqa: BLE001
            solids = []
        shapes = solids if len(solids) > 1 else [shape]
    elif hasattr(shape, "__iter__"):
        shapes = list(shape)
    else:
        shapes = [shape]

    result: list[tuple[str, Any]] = []
    for i, s in enumerate(shapes, start=1):
        name = names[i - 1] if i - 1 < len(names) else f"Body{i}"
        result.append((name, s))
    return result


def _import_iges(path: Path) -> list[tuple[str, Any]]:
    from OCP.IGESControl import IGESControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.ShapeFix import ShapeFix_Shape
    from build123d import Shape, Shell, Solid, Compound

    reader = IGESControl_Reader()
    status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"IGES reader failed with status {status}")
    roots = reader.NbRootsForTransfer()
    transferred = reader.TransferRoots()
    if transferred != roots:
        if transferred == 0:
            raise RuntimeError(f"IGES reader transferred 0 of {roots} roots from '{path}'")
        warnings.warn(
            f"{transferred} of {roots} IGES roots translated from '{path}'",
            RuntimeWarning, stacklevel=2)

    result: list[tuple[str, Any]] = []
    for i in range(1, reader.NbShapes() + 1):
        raw = reader.Shape(i)
        # IGES comes as a face-soup; sew into a closed shell, then fix.
        sewing = BRepBuilderAPI_Sewing(0.01)
        sewing.Add(raw)
        sewing.Perform()
        sewn = sewing.SewedShape()
        fixer = ShapeFix_Shape(sewn)
        fixer.Perform()
        fixed = fixer.Shape()
        # Wrap with the most appropriate build123d carrier
        wrapped: Any = None
        for cls in (Solid, Compound, Shell):
            wrapped = cls.cast(fixed)
            if wrapped is not None:
                break
        if wrapped is None:
            wrapped = Shape.cast(fixed)
        result.append((f"Body{i}", wrapped))
    return result


def _import_brep(path: Path) -> list[tuple[str, Any]]:
    from build123d import import_brep
    shape = import_brep(str(path))
    return [("Body1", shape)]


def _import_stl(path: Path) -> list[tuple[str, Any]]:
    """STL → one MeshBody (triangle soup). STL has no topology, so it is never a B-rep solid."""
    return _load_mesh_trimesh(path, "stl")


def _import_3mf(path: Path) -> list[tuple[str, Any]]:
    """3MF → one MeshBody per mesh object via **lib3mf** (BSD, already bundled).

    lib3mf is purpose-built for 3MF and needs no scene-graph deps (trimesh's 3MF path pulls in
    networkx + lxml). Build-item transforms are not yet applied (single-object 3MF is the common
    case); F1 adds transform + color/material fidelity."""
    import lib3mf
    import numpy as np

    from services.formats.mesh_body import MeshBody

    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    reader = model.QueryReader("3mf")
    reader.ReadFromFile(str(path))
    unit_to_mm = {
        lib3mf.ModelUnit.MicroMeter: 0.001,
        lib3mf.ModelUnit.MilliMeter: 1.0,
        lib3mf.ModelUnit.CentiMeter: 10.0,
        lib3mf.ModelUnit.Inch: 25.4,
        lib3mf.ModelUnit.Foot: 304.8,
        lib3mf.ModelUnit.Meter: 1000.0,
    }.get(model.GetUnit())
    if unit_to_mm is None:
        raise ValueError(f"3MF '{path}' has an unsupported model length unit")

    result: list[tuple[str, Any]] = []
    mesh_iter = model.GetMeshObjects()
    idx = 0
    while mesh_iter.MoveNext():
        idx += 1
        mesh = mesh_iter.GetCurrentMeshObject()
        if mesh.GetTriangleCount() == 0:
            continue
        gv = mesh.GetVertices()
        gi = mesh.GetTriangleIndices()
        verts = np.array([[v.Coordinates[0], v.Coordinates[1], v.Coordinates[2]] for v in gv],
                         dtype=np.float64)
        if unit_to_mm != 1.0:
            verts *= unit_to_mm
        verts = verts.astype(np.float32)
        tris = np.array([[t.Indices[0], t.Indices[1], t.Indices[2]] for t in gi], dtype=np.uint32)
        name = mesh.GetName() or f"{Path(path).stem}_{idx}"
        result.append((name, MeshBody(verts=verts, tris=tris, name=name)))

    if not result:
        raise ValueError(f"no mesh objects found in 3MF '{path}'")
    return result


def _export_3mf_lib3mf(session_bodies: list[tuple[str, Any]], path: Path) -> str:
    """Write bodies to 3MF via lib3mf (BSD). MeshBody goes out directly; a B-rep solid is
    tessellated first. One 3MF mesh object + build item per body."""
    import lib3mf
    import numpy as np

    from services.formats.mesh_body import MeshBody

    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    identity = wrapper.GetIdentityTransform()
    added = 0
    for name, s in session_bodies:
        if isinstance(s, MeshBody):
            verts, tris = np.asarray(s.verts), np.asarray(s.tris)
        else:
            verts, tris = _shape_to_triangles(s)
        if len(tris) == 0:
            continue
        mo = model.AddMeshObject()
        mo.SetName(str(name))
        ps = []
        for v in np.asarray(verts, dtype=np.float64):
            p = lib3mf.Position()
            p.Coordinates = (float(v[0]), float(v[1]), float(v[2]))
            ps.append(p)
        ts = []
        for t in np.asarray(tris, dtype=np.int64):
            tri = lib3mf.Triangle()
            tri.Indices = (int(t[0]), int(t[1]), int(t[2]))
            ts.append(tri)
        mo.SetGeometry(ps, ts)
        model.AddBuildItem(mo, identity)
        added += 1
    if added == 0:
        raise ValueError("3MF export: no mesh geometry to write")
    model.QueryWriter("3mf").WriteToFile(str(path))
    return str(path)


# -- unified mesh lane (trimesh) ----------------------------------------------
# All mesh formats (STL / OBJ / PLY / 3MF / glTF) share one trimesh-backed reader → MeshBody and
# one trimesh-backed writer. trimesh (MIT, already a dependency) reads/writes every one of these
# robustly; the resulting MeshBody re-exports through the Python format helpers.


def _trimesh_color(geom) -> tuple | None:
    """Best-effort representative sRGB triple in [0,1] from a trimesh geometry's visual, else None."""
    try:
        vis = getattr(geom, "visual", None)
        main = getattr(vis, "main_color", None)
        if main is not None and len(main) >= 3:
            return (float(main[0]) / 255.0, float(main[1]) / 255.0, float(main[2]) / 255.0)
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_mesh_trimesh(path: Path, fmt: str | None = None) -> list[tuple[str, Any]]:
    """Load a mesh file via trimesh → ``list[(name, MeshBody)]``.

    Handles a single ``Trimesh`` and a multi-geometry ``Scene`` (OBJ groups, 3MF build items,
    glTF nodes) — for a Scene the node transforms are baked so every body is world-placed."""
    import numpy as np
    import trimesh

    from services.formats.mesh_body import MeshBody

    stem = Path(path).stem
    loaded = trimesh.load(str(path), force=None)
    result: list[tuple[str, Any]] = []

    def _add(geom, name: str) -> None:
        faces = getattr(geom, "faces", None)
        if faces is None or len(faces) == 0:
            return
        verts = np.asarray(geom.vertices, dtype=np.float32)
        tris = np.asarray(geom.faces, dtype=np.uint32)
        result.append((name, MeshBody(verts=verts, tris=tris, name=name,
                                      color=_trimesh_color(geom))))

    if isinstance(loaded, trimesh.Scene):
        geoms = loaded.dump(concatenate=False)  # node transforms baked → world space
        for i, geom in enumerate(geoms, start=1):
            gname = None
            try:
                gname = geom.metadata.get("name")
            except Exception:  # noqa: BLE001
                gname = None
            _add(geom, gname or f"{stem}_{i}")
    elif isinstance(loaded, trimesh.Trimesh):
        _add(loaded, stem)

    if not result:
        raise ValueError(f"no triangle mesh found in '{path}' ({fmt or 'mesh'})")
    return result


# trimesh export file_type per format key.
_TRIMESH_EXPORT_TYPE: dict[str, str] = {
    "stl": "stl", "3mf": "3mf", "obj": "obj", "ply": "ply", "gltf": "gltf", "glb": "glb",
}


def _shape_to_triangles(shape):
    """Tessellate a build123d / OCCT shape to ``(verts (N,3), tris (M,3))`` for mesh export."""
    from services.sim import tessellate as tess
    bt = tess.tessellate_body(shape, body_id=0, name="x", color=(0.5, 0.5, 0.5))
    return bt.pos, bt.idx


def _export_mesh_trimesh(session_bodies: list[tuple[str, Any]], path: Path, fmt: str) -> str:
    """Write all bodies (MeshBody or B-rep solid) to a mesh format via trimesh."""
    import numpy as np
    import trimesh

    from services.formats.mesh_body import MeshBody

    meshes = []
    for _name, s in session_bodies:
        if isinstance(s, MeshBody):
            v, f = np.asarray(s.verts), np.asarray(s.tris)
        else:
            v, f = _shape_to_triangles(s)  # B-rep solid → triangulated for the mesh format
        if len(f):
            meshes.append(trimesh.Trimesh(vertices=np.asarray(v, dtype=np.float64),
                                          faces=np.asarray(f, dtype=np.int64), process=False))
    if not meshes:
        raise ValueError("no mesh geometry to export")
    combined = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    combined.export(str(path), file_type=_TRIMESH_EXPORT_TYPE.get(fmt, fmt))
    return str(path)


# -- Rhino lane (rhino3dm) -----------------------------------------------------
# .3dm uses the OpenNURBS representation, separate from OCCT. rhino3dm (MIT) reads/writes it but has
# NO mesher, so we operate at the MESH level: import every Mesh / Extrusion-mesh as a MeshBody, and
# export bodies as render meshes. A NURBS/Brep-only .3dm cannot be tessellated by rhino3dm — we say
# so and point the user at STEP. (True NURBS↔OCCT conversion is out of scope.)


def _rhino_mesh_to_arrays(mesh):
    """Convert a rhino3dm.Mesh to ``(verts (N,3) float32, tris (M,3) uint32)``; quads → 2 tris."""
    import numpy as np
    nv = len(mesh.Vertices)
    verts = np.empty((nv, 3), dtype=np.float32)
    for i in range(nv):
        p = mesh.Vertices[i]
        verts[i] = (p.X, p.Y, p.Z)
    tris: list[tuple[int, int, int]] = []
    for fi in range(len(mesh.Faces)):
        f = tuple(mesh.Faces[fi])
        a, b, c = int(f[0]), int(f[1]), int(f[2])
        d = int(f[3]) if len(f) > 3 else c
        tris.append((a, b, c))
        if d != c:  # a real quad → second triangle
            tris.append((a, c, d))
    return verts, np.array(tris, dtype=np.uint32).reshape(-1, 3)


def _import_3dm(path: Path) -> list[tuple[str, Any]]:
    """Rhino .3dm → MeshBody per Mesh / Extrusion object (rhino3dm, MIT)."""
    import rhino3dm as r

    from services.formats.mesh_body import MeshBody

    model = r.File3dm.Read(str(path))
    if model is None:
        raise ValueError(f"could not read Rhino file '{path}'")

    result: list[tuple[str, Any]] = []
    skipped = 0
    for obj in model.Objects:
        g = obj.Geometry
        mesh = None
        if isinstance(g, r.Mesh):
            mesh = g
        elif isinstance(g, r.Extrusion):
            try:
                mesh = g.GetMesh(r.MeshType.Any)
            except Exception:  # noqa: BLE001
                mesh = None
        if mesh is None or len(mesh.Vertices) == 0 or len(mesh.Faces) == 0:
            skipped += 1
            continue
        verts, tris = _rhino_mesh_to_arrays(mesh)
        name = None
        try:
            name = obj.Attributes.Name
        except Exception:  # noqa: BLE001
            name = None
        name = name or f"{Path(path).stem}_{len(result) + 1}"
        result.append((name, MeshBody(verts=verts, tris=tris, name=name)))

    if not result:
        raise ValueError(
            f"'{path}' has no mesh geometry rhino3dm can read ({skipped} NURBS/Brep object(s) "
            "skipped — rhino3dm has no mesher). Re-export from Rhino as STEP, or with render meshes.")
    return result


def _export_3dm(session_bodies: list[tuple[str, Any]], path: Path) -> str:
    """Write bodies to a Rhino .3dm as render meshes (MeshBody direct; B-rep solid tessellated)."""
    import numpy as np
    import rhino3dm as r

    from services.formats.mesh_body import MeshBody

    model = r.File3dm()
    added = 0
    for name, s in session_bodies:
        if isinstance(s, MeshBody):
            verts, tris = np.asarray(s.verts), np.asarray(s.tris)
        else:
            verts, tris = _shape_to_triangles(s)
        if len(tris) == 0:
            continue
        m = r.Mesh()
        for v in np.asarray(verts, dtype=np.float64):
            m.Vertices.Add(float(v[0]), float(v[1]), float(v[2]))
        for t in np.asarray(tris, dtype=np.int64):
            m.Faces.AddFace(int(t[0]), int(t[1]), int(t[2]))
        attr = r.ObjectAttributes()
        attr.Name = str(name)
        model.Objects.AddMesh(m, attr)
        added += 1
    if added == 0:
        raise ValueError("3DM export: no mesh geometry to write")
    if not model.Write(str(path), 0):
        raise RuntimeError("rhino3dm File3dm.Write returned failure")
    return str(path)


# --------------------------------------------------------------------------- compiler cache key


_IMPORT_CACHE: dict[str, list[tuple[str, Any]]] = {}


def _cached_import(path: str, format: str | None) -> list[tuple[str, Any]]:  # noqa: A002
    """Import *path* with a simple mtime-keyed cache so repeated recompiles are cheap."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    key = f"{path}:{mtime}"
    if key not in _IMPORT_CACHE:
        _IMPORT_CACHE[key] = import_path(path, format)
    return _IMPORT_CACHE[key]


# --------------------------------------------------------------------------- native document boundary


EmitFn = Callable[[dict], None]


# Native document import/export and the format matrix live in core/doc/formats.cpp.
# These Python helpers stay closure-free; services.ecad.threed imports import_path.
# _detect_format/_EXT_TO_FORMAT above serve the remaining Python import/export helpers.
