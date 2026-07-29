"""CAD query/measure/check MCP tools (T-RF8 split of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from pydantic import Field
from typing import Annotated
from typing import Any
from typing import Optional
from typing import Union
import json
import os
import re

from .cad_common import (
    CoreError,
    _BBOX_INTENT_RE,
    _EMPHASIS_PALETTE,
    _QVOCAB_HELP,
    _apply_emphasis_overlay,
    _as_face_query,
    _between_from_batch,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _crop_png_to_rect,
    _crop_rect_for_target,
    _doc,
    _dumps,
    _emphasis_color_name,
    _err,
    _palette_caption_block,
    _reading_from_measure,
    _resolve_measure_target,
    _resolve_one_selector,
    _target_bbox,
    _tmp,
    _vnorm,
    cb,
)

# --------------------------------------------------------------------------- input dialect
# `_reconcile_kwargs` (services/agent/agent_turn.py) rejects an unknown kwarg BEFORE the tool
# is ever called — unless the tool declares a var-keyword parameter, which makes it the owner
# of its own input dialect. The tools below therefore take `**kwargs` and normalise in-body.
# Every rewrite is echoed back to the model as `interpreted` (the cad_doc_tools house
# convention); genuinely ambiguous input raises a teaching error instead of a silent guess.

# The structured geometry-query keys. A model that writes the query FLATTENED across top-level
# kwargs — query_geometry(of="face", side="top") — means exactly the documented dict, so
# assemble it rather than refusing the call.
_QUERY_KEYS = ("of", "side", "surf", "surface", "shape", "parallel", "perpendicular",
               "pick", "near", "count", "limit", "max", "max_results",
               "index", "body_id")
_QUERY_KEY_CANON = {"surface": "surf", "shape": "surf",
                    # `count` IS a result cap (core/doc/picks.cpp truncates the
                    # match set to it), so the spellings every model reaches
                    # for — limit/max/max_results — are the same intent, not a
                    # new concept. Live 2026-07-27 (claude-opus, flyswatter):
                    # query_geometry(..., limit=30) was rejected outright.
                    "limit": "count", "max": "count", "max_results": "count"}

# Spellings of the `query` PARAMETER itself (as opposed to a key inside it).
_QUERY_SPELLINGS = ("target", "q", "face", "edge", "selector", "where")


def _take_alias(kwargs: dict, spellings: tuple) -> list:
    """Every supplied spelling of one parameter, removed from `kwargs` -> [(name, value)]."""
    return [(k, kwargs.pop(k)) for k in list(kwargs) if k in spellings]


def _one_alias(tool: str, canonical: str, kwargs: dict, spellings: tuple,
               value=None, notes: Optional[list] = None):
    """Adopt an alias spelling of `canonical` out of `kwargs`, echoing the rewrite in `notes`.

    Two spellings of the same parameter that DISAGREE are a real ambiguity, so they raise a
    teaching error naming both rather than letting one win silently."""
    found = [(k, v) for k, v in _take_alias(kwargs, spellings) if v is not None]
    if not found:
        return value
    if value is not None:
        clash = [(k, v) for k, v in found if v != value]
        if clash:
            raise ValueError(
                f"{tool}() got {canonical}={value!r} and "
                + " and ".join(f"{k}={v!r}" for k, v in clash)
                + f" — those are the same parameter spelled two ways and they disagree. "
                  f"Pass {canonical}= once with the value you mean.")
        if notes is not None:
            notes.append(f"{found[0][0]}= is another spelling of {canonical}= "
                         f"(same value) — kept once")
        return value
    if len({repr(v) for _, v in found}) > 1:
        raise ValueError(
            f"{tool}() got {len(found)} spellings of {canonical}= with different values ("
            + ", ".join(f"{k}={v!r}" for k, v in found)
            + f") — they are one parameter, so pass {canonical}= once.")
    if notes is not None:
        notes.append(f"{found[0][0]}={found[0][1]!r} read as {canonical}=")
    return found[0][1]


# What measure_part ALREADY reports. A `what=`/`measure=` naming one of these is a description of
# this tool's own output, not a geometry target, so it is dropped with a note instead of routed
# elsewhere (live 2026-07-29: measure_part(what="mass") was wrongly sent to measure_between, which
# has no way to answer it). Anything NOT matched here is treated as geometry and routed.
_MASS_PROPERTY_RE = re.compile(
    r"\b(mass|weigh\w*|volume|density|inerti\w+|centre\s+of\s+mass|center\s+of\s+mass|"
    r"centroid|com|cog|mass\s+propert\w+|properties)\b")


def _reject_unknown(tool: str, kwargs: dict, accepted: str) -> None:
    """Leftover kwargs are a real mistake — name them and the parameters that DO exist."""
    if not kwargs:
        return
    raise ValueError(
        f"{tool}() got unknown argument(s) "
        + ", ".join(f"{k}={kwargs[k]!r}" for k in sorted(kwargs))
        + f" — {accepted}")


def _interpreted(out: dict, notes: list) -> dict:
    """Attach the de-duplicated echo list, and ONLY when something was actually rewritten, so a
    canonical call's response is byte-identical to what it always was."""
    seen: list = []
    for n in notes:
        if n and n not in seen:
            seen.append(n)
    if seen:
        out["interpreted"] = seen
    return out


# ------------------------------------------------------------------- "no bodies" diagnostics

def _open_docs() -> list:
    """Every open document row (doc_id + name + feature_count), [] when doc_list is unusable."""
    try:
        return [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                if isinstance(e, dict) and e.get("doc_id")]
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original error
        return []


def _feature_rows(doc_id: str) -> Optional[list]:
    """The document's feature list, or None when the tree could NOT be read.

    None and [] must stay distinct: "this part has no features" and "I could not look" lead to
    different advice, and guessing between them is exactly the failure mode we are fixing."""
    try:
        tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
    except Exception:  # noqa: BLE001
        return None
    feats = tree.get("features")
    return feats if isinstance(feats, list) else None


def _add_box_call(doc_id: str) -> str:
    return (f'add_features(doc_id="{doc_id}", ops=[{{"op":"add_feature","feature":'
            '{"type":"box","name":"Plate","params":{"length":80,"width":60,"height":10}}}])')


def _teach_no_bodies(e: Exception, doc_id: Optional[str], retry: str) -> Exception:
    """Turn the engine's bare "document has no bodies to <x>" into a fixable instruction.

    core/doc/session.cpp::throw_no_bodies emits TWO different situations under one headline:
    a document that simply has no solid in it, and a document whose features all FAILED to
    rebuild (then, and only then, it appends the per-feature errors and sets
    detail.reason="feature rebuild failed"). The remedies are opposite — add geometry vs.
    repair the feature that broke — so this names which one it is, quoting the resolved doc_id
    and part name, and says plainly when the tree could not be read to tell them apart."""
    if not doc_id or "no bodies" not in str(e).lower():
        return e
    msg = str(e)
    detail = getattr(e, "detail", None)
    detail = dict(detail) if isinstance(detail, dict) else {"doc_id": doc_id}
    entry = next((x for x in _open_docs() if x.get("doc_id") == doc_id), {})
    name = entry.get("name") or ""
    who = f'{doc_id} "{name}"' if name else doc_id
    rows = _feature_rows(doc_id)
    rebuild_failed = (detail.get("reason") == "feature rebuild failed"
                      or "failed to rebuild" in msg)

    if rebuild_failed:
        because = msg.split(" — ", 1)[1].strip() if " — " in msg else "see `detail`"
        text = (f"{who} resolved fine but has no bodies because its feature(s) FAILED TO "
                f"REBUILD: {because}. Adding more geometry will not help — repair the broken "
                f'feature: call get_part(doc_id="{doc_id}") to read the tree and each '
                f"feature's error, then edit_feature (fix the parameter / re-point the "
                f"reference) or delete it, and retry {retry}.")
        code = "no_bodies_rebuild_failed"
    elif rows == []:
        text = (f"{who} resolved fine but is EMPTY — 0 features, so 0 bodies and nothing to "
                f"work on. Add geometry first, e.g. {_add_box_call(doc_id)}, then retry "
                f"{retry}.")
        code = "no_bodies_empty_document"
    elif rows:
        kinds = ", ".join(sorted({str(f.get("type") or "?") for f in rows
                                  if isinstance(f, dict)})) or "unknown types"
        text = (f"{who} has {len(rows)} feature(s) ({kinds}) and the engine reports no rebuild "
                f"failure, yet the document has no SOLID body — those features are "
                f"construction-only (sketch / plane / axis) and never made material. Turn one "
                f"into a solid (add_features with an extrude/revolve of the profile, or a "
                f"primitive: {_add_box_call(doc_id)}), then retry {retry}.")
        code = "no_bodies_no_solid_feature"
    else:
        text = (f"{who} resolved fine but has no bodies, and its feature tree could NOT be read "
                f"(doc_tree failed), so I cannot tell you whether the part is empty or its "
                f'features failed to build. Call get_part(doc_id="{doc_id}"): if it lists no '
                f"features, add geometry ({_add_box_call(doc_id)}); if it lists features, fix "
                f"the one whose status is not ok. Then retry {retry}.")
        code = "no_bodies_undiagnosed"

    detail.setdefault("doc_id", doc_id)
    if name:
        detail["doc_name"] = name
    detail["engine_message"] = msg
    # The transport `code` stays whatever the engine said (the envelope is unchanged); the
    # WHICH-of-the-two-situations verdict rides along additively so a caller can branch on it.
    detail["diagnosis"] = code
    return CoreError(text, getattr(e, "code", "") or "invalid_op", detail)


