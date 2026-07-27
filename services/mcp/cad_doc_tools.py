"""CAD document-lifecycle MCP tools (T-RF8 split of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Annotated
from typing import Any
from typing import Optional
from typing import Union

from pydantic import Field  # noqa: TC002 — runtime-read by FastMCP's schema builder
import json
import os
import re

from .cad_common import (
    _CUR,
    _DOC_ALIASES,
    _JOBS,
    _JOB_LOCK,
    _LAST_DELIVER_REV,
    _LAST_VOLUME,
    _SID_RE,
    _autosave,
    _doc,
    _doc_revision,
    _dumps,
    _err,
    _face_hist,
    _publish_current,
    _project_save,
    _resolve_alias,
    _resolve_appearance,
    _sanitize_output_path,
    _sid_match,
    _solve_sync_or_handoff,
    _start_job,
    _tmp,
    _unpublish,
    _write_diff,
    cb,
    recover_autosaved,
)

from .cad_guide import (
    cad_guide_text,
)

# ---------------------------------------------------------------------------
# Sketch-op shape normalizer (model-robustness).
#
# The engine's sketch schema is strict: entities under params with e<N> ids and
# p1/p2 point fields, constraints with c<N> ids + `refs` handles (eN.pM), the
# sketch plane under refs.plane. LLMs (esp. smaller ones) very naturally emit a
# flatter form instead — L1/L2 ids, start/end keys, a `constraints` list keyed on
# `entities`, a separate `dimensions` array, and a top-level `plane:"XY"`. That
# form is REJECTED wholesale by validate.cpp (invalid_op), which surfaces as a red
# "add_features failed" card mid-build. Normalize the common deviations into the
# canonical shape BEFORE doc_apply_ops so a natural sketch just works. Conservative:
# only rewrites a feature that shows the deviant form; canonical ops pass through
# untouched (byte-identical), and any failure falls back to the original op.
# The engine's RESERVED DATUMS (core/doc/validate.cpp::RESERVED_DATUMS) are the only bare
# strings a datum/plane/axis ref may carry besides an f<N> feature id — everything else is
# rejected as "'XZ' is neither a reserved datum nor a feature id". Models write the plane
# the way a person names it ("XZ", "front", "Top Plane"), so map every natural spelling onto
# the reserved token here, before the call.
_DATUM_TOKENS = frozenset({
    "origin", "point.origin",
    "plane.front", "plane.top", "plane.right",
    "axis.x", "axis.y", "axis.z",
})
_PLANE_ALIASES = {
    "xy": "plane.top", "yx": "plane.top", "top": "plane.top", "topplane": "plane.top",
    "plane.xy": "plane.top", "plane_xy": "plane.top", "top_plane": "plane.top",
    "xz": "plane.front", "zx": "plane.front", "front": "plane.front",
    "frontplane": "plane.front", "plane.xz": "plane.front", "plane_xz": "plane.front",
    "front_plane": "plane.front",
    "yz": "plane.right", "zy": "plane.right", "right": "plane.right",
    "rightplane": "plane.right", "plane.yz": "plane.right", "plane_yz": "plane.right",
    "right_plane": "plane.right", "side": "plane.right",
}
_AXIS_DATUM_ALIASES = {
    "x": "axis.x", "+x": "axis.x", "-x": "axis.x", "xaxis": "axis.x", "x_axis": "axis.x",
    "axis_x": "axis.x", "axis.x": "axis.x",
    "y": "axis.y", "+y": "axis.y", "-y": "axis.y", "yaxis": "axis.y", "y_axis": "axis.y",
    "axis_y": "axis.y", "axis.y": "axis.y",
    "z": "axis.z", "+z": "axis.z", "-z": "axis.z", "zaxis": "axis.z", "z_axis": "axis.z",
    "axis_z": "axis.z", "axis.z": "axis.z",
}
_DATUM_WORDS = {**_PLANE_ALIASES, **_AXIS_DATUM_ALIASES}
_CID_RE = re.compile(r"^c\d+$")
_EID_RE = re.compile(r"^e\d+$")
_FID_RE = re.compile(r"^f\d+$")
_BATCH_REF_RE = re.compile(r"^\$\d+$")
_DIM_CTYPES = frozenset({"distance", "length", "angle", "radius", "diameter"})


def _xy(v: Any) -> Any:
    """Coerce a coordinate to the engine's 2D [x, y]; pass anything else through."""
    if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:2]):
        return [v[0], v[1]]
    return v


def _norm_datum(v: Any) -> Optional[str]:
    """A plane/axis word -> the engine's reserved datum token. Non-strings -> None.

    An already-canonical token, an f<N> feature id and a same-batch "$N" placeholder are
    returned untouched; anything the map does not know is handed back verbatim so the
    engine (not this function) reports it.
    """
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s in _DATUM_TOKENS or _FID_RE.match(s) or _BATCH_REF_RE.match(s):
        return s
    return _DATUM_WORDS.get(s.lower().replace(" ", "").replace("-", "_"), s)


def _is_canon_datum(v: Any) -> bool:
    """True when a bare-string ref would satisfy validate_sel_or_datum as-is."""
    return isinstance(v, str) and (v in _DATUM_TOKENS or bool(_FID_RE.match(v))
                                   or bool(_BATCH_REF_RE.match(v)))


def _norm_dim_value(c: dict) -> dict:
    """Wrap a bare numeric dimension value in the engine's {value, expr} literal."""
    if c.get("type") in _DIM_CTYPES:
        val = c.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            c["value"] = {"value": val, "expr": None}
        elif isinstance(val, dict) and "value" in val and "expr" not in val:
            c["value"] = {"value": val["value"], "expr": val.get("expr")}
        c.setdefault("driven", False)
    return c


# A POLYLINE is how a model draws a revolve/extrude profile in one breath
# ({"type":"polyline","closed":true,"points":[[x,y],…]}) and the engine has no such
# entity type, so the whole batch died on «params.entities[0].type: not a sketch entity
# type» (live run 2026-07-27, "mudbucket"). Unlike `rectangle`/`polygon` — which
# session.cpp already expands from a size — a polyline carries its own coordinates, so
# turning it into the N `line` entities it names needs no geometry guessing at all.
_POLYLINE_TYPES = frozenset({"polyline", "poly_line", "polyline_closed"})


def _polyline_points(ent: dict) -> list:
    """The polyline's [x,y] points, consecutive duplicates dropped. [] = not expandable."""
    raw = ent.get("points")
    if not isinstance(raw, list):
        return []
    pts: list = []
    for p in raw:
        q = _xy(p)
        if not (isinstance(q, list) and len(q) == 2 and _is_num(q[0]) and _is_num(q[1])):
            return []
        if not pts or pts[-1] != q:
            pts.append(q)
    return pts


def _expand_polyline(ent: dict, start_id: int) -> list:
    """A polyline entity -> the `line` entities it means, ids e<start_id+1…>.

    `closed` adds the segment back to the first point (a repeated closing point is the
    same segment, so it is dropped first). [] when the shape is not unambiguously a
    polyline — a non-[x,y] point, or fewer than two distinct points — so the engine's
    own error still teaches instead of this inventing a profile.
    """
    pts = _polyline_points(ent)
    closed = bool(ent.get("closed", ent.get("close", False)))
    if closed and len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 2:
        return []
    constr = bool(ent.get("construction", False))
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if closed and len(pts) > 2:
        segs.append((pts[-1], pts[0]))
    return [{"id": f"e{start_id + i + 1}", "type": "line", "construction": constr,
             "p1": list(a), "p2": list(b)} for i, (a, b) in enumerate(segs)]


def _sketch_needs_norm(feat: dict, params: dict) -> bool:
    ents = params.get("entities") if isinstance(params.get("entities"), list) else feat.get("entities")
    cons = params.get("constraints") if isinstance(params.get("constraints"), list) else feat.get("constraints")
    if "entities" in feat or "constraints" in feat or "dimensions" in feat or "plane" in feat:
        return True
    if "dimensions" in params or "plane" in params:
        return True
    plane_ref = (feat.get("refs") or {}).get("plane")
    if not plane_ref:
        return True
    # A plane the model NAMED ("XZ", "Top Plane") is not a reserved datum, so the batch dies
    # on the dangling-ref guardrail even though every entity is already canonical. That path
    # never reached the normalizer before: fire on it too.
    if isinstance(plane_ref, str) and not _is_canon_datum(plane_ref):
        return True
    # `params.entities` / `params.constraints` are BOTH required for a sketch; a feature that
    # omits one is rejected outright ("params.constraints: required for sketch").
    if not isinstance(params.get("entities"), list) or not isinstance(
            params.get("constraints"), list):
        return True
    for e in (ents or []):
        if not isinstance(e, dict):
            continue
        if _slug(e.get("type")) in _POLYLINE_TYPES:
            return True
        if not (isinstance(e.get("id"), str) and _EID_RE.match(e["id"])):
            return True
        if not isinstance(e.get("construction"), bool):
            return True
        if e.get("type") == "line" and ("p1" not in e or "p2" not in e):
            return True
    for c in (cons or []):
        if not isinstance(c, dict):
            continue
        if "entities" in c or "handles" in c:
            return True
        if not (isinstance(c.get("id"), str) and _CID_RE.match(c["id"])):
            return True
    return False


def _normalize_sketch_feature(feat: dict, notes: Optional[list] = None) -> dict:
    """Rewrite a natural/flat sketch add_feature into the engine's canonical shape."""
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    sink = notes if isinstance(notes, list) else []

    def _pick(key: str) -> Any:
        if isinstance(params.get(key), list):
            return params[key]
        if isinstance(feat.get(key), list):
            return feat[key]
        return None

    # A sketch with no `entities` at all is still a sketch: the engine REQUIRES
    # params.entities and params.constraints on every one, so an empty profile must be
    # normalized into the canonical empty lists rather than skipped.
    ents_in = _pick("entities") or []
    if not _sketch_needs_norm(feat, params):
        return feat  # already canonical — leave byte-identical

    cons_in = _pick("constraints") or []
    dims_in = _pick("dimensions") or []

    id_map: dict[str, str] = {}
    norm_ents: list = []
    eid = 0
    for e in ents_in:
        if not isinstance(e, dict):
            norm_ents.append(e)
            continue
        if _slug(e.get("type")) in _POLYLINE_TYPES:
            seg = _expand_polyline(e, eid)
            if seg:
                if isinstance(e.get("id"), str) and e["id"]:
                    id_map[e["id"]] = seg[0]["id"]
                eid += len(seg)
                norm_ents.extend(seg)
                sink.append(
                    f'{_feat_label(feat, "sketch")}: expanded the polyline into '
                    f"{len(seg)} line entities — a sketch entity is one segment "
                    f"(line/arc/circle/spline), so a profile is a list of them")
                continue
            # < 2 distinct points, or a point that is not [x,y]: nothing unambiguous to
            # expand. Fall through and let the engine name what is wrong with it.
        eid += 1
        new_id = f"e{eid}"
        if isinstance(e.get("id"), str) and e["id"]:
            id_map[e["id"]] = new_id
        ne = dict(e)
        ne["id"] = new_id
        ne["construction"] = bool(ne.get("construction", False))
        etype = ne.get("type")
        if etype == "line":
            # Endpoint aliases models write: p1/p2 canonical; start/end, from/to, and
            # a/b all seen in live rejected batches (a/b from a 2026-07-27 copilot run).
            p1 = ne.get("p1", ne.get("start", ne.get("from", ne.get("a"))))
            p2 = ne.get("p2", ne.get("end", ne.get("to", ne.get("b"))))
            if p1 is not None:
                ne["p1"] = _xy(p1)
            if p2 is not None:
                ne["p2"] = _xy(p2)
            for k in ("start", "end", "from", "to", "a", "b"):
                ne.pop(k, None)
        elif etype in ("circle", "arc"):
            if "center" in ne:
                ne["center"] = _xy(ne["center"])
            for k in ("start", "end"):
                if isinstance(ne.get(k), list):
                    ne[k] = _xy(ne[k])
            if etype == "arc" and "ccw" not in ne:
                ne["ccw"] = True
        norm_ents.append(ne)

    def _remap(h: Any) -> Any:
        if not isinstance(h, str) or h in ("origin", "origin.p"):
            return h
        base, _, sub = h.partition(".")
        base = id_map.get(base, base)
        return base + ("." + sub if sub else "")

    norm_cons = []
    cn = 0
    for c in list(cons_in) + list(dims_in):
        if not isinstance(c, dict):
            norm_cons.append(c)
            continue
        nc = dict(c)
        refs = nc.get("refs", nc.get("entities", nc.get("handles")))
        if isinstance(refs, list):
            nc["refs"] = [_remap(x) for x in refs]
        nc.pop("entities", None)
        nc.pop("handles", None)
        cid = nc.get("id")
        if isinstance(cid, str) and _CID_RE.match(cid):
            cn = max(cn, int(cid[1:]))
        else:
            cn += 1
            nc["id"] = f"c{cn}"
        norm_cons.append(_norm_dim_value(nc))

    refs_out = dict(feat.get("refs") or {})
    plane = None
    for src in (refs_out.get("plane"), params.get("plane"), feat.get("plane")):
        p = _norm_datum(src) if src is not None else None
        if p:
            plane = p
            break
    refs_out["plane"] = plane or "plane.top"

    new_feat = dict(feat)
    for k in ("entities", "constraints", "dimensions", "plane"):
        new_feat.pop(k, None)
    # Models label a sketch with "id":"profile" intending to reference it later as the
    # extrude/revolve profile — but refs resolve by feature NAME (or "$N"/f-id), so the
    # label was dead weight and the batch died on "profile 'profile' not found" (live
    # copilot run, 2026-07-27). Promote it to the display name when none was given.
    if (not isinstance(new_feat.get("name"), str) or not new_feat["name"].strip()):
        sid = new_feat.get("id")
        if isinstance(sid, str) and sid.strip():
            new_feat["name"] = sid.strip()
    new_params = dict(params)
    new_params.pop("plane", None)
    new_params.pop("dimensions", None)
    new_params["entities"] = norm_ents
    new_params["constraints"] = norm_cons
    new_feat["params"] = new_params
    new_feat["refs"] = refs_out
    return new_feat


def _normalize_sketch_ops(ops: Any, notes: Optional[list] = None) -> Any:
    """Normalize any sketch add_feature ops in-place-safe; return a new list."""
    if not isinstance(ops, list):
        return ops
    out = []
    for op in ops:
        try:
            if (isinstance(op, dict) and op.get("op") == "add_feature"
                    and isinstance(op.get("feature"), dict)
                    and op["feature"].get("type") == "sketch"):
                op = {**op, "feature": _normalize_sketch_feature(op["feature"], notes)}
        except Exception:  # noqa: BLE001 — never let normalization break a valid call
            pass
        out.append(op)
    return out


# ---------------------------------------------------------------------------
# Primitive placement/orientation dialect normalizer (model-robustness).
#
# The canonical primitive add_feature carries placement + orientation as plain
# feature-level keys: at / at_offset / axis / centered / mode / merge. Frontier
# models instead reach for the shape they know from other CAD APIs — a nested
# `placement:{origin:[…],direction:[…]}` plus `boolean:"union"` — and those keys
# used to be DROPPED WHOLE: a spout meant to sit at x=200 pointing along +X was
# built centred on the origin pointing up, buried in the body, and surfaced only
# as the opaque "cylinder boss added no material". The engine now hard-errors on
# any unrecognised feature key, so the dialect is rewritten into the canonical
# keys HERE, before the call — and every dialect key this understands must be
# FULLY CONSUMED (popped, never left beside the rewritten form), because a
# leftover key is now a rejected batch. Conservative: primitives only, and only a
# feature that actually shows a dialect key; canonical ops pass through untouched.
_PRIMITIVE_TYPES = frozenset({"box", "cylinder", "sphere", "cone", "torus"})
_AT_ALIASES = ("position", "location", "origin")
_AXIS_ALIASES = ("direction", "dir", "normal", "orientation")
_BOOLEAN_ALIASES = ("boolean", "operation")
_PLACEMENT_AT_KEYS = ("at", "origin", "position", "location", "point")
_PLACEMENT_AXIS_KEYS = ("axis", "direction", "normal", "dir")
_PRIM_DIALECT_KEYS = frozenset(
    ("placement",) + _AT_ALIASES + _AXIS_ALIASES + _BOOLEAN_ALIASES)
_AXIS_WORDS = frozenset({"x", "+x", "-x", "y", "+y", "-y", "z", "+z", "-z"})
_BOOLEAN_MODES = {
    "union": "boss", "add": "boss", "join": "boss", "fuse": "boss",
    "merge": "boss", "weld": "boss", "boss": "boss",
    "cut": "cut", "subtract": "cut", "difference": "cut", "remove": "cut",
}
_BOOLEAN_NEW_BODY = frozenset({"new", "none", "new_body", "newbody"})
_BOOLEAN_INTERSECT = frozenset({"intersect", "intersection"})
_BOOLEAN_VALUES = ('union|add|join|fuse|merge|weld -> mode:"boss"; '
                   'cut|subtract|difference|remove -> mode:"cut"; '
                   'new|none|new_body -> merge:false (a separate body)')
_MISSING = object()


