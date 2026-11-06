"""interface_check_tools.py — MCP tool: design-time interface alignment checker.

Purpose: give the AI the tooling to check that component
interfaces in an assembly LINE UP — the class of layout error that passes interference
(nothing overlaps) but is still wrong: bolt holes that don't register, mating bores that
aren't coaxial, contact faces that aren't flush, a screw that can't pass the stack.

Contract / registration:
  * Registered by services/mcp/server.py (append-only) as `interface_check`.
  * Wired into the design_review battery + services/verify/battery.py so verify_battery /
    design_review can run it, and cad_guide('review') mentions it (MCP freshness rule).
  * JSON output shape is STABLE and documented below — the plan-first layout workflow (layout_plan_tools) consumes it.

Reuse, not fork:
  * `doc_query` (query_geometry) — per-body cylindrical/planar face interrogation. The engine's
    doc_query gained an optional 1-based `body_id` (mirrors eval_measure/eval_mass) so we
    can read EACH component's faces in their WORLD placement instead of only the last body.
  * `asm_world_bodies` (list_world_bodies) — enumerates the placed bodies/components + bboxes.
  * `cam_extract(kind="holes")` — OCCT hole grouping (minor/counterbore/countersink), used
    as a cross-check / richer hole classification on the whole doc.
  * `research.thread_spec` and the ISO 273 lookup — nominal fastener diameter and
    published metric clearance-hole series.
  * `check_interference` remains the separate hard-clash gate; this tool is its alignment complement.

--------------------------------------------------------------------------------------------
JSON CONTRACT  (interface_check return — consumed by layout_plan_tools; keep stable)
--------------------------------------------------------------------------------------------
{
  "ok": bool,                     # True when the check ran; ok=false only for a refusal/error
  "status": "pass"|"fail"|"skip", # the verdict: "fail" iff a finding has severity "fail";
                                  # "skip" when no component pair was compared
  "doc_id": str,
  "tool": "interface_check",
  "version": 1,
  "components": [                 # every body in the doc, i.e. the pairing CANDIDATES
      {"id": "f2", "name": "bracket", "bbox": {...}}, ...
  ],
  "body_count": int,              # how many bodies the verdict weighed (scratch bodies included)
  "pair_selection": "explicit"|"proximity"|"contact_faces"|"none",
  "pairs": [                      # one entry per interface (component pair) examined
    {
      "a": "f2", "b": "f3",
      "a_name": "bracket", "b_name": "plate",
      "checks": {
        "hole_pattern": {
            "status": "pass"|"fail"|"skip",
            "matched":   [ {"a_hole": {...}, "b_hole": {...}, "pos_delta_mm": float,
                            "dia_delta_mm": float, "diameter_fit": "similar"|"different",
                            "clearance_verdict": "clear"|"insufficient"|"unverified" } ],
            "misaligned":[ {"a_hole": {...}, "b_hole": {...}, "pos_delta_mm": float,
                            "offset_vec_mm": [dx,dy,dz], "dia_delta_mm": float,
                            "fix": "..." } ],
            "unmatched_a": [ {...hole...} ],   # holes on A with no counterpart on B
            "unmatched_b": [ {...hole...} ],
            "summary": "..."
        },
        "coaxiality": {
            "status": ...,
            "pairs": [ {"a_feature": "...", "b_feature": "...", "axis_offset_mm": float,
                        "axis_angle_deg": float, "status": "pass"|"fail", "fix": "..."} ],
            "summary": "..."
        },
        "contact_plane": {
            "status": ...,
            "gap_mm": float|null,           # signed: + = gap, - = overlap along the shared normal
            "parallel_angle_deg": float|null,
            "a_feature": "...", "b_feature": "...",
            "reference_source": "explicit"|"mate:<id>", "fix": "...", "summary": "..."
        },
        "fastener_passthrough": {
            "status": "pass"|"fail"|"skip",
            "fastener": "M6"|null,
            "stacks": [ {"nominal_mm": float, "min_hole_dia_mm": float,
                         "clearance_mm": float, "passes": bool|null, "status": str} ],
            "summary": "..."
        }
      }
    }
  ],
  "findings": [                   # flat, agent-actionable list across all pairs (the payload
                                  # design_review / verify narrate). Each finding is self-contained.
     {"severity": "fail"|"warn", "component_a": "f2", "component_b": "f3",
      "check": "hole_pattern", "feature": "hole at (15.0, 10.0)", "error_mm": 1.5,
      "detail": "hole pattern on plate offset 1.50 mm in +X relative to bracket",
      "fix": "shift plate hole pattern -1.50 mm in X (or re-datum the bolt circle)"},
     ...
  ],
  "summary": "...",               # one-line verdict
  "scope_note": "..."             # honest v1 limits (see V1_SCOPE_NOTE)
}
--------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# Tolerances (mm / deg). Deliberately modest defaults — an interface that lines up to
# within these registers as "aligned"; anything looser is a real layout error worth flagging.
DEFAULT_POS_TOL_MM = 0.20      # hole-pattern position registration tolerance
DEFAULT_DIA_TOL_MM = 0.05      # reportable bore diameter difference, not a fit tolerance
DEFAULT_COAX_OFFSET_TOL_MM = 0.10   # coaxiality: perpendicular axis-to-axis distance
DEFAULT_COAX_ANGLE_TOL_DEG = 0.50   # coaxiality: axis angular misalignment
DEFAULT_PLANE_ANGLE_TOL_DEG = 0.50  # contact plane parallelism
DEFAULT_PLANE_GAP_TOL_MM = 0.10     # contact plane flushness (|gap|)
# A candidate mating pair is only examined when the two components' bounding boxes are within
# this gap — the proximity heuristic that stands in for "intended to mate" (see scope note).
DEFAULT_PROXIMITY_MM = 2.0
# Matching a hole on A to its counterpart on B: two holes are "the same hole across the
# interface" when their axes are (near) collinear within this lateral tolerance.
HOLE_MATCH_LATERAL_MM = 5.0

V1_SCOPE_NOTE = (
    "interface_check v1 examines component pairs whose world bounding boxes are adjacent "
    "(within proximity_mm) OR an explicit pair you name — this proximity heuristic stands in "
    "for 'intended to mate'; it does NOT infer intent for components that are meant to align "
    "but sit far apart, and it will not invent a mate that the layout does not physically "
    "suggest. Hole-pattern matching pairs holes whose axes are near-collinear across the "
    "interface; a deliberately different pattern reads as unmatched holes, not a misalignment. "
    "Coaxiality/contact-plane use the cylindrical/planar faces the geometry kernel reports; "
    "contact requires explicit faces or resolvable mate references, and otherwise is unverified. "
    "Different bore diameters are not a positional error; nominal fastener clearance does not "
    "establish head, grommet, or recess fit. Spline/toroidal surfaces are out of scope. "
    "Pass-through assumes straight parallel bores, nominal diameter and an additional radial "
    "relative-position uncertainty; it does not check thread engagement, head/washer seating, "
    "hole size tolerance, or torque. This complements check_interference (hard clash)."
)


# ---------------------------------------------------------------------------
# Small vector helpers (pure Python — no numpy dependency in the tool layer)
# ---------------------------------------------------------------------------
def _v(a: list) -> tuple:
    return (float(a[0]), float(a[1]), float(a[2]))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _axis_angle_deg(u, v) -> float:
    """Angle between two axis DIRECTIONS, folded to 0..90 (an axis has no sign)."""
    uu, vv = _unit(u), _unit(v)
    c = max(-1.0, min(1.0, abs(_dot(uu, vv))))
    return math.degrees(math.acos(c))


def _perp_distance_between_axes(p1, d1, p2, d2) -> float:
    """Minimum distance between two infinite lines (axes). For parallel axes it degrades to
    the perpendicular component of the offset — exactly the coaxiality error we want."""
    d1, d2 = _unit(d1), _unit(d2)
    n = _cross(d1, d2)
    w = _sub(p2, p1)
    nn = _norm(n)
    if nn < 1e-9:
        # parallel: reject the component of w along d1
        along = _dot(w, d1)
        perp = (w[0] - along * d1[0], w[1] - along * d1[1], w[2] - along * d1[2])
        return _norm(perp)
    return abs(_dot(w, n)) / nn


def _dominant_axis_label(vec) -> str:
    """Plain-words +X/-Y/... label for the largest component of an offset vector."""
    ax = [("X", vec[0]), ("Y", vec[1]), ("Z", vec[2])]
    ax.sort(key=lambda t: abs(t[1]), reverse=True)
    name, val = ax[0]
    if abs(val) < 1e-9:
        return "(negligible)"
    return f"{'+' if val > 0 else '-'}{name}"


# ---------------------------------------------------------------------------
# Engine access — per-body face + hole extraction (reused verbs)
# ---------------------------------------------------------------------------
def _round_faces_for_body(cb, doc_id: str, body_id: int) -> list[dict]:
    """Cylindrical (bore/boss) faces of ONE body, in world coords, via doc_query body_id."""
    r = cb.call("doc_query", {"doc_id": doc_id,
                              "query": {"of": "face", "surf": "round", "body_id": body_id}})
    return [m for m in r.get("matches", []) if m.get("surf") == "cylinder"]


def _planar_faces_for_body(cb, doc_id: str, body_id: int) -> list[dict]:
    """Planar faces of ONE body, in world coords, via doc_query body_id."""
    r = cb.call("doc_query", {"doc_id": doc_id,
                              "query": {"of": "face", "surf": "flat", "body_id": body_id}})
    return [m for m in r.get("matches", []) if m.get("surf") in ("plane", "flat")]


def _cylinders_from_faces(round_faces: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split cylindrical faces into (bores, bosses) — concave holes vs convex shafts/bosses.

    Each record carries the axis direction, a point on the geometric axis, radius/diameter,
    and `kind` ("bore"|"boss"). Faces of the SAME cylinder split by seams are merged by
    near-collinear axis + equal radius. Bores drive hole-pattern + fastener checks; bores AND
    bosses drive coaxiality (a boss/shaft on one component seating into a bore on the other)."""
    bores: list[dict] = []
    bosses: list[dict] = []
    for f in round_faces:
        concavity = f.get("concavity")
        is_bore = concavity in ("bore", "concave")
        target = bores if is_bore else bosses
        axis = _unit(_v(f.get("axis", [0, 0, 1])))
        # A partial cylindrical surface's area centroid is not on its cylinder axis.
        # Keep the public record shape, but use the kernel's actual axis point for all
        # axis comparisons and seam merging.
        centroid = _v(f["axis_point"])
        radius = float(f.get("radius", 0.0))
        merged = False
        for h in target:
            if abs(h["radius"] - radius) < 1e-3 and \
               _perp_distance_between_axes(h["centroid"], h["axis"], centroid, axis) < 1e-2 and \
               _axis_angle_deg(h["axis"], axis) < 1.0:
                merged = True
                break
        if not merged:
            target.append({"axis": axis, "centroid": centroid, "radius": radius,
                           "dia": round(radius * 2, 4), "sid": f.get("sid"),
                           "kind": "bore" if is_bore else "boss"})
    return bores, bosses