def _extra_kwargs(kwargs: dict) -> dict:
    """The var-keyword bag, cleaned up before anything reads it.

    FastMCP's func_metadata has no VAR_KEYWORD case: it synthesises a pydantic field literally
    named "kwargs" and hands it back as {"kwargs": None}, so drop that. A model that wrapped
    its real arguments in kwargs={...} gets them unwrapped instead of rejected."""
    nested = kwargs.pop("kwargs", None)
    if isinstance(nested, dict):
        for k, v in nested.items():
            kwargs.setdefault(k, v)
    return kwargs


def _read_query(tool: str, query, kwargs: dict, notes: list) -> tuple:
    """Normalise every spelling of a geometry query into ONE query dict.

    Accepts the canonical `query=` (dict or phrase), the alias spellings in _QUERY_SPELLINGS,
    and the query written FLATTENED across top-level kwargs (of=/side=/surf=/pick=/…) — a
    model writing `of="face", side="top"` means the documented dict, and that intent is not
    ambiguous. Returns (query_dict, subjects); `subjects` are alias values that are NOT
    geometry vocabulary (a part or feature name attached to the call), which the caller
    verifies rather than ignores. Every rewrite lands in `notes`."""
    flat: dict = {}
    spelled_flat: dict = {}   # canonical key -> the spelling it arrived under
    for key in list(kwargs):
        if key in _QUERY_KEYS:
            canon = _QUERY_KEY_CANON.get(key, key)
            value = kwargs.pop(key)
            # Two spellings of the SAME key (count=5 beside limit=30) must not
            # collapse silently — that is one value, so pass it once.
            if canon in flat and flat[canon] != value:
                raise ValueError(
                    f"{tool}(): the query key {canon!r} arrived twice with "
                    f"different values ({flat[canon]!r} as "
                    f"{spelled_flat[canon]!r}, {value!r} as {key!r}) — that "
                    f"is one query, so pass it once.")
            flat[canon] = value
            spelled_flat[canon] = key

    spelled = [(k, v) for k, v in _take_alias(kwargs, _QUERY_SPELLINGS) if v is not None]
    if query is not None:
        spelled.insert(0, ("query", query))

    readable: list = []   # (spelling, original value, query dict)
    subjects: list = []   # (spelling, original value, why it is not a query)
    for name, value in spelled:
        if isinstance(value, dict):
            readable.append((name, value, dict(value)))
            continue
        try:
            q = _as_face_query(value)
        except ValueError as ve:
            subjects.append((name, value, str(ve)))
            continue
        if name in ("face", "edge"):
            q = dict(q, of=name)  # the spelling itself pins the topology kind
        readable.append((name, value, q))

    if not readable and not flat:
        if subjects:
            name, value, why = subjects[0]
            raise ValueError(
                f"{tool}(): {name}={value!r} is the only argument that could be the geometry "
                f"query, and it is not query vocabulary. {why}")
        raise ValueError(
            f'{tool}() needs a geometry query. Pass query={{"of":"face","side":"top"}}, a bare '
            f'side word (query="top"), or write the keys flat as kwargs '
            f'({tool}(of="face", side="top")). Keys: {_QVOCAB_HELP}')

    merged: dict = {}
    origin: dict = {}

    def _merge(label: str, src: dict) -> None:
        for k, v in src.items():
            if k in merged and merged[k] != v:
                raise ValueError(
                    f"{tool}(): the query key {k!r} arrived twice with different values "
                    f"({merged[k]!r} from {origin[k]}, {v!r} from {label}) — that is one "
                    f"query, so pass it once as query={{...}}.")
            merged[k] = v
            origin.setdefault(k, label)

    for name, _value, q in readable:
        _merge(f"{name}=", q)
    if flat:
        _merge("the flattened kwargs", flat)
        notes.append(
            "the query was written flat across top-level kwargs ("
            + ", ".join(f"{k}={v!r}" for k, v in flat.items())
            + ") — assembled into query=" + json.dumps(merged, sort_keys=True))
    for name, value, q in readable:
        if name != "query":
            notes.append(f"{name}={value!r} read as query={json.dumps(q, sort_keys=True)}")
    return merged, subjects

# --------------------------------------------------------------- datum plane / direction words
# The engine only accepts the three reserved datum names, so "XY"/"xz"/"z" — the spellings a
# draftsman and a model both reach for — died on `plane 'xy' is not a reserved planar datum`.
# The mapping is exact (Z-up: top=XY, front=XZ, right=YZ), so accepting it is a rename, not a
# guess; it is still echoed.
_PLANE_WORDS = {
    "top": "top", "xy": "top", "yx": "top", "plan": "top", "horizontal": "top",
    "z": "top", "+z": "top", "-z": "top", "normalz": "top",
    "front": "front", "xz": "front", "zx": "front", "elevation": "front",
    "y": "front", "+y": "front", "-y": "front", "normaly": "front",
    "right": "right", "yz": "right", "zy": "right", "side": "right", "profile": "right",
    "x": "right", "+x": "right", "-x": "right", "normalx": "right",
}

# Signed direction words -> a unit vector. Unlike a query `side`, a PULL direction's sign is
# load-bearing (draft along -Z is not draft along +Z), so the sign is preserved.
_DIR_WORDS = {
    "x": (1.0, 0.0, 0.0), "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0), "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "z": (0.0, 0.0, 1.0), "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
    "top": (0.0, 0.0, 1.0), "up": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
    "down": (0.0, 0.0, -1.0), "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
    "back": (0.0, 1.0, 0.0), "rear": (0.0, 1.0, 0.0), "front": (0.0, -1.0, 0.0),
    "vertical": (0.0, 0.0, 1.0),
}


def _read_plane(tool: str, plane, notes: list) -> str:
    """A reserved datum-plane name from any of its spellings ("XY" -> "top")."""
    if plane is None:
        return "top"
    raw = str(plane).strip().lower().replace("plane", "")
    sign = raw[:1] if raw[:1] in "+-" else ""   # "-x" must not lose its sign to separator strip
    body = raw[len(sign):]
    for junk in (" ", "\t", "_", "-"):
        body = body.replace(junk, "")
    hit = _PLANE_WORDS.get((sign + body) or "top")
    if hit is None:
        raise ValueError(
            f"{tool}(plane={plane!r}): the engine has exactly three reserved datum planes "
            f'(Z-up) — "top" (XY, normal +Z), "front" (XZ, normal -Y) and "right" (YZ, normal '
            f'+X). Pass one of those names; "XY"/"XZ"/"YZ" and a bare axis letter are accepted '
            f"spellings of them.")
    if hit != str(plane):
        notes.append(f"plane={plane!r} read as the reserved datum plane {hit!r}")
    return hit