def _norm_axis_value(v: Any) -> Any:
    """Canonicalize an axis word ("+X " -> "+x"); a [x,y,z] vector passes through."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _AXIS_WORDS:
            return s
    return v


def _primitive_needs_norm(feat: dict, params: dict) -> bool:
    """True when a primitive carries a dialect key (or an un-canonical axis word)."""
    for k in _PRIM_DIALECT_KEYS:
        if k in feat or k in params:
            return True
    for src in (feat.get("axis"), params.get("axis")):
        if isinstance(src, str) and src.strip().lower() != src:
            return True
    return False


def _normalize_primitive_feature(feat: dict) -> dict:
    """Rewrite a dialect primitive add_feature into the canonical placement shape.

    placement/position/direction/boolean -> at / axis / mode / merge. Pure: the
    caller's feature (and its params) are never mutated. When a dialect key and its
    canonical target disagree the CANONICAL key wins and the dialect key is dropped
    (the model asked for the same thing twice; honour the documented spelling).
    """
    out = dict(feat)
    params = feat.get("params")
    new_params = dict(params) if isinstance(params, dict) else {}

    def peek(key: str) -> Any:
        # `params` is merged into the feature by the engine, so either level counts.
        if key in out:
            return out[key]
        return new_params.get(key, _MISSING)

    def consume(key: str) -> Any:
        v = peek(key)
        out.pop(key, None)
        new_params.pop(key, None)
        return v

    at_val: Any = _MISSING
    axis_val: Any = _MISSING

    # placement:{origin|position|location|point:[x,y,z], direction|axis|normal|dir:…}
    # A non-dict `placement` is NOT consumed — we don't understand it, so let the
    # engine's unknown-key error name it rather than dropping the model's intent.
    place = peek("placement")
    if isinstance(place, dict):
        consume("placement")
        for k in _PLACEMENT_AT_KEYS:
            if place.get(k) is not None:
                at_val = place[k]
                break
        for k in _PLACEMENT_AXIS_KEYS:
            if place.get(k) is not None:
                axis_val = place[k]
                break

    for k in _AT_ALIASES:
        v = consume(k)
        if at_val is _MISSING and v is not _MISSING and v is not None:
            at_val = v
    for k in _AXIS_ALIASES:
        v = consume(k)
        if axis_val is _MISSING and v is not _MISSING and v is not None:
            axis_val = v

    if at_val is not _MISSING and peek("at") is _MISSING:
        out["at"] = at_val
    if axis_val is not _MISSING and peek("axis") is _MISSING:
        out["axis"] = axis_val

    # boolean|operation -> mode / merge. An unusable value is an ERROR, not a drop:
    # the model named an operation it believes in, and "intersect" in particular has
    # no primitive mode at all (it would silently fuse instead of intersecting).
    for k in _BOOLEAN_ALIASES:
        v = consume(k)
        if v is _MISSING or v is None:
            continue
        word = str(v).strip().lower()
        if word in _BOOLEAN_INTERSECT:
            raise ValueError(
                f"add_feature {feat.get('type')}: '{k}':'{v}' is not supported — the "
                f"engine has no intersect mode for primitives. Supported: "
                f"{_BOOLEAN_VALUES}")
        if word in _BOOLEAN_MODES:
            if peek("mode") is _MISSING:
                out["mode"] = _BOOLEAN_MODES[word]
        elif word in _BOOLEAN_NEW_BODY:
            if peek("merge") is _MISSING:
                out["merge"] = False
        else:
            raise ValueError(
                f"add_feature {feat.get('type')}: unknown '{k}':'{v}'. Supported: "
                f"{_BOOLEAN_VALUES}")

    # Canonicalize the axis WORD wherever it ended up ("+X" -> "+x"); a [x,y,z]
    # vector passes through. Feature level is the canonical home (session.cpp's
    # op_add_feature hoists it into params alongside at/centered/mode/merge).
    axis_out = peek("axis")
    if axis_out is not _MISSING and axis_out is not None:
        if "axis" in out:
            out["axis"] = _norm_axis_value(axis_out)
        else:
            new_params["axis"] = _norm_axis_value(axis_out)

    if isinstance(params, dict) or new_params:
        out["params"] = new_params
    return out


# ---------------------------------------------------------------------------
# Dimension dialect: unit suffixes, diameter->radius, box `size` (model-robustness).
#
# The engine's MODEL SPACE IS MILLIMETRES — `units.system` ("MMGS"/"IPS") is a display
# setting read by the drawing formatter, never a geometry scale — and a dimension may
# reach it as a bare number, a numeric string, {"value":N,"expr":null}, or an EXPRESSION
# string evaluated by core/util/expr.cpp ("width/2"). What it cannot parse is the way a
# person actually states a size: "25mm", "1in", '2.5"' all die in the expression lexer as
# «syntax error in '25mm': trailing tokens». The same run also loses a cylinder spelled by
# `diameter` (the single most common way to describe a shaft or a bore) and a box spelled
# by `size:[l,w,h]`, both hard-rejected as a missing required param.
#
# All three are rewritten HERE, before the call, so the engine's grammar stays closed and
# still receives a clean number. Conservative on purpose:
#   * a string is converted ONLY when it is ENTIRELY a number plus a unit suffix — a real
#     expression ("width/2"), a bare number ("25") and a size code ("M8") pass through
#     untouched for the engine to evaluate;
#   * `diameter` is rewritten PER FEATURE TYPE, only where the engine wants a radius.
#     hole and bolt_circle take a REAL `diameter` param (core/doc/validate.cpp) — halving
#     it there would silently drill a bore at half size, so those types are never touched;
#   * every rewrite is ECHOED back in the response (`interpreted`) so the model learns the
#     canonical spelling instead of leaning on the alias forever.
_UNIT_TO_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0, "millimetres": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0, "centimetre": 10.0,
    "centimetres": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0, "metres": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "\u201d": 25.4, "\u2033": 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8, "'": 304.8, "\u2019": 304.8, "\u2032": 304.8,
    "thou": 0.0254, "mil": 0.0254, "mils": 0.0254,
    "um": 0.001, "\u00b5m": 0.001, "\u03bcm": 0.001, "micron": 0.001, "microns": 0.001,
}
# <number><unit>, the WHOLE string. `[^\W\d_]+` is the unicode letter run (so "µm" works);
# the alternation adds the prime marks, which are not letters.
_UNIT_NUM_RE = re.compile(
    r"^\s*(?P<n>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(?P<u>[^\W\d_]+|[\"'\u2019\u201d\u2032\u2033])\s*$")
# The compound imperial form a person speaks: 5'6", 5 ft 6 in.
_FT_IN_RE = re.compile(
    r"^\s*(?P<ft>\d+(?:\.\d*)?)\s*(?:'|\u2019|\u2032|ft|foot|feet)"
    r"\s*(?P<in>\d+(?:\.\d*)?)\s*(?:\"|\u201d|\u2033|in|inch|inches)?\s*$")
# Params whose value is TEXT, never a dimension — a text feature that embosses the literal
# string "25mm" must not have it silently turned into the number 25.
_NON_DIM_KEYS = frozenset({
    "type", "name", "id", "text", "font", "label", "title", "note", "comment",
    "description", "code", "source", "script", "style", "end", "standard", "tap",
    "mode", "align", "material", "appearance", "system", "unit", "units", "format",
    "plane", "size", "of", "side", "surf", "pick", "op",
})
# diameter -> radius, PER PRIMITIVE TYPE (the engine's radius params, validate.cpp).
# Deliberately absent: hole and bolt_circle, whose `diameter` is the real param.
_DIAMETER_ALIASES = {
    "cylinder": {"diameter": "radius", "dia": "radius", "d": "radius"},
    "sphere": {"diameter": "radius", "dia": "radius", "d": "radius"},
    "cone": {"d1": "r1", "d2": "r2", "diameter": "r1", "dia": "r1",
             "base_diameter": "r1", "bottom_diameter": "r1", "top_diameter": "r2"},
    "torus": {"major_diameter": "major_r", "outer_diameter": "major_r", "od": "major_r",
              "minor_diameter": "minor_r", "tube_diameter": "minor_r",
              "diameter": "major_r"},
}
_SIZE_KEYS = ("size", "dimensions", "dimension", "dims")
_BOX_DIMS = ("length", "width", "height")
_MAX_INTERPRETED_NOTES = 8


def _clean_num(x: float) -> Union[int, float]:
    """Kill binary-float noise from a unit conversion; hand back an int when it is one."""
    v = round(x, 9)
    return int(v) if v == int(v) and abs(v) < 1e15 else v


def _dim_mm(s: Any) -> Optional[Union[int, float]]:
    """A plain "<number><unit>" string -> millimetres. None for anything else.

    None means "not a unit literal" — a bare number, an expression ("width/2"), a size
    code ("M8"), a word. Those are the engine's business, not ours.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    m = _UNIT_NUM_RE.match(s)
    if m:
        factor = _UNIT_TO_MM.get(m.group("u").lower())
        if factor is None:
            return None
        return _clean_num(float(m.group("n")) * factor)
    m = _FT_IN_RE.match(s)
    if m:
        return _clean_num(float(m.group("ft")) * 304.8 + float(m.group("in")) * 25.4)
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_dim_object(v: Any) -> bool:
    """The canonical dim shape {"value":…,"expr":…} (± an explicit unit field)."""
    return (isinstance(v, dict) and ("value" in v or "expr" in v)
            and not (set(v) - {"value", "expr", "unit", "units"}))


def _convert_units(val: Any, notes: list, label: str, key: str = "", depth: int = 0):
    """Recursively convert unit-suffixed dimension strings to mm. -> (value, changed).

    Pure: an unchanged subtree is returned BY IDENTITY, so a canonical op round-trips
    byte-for-byte and no caller dict is ever copied for nothing.
    """
    if depth > 6:
        return val, False
    where = f"{label}{key}" if key else label.rstrip(".")

    if isinstance(val, str):
        mm = _dim_mm(val)
        if mm is None:
            return val, False
        notes.append(f"{where}: interpreted {val!r} as {mm} (document units are mm)")
        return mm, True

    if _is_dim_object(val):
        expr = val.get("expr")
        if isinstance(expr, str):
            mm = _dim_mm(expr)
            if mm is not None:
                notes.append(
                    f"{where}: interpreted expr {expr!r} as the value {mm} (mm)")
                return {"value": mm, "expr": None}, True
            return val, False  # a REAL expression — the engine evaluates it
        unit = val.get("unit") or val.get("units")
        if isinstance(unit, str) and _is_num(val.get("value")):
            factor = _UNIT_TO_MM.get(unit.strip().lower())
            if factor is not None:
                mm = _clean_num(float(val["value"]) * factor)
                notes.append(f"{where}: interpreted {val['value']} {unit} as {mm} (mm)")
                return {"value": mm, "expr": None}, True
        if isinstance(val.get("value"), str):
            mm = _dim_mm(val["value"])
            if mm is not None:
                notes.append(
                    f"{where}: interpreted {val['value']!r} as {mm} (document units are mm)")
                out = {k: v for k, v in val.items() if k not in ("unit", "units")}
                out["value"] = mm
                return out, True
        return val, False

    if isinstance(val, list):
        new: list = []
        changed = False
        for item in val:
            nv, ch = _convert_units(item, notes, label, key, depth + 1)
            changed = changed or ch
            new.append(nv)
        return (new, True) if changed else (val, False)

    if isinstance(val, dict):
        new = {}
        changed = False
        for k, v in val.items():
            if str(k).strip().lower() in _NON_DIM_KEYS:
                new[k] = v
                continue
            nv, ch = _convert_units(v, notes, label, str(k), depth + 1)
            changed = changed or ch
            new[k] = nv
        return (new, True) if changed else (val, False)

    return val, False


def _feat_label(feat: dict, ftype: str) -> str:
    """"Boss" / "cylinder" — names the feature a note is about."""
    name = feat.get("name")
    return name if isinstance(name, str) and name else (ftype or "feature")


def _split_two_levels(feat: dict):
    """(feature copy, params copy) plus peek/consume over BOTH levels.

    `params` is merged into the feature by session.cpp::op_add_feature, so a model may put
    a dimension at either level and mean the same thing.
    """
    out = dict(feat)
    params = feat.get("params")
    new_params = dict(params) if isinstance(params, dict) else {}

    def peek(key: str) -> Any:
        if key in out:
            return out[key]
        return new_params.get(key, _MISSING)

    def consume(key: str) -> Any:
        v = peek(key)
        out.pop(key, None)
        new_params.pop(key, None)
        return v

    return out, new_params, peek, consume


def _halve(v: Any):
    """diameter value -> radius value, preserving the shape it arrived in. None = can't."""
    if _is_num(v):
        return _clean_num(float(v) / 2.0)
    if isinstance(v, str):
        mm = _dim_mm(v)
        if mm is not None:
            return _clean_num(float(mm) / 2.0)
        try:
            return _clean_num(float(v.strip()) / 2.0)
        except (TypeError, ValueError):
            # A real expression ("shaft_d"): halve it AS an expression, still the engine's job.
            return {"value": 0.0, "expr": f"({v.strip()})/2"}
    if _is_dim_object(v):
        expr = v.get("expr")
        if isinstance(expr, str) and expr.strip():
            mm = _dim_mm(expr)
            if mm is None:
                return {"value": 0.0, "expr": f"({expr.strip()})/2"}
            return {"value": _clean_num(float(mm) / 2.0), "expr": None}
        inner = _halve(v.get("value"))
        if inner is None:
            return None
        return {"value": inner, "expr": None} if not isinstance(inner, dict) else inner
    return None


def _normalize_primitive_dims(feat: dict, ftype: str, notes: list) -> dict:
    """box `size` shorthand + diameter->radius aliases. Pure; identity when nothing applies."""
    aliases = _DIAMETER_ALIASES.get(ftype, {})
    size_key = next((k for k in _SIZE_KEYS
                     if k in feat or (isinstance(feat.get("params"), dict)
                                      and k in feat["params"])), None) if ftype == "box" else None
    hits = [k for k in aliases
            if k in feat or (isinstance(feat.get("params"), dict) and k in feat["params"])]
    if not hits and not size_key:
        return feat

    label = _feat_label(feat, ftype)
    out, new_params, peek, consume = _split_two_levels(feat)

    if size_key:
        v = consume(size_key)
        dims: Optional[list] = None
        if isinstance(v, list) and len(v) == 3:
            dims = list(v)
        elif isinstance(v, list) and len(v) == 1:
            dims = [v[0]] * 3
        elif _is_num(v) or isinstance(v, str) or _is_dim_object(v):
            dims = [v] * 3
        if dims is None:
            # An unusable shape — hand it back so the engine names it rather than guessing.
            new_params[size_key] = v
        else:
            written = []
            for name, dv in zip(_BOX_DIMS, dims):
                if peek(name) is not _MISSING:
                    continue  # an explicit length/width/height wins over the shorthand
                # Convert a unit literal HERE so the split reads as one rewrite
                # ("size '1in' as a 25.4 cube") instead of one note per side.
                mm = _dim_mm(dv)
                new_params[name] = dv if mm is None else mm
                written.append(f"{name} {new_params[name]}")
            if written:
                shape = "a cube" if len(set(map(str, dims))) == 1 else "l/w/h"
                notes.append(f"{label}: interpreted {size_key} {v!r} as {shape}: "
                             + ", ".join(written) + " — a box takes length/width/height")

    for key in sorted(hits):
        target = aliases[key]
        v = consume(key)
        if v is _MISSING or v is None:
            continue
        if peek(target) is not _MISSING:
            notes.append(f"{label}: ignored {key} (the canonical {target} was also given)")
            continue
        r = _halve(v)
        if r is None:
            new_params[key] = v  # unusable — let the engine report it
            continue
        new_params[target] = r
        shown = r.get("expr") if isinstance(r, dict) and r.get("expr") else (
            r["value"] if isinstance(r, dict) else r)
        given = v["value"] if _is_dim_object(v) and "value" in v else v
        notes.append(f"{label}: interpreted {key} {given!r} as {target} {shown!r} "
                     f"— a {ftype} is dimensioned by radius, not diameter")

    if isinstance(feat.get("params"), dict) or new_params:
        out["params"] = new_params
    return out


def _normalize_feature_dims(feat: dict, ftype: str, notes: list) -> dict:
    """Every dimension dialect on one feature: box size, then units, then diameter.

    ORDER MATTERS. `size:["40mm",…]` must be split before the unit pass can see its
    elements, and `diameter:"50mm"` must already be the number 50 before it is halved.
    """
    out = feat
    if ftype in _PRIMITIVE_TYPES:
        out = _normalize_primitive_dims(out, ftype, notes)
    converted, changed = _convert_units(out, notes, _feat_label(feat, ftype) + ".")
    if changed:
        out = converted
    if ftype in _PRIMITIVE_TYPES:
        out = _normalize_primitive_dims(out, ftype, notes)
    return out


def _normalize_feature_ops(ops: Any, notes: Optional[list] = None) -> Any:
    """Normalize dialect primitive placement/orientation + dimension + schema ops.

    `notes` (optional) collects a plain-English line per rewrite, echoed to the model as
    `interpreted` so it learns the canonical spelling. Returns a new list.
    """
    if not isinstance(ops, list):
        return ops
    sink = notes if isinstance(notes, list) else []
    out = []
    for op in ops:
        try:
            if isinstance(op, dict) and op.get("op") == "add_feature" and isinstance(
                    op.get("feature"), dict):
                feat = op["feature"]
                ftype = str(feat.get("type") or "").strip().lower()
                params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
                new = feat
                if ftype in _PRIMITIVE_TYPES and _primitive_needs_norm(feat, params):
                    new = _normalize_primitive_feature(feat)
                new = _normalize_feature_dims(new, ftype, sink)
                # Last: the requirements the engine enforces and the model forgets
                # (hole style/end/face, boolean op, extrude/revolve mode + profile ref,
                # datum words, kind-less selectors, {"value":[…]} around a coordinate).
                new = _normalize_feature_schema(new, ftype, sink)
                if new is not feat:
                    op = {**op, "feature": new}
            elif (isinstance(op, dict) and op.get("op") == "edit_feature"
                    and isinstance(op.get("params"), dict)):
                # An edit carries bare params (no feature wrapper) and no type, so only the
                # type-independent unit pass applies — never the per-type diameter alias.
                label = str(op.get("id") or "edit_feature") + "."
                conv, changed = _convert_units(op["params"], sink, label)
                if changed:
                    op = {**op, "params": conv}
        except ValueError:
            raise  # an unsupported request (boolean:"intersect") — tell the model
        except Exception:  # noqa: BLE001 — never let normalization break a valid call
            pass
        out.append(op)
    return out


def _interpreted_notes(notes: list) -> list:
    """De-duplicate and cap the echo list (a 20-hole batch must not bury the response)."""
    seen: list = []
    for n in notes:
        if n not in seen:
            seen.append(n)
    if len(seen) > _MAX_INTERPRETED_NOTES:
        extra = len(seen) - _MAX_INTERPRETED_NOTES
        seen = seen[:_MAX_INTERPRETED_NOTES] + [f"(+{extra} more of the same kind)"]
    return seen


# ---------------------------------------------------------------------------
# OP-ELEMENT + FEATURE-TYPE normalizer (model-robustness).
#
# The canonical batch element is {"op":"add_feature","feature":{"type":…}}. The single
# most common live rejection in the recorded corpus is one level flatter than that —
# ops=[{"type":"box","name":"Plate","params":{…}}], which dies as «op[0]: missing 'op'»
# — closely followed by the model using a feature TYPE as the op name
# ({"op":"box",…}) and by a type spelled the way a person says it ("cube", "cylindrical",
# "cut_cylinder"). All three are unambiguous, so they are rewritten here instead of
# rejected; each rewrite is echoed ONCE per batch in `interpreted` (the notes are
# de-duplicated, so a 20-element batch adds one line, not twenty).
_FEATURE_TYPE_ALIASES = {
    "cube": "box", "cuboid": "box", "rect_prism": "box", "rectangular_prism": "box",
    "block": "box", "rectangle": "box", "prism": "box", "plate": "box", "slab": "box",
    "circle": "cylinder", "disc": "cylinder", "disk": "cylinder", "cylindrical": "cylinder",
    "rod": "cylinder", "shaft": "cylinder", "tube": "cylinder", "pipe": "cylinder",
    "round_bar": "cylinder", "boss": "cylinder",
    "ball": "sphere", "spherical": "sphere",
    "conical": "cone", "frustum": "cone", "taper": "cone",
    "toroid": "torus", "ring": "torus", "donut": "torus",
    "bore": "hole", "drill": "hole", "counterbore": "hole", "countersink": "hole",
    "cbore": "hole", "csink": "hole", "tapped_hole": "hole", "clearance_hole": "hole",
    "pocket": "extrude", "profile": "sketch", "sketch2d": "sketch", "sketch_2d": "sketch",
    "round": "fillet", "blend": "fillet", "bevel": "chamfer", "hollow": "shell",
    "pattern": "linear_pattern", "array": "linear_pattern",
    "polar_pattern": "circular_pattern", "revolved_pattern": "circular_pattern",
    "engrave": "text", "emboss": "text", "extrusion": "extrude", "revolution": "revolve",
}
# An alias that also implies a boolean MODE ("a pocket" is an extrude that removes material).
_TYPE_ALIAS_MODE = {"pocket": "cut"}
# An alias that also pins the hole STYLE the word names.
_TYPE_ALIAS_HOLE_STYLE = {
    "counterbore": "cbore", "cbore": "cbore",
    "countersink": "csink", "csink": "csink",
    "tapped_hole": "tap",
}
# "<something>_cut" / "cut_<something>" = that shape, subtracted.
_CUT_TYPE_PREFIXES = ("cut_", "remove_", "subtract_", "negative_")
_CUT_TYPE_SUFFIXES = ("_cut", "_pocket", "_subtract", "_removal")
# A boolean OPERATION written where a feature type belongs — never a type, always a mode.
_BOOLEAN_AS_TYPE = frozenset({
    "cut", "subtract", "difference", "remove", "union", "add", "join", "fuse", "merge",
    "intersect", "intersection", "common", "combine",
})
# Suffix noise a model adds to a real type ("box3D", "cylinder_solid").
_TYPE_NOISE_SUFFIXES = ("_3d", "3d", "_solid", "_feature", "_prim", "_primitive", "_shape")


