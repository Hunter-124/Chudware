"""Measured shot planner (ViewNet §3): the pictures an agent gets of the geometry it edits.

Every candidate camera is MEASURED by the engine (`doc_view_metrics`: exact z-buffer face and
edge visibility through the same display mesh and camera math `doc_render_view` uses), the
view SET is ranked by a utility over those measurements (the trained utility model when its
weights load, else features.hand_utility) and picked greedily on marginal utility, so four
tiles carry four different pieces of information. Nothing here guesses visibility in Python.

  candidate_cameras  icosphere + target-normal lattice (+ section cuts through an internal
                     target), each an exact perspective pose fitted to the subject's own points
  view_metrics       batched doc_view_metrics, faces joined to doc_query ordinals and SIDs
  plan_shots         candidates -> metrics -> greedy set -> {views, model, coverage, unseen}
  render_shot_grid   the chosen views rendered, overlaid, SID-tagged and tiled A-D
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from . import cad_common as cc
from . import cad_viewpoint as _vp

try:
    from services.viewpoint.utility import features as _uf
    from services.viewpoint.utility import model as _um
except ImportError:  # pragma: no cover - server launched with services/ itself on sys.path
    from viewpoint.utility import features as _uf  # type: ignore[no-redef]
    from viewpoint.utility import model as _um  # type: ignore[no-redef]


METRICS_W, METRICS_H = 192, 144
TILE_W, TILE_H = 512, 384
DEDUPE_DEG = 25.0
_BATCH = 170                    # doc_view_metrics budget per call (contract §1)
_LATTICE_DEDUPE_DEG = 3.0
_LATTICE_TILTS = (20, 35, 55, 70)
# Past this visible/projected fraction some plain view "sees" the target; below it a cut
# through the target is the only picture that shows it.
_SEE_ENOUGH = 0.5
# Share of the limiting tile dimension the subject's PROJECTED extent fills once centred. The
# subject is the part's own tessellation (its hull points), or the target's surface points — a
# target fills less so a ring of its surroundings stays in frame to place it.
_PART_FILL = 0.82
_TARGET_FILL = 0.62
_SECTION_FILL = 0.5
_HULL_DIRS = 642                # directions whose extreme vertices stand in for the whole mesh
_NEAR_VERTICAL = 0.94           # |dir.z| past this (~20 deg off vertical) snaps screen-up
_SECTION_TILT_DEG = 45.0
_TAG_MAX = 8
_TAG_MIN_FRAC = 0.0015          # a face must hold >= 0.15 % of the tile to earn a SID tag
_CAM_KEYS = ("projection", "position", "target", "up", "vertical_fov_deg", "near", "far",
             "width", "height")
_SIDE_WORD = {"top": "+Z", "bottom": "-Z", "front": "-Y", "back": "+Y", "right": "+X",
              "left": "-X"}
_ROLE_RANK = (("target", 0), ("bc", 1), ("context", 2))
_ROLE_NAMES = ("target", "bc", "context", "other")


# ------------------------------------------------------------------------------ vectors
def _dot(a, b) -> float:
    return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


def _cross(a, b) -> tuple:
    return (float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
            float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
            float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]))


def _unit(v) -> Optional[tuple]:
    try:
        q = (float(v[0]), float(v[1]), float(v[2]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    n = math.sqrt(_dot(q, q))
    if not math.isfinite(n) or n < 1e-12:
        return None
    return (q[0] / n, q[1] / n, q[2] / n)


def _tangents(axis: tuple) -> tuple:
    ref = (0.0, 1.0, 0.0) if abs(axis[2]) > 0.985 else (0.0, 0.0, 1.0)
    t0 = _unit(_cross(ref, axis))
    return t0, _cross(axis, t0)


def _dominant_normal(normals) -> Optional[tuple]:
    """Sign-aligned mean of the target's normals/axes (a bore's axis sign is arbitrary)."""
    acc = [0.0, 0.0, 0.0]
    found = False
    for item in normals or ():
        n = _unit(item)
        if n is None:
            continue
        if found and _dot(acc, n) < 0.0:
            n = (-n[0], -n[1], -n[2])
        acc = [acc[k] + n[k] for k in range(3)]
        found = True
    return _unit(acc) if found else None


def _az_el(d) -> str:
    """render_part's azimuth/elevation vocabulary: az 0 = front (-Y), 90 = right (+X)."""
    el = math.degrees(math.asin(max(-1.0, min(1.0, float(d[2])))))
    az = math.degrees(math.atan2(float(d[0]), -float(d[1]))) % 360.0
    return f"az{az:03.0f} el{el:+03.0f}"


def _axis_word(n) -> str:
    for k, name in enumerate("XYZ"):
        if abs(float(n[k])) >= 0.95:
            return ("+" if float(n[k]) > 0 else "-") + name
    return "[" + ", ".join(f"{float(x):.2f}" for x in n[:3]) + "]"


def _bbox_center(bb: dict) -> tuple:
    lo, hi = cc._v3(bb.get("min")), cc._v3(bb.get("max"))
    return tuple((lo[k] + hi[k]) * 0.5 for k in range(3))


def icosphere_dirs(n_min: int = 162) -> list:
    """Unit directions of the smallest subdivided icosahedron with >= n_min vertices
    (12, 42, 162, 642, ...), in a fixed deterministic order."""
    t = (1.0 + math.sqrt(5.0)) / 2.0
    verts = [_unit(v) for v in ((-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0),
                                (0, -1, t), (0, 1, t), (0, -1, -t), (0, 1, -t),
                                (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1))]
    faces = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9),
             (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2),
             (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10),
             (8, 6, 7), (9, 8, 1)]
    while len(verts) < int(n_min):
        cache: dict = {}

        def _mid(a: int, b: int) -> int:
            key = (min(a, b), max(a, b))
            if key not in cache:
                verts.append(_unit(tuple((verts[a][k] + verts[b][k]) * 0.5 for k in range(3))))
                cache[key] = len(verts) - 1
            return cache[key]

        nxt = []
        for a, b, c in faces:
            ab, bc, ca = _mid(a, b), _mid(b, c), _mid(c, a)
            nxt += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        faces = nxt
    return verts


# ------------------------------------------------------------------------------ candidates
def _fit_camera(points, d, width: int, height: int, fill: float, part_bbox: dict,
                fov_deg: float = _vp.PERSPECTIVE_FOV_DEG) -> dict:
    """Exact perspective pose looking along -d whose view of `points` fills `fill` of the
    limiting tile dimension, centred on the points' projected extent.

    World +Z stays up on screen. A near-vertical camera (within ~20 deg of +-Z) has almost no
    +Z to show, and Z-up there spins the part to whatever azimuth the camera happens to sit
    at; it gets the world +-X/+-Y axis nearest that screen-up instead (world +Y straight down),
    so the plan reads square. The camera only translates to centre the silhouette — the
    optical axis stays `d`, so the view never rolls. For look-at c at distance D, a point at
    camera-basis offset (x, y, z) projects to x / (D - z); the fit is D = max(z + |x| /
    (tan_x * fill), z + |y| / (tan_y * fill)), then c moves in the image plane to the middle
    of the projected extent and D is refitted (three passes converge to well under a pixel).
    Same camera_pose.v1 dict as cad_viewpoint.perspective_camera, near/far sized the same way."""
    import numpy as np  # noqa: PLC0415
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    d = np.asarray(_unit(d) or _unit((1.0, 1.0, 0.8)), dtype=np.float64)
    if abs(d[2]) > 0.985:
        up_ref = np.array([0.0, 1.0, 0.0])
    else:
        up_ref = np.array([0.0, 0.0, 1.0])
        if abs(d[2]) > _NEAR_VERTICAL:
            z_up = up_ref - d * d[2]
            axes = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                             [0.0, -1.0, 0.0]])
            up_ref = axes[int(np.argmax(axes @ z_up))]
    right = np.cross(-d, up_ref)
    right /= np.linalg.norm(right)
    up = np.cross(right, -d)
    fov = max(5.0, min(float(fov_deg), 120.0))
    tan_y = math.tan(math.radians(fov) / 2.0)
    tan_x = tan_y * float(width) / float(max(height, 1))
    fill = max(0.05, min(float(fill), 1.0))
    R, U, Z = P @ right, P @ up, P @ d
    ctr = 0.5 * (P.min(axis=0) + P.max(axis=0))
    cr, cu, cz = float(ctr @ right), float(ctr @ up), float(ctr @ d)
    for it in range(4):
        x, y, z = R - cr, U - cu, Z - cz
        span = 2.0 * float(np.sqrt((x * x + y * y + z * z).max())) or 1.0
        z_front = float(z.max())
        fit = max(float((z + np.abs(x) / (tan_x * fill)).max()),
                  float((z + np.abs(y) / (tan_y * fill)).max()), z_front + 0.05 * span)
        if it == 3:
            break
        depth = np.maximum(fit - z, 1e-9)
        sx, sy = x / depth, y / depth
        cr += 0.5 * float(sx.max() + sx.min()) * fit
        cu += 0.5 * float(sy.max() + sy.min()) * fit
    center = cr * right + cu * up + cz * d
    position = center + d * fit
    near = max(1e-3 * fit, 1e-4)
    corners = np.asarray(_vp._box_corners(part_bbox), dtype=np.float64) if part_bbox else P
    far_depth = max(fit + span, float(((corners - position) @ -d).max()))
    far = max(far_depth * 1.05, near * 10.0)
    return {"projection": "perspective", "position": position.tolist(),
            "target": center.tolist(), "up": up.tolist(), "vertical_fov_deg": fov,
            "near": float(near), "far": float(far), "width": int(width),
            "height": int(height), "distance": float(fit), "fit_distance": float(fit)}