def _read_direction(tool: str, param: str, value, default: tuple) -> tuple:
    """A [x,y,z] direction from a vector OR a signed axis/side word, never from a guess.

    The old code silently substituted +Z for anything that was not a 3-list, so
    draft_analysis(pull_dir=[1,0]) reported a Z-pull result under the caller's X-pull
    question. A malformed vector now says so instead."""
    if value is None:
        return default
    if isinstance(value, str):
        hit = _DIR_WORDS.get(value.strip().lower())
        if hit is None:
            raise ValueError(
                f"{tool}({param}={value!r}): pass a direction as [x,y,z] (e.g. [0,0,1]) or as a "
                f"signed axis / side word (+x/-x/+y/-y/+z/-z, top/bottom/left/right/front/back).")
        return hit
    if isinstance(value, dict):
        try:
            return (float(value["x"]), float(value["y"]), float(value["z"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"{tool}({param}={value!r}): a dict direction needs numeric x, y and z keys; "
                f"[x,y,z] or a signed axis word (\"+z\") also work.") from None
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError(
                f"{tool}({param}={list(value)!r}): a direction needs exactly 3 components "
                f"[x,y,z] — got {len(value)}. (This used to be silently replaced by [0,0,1], "
                f"so the answer described a different pull than the one you asked about.)")
        try:
            return (float(value[0]), float(value[1]), float(value[2]))
        except (TypeError, ValueError):
            raise ValueError(
                f"{tool}({param}={list(value)!r}): every component must be a number.") from None
    raise ValueError(
        f"{tool}({param}={value!r}): pass [x,y,z] or a signed axis word (\"+z\", \"bottom\").")


def _sketch_features(doc_id: str) -> list:
    """The document's sketch features (empty when it has none, or when the tree is unreadable)."""
    return [f for f in (_feature_rows(doc_id) or [])
            if isinstance(f, dict) and str(f.get("type") or "") == "sketch"]


def _sketch_roster(doc_id: str) -> str:
    """One clause naming every sketch the model may legally pass, or that there are none."""
    rows = _sketch_features(doc_id)
    if not rows:
        return f"{doc_id} has no sketch features yet — author one with add_features first."
    return ("Its sketch features are: "
            + ", ".join(f'{f.get("id")} "{f.get("name") or ""}"' for f in rows) + ".")


def _teach_unknown_sketch(e: Exception, doc_id: Optional[str]) -> Exception:
    """"no sketch feature 's1'" names the symptom; the document's actual sketch roster — or the
    fact that it has none — names the fix."""
    msg = str(e)
    if not doc_id or "no sketch feature" not in msg.lower():
        return e
    rows = _feature_rows(doc_id)
    if rows is None:
        return e
    sketches = _sketch_features(doc_id)
    if sketches:
        text = (f"{msg} in {doc_id}. {_sketch_roster(doc_id)} Pass one of those ids, "
                f'e.g. check_sketch(sketch="{sketches[0].get("id")}", doc_id="{doc_id}").')
    else:
        text = (f"{msg}: {doc_id} has {len(rows)} feature(s) and NOT ONE of them is a sketch, so "
                f"there is nothing for check_sketch to solve. Author a sketch first — "
                f'add_features(doc_id="{doc_id}", ops=[{{"op":"add_feature","feature":'
                '{"type":"sketch","name":"Sk","params":{"plane":"top"},"entities":[…]}}]) — '
                f'or call get_part(doc_id="{doc_id}") to see what the tree really holds.')
    return CoreError(text, getattr(e, "code", "") or "invalid_op",
                     {"doc_id": doc_id, "reason": "unknown sketch",
                      "sketches": [f.get("id") for f in sketches]})


def register_cad_eval_tools(mcp: FastMCP) -> None:
    """CAD query/measure/check MCP tools (T-RF8 split of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""


    @mcp.tool()
    def query_geometry(query: Optional[Union[str, dict]] = None,
                       doc_id: Optional[str] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Resolve a STRUCTURED face/edge query against the part — explore/confirm geometry.

        `query` is a dict of query keys — a whole sentence is NOT a query (see the note below):
          of             "face" | "edge" — defaults to "face"
          side           top/bottom/left/right/front/back(rear) or +x/-x/+y/-y/+z/-z — the
                         extreme face/edge that way (top=+Z, right=+X, front=-Y, back=+Y)
          surf           faces: flat/round/cylindrical/conical/spherical/toroidal ("round" = ANY
                         curved face); edges: straight/round/circular/elliptical
          parallel       "x"|"y"|"z" — keep those whose axis is parallel to it. A FACE's axis is
          perpendicular  its NORMAL, an EDGE's is its DIRECTION, so a vertical wall face is
                         perpendicular:"z" while a vertical edge is parallel:"z".
          pick           largest/smallest (area), longest/shortest (length), inner/outer
                         (bore vs boss, for coaxial round faces/edges)
          near/count/index   [x,y,z] nearest-first / cap the set / keep specific ordinals

        Examples:
          {"of":"face","side":"top"}                                     the top face
          {"of":"face","surf":"cylinder","side":"bottom","pick":"outer"} outer barrel, bottom end
          {"of":"edge","surf":"line","parallel":"z"}                     the vertical straight edges
          {"of":"edge","surf":"line","near":[40,0,2],"count":1}          exactly ONE edge, by position

        ACCEPTED SPELLINGS (all normalised to the dict above, and echoed back in `interpreted`
        so you always see what was actually asked):
          query={...} / query="top"        the canonical form
          target= / q= / selector= / where=   the same value under another name
          face="top" / edge="longest"      the alias also pins `of` to face / edge
          of="face", side="top", …         the query written FLAT across top-level kwargs —
                                           assembled into {"of":"face","side":"top"}
          surface= / shape=                spellings of `surf`
          limit= / max= / max_results=     spellings of `count` (a result cap)
        A spelling that is NOT query vocabulary (target="bushing_body") is read as the SUBJECT
        — the part or feature the query is about: an open document's id/name selects that
        document, a feature name is reported (this tool always searches the whole document),
        and anything else is reported as unresolved instead of quietly steering the query.

        Returns each matching face/edge with its shape (surf), side, centroid, size and stable
        sid, so you can confirm "the top face" resolves before you reference it in a feature or a
        simulation. CONVENIENCE: a short phrase built only from the words above ("top", "outer
        cylindrical face at bottom") is normalised into those keys; anything else is rejected
        with the vocabulary rather than silently mis-resolved. For open-ended exploration call
        describe_part (the named face/edge roster) then hover(sid) — not this tool with a sentence.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            q, subjects = _read_query("query_geometry", query, extra, notes)
            # Output-shape flags asking for something this tool ALREADY does. Every match carries
            # centroid/normal/axis unconditionally, so return_coords=True is a request that is
            # already satisfied — refusing the whole call over it costs the model a round trip and
            # teaches it nothing it can act on (measured live 2026-07-29:
            # query_geometry(query="all planar faces", return_coords=True)). Drop them WITH a note
            # so the answer still says what happened; this is a closed list, never a catch-all.
            for flag in ("return_coords", "return_coordinates", "include_coords",
                         "include_coordinates", "with_coords", "with_coordinates",
                         "include_centroid", "return_centroid", "measure", "measures",
                         "include_area", "return_area", "include_size"):
                if flag in extra:
                    extra.pop(flag)
                    notes.append(f"ignored {flag}= — every match already carries centroid, "
                                 f"normal, axis, area_or_len and sid")
            _reject_unknown(
                "query_geometry", extra,
                'it takes query=, doc_id=, the query spellings (target/q/face/edge/selector/'
                'where) or the query keys written flat (of/side/surf/parallel/perpendicular/'
                'pick/near/count/limit/max/max_results/index/body_id).')

            # A non-vocabulary spelling names the SUBJECT. When it is an open document and no
            # doc_id was given, that IS the document the model meant — adopt it and say so.
            docs = _open_docs() if subjects else []

            def _as_doc(value) -> Optional[dict]:
                text = str(value).strip()
                return next((x for x in docs
                             if x.get("doc_id") == text
                             or (x.get("name") or "").strip().casefold() == text.casefold()),
                            None)

            adopted = None
            if doc_id is None:
                for name, value, _why in subjects:
                    hit = _as_doc(value)
                    if hit:
                        doc_id = hit["doc_id"]
                        adopted = (name, value)
                        notes.append(f"{name}={value!r} names the open part "
                                     f"{hit['doc_id']} \"{hit.get('name') or ''}\" — used as "
                                     f"doc_id=")
                        break

            d = _doc(doc_id)
            unresolved: list = []
            for name, value, _why in subjects:
                if adopted == (name, value):
                    continue
                hit = _as_doc(value)
                if hit and hit["doc_id"] == d:
                    notes.append(f"{name}={value!r} names this document ({d}) — read as the "
                                 f"subject, not as the query")
                    continue
                if hit:
                    raise ValueError(
                        f"query_geometry() was pointed at two different documents: doc_id={d} "
                        f"and {name}={value!r} (part {hit['doc_id']}). Pass one — "
                        f'doc_id="{hit["doc_id"]}" queries the part you named.')
                feats = _feature_rows(d) or []
                feat = next((f for f in feats if isinstance(f, dict)
                             and str(f.get("name") or "").strip().casefold()
                             == str(value).strip().casefold()), None)
                if feat:
                    notes.append(
                        f"{name}={value!r} names the feature {feat.get('name')!r} "
                        f"({feat.get('id')}) — query_geometry always searches the WHOLE "
                        f"document, so the matches below are document-wide; for just that "
                        f"feature's faces call inspect_part(target={feat.get('name')!r})")
                    continue
                unresolved.append(value)
                notes.append(
                    f"{name}={value!r} is neither geometry-query vocabulary nor the name of an "
                    f"open part or of a feature in {d} — it did NOT restrict this query. The "
                    f"matches below answer query={json.dumps(q, sort_keys=True)} over the whole "
                    f"document; call get_part or describe_part to see what is really there.")

            try:
                res = cb.call("doc_query", {"doc_id": d, "query": q})
            except CoreError as ce:
                raise _teach_no_bodies(
                    ce, d, f"query_geometry(query={json.dumps(q, sort_keys=True)})") from None
            # Every other tool reports `ok`; doc_query returns a bare match list, so a model
            # checking the usual success flag saw None and couldn't tell a hit from a miss.
            # Echo the RESOLVED query too: when a phrase was normalised into keys, the model
            # needs to see what it actually asked for before trusting the matches.
            out = {"ok": True, "query": q}
            out.update(res if isinstance(res, dict) else {"result": res})
            if not (out.get("matches") or []):
                out["note"] = ("no face/edge matched this query — loosen it (drop `side` or "
                               "`surf`) or call describe_part to see the actual roster")
            if unresolved:
                out["unresolved_subject"] = unresolved
            return _dumps(_interpreted(out, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "query_geometry(query=…)"))



    @mcp.tool()
    def check_sketch(sketch: Optional[str] = None, doc_id: Optional[str] = None,
                     **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Check whether a sketch is well-constrained (fully defined) and report what's missing.

        Solves the named sketch feature and returns its constraint health so you can author
        SolidWorks-quality sketches — explicit geometric RELATIONS (coincident, horizontal/vertical,
        parallel, perpendicular, tangent, equal, concentric, symmetric, midpoint, collinear) plus
        DRIVING DIMENSIONS — instead of loose geometry that only looks right.

        sketch: the sketch feature id (e.g. "s1") whose constraints to evaluate. Also accepted
          as sketch_id= / sketch_name= / feature= / feature_id= / id= / name= / target= (every
          rewrite is echoed in `interpreted`); a sketch id that does not exist comes back with
          the document's actual sketch roster, not just "no sketch feature".

        Returns:
          status       "full" (fully defined — the goal), "under" (under-defined), or "over"
          dof          remaining degrees of freedom (0 = fully defined)
          conflicting  constraint ids that contradict each other (an over-defined sketch) — remove one
          redundant    constraint ids implied by the others (harmless, but a redundant DRIVING
                       dimension over-defines — prefer a relation or delete the duplicate)
          summary      one-line plain-English verdict + what to add/remove to reach dof 0

        Workflow: after authoring a sketch, READ the ASCII readback from the mutation response
        (or call render_sketch_ascii), then call this / sketch_health; if `under`, add the missing
        relations/driving dimensions and re-check; if `over`, drop a conflicting/redundant
        constraint. Aim for dof 0 so editing a driving dimension re-solves the geometry
        parametrically. Before declaring a sketch done: render_sketch_ascii + sketch_health /
        check_sketch with dof 0.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            sketch = _one_alias("check_sketch", "sketch", extra,
                                ("sketch_id", "sketch_name", "sketch_fid", "feature",
                                 "feature_id", "id", "name", "target"), sketch, notes)
            _reject_unknown("check_sketch", extra,
                            'it takes sketch= (the sketch feature id, e.g. "s1") and doc_id=.')
            d = _doc(doc_id)
            if not sketch:
                raise ValueError(
                    "check_sketch() needs sketch= — the sketch FEATURE id to evaluate (e.g. "
                    f'check_sketch(sketch="s1", doc_id="{d}")). {_sketch_roster(d)}')
            # sketch_health is the read-only source of truth (never touches the interactive
            # active_sketch state). It returns status/dof plus the narrated read; its
            # conflicting/redundant are [{id,type,suggestion}] objects, from which we pull the
            # ids for check_sketch's id-list contract. (The old "doc_sketch_solve"/"sketch_fid"
            # call never dispatched — wrong method + key — so check_sketch silently errored.)
            r = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            status = r.get("status", "")
            dof = r.get("dof", 0)

            def _ids(items):
                out = []
                for it in items or []:
                    if isinstance(it, dict) and it.get("id") is not None:
                        out.append(it["id"])
                    elif isinstance(it, str):
                        out.append(it)
                return out

            conflicting = _ids(r.get("conflicting"))
            redundant = _ids(r.get("redundant"))
            narration = r.get("dof_narration")
            if conflicting:
                summary = (f"Over-defined: {len(conflicting)} conflicting constraint(s) "
                           f"{conflicting} — remove one to resolve the contradiction.")
            elif status == "full" or dof == 0:
                summary = "Fully defined (dof 0) — every entity is pinned by relations + dimensions."
            else:
                summary = (f"Under-defined: {dof} degree(s) of freedom remain — add geometric "
                           f"relations and/or driving dimensions until dof reaches 0.")
                if narration:
                    summary += f" Free: {narration}"
                if redundant:
                    summary += f" (Also {len(redundant)} redundant: {redundant}.)"
            out = {"ok": True, "doc_id": d, "sketch": sketch, "status": status,
                   "dof": dof, "fully_defined": (status == "full" or dof == 0),
                   "conflicting": conflicting, "redundant": redundant, "summary": summary,
                   # narrated enrichment straight from sketch_health (the advisor source).
                   "open_endpoints": (r.get("closure") or {}).get("open_endpoints"),
                   "dof_narration": narration, "schedule": r.get("schedule"),
                   "advisory": r.get("advisory")}
            return _dumps(_interpreted(out, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_unknown_sketch(_teach_no_bodies(
                e, d, f"check_sketch(sketch={sketch!r})"), d))



    @mcp.tool()
    def measure(targets: Optional[list] = None,
                doc_id: Optional[str] = None,
                view: Optional[Union[str, dict]] = "auto",
                annotate: bool = True,
                **kwargs: Annotated[Any, Field(default=None)]) -> list:
        """Color-coded multi-element measurement with an annotated render.

        Resolves each plain-English target (or resolved selector dict — same vocabulary as
        inspect_part / measure_between / query_geometry), measures via the engine's
        `eval_measure`, paints every measured element a DISTINCT colour from the shared
        10-colour emphasis palette (per_face mode), and returns [image, caption] so vision
        and non-vision models both get the reading.

        targets: non-empty list of face/edge/vertex selectors — e.g. ["top",
          {"of":"edge","surf":"circle","pick":"largest"}, {"of":"face","side":"front"}].
          One target → that element's intrinsic measure (length / area / diameter / …).
          Two targets → also the distance (+ angle when applicable) between them.
          Three+ → chain multi-distance across the ordered refs.
          ACCEPTED SPELLINGS (echoed in the caption's `interpreted`): a bare selector instead
          of a list (targets="top" → ["top"]); target= / element(s)= / ref(s)= / selector(s)= /
          query= / faces= / edges= as other names for targets=; and the two-element form
          written apart, measure(a="top", b="bottom") → targets=["top","bottom"].
        view: "auto" (default — frame along the measured elements' normals) or a named /
          custom camera like render_part / inspect_part.
        annotate: pass face labels / triad through to the underlying render (default True).

        Caption carries structured readings as JSON: each element gets
        {label, kind, value, unit, color, rgb, sid?, anchor?} plus an optional `between`
        block when 2+ refs yield a distance/angle. Prefer this over measure_between when
        you want the same colour-coded highlight view the Evaluate ribbon shows.

        Returns [image, caption].
        """
        extra = _extra_kwargs(dict(kwargs))
        notes: list = []
        targets = _one_alias("measure", "targets", extra,
                             ("target", "elements", "element", "refs", "ref", "selectors",
                              "selector", "queries", "query", "faces", "edges"),
                             targets, notes)
        a_val = _one_alias("measure", "targets[0]", extra,
                           ("a", "a_query", "from", "first"), None, notes)
        b_val = _one_alias("measure", "targets[1]", extra,
                           ("b", "b_query", "to", "second"), None, notes)
        if a_val is not None or b_val is not None:
            if targets is not None:
                raise ValueError(
                    "measure() got targets= AND the split a=/b= form — pass one: "
                    "targets=['top','bottom'].")
            targets = [v for v in (a_val, b_val) if v is not None]
        if isinstance(targets, (str, dict)):
            notes.append(f"targets={targets!r} is a single selector — wrapped as [{targets!r}]")
            targets = [targets]
        elif isinstance(targets, tuple):
            targets = list(targets)
        _reject_unknown("measure", extra,
                        "it takes targets= (a list of selectors), doc_id=, view= and annotate=.")
        d = _doc(doc_id)
        if not isinstance(targets, list) or not targets:
            raise ValueError(
                "measure(targets=[...]) needs a non-empty list of plain-English selectors "
                "(e.g. ['top', {'of':'edge','parallel':'z','pick':'largest'}]). A single "
                "selector on its own (targets='top'), or the pair written apart "
                "(a='top', b='bottom'), is accepted too.")

        refs: list = []
        matches: list = []
        labels: list = []
        for t in targets:
            ref, label = _resolve_measure_target(d, t)
            refs.append(ref)
            matches.append(ref if isinstance(ref, dict) else {"kind": "face"})
            # Prefer the resolved selector dict for emphasis projection when available.
            if isinstance(ref, dict):
                matches[-1] = ref
            labels.append(label)

        # Batch measure (all refs at once) — single / pair / multi-distance.
        try:
            batch = cb.call("eval_measure", {"doc_id": d, "refs": refs})
        except CoreError as e:
            taught = _teach_no_bodies(e, d, f"measure(targets={targets!r})")
            if taught is not e:
                raise taught from None
            raise ValueError(
                f"eval_measure failed: {e}. The selectors resolved to {refs!r} — re-check them "
                f"with query_geometry (each must name ONE face/edge/vertex) and retry.") from e
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"eval_measure failed: {e}") from e

        # Per-element intrinsic readings with index-distinct palette colours.
        palette = _build_emphasis_palette(matches, mode="per_face")
        readings: list = []
        for i, (ref, label) in enumerate(zip(refs, labels)):
            entry = palette["entries"][i] if i < len(palette["entries"]) else {}
            rgb = tuple(entry.get("rgb") or _EMPHASIS_PALETTE[i % len(_EMPHASIS_PALETTE)])
            color_name = _emphasis_color_name(rgb)
            if len(refs) == 1:
                mres = batch if isinstance(batch, dict) else {}
            else:
                try:
                    mres = cb.call("eval_measure", {"doc_id": d, "refs": [ref]})
                except Exception as e:  # noqa: BLE001
                    mres = {"kind": "error", "error": str(e)}
            readings.append(_reading_from_measure(
                mres if isinstance(mres, dict) else {},
                label=label, color_name=color_name, rgb=rgb,
                sid=(ref.get("sid") if isinstance(ref, dict) else None)))

        between = None
        if len(refs) >= 2 and isinstance(batch, dict):
            between = _between_from_batch(batch)

        # Render + per_face emphasis so each measured element is colour-coded.
        try:
            part_bb = cb.call("doc_bbox", {"doc_id": d})
        except CoreError as e:
            taught = _teach_no_bodies(e, d, f"measure(targets={targets!r})")
            if taught is not e:
                raise taught from None
            raise ValueError(f"doc_bbox failed: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"doc_bbox failed: {e}") from e
        if not part_bb or not part_bb.get("size"):
            raise ValueError(
                f"{d} has no bounding box, so there is no geometry to frame or measure. Add "
                f"geometry first — {_add_box_call(d)} — then retry measure(targets={targets!r}).")

        target_bb = _target_bbox(d, matches)
        view_dir = _camera_dir_for_target(matches, view)
        png_full = _tmp(".png")
        r = cb.call("doc_render_view", {
            "doc_id": d, "out_path": png_full,
            "view": {"dir": list(view_dir)}, "annotate": bool(annotate),
        })
        if not r.get("ok") or not os.path.exists(png_full):
            r2 = cb.call("doc_render_png",
                         {"doc_id": d, "out_path": png_full, "annotate": bool(annotate)})
            if not r2.get("ok") or not os.path.exists(png_full):
                raise ValueError(
                    f"measure render failed for {d}: "
                    f"{r.get('reason', r.get('error', 'no bodies'))}. The readings above could "
                    f"not be turned into a picture — call render_part(doc_id=\"{d}\") to see "
                    f"whether the part renders at all, or measure_between for a text-only "
                    f"reading of the same selectors.")
            view_dir = _vnorm((1.0, 1.0, 0.8))

        rect = _crop_rect_for_target(part_bb, target_bb, view_dir,
                                    margin=0.18, zoom_factor=1.35)
        png_crop = _tmp(".png")
        _crop_png_to_rect(png_full, rect, png_crop)
        png_out, palette = _apply_emphasis_overlay(
            png_crop, d, matches, part_bb, view_dir, mode="per_face",
            palette=palette)


        # Human caption + structured block (non-vision models read the JSON).
        lines = []
        for rd in readings:
            val = rd.get("value")
            if isinstance(val, float):
                val_s = f"{val:.6g}"
            else:
                val_s = str(val) if val is not None else "?"
            unit = rd.get("unit") or ""
            lines.append(
                f"  [{rd.get('color')}] {rd.get('label')}: "
                f"{rd.get('kind')} = {val_s}{' ' + unit if unit else ''}")
        if between:
            bval = between.get("value")
            bval_s = f"{bval:.6g}" if isinstance(bval, float) else str(bval)
            angle_txt = ""
            if between.get("angle") is not None:
                angle_txt = f", angle={between['angle']:.4g} deg"
            lines.append(
                f"  between: {between.get('kind')} = {bval_s}"
                f"{' ' + (between.get('unit') or '') if between.get('unit') else ''}"
                f"{angle_txt}")
        structured = {
            "ok": True,
            "doc_id": d,
            "readings": readings,
            "between": between,
            "n_targets": len(refs),
            "palette_mode": "per_face",
        }
        _interpreted(structured, notes)
        caption = (
            f"MEASURE ({len(refs)} target{'s' if len(refs) != 1 else ''}):\n"
            + "\n".join(lines)
            + _palette_caption_block(palette)
            + f" [measure_json={json.dumps(structured, separators=(',', ':'), default=str)}]"
        )
        return [Image(path=png_out), caption]



    @mcp.tool()
    def measure_part(doc_id: Optional[str] = None, density_g_cm3: float = 7.85,
                     **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        density_g_cm3 sets the material density used for mass (default 7.85 = steel). Also
        accepted as density= / density_g_cc= / material_density= / rho= (echoed in
        `interpreted`). Also runs a geometry sanity check (closed/valid solid).

        Multi-body docs: the top-level volume/mass/com/inertia are the COMBINED total of ALL
        bodies, and `mass.bodies[]` lists each body's own properties (with `mass.body_count`).
        Pass body_id via a direct query if you need just one. (Previously this silently reported
        only the last body of an assembly.)

        A document with no solid says WHICH situation it is — empty, or every feature failed to
        rebuild — and gives the call that fixes it.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            density = _one_alias("measure_part", "density_g_cm3", extra,
                                 ("density", "density_g_cc", "density_gcm3", "material_density",
                                  "rho"), None, notes)
            if density is not None:
                try:
                    density_g_cm3 = float(density)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"measure_part(density_g_cm3={density!r}): density is a NUMBER in g/cm^3 "
                        "(steel 7.85, aluminium 2.70, ABS 1.04). To pick a material by name use "
                        "set_material instead.") from None
            # A "what should I measure" argument is ambiguous, and the two readings need OPPOSITE
            # answers, so the VALUE decides — not the key.
            #  * what="mass" / "volume" / "centre of mass" names something measure_part ALREADY
            #    returns. Routing that away is wrong: measured live 2026-07-29 (sim pass 2b) the
            #    first version of this block sent measure_part(what="mass") to measure_between,
            #    which cannot answer it, and cost two calls.
            #  * what="the hole diameter" names GEOMETRY, which measure_part genuinely cannot do
            #    (measured live 2026-07-29, drawing pass 2) — that one must route.
            asked = next((k for k in ("what", "target", "feature", "of", "query", "measure",
                                      "measures", "measurements", "properties", "props",
                                      "report", "values", "queries", "dimension", "element")
                          if k in extra), None)
            if asked is not None:
                wanted = " ".join(str(extra[asked]).lower().split())
                if _MASS_PROPERTY_RE.search(wanted):
                    extra.pop(asked)
                    notes.append(f"ignored {asked}={wanted!r} — measure_part always reports "
                                 f"volume, mass, centre of mass and inertia together")
                    # A combined ask ("mass and bounding box") gets its mass half answered here;
                    # say where the other half lives rather than leaving it unmentioned.
                    if _BBOX_INTENT_RE.search(wanted):
                        notes.append("the bounding box is NOT part of mass properties — "
                                     "describe_part(doc_id) reports it as `bbox WxDxH mm`")
                else:
                    raise ValueError(
                        f"measure_part({asked}={extra[asked]!r}) — measure_part reports the WHOLE "
                        f"part's mass properties (volume, mass, centre of mass, inertia) and takes "
                        f"no geometry target. To measure one face/edge or the distance between "
                        f"two, call measure_between(a_query=…[, b_query=…]); query_geometry(query) "
                        f"first lists the candidates and their sids, and describe_part(doc_id) "
                        f"gives the part's bbox plus the named face/edge roster.")
            _reject_unknown("measure_part", extra,
                            "it takes doc_id= and density_g_cm3= only.")
            d = _doc(doc_id)
            mass = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": density_g_cm3})
            out = {"ok": True, "doc_id": d, "mass": mass}
            try:
                out["check"] = cb.call("eval_check", {"doc_id": d})
            except CoreError:
                pass
            return _dumps(_interpreted(out, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "measure_part()"))



    # ---------------------------------------------------------------- inspection

    @mcp.tool()
    def measure_between(a_query: Optional[Union[str, dict]] = None,
                        b_query: Optional[Union[str, dict]] = None,
                        doc_id: Optional[str] = None,
                        **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Measure a face/edge — or the distance + angle BETWEEN two of them — in plain words.

        This is the engineer's measure/probe tool. Address geometry the same way you do
        everywhere else (a side word like "top", or a full query {"of":"face","side":"top"} /
        {"of":"edge","parallel":"z","pick":"largest"}).

        One query (b_query omitted) -> that single feature's intrinsic measure:
          * a face   -> its area (mm^2)
          * an edge  -> its length (mm), or radius (mm) for a circular edge
          * a vertex -> its point coordinates
        Two queries -> the minimum DISTANCE (mm) between them (with the two closest points) and,
        when applicable (face↔face normals or edge↔edge tangents), the ANGLE (degrees, 0..90).

        Each query must resolve to exactly ONE face/edge. If it matches several (e.g. "all the
        vertical edges"), narrow it with a predicate ("pick":"largest"/"side":"top"/…) — call
        query_geometry(query) first to see the candidates. Returns a readable `summary` line.

        ACCEPTED SPELLINGS (echoed in `interpreted`): a= / from= / first= / target= for
        a_query=, b= / to= / second= for b_query=, and the pair given as one list —
        measure_between(targets=["top","bottom"]) or a_query=["top","bottom"].
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            a_query = _one_alias("measure_between", "a_query", extra,
                                 ("a", "from", "first", "target", "query", "selector"),
                                 a_query, notes)
            b_query = _one_alias("measure_between", "b_query", extra,
                                 ("b", "to", "second"), b_query, notes)
            pair = _one_alias("measure_between", "a_query/b_query", extra,
                              ("targets", "refs", "selectors", "queries", "elements"),
                              None, notes)
            if pair is not None:
                if a_query is not None or b_query is not None:
                    raise ValueError(
                        "measure_between() got the pair BOTH as a list and as a_query/b_query — "
                        "pass one form.")
                a_query = pair
            if isinstance(a_query, (list, tuple)):
                items = list(a_query)
                if not 1 <= len(items) <= 2:
                    raise ValueError(
                        f"measure_between(): a list of selectors must hold 1 or 2 entries (got "
                        f"{len(items)}). For three or more use measure(targets=[…]).")
                notes.append(f"the selector list {items!r} was split into a_query/b_query")
                a_query = items[0]
                b_query = items[1] if len(items) == 2 else b_query
            _reject_unknown("measure_between", extra,
                            "it takes a_query=, b_query= and doc_id=.")
            if a_query is None:
                raise ValueError(
                    'measure_between() needs at least a_query= — the face/edge to measure (e.g. '
                    'a_query="top", or a_query={"of":"edge","pick":"longest"}). Add b_query= to '
                    "get the distance/angle between two of them.")
            d = _doc(doc_id)
            ref_a = _resolve_one_selector(d, a_query)
            refs = [ref_a]
            if b_query is not None:
                refs.append(_resolve_one_selector(d, b_query))
            res = cb.call("eval_measure", {"doc_id": d, "refs": refs})
            kind = res.get("kind")
            out = {"ok": True, "doc_id": d, "a_query": a_query, **res}
            if b_query is not None:
                out["b_query"] = b_query
                parts = []
                if "distance" in res:
                    parts.append(f"distance {res['distance']:.4g} mm")
                if "angle" in res:
                    parts.append(f"angle {res['angle']:.4g} deg")
                out["summary"] = ("between the two selections: " + ", ".join(parts)) if parts \
                    else "no distance/angle computed"
            else:
                if kind == "area":
                    out["summary"] = f"area = {res.get('area'):.6g} mm^2"
                elif kind == "length":
                    out["summary"] = f"length = {res.get('length'):.6g} mm"
                elif kind == "radius":
                    out["summary"] = f"radius = {res.get('radius'):.6g} mm"
                elif kind == "point":
                    out["summary"] = f"point = {res.get('points')}"
                elif kind == "volume":
                    out["summary"] = f"volume = {res.get('volume'):.6g} mm^3"
                else:
                    out["summary"] = f"measured ({kind})"
            return _dumps(_interpreted(out, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, f"measure_between(a_query={a_query!r})"))



    @mcp.tool()
    def check_interference(doc_id: Optional[str] = None,
                           include_coincident: Optional[bool] = None,
                           min_clearance_mm: Optional[float] = None,
                           **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Clash/clearance check — do the bodies OVERLAP, and if not, how tight is the gap?

        Runs a pairwise interference analysis over every body in the document (for an assembly
        doc, over the world-placed component bodies). For each pair it reports either an
        INTERFERENCE (overlap volume in mm^3 — a real collision the design must not have) or a
        CLEARANCE (minimum gap in mm). Use it to validate that mating/instanced parts fit without
        clashing, or to find the tightest gap in an assembly. For overlap WITHIN a single part's
        own features (two bosses merged, a cut swallowing a boss) use check_feature_overlap
        instead — this tool only sees whole bodies/components.

        include_coincident: also classify near-zero-clearance face-touching pairs (distance
        <= 1e-6 mm) as `contacts` entries instead of clearances — the "coincident faces" case
        SolidWorks flags separately (two parts touching exactly, not overlapping or gapped).
        min_clearance_mm: tag any clearance pair tighter than this with a "below min clearance"
        warning (e.g. flag a press-fit gap that's actually too tight to assemble by hand). Also
        accepted as min_clearance= / clearance= / clearance_mm= / min_gap_mm=, and
        include_coincident= as coincident= / include_contacts= (echoed in `interpreted`).

        Returns {ok, summary, parts:[{name,bbox,volume}], interferences:[{a,b,volume}],
        clearances:[{a,b,distance}], contacts:[{a,b,distance}]}. ok is True only when NOTHING
        interferes. A single-body part trivially reports no interference (build a multi-body
        part with the `transform` copy feature, or open an assembly, to get meaningful pairs).
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            coincident = _one_alias(
                "check_interference", "include_coincident", extra,
                ("coincident", "include_contacts", "contacts"), include_coincident, notes)
            clearance = _one_alias(
                "check_interference", "min_clearance_mm", extra,
                ("min_clearance", "clearance", "clearance_mm", "min_gap_mm", "min_gap"),
                min_clearance_mm, notes)
            try:
                min_clearance_mm = 0.0 if clearance is None else float(clearance)
            except (TypeError, ValueError):
                raise ValueError(
                    f"check_interference(min_clearance_mm={clearance!r}): the minimum "
                    "clearance is a NUMBER of millimetres, e.g. min_clearance_mm=0.2."
                ) from None
            include_coincident = bool(coincident)
            _reject_unknown("check_interference", extra,
                            "it takes doc_id=, include_coincident= and min_clearance_mm=.")
            d = _doc(doc_id)
            res = cb.call("eval_interference", {"doc_id": d, "include_coincident": include_coincident,
                                                "min_clearance_mm": min_clearance_mm})
            return _dumps(_interpreted({"ok": res.get("ok", True), "doc_id": d, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "check_interference()"))



    @mcp.tool()
    def check_feature_overlap(doc_id: Optional[str] = None, features: Optional[list] = None,
                              **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Intra-PART feature-vs-feature overlap — do two of THIS part's own features collide?

        Tests each feature's own pre-boolean tool solid (the shape it added/removed BEFORE it
        got fused/cut into the body) against every other feature's tool, so it catches things a
        single compiled solid can't show you: two bosses that silently merged (add+add), a
        redundant cut carved twice over the same region (cut+cut), or — the dangerous one — a
        later cut that erased an EARLIER boss entirely (add+cut where the common volume ≈ the
        boss's whole volume). Use this for one part's feature tree; use check_interference for
        separate BODIES or assembly components.

        features (optional): restrict the scan to pairs involving these feature ids (e.g. the
        ids you just added) instead of the whole tree — add_features already runs this
        automatically on each new batch and surfaces it as `overlap_warnings` in its return, so
        you rarely need to call this yourself right after building; call it explicitly to
        re-check the whole part or a specific subset. A single id on its own (features="f2") is
        accepted and wrapped into a list, as are feature= / feature_ids= / ids= (echoed in
        `interpreted`).

        Returns {ok, summary, pairs:[{a, b, a_name, b_name, kind, volume_mm3}], dead_features}.
          kind "boss_overlap"   — two ADDITIVE features share volume (often intentional — a
                                  boss deliberately overlapping a rib — but flagged as a warning).
          kind "cut_overlap"    — two cuts remove overlapping material (a redundant cut, usually
                                  a modeling mistake worth cleaning up).
          kind "cut_into_boss"  — a cut removes PART of an earlier additive feature.
          kind "boss_swallowed" — a cut removes essentially the WHOLE earlier boss (>=99% of its
                                  volume) — almost certainly an error; this is the ONLY kind that
                                  sets ok:false.
        dead_features: feature ids that built successfully but own NO surviving faces (fully
        consumed by a later op) — a feature silently doing nothing right now.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            features = _one_alias("check_feature_overlap", "features", extra,
                                  ("feature", "feature_ids", "feature_id", "ids", "id"),
                                  features, notes)
            if isinstance(features, (str, dict)):
                notes.append(f"features={features!r} is a single feature — wrapped as a list")
                features = [features]
            elif isinstance(features, tuple):
                features = list(features)
            _reject_unknown("check_feature_overlap", extra,
                            "it takes doc_id= and features= (a list of feature ids).")
            d = _doc(doc_id)
            params: dict = {"doc_id": d}
            if features is not None:
                params["features"] = features
            res = cb.call("eval_feature_overlap", params)
            return _dumps(_interpreted({"ok": res.get("ok", True), "doc_id": d, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "check_feature_overlap()"))



    @mcp.tool()
    def check_symmetry(plane: Optional[str] = None, doc_id: Optional[str] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Is the part mirror-symmetric about a principal datum plane? Returns the deviation.

        plane : "top" (XY, mirror across Z), "front" (XZ, mirror across Y), or "right" (YZ,
                mirror across X) — the reserved Chudware datum planes (Z-up). The plane may be
                spelled as its axis pair ("XY"/"xz"/"YZ"), as a normal axis ("z"/"+x"), or as
                about= / datum= / mirror_plane= / about_plane=; every rewrite is echoed in
                `interpreted`, and an unreadable name lists the three legal planes.
        Mirrors the active body across the plane and measures the symmetric-difference volume.
        Returns {symmetric: bool, deviation_mm3}: symmetric is True when the leftover volume is
        within a tiny tolerance (1e-3 of the part volume). Handy to confirm a feature you meant
        to place symmetrically really is, or to pick a sensible mirror plane.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            plane = _one_alias("check_symmetry", "plane", extra,
                               ("about", "about_plane", "datum", "datum_plane", "mirror_plane",
                                "plane_name", "axis"), plane, notes)
            _reject_unknown("check_symmetry", extra, "it takes plane= and doc_id=.")
            plane = _read_plane("check_symmetry", plane, notes)
            d = _doc(doc_id)
            res = cb.call("eval_symmetry", {"doc_id": d, "plane": plane})
            sym = res.get("symmetric")
            dev = res.get("deviation_mm3")
            summary = (f"symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)" if sym
                       else f"NOT symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)")
            return _dumps(_interpreted(
                {"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, f'check_symmetry(plane="{plane or "top"}")'))



    @mcp.tool()
    def inspect_section(plane: Optional[str] = None, doc_id: Optional[str] = None,
                        **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Cut the part with a datum plane and report the CROSS-SECTION area + section moments.

        This is the "section the part to inspect its internals" probe. It slices the active
        body with one of the reserved datum planes and computes the real properties of the
        resulting cut face — the cross-section a draftsman would dimension.

        plane : "top" (XY, normal +Z), "front" (XZ, normal -Y), or "right" (YZ, normal +X) —
                the reserved Chudware datum planes (Z-up). The cut passes through the origin.
                Spellable as its axis pair ("XY"/"xz"/"YZ"), as a normal axis ("z"/"+x"), or as
                section= / at= / datum= / plane_name= — every rewrite is echoed in
                `interpreted`, and an unreadable name lists the three legal planes instead of
                dying on "not a reserved planar datum".

        Returns {ok, area_mm2, centroid:[x,y,z], Ixx, Iyy, Ixy, summary}:
          * area_mm2 — the cross-section area (mm^2), the headline number. For a solid this is
            the filled region; for a thin/hollow wall it's the material the plane cuts through.
          * centroid — the area centroid of the section, in world coordinates.
          * Ixx/Iyy/Ixy — the section second moments of area (mm^4), expressed in the cutting
            plane's frame (use for bending stiffness / section modulus reasoning).
        Use it to verify a beam's cross-section, check a wall's cut area, or feed a hand
        bending calc. A plane that misses the body (empty cut) returns area 0.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            plane = _one_alias("inspect_section", "plane", extra,
                               ("section", "section_plane", "at", "datum", "datum_plane",
                                "plane_name", "axis"), plane, notes)
            _reject_unknown("inspect_section", extra, "it takes plane= and doc_id=.")
            plane = _read_plane("inspect_section", plane, notes)
            d = _doc(doc_id)
            res = cb.call("eval_section_props", {"doc_id": d, "plane": plane})
            area = res.get("area_mm2", 0.0)
            if area and area > 0:
                summary = f"cross-section area at plane '{plane}' = {area:.6g} mm^2"
            else:
                summary = (f"plane '{plane}' produced no closed cross-section (the cut may miss "
                           f"the body or only graze an edge) — area = 0. The cut always passes "
                           f"through the ORIGIN, so a part modelled away from it sections empty: "
                           f"check where the body sits with measure_part (centre of mass) and "
                           f"try another plane, e.g. inspect_section(plane=\"front\").")
            return _dumps(_interpreted(
                {"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, f'inspect_section(plane="{plane or "top"}")'))



    @mcp.tool()
    def wall_thickness(doc_id: Optional[str] = None) -> str:
        """How THIN is the thinnest wall? Inward ray-cast wall-thickness over the whole part.

        From every surface sample it casts a ray straight into the material and measures the
        distance to the far wall — the local wall thickness. The headline value is the MINIMUM
        ("the thinnest wall is X mm"), the number that governs whether a part can be molded,
        machined, or 3D-printed, and where it's likely to be weak or warp.

        Returns {ok, min_thickness_mm, mean_thickness_mm, max_thickness_mm, n_sampled,
        aicm_path, summary}:
          * min_thickness_mm — the thinnest wall found (the key value). null if no sample got a
            through-wall hit (e.g. an open sheet body with no opposite wall).
          * mean/max — the average and thickest sampled wall.
          * aicm_path — a temp field file for the in-app thickness colour-map viewer.
        Compare min_thickness_mm against your process minimum (e.g. ~1 mm injection wall,
        ~0.8 mm FDM) to flag thin spots before manufacturing.
        """
        d = None
        try:
            d = _doc(doc_id)
            res = cb.call("eval_thickness", {"doc_id": d})
            mn = res.get("min_thickness_mm")
            if mn is not None:
                mean = res.get("mean_thickness_mm")
                summary = (f"thinnest wall = {mn:.4g} mm"
                           + (f" (mean {mean:.4g} mm)" if mean is not None else ""))
            else:
                summary = ("no through-wall thickness could be measured (open/sheet body, or no "
                           "opposite wall in line of sight) — every inward ray left the material "
                           f"without hitting a far wall. Confirm {d} really holds a closed solid "
                           "with measure_part (a valid solid reports a volume), and if the part "
                           "is meant to be hollow, shell it first (add_features with a shell op) "
                           "so there ARE two walls to measure between.")
            return _dumps({"ok": True, "doc_id": d, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "wall_thickness()"))



    @mcp.tool()
    def draft_analysis(pull_dir: Optional[list] = None, min_draft_deg: float = 3.0,
                       doc_id: Optional[str] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Moldability check: which faces lack enough DRAFT to release from the mold?

        For a given mold-pull (ejection) direction it measures each face's draft angle — how far
        its surface tilts away from the pull axis. Faces whose worst-case draft is below
        min_draft_deg would drag on the tool during ejection and need a draft feature added.

        pull_dir : the mold-opening / ejection direction as [x,y,z] (defaults to +Z, [0,0,1]).
                   A signed axis or side word is accepted too — "+z"/"-z"/"x"/"bottom"/"front" —
                   and the SIGN is kept (pulling -Z is not the same question as +Z). Also
                   spellable as pull= / direction= / pull_direction= / ejection_dir= / axis=.
                   A malformed vector (two components, a non-number) is now an error naming the
                   accepted forms: it used to be replaced by [0,0,1] in silence, so the answer
                   described a different pull than the one asked about.
        min_draft_deg : the minimum acceptable draft angle in degrees (default 3°, a common
                        injection-molding minimum). Also angle_deg= / min_angle= / draft_deg= /
                        threshold_deg= / min_draft=.
        Every rewrite is echoed back in `interpreted`.

        Returns {ok, min_draft_deg, max_draft_deg, threshold_deg, n_faces_below_threshold,
        faces_below_threshold:[{face_id, min_draft_deg, area_mm2, centroid}], aicm_path,
        summary}. n_faces_below_threshold == 0 means every face clears the threshold (good to
        mold along that direction). The faces_below_threshold list (centroid + area) tells you
        WHERE to add draft. aicm_path is the colour-map field for the in-app viewer.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            pull_dir = _one_alias("draft_analysis", "pull_dir", extra,
                                  ("pull", "direction", "pull_direction", "ejection_dir",
                                   "eject_dir", "axis", "dir"), pull_dir, notes)
            angle = _one_alias("draft_analysis", "min_draft_deg", extra,
                               ("angle_deg", "min_angle", "min_angle_deg", "draft_deg",
                                "threshold_deg", "min_draft", "draft_angle"), None, notes)
            _reject_unknown("draft_analysis", extra,
                            "it takes pull_dir=, min_draft_deg= and doc_id=.")
            if angle is not None:
                try:
                    min_draft_deg = float(angle)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"draft_analysis(min_draft_deg={angle!r}): the draft minimum is a NUMBER "
                        "of degrees, e.g. min_draft_deg=3.") from None
            pull = list(_read_direction("draft_analysis", "pull_dir", pull_dir, (0.0, 0.0, 1.0)))
            if isinstance(pull_dir, str):
                notes.append(f"pull_dir={pull_dir!r} read as the direction {pull}")
            d = _doc(doc_id)
            res = cb.call("eval_draft",
                          {"doc_id": d, "pull": [float(x) for x in pull],
                           "angle_deg": float(min_draft_deg)})
            n_bad = res.get("n_faces_below_threshold", 0)
            thr = res.get("threshold_deg", min_draft_deg)
            if n_bad == 0:
                summary = (f"all faces have >= {thr:g}deg draft along pull {list(pull)} — "
                           f"moldable in that direction")
            else:
                summary = (f"{n_bad} face(s) below the {thr:g}deg draft minimum along pull "
                           f"{list(pull)} — add draft there before molding: add_features with a "
                           f'{{"type":"draft"}} op on those faces (their centroids are in '
                           f"faces_below_threshold), then re-run draft_analysis")
            return _dumps(_interpreted({"ok": True, "doc_id": d, "pull_dir": list(pull),
                                        "summary": summary, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "draft_analysis()"))


    # ---------------------------------------------------------------- solid health
    # The verification gap: nothing answered "is this body actually a sound closed
    # solid?", so an agent committed to an FEA mesh / a CAM toolpath / a STEP-STL export
    # and only learned the body was open from a confusing downstream failure — or, worse,
    # not at all. eval_solid_health (core/doc/session.cpp) computes the census; this tool
    # turns it into findings that name the offending sid, and REFUSES to pretend it ran a
    # check the kernel does not have.

    _REPAIR_KNOBS = ("repair", "fill_holes", "fix_normals", "fix_winding")

    def _health_report(doc_id, body, body_id, repair, fill_holes, fix_normals,
                       fix_winding, called_as: str) -> str:
        d = None
        try:
            asked_repair = [n for n, v in zip(
                _REPAIR_KNOBS, (repair, fill_holes, fix_normals, fix_winding)) if v]
            which = body if body is not None else body_id
            if which is not None:
                try:
                    which = int(which)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{called_as}(body={which!r}): body is a 1-BASED body number "
                        "(body=1 is the first solid). Omit it to check every body.") from None
            d = _doc(doc_id)
            res = cb.call("eval_solid_health",
                          {"doc_id": d, "body": int(which) if which is not None else 0})

            bodies = res.get("bodies") or []
            for b in bodies:
                bid = b.get("body_id")
                findings: list = []
                free = b.get("free_edges") or {}
                nonman = b.get("non_manifold_edges") or {}
                tiny = b.get("tiny_edges") or {}
                sliver = b.get("sliver_faces") or {}
                if b.get("solids", 0) < 1:
                    findings.append(
                        "no SOLID was produced — this body is a loose face/shell, so it has "
                        "no inside. Nothing downstream (FEA mesh, CAM stock removal, STEP "
                        "solid export) can consume it. Fix the feature that produced it: an "
                        "extrude/revolve of an OPEN profile makes a surface, not a solid.")
                if free.get("count"):
                    findings.append(
                        f"{free['count']} FREE (boundary) edge(s) — each bounds exactly one "
                        f"face, so the skin has a hole there. Offending edges: "
                        f"{', '.join(free.get('sids') or [])}. Hover/measure them to see "
                        f"where the skin opens, then fix the feature that left the gap "
                        f"(a shell that removed a face it should not have, a loft/sweep "
                        f"that failed to cap, an imported surface that was never sewn). "
                        f"Seam edges of a cylinder/sphere are NOT counted here.")
                if nonman.get("count"):
                    findings.append(
                        f"{nonman['count']} NON-MANIFOLD edge(s) — shared by MORE than two "
                        f"faces, so the body pinches to a knife at that edge and is not a "
                        f"valid manifold solid. Offending edges: "
                        f"{', '.join(nonman.get('sids') or [])}. This is almost always two "
                        f"features meeting exactly tangentially or a boolean of touching "
                        f"bodies — nudge one of them so they overlap or clear properly.")
                orient = b.get("orientation") or {}
                if orient.get("checked") and orient.get("consistent") is False:
                    findings.append(
                        f"face NORMALS are inconsistently oriented: {orient.get('problems')}. "
                        f"An inside-out patch makes exports and meshers disagree about which "
                        f"side is material.")
                if b.get("brep_valid") is False:
                    findings.append(
                        "BRepCheck reports the B-rep itself as INVALID (bad geometry/topology "
                        "on some sub-shape) — run check_geometry (eval_check) for the "
                        "per-status list.")
                if b.get("volume_mm3") is None:
                    findings.append(
                        f"volume was NOT computed — {b.get('volume_reason')}. Any mass, CG or "
                        f"inertia number you were about to quote for this body does not exist "
                        f"yet; make it a solid first (surf_thicken / surf_knit a closed set of "
                        f"panels).")
                elif not b.get("volume_positive"):
                    findings.append(
                        f"volume is {b.get('volume_mm3')} mm^3 — not positive. A zero volume "
                        f"means no enclosed material; a NEGATIVE volume is the classic "
                        f"inverted/inside-out solid tell. Do not mesh or export this body.")
                if tiny.get("count"):
                    findings.append(
                        f"{tiny['count']} tiny edge(s) < 0.01 mm ({', '.join(tiny.get('sids') or [])})"
                        f" — legal topology, but they blow up mesh element counts and often "
                        f"mark a near-degenerate fillet/boolean.")
                if sliver.get("count"):
                    findings.append(
                        f"{sliver['count']} sliver face(s) < 0.01 mm^2 "
                        f"({', '.join(sliver.get('sids') or [])}) — same story: meshers choke "
                        f"on them.")
                b["findings"] = findings
                b["verdict"] = (
                    f"body {bid}: CLOSED MANIFOLD SOLID — {b.get('shells')} shell, "
                    f"{b.get('faces')} faces, {b.get('edges')} edges, 0 free, 0 non-manifold, "
                    f"volume {b.get('volume_mm3')} mm^3"
                    if b.get("sound") else
                    f"body {bid}: NOT SOUND — " + "; ".join(f.split(" —")[0] for f in findings))

            # Never imply a check we did not run. The engine marks these checked:false;
            # surface them at the TOP level so a model skimming the answer cannot read
            # "watertight" as "verified free of self-intersection".
            not_checked: list = []
            for b in bodies:
                si = b.get("self_intersection") or {}
                if si.get("checked") is False and not any(
                        x.startswith("self_intersection") for x in not_checked):
                    not_checked.append(f"self_intersection: {si.get('reason')}")
                orient = b.get("orientation") or {}
                if orient.get("checked") is False:
                    not_checked.append(
                        f"orientation (body {b.get('body_id')}): {orient.get('reason')}")
                if b.get("brep_valid") is None:
                    not_checked.append(
                        f"brep_valid (body {b.get('body_id')}): "
                        f"{b.get('brep_valid_reason')}")
                if b.get("volume_mm3") is None:
                    not_checked.append(
                        f"volume (body {b.get('body_id')}): {b.get('volume_reason')}")

            out = {"ok": True, "doc_id": d,
                   "watertight": bool(res.get("all_watertight")),
                   "is_watertight": bool(res.get("all_watertight")),
                   "sound": bool(res.get("all_sound")),
                   "body_count": res.get("body_count"),
                   "bodies": bodies,
                   "not_checked": not_checked}

            if asked_repair:
                out["repair"] = {
                    "performed": False,
                    "refused": (
                        f"{called_as}({'=True, '.join(asked_repair)}=True) — NOT done, and "
                        "nothing was changed. This is a parametric B-rep kernel, not a mesh "
                        "repairer: there is no hole-filling / normal-flipping pass to run, "
                        "and bolting one on would hide the real defect in your feature tree. "
                        "Fix the FEATURE that produced the bad body — the findings above name "
                        "the offending edge/face sids; hover(sid) or measure them, then edit "
                        "(add_features op=edit_feature) or delete the feature responsible and "
                        "re-run this check.")}

            heads = []
            if asked_repair:
                heads.append("repair REFUSED (nothing was modified) — see repair.refused")
            bad = [b for b in bodies if not b.get("sound")]
            if not bodies:
                heads.append("no bodies to check")
            elif not bad:
                heads.append(
                    f"{len(bodies)} body(ies): every one is a closed manifold solid with "
                    f"positive volume "
                    f"({', '.join(str(b.get('volume_mm3')) + ' mm^3' for b in bodies)})")
            else:
                heads.append(f"{len(bad)} of {len(bodies)} body(ies) UNSOUND: " +
                             " | ".join(b["verdict"] for b in bad))
            if not_checked:
                heads.append("NOT CHECKED: " + " | ".join(
                    x.split(":", 1)[0] for x in not_checked))
            out["summary"] = ". ".join(heads)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, f"{called_as}()"))

    @mcp.tool()
    def check_geometry_health(doc_id: Optional[str] = None,
                              body: Optional[int] = None,
                              body_id: Optional[int] = None,
                              repair: bool = False,
                              fill_holes: bool = False,
                              fix_normals: bool = False,
                              fix_winding: bool = False) -> str:
        """Is this solid actually SOUND? Watertight / closed-manifold verdict + the counts that prove it.

        Run this BEFORE you commit to anything expensive or irreversible — an FEA mesh, a CAM
        toolpath, a STEP/STL/3MF export, a mass/CG number you will quote. All of those assume a
        closed manifold solid and none of them check; the first symptom of a bad body is a
        confusing downstream failure or a silently wrong export.

        Per body it reports:
          watertight / is_watertight   closed manifold solid? (seam edges of a cylinder/sphere
                                       are legitimate B-rep topology and are NOT counted as holes)
          free_edges                   {count, sids} — boundary edges bounding ONE face: holes
          non_manifold_edges           {count, sids} — edges shared by MORE than two faces
          shells / solids / faces / edges / vertices, seam_edges, degenerate_edges
          tiny_edges / sliver_faces    {count, sids} — legal, but they wreck meshes
          orientation                  face-normal consistency per shell (checked:false + a
                                       reason when the body has no shell to orient)
          brep_valid                   BRepCheck_Analyzer verdict
          volume_mm3 / volume_positive a zero or NEGATIVE volume is the inverted-solid tell
          self_intersection            ALWAYS {checked:false, reason} — this kernel exposes no
                                       single-body self-intersection test. A watertight verdict
                                       is topological and does NOT clear a body of self-
                                       intersection. Use check_interference for body-vs-body.
          findings                     one actionable sentence per defect, naming the sids
          verdict                      the one-line answer
        `not_checked` at the top level lists every check that did NOT run, so a passing
        `watertight` can never be mistaken for a clean bill of health.

        doc_id : the part (defaults to the active document).
        body   : 1-based body number; omit to check EVERY body (also spelled body_id=).
        repair / fill_holes / fix_normals / fix_winding : accepted so the trimesh-style call
          you were about to write does not die on an unknown argument — but they are REFUSED,
          not performed. This is a parametric B-rep kernel: the fix for a bad body is to fix
          the FEATURE that produced it, which the findings point you at.

        Aliases (same tool): is_watertight, assess_mesh_quality, analyze_stl_geometry,
        health_check, design_audit.
        """
        return _health_report(doc_id, body, body_id, repair, fill_holes, fix_normals,
                              fix_winding, "check_geometry_health")

    # The spellings models already write for this question, resolved to the ONE
    # implementation above rather than reimplemented. `is_watertight` is the trimesh verb;
    # the rest are the namings observed across the CAD/mesh MCP ecosystem.
    for _alias_name in ("is_watertight", "assess_mesh_quality", "analyze_stl_geometry",
                        "health_check", "design_audit"):

        def _make_alias(alias: str):
            def _health_alias(doc_id: Optional[str] = None,
                              body: Optional[int] = None,
                              body_id: Optional[int] = None,
                              repair: bool = False,
                              fill_holes: bool = False,
                              fix_normals: bool = False,
                              fix_winding: bool = False) -> str:
                return _health_report(doc_id, body, body_id, repair, fill_holes,
                                      fix_normals, fix_winding, alias)
            _health_alias.__name__ = alias
            return _health_alias

        mcp.tool(name=_alias_name, description=(
            f"Alias of check_geometry_health — solid health: watertight / closed-manifold "
            f"verdict per body with the free-edge, non-manifold-edge, shell/face counts and "
            f"offending sids that justify it, plus volume sign. Identical arguments and "
            f"identical answer; call check_geometry_health directly if you prefer. Repair "
            f"arguments are refused, not performed."))(_make_alias(_alias_name))