def _feature_type_vocab() -> tuple[frozenset, bool]:
    """(the engine's feature types, is-it-live?).

    Sourced from `doc_list_ops` via op_vocab — the same single source the docstring's OP
    VOCABULARY block quotes — with op_registry's COMMON_FEATURE_TYPES as the offline
    fallback. The flag matters: the fallback is a SUBSET, so an unknown type may only be
    REJECTED here when the list came from the live engine.
    """
    types: list = []
    try:
        from services.mcp import op_vocab as _ov  # noqa: PLC0415
        types = [str(t) for t in _ov.feature_types() if t]
    except Exception:  # noqa: BLE001
        types = []
    fallback: list = []
    try:
        from services.mcp.op_registry import COMMON_FEATURE_TYPES  # noqa: PLC0415
        fallback = list(COMMON_FEATURE_TYPES)
    except Exception:  # noqa: BLE001
        fallback = []
    live = bool(types) and set(types) != set(fallback)
    return frozenset(types or fallback), live


def _doc_op_vocab() -> frozenset:
    """The engine's op names (doc_list_ops, op_registry fallback)."""
    try:
        from services.mcp import op_vocab as _ov  # noqa: PLC0415
        ops = [str(o) for o in _ov.doc_ops() if o]
        if ops:
            return frozenset(ops)
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.mcp.op_registry import DOC_OP_KINDS  # noqa: PLC0415
        return frozenset(DOC_OP_KINDS)
    except Exception:  # noqa: BLE001
        return frozenset()


def _slug(word: Any) -> str:
    return str(word or "").strip().lower().replace("-", "_").replace(" ", "_")


def _canon_feature_type(word: Any, vocab: frozenset) -> Optional[str]:
    """A word -> an engine feature type, or None. Pure (alias table + suffix noise)."""
    w = _slug(word)
    if not w:
        return None
    if w in vocab:
        return w
    if w in _FEATURE_TYPE_ALIASES:
        return _FEATURE_TYPE_ALIASES[w]
    for suf in _TYPE_NOISE_SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf):
            base = w[: -len(suf)]
            if base in vocab:
                return base
            if base in _FEATURE_TYPE_ALIASES:
                return _FEATURE_TYPE_ALIASES[base]
    if w.endswith("s") and len(w) > 1:  # "boxes" is not a thing, but "holes" is written
        base = w[:-1]
        if base in vocab:
            return base
        if base in _FEATURE_TYPE_ALIASES:
            return _FEATURE_TYPE_ALIASES[base]
    return None


def _feature_type_error(raw: Any, vocab: frozenset) -> str:
    """The unknown-feature-type message: closest matches FIRST, then the real vocabulary."""
    import difflib  # noqa: PLC0415 — failure path only
    known = sorted(vocab)
    near = difflib.get_close_matches(_slug(raw), known, n=4, cutoff=0.4)
    head = f'add_feature: "{raw}" is not a feature type.'
    if near:
        head += " Closest: " + ", ".join(near) + "."
    return (f"{head} A feature type goes in feature.type, never in op — "
            f'{{"op":"add_feature","feature":{{"type":"box","name":"Block",'
            f'"params":{{…}}}}}}. Feature types: ' + ", ".join(known) + ".")


def _retype_feature(feat: dict, vocab: frozenset, live: bool, notes: list) -> dict:
    """Map a natural type word onto an engine feature type. Identity when already canonical."""
    raw = feat.get("type")
    if isinstance(raw, str) and raw.strip() in vocab:
        return feat  # canonical — byte-identical
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            'add_feature: the feature has no "type". Every feature needs one, e.g. '
            '{"op":"add_feature","feature":{"type":"cylinder","name":"Shaft",'
            '"params":{"radius":10,"height":40}}}. Feature types: '
            + ", ".join(sorted(vocab)) + ".")

    word = _slug(raw)
    if word in _BOOLEAN_AS_TYPE:
        raise ValueError(
            f'add_feature: "{raw}" is a boolean OPERATION, not a feature type. Subtract by '
            f'building the SAME shape with mode:"cut" (e.g. {{"type":"cylinder","params":'
            f'{{"radius":10,"height":40}},"at":[0,0,0],"mode":"cut"}}), fuse with '
            f'mode:"boss" (the default), or combine two existing bodies with '
            f'{{"type":"boolean","params":{{"op":"cut","a":"f1","b":"f2"}}}}.')

    base, cut = word, False
    for pre in _CUT_TYPE_PREFIXES:
        if base.startswith(pre) and len(base) > len(pre):
            base, cut = base[len(pre):], True
            break
    else:
        for suf in _CUT_TYPE_SUFFIXES:
            if base.endswith(suf) and len(base) > len(suf):
                base, cut = base[: -len(suf)], True
                break

    ftype = _canon_feature_type(base, vocab)
    if ftype is None:
        if live:
            raise ValueError(_feature_type_error(raw, vocab))
        return feat  # offline vocabulary is a subset — let the engine be the authority

    out = dict(feat)
    out["type"] = ftype
    params = dict(out["params"]) if isinstance(out.get("params"), dict) else {}
    extra = ""
    if cut or _TYPE_ALIAS_MODE.get(word) == "cut":
        if out.get("mode") is None and params.get("mode") is None:
            out["mode"] = "cut"
            extra = ' with mode:"cut"'
    style = _TYPE_ALIAS_HOLE_STYLE.get(word)
    if style and ftype == "hole" and params.get("style") is None:
        params["style"] = style
        extra += f' (style "{style}")'
    if params or isinstance(out.get("params"), dict):
        out["params"] = params
    if word in ("tube", "pipe"):
        extra += " — a hollow tube also needs a concentric mode:\"cut\" cylinder for its bore"
    notes.append(f'{_feat_label(feat, ftype)}: interpreted type "{raw}" as "{ftype}"{extra}')
    return out


def _normalize_feature_types(ops: Any, notes: list) -> Any:
    """Map every add_feature's `type` onto the engine vocabulary. Runs FIRST, because the
    sketch/primitive/dimension normalizers downstream all branch on the canonical type —
    a sketch spelled "profile" used to slip past the sketch normalizer entirely."""
    if not isinstance(ops, list):
        return ops
    vocab, live = _feature_type_vocab()
    out = []
    for op in ops:
        if (isinstance(op, dict) and op.get("op") == "add_feature"
                and isinstance(op.get("feature"), dict)):
            new = _retype_feature(op["feature"], vocab, live, notes)
            op = op if new is op["feature"] else {**op, "feature": new}
        out.append(op)
    return out


# The op keys that name the feature an op acts on. The canonical spelling is `id`;
# `feature_id` in particular produced the silent «delete_feature: no feature ''».
_OP_TARGET_ALIASES = ("feature_id", "featureId", "feature", "target", "target_id", "fid")
_OP_TARGET_OPS = frozenset({
    "delete_feature", "set_suppressed", "reorder_feature", "rename", "set_folder",
    "copy_feature", "edit_feature", "set_rollback",
})


def _normalize_op_elements(ops: list, notes: list) -> list:
    """Give every batch element a valid `op`. Raises ValueError with a teaching message
    for an element that names neither an op nor a feature type."""
    ftypes, _live = _feature_type_vocab()
    docops = _doc_op_vocab()
    out: list = []
    for i, el in enumerate(ops):
        if not isinstance(el, dict):
            pair = (' A ["<name>", "<value>"] pair is not an op: the op name and its data '
                    "live in ONE object, keyed."
                    if isinstance(el, (list, tuple)) else "")
            raise ValueError(
                f"ops[{i}] is {type(el).__name__}, not an object.{pair} Every element is "
                f'either {{"op":"<op name>", …}} from the op vocabulary or a bare feature '
                f'{{"type":"box","name":"Block","params":{{"length":40,"width":30,'
                f'"height":20}}}} (read as add_feature). Got: {el!r}')
        name = el.get("op")
        if isinstance(name, str) and name.strip():
            kind = name.strip()
            # {"op":"add_feature","type":"sketch","refs":{…}} — the feature keys sit BESIDE
            # the op instead of inside a "feature" object. Unambiguous, so wrap them.
            if (kind == "add_feature" and not isinstance(el.get("feature"), dict)
                    and el.get("type") is not None):
                order = {k: el[k] for k in _OP_ORDER_KEYS if k in el}
                inner = {k: v for k, v in el.items()
                         if k != "op" and k not in _OP_ORDER_KEYS and k != "feature"}
                out.append({"op": "add_feature", "feature": inner, **order})
                notes.append('read the feature keys sitting beside op:"add_feature" as its '
                             '"feature" object — an add_feature op is '
                             '{"op":"add_feature","feature":{"type":…,"params":{…}}}')
                continue
            if kind in docops or kind == "new_part":
                out.append(_normalize_op_target(el, kind, notes))
                continue
            ftype = _canon_feature_type(kind, ftypes)
            if ftype:
                rest = {k: v for k, v in el.items() if k != "op"}
                inner = rest.pop("feature", None)
                feature = dict(inner) if isinstance(inner, dict) else rest
                feature.setdefault("type", ftype)
                out.append({"op": "add_feature", "feature": feature})
                notes.append(
                    f'interpreted op:"{kind}" as {{"op":"add_feature","feature":'
                    f'{{"type":"{ftype}",…}}}} — a feature type is not an op name')
                continue
            out.append(el)  # a genuinely unknown op: the engine names it (enrich_unknown_op)
            continue
        if isinstance(el.get("feature"), dict):
            out.append({"op": "add_feature", **el})
            notes.append('read an element carrying a "feature" but no "op" as op:"add_feature"')
            continue
        if el.get("type") is not None:
            out.append({"op": "add_feature", "feature": el})
            notes.append(
                'read bare feature dict(s) in ops as {"op":"add_feature","feature":{…}} — '
                'every ops element needs an "op"')
            continue
        raise ValueError(
            f'ops[{i}] has no "op". An ops element is either {{"op":"<name>", …}} from the op '
            f'vocabulary or a bare feature {{"type":"<feature type>","name":…,"params":{{…}}}} '
            f"(read as add_feature). Got keys: {sorted(el)}.")
    return out


def _normalize_op_target(op: dict, kind: str, notes: list) -> dict:
    """`{"op":"delete_feature","feature_id":"f2"}` -> the canonical `id` key."""
    if kind not in _OP_TARGET_OPS or op.get("id") is not None:
        return op
    for alias in _OP_TARGET_ALIASES:
        v = op.get(alias)
        if isinstance(v, str) and v.strip():
            out = dict(op)
            out.pop(alias, None)
            out["id"] = v.strip()
            notes.append(f'{kind}: interpreted {alias}="{v}" as id="{v}" '
                         f"— the op addresses its feature with `id`")
            return out
    return op


# ---------------------------------------------------------------------------
# VALIDATION-MISS normalizer (model-robustness).
#
# These are the schema requirements a model reliably forgets, every one of them
# recovered from a real rejected batch in the run ledgers:
#   hole      params.style + params.end are REQUIRED and their vocabulary is
#             simple|tap|cbore|csink / through_all|blind (core/doc/document.cpp) — the
#             model writes "counterbore"/"through", or omits them entirely; it also omits
#             refs.face, which the compiler needs to know WHICH face to drill.
#   sketch    refs.plane must be a RESERVED DATUM, not "XZ" (handled in the sketch pass).
#   boolean   params.op must be fuse|cut|common with operands a/b, not
#             params.operation:"union" + params.targets:[…].
#   extrude/  params.mode is required, and the profile is a refs.region selector, not a
#   revolve   params.sketch name / an inline polyline.
#   any       at/axis/origin take a plain [x,y,z]; only SIZES take {"value":N,"expr":…}.
# Each rewrite is echoed in `interpreted`; anything genuinely ambiguous raises a teaching
# ValueError instead of guessing.
_HOLE_STYLES = frozenset({"simple", "tap", "cbore", "csink"})
_HOLE_STYLE_WORDS = {
    "simple": "simple", "plain": "simple", "straight": "simple", "drilled": "simple",
    "clearance": "simple", "standard": "simple", "normal": "simple", "none": "simple",
    "cbore": "cbore", "counterbore": "cbore", "c_bore": "cbore", "counter_bore": "cbore",
    "counterbored": "cbore", "socket": "cbore", "spotface": "cbore",
    "csink": "csink", "countersink": "csink", "c_sink": "csink", "counter_sink": "csink",
    "countersunk": "csink", "csk": "csink", "flathead": "csink", "chamfered": "csink",
    "tap": "tap", "tapped": "tap", "threaded": "tap", "thread": "tap",
}
_HOLE_ENDS = frozenset({"through_all", "blind"})
_HOLE_END_WORDS = {
    "through_all": "through_all", "through": "through_all", "thru": "through_all",
    "throughall": "through_all", "through_hole": "through_all", "all": "through_all",
    "full": "through_all", "fully": "through_all", "open": "through_all",
    "blind": "blind", "to_depth": "blind", "depth": "blind", "partial": "blind",
    "closed": "blind", "stopped": "blind",
}
_HOLE_STYLE_KEYS = ("style", "hole_style", "hole_type")
_HOLE_END_KEYS = ("end", "end_condition", "termination", "depth_type", "end_type")
_HOLE_DIR_KEYS = ("direction", "dir", "normal", "drill_direction")
_HOLE_AT_KEYS = ("origin", "position", "location", "at", "center", "centre")
# The face word a drill axis names. The SIGN is deliberately dropped: a bore along Z is
# entered from the top face in every real part, and the note says so.
_AXIS_FACE_WORD = ("right", "back", "top")


def _vec3(v: Any) -> Optional[list]:
    if isinstance(v, list) and len(v) == 3 and all(_is_num(x) for x in v):
        return [float(x) for x in v]
    return None


def _face_word_for_axis(v: Any) -> Optional[str]:
    vec = _vec3(v)
    if vec is None:
        if isinstance(v, str):
            w = _slug(v).lstrip("+-")
            return {"x": "right", "y": "back", "z": "top"}.get(w)
        return None
    dom = max(range(3), key=lambda i: abs(vec[i]))
    return _AXIS_FACE_WORD[dom] if abs(vec[dom]) > 0 else None


def _normalize_hole_feature(feat: dict, notes: list) -> dict:
    """style/end defaults + synonyms, and the face the bore is entered through."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, "hole")
    changed = False

    style_raw = _MISSING
    for k in _HOLE_STYLE_KEYS:
        v = consume(k)
        if style_raw is _MISSING and v is not _MISSING and v is not None:
            style_raw = v
    end_raw = _MISSING
    for k in _HOLE_END_KEYS:
        v = consume(k)
        if end_raw is _MISSING and v is not _MISSING and v is not None:
            end_raw = v

    # "depth":"through" — an END CONDITION written into the depth slot. The engine reads
    # `depth` as a number, so leaving it there is a hard parse failure later.
    depth_v = peek("depth")
    if isinstance(depth_v, str) and _slug(depth_v) in _HOLE_END_WORDS:
        if end_raw is _MISSING:
            end_raw = depth_v
        consume("depth")
        changed = True
        notes.append(f'{label}: read depth="{depth_v}" as the end condition, not a distance '
                     f'— a hole takes end:"through_all"|"blind" plus a numeric depth when blind')

    style = None
    if style_raw is not _MISSING:
        w = _slug(style_raw)
        if w in _HOLE_STYLE_WORDS:
            style = _HOLE_STYLE_WORDS[w]
            if style != str(style_raw).strip():
                changed = True
                notes.append(f'{label}: interpreted style "{style_raw}" as "{style}" '
                             f"— hole styles are simple|tap|cbore|csink")
        elif w in _HOLE_END_WORDS and end_raw is _MISSING:
            end_raw = style_raw
            changed = True
            notes.append(f'{label}: "{style_raw}" is an END condition, not a style — moved to '
                         f"end; style defaulted to simple")
        else:
            raise ValueError(
                f'hole "{label}": style "{style_raw}" is not a hole style. Use simple (a plain '
                f'bore), tap (tapped), cbore (counterbored) or csink (countersunk).')
    if style is None:
        if peek("cbore_d") is not _MISSING or peek("cbore_depth") is not _MISSING:
            style = "cbore"
        elif peek("csink_d") is not _MISSING or peek("csink_angle") is not _MISSING:
            style = "csink"
        elif peek("tap") is not _MISSING or peek("thread") is not _MISSING:
            style = "tap"
        else:
            style = "simple"
        changed = True
        notes.append(f'{label}: params.style is required for a hole — defaulted to "{style}" '
                     f"(simple|tap|cbore|csink)")

    end = None
    if end_raw is not _MISSING:
        w = _slug(end_raw)
        if w in _HOLE_END_WORDS:
            end = _HOLE_END_WORDS[w]
            if end != str(end_raw).strip():
                changed = True
                notes.append(f'{label}: interpreted end "{end_raw}" as "{end}" — a hole ends '
                             f'"through_all" or "blind"')
        else:
            raise ValueError(
                f'hole "{label}": end "{end_raw}" is not an end condition. Use '
                f'end:"through_all" (drill straight through) or end:"blind" with a numeric '
                f"params.depth.")
    if end is None:
        depth_now = peek("depth")
        end = "blind" if (_is_num(depth_now) or _is_dim_object(depth_now)) else "through_all"
        changed = True
        notes.append(f'{label}: params.end is required for a hole — defaulted to "{end}"'
                     + (" (a depth was given)" if end == "blind" else " (no depth was given)"))
    if end == "blind" and not (_is_num(peek("depth")) or _is_dim_object(peek("depth"))):
        raise ValueError(
            f'hole "{label}": end:"blind" needs a numeric params.depth (mm). Give the depth, '
            f'or use end:"through_all" to drill through.')

    params["style"] = style
    params["end"] = end

    # A drill DIRECTION names the entry face: that is a rewrite of what the model wrote, so
    # it belongs here. Supplying a face nobody named is a different decision — see
    # _supply_missing_refs, which add_features runs on its own.
    refs = dict(out.get("refs") or {})
    side = None
    for k in _HOLE_DIR_KEYS:
        v = consume(k)
        if v is not _MISSING and v is not None and side is None:
            side = _face_word_for_axis(v)
            changed = True
    if side and not refs.get("face") and not refs.get("positions_sketch"):
        refs["face"] = {"of": "face", "side": side}
        changed = True
        notes.append(f'{label}: interpreted the drill direction as the {side} face '
                     f'(refs.face={{"of":"face","side":"{side}"}}) — a bore is entered '
                     f"through a named planar face, not along a world vector")

    # A world XYZ where the hole's FACE-LOCAL [u,v] positions belong.
    if not refs.get("positions") and params.get("positions") is None:
        for k in _HOLE_AT_KEYS:
            v = peek(k)
            vec = _vec3(v)
            if vec is None:
                continue
            consume(k)
            changed = True
            u, w2 = vec[0], vec[1]
            if abs(u) < 1e-9 and abs(w2) < 1e-9:
                notes.append(f'{label}: dropped {k}={vec} — hole positions are FACE-LOCAL '
                             f"[u,v] pairs and [0,0] is the face centre (the default)")
            else:
                params["positions"] = [[u, w2]]
                notes.append(f'{label}: interpreted {k}={vec} as positions=[[{u}, {w2}]] '
                             f"— hole positions are FACE-LOCAL [u,v] pairs on refs.face")
            break

    if not changed:
        return feat
    out["params"] = params
    out["refs"] = refs
    return out


_BOOLEAN_OP_WORDS = {
    "fuse": "fuse", "union": "fuse", "add": "fuse", "join": "fuse", "merge": "fuse",
    "combine": "fuse", "weld": "fuse", "boss": "fuse", "plus": "fuse",
    "cut": "cut", "subtract": "cut", "difference": "cut", "remove": "cut", "minus": "cut",
    "common": "common", "intersect": "common", "intersection": "common",
}
_BOOLEAN_OP_KEYS = ("op", "operation", "boolean", "mode", "action", "kind")
_BOOLEAN_OPERAND_KEYS = ("targets", "bodies", "operands", "inputs", "features", "tools")
# The SINGULAR operand pair. `{"op":"boolean","target":"body","tool":"boss","kind":"union"}`
# is the flat op a model writes for a boolean (live run 2026-07-27) and it died on «needs
# its two operands» because only the plural LIST spellings above were read. target = the
# body that is kept (params.a), tool = the body applied to it (params.b) — the roles are
# named, so there is nothing to guess.
_BOOLEAN_TARGET_KEYS = ("target", "targets", "base", "target_body", "base_body")
_BOOLEAN_TOOL_KEYS = ("tool", "tools", "tool_body", "cutter")


def _operand_name(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("owner_feature", "feature", "id", "body", "name"):
            s = v.get(k)
            if isinstance(s, str) and s.strip():
                return s.strip()
    return None


def _one_operand(v: Any) -> Optional[str]:
    """One operand name from a body reference, or a one-element list holding one."""
    if isinstance(v, list):
        return _one_operand(v[0]) if len(v) == 1 else None
    return _operand_name(v)


def _normalize_boolean_feature(feat: dict, notes: list) -> dict:
    """params.operation:"union" + targets:[a,b] -> params.op:"fuse" + params.a/b."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, "boolean")
    changed = False

    raw_op = _MISSING
    for k in _BOOLEAN_OP_KEYS:
        v = peek(k)
        if v is _MISSING or v is None:
            continue
        if k != "op":
            consume(k)
            changed = True
        raw_op = v
        break
    if raw_op is not _MISSING:
        word = _slug(raw_op)
        canon = _BOOLEAN_OP_WORDS.get(word)
        if canon is None:
            raise ValueError(
                f'boolean "{label}": op "{raw_op}" is not a boolean operation. Use '
                f'"fuse" (union), "cut" (subtract) or "common" (intersect).')
        if canon != str(raw_op).strip() or params.get("op") != canon:
            changed = changed or params.get("op") != canon
            if params.get("op") != canon:
                notes.append(f'{label}: interpreted boolean op "{raw_op}" as "{canon}" '
                             f"— the engine's operations are fuse|cut|common")
        params["op"] = canon

    if params.get("a") is None or params.get("b") is None:
        operands: list = []
        for k in _BOOLEAN_OPERAND_KEYS:
            v = peek(k)
            if isinstance(v, list) and len(v) >= 2:
                operands = [_operand_name(x) for x in v[:2]]
                consume(k)
                break
        if not operands:
            tv = (out.get("refs") or {}).get("targets")
            if isinstance(tv, list) and len(tv) >= 2:
                operands = [_operand_name(x) for x in tv[:2]]
        pair_note = None
        if not operands:
            tgt = next((k for k in _BOOLEAN_TARGET_KEYS
                        if _one_operand(peek(k)) is not None), None)
            tol = next((k for k in _BOOLEAN_TOOL_KEYS
                        if _one_operand(peek(k)) is not None), None)
            if tgt and tol:
                operands = [_one_operand(consume(tgt)), _one_operand(consume(tol))]
                pair_note = (f'{label}: read {tgt}="{operands[0]}" as params.a (the body '
                             f'kept) and {tol}="{operands[1]}" as params.b (the body '
                             f"applied to it) — a boolean's operands are params.a/params.b")
        if len(operands) == 2 and all(operands):
            params["a"], params["b"] = operands
            changed = True
            notes.append(pair_note or
                         f'{label}: interpreted the operand list as a="{operands[0]}", '
                         f'b="{operands[1]}" — a boolean fuses/cuts exactly two bodies')
        elif params.get("op") is not None:
            raise ValueError(
                f'boolean "{label}": needs its two operands — params.a and params.b, each a '
                f'feature id ("f1") or a same-batch placeholder ("$0"). Build both bodies with '
                f'"merge":false first so they stay separate solids.')
    if not changed:
        return feat
    out["params"] = params
    return out


