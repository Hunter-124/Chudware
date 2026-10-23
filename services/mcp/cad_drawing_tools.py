"""CAD 2D-drawing MCP tools (split out of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from pydantic import Field
from typing import Annotated, Any, Optional
import os

from .cad_common import (
    _doc,
    _dumps,
    _err,
    _feature_owned_matches,
    _resolve_drawing_view_id,
    _tmp,
    cb,
    coerce_json_or_repr,
    require_list,
    require_mapping,
    sh,
)

import re as _re

# ---------------------------------------------------------------- section discriminators
#
# SILENT-WRONG-OUTPUT guard. `add_section_view(axis=…)` used to read
#     cut = horizontal_line if axis == "horizontal" else vertical_line
# so EVERY value that was not the exact string "horizontal" produced a VERTICAL cut —
# axis="vert", axis="y", axis="diagonal", axis="x" all cut vertically, and a section on the
# wrong plane looks completely normal on the sheet. Same class one slot over: `mode` was
# forwarded into `opts` untouched, and core/doc/drawing_session.cpp:926 only honours it when
# it is literally "full" or "half" (`if (mode == "full" || mode == "half") view["mode"] = mode`)
# — anything else is dropped and the section silently comes back FULL.
#
# Vocabulary sources, not guesses:
#   axis — the cut is a 2D cutting LINE laid across the parent view's sheet-mm bbox
#          (add_section_view below, and the engine's own auto-section at
#          core/doc/drawing_session.cpp:2049-2050). "horizontal" builds
#          [[x0-pad, cy], [x1+pad, cy]] — a line at constant sheet y spanning x, i.e. the
#          cutting LINE is horizontal. So this codebase uses the drafting convention
#          (horizontal = horizontal cutting line), not "normal to X".
#   mode — core/doc/drawing_session.cpp:926, exactly {"full", "half"}.


def _canon_word(v: Any) -> str:
    """lower/strip and fold spaces + hyphens onto '_' so 'Half Section' == 'half_section'."""
    return _re.sub(r"[\s\-]+", "_", str(v if v is not None else "").strip().lower())


#: Accepted spellings of the two REAL cuts. A model that writes "vert" plainly means the
#: vertical cut, so mapping it is a fix, not a guess; anything absent here is rejected.
_SECTION_AXIS_WORDS = {
    "horizontal": "horizontal", "horizontally": "horizontal", "horiz": "horizontal",
    "hor": "horizontal", "h": "horizontal", "horizontal_cut": "horizontal",
    "vertical": "vertical", "vertically": "vertical", "vert": "vertical",
    "v": "vertical", "vertical_cut": "vertical",
}

#: Bare axis LETTERS are deliberately NOT in the table above — see _canon_section_axis.
#: Matches "x", "-y", "+z", "x_axis" (whitespace/hyphens are already folded onto "_").
_SECTION_AXIS_LETTER_RE = _re.compile(r"[+\-_]*([xyz])(_axis)?")

#: core/doc/drawing_session.cpp:926 honours exactly these two.
_SECTION_MODE_WORDS = {
    "full": "full", "full_section": "full", "fullsection": "full", "whole": "full",
    "half": "half", "half_section": "half", "halfsection": "half",
}


def _canon_section_axis(axis: Any) -> tuple[Optional[str], Optional[dict]]:
    """('horizontal'|'vertical', None) or (None, an error dict naming the valid set).

    x/y/z are REJECTED rather than mapped, and that is the deliberate call. A letter has two
    equally common readings — "the cutting line runs along X" (a horizontal cut) and "the
    section plane's normal is X" (a vertical cut) — and they give OPPOSITE cuts. Worse, the
    cut line lives in the PARENT view's sheet frame, whose axes are not model axes at all:
    sheet-y is model Y on a top view but model Z on a front view, so even a settled reading
    of the letter would not settle which model plane gets cut. An ambiguous mapping that is
    silently wrong half the time is exactly the defect this guard exists to remove, so the
    letter earns a loud error that names the two unambiguous words instead.
    """
    w = _canon_word(axis)
    hit = _SECTION_AXIS_WORDS.get(w)
    if hit:
        return hit, None
    if _SECTION_AXIS_LETTER_RE.fullmatch(w):
        return None, {
            "ok": False,
            "error": f"ambiguous section axis {axis!r} — use 'horizontal' or 'vertical'",
            "hint": "An axis LETTER does not say which cut you mean: 'x' reads equally as a "
                    "cutting line ALONG x (a horizontal section) or a plane NORMAL to x (a "
                    "vertical one), and the cut line is in the parent view's sheet frame, not "
                    "model space. Name the cut line: axis='horizontal' (cut line across the "
                    "view, left-to-right) or axis='vertical' (cut line down the view).",
            "valid_axis": ["horizontal", "vertical"],
        }
    return None, {
        "ok": False,
        "error": f"unknown section axis {axis!r}",
        "hint": "Use one of: 'horizontal' (a horizontal cutting line through the parent "
                "view's centre) or 'vertical' (a vertical one). 'horiz'/'h' and 'vert'/'v' "
                "are accepted spellings.",
        "valid_axis": ["horizontal", "vertical"],
    }


def _canon_section_mode(mode: Any) -> tuple[Optional[str], Optional[dict]]:
    """('full'|'half', None) or (None, an error dict naming the valid set).

    'partial' is NOT mapped onto 'half': a partial/broken-out section is a different drafting
    construct, so accepting it would be the same silent-wrong-answer bug wearing a synonym.
    """
    w = _canon_word(mode)
    hit = _SECTION_MODE_WORDS.get(w)
    if hit:
        return hit, None
    return None, {
        "ok": False,
        "error": f"unknown section mode {mode!r}",
        "hint": "Use one of: 'full' (the whole part is sectioned) or 'half' (half sectioned, "
                "half left as an external view). The engine honours no other value — it "
                "would silently fall back to a full section.",
        "valid_mode": ["full", "half"],
    }


# ---------------------------------------------------------------- dimension type vocabulary
#
# LIVE-DIALECT gap (2026-07-29): add_dimension(dtype="diameter") was hard-rejected with
#   {"ok":false,"error":"annotation type 'diameter' not recognized"}
# even though the engine fully implements the dimension — core/doc/drawing_session.cpp:1106-1108
# accepts {linear_dim, aligned_dim, note} + {radial_dim, diameter_dim, angular_dim,
# center_mark, center_line}, and :1034-1060 computes the diameter (2*r) for `diameter_dim`.
# The model simply wrote the drafting WORD instead of the engine's `_dim` suffix, and a
# diameter callout on a hole is the single most common dimension on a sheet.
#
# The vocabulary below is read off that engine set — nothing here maps onto a type the engine
# does not implement. `radius` and `diameter` are deliberately kept APART: the engine returns
# r_model for radial_dim and 2*r_model for diameter_dim (:1048-1049), so collapsing the two
# would silently report a value off by a factor of two on a drawing that looks correct.

#: The engine's real accepted set (core/doc/drawing_session.cpp:1106-1108), in the order we
#: teach it back to the caller.
_DIM_TYPES = ("linear_dim", "aligned_dim", "radial_dim", "diameter_dim", "angular_dim",
              "center_mark", "center_line", "note")

#: Suffix-less / abbreviated / symbol spellings -> the engine type. Keys are already folded by
#: _canon_word (lowercased, whitespace+hyphens -> "_"), so "Center Mark" hits "center_mark".
_DIM_TYPE_WORDS = {
    # a straight distance between two picks
    "linear": "linear_dim", "length": "linear_dim", "distance": "linear_dim",
    "linear_dimension": "linear_dim",
    # the same, measured along the pick-to-pick direction rather than the larger axis
    "aligned": "aligned_dim", "aligned_dimension": "aligned_dim",
    # R… — reports the RADIUS
    "radius": "radial_dim", "radial": "radial_dim", "rad": "radial_dim",
    "radius_dimension": "radial_dim", "radial_dimension": "radial_dim",
    # ⌀… — reports the DIAMETER (2*r). Never folded together with the radius words above.
    "diameter": "diameter_dim", "diam": "diameter_dim", "dia": "diameter_dim",
    "\u00f8": "diameter_dim", "\u2300": "diameter_dim", "\u2205": "diameter_dim",
    "diameter_dimension": "diameter_dim",
    # included angle between two lines
    "angular": "angular_dim", "angle": "angular_dim", "angular_dimension": "angular_dim",
    "angle_dimension": "angular_dim",
    # the crosshair at a circle's centre (no value)
    "centermark": "center_mark", "centre_mark": "center_mark", "centremark": "center_mark",
    "center_marker": "center_mark",
    "centerline": "center_line", "centre_line": "center_line", "centreline": "center_line",
}
_DIM_TYPE_WORDS.update({t: t for t in _DIM_TYPES})


def _canon_dim_type(dtype: Any) -> tuple[Optional[str], Optional[dict]]:
    """(an engine dimension type, None) or (None, an error dict listing the real vocabulary).

    Maps the drafting words a model writes onto the engine's `*_dim` spellings. An unknown
    value STILL fails — now naming every type the engine actually implements, and spelling out
    that radius and diameter are separate because they differ by a factor of two.
    """
    w = _canon_word(dtype)
    hit = _DIM_TYPE_WORDS.get(w)
    if hit:
        return hit, None
    return None, {
        "ok": False,
        "code": "invalid_op",
        "error": f"unknown dimension type {dtype!r}; the drawing engine implements: "
                 + ", ".join(_DIM_TYPES),
        "hint": "Plain drafting words are accepted and mapped: 'diameter'/'diam'/'dia'/'\u2300' "
                "-> diameter_dim (reports 2*r), 'radius'/'radial'/'rad' -> radial_dim (reports "
                "r) — these two are NOT interchangeable; 'linear'/'length'/'distance' -> "
                "linear_dim, 'aligned' -> aligned_dim, 'angular'/'angle' -> angular_dim, "
                "'center mark' -> center_mark, 'center line' -> center_line.",
        "valid_dtype": list(_DIM_TYPES),
    }

# ---------------------------------------------------------------- argument-name dialect
#
# LIVE-DIALECT gaps (2026-07-29, claude-sonnet-4-5, general lane). Four drawing calls died on
# an argument NAME and never reached the engine:
#   make_drawing(source_doc="doc-cpp00000008", sheet_size="A3", title=…)   -> unknown_argument
#   make_drawing(doc_id=…, template="A4", model_items="basic_dims")        -> unknown_argument
#   export_drawing(doc_id=…, format="pdf", filename="90x50x12_plate_drawing")
#   set_titleblock(doc_id=…, part_name="40mm Cube", revision="A", units="mm", scale="1:1")
#
# `source_doc_id`, `sheet_size` and `out_path` ALREADY reconcile, through the shared affix rule
# in services/agent/arg_reconciliation.py::resolve_unknown (one name a prefix/suffix of the
# other). The shorter spellings do not: "sourcedoc" is neither a prefix nor a suffix of
# "docid", and "filename" is neither of "path". Those need an explicit, tool-scoped table.
#
# The aliases are declared as REAL PARAMETERS (`source_doc`, `template`, `filename`) rather
# than swept up by a **kwargs bag, deliberately. A tool that declares **kwargs turns the
# shared reconciler off for itself — both dispatch paths read VAR_KEYWORD as "accepts
# anything" (agent_turn._signature_spec:4299, server._arg_meta:705-713) — and on the external
# MCP path FastMCP's generated arg model then DROPS the unknown key silently instead of
# refusing it (measured: set_titleblock's flattened-kwarg dialect only lands on the agent
# path). A real parameter works identically on both paths, appears in the tool schema so a
# model can read it instead of guessing, and leaves the affix rule intact for the longer
# spellings (`source_doc_id`, `sheet_size`, `out_path`) that already reconcile today.
#
# Nothing here maps across a meaning boundary. `part_name` is NOT `part_number` (a name and an
# identifier are different title-block cells), `units` is NOT a title-block field at all, and a
# `template` value that is not a paper size is refused rather than filed as one.

from services.agent.arg_reconciliation import norm_arg as _norm_arg  # noqa: E402 — leaf module


def _fold_alias_param(canon_name: str, canon: Any, alias_name: str,
                      alias: Any) -> tuple[Any, Optional[str], Optional[dict]]:
    """(the value to use, an interpretation note, None) — or (…, …, a refusal).

    Both spellings supplied and DISAGREEING is the one case we must not resolve silently:
    picking a winner draws the wrong document (or writes the wrong file) while reporting
    success. Identical values are harmless and fold quietly.
    """
    if alias is None:
        return canon, None, None
    if canon is None:
        return alias, f"interpreted {alias_name!r} as {canon_name!r}", None
    if canon == alias:
        return canon, None, None
    return canon, None, {
        "ok": False,
        "error": f"{canon_name}={canon!r} and {alias_name}={alias!r} are two spellings of the "
                 f"same argument but disagree. {alias_name} is accepted only as a synonym of "
                 f"{canon_name} — pass one of them, not both; the call was NOT made.",
    }


#: The engine's real paper sizes — sheet_dims in core/doc/drawing_session.cpp (ISO A0..A4 +
#: ANSI A..E). Landscape by default; a trailing "P" is the same paper portrait ("A4P").
_SHEET_SIZES = ("A4", "A3", "A2", "A1", "A0", "A", "B", "C", "D", "E")
#: Words a model decorates a size with ("ISO A3", "A4 landscape", "A3 sheet size").
_SHEET_NOISE = frozenset({"iso", "ansi", "din", "jis", "landscape", "portrait",
                          "size", "sheet", "paper", "format"})


def _canon_sheet_size(value: Any) -> tuple[Optional[str], Optional[dict]]:
    """('A3' | 'A4P', None) or (None, an error dict naming the real sizes).

    SILENT-WRONG-OUTPUT guard: drawing_make wraps the sheet write in
    `try { drawing_set_sheet(...) } catch (const OpError&) { /* bad size -> keep default A3 */ }`,
    so an unrecognised size produced an A3 sheet that looks completely normal — the caller
    asked for A1 and shipped A3. Validate the spelling before the drawing is created instead.
    "portrait" (or a trailing P, "A4P") selects the portrait orientation of the same paper.
    """
    raw = str(value if value is not None else "").strip()
    words = [w for w in _re.split(r"[\s\-_]+", raw.lower()) if w]
    portrait = "portrait" in words
    kept = [w for w in words if w not in _SHEET_NOISE] or words
    s = "".join(kept).upper()
    if len(s) >= 2 and s.endswith("P") and s[:-1] in _SHEET_SIZES:
        s, portrait = s[:-1], True
    if s in _SHEET_SIZES:
        return (s + "P" if portrait else s), None
    return None, {
        "ok": False,
        "code": "invalid_op",
        "error": f"sheet {raw!r} is not a paper size; the drawing engine knows "
                 + ", ".join(_SHEET_SIZES)
                 + " (ISO A0-A4 and ANSI A-E, landscape; add 'portrait' or a P suffix, e.g. "
                   "'A4P', for portrait). An unknown size used to fall back to A3 silently, so "
                   "this call was refused instead of drawn on the wrong sheet.",
        "valid_sheet": list(_SHEET_SIZES),
    }



def _template_verdict(value: Any) -> Optional[dict]:
    """None when *value* IS a paper size (safe to fold onto `sheet`), else a refusal.

    `template="A4"` is the paper size under another name. A template that is NOT a size has no
    home: this codebase has no drawing-template / titleblock-file concept anywhere (the only
    `template` keys in the tree belong to the deliverables report engine, the sourcing
    spec-sheet matcher and the reveng session graphs — different subsystems entirely). A sheet
    here is exactly two things: a PAPER SIZE (drawing_session.cpp:40-45) and title-block TEXT
    (drawing_set_titleblock, drawing_session.cpp:1758-1768). Folding a template FILE onto the
    paper size would file one concept as the other, so it is refused with both concepts named.
    """
    if value is None or _canon_sheet_size(value)[0]:
        return None
    return {
        "ok": False,
        "status": "unknown_argument",
        "error": f"make_drawing() has no drawing-TEMPLATE parameter, and template={value!r} is "
                 f"not a paper size. Chudware has no titleblock/border template file: a sheet "
                 f"is its PAPER SIZE (sheet=" + "|".join(_SHEET_SIZES) + ") plus its "
                   'title-block TEXT (make_drawing(title={"title":…}) or '
                 f"set_titleblock(...)). Those are two separate arguments — pick the one you "
                 f"meant; the call was NOT made.",
        "unknown_params": ["template"],
        "hint": 'make_drawing(doc_id=…, sheet="A4", title={"title": "…", "rev": "A"})',
    }


#: `model_items` is the engine's auto-dimension pass over the ortho views
#: (core/doc/drawing_session.cpp:1948-1952 -> drawing_model_items at :1648, which emits the
#: overall width/height linear dims and one "N× ⌀d" diameter dim per distinct hole). It is a
#: BOOLEAN switch: it cannot be told WHICH dimensions to place. The drafting phrasings below
#: all describe exactly that pass, so reading them as "yes" is a spelling fix, not an invented
#: feature; anything else is refused rather than passed to bool(), which would turn
#: model_items="none" and model_items="false" into TRUE.
_MODEL_ITEMS_ON = frozenset({
    "true", "1", "yes", "y", "on", "enable", "enabled", "auto", "all", "default",
    "basic_dims", "basic_dimensions", "basic", "basic_dim", "model_items", "model_item",
    "model_dims", "model_dimensions", "model_dim", "dims", "dimensions", "dim",
    "auto_dims", "auto_dimensions", "autodim", "standard", "standard_dims", "ortho_dims",
})
_MODEL_ITEMS_OFF = frozenset({
    "false", "0", "no", "n", "off", "disable", "disabled", "none", "skip", "never", "manual",
})


def _canon_model_items(value: Any) -> tuple[Optional[bool], Optional[dict]]:
    """(True|False, None) or (None, an error dict saying what the switch does.

    Live 2026-07-29: model_items="basic_dims". The capability is REAL, so the string is
    honoured — but `bool("none")` is True, so an unrecognised word must never reach the flag.
    """
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value), None
    w = _canon_word(value)
    if w in _MODEL_ITEMS_ON:
        return True, None
    if w in _MODEL_ITEMS_OFF:
        return False, None
    return None, {
        "ok": False,
        "code": "invalid_op",
        "error": f"model_items={value!r} is not a recognised value. model_items is a "
                 f"true/false SWITCH for the engine's auto-dimension pass, which places the "
                 f"overall width/height dimensions and one '\u00d7 \u2300' callout per distinct "
                 f"hole diameter on every ortho view — it cannot be told which dimensions to "
                 f"place. Pass model_items=true to run it (that is the default), or place "
                 f"individual dimensions yourself with add_dimension(view_id, a, b, dtype=...).",
        "valid_model_items": ["true", "false"],
    }

# ---------------------------------------------------------------- dimensioning a MODEL target
#
# Live (2026-07-29, claude-sonnet-4-5): the model held the stable sid of the hole it wanted
# called out and handed it straight to add_dimension —
#   add_dimension(dtype="diameter", a=["bore","F6356a7"], b=["bore","F6356a7"], pos=[80,120])
#   add_dimension(a=["F6356a7","center"], b=["F6356a7","edge"])
# add_dimension speaks SHEET millimetres; the agent holds MODEL handles (the sid describe_part /
# query_geometry hand out, or a feature name).
#
# drawing_get joins the two natively. Every projected circle (`arcs[].src`) and polyline
# (`visible_src` / `hidden_src`, index-aligned with `visible` / `hidden`) names the model edge it
# was drawn from — source body, TopExp edge id, the edge's sid and the sids of the faces it
# bounds — read off OCCT's HLR data structure (core/doc/drawing.cpp::hlr_project), not matched
# by size. A target therefore resolves to the projected circle that IS its sid: four identical
# holes are four sids on four circles. A feature name resolves to the round faces that feature
# created (cad_common._feature_owned_matches); if those are several separate circles the call is
# refused with each circle's sid, never guessed.
#
# The engine snaps a radial/diameter/center-mark to the arc whose RING is nearest the pick
# (drawing_session.cpp::nearest_arc) and reads the value off that arc, so the point handed over
# sits ON the matched ring, where its distance is exactly zero. A dimension attaches to visible
# geometry: a target drawn only as hidden lines in the asked view is refused, naming the views
# where it IS a visible circle.

#: A stable sid as doc_query / describe_part / hover hand them out: "F6356a7", "Ec2b379".
_SID_RE = _re.compile(r"^[FE][0-9A-Fa-f]{4,}$")

#: Words that say WHERE on a target to attach rather than WHAT the target is — the live call
#: wrote a=["F6356a7","center"], b=["F6356a7","edge"]. For a diameter/radius the arc determines
#: both points, so these carry no information; they are dropped WITH a note, never silently.
#: Deliberately NOT here: "hole"/"bore"/"boss"/"fillet" — those are ordinary feature NAMES (the
#: same run's target really was a feature called "bore"), so they must go through resolution.
_TARGET_QUALIFIERS = frozenset({
    "center", "centre", "centerpoint", "centrepoint", "center_point", "centre_point",
    "middle", "mid", "midpoint", "edge", "rim", "circle", "circumference", "arc",
    "boundary", "perimeter", "outline", "axis", "vertex", "point", "outer", "inner",
})

#: Sheet-mm float noise below which two projected circles are the same drawn circle — a through
#: hole's two rims project onto one ring (and one rim can be split into visible + hidden pieces).
_SAME_CIRCLE_MM = 1e-4

#: Dimension types whose geometry is fully determined by the picked ARC, so `b` is unused —
#: build_ext_annotation (drawing_session.cpp:1034-1061) reads the nearest arc and never looks
#: at b for these three.
_ARC_DETERMINED = frozenset({"radial_dim", "diameter_dim", "center_mark"})


def _point_or_target(value: Any, slot: str) -> tuple[Optional[list], Optional[list],
                                                     Optional[dict]]:
    """(an [x,y] sheet point, the model-target tokens, a refusal) — exactly one is not None.

    [80, 120] is a sheet point; "F6356a7" / ["bore", "F6356a7"] is a model target; a stringified
    form of either decodes first. A mix of numbers and names is refused rather than guessed at:
    [80, "F6356a7"] could be a point whose y is a sid or a target with a stray coordinate.
    """
    decoded = coerce_json_or_repr(value) if isinstance(value, str) else value
    if isinstance(decoded, (list, tuple)):
        items = list(decoded)
        nums = [x for x in items if isinstance(x, (int, float)) and not isinstance(x, bool)]
        names = [str(x).strip() for x in items
                 if isinstance(x, str) and str(x).strip()]
        if len(nums) == len(items) and items:
            return [float(x) for x in items], None, None
        if len(names) == len(items) and items:
            return None, names, None
        return None, None, {
            "ok": False,
            "error": f"add_dimension: {slot}={value!r} mixes sheet coordinates with names. "
                     f"Pass EITHER a sheet-mm point ({slot}=[80, 120], read off "
                     f"review_drawing) OR the model feature to dimension ({slot}=\"F6356a7\" "
                     f"— the sid describe_part/query_geometry gave you — or {slot}=\"Bore\", "
                     f"the feature name). Nothing was added.",
        }
    if isinstance(decoded, str) and decoded.strip():
        return None, [decoded.strip()], None
    return None, None, {
        "ok": False,
        "error": f"add_dimension: {slot}={value!r} is neither a sheet-mm point ([80, 120]) nor "
                 f"a model target (a face/edge sid like \"F6356a7\", or a feature name like "
                 f"\"Bore\"). Nothing was added.",
    }


def _source_entities(source_doc_id: str) -> list:
    """Every face + edge of the source part as doc_query hands them out (sid, surf, radius, …)."""
    out: list = []
    for of in ("face", "edge"):
        try:
            res = cb.call("doc_query", {"doc_id": source_doc_id, "query": {"of": of}})
        except Exception:  # noqa: BLE001 — a source with no bodies simply has no entities
            continue
        for m in (res.get("matches") or []) if isinstance(res, dict) else []:
            if isinstance(m, dict) and m.get("sid"):
                out.append(m)
    return out


def _source_features(source_doc_id: str) -> list:
    """The source part's feature rows (id, name, type, params) — [] when unreadable."""
    try:
        tree = cb.call("doc_tree", {"doc_id": source_doc_id})
    except Exception:  # noqa: BLE001
        return []
    node = tree.get("tree") if isinstance(tree, dict) else None
    feats = node.get("features") if isinstance(node, dict) else None
    return [f for f in (feats or []) if isinstance(f, dict)]


def _param_number(params: Any, *names: str) -> Optional[float]:
    """A feature param's number, whether stored bare or as {"value": 10, "expr": None}."""
    if not isinstance(params, dict):
        return None
    for n in names:
        v = params.get(n)
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None



def _is_round(m: dict) -> bool:
    r = m.get("radius")
    return isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0.0


def _entity_label(m: dict) -> str:
    """`F6ea1c6 (cylinder face, ⌀6 mm)` — what a sid names, in the words a refusal needs."""
    size = f", ⌀{round(2.0 * float(m['radius']), 6)} mm" if _is_round(m) else ""
    return f"{m.get('sid')} ({m.get('surf') or 'round'} {m.get('kind') or 'entity'}{size})"


def _target_entities(source_doc_id: str, tokens: list, slot: str,
                     notes: list) -> tuple[Optional[dict], Optional[dict]]:
    """({"sids", "label"} for the round model entities *tokens* name, None) or (None, a refusal).

    A sid names itself; a feature name names the round faces that feature created. An explicit
    sid is the most specific handle, so beside a feature name it wins and the name is only
    checked against it. Positional qualifiers ("center", "edge") carry nothing for a circle and
    are dropped with a note. Appends what was read/ignored to *notes*.
    """
    what = tokens if len(tokens) > 1 else tokens[0]
    sid_tokens: list = []
    feature_tokens: list = []
    for t in tokens:
        text = str(t).strip()
        if _SID_RE.match(text):
            sid_tokens.append(text)
        elif _canon_word(text) in _TARGET_QUALIFIERS:
            notes.append(f"{slot}: ignored the qualifier {text!r} — the projected circle "
                         f"determines the dimension's points")
        else:
            feature_tokens.append(text)
    misses: list = []
    picked: list = []
    if sid_tokens:
        by_sid = {str(m["sid"]).casefold(): m for m in _source_entities(source_doc_id)}
        for text in sid_tokens:
            hit = by_sid.get(text.casefold())
            if hit is None:
                misses.append(f"{text!r} is not a face/edge sid of the source part "
                              f"{source_doc_id} (sids are regenerated when the model rebuilds — "
                              f"call describe_part / query_geometry for the current ones)")
            elif not _is_round(hit):
                misses.append(f"{text!r} is a {hit.get('surf') or 'non-round'} "
                              f"{hit.get('kind') or 'entity'} — it has no radius, so it does not "
                              f"project as a circle. Pass the sid of a round face or circular "
                              f"edge (query_geometry(query={{'of':'face','surf':'cylinder'}})), "
                              f"or dimension it with two sheet-mm points")
            else:
                picked.append(hit)
    by_feature: list = []
    for text in feature_tokens:
        try:
            owned = _feature_owned_matches(source_doc_id, text)
        except ValueError as exc:
            misses.append(str(exc))
            continue
        rounds = [m for m in owned if isinstance(m, dict) and m.get("sid") and _is_round(m)]
        if not rounds:
            misses.append(f"the feature {text!r} created no round face or edge, so there is no "
                          f"circle to dimension. Pass the sid of a round face/edge "
                          f"(query_geometry(query={{'of':'face','surf':'cylinder'}})) or two "
                          f"sheet-mm points")
            continue
        by_feature.append((text, rounds))
    if not picked and not by_feature:
        detail = "; ".join(misses) or "it named nothing resolvable"
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot}={what!r} did not resolve to a round model "
                     f"feature — {detail}. Nothing was added.",
        }
    if picked:
        sids = {str(m["sid"]) for m in picked}
        label = ", ".join(_entity_label(m) for m in picked)
        for name, rounds in by_feature:
            owned = {str(m["sid"]) for m in rounds}
            if sids & owned:
                notes.append(f"{slot}: {name!r} owns {', '.join(sorted(sids & owned))} — "
                             f"consistent with the sid")
            else:
                notes.append(f"{slot}: {name!r} does not own {', '.join(sorted(sids))} (its "
                             f"round faces are {', '.join(sorted(owned))}); the explicit sid "
                             f"was used")
    else:
        sids = {str(m["sid"]) for _name, rounds in by_feature for m in rounds}
        label = "; ".join(f"the feature {name!r} (round face(s) "
                          f"{', '.join(_entity_label(m) for m in rounds)})"
                          for name, rounds in by_feature)
    for why in misses:
        notes.append(f"{slot}: ignored — {why}")
    notes.append(f"{slot}={what!r} read as {label}")
    return {"sids": sids, "label": label}, None


