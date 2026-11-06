"""Machining DfM screens based on authored holes and exact edge-analysis evidence.

The mill screens cover straight concave edges and cylindrical blends parallel to the
selected tool axis only; they are not a tool-accessibility or collision analysis.
"""
from __future__ import annotations

import re
import math

from services.mcp.dfm_common import (DfmEvidenceError, fix_proposal, make_row,
                                     not_applicable, not_evaluated, num, op_edit_param,
                                     op_fillet, region, unit)
from services.research.machining_reference import machining_ref_get
from services.research.machining import tap_drill

PROCESSES = frozenset(("cnc_mill", "lathe"))
CHECKS = ("sharp_internal_corners", "internal_corner_radius", "hole_depth_diameter",
          "drill_size", "pocket_depth_reach", "pocket_depth_width", "hole_edge_distance",
          "counterbore_fastener", "countersink_fastener", "tap_drill_size",
          "tap_engagement", "tolerance_feasibility")


def _parallel(a, b):
    try:
        x, y = unit(a), unit(b)
    except (TypeError, ValueError):
        return False
    # Floating-point direction comparison, not a manufacturing angular tolerance.
    return abs(abs(sum(p * q for p, q in zip(x, y))) - 1.0) <= 1e-9


def _cam_tool(ctx, diameter=None):
    from services.mcp import cam_tools, core_bridge as cb
    job = cam_tools._CAM.get("doc_id")
    if not job:
        return None
    try:
        tree = (cb.call("cam_tree", {"doc_id": job}) or {}).get("tree") or {}
        library = tree.get("tools") or []
        if isinstance(library, dict):
            library = library.get("tools") or []
        tools = [t for t in library if isinstance(t, dict) and t.get("type") == "endmill_flat"
                 and num(t.get("diameter")) is not None and num(t["diameter"]) > 0
                 and (diameter is None or math.isclose(num(t["diameter"]), diameter, abs_tol=1e-7))]
        return min(tools, key=lambda t: num(t["diameter"])) if tools else None
    except Exception:
        return None


def _tool(ctx):
    raw = ctx.options.get("tool_diameter_mm")
    if raw is not None:
        diameter = num(raw)
        source = "Caller-specified tool_diameter_mm"
        assumption = "Uses the caller-specified end mill diameter."
    else:
        selected = _cam_tool(ctx)
        if selected:
            diameter = num(selected["diameter"])
            source = f"Current CAM job tool library, tool {selected.get('number')}"
            assumption = "Uses the smallest flat end mill in the current CAM job; suitability and job association must be confirmed."
        else:
            diameter = num(ctx.rule_or_none("min_tool_diameter_mm"))
            source = ctx.rule_source("min_tool_diameter_mm")
            assumption = "Uses the profile's cited smallest assumed end mill, not an actual tool library."
            if not source:
                diameter = None
    if diameter is None or diameter <= 0:
        raise DfmEvidenceError("A positive tool_diameter_mm, CAM flat end mill, or cited profile tool is required")
    return diameter, source, assumption


def _edge_data(ctx):
    result = ctx.edge_analysis(min_dihedral_dev_deg=0, kinds=("concave",))
    if result.get("truncated") or result.get("blends_truncated") or result.get("complete") is False:
        raise DfmEvidenceError("edge analysis is incomplete or truncated")
    if result.get("unclassified_count"):
        raise DfmEvidenceError("edge analysis includes unclassified edges")
    return result


def _owners(ctx, ids, edge_owner=None):
    owners = [edge_owner]
    for sid in ids:
        face = ctx.face(sid)
        if face:
            owners.append(face.get("owner_feature"))
    return owners


