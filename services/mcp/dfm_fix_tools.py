"""Read-only, shadow-verified proposals for strengthening thin walls.

Thickness is sampled on the native inscribed-sphere field; a verified proposal is
still a geometric screening result, not an assessment of loading or fit.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

from services.mcp import core_bridge as cb
from services.mcp.cad_tools import _doc, _dumps, _err
from services.mcp.dfm_common import num, op_edit_param, op_fillet, op_sketch_dim
from services.mcp.dfm_fix_verify import verify_fix_ops


def wall_target(params: dict, requested: Optional[float] = None) -> tuple[float, dict]:
    """Target the process minimum and whole extrusion perimeters, in mm."""
    from services.mcp.print_tools import print_params
    rules = params.get("rules") or {}
    p = print_params(process="fdm", dfm=params)
    width = num(rules.get("line_width_mm")) or p.line_width_mm
    perimeters = num(rules.get("perimeters")) or p.walls
    minimum = num(params.get("min_wall_mm"))
    if minimum is None or minimum <= 0 or width <= 0 or perimeters <= 0:
        raise ValueError("FDM profile needs positive min_wall_mm, extrusion width and perimeters")
    if requested is not None:
        requested = num(requested)
        if requested is None or requested <= 0:
            raise ValueError("target_wall_mm must be a positive finite number")
    target = max(minimum, width * perimeters, requested or 0)
    return target, {"process_min_wall_mm": minimum, "line_width_mm": width,
                    "perimeters": perimeters,
                    "assumptions": ["Extrusion width and perimeter count use the process profile or print parameter defaults; select a printer and verify its slicer settings."]}


def _candidates(region: dict, features: list[dict], target: float) -> list[dict]:
    """Candidate literal edits; expressions remain intact and are never overridden."""
    before = num(region.get("min_thickness_mm"))
    if before is None or before <= 0:
        return []
    owners = {str(x.get("id")) for x in region.get("built_by", []) if isinstance(x, dict)}
    candidates = []
    for f in features:
        if str(f.get("id")) not in owners:
            continue
        kind = str(f.get("type") or "").lower()
        params = f.get("params") or {}
        keys = ("thickness",) if kind in ("shell", "rib", "sm_base_flange") else ()
        if kind == "extrude":
            keys = ("thickness", "depth")
        for key in keys:
            original = params.get(key)
            value = num(original)
            if (value is None or value <= 0 or (isinstance(original, dict) and original.get("expr"))
                    or abs(value - before) > max(0.2 * before, 0.15)):
                continue
            desired = value + target - before + 0.01
            candidates.append({"kind": "thicken", "control": "edit_feature", "feature_id": f.get("id"),
                               "parameter": key, "old_value_mm": value,
                               "new_value_mm": desired,
                               "ops": [op_edit_param(str(f["id"]), **{key: desired})]})
        # A profile dimension is considered only when it is a literal driving
        # distance matching the sampled thickness. Shadow measurement decides
        # whether it really controls this region.
        refs = f.get("refs") or {}
        profile = refs.get("profile") or refs.get("sections") or []
        if isinstance(profile, (dict, str)):
            profile = [profile]
        sketch_ids = {r.get("sketch") if isinstance(r, dict) else r for r in profile
                      if isinstance(r, (dict, str))}
        for sk in features:
            if sk.get("id") not in sketch_ids or sk.get("type") != "sketch":
                continue
            for c in (sk.get("params") or {}).get("constraints") or []:
                val = c.get("value")
                v = num(val)
                if (c.get("type") not in ("distance", "length") or c.get("driven") or not c.get("id")
                        or v is None or v <= 0 or (isinstance(val, dict) and val.get("expr"))
                        or abs(v - before) > max(0.2 * before, 0.15)):
                    continue
                desired = v + target - before + 0.01
                candidates.append({"kind": "thicken", "control": "sketch_dimension", "feature_id": sk["id"],
                                   "parameter": c["id"], "old_value_mm": v,
                                   "new_value_mm": desired,
                                   "ops": [op_sketch_dim(sk["id"], c["id"], desired)]})
    return candidates[:8]


def _free_sketch_candidates(doc_id, region, features, target, spacing):
    """Trial an explicit dimension on a free profile line at the thin region."""
    import itertools
    import numpy as np

    before = num(region.get("min_thickness_mm"))
    box = region.get("bbox") or {}
    lo, hi = np.asarray(box.get("min", []), dtype=float), np.asarray(box.get("max", []), dtype=float)
    if not before or lo.shape != (3,) or hi.shape != (3,):
        return []
    owners = {str(x.get("id")) for x in region.get("built_by", []) if isinstance(x, dict)}
    sketch_ids = set()
    for feature in features:
        if str(feature.get("id")) not in owners:
            continue
        refs = feature.get("refs") or {}
        profiles = refs.get("profile") or refs.get("sections") or []
        if isinstance(profiles, (str, dict)):
            profiles = [profiles]
        sketch_ids.update(p.get("sketch") if isinstance(p, dict) else p for p in profiles
                          if isinstance(p, (str, dict)))
    result = []
    for sk in features:
        sid = sk.get("id")
        if sid not in sketch_ids or sk.get("type") != "sketch":
            continue
        overview = cb.call("doc_sketch_overview", {"doc_id": doc_id, "sketch": sid})
        health = cb.call("sketch_health", {"doc_id": doc_id, "sketch": sid})
        if overview.get("solve_rejected") or health.get("status") != "under":
            continue
        frame = np.asarray(overview.get("plane_frame") or [], dtype=float)
        if frame.size != 16:
            continue
        frame = frame.reshape(4, 4)
        corners = np.asarray(list(itertools.product(*zip(lo, hi))))
        projected = (corners - frame[:3, 3]) @ frame[:3, :2]
        bounds = (projected.min(axis=0) - spacing, projected.max(axis=0) + spacing)
        entities = overview.get("entities") or []
        if isinstance(entities, dict):
            entities = [{"id": k, **v} for k, v in entities.items() if isinstance(v, dict)]
        constraints = (sk.get("params") or {}).get("constraints") or []
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("type") != "line" or not entity.get("id"):
                continue
            p1, p2 = np.asarray(entity.get("p1", []), dtype=float), np.asarray(entity.get("p2", []), dtype=float)
            if p1.shape != (2,) or p2.shape != (2,):
                continue
            length = float(np.linalg.norm(p2 - p1))
            mid = (p1 + p2) / 2
            if abs(length - before) > max(2 * spacing, 0.2 * before) or np.any(mid < bounds[0]) or np.any(mid > bounds[1]):
                continue
            eid = entity["id"]
            if any(c.get("type") in ("length", "distance") and not c.get("driven")
                   and eid in (c.get("refs") or []) for c in constraints):
                continue
            desired = length + target - before + 0.01
            result.append({"kind": "thicken", "control": "new_sketch_dimension", "feature_id": sid,
                "entity": eid, "old_value_mm": length, "new_value_mm": desired,
                "ops": [{"op": "sketch_add_constraint", "sketch": sid,
                         "constraint": {"type": "length", "refs": [eid], "value": desired}}]})
            if len(result) == 8:
                return result
    return result


def _local_field_min(fields: list, region: dict, padding_mm: float) -> tuple[Optional[float], int]:
    """Minimum finite field sample near the original region; no sample means unknown."""
    import numpy as np
    from services.aicm_handoff import read_handoff
    from services.sim.tessellate import read_aicm
    box = region.get("bbox") or {}
    lo, hi = np.asarray(box.get("min", []), dtype=float), np.asarray(box.get("max", []), dtype=float)
    if lo.shape != (3,) or hi.shape != (3,):
        return None, 0
    values = []
    for body_number, handle in fields:
        if body_number != region.get("body", 1):
            continue
        field = read_aicm(read_handoff(handle))
        for scalar in field.get("scalars") or []:
            body = next((b for b in field.get("bodies") or [] if b["body_id"] == scalar.get("body_id")), None)
            if body is None:
                continue
            positions = np.asarray(body["pos"], dtype=float).reshape(-1, 3)
            thickness = np.asarray(scalar.get("values"), dtype=float)
            if len(positions) != len(thickness):
                continue
            mask = (np.isfinite(thickness) & np.all(positions >= lo - padding_mm, axis=1)
                    & np.all(positions <= hi + padding_mm, axis=1))
            values.extend(thickness[mask].tolist())
    return (float(min(values)), len(values)) if values else (None, 0)


def _fit_geometry(doc_id: str, features: list[dict]) -> dict:
    """Collect directly measured cylindrical radii and cut-wall separations."""
    faces = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}) or {}).get("matches")
    if not isinstance(faces, list):
        raise ValueError("Face cards unavailable for fit comparison")
    tree = cb.call("doc_tree", {"doc_id": doc_id}) or {}
    owners = {}
    for owner, entry in (tree.get("owner_map") or {}).items():
        for pair in entry.get("faces") or []:
            if isinstance(pair, list) and len(pair) == 2:
                owners[(int(pair[0]), int(pair[1]))] = owner
    faces = [{**f, "owner_feature": f.get("owner_feature") or owners.get((f.get("body"), f.get("topo")))}
             for f in faces if isinstance(f, dict)]
    cylinders = [(str(f.get("owner_feature")), round(num(f["radius"]), 6),
                  round(num(f["area_or_len"]), 6) if num(f.get("area_or_len")) is not None else None)
                 for f in faces if f.get("surf") == "cylinder" and num(f.get("radius")) is not None]
    cylinders.sort(key=repr)
    cut_ids = {str(f.get("id")) for f in features
               if str((f.get("params") or {}).get("mode", "")).lower() in ("cut", "subtract")
               or str(f.get("type", "")).lower() in ("hole", "slot")}
    planes = [f for f in faces if f.get("surf") == "plane"
              and str(f.get("owner_feature")) in cut_ids
              and isinstance(f.get("normal"), list) and isinstance(f.get("centroid"), list)
              and len(f["normal"]) == 3 and len(f["centroid"]) == 3]
    separations = []
    for i, a in enumerate(planes):
        for b in planes[i+1:]:
            if a.get("owner_feature") != b.get("owner_feature"):
                continue
            n, m = a["normal"], b["normal"]
            if abs(sum(float(x)*float(y) for x, y in zip(n, m)) + 1.0) > 1e-6:
                continue
            distance = abs(sum(float(n[k]) * (float(a["centroid"][k]) - float(b["centroid"][k]))
                               for k in range(3)))
            separations.append((str(a.get("owner_feature")), round(distance, 6)))
    return {"cylindrical_radii": cylinders, "opposed_cut_face_distances": sorted(separations)}



def _region_points(region, fields):
    """Surface sample positions in a thickness region's original bounding box."""
    import numpy as np
    from services.aicm_handoff import read_handoff
    from services.sim.tessellate import read_aicm

    box = region.get("bbox") or {}
    lo, hi = np.asarray(box.get("min", []), dtype=float), np.asarray(box.get("max", []), dtype=float)
    if lo.shape != (3,) or hi.shape != (3,):
        return np.empty((0, 3))
    points = []
    for body_number, handle in fields:
        if body_number != region.get("body", 1):
            continue
        field = read_aicm(read_handoff(handle))
        for body in field.get("bodies") or []:
            pos = np.asarray(body["pos"], dtype=float).reshape(-1, 3)
            points.extend(pos[np.all((pos >= lo) & (pos <= hi), axis=1)])
    return np.asarray(points, dtype=float).reshape(-1, 3)