def _subject(points, bbox: dict):
    """The points a camera is fitted to: the given surface points, else the box corners."""
    return points if points is not None and len(points) >= 2 else _vp._box_corners(bbox)


def hull_points(pos, n_dirs: int = _HULL_DIRS):
    """The vertices of `pos` that are extreme along any of ~n_dirs directions: a few hundred
    points whose projection bounds the whole mesh's from every candidate camera (to within the
    angular gap between directions), so fitting 170 cameras costs nothing."""
    import numpy as np  # noqa: PLC0415
    P = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
    if P.shape[0] <= n_dirs:
        return P
    # The icosphere holds every direction's antipode, so maxima alone cover both extremes.
    D = np.asarray(icosphere_dirs(n_dirs), dtype=np.float32).T
    Q = (P - P.mean(axis=0)).astype(np.float32)
    best = np.full(D.shape[1], -np.inf, dtype=np.float32)
    arg = np.zeros(D.shape[1], dtype=np.int64)
    cols = np.arange(D.shape[1])
    for lo in range(0, Q.shape[0], 4096):
        proj = Q[lo:lo + 4096] @ D
        k = proj.argmax(axis=0)
        v = proj[k, cols]
        better = v > best
        best[better] = v[better]
        arg[better] = k[better] + lo
    return P[np.unique(arg)]


def section_candidates(part_bbox: dict, target_bbox: dict, target_normals=None, *,
                       target_points=None, width: int = TILE_W, height: int = TILE_H) -> list:
    """Cuts through the target centre. Plane normals: the three principal axes plus the two
    directions perpendicular to the target's dominant normal/axis (planes CONTAINING it, the
    cut that shows a bore's profile). Each plane is taken with both signs — the kept half is
    dot(p - point, normal) <= 0, so the other sign keeps the other half — and for each the
    camera sits on the removed (+normal) side, 45 deg off the plane normal at four azimuths,
    looking at the kept half's cut face; the engine draws the removed half as a ghost."""
    planes: list = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    dom = _dominant_normal(target_normals)
    if dom is not None:
        planes += list(_tangents(dom))
    return _section_planes(part_bbox, target_bbox, planes, target_points=target_points,
                           width=width, height=height)


def _section_planes(part_bbox: dict, target_bbox: dict, planes: list, *,
                    target_points=None, width: int = TILE_W, height: int = TILE_H) -> list:
    center = _bbox_center(target_bbox)
    subject = _subject(target_points, target_bbox)
    unique: list = []
    lim = math.cos(math.radians(10.0))
    for n in planes:
        if all(abs(_dot(n, u)) < lim for u in unique):
            unique.append(n)
    ct, st = math.cos(math.radians(_SECTION_TILT_DEG)), math.sin(math.radians(_SECTION_TILT_DEG))
    out: list = []
    for n in unique:
        for sign in (1.0, -1.0):
            nn = tuple(sign * n[k] for k in range(3))
            u1, u2 = _tangents(nn)
            for k, tan in enumerate((u1, u2, tuple(-x for x in u1), tuple(-x for x in u2))):
                d = _unit(tuple(ct * nn[i] + st * tan[i] for i in range(3)))
                out.append({
                    "camera": _fit_camera(subject, d, width, height, _SECTION_FILL, part_bbox),
                    "dir": list(d),
                    "label": f"section {_axis_word(nn)} {_az_el(d)}",
                    "section": {"point": [float(c) for c in center],
                                "normal": [float(c) for c in nn]},
                })
    return out


def _diagonal_section_candidates(part_bbox: dict, target_bbox: dict, target_normals,
                                 target_points) -> list:
    """Planner-only profiles: keep the training pool's saved indices unchanged."""
    dom = _dominant_normal(target_normals)
    if dom is None:
        return []
    t0, t1 = _tangents(dom)
    existing = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), t0, t1]
    lim = math.cos(math.radians(10.0))
    planes = [_unit(tuple(t0[i] + sign * t1[i] for i in range(3))) for sign in (-1, 1)]
    planes = [n for n in planes if all(abs(_dot(n, u)) < lim for u in existing)]
    return _section_planes(part_bbox, target_bbox, planes, target_points=target_points)


def candidate_cameras(part_bbox: dict, *, target_bbox: Optional[dict] = None,
                      target_normals=None, target_points=None, part_points=None,
                      internal: bool = False, n_sphere: int = 162, width: int = TILE_W,
                      height: int = TILE_H) -> list:
    """Every camera the planner measures: {"camera", "dir", "label", "section"}.

    Icosphere directions (all of them — the back of a part is information), plus with a
    target the lattice around its dominant normal in BOTH hemispheres (tilts 20/35/55/70 deg
    x 8 azimuths: shallow tilts read a planar face obliquely, steep ones are what a bore or a
    boss needs); section cuts when `internal`. Directions within 3 deg of an earlier one are
    dropped. Each camera is fitted (_fit_camera) to the subject's own points — the target's
    surface points, or `part_points` (hull_points of the tessellation) for the whole part;
    the bbox corners only when those are missing."""
    if target_bbox is not None:
        subject, fill = _subject(target_points, target_bbox), _TARGET_FILL
    else:
        subject, fill = _subject(part_points, part_bbox), _PART_FILL
    out: list = []
    lim = math.cos(math.radians(_LATTICE_DEDUPE_DEG))

    def _add(d, tag: str) -> None:
        d = _unit(d)
        if d is None or any(_dot(d, c["dir"]) >= lim for c in out):
            return
        out.append({"camera": _fit_camera(subject, d, width, height, fill, part_bbox),
                    "dir": list(d), "label": f"{tag} {_az_el(d)}", "section": None})

    dom = _dominant_normal(target_normals) if target_bbox is not None else None
    if dom is not None:
        for sign, tag in ((1.0, "normal"), (-1.0, "anti")):
            ax = tuple(sign * dom[k] for k in range(3))
            _add(ax, tag)
            t0, t1 = _tangents(ax)
            for tilt in _LATTICE_TILTS:
                ct, st = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
                for az in range(0, 360, 45):
                    ca, sa = math.cos(math.radians(az)), math.sin(math.radians(az))
                    _add(tuple(ct * ax[k] + st * (ca * t0[k] + sa * t1[k]) for k in range(3)),
                         f"{tag}+{tilt}")
    for d in icosphere_dirs(n_sphere):
        _add(d, "sphere")
    if internal and target_bbox is not None:
        out += section_candidates(part_bbox, target_bbox, target_normals,
                                  target_points=target_points, width=width, height=height)
    return out


# ------------------------------------------------------------------------------ measurement
def _section_payload(section) -> Optional[dict]:
    if not section:
        return None
    return {"point": [float(x) for x in section["point"][:3]],
            "normal": [float(x) for x in section["normal"][:3]]}


def _measure(doc_id: str, candidates: list, width: int, height: int) -> dict:
    cams, secs = [], []
    for c in candidates:
        cam = {k: c["camera"][k] for k in _CAM_KEYS if k in c["camera"]}
        cam["width"], cam["height"] = int(width), int(height)
        cams.append(cam)
        secs.append(_section_payload(c.get("section")))
    use_secs = any(s is not None for s in secs)
    out: Optional[dict] = None
    for lo in range(0, len(cams), _BATCH):
        params = {"doc_id": doc_id, "width": int(width), "height": int(height),
                  "cameras": cams[lo:lo + _BATCH]}
        if use_secs:
            params["sections"] = secs[lo:lo + _BATCH]
        r = cc.cb.call("doc_view_metrics", params) or {}
        if not r.get("ok"):
            raise ValueError("doc_view_metrics refused: "
                             + str(r.get("reason") or r.get("error") or "no reason given"))
        views = list(r.get("views") or [])
        if len(views) != len(params["cameras"]):
            raise ValueError(f"doc_view_metrics answered {len(views)} views for "
                             f"{len(params['cameras'])} cameras")
        if out is None:
            out = dict(r)
            out["views"] = views
        else:
            if r.get("revision") != out.get("revision"):
                raise ValueError("the document changed while its views were being measured")
            out["views"].extend(views)
            out["timing_ms"] = float(out.get("timing_ms") or 0.0) + float(r.get("timing_ms") or 0.0)
    if out is None:
        raise ValueError("view_metrics: no candidate cameras to measure")
    for view, c in zip(out["views"], candidates):
        view["dir"] = [float(x) for x in c["dir"]]
        view["label"] = c.get("label")
        view["section"] = c.get("section")
        view["reveal_key"] = c.get("reveal_key")
        view["xray"] = bool(c.get("xray"))
    return out


