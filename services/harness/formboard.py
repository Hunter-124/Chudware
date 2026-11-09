"""Formboard: flatten a routed harness tree into a true-length 2D drawing.

Algorithm (deterministic — no randomness, ids and lengths decide every tie):

1. Work on a copy of the harness with its derived state rebuilt; keep only segments that
   carry wires. Each connected component must be a tree (a loop is refused).
2. Per component the *trunk* is the longest path (by true length) between two nodes; it is laid
   along +x. Every other branch is attached at its breakout node at ``alpha`` degrees from
   the trunk (alternating sides, widening by ``fan`` for further branches at the same node);
   sub-branches fan out around the heading they inherit. Every segment is drawn at its TRUE
   length times the sheet ``scale`` (1.0 = full size).
3. Layout parameters ``(alpha, fan, first_side)`` are searched in a fixed order; the first
   candidate with no overlap wins, else the one with the fewest overlaps (reported, never
   hidden). The overlap check covers segment-vs-segment distance (bundle half widths + gap),
   the angle between sibling segments at a shared node, connector symbols and label boxes.
4. Connector symbols, segment-length labels, breakout angles, clamp ticks, the wire table and
   per-connector pinout / from-to tables are added; everything is returned as one layout
   dict that :func:`render_svg`, :func:`write_dxf` and :func:`write_pdf` draw.

Coordinates in the layout are sheet millimetres, x right, y UP (SVG output flips y).
"""
from __future__ import annotations

import copy
import math
from typing import Any, Optional
from xml.sax.saxutils import escape

from services.harness import model as M

SCHEMA = "aicad.harness.formboard/1"
TEXT_H = 3.0                    # label glyph height, sheet mm
CHAR_W = 0.6 * TEXT_H           # monospace advance used for every width estimate
SYMBOL_W = 14.0                 # connector symbol length (along the cable), sheet mm
SYMBOL_H = 8.0                  # connector symbol width, sheet mm
MIN_HALF_WIDTH = 0.4            # thinnest drawn half-width of a bundle, sheet mm
MIN_BREAKOUT_DEG = 25.0         # sibling segments closer than this overlap at their node
GAP_MM = 3.0                    # minimum clear space between non-adjacent bundles
LABEL_GAP_MM = 0.8              # clearance around label boxes
ROW_H = TEXT_H * 1.8
PAD = CHAR_W
MARGIN = 10.0
_ALPHAS = (60.0, 90.0, 45.0, 120.0, 75.0, 105.0, 135.0)
_FANS = (50.0, 70.0, 35.0)
_SIDES = (1, -1)


# ----------------------------------------------------------------------------- 2D geometry

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _len(a):
    return math.hypot(a[0], a[1])


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_seg(a, b, c, eps=1e-9) -> bool:
    return (min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps)


def _intersect(p1, p2, q1, q2, eps=1e-9) -> bool:
    o1, o2 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    o3, o4 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and \
            ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    return ((abs(o1) <= eps and _on_seg(p1, p2, q1)) or (abs(o2) <= eps and _on_seg(p1, p2, q2))
            or (abs(o3) <= eps and _on_seg(q1, q2, p1)) or (abs(o4) <= eps and _on_seg(q1, q2, p2)))


def _pt_seg(p, a, b) -> float:
    ab = _sub(b, a)
    den = ab[0] * ab[0] + ab[1] * ab[1]
    t = 0.0 if den < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / den))
    return math.hypot(p[0] - (a[0] + t * ab[0]), p[1] - (a[1] + t * ab[1]))


def _seg_dist(p0, p1, q0, q1) -> float:
    if _intersect(p0, p1, q0, q1):
        return 0.0
    return min(_pt_seg(p0, q0, q1), _pt_seg(p1, q0, q1), _pt_seg(q0, p0, p1), _pt_seg(q1, p0, p1))


def _rect_edges(r):
    x0, y0, x1, y1 = r
    c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def _in_rect(p, r) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def _in_convex(p, polygon) -> bool:
    turns = [_orient(a, b, p) for a, b in zip(polygon, polygon[1:] + polygon[:1])]
    return all(v >= -1e-9 for v in turns) or all(v <= 1e-9 for v in turns)


def _seg_rect_dist(a, b, r) -> float:
    if _in_rect(a, r) or _in_rect(b, r):
        return 0.0
    return min(_seg_dist(a, b, e0, e1) for e0, e1 in _rect_edges(r))


def _rect_gap(r1, r2) -> float:
    dx = max(r1[0] - r2[2], r2[0] - r1[2], 0.0)
    dy = max(r1[1] - r2[3], r2[1] - r1[3], 0.0)
    return math.hypot(dx, dy)



def _rect_polygon_gap(rect, polygon) -> float:
    corners = [(rect[0], rect[1]), (rect[2], rect[1]),
               (rect[2], rect[3]), (rect[0], rect[3])]
    if _in_convex(corners[0], polygon) or _in_rect(polygon[0], rect):
        return 0.0
    return min(_seg_dist(a, b, c, d)
               for a, b in _rect_edges(rect)
               for c, d in zip(polygon, polygon[1:] + polygon[:1]))



def _wrap_deg(a: float) -> float:
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


# ----------------------------------------------------------------------------- tree extraction

def _active(h: dict) -> "tuple[list, dict]":
    """Segments that carry wires and have length; and the nodes dict."""
    nodes = {n["id"]: n for n in h["nodes"]}
    segs = [s for s in sorted(h["segments"], key=lambda s: s["id"])
            if s.get("wires") and M.seg_length(s) > 1e-9]
    return segs, nodes


