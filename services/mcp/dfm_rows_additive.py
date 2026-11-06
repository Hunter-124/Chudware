"""Additive strength screening using measured sharp concave edges and authored pins."""
from __future__ import annotations

import math

from services.mcp.dfm_common import make_row, not_evaluated, num, op_fillet, fix_proposal, region

PROCESSES = frozenset({"fdm", "sla", "sls", "mjf"})
CHECKS = ("sharp_internal_corners", "layer_anisotropy", "min_pin")


def _sharp(ctx):
    from services.mcp.cad_eval_tools import _eval_thickness_all_bodies
    from services.mcp.dfm_fix_verify import verify_fix_ops
    from services.mcp.dfm_fix_tools import _local_field_min

    data = ctx.edge_analysis(kinds=("concave",))
    edges = data["edges"]
    wall = num(ctx.params.get("min_wall_mm"))
    ratio = num(ctx.rule_or_none("internal_corner_radius_wall_ratio"))
    if wall is None or ratio is None or wall <= 0 or ratio <= 0:
        return not_evaluated("sharp_internal_corners", "Profile needs a sourced internal_corner_radius_wall_ratio and minimum wall.")
    if not edges:
        return make_row("sharp_internal_corners", "pass", "No sharp concave edges found.",
                        basis="B-rep signed edge dihedral", confidence="geometry_heuristic")
    thickness, fields = _eval_thickness_all_bodies(ctx.doc_id, wall)
    spacing = num((thickness.get("measurement_basis") or {}).get("spacing_mm"))
    if not spacing or spacing <= 0:
        return not_evaluated("sharp_internal_corners", "Local wall sampling spacing is unavailable.")
    regions, fixes = [], []
    for e in edges[:ctx.limit]:
        at = e.get("mid")
        if not isinstance(at, list) or len(at) != 3:
            continue
        local, samples = _local_field_min(fields, {"body": e.get("body", 1),
            "bbox": {"min": at, "max": at}}, 2 * spacing)
        finding = region(faces=e.get("faces"), built_by=[e.get("owner_feature")],
            centroid=at, edge_sid=e.get("sid"), dihedral_deg=e.get("dihedral_deg"),
            local_wall_mm=local, local_samples=samples)
        regions.append(finding)
        if ctx.options.get("verify_fixes", True) is False:
            finding["fix_unavailable_reason"] = "Fix verification disabled."
            continue
        if len(regions) > 5:
            finding["fix_unavailable_reason"] = "Fix verification cap reached."
            continue
        if local is None or local <= 0:
            finding["fix_unavailable_reason"] = "No local wall samples."
            continue
        radius = max(ratio * local, 0.5 * local)
        op = op_fillet("Internal strength fillet", radius, [at])
        verified = verify_fix_ops(ctx.doc_id, [op])
        status = (verified["side_effects"].get("feature_status") or [{}])[-1]
        report = (status.get("data") or {}).get("fillet") or {}
        effective = num((report.get("effective") or {}).get("radius"))
        if (verified["builds"] is not True or effective is None or effective + 1e-9 < radius
                or report.get("selected_edges") != 1 or report.get("covered_edges") != 1):
            finding["fix_unavailable_reason"] = verified.get("error") or "Fillet failed or was clamped below the requested radius."
            finding["fillet_result"] = report
            continue
        fixes.append(fix_proposal("Add internal corner fillet", [op],
            rule=ctx.rule_source("internal_corner_radius_wall_ratio") or "profile internal corner guidance",
            kind="fillet", verified=True, requested_radius_mm=radius,
            effective_radius_mm=effective, adjustment=report.get("adjustment"),
            effect="Built on a detached clone; load and fit remain unverified."))
    return make_row("sharp_internal_corners", "fail", f"{len(edges)} sharp concave edges found.",
                    basis="B-rep signed dihedral and sampled local wall; fillet builds on an internal detached clone",
                    confidence="geometry_heuristic", metrics={"sharp_edges": len(edges)},
                    regions=regions, fix_ops=fixes, omitted=max(0, len(edges)-ctx.limit),
                    detail=ctx.detail, rule=ctx.rule_source("internal_corner_radius_wall_ratio"))


