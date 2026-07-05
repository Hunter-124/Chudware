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

import math
from collections import defaultdict
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server import core_bridge as cb
from mcp_server.cad_tools import _doc, _dumps, _err
from mcp_server.fit_narration import _format_fit  # single home (T-F8); loop.py shares it


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
# A feature type that never owns solid faces (references/sketches) — never gets a placement
# line or a "consumed" line, since it was never expected to own B-rep faces to begin with.
_NON_BODY_FEATURE_TYPES = {"sketch", "plane", "axis", "point", "csys"}

_SIDE_AXIS = {"top": "z", "bottom": "z", "right": "x", "left": "x", "front": "y", "back": "y"}
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _feature_placement(owned: list, bbox: dict) -> Optional[dict]:
    """Where a feature's SURVIVING faces sit: mean centroid, the dominant principal side among
    them (top/bottom/right/left/front/back), and offsets from the part bbox on the two axes
    PERPENDICULAR to that side (e.g. a boss on the top face is placed by its X/Y offset from the
    bbox corner). Falls back to axis "z" (offsets on X/Y) when no owned face has a clear
    principal-axis side — e.g. a through-hole's cylindrical bore wall, whose normal is radial."""
    if not owned:
        return None
    n = len(owned)
    cx = sum((f.get("centroid") or [0, 0, 0])[0] for f in owned) / n
    cy = sum((f.get("centroid") or [0, 0, 0])[1] for f in owned) / n
    cz = sum((f.get("centroid") or [0, 0, 0])[2] for f in owned) / n
    centroid = [cx, cy, cz]
    sides = [f.get("side") for f in owned if f.get("side")]
    dominant = max(set(sides), key=sides.count) if sides else None
    axis = _SIDE_AXIS.get(dominant, "z")
    perp = [a for a in ("x", "y", "z") if a != axis]
    bmin, bmax = bbox.get("min"), bbox.get("max")
    offsets = None
    if (isinstance(bmin, list) and isinstance(bmax, list) and
            len(bmin) == 3 and len(bmax) == 3):
        offsets = {}
        for a in perp:
            i = _AXIS_INDEX[a]
            offsets[a] = {"from_min_mm": round(centroid[i] - bmin[i], 2),
                          "from_max_mm": round(bmax[i] - centroid[i], 2)}
    return {"centroid": [round(c, 2) for c in centroid], "side": dominant,
           "offsets": offsets, "n_faces": n}


def _feature_group_line(fg: dict) -> str:
    """One-line placement sentence for a single feature_groups entry (token discipline: one
    line per feature, no matter how many faces it owns)."""
    if fg.get("consumed"):
        return f'feature {fg["id"]} owns no surviving faces (consumed by later ops)'
    label = f'{fg["id"]} {fg.get("name") or ""} [{fg.get("type")}]'.replace("  ", " ").strip()
    pl = fg.get("placement")
    if not pl:
        return f'{label}: {fg["faces"]} face(s)'
    touches = pl.get("touches") or []
    touch_txt = f' of {", ".join(touches)}' if touches else ""
    side = pl.get("side")
    if side:
        where = f'on {side} face{touch_txt}'
    else:
        where = f'no clear reference face{touch_txt} (curved/through feature)'
    off = pl.get("offsets")
    if off:
        off_txt = " / ".join(
            f'{off[a]["from_min_mm"]:.0f} mm from -{a.upper()} edge' for a in sorted(off))
        return f'{label} ({fg["faces"]} face(s)): {where}, centered {off_txt}'
    return f'{label} ({fg["faces"]} face(s)): {where}'