def _verified_corner(ctx, op, point, minimum, target):
    """Accept only a rebuilt clone with a qualifying nearby cylindrical blend."""
    from services.mcp.dfm_common import DfmContext
    from services.mcp.dfm_fix_verify import verify_fix_ops

    def measure(doc_id):
        clone = DfmContext(doc_id, ctx.process, ctx.profile, ctx.direction,
                           ctx.direction_source, options=ctx.options)
        data = _edge_data(clone)
        candidates = []
        for blend in data.get("concave_blends", []):
            center = blend.get("center")
            radius = num(blend.get("radius_mm"))
            if (blend.get("surf") == "cylinder" and radius is not None
                    and isinstance(center, (list, tuple)) and len(center) == 3
                    and _parallel(blend.get("axis"), ctx.direction)):
                distance = math.dist(point, center)
                if distance <= 2 * target:
                    candidates.append({"radius_mm": radius, "distance_mm": distance,
                                       "face_sid": blend.get("face_sid")})
        return candidates

    evidence = verify_fix_ops(ctx.doc_id, [op], measure)
    before = evidence.get("before") or []
    after = evidence.get("after") or []
    measured = max((x["radius_mm"] for x in after), default=0)
    previous = max((x["radius_mm"] for x in before), default=0)
    if (not evidence.get("builds") or evidence.get("error")
            or measured + 1e-6 < minimum or measured <= previous + 1e-6):
        return None
    return evidence

def _corners(ctx, name):
    if ctx.process != "cnc_mill":
        return not_applicable(name, "End-mill corner geometry applies to milling, not turning.")
    diameter, tool_source, assumption = _tool(ctx)
    ratio = num(ctx.rule("internal_corner_radius_tool_ratio_min"))
    if ratio is None or ratio <= 0:
        raise DfmEvidenceError("internal_corner_radius_tool_ratio_min must be positive")
    minimum = diameter * ratio / 2
    recommended = num(ctx.rule_or_none("internal_corner_radius_tool_ratio_recommended"))
    target = diameter * max(ratio, recommended or ratio) / 2
    source = ctx.rule_source("internal_corner_radius_tool_ratio_min")
    data = _edge_data(ctx)
    findings = []
    if name == "sharp_internal_corners":
        for edge in data.get("edges", []):
            if edge.get("kind") != "concave" or edge.get("curve") != "line":
                continue
            try:
                unit(edge.get("dir"))
            except ValueError as exc:
                raise DfmEvidenceError("concave straight edge direction unavailable") from exc
            if not _parallel(edge["dir"], ctx.direction):
                continue
            p, faces = edge.get("mid"), edge.get("faces") or []
            if not isinstance(p, (list, tuple)) or len(p) != 3 or len(faces) != 2:
                raise DfmEvidenceError("concave edge is missing midpoint or adjacent face IDs")
            findings.append((region(faces=faces, built_by=_owners(ctx, faces, edge.get("owner_feature")),
                                    centroid=p, edge_sid=edge.get("sid"), radius_mm=0), p))
    else:
        for blend in data.get("concave_blends", []):
            if blend.get("surf") != "cylinder":
                continue
            try:
                unit(blend.get("axis"))
            except ValueError as exc:
                raise DfmEvidenceError("concave cylindrical blend axis unavailable") from exc
            if not _parallel(blend["axis"], ctx.direction):
                continue
            radius = num(blend.get("radius_mm"))
            if radius is None or radius <= 0:
                raise DfmEvidenceError("concave cylindrical blend is missing its radius")
            if radius >= minimum:
                continue
            p = blend.get("center")
            faces = [blend.get("face_sid")] + (blend.get("adjacent_faces") or [])
            if not isinstance(p, (list, tuple)) or len(p) != 3 or not blend.get("face_sid"):
                raise DfmEvidenceError("concave blend is missing its center or face ID")
            owner = ctx.feature(blend.get("owner_feature"))
            op = (op_edit_param(owner["id"], radius=target)
                  if owner and owner.get("type") == "fillet"
                  and num((owner.get("params") or {}).get("radius")) is not None else None)
            findings.append((region(faces=faces, built_by=_owners(ctx, faces, blend.get("owner_feature")),
                                    centroid=p, radius_mm=radius), op))
    shown = findings[:ctx.limit]
    proposals = []
    if ctx.options.get("verify_fixes", True):
        for location, value in shown[:5]:
            op = (op_fillet("Mill corner radius", target, [value])
                  if name == "sharp_internal_corners" else value)
            if op is None:
                continue
            evidence = _verified_corner(ctx, op, location["centroid"], minimum, target)
            if evidence:
                proposals.append(fix_proposal(
                    "Increase the corner radius", [op], rule=source or tool_source or "",
                    kind="fillet" if name == "sharp_internal_corners" else "edit_param",
                    effect="Increase the internal corner radius; verify fit and accessibility.",
                    verified=True, verification=evidence))
    if name == "internal_corner_radius" and findings:
        note = "Existing corner blend is smaller than the selected cutter radius; revise its owning fillet or use a smaller cutter."
    else:
        note = ("Sharp vertical internal corners cannot be made by the selected end mill."
                if findings else "No sharp vertical straight concave edges found in the evaluated direction.")
    if name == "internal_corner_radius" and not findings:
        note = "No undersized vertical cylindrical corner blends found."
    return make_row(name, "fail" if findings else "pass", note, basis="C1 exact edge and concave-blend geometry",
                    confidence="measured", assumptions=[assumption, "Only straight edges and cylindrical blends parallel to the selected direction are screened; access is not checked."],
                    metrics={"tool_diameter_mm": diameter, "finding_count": len(findings)},
                    limits={"minimum_radius_mm": minimum, "proposed_radius_mm": target},
                    regions=[r for r, _ in shown],
                    fix=["Increase the corner radius or select a smaller cutter."] if findings else [],
                    fix_ops=proposals,
                    omitted=max(0, len(findings) - len(shown)), detail=ctx.detail, rule=source)


