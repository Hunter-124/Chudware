"""CAD query/measure/check MCP tools (split out of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""
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
    COMPONENT_KEY_RE,
    component_key_refusal,
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
    _captioned_render,
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
    _tile_pngs_grid,
    _tmp,
    already_resolved_selector,
    is_geom_query,
    normalize_geom_query,
    note_interference_check,
    render_output_path,
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


def _eval_thickness_all_bodies(doc_id: str, min_wall_mm: float,
                               resolution: float | None = None) -> tuple[dict, list]:
    """Merge the native study across all solids, retaining each field's body ordinal."""
    params = {"doc_id": doc_id, "min_wall_mm": min_wall_mm}
    if resolution is not None:
        params["resolution"] = resolution
    count = _doc_body_count(doc_id)
    if count <= 1:
        res = cb.call("eval_thickness", params)
        res["thin_regions"] = [dict(r, body=1) for r in res.get("thin_regions") or []]
        handle = res.get("aicm_path")
        return res, ([(1, str(handle))] if handle else [])
    per = [(b, cb.call("eval_thickness", {**params, "body": b}))
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
        basis["spacing_mm"] = max(float((r.get("measurement_basis") or {}).get("spacing_mm") or 0)
                                  for _, r in per)
        basis["budget_limited"] = any(
            (r.get("measurement_basis") or {}).get("budget_limited") for _, r in per)
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
        "min_wall_mm": min_wall_mm,
        "thin_area_mm2": sum(float(r.get("thin_area_mm2") or 0) for _, r in per),
        "n_thin_regions": sum(int(r.get("n_thin_regions") or 0) for _, r in per),
        "thin_regions": sorted(
            (dict(region, body=b) for b, r in per for region in r.get("thin_regions") or []),
            key=lambda r: r["min_thickness_mm"]),
    }
    return merged, [(b, str(r["aicm_path"])) for b, r in measured if r.get("aicm_path")]


def _wall_minimum(doc_id: str, min_wall_mm: float | None) -> float:
    """Use the persisted set_process profile, otherwise the native FDM profile."""
    if min_wall_mm is None:
        from .intake_tools import _K_PROCESS, _dfm, _get
        profile = _get(doc_id, _K_PROCESS)
        if not isinstance(profile, dict) or (profile.get("params") or {}).get("min_wall_mm") is None:
            profile = _dfm("dfm_get_profile", {
                "process": (profile.get("process") if isinstance(profile, dict) else None) or "fdm"})
        min_wall_mm = profile["params"]["min_wall_mm"]
    if isinstance(min_wall_mm, bool) or not math.isfinite(min_wall_mm) or min_wall_mm <= 0:
        raise ValueError("min_wall_mm must be a positive, finite thickness in mm.")
    return float(min_wall_mm)


def _wall_face_metadata(doc_id: str) -> dict:
    """Join body-local TopExp face IDs to SIDs and the feature that last rebuilt them."""
    feats, owner_map = _target_feature_roster(doc_id)
    names = {str(f.get("id")): f.get("name") for f in feats}
    faces = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}) or {}
             ).get("matches") or []
    metadata = {(int(m["body"]), int(m["topo"])): {
        "face": m.get("sid"), "surf": m.get("surf")} for m in faces
        if isinstance(m, dict) and m.get("body") is not None and m.get("topo") is not None}
    for fid, owner in owner_map.items():
        for pair in owner.get("faces") or []:
            if isinstance(pair, list) and len(pair) == 2:
                metadata.setdefault((int(pair[0]), int(pair[1])), {})["built_by"] = {
                    "id": str(fid), "name": names.get(str(fid))}
    return metadata


def _wall_thickness_result(doc_id: str, min_wall_mm: float | None = None,
                           resolution: float | None = None) -> dict:
    """Build the canonical whole-part study, including actionable thin-region ownership."""
    threshold = _wall_minimum(doc_id, min_wall_mm)
    if resolution is not None and (
            isinstance(resolution, bool) or not math.isfinite(resolution) or resolution <= 0):
        raise ValueError("resolution must be a positive, finite sample spacing in mm.")
    res, fields = _eval_thickness_all_bodies(doc_id, threshold, resolution)
    mn = res.get("min_thickness_mm")
    basis = res.get("measurement_basis") or {}
    spacing = basis.get("spacing_mm")
    summary = (f"minimum inscribed-sphere wall thickness = {mn:.4g} mm"
               if mn is not None else "wall thickness unmeasured: no opposing wall found")
    summary += f"; process comparison minimum = {threshold:g} mm"
    out = {"ok": True, "doc_id": doc_id, "summary": summary, **res}
    out["min_wall_mm"] = threshold
    out["basis_note"] = ("Inscribed-sphere wall thickness"
                         + (f"; surface spacing {spacing:.4g} mm" if spacing else "")
                         + "; mesh-based screening, not manufacturing approval.")
    out["colour_legend"] = (
        f"red <= {threshold:g} mm; yellow/green/cyan increasing; "
        f"blue >= {3 * threshold:g} mm; grey = unmeasured")
    metadata = _wall_face_metadata(doc_id)
    for index, region in enumerate(out.get("thin_regions") or [], start=1):
        region["index"] = index
        face_info = [metadata.get((region["body"], int(fid)), {})
                     for fid in region.get("face_ids") or []]
        region["faces"] = list(dict.fromkeys(m["face"] for m in face_info if m.get("face")))
        owners = {m["built_by"]["id"]: m["built_by"] for m in face_info if m.get("built_by")}
        region["built_by"] = list(owners.values())
    if mn is not None and fields:
        out["thinnest"] = _thinnest_wall_faces(doc_id, fields, metadata=metadata)
    minimum_sample = out.get("minimum_sample")
    if isinstance(minimum_sample, dict):
        minimum_sample = dict(minimum_sample, body=minimum_sample.get("body", 1))
        minimum_sample.update(metadata.get(
            (minimum_sample["body"], minimum_sample.get("face_id")), {}))
        out["minimum_sample"] = minimum_sample
    return out