def _join_topology(doc_id: str, metrics: dict, *, topo=None) -> None:
    """Stamp every face/edge row with its doc_query running ordinal, topo id and SID.

    The engine's (body, face_id) is per-body 1-based (AICM/TopExp); doc_query numbers the
    whole document with one counter. cad_common._mesh_topo_index is that per-body offset."""
    face_by_topo, edge_by_topo = cc._mesh_topo_index({"faces": metrics.get("faces") or [],
                                                      "edges": metrics.get("edges") or []})
    topo = cc._doc_topo_maps(doc_id) if topo is None else topo
    for rows, by_topo, matches in ((metrics.get("faces") or [], face_by_topo, topo["face"]),
                                   (metrics.get("edges") or [], edge_by_topo, topo["edge"])):
        topo_of = {i: t for t, i in by_topo.items()}
        for i, row in enumerate(rows):
            t = topo_of.get(i)
            m = matches[t - 1] if t is not None and 0 < t <= len(matches) else {}
            row["topo"] = t
            row["ordinal"] = (t - 1) if t is not None else None
            row["sid"] = m.get("sid")
            for key in ("surf", "side", "axis", "radius", "concavity"):
                if m.get(key) is not None:
                    row[key] = m.get(key)


def _edge_faces(doc_id: str, metrics: dict, *, mesh=None) -> Optional[list]:
    """edge index -> adjacent face indices, from the tessellation: the display mesh shares the
    edge discretisation with both faces it bounds, so the faces an edge touches are the faces
    owning a vertex at EVERY point of its polyline (endpoints alone also touch the faces
    meeting at the corner; intersecting over the whole polyline leaves the two neighbours).
    None when the tessellation is unreadable."""
    mesh = cc._doc_mesh(doc_id) if mesh is None else mesh
    if mesh is None:
        return None
    import numpy as np  # noqa: PLC0415
    faces = metrics.get("faces") or []
    edges = metrics.get("edges") or []
    face_key = {(int(f["body"]), int(f["face_id"])): i for i, f in enumerate(faces)}
    edge_key = {(int(e["body"]), int(e["edge_id"])): i for i, e in enumerate(edges)}
    # Both tables walk bodies in ascending order; when the engine numbers them differently
    # from the tessellation (ordinal vs id), pair them positionally.
    rpc_bodies = sorted({b for b, _f in face_key})
    mesh_bodies = sorted({int(fr["body"]) for fr in mesh["faces"]})
    body_of = (dict(zip(mesh_bodies, rpc_bodies))
               if set(mesh_bodies) != set(rpc_bodies) and len(mesh_bodies) == len(rpc_bodies)
               else {})
    mf = np.asarray([face_key.get((body_of.get(int(fr["body"]), int(fr["body"])),
                                   int(fr["face_id"])), -1)
                     for fr in mesh["faces"]] or [-1], dtype=np.int64)
    tri_face = mesh["tri_face"]
    ok = tri_face >= 0
    vid = mesh["idx"][ok].reshape(-1)
    fid = mf[np.repeat(tri_face[ok], 3)]
    keep = fid >= 0
    pairs = np.unique(np.stack([vid[keep], fid[keep]], axis=1), axis=0)
    vkeys = [tuple(r) for r in np.round(mesh["pos"], 4).tolist()]
    at: dict = {}
    for v, f in pairs.tolist():
        at.setdefault(vkeys[v], set()).add(int(f))
    out: list = [[] for _ in edges]
    for me in mesh["edges"]:
        ei = edge_key.get((body_of.get(int(me["body"]), int(me["body"])), int(me["edge_id"])))
        if ei is None:
            continue
        common: Optional[set] = None
        for p in np.round(np.asarray(me["pts"], dtype=np.float64), 4).tolist():
            here = at.get(tuple(p), set())
            common = set(here) if common is None else (common & here)
            if not common:
                break
        out[ei] = sorted(common or ())
    return out


def view_metrics(doc_id: str, candidates: list, width: int = METRICS_W,
                 height: int = METRICS_H) -> dict:
    """Batched doc_view_metrics over `candidates` (same order in "views"). Each view also
    carries the candidate's "dir", "label" and "section"; each face/edge row its doc_query
    "ordinal" (0-based), "topo" (1-based), "sid" and the query's surf/side/axis/radius; and
    "edge_faces" maps edge index -> adjacent face indices (None without a tessellation).
    Raises ValueError with the engine's reason when it refuses."""
    metrics = _measure(doc_id, candidates, width, height)
    _join_topology(doc_id, metrics)
    metrics["edge_faces"] = _edge_faces(doc_id, metrics)
    return metrics


def _extend(doc_id: str, metrics: dict, candidates: list) -> None:
    more = _measure(doc_id, candidates, int(metrics.get("width") or METRICS_W),
                    int(metrics.get("height") or METRICS_H))
    if more.get("revision") != metrics.get("revision"):
        raise ValueError("the document changed while its views were being measured")
    metrics["views"].extend(more["views"])


# ------------------------------------------------------------------------------ selection
def _section_key(section) -> Optional[tuple]:
    if not section:
        return None
    n = _unit(section["normal"])
    if n is None:
        return None
    if next((x for x in n if abs(x) > 1e-8), 1.0) < 0:
        n = tuple(-x for x in n)
    return (tuple(round(x, 3) for x in n),
            round(_dot(n, section["point"]), 3))


def _symmetry(metrics: dict, target_faces=None) -> tuple:
    """Recover only supported symmetries from face geometry, never from camera scores.

    Coaxial revolved surfaces and perpendicular end planes describe a body of revolution.
    A target can have that symmetry while its surroundings do not. Reflection is accepted
    only when every face has a same-area, same-type reflected counterpart.
    """
    import numpy as np  # noqa: PLC0415
    d = _uf._dense(metrics)
    ids = list(target_faces) if target_faces else list(range(d["F"]))
    axes = []
    for fi in ids:
        a = _unit(d["axis"][fi])
        if a is None or any(abs(_dot(a, old)) > 0.99 for old in axes):
            continue
        curved = [j for j in ids if d["revolved"][j]]
        if not curved or any(abs(_dot(a, d["axis"][j])) < 0.99 for j in curved):
            continue
        center = d["centroid"][curved[0]]
        scale = max(d["diag"], 1.0)
        supported = True
        for j in ids:
            delta = d["centroid"][j] - center
            radial = delta - np.dot(delta, a) * np.asarray(a)
            if d["surf"][j] == 0:
                supported &= abs(_dot(a, d["normal"][j])) > 0.99
            elif not d["revolved"][j]:
                supported = False
            # The centroid of a complete revolution lies on its axis.
            supported &= float(np.linalg.norm(radial)) < scale * 0.015
        if supported:
            axes.append(a)
    mirrors = []
    if len(ids) >= 2:
        P, N = d["centroid"][ids], d["normal"][ids]
        center = (P.min(axis=0) + P.max(axis=0)) * 0.5
        tol = max(d["diag"] * 0.005, 1e-5)
        for axis in np.eye(3):
            reflected = P - 2 * ((P - center) @ axis)[:, None] * axis
            normals = N - 2 * (N @ axis)[:, None] * axis
            supported = True
            for q, j in enumerate(ids):
                matched = [r for r, f in enumerate(ids)
                           if d["surf"][f] == d["surf"][j] and
                           abs(d["area"][f] - d["area"][j]) <= max(d["area"][j] * 0.02, 1e-6)
                           and np.linalg.norm(P[r] - reflected[q]) <= tol and
                           (d["revolved"][j] or np.linalg.norm(N[r] - normals[q]) < 0.05)]
                if not matched:
                    supported = False
                    break
            if supported:
                mirrors.append(tuple(axis))
    return axes, mirrors


