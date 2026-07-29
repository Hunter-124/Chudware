"""Geometric / drawing-quality checks for ``aicad.sch/1`` schematics — the pre-ERC self-check.

ERC (core/ecad/erc.cpp, surfaced by ``ecad_run_erc``) catches ELECTRICAL problems: undriven
rails, output-output shorts, floating pins. It says nothing about whether the drawing is a
*mess*, so an agent can emit a schematic that passes ERC and is still wrong to a human:

  1. two symbol bodies drawn on top of each other        -> ``symbol_overlap``
  2. a wire whose end touches nothing at all             -> ``dangling_wire``
  3. a wire routed straight THROUGH a symbol body        -> ``wire_crosses_symbol``
  4. a net label sitting on empty paper                  -> ``floating_label``

Everything here is pure geometry over the document dict (the same dict ``ecad_status`` reads),
so there is no RPC and nothing to invent: a finding is a measured coordinate, never a guess.

False positives are worse than no tool at all — an agent that learns to ignore this check has
lost the capability. The deliberate exclusions, each covered by a test:

* **Body, not bounding box.** A symbol's "body" is the bbox of its *body* graphics only. Pin
  LEAD lines (a graphic line with an endpoint on a pin root or pin tip) are excluded, so a
  resistor's body is its 2.03x1.27 mm rect, not the 5.08 mm pin-to-pin span. Wires reaching a
  pin tip therefore never enter the body.
* **A wire may END on the boundary.** Crossing is measured against the body inset by
  ``BODY_INSET``, and only a crossing longer than ``MIN_CROSS_MM`` counts. A wire terminating
  exactly on a pin that sits on the body edge clips to zero length and is not reported.
* **A pin tip inside its own body is the symbol definition's fault, not the router's.** If a
  wire ends on a pin tip that is geometrically inside that symbol's body, that symbol is
  skipped for that wire.
* **Junctions, labels, power ports, no-connect markers and sheet pins all terminate a wire.**
  A named-net stub (wire + label on its end) and a bus are not orphans.
* **Sheets are independent.** Nothing is ever compared across two sheets.
* **A symbol with no resolvable body is skipped** for the overlap/crossing checks and named in
  ``symbols_without_body``, rather than guessed at.

Coincidence tolerance is ``TOL_MM`` = 1e-4 mm. The netlist snaps at 1e-6 mm
(core/ecad/netlist.cpp kSnapTol), so anything this check calls "touching" really is one net to
the engine, and a near-miss stub stays a finding instead of being tolerated away.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from services.ecad.schema import SCHEMA_SCH, local_to_world, pin_world

# Coincidence tolerance (mm). Far below any meaningful schematic offset (grid = 2.54 mm) and far
# above the float noise of the rotate/mirror transform.
TOL_MM = 1e-4
# Body inset (mm) used for the crossing/overlap tests so touching an edge is never a crossing.
BODY_INSET = 0.05
# A crossing shorter than this is a graze, not a wire routed through a part.
MIN_CROSS_MM = 0.05
# Minimum body extent (mm) — a degenerate (collinear) body cannot be crossed or overlapped.
MIN_BODY_MM = 0.05


# ─────────────────────────────────────────────────────────── small geometry primitives


def _near(a: Iterable[float], b: Iterable[float], tol: float = TOL_MM) -> bool:
    ax, ay = float(a[0]), float(a[1])  # type: ignore[index]
    bx, by = float(b[0]), float(b[1])  # type: ignore[index]
    return abs(ax - bx) <= tol and abs(ay - by) <= tol


def _seg_dist(p, a, b) -> float:
    """Distance from point ``p`` to the closed segment ``a``-``b``."""
    px, py = float(p[0]), float(p[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    if den <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / den
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _polyline_dist(p, pts: list) -> float:
    """Distance from ``p`` to a polyline (inf if fewer than 2 points)."""
    best = math.inf
    for i in range(len(pts) - 1):
        d = _seg_dist(p, pts[i], pts[i + 1])
        if d < best:
            best = d
    if len(pts) == 1:
        best = min(best, math.hypot(float(p[0]) - float(pts[0][0]),
                                    float(p[1]) - float(pts[0][1])))
    return best


def _ccw(poly: list) -> list:
    """Return the polygon with counter-clockwise winding (positive signed area)."""
    area = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return poly if area >= 0.0 else list(reversed(poly))


def _shrink(poly: list, margin: float) -> list:
    """Inset a convex polygon by pulling each vertex ``margin`` mm toward the centroid."""
    n = len(poly)
    cx = sum(p[0] for p in poly) / n
    cy = sum(p[1] for p in poly) / n
    out = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy)
        s = 0.0 if d <= margin else (1.0 - margin / d)
        out.append((cx + dx * s, cy + dy * s))
    return out


def _clip_segment(a, b, poly: list) -> Optional[tuple[float, float]]:
    """Cyrus-Beck clip of segment ``a``-``b`` against a CCW convex polygon.

    Returns ``(t_enter, t_exit)`` in [0, 1] for the portion inside, or None.
    """
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    n = len(poly)
    for i in range(n):
        px, py = poly[i]
        qx, qy = poly[(i + 1) % n]
        # inward normal of edge p->q for CCW winding
        nx, ny = -(qy - py), (qx - px)
        num = nx * (ax - px) + ny * (ay - py)
        den = nx * dx + ny * dy
        if abs(den) < 1e-15:
            if num < 0.0:
                return None
            continue
        t = -num / den
        if den > 0.0:
            if t > t0:
                t0 = t
        else:
            if t < t1:
                t1 = t
        if t0 >= t1:
            return None
    return (t0, t1)


def _point_in_poly(p, poly: list) -> bool:
    """Is ``p`` inside (or on) a CCW convex polygon?"""
    px, py = float(p[0]), float(p[1])
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (-(by - ay)) * (px - ax) + (bx - ax) * (py - ay) < -TOL_MM:
            return False
    return True


def _convex_overlap(pa: list, pb: list) -> bool:
    """Separating-axis test: do two convex polygons share interior area?"""
    for poly in (pa, pb):
        n = len(poly)
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            nx, ny = -(by - ay), (bx - ax)
            d = math.hypot(nx, ny)
            if d <= 0.0:
                continue
            nx, ny = nx / d, ny / d
            a_proj = [nx * x + ny * y for x, y in pa]
            b_proj = [nx * x + ny * y for x, y in pb]
            if max(a_proj) < min(b_proj) or max(b_proj) < min(a_proj):
                return False
    return True


def _bbox(pts: list) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _r(v: float, n: int = 3) -> float:
    return round(float(v) + 0.0, n)


def _rp(p) -> list:
    return [_r(p[0]), _r(p[1])]


# ─────────────────────────────────────────────────────────── symbol bodies


def _visible_pins(sdef: dict, unit: int) -> list:
    return [p for p in sdef.get("pins", []) or []
            if isinstance(p, dict) and p.get("unit", 1) in (unit, 0)]


def _pin_lead_points(sdef: dict, unit: int) -> set:
    """Rounded local (root, tip) points of every visible pin — used to spot lead graphics."""
    out = set()
    for pin in _visible_pins(sdef, unit):
        pos = pin.get("pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            continue
        px, py = float(pos[0]), float(pos[1])
        out.add((round(px, 4), round(py, 4)))
        length = float(pin.get("length", 2.54) or 0.0)
        a = math.radians(float(pin.get("angle", 0.0) or 0.0))
        out.add((round(px + length * math.cos(a), 4), round(py + length * math.sin(a), 4)))
    return out


def _graphic_points(g: dict) -> list:
    """Local points a symbol graphic spans (a circle contributes its bbox corners)."""
    t = str(g.get("type") or "")
    if t == "circle":
        c = g.get("center") or [0.0, 0.0]
        r = abs(float(g.get("radius", 0.0) or 0.0))
        cx, cy = float(c[0]), float(c[1])
        return [(cx - r, cy - r), (cx + r, cy + r)]
    pts: list = []
    for key in ("p1", "p2", "start", "mid", "end", "center"):
        v = g.get(key)
        if isinstance(v, (list, tuple)) and len(v) == 2:
            pts.append((float(v[0]), float(v[1])))
    seq = g.get("pts")
    if isinstance(seq, (list, tuple)):
        for v in seq:
            if isinstance(v, (list, tuple)) and len(v) == 2:
                pts.append((float(v[0]), float(v[1])))
    return pts


def body_local_rect(sdef: dict, unit: int = 1) -> Optional[tuple[float, float, float, float]]:
    """Local-frame body bbox of a symbol definition, or None when it has no body.

    Pin lead graphics — a ``line`` with an endpoint on a pin root or pin tip — are EXCLUDED,
    which is what keeps the resistor's body off its pin span and keeps wires-to-pins out of
    the crossing check.
    """
    leads = _pin_lead_points(sdef, unit)
    pts: list = []
    for g in sdef.get("graphics", []) or []:
        if not isinstance(g, dict):
            continue
        gpts = _graphic_points(g)
        if not gpts:
            continue
        if str(g.get("type") or "") == "line" and len(gpts) == 2:
            if any((round(x, 4), round(y, 4)) in leads for x, y in gpts):
                continue  # a pin lead stub, not the body
        pts.extend(gpts)
    if len(pts) < 2:
        return None
    x0, y0, x1, y1 = _bbox(pts)
    if (x1 - x0) < MIN_BODY_MM or (y1 - y0) < MIN_BODY_MM:
        return None
    return (x0, y0, x1, y1)


def symbol_body_quad(doc: dict, sym: dict) -> Optional[list]:
    """The four world-space corners (CCW) of a placed symbol's body, or None."""
    sdef = (doc.get("lib_cache") or {}).get(sym.get("libid"))
    if not isinstance(sdef, dict):
        return None
    rect = body_local_rect(sdef, sym.get("unit", 1))
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    pos = sym.get("pos") or [0.0, 0.0]
    ang = float(sym.get("angle", 0.0) or 0.0)
    mir = sym.get("mirror", "none") or "none"
    corners = [local_to_world(p, pos, ang, mir)
               for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return _ccw([(float(x), float(y)) for x, y in corners])


def _sheet(obj: dict) -> str:
    return str(obj.get("sheet") or "sh1")


def _placed_bodies(doc: dict) -> list:
    """``[{sym, ref, quad, sheet}, ...]`` for every placed symbol with a resolvable body."""
    out = []
    for sym in doc.get("symbols", []) or []:
        if not isinstance(sym, dict):
            continue
        quad = symbol_body_quad(doc, sym)
        if quad is None:
            continue
        out.append({"sym": sym, "ref": sym.get("ref") or sym.get("id") or "?",
                    "quad": quad, "sheet": _sheet(sym)})
    return out


def symbols_without_body(doc: dict) -> list:
    """Refs of placed symbols whose body could not be resolved (honestly skipped, not guessed)."""
    out = []
    for sym in doc.get("symbols", []) or []:
        if isinstance(sym, dict) and symbol_body_quad(doc, sym) is None:
            out.append(sym.get("ref") or sym.get("id") or "?")
    return out


# ─────────────────────────────────────────────────────────── connection points


def _polylines(doc: dict) -> list:
    """Every wire-like polyline: ``{id, pts, sheet, kind}`` over wires + buses."""
    out = []
    for w in doc.get("wires", []) or []:
        pts = w.get("pts") if isinstance(w, dict) else None
        if isinstance(pts, list) and len(pts) >= 2:
            out.append({"id": w.get("id") or "?", "pts": pts, "sheet": _sheet(w),
                        "kind": "bus" if w.get("bus") else "wire"})
    for b in doc.get("buses", []) or []:
        pts = b.get("pts") if isinstance(b, dict) else None
        if isinstance(pts, list) and len(pts) >= 2:
            out.append({"id": b.get("id") or "?", "pts": pts, "sheet": _sheet(b),
                        "kind": "bus"})
    return out


def _terminators(doc: dict) -> list:
    """Everything a wire end or a label may legitimately land on.

    ``[{pos, sheet, what}, ...]`` — pin tips, junctions, power ports, no-connect markers and
    hierarchical sheet pins. Labels are handled separately (a label terminates a wire, but a
    label on a label is not a connection).
    """
    out: list[dict] = []
    cache = doc.get("lib_cache") or {}
    for sym in doc.get("symbols", []) or []:
        if not isinstance(sym, dict):
            continue
        sdef = cache.get(sym.get("libid"))
        if not isinstance(sdef, dict):
            continue
        ref = sym.get("ref") or sym.get("id") or "?"
        for pin in _visible_pins(sdef, sym.get("unit", 1)):
            if not (isinstance(pin.get("pos"), (list, tuple)) and len(pin["pos"]) == 2):
                continue
            out.append({"pos": pin_world(sym, pin), "sheet": _sheet(sym),
                        "what": f"pin {ref}.{pin.get('number', '?')}"})
    for j in doc.get("junctions", []) or []:
        if isinstance(j, dict) and isinstance(j.get("pos"), (list, tuple)):
            out.append({"pos": tuple(j["pos"]), "sheet": _sheet(j),
                        "what": f"junction {j.get('id', '?')}"})
    for p in doc.get("power", []) or []:
        if isinstance(p, dict) and isinstance(p.get("pos"), (list, tuple)):
            out.append({"pos": tuple(p["pos"]), "sheet": _sheet(p),
                        "what": f"power port {p.get('net', '?')}"})
    for nc in doc.get("no_connect", []) or []:
        if isinstance(nc, dict) and isinstance(nc.get("pos"), (list, tuple)):
            out.append({"pos": tuple(nc["pos"]), "sheet": _sheet(nc),
                        "what": f"no-connect {nc.get('id', '?')}"})
    for hs in doc.get("sheet_symbols", []) or []:
        if not isinstance(hs, dict):
            continue
        hpos = hs.get("pos") or [0.0, 0.0]
        for pn in hs.get("pins", []) or []:
            if isinstance(pn, dict) and isinstance(pn.get("pos"), (list, tuple)):
                out.append({"pos": local_to_world(pn["pos"], hpos,
                                                  float(hs.get("angle", 0.0) or 0.0),
                                                  hs.get("mirror", "none") or "none"),
                            "sheet": _sheet(hs),
                            "what": f"sheet pin {hs.get('id', '?')}.{pn.get('name', '?')}"})
    return out


def _label_points(doc: dict) -> list:
    out = []
    for lb in doc.get("labels", []) or []:
        if isinstance(lb, dict) and isinstance(lb.get("pos"), (list, tuple)):
            out.append({"pos": tuple(lb["pos"]), "sheet": _sheet(lb),
                        "what": f"label {lb.get('text', '?')!r}",
                        "text": lb.get("text", ""), "id": lb.get("id", "?")})
    return out


# ─────────────────────────────────────────────────────────── the four checks


def check_symbol_overlaps(doc: dict) -> list[dict]:
    """Placed symbols whose BODIES interpenetrate (same sheet, positive shared area)."""
    bodies = _placed_bodies(doc)
    findings: list[dict] = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a, b = bodies[i], bodies[j]
            if a["sheet"] != b["sheet"]:
                continue
            if not _convex_overlap(_shrink(a["quad"], BODY_INSET),
                                   _shrink(b["quad"], BODY_INSET)):
                continue
            ax0, ay0, ax1, ay1 = _bbox(a["quad"])
            bx0, by0, bx1, by1 = _bbox(b["quad"])
            ox0, oy0 = max(ax0, bx0), max(ay0, by0)
            ox1, oy1 = min(ax1, bx1), min(ay1, by1)
            findings.append({
                "kind": "symbol_overlap",
                "refs": [a["ref"], b["ref"]],
                "sheet": a["sheet"],
                "a": {"ref": a["ref"], "libid": a["sym"].get("libid"),
                      "pos": _rp(a["sym"].get("pos") or [0, 0]),
                      "body_bbox": [_r(ax0), _r(ay0), _r(ax1), _r(ay1)]},
                "b": {"ref": b["ref"], "libid": b["sym"].get("libid"),
                      "pos": _rp(b["sym"].get("pos") or [0, 0]),
                      "body_bbox": [_r(bx0), _r(by0), _r(bx1), _r(by1)]},
                "overlap_bbox": [_r(ox0), _r(oy0), _r(ox1), _r(oy1)],
                "message": (f"{a['ref']} body overlaps {b['ref']} body over "
                            f"[{_r(ox0)}, {_r(oy0)}]..[{_r(ox1)}, {_r(oy1)}] mm — "
                            f"move one of them (render_schematic_ascii, then "
                            f"apply_ascii_edits with "
                            f"[{{'op': 'move', 'ref': '{b['ref']}', 'to_cell': [..]}}])."),
            })
    return findings


def check_dangling_wires(doc: dict) -> list[dict]:
    """Wire ends that touch nothing: no pin, no other wire, no junction/label/power/NC/sheet pin."""
    lines = _polylines(doc)
    terms = _terminators(doc) + _label_points(doc)
    findings: list[dict] = []
    for idx, ln in enumerate(lines):
        pts = ln["pts"]
        for which, p in (("start", pts[0]), ("end", pts[-1])):
            hit = None
            nearest = (math.inf, "")
            for t in terms:
                if t["sheet"] != ln["sheet"]:
                    continue
                d = math.hypot(float(p[0]) - float(t["pos"][0]),
                               float(p[1]) - float(t["pos"][1]))
                if d <= TOL_MM:
                    hit = t["what"]
                    break
                if d < nearest[0]:
                    nearest = (d, t["what"])
            if hit:
                continue
            for k, other in enumerate(lines):
                if k == idx or other["sheet"] != ln["sheet"]:
                    continue
                d = _polyline_dist(p, other["pts"])
                if d <= TOL_MM:
                    hit = f"{other['kind']} {other['id']}"
                    break
                if d < nearest[0]:
                    nearest = (d, f"{other['kind']} {other['id']}")
            if hit:
                continue
            f = {
                "kind": "dangling_wire",
                "wire_id": ln["id"], "wire_kind": ln["kind"], "sheet": ln["sheet"],
                "end": which, "pos": _rp(p),
                "message": (f"{ln['kind']} {ln['id']} {which} at "
                            f"({_r(p[0])}, {_r(p[1])}) touches nothing — "
                            f"wire it to a pin (ecad_wire), drop a label/power port on it, "
                            f"or delete the stub."),
            }
            if nearest[1]:
                f["nearest"] = {"what": nearest[1], "distance_mm": _r(nearest[0], 4)}
                if nearest[0] <= 2.54:
                    f["message"] += (f" Nearest connection point is {nearest[1]} "
                                     f"{_r(nearest[0], 4)} mm away — a near miss, not a "
                                     f"connection.")
            findings.append(f)
    return findings


def check_wires_crossing_symbols(doc: dict) -> list[dict]:
    """Wire segments that run THROUGH a symbol body (not merely terminate on its pins)."""
    bodies = _placed_bodies(doc)
    lines = _polylines(doc)
    findings: list[dict] = []
    for ln in lines:
        pts = ln["pts"]
        ends = (pts[0], pts[-1])
        for body in bodies:
            if body["sheet"] != ln["sheet"]:
                continue
            quad = body["quad"]
            # A pin tip that lives INSIDE its own body forces any wire reaching it to enter the
            # body — that is the symbol definition's geometry, not a routing defect.
            forced = False
            sdef = (doc.get("lib_cache") or {}).get(body["sym"].get("libid")) or {}
            for pin in _visible_pins(sdef, body["sym"].get("unit", 1)):
                if not (isinstance(pin.get("pos"), (list, tuple)) and len(pin["pos"]) == 2):
                    continue
                tip = pin_world(body["sym"], pin)
                if _point_in_poly(tip, quad) and any(_near(e, tip) for e in ends):
                    forced = True
                    break
            if forced:
                continue
            inset = _shrink(quad, BODY_INSET)
            worst = None
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                clip = _clip_segment(a, b, inset)
                if clip is None:
                    continue
                t0, t1 = clip
                seg_len = math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
                inside = (t1 - t0) * seg_len
                if inside < MIN_CROSS_MM:
                    continue
                enter = (float(a[0]) + t0 * (float(b[0]) - float(a[0])),
                         float(a[1]) + t0 * (float(b[1]) - float(a[1])))
                exit_ = (float(a[0]) + t1 * (float(b[0]) - float(a[0])),
                         float(a[1]) + t1 * (float(b[1]) - float(a[1])))
                if worst is None or inside > worst[0]:
                    worst = (inside, i, enter, exit_)
            if worst is None:
                continue
            inside, i, enter, exit_ = worst
            findings.append({
                "kind": "wire_crosses_symbol",
                "wire_id": ln["id"], "wire_kind": ln["kind"], "sheet": ln["sheet"],
                "ref": body["ref"], "libid": body["sym"].get("libid"),
                "segment": [_rp(pts[i]), _rp(pts[i + 1])],
                "enter": _rp(enter), "exit": _rp(exit_),
                "inside_mm": _r(inside),
                "message": (f"{ln['kind']} {ln['id']} runs through {body['ref']}'s body for "
                            f"{_r(inside)} mm ([{_r(enter[0])}, {_r(enter[1])}] -> "
                            f"[{_r(exit_[0])}, {_r(exit_[1])}]) — delete that wire and re-run "
                            f"ecad_wire between the intended pins, or move {body['ref']} "
                            f"(render_schematic_ascii + apply_ascii_edits)."),
            })
    return findings


def check_floating_labels(doc: dict) -> list[dict]:
    """Net labels that sit on no wire and no pin, so they name nothing."""
    lines = _polylines(doc)
    terms = _terminators(doc)
    findings: list[dict] = []
    for lb in _label_points(doc):
        p = lb["pos"]
        hit = None
        nearest = (math.inf, "")
        for t in terms:
            if t["sheet"] != lb["sheet"]:
                continue
            d = math.hypot(float(p[0]) - float(t["pos"][0]), float(p[1]) - float(t["pos"][1]))
            if d <= TOL_MM:
                hit = t["what"]
                break
            if d < nearest[0]:
                nearest = (d, t["what"])
        if hit is None:
            for ln in lines:
                if ln["sheet"] != lb["sheet"]:
                    continue
                d = _polyline_dist(p, ln["pts"])
                if d <= TOL_MM:
                    hit = f"{ln['kind']} {ln['id']}"
                    break
                if d < nearest[0]:
                    nearest = (d, f"{ln['kind']} {ln['id']}")
        if hit:
            continue
        f = {
            "kind": "floating_label",
            "label_id": lb["id"], "text": lb["text"], "sheet": lb["sheet"],
            "pos": _rp(p),
            "message": (f"label {lb['text']!r} at ({_r(p[0])}, {_r(p[1])}) is on no wire and "
                        f"no pin — it names nothing. Move it onto a wire or a pin's world xy "
                        f"(ecad_label with the pin xy from ecad_place_symbol)."),
        }
        if nearest[1]:
            f["nearest"] = {"what": nearest[1], "distance_mm": _r(nearest[0], 4)}
            f["message"] += f" Nearest is {nearest[1]} at {_r(nearest[0], 4)} mm."
        findings.append(f)
    return findings


# ─────────────────────────────────────────────────────────── report


CHECK_KINDS = ("symbol_overlap", "dangling_wire", "wire_crosses_symbol", "floating_label")


def check_schematic_geometry(doc: dict, *, max_per_kind: int = 20) -> dict:
    """Run all four geometric checks over a schematic doc and return a structured report.

    Raises ValueError when ``doc`` is not an ``aicad.sch/1`` document (there is nothing
    honest to report about a board here).
    """
    if doc.get("schema") != SCHEMA_SCH:
        raise ValueError(f"not a schematic document (schema={doc.get('schema')!r})")
    by_kind: dict[str, list[dict]] = {
        "symbol_overlap": check_symbol_overlaps(doc),
        "dangling_wire": check_dangling_wires(doc),
        "wire_crosses_symbol": check_wires_crossing_symbols(doc),
        "floating_label": check_floating_labels(doc),
    }
    counts = {k: len(v) for k, v in by_kind.items()}
    total = sum(counts.values())
    truncated: dict[str, int] = {}
    findings: list[dict] = []
    for kind in CHECK_KINDS:
        items = by_kind[kind]
        cap = max(0, int(max_per_kind))
        if cap and len(items) > cap:
            truncated[kind] = len(items) - cap
            items = items[:cap]
        findings.extend(items)
    no_body = symbols_without_body(doc)
    return {
        "clean": total == 0,
        "total": total,
        "counts": counts,
        "findings": findings,
        "truncated": truncated,
        "checked": {
            "symbols": len(doc.get("symbols", []) or []),
            "wires": len(doc.get("wires", []) or []),
            "buses": len(doc.get("buses", []) or []),
            "labels": len(doc.get("labels", []) or []),
            "junctions": len(doc.get("junctions", []) or []),
            "bodies_resolved": len(_placed_bodies(doc)),
            "symbols_without_body": no_body,
        },
    }


def verdict(report: dict) -> str:
    """One human line: what is geometrically wrong with this drawing."""
    if report.get("clean"):
        ch = report.get("checked", {})
        return (f"Drawing is geometrically clean: {ch.get('symbols', 0)} symbol(s), "
                f"{ch.get('wires', 0)} wire(s), {ch.get('labels', 0)} label(s) — no overlaps, "
                f"no dangling wire ends, no wires through bodies, no floating labels.")
    c = report.get("counts", {})
    parts = []
    if c.get("symbol_overlap"):
        parts.append(f"{c['symbol_overlap']} overlapping symbol pair(s)")
    if c.get("dangling_wire"):
        parts.append(f"{c['dangling_wire']} dangling wire end(s)")
    if c.get("wire_crosses_symbol"):
        parts.append(f"{c['wire_crosses_symbol']} wire(s) through a symbol body")
    if c.get("floating_label"):
        parts.append(f"{c['floating_label']} floating label(s)")
    return f"{report.get('total', 0)} drawing problem(s): " + ", ".join(parts) + "."
