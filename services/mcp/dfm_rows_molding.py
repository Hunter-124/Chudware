"""Molding screens using a supplied/profile pull and cited process-profile limits.

Depth peeling is a finite-resolution two-sided straight-pull screen, not a mold
parting-line or side-action solver. Signed draft is a one-sided ejection screen;
its sign cannot be assigned to both halves without a specified parting scheme.
"""
from __future__ import annotations

import numpy as np

from services.mcp import core_bridge as cb
from services.dfam.geometry import BuildGeometry, depth_peel
from services.mcp.dfm_common import (
    DfmEvidenceError, MissingRule, fix_proposal, make_row,
    not_evaluated, num, op_edit_param, op_fillet, region, unit,
)

PROCESSES = frozenset({"injection", "cast"})
CHECKS = ("draft", "undercuts", "molding_wall", "rib_thickness_ratio",
          "boss_wall_ratio", "wall_uniformity", "corner_radius_molded")


def _rule(ctx, key):
    value = num(ctx.rule(key))
    source = ctx.rule_source(key)
    if value is None or value <= 0 or not source:
        raise DfmEvidenceError(f"{key} requires a positive numeric rule and cited rule source")
    return value, source


def _pull(ctx):
    if ctx.direction_source not in ("caller", "profile", "build_dir"):
        raise DfmEvidenceError("a pull_dir or build_dir is required; mold orientation is not assumed")
    try:
        return unit(ctx.direction)
    except ValueError as exc:
        raise DfmEvidenceError(str(exc)) from exc


def _pull_assumption(ctx):
    return {"caller": "Pull direction explicitly supplied by the caller.",
            "profile": "Pull direction taken from the process profile.",
            "build_dir": "The supplied build direction is used as an inferred mold pull."}[ctx.direction_source]


def _geometry(ctx):
    direction = _pull(ctx)
    return BuildGeometry(ctx.mesh(), direction)


def _face(mesh, triangle):
    index = int(mesh.tri_face[int(triangle)])
    if index < 0:
        return None, None
    face = mesh.faces[index]
    return face.sid, face.owner_feature


def _undercut(ctx):
    geom = _geometry(ctx)
    footprint = geom.bounds[1, :2] - geom.bounds[0, :2]
    # Sampling resolution, not a manufacturing rule. The cap bounds raster work.
    pitch = max(float(np.max(footprint)) / 200.0, 1e-6)
    peel = depth_peel(geom, cell=pitch, max_cells=40000)
    active = np.flatnonzero(peel._active_surface)
    if not len(active):
        raise DfmEvidenceError("depth peel found no material crossings")
    cells = peel.cells[active]
    starts = np.flatnonzero(np.r_[True, cells[1:] != cells[:-1]])
    ends = np.r_[starts[1:], len(active)]
    if any((end - start) % 2 for start, end in zip(starts, ends)):
        raise DfmEvidenceError("depth peel has unpaired material crossings; mesh may be open or misoriented")
    findings = {}
    crossing_count = 0
    for start, end in zip(starts, ends):
        # Internal surfaces cannot be reached by either straight-pulling mold half.
        for hit in active[start + 1:end - 1] if end - start > 2 else ():
            crossing_count += 1
            sid, owner = _face(geom.mesh, peel.tri[hit])
            key = (sid, owner, int(peel.tri[hit]) if sid is None else None)
            if key not in findings:
                xy = peel.xy_for_cells([peel.cells[hit]])[0]
                point = geom.model_points([[xy[0], xy[1], peel.z[hit]]])[0]
                findings[key] = region(faces=[sid], built_by=[owner], centroid=point)
    locations = list(findings.values())
    return make_row(
        "undercuts", "fail" if findings else "pass",
        f"{crossing_count} sampled internal crossings obstruct opposed straight pulls" if findings
        else "No internal crossing found in sampled straight-pull columns",
        basis="signed depth peel along selected pull", confidence="geometry_heuristic",
        assumptions=[_pull_assumption(ctx),
                     "Opposed straight-pulling mold halves; no side actions or parting geometry are modeled.",
                     "A finite mesh and center-sampled grid may miss narrow features."],
        metrics={"pull_dir": _pull(ctx), "direction_source": ctx.direction_source, "pitch_mm": float(peel.cell),
                 "sampled_columns": len(starts), "internal_crossings": crossing_count},
        regions=locations[:ctx.limit], omitted=max(0, len(locations) - ctx.limit), detail=ctx.detail,
        fix=["Change the obstructing geometry or design a side action and parting scheme."] if findings else [])