def greedy_select(metrics: dict, score_set: Callable, k: int = 4, *,
                  dedupe_deg: float = DEDUPE_DEG, pinned=None, eligible=None,
                  uninformative=None, target_faces=None, eligibility_tiers=None,
                  guarantee_k: bool = True) -> list:
    """Greedy marginal utility, excluding cameras or target footprints that repeat a tile.

    The face-pixel histogram is a proxy for the target's screen mask: the metrics RPC
    reports per-face projected/visible pixels, not a pixel buffer. Distinct section
    planes and an x-ray versus a cut may show different geometry at the same angle,
    but neither excuses two nearly identical camera directions.
    """
    views = metrics.get("views") or []
    dirs = [_unit(v.get("dir")) or (0.0, 0.0, 1.0) for v in views]
    keys = [_section_key(v.get("section")) for v in views]
    lim = math.cos(math.radians(max(0.0, float(dedupe_deg))))
    import numpy as np  # noqa: PLC0415
    dense = _uf._dense(metrics)
    coverage = np.clip(dense["legible_front"] / np.maximum(dense["area"], 1e-9),
                       0.0, 1.0)
    target = np.asarray(list(target_faces or ()), dtype=np.int64)
    target = target[(target >= 0) & (target < dense["F"])]
    pixels = dense["vis_front"][:, target] if target.size else dense["vis_front"]
    symmetry_axes, mirror_planes = _symmetry(metrics, target.tolist())
    if target.size:
        whole_axes, _ = _symmetry(metrics)
        symmetry_axes += [a for a in whole_axes
                          if all(abs(_dot(a, b)) < 0.99 for b in symmetry_axes)]
    tiers = (list(eligibility_tiers) if eligibility_tiers is not None else
             [0 if eligible is None or eligible[i] else 1 for i in range(len(views))])

    def symmetric(i: int, j: int) -> bool:
        if bool(views[i].get("section")) != bool(views[j].get("section")) or (
                bool(views[i].get("xray")) != bool(views[j].get("xray"))):
            return False
        for axis in symmetry_axes:
            if abs(_dot(dirs[i], axis) - _dot(dirs[j], axis)) < 0.16:
                if keys[i] is None or (
                        abs(abs(_dot(keys[i][0], axis)) - abs(_dot(keys[j][0], axis))) < 0.1):
                    return True
        for normal in mirror_planes:
            reflected = tuple(dirs[i][q] - 2 * _dot(dirs[i], normal) * normal[q]
                              for q in range(3))
            if _dot(reflected, dirs[j]) > math.cos(math.radians(12)):
                if keys[i] is None:
                    return True
                cut = tuple(keys[i][0][q] - 2 * _dot(keys[i][0], normal) * normal[q]
                            for q in range(3))
                if abs(_dot(cut, keys[j][0])) > 0.98:
                    return True
        return False

    def looks_same(i: int, j: int) -> bool:
        if keys[i] is not None and keys[j] is not None:
            if abs(_dot(keys[i][0], keys[j][0])) < math.cos(math.radians(30.0)):
                return False
        a, b = coverage[i], coverage[j]
        face_overlap = float(np.minimum(a, b).sum()) / max(
            float(np.maximum(a, b).sum()), 1e-9)
        if _dot(dirs[i], dirs[j]) > math.cos(math.radians(42.0)) and face_overlap > 0.52:
            return True
        # Equal target-pixel distributions on two same-mode close-ups convey the
        # same surface detail even when their remaining context faces differ.
        if bool(views[i].get("section")) != bool(views[j].get("section")) or (
                bool(views[i].get("xray")) != bool(views[j].get("xray"))):
            return False
        a, b = pixels[i], pixels[j]
        if min(float(a.sum()), float(b.sum())) < 2.0:
            return False
        overlap = float(np.minimum(a, b).sum()) / max(
            float(np.maximum(a, b).sum()), 1e-9)
        return overlap > 0.8 and _dot(dirs[i], dirs[j]) > math.cos(math.radians(70.0))

    chosen: list = []
    out: list = []
    cur = float(score_set([]))
    for i in pinned or ():
        i = int(i)
        if 0 <= i < len(views) and i not in chosen:
            u = float(score_set(chosen + [i]))
            out.append({"index": i, "utility": u, "marginal_gain": u - cur})
            chosen.append(i)
            cur = u
    while len(chosen) < max(int(k), 1):
        best = None
        taken = set(chosen)
        already_blank = uninformative is not None and any(uninformative[j] for j in chosen)
        # Eligibility and deduplication are preferences, not a reason to return a
        # two- or three-tile grid. Exhaust the readable unique pool before relaxing.
        stages = (0, 1, 2) if guarantee_k else (0,)
        for stage in stages:
            pool = []
            for i in range(len(views)):
                if i in taken or (stage == 0 and already_blank and uninformative[i]):
                    continue
                if not guarantee_k and eligible is not None and not eligible[i]:
                    continue
                if stage < 2 and any(
                        _dot(dirs[i], dirs[j]) > lim or
                        (keys[i] is not None and keys[i] == keys[j]) or
                        symmetric(i, j) or (stage == 0 and looks_same(i, j))
                        for j in chosen):
                    continue
                if stage == 2 and any(
                        _dot(dirs[i], dirs[j]) > 0.999999 and keys[i] == keys[j] and
                        bool(views[i].get("xray")) == bool(views[j].get("xray"))
                        for j in chosen):
                    continue
                pool.append(i)
            rank = min((tiers[i] if guarantee_k else 0 for i in pool), default=5)
            if best is None or rank <= best[2]:
                # Find the best eligibility class before invoking the learned
                # model or hand scorer; weaker classes cannot win this round.
                for i in pool:
                    if guarantee_k and tiers[i] != rank:
                        continue
                    u = float(score_set(chosen + [i]))
                    if best is None or rank < best[2] or u > best[1] + 1e-12:
                        best = (i, u, rank)
            if best is not None and (
                    (stage == 0 and best[2] <= 1) or (stage == 1 and best[2] < 4) or
                    stage == 2 or not guarantee_k):
                break
        if best is None:
            break
        out.append({"index": best[0], "utility": best[1], "marginal_gain": best[1] - cur})
        chosen.append(best[0])
        cur = best[1]
    return out


def set_scorer(metrics: dict, roles: Optional[dict], context: dict, model=None) -> Callable:
    """score(view_indices) -> float: the model's logit when `model` is given, else
    features.hand_utility."""
    if model is None:
        return lambda S: _uf.hand_utility(metrics, S, roles)
    return lambda S: model.score(_uf.build_set_tokens(metrics, S, roles, context))


def _choose_model(mode: str) -> tuple:
    """(model or None, model id for the caption, note)."""
    mode = str(mode or "auto").strip().lower()
    if mode == "hand":
        return None, "hand", "hand utility requested"
    if mode not in ("auto", "model"):
        raise ValueError(f"plan_shots mode must be 'auto', 'model' or 'hand', got {mode!r}")
    model, note = _um.load_default()
    if model is None:
        if mode == "model":
            raise ValueError(f"plan_shots(mode='model'): {note}")
        return None, "hand", note
    return model, model.name, ""


# ------------------------------------------------------------------------------ planning
def target_points(doc_id: str, matches: list) -> Optional[list]:
    """World vertices of the target's own triangles (what a tight camera fit must enclose)."""
    if not matches:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        mesh = cc._doc_mesh(doc_id)
        sel = cc._target_mesh_tris(mesh, doc_id, matches)
        if sel is None:
            return None
        ids = np.unique(mesh["idx"][sel].reshape(-1))
        return mesh["pos"][ids].tolist()
    except Exception:  # noqa: BLE001 — no tessellation: fit the target box corners instead
        return None


_KIND_OF = {"face": "face", "edge": "edge", "feature": "feature", "point": "point",
            "part": "part"}


def _resolve_target(doc_id: str, target) -> Optional[dict]:
    if target is None:
        return None
    if isinstance(target, dict) and "matches" in target:
        matches = list(target.get("matches") or [])
        bbox = target.get("bbox") or (cc._target_bbox(doc_id, matches) if matches else None)
        if bbox is None:
            raise ValueError("plan_shots: a target needs matches or a bbox to frame")
        return {"matches": matches, "label": str(target.get("label") or "target"),
                "bbox": bbox, "kind": _KIND_OF.get(str(target.get("kind") or ""), "set")}
    specs = target if isinstance(target, list) else [target]
    resolved = [cc._resolve_inspect_target(doc_id, s) for s in specs]
    matches = [m for r in resolved for m in (r.get("matches") or [])]
    kind = _KIND_OF.get(str(resolved[0].get("kind") or ""), "set") if len(resolved) == 1 else "set"
    return {"matches": matches,
            "label": ", ".join(str(r.get("label") or s) for r, s in zip(resolved, specs)),
            "bbox": cc._target_bbox(doc_id, matches), "kind": kind}


def _roles(metrics: dict, matches: list, roles: Optional[dict]) -> dict:
    """Explicit roles win; otherwise the target's faces (an edge target: the faces it bounds)
    and, as the context ring, every face sharing an edge with them."""
    out = {k: sorted({int(o) for o in (roles or {}).get(k) or ()})
           for k in ("target", "context", "bc")}
    faces = metrics.get("faces") or []
    edges = metrics.get("edges") or []
    edge_faces = metrics.get("edge_faces") or []
    if matches and not out["target"]:
        face_ord = {str(f["sid"]): f["ordinal"] for f in faces
                    if f.get("sid") and f.get("ordinal") is not None}
        edge_idx = {str(e["sid"]): i for i, e in enumerate(edges) if e.get("sid")}
        tgt: set = set()
        for m in matches:
            sid = str(m.get("sid") or "")
            if sid in face_ord:
                tgt.add(int(face_ord[sid]))
            elif sid in edge_idx and edge_idx[sid] < len(edge_faces):
                for fi in edge_faces[edge_idx[sid]]:
                    if faces[fi].get("ordinal") is not None:
                        tgt.add(int(faces[fi]["ordinal"]))
        out["target"] = sorted(tgt)
    if out["target"] and not out["context"] and edge_faces:
        ords = [f.get("ordinal") for f in faces]
        tset = set(out["target"])
        ring: set = set()
        for fs in edge_faces:
            os_ = {ords[fi] for fi in fs if ords[fi] is not None}
            if os_ & tset:
                ring |= os_ - tset
        out["context"] = sorted(int(o) for o in ring)
    return out


def _pinned(item: dict, part_bb: dict, subject, fill: float) -> dict:
    d = _unit(item.get("dir")) or _unit((1.0, 1.0, 0.8))
    extra = [list(p)[:3] for p in (item.get("fit_points") or ())]
    pts = [list(p)[:3] for p in subject] + extra
    cam = item.get("camera") or _fit_camera(pts, d, TILE_W, TILE_H, fill, part_bb)
    return dict(item, camera=cam, dir=list(d), label=item.get("label") or f"pinned {_az_el(d)}",
                section=item.get("section"))


def part_points(doc_id: str):
    """hull_points of the document's tessellation (the whole-part camera fit), or None."""
    mesh = cc._doc_mesh(doc_id)
    return hull_points(mesh["pos"]) if mesh is not None else None