_WALL_REGIONS_CAP = 20
_WALL_REGION_FACES_CAP = 12


def _cap_wall_result(result: dict) -> dict:
    """The JSON form of a wall study, bounded: the thinnest `_WALL_REGIONS_CAP` regions, each
    naming at most `_WALL_REGION_FACES_CAP` faces. The cut is announced, never silent."""
    out = dict(result)
    regions = [r for r in (out.get("thin_regions") or []) if isinstance(r, dict)]
    if len(regions) > _WALL_REGIONS_CAP:
        regions.sort(key=lambda r: (r.get("min_thickness_mm") is None,
                                    r.get("min_thickness_mm") or 0.0))
        out["thin_regions_truncated"] = {
            "returned": _WALL_REGIONS_CAP, "total": len(regions),
            "note": "thinnest regions first; fix those and re-run for the rest"}
        regions = regions[:_WALL_REGIONS_CAP]
    capped = []
    for region in regions:
        region = dict(region)
        for key in ("faces", "face_ids", "built_by"):
            listed = region.get(key) or []
            if len(listed) > _WALL_REGION_FACES_CAP:
                region[key] = listed[:_WALL_REGION_FACES_CAP]
                region[f"{key}_total"] = len(listed)
        capped.append(region)
    if "thin_regions" in out:
        out["thin_regions"] = capped
    return out


def _mass_warnings(mass: dict) -> list:
    """Top-level warnings for an eval_mass result.

    A density that was DEFAULTED (steel, 7.85 g/cm^3) instead of read from an applied material
    used to be visible only as `mass.material.source == "default"` — one level down, easy to
    miss while quoting a mass. This lifts it, and the engine's own reliability warnings (an
    open shell makes volume/inertia meaningless), to the response top level."""
    out: list = []

    def _material(material, who: str = "") -> None:
        if not isinstance(material, dict):
            return
        if material.get("unresolved_key"):
            out.append(f"{who}material key {material.get('key')!r} has no density in the material "
                       f"registry, so 7.85 g/cm^3 (steel) was ASSUMED — mass and inertia scale "
                       f"with it; fix the material key or pass density_g_cm3=")
        elif material.get("source") == "default":
            out.append(f"{who}no material is applied, so 7.85 g/cm^3 (steel) was ASSUMED — mass "
                       f"and inertia scale with it; set_material(...) or pass density_g_cm3= / "
                       f"material=")

    bodies = [b for b in (mass.get("bodies") or []) if isinstance(b, dict)]
    _material(mass.get("material"))
    if (mass.get("material") or {}).get("mixed"):
        for b in bodies:
            _material(b.get("material"), f"body {b.get('body_id')}: ")
    if mass.get("warning"):
        out.append(str(mass["warning"]))
    for b in bodies:
        if b.get("warning"):
            out.append(f"body {b.get('body_id')}: {b['warning']}")
    return list(dict.fromkeys(out))


_THINNEST_FACES = 3