def _hole_faces(ctx, feature):
    fid = feature.get("id")
    return [f for f in ctx.faces() if fid and f.get("owner_feature") == fid
            and f.get("surf") == "cylinder"]


def _holes(ctx):
    return [f for f in ctx.features() if str(f.get("type", "")).casefold() == "hole"]


def _hole_depth(ctx):
    name = "hole_depth_diameter"
    limit = num(ctx.rule("hole_max_depth_to_dia_standard"))
    if limit is None or limit <= 0:
        raise DfmEvidenceError("hole_max_depth_to_dia_standard must be positive")
    holes = _holes(ctx)
    if not holes:
        raise DfmEvidenceError("No authored hole features; imported and cut-profile holes are not measured")
    findings, unknown = [], []
    for f in holes:
        params = f.get("params") or {}
        diameter, depth = num(params.get("diameter", params.get("dia"))), num(params.get("depth"))
        end = str(params.get("end") or "blind").casefold()
        if end != "blind" or diameter is None or depth is None or diameter <= 0 or depth <= 0:
            unknown.append(str(f.get("id")))
            continue
        if depth / diameter <= limit:
            continue
        faces = _hole_faces(ctx, f)
        located = [face for face in faces if face.get("sid") and face.get("centroid") is not None]
        if not located:
            unknown.append(str(f.get("id")))
            continue
        face = located[0]
        r = region(faces=[x.get("sid") for x in faces], built_by=[f.get("id")],
                   centroid=face["centroid"], diameter_mm=diameter, depth_mm=depth, depth_to_diameter=depth / diameter)
        op = op_edit_param(f["id"], depth=diameter * limit)
        findings.append((r, op))
    if unknown:
        return not_evaluated(name, "Hole depth, diameter, or located bore geometry unavailable for: " + ", ".join(unknown),
                             regions=[r for r, _ in findings[:ctx.limit]])
    shown = findings[:ctx.limit]
    source = ctx.rule_source("hole_max_depth_to_dia_standard")
    fixes = [fix_proposal("Shorten the hole if its function permits", [op], rule=source or "", kind="edit_param",
                          effect="Bring the authored depth within the standard-drill planning ratio; alternatively select deep-hole tooling.") for _, op in shown]
    return make_row(name, "warn" if findings else "pass", "Review drill reach and chip evacuation for deep holes." if findings else "Authored blind holes are within the standard-drill depth ratio.",
                    basis="Authored blind hole dimensions", confidence="authored_params",
                    assumptions=["Does not measure through holes or holes made by other feature types."],
                    limits={"depth_to_diameter_max": limit}, metrics={"holes_checked": len(holes)},
                    regions=[r for r, _ in shown], fix=["Shorten the hole if function permits, or use deep-hole tooling."] if findings else [],
                    fix_ops=fixes,
                    omitted=max(0, len(findings) - len(shown)), detail=ctx.detail, rule=source)


