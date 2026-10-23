"""GSL (Geometry-Semantics Layer) MCP tools — a compact, NAMED, progressive-pull view of a part's
geometry that replaces the flat 68-face dump.

  describe_part(doc_id)         -> the part OUTLINE: bbox, feature groups WITH a placement
                                   narrative (which face each feature sits on, offsets from the
                                   part bbox, what it touches), detected symmetry/instances,
                                   addressable handles (bores/caps/round faces) with stable ids.
  query_adjacency(doc_id)       -> the face-adjacency graph (W3): which faces touch, convex/concave.
  hover(sid, doc_id)            -> expand ONE face/edge by the stable id describe_part gave.
  describe_sketch(sketch, ...)  -> a sketch's plane, entity roster, constraints, regions and DOF
                                   verdict as text — the non-vision equivalent of looking at it.
  describe_scene(views, ...)    -> deterministic "what the render would show" per view, from
                                   B-rep face normals — the non-vision fallback for render_part.

These let the model pull a small, named view and address geometry by a STABLE handle instead of
re-reading the whole B-rep after every edit.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Annotated, Any, Optional, Union

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from services.mcp import core_bridge as cb
from services.mcp.cad_common import (_NON_BODY_FEATURE_TYPES, _face_attribution,
                                   _solid_feature_order, coerce_json_or_repr,
                                   placement_source_of, placement_warnings, require_list)
from services.mcp.cad_asm_tools import resolve_component_source
from services.mcp.cad_tools import _doc, _dumps, _err
from services.mcp.fit_narration import _format_fit  # single home; loop.py shares it


def _axis_order(faces: list) -> Optional[dict]:
    """If a set of faces sits at equal radius around a principal axis at uniform angular spacing,
    report the rotational order. This is the structure the impeller exposes (N identical blades)
    that the flat read-back forces the model to reverse-engineer."""
    import math
    n = len(faces)
    if n < 3:
        return None
    for ax, (a, b) in (("z", (0, 1)), ("x", (1, 2)), ("y", (0, 2))):
        angs = []
        ok = True
        for f in faces:
            c = f.get("centroid") or [0, 0, 0]
            u, v = c[a], c[b]
            r = math.hypot(u, v)
            if r < 1e-6:
                ok = False
                break
            angs.append(math.degrees(math.atan2(v, u)) % 360.0)
        if not ok:
            continue
        angs.sort()
        gaps = [(angs[(i + 1) % n] - angs[i]) % 360.0 for i in range(n)]
        exp = 360.0 / n
        if all(abs(g - exp) < max(3.0, exp * 0.15) for g in gaps):
            return {"type": "rotational", "order": n, "axis": ax}
    return None


# ---------------------------------------------------------------- A3: placement narrative

_SIDE_AXIS = {"top": "z", "bottom": "z", "right": "x", "left": "x", "front": "y", "back": "y"}
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_SIDE_OF = {("x", 1): "right", ("x", -1): "left", ("y", 1): "back", ("y", -1): "front",
            ("z", 1): "top", ("z", -1): "bottom"}
_OPPOSITE_SIDE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left",
                  "front": "back", "back": "front"}
_AXIS_SIDE_WORD = {"+x": "right", "-x": "left", "+y": "back", "-y": "front",
                   "+z": "top", "-z": "bottom"}
# A feature has to sit at least this fraction of the part's extent off centre before we call
# it a side. Below it the feature is central (a through hole down the axis) and gets NO side,
# which is honest, instead of a coin-flip one that its own faces then contradict.
_SIDE_ECCENTRICITY = 0.05


def _bbox_bounds(bbox: dict):
    """(min, max) as 3-lists, or None when doc_bbox gave us nothing usable."""
    bmin, bmax = bbox.get("min"), bbox.get("max")
    if (isinstance(bmin, list) and isinstance(bmax, list) and
            len(bmin) == 3 and len(bmax) == 3):
        return bmin, bmax
    return None


def _centroid_side(centroid: list, bmin: list, bmax: list) -> Optional[str]:
    """The principal side a point sits on, by its LARGEST relative offset from the part
    centre. This is the single derivation every reported side comes from, so a +X feature
    can never read "left"."""
    best, best_rel = None, _SIDE_ECCENTRICITY
    for a, i in _AXIS_INDEX.items():
        size = bmax[i] - bmin[i]
        if size <= 1e-9:
            continue
        rel = (centroid[i] - 0.5 * (bmin[i] + bmax[i])) / size
        if abs(rel) > best_rel:
            best, best_rel = _SIDE_OF[(a, 1 if rel > 0 else -1)], abs(rel)
    return best


def _authored_side(ft: Optional[dict]) -> Optional[str]:
    """The side the AUTHOR placed the feature on, from its refs ({"face":{"side":"top"}} or
    "plane.top"). Used only for a feature whose centroid is dead-centre — a through hole
    carries no positional cue, but the hole was still drilled into a named face."""
    if not isinstance(ft, dict):
        return None
    refs = ft.get("refs") or {}
    for key in ("face", "plane", "start", "from", "sketch"):
        ref = refs.get(key)
        s = None
        if isinstance(ref, dict):
            s = ref.get("side")
        elif isinstance(ref, str):
            s = ref
        if not isinstance(s, str):
            continue
        s = s.strip().lower()
        if s.startswith("plane."):
            s = s[6:]
        if s in _SIDE_AXIS:
            return s
        if s in _AXIS_SIDE_WORD:
            return _AXIS_SIDE_WORD[s]
    return None


def _pick_anchor(fid: str, neighbors, rank: dict, areas: dict) -> Optional[str]:
    """The feature this one SITS ON: the largest feature it touches that came BEFORE it and
    is at least as big as it. A parent hosts its children, never the reverse — a 165 mm body
    is not "on" the 25 mm spout welded to it, and a plate is not "on" its own boss. When
    nothing earlier and larger touches the feature we return None and describe it absolutely
    rather than invent a relationship."""
    mine = rank.get(fid)
    if mine is None:
        return None
    my_area = areas.get(fid, 0.0)
    best = None
    for other in neighbors:
        r = rank.get(other)
        if r is None or r >= mine:
            continue
        a = areas.get(other, 0.0)
        if a < my_area:
            continue
        if best is None or (a, -r) > (best[0], -best[1]):
            best = (a, r, other)
    return best[2] if best else None


def _face_side_vote(owned: list) -> Optional[str]:
    """The side the feature's own PLANAR faces point to, weighted by area — the same face
    records planar_caps reports, so a side taken from here can never disagree with it.
    A feature whose two opposite sides are the same size (a plain box) is symmetric about
    that axis and gets NO side rather than a coin flip."""
    by_side: dict = defaultdict(float)
    for f in owned:
        if f.get("surf") == "plane" and f.get("side"):
            by_side[f["side"]] += f.get("area_or_len") or 0.0
    if not by_side:
        return None
    top_side, top_area = min(by_side.items(), key=lambda kv: (-kv[1], kv[0]))
    if by_side.get(_OPPOSITE_SIDE[top_side], 0.0) >= top_area * 0.99:
        return None
    return top_side


def _side_contradicts(side: str, centroid: list, bounds) -> bool:
    """True when the centroid puts the feature meaningfully on the OPPOSITE side."""
    if not side or not bounds:
        return False
    bmin, bmax = bounds
    i = _AXIS_INDEX[_SIDE_AXIS[side]]
    size = bmax[i] - bmin[i]
    if size <= 1e-9:
        return False
    rel = (centroid[i] - 0.5 * (bmin[i] + bmax[i])) / size
    return abs(rel) > _SIDE_ECCENTRICITY and _SIDE_OF[
        (_SIDE_AXIS[side], 1 if rel > 0 else -1)] != side


def _feature_placement(owned: list, bbox: dict, ft: Optional[dict] = None,
                       anchor: Optional[str] = None) -> Optional[dict]:
    """Where a feature's OWN faces sit: mean centroid, the principal side it is on
    (top/bottom/right/left/front/back), the feature it sits on, and offsets from the part
    bbox on the two axes PERPENDICULAR to that side (a boss on the top face is placed by its
    X/Y offset from the bbox corner).

    `side` has ONE derivation, and it is cross-checked against the centroid printed beside
    it, so the two halves of this record can never contradict each other: the area-dominant
    side among the feature's own planar faces wins (those are exactly the planar_caps
    records), unless the centroid puts the feature on the opposite side; then the centroid's
    own side; then the face the author placed the feature on (a centred through-hole has no
    positional cue but was still drilled into a named face); then no side at all, which is
    honest, rather than a coin flip its own faces then contradict."""
    if not owned:
        return None
    n = len(owned)
    centroid = [sum((f.get("centroid") or [0, 0, 0])[i] for f in owned) / n for i in range(3)]
    bounds = _bbox_bounds(bbox)

    side = _face_side_vote(owned)
    if side is not None and _side_contradicts(side, centroid, bounds):
        side = None
    if side is None and bounds:
        side = _centroid_side(centroid, *bounds)
    if side is None:
        authored = _authored_side(ft)
        if authored and not _side_contradicts(authored, centroid, bounds):
            side = authored
    # Final cross-check: never report a side no planar face of ours faces while one faces
    # the opposite way — that is the exact contradiction planar_caps used to expose.
    planar_sides = {f.get("side") for f in owned
                    if f.get("surf") == "plane" and f.get("side")}
    if side and planar_sides and side not in planar_sides and \
            _OPPOSITE_SIDE[side] in planar_sides:
        side = None

    axis = _SIDE_AXIS.get(side, "z")
    perp = [a for a in ("x", "y", "z") if a != axis]
    offsets = None
    if bounds:
        bmin, bmax = bounds
        offsets = {}
        for a in perp:
            i = _AXIS_INDEX[a]
            offsets[a] = {"from_min_mm": round(centroid[i] - bmin[i], 2),
                          "from_max_mm": round(bmax[i] - centroid[i], 2)}
    return {"centroid": [round(c, 2) for c in centroid], "side": side,
            "offsets": offsets, "n_faces": n, "anchor": anchor}


def _feature_group_line(fg: dict) -> str:
    """One-line placement sentence for a single feature_groups entry (token discipline: one
    line per feature, no matter how many faces it owns)."""
    if fg.get("consumed"):
        return f'feature {fg["id"]} owns no surviving faces (consumed by later ops)'
    label = f'{fg["id"]} {fg.get("name") or ""} [{fg.get("type")}]'.replace("  ", " ").strip()
    if fg.get("unattributed"):
        return (f'{label}: owns no face of its own — the {fg.get("attributed_faces", 0)} '
                'face(s) the kernel credited to it were re-created from earlier features')
    pl = fg.get("placement")
    if not pl:
        return f'{label}: {fg["faces"]} face(s)'
    side = pl.get("side")
    host = pl.get("anchor_label") or pl.get("anchor")
    if side and host:
        where = f'on the {side} face of {host}'
    elif side:
        where = f'on the {side} side of the part'
    elif host:
        where = f'centered in {host}'
    else:
        where = 'spans the part (no host feature)'
    off = pl.get("offsets")
    if off:
        off_txt = " / ".join(
            f'{off[a]["from_min_mm"]:.0f} mm from -{a.upper()} edge' for a in sorted(off))
        return f'{label} ({fg["faces"]} face(s)): {where}, centered {off_txt}'
    return f'{label} ({fg["faces"]} face(s)): {where}'


# ------------------------------------------------- feature -> face attribution, de-smeared
# doc_tree's owner_map credits every face a boolean REGENERATED to the feature that ran the
# boolean, in both directions: a small cut ends up owning the large planar face it merely
# pierced, and a small boss ends up owning the large face it merely landed on (measured: an r8
# through-bore in a 120x80x15 plate owns the plate's whole 9398 mm^2 bottom face; a 25 mm spout
# fused onto a 165 mm bucket owns the bucket's 309048 mm^2 outer wall). Un-smearing that is
# cad_common's job — _face_attribution there is the single implementation, shared with the
# inspect/render targeting join and the callout roster. This module only NARRATES its output:
# `owned` is the de-smeared partition, `claimed` the raw kernel count that stays visible as
# `attributed_faces`, and `reattributed` the number of faces that changed hands.


def _kept_import_bodies(names: list, keep, exclude) -> list:
    """The SURVIVING bodies of an import feature as [(source_ordinal, name), ...].

    Mirrors the compiler's sub-selection (core/doc/document.cpp import branch,
    contracts/feature_params.md): entries are 1-based SOURCE ordinals or body-name strings
    (a repeated name selects every match), `keep_bodies` restricts first and
    `exclude_bodies` drops from whatever survived. Narrating the raw source roster instead
    told the model about bodies the document does not have. An unresolvable entry is a
    compile error, so a live doc can only carry resolvable ones — they are skipped here
    rather than guessed at.
    """
    live = [True] * len(names)

    def _apply(entries: list, on: bool) -> None:
        for e in entries:
            if isinstance(e, bool):
                continue
            if isinstance(e, int):
                if 1 <= e <= len(names):
                    live[e - 1] = on
            elif isinstance(e, str):
                for i, nm in enumerate(names):
                    if nm == e:
                        live[i] = on

    if isinstance(keep, list) and keep:
        live = [False] * len(names)
        _apply(keep, True)
    if isinstance(exclude, list):
        _apply(exclude, False)
    return [(i + 1, names[i]) for i in range(len(names)) if live[i]]


def _digest(doc_id: str) -> dict:
    bodies = cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False}).get("bodies", [])
    if not bodies:
        raise ValueError("document has no bodies")
    faces = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}).get("matches", [])
    edges = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "edge"}}).get("matches", [])
    try:
        tree = cb.call("doc_tree", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        tree = {}
    try:
        bbox = cb.call("doc_bbox", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        bbox = {}
    feats = ((tree.get("tree") or {}).get("features")) or []
    owner_map = tree.get("owner_map") or {}  # the raw feature->topology table; de-smeared below

    # Instance/symmetry clustering: faces with the same shape+size are likely pattern copies.
    groups: dict = defaultdict(list)
    for f in faces:
        key = (f.get("surf"), round(f.get("area_or_len") or 0.0, 1),
               round(f.get("radius") or 0.0, 2), f.get("concavity"))
        groups[key].append(f)
    instanced = []
    for key, members in groups.items():
        if len(members) >= 3:
            sym = _axis_order(members)
            instanced.append({"surf": key[0], "area": key[1], "radius": key[2] or None,
                              "concavity": key[3], "count": len(members),
                              "sids": [m.get("sid") for m in members],
                              "pattern": sym})
    instanced.sort(key=lambda g: -g["count"])
    part_symmetry = next((g["pattern"] for g in instanced if g["pattern"]), None)

    bores = [{"sid": f.get("sid"), "radius": f.get("radius")} for f in faces
             if f.get("concavity") == "bore"]
    caps = [{"sid": f.get("sid"), "side": f.get("side"), "area": f.get("area_or_len")}
            for f in faces if f.get("surf") == "plane" and f.get("side")]

    att = _face_attribution(doc_id, feats, owner_map, faces)
    ord_owner = att["owner"]              # per doc_query face index: the de-smeared owner
    faces_by_owner = att["owned"]
    attributed = att["claimed"]           # raw owner_map count, for honest bookkeeping below
    reattributed = att["reattributed"]
    order = _solid_feature_order(feats)
    rank = {fid: i for i, fid in enumerate(order)}
    area_by_owner = {fid: sum(f.get("area_or_len") or 0.0 for f in fl)
                     for fid, fl in faces_by_owner.items()}

    # "touches": bridge adjacency edges across owner sets. doc_query's face-match order IS
    # doc_adjacency's `ord` numbering (both walk the same TopExp_Explorer order), so index i
    # in `faces` is face `ord` i.
    touches: dict = defaultdict(set)
    try:
        adj = cb.call("doc_adjacency", {"doc_id": doc_id})
        for e in adj.get("edges", []):
            between = e.get("between") or []
            if len(between) != 2:
                continue
            ia, ib = between
            if not (0 <= ia < len(ord_owner)) or not (0 <= ib < len(ord_owner)):
                continue
            oa, ob = ord_owner[ia], ord_owner[ib]
            if oa and ob and oa != ob:
                touches[oa].add(ob)
                touches[ob].add(oa)
    except Exception:  # noqa: BLE001
        pass

    names = {ft["id"]: ft.get("name") for ft in feats
             if isinstance(ft, dict) and ft.get("id") in rank}
    feat_summary = []
    for ft in feats:
        if not isinstance(ft, dict):
            continue
        fid = ft.get("id")
        if not fid or ft.get("type") in _NON_BODY_FEATURE_TYPES:
            continue
        owned = faces_by_owner.get(fid, [])
        entry = {"id": fid, "name": ft.get("name"), "type": ft.get("type"), "faces": len(owned)}
        # Honest bookkeeping: the raw owner_map count stays visible whenever we changed it.
        if attributed.get(fid, 0) != len(owned):
            entry["attributed_faces"] = attributed.get(fid, 0)
        # A multi-body import is a ROSTER, not one opaque blob: the failed merge session
        # could not tell which of a 760-body frame import was legacy geometry, so name every
        # body with its 1-based ordinal. The roster is the POST-FILTER set — keep_bodies /
        # exclude_bodies drop bodies at import time, and narrating the source list made the
        # model address bodies this document never registered. Ordinals and refs follow the
        # kept renumbering (first survivor is `<fid>`, the rest `<fid>#1`, `<fid>#2`, … —
        # contracts/feature_params.md), which is what `<fid>#k` addressing takes; a re-import
        # filter still speaks SOURCE ordinals, kept visible as `source_ordinal`.
        if ft.get("type") == "import":
            params = ft.get("params") if isinstance(ft.get("params"), dict) else ft
            enames = params.get("embedded_body_names")
            keep_sel, drop_sel = params.get("keep_bodies"), params.get("exclude_bodies")
            filtered = isinstance(keep_sel, list) or isinstance(drop_sel, list)
            if isinstance(enames, list) and (len(enames) > 1 or filtered):
                kept = _kept_import_bodies(enames, keep_sel, drop_sel)
                entry["body_count"] = len(kept)
                if len(kept) != len(enames):
                    entry["filtered_from"] = len(enames)
                entry["bodies"] = [
                    {"ordinal": j + 1, "name": nm,
                     "ref": fid if j == 0 else f"{fid}#{j}",
                     **({"source_ordinal": src} if src != j + 1 else {})}
                    for j, (src, nm) in enumerate(kept)]
        if owned:
            anchor = _pick_anchor(fid, touches.get(fid, ()), rank, area_by_owner)
            placement = _feature_placement(owned, bbox, ft, anchor)
            if placement is not None:
                placement["touches"] = sorted(touches.get(fid, []))
                if anchor:
                    placement["anchor_label"] = f'{anchor} {names.get(anchor) or ""}'.strip()
                entry["placement"] = placement
        elif attributed.get(fid, 0):
            # Every face the kernel gave it belongs to an earlier feature — say so, rather
            # than keep a face it demonstrably did not create.
            entry["unattributed"] = True
        else:
            entry["consumed"] = True
        feat_summary.append(entry)

    return {
        "part": (tree.get("tree") or {}).get("name"),
        # Assembly rows are components; a multibody component carries one source
        # ordinal per solid. Surface-only rows still represent one body.
        "bodies": sum(max(1, len(body["source_ordinals"])) for body in bodies),
        "bbox_mm": bbox.get("size"),
        "faces": len(faces), "edges": len(edges),
        "symmetry": part_symmetry,
        "feature_groups": feat_summary,
        "reattributed_faces": reattributed,
        "instances": instanced,
        "bores": bores,
        "planar_caps": caps,
    }


def _outline_text(d: dict) -> str:
    lines = []
    bb = d.get("bbox_mm")
    bbs = (f"{bb[0]:.0f}x{bb[1]:.0f}x{bb[2]:.0f} mm" if isinstance(bb, list) and len(bb) == 3
           else "?")
    lines.append(f'Part "{d.get("part") or "part"}"  bbox {bbs}  | {d["bodies"]} bodies, {d["faces"]} faces, '
                 f'{d["edges"]} edges')
    if d.get("symmetry"):
        s = d["symmetry"]
        lines.append(f'Symmetry: {s["order"]}-fold {s["type"]} about +{s["axis"].upper()}')
    if d.get("feature_groups"):
        lines.append("Features (placement narrative, one line each):")
        for fg in d["feature_groups"]:
            lines.append(f'  - {_feature_group_line(fg)}')
            if fg.get("bodies"):
                kept = ("body" if fg["body_count"] == 1 else "bodies")
                src = (f' (kept from {fg["filtered_from"]} in the source file by this '
                       f'import\'s keep_bodies/exclude_bodies)' if fg.get("filtered_from")
                       else '')
                lines.append(f'      {fg["body_count"]} embedded {kept}{src} — address one '
                             f'by its `ref` below, or drop/keep them at import time with '
                             f'import_part(exclude_bodies=[...]) / keep_bodies=[...] '
                             f'(SOURCE ordinals or names):')
                for bd in fg["bodies"][:40]:
                    srco = (f' [source #{bd["source_ordinal"]}]' if bd.get("source_ordinal")
                            else '')
                    lines.append(f'        {bd["ordinal"]}. {bd["name"]} -> {bd["ref"]}{srco}')
                if fg["body_count"] > 40:
                    lines.append(f'        … {fg["body_count"] - 40} more (full roster in '
                                 f'`digest.feature_groups[].bodies`)')
        if d.get("reattributed_faces"):
            lines.append(f'    ({d["reattributed_faces"]} face(s) the kernel credited to a '
                         'boolean were re-credited to the earlier feature that authored the '
                         'surface they lie on)')
    if d.get("instances"):
        lines.append("Repeated/instanced groups (likely patterns - address as a set):")
        for g in d["instances"]:
            pat = (f' [{g["pattern"]["order"]}x {g["pattern"]["type"]} about '
                   f'+{g["pattern"]["axis"].upper()}]' if g.get("pattern") else "")
            r = f' r{g["radius"]}' if g.get("radius") else ""
            conc = f' {g["concavity"]}' if g.get("concavity") else ""
            lines.append(f'  - {g["count"]}x {g["surf"]}{r}{conc} (area {g["area"]}){pat} '
                         f'-> sids {g["sids"][:6]}')
    if d.get("bores"):
        lines.append("Bores/holes: " + ", ".join(
            f'{b["sid"]}(r{b["radius"]})' for b in d["bores"][:12]))
    if d.get("planar_caps"):
        lines.append("Flat faces by side: " + ", ".join(
            f'{c["side"]}={c["sid"]}' for c in d["planar_caps"][:12]))
    lines.append('Refer to any face/edge by its sid (e.g. "fillet the edge ...") or expand one '
                 'with hover(sid).')
    return "\n".join(lines)


# ---------------------------------------------------------------- A1: describe_sketch
_DIM_CONSTRAINT_TYPES = {"distance", "length", "angle", "radius", "diameter", "arc_length"}


def _sketch_authored_params(feat: Optional[dict]) -> dict:
    """The sketch feature's authored {entities, constraints} — canonical `params` shape, or the
    flat fallback some older docs store (mirrors the core's `sketch_params`)."""
    if not feat:
        return {"entities": [], "constraints": []}
    params = feat.get("params")
    if isinstance(params, dict) and ("entities" in params or "constraints" in params):
        return params
    return {"entities": feat.get("entities") or [], "constraints": feat.get("constraints") or []}


def _fmt_xy(v) -> str:
    return f'({v[0]:.3g},{v[1]:.3g})' if isinstance(v, list) and len(v) >= 2 else "?"


def _entity_geom_str(etype: str, rec: dict) -> str:
    """A short 'coords/dims' description for one sketch entity, from its solved (or authored,
    if unsolved) coordinate record."""
    if etype == "line":
        p1, p2 = rec.get("p1"), rec.get("p2")
        length = (math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                  if isinstance(p1, list) and isinstance(p2, list) else None)
        return f'{_fmt_xy(p1)}\u2192{_fmt_xy(p2)}' + (f' len {length:.3g}' if length else "")
    if etype == "circle":
        return f'ctr{_fmt_xy(rec.get("center"))} r{rec.get("radius")}'
    if etype == "arc":
        return (f'ctr{_fmt_xy(rec.get("center"))} r{rec.get("radius")} '
               f'{rec.get("start")}\u00b0\u2192{rec.get("end")}\u00b0')
    if etype == "point":
        return _fmt_xy(rec.get("p"))
    if etype in ("ellipse", "ellipse_arc"):
        return f'ctr{_fmt_xy(rec.get("center"))} rx{rec.get("rx")} ry{rec.get("ry")}'
    if etype == "slot":
        return f'{_fmt_xy(rec.get("p1"))}\u2192{_fmt_xy(rec.get("p2"))} w{rec.get("width")}'
    if etype == "arc_slot":
        return f'ctr{_fmt_xy(rec.get("center"))} r{rec.get("radius")} w{rec.get("width")}'
    if etype in ("spline", "eq_curve"):
        pts = rec.get("points") or []
        return f'{len(pts)} pt(s)' if pts else "parametric curve"
    keys = [k for k in rec if k not in ("id", "type", "construction")
            and not isinstance(rec[k], (list, dict))]
    return ", ".join(f'{k}={rec[k]}' for k in keys[:4]) or "?"


def _sketch_digest(sketch: str, overview: dict, regions_resp: dict,
                   feat: Optional[dict]) -> dict:
    authored = _sketch_authored_params(feat)
    live_by_id = {e.get("id"): e for e in (overview.get("entities") or [])
                 if isinstance(e, dict) and e.get("id")}
    statuses = overview.get("statuses") or {}

    entities = []
    for e in authored.get("entities") or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        etype = e.get("type", "?")
        live = live_by_id.get(eid, e)
        entities.append({"id": eid, "type": etype, "geom": _entity_geom_str(etype, live),
                         "status": statuses.get(eid, "?"),
                         "construction": bool(e.get("construction"))})

    by_kind: dict = defaultdict(list)
    dims = []
    for c in authored.get("constraints") or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type", "?")
        by_kind[ctype].append(c.get("id"))
        if ctype in _DIM_CONSTRAINT_TYPES:
            val = c.get("value") or {}
            dims.append({"id": c.get("id"), "type": ctype, "value": val.get("value"),
                        "expr": val.get("expr"), "driven": bool(c.get("driven"))})
    relation_counts = {k: len(v) for k, v in by_kind.items() if k not in _DIM_CONSTRAINT_TYPES}

    regions = []
    for r in regions_resp.get("regions") or []:
        if not isinstance(r, dict):
            continue
        outer = r.get("outer") or []
        us = [pt[0] for pt in outer if isinstance(pt, list) and len(pt) >= 2]
        vs = [pt[1] for pt in outer if isinstance(pt, list) and len(pt) >= 2]
        bbox = (None if not (us and vs) else
               {"w": round(max(us) - min(us), 3), "h": round(max(vs) - min(vs), 3),
                "at": [round(min(us), 3), round(min(vs), 3)]})
        regions.append({"index": r.get("index"), "area": r.get("area"),
                        "is_material": r.get("is_material", True), "bbox": bbox})

    status = overview.get("status", "")
    dof = overview.get("dof", 0)
    over_ids = [k for k, v in statuses.items() if v == "over"]
    skipped = overview.get("skipped_constraints") or []
    from services.mcp.ascii2d import sketch_design_intent
    intent = sketch_design_intent(
        authored.get("entities") or [], authored.get("constraints") or [],
        health={**overview, "external_refs": authored.get("external_refs") or []})
    if status == "over" or over_ids:
        verdict = "Over-defined" + (f': conflicting entities {over_ids} — remove one constraint.'
                                    if over_ids else ' — remove a conflicting constraint.')
        fully_defined = False
    elif intent["pinned_only"]:
        verdict = ("Pinned instead of constrained — replace profile fix pins with "
                   "relations/driving dimensions, then sketch_fully_define.")
        fully_defined = False
    elif dof == 0 and not skipped:
        verdict = "Fully defined (dof 0) — constrained parametrically, not pinned_only."
        fully_defined = True
    else:
        verdict = (f'Under-defined: {dof} degree(s) of freedom remain — add geometric relations '
                  'and/or driving dimensions.')
        fully_defined = False
    if skipped:
        verdict += f' {len(skipped)} constraint(s) skipped by the solver: {skipped}.'

    frame = overview.get("plane_frame") or []
    plane = None
    if isinstance(frame, list) and len(frame) >= 16:
        plane = {"origin": [round(frame[3], 3), round(frame[7], 3), round(frame[11], 3)],
                "u_axis": [round(frame[0], 3), round(frame[4], 3), round(frame[8], 3)],
                "v_axis": [round(frame[1], 3), round(frame[5], 3), round(frame[9], 3)],
                "normal": [round(frame[2], 3), round(frame[6], 3), round(frame[10], 3)]}
    plane_ref = (feat.get("refs") or {}).get("plane") if feat else None
    plane_name = (plane_ref if isinstance(plane_ref, str)
                 else plane_ref.get("kind") if isinstance(plane_ref, dict) else None)

    return {"sketch": sketch, "plane": plane, "plane_ref": plane_name,
           "entities": entities, "relation_counts": relation_counts, "dimensions": dims,
           "regions": regions, "status": status, "dof": dof,
           "fully_defined": fully_defined, "skipped_constraints": skipped, "verdict": verdict,
           **intent}


def _sketch_text(d: dict) -> str:
    lines = []
    p = d.get("plane")
    if p:
        named = f' on {d["plane_ref"]}' if d.get("plane_ref") else ""
        lines.append(f'Plane{named}: origin {p["origin"]}, u-axis {p["u_axis"]}, '
                     f'normal {p["normal"]}')
    else:
        lines.append("Plane: unknown (no plane_frame reported)")

    ents = d.get("entities") or []
    lines.append(f'Entities ({len(ents)}):')
    if ents:
        for e in ents:
            cons = " [construction]" if e.get("construction") else ""
            lines.append(f'  {e["id"]} {e["type"]} {e["geom"]} [{e["status"]}]{cons}')
    else:
        lines.append("  (none)")

    rel = d.get("relation_counts") or {}
    dims = d.get("dimensions") or []
    lines.append("Constraints:")
    if rel:
        lines.append("  relations: " + "; ".join(f'{k} x{v}' for k, v in sorted(rel.items())))
    if dims:
        dim_txt = []
        for c in dims:
            v = c.get("value")
            vt = "?" if v is None else (f'{v} ({c["expr"]})' if c.get("expr") else f'{v}')
            dim_txt.append(f'{c["id"]} {c["type"]}={vt}' + (' [ref]' if c.get("driven") else ""))
        lines.append("  driving dims: " + "; ".join(dim_txt))
    if not rel and not dims:
        lines.append("  (none)")

    regs = d.get("regions") or []
    if regs:
        parts = []
        for r in regs:
            kind = "region" if r.get("is_material", True) else "void"
            area = f'{r["area"]:.3g} mm\u00b2' if r.get("area") is not None else "?"
            bx = r.get("bbox")
            bt = f' {bx["w"]:.3g}\u00d7{bx["h"]:.3g} mm at {bx["at"]}' if bx else ""
            parts.append(f'R{r.get("index")} {kind} area {area}{bt}')
        lines.append(f'Regions: {len(regs)} closed loop(s) - ' + "; ".join(parts))
    else:
        lines.append("Regions: 0 closed loops")

    lines.append(d.get("verdict", ""))
    lines.extend(f"Intent: {finding['message']}" for finding in d.get("findings") or [])
    return "\n".join(lines)


# ---------------------------------------------------------------- A2: describe_scene
def _iso_dir() -> tuple:
    vx, vy, vz = 1.0, 1.0, 0.8
    n = math.sqrt(vx * vx + vy * vy + vz * vz)
    return (vx / n, vy / n, vz / n)


# Scene->camera direction per named view — matches render_part's documented convention
# (front looks at the -Y face, right at +X, top at +Z; iso is the legacy corner shot).
_VIEW_DIRS = {
    "front": (0.0, -1.0, 0.0), "back": (0.0, 1.0, 0.0),
    "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
    "iso": _iso_dir(),
}


def _view_dir(name: str) -> tuple:
    return _VIEW_DIRS.get(name, _VIEW_DIRS["iso"])


def _visible_faces(faces: list, direction: tuple) -> list:
    """Faces whose signed normal points toward the camera (dot > 0.15) — a face-normal
    approximation of visibility. OCCLUSION IS NOT MODELED: a face behind another survives this
    filter if it faces the camera, exactly like a wireframe/normals-only render would show."""
    dx, dy, dz = direction
    scored = []
    for f in faces:
        n = f.get("normal")
        if not isinstance(n, list) or len(n) != 3:
            continue
        dot = n[0] * dx + n[1] * dy + n[2] * dz
        if dot > 0.15:
            scored.append((dot, f))
    scored.sort(key=lambda t: -t[0])
    return [f for _, f in scored]


def _bbox_str(size) -> str:
    return (f'{size[0]:.0f}x{size[1]:.0f}x{size[2]:.0f} mm'
           if isinstance(size, list) and len(size) == 3 else "unknown size")


def _scene_caption(doc_id: str, views: list) -> str:
    """ONE-PARAGRAPH deterministic caption for a render: the view name(s), the part bbox, and
    the principal visible labelled faces per view — computed from B-rep face normals with NO
    occlusion modeling. Dependency-free (doc_id + views only) so render_part/contact_sheet can
    import this straight for their non-vision caption without pulling in the rest of GSL."""
    try:
        faces = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}).get("matches", [])
    except Exception:  # noqa: BLE001
        faces = []
    try:
        bbox = cb.call("doc_bbox", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        bbox = {}
    bbs = _bbox_str(bbox.get("size"))
    view_names = views or ["iso"]
    parts = [f'{"/".join(view_names)} view of a {bbs} part.']
    for name in view_names:
        vis = _visible_faces(faces, _view_dir(name))
        labelled = [f'{f.get("sid")}({f.get("side") or f.get("surf")})' for f in vis[:6]]
        if labelled:
            more = f' +{len(vis) - 6} more' if len(vis) > 6 else ""
            parts.append(f'{name}: {", ".join(labelled)}{more}.')
        else:
            parts.append(f'{name}: no faces facing the camera.')
    parts.append("Computed from B-rep normals; occlusion not modeled.")
    return " ".join(parts)


# ================================================================ narration helpers (geometry)


def _gcode_digest(nc_text: str) -> dict:
    """Parse G-code text for bounds (from G0/G1 moves), feed range, tool-change count.
    Modal G-code interpretation — deterministic subset only. Never computes toolpath semantics."""
    import re as _re
    lines = nc_text.splitlines()

    xs, ys, zs, feeds = [], [], [], []
    tool_changes = 0
    x_cur = y_cur = z_cur = None  # track modal position so we detect motion-line X/Y/Z words

    for line in lines:
        line = line.split(";")[0].split("(")[0].strip().upper()
        # Tool changes: T<n> M6 pattern
        if _re.search(r'\bM0*6\b', line):
            tool_changes += 1
        # Feed value
        fm = _re.search(r'F([\d.]+)', line)
        if fm:
            try:
                feeds.append(float(fm.group(1)))
            except ValueError:
                pass
        # Coordinate words on G0/G1 lines (or modal continuation)
        if _re.search(r'\bG0*[01]\b', line) or (x_cur is not None and _re.search(r'[XYZ][+-]?[\d.]', line)):
            xm = _re.search(r'X([+-]?[\d.]+)', line)
            ym = _re.search(r'Y([+-]?[\d.]+)', line)
            zm = _re.search(r'Z([+-]?[\d.]+)', line)
            if xm:
                try:
                    v = float(xm.group(1)); xs.append(v); x_cur = v
                except ValueError:
                    pass
            if ym:
                try:
                    v = float(ym.group(1)); ys.append(v); y_cur = v
                except ValueError:
                    pass
            if zm:
                try:
                    v = float(zm.group(1)); zs.append(v); z_cur = v
                except ValueError:
                    pass

    def _rng(vals):
        return [min(vals), max(vals)] if vals else [0.0, 0.0]

    return {
        "lines": len(lines),
        "bounds": {"x": _rng(xs), "y": _rng(ys), "z": _rng(zs)},
        "feed_min": min(feeds) if feeds else 0.0,
        "feed_max": max(feeds) if feeds else 0.0,
        "tool_changes": tool_changes,
    }


def _face_label(f: dict) -> str:
    """'face <sid> (<surf> on <side>)' — the same sid namespace describe_part hands out."""
    sid = f.get("sid", "?")
    surf = f.get("surf", "?")
    side = f.get("side")
    return f'face {sid} ({surf} on {side})' if side else f'face {sid} ({surf})'


def _study_bc_faces(doc_id: str, study: dict) -> dict:
    """Sid join for the study's boundary conditions: resolve the STORED fixture/load face
    queries (persisted as the original plain-English queries by _persist_study) against the
    CURRENT face layer — the same sid namespace describe_part uses. Returns
    {"fixed": [labels], "loaded": [labels]}; each list empty when a query no longer resolves
    (the caller degrades gracefully). The per-node stress field is NOT persisted in the study
    record, so the max-stress node cannot be located from stored data — the loaded faces are
    the traceable stand-in for where the load (and typically the stress path) enters the part."""
    out: dict = {"fixed": [], "loaded": []}
    try:
        faces = cb.call("doc_query", {"doc_id": doc_id,
                                      "query": {"of": "face"}}).get("matches", [])
        by_side: dict = {}
        for f in faces:
            if f.get("side"):
                by_side.setdefault(f["side"], []).append(f)

        def _resolve(q) -> list:
            # A stored query is either a plain side name ("bottom") or a structured
            # {"of":"face","side":...} dict — match by side against the live face list.
            side = q if isinstance(q, str) else (q.get("side") if isinstance(q, dict) else None)
            return by_side.get(side, []) if side else []

        for q in study.get("fixtures") or []:
            out["fixed"].extend(_face_label(f) for f in _resolve(q))
        for ld in study.get("loads") or []:
            if isinstance(ld, dict) and "face" in ld:
                out["loaded"].extend(_face_label(f) for f in _resolve(ld["face"]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _describe_toolpaths_text(job_id: Optional[str] = None, audit: bool = True) -> str:
    """Narrate a CAM job's toolpaths — data from cam_tree, cam_simulate, cam_post, cam_audit."""
    from services.mcp.cam_tools import _CAM  # shared job state  # noqa: PLC0415

    jid = job_id or _CAM.get("doc_id")
    if not jid:
        return _dumps({"ok": False, "error": "No CAM job. Call cam_new_job first."})

    # 1. Job tree — authoritative source for ops, tools, setup hierarchy.
    # cam_tree returns {"revision", "tree": {id,name,units,post,tools,setups}, "stats"}
    # (core/cam/session.cpp tree_of) — the job payload is nested under "tree".
    try:
        tree_r = cb.call("cam_tree", {"doc_id": jid})
    except Exception as e:  # noqa: BLE001
        return _dumps({"ok": False, "error": f"cam_tree failed: {e}"})

    tree = tree_r.get("tree") or {}
    job_name = tree.get("name", "CAM Job")
    setups = tree.get("setups", [])
    tools_by_num = {t["number"]: t for t in tree.get("tools", [])}

    total_ops = sum(len(s.get("operations", [])) for s in setups)
    lines = [f'CAM job "{job_name}" — {len(setups)} setup(s), {total_ops} operation(s) total']

    # 2. Simulation results (best-effort). cam_simulate returns {"results": [SimResult...]}
    # with per-setup keys machined_fraction (0..1), cycle_time_s, gouges, rapid_collisions
    # (core/cam/stock_sim.cpp SimResult::to_json) — narrated verbatim, never recomputed.
    sim_by_setup: dict = {}
    sim_available = False
    try:
        sim_r = cb.call("cam_simulate", {"doc_id": jid,
                                         "options": {"resolution": 0.5,
                                                     "check_gouge": True,
                                                     "check_rapid": True}})
        for s in sim_r.get("results", []):
            sim_by_setup[s.get("setup_id")] = s
        sim_available = True
    except Exception:  # noqa: BLE001
        pass

    # 3. G-code digest (best-effort). cam_post returns {ok, gcode, line_count, tool_changes,
    # est_time_s, warnings} (core/cam/post.cpp PostResult::to_json) — line/tool-change counts
    # come from the post itself; only bounds + feed range are parsed from the emitted text.
    post_r = None
    gcode_text = None
    try:
        post_r = cb.call("cam_post", {"doc_id": jid,
                                      "options": {"program_name": "Chudware",
                                                  "program_number": 1000}})
        gcode_text = post_r.get("gcode")
    except Exception:  # noqa: BLE001
        pass

    for setup in setups:
        setup_name = setup.get("name", "Setup")
        machine_info = setup.get("machine") or {}
        machine = machine_info.get("kind", "?") if isinstance(machine_info, dict) else str(machine_info)
        ops = setup.get("operations", [])
        setup_id = setup.get("id")

        lines.append(f'\nSetup "{setup_name}" [{machine}] — {len(ops)} op(s)')

        for op in ops:
            op_name = op.get("name", op.get("type", "?"))
            op_type = op.get("type", "?")
            tool_num = op.get("tool_number") or op.get("tool", "?")
            tool = tools_by_num.get(tool_num, {})
            tool_type = tool.get("type", "?")
            tool_diam = tool.get("diameter", "?")

            lines.append(f'  Op "{op_name}" [{op_type}]: T{tool_num} {tool_type} ø{tool_diam}mm')

            params = op.get("params", {}) or {}
            stepdown = op.get("stepdown") or params.get("stepdown")
            stepover = op.get("stepover") or params.get("stepover")
            stock = op.get("stock_to_leave") or params.get("stock_to_leave")
            engage = op.get("engage_pct") or params.get("engage_pct")

            param_parts = []
            if stepdown is not None:
                param_parts.append(f"stepdown {stepdown:.3g}mm")
            if stepover is not None:
                param_parts.append(f"stepover {stepover:.3g}mm")
            if stock is not None:
                param_parts.append(f"stock-to-leave {stock:.3g}mm")
            if param_parts:
                lines.append(f'    {", ".join(param_parts)}')
            if engage is not None:
                lines.append(f'    Engage: {engage:.1f}%')

        # Simulation for this setup — SimResult keys narrated verbatim; machined_fraction
        # is a 0..1 fraction, formatted as a percentage (formatting, not recomputation).
        sim = sim_by_setup.get(setup_id)
        if sim:
            frac = sim.get("machined_fraction")
            cycle = sim.get("cycle_time_s")
            gouges = sim.get("gouges", 0)
            rapids = sim.get("rapid_collisions", 0)
            remaining = sim.get("remaining_volume_mm3")
            sim_parts = []
            if frac is not None:
                sim_parts.append(f"machined {frac * 100.0:.1f}%")
            if remaining is not None:
                sim_parts.append(f"remaining stock {remaining:.0f} mm3")
            if cycle is not None:
                sim_parts.append(f"cycle ~{cycle:.1f}s")
            lines.append(f'  Simulation: {", ".join(sim_parts) if sim_parts else "no data"}'
                         '  (source: cam_simulate)')
            lines.append(f'  Gouges: {gouges}; rapid-through-stock collisions: {rapids}'
                         '  (source: cam_simulate)')
        elif not sim_available:
            lines.append('  Simulation: not run — call cam_simulate for cycle time and gouge checks')
        else:
            lines.append('  Simulation: no data for this setup')

    # G-code digest — one per posted program (cam_post posts the whole job).
    if gcode_text:
        digest = _gcode_digest(gcode_text)
        b = digest["bounds"]
        n_lines = post_r.get("line_count", digest["lines"])
        n_tc = post_r.get("tool_changes", digest["tool_changes"])
        est = post_r.get("est_time_s")
        est_txt = f', est. time {est:.1f}s' if isinstance(est, (int, float)) else ""
        lines.append(
            f'\nG-code digest (source: cam_post): {n_lines} lines, '
            f'bounds X[{b["x"][0]:.3g}..{b["x"][1]:.3g}] '
            f'Y[{b["y"][0]:.3g}..{b["y"][1]:.3g}] '
            f'Z[{b["z"][0]:.3g}..{b["z"][1]:.3g}] mm, '
            f'feed {digest["feed_min"]:.0f}–{digest["feed_max"]:.0f} mm/min, '
            f'{n_tc} tool change(s){est_txt}'
        )
    else:
        lines.append('\nG-code: not available — run cam_post first')

    if audit:
        try:
            ar = cb.call("cam_audit", {"doc_id": jid})
            ok = bool(ar.get("ok"))
            n_err = len(ar.get("errors") or [])
            n_warn = len(ar.get("warnings") or [])
            stats = ar.get("stats") or {}
            bounds = stats.get("bounds") or {}
            bx = bounds.get("x") or [0, 0]
            by = bounds.get("y") or [0, 0]
            bz = bounds.get("z") or [0, 0]
            fr = stats.get("feed_range_mm_min") or [0, 0]
            est = stats.get("est_time_s")
            status = "ok" if ok else "FAILED"
            est_s = f"{float(est):.1f}" if isinstance(est, (int, float)) else "?"
            lines.append(
                f'\nAudit (source: cam_audit): {status} — {n_err} error(s), {n_warn} warning(s); '
                f'bounds X[{bx[0]:.3g}..{bx[1]:.3g}] Y[{by[0]:.3g}..{by[1]:.3g}] '
                f'Z[{bz[0]:.3g}..{bz[1]:.3g}]; feed {fr[0]:.0f}–{fr[1]:.0f}; est. {est_s}s'
            )
        except Exception as e:  # noqa: BLE001
            lines.append(f'\nAudit unavailable: {e}')

    if not setups:
        lines.append('No setups configured — call cam_add_setup to add one.')

    src = "cam_job_tree, cam_simulate, cam_post"
    if audit:
        src += ", cam_audit"
    lines.append(f'\n(Source: {src} — numbers narrated, never recomputed)')
    return _dumps({"ok": True, "text": "\n".join(lines)})


def _describe_results_text(doc_id: str, study_name: Optional[str] = None) -> str:
    """Narrate FEA/CFD results — data from the doc's persisted studies (sim_study_list, the
    same records _persist_study writes after every solve) — never recomputed."""
    studies = []
    try:
        studies_r = cb.call("sim_study_list", {"doc_id": doc_id})
        studies = studies_r.get("studies", [])
    except Exception:  # noqa: BLE001
        pass

    if not studies:
        return _dumps({"ok": True, "text":
                       "No simulation results attached — "
                       "run run_fea_static or run_fea_modal first."})

    if study_name:
        study = next((s for s in studies if s.get("name") == study_name), None)
        if not study:
            return _dumps({"ok": False,
                           "error": f"No study named {study_name!r}. "
                                    f"Available: {[s.get('name') for s in studies]}"})
    else:
        study = studies[-1]

    stype = study.get("type", "unknown")
    sname = study.get("name", "Study")
    mat = study.get("material", {}) or {}
    mat_name = mat.get("name", "unknown")
    yield_mpa = mat.get("yield_mpa")
    results = study.get("results", {}) or {}

    lines = [f'FEA results — "{sname}" ({stype})']

    mat_src = {"fea_static": "run_fea_static", "fea_modal": "run_fea_modal",
               "cfd_flow": "run_cfd_flow"}.get(stype, "the study record")
    if yield_mpa is not None:
        lines.append(f'Material: {mat_name} (yield {yield_mpa:.1f} MPa, '
                     f'source: {mat_src} material record)')
    elif mat_name != "unknown":
        lines.append(f'Material: {mat_name} (yield not specified)')

    if stype == "fea_static":
        vm = results.get("max_von_mises_mpa")
        disp = results.get("max_disp_mm")
        fos = results.get("min_fos")
        n_dof = study.get("n_dof")
        mesh_fine = study.get("mesh_fineness", 0.5)

        lines.append('\nStatic stress:')
        if vm is not None:
            lines.append(f'  Max von Mises stress: {vm:.1f} MPa  (source: run_fea_static)')
        else:
            lines.append('  Max von Mises stress: not available')
        bc = _study_bc_faces(doc_id, study)
        if bc["fixed"]:
            lines.append(f'  Fixed at: {", ".join(bc["fixed"][:4])}  (sid join of the stored '
                         'fixture queries)')
        if bc["loaded"]:
            lines.append(f'  Load applied on: {", ".join(bc["loaded"][:4])}  (sid join of the '
                         'stored load queries)')
        lines.append('  Max-stress node location: not stored in the study record — the study '
                     'persists summary scalars only; re-run run_fea_static for the live field.')

        if disp is not None:
            lines.append(f'  Max displacement: {disp:.3f} mm  (source: run_fea_static)')
        else:
            lines.append('  Max displacement: not available')

        if fos is not None:
            verdict = "holds" if fos >= 1.0 else "FAILS"
            lines.append(f'  Min factor of safety: {fos:.2f}  '
                         f'(yield / max stress; >1 means holds — {verdict})')
            if yield_mpa is not None and vm is not None:
                margin = yield_mpa - vm
                direction = "above" if margin >= 0 else "below"
                lines.append(f'  Margin: {abs(margin):.1f} MPa {direction} yield')
        else:
            lines.append('  Min factor of safety: not available')

        if n_dof is not None:
            lines.append(f'  Mesh: {n_dof} DOF  (mesh_fineness {mesh_fine:.2f}, tet10 elements)')

        lines.append('\nConvergence note: tet10 (2nd-order) elements used; re-run with finer mesh '
                     '(mesh_fineness → 0.8) to verify convergence for final design.')

    elif stype == "fea_modal":
        freqs = results.get("freqs_hz", [])
        lines.append('\nModal frequencies:')
        if freqs:
            for i, f in enumerate(freqs, 1):
                lines.append(f'  Mode {i}: {f:.1f} Hz  (source: run_fea_modal)')
        else:
            lines.append('  No frequency data available')
        lines.append('\nNote: frequencies depend on boundary conditions and material density; '
                     'rigid-body modes near 0 Hz indicate insufficient fixtures.')

    elif "cfd" in stype:
        lines.append('\nCFD results:')
        for k, v in results.items():
            if isinstance(v, (int, float)):
                lines.append(f'  {k}: {v}  (source: run_cfd_flow)')

    else:
        lines.append(f'\nResults ({stype}):')
        for k, v in results.items():
            if isinstance(v, (int, float)):
                lines.append(f'  {k}: {v}')

    # Additive sim-advisor verdict (margin policy + beam anchor + suggestions).
    # Only run for fea_static studies (beam-anchor comparison only meaningful for static).
    if stype == "fea_static":
        try:
            from services.agent.advisor.sim_advisor import postcheck, narrate_postcheck  # noqa: PLC0415
            pc = postcheck(study)
            verdict_text = narrate_postcheck(pc)
            if verdict_text:
                lines.append(verdict_text)
        except Exception:  # noqa: BLE001
            pass  # advisor postcheck is advisory-only; never crash describe_results

    lines.append('\n(Source: sim_study_list — numbers narrated from the stored study, '
                 'never recomputed)')
    return _dumps({"ok": True, "text": "\n".join(lines)})


# ================================================================ narration helpers (assembly/board/project)


def _component_sources(doc_id: str) -> tuple:
    """(base, rows) for an assembly's components; rows give the stored and resolved source.

    Each row is {id, name, source, source_resolved}. `source` is the stored object exactly as
    the document persists it. `source_resolved` is the absolute file the engine opens: the
    stored path joined to `base` (the assembly file's folder, else the project folder of an
    unsaved assembly). It is None when a relative path has no base. A new assembly resolves
    relative paths against its own base, so callers reuse `source_resolved` (2026-10-10:
    `parts/redesign/claw_base.step+` copied into a new assembly opened under hostd's cwd)."""
    tree_r = cb.call("doc_tree", {"doc_id": doc_id}) or {}
    base = tree_r.get("component_source_base")
    base = base if isinstance(base, str) and base else None
    rows = []
    for comp in (tree_r.get("tree") or {}).get("components") or []:
        if not isinstance(comp, dict):
            continue
        source = comp.get("source") if isinstance(comp.get("source"), dict) else {}
        stored = source.get("path")
        rows.append({"id": comp.get("id"), "name": comp.get("name"), "source": source,
                     "source_resolved": (resolve_component_source(stored, base)
                                         if isinstance(stored, str) and stored else None)})
    return base, rows


def _describe_assembly_text(doc_id: str) -> str:
    """Narrate world placement, the mate-solve verdict, and collision-only fit.

    Calls asm_world_bodies, then doc_solve_mates for the verdict (status, dof), then
    eval_interference in zero-clearance threshold mode. A re-solve that moves nothing
    is not an edit (core/doc/session.cpp::doc_solve_mates), so describing a solved
    assembly never bumps its revision or marks it dirty. The native fit check
    evaluates geometry and exact overlap volumes, not minimum measured gaps.
    Preserve the raw fit evidence in the reply.
    """
    # 1. World-placed bodies
    try:
        wb_r = cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False})
    except Exception as e:  # noqa: BLE001
        return _err(e)

    if wb_r.get("ok") is False:
        return _dumps(wb_r)

    bodies = wb_r.get("bodies", [])
    is_multi = wb_r.get("multibody_part", False)
    n = len(bodies)

    if n == 0:
        return _dumps({"ok": False, "collision_free": False, "bodies": bodies, "text":
                       "No bodies found in this document — "
                       "add components (assembly) or features (part) first. "
                       "No collision qualification is possible without checked body pairs."})

    doc_kind = "multi-body part" if is_multi else "assembly"
    lines = [f'Assembly "{doc_id}" — {n} component(s) / bod(y/ies) [{doc_kind}]']

    # 2. Per-body placement narrative
    lines.append("\nComponent placement (world frame):")
    for b in bodies:
        name = b.get("name") or b.get("component") or "?"
        bbox = b.get("bbox") or {}
        bmin = bbox.get("min", [None, None, None])
        bmax = bbox.get("max", [None, None, None])
        bsize = bbox.get("size", [None, None, None])

        def _coord(v):
            return f"{v:.3g}" if isinstance(v, (int, float)) else "?"

        if all(v is not None for v in bsize):
            size_txt = (f"{_coord(bsize[0])} × {_coord(bsize[1])} × {_coord(bsize[2])} mm")
        else:
            size_txt = "?"
        if all(v is not None for v in (bmin[0], bmin[1], bmin[2])):
            origin_txt = (f"origin [{_coord(bmin[0])}, {_coord(bmin[1])}, {_coord(bmin[2])}]")
        else:
            origin_txt = "origin ?"
        psrc = placement_source_of(doc_id, b.get("component"))
        b["placement_source"] = psrc
        psrc_txt = f", placed by {psrc}" if psrc else ""
        lines.append(f"  {name}: {size_txt}, {origin_txt}{psrc_txt}  "
                     f"(source: asm_world_bodies)")

    # 2b. Component sources: the stored path and the absolute file the engine opens.
    components: list = []
    source_base = None
    if not is_multi:
        source_base, components = _component_sources(doc_id)
        unresolved = [c for c in components
                      if c["source_resolved"] is None and c["source"].get("path")]
        if components:
            lines.append(f"\nComponent sources: relative paths resolve against "
                         f"{source_base or 'no folder (no file and no project folder)'}. "
                         "To insert the same file into another assembly, use "
                         "components[].source_resolved as source.path.")
        for c in unresolved:
            lines.append(f"  WARNING {c['name'] or c['id']}: the relative source "
                         f"{c['source']['path']!r} has no base folder. Give an absolute path.")

    # 3. Solve/DOF summary, including solved-pose freedom beside mechanism mobility.
    try:
        sm_r = cb.call("doc_solve_mates", {"doc_id": doc_id})
        if sm_r.get("ok") is False:
            if sm_r.get("code") == "engine_connection_lost":
                return _dumps(sm_r)
            lines.append(f"\nMate solve failed: {sm_r.get('error') or sm_r.get('reason') or sm_r}")
        rebuild = sm_r.get("rebuild") or {}
        status = rebuild.get("doc_status") or sm_r.get("status") or ""
        if status:
            lines.append(f"\nMate solve: {status}  (source: doc_solve_mates)")
            if "dof" in rebuild:
                freedom = f"dof: {rebuild['dof']}"
                if "residual_dof" in rebuild:
                    freedom += f", residual_dof: {rebuild['residual_dof']}"
                lines.append(f"  {freedom}")
        elif sm_r.get("ok") is not False:
            lines.append("\nMate solve: solve completed (DOF detail not available via read RPC — "
                         "call solve_mates for the live result)")
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) == "engine_connection_lost":
            raise
        lines.append(f"\nMate solve: not available for this document type ({e})")

    # 4. Live collision classification: no hidden exact-distance work for close pairs.
    try:
        if_r = cb.call("eval_interference", {"doc_id": doc_id,
                                            "include_coincident": False,
                                            "min_clearance_mm": 0.0,
                                            "clearance_mode": "threshold"})
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if if_r.get("code") == "engine_connection_lost":
        return _dumps({**if_r, "ok": False})
    checked = if_r.get("pairs_checked")
    total = if_r.get("pairs_total")
    complete = (
        if_r.get("complete") is True
        and type(checked) is int and type(total) is int
        and checked == total and checked >= 0
        and not if_r.get("pair_failures") and not if_r.get("geometry_failures")
        and not if_r.get("error")
        and (if_r.get("ok") is True or bool(if_r.get("interferences")))
    )
    if complete:
        lines.append("\n" + _format_fit(if_r))
        if checked == 0:
            lines.append("No body pairs checked; this is not a collision-free qualification.")
    else:
        lines.append("\n" + _format_fit({
            **if_r,
            "summary": "Interference check incomplete; collision-free status is not established.",
        }))
        for failure in if_r.get("geometry_failures", []):
            lines.append(f"  Geometry failure: {failure}")
        for failure in if_r.get("pair_failures", []):
            lines.append(f"  Pair failure: {failure}")
        if if_r.get("error"):
            lines.append(f"  Fit error: {if_r['error']}")
    lines.append(f"  Pair coverage: {checked if checked is not None else 'unknown'} checked / "
                 f"{total if total is not None else 'unknown'} total.")
    lines.append("  Collision-only threshold check; minimum measured gap is not available.")

    # 5. Placement provenance: a transform-placed component nobody re-checked is the failure
    #    mode this narration exists to catch (a merged wheel exported off-frame).
    for w in placement_warnings(doc_id):
        lines.append(f"\nWARNING {w} — re-run check_interference and confirm the mating "
                     "references with measure_between / fit_cylinder_axis before exporting")

    lines.append("\n(Source: asm_world_bodies pre-solve placement snapshot; doc_solve_mates "
                 "live solve; eval_interference live threshold classification and overlap volumes)")
    return _dumps({"ok": complete, "text": "\n".join(lines), "bodies": bodies,
                   "components": components, "component_source_base": source_base,
                   "fit": if_r,
                   "collision_free": complete and checked > 0 and if_r.get("ok") is True
                   and not if_r.get("interferences")})