def _thinnest_wall_faces(doc_id: str, fields: list, *, metadata: dict | None = None) -> list:
    """Locate the lowest inscribed-sphere face minima on the refined field mesh.

    Each field labels its body as 1; `fields` retains the document ordinal. FaceRec IDs
    are body-local TopExp indices, the same keys used by doc_query and owner_map.
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

    if metadata is None:
        metadata = _wall_face_metadata(doc_id)
    spots = []
    for thickness, body_id, face_id, vertex_index, point, location in ranked:
        spot = {"thickness_mm": round(thickness, 4), "at": point, "body": body_id,
                "vertex_index": vertex_index, "sample_location": location}
        spot.update(metadata.get((body_id, face_id), {}))
        spots.append(spot)
    return spots


def _wall_region_section(region: dict, view_dir) -> tuple:
    """(section, camera direction) showing a thin region's wall in cross-section.

    The plane passes through the region centroid, CONTAINS the measured wall direction (so
    the cut shows the wall's own thickness) and crosses the region's longest extent (so a
    long thin rib is cut across, not along). The camera looks from the removed side."""
    from . import cad_viewpoint as vp
    axis = vp._unit(region["wall_direction"])
    if axis is None:
        raise ValueError("Thin region has no wall-thickness direction to section.")

    def across(ref):
        return vp._unit([ref[k] - vp._dot(ref, axis) * axis[k] for k in range(3)])

    lo, hi = region["bbox"]["min"], region["bbox"]["max"]
    normal = None
    for _, k in sorted(((hi[k] - lo[k], k) for k in range(3)), reverse=True):
        if abs(axis[k]) < 0.9:  # an extent that is not the wall direction itself
            normal = across([1.0 if j == k else 0.0 for j in range(3)])
            break
    view = vp._unit(view_dir, (1, 1, 0.8))
    if normal is None:
        normal = across(view) or across(min(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                                            key=lambda r: abs(vp._dot(r, axis))))
    if vp._dot(normal, view) < 0:
        normal = [-c for c in normal]
    # Mostly face-on to the cut, tilted along the wall and along the cut (toward the side the
    # overview camera came from) so the section reads in 3D context.
    along = vp._cross(normal, axis)
    if vp._dot(along, view) < 0:
        along = [-c for c in along]
    direction = vp._unit([normal[k] + 0.35 * axis[k] + 0.35 * along[k] for k in range(3)])
    return {"point": list(region["centroid"]), "normal": list(normal)}, direction


def _wall_thickness_image(doc_id: str, result: dict) -> list:
    """Planner-chosen whole-part heat maps plus ghosted cuts through the worst regions."""
    from . import shot_planner
    threshold = result["min_wall_mm"]
    heatmap = {"min_wall_mm": threshold}
    requested = (result.get("measurement_basis") or {}).get("requested_spacing_mm")
    if requested is not None:
        heatmap["resolution"] = requested
    plan = shot_planner.plan_shots(doc_id, k=2)
    tiles, labels = [], []
    for index, view in enumerate(plan["views"], start=1):
        label = f"Whole part {index}"
        png, _, _ = _render_view_png(
            doc_id, view["dir"], _tmp(".png"), name=label.upper(), camera=view["camera"],
            section=view.get("section"), thickness=heatmap, annotate=True)
        tiles.append(png)
        labels.append(label)
    for region in (result.get("thin_regions") or [])[:3]:
        section, direction = _wall_region_section(region, plan["views"][0]["dir"])
        # Frame the region (and its opposing side) as a thin slab around the cut: along the
        # section normal only `pad` of depth, so the region's length does not shrink the
        # wall's cross-section in the picture.
        bbox = region["bbox"]
        pad = max(threshold, 2 * region["min_thickness_mm"])
        normal, point = section["normal"], section["point"]
        focus = {"min": [], "max": []}
        for k in range(3):
            lo = min(bbox["min"][k], region["opposite_at"][k]) - pad
            hi = max(bbox["max"][k], region["opposite_at"][k]) + pad
            w = abs(normal[k])
            focus["min"].append(w * (point[k] - pad) + (1 - w) * lo)
            focus["max"].append(w * (point[k] + pad) + (1 - w) * hi)
        label = f"Region {region['index']}: {region['min_thickness_mm']:.3g} mm"
        png, _, _ = _render_view_png(
            doc_id, direction, _tmp(".png"), name=label.upper(), focus=focus,
            section=section, thickness=heatmap, W=512, H=384, annotate=True)
        tiles.append(png)
        labels.append(label)
    png = _tile_pngs_grid(
        tiles, render_output_path(f"{doc_id}-wall-thickness", ".png"), labels=labels, cols=2)
    caption = {key: result.get(key) for key in (
        "min_thickness_mm", "min_wall_mm", "thin_area_mm2", "n_thin_regions")}
    caption["regions"] = [
        {key: r.get(key) for key in ("index", "body", "min_thickness_mm", "kind", "faces",
                                   "built_by", "at")}
        for r in result.get("thin_regions") or []]
    caption["panels"] = labels
    caption["colour_legend"] = result["colour_legend"]
    caption["basis_note"] = result["basis_note"]
    return _captioned_render(png, doc_id, labels, caption=_dumps(caption))


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


_DATUM_NORMALS = {"top": [0.0, 0.0, 1.0], "front": [0.0, -1.0, 0.0], "right": [1.0, 0.0, 0.0]}


def _cut_plane_ref(tool: str, plane, offset, origin, normal, xdir, notes: list):
    """The engine plane ref for a cutting/mirror plane in any of the spellings inspect_section
    and check_symmetry accept: a datum name (str) for an unshifted reserved plane, otherwise
    {origin, normal[, xdir]}. Sources, one at a time:
      * plane={origin,normal[,xdir]}                      a custom plane object
      * origin= + normal= [+ xdir=]                       the same, flattened
      * plane="top"/"XY"/"+z"... [+ offset= mm]           a reserved datum, optionally shifted
                                                          along its normal
    offset= may also shift a custom plane along its normal. Every rewrite lands in `notes`;
    an under-specified or contradictory plane is a teaching error, never a guess."""
    if offset is not None and (isinstance(offset, bool) or
                               not isinstance(offset, (int, float)) or
                               not math.isfinite(offset)):
        raise ValueError(f"{tool}(): offset must be a finite number in mm.")
    if isinstance(plane, dict):
        plane_ref: Union[str, dict] = dict(plane)
        if origin is not None or normal is not None or xdir is not None:
            raise ValueError(
                f"{tool}(): pass the custom plane either as plane={{origin,normal"
                "[,xdir]} OR as flattened origin=/normal=/xdir= arguments, not both.")
    elif origin is not None or normal is not None or xdir is not None:
        if origin is None or normal is None:
            raise ValueError(
                f"{tool}(): a custom plane needs both origin=[x,y,z] and "
                "normal=[nx,ny,nz]; xdir=[x,y,z] is optional.")
        plane_ref = {"origin": origin, "normal": normal}
        if xdir is not None:
            plane_ref["xdir"] = xdir
        if plane is not None:
            raise ValueError(f"{tool}(): specify either plane= or origin=/normal=, not both.")
    else:
        named = _read_plane(tool, plane, notes)
        plane_ref = named
        if offset is not None:
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                raise ValueError(f"{tool}(offset={offset!r}): offset must be a number in mm.")
            distance = float(offset)
            if distance != 0.0:
                n = _DATUM_NORMALS[named]
                plane_ref = {"origin": [distance * n[0], distance * n[1], distance * n[2]],
                             "normal": list(n)}
                notes.append(f"offset={distance:g} mm shifted the {named!r} datum along its normal")
                offset = None  # applied; do not re-shift in the custom-plane path below
    if isinstance(plane_ref, dict) and offset not in (None, 0, 0.0):
        if "origin" not in plane_ref or "normal" not in plane_ref:
            raise ValueError(f"{tool}(): offset on a custom plane requires origin and normal.")
        o = plane_ref.get("origin")
        n = plane_ref.get("normal")
        if not (isinstance(o, (list, tuple)) and len(o) == 3
                and isinstance(n, (list, tuple)) and len(n) == 3):
            raise ValueError(f"{tool}(): custom plane origin/normal must be numeric [x,y,z].")
        mag = sum(float(v) * float(v) for v in n) ** 0.5
        if mag <= 1e-12:
            raise ValueError(f"{tool}(): custom plane normal is near-zero.")
        distance = float(offset)
        plane_ref["origin"] = [float(o[i]) + distance * float(n[i]) / mag for i in range(3)]
        notes.append(f"offset={distance:g} mm shifted the custom plane along its normal")
    return plane_ref


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
    """CAD query/measure/check MCP tools (split out of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""


    @mcp.tool()
    def query_geometry(query: Optional[Union[str, dict]] = None,
                       doc_id: Optional[str] = None,
                       owner: Optional[str] = None, feature: Optional[str] = None,
                       offset: int = 0, page_size: int = 100,
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
        owner= or feature= names a feature by ID or name; only its raw doc_tree owner_map
        faces/edges are considered, BEFORE side, pick, near, count and index. This is
        engine attribution, which may include geometry regenerated by a boolean, not
        necessarily only geometry created by the feature. offset/page_size page results
        (default 100, maximum 100). count remains a geometric selection cap.
        Shared SIDs are reported in ambiguous_sids; never assume a SID uniquely identifies
        geometry when it is listed there.

        Returns each matching face/edge with its shape (surf), side, signed outward normal,
        centroid, size and revision-scoped sid (round faces add radius, concavity and axis_point — a point
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
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("offset must be a nonnegative integer.")
            if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
                raise ValueError("page_size must be an integer from 1 to 100.")
            if isinstance(query, dict):
                query = dict(query)
                for key in ("owner", "feature"):
                    if key in query:
                        value = query.pop(key)
                        current = owner if key == "owner" else feature
                        if current is not None and current != value:
                            raise ValueError(f"{key} supplied twice with different values.")
                        if key == "owner":
                            owner = value
                        else:
                            feature = value
            if owner is not None and feature is not None and owner != feature:
                raise ValueError("owner and feature name the same predicate; specify only one.")
            owner = owner if owner is not None else feature
            # A sid IS an element, not a query. This tool hands sids out on every match and
            # the docs tell the model to address the element by one, so asking about a sid
            # here has exactly one meaning. Live claude-opus-5 2026-08-04:
            # query_geometry(doc_id=…, sid="F1b1b80") answered "needs a geometry query".
            sid = extra.pop("sid", None)
            kind = extra.pop("of", None) if sid is not None else None
            if isinstance(query, dict) and "sid" in query:
                if sid is not None:
                    raise ValueError("query_geometry: specify sid only once.")
                unsupported = set(query) - {"sid", "of"}
                if unsupported:
                    raise ValueError(f"query_geometry: sid cannot be combined with predicates {sorted(unsupported)}.")
                sid, kind, query = query["sid"], query.get("of"), None
            if sid is None and isinstance(query, str) and _SID_RE.fullmatch(query.strip()):
                sid, query = query.strip(), None
            if sid is not None:
                if not isinstance(sid, str) or not sid.strip():
                    raise ValueError("query_geometry: sid must be a nonempty string.")
                if query is not None or extra or owner is not None:
                    raise ValueError("query_geometry: sid cannot be combined with other query predicates or arguments.")
                if not _SID_RE.fullmatch(sid.strip()) or sid.strip()[0].lower() not in ("f", "e"):
                    raise ValueError("query_geometry: sid must be a face or edge SID.")
                d = _doc(doc_id)
                actual_kind = {"f": "face", "e": "edge"}[sid.strip()[0].lower()]
                if kind is not None and kind != actual_kind:
                    raise ValueError(f"query_geometry: of={kind!r} contradicts sid {sid!r}, which names a {actual_kind}.")
                res = cb.call("doc_query", {"doc_id": d, "query": {"of": actual_kind}}) or {}
                matches = [m for m in res.get("matches", [])
                           if str(m.get("sid", "")).casefold() == sid.strip().casefold()]
                if not matches:
                    raise ValueError(f"query_geometry: no {actual_kind} with sid {sid!r}; requery current geometry.")
                if len(matches) != 1:
                    raise ValueError(f"query_geometry: sid {sid!r} is ambiguous ({len(matches)} matches); use geometry predicates and body/topo to distinguish them.")
                return _dumps({"ok": True, "doc_id": d, "query": {"sid": sid.strip()},
                               "count": 1, "total_count": 1, "offset": offset,
                               "page_size": page_size, "next_offset": None,
                               "matches": matches[offset:offset + page_size]})
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

            engine_query = dict(q)
            if owner is not None:
                tree = cb.call("doc_tree", {"doc_id": d}) or {}
                feats = (tree.get("tree") or {}).get("features") or []
                fid, fname, _ = _resolve_feature_ref(d, owner, feats, what="owner")
                owned = (tree.get("owner_map") or {}).get(fid) or {}
                kind = str(q.get("of", "face")).lower()
                if kind not in ("face", "edge"):
                    raise ValueError("owner filtering supports face or edge queries only.")
                engine_query["within"] = owned.get("faces" if kind == "face" else "edges") or []

            try:
                res = cb.call("doc_query", {"doc_id": d, "query": engine_query})
            except CoreError as ce:
                raise _teach_no_bodies(
                    ce, d, f"query_geometry(query={json.dumps(q, sort_keys=True)})") from None
            # Every other tool reports `ok`; doc_query returns a bare match list, so a model
            # checking the usual success flag saw None and couldn't tell a hit from a miss.
            # Echo the RESOLVED query too: when a phrase was normalised into keys, the model
            # needs to see what it actually asked for before trusting the matches.
            out = {"ok": True, "doc_id": d, "query": q}
            out.update(res if isinstance(res, dict) else {"result": res})
            matches = out.get("matches") or []
            out["total_count"] = len(matches)
            out["offset"] = offset
            out["page_size"] = page_size
            out["matches"] = matches[offset:offset + page_size]
            out["next_offset"] = offset + page_size if offset + page_size < len(matches) else None
            if "ambiguous_sids" in out:
                page_sids = {m.get("sid") for m in out["matches"]}
                out["ambiguous_sids"] = {sid: n for sid, n in out["ambiguous_sids"].items()
                                         if sid in page_sids}
            if owner is not None:
                out["owner"] = {"id": fid, "name": fname, "source": "doc_tree.owner_map"}
            # Geometry selectors are revision-sensitive. Return a deliberately bounded tree
            # fingerprint with every successful query so the caller can tell whether a later
            # mutation invalidated these sids without receiving every feature's parameters.
            out["doc_id"] = d
            out["model_state"] = _query_model_state(d)
            if not matches:
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
          converged    whether the solver converged; fully_defined requires it, plus dof 0, no
                       conflicts, no skipped constraints and NOTHING fix-pinned (pinned_entities
                       empty — a single dimension beside blanket fix pins is still pinned)
          authored_dimensions / auto_dimensions  driving dimensions you authored vs engine-minted
                       auto ones (sketch_fully_define); findings all_dimensions_auto and
                       dimension_heavy (with relation_dimension_ratio) say when to confirm/replace
                       auto values with the critical dimensions or trade dimensions for relations
          hints        unordered suggestions from sketch_health (NOT an ordered plan)
          closure_usage / open_endpoints  open endpoints are a defect only when closure_usage is
                       closed_profile or mixed; for open_path / unknown they are informational

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
            snapshot = cb.call("doc_tree", {"doc_id": d})
            r = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            if r.get("ok") is False:
                return _dumps(r)
            status = r.get("status", "")
            dof = r.get("dof")
            from services.mcp.ascii2d import live_sketch_design_intent
            intent = live_sketch_design_intent(d, sketch, r, snapshot=snapshot)
            revision_before = snapshot.get("revision")
            revision_after = cb.call("doc_tree", {"doc_id": d}).get("revision")

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
            skipped = r.get("skipped_constraints") or []
            converged = r.get("converged")
            pinned = intent["pinned_entities"]
            # Solver truth AND-ed with everything that makes dof 0 meaningless: a failed solve,
            # skipped (unapplied) constraints, conflicts, or ANY fix-pinned authored entity —
            # not only the pinned_only shape (one dimension beside blanket pins is still pinned).
            fully_defined = (dof == 0 and status == "full" and not conflicting
                             and converged is True and not skipped and not pinned)
            if conflicting:
                summary = (f"Over-defined: {len(conflicting)} conflicting constraint(s) "
                           f"{conflicting} — remove one to resolve the contradiction "
                           "(sketch_health lists each one's conflicting partners and values).")
            elif converged is not True:
                if skipped:
                    reason = (f"{len(skipped)} constraint(s) were skipped: " + "; ".join(
                        f"{row.get('id', '?')} ({row.get('reason', 'no reason reported')})"
                        if isinstance(row, dict) else str(row) for row in skipped[:4]))
                elif converged is False:
                    reason = ("the solver reported converged=false without naming a conflict "
                              "or a skipped constraint — read sketch_health(text=True)")
                else:
                    reason = "sketch_health reported no convergence flag"
                summary = (f"Not converged: dof {dof} is not a definition state — {reason}. "
                           "Fix that first; fully_defined cannot be claimed.")
            elif intent["pinned_only"]:
                summary = ("Pinned instead of constrained (pinned_only): remove profile fix "
                           "relations, add design-intent relations/driving dimensions, then "
                           "sketch_fully_define. DOF 0 from blanket pins is not fully defined.")
            elif pinned:
                summary = (f"Not fully defined: authored entities {pinned} are fix-pinned, so "
                           "dof 0 does not mean they are constrained parametrically — replace "
                           "each fix with relations (horizontal/vertical, coincident, tangent, "
                           "equal) and the critical dimensions.")
            elif skipped:
                summary = "Constraints were skipped — correct their refs/type before declaring fully defined."
            elif fully_defined:
                summary = "Fully defined (dof 0, converged) — constrained by relations and dimensions, nothing fix-pinned."
            elif dof == 0:
                summary = f"Not fully defined: solver reports status {status!r} despite dof 0; inspect sketch_health."
            else:
                summary = (f"Under-defined: {dof} degree(s) of freedom remain — add geometric "
                           f"relations and/or driving dimensions until dof reaches 0.")
                if narration:
                    summary += f" Free: {narration}"
                if redundant:
                    summary += f" (Also {len(redundant)} redundant: {redundant}.)"
            closure = r.get("closure") or {}
            out = {"ok": True, "doc_id": d, "sketch": sketch, "status": status,
                   "dof": dof, "converged": converged, "fully_defined": fully_defined,
                   "conflicting": conflicting, "redundant": redundant, "summary": summary,
                   # narrated enrichment straight from sketch_health (the advisor source).
                   "open_endpoints": closure.get("open_endpoints"),
                   "closure_usage": closure.get("usage"),
                   "dof_narration": narration, "hints": r.get("hints"),
                   "advisory": r.get("advisory")}
            out.update(intent)
            out["skipped_constraints"] = skipped
            if type(revision_before) is int and type(revision_after) is int and revision_before == revision_after:
                out["revision"] = revision_before
            else:
                out["revision_stale"] = True
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
                     body_id: Optional[int] = None,
                     **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        Mass uses the material applied to the model: each body's set_material override, else
        the part material (assembly components use their source parts' materials). Only a body
        with no material — or a material key the registry has no density for — falls back to
        7.85 g/cm^3 steel, and then the top-level `warnings` says so (never silent).
        `mass.material` names the density and its `source` (body_material | document_material |
        component_material | default | explicit; `per_body` with `mixed:true` when bodies
        differ). density_g_cm3 overrides every body for this readout. It is also accepted as
        density= / density_g_cc= / material_density= / rho=, or as a NAME: material="aluminum"
        resolves its registry density, including density-only and orthotropic records. The
        density is g/cm^3 and a value above 25 (osmium, the densest element, is 22.6) is
        REFUSED as a unit error — steel is 7.85, not 7850. The geometry sanity check covers
        only the last compiled body and is skipped for a selected physical solid; use
        check_geometry_health to check every body. Check failures appear in `warnings`.

        Results are nested under `mass`: use `result["mass"]["volume_mm3"]` and
        `result["mass"]["mass_g"]`. Volume is also available as top-level `volume_mm3`.
        Units and frame are stated in `mass.units` / `mass.inertia_frame`: volume mm^3, mass g,
        com mm, inertia g*mm^2 as the full 3x3 tensor (row-major) about the CENTRE OF MASS with
        axes parallel to the world axes; principal_moments (g*mm^2, ascending) and
        principal_axes are its eigen-decomposition.
        For multi-body docs these are the COMBINED total of ALL bodies (about the combined
        centre of mass), and `mass.bodies[]` lists each body's own properties (with
        `mass.body_count`). body_id=N (1-based, an entry of mass.bodies[]) measures just that
        one physical solid.

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
                if not math.isfinite(density) or not 0 < density <= 25:
                    raise ValueError(
                        f"measure_part: density {density!r} must be in g/cm^3, between 0 and 25; "
                        "steel is 7.85 g/cm^3 (7850 kg/m^3). Divide kg/m^3 by 1000.")
            body_id = _one_alias("measure_part", "body_id", extra, ("body", "body_number"),
                                 body_id, notes)
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
                            "it takes doc_id=, body_id= and density_g_cm3= only.")
            if body_id is not None and (isinstance(body_id, bool) or not isinstance(body_id, int)
                                        or body_id < 1):
                raise ValueError(
                    f"measure_part(body_id={body_id!r}): body_id is a 1-based physical solid "
                    "number — the entries of mass.bodies[] from a call without it.")
            d = _doc(doc_id)
            if route_wall_thickness:
                if body_id is not None or density is not None:
                    raise ValueError("wall thickness is whole-part; body_id and density do not apply.")
                return _dumps(_interpreted(_wall_thickness_result(d), notes))
            params = {"doc_id": d}
            if density is not None:
                params["density_g_cm3"] = density
            if body_id is not None:
                params["body"] = body_id
            mass = cb.call("eval_mass", params)
            out = {"ok": True, "doc_id": d, "mass": mass}
            if "volume_mm3" in mass:
                out["volume_mm3"] = mass["volume_mm3"]
            warnings = _mass_warnings(mass)
            # eval_check inspects the last compiled body, not all physical solids; its body
            # numbering differs from eval_mass's exploded solids. State its scope explicitly.
            if body_id is None:
                try:
                    out["check"] = cb.call("eval_check", {"doc_id": d})
                    out["check_scope"] = "last compiled body only"
                    if mass.get("body_count", 1) > 1:
                        warnings.append("geometry sanity check covers only the last compiled body; "
                                        "use check_geometry_health to check every body")
                except CoreError as ce:
                    out["check"] = {"ran": False, "error": str(ce)}
                    warnings.append(f"the geometry sanity check did NOT run ({ce}); the "
                                    f"numbers above are unchecked")
            else:
                warnings.append("geometry sanity check not run for an individual physical solid; "
                                "use check_geometry_health")
            if warnings:
                out["warnings"] = warnings
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
          * a face   -> its area (mm^2) and perimeter; a CYLINDRICAL or SPHERICAL face also
            reports radius and diameter (mm), plus axis/axis_point (cylinder) or center (sphere)
          * an edge  -> its length (mm), or radius and diameter (mm) for a circular edge
          * a vertex -> its point coordinates
        Two queries -> the minimum DISTANCE (mm) between them (with the two closest points) and,
        when applicable (face↔face normals or edge↔edge tangents), the ANGLE (degrees, 0..90).
        When BOTH are cylindrical faces or circular edges (two holes, two pins) it also returns
        axis_distance — the distance between the two AXES, i.e. the hole pitch / centre spacing —
        with axes_parallel and axis_angle. `distance` stays the surface-to-surface gap, which
        differs from the pitch by the radii.

        Each query must resolve to EXACTLY ONE face/edge. Several matches are an error that
        lists their sids, even when the query carries pick/count/index: a tied pick (the four
        equal vertical edges, pick:"longest") is NOT broken by taking the first. Narrow it with
        near:[x,y,z] (nearest wins), a pick with no ties, or pass one sid — query_geometry(query)
        shows the candidates. Returns a readable `summary` line.
        A sid is a REVISION-scoped fingerprint (24-bit hash of kind, shape, centroid, radius):
        geometry edits retire it, and a sid shared by several elements is refused as
        ambiguous. A dangling-selector error names live alternatives; when a selector also
        supplies its old centroid, alternatives are ranked by that location. Never treat those
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
                if "axis_distance" in res:
                    kind_note = ("parallel axes" if res.get("axes_parallel")
                                 else f"skew axes, {res.get('axis_angle', 0.0):.3g} deg apart")
                    parts.append(f"axis-to-axis distance {res['axis_distance']:.4g} mm "
                                 f"({kind_note}; for parallel holes this is the pitch, while "
                                 f"`distance` is the wall-to-wall gap)")
                out["summary"] = ("between the two selections: " + ", ".join(parts)) if parts \
                    else "no distance/angle computed"
            else:
                if kind == "area":
                    out["summary"] = f"area = {res.get('area'):.6g} mm^2"
                    if res.get("radius") is not None:
                        out["summary"] += (f"; {res.get('surface', 'round')} face radius "
                                           f"{res['radius']:.6g} mm (diameter "
                                           f"{res['diameter']:.6g} mm)")
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
        members: optional exact member names or assembly component IDs; components is an alias.
        Checks only pairs WITHIN this selection. Unknown names are refused.
        Omit or pass [] to check all members. A scoped result does not
        verify placement of the rest of the document; scope and pair counts are explicit.

        include_coincident: also classify near-zero-clearance face-touching pairs (distance
        <= 1e-6 mm) as `contacts` entries instead of clearances — the "coincident faces" case
        SolidWorks flags separately (two parts touching exactly, not overlapping or gapped).
        min_clearance_mm: compare clearances against this minimum. Measured pairs report
        clearance_status="below"|"within_tolerance"|"above" and clearance_tolerance_mm.
        A gap within the band is not a definite pass or violation. Also accepted as
        min_clearance= / clearance= / clearance_mm= / min_gap_mm=, and
        include_coincident= as coincident= / include_contacts= (echoed in `interpreted`).

        Returns {ok, clear, complete, clearance_mode, summary, pairs_checked, pairs_total,
        parts:[{name,bbox,volume}], interferences:[{a,b,volume}], clearances, contacts}.
        Pairs resolved with a fuzzy Boolean fallback have method="fallback_fuzzy_common",
        "fallback_fuzzy_cut", or "fallback_fuzzy_common_and_cut", approximate=true and
        fuzzy_tolerance_mm; their overlap classification and volume are approximate.
        fallback_extrema_disjoint=true marks constituent separation established by
        positive solid distance when Booleans failed; method="fallback_extrema_disjoint"
        is used when no fuzzy Boolean was needed. Both fallbacks can occur in one pair.
        clipped_common=true (method="clipped_common" when alone) marks an exact, not
        approximate, Common recovered after the direct one failed by clipping one body
        to the other's tight bounds. For an incomplete pair, summary names the first
        failed pair and pair_failures[].detail lists the OCCT alert, operand (first=a,
        second=b), world location and sub-shape tolerance: run query_geometry near that
        point to find the face/feature; sub-shapes closer together than the other body's
        coarse fillet tolerance (e.g. a 0.01 mm step) make OCCT merge them.
        Additional fallback attempts share a 10 s cooperative OCCT budget per pair;
        exhaustion leaves that pair incomplete rather than reporting a clear result.
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
            members = _one_alias("check_interference", "members", extra,
                                 ("components",), members, notes)
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
            if members:
                bodies = cb.call("asm_world_bodies", {"doc_id": d, "explode": False}).get("bodies", [])
                by_id = {b.get("component"): b.get("name") for b in bodies
                         if b.get("component") and b.get("name")}
                members = [by_id.get(member, member) for member in members]
                names = set(by_id.values())
                for member in members:
                    if (isinstance(member, str) and member not in names
                            and COMPONENT_KEY_RE.fullmatch(member)):
                        refusal = component_key_refusal(d, member, "check_interference")
                        if refusal:
                            raise ValueError(refusal)
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
    def check_model_history(doc_id: Optional[str] = None) -> str:
        """Inspect a part's editable feature history without changing or rebuilding it.

        Returns native history findings, suggested edits, conservative safe-fix candidates,
        the analyzed revision and limitations. A complete report covers the native history
        rules only; it does not certify geometry, engineering safety or manufacturability.
        Use simplify_part() to preview the exact eligible operations and opt in to applying
        them. Non-automatic suggestions require human judgment.
        """
        try:
            d = _doc(doc_id)
            return _dumps(cb.call("eval_model_history", {"doc_id": d}))
        except Exception as e:  # noqa: BLE001
            return _err(e)


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
        """Inspect intersections between this part's pre-boolean feature tools.

        Compares the raw tool solids before they are fused or cut into the body. Their
        intersection is NOT a measurement of material added or removed: for example, a
        through-all cutting tool can extend far beyond the part. Inspect the model and
        feature history before interpreting an intersection. Use check_interference for
        separate bodies or assembly components.

        features (optional): restrict the scan to pairs involving these feature ids (e.g. the
        ids you just added) instead of the whole tree — add_features already runs this
        automatically on each new batch and surfaces it as `overlap_warnings` in its return, so
        you rarely need to call this yourself right after building; call it explicitly to
        re-check the whole part or a specific subset. A single id on its own (features="f2") is
        accepted and wrapped into a list, as are feature= / feature_ids= / ids= (echoed in
        `interpreted`).

        Returns {ok, summary, basis, note, pairs:[{a, b, a_name, b_name, kind,
        tool_kind, volume_mm3, volume_basis}], dead_features}. The basis is
        "pre_boolean_tool_intersection"; tool_kind is "add_tool_overlap",
        "cut_tool_overlap", or "add_cut_tool_overlap". Legacy kind values are
        "boss_overlap", "cut_overlap", "cut_into_boss", and "boss_swallowed".
        They describe tool-space intersections, not proof that a boss was swallowed
        or material was removed twice. Legacy ok=false for "boss_swallowed" flags
        a tool-space finding, not a confirmed loss of material.
        dead_features identifies feature ids with no surviving owned faces; investigate
        their history rather than assuming why those faces disappeared.
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
    def check_symmetry(plane: Optional[Union[str, dict]] = None, doc_id: Optional[str] = None,
                       offset: Optional[float] = None, origin: Optional[list] = None,
                       normal: Optional[list] = None, body: Optional[int] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Is the part mirror-symmetric about a plane? Returns the deviation.

        plane : "top" (XY, mirror across Z), "front" (XZ, mirror across Y), or "right" (YZ,
                mirror across X) — the reserved Chudware datum planes (Z-up) through the world
                origin. Spellable as its axis pair ("XY"/"xz"/"YZ"), a normal axis ("z"/"+x"),
                or as about= / datum= / mirror_plane= / about_plane=.
        offset: shift a named plane by this many mm along its normal — a part symmetric about
                x = 30 is not symmetric about the origin plane.
        origin/normal: ANY plane, as origin=[x,y,z] with normal=[nx,ny,nz] (both required), or
                plane={"origin":[…],"normal":[…]}. A part's own mid-plane is origin = bbox
                centre (describe_part gives the bbox).
        body  : 1-based body number (also body_id=); omit for the LAST body. Pass it to check
                an earlier body of a multi-body part.
        Every rewrite is echoed in `interpreted`; an unreadable plane name lists the three
        legal planes.

        Mirrors the chosen body across the plane and measures the symmetric-difference volume.
        Returns {symmetric: bool, deviation_mm3, volume_mm3, tolerance_mm3, body_id, plane:
        {origin, normal}}: symmetric is True when the leftover volume is within 1e-3 of the body
        volume. If the mirror or its Boolean cannot complete, or the body has no volume, the
        call is an ERROR (code symmetry_failed) — never an "asymmetric" verdict. Handy to
        confirm a feature you meant to place symmetrically really is, or to pick a mirror plane.
        """
        d = None
        try:
            extra = _extra_kwargs(dict(kwargs))
            notes: list = []
            plane = _one_alias("check_symmetry", "plane", extra,
                               ("about", "about_plane", "datum", "datum_plane", "mirror_plane",
                                "plane_name", "axis"), plane, notes)
            offset = _one_alias("check_symmetry", "offset", extra,
                                ("offset_mm", "distance", "distance_mm"), offset, notes)
            origin = _one_alias("check_symmetry", "origin", extra,
                                ("point", "location"), origin, notes)
            normal = _one_alias("check_symmetry", "normal", extra,
                                ("normal_dir", "direction"), normal, notes)
            body = _one_alias("check_symmetry", "body", extra, ("body_id",), body, notes)
            _reject_unknown("check_symmetry", extra,
                            "it takes plane=, offset=, origin=, normal=, body= and doc_id=.")
            if body is not None and (isinstance(body, bool) or not isinstance(body, int)
                                     or body < 1):
                raise ValueError(f"check_symmetry(body={body!r}): body is a 1-based body number.")
            plane_ref = _cut_plane_ref("check_symmetry", plane, offset, origin, normal, None,
                                       notes)
            d = _doc(doc_id)
            params = {"doc_id": d, "plane": plane_ref}
            if body is not None:
                params["body"] = body
            res = cb.call("eval_symmetry", params)
            if (not isinstance(res, dict) or res.get("error") or
                    not isinstance(res.get("symmetric"), bool) or
                    not isinstance(res.get("deviation_mm3"), (int, float)) or
                    not math.isfinite(res["deviation_mm3"]) or res["deviation_mm3"] < 0):
                raise ValueError(f"symmetry evaluation failed or returned no valid verdict: {res!r}")
            dev = float(res["deviation_mm3"])
            vol = float(res.get("volume_mm3") or 0.0)
            where = plane_ref if isinstance(plane_ref, str) else "the given plane"
            pct = f", {100.0 * dev / vol:.3g}% of the body volume" if vol > 0 else ""
            summary = (f"body {res.get('body_id')} is symmetric about '{where}' "
                       f"(deviation {dev:.4g} mm^3{pct})" if res.get("symmetric")
                       else f"body {res.get('body_id')} is NOT symmetric about '{where}' "
                            f"(deviation {dev:.4g} mm^3{pct}; tolerance "
                            f"{res.get('tolerance_mm3', 0.0):.4g} mm^3)")
            return _dumps(_interpreted({"ok": True, "doc_id": d, "summary": summary, **res},
                                       notes))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, 'check_symmetry(plane="top")'))



    @mcp.tool()
    def inspect_section(plane: Optional[Union[str, dict]] = None,
                        doc_id: Optional[str] = None,
                        offset: Optional[float] = None,
                        origin: Optional[list] = None,
                        normal: Optional[list] = None,
                        xdir: Optional[list] = None,
                        target_center: Optional[list] = None,
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
        target_center: REFUSED — it used to be accepted and ignored. Use origin=/normal= (cut
                through a point) or offset= (shift a named plane) to place the cut.

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
            if target_center is not None:
                raise ValueError(
                    "inspect_section(): target_center does nothing here — this tool sections "
                    "the WHOLE plane, so it cannot be 'aimed' at a point. To cut THROUGH a "
                    "point pass origin=[x,y,z] with normal=[nx,ny,nz] (or a named plane with "
                    "offset= in mm along its normal); for a picture of the cut use "
                    "render_part(section=...).")

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

            plane_ref = _cut_plane_ref("inspect_section", plane, offset, origin, normal, xdir,
                                       notes)

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
    def wall_thickness(doc_id: Optional[str] = None, min_wall_mm: Optional[float] = None,
                       resolution: Optional[float] = None, image: bool = True) -> Any:
        """Study wall thickness on EVERY solid, with an actionable red-to-blue heat map.

        min_wall_mm: comparison minimum T; defaults to the bound set_process profile, else
        the FDM process profile. resolution: optional surface spacing in mm (reported in
        measurement_basis; the engine may coarsen it to its sample budget).

        By default returns [Image, caption]: complementary whole-part views and ghosted
        cross-sections through up to three worst thin regions. Red <= T, blue >= 3T,
        grey = unmeasured. Region numbers match the caption; faces and built_by identify
        exactly where to edit. Fix or explicitly justify every red region before print export.

        image=False returns JSON: min/mean/max_thickness_mm, min_wall_mm, thin_area_mm2,
        thin_regions ({index, body, min_thickness_mm, kind, faces, built_by, at, opposite_at,
        bbox}), thinnest face minima, minimum_sample, measurement_basis and field aicm_path.
        The JSON lists the 20 THINNEST regions only (each with at most 12 face names);
        `thin_regions_truncated` gives the true total when it cut some, and n_thin_regions
        always counts them all. Inscribed-sphere measurements use a refined surface mesh;
        null means unmeasured (no opposing wall found), not proof of adequate thickness. Run
        dfm_check too.
        """
        d = None
        try:
            d = _doc(doc_id)
            result = _wall_thickness_result(d, min_wall_mm, resolution)
            return _wall_thickness_image(d, result) if image else _dumps(_cap_wall_result(result))
        except Exception as e:  # noqa: BLE001
            return _err(_teach_no_bodies(e, d, "wall_thickness()"))



    @mcp.tool()
    def draft_analysis(pull_dir: Optional[list] = None, min_draft_deg: float = 3.0,
                       doc_id: Optional[str] = None,
                       **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Draft-angle check along ONE pull direction: which faces have less than min_draft_deg of draft?

        For a given mold-pull (ejection) direction it measures each face's draft angle — how far
        its surface tilts away from the pull axis — from the tessellated surface normals, as an
        ABSOLUTE value, on the LAST body only. Faces whose worst-case |draft| is below
        min_draft_deg would drag on the tool during ejection and need a draft feature added.
        It does NOT detect undercuts, choose a parting line or find side actions, so a clean
        result means "draft >= min_draft_deg everywhere", never "moldable".

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
        summary}. n_faces_below_threshold == 0 means every face has at least min_draft_deg of
        draft along that pull (it is NOT a moldability verdict — see above). The
        faces_below_threshold list (centroid + area) tells you WHERE to add draft. aicm_path is
        the colour-map field for the in-app viewer.
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
                summary = (f"every face has >= {thr:g}deg draft along pull {list(pull)} "
                           f"(|draft| per mesh vertex, last body only). That is ALL this "
                           f"checks: undercuts, parting line and side actions are not "
                           f"analysed, so it is not a moldability verdict")
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
        """
        return _health_report(doc_id, body, body_id, repair, fill_holes, fix_normals,
                              fix_winding, "check_geometry_health")

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
