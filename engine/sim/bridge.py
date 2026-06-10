"""C19 doc-side simulation bridge.

The sim worker (``engine.sim.worker``) is a *separate* process that only ever sees FILES
(STEP/STL) + study JSON — it never touches live document state.  The DOC worker
(``engine.worker``) owns the live :class:`~engine.doc.session.DocSession`s, so producing the sim
INPUTS from a session is the doc worker's job.  This module is the small, precise helper pair the
doc worker calls:

* :func:`export_step` — write the session's last-good solid bodies to a STEP file (build123d
  export, exactly like ``stepplus`` does), so the sim worker can mesh / voxelise it.
* :func:`face_points` — resolve a C03 selector against the live compile result and sample that
  face's triangulation nodes, yielding the surface point cloud the FEA solver uses to identify
  fixed / loaded node sets (``fea_static.FaceSet``).

Both work off the session's *current* :class:`CompileResult` (``session.last_result``); they
rebuild it on demand if the session has never been built.  Canonical mm throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from engine.doc import selectors as sel


# --------------------------------------------------------------------------- helpers


def _ensure_result(session):
    """Return the session's current CompileResult, rebuilding if needed."""
    from engine.doc import session as docsess
    return docsess._ensure_built(session)


def _solid_bodies(result) -> list:
    return list(result.bodies)


def _bbox_diag(shape) -> float:
    try:
        bb = shape.bounding_box()
        import math
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


# --------------------------------------------------------------------------- export_step


def export_step(session, path: str) -> str:
    """Export the session's last-good solid bodies to ``path``. Returns the written path.

    Format follows the extension: ``.stl`` -> binary STL (for the CFD voxeliser, which loads via
    trimesh), anything else -> STEP (AP214) for the FEA mesher.  A single body exports as-is;
    multiple bodies are combined into a build123d ``Compound``.
    """
    result = _ensure_result(session)
    bodies = [bo.shape for bo in _solid_bodies(result)]
    if not bodies:
        raise ValueError("document has no solid bodies to export")

    from build123d import Compound

    shape = bodies[0] if len(bodies) == 1 else Compound(children=list(bodies))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() == ".stl":
        from build123d import export_stl
        export_stl(shape, str(p))
    else:
        from build123d import export_step as _export_step
        _export_step(shape, str(p))
    return str(p)


# --------------------------------------------------------------------------- face_points


def face_points(session, selector_json: dict, max_n: int = 400) -> dict:
    """Resolve a face selector and return a sampled point cloud on that face.

    The selector (a C03 selector object, ``kind == "face"``) is resolved against the live compile
    result's last body topology — exactly how the worker's evaluate handlers resolve refs.  The
    resolved face is tessellated (its triangulation nodes), then down-sampled to at most
    ``max_n`` points (uniform stride) so the FEA node-matching stays cheap.

    Returns ``{"points": [[x, y, z], ...], "tol_mm": float}`` where ``tol_mm`` is a generous
    radius (a fraction of the body bbox diagonal) for the solver to gather mesh nodes near the
    sampled face points.
    """
    if not isinstance(selector_json, dict) or selector_json.get("kind") != "face":
        raise ValueError("face_points requires a face selector")

    result = _ensure_result(session)
    bodies = _solid_bodies(result)
    if not bodies:
        raise ValueError("document has no bodies")

    # Resolve against the last body's faces (matches the worker's eval ref resolution policy;
    # multi-body sim selection is a later-card refinement).
    body = bodies[-1]
    raw = body.shape.wrapped
    diag = _bbox_diag(body.shape)

    infos = sel.describe_faces(raw)
    elems = body.shape.faces()
    rr = sel.resolve(selector_json, infos, diag)
    if rr.index is None:
        raise ValueError(f"face selector dangling: {rr.dangling_reason}")

    face = elems[rr.index]

    pts = _sample_face_nodes(face)
    if pts.shape[0] == 0:
        # Fall back to the selector centroid so the solver still has at least one anchor point.
        c = selector_json.get("centroid")
        if isinstance(c, (list, tuple)) and len(c) == 3:
            pts = np.array([c], dtype=np.float64)
        else:
            raise ValueError("face has no triangulation nodes to sample")

    # Down-sample to <= max_n points with a uniform stride.
    if pts.shape[0] > max_n:
        stride = (pts.shape[0] + max_n - 1) // max_n
        pts = pts[::stride][:max_n]

    tol_mm = max(0.5, diag * 0.02)
    return {"points": pts.tolist(), "tol_mm": float(tol_mm)}


def _sample_face_nodes(face) -> np.ndarray:
    """Return the (k, 3) world-space triangulation node coordinates of a build123d face."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location

    shape = face.wrapped if hasattr(face, "wrapped") else face
    # Ensure the face carries a triangulation (cheap if already meshed by the doc tessellation).
    BRepMesh_IncrementalMesh(shape, 0.5, False, 0.5, True).Perform()

    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(shape, loc)
    if tri is None:
        return np.zeros((0, 3), dtype=np.float64)

    trsf = loc.Transformation()
    is_identity = loc.IsIdentity()
    out: list[list[float]] = []
    for ni in range(1, tri.NbNodes() + 1):
        nd = tri.Node(ni)
        if not is_identity:
            nd.Transform(trsf)
        out.append([nd.X(), nd.Y(), nd.Z()])
    return np.array(out, dtype=np.float64) if out else np.zeros((0, 3), dtype=np.float64)
