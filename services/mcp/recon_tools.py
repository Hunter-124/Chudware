"""Recon MCP tools — reverse-engineer an imported STEP/mesh body into an EDITABLE parametric
feature tree, verified against the source solid by boolean comparison.

  reverse_engineer(source)        -> import (path) or take a doc_id, run the deterministic
                                     recognizer, rebuild base/holes/patterns/fillets as real
                                     features in a NEW part, verify every batch with
                                     eval_compare, and report match % + what was NOT explained.
  compare_parts(doc_a, doc_b)     -> thin wrapper over the core's eval_compare (boolean
                                     common/missing/extra between two docs' solids).
  convert_face_to_sketch(face)    -> "convert entities": one planar face's wire loops as a new
                                     editable sketch feature ON that face.

Design contract (docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md §3.4/§3.6): DETERMINISTIC passes
first; heuristics never suppress the raw face list — unclaimed faces are always reported, and
every applied batch is verified against the source solid (regression -> revert -> `rejected`).

The recognizer (`_recognize`) runs 5 deterministic passes over the doc_query face cards
(sid/surf/side/normal/concavity/radius/centroid/area) + doc_adjacency + doc_bbox:
  1. prismatic base  — largest planar face + antiparallel partner at distance d, with
                       perpendicular/parallel laterals explaining >=80% of the remaining area;
                       profile pulled from doc_face_wires (outer wire, fillet arcs un-rounded
                       back to sharp corners so dress-ups stay separate features).
  2. turned body     — every curved face coaxial on a principal axis through the origin ->
                       ANALYTIC revolve profile from the face params (radius + span from
                       area/(2*pi*r)); no sectioning.
  3. holes           — bore-concavity cylinders -> hole {d=2r, depth|through} entered from the
                       planar cap the bore opens onto.
  4. patterns        — gsl_tools._axis_order over same-radius hole groups -> ONE seed hole +
                       circular pattern (a collinear equal-spacing fallback -> linear pattern).
  5. fillet/chamfer  — CANDIDATES only: small convex cylinders/tori (fillets) and 45 deg +/- 2
                       planar bridges (chamfers). Applied only when a plain-word edge query
                       resolves on the REBUILT body AND the match does not regress; otherwise
                       reported under `candidates` for the caller to try.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from mcp_server import core_bridge as cb
from mcp_server.cad_tools import _CUR, _doc, _dumps, _err, _publish_current, _read_prompt
from mcp_server.gsl_tools import _axis_order

# cad_guide("reverse") lazy-imports this name; the guide text ships as a normal cheatsheet so
# cad_guide("reverse_engineer") auto-discovers it too. Fallback keeps the import safe when the
# prompts dir is missing (e.g. a bare checkout without staged prompts).
_RE_GUIDE = _read_prompt("shared/cheatsheets/reverse_engineer.md") or (
    "REVERSE-ENGINEERING: reverse_engineer(source) imports a STEP/mesh (or takes an open "
    "doc_id), recognizes prismatic/turned bases, holes, patterns and fillet/chamfer candidates "
    "deterministically, rebuilds them as an editable feature tree in a new part, and verifies "
    "each batch with a boolean compare against the source. Read `match_pct`, `candidates`, "
    "`unexplained` and `next_steps` in the result; compare_parts / convert_face_to_sketch "
    "cover manual workflows.")


# ---------------------------------------------------------------- small vector helpers
_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s: float):
    return (a[0] * s, a[1] * s, a[2] * s)


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return _scale(a, 1.0 / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _principal_axis(v, tol: float = 1e-3) -> Optional[str]:
    """The principal axis letter v is (anti)parallel to, or None."""
    u = _unit(v)
    for letter, w in _AXES.items():
        if abs(abs(_dot(u, w)) - 1.0) < tol:
            return letter
    return None


def _perp(a, w):
    """Component of `a` perpendicular to unit direction `w`."""
    return _sub(a, _scale(w, _dot(a, w)))


def _f3(rec: dict, key: str):
    v = rec.get(key)
    return tuple(float(x) for x in v) if isinstance(v, list) and len(v) == 3 else None


# ---------------------------------------------------------------- reserved-plane frames
# Verified against the engine (see geom/plane.h + an empirical probe): a sketch with params
# `offset` sits at (normal * offset) and a blind extrude runs along +normal.
#   plane.top   u=+X v=+Y n=+Z      plane.front u=+X v=+Z n=-Y      plane.right u=+Y v=+Z n=+X
_RESERVED = {
    "z": {"plane": "plane.top", "u": (1.0, 0.0, 0.0), "v": (0.0, 1.0, 0.0),
          "n": (0.0, 0.0, 1.0)},
    "y": {"plane": "plane.front", "u": (1.0, 0.0, 0.0), "v": (0.0, 0.0, 1.0),
          "n": (0.0, -1.0, 0.0)},
    "x": {"plane": "plane.right", "u": (0.0, 1.0, 0.0), "v": (0.0, 0.0, 1.0),
          "n": (1.0, 0.0, 0.0)},
}


def _frame_of(plane_frame: list) -> Optional[dict]:
    """Decode doc_face_wires' 16-float row-major plane_frame into {origin, u, v, n}."""
    if not isinstance(plane_frame, list) or len(plane_frame) < 16:
        return None
    m = plane_frame
    return {
        "u": (m[0], m[4], m[8]), "v": (m[1], m[5], m[9]),
        "n": (m[2], m[6], m[10]), "origin": (m[3], m[7], m[11]),
    }


def _entities_to_reserved(entities: list, face_frame: dict, res: dict) -> Optional[list]:
    """Re-express face_wires 2D entities (authored in the face's own plane frame) in a RESERVED
    plane's frame, so they can be re-authored as a sketch on plane.top/front/right + offset.

    Both frames are orthonormal and share the same plane (face normal is +/- the reserved
    normal), so the map is a rigid/reflective 2D transform p -> M p + t with det(M) = +/-1;
    a negative determinant flips arc sweep direction (ccw). Returns None on an entity type the
    transform can't carry exactly (ellipse_arc) so the caller can fall back to unclaimed."""
    fu, fv, fo = face_frame["u"], face_frame["v"], face_frame["origin"]
    ru, rv = res["u"], res["v"]
    m00, m01 = _dot(ru, fu), _dot(ru, fv)
    m10, m11 = _dot(rv, fu), _dot(rv, fv)
    tx, ty = _dot(ru, fo), _dot(rv, fo)
    det = m00 * m11 - m01 * m10

    def pt(p):
        return [m00 * p[0] + m01 * p[1] + tx, m10 * p[0] + m11 * p[1] + ty]

    def ang(a_deg: float, center) -> float:
        d = (math.cos(math.radians(a_deg)), math.sin(math.radians(a_deg)))
        w = (m00 * d[0] + m01 * d[1], m10 * d[0] + m11 * d[1])
        return math.degrees(math.atan2(w[1], w[0])) % 360.0

    out = []
    for e in entities:
        t = e.get("type")
        ne = {"id": e["id"], "type": t, "construction": bool(e.get("construction"))}
        if t == "line":
            ne["p1"], ne["p2"] = pt(e["p1"]), pt(e["p2"])
        elif t == "circle":
            ne["center"], ne["radius"] = pt(e["center"]), e["radius"]
        elif t == "arc":
            ne["center"], ne["radius"] = pt(e["center"]), e["radius"]
            ne["start"], ne["end"] = ang(e["start"], e["center"]), ang(e["end"], e["center"])
            ne["ccw"] = bool(e.get("ccw", True)) ^ (det < 0)
        elif t == "ellipse":
            ne["center"], ne["rx"], ne["ry"] = pt(e["center"]), e["rx"], e["ry"]
            ne["rot"] = ang(e.get("rot", 0.0), e["center"])
        else:  # ellipse_arc (or anything new): trim parameters don't survive a reflection
            return None
        out.append(ne)
    return out