def _drill_size(ctx):
    name = "drill_size"
    record = machining_ref_get("metric-preferred-drill-series")
    if not record.get("ok"):
        raise DfmEvidenceError("ISO 235 preferred drill series unavailable")
    # ISO 235:2016 Tables 1-3; ranges and increments come from the curated reference.
    table = record["record"]["table"]["rows"]
    holes = _holes(ctx)
    if not holes:
        raise DfmEvidenceError("No authored hole features; geometry-derived bore sizes are unavailable")
    findings, unknown = [], []
    for f in holes:
        d = num((f.get("params") or {}).get("diameter", (f.get("params") or {}).get("dia")))
        series = None
        if d is not None:
            for span, step in table:
                lo, hi = (float(x) for x in span.split("-"))
                if lo <= d <= hi:
                    series = (lo, hi, float(step))
                    break
        if series is None:
            unknown.append(str(f.get("id")))
            continue
        lo, hi, step = series
        nearest = min(hi, max(lo, lo + round((d - lo) / step) * step))
        if math.isclose(d, nearest, rel_tol=0, abs_tol=1e-7):
            continue
        faces = _hole_faces(ctx, f)
        located = [face for face in faces if face.get("sid") and face.get("centroid") is not None]
        if not located:
            unknown.append(str(f.get("id")))
            continue
        findings.append((region(faces=[face.get("sid") for face in faces], built_by=[f.get("id")],
                                centroid=located[0]["centroid"], diameter_mm=d, preferred_diameter_mm=nearest),
                         op_edit_param(f["id"], diameter=nearest)))
    if unknown:
        return not_evaluated(name, "Preferred drill size or located bore geometry unavailable for: " + ", ".join(unknown),
                             regions=[r for r, _ in findings[:ctx.limit]])
    source = "ISO 235:2016 Tables 1-3, preferred metric twist drill diameters"
    shown = findings[:ctx.limit]
    fixes = [fix_proposal("Consider the preferred drill diameter if fit permits", [op], rule=source, kind="edit_param",
                          effect="Select a preferred metric diameter; confirm fit, tolerance and stocked tooling.") for _, op in shown]
    return make_row(name, "warn" if findings else "pass", "Some authored holes use non-preferred metric drill sizes." if findings else "Authored hole diameters are in the preferred metric series.",
                    basis="Authored hole diameters and preferred metric series", confidence="table",
                    assumptions=["Non-preferred sizes may be intentional; tapped, reamed and bored holes may use other tooling."],
                    metrics={"holes_checked": len(holes)}, regions=[r for r, _ in shown],
                    fix=["Consider a preferred drill diameter if fit permits."] if findings else [],
                    fix_ops=fixes, omitted=max(0, len(findings) - len(shown)), detail=ctx.detail, rule=source)


def _located_feature(ctx, f):
    faces = [x for x in ctx.faces() if x.get("owner_feature") == f.get("id") and x.get("sid")]
    located = next((x for x in faces if x.get("centroid") is not None), None)
    if located is None:
        raise DfmEvidenceError(f"Located surviving faces unavailable for feature {f.get('id')}")
    return faces, located["centroid"]