def _view_summary(metrics: dict, v: int, roles: dict) -> dict:
    view = metrics["views"][v]
    faces = metrics.get("faces") or []
    scored = []
    for row in view.get("face_vis") or []:
        fi = int(row[0])
        if not 0 <= fi < len(faces):
            continue
        leg = float(row[2]) * float(_uf.legibility(float(row[3])))
        if leg > 0.0:
            scored.append((leg, fi, float(row[1]), float(row[3])))
    scored.sort(key=lambda s: (-s[0], s[1]))
    best = [{"sid": faces[fi].get("sid"), "ordinal": faces[fi].get("ordinal"),
             "legible_mm2": round(leg, 3), "mean_abs_cos": round(c, 3), "visible_px": int(px)}
            for leg, fi, px, c in scored[:6]]
    tv = _uf.target_visible_fraction(metrics, v, roles)
    return {"target_visible": None if tv is None else round(tv, 4), "best_faces": best,
            "normal_entropy": view.get("normal_entropy"),
            "principal_align": view.get("principal_align")}


def _whole_hidden_cuts(metrics: dict, part_bb: dict, whole) -> list:
    """Probe cuts through small or curved faces that no exterior camera reads legibly.
    The camera still frames the entire part rather than cropping into the hidden feature."""
    import numpy as np  # noqa: PLC0415
    faces = metrics.get("faces") or []
    if not faces or not metrics.get("views"):
        return []
    d = _uf._dense(metrics)
    cov = np.clip(d["legible_front"] / np.maximum(d["area"], 1e-9), 0, 1).max(axis=0)
    areas = d["area"][d["area"] > 0]
    typical = float(np.median(areas)) if areas.size else 1.0
    ranked = []
    for i, face in enumerate(faces):
        if cov[i] >= 0.35 or d["area"][i] < typical * 0.001:
            continue
        curved = int(face.get("surf_type") or 0) != 0
        if not curved and d["area"][i] > typical * 0.3:
            continue
        ranked.append((float((1 - cov[i]) * (2 if curved else 1) *
                             min(1.0, math.sqrt(d["area"][i] / max(typical, 1e-9)))), i))
    ranked.sort(reverse=True)
    out: list = []
    used = set()
    for _importance, fi in ranked:
        f = faces[fi]
        axis = _unit(f.get("axis")) or _unit(f.get("normal"))
        if axis is None:
            continue
        center = tuple(float(x) for x in f["centroid"])
        normal = _tangents(axis)[0]
        key = (tuple(round(x, 2) for x in center), tuple(round(abs(x), 2) for x in axis))
        if key in used:
            continue
        used.add(key)
        for sign in (1, -1):
            nn = tuple(sign * x for x in normal)
            # Tilt toward the nearest axial opening: a horizontal view of a
            # vertical bore hides its floor behind the section cap.
            main = max(range(3), key=lambda k: abs(axis[k]))
            towards = 1 if center[main] >= _bbox_center(part_bb)[main] else -1
            side = tuple(axis[k] * towards * (1 if axis[main] > 0 else -1)
                         for k in range(3))
            direction = _unit(tuple(nn[k] * 0.75 + side[k] * 0.66 for k in range(3)))
            out.append({"camera": _fit_camera(_subject(whole, part_bb), direction,
                                                TILE_W, TILE_H, _PART_FILL, part_bb),
                        "dir": list(direction), "label": f"section {_axis_word(nn)} {_az_el(direction)}",
                        "section": {"point": list(center), "normal": list(nn)},
                        "reveal_key": f.get("sid") or f"face:{fi}"})
        if len(out) >= 4:
            break
    return out


def _xray_candidates(doc_id: str, metrics: dict, candidates: list, roles: dict,
                     part_bb: dict, target_subject, matches=()) -> tuple:
    """Inspect occluded target triangles against a translucent shell. An x-ray
    needs the surrounding opening in frame; its fit is wider than a face close-up."""
    faces = metrics.get("faces") or []
    indices = {i for i, f in enumerate(faces) if f.get("ordinal") in roles["target"]}
    if not indices:
        return [], []
    axis = next((_unit(f.get("axis")) for j, f in enumerate(faces)
                 if j in indices and int(f.get("surf_type") or 0) in (1, 2, 4)
                 and _unit(f.get("axis")) is not None), None)
    if axis is None:
        axis = next((_unit(m.get("axis")) for m in matches
                     if m.get("kind") == "edge" and m.get("radius") is not None
                     and _unit(m.get("axis")) is not None), None)
    ranked = []
    for i, v in enumerate(metrics["views"]):
        if candidates[i].get("section"):
            continue
        rows = [row for row in v.get("face_vis") or [] if int(row[0]) in indices]
        proj = sum(float(row[4]) for row in rows)
        occluded = sum(max(0.0, float(row[4]) - float(row[1])) for row in rows)
        align = abs(_dot(axis, candidates[i]["dir"])) if axis else 0.0
        ranked.append((occluded * (1.0 - align * align) * (0.4 + align)
                       if axis else occluded, proj, i))
    ranked.sort(reverse=True)
    result = []
    for occluded, proj, i in ranked:
        d = candidates[i]["dir"]
        align = abs(_dot(axis, d)) if axis else 0.0
        if (occluded <= 0 or proj <= 0 or (axis and not 0.32 <= align <= 0.78)
                or (axis and abs(axis[2]) > 0.9 and
                    min(abs(d[0]), abs(d[1])) < 0.25) or any(
                _dot(d, c["dir"]) > 0.8 for c in result)):
            continue
        c = dict(candidates[i], section=None, xray=True, reveal_key="target",
                 label=f"x-ray {_az_el(candidates[i]['dir'])}")
        c["camera"] = _fit_camera(target_subject, c["dir"], TILE_W, TILE_H,
                                  0.45, part_bb)
        result.append(c)
        if len(result) >= 4:
            break
    if not result:
        return [], []
    measured = _measure(doc_id, result, int(metrics.get("width") or METRICS_W),
                        int(metrics.get("height") or METRICS_H))["views"]
    for v in measured:
        for row in v.get("face_vis") or []:
            if int(row[0]) in indices and row[4] > 0:
                # This camera rasterizes the target alone in its full-size tile;
                # projected pixels bound its depth-visible footprint.
                row[1] = max(row[1], row[4] * 0.7)
                row[2] = max(row[2], float(faces[int(row[0])]["area_mm2"]) * 0.55)
    return result, measured


def _readability_tiers(metrics: dict, candidates: list, roles: dict, subject,
                       matches=(), *, targeted: bool = False) -> tuple:
    """Progressively relaxed, mode-independent readable footprints.

    Pixels alone admit a foreshortened wall; true area alone admits a sub-pixel
    sliver. Both must pass, and a section cap never substitutes for a real surface.
    """
    import numpy as np  # noqa: PLC0415
    d = _uf._dense(metrics)
    face_ids = [d["ord_index"][o] for o in roles.get("target", ()) if o in d["ord_index"]]
    ids = face_ids if targeted and face_ids else list(range(d["F"]))
    px = d["vis_front"][:, ids].sum(axis=1) / d["frame_px"]
    area = d["legible_front"][:, ids].sum(axis=1) / max(float(d["area"][ids].sum()), 1e-9)
    projected = d["proj_px"][:, ids].sum(axis=1)
    visible = d["vis_front"][:, ids].sum(axis=1) / np.maximum(projected, 1.0)
    axis_ids = set(ids)
    if targeted:
        axis_ids.update(d["ord_index"][o] for o in roles.get("context", ())
                        if o in d["ord_index"])
    axes = [_unit(d["axis"][fi]) for fi in axis_ids if d["revolved"][fi]]
    axes += [_unit(m.get("axis")) for m in matches
             if m.get("kind") == "edge" and m.get("radius") is not None]
    axes = [a for a in axes if a is not None]
    stare = np.asarray([max((abs(_dot(c["dir"], a)) for a in axes), default=0.0)
                        for c in candidates])
    mean_cos = (d["vis_front"][:, ids] * d["cos"][:, ids]).sum(axis=1) / np.maximum(
        d["vis_front"][:, ids].sum(axis=1), 1.0)
    section_stare = np.asarray([
        max((abs(_dot(c["section"]["normal"], a)) for a in axes), default=0.0)
        if c.get("section") else 0.0 for c in candidates])
    # Project the actual subject: a plate's box must not earn a full-size
    # footprint when its own vertices are seen edge-on.
    P = np.asarray(subject, dtype=np.float64)
    extents, major_extents = [], []
    for c in candidates:
        cam = c["camera"]
        forward = _unit(np.subtract(cam["target"], cam["position"]))
        right = _unit(_cross(forward, cam["up"]))
        up = _cross(right, forward)
        Q = P - np.asarray(cam["position"])
        z = np.maximum(Q @ np.asarray(forward), 1e-9)
        scale = TILE_H / (2 * math.tan(math.radians(cam["vertical_fov_deg"]) * 0.5))
        spans = (float(np.ptp((Q @ right) / z) * scale),
                 float(np.ptp((Q @ up) / z) * scale))
        extents.append(min(spans))
        major_extents.append(max(spans))
    # A partially occluded target may have a broad projected box but only a
    # hairline of visible pixels. Area divided by its long extent bounds the
    # average visible thickness, catching caps that hide all but a narrow band.
    extent = np.minimum(np.asarray(extents), px * TILE_W * TILE_H /
                        np.maximum(np.asarray(major_extents), 1.0))
    cap_share = d["cap"] / np.maximum(d["fg"], 1.0)
    tiers = np.full(len(candidates), 4, dtype=np.int64)
    needs_profile = targeted and any(
        metrics["faces"][fi].get("concavity") == "bore" for fi in ids)
    plain = np.asarray([not c.get("section") and not c.get("xray") for c in candidates])
    wall_cap = None
    if targeted and len(ids) == 1 and d["surf"][ids[0]] == 0:
        normal = _unit(d["normal"][ids[0]])
        if normal is not None:
            # A cut coincident with a planar target paints a cap over the very
            # wall being inspected. Prefer cuts crossing that wall, not along it.
            wall_cap = np.asarray([
                abs(_dot(c["section"]["normal"], normal)) if c.get("section") else 0.0
                for c in candidates])
    for level, (min_px, min_area, min_extent, max_stare, max_align, max_cap) in enumerate((
            (0.02 if targeted else 0.07, 0.18 if targeted else 0.12, 28, 0.65, 0.95, 0.65),
            (0.012 if targeted else 0.04, 0.10 if targeted else 0.08, 20, 0.77, 0.97, 0.8),
            (0.005 if targeted else 0.02, 0.04, 16, 0.88, 0.99, 0.9),
            (0.001, 0.01, 8, 0.95, 1.01, 0.98))):
        ok = ((px >= min_px) & (area >= min_area) & (extent >= min_extent) &
              (stare <= max_stare) & (d["palign"] <= max_align) &
              (cap_share <= max_cap) & (mean_cos >= (0.40 if level < 2 else 0.30)) &
              ((section_stare <= (0.3 if level < 2 else 0.85)) if targeted else True))
        if not targeted and level < 2:
            # A thin part can exceed an absolute pixel floor edge-on. Compare
            # geometric spans here: a cut legitimately keeps less exterior,
            # whose visible thickness is already bounded by min_extent.
            spans = np.asarray(extents)
            ok &= spans >= float(spans.max()) * (0.35 if level == 0 else 0.25)
        if wall_cap is not None:
            ok &= wall_cap <= (0.8 if level < 2 else 0.98)
        if targeted:
            ok &= visible >= 0.18
            if needs_profile and level < 2:
                ok &= ~plain | ((stare <= (0.4 if level == 0 else 0.55)) & (visible >= 0.55))
        tiers[ok & (tiers == 4)] = level
    return tiers.tolist(), face_ids