def _hole_ref(h: dict) -> str:
    """Plain-words reference for a cylinder (bore/boss) — the style the engine already uses."""
    c = h["centroid"]
    word = "boss" if h.get("kind") == "boss" else "hole"
    return f"{word} Ø{h['dia']:g} at ({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f})"


# ---------------------------------------------------------------------------
# Per-interface checks
# ---------------------------------------------------------------------------
def _check_hole_pattern(a_holes, b_holes, pos_tol, dia_tol, findings, a_id, b_id, a_name, b_name):
    matched, misaligned, unmatched_a = [], [], []
    used_b = set()
    for ha in a_holes:
        # find the nearest B hole whose axis is near-collinear (same hole across the interface)
        best, best_lat = None, HOLE_MATCH_LATERAL_MM
        for j, hb in enumerate(b_holes):
            if j in used_b:
                continue
            if _axis_angle_deg(ha["axis"], hb["axis"]) > 5.0:
                continue
            lat = _perp_distance_between_axes(ha["centroid"], ha["axis"], hb["centroid"], hb["axis"])
            if lat < best_lat:
                best, best_lat, best_j = hb, lat, j
        if best is None:
            unmatched_a.append(ha)
            continue
        used_b.add(best_j)
        offset = _sub(best["centroid"], ha["centroid"])
        # project the offset onto the plane perpendicular to the hole axis (in-plane registration)
        along = _dot(offset, ha["axis"])
        perp = (offset[0] - along * ha["axis"][0],
                offset[1] - along * ha["axis"][1],
                offset[2] - along * ha["axis"][2])
        pos_delta = _norm(perp)
        dia_delta = abs(ha["dia"] - best["dia"])
        rec = {"a_hole": _pub_hole(ha), "b_hole": _pub_hole(best),
               "pos_delta_mm": round(pos_delta, 4), "dia_delta_mm": round(dia_delta, 4)}
        rec["diameter_fit"] = "different" if dia_delta > dia_tol else "similar"
        if pos_delta <= pos_tol:
            matched.append(rec)
        else:
            direction = _dominant_axis_label(perp)
            rec["offset_vec_mm"] = [round(perp[0], 4), round(perp[1], 4), round(perp[2], 4)]
            rec["fix"] = (f"shift {b_name} hole pattern {pos_delta:.4g} mm toward "
                          f"{_dominant_axis_label((-perp[0], -perp[1], -perp[2]))} to register "
                          f"with {a_name} (or re-datum the pattern)")
            misaligned.append(rec)
            findings.append({
                "severity": "fail", "component_a": a_id, "component_b": b_id,
                "check": "hole_pattern", "feature": _hole_ref(ha),
                "error_mm": round(pos_delta, 4),
                "detail": (f"hole pattern on {b_name} offset {pos_delta:.4g} mm in {direction} "
                           f"relative to {a_name}"),
                "fix": rec["fix"],
            })
    unmatched_b = [hb for j, hb in enumerate(b_holes) if j not in used_b]
    for ha in unmatched_a:
        findings.append({
            "severity": "warn", "component_a": a_id, "component_b": b_id,
            "check": "hole_pattern", "feature": _hole_ref(ha), "error_mm": None,
            "detail": f"{a_name} {_hole_ref(ha)} has no matching hole on {b_name} across the interface",
            "fix": f"add the counterpart hole to {b_name} or confirm this hole is not a through-fastener",
        })
    status = "fail" if misaligned else ("skip" if not a_holes and not b_holes else "pass")
    summary = (f"{len(matched)} aligned, {len(misaligned)} misaligned, "
               f"{len(unmatched_a)}+{len(unmatched_b)} unmatched")
    return {"status": status, "matched": matched, "misaligned": misaligned,
            "unmatched_a": [_pub_hole(h) for h in unmatched_a],
            "unmatched_b": [_pub_hole(h) for h in unmatched_b], "summary": summary}


