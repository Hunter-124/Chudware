"""Geometry-based stress-riser screening and bounded native evaluation tools.

Severity is a dimensionless geometry-screening priority, not a stress, safety factor,
fatigue assessment, or prediction of failure. No loading is inferred from geometry.
"""
from __future__ import annotations

import math
from typing import Optional
from statistics import median

from mcp.server.fastmcp import FastMCP

from services.mcp import core_bridge as cb
from services.mcp.cad_common import _doc, _dumps, _err
from services.mcp.dfm_common import _flatten_features, num

_LEVEL = {"low": 0, "medium": 1, "high": 2}
_MAX_EDGE_PAGES = 10
_MAX_BODIES = 64


def _page_args(offset, limit):
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
        raise ValueError("offset must be an integer between 0 and 10000")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")


def _body_arg(body):
    if body is not None and (isinstance(body, bool) or not isinstance(body, int) or body < 1):
        raise ValueError("body must be a positive integer")


def _call(method, params):
    result = cb.call(method, params)
    if not isinstance(result, dict):
        raise ValueError(f"{method} failed: no result object")
    if result.get("ok") is False:
        raise ValueError(f"{method} failed: {result.get('error', 'no result')}")
    return result


def _kt(kind):
    reason = "Loading, nominal stress definition and applicable idealized geometry are not established; no Kt is inferred."
    source = None
    if kind == "sharp_concave_edge":
        reason += (" A re-entrant elastic corner can be singular depending on loading and boundary conditions;"
                   " in such a singular idealization no finite mesh-independent peak Kt exists.")
        source = ("M. L. Williams, Stress Singularities Resulting from Various Boundary Conditions "
                  "in Angular Corners of Plates in Extension, Journal of Applied Mechanics (1952).")
    return {"value": None, "source": source, "status": "not_evaluated", "reason": reason}


def _fillet_proposal(edge, selector, minimum):
    if (not isinstance(selector, dict) or selector.get("kind") != "edge"
            or selector.get("sid") != edge.get("sid") or not selector.get("centroid")
            or minimum is None or minimum <= 0):
        return None
    radius = minimum * 0.25
    return {"tool": "add_features", "ops": [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "Stress relief fillet",
        "params": {"type": "constant", "radius": {"value": radius, "expr": None}},
        "refs": {"edges": [selector]}}}],
        "radius_basis": ("Trial radius is 25% of the body's sampled minimum thickness from eval_thickness; "
                         "this is a cautious starting geometry, not a local wall measurement, a guaranteed "
                         "conservative bound, or a Kt-based design requirement."),
        "requested_radius_mm": radius, "verification": {"status": "not_run", "builds": None}}