_PROFILE_MODE_TYPES = frozenset({"extrude", "revolve", "loft", "sweep"})
_PROFILE_SOURCE_KEYS = ("profile", "sketch", "section", "profile_sketch", "sketch_id",
                        "source_sketch", "sections")
# A loft's profiles live ONLY in refs.sections (validate.cpp: SelList, required). The
# plural spellings a model reaches for — profiles / sketches — name that same slot, so
# they are unambiguous aliases; a genuinely unknown slot keeps the engine's error.
_LOFT_SECTION_ALIASES = ("profiles", "sketches")
# Dict keys that wrap the real list one level too deep ({"sections":[…]}).
_LOFT_SECTION_WRAP_KEYS = ("sections",) + _LOFT_SECTION_ALIASES


def _loft_section_list(v: Any) -> Optional[list]:
    """A loft sections value in any unambiguous dialect -> the canonical selector LIST.

    A bare string is one sketch name; a selector dict is ONE section (the value should
    have been a list); a dict whose only key is sections/profiles/sketches is the list
    nested one level too deep. None when the shape says nothing certain — an empty list,
    a dict that is neither wrapper nor selector, a non-string/selector member — so the
    engine's error names it instead of this inventing sections.
    """
    if isinstance(v, str):
        name = v.strip()
        return [{"kind": "region", "sketch": name, "hint_index": 0}] if name else None
    if isinstance(v, dict):
        wrap = [k for k in _LOFT_SECTION_WRAP_KEYS if isinstance(v.get(k), list)]
        if len(wrap) == 1 and set(v) == {wrap[0]}:
            return _loft_section_list(v[wrap[0]])
        if v.get("kind") or v.get("of") or isinstance(v.get("sketch"), str):
            return [v]
        return None
    if isinstance(v, list):
        if not v:
            return None
        out: list = []
        for el in v:
            if isinstance(el, str) and el.strip():
                out.append({"kind": "region", "sketch": el.strip(), "hint_index": 0})
            elif isinstance(el, dict):
                out.append(el)
            else:
                return None
        return out
    return None
_AXIS_SOURCE_TYPES = frozenset({"revolve", "circular_pattern", "surf_revolve", "helix"})


def _is_inline_geometry(v: Any) -> bool:
    """True when a profile source carries COORDINATES instead of naming a sketch.

    A sketch is named ("Profile", "f1", "$0"); anything holding numbers is geometry
    the model inlined. Both shapes it writes count: {"points": …}/{"entities": …} and
    the bare point list [[335,0],[345,0],…] — the second used to slip through and
    resurface as the unhelpful "refs.profile: required for revolve"."""
    if isinstance(v, dict):
        return bool(v.get("points") or v.get("entities"))
    if isinstance(v, list):
        return any(isinstance(p, (list, tuple)) and len(p) >= 2
                   and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           for c in p[:2])
                   for p in v)
    return False


# The datum plane to sketch a profile on for a revolve about each axis: the plane must
# CONTAIN the axis (revolving about a plane's own NORMAL sweeps nothing and the engine
# rejects it as a zero-volume solid). Verified live against all nine combinations.
_PLANE_CONTAINING = {"axis.x": "plane.top", "axis.y": "plane.right", "axis.z": "plane.front"}


def _normalize_profile_feature(feat: dict, ftype: str, notes: list) -> dict:
    """`params.sketch:"Profile"` + `params.axis:"Z"` -> the refs the engine requires."""
    out, params, peek, consume = _split_two_levels(feat)
    refs = dict(out.get("refs") or {})
    label = _feat_label(feat, ftype)
    changed = False

    if not refs.get("profile") and not refs.get("sections"):
        keys = _PROFILE_SOURCE_KEYS + (
            ("profiles", "sketches") if ftype == "loft" else ())
        for k in keys:
            v = peek(k)
            if v is _MISSING or v is None:
                continue
            if _is_inline_geometry(v):
                av = peek("axis")
                plane = _PLANE_CONTAINING.get(
                    _norm_datum(av) if isinstance(av, str) else "", "plane.top")
                raise ValueError(
                    f'{ftype} "{label}": params.{k} carries INLINE geometry. A profile is '
                    f"always a sketch FEATURE, so this needs two ops in the same batch — "
                    f'{{"op":"add_feature","feature":{{"type":"sketch","name":"{label}Profile",'
                    f'"params":{{"plane":"{plane}","entities":[{{"type":"line","p1":[x1,y1],'
                    f'"p2":[x2,y2]}}, …closing the loop]}}}}}} first, then this {ftype} with '
                    f'refs.profile=[{{"kind":"region","sketch":"$<index of that sketch op>",'
                    f'"hint_index":0}}] and no params.{k}. Sketch coordinates are 2D on that '
                    f'plane — "{plane}" is one that CONTAINS the axis, which a revolve needs.')
            if ftype == "loft" and isinstance(v, (list, dict)):
                canon = _loft_section_list(v)
                if canon is None:
                    continue  # ambiguous shape — the engine names it better than a guess
                consume(k)
                refs["sections"] = canon
                changed = True
                notes.append(
                    f'{label}: interpreted params.{k} as refs.sections ({len(canon)} '
                    f"region selector(s)) — a loft's profiles are a LIST of "
                    f'{{"kind":"region","sketch":…}} selectors under refs.sections, '
                    f"not a params.{k} value")
                break
            if isinstance(v, str) and v.strip():
                consume(k)
                slot = "sections" if ftype == "loft" else "profile"
                refs[slot] = [{"kind": "region", "sketch": v.strip(), "hint_index": 0}]
                changed = True
                notes.append(
                    f'{label}: interpreted params.{k}="{v}" as refs.{slot}='
                    f'[{{"kind":"region","sketch":"{v}","hint_index":0}}] — a profile is a '
                    f"region selector on a sketch feature, not a param")
                break

    if ftype in _AXIS_SOURCE_TYPES and not refs.get("axis"):
        v = peek("axis")
        datum = _norm_datum(v) if isinstance(v, str) else None
        if datum in _DATUM_TOKENS:
            consume("axis")
            refs["axis"] = datum
            changed = True
            notes.append(f'{label}: interpreted params.axis="{v}" as refs.axis="{datum}" '
                         f"— the axis of revolution is a datum REF, not a param")

    if ftype in _PROFILE_MODE_TYPES and params.get("mode") is None:
        params["mode"] = "cut" if out.get("mode") == "cut" else "boss"
        out.pop("mode", None)
        changed = True
        notes.append(f'{label}: params.mode is required for a {ftype} — defaulted to '
                     f'"{params["mode"]}" (boss adds material, cut removes it)')

    # A revolve with no angle means a FULL revolution — the single most common revolve
    # intent (a bucket, a knob, a shaft); omitting it used to bounce the whole batch on
    # "params.angle: required for revolve" (live copilot run, 2026-07-27).
    if ftype == "revolve" and params.get("angle") is None:
        params["angle"] = 360
        changed = True
        notes.append(f'{label}: params.angle is required for a revolve — defaulted to 360 '
                     f'(a full revolution; pass angle=180 etc. for a partial one)')

    if not changed:
        return feat
    out["params"] = params
    out["refs"] = refs
    return out


# A ref slot -> the selector kind its members must carry.
_REF_SLOT_KIND = {
    "face": "face", "faces": "face", "target_face": "face", "middle_face": "face",
    "face_set_a": "face", "face_set_b": "face", "remove": "face", "parting": "face",
    "neutral": "face",
    "edges": "edge", "edge": "edge", "edge_a": "edge", "edge_b": "edge",
    "vertices": "vertex",
    "targets": "body", "tools": "body", "bodies": "body",
}
# Slots whose bare-string value must be a reserved datum / feature id.
_DATUM_REF_SLOTS = frozenset({"plane", "axis", "dir1", "dir2", "pull", "base", "path"})
# Ref slots the engine types Sel / SelOrDatum (validate.cpp::FEATURE_TYPES) — ONE selector
# object or datum string, never a list. A model that wraps the single selector in a list
# ("axis":[{"kind":"datum",…}]) died on «selector must be an object»; a ONE-element list
# says exactly one thing, so unwrap it. A multi-element list keeps the error — that is a
# real disagreement about the slot, not a spelling.
_SINGLE_REF_SLOTS = frozenset({
    "plane", "axis", "base", "second", "a", "b", "target", "origin", "x_dir", "y_dir",
    "offset", "at", "up_to", "up_to2", "start_ref", "end_ref", "end2_ref", "path",
    "centerline", "target_face", "middle_face", "reference_face", "neutral", "pull",
    "face", "positions_sketch", "dir1", "dir2", "edge", "sketch",
})
# The keys that make a dict a geometry QUERY rather than a topology selector.
_QUERY_KEYS = frozenset({"side", "surf", "nearest", "largest", "smallest", "index", "at",
                         "all", "where", "contains", "min", "max"})


def _repair_selector(v: Any, kind: str) -> Any:
    """Give a kind-less ref member the shape validate_selector / validate_geom_query wants."""
    if isinstance(v, str) and v.strip() and kind == "body":
        return {"kind": "body", "owner_feature": v.strip()}
    if not isinstance(v, dict) or v.get("kind") or v.get("of"):
        return v
    if kind in ("face", "edge") and (set(v) & _QUERY_KEYS):
        return {"of": kind, **v}
    owner = _operand_name(v)
    if owner and kind == "body":
        return {"kind": "body", "owner_feature": owner}
    return v


def _repair_datum_selector(v: Any) -> Optional[dict]:
    """`{"kind":"datum","id":"Z"}` -> `{"kind":"datum","feature":"axis.z"}`.

    A datum selector names its datum under `feature` and validate_selector accepts nothing
    else; models reach for id/name/ref. None when there is nothing unambiguous to fix —
    already canonical, not a datum selector, or a word that is no datum the engine knows.
    """
    if not isinstance(v, dict) or _slug(v.get("kind")) != "datum":
        return None
    if _is_canon_datum(v.get("feature")):
        return None
    for k in ("feature", "datum", "id", "name", "ref"):
        token = _norm_datum(v.get(k))
        if token and _is_canon_datum(token):
            out = dict(v)
            if k != "feature":
                out.pop(k, None)
            out["feature"] = token
            return out
    return None


def _normalize_feature_refs(feat: dict, ftype: str, notes: list) -> dict:
    """Datum words + kind-less selectors in `refs`. Identity when everything is canonical."""
    refs = feat.get("refs")
    if not isinstance(refs, dict) or not refs:
        return feat
    label = _feat_label(feat, ftype)
    out_refs = dict(refs)
    changed = False
    for slot, v in refs.items():
        if (slot in _SINGLE_REF_SLOTS and isinstance(v, list) and len(v) == 1
                and isinstance(v[0], (dict, str))):
            v = v[0]
            out_refs[slot] = v
            changed = True
            notes.append(f"{label}: unwrapped the one-element list in refs.{slot} — that "
                         f"slot takes ONE selector; only a multi-selector slot "
                         f"(refs.profile, refs.edges, refs.faces) is a list")
        if slot in _DATUM_REF_SLOTS:
            if isinstance(v, str):
                datum = _norm_datum(v)
                if datum != v:
                    out_refs[slot] = datum
                    changed = True
                    notes.append(f'{label}: interpreted refs.{slot}="{v}" as the reserved '
                                 f'datum "{datum}" — the engine knows plane.top/plane.front/'
                                 f"plane.right and axis.x/axis.y/axis.z")
                continue
            fixed = _repair_datum_selector(v)
            if fixed is not None:
                out_refs[slot] = fixed
                changed = True
                notes.append(f"{label}: completed the datum selector in refs.{slot} into "
                             f'{{"kind":"datum","feature":"{fixed["feature"]}"}} — a datum '
                             f"selector names its datum under `feature`")
            continue
        kind = _REF_SLOT_KIND.get(slot)
        if kind is None:
            continue
        if isinstance(v, list):
            new = [_repair_selector(x, kind) for x in v]
            if new != v:
                out_refs[slot] = new
                changed = True
                notes.append(f'{label}: completed refs.{slot} into {kind} selectors — every '
                             f'member needs a "kind" (or an "of" geometry query)')
        else:
            new = _repair_selector(v, kind)
            if new is not v and new != v:
                out_refs[slot] = new
                changed = True
                notes.append(f'{label}: read refs.{slot} as a {kind} query — a bare '
                             f'{{"side":…}} is not a topology selector')
    if ftype == "loft":
        # refs.sections is the ONLY profile slot a loft has — so refs.profiles /
        # refs.sketches can only mean it, a {"sections":[…]} wrapper is the list nested
        # one level too deep, and ONE bare selector/string is the list that should have
        # been. Remap/unwrap; anything ambiguous keeps the engine's error.
        src = "sections" if "sections" in out_refs else next(
            (a for a in _LOFT_SECTION_ALIASES if a in out_refs), None)
        if src is not None:
            canon = _loft_section_list(out_refs[src])
            if canon is not None and (src != "sections" or canon != out_refs[src]):
                if src != "sections":
                    out_refs.pop(src)
                out_refs["sections"] = canon
                changed = True
                notes.append(
                    f"{label}: read refs.{src} as refs.sections ({len(canon)} region "
                    f"selector(s)) — a loft's profiles are a LIST of "
                    f'{{"kind":"region","sketch":…}} selectors under refs.sections')
    if not changed:
        return feat
    out = dict(feat)
    out["refs"] = out_refs
    return out


# Keys whose value is a COORDINATE, never a dimension: a {"value":[x,y,z]} wrapper around
# one of these is rejected outright ("params.at.value: missing or non-numeric").
_VECTOR_KEYS = frozenset({
    "at", "at_offset", "axis", "axis_dir", "dir", "direction", "normal", "translate",
    "rotate", "origin", "position", "location", "center", "centre", "point", "up",
})


def _unwrap_vector_dims(feat: dict, ftype: str, notes: list) -> dict:
    """`at:{"value":[0,0,-7.5],"expr":null}` -> `at:[0,0,-7.5]`."""
    out = dict(feat)
    params = dict(out["params"]) if isinstance(out.get("params"), dict) else None
    changed = False
    for holder in (out, params):
        if holder is None:
            continue
        for k in list(holder):
            if k not in _VECTOR_KEYS:
                continue
            v = holder[k]
            if (isinstance(v, dict) and isinstance(v.get("value"), list)
                    and set(v) <= {"value", "expr", "unit"}):
                holder[k] = v["value"]
                changed = True
    if not changed:
        return feat
    if params is not None:
        out["params"] = params
    notes.append(f'{_feat_label(feat, ftype)}: unwrapped a {{"value":[…]}} around a coordinate '
                 f"— at/axis/origin take a plain [x,y,z]; only SIZES take "
                 f'{{"value":N,"expr":…}}')
    return out


# A placement written as a WORD. `at` accepts a [x,y,z] or a geometry query, never a bare
# adjective — "location":"center" reached the engine as at:"center" and died on
# "'center' is neither a reserved datum nor a feature id".
_AT_CENTER_WORDS = frozenset({"center", "centre", "origin", "middle", "centered", "centred",
                              "0,0,0", "world_origin"})
_AT_SIDE_WORDS = frozenset({"top", "bottom", "left", "right", "front", "back",
                            "+x", "-x", "+y", "-y", "+z", "-z"})


def _normalize_placement_word(feat: dict, ftype: str, notes: list) -> dict:
    """`at:"center"` -> [0,0,0]; `at:"top"` -> the top-face geometry query."""
    out, params, peek, _consume = _split_two_levels(feat)
    v = peek("at")
    if not isinstance(v, str) or not v.strip():
        return feat
    word = _slug(v)
    label = _feat_label(feat, ftype)
    if word in _AT_CENTER_WORDS:
        new: Any = [0, 0, 0]
        why = "the world origin (which is also the default)"
    elif word.replace("_", "") in _AT_SIDE_WORDS or word in _AT_SIDE_WORDS:
        side = word if word in _AT_SIDE_WORDS else word.replace("_", "")
        new = {"of": "face", "side": side}
        why = f'the centroid of the {side} face'
    else:
        return feat  # not a word we can honour — let the engine name it
    if "at" in out:
        out["at"] = new
    else:
        params["at"] = new
        out["params"] = params
    notes.append(f'{label}: interpreted at="{v}" as {why} — placement is [x,y,z] or a '
                 f'geometry query like {{"of":"face","side":"top"}}')
    return out