def _pub_hole(h: dict) -> dict:
    """Public (JSON-serialisable) hole record."""
    return {"dia_mm": h["dia"], "centroid_mm": [round(c, 4) for c in h["centroid"]],
            "axis": [round(a, 4) for a in h["axis"]], "ref": _hole_ref(h)}


def _check_coaxiality(a_bosses, b_bores, b_bosses, a_bores, offset_tol, angle_tol, findings,
                      a_id, b_id, a_name, b_name):
    """Coaxiality of MATING CYLINDRICAL features across the interface — bore/boss & shaft/bore.

    A boss/shaft on one component that seats into a bore on the other should be COAXIAL. We
    match each boss to the nearest opposite-component bore whose radius it plausibly fits
    (|Δr| small) and whose axis is roughly aligned, then report the perpendicular axis offset
    and angular tilt. (Bore-to-bore hole registration is the hole_pattern check's job; this
    check is specifically the boss-in-bore / shaft-in-bore fit that hole_pattern can't see.)"""
    pairs = []

    def match_bosses(bosses, bores, boss_owner, bore_owner):
        used = set()
        for bo in bosses:
            best, best_off, best_ang, best_j = None, HOLE_MATCH_LATERAL_MM, None, None
            for j, br in enumerate(bores):
                if j in used:
                    continue
                # a boss can only seat into a bore of similar-or-larger radius
                if bo["radius"] - br["radius"] > 0.5:
                    continue
                off = _perp_distance_between_axes(bo["centroid"], bo["axis"],
                                                  br["centroid"], br["axis"])
                if off < best_off:
                    best, best_off, best_ang, best_j = br, off, \
                        _axis_angle_deg(bo["axis"], br["axis"]), j
            if best is None:
                continue
            used.add(best_j)
            ok = best_off <= offset_tol and best_ang <= angle_tol
            rec = {"a_feature": _hole_ref(bo) + f" ({boss_owner})",
                   "b_feature": _hole_ref(best) + f" ({bore_owner})",
                   "axis_offset_mm": round(best_off, 4), "axis_angle_deg": round(best_ang, 4),
                   "status": "pass" if ok else "fail"}
            if not ok:
                rec["fix"] = (f"re-align the {boss_owner} boss/shaft into the {bore_owner} bore: "
                              f"{best_off:.3f} mm axis offset, {best_ang:.2f}° tilt to remove")
                findings.append({
                    "severity": "fail", "component_a": a_id, "component_b": b_id,
                    "check": "coaxiality", "feature": _hole_ref(bo),
                    "error_mm": round(best_off, 4),
                    "detail": (f"{boss_owner} boss/shaft not coaxial with the {bore_owner} bore "
                               f"it mates: axis offset {best_off:.3f} mm, angular misalignment "
                               f"{best_ang:.2f}°"),
                    "fix": rec["fix"],
                })
            pairs.append(rec)

    match_bosses(a_bosses, b_bores, a_name, b_name)   # A's boss into B's bore
    match_bosses(b_bosses, a_bores, b_name, a_name)   # B's boss into A's bore
    status = "fail" if any(p["status"] == "fail" for p in pairs) else ("pass" if pairs else "skip")
    return {"status": status, "pairs": pairs,
            "summary": (f"{sum(1 for p in pairs if p['status']=='pass')}/{len(pairs)} "
                        f"boss/bore pairs coaxial") if pairs
                       else "no mating boss/shaft-in-bore features detected"}


