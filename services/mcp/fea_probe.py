"""Point sampling of actual FEA VTU fields and surface attribution of hot spots."""
from __future__ import annotations

import math
from typing import Optional

from .cad_common import _doc, _dumps, _err, cb, resolve_agent_path


def probe_vtu(path: str, point_mm: list) -> dict:
    """Interpolate point-data at a world-coordinate point inside the solved mesh."""
    import pyvista as pv
    import numpy as np

    if not isinstance(point_mm, (list, tuple)) or len(point_mm) != 3:
        raise ValueError("point_mm must contain three finite coordinates in mm")
    point = [float(x) for x in point_mm]
    if not all(math.isfinite(x) for x in point):
        raise ValueError("point_mm must contain three finite coordinates in mm")
    path = resolve_agent_path(path, "vtu_path")
    if not path.lower().endswith(".vtu"):
        raise ValueError("vtu_path must name a VTU field written by the solver")
    mesh = pv.read(path)
    sampled = pv.PolyData(np.asarray([point], dtype=float)).sample(mesh)
    if not sampled.point_data.get("vtkValidPointMask", [0])[0]:
        raise ValueError("point_mm is outside the VTU mesh; no field value can be interpolated")
    fields = {}
    for source, name in (("von_mises", "von_mises_mpa"),
                         ("factor_of_safety", "factor_of_safety"),
                         ("displacement", "displacement_mm")):
        if source not in mesh.point_data:
            continue
        value = sampled.point_data[source][0]
        value = value.tolist() if hasattr(value, "tolist") else float(value)
        fields[name] = value
    if not fields:
        raise ValueError("VTU contains none of the supported FEA point fields")
    return {"ok": True, "vtu_path": path, "point_mm": point, "fields": fields,
            "method": "element interpolation of VTU point data",
            "note": "A sampled FoS may be solver-capped at 15; stress can be mesh-sensitive."}


def attribute_hot_spots(doc_id: str, spots: list, radius_mm: float) -> tuple:
    """Join nearest display triangle to a CAD face; report distance and approximation."""
    import numpy as np
    import pyvista as pv
    from .print_tools import load_doc
    from .dfm_common import DfmContext
    from .cad_common import _doc_mesh, _face_attribution

    mesh, _, _ = load_doc(doc_id)
    faces = np.column_stack((np.full(len(mesh.triangles), 3), mesh.triangles)).ravel()
    surface = pv.PolyData(mesh.vertices, faces)
    attribution = _face_attribution(doc_id)
    owners = {f.get("sid"): owner for f, owner in
              zip(attribution["faces"], attribution["owner"]) if f.get("sid")}
    context = DfmContext(doc_id, "", {}, [0, 0, 1], "default")
    try:
        edge_data = context.edge_analysis(kinds=("concave",))
    except Exception as exc:  # Preserve the independent tessellation attribution.
        edge_data = {"edges": [], "concave_blends": [], "error": str(exc)}
    edges = edge_data["edges"]
    raw_mesh = _doc_mesh(doc_id) or {}
    polylines = {(edge.get("body"), edge.get("edge_id")): np.asarray(edge.get("pts"))
                 for edge in raw_mesh.get("edges", [])}
    measured = []
    for edge in edges:
        polyline = polylines.get((edge.get("body"), edge.get("topo")))
        if (polyline is not None and polyline.ndim == 2 and len(polyline) >= 2
                and np.isfinite(polyline).all() and np.any(np.diff(polyline, axis=0))):
            measured.append((edge, polyline))
    complete = (not edge_data.get("error") and not edge_data.get("truncated")
                and len(measured) == len(edges))
    tolerance = max(radius_mm * 0.1, 1e-5)
    for spot in spots:
        cell, closest = surface.find_closest_cell(spot["location_mm"], return_closest_point=True)
        distance = float(np.linalg.norm(np.asarray(spot["location_mm"]) - closest))
        face_index = int(mesh.tri_face[cell])
        spot["nearest_surface_distance_mm"] = distance
        # Interior maxima are not claimed to lie on a CAD face.
        if face_index >= 0 and distance <= max(radius_mm * 0.1, 1e-5):
            face = mesh.faces[face_index]
            spot["face_sid"] = face.sid
            spot["owner_feature"] = owners.get(face.sid) or face.owner_feature
            spot["face_attribution"] = "nearest display triangle (tessellation approximation)"
        p = np.asarray(spot["location_mm"])
        distances = []
        for edge, polyline in measured:
            start, delta = polyline[:-1], polyline[1:] - polyline[:-1]
            length2 = np.einsum("ij,ij->i", delta, delta)
            valid = length2 > 0
            if not valid.any():
                continue
            start, delta, length2 = start[valid], delta[valid], length2[valid]
            t = np.clip(np.einsum("ij,ij->i", p - start, delta) / length2, 0, 1)
            distance = float(np.min(np.linalg.norm(p - (start + t[:, None] * delta), axis=1)))
            distances.append((distance, edge))
        if distances:
            distance, edge = min(distances, key=lambda item: item[0])
            spot["nearest_sharp_edge_distance_mm"] = distance
            spot["sharp_edge_tolerance_mm"] = tolerance
            if distance <= tolerance:
                spot["at_sharp_edge"] = True
                spot["sharp_edge_sid"] = edge.get("sid")
                spot["sharp_edge_warning"] = ("Peak is within the stated tolerance of a "
                    "tessellated concave sharp edge; assess refinement and fixture singularity.")
            else:
                spot["at_sharp_edge"] = False if complete else None
        else:
            spot["at_sharp_edge"] = False if complete and not edges else None
        if spot["at_sharp_edge"] is None:
            spot["sharp_edge_assessment_reason"] = "Concave-edge tessellation was unavailable or incomplete."
    return spots, edge_data