def plan_shots(doc_id: str, *, target=None, roles: Optional[dict] = None, k: int = 4,
               mode: str = "auto", pinned: Optional[list] = None,
               part_bbox: Optional[dict] = None) -> dict:
    """Plan the k most useful complementary shots of the part (or of `target`).

    target: None (whole part), a resolved {"matches", "label", "bbox"[, "kind"]} dict, or any
    _resolve_inspect_target spec (feature id/name, sid, geometry query, or a list of them).
    roles: {"target", "context", "bc"} doc_query face ordinals; by default the target's faces
    and the faces sharing an edge with them. mode: "auto" (trained model when its weights load
    and match the feature schema, else hand_utility), "model" (refuse without it), "hand".
    pinned: views forced into the set first ({"dir", "label", "section"?, "camera"?,
    "fit_points"?, ...}; extra keys ride through to the plan's view). part_bbox: the caller's
    doc_bbox answer for this revision (doc_bbox is ~0.25 s on a dense part), else it is asked.

    When the target is internal, or no uncut view sees more than half of it, section cuts
    through the target join the candidates. Returns {"views": [{camera, dir, label, section,
    utility, marginal_gain, summary, ...}], "model", "model_note", "coverage", "unseen",
    "candidates_scored", "roles", "target", "internal", "sectioned", "part_bbox", "metrics"}."""
    part_bb = part_bbox or cc.cb.call("doc_bbox", {"doc_id": doc_id}) or {}
    if not part_bb.get("size"):
        raise ValueError("plan_shots: the document has no geometry to frame yet")
    model, model_name, note = _choose_model(mode)
    if target is not None and not (isinstance(target, dict) and "matches" in target):
        prime_face_clouds(doc_id)       # resolving a spec runs the face attribution
    tgt = _resolve_target(doc_id, target)
    matches = tgt["matches"] if tgt else []
    internal = bool(matches) and cc._target_is_internal(matches, part_bb)
    fit_bb = tgt["bbox"] if tgt else part_bb
    normals = [(m.get("axis") if m.get("surf") in ("cylinder", "cone", "torus")
                else m.get("normal")) or m.get("axis") for m in matches]
    normals = [n for n in normals if isinstance(n, (list, tuple))]
    pts = target_points(doc_id, matches)
    whole = None if tgt else part_points(doc_id)
    subject = _subject(pts, fit_bb) if tgt else _subject(whole, part_bb)
    pins = [_pinned(p, part_bb, subject, _TARGET_FILL if tgt else _PART_FILL)
            for p in (pinned or [])]
    cands = pins + candidate_cameras(part_bb, target_bbox=tgt["bbox"] if tgt else None,
                                     target_normals=normals, target_points=pts,
                                     part_points=whole, internal=internal)
    if internal and tgt:
        cands += _diagonal_section_candidates(part_bb, tgt["bbox"], normals, pts)
    metrics = view_metrics(doc_id, cands)
    roles = _roles(metrics, matches, roles)
    sectioned = internal
    if tgt and not internal and roles["target"]:
        seen = max((_uf.target_visible_fraction(metrics, v, roles) or 0.0)
                   for v in range(len(cands)))
        whole_axes, _ = _symmetry(metrics)
        planar_cap = bool(whole_axes) and all(
            m.get("surf") == "plane" and any(
                abs(_dot(_unit(m.get("normal")) or (0, 0, 0), a)) > 0.99
                for a in whole_axes) for m in matches)
        if seen < _SEE_ENOUGH or planar_cap:
            extra = section_candidates(part_bb, tgt["bbox"], normals, target_points=pts)
            extra += _diagonal_section_candidates(part_bb, tgt["bbox"], normals, pts)
            if planar_cap:
                # Rotating around an end cap cannot create four different
                # profiles. A shallower cut complements the existing 45-degree
                # cut with a different cap/wall proportion.
                t0, t1 = _tangents(whole_axes[0])
                a = math.radians(22.5)
                n = tuple(math.cos(a) * t0[q] + math.sin(a) * t1[q] for q in range(3))
                face_normal = _unit(matches[0].get("normal"))
                direction = _unit(tuple(math.sqrt(0.75) * n[q] + 0.5 * face_normal[q]
                                        for q in range(3)))
                extra.append({
                    "camera": _fit_camera(subject, direction, TILE_W, TILE_H,
                                          _SECTION_FILL, part_bb),
                    "dir": list(direction),
                    "label": f"section cap profile {_az_el(direction)}",
                    "section": {"point": list(_bbox_center(tgt["bbox"])),
                                "normal": list(n)},
                })
            _extend(doc_id, metrics, extra)
            cands += extra
            sectioned = True
    if tgt and sectioned and roles["target"]:
        for c, v in zip(cands, metrics["views"]):
            if c.get("section"):
                c["reveal_key"] = v["reveal_key"] = "target"
        transparent, transparent_views = _xray_candidates(
            doc_id, metrics, cands, roles, part_bb, subject, matches)
        cands.extend(transparent)
        metrics["views"].extend(transparent_views)
    if not tgt:
        extra = _whole_hidden_cuts(metrics, part_bb, whole)
        if extra:
            _extend(doc_id, metrics, extra)
            cands += extra
            sectioned = True
    tiers, face_ids = _readability_tiers(metrics, cands, roles, subject, matches,
                                        targeted=bool(tgt))
    uninformative = None
    mandatory = list(range(len(pins)))
    if not tgt and sectioned:
        import numpy as np  # noqa: PLC0415
        d = _uf._dense(metrics)
        secs = [i for i, c in enumerate(cands) if c.get("section")]
        curved = (d["surf"] != 0) & (d["area"] > 0)
        if secs and curved.any():
            exterior = [i for i, c in enumerate(cands) if not c.get("section")]
            plain = (d["legible_front"][exterior] / np.maximum(d["area"], 1e-9)).max(axis=0)
            hidden = curved & (plain < 0.35)
            if hidden.any():
                rich = (d["legible_front"][:, hidden] /
                        np.maximum(d["area"][hidden], 1e-9)).sum(axis=1)
                uninformative = [not c.get("section") and rich[i] < 0.015
                                 for i, c in enumerate(cands)]
                section_scores = [
                    float((d["legible"][i, hidden] / np.maximum(d["area"][hidden], 1e-9)
                           * (1 - plain[hidden])).sum()) for i in secs]
                readable_sections = [p for p, i in enumerate(secs) if tiers[i] <= 1]
                if readable_sections:
                    best_pos = max(readable_sections, key=lambda p: section_scores[p])
                    if section_scores[best_pos] > 0.05:
                        mandatory.append(secs[best_pos])
    context = _uf.context_from(metrics, roles=roles,
                               target_kind=tgt["kind"] if tgt else "part",
                               internal=internal, part_bbox=part_bb)
    score = set_scorer(metrics, roles, context, model)
    picks = greedy_select(metrics, score, max(int(k), len(mandatory)),
                          pinned=mandatory, eligibility_tiers=tiers, guarantee_k=True,
                          uninformative=uninformative, target_faces=face_ids if tgt else None)
    chosen = [p["index"] for p in picks]
    views = [dict(cands[p["index"]], utility=p["utility"], marginal_gain=p["marginal_gain"],
                  metrics_index=p["index"], summary=_view_summary(metrics, p["index"], roles))
             for p in picks]
    faces = metrics.get("faces") or []
    seen_mask = _uf.seen_faces(metrics, chosen)
    unseen = sorted((i for i in range(len(faces)) if not seen_mask[i]),
                    key=lambda i: (-float(faces[i].get("area_mm2") or 0.0), i))
    return {
        "views": views,
        "model": model_name,
        "model_note": note,
        "coverage": _uf.set_coverage(metrics, chosen, roles),
        "unseen": [faces[i].get("sid") or f"face#{faces[i].get('topo')}" for i in unseen],
        "candidates_scored": len(metrics.get("views") or []),
        "roles": roles,
        "target": ({"matches": matches, "label": tgt["label"], "bbox": tgt["bbox"],
                    "kind": tgt["kind"]} if tgt else None),
        "internal": internal,
        "sectioned": sectioned,
        "part_bbox": part_bb,
        "metrics": metrics,
    }