# The extrude family's param NAMES, verified against the keys document.cpp actually reads
# on an extrude feature (depth, depth2, draft, draft_outward, end, end2, end_offset, flip,
# mid_plane, mode, offset, start, start_offset, start_ref, thin, thin_side, thin_thickness,
# up_to). Anything else the model writes for "how far" or "both ways" is inert: the batch
# died on a raw `key 'depth' not found` kernel exception with nothing to learn from.
_EXTRUDE_TYPES = frozenset({"extrude", "surf_extrude"})
_EXTRUDE_DEPTH_ALIASES = ("distance", "length", "extent", "amount", "height", "thickness")
_MID_PLANE_ALIASES = ("symmetric", "symmetrical", "both_directions", "both_sides",
                      "midplane", "centered")
_EXTRUDE_END_WORDS = {"through": "through_all", "thru": "through_all", "all": "through_all",
                      "midplane": "mid_plane", "symmetric": "mid_plane"}


def _normalize_extrude_params(feat: dict, ftype: str, notes: list) -> dict:
    """distance/length -> depth, symmetric/both_directions -> mid_plane, through -> the end."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, ftype)
    changed = False

    if peek("depth") is _MISSING:
        for k in _EXTRUDE_DEPTH_ALIASES:
            v = peek(k)
            if v is _MISSING or v is None:
                continue
            if k == "thickness" and peek("thin") not in (_MISSING, None, False):
                continue  # a THIN feature's wall thickness (thin_thickness), not the run
            consume(k)
            params["depth"] = v
            changed = True
            notes.append(f'{label}: interpreted {k}={v!r} as params.depth — an {ftype} is '
                         f"dimensioned by `depth` along its direction")
            break

    for k in _MID_PLANE_ALIASES:
        v = peek(k)
        if v is _MISSING or v is None:
            continue
        consume(k)
        changed = True
        if peek("mid_plane") is _MISSING:
            params["mid_plane"] = bool(v)
            notes.append(f'{label}: interpreted {k}={v!r} as params.mid_plane — the run is '
                         f"split evenly either side of the sketch plane")

    end_v = peek("end")
    if isinstance(end_v, str) and _slug(end_v) in _EXTRUDE_END_WORDS:
        canon = _EXTRUDE_END_WORDS[_slug(end_v)]
        params["end"] = canon
        out.pop("end", None)
        changed = True
        notes.append(f'{label}: interpreted end "{end_v}" as "{canon}" — an {ftype} ends '
                     f'"blind" (depth), "through_all" or "mid_plane"')

    if not changed:
        return feat
    out["params"] = params
    return out


# Sketching on a FACE is core CAD and the engine supports it, but a ref slot only takes a
# reserved datum, an f<N> id, a "$N" placeholder or a selector OBJECT — so the bare stable
# sid describe_part/query_geometry hands the model back ("Ffa9a6e") is rejected outright.
# The sid grammar and the kind its leading letter implies are cad_common's (_SID_RE /
# _sid_match, core/doc/selectors.cpp::stable_sid); wrap it into the selector the slot wants,
# and VERIFY it against the live document first — a stale sid must name what does exist.
_SID_KINDS = {"f": "face", "e": "edge", "v": "vertex"}
_SID_REF_SLOTS = frozenset({
    "plane", "face", "faces", "target_face", "middle_face", "face_set_a", "face_set_b",
    "remove", "parting", "neutral", "reference_face", "edges", "edge", "edge_a", "edge_b",
    "vertices", "surface", "tool", "up_to", "start_ref", "end_ref", "base", "path", "axis",
})


def _bare_sid(v: Any) -> Optional[str]:
    """The stable sid this ref value IS, or None (a datum / f<N> id / selector is not one)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if s in _DATUM_TOKENS or _BATCH_REF_RE.match(s):
        return None
    return s if _SID_RE.fullmatch(s) else None


def _walk_sid_refs(ops: Any):
    """Yield (refs dict, slot, index-or-None, sid) for every bare sid sitting in a ref slot."""
    for op in ops if isinstance(ops, list) else ():
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            continue
        for slot, v in refs.items():
            if slot not in _SID_REF_SLOTS:
                continue
            if isinstance(v, list):
                for i, x in enumerate(v):
                    sid = _bare_sid(x)
                    if sid:
                        yield refs, slot, i, sid
            else:
                sid = _bare_sid(v)
                if sid:
                    yield refs, slot, None, sid


def _sid_roster(doc_id: str, kind: str, limit: int = 12) -> list:
    """The document's live sids of one kind, for a teaching error."""
    try:
        res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": kind}}) or {}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in res.get("matches") or []:
        sid = m.get("sid")
        if not sid:
            continue
        bits = [b for b in (m.get("side"), m.get("surf")) if b]
        out.append(f"{sid} ({'/'.join(bits)})" if bits else str(sid))
        if len(out) >= limit:
            break
    return out


def _resolve_sid_refs(ops: Any, doc_id: str, notes: list) -> Any:
    """Wrap a bare stable sid in a ref slot into the selector the engine accepts.

    Costs one doc_query per sid KIND, and only when a bare sid is actually present — a
    canonical batch never touches the engine here. A sid the document does not carry is a
    teaching error naming the live roster, never a blind wrap (sids change on every edit).
    """
    found = list(_walk_sid_refs(ops))
    if not found:
        return ops
    resolved: dict = {}
    for _refs, slot, _i, sid in found:
        if sid in resolved:
            continue
        m = _sid_match(doc_id, sid)
        if m is None:
            kind = _SID_KINDS.get(sid[:1].lower(), "face")
            roster = _sid_roster(doc_id, "face" if kind == "vertex" else kind)
            extra = (" Vertex sids are not selectable — name the face or edge that carries it."
                     if kind == "vertex" else "")
            raise ValueError(
                f'refs.{slot}: no {kind} with sid "{sid}" in this document (sids change '
                f"whenever the model changes).{extra} Live {kind} sids: "
                + (", ".join(roster) if roster else "none — the part has no bodies yet")
                + ". Call describe_part(doc_id) for the current roster, or use a geometry "
                  'query like {"of":"face","side":"top"}.')
        # The LIVE doc_query match IS the canonical selector: validate_selector wants a
        # centroid on a face/edge/vertex, and the resolver already carries it (plus surf /
        # axis / owner_feature, which is what makes the pick survive a rebuild).
        sel = {"kind": m.get("kind") or _SID_KINDS.get(sid[:1].lower(), "face"),
               "sid": m.get("sid") or sid}
        for k in ("centroid", "surf", "axis", "owner_feature", "radius"):
            if m.get(k) is not None:
                sel[k] = m[k]
        resolved[sid] = sel
    out = []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            out.append(op)
            continue
        new_refs = dict(refs)
        touched = False
        for slot, v in refs.items():
            if slot not in _SID_REF_SLOTS:
                continue
            if isinstance(v, list):
                lst = [resolved.get(_bare_sid(x) or "", x) for x in v]
                if lst != v:
                    new_refs[slot], touched = lst, True
            else:
                sid = _bare_sid(v)
                if sid:
                    new_refs[slot], touched = resolved[sid], True
        if not touched:
            out.append(op)
            continue
        out.append({**op, "feature": {**feat, "refs": new_refs}})
    if resolved:
        shown = ", ".join(sorted(resolved))
        notes.append(
            f"wrapped bare face/edge sid(s) {shown} into selector objects "
            f'({{"kind":"face","sid":"…"}}) — a ref slot takes a selector, a reserved datum, '
            f'an f<N> id or a "$N" placeholder, never a bare sid')
    return out


# A cylinder with no height cannot build. When the call also reads as a BORE, the model
# reached for the wrong feature type: a bore in existing material is a `hole`, which is
# dimensioned by diameter and drilled through a named face. Say that instead of letting the
# schema report a missing param — but never convert it silently: cylinder-vs-hole is a real
# modelling choice (a standalone post IS a cylinder).
_BORE_INTENT_WORDS = ("bore", "hole", "drill", "counterbore", "countersink", "pocket",
                      "clearance", "thru", "through")


def _check_cylinder_height(feat: dict, ftype: str, notes: list) -> dict:
    if ftype != "cylinder":
        return feat
    _out, _params, peek, _consume = _split_two_levels(feat)
    if peek("height") is not _MISSING:
        return feat
    label = _feat_label(feat, ftype)
    boreish = (str(peek("mode") or "").strip().lower() == "cut"
               or any(w in label.lower() for w in _BORE_INTENT_WORDS))
    msg = (f'cylinder "{label}": params.height is required (a cylinder is radius + height, '
           f"both in mm).")
    if boreish:
        msg += (' This reads like a BORE, and a bore in existing material is a `hole`, not a '
                'cylinder: {"op":"add_feature","feature":{"type":"hole","name":"' + label +
                '","params":{"diameter":20.042,"style":"simple","end":"through_all"},'
                '"refs":{"face":{"of":"face","side":"top"}}}} — a hole takes the DIAMETER '
                'directly and is drilled through a named face. Use a cylinder with '
                'mode:"cut" only when you want a free-standing tool solid.')
    raise ValueError(msg)


def _supply_missing_refs(ops: Any, notes: list) -> Any:
    """Supply a REF the engine cannot infer and the model did not name.

    Deliberately separate from the dialect passes above: those rewrite what the model
    wrote, this one adds something it never said. Today that is a hole's entry face — a
    solid has six of them, the compiler refuses to pick ("hole: 'face' selector is
    required (the planar face to drill into)"), and the top face is what a person means by
    an undecorated bore. Echoed, so the model can name another face next turn.
    """
    if not isinstance(ops, list):
        return ops
    out = []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not (isinstance(feat, dict) and op.get("op") == "add_feature"
                and str(feat.get("type") or "").strip().lower() == "hole"):
            out.append(op)
            continue
        refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        if refs.get("face") or refs.get("positions_sketch"):
            out.append(op)
            continue
        out.append({**op, "feature": {**feat,
                                      "refs": {**refs,
                                               "face": {"of": "face", "side": "top"}}}})
        notes.append(
            f'{_feat_label(feat, "hole")}: no refs.face given — drilling the TOP face '
            f'(refs.face={{"of":"face","side":"top"}}); a bore has to be entered through a '
            f"named planar face, so name another one to drill elsewhere")
    return out


def _normalize_feature_schema(feat: dict, ftype: str, notes: list) -> dict:
    """Every "the engine requires this and the model forgot" rewrite for one feature."""
    out = _unwrap_vector_dims(feat, ftype, notes)
    out = _normalize_placement_word(out, ftype, notes)
    out = _normalize_feature_refs(out, ftype, notes)
    if ftype in _PROFILE_MODE_TYPES or ftype in _AXIS_SOURCE_TYPES:
        out = _normalize_profile_feature(out, ftype, notes)
    if ftype in _EXTRUDE_TYPES:
        out = _normalize_extrude_params(out, ftype, notes)
    elif ftype == "hole":
        out = _normalize_hole_feature(out, notes)
    elif ftype == "boolean":
        out = _normalize_boolean_feature(out, notes)
    elif ftype == "cylinder":
        out = _check_cylinder_height(out, ftype, notes)
    return out


def _resolve_batch_name_refs(ops: Any, notes: list) -> Any:
    """Rewrite a reference to a feature this SAME batch creates, by NAME, into its "$N".

    Models name their features and then reference the name ("targets":["Barrel"]); the engine
    only knows f<N> ids and same-batch "$<index>" placeholders, so the batch died on the
    dangling-ref guardrail. Pure: only names created earlier in this very list are rewritten.
    """
    if not isinstance(ops, list):
        return ops
    index: dict[str, str] = {}
    for i, op in enumerate(ops):
        if isinstance(op, dict) and op.get("op") == "add_feature":
            nm = (op.get("feature") or {}).get("name")
            if isinstance(nm, str) and nm.strip():
                index.setdefault(nm.strip(), f"${i}")
    if not index:
        return ops
    hits: list = []
    # Slots whose STRING value names a feature (everything else is left verbatim).
    ref_keys = ("feature", "owner_feature", "sketch", "a", "b", "id")
    param_keys = ("a", "b", "sketch", "profile", "targets", "bodies", "features")

    def _sub(v: Any, table: dict) -> Any:
        if isinstance(v, str):
            tgt = table.get(v.strip())
            if tgt and not _is_canon_datum(v.strip()):
                hits.append(v.strip())
                return tgt
            return v
        if isinstance(v, list):
            return [_sub(x, table) for x in v]
        if isinstance(v, dict):
            return {k: (_sub(x, table) if k in ref_keys or isinstance(x, (list, dict)) else x)
                    for k, x in v.items()}
        return v

    out = []
    for i, op in enumerate(ops):
        if not (isinstance(op, dict) and op.get("op") == "add_feature"
                and isinstance(op.get("feature"), dict)):
            out.append(op)
            continue
        feat = op["feature"]
        # A feature never references ITSELF, and its own `name` is not a ref.
        table = {k: v for k, v in index.items() if v != f"${i}"}
        refs, params = feat.get("refs"), feat.get("params")
        new_refs = _sub(refs, table) if (table and isinstance(refs, dict)) else refs
        new_params = params
        if table and isinstance(params, dict):
            new_params = {k: (_sub(v, table) if k in param_keys else v)
                          for k, v in params.items()}
        if new_refs != refs or new_params != params:
            nf = dict(feat)
            if new_refs != refs:
                nf["refs"] = new_refs
            if new_params != params:
                nf["params"] = new_params
            op = {**op, "feature": nf}
        out.append(op)
    if hits:
        notes.append("resolved same-batch feature name(s) " + ", ".join(sorted(set(hits)))
                     + ' to their "$<index>" placeholders — a ref names an f<N> id or a '
                       '"$N" position in THIS batch, never a display name')
    return out


# ---------------------------------------------------------------------------
# The sketch-space coordinate that measures a profile's distance from each revolve
# axis, per sketch plane (plane bases: top u=+X v=+Y, front u=+X v=+Z, right u=+Y
# v=+Z — core/geom/plane.h). 0 = the u ("x") coordinate, 1 = the v ("y") one. A
# plane that does not CONTAIN the axis has no single radial coordinate -> absent.
_RADIAL_COORD = {("plane.top", "axis.x"): 1, ("plane.top", "axis.y"): 0,
                 ("plane.front", "axis.x"): 1, ("plane.front", "axis.z"): 0,
                 ("plane.right", "axis.y"): 1, ("plane.right", "axis.z"): 0}


def _datum_token(v: Any) -> Optional[str]:
    """A ref-slot datum value (bare word or {"kind":"datum","feature":…}) -> token."""
    if isinstance(v, dict):
        v = v.get("feature")
    tok = _norm_datum(v)
    return tok if tok in _DATUM_TOKENS else None


def _sketch_radial_extent(sketch_feat: dict, plane: str, axis: str):
    """(min, max) of the profile's radial coordinate, or None when not cheaply known.

    Only reads points already written in THIS batch's sketch entities (line/arc
    p1/p2, circle/arc centre +/- radius) — never invents or repairs geometry.
    """
    idx = _RADIAL_COORD.get((plane, axis))
    if idx is None:
        return None
    ents = ((sketch_feat.get("params") or {}).get("entities")
            if isinstance(sketch_feat.get("params"), dict) else None)
    if not isinstance(ents, list):
        return None
    vals: list = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        for pk in ("p1", "p2", "center"):
            p = e.get(pk)
            if (isinstance(p, list) and len(p) >= 2
                    and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                            for c in p[:2])):
                vals.append(float(p[idx]))
                r = e.get("radius") if pk == "center" else None
                if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0:
                    vals.extend((float(p[idx]) - r, float(p[idx]) + r))
    return (min(vals), max(vals)) if vals else None


def _enrich_zero_volume_errors(fs: list, ops: list, idmap: dict) -> None:
    """Make a revolve zero-volume failure ACTIONABLE, in place on feature_status.

    The engine guard ("revolve produced a zero-volume solid") says WHAT is wrong but
    not WHERE the profile sits, and the model resent the identical batch (live k3
    run, 2026-07-27). This is a modelling mistake, never a dialect — no geometry is
    repaired; when the profile sketch is in the SAME batch its radial extent is
    cheap, so the message names the exact offset that clears the axis.
    """
    idx_of = {fid: int(k[1:]) for k, fid in idmap.items()
              if isinstance(k, str) and k.startswith("$")
              and k[1:].isdigit() and isinstance(fid, str)}
    for s in fs:
        err = str(s.get("error") or "")
        if s.get("ok") or "zero-volume" not in err or "revolve" not in err:
            continue
        i = idx_of.get(s.get("id"))
        feat = ops[i].get("feature") if i is not None and i < len(ops) else None
        if not isinstance(feat, dict) or feat.get("type") != "revolve":
            continue
        refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        axis = _datum_token(refs.get("axis")) or "axis.y"
        prof = refs.get("profile")
        sel = prof[0] if isinstance(prof, list) and prof else prof
        sk_name = (sel.get("sketch") if isinstance(sel, dict)
                   else sel if isinstance(sel, str) else None)
        sketch = None
        if isinstance(sk_name, str) and sk_name.startswith("$") and sk_name[1:].isdigit():
            j = int(sk_name[1:])
            f = ops[j].get("feature") if j < len(ops) else None
            sketch = f if isinstance(f, dict) and f.get("type") == "sketch" else None
        elif sk_name is not None:
            sketch = next((o["feature"] for o in ops
                           if isinstance(o, dict)
                           and isinstance(o.get("feature"), dict)
                           and o["feature"].get("type") == "sketch"
                           and o["feature"].get("name") == sk_name), None)
        extent = None
        plane = "plane.top"
        if sketch is not None:
            srefs = sketch.get("refs") if isinstance(sketch.get("refs"), dict) else {}
            plane = (_datum_token(srefs.get("plane"))
                     or _datum_token((sketch.get("params") or {}).get("plane"))
                     or "plane.top")
            extent = _sketch_radial_extent(sketch, plane, axis)
        if extent is None:
            s["error"] = err + (" | fix: move the profile so it sits strictly to ONE "
                                "side of the axis — no point on or across it")
            continue
        lo, hi = extent
        coord = "xy"[_RADIAL_COORD[(plane, axis)]]
        if lo > 0:
            s["error"] = err + (f" | the profile already clears the axis "
                                f"({coord} in [{lo:g}, {hi:g}] mm on {plane}) — the "
                                f"cause is the angle; use an angle well above 0")
        else:
            shift = 1.0 - lo
            s["error"] = err + (
                f" | fix: on {plane} the profile spans {coord} in [{lo:g}, {hi:g}] mm "
                f"and the axis ({axis}) lies along the sketch {coord}=0 line — offset "
                f"the profile so its minimum {coord} is > 0 (e.g. add {shift:g} mm to "
                f"every point's {coord}, giving [{lo + shift:g}, {hi + shift:g}])")


# add_features ARGUMENT-SHAPE normalizer (model-robustness).
#
# `ops=[{"op":"add_feature","feature":{…}}]` is the documented call and is passed through
# byte-identical. Everything ELSE a model actually writes is rewritten into it here rather
# than rejected, because a teaching error still burns a whole turn:
#   ops={…} / ops="[…]"          one op, or the array as a JSON string
#   feature= features= op=       the batch under the name the model reached for
#   operations= actions= ops_json=
#   type=… name=… diameter=…     ONE feature spread over flat top-level kwargs
#   name= identifier= part=      the DOCUMENT, when no feature type is in sight
# add_features takes a var-keyword parameter for exactly this reason: agent_turn's
# _reconcile_kwargs hands a var-kw tool its arguments UNTOUCHED instead of rejecting the
# unknown names, so the tool owns its own input dialect. Every rewrite is echoed in
# `interpreted`; genuinely ambiguous input raises a teaching error that names the accepted
# forms, and nothing is ever silently dropped.
_BATCH_ALIASES = ("features", "feature", "operations", "actions", "ops_json", "op")
_DOC_HANDLE_ALIASES = ("identifier", "part_name", "part", "document_id", "document", "doc",
                       "part_id", "docid", "target_doc", "name")
