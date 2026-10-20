"""CAD query/measure/check MCP tools (T-RF8 split of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from pydantic import Field
from typing import Annotated
from typing import Any
from typing import Literal
from typing import Optional
from typing import Union
import json
import math
import os
import re

from .cad_common import (
    CoreError,
    _BBOX_INTENT_RE,
    _CLOSEUP_H,
    _CLOSEUP_W,
    _EMPHASIS_PALETTE,
    _QVOCAB_HELP,
    _SID_RE,
    _annotate_render,
    _as_face_query,
    _bbox_from_points,
    _between_from_batch,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _doc,
    _dumps,
    _emphasis_color_name,
    _err,
    _ideal_views,
    _palette_caption_block,
    _publish_current,
    _reading_from_measure,
    _render_view_png,
    _resolve_feature_ref,
    _resolve_material_density,
    _resolve_measure_target,
    _resolve_one_selector,
    _roster_text,
    _target_bbox,
    _target_feature_roster,
    already_resolved_selector,
    is_geom_query,
    normalize_geom_query,
    note_interference_check,
    render_output_path,
    resolve_readback_selector,
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

# `measure_part` is normally mass-properties only, but live models also use it as a generic
# "measure this part" verb. These exact whole-part wall-thickness spellings name the existing
# evaluate operation, not a face/edge selector. Keep this deliberately closed: a phrase such as
# "hole thickness" still needs an explicit geometry query rather than a guessed target.
_WALL_THICKNESS_INTENTS = frozenset({
    "thickness",
    "wall thickness",
    "minimum thickness",
    "minimum wall thickness",
    "min thickness",
    "min wall thickness",
    "thinnest wall",
    "thinnest wall thickness",
})


def _wall_thickness_intent(value) -> bool:
    """Whether an argument value unambiguously asks for whole-part wall thickness."""
    if not isinstance(value, str):
        return False
    normal = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    return normal in _WALL_THICKNESS_INTENTS


def _reject_unknown(tool: str, kwargs: dict, accepted: str) -> None:
    """Leftover kwargs are a real mistake — name them and the parameters that DO exist."""
    if not kwargs:
        return
    raise ValueError(
        f"{tool}() got unknown argument(s) "
        + ", ".join(f"{k}={kwargs[k]!r}" for k in sorted(kwargs))
        + f" — {accepted}")


def _measure_ref(doc_id: str, target):
    """Use the same owner-preserving resolver for text and annotated measurements."""
    ref, _label = _resolve_measure_target(doc_id, target)
    return ref


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


_QUERY_STATE_RECENT_FEATURES = 8


def _query_model_state(doc_id: str) -> dict:
    """Return a compact current feature roster to bind geometry matches to this model revision."""
    state = cb.call("doc_tree", {"doc_id": doc_id}) or {}
    tree = state.get("tree") or {}
    features = tree.get("features") if isinstance(tree, dict) else []
    features = features if isinstance(features, list) else []
    total = len(features)
    recent = features[-_QUERY_STATE_RECENT_FEATURES:]
    recent_rows = [
        {
            "id": feature.get("id"),
            "name": feature.get("name"),
            "type": feature.get("type"),
            "suppressed": bool(feature.get("suppressed", False)),
        }
        for feature in recent if isinstance(feature, dict)
    ]
    return {
        "revision": state.get("revision"),
        "feature_count": total,
        "recent_features": recent_rows,
        "recent_features_limit": _QUERY_STATE_RECENT_FEATURES,
        "recent_features_returned": len(recent_rows),
        "recent_features_omitted": total - len(recent_rows),
        "recent_features_truncated": total > _QUERY_STATE_RECENT_FEATURES,
    }


def _doc_body_count(doc_id: str) -> int:
    """Solids in the document, from doc_query's per-match body ordinal (1 if unstamped)."""
    faces = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}) or {}
             ).get("matches") or []
    return max((int(m["body"]) for m in faces
                if isinstance(m, dict) and m.get("body") is not None), default=1)


def _eval_thickness_all_bodies(doc_id: str) -> tuple[dict, list]:
    """(merged eval_thickness reply, [(body ordinal, field handle), ...]).

    eval_thickness measures ONE body — the last, unless `body` names one — so on a
    part with a second solid every other body went unmeasured and the "thinnest wall"
    could be anything but. A single-body part keeps the engine's reply byte-for-byte.
    """
    count = _doc_body_count(doc_id)
    if count <= 1:
        res = cb.call("eval_thickness", {"doc_id": doc_id})
        handle = res.get("aicm_path")
        return res, ([(1, str(handle))] if handle else [])
    per = [(b, cb.call("eval_thickness", {"doc_id": doc_id, "body": b}))
           for b in range(1, count + 1)]
    measured = [(b, r) for b, r in per if r.get("min_thickness_mm") is not None]
    n_total = sum(int(r.get("n_sampled") or 0) for _, r in per)
    min_body, thinnest = min(measured, key=lambda br: br[1]["min_thickness_mm"],
                             default=per[-1])
    weighted = sum(float(r.get("mean_thickness_mm") or 0) * int(r.get("n_sampled") or 0)
                   for _, r in measured)
    n_measured = sum(int(r.get("n_sampled") or 0) for _, r in measured)
    basis = dict(thinnest.get("measurement_basis") or {})
    if basis:
        for key in ("n_vertices", "n_unmeasured"):
            basis[key] = sum(int((r.get("measurement_basis") or {}).get(key) or 0)
                             for _, r in per)
    minimum_sample = thinnest.get("minimum_sample")
    merged = {
        "aicm_path": thinnest.get("aicm_path"),
        "max_thickness_mm": max((r["max_thickness_mm"] for _, r in measured
                                 if r.get("max_thickness_mm") is not None), default=None),
        "mean_thickness_mm": (weighted / n_measured) if n_measured else None,
        "min_thickness_mm": thinnest.get("min_thickness_mm") if measured else None,
        "n_sampled": n_total,
        "measurement_basis": basis,
        "minimum_sample": ({**minimum_sample, "body": min_body}
                           if isinstance(minimum_sample, dict) else None),
        "bodies": [{"body": b, "min_thickness_mm": r.get("min_thickness_mm")} for b, r in per],
    }
    return merged, [(b, str(r["aicm_path"])) for b, r in measured if r.get("aicm_path")]


def _wall_thickness_result(doc_id: str) -> dict:
    """Build the canonical whole-part wall-thickness response for every route."""
    res, fields = _eval_thickness_all_bodies(doc_id)
    mn = res.get("min_thickness_mm")
    if mn is not None:
        mean = res.get("mean_thickness_mm")
        summary = (f"minimum sampled local wall span = {mn:.4g} mm"
                   + (f" (mean {mean:.4g} mm)" if mean is not None else "")
                   + "; includes edge tapers, not a verified structural wall minimum")
    else:
        summary = ("no accepted inward-ray wall span — rays can miss an exit, graze a face, "
                   "or fall below the sampling cutoff; this does not establish wall thickness "
                   "or safety")
    # Keep this insertion order identical to the long-standing wall_thickness response.
    out = {"ok": True, "doc_id": doc_id, "summary": summary, **res}
    out["measurement_caveat"] = (
        "Tessellated surface-normal ray spans include real local taper at acute convex edges. "
        "A boundary minimum is neither a verified structural wall thickness nor evidence that "
        "the part is safe; a dihedral angle alone cannot dismiss it. Inspect sections at and "
        "near the reported location and the relevant load path. Unsampled regions and spans "
        "below the acceptance cutoff remain unresolved.")
    if mn is not None and fields:
        try:
            out["thinnest"] = _thinnest_wall_faces(doc_id, fields)
            minimum_sample = out.get("minimum_sample")
            if isinstance(minimum_sample, dict):
                minimum_sample = {**minimum_sample,
                                  "body": minimum_sample.get("body", 1)}
                for spot in out["thinnest"]:
                    if (spot["body"] == minimum_sample["body"]
                            and spot["vertex_index"] == minimum_sample["vertex_index"]):
                        minimum_sample.update({key: spot[key] for key in
                                               ("sample_location", "face", "surf", "built_by")
                                               if key in spot})
                        break
                out["minimum_sample"] = minimum_sample
        except Exception as exc:  # noqa: BLE001 — the measurement itself still stands
            out["thinnest_unavailable"] = f"{type(exc).__name__}: {exc}"
    return out


_THINNEST_FACES = 3