def _vtu_for_study(doc_id: Optional[str], study: Optional[str], vtu_path: Optional[str]):
    if study is not None and vtu_path is not None:
        raise ValueError("Pass study or vtu_path, not both.")
    if vtu_path is not None:
        return doc_id, vtu_path, None
    doc_id = _doc(doc_id)
    rows = (cb.call("sim_study_list", {"doc_id": doc_id}) or {}).get("studies") or []
    if study is not None:
        rows = [row for row in rows if row.get("id") == study or row.get("name") == study]
        if len(rows) != 1:
            raise ValueError("study must identify exactly one stored study by id or name.")
    else:
        rows = [row for row in rows if row.get("vtu_path")]
        rows = rows[-1:]
    if not rows or not rows[0].get("vtu_path"):
        raise ValueError("No VTU is stored for that study. Re-run FEA with vtu_path=... .")
    return doc_id, rows[0]["vtu_path"], rows[0].get("id")


def _point3(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain three finite coordinates in mm.")
    result = [float(x) for x in value]
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f"{label} must contain three finite coordinates in mm.")
    return result


def probe_region(path: str, *, region: Optional[dict] = None,
                 doc_id: Optional[str] = None, face_sid: Optional[str] = None,
                 surface_tolerance_mm: float = 0.1) -> dict:
    """Reduce real VTU nodal fields over a box/sphere or a tessellated CAD face."""
    import numpy as np
    import pyvista as pv
    path = resolve_agent_path(path, "vtu_path")
    if not path.lower().endswith(".vtu"):
        raise ValueError("vtu_path must name a VTU field written by the solver")
    mesh = pv.read(path)
    points = np.asarray(mesh.points)
    if region is not None:
        if not isinstance(region, dict):
            raise ValueError("region must be a box {min_mm,max_mm} or sphere {center_mm,radius_mm}.")
        if set(region) == {"min_mm", "max_mm"}:
            lo, hi = _point3(region["min_mm"], "region.min_mm"), _point3(region["max_mm"], "region.max_mm")
            if any(a > b for a, b in zip(lo, hi)):
                raise ValueError("region.min_mm must not exceed region.max_mm.")
            selected = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        elif set(region) == {"center_mm", "radius_mm"}:
            center = _point3(region["center_mm"], "region.center_mm")
            radius = float(region["radius_mm"])
            if not math.isfinite(radius) or radius <= 0:
                raise ValueError("region.radius_mm must be finite and positive.")
            selected = np.flatnonzero(np.linalg.norm(points - center, axis=1) <= radius)
        else:
            raise ValueError("region must contain exactly min_mm/max_mm or center_mm/radius_mm.")
        method = "VTU nodes inside requested region"
    else:
        from .print_tools import load_doc
        tolerance = float(surface_tolerance_mm)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("surface_tolerance_mm must be finite and positive.")
        doc_id = _doc(doc_id)
        cad, _, _ = load_doc(doc_id)
        ids = [i for i, f in enumerate(cad.faces) if f.sid == face_sid]
        if not ids:
            raise ValueError("face_sid does not identify a tessellated face in the current document.")
        tris = cad.triangles[np.isin(cad.tri_face, ids)]
        if not len(tris):
            raise ValueError("face_sid has no display triangles.")
        def surface(triangles):
            cells = np.column_stack((np.full(len(triangles), 3), triangles)).ravel()
            return pv.PolyData(cad.vertices, cells)
        target, whole = surface(tris), surface(cad.triangles)
        selected = []
        for i, p in enumerate(points):
            _, near = target.find_closest_cell(p, return_closest_point=True)
            dist = float(np.linalg.norm(p - near))
            if dist > tolerance:
                continue
            _, closest = whole.find_closest_cell(p, return_closest_point=True)
            if dist <= float(np.linalg.norm(p - closest)) + 1e-7:
                selected.append(i)
        selected = np.asarray(selected, dtype=int)
        method = ("VTU nodes near the current document's tessellated face; "
                  "ties at shared edges are included, not an exact CAD projection")
    if not len(selected):
        raise ValueError("No VTU nodes matched the requested region/face; adjust the region or "
                         "surface_tolerance_mm, and ensure the VTU matches the current geometry.")
    fields = {}
    for source, name in (("von_mises", "von_mises_mpa"), ("factor_of_safety", "factor_of_safety"),
                         ("displacement", "displacement_mm")):
        if source not in mesh.point_data:
            continue
        values = np.asarray(mesh.point_data[source])[selected]
        if source == "displacement" and values.ndim == 2:
            values = np.linalg.norm(values, axis=1)
            name = "displacement_magnitude_mm"
        elif values.ndim == 2 and values.shape[1] == 1:
            values = values[:, 0]
        valid = np.isfinite(values)
        if not valid.any():
            continue
        indices = selected[valid]
        vals = values[valid]
        imax, imin = int(np.argmax(vals)), int(np.argmin(vals))
        fields[name] = {"min": float(vals[imin]), "max": float(vals[imax]),
                        "max_location_mm": points[indices[imax]].tolist()}
    if not fields:
        raise ValueError("VTU contains no finite supported FEA point fields in this selection.")
    return {"ok": True, "vtu_path": path, "face_sid": face_sid, "region": region,
            "n_nodes": len(selected), "fields": fields, "method": method,
            "note": "Nodal extrema are mesh-dependent, not continuous face maxima; FoS may be capped at 15."}


