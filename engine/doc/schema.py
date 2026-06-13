"""Schema validation for aicad.part/1 and aicad.asm/1 documents.

This is the executable form of docs/contracts/doc_schema.md + feature_params.md:
structural validation (envelopes, types, arities, handles, selectors, dimension values,
expression grammar + name resolution). Geometric/semantic validation (does the profile
close, does the axis cross the profile) belongs to the M1 compiler, not here.

Every validate_* function returns a list of error strings (empty = valid), each prefixed
with a JSON-path-ish locator so failures are actionable.
"""

from __future__ import annotations

import re
from typing import Any

from .equations import EquationError, expr_names, validate_expr

SCHEMA_PART = "aicad.part/1"
SCHEMA_ASM = "aicad.asm/1"
SCHEMA_DRAWING = "aicad.drawing/1"  # W12 — 2D orthographic/iso drawing of a source part/assembly

# Drawing sheet sizes (ISO A-series + ANSI). Width/height (landscape) lookup lives in drawing.py;
# the validator only checks the size token is one we know.
SHEET_SIZES = {"A4", "A3", "A2", "A1", "A0", "A", "B", "C", "D", "E"}
# Drawing view kinds + canonical orientations (Z-up: front looks +Y, top -Z, right -X — see
# engine/doc/features/datum.py reserved_datums + drawing.py orientation table).
VIEW_TYPES = {"base", "projected", "iso", "section", "detail"}
VIEW_ORIENTATIONS = {"front", "back", "top", "bottom", "left", "right", "iso",
                     "section", "detail"}

RESERVED_DATUMS = {
    "origin",
    "plane.front",
    "plane.top",
    "plane.right",
    "axis.x",
    "axis.y",
    "axis.z",
}

UNIT_SYSTEMS = {"MMGS", "IPS"}

HANDLE_RE = re.compile(r"^e\d+(\.(p1|p2|mid|p|center|start|end|vertex|apex|p\d+))?$")
FEATURE_ID_RE = re.compile(r"^f\d+$")

SELECTOR_KINDS = {"face", "edge", "vertex", "body", "datum", "region"}
FACE_SURFS = {"plane", "cylinder", "cone", "sphere", "torus", "bspline", "other"}
EDGE_SURFS = {"line", "circle", "ellipse", "bspline", "other"}

MATE_TYPES = {
    "coincident", "parallel", "perpendicular", "tangent",
    "concentric", "distance", "angle", "lock",
}
MATE_VALUE_TYPES = {"distance", "angle"}

# Entity type -> required fields beyond {id, type, construction}
ENTITY_FIELDS: dict[str, list[str]] = {
    "point": ["p"],
    "line": ["p1", "p2"],
    "circle": ["center", "radius"],
    "arc": ["center", "radius", "start", "end", "ccw"],
    "ellipse": ["center", "rx", "ry", "rot"],
    "ellipse_arc": ["center", "rx", "ry", "rot", "a0", "a1"],
    "parabola": ["vertex", "focus", "t0", "t1"],
    "conic": ["p0", "p1", "apex", "rho"],
    "spline": ["points"],
    "slot": ["p1", "p2", "width"],
    "arc_slot": ["center", "radius", "a0", "a1", "width"],  # C30-B: arc-spine slot
    "text": ["p", "text", "height"],
}

# Entity type -> point sub-handles it exposes
_ENTITY_SUBS: dict[str, set[str]] = {
    "point": {"p"},
    "line": {"p1", "p2", "mid"},   # mid = line midpoint (SolidWorks midpoint dim target)
    "circle": {"center"},
    "arc": {"center", "start", "end"},
    "ellipse": {"center"},
    "ellipse_arc": {"center", "start", "end"},
    "parabola": {"vertex", "start", "end"},
    "conic": {"start", "end", "apex"},
    "slot": {"p1", "p2"},
    "arc_slot": {"center", "start", "end"},  # C30-B: derived spine endpoints
    "text": {"p"},
    # spline handled specially (p0..pN)
}

# Constraint type -> (min_refs, max_refs, needs_value)
CONSTRAINT_SPECS: dict[str, tuple[int, int, bool]] = {
    "coincident": (2, 2, False),
    "horizontal": (1, 2, False),
    "vertical": (1, 2, False),
    "parallel": (2, 2, False),
    "perpendicular": (2, 2, False),
    "tangent": (2, 2, False),
    "collinear": (2, 2, False),
    "equal": (2, 2, False),
    "concentric": (2, 2, False),
    "coradial": (2, 2, False),
    "midpoint": (2, 2, False),
    "symmetric": (3, 3, False),
    "fix": (1, 1, False),
    "point_on": (2, 2, False),
    "distance": (2, 2, True),
    "angle": (2, 2, True),
    "radius": (1, 1, True),
    "diameter": (1, 1, True),
}

# Ref-slot value forms
_SEL = "selector"                  # selector object
_SEL_OR_DATUM = "selector_or_datum"  # selector object | datum feature id | reserved datum
_SEL_LIST = "selector_list"
_FEAT_LIST = "feature_id_list"
_COMP_LIST = "component_id_list"
_UV_LIST = "uv_list"               # list of [u,v] local-frame offsets (C33-A hole positions)
_ENTITY_ID_LIST = "entity_id_list"  # list of sketch entity ids e<N> (weldment segment picks)

