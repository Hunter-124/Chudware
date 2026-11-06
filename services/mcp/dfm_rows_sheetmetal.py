"""Sheet-metal DfM using resolved bend construction records.

Construction records do not prove that a later feature preserved a bend.  We
only compare a single, final edge-flange with a surviving owned face; imported
sheet, jogs, hems and subsequently modified bends need a topology-aware survey.
"""
from __future__ import annotations

import math

from services.mcp import core_bridge as cb
from services.mcp.dfm_common import (
    DfmEvidenceError, fix_proposal, make_row, not_evaluated, num, op_edit_param, region,
)

PROCESSES = frozenset({"sheet_metal"})
CHECKS = ("bend_radius", "hole_to_bend", "hole_to_edge", "flange_length",
          "bend_relief", "k_factor")


def _positive(value, name):
    value = num(value)
    if value is None or value <= 0:
        raise DfmEvidenceError(f"{name} is not a positive finite number")
    return value


def _bend(ctx, *, allow_later_holes=False, index=None):
    try:
        answer = cb.call("eval_sheet_metal_bends", {"doc_id": ctx.doc_id})
    except Exception as exc:  # noqa: BLE001
        raise DfmEvidenceError(f"eval_sheet_metal_bends unavailable: {exc}") from exc
    if not isinstance(answer, dict) or answer.get("ok") is False:
        raise DfmEvidenceError("eval_sheet_metal_bends failed")
    records = answer.get("records")
    if not isinstance(records, list) or not records:
        raise DfmEvidenceError("no compiled bend records are available")
    if index is None and len(records) != 1:
        raise DfmEvidenceError("multiple bend records require an individual geometry survey")
    bend = records[0 if index is None else index]
    if not isinstance(bend, dict) or not bend.get("feature_id"):
        raise DfmEvidenceError("bend record has no generating feature")
    features = ctx.features()
    matches = [i for i, f in enumerate(features) if f.get("id") == bend["feature_id"]]
    if len(matches) != 1 or features[matches[0]].get("type") != "sm_edge_flange":
        raise DfmEvidenceError("only an identifiable edge flange can be certified from its construction record")
    # Any subsequent solid operation could trim, replace, flatten or transform it.
    harmless = {"sketch", "sketch3d", "datum_plane", "datum_axis", "datum_point", "datum_csys"}
    if allow_later_holes:
        harmless.update(("hole", "sm_edge_flange"))
    if any(f.get("type") not in harmless for f in features[matches[0] + 1:]):
        raise DfmEvidenceError("a later feature may have changed the recorded bend geometry")
    radius = _positive(bend.get("radius_mm"), "bend radius")
    faces = [f for f in ctx.faces() if f.get("owner_feature") == bend["feature_id"]
             and f.get("surf") == "cylinder" and num(f.get("radius")) is not None
             and math.isclose(num(f["radius"]), radius, rel_tol=1e-6, abs_tol=1e-7)
             and f.get("sid") and isinstance(f.get("centroid"), (list, tuple))
             and len(f["centroid"]) == 3 and all(num(v) is not None for v in f["centroid"])]
    if not faces:
        raise DfmEvidenceError("no surviving owned cylindrical bend face matches the recorded radius")
    return bend, faces


def _finding(ctx, check, bend, faces, measured, limit, field, parameter, rule_key):
    failed = measured < limit
    face = faces[0]
    location = region(faces=[f["sid"] for f in faces], built_by=[bend["feature_id"]],
                      centroid=face["centroid"], **{field: measured})
    source = ctx.rule_source(rule_key)
    kwargs = {}
    if failed:
        ops = [op_edit_param(bend["feature_id"], **{parameter: limit})]
        kwargs = {"fix": [f"Increase the flange {parameter} to at least {limit:.3f} mm."],
                  "fix_ops": [fix_proposal(f"Increase {parameter}", ops, rule=source or rule_key,
                                           kind="edit_feature")]}
    return make_row(check, "fail" if failed else "pass",
                    f"Measured {measured:.3f} mm; minimum {limit:.3f} mm.",
                    basis="compiled edge-flange construction and surviving owned face",
                    confidence="authored_params", metrics={field: measured},
                    limits={"minimum_mm": limit}, regions=[location] if failed else [],
                    rule=source, **kwargs)