def _components(segs: list) -> list:
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in segs:
        ra, rb = find(s["a"]), find(s["b"])
        if ra == rb:
            raise M.HarnessError(
                "not_a_tree", f"segment {s['id']} closes a loop; a formboard needs a tree",
                hint="remove the redundant segment (harness_edit action='remove_segment', "
                     "segment_id=...) and re-route; "
                     "wires follow the unique tree path between connectors")
        parent[ra] = rb
    groups: dict = {}
    for s in segs:
        groups.setdefault(find(s["a"]), []).append(s)
    return sorted(groups.values(), key=lambda g: min(min(s["a"], s["b"]) for s in g))


class _Comp:
    """One connected tree: adjacency, true lengths and the trunk."""

    def __init__(self, segs: list):
        self.segs = {s["id"]: s for s in segs}
        self.len = {s["id"]: M.seg_length(s) for s in segs}
        self.adj: dict = {}
        for s in segs:
            self.adj.setdefault(s["a"], []).append((s["id"], s["b"]))
            self.adj.setdefault(s["b"], []).append((s["id"], s["a"]))
        for k in self.adj:
            self.adj[k].sort()
        self.nodes = sorted(self.adj)
        self.trunk_nodes, self.trunk_segs = self._trunk()

    def _far(self, start: str) -> "tuple[str, dict]":
        dist = {start: 0.0}
        par: dict = {start: None}
        stack = [start]
        while stack:
            u = stack.pop()
            for sid, v in self.adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + self.len[sid]
                    par[v] = (u, sid)
                    stack.append(v)
        best = start
        for n in sorted(dist):
            if dist[n] > dist[best] + 1e-9:
                best = n
        return best, par

    def _trunk(self) -> "tuple[list, list]":
        t0, _ = self._far(self.nodes[0])
        t1, par = self._far(t0)
        nodes, segs = [t1], []
        cur = t1
        while par[cur] is not None:
            u, sid = par[cur]
            segs.append(sid)
            nodes.append(u)
            cur = u
        nodes.reverse()
        segs.reverse()
        return nodes, segs

    def subtree_weight(self, u: str, parent_sid: Optional[str]) -> float:
        total = 0.0
        stack = [(u, parent_sid)]
        while stack:
            n, ps = stack.pop()
            for sid, v in self.adj[n]:
                if sid != ps:
                    total += self.len[sid]
                    stack.append((v, sid))
        return total