def _check_contact_plane(a_id, b_id, a_name, b_name, a_planes, b_planes,
                         angle_tol, gap_tol, findings, reference=None):
    """Compare named or mate-referenced planes; a heuristic cannot establish intent."""
    if reference is None:
        return {"status": "skip", "gap_mm": None, "parallel_angle_deg": None,
                "a_feature": None, "b_feature": None,
                "summary": "contact faces unverified; supply contact_faces or a planar mate"}
    a_planes, b_planes, source = [reference[0]], [reference[1]], reference[2]
    best = None
    for fa in a_planes:
        na = _unit(_v(fa.get("normal", fa.get("axis", [0, 0, 1]))))
        ca = _v(fa.get("centroid", [0, 0, 0]))
        for fb in b_planes:
            nb = _unit(_v(fb.get("normal", fb.get("axis", [0, 0, 1]))))
            cb_c = _v(fb.get("centroid", [0, 0, 0]))
            ang = _axis_angle_deg(na, nb)          # 0..90 (parallel planes)
            # Explicit or mated references may have same-facing normals.
            gap = _dot(_sub(cb_c, ca), na)          # signed gap along A's outward normal
            lateral = _norm(_sub(_sub(cb_c, ca), (gap * na[0], gap * na[1], gap * na[2])))
            score = abs(gap) + 0.1 * lateral
            if best is None or score < best[0]:
                best = (score, gap, ang, fa, fb, lateral)
    if best is None:
        return {"status": "skip", "gap_mm": None, "parallel_angle_deg": None,
                "a_feature": None, "b_feature": None, "summary": "no opposed planar faces found"}
    _, gap, ang, fa, fb, lateral = best
    a_ref = f"{fa.get('side', 'face')} planar face ({fa.get('sid', 'unknown')})"
    b_ref = f"{fb.get('side', 'face')} planar face ({fb.get('sid', 'unknown')})"
    ok = abs(gap) <= gap_tol and ang <= angle_tol
    out = {"status": "pass" if ok else "fail", "gap_mm": round(gap, 4),
           "parallel_angle_deg": round(ang, 4), "a_feature": a_ref, "b_feature": b_ref,
           "reference_source": source,
           "summary": (f"contact gap {gap:+.3f} mm, {ang:.2f}° out of parallel")}
    if not ok:
        if abs(gap) > gap_tol and gap > 0:
            fix = f"close the {gap:.3f} mm gap — bring {b_name} into contact with {a_name}"
        elif abs(gap) > gap_tol:
            fix = f"back off {abs(gap):.3f} mm interference — {b_name} overshoots the {a_name} face"
        else:
            fix = f"tilt {b_name} mating face {ang:.2f}° to seat flush against {a_name}"
        out["fix"] = fix
        findings.append({
            "severity": "fail", "component_a": a_id, "component_b": b_id,
            "check": "contact_plane", "feature": a_ref, "error_mm": round(abs(gap), 4),
            "detail": (f"contact faces of {a_name}/{b_name} not flush: gap {gap:+.3f} mm, "
                       f"{ang:.2f}° out of parallel"),
            "fix": fix,
        })
    return out