def _draft(ctx):
    geom = _geometry(ctx)
    draft = ctx.params.get("draft") or {}
    minimum = num(draft.get("min_deg")) if isinstance(draft, dict) else None
    source = ctx.rule_source("draft.min_deg") or ctx.rule_source("draft_min_deg")
    if minimum is None or minimum <= 0 or not source:
        raise DfmEvidenceError("draft.min_deg needs a positive value and cited rule source")
    # End caps normal to pull are not draft-bearing side walls. Preserve sign for
    # all other facets: asin(n dot pull), not the engine's absolute-angle check.
    dots = np.clip(geom.normals[:, 2], -1.0, 1.0)
    side = np.abs(dots) < 1.0 - 1e-10
    if not np.any(side):
        raise DfmEvidenceError("mesh has no draft-bearing side facets")
    angles = np.degrees(np.arcsin(dots))
    bad = np.flatnonzero(side & (angles < minimum))
    findings = []
    for index in sorted(bad, key=lambda i: angles[i]):
        sid, owner = _face(geom.mesh, index)
        findings.append(region(faces=[sid], built_by=[owner],
                               centroid=geom.model_points([geom.centroids[index]])[0],
                               signed_draft_deg=float(angles[index])))
    depth_rule = ctx.rule_or_none("draft_min_deg_per_mm_depth")
    if depth_rule is not None and not findings:
        return not_evaluated(
            "draft", "draft_min_deg_per_mm_depth requires cavity depth from an identified parting scheme",
            basis="signed tessellated surface normals along selected pull",
            assumptions=[_pull_assumption(ctx)],
            metrics={"pull_dir": _pull(ctx), "direction_source": ctx.direction_source, "side_facets": int(side.sum()),
                     "min_signed_draft_deg": float(angles[side].min())},
            limits={"min_draft_deg": minimum}, rule=source)
    return make_row(
        "draft", "fail" if findings else "pass",
        f"{len(findings)} side facets below {minimum:g} deg signed draft" if findings
        else f"Sampled side facets meet {minimum:g} deg signed draft",
        basis="signed tessellated surface normals along selected pull", confidence="geometry_heuristic",
        assumptions=[_pull_assumption(ctx),
                     "All side facets are assigned to the specified one-sided ejection direction; a two-half mold requires a parting scheme.",
                     "Perpendicular end caps are excluded; tessellation approximates curved faces."],
        metrics={"pull_dir": _pull(ctx), "direction_source": ctx.direction_source, "side_facets": int(side.sum()),
                 "min_signed_draft_deg": float(angles[side].min())},
        limits={"min_draft_deg": minimum}, rule=source,
        regions=findings[:ctx.limit], omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
        fix=["Add draft in the selected pull direction or specify a suitable mold parting scheme."] if findings else [])


def _thickness(ctx, threshold):
    from services.mcp.cad_eval_tools import _eval_thickness_all_bodies
    result, _ = _eval_thickness_all_bodies(ctx.doc_id, threshold)
    if not isinstance(result, dict) or result.get("ok") is False:
        raise DfmEvidenceError("inscribed-sphere wall thickness evaluation failed")
    minimum = num(result.get("min_thickness_mm"))
    maximum = num(result.get("max_thickness_mm"))
    if minimum is None or minimum <= 0 or maximum is None or maximum <= 0:
        raise DfmEvidenceError("inscribed-sphere wall thickness has no measured minimum and maximum")
    return result, minimum, maximum