def _flange(ctx):
    factor = _positive(ctx.rule("min_flange_length_thickness_factor"),
                       "min_flange_length_thickness_factor")
    bend, faces = _bend(ctx)
    length = _positive(bend.get("wall_length_mm"), "wall length")
    thickness = _positive(bend.get("thickness_mm"), "thickness")
    # Profile citation specifies flange >= factor * thickness, with no radius term.
    return _finding(ctx, "flange_length", bend, faces, length, factor * thickness,
                    "flange_length_mm", "length", "min_flange_length_thickness_factor")


def _radius(ctx):
    ratios = ctx.rule("min_bend_radius_per_thickness")
    if not isinstance(ratios, dict):
        raise DfmEvidenceError("min_bend_radius_per_thickness is not a material-class table")
    mass = cb.call("eval_mass", {"doc_id": ctx.doc_id}) or {}
    material = mass.get("material") or {}
    if not isinstance(material, dict) or material.get("source") in (None, "default", "per_body"):
        raise DfmEvidenceError("applied material is unavailable or mixed")
    key = material.get("key")
    if not key or key not in ratios:
        raise DfmEvidenceError(f"no bend-radius rule for applied material {key!r}")
    ratio = _positive(ratios[key], f"min_bend_radius_per_thickness[{key}]")
    bend, faces = _bend(ctx)
    radius = _positive(bend.get("radius_mm"), "bend radius")
    thickness = _positive(bend.get("thickness_mm"), "thickness")
    row = _finding(ctx, "bend_radius", bend, faces, radius, ratio * thickness,
                   "bend_radius_mm", "radius", "min_bend_radius_per_thickness")
    row["assumptions"].append("Across-grain design starting point; confirm grain direction, gauge, material condition and tooling with the fabricator.")
    return row


def _k_factor(ctx):
    lower = num(ctx.rule("k_factor_min"))
    upper = num(ctx.rule("k_factor_max"))
    if lower is None or upper is None or lower > upper:
        raise DfmEvidenceError("invalid k_factor_min/k_factor_max rules")
    bend, faces = _bend(ctx)
    t = _positive(bend.get("thickness_mm"), "thickness")
    r = _positive(bend.get("radius_mm"), "bend radius")
    angle = num(bend.get("angle_deg"))
    allowance = num(bend.get("bend_allowance_mm"))
    if angle is None or angle == 0 or allowance is None:
        raise DfmEvidenceError("bend angle or allowance unavailable")
    k = (allowance / math.radians(angle) - r) / t
    failed = not lower <= k <= upper
    face = faces[0]
    loc = region(faces=[f["sid"] for f in faces], built_by=[bend["feature_id"]],
                 centroid=face["centroid"], k_factor=k)
    source = "; ".join(s for s in (ctx.rule_source("k_factor_min"), ctx.rule_source("k_factor_max")) if s)
    kwargs = {}
    if failed:
        target = min(upper, max(lower, k))
        ops = [op_edit_param(bend["feature_id"], k_factor=target)]
        kwargs = {"fix": [f"Set the flange K-factor to {target:.3f}."],
                  "fix_ops": [fix_proposal("Adjust K-factor", ops, rule=source or "k_factor",
                                           kind="edit_feature")]}
    return make_row("k_factor", "fail" if failed else "pass",
                    f"Resolved K-factor {k:.3f}; allowed range {lower:.3f}–{upper:.3f}.",
                    basis="bend allowance and resolved bend dimensions", confidence="authored_params",
                    metrics={"k_factor": k}, limits={"minimum": lower, "maximum": upper},
                    regions=[loc] if failed else [], rule=source, **kwargs)


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise DfmEvidenceError("bend or hole frame vector is unavailable")
    values = [num(x) for x in value]
    if any(x is None for x in values):
        raise DfmEvidenceError("bend or hole frame vector is invalid")
    return values


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit(value):
    v = _vector(value)
    length = math.sqrt(_dot(v, v))
    if length < 1e-12:
        raise DfmEvidenceError("bend or hole frame vector is zero")
    return [x / length for x in v]