def _vec3(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    result = [num(x) for x in value]
    return result if all(x is not None for x in result) else None


def _ligament_fix(item, face, features):
    """Propose only a single, explicitly world-positioned hole edit with a measured direction."""
    owner = item.get("owner_feature")
    matches = [f for f in features if f.get("id") == owner] if owner else []
    if len(matches) != 1 or matches[0].get("type") != "hole":
        return None, "No unique owning hole feature; inspect the nearest boundary and edit the relevant hole or edge distance."
    params = matches[0].get("params") or {}
    points = params.get("world_points")
    if (not isinstance(points, list) or len(points) != 1 or params.get("positions") or params.get("points")
            or _vec3(points[0]) is None or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) for x in points[0])):
        return None, "The owning hole lacks one unambiguous literal world_points coordinate; edit its location or the boundary distance manually."
    if item.get("kind") == "bore_bore" and item.get("other_owner_feature") == owner:
        return None, "Both bores share the same owner; moving the feature may move both. Edit the individual hole positions manually."
    if not item.get("face_sid") or face.get("sid") != item["face_sid"]:
        return None, "The measured bore face could not be matched to its topology selector; inspect and edit manually."
    point, other = _vec3(item.get("point")), _vec3(item.get("other_point"))
    axis = _vec3(face.get("axis"))
    axis_point = _vec3(face.get("axis_point"))
    authored = _vec3(points[0])
    gap, radius = num(item.get("distance_mm")), num(item.get("radius_mm"))
    if point is None or other is None or axis is None or axis_point is None or gap is None or gap <= 0 or radius is None or radius <= 0:
        return None, "Measured bore axis, finite-face points, and positive gap are required to derive a move direction; inspect and edit manually."
    mag = math.hypot(*axis)
    if not math.isfinite(mag) or mag == 0:
        return None, "Bore axis is undefined; inspect and edit the hole location manually."
    axis = [x / mag for x in axis]
    relative = [authored[i] - axis_point[i] for i in range(3)]
    axial = sum(relative[i] * axis[i] for i in range(3))
    if math.hypot(*(relative[i] - axial * axis[i] for i in range(3))) > 1e-5 * max(1.0, radius):
        return None, "The authored point does not identify this bore axis; inspect and edit manually."
    separation = [point[i] - other[i] for i in range(3)]
    axial = sum(separation[i] * axis[i] for i in range(3))
    direction = [separation[i] - axial * axis[i] for i in range(3)]
    length = math.hypot(*direction)
    if not math.isfinite(length) or length <= 1e-12:
        return None, "Nearest-face separation has no measurable component across the bore axis; no move direction is inferred."
    distance = min(gap, radius)
    moved = [authored[i] + distance * direction[i] / length for i in range(3)]
    return {"tool": "add_features", "ops": [{"op": "edit_feature", "id": owner,
                                               "params": {"world_points": [moved]}}],
            "move_mm": distance, "direction": [x / length for x in direction],
            "basis": ("Trial move away from the measured closest face, projected perpendicular to the measured bore axis; "
                      "distance is the smaller of measured gap and bore radius. This does not establish an improved ligament."),
            "verification": {"status": "not_run", "builds": None}}, None


def _shoulder_cases(d, D, radius):
    """Evaluate only within the chart fit's range under an explicitly assumed span."""
    from services.research.calcs import run_calc

    if not all(math.isfinite(x) and x > 0 for x in (d, D, radius)) or D <= d:
        return None
    cases = {}
    for load in ("tension", "bending"):
        rd = radius / d
        minimum = (-1.89 * (rd - 0.15) + 5.5 if load == "tension"
                   else -2.05 * (rd - 0.025) + 2.0)
        assumed_ratio = max(6.0, minimum + 0.01)
        result = run_calc("stress_concentration", {
            "geometry": "flat_shoulder", "load": load, "d_mm": d, "D_mm": D,
            "r_mm": radius, "L_mm": D * assumed_ratio})
        if not result.get("ok"):
            return None
        cases[load] = {"kt": result["answer"]["kt"], "citation": result["source"]["citation"],
                       "nominal_stress_basis": result["answer"]["nominal_stress_basis"],
                       "validity": result["answer"]["validity"], "assumed_L_over_D": assumed_ratio}
    return cases


def _shoulder_screen(sides, radius):
    """Hypothetical chart ranking, never a Kt validated for the actual part."""
    values = [num(s.get("thickness_mm")) for s in sides]
    out = {"status": "not_evaluated", "basis": "geometry screen, assumed flat-bar shoulder, load direction unknown",
           "assumptions": ["Side thicknesses are used as hypothetical flat-bar widths; actual shoulder geometry and load are not established.",
                           "Transition separation L is not measured; L/D is assumed large enough for each chart fit.",
                           "Linear-elastic isolated-notch chart; not a strength or fatigue prediction."],
           "sides": sides}
    if len(values) != 2 or any(v is None or v <= 0 for v in values):
        out["reason"] = "Two reliable local side-thickness samples are unavailable."
        return out
    d, D = sorted(values)
    out.update({"assumed_d_mm": d, "assumed_D_mm": D, "D_over_d": D / d})
    if D <= d or math.isclose(D, d, rel_tol=1e-6, abs_tol=1e-9):
        out["reason"] = "No resolvable step between sampled side thicknesses."
        return out
    if radius is None or radius <= 0:
        out["reason"] = "A positive root radius is required for a finite shoulder chart."
        return out
    out["r_over_d"] = radius / d
    cases = _shoulder_cases(d, D, radius)
    if cases is None:
        out["reason"] = "Sampled geometry is outside the documented tension or bending chart-fit domain."
        return out
    out.update({"status": "hypothetical", "cases": cases,
                "band": [min(c["kt"] for c in cases.values()), max(c["kt"] for c in cases.values())],
                "reason": "Illustrative chart values under assumed geometry and span; not actual-part Kt."})
    return out