def _board_power_lines(engs: list, label: str) -> list:
    """Narration lines for per-component power + board total (honest nulls).

    ``engs`` is [(name, normalized_eng_or_None), ...]. Components without power data are
    counted, never zero-filled (contracts/ecad/boardphys.md).
    """
    try:
        from services.ecad import metadata as eng_meta  # STAYS: no C++ counterpart  # noqa: PLC0415
    except ImportError:
        return []
    totals = eng_meta.power_totals([e for _n, e in engs if e])
    lines = ["\nPower (engineering-metadata layer):"]
    for name, e in engs:
        if not e:
            lines.append(f"  {name}: power unknown (no metadata)")
            continue
        p = e.get("power_w") or {}
        typ, mx = p.get("typ"), p.get("max")
        if typ is None and mx is None:
            lines.append(f"  {name}: power unknown")
        else:
            t = f"{typ * 1000:.1f} mW typ" if typ is not None else "typ unknown"
            m = f"{mx * 1000:.1f} mW max" if mx is not None else "max unknown"
            lines.append(f"  {name}: {t}, {m}")
    if totals and totals.get("power_w_typ") is not None:
        lines.append(f"  {label} total: {totals['power_w_typ'] * 1000:.1f} mW typ"
                     + (f", {totals['power_w_max'] * 1000:.1f} mW max"
                        if totals.get("power_w_max") is not None else "")
                     + f"  ({totals['n_missing_power']} component(s) without power data)")
    elif totals and totals.get("power_w_max") is not None:
        lines.append(f"  {label} total: {totals['power_w_max'] * 1000:.1f} mW max"
                     f"  ({totals['n_missing_power']} component(s) without power data)")
    else:
        lines.append(f"  {label} total: unknown (no component carries power metadata)")
    lines.append("  (source: engine.ecad.metadata — seeds carry datasheet source notes)")
    return lines


