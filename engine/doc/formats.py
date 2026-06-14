"""Export / import surface for all supported geometry formats (M8 / C21).

Formats supported
-----------------
* **STEP** (AP214) — via ``build123d.export_step`` / ``import_step``.
* **IGES** — via ``OCP.IGESControl`` (OCP IGES writer/reader; reader result is face-soup,
  re-sewn to a shell and volume is computed from the closed shell).
* **BREP** — via ``build123d.export_brep`` / ``import_brep``.
* **STL** — via ``build123d.export_stl`` / ``import_stl`` (mesh only; no parametrics).
* **3MF** — via ``build123d.Mesher`` / ``lib3mf`` (mesh only).

Public API
----------
``export_document(session_bodies, path, format)``
    Export a list of ``(name, build123d_shape)`` bodies to *path* in the requested format.

``import_path(path, format=None) -> list[(name, shape)]``
    Import geometry from *path*, auto-detecting format when omitted.  Returns a list of
    ``(name, shape)`` pairs.  Names come from STEP product labels when available, fall back to
    "Body1", "Body2", …

Worker RPC handlers
-------------------
``WORKER_METHODS`` — a dict mapping method names to handler callables.  The orchestrator
registers these via ``_METHODS.update(formats.WORKER_METHODS)``.  The handlers follow the
worker_rpc.md calling convention: ``handler(params, emit) -> result_dict``  where *emit* is a
callable for streaming events.

    ``import_file {path, into?: doc_id}``
        Opens the file with :func:`import_path`, constructs one ``import`` feature per body,
        applies them through the session (``doc_apply_ops``), and returns an ops-response.
        When ``into`` is supplied the features are appended to that document; otherwise a new
        part session is created.

    ``export_file {doc_id, path, format}``
        Compiles the current document (uses the cached last_result) and exports all bodies to
        the requested format.  Returns ``{path}``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

# ---- lazy imports to keep engine.doc.__init__ OCP-free -----------------------
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
}


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
    path = Path(path)
    fmt = format or _detect_format(path)
    fmt = fmt.lower()
    path.parent.mkdir(parents=True, exist_ok=True)

    shape = _shapes_to_export_list(session_bodies)

    if fmt == "step":
        from build123d import export_step
        export_step(shape, str(path))

    elif fmt == "iges":
        from OCP.IGESControl import IGESControl_Writer
        w = IGESControl_Writer()
        w.AddShape(shape.wrapped)
        w.ComputeModel()
        ok = w.Write(str(path))
        if not ok:
            raise RuntimeError("IGES writer returned failure")

    elif fmt == "brep":
        from build123d import export_brep
        export_brep(shape, str(path))

    elif fmt == "stl":
        from build123d import export_stl
        export_stl(shape, str(path))

    elif fmt == "3mf":
        from build123d import Mesher
        m = Mesher()
        added = 0
        for name, s in session_bodies:
            # 3MF (via lib3mf) is a solid-mesh format — an open surface body has no enclosed
            # volume to mesh, so skip it rather than emit a degenerate/closed-shell mesh. Other
            # formats (STEP/IGES/BREP/STL) already accept shells, so they keep surface bodies.
            if not _is_solid_shape(s):
                continue
            m.add_shape(s)
            added += 1
        if added == 0:
            raise ValueError(
                "3MF export needs at least one solid body; all bodies are open surfaces "
                "(export STEP/IGES/STL for surface bodies)")
        m.write(str(path))

    else:
        raise ValueError(f"unsupported export format '{fmt}'")

    return str(path)


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

    # Normalise: wrap single shape in list
    if not hasattr(shape, "__iter__") or hasattr(shape, "wrapped"):
        shapes = [shape]
    else:
        shapes = list(shape)

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
    reader.TransferRoots()

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
    from build123d import import_stl
    shape = import_stl(str(path))
    return [("Body1", shape)]


def _import_3mf(path: Path) -> list[tuple[str, Any]]:
    """Import 3MF via lib3mf — returns one entry per mesh with a volume computed from triangles."""
    import lib3mf
    import numpy as np

    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    reader = model.QueryReader("3mf")
    reader.ReadFromFile(str(path))

    result: list[tuple[str, Any]] = []
    mesh_iter = model.GetMeshObjects()
    idx = 0
    while mesh_iter.MoveNext():
        idx += 1
        mesh = mesh_iter.GetCurrentMeshObject()
        vc = mesh.GetVertexCount()
        tc = mesh.GetTriangleCount()
        verts = [mesh.GetVertex(j) for j in range(vc)]
        tris = [mesh.GetTriangle(j) for j in range(tc)]
        name = mesh.GetName() or f"Body{idx}"

        # Build a pseudo-shape with a .volume property computed from the mesh (divergence
        # theorem: V = 1/6 * |Σ v0·(v1×v2)|).
        vol = 0.0
        for tri in tris:
            idxs = tri.Indices
            v0 = np.array([verts[idxs[0]].Coordinates[k] for k in range(3)])
            v1 = np.array([verts[idxs[1]].Coordinates[k] for k in range(3)])
            v2 = np.array([verts[idxs[2]].Coordinates[k] for k in range(3)])
            vol += float(np.dot(v0, np.cross(v1, v2)))
        vol = abs(vol) / 6.0

        # Return a thin wrapper so callers can call .volume
        result.append((name, _MeshShapeProxy(vol, verts, tris)))

    return result


class _MeshShapeProxy:
    """Minimal proxy returned for 3MF/STL mesh bodies that don't have an OCCT solid."""

    def __init__(self, volume: float, verts: list, tris: list) -> None:
        self.volume = volume
        self._verts = verts
        self._tris = tris
        # Satisfy callers that check `.wrapped` (will be None — no OCCT solid)
        self.wrapped = None

    def __repr__(self) -> str:
        return f"<MeshShapeProxy volume={self.volume:.1f}>"


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