def _local_sides(doc, revision, row, is_blend):
    faces = row.get("adjacent_face_topo" if is_blend else "face_topo")
    topo = row.get("topo")
    if not isinstance(faces, list) or len(faces) != 2 or faces[0] == faces[1] or not topo:
        return [], "Two unambiguous adjacent faces are unavailable."
    sides = []
    for face in faces:
        params = {"doc_id": doc, "body": row.get("body"), "face_topo": face,
                  "blend_topo" if is_blend else "edge_topo": topo}
        try:
            result = _call("eval_local_thickness", params)
            if revision is None or result.get("revision") != revision:
                return [], "Local thickness revision was unavailable or changed."
            basis = result.get("measurement_basis") or {}
            spacing = num(basis.get("spacing_mm"))
            if result.get("face_topo") != face or spacing is None or spacing <= 0 or basis.get("budget_limited"):
                return [], "Local face identity, resolution or sampling budget is insufficient."
            samples = result.get("samples") or []
            valid = [s for s in samples if isinstance(s, dict) and num(s.get("thickness_mm")) is not None
                     and num(s.get("thickness_mm")) > 0 and num(s.get("offset_mm")) is not None
                     and 0.4 * spacing * (1 - 1e-8) <= num(s.get("offset_mm")) <= 3 * spacing * (1 + 1e-8)
                     and _vec3(s.get("at")) is not None and _vec3(s.get("edge_at")) is not None]
            if not valid:
                return [], str(result.get("unavailable_reason") or "No finite local thickness samples.")[:300]
            sides.append({"face_topo": face, "face_sid": result.get("face_sid"),
                          "thickness_mm": median(num(s["thickness_mm"]) for s in valid),
                          "samples": [{k: s.get(k) for k in ("at", "thickness_mm", "offset_mm", "edge_at")}
                                      for s in valid[:8]],
                          "sample_count": len(valid), "measurement_basis": result.get("measurement_basis")})
        except Exception as exc:  # noqa: BLE001
            return [], f"Local thickness unavailable: {str(exc)[:300]}"
    return sides, None


def _chart_target_radius(sides):
    values = [num(s.get("thickness_mm")) for s in sides]
    if len(values) != 2 or any(v is None or v <= 0 for v in values):
        return None
    d, D = sorted(values)
    h = (D - d) / 2
    if h <= 0 or math.isclose(D, d, rel_tol=1e-6, abs_tol=1e-9):
        return None
    # The chart covers 0.1 <= h/r <= 20. Search a bounded geometric grid without
    # claiming that this hypothetical model or the resulting radius fits the part.
    lo, hi = h / 20, h / 0.1
    for i in range(48):
        radius = lo * (hi / lo) ** (i / 47)
        cases = _shoulder_cases(d, D, radius)
        if cases and max(c["kt"] for c in cases.values()) <= 1.5:
            return radius
    return None