def _wall_regions(ctx, result):
    faces = {(int(f.get("body") or 1), int(f["topo"])): f
             for f in ctx.faces() if f.get("topo") is not None}
    out = []
    for item in result.get("thin_regions") or []:
        found = [faces.get((int(item.get("body") or 1), int(fid)), {})
                 for fid in item.get("face_ids") or []]
        point = item.get("centroid") or item.get("at")
        out.append(region(faces=[f.get("sid") for f in found],
                          built_by=[f.get("owner_feature") for f in found], centroid=point,
                          thickness_mm=num(item.get("min_thickness_mm"))))
    if not out:
        sample = result.get("minimum_sample") or {}
        face_id = sample.get("face_id")
        point = sample.get("at")
        if face_id is not None and point is not None:
            face = faces.get((int(sample.get("body") or 1), int(face_id)), {})
            out.append(region(faces=[face.get("sid")], built_by=[face.get("owner_feature")],
                              centroid=point, thickness_mm=num(result.get("min_thickness_mm"))))
    return out


def _wall_material(ctx):
    # The currently published profile wall tables are for ABS injection and
    # light-alloy sand casting, not generic plastic or all cast metals.
    mass = cb.call("eval_mass", {"doc_id": ctx.doc_id}) or {}
    material = mass.get("material") or {}
    if not isinstance(material, dict) or material.get("source") in (None, "default", "per_body"):
        raise DfmEvidenceError("applied material is unavailable or mixed; wall rule is material-specific")
    key = str(material.get("key") or "").lower()
    if ctx.process == "injection" and key != "abs":
        raise DfmEvidenceError(f"wall rule is for ABS, not applied material {key!r}")
    if ctx.process == "cast" and not (key.startswith("aluminum") or key.startswith("aluminium")
                                       or key.startswith("magnesium")):
        raise DfmEvidenceError(f"sand-casting wall rule is for light alloys, not {key!r}")
    return key


def _wall(ctx):
    minimum, source = _rule(ctx, "wall_min_mm")
    material = _wall_material(ctx)
    result, actual_min, actual_max = _thickness(ctx, minimum)
    maximum = ctx.rule_or_none("wall_max_mm")
    if maximum is not None:
        maximum, max_source = _rule(ctx, "wall_max_mm")
        source += "; " + max_source
    low = actual_min < minimum
    high = maximum is not None and actual_max > maximum
    found = _wall_regions(ctx, result) if low else []
    # Maximum inscribed spheres do not establish the location or thickness of a
    # nominal wall, so an apparent excess is an evidence gap, not a warning.
    if high and not low:
        return not_evaluated("molding_wall", "maximum nominal wall thickness is not identified",
                             basis="mesh inscribed-sphere thickness",
                             metrics={"min_thickness_mm": actual_min, "max_thickness_mm": actual_max},
                             limits={"wall_min_mm": minimum, "wall_max_mm": maximum}, rule=source)
    status = "fail" if low else "pass"
    return make_row("molding_wall", status,
                    "Measured minimum wall is below the process minimum" if low else
                    "Sampled minimum wall meets the process minimum",
                    basis="mesh inscribed-sphere thickness", confidence="geometry_heuristic",
                    assumptions=["Inscribed-sphere thickness is a mesh-based screen, not a classification of nominal walls."],
                    metrics={"min_thickness_mm": actual_min, "max_thickness_mm": actual_max,
                             "applied_material": material},
                    limits={"wall_min_mm": minimum, **({"wall_max_mm": maximum} if maximum else {})},
                    regions=found[:ctx.limit], omitted=max(0, len(found) - ctx.limit), detail=ctx.detail,
                    fix=["Adjust the local wall thickness and remeasure."] if status != "pass" else [], rule=source)