def _tap_engagement(ctx):
    from services.mcp import core_bridge as cb
    reference = machining_ref_get("nord-lock-thread-engagement")
    if not reference.get("ok"):
        raise DfmEvidenceError("Cited thread engagement guidance is unavailable")
    material = (cb.call("eval_mass", {"doc_id": ctx.doc_id}) or {}).get("material") or {}
    if not isinstance(material, dict) or material.get("source") in (None, "default", "per_body"):
        raise DfmEvidenceError("Applied tapped-hole material is unavailable or mixed")
    key = str(material.get("key") or "").lower()
    if key.startswith(("aluminum_", "alu_")):
        category = "aluminum"
    elif key.startswith(("steel_", "stainless_")):
        category = "steel"
    else:
        raise DfmEvidenceError(f"No cited engagement guidance for material {key!r}")
    ratio = next((num(row[1]) for row in reference["record"]["table"]["rows"]
                  if row[0] == category), None)
    if ratio is None or ratio <= 0:
        raise DfmEvidenceError("Thread engagement ratio is unavailable")
    threads = [f for f in ctx.features() if f.get("type") == "thread"
               and str((f.get("params") or {}).get("style", "external")).casefold()
               in ("internal", "tapped", "bore") and not f.get("suppressed")]
    if not threads:
        raise DfmEvidenceError("No identifiable authored internal thread features")
    findings, missing = [], []
    for feature in threads:
        params = feature.get("params") or {}
        size = params.get("size")
        match = re.fullmatch(r"M(\d+(?:\.\d+)?)(?:x[\d.]+)?", size, re.IGNORECASE) if isinstance(size, str) else None
        major = num(params.get("major_dia")) or (float(match.group(1)) if match else None)
        length = num(params.get("length"))
        if (major is None or major <= 0 or length is None or length <= 0
                or num(params.get("turns")) not in (None, 0)):
            missing.append(str(feature.get("id")))
            continue
        if length < ratio * major:
            try:
                faces, center = _located_feature(ctx, feature)
            except DfmEvidenceError:
                missing.append(str(feature.get("id")))
                continue
            findings.append(region(faces=[f["sid"] for f in faces], built_by=[feature.get("id")],
                                   centroid=center, available_thread_length_mm=length,
                                   nominal_diameter_mm=major, guideline_engagement_mm=ratio * major))
    source = "Nord-Lock, The Experts: Thread engagement in a tapped hole (steel 1d, aluminium 2d)"
    if not findings:
        raise DfmEvidenceError("Actual assembled fastener engagement is unavailable; adequate authored thread length does not establish it"
                               + ("; unevaluated threads: " + ", ".join(missing) if missing else ""))
    return make_row("tap_engagement", "warn", "The available internal thread length is shorter than the engagement guideline.",
                    basis="authored internal-thread length and material-class planning guideline",
                    confidence="authored_params",
                    assumptions=["Authored thread length bounds available engagement; actual screw engagement and joint strength are not measured."]
                                + (["Some threads lack evaluable geometry: " + ", ".join(missing)] if missing else []),
                    metrics={"threads_checked": len(threads) - len(missing)},
                    limits={"engagement_to_diameter": ratio},
                    regions=findings[:ctx.limit], omitted=max(0, len(findings) - ctx.limit),
                    detail=ctx.detail, rule=source,
                    fix=["Increase available thread length if geometry and joint design permit."])


def _feature_findings(ctx, name, findings, source, checked, description):
    shown = findings[:ctx.limit]
    fixes = [fix_proposal(label, [op], rule=source or "", kind="edit_param",
                          effect="Confirm functional dimensions and tooling before applying.")
             for _, op, label in shown]
    return make_row(name, "warn" if findings else "pass", description, basis="Authored feature dimensions",
                    confidence="authored_params", metrics={"features_checked": checked},
                    assumptions=["Only the explicitly identified authored features are screened."],
                    regions=[r for r, _, _ in shown], fix=[label for _, _, label in shown],
                    fix_ops=fixes, rule=source,
                    omitted=max(0, len(findings) - len(shown)), detail=ctx.detail)