def _view_scale(view: dict) -> float:
    scale = view.get("scale") or 1.0
    if not isinstance(scale, (int, float)) or scale == 0:
        scale = 1.0
    return float(scale)


def _src_sids(src: Any) -> set:
    """Every sid one provenance record answers to: its own sid and the faces it bounds."""
    if not isinstance(src, dict):
        return set()
    out = {s for s in (src.get("face_sids") or []) if isinstance(s, str)}
    if isinstance(src.get("sid"), str):
        out.add(src["sid"])
    return out


def _view_circles(view: dict, sids: Optional[set] = None,
                  need_closed: bool = False) -> list:
    """This view's projected circles, coincident rims collapsed, each as {center (sheet mm),
    radius (sheet mm), model_radius, closed, visible, edge_sids, face_sids, matched}.

    `visible` is True when any collapsed arc is on the visible layer. With *sids*, only arcs
    whose provenance (drawing_get `arcs[].src`) answers to one of them are kept, and `matched`
    lists which. Sorted by sheet position.
    """
    scale = _view_scale(view)
    out: list = []
    for arc in view.get("arcs") or []:
        if not isinstance(arc, dict):
            continue
        c = arc.get("center")
        r_sheet = arc.get("radius")
        if not (isinstance(c, list) and len(c) == 2):
            continue
        if not isinstance(r_sheet, (int, float)) or r_sheet <= 0.0:
            continue
        closed = bool(arc.get("closed"))
        if need_closed and not closed:
            continue
        src = arc.get("src")
        answers = _src_sids(src)
        matched = answers & sids if sids is not None else set()
        if sids is not None and not matched:
            continue
        center = [float(c[0]), float(c[1])]
        item = next((o for o in out
                     if abs(o["center"][0] - center[0]) < _SAME_CIRCLE_MM
                     and abs(o["center"][1] - center[1]) < _SAME_CIRCLE_MM
                     and abs(o["radius"] - float(r_sheet)) < _SAME_CIRCLE_MM
                     and o["closed"] == closed), None)
        if item is None:
            item = {"center": center, "radius": float(r_sheet),
                    "model_radius": float(r_sheet) / scale, "closed": closed,
                    "visible": False, "edge_sids": [], "face_sids": [], "matched": []}
            out.append(item)
        item["visible"] = item["visible"] or arc.get("layer") == "visible"
        if isinstance(src, dict):
            if isinstance(src.get("sid"), str) and src["sid"] not in item["edge_sids"]:
                item["edge_sids"].append(src["sid"])
            for f in src.get("face_sids") or []:
                if isinstance(f, str) and f not in item["face_sids"]:
                    item["face_sids"].append(f)
        for m in sorted(matched):
            if m not in item["matched"]:
                item["matched"].append(m)
    out.sort(key=lambda it: (round(it["center"][0], 6), round(it["center"][1], 6)))
    return out


def _views_of(full: dict) -> list:
    out: list = []
    for sheet in (full.get("sheets") or []):
        for v in (sheet.get("views") or []):
            if isinstance(v, dict):
                out.append(v)
    return out


def _drawn_as_lines(view: dict, sids: set) -> bool:
    """True when *view* draws any polyline whose provenance answers to one of *sids*."""
    for key in ("visible_src", "hidden_src"):
        for src in view.get(key) or []:
            if _src_sids(src) & sids:
                return True
    return False


def _resolve_target_point(full: dict, view_id: str, target: dict, dtype: str,
                          slot: str, notes: list) -> tuple[Optional[dict], Optional[dict]]:
    """({"point", "circle"} for the ONE visible projected circle of *target* in *view_id*, None)
    or (None, a refusal). `point` is the circle centre for spacing dims and a point on its ring
    for arc-determined dims."""
    need_closed = dtype == "diameter_dim"          # engine: want_closed=1 only for diameter_dim
    shape = "full circle" if need_closed else "circle or arc"
    sids, label = target["sids"], target["label"]
    view = next((v for v in _views_of(full)
                 if view_id in (v.get("view_id"), v.get("id"))), None)
    if view is None:
        return None, {"ok": False, "error": f"add_dimension: no view {view_id!r} in this drawing"}
    here = f"view {view_id} ({view.get('orientation') or view.get('type')})"
    circles = _view_circles(view, sids, need_closed)
    visible = [c for c in circles if c["visible"]]
    if len(visible) > 1:
        listing = "; ".join(
            f"⌀{round(2.0 * c['model_radius'], 6)} at {[round(x, 3) for x in c['center']]} "
            f"(sid {', '.join(c['matched'])}; edge {', '.join(c['edge_sids']) or '—'})"
            for c in visible)
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot}={label} is {len(visible)} separate projected "
                     f"{shape}s in {here}: {listing}. A dimension has one target — pass the "
                     f"sid of the one you mean. Nothing was added.",
        }
    if not visible:
        seen = []
        for v in _views_of(full):
            vid = v.get("view_id") or v.get("id")
            if vid == view_id:
                continue
            if any(c["visible"] for c in _view_circles(v, sids, need_closed)):
                seen.append(f'{vid} ({v.get("orientation") or v.get("type")})')
        if circles:
            why = (f"is HIDDEN in {here}: it is drawn there only as hidden lines, and a "
                   f"dimension must attach to visible geometry.")
        elif need_closed and _view_circles(view, sids):
            why = (f"is only partly visible in {here}: hidden portions break its circle into "
                   f"arcs, so there is no full circle to put a diameter on.")
        elif _drawn_as_lines(view, sids):
            why = f"is seen edge-on in {here}: it projects there as lines, not as a {shape}."
        else:
            why = f"does not appear in {here} at all."
        where = (f" It is a visible {shape} in view(s) {', '.join(seen)} — dimension it there."
                 if seen else
                 f" No view of this drawing shows it as a visible {shape}: add the view that "
                 f"looks down its axis with add_drawing_view(orientation=…), then dimension it "
                 f"there.")
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot}={label} {why}{where} Nothing was added.",
        }
    chosen = visible[0]
    notes.append(f"{slot}: {', '.join(chosen['matched'])} is the projected "
                 f"⌀{round(2.0 * chosen['model_radius'], 6)} mm {shape} at "
                 f"{[round(x, 3) for x in chosen['center']]} sheet mm in view {view_id}")
    if dtype not in _ARC_DETERMINED:
        return {"point": list(chosen["center"]), "circle": chosen}, None
    # The engine selects the arc whose RING is nearest the pick. The centre would make every
    # concentric ring equally near and the smallest would win; a point on the matched ring is
    # at distance zero from it.
    return {"point": [chosen["center"][0] + chosen["radius"], chosen["center"][1]],
            "circle": chosen}, None


# ---------------------------------------------------------------- drawing / view scale
#
# LIVE-DIALECT gap (2026-07-29): make_drawing(scale=1.0) died twice as unknown_argument. Two
# DIFFERENT things are called "scale" in a drawing and they must not be confused:
#   * the title block's SCALE cell — a TEXT field ("1:1"), set through set_titleblock(scale=…)
#     / make_drawing(title={"scale":"1:1"}). It only prints.
#   * a view's geometric scale — a real number on every view (drawing_session.cpp:855, honoured
#     by project_one at :430 as geometry = model*scale + position, and settable through
#     drawing_set_view :1009-1012). It changes the drawing.
# scale=1.0 is a NUMBER, so it plainly means the second one, and the second one exists — so it
# is wired, not refused. drawing_make itself has no scale key in its spec (it auto-scales to fit,
# :1841-1946), so the value is applied afterwards per view.
#
# Rescaling a view MOVES its geometry: the engine lays geometry at model*scale + position, so
# leaving `position` alone would slide every view across the sheet. The position is therefore
# recomputed from the engine's own documented relation (drawing_session.cpp:1917-1920) —
# centre_model = (bbox_centre - position)/old_scale, then position = bbox_centre -
# centre_model*new_scale — and then READ BACK: the reply reports each view's resulting scale and
# flags any view whose centre moved or that no longer fits the sheet. The title-block SCALE text
# is rewritten to match, because a sheet that reads "1:2" while its views are 1:1 is exactly the
# silently-wrong drawing this file exists to prevent.

#: ISO 5455 / ASME Y14.1 preferred scales — core/doc/drawing_session.cpp:56-63. A value off this
#: table still applies (the engine accepts any positive scale); it is only reported as such.
_STANDARD_SCALES = (100.0, 50.0, 20.0, 10.0, 5.0, 2.0, 1.0,
                    1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50,
                    1 / 100, 1 / 200, 1 / 500, 1 / 1000)

_RATIO_RE = _re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*[:/]\s*([0-9]*\.?[0-9]+)\s*$")


def _scale_text(scale: float) -> str:
    """The title block's "N:1"/"1:N" text for a numeric scale — mirrors scale_text() at
    core/doc/drawing_session.cpp:77-89, so an explicitly-scaled drawing prints the same
    convention as an auto-scaled one."""
    if scale >= 1.0:
        return (f"{int(round(scale))}:1" if abs(scale - round(scale)) < 1e-6
                else f"{scale:.3g}:1")
    inv = 1.0 / scale
    return f"1:{int(round(inv))}" if abs(inv - round(inv)) < 1e-6 else f"1:{inv:.3g}"


def _canon_view_scale(value: Any) -> tuple[Optional[float], Optional[dict]]:
    """(a positive scale factor, None) or (None, a refusal naming the two "scale" concepts)."""
    raw = value
    if isinstance(value, bool):
        num = None
    elif isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value if value is not None else "").strip()
        m = _RATIO_RE.match(text)
        if m:
            num, den = float(m.group(1)), float(m.group(2))
            num = (num / den) if den else None
        else:
            try:
                num = float(text)
            except ValueError:
                num = None
    if num is None or num <= 0.0:
        return None, {
            "ok": False,
            "error": f"make_drawing: scale={raw!r} is not a scale factor. A view scale is a "
                     f"POSITIVE number or ratio — scale=1.0 (1:1), scale=0.5 or \"1:2\" (half "
                     f"size), scale=2 or \"2:1\" (twice size); it rescales the projected "
                     f"geometry. The title block's printed SCALE cell is a different thing — "
                     f"pass that as title={{\"scale\": \"1:1\"}} or "
                     f"set_titleblock(scale=\"1:1\"). Nothing was created.",
            "valid_scale": ["1.0", "0.5", "2", "1:2", "2:1"],
        }
    return num, None


def _scale_readback(doc_id: str, want: float, notes: list) -> tuple[list, list]:
    """Read back every view's applied scale after make_drawing laid the sheet out at *want*.

    Returns (per-view {view_id, orientation, scale}, warnings) — a view that kept another
    scale, or that the requested scale pushed past the paper, is reported, never silent.
    """
    after = cb.call("drawing_get", {"doc_id": doc_id})
    sheets = after.get("sheets") or []
    size_mm = (sheets[0].get("size_mm") if sheets else None) or [0.0, 0.0]
    applied: list = []
    warnings: list = []
    for v in _views_of(after):
        if v.get("type") == "table_overlay":
            continue
        vid = v.get("view_id") or v.get("id")
        got = v.get("scale")
        applied.append({"view_id": vid, "orientation": v.get("orientation") or v.get("type"),
                        "scale": got})
        if (isinstance(got, (int, float)) and v.get("type") not in ("detail",)
                and abs(float(got) - want) > 1e-9):
            warnings.append(f"view {vid} kept scale {got} instead of {want}")
        bbox = v.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4
                and all(isinstance(x, (int, float)) for x in bbox)):
            continue
        if size_mm[0] > 0.0 and (bbox[0] < 0.0 or bbox[1] < 0.0
                                 or bbox[2] > size_mm[0] or bbox[3] > size_mm[1]):
            warnings.append(f"view {vid} now extends past the {size_mm[0]}x{size_mm[1]} mm "
                            f"sheet at scale {want} — use a bigger sheet= or a smaller scale")
    notes.append(f"scale={want} applied to {len(applied)} view(s) as a geometric view scale "
                 f"(title-block SCALE text set to {_scale_text(want)!r})")
    return applied, warnings


# ---------------------------------------------------------------- sheet layout readback
#
# SILENT-WRONG-OUTPUT class, measured 2026-07-30 driving the full "make a dimensioned drawing"
# flow on a 50x30x4 plate (A3, 420x297 mm):
#   set_view(view_id="v2", x=900, y=900)  -> {"ok":true,"position":[900,900]}
#       the view's bbox then reads [900, 840, 1000, 900] — entirely OFF the paper.
#   set_view(view_id="v1", scale=50)      -> {"ok":true,"scale":50.0}
#       bbox [258, -114.8, 2758, 85.2] — 2.5 m of geometry hanging off three sides.
#   export_drawing then still answered ok with 91 kB of PDF.
# Nothing in any reply said the view had left the sheet, and set_view's own docstring is what
# tells a model to "spread overlapping views apart" in the first place — so the tool invites
# exactly the move that silently destroys the drawing.
#
# The rule is not new: make_drawing's rescale pass already refuses to stay quiet about it
# (_apply_view_scale above warns "view v1 now extends past the 420x297 mm sheet"). It had
# simply never been applied to the per-view tools. Overlap is reported the same way, because
# "views colliding on the sheet" is the other half of what review_drawing's docstring asks the
# model to look for — and a rectangle intersection is a NUMBER we can hand it instead of an
# image to squint at.


def _bbox_of(view: Any) -> Optional[list]:
    """A view's [x0,y0,x1,y1] sheet-mm bbox, or None when it has none (an empty view)."""
    bb = view.get("bbox") if isinstance(view, dict) else None
    if (isinstance(bb, list) and len(bb) == 4
            and all(isinstance(x, (int, float)) for x in bb)):
        return [float(x) for x in bb]
    return None


def _sheet_size(full: dict) -> Optional[list]:
    """The sheet's [width, height] in mm (landscape w >= h; portrait "…P" sizes w < h)."""
    for sh in (full.get("sheets") or []):
        size = sh.get("size_mm")
        if (isinstance(size, list) and len(size) == 2
                and all(isinstance(x, (int, float)) and x > 0 for x in size)):
            return [float(size[0]), float(size[1])]
    return None