def _describe_board_text(doc_id: str) -> str:
    """Narrate a PCB or schematic board's status as GSL text.

    Promotes ecad_status data into the narration contract layer. Reads directly from
    the ecad Python modules (same sources ecad_status uses) and reformats as narration
    text following contracts/narration.md section order.

    Data sources:
    1. ecad_session.get_session → kind (sch|pcb), component list, netlist
    2. engine.ecad.netlist.build_netlist → components, nets
    3. engine.ecad.erc.run_erc → ERC/DRC violation summary
    """
    # The session/netlist facades route to the C++ core via core_bridge RPCs.
    from services.mcp.ecad_common import (  # noqa: PLC0415
        _ECAD, _compile_of, _unconnected_pins, ecad_session, netlist_mod,
    )

    did = (doc_id or _ECAD["current"] or _ECAD["current_sch"]
           or _ECAD["current_pcb"])
    if not did:
        return _dumps({"ok": False,
                       "error": "No active ECAD document — open one with "
                                "ecad_new_schematic or ecad_new_pcb first."})
    try:
        session = ecad_session.get_session(did)
    except Exception as e:  # noqa: BLE001
        return _dumps({"ok": False,
                       "error": f"No active ECAD document: {e} — "
                                "open one with ecad_new_schematic or ecad_new_pcb first."})

    doc = session.doc
    kind = session.kind  # "sch" or "pcb"
    doc_name = doc.get("name", "board")

    if kind == "pcb":
        fps = doc.get("footprints", [])
        nets = doc.get("nets", [])
        tracks = doc.get("tracks", [])
        vias = doc.get("vias", [])
        outline = doc.get("outline") or {}
        has_outline = bool(outline.get("shapes"))
        lines = [
            f'Board "{doc_name}" — PCB layout',
            f'  {len(fps)} footprint(s), {len(nets)} net(s), '
            f'{len(tracks)} track(s), {len(vias)} via(s)  '
            f'(source: ecad_session)',
        ]
        if has_outline:
            lines.append("  Board outline: defined  (source: ecad_session)")
        else:
            lines.append("  Board outline: NOT defined — set one with a pcb_set_outline op")

        # DRC summary (best-effort)
        n_err = 0
        try:
            res = _compile_of(session).get("drc", [])  # C++ compile runs engine.ecad.drc
            viols = res if isinstance(res, list) else res.get("violations", [])
            n_err = sum(1 for v in viols if v.get("severity") == "error")
            n_warn = sum(1 for v in viols if v.get("severity") == "warning")
            if n_err:
                lines.append(f"\nDRC: {n_err} error(s), {n_warn} warning(s) — fix errors before "
                              "fabrication  (source: engine.ecad.drc)")
            else:
                lines.append(f"\nDRC: clean — {n_warn} warning(s)  (source: engine.ecad.drc)")
        except Exception:  # noqa: BLE001
            lines.append("\nDRC: not available")

        if not fps:
            lines.append("\nNo footprints placed — use ecad_find_part then ecad_place_symbol.")
        if fps:
            try:
                from services.ecad import metadata as eng_meta  # STAYS: no C++ counterpart  # noqa: PLC0415
                engs = [(f.get("ref") or f.get("id") or "?",
                         eng_meta.eng_for_footprint(f)) for f in fps]
                lines.extend(_board_power_lines(engs, "Board"))
            except Exception:  # noqa: BLE001 — power narration is advisory
                pass
        caveats = []
        if not has_outline:
            caveats.append("Board outline missing — required for fabrication (pcb_set_outline).")
        if n_err:
            caveats.append("DRC errors present — resolve before export.")
        if caveats:
            lines.append("\nCaveats:")
            for c in caveats:
                lines.append(f"  - {c}")
        lines.append("\n(Source: ecad_session, engine.ecad.drc — numbers narrated, never recomputed)")
        return _dumps({"ok": True, "text": "\n".join(lines)})

    # ---- schematic ----
    nl = netlist_mod.build_netlist(doc)
    components = nl.get("components", [])
    nets_dict = nl.get("nets", {})
    net_list = sorted(nets_dict.items())

    lines = [
        f'Board "{doc_name}" — schematic',
        f'  {len(components)} component(s), {len(net_list)} net(s)  '
        f'(source: engine.ecad.netlist)',
    ]

    # Component roster
    if components:
        lines.append("\nComponents:")
        for c in components:
            ref = c.get("ref", "?")
            val = c.get("value", "")
            libid = c.get("libid", "")
            fp = c.get("footprint") or ""
            fp_note = f", fp={fp}" if fp else ", no footprint"
            lines.append(f"  {ref}: {val} ({libid}{fp_note})")
    else:
        lines.append("\nNo components — add parts with ecad_find_part + ecad_place_symbol.")

    # Per-component power + total (honest nulls — contracts/ecad/boardphys.md)
    if components:
        try:
            from services.ecad import metadata as eng_meta  # STAYS: no C++ counterpart  # noqa: PLC0415
            engs = [(c.get("ref") or "?",
                     eng_meta.resolve_eng(libid=c.get("libid") or "",
                                          fpid=c.get("footprint") or "",
                                          overrides=c.get("eng_overrides")))
                    for c in components]
            lines.extend(_board_power_lines(engs, "Design"))
        except Exception:  # noqa: BLE001 — power narration is advisory
            pass

    # Net roster (cap at 20 to stay token-bounded)
    if net_list:
        lines.append("\nNets:")
        for name, members in net_list[:20]:
            pins = ", ".join(f"{m.get('ref')}.{m.get('pin')}" for m in members)
            lines.append(f"  {name} ({len(members)} pin(s)): {pins}")
        if len(net_list) > 20:
            lines.append(f"  +{len(net_list) - 20} more nets")
    else:
        lines.append("\nNo nets — wire pins with ecad_wire.")

    # ERC summary
    n_erc_err = 0
    try:
        violations = _compile_of(session).get("erc", []) or []  # C++ compile runs engine.ecad.erc
        n_erc_err = sum(1 for v in violations if v.get("severity") == "error")
        n_erc_warn = sum(1 for v in violations if v.get("severity") == "warning")
        if n_erc_err:
            lines.append(f"\nERC: {n_erc_err} error(s), {n_erc_warn} warning(s)  "
                         "(source: engine.ecad.erc)")
        else:
            lines.append(f"\nERC: clean — {n_erc_warn} warning(s)  (source: engine.ecad.erc)")
    except Exception:  # noqa: BLE001
        lines.append("\nERC: not available")

    # A single-pin net is still unconnected: reuse the ECAD family's sole definition.
    unconnected_records = _unconnected_pins(doc, nl)
    unconnected = [f"{p.get('ref') or '?'}.{p.get('pin') or '?'}"
                   for p in unconnected_records]

    if unconnected:
        sample = ", ".join(unconnected[:6])
        more = f" (+{len(unconnected) - 6} more)" if len(unconnected) > 6 else ""
        lines.append(f"\nUnconnected pins ({len(unconnected)}): {sample}{more} — "
                     "wire with ecad_wire or mark no-connect.")
    else:
        lines.append("\nAll pins connected.")

    caveats = []
    if n_erc_err:
        caveats.append("ERC errors present — fix before simulation or layout.")
    if unconnected:
        caveats.append(f"{len(unconnected)} unconnected pin(s) — wire or mark no-connect.")
    if not components:
        caveats.append("Empty schematic — add parts first.")
    if caveats:
        lines.append("\nCaveats:")
        for c in caveats:
            lines.append(f"  - {c}")

    lines.append("\n(Source: engine.ecad.netlist, engine.ecad.erc — "
                 "numbers narrated, never recomputed)")
    return _dumps({"ok": True, "text": "\n".join(lines)})


