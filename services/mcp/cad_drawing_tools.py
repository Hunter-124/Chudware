"""CAD 2D-drawing MCP tools (T-RF8 split of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""
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


#: The engine's real paper sizes — core/doc/drawing_session.cpp:40-45 (ISO A0..A4 + ANSI A..E).
_SHEET_SIZES = ("A4", "A3", "A2", "A1", "A0", "A", "B", "C", "D", "E")
#: Words a model decorates a size with ("ISO A3", "A4 landscape", "A3 sheet size").
_SHEET_NOISE = frozenset({"iso", "ansi", "din", "jis", "landscape", "portrait",
                          "size", "sheet", "paper", "format"})


def _canon_sheet_size(value: Any) -> tuple[Optional[str], Optional[dict]]:
    """('A3', None) or (None, an error dict naming the real sizes).

    SILENT-WRONG-OUTPUT guard: drawing_make wraps the sheet write in
    `try { drawing_set_sheet(...) } catch (const OpError&) { /* bad size -> keep default A3 */ }`
    (core/doc/drawing_session.cpp:1796-1797), so an unrecognised size produced an A3 sheet that
    looks completely normal — the caller asked for A1 and shipped A3. Validate the spelling
    before the drawing is created instead.
    """
    raw = str(value if value is not None else "").strip()
    words = [w for w in _re.split(r"[\s\-_]+", raw.lower()) if w]
    kept = [w for w in words if w not in _SHEET_NOISE] or words
    s = "".join(kept).upper()
    if s in _SHEET_SIZES:
        return s, None
    return None, {
        "ok": False,
        "code": "invalid_op",
        "error": f"sheet {raw!r} is not a paper size; the drawing engine knows "
                 + ", ".join(_SHEET_SIZES)
                 + " (ISO A0-A4 and ANSI A-E). An unknown size used to fall back to A3 "
                   "silently, so this call was refused instead of drawn on the wrong sheet.",
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

# ---------------------------------------------------------------- dimensioning a FEATURE
#
# DESIGN gap measured live (2026-07-29, claude-sonnet-4-5) — three failures in ONE run, all the
# same idea: the model held the stable sid of the hole it wanted called out and handed it
# straight to add_dimension.
#   add_dimension(dtype="diameter", a=["bore","F6356a7"], b=["bore","F6356a7"], pos=[80,120])
#       -> "dimension point a must be [x,y] sheet mm"   (the engine, drawing_session.cpp:1117)
#   add_dimension(dtype="diameter", a=["bore","F6356a7"], pos=[80,120])
#       -> missing_argument: b                          (this tool's own signature)
#   add_dimension(a=["F6356a7","center"], b=["F6356a7","edge"])   (earlier pass, same run)
# add_dimension speaks SHEET millimetres and nothing in the toolset converted a model sid into
# them, so the model would have had to re-derive the projection (orientation frame x view scale
# + view position) by hand. For a diameter/radius the two points are not even independent — the
# projected ARC determines them.
#
# WHAT THE ENGINE ALREADY GIVES US (read, not assumed):
#   * drawing_get returns, per view, the projected analytic circles in SHEET mm:
#     arcs[] = {center:[x,y], radius, closed, layer} (core/doc/drawing.cpp::compound_arcs
#     :149-186, attached to every view by project_view :354-373), plus that view's `scale`. So
#     model radius = radius/scale — exactly the arithmetic the engine's own auto-dimension pass
#     uses (drawing_session.cpp:1655 and :1738).
#   * doc_query {"of":"face"|"edge"} on the SOURCE part returns every entity's stable sid WITH
#     its `radius` (the ⌀10 bore of a test plate reads radius 5.0, sid "F6356a7").
#   * drawing_get["source_doc_id"] (drawing_session.cpp:783) names the part a drawing projects,
#     so the two ends can be joined at all.
# WHAT IT DOES NOT GIVE US: the projected arcs carry NO sid. HLR rebuilds the edges and
# compound_arcs emits pure geometry, and drawing_model_items (:1690-1745) — the existing
# "N x ⌀d" hole-callout pass — likewise identifies holes purely by projected geometry, never by
# sid. There is therefore NO sid -> sheet-point projection to call, in C++ or from Python.
#
# The join we CAN make without inventing a projection is on the RADIUS, which is scale-exact at
# both ends: resolve the sid/feature to its MODEL radius R, then keep this view's projected
# circles whose radius/scale equals R, and hand the engine that circle's own centre. The engine
# then snaps the dimension to the nearest arc itself (drawing_session.cpp:1034-1049) and reads
# the value off THAT arc, so the reported number is the engine's, not one we computed. Nothing
# here multiplies by a scale factor of our own.
#
# The residual ambiguity is REPORTED, never hidden: N identical holes project to N circles of
# the same radius which all carry the same diameter VALUE but sit at different sheet positions,
# so the reply says how many matched and which centre was used. Zero matches is a refusal that
# names the diameters this view really has, and the views where the target does project as a
# circle.

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

#: |r_model - R| below this counts as the same circle. The two ends are the SAME number in the
#: engine (a hole's cylindrical face radius and its projected circle's radius/scale), so this is
#: a float-noise tolerance, not a fuzzy match.
_CIRCLE_TOL = 1e-4

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


def _token_radius(source_doc_id: str, token: str) -> tuple[Optional[float], str, str]:
    """(the target's MODEL radius, how it resolved, why it did not) for one target token."""
    text = str(token).strip()
    if _SID_RE.match(text):
        ents = _source_entities(source_doc_id)
        hit = next((m for m in ents if str(m.get("sid")) == text), None)
        if hit is None:
            hit = next((m for m in ents
                        if str(m.get("sid")).casefold() == text.casefold()), None)
        if hit is None:
            return None, "", (f"{text!r} is not a face/edge sid of the source part "
                              f"{source_doc_id} (sids are regenerated when the model rebuilds — "
                              f"call describe_part / query_geometry for the current ones)")
        r = hit.get("radius")
        if not isinstance(r, (int, float)) or r <= 0.0:
            return None, "", (f"{text!r} is a {hit.get('surf') or 'non-round'} "
                              f"{hit.get('kind') or 'entity'} — it has no radius, so it does not "
                              f"project as a circle. Pass the sid of a round face or circular "
                              f"edge (query_geometry(query={{'of':'face','surf':'cylinder'}})), "
                              f"or dimension it with two sheet-mm points")
        return float(r), (f"{text} ({hit.get('surf')} {hit.get('kind')}, "
                          f"r={round(float(r), 6)} mm)"), ""
    feats = _source_features(source_doc_id)
    hit = next((f for f in feats
                if str(f.get("id")) == text
                or str(f.get("name") or "").strip().casefold() == text.casefold()), None)
    if hit is None:
        roster = ", ".join(f'{f.get("id")} "{f.get("name") or ""}"' for f in feats) or "none"
        return None, "", (f"{text!r} is neither a face/edge sid nor a feature of the source part "
                          f"{source_doc_id} (its features are: {roster})")
    params = hit.get("params")
    dia = _param_number(params, "diameter", "dia", "hole_diameter", "bore_diameter")
    rad = _param_number(params, "radius", "r", "hole_radius")
    if dia is not None and dia > 0.0:
        return dia / 2.0, (f"the feature {hit.get('name')!r} ({hit.get('id')}, "
                           f"{hit.get('type')}, ⌀{round(dia, 6)} mm)"), ""
    if rad is not None and rad > 0.0:
        return rad, (f"the feature {hit.get('name')!r} ({hit.get('id')}, {hit.get('type')}, "
                     f"r={round(rad, 6)} mm)"), ""
    return None, "", (f"the feature {hit.get('name')!r} ({hit.get('id')}, {hit.get('type')}) "
                      f"carries no diameter or radius parameter, so there is no circle to "
                      f"match. Pass the sid of one of its round faces/edges "
                      f"(query_geometry(query={{'of':'face','surf':'cylinder'}})) or two "
                      f"sheet-mm points")


def _resolve_target_radius(source_doc_id: str, tokens: list, slot: str,
                           notes: list) -> tuple[Optional[float], Optional[dict]]:
    """(the target's MODEL radius, a refusal). Appends what was read/ignored to *notes*."""
    hits: list = []
    misses: list = []
    for t in tokens:
        r, how, why = _token_radius(source_doc_id, t)
        if r is None:
            misses.append((t, why))
        else:
            hits.append((t, r, how))
    if not hits:
        detail = "; ".join(why for _t, why in misses) or "it named nothing resolvable"
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot}={tokens if len(tokens) > 1 else tokens[0]!r} did "
                     f"not resolve to a model feature — {detail}. Nothing was added.",
        }
    radii = sorted({round(r, 6) for _t, r, _h in hits})
    if len(radii) > 1:
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot}={tokens!r} names TWO different circles "
                     f"(radii {radii} mm) — a dimension has one target, so nothing was added. "
                     f"Pass the single sid/feature you meant.",
        }
    for t, why in misses:
        if _canon_word(t) in _TARGET_QUALIFIERS:
            notes.append(f"{slot}: ignored the qualifier {t!r} — the projected arc determines "
                         f"the dimension's points")
        else:
            notes.append(f"{slot}: ignored {t!r} ({why}) because {hits[0][0]!r} resolved")
    notes.append(f"{slot}={tokens if len(tokens) > 1 else tokens[0]!r} read as {hits[0][2]}")
    return hits[0][1], None