def _off_sheet_by(bbox: list, size: list) -> dict:
    """How far each side of *bbox* sticks out past a 0,0..size sheet — {} when it fits."""
    out = {}
    for name, over in (("left", -bbox[0]), ("bottom", -bbox[1]),
                       ("right", bbox[2] - size[0]), ("top", bbox[3] - size[1])):
        if over > 1e-6:
            out[name] = round(over, 3)
    return out


def _rect_overlap(p: list, q: list) -> Optional[list]:
    """The [width, height] mm of the rectangle p and q share, or None when they do not."""
    w = min(p[2], q[2]) - max(p[0], q[0])
    h = min(p[3], q[3]) - max(p[1], q[1])
    return [round(w, 3), round(h, 3)] if w > 1e-6 and h > 1e-6 else None


def _layout_report(full: dict, view_id: str) -> tuple[dict, list]:
    """(what *view_id* occupies on the sheet, the warnings that occupation earns).

    Read back off the engine's own projection, never computed here: the bbox is what
    project_one produced after the move/rescale, so the numbers are the drawing's.
    """
    size = _sheet_size(full)
    me = None
    others = []
    for v in _views_of(full):
        vid = v.get("view_id") or v.get("id")
        if vid == view_id:
            me = v
        else:
            others.append(v)
    report: dict = {}
    warnings: list = []
    if me is None:
        return report, warnings
    if size:
        report["sheet_mm"] = size
    bbox = _bbox_of(me)
    err = me.get("error")
    if err:
        # The engine already knows the view is empty (e.g. "detail circle encloses no parent
        # geometry") and files it on the view; only the reply was hiding it.
        warnings.append(f"view {view_id} has NO geometry: {err}")
    if bbox is None:
        report["entities"] = 0
        if not err:
            warnings.append(f"view {view_id} projected no geometry — it will render blank")
        return report, warnings
    report["bbox"] = [round(x, 3) for x in bbox]
    report["entities"] = len(me.get("visible") or []) + len(me.get("arcs") or [])
    if report["entities"] == 0 and not err:
        warnings.append(f"view {view_id} projected no geometry — it will render blank")
    if size:
        off = _off_sheet_by(bbox, size)
        report["on_sheet"] = not off
        if off:
            report["off_sheet_by_mm"] = off
            warnings.append(
                f"view {view_id} hangs off the {size[0]}x{size[1]} mm sheet by "
                + ", ".join(f"{v} mm past the {k}" for k, v in sorted(off.items()))
                + " — it will be missing from the render and the export")
    hits = []
    for v in others:
        ob = _bbox_of(v)
        if ob is None:
            continue
        lap = _rect_overlap(bbox, ob)
        if lap:
            vid = v.get("view_id") or v.get("id")
            hits.append({"view_id": vid, "orientation": v.get("orientation") or v.get("type"),
                         "overlap_mm": lap})
    if hits:
        report["overlaps"] = hits
        warnings.append(
            f"view {view_id} overlaps "
            + ", ".join(f"{h['view_id']} ({h['orientation']}) by "
                        f"{h['overlap_mm'][0]}x{h['overlap_mm'][1]} mm" for h in hits)
            + " — move one with set_view(view_id, x=…, y=…)")
    return report, warnings

def _source_material(full: dict) -> Optional[str]:
    """The source part's stored material key, if the drawing still has a live source."""
    source = full.get("source_doc_id") or full.get("source")
    if not source:
        return None
    try:
        tree = cb.call("doc_tree", {"doc_id": source})
        material = (tree.get("tree") or {}).get("material")
        if isinstance(material, dict):
            value = material.get("key") or material.get("name")
        else:
            value = material
        return str(value).strip() if value else None
    except Exception:  # noqa: BLE001 — review still works after the source closes
        return None


def _material_matches(source: str, printed: str) -> bool:
    source_tokens = [t for t in _re.split(r"[^a-z0-9]+", source.lower()) if t]
    printed_norm = _re.sub(r"[^a-z0-9]+", "", printed.lower())
    meaningful = [t for t in source_tokens if len(t) >= 3 and t not in {"material"}]
    return bool(meaningful) and any(t in printed_norm for t in meaningful)


def _drawing_review_summary(full: dict, render: dict,
                            audit_result: Optional[Any] = None) -> dict:
    """Compact sheet-mm inspection payload paired with review_drawing's pixels.

    Layout review is necessary but never sufficient for release.  Manufacturing readiness is
    true only when the caller supplies a successful ``audit_drawing`` result for this drawing.
    """
    warnings: list[str] = []
    views: list[dict] = []
    seen_warnings: set[str] = set()
    for view in _views_of(full):
        # Extended title/revision/standards tables are emitted as a synthetic overlay view.
        # It deliberately has no projected model geometry and must not be diagnosed as blank.
        if _canon_word(view.get("type")) == "table_overlay":
            continue
        vid = view.get("view_id") or view.get("id")
        if not vid:
            continue
        report, view_warnings = _layout_report(full, vid)
        for warning in view_warnings:
            if warning not in seen_warnings:
                warnings.append(warning)
                seen_warnings.add(warning)
        annotations = []
        for ann in view.get("annotations") or []:
            if not isinstance(ann, dict):
                continue
            annotations.append({
                key: ann.get(key) for key in
                ("id", "kind", "type", "text", "value", "pos", "auto")
                if ann.get(key) is not None
            })
        circles = [{
            "center": [round(x, 3) for x in circle["center"]],
            "diameter_mm": round(2.0 * circle["model_radius"], 6),
            "closed": circle["closed"],
            "visible": circle["visible"],
            # The model handles this circle was drawn from — add_dimension(a=<sid>) lands on it.
            "edge_sids": circle["edge_sids"],
            "face_sids": circle["face_sids"],
        } for circle in _view_circles(view)[:24]]
        views.append({
            "view_id": vid,
            "orientation": view.get("orientation") or view.get("type"),
            "type": view.get("type"),
            "position": view.get("position"),
            "scale": view.get("scale"),
            **report,
            "circles": circles,
            "annotations": annotations,
        })

    sheet = (full.get("sheets") or [{}])[0]
    title_block = sheet.get("title_block") if isinstance(sheet, dict) else {}
    title_block = title_block if isinstance(title_block, dict) else {}
    required = ("title", "part_number", "material", "scale", "rev")
    missing = [field for field in required if not str(title_block.get(field) or "").strip()]
    if missing:
        warnings.append("title block missing manufacturing fields: " + ", ".join(missing))
    source_material = _source_material(full)
    printed_material = str(title_block.get("material") or "").strip()
    if source_material and printed_material and not _material_matches(source_material,
                                                                      printed_material):
        warnings.append(
            f"material mismatch: source model stores {source_material!r}, title block prints "
            f"{printed_material!r}; set_material on the source or correct the title block")

    audit = coerce_json_or_repr(audit_result) if audit_result is not None else None
    drawing_id = full.get("doc_id")
    drawing_revision = full.get("revision")
    audit_id = (audit.get("drawing_id") or audit.get("drawing_doc_id")
                if isinstance(audit, dict) else None)
    audit_revision = audit.get("drawing_revision") if isinstance(audit, dict) else None
    audit_matches = isinstance(audit, dict) and (
        not audit_id or str(audit_id) == str(drawing_id)
    ) and (drawing_revision is None or audit_revision == drawing_revision)
    audit_passed = bool(audit_matches and audit.get("ok")
                        and audit.get("verdict") == "release_ready")
    layout_clean = not warnings
    manufacturing_ready = layout_clean and audit_passed
    if audit_result is None:
        ready_reason = ("manufacturing completeness was not audited; call audit_drawing with "
                        "the governing spec, then pass its result as audit_result")
    elif not isinstance(audit, dict):
        ready_reason = "audit_result is not an audit_drawing result object"
    elif not audit_matches:
        ready_reason = (f"audit_result does not match drawing {drawing_id!r} at revision "
                        f"{drawing_revision!r}; rerun audit_drawing after the latest mutation")
    elif audit.get("verdict") != "release_ready":
        blockers = (
            audit.get("missing_critical")
            or (audit.get("standards") or {}).get("failures")
            or audit.get("verdict")
        )
        ready_reason = f"audit_drawing did not release this sheet: {blockers}"
    elif warnings:
        ready_reason = "manufacturing audit passed, but layout/title warnings remain"
    else:
        ready_reason = "layout, manufacturing coverage, and standards audit all passed"
    next_actions = []
    if warnings:
        next_actions.append("Fix every layout/title warning, then review_drawing again.")
    if not audit_passed:
        next_actions.append(
            "Run audit_drawing(doc_id, drawing_id, spec), resolve every missing critical row, "
            "and apply every standards failure remedy.")
    if manufacturing_ready:
        next_actions.append(
            "Layout, audited spec coverage, and drawing standards are clean; "
            "export without mutations.")
    return {
        "ok": True,
        "drawing_doc_id": drawing_id,
        "source_doc_id": full.get("source_doc_id"),
        "source_material": source_material,
        "sheet_mm": _sheet_size(full),
        "render": {k: render.get(k) for k in
                   ("visible", "hidden", "annotations", "title_block") if k in render},
        "views": views,
        "title_block": title_block,
        "warnings": warnings,
        "layout_clean": layout_clean,
        "manufacturing_ready": manufacturing_ready,
        "manufacturing_ready_reason": ready_reason,
        "audit_standards": audit.get("standards") if isinstance(audit, dict) else None,
        "next_actions": next_actions,
    }


def _find_view(full: dict, handle: Any, *, derived: bool = True) -> Optional[dict]:
    """The view *handle* names — its view_id, its id, its orientation, or its section/detail
    label. *derived* False skips section and detail views (a section's parent must be a real
    projected view). Returns the view row, or None."""
    want = str(handle if handle is not None else "").strip()
    if not want:
        return None
    folded = want.casefold()
    for v in _views_of(full):
        if not derived and v.get("type") in ("section", "detail"):
            continue
        if want in (v.get("view_id"), v.get("id")):
            return v
        for key in ("orientation", "label"):
            got = v.get(key)
            if isinstance(got, str) and got.casefold() == folded:
                return v
    return None


def _view_handles(full: dict, *, derived: bool = True) -> str:
    """Every handle a caller could have passed, as the teaching errors quote them back."""
    out = []
    for v in _views_of(full):
        if not derived and v.get("type") in ("section", "detail"):
            continue
        vid = v.get("view_id") or v.get("id")
        label = v.get("orientation") or v.get("label") or v.get("type")
        out.append(f"{vid} ({label})" if label else str(vid))
    return ", ".join(out) if out else "none — the drawing has no views"


# ---------------------------------------------------------------- title-block vocabulary
#
# Lives at module scope because TWO tools write the title block and only one of them was
# guarding it. set_titleblock has enforced this vocabulary since 2026-07-29; make_drawing's
# `title={...}` handed the dict straight to drawing_make, which forwards it verbatim to
# drawing_set_titleblock (core/doc/drawing_session.cpp:2068 -> :1759, a raw merge). Measured
# 2026-07-30 with the same `part_name` dialect the live run wrote at the other door:
#   make_drawing(title={"part_name":"Plate","scale":"1:1"})
#     -> ok:true, and the sheet's title_block reads {"part_name":"Plate", ...}
# The renderer keys its ten cells by name (core/doc/session.cpp:7106-7117) and the exporter by
# eight (core/doc/export2d.cpp:205-207), so "part_name" is a key NOTHING draws: the part name
# the caller asked for is simply absent from the sheet, with no error anywhere. Routing both
# doors through one table is the fix — the same call now lands in TITLE, exactly as it does
# through set_titleblock.
#
# EXPORT MIRROR. The on-screen renderer accepts {"rev","revision"} interchangeably
# (session.cpp:7114) but export2d.cpp:205-207 reads only "revision" — and the canonical name
# this layer folds onto is "rev". Measured 2026-07-30: set_titleblock(revision="B",
# material="6061-T6", drawn="hg") -> exported DXF contains "6061-T6" and "hg" and NO revision
# line at all. The revision is the one title-block cell a shop floor keys off, so it vanishing
# from every delivered PDF/DXF while showing on screen is the worst shape this file guards
# against. Writing BOTH spellings is exact, not a guess: the two are the same cell to the
# renderer, and it is the only one of them the exporter can see.

#: Canonical title-block field names — echoed in the docstring AND accepted flattened as
#: top-level kwargs. Live 2026-07-27 (claude-opus, flyswatter run): the model was SHOWN these
#: exact names in the docstring but wrote them as set_titleblock(title=…, drawn_by=…, date=…,
#: rev=…) instead of nesting them in fields={...}, and got "unknown argument(s)" for all four.
#: Not a synonym of `fields` (there is no single unambiguous target) — it is the same
#: flattened-dict dialect query_geometry already accepts for its query keys.
_TITLEBLOCK_FIELDS = ("title", "drawn_by", "date", "material", "finish",
                      "scale", "rev", "sheet", "part_number", "company",
                      "checked_by", "approved_by", "projection", "general_tolerance",
                      "mass", "drawing_standard")

#: _norm_arg spelling -> the canonical field ("drawnby" -> "drawn_by"), so a caller who writes
#: drawnBy / DRAWN_BY hits the same cell the docstring names.
_TITLEBLOCK_BY_NORM = {_norm_arg(f): f for f in _TITLEBLOCK_FIELDS}

#: Field-name aliases. The RENDERER already treats each of these as the SAME cell:
#: core/doc/session.cpp:7106-7117 keys the ten title-block cells by a pair — {"rev","revision"},
#: {"part_number","part_no"}, {"drawn_by","drawn"}. Folding them onto the canonical name is
#: therefore not a guess about intent; it is what the drawing does with them anyway.
#: Keys normalised through _norm_arg.
_TITLEBLOCK_ALIASES = {"revision": "rev", "partno": "part_number", "drawn": "drawn_by",
                       "checked": "checked_by", "approved": "approved_by",
                       "tolerance": "general_tolerance", "generaltolerances": "general_tolerance",
                       "standard": "drawing_standard", "weight": "mass",
                       "projectionmethod": "projection", "drawingnumber": "part_number"}

#: Spellings of "the part's NAME". A title block's TITLE cell is what carries a part's name
#: ("40mm Cube"); PART NO carries its identifier. So a name maps to `title` — and NEVER to
#: `part_number`: a name printed in the box a shop floor orders from is a wrong drawing that
#: looks right. Only folded while `title` is still free; if the caller set both, we refuse
#: rather than pick a winner.
_TITLEBLOCK_NAME_ARGS = frozenset({"partname", "name", "parttitle", "drawingtitle",
                                   "sheettitle"})

#: There is no units CELL — units are a property of the DOCUMENT. See the refusal below.
_TITLEBLOCK_UNIT_ARGS = frozenset({"units", "unit", "unitsystem", "unitssystem",
                                   "dimensionunits", "dimunits", "measurementunits",
                                   "unitofmeasure", "unitsofmeasure"})

#: canonical -> the second spelling that must be written beside it so the EXPORTER sees the
#: cell too. See the EXPORT MIRROR note above.
_TITLEBLOCK_EXPORT_MIRROR = {"rev": "revision"}


def _titleblock_units_refusal(name: str, value: Any) -> str:
    """Why `units` has no home here — verified against the engine, not assumed."""
    return (
        f"set_titleblock() has no {name!r} field. The title-block cells this drawing actually "
        f"renders are {', '.join(_TITLEBLOCK_FIELDS)} (the sheet frame, core/doc/annot_draw.cpp) — "
        f"units is not one of them, and passing it as a field would store a key nothing "
        f"draws. Units are a property of the DOCUMENT, not of the title block: the "
        f"drawing's own units.system field is 'MMGS' (millimetres) by default and 'IPS' "
        f"for inches, and it is what every dimension is formatted in "
        f"(core/doc/drawing_session.cpp:539-556). It is set on the document with the "
        f"set_units op — add_features(ops=[{{'op': 'set_units', 'units': 'mm'}}]) — not "
        f"here. {value!r} is already the default, so nothing needed changing; the call "
        f"was NOT made.")


def _canon_titleblock_fields(fields: dict, where: str) -> tuple[dict, list, Optional[dict]]:
    """(the fields as the drawing really keys them, interpretation notes, a refusal).

    *where* names the calling tool in the refusals, because the same dict arrives through
    make_drawing(title={...}) and set_titleblock(fields={...}).
    """
    out: dict = {}
    notes: list = []
    for key, value in fields.items():
        if value is None:
            continue
        n = _norm_arg(key)
        if n in _TITLEBLOCK_UNIT_ARGS:
            return {}, [], {"ok": False, "error": _titleblock_units_refusal(key, value)}
        target = _TITLEBLOCK_BY_NORM.get(n) or _TITLEBLOCK_ALIASES.get(n)
        if target is None and n in _TITLEBLOCK_NAME_ARGS:
            if "title" in out or any(_norm_arg(k) == "title" for k in fields):
                return {}, [], {
                    "ok": False,
                    "error": f"{where} got {key}={value!r} alongside an explicit title. A "
                             f"title block has no separate part-NAME cell: TITLE carries the "
                             f"part's name and PART NO carries its identifier "
                             f"(core/doc/annot_draw.cpp). {key} is NOT part_number — a "
                             f"name in the box a shop floor orders from is a wrong drawing "
                             f"that looks right. Pass the name as title=, or the identifier "
                             f"as part_number=. Nothing was written."}
            target = "title"
        if target is None:
            return {}, [], {
                "ok": False,
                "error": f"{where} got the unknown title-block field {key!r}. The cells this "
                         f"drawing renders are {', '.join(_TITLEBLOCK_FIELDS)} "
                         f"(the sheet frame, core/doc/annot_draw.cpp); anything else is stored as a key "
                         f"nothing draws, so the value would silently never appear on the "
                         f"sheet. Nothing was written."}
        if target in out:
            return {}, [], {
                "ok": False,
                "error": f"{where} got {key}={value!r} and another spelling of the same "
                         f"title-block cell {target!r}. Pass one. Nothing was written."}
        out[target] = value
        if target != key:
            notes.append(f"interpreted {key!r} as the title-block field {target!r}")
    return out, notes, None


def _titleblock_payload(fields: dict) -> dict:
    """*fields* plus the export-only spellings of the same cells (see EXPORT MIRROR above)."""
    payload = dict(fields)
    for canon, mirror in _TITLEBLOCK_EXPORT_MIRROR.items():
        if canon in payload and mirror not in payload:
            payload[mirror] = payload[canon]
    return payload

# ---------------------------------------------------------------- sheet orientation
#
# `orientation` has two real meanings and both are honoured. "portrait"/"landscape" is the
# SHEET's orientation — the engine keys portrait paper with a P suffix ("A4P" = 210 x 297,
# sheet_dims in core/doc/drawing_session.cpp), so make_drawing folds it onto `sheet`.
# "front"/"top"/"iso" is a VIEW orientation, which is what `views` takes (and what
# add_drawing_view calls the argument); that spelling is folded onto `views` with a note.

#: orient_frame's real view directions (core/doc/drawing_session.cpp) + "iso".
_VIEW_ORIENTATIONS = ("front", "back", "top", "bottom", "left", "right", "iso")


def _orientation_verdict(value: Any) -> tuple[Optional[list], Optional[dict]]:
    """(view names to draw, None) when *value* names VIEW orientations, else (None, a refusal)."""
    items = value if isinstance(value, (list, tuple)) else [value]
    words = [_canon_word(v) for v in items if str(v).strip()]
    if words and all(w in _VIEW_ORIENTATIONS for w in words):
        return list(words), None
    return None, {
        "ok": False,
        "error": f"make_drawing: orientation={value!r} is neither a VIEW orientation "
                 f"({', '.join(_VIEW_ORIENTATIONS)} — a synonym of views=) nor a sheet "
                 f"orientation (portrait | landscape). Nothing was created.",
        "valid_views": list(_VIEW_ORIENTATIONS),
    }

# ---------------------------------------------------------------- manufacturing semantics

_GDT_SYMBOLS = frozenset({
    "position", "flatness", "perpendicularity", "parallelism", "angularity",
    "concentricity", "symmetry", "profile_surface", "profile_line", "circularity",
    "cylindricity", "straightness", "circular_runout", "total_runout",
})
_GDT_MODIFIERS = frozenset({"none", "mmc", "lmc", "projected", "free", "tangent"})
_TOL_TYPES = frozenset({"none", "symmetric", "deviation", "limit", "basic"})
_WELD_TYPES = frozenset({"fillet", "square", "vee", "bevel", "plug"})
_WELD_CONTOURS = frozenset({"none", "flush", "convex", "concave"})
_WELD_FINISHES = frozenset({
    "none", "chipping", "grinding", "hammering", "machining", "planishing",
    "rolling", "unspecified",
})
_DATUM_TARGET_TYPES = frozenset({"point", "line", "area"})
_TABLE_KINDS = frozenset({"bom", "revision", "holes", "notes", "cutlist"})


def _engine_unsupported(tool: str, method: str, exc: Exception) -> Optional[dict]:
    """Return the stable honest-degrade envelope for a missing drawing RPC."""
    message = str(exc)
    folded = message.lower()
    if ("unknown method" not in folded and "cannot dispatch" not in folded
            and "not implemented" not in folded and "unsupported method" not in folded):
        return None
    return {
        "ok": False,
        "reason": "engine_unsupported",
        "tool": tool,
        "rpc": method,
        "error": message,
    }