def _verify(doc, proposal, revision):
    try:
        dry = cb.call("doc_dry_run_ops", {"doc_id": doc, "ops": proposal["ops"], "budget_ms": 5000})
        if not isinstance(dry, dict):
            raise ValueError("doc_dry_run_ops returned no result object")
        if revision is None or dry.get("source_revision") != revision:
            proposal["verification"] = {"status": "not_verified", "builds": None, "reason": "Document revision changed or could not be confirmed during analysis."}
            return
        statuses = (dry.get("rebuild") or {}).get("feature_status") or []
        op = proposal["ops"][0] if proposal.get("ops") else {}
        is_edit = op.get("op") == "edit_feature"
        status = (next((s for s in statuses if s.get("id") == op.get("id")), {})
                  if is_edit else (statuses[-1] if statuses else {}))
        report = (status.get("data") or {}).get("fillet") or {}
        effective = num((report.get("effective") or {}).get("radius"))
        selected, covered = report.get("selected_edges"), report.get("covered_edges")
        verified = (dry.get("ok") is True and (dry.get("rebuild") or {}).get("ok") is True
                    and status.get("ok") is True and not status.get("rolled_back")
                    and (is_edit or (effective is not None and effective > 0 and selected == 1 and covered == 1)))
        failed = (not dry.get("skipped") and (dry.get("ok") is False
                  or (dry.get("rebuild") or {}).get("ok") is False
                  or status.get("ok") is False or status.get("rolled_back") is True))
        proposal["verification"] = {
            "status": "built" if verified else "failed" if failed else "not_verified",
            "builds": True if verified else False if failed else None, "source_revision": dry.get("source_revision"),
            "effective_radius_mm": effective, "requested_radius_mm": (report.get("requested") or {}).get("radius"),
            "adjustment": report.get("adjustment"), "selected_edges": selected, "covered_edges": covered,
            "warning": status.get("warning"), "error": dry.get("error") or status.get("error"),
            "note": ("Build evidence only; no stress improvement or fitness is established. "
                     "Any chart target describes the requested, not necessarily the effective, radius.")}
    except Exception as exc:  # noqa: BLE001 -- a missing dry run is not a verified fix
        proposal["verification"] = {"status": "not_verified", "builds": None, "reason": str(exc)[:500]}


def _edge_inventory(doc, body):
    edges, blends = [], []
    first = None
    cursor = 0
    truncated = False
    for _ in range(_MAX_EDGE_PAGES):
        params = {"doc_id": doc, "kinds": ["concave"], "limit": 500, "cursor": cursor,
                  "include_ligaments": cursor == 0}
        if body is not None:
            params["body"] = body
        page = _call("eval_edge_analysis", params)
        if first is None:
            first = page
            blends = page.get("concave_blends") or []
        elif page.get("revision") != first.get("revision"):
            raise ValueError("Document changed during edge pagination; repeat the analysis.")
        edges.extend(page.get("edges") or [])
        if not page.get("truncated"):
            break
        next_cursor = page.get("next_cursor")
        if not isinstance(next_cursor, int) or next_cursor <= cursor:
            truncated = True
            break
        cursor = next_cursor
    else:
        truncated = True
    return first, edges, blends, truncated


def _finding(kind, severity, body, sid, location, owner, evidence):
    identity = ":".join(str(x if x is not None else "-") for x in (
        kind, body, sid, evidence.get("topo"), evidence.get("kind"),
        evidence.get("other_face_sid"), evidence.get("other_topo")))
    return {"id": identity, "kind": kind, "severity": severity, "body": body, "sid": sid,
            "sids": [s for s in [sid] if s], "location": location, "owner_feature": owner,
            "basis": evidence.get("source"), "confidence": "geometry_heuristic",
            "assumptions": ["Geometry screening only; load, stress and suitability are not established."],
            "evidence": evidence, "kt_estimate": _kt(kind)}