def _describe_project_text(run: dict | None = None, notes: list | None = None) -> str:
    """Narrate a DAG run's status: nodes done/blocked, budgets, open questions.

    Accepts an optional ``run`` dict matching contracts/dag_schema.md; returns
    a no-run explanation when no data is supplied.

    ``run["brief"]`` (a {field: value} map assembled by _describe_project_args from the
    semantic fields a model wrote instead of a run dict) is narrated back verbatim, so
    nothing the caller supplied silently disappears. ``notes`` is echoed as `interpreted`.

    Data sources: the caller-supplied run dict (no engine RPC — DAG is Python-side).
    """
    if run is None:
        out = {"ok": True, "text":
               "No DAG run data supplied — pass run=<dag_run_dict> from "
               "dag_status()['run']. See contracts/dag_schema.md "
               "for the expected shape."}
        if notes:
            out["interpreted"] = notes
        return _dumps(out)

    run_id = run.get("id") or run.get("run_id") or "?"
    name = run.get("name") or run.get("project") or run_id
    nodes = run.get("nodes") or []
    budget = run.get("budget") or {}
    questions = run.get("open_questions") or []

    # Node status counts
    done = [n for n in nodes if n.get("status") in ("done", "review")]
    blocked = [n for n in nodes if n.get("status") == "blocked"]
    doing = [n for n in nodes if n.get("status") == "doing"]
    todo = [n for n in nodes if n.get("status") in ("todo", "ready", "pending")]

    lines = [
        f'Project "{name}" (DAG run {run_id})',
        f'  {len(nodes)} node(s) total: {len(done)} done/review, {len(doing)} in-progress, '
        f'{len(blocked)} blocked, {len(todo)} pending',
    ]

    # Budget
    if budget:
        spent = budget.get("spent") or budget.get("tokens_used")
        cap = budget.get("cap") or budget.get("tokens_cap")
        if spent is not None and cap is not None:
            pct = spent / cap * 100.0 if cap else 0.0
            lines.append(f"  Budget: {spent:,} / {cap:,} tokens used ({pct:.1f}%)  "
                         "(source: dag_run.budget)")
        elif spent is not None:
            lines.append(f"  Budget: {spent:,} tokens used  (source: dag_run.budget)")

    # Whatever the caller described in fields instead of a run dict — narrated back, never
    # dropped and never treated as planning input (this tool narrates, it does not plan).
    brief = run.get("brief") if isinstance(run.get("brief"), dict) else None
    if brief:
        lines.append("\nSupplied project description (narrated back, not planned from):")
        for k, v in brief.items():
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v)
            elif isinstance(v, dict):
                v = json.dumps(v, sort_keys=True)
            lines.append(f"  {k}: {v}")

    # Done/review nodes
    if done:
        lines.append("\nCompleted nodes:")
        for n in done:
            nid = n.get("id") or n.get("task") or "?"
            title = n.get("title") or n.get("name") or nid
            lines.append(f"  [done] {nid}: {title}")

    # In-progress
    if doing:
        lines.append("\nIn progress:")
        for n in doing:
            nid = n.get("id") or n.get("task") or "?"
            title = n.get("title") or n.get("name") or nid
            owner = n.get("owner") or "unassigned"
            lines.append(f"  [doing] {nid}: {title}  (owner: {owner})")

    # Blocked nodes
    if blocked:
        lines.append("\nBlocked:")
        for n in blocked:
            nid = n.get("id") or n.get("task") or "?"
            title = n.get("title") or n.get("name") or nid
            reason = n.get("blocked_reason") or n.get("reason") or "unspecified"
            lines.append(f"  [blocked] {nid}: {title}  reason: {reason}")

    # Open questions
    if questions:
        lines.append(f"\nOpen questions ({len(questions)}):")
        for q in questions:
            qid = q.get("id") or q.get("task") or "?"
            text = q.get("question") or q.get("text") or str(q)
            lines.append(f"  Q ({qid}): {text}")
    else:
        lines.append("\nNo open questions.")

    if not nodes:
        lines.append("\nNo nodes in this run — the DAG may not have started yet.")

    lines.append("\n(Source: dag_run dict — numbers narrated from the DAG run record, "
                 "never recomputed)")
    out = {"ok": True, "text": "\n".join(lines)}
    if notes:
        out["interpreted"] = notes
    return _dumps(out)