def _pins(ctx):
    threshold = num(ctx.rule_or_none("min_pin_mm"))
    if threshold is None:
        return not_evaluated("min_pin", "Process profile has no sourced minimum pin diameter.")
    features = [f for f in ctx.faces() if f.get("surf") == "cylinder" and f.get("concavity") == "boss" and num(f.get("radius")) is not None]
    if not features:
        return not_evaluated("min_pin", "No identifiable convex cylindrical pin faces were evaluated.")
    bad = [f for f in features if 2 * num(f["radius"]) < threshold]
    found = [region(faces=[f.get("sid")], built_by=[f.get("owner_feature")], centroid=f.get("centroid"), diameter_mm=2*num(f["radius"])) for f in bad]
    return make_row("min_pin", "fail" if bad else "pass", f"{len(bad)} of {len(features)} cylindrical protrusions below minimum pin diameter.",
                    basis="B-rep convex cylindrical face radius; protrusions screened as pins", confidence="geometry_heuristic",
                    assumptions=["Convex cylinders are treated as possible pins; some may be bosses or fillets."],
                    limits={"min_pin_mm": threshold}, regions=found[:ctx.limit], omitted=max(0,len(found)-ctx.limit),
                    fix=["Increase the controlling pin diameter or use an inserted pin."] if bad else [], rule=ctx.rule_source("min_pin_mm"))