def _screen(doc, body, load_hint, min_severity, verify_fixes, offset, limit, local_offset):
    _body_arg(body)
    _page_args(offset, limit)
    _page_args(local_offset, 1)
    if min_severity not in _LEVEL:
        raise ValueError("min_severity must be low, medium or high")
    if load_hint is not None and (not isinstance(load_hint, str) or len(load_hint) > 500):
        raise ValueError("load_hint must be a string of at most 500 characters")
    first, edges, blends, truncated = _edge_inventory(doc, body)
    revision = first.get("revision")
    incomplete = bool(truncated or first.get("complete") is False or first.get("blends_truncated")
                      or first.get("unclassified_count"))
    notes = ["Severity ranks geometry only; it is not a failure or safety assessment.",
             "Kt not computed: load direction, boundary conditions, nominal stress and applicability must be established."]
    if load_hint:
        notes.append("load_hint is recorded, not treated as validated loading or used to infer Kt.")
    if truncated:
        notes.append("Edge scan stopped at its bounded page budget; later edges were not screened.")
    if first.get("blends_truncated"):
        notes.append("Native concave blend inventory was truncated.")
    if first.get("unclassified_count"):
        notes.append(f"{first['unclassified_count']} edges could not be classified by the native scan.")
    if first.get("complete") is False:
        notes.append("Source geometry is incomplete; findings are not a complete inventory.")
    findings = []
    # Minimum thickness is a mesh-derived local measurement, not proof of a structural neck.
    bodies = [body] if body is not None else [b.get("body_id") for b in first.get("bodies", []) if b.get("body_id")]
    if not bodies:
        notes.append("Thickness screening skipped: no body IDs supplied by edge analysis.")
        incomplete = True
    if len(bodies) > _MAX_BODIES:
        notes.append("Thickness screening limited to the first 64 bodies.")
        incomplete = True
    try:
        faces = _call("doc_query", {"doc_id": doc, "query": {"of": "face"}}).get("matches") or []
        face_map = {(f.get("body"), f.get("topo")): f for f in faces if isinstance(f, dict)}
    except Exception as exc:  # noqa: BLE001
        face_map = {}
        notes.append(f"Thickness face attribution unavailable: {str(exc)[:300]}")
        incomplete = True
    thickness_by_body = {}
    for b in bodies[:_MAX_BODIES]:
        try:
            thickness = _call("eval_thickness", {"doc_id": doc, "body": b})
            minimum, mean = num(thickness.get("min_thickness_mm")), num(thickness.get("mean_thickness_mm"))
            if minimum is not None and minimum > 0:
                thickness_by_body[b] = minimum
            sample = thickness.get("minimum_sample") or {}
            if minimum is None or mean is None or mean <= 0 or not sample.get("at"):
                notes.append(f"Body {b}: thickness minimum, mean or location unavailable; neck screening skipped.")
                incomplete = True
                continue
            if minimum / mean >= 0.5:
                continue
            face = face_map.get((b, sample.get("face_id")), {})
            findings.append(_finding("thin_neck_candidate", "medium", b, face.get("sid"), sample.get("at"),
                                     face.get("owner_feature"),
                                     {"source": "eval_thickness", "topo": sample.get("face_id"), "minimum_mm": minimum, "mean_mm": mean,
                                      "minimum_to_mean": minimum / mean, "opposite_at": sample.get("opposite_at"),
                                      "measurement_basis": thickness.get("measurement_basis"),
                                      "note": "Mesh-based local thickness; does not establish a load-bearing neck."}))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Body {b}: thickness screening unavailable: {str(exc)[:300]}")
            incomplete = True
    edge_selectors = {}
    if edges:
        try:
            matches = _call("doc_query", {"doc_id": doc, "query": {"of": "edge"}}).get("matches") or []
            edge_selectors = {(m.get("body"), m.get("topo")): m for m in matches if isinstance(m, dict)}
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Fillet proposals unavailable: edge selector query failed: {str(exc)[:300]}")
            incomplete = True
    local_seen = {False: 0, True: 0}
    local_omitted = 0
    def sample_sides(candidate, is_blend):
        nonlocal local_omitted
        index = local_seen[is_blend]
        local_seen[is_blend] += 1
        if not local_offset <= index < local_offset + 4:
            local_omitted += 1
            return [], "Outside this call's four-candidate local sampling window; use local_offset to inspect it."
        return _local_sides(doc, revision, candidate, is_blend)
    for e in edges:
        if e.get("kind") != "concave":
            continue
        dev = num(e.get("deviation_deg"))
        level = "high"
        row = _finding("sharp_concave_edge", level, e.get("body"), e.get("sid"), e.get("mid"),
                       e.get("owner_feature"), {"source": "eval_edge_analysis", "topo": e.get("topo"), "dihedral_deg": e.get("dihedral_deg"),
                                                "deviation_deg": dev, "length_mm": e.get("length_mm"),
                                                "faces": (e.get("faces") or [])[:2]})
        sides, local_error = sample_sides(e, False)
        row["kt_screen"] = {
            "status": "singular_idealization", "value": "unbounded (sharp re-entrant)",
            "basis": "geometry screen: idealized sharp elastic re-entrant corner; load direction unknown",
            "source": row["kt_estimate"]["source"], "sides": sides,
            "assumptions": ["An elastic re-entrant corner can be singular for applicable loading and boundary conditions; actual loading is unknown."],
            "local_sampling_unavailable": local_error}
        proposal = _fillet_proposal(e, edge_selectors.get((e.get("body"), e.get("topo"))),
                                    thickness_by_body.get(e.get("body")))
        target = _chart_target_radius(sides) if proposal and sides else None
        if target is not None:
            proposal["requested_radius_mm"] = target
            proposal["ops"][0]["feature"]["params"]["radius"]["value"] = target
            proposal["radius_basis"] = ("Hypothetical flat-shoulder tension and bending chart-grid trial targeting Kt <= 1.5; "
                                        "side widths and L are assumed, not validated. The dry run must establish whether this radius builds.")
            proposal["chart_target"] = _shoulder_screen(sides, target)
        if proposal:
            row["fix"] = proposal
        else:
            row["fix_unavailable"] = "A complete matching engine edge selector or sampled body thickness is unavailable."
        findings.append(row)
    for blend in blends:
        radius = num(blend.get("radius_mm"))
        thickness = thickness_by_body.get(blend.get("body"))
        if radius is None or radius <= 0:
            continue
        if thickness is None:
            incomplete = True
            notes.append(f"Body {blend.get('body')}: blend radius/thickness ratio unavailable.")
        sides, local_error = sample_sides(blend, True)
        screen = _shoulder_screen(sides, radius)
        if local_error:
            screen["local_sampling_unavailable"] = local_error
        upper = screen.get("band", [None, None])[1]
        level = "high" if upper is not None and upper >= 3 else "medium" if upper is not None and upper >= 2 else "low"
        row = _finding("concave_radius", level, blend.get("body"), blend.get("face_sid"),
                       blend.get("center"), blend.get("owner_feature"),
                       {"source": "eval_edge_analysis + eval_thickness", "topo": blend.get("topo"), "radius_mm": radius,
                        "sampled_body_min_thickness_mm": thickness,
                        "radius_to_min_thickness": radius / thickness if thickness else None,
                        "note": "Fallback rank uses radius/body sampled minimum thickness; this is not local r/t or a design limit."})
        row["kt_screen"] = screen
        findings.append(row)
    if local_omitted:
        notes.append(f"Local sampling window starts at {local_offset} and covers four edges and four blends; {local_omitted} candidates use fallback ranking.")
    ligaments = first.get("ligaments")
    if not isinstance(ligaments, dict) or ligaments.get("evaluated") is False:
        notes.append("Bore/ligament checks unavailable: edge analysis supplied no ligament inventory.")
        incomplete = True
    else:
        if ligaments.get("truncated") or any(
                b.get("evaluated") is False or b.get("incomplete") or b.get("truncated")
                for b in ligaments.get("bodies", [])):
            notes.append("Bore/ligament inventory incomplete or truncated; not all pairs were assessed.")
            incomplete = True
        if any(b.get("excluded_partial_cylinders") or b.get("failed_distances")
               for b in ligaments.get("bodies", [])):
            notes.append("Some partial cylinders or distance computations were excluded from the ligament inventory.")
            incomplete = True
        features = []
        if ligaments.get("rows"):
            try:
                tree = _call("doc_tree", {"doc_id": doc})
                if tree.get("revision") is not None and tree["revision"] != revision:
                    raise ValueError("document revision changed")
                features = _flatten_features(tree)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Hole feature edits unavailable: {str(exc)[:300]}")
        for item in ligaments.get("rows") or []:
            gap, radius = num(item.get("distance_mm")), num(item.get("radius_mm"))
            if gap is None or radius is None or gap < 0 or radius <= 0:
                continue
            ratio = gap / (2 * radius)
            level = "high" if ratio < 0.5 else "medium" if ratio < 1 else "low"
            row = _finding("hole_ligament_candidate", level, item.get("body"), item.get("face_sid"),
                           item.get("point"), item.get("owner_feature"),
                           {"source": "eval_edge_analysis", "kind": item.get("kind"),
                            "topo": item.get("face_id"), "other_topo": item.get("other_face_id"),
                            "distance_mm": gap, "bore_radius_mm": radius,
                            "other_radius_mm": item.get("other_radius_mm"),
                            "other_face_sid": item.get("other_face_sid"),
                            "other_point": item.get("other_point"), "gap_to_diameter": ratio,
                            "basis": item.get("basis"),
                            "note": "Finite-face distance only: the segment is not certified to lie in material, and the boundary need not be external. Ratio is a screening heuristic."})
            face = face_map.get((item.get("body"), item.get("face_id")), {})
            fix, reason = _ligament_fix(item, face, features)
            if fix:
                row["fix"] = fix
            else:
                row["fix_unavailable"] = reason
            findings.append(row)
    findings = [f for f in findings if _LEVEL[f["severity"]] >= _LEVEL[min_severity]]
    findings.sort(key=lambda f: (-_LEVEL[f["severity"]],
                                 -(f.get("kt_screen", {}).get("band") or [0, 0])[1],
                                 f["kind"], f["evidence"].get("radius_to_min_thickness")
                                 if f["evidence"].get("radius_to_min_thickness") is not None else float("inf"),
                                 str(f.get("body")), f["id"]))
    effective_limit = min(limit, 8) if verify_fixes else limit
    page = findings[offset:offset + effective_limit]
    if verify_fixes:
        if effective_limit != limit:
            notes.append("Page limit reduced to eight so every returned fix is dry-run attempted.")
        for row in page:
            if row.get("fix"):
                _verify(doc, row["fix"], revision)
    else:
        notes.append("Fix verification skipped by request.")
    return {"ok": True, "doc_id": doc, "revision": revision, "load_hint": load_hint,
            "findings": page, "total_findings": len(findings), "offset": offset,
            "requested_limit": limit, "effective_limit": effective_limit,
            "next_offset": offset + effective_limit if offset + effective_limit < len(findings) else None,
            "local_sampling": {"offset": local_offset, "window_per_kind": 4,
                               "edge_candidates": local_seen[False], "blend_candidates": local_seen[True],
                               "next_local_offset": (local_offset + 4 if local_offset + 4 < max(local_seen.values()) else None),
                               "note": "To cover all candidates, advance local_offset; ranking can change between windows. Retrieve result pages and merge by finding id."},
            "inventory_incomplete": incomplete,
            "notes": notes[:80]}