def _pocket_depth(ctx):
    name = "pocket_depth_reach"
    if ctx.process != "cnc_mill":
        return not_applicable(name, "End-mill pocket reach applies to milling, not turning.")
    diameter, _, _ = _tool(ctx)
    tool = _cam_tool(ctx, diameter)
    reach = num(tool.get("flute_length")) if tool else None
    source = (f"Current CAM job tool {tool.get('number')} flute_length" if reach and reach > 0
              else ctx.rule_source("pocket_max_depth_to_tool_dia"))
    if reach is None or reach <= 0:
        limit = num(ctx.rule_or_none("pocket_max_depth_to_tool_dia"))
        if limit is None or limit <= 0 or not source:
            raise DfmEvidenceError("Selected tool flute length or cited pocket reach rule is required")
        reach = diameter * limit
    pockets = [f for f in ctx.features()
               if f.get("type") == "extrude" and (f.get("params") or {}).get("mode") == "cut"]
    if not pockets:
        raise DfmEvidenceError("No authored cut extrusions; other pocket geometry is not measured")
    findings = []
    for f in pockets:
        p = f["params"]
        depth = num(p.get("depth"))
        if (p.get("end") or "blind") != "blind" or depth is None or depth <= 0:
            raise DfmEvidenceError(f"Blind pocket depth unavailable for feature {f.get('id')}")
        if depth > reach:
            faces, center = _located_feature(ctx, f)
            findings.append((region(faces=[x["sid"] for x in faces], built_by=[f["id"]],
                                    centroid=center, depth_mm=depth, tool_diameter_mm=diameter,
                                    depth_to_tool_diameter=depth / diameter, flute_reach_mm=reach),
                             op_edit_param(f["id"], depth=reach),
                             "Reduce pocket depth if function permits"))
    return _feature_findings(ctx, name, findings, source, len(pockets),
                             "Blind authored pocket depth screened against the selected flute reach or cited planning rule; access and pocket width are not measured.")


def _fastener_checks(ctx, name):
    holes = _holes(ctx)
    style = "cbore" if name == "counterbore_fastener" else "tap"
    holes = [f for f in holes if (f.get("params") or {}).get("style") == style]
    if not holes:
        raise DfmEvidenceError(f"No authored {style} holes; other hole geometry is not classified")
    ref = machining_ref_get("metric-shcs-counterbore") if style == "cbore" else None
    if ref is not None and not ref.get("ok"):
        raise DfmEvidenceError("Metric socket-head counterbore reference unavailable")
    table = ref["record"]["table"]["rows"] if ref else []
    source = ("ISO 4762:2004 head dimensions / ISO 273:1979 normal clearance; "
              "metric socket-head counterbore planning table" if ref else
              "Machinery's Handbook 31st ed. tap-drill sizes / ISO 2306 / ASME B94.9")
    findings = []
    for f in holes:
        p = f["params"]
        designation = next((p.get(k) for k in ("thread", "tap", "screw", "fastener", "bolt", "size")
                            if isinstance(p.get(k), str) and p.get(k)), None)
        if designation is None:
            raise DfmEvidenceError(f"Fastener/thread designation unavailable for feature {f.get('id')}")
        changes = {}
        if style == "cbore":
            spec = next((r for r in table if r[0].casefold() == designation.casefold()), None)
            shaft, bore, depth = num(p.get("diameter")), num(p.get("cbore_d")), num(p.get("cbore_depth"))
            if spec is None or any(v is None or v <= 0 for v in (shaft, bore, depth)):
                raise DfmEvidenceError(f"Counterbore dimensions or matching fastener table unavailable for {f.get('id')}")
            for key, measured, required in (("diameter", shaft, spec[1]), ("cbore_d", bore, spec[2]),
                                            ("cbore_depth", depth, spec[3])):
                if measured < required:
                    changes[key] = required
        else:
            answer = tap_drill(designation)
            diameter = num(p.get("diameter"))
            target = num((answer.get("answer") or {}).get("tap_drill_mm"))
            if answer.get("ok") is False or target is None or diameter is None or diameter <= 0:
                raise DfmEvidenceError(f"Tap drill size or diameter unavailable for {f.get('id')}")
            if not math.isclose(diameter, target, rel_tol=0, abs_tol=1e-7):
                changes["diameter"] = target
        if changes:
            faces, center = _located_feature(ctx, f)
            findings.append((region(faces=[x["sid"] for x in faces], built_by=[f["id"]],
                                    centroid=center, designation=designation, proposed_params=changes),
                             op_edit_param(f["id"], **changes),
                             "Consider the matching fastener or tap-drill dimensions"))
    return _feature_findings(ctx, name, findings, source, len(holes),
                             "Compare authored holes with selected fastener or tap-drill planning dimensions; verify actual purchased hardware and functional fit.")