def _defillet_wire(entity_ids: list, ents_by_id: dict, radii: list, tol: float = 1e-2) -> list:
    """Un-round a wire: replace each line-ARC-line corner whose arc radius matches a detected
    vertical fillet candidate with the sharp line intersection, so the fillet is NOT baked into
    the base profile (it stays recoverable as a separate, editable fillet feature).

    Works on the wire's cyclic entity order (face_wires emits entities in traversal order).
    Arcs that don't match a candidate radius, or whose neighbours are parallel/not lines, are
    kept as-is. Returns the surviving entity list (mutated copies)."""
    ents = [dict(ents_by_id[eid]) for eid in entity_ids]
    n = len(ents)
    if n < 3 or not radii:
        return ents
    drop = set()
    for i, e in enumerate(ents):
        if e.get("type") != "arc":
            continue
        r = float(e.get("radius") or 0.0)
        if not any(abs(r - rr) <= max(tol, 0.01 * rr) for rr in radii):
            continue
        a, b = ents[(i - 1) % n], ents[(i + 1) % n]
        if a.get("type") != "line" or b.get("type") != "line" or (i - 1) % n in drop:
            continue
        p1, p2, q1, q2 = a["p1"], a["p2"], b["p1"], b["p2"]
        da = (p2[0] - p1[0], p2[1] - p1[1])
        db = (q2[0] - q1[0], q2[1] - q1[1])
        den = da[0] * db[1] - da[1] * db[0]
        if abs(den) < 1e-9 * (math.hypot(*da) * math.hypot(*db) + 1e-12):
            continue  # near-parallel lines (e.g. a slot end cap) — keep the arc
        s = ((q1[0] - p1[0]) * db[1] - (q1[1] - p1[1]) * db[0]) / den
        x = [p1[0] + s * da[0], p1[1] + s * da[1]]
        a["p2"] = list(x)
        b["p1"] = list(x)
        drop.add(i)
    return [e for i, e in enumerate(ents) if i not in drop]


def _reid(entities: list) -> list:
    for i, e in enumerate(entities):
        e["id"] = f"e{i + 1}"
    return entities


def _dim(v: float) -> dict:
    return {"value": round(float(v), 6), "expr": None}


# ---------------------------------------------------------------- the 5-pass recognizer