def _rib_candidates(region, fields, edges, faces, target, radius_ratio=None):
    """Trial web across a measured concave root in a region's principal section."""
    import numpy as np
    box = region.get("bbox") or {}
    lo, hi = np.asarray(box.get("min", []), dtype=float), np.asarray(box.get("max", []), dtype=float)
    if lo.shape != (3,) or hi.shape != (3,):
        return []
    points = _region_points(region, fields)
    if len(points) < 3:
        return []
    region_center = points.mean(axis=0)
    covariance = (points - region_center).T @ (points - region_center)
    values, _ = np.linalg.eigh(covariance)
    if values[-1] <= 0:
        return []
    face_map = {f.get("sid"): f for f in faces}
    span = float(np.linalg.norm(hi - lo)) / 4
    if span <= 0:
        return []
    result = []
    for edge in edges:
        mid, adjacent = edge.get("mid"), edge.get("faces") or []
        if (edge.get("body", 1) != region.get("body", 1) or not isinstance(mid, list)
                or len(mid) != 3 or len(adjacent) != 2):
            continue
        root = np.asarray(mid, dtype=float)
        if np.any(root < lo - target) or np.any(root > hi + target):
            continue
        tangent = edge.get("dir")
        if not isinstance(tangent, list) or len(tangent) != 3:
            continue
        axis = np.asarray(tangent, dtype=float)
        if np.linalg.norm(axis) < 1e-9:
            continue
        axis /= np.linalg.norm(axis)
        center = root + float((region_center - root) @ axis) * axis
        reference = np.array([1.0, 0, 0])
        if abs(float(reference @ axis)) > 1 - 1e-9:
            reference = np.array([0.0, 1, 0])
        u = reference - float(reference @ axis) * axis
        u /= np.linalg.norm(u)
        v = np.cross(axis, u)
        normals = [face_map.get(s, {}).get("normal") for s in adjacent]
        if any(not isinstance(n, list) or len(n) != 3 for n in normals):
            continue
        wall_face = next((sid for sid in adjacent if sid in (region.get("faces") or [])), None)
        if wall_face is None:
            continue
        wall_normal = np.asarray(face_map[wall_face]["normal"], dtype=float)
        wall_normal -= float(wall_normal @ axis) * axis
        if np.linalg.norm(wall_normal) < 1e-9:
            continue
        wall_normal /= np.linalg.norm(wall_normal)
        other_face = next(sid for sid in adjacent if sid != wall_face)
        along_wall = np.asarray(face_map[other_face]["normal"], dtype=float)
        along_wall -= float(along_wall @ axis) * axis
        if np.linalg.norm(along_wall) < 1e-9:
            continue
        along_wall /= np.linalg.norm(along_wall)
        # Compare a cross-section of the reinforced leg, rather than scaling
        # the inertia of the entire L (including its perpendicular leg).
        section_origin = center + 0.5 * span * along_wall
        bend_axis = axis
        # The opposing outward normals point along the two legs of a concave root.
        root = root - float((root - center) @ axis) * axis
        legs = []
        for n in normals:
            n = np.asarray(n, dtype=float)
            n = n - float(n @ axis) * axis
            size = np.linalg.norm(n)
            if size < 1e-9:
                break
            p = root + span * n / size - center
            legs.append([float(p @ u), float(p @ v)])
        if len(legs) != 2 or np.linalg.norm(np.asarray(legs[0]) - legs[1]) < 1e-9:
            continue
        ops = [
            {"op": "add_feature", "feature": {"type": "plane", "name": "Reinforcement section",
                "params": {"method": "point_normal", "origin": center.tolist(), "normal": axis.tolist()}}},
            {"op": "add_feature", "feature": {"type": "sketch", "name": "Reinforcement web",
                "refs": {"plane": "$0"}, "params": {"entities": [
                    {"id": "e1", "type": "line", "p1": legs[0], "p2": legs[1]}]}}},
            {"op": "add_feature", "feature": {"type": "rib", "name": "Reinforcement rib",
                "params": {"thickness": target}, "refs": {"profile": "$1"}}}]
        trial = {"kind": "reinforce", "reinforcement": "rib", "root_edge": edge.get("sid"),
                 "section_origin": section_origin.tolist(), "section_axis": along_wall.tolist(),
                 "bending_axis": bend_axis.tolist(), "thickness_mm": target, "ops": ops}
        result.append(trial)
        if radius_ratio is not None and radius_ratio > 0:
            radius = max(radius_ratio * float(region["min_thickness_mm"]),
                         0.5 * float(region["min_thickness_mm"]))
            result.append({**trial, "reinforcement": "fillet", "requested_radius_mm": radius,
                           "ops": [op_fillet("Reinforcement root fillet", radius, [mid])]})
        if len(result) == 4:
            break
    return result