def _check_fastener_passthrough(a_holes, b_holes, fastener, findings, a_id, b_id, a_name, b_name,
                               clearance_class="normal", position_tolerance_mm=0.0):
    """Evaluate straight-shank clearance of matched parallel bores, not thread engagement."""
    if not fastener:
        return {"status": "skip", "fastener": None, "stacks": [],
                "summary": "no fastener given (pass fastener='M6' to check pass-through)"}
    if (not isinstance(position_tolerance_mm, (int, float)) or isinstance(position_tolerance_mm, bool)
            or not math.isfinite(position_tolerance_mm) or position_tolerance_mm < 0):
        return {"status": "skip", "fastener": fastener, "stacks": [],
                "summary": "position tolerance must be a finite nonnegative radial distance"}
    from services.research.thread_spec import thread_spec
    from services.research.fits import iso273_clearance_hole
    spec = thread_spec(str(fastener))
    sp = spec.get("spec", {}) if isinstance(spec, dict) else {}
    nominal = sp.get("major_mm")
    if nominal is None:
        return {"status": "skip", "fastener": fastener, "stacks": [],
                "summary": f"fastener {fastener} has no resolvable nominal diameter"}
    # ISO 273 is metric; an inch fastener with the same converted diameter is not an ISO row.
    iso = (iso273_clearance_hole(nominal, clearance_class)
           if str(fastener).strip().lower().startswith("m")
           else {"ok": False, "reason": "non_metric_fastener"})
    if iso.get("reason") == "unsupported_clearance_class":
        return {"status": "skip", "fastener": fastener, "stacks": [],
                "summary": iso["hint"]}
    max_stacks = 100
    truncated = len(a_holes) > max_stacks or len(b_holes) > max_stacks
    a_holes, b_holes = a_holes[:max_stacks], b_holes[:max_stacks]
    stacks, used_b = [], set()
    for ha in a_holes:
        best, best_lat, best_j = None, HOLE_MATCH_LATERAL_MM, None
        for j, hb in enumerate(b_holes):
            if j in used_b or _axis_angle_deg(ha["axis"], hb["axis"]) >= 5.0:
                continue
            offset = _sub(hb["centroid"], ha["centroid"])
            along = _dot(offset, ha["axis"])
            lat = _norm(tuple(offset[k] - along * ha["axis"][k] for k in range(3)))
            if lat < best_lat:
                best, best_lat, best_j = hb, lat, j
        rec = {"nominal_mm": nominal, "a_hole": _hole_ref(ha), "passes": None}
        if best is None:
            rec.update(status="not_evaluated", reason="no matching bore on the other component")
            stacks.append(rec)
            continue
        used_b.add(best_j)
        rec["b_hole"] = _hole_ref(best)
        angle = _axis_angle_deg(ha["axis"], best["axis"])
        if angle > 1e-5:
            rec.update(status="not_evaluated", reason="nonparallel bores require finite bore extents")
            stacks.append(rec)
            continue
        min_dia = min(ha["dia"], best["dia"])
        clearance = min_dia - nominal
        # The two allowable shank-centre disks overlap iff their radii sum exceeds
        # the relative axis offset. Include the requested worst-case radial uncertainty.
        margin = (ha["dia"] - nominal) / 2 + (best["dia"] - nominal) / 2 - best_lat - position_tolerance_mm
        passes = clearance > 0 and margin > 0
        rec.update(status="pass" if passes else "fail", passes=passes,
                   min_hole_dia_mm=round(min_dia, 4), clearance_mm=round(clearance, 4),
                   axis_offset_mm=round(best_lat, 4),
                   position_tolerance_mm=position_tolerance_mm,
                   radial_margin_mm=round(margin, 4))
        if iso["ok"]:
            rec["iso273_meets_series"] = min_dia >= iso["diameter_mm"]
        if not passes:
            rec["fix"] = "reduce bore offset/position tolerance or enlarge the bores for the fastener"
            findings.append({
                "severity": "fail", "component_a": a_id, "component_b": b_id,
                "check": "fastener_passthrough", "feature": _hole_ref(ha),
                "error_mm": round(max(0.0, -clearance, -margin), 4),
                "detail": f"{fastener} cannot clear the nominal bore stack at the specified offset and tolerance",
                "fix": rec["fix"],
            })
        stacks.append(rec)
    for j, hb in enumerate(b_holes):
        if j not in used_b:
            if len(stacks) == max_stacks:
                truncated = True
                break
            stacks.append({"a_hole": None, "b_hole": _hole_ref(hb), "passes": None,
                           "status": "not_evaluated", "reason": "no matching bore on the other component"})
    status = ("fail" if any(s["passes"] is False for s in stacks)
              else "skip" if truncated or not stacks or any(s["passes"] is None for s in stacks) else "pass")
    return {"status": status, "fastener": fastener, "nominal_mm": nominal, "stacks": stacks,
            "truncated": truncated,
            "iso273": iso if iso["ok"] else {"status": "not_evaluated", "reason": iso["reason"]},
            "assumptions": ["nominal straight fastener and parallel cylindrical bores",
                            "position tolerance is additional worst-case radial relative offset"],
            "summary": f"{sum(s['passes'] is True for s in stacks)}/{len(stacks)} stacks clear {fastener}; "
                       "unmatched, angled or truncated stacks are not evaluated"}


# ---------------------------------------------------------------------------
# Pair discovery + top-level orchestration
# ---------------------------------------------------------------------------
def _bbox_gap(a_bbox: dict, b_bbox: dict) -> float:
    """Minimum axis-aligned gap between two bounding boxes (0 if they overlap)."""
    amin, amax = a_bbox["min"], a_bbox["max"]
    bmin, bmax = b_bbox["min"], b_bbox["max"]
    gap = 0.0
    for i in range(3):
        if amax[i] < bmin[i]:
            gap = max(gap, bmin[i] - amax[i])
        elif bmax[i] < amin[i]:
            gap = max(gap, amin[i] - bmax[i])
    return gap