def register_stress_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def find_stress_risers(doc_id: Optional[str] = None, body: Optional[int] = None,
                           load_hint: Optional[str] = None, min_severity: str = "low",
                           verify_fixes: bool = True, offset: int = 0, limit: int = 20,
                           local_offset: int = 0) -> str:
        """Screen located geometry for possible stress risers; severity is not a safety verdict.

        Sharp corners, concave blend radii, bore-to-boundary distances and local thickness
        minima are screened where evidence exists. No load is assumed and no Kt is fabricated.
        Proposed fixes can be dry-run built, not proof of improved strength. With
        verify_fixes=true, each page is limited to eight so every returned fix is attempted.
        Pagination is by offset/limit. Local analysis is limited to four edges and four blends
        per local_offset window; advance next_local_offset to cover the inventory and merge
        result pages by id. Ranking can change between local windows; inspect notes.
        """
        try:
            return _dumps(_screen(_doc(doc_id), body, load_hint, min_severity, verify_fixes, offset, limit, local_offset))
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def eval_curvature(doc_id: Optional[str] = None, body: Optional[int] = None,
                       kind: str = "max") -> str:
        """Evaluate one body's curvature field; returns a bounded AICM handle and extrema.

        kind is mean, gaussian or max. Omitted body means the engine's last body. Mean/max
        have units 1/mm, Gaussian 1/mm²; undefined vertices are not measured.
        """
        try:
            _body_arg(body)
            if kind not in ("mean", "gaussian", "max"):
                raise ValueError("kind must be mean, gaussian or max")
            d = _doc(doc_id)
            params = {"doc_id": d, "kind": kind}
            if body is not None:
                params["body"] = body
            result = _call("eval_curvature", params)
            return _dumps({"ok": True, "doc_id": d, "body": body, "kind": kind,
                           "aicm_path": result.get("aicm_path"),
                           "min_curvature": result.get("min_curvature"),
                           "max_curvature": result.get("max_curvature"),
                           "units": "1/mm²" if kind == "gaussian" else "1/mm"})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def eval_geometry_analysis(doc_id: Optional[str] = None, min_face_area: Optional[float] = None,
                               min_edge_length: Optional[float] = None, sliver_ratio: Optional[float] = None,
                               gap_tol: Optional[float] = None, include_discontinuities: bool = False,
                               offset: int = 0, limit: int = 20) -> str:
        """Inspect B-rep geometry defects; page flattened body-local findings with offset/limit.

        Thresholds are mm², mm, dimensionless and mm respectively. Zero disables the
        corresponding check; omitted values use native scale-dependent defaults. The
        underlying source and gap scans may be incomplete, as reported in body summaries.
        """
        try:
            _page_args(offset, limit)
            d = _doc(doc_id)
            params = {"doc_id": d, "include_discontinuities": include_discontinuities}
            for key, value in (("min_face_area", min_face_area), ("min_edge_length", min_edge_length),
                               ("sliver_ratio", sliver_ratio), ("gap_tol", gap_tol)):
                if value is not None:
                    n = num(value)
                    if n is None or n < 0:
                        raise ValueError(f"{key} must be finite and nonnegative")
                    params[key] = n
            result = _call("eval_geometry_analysis", params)
            bodies = result.get("bodies") or []
            findings = [dict(f, body_id=b.get("body_id")) for b in bodies for f in b.get("findings", [])]
            summaries = [{k: b.get(k) for k in ("body_id", "brep_valid", "face_count", "edge_count",
                                               "thresholds", "gap_scan_complete")} for b in bodies[:_MAX_BODIES]]
            return _dumps({"ok": True, "doc_id": d, "revision": result.get("revision"),
                           "complete": result.get("complete"),
                           "source_failures": (result.get("source_failures") or [])[:20],
                           "source_failures_truncated": len(result.get("source_failures") or []) > 20,
                           "bodies": summaries, "bodies_truncated": len(bodies) > _MAX_BODIES,
                           "findings": findings[offset:offset + limit], "total_findings": len(findings),
                           "offset": offset, "next_offset": offset + limit if offset + limit < len(findings) else None})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