def _hole_to_bend_one(ctx, index):
    factor = _positive(ctx.rule("hole_to_bend_thickness_factor"),
                       "hole_to_bend_thickness_factor")
    bend, bend_faces = _bend(ctx, allow_later_holes=True, index=index)
    thickness = _positive(bend.get("thickness_mm"), "thickness")
    radius = _positive(bend.get("radius_mm"), "bend radius")
    start, end = _vector(bend.get("start")), _vector(bend.get("end"))
    along = _unit([b - a for a, b in zip(start, end)])
    outward, normal = _unit(bend.get("length_dir")), _unit(bend.get("fold_dir"))
    if abs(_dot(along, outward)) > 1e-6 or abs(_dot(along, normal)) > 1e-6 or abs(_dot(outward, normal)) > 1e-6:
        raise DfmEvidenceError("bend frame is not orthogonal")
    span = math.sqrt(sum((b - a) ** 2 for a, b in zip(start, end)))
    # sm_edge_flange_solid's inner tangent offset from its selected edge.
    position = bend.get("position")
    if position not in ("material_inside", "material_outside", "bend_outside"):
        raise DfmEvidenceError("unknown bend position")
    tangent = {"material_inside": -radius - thickness, "material_outside": -radius,
               "bend_outside": 0.0}[position]
    threshold = factor * thickness + radius
    features = {f.get("id"): f for f in ctx.features() if f.get("id")}
    findings = []
    screened = 0
    bore_count = sum(1 for f in ctx.faces() if f.get("surf") == "cylinder"
                     and f.get("concavity") == "bore")
    for face in ctx.faces():
        owner = face.get("owner_feature")
        feature = features.get(owner, {})
        params = feature.get("params") or {}
        points = params.get("world_points")
        if (face.get("surf") != "cylinder" or face.get("concavity") != "bore"
                or feature.get("type") != "hole" or params.get("end") != "through_all"
                or not isinstance(points, list) or len(points) != 1 or not face.get("sid")
                or face.get("body") is None or face.get("body") not in {f.get("body") for f in bend_faces}):
            continue
        try:
            hole_r = _positive(face.get("radius"), "hole radius")
            area = _positive(face.get("area_or_len"), "hole face area")
            axis, origin, point = _unit(face.get("axis")), _vector(face.get("axis_point")), _vector(points[0])
            centroid = _vector(face.get("centroid"))
        except DfmEvidenceError:
            continue
        # A full circular through-hole in this constant-thickness flat has area 2*pi*r*t.
        # Reject partial arcs, countersinks, oblique bores and ambiguous placements.
        if (abs(abs(_dot(axis, normal)) - 1) > 1e-6
                or not math.isclose(area, 2 * math.pi * hole_r * thickness, rel_tol=1e-5, abs_tol=1e-6)):
            continue
        delta = [a - b for a, b in zip(origin, start)]
        pdelta = [a - b for a, b in zip(point, start)]
        u, x = _dot(delta, along), _dot(delta, outward)
        cdelta = [a - b for a, b in zip(centroid, start)]
        if (abs(_dot(pdelta, along) - u) > 1e-5 or abs(_dot(pdelta, outward) - x) > 1e-5
                or not math.isclose(abs(_dot(cdelta, normal)), thickness / 2, rel_tol=1e-5, abs_tol=1e-5)
                or u - hole_r < 0 or u + hole_r > span or x + hole_r > tangent):
            continue
        screened += 1
        distance = tangent - x - hole_r
        if distance >= threshold:
            continue
        shift = threshold - distance
        moved = [[point[i] - outward[i] * shift for i in range(3)]]
        op = {"op": "edit_feature", "id": owner, "params": {"world_points": moved}}
        loc = region(faces=[face["sid"]] + [f["sid"] for f in bend_faces], built_by=[owner, bend["feature_id"]],
                     centroid=centroid, hole_to_bend_mm=distance)
        findings.append((distance, loc, op))
    if not findings:
        if screened and screened == bore_count:
            return make_row("hole_to_bend", "pass", "All identified bores meet the screened bend distance.",
                            basis="B-rep cylindrical through-hole and compiled inner tangent frame",
                            confidence="geometry_heuristic", metrics={"holes_screened": screened},
                            limits={"minimum_mm": threshold},
                            rule=ctx.rule_source("hole_to_bend_thickness_factor"))
        raise DfmEvidenceError("no violating unambiguous planar through-hole was identified; other holes and reliefs were not surveyed")
    findings.sort(key=lambda f: f[0])
    shown = findings[:ctx.limit]
    source = ctx.rule_source("hole_to_bend_thickness_factor")
    return make_row(
        "hole_to_bend", "warn",
        f"Screened hole edge is {shown[0][0]:.3f} mm from the recorded inner bend tangent; guideline is {threshold:.3f} mm. Other holes may not have been assessed.",
        basis="B-rep cylindrical through-hole and compiled inner tangent frame", confidence="geometry_heuristic",
        assumptions=["A full cylindrical hole face of area 2πrt identifies a normal through-hole in the flat sheet.",
                     "The guideline is conservatively applied from the recorded inner tangent; this is not a full topology survey."],
        metrics={"minimum_screened_distance_mm": shown[0][0], "screened_violations": len(findings)},
        limits={"minimum_mm": threshold}, regions=[f[1] for f in shown],
        fix=["Move the indicated hole away from the bend in the flat-sheet plane."],
        fix_ops=[fix_proposal("Move hole away from bend", [f[2]], rule=source or "hole_to_bend_thickness_factor",
                              kind="edit_feature") for f in shown],
        omitted=len(findings) - len(shown), detail=ctx.detail, rule=source)


