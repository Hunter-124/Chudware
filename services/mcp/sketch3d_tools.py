"""3D-sketch + sketch-preview MCP tools — ENGINE-BACKED (doc feature `sketch3d`).

These tools drive the REAL engine `sketch3d` feature (doc_schema §3B, contract:
contracts/sketch3d_gui_contract.md).  A 3D sketch is an ordinary feature in the part's
history — world-mm free-space entities (point / line / 3-point arc / spline), a constraint
solver (coincident, fix, distance, length, parallel, perpendicular, angle, along_axis,
point_on), and compiled 3D wire chains that a downstream `sweep` consumes as `path` or a
`guides[]` entry.  Persistence comes free with the document (save/undo/redo/PLM).

    sketch3d_new(name)                    create a real sketch3d FEATURE in the active doc
    sketch3d_add_curves(id, curves)       append entities (edit_feature) + solve report
    sketch3d_add_constraints(id, cons)    append constraints + narrated solve results
    check_sketch3d(id)                    narrated health (status/dof/conflicts/free vars)
    sketch3d_get(id)                      authored params + SOLVED entities/wires/dof
    sketch3d_list()                       all sketch3d features in the doc
    sketch3d_clear(id)                    empty the feature (keeps the feature id)
    sketch3d_preview(id)                  no-vision TEXT read: entities, wires, health
    sketch3d_path_from_points(...)        one-call "make me a routing path"
    sketch3d_pipe(id, diameter_mm=..)     one-call "run a tube along this path" (profile+sweep)
    sketch3d_as_sweep_guide(id, role)     the exact sweep path/guides ref syntax for this sketch
    sketch3d_sample_at(id, t)             point + tangent at fractional arc-length t (solved geom)
    sketch3d_section_at(id, t, doc_id)    origin-plane section oriented from the path tangent

    render_sketch / sketch_preview_text / doc_sketch_contact_sheet — 2D sketch preview
    helpers (unchanged; see their docstrings).

History: these tools used to speak a Python-only session store (points + segments, no
solver).  They were re-pointed to the engine feature when `sketch3d` landed
(core/sketch/sketch3d.cpp, sketch3d_health/sketch3d_drag RPCs).  Tool names and rough
signatures were kept stable; `sketch3d_id` now means the doc FEATURE id (e.g. "f3").

Engine RPCs used: doc_apply_ops (add/edit_feature), doc_tree, sketch3d_health,
eval_section_props, eval_mass, doc_render_* for previews.

Registered onto the FastMCP instance by register_sketch3d_tools(mcp).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from .core_bridge import CoreError
from .cad_tools import _doc, _dumps, _err
from .cad_common import coerce_json_or_repr, require_list, require_mapping


# ───────────────────────────────────────── doc helpers ───────────────────────────────────────────

def _tree_features(doc_id: str) -> list:
    tree = cb.call("doc_tree", {"doc_id": doc_id})
    return ((tree.get("tree") or {}).get("features")) or []


def _find_sketch3d(doc_id: str, fid: str) -> dict:
    """Return the sketch3d feature dict for `fid`, or raise a helpful ValueError."""
    s3d_ids = []
    for f in _tree_features(doc_id):
        if not isinstance(f, dict):
            continue
        if f.get("type") == "sketch3d":
            if f.get("id") == fid:
                return f
            s3d_ids.append(f.get("id"))
    raise ValueError(
        f"No sketch3d feature {fid!r} in the doc. "
        f"Existing sketch3d features: {s3d_ids or '(none — call sketch3d_new first)'}"
    )


def _health(doc_id: str, fid: str) -> dict:
    return cb.call("sketch3d_health", {"doc_id": doc_id, "sketch": fid})


def _apply_ops(doc_id: str, ops: list) -> dict:
    """Apply an atomic PART batch or revision-guard its fallback rollback."""
    ckpt = None
    try:
        ckpt = cb.call("doc_checkpoint", {"doc_id": doc_id})
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        ckpt = None
    r = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    ok = all(s.get("ok") for s in fs) if fs else True
    if not ok:
        reverted = bool(r.get("rejected_tree"))
        if not reverted:
            reverted = cb.revert_own_batch(doc_id, ckpt, r.get("revision"))
        fails = [{"id": s.get("id"), "error": s.get("error")} for s in fs if not s.get("ok")]
        state = "doc rolled back" if reverted else "failed features left in the tree"
        raise ValueError(f"ops rejected ({state}): {json.dumps(fails)}")
    _mirror(doc_id)
    return r


def _mirror(doc_id: str) -> None:
    """Best-effort: mirror the doc to the running desktop app (same as add_features)."""
    try:
        from .cad_tools import _publish_current
        _publish_current(doc_id)
    except Exception:  # noqa: BLE001
        pass


def _next_id(entities: list, prefix: str) -> int:
    n = 0
    for e in entities or []:
        eid = str((e or {}).get("id", ""))
        if eid.startswith(prefix) and eid[len(prefix):].isdigit():
            n = max(n, int(eid[len(prefix):]))
    return n


def _dim(v: Any) -> dict:
    if isinstance(v, dict) and "value" in v:
        return {"value": float(v["value"]), "expr": v.get("expr")}
    return {"value": float(v), "expr": None}


# ─────────────────────────────────── curve-spec → entity mapping ──────────────────────────────────

def _coerce_point3(v: Any, ctx: str) -> list:
    """Coerce ONE world point to [x, y, z].

    Accepts a 3-element list/tuple OR an {"x":..,"y":..,"z":..} object — the exact shape a
    model sends when it treats a point as a named-field record instead of an array (proven
    live on the 2D sketch path, where an un-coerced point silently built at the origin
    instead of failing loud). A 2-key {"x","y"} object (no z) is genuinely ambiguous for a
    3D sketch, so it is rejected with a teaching error rather than guessing z=0.
    """
    v = coerce_json_or_repr(v)
    if isinstance(v, dict):
        if {"x", "y", "z"} <= v.keys():
            return [float(v["x"]), float(v["y"]), float(v["z"])]
        raise ValueError(f"{ctx} must be [x,y,z] or {{'x':..,'y':..,'z':..}} — got {v!r} "
                          f"(missing key(s) {sorted({'x', 'y', 'z'} - v.keys())})")
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return [float(v[0]), float(v[1]), float(v[2])]
    raise ValueError(f"{ctx} must be [x,y,z] — got {v!r}")


def _validate_pts(pts: Any, min_n: int = 1, name: str = "pts") -> list:
    # A stringified points list ('[[1,2,3],[4,5,6]]' or its Python-repr twin) is the same
    # dialect a stringified curves/entities list arrives in — coerce before the shape check.
    pts = coerce_json_or_repr(pts)
    if not isinstance(pts, list) or len(pts) < min_n:
        got = f"a list of {len(pts)}" if isinstance(pts, list) else type(pts).__name__
        raise ValueError(f"{name} must be a list of at least {min_n} [x,y,z] points — got "
                          f"{got}.")
    return [_coerce_point3(p, f"{name}[{i}]") for i, p in enumerate(pts)]


def _curves_to_entities(curves: list, start_n: int = 0) -> list:
    """Map curve specs to engine `sketch3d` entities (doc_schema §3B).

    Accepts TWO forms per element:
      • legacy curve spec {kind: line|polyline|spline|arc, pts: [[x,y,z],…]}
        (polyline expands to one line entity per consecutive pair; arc pts = [start,mid,end])
      • raw engine entity {id?, type: point|line|arc|spline, construction?, p/p1/p2/pm/points}
        (missing ids are auto-assigned e<N> continuing from the existing max)
    """
    entities: list = []
    n = start_n

    def _eid() -> str:
        nonlocal n
        n += 1
        return f"e{n}"

    # Raw-entity point fields keyed by type — used both to detect the raw-entity form
    # without a "type" key (see kind/type fallback below) and to coerce each point.
    _RAW_PT_FIELDS = {"line": ("p1", "p2"), "arc": ("p1", "pm", "p2"), "point": ("p",)}

    for i, spec in enumerate(curves):
        if not isinstance(spec, dict):
            raise ValueError(f"curves[{i}] must be a dict, got {type(spec).__name__}")
        raw_pt_hit = {"p1", "p2", "pm", "p", "points"} & spec.keys()
        if "type" not in spec and raw_pt_hit and "pts" not in spec and \
                spec.get("kind") in ("line", "arc", "spline", "point", "eq_curve3d"):
            # A raw engine entity that named its shape "kind" instead of "type" — the exact
            # kind/type confusion already seen live on the 2D sketch path. Unambiguous here
            # because raw point fields (p1/p2/pm/p/points), not "pts", are present.
            spec = dict(spec)
            spec["type"] = spec.pop("kind")
        if "type" in spec:  # raw engine entity
            ent = dict(spec)
            ent.setdefault("construction", False)
            etype = ent.get("type")
            for field in _RAW_PT_FIELDS.get(etype, ()):
                if field in ent:
                    ent[field] = _coerce_point3(ent[field], f"curves[{i}].{field}")
            if etype == "spline" and "points" in ent:
                raw_points = coerce_json_or_repr(ent["points"])
                if not isinstance(raw_points, list):
                    raise ValueError(f"curves[{i}].points must be a list of [x,y,z] points "
                                      f"— got {type(raw_points).__name__}.")
                ent["points"] = [_coerce_point3(p, f"curves[{i}].points[{j}]")
                                 for j, p in enumerate(raw_points)]
            if not ent.get("id"):
                ent["id"] = _eid()
            else:
                m = str(ent["id"])
                if m.startswith("e") and m[1:].isdigit():
                    n = max(n, int(m[1:]))
            entities.append(ent)
            continue
        kind = spec.get("kind", "line")
        cons = bool(spec.get("construction", False) or
                    (spec.get("meta") or {}).get("construction", False))
        if kind == "line":
            pts = _validate_pts(spec.get("pts", []), 2)
            if len(pts) != 2:
                raise ValueError(f"curves[{i}]: a 'line' needs exactly 2 points; got {len(pts)}.")
            entities.append({"id": _eid(), "type": "line", "construction": cons,
                             "p1": pts[0], "p2": pts[1]})
        elif kind == "arc":
            pts = _validate_pts(spec.get("pts", []), 3)
            if len(pts) != 3:
                raise ValueError(
                    f"curves[{i}]: an 'arc' needs exactly 3 points [start, mid, end]; "
                    f"got {len(pts)}.")
            entities.append({"id": _eid(), "type": "arc", "construction": cons,
                             "p1": pts[0], "pm": pts[1], "p2": pts[2]})
        elif kind == "spline":
            pts = _validate_pts(spec.get("pts", []), 2)
            entities.append({"id": _eid(), "type": "spline", "construction": cons,
                             "points": pts})
        elif kind == "polyline":
            pts = _validate_pts(spec.get("pts", []), 2)
            for j in range(len(pts) - 1):
                entities.append({"id": _eid(), "type": "line", "construction": cons,
                                 "p1": pts[j], "p2": pts[j + 1]})
        elif kind == "point":
            pts = _validate_pts(spec.get("pts", []), 1)
            entities.append({"id": _eid(), "type": "point", "construction": cons,
                             "p": pts[0]})
        elif kind == "eq_curve3d":
            # Equation-driven 3D curve (doc_schema §3B): x(t)/y(t)/z(t) expression
            # strings over t in [t1, t2] (degrees trig, doc equations — §4). 0 DOF —
            # equation-fixed, like the 2D eq_curve; endpoints (p1@t1, p2@t2) are usable as
            # coincident/point_on targets. e.g. a helix: xt="R*cos(t)", yt="R*sin(t)",
            # zt="P*t/360", t1=0, t2=720 (two turns; R/P from doc equations or literals).
            for key in ("xt", "yt", "zt"):
                if not isinstance(spec.get(key), str):
                    raise ValueError(f"curves[{i}]: eq_curve3d needs a string '{key}' expression")
            entities.append({"id": _eid(), "type": "eq_curve3d", "construction": cons,
                             "xt": spec["xt"], "yt": spec["yt"], "zt": spec["zt"],
                             "t1": spec.get("t1", 0), "t2": spec.get("t2", 1)})
        else:
            raise ValueError(
                f"curves[{i}]: unknown kind {kind!r}. Use 'line', 'polyline', 'spline', "
                f"'arc', 'point', 'eq_curve3d' — or pass a raw engine entity with a 'type' "
                f"field.")
    return entities


# ─────────────────────────────── solved-geometry polyline sampling ────────────────────────────────

def _v_sub(a, b): return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
def _v_add(a, b): return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
def _v_scale(a, s): return [a[0] * s, a[1] * s, a[2] * s]
def _v_dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _v_cross(a, b): return [a[1] * b[2] - a[2] * b[1],
                            a[2] * b[0] - a[0] * b[2],
                            a[0] * b[1] - a[1] * b[0]]
def _v_norm(a): return math.sqrt(_v_dot(a, a))


def _arc_polyline(p1: list, pm: list, p2: list, n: int = 24) -> list:
    """Tessellate the 3-point arc (circumcircle through p1→pm→p2) into n+1 points.

    Falls back to the straight polyline [p1, pm, p2] when the points are (near) collinear —
    matching the engine, which skips the degenerate edge.
    """
    u = _v_sub(pm, p1)
    v = _v_sub(p2, p1)
    nrm = _v_cross(u, v)
    if _v_norm(nrm) < 1e-9 or _v_norm(u) < 1e-9 or _v_norm(v) < 1e-9:
        return [p1, pm, p2]
    # Orthonormal in-plane frame (e1, e2), normal e3.
    e1 = _v_scale(u, 1.0 / _v_norm(u))
    e3 = _v_scale(nrm, 1.0 / _v_norm(nrm))
    e2 = _v_cross(e3, e1)

    def to2d(p):
        d = _v_sub(p, p1)
        return (_v_dot(d, e1), _v_dot(d, e2))

    ax, ay = to2d(p1)          # (0, 0)
    bx, by = to2d(pm)
    cx, cy = to2d(p2)
    # 2D circumcenter.
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [p1, pm, p2]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)

    def ang(x, y):
        return math.atan2(y - uy, x - ux)

    a1, am, a2 = ang(ax, ay), ang(bx, by), ang(cx, cy)

    def ccw_span(f, t):  # ccw angle from f to t in [0, 2pi)
        return (t - f) % (2.0 * math.pi)

    # Go CCW from a1 to a2 if am lies on that span, else CW.
    if ccw_span(a1, am) <= ccw_span(a1, a2) + 1e-12:
        span = ccw_span(a1, a2)
    else:
        span = -((a1 - a2) % (2.0 * math.pi))
    pts = []
    for k in range(n + 1):
        th = a1 + span * (k / n)
        x = ux + r * math.cos(th)
        y = uy + r * math.sin(th)
        pts.append(_v_add(p1, _v_add(_v_scale(e1, x), _v_scale(e2, y))))
    return pts


def _entity_polyline(ent: dict) -> list:
    """Polyline approximation of one solved entity (authored order; [] for points)."""
    t = ent.get("type")
    if t == "line":
        return [list(ent["p1"]), list(ent["p2"])]
    if t == "arc":
        return _arc_polyline(list(ent["p1"]), list(ent["pm"]), list(ent["p2"]))
    if t == "spline":
        # Piecewise-linear through the interpolation points — an approximation of the
        # engine's cubic (fine for sampling/section orientation; noted in docstrings).
        return [list(p) for p in ent.get("points", [])]
    return []


def _sketch_polyline(entities: list) -> list:
    """Concatenated per-entity polylines (skip construction + point entities)."""
    chains = []
    for ent in entities or []:
        if ent.get("construction"):
            continue
        pl = _entity_polyline(ent)
        if len(pl) >= 2:
            chains.append(pl)
    return chains


def _polyline_length(chains: list) -> float:
    total = 0.0
    for pl in chains:
        for i in range(len(pl) - 1):
            total += _v_norm(_v_sub(pl[i + 1], pl[i]))
    return total


def _sample_polyline(chains: list, t: float) -> Optional[dict]:
    """Sample point + unit tangent at fractional arc-length t over concatenated chains."""
    total = _polyline_length(chains)
    if not chains:
        return None
    if total < 1e-9:
        return {"point": list(chains[0][0]), "tangent": [0.0, 0.0, 1.0],
                "total_length_mm": 0.0}
    t = max(0.0, min(1.0, float(t)))
    target = t * total
    walked = 0.0
    last_a, last_b = chains[0][0], chains[0][0]
    for pl in chains:
        for i in range(len(pl) - 1):
            a, b = pl[i], pl[i + 1]
            step = _v_norm(_v_sub(b, a))
            if step < 1e-12:
                continue
            last_a, last_b = a, b
            if walked + step >= target - 1e-9:
                lt = max(0.0, min(1.0, (target - walked) / step))
                point = _v_add(a, _v_scale(_v_sub(b, a), lt))
                tang = _v_scale(_v_sub(b, a), 1.0 / step)
                return {"point": point, "tangent": tang, "total_length_mm": total}
            walked += step
    step = _v_norm(_v_sub(last_b, last_a))
    tang = _v_scale(_v_sub(last_b, last_a), 1.0 / step) if step > 1e-12 else [0.0, 0.0, 1.0]
    return {"point": list(last_b), "tangent": tang, "total_length_mm": total}


def _bbox_of(chains: list) -> Optional[dict]:
    pts = [p for pl in chains for p in pl]
    if not pts:
        return None
    xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
    return {"min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)],
            "size": [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]}


# ─────────────────────────────────── health narration (2D-parity) ─────────────────────────────────

def _free_narration(free: list) -> str:
    """Group free variables per entity into plain words: 'e2: p2 x/y/z loose'."""
    by_ent: dict = {}
    for fvar in free or []:
        ent = fvar.get("entity", "?")
        by_ent.setdefault(ent, []).append(fvar.get("param", "?"))
    parts = []
    for ent, params in by_ent.items():
        # Collapse "p2 x, p2 y, p2 z" → "p2 x/y/z".
        by_pt: dict = {}
        for p in params:
            bits = str(p).rsplit(" ", 1)
            if len(bits) == 2:
                by_pt.setdefault(bits[0], []).append(bits[1])
            else:
                by_pt.setdefault(p, [])
        segs = [f"{pt} {'/'.join(axes)}" if axes else pt for pt, axes in by_pt.items()]
        parts.append(f"{ent}: {', '.join(segs)} loose")
    return "; ".join(parts)


def _health_read(h: dict) -> dict:
    """check_sketch-style narrated read of a sketch3d_health response."""
    status = h.get("status", "")
    dof = h.get("dof", 0)
    conflicting = h.get("conflicting") or []
    redundant = h.get("redundant") or []
    free_text = _free_narration(h.get("free"))
    converged = h.get("converged", True)
    wires = h.get("wires") or []
    if conflicting or status == "over":
        summary = (f"Over-defined: {len(conflicting)} conflicting constraint(s) "
                   f"{conflicting} — remove one to resolve the contradiction.")
        if not converged:
            summary += " (Solver did not converge.)"
    elif status == "full" or dof == 0:
        summary = "Fully defined (dof 0) — every entity is pinned by constraints."
    else:
        summary = (f"Under-defined: {dof} degree(s) of freedom remain — add constraints "
                   f"(fix / along_axis / length / distance / coincident / …) until dof is 0.")
        if free_text:
            summary += f" Free: {free_text}."
        if redundant:
            summary += f" (Also {len(redundant)} redundant: {redundant}.)"
    if wires:
        wire_txt = ", ".join(
            f"wire {i}: {w.get('n_edges', '?')} edge(s)"
            f"{' CLOSED' if w.get('closed') else ''}" for i, w in enumerate(wires))
        summary += f" Wires: {wire_txt}."
    else:
        summary += " No wire chains yet (points-only or construction-only sketch)."
    return {
        "status": status,
        "dof": dof,
        "fully_defined": (status == "full" or dof == 0) and not conflicting,
        "converged": converged,
        "conflicting": conflicting,
        "redundant": redundant,
        "free": h.get("free") or [],
        "free_narration": free_text,
        "entity_statuses": h.get("statuses") or {},
        "wires": wires,
        "summary": summary,
    }


def _preview_text(fid: str, name: str, h: dict) -> str:
    """Human-readable, no-vision description of a solved sketch3d."""
    lines = [f"3D sketch {fid!r} ('{name}')"]
    entities = h.get("entities") or []
    statuses = h.get("statuses") or {}
    for ent in entities:
        eid = ent.get("id", "?")
        t = ent.get("type", "?")
        st = statuses.get(eid, "?")
        cons = " (construction)" if ent.get("construction") else ""

        def fp(p):
            return f"({p[0]:.2f},{p[1]:.2f},{p[2]:.2f})"

        if t == "line":
            geo = f"{fp(ent['p1'])} → {fp(ent['p2'])}  len={_v_norm(_v_sub(ent['p2'], ent['p1'])):.2f} mm"
        elif t == "arc":
            geo = f"{fp(ent['p1'])} ⌒{fp(ent['pm'])}⌒ {fp(ent['p2'])}"
        elif t == "spline":
            pts = ent.get("points", [])
            geo = " → ".join(fp(p) for p in pts[:5]) + (" …" if len(pts) > 5 else "")
        elif t == "point":
            geo = fp(ent.get("p", [0, 0, 0]))
        else:
            geo = ""
        lines.append(f"  {eid} {t}{cons}: {geo}  [{st}]")
    read = _health_read(h)
    chains = _sketch_polyline(entities)
    lines.append(f"  Total length: {_polyline_length(chains):.2f} mm")
    bb = _bbox_of(chains)
    if bb:
        s = bb["size"]
        lines.append(f"  Bbox: {s[0]:.2f} x {s[1]:.2f} x {s[2]:.2f} mm")
    wires = read["wires"]
    if wires:
        for i, w in enumerate(wires):
            lines.append(f"  wire {i}: {w.get('n_edges')} edge(s)"
                         f"{', closed' if w.get('closed') else ', open'}"
                         f"  (ref: {{\"feature\": \"{fid}\", \"wire\": {i}}})")
    else:
        lines.append("  wires: none")
    lines.append(f"  Health: {read['summary']}")
    return "\n".join(lines)


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="chudware_s3d_")
    os.close(fd)
    return path


# ─────────────────────────────── pipe helper plane placement ─────────────────────────────────────

def _profile_plane_ops(start: list, tangent: list) -> tuple:
    """Ops placing a datum plane at the spine start, EXACTLY perpendicular to the tangent.

    The datum "plane" feature now has a "point_normal" method
    (arbitrary origin + normal, core/doc/document.cpp) — this used to guess the nearest
    reserved axis plane and warn past ~30° tilt (SolidWorks users hit real skewed starts,
    e.g. an angled boss). point_normal makes the profile plane EXACT for every tangent, so
    there is no longer an axis-dominant approximation or a tilt warning to surface.

    Returns (plane_op, uv_center, tilt_deg): the datum-plane add_feature op, the (u,v)
    coordinates of the spine start within that EXACT plane (always [0, 0] — the plane's
    origin IS the spine start), and tilt_deg (always 0.0 now — kept in the return shape so
    callers don't need to change).
    """
    plane_op = {"op": "add_feature", "feature": {
        "type": "plane", "name": "PipeProfilePlane",
        "params": {"method": "point_normal", "origin": list(start), "normal": list(tangent)}}}
    return plane_op, [0.0, 0.0], 0.0


# ───────────────────── sweep `mode` value guard (silent-fall-through class) ───────────────────────
#
# The engine never validates a sweep's `mode`. core/doc/document.cpp:4274-4304 reads it as
#     smode  = feat.value("mode", "boss")
#     smerge = feat.value("merge", true) && (smode=="boss" || smode=="add" || smode=="cut")
#     out    = (smode == "cut") ? cut(target, tool) : fuse_unify(target, tool)
# — four TESTS and no vocabulary check, so an unrecognised value simply fails every test:
# nothing merges, and the swept tube is committed as a second DISJOINT body. Measured on a
# 50 mm cube with a Ø12 spine driven straight through it (2026-07-29, this engine):
#     mode="cut"      ok=true   -5654.867 mm³   1 body    material removed, as asked
#     mode="remove"   ok=true   +9047.787 mm³   2 bodies  material ADDED
# "remove" is the plainest spelling of the intent, `boolean_mode='cut'` reconciles onto this
# very parameter, and the wrong answer arrives as a successful build. Hence the value is
# checked HERE — the tool is the only layer that can, and the failure direction is the
# dangerous one.
#
#: The four values document.cpp actually tests for. A canonical value is forwarded
#: BYTE-IDENTICAL: "add" stays "add" (it fuses exactly like "boss"), never rewritten.
_PIPE_MODES = ("boss", "add", "cut", "new")
#: Equivalent spellings a caller (or the argument-reconciliation affix rule) plausibly
#: writes. Kept consistent with cad_doc_tools._BOOLEAN_MODES / _BOOLEAN_NEW_BODY, which
#: map the same words for primitives, plus the pocket/protrusion words a sweep attracts.
_PIPE_MODE_ALIASES = {
    "union": "boss", "join": "boss", "fuse": "boss", "merge": "boss", "weld": "boss",
    "additive": "boss", "protrusion": "boss", "pad": "boss", "material_add": "boss",
    "subtract": "cut", "remove": "cut", "difference": "cut", "pocket": "cut",
    "subtractive": "cut", "minus": "cut", "cutout": "cut", "cut_out": "cut",
    "material_remove": "cut",
    "new_body": "new", "newbody": "new", "separate": "new", "standalone": "new",
    "disjoint": "new", "none": "new",
}
#: The vocabulary, named in the error the way ecad_simulate names its analyses.
_PIPE_MODE_VALUES = ("'boss' (default — fuse the tube into the current body); "
                     "'cut' (subtract it); 'new' (leave it as a separate body). "
                     "Also accepted: union|join|fuse|merge|weld|additive|protrusion|pad "
                     "-> boss; subtract|remove|difference|pocket|subtractive|minus|cutout "
                     "-> cut; new_body|separate|standalone|none -> new")


def _resolve_pipe_mode(mode: Any, merge: bool) -> str:
    """Validate a sweep `mode` VALUE, case/whitespace-insensitively.

    Returns the engine spelling (a canonical value passes through unchanged). Raises
    ValueError naming the whole vocabulary for anything else — an unknown value used to
    build a disjoint boss where the caller asked for a cut, reporting success.
    """
    raw = mode if isinstance(mode, str) else ("" if mode is None else str(mode))
    word = raw.strip().lower()
    canon = word if word in _PIPE_MODES else _PIPE_MODE_ALIASES.get(word)
    if canon is None:
        raise ValueError(
            f"unknown sweep mode {raw!r} — nothing was built. Use one of: "
            f"{_PIPE_MODE_VALUES}. An unrecognised mode does NOT error in the engine: it "
            f"silently leaves the tube as a separate body, ADDING material.")
    # merge=False switches the engine's boolean off entirely, so a "cut" would be committed
    # as a standalone +volume body (measured: +9047.787 mm³ where -5654.867 was asked for).
    # The two arguments contradict each other; say so instead of adding material.
    if canon == "cut" and not merge:
        raise ValueError(
            "mode='cut' with merge=False cannot remove material — the engine only subtracts "
            "a swept tool when it is merged into a body, so this would ADD the tube as a "
            "separate solid. Pass merge=True to cut, or mode='new' for a separate body.")
    return canon


# ──────────────────────────────────────────── registration ───────────────────────────────────────

def register_sketch3d_tools(mcp: FastMCP) -> None:
    """Attach 3D-sketch + sketch-preview tools to the FastMCP server."""

    # ──────────────────────────────────────── 3D sketch CRUD ─────────────────────────────────────

    @mcp.tool()
    def sketch3d_new(name: str, doc_id: Optional[str] = None) -> str:
        """Create a NEW `sketch3d` FEATURE in the part document and return its feature id.

        A 3D sketch is a real, persistent doc feature (doc_schema §3B): free-space entities
        (point / line / 3-point arc / spline) in world millimetres — no work plane — with a
        constraint solver.  It never produces a body; it compiles to 3D wire chains that a
        downstream `sweep` consumes as `path` (spine) or a `guides[]` entry.  It saves with
        the document and participates in undo/redo like any feature.

        name: a human-readable label (e.g. "coolant_route", "handle_path").
        doc_id: part document (default: current part — call new_part first).

        Returns {ok, id, name, doc_id} — use ``id`` (a feature id like "f3") in every other
        sketch3d_* tool.  Populate it with sketch3d_add_curves or, for a simple path, prefer
        the one-call sketch3d_path_from_points.
        """
        try:
            d = _doc(doc_id)
            r = _apply_ops(d, [{"op": "add_feature", "feature": {
                "type": "sketch3d", "name": (name or "").strip() or "3DSketch",
                "params": {"entities": [], "constraints": []}}}])
            fid = (r.get("id_map") or {}).get("$0")
            return _dumps({"ok": True, "id": fid, "name": name, "doc_id": d,
                           "hint": "now call sketch3d_add_curves (or use "
                                   "sketch3d_path_from_points for a one-call path)"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_add_curves(sketch3d_id: str, curves: list,
                            doc_id: Optional[str] = None) -> str:
        """Append curve entities to a sketch3d feature (edit_feature) and report the solve.

        sketch3d_id: the FEATURE id returned by sketch3d_new (e.g. "f3").
        curves: a list where each element is EITHER
          • a curve spec {kind, pts, construction?}:
              kind: 'line' (2 pts) | 'polyline' (2+ pts → line per pair) |
                    'spline' (2+ through-points, cubic) | 'arc' (3 pts [start, mid, end] —
                    the engine arc is THREE-POINT: circumcircle through them) |
                    'point' (1 pt — anchor/constraint target, never joins a wire) |
                    'eq_curve3d' (xt/yt/zt expression strings + t1/t2, no pts — equation-
                    driven curve, e.g. a helix; see doc_schema §3B)
              pts: [[x,y,z], ...] in mm, WORLD space (not used by eq_curve3d)
          • or a raw engine entity {type, p1/p2/pm/p/points, construction?, id?}
            (doc_schema §3B; missing ids auto-assigned).

        Entities whose endpoints touch (tol 1e-4 mm) chain into wires automatically.
        Returns {ok, id, added, entity_count, health} where health is the narrated solve
        read (status/dof/wires) — same shape as check_sketch3d.
        """
        try:
            d = _doc(doc_id)
            feat = _find_sketch3d(d, sketch3d_id)
            params = feat.get("params") or {}
            entities = list(params.get("entities") or [])
            # A model-stringified curves list (JSON or Python-repr) is the same dialect
            # cad_common.require_list already teaches elsewhere — coerce before the shape
            # check so this fails on genuine garbage, not on a well-formed batch that just
            # arrived as text (a bare isinstance(list) check would silently reject it).
            curves = require_list(curves, "curves",
                                   'a list of curve specs, e.g. '
                                   '[{"kind":"line","pts":[[x,y,z],[x,y,z]]}]')
            if not curves:
                return _dumps({"ok": False, "error": "curves must be a non-empty list"})
            new_ents = _curves_to_entities(curves, start_n=_next_id(entities, "e"))
            entities.extend(new_ents)
            _apply_ops(d, [{"op": "edit_feature", "id": sketch3d_id,
                            "params": {"entities": entities}}])
            read = _health_read(_health(d, sketch3d_id))
            return _dumps({"ok": True, "id": sketch3d_id, "doc_id": d,
                           "added": len(new_ents), "entity_count": len(entities),
                           "new_entity_ids": [e["id"] for e in new_ents],
                           "health": read})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_add_constraints(sketch3d_id: str, constraints: list,
                                 doc_id: Optional[str] = None) -> str:
        """Add constraints to a sketch3d and return the narrated solve results.

        constraints: list of constraint records (doc_schema §3B); `id` ("c<N>") is optional
        (auto-assigned). Relations and dimensions:
          {type:'coincident',  refs:[pointH, pointH]}                pull two points together
          {type:'fix',         refs:[handle or 'e<N>']}             pin at authored coords
          {type:'distance',    refs:[pointH, pointH|lineE], value: mm}
          {type:'length',      refs:[lineE], value: mm}
          {type:'parallel',    refs:[lineE, lineE]}
          {type:'perpendicular', refs:[lineE, lineE]}
          {type:'angle',       refs:[lineE, lineE], value: deg}
          {type:'along_axis',  refs:[lineE], axis:'x'|'y'|'z'}  (or dir:[x,y,z])
          {type:'point_on',    refs:[pointH, lineE|arcE|splineE]}
          {type:'tangent'|'equal'|'collinear'|'concentric'|'coradial', refs:[curveE,curveE]}
          {type:'midpoint', refs:[pointH,lineE|arcE]}
          {type:'on_plane', refs:[pointH], plane:'front'|'top'|'right'}
          {type:'parallel_plane'|'normal_plane', refs:[lineE], plane:'top'}
          {type:'symmetric', refs:[pointH,pointH], plane:'right'}
        plane may also be {origin:[x,y,z],normal:[x,y,z]}. Point handles:
        'e3.p1'/'e3.p2' (line/arc ends), 'e3.pm' (arc through-point),
        'e3.p0'..'e3.pN' (spline points), 'e3.p' (point entity), or an x<N> model-vertex
        reference from sketch3d_ref_vertex. `value` is a number or {value,expr};
        driven:true measures instead of driving. Prefer relations + driving dimensions
        over fix-pinning authored coordinates; fix is appropriate for external references.

        Returns {ok, id, added, health} — health mirrors check_sketch's narrated read:
        status full/under/over, dof, conflicting/redundant constraint ids, and the remaining
        free variables in plain words (e.g. "e2: p2 x/y/z loose").  Aim for dof 0.
        """
        try:
            d = _doc(doc_id)
            feat = _find_sketch3d(d, sketch3d_id)
            params = feat.get("params") or {}
            cons = list(params.get("constraints") or [])
            # Same stringified-list dialect as curves — coerce before the shape check
            # rather than bare-isinstance-rejecting a well-formed but stringified batch.
            constraints = require_list(constraints, "constraints",
                                        'a list of constraint records, e.g. '
                                        '[{"type":"fix","refs":["e1"]}]')
            if not constraints:
                return _dumps({"ok": False, "error": "constraints must be a non-empty list"})
            n = _next_id(cons, "c")
            added = []
            for i, c in enumerate(constraints):
                if not isinstance(c, dict) or not c.get("type"):
                    return _dumps({"ok": False,
                                   "error": f"constraints[{i}] must be a dict with 'type'"})
                rec = dict(c)
                if not rec.get("id"):
                    n += 1
                    rec["id"] = f"c{n}"
                if "value" in rec and not isinstance(rec["value"], dict):
                    rec["value"] = _dim(rec["value"])
                added.append(rec)
            cons.extend(added)
            _apply_ops(d, [{"op": "edit_feature", "id": sketch3d_id,
                            "params": {"constraints": cons}}])
            read = _health_read(_health(d, sketch3d_id))
            return _dumps({"ok": True, "id": sketch3d_id, "doc_id": d,
                           "added": len(added),
                           "new_constraint_ids": [c["id"] for c in added],
                           "constraint_count": len(cons), "health": read})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_ref_vertex(sketch3d_id: str, body_id: int = 1, topo_id: Optional[int] = None,
                            selector: Optional[dict] = None,
                            doc_id: Optional[str] = None) -> str:
        """Ground a sketch3d constraint to a specific VERTEX on the built model.

        Motivating use case: "a spline that interacts with a specific corner on my part" — this
        mints an external reference (doc_schema §3B.1b) that tracks a model vertex across
        rebuilds. Pass EITHER:
          topo_id   the vertex pick id from a viewport click (kind="vertex") — same id space
                    doc_resolve_pick uses; body_id defaults to 1 (the single-body case)
          selector  a raw vertex selector dict (already resolved elsewhere, e.g. by a prior
                    sketch3d_ref_vertex call you're re-using, or hand-built)

        Returns {ok, ref_id, point} — ``ref_id`` (e.g. "x1") is a POINT HANDLE usable directly
        in sketch3d_add_constraints, e.g. {"type":"coincident","refs":["e3.p2","x1"]} or
        {"type":"distance","refs":["e1.p1","x1"],"value":10}. The point is re-resolved on
        every rebuild (it follows the vertex if upstream geometry moves); check_sketch3d /
        sketch3d_get surface a warning if the source vertex stops resolving (stale — last-good
        position kept, the sketch never breaks).
        """
        try:
            d = _doc(doc_id)
            feat = _find_sketch3d(d, sketch3d_id)
            if selector is not None:
                # A stringified selector dict is the same dialect require_mapping already
                # teaches on cad_asm_tools' `a`/`b` — coerce before it gets embedded verbatim
                # into external_refs and fails deep inside the engine with no context.
                selector = require_mapping(selector, "selector",
                                            "a raw vertex selector dict (e.g. from a prior "
                                            "sketch3d_ref_vertex call or doc_resolve_pick)")
            if selector is None:
                if topo_id is None:
                    return _dumps({"ok": False,
                                   "error": "pass either topo_id (a viewport vertex pick) or "
                                            "selector (a raw vertex selector dict)"})
                pick = cb.call("doc_resolve_pick",
                               {"doc_id": d, "body_id": body_id, "kind": "vertex",
                                "topo_id": topo_id})
                selector = pick.get("selector")
                if not selector:
                    return _dumps({"ok": False, "error": f"vertex pick {topo_id} did not resolve"})
            # Mint a doc-wide-unique "x<N>" id (external refs share their own id space —
            # separate from "e<N>" entities / "c<N>" constraints — across every sketch3d
            # feature, so a ref minted for one sketch never collides with another's).
            used = set()
            for f in _tree_features(d):
                if f.get("type") != "sketch3d":
                    continue
                for xr in ((f.get("params") or {}).get("external_refs") or []):
                    rid = xr.get("id") if isinstance(xr, dict) else None
                    if isinstance(rid, str) and rid.startswith("x") and rid[1:].isdigit():
                        used.add(int(rid[1:]))
            n = (max(used) + 1) if used else 1
            ref_id = f"x{n}"
            refs = list((feat.get("params") or {}).get("external_refs") or [])
            refs.append({"id": ref_id, "source": {"kind": "vertex", "selector": selector},
                        "stale": True})
            _apply_ops(d, [{"op": "edit_feature", "id": sketch3d_id,
                            "params": {"external_refs": refs}}])
            # Force a rebuild so refresh_sketch3d_external_refs resolves it immediately
            # (edit_feature alone re-solves the sketch but the refresh pass runs on every
            # mutating apply_ops batch, so this has already happened by now — the extra
            # doc_rebuild call just guarantees a fresh read-back for the caller).
            cb.call("doc_rebuild", {"doc_id": d})
            feat2 = _find_sketch3d(d, sketch3d_id)
            refs2 = (feat2.get("params") or {}).get("external_refs") or []
            rec = next((r for r in refs2 if r.get("id") == ref_id), None)
            point = (rec or {}).get("last_point")
            stale = (rec or {}).get("stale", True)
            return _dumps({"ok": True, "id": sketch3d_id, "doc_id": d, "ref_id": ref_id,
                           "point": point, "stale": stale,
                           "hint": (f"use '{ref_id}' as a point handle in "
                                    "sketch3d_add_constraints, e.g. "
                                    f'{{"type":"coincident","refs":["e1.p1","{ref_id}"]}}')})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def check_sketch3d(sketch3d_id: str, doc_id: Optional[str] = None) -> str:
        """Check whether a 3D sketch is well-constrained — the sketch3d twin of check_sketch.

        Solves the sketch3d feature (stateless sketch3d_health RPC) and returns:
          status        "full" (fully defined — the goal), "under", or "over" (conflict)
          dof           remaining degrees of freedom (0 = fully defined)
          conflicting   constraint ids that contradict each other — remove one
          redundant     constraint ids implied by the others
          free_narration remaining loose coordinates in plain words ("e2: p2 x/y/z loose")
          entity_statuses per-entity full/under/over
          wires         chain report [{closed, n_edges}] — index k maps 1:1 to the sweep
                        ref {"feature": "<fid>", "wire": k}
          summary       one-line plain-English verdict + what to do next

        Workflow: author entities → check_sketch3d → add the missing constraints via
        sketch3d_add_constraints (fix an endpoint, along_axis a leg, length/distance dims)
        → re-check until dof 0.  A path used only as a sweep spine does not strictly NEED
        dof 0, but pinned paths survive edits parametrically.
        """
        try:
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            read = _health_read(_health(d, sketch3d_id))
            return _dumps({"ok": True, "doc_id": d, "sketch": sketch3d_id, **read})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_get(sketch3d_id: str, doc_id: Optional[str] = None) -> str:
        """Read back a sketch3d feature: authored params + SOLVED geometry + health.

        Returns {ok, id, name, entities (solved coords), constraints (authored), wires,
        health, bbox, total_length_mm}.  `entities` carry the solver's current coordinates
        (what the GUI displays); `constraints` are the authored records.
        """
        try:
            d = _doc(doc_id)
            feat = _find_sketch3d(d, sketch3d_id)
            h = _health(d, sketch3d_id)
            chains = _sketch_polyline(h.get("entities") or [])
            return _dumps({
                "ok": True, "id": sketch3d_id, "doc_id": d,
                "name": feat.get("name"),
                "entities": h.get("entities") or [],
                "constraints": (feat.get("params") or {}).get("constraints") or [],
                "wires": h.get("wires") or [],
                "health": _health_read(h),
                "bbox": _bbox_of(chains),
                "total_length_mm": round(_polyline_length(chains), 4),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_clear(sketch3d_id: str, doc_id: Optional[str] = None) -> str:
        """Erase all entities + constraints from a sketch3d feature (keeps the feature id).

        Use this to restart a path without deleting/re-adding the feature (downstream sweep
        refs to the fid survive, though they will fail until the sketch has a wire again).
        Returns {ok, id}.
        """
        try:
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            _apply_ops(d, [{"op": "edit_feature", "id": sketch3d_id,
                            "params": {"entities": [], "constraints": []}}])
            return _dumps({"ok": True, "id": sketch3d_id, "doc_id": d,
                           "entity_count": 0, "constraint_count": 0})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_list(doc_id: Optional[str] = None) -> str:
        """List all sketch3d FEATURES in the part document.

        Returns {ok, sketches: [{id, name, entity_count, constraint_count, wires, status,
        dof, total_length_mm}], count}.  These are persistent doc features — they save with
        the part.
        """
        try:
            d = _doc(doc_id)
            out = []
            for f in _tree_features(d):
                if not isinstance(f, dict) or f.get("type") != "sketch3d":
                    continue
                fid = f.get("id")
                params = f.get("params") or {}
                row = {"id": fid, "name": f.get("name"),
                       "entity_count": len(params.get("entities") or []),
                       "constraint_count": len(params.get("constraints") or []),
                       "suppressed": bool(f.get("suppressed", False))}
                try:
                    h = _health(d, fid)
                    chains = _sketch_polyline(h.get("entities") or [])
                    row.update({"status": h.get("status"), "dof": h.get("dof"),
                                "wires": h.get("wires") or [],
                                "total_length_mm": round(_polyline_length(chains), 4)})
                except Exception:  # noqa: BLE001
                    pass
                out.append(row)
            return _dumps({"ok": True, "doc_id": d, "sketches": out, "count": len(out)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_preview(sketch3d_id: str, doc_id: Optional[str] = None) -> str:
        """TEXT description of a sketch3d's SOLVED geometry — the no-vision describe.

        One line per entity (type, solved coords, per-entity constraint status), the wire
        chain report with the exact {"feature","wire"} ref for each chain, total length,
        bbox, and the narrated health verdict.  Use this to 'see' a 3D path headless before
        sweeping along it.

        Returns {ok, id, name, summary_text, entity_count, wires, health}.
        """
        try:
            d = _doc(doc_id)
            feat = _find_sketch3d(d, sketch3d_id)
            h = _health(d, sketch3d_id)
            text = _preview_text(sketch3d_id, feat.get("name") or "", h)
            return _dumps({"ok": True, "id": sketch3d_id, "doc_id": d,
                           "name": feat.get("name"),
                           "summary_text": text,
                           "entity_count": len(h.get("entities") or []),
                           "wires": h.get("wires") or [],
                           "health": _health_read(h)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ─────────────────────────────── one-call ergonomic helpers ──────────────────────────────────

    @mcp.tool()
    def sketch3d_path_from_points(name: str, points: list, fit: str = "polyline",
                                  close: bool = False,
                                  doc_id: Optional[str] = None) -> str:
        """ONE-CALL routing path: create a sketch3d feature through the given world points.

        name: label for the new feature (e.g. "hose_route").
        points: [[x,y,z], ...] in mm, world space (≥ 2).
        fit: 'polyline' (default — straight line entity per consecutive pair; crisp bends)
             or 'spline' (a single cubic through ALL the points; smooth path).
        close: True appends a closing segment back to the first point (closed wire — valid
               sweep path).  For fit='spline' the first point is appended to the spline.
        doc_id: part document (default: current part).

        Returns {ok, id, name, entity_count, wires, health, total_length_mm} — `id` is the
        feature id, ready for sketch3d_pipe(id, diameter_mm=...) or a sweep
        {"path": "<id>"} ref.  Constrain it afterwards with sketch3d_add_constraints
        (e.g. fix the first endpoint + along_axis the straight legs) and check_sketch3d.
        """
        try:
            d = _doc(doc_id)
            pts = _validate_pts(points, 2, name="points")
            if fit not in ("polyline", "spline"):
                return _dumps({"ok": False,
                               "error": f"fit must be 'polyline' or 'spline', got {fit!r}"})
            if close:
                pts = pts + [list(pts[0])]
            if fit == "spline":
                entities = _curves_to_entities([{"kind": "spline", "pts": pts}])
            else:
                entities = _curves_to_entities([{"kind": "polyline", "pts": pts}])
            r = _apply_ops(d, [{"op": "add_feature", "feature": {
                "type": "sketch3d", "name": (name or "").strip() or "Path3D",
                "params": {"entities": entities, "constraints": []}}}])
            fid = (r.get("id_map") or {}).get("$0")
            h = _health(d, fid)
            chains = _sketch_polyline(h.get("entities") or [])
            return _dumps({"ok": True, "id": fid, "doc_id": d, "name": name,
                           "fit": fit, "closed": bool(close),
                           "entity_count": len(entities),
                           "wires": h.get("wires") or [],
                           "health": _health_read(h),
                           "total_length_mm": round(_polyline_length(chains), 4),
                           "hint": f"sweep ref: {{\"path\": \"{fid}\"}} — or one-call: "
                                   f"sketch3d_pipe('{fid}', diameter_mm=...)"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_pipe(sketch3d_id: str,
                      diameter_mm: Optional[float] = None,
                      profile_sketch: Optional[str] = None,
                      wire: Optional[int] = None,
                      guide: Optional[Any] = None,
                      mode: str = "boss",
                      merge: bool = True,
                      doc_id: Optional[str] = None) -> str:
        """ONE-CALL tube/sweep along a sketch3d path: profile placement + sweep, applied.

        "Run a 12 mm tube along this path" == sketch3d_pipe(fid, diameter_mm=12).

        Profile — pass exactly ONE of:
          diameter_mm: builds a circular profile automatically.  A datum plane is placed at
            the path START, EXACTLY perpendicular to the start tangent (point_normal datum
            method), with the circle centred exactly on the path start (the
            sweep pierce rule).  Works for any tangent direction, no axis-alignment needed.
          profile_sketch: an EXISTING 2D sketch feature id whose region is the profile
            (you are responsible for it sitting at the path start, ⟂ the tangent).

        wire: chain index when the sketch3d has multiple disconnected chains (from
          check_sketch3d/sketch3d_preview wires; bare fid is used when omitted — the engine
          rejects an ambiguous multi-chain bare ref loudly).
        guide: optional guide-curve ref — a sketch3d/curve feature id or
          {"feature": fid, "wire": k}.  ONE guide max; must differ from the path; it drives
          profile orientation + SCALING along the path (SolidWorks Guide Curves).
        mode: 'boss' (default — fuse into the current body), 'cut' (subtract the tube from
          it), or 'new' (leave it as a separate body).  'add' is accepted as the engine's
          synonym for boss, and equivalent words (union/join/fuse, subtract/remove/pocket,
          new_body/separate) are mapped; ANY OTHER value is REJECTED rather than silently
          building a disjoint boss where a cut was asked for.  merge: fuse with existing
          bodies (default True) — merge=False plus mode='cut' is a contradiction and errors.

        Returns {ok, feature_ids {plane?, profile?, sweep}, body_count, volume_mm3,
        start_point, start_tangent, warning?}.  On a zero-volume/pierce error the engine
        names the actual gap distance — fix the profile location and retry.
        """
        try:
            # FIRST, before any engine call: an out-of-vocabulary mode used to sail through
            # to the engine, which tests the value without validating it and commits the
            # swept tube as a separate body — reporting ok:true while ADDING material a
            # 'cut' asked it to remove. Nothing is touched when this raises.
            mode = _resolve_pipe_mode(mode, merge)
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            if (diameter_mm is None) == (profile_sketch is None):
                return _dumps({"ok": False, "error":
                               "pass exactly one of diameter_mm or profile_sketch"})
            h = _health(d, sketch3d_id)
            chains = _sketch_polyline(h.get("entities") or [])
            s = _sample_polyline(chains, 0.0)
            if s is None:
                return _dumps({"ok": False,
                               "error": "sketch3d has no curve entities to sweep along"})
            wires = h.get("wires") or []
            path_ref: Any = sketch3d_id if wire is None else \
                {"feature": sketch3d_id, "wire": int(wire)}
            if wire is None and len(wires) > 1:
                return _dumps({"ok": False, "error":
                               f"sketch3d has {len(wires)} wire chains — pass wire=<k> "
                               f"(see check_sketch3d wires)"})

            ops: list = []
            ids: dict = {}
            warning = None
            if diameter_mm is not None:
                plane_op, uv, _tilt = _profile_plane_ops(s["point"], s["tangent"])
                ops.append(plane_op)                                   # $0 plane
                ops.append({"op": "add_feature", "feature": {
                    "type": "sketch", "name": "PipeProfile",
                    "params": {"entities": [{"id": "e1", "type": "circle",
                                             "construction": False,
                                             "center": [uv[0], uv[1]],
                                             "radius": float(diameter_mm) / 2.0}],
                               "constraints": []},
                    "refs": {"plane": "$0"}}})                          # $1 profile
                profile_ref = [{"kind": "region", "sketch": "$1"}]
                sweep_index = 2
            else:
                profile_ref = [{"kind": "region", "sketch": profile_sketch}]
                sweep_index = 0
            sweep_refs: dict = {"profile": profile_ref, "path": path_ref}
            if guide is not None:
                # A model-stringified guide ref ({"feature":...} or a bare fid string that
                # happens to be Python-repr'd) — coerce_json_or_repr never raises, so a real
                # bare-string fid passes through untouched while a stringified dict resolves.
                sweep_refs["guides"] = [coerce_json_or_repr(guide)]
            ops.append({"op": "add_feature", "feature": {
                "type": "sweep", "name": "Pipe",
                "params": {"mode": mode, "merge": bool(merge)},
                "refs": sweep_refs}})
            r = _apply_ops(d, ops)
            id_map = r.get("id_map") or {}
            if diameter_mm is not None:
                ids = {"plane": id_map.get("$0"), "profile": id_map.get("$1"),
                       "sweep": id_map.get(f"${sweep_index}")}
            else:
                ids = {"sweep": id_map.get("$0")}
            out = {"ok": True, "doc_id": d, "sketch3d_id": sketch3d_id,
                   "feature_ids": ids,
                   "start_point": [round(v, 4) for v in s["point"]],
                   "start_tangent": [round(v, 6) for v in s["tangent"]],
                   "path_length_mm": round(s["total_length_mm"], 4)}
            if warning:
                out["warning"] = warning
            try:
                m = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
                out["body_count"] = m.get("body_count")
                out["volume_mm3"] = round(m.get("mass_g", 0.0) * 1000.0, 3)
            except Exception:  # noqa: BLE001
                pass
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_as_sweep_guide(
        sketch3d_id: str,
        role: str = "guide",
        doc_id: Optional[str] = None,
    ) -> str:
        """Get the exact sweep ref syntax for using this sketch3d as the PATH or a GUIDE.

        The sketch3d is already a real doc feature (no conversion needed) — this tool reads
        its wire chains and returns the ready-to-use ref for the sweep op.

        sketch3d_id: the sketch3d FEATURE id (e.g. "f3").
        role: "spine" — the primary sweep path ref ({"path": ...});
              "guide" (default) — a guides[] entry (ONE guide per sweep; must be a
              DIFFERENT feature than the path; drives profile orientation + scaling).
        doc_id: part document (default: current part).

        Returns {ok, sketch3d_id, role, guide_support:true, ref, wires, coord_summary,
        workflow_hint}.  `ref` is a bare fid for a single-chain sketch, or
        {"feature": fid, "wire": 0} when explicit chain selection is needed.
        For the common circular-tube case, sketch3d_pipe does the whole profile+sweep in
        one call.
        """
        try:
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            h = _health(d, sketch3d_id)
            wires = h.get("wires") or []
            if not wires:
                return _dumps({"ok": False, "error":
                               "sketch3d has no wire chains — add curve entities first "
                               "(sketch3d_add_curves)"})
            entities = h.get("entities") or []
            coord_lines = []
            for ent in entities:
                pl = _entity_polyline(ent)
                if not pl:
                    continue
                head = " → ".join(f"({p[0]:.2f},{p[1]:.2f},{p[2]:.2f})"
                                  for p in (pl[:2] + (["…"] if len(pl) > 2 else []))
                                  if isinstance(p, list))
                coord_lines.append(f"  {ent.get('id')} {ent.get('type')}: {head}")
            multi = len(wires) > 1
            ref: Any = ({"feature": sketch3d_id, "wire": 0} if multi else sketch3d_id)
            if role == "spine":
                hint = (f"Sweep with refs {{\"profile\": [{{\"kind\":\"region\","
                        f"\"sketch\":\"<2D-profile-fid>\"}}], \"path\": "
                        f"{json.dumps(ref)}}}. The 2D profile must sit at the path START, "
                        f"roughly perpendicular to its tangent (sketch3d_sample_at(id, 0) "
                        f"gives point+tangent). One-call alternative: sketch3d_pipe.")
            else:
                hint = (f"On the sweep set \"guides\": "
                        f"[{{\"feature\": \"{sketch3d_id}\", \"wire\": 0}}] — ONE guide, "
                        f"different feature than the path; it drives profile orientation "
                        f"+ scaling (SolidWorks Guide Curves).")
            return _dumps({
                "ok": True, "sketch3d_id": sketch3d_id, "role": role, "doc_id": d,
                "guide_support": True,
                "ref": ref if role == "spine" else {"feature": sketch3d_id, "wire": 0},
                "wires": wires,
                "coord_summary": "\n".join(coord_lines),
                "workflow_hint": hint,
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ──────────────────────────────────── path sampling / section ────────────────────────────────

    @mcp.tool()
    def sketch3d_sample_at(sketch3d_id: str, t: float = 0.5,
                           doc_id: Optional[str] = None) -> str:
        """Sample a POINT + TANGENT on the SOLVED sketch3d path at arc-length fraction t.

        Walks the solved entities in authored order to the fraction t ∈ [0,1] of total
        arc-length (arcs tessellated on their circumcircle; splines approximated by their
        through-points polyline) and returns the world point plus the unit tangent.  Useful
        for placing profile planes along a sweep spine or locating a datum on a route.

        Returns {ok, t, point:[x,y,z], tangent:[dx,dy,dz], total_length_mm}.
        t values outside [0,1] are clamped.  sketch3d_pipe uses this same t=0 logic to
        place its auto profile.
        """
        try:
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            h = _health(d, sketch3d_id)
            chains = _sketch_polyline(h.get("entities") or [])
            s = _sample_polyline(chains, t)
            if s is None:
                return _dumps({"ok": False, "error": "sketch3d has no curve entities"})
            return _dumps({"ok": True, "t": max(0.0, min(1.0, float(t))),
                           "point": [round(v, 4) for v in s["point"]],
                           "tangent": [round(v, 6) for v in s["tangent"]],
                           "total_length_mm": round(s["total_length_mm"], 4),
                           "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_section_at(
        sketch3d_id: str,
        t: float = 0.5,
        doc_id: Optional[str] = None,
    ) -> str:
        """Cross-section the part with a plane whose ORIENTATION follows the path tangent.

        ENGINE LIMIT: eval_section_props always cuts at the WORLD ORIGIN — `t` selects the
        tangent direction at that arc-length fraction of the SOLVED sketch3d, which picks
        the nearest reserved plane ORIENTATION; the cut LOCATION is always the origin.
        Use sketch3d_sample_at for the actual on-path point, and add_section_view with
        offset_mm for an offset cut.

        Plane from dominant tangent axis: X → 'right' (YZ), Y → 'front' (XZ), Z → 'top' (XY).

        Returns {ok, t, point (on-path), tangent, plane_used, area_mm2, centroid,
        Ixx, Iyy, Ixy, summary}.  area_mm2 is 0 if the origin plane misses the solid.
        If the section fails, the tool returns ok=false with the engine code and detail.
        Example: code "section_failed" with detail.reason "body_not_closed_solid" when the
        plane cuts an open body. Run check_geometry_health on that body to find the cause.
        """
        try:
            d = _doc(doc_id)
            _find_sketch3d(d, sketch3d_id)
            h = _health(d, sketch3d_id)
            chains = _sketch_polyline(h.get("entities") or [])
            s = _sample_polyline(chains, t)
            if s is None:
                return _dumps({"ok": False, "error": "sketch3d has no curve entities"})
            point, tangent = s["point"], s["tangent"]
            tx, ty, tz = tangent
            dominant = max(range(3), key=lambda i: abs(tangent[i]))
            plane_used = {0: "right", 1: "front", 2: "top"}[dominant]
            # A genuine empty intersection returns area 0 from the engine. Any error is a
            # real failure (e.g. section_failed / body_not_closed_solid) and reaches _err with
            # its code and detail; it is never an area of 0.
            res = cb.call("eval_section_props", {"doc_id": d, "plane": plane_used})
            area = res.get("area_mm2", 0.0)
            t_clamped = max(0.0, min(1.0, float(t)))
            summary = (f"Section plane='{plane_used}' (at world origin, normal from tangent "
                       f"at t={t_clamped:.2f} tangent=({tx:.2f},{ty:.2f},{tz:.2f})): "
                       f"area = {area:.4g} mm^2")
            if not area:
                summary += (" — area=0: the origin-plane may not intersect the solid. "
                            "Use add_section_view with offset_mm for an offset cut.")
            return _dumps({
                "ok": True, "t": t_clamped,
                "point": [round(p, 4) for p in point],
                "tangent": [round(v, 6) for v in tangent],
                "plane_used": plane_used, "area_mm2": area,
                "centroid": res.get("centroid"), "Ixx": res.get("Ixx"),
                "Iyy": res.get("Iyy"), "Ixy": res.get("Ixy"),
                "summary": summary, "doc_id": d,
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ──────────────────────────────────────── sketch preview tools ────────────────────────────────

    @mcp.tool()
    def render_sketch(
        sketch: str,
        doc_id: Optional[str] = None,
        views: Optional[list] = None,
    ) -> Any:
        """Render the part containing a 2D sketch as a multi-view image.

        Shows the SOLID BODIES from standard camera angles so you can see the model context
        around the sketch.  The caption includes the sketch health summary (DOF, closure).

        IMPORTANT LIMITS:
          • The engine has no sketch-isolated flat-view RPC — there is no "doc_render_sketch"
            method.  This renders solid bodies (doc_render_view / doc_render_contact_sheet),
            not bare sketch wireframes.  If the doc has no solid geometry yet the call falls
            back to a matplotlib wireframe of the sketch entities.
          • For the 2D sketch geometry itself (entity coords, constraint list, DOF, regions)
            use sketch_preview_text — it gives full text without needing a rendered image.
          • For a 3D sketch (sketch3d feature) use sketch3d_preview (text) — this tool is
            for 2D plane sketches.

        sketch: the sketch feature id (e.g. "s1", "s2").  Used only for the health caption.
        doc_id: part document (default: current part).
        views: camera list — any subset of ["front","right","top","iso","left","back","bottom"]
               (default: ["top", "iso"]).

        Returns [image, caption] — the PNG contact sheet plus a sketch health summary.
        """
        try:
            d = _doc(doc_id)
            view_list = require_list(views, "views", 'a list of view names, e.g. '
                                      '["front","top"]') if views else ["top", "iso"]
            view_list = ["top" if v == "flat" else v for v in view_list]
            png = _tmp(".png")

            rendered = False
            try:
                r = cb.call("doc_render_contact_sheet", {
                    "doc_id": d, "out_path": png, "views": view_list})
                rendered = r.get("ok") and os.path.exists(png)
            except Exception:  # noqa: BLE001
                rendered = False

            if not rendered:
                try:
                    r2 = cb.call("doc_render_view", {
                        "doc_id": d, "out_path": png, "view": "iso"})
                    rendered = r2.get("ok") and os.path.exists(png)
                except Exception:  # noqa: BLE001
                    rendered = False

            if not rendered:
                # Fallback: matplotlib wireframe of the 2D sketch entities (pre-extrude).
                try:
                    overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
                    wf_ok = _render_sketch_wireframe(overview, sketch, png)
                except Exception:  # noqa: BLE001
                    wf_ok = False
                if wf_ok:
                    try:
                        h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                        health_text = (f"Sketch {sketch!r} wireframe (pre-extrude): "
                                       f"{h.get('status', 'unknown').upper()} "
                                       f"dof={h.get('dof', '?')}")
                    except Exception:  # noqa: BLE001
                        health_text = f"Sketch {sketch!r} wireframe (no solid yet)"
                    return [Image(path=png), health_text]
                return _dumps({
                    "ok": False,
                    "error": ("could not render — no solid geometry built yet and "
                              "sketch entity coords not available. "
                              "Call sketch_preview_text for text-only sketch info."),
                })

            health_text = ""
            try:
                h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                status = h.get("status", "unknown")
                dof = h.get("dof", "?")
                closure = (h.get("closure") or {})
                open_eps = closure.get("open_endpoints") or []
                closure_str = "CLOSED" if not open_eps else f"OPEN ({len(open_eps)} endpoint(s))"
                health_text = (f"Sketch {sketch!r}: {status.upper()} "
                               f"(dof={dof}, closure={closure_str})")
                if h.get("dof_narration"):
                    health_text += f" — {h['dof_narration']}"
            except Exception:  # noqa: BLE001
                health_text = f"(sketch health unavailable for {sketch!r})"

            caption = f"Part render (views: {', '.join(view_list)})\n{health_text}"
            return [Image(path=png), caption]
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch_preview_text(sketch: str, doc_id: Optional[str] = None) -> str:
        """Full TEXT description of a 2D sketch — entities, constraints, DOF, regions, health.

        The no-vision equivalent of render_sketch: combines describe_sketch (plane / entity
        roster / constraint list / closed regions) with sketch_health's narrated constraint
        schedule into a single, readable block a local model can act on without images.
        (For a 3D sketch use sketch3d_preview / check_sketch3d.)

        sketch: the sketch feature id (e.g. "s1").
        doc_id: part document (default: current part).

        Returns {ok, sketch, text} where text contains PLANE / ENTITIES / CONSTRAINTS /
        REGIONS / HEALTH sections.
        """
        try:
            d = _doc(doc_id)
            from .gsl_tools import _sketch_digest, _sketch_text, sketch_health_text  # type: ignore

            describe_text = ""
            try:
                overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
                try:
                    regions_resp = cb.call("doc_sketch_regions", {"doc_id": d, "sketch": sketch})
                except Exception:  # noqa: BLE001
                    regions_resp = {}
                feat = None
                try:
                    tree = cb.call("doc_tree", {"doc_id": d})
                    for f in ((tree.get("tree") or {}).get("features") or []):
                        if isinstance(f, dict) and f.get("id") == sketch:
                            feat = f
                            break
                except Exception:  # noqa: BLE001
                    pass
                digest = _sketch_digest(sketch, overview, regions_resp, feat)
                describe_text = _sketch_text(digest)
            except Exception:  # noqa: BLE001
                describe_text = "(entity description unavailable)"

            health_section = ""
            try:
                h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                health_section = sketch_health_text(h)
            except Exception:  # noqa: BLE001
                health_section = "(health narration unavailable)"

            combined = "\n\n".join(filter(None, [describe_text, "HEALTH\n" + health_section]))
            return _dumps({"ok": True, "sketch": sketch, "doc_id": d, "text": combined})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def doc_sketch_contact_sheet(
        doc_id: Optional[str] = None,
        include_health: bool = True,
    ) -> Any:
        """Multi-view contact sheet of the whole doc INCLUDING all sketches-in-progress.

        Renders the part from standard angles (front / right / top / iso) and appends a
        per-sketch health summary — 2D sketches AND 3D sketches (sketch3d features) — so
        you can see everything in one call.  Ideal before a commit: it flags any
        under-defined sketches that would fail an extrude/sweep.

        doc_id: part document (default: current part).
        include_health: True (default) — append the per-sketch health table to the caption.

        Returns [image, caption].
        """
        try:
            d = _doc(doc_id)
            png = _tmp(".png")

            r = cb.call("doc_render_contact_sheet", {
                "doc_id": d, "out_path": png, "views": []})
            if not r.get("ok") or not os.path.exists(png):
                r2 = cb.call("doc_render_png", {"doc_id": d, "out_path": png})
                if not r2.get("ok") or not os.path.exists(png):
                    return _dumps({
                        "ok": False,
                        "error": "nothing to render — build geometry first",
                        "hint": "add_features([{op:sketch,...},{op:extrude,...}]) then retry",
                    })

            caption_lines: list[str] = []
            try:
                meas = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
                bodies = meas.get("body_count")
                if bodies is not None:
                    caption_lines.append(f"Bodies: {bodies}")
            except Exception:  # noqa: BLE001
                pass

            if include_health:
                rows: list[str] = []
                try:
                    for feat in _tree_features(d):
                        if not isinstance(feat, dict):
                            continue
                        ftype = feat.get("type")
                        sid = feat.get("id", "?")
                        if ftype == "sketch":
                            try:
                                h = cb.call("sketch_health", {"doc_id": d, "sketch": sid})
                                status = h.get("status", "?")
                                closure = h.get("closure") or {}
                                open_eps = closure.get("open_endpoints") or []
                                cl = "CLOSED" if not open_eps else f"OPEN({len(open_eps)})"
                                emoji = {"full": "[OK]", "under": "[?]",
                                         "over": "[!]"}.get(status, "[-]")
                                rows.append(f"  {emoji} {sid}: {status.upper()} "
                                            f"dof={h.get('dof', '?')} {cl}")
                            except Exception:  # noqa: BLE001
                                rows.append(f"  [-] {sid}: (health unavailable)")
                        elif ftype == "sketch3d":
                            try:
                                h = _health(d, sid)
                                status = h.get("status", "?")
                                wires = h.get("wires") or []
                                emoji = {"full": "[OK]", "under": "[?]",
                                         "over": "[!]"}.get(status, "[-]")
                                rows.append(f"  {emoji} {sid} (3D): {status.upper()} "
                                            f"dof={h.get('dof', '?')} "
                                            f"wires={len(wires)}")
                            except Exception:  # noqa: BLE001
                                rows.append(f"  [-] {sid} (3D): (health unavailable)")
                except Exception:  # noqa: BLE001
                    pass
                if rows:
                    caption_lines.append("Sketches:")
                    caption_lines.extend(rows)
                else:
                    caption_lines.append("Sketches: (none in feature tree)")

            caption = "\n".join(caption_lines) if caption_lines else "Contact sheet rendered."
            return [Image(path=png), caption]
        except Exception as e:  # noqa: BLE001
            return _err(e)


# ─────────────────────────────── matplotlib 2D wireframe fallback ─────────────────────────────────

def _render_sketch_wireframe(overview: dict, sketch_id: str, out_path: str) -> bool:
    """Render a 2D sketch wireframe to a PNG using matplotlib (pre-extrude fallback).

    Reads entity solved-coords from a doc_sketch_overview response and draws each entity
    as a colored line/arc in the sketch's 2D plane.  Returns True if the PNG was written.
    Works headless (Agg backend).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return False

    entities = (overview.get("entities") or [])
    if not entities:
        return False

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.set_aspect("equal")
    ax.set_title(f"Sketch {sketch_id!r} — wireframe preview", fontsize=10)
    ax.set_xlabel("u (mm)")
    ax.set_ylabel("v (mm)")
    ax.grid(True, linestyle="--", alpha=0.4)

    color_map = {
        "line": "#2196F3",
        "circle": "#4CAF50",
        "arc": "#FF9800",
        "spline": "#9C27B0",
        "polyline": "#9C27B0",
        "point": "#F44336",
    }

    any_drawn = False
    for ent in entities:
        etype = (ent.get("type") or "").lower()
        color = color_map.get(etype, "#607D8B")
        coords = ent.get("coords") or ent.get("solved_coords") or {}
        eid = ent.get("id", "")

        if etype == "line":
            sx = coords.get("start_x") or coords.get("x1", 0)
            sy = coords.get("start_y") or coords.get("y1", 0)
            ex = coords.get("end_x") or coords.get("x2", 0)
            ey = coords.get("end_y") or coords.get("y2", 0)
            ax.plot([sx, ex], [sy, ey], color=color, linewidth=1.5)
            mx, my = (sx + ex) / 2, (sy + ey) / 2
            ax.annotate(eid, (mx, my), fontsize=6, color=color, alpha=0.7)
            any_drawn = True

        elif etype == "circle":
            cx = coords.get("center_x", 0)
            cy = coords.get("center_y", 0)
            r = coords.get("radius", 1)
            circ = mpatches.Circle((cx, cy), r, fill=False, color=color, linewidth=1.5)
            ax.add_patch(circ)
            ax.annotate(eid, (cx + r * 0.7, cy + r * 0.7), fontsize=6, color=color, alpha=0.7)
            ax.set_xlim(auto=True)
            ax.set_ylim(auto=True)
            any_drawn = True

        elif etype == "arc":
            cx = coords.get("center_x", 0)
            cy = coords.get("center_y", 0)
            r = coords.get("radius", 1)
            a1 = math.degrees(coords.get("start_angle", 0))
            a2 = math.degrees(coords.get("end_angle", math.pi))
            arc_patch = mpatches.Arc((cx, cy), 2 * r, 2 * r,
                                     angle=0, theta1=a1, theta2=a2,
                                     color=color, linewidth=1.5)
            ax.add_patch(arc_patch)
            any_drawn = True

        elif etype in ("spline", "polyline"):
            pts = coords.get("points") or coords.get("control_points") or []
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                ax.plot(xs, ys, color=color, linewidth=1.5, linestyle="--")
                ax.annotate(eid, (xs[0], ys[0]), fontsize=6, color=color, alpha=0.7)
                any_drawn = True

        elif etype == "point":
            px = coords.get("x", 0)
            py = coords.get("y", 0)
            ax.plot(px, py, "+", color=color, markersize=8)
            ax.annotate(eid, (px, py), fontsize=6, color=color, alpha=0.7)
            any_drawn = True

    if not any_drawn:
        plt.close(fig)
        return False

    ax.autoscale_view()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return os.path.exists(out_path)
