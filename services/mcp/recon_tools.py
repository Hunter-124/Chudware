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

# ---------------------------------------------------------------------------
# T-F13: hypothesis-ranker hook (flag-gated, default OFF)
#
# Set env var AICAD_RECON_RANKER=1 to enable.  The ranker re-orders the
# `candidates` list only — it never generates new candidates.  Falls back to
# deterministic order whenever weights are missing or the import fails.
# ---------------------------------------------------------------------------

import os as _os

_RANKER_ENABLED: bool = _os.environ.get("AICAD_RECON_RANKER", "0").strip() == "1"
_RANKER_WEIGHTS: dict = {}  # loaded lazily on first use


def _load_ranker_weights() -> dict:
    """Lazily load ranker weights from the canonical JSON path."""
    global _RANKER_WEIGHTS
    if _RANKER_WEIGHTS:
        return _RANKER_WEIGHTS
    import json as _json
    from pathlib import Path as _Path
    candidates = [
        _Path(__file__).resolve().parent.parent.parent
        / "tools" / "gsl_gnn" / "ranker_weights.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                _RANKER_WEIGHTS = _json.loads(p.read_text())
                return _RANKER_WEIGHTS
            except Exception:  # noqa: BLE001
                pass
    return {}


def _reorder_candidates(candidates: list, ref_faces: list, diag: float) -> list:
    """Re-order `candidates` by ranker score (desc).  Deterministic fallback on
    any failure or when flag is off."""
    if not _RANKER_ENABLED or not candidates:
        return candidates
    try:
        weights = _load_ranker_weights()
        if not weights:
            return candidates
        from tools.gsl_gnn.ranker import rank_candidates as _rank  # noqa: PLC0415
        faces_by_sid = {f.get("sid"): f for f in ref_faces if f.get("sid")}
        return _rank(candidates, faces_by_sid, diag, weights)
    except Exception:  # noqa: BLE001
        return candidates  # silent fallback — deterministic order preserved


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


# ------------------------------------------------------- RE-14: fitted-primitive hints

# A hinted primitive is only worth trying when the deterministic passes did NOT already
# recover the geometry it describes. Cylinder fits are the useful case: a large convex
# boss (r > pass-5's fillet bound, not a bore so not a hole) falls through every frozen
# pass and lands in `unclaimed` — exactly the gap a RANSAC cylinder fit fills.
_HINT_MIN_CONFIDENCE = 0.5


def _cylinder_boss_specs(hints: Optional[dict],
                         claimed_cyl_axes: list) -> list:
    """Pure-Python: turn an ``aicad.reveng_fits/1`` doc (contracts/reveng.md §3) into a
    list of candidate cylinder-BOSS specs, dropping fits the deterministic passes already
    explain. Engine-free and side-effect-free so it is unit-testable without libaicad.

    hints             : the fits doc, or None (returns []).
    claimed_cyl_axes  : (axis_unit, origin) pairs the passes already own (base/holes) —
                        a fit coaxial+coincident with one of these is a duplicate, skipped.

    Returns [{radius, height, axis, origin, confidence, face_ids}] — additive candidates,
    NOT yet verified. reverse_engineer routes each through apply_batch (keep-or-revert), so
    a bad hint can only revert, never regress the match. hints=None -> [] -> no behavior
    change (this is what keeps the frozen recognizer and every existing test untouched)."""
    if not hints:
        return []
    fits = hints.get("fits") if isinstance(hints, dict) else None
    if not isinstance(fits, list):
        return []
    specs: list = []
    for fit in fits:
        if not isinstance(fit, dict) or fit.get("kind") != "cylinder":
            continue
        if float(fit.get("confidence") or 0.0) < _HINT_MIN_CONFIDENCE:
            continue
        p = fit.get("params_mm") or {}
        r = float(p.get("radius") or 0.0)
        h = float(p.get("height") or 0.0)
        ax = _f3(p, "axis")
        origin = _f3(p, "origin")
        if r <= 0.0 or h <= 0.0 or ax is None or origin is None:
            continue
        a = _unit(ax)
        if _norm(a) < 0.5:
            continue
        # skip a fit the passes already recovered: coaxial (parallel axes) AND the fit's
        # origin sits on that axis line (perpendicular offset ~0).
        dup = False
        for cax, corg in claimed_cyl_axes:
            cau = _unit(cax)
            if abs(abs(_dot(a, cau)) - 1.0) > 1e-2:
                continue
            if _norm(_perp(_sub(origin, corg), cau)) < max(0.5, 0.1 * r):
                dup = True
                break
        if dup:
            continue
        specs.append({"radius": r, "height": h, "axis": a, "origin": list(origin),
                      "confidence": float(fit.get("confidence") or 0.0),
                      "face_ids": fit.get("face_ids") or []})
    return specs


# ---------------------------------------------------------------- registration

def register_recon_tools(mcp: FastMCP) -> None:
    """Attach the reverse-engineering tools to the FastMCP server."""

    @mcp.tool()
    def reverse_engineer(source: str, name: Optional[str] = None,
                         hints: Optional[dict] = None) -> str:
        """Rebuild an imported solid (STEP/IGES/BREP/STL/...) as an EDITABLE parametric
        feature tree, verified against the source by boolean comparison.

        source : a file path (imported first; meshes are sewn to a solid) OR the doc_id of an
                 already-open document (e.g. from import_part).
        name   : optional name for the NEW rebuilt part (default "<source>-rebuilt").
        hints  : OPTIONAL ``aicad.reveng_fits/1`` doc (contracts/reveng.md §3) from
                 reveng_fit_primitives — RANSAC/segmentation primitive fits. When present,
                 cylinder fits the deterministic passes could NOT recover (a convex boss that
                 fell through into `recognition.unclaimed`) are tried as ADDITIVE boss batches
                 AFTER the deterministic rebuild, each verified by the same eval_compare
                 keep-or-revert gate — so a hint can only improve or be reverted, never
                 regress the match. Omitting hints leaves the pipeline byte-for-byte unchanged.

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

            # ---------------- batch 5 (RE-14): fitted-primitive HINTS — additive only.
            # Cylinder fits the frozen passes could not recover (convex bosses that fell into
            # `unclaimed`) are tried as boss extrudes AFTER the deterministic rebuild. Each
            # goes through apply_batch (checkpoint -> eval_compare -> keep/revert), so this can
            # only raise or leave match_pct — never lower it. hints=None -> no specs -> no-op.
            claimed_cyl_axes: list = []
            for hl in plan["holes"]:
                a_h, o_h = _f3(hl, "axis"), _f3(hl, "center")
                if a_h is not None and o_h is not None:
                    claimed_cyl_axes.append((a_h, o_h))
            base = plan.get("base")
            if base and base.get("type") == "revolve":
                w = _AXES.get(base.get("axis"))
                if w is not None:
                    claimed_cyl_axes.append((list(w), [0.0, 0.0, 0.0]))
            hint_specs = _cylinder_boss_specs(hints, claimed_cyl_axes)
            if hint_specs:
                notes.append(f"{len(hint_specs)} fitted cylinder hint(s) not already "
                             "recovered — trying as additive boss batches")
            for si, spec in enumerate(hint_specs):
                letter = _principal_axis(spec["axis"])
                # The `cylinder` primitive is Z-aligned and centered, placed by an absolute
                # `at:[x,y,z]` (core/doc/document.cpp) — so only a Z-axis fit is authorable as
                # a boss primitive today. Non-Z fits are surfaced as candidates, not applied.
                if letter != "z":
                    candidates.append({"kind": "cylinder_hint", "radius": spec["radius"],
                                       "height": spec["height"], "axis": spec["axis"],
                                       "note": "fit axis is not +Z — the boss primitive is "
                                               "Z-aligned; author this cylinder by hand"})
                    continue
                w = _AXES[letter]
                o = spec["origin"]
                # Centered cylinder: `at` is the mid-point = fit origin + axis*(height/2).
                mid = [o[i] + w[i] * spec["height"] / 2.0 for i in range(3)]
                ops = [{"op": "add_feature", "feature": {
                    "type": "cylinder", "name": f"HintBoss{si + 1}",
                    "params": {"radius": _dim(spec["radius"]),
                               "height": _dim(spec["height"]), "mode": "boss",
                               "at": [round(x, 6) for x in mid]}}}]
                r = apply_batch(f"hint boss r{spec['radius']:g}", ops)
                if r:
                    applied.append({"id": (r.get("id_map") or {}).get("$0"),
                                    "type": "cylinder", "name": f"HintBoss{si + 1}",
                                    "role": "hint", "radius_mm": spec["radius"],
                                    "height_mm": spec["height"],
                                    "confidence": spec["confidence"]})
                else:
                    candidates.append({"kind": "cylinder_hint", "radius": spec["radius"],
                                       "height": spec["height"], "axis": spec["axis"],
                                       "confidence": spec["confidence"],
                                       "note": "hint boss did not build or regressed the "
                                               "match — reverted (see rejected)"})

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

            # T-F13: re-order candidates by ranker score (flag-gated, default OFF)
            _bbox = plan.get("bbox_mm") or [1.0, 1.0, 1.0]
            _diag = math.sqrt(sum(float(s) ** 2 for s in _bbox)) or 1.0
            candidates = _reorder_candidates(candidates, ref_faces, _diag)

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
                               sketch_name: Optional[str] = None,
                               associative: bool = True) -> str:
        """"Convert entities": trace a planar face's boundary wires into a NEW editable sketch
        ON that face — the manual reverse-engineering primitive (grab an imported body's face
        outline, then extrude/cut/revolve it yourself).

        face : the face to trace — a stable sid from describe_part/query_geometry (e.g.
               "Fdb31d2"), a side word ("top"), or a full plain-word query dict
               ({"of":"face","side":"top","pick":"largest"}). Must be PLANAR.
        doc_id : the document (default: current part).
        sketch_name : name for the new sketch feature (default "FaceSketch").
        associative : True (default) stores a persistent external reference on the sketch
               (params.external_refs) — the traced entities re-project on every rebuild when
               the source face moves/changes, keeping stable entity ids so constraints
               attached to them survive; if the face stops resolving the ref goes stale
               (last-good geometry kept, rebuild still succeeds, warning surfaced). False =
               legacy one-shot copy (entities baked, never updated).

        The sketch is created on the face's own plane (refs.plane = the face selector) with
        one entity per boundary edge — lines/arcs/circles/ellipses exactly, anything else
        (splines) as a sampled polyline with `approx: true`. Outer and inner (hole) loops are
        all included. Returns {ok, doc_id, sketch, entity_count, wires, approx, associative,
        external_refs:[{id, stale, entity_ids}]}.
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
            if associative:
                # Empty sketch ON the face, then an associative Convert Entities of the
                # SAME face into it — face_wires and the projector share the face-centroid
                # frame convention, so the traced loop lands identically, but now with a
                # persistent external_ref that re-projects on every rebuild.
                ops = [
                    {"op": "add_feature", "feature": {
                        "type": "sketch", "name": sketch_name or "FaceSketch",
                        "params": {"entities": [], "constraints": []},
                        "refs": {"plane": rec}}},
                    {"op": "sketch_convert", "sketch": "$0",
                     "refs": [{"kind": "face", "selector": rec}],
                     "construction": False, "associative": True},
                ]
            else:
                ops = [{"op": "add_feature", "feature": {
                    "type": "sketch", "name": sketch_name or "FaceSketch",
                    "params": {"entities": ents, "constraints": []},
                    "refs": {"plane": rec}}}]
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            ok, bad = _feature_ok(r)
            if not ok:
                return _dumps({"ok": False, "error": "sketch feature failed to build",
                               "failed_features": bad})
            sk = (r.get("id_map") or {}).get("$0")
            ext_refs = _external_refs_of(r, sk, []) if associative else []
            return _dumps({"ok": True, "doc_id": d, "sketch": sk,
                           "entity_count": len(ents),
                           "wires": [{"outer": w.get("outer"),
                                      "entities": len(w.get("entity_ids") or [])}
                                     for w in fw.get("wires", [])],
                           "approx": bool(fw.get("approx")),
                           "associative": associative, "external_refs": ext_refs,
                           "note": "entities are 2D in the face's plane frame; extrude/"
                                   "revolve the sketch's regions to build on this face"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    def _external_refs_of(resp: dict, sketch_fid: str, new_entity_ids: list) -> list:
        """Pull the external_refs rows created by THIS call out of a doc_apply_ops
        response tree: rows on `sketch_fid` whose entity_ids intersect the ids we just
        inserted (a sketch may already carry earlier refs)."""
        new_set = set(new_entity_ids or [])
        out: list = []
        for feat in ((resp.get("tree") or {}).get("features") or []):
            if not isinstance(feat, dict) or feat.get("id") != sketch_fid:
                continue
            xrs = ((feat.get("params") or {}).get("external_refs") or [])
            for xr in xrs:
                if not isinstance(xr, dict):
                    continue
                eids = xr.get("entity_ids") or []
                if not new_set or new_set.intersection(eids):
                    out.append({"id": xr.get("id"), "stale": bool(xr.get("stale")),
                                "entity_ids": eids})
        return out

    @mcp.tool()
    def project_into_sketch(sketch: str, refs: Union[list, dict],
                            doc_id: Optional[str] = None,
                            construction: bool = False,
                            associative: bool = True) -> str:
        """"Convert entities" INTO an existing sketch — SolidWorks-style edge/face projection that
        appends real (or construction) geometry to a sketch you are already working in.

        DISTINCT FROM convert_face_to_sketch, which creates an entirely NEW sketch on one planar
        face's own plane.  Use THIS tool when you have an open sketch and want to trace edges or
        faces from the surrounding solid into it — the way SolidWorks "Convert Entities" works.

        Behaviour:
          * Each projected entity is appended as a real sketch entity (lines/arcs/circles/
            ellipses; oblique circular edges become ellipses; near-perpendicular edges collapse to
            a point; unclassifiable curves fall back to a 24-seg polyline with approx:true).
          * Every new entity is immediately pinned with a whole-entity `fix` relation so it
            cannot drift during sketch solving; you can still constrain/dimension OTHER
            entities TO the converted geometry.
          * ASSOCIATIVE by default (associative=True): each ref is stored on the sketch as a
            persistent external reference (params.external_refs, id "x<N>"). On every rebuild
            the engine re-resolves the source selector and re-projects, updating the converted
            entities IN PLACE (stable entity ids — constraints attached to them survive). If
            the source stops resolving (owner feature deleted / geometry moved too far), the
            ref is marked stale: the last-good geometry is kept, the rebuild still succeeds,
            and the sketch's feature status / sketch_health carries a stale warning.
          * Pass associative=False for a legacy one-shot copy (no link stored; converted
            geometry never updates).

        construction : if True the projected entities are flagged construction (blue in the app);
                       they define references but are excluded from extruded regions.

        refs grammar (a single ref or a list of refs):
          Datum plane:  "front" | "top" | "right"
                        OR  {"datum": "front"}   — the plane's intersection with the sketch plane
                        becomes a line.
          Face:         "F<sid>"  (a face sid from describe_part, e.g. "Fdb31d2")
                        OR  a side word ("top"/"bottom"/"left"/"right"/"front"/"back") — resolved
                        via doc_query to the matching face sid
                        OR  {"kind": "face", "selector": {"sid": "Fdb31d2"}}
                        A face ref projects ALL its boundary wires (outer + hole loops).
          Edge:         "E<sid>"  (an edge sid from describe_part, e.g. "E4a7c9")
                        OR  {"kind": "edge", "selector": {"sid": "E4a7c9"}}

        Examples:
          project_into_sketch("s2", "front")
              -> projects the front datum plane intersection line into sketch s2
          project_into_sketch("s2", ["Fdb31d2", "E4a7c9"])
              -> projects a face's full wire loop + one edge into sketch s2
          project_into_sketch("s2", {"kind":"face","selector":{"sid":"Fdb31d2"}},
                              construction=True)
              -> appends construction geometry tracing the face's wires

        Returns {ok, doc_id, sketch, entity_ids, construction, associative,
                 external_refs:[{id, stale, entity_ids}]} (external_refs empty when
        associative=False; use sketch_health to re-check stale status later, and the
        sketch_break_external_ref doc-op to sever a link).
        entity_ids lists the ids of every appended entity in order (first is "$0", rest are
        "$0#1", "$0#2", … from the engine id_map).
        """
        try:
            d = _doc(doc_id)

            # ---- normalise to a list ----
            raw = refs if isinstance(refs, list) else [refs]
            norm_refs: list = []

            _DATUMS = {"front", "top", "right"}
            _SIDES = {"top", "bottom", "left", "right", "front", "back"}

            for ref in raw:
                # Already a well-formed dict — pass through unchanged
                if isinstance(ref, dict):
                    norm_refs.append(ref)
                    continue

                # String shorthand
                s = ref.strip()
                if s in _DATUMS:
                    # Datum plane takes priority over face-side words for front/top/right
                    norm_refs.append({"datum": s})
                elif s.startswith("F"):
                    # Face sid — verify it exists
                    got = cb.call("doc_query", {"doc_id": d,
                                                "query": {"of": "face"}}).get("matches", [])
                    match = next((m for m in got if m.get("sid") == s), None)
                    if match is None:
                        return _dumps({"ok": False, "error":
                                       f"no face with sid {s!r} (call describe_part for current "
                                       f"sids, or use a side word for the face-side lookup)"})
                    norm_refs.append({"kind": "face", "selector": match})
                elif s.startswith("E"):
                    # Edge sid — verify it exists
                    got = cb.call("doc_query", {"doc_id": d,
                                                "query": {"of": "edge"}}).get("matches", [])
                    match = next((m for m in got if m.get("sid") == s), None)
                    if match is None:
                        return _dumps({"ok": False, "error":
                                       f"no edge with sid {s!r} (call describe_part or "
                                       f"query_geometry to list edge sids)"})
                    norm_refs.append({"kind": "edge", "selector": match})
                elif s in _SIDES:
                    # Side word for a face — resolve via doc_query
                    got = cb.call("doc_query", {"doc_id": d,
                                                "query": {"of": "face", "side": s}
                                                }).get("matches", [])
                    if not got:
                        return _dumps({"ok": False, "error":
                                       f"no face with side {s!r} — check describe_part; for "
                                       f"datum planes use 'front'/'top'/'right' directly"})
                    norm_refs.append({"kind": "face", "selector": got[0]})
                else:
                    return _dumps({"ok": False, "error":
                                   f"unrecognised ref {s!r} — expected a datum string "
                                   f"('front'/'top'/'right'), a face sid ('F…'), an edge sid "
                                   f"('E…'), a side word, or a ref dict"})

            op = {"op": "sketch_convert", "sketch": sketch, "refs": norm_refs,
                  "construction": construction, "associative": associative}
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": [op]})
            ok, bad = _feature_ok(r)
            if not ok:
                return _dumps({"ok": False, "error": "sketch_convert op failed to build",
                               "failed_features": bad})

            # Collect entity ids: "$0" is the first; "$0#1", "$0#2", … are the rest
            id_map = r.get("id_map") or {}
            first = id_map.get("$0")
            entity_ids: list = []
            if first is not None:
                entity_ids.append(first)
                k = 1
                while True:
                    nxt = id_map.get(f"$0#{k}")
                    if nxt is None:
                        break
                    entity_ids.append(nxt)
                    k += 1

            ext_refs = _external_refs_of(r, sketch, entity_ids) if associative else []
            note = ("associative: converted geometry re-projects on every rebuild; a "
                    "non-resolving source marks the ref stale (see sketch_health)"
                    if associative else
                    "one-shot projection: converted geometry does NOT update if the "
                    "source model changes")
            return _dumps({"ok": True, "doc_id": d, "sketch": sketch,
                           "entity_ids": entity_ids, "construction": construction,
                           "associative": associative, "external_refs": ext_refs,
                           "note": note})
        except Exception as e:  # noqa: BLE001
            return _err(e)


# ---------------------------------------------------------------- photo-capture + photogrammetry entry path (T-F6)

_SESSIONS: dict = {}  # port -> CaptureSession; module-level, no global state leaks


def register_photo_recon_tools(mcp: FastMCP) -> None:
    """Photo capture session + photogrammetry entry path for reverse_engineer.

    DEFAULT OFF — the local HTTP listener only starts when start_capture_session is called
    with explicit params. Offline mode (reverse_engineer on a file) is completely unaffected.
    """

    @mcp.tool()
    def start_capture_session(
        port: int = 8742,
        scale_marker_mm: Optional[float] = None,
    ) -> str:
        """Start a local photo-capture server. Returns QR code URL + upload instructions.

        The phone scans the QR code and opens a guided upload page. Photos go into a
        temporary directory. Call stop_capture_session() when done to get the paths and
        optionally trigger reconstruction.

        scale_marker_mm: if you placed a known-size fiducial marker in the scene,
        specify its size in mm for automatic scale normalization.
        """
        try:
            # Lazy import so a missing qrcode/open3d can't break server startup
            from services.vision.capture_server import start_session

            if port in _SESSIONS:
                existing = _SESSIONS[port]
                return _dumps({
                    "ok": False,
                    "reason": f"a capture session is already running on port {port}",
                    "hint": "call stop_capture_session first, or use a different port",
                    "upload_url": existing.upload_url,
                    "photo_count": len(existing.received),
                })

            session = start_session(port=port)
            port = session.port                       # resolved (port=0 -> ephemeral pick)
            # Store scale_marker_mm alongside the session
            session._scale_marker_mm = scale_marker_mm  # type: ignore[attr-defined]
            _SESSIONS[port] = session

            return _dumps({
                "ok": True,
                "port": port,
                "upload_url": session.upload_url,
                "qr": session.qr,
                "shot_names": session.shot_names,
                "scale_marker_mm": scale_marker_mm,
                "instructions": (
                    "Scan the QR code with your phone (or open the upload_url in a mobile browser). "
                    f"Upload one photo per guided angle ({len(session.shot_names)} angles). "
                    "When done, call stop_capture_session(port=" + str(port) + ") to "
                    "run reconstruction and get the mesh path for reverse_engineer()."
                ),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def stop_capture_session(
        port: int = 8742,
        run_reconstruction: bool = True,
    ) -> str:
        """Stop the capture session and optionally run photogrammetry reconstruction.

        Returns {ok, photo_count, reconstruction, mesh_path, next_steps}.
        Pass the returned mesh_path to reverse_engineer(source=<mesh_path>) to complete
        the round-trip from photos to an editable parametric feature tree.
        """
        try:
            from services.vision.capture_server import stop_session
            from services.vision import photogrammetry as _pg
            from services.vision import mesh_cleanup as _mc
            import os as _os
            import shutil as _shutil
            import tempfile

            session = _SESSIONS.pop(port, None)
            if session is None:
                return _dumps({
                    "ok": False,
                    "reason": f"no capture session is running on port {port}",
                    "hint": "call start_capture_session first",
                })

            photo_count = len(session.received)
            work_dir = session.work_dir
            stop_session(session)

            result: dict = {
                "ok": True,
                "photo_count": photo_count,
                "photos": session.received,
            }

            if not run_reconstruction or photo_count == 0:
                result["reconstruction"] = None
                result["mesh_path"] = None
                result["next_steps"] = (
                    ["capture more photos and try again"] if photo_count == 0
                    else ["pass the photo paths to your preferred photogrammetry tool "
                          "and then call reverse_engineer(source=<mesh_path>)"]
                )
                # Clean up work_dir only if we're not handing it off
                if work_dir is not None and photo_count == 0:
                    _shutil.rmtree(work_dir, ignore_errors=True)
                return _dumps(result)

            # Run photogrammetry reconstruction
            recon_dir = tempfile.mkdtemp(prefix="aicad_recon_")
            scale_mm = getattr(session, "_scale_marker_mm", None)
            recon = _pg.reconstruct(
                image_dir=work_dir,
                out_dir=recon_dir,
                scale_mm=scale_mm,
            )
            result["reconstruction"] = recon

            mesh_path = None
            if recon.get("ok") and recon.get("mesh_path"):
                raw_mesh = recon["mesh_path"]
                cleaned_path = _os.path.join(recon_dir, "cleaned_mesh.obj")
                cleanup = _mc.cleanup_mesh(raw_mesh, cleaned_path, scale_mm=scale_mm)
                result["cleanup"] = cleanup
                if cleanup.get("ok"):
                    mesh_path = cleanup["mesh_path"]
                    if cleanup.get("notes"):
                        result["warnings"] = cleanup["notes"]
                else:
                    # Fallback to raw mesh if cleanup fails (e.g. open3d not installed)
                    mesh_path = raw_mesh
                    result["cleanup_skipped"] = cleanup.get("reason")

            result["mesh_path"] = mesh_path

            steps = []
            if mesh_path:
                steps.append(
                    f"call reverse_engineer(source={mesh_path!r}) to convert the mesh "
                    "into an editable parametric feature tree"
                )
            else:
                steps.append(
                    "photogrammetry did not produce a mesh — check reconstruction.reason; "
                    "ensure MESHROOM_BIN or COLMAP_BIN is set"
                )
            result["next_steps"] = steps

            # Clean up photo work_dir now that mesh is written to recon_dir
            if work_dir is not None:
                _shutil.rmtree(work_dir, ignore_errors=True)

            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)