# Feature type -> {"params": [required keys], "refs": {slot: (form, required)}}
FEATURE_TYPES: dict[str, dict[str, Any]] = {
    "plane": {"params": ["method"], "refs": {"base": (_SEL_OR_DATUM, False),
                                             "second": (_SEL_OR_DATUM, False),
                                             "points": (_SEL_LIST, False)}},
    "axis": {"params": ["method"], "refs": {"a": (_SEL_OR_DATUM, False),
                                            "b": (_SEL_OR_DATUM, False)}},
    "point": {"params": ["method"], "refs": {"target": (_SEL_OR_DATUM, False)}},
    "csys": {"params": [], "refs": {"origin": (_SEL_OR_DATUM, False),
                                    "x_dir": (_SEL_OR_DATUM, False),
                                    "y_dir": (_SEL_OR_DATUM, False)}},
    "sketch": {"params": ["entities", "constraints"], "refs": {"plane": (_SEL_OR_DATUM, True)}},
    "box": {"params": ["length", "width", "height"], "refs": {"at": (_SEL_OR_DATUM, False)}},
    "cylinder": {"params": ["radius", "height"], "refs": {"at": (_SEL_OR_DATUM, False)}},
    "sphere": {"params": ["radius"], "refs": {"at": (_SEL_OR_DATUM, False)}},
    "cone": {"params": ["r1", "r2", "height"], "refs": {"at": (_SEL_OR_DATUM, False)}},
    "torus": {"params": ["major_r", "minor_r"], "refs": {"at": (_SEL_OR_DATUM, False)}},
    "extrude": {"params": ["mode", "end", "depth"], "refs": {"profile": (_SEL_LIST, True)}},
    "revolve": {"params": ["mode", "angle"], "refs": {"profile": (_SEL_LIST, True),
                                                      "axis": (_SEL_OR_DATUM, True)}},
    "sweep": {"params": ["mode"], "refs": {"profile": (_SEL_LIST, True),
                                           "path": (_SEL_OR_DATUM, True)}},
    "loft": {"params": ["mode"], "refs": {"sections": (_SEL_LIST, True)}},
    "helix": {"params": ["pitch", "height"], "refs": {"base": (_SEL_OR_DATUM, True)}},
    # fillet: edges/radius required only on the normal path; the full_round path
    # uses params.full_round + refs.middle_face (enforced in _validate_feature).
    "fillet": {"params": [], "refs": {"edges": (_SEL_LIST, False),
                                      "middle_face": (_SEL_OR_DATUM, False)}},
    "chamfer": {"params": ["length"], "refs": {"edges": (_SEL_LIST, True)}},
    "shell": {"params": ["thickness"], "refs": {"remove": (_SEL_LIST, False)}},
    "draft": {"params": ["angle"], "refs": {"neutral": (_SEL_OR_DATUM, True),
                                            "pull": (_SEL_OR_DATUM, False),
                                            "faces": (_SEL_LIST, True)}},
    "hole": {"params": ["style", "end"], "refs": {"face": (_SEL_OR_DATUM, False),
                                                  "positions": (_SEL_LIST, False),
                                                  "positions_sketch": (_SEL_OR_DATUM, False),
                                                  "points": (_UV_LIST, False)}},
    "linear_pattern": {"params": ["count1", "spacing1"],
                       "refs": {"dir1": (_SEL_OR_DATUM, True),
                                "dir2": (_SEL_OR_DATUM, False),
                                "features": (_FEAT_LIST, True)}},
    "circular_pattern": {"params": ["count", "angle"],
                         "refs": {"axis": (_SEL_OR_DATUM, True),
                                  "features": (_FEAT_LIST, True)}},
    "mirror": {"params": [], "refs": {"plane": (_SEL_OR_DATUM, True),
                                      "features": (_FEAT_LIST, False),
                                      "bodies": (_SEL_LIST, False)}},
    "boolean": {"params": ["op"], "refs": {"targets": (_SEL_LIST, True),
                                           "tools": (_SEL_LIST, False)}},
    # Sheet metal — base flange (sheet from a region) + edge flange (bent wall off an edge).
    "sm_base_flange": {"params": [], "refs": {"profile": (_SEL_LIST, True)}},
    "sm_edge_flange": {"params": [], "refs": {"edge": (_SEL_OR_DATUM, True)}},
    # Hem (fold an edge back on itself) + jog (two bends -> a parallel offset step).
    "sm_hem": {"params": [], "refs": {"edge": (_SEL_OR_DATUM, True)}},
    "sm_jog": {"params": [], "refs": {"edge": (_SEL_OR_DATUM, True)}},
    # Flat pattern (unfold) — operates on the running sheet-metal body + stored bend records.
    "sm_flat_pattern": {"params": [], "refs": {}},
    # Weldments — sweep a structural profile along sketch segments → members + cut-list data.
    # refs.sketch is a sketch feature id; refs.segments (optional) is a list of entity ids.
    "weld_member": {"params": ["profile"], "refs": {"sketch": (_SEL_OR_DATUM, True),
                                                    "segments": (_ENTITY_ID_LIST, False)}},
    # Weldment dress features — gusset (plate in an L-corner of two members) + end cap (plate
    # closing a member's open end). Both resolve from picked planar faces on the member bodies.
    "weld_gusset": {"params": [], "refs": {"faces": (_SEL_LIST, True)}},
    "weld_end_cap": {"params": [], "refs": {"face": (_SEL_OR_DATUM, True)}},
    "script": {"params": ["code"], "refs": {}},
    "import": {"params": ["source_path", "format"], "refs": {}},
    # Stretch — schema-reserved (envelope accepted, no deep checks yet):
    "rib": {"params": [], "refs": {}},
    "split": {"params": [], "refs": {}},
    # Assembly component patterns:
    "asm_linear_pattern": {"params": ["count1", "spacing1"],
                           "refs": {"dir1": (_SEL_OR_DATUM, True),
                                    "components": (_COMP_LIST, True)}},
    "asm_circular_pattern": {"params": ["count", "angle"],
                             "refs": {"axis": (_SEL_OR_DATUM, True),
                                      "components": (_COMP_LIST, True)}},
}


# --------------------------------------------------------------------------- helpers

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_vec(v: Any, n: int) -> bool:
    return isinstance(v, list) and len(v) == n and all(_is_num(x) for x in v)


def validate_dim(v: Any, where: str, globals_names: set[str]) -> list[str]:
    """A dimension value: {"value": float, "expr": str|null}."""
    errs: list[str] = []
    if not isinstance(v, dict):
        return [f"{where}: dimension must be an object {{value, expr}}"]
    if "value" not in v or not _is_num(v["value"]):
        errs.append(f"{where}.value: missing or non-numeric")
    expr = v.get("expr", None)
    if expr is not None:
        if not isinstance(expr, str):
            errs.append(f"{where}.expr: must be string or null")
        else:
            e = validate_expr(expr)
            if e:
                errs.append(f"{where}.expr: {e}")
            else:
                try:
                    unknown = expr_names(expr) - globals_names
                except EquationError as exc:
                    unknown = set()
                    errs.append(f"{where}.expr: {exc}")
                if unknown:
                    errs.append(
                        f"{where}.expr: unknown name(s) {sorted(unknown)}"
                    )
    return errs