def _recognize(ref_doc_id: str) -> dict:
    """Run the 5 deterministic recognition passes over `ref_doc_id`'s active body.

    Returns a PLAN (no geometry is written): {kind, base, holes, patterns, fillet_candidates,
    chamfer_candidates, claimed, unclaimed, notes}. Heuristics never suppress data — every
    source face lands either in `claimed` (with the pass that owns it) or in `unclaimed`."""
    faces = cb.call("doc_query", {"doc_id": ref_doc_id,
                                  "query": {"of": "face"}}).get("matches", [])
    if not faces:
        raise ValueError(f"document {ref_doc_id!r} has no solid bodies to recognize")
    try:
        adj = cb.call("doc_adjacency", {"doc_id": ref_doc_id})
    except Exception:  # noqa: BLE001
        adj = {}
    bbox = cb.call("doc_bbox", {"doc_id": ref_doc_id})
    size = bbox.get("size") or [0.0, 0.0, 0.0]
    center = bbox.get("center") or [0.0, 0.0, 0.0]
    dims = sorted(float(s) for s in size)
    diag = _norm(size)

    plan: dict = {"kind": None, "base": None, "holes": [], "patterns": [],
                  "fillet_candidates": [], "chamfer_candidates": [],
                  "claimed": {}, "unclaimed": [], "notes": [],
                  "face_count": len(faces), "bbox_mm": [round(s, 3) for s in size]}
    claimed = plan["claimed"]

    planar = [f for f in faces if f.get("surf") == "plane"]
    curved = [f for f in faces if f.get("surf") != "plane"]

    # ---- pass 5 (detection first — pass 1 needs the fillet radii to un-round the profile).
    # Dress-up size bound: the handoff spec says r <= 20% of the MIN bbox dim, but that
    # under-fires on thin plates (its own acceptance part is an 8 mm plate with an r3 fillet:
    # 3 > 0.2*8), so the bound uses the MEDIAN bbox dim — same intent (a fillet is small
    # relative to the part), calibrated so the spec's acceptance geometry is representable.
    r_max = 0.2 * dims[1] if dims[1] > 0 else 0.0
    for f in curved:
        surf, r = f.get("surf"), f.get("radius")
        if surf not in ("cylinder", "torus") or not r or r > r_max:
            continue
        if f.get("concavity") == "bore":
            continue  # concave/bore cylinders are the hole pass's material
        c, ax = _f3(f, "centroid"), _f3(f, "axis")
        if c is None:
            continue
        near = list(c)
        if surf == "cylinder" and ax is not None:
            out = _unit(_perp(_sub(c, center), _unit(ax)))
            near = [c[i] + out[i] * 0.6 * float(r) for i in range(3)]
        plan["fillet_candidates"].append({
            "kind": "fillet", "surf": surf, "radius": float(r), "sids": [f.get("sid")],
            "axis": list(ax) if ax else None, "near": [round(x, 4) for x in near],
        })
        claimed[f.get("sid")] = "fillet_candidate"

    # chamfer candidates: a planar face whose normal sits 45 deg (+/-2) off two PERPENDICULAR
    # planar neighbours — the classic edge-break bridge.
    adj_faces = adj.get("faces") or []
    neighbors = {af.get("ord"): af.get("neighbors") or [] for af in adj_faces
                 if isinstance(af, dict)}
    for i, f in enumerate(faces):
        if f.get("surf") != "plane":
            continue
        n_i = _f3(f, "normal")
        if n_i is None or _principal_axis(n_i, 5e-2) is not None:
            continue  # axis-aligned planes are base/lateral material, not chamfer bridges
        bridged = []
        for j in neighbors.get(i, []):
            if not (0 <= j < len(faces)) or faces[j].get("surf") != "plane":
                continue
            n_j = _f3(faces[j], "normal")
            if n_j is None:
                continue
            angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(_dot(_unit(n_i),
                                                                       _unit(n_j)))))))
            if abs(angle - 45.0) <= 2.0:
                bridged.append(j)
        if len(bridged) >= 2:
            na, nb = _f3(faces[bridged[0]], "normal"), _f3(faces[bridged[1]], "normal")
            if na is None or nb is None or abs(_dot(_unit(na), _unit(nb))) > 5e-2:
                continue  # the two bridged planes must be perpendicular to each other
            width = None
            try:
                fw = cb.call("doc_face_wires", {"doc_id": ref_doc_id,
                                                "face": {"sid": f.get("sid")}})
                pts = [p for e in fw.get("entities", []) for p in (e.get("p1"), e.get("p2"))
                       if isinstance(p, list)]
                if pts:
                    w = min(max(p[0] for p in pts) - min(p[0] for p in pts),
                            max(p[1] for p in pts) - min(p[1] for p in pts))
                    width = w
            except Exception:  # noqa: BLE001
                width = None
            c = _f3(f, "centroid")
            plan["chamfer_candidates"].append({
                "kind": "chamfer", "sids": [f.get("sid")],
                "length": round(width * math.cos(math.radians(45.0)), 4) if width else None,
                "near": [round(x, 4) for x in c] if c else None,
            })
            claimed[f.get("sid")] = "chamfer_candidate"

    fillet_radii_by_axis: dict = defaultdict(list)
    for fc in plan["fillet_candidates"]:
        if fc["surf"] == "cylinder" and fc.get("axis"):
            letter = _principal_axis(fc["axis"])
            if letter:
                fillet_radii_by_axis[letter].append(fc["radius"])

    # ---- pass 2: turned body — checked BEFORE prismatic (a shaft's end cap would otherwise
    # masquerade as a prismatic base). Signature: every curved face coaxial on one principal
    # axis THROUGH THE ORIGIN, every planar face perpendicular to it, and at least one convex
    # (outer) curved face.
    turned_axis = None
    if curved:
        for letter, w in _AXES.items():
            if not all((ax := _f3(f, "axis")) and abs(abs(_dot(_unit(ax), w)) - 1.0) < 1e-3
                       for f in curved):
                continue
            if not all((c := _f3(f, "centroid")) and
                       _norm(_perp(c, w)) < max(0.05, 1e-4 * diag) for f in curved):
                continue
            if not all((n := _f3(f, "normal")) and abs(abs(_dot(_unit(n), w)) - 1.0) < 1e-3
                       for f in planar):
                continue
            if not any(f.get("concavity") != "bore" for f in curved):
                continue
            turned_axis = letter
            break

    if turned_axis:
        w = _AXES[turned_axis]
        segs = []
        for f in curved:
            if f.get("surf") != "cylinder" or f.get("concavity") == "bore":
                continue
            r = float(f.get("radius") or 0.0)
            area = float(f.get("area_or_len") or 0.0)
            c = _f3(f, "centroid")
            if r <= 0 or area <= 0 or c is None:
                continue
            h = area / (2.0 * math.pi * r)
            tc = _dot(c, w)
            segs.append({"t0": tc - h / 2.0, "t1": tc + h / 2.0, "r": r,
                         "sid": f.get("sid")})
        segs.sort(key=lambda s: s["t0"])
        cap_ts = sorted({round(_dot(_f3(f, "centroid"), w), 3) for f in planar
                         if _f3(f, "centroid")})
        if segs and cap_ts:

            def snap(t: float) -> float:
                best = min(cap_ts, key=lambda ct: abs(ct - t))
                return best if abs(best - t) < 0.5 else t

            for s in segs:
                s["t0"], s["t1"] = snap(s["t0"]), snap(s["t1"])
            # merge same-radius contiguity, then close residual gaps at the midpoint (a small
            # shoulder fillet/chamfer eats into the neighbouring spans).
            merged = [segs[0]]
            for s in segs[1:]:
                p = merged[-1]
                if abs(s["r"] - p["r"]) < 1e-6 and s["t0"] <= p["t1"] + 0.5:
                    p["t1"] = max(p["t1"], s["t1"])
                    p["sid"] = p["sid"]
                else:
                    merged.append(s)
            for a, b in zip(merged, merged[1:]):
                if b["t0"] - a["t1"] > 1e-6:
                    mid = 0.5 * (a["t1"] + b["t0"])
                    a["t1"], b["t0"] = mid, mid
            merged[0]["t0"] = min(merged[0]["t0"], cap_ts[0])
            merged[-1]["t1"] = max(merged[-1]["t1"], cap_ts[-1])
            plan["kind"] = "turned"
            plan["base"] = {"type": "revolve", "axis": turned_axis, "segments": [
                {"t0": round(s["t0"], 6), "t1": round(s["t1"], 6), "r": round(s["r"], 6)}
                for s in merged]}
            for f in curved:
                if f.get("surf") == "cylinder" and f.get("concavity") != "bore":
                    claimed[f.get("sid")] = "base"
            for f in planar:
                claimed.setdefault(f.get("sid"), "base")
            # On a turned body every coaxial non-bore CYLINDER is a profile segment, so it
            # must not double as a fillet candidate (turned fillets are the TORI); keep the
            # candidate list consistent with the claims just made.
            plan["fillet_candidates"] = [
                fc for fc in plan["fillet_candidates"]
                if not (fc["surf"] == "cylinder" and
                        claimed.get(fc["sids"][0]) == "base")]
        else:
            plan["notes"].append("turned signature detected but no cylinder/cap data to "
                                 "build a profile from")

    # ---- pass 1: prismatic base (when the turned pass didn't claim the body).
    if plan["kind"] is None and planar:
        f_base = max(planar, key=lambda f: float(f.get("area_or_len") or 0.0))
        n_base = _f3(f_base, "normal")
        letter = _principal_axis(n_base) if n_base else None
        if letter is None:
            plan["notes"].append("largest planar face is not axis-aligned — no deterministic "
                                 "base recognized")
        else:
            nb = _unit(n_base)
            partners = []
            for p in planar:
                np_ = _f3(p, "normal")
                if np_ is None or _dot(_unit(np_), nb) > -0.999:
                    continue
                d = abs(_dot(_sub(_f3(p, "centroid"), _f3(f_base, "centroid")), nb))
                if d > 1e-3:
                    partners.append((p, d))
            if not partners:
                plan["notes"].append("no antiparallel partner face for the largest planar "
                                     "face — no deterministic base recognized")
            else:
                partners.sort(key=lambda pd: (-float(pd[0].get("area_or_len") or 0.0),
                                              -pd[1]))
                f_part, depth = partners[0]
                rest = [f for f in faces if f.get("sid") not in
                        (f_base.get("sid"), f_part.get("sid"))]
                rest_area = sum(float(f.get("area_or_len") or 0.0) for f in rest)
                lateral = []
                for f in rest:
                    if f.get("surf") == "plane":
                        n = _f3(f, "normal")
                        if n is not None and abs(_dot(_unit(n), nb)) < 0.05:
                            lateral.append(f)
                    else:
                        ax = _f3(f, "axis")
                        if ax is not None and abs(abs(_dot(_unit(ax), nb)) - 1.0) < 1e-3:
                            lateral.append(f)
                lat_area = sum(float(f.get("area_or_len") or 0.0) for f in lateral)
                if rest_area > 1e-9 and lat_area < 0.8 * rest_area:
                    plan["notes"].append(
                        f"laterals explain only {100.0 * lat_area / rest_area:.0f}% of the "
                        "non-cap area (<80%) — no deterministic prismatic base")
                else:
                    try:
                        fw = cb.call("doc_face_wires", {"doc_id": ref_doc_id,
                                                        "face": {"sid": f_base.get("sid")}})
                    except Exception as e:  # noqa: BLE001
                        fw = None
                        plan["notes"].append(f"doc_face_wires failed on the base face: {e}")
                    if fw:
                        ents_by_id = {e["id"]: e for e in fw.get("entities", [])}
                        outer = next((wr for wr in fw.get("wires", []) if wr.get("outer")),
                                     None)
                        frame = _frame_of(fw.get("plane_frame"))
                        if outer and frame:
                            prof = _defillet_wire(outer.get("entity_ids") or [], ents_by_id,
                                                  fillet_radii_by_axis.get(letter, []))
                            res = _RESERVED[letter]
                            prof2 = _entities_to_reserved(prof, frame, res)
                            if prof2 is None:
                                plan["notes"].append("base profile contains trimmed-ellipse "
                                                     "edges — not re-authorable exactly; base "
                                                     "left unclaimed")
                            else:
                                w = _AXES[letter]
                                t_f = _dot(_f3(f_base, "centroid"), w)
                                t_p = _dot(_f3(f_part, "centroid"), w)
                                t0, t1 = min(t_f, t_p), max(t_f, t_p)
                                # sketch sits at (reserved_normal * offset); extrude runs
                                # along +reserved_normal (empirically verified).
                                s = _dot(res["n"], w)
                                offset = t0 if s > 0 else -t1
                                cen = _f3(f_base, "centroid")
                                plan["kind"] = "prismatic"
                                plan["base"] = {
                                    "type": "extrude", "axis": letter,
                                    "plane": res["plane"], "offset": round(offset, 6),
                                    "depth": round(t1 - t0, 6),
                                    "entities": _reid(prof2),
                                    "region_centroid": [round(_dot(res["u"], cen), 4),
                                                        round(_dot(res["v"], cen), 4)],
                                }
                                claimed.setdefault(f_base.get("sid"), "base")
                                claimed.setdefault(f_part.get("sid"), "base")
                                for f in lateral:
                                    if f.get("surf") == "plane":
                                        claimed.setdefault(f.get("sid"), "base")

    # ---- pass 3: holes — bore-concavity cylinders entered from a planar cap.
    caps = [(f, _unit(_f3(f, "normal"))) for f in planar if _f3(f, "normal")]
    for f in curved:
        if f.get("surf") != "cylinder" or f.get("concavity") != "bore":
            continue
        r = float(f.get("radius") or 0.0)
        area = float(f.get("area_or_len") or 0.0)
        c, ax = _f3(f, "centroid"), _f3(f, "axis")
        if r <= 0 or area <= 0 or c is None or ax is None:
            continue
        a = _unit(ax)
        h = area / (2.0 * math.pi * r)
        ends = [_add(c, _scale(a, h / 2.0)), _sub(c, _scale(a, h / 2.0))]
        openings = []
        for e in ends:
            for q, nq in caps:
                if abs(abs(_dot(nq, a)) - 1.0) > 1e-3:
                    continue
                if abs(_dot(_sub(_f3(q, "centroid"), e), nq)) > 0.2:
                    continue
                if _dot(nq, _sub(e, c)) <= 0:
                    continue  # cap's outward normal must point away from the bore interior
                openings.append((e, q, nq))
                break
        if not openings:
            plan["notes"].append(f"bore {f.get('sid')} (r{r:.3g}) has no planar opening face "
                                 "— left unclaimed")
            continue
        e_entry, q_entry, n_entry = openings[0]
        through = len(openings) >= 2
        hole = {
            "sid": f.get("sid"), "face": f, "diameter": round(2.0 * r, 6),
            "radius": round(r, 6), "center": [round(x, 6) for x in c],
            "axis": [round(x, 6) for x in a], "depth": None if through else round(h, 6),
            "through": through,
            "entry": {"point": [round(x, 6) for x in e_entry],
                      "face_centroid": list(_f3(q_entry, "centroid")),
                      "normal": [round(x, 6) for x in n_entry],
                      "sid": q_entry.get("sid")},
        }
        plan["holes"].append(hole)
        claimed[f.get("sid")] = "hole"
        if not through:
            # claim the flat bottom disc of a blind bore, if present
            bottom = ends[1] if openings and ends[0] == e_entry else ends[0]
            for q, nq in caps:
                if (abs(abs(_dot(nq, a)) - 1.0) < 1e-3 and
                        _norm(_sub(_f3(q, "centroid"), bottom)) < max(0.2, r) and
                        float(q.get("area_or_len") or 0.0) <= 1.2 * math.pi * r * r):
                    claimed.setdefault(q.get("sid"), "hole")

    # ---- pass 4: patterns — same-radius/same-class hole groups.
    groups: dict = defaultdict(list)
    for idx, hl in enumerate(plan["holes"]):
        key = (round(hl["radius"], 3), hl["through"],
               None if hl["depth"] is None else round(hl["depth"], 2),
               _principal_axis(hl["axis"]) or "?")
        groups[key].append(idx)
    for key, idxs in groups.items():
        if len(idxs) < 3:
            continue
        members = [plan["holes"][i]["face"] for i in idxs]
        rot = _axis_order(members)
        if rot and rot.get("order") == len(idxs):
            plan["patterns"].append({"type": "circular", "axis": rot["axis"],
                                     "count": len(idxs), "holes": idxs, "seed": idxs[0]})
            continue
        # linear fallback: collinear + equal spacing along a principal direction
        cs = [_f3(plan["holes"][i]["face"], "centroid") for i in idxs]
        d0 = _sub(cs[1], cs[0])
        letter = _principal_axis(d0, 5e-3)
        if letter is None:
            continue
        w = _AXES[letter]
        order = sorted(range(len(cs)), key=lambda k: _dot(cs[k], w))
        cs_sorted = [cs[k] for k in order]
        gaps = [_sub(b_, a_) for a_, b_ in zip(cs_sorted, cs_sorted[1:])]
        spacing = _dot(gaps[0], w)
        if spacing <= 1e-6:
            continue
        if all(_norm(_perp(g, w)) < 0.05 and abs(_dot(g, w) - spacing) < 0.05 for g in gaps):
            plan["patterns"].append({"type": "linear", "axis": letter, "count": len(idxs),
                                     "spacing": round(spacing, 6), "holes": idxs,
                                     "seed": idxs[order[0]]})

    # ---- always report what nothing claimed (the §3.4 rule: heuristics never suppress the
    # raw face list).
    plan["unclaimed"] = [
        {"sid": f.get("sid"), "surf": f.get("surf"), "side": f.get("side"),
         "concavity": f.get("concavity"), "radius": f.get("radius"),
         "area_mm2": f.get("area_or_len"),
         "centroid": [round(x, 3) for x in (_f3(f, "centroid") or (0, 0, 0))]}
        for f in faces if f.get("sid") not in claimed]
    if plan["unclaimed"]:
        plan["notes"].append(f"{len(plan['unclaimed'])}/{len(faces)} face(s) unclaimed by "
                             "the deterministic passes")
    return plan