# How many per-entity clauses of a dof narration / schedule collapse are worth printing.
# Live 2026-07-30 (3-rectangle F profile, 12 lines, dof 40): the narration enumerated all
# 40 free slots as "line eN p1 x, p1 y, p2 x, p2 y" x12 (~840 chars), the schedule then
# printed 12 rows whose action and why were IDENTICAL apart from the entity id (~1150
# chars), and the readback's findings restated both again. Nothing in that repetition is
# a fact the first clause plus a count does not already carry, and an unbounded
# enumeration is what pushed one sketch readback to 7.6k chars.
_HEALTH_SAMPLE = 4


def _bounded_clauses(text: str, limit: int = _HEALTH_SAMPLE) -> str:
    """First ``limit`` semicolon-separated clauses of a narration + "+N more"."""
    clauses = [c.strip() for c in (text or "").split(";") if c.strip()]
    if len(clauses) <= limit:
        return "; ".join(clauses)
    return "; ".join(clauses[:limit]) + f"; … +{len(clauses) - limit} more"


def _schedule_lines(schedule: list) -> list[str]:
    """Schedule rows, collapsing runs that differ only by which entity they name.

    A 12-row schedule whose every row says "add driving dimension / geometric relation
    for eN — eN is under-constrained in p1 x, p1 y, p2 x, p2 y" carries exactly one
    instruction and a list of 12 entities. Emit it that way; keep distinct remedies
    verbatim and in order."""
    groups: list[tuple[str, str, str, list[str], dict]] = []
    for step in schedule:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind", "?"))
        action = str(step.get("action", "?"))
        why = str(step.get("why", ""))
        entity = str(step.get("entity", "") or "")
        # Signature of the remedy with the entity id factored out, so "…for e7 — e7 is
        # under-constrained in p1 x…" and the e8 row hash to the same instruction.
        if entity:
            sig = (kind, action.replace(entity, "$E"), why.replace(entity, "$E"))
        else:
            sig = (kind, action, why)
        if groups and groups[-1][:3] == sig:
            groups[-1][3].append(entity)
            groups[-1][5].append(step.get("order"))
        else:
            groups.append((sig[0], sig[1], sig[2], [entity], step,
                           [step.get("order")]))

    out: list[str] = []
    for kind, action_sig, why_sig, ents, first, orders in groups:
        order = first.get("order", "?")
        action = str(first.get("action", "?"))
        why = str(first.get("why", ""))
        handles = first.get("handles") or []
        named = [e for e in ents if e]
        if len(named) > 1:
            shown = ", ".join(named[:8])
            more = f" … +{len(named) - 8}" if len(named) > 8 else ""
            # Restate the remedy once with the entity slot generic, then name the set.
            # The order SPAN is kept so a model applying the schedule still knows the
            # collapsed row stands for steps 2..13, not for step 2 alone.
            nums = [o for o in orders if isinstance(o, int)]
            span = f"{min(nums)}-{max(nums)}" if len(nums) > 1 else str(order)
            generic_action = action_sig.replace("$E", "each entity")
            generic_why = why_sig.replace("$E", "each")
            out.append(f"  {span}. [{kind}] x{len(named)}: {generic_action}"
                       f" — {generic_why} — entities: {shown}{more}")
            continue
        if handles:
            h_str = ", ".join(str(h) for h in handles[:6])
            more_h = f" … +{len(handles) - 6}" if len(handles) > 6 else ""
            out.append(f"  {order}. [{kind}] {action} at {h_str}{more_h} — {why}")
        elif named:
            out.append(f"  {order}. [{kind}] {action} for {named[0]} — {why}")
        else:
            out.append(f"  {order}. [{kind}] {action} — {why}")
    return out