_PARAMS_ALIASES = ("params", "parameters", "param", "properties", "props", "attributes")
_FEATURE_NAME_ALIASES = ("feature", "identifier", "feature_name", "label", "name")
_BACKGROUND_ALIASES = ("async", "in_background", "run_in_background", "defer")
# The feature envelope the engine accepts beside "type" (session.cpp::kPrimitiveKeys plus
# the structural keys). Anything else on a flat call is a param.
_FEATURE_ENVELOPE_KEYS = frozenset({
    "type", "name", "params", "refs", "at", "at_offset", "axis", "axis_dir", "centered",
    "mode", "merge", "folder", "id", "suppressed", "body", "dir",
})
_OP_ORDER_KEYS = ("before", "after")
# DESIGN INTENT, not geometry. A model routinely annotates its build call with why it chose
# a number; none of these is a parameter of anything. Failing the whole build over an
# annotation is the exact turn-burning behaviour this normalizer exists to kill — so they
# are accepted, ignored, and reported.
_COMMENTARY_KEYS = frozenset({
    "purpose", "basis", "rationale", "reason", "why", "intent", "design_intent",
    "justification", "note", "notes", "comment", "comments", "description", "summary",
    "explanation", "requirement", "requirements", "acceptance", "acceptance_criteria",
    "criteria", "context", "assumption", "assumptions", "citation", "confidence",
    "clearance", "goal", "objective", "source", "reference",
})
_ADD_FEATURES_ACCEPTS = (
    'add_features accepts ops=[{"op":"add_feature","feature":{"type":"box","name":"Block",'
    '"params":{"length":40,"width":30,"height":20}}}] (the canonical batch), '
    'doc_id="doc-…" (an open part\'s NAME also resolves) and background=true. The batch may '
    "also arrive as feature=/features=/op=/operations=/actions=/ops_json=, as one op dict, "
    "as a JSON string, or as ONE feature spread over top-level kwargs "
    "(type=…, name=…, params={…}). Call cad_guide() for the op vocabulary."
)


def _jsonify(v: Any) -> Any:
    """Tuples/sets from a Python literal -> JSON-shaped lists."""
    if isinstance(v, (list, tuple, set)):
        return [_jsonify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonify(x) for k, x in v.items()}
    return v