def register_fea_probe(mcp) -> None:
    @mcp.tool()
    def fea_probe(study: Optional[str] = None, doc_id: Optional[str] = None,
                  vtu_path: Optional[str] = None, point_mm: Optional[list] = None,
                  points: Optional[list] = None, face_sid: Optional[str] = None,
                  region: Optional[dict] = None, surface_tolerance_mm: float = 0.1) -> str:
        """Sample a solver VTU by point(s), CAD face SID, or 3D box/sphere region.

        Select a stored study by id/name (or the latest with a VTU), or pass vtu_path.
        Supply exactly one of point_mm, points (up to 100 XYZ points), face_sid, or
        region={min_mm,max_mm} / {center_mm,radius_mm}. Points use element interpolation;
        face/region extrema are VTU nodal extrema, not continuous maxima. Face selection
        uses the current CAD tessellation and surface_tolerance_mm, so the field and
        current geometry must correspond. FoS may be capped at 15.
        """
        try:
            if sum(value is not None for value in (point_mm, points, face_sid, region)) != 1:
                raise ValueError("Specify exactly one of point_mm, points, face_sid or region.")
            d, path, study_id = _vtu_for_study(doc_id, study, vtu_path)
            if points is not None:
                if not isinstance(points, list) or not 1 <= len(points) <= 100:
                    raise ValueError("points must be a list of 1 to 100 XYZ points.")
                result = {"ok": True, "samples": [probe_vtu(path, p) for p in points]}
            elif point_mm is not None:
                result = probe_vtu(path, point_mm)
            else:
                result = probe_region(path, region=region, doc_id=d, face_sid=face_sid,
                                      surface_tolerance_mm=surface_tolerance_mm)
            if study_id is not None:
                result["study_id"] = study_id
            return _dumps(result)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