def sketch_health_text(d: dict) -> str:
    """Render a ``doc_sketch_health`` result dict as plain English text for no-vision models.

    Returns a multi-line string with four sections:
      1. Closure: "CLOSED (N material regions)" or "OPEN: N dangling endpoints at: [handles]"
      2. Remaining DOF in plain words (from dof_narration).
      3. Conflicts and redundant constraints (if any).
      4. Ordered relations-then-dimensions schedule.

    This is the feedback channel that lets a no-vision local model SEE its work
    and know what to do next.
    """
    lines: list[str] = []

    # ── 1. Closure ───────────────────────────────────────────────────────────────
    closure = d.get("closure") or {}
    open_eps = closure.get("open_endpoints") or []
    mat_regions = closure.get("material_regions")
    if not open_eps:
        region_txt = f" ({mat_regions} material region{'s' if mat_regions != 1 else ''})" \
            if mat_regions is not None else ""
        lines.append(f"Closure: CLOSED{region_txt}")
    else:
        handles_str = ", ".join(str(h) for h in open_eps[:8])
        more = f" … and {len(open_eps) - 8} more" if len(open_eps) > 8 else ""
        lines.append(f"Closure: OPEN — {len(open_eps)} dangling endpoint(s) at: "
                     f"{handles_str}{more}")
        lines.append("  → run sketch_auto_constrain to stitch near-touching endpoints")

    # ── 2. DOF / free variables ──────────────────────────────────────────────────
    dof = d.get("dof", 0)
    status = d.get("status", "unknown")
    narration = d.get("dof_narration") or ""
    if d.get("pinned_only"):
        lines.append(f"DOF: {dof} — profile pinned instead of constrained; replace fix pins "
                     "with relations/driving dimensions, then sketch_fully_define")
    elif dof <= 0 and status == "full":
        lines.append("DOF: 0 — fully constrained, not pinned_only")
    elif dof < 0 or status == "over":
        lines.append(f"DOF: {dof} — OVER-CONSTRAINED (conflicting constraints, see below)")
    else:
        lines.append(f"DOF: {dof} free — {_bounded_clauses(narration)}")

    # ── 3. Conflicts and redundants ──────────────────────────────────────────────
    conflicting = d.get("conflicting") or []
    redundant = d.get("redundant") or []
    if conflicting:
        lines.append(f"Conflicts ({len(conflicting)}):")
        for c in conflicting:
            lines.append(f"  CONFLICT {c.get('id','?')} ({c.get('type','?')})"
                         f" — {c.get('suggestion','remove it')}")
    if redundant:
        lines.append(f"Redundant ({len(redundant)}):")
        for r in redundant:
            lines.append(f"  REDUNDANT {r.get('id','?')} ({r.get('type','?')})"
                         f" — {r.get('suggestion','remove it')}")

    # ── 4. Schedule ──────────────────────────────────────────────────────────────
    schedule = d.get("schedule") or []
    if schedule:
        lines.append("Schedule (apply in order):")
        lines.extend(_schedule_lines(schedule))
    elif dof == 0 and status == "full" and not d.get("pinned_only"):
        lines.append("Schedule: (nothing to do — sketch is fully constrained)")
    for finding in d.get("findings") or []:
        lines.append(f"Intent: {finding.get('message', '')}")

    return "\n".join(lines)


# ------------------------------------------------------- accepted input dialect (describe_*)
# Recorded in the agent ledgers: describe_part(part=...)/`part_name=`/`attributes=` 6x and
# describe_project(run_id=)/`project=`/`project_name=`/`mission_request=`/`deliverable=` 11x,
# every one of them rejected before the function ran and every one a burnt turn. The model is
# naming the thing in the words of its own request, which is not a mistake worth a round trip —
# so these two tools OWN those spellings. Every rewrite is echoed in `interpreted`; nothing is
# guessed silently, and an argument this tool genuinely has no use for is named in the echo
# rather than dropped. A canonical call (only doc_id= / only run=) produces no echo at all.
#
# The var-keyword parameter is what makes this reachable: agent_turn._reconcile_kwargs inspects
# the target signature and rejects unknown kwargs BEFORE calling, so a fixed signature can
# never receive `part=`. It MUST be spelled `**kwargs: Annotated[Any, Field(default=None)]` —
# FastMCP's func_metadata turns a bare `**kwargs` into a REQUIRED pydantic field literally
# named "kwargs", which breaks every FastMCP call including the canonical one. FastMCP then
# passes {"kwargs": None}, so each body pops that literal key first.
_DOC_ALIASES = ("part", "part_name", "part_id", "name", "target", "doc", "docid",
                "document", "document_id", "model", "model_name")
_VIEW_ALIASES = ("attributes", "attribute", "parameters", "params", "properties",
                 "fields", "include", "sections", "section", "detail", "view")


def _unwrap_var_kw(kwargs: dict) -> None:
    """Normalise the var-keyword bag in place, whichever path filled it.

    The built-in agent calls the function directly, so `kwargs` already holds the extra
    arguments. FastMCP instead validates against a generated pydantic model: it hands the tool
    {"kwargs": None} for a canonical call, and DROPS any argument the model has no field for.
    So a FastMCP client reaches the dialect through the one field the schema does carry —
    kwargs={"part": "bracket"} — and that nested mapping is merged here. A model that JSON- or
    repr-encodes that nested value as a STRING (kwargs='{"part": "bracket"}') used to fail the
    bare isinstance check and vanish silently — coerce first so the intended aliases still land."""
    nested = coerce_json_or_repr(kwargs.pop("kwargs", None))
    if isinstance(nested, dict):
        kwargs.update(nested)


def _describe_part_args(doc_id: Optional[str], kwargs: dict) -> tuple:
    """(doc_id, interpreted notes) for describe_part's accepted input dialect.

    Consumes `kwargs` in place. A document alias routes to doc_id — which resolves a doc id OR
    an open part's NAME (cad_common._doc) — so `part="bracket"` needs no extra lookup."""
    notes: list = []
    for key in _DOC_ALIASES:
        v = kwargs.pop(key, None)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() and not doc_id:
            doc_id = v.strip()
            notes.append(f'read {key}={v.strip()!r} as the document to describe '
                         '(doc_id takes a doc id OR an open part name)')
        else:
            notes.append(f'ignored {key}={v!r}: doc_id={doc_id!r} already names the document')
    asked = [k for k in _VIEW_ALIASES if kwargs.pop(k, None) is not None]
    if asked:
        notes.append(
            f'{"/".join(asked)} is not a filter describe_part has — it always returns the whole '
            'outline, and everything is already in the response: digest.bbox_mm, digest.faces/'
            'edges, digest.feature_groups (one placement record per feature), digest.symmetry, '
            'digest.instances, digest.bores, digest.planar_caps. Read the one you wanted from '
            'there instead of asking again.')
    leftover = sorted(k for k, v in kwargs.items() if v is not None)
    if leftover:
        notes.append(
            f'ignored unrecognised argument(s) {", ".join(leftover)} — describe_part takes only '
            'doc_id (a doc id or an open part name). Nothing else changes what it returns.')
    return doc_id, notes