def _place(comp: _Comp, scale: float, alpha: float, fan: float, first_side: int) -> "tuple[dict, dict, list]":
    """Positions (sheet mm) of every node, per-segment heading and breakout records."""
    pos: dict = {}
    heading: dict = {}          # segment id -> heading deg of travel away from its parent node
    breakouts: list = []

    def place_child(u: str, sid: str, v: str, theta: float, parent_sid: Optional[str], rel: Optional[float]):
        L = comp.len[sid] * scale
        pos[v] = (pos[u][0] + L * math.cos(math.radians(theta)),
                  pos[u][1] + L * math.sin(math.radians(theta)))
        heading[sid] = theta
        recurse(v, sid, theta)

    def recurse(u: str, parent_sid: Optional[str], theta: float):
        kids = [(sid, v) for sid, v in comp.adj[u] if sid != parent_sid]
        if not kids:
            return
        if len(kids) == 1:
            place_child(u, kids[0][0], kids[0][1], theta, parent_sid, 0.0)
            return
        kids.sort(key=lambda kv: (-comp.subtree_weight(kv[1], kv[0]), kv[0]))
        # alternate around the inherited heading, heaviest nearest the heading
        k = len(kids)
        offsets = [(i - (k - 1) / 2.0) * fan for i in range(k)]
        order = sorted(range(k), key=lambda i: (abs(offsets[i]), -offsets[i]))
        rec = {"node": u, "at": None, "branches": []}
        for rank, i in enumerate(order):
            sid, v = kids[rank]
            off = offsets[i]
            place_child(u, sid, v, theta + off, parent_sid, off)
            rec["branches"].append({"segment": sid, "angle_deg": _wrap_deg(off)})
        breakouts.append(rec)

    tn, ts = comp.trunk_nodes, comp.trunk_segs
    pos[tn[0]] = (0.0, 0.0)
    for i, sid in enumerate(ts):
        L = comp.len[sid] * scale
        pos[tn[i + 1]] = (pos[tn[i]][0] + L, 0.0)
        heading[sid] = 0.0
    trunk_set = set(ts)
    for i, u in enumerate(tn):
        kids = [(sid, v) for sid, v in comp.adj[u] if sid not in trunk_set]
        if not kids:
            continue
        kids.sort(key=lambda kv: (-comp.subtree_weight(kv[1], kv[0]), kv[0]))
        rec = {"node": u, "at": None, "branches": []}
        for j, (sid, v) in enumerate(kids):
            side = first_side if j % 2 == 0 else -first_side
            off = side * (alpha + (j // 2) * fan)
            place_child(u, sid, v, off, None, off)
            rec["branches"].append({"segment": sid, "angle_deg": _wrap_deg(off)})
        breakouts.append(rec)
    return pos, heading, breakouts


# ----------------------------------------------------------------------------- evaluation

def _text_w(text: str) -> float:
    return len(text) * CHAR_W


def _circle_R() -> float:
    return math.hypot(SYMBOL_W, SYMBOL_H / 2.0)


def _build_items(comp: _Comp, h: dict, pos: dict, scale: float) -> "tuple[list, list]":
    """Segment drawables and connector symbols for one candidate placement."""
    segs = []
    for sid in sorted(comp.segs):
        s = comp.segs[sid]
        p0, p1 = pos[s["a"]], pos[s["b"]]
        od = s.get("od_mm")
        segs.append({"id": sid, "a": s["a"], "b": s["b"], "p0": p0, "p1": p1,
                     "length_mm": comp.len[sid],
                     "drawn_length_mm": _len(_sub(p1, p0)) / scale,
                     "od_mm": od, "half_width": max(od * scale / 2.0, MIN_HALF_WIDTH)
                     if od is not None else MIN_HALF_WIDTH,
                     "wires": list(s.get("wires") or [])})
    conns = []
    cmap = {c["id"]: c for c in h["connectors"]}
    for n in comp.nodes:
        if n not in cmap:
            continue
        c = cmap[n]
        out = [(sid, v) for sid, v in comp.adj[n]]
        if len(out) == 1:
            sid, v = out[0]
            d = _sub(pos[v], pos[n])
            L = _len(d)
            ux, uy = (d[0] / L, d[1] / L) if L > 1e-9 else (1.0, 0.0)
        else:
            ux, uy = 1.0, 0.0
            if out:
                sx = sum(pos[v][0] - pos[n][0] for _s, v in out)
                sy = sum(pos[v][1] - pos[n][1] for _s, v in out)
                L = math.hypot(sx, sy)
                if L > 1e-9:
                    ux, uy = sx / L, sy / L
        # symbol sits beyond the node, opposite the cable
        cx, cy = pos[n][0] - ux * SYMBOL_W / 2.0, pos[n][1] - uy * SYMBOL_W / 2.0
        nx, ny = -uy, ux
        hw, hh = SYMBOL_W / 2.0, SYMBOL_H / 2.0
        corners = [(cx + ux * a + nx * b, cy + uy * a + ny * b)
                   for a, b in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
        text = f"{c['ref']} {c['family_label']} {c['pins']}P"
        tw = _text_w(text)
        lx = pos[n][0] - ux * (SYMBOL_W + 1.5 + tw / 2.0)
        ly = pos[n][1] - uy * (SYMBOL_W + 1.5 + TEXT_H / 2.0 + 1.0)
        rect = (lx - tw / 2.0, ly - TEXT_H / 2.0, lx + tw / 2.0, ly + TEXT_H / 2.0)
        conns.append({"id": n, "ref": c["ref"], "label": text, "x": pos[n][0], "y": pos[n][1],
                      "dir": (ux, uy), "corners": corners, "label_rect": rect,
                      "label_xy": (rect[0], rect[1] + 0.8)})
    return segs, conns


def _place_labels(segs: list, conns: list) -> "tuple[list, list]":
    """Segment-length labels (greedy, deterministic) and the label overlaps found."""
    problems: list = []
    placed: list = [(c["label_rect"], f"connector {c['ref']}") for c in conns]
    for s in segs:
        text = f"{s['length_mm']:.1f}"
        w, hgt = _text_w(text), TEXT_H
        mx, my = (s["p0"][0] + s["p1"][0]) / 2.0, (s["p0"][1] + s["p1"][1]) / 2.0
        d = _sub(s["p1"], s["p0"])
        L = _len(d)
        ux, uy = (d[0] / L, d[1] / L) if L > 1e-9 else (1.0, 0.0)
        nx, ny = -uy, ux
        sup = abs(nx) * w / 2.0 + abs(ny) * hgt / 2.0
        chosen = None
        first = None
        for side in (1, -1):
            off = side * (s["half_width"] + LABEL_GAP_MM + sup)
            cx, cy = mx + nx * off, my + ny * off
            rect = (cx - w / 2.0, cy - hgt / 2.0, cx + w / 2.0, cy + hgt / 2.0)
            first = first or (rect, side)
            bad = False
            for o in segs:
                if _seg_rect_dist(o["p0"], o["p1"], rect) < o["half_width"] + LABEL_GAP_MM * 0.5:
                    bad = True
                    break
            if not bad:
                for r2, _n in placed:
                    if _rect_gap(rect, r2) < LABEL_GAP_MM * 0.5:
                        bad = True
                        break
            if not bad:
                for c in conns:
                    if _rect_polygon_gap(rect, c["corners"]) < LABEL_GAP_MM * 0.5:
                        bad = True
                        break
            if not bad:
                chosen = (rect, side)
                break
        if chosen is None:
            chosen = first
            problems.append({"kind": "label", "segment": s["id"],
                             "detail": f"length label of {s['id']} overlaps another item"})
        rect = chosen[0]
        s["label"] = {"text": text, "x": rect[0], "y": rect[1] + 0.8, "rect": rect}
        placed.append((rect, f"label {s['id']}"))
    return segs, problems


def _check(segs: list, conns: list, comp: _Comp) -> list:
    """Hard layout violations: overlapping bundles, tight sibling angles, connector clashes."""
    out: list = []
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            a, b = segs[i], segs[j]
            shared = {a["a"], a["b"]} & {b["a"], b["b"]}
            if shared:
                n = next(iter(shared))
                pn = a["p0"] if a["a"] == n else a["p1"]
                oa = a["p1"] if a["a"] == n else a["p0"]
                ob = b["p1"] if b["a"] == n else b["p0"]
                da, db = _sub(oa, pn), _sub(ob, pn)
                if _len(da) > 1e-9 and _len(db) > 1e-9:
                    cosv = (da[0] * db[0] + da[1] * db[1]) / (_len(da) * _len(db))
                    ang = math.degrees(math.acos(max(-1.0, min(1.0, cosv))))
                    if ang < MIN_BREAKOUT_DEG:
                        out.append({"kind": "adjacent_angle", "segments": [a["id"], b["id"]],
                                    "node": n, "angle_deg": M._r(ang, 2),
                                    "detail": f"{a['id']} and {b['id']} leave {n} only "
                                              f"{ang:.1f} deg apart"})
                continue
            dist = _seg_dist(a["p0"], a["p1"], b["p0"], b["p1"])
            need = a["half_width"] + b["half_width"] + GAP_MM
            if dist < need:
                out.append({"kind": "segment_overlap", "segments": [a["id"], b["id"]],
                            "distance_mm": M._r(dist, 3), "required_mm": M._r(need, 3),
                            "detail": f"{a['id']} and {b['id']} are {dist:.1f} mm apart "
                                      f"(need {need:.1f})"})
    R = _circle_R()
    for c in conns:
        for s in segs:
            if c["id"] in (s["a"], s["b"]):
                continue
            d = _pt_seg((c["x"], c["y"]), s["p0"], s["p1"])
            if d < R + s["half_width"] + GAP_MM:
                out.append({"kind": "connector_overlap", "connector": c["ref"],
                            "segments": [s["id"]], "distance_mm": M._r(d, 3),
                            "detail": f"connector {c['ref']} symbol is {d:.1f} mm from "
                                      f"segment {s['id']}"})
    # Connector geometry and labels must not silently collide with other drawing items.
    for i, c in enumerate(conns):
        rect = c["label_rect"]
        for s in segs:
            if _seg_rect_dist(s["p0"], s["p1"], rect) < s["half_width"]:
                out.append({"kind": "connector_label_overlap", "connector": c["ref"],
                            "segments": [s["id"]], "detail": "connector label overlaps a bundle"})
        for other in conns[i + 1:]:
            if _rect_gap(rect, other["label_rect"]) < LABEL_GAP_MM:
                out.append({"kind": "connector_label_overlap", "connectors": [c["ref"], other["ref"]],
                            "detail": "connector labels overlap"})
            edges1 = list(zip(c["corners"], c["corners"][1:] + c["corners"][:1]))
            edges2 = list(zip(other["corners"], other["corners"][1:] + other["corners"][:1]))
            if (min(_seg_dist(a, b, d, e) for a, b in edges1 for d, e in edges2) < GAP_MM
                    or _in_convex(c["corners"][0], other["corners"])
                    or _in_convex(other["corners"][0], c["corners"])):
                out.append({"kind": "connector_overlap", "connectors": [c["ref"], other["ref"]],
                            "detail": "connector symbols overlap or are too close"})
        for other in conns:
            if other is c:
                continue
            edges = list(zip(other["corners"], other["corners"][1:] + other["corners"][:1]))
            if (any(_seg_rect_dist(a, b, rect) < LABEL_GAP_MM for a, b in edges)
                    or _in_convex((rect[0], rect[1]), other["corners"])):
                out.append({"kind": "connector_label_overlap", "connectors": [c["ref"], other["ref"]],
                            "detail": "connector label overlaps another connector symbol"})
    return out


def _layout_component(comp: _Comp, h: dict, scale: float) -> dict:
    best = None
    for alpha in _ALPHAS:
        for fan in _FANS:
            for side in _SIDES:
                pos, heading, breakouts = _place(comp, scale, alpha, fan, side)
                segs, conns = _build_items(comp, h, pos, scale)
                segs, label_problems = _place_labels(segs, conns)
                hard = _check(segs, conns, comp)
                score = (len(hard), len(label_problems))
                cand = {"pos": pos, "heading": heading, "breakouts": breakouts, "segments": segs,
                        "connectors": conns, "overlaps": hard + label_problems,
                        "params": {"alpha_deg": alpha, "fan_deg": fan, "first_side": side},
                        "score": score}
                if best is None or score < best["score"]:
                    best = cand
                if score == (0, 0):
                    return cand
    return best


# ----------------------------------------------------------------------------- tables

def _table(title: str, columns: list, rows: list, x: float, y_top: float) -> dict:
    cells = [[str(c) for c in columns]] + [[("" if v is None else str(v)) for v in r] for r in rows]
    ncol = len(columns)
    widths = [max(len(r[i]) for r in cells) * CHAR_W + 2 * PAD for i in range(ncol)]
    title_h = ROW_H
    total_w = max(sum(widths), _text_w(title) + 2 * PAD)
    total_h = title_h + ROW_H * len(cells)
    return {"title": title, "x": x, "y": y_top, "columns": [str(c) for c in columns],
            "rows": cells[1:], "col_w": widths, "row_h": ROW_H, "title_h": title_h,
            "w": total_w, "h": total_h}


def _tables(h: dict, report: dict, breakouts: list, x: float, y_top: float) -> "tuple[list, float]":
    tables: list = []
    y = y_top
    wire_rows = [[w["wire"], w["from"], w["to"], w["gauge"], w["color"] or "-",
                  w["signal_class"] or "-",
                  "-" if w["routed_length_mm"] is None else f"{w['routed_length_mm']:.1f}",
                  "-" if w["cut_length_mm"] is None else f"{w['cut_length_mm']:.1f}"]
                 for w in report["wires"]]
    t = _table("WIRE LIST (lengths mm; cut = routed + service loop + strip)",
               ["Wire", "From", "To", "Gauge", "Color", "Class", "Routed", "Cut"], wire_rows, x, y)
    tables.append(t)
    y -= t["h"] + 2 * TEXT_H
    for c in report["connectors"]:
        rows = [[p["pin"], p["signal"] or "-", p["wire"] or "-",
                 "-" if p["to_connector"] is None else f"{p['to_connector']}:{p['to_pin']}",
                 p["color"] or "-"] for p in c["pinout"]]
        t = _table(f"PINOUT {c['ref']} - {c['family']} {c['pins']}P", ["Pin", "Signal", "Wire", "To", "Color"],
                   rows, x, y)
        tables.append(t)
        y -= t["h"]
        rows = [[g["to"], w["wire"], w["pin"], w["to_pin"], w.get("signal") or "-"]
                for g in c["from_to"] for w in g["wires"]]
        t = _table(f"FROM-TO {c['ref']}", ["To", "Wire", "From pin", "To pin", "Signal"],
                   rows, x, y)
        tables.append(t)
        y -= t["h"] + 2 * TEXT_H
    clamps = [n for n in h["nodes"] if n["kind"] == "clamp"]
    rows = []
    for n in sorted(clamps, key=lambda n: n["id"]):
        at = [s for s in h["segments"] if n["id"] in (s["a"], s["b"])]
        rows.append([n["id"], ", ".join(f"{s['id']}={M._r(M.seg_length(s), 1)}" for s in at)])
    t = _table("CLAMPS (position = adjacent segment lengths, mm)", ["Clamp", "Segments"], rows, x, y)
    tables.append(t)
    y -= t["h"] + 2 * TEXT_H
    rows = [[b["node"], br["segment"], f"{br['angle_deg']:+.1f}"]
            for b in breakouts for br in b["branches"]]
    t = _table("BREAKOUT ANGLES (degrees relative to inherited heading)",
               ["Node", "Segment", "Angle"], rows, x, y)
    tables.append(t)
    y -= t["h"] + 2 * TEXT_H
    return tables, y


# ----------------------------------------------------------------------------- public layout

def layout_formboard(harness: dict, scale: Optional[float] = None, max_sheet_mm: float = 1500.0) -> dict:
    """Flatten ``harness`` (not modified) into a formboard layout dict.

    ``scale`` is sheet-mm per real-mm (1.0 = full size). ``None`` = 1.0, reduced only when the
    drawing would exceed ``max_sheet_mm``. Raises :class:`HarnessError` when nothing is routed
    or the segment graph has a loop.
    """
    try:
        max_sheet_mm = float(max_sheet_mm)
    except (TypeError, ValueError):
        raise M.HarnessError("bad_sheet_size", "max_sheet_mm must be a positive finite number") from None
    if not math.isfinite(max_sheet_mm) or max_sheet_mm <= 0:
        raise M.HarnessError("bad_sheet_size", "max_sheet_mm must be a positive finite number")
    if scale is not None:
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            raise M.HarnessError("bad_scale", "scale must be a positive finite number") from None
        if not math.isfinite(scale) or scale <= 0:
            raise M.HarnessError("bad_scale", f"scale must be a positive number; got {scale!r}")
    h = copy.deepcopy(harness)
    # Reject ambiguous routes before deriving wire membership using a graph path search.
    _components(h["segments"])
    M.recompute_derived(h)
    for s in h["segments"]:
        if s.get("wires") and M.seg_length(s) <= 1e-9:
            raise M.HarnessError("zero_length_segment", f"routed segment {s['id']} has zero length")
    segs, _nodes = _active(h)
    if not segs:
        raise M.HarnessError("nothing_to_lay_out", "no routed segment carries a wire",
                             hint="harness_route first (wires without a route have no length)")
    comps = [_Comp(g) for g in _components(segs)]
    unknown_od = [s["id"] for s in segs if s.get("od_mm") is None]
    notes: list = []
    if unknown_od:
        notes.append("Bundle OD is unknown for segments " + ", ".join(unknown_od)
                     + "; drawn widths are placeholders and clearance is unverified. "
                       "Supply a design packing factor for bundles of three or more wires.")
    s_used = 1.0 if scale is None else scale
    for _attempt in range(2):
        placed = _stack(comps, h, s_used)
        extent = max(placed["bbox"][2] - placed["bbox"][0], placed["bbox"][3] - placed["bbox"][1])
        if scale is None and extent > max_sheet_mm and _attempt == 0:
            s_used = max(1e-3, max_sheet_mm / extent) * (s_used if extent else 1.0)
            notes.append(f"scale reduced to {s_used:.3f} to reduce the drawing extent toward {max_sheet_mm:g} mm")
            continue
        break
    report = M.build_report(h)
    tx = placed["bbox"][0]
    ty = placed["bbox"][1] - 4 * TEXT_H
    tables, y_end = _tables(h, report, placed["breakouts"], tx, ty)
    xmin, ymin, xmax, ymax = placed["bbox"]
    for t in tables:
        xmax = max(xmax, t["x"] + t["w"])
        ymin = min(ymin, y_end if t is tables[-1] else t["y"] - t["h"])
    ymin = min(ymin, y_end)
    title = f"HARNESS {h['name']} ({h['id']}) - FORMBOARD scale {s_used:g}:1 - dimensions mm, true 3D bundle lengths"
    if unknown_od:
        title = "INCOMPLETE - UNKNOWN BUNDLE OD / CLEARANCE UNVERIFIED - " + title
    xmax = max(xmax, tx + _text_w(title))
    if max(xmax - xmin, max(ymax, placed["bbox"][3] + 4 * TEXT_H) - ymin) + 2 * MARGIN > max_sheet_mm:
        notes.append(f"complete sheet exceeds {max_sheet_mm:g} mm, including tables and margins")
    max_err = 0.0
    for s in placed["segments"]:
        max_err = max(max_err, abs(s["drawn_length_mm"] - s["length_mm"]))
    overlaps = placed["overlaps"]
    return {
        "schema": SCHEMA, "harness_id": h["id"], "name": h["name"], "scale": s_used,
        "title": title, "title_xy": (tx, placed["bbox"][3] + 2 * TEXT_H),
        "components": placed["components"], "nodes": {k: list(v) for k, v in placed["nodes"].items()},
        "segments": placed["segments"], "connectors": placed["connectors"],
        "clamps": placed["clamps"], "breakouts": placed["breakouts"], "tables": tables,
        "bbox": [xmin, ymin, xmax, max(ymax, placed["bbox"][3] + 4 * TEXT_H)],
        "overlaps": overlaps, "unrouted_wires": report["unrouted_wires"],
        "unknown_od_segments": unknown_od,
        "ok": not overlaps and not report["unrouted_wires"] and not unknown_od,
        "verification": {"max_length_error_mm": M._r(max_err, 6),
                         "segments_checked": len(placed["segments"]),
                         "rule": "drawn length / scale == true 3D segment length"},
        "warnings": notes + report["warnings"],
    }


def _stack(comps: list, h: dict, scale: float) -> dict:
    """Lay every component out and stack them vertically with a 12 mm gutter."""
    all_segs: list = []
    all_conns: list = []
    nodes: dict = {}
    breakouts: list = []
    overlaps: list = []
    comp_meta: list = []
    clamps: list = []
    kinds = {n["id"]: n["kind"] for n in h["nodes"]}
    y_cursor = 0.0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for idx, comp in enumerate(comps):
        cand = _layout_component(comp, h, scale)
        pts: list = []
        for s in cand["segments"]:
            pts += [s["p0"], s["p1"]]
            r = s["label"]["rect"]
            pts += [(r[0], r[1]), (r[2], r[3])]
        for c in cand["connectors"]:
            pts += c["corners"]
            r = c["label_rect"]
            pts += [(r[0], r[1]), (r[2], r[3])]
        bx0 = min(p[0] for p in pts)
        by0 = min(p[1] for p in pts)
        by1 = max(p[1] for p in pts)
        dx, dy = -bx0, y_cursor - by0
        sh = lambda p: (p[0] + dx, p[1] + dy)  # noqa: E731
        shr = lambda r: (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)  # noqa: E731
        for s in cand["segments"]:
            s["p0"], s["p1"] = sh(s["p0"]), sh(s["p1"])
            s["label"]["x"] += dx
            s["label"]["y"] += dy
            s["label"]["rect"] = shr(s["label"]["rect"])
            s["component"] = idx
        for c in cand["connectors"]:
            c["x"] += dx
            c["y"] += dy
            c["corners"] = [sh(p) for p in c["corners"]]
            c["label_rect"] = shr(c["label_rect"])
            c["label_xy"] = (c["label_xy"][0] + dx, c["label_xy"][1] + dy)
            c["component"] = idx
        for n, p in cand["pos"].items():
            q = sh(p)
            nodes[n] = q
            if kinds.get(n) == "clamp":
                clamps.append({"node": n, "x": q[0], "y": q[1]})
        for b in cand["breakouts"]:
            b["at"] = list(nodes[b["node"]])
            breakouts.append(b)
        all_segs += cand["segments"]
        all_conns += cand["connectors"]
        overlaps += [dict(o, component=idx) for o in cand["overlaps"]]
        comp_meta.append({"index": idx, "params": cand["params"], "trunk": comp.trunk_segs,
                          "nodes": comp.nodes})
        height = by1 - by0
        for s in cand["segments"]:
            xmin = min(xmin, s["p0"][0], s["p1"][0], s["label"]["rect"][0])
            xmax = max(xmax, s["p0"][0], s["p1"][0], s["label"]["rect"][2])
        for c in cand["connectors"]:
            xmin = min(xmin, *(p[0] for p in c["corners"]), c["label_rect"][0])
            xmax = max(xmax, *(p[0] for p in c["corners"]), c["label_rect"][2])
        ymin = min(ymin, y_cursor)
        ymax = max(ymax, y_cursor + height)
        y_cursor += height + 12.0 + 4 * TEXT_H
    return {"segments": all_segs, "connectors": all_conns, "nodes": nodes,
            "breakouts": breakouts, "overlaps": overlaps, "components": comp_meta,
            "clamps": clamps, "bbox": [xmin, ymin, xmax, ymax]}


# ----------------------------------------------------------------------------- renderers

def _mapper(layout: dict) -> "tuple[Any, Any, float, float]":
    x0, y0, x1, y1 = layout["bbox"]
    W = (x1 - x0) + 2 * MARGIN
    H = (y1 - y0) + 2 * MARGIN
    return (lambda x: x - x0 + MARGIN), (lambda y: y1 - y + MARGIN), W, H


def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def render_svg(layout: dict) -> str:
    """SVG text; one ``<line data-segment=.. data-length-mm=..>`` per bundle, units mm."""
    fx, fy, W, H = _mapper(layout)
    out: list = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(W)}mm" height="{_f(H)}mm" '
        f'viewBox="0 0 {_f(W)} {_f(H)}" font-family="monospace" font-size="{_f(TEXT_H)}">',
        f'<title>{escape(layout["title"])}</title>',
        f'<rect x="0" y="0" width="{_f(W)}" height="{_f(H)}" fill="#ffffff"/>',
        f'<text x="{_f(fx(layout["title_xy"][0]))}" y="{_f(fy(layout["title_xy"][1]))}" '
        f'font-weight="bold">{escape(layout["title"])}</text>',
    ]
    for s in layout["segments"]:
        out.append(
            f'<line data-segment="{escape(s["id"], {chr(34): "&quot;"})}" data-length-mm="{_f(s["length_mm"])}" '
            f'x1="{_f(fx(s["p0"][0]))}" y1="{_f(fy(s["p0"][1]))}" x2="{_f(fx(s["p1"][0]))}" '
            f'y2="{_f(fy(s["p1"][1]))}" stroke="#202020" stroke-width="{_f(2 * s["half_width"])}" '
            f'stroke-linecap="butt"/>')
        lb = s["label"]
        out.append(f'<text x="{_f(fx(lb["x"]))}" y="{_f(fy(lb["y"]))}">{escape(lb["text"])}</text>')
    for c in layout["connectors"]:
        pts = " ".join(f"{_f(fx(p[0]))},{_f(fy(p[1]))}" for p in c["corners"])
        out.append(f'<polygon data-connector="{escape(c["id"], {chr(34): "&quot;"})}" points="{pts}" fill="#dde6f5" '
                   f'stroke="#202020" stroke-width="0.4"/>')
        out.append(f'<text x="{_f(fx(c["label_xy"][0]))}" y="{_f(fy(c["label_xy"][1]))}">'
                   f'{escape(c["label"])}</text>')
    for n in layout["clamps"]:
        out.append(f'<circle data-clamp="{escape(n["node"], {chr(34): "&quot;"})}" cx="{_f(fx(n["x"]))}" '
                   f'cy="{_f(fy(n["y"]))}" r="1.5" fill="#ffffff" stroke="#202020" stroke-width="0.4"/>')
        out.append(f'<text x="{_f(fx(n["x"]) + 2)}" y="{_f(fy(n["y"]) - 2)}">{escape(n["node"])}</text>')
    for t in layout["tables"]:
        _svg_table(out, t, fx, fy)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _svg_table(out: list, t: dict, fx, fy) -> None:
    x, y = t["x"], t["y"]
    out.append(f'<text x="{_f(fx(x))}" y="{_f(fy(y - t["title_h"] * 0.7))}" font-weight="bold">'
               f'{escape(t["title"])}</text>')
    if not t["columns"]:
        return
    ytop = y - t["title_h"]
    out.append(f'<rect x="{_f(fx(x))}" y="{_f(fy(ytop))}" width="{_f(sum(t["col_w"]))}" '
               f'height="{_f(t["row_h"] * (len(t["rows"]) + 1))}" fill="none" stroke="#202020" '
               f'stroke-width="0.3"/>')
    for ri, row in enumerate([t["columns"]] + t["rows"]):
        ry = ytop - ri * t["row_h"]
        cx = x
        for ci, cell in enumerate(row):
            weight = ' font-weight="bold"' if ri == 0 else ""
            out.append(f'<text x="{_f(fx(cx + PAD))}" y="{_f(fy(ry - t["row_h"] * 0.68))}"'
                       f'{weight}>{escape(cell)}</text>')
            cx += t["col_w"][ci]
        out.append(f'<line x1="{_f(fx(x))}" y1="{_f(fy(ry - t["row_h"]))}" '
                   f'x2="{_f(fx(x + sum(t["col_w"])))}" y2="{_f(fy(ry - t["row_h"]))}" '
                   f'stroke="#202020" stroke-width="0.2"/>')
    for a, b in _table_grid(t):
        if a[0] == b[0]:
            out.append(f'<line x1="{_f(fx(a[0]))}" y1="{_f(fy(a[1]))}" '
                       f'x2="{_f(fx(b[0]))}" y2="{_f(fy(b[1]))}" stroke="#202020" stroke-width="0.2"/>')


def _table_grid(t: dict) -> list:
    x, top = t["x"], t["y"] - t["title_h"]
    bottom = top - t["row_h"] * (len(t["rows"]) + 1)
    lines = []
    for width in t["col_w"][:-1]:
        x += width
        lines.append(((x, top), (x, bottom)))
    for row in range(1, len(t["rows"]) + 1):
        y = top - row * t["row_h"]
        lines.append(((t["x"], y), (t["x"] + sum(t["col_w"]), y)))
    return lines


def _table_texts(t: dict) -> list:
    """``[(x, y_baseline, text, bold)]`` of a table, y up."""
    out = [(t["x"], t["y"] - t["title_h"] * 0.7, t["title"], True)]
    if not t["columns"]:
        return out
    ytop = t["y"] - t["title_h"]
    for ri, row in enumerate([t["columns"]] + t["rows"]):
        ry = ytop - ri * t["row_h"]
        cx = t["x"]
        for ci, cell in enumerate(row):
            out.append((cx + PAD, ry - t["row_h"] * 0.68, cell, ri == 0))
            cx += t["col_w"][ci]
    return out


def write_dxf(layout: dict, path: str) -> str:
    """Write an R2010 DXF (mm). Layers: HARNESS_BUNDLE / _CONNECTOR / _CLAMP / _TEXT / _TABLE."""
    try:
        import ezdxf  # noqa: PLC0415 — declared dependency (requirements.txt)
    except ImportError as exc:
        raise M.HarnessError("dxf_unavailable", "ezdxf is not installed in this runtime",
                             hint="use format='svg' or 'pdf', or install ezdxf") from exc
    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM
    for name, color in (("HARNESS_BUNDLE", 7), ("HARNESS_CONNECTOR", 5), ("HARNESS_CLAMP", 3),
                        ("HARNESS_TEXT", 7), ("HARNESS_TABLE", 8)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()

    def text(x: float, y: float, s: str, layer: str) -> None:
        msp.add_text(s, height=TEXT_H, dxfattribs={"layer": layer}).set_placement((x, y))

    for s in layout["segments"]:
        msp.add_lwpolyline([tuple(s["p0"]), tuple(s["p1"])],
                           dxfattribs={"layer": "HARNESS_BUNDLE", "const_width": 2 * s["half_width"]})
        text(s["label"]["x"], s["label"]["y"], s["label"]["text"], "HARNESS_TEXT")
    for c in layout["connectors"]:
        msp.add_lwpolyline([tuple(p) for p in c["corners"]], close=True,
                           dxfattribs={"layer": "HARNESS_CONNECTOR"})
        text(c["label_xy"][0], c["label_xy"][1], c["label"], "HARNESS_TEXT")
    for n in layout["clamps"]:
        msp.add_circle((n["x"], n["y"]), 1.5, dxfattribs={"layer": "HARNESS_CLAMP"})
        text(n["x"] + 2.0, n["y"] + 2.0, n["node"], "HARNESS_TEXT")
    text(layout["title_xy"][0], layout["title_xy"][1], layout["title"], "HARNESS_TEXT")
    for t in layout["tables"]:
        for x, y, s, _bold in _table_texts(t):
            text(x, y, s, "HARNESS_TABLE")
        if t["columns"]:
            ytop = t["y"] - t["title_h"]
            w = sum(t["col_w"])
            hgt = t["row_h"] * (len(t["rows"]) + 1)
            msp.add_lwpolyline([(t["x"], ytop), (t["x"] + w, ytop), (t["x"] + w, ytop - hgt),
                                (t["x"], ytop - hgt)], close=True, dxfattribs={"layer": "HARNESS_TABLE"})
            for a, b in _table_grid(t):
                msp.add_line(a, b, dxfattribs={"layer": "HARNESS_TABLE"})
    doc.saveas(path)
    return path


def write_pdf(layout: dict, path: str) -> str:
    """Write a one-page PDF whose page size equals the sheet (mm)."""
    try:
        from reportlab.pdfgen import canvas  # noqa: PLC0415 — declared dependency
    except ImportError as exc:
        raise M.HarnessError("pdf_unavailable", "reportlab is not installed in this runtime",
                             hint="use format='svg' or 'dxf'") from exc
    pt = 72.0 / 25.4
    x0, y0, x1, y1 = layout["bbox"]
    W, H = (x1 - x0) + 2 * MARGIN, (y1 - y0) + 2 * MARGIN
    if max(W, H) * pt > 14000:
        raise M.HarnessError("sheet_too_large", f"sheet {W:.0f} x {H:.0f} mm exceeds the PDF page limit",
                             hint="pass a smaller scale (e.g. scale=0.5)")
    c = canvas.Canvas(path, pagesize=(W * pt, H * pt), invariant=1)
    X = lambda x: (x - x0 + MARGIN) * pt  # noqa: E731
    Y = lambda y: (y - y0 + MARGIN) * pt  # noqa: E731
    c.setLineCap(0)
    for s in layout["segments"]:
        c.setLineWidth(2 * s["half_width"] * pt)
        c.line(X(s["p0"][0]), Y(s["p0"][1]), X(s["p1"][0]), Y(s["p1"][1]))
    c.setLineWidth(0.4 * pt)
    for cn in layout["connectors"]:
        p = c.beginPath()
        pts = cn["corners"]
        p.moveTo(X(pts[0][0]), Y(pts[0][1]))
        for q in pts[1:]:
            p.lineTo(X(q[0]), Y(q[1]))
        p.close()
        c.drawPath(p, stroke=1, fill=0)
    for n in layout["clamps"]:
        c.circle(X(n["x"]), Y(n["y"]), 1.5 * pt, stroke=1, fill=0)
    c.setFont("Courier", TEXT_H * pt)
    texts = [(s["label"]["x"], s["label"]["y"], s["label"]["text"]) for s in layout["segments"]]
    texts += [(cn["label_xy"][0], cn["label_xy"][1], cn["label"]) for cn in layout["connectors"]]
    texts += [(n["x"] + 2.0, n["y"] + 2.0, n["node"]) for n in layout["clamps"]]
    texts.append((layout["title_xy"][0], layout["title_xy"][1], layout["title"]))
    for t in layout["tables"]:
        texts += [(x, y, s) for x, y, s, _b in _table_texts(t)]
        if t["columns"]:
            ytop = t["y"] - t["title_h"]
            c.rect(X(t["x"]), Y(ytop - t["row_h"] * (len(t["rows"]) + 1)), sum(t["col_w"]) * pt,
                   t["row_h"] * (len(t["rows"]) + 1) * pt, stroke=1, fill=0)
            for a, b in _table_grid(t):
                c.line(X(a[0]), Y(a[1]), X(b[0]), Y(b[1]))
    for x, y, s in texts:
        c.drawString(X(x), Y(y), s)
    c.showPage()
    c.save()
    return path


def overlap_count(layout: dict) -> int:
    return len(layout.get("overlaps") or [])