def validate_selector(sel: Any, where: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(sel, dict):
        return [f"{where}: selector must be an object"]
    kind = sel.get("kind")
    if kind not in SELECTOR_KINDS:
        return [f"{where}.kind: '{kind}' not in {sorted(SELECTOR_KINDS)}"]
    if kind == "datum":
        feat = sel.get("feature")
        if not (isinstance(feat, str) and (FEATURE_ID_RE.match(feat) or feat in RESERVED_DATUMS)):
            errs.append(f"{where}.feature: datum selector needs a feature id or reserved datum")
        return errs
    if kind == "region":
        # Three valid forms (any may carry the sketch feature id):
        #   whole sketch  -> {sketch}                (no index/centroid; all material regions)
        #   indexed pick  -> {sketch, index}         (stable region ordinal from the UI)
        #   legacy match  -> {sketch, centroid, hint_index}
        if not isinstance(sel.get("sketch"), str):
            errs.append(f"{where}.sketch: region selector needs a sketch feature id")
        if sel.get("index") is not None and not _is_num(sel.get("index")):
            errs.append(f"{where}.index: region index must be a number")
        if sel.get("centroid") is not None:
            if not _is_vec(sel.get("centroid"), 2):
                errs.append(f"{where}.centroid: region centroid must be [u,v]")
            if not _is_num(sel.get("hint_index")):
                errs.append(f"{where}.hint_index: required when a centroid is given")
        return errs
    if kind == "body":
        if not isinstance(sel.get("owner_feature"), str):
            errs.append(f"{where}.owner_feature: required for body selectors")
        return errs
    # face / edge / vertex
    if not _is_vec(sel.get("centroid"), 3):
        errs.append(f"{where}.centroid: must be [x,y,z]")
    surf = sel.get("surf")
    if kind == "face" and surf is not None and surf not in FACE_SURFS:
        errs.append(f"{where}.surf: '{surf}' not a face surface type")
    if kind == "edge" and surf is not None and surf not in EDGE_SURFS:
        errs.append(f"{where}.surf: '{surf}' not an edge curve type")
    axis = sel.get("axis")
    if axis is not None and not _is_vec(axis, 3):
        errs.append(f"{where}.axis: must be [x,y,z] or null")
    if not isinstance(sel.get("owner_feature"), (str, type(None))):
        errs.append(f"{where}.owner_feature: must be a feature id or null")
    return errs


def _validate_sel_or_datum(v: Any, where: str) -> list[str]:
    if isinstance(v, str):
        if v in RESERVED_DATUMS or FEATURE_ID_RE.match(v):
            return []
        return [f"{where}: '{v}' is neither a reserved datum nor a feature id"]
    return validate_selector(v, where)


# --------------------------------------------------------------------------- sketch

def _handle_targets_entity(handle: str, entities_by_id: dict[str, dict]) -> str | None:
    """Return an error string if the handle doesn't fit its entity, else None."""
    base, _, sub = handle.partition(".")
    ent = entities_by_id.get(base)
    if ent is None:
        return f"references unknown entity '{base}'"
    if not sub:
        return None
    etype = ent.get("type")
    if etype == "spline":
        if re.match(r"^p\d+$", sub):
            npts = len(ent.get("points") or [])
            idx = int(sub[1:])
            return None if idx < npts else f"spline point '{sub}' out of range"
        return f"'{sub}' is not a spline handle"
    subs = _ENTITY_SUBS.get(etype or "", set())
    if sub not in subs:
        return f"'{sub}' is not a handle of a {etype}"
    return None


def validate_entity(e: Any, where: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(e, dict):
        return [f"{where}: entity must be an object"]
    etype = e.get("type")
    if etype not in ENTITY_FIELDS:
        return [f"{where}.type: '{etype}' not a sketch entity type"]
    if not isinstance(e.get("id"), str) or not re.match(r"^e\d+$", e["id"]):
        errs.append(f"{where}.id: must match e<N>")
    if not isinstance(e.get("construction"), bool):
        errs.append(f"{where}.construction: required bool")
    for field in ENTITY_FIELDS[etype]:
        if field not in e:
            errs.append(f"{where}.{field}: required for {etype}")
            continue
        v = e[field]
        if field in ("p", "p1", "p2", "p0", "center", "vertex", "focus", "apex"):
            if not _is_vec(v, 2):
                errs.append(f"{where}.{field}: must be [x,y]")
        elif field == "points":
            if not (isinstance(v, list) and len(v) >= 2 and all(_is_vec(p, 2) for p in v)):
                errs.append(f"{where}.points: must be >=2 [x,y] pairs")
        elif field == "ccw":
            if not isinstance(v, bool):
                errs.append(f"{where}.ccw: must be bool")
        elif field == "text":
            if not isinstance(v, str):
                errs.append(f"{where}.text: must be string")
        else:  # radius, start, end, rx, ry, rot, width, height
            if not _is_num(v):
                errs.append(f"{where}.{field}: must be a number")
    return errs


def validate_constraint(
    c: Any, where: str, entities_by_id: dict[str, dict], globals_names: set[str]
) -> list[str]:
    errs: list[str] = []
    if not isinstance(c, dict):
        return [f"{where}: constraint must be an object"]
    ctype = c.get("type")
    if ctype not in CONSTRAINT_SPECS:
        return [f"{where}.type: '{ctype}' not a constraint type"]
    if not isinstance(c.get("id"), str) or not re.match(r"^c\d+$", c["id"]):
        errs.append(f"{where}.id: must match c<N>")
    lo, hi, needs_value = CONSTRAINT_SPECS[ctype]
    refs = c.get("refs")
    if not isinstance(refs, list) or not (lo <= len(refs) <= hi):
        errs.append(f"{where}.refs: {ctype} takes {lo}..{hi} refs")
        refs = []
    for i, h in enumerate(refs):
        if isinstance(h, str) and h in ("origin", "origin.p"):
            continue  # C50: virtual fixed sketch origin — valid in any point slot
        if not isinstance(h, str) or not HANDLE_RE.match(h):
            errs.append(f"{where}.refs[{i}]: '{h}' is not a valid handle")
            continue
        bad = _handle_targets_entity(h, entities_by_id)
        if bad:
            errs.append(f"{where}.refs[{i}]: {bad}")
    if needs_value:
        if "value" not in c:
            errs.append(f"{where}.value: required for {ctype}")
        else:
            errs += validate_dim(c["value"], f"{where}.value", globals_names)
        if "driven" in c and not isinstance(c["driven"], bool):
            errs.append(f"{where}.driven: must be bool")
        # Optional annotation anchor [u, v] in sketch-plane coords (C35-overhaul §4).
        if "placement" in c and c["placement"] is not None and not _is_vec(c["placement"], 2):
            errs.append(f"{where}.placement: must be [u, v] (two numbers) or null")
    elif c.get("value") is not None:
        errs.append(f"{where}.value: {ctype} must not carry a value")

    # Dimensional-constraint ref typing (C35-overhaul §3): angle is between two LINE entities;
    # distance accepts point-point (optional axis), or point-line (perpendicular foot distance).
    if ctype == "angle":
        for i, h in enumerate(refs[:2]):
            base = h.split(".")[0] if isinstance(h, str) else None
            ent = entities_by_id.get(base) if base else None
            if "." in (h or "") or (ent is not None and ent.get("type") != "line"):
                errs.append(f"{where}.refs[{i}]: angle takes two line entity ids")
    # collinear: two whole LINE entities lying on the same infinite line.
    if ctype == "collinear":
        for i, h in enumerate(refs[:2]):
            base = h.split(".")[0] if isinstance(h, str) else None
            ent = entities_by_id.get(base) if base else None
            if "." in (h or "") or (ent is not None and ent.get("type") != "line"):
                errs.append(f"{where}.refs[{i}]: collinear takes two line entity ids")
    # coradial: two whole arc/circle entities (same center AND same radius).
    if ctype == "coradial":
        for i, h in enumerate(refs[:2]):
            base = h.split(".")[0] if isinstance(h, str) else None
            ent = entities_by_id.get(base) if base else None
            if "." in (h or "") or (ent is not None and ent.get("type") not in ("circle", "arc")):
                errs.append(f"{where}.refs[{i}]: coradial takes two arc/circle entity ids")
    # C50: distance arc-conditions — optional "cond", a 2-list aligned with refs,
    # each "center"|"min"|"max" (honored only for bare circle/arc refs; see solver).
    if ctype == "distance" and c.get("cond") is not None:
        cnd = c["cond"]
        if not (isinstance(cnd, list) and len(cnd) == 2
                and all(x in ("center", "min", "max") for x in cnd)):
            errs.append(f"{where}.cond: must be a 2-list of 'center'|'min'|'max'")
    if ctype == "distance" and "axis" in c and c["axis"] is not None:
        if c["axis"] not in ("h", "v"):
            errs.append(f"{where}.axis: must be 'h', 'v', or null")
        else:
            # axis is point-point only — a point-line distance is already directional.
            for h in refs[:2]:
                base = h.split(".")[0] if isinstance(h, str) else None
                ent = entities_by_id.get(base) if base else None
                if ent is not None and ent.get("type") == "line" and "." not in (h or ""):
                    errs.append(f"{where}.axis: not allowed for a point-line distance")
    return errs


def _validate_sketch_params(params: dict, where: str, globals_names: set[str]) -> list[str]:
    errs: list[str] = []
    entities = params.get("entities")
    constraints = params.get("constraints")
    if not isinstance(entities, list):
        return [f"{where}.entities: must be a list"]
    if not isinstance(constraints, list):
        return [f"{where}.constraints: must be a list"]
    by_id: dict[str, dict] = {}
    for i, e in enumerate(entities):
        errs += validate_entity(e, f"{where}.entities[{i}]")
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            if e["id"] in by_id:
                errs.append(f"{where}.entities[{i}].id: duplicate '{e['id']}'")
            by_id[e["id"]] = e
    seen_c: set[str] = set()
    for i, c in enumerate(constraints):
        errs += validate_constraint(c, f"{where}.constraints[{i}]", by_id, globals_names)
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            if c["id"] in seen_c:
                errs.append(f"{where}.constraints[{i}].id: duplicate '{c['id']}'")
            seen_c.add(c["id"])
    return errs


# --------------------------------------------------------------------------- features

def validate_feature(
    f: Any,
    where: str,
    earlier_ids: set[str],
    globals_names: set[str],
    component_ids: set[str] | None = None,
) -> list[str]:
    errs: list[str] = []
    if not isinstance(f, dict):
        return [f"{where}: feature must be an object"]
    ftype = f.get("type")
    if ftype not in FEATURE_TYPES:
        return [f"{where}.type: '{ftype}' is not a feature type"]
    fid = f.get("id")
    if not isinstance(fid, str) or not FEATURE_ID_RE.match(fid):
        errs.append(f"{where}.id: must match f<N>")
    if not isinstance(f.get("name"), str) or not f["name"]:
        errs.append(f"{where}.name: required")
    if not isinstance(f.get("suppressed"), bool):
        errs.append(f"{where}.suppressed: required bool")
    spec = FEATURE_TYPES[ftype]
    params = f.get("params")
    refs = f.get("refs")
    if not isinstance(params, dict):
        errs.append(f"{where}.params: must be an object")
        params = {}
    if not isinstance(refs, dict):
        errs.append(f"{where}.refs: must be an object")
        refs = {}

    for key in spec["params"]:
        if key not in params:
            errs.append(f"{where}.params.{key}: required for {ftype}")
    # Dimension-shaped params get dim validation; ints/bools/strings pass structurally.
    for key, v in params.items():
        if isinstance(v, dict) and ("value" in v or "expr" in v):
            errs += validate_dim(v, f"{where}.params.{key}", globals_names)

    if ftype == "sketch":
        errs += _validate_sketch_params(params, f"{where}.params", globals_names)

    if ftype == "fillet":
        # Conditional requiredness: full_round needs middle_face; else edges+radius.
        if params.get("full_round"):
            if not refs.get("middle_face"):
                errs.append(f"{where}.refs.middle_face: required for full_round fillet")
        else:
            if "radius" not in params:
                errs.append(f"{where}.params.radius: required for fillet")
            if not refs.get("edges"):
                errs.append(f"{where}.refs.edges: required for fillet")

    known_slots = spec["refs"]
    for slot, (form, required) in known_slots.items():
        if slot not in refs or refs[slot] is None:
            if required:
                errs.append(f"{where}.refs.{slot}: required for {ftype}")
            continue
        v = refs[slot]
        loc = f"{where}.refs.{slot}"
        if form == _SEL:
            errs += validate_selector(v, loc)
        elif form == _SEL_OR_DATUM:
            errs += _validate_sel_or_datum(v, loc)
        elif form == _SEL_LIST:
            if not isinstance(v, list) or not v:
                errs.append(f"{loc}: must be a non-empty list")
            else:
                for i, s in enumerate(v):
                    errs += validate_selector(s, f"{loc}[{i}]")
        elif form == _FEAT_LIST:
            if not isinstance(v, list) or not v:
                errs.append(f"{loc}: must be a non-empty list of feature ids")
            else:
                for i, s in enumerate(v):
                    if not isinstance(s, str) or not FEATURE_ID_RE.match(s):
                        errs.append(f"{loc}[{i}]: not a feature id")
                    elif s not in earlier_ids:
                        errs.append(f"{loc}[{i}]: '{s}' does not precede this feature")
        elif form == _UV_LIST:
            if not isinstance(v, list) or not v:
                errs.append(f"{loc}: must be a non-empty list of [u,v] points")
            else:
                for i, pt in enumerate(v):
                    if not _is_vec(pt, 2):
                        errs.append(f"{loc}[{i}]: must be [u,v] (two numbers)")
        elif form == _ENTITY_ID_LIST:
            if not isinstance(v, list) or not v:
                errs.append(f"{loc}: must be a non-empty list of sketch entity ids")
            else:
                for i, s in enumerate(v):
                    if not isinstance(s, str) or not re.match(r"^e\d+$", s):
                        errs.append(f"{loc}[{i}]: '{s}' is not a sketch entity id (e<N>)")
        elif form == _COMP_LIST:
            comp = component_ids or set()
            if not isinstance(v, list) or not v:
                errs.append(f"{loc}: must be a non-empty list of component ids")
            else:
                for i, s in enumerate(v):
                    if s not in comp:
                        errs.append(f"{loc}[{i}]: unknown component '{s}'")
    for slot in refs:
        if slot not in known_slots:
            errs.append(f"{where}.refs.{slot}: unknown ref slot for {ftype}")
    return errs


# --------------------------------------------------------------------------- documents

def _validate_equations(doc: dict, where: str) -> tuple[list[str], set[str]]:
    errs: list[str] = []
    eq = doc.get("equations", {"globals": []})
    if not isinstance(eq, dict) or not isinstance(eq.get("globals"), list):
        return [f"{where}.equations: must be {{globals: [...]}}"], set()
    names: set[str] = set()
    for i, g in enumerate(eq["globals"]):
        loc = f"{where}.equations.globals[{i}]"
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not isinstance(g.get("expr"), str):
            errs.append(f"{loc}: must be {{name, expr}}")
            continue
        if not re.match(r"^[A-Za-z_]\w*$", g["name"]):
            errs.append(f"{loc}.name: invalid identifier '{g['name']}'")
        if g["name"] in names:
            errs.append(f"{loc}.name: duplicate '{g['name']}'")
        names.add(g["name"])
        e = validate_expr(g["expr"])
        if e:
            errs.append(f"{loc}.expr: {e}")
    return errs, names


def _validate_appearances(doc: dict) -> list[str]:
    """Optional document-level body appearances (C26 + C32-A material).

    Two value forms are accepted (both round-trip):
      * legacy bare ``[r,g,b]`` in 0..1 (color only — dielectric default material);
      * object ``{"color": [r,g,b], "metallic": 0..1, "roughness": 0..1}`` (RealView material).
        ``color`` is required; ``metallic``/``roughness`` are optional 0..1 floats.

    The body_key is the body's owner-feature id (parts) or component id (assemblies) — whatever the
    rebuild reports as the body's stable owner. A missing key just means "default color".
    """
    ap = doc.get("appearances")
    if ap is None:
        return []
    if not isinstance(ap, dict):
        return ["appearances: must be an object {body_key: [r,g,b] | {color,...}}"]
    errs: list[str] = []
    for key, val in ap.items():
        if not isinstance(key, str) or not key:
            errs.append(f"appearances: key '{key}' must be a non-empty string")
        if isinstance(val, dict):
            col = val.get("color")
            if not _is_vec(col, 3) or any(not (0.0 <= float(c) <= 1.0) for c in col):
                errs.append(f"appearances.{key}.color: must be [r,g,b] with each component in 0..1")
            for mk in ("metallic", "roughness"):
                if mk in val and (not _is_num(val[mk]) or not (0.0 <= float(val[mk]) <= 1.0)):
                    errs.append(f"appearances.{key}.{mk}: must be a number in 0..1")
        elif not _is_vec(val, 3) or any(not (0.0 <= float(c) <= 1.0) for c in val):
            errs.append(f"appearances.{key}: must be [r,g,b] (0..1) or {{color, metallic, roughness}}")
    return errs


def _validate_rollback(doc: dict, feature_ids: set[str]) -> list[str]:
    """Optional doc-level ``rollback_after``: null (bar at end) or an existing feature id (the bar
    sits AFTER that feature). C35-overhaul §6."""
    rb = doc.get("rollback_after")
    if rb is None:
        return []
    if not isinstance(rb, str):
        return ["rollback_after: must be a feature id string or null"]
    if rb not in feature_ids:
        return [f"rollback_after: '{rb}' is not an existing feature id"]
    return []


def _validate_configurations(doc: dict, features_by_id: dict[str, dict]) -> list[str]:
    """Optional doc-level ``configurations`` + ``active_configuration`` (CONFIG version bump).

    A part/assembly may carry NAMED configurations — variants of one document driven by per-config
    feature-param overrides and/or feature suppression. The shape::

        "configurations": [
            {"name": str,
             "param_overrides": {"<feature_id>.<param_key>": <value | {value, expr}>},
             "suppressions": {"<feature_id>": bool}}
        ],
        "active_configuration": "<name>"

    ABSENT ``configurations`` => exactly today's behavior (one implicit "Default" config). The
    validator checks: names are non-empty + unique; ``active_configuration`` (when present) names a
    real config; each override key is ``fid.param`` referencing an existing feature whose params
    carry that key; each suppression key references an existing feature. ``param_overrides`` values
    that look like dims (``{value, expr}``) are dim-validated. Errors are clean (no exception)."""
    configs = doc.get("configurations")
    if configs is None:
        # active_configuration is meaningless without a configurations list; flag a stray pointer.
        ac = doc.get("active_configuration")
        if ac is not None and not isinstance(ac, str):
            return ["active_configuration: must be a configuration name string or null"]
        return []
    if not isinstance(configs, list):
        return ["configurations: must be a list"]

    errs: list[str] = []
    names: set[str] = set()
    for i, cfg in enumerate(configs):
        loc = f"configurations[{i}]"
        if not isinstance(cfg, dict):
            errs.append(f"{loc}: must be an object")
            continue
        name = cfg.get("name")
        if not isinstance(name, str) or not name:
            errs.append(f"{loc}.name: required non-empty string")
        elif name in names:
            errs.append(f"{loc}.name: duplicate '{name}'")
        else:
            names.add(name)

        overrides = cfg.get("param_overrides", {})
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            errs.append(f"{loc}.param_overrides: must be an object")
        else:
            for key, val in overrides.items():
                kloc = f"{loc}.param_overrides.{key}"
                if not isinstance(key, str) or "." not in key:
                    errs.append(f"{kloc}: key must be '<feature_id>.<param_key>'")
                    continue
                fid, _, pkey = key.partition(".")
                feat = features_by_id.get(fid)
                if feat is None:
                    errs.append(f"{kloc}: feature '{fid}' does not exist")
                    continue
                params = feat.get("params")
                if isinstance(params, dict) and pkey not in params:
                    errs.append(f"{kloc}: feature '{fid}' has no param '{pkey}'")
                # Dim-shaped override values get the same structural check as stored dims.
                if isinstance(val, dict) and ("value" in val or "expr" in val):
                    errs += validate_dim(val, kloc, set())

        sup = cfg.get("suppressions", {})
        if sup is None:
            sup = {}
        if not isinstance(sup, dict):
            errs.append(f"{loc}.suppressions: must be an object")
        else:
            for fid, flag in sup.items():
                sloc = f"{loc}.suppressions.{fid}"
                if fid not in features_by_id:
                    errs.append(f"{sloc}: feature '{fid}' does not exist")
                if not isinstance(flag, bool):
                    errs.append(f"{sloc}: must be a bool")

    ac = doc.get("active_configuration")
    if ac is not None:
        if not isinstance(ac, str):
            errs.append("active_configuration: must be a configuration name string or null")
        elif ac not in names:
            errs.append(f"active_configuration: '{ac}' is not a defined configuration")
    return errs


def _validate_common(doc: dict, schema: str) -> list[str]:
    errs: list[str] = []
    if doc.get("schema") != schema:
        errs.append(f"schema: expected '{schema}', got '{doc.get('schema')}'")
    if not isinstance(doc.get("id"), str) or not doc["id"]:
        errs.append("id: required string")
    if not isinstance(doc.get("name"), str) or not doc["name"]:
        errs.append("name: required string")
    units = doc.get("units")
    if not isinstance(units, dict) or units.get("system") not in UNIT_SYSTEMS:
        errs.append(f"units.system: must be one of {sorted(UNIT_SYSTEMS)}")
    errs += _validate_appearances(doc)
    return errs


def validate_part(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    errs = _validate_common(doc, SCHEMA_PART)
    mat = doc.get("material")
    if mat is not None and not (isinstance(mat, dict) and isinstance(mat.get("key"), str)):
        errs.append("material: must be null or {key}")
    eq_errs, globals_names = _validate_equations(doc, "")
    errs += eq_errs
    feats = doc.get("features")
    if not isinstance(feats, list):
        return errs + ["features: must be a list"]
    earlier: set[str] = set(RESERVED_DATUMS)
    seen: set[str] = set()
    feat_ids: set[str] = set()
    feats_by_id: dict[str, dict] = {}
    for i, f in enumerate(feats):
        fid = f.get("id") if isinstance(f, dict) else None
        loc = f"features[{i}]({fid})"
        errs += validate_feature(f, loc, earlier, globals_names)
        if isinstance(fid, str):
            if fid in seen:
                errs.append(f"{loc}.id: duplicate")
            seen.add(fid)
            earlier.add(fid)
            feat_ids.add(fid)
            if isinstance(f, dict):
                feats_by_id[fid] = f
    errs += _validate_rollback(doc, feat_ids)
    errs += _validate_configurations(doc, feats_by_id)
    return errs


def validate_assembly(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    errs = _validate_common(doc, SCHEMA_ASM)
    eq_errs, globals_names = _validate_equations(doc, "")
    errs += eq_errs

    comps = doc.get("components")
    comp_ids: set[str] = set()
    if not isinstance(comps, list):
        errs.append("components: must be a list")
        comps = []
    for i, k in enumerate(comps):
        loc = f"components[{i}]"
        if not isinstance(k, dict):
            errs.append(f"{loc}: must be an object")
            continue
        kid = k.get("id")
        if not isinstance(kid, str) or not re.match(r"^k\d+$", kid):
            errs.append(f"{loc}.id: must match k<N>")
        elif kid in comp_ids:
            errs.append(f"{loc}.id: duplicate '{kid}'")
        else:
            comp_ids.add(kid)
        if not isinstance(k.get("name"), str) or not k["name"]:
            errs.append(f"{loc}.name: required")
        src = k.get("source")
        if not (isinstance(src, dict) and (isinstance(src.get("path"), str) or isinstance(src.get("embedded"), str))):
            errs.append(f"{loc}.source: must be {{path}} or {{embedded}}")
        t = k.get("transform")
        if not _is_vec(t, 16):
            errs.append(f"{loc}.transform: must be 16 floats (row-major)")
        for flag in ("fixed", "suppressed"):
            if not isinstance(k.get(flag), bool):
                errs.append(f"{loc}.{flag}: required bool")

    mates = doc.get("mates")
    if not isinstance(mates, list):
        errs.append("mates: must be a list")
        mates = []
    seen_m: set[str] = set()
    for i, m in enumerate(mates):
        loc = f"mates[{i}]"
        if not isinstance(m, dict):
            errs.append(f"{loc}: must be an object")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not re.match(r"^m\d+$", mid):
            errs.append(f"{loc}.id: must match m<N>")
        elif mid in seen_m:
            errs.append(f"{loc}.id: duplicate '{mid}'")
        else:
            seen_m.add(mid)
        mtype = m.get("type")
        if mtype not in MATE_TYPES:
            errs.append(f"{loc}.type: '{mtype}' not a mate type")
        if m.get("align") not in ("aligned", "anti"):
            errs.append(f"{loc}.align: must be 'aligned' or 'anti'")
        if not isinstance(m.get("suppressed"), bool):
            errs.append(f"{loc}.suppressed: required bool")
        refs = m.get("refs")
        if not isinstance(refs, list) or len(refs) != 2:
            errs.append(f"{loc}.refs: must be exactly 2 entries")
            refs = []
        for j, r in enumerate(refs):
            rloc = f"{loc}.refs[{j}]"
            if not isinstance(r, dict):
                errs.append(f"{rloc}: must be {{component, selector}}")
                continue
            if r.get("component") not in comp_ids:
                errs.append(f"{rloc}.component: unknown '{r.get('component')}'")
            errs += validate_selector(r.get("selector"), f"{rloc}.selector")
        if mtype in MATE_VALUE_TYPES:
            if m.get("value") is None:
                errs.append(f"{loc}.value: required for {mtype}")
            else:
                errs += validate_dim(m["value"], f"{loc}.value", globals_names)
        elif m.get("value") is not None:
            errs.append(f"{loc}.value: {mtype} must not carry a value")

    feats = doc.get("features", [])
    if not isinstance(feats, list):
        errs.append("features: must be a list")
        feats = []
    earlier: set[str] = set(RESERVED_DATUMS)
    asm_feat_ids: set[str] = set()
    asm_feats_by_id: dict[str, dict] = {}
    for i, f in enumerate(feats):
        fid = f.get("id") if isinstance(f, dict) else None
        errs += validate_feature(
            f, f"features[{i}]({fid})", earlier, globals_names, component_ids=comp_ids
        )
        if isinstance(fid, str):
            earlier.add(fid)
            asm_feat_ids.add(fid)
            if isinstance(f, dict):
                asm_feats_by_id[fid] = f
    errs += _validate_rollback(doc, asm_feat_ids)
    errs += _validate_configurations(doc, asm_feats_by_id)

    explode = doc.get("explode", [])
    if not isinstance(explode, list):
        errs.append("explode: must be a list")
        explode = []
    for i, s in enumerate(explode):
        loc = f"explode[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{loc}: must be an object")
            continue
        if s.get("component") not in comp_ids:
            errs.append(f"{loc}.component: unknown '{s.get('component')}'")
        if not _is_vec(s.get("vector"), 3):
            errs.append(f"{loc}.vector: must be [x,y,z]")
        if not _is_num(s.get("distance")):
            errs.append(f"{loc}.distance: must be a number")
    return errs


# --------------------------------------------------------------------------- drawing (W12)


def _validate_view(v: Any, where: str) -> list[str]:
    """A drawing View: {id, type, orientation, scale, position:[x,y], show_hidden}.

    Optional reserved fields (W13): ``annotations`` (a list) and ``label`` (a string). W15 adds two
    derived view types — ``section`` (a cut, oriented "section", carrying ``parent`` + a world
    ``plane`` {normal, point} OR a 2D ``cut_line`` [[x,y],[x,y]] in the parent's sheet mm) and
    ``detail`` (a magnified crop, oriented "detail", carrying ``parent`` + ``center`` [x,y] +
    ``radius``). Geometry — whether the projection actually yields edges — is the projector's
    concern (drawing.py), not the structural validator's.
    """
    errs: list[str] = []
    if not isinstance(v, dict):
        return [f"{where}: view must be an object"]
    if not isinstance(v.get("id"), str) or not re.match(r"^v\d+$", v["id"]):
        errs.append(f"{where}.id: must match v<N>")
    vtype = v.get("type")
    if vtype not in VIEW_TYPES:
        errs.append(f"{where}.type: '{vtype}' not in {sorted(VIEW_TYPES)}")
    if v.get("orientation") not in VIEW_ORIENTATIONS:
        errs.append(f"{where}.orientation: '{v.get('orientation')}' not in {sorted(VIEW_ORIENTATIONS)}")
    # An iso view must be oriented "iso"; an ortho (base/projected) view must not.
    if vtype == "iso" and v.get("orientation") != "iso":
        errs.append(f"{where}: an 'iso' view requires orientation 'iso'")
    if vtype in ("base", "projected") and v.get("orientation") == "iso":
        errs.append(f"{where}: orientation 'iso' is only valid for an 'iso' view")
    # A section/detail view is oriented by its own keyword and references a parent view id.
    if vtype == "section" and v.get("orientation") != "section":
        errs.append(f"{where}: a 'section' view requires orientation 'section'")
    if vtype == "detail" and v.get("orientation") != "detail":
        errs.append(f"{where}: a 'detail' view requires orientation 'detail'")
    if vtype in ("section", "detail"):
        if not (isinstance(v.get("parent"), str) and re.match(r"^v\d+$", v["parent"])):
            errs.append(f"{where}.parent: {vtype} view needs a parent view id (v<N>)")
    if vtype == "section":
        plane = v.get("plane")
        cut_line = v.get("cut_line")
        has_plane = (isinstance(plane, dict) and _is_vec(plane.get("normal"), 3)
                     and _is_vec(plane.get("point"), 3))
        has_line = (isinstance(cut_line, list) and len(cut_line) == 2
                    and all(_is_vec(q, 2) for q in cut_line))
        if not (has_plane or has_line):
            errs.append(f"{where}: section needs a 'plane' {{normal,point}} or a 2D 'cut_line'")
    if vtype == "detail":
        if not _is_vec(v.get("center"), 2):
            errs.append(f"{where}.center: detail view needs [x,y] (parent sheet mm)")
        if not _is_num(v.get("radius")) or float(v.get("radius", 0)) <= 0:
            errs.append(f"{where}.radius: detail view needs a positive radius")
    if not _is_num(v.get("scale")) or float(v.get("scale", 0)) <= 0:
        errs.append(f"{where}.scale: must be a positive number")
    if not _is_vec(v.get("position"), 2):
        errs.append(f"{where}.position: must be [x,y] (sheet mm)")
    if not isinstance(v.get("show_hidden"), bool):
        errs.append(f"{where}.show_hidden: required bool")
    if "annotations" in v:
        if not isinstance(v["annotations"], list):
            errs.append(f"{where}.annotations: must be a list (W13 dimensions/notes)")
        else:
            for j, a in enumerate(v["annotations"]):
                errs += _validate_annotation(a, f"{where}.annotations[{j}]")
    if "label" in v and not isinstance(v["label"], (str, type(None))):
        errs.append(f"{where}.label: must be a string or null")
    return errs


ANNOTATION_TYPES = {"linear_dim", "aligned_dim", "note"}


def _validate_annotation(a: Any, where: str) -> list[str]:
    """A drawing annotation: {id, type, a:[x,y], b:[x,y], pos:[x,y], value, text} (W13).

    ``a``/``b`` are sheet-mm endpoints (point picks); ``pos`` is the text/leader anchor; ``value`` is
    the measured MODEL length; ``text`` is the displayed string. A ``note`` may omit a/b (value 0).
    """
    errs: list[str] = []
    if not isinstance(a, dict):
        return [f"{where}: annotation must be an object"]
    if not isinstance(a.get("id"), str) or not re.match(r"^a\d+$", a["id"]):
        errs.append(f"{where}.id: must match a<N>")
    atype = a.get("type")
    if atype not in ANNOTATION_TYPES:
        errs.append(f"{where}.type: '{atype}' not in {sorted(ANNOTATION_TYPES)}")
    is_note = atype == "note"
    for key in ("a", "b"):
        if key in a and a[key] is not None and not _is_vec(a[key], 2):
            errs.append(f"{where}.{key}: must be [x,y] (sheet mm)")
        elif not is_note and a.get(key) is None:
            errs.append(f"{where}.{key}: required for a dimension")
    if "pos" in a and a["pos"] is not None and not _is_vec(a["pos"], 2):
        errs.append(f"{where}.pos: must be [x,y] (sheet mm) or null")
    if "value" in a and a["value"] is not None and not _is_num(a["value"]):
        errs.append(f"{where}.value: must be a number")
    if "text" in a and a["text"] is not None and not isinstance(a["text"], str):
        errs.append(f"{where}.text: must be a string or null")
    return errs


def validate_drawing(doc: Any) -> list[str]:
    """Validate a ``aicad.drawing/1`` document (W12).

    A drawing is a 2D detailing document that PROJECTS a source part/assembly into orthographic +
    isometric views. It carries no features/equations/material — only a ``source`` reference and a
    list of sheets, each holding views.
    """
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    errs = _validate_common(doc, SCHEMA_DRAWING)

    # source: an open doc id (string) or an embedded source spec {path} / {doc_id}.
    src = doc.get("source")
    if isinstance(src, str):
        if not src:
            errs.append("source: must be a non-empty doc id / path string")
    elif isinstance(src, dict):
        if not (isinstance(src.get("path"), str) or isinstance(src.get("doc_id"), str)):
            errs.append("source: object form must carry {path} or {doc_id}")
    else:
        errs.append("source: must be a source doc id string or {path}|{doc_id}")

    sheets = doc.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        errs.append("sheets: must be a non-empty list")
        sheets = []
    sheet_ids: set[str] = set()
    view_ids: set[str] = set()
    for i, sh in enumerate(sheets):
        loc = f"sheets[{i}]"
        if not isinstance(sh, dict):
            errs.append(f"{loc}: must be an object")
            continue
        sid = sh.get("id")
        if not isinstance(sid, str) or not re.match(r"^s\d+$", sid):
            errs.append(f"{loc}.id: must match s<N>")
        elif sid in sheet_ids:
            errs.append(f"{loc}.id: duplicate '{sid}'")
        else:
            sheet_ids.add(sid)
        if sh.get("size") not in SHEET_SIZES:
            errs.append(f"{loc}.size: '{sh.get('size')}' not in {sorted(SHEET_SIZES)}")
        views = sh.get("views")
        if not isinstance(views, list):
            errs.append(f"{loc}.views: must be a list")
            views = []
        for j, v in enumerate(views):
            errs += _validate_view(v, f"{loc}.views[{j}]")
            if isinstance(v, dict) and isinstance(v.get("id"), str):
                if v["id"] in view_ids:
                    errs.append(f"{loc}.views[{j}].id: duplicate '{v['id']}' (view ids are doc-unique)")
                view_ids.add(v["id"])
    return errs


def validate_document(doc: Any) -> list[str]:
    """Dispatch on the schema string."""
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    schema = doc.get("schema")
    if schema == SCHEMA_PART:
        return validate_part(doc)
    if schema == SCHEMA_ASM:
        return validate_assembly(doc)
    if schema == SCHEMA_DRAWING:
        return validate_drawing(doc)
    return [f"schema: unknown '{schema}'"]