def _view_circles(view: dict) -> list:
    """This view's projected circles as {center (sheet mm), model_radius, closed}, deduped.

    A through hole projects its two rims onto coincident circles, and the same circle appears on
    both the visible and the hidden layer, so identical (centre, radius, closed) entries collapse
    — the same dedupe drawing_model_items does (drawing_session.cpp:1704-1710).
    """
    scale = view.get("scale") or 1.0
    if not isinstance(scale, (int, float)) or scale == 0:
        scale = 1.0
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
        item = {"center": [float(c[0]), float(c[1])],
                "model_radius": float(r_sheet) / float(scale),
                "closed": bool(arc.get("closed"))}
        if any(abs(o["center"][0] - item["center"][0]) < 1e-4
               and abs(o["center"][1] - item["center"][1]) < 1e-4
               and abs(o["model_radius"] - item["model_radius"]) < _CIRCLE_TOL
               and o["closed"] == item["closed"] for o in out):
            continue
        out.append(item)
    out.sort(key=lambda it: (round(it["center"][0], 6), round(it["center"][1], 6)))
    return out


def _views_of(full: dict) -> list:
    out: list = []
    for sheet in (full.get("sheets") or []):
        for v in (sheet.get("views") or []):
            if isinstance(v, dict):
                out.append(v)
    return out