def run_interface_check(
    doc_id: Optional[str] = None,
    components: Optional[list] = None,
    fastener: Optional[str] = None,
    proximity_mm: float = DEFAULT_PROXIMITY_MM,
    pos_tol_mm: float = DEFAULT_POS_TOL_MM,
    dia_tol_mm: float = DEFAULT_DIA_TOL_MM,
    coax_offset_tol_mm: float = DEFAULT_COAX_OFFSET_TOL_MM,
    coax_angle_tol_deg: float = DEFAULT_COAX_ANGLE_TOL_DEG,
    plane_angle_tol_deg: float = DEFAULT_PLANE_ANGLE_TOL_DEG,
    plane_gap_tol_mm: float = DEFAULT_PLANE_GAP_TOL_MM,
    contact_faces: Optional[list] = None,
    clearance_class: str = "normal",
    fastener_position_tolerance_mm: float = 0.0,
) -> str:
    """Core implementation (importable by tests + the verify battery without going through MCP)."""
    from services.mcp import core_bridge as cb
    from services.mcp.cad_tools import _doc, _dumps, _err
    try:
        d = _doc(doc_id)
        wb = cb.call("asm_world_bodies", {"doc_id": d, "explode": False})
        bodies = wb.get("bodies", [])
        if len(bodies) < 2:
            return _dumps({"ok": True, "status": "skip", "doc_id": d, "tool": "interface_check",
                           "version": 1,
                           "components": [{"id": b.get("component") or b.get("name"),
                                           "name": b.get("name"), "bbox": b.get("bbox")}
                                          for b in bodies],
                           "body_count": len(bodies), "pair_selection": "none",
                           "pairs": [], "findings": [],
                           "summary": "interface_check needs >=2 components/bodies; nothing to compare.",
                           "scope_note": V1_SCOPE_NOTE})

        # Map component id -> 1-based body_id (doc_query body selection) + metadata.
        comp_meta = []
        for idx, b in enumerate(bodies):
            comp_meta.append({"id": b.get("component") or b.get("name") or f"b{idx+1}",
                              "name": b.get("name") or b.get("component") or f"b{idx+1}",
                              "bbox": b.get("bbox"), "body_id": idx + 1})
        id_to_meta = {c["id"]: c for c in comp_meta}
        if contact_faces is not None:
            if (not isinstance(contact_faces, list) or len(contact_faces) != 2
                    or any(not isinstance(r, dict) or not isinstance(r.get("component"), str)
                           or not isinstance(r.get("sid"), str) or not r["sid"]
                           for r in contact_faces)
                    or len({r["component"] for r in contact_faces}) != 2
                    or any(r["component"] not in id_to_meta for r in contact_faces)):
                return _dumps({"ok": False, "code": "bad_contact_faces",
                               "error": "contact_faces must be two {component, sid} references "
                                        "on distinct known components."})
        tree = cb.call("doc_tree", {"doc_id": d}).get("tree") or {}
        tree_components = {c["id"]: c for c in tree.get("components", [])
                           if isinstance(c, dict) and c.get("id")}


        # Determine which pairs to examine.
        #
        # `components` MUST be validated before use. It used to be filtered with a silent
        # `if c in id_to_meta`, so naming a component that does not exist in this document
        # produced zero pairs, no findings and ok=True — a FALSE PASS. The caller asked "do
        # these two line up?" and was told "yes", while the summary blamed proximity_mm and
        # never mentioned that the ids were unknown. Refuse instead, and teach: name the bad
        # value, list the ids this document actually has, and show the corrected call.
        pairs_to_check: list[tuple] = []
        pair_selection = "proximity"
        if components:
            if not isinstance(components, (list, tuple)):
                return _dumps({
                    "ok": False, "doc_id": d, "tool": "interface_check", "version": 1,
                    "code": "bad_components",
                    "error": f"`components` must be a list of component ids, got "
                             f"{type(components).__name__}. Available in this document: "
                             f"{[c['id'] for c in comp_meta]}. "
                             f"Corrected call: interface_check(doc_id={d!r}, "
                             f"components={[c['id'] for c in comp_meta[:2]]})",
                    "available_components": [c["id"] for c in comp_meta]})
            unknown = [c for c in components if c not in id_to_meta]
            if unknown:
                return _dumps({
                    "ok": False, "doc_id": d, "tool": "interface_check", "version": 1,
                    "code": "unknown_component",
                    "error": f"unknown component id(s) {unknown} — this document has "
                             f"{len(comp_meta)} body/bodies with ids "
                             f"{[c['id'] for c in comp_meta]}. Component ids come from "
                             f"interface_check(doc_id) with no `components` (its "
                             f"`components[].id`), or from describe_assembly/"
                             f"asm_world_bodies. Corrected call: "
                             f"interface_check(doc_id={d!r}, "
                             f"components={[c['id'] for c in comp_meta[:2]]})",
                    "unknown_components": unknown,
                    "available_components": [c["id"] for c in comp_meta]})
            named = [id_to_meta[c] for c in dict.fromkeys(components)]
            if len(named) < 2:
                return _dumps({
                    "ok": False, "doc_id": d, "tool": "interface_check", "version": 1,
                    "code": "components_needs_pair",
                    "error": f"`components` names {len(named)} distinct component(s) "
                             f"{[c['id'] for c in named]}; an interface is between TWO, so "
                             f"name at least two. Available: "
                             f"{[c['id'] for c in comp_meta]}. Omit `components` entirely to "
                             f"check every adjacent pair automatically, or use the corrected "
                             f"call: interface_check(doc_id={d!r}, "
                             f"components={[c['id'] for c in comp_meta[:2]]})",
                    "available_components": [c["id"] for c in comp_meta]})
            pair_selection = "explicit"
            # explicit list: check all unordered pairs among the named components
            for i in range(len(named)):
                for j in range(i + 1, len(named)):
                    pairs_to_check.append((named[i], named[j]))
        else:
            for i in range(len(comp_meta)):
                for j in range(i + 1, len(comp_meta)):
                    a, b = comp_meta[i], comp_meta[j]
                    if a["bbox"] and b["bbox"] and _bbox_gap(a["bbox"], b["bbox"]) <= proximity_mm:
                        pairs_to_check.append((a, b))
        if contact_faces is not None:
            face_ids = {r["component"] for r in contact_faces}
            if not any({a["id"], b["id"]} == face_ids for a, b in pairs_to_check):
                if components:
                    return _dumps({"ok": False, "code": "bad_contact_faces",
                                   "error": "contact_faces components must be included in components."})
                pairs_to_check.append(tuple(id_to_meta[r["component"]] for r in contact_faces))
                pair_selection = "contact_faces"


        # Cache per-body geometry. Cylinders are split once into (bores, bosses):
        # bores drive hole-pattern + fastener checks, bores AND bosses drive coaxiality.
        cyl_cache: dict[int, tuple[list, list]] = {}
        planes_cache: dict[int, list] = {}

        def cylinders_for(meta):
            bid = meta["body_id"]
            if bid not in cyl_cache:
                cyl_cache[bid] = _cylinders_from_faces(_round_faces_for_body(cb, d, bid))
            return cyl_cache[bid]

        def planes_for(meta):
            bid = meta["body_id"]
            if bid not in planes_cache:
                planes_cache[bid] = _planar_faces_for_body(cb, d, bid)
            return planes_cache[bid]

        def contact_reference(a, b):
            a_planes, b_planes = planes_for(a), planes_for(b)
            if contact_faces is not None:
                refs = {r["component"]: r["sid"] for r in contact_faces}
                if a["id"] in refs and b["id"] in refs:
                    aa = [f for f in a_planes if f.get("sid") == refs[a["id"]]]
                    bb = [f for f in b_planes if f.get("sid") == refs[b["id"]]]
                    if len(aa) != 1 or len(bb) != 1:
                        raise ValueError("contact_faces must identify one world planar face "
                                         "on each named component")
                    return aa[0], bb[0], "explicit"
            from services.mcp.cad_render_tools import _mate_face_matches
            for mate in tree.get("mates", []):
                if (not isinstance(mate, dict) or mate.get("suppressed")
                        or mate.get("type") not in ("coincident", "lock", "distance")):
                    continue
                refs = {r.get("component"): r for r in mate.get("refs", [])
                        if isinstance(r, dict)}
                if a["id"] not in refs or b["id"] not in refs:
                    continue
                aa = _mate_face_matches(refs[a["id"]], tree_components, {"face": a_planes})
                bb = _mate_face_matches(refs[b["id"]], tree_components, {"face": b_planes})
                if len(aa) == len(bb) == 1:
                    return aa[0], bb[0], "mate:" + str(mate.get("id", ""))
            return None

        all_findings: list[dict] = []
        pair_results: list[dict] = []
        for a, b in pairs_to_check:
            a_bores, a_bosses = cylinders_for(a)
            b_bores, b_bosses = cylinders_for(b)
            checks = {}
            # Hole-pattern registration is bore-to-bore across the interface.
            checks["hole_pattern"] = _check_hole_pattern(
                a_bores, b_bores, pos_tol_mm, dia_tol_mm, all_findings,
                a["id"], b["id"], a["name"], b["name"])
            # Coaxiality is boss/shaft-in-bore (A's boss into B's bore and vice-versa).
            checks["coaxiality"] = _check_coaxiality(
                a_bosses, b_bores, b_bosses, a_bores, coax_offset_tol_mm, coax_angle_tol_deg,
                all_findings, a["id"], b["id"], a["name"], b["name"])
            checks["contact_plane"] = _check_contact_plane(
                a["id"], b["id"], a["name"], b["name"],
                planes_for(a), planes_for(b), plane_angle_tol_deg, plane_gap_tol_mm, all_findings,
                contact_reference(a, b))
            # Fastener pass-through is through the bore stack.
            checks["fastener_passthrough"] = _check_fastener_passthrough(
                a_bores, b_bores, fastener, all_findings,
                a["id"], b["id"], a["name"], b["name"],
                clearance_class, fastener_position_tolerance_mm)
            clearance = {s["a_hole"]: s for s in checks["fastener_passthrough"]["stacks"]}
            for rec in checks["hole_pattern"]["matched"] + checks["hole_pattern"]["misaligned"]:
                if rec["diameter_fit"] == "different":
                    stack = clearance.get(rec["a_hole"]["ref"])
                    rec["clearance_verdict"] = ("unverified" if stack is None or stack["passes"] is None else
                                                  "clear" if stack["passes"] else "insufficient")
            pair_results.append({"a": a["id"], "b": b["id"],
                                 "a_name": a["name"], "b_name": b["name"], "checks": checks})

        fails = [f for f in all_findings if f["severity"] == "fail"]
        # An alignment failure is a finding, not a tool failure: ok stays true.
        status = "fail" if fails else ("pass" if pairs_to_check else "skip")
        # The scope the verdict covers is part of the verdict. Without it a caller who left a
        # scratch body in the document reads "All 1 interface(s) aligned" and cannot tell that
        # the tool weighed 5 bodies, nor which pairing rule produced the pairs it reports.
        scope = (f"scope: {len(comp_meta)} body/bodies in the document, "
                 f"{len(pairs_to_check)} pair(s) checked "
                 + ("(explicit components=[...])" if pair_selection == "explicit"
                    else "(includes explicit contact_faces)" if pair_selection == "contact_faces"
                    else f"(auto: bbox gap <= proximity_mm {proximity_mm} mm)") + ".")
        if not pairs_to_check:
            summary = ("No adjacent component pairs found within proximity_mm "
                       f"({proximity_mm} mm) among the {len(comp_meta)} bodies of this "
                       "document — name components=[a,b] to force a check.")
        elif not fails:
            summary = (f"No alignment failures in {len(pairs_to_check)} interface(s) "
                       f"({len(all_findings)} advisory note(s)); skipped checks remain unverified. {scope}")
        else:
            summary = (f"{len(fails)} interface alignment failure(s) across "
                       f"{len(pairs_to_check)} interface(s): "
                       + "; ".join(sorted({f['check'] for f in fails})) + f". {scope}")

        return _dumps({
            "ok": True, "status": status, "doc_id": d, "tool": "interface_check", "version": 1,
            "components": [{"id": c["id"], "name": c["name"], "bbox": c["bbox"]} for c in comp_meta],
            "body_count": len(comp_meta), "pair_selection": pair_selection,
            "pairs": pair_results, "findings": all_findings,
            "summary": summary, "scope_note": V1_SCOPE_NOTE,
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


def register_interface_check_tools(mcp: "FastMCP") -> None:
    """Register interface_check as an MCP tool onto *mcp* (append-only in server.py)."""

    @mcp.tool()
    def interface_check(
        doc_id: Optional[str] = None,
        components: Optional[list] = None,
        fastener: Optional[str] = None,
        proximity_mm: float = DEFAULT_PROXIMITY_MM,
        contact_faces: Optional[list] = None,
        clearance_class: str = "normal",
        fastener_position_tolerance_mm: float = 0.0,
    ) -> str:
        """Design-time INTERFACE ALIGNMENT check between mating components — the errors that
        pass check_interference (nothing overlaps) but are still wrong.

        Given an assembly / multi-body doc, examines each candidate mating pair (adjacent
        bounding boxes, or the components=["a","b"] you name) and reports, per interface:
          * HOLE-PATTERN alignment — flags position deltas, separately records diameter differences
            and their nominal fastener clearance verdict, and lists unmatched holes.
          * COAXIALITY — mating bore/shaft axes: perpendicular offset (mm) + angular tilt (deg).
          * CONTACT PLANE — explicit or mate-referenced faces: signed gap and parallelism.
            Without resolvable references the contact check is skipped, not passed.
          * FASTENER PASS-THROUGH — evaluates nominal straight-shank clearance for matched,
            parallel bores with positional uncertainty; reports ISO 273 metric hole series.

        Every finding names the components + the feature (plain-words ref), the numeric error,
        and a FIX DIRECTION, so an agent can act on it during layout — e.g. "hole pattern on
        plate offset 1.50 mm in +X relative to bracket".

        Args:
          doc_id:       assembly / multi-body part doc (default: current). Call solve_mates first
                        on a mate-driven assembly so components sit at their solved placement.
          components:   optional [a, b, ...] component/body ids to force-check as pairs (bypasses
                        the proximity heuristic). Needs at least TWO distinct KNOWN ids: an id
                        this document does not have is refused (code "unknown_component", with
                        the available ids), never silently dropped. Omit to auto-detect adjacent
                        pairs. The ids are the `components[].id` of an un-scoped call.
          fastener:     optional thread designation ("M6", "1/4-20") to run the pass-through check.
          proximity_mm: bbox adjacency gap for auto-pair detection (default 2.0 mm).
          contact_faces: optional [{"component":"k1","sid":"F..."}, {"component":"k2","sid":"F..."}]
                         using world planar face SIDs from query_geometry; overrides mate references.
          clearance_class: ISO 273 fine/close, medium/normal or coarse/loose (default normal).
          fastener_position_tolerance_mm: additional worst-case radial relative bore-position
                        uncertainty in mm (not a diametral GD&T zone; default 0).

        Returns a STABLE JSON verdict (tool="interface_check", version 1) with per-pair `checks`
        and a flat `findings` list. `status` is the verdict: "fail" when a finding has severity
        "fail", "pass" when no checked finding fails (skipped checks remain unverified), or
        "skip" when no pair was compared. A
        misalignment is a finding, so ok stays true. ok=false means the call was refused (bad
        or unknown components) or could not run. See `scope_note` for what v1 does NOT infer.
        This is the ALIGNMENT complement to check_interference (clash), not a replacement — run
        both.
        """
        return run_interface_check(doc_id=doc_id, components=components,
                                   fastener=fastener, proximity_mm=proximity_mm,
                                   contact_faces=contact_faces, clearance_class=clearance_class,
                                   fastener_position_tolerance_mm=fastener_position_tolerance_mm)
