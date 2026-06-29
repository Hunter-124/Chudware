"""GSL (Geometry-Semantics Layer) MCP tools — a compact, NAMED, progressive-pull view of a part's
geometry that replaces the flat 68-face dump.

  describe_part(doc_id)      -> the part OUTLINE: bbox, feature groups, detected symmetry/instances,
                                addressable handles (bores/caps/round faces) with their stable ids.
  query_adjacency(doc_id)    -> the face-adjacency graph (W3): which faces touch, convex vs concave.
  hover(sid, doc_id)         -> expand ONE face/edge by the stable id describe_part/query_geometry gave.

These let the model pull a small, named view and address geometry by a STABLE handle instead of
re-reading the whole B-rep after every edit.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server import core_bridge as cb
from mcp_server.cad_tools import _doc, _dumps, _err


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
    fmap = {f.get("id"): f for f in feats if isinstance(f, dict)}
    owner_map = tree.get("owner_map") or {}

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

    feat_summary = []
    for fid, om in owner_map.items():
        nf = len(om.get("faces", []))
        if not nf:
            continue
        ft = fmap.get(fid, {})
        feat_summary.append({"id": fid, "name": ft.get("name"), "type": ft.get("type"),
                             "faces": nf})

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
        lines.append("Features (each owns these many faces):")
        for fg in d["feature_groups"]:
            lines.append(f'  - {fg["id"]} {fg.get("name") or ""} [{fg.get("type")}]: '
                         f'{fg["faces"]} face(s)')
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


def register_gsl_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def describe_part(doc_id: Optional[str] = None) -> str:
        """Compact, NAMED outline of the part — use INSTEAD of dumping every face on a complex part.

        Returns the bounding box, the feature groups, any detected symmetry/instanced patterns
        (e.g. "6-fold rotational about +Z" with the blade faces collapsed to one line), the
        bores/holes and flat faces by side, and a stable `sid` for every addressable face/edge.
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