def _resolve_target_point(full: dict, view_id: str, radius: float, dtype: str,
                          slot: str, notes: list) -> tuple[Optional[list], Optional[dict]]:
    """Return the circle centre for spacing dims, or a point on its ring for arc dims."""
    need_closed = dtype == "diameter_dim"          # engine: want_closed=1 only for diameter_dim
    view = next((v for v in _views_of(full)
                 if view_id in (v.get("view_id"), v.get("id"))), None)
    if view is None:
        return None, {"ok": False, "error": f"add_dimension: no view {view_id!r} in this drawing"}
    circles = [c for c in _view_circles(view)
               if (c["closed"] or not need_closed)
               and abs(c["model_radius"] - radius) <= _CIRCLE_TOL]
    if not circles:
        here = sorted({round(2.0 * c["model_radius"], 4) for c in _view_circles(view)
                       if c["closed"] or not need_closed})
        elsewhere = []
        for v in _views_of(full):
            vid = v.get("view_id") or v.get("id")
            if vid == view_id:
                continue
            if any((c["closed"] or not need_closed)
                   and abs(c["model_radius"] - radius) <= _CIRCLE_TOL for c in _view_circles(v)):
                elsewhere.append(f'{vid} ({v.get("orientation") or v.get("type")})')
        where = (f" It DOES project as a circle in view(s) {', '.join(elsewhere)} — dimension it "
                 f"there." if elsewhere else
                 " No view of this drawing shows it as a circle: add the view that looks down "
                 "its axis with add_drawing_view(orientation=…), then dimension it there.")
        shows = (f"the {'diameters' if need_closed else 'arc diameters'} it does show are "
                 f"{here} mm." if here else
                 f"it shows no {'full circles' if need_closed else 'arcs'} at all.")
        return None, {
            "ok": False,
            "error": f"add_dimension: {slot} resolved to a ⌀{round(2.0 * radius, 6)} mm circle, "
                     f"but view {view_id} "
                     f"({view.get('orientation') or view.get('type')}) projects no "
                     f"{'full circle' if need_closed else 'arc'} of that size — {shows}"
                     f"{where} Nothing was added.",
        }
    chosen = circles[0]
    if len(circles) > 1:
        notes.append(
            f"{slot}: {len(circles)} circles of ⌀{round(2.0 * radius, 6)} mm project into view "
            f"{view_id} (identical features measure the same); the dimension was placed on the "
            f"one at {[round(x, 3) for x in chosen['center']]} — the others are at "
            f"{[[round(x, 3) for x in c['center']] for c in circles[1:]]}, pass one of those as "
            f"{slot}=[x,y] to move it")
    else:
        notes.append(f"{slot}: matched the projected ⌀{round(2.0 * radius, 6)} mm circle at "
                     f"{[round(x, 3) for x in chosen['center']]} sheet mm in view {view_id}")
    if dtype not in _ARC_DETERMINED:
        return list(chosen["center"]), None
    scale = view.get("scale") or 1.0
    if not isinstance(scale, (int, float)) or scale == 0:
        scale = 1.0
    # The engine selects the arc whose RING is nearest the pick. Passing the centre makes every
    # concentric ring equally located and the smallest one wins (live: ⌀9.8 and ⌀20 both became
    # ⌀8.8). Pick the matched ring itself, where its distance metric is exactly zero.
    return [chosen["center"][0] + radius * float(scale), chosen["center"][1]], None


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