def _empty_part_text(doc_id: str) -> str:
    """The answer for describe_part on a document that has no solid body yet.

    doc_query raises "document has no bodies to query" there, which reads like a broken tool and
    got retried verbatim 5x in the ledgers. new_part() creates the DOCUMENT; the first
    add_features() creates the body. Name what was resolved, say it is empty, give the next
    call."""
    name, feats = doc_id, []
    try:
        tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
        name = tree.get("name") or doc_id
        feats = [f for f in (tree.get("features") or []) if isinstance(f, dict)]
    except Exception:  # noqa: BLE001
        pass
    lines = [f'Part "{name}" ({doc_id}) is EMPTY: 0 bodies; the document exists but holds no '
             'solid body yet, so there is nothing to describe. This is not a tool failure.']
    if feats:
        roster = ", ".join(f'{f.get("id")} [{f.get("type")}]' for f in feats[:8])
        lines.append(f'  It has {len(feats)} feature(s) ({roster}) but none of them produced a '
                     'body — a sketch or datum alone makes no solid; extrude/revolve it.')
    lines.append('  new_part() only creates the document. Add the first solid, then describe:')
    lines.append(f'    add_features(doc_id="{doc_id}", ops=[{{"op": "add_feature", "feature": '
                 '{"type": "box", "name": "Base", "params": {"length": 100, "width": 60, '
                 '"height": 20}}}])')
    lines.append(f'    describe_part(doc_id="{doc_id}")')
    return "\n".join(lines)


_RUN_ID_ALIASES = ("run_id", "runid", "run", "id")
_PROJECT_ALIASES = ("project", "project_name", "name", "mission", "mission_name", "title")
_BRIEF_ALIASES = ("mission_request", "mission_spec", "brief", "description", "request",
                  "deliverable", "deliverables", "components", "component", "constraints",
                  "constraint", "goal", "goals", "requirements", "scope", "notes")
_RUN_FIELD_ALIASES = ("nodes", "budget", "open_questions", "questions", "status")


def _runs_dir():
    """services/dag's persisted-run directory (mirrors executor.default_runs_dir)."""
    from pathlib import Path  # noqa: PLC0415
    import os  # noqa: PLC0415
    import platform  # noqa: PLC0415
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "runs"