# ---------------------------------------------------------------- rebuild helpers

def _feature_ok(r: dict):
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    bad = [{"id": s.get("id"), "error": s.get("error")} for s in fs if not s.get("ok")]
    return (not bad), bad


def _query_faces(doc_id: str) -> list:
    return cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}).get("matches", [])


def _compare(ref_doc: str, new_doc: str) -> dict:
    """eval_compare with the SOURCE as A: `missing` = source material the rebuild lacks,
    `extra` = rebuild material the source never had."""
    return cb.call("eval_compare", {"doc_a": ref_doc, "doc_b": new_doc})


def _calibrate_face_frame(doc_id: str, face_query: dict, origin) -> Optional[dict]:
    """Recover the hole-position frame of a planar face EMPIRICALLY: hole `positions` are
    [u,v] mm in the face's OCCT surface-parameter axes (gp_Pln XAxis/YAxis) about the area
    centroid — axes that are not analytically derivable from the query face card. So: drill a
    tiny 2-position probe hole (checkpointed), locate the two probe bores, derive the u/v unit
    directions, and revert. Deterministic and self-verifying; returns {"o", "xhat", "yhat"}
    or None when the probe can't run (the caller then degrades those holes to candidates)."""
    try:
        fw = cb.call("doc_face_wires", {"doc_id": doc_id, "face": face_query})
        pts = [p for e in fw.get("entities", []) for p in
               (e.get("p1"), e.get("p2"), e.get("center"))
               if isinstance(p, list) and len(p) >= 2]
        if not pts:
            return None
        half = min(max(p[0] for p in pts) - min(p[0] for p in pts),
                   max(p[1] for p in pts) - min(p[1] for p in pts)) / 2.0
        d1, d2 = 0.5 * half, 0.3 * half
        if d2 < 1.0:
            return None
        before = {f.get("sid") for f in _query_faces(doc_id)}
        ck = cb.call("doc_checkpoint", {"doc_id": doc_id}).get("checkpoint")
        probe_r = 0.4
        try:
            r = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{
                "op": "add_feature", "feature": {
                    "type": "hole", "name": "_re_probe",
                    "params": {"style": "simple", "standard": "custom",
                               "diameter": _dim(2 * probe_r), "end": "through_all",
                               "positions": [[d1, 0.0], [0.0, d2]]},
                    "refs": {"face": face_query}}}]})
            ok, _bad = _feature_ok(r)
            if not ok:
                return None
            bores = [f for f in _query_faces(doc_id)
                     if f.get("sid") not in before and f.get("concavity") == "bore"
                     and abs(float(f.get("radius") or 0.0) - probe_r) < 0.05]
            if len(bores) != 2:
                return None
            got = {}
            for b in bores:
                c = _perp(_sub(_f3(b, "centroid"), origin), _unit(_f3(b, "axis")))
                dist = _norm(c)
                which = "x" if abs(dist - d1) < abs(dist - d2) else "y"
                got[which] = _scale(c, 1.0 / dist)
            if "x" not in got or "y" not in got or abs(_dot(got["x"], got["y"])) > 0.05:
                return None
            return {"o": origin, "xhat": got["x"], "yhat": got["y"]}
        finally:
            if ck:
                cb.call("doc_revert", {"doc_id": doc_id, "checkpoint": ck})
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- registration