def _apply_view_scale(doc_id: str, want: float, notes: list) -> tuple[list, list]:
    """Set every view's geometric scale to *want*, keeping each view centred where it is.

    Returns (per-view {view_id, orientation, scale} read back off the drawing, warnings).
    """
    before = cb.call("drawing_get", {"doc_id": doc_id})
    keep: dict = {}
    for v in _views_of(before):
        vid = v.get("view_id") or v.get("id")
        bbox = v.get("bbox")
        pos = v.get("position")
        old = v.get("scale") or 1.0
        if not vid:
            continue
        params: dict = {"doc_id": doc_id, "view_id": vid, "scale": want}
        if (isinstance(bbox, list) and len(bbox) == 4
                and all(isinstance(x, (int, float)) for x in bbox)
                and isinstance(pos, list) and len(pos) == 2 and old):
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            keep[vid] = (cx, cy)
            # geometry = model*scale + position (drawing_session.cpp:1917-1920), so hold the
            # view's bbox centre still while the scale changes.
            params["position"] = [cx - (cx - pos[0]) / old * want,
                                  cy - (cy - pos[1]) / old * want]
        cb.call("drawing_set_view", params)
    after = cb.call("drawing_get", {"doc_id": doc_id})
    sheets = after.get("sheets") or []
    size_mm = (sheets[0].get("size_mm") if sheets else None) or [0.0, 0.0]
    applied: list = []
    warnings: list = []
    for v in _views_of(after):
        vid = v.get("view_id") or v.get("id")
        got = v.get("scale")
        applied.append({"view_id": vid, "orientation": v.get("orientation") or v.get("type"),
                        "scale": got})
        if isinstance(got, (int, float)) and abs(float(got) - want) > 1e-9:
            warnings.append(f"view {vid} kept scale {got} instead of {want}")
        bbox = v.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4
                and all(isinstance(x, (int, float)) for x in bbox)):
            continue
        if vid in keep:
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            drift = max(abs(cx - keep[vid][0]), abs(cy - keep[vid][1]))
            if drift > 0.5:
                warnings.append(f"view {vid} moved {round(drift, 3)} mm on the sheet while "
                                f"rescaling")
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
    """The sheet's [width, height] in mm — core/doc/drawing_session.cpp:39-48 stores every
    paper size landscape, so this is always (w >= h)."""
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


def _drawing_review_summary(full: dict, render: dict) -> dict:
    """Compact sheet-mm inspection payload paired with review_drawing's pixels."""
    warnings: list[str] = []
    views: list[dict] = []
    seen_warnings: set[str] = set()
    for view in _views_of(full):
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
    return {
        "ok": True,
        "drawing_doc_id": full.get("doc_id"),
        "source_doc_id": full.get("source_doc_id"),
        "source_material": source_material,
        "sheet_mm": _sheet_size(full),
        "render": {k: render.get(k) for k in
                   ("visible", "hidden", "annotations", "title_block") if k in render},
        "views": views,
        "title_block": title_block,
        "warnings": warnings,
        "manufacturing_ready": not warnings,
        "next_actions": (
            ["Fix every warning, then review_drawing again before export_drawing."]
            if warnings else
            ["Pixels and structured checks are clean; export without further sheet mutations."]
        ),
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
                      "scale", "rev", "sheet", "part_number", "company")

#: _norm_arg spelling -> the canonical field ("drawnby" -> "drawn_by"), so a caller who writes
#: drawnBy / DRAWN_BY hits the same cell the docstring names.
_TITLEBLOCK_BY_NORM = {_norm_arg(f): f for f in _TITLEBLOCK_FIELDS}