def _as_batch(value: Any, where: str) -> Optional[list]:
    """Any container a model reaches for -> a list of ops. None: not a batch at all."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        s = value.strip()
        if s[:1] not in ("[", "{"):
            return None  # a bare word — a NAME, not a batch
        try:
            parsed = json.loads(s)
        except ValueError as e:
            # A Python REPR of the same structure ("[{'op': 'add_feature', …}]") is the
            # other thing models emit when told "send JSON"; it is unambiguous, so read it
            # rather than burn the turn. Only genuine pseudo-code falls through to the error.
            import ast  # noqa: PLC0415 — failure path only
            try:
                lit = ast.literal_eval(s)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                lit = None
            if not isinstance(lit, (list, tuple, dict)):
                raise ValueError(
                    f"{where} is a string that parses as neither JSON nor a Python literal "
                    f"({e}). Send a real JSON array: "
                    f'ops=[{{"op":"add_feature","feature":{{"type":"box","name":"Block",'
                    f'"params":{{"length":40,"width":30,"height":20}}}}}}].') from None
            parsed = _jsonify(lit)
        return parsed if isinstance(parsed, list) else [parsed]
    return None


def _batch_from_flat_kwargs(kw: dict, notes: list) -> Optional[list]:
    """ONE feature (or one op) spread over top-level kwargs -> a one-op batch.

    Consumes the keys it uses out of *kw*; returns None when the call names neither an op
    nor a feature type (the caller then reports the leftovers).
    """
    docops = _doc_op_vocab()
    ftypes, _live = _feature_type_vocab()

    op_key = op_word = None
    for k in ("op", "operation", "action", "kind"):
        v = kw.get(k)
        if isinstance(v, str) and v.strip():
            op_key, op_word = k, v.strip()
            break
    type_word = kw.get("type") if isinstance(kw.get("type"), str) else None
    type_word = type_word.strip() if type_word and type_word.strip() else None
    if not op_word and not type_word:
        return None

    # A doc op spelled flat: {"op":"delete_feature","id":"f2"}.
    if op_word and op_word in docops and not type_word:
        kw.pop(op_key, None)
        rest = {k: kw.pop(k) for k in list(kw)}
        notes.append(f'interpreted the flat call as ops=[{{"op":"{op_word}", …}}] — '
                     f"add_features always takes a LIST of ops")
        return [_normalize_op_target({"op": op_word, **rest}, op_word, notes)]

    ftype = type_word or op_word
    if op_word and not type_word:
        kw.pop(op_key, None)
        # Canonicalize HERE so the echoed note names the type the engine will see; an
        # unresolvable word is left verbatim for _retype_feature's teaching error.
        ftype = _canon_feature_type(op_word, ftypes) or op_word
        notes.append(f'interpreted {op_key}="{op_word}" as feature type "{ftype}" — '
                     f"a feature type is not an op name")
    else:
        kw.pop("type", None)

    feature: dict = {"type": ftype}
    params: dict = {}
    for pk in _PARAMS_ALIASES:
        v = kw.pop(pk, None)
        if isinstance(v, dict):
            params.update(v)
            if pk != "params":
                notes.append(f"interpreted {pk}= as the feature's params")
        elif v is not None:
            kw[pk] = v  # not a mapping — fall through to the leftover report

    order = {k: kw.pop(k) for k in _OP_ORDER_KEYS if k in kw}
    for k in list(kw):
        if k in _FEATURE_ENVELOPE_KEYS and k != "type":
            feature[k] = kw.pop(k)
    if not feature.get("name"):
        for k in _FEATURE_NAME_ALIASES:
            v = kw.get(k)
            if isinstance(v, str) and v.strip():
                feature["name"] = v.strip()
                kw.pop(k)
                notes.append(f'interpreted {k}="{v}" as the feature name')
                break
    folded = sorted(kw)
    for k in folded:
        params[k] = kw.pop(k)
    if params or "params" in feature:
        feature["params"] = {**params, **(feature.get("params") or {})}
    if folded:
        notes.append("read top-level " + ", ".join(folded) + " as the feature's params")
    notes.append(
        'interpreted a bare feature spec as one add_feature op; the canonical form is '
        'ops=[{"op":"add_feature","feature":{…}}]')
    return [{"op": "add_feature", "feature": feature, **order}]


def _normalize_add_features_args(ops: Any, doc_id: Optional[str], extra: dict,
                                 notes: list) -> tuple:
    """-> (ops list, doc_id, background_override). Raises ValueError to TEACH, never guesses."""
    supplied = {k: v for k, v in (extra or {}).items() if v is not None}
    background = None
    for k in _BACKGROUND_ALIASES:
        if k in supplied:
            background = bool(supplied.pop(k))
            notes.append(f"interpreted {k}= as background=")

    batches: list = []
    if ops is not None:
        primary = _as_batch(ops, "ops")
        if primary is None:
            raise ValueError(
                f"ops must be a list of ops — got {type(ops).__name__} {ops!r}. "
                + _ADD_FEATURES_ACCEPTS)
        if not isinstance(ops, list):
            notes.append(f"read ops={type(ops).__name__} as a one-element op list"
                         if isinstance(ops, dict) else "parsed the ops JSON string")
        batches.append(("ops", primary))
    for key in _BATCH_ALIASES:
        if key not in supplied:
            continue
        batch = _as_batch(supplied[key], key)
        if batch is None:
            continue  # a bare word under an alias name — a NAME; resolved further down
        supplied.pop(key)
        batches.append((key, batch))
        notes.append(f"interpreted {key}= as ops= — the batch parameter is called `ops`")
    if len(batches) > 1:
        head_label, head = batches[0]
        clashing = [lbl for lbl, b in batches[1:] if b != head]
        if clashing:
            raise ValueError(
                f"add_features() got DIFFERENT op batches under "
                f"{', '.join([head_label] + clashing)} and cannot tell which one to build. "
                f"Send exactly one, under `ops`. " + _ADD_FEATURES_ACCEPTS)
        notes.append(f"ignored duplicate batch(es) under "
                     f"{', '.join(lbl for lbl, _ in batches[1:])} (identical to {head_label})")

    commentary = [k for k in sorted(supplied) if _slug(k) in _COMMENTARY_KEYS]
    for k in commentary:
        supplied.pop(k)
    if commentary:
        notes.append("ignored " + ", ".join(commentary) + " — design intent/commentary, not "
                     "modeling parameters; record reasoning with ledger_note()")

    final = batches[0][1] if batches else _batch_from_flat_kwargs(supplied, notes)

    for key in _DOC_HANDLE_ALIASES:
        v = supplied.get(key)
        if not (isinstance(v, str) and v.strip()):
            continue
        supplied.pop(key)
        if doc_id and str(doc_id).strip() != v.strip():
            raise ValueError(
                f'add_features() got doc_id="{doc_id}" AND {key}="{v}", which name different '
                f"documents. Pass the target part once, as doc_id (its id or its exact name).")
        if not doc_id:
            doc_id = v.strip()
            notes.append(
                f'interpreted {key}="{v}" as doc_id — add_features targets a DOCUMENT and an '
                f"open part's name resolves to its doc_id; a FEATURE's name goes in "
                f"feature.name inside an op")
        break

    if supplied:
        raise ValueError(
            f"add_features() got argument(s) it cannot place: {', '.join(sorted(supplied))}. "
            + _ADD_FEATURES_ACCEPTS)
    if final is None:
        raise ValueError("add_features() was called with no ops. " + _ADD_FEATURES_ACCEPTS)
    return final, doc_id, background



def register_cad_doc_tools(mcp: FastMCP) -> None:
    """CAD document-lifecycle MCP tools (T-RF8 split of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""


    # ---------------------------------------------------------------- knowledge

    @mcp.tool()
    def cad_guide(topic: str = "overview") -> str:
        """Return the engine's modeling/simulation knowledge so you can drive it well. CALL FIRST.

        This is the same closed op vocabulary + worked examples the in-app agent's modeler is
        taught. topic="overview" (default) gives the workflow + full op cheatsheet (primitives,
        sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell/draft, holes, bolt_circle,
        patterns/mirror, edits) and the plain-English geometry-query predicates. Other topics:
        "assembly" (new_assembly/asm_add_component/asm_mate/solve_mates — put saved parts
        together with mates so they actually fit; seating patterns + validation loop),
        "simulation" (FEA/CFD how-to), "motion" (simulate_motion — drive a mate to animate an
        assembly + swept interference, kinematics), "review" (how to read the annotated render), "gsl" (the
        progressive TEXT read of a part's geometry — describe_part/describe_sketch/
        describe_scene/hover/query_adjacency — including the non-vision self-review path),
        "narration" (how to use describe_toolpaths and describe_results to get compact
        text summaries of CAM jobs and FEA/CFD results instead of raw JSON dumps),
        "parts" (standard-parts library: search→get→place workflow, standard-before-custom
        bias, parts_taxonomy category browse + faceted part_search, domain routing,
        editability guarantee, sourcing), "reverse"
        (reverse-engineering an imported STEP/mesh into an editable feature tree),
        "requirements" (req_create/update/status/report — design-brief CRUD),
        "dag" (dag_plan/run/status/approve/abort/replan — multi-step mission orchestration),
        "verify" (verify_battery — done-check before declaring a design finished),
        "interaction" (ask_user elicitation seam — batch unknowns into one call),
        "sourcing" (part_source need→ranked candidates, artifact plans (place / scrape+import /
        make_standard_part), vendors_health login etiquette, cam_tool_import cutting tools,
        bom_rollup/bom_price + request_quote),
        "cost" (cam_cost_estimate/request_quote — made-part cost model),
        "spec" (match_part_number/spec_sheet_match — MPN resolution + datasheet extraction),
        "piping" (piping_solve/report/... — 1-D hydraulic solve + CFD handoff),
        "hydraulic" (hydraulic_new/add_component/... — ISO 1219 circuit design),
        "firmware" (fw_new_project/build/sim_run/flash/debug — embedded compile loop),
        "structural" (struct_analyze/check/estimate — PyNite member forces + code check),
        "mechanical" (gear_check/bearing_life — AGMA gear tooth stress + ISO 281 bearing life),
        "regen" (regen_diagnose — diagnose + propose fix-ops on rebuild failure),
        "code_compliance" (code_check — building-code compliance with RAG citations),
        "serving" (serving_status/set_canary/promote/rollback — LoRA adapter routing),
        "design_review" (design_review — one-call geometry battery narrated verdict),
        "doc_diff" (doc_diff_tool — semantic feature-level diff between two revisions),
        "plm" (project-vault versioning stub — T-R2 fills this),
        "tolerance_stackup" (stackup_analyze — 1D worst-case + RSS tolerance stack-up),
        "sketch3d" (sketch3d_new/add_curves/get/as_sweep_guide + render_sketch/
        sketch_preview_text/doc_sketch_contact_sheet — 3D free-space sketch curves
        usable as sweep spines/guide curves, and 2D sketch preview before extruding),
        or a single feature's cheatsheet by name ("hole", "fillet", "revolve", "loft",
        "sweep", "shell", "chamfer", "pattern", "mirror", "sketch", "airfoil",
        "drawing", "hardware", ...).
        """
        return cad_guide_text(topic)



    # ---------------------------------------------------------------- diagnostics

    @mcp.tool()
    def engine_profile(action: str = "status", hz: int = 0, mode: str = "modeling",
                       top: int = 40, min_samples: int = 0,
                       write_folded: str = "", write_speedscope: str = "") -> str:
        """Profile THIS engine process (the one your modeling RPCs run in) at runtime.

        The desktop app's profiler window only sees the GUI's own engine — a DIFFERENT process
        from this MCP server, where subagent modeling actually happens. This tool drives the
        engine's built-in sampling profiler + lock-contention meters in-process, so a heavy
        multi-subagent run can be measured without a restart. All modeling in one engine
        serialises under a single global lock; the `contention` action is what reveals time
        spent BLOCKED on that lock (which the CPU sampler cannot see).

        action:
          "start"       begin sampling. hz sets the rate (default ~1kHz); mode "modeling"
                        (default) samples only modeling ops, "all" samples every RPC.
          "stop"        stop sampling and return the symbolized report.
          "report"      return the report WITHOUT stopping (modules + hot frames + folded stacks).
          "status"      (default) is the sampler running? how many samples so far?
          "contention"  return g_mtx lock-wait/hold/queue-depth stats since last reset — the
                        primary multi-subagent bottleneck signal (%wall blocked, top callers).
          "reset_contention"  zero the contention counters (call before a measured run).
          "phases"      per-study simulation phase timings (always-on, no start needed):
                        every mesher / FEA static / modal / thermal / fatigue / LBM /
                        flow / overlay run as {domain, total_ms, phases:[{name,ms,pct}]}
                        — mesh vs assemble vs factorize vs post, per study. The same
                        breakdown also rides along on "stop"/"report" under "phases".
          "reset_phases"  clear the phase registry (call before a measured run).

        top/min_samples/write_folded/write_speedscope tune the sampling report (see PROFILER.md).
        """
        try:
            a = (action or "status").strip().lower()
            if a == "start":
                p = {"mode": mode}
                if hz and hz > 0:
                    p["hz"] = int(hz)
                return _dumps(cb.call("prof_start", p))
            if a == "stop":
                return _dumps(cb.call("prof_stop", {"top": top, "min_samples": min_samples,
                                                    "write_folded": write_folded or None,
                                                    "write_speedscope": write_speedscope or None}))
            if a == "report":
                return _dumps(cb.call("prof_report", {"top": top, "min_samples": min_samples,
                                                      "write_folded": write_folded or None,
                                                      "write_speedscope": write_speedscope or None}))
            if a == "status":
                return _dumps(cb.call("prof_status", {}))
            if a == "contention":
                return _dumps(cb.call("prof_contention", {}))
            if a == "reset_contention":
                return _dumps(cb.call("prof_contention", {"reset": True}))
            if a == "phases":
                return _dumps(cb.call("prof_phases", {}))
            if a == "reset_phases":
                return _dumps(cb.call("prof_phases", {"reset": True}))
            return _err(ValueError(
                f"unknown action {action!r}; use start|stop|report|status|contention|"
                f"reset_contention|phases|reset_phases"))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- modeling

    @mcp.tool()
    def new_part(name: str = "part") -> str:
        """Create a new, empty editable part document and make it current. Returns its doc_id.

        Follow with add_features(...) to build geometry. Each part is a real parametric feature
        tree (undo/redo, editable parameters), not a one-shot mesh.
        """
        try:
            r = cb.call("doc_new", {"kind": "part", "name": name})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            # Seed the write-diff baseline: an empty part has zero volume, so the FIRST
            # add_features reports a real volume delta ("0.0 -> X (+X)") instead of degrading
            # to the volume-only "volume now X" branch of _write_diff.
            _LAST_VOLUME[did] = 0.0
            out = {"ok": True, "doc_id": did, "name": name,
                   "revision": r.get("revision", 0)}
            # Land the (still empty) part in the user's project folder immediately so it
            # exists as a real, openable file; add_features refreshes it as geometry grows.
            saved = _project_save(did, name)
            if saved:
                out["saved_path"] = saved
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_features(ops: Optional[list] = None, doc_id: Optional[str] = None,
                     background: bool = False,
                     **extra: Annotated[Any, Field(default=None)]) -> str:
        """Apply a list of document ops to build/edit the part (the core modeling tool).

        `ops` is a JSON array of ops from the CLOSED vocabulary (see OP VOCABULARY below
        and cad_guide()) — e.g.
          [{"op":"add_feature","feature":{"type":"box","name":"Block",
              "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
                        "height":{"value":20,"expr":null}}}},
           {"op":"add_feature","feature":{"type":"fillet","name":"Round",
              "params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}]
        Geometry is addressed in plain words via `refs` (e.g. {"face":{"of":"face",
        "side":"top"}}). Ops in one call may reference an earlier op in the SAME array as
        "$<index>" (0-based); existing tree features by id ("f1").

        ARGUMENT SHAPES — the canonical call is `ops=[…]`; these are accepted too, and every
        rewrite comes back in `interpreted` (prefer the canonical spelling once you know it):
          ops={"op":…}           a single op dict — read as a one-element batch
          ops="[{…}]"            the array as a JSON string (real JSON only: Python repr
                                 with single quotes/tuples is a teaching error, not a guess)
          feature= features= op= operations= actions= ops_json=
                                 the batch under another name. Two DIFFERENT batches in one
                                 call is an error — send exactly one.
          ops=[{"type":"box",…}] bare feature dicts with no "op" — read as add_feature
          {"op":"box",…}         a feature TYPE used as the op name — read as add_feature
          type=… name=… params={…} diameter=… (no ops at all)
                                 ONE feature spread over top-level kwargs; unrecognised
                                 kwargs become that feature's params
          name= identifier= part= part_name=   with NO type= in the call: the DOCUMENT to
                                 build into (an open part's name resolves to its doc_id).
                                 Supplying both a differing doc_id and one of these is an
                                 error. A feature's name always goes in feature.name.
          purpose= basis= rationale= note= …   design intent is ACCEPTED and ignored (it is
                                 not a modeling parameter) — record reasoning with
                                 ledger_note(), not here.

        DIMENSIONS — write a size in whichever of these is most natural; all are accepted:
          40                     a bare number, in DOCUMENT UNITS (millimetres)
          "40"                   the same number as a string
          {"value":40,"expr":null}   the canonical dim object (what get_part hands back)
          "40mm" "2.5cm" "1in" '2.5"' "1ft" "5 ft 6 in" "0.5m" "20thou"
                                 a number plus a unit — converted to mm for you, and the
                                 conversion is echoed back in the response's `interpreted`
          {"value":2,"unit":"in"}    the same thing with the unit in its own field
          "width/2"              an EXPRESSION, evaluated by the engine against the part's
                                 equations/globals (also {"value":0,"expr":"width/2"}).
                                 Anything that isn't purely <number><unit> is left alone and
                                 handed to the expression evaluator, so a real formula is
                                 never mangled — but a unit inside a formula ("2*10mm") is
                                 NOT understood: do the arithmetic or use one unit literal.
        DIMENSION SHORTHAND (rewritten for you, and reported in `interpreted` — prefer the
        canonical spelling once you know it):
          "diameter" (or dia/d) on cylinder/sphere -> radius, HALVED. On a cone d1/d2 -> r1/r2;
            on a torus major_diameter/minor_diameter -> major_r/minor_r.
            NOT on hole / bolt_circle: those take a REAL "diameter" param and are never halved.
          "size":[l,w,h] on a box -> length/width/height; "size":40 -> a 40 cube.
        These forms work on any add_feature params and on edit_feature params (the unit
        conversion; the per-type diameter alias needs the feature type, so it is add-only).
        Every rewrite this tool performs comes back as `interpreted`:[…] beside the result.

        SCHEMA DEFAULTS the engine requires and you may omit (all echoed in `interpreted`):
          hole      params.style defaults to "simple" — the vocabulary is simple|tap|cbore|
                    csink, and counterbore/c-bore/countersink/csk/tapped are mapped onto it.
                    params.end defaults to "blind" when a numeric depth is present and
                    "through_all" otherwise; through/thru are mapped onto "through_all".
                    refs.face defaults to {"of":"face","side":"top"} (a bore has to be
                    entered SOMEWHERE) — a direction vector picks the face instead. A world
                    [x,y,z] under origin/position/at becomes FACE-LOCAL positions=[[u,v]].
          sketch    entities get sequential e1..eN ids and every constraint/dimension handle
                    that referenced the old id is rewritten with them; params.constraints
                    defaults to []; refs.plane accepts XY/XZ/YZ/top/front/right (any case)
                    and becomes the reserved datum plane.top/plane.front/plane.right.
          boolean   params.op accepts union/add/join -> "fuse", subtract/difference -> "cut",
                    intersect -> "common"; targets/bodies/operands become params.a and .b.
          extrude / revolve / loft / sweep
                    params.mode defaults to "boss"; params.sketch:"<name>" becomes
                    refs.profile=[{"kind":"region","sketch":…}]; revolve's params.axis:"Z"
                    becomes refs.axis:"axis.z", and a revolve with no angle defaults to
                    a full 360° revolution.
          any       feature TYPE synonyms are mapped (cube/cuboid/block -> box, cylindrical/
                    rod/tube -> cylinder, ball -> sphere, cut_<shape> -> <shape> with
                    mode:"cut", pocket -> extrude with mode:"cut"); an unknown type is
                    rejected with the closest real types named. A ref that names a feature
                    CREATED IN THIS SAME BATCH by its display name resolves to its "$N".
                    at/axis/origin take a plain [x,y,z] — a {"value":[…]} wrapper around a
                    coordinate is unwrapped, and at:"center" becomes [0,0,0].

        OP VOCABULARY (closed — do NOT invent names):
          ops (field op=…): add_feature, edit_feature, delete_feature, reorder_feature,
            set_suppressed, set_folder, rename, set_units, set_material, set_appearance,
            set_rollback, eq_set, eq_delete, sketch_add_entity, sketch_add_constraint,
            sketch_edit_entity, sketch_delete_entity, sketch_delete_constraint, sketch_set_dim,
            sketch_toggle_construction, sketch_convert, sketch_break_external_ref, sketch_trim,
            sketch_extend, sketch_offset, sketch_fillet, sketch_chamfer, sketch_mirror_entities,
            sketch_pattern_entities, sketch_move, sketch_rotate, asm_insert_component,
            asm_edit_component, asm_delete_component, asm_add_mate, asm_edit_mate,
            asm_delete_mate, asm_set_explode, copy_feature, paste_feature,
            paste_feature_as_derived, sketch_paste_entities
          feature types (ONLY under op=add_feature): box, cylinder, sphere, cone, torus,
            sketch, extrude, revolve, loft, sweep, helix, fillet, chamfer, shell, draft,
            hole, bolt_circle, linear_pattern, circular_pattern, mirror, transform, boolean,
            rib, split, thread, text, import, script, plane, axis, point, csys, sketch3d, …
          WRONG: {"op":"box",…} / {"op":"sketch",…} / {"op":"extrude",…} / {"op":"cylinder",…}
          RIGHT: {"op":"add_feature","feature":{"type":"box"|"cylinder"|"sketch"|…, …}}
          A leading {"op":"new_part","name":…} (or bare {"type":"new_part",…}) is also
            accepted as a convenience: it mints the document and the rest of the batch
            builds into it, so you can create-and-build in one call. Everywhere else
            new_part is the standalone tool, not an op.
          Live list: engine doc_list_ops (sourced from core/doc/op_registry — single source).
          Call cad_guide() for worked examples and full param shapes.

        PRIMITIVE PLACEMENT + ORIENTATION (box/cylinder/sphere/cone/torus) — feature-level
        keys beside "type"/"name"/"params", all optional. An unrecognised key is REJECTED,
        never silently ignored, so use exactly these spellings:
          "at":[x,y,z]           where it goes (default: centred on the origin). May instead
                                 be a geometry query — "at":{"of":"face","side":"top"} places
                                 it at that face's centroid.
          "at_offset":[dx,dy,dz] world-axis nudge added on top of "at".
          "axis":[x,y,z] | "+x"|"-x"|"+y"|"-y"|"+z"|"-z"   which way it POINTS: the primitive's
                                 natural local +Z is rotated onto "axis" (default "+z" = up).
                                 This is how you get a sideways cylinder/cone/box without a
                                 sketch — "centered":false then measures along the ROTATED axis.
          "centered":false       put the base/corner at "at" instead of the centre (box corner,
                                 cylinder/cone base); sphere/torus are always centred.
          "mode":"boss"|"cut"    fuse into (default) or subtract from the current body.
          "merge":false          start a SEPARATE new body instead of combining.
          An off-origin sideways boss, end to end:
          {"op":"add_feature","feature":{"type":"cylinder","name":"DrainSpout",
              "params":{"radius":{"value":25,"expr":null},"height":{"value":60,"expr":null}},
              "at":[200,0,40],"axis":[1,0,0],"centered":false,"mode":"boss"}}
          WRONG: "placement":{"origin":[…],"direction":[…]} / "boolean":"union"
          RIGHT: "at":[…], "axis":[…], "mode":"boss"

        Returns revision, body_count, volume_mm3, and per-feature status (ok / error / warning)
        so you can immediately tell a dropped cut, a failed reference, or a stray extra body.
        VERIFY LOOP (after every mutation): call render_part() or render_views() to SEE the
        result, then measure_part()/query_geometry()/inspect_feature(target) on the changed
        region, and before declaring done run verify_battery(doc_id). On a validation error
        the whole batch is rejected — read `error`, fix the ops, call again. A rebuild-failure
        response may also carry `regen_diagnosis`: an IP-18 fix-preview card (diagnosis +
        editable fix_ops + verifier verdict) — record your verdict with regen_resolve(card, action).

        SKETCH MUTATIONS: any batch that includes sketch_add_entity / sketch_add_constraint /
        sketch_set_dim / sketch_fillet / … auto-appends a compact ASCII + NL readback
        (`ascii_block`, `sketch_summary`, `findings`) for each touched sketch. READ that
        readback before the next sketch edit. Before declaring a sketch done, call
        render_sketch_ascii(sketch) + sketch_health(sketch) (or check_sketch) and require dof 0.

        HEAVY OPS: a costly op (a global fillet/chamfer/shell/boolean on a many-edged body) is
        run on a worker thread and AUTO-HANDED-OFF to the background if it overruns the call
        budget, so it can never block past the MCP transport timeout (which would otherwise drop
        the whole session). If that happens you get {job_id, status:"running"} — poll get_job().
        All open docs are autosaved BEFORE the op, so nothing is lost even on a hard timeout.
        Pass background=True to hand off immediately for an op you already know is heavy.
        """
        interpreted: list = []
        try:
            # FastMCP's schema builder turns a var-keyword parameter into a real (optional,
            # Annotated) field named after it, so a FastMCP call arrives carrying
            # {"extra": None}. Drop that artifact — and ONLY that artifact — before reading
            # anything the model actually wrote. The built-in agent path (_bind_tool ->
            # tool.fn) never sets it.
            if extra.get("extra", _MISSING) is None:
                extra.pop("extra", None)
            if extra.get("kwargs", _MISSING) is None:
                extra.pop("kwargs", None)
            # Accept the argument shapes models actually write (a bare feature spec, an
            # aliased batch, a JSON string, a lone document name) and rewrite them into the
            # canonical ops list. Canonical input is returned untouched with no notes.
            ops, doc_id, bg = _normalize_add_features_args(ops, doc_id, extra, interpreted)
            if bg is not None:
                background = bg
            ops = list(ops)
            # Model-robustness: `new_part` is a top-level TOOL, not a batch op — but models
            # reliably open a build batch with {"op":"new_part","name":…} (or the bare
            # {"type":"new_part",…} — a real rejected batch from the field) because that
            # reads as the natural first step. Rejecting the whole batch over it (and over
            # the bare "No current part" that follows) burns a turn for nothing, so honour
            # the intent: mint the document here and apply the remaining ops to it.
            created: Optional[str] = None
            def _is_new_part_head(el: Any) -> bool:
                if not isinstance(el, dict):
                    return False
                word = el.get("op") if el.get("op") is not None else el.get("type")
                return _slug(word) == "new_part"
            while ops and _is_new_part_head(ops[0]):
                head = ops.pop(0)
                if head.get("op") is None:
                    interpreted.append(
                        'read bare {"type":"new_part",…} as the leading {"op":"new_part"} '
                        "create — new_part is a tool/doc op, never a feature type")
                if doc_id or created:
                    continue  # a target doc already exists; the extra create is a no-op
                r = cb.call("doc_new", {"kind": "part",
                                        "name": head.get("name") or head.get("part") or "part"})
                created = r.get("doc_id")
                _CUR["doc_id"] = created
                _LAST_VOLUME[created] = 0.0
            if created and not doc_id:
                doc_id = created
            d = _doc(doc_id)
            if created and not ops:
                return _dumps({"ok": True, "doc_id": d, "created_doc": created,
                               "note": "created the part; send its build ops in the next call"})
            # Give every element an `op` (a bare feature dict is the single most common
            # rejection in the field) and map every feature TYPE onto the engine vocabulary
            # FIRST — the sketch/primitive passes below branch on the canonical type, so a
            # sketch spelled "profile" used to slip past the sketch normalizer entirely.
            ops = _normalize_op_elements(ops, interpreted)
            ops = _normalize_feature_types(ops, interpreted)
            # Model-robustness: accept the natural/flat sketch shape LLMs emit
            # (L1 ids, start/end, dimensions[], top-level plane) by normalizing it
            # to the engine's canonical schema before validation.
            ops = _normalize_sketch_ops(ops, interpreted)
            # Same, for the primitive placement/orientation dialect models emit
            # (placement:{origin,direction}, boolean:"union") — rewritten to the
            # canonical at/axis/mode/merge keys the engine actually accepts — for the
            # dimension dialect ("25mm", diameter, box size), and for the schema
            # requirements models forget (hole style/end/face, boolean op, extrude mode).
            ops = _normalize_feature_ops(ops, interpreted)
            # Supply the one ref the engine cannot infer (a hole's entry face).
            ops = _supply_missing_refs(ops, interpreted)
            # A bare face/edge sid in a ref slot -> the selector object the engine wants,
            # verified against THIS document (one doc_query, only when a sid is present).
            ops = _resolve_sid_refs(ops, d, interpreted)
            # Finally, a ref that names a feature THIS batch creates -> its "$N" placeholder.
            ops = _resolve_batch_name_refs(ops, interpreted)
            interpreted = _interpreted_notes(interpreted)

            def _apply() -> dict:
                # Transactional batch: snapshot first so a partial failure rolls the WHOLE batch
                # back (the engine otherwise leaves failed features in the tree). All-or-nothing.
                ckpt = None
                try:
                    ckpt = cb.call("doc_checkpoint", {"doc_id": d}).get("checkpoint")
                except Exception:  # noqa: BLE001
                    ckpt = None

                # GSL Stage 1a — snapshot the PRE-op topology for the write-diff (W2).
                before_hist = _face_hist(d)
                before_vol = _LAST_VOLUME.get(d)

                # T-W12 auto-preflight: dry-run on shadow clone before mutating.
                # Freedom rule: fail verdict never blocks — we proceed regardless;
                # if verdict==fail we log a preflight_overridden event after apply.
                _pf_result: dict = {}
                try:
                    from services.mcp.preflight_tools import run_auto_preflight
                    _pf_result = run_auto_preflight(d, "add_features", ops)
                except Exception:  # noqa: BLE001
                    pass

                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
                fs = (r.get("rebuild") or {}).get("feature_status", [])
                try:
                    _enrich_zero_volume_errors(fs, ops, r.get("id_map") or {})
                except Exception:  # noqa: BLE001 — enrichment must never mask a failure
                    pass
                feats = [{"id": s.get("id"), "ok": s.get("ok"),
                          "error": s.get("error"), "warning": s.get("warning")} for s in fs]
                bodies = (r.get("tess") or {}).get("bodies", [])
                ok = all(s.get("ok") for s in fs) if fs else True

                if not ok and ckpt:
                    # Roll the tree back to the pre-batch revision; report WHY each feature failed.
                    reverted = False
                    try:
                        cb.call("doc_revert", {"doc_id": d, "checkpoint": ckpt})
                        reverted = True
                    except Exception:  # noqa: BLE001
                        reverted = False
                    fail = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                            if not s.get("ok")]
                    _publish_current(d)  # mirror the rolled-back (or current) state to the app
                    # The agent card renders `error` TRUNCATED (~160 chars) and never
                    # reads `failed_features`, so a constant "batch rejected" told the
                    # model nothing and it resent the identical ops. Carry the engine's
                    # actual per-feature reasons — named by feature — in `error` itself.
                    fnames = {}
                    idmap = r.get("id_map") or {}
                    for i, o in enumerate(ops):
                        f = o.get("feature") if isinstance(o, dict) else None
                        fid = idmap.get("$" + str(i))
                        if isinstance(f, dict) and f.get("name") and isinstance(fid, str) and fid:
                            fnames[fid] = str(f["name"])
                    parts = []
                    for f in fail:
                        fid = str(f.get("id") or "?")
                        why = str(f.get("error") or "").strip() or "no reason reported"
                        nm = fnames.get(f.get("id"))
                        parts.append(f"{fid} ({nm}): {why}" if nm else f"{fid}: {why}")
                    head = ("batch rejected, nothing was kept" if reverted
                            else "batch failed and rollback failed")
                    fail_result = {
                        "ok": False,
                        "doc_id": d,
                        "rolled_back": reverted,
                        "error": (head + " — " + "; ".join(parts)) if parts else head,
                        "failed_features": fail,
                        "features": feats,
                    }
                    if interpreted:
                        # The batch failed, so the rewrite is now a SUSPECT: say what we
                        # read the ops as before the model re-sends the same spelling.
                        fail_result["interpreted"] = interpreted
                    # T-BL22: rebuild failure -> IP-18 regen_diagnose fix-preview card
                    # (T-H11 seam). Diagnoses the FAILED tree from the ops response (the
                    # doc itself was already rolled back above), logs the model call as an
                    # IP-18 correction trace, and attaches the accept/edit/reject card to
                    # the failure response for the copilot/caller to surface. Best-effort:
                    # any error (no provider, no tree) degrades to the plain failure dict.
                    try:
                        failed_tree = r.get("tree") or (r.get("rebuild") or {}).get("tree")
                        if failed_tree:
                            from services.mcp.regen_tools import diagnose_failed_document
                            engine_err = "; ".join(
                                str(f.get("error")) for f in fail if f.get("error")) or None
                            diag = diagnose_failed_document(
                                {"revision": r.get("revision", 0), "tree": failed_tree,
                                 "owner_map": r.get("owner_map") or {}},
                                engine_error=engine_err)
                            if diag.get("ok"):
                                card = dict(diag["card"])
                                # strip in-process handles (not JSON-serializable intent)
                                card.pop("_corrupted_document", None)
                                card.pop("_fixed_document", None)
                                fail_result["regen_diagnosis"] = card
                    except Exception:  # noqa: BLE001 — diagnosis must never mask the failure
                        pass
                    return fail_result

                out = {
                    "ok": ok,
                    "doc_id": d,
                    "revision": r.get("revision"),
                    "body_count": len(bodies),
                    "volume_mm3": r.get("volume"),
                    "features": feats,
                }
                if interpreted:
                    out["interpreted"] = interpreted
                # GSL Stage 1a — SEMANTIC write-confirmation (W2): say what the op actually did
                # ("added 2 faces (+2 cylinder); 2 new bore/hole(s); volume 24000→23435") or warn
                # "no geometry change" so the model isn't left diffing a bare volume number.
                vol_after = r.get("volume")
                if ok:
                    try:
                        out["changes"] = _write_diff(before_hist, _face_hist(d), before_vol,
                                                     vol_after)
                    except Exception:  # noqa: BLE001
                        out["changes"] = {
                            "summary": "geometry changed but write-diff unavailable",
                            "faces_delta": None,
                        }
                    if isinstance(vol_after, (int, float)):
                        _LAST_VOLUME[d] = vol_after
                    # B3 — best-effort AUTO-WARNING: if this batch added feature(s), check the
                    # new batch against the rest of the tree for boss/cut overlap or a swallowed
                    # boss (features filter keeps this bounded to new-batch x existing pairs, not
                    # a full-tree scan). Never fails the batch — degrades to no warning.
                    added_ids = [fid for i, op in enumerate(ops)
                                 if isinstance(op, dict) and op.get("op") == "add_feature"
                                 for fid in [(r.get("id_map") or {}).get(f"${i}")] if fid]
                    if added_ids:
                        try:
                            ov = cb.call("eval_feature_overlap",
                                         {"doc_id": d, "features": added_ids})
                            if ov.get("pairs") or ov.get("dead_features"):
                                out["overlap_warnings"] = {
                                    "summary": ov.get("summary"),
                                    "pairs": ov.get("pairs", []),
                                    "dead_features": ov.get("dead_features", []),
                                }
                        except Exception:  # noqa: BLE001
                            pass
                # Push the updated part to the desktop app's viewport (if it's open) so the user
                # watches it build. Best-effort; only when something actually got built.
                if ok and bodies:
                    pub = _publish_current(d)
                    if pub:
                        out["shown_in_app"] = True
                    _autosave(d)  # persist so a server restart can recover this doc
                # Refresh the deliverable .step+ in the user's project folder so the file on
                # disk tracks the latest revision (no-op unless the workspace is configured).
                if ok:
                    saved = _project_save(d)
                    if saved:
                        out["saved_path"] = saved
                # T-W12: attach preflight advisory items and log override event.
                if _pf_result:
                    adv_items = _pf_result.get("advisory_items") or []
                    if adv_items:
                        out["preflight_advisory"] = adv_items
                    pf_verdict = _pf_result.get("verdict", "skipped")
                    if pf_verdict == "fail":
                        # Applied despite failed preflight — log audit trigger (§11).
                        try:
                            from services.mcp.preflight_tools import log_preflight_override
                            log_preflight_override(d, "add_features", _pf_result)
                        except Exception:  # noqa: BLE001
                            pass
                # Auto-readback for 2D sketch mutations (compact ASCII + NL summary).
                # Best-effort: never fails the batch if the live sketch load is unavailable.
                if ok:
                    try:
                        from mcp_server.ascii2d import (  # noqa: PLC0415
                            sketch_ids_from_ops, live_sketch_readback,
                        )
                        sids = sketch_ids_from_ops(ops)
                        # Also pick up newly-created sketch features referenced as $N
                        # once id_map has resolved them for sketch-bearing add_feature.
                        id_map = r.get("id_map") or {}
                        for i, op in enumerate(ops or []):
                            if not isinstance(op, dict):
                                continue
                            if op.get("op") != "add_feature":
                                continue
                            feat = op.get("feature") or {}
                            if (feat.get("type") or "").lower() != "sketch":
                                continue
                            fid = id_map.get(f"${i}")
                            if isinstance(fid, str) and fid not in sids:
                                sids.append(fid)
                        if sids:
                            readbacks = []
                            for sid in sids[:4]:
                                rb = live_sketch_readback(d, sid)
                                if rb:
                                    readbacks.append(rb)
                            if readbacks:
                                out["sketch_readbacks"] = readbacks
                                # Flatten first for models that expect top-level ascii/summary.
                                primary = readbacks[0]
                                out["ascii"] = primary.get("ascii")
                                out["ascii_block"] = primary.get("ascii_block")
                                out["sketch_summary"] = primary.get("summary")
                                out["ascii_readback"] = primary
                                out["readback_hint"] = primary.get("hint")
                                if primary.get("findings") is not None:
                                    out.setdefault("findings", primary.get("findings"))
                    except Exception:  # noqa: BLE001
                        pass
                return out

            if background:
                jid = _start_job("add_features", _apply)
                return _dumps({"ok": True, "doc_id": d, "job_id": jid, "status": "running",
                               "created_doc": created,
                               "note": f"applying {len(ops)} op(s) in the background — "
                                       f"poll get_job('{jid}')."})
            # Run on a worker thread with auto-handoff: a fast batch returns inline exactly as
            # before; a heavy one that overruns the budget keeps running and returns a job_id so
            # the transport call returns in time and the session survives.
            raw = _solve_sync_or_handoff("add_features", _apply)
            if created and isinstance(raw, dict):
                raw["created_doc"] = created
            # _solve_sync_or_handoff CATCHES an engine throw and hands it back as
            # {"ok":false,"error":…}, so the outer `except` never sees a rejected batch. Echo
            # the rewrites here too: a validation rejection is exactly when the model needs to
            # know how its arguments were read before it re-sends the same spelling.
            if (isinstance(raw, dict) and not raw.get("ok") and interpreted
                    and "interpreted" not in raw):
                raw["interpreted"] = interpreted
            # mega-8 F7: enrich thin unknown-op errors with closest + cad_guide
            # (C++ path sometimes returns only "unknown op 'cylinder'").
            try:
                from services.mcp import op_vocab as _ov  # noqa: PLC0415
                enriched = _ov.enrich_unknown_op_error(raw)
                if isinstance(enriched, dict):
                    return _dumps(enriched)
                if isinstance(enriched, str):
                    return enriched
            except Exception:  # noqa: BLE001
                pass
            return _dumps(raw) if not isinstance(raw, str) else raw
        except Exception as e:  # noqa: BLE001
            # A rejected batch is exactly when the model most needs to see how its arguments
            # were read: an interpretation it did not expect is the first thing to check
            # before it re-sends the identical spelling.
            raw_err = _err(e)
            notes = _interpreted_notes(interpreted)
            if not notes:
                return raw_err
            try:
                obj = json.loads(raw_err)
                obj["interpreted"] = notes
                return _dumps(obj)
            except Exception:  # noqa: BLE001
                return raw_err



    @mcp.tool()
    def get_part(doc_id: Optional[str] = None, summary: bool = False) -> str:
        """Return the feature tree (features, parameters, ids) + revision for editing.

        summary=True returns a LIGHTWEIGHT tree — feature id/name/type/suppressed + parameters,
        with heavy embedded geometry (the import features' base64 BREP blobs) and any oversized
        string value ELIDED. Use it on big assemblies/imported parts where the full tree blows
        the tool token limit (a 600k-char part lists in a few KB). The full (default) form
        carries the complete params verbatim for round-trip editing.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_tree", {"doc_id": d})
            if not summary:
                return _dumps(r)

            def _trim(value: Any) -> Any:
                # Elide base64 geometry blobs / very long strings that bloat the tree but carry
                # no design intent the caller would read in a summary.
                if isinstance(value, str) and len(value) > 200:
                    return f"<{len(value)} chars elided>"
                if isinstance(value, dict):
                    return {k: ("<elided>" if k in ("embedded_brep",) else _trim(v))
                            for k, v in value.items()}
                if isinstance(value, list):
                    return [_trim(v) for v in value] if len(value) <= 64 else \
                        f"<list of {len(value)} elided>"
                return value

            tree = r.get("tree") or {}
            feats = tree.get("features") or []
            out_feats = []
            for f in feats:
                if not isinstance(f, dict):
                    continue
                params = {k: ("<elided>" if k == "embedded_brep" else _trim(v))
                          for k, v in (f.get("params") or {}).items()}
                out_feats.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "suppressed": bool(f.get("suppressed", False)),
                    "params": params,
                })
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "summary": True, "feature_count": len(out_feats),
                           "features": out_feats})
        except Exception as e:  # noqa: BLE001
            return _err(e)






    @mcp.tool()
    def set_material(material: Union[str, dict, list],
                     body: Optional[Union[str, int]] = None,
                     doc_id: Optional[str] = None) -> str:
        """Set a body's MATERIAL APPEARANCE — colour + finish — so it renders like the real material.

        material: a preset name (aluminum, steel, stainless, brass, copper, gold, plastic, abs,
        black) OR an explicit dict {"color": [r,g,b] 0..1, "metallic"?: 0..1, "roughness"?: 0..1},
        OR a bare [r,g,b] list for a plain colour.
        body: which body — "b1", "b2", ... (1-based) or an int ordinal. Default: ALL bodies.

        Persists on the document (round-trips through the native .step+) and is picked up by
        render_part. NOTE: this is the VISUAL material; to set the material for SIMULATION
        (density/stiffness) pass `material=` to run_fea_static / run_cfd_flow instead.
        """
        try:
            d = _doc(doc_id)
            ap = _resolve_appearance(material)
            # Resolve target body keys. None -> all bodies (an empty op batch returns the tess
            # body list without mutating the doc); int -> "b<n>"; str -> the key as given.
            if body is None:
                info = cb.call("doc_apply_ops", {"doc_id": d, "ops": []})
                n = len(info.get("tess", {}).get("bodies", [])) or 1
                targets = [f"b{i + 1}" for i in range(n)]
            elif isinstance(body, int):
                targets = [f"b{body}"]
            else:
                targets = [str(body)]
            ops = [{"op": "set_appearance", "id": k, **ap} for k in targets]
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            return _dumps({"ok": True, "doc_id": d, "bodies": targets,
                           "appearance": ap, "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def undo(doc_id: Optional[str] = None) -> str:
        """Undo the last edit on the current part.

        Multi-user note: from mcp/agent connections the daemon rejects this with
        code ``undo_denied`` (agents use doc_checkpoint/doc_revert or PLM rollback).
        Set env ``CHUDWARE_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after undo, call render_part()/render_views() (and inspect_feature on the
        affected region) to confirm the rolled-back state, then verify_battery before done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_undo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def redo(doc_id: Optional[str] = None) -> str:
        """Redo the last undone edit on the current part.

        Multi-user note: from mcp/agent connections the daemon rejects this with
        code ``undo_denied`` (agents use doc_checkpoint/doc_revert or PLM rollback).
        Set env ``CHUDWARE_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after redo, call render_part()/render_views() to SEE the restored state
        and verify_battery before declaring done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_redo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def show_in_app(doc_id: Optional[str] = None) -> str:
        """Push the current part to the OPEN Chudware desktop app's 3D viewport so the user can see it.

        Modeling tools already do this automatically after each change; call this to force a refresh
        (e.g. after switching the current part). No-op effect if the desktop app isn't running — the
        part is written to the handoff folder and shown whenever the app next opens.
        """
        try:
            d = _doc(doc_id)
            pub = _publish_current(d)
            if not pub:
                return _dumps({"ok": False, "error": "could not publish (no geometry yet?)"})
            return _dumps({"ok": True, "doc_id": d, **pub})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_parts() -> str:
        """List all open part/drawing documents in this engine session."""
        try:
            return _dumps(cb.call("doc_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def close_part(doc_id: Optional[str] = None, discard: bool = False) -> str:
        """Close a part document and free it. Clears the current part if it was current.

        SAVE DISCIPLINE: docs you (the AI) create are AI-owned. The desktop app never warns
        the user about YOUR unsaved docs — so YOU are responsible for saving deliverables
        before closing. If the doc has unsaved changes this returns {"ok": false,
        "code": "unsaved_changes"} WITHOUT closing; call save_part(path=...) to deliver it,
        or close_part(discard=True) to intentionally drop scratch work.

        If another client (e.g. the user, who adopted this doc by editing it) is still
        editing, the daemon degrades the close to a detach and returns {"detached": true} —
        your live view drops but their doc lives on.
        """
        try:
            d = _doc(doc_id)
            if not discard:
                # "Undelivered work" = mutated since the last save_part/export_part. Revision is
                # the reliable signal (the engine `dirty` flag is cleared by the MCP autosave's
                # doc_save to a temp path, so it would false-negative here).
                name = d
                for e in cb.call("doc_list", {}).get("docs", []):
                    if isinstance(e, dict) and e.get("doc_id") == d:
                        name = e.get("name") or d
                        break
                rev = _doc_revision(d)
                delivered = _LAST_DELIVER_REV.get(d)
                if rev is not None and rev > 0 and delivered != rev:
                    return _dumps({
                        "ok": False, "code": "unsaved_changes", "doc_id": d, "dirty": True,
                        "message": f"{name} has unsaved changes — call save_part(path=...) "
                                   f"to deliver it, or close_part(discard=True) to drop it",
                    })
            r = cb.call("doc_close", {"doc_id": d})
            if r.get("detached"):
                # Another client is editing it: our view detached, the doc lives on.
                _unpublish(d)
                if _CUR["doc_id"] == d:
                    _CUR["doc_id"] = None
                _LAST_DELIVER_REV.pop(d, None)
                return _dumps({"ok": True, "detached": True, "doc_id": d,
                               "note": r.get("note")})
            _unpublish(d)  # drop its mirror tab in the app
            if _CUR["doc_id"] == d:
                _CUR["doc_id"] = None
            # Prune recovery aliases that resolve to this now-closed doc. The bridge
            # outlives daemon restarts (which reset the doc-id counter), so a stale
            # old_id -> d alias could later misroute an agent's call to a REUSED d.
            for k in [k for k in _DOC_ALIASES if k == d or _resolve_alias(k) == d]:
                _DOC_ALIASES.pop(k, None)
            _LAST_DELIVER_REV.pop(d, None)
            return _dumps({"ok": True, "closed": r.get("closed", True), "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def save_part(path: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Save the part as Chudware's NATIVE .step+ — the editable, reopenable format. PREFER THIS.

        .step+ is the only format that round-trips the full PARAMETRIC feature tree (so the user
        can keep editing parameters/features), its SIMULATION STUDIES (FEA/CFD setups + results),
        and any associated 2D drawings — all in one file the desktop app opens fully editable. Use
        this to deliver a part the user will keep working on. (export_part writes flat interchange
        or mesh files: a plain STEP/STL has geometry only, no feature history or studies.)

        path : where to write. If omitted, a temp .step+ is written. A ".step+" extension is added
               if missing. Returns the written path and bytes so you can confirm a real save.

        Save every deliverable BEFORE your session ends: the user is never warned about your
        unsaved docs, so anything you don't save (and don't close_part(discard=True)) is only
        recoverable from a fallback crash-recovery tier — not a delivery mechanism.
        """
        try:
            d = _doc(doc_id)
            # K3: canonicalise the caller-supplied path so any '..' traversal sequences are
            # fully resolved before we pass them to the engine.  This means the caller sees
            # exactly where the file will be written (no hidden navigation).
            out = _sanitize_output_path(path) if path else _tmp(".step+")
            if not out.lower().endswith(".step+"):
                out += ".step+"
            r = cb.call("doc_save", {"doc_id": d, "path": out})
            written = r.get("path", out)
            ok = os.path.exists(written)
            if ok:
                # Delivered: record the revision so close_part won't warn until further edits.
                rev = _doc_revision(d)
                if rev is not None:
                    _LAST_DELIVER_REV[d] = rev
            return _dumps({"ok": ok, "format": "step+", "native": True, "doc_id": d,
                           "path": written,
                           "bytes": (os.path.getsize(written) if ok else 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def get_job(job_id: str) -> str:
        """Poll a background job started by run_cfd_flow/run_fea_*(background=True).

        Returns {status: "running"|"done"|"error"}; when done, the solve `result` is included.
        Polling a long solve this way never trips the per-call transport timeout that would
        otherwise drop the whole session.
        """
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return _dumps({"ok": False, "error": f"unknown job_id {job_id!r}"})
            snap = dict(job)
        out = {"ok": True, "job_id": job_id, "status": snap["status"], "kind": snap["kind"]}
        if snap["status"] == "running" and snap.get("progress"):
            progress = dict(snap["progress"])  # live {pct, stage, text} for long OpenFOAM runs
            # LBM reports progress via a file the engine writes each checkpoint — fold it in so the
            # built-in CFD path gets an ETA-style readout like OpenFOAM (report footnote).
            pf = progress.pop("progress_file", None)
            if pf:
                try:
                    with open(pf, encoding="utf-8") as fh:
                        progress.update(json.load(fh))
                except (OSError, ValueError):
                    pass  # not written yet / mid-write — keep the seed
            out["progress"] = progress
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] == "error":
            out["ok"] = False
            out["error"] = snap["error"]
        return _dumps(out)



    @mcp.tool()
    def recover_parts(all: bool = False) -> str:
        """Reopen auto-saved parts after a server restart (recover a dropped session).

        Every successful add_features auto-persists the part to a recoverable .step+; if the
        server was restarted (e.g. a client timeout killed it) the in-memory docs are gone — call
        this to reload them (each gets a fresh doc_id). list_parts() then shows them.

        By default this recovers ONLY the most-recent prior session's parts (the one that was
        dropped) — not the entire autosave history, so you don't drag in unrelated old docs.
        Pass all=True to reach every autosaved part. In daemon mode this also reopens parts the
        daemon retired (unclosed docs from ended sessions — they were saved and closed instead of
        lingering forever; nothing reopens them except this call or the GUI's recovery prompt).
        Each entry returns its fresh `doc_id` plus the saved `name` and `original_doc_id` so you
        can re-map old handles. Stale autosaves (older than CHUDWARE_AUTOSAVE_MAX_AGE_DAYS,
        default 7) are pruned automatically.
        """
        try:
            recovered = recover_autosaved(scope="all" if all else "recent",
                                          include_orphaned=True)
            return _dumps({"ok": True, "recovered": recovered, "count": len(recovered),
                           "scope": "all" if all else "recent"})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- configurations

    @mcp.tool()
    def list_configurations(doc_id: Optional[str] = None) -> str:
        """List the part's configurations (parametric variants) and which one is active.

        A configuration is a named variant of the SAME feature tree with its own parameter
        overrides and feature suppressions — e.g. "Long" / "Short" of one bracket, or a family of
        sizes. Returns {configurations:[names], active, detail:[{name, param_overrides,
        suppressions}]}. A fresh part has just the implicit "Default". Build variants with
        add_configuration + set_config_override / set_config_suppression, switch with
        activate_configuration; measure_part/render_part then reflect the active variant.
        """
        try:
            return _dumps(cb.call("config_list", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_configuration(name: str, copy_from: Optional[str] = None,
                          doc_id: Optional[str] = None) -> str:
        """Add a new configuration (a named parametric variant) to the current part.

        name      : the new configuration's name (must be unique).
        copy_from : an existing configuration name to seed its overrides/suppressions from
                    (omit to start empty = identical to the base part). Then tailor it with
                    set_config_override / set_config_suppression, and activate_configuration to
                    make it current. The new config is created but NOT activated.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_add", {"doc_id": d, "name": name, "copy_from": copy_from})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "added": name,
                           "active": (r.get("tree") or {}).get("active_configuration"),
                           "configurations": [c.get("name") for c in
                                              (r.get("tree") or {}).get("configurations", [])]})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def activate_configuration(name: str, doc_id: Optional[str] = None) -> str:
        """Switch the active configuration. The part rebuilds to that variant's parameters.

        After this, measure_part / render_part / export_part reflect the chosen variant. Returns
        the new active configuration name and the rebuilt body count/volume so you can confirm the
        switch took (e.g. a suppressed feature drops a body, an override changes the volume).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_activate", {"doc_id": d, "name": name})
            bodies = (r.get("tess") or {}).get("bodies", [])
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "active": name,
                           "body_count": len(bodies),
                           "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_override(config: str, feature_id: str, param: str,
                            value: Optional[Union[float, list]] = None, clear: bool = False,
                            doc_id: Optional[str] = None) -> str:
        """Override one feature parameter in a configuration (the parametric-variant edit).

        config     : the configuration name to edit (see list_configurations).
        feature_id : the feature's id from get_part (e.g. "f1").
        param      : the parameter name on that feature (e.g. "length", "radius", "translate").
        value      : the override value for this configuration only — a plain NUMBER for a scalar
                     param (length/radius/angle…) OR a LIST for a vector param, e.g.
                     translate:[x,y,z] on a `transform` feature (so one assembly file can hold a
                     Default and a STOWED config that differ only in a placement vector).
        clear      : pass True (and omit value) to REMOVE the override so the config falls back to
                     the base value.
        The base/Default config and other configurations are untouched. Activate the configuration
        (or it may already be active) and measure_part to see the effect.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "name": config, "feature_id": feature_id,
                            "param": param}
            if clear or value is None:
                params["clear"] = True
            elif isinstance(value, (list, tuple)):
                # Vector/array param (e.g. transform.translate): the engine reads it as a raw
                # array (opt_vec3), so the override must be the bare list — NOT wrapped in a
                # {value, expr} scalar object. Coerce numeric components to float.
                params["value"] = [float(x) if isinstance(x, (int, float)) else x for x in value]
            else:
                # Scalar params are {value, expr} objects in the engine; wrap a bare number.
                params["value"] = {"value": float(value), "expr": None}
            cb.call("config_set_override", params)
            _publish_current(d)
            _autosave(d)
            echoed = None
            if not (clear or value is None):
                echoed = (params["value"] if isinstance(value, (list, tuple)) else float(value))
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "param": param,
                           "value": echoed,
                           "cleared": bool(clear or value is None)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_suppression(config: str, feature_id: str, suppressed: bool = True,
                               doc_id: Optional[str] = None) -> str:
        """Suppress (or un-suppress) a feature in a configuration — turn a feature ON/OFF per variant.

        config     : the configuration name (see list_configurations).
        feature_id : the feature id from get_part (e.g. "f3" — a fillet, a hole, a boss).
        suppressed : True to remove that feature in this configuration; False to bring it back.
        Lets one variant include a feature (a mounting boss, a chamfer) that another omits, without
        forking the feature tree. Activate the configuration + render_part to confirm.
        """
        try:
            d = _doc(doc_id)
            cb.call("config_set_suppression", {"doc_id": d, "name": config,
                                               "feature_id": feature_id,
                                               "suppressed": bool(suppressed)})
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "suppressed": bool(suppressed)})
        except Exception as e:  # noqa: BLE001
            return _err(e)