def _anisotropy(ctx):
    # Prusa Research, "Modeling with 3D printing in mind", orientation and layer
    # adhesion: https://help.prusa3d.com/article/modeling-with-3d-printing-in-mind_164135
    # This is a geometry screen, not an inference of the actual service load.
    import numpy as np

    mesh = ctx.mesh()
    direction = np.asarray(ctx.direction, dtype=float)
    candidates = []
    evaluated = 0
    for index, info in enumerate(mesh.faces):
        face = next((f for f in ctx.faces() if f.get("sid") == info.sid), None)
        if not face or face.get("surf") != "cylinder" or face.get("concavity") != "boss":
            continue
        radius = num(face.get("radius"))
        axis = face.get("axis")
        if not radius or not isinstance(axis, (list, tuple)) or len(axis) != 3:
            continue
        axis = np.asarray(axis, dtype=float)
        magnitude = np.linalg.norm(axis)
        triangles = mesh.triangles[mesh.tri_face == index]
        if not magnitude or not len(triangles):
            continue
        axis /= magnitude
        projected = mesh.vertices[triangles.reshape(-1)] @ axis
        length = float(np.ptp(projected))
        diameter = 2 * radius
        if length <= 0:
            continue
        evaluated += 1
        # More parallel than perpendicular is a geometric distinction, not a
        # material-specific allowable angle or an assertion of a safe orientation.
        alignment = abs(float(axis @ direction))
        if length <= diameter or alignment <= math.sqrt(0.5):
            continue
        in_plane = axis - float(axis @ direction) * direction
        if np.linalg.norm(in_plane) < 1e-10:
            seed = np.eye(3)[int(np.argmin(np.abs(direction)))]
            in_plane = seed - float(seed @ direction) * direction
        in_plane /= np.linalg.norm(in_plane)
        rotation_axis = np.cross(axis, in_plane)
        owner = info.owner_feature
        if not owner and face.get("body") is not None and face.get("topo") is not None:
            pair = [face["body"], face["topo"]]
            owner = next((fid for fid, data in (ctx.tree().get("owner_map") or {}).items()
                          if pair in (data.get("faces") or [])), None)
        rotation_axis /= np.linalg.norm(rotation_axis)
        candidates.append(region(faces=[info.sid], built_by=[owner],
            centroid=face.get("centroid"), axis=axis.tolist(), diameter_mm=diameter,
            length_mm=length, slenderness=length / diameter, build_alignment=alignment,
            suggested_rotation={"axis": rotation_axis.tolist(),
                "angle_deg": math.degrees(math.acos(float(np.clip(axis @ in_plane, -1, 1))))}))
    wall_limit = num(ctx.params.get("min_wall_mm"))
    if wall_limit and wall_limit > 0:
        from services.mcp.cad_eval_tools import _eval_thickness_all_bodies, _wall_thickness_result
        from services.mcp.dfm_fix_tools import _region_points
        try:
            wall_regions = _wall_thickness_result(ctx.doc_id, wall_limit).get("thin_regions") or []
            _, wall_fields = _eval_thickness_all_bodies(ctx.doc_id, wall_limit)
            for thin in wall_regions:
                thickness = num(thin.get("min_thickness_mm"))
                points = _region_points(thin, wall_fields)
                if not thickness or thickness <= 0 or len(points) < 3:
                    continue
                center = points.mean(axis=0)
                _, vectors = np.linalg.eigh((points-center).T @ (points-center))
                axis = vectors[:, -1]
                length = float(np.ptp(points @ axis))
                if length <= thickness:
                    continue
                evaluated += 1
                alignment = abs(float(axis @ direction))
                if alignment <= math.sqrt(0.5):
                    continue
                in_plane = axis - float(axis @ direction) * direction
                if np.linalg.norm(in_plane) < 1e-10:
                    seed = np.eye(3)[int(np.argmin(np.abs(direction)))]
                    in_plane = seed - float(seed @ direction) * direction
                in_plane /= np.linalg.norm(in_plane)
                rotation_axis = np.cross(axis, in_plane)
                rotation_axis /= np.linalg.norm(rotation_axis)
                candidates.append(region(faces=thin.get("faces"),
                    built_by=[x.get("id") for x in thin.get("built_by", []) if isinstance(x, dict)],
                    centroid=center.tolist(), axis=axis.tolist(), section_mm=thickness,
                    length_mm=length, slenderness=length / thickness, build_alignment=alignment,
                    suggested_rotation={"axis": rotation_axis.tolist(),
                        "angle_deg": math.degrees(math.acos(float(np.clip(axis @ in_plane, -1, 1))))}))
        except Exception:  # noqa: BLE001 -- do not erase measured cylindrical findings
            pass
    if not evaluated:
        return not_evaluated("layer_anisotropy", "No protrusion or thin region with measured axial extent and section was found.")
    return make_row("layer_anisotropy", "warn" if candidates else "not_evaluated",
                    f"{len(candidates)} elongated build-aligned protrusions or thin regions may load across layers.",
                    basis="Tessellated cylinder axial extent / diameter, or principal thin-region sample extent / inscribed-sphere thickness; axis compared with build direction. Prusa Research, Modeling with 3D printing in mind: https://help.prusa3d.com/article/modeling-with-3d-printing-in-mind_164135",
                    confidence="geometry_heuristic",
                    assumptions=["Actual load direction and whether a thin region is a cantilever are unknown; unsampled regions are not assessed."],
                    metrics={"build_dir": ctx.direction, "regions_evaluated": evaluated},
                    regions=candidates[:ctx.limit], omitted=max(0,len(candidates)-ctx.limit),
                    fix=["Lay the feature axis in the build plane or reinforce locally; check optimize_print_orientation with the actual load direction."] if candidates else [])


def rows(ctx, names):
    functions = {"sharp_internal_corners": _sharp, "min_pin": _pins, "layer_anisotropy": _anisotropy}
    result = []
    for name in names:
        try:
            result.append(functions[name](ctx))
        except Exception as exc:  # noqa: BLE001
            result.append(not_evaluated(name, str(exc)))
    return result