def _digest(doc_id: str) -> dict:
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
    owner_map = tree.get("owner_map") or {}  # kept for callers that want the raw map

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

    # A3 — group owned faces by feature. doc_query's per-face `owner_feature` field is ALWAYS
    # null (core/doc/picks.cpp resolve_geom_query hardcodes ti.owner = nullopt — verified at
    # runtime), so ownership must be joined from doc_tree's owner_map instead: it keys each
    # feature's owned faces as [body_id, topo_id] pairs (1-based TopExp topo index). doc_query
    # enumerates the SAME body's faces in the SAME order (both walk face topology in first-
    # encounter order for a single-solid body — verified: owner_map's (body,topo) pairs line up
    # 1:1 with doc_query's 0-based match order via topo_id == index+1), and doc_query always
    # queries the LAST body, so we join against the highest body_id owner_map mentions.
    last_body = 0
    for om in owner_map.values():
        for pair in om.get("faces", []) or []:
            if isinstance(pair, list) and len(pair) == 2:
                last_body = max(last_body, pair[0])
    owner_by_topo: dict = {}
    for fid, om in owner_map.items():
        for pair in om.get("faces", []) or []:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == last_body:
                owner_by_topo[pair[1]] = fid
    faces_by_owner: dict = defaultdict(list)
    ord_owner = [owner_by_topo.get(i + 1) for i in range(len(faces))]
    for i, f in enumerate(faces):
        fo = ord_owner[i]
        if fo:
            faces_by_owner[fo].append(f)

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

    feat_summary = []
    for ft in feats:
        if not isinstance(ft, dict):
            continue
        fid = ft.get("id")
        if not fid or ft.get("type") in _NON_BODY_FEATURE_TYPES:
            continue
        owned = faces_by_owner.get(fid, [])
        entry = {"id": fid, "name": ft.get("name"), "type": ft.get("type"), "faces": len(owned)}
        if owned:
            placement = _feature_placement(owned, bbox)
            if placement is not None:
                placement["touches"] = sorted(touches.get(fid, []))
                entry["placement"] = placement
        else:
            entry["consumed"] = True
        feat_summary.append(entry)

    return {
        "part": (tree.get("tree") or {}).get("name"),
        "bbox_mm": bbox.get("size"),
        "faces": len(faces), "edges": len(edges),
        "symmetry": part_symmetry,
        "feature_groups": feat_summary,
        "instances": instanced,
        "bores": bores,
        "planar_caps": caps,
    }


def _outline_text(d: dict) -> str:
    lines = []
    bb = d.get("bbox_mm")
    bbs = (f"{bb[0]:.0f}x{bb[1]:.0f}x{bb[2]:.0f} mm" if isinstance(bb, list) and len(bb) == 3
           else "?")
    lines.append(f'Part "{d.get("part") or "part"}"  bbox {bbs}  | {d["faces"]} faces, '
                 f'{d["edges"]} edges')
    if d.get("symmetry"):
        s = d["symmetry"]
        lines.append(f'Symmetry: {s["order"]}-fold {s["type"]} about +{s["axis"].upper()}')
    if d.get("feature_groups"):
        lines.append("Features (placement narrative, one line each):")
        for fg in d["feature_groups"]:
            lines.append(f'  - {_feature_group_line(fg)}')
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
_DIM_CONSTRAINT_TYPES = {"distance", "angle", "radius", "diameter"}


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
    if status == "over" or over_ids:
        verdict = "Over-defined" + (f': conflicting entities {over_ids} — remove one constraint.'
                                    if over_ids else ' — remove a conflicting constraint.')
        fully_defined = False
    elif status == "full" or dof == 0:
        verdict = "Fully defined (dof 0) — every entity is pinned by relations + dimensions."
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
           "fully_defined": fully_defined, "skipped_constraints": skipped, "verdict": verdict}


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


# ================================================================ narration helpers (T-F7)


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