# --------------------------------------------------------------------------- worker RPC handlers


EmitFn = Callable[[dict], None]


def _handler_import_file(params: dict, emit: EmitFn) -> dict:
    """Worker handler for ``import_file`` (worker_rpc.md §Formats).

    Imports the geometry at ``params["path"]``, creates one ``import`` feature per body, and
    applies them to the target session (``params.get("into")`` = existing doc_id, or a new
    part session when absent).  Returns an ops-response dict.
    """
    from engine.doc.session import new_session, get_session, apply_ops as sess_apply_ops
    from engine.doc.ops import OpError

    path = params.get("path")
    if not path:
        raise OpError("invalid_op", "import_file: 'path' is required", {"reason": "missing path"})

    into_id: str | None = params.get("into")

    try:
        bodies = import_path(path)
    except Exception as exc:  # noqa: BLE001
        raise OpError("import_failed", f"import_file: {exc}", {"path": path}) from exc

    if not bodies:
        raise OpError("import_failed", f"import_file: no bodies found in '{path}'", {"path": path})

    # Target session
    if into_id:
        session = get_session(into_id)
    else:
        session = new_session("part", name=Path(path).stem)

    # Build add_feature ops for each body
    fmt = _detect_format(path) if not params.get("format") else params["format"]
    ops = []
    for name, _shape in bodies:
        ops.append({
            "op": "add_feature",
            "feature": {
                "type": "import",
                "name": name,
                "params": {
                    "source_path": str(path),
                    "format": fmt,
                    "original_name": name,
                },
                "refs": {},
            },
        })

    response = sess_apply_ops(session, None, ops, emit)
    response["doc_id"] = session.doc_id
    return response


def _handler_export_file(params: dict, emit: EmitFn) -> dict:  # noqa: ARG001
    """Worker handler for ``export_file`` (worker_rpc.md §Formats).

    Exports the compiled bodies of the document identified by ``params["doc_id"]`` to
    ``params["path"]`` in ``params["format"]``.  Returns ``{path}``.
    """
    from engine.doc.session import get_session
    from engine.doc.ops import OpError

    doc_id = params.get("doc_id")
    path = params.get("path")
    fmt = params.get("format")

    if not doc_id:
        raise OpError("invalid_op", "export_file: 'doc_id' is required", {"reason": "missing doc_id"})
    if not path:
        raise OpError("invalid_op", "export_file: 'path' is required", {"reason": "missing path"})
    if not fmt:
        raise OpError("invalid_op", "export_file: 'format' is required", {"reason": "missing format"})

    session = get_session(doc_id)
    from engine.doc.session import _rebuild, _solid_bodies

    result = session.last_result
    if result is None:
        result = _rebuild(session)

    bodies = _solid_bodies(result)
    if not bodies:
        raise OpError("export_failed", "export_file: document has no bodies to export",
                      {"doc_id": doc_id})

    session_bodies = [(bo.name or f"Body{i}", bo.shape) for i, bo in enumerate(bodies, start=1)]

    try:
        out_path = export_document(session_bodies, path, fmt)
    except Exception as exc:  # noqa: BLE001
        raise OpError("export_failed", f"export_file: {exc}",
                      {"doc_id": doc_id, "path": path, "format": fmt}) from exc

    return {"path": out_path}


# The orchestrator wires these via: _METHODS.update(formats.WORKER_METHODS)
WORKER_METHODS: dict[str, Callable] = {
    "import_file": _handler_import_file,
    "export_file": _handler_export_file,
}