def _uniformity(ctx):
    from services.aicm_handoff import read_handoff
    from services.mcp.cad_eval_tools import _eval_thickness_all_bodies
    from services.sim.tessellate import read_aicm

    table = ctx.rule("wall_uniformity_deviation_by_material")
    source = ctx.rule_source("wall_uniformity_deviation_by_material")
    material = (cb.call("eval_mass", {"doc_id": ctx.doc_id}) or {}).get("material") or {}
    if not isinstance(material, dict) or material.get("source") in (None, "default", "per_body"):
        raise DfmEvidenceError("uniformity requires an identified, unmixed material")
    key = str(material.get("key") or "").lower()
    deviation = num(table.get(key)) if isinstance(table, dict) else None
    if deviation is None or not 0 < deviation < 1 or not source:
        raise DfmEvidenceError(f"no cited uniformity rule for applied material {key!r}")
    try:
        _, fields = _eval_thickness_all_bodies(ctx.doc_id, 0)
        samples = []
        for body_no, handle in fields:
            field = read_aicm(read_handoff(handle))
            for scalar in field.get("scalars") or []:
                if scalar.get("name") != "thickness":
                    continue
                body = next((b for b in field.get("bodies") or []
                             if b["body_id"] == scalar.get("body_id")), None)
                if body is None:
                    continue
                vertices = np.asarray(body["pos"], dtype=float).reshape(-1, 3)
                triangles = np.asarray(body["idx"], dtype=int).reshape(-1, 3)
                values = np.asarray(scalar["values"], dtype=float)
                if len(values) != len(vertices):
                    continue
                tri_faces = np.zeros(len(triangles), dtype=int)
                for face in body["faces"]:
                    start = face["first_tri"]
                    tri_faces[start:start + face["tri_count"]] = face["face_id"]
                xyz = vertices[triangles]
                area = np.linalg.norm(np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0]), axis=1) / 2
                thick = values[triangles].mean(axis=1)
                for i in np.flatnonzero(np.isfinite(thick) & (thick > 0) & (area > 0)):
                    samples.append((float(thick[i]), float(area[i]), int(body_no),
                                    int(tri_faces[i]), xyz[i].mean(axis=0).tolist()))
    except Exception as exc:
        raise DfmEvidenceError(f"thickness field unavailable: {exc}") from exc
    if not samples:
        raise DfmEvidenceError("thickness field contains no finite surface samples")
    samples.sort(key=lambda s: s[0])
    areas = np.cumsum([s[1] for s in samples])
    nominal = samples[int(np.searchsorted(areas, areas[-1] / 2))][0]
    lower, upper = nominal * (1 - deviation), nominal * (1 + deviation)
    face_map = {(int(f.get("body") or 1), int(f["topo"])): f
                for f in ctx.faces() if f.get("topo") is not None}
    worst = {}
    for thickness, area, body_no, face_id, point in samples:
        if lower <= thickness <= upper:
            continue
        face = face_map.get((body_no, face_id), {})
        score = abs(thickness / nominal - 1)
        if (body_no, face_id) not in worst or score > worst[(body_no, face_id)][0]:
            worst[(body_no, face_id)] = (score, region(
                faces=[face.get("sid")], built_by=[face.get("owner_feature")],
                centroid=point, thickness_mm=thickness, nominal_wall_mm=nominal))
    findings = [entry[1] for entry in sorted(worst.values(), key=lambda x: x[0], reverse=True)]
    return make_row(
        "wall_uniformity", "warn" if findings else "pass",
        f"{len(findings)} sampled faces exceed the material's nominal-wall variation" if findings
        else "Sampled thicknesses are within the material's nominal-wall variation",
        basis="area-weighted median of inscribed-sphere thickness field triangles",
        confidence="geometry_heuristic",
        assumptions=["Median sampled surface thickness approximates nominal wall; fillets, bosses and non-wall geometry can affect the field.",
                     "Finite tessellation may miss local variations; confirm by sectional analysis."],
        metrics={"nominal_wall_mm": nominal, "sampled_triangles": len(samples), "material": key},
        limits={"minimum_mm": lower, "maximum_mm": upper, "deviation_fraction": deviation},
        regions=findings[:ctx.limit], omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
        fix=["Core out thick regions or transition wall thickness gradually."] if findings else [], rule=source)