#: Field-name aliases. The RENDERER already treats each of these as the SAME cell:
#: core/doc/session.cpp:7106-7117 keys the ten title-block cells by a pair — {"rev","revision"},
#: {"part_number","part_no"}, {"drawn_by","drawn"}. Folding them onto the canonical name is
#: therefore not a guess about intent; it is what the drawing does with them anyway.
#: Keys normalised through _norm_arg.
_TITLEBLOCK_ALIASES = {"revision": "rev", "partno": "part_number", "drawn": "drawn_by"}

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
        f"set_titleblock() has no {name!r} field. The ten cells this drawing actually "
        f"renders are {', '.join(_TITLEBLOCK_FIELDS)} (core/doc/session.cpp:7106-7117) — "
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
                             f"(core/doc/session.cpp:7106-7117). {key} is NOT part_number — a "
                             f"name in the box a shop floor orders from is a wrong drawing "
                             f"that looks right. Pass the name as title=, or the identifier "
                             f"as part_number=. Nothing was written."}
            target = "title"
        if target is None:
            return {}, [], {
                "ok": False,
                "error": f"{where} got the unknown title-block field {key!r}. The cells this "
                         f"drawing renders are {', '.join(_TITLEBLOCK_FIELDS)} "
                         f"(core/doc/session.cpp:7106-7117); anything else is stored as a key "
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
# LIVE-DIALECT gap (2026-07-29): make_drawing(orientation="landscape") died as
# unknown_argument. There is NO sheet-orientation concept in the engine to wire it to: every
# paper size in sheet_dims (core/doc/drawing_session.cpp:39-48) is stored LANDSCAPE — the table
# is literally commented "Landscape (w >= h)" and A3 is {420, 297} — and drawing_set_sheet
# (:1771-1782) takes a size string and nothing else. So orientation is REFUSED, because
# accepting it would tell the caller a sheet was rotated when it was not.
#
# One value of `orientation` IS real here though, and it is not the sheet's: "front"/"top"/"iso"
# is a VIEW orientation, which is what `views` takes (and what add_drawing_view calls the
# argument). That spelling is folded onto `views` with a note instead of being refused.
_SHEET_ORIENTATIONS = frozenset({"landscape", "portrait", "horizontal", "vertical"})

#: orient_frame's real view directions — core/doc/drawing_session.cpp:342-355 (+ "iso").
_VIEW_ORIENTATIONS = ("front", "back", "top", "bottom", "left", "right", "iso")


def _orientation_verdict(value: Any) -> tuple[Optional[list], Optional[dict]]:
    """(view names to draw, None) when *value* names VIEW orientations, else (None, a refusal)."""
    items = value if isinstance(value, (list, tuple)) else [value]
    words = [_canon_word(v) for v in items if str(v).strip()]
    if words and all(w in _VIEW_ORIENTATIONS for w in words):
        return list(words), None
    bad = next((w for w in words if w in _SHEET_ORIENTATIONS), None)
    if bad:
        return None, {
            "ok": False,
            "error": f"make_drawing: orientation={value!r} — a Chudware sheet has no "
                     f"orientation to set. Every paper size is stored LANDSCAPE "
                     f"(A4 297x210, A3 420x297, … core/doc/drawing_session.cpp:39-48) and the "
                     f"only sheet property there is its SIZE, so accepting "
                     f"orientation={bad!r} would report a rotation that never happened. Choose "
                     f"the paper with sheet=\"A4\"…\"A0\"/\"A\"…\"E\"; for a taller drawing use "
                     f"the next size up. (orientation= IS accepted when it names VIEW "
                     f"orientations — orientation=\"front\" or [\"front\",\"top\"] — which is "
                     f"what views= takes.) Nothing was created.",
            "valid_sheet": list(_SHEET_SIZES),
        }
    return None, {
        "ok": False,
        "error": f"make_drawing: orientation={value!r} is neither a VIEW orientation "
                 f"({', '.join(_VIEW_ORIENTATIONS)} — a synonym of views=) nor anything a "
                 f"sheet has: sheets are landscape-only and carry just a SIZE "
                 f"(sheet=\"A4\"…\"A0\"/\"A\"…\"E\"). Nothing was created.",
        "valid_views": list(_VIEW_ORIENTATIONS),
    }


def register_cad_drawing_tools(mcp: FastMCP) -> None:
    """CAD 2D-drawing MCP tools (T-RF8 split of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""


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
                     name: Optional[str] = None) -> str:
        """Create a 2D engineering drawing of the current part in ONE shot, then refine it.

        Builds projected views (hidden-line removed), auto-dimensions the ortho views, calls out
        holes, and fills the title block. Returns the new drawing_doc_id.

        views   : list of orientations, e.g. ["front","top","right","iso"] (default this set).
        sections: list of auto-section requests, each a dict
                  {"parent":"front","axis":"vertical"|"horizontal","mode":"full"|"half",
                   "reverse":false,"label":"A"} — cuts the parent view through its centre, no
                  sheet-mm math needed.
        sheet   : paper size "A4".."A0" (ISO) or "A".."E" (ANSI) — default A3. Also accepted as
                  sheet_size= / paper_size= / template= when the value IS a size; a template
                  FILE has no meaning here (there is no border/titleblock template concept).
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
        orientation: only as a synonym of views= (a VIEW orientation, "front"/["front","top"]).
                  Sheets have no orientation — every paper size is landscape — so
                  orientation="landscape" is refused rather than silently ignored.

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
            # LIVE-DIALECT gap: orientation="landscape" died as unknown_argument. A sheet has no
            # orientation to set (see the sheet-orientation block above), but a VIEW orientation
            # is real and is what views= takes, so that reading is folded on instead of refused.
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
                                 f"VIEW orientation; sheets have no orientation)")
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
            # drawing_make always auto-scales to fit the sheet (drawing_session.cpp:1841-1946)
            # and its spec has no scale key, so an explicit scale is applied to the finished
            # views — repositioning each so it stays where the layout put it.
            scale_warnings: list = []
            view_scales: list = []
            if want_scale is not None and drawing_doc_id:
                view_scales, scale_warnings = _apply_view_scale(drawing_doc_id, want_scale, notes)
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

    def _drawing_out_path(path: str, fmt: str) -> tuple[Optional[str], Optional[dict]]:
        """(the absolute file to write, None) or (None, a refusal).

        `filename="90x50x12_plate_drawing"` carries no extension, so the format's extension is
        appended — but appending unconditionally is how save_part shipped "x.pdf.pdf", so an
        extension that already MATCHES fmt is left exactly as it is. A path ending in the OTHER
        drawing extension is a contradiction we must not resolve by guessing: writing PDF bytes
        into a .dxf file produces a file no viewer can open, and the reverse loses the drawing.
        """
        out = os.path.abspath(os.path.expanduser(path))
        ext = os.path.splitext(out)[1].lower().lstrip(".")
        if ext == fmt:
            return out, None
        other = "dxf" if fmt == "pdf" else "pdf"
        if ext == other:
            return None, {
                "ok": False,
                "error": f"export_drawing: fmt={fmt!r} contradicts the {other!r} extension on "
                         f"{os.path.basename(out)!r}. Writing {fmt.upper()} bytes to a .{other} "
                         f"file produces a file nothing can open, so nothing was written — "
                         f"re-issue with a matching pair, e.g. fmt={fmt!r} with "
                         f"{os.path.splitext(os.path.basename(out))[0]}.{fmt}, or drop fmt and "
                         f"let the extension choose it.",
            }
        return out + "." + fmt, None

    @mcp.tool()
    def export_drawing(fmt: Optional[str] = None, doc_id: Optional[str] = None,
                       path: Optional[str] = None, filename: Optional[str] = None) -> str:
        """Export a 2D DRAWING document to a vector file so you can deliver it.

        fmt    : "pdf" (a fixed-size engineering sheet) or "dxf" (CAD interchange). Omitted,
                 it is taken from the output extension, else pdf. Pass the drawing_doc_id from
                 make_drawing as doc_id. Call review_drawing after the final sheet mutation first.
        path   : the file to write (also accepted as filename= / out_path= / file_path=). An
                 extension is appended when missing and never doubled. A path whose extension
                 contradicts fmt is refused rather than written.
        Returns path, entity/annotation counts and the fixed paper size. export_part is for
        3D B-rep/mesh only; a drawing is 2D and uses this exporter.
        """
        try:
            # LIVE-DIALECT gap: filename= died as unknown_argument before the engine was
            # reached — see the argument-name dialect block above.
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
                fmt = ext if ext in ("pdf", "dxf") else "pdf"
                if ext in ("pdf", "dxf"):
                    notes.append(f"read the format {fmt!r} from the output extension")
            fmt = str(fmt).lower().lstrip(".")
            if fmt not in ("pdf", "dxf"):
                return _dumps({"ok": False, "error": f"fmt must be 'pdf' or 'dxf', got {fmt!r}"})
            # A caller-supplied relative path resolves against the DAEMON's cwd, not
            # the worker's project root, so the file silently lands somewhere the user
            # will never find. Resolve before the payload crosses the process boundary.
            if path:
                out_path, bad = _drawing_out_path(path, fmt)
                if bad:
                    return _dumps(bad)
            else:
                out_path = _tmp("." + fmt)
            r = cb.call("export_drawing", {"doc_id": d, "path": out_path, "fmt": fmt})
            out = {"ok": True, "doc_id": d, "format": fmt,
                   "path": r.get("path", out_path),
                   "visible": r.get("visible"), "hidden": r.get("hidden"),
                   "annotations": r.get("annotations"), "entities": r.get("entities"),
                   "fixed_page": r.get("fixed_page"), "sheet_mm": r.get("sheet_mm")}
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def review_drawing(doc_id: Optional[str] = None) -> Any:
        """Render and inspect a drawing as pixels plus exact sheet-mm metadata.

        The image is the same projected geometry, annotations, title block and border that the
        vector export contains. The paired JSON caption lists every view bbox, projected circle,
        annotation id/text/position, title-block field, off-sheet/overlap warning, and any source
        model versus title-block material mismatch. Fix every warning and review again; the
        built-in agent will not export a sheet changed since its last review.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
        if not rd.get("ok") or not os.path.exists(png):
            raise ValueError(f"Drawing render failed: {rd.get('reason', 'not a drawing doc?')}")
        full = cb.call("drawing_get", {"doc_id": d})
        return [Image(path=png), _dumps(_drawing_review_summary(full, rd))]



    @mcp.tool()
    def add_drawing_view(orientation: str = "front", doc_id: Optional[str] = None,
                         view_type: str = "projected", x: float = 150.0, y: float = 150.0) -> str:
        """Add ONE projected view to a drawing. doc_id = the drawing_doc_id.

        orientation: front|back|top|bottom|left|right|iso. view_type: base (the first/anchor
        ortho view) | projected | iso. x,y = the view centre on the sheet in millimetres.

        Returns {ok, view_id, bbox, entities, sheet_mm, on_sheet} — the sheet area the new view
        actually occupies, so a placement that lands off the paper or on top of an existing
        view is a number in the reply rather than something only review_drawing could show.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("drawing_add_view", {"doc_id": d, "type": view_type,
                                             "orientation": orientation, "position": [x, y]})
            vid = r.get("view_id")
            out = {"ok": True, "view_id": vid}
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
                         doc_id: Optional[str] = None) -> str:
        """Add a SECTION view by cutting a parent view through its centre. doc_id = drawing_doc_id.

        parent: the orientation ("front"/"top"/...) or view_id of the view to cut.
        axis  : "vertical" or "horizontal" — the orientation of the CUT LINE through the part
                centre ("horiz"/"h" and "vert"/"v" also accepted). An axis letter (x/y/z) is
                refused as ambiguous rather than guessed at.
        mode  : "full" (whole part sectioned) or "half" (half sectioned, half external view).
        reverse: flip which side of the cut is removed.
        No sheet-mm math needed — the engine places the cut line across the parent's bbox.

        Returns the cut line plus the section view's own bbox / entity count / sheet fit.
        """
        try:
            # Discriminator guard: an unvalidated axis/mode produced a plausible-looking
            # section on the WRONG plane (or a silently-full one) with nothing in the output
            # to reveal it. Validate BEFORE any engine call so a bad value adds no view.
            axis_c, bad = _canon_section_axis(axis)
            if bad:
                return _dumps(bad)
            mode_c, bad = _canon_section_mode(mode)
            if bad:
                return _dumps(bad)
            d = _doc(doc_id)
            # Reuse make-style auto-section via a tiny spec on the existing drawing.
            full = cb.call("drawing_get", {"doc_id": d})
            # A section's parent must be a real projected view — you cannot section a section.
            pv = _find_view(full, parent, derived=False)
            bbox = _bbox_of(pv) if pv else None
            if bbox is None:
                return _dumps({
                    "ok": False,
                    "error": f"add_section_view: no parent view {parent!r} to cut. This "
                             f"drawing's views are {_view_handles(full, derived=False)} — pass "
                             f"a view_id or an orientation. Nothing was added."})
            vid = pv.get("view_id") or pv.get("id")
            x0, y0, x1, y1 = bbox
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            pad = 0.1 * max(x1 - x0, y1 - y0) + 2.0
            cut = ([[x0 - pad, cy], [x1 + pad, cy]] if axis_c == "horizontal"
                   else [[cx, y0 - pad], [cx, y1 + pad]])
            params = {"doc_id": d, "parent_view_id": vid, "cut_line": cut,
                      "opts": {"mode": mode_c, "reverse": reverse}}
            if label:
                params["label"] = label
            r = cb.call("drawing_add_section", params)
            out = {"ok": True, "view_id": r.get("view_id"),
                   "axis": axis_c, "mode": mode_c, "cut_line": cut}
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
    def add_dimension(view_id: str, a: Any, b: Optional[Any] = None,
                      dtype: str = "linear_dim", pos: Optional[list] = None,
                      doc_id: Optional[str] = None, text: Optional[str] = None) -> str:
        """Add a dimension to a view. doc_id = drawing_doc_id.

        a, b name WHAT to dimension, either way round:
          * sheet-mm points  — a=[80,120] (read them off review_drawing / the view's arcs).
          * a MODEL feature  — a="F6356a7" (the stable sid describe_part / query_geometry gave
            you), or a="Bore" (the feature name), or a=["bore","F6356a7"]. The tool matches
            that feature's circle in this view's projection and dimensions it, so you never
            convert model geometry to sheet millimetres yourself. The reply says which circle
            it landed on, and how many identical ones were there.
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
                    radius, bad = _resolve_target_radius(src, tokens, slot, notes)
                    if bad:
                        return _dumps(bad)
                    point, bad = _resolve_target_point(full, view_id, radius, dtype, slot, notes)
                    if bad:
                        return _dumps(bad)
                    resolved[slot] = {"from": tokens if len(tokens) > 1 else tokens[0],
                                      "sheet_point": point,
                                      "model_diameter": round(2.0 * radius, 6)}
                    if slot == "a":
                        a_pt = point
                    else:
                        b_pt = point
                if (a_pt and b_pt and dtype not in _ARC_DETERMINED
                        and abs(a_pt[0] - b_pt[0]) < 1e-6 and abs(a_pt[1] - b_pt[1]) < 1e-6):
                    return _dumps({
                        "ok": False,
                        "error": f"add_dimension: a and b both resolved to the SAME circle at "
                                 f"{[round(x, 3) for x in a_pt]} sheet mm, so this {dtype} "
                                 f"would measure zero. Identical features share a radius and "
                                 f"cannot be told apart by it — pass the two circle centres as "
                                 f"explicit sheet points (they are listed in the note above, or "
                                 f"read them off the view's arcs), or dimension a different "
                                 f"pair. Nothing was added.",
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
            # Report the resolved type and the measured value, so a caller can SEE which
            # quantity was dimensioned (a radius reported as a diameter is the whole point of
            # keeping the two words apart) instead of a bare ok:true.
            out = {"ok": True, "annotation": ann.get("id"), "dtype": dtype}
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
    def add_gdt(view_id: str, symbol: str, tolerance: str, a: list, pos: list,
                datums: Optional[list] = None, diameter: bool = False,
                modifier: str = "none", doc_id: Optional[str] = None) -> str:
        """Add an ASME Y14.5 feature control frame. doc_id = drawing_doc_id.

        symbol  : position|flatness|perpendicularity|parallelism|angularity|concentricity|
                  symmetry|profile_surface|profile_line|circularity|cylindricity|straightness|
                  circular_runout|total_runout
        tolerance: the zone value, e.g. "0.1".  diameter=True for a ⌀ tolerance zone.
        modifier : none|mmc|lmc|projected (material condition on the tolerance).
        datums   : a list of datum letters ["A","B","C"] (or dicts {"ref":"A","modifier":"mmc"}).
        a   : the feature point on the view the frame points at (sheet mm, from review_drawing).
        pos : where the frame sits on the sheet (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            # LIVE-DIALECT guard: point args / `datums` as stringified lists — `for it in
            # datums` would otherwise iterate a string's CHARACTERS silently.
            a = require_list(a, "a", "a [x,y] sheet-mm point")
            pos = require_list(pos, "pos", "a [x,y] sheet-mm point")
            datums = require_list(datums, "datums", 'a list of datum letters, e.g. ["A","B"]') if datums else []
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            dl = []
            for it in datums:
                dl.append(it if isinstance(it, dict) else {"ref": str(it)})
            spec = {"symbol": symbol, "tol_value": tolerance, "tol_diameter": bool(diameter),
                    "tol_modifier": modifier, "datums": dl, "a": a, "pos": pos}
            r = cb.call("drawing_add_gdt", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
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
                 scale: Optional[float] = None, doc_id: Optional[str] = None) -> str:
        """Move and/or rescale a view. doc_id = drawing_doc_id. x,y reposition the view on the
        sheet (mm) — either one alone moves along that axis only; scale sets the view scale
        (1.0 = 1:1, 0.5 = 1:2, 2.0 = 2:1). Use this to spread overlapping views apart after
        review_drawing.

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).

        Returns {ok, view_id, position, scale, orientation} plus the sheet area the view now
        occupies — bbox, entities, sheet_mm, on_sheet, and any overlap with another view — so a
        move that pushes the view off the paper or onto its neighbour is a number in the reply,
        not something only review_drawing could reveal.
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
            params = {"doc_id": d, "view_id": view_id}
            if x is not None or y is not None:
                at = target.get("position")
                at = ([float(at[0]), float(at[1])]
                      if isinstance(at, list) and len(at) == 2 else [0.0, 0.0])
                params["position"] = [float(x) if x is not None else at[0],
                                      float(y) if y is not None else at[1]]
            if scale is not None:
                params["scale"] = scale
            r = cb.call("drawing_set_view", params)
            view = r.get("view") or {}
            out = {"ok": True, "view_id": r.get("view_id", view_id),
                   "position": view.get("position"), "scale": view.get("scale"),
                   "orientation": view.get("orientation")}
            report, warnings = _layout_report(cb.call("drawing_get", {"doc_id": d}), view_id)
            out.update(report)
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



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