def _pocket_width(ctx):
    from services.mcp import core_bridge as cb
    if ctx.process != "cnc_mill":
        return not_applicable("pocket_depth_width", "Pocket width relative to end mills applies to milling.")
    diameter, source, assumption = _tool(ctx)
    faces = {f.get("sid"): f for f in ctx.faces() if f.get("sid")}
    neighbors = {}
    for edge in _edge_data(ctx).get("edges", []):
        if edge.get("kind") != "concave":
            continue
        pair = edge.get("faces") or []
        if len(pair) == 2:
            neighbors.setdefault(pair[0], set()).add(pair[1])
            neighbors.setdefault(pair[1], set()).add(pair[0])
    measurements = []
    for floor_sid, sides in neighbors.items():
        floor = faces.get(floor_sid, {})
        if floor.get("surf") not in ("plane", "flat") or not _parallel(floor.get("normal"), ctx.direction):
            continue
        walls = []
        for sid in sides:
            wall = faces.get(sid, {})
            try:
                normal = unit(wall.get("normal"))
                if wall.get("surf") not in ("plane", "flat") or abs(sum(a*b for a, b in zip(normal, ctx.direction))) > 1e-6:
                    continue
                center = [float(v) for v in wall["centroid"]]
            except (ValueError, TypeError, KeyError):
                continue
            walls.append((sid, wall, normal, center))
        for i, (sid_a, a, na, ca) in enumerate(walls):
            for sid_b, b, nb, cbp in walls[i + 1:]:
                if sum(x*y for x, y in zip(na, nb)) > -1 + 1e-6:
                    continue
                if sum((y-x)*n for x, y, n in zip(ca, cbp, na)) <= 0:
                    continue
                try:
                    answer = cb.call("eval_measure", {"doc_id": ctx.doc_id, "refs": [a, b]}) or {}
                    width = num(answer.get("distance"))
                except Exception as exc:
                    raise DfmEvidenceError(f"pocket side separation unavailable: {exc}") from exc
                if width is None or width <= 0:
                    raise DfmEvidenceError("pocket side separation was not measured")
                center = [(x+y)/2 for x, y in zip(ca, cbp)]
                measurements.append((width, region(faces=[floor_sid, sid_a, sid_b],
                    built_by=[floor.get("owner_feature"), a.get("owner_feature"), b.get("owner_feature")],
                    centroid=center, pocket_width_mm=width, tool_diameter_mm=diameter)))
    if not measurements:
        raise DfmEvidenceError("No concave pocket floor with opposed parallel planar walls was identified")
    findings = sorted((x for x in measurements if x[0] < diameter), key=lambda x: x[0])
    return make_row(
        "pocket_depth_width", "fail" if findings else "pass",
        "Opposed pocket walls are closer than the selected cutter diameter" if findings
        else "Screened opposed pocket walls clear the selected cutter diameter",
        basis="concave floor adjacency and exact opposed planar-face separation",
        confidence="measured", assumptions=[assumption,
            "Only identified opposed parallel planar walls sharing a concave floor are screened."],
        metrics={"pairs_checked": len(measurements), "tool_diameter_mm": diameter},
        limits={"minimum_width_mm": diameter}, regions=[r for _, r in findings[:ctx.limit]],
        omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
        fix=["Widen the pocket or select a smaller cutter."] if findings else [], rule=source)