def _rib(ctx):
    ribs = [f for f in ctx.features() if f.get("type") == "rib" and not f.get("suppressed")]
    if not ribs:
        return not_evaluated("rib_thickness_ratio", "no authored rib features; geometric ribs have not been classified")
    ratio, source = _rule(ctx, "rib_thickness_ratio_max")
    nominal = num(ctx.options.get("target_wall_mm"))
    if nominal is None or nominal <= 0:
        raise DfmEvidenceError("rib thickness requires explicit target_wall_mm for the adjacent nominal wall")
    matches = ctx.faces()
    findings, missing = [], []
    for feature in ribs:
        fid = feature.get("id")
        thickness = num((feature.get("params") or {}).get("thickness"))
        associated = [f for f in matches if f.get("owner_feature") == fid]
        located = next((f for f in associated if f.get("centroid") is not None), None)
        if thickness is None or thickness <= 0 or not fid or located is None:
            missing.append(str(fid or "unknown"))
            continue
        if thickness > nominal * ratio:
            findings.append((feature, thickness, associated, located))
    if not findings and missing:
        raise DfmEvidenceError("rib thickness or face location unavailable for " + ", ".join(missing))
    regions = [region(faces=[f.get("sid") for f in fs], built_by=[feature["id"]],
                      centroid=loc["centroid"], thickness_mm=thickness,
                      ratio=thickness / nominal) for feature, thickness, fs, loc in findings]
    proposals = [fix_proposal("Reduce rib thickness", [op_edit_param(feature["id"], thickness=nominal * ratio)],
                              rule=source, kind="edit_feature")
                 for feature, _, _, _ in findings[:ctx.limit]]
    status = "fail" if findings else "pass"
    note = f"{len(findings)} authored ribs exceed the thickness ratio" if findings else "Authored rib thicknesses meet the ratio"
    if missing:
        note += "; some ribs could not be evaluated: " + ", ".join(missing)
    return make_row("rib_thickness_ratio", status, note, basis="authored rib thickness / caller nominal wall",
                    confidence="authored_params", assumptions=["target_wall_mm is the adjacent nominal wall thickness.",
                    "Only authored rib features are classified; rib height is not inferred from an unconstrained sketch."],
                    metrics={"nominal_wall_mm": nominal, "ribs_checked": len(ribs) - len(missing)},
                    limits={"rib_thickness_ratio_max": ratio}, rule=source,
                    regions=regions[:ctx.limit], omitted=max(0, len(regions) - ctx.limit), detail=ctx.detail,
                    fix=["Reduce the rib root thickness or change the adjacent nominal wall."] if findings else [],
                    fix_ops=proposals)


def _boss_wall(ctx):
    ratio, source = _rule(ctx, "boss_wall_ratio_max")
    nominal = num(ctx.options.get("target_wall_mm"))
    if nominal is None or nominal <= 0:
        raise DfmEvidenceError("boss wall requires explicit target_wall_mm for the adjacent nominal wall")
    faces = ctx.faces()
    graph = cb.call("doc_adjacency", {"doc_id": ctx.doc_id}) or {}
    neighbors = {}
    for edge in graph.get("edges") or []:
        pair = edge.get("between_sids") or []
        if len(pair) == 2:
            neighbors.setdefault(pair[0], set()).add(pair[1])
            neighbors.setdefault(pair[1], set()).add(pair[0])
    planar = {f.get("sid") for f in faces if f.get("surf") in ("plane", "flat")}
    bosses = [f for f in faces if f.get("surf") == "cylinder" and f.get("concavity") == "boss"]
    bores = [f for f in faces if f.get("surf") == "cylinder" and f.get("concavity") == "bore"]
    measured = []
    for boss in bosses:
        for bore in bores:
            if (not boss.get("sid") or not bore.get("sid") or boss.get("body") != bore.get("body")
                    or not (neighbors.get(boss["sid"], set()) & neighbors.get(bore["sid"], set()) & planar)):
                continue
            try:
                axis = np.asarray(unit(boss.get("axis")))
                bore_axis = np.asarray(unit(bore.get("axis")))
                offset = np.asarray(boss.get("axis_point"), dtype=float) - np.asarray(bore.get("axis_point"), dtype=float)
                outside, inside = num(boss.get("radius")), num(bore.get("radius"))
                if (offset.shape != (3,) or not np.all(np.isfinite(offset))
                        or abs(abs(float(axis @ bore_axis)) - 1) > 1e-6
                        or np.linalg.norm(np.cross(axis, offset)) > 1e-6
                        or outside is None or inside is None or inside <= 0 or outside <= inside
                        or boss.get("centroid") is None):
                    continue
            except (ValueError, TypeError):
                continue
            wall = outside - inside
            measured.append((wall, region(faces=[boss["sid"], bore["sid"]],
                built_by=[boss.get("owner_feature"), bore.get("owner_feature")],
                centroid=boss["centroid"], boss_wall_mm=wall, nominal_wall_mm=nominal,
                wall_ratio=wall / nominal)))
    if not measured:
        raise DfmEvidenceError("no coaxial boss and bore sharing a planar end face were identified")
    findings = sorted((item for item in measured if item[0] > ratio * nominal),
                      key=lambda item: item[0], reverse=True)
    return make_row(
        "boss_wall_ratio", "warn" if findings else "pass",
        "Some screened boss walls exceed the nominal-wall ratio" if findings
        else "Screened boss walls meet the nominal-wall ratio",
        basis="coaxial cylindrical boss and bore sharing an end face",
        confidence="geometry_heuristic",
        assumptions=["target_wall_mm represents the adjacent nominal wall.",
                     "Only coaxial annular bosses with an identifiable planar end are screened."],
        metrics={"bosses_checked": len(measured), "nominal_wall_mm": nominal},
        limits={"boss_wall_ratio_max": ratio}, regions=[r for _, r in findings[:ctx.limit]],
        omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail, rule=source,
        fix=["Core the boss or adjust its outer diameter to reduce wall thickness."] if findings else [])