def _describe_toolpaths_text(job_id: Optional[str] = None) -> str:
    """Narrate a CAM job's toolpaths — data from cam_tree, cam_simulate, cam_post only."""
    from mcp_server.cam_tools import _CAM  # shared job state  # noqa: PLC0415

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
                                      "options": {"program_name": "AICAD",
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

    if not setups:
        lines.append('No setups configured — call cam_add_setup to add one.')

    lines.append('\n(Source: cam_job_tree, cam_simulate, cam_post — numbers narrated, never recomputed)')
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

    # T-W9: additive sim-advisor verdict (margin policy + beam anchor + suggestions).
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


# ================================================================ narration helpers (T-F8)


def _describe_assembly_text(doc_id: str) -> str:
    """Narrate an assembly's world placement, fit/clearance, and mate summary.

    Data sources (in call order):
    1. asm_world_bodies  — body names, placements, bounding boxes
    2. eval_interference — pairwise clash (volume) and gap (distance) data
    3. doc_solve_mates   — top-level DOF/converged summary string (best-effort)

    Numbers narrated verbatim from the tool payloads — never recomputed.
    """
    # 1. World-placed bodies
    try:
        wb_r = cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False})
    except Exception as e:  # noqa: BLE001
        return _dumps({"ok": False, "error": f"asm_world_bodies failed: {e}"})

    bodies = wb_r.get("bodies", [])
    is_multi = wb_r.get("multibody_part", False)
    n = len(bodies)

    if n == 0:
        return _dumps({"ok": True, "text":
                       "No bodies found in this document — "
                       "add components (assembly) or features (part) first."})

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
        lines.append(f"  {name}: {size_txt}, {origin_txt}  (source: asm_world_bodies)")

    # 3. Solve/DOF summary (best-effort — doc_solve_mates returns a status string)
    try:
        sm_r = cb.call("doc_solve_mates", {"doc_id": doc_id})
        status = (sm_r.get("rebuild") or {}).get("doc_status") or sm_r.get("status") or ""
        if status:
            lines.append(f"\nMate solve: {status}  (source: doc_solve_mates)")
        else:
            lines.append("\nMate solve: solve completed (DOF detail not available via read RPC — "
                         "call solve_mates for the live result)")
    except Exception:  # noqa: BLE001
        lines.append("\nMate solve: not available for this document type")

    # 4. Interference / clearance (best-effort)
    try:
        if_r = cb.call("eval_interference", {"doc_id": doc_id,
                                             "include_coincident": False,
                                             "min_clearance_mm": 0.0})
        fit_text = _format_fit(if_r)
        if fit_text:
            lines.append("")
            lines.extend(fit_text.splitlines())
        elif not if_r.get("interferences"):
            n_pairs = len(if_r.get("clearances", []))
            lines.append(f"\nFit: no interference detected ({n_pairs} clearance pair(s))  "
                         "(source: eval_interference)")
    except Exception:  # noqa: BLE001
        lines.append("\nFit/clearance: not available for this document")

    lines.append("\n(Source: asm_world_bodies, eval_interference, doc_solve_mates — "
                 "numbers narrated, never recomputed)")
    return _dumps({"ok": True, "text": "\n".join(lines)})


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
    try:
        from engine.ecad import session as ecad_session  # noqa: PLC0415
    except ImportError:
        return _dumps({"ok": False,
                       "error": "ECAD engine not available — "
                                "open a schematic or PCB first (ecad_new_schematic / ecad_new_pcb)"})

    try:
        from engine.ecad import netlist as netlist_mod  # noqa: PLC0415
    except ImportError:
        netlist_mod = None  # type: ignore[assignment]

    did = doc_id or ""
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
            from engine.ecad import drc as drc_mod  # noqa: PLC0415
            res = drc_mod.run_drc(doc)
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
    if netlist_mod is None:
        return _dumps({"ok": False,
                       "error": "engine.ecad.netlist not available — check your install."})

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
        from engine.ecad import erc as erc_mod  # noqa: PLC0415
        violations = erc_mod.run_erc(doc, nl)
        n_erc_err = sum(1 for v in violations if v.get("severity") == "error")
        n_erc_warn = sum(1 for v in violations if v.get("severity") == "warning")
        if n_erc_err:
            lines.append(f"\nERC: {n_erc_err} error(s), {n_erc_warn} warning(s)  "
                         "(source: engine.ecad.erc)")
        else:
            lines.append(f"\nERC: clean — {n_erc_warn} warning(s)  (source: engine.ecad.erc)")
    except Exception:  # noqa: BLE001
        lines.append("\nERC: not available")

    # Unconnected pins (pins with no net connection)
    try:
        all_pins = {(c.get("ref"), str(p))
                    for c in components
                    for p in (c.get("pins") or [])}
        wired = {(m.get("ref"), str(m.get("pin")))
                 for _, members in nets_dict.items()
                 for m in members}
        unconnected = sorted(f"{r}.{p}" for r, p in all_pins - wired)
    except Exception:  # noqa: BLE001
        unconnected = []

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