def _thinnest_wall_faces(doc_id: str, fields: list) -> list:
    """WHERE the thinnest walls are: the lowest-thickness faces, each with its thinnest sample.

    eval_thickness samples the tessellation vertices and writes the per-vertex thickness
    into its colour-map field. A bare minimum sent agents bisecting the tree by suppressing
    features one at a time to find the face that owned it (c219: three sub-agents, ~25
    add_features + wall_thickness round trips). Each field holds one body (always labelled
    1 inside the field); `fields` pairs it with the document's body ordinal. Its FACE ids are
    that body's TopExp index — the `topo` of doc_query and the second half of every owner_map
    pair — so each spot is named by face sid and by the feature whose rebuild produced that
    face. That is the raw owner_map join, not describe_part's de-smeared one: the de-smear
    costs seconds per call on a 90-face part, and "the feature that last rebuilt this
    face" is the one to edit.
    """
    import numpy as np

    from services.aicm_handoff import read_handoff
    from services.sim.tessellate import read_aicm

    candidates = []
    for body_no, handle in fields:
        field = read_aicm(read_handoff(handle))
        bodies = field.get("bodies") or []
        for scalar in field.get("scalars") or []:
            body = next((b for b in bodies if b["body_id"] == scalar.get("body_id")), None)
            values = np.asarray(scalar.get("values"), dtype=float)
            if body is None or len(values) != body["n_verts"]:
                continue
            tri_face = np.zeros(body["n_tris"], dtype=np.int64)
            for face in body["faces"]:
                tri_face[face["first_tri"]:face["first_tri"] + face["tri_count"]] = \
                    face["face_id"]
            vert_face = np.zeros(body["n_verts"], dtype=np.int64)
            vert_face[body["idx"].reshape(-1)] = np.repeat(tri_face, 3)
            order = np.argsort(values, kind="stable")
            order = order[np.isfinite(values[order])]
            # First occurrence of each face in ascending-thickness order = its minimum.
            _, first = np.unique(vert_face[order], return_index=True)
            triangles = body["idx"].reshape(-1, 3)
            for vi in order[np.sort(first)][:_THINNEST_FACES]:
                incident = triangles[np.any(triangles == vi, axis=1)]
                _, edge_counts = np.unique(incident[incident != vi], return_counts=True)
                location = ("face_mesh_boundary" if np.any(edge_counts == 1)
                            else "face_mesh_interior")
                candidates.append((float(values[vi]), int(body_no), int(vert_face[vi]),
                                   int(vi), [round(float(c), 3) for c in body["pos"][vi]],
                                   location))
    ranked = sorted(candidates)[:_THINNEST_FACES]
    if not ranked:
        return []

    # The thickness field's (body_id, face_id) is the same (body ordinal, per-body TopExp
    # index) key owner_map pairs and doc_query's body/topo stamps use, so every body joins.
    feats, owner_map = _target_feature_roster(doc_id)
    names = {str(f.get("id")): f.get("name") for f in feats}
    faces = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}) or {}
             ).get("matches") or []
    by_key = {(int(m["body"]), int(m["topo"])): m for m in faces
              if isinstance(m, dict) and m.get("body") is not None
              and m.get("topo") is not None}
    built_by = {(int(pair[0]), int(pair[1])): str(fid) for fid, om in owner_map.items()
                for pair in (om.get("faces") or [])
                if isinstance(pair, list) and len(pair) == 2}
    spots = []
    for thickness, body_id, face_id, vertex_index, point, location in ranked:
        spot = {"thickness_mm": round(thickness, 4), "at": point, "body": body_id,
                "vertex_index": vertex_index, "sample_location": location}
        match = by_key.get((body_id, face_id))
        if match is not None:
            spot["face"] = match.get("sid")
            spot["surf"] = match.get("surf")
        owner = built_by.get((body_id, face_id))
        if owner:
            spot["built_by"] = {"id": owner, "name": names.get(owner)}
        spots.append(spot)
    return spots


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
    # A dict query never went through _QUERY_KEY_CANON, and the engine ignores a key it
    # does not know — so `shape` silently stopped filtering and `sid` silently matched the
    # whole body. Canonicalise and refuse a query that cannot select.
    merged, guard_notes = normalize_geom_query(merged, where=f"{tool}()")
    notes.extend(guard_notes)
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
          near           [x,y,z] a point ON (or the centroid of) the face/edge you mean —
                         ranked by distance to its actual geometry, ties to the nearer centroid
          count/index    cap the set / keep specific ordinals

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

        Returns each matching face/edge with its shape (surf), side, signed outward normal,
        centroid, size and stable sid (round faces add radius, concavity and axis_point — a point
        ON the cylinder/cone axis; torus faces such as sweep bends instead add major_radius,
        minor_radius, torus_axis and torus_center — bend radius, tube radius, bend axis and
        bend centre), so you can confirm "the top face" resolves before you
        reference it in a feature or a simulation. CONVENIENCE: a short phrase built only from
        the words above ("top", "outer cylindrical face at bottom") is normalised into those
        keys; anything else is rejected with the vocabulary rather than silently mis-resolved.
        For open-ended exploration call describe_part (the named face/edge roster) then
        hover(sid) — not this tool with a sentence.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            # A sid IS an element, not a query. This tool hands sids out on every match and
            # the docs tell the model to address the element by one, so asking about a sid
            # here has exactly one meaning. Live claude-opus-5 2026-08-04:
            # query_geometry(doc_id=…, sid="F1b1b80") answered "needs a geometry query".
            sid = extra.pop("sid", None)
            if sid is None and isinstance(query, dict) and set(query) == {"sid"}:
                sid, query = query["sid"], None
            if sid is None and isinstance(query, str) and _SID_RE.fullmatch(query.strip()):
                sid, query = query.strip(), None
            if isinstance(sid, str) and sid.strip():
                d = _doc(doc_id)
                match = resolve_readback_selector(d, {"sid": sid.strip()})
                notes.append(f'sid="{sid.strip()}" names ONE element — resolved it against '
                             f"this document instead of reading it as a query predicate "
                             f"(the engine has no `sid` predicate)")
                return _dumps(_interpreted(
                    {"ok": True, "doc_id": d, "query": {"sid": sid.strip()},
                     "count": 1, "matches": [match]}, notes))
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
            out = {"ok": True, "doc_id": d, "query": q}
            out.update(res if isinstance(res, dict) else {"result": res})
            # Geometry selectors are revision-sensitive. Return a deliberately bounded tree
            # fingerprint with every successful query so the caller can tell whether a later
            # mutation invalidated these sids without receiving every feature's parameters.
            out["doc_id"] = d
            out["model_state"] = _query_model_state(d)
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
            dof = r.get("dof")
            from services.mcp.ascii2d import live_sketch_design_intent
            intent = live_sketch_design_intent(d, sketch, r)

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
            fully_defined = (dof == 0 and status != "over" and not conflicting
                             and not intent["pinned_only"] and not r.get("skipped_constraints"))
            if conflicting:
                summary = (f"Over-defined: {len(conflicting)} conflicting constraint(s) "
                           f"{conflicting} — remove one to resolve the contradiction.")
            elif intent["pinned_only"]:
                summary = ("Pinned instead of constrained (pinned_only): remove profile fix "
                           "relations, add design-intent relations/driving dimensions, then "
                           "sketch_fully_define. DOF 0 from blanket pins is not fully defined.")
            elif fully_defined:
                summary = "Fully defined (dof 0) — constrained parametrically, not fix-pinned."
            elif r.get("skipped_constraints"):
                summary = "Constraints were skipped — correct their refs/type before declaring fully defined."
            else:
                summary = (f"Under-defined: {dof} degree(s) of freedom remain — add geometric "
                           f"relations and/or driving dimensions until dof reaches 0.")
                if narration:
                    summary += f" Free: {narration}"
                if redundant:
                    summary += f" (Also {len(redundant)} redundant: {redundant}.)"
            out = {"ok": True, "doc_id": d, "sketch": sketch, "status": status,
                   "dof": dof, "fully_defined": fully_defined,
                   "conflicting": conflicting, "redundant": redundant, "summary": summary,
                   # narrated enrichment straight from sketch_health (the advisor source).
                   "open_endpoints": (r.get("closure") or {}).get("open_endpoints"),
                   "dof_narration": narration, "schedule": r.get("schedule"),
                   "advisory": r.get("advisory")}
            out.update(intent)
            out["skipped_constraints"] = r.get("skipped_constraints") or []
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

        # Native readbacks are in world coordinates even when the input was a
        # local selector on a rotated/translated assembly instance.
        if isinstance(batch, dict) and isinstance(batch.get("resolved_refs"), list):
            matches = batch["resolved_refs"]

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
                sid=(matches[i].get("sid") if i < len(matches) else None)))

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
        if view is None or view == "auto":
            best = _ideal_views(d, matches, target_bb, n=1)
            if best:
                view_dir = best[0]["dir"]
        # Frame the measured elements themselves: the engine fits their AABB, so a 5 mm feature
        # is rendered at 5 mm scale instead of cropped out of a whole-part shot and upscaled.
        png_out = render_output_path(f"{d}-measure", ".png")
        try:
            png_out, frame, _r = _render_view_png(
                d, view_dir, png_out, name="MEASURE", focus=target_bb,
                W=_CLOSEUP_W, H=_CLOSEUP_H, annotate=bool(annotate))
        except ValueError as e:
            raise ValueError(
                f"measure render failed for {d}: {e}. The readings above could not be turned "
                f"into a picture — call render_part(doc_id=\"{d}\") to see whether the part "
                f"renders at all, or measure_between for a text-only reading of the same "
                f"selectors.") from e

        png_out, ann = _annotate_render(
            png_out, d, view_dir, frame=frame, edges=True, callouts=False,
            scale=True, out_png=render_output_path(f"{d}-measure", ".png"),
            emphasis={"matches": matches, "mode": "per_face",
                      "label": "measure", "palette": palette})
        palette = ((ann.get("emphasis") or {}).get("palette")) or palette


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
    def measure_part(doc_id: Optional[str] = None, density_g_cm3: Optional[float] = None,
                     **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        Mass uses the material applied to the model: each body's set_material override, else
        the part material (assembly components use their source parts' materials). Only a body
        with no material falls back to 7.85 g/cm^3 steel. `mass.material` names the density
        and its `source` (body_material | document_material | component_material | default |
        explicit; `per_body` with `mixed:true` when bodies differ). density_g_cm3 overrides
        every body for this readout. It is also accepted as density= / density_g_cc= /
        material_density= / rho=, or as a NAME: material="aluminum" resolves its registry
        density, including density-only and orthotropic records. Also runs a geometry sanity
        check (closed/valid solid).

        Multi-body docs: the top-level volume/mass/com/inertia are the COMBINED total of ALL
        bodies, and `mass.bodies[]` lists each body's own properties (with `mass.body_count`).
        Pass body_id via a direct query if you need just one. (Previously this silently reported
        only the last body of an assembly.)

        Whole-part wall thickness is also accepted only under exact intent spellings — e.g.
        `measure_part(kind="thickness")` or `measure_part(what="wall thickness")` routes to
        `wall_thickness` and returns its exact structured result plus `interpreted`. A face/edge
        phrase remains a real geometry target and is refused rather than guessed.

        A document with no solid says WHICH situation it is — empty, or every feature failed to
        rebuild — and gives the call that fixes it.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            density = _one_alias("measure_part", "density_g_cm3", extra,
                                 ("density", "density_g_cc", "density_gcm3", "material_density",
                                  "rho"), density_g_cm3, notes)
            # A material NAME is the spelling a model reaches for once it has decided the
            # part is aluminium: resolve it against the density registry and echo the exact
            # density used. With neither, the engine applies the model's own materials.
            named = extra.pop("material", None)
            named = _one_alias("measure_part", "material", extra,
                               ("material_name", "mat"), named, notes)
            if named is not None and density is None:
                resolved = _resolve_material_density(named)
                density = resolved
                notes.append(f"material={named!r} resolved to density_g_cm3={resolved} — this "
                             f"readout uses it instead of the material applied to the model")
            elif named is not None:
                notes.append(f"ignored material={named!r} — density_g_cm3={density} was given "
                             f"explicitly and wins")
            if density is not None:
                try:
                    density = float(density)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"measure_part(density_g_cm3={density!r}): density is a NUMBER in g/cm^3 "
                        "(steel 7.85, aluminium 2.70, ABS 1.04), or pass material= with a name "
                        "from the material table.") from None
            # A "what should I measure" argument is ambiguous, and the two readings need OPPOSITE
            # answers, so the VALUE decides — not the key.
            #  * exact whole-part thickness spellings route to the dedicated wall-thickness probe;
            #  * what="mass" / "volume" / "centre of mass" names something measure_part ALREADY
            #    returns, so routing that away is wrong;
            #  * what="the hole diameter" names GEOMETRY, which measure_part cannot infer.
            asked = next((k for k in ("what", "target", "feature", "of", "query", "measure",
                                      "measures", "measurements", "properties", "props",
                                      "report", "values", "queries", "dimension", "element")
                          if k in extra), None)
            # `kind` is NOT a generic measure_part alias: only its exact thickness value has a
            # documented, unambiguous route. Keep unrelated kind= values on the refusal path.
            if asked is None and "kind" in extra:
                asked = "kind"
            route_wall_thickness = False
            if asked is not None:
                wanted = " ".join(str(extra[asked]).lower().split())
                if _wall_thickness_intent(extra[asked]):
                    extra.pop(asked)
                    route_wall_thickness = True
                    notes.append(f"{asked}={wanted!r} read as whole-part wall_thickness")
                elif asked == "kind":
                    raise ValueError(
                        f"measure_part(kind={extra[asked]!r}) only accepts kind=\"thickness\" "
                        "for the whole-part wall-thickness computation. For a face/edge "
                        "measurement, call measure_between(a_query=…[, b_query=…]).")
                elif _MASS_PROPERTY_RE.search(wanted):
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
            if route_wall_thickness:
                return _dumps(_interpreted(_wall_thickness_result(d), notes))
            params = {"doc_id": d}
            if density is not None:
                params["density_g_cm3"] = density
            mass = cb.call("eval_mass", params)
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
        Engine readback SIDs are accepted verbatim, but geometry edits can retire them.
        A dangling-selector error names live alternatives; when a selector also supplies
        its old centroid, alternatives are ranked by that location. Never treat those
        suggestions as automatic replacements: query the current geometry before retrying.

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
            ref_a = _measure_ref(d, a_query)
            refs = [ref_a]
            if b_query is not None:
                refs.append(_measure_ref(d, b_query))
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
    def fit_cylinder_axis(near: Optional[list] = None,
                          search_radius_mm: float = 10.0,
                          target: Optional[str] = None,
                          doc_id: Optional[str] = None,
                          view: Optional[Union[str, dict]] = "auto",
                          annotate: bool = True,
                          **kwargs: Annotated[Any, Field(default=None)]) -> list:
        """Least-squares CYLINDER AXIS from the geometry around a point — works on TESSELLATED
        imports where there is no analytic cylindrical face to query.

        This is how you find a hub bore, an axle, a bearing seat or a bolt shank on an
        imported STEP/mesh body so a component can be placed on MEASURED references instead of
        a guessed transform. Point it roughly at the feature and it fits the surrounding
        points:
          near             : [x,y,z] mm — a point on or near the cylindrical surface (required).
          search_radius_mm : how far around `near` to gather points (default 10 mm). Too small
                             starves the fit (n_points_used low), too large swallows
                             neighbouring geometry (rms_residual_mm blows up).
          target           : optional body name/id to restrict the fit to ONE body of a
                             multi-body part (also accepted as body=).

        Returns [image, caption]; the caption's `fit_cylinder_json` carries
        {axis_point, axis_dir, radius_mm, rms_residual_mm, n_points_used}. TRUST THE RESIDUAL:
        an rms_residual_mm that is a meaningful fraction of radius_mm means you did not fit a
        cylinder (wrong point, too big a search radius, or a chamfer/fillet in the window) —
        re-aim rather than place a part on it.

        Merge recipe: fit_cylinder_axis on the bore, fit_cylinder_axis on the shaft/axle, then
        rotate axis_dir → axis_dir and translate axis_point → axis_point (or feed the two axes
        to an asm_mate "concentric"), and finish with check_interference + measure_between.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            near = _one_alias("fit_cylinder_axis", "near", extra,
                              ("at", "point", "position", "location", "xyz"), near, notes)
            search_radius_mm = _one_alias(
                "fit_cylinder_axis", "search_radius_mm", extra,
                ("search_radius", "radius_mm", "radius", "window_mm"),
                search_radius_mm, notes)
            target = _one_alias("fit_cylinder_axis", "target", extra,
                                ("body", "body_name", "body_id", "part"), target, notes)
            _reject_unknown("fit_cylinder_axis", extra,
                            "it takes near=[x,y,z], search_radius_mm=, target= (a body), "
                            "doc_id=, view= and annotate=.")
            if isinstance(near, tuple):
                near = list(near)
            if not isinstance(near, list) or len(near) != 3:
                raise ValueError(
                    "fit_cylinder_axis(near=[x,y,z]) needs a 3-number point in mm on or near "
                    "the cylindrical surface to fit — e.g. near=[120.0, 0.0, 42.5]. Get a "
                    "starting point from describe_part's bores/sids, hover(sid), or "
                    "list_world_bodies' bbox centre.")
            near = [float(v) for v in near]
            try:
                search_radius_mm = float(search_radius_mm)
            except (TypeError, ValueError):
                raise ValueError(
                    f"fit_cylinder_axis(search_radius_mm={search_radius_mm!r}): the search "
                    "window is a NUMBER of millimetres, e.g. search_radius_mm=8.") from None
            if search_radius_mm <= 0.0:
                raise ValueError("fit_cylinder_axis(search_radius_mm=) must be > 0 mm.")
            d = _doc(doc_id)
            req: dict = {"doc_id": d, "near": near, "search_radius_mm": search_radius_mm}
            if target:
                req["body"] = str(target)
            try:
                res = cb.call("eval_fit_cylinder", req)
            except CoreError as e:
                taught = _teach_no_bodies(e, d, f"fit_cylinder_axis(near={near!r})")
                if taught is not e:
                    raise taught from None
                raise ValueError(
                    f"eval_fit_cylinder failed: {e}. Re-aim `near` at a point that really "
                    f"lies on the bore/shaft wall (describe_part lists the bores; hover(sid) "
                    f"gives a point on a face) and widen/narrow search_radius_mm.") from e
            axis_point = res.get("axis_point")
            axis_dir = res.get("axis_dir")
            radius = res.get("radius_mm")
            rms = res.get("rms_residual_mm")
            n_pts = res.get("n_points_used")
            structured = {"ok": res.get("ok", True), "doc_id": d, "near": near,
                          "search_radius_mm": search_radius_mm, "target": target,
                          "axis_point": axis_point, "axis_dir": axis_dir,
                          "radius_mm": radius, "rms_residual_mm": rms,
                          "n_points_used": n_pts}
            if isinstance(rms, (int, float)) and isinstance(radius, (int, float)) and radius:
                structured["residual_pct_of_radius"] = 100.0 * float(rms) / float(radius)
                if structured["residual_pct_of_radius"] > 2.0:
                    structured["fit_warning"] = (
                        "rms_residual_mm is more than 2% of radius_mm — this window is "
                        "probably not a single cylinder; re-aim `near` or shrink "
                        "search_radius_mm before placing anything on this axis")
            _interpreted(structured, notes)

            # Same annotated-render shape as `measure`: frame the fitted window so the reading
            # can be seen, not just trusted.
            focus = None
            if isinstance(axis_point, list) and len(axis_point) == 3:
                pad = max(float(radius or 0.0), search_radius_mm)
                focus = _bbox_from_points([[axis_point[i] - pad for i in range(3)],
                                           [axis_point[i] + pad for i in range(3)]])
            view_dir = _camera_dir_for_target([], view)
            if isinstance(axis_dir, list) and len(axis_dir) == 3 and (view is None or
                                                                      view == "auto"):
                # Look ACROSS the axis (not down it) so the fitted barrel is visible.
                ax = axis_dir
                cand = (1.0, 0.0, 0.0) if abs(ax[0]) < 0.8 else (0.0, 0.0, 1.0)
                view_dir = (ax[1] * cand[2] - ax[2] * cand[1],
                            ax[2] * cand[0] - ax[0] * cand[2],
                            ax[0] * cand[1] - ax[1] * cand[0])
                if not any(view_dir):
                    view_dir = _camera_dir_for_target([], "auto")
            png_out = render_output_path(f"{d}-fitcyl", ".png")
            png_out, frame, _r = _render_view_png(
                d, view_dir, png_out, name="FIT CYLINDER", focus=focus,
                W=_CLOSEUP_W, H=_CLOSEUP_H, annotate=bool(annotate))
            png_out, _ann = _annotate_render(
                png_out, d, view_dir, frame=frame, edges=True, callouts=False,
                scale=True, out_png=render_output_path(f"{d}-fitcyl", ".png"))

            def _f(v) -> str:
                return f"{float(v):.6g}" if isinstance(v, (int, float)) else "?"

            caption = (
                f"FIT CYLINDER near {near} (window {_f(search_radius_mm)} mm"
                f"{', body ' + str(target) if target else ''}):\n"
                f"  radius = {_f(radius)} mm, diameter = "
                f"{_f(radius * 2) if isinstance(radius, (int, float)) else '?'} mm\n"
                f"  axis_point = {axis_point}, axis_dir = {axis_dir}\n"
                f"  rms_residual = {_f(rms)} mm over {n_pts} point(s)"
                + (f"\n  WARNING: {structured['fit_warning']}"
                   if structured.get("fit_warning") else "")
                + f" [fit_cylinder_json="
                  f"{json.dumps(structured, separators=(',', ':'), default=str)}]")
            return [Image(path=png_out), caption]
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                f"fit_cylinder_axis(near={near!r}) failed: "
                f"{_teach_no_bodies(e, d, 'fit_cylinder_axis()')}") from e




    @mcp.tool()
    def check_interference(doc_id: Optional[str] = None,
                           include_coincident: Optional[bool] = None,
                           min_clearance_mm: Optional[float] = None,
                           members: Optional[list[str]] = None,
                           clearance_mode: Literal["exact", "threshold"] = "exact",
                           **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Clash/clearance check — do the bodies OVERLAP, and if not, how tight is the gap?

        Runs a pairwise interference analysis over every body in the document (for an assembly
        doc, over the world-placed component bodies). For each pair it reports either an
        INTERFERENCE (overlap volume in mm^3 — a real collision the design must not have) or a
        CLEARANCE. clearance_mode="exact" (default) measures each noninterfering pair's
        minimum gap. clearance_mode="threshold" classifies overlap and requested clearance,
        avoiding unrequested exact gap work. With zero minimum clearance and no coincident
        request, completed overlap classification never launches exact distance work.
        Positive clearance/contact requests still require exact distances when conservative
        bounds cannot decide them. Bounds are distance_lower_bound_mm, NOT measured gaps;
        threshold mode cannot claim a tightest gap. For overlap WITHIN a single part's own
        features use check_feature_overlap instead — this tool sees whole bodies/components.
        members: optional exact member names, e.g. ["b434", "b435"] for a multi-body part;
        assembly names match parts[].name. Checks only pairs WITHIN this selection. Unknown
        names are refused. Omit or pass [] to check all members. A scoped result does not
        verify placement of the rest of the document; scope and pair counts are explicit.

        include_coincident: also classify near-zero-clearance face-touching pairs (distance
        <= 1e-6 mm) as `contacts` entries instead of clearances — the "coincident faces" case
        SolidWorks flags separately (two parts touching exactly, not overlapping or gapped).
        min_clearance_mm: tag any clearance pair tighter than this with a "below min clearance"
        warning (e.g. flag a press-fit gap that's actually too tight to assemble by hand). Also
        accepted as min_clearance= / clearance= / clearance_mm= / min_gap_mm=, and
        include_coincident= as coincident= / include_contacts= (echoed in `interpreted`).

        Returns {ok, clear, complete, clearance_mode, summary, pairs_checked, pairs_total,
        parts:[{name,bbox,volume}], interferences:[{a,b,volume}], clearances, contacts}.
        A threshold clearance has distance_status="lower_bound" and distance_lower_bound_mm,
        or distance_status="exact" and distance; contacts carry exact distance.
        ok tells you if the check ran to completion. It does NOT tell you if the bodies clash.
        clear is the verdict: true when the complete check found no interference, false
        when it found interference or did not complete. A completed check that finds
        interference returns ok=true, clear=false and lists the pairs: that is a finding to
        fix, not a tool failure. Minimum-clearance violations stay warnings and do not change
        clear. ok=false means the check is incomplete (pair_failures/geometry_failures), was
        refused, or got bad arguments; never read it as collision-free. An EMPTY document is
        refused with reason="insufficient_bodies"; a single body returns ok with
        pairs_checked=0, which is not pairwise verification evidence.
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
            if clearance_mode not in ("exact", "threshold"):
                raise ValueError("check_interference: clearance_mode must be 'exact' or 'threshold'.")
            _reject_unknown("check_interference", extra,
                            "it takes doc_id=, include_coincident=, min_clearance_mm=, members= "
                            "and clearance_mode='exact'|'threshold'.")
            d = _doc(doc_id)
            params = {"doc_id": d, "include_coincident": include_coincident,
                      "min_clearance_mm": min_clearance_mm, "clearance_mode": clearance_mode}
            if members is not None:
                params["members"] = members
            res = cb.call("eval_interference", params)
            # Only a complete document-wide check verifies transform placement.
            if not members and res.get("complete", False):
                note_interference_check(d)
            # ok = the check ran to completion; clear = the complete check found no overlap.
            # Interference is a finding, so it never makes the tool call fail.
            complete = res.get("complete") is True
            out = {**res, "ok": complete, "doc_id": d,
                   "clear": complete and not res.get("interferences")}
            if complete and res.get("interferences"):
                out["summary"] = (f"Interference found: {res.get('summary', '')}. Move or "
                                  "change the bodies, then run check_interference again.")
            elif not complete and not res.get("reason"):
                out["error"] = res.get("summary") or "Interference check incomplete."
            return _dumps(_interpreted(out, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "check_interference()"))

    @mcp.tool()
    def inspect_model_integrity(
        doc_id: Optional[str] = None,
        proximity_mm: Annotated[float, Field(strict=True)] = 0.05,
        include_nearest: Annotated[bool, Field(strict=True)] = True,
        pair_budget_s: Annotated[float, Field(strict=True)] = 2.0,
    ) -> str:
        """Census every solid, including disconnected solids INSIDE assembly components.

        Read-only native inspection of part bodies and world-placed assembly sources:
        per-solid provenance, BRep validity, volume, centroid and bounds; exact near-pair
        distances/overlap volumes; geometric islands and isolated solid IDs. IDs identify
        THIS model revision only, not persistent references across edits.

        proximity_mm (finite 0..1000 mm) is a geometric search radius, NOT an acceptable
        clearance. Pairs are overlap, touching, or proximity: a positive gap is NEVER
        contact. include_nearest also finds the exact nearest solid for each isolated
        solid, even outside the radius. nearest[].status is exact, unknown, or
        no_other_solids; unknown distances stay null, and candidate_best_* is only partial
        evidence, not a proven minimum. pair_budget_s is a finite per-pair budget (0,60].

        Returns the native census intact: ok, complete, solids, non_solids, pairs,
        pair_failures, islands, isolated_solids, optional nearest, assembly, summary and
        limitations, plus inspection_summary and interpretation. ok means the QUERY ran,
        NOT geometry is safe; incomplete results cannot rule out omitted connections or
        overlaps. Pair failures or invalid sources make complete false.

        Islands describe geometric connectivity only: neither touching nor proximity
        proves physical attachment or a load path. Fixed/mate flags do not prove either.
        An isolated solid may be an intentional running-clearance part, not a floating
        error; verify its retention/joints and motion across the operating range. A
        proximity pair can be an intentional bearing clearance, not a connection to fuse.
        This does not verify engineering criteria, manufacturing safety, or interference
        placement, and does not save or modify the document.
        """
        try:
            for name, value, upper, zero_allowed in (
                ("proximity_mm", proximity_mm, 1000.0, True),
                ("pair_budget_s", pair_budget_s, 60.0, False),
            ):
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value > upper
                        or (value < 0 if zero_allowed else value <= 0)):
                    interval = f"[0, {upper:g}]" if zero_allowed else f"(0, {upper:g}]"
                    raise ValueError(
                        f"inspect_model_integrity: {name} must be a finite number in {interval}.")
            if not isinstance(include_nearest, bool):
                raise ValueError("inspect_model_integrity: include_nearest must be true or false.")
            d = _doc(doc_id)
            res = cb.call("eval_model_integrity", {
                "doc_id": d, "proximity_mm": proximity_mm,
                "include_nearest": include_nearest, "pair_budget_s": pair_budget_s,
            })
            status = ("Query refused" if res.get("ok") is not True else
                      "Complete geometric census" if res.get("complete") is True else
                      "Incomplete geometric census; unknown evidence remains")
            inspection_summary = (
                f"{status}: {len(res.get('solids', []))} solids, "
                f"{len(res.get('non_solids', []))} non-solid records, "
                f"{len(res.get('pairs', []))} near pairs, "
                f"{len(res.get('pair_failures', []))} pair failures, "
                f"{len(res.get('islands', []))} geometric islands, "
                f"{len(res.get('isolated_solids', []))} isolated solids. "
                "Positive gaps are proximity, not contact. Geometric islands do not prove "
                "physical attachment or load paths; assess retention and running clearances "
                "separately. No engineering criteria or placement verification performed."
            )
            return _dumps({
                **res,
                "inspection_summary": inspection_summary,
                "interpretation": {
                    "physical_attachment": "not_assessed",
                    "load_paths": "not_assessed",
                    "running_clearances": "not_assessed",
                    "placement_verification": "not_performed",
                },
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)



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
    def inspect_section(plane: Optional[Union[str, dict]] = None,
                        doc_id: Optional[str] = None,
                        offset: Optional[float] = None,
                        origin: Optional[list] = None,
                        normal: Optional[list] = None,
                        xdir: Optional[list] = None,
                        **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Cut the part with a plane and report the CROSS-SECTION area + section moments.

        This is the "section the part to inspect its internals" probe. It slices every
        world-placed body independently and sums the material cut faces (outer regions
        minus holes), with an area-weighted centroid and centroidal section moments.

        plane : "top" (XY, normal +Z), "front" (XZ, normal -Y), or "right" (YZ, normal +X) —
                the reserved Chudware datum planes (Z-up). Spellable as "XY"/"XZ"/"YZ", a
                normal axis, section=, datum=, or plane_name=. You may instead pass a custom
                {"origin":[x,y,z], "normal":[nx,ny,nz], "xdir":[...]} plane.
        offset: shift a named plane by this many mm along its signed normal. offset=0 is the
                world-origin datum. offset_mm=, distance= and a numeric at= are accepted.
        origin/normal/xdir: flattened spelling of the custom-plane object. origin and normal
                must be supplied together; xdir is optional.

        Returns {ok, area_mm2, centroid:[x,y,z], Ixx, Iyy, Ixy, summary}:
          * area_mm2 — the cross-section area (mm^2), the headline number. For a solid this is
            the filled region; for a thin/hollow wall it's the material the plane cuts through.
          * centroid — the area centroid of the section, in world coordinates.
          * Ixx/Iyy/Ixy — centroidal section second moments (mm^4) in the cutting-plane frame.
        A plane that misses the body (empty cut) returns area 0.
        Coplanar steps/floors belong to the exact-plane material section; tangent edges
        or points alone have zero area. A closed shell counts as the solid it bounds.
        Only a closed body has a section area. If the plane cuts a body with free edges,
        the tool refuses: code `section_failed`, `detail.reason` = "body_not_closed_solid",
        with `detail.body_id` and `detail.free_edges` {count, crossing_plane, sids}.
        To fix it, run check_geometry_health on that body, then repair the feature that
        opened it. Use heal_body only for an imported body.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []

            at = extra.pop("at", None)
            if at is not None:
                if isinstance(at, (int, float)) and not isinstance(at, bool):
                    if offset is not None and float(offset) != float(at):
                        raise ValueError(
                            f"inspect_section(): offset={offset!r} conflicts with at={at!r}.")
                    offset = float(at)
                    notes.append(f"at={at!r} read as offset={offset:g} mm")
                else:
                    if plane is not None and plane != at:
                        raise ValueError(
                            f"inspect_section(): plane={plane!r} conflicts with at={at!r}.")
                    plane = at
                    notes.append(f"at={at!r} read as plane={at!r}")

            plane = _one_alias("inspect_section", "plane", extra,
                               ("section", "section_plane", "datum", "datum_plane",
                                "plane_name", "axis"), plane, notes)
            offset = _one_alias("inspect_section", "offset", extra,
                                ("offset_mm", "distance", "distance_mm"), offset, notes)
            origin = _one_alias("inspect_section", "origin", extra,
                                ("point", "location"), origin, notes)
            normal = _one_alias("inspect_section", "normal", extra,
                                ("normal_dir", "direction"), normal, notes)
            xdir = _one_alias("inspect_section", "xdir", extra,
                              ("x_dir",), xdir, notes)
            _reject_unknown(
                "inspect_section", extra,
                "it takes plane=, offset=, origin=, normal=, xdir= and doc_id=.")

            if isinstance(plane, dict):
                plane_ref: Union[str, dict] = dict(plane)
                if origin is not None or normal is not None or xdir is not None:
                    raise ValueError(
                        "inspect_section(): pass the custom plane either as plane={origin,normal"
                        "[,xdir]} OR as flattened origin=/normal=/xdir= arguments, not both.")
            elif origin is not None or normal is not None or xdir is not None:
                if origin is None or normal is None:
                    raise ValueError(
                        "inspect_section(): a custom cut needs both origin=[x,y,z] and "
                        "normal=[nx,ny,nz]; xdir=[x,y,z] is optional.")
                plane_ref = {"origin": origin, "normal": normal}
                if xdir is not None:
                    plane_ref["xdir"] = xdir
                if plane is not None:
                    notes.append(
                        f"plane={plane!r} ignored because explicit origin=/normal= define the cut")
            else:
                named = _read_plane("inspect_section", plane, notes)
                plane_ref = named
                if offset is not None:
                    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                        raise ValueError(
                            f"inspect_section(offset={offset!r}): offset must be a number in mm.")
                    distance = float(offset)
                    if distance != 0.0:
                        normals = {
                            "top": [0.0, 0.0, 1.0],
                            "front": [0.0, -1.0, 0.0],
                            "right": [1.0, 0.0, 0.0],
                        }
                        n = normals[named]
                        plane_ref = {
                            "origin": [distance * n[0], distance * n[1], distance * n[2]],
                            "normal": n,
                        }
                        notes.append(
                            f"offset={distance:g} mm shifted the {named!r} datum along its normal")

                        offset = None  # already applied; do not re-shift in the custom-plane path
            if isinstance(plane_ref, dict) and offset not in (None, 0, 0.0):
                if "origin" not in plane_ref or "normal" not in plane_ref:
                    raise ValueError(
                        "inspect_section(): offset on a custom plane requires origin and normal.")
                o = plane_ref.get("origin")
                n = plane_ref.get("normal")
                if not (isinstance(o, (list, tuple)) and len(o) == 3
                        and isinstance(n, (list, tuple)) and len(n) == 3):
                    raise ValueError(
                        "inspect_section(): custom plane origin/normal must be numeric [x,y,z].")
                mag = sum(float(v) * float(v) for v in n) ** 0.5
                if mag <= 1e-12:
                    raise ValueError("inspect_section(): custom plane normal is near-zero.")
                distance = float(offset)
                plane_ref["origin"] = [
                    float(o[i]) + distance * float(n[i]) / mag for i in range(3)
                ]
                notes.append(f"offset={distance:g} mm shifted the custom plane along its normal")

            d = _doc(doc_id)
            res = cb.call("eval_section_props", {"doc_id": d, "plane": plane_ref})
            area = res.get("area_mm2", 0.0)
            label = plane_ref if isinstance(plane_ref, str) else "custom"
            if area and area > 0:
                summary = f"cross-section area at plane '{label}' = {area:.6g} mm^2"
            else:
                summary = (
                    f"plane '{label}' produced no closed cross-section (the cut may miss the "
                    f"body or only graze an edge) — area = 0. Check where the body sits with "
                    f"measure_part and move the cut with offset= or origin=/normal=.")
            return _dumps(_interpreted(
                {"ok": True, "doc_id": d, "plane": plane_ref,
                 "summary": summary, **res}, notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, 'inspect_section(plane="top", offset=0)'))



    @mcp.tool()
    def wall_thickness(doc_id: Optional[str] = None) -> str:
        """Sample local wall spans by inward normal rays; NOT a structural-wall safety verdict.

        The minimum includes real local taper at acute convex edges, where a normal-ray span
        tends to zero even when the adjacent wall is thick. A micron value is not automatically
        a self-hit, and a moderate dihedral angle does not prove the part safe. Inspect sections
        at AND near its location and the relevant load path before accepting or redesigning it.
        No edge samples or low percentiles are removed.

        Rays start at each face-local tessellation vertex, shifted tangentially 0.001 mm into
        its face and normally 0.001 mm into the material. Only outward EXIT hits more than 5°
        from the exit plane count; the reported span adds back the inward offset. Accepted
        spans must exceed 0.002 mm. These are mesh-based samples, not an exact B-rep minimum,
        a precision guarantee, or coverage of unsampled/sub-cutoff thin regions.

        Returns {ok, min_thickness_mm, mean_thickness_mm, max_thickness_mm, n_sampled,
        aicm_path, summary, thinnest, minimum_sample, measurement_basis, measurement_caveat}:
          * min/mean/max — sampled local normal-ray spans; null if no accepted span.
          * minimum_sample — unrounded tessellation `at`, vertex_index, body, actual ray_origin,
            inward_direction, exit_at, and tangent_nudge vector. The span is the distance
            from ray_origin to exit_at PLUS measurement_basis.surface_offset_mm.
          * measurement_basis — method, sampling locations, vertex/unmeasured counts, offsets,
            exit-angle filter, and minimum_accepted_span_exclusive_mm. Missing rays and
            sub-cutoff spans are unresolved, not proof of adequate thickness.
          * thinnest — up to 3 face minima, each {thickness_mm, at:[x,y,z], body, vertex_index,
            sample_location, face:<sid>, surf, built_by:{id,name}}. `at` is the mesh vertex,
            not the nudged ray origin. sample_location is face_mesh_boundary (which can also
            be a face seam) or face_mesh_interior; neither label alone classifies structural
            safety. built_by names the feature that last rebuilt the face. Locate it with
            inspect_section/query_geometry instead of suppressing features to find it.
          * aicm_path — field file for the in-app thickness colour-map viewer.
        Process minimum comparisons are screening findings, not manufacturing approval.
        """
        d = None
        try:
            d = _doc(doc_id)
            return _dumps(_wall_thickness_result(d))
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
                selfint = b.get("self_intersection") or {}
                if selfint.get("self_intersecting"):
                    findings.append(
                        f"the body SELF-INTERSECTS — {selfint.get('interference_count')} "
                        f"interference(s) "
                        f"({', '.join(selfint.get('kinds') or []) or 'unclassified'}) found by "
                        f"the kernel's self-intersection check. Its own skin passes through "
                        f"itself, so volume/mass, meshing and every boolean against this body "
                        f"are wrong even where the topology looks closed. Fix the feature "
                        f"whose profile or path folds back on itself: a sweep round too tight "
                        f"a corner, a thicken/offset larger than a local radius, a loft that "
                        f"twists, or panels knit together that cross each other.")
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
                        "repairer: there is no hole-filling / normal-flipping pass to run "
                        "HERE, and bolting one on would hide the real defect in your feature "
                        "tree. Fix the FEATURE that produced the bad body — the findings "
                        "above name the offending edge/face sids; hover(sid) or measure "
                        "them, then edit (add_features op=edit_feature) or delete the "
                        "feature responsible and re-run this check. The ONE exception is a "
                        "body that arrived broken from an IMPORT (no feature of yours "
                        "authored it): heal_body(doc_id) records the repair as a feature, "
                        "escalating from ShapeFix through re-sewing, orientation repair and "
                        "a face-based solid rebuild, and reports what it cleared.")}

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
          self_intersection            {checked, self_intersecting, interference_count, kinds,
                                       elapsed_ms} — a REAL kernel self-intersection run
                                       (BOPAlgo_CheckerSI, level 9) under a 20 s per-body
                                       budget. A body it flags is NOT sound: its skin passes
                                       through itself, so mass, meshing and booleans are all
                                       wrong however closed the topology looks. If the check
                                       could not run (budget exceeded, OCCT failure) it
                                       reports checked:false + reason and lands in
                                       not_checked — never a passing verdict. Body-vs-body
                                       overlap is still check_interference.
          findings                     one actionable sentence per defect, naming the sids
          verdict                      the one-line answer
        `not_checked` at the top level lists every check that did NOT run, so a passing
        `watertight` can never be mistaken for a clean bill of health.

        doc_id : the part (defaults to the active document).
        body   : 1-based body number; omit to check EVERY body (also spelled body_id=).
        repair / fill_holes / fix_normals / fix_winding : accepted so the trimesh-style call
          you were about to write does not die on an unknown argument — but they are REFUSED,
          not performed. This is a parametric B-rep kernel: the fix for a bad body is to fix
          the FEATURE that produced it, which the findings point you at. For a body that
          arrived broken from an IMPORT — no feature of yours authored it — call heal_body,
          which records the repair as a feature (ShapeFix, then the opt-in sewing /
          orientation / rebuild_solid rungs) and refuses one that would cost material.

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

    # ------------------------------------------------------------------ solid repair
    # check_geometry_health REFUSES repair, and that refusal is right for a defect the
    # feature tree caused: fix the feature. But an IMPORTED body has no feature to fix —
    # the defect arrived in the file. A live frame assembly's rear spine (imported STEP)
    # failed health with bad_orientation_of_subshape, and with no repair op the agent
    # burned a run on booleans, BREP round-trips and STL sewing. `heal_body` is that
    # missing op: it appends a RECORDED `heal` feature (contracts/feature_params.md#heal),
    # so the repair re-runs on every rebuild and its before/after verdict is auditable.

    def _health_digest(doc_id: str) -> dict:
        """The check_geometry_health verdict, trimmed to what a before/after diff needs."""
        raw = _health_report(doc_id, None, None, False, False, False, False,
                             "check_geometry_health")
        rep = json.loads(raw)
        if rep.get("ok") is not True:
            return {"ok": False, "error": rep.get("error") or rep}
        bodies = []
        for b in rep.get("bodies") or []:
            bodies.append({
                "body_id": b.get("body_id"),
                "sound": b.get("sound"),
                "watertight": b.get("watertight"),
                "brep_valid": b.get("brep_valid"),
                "free_edges": (b.get("free_edges") or {}).get("count"),
                "non_manifold_edges": (b.get("non_manifold_edges") or {}).get("count"),
                "orientation_consistent": (b.get("orientation") or {}).get("consistent"),
                "volume_mm3": b.get("volume_mm3"),
                "findings": b.get("findings") or [],
            })
        vols = [b["volume_mm3"] for b in bodies if isinstance(b["volume_mm3"], (int, float))]
        return {"ok": True, "summary": rep.get("summary"),
                "watertight": rep.get("watertight"), "sound": rep.get("sound"),
                "body_count": rep.get("body_count"), "bodies": bodies,
                "volume_mm3": round(sum(vols), 6) if vols else None}

    def _bbox_of(doc_id: str) -> Optional[dict]:
        try:
            bb = cb.call("doc_bbox", {"doc_id": doc_id}) or {}
        except Exception:  # noqa: BLE001 — a bbox is evidence, not a precondition
            return None
        return {k: bb.get(k) for k in ("min", "max", "size") if bb.get(k) is not None} or None

    def _vec_delta(a, b) -> Optional[list]:
        if not (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)):
            return None
        return [round(float(y) - float(x), 6) for x, y in zip(a, b)]

    @mcp.tool()
    def heal_body(target: Optional[Union[int, str]] = None,
                  doc_id: Optional[str] = None,
                  tolerance_mm: Optional[float] = None,
                  fix_small_edges: bool = False,
                  sew_tolerance_mm: Optional[float] = None,
                  fix_orientation: bool = False,
                  rebuild_solid: bool = False,
                  rebuild_fuzzy_mm: Optional[float] = None,
                  unify_faces: bool = False,
                  max_volume_change_pct: Optional[float] = None) -> str:
        """REPAIR a topologically broken solid, as a RECORDED feature with an escalation ladder.

        This is the answer to a `check_geometry_health` failure that your feature tree did NOT
        cause — an imported STEP/IGES/mesh body that arrived invalid (`bad_orientation_of_
        subshape`, `not_closed`, `free_edge`, a wrong-signed volume). For a defect your own
        features caused, fix the feature instead: healing over it hides the real bug and the
        next parameter edit brings it back.

        It appends a `heal` feature to the tree, so the repair is part of the rebuild rather
        than a one-off mutation — never "repair" a body outside the tree with a BREP/STL
        round-trip or a boolean against itself; nothing downstream can then tell what changed,
        and the next rebuild loses it.

        THE LADDER — climb it in order, one rung per call, and read the returned evidence
        before the next rung. Everything past the first rung is OPT-IN because each one is
        more destructive than the last:
          1. bare call            ShapeFix_Shape → ShapeFix_Solid (+ ShapeFix_Wireframe if
                                  fix_small_edges). Clears most imports outright.
          2. sew_tolerance_mm     re-derives edge sharing from GEOMETRY (BRepBuilderAPI_Sewing)
                                  — for a body whose STORED sharing is wrong.
          3. fix_orientation      flood-fills the face-orientation graph, then re-wraps the
                                  shells outward. Purely topological: no surface is rebuilt, so
                                  areas and analytic surface types survive exactly. The right
                                  escalation for `bad_orientation_of_subshape`, and the whole
                                  fix for a merely inside-out import.
          4. rebuild_solid        re-derives the solid from its faces (BOPAlgo_MakerVolume), so
                                  orientation comes from geometry, not stored flags. Needed when
                                  the returned `orientation.conflicts` > 0 — the graph is self-
                                  CONTRADICTORY and no flag repair can satisfy it. This is the
                                  rung that fixed the live frame spine. Pair it with
                                  unify_faces to merge the same-domain faces a rebuild splits,
                                  and with rebuild_fuzzy_mm=0.05 so the rebuild does not leave
                                  RAZOR STRIP faces: at OCCT's own tolerance every pair of
                                  near-coincident input faces gets its own intersection line,
                                  and the leftovers (0.011 mm wide on the live spine) make an
                                  otherwise BRep-valid body unmeshable at ANY element size.
                                  Read `rebuild.narrowest_face_mm` — under ~1e-4 of the part's
                                  diagonal, `warning` says so and names the fix.
        A rung that does not finish the job says so in `warning`, naming the next one.

        Returns, so you can judge the repair instead of trusting it:
          fixed             was the body invalid before and valid after?
          fixes             the BRepCheck defects that were CLEARED
          remaining_issues  defects still present — a partial repair says so
          before / after    full check_geometry_health digests (summary, per-body counts, volume)
          bad_edges_before/after  edges traversed the SAME way by both adjacent faces: the
                            defect itself, counted. 144 -> 0 is a repair; 144 -> 68 is not.
          material_volume_before/after + material_change_pct   the verdict numbers. A signed
                            volume integral over a shell whose faces disagree is not a volume (a
                            reversed face on a 24000 mm^3 box reads 16000), so both sides are
                            measured with their orientations flood-corrected first and the
                            budget is applied to THAT. This is what the refusal is judged on.
          volume_delta_mm3  raw reading change, for reference; on an orientation repair the
                            READING moves because the broken integral was wrong.
          orientation       {faces_flipped, conflicts} when fix_orientation ran
          rebuild           {lumps, kept_volume_mm3, discarded_volume_mm3, discarded_pct,
                            narrowest_face_mm} when rebuild_solid ran
          bbox_before/after + bbox_delta   the envelope must not move; if it did, the repair
                            rebuilt geometry and you should verify the part, not ship it.

        REFUSAL: the feature FAILS (and the batch rolls back, so the body is untouched) when the
        result is worse than the input — more defect KINDS than it found, the envelope moved, or
        material changed by more than max_volume_change_pct. The error carries every number the
        verdict was made on. A heal that costs material is not a heal.

        target          which body to heal: a feature id ("f3"), a feature NAME ("Spine"), or a
                        1-based body number (1 = first body). Omit for the current running body
                        (correct for a single-body part).
        doc_id          the part (defaults to the active document).
        tolerance_mm    ShapeFix precision. Default 1e-7 (OCCT's confusion tolerance) — raise to
                        0.01–0.1 when the import's gaps are real and the default leaves defects.
                        Above the part's real feature size it MERGES geometry instead of
                        repairing it (0.1 mm on the live spine invented three new defect kinds
                        and was refused), so raise it one decade at a time.
        fix_small_edges also drop tiny edges / close wire gaps (ShapeFix_Wireframe). Off by
                        default: it REMOVES topology, so a body whose small edges are intended
                        (a 0.05 mm fillet band) would lose them.
        sew_tolerance_mm  rung 2: re-sew the faces at this tolerance (mm). Omit to skip sewing.
        fix_orientation rung 3: flood-fill and repair the face-orientation graph.
        rebuild_solid   rung 4: re-derive the solid from its faces. Reports the lumps it found
                        and refuses if the discarded ones exceed the budget.
        rebuild_fuzzy_mm  the rebuild's intersection merge distance (mm), e.g. 0.05. Omit for
                        OCCT's own tolerance, which leaves razor strips on an import whose
                        faces are near-coincident. It also widens the envelope-movement budget
                        to 10x itself, since you authorized merging at that distance.
        unify_faces     merge same-domain faces afterwards (use with rebuild_solid).
        max_volume_change_pct  refusal budget on material change, default 0.5 (%).
        """
        d = None
        try:
            d = _doc(doc_id)
            def _positive_mm(name, value, hint):
                if value is None:
                    return None
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"heal_body({name}={value!r}): {name} is a length in mm "
                        f"(e.g. 0.01). {hint}") from None
                if not value > 0:
                    raise ValueError(f"heal_body({name}={value}): must be > 0.")
                return value

            tolerance_mm = _positive_mm("tolerance_mm", tolerance_mm,
                                        "Omit it for the 1e-7 default.")
            sew_tolerance_mm = _positive_mm(
                "sew_tolerance_mm", sew_tolerance_mm,
                "Omit it to skip the sewing pass entirely.")
            rebuild_fuzzy_mm = _positive_mm(
                "rebuild_fuzzy_mm", rebuild_fuzzy_mm,
                "Omit it for OCCT's own intersection tolerance.")
            if max_volume_change_pct is not None:
                try:
                    max_volume_change_pct = float(max_volume_change_pct)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"heal_body(max_volume_change_pct={max_volume_change_pct!r}): a PERCENT "
                        f"of the body's volume (0.5 = half a percent). Omit for the 0.5 "
                        f"default.") from None
                if max_volume_change_pct < 0:
                    raise ValueError(
                        f"heal_body(max_volume_change_pct={max_volume_change_pct}): must be "
                        f">= 0 (0 refuses ANY material change).")

            # --- resolve `target` to the feature id the `heal` op takes ----------------
            body_fid = None
            if target is not None and not (isinstance(target, str) and not target.strip()):
                feats, owner_map = _target_feature_roster(d)
                as_body_no = None
                if isinstance(target, bool):
                    raise ValueError("heal_body(target=True/False) is not a body — pass a "
                                     "feature id, a feature name, or a 1-based body number.")
                if isinstance(target, int):
                    as_body_no = target
                elif isinstance(target, str) and re.fullmatch(r"[bB]?\d+", target.strip()):
                    as_body_no = int(target.strip().lstrip("bB"))
                if as_body_no is not None:
                    if as_body_no < 1:
                        raise ValueError(
                            f"heal_body(target={target!r}): body numbers are 1-BASED "
                            f"(target=1 is the first body).")
                    # The body's owner is the LAST feature owner_map credits with it — an
                    # earlier feature on the same body was consumed by a later one, and
                    # healing that stale id would resurrect it as a duplicate body.
                    order = {str(f.get("id")): i for i, f in enumerate(feats)}
                    owners = [fid for fid, om in (owner_map or {}).items()
                              if as_body_no in [int(x) for x in (om.get("bodies") or [])]]
                    owners.sort(key=lambda fid: order.get(fid, -1))
                    if not owners:
                        raise ValueError(
                            f"heal_body(target={target!r}): no feature owns body "
                            f"{as_body_no} in this document. Features: {_roster_text(feats)}. "
                            f"check_geometry_health(doc_id) lists the real body numbers.")
                    body_fid = owners[-1]
                else:
                    body_fid, _fname, _feats = _resolve_feature_ref(d, target, feats,
                                                                    "heal_body target")

            before = _health_digest(d)
            if before.get("ok") is not True:
                return _dumps({"ok": False, "doc_id": d,
                               "error": "could not read health BEFORE healing; nothing was "
                                        "changed",
                               "detail": before.get("error")})
            bbox_before = _bbox_of(d)

            params: dict = {}
            if body_fid:
                params["body"] = body_fid
            if tolerance_mm is not None:
                params["tolerance"] = tolerance_mm
            if fix_small_edges:
                params["fix_small_edges"] = True
            if sew_tolerance_mm is not None:
                params["sew_tolerance_mm"] = sew_tolerance_mm
            if fix_orientation:
                params["fix_orientation"] = True
            if rebuild_solid:
                params["rebuild_solid"] = True
            if rebuild_fuzzy_mm is not None:
                params["rebuild_fuzzy_mm"] = rebuild_fuzzy_mm
            if unify_faces:
                params["unify_faces"] = True
            if max_volume_change_pct is not None:
                params["max_volume_change_pct"] = max_volume_change_pct
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": [
                {"op": "add_feature",
                 "feature": {"type": "heal", "name": "Heal", "params": params}}]})
            rows = (r.get("rebuild") or {}).get("feature_status") or []
            row = rows[-1] if rows else {}
            if not row.get("ok"):
                # A refusal is a RESULT, not a mystery: hand back the evidence the engine
                # judged on so the caller can pick the next rung instead of guessing.
                out = {
                    "ok": False, "doc_id": d, "target": target,
                    "error": row.get("error") or "the heal feature failed to build",
                    "note": "a rejected batch is rolled back — the document is unchanged",
                    "before": before}
                if isinstance(row.get("data"), dict):
                    out["heal_evidence"] = row["data"]
                return _dumps(out)

            feature_id = (r.get("id_map") or {}).get("$0") or row.get("id")
            data = row.get("data") or {}
            before_issues = list((data.get("before") or {}).get("issues") or [])
            after_issues = list((data.get("after") or {}).get("issues") or [])
            fixes = [i for i in before_issues if i not in after_issues]

            after = _health_digest(d)
            bbox_after = _bbox_of(d)
            _publish_current(d)

            vb, va = before.get("volume_mm3"), after.get("volume_mm3")
            out = {
                "ok": True, "doc_id": d, "feature_id": feature_id,
                "target": target, "body_feature": body_fid,
                "tolerance_mm": data.get("tolerance", tolerance_mm),
                "fix_small_edges": bool(data.get("fix_small_edges", fix_small_edges)),
                "fixed": bool(data.get("fixed")),
                "fixes": fixes,
                "remaining_issues": after_issues,
                "brep_valid_before": (data.get("before") or {}).get("valid"),
                "brep_valid_after": (data.get("after") or {}).get("valid"),
                "body_volume_before_mm3": data.get("volume_before"),
                "body_volume_after_mm3": data.get("volume_after"),
                "bad_edges_before": (data.get("before") or {}).get("bad_edges"),
                "bad_edges_after": (data.get("after") or {}).get("bad_edges"),
                "material_volume_before_mm3": data.get("material_volume_before"),
                "material_volume_after_mm3": data.get("material_volume_after"),
                "material_change_pct": data.get("material_change_pct"),
                "sew_tolerance_mm": data.get("sew_tolerance_mm"),
                "fix_orientation": bool(data.get("fix_orientation", fix_orientation)),
                "rebuild_solid": bool(data.get("rebuild_solid", rebuild_solid)),
                "rebuild_fuzzy_mm": data.get("rebuild_fuzzy_mm"),
                "rebuild": data.get("rebuild"),
                "unify_faces": bool(data.get("unify_faces", unify_faces)),
                "max_volume_change_pct": data.get("max_volume_change_pct"),
                "before": before, "after": after,
                "volume_delta_mm3": (round(va - vb, 6)
                                     if isinstance(vb, (int, float))
                                     and isinstance(va, (int, float)) else None),
                "bbox_before": bbox_before, "bbox_after": bbox_after,
            }
            if row.get("warning"):
                out["warning"] = row["warning"]
            if bbox_before and bbox_after:
                out["bbox_delta"] = {
                    k: _vec_delta(bbox_before.get(k), bbox_after.get(k))
                    for k in ("min", "max", "size")}

            heads = []
            if out["fixed"]:
                heads.append("HEALED: " + (", ".join(fixes) if fixes else "BRep is now valid"))
                # A "HEALED" body that carries a razor strip is unmeshable, so the engine's
                # note must reach the summary and not just the `warning` field.
                if "razor face" in (row.get("warning") or ""):
                    heads.append(row["warning"].split("heal:", 1)[-1].strip())
            elif before_issues and after_issues:
                # The engine's own warning names the NEXT rung (it knows whether the
                # orientation graph was self-contradictory); a generic "raise the tolerance"
                # sent the live run round the ladder in the wrong order.
                nxt = (row.get("warning") or "").split("Escalate:")[-1].strip()
                if not nxt:
                    nxt = ("try fix_orientation:true, then rebuild_solid:true" if not
                           rebuild_solid else "fix the import that produced the body")
                heads.append(
                    f"PARTIAL: cleared {', '.join(fixes) if fixes else 'nothing'}; still "
                    f"{', '.join(after_issues)} "
                    f"({out['bad_edges_before']} -> {out['bad_edges_after']} bad edges). "
                    f"Next: {nxt}")
            elif not before_issues:
                heads.append("NO DEFECT: the body was already BRep-valid — nothing to fix "
                             "(the heal feature is recorded as a no-op)")
            else:
                heads.append("NOT HEALED: " + ", ".join(after_issues))
            heads.append(f"health: {after.get('summary')}")
            if out["volume_delta_mm3"]:
                heads.append(
                    f"volume reading {vb} -> {va} mm^3 (an orientation repair changes the "
                    f"READING, because the broken integral was wrong)")
            out["summary"] = ". ".join(h for h in heads if h)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "heal_body()"))