def _corner(ctx):
    ratio, source = _rule(ctx, "internal_corner_radius_wall_ratio")
    nominal = num(ctx.options.get("target_wall_mm"))
    if nominal is None or nominal <= 0:
        raise DfmEvidenceError("corner radius requires explicit target_wall_mm for adjacent nominal wall")
    minimum = nominal * ratio
    analysis = ctx.edge_analysis(kinds=("concave",))
    if analysis.get("truncated"):
        raise DfmEvidenceError("concave edge analysis is truncated")
    findings, proposals, missing = [], [], False
    for edge in analysis.get("edges") or []:
        if edge.get("kind") != "concave":
            continue
        point = edge.get("mid")
        if point is None:
            missing = True
            continue
        owners = [edge.get("owner_feature")]
        findings.append(region(faces=edge.get("faces"), built_by=owners, centroid=point,
                               radius_mm=0.0))
        proposals.append(fix_proposal("Round the internal corner",
                                      [op_fillet("Mold corner", minimum, [point])],
                                      rule=source, kind="fillet"))
    for blend in analysis.get("concave_blends") or []:
        radius = num(blend.get("radius_mm"))
        face = ctx.face(blend.get("face_sid")) or {}
        if radius is None or face.get("centroid") is None:
            missing = True
            continue
        if radius < minimum:
            findings.append(region(faces=[blend.get("face_sid")],
                                   built_by=[blend.get("owner_feature") or face.get("owner_feature")],
                                   centroid=face["centroid"], radius_mm=radius))
    if missing and not findings:
        raise DfmEvidenceError("one or more concave corners lack radius or location evidence")
    return make_row(
        "corner_radius_molded", "fail" if findings else "pass",
        f"{len(findings)} internal corners fall below the recommended radius" if findings
        else "Measured internal corners meet the recommended radius",
        basis="concave B-rep edges and blend radii", confidence="geometry_heuristic",
        assumptions=["target_wall_mm represents the adjacent nominal wall thickness.",
                     "Only identified concave edges and concave blends are evaluated."]
                    + (["Some corners lack radius or location evidence."] if missing else []),
        metrics={"nominal_wall_mm": nominal}, limits={"min_radius_mm": minimum}, rule=source,
        regions=findings[:ctx.limit], omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
        fix=["Increase the internal corner radius."] if findings else [],
        fix_ops=proposals[:ctx.limit])


def rows(ctx, names):
    handlers = {"draft": _draft, "undercuts": _undercut, "molding_wall": _wall,
                "rib_thickness_ratio": _rib, "boss_wall_ratio": _boss_wall,
                "wall_uniformity": _uniformity, "corner_radius_molded": _corner}
    result = []
    for name in names:
        try:
            row = handlers[name](ctx)
            if row.get("fix_ops"):
                if ctx.options.get("verify_fixes", True):
                    from services.mcp.dfm_fix_verify import verify_row_fixes
                    row = verify_row_fixes(ctx, row, lambda fresh: handlers[name](fresh))
                else:
                    row["fix_ops"] = []
            result.append(row)
        except (DfmEvidenceError, MissingRule, ValueError) as exc:
            result.append(not_evaluated(name, str(exc)))
        except Exception as exc:  # keep failures isolated to the requested check
            result.append(not_evaluated(name, f"molding evidence unavailable: {exc}"))
    return result