def register_recon_tools(mcp: FastMCP) -> None:
    """Attach the reverse-engineering tools to the FastMCP server."""

    @mcp.tool()
    def reverse_engineer(source: str, name: Optional[str] = None) -> str:
        """Rebuild an imported solid (STEP/IGES/BREP/STL/...) as an EDITABLE parametric
        feature tree, verified against the source by boolean comparison.

        source : a file path (imported first; meshes are sewn to a solid) OR the doc_id of an
                 already-open document (e.g. from import_part).
        name   : optional name for the NEW rebuilt part (default "<source>-rebuilt").

        Pipeline: import -> deterministic recognition (prismatic/turned base, holes, patterns,
        fillet/chamfer candidates) -> rebuild in batches (base -> holes -> patterns -> dress-
        ups), running eval_compare against the source after EVERY batch; a batch that fails to
        build or makes the match WORSE is reverted and listed under `rejected`. Dress-ups are
        applied only when a plain-word edge query resolves on the rebuilt body; otherwise they
        stay in `candidates` for you to try by hand.

        Returns {ok, doc_id, ref_doc_id, match_pct, features, rejected, candidates,
        unexplained:{missing_mm3, extra_mm3, regions}, next_steps, notes, recognition}.
        match_pct is common_volume / max(volume) between the two solids (100 = identical);
        `unexplained` localizes the residual (largest missing/extra regions with centers).
        The comparison is WORLD-FRAME (no alignment/ICP) — the rebuild is authored in the
        source's own coordinates so this is exact, but comparing to a moved copy will not
        match. Mesh input (STL/OBJ/...) is faceted: expect a low deterministic yield — the
        result honestly reports the source faces the passes could not claim.
        """
        try:
            notes: list = []
            src = source.strip()
            if os.path.isfile(src) or (os.path.sep in src and
                                       os.path.isfile(os.path.abspath(src))):
                path = os.path.abspath(src)
                ext = os.path.splitext(path)[1].lower()
                is_mesh = ext in (".stl", ".obj", ".ply", ".3mf", ".amf", ".off")
                imp = cb.call("import_file", {"path": path, "as_solid": bool(is_mesh)})
                ref_doc = imp.get("doc_id")
                if not ref_doc:
                    return _dumps({"ok": False, "error": f"import of {path!r} produced no "
                                   "document"})
                if is_mesh:
                    notes.append("facet geometry: expect low deterministic yield (mesh "
                                 "facets rarely satisfy the exact-surface passes)")
            else:
                ref_doc = _doc(src)

            ref_faces = _query_faces(ref_doc)
            if not ref_faces:
                return _dumps({"ok": False, "error": f"document {ref_doc!r} has no solid "
                               "bodies — nothing to reverse-engineer"})

            plan = _recognize(ref_doc)
            notes.extend(plan["notes"])

            try:
                ref_name = (cb.call("doc_tree", {"doc_id": ref_doc}).get("tree")
                            or {}).get("name") or "import"
            except Exception:  # noqa: BLE001
                ref_name = "import"
            new_doc = cb.call("doc_new", {"kind": "part",
                                          "name": name or f"{ref_name}-rebuilt"})["doc_id"]

            state = {"match": 0.0, "degraded": False}
            rejected: list = []
            candidates: list = []
            applied: list = []

            def apply_batch(label: str, ops: list) -> Optional[dict]:
                """Checkpoint -> apply -> verify (eval_compare) -> keep or revert."""
                try:
                    ck = cb.call("doc_checkpoint", {"doc_id": new_doc}).get("checkpoint")
                except Exception:  # noqa: BLE001
                    ck = None

                def revert() -> None:
                    if ck:
                        try:
                            cb.call("doc_revert", {"doc_id": new_doc, "checkpoint": ck})
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    r = cb.call("doc_apply_ops", {"doc_id": new_doc, "ops": ops})
                except Exception as e:  # noqa: BLE001
                    revert()
                    rejected.append({"batch": label, "reason": str(e)})
                    return None
                ok, bad = _feature_ok(r)
                if not ok:
                    revert()
                    rejected.append({"batch": label, "reason": "feature build failed",
                                     "failed_features": bad})
                    return None
                try:
                    cmp_res = _compare(ref_doc, new_doc)
                except Exception as e:  # noqa: BLE001
                    revert()
                    rejected.append({"batch": label,
                                     "reason": f"verification compare failed: {e}"})
                    return None
                m = cmp_res.get("match_pct")
                if m is None:
                    state["degraded"] = True
                    notes.append(f"batch {label!r}: compare degraded to volume/bbox "
                                 "agreement — kept without a boolean verdict")
                    return r
                if m + 1e-9 < state["match"] - 0.01:
                    revert()
                    rejected.append({"batch": label, "reason": "match regressed",
                                     "match_before": round(state["match"], 3),
                                     "match_after": round(m, 3)})
                    return None
                state["match"] = m
                return r

            # ---------------- batch 1: the base (extrude or revolve)
            base = plan.get("base")
            if base and base["type"] == "extrude":
                ops = [
                    {"op": "add_feature", "feature": {
                        "type": "sketch", "name": "BaseProfile",
                        "params": {"entities": base["entities"], "constraints": [],
                                   "offset": _dim(base["offset"])},
                        "refs": {"plane": base["plane"]}}},
                    {"op": "add_feature", "feature": {
                        "type": "extrude", "name": "Base",
                        "params": {"mode": "boss", "end": "blind",
                                   "depth": _dim(base["depth"])},
                        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                              "centroid": base["region_centroid"],
                                              "hint_index": 0}]}}},
                ]
                r = apply_batch("base extrude", ops)
                if r:
                    applied.append({"id": (r.get("id_map") or {}).get("$1"),
                                    "type": "extrude", "name": "Base", "role": "base",
                                    "depth_mm": base["depth"]})
            elif base and base["type"] == "revolve":
                letter = base["axis"]
                segs = base["segments"]
                t_lo, t_hi = segs[0]["t0"], segs[-1]["t1"]
                pts = [(0.0, t_lo)]
                for s in segs:
                    pts.append((s["r"], s["t0"]))
                    pts.append((s["r"], s["t1"]))
                pts.append((0.0, t_hi))
                # (radius, axis-position) -> sketch (u, v) on the reserved plane CONTAINING
                # the axis: z -> plane.front (u=r along +X, v=t along +Z); x -> plane.front
                # (u=t, v=r); y -> plane.top (u=r along +X, v=t along +Y).
                if letter == "z":
                    plane, uv = "plane.front", [(r_, t_) for r_, t_ in pts]
                elif letter == "x":
                    plane, uv = "plane.front", [(t_, r_) for r_, t_ in pts]
                else:
                    plane, uv = "plane.top", [(r_, t_) for r_, t_ in pts]
                ents = _reid([{"id": "e0", "type": "line", "construction": False,
                               "p1": [round(a[0], 6), round(a[1], 6)],
                               "p2": [round(b[0], 6), round(b[1], 6)]}
                              for a, b in zip(uv, uv[1:] + uv[:1])
                              if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9])
                r_min = min(s["r"] for s in segs)
                mid = 0.5 * (t_lo + t_hi)
                cen = ((r_min / 2.0, mid) if letter == "z" else
                       (mid, r_min / 2.0) if letter == "x" else (r_min / 2.0, mid))
                ops = [
                    {"op": "add_feature", "feature": {
                        "type": "sketch", "name": "TurnProfile",
                        "params": {"entities": ents, "constraints": []},
                        "refs": {"plane": plane}}},
                    {"op": "add_feature", "feature": {
                        "type": "revolve", "name": "Turned",
                        "params": {"mode": "boss", "angle": _dim(360)},
                        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                              "centroid": [round(cen[0], 4),
                                                           round(cen[1], 4)],
                                              "hint_index": 0}],
                                 "axis": {"kind": "datum",
                                          "feature": f"axis.{letter}"}}}},
                ]
                r = apply_batch("base revolve", ops)
                if r:
                    applied.append({"id": (r.get("id_map") or {}).get("$1"),
                                    "type": "revolve", "name": "Turned", "role": "base",
                                    "axis": letter})
            else:
                notes.append("no deterministic base recognized — nothing was rebuilt; see "
                             "`recognition` for the raw face evidence")

            # ---------------- batch 2: holes (pattern seeds + singles)
            hole_ids: dict = {}
            if plan["holes"] and applied:
                pattern_holes = {i for p in plan["patterns"] for i in p["holes"]}
                seeds = {p["seed"] for p in plan["patterns"]}
                to_drill = [i for i in range(len(plan["holes"]))
                            if i not in pattern_holes or i in seeds]

                new_faces = _query_faces(new_doc)
                frames: dict = {}     # entry key -> calibration/None
                entry_face: dict = {}  # entry key -> (query, rebuilt centroid)
                specs: list = []      # (hole idx, entry key, positions)
                skipped: list = []
                for i in to_drill:
                    hl = plan["holes"][i]
                    a = tuple(hl["axis"])
                    e_pt = tuple(hl["entry"]["point"])
                    key = (round(_dot(e_pt, a), 3),
                           tuple(round(x, 3) for x in hl["entry"]["normal"]))
                    if key not in entry_face:
                        n_entry = tuple(hl["entry"]["normal"])
                        cands = []
                        for g in new_faces:
                            if g.get("surf") != "plane":
                                continue
                            ng, cg = _f3(g, "normal"), _f3(g, "centroid")
                            if ng is None or cg is None or _dot(_unit(ng), n_entry) < 0.999:
                                continue
                            if abs(_dot(_sub(cg, e_pt), n_entry)) > 0.2:
                                continue
                            cands.append(g)
                        if not cands:
                            entry_face[key] = None
                        else:
                            g = max(cands, key=lambda f: float(f.get("area_or_len") or 0.0))
                            q = {"of": "face", "surf": "flat",
                                 "near": [round(x, 4) for x in _f3(g, "centroid")],
                                 "count": 1}
                            entry_face[key] = (q, _f3(g, "centroid"))
                    if entry_face[key] is None:
                        skipped.append((i, "entry face not found on the rebuilt body"))
                        continue
                    q, o = entry_face[key]
                    off = _perp(_sub(tuple(hl["center"]), o), _unit(a))
                    if _norm(off) < 0.05:
                        positions = [[0.0, 0.0]]
                    else:
                        if key not in frames:
                            frames[key] = _calibrate_face_frame(new_doc, q, o)
                        fr = frames[key]
                        if fr is None:
                            skipped.append((i, "hole-position frame calibration failed"))
                            continue
                        positions = [[round(_dot(off, fr["xhat"]), 4),
                                      round(_dot(off, fr["yhat"]), 4)]]
                    specs.append((i, key, positions[0]))
                # ONE hole feature per (entry face, diameter, end/depth) with all its
                # positions: the engine measures positions from the face's LIVE area centroid
                # at compile time, so drilling siblings as separate features would shift the
                # centroid under later ones. Pattern SEEDS stay solo features so the pattern
                # repeats exactly one bore.
                drill_ops: list = []
                op_hole_idxs: list = []  # per op: the hole indices it drills
                grouped: dict = {}
                for i, key, pos in specs:
                    hl = plan["holes"][i]
                    gkey = ("seed", i) if i in seeds else (
                        key, round(hl["diameter"], 6), hl["through"],
                        None if hl["depth"] is None else round(hl["depth"], 4))
                    if gkey in grouped:
                        k = grouped[gkey]
                        drill_ops[k]["feature"]["params"]["positions"].append(pos)
                        op_hole_idxs[k].append(i)
                        continue
                    params = {"style": "simple", "standard": "custom",
                              "diameter": _dim(hl["diameter"]),
                              "end": "through_all" if hl["through"] else "blind",
                              "positions": [pos]}
                    if not hl["through"]:
                        params["depth"] = _dim(hl["depth"])
                    grouped[gkey] = len(drill_ops)
                    drill_ops.append({"op": "add_feature", "feature": {
                        "type": "hole", "name": f"Hole{len(drill_ops) + 1}",
                        "params": params, "refs": {"face": entry_face[key][0]}}})
                    op_hole_idxs.append([i])
                for i, why in skipped:
                    hl = plan["holes"][i]
                    candidates.append({"kind": "hole", "diameter": hl["diameter"],
                                       "center": hl["center"], "axis": hl["axis"],
                                       "through": hl["through"], "depth": hl["depth"],
                                       "sids": [hl["sid"]], "note": why})
                if drill_ops:
                    r = apply_batch("holes", drill_ops)
                    if r:
                        id_map = r.get("id_map") or {}
                        for k, idxs in enumerate(op_hole_idxs):
                            fid = id_map.get(f"${k}")
                            hl = plan["holes"][idxs[0]]
                            for i in idxs:
                                hole_ids[i] = fid
                            applied.append({"id": fid, "type": "hole",
                                            "name": f"Hole{k + 1}", "role": "hole",
                                            "diameter_mm": hl["diameter"],
                                            "through": hl["through"],
                                            "positions": len(idxs)})
                    else:
                        for idxs in op_hole_idxs:
                            for i in idxs:
                                hl = plan["holes"][i]
                                candidates.append({"kind": "hole",
                                                   "diameter": hl["diameter"],
                                                   "center": hl["center"],
                                                   "sids": [hl["sid"]],
                                                   "note": "holes batch was rejected"})

            # ---------------- batch 3: patterns (each references its drilled seed)
            for p in plan["patterns"]:
                seed_fid = hole_ids.get(p["seed"])
                if not seed_fid:
                    candidates.append({"kind": f"{p['type']}_pattern", "count": p["count"],
                                       "note": "seed hole was not applied"})
                    continue
                if p["type"] == "circular":
                    op = {"op": "add_feature", "feature": {
                        "type": "circular_pattern", "name": "HoleRing",
                        "params": {"count": p["count"], "angle": _dim(360),
                                   "equal_spacing": True},
                        "refs": {"axis": {"kind": "datum", "feature": f"axis.{p['axis']}"},
                                 "features": [seed_fid]}}}
                else:
                    op = {"op": "add_feature", "feature": {
                        "type": "linear_pattern", "name": "HoleRow",
                        "params": {"count1": p["count"], "spacing1": _dim(p["spacing"])},
                        "refs": {"dir1": {"kind": "datum", "feature": f"axis.{p['axis']}"},
                                 "features": [seed_fid]}}}
                r = apply_batch(f"{p['type']} pattern", [op])
                if r:
                    applied.append({"id": (r.get("id_map") or {}).get("$0"),
                                    "type": op["feature"]["type"], "name":
                                    op["feature"]["name"], "role": "pattern",
                                    "count": p["count"], "seed": seed_fid})
                else:
                    candidates.append({"kind": f"{p['type']}_pattern", "count": p["count"],
                                       "seed": seed_fid,
                                       "note": "pattern batch was rejected"})

            # ---------------- batch 4: dress-ups (fillets, then chamfers) — applied only
            # when a plain-word edge query RESOLVES on the rebuilt body (the spec's gate).
            for group, feat_type, param_key in (("fillet_candidates", "fillet", "radius"),
                                                ("chamfer_candidates", "chamfer", "length")):
                for cand in plan[group]:
                    val = cand.get(param_key)
                    near = cand.get("near")
                    if not applied or val is None or near is None:
                        candidates.append({**{k: v for k, v in cand.items() if k != "axis"},
                                           "note": "no rebuilt body / underdetermined "
                                                   "parameters — not applied"})
                        continue
                    edge_q = {"of": "edge", "near": [round(x, 4) for x in near], "count": 1}
                    resolved = None
                    try:
                        em = cb.call("doc_query", {"doc_id": new_doc,
                                                   "query": edge_q}).get("matches", [])
                        resolved = em[0] if em else None
                    except Exception:  # noqa: BLE001
                        resolved = None
                    if resolved and feat_type == "fillet" and cand.get("axis") and \
                            resolved.get("surf") == "line":
                        eax = _f3(resolved, "axis")
                        if eax is not None and \
                                abs(abs(_dot(_unit(eax), _unit(cand["axis"]))) - 1.0) > 1e-2:
                            resolved = None  # resolved a stray edge, not the sharp corner
                    if not resolved:
                        candidates.append({**{k: v for k, v in cand.items() if k != "axis"},
                                           "note": "edge query did not resolve on the "
                                                   "rebuilt body — try applying by hand"})
                        continue
                    op = {"op": "add_feature", "feature": {
                        "type": feat_type, "name": f"{feat_type.capitalize()}"
                                                   f"{len(applied) + 1}",
                        "params": {param_key: _dim(val)}, "refs": {"edges": edge_q}}}
                    r = apply_batch(f"{feat_type} r{val:g}", [op])
                    if r:
                        applied.append({"id": (r.get("id_map") or {}).get("$0"),
                                        "type": feat_type,
                                        "name": op["feature"]["name"], "role": "dressup",
                                        param_key: val, "edge": resolved.get("sid")})
                    else:
                        candidates.append({**{k: v for k, v in cand.items()
                                              if k != "axis"},
                                           "note": "applied but the match regressed — "
                                                   "reverted (see rejected)"})

            # ---------------- final verification + report
            if applied:
                try:
                    final = _compare(ref_doc, new_doc)
                except Exception as e:  # noqa: BLE001
                    final = {"match_pct": None, "summary": f"final compare failed: {e}"}
            else:
                # Nothing was rebuilt (e.g. faceted mesh input): an honest 0, not a
                # confusing compare-against-an-empty-doc error.
                final = {"match_pct": 0.0,
                         "summary": "no features rebuilt — the deterministic passes claimed "
                                    "nothing; see recognition.unclaimed"}
            match_pct = final.get("match_pct")
            unexplained = {"missing_mm3": final.get("missing_mm3"),
                           "extra_mm3": final.get("extra_mm3"),
                           "regions": final.get("residual_regions", [])}

            steps = []
            if match_pct is not None and match_pct < 99.5:
                steps.append("inspect `unexplained.regions` (world-space centers of the "
                             "residual volumes) and model the missing features by hand")
            if candidates:
                steps.append("try the `candidates` (fillets/chamfers/holes the recognizer "
                             "saw but could not safely apply) with add_features, re-checking "
                             "with compare_parts after each")
            if plan["unclaimed"]:
                steps.append("hover(sid) the `recognition.unclaimed` faces on the SOURCE doc "
                             "to understand what geometry the passes could not explain")
            steps.append("edit any recovered feature's params with add_features "
                         "(edit_feature) — the tree recompiles parametrically; verify edits "
                         "with compare_parts(ref, rebuilt)")

            _CUR["doc_id"] = new_doc
            try:
                _publish_current(new_doc)
            except Exception:  # noqa: BLE001
                pass

            return _dumps({
                "ok": True,
                "doc_id": new_doc,
                "ref_doc_id": ref_doc,
                "match_pct": round(match_pct, 3) if match_pct is not None else None,
                "summary": final.get("summary"),
                "features": applied,
                "rejected": rejected,
                "candidates": candidates,
                "unexplained": unexplained,
                "next_steps": steps,
                "notes": notes,
                "recognition": {"kind": plan["kind"], "face_count": plan["face_count"],
                                "bbox_mm": plan["bbox_mm"],
                                "holes": len(plan["holes"]),
                                "patterns": plan["patterns"],
                                "unclaimed": plan["unclaimed"]},
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def compare_parts(doc_a: str, doc_b: str, body_a: Optional[str] = None,
                      body_b: Optional[str] = None) -> str:
        """Boolean-compare two documents' solids: how much volume they share and where they
        differ. The ground-truth check behind reverse_engineer — use it after ANY manual
        rebuild/edit to prove the geometry still matches a reference.

        doc_a / doc_b : the two document ids (doc_a is treated as the REFERENCE: `missing` =
                        material in A that B lacks, `extra` = material in B that A never had).
        body_a/body_b : optional body/feature name to pick ONE body out of a multi-body doc
                        (default: the fused union of all bodies).

        Returns {ok, match_pct, volume_a_mm3, volume_b_mm3, common_mm3, missing_mm3,
        extra_mm3, residual_regions:[{side, volume_mm3, center, bbox}], summary}. match_pct =
        common / max(volumes). WORLD-FRAME comparison — no alignment/ICP is attempted, so the
        parts must already sit in the same coordinates; a perfect copy that has been moved
        reports a LOW match, which is correct for this tool's purpose. If the boolean fails
        (sick imported geometry) the result degrades to volume/bbox agreement with
        degraded:true and match_pct:null rather than guessing.
        """
        try:
            params: dict = {"doc_a": _doc(doc_a), "doc_b": _doc(doc_b)}
            if body_a:
                params["body_a"] = body_a
            if body_b:
                params["body_b"] = body_b
            res = cb.call("eval_compare", params)
            return _dumps({"ok": res.get("ok", True), **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def convert_face_to_sketch(face: Union[str, dict], doc_id: Optional[str] = None,
                               sketch_name: Optional[str] = None) -> str:
        """"Convert entities": trace a planar face's boundary wires into a NEW editable sketch
        ON that face — the manual reverse-engineering primitive (grab an imported body's face
        outline, then extrude/cut/revolve it yourself).

        face : the face to trace — a stable sid from describe_part/query_geometry (e.g.
               "Fdb31d2"), a side word ("top"), or a full plain-word query dict
               ({"of":"face","side":"top","pick":"largest"}). Must be PLANAR.
        doc_id : the document (default: current part).
        sketch_name : name for the new sketch feature (default "FaceSketch").

        The sketch is created on the face's own plane (refs.plane = the face selector) with
        one entity per boundary edge — lines/arcs/circles/ellipses exactly, anything else
        (splines) as a sampled polyline with `approx: true`. Outer and inner (hole) loops are
        all included. Returns {ok, doc_id, sketch, entity_count, wires, approx}.
        """
        try:
            d = _doc(doc_id)
            if isinstance(face, str):
                fs = face.strip()
                matches = _query_faces(d)
                rec = next((m for m in matches if m.get("sid") == fs), None)
                if rec is None:
                    q = {"of": "face", "side": fs}
                    got = cb.call("doc_query", {"doc_id": d, "query": q}).get("matches", [])
                    if not got:
                        return _dumps({"ok": False, "error": f"no face with sid or side "
                                       f"{fs!r} (call describe_part for current sids)"})
                    rec = got[0]
                wires_ref: dict = {"sid": rec.get("sid")}
            else:
                got = cb.call("doc_query", {"doc_id": d, "query": face}).get("matches", [])
                if not got:
                    return _dumps({"ok": False, "error": "face query matched nothing"})
                rec = got[0]
                wires_ref = {"sid": rec.get("sid")}
            fw = cb.call("doc_face_wires", {"doc_id": d, "face": wires_ref})
            ents = fw.get("entities", [])
            if not ents:
                return _dumps({"ok": False, "error": "face has no boundary wires"})
            op = {"op": "add_feature", "feature": {
                "type": "sketch", "name": sketch_name or "FaceSketch",
                "params": {"entities": ents, "constraints": []},
                "refs": {"plane": rec}}}
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": [op]})
            ok, bad = _feature_ok(r)
            if not ok:
                return _dumps({"ok": False, "error": "sketch feature failed to build",
                               "failed_features": bad})
            sk = (r.get("id_map") or {}).get("$0")
            return _dumps({"ok": True, "doc_id": d, "sketch": sk,
                           "entity_count": len(ents),
                           "wires": [{"outer": w.get("outer"),
                                      "entities": len(w.get("entity_ids") or [])}
                                     for w in fw.get("wires", [])],
                           "approx": bool(fw.get("approx")),
                           "note": "entities are 2D in the face's plane frame; extrude/"
                                   "revolve the sketch's regions to build on this face"})
        except Exception as e:  # noqa: BLE001
            return _err(e)