def _hole_to_bend(ctx):
    answer = cb.call("eval_sheet_metal_bends", {"doc_id": ctx.doc_id}) or {}
    records = answer.get("records")
    if not isinstance(records, list) or not records:
        raise DfmEvidenceError("no compiled bend records are available")
    if len(records) == 1:
        return _hole_to_bend_one(ctx, 0)
    evaluated, incomplete = [], []
    for index in range(len(records)):
        try:
            evaluated.append(_hole_to_bend_one(ctx, index))
        except DfmEvidenceError as exc:
            incomplete.append(str(exc))
    violations = [row for row in evaluated if row["status"] == "warn"]
    if violations:
        first = violations[0]
        regions = [r for row in violations for r in row["regions"]]
        proposals = [p for row in violations for p in row["fix_ops"]]
        return make_row(
            "hole_to_bend", "warn", f"{len(violations)} bends have holes too close to the bend."
            + (" Some bend/hole pairs could not be assessed." if incomplete else ""),
            basis="B-rep cylindrical through-holes and compiled bend tangent frames",
            confidence="geometry_heuristic",
            assumptions=["Each bend and identifiable planar through-hole was screened independently.",
                         "Other hole placements and reliefs may not be assessed."],
            metrics={"bends_with_violations": len(violations), "bends_unassessed": len(incomplete)},
            limits=first["limits"], regions=regions[:ctx.limit], fix=first["fix"],
            fix_ops=proposals[:ctx.limit], omitted=max(0, len(regions) - ctx.limit),
            detail=ctx.detail, rule=first.get("rule"))
    if incomplete:
        raise DfmEvidenceError("; ".join(incomplete))
    return make_row("hole_to_bend", "pass", "Screened bores meet all evaluated bend distances.",
                    basis="B-rep cylindrical through-holes and compiled bend tangent frames",
                    confidence="geometry_heuristic",
                    metrics={"bends_checked": len(evaluated)},
                    rule=ctx.rule_source("hole_to_bend_thickness_factor"))