def _load_dag_run(run_id: str) -> Optional[dict]:
    """The persisted aicad.dag/1 graph for `run_id`, shaped as the run dict this tool narrates.

    services/dag/executor.py writes <runs_dir>/<run_id>.graph.json beside the checkpoint DB.
    Per-node VERDICTS are not persisted (dag_status documents that limitation), so nodes come
    back with their graph identity and no status — narrated as pending, which is honest, rather
    than invented. None when no such run exists on disk."""
    if not run_id or not str(run_id).strip():
        return None
    rid = str(run_id).strip()
    path = _runs_dir() / f"{rid}.graph.json"
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unknown / unreadable run is not an error here
        return None
    nodes = [{"id": n.get("id"), "title": n.get("name") or n.get("title") or n.get("id"),
              "owner": n.get("type"), "status": n.get("status") or "pending"}
             for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    return {"id": graph.get("id") or rid, "name": graph.get("name") or rid, "nodes": nodes}


def _describe_project_args(run, kwargs: dict) -> tuple:
    """(run dict | None, interpreted notes) for describe_project's accepted input dialect.

    Consumes `kwargs` in place. Three shapes beyond the canonical run dict:
      * a run ID (run="<id>", run_id=, id=) — loaded from the persisted DAG run when it exists,
        and otherwise narrated as the bare id it is, with how to start one;
      * a project NAME (project=, project_name=, mission=);
      * a free-form description in semantic fields (mission_request=, deliverables=,
        constraints=, ...) — collected under run["brief"] and narrated straight back, because
        this tool narrates a run, it does not plan one.

    `run` (and the run_id/run/id aliases below) is coerced through coerce_json_or_repr first:
    a model that JSON/repr-encodes the run dict as a STRING (run='{"id": "r7", "nodes": []}')
    used to fall through to the run_id branch and get treated as a literal id — a SILENT
    WRONG READ (the real run dict was discarded and the tool answered "no such run" instead
    of narrating the run it was actually given)."""
    notes: list = []
    run = coerce_json_or_repr(run)
    if isinstance(run, dict):
        for k in list(kwargs):
            if kwargs[k] is None:
                kwargs.pop(k)
        if kwargs:
            notes.append(f'narrated the run dict; ignored {", ".join(sorted(kwargs))} '
                         '(describe_project reads the run dict only)')
        return run, notes

    run_id = run.strip() if isinstance(run, str) and run.strip() else None
    if run_id:
        notes.append(f'read run={run_id!r} as a DAG run id (run also takes the full run dict)')
    for key in _RUN_ID_ALIASES:
        v = coerce_json_or_repr(kwargs.pop(key, None))
        if isinstance(v, dict) and not run_id:
            return v, notes + [f'read {key}= as the run dict']
        if isinstance(v, str) and v.strip() and not run_id:
            run_id = v.strip()
            notes.append(f'read {key}={run_id!r} as the DAG run id')

    name = None
    for key in _PROJECT_ALIASES:
        v = kwargs.pop(key, None)
        if isinstance(v, str) and v.strip() and not name:
            name = v.strip()
            notes.append(f'read {key}={name!r} as the project name')

    fields: dict = {}
    for key in _RUN_FIELD_ALIASES:
        v = kwargs.pop(key, None)
        if v is not None:
            fields["open_questions" if key == "questions" else key] = v
            notes.append(f'took {key}= straight into the run record')

    brief: dict = {}
    for key in _BRIEF_ALIASES:
        v = kwargs.pop(key, None)
        if v is not None:
            brief[key] = v
    extra = {k: v for k, v in kwargs.items() if v is not None}
    if extra:
        brief.update(extra)
        notes.append(f'kept unrecognised argument(s) {", ".join(sorted(extra))} as part of the '
                     'supplied description — describe_project narrates a DAG run, so they are '
                     'echoed back rather than acted on')

    loaded = _load_dag_run(run_id) if run_id else None
    if loaded is not None:
        notes.append(f'loaded DAG run {loaded["id"]!r} from {_runs_dir()} '
                     f'({len(loaded["nodes"])} node(s)); per-node status is not persisted, so '
                     'nodes read as pending')
        out = loaded
    elif run_id or name or brief or fields:
        out = {"id": run_id or "(no run id)", "name": name or run_id or "(unnamed project)",
               "nodes": []}
        if run_id:
            notes.append(f'no DAG run {run_id!r} under {_runs_dir()} — narrating the id you '
                         'gave and nothing more. Start a run with dag_plan(brief=...) then '
                         'dag_run(graph_json=...), or pass run=dag_status()["run"]')
        else:
            notes.append('no run id or run dict was given, so there is no DAG run to read — '
                         'narrating what you described. dag_plan(brief=...) turns a description '
                         'like this into a real run')
    else:
        return None, notes

    out.update(fields)
    if brief:
        out["brief"] = brief
    return out, notes


def register_gsl_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def describe_part(doc_id: Optional[str] = None,
                      **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Compact, NAMED outline of the part — use INSTEAD of dumping every face on a complex part.

        Returns the bounding box, the feature groups WITH a placement narrative (the side of the
        part each feature sits on, the EARLIER, larger feature it sits on — a boss is on its
        plate, never the plate on its boss — and its offset from the part bbox on the two
        perpendicular axes), any detected symmetry/instanced patterns (e.g. "6-fold rotational
        about +Z" with the blade faces collapsed to one line), the bores/holes and flat faces by
        side, and a stable `sid` for every addressable face/edge. A feature that owns no surviving
        faces (fully consumed by a later boolean) is called out explicitly. Face counts are the
        faces a feature can be shown to have CREATED: the kernel credits every face a boolean
        regenerated to the feature that ran it — both the faces a CUT merely pierced and the ones
        a FUSE merely landed on — so a face outside a feature's own tool is re-credited to the
        earlier feature that authored the surface, and the raw kernel count stays visible as
        `attributed_faces`.
        Token cost scales with the number of DISTINCT feature roles, not the raw face count — a
        68-face impeller describes in a few hundred tokens. Drill into one entity with hover(sid).

        doc_id: a doc id ("doc-cpp0000001c") or an open part's NAME ("bracket"). Omit it for the
        current part. ALSO ACCEPTED for the same thing: part=, part_name=, part_id=, name=,
        target=, document=, document_id=, model=. attributes=/parameters=/fields=/detail= are
        accepted and ignored — describe_part has no filtered view, and the whole outline is
        already the answer. Anything rewritten or ignored is listed in `interpreted` on the
        response; a plain describe_part(doc_id=...) carries no such key.
        A document with no solid body yet answers ok with `empty: true` and the add_features call
        to make one — that is an empty part, not a tool failure.
        """
        _unwrap_var_kw(kwargs)
        try:
            doc_id, notes = _describe_part_args(doc_id, kwargs)
            did = _doc(doc_id)
            try:
                d = _digest(did)
            except Exception as e:  # noqa: BLE001
                if "no bodies" not in str(e):
                    raise
                out = {"ok": True, "empty": True, "doc_id": did,
                       "digest": {"bodies": 0, "faces": 0, "edges": 0, "bbox_mm": None},
                       "outline": _empty_part_text(did)}
                if notes:
                    out["interpreted"] = notes
                return _dumps(out)
            out = {"ok": True, "outline": _outline_text(d), "digest": d}
            if notes:
                out["interpreted"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def query_adjacency(doc_id: Optional[str] = None) -> str:
        """The face-adjacency graph (W3): which faces touch which, and whether each shared edge is
        a CONVEX outside-corner or a CONCAVE inside-corner/pocket edge.

        Returns {faces:[{ord, surf, side, neighbors:[ord,...]}], edges:[{between:[fa,fb], convex,
        length}]}. This is what lets you reason about "the edge between the top and front face" or
        "the faces this pocket cut into" — relationships the flat per-face read-back never carried.
        """
        try:
            return _dumps(cb.call("doc_adjacency", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def hover(sid: str, doc_id: Optional[str] = None) -> str:
        """Expand ONE face/edge by the stable `sid` describe_part / query_geometry handed you —
        its full semantic card (surf, side, signed normal, concavity, radius, centroid, area).

        Progressive disclosure: pull the outline once, then hover only the 1-2 entities an edit
        touches, instead of re-reading the whole part. Returns the match, or a not-found note.
        """
        try:
            d = _doc(doc_id)
            for of in ("face", "edge"):
                res = cb.call("doc_query", {"doc_id": d, "query": {"of": of}})
                for m in res.get("matches", []):
                    if m.get("sid") == sid:
                        return _dumps({"ok": True, "match": m})
            return _dumps({"ok": False, "error": f"no face/edge with sid {sid!r} "
                           "(it may have changed — call describe_part for current sids)"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def describe_sketch(sketch: str, doc_id: Optional[str] = None) -> str:
        """Compose a sketch's PLANE, ENTITY roster, CONSTRAINT roster, closed REGIONS and DOF
        verdict into text — the non-vision equivalent of looking at the sketch overlay.

        sketch: the sketch feature id (e.g. "s1"). Returns {ok, sketch, text, digest} where text
        has 5 sections: (1) the plane's 3D origin/axes (and which face/datum it's on, if named),
        (2) one line per entity — id, type, solved coords/dims, [full]/[under]/[over] status,
        construction flag, (3) constraints grouped by kind with counts, plus driving dimensions
        listed individually with their authored value, (4) region count with area + bbox per
        closed loop, (5) a DOF verdict phrased like check_sketch's (fully/under/over-defined).

        Use this instead of render_part when you cannot see images — pair with check_sketch for
        the same DOF numbers in a smaller payload once you already know the sketch's shape.
        """
        try:
            d = _doc(doc_id)
            # Models pass the sketch's display NAME as often as its f<N> id, and an id from
            # a batch that was rolled back is stale by definition. Resolve names, and when
            # nothing matches say WHICH sketches exist instead of "call get_part" (live
            # 2026-07-28: describe_sketch("f4") answered with nothing actionable and the
            # model had to spend a turn guessing).
            from services.mcp.cad_eval_tools import _sketch_features, _sketch_roster
            rows = _sketch_features(d)
            ids = {str(f.get("id")) for f in rows}
            if sketch not in ids:
                by_name = {str(f.get("name")): str(f.get("id")) for f in rows if f.get("name")}
                if sketch in by_name:
                    sketch = by_name[sketch]
                else:
                    return _dumps({"ok": False,
                                   "error": f"no sketch {sketch!r} in {d}. "
                                            + _sketch_roster(d),
                                   "sketches": sorted(ids)})
            try:
                overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
            except Exception as e:  # noqa: BLE001
                detail = getattr(e, "detail", None)
                if isinstance(detail, dict) and detail.get("reason") == "unknown sketch":
                    return _dumps({"ok": False,
                                   "error": f"no sketch {sketch!r} in {d}. "
                                            + _sketch_roster(d)})
                raise
            try:
                regions_resp = cb.call("doc_sketch_regions", {"doc_id": d, "sketch": sketch})
            except Exception:  # noqa: BLE001
                regions_resp = {}
            feat = None
            try:
                tree = cb.call("doc_tree", {"doc_id": d})
                for f in ((tree.get("tree") or {}).get("features")) or []:
                    if isinstance(f, dict) and f.get("id") == sketch:
                        feat = f
                        break
            except Exception:  # noqa: BLE001
                feat = None
            digest = _sketch_digest(sketch, overview, regions_resp, feat)
            return _dumps({"ok": True, "sketch": sketch, "text": _sketch_text(digest),
                           "digest": digest})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch_health(sketch: str, text: bool = False,
                      doc_id: Optional[str] = None) -> str:
        """The single narrated read of a sketch's constraint state — solve it and get back WHY it
        is under/over-defined plus an ordered plan to reach fully-constrained.

        sketch: the sketch feature id (e.g. "s1"). Returns {ok, sketch, status, dof, converged, ...}
        where the narrated fields are:
          closure       {open_endpoints, closed_chains_ok, material_regions?} — open_endpoints names
                        the dangling endpoint handles where chains fail to close (feed these to
                        sketch_auto_constrain to stitch them coincident).
          dof_narration plain words for the remaining free variables, e.g. "circle C1 radius;
                        line L4 end x/y" — read this instead of counting dof yourself.
          free_variables the same, structured: [{entity, param, var}].
          statuses      per-entity id -> "under" | "full" | "over".
          redundant     [{id, type, suggestion}] — constraints implied by the others, with a removal
                        suggestion (a redundant DRIVING dimension over-defines — drop it).
          conflicting   [{id, type, suggestion}] — constraints that contradict each other; the
                        suggestion says which to remove.
          schedule      ordered [{order, kind, action, why, ...}] to reach dof 0 — geometric
                        RELATIONS first, then DRIVING dimensions, mirroring how a person constrains.
          advisory      [aicad.advisory/1 items] — emittable straight to the advisor layer.

        text: when True, also includes a "health_text" key with a plain-English multi-line
          summary (closure status, DOF in words, conflicts/redundants, ordered schedule) —
          the feedback channel for no-vision local models. See sketch_health_text().

        Sketch loop: draw roughly -> relations -> driving dimensions -> sketch_fully_define
        -> check_sketch: dof 0 and not pinned_only. Fix-pinned profile entities are findings,
        not design intent; fixed external construction references are legitimate.
        """
        try:
            d = _doc(doc_id)
            result = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            from services.mcp.ascii2d import live_sketch_design_intent
            result.update(live_sketch_design_intent(d, sketch, result))
            result["fully_defined"] = (
                result.get("dof") == 0 and result.get("status") != "over"
                and not result.get("conflicting") and not result.get("pinned_only")
                and not result.get("skipped_constraints"))
            if text and result.get("ok"):
                result["health_text"] = sketch_health_text(result)
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch_fully_define(sketch: str, relations: bool = True, dimensions: bool = True,
                             anchor: str = "origin", entities: Optional[list[str]] = None,
                             doc_id: Optional[str] = None) -> str:
        """Finish a 2D sketch with editable geometric relations and driving dimensions, never fix pins.

        Author design intent first (coincident/horizontal/vertical/tangent/equal/symmetric,
        then critical dimensions); this engine op defines remaining freedom at the current
        shape. relations/dimensions enable each pass; anchor="origin" adds positional intent,
        anchor="none" leaves free placement. entities optionally limits the entity ids.
        Returns exact relations_added/dimensions_added and solver health (dof/pinned_only).
        A partial result is honest: inspect findings and remaining DOF instead of assuming
        every curve can be fully defined. add_features already runs this after new sketches
        and sketch_add_entity batches unless auto_define:false.
        """
        try:
            d = _doc(doc_id)
            op = {"op": "sketch_fully_define", "sketch": sketch, "relations": relations,
                  "dimensions": dimensions, "anchor": anchor}
            if entities is not None:
                op["entities"] = entities
            result = cb.call("doc_apply_ops", {"doc_id": d, "ops": [op]})
            health = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            from services.mcp.ascii2d import live_sketch_design_intent, sketch_define_additions
            health.update(live_sketch_design_intent(d, sketch, health))
            tree = result.get("tree") or {}
            feature = next((row for row in tree.get("features") or []
                            if row.get("id") == sketch), {})
            constraints = (feature.get("params") or {}).get("constraints") or []
            failed = [row for row in (result.get("rebuild") or {}).get("feature_status") or []
                      if not row.get("ok")]
            return _dumps({"ok": not failed, "doc_id": d, "sketch": sketch,
                           "revision": result.get("revision"), "health": health,
                           "dof": health.get("dof"), "pinned_only": health["pinned_only"],
                           **sketch_define_additions(result.get("id_map") or {}, 0, constraints),
                           **({"failed_features": failed} if failed else {})})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch_auto_constrain(sketch: str, apply: bool = True, tol_mm: float = 1e-4,
                              doc_id: Optional[str] = None) -> str:
        """Stitch a sketch's near-touching endpoints closed by adding coincident constraints — the
        cheap way to close chains before dimensioning.

        Finds entity endpoints that lie within tol_mm of each other (they touch visually but are not
        yet coincident) and adds a coincident constraint for each pair. With apply=True (default) it
        mutates the sketch; with apply=False it only returns the proposals (no mutation) so you can
        preview them. Every addition is rollback-safe: a proposal that would make the sketch
        conflicting is skipped (counted in skipped_conflicting), never applied.

        sketch:   the sketch feature id (e.g. "s1").
        apply:    True to add the coincident constraints, False to only propose them.
        tol_mm:   endpoint gap tolerance in mm (default 1e-4).

        Returns {proposals:[{a:{handle}, b:{handle}, gap_mm}], applied:[cids], skipped_conflicting,
        regions_before, regions_after, converged} — regions_before/regions_after report which closed
        chains (material regions) the pass created.

        This same pass also runs automatically after sketch edits when the doc meta
        `sketch.auto_coincident` is true (default ON), so you usually only call it explicitly to
        preview (apply=False) or to re-stitch after loosening the tolerance.
        """
        try:
            d = _doc(doc_id)
            result = cb.call("sketch_auto_constrain",
                             {"doc_id": d, "sketch": sketch,
                              "opts": {"tol_mm": tol_mm, "apply": apply}})
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def describe_scene(views: Optional[Union[list, str]] = None,
                       doc_id: Optional[str] = None) -> str:
        """Deterministic "what the render would show" per view, from B-rep face normals — the
        non-vision fallback for render_part / render_views / contact_sheet.

        views: named views to describe, default ["front", "right", "top", "iso"] (same names
        render_part / render_views accept; pass the full 6-ortho+iso set to mirror
        render_views()). For each view: the camera direction, then every face whose signed
        normal points toward the camera (dot > 0.15) as "sid surf side r<radius>/area" (capped at
        20/view). Also includes the part-level narrative once at the top (bbox + the A3 feature
        placement lines from describe_part). OCCLUSION IS NOT MODELED — a face behind another
        still lists if it faces the camera; this is stated explicitly in the returned text.

        Returns {ok, text, views:{<name>: {dir, visible:[sids]}}}.
        """
        try:
            d = _doc(doc_id)
            # A stringified views list ('["front","top"]' or a bare CSV word) used to be
            # handed straight to `for name in view_names`, iterating it CHARACTER BY
            # CHARACTER into garbage view names that all silently fell back to the iso
            # direction (ok:true, wrong views) — the SILENT WRONG BEHAVIOR class.
            if views is not None:
                views = require_list(views, "views",
                                      'a list of view names, e.g. ["front","top","iso"]')
            view_names = views or ["front", "right", "top", "iso"]
            faces = cb.call("doc_query", {"doc_id": d, "query": {"of": "face"}}).get(
                "matches", [])
            digest = _digest(d)
            bbs = _bbox_str(digest.get("bbox_mm"))

            lines = [f'Part "{digest.get("part") or "part"}" — bbox {bbs}, '
                    f'{digest["faces"]} face(s), {len(digest.get("feature_groups") or [])} '
                    'feature(s)']
            if digest.get("feature_groups"):
                lines.append("Feature placement:")
                for fg in digest["feature_groups"]:
                    lines.append(f'  - {_feature_group_line(fg)}')

            views_out = {}
            for name in view_names:
                direction = _view_dir(name)
                vis = _visible_faces(faces, direction)
                lines.append(f'{name} view — part {bbs} '
                             f'(camera dir [{direction[0]:.2f}, {direction[1]:.2f}, '
                             f'{direction[2]:.2f}]):')
                for f in vis[:20]:
                    dims = f'r{f["radius"]}' if f.get("radius") else f'{f.get("area_or_len")}mm\u00b2'
                    lines.append(f'  {f.get("sid")} {f.get("surf")} {f.get("side") or "-"} {dims}')
                if len(vis) > 20:
                    lines.append(f'  +{len(vis) - 20} more')
                views_out[name] = {"dir": list(direction), "visible": [f.get("sid") for f in vis]}

            lines.append("Computed from B-rep normals; occlusion not modeled.")
            return _dumps({"ok": True, "text": "\n".join(lines), "views": views_out})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- narration (geometry)

    @mcp.tool()
    def describe_toolpaths(job_id: Optional[str] = None, audit: bool = True) -> str:
        """Compact narrative of a CAM job's toolpaths and simulation results — use INSTEAD of
        dumping the raw cam_job_tree JSON. Returns per-op strategy/tool/stepdown/engagement,
        cycle-time and gouge/rapid checks from cam_simulate, and a G-code digest (bounds, feed
        range, tool-change count) from cam_post. When audit=True (default), also calls cam_audit
        and appends the audit summary. Narrates existing numbers only — never recomputes.
        Call cam_generate + cam_simulate first for full results."""
        try:
            return _describe_toolpaths_text(job_id, audit=audit)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def describe_results(doc_id: Optional[str] = None, study_name: Optional[str] = None) -> str:
        """Compact narrative of FEA/CFD simulation results — use INSTEAD of dumping raw study
        JSON. Returns max von Mises stress, displacement, factor of safety, margin vs material
        yield, the fixed/loaded faces named by sid (join of the stored fixture/load queries
        against describe_part's face layer), modal frequencies in words, and mesh convergence
        caveats. Narrates the doc's persisted studies (run_fea_static / run_fea_modal /
        run_cfd_flow records) only — never recomputes. Returns a graceful 'no results
        attached' message when no study exists."""
        try:
            return _describe_results_text(_doc(doc_id), study_name)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- narration (assembly/board/project)

    @mcp.tool()
    def describe_assembly(doc_id: Optional[str] = None) -> str:
        """Compact narrative of an assembly (or multi-body part) — world placement, fit/clearance,
        and mate solve summary. Use INSTEAD of dumping asm_world_bodies JSON.

        Read-only on a solved assembly: queries world-frame bounding boxes/origins, reads
        the mate-solve verdict (status, dof) without editing, dirtying or re-revisioning
        the document, then evaluates collision-only fit with clearance_mode='threshold',
        min_clearance_mm=0 and include_coincident=False. Overlap volumes remain exact;
        clearance lower bounds are not measured gaps and no tightest measured gap is
        reported. Returns raw fit completeness, pair coverage and failures; no-pair or
        incomplete results cannot qualify collision-free. Works on assembly docs and
        multi-body parts. To re-seat components after edits, use solve_mates.

        On an assembly, `components` lists each component with its stored `source` and
        `source_resolved`, the absolute path of the file the engine opens. A relative stored
        path is relative to `component_source_base`. To insert the same file into another
        assembly, use `source_resolved` as `source.path`.
        """
        try:
            return _describe_assembly_text(_doc(doc_id))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def describe_board(doc_id: str = "") -> str:
        """Compact narrative of an ECAD schematic or PCB — component roster, net list, ERC/DRC
        summary, and unconnected pins. Promotes ecad_status into the GSL narration layer.

        Returns the component count and list (ref, value, footprint), net count and per-net
        members (capped at 20 nets), ERC error/warning counts for schematics or DRC status for
        PCBs, and a list of unconnected pins. Narrates existing numbers only — never recomputes.
        Returns a graceful message when no ECAD document is active. Open a schematic first with
        ecad_new_schematic or ecad_new_pcb.
        """
        try:
            return _describe_board_text(doc_id)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def describe_project(run: Optional[Union[dict, str]] = None,
                         **kwargs: Annotated[Any, Field(default=None)]) -> str:
        """Compact narrative of a DAG project run — nodes done/blocked, budgets, open questions.
        Uses the live DAG run's status, budget and open-question data.

        run: a dag_run dict matching contracts/dag_schema.md — pass dag_status()['run'] or your
        executor's run record. When omitted, returns a 'no run supplied' explanation.

        ALSO ACCEPTED, because a run dict is rarely what a caller has to hand:
          * a run ID string — run="<id>", run_id="<id>", id="<id>". The persisted DAG run is
            loaded from disk when it exists (per-node status is not persisted, so its nodes read
            as pending); an unknown id is narrated as the bare id, with how to start a real run.
          * a project name — project=, project_name=, mission=, title=.
          * a free-form description — mission_request=, mission_spec=, brief=, deliverables=,
            components=, constraints=, requirements=, scope=. These are narrated straight back
            under "Supplied project description": describe_project reports on a run, it does not
            plan one — dag_plan(brief=...) is what turns a description into a run.
          * run-record fields given loose — nodes=, budget=, open_questions=, status=.
        Every rewrite is listed in `interpreted` on the response; a plain describe_project() or
        describe_project(run={...}) carries no such key.

        Returns node status counts (done/doing/blocked/pending), per-node titles and owners,
        budget usage (tokens spent vs cap), and open questions by task id. Narrates the dag_run
        dict only — never queries the engine. See contracts/dag_schema.md for the run shape.
        """
        _unwrap_var_kw(kwargs)
        try:
            run, notes = _describe_project_args(run, kwargs)
            return _describe_project_text(run, notes)
        except Exception as e:  # noqa: BLE001
            return _err(e)