def reinforce(doc_id: str, process: Optional[str] = None, target_wall_mm: Optional[float] = None,
              limit: int = 5, cursor: int = 0) -> dict:
    """Build and remeasure candidate edits in isolated internal shadow documents."""
    from services.mcp.intake_tools import _canon_process, _dfm, _process_id, _dfm_features, _get, _K_PROCESS
    from services.mcp.cad_eval_tools import _wall_thickness_result, _eval_thickness_all_bodies
    proc = _canon_process(process) or _process_id(doc_id)
    if proc != "fdm":
        raise ValueError("dfm_reinforce currently requires an FDM process; set_process('fdm') for FDM parts")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    bound = _get(doc_id, _K_PROCESS)
    profile = bound if isinstance(bound, dict) and bound.get("process") == proc else _dfm("dfm_get_profile", {"process": proc})
    target, target_basis = wall_target(profile.get("params") or {}, target_wall_mm)
    tree = cb.call("doc_tree", {"doc_id": doc_id})
    revision = tree.get("revision")
    features = _dfm_features(tree)
    baseline = _wall_thickness_result(doc_id, target)
    if baseline.get("min_thickness_mm") is None:
        raise ValueError("No wall thickness was measured; inspect the part with wall_thickness before proposing reinforcement")
    regions = baseline.get("thin_regions") or []
    page = regions[cursor:cursor + limit]
    output = []
    rib_evidence_error = None
    rib_fields, rib_edges, rib_faces = [], [], []
    if page:
        try:
            from services.mcp.dfm_common import DfmContext
            _, rib_fields = _eval_thickness_all_bodies(doc_id, target)
            context = DfmContext(doc_id, proc, profile, [0, 0, 1], "default")
            rib_edges = context.edge_analysis(kinds=("concave",))["edges"]
            rib_faces = context.faces()
        except Exception as exc:  # noqa: BLE001 -- parameter candidates can still be checked
            rib_evidence_error = str(exc)
    for original in page:
        item = {**original, "target_wall_mm": target, "before_mm": original.get("min_thickness_mm"),
                "verified_fixes": [], "rejected_candidates": []}
        candidates = _candidates(original, features, target)
        try:
            spacing = num((baseline.get("measurement_basis") or {}).get("spacing_mm")) or 0
            candidates.extend(_free_sketch_candidates(doc_id, original, features, target, spacing))
        except Exception as exc:  # noqa: BLE001
            item["sketch_dimension_not_evaluated"] = str(exc)
        if rib_evidence_error:
            item["rib_not_evaluated"] = rib_evidence_error
        else:
            try:
                candidates.extend(_rib_candidates(original, rib_fields, rib_edges, rib_faces, target,
                    num(((profile.get("params") or {}).get("rules") or {}).get("internal_corner_radius_wall_ratio"))))
            except Exception as exc:  # noqa: BLE001
                item["rib_not_evaluated"] = str(exc)
        if not candidates:
            item["note"] = ("No controlling literal parameter or driving sketch dimension could be identified. "
                            "Inspect the wall faces and source sketch; add a constrained rib or gusset if appropriate.")
        def measure(which_doc):
            study, fields = _eval_thickness_all_bodies(which_doc, target)
            spacing = num((study.get("measurement_basis") or {}).get("spacing_mm")) or 0
            padding = max(2 * spacing, target - float(item["before_mm"]) + 0.1)
            value, samples = _local_field_min(fields, original, padding)
            interference = cb.call("eval_interference", {"doc_id": which_doc})
            if not isinstance(interference, dict) or not interference.get("complete"):
                raise ValueError("Body interference evaluation incomplete")
            pairs = sorted(tuple(sorted((str(p.get("a")), str(p.get("b")))))
                           for p in interference.get("interferences") or [])
            section = None
            if candidate.get("kind") == "reinforce":
                section = cb.call("eval_section_props", {"doc_id": which_doc, "plane": {
                    "origin": candidate["section_origin"], "normal": candidate["section_axis"],
                    "xdir": candidate["bending_axis"]}})
                if not isinstance(section, dict) or num(section.get("Ixx")) is None:
                    raise ValueError("Bending-axis section inertia unavailable for rib verification")
            return {"thickness_mm": value, "samples": samples,
                    "thin_region_count": study.get("n_thin_regions"),
                    "thin_regions": (study.get("thin_regions") or [])[:20],
                    "fit_geometry": _fit_geometry(which_doc, features),
                    "interference_pairs": pairs, "section": section}
        for candidate in candidates:
            verification = verify_fix_ops(doc_id, candidate["ops"], measure)
            after = verification.get("after") or {}
            measured = after.get("thickness_mm")
            delta = verification["side_effects"].get("volume_delta_mm3")
            if (verification.get("builds") is not True or measured is None
                    or (candidate.get("kind") != "reinforce" and measured < target - 1e-4)
                    or delta is None or delta <= 0):
                item["rejected_candidates"].append({**candidate,
                    "reason": verification.get("error") or "Local wall target or positive added volume was not confirmed",
                    "after_mm": measured})
                continue
            before = verification.get("before") or {}
            before_fit, after_fit = before.get("fit_geometry") or {}, after.get("fit_geometry") or {}
            lost_cylinders = Counter(map(tuple, before_fit.get("cylindrical_radii") or [])) - Counter(map(tuple, after_fit.get("cylindrical_radii") or []))
            if lost_cylinders or before_fit.get("opposed_cut_face_distances") != after_fit.get("opposed_cut_face_distances"):
                item["rejected_candidates"].append({**candidate, "reason": "Would alter measured fit features",
                    "before_fit": before.get("fit_geometry"), "after_fit": after.get("fit_geometry")})
                continue
            new_pairs = sorted(set(map(tuple, after.get("interference_pairs") or [])) -
                               set(map(tuple, before.get("interference_pairs") or [])))
            if new_pairs:
                item["rejected_candidates"].append({**candidate, "reason": "Would introduce body interference",
                                                    "new_interference_pairs": new_pairs})
                continue
            if candidate.get("reinforcement") == "fillet":
                statuses = verification["side_effects"].get("feature_status") or []
                report = ((statuses[-1].get("data") or {}).get("fillet") or {}) if statuses else {}
                effective = num((report.get("effective") or {}).get("radius"))
                if (effective is None or effective + 1e-9 < candidate["requested_radius_mm"]
                        or report.get("selected_edges") != 1 or report.get("covered_edges") != 1):
                    item["rejected_candidates"].append({**candidate, "reason": "Fillet did not build at required radius",
                                                        "fillet_result": report})
                    continue
                candidate["effective_radius_mm"] = effective
                candidate["fillet_adjustment"] = report.get("adjustment")
            side_effects = verification["side_effects"]
            side_effects.update({"thin_region_count_before": before.get("thin_region_count"),
                                 "thin_region_count_after": after.get("thin_region_count"),
                                 "other_regions": after.get("thin_regions"),
                                 "fit_and_clearance": {"status": "measured_cards_unchanged",
                                    "limitation": "Cylindrical radii and opposed authored cut faces only; other fit intent remains unknown."},
                                 "body_interference": {"status": "evaluated",
                                    "before": before.get("interference_pairs"), "after": after.get("interference_pairs")}})
            if candidate.get("kind") == "reinforce":
                before_section, after_section = before.get("section") or {}, after.get("section") or {}
                before_i, after_i = num(before_section.get("Ixx")), num(after_section.get("Ixx"))
                before_wall = num(item["before_mm"])
                if before_i is None or after_i is None or before_i <= 0 or after_i <= 0 or not before_wall or before_wall <= 0:
                    item["rejected_candidates"].append({**candidate, "reason": "Bending inertia or wall thickness unavailable"})
                    continue
                # Rectangular wall section I = b*t^3/12. This comparison is
                # an idealized proxy, not a reconstructed target wall.
                required_i = before_i * (target / before_wall) ** 3
                if after_i < required_i:
                    item["rejected_candidates"].append({**candidate,
                        "reason": "Bending inertia below target-thickness equivalent",
                        "before_inertia_mm4": before_i, "after_inertia_mm4": after_i,
                        "target_inertia_mm4": required_i})
                    continue
                candidate["stiffness_comparison"] = {"before_inertia_mm4": before_i,
                    "after_inertia_mm4": after_i, "target_inertia_mm4": required_i,
                    "ratio_to_target": after_i / required_i,
                    "basis": "Cross-section Ixx about wall bending axis; idealized constant-width rectangular I scales as thickness cubed, not a reconstructed target geometry.",
                    "assumptions": ["Same material and bending axis; actual loading, deflection, and local failure are not established."]}
                side_effects["section_before"] = before_section
                side_effects["section_after"] = after_section
            item["verified_fixes"].append({**candidate, "verified": True,
                "before_mm": item["before_mm"], "after_mm": measured,
                "samples": after.get("samples"),
                "basis": "native inscribed-sphere field samples in expanded original region bbox",
                "residual_thin_samples": measured < target - 1e-4,
                "side_effects": side_effects})
        item["verified_fixes"].sort(key=lambda fix: fix["side_effects"]["volume_delta_mm3"])
        output.append(item)
    current = cb.call("doc_tree", {"doc_id": doc_id}).get("revision")
    if revision is not None and current != revision:
        raise ValueError("Document changed during reinforcement analysis; rerun dfm_reinforce on the current revision")
    next_cursor = cursor + len(page)
    truncated = next_cursor < len(regions) or int(baseline.get("n_thin_regions") or 0) > len(regions)
    return {"ok": True, "process": proc, "doc_id": doc_id, "source_revision": revision,
            "target_wall_mm": target, "basis": "native sampled inscribed-sphere wall thickness",
            "confidence": "geometry_heuristic", **target_basis, "regions": output,
            "truncated": truncated, "next_cursor": next_cursor if next_cursor < len(regions) else None,
            "note": "No operation was applied to the source document. Check load paths, fit and all side effects before applying a proposal."}


def register_dfm_fix_tools(mcp) -> None:
    @mcp.tool()
    def dfm_reinforce(doc_id: Optional[str] = None, process: Optional[str] = None,
                      target_wall_mm: Optional[float] = None, limit: int = 5, cursor: int = 0) -> str:
        """Preview FDM wall-thickening edits verified on isolated clones, without changing the part.

        Returns located thin regions and controlling literal feature/sketch dimensions where
        identifiable. Thickening edits must reach the local wall target; rib and fillet
        proposals must meet an idealized section-inertia target and may leave thin samples.
        Review volume and other changed regions before applying ops through add_features.
        Unmeasured regions and unidentified controlling dimensions are not evidence of safety.
        """
        try:
            return _dumps(reinforce(_doc(doc_id), process, target_wall_mm, limit, cursor))
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