def _hole_to_edge(ctx):
    machining = ctx.process in ("cnc_mill", "lathe")
    key = "hole_to_edge_diameter_factor" if machining else "hole_to_edge_thickness_factor"
    factor = _positive(ctx.rule(key), key)
    source = ctx.rule_source(key)
    if not source:
        raise DfmEvidenceError("hole-to-edge rule has no citation")
    thickness = None
    if not machining:
        records = (cb.call("eval_sheet_metal_bends", {"doc_id": ctx.doc_id}) or {}).get("records") or []
        thicknesses = {_positive(r.get("thickness_mm"), "thickness") for r in records}
        if len(thicknesses) != 1:
            raise DfmEvidenceError("a single sheet thickness is required")
        thickness = next(iter(thicknesses))
    faces = ctx.faces()
    if len({f.get("body") for f in faces if f.get("body") is not None}) > 1:
        raise DfmEvidenceError("face-wire measurement currently supports a single body")
    by_sid = {f.get("sid"): f for f in faces if f.get("sid")}
    graph = cb.call("doc_adjacency", {"doc_id": ctx.doc_id}) or {}
    adjacent = {}
    for edge in graph.get("edges") or []:
        pair = edge.get("between_sids") or []
        if len(pair) == 2:
            adjacent.setdefault(pair[0], set()).add(pair[1])
            adjacent.setdefault(pair[1], set()).add(pair[0])
    bores = [f for f in faces if f.get("surf") == "cylinder" and f.get("concavity") == "bore"]
    if not bores:
        raise DfmEvidenceError("no cylindrical bore faces were identified")
    measurements, incomplete = [], 0
    for bore in bores:
        found = []
        try:
            axis = _unit(bore.get("axis"))
            origin = _vector(bore.get("axis_point"))
            radius = _positive(bore.get("radius"), "hole radius")
            for sid in adjacent.get(bore.get("sid"), ()):
                face = by_sid.get(sid, {})
                if face.get("surf") not in ("plane", "flat"):
                    continue
                try:
                    if abs(abs(_dot(axis, _unit(face.get("normal")))) - 1) > 1e-6:
                        continue
                    wires = cb.call("doc_face_wires", {"doc_id": ctx.doc_id, "face": {"sid": sid}}) or {}
                    if wires.get("approx"):
                        continue
                    matrix = wires.get("plane_frame")
                    if not isinstance(matrix, list) or len(matrix) < 16:
                        continue
                    frame_origin = [matrix[3], matrix[7], matrix[11]]
                    u, v = [matrix[0], matrix[4], matrix[8]], [matrix[1], matrix[5], matrix[9]]
                    delta = [x-y for x, y in zip(origin, frame_origin)]
                    point = [_dot(delta, u), _dot(delta, v)]
                    outer = next((w for w in wires.get("wires") or [] if w.get("outer")), None)
                    entities = {e.get("id"): e for e in wires.get("entities") or []}
                    boundary = [entities.get(i) for i in (outer or {}).get("entity_ids", [])]
                    if not boundary or any(not e or e.get("type") != "line" for e in boundary):
                        continue
                    distances = []
                    for entity in boundary:
                        a, b = entity["p1"], entity["p2"]
                        dx, dy = b[0]-a[0], b[1]-a[1]
                        length2 = dx*dx + dy*dy
                        if length2 <= 0:
                            raise ValueError("zero-length boundary edge")
                        t = max(0, min(1, ((point[0]-a[0])*dx + (point[1]-a[1])*dy) / length2))
                        distances.append(math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy))
                    found.append((min(distances) - radius, face))
                except (ValueError, KeyError, TypeError):
                    continue
        except DfmEvidenceError:
            incomplete += 1
            continue
        if not found:
            incomplete += 1
            continue
        distance, face = min(found, key=lambda item: item[0])
        threshold = factor * (2 * radius if machining else thickness)
        measurements.append((distance, threshold, region(faces=[bore.get("sid"), face.get("sid")],
            built_by=[bore.get("owner_feature"), face.get("owner_feature")],
            centroid=bore.get("centroid"), hole_edge_distance_mm=distance, minimum_mm=threshold)))
    findings = sorted((item for item in measurements if item[0] < item[1]), key=lambda x: x[0])
    if not findings and incomplete:
        raise DfmEvidenceError(f"{incomplete} bore faces lack a measurable planar line-segment outer boundary")
    if not measurements:
        raise DfmEvidenceError("no hole-to-edge distance could be measured")
    return make_row("hole_edge_distance" if machining else "hole_to_edge", "warn" if findings else "pass",
                    "Some hole edges are too close to the part boundary" if findings
                    else "Identified bores meet the screened edge distance",
                    basis="cylindrical bore adjacency and exact planar outer-wire line segments",
                    confidence="measured",
                    assumptions=["Only single-body holes on planar faces with line-only outer wires are assessed."]
                                + ([f"{incomplete} bore faces could not be assessed."] if incomplete else []),
                    metrics={"holes_checked": len(measurements), "holes_unassessed": incomplete},
                    limits={("diameter_factor" if machining else "minimum_mm"):
                            factor if machining else factor * thickness},
                    regions=[r for _, _, r in findings[:ctx.limit]],
                    omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
                    fix=["Move the hole farther from the part edge."] if findings else [], rule=source)


def rows(ctx, names):
    producers = {"bend_radius": _radius, "flange_length": _flange, "k_factor": _k_factor,
                 "hole_to_bend": _hole_to_bend, "hole_to_edge": _hole_to_edge}
    unavailable = {
        "bend_relief": "bend-end relief width and depth on the current sheet are unavailable",
    }
    out = []
    for name in names:
        if name in unavailable:
            out.append(not_evaluated(name, unavailable[name]))
            continue
        try:
            row = producers[name](ctx)
            if row.get("fix_ops"):
                if ctx.options.get("verify_fixes", True):
                    from services.mcp.dfm_fix_verify import verify_row_fixes
                    row = verify_row_fixes(ctx, row, lambda fresh: producers[name](fresh))
                else:
                    row["fix_ops"] = []
            out.append(row)
        except Exception as exc:  # noqa: BLE001 - each missing source affects only its own row
            out.append(not_evaluated(name, str(exc) or exc.__class__.__name__))
    return out