# ------------------------------------------------------------------------------ rendering
def draw_triad(draw, frame: dict, H: int) -> None:
    """World X/Y/Z triad bottom-left, through the render's own camera basis."""
    ox, oy, L = 26, H - 26, 18
    for axis, rgb, name in (((1, 0, 0), (220, 80, 80), "X"), ((0, 1, 0), (95, 185, 95), "Y"),
                            ((0, 0, 1), (90, 140, 225), "Z")):
        su = sum(axis[i] * frame["r"][i] for i in range(3))
        sv = sum(axis[i] * frame["u"][i] for i in range(3))
        ex, ey = ox + su * L, oy - sv * L
        draw.line([(ox, oy), (ex, ey)], fill=rgb, width=2)
        draw.text((ex + (3 if su >= 0 else -9), ey - 6), name, fill=rgb)


def _letter(j: int) -> str:
    return chr(ord("A") + j) if j < 26 else f"T{j + 1}"


def _reserved_rects(W: int, H: int) -> list:
    """Tile corners the letter badge and the triad own — tags route around them (the scale
    plate registers its own measured rect in cad_common._annotate_render)."""
    return [(0, 0, 44, 38), (0, H - 58, 62, H)]


def _stamp_tile(png: str, letter: str, frame: Optional[dict]) -> None:
    from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    with PILImage.open(png) as im0:
        im = im0.convert("RGB")
    draw = ImageDraw.Draw(im)
    font = cc._annot_font(22)
    draw.rectangle([4, 4, 36, 34], fill=(18, 19, 22), outline=(235, 240, 248))
    try:
        bb = draw.textbbox((0, 0), letter, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text((20 - tw / 2 - bb[0], 19 - th / 2 - bb[1]), letter, font=font,
                  fill=(250, 250, 252))
    except Exception:  # noqa: BLE001 — bitmap font without textbbox
        draw.text((14, 12), letter, fill=(250, 250, 252))
    if frame is not None and frame.get("r") is not None and frame.get("u") is not None:
        draw_triad(draw, frame, im.size[1])
    im.save(png, format="PNG")


def _compose(tiles: list, out_png: str) -> str:
    from PIL import Image as PILImage  # noqa: PLC0415
    imgs = [PILImage.open(p).convert("RGB") for p in tiles]
    try:
        tw, th = imgs[0].size
        n = len(imgs)
        cols = 1 if n == 1 else (2 if n <= 4 else 3)
        rows = (n + cols - 1) // cols
        gap = 4
        sheet = PILImage.new("RGB", (tw * cols + gap * (cols - 1), th * rows + gap * (rows - 1)),
                             (30, 30, 32))
        for i, im in enumerate(imgs):
            r, c = divmod(i, cols)
            sheet.paste(im.resize((tw, th)), (c * (tw + gap), r * (th + gap)))
        sheet.save(out_png, format="PNG")
        return out_png
    finally:
        for im in imgs:
            im.close()


def _tag_priority(roles: Optional[dict]) -> dict:
    prio: dict = {}
    for key, rank in reversed(_ROLE_RANK):
        for o in (roles or {}).get(key) or ():
            prio[int(o) + 1] = rank
    return prio


def prime_face_clouds(doc_id: str) -> None:
    """Fill the attribution's {sid: world points} memo from the display mesh already in hand.

    _face_attribution's footprint test asks doc_face_points for each cap/wall it judges; on a
    dense spline part that is ~0.3 s a face (4.7 s for the holster's legend). The same
    triangulation nodes are the display mesh's vertices of that face, and the memo is keyed on
    the same revision token as the mesh, so this is the same answer without the round trips."""
    import numpy as np  # noqa: PLC0415
    mesh = cc._doc_mesh(doc_id)
    if mesh is None:
        return
    memo = cc._face_cloud_memo(doc_id)
    face_by_topo = cc._mesh_topo_index(mesh)[0]
    for t, m in enumerate(cc._doc_topo_maps(doc_id)["face"], start=1):
        sid, fi = m.get("sid"), face_by_topo.get(t)
        if not sid or fi is None or sid in memo:
            continue
        fr = mesh["faces"][fi]
        tris = mesh["idx"][fr["first_tri"]:fr["first_tri"] + fr["tri_count"]]
        if len(tris):
            memo[sid] = [tuple(p) for p in mesh["pos"][np.unique(tris)].tolist()]


def _face_owners(doc_id: str) -> dict:
    """topo id -> "f3 Boss": the feature that CREATED the face (describe_part's attribution).
    Empty when the feature tree cannot be read; the legend then omits the owner."""
    try:
        prime_face_clouds(doc_id)
        feats, owner_map = cc._target_feature_roster(doc_id)
        att = cc._face_attribution(doc_id, feats, owner_map)
    except Exception:  # noqa: BLE001
        return {}
    topo_of = {id(m): i + 1 for i, m in enumerate(att.get("faces") or [])}
    names = {f.get("id"): (f.get("name") or f.get("type") or "") for f in feats}
    out: dict = {}
    for fid, ms in (att.get("created") or {}).items():
        for m in ms or ():
            t = topo_of.get(id(m))
            if t is not None:
                out[t] = f"{fid} {names.get(fid) or ''}".strip()
    return out


def _dir_word(m: dict) -> str:
    surf = str(m.get("surf") or "")
    if surf in ("cylinder", "cone", "torus") and _unit(m.get("axis")) is not None:
        return "axis " + _axis_word(_unit(m["axis"])).lstrip("+")
    side = str(m.get("side") or "").lower()
    if side in _SIDE_WORD:
        return _SIDE_WORD[side]
    n = _unit(m.get("normal"))
    return _axis_word(n) if n is not None else ""


def _face_dims(mesh: Optional[dict], face_by_topo: dict, t: int, m: dict) -> str:
    if mesh is None or t not in face_by_topo:
        return ""
    import numpy as np  # noqa: PLC0415
    fr = mesh["faces"][face_by_topo[t]]
    tris = mesh["idx"][fr["first_tri"]:fr["first_tri"] + fr["tri_count"]]
    if not len(tris):
        return ""
    pts = mesh["pos"][np.unique(tris.reshape(-1))]
    surf = str(m.get("surf") or "")
    ax = _unit(m.get("axis"))
    try:
        r = float(m.get("radius"))
    except (TypeError, ValueError):
        r = 0.0
    if surf in ("cylinder", "cone") and ax is not None and r > 0:
        length = float(np.ptp(pts @ np.asarray(ax)))
        return f"Ø{2 * r:.3g} x {length:.3g} mm"
    n = _unit(m.get("normal"))
    if surf == "plane" and n is not None:
        u, v = _tangents(n)
        return (f"{float(np.ptp(pts @ np.asarray(u))):.3g}x"
                f"{float(np.ptp(pts @ np.asarray(v))):.3g} mm")
    ext = sorted(np.ptp(pts, axis=0).tolist(), reverse=True)
    return f"{ext[0]:.3g}x{ext[1]:.3g}x{ext[2]:.3g} mm"


def _legend_line(sid: str, m: dict, dims: str, owner: str) -> str:
    bits = [b for b in (str(m.get("surf") or "face"), _dir_word(m), dims, owner) if b]
    return f"{sid} — " + ", ".join(bits)


def _vec_text(v) -> str:
    return "[" + ", ".join(f"{float(x):.2f}" for x in v[:3]) + "]"


def _overlay_problems(ann: Optional[dict], *, annotate: bool, section: bool,
                      emphasis: bool) -> list:
    """What a tile's overlay promised and did not draw, in words for the caption. Empty when
    the overlay was not asked for or delivered everything asked of it."""
    if not (annotate or emphasis):
        return []
    if ann is None:
        return ["overlay did not run"]
    note = str(ann.get("note") or "").strip()
    if not ann.get("available"):
        return [note or "overlay failed"]
    out: list = []
    if not ann.get("hlr"):
        out.append(note or "no depth pass: no edges, tags, tint or emphasis mask")
        return out
    if annotate and not section and not ann.get("edges"):
        out.append("no B-rep edge was drawn")
    if annotate and not ann.get("tags"):
        out.append("no face got a SID tag")
    emp = ann.get("emphasis") or {}
    if emp.get("note"):
        out.append(str(emp["note"]))
    return out


def render_shot_grid(doc_id: str, plan: dict, tile: tuple = (TILE_W, TILE_H), *,
                     highlight=None, emphasis_mode: Optional[str] = "emphasis",
                     annotate: bool = True, background=None,
                     decorate: Optional[Callable] = None,
                     stem: Optional[str] = None) -> tuple:
    """Render `plan["views"]` as ONE image: one tile per view (1 -> single tile, 2-4 -> 2
    columns, more -> 3), lettered A, B, C... left-to-right, top-to-bottom.

    Every tile is an exact perspective render of the planned camera at `tile` size; a
    section view renders the engine's cut (ghosted removed half, hatched cap) and skips the
    Python hidden-line overlay since the engine draws the ghost's edges itself. With a target
    (plan["target"]["matches"]) the tile carries the close-up emphasis overlay (target painted
    with its shading, context desaturated, occluded target ghosted). With `annotate`, up to 8
    faces per tile get a SID tag — target faces first, then fixture/load, then the context
    ring, then the largest visible — on a leader to the face's largest visible pixel cluster;
    tags never overlap each other or the letter/triad/scale-bar corners. `decorate(png,
    frame, view)` paints extra evidence on a tile before it is stamped.

    Returns (png_path, info): info["caption"] is the self-sufficient text (per tile: camera
    direction, section or not, target visible %, best-seen faces and their features; union
    coverage; faces no tile sees, by SID; the tag legend; the ranking model), info["tiles"]
    the per-tile records, info["palette"] the emphasis palette, info["unseen"] the SIDs."""
    W, H = int(tile[0]), int(tile[1])
    views = list(plan.get("views") or [])
    if not views:
        raise ValueError("render_shot_grid: the plan has no views")
    tgt = plan.get("target") or None
    matches = list((tgt or {}).get("matches") or [])
    label = str((tgt or {}).get("label") or "")
    palette = (cc._build_emphasis_palette(matches, mode=emphasis_mode)
               if matches and emphasis_mode else None)
    prio = _tag_priority(plan.get("roles"))
    tiles: list = []
    records: list = []
    for j, v in enumerate(views):
        letter = _letter(j)
        cam = {k: v["camera"][k] for k in _CAM_KEYS if k in v["camera"]}
        cam["width"], cam["height"] = W, H
        sec = v.get("section")
        png, frame, _r = cc._render_view_png(
            doc_id, v["dir"], cc._tmp(".png"), name=f"{letter} {v.get('label') or ''}"[:28],
            camera=cam, annotate=False, background=background, section=sec)
        emphasis = ({"matches": matches, "mode": emphasis_mode, "label": label,
                     "palette": palette, "draw_label": False,
                     "xray": bool(v.get("xray"))} if palette is not None else None)
        ann = None
        if annotate or emphasis is not None or highlight is not None:
            png, ann = cc._annotate_render(
                png, doc_id, tuple(v["dir"]), highlight=highlight, callouts=False,
                edges=bool(annotate) and not sec, scale=bool(annotate), frame=frame,
                emphasis=emphasis, out_png=cc._tmp(".png"),
                face_tags=({"max": _TAG_MAX, "priority": prio,
                            "min_px": int(_TAG_MIN_FRAC * W * H)} if annotate else None),
                reserved=_reserved_rects(W, H), tint=bool(annotate))
        if decorate is not None:
            decorate(png, frame, v)
        _stamp_tile(png, letter, frame)
        tiles.append(png)
        summ = v.get("summary") or {}
        records.append({"letter": letter, "label": v.get("label"), "dir": list(v["dir"]),
                        "section": sec, "target_visible": summ.get("target_visible"),
                        "best_faces": [b.get("sid") for b in summ.get("best_faces") or []
                                       if b.get("sid")],
                        "tags": list((ann or {}).get("tags") or []),
                        "seen_topos": (ann or {}).get("seen_topos"),
                        "annotation": ann,
                        "overlay_problems": _overlay_problems(
                            ann, annotate=bool(annotate), section=bool(sec),
                            emphasis=emphasis is not None or highlight is not None)})
    out = cc.render_output_path(
        stem or f"{(cc._handoff_doc_name(doc_id) or '').strip() or doc_id}-shots", ".png")
    _compose(tiles, out)

    topo = cc._doc_topo_maps(doc_id)
    owners = _face_owners(doc_id)
    face_sid_topo = topo["face_sid"]
    if all(r["seen_topos"] is not None for r in records):
        seen = {int(t) for r in records for t in r["seen_topos"]}
        unseen = [str(m.get("sid") or f"face#{i + 1}") for i, m in enumerate(topo["face"])
                  if (i + 1) not in seen]
        unseen_basis = f"measured on the {W}x{H} tiles"
    else:
        unseen = [str(s) for s in plan.get("unseen") or []]
        unseen_basis = "measured by the planner"

    def _owned(sid: str) -> str:
        own = owners.get(face_sid_topo.get(sid))
        return f"{sid} ({own})" if own else sid

    letters = [r["letter"] for r in records]
    what = f"{label!r}" if tgt else "the whole part"
    bb = plan.get("part_bbox") or {}
    size = bb.get("size") or ()
    parts = [f"SHOT GRID of {what}: {len(records)} planner-chosen perspective view"
             f"{'s' if len(records) != 1 else ''}, tiles {W}x{H} lettered "
             f"{letters[0]}-{letters[-1]} left-to-right, top-to-bottom, each with a world "
             f"X/Y/Z triad bottom-left."]
    broken = [f"tile {r['letter']}: {'; '.join(r['overlay_problems'])}" for r in records
              if r["overlay_problems"]]
    if broken:
        parts.append("OVERLAY FAILED — these tiles are missing annotation the grid promises, "
                     "so do not read absent edges/tags/emphasis as absent geometry: "
                     + " | ".join(broken) + ".")
    if len(size) >= 3:
        parts.append(f"Part bbox {float(size[0]):.4g} x {float(size[1]):.4g} x "
                     f"{float(size[2]):.4g} mm.")
    model = str(plan.get("model") or "hand")
    note = str(plan.get("model_note") or "")
    parts.append(f"Views ranked by {model}" + (f" ({note})" if note else "")
                 + f" over {plan.get('candidates_scored') or 0} measured candidates.")
    cov = plan.get("coverage") or {}
    if cov.get("all") is not None:
        extra = [f"{k} {cov[k] * 100:.0f}%" for k in ("target", "context", "bc")
                 if cov.get(k) is not None]
        parts.append(f"Union legible coverage: {cov['all'] * 100:.0f}% of the surface area"
                     + (f" ({', '.join(extra)})" if extra else "") + ".")
    for r in records:
        bits = [f"dir {_vec_text(r['dir'])} ({r['label']})"]
        if r["section"]:
            bits.append(f"SECTION through {_vec_text(r['section']['point'])} normal "
                        f"{_vec_text(r['section']['normal'])}: cut face hatched, removed half "
                        f"ghosted")
        else:
            bits.append("exterior view")
        if r["target_visible"] is not None:
            bits.append(f"target visible {r['target_visible'] * 100:.0f}%")
        if r["best_faces"]:
            bits.append("best seen: " + ", ".join(_owned(s) for s in r["best_faces"][:4]))
        tags = [t.get("text", t["sid"]) for t in r["tags"]]
        if tags:
            bits.append("tagged: " + ", ".join(tags))
        parts.append(f"{r['letter']}: " + "; ".join(bits) + ".")
    if unseen:
        parts.append(f"Faces NO tile sees ({len(unseen)}, {unseen_basis}): "
                     + ", ".join(unseen[:16]) + (f" +{len(unseen) - 16} more" if
                                                 len(unseen) > 16 else "")
                     + " — inspect_section or render_part(target=<sid>) to see them.")
    else:
        parts.append(f"Every face is visible in at least one tile ({unseen_basis}).")

    legend: list = []
    tagged: list = []
    tag_text = {}
    for r in records:
        for t in r["tags"]:
            if t["sid"] not in tagged:
                tagged.append(t["sid"])
            tag_text[t["sid"]] = t.get("text", t["sid"])
    if tagged:
        mesh = cc._doc_mesh(doc_id)
        face_by_topo = cc._mesh_topo_index(mesh)[0] if mesh is not None else {}
        for sid in tagged:
            t = face_sid_topo.get(sid)
            m = topo["face"][t - 1] if t else {}
            legend.append(_legend_line(tag_text[sid], m, _face_dims(mesh, face_by_topo, t, m),
                                       owners.get(t, "")))
        parts.append("Tags (the SIDs query_geometry / add_features refs accept): "
                     + " | ".join(legend) + ".")
    info = {"tiles": records, "caption": " ".join(parts), "palette": palette,
            "legend": legend, "unseen": unseen, "model": model, "model_note": note,
            "annotations": [r["annotation"] for r in records],
            "overlay_problems": {r["letter"]: r["overlay_problems"] for r in records
                                 if r["overlay_problems"]}}
    return out, info