def _drawing_rpc(tool: str, method: str, params: dict) -> tuple[Optional[dict], Optional[dict]]:
    try:
        return cb.call(method, params), None
    except Exception as exc:  # noqa: BLE001 — classify only missing-RPC errors
        unsupported = _engine_unsupported(tool, method, exc)
        folded = str(exc).lower()
        # Older cores know drawing_export but reject the newer SVG/DWG tokens. That is the
        # same capability mismatch as a missing RPC, not a malformed current-client request.
        if (not unsupported and params.get("fmt") in ("svg", "dwg") and "format" in folded
                and "pdf" in folded and "dxf" in folded):
            unsupported = {
                "ok": False,
                "reason": "engine_unsupported",
                "tool": tool,
                "rpc": method,
                "error": str(exc),
            }
        if unsupported:
            return None, unsupported
        raise


def _word(value: Any, valid: frozenset[str], field: str, tool: str) -> tuple[str, Optional[dict]]:
    word = _canon_word(value)
    if word in valid:
        return word, None
    return word, {
        "ok": False,
        "error": f"{tool}: {field} must be one of {', '.join(sorted(valid))}, got "
                 f"{value!r}. Nothing was added.",
        f"valid_{field}": sorted(valid),
    }


def _positive(value: Any, field: str, tool: str, *, allow_zero: bool = False) -> Optional[dict]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = -1.0
    if number < 0.0 or (not allow_zero and number <= 0.0):
        op = "non-negative" if allow_zero else "positive"
        return {"ok": False, "error": f"{tool}: {field} must be a {op} number, got "
                                      f"{value!r}. Nothing was added."}
    return None


def _feature_param_text(params: Any, *names: str) -> Optional[str]:
    if not isinstance(params, dict):
        return None
    for name in names:
        value = params.get(name)
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _hole_feature_defaults(full: dict, feature_ref: Any) -> tuple[dict, Optional[dict]]:
    """Read smart-callout fields from one source Hole feature without guessing geometry."""
    source = full.get("source_doc_id") or full.get("source")
    if not source:
        return {}, {"ok": False, "error": "add_hole_callout: this drawing has no live source "
                                         "document, so feature_ref cannot be resolved."}
    token = str(feature_ref).strip().casefold()
    features = _source_features(source)
    hits = [
        feature for feature in features
        if str(feature.get("id") or "").casefold() == token
        or str(feature.get("name") or "").casefold() == token
    ]
    if len(hits) != 1:
        choices = [
            {"id": feature.get("id"), "name": feature.get("name")}
            for feature in features
            if _canon_word(feature.get("type")) == "hole"
        ]
        return {}, {
            "ok": False,
            "error": f"add_hole_callout: feature_ref={feature_ref!r} resolved to {len(hits)} "
                     f"features; pass one Hole feature id or exact name. Nothing was added.",
            "hole_features": choices,
        }
    feature = hits[0]
    if _canon_word(feature.get("type")) != "hole":
        return {}, {
            "ok": False,
            "error": f"add_hole_callout: feature_ref={feature_ref!r} is a "
                     f"{feature.get('type')!r} feature, not a Hole. Nothing was added.",
        }
    params = feature.get("params") if isinstance(feature.get("params"), dict) else {}
    style = _canon_word(params.get("style") or params.get("hole_style")
                        or params.get("hole_type") or "simple")
    hole_type = {
        "tap": "tapped", "tapped": "tapped",
        "cbore": "counterbore", "counterbore": "counterbore",
        "csink": "countersink", "csk": "countersink",
        "countersink": "countersink",
    }.get(style, "simple")
    defaults: dict = {
        "hole_type": hole_type,
        "diameter": _param_number(params, "diameter", "dia", "d"),
        "depth": _param_number(params, "depth"),
        "cbore_dia": _param_number(params, "cbore_dia", "cbore_d", "counterbore_diameter"),
        "cbore_depth": _param_number(params, "cbore_depth", "counterbore_depth"),
        "csk_dia": _param_number(params, "csk_dia", "csink_d", "countersink_diameter"),
        "csk_depth": _param_number(params, "csk_depth", "csink_depth", "countersink_depth"),
        "csk_angle": _param_number(params, "csk_angle", "csink_angle", "countersink_angle"),
        "thread": _feature_param_text(params, "thread", "tap", "standard", "thread_size"),
        "thru": _canon_word(params.get("end") or params.get("end_condition")) in
                {"through", "through_all", "thru"},
    }
    for key in ("positions", "world_points"):
        placed = params.get(key)
        if isinstance(placed, list) and placed:
            defaults["count"] = len(placed)
            break
    return {key: value for key, value in defaults.items() if value is not None}, None


def _all_annotations(full: dict) -> list[dict]:
    out: list[dict] = []
    for view in _views_of(full):
        view_id = view.get("view_id") or view.get("id")
        for annotation in view.get("annotations") or []:
            if isinstance(annotation, dict):
                out.append({
                    **annotation,
                    "_declared_view_id": annotation.get("view_id"),
                    "view_id": view_id,
                })
    return out


def _audit_spec_rows(spec: Any) -> tuple[list[dict], Optional[dict]]:
    decoded = coerce_json_or_repr(spec)
    rows: Any = decoded
    if isinstance(decoded, dict):
        rows = decoded.get("requirements", decoded.get("rows", decoded.get("ledger")))
        if rows is None:
            rows = []
            for category, values in decoded.items():
                if not isinstance(values, list):
                    continue
                singular = {
                    "dimensions": "dimension_tolerance", "threads": "thread",
                    "finishes": "finish", "notes": "note", "gdt": "gdt",
                    "datums": "datum", "welds": "weld", "title_fields": "title_field",
                }.get(_canon_word(category), _canon_word(category).rstrip("s"))
                for value in values:
                    rows.append({**value, "type": value.get("type", singular)}
                                if isinstance(value, dict)
                                else {"type": singular, "value": value})
    if not isinstance(rows, list) or not rows:
        return [], {
            "ok": False,
            "reason": "spec_required",
            "error": "audit_drawing: spec must contain at least one manufacturing requirement "
                     "row; an empty audit can never release a drawing.",
        }
    normalized: list[dict] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            return [], {"ok": False, "error": f"audit_drawing: spec row {index} must be an "
                                             "object naming its type and requirement."}
        item = dict(row)
        item.setdefault("requirement_id", item.get("id") or f"req-{index}")
        item.setdefault("critical", True)
        kind = _canon_word(item.get("type") or item.get("kind"))
        aliases = {
            "dimension": "dimension_tolerance", "dimension+tolerance": "dimension_tolerance",
            "dimension_and_tolerance": "dimension_tolerance", "surface_finish": "finish",
            "gd&t": "gdt", "g_and_t": "gdt", "title": "title_field",
        }
        item["type"] = aliases.get(kind, kind)
        if item["type"] not in {
            "dimension_tolerance", "thread", "finish", "note", "gdt", "datum", "weld",
            "title_field",
        }:
            return [], {
                "ok": False,
                "error": f"audit_drawing: spec row {index} has unsupported type "
                         f"{item['type']!r}. Valid types: dimension_tolerance, thread, finish, "
                         "note, gdt, datum, weld, title_field.",
            }
        normalized.append(item)
    return normalized, None


def _audit_annotation_matches(row: dict, annotation: dict) -> bool:
    row_type = row["type"]
    kind = _canon_word(annotation.get("kind") or annotation.get("type"))

    def same(expected: Any, actual: Any) -> bool:
        if isinstance(expected, bool) or isinstance(actual, bool):
            return bool(expected) == bool(actual)
        try:
            return abs(float(expected) - float(actual)) <= 1e-9
        except (TypeError, ValueError):
            return str(expected).strip().casefold() == str(actual).strip().casefold()

    def require_fields(mapping: dict[str, tuple[str, ...]]) -> bool:
        for row_key, ann_keys in mapping.items():
            if row_key not in row:
                continue
            actual = next((annotation.get(key) for key in ann_keys
                           if annotation.get(key) is not None), None)
            if actual is None or not same(row[row_key], actual):
                return False
        return True

    if row_type == "dimension_tolerance":
        if kind not in {
            "linear_dim", "aligned_dim", "radial_dim", "diameter_dim", "angular_dim",
            "chamfer", "ordinate",
        }:
            return False
        if _canon_word(annotation.get("tol_type") or "none") == "none":
            return False
        tolerance = row.get("tolerance")
        if isinstance(tolerance, dict):
            for source, target in (("type", "tol_type"), ("upper", "tol_upper"),
                                   ("lower", "tol_lower")):
                if source in tolerance and not same(tolerance[source], annotation.get(target)):
                    return False
        if not require_fields({
            "tol_type": ("tol_type",), "tol_upper": ("tol_upper",),
            "tol_lower": ("tol_lower",), "precision": ("precision",),
            "value": ("value",),
        }):
            return False
    elif row_type == "thread":
        if kind != "hole_callout" or not (annotation.get("thread")
                                          or "M" in str(annotation.get("text") or "")):
            return False
        if not require_fields({
            "thread": ("thread",), "depth": ("depth",), "count": ("count",),
            "cbore_dia": ("cbore_dia",), "cbore_depth": ("cbore_depth",),
            "csk_dia": ("csk_dia", "cbore_dia"), "csk_depth": ("csk_depth",),
            "csk_angle": ("csk_angle",),
        }):
            return False
        if row.get("thru") is True and "thru" not in str(annotation.get("text") or "").casefold():
            return False
    elif row_type == "finish":
        if kind != "surface_finish" or not require_fields({
                "ra": ("value", "ra"), "value": ("value",), "machining": ("machining",),
                "lay": ("lay",)}):
            return False
    elif row_type == "note" and kind != "note":
        return False
    elif row_type == "gdt":
        if kind != "gdt_frame" or not require_fields({
                "symbol": ("symbol",), "tolerance": ("tol_value",),
                "modifier": ("tol_modifier",)}):
            return False
    elif row_type == "datum":
        if kind not in {"datum_feature", "datum_target"} or not require_fields({
                "label": ("label",), "target_type": ("target_type",), "size": ("size",)}):
            return False
    elif row_type == "weld":
        if kind != "weld_symbol" or not require_fields({
                "weld_type": ("weld_type",), "size": ("size",),
                "intermittent_length": ("intermittent_length",), "pitch": ("pitch",),
                "contour": ("contour",), "finish": ("finish",),
                "field_weld": ("field_weld",), "tail_text": ("tail_text",),
                "full_penetration": ("full_penetration",)}):
            return False
    expected = row.get("text")
    if expected is None:
        return True
    needle = str(expected).strip().casefold()
    haystack = " ".join(str(annotation.get(key) or "") for key in
                        ("text", "value", "thread", "label", "symbol")).casefold()
    return needle in haystack


def _audit_coverage(full: dict, rows: list[dict]) -> list[dict]:
    annotations = _all_annotations(full)
    by_id = {str(annotation.get("id")): annotation for annotation in annotations
             if annotation.get("id") is not None}
    sheet = (full.get("sheets") or [{}])[0]
    title = sheet.get("title_block") if isinstance(sheet, dict) else {}
    title = title if isinstance(title, dict) else {}
    coverage: list[dict] = []
    for row in rows:
        resolved_id = None
        evidence = None
        if row["type"] == "title_field":
            field = _canon_word(row.get("field") or row.get("name") or row.get("value"))
            expected = row.get("expected")
            actual = title.get(field)
            if actual is not None and str(actual).strip() and (
                    expected is None or str(expected).strip().casefold() in
                    str(actual).strip().casefold()):
                resolved_id = f"title_block.{field}"
                evidence = {"field": field, "value": actual}
        else:
            explicit = row.get("annotation_id")
            candidates = [by_id[str(explicit)]] if explicit is not None and str(explicit) in by_id \
                else ([] if explicit is not None else annotations)
            match = next((ann for ann in candidates if _audit_annotation_matches(row, ann)), None)
            if match:
                resolved_id = match.get("id")
                evidence = {
                    key: match.get(key) for key in
                    ("view_id", "kind", "text", "value", "tol_type", "tol_upper", "tol_lower")
                    if match.get(key) is not None
                }
        coverage.append({
            "requirement_id": row["requirement_id"],
            "type": row["type"],
            "critical": bool(row.get("critical", True)),
            "status": "covered" if resolved_id is not None else "missing",
            "annotation_id": resolved_id,
            "evidence": evidence,
        })
    return coverage


_AUDIT_TITLE_FIELDS = (
    "title", "part_number", "material", "scale", "revision", "drawn_by", "date",
    "projection", "drawing_standard", "general_tolerance",
)
_AUDIT_TITLE_ALIASES = {
    "revision": ("revision", "rev"),
    "drawn_by": ("drawn_by", "drawn"),
    "drawing_standard": ("drawing_standard", "standard"),
    "general_tolerance": ("general_tolerance", "general_tolerances"),
}
_THREAD_DESIGNATION = _re.compile(
    r"(?:\bM\d+(?:\.\d+)?\s*[x\u00d7]\s*\d+(?:\.\d+)?"
    r"(?:\s*-?\s*(?:6H|6g))?\b|"
    r"(?:#?\d+|\d+/\d+)\s*-\s*\d+\s+(?:UNC|UNF)"
    r"(?:\s*-?\s*(?:2A|2B))?\b)",
    _re.IGNORECASE,
)
_UNITS_STATEMENT = _re.compile(
    r"(?:\bunits?\b|\bmm\b|\bmillimet(?:er|re)s?\b|\binches?\b|\bIPS\b|\bMMGS\b)",
    _re.IGNORECASE,
)
_FINISH_NOTE = _re.compile(
    r"(?:\bsurface\s+finish\b|\bfinish(?:es)?\b|\broughness\b|\bRa\s*[0-9])",
    _re.IGNORECASE,
)
_AUDIT_STANDARD_CHECKS = (
    "title_block", "units_statement", "gdt", "thread_callout",
    "annotation_anchor", "surface_finish_note",
)


def _audit_title_value(title: dict, field: str) -> Any:
    for key in _AUDIT_TITLE_ALIASES.get(field, (field,)):
        value = title.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _audit_datum_ref(value: Any) -> str:
    label = str(value or "").strip().upper()
    match = _re.match(r"[A-Z]+", label)
    return match.group(0) if match else label


def _audit_gdt_refs(annotation: dict) -> list[str]:
    refs = []
    for row in (annotation, annotation.get("row2")):
        if not isinstance(row, dict):
            continue
        for datum in row.get("datums") or []:
            ref = datum.get("ref") if isinstance(datum, dict) else datum
            normalized = _audit_datum_ref(ref)
            if normalized:
                refs.append(normalized)
    return refs


def _audit_thread_text(record: dict) -> str:
    return " ".join(str(record.get(key) or "") for key in
                    ("thread", "designation", "text")).strip()


def _audit_thread_depth_stated(record: dict) -> bool:
    depth = record.get("depth")
    try:
        if depth is not None and float(depth) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if record.get("thru") is True:
        return True
    return bool(_re.search(r"(?:\bTHRU\b|\bTHROUGH\b|\bDEPTH\b|\u21a7)",
                           str(record.get("text") or ""), _re.IGNORECASE))


def _audit_standards(full: dict, rows: list[dict]) -> dict:
    """Deterministic drawing-standard checks independent of requirement coverage."""
    failures: list[dict] = []

    def fail(check: str, code: str, message: str, remedy: str, **details) -> None:
        failures.append({
            "category": "standards", "check": check, "code": code,
            "message": message, "remedy": remedy, **details,
        })

    sheet = (full.get("sheets") or [{}])[0]
    title = sheet.get("title_block") if isinstance(sheet, dict) else {}
    title = title if isinstance(title, dict) else {}
    for field in _AUDIT_TITLE_FIELDS:
        if _audit_title_value(title, field) is None:
            fail(
                "title_block", "title_field_missing",
                f"title block field {field!r} is missing or empty",
                f"set_titleblock(fields={{\"{field}\": \"<controlled value>\"}})",
                field=field,
            )

    annotations = _all_annotations(full)
    notes = [str(annotation.get("text") or "") for annotation in annotations
             if _canon_word(annotation.get("kind") or annotation.get("type")) == "note"]
    tolerance_text = str(_audit_title_value(title, "general_tolerance") or "")
    if not _UNITS_STATEMENT.search(" ".join([tolerance_text, *notes])):
        fail(
            "units_statement", "units_statement_missing",
            "no units statement appears in general_tolerance or a drawing note",
            "set_titleblock(fields={\"general_tolerance\": "
            "\"UNLESS OTHERWISE SPECIFIED: DIMENSIONS ARE IN MM; ...\"})",
            field="general_tolerance",
        )

    datum_positions: dict[str, int] = {}
    for index, annotation in enumerate(annotations):
        kind = _canon_word(annotation.get("kind") or annotation.get("type"))
        if kind in {"datum_feature", "datum_target"}:
            label = _audit_datum_ref(annotation.get("label"))
            if label and label not in datum_positions:
                datum_positions[label] = index
    for index, annotation in enumerate(annotations):
        if _canon_word(annotation.get("kind") or annotation.get("type")) != "gdt_frame":
            continue
        annotation_id = annotation.get("id")
        view_id = annotation.get("view_id")
        row2_present = "row2" in annotation
        row2 = annotation.get("row2")
        top_shaped = bool(
            str(annotation.get("symbol") or "").strip()
            and str(annotation.get("tol_value") or "").strip()
            and isinstance(annotation.get("datums", []), list)
            and all(isinstance(datum, dict) and str(datum.get("ref") or "").strip()
                    for datum in annotation.get("datums", []))
        )
        second_shaped = bool(
            isinstance(row2, dict)
            and str(row2.get("tol_value") or "").strip()
            and isinstance(row2.get("datums", []), list)
            and all(isinstance(datum, dict) and str(datum.get("ref") or "").strip()
                    for datum in row2.get("datums", []))
        )
        if row2_present and not (top_shaped and second_shaped):
            fail(
                "gdt", "gdt_composite_malformed",
                f"composite GD&T frame {annotation_id!r} does not contain two shaped rows",
                f"edit_drawing_annotation(view_id={view_id!r}, "
                f"annotation_id={annotation_id!r}, delete=true), then "
                "add_gdt(..., row2={\"tolerance\":...,\"datums\":[...]})",
                annotation_id=annotation_id, view_id=view_id,
            )
        for datum in sorted(set(_audit_gdt_refs(annotation))):
            if datum not in datum_positions:
                fail(
                    "gdt", "gdt_datum_unresolved",
                    f"GD&T frame {annotation_id!r} cites datum {datum!r}, "
                    "but no such datum annotation exists",
                    f"add_datum(view_id={view_id!r}, label={datum!r}, a=[...], pos=[...]) "
                    "before the feature-control frame",
                    annotation_id=annotation_id, view_id=view_id, datum=datum,
                )
            elif datum_positions[datum] >= index:
                fail(
                    "gdt", "gdt_datum_order",
                    f"GD&T frame {annotation_id!r} cites datum {datum!r} before its annotation",
                    f"edit_drawing_annotation(view_id={view_id!r}, "
                    f"annotation_id={annotation_id!r}, delete=true), then recreate it with "
                    "add_gdt(...) after add_datum(...)",
                    annotation_id=annotation_id, view_id=view_id, datum=datum,
                )

    by_id = {str(annotation.get("id")): annotation for annotation in annotations
             if annotation.get("id") is not None}
    thread_records: list[tuple[str, dict]] = [
        ("spec", row) for row in rows if row.get("type") == "thread"
    ]
    included_annotations: set[str] = set()
    for row in rows:
        if row.get("type") != "thread" or row.get("annotation_id") is None:
            continue
        annotation = by_id.get(str(row["annotation_id"]))
        if annotation is not None:
            thread_records.append(("annotation", annotation))
            included_annotations.add(str(annotation.get("id")))
    for annotation in annotations:
        kind = _canon_word(annotation.get("kind") or annotation.get("type"))
        tapped = _canon_word(annotation.get("hole_type")) == "tapped"
        if kind == "hole_callout" and (
                tapped or annotation.get("thread") or
                _THREAD_DESIGNATION.search(str(annotation.get("text") or ""))):
            annotation_id = str(annotation.get("id"))
            if annotation_id not in included_annotations:
                thread_records.append(("annotation", annotation))
                included_annotations.add(annotation_id)
    seen_thread_findings: set[tuple[str, str, str]] = set()
    for source_kind, record in thread_records:
        annotation_id = record.get("id") if source_kind == "annotation" else None
        requirement_id = record.get("requirement_id") if source_kind == "spec" else None
        identity = str(annotation_id or requirement_id or "")
        designation = _audit_thread_text(record)
        if not _THREAD_DESIGNATION.search(designation):
            key = (source_kind, identity, "thread_designation_invalid")
            if key not in seen_thread_findings:
                seen_thread_findings.add(key)
                remedy = (
                    f"edit_drawing_annotation(view_id={record.get('view_id')!r}, "
                    f"annotation_id={annotation_id!r}, delete=true), then "
                    "add_hole_callout(..., thread='M6x1.0' or '1/4-20 UNC-2B', "
                    "depth=... or thru=true)"
                    if source_kind == "annotation" else
                    "mfg_plan_upsert(..., plan=<correct this thread row to a full "
                    "M<d>x<pitch> or <size>-<TPI> UNC/UNF designation>)"
                )
                fail(
                    "thread_callout", "thread_designation_invalid",
                    f"{source_kind} thread row {identity!r} has no valid Metric/Unified "
                    f"designation: {designation!r}",
                    remedy, source=source_kind, annotation_id=annotation_id,
                    requirement_id=requirement_id,
                )
        if not _audit_thread_depth_stated(record):
            key = (source_kind, identity, "thread_extent_missing")
            if key not in seen_thread_findings:
                seen_thread_findings.add(key)
                fail(
                    "thread_callout", "thread_extent_missing",
                    f"{source_kind} thread row {identity!r} states neither depth nor THRU",
                    (f"edit_drawing_annotation(view_id={record.get('view_id')!r}, "
                     f"annotation_id={annotation_id!r}, delete=true), then "
                     "add_hole_callout(..., depth=... or thru=true)"
                     if source_kind == "annotation" else
                     "mfg_plan_upsert(..., plan=<set depth or thru=true on this thread row>)"),
                    source=source_kind, annotation_id=annotation_id,
                    requirement_id=requirement_id,
                )

    view_ids = {str(view.get("view_id") or view.get("id")) for view in _views_of(full)
                if view.get("view_id") or view.get("id")}
    for annotation in annotations:
        refs = []
        for key in ("_declared_view_id", "anchor_view_id", "view_ref", "source_view_id"):
            if annotation.get(key):
                refs.append(str(annotation[key]))
        anchor = annotation.get("anchor")
        if isinstance(anchor, dict):
            for key in ("view_id", "view_ref", "source_view_id"):
                if anchor.get(key):
                    refs.append(str(anchor[key]))
        missing_refs = sorted({ref for ref in refs if ref not in view_ids})
        if missing_refs:
            fail(
                "annotation_anchor", "annotation_dangling",
                f"annotation {annotation.get('id')!r} refers to missing view(s) "
                + ", ".join(missing_refs),
                f"edit_drawing_annotation(view_id={annotation.get('view_id')!r}, "
                f"annotation_id={annotation.get('id')!r}, delete=true), then recreate it "
                "on an existing view",
                annotation_id=annotation.get("id"), view_id=annotation.get("view_id"),
                missing_view_ids=missing_refs,
            )

    finish_symbols = [annotation for annotation in annotations
                      if _canon_word(annotation.get("kind") or annotation.get("type"))
                      == "surface_finish"]
    finish_text = " ".join([
        str(title.get("finish") or ""),
        str(_audit_title_value(title, "general_tolerance") or ""),
        *notes,
    ])
    if finish_symbols and not _FINISH_NOTE.search(finish_text):
        fail(
            "surface_finish_note", "finish_note_missing",
            "surface-finish symbols exist, but title block/notes state no general or "
            "specific finish requirement",
            "set_titleblock(fields={\"finish\":\"RA ...\"}) or "
            "add_note(..., text=\"SURFACE FINISH ...\")",
            annotation_ids=[annotation.get("id") for annotation in finish_symbols],
        )

    checks = []
    for check_name in _AUDIT_STANDARD_CHECKS:
        count = sum(1 for finding in failures if finding["check"] == check_name)
        checks.append({
            "check": check_name, "status": "fail" if count else "pass",
            "failure_count": count,
        })
    return {
        "status": "fail" if failures else "pass",
        "failure_count": len(failures), "checks": checks, "failures": failures,
    }