def _countersink(ctx):
    holes = [f for f in _holes(ctx) if (f.get("params") or {}).get("style") == "csink"]
    if not holes:
        raise DfmEvidenceError("No authored countersunk holes with fastener associations")
    reference = machining_ref_get("iso-10642-legacy-head-envelope")
    if not reference.get("ok"):
        raise DfmEvidenceError("Legacy ISO countersunk head table unavailable")
    table = {r[0].upper(): r for r in reference["record"]["table"]["rows"]}
    findings, missing = [], []
    for hole in holes:
        params = hole.get("params") or {}
        designation = next((params.get(k) for k in ("screw", "fastener", "bolt", "size")
                            if isinstance(params.get(k), str) and params.get(k)), "")
        match = re.fullmatch(r"\s*(M\d+)(?:x[\d.]+)?\s*", designation, re.IGNORECASE)
        spec = table.get(match.group(1).upper()) if match else None
        diameter, angle = num(params.get("csink_d")), num(params.get("csink_angle"))
        if spec is None or diameter is None or angle is None or diameter <= 0 or angle <= 0:
            missing.append(str(hole.get("id")))
            continue
        if diameter < spec[1] or not math.isclose(angle, spec[2], abs_tol=1e-6):
            faces, center = _located_feature(ctx, hole)
            findings.append(region(faces=[f["sid"] for f in faces], built_by=[hole["id"]],
                                   centroid=center, designation=spec[0], countersink_diameter_mm=diameter,
                                   minimum_head_diameter_mm=spec[1], countersink_angle_deg=angle,
                                   required_angle_deg=spec[2]))
    if missing and not findings:
        raise DfmEvidenceError("Countersink dimensions or exact supported fastener designation unavailable for: " + ", ".join(missing))
    return make_row(
        "countersink_fastener", "warn" if findings else "pass",
        "Countersink does not match the selected legacy ISO head envelope" if findings
        else "Authored countersinks match the selected legacy ISO head envelope",
        basis="authored countersink dimensions and cited fastener head envelope", confidence="table",
        assumptions=["Historical ISO 10642:1998 head sizes are planning data, not current acceptance limits.",
                     "Confirm purchased head diameter, fit clearance and seating depth."]
                    + (["Some countersinks lack matching evidence: " + ", ".join(missing)] if missing else []),
        metrics={"holes_checked": len(holes) - len(missing)}, regions=findings[:ctx.limit],
        omitted=max(0, len(findings) - ctx.limit), detail=ctx.detail,
        fix=["Use the selected screw's head diameter and included angle."] if findings else [],
        rule="RoyMech ISO 10642 legacy head diameter table; ISO 10642 metric 90-degree head")


def rows(ctx, names):
    out = []
    for name in names:
        try:
            if name in ("sharp_internal_corners", "internal_corner_radius"):
                result = _corners(ctx, name)
            elif name == "hole_depth_diameter":
                result = _hole_depth(ctx)
            elif name == "drill_size":
                result = _drill_size(ctx)
            elif name == "pocket_depth_reach":
                result = _pocket_depth(ctx)
            elif name in ("counterbore_fastener", "tap_drill_size"):
                result = _fastener_checks(ctx, name)
            elif name == "pocket_depth_width":
                result = _pocket_width(ctx)
            elif name == "hole_edge_distance":
                from services.mcp.dfm_rows_sheetmetal import _hole_to_edge
                try:
                    result = _hole_to_edge(ctx)
                except DfmEvidenceError:
                    raise
                except Exception as exc:
                    raise DfmEvidenceError(f"hole-edge geometry unavailable: {exc}") from exc
            elif name == "countersink_fastener":
                result = _countersink(ctx)
            elif name == "tap_engagement":
                result = _tap_engagement(ctx)
            elif name == "tolerance_feasibility":
                result = not_evaluated(name, "Feature-specific drawing tolerances and machining operations are not available in the part feature tree; ISO capability tables cannot be applied without them.")
            else:
                raise ValueError(f"unknown machining check: {name}")
        except DfmEvidenceError as exc:
            result = not_evaluated(name, str(exc))
        if name not in ("sharp_internal_corners", "internal_corner_radius") and result.get("fix_ops"):
            if ctx.options.get("verify_fixes", True):
                from services.mcp.dfm_fix_verify import verify_row_fixes
                result = verify_row_fixes(ctx, result, lambda fresh: rows(fresh, [name])[0])
            else:
                result["fix_ops"] = []
        out.append(result)
    return out