def _describe_project_text(run: dict | None = None) -> str:
    """Narrate a DAG run's status: nodes done/blocked, budgets, open questions.

    Feeds WS-A dag_status and the T-F17 mission-control UI. Accepts an optional
    ``run`` dict matching contracts/dag_schema.md; degrades gracefully when no run
    data is available (services/dag/ not yet landed as of T-F8).

    Data sources: the caller-supplied run dict (no engine RPC — DAG is Python-side).
    """
    if run is None:
        return _dumps({"ok": True, "text":
                       "No DAG run data supplied — pass run=<dag_run_dict> from "
                       "dag_status() once services/dag is available, or from your "
                       "WS-A dag driver directly. See contracts/dag_schema.md "
                       "for the expected shape."})

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
    return _dumps({"ok": True, "text": "\n".join(lines)})


def sketch_health_text(d: dict) -> str:
    """Render a ``doc_sketch_health`` result dict as plain English text for no-vision models.

    Returns a multi-line string with four sections:
      1. Closure: "CLOSED (N material regions)" or "OPEN: N dangling endpoints at: [handles]"
      2. Remaining DOF in plain words (from dof_narration).
      3. Conflicts and redundant constraints (if any).
      4. Ordered relations-then-dimensions schedule.

    T-DP7 DoD §4: this is the feedback channel that lets a no-vision local model SEE its work
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
    if dof <= 0 and status == "full":
        lines.append("DOF: 0 — fully constrained, ready to extrude")
    elif dof < 0 or status == "over":
        lines.append(f"DOF: {dof} — OVER-CONSTRAINED (conflicting constraints, see below)")
    else:
        lines.append(f"DOF: {dof} free — {narration}")

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
        for step in schedule:
            order = step.get("order", "?")
            kind = step.get("kind", "?")
            action = step.get("action", "?")
            why = step.get("why", "")
            entity = step.get("entity", "")
            handles = step.get("handles") or []
            if handles:
                h_str = ", ".join(str(h) for h in handles[:6])
                more_h = f" … +{len(handles)-6}" if len(handles) > 6 else ""
                lines.append(f"  {order}. [{kind}] {action}"
                             f" at {h_str}{more_h} — {why}")
            elif entity:
                lines.append(f"  {order}. [{kind}] {action} for {entity} — {why}")
            else:
                lines.append(f"  {order}. [{kind}] {action} — {why}")
    elif dof == 0 and status == "full":
        lines.append("Schedule: (nothing to do — sketch is fully constrained)")

    return "\n".join(lines)


def register_gsl_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def describe_part(doc_id: Optional[str] = None) -> str:
        """Compact, NAMED outline of the part — use INSTEAD of dumping every face on a complex part.

        Returns the bounding box, the feature groups WITH a placement narrative (which face each
        feature sits on, its offset from the part bbox on the two perpendicular axes, and which
        other features it touches via adjacency), any detected symmetry/instanced patterns (e.g.
        "6-fold rotational about +Z" with the blade faces collapsed to one line), the bores/holes
        and flat faces by side, and a stable `sid` for every addressable face/edge. A feature that
        owns no surviving faces (fully consumed by a later boolean) is called out explicitly.
        Token cost scales with the number of DISTINCT feature roles, not the raw face count — a
        68-face impeller describes in a few hundred tokens. Drill into one entity with hover(sid).
        """
        try:
            d = _digest(_doc(doc_id))
            return _dumps({"ok": True, "outline": _outline_text(d), "digest": d})
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
            try:
                overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
            except Exception as e:  # noqa: BLE001
                detail = getattr(e, "detail", None)
                if isinstance(detail, dict) and detail.get("reason") == "unknown sketch":
                    return _dumps({"ok": False,
                                   "error": f"no sketch {sketch!r} "
                                            "(call get_part for current ids)"})
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

        Sketch loop: edit -> sketch_health -> add the scheduled relations / run sketch_auto_constrain
        -> sketch_health again until status is "full".
        """
        try:
            d = _doc(doc_id)
            result = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            if text and result.get("ok"):
                result["health_text"] = sketch_health_text(result)
            return _dumps(result)
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
    def describe_scene(views: Optional[list] = None, doc_id: Optional[str] = None) -> str:
        """Deterministic "what the render would show" per view, from B-rep face normals — the
        non-vision fallback for render_part/contact_sheet.

        views: named views to describe, default ["front", "right", "top", "iso"] (same names
        render_part accepts). For each view: the camera direction, then every face whose signed
        normal points toward the camera (dot > 0.15) as "sid surf side r<radius>/area" (capped at
        20/view). Also includes the part-level narrative once at the top (bbox + the A3 feature
        placement lines from describe_part). OCCLUSION IS NOT MODELED — a face behind another
        still lists if it faces the camera; this is stated explicitly in the returned text.

        Returns {ok, text, views:{<name>: {dir, visible:[sids]}}}.
        """
        try:
            d = _doc(doc_id)
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

    # ---------------------------------------------------------------- narration (T-F7)

    @mcp.tool()
    def describe_toolpaths(job_id: Optional[str] = None) -> str:
        """Compact narrative of a CAM job's toolpaths and simulation results — use INSTEAD of
        dumping the raw cam_job_tree JSON. Returns per-op strategy/tool/stepdown/engagement,
        cycle-time and gouge/rapid checks from cam_simulate, and a G-code digest (bounds, feed
        range, tool-change count) from cam_post. Narrates existing numbers only — never
        recomputes. Call cam_generate + cam_simulate first for full results."""
        try:
            return _describe_toolpaths_text(job_id)
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

    # ---------------------------------------------------------------- narration (T-F8)

    @mcp.tool()
    def describe_assembly(doc_id: Optional[str] = None) -> str:
        """Compact narrative of an assembly (or multi-body part) — world placement, fit/clearance,
        and mate solve summary. Use INSTEAD of dumping asm_world_bodies JSON.

        Returns per-component bounding boxes and world-frame origins, interference/clearance pairs
        from check_interference (volumes and gap distances), and the top-level mate solve status.
        Narrates existing numbers only — never recomputes. Works on assembly docs (components placed
        by mates) and multi-body parts (transform-assembled bodies). Returns a graceful message when
        no bodies are present. Call solve_mates first to update the assembly placement.
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
    def describe_project(run: Optional[dict] = None) -> str:
        """Compact narrative of a DAG project run — nodes done/blocked, budgets, open questions.
        Feeds WS-A dag_status and the T-F17 mission-control UI.

        run: a dag_run dict matching contracts/dag_schema.md — pass dag_status()['run'] or your
        WS-A driver's run record. When omitted, returns a graceful 'no run supplied' message.

        Returns node status counts (done/doing/blocked/pending), per-node titles and owners,
        budget usage (tokens spent vs cap), and open questions by task id. Narrates the dag_run
        dict only — never queries the engine. See contracts/dag_schema.md for the run shape.
        """
        try:
            return _describe_project_text(run)
        except Exception as e:  # noqa: BLE001
            return _err(e)