def register_cad_drawing_tools(mcp: FastMCP) -> None:
    """CAD 2D-drawing MCP tools (split out of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""


    # ---------------------------------------------------------------- drawings & parts

    # make_drawing has exactly ONE source-document slot and ONE paper-size slot, so a spelling
    # of "the 3D model I am drawing" or of "the paper" cannot be misrouted HERE. `source_doc` and
    # `template` are the two the live run wrote.
    #
    # `source_doc_id` is declared for a REGRESSION reason, not a new one: it used to reach doc_id
    # through the affix rule alone, but with `source_doc` now a real parameter that rule sees TWO
    # candidates ("sourcedocid" ends with "docid" AND starts with "sourcedoc"), and
    # resolve_unknown refuses on ambiguity — which would have silently un-fixed a spelling that
    # worked before this change. An exact parameter name is matched before the affix rule, so
    # declaring it restores it. The wider family (source / from_doc / model_doc / part_doc) is
    # tool-scoped in services/agent/arg_reconciliation.py::TOOL_ARG_SYNONYMS, which introduces no
    # such ambiguity because the synonym rule runs before the affix rule.

    @mcp.tool()
    def make_drawing(doc_id: Optional[str] = None, views: Optional[list] = None,
                     sections: Optional[list] = None, sheet: Optional[str] = None,
                     hole_callouts: bool = True, model_items: Optional[Any] = None,
                     title: Optional[Any] = None, template: Optional[str] = None,
                     source_doc: Optional[str] = None,
                     source_doc_id: Optional[str] = None,
                     scale: Optional[Any] = None, orientation: Optional[Any] = None,
                     name: Optional[str] = None, projection: Optional[str] = None) -> str:
        """Create a 2D engineering drawing of the current part in ONE shot, then refine it.

        Builds a professional sheet: ISO 5457 frame with zones, ISO 7200 title block, standard
        views laid out on the projection lattice at ONE standard scale, hidden lines in the
        orthographic views, centre marks / centre lines / bolt circles, non-redundant overall
        dimensions and hole callouts. Returns the new drawing_doc_id.

        views   : list of orientations, e.g. ["front","top","right","iso"] (default this set).
        sections: list of auto-section requests, each a dict
                  {"parent":"front","axis":"vertical"|"horizontal","mode":"full"|"half",
                   "reverse":false,"label":"A"} — cuts the parent view through its centre, no
                  sheet-mm math needed.
        sheet   : paper size "A4".."A0" (ISO) or "A".."E" (ANSI) — default A3, landscape; add
                  " portrait" (or a P suffix, "A4P") for portrait paper. Also accepted as
                  sheet_size= / paper_size= / template= when the value IS a size; a template
                  FILE has no meaning here (there is no border/titleblock template concept).
        projection: "third" (ASME, default: top view above front, right view to its right) or
                  "first" (ISO E: top below, right view to the left). Printed in the title block
                  with its ISO 5456-2 symbol. Re-lay later with arrange_drawing().
        model_items: true (default) runs the engine's auto-dimension pass on the ortho views —
                  overall width/height plus one "N× ⌀d" per distinct hole diameter. A switch
                  only: individual dimensions are placed with add_dimension.
        title   : the drawing title as a plain string ("90x50x12 Plate"), or the full
                  title-block fields dict {"title":..,"material":..,"scale":"1:1","rev":"A",...}.
        scale   : the GEOMETRIC view scale applied to every view — 1.0 (1:1), 0.5 or "1:2"
                  (half size), 2 or "2:1". Omitted, the engine auto-scales the part to fit the
                  sheet. This is NOT the title block's printed SCALE cell (that is a text field:
                  title={"scale":"1:1"}); setting scale= rewrites that text to match.
        name    : a name for the DRAWING DOCUMENT ("40mm_cube_drawing"). Defaults to
                  "<part> - Drawing". The title BLOCK's printed title is `title`, not this.
        orientation: "portrait"/"landscape" sets the SHEET orientation; a VIEW orientation
                  ("front" / ["front","top"]) is read as a synonym of views=.

        source_doc: a synonym of doc_id (the 3D model to draw) — source_doc_id / model_doc /
                  from_doc / part_doc are understood too.
        template: a synonym of sheet, and ONLY when its value is a paper size. There is no
                  drawing-template file in Chudware; a non-size value is refused, not guessed.

        Returns `views` as {view_id: orientation} and `view_entities` as {view_id: count} —
        how many projected polylines + circles that view really got, so a view that came back
        blank shows in the reply (and in `warnings`) instead of only on the sheet.

        AFTER making it: call review_drawing(drawing_doc_id) to SEE the sheet, then refine with
        add_dimension / add_gdt / add_datum / add_surface_finish / add_section_view /
        add_detail_view / set_view (move/scale) / set_titleblock, reviewing as you go. Deliver
        with export_drawing('pdf'|'dxf', drawing_doc_id).
        """
        try:
            # LIVE-DIALECT gap: source_doc= / template= died as unknown_argument before the
            # engine was ever reached — see the argument-name dialect block above.
            notes: list = []
            bad = _template_verdict(template)
            if bad:
                return _dumps(bad)
            for alias_name, alias in (("source_doc", source_doc),
                                      ("source_doc_id", source_doc_id),
                                      ("template", template)):
                slot = "sheet" if alias_name == "template" else "doc_id"
                current = sheet if slot == "sheet" else doc_id
                value, note, bad = _fold_alias_param(slot, current, alias_name, alias)
                if bad:
                    return _dumps(bad)
                if note:
                    notes.append(note)
                if slot == "sheet":
                    sheet = value
                else:
                    doc_id = value
            # `model_items` reaches the engine as a bool; bool("none") is True, so an
            # unrecognised word must be refused rather than silently switched ON.
            model_items, bad = _canon_model_items(True if model_items is None else model_items)
            if bad:
                return _dumps(bad)
            # An unknown paper size used to fall back to A3 SILENTLY (drawing_session.cpp:1797).
            if sheet is not None:
                sheet, bad = _canon_sheet_size(sheet)
                if bad:
                    return _dumps(bad)
            # orientation="portrait"/"landscape" is the SHEET's orientation (a size suffix in the
            # engine: "A4P" is A4 portrait); "front"/["front","top"] is a VIEW orientation, which
            # is what views= takes, so that reading is folded onto views instead.
            if orientation is not None:
                word = _canon_word(orientation) if not isinstance(orientation, (list, tuple)) else ""
                if word in ("portrait", "landscape", "horizontal", "vertical"):
                    cur = sheet or "A3"
                    base = cur[:-1] if cur.endswith("P") and cur[:-1] in _SHEET_SIZES else cur
                    sheet = base + "P" if word in ("portrait", "vertical") else base
                    notes.append(f"orientation={orientation!r} applied to the sheet: {sheet}")
                    orientation = None
            if orientation is not None:
                as_views, bad = _orientation_verdict(orientation)
                if bad:
                    return _dumps(bad)
                if views:
                    have = [_canon_word(v) for v in (views if isinstance(views, list) else [views])]
                    if sorted(have) != sorted(as_views):
                        return _dumps({
                            "ok": False,
                            "error": f"make_drawing: orientation={orientation!r} and "
                                     f"views={views!r} both name which views to draw, and they "
                                     f"disagree. Pass one — views={as_views!r} draws exactly "
                                     f"those. Nothing was created."})
                    notes.append(f"orientation={orientation!r} repeats views={views!r}")
                else:
                    views = as_views
                    notes.append(f"orientation={orientation!r} read as views={as_views!r} (a "
                                 f"VIEW orientation)")
            # LIVE-DIALECT gap: scale=1.0 died as unknown_argument (twice). A real geometric view
            # scale exists — see the drawing/view-scale block above.
            want_scale = None
            if scale is not None:
                want_scale, bad = _canon_view_scale(scale)
                if bad:
                    return _dumps(bad)
            d = _doc(doc_id)
            tree = cb.call("doc_tree", {"doc_id": d})
            tree_node = tree.get("tree") or {}
            part_name = tree_node.get("name", "") if isinstance(tree_node, dict) else ""

            # D2: reject make_drawing on a doc that has no geometry so we don't silently
            # draw a stale "last-used" part.  We consider the doc empty when it has no
            # features in the tree AND no tessellated bodies (body_count==0 or missing).
            features = tree_node.get("features", []) if isinstance(tree_node, dict) else []
            body_count = tree.get("body_count", tree_node.get("body_count", None))
            if body_count is None:
                # Some engine versions put body_count at the top level of doc_tree,
                # others bury it inside the tree node.  If we can't read it, check features.
                body_count = len(features)
            if not features and not body_count:
                return _dumps({"ok": False,
                               "error": f"make_drawing: document {d!r} has no geometry to draw"})

            # LIVE-DIALECT guard: `views`/`sections` used to forward a stringified list
            # untouched to the engine (a contentless engine-side rejection); `title` used to
            # SILENTLY drop a stringified dict via the bare isinstance() check (no error at
            # all — the class-5 "reports success but ignored the arg" failure). Coerce all three.
            views = require_list(views, "views", 'a list of view names, e.g. ["front","top"]') if views else None
            sections = require_list(sections, "sections", "a list of section-request dicts") if sections else None
            if sections:
                # Same discriminator guard as add_section_view: the engine's own auto-section
                # (core/doc/drawing_session.cpp:2048) is the identical
                # `axis == "horizontal" ? … : vertical` fall-through, so an unrecognised axis
                # here also cuts vertically without a word of complaint. Canonicalise in
                # place; reject before the drawing is created, so a bad spec builds nothing.
                norm = []
                for i, sec in enumerate(sections):
                    if not isinstance(sec, dict):
                        norm.append(sec)
                        continue
                    sec = dict(sec)
                    for key, canon in (("axis", _canon_section_axis),
                                       ("mode", _canon_section_mode)):
                        if key not in sec or sec[key] is None:
                            continue
                        val, bad = canon(sec[key])
                        if bad:
                            bad["error"] = f"sections[{i}]: {bad['error']}"
                            return _dumps(bad)
                        sec[key] = val
                    norm.append(sec)
                sections = norm
            # LIVE-DIALECT gap (2026-07-29): make_drawing(title="90x50x12 Plate") was refused
            # ("title must be a title-block fields dict"). The parameter is literally named
            # `title`, so a bare string is the most natural call there is — and the title block
            # HAS a "title" field (the key the engine stores and set_titleblock reads back:
            # core/doc/drawing_session.cpp:1758-1768). Promote a plain string into that field.
            # A dict still goes through require_mapping unchanged, and a genuinely malformed
            # value (a list, a number) is still refused with the teaching error.
            if title is not None:
                decoded = coerce_json_or_repr(title)
                if isinstance(decoded, str) and decoded.strip():
                    title = {"title": decoded.strip()}
            title = require_mapping(title, "title",
                                    'a title-block fields dict (or just the title as a string, '
                                    'e.g. title="90x50x12 Plate")') if title else None
            spec = {"views": views or ["front", "top", "right", "iso"],
                    "model_items": model_items, "hole_callouts": bool(hole_callouts)}
            if sections:
                spec["sections"] = sections
            if sheet:
                spec["sheet"] = sheet
            if projection is not None:
                proj = _canon_word(projection).replace("_angle", "").replace("angle", "")
                if proj in ("1st", "iso_e", "e", "european"):
                    proj = "first"
                if proj in ("3rd", "asme", "american"):
                    proj = "third"
                if proj not in ("first", "third"):
                    return _dumps({"ok": False,
                                   "error": f"make_drawing: projection={projection!r} must be "
                                            f"'third' (ASME) or 'first' (ISO E). Nothing was "
                                            f"created."})
                spec["projection"] = proj
            if want_scale is not None:
                spec["scale"] = want_scale
            # LIVE-DIALECT gap: name="40mm_cube_drawing" died as unknown_argument although
            # drawing_make already reads spec["name"] and hands it to drawing_new as the drawing
            # DOCUMENT's name (core/doc/drawing_session.cpp:1788 -> :813); with none it composes
            # "<part> - Drawing". Distinct from the title BLOCK's printed title, which is `title`.
            if name is not None and str(name).strip():
                spec["name"] = str(name).strip()
            # SILENT-DROP gap (2026-07-30): this dict went straight to drawing_make, which
            # merges it into the sheet's title_block verbatim (drawing_session.cpp:2068 ->
            # :1759), so make_drawing(title={"part_name":"Plate"}) reported ok and stored a key
            # neither the renderer (session.cpp:7106-7117) nor the exporter
            # (export2d.cpp:205-207) draws — the part name simply never appeared. The
            # vocabulary set_titleblock has enforced since 2026-07-29 now guards both doors.
            tb, tb_notes, bad = _canon_titleblock_fields(
                dict(title) if isinstance(title, dict) else {}, "make_drawing(title=…)")
            if bad:
                return _dumps(bad)
            notes.extend(tb_notes)
            if part_name and "title" not in tb:
                tb["title"] = part_name
            # An explicit geometric scale and an explicit printed SCALE cell that disagree are a
            # contradiction we must not resolve by picking one: the sheet would state a scale its
            # geometry does not have. Equal is fine (and noted).
            if want_scale is not None:
                printed = tb.get("scale")
                if isinstance(printed, str) and printed.strip():
                    as_number, _bad = _canon_view_scale(printed)
                    if as_number is None or abs(as_number - want_scale) > 1e-9:
                        return _dumps({
                            "ok": False,
                            "error": f"make_drawing: scale={scale!r} draws the views at "
                                     f"{_scale_text(want_scale)}, but the title block was told "
                                     f"to PRINT {printed!r}. A sheet whose SCALE cell "
                                     f"contradicts its geometry is a wrong drawing that looks "
                                     f"right, so nothing was created — pass one, or make them "
                                     f"agree."})
                    notes.append(f"title scale {printed!r} agrees with scale={want_scale}")
                tb["scale"] = _scale_text(want_scale)
            if tb:
                spec["title"] = _titleblock_payload(tb)
            r = cb.call("drawing_make", {"source_doc_id": d, "spec": spec})
            drawing_doc_id = r.get("doc_id")
            # The engine lays every view out at the explicit scale before dimensioning, so
            # offsets and alignment are right at that scale; read back what was really drawn.
            scale_warnings: list = []
            view_scales: list = []
            if want_scale is not None and drawing_doc_id:
                view_scales, scale_warnings = _scale_readback(drawing_doc_id, want_scale, notes)
                if want_scale not in _STANDARD_SCALES:
                    notes.append(f"scale={want_scale} is not one of the ISO 5455 / ASME Y14.1 "
                                 f"preferred scales (…2:1, 1:1, 1:2, 1:5…) — applied as asked")
            # D7: include the view_id ↔ orientation map so callers can pass either a view_id
            # or an orientation string to add_gdt / add_datum / add_dimension without having
            # to call drawing_get first.
            # Beside it, `view_entities` counts the projected polylines + circles each view
            # really got: "a view that renders nothing" is the top drawing defect class, and a
            # blank view is otherwise indistinguishable from a good one until someone looks at
            # the sheet. A zero also earns a warning, because a count is easy to skim past.
            view_map: dict = {}
            view_entities: dict = {}
            blank: list = []
            sheet_size = None
            drawing_name = None
            if drawing_doc_id:
                try:
                    full = cb.call("drawing_get", {"doc_id": drawing_doc_id})
                    drawing_name = (full.get("tree") or {}).get("name")
                    for sh in full.get("sheets", []):
                        if sheet_size is None:
                            sheet_size = sh.get("size")
                        for v in sh.get("views", []):
                            vid = v.get("view_id") or v.get("id")
                            ori = v.get("orientation") or v.get("type", "")
                            if v.get("type") == "table_overlay":
                                continue
                            if not vid:
                                continue
                            view_map[vid] = ori
                            n = len(v.get("visible") or []) + len(v.get("arcs") or [])
                            view_entities[vid] = n
                            if not n:
                                blank.append(f"view {vid} ({ori}) projected no geometry and "
                                             f"will render blank"
                                             + (f": {v['error']}" if v.get("error") else ""))
                except Exception:  # noqa: BLE001
                    pass
            # Report the RESOLVED sheet + auto-dimension switch + drawing name + view scale, not
            # just ok:true: an alias that landed in the wrong slot is otherwise invisible in the
            # reply. `name` is the drawing document's own name as the engine stored it.
            out = {"ok": True, "source_doc_id": d,
                   "drawing_doc_id": drawing_doc_id, "name": drawing_name,
                   "part_name": part_name,
                   "sheet": sheet_size, "model_items": model_items,
                   "views": view_map or None, "view_entities": view_entities or None,
                   "next_actions": [
                       "review_drawing(drawing_doc_id)  # SEE the drawing, then refine",
                       "add_gdt / add_dimension / set_view / set_titleblock  # refine",
                       "export_drawing('pdf', drawing_doc_id)  # or 'dxf'"]}
            if want_scale is not None:
                out["scale"] = want_scale
                out["scale_text"] = _scale_text(want_scale)
                out["view_scales"] = view_scales
                if scale_warnings:
                    out["warnings"] = scale_warnings
            if blank:
                out.setdefault("warnings", []).extend(blank)
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # export_drawing(fmt, doc_id, path) has ONE output-name slot, so a spelling of "what to
    # call the file" cannot be misrouted HERE. `out_path` / `file_path` already reach `path`
    # through the shared affix rule; `filename` — the universal spelling, and the one the live
    # call wrote — does not: it is neither a prefix nor a suffix of "path".
    _DRAWING_FORMATS = ("pdf", "dxf", "dwg", "svg")
    _DRAWING_COMPANIONS = ("dxf", "dwg", "svg")

    def _drawing_out_path(path: str, fmt: str) -> tuple[Optional[str], Optional[dict]]:
        """Return an absolute matching output path, or an honest extension refusal."""
        out = os.path.abspath(os.path.expanduser(path))
        ext = os.path.splitext(out)[1].lower().lstrip(".")
        if ext == fmt:
            return out, None
        if ext in _DRAWING_FORMATS:
            return None, {
                "ok": False,
                "error": f"export_drawing: fmt={fmt!r} contradicts the {ext!r} extension on "
                         f"{os.path.basename(out)!r}. Writing {fmt.upper()} bytes to a .{ext} "
                         f"file produces a file nothing can open, so nothing was written — "
                         f"re-issue with a matching pair, e.g. fmt={fmt!r} with "
                         f"{os.path.splitext(os.path.basename(out))[0]}.{fmt}, or drop fmt and "
                         f"let the extension choose it.",
            }
        return out + "." + fmt, None

    @mcp.tool()
    def export_drawing(fmt: Optional[str] = None, doc_id: Optional[str] = None,
                       path: Optional[str] = None, filename: Optional[str] = None,
                       sheet: Optional[str] = None,
                       companions: Optional[list] = None) -> str:
        """Export a 2D DRAWING document to editable vector deliverables.

        fmt    : "pdf" (fixed-size engineering sheet), "dxf" (CAD interchange), "dwg" (AutoCAD
                 R2000 binary written natively; same layers/colors/text as DXF, hidden lines
                 on HIDDEN with the DASHED linetype, centre lines CENTER), or "svg" (editable
                 paths and real text). Omitted, it is taken from the output extension, else
                 pdf. Pass the drawing_doc_id from make_drawing as doc_id. Call
                 review_drawing after the final sheet mutation first.
        path   : the file to write (also accepted as filename= / out_path= / file_path=). An
                 extension is appended when missing and never doubled. A path whose extension
                 contradicts fmt is refused rather than written.
        sheet  : omitted uses the active sheet; "all" exports every sheet; otherwise pass an
                 exact stable sheet id such as "s2". Numeric indices are refused as ambiguous.
        companions: only with fmt="pdf", pass any of ["dxf", "dwg", "svg"] to write editable
                 siblings beside the PDF in the same call. The result's paths lists every file.
        Returns every output path, entity/annotation counts and the fixed paper size. export_part
        is for 3D B-rep/mesh only; a drawing is 2D and uses this exporter.
        """
        try:
            notes: list = []
            path, note, bad = _fold_alias_param("path", path, "filename", filename)
            if bad:
                return _dumps(bad)
            if note:
                notes.append(note)
            d = _doc(doc_id)
            if fmt is None:
                # No format given: the extension the caller wrote IS the format they meant.
                ext = os.path.splitext(str(path or ""))[1].lower().lstrip(".")
                fmt = ext if ext in _DRAWING_FORMATS else "pdf"
                if ext in _DRAWING_FORMATS:
                    notes.append(f"read the format {fmt!r} from the output extension")
            fmt = str(fmt).lower().lstrip(".")
            if fmt not in _DRAWING_FORMATS:
                return _dumps({
                    "ok": False,
                    "error": f"fmt must be 'pdf', 'dxf', 'dwg', or 'svg', got {fmt!r}",
                })

            if companions is None:
                companion_formats: list[str] = []
            elif not isinstance(companions, list):
                return _dumps({
                    "ok": False,
                    "error": "export_drawing: companions must be a list containing 'dxf', "
                             "'dwg' and/or 'svg'. Nothing was exported.",
                })
            else:
                companion_formats = []
                for item in companions:
                    token = str(item).lower().lstrip(".")
                    if token not in _DRAWING_COMPANIONS:
                        return _dumps({
                            "ok": False,
                            "error": "export_drawing: companions entries must be 'dxf', 'dwg' "
                                     f"or 'svg', got {item!r}. Nothing was exported.",
                        })
                    if token not in companion_formats:
                        companion_formats.append(token)
            if companion_formats and fmt != "pdf":
                return _dumps({
                    "ok": False,
                    "error": "export_drawing: companions are editable siblings of a PDF export; "
                             "set fmt='pdf'. Nothing was exported.",
                })

            # A caller-supplied relative path resolves against the daemon's cwd, so resolve it
            # before the payload crosses the process boundary.
            if path:
                out_path, bad = _drawing_out_path(path, fmt)
                if bad:
                    return _dumps(bad)
            else:
                out_path = _tmp("." + fmt)

            method = "export_drawing"
            sheet_value = None
            if sheet is not None:
                if not isinstance(sheet, str) or not (
                        sheet.strip().lower() == "all"
                        or _re.fullmatch(r"s[1-9][0-9]*", sheet.strip(), _re.IGNORECASE)):
                    return _dumps({"ok": False, "error": "export_drawing: sheet must be "
                                   "\"all\" or an exact sheet id like \"s2\"; numeric indices "
                                   "are ambiguous. Nothing was exported."})
                sheet_value = sheet.strip().lower()
                method = "drawing_export"

            def run_export(export_fmt: str, export_path: str) -> tuple[dict, Optional[dict]]:
                params = {"doc_id": d, "path": export_path, "fmt": export_fmt}
                if sheet_value is not None:
                    params["sheet"] = sheet_value
                result, unsupported = _drawing_rpc("export_drawing", method, params)
                return result or {}, unsupported

            def result_paths(result: dict, fallback: str) -> list[str]:
                values = result.get("paths")
                if isinstance(values, list) and values:
                    return [str(value) for value in values]
                return [str(result.get("path") or fallback)]

            primary, unsupported = run_export(fmt, out_path)
            if unsupported:
                return _dumps(unsupported)
            paths = result_paths(primary, out_path)

            stem = os.path.splitext(out_path)[0]
            for companion_fmt in companion_formats:
                companion_path = stem + "." + companion_fmt
                companion, unsupported = run_export(companion_fmt, companion_path)
                if unsupported:
                    unsupported["paths"] = paths
                    unsupported["partial"] = bool(paths)
                    return _dumps(unsupported)
                paths.extend(result_paths(companion, companion_path))

            out = {"ok": True, "doc_id": d, "format": fmt,
                   "path": paths[0], "paths": paths,
                   "companions": companion_formats,
                   "visible": primary.get("visible"), "hidden": primary.get("hidden"),
                   "annotations": primary.get("annotations"),
                   "entities": primary.get("entities"),
                   "fixed_page": primary.get("fixed_page"),
                   "sheet_mm": primary.get("sheet_mm"),
                   "sheet_mm_all": primary.get("sheet_mm_all"),
                   "page_count": primary.get("page_count"),
                   "sheet": primary.get("sheet", sheet),
                   "exported_sheets": primary.get("sheets",
                                                   primary.get("exported_sheets"))}
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def review_drawing(doc_id: Optional[str] = None,
                       audit_result: Optional[Any] = None) -> Any:
        """Render and inspect a drawing as pixels plus exact sheet-mm metadata.

        This is a LAYOUT review, not a manufacturing-completeness gate.  The JSON caption
        reports ``layout_clean``.  ``manufacturing_ready`` remains for compatibility but is
        false unless the supplied current audit_drawing result has complete requirement
        coverage, zero standards failures, and the layout is clean.  Never release a
        nominal-only sheet from review_drawing alone.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
        if not rd.get("ok") or not os.path.exists(png):
            raise ValueError(f"Drawing render failed: {rd.get('reason', 'not a drawing doc?')}")
        full = cb.call("drawing_get", {"doc_id": d})
        return [Image(path=png), _dumps(_drawing_review_summary(full, rd, audit_result))]

    @mcp.tool()
    def audit_drawing(doc_id: str, drawing_id: str, spec: Any) -> str:
        """Audit manufacturing coverage and deterministic drawing-standard compliance.

        ``spec`` is REQUIRED and contains requirement rows (a list, or
        ``{"requirements":[...]}``).  Each row names type=dimension_tolerance|thread|finish|
        note|gdt|datum|weld|title_field, an optional annotation_id, and critical=true by
        default.  ``coverage_matrix`` resolves those rows; ``standards`` independently checks
        the controlled title block/units, GD&T datum order and composite shape, thread grammar
        and extent, dangling annotation anchors, and inverse surface-finish-note consistency.
        release_ready requires complete critical coverage, clean layout, and zero standards
        failures.
        """
        try:
            drawing_doc_id = _doc(drawing_id)
            rows, bad = _audit_spec_rows(spec)
            if bad:
                return _dumps(bad)
            full = cb.call("drawing_get", {"doc_id": drawing_doc_id})
            source = full.get("source_doc_id") or full.get("source")
            if source and str(source) != str(doc_id):
                return _dumps({
                    "ok": False,
                    "reason": "source_mismatch",
                    "error": f"audit_drawing: drawing {drawing_doc_id!r} projects "
                             f"{source!r}, not doc_id={doc_id!r}. Nothing was audited.",
                })
            coverage = _audit_coverage(full, rows)
            missing = [row for row in coverage if row["status"] == "missing"]
            missing_critical = [row for row in missing if row["critical"]]
            standards = _audit_standards(full, rows)
            standards_failures = standards["failures"]
            layout = _drawing_review_summary(full, {}, None)
            layout_warnings = layout["warnings"]
            release_ready = (
                not missing_critical
                and not standards_failures
                and not layout_warnings
            )
            return _dumps({
                "ok": True,
                "doc_id": doc_id,
                "drawing_id": drawing_doc_id,
                "drawing_revision": full.get("revision"),
                "coverage_matrix": coverage,
                "standards": standards,
                "standards_failures": standards_failures,
                "covered": len(coverage) - len(missing),
                "total": len(coverage),
                "missing": missing,
                "missing_critical": missing_critical,
                "layout_warnings": layout_warnings,
                "layout_clean": not layout_warnings,
                "verdict": "release_ready" if release_ready else "not_release_ready",
            })
        except Exception as exc:  # noqa: BLE001
            return _err(exc)



    @mcp.tool()
    def add_drawing_view(orientation: str = "front", doc_id: Optional[str] = None,
                         view_type: str = "projected", x: float = 150.0, y: float = 150.0,
                         show_hidden: bool = False) -> str:
        """Add one projected view. orientation is front|back|top|bottom|left|right|iso;
        view_type is base|projected|iso. ``show_hidden`` controls hidden-line display.
        x,y are the view centre in sheet millimetres."""
        try:
            orientation = _canon_word(orientation)
            view_type = _canon_word(view_type)
            if orientation not in _VIEW_ORIENTATIONS:
                return _dumps({"ok": False, "error": "add_drawing_view: orientation must be "
                               + ", ".join(_VIEW_ORIENTATIONS) + ". Nothing was added."})
            if view_type not in {"base", "projected", "iso"}:
                return _dumps({"ok": False, "error": "add_drawing_view: view_type must be "
                               "base|projected|iso. Nothing was added."})
            d = _doc(doc_id)
            r = cb.call("drawing_add_view", {
                "doc_id": d, "type": view_type, "orientation": orientation,
                "position": [x, y], "show_hidden": bool(show_hidden),
            })
            vid = r.get("view_id")
            out = {"ok": True, "view_id": vid, "orientation": orientation,
                   "show_hidden": bool(show_hidden)}
            if vid:
                report, warnings = _layout_report(cb.call("drawing_get", {"doc_id": d}), vid)
                out.update(report)
                if warnings:
                    out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_section_view(parent: str = "front", axis: str = "vertical", mode: str = "full",
                         reverse: bool = False, label: Optional[str] = None,
                         doc_id: Optional[str] = None, plane: Optional[Any] = None,
                         offset: float = 0.0, depth: Optional[float] = None,
                         multipoint: Optional[Any] = None, show_hidden: bool = False,
                         position: Optional[Any] = None) -> str:
        """Add a section with an explicit world plane or a sheet cut line.

        ``plane`` is {"normal":[x,y,z],"point":[x,y,z]}; offset moves it along its normal.
        Without a plane, axis=vertical|horizontal creates a centre cut shifted by ``offset``
        sheet mm. ``multipoint`` replaces that straight line with [[x,y], ...] for an offset/
        jogged cut. ``depth`` is a positive model distance; reverse flips the retained side.
        """
        try:
            axis_c, bad = _canon_section_axis(axis)
            if bad:
                return _dumps(bad)
            mode_c, bad = _canon_section_mode(mode)
            if bad:
                return _dumps(bad)
            if depth is not None and (bad := _positive(depth, "depth", "add_section_view")):
                return _dumps(bad)
            try:
                offset = float(offset)
            except (TypeError, ValueError):
                return _dumps({"ok": False, "error": "add_section_view: offset must be a "
                               "number. Nothing was added."})
            if plane is not None and multipoint is not None:
                return _dumps({"ok": False, "error": "add_section_view: plane and multipoint "
                               "both define the cut; pass one. Nothing was added."})
            d = _doc(doc_id)
            full = cb.call("drawing_get", {"doc_id": d})
            pv = _find_view(full, parent, derived=False)
            bbox = _bbox_of(pv) if pv else None
            if bbox is None:
                return _dumps({
                    "ok": False,
                    "error": f"add_section_view: no parent view {parent!r} to cut. This "
                             f"drawing's views are {_view_handles(full, derived=False)} — pass "
                             f"a view_id or an orientation. Nothing was added."})
            vid = pv.get("view_id") or pv.get("id")
            params: dict = {
                "doc_id": d, "parent_view_id": vid,
                "opts": {"mode": mode_c, "reverse": bool(reverse)},
                "show_hidden": bool(show_hidden),
            }
            if depth is not None:
                params["opts"]["depth"] = float(depth)
            cut = None
            resolved_plane = None
            if plane is not None:
                resolved_plane = require_mapping(plane, "plane",
                                                 '{"normal":[x,y,z],"point":[x,y,z]}')
                normal = require_list(resolved_plane.get("normal"), "plane.normal",
                                      "a 3D vector [x,y,z]")
                point = require_list(resolved_plane.get("point"), "plane.point",
                                     "a 3D point [x,y,z]")
                if len(normal) != 3 or len(point) != 3 or not all(
                        isinstance(value, (int, float)) for value in normal + point):
                    return _dumps({"ok": False, "error": "add_section_view: plane.normal and "
                                   "plane.point must each be three numbers. Nothing was added."})
                length = sum(float(value) ** 2 for value in normal) ** 0.5
                if length <= 1e-12:
                    return _dumps({"ok": False, "error": "add_section_view: plane.normal cannot "
                                   "be zero. Nothing was added."})
                unit = [float(value) / length for value in normal]
                resolved_plane = {
                    "normal": [float(value) for value in normal],
                    "point": [float(point[i]) + offset * unit[i] for i in range(3)],
                }
                params["plane"] = resolved_plane
            else:
                x0, y0, x1, y1 = bbox
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                pad = 0.1 * max(x1 - x0, y1 - y0) + 2.0
                if multipoint is not None:
                    cut = require_list(multipoint, "multipoint",
                                       "at least two [x,y] sheet-mm points")
                    if len(cut) < 2 or any(
                            not isinstance(point, list) or len(point) != 2
                            or not all(isinstance(value, (int, float)) for value in point)
                            for point in cut):
                        return _dumps({"ok": False, "error": "add_section_view: multipoint must "
                                       "contain at least two numeric [x,y] points. Nothing was added."})
                    cut = [[float(value) for value in point] for point in cut]
                    if offset:
                        index = 1 if axis_c == "horizontal" else 0
                        for point in cut:
                            point[index] += offset
                else:
                    cut = ([[x0 - pad, cy + offset], [x1 + pad, cy + offset]]
                           if axis_c == "horizontal"
                           else [[cx + offset, y0 - pad], [cx + offset, y1 + pad]])
                params["cut_line"] = cut
            if position is not None:
                position = require_list(position, "position", "a [x,y] sheet-mm point")
                if len(position) != 2:
                    return _dumps({"ok": False, "error": "add_section_view: position must be "
                                   "[x,y]. Nothing was added."})
                params["position"] = position
            if label:
                params["label"] = label
            r = cb.call("drawing_add_section", params)
            out = {
                "ok": True, "view_id": r.get("view_id"), "axis": axis_c, "mode": mode_c,
                "cut_line": cut, "plane": resolved_plane, "offset": offset,
                "depth": depth, "reverse": bool(reverse), "show_hidden": bool(show_hidden),
            }
            if r.get("view_id"):
                report, warnings = _layout_report(cb.call("drawing_get", {"doc_id": d}),
                                                  r["view_id"])
                out.update(report)
                if warnings:
                    out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # SILENT-WRONG-OUTPUT, measured 2026-07-30 on the A3 plate drawing:
    #   add_detail_view(parent_view_id="v2", cx=50, cy=50, radius=5)
    #     -> {"ok": true, "view_id": "v5"}
    # while the view the engine had just created carried
    #     "error": "detail circle encloses no parent geometry", "bbox": [], "visible": []
    # The engine KNEW the detail was empty and filed the reason on the view; only the reply
    # was hiding it, so a model gets a blank magnified circle on its sheet and is told the
    # call succeeded. The empty view is rolled back (drawing_remove_view,
    # core/doc/drawing_session.cpp:982) so a retry with a real centre does not leave debris.
    #
    # LIVE-DIALECT gap in the same call: parent_view_id="top" was rejected outright with
    # "detail parent view 'top' not found", although add_dimension / add_gdt / add_datum /
    # add_section_view all take an orientation word for the same drawing. Detail views may
    # legitimately magnify a SECTION, so the resolver here accepts derived views (and their
    # section label) too, which _resolve_drawing_view_id deliberately skips.

    @mcp.tool()
    def add_detail_view(parent_view_id: str, cx: float, cy: float, radius: float,
                        doc_id: Optional[str] = None) -> str:
        """Add a magnified DETAIL view of a circular region of a parent view. doc_id = drawing_doc_id.

        parent_view_id accepts a view_id (v1, v2, …), an orientation ("front"/"top"/…) or a
        section label ("A"). cx,cy,radius are in the PARENT view's sheet millimetres — read
        them off that view's bbox/arcs (drawing_get) or review_drawing.

        A circle that encloses none of the parent's geometry is refused, not filed as an empty
        view: the reply names the parent's real bbox so the next pick lands on the part.
        """
        try:
            d = _doc(doc_id)
            full = cb.call("drawing_get", {"doc_id": d})
            pv = _find_view(full, parent_view_id)
            if pv is None:
                return _dumps({
                    "ok": False,
                    "error": f"add_detail_view: no parent view {parent_view_id!r}. This "
                             f"drawing's views are {_view_handles(full)} — pass a view_id, an "
                             f"orientation or a section label. Nothing was added."})
            pid = pv.get("view_id") or pv.get("id")
            r = cb.call("drawing_add_detail", {"doc_id": d, "parent_view_id": pid,
                                               "center": [cx, cy], "radius": radius})
            vid = r.get("view_id")
            out = {"ok": True, "view_id": vid, "parent_view_id": pid}
            if not vid:
                return _dumps(out)
            report, warnings = _layout_report(cb.call("drawing_get", {"doc_id": d}), vid)
            if not report.get("entities"):
                cb.call("drawing_remove_view", {"doc_id": d, "view_id": vid})
                pbb = _bbox_of(pv)
                return _dumps({
                    "ok": False,
                    "error": f"add_detail_view: the circle at ({cx}, {cy}) r={radius} encloses "
                             f"none of view {pid}'s geometry, so the detail would render "
                             f"blank. That view occupies "
                             + (f"{[round(v, 3) for v in pbb]} sheet mm "
                                f"(centre {round((pbb[0] + pbb[2]) / 2, 3)}, "
                                f"{round((pbb[1] + pbb[3]) / 2, 3)})"
                                if pbb else "no sheet area at all")
                             + ". The empty view was removed; re-pick inside those bounds."})
            out.update(report)
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_hole_callout(view_id: str, a: Any, doc_id: Optional[str] = None,
                         feature_ref: Optional[Any] = None,
                         hole_type: Optional[str] = None, thread: Optional[str] = None,
                         diameter: Optional[float] = None, depth: Optional[float] = None,
                         thru: Optional[bool] = None, cbore_dia: Optional[float] = None,
                         cbore_depth: Optional[float] = None,
                         csk_dia: Optional[float] = None, csk_depth: Optional[float] = None,
                         csk_angle: Optional[float] = None, count: Optional[int] = None,
                         pos: Optional[Any] = None) -> str:
        """Add a semantic hole/thread callout at projected circle pick ``a``.

        feature_ref may be a source Hole id/name; its style, thread, depth, end condition,
        counterbore/countersink fields and pattern count fill any omitted arguments.  Explicit
        arguments win.  Example: thread="M6x1.0", depth=14 emits ``M6x1.0 ↧ 14`` rather than
        the tap-drill diameter.  thru=true emits THRU.
        """
        try:
            d = _doc(doc_id)
            a = require_list(a, "a", "a [x,y] sheet-mm point on the projected hole")
            if len(a) != 2:
                return _dumps({"ok": False, "error": "add_hole_callout: a must be [x,y]. "
                               "Nothing was added."})
            if pos is not None:
                pos = require_list(pos, "pos", "a [x,y] sheet-mm text position")
                if len(pos) != 2:
                    return _dumps({"ok": False, "error": "add_hole_callout: pos must be [x,y]. "
                                   "Nothing was added."})
            full = cb.call("drawing_get", {"doc_id": d})
            view_id = _resolve_drawing_view_id(d, view_id)
            defaults: dict = {}
            if feature_ref is not None:
                defaults, bad = _hole_feature_defaults(full, feature_ref)
                if bad:
                    return _dumps(bad)
            values = {
                "hole_type": hole_type, "thread": thread, "diameter": diameter, "depth": depth,
                "thru": thru, "cbore_dia": cbore_dia, "cbore_depth": cbore_depth,
                "csk_dia": csk_dia, "csk_depth": csk_depth, "csk_angle": csk_angle,
                "count": count,
            }
            for key, value in defaults.items():
                if values.get(key) is None:
                    values[key] = value
            style = _canon_word(values.get("hole_type") or "")
            style = {
                "tap": "tapped", "thread": "tapped",
                "cbore": "counterbore", "c_bore": "counterbore",
                "csk": "countersink", "csink": "countersink",
            }.get(style, style)
            if not style:
                if values["thread"]:
                    style = "tapped"
                elif values["cbore_dia"] is not None or values["cbore_depth"] is not None:
                    style = "counterbore"
                elif any(values[key] is not None for key in
                         ("csk_dia", "csk_depth", "csk_angle")):
                    style = "countersink"
                else:
                    style = "simple"
            if style not in {"simple", "tapped", "counterbore", "countersink"}:
                return _dumps({"ok": False, "error": "add_hole_callout: hole_type must be "
                               "simple|tapped|counterbore|countersink. Nothing was added."})
            for field in ("diameter", "depth", "cbore_dia", "cbore_depth", "csk_dia",
                          "csk_depth", "csk_angle"):
                if values[field] is not None and (
                        bad := _positive(values[field], field, "add_hole_callout")):
                    return _dumps(bad)
            if values["count"] is not None:
                if isinstance(values["count"], bool) or int(values["count"]) != values["count"] \
                        or int(values["count"]) <= 0:
                    return _dumps({"ok": False, "error": "add_hole_callout: count must be a "
                                   "positive integer. Nothing was added."})
                values["count"] = int(values["count"])
            opts: dict = {"hole_type": style}
            for key in ("thread", "diameter", "depth", "cbore_dia", "cbore_depth", "csk_angle"):
                if values[key] is not None:
                    opts[key] = values[key]
            if values["csk_dia"] is not None:
                # The established engine field is shared by counterbores and countersinks.
                opts["cbore_dia"] = values["csk_dia"]
                opts["csk_dia"] = values["csk_dia"]
            suffixes = []
            if values["thru"]:
                suffixes.append("THRU")
            if values["csk_depth"] is not None:
                suffixes.append(f"↧ {float(values['csk_depth']):g}")
                opts["csk_depth"] = values["csk_depth"]
            if suffixes:
                opts["suffix"] = " " + " ".join(suffixes)
            if pos is not None:
                opts["pos"] = pos
            result, unsupported = _drawing_rpc(
                "add_hole_callout", "drawing_add_callout",
                {"doc_id": d, "view_id": view_id, "kind": "hole", "a": a, "opts": opts})
            if unsupported:
                return _dumps(unsupported)
            annotation = (result or {}).get("annotation") or {}
            if (result or {}).get("suppressed"):
                return _dumps({"ok": False, "reason": (result or {}).get("reason"),
                               "tool": "add_hole_callout"})
            if values["count"] is not None and annotation.get("id"):
                patched = cb.call("drawing_set_annotation", {
                    "doc_id": d, "view_id": view_id, "ann_id": annotation["id"],
                    "patch": {"count": values["count"]},
                })
                annotation = patched.get("annotation") or annotation
            return _dumps({
                "ok": True, "annotation": annotation.get("id"), "view_id": view_id,
                "text": annotation.get("text"), "hole_type": style,
                "feature_ref": feature_ref, "count": annotation.get("count"),
            })
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def add_balloon(view_id: str, a: Any, item: int, doc_id: Optional[str] = None,
                    pos: Optional[Any] = None, text: Optional[str] = None) -> str:
        """Add a BOM item balloon with a leader anchored at sheet point ``a``."""
        try:
            if isinstance(item, bool) or int(item) != item or int(item) <= 0:
                return _dumps({"ok": False, "error": "add_balloon: item must be a positive "
                               "BOM item number. Nothing was added."})
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            a = require_list(a, "a", "a [x,y] sheet-mm anchor")
            opts: dict = {"item": int(item)}
            if pos is not None:
                opts["pos"] = require_list(pos, "pos", "a [x,y] sheet-mm position")
            if text is not None:
                opts["text"] = str(text)
            result, unsupported = _drawing_rpc(
                "add_balloon", "drawing_add_callout",
                {"doc_id": d, "view_id": view_id, "kind": "balloon", "a": a, "opts": opts})
            if unsupported:
                return _dumps(unsupported)
            annotation = (result or {}).get("annotation") or {}
            return _dumps({"ok": True, "annotation": annotation.get("id"),
                           "item": annotation.get("item", int(item)),
                           "text": annotation.get("text")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def drawing_bom(doc_id: Optional[str] = None) -> str:
        """Return stable item/name/quantity/source BOM rows from a drawing's source assembly."""
        try:
            d = _doc(doc_id)
            result, unsupported = _drawing_rpc(
                "drawing_bom", "drawing_bom", {"doc_id": d})
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            return _dumps({"ok": True, "doc_id": d, "rows": result.get("rows") or [],
                           "is_assembly": bool(result.get("is_assembly")),
                           "source_doc_id": result.get("source_doc_id"),
                           "note": result.get("note")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)



    @mcp.tool()
    def add_dimension(view_id: str, a: Any, b: Optional[Any] = None,
                      dtype: str = "linear_dim", pos: Optional[list] = None,
                      doc_id: Optional[str] = None, text: Optional[str] = None,
                      tol_type: str = "none", tol_upper: Optional[float] = None,
                      tol_lower: Optional[float] = None,
                      precision: Optional[int] = None) -> str:
        """Add a dimension to a view. doc_id = drawing_doc_id.

        a, b name WHAT to dimension, either way round:
          * sheet-mm points  — a=[80,120] (read them off review_drawing / the view's arcs).
          * a MODEL feature  — a="F6356a7" (the stable sid describe_part / query_geometry /
            review_drawing's circles gave you), or a="Bore" (the feature name), or
            a=["bore","F6356a7"]. Every projected circle carries the sid of the model edge and
            faces it was drawn from, so the sid lands on exactly its own circle — one of four
            identical holes is its own sid. You never convert model geometry to sheet mm
            yourself. A target drawn only as hidden lines in this view is refused, naming the
            views where it is visible; a feature name covering several circles is refused with
            each circle's sid.
        pos is the optional text position (sheet mm). text is an optional verbatim display
        override; for dtype="note" it is REQUIRED and creates a single-leader free-text note.

        dtype: linear_dim | aligned_dim | radial_dim | diameter_dim | angular_dim |
               center_mark | center_line | note. The plain drafting words are accepted too —
               "diameter"/"diam"/"dia"/"⌀" -> diameter_dim (the callout reads 2×r) and
               "radius"/"radial" -> radial_dim (it reads r). Those two are NOT synonyms.
               radial_dim / diameter_dim / center_mark are determined by the ARC alone: pass
               only `a`. A note also takes only `a`, plus text="...".
        To auto-dimension a whole view instead, use make_drawing's model_items.

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front", "top", …)
        matching the view labels returned by review_drawing / make_drawing.
        """
        try:
            d = _doc(doc_id)
            notes: list = []
            # LIVE-DIALECT guard: the suffix-less drafting word ("diameter") used to reach the
            # engine untouched and be rejected as "not recognized" — see _canon_dim_type.
            dtype, bad = _canon_dim_type(dtype)
            if bad:
                return _dumps(bad)
            tol_type = _canon_word(tol_type or "none")
            if tol_type not in _TOL_TYPES:
                return _dumps({"ok": False, "error": "add_dimension: tol_type must be "
                               "none|symmetric|deviation|limit|basic. Nothing was added."})
            if tol_type != "none" and dtype in {"center_mark", "center_line", "note"}:
                return _dumps({"ok": False, "error": f"add_dimension: {dtype} is not a measured "
                               "dimension and cannot carry a tolerance. Nothing was added."})
            if precision is not None and (
                    isinstance(precision, bool) or int(precision) != precision
                    or int(precision) < 0):
                return _dumps({"ok": False, "error": "add_dimension: precision must be a "
                               "non-negative integer. Nothing was added."})
            if tol_type == "symmetric":
                if tol_upper is None:
                    return _dumps({"ok": False, "error": "add_dimension: symmetric tolerance "
                                   "requires tol_upper. Nothing was added."})
                tol_lower = tol_upper
            if tol_type in {"deviation", "limit"} and (
                    tol_upper is None or tol_lower is None):
                return _dumps({"ok": False, "error": f"add_dimension: {tol_type} tolerance "
                               "requires tol_upper and tol_lower. Nothing was added."})
            if tol_type == "none" and (tol_upper is not None or tol_lower is not None):
                return _dumps({"ok": False, "error": "add_dimension: tol_upper/tol_lower need "
                               "tol_type=symmetric|deviation|limit. Nothing was added."})
            for field, value in (("tol_upper", tol_upper), ("tol_lower", tol_lower)):
                if value is not None and (bad := _positive(
                        value, field, "add_dimension", allow_zero=True)):
                    return _dumps(bad)
            # LIVE-DIALECT guard: point args as stringified lists used to forward untouched to
            # the engine, producing a contentless engine-side rejection. A name (sid / feature)
            # is now a legitimate value here — see the "dimensioning a FEATURE" block above.
            a_pt, a_target, bad = _point_or_target(a, "a")
            if bad:
                return _dumps(bad)
            b_pt = b_target = None
            if dtype in _ARC_DETERMINED:
                # The engine reads the nearest arc and never looks at b for these three
                # (drawing_session.cpp:1034-1061), which is why b is optional here.
                if b is not None:
                    notes.append(f"{dtype} is determined by the picked circle alone — b was "
                                 f"not needed and was ignored")
            elif dtype == "note":
                if not isinstance(text, str) or not text.strip():
                    return _dumps({
                        "ok": False,
                        "error": "add_dimension: dtype='note' needs non-empty text='...'. "
                                 "Nothing was added."})
                if b is not None:
                    b_pt, b_target, bad = _point_or_target(b, "b")
                    if bad:
                        return _dumps(bad)
                else:
                    b_pt = list(a_pt) if a_pt else None
            elif b is None:
                return _dumps({
                    "ok": False,
                    "error": f"add_dimension: {dtype} measures BETWEEN two picks, so it needs "
                             f"both a and b (sheet-mm points, or model targets like "
                             f'a="F6356a7"). Only radial_dim / diameter_dim / center_mark and '
                             f"note take a alone. Nothing was added."})
            else:
                b_pt, b_target, bad = _point_or_target(b, "b")
                if bad:
                    return _dumps(bad)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            resolved: dict = {}
            if a_target or b_target:
                full = cb.call("drawing_get", {"doc_id": d})
                src = full.get("source_doc_id") or d
                for slot, tokens in (("a", a_target), ("b", b_target)):
                    if not tokens:
                        continue
                    target, bad = _target_entities(src, tokens, slot, notes)
                    if bad:
                        return _dumps(bad)
                    hit, bad = _resolve_target_point(full, view_id, target, dtype, slot, notes)
                    if bad:
                        return _dumps(bad)
                    circle = hit["circle"]
                    resolved[slot] = {"from": tokens if len(tokens) > 1 else tokens[0],
                                      "sids": circle["matched"],
                                      "edge_sids": circle["edge_sids"],
                                      "sheet_point": hit["point"],
                                      "model_diameter": round(2.0 * circle["model_radius"], 6)}
                    if slot == "a":
                        a_pt = hit["point"]
                    else:
                        b_pt = hit["point"]
                if (a_pt and b_pt and dtype not in _ARC_DETERMINED
                        and abs(a_pt[0] - b_pt[0]) < 1e-6 and abs(a_pt[1] - b_pt[1]) < 1e-6):
                    return _dumps({
                        "ok": False,
                        "error": f"add_dimension: a and b both resolved to the SAME circle at "
                                 f"{[round(x, 3) for x in a_pt]} sheet mm, so this {dtype} "
                                 f"would measure zero — pass the sids of two different "
                                 f"features. Nothing was added.",
                        "interpreted": notes})
            # SILENT-WRONG-OUTPUT guard, measured 2026-07-30 on the A3 plate drawing whose
            # `top` view occupies x 185..235, y 200.75..230.75 sheet mm:
            #   add_dimension(view_id="top", a=[0,0], b=[10,0])
            #     -> {"ok":true,"dtype":"linear_dim","value":5.0,"text":"5"}
            # A confident "5" measured between two points that touch nothing, at the sheet
            # origin, on a drawing that otherwise looks finished. linear_dim/aligned_dim read
            # their VALUE straight off the two picks (drawing_session.cpp:1148-1151), so a pick
            # that is not on the projected part reports a number about nothing.
            # The bbox is the exact extent of this view's projected geometry, so BOTH picks
            # outside it proves the dimension cannot be measuring this view under any pick
            # tolerance — that is refused. ONE pick outside is a legitimate shape (a witness
            # line extended off the silhouette), so it is reported, not refused. radial /
            # diameter / center_mark snap to the nearest arc themselves and center_line / note
            # are construction, so none of them are checked here.
            if dtype in ("linear_dim", "aligned_dim") and a_pt and b_pt:
                vbb = _bbox_of(_find_view(cb.call("drawing_get", {"doc_id": d}), view_id) or {})
                if vbb:
                    def _outside(p: list) -> bool:
                        return not (vbb[0] - 1e-6 <= p[0] <= vbb[2] + 1e-6
                                    and vbb[1] - 1e-6 <= p[1] <= vbb[3] + 1e-6)
                    off = [s for s, p in (("a", a_pt), ("b", b_pt)) if _outside(p)]
                    shown = [round(v, 3) for v in vbb]
                    if len(off) == 2:
                        return _dumps({
                            "ok": False,
                            "error": f"add_dimension: neither pick is on view {view_id}. Its "
                                     f"projected geometry occupies {shown} sheet mm, but "
                                     f"a={[round(v, 3) for v in a_pt]} and "
                                     f"b={[round(v, 3) for v in b_pt]} are both outside it, so "
                                     f"this {dtype} would report a distance between two points "
                                     f"that touch nothing. Pick inside those bounds (the view's "
                                     f"arcs and visible polylines in drawing_get are in the "
                                     f"same sheet mm), or name the feature — a=\"F6356a7\" / "
                                     f"a=\"Bore\" resolves it for you. Nothing was added."})
                    if off:
                        notes.append(
                            f"pick {off[0]} lies outside view {view_id}'s projected geometry "
                            f"({shown} sheet mm) — the dimension was placed as asked, but "
                            f"check it is measuring the edge you meant")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm point") if pos else None
            params = {"doc_id": d, "view_id": view_id, "type": dtype, "a": a_pt}
            if b_pt is not None:
                params["b"] = b_pt
            if pos:
                params["pos"] = pos
            if text is not None:
                params["text"] = text
            r = cb.call("drawing_add_dim", params)
            ann = r.get("annotation") or {}
            patch: dict = {}
            if tol_type != "none":
                patch["tol_type"] = tol_type
                if tol_upper is not None:
                    patch["tol_upper"] = float(tol_upper)
                if tol_lower is not None:
                    patch["tol_lower"] = float(tol_lower)
            if precision is not None:
                patch["precision"] = int(precision)
            if patch and ann.get("id"):
                try:
                    updated = cb.call("drawing_set_annotation", {
                        "doc_id": d, "view_id": view_id, "ann_id": ann["id"], "patch": patch})
                    ann = updated.get("annotation") or ann
                except Exception:
                    cb.call("drawing_remove_annotation", {
                        "doc_id": d, "view_id": view_id, "ann_id": ann["id"]})
                    raise
            out = {"ok": True, "annotation": ann.get("id"), "dtype": dtype,
                   "tol_type": ann.get("tol_type", "none")}
            if "value" in ann:
                out["value"] = ann["value"]
            if ann.get("text"):
                out["text"] = ann["text"]
            if resolved:
                out["resolved"] = resolved
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_chamfer_dimension(view_id: str, a: Any, pos: Any, length: float,
                              angle: float = 45.0, doc_id: Optional[str] = None,
                              text: Optional[str] = None) -> str:
        """Add a native chamfer callout (default text ``length × angle°``)."""
        try:
            for field, value in (("length", length), ("angle", angle)):
                if bad := _positive(value, field, "add_chamfer_dimension"):
                    return _dumps(bad)
            if float(angle) >= 180.0:
                return _dumps({"ok": False, "error": "add_chamfer_dimension: angle must be "
                               "between 0 and 180 degrees. Nothing was added."})
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            a = require_list(a, "a", "a [x,y] sheet-mm attachment point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm text position")
            params = {
                "doc_id": d, "view_id": view_id, "a": a, "pos": pos,
                "length": float(length), "angle": float(angle),
            }
            if text is not None:
                params["text"] = text
            result, unsupported = _drawing_rpc(
                "add_chamfer_dimension", "drawing_add_chamfer_dimension", params)
            if unsupported:
                return _dumps(unsupported)
            annotation = (result or {}).get("annotation") or {}
            return _dumps({"ok": True, "annotation": annotation.get("id"),
                           "kind": annotation.get("kind", "chamfer"),
                           "text": annotation.get("text")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def add_ordinate_dimensions(view_id: str, origin: Any, points: Any,
                                axis: str = "x", doc_id: Optional[str] = None,
                                positions: Optional[Any] = None) -> str:
        """Add native datum-origin ordinate dimensions for target sheet points.

        axis is x|y|both. ``positions`` optionally supplies one text position per target.
        The engine computes model-unit deltas from the view scale; this tool never fabricates
        dimension values.
        """
        try:
            axis = _canon_word(axis)
            if axis not in {"x", "y", "both"}:
                return _dumps({"ok": False, "error": "add_ordinate_dimensions: axis must be "
                               "x|y|both. Nothing was added."})
            origin = require_list(origin, "origin", "a [x,y] sheet-mm datum origin")
            points = require_list(points, "points", "one or more [x,y] sheet-mm targets")
            if len(origin) != 2 or not points or any(
                    not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(value, (int, float)) for value in point)
                    for point in points):
                return _dumps({"ok": False, "error": "add_ordinate_dimensions: origin must be "
                               "[x,y] and points must be a non-empty list of [x,y] values. "
                               "Nothing was added."})
            if positions is not None:
                positions = require_list(positions, "positions",
                                         "one [x,y] text position per target")
                if len(positions) != len(points):
                    return _dumps({"ok": False, "error": "add_ordinate_dimensions: positions "
                                   "must have the same length as points. Nothing was added."})
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            base_params = {"doc_id": d, "view_id": view_id, "origin": origin,
                           "points": points}
            if positions is not None:
                base_params["positions"] = positions
            annotations: list = []
            try:
                for one_axis in (("x", "y") if axis == "both" else (axis,)):
                    result, unsupported = _drawing_rpc(
                        "add_ordinate_dimensions", "drawing_add_ordinate_dimensions",
                        {**base_params, "axis": one_axis})
                    if unsupported:
                        for annotation in annotations:
                            if isinstance(annotation, dict) and annotation.get("id"):
                                cb.call("drawing_remove_annotation", {
                                    "doc_id": d, "view_id": view_id,
                                    "ann_id": annotation["id"]})
                        return _dumps(unsupported)
                    annotations.extend((result or {}).get("annotations") or [])
            except Exception:
                for annotation in annotations:
                    if isinstance(annotation, dict) and annotation.get("id"):
                        cb.call("drawing_remove_annotation", {
                            "doc_id": d, "view_id": view_id, "ann_id": annotation["id"]})
                raise
            return _dumps({"ok": True, "annotations": [
                annotation.get("id") if isinstance(annotation, dict) else annotation
                for annotation in annotations
            ], "axis": axis, "count": len(annotations)})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)



    @mcp.tool()
    def add_gdt(view_id: str, symbol: str, tolerance: str, a: list, pos: list,
                datums: Optional[list] = None, diameter: bool = False,
                modifier: str = "none", doc_id: Optional[str] = None,
                row2: Optional[Any] = None) -> str:
        """Add an ASME Y14.5 feature-control frame, including an optional composite row.

        modifier and every datum modifier accept none|mmc|lmc|projected|free|tangent.
        row2 is {"tolerance":"0.05","diameter":true,"modifier":"mmc",
        "datums":[{"ref":"A","modifier":"mmc"}]}.
        """
        try:
            symbol = _canon_word(symbol)
            if symbol not in _GDT_SYMBOLS:
                return _dumps({"ok": False, "error": "add_gdt: symbol must be one of "
                               + ", ".join(sorted(_GDT_SYMBOLS)) + ". Nothing was added."})
            modifier, bad = _word(modifier, _GDT_MODIFIERS, "modifier", "add_gdt")
            if bad:
                return _dumps(bad)

            def _datum_rows(values: Any, field: str) -> tuple[list, Optional[dict]]:
                values = require_list(values, field, 'a list such as ["A",{"ref":"B",'
                                      '"modifier":"mmc"}]') if values else []
                out = []
                for value in values:
                    datum = dict(value) if isinstance(value, dict) else {"ref": str(value)}
                    ref = str(datum.get("ref") or "").strip().upper()
                    if not ref:
                        return [], {"ok": False, "error": f"add_gdt: {field} contains a datum "
                                                          "with no ref. Nothing was added."}
                    datum_modifier, datum_bad = _word(
                        datum.get("modifier", "none"), _GDT_MODIFIERS,
                        "modifier", "add_gdt")
                    if datum_bad:
                        datum_bad["error"] = f"add_gdt: {field} datum {ref}: " + \
                                             datum_bad["error"].split(": ", 1)[-1]
                        return [], datum_bad
                    out.append({"ref": ref, "modifier": datum_modifier})
                return out, None

            dl, bad = _datum_rows(datums, "datums")
            if bad:
                return _dumps(bad)
            row2_spec = None
            if row2 is not None:
                raw = require_mapping(row2, "row2", "a composite GDT row dict")
                row2_modifier, bad = _word(
                    raw.get("modifier", raw.get("tol_modifier", "none")),
                    _GDT_MODIFIERS, "modifier", "add_gdt")
                if bad:
                    return _dumps(bad)
                row2_datums, bad = _datum_rows(raw.get("datums"), "row2.datums")
                if bad:
                    return _dumps(bad)
                row2_tolerance = raw.get("tolerance", raw.get("tol_value"))
                if row2_tolerance is None:
                    return _dumps({"ok": False, "error": "add_gdt: row2 requires tolerance. "
                                   "Nothing was added."})
                row2_spec = {
                    "tol_value": row2_tolerance,
                    "tol_diameter": bool(raw.get("diameter", raw.get("tol_diameter", False))),
                    "tol_modifier": row2_modifier,
                    "datums": row2_datums,
                }
            d = _doc(doc_id)
            a = require_list(a, "a", "a [x,y] sheet-mm point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm point")
            view_id = _resolve_drawing_view_id(d, view_id)
            spec = {"symbol": symbol, "tol_value": tolerance, "tol_diameter": bool(diameter),
                    "tol_modifier": modifier, "datums": dl, "a": a, "pos": pos}
            if row2_spec is not None:
                spec["row2"] = row2_spec
            r = cb.call("drawing_add_gdt", {"doc_id": d, "view_id": view_id, "spec": spec})
            annotation = r.get("annotation") or {}
            return _dumps({"ok": True, "annotation": annotation.get("id"),
                           "text": annotation.get("text"), "row2": annotation.get("row2")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_datum(view_id: str, label: str, a: list, pos: list,
                  doc_id: Optional[str] = None) -> str:
        """Add a datum feature symbol (triangle + boxed letter). doc_id = drawing_doc_id.
        label is the datum letter ("A"); a is the feature point, pos the label box (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            a = require_list(a, "a", "a [x,y] sheet-mm point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm point")
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            spec = {"label": label, "a": a, "pos": pos}
            r = cb.call("drawing_add_datum", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_datum_target(view_id: str, label: str, target_type: str, a: Any, pos: Any,
                         size: Optional[str] = None,
                         doc_id: Optional[str] = None) -> str:
        """Add a Y14.5 datum target. target_type is point|line|area; labels use A1, A2, ..."""
        try:
            target_type, bad = _word(
                target_type, _DATUM_TARGET_TYPES, "target_type", "add_datum_target")
            if bad:
                return _dumps(bad)
            label = str(label).strip().upper()
            if not _re.fullmatch(r"[A-Z][1-9][0-9]*", label):
                return _dumps({"ok": False, "error": "add_datum_target: label must be a datum "
                               "letter plus target number, e.g. A1. Nothing was added."})
            if target_type in {"line", "area"} and not str(size or "").strip():
                return _dumps({"ok": False, "error": f"add_datum_target: {target_type} targets "
                               "require size. Nothing was added."})
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            a = require_list(a, "a", "a [x,y] sheet-mm target point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm label position")
            spec = {"label": label, "target_type": target_type, "a": a, "pos": pos}
            if size is not None:
                spec["size"] = str(size)
            result, unsupported = _drawing_rpc(
                "add_datum_target", "drawing_add_datum_target",
                {"doc_id": d, "view_id": view_id, "spec": spec})
            if unsupported:
                return _dumps(unsupported)
            annotation = (result or {}).get("annotation") or {}
            return _dumps({"ok": True, "annotation": annotation.get("id"),
                           "label": annotation.get("label", label),
                           "target_type": annotation.get("target_type", target_type)})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def add_weld_symbol(view_id: str, weld_type: str, size: str, a: Any, pos: Any,
                        doc_id: Optional[str] = None, other_side: bool = False,
                        all_around: bool = False,
                        intermittent_length: Optional[float] = None,
                        pitch: Optional[float] = None, contour: str = "none",
                        finish: str = "none", field_weld: bool = False,
                        tail_text: Optional[str] = None,
                        full_penetration: bool = False) -> str:
        """Add a structured AWS weld symbol.

        weld_type: fillet|square|vee|bevel|plug. contour: none|flush|convex|concave.
        finish: none|chipping|grinding|hammering|machining|planishing|rolling|unspecified.
        Extended fields degrade with reason=engine_unsupported on a pre-extension engine; the
        tool removes the partial basic symbol instead of claiming those fields landed.
        """
        try:
            weld_type, bad = _word(weld_type, _WELD_TYPES, "weld_type", "add_weld_symbol")
            if bad:
                return _dumps(bad)
            contour, bad = _word(contour, _WELD_CONTOURS, "contour", "add_weld_symbol")
            if bad:
                return _dumps(bad)
            finish, bad = _word(finish, _WELD_FINISHES, "finish", "add_weld_symbol")
            if bad:
                return _dumps(bad)
            for field, value in (("intermittent_length", intermittent_length),
                                 ("pitch", pitch)):
                if value is not None and (
                        bad := _positive(value, field, "add_weld_symbol")):
                    return _dumps(bad)
            if (intermittent_length is None) != (pitch is None):
                return _dumps({"ok": False, "error": "add_weld_symbol: intermittent_length and "
                               "pitch must be supplied together. Nothing was added."})
            if not str(size).strip():
                return _dumps({"ok": False, "error": "add_weld_symbol: size is required. "
                               "Nothing was added."})
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            a = require_list(a, "a", "a [x,y] sheet-mm leader anchor")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm symbol position")
            spec: dict = {
                "weld_type": weld_type, "size": str(size), "other_side": bool(other_side),
                "all_around": bool(all_around), "a": a, "pos": pos,
            }
            extended: dict = {}
            if intermittent_length is not None:
                extended["intermittent_length"] = float(intermittent_length)
                extended["pitch"] = float(pitch)
            if contour != "none":
                extended["contour"] = contour
            if finish != "none":
                extended["finish"] = finish
            if field_weld:
                extended["field_weld"] = True
            if tail_text is not None and str(tail_text).strip():
                extended["tail_text"] = str(tail_text).strip()
            if full_penetration:
                extended["full_penetration"] = True
            spec.update(extended)
            result, unsupported = _drawing_rpc(
                "add_weld_symbol", "drawing_add_weld",
                {"doc_id": d, "view_id": view_id, "spec": spec})
            if unsupported:
                return _dumps(unsupported)
            annotation = (result or {}).get("annotation") or {}
            missing_extended = [
                key for key, value in extended.items() if annotation.get(key) != value
            ]
            if missing_extended:
                if annotation.get("id"):
                    cb.call("drawing_remove_annotation", {
                        "doc_id": d, "view_id": view_id, "ann_id": annotation["id"]})
                return _dumps({
                    "ok": False, "reason": "engine_unsupported", "tool": "add_weld_symbol",
                    "rpc": "drawing_add_weld",
                    "error": "loaded engine accepted the basic weld but did not preserve "
                             "extended field(s): " + ", ".join(missing_extended)
                             + "; the partial symbol was removed",
                })
            return _dumps({"ok": True, "annotation": annotation.get("id"),
                           "text": annotation.get("text"), "weld_type": weld_type,
                           "extended_fields": sorted(extended)})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)



    @mcp.tool()
    def add_surface_finish(view_id: str, ra: str, a: list, pos: list, machining: str = "required",
                           all_around: bool = False, doc_id: Optional[str] = None) -> str:
        """Add a surface-finish symbol. doc_id = drawing_doc_id. ra is the roughness value ("1.6");
        machining: any|required|prohibited. a is the surface point, pos the symbol (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…)."""
        try:
            d = _doc(doc_id)
            a = require_list(a, "a", "a [x,y] sheet-mm point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm point")
            # LIVE-DIALECT gap (2026-07-30): add_surface_finish(view_id="top") died with the
            # engine's "no view 'top'" although its three annotation siblings (add_dimension /
            # add_gdt / add_datum) had taken the orientation word since D7. The one tool in the
            # set that did not resolve it.
            view_id = _resolve_drawing_view_id(d, view_id)
            spec = {"value": ra, "machining": machining, "all_around": bool(all_around),
                    "a": a, "pos": pos}
            r = cb.call("drawing_add_surface_finish", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # SILENT-DROP, measured 2026-07-30: `if x is not None and y is not None` meant a
    # single-axis move was thrown away without a word —
    #   set_view(view_id="v1", x=250)  ->  {"ok":true,"position":[258.0,85.25],"scale":2.0}
    # ok:true, and the position it reports back is the one the view already had. "Nudge this
    # view right" is the most natural way to obey set_view's own "spread overlapping views
    # apart" instruction, and it did nothing. The engine takes a full [x,y] (drawing_set_view,
    # core/doc/drawing_session.cpp:1013-1014), so the missing axis is filled from the view's
    # CURRENT position rather than defaulted to zero — which would fling the view to the sheet
    # edge.

    @mcp.tool()
    def set_view(view_id: str, x: Optional[float] = None, y: Optional[float] = None,
                 scale: Optional[float] = None, doc_id: Optional[str] = None,
                 orientation: Optional[str] = None,
                 show_hidden: Optional[bool] = None) -> str:
        """Move, rescale, reorient, and/or toggle hidden lines for a view.

        x/y are sheet mm; scale is geometric; orientation is front|back|top|bottom|left|right|
        iso; show_hidden is an explicit boolean.  Omitted fields are unchanged.
        """

        try:
            d = _doc(doc_id)
            before = cb.call("drawing_get", {"doc_id": d})
            target = _find_view(before, view_id)
            if target is None:
                return _dumps({
                    "ok": False,
                    "error": f"set_view: no view {view_id!r}. This drawing's views are "
                             f"{_view_handles(before)}. Nothing was changed."})
            view_id = target.get("view_id") or target.get("id") or view_id
            if orientation is not None:
                orientation = _canon_word(orientation)
                if orientation not in _VIEW_ORIENTATIONS:
                    return _dumps({"ok": False, "error": "set_view: orientation must be "
                                   + ", ".join(_VIEW_ORIENTATIONS)
                                   + ". Nothing was changed."})
            params = {"doc_id": d, "view_id": view_id}
            if x is not None or y is not None:
                at = target.get("position")
                at = ([float(at[0]), float(at[1])]
                      if isinstance(at, list) and len(at) == 2 else [0.0, 0.0])
                params["position"] = [float(x) if x is not None else at[0],
                                      float(y) if y is not None else at[1]]
            if scale is not None:
                params["scale"] = scale
            if orientation is not None:
                params["orientation"] = orientation
            if show_hidden is not None:
                params["show_hidden"] = bool(show_hidden)
            r = cb.call("drawing_set_view", params)
            view = r.get("view") or {}
            out = {"ok": True, "view_id": r.get("view_id", view_id),
                   "position": view.get("position"), "scale": view.get("scale"),
                   "orientation": view.get("orientation"),
                   "show_hidden": view.get("show_hidden")}
            report, warnings = _layout_report(cb.call("drawing_get", {"doc_id": d}), view_id)
            out.update(report)
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def arrange_drawing(doc_id: Optional[str] = None, projection: Optional[str] = None,
                        scale: Optional[Any] = None, sheet_id: Optional[str] = None,
                        size: Optional[str] = None) -> str:
        """Re-lay a drawing sheet professionally in one call (no sheet-mm math).

        Picks ONE ISO 5455 standard scale that fits the views in the free drawing area (frame
        minus title block and tables) unless `scale` is given (1.0, 0.5, "1:2", "2:1"), puts the
        standard views on the projection lattice — projection "third" (ASME; top above front,
        right to its right) or "first" (ISO E; mirrored) — with shared alignment, places
        sections/details beside their parent row, and updates the title block SCALE and
        projection symbol. Dimensions and section cuts move with their views. `size` optionally
        changes the paper first ("A3", "A4 portrait", "A2"). doc_id = drawing_doc_id.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "sheet_id": sheet_id or ""}
            notes: list = []
            if size is not None:
                paper, bad = _canon_sheet_size(size)
                if bad:
                    return _dumps(bad)
                cb.call("drawing_set_sheet", {"doc_id": d, "sheet_id": sheet_id or "",
                                              "size": paper})
                notes.append(f"sheet set to {paper}")
            if projection is not None:
                proj = _canon_word(projection).replace("_angle", "").replace("angle", "")
                proj = {"1st": "first", "3rd": "third"}.get(proj, proj)
                if proj not in ("first", "third"):
                    return _dumps({"ok": False, "error": "arrange_drawing: projection must be "
                                   "'third' (ASME) or 'first' (ISO E)."})
                params["projection"] = proj
            if scale is not None:
                want, bad = _canon_view_scale(scale)
                if bad:
                    return _dumps(bad)
                params["scale"] = want
            result, unsupported = _drawing_rpc("arrange_drawing", "drawing_arrange", params)
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            out = {"ok": True, "doc_id": d, "scale": result.get("scale"),
                   "scale_text": result.get("scale_text"),
                   "projection": result.get("projection")}
            full = result.get("drawing") or {}
            sheets = full.get("sheets") or []
            size_mm = _sheet_size(full)
            warnings = []
            for v in _views_of(full):
                bb = _bbox_of(v)
                if bb and size_mm and _off_sheet_by(bb, size_mm):
                    warnings.append(f"view {v.get('view_id')} extends past the sheet — use a "
                                    f"bigger size= or a smaller scale")
            if sheets:
                out["sheet"] = sheets[0].get("size")
            if warnings:
                out["warnings"] = warnings
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def add_drawing_sheet(doc_id: Optional[str] = None, size: str = "A3",
                          name: Optional[str] = None, title: Optional[str] = None) -> str:
        """Add a sheet to a drawing and return its stable sheet id."""
        try:
            size, bad = _canon_sheet_size(size)
            if bad:
                return _dumps(bad)
            d = _doc(doc_id)
            params = {"doc_id": d, "size": size}
            if name is not None:
                params["name"] = str(name)
            if title is not None:
                params["title"] = str(title)
            result, unsupported = _drawing_rpc(
                "add_drawing_sheet", "drawing_add_sheet", params)
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            return _dumps({"ok": True, "sheet_id": result.get("sheet_id")
                           or (result.get("sheet") or {}).get("id"),
                           "sheet": result.get("sheet")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def list_drawing_sheets(doc_id: Optional[str] = None) -> str:
        """List every drawing sheet and the active sheet id."""
        try:
            d = _doc(doc_id)
            result, unsupported = _drawing_rpc(
                "list_drawing_sheets", "drawing_list_sheets", {"doc_id": d})
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            return _dumps({"ok": True, "sheets": result.get("sheets") or [],
                           "active_sheet_id": result.get("active_sheet_id")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def set_active_drawing_sheet(sheet_id: str, doc_id: Optional[str] = None) -> str:
        """Select the sheet targeted by subsequent view/annotation operations."""
        try:
            if not str(sheet_id).strip():
                return _dumps({"ok": False, "error": "set_active_drawing_sheet: sheet_id is "
                               "required. Nothing was changed."})
            d = _doc(doc_id)
            result, unsupported = _drawing_rpc(
                "set_active_drawing_sheet", "drawing_set_active_sheet",
                {"doc_id": d, "sheet_id": str(sheet_id).strip()})
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            return _dumps({"ok": True, "sheet_id": result.get("sheet_id", sheet_id),
                           "sheet": result.get("sheet")})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @mcp.tool()
    def add_drawing_table(kind: str, doc_id: Optional[str] = None,
                          sheet_id: Optional[str] = None,
                          position: Optional[Any] = None,
                          title: Optional[str] = None,
                          rows: Optional[Any] = None) -> str:
        """Add an on-sheet bom|revision|holes|notes|cutlist table.

        BOM and other supported kinds auto-populate from the source where possible.  Explicit
        rows are useful for revision/notes tables and are never fabricated by this wrapper.
        """
        try:
            kind, bad = _word(kind, _TABLE_KINDS, "kind", "add_drawing_table")
            if bad:
                return _dumps(bad)
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "kind": kind}
            if sheet_id is not None:
                params["sheet_id"] = str(sheet_id)
            if position is not None:
                position = require_list(position, "position", "a [x,y] sheet-mm anchor")
                if len(position) != 2:
                    return _dumps({"ok": False, "error": "add_drawing_table: position must be "
                                   "[x,y]. Nothing was added."})
                params["position"] = position
            if title is not None:
                params["title"] = str(title)
            if rows is not None:
                params["rows"] = require_list(rows, "rows", "a list of table row objects")
            result, unsupported = _drawing_rpc(
                "add_drawing_table", "drawing_add_table", params)
            if unsupported:
                return _dumps(unsupported)
            result = result or {}
            return _dumps({"ok": True, "table_id": result.get("table_id")
                           or (result.get("table") or {}).get("id"),
                           "table": result.get("table"), "kind": kind})
        except Exception as exc:  # noqa: BLE001
            return _err(exc)



    @mcp.tool()
    def set_titleblock(fields: Optional[dict] = None, doc_id: Optional[str] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Fill the drawing's title block. doc_id = drawing_doc_id. fields is a dict of any of:
        title, drawn_by, date, material, finish, scale, rev, sheet, part_number, company.

        Aliases the drawing itself honours are folded on: revision -> rev, part_no ->
        part_number, drawn -> drawn_by. A part's NAME goes in `title` (part_name= / name= land
        there), never in `part_number` — that cell is the identifier a shop orders from. There
        is no units field: units are a document property (mm by default), not a title-block cell.

        Those field names are ALSO accepted flattened as top-level kwargs —
        set_titleblock(title="Widget", rev="B") works the same as
        set_titleblock(fields={"title": "Widget", "rev": "B"}), merged with any
        fields= dict you also pass. Any other top-level kwarg is rejected (not a
        recognized title-block field and not a synonym).

        Returns {ok, sheet_id, fields_set, title_block} where title_block is the sheet's FULL
        resulting field set (not just what you just passed) — confirms the write landed and
        shows any prior fields it's layered on top of.
        """
        try:
            d = _doc(doc_id)
            nested = kwargs.pop("kwargs", None)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    kwargs.setdefault(k, v)
            # LIVE-CRASH guard: `sorted(fields.keys())` blows up on a stringified fields dict
            # with a bare AttributeError (identical shape to run-97813015264134).
            fields = require_mapping(fields, "fields",
                                     "a title-block fields dict") if fields else {}
            flat = {k: v for k, v in kwargs.items() if v is not None}
            notes: list = []
            if flat:
                notes.append(f"flattened kwarg(s) {', '.join(sorted(flat))} read as "
                             f"fields={{...}}")
            merged = {**fields, **flat}
            if not merged:
                require_mapping(None, "fields", "a title-block fields dict")
            # BOTH doors go through ONE vocabulary now (module-level title-block block).
            # LIVE-DIALECT gap (2026-07-29): set_titleblock(part_name="40mm Cube",
            # revision="A", units="mm", scale="1:1") — scale landed, the other three were
            # refused as unknown. Two of them have a real home; `units` genuinely has none.
            # SILENT-DROP gap (2026-07-30): only the flattened-kwarg door was guarded, so
            # set_titleblock(fields={"part_name": "Plate"}) stored a key nothing renders.
            canon, folded, bad = _canon_titleblock_fields(merged, "set_titleblock()")
            if bad:
                return _dumps(bad)
            notes.extend(folded)
            r = cb.call("drawing_set_titleblock",
                        {"doc_id": d, "sheet_id": "s1",
                         "fields": _titleblock_payload(canon)})
            tb = r.get("title_block") or {}
            out = {"ok": True, "sheet_id": r.get("sheet_id", "s1"),
                   "fields_set": sorted(canon), "title_block": tb}
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def edit_drawing_annotation(view_id: str, annotation_id: str,
                                fields: Optional[dict] = None, delete: bool = False,
                                doc_id: Optional[str] = None,
                                **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Edit or delete one existing drawing annotation without rebuilding the sheet.

        view_id accepts a view id or orientation. annotation_id is listed by review_drawing.
        delete=true removes it. Otherwise pass fields (or flattened kwargs) containing any of:
        text, prefix, suffix, precision, critical, count, pos, value, tol_type, tol_upper,
        tol_lower. Empty text clears a prior verbatim override and recomposes a numeric dim.
        """
        try:
            d = _doc(doc_id)
            view_id = _resolve_drawing_view_id(d, view_id)
            nested = kwargs.pop("kwargs", None)
            if isinstance(nested, dict):
                for key, value in nested.items():
                    kwargs.setdefault(key, value)
            fields = require_mapping(fields, "fields",
                                     "an annotation patch dict") if fields else {}
            patch = {**fields, **{k: v for k, v in kwargs.items() if v is not None}}
            allowed = {
                "tol_type", "tol_upper", "tol_lower", "prefix", "suffix",
                "precision", "critical", "count", "pos", "value", "text",
            }
            unknown = sorted(set(patch) - allowed)
            if unknown:
                return _dumps({
                    "ok": False,
                    "error": "edit_drawing_annotation: unsupported field(s) "
                             + ", ".join(unknown) + "; valid fields are "
                             + ", ".join(sorted(allowed))})
            if delete and patch:
                return _dumps({
                    "ok": False,
                    "error": "edit_drawing_annotation: delete=true conflicts with edit fields; "
                             "choose delete or patch. Nothing changed."})
            full = cb.call("drawing_get", {"doc_id": d})
            view = _find_view(full, view_id)
            annotations = view.get("annotations") if isinstance(view, dict) else []
            ids = [str(ann.get("id")) for ann in (annotations or []) if ann.get("id")]
            if annotation_id not in ids:
                return _dumps({
                    "ok": False,
                    "error": f"edit_drawing_annotation: no annotation {annotation_id!r} on "
                             f"view {view_id}; available ids are {ids}. Nothing changed."})
            if delete:
                result = cb.call("drawing_remove_annotation", {
                    "doc_id": d, "view_id": view_id, "ann_id": annotation_id})
                return _dumps({
                    "ok": True, "deleted": annotation_id, "view_id": view_id,
                    "removed": result.get("removed", True)})
            if not patch:
                return _dumps({
                    "ok": False,
                    "error": "edit_drawing_annotation needs fields={...} or delete=true. "
                             "Nothing changed."})
            result = cb.call("drawing_set_annotation", {
                "doc_id": d, "view_id": view_id, "ann_id": annotation_id,
                "patch": patch})
            annotation = result.get("annotation") or {}
            return _dumps({
                "ok": True, "view_id": view_id, "annotation": annotation,
                "fields_set": sorted(patch)})
        except Exception as e:  # noqa: BLE001
            return _err(e)
