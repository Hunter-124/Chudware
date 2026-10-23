"""View-SET features for the ViewNet utility model (numpy only).

This module is the ONE source of utility features: the planner scores candidate sets with it at
inference, and any trained weights must be produced from the same tensors, so the two can never
drift. Anything that changes a feature's meaning bumps FEATURE_SCHEMA, and a model trained
against another schema is refused (model.UtilityModel.load).

Input is the measured `doc_view_metrics` response as returned by
services.mcp.shot_planner.view_metrics: a doc-wide face table (joined to doc_query ordinals),
an edge table, `edge_faces` (edge index -> adjacent face indices, may be absent) and one
record per candidate view (face_vis / edge_vis rows, per-view scalars, plus the planner's
`dir` and `section`). A view SET is a list of view indices; every feature aggregates over the
set, which is what makes the utility set-valued: a face counts as seen once any view in the
set shows it legibly, so a near-duplicate view adds nothing and a complementary one adds the
faces only it can see.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

FEATURE_SCHEMA = "viewutil.features/1"
DF = 32
DG = 24
DC = 16
QUESTION_KINDS = ("count", "presence_side", "through_blind", "type", "dimension_ratio",
                  "general")
TARGET_KINDS = ("part", "face", "edge", "feature", "point", "set")

N_SURF = 7                   # AICM surf_type: 0 plane .. 5 bspline, 6 other
_REVOLVED = (1, 2, 4)        # cylinder, cone, torus: faces with an axis to stare down
_ENTROPY_MAX = math.log2(20.0)
# |cos| of the view ray against the surface below which a face is too foreshortened to read.
# A soft ramp around ~0.3 rather than a step, so a view does not flip between "seen" and
# "unseen" on a hair of rotation.
_LEGIBLE_LO, _LEGIBLE_HI = 0.2, 0.4
_CONTRAST_FULL = 0.25        # shade difference (0..1) that reads as a crisp edge


def legibility(cos):
    """0..1 weight of how readable a surface seen at mean |cos| is (0 below ~0.2, 1 above
    ~0.4) — the "legible" in legible coverage."""
    return np.clip((cos - _LEGIBLE_LO) / (_LEGIBLE_HI - _LEGIBLE_LO), 0.0, 1.0)


def _num(v, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _vec3(v) -> list:
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return [_num(v[0]), _num(v[1]), _num(v[2])]
    return [0.0, 0.0, 0.0]


def _unit_rows(a):
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return np.where(n > 1e-12, a / np.maximum(n, 1e-12), 0.0)


# Dense per-view matrices are rebuilt only when a metrics dict is new or has grown (the
# planner appends section candidates to the same dict). Keyed by identity, bounded, and kept
# OUT of the dict so a caller can still serialise the metrics it was handed.
_CACHE: list = []


def _dense(metrics: dict) -> dict:
    n_views = len(metrics.get("views") or [])
    for m, d in _CACHE:
        if m is metrics and d["V"] == n_views:
            return d
    d = _build_dense(metrics)
    _CACHE[:] = [(m, dd) for m, dd in _CACHE if m is not metrics][-3:] + [(metrics, d)]
    return d


def _build_dense(metrics: dict) -> dict:
    faces = metrics.get("faces") or []
    edges = metrics.get("edges") or []
    views = metrics.get("views") or []
    F, E, V = len(faces), len(edges), len(views)
    W = max(int(_num(metrics.get("width"), 192.0)), 1)
    H = max(int(_num(metrics.get("height"), 144.0)), 1)

    area = np.asarray([max(_num(f.get("area_mm2")), 0.0) for f in faces], dtype=np.float64)
    surf = np.clip(np.asarray([int(_num(f.get("surf_type"))) for f in faces], dtype=np.int64),
                   0, N_SURF - 1)
    normal = np.asarray([_vec3(f.get("normal")) for f in faces], dtype=np.float64).reshape(F, 3)
    centroid = np.asarray([_vec3(f.get("centroid")) for f in faces],
                          dtype=np.float64).reshape(F, 3)
    axis = _unit_rows(np.asarray([_vec3(f.get("axis")) for f in faces],
                                 dtype=np.float64).reshape(F, 3))
    axis[~np.isin(surf, _REVOLVED)] = 0.0
    tri = np.asarray([max(_num(f.get("tri_count")), 0.0) for f in faces], dtype=np.float64)
    ord_index: dict = {}
    for i, f in enumerate(faces):
        o = f.get("ordinal")
        if o is not None:
            ord_index[int(o)] = i
    edge_len = np.asarray([max(_num(e.get("length_mm")), 0.0) for e in edges],
                          dtype=np.float64)

    vis_px = np.zeros((V, F)); proj_px = np.zeros((V, F))
    vis_area = np.zeros((V, F)); cos = np.zeros((V, F))
    e_vis = np.zeros((V, E)); e_len = np.zeros((V, E))
    e_con = np.zeros((V, E)); e_sil = np.zeros((V, E))
    dirs = np.zeros((V, 3))
    is_sec = np.zeros(V, dtype=bool)
    entropy = np.zeros(V); palign = np.zeros(V); bg = np.zeros(V); cap = np.zeros(V)
    depth_edge = np.zeros(V); fg = np.zeros(V)
    for v, view in enumerate(views):
        rows = view.get("face_vis") or []
        if len(rows):
            a = np.asarray(rows, dtype=np.float64).reshape(-1, 5)
            ii = a[:, 0].astype(np.int64)
            ok = (ii >= 0) & (ii < F)
            ii, a = ii[ok], a[ok]
            vis_px[v, ii] = a[:, 1]
            vis_area[v, ii] = a[:, 2]
            cos[v, ii] = a[:, 3]
            proj_px[v, ii] = a[:, 4]
        rows = view.get("edge_vis") or []
        if len(rows):
            a = np.asarray(rows, dtype=np.float64).reshape(-1, 5)
            ii = a[:, 0].astype(np.int64)
            ok = (ii >= 0) & (ii < E)
            ii, a = ii[ok], a[ok]
            e_vis[v, ii] = a[:, 1]
            e_len[v, ii] = a[:, 2]
            e_con[v, ii] = a[:, 3]
            e_sil[v, ii] = a[:, 4]
        dirs[v] = _vec3(view.get("dir"))
        is_sec[v] = bool(view.get("section"))
        entropy[v] = _num(view.get("normal_entropy"))
        palign[v] = _num(view.get("principal_align"))
        bg[v] = _num(view.get("bg_px"))
        cap[v] = _num(view.get("cap_px"))
        depth_edge[v] = _num(view.get("depth_edge_px"))
        fg[v] = _num(view.get("fg_px"))
    dirs = _unit_rows(dirs)

    pe, pf = [], []
    for ei, fs in enumerate(metrics.get("edge_faces") or []):
        if ei >= E:
            break
        for fi in fs or ():
            if 0 <= int(fi) < F:
                pe.append(ei)
                pf.append(int(fi))
    pe = np.asarray(pe, dtype=np.int64)
    pf = np.asarray(pf, dtype=np.int64)

    edge_q = (e_vis * np.maximum(np.minimum(e_con / _CONTRAST_FULL, 1.0), e_sil)
              * (e_len / (e_len + 8.0)))
    # Uncut shells can expose back-face winners through an opening; a section
    # intentionally exposes those inner surfaces.
    vis_front = np.minimum(vis_px, proj_px)
    # In an uncut shell a back-face winner seen through an opening is not evidence
    # of its outward surface. A section exposes that inside as the intentional cut,
    # where back-face winners ARE the legible bore/passage surface.
    vis_front = np.where(is_sec[:, None], vis_px, vis_front)
    front_share = np.where(vis_px > 0.0, vis_front / np.maximum(vis_px, 1e-9), 0.0)
    diag = float(np.linalg.norm(centroid.max(axis=0) - centroid.min(axis=0))) if F else 0.0
    return {"F": F, "E": E, "V": V, "W": W, "H": H, "frame_px": float(W * H),
            "area": area, "root_area": np.sqrt(area), "surf": surf, "normal": normal,
            "centroid": centroid, "axis": axis, "revolved": np.isin(surf, _REVOLVED),
            "tri": tri, "ord_index": ord_index, "edge_len": edge_len,
            "vis_px": vis_px, "proj_px": proj_px, "vis_area": vis_area, "cos": cos,
            "legible": vis_area * legibility(cos),
            "vis_front": vis_front, "legible_front": vis_area * legibility(cos) * front_share,
            "e_vis": e_vis, "e_len": e_len, "e_con": e_con, "e_sil": e_sil, "edge_q": edge_q,
            "dirs": dirs, "is_sec": is_sec, "entropy": entropy, "palign": palign, "bg": bg,
            "cap": cap, "depth_edge": depth_edge, "fg": fg, "pe": pe, "pf": pf,
            "diag": max(diag, 1e-9)}


def _role_masks(d: dict, roles: Optional[dict]) -> tuple:
    F = d["F"]
    masks = {k: np.zeros(F, dtype=bool) for k in ("target", "context", "bc")}
    for key, arr in masks.items():
        for o in (roles or {}).get(key) or ():
            i = d["ord_index"].get(int(o))
            if i is not None:
                arr[i] = True
    masks["context"] &= ~masks["target"]
    return masks["target"], masks["context"], masks["bc"]


def _views(d: dict, view_indices) -> list:
    return [int(i) for i in (view_indices or ()) if 0 <= int(i) < d["V"]]


def _face_coverage(d: dict, S: list, key: str = "legible"):
    """Per-face legible coverage of the SET in 0..1: the best single view, plus a quarter of
    what the probabilistic union adds on top — two views that each see a different half of a
    big face show more of it than either, but two copies of one view show nothing new.
    `key` "legible_front" counts only what the views show of each face's front side."""
    frac = np.clip(d[key][S] / np.maximum(d["area"], 1e-9), 0.0, 1.0)
    best = frac.max(axis=0)
    union = 1.0 - np.prod(1.0 - frac, axis=0)
    return best + 0.25 * (union - best)


def _stare(d: dict, S: list, weight):
    """Views down a revolved feature's axis collapse its wall into a filled disc.

    Use the target's geometry, not visible curved-wall area: that area approaches zero
    exactly when the camera is staring down a blind bore, where the penalty matters most.
    """
    rev = d["revolved"] & (weight > 0)
    axes = d["axis"][rev]
    axes = axes[np.linalg.norm(axes, axis=1) > 0.5]
    if not len(axes):
        return np.zeros(len(S))
    alignment = np.max(np.abs(d["dirs"][S] @ axes.T), axis=1)
    return np.clip((alignment - math.cos(math.radians(35.0))) /
                   (1.0 - math.cos(math.radians(35.0))), 0.0, 1.0)


def _target_screen_frac(d: dict, S: list, t, key: str = "vis_px") -> np.ndarray:
    return d[key][S][:, t].sum(axis=1) / d["frame_px"]


def _redundancy(shown, dirs) -> float:
    """Pairwise weighted Jaccard overlap of per-face coverage. `dirs` scales by
    camera alignment for geometric redundancy; without it, matching pixel coverage
    still penalizes visually duplicate shots taken from different angles."""
    n = shown.shape[0]
    if n < 2:
        return 0.0
    tot = 0.0
    for i in range(n - 1):
        lo = np.minimum(shown[i][None, :], shown[i + 1:]).sum(axis=1)
        hi = np.maximum(shown[i][None, :], shown[i + 1:]).sum(axis=1)
        align = np.maximum(dirs[i + 1:] @ dirs[i], 0.0) if dirs is not None else 1.0
        tot += float((lo / np.maximum(hi, 1e-12) * align).sum())
    return tot / (n - 1)


def _hand_static(d: dict, roles: Optional[dict]) -> dict:
    """Cache role-dependent weights and per-view terms for this immutable dense revision.

    Candidate sets change hundreds of times during greedy ranking; their geometry and
    roles do not. Key role contents, not identity, so mutating a role list is safe.
    """
    key = tuple(tuple(int(o) for o in (roles or {}).get(k) or ())
                for k in ("target", "context", "bc"))
    cache = d.setdefault("hand_static", {})
    if key in cache:
        return cache[key]
    t, c, b = _role_masks(d, roles)
    has_t = bool(t.any())
    w = 0.08 + 0.92 * t + 0.27 * c + 0.52 * b if has_t else 1.0 + 0.6 * b
    scale = max(float(np.median(d["area"][d["area"] > 0])) if (d["area"] > 0).any()
                else 1.0, 1e-9)
    area_weight = np.power(np.minimum(d["area"], scale * 2.0) / scale, 0.25)
    detail = np.where(d["surf"] == 0, 1.0, 1.8)
    if d["pe"].size:
        perimeter = np.zeros(d["F"])
        np.add.at(perimeter, d["pf"], d["edge_len"][d["pe"]])
        detail *= 1.0 + 0.45 * np.clip(perimeter / np.maximum(np.sqrt(d["area"]), 1e-9)
                                        / 5.0, 0.0, 1.0)
    wa = w * area_weight * detail
    ew = np.sqrt(d["edge_len"])
    if has_t and d["pe"].size:
        touch = np.zeros(d["E"], dtype=bool)
        touch[d["pe"][t[d["pf"]]]] = True
        ew = ew * (1.0 + 2.0 * touch)
    frac = np.clip(d["legible_front"] / np.maximum(d["area"], 1e-9), 0.0, 1.0)
    stare = _stare(d, list(range(d["V"])),
                   t.astype(np.float64) if has_t else np.ones(d["F"]))
    target = missing = np.zeros(d["V"])
    if has_t:
        s = d["vis_front"][:, t].sum(axis=1) / d["frame_px"]
        target = np.minimum(s / 0.06, 1.0) - np.clip((s - 0.55) / 0.3, 0.0, 1.0)
        shown = (frac[:, t] * wa[t]).sum(axis=1) / max(float(wa[t].sum()), 1e-12)
        missing = 1.0 - np.clip(shown / 0.3, 0.0, 1.0)
    h = {"wa": wa, "wa_sum": max(float(wa.sum()), 1e-12),
         "ew": ew, "ew_sum": max(float(ew.sum()), 1e-12), "frac": frac,
         "shown": frac * wa, "pixels": d["vis_front"] / max(d["frame_px"], 1.0) * wa,
         "flat": np.clip((d["palign"] - 0.95) / 0.035, 0.0, 1.0),
         "means": np.asarray([0.6 * (d["entropy"] / _ENTROPY_MAX) - 0.8 * stare,
                              target, np.clip(d["fg"] / d["frame_px"] / 0.35, 0.0, 1.0),
                              d["is_sec"], missing, stare]),
         "pairs": {}}
    if len(cache) >= 4:
        cache.pop(next(iter(cache)))
    cache[key] = h
    return h


def _hand_pair_row(h: dict, d: dict, i: int) -> tuple:
    """Lazily compute overlap against all candidates for each chosen prefix view."""
    pairs = h["pairs"]
    if i not in pairs:
        rows = []
        for key in ("shown", "pixels"):
            shown = h[key]
            lo = np.minimum(shown[i], shown).sum(axis=1)
            hi = np.maximum(shown[i], shown).sum(axis=1)
            overlap = lo / np.maximum(hi, 1e-12)
            if key == "shown":
                overlap *= np.maximum(d["dirs"] @ d["dirs"][i], 0.0)
            rows.append(overlap)
        pairs[i] = tuple(rows)
    return pairs[i]


def hand_utility(metrics: dict, view_indices, roles: Optional[dict]) -> float:
    """Deterministic set utility — the planner's fallback and the trainer's baseline.

    U(S) rewards legible information, not square millimetres: a planar stock face
    saturates quickly while a small wall or floor and its B-rep boundary count. Curved
    faces and faces with many short bounding edges receive extra weight. A section's
    newly revealed internal face counts; a duplicate camera whose foreground and
    per-face pixel coverage closely match a chosen shot pays a redundancy penalty.
    Target, BC, and context roles retain their relative precedence."""
    d = _dense(metrics)
    S = _views(d, view_indices)
    if not S or d["F"] == 0:
        return 0.0
    h = _hand_static(d, roles)
    frac = h["frac"][S]
    best = frac.max(axis=0)
    union = 1.0 - np.prod(1.0 - frac, axis=0)
    cov = best + 0.25 * (union - best)
    C = float((h["wa"] * cov).sum() / h["wa_sum"])
    E = 0.0
    if d["E"]:
        E = float((h["ew"] * d["edge_q"][S].max(axis=0)).sum() / h["ew_sum"])
    Q, T, O, X, M, stare = h["means"][:, S].mean(axis=1)
    F = float(h["flat"][S].sum())
    R = pixel_overlap = 0.0
    if len(S) > 1:
        for j, i in enumerate(S[:-1]):
            geometric, pixels = _hand_pair_row(h, d, i)
            R += float(geometric[S[j + 1:]].sum())
            pixel_overlap += float(pixels[S[j + 1:]].sum())
        R /= len(S) - 1
        pixel_overlap /= len(S) - 1
    return float(3.8 * C + 1.2 * E + 0.6 * Q + 0.8 * T + 1.0 * O - 0.12 * X - 1.0 * F
                 - 0.5 * R - 1.3 * pixel_overlap - 2.2 * M - 3.0 * stare)


def context_from(metrics: dict, *, roles: Optional[dict] = None, target_kind: str = "part",
                 internal: bool = False, part_bbox: Optional[dict] = None) -> dict:
    """The raw per-query context build_set_tokens encodes: part complexity, bbox shape, what
    kind of thing is targeted and whether it is internal / carries boundary conditions."""
    faces = metrics.get("faces") or []
    bodies = {int(_num(f.get("body"))) for f in faces}
    size = None
    if isinstance(part_bbox, dict):
        if isinstance(part_bbox.get("size"), (list, tuple)):
            size = [abs(_num(x)) for x in part_bbox["size"][:3]]
        elif part_bbox.get("min") is not None and part_bbox.get("max") is not None:
            lo, hi = _vec3(part_bbox["min"]), _vec3(part_bbox["max"])
            size = [abs(hi[k] - lo[k]) for k in range(3)]
    if size is None:
        cs = np.asarray([_vec3(f.get("centroid")) for f in faces], dtype=np.float64).reshape(-1, 3)
        size = (cs.max(axis=0) - cs.min(axis=0)).tolist() if len(cs) else [0.0, 0.0, 0.0]
    kind = target_kind if target_kind in TARGET_KINDS else "set"
    return {"n_faces": len(faces), "n_bodies": len(bodies), "bbox_size": size,
            "target_kind": kind, "internal": bool(internal),
            "has_bc": bool((roles or {}).get("bc")),
            "has_target": bool((roles or {}).get("target"))}


def encode_context(context: dict) -> np.ndarray:
    v = np.zeros(DC, dtype=np.float64)
    v[0] = math.log1p(max(_num(context.get("n_faces")), 0.0))
    v[1] = math.log1p(max(_num(context.get("n_bodies")), 0.0))
    size = sorted((abs(_num(x)) for x in (context.get("bbox_size") or [0.0, 0.0, 0.0])[:3]),
                  reverse=True)
    size += [0.0] * (3 - len(size))
    mx = max(size[0], 1e-9)
    v[2:5] = [s / mx for s in size]
    kind = context.get("target_kind") or "part"
    v[5 + (TARGET_KINDS.index(kind) if kind in TARGET_KINDS else TARGET_KINDS.index("set"))] = 1.0
    v[11] = 1.0 if context.get("internal") else 0.0
    v[12] = 1.0 if context.get("has_bc") else 0.0
    v[13] = math.log1p(math.sqrt(sum(s * s for s in size)))
    v[14] = 1.0 if context.get("has_target") else 0.0
    return v.astype(np.float32)


def build_set_tokens(metrics: dict, view_indices, roles: Optional[dict],
                     context: dict) -> dict:
    """{"face_tokens": f32[F,DF], "face_mask": bool[F], "set_global": f32[DG],
    "context": f32[DC]} for the view SET `view_indices`.

    Face token columns: 0 log1p area | 1-7 surf one-hot | 8-10 role target/context/bc |
    11-13 normal | 14 best visible fraction (visible/projected px) | 15 best |cos| where seen |
    16 #views seeing it | 17 occlusion 1-sum(visible)/sum(projected) | 18 legible coverage |
    19 log1p best visible px | 20-22 boundary edges' best contrast / visible fraction /
    silhouette | 23 log1p boundary edges' best on-screen length | 24 log1p tri count |
    25 area share | 26 distance to the target centre / part diagonal | 27 seen only in a
    section view | 28 least revolve-axis alignment among views seeing it | 29 best
    |cos| x visible fraction | 30-31 zero.

    set_global: 0 set size/4 | 1-4 legible coverage all/target/context/bc (area-weighted) |
    5-6 mean/max normal entropy (bits/log2 20) | 7 mean principal_align | 8 share of views
    with principal_align > 0.95 | 9 any section | 10 section share | 11 log1p silhouette px |
    12 log1p edge crispness | 13 edge-length share seen at >50 % | 14 mean fg share | 15 mean
    depth-edge share | 16 mean cap share | 17 min pairwise angle/180 | 18 mean pairwise
    |dot| | 19 mean target screen share | 20 target visible fraction | 21 worst revolve
    stare | 22 mean background share | 23 mean view elevation (dir z)."""
    d = _dense(metrics)
    S = _views(d, view_indices)
    F = d["F"]
    t, c, b = _role_masks(d, roles)
    ft = np.zeros((F, DF), dtype=np.float64)
    sg = np.zeros(DG, dtype=np.float64)
    if F:
        ft[:, 0] = np.log1p(d["area"])
        ft[np.arange(F), 1 + d["surf"]] = 1.0
        ft[:, 8], ft[:, 9], ft[:, 10] = t, c, b
        ft[:, 11:14] = d["normal"]
        ft[:, 24] = np.log1p(d["tri"])
        ft[:, 25] = d["area"] / max(float(d["area"].sum()), 1e-12)
        if t.any():
            w = d["area"][t]
            ctr = (d["centroid"][t] * w[:, None]).sum(axis=0) / max(float(w.sum()), 1e-12)
            ft[:, 26] = np.linalg.norm(d["centroid"] - ctr, axis=1) / d["diag"]
    if S and F:
        vis_px, proj = d["vis_px"][S], d["proj_px"][S]
        seen = vis_px > 0.0
        frac = np.where(proj > 0.0, vis_px / np.maximum(proj, 1e-12), 0.0)
        ft[:, 14] = frac.max(axis=0)
        ft[:, 15] = np.where(seen, d["cos"][S], 0.0).max(axis=0)
        ft[:, 16] = seen.sum(axis=0)
        ft[:, 17] = 1.0 - vis_px.sum(axis=0) / np.maximum(proj.sum(axis=0), 1e-12)
        ft[proj.sum(axis=0) <= 0.0, 17] = 1.0
        cov = _face_coverage(d, S)
        ft[:, 18] = cov
        ft[:, 19] = np.log1p(vis_px.max(axis=0))
        if d["pe"].size:
            ev, el = d["e_vis"][S], d["e_len"][S]
            best = {20: np.where(ev > 0, d["e_con"][S], 0.0).max(axis=0),
                    21: ev.max(axis=0),
                    22: (d["e_sil"][S] * ev).max(axis=0),
                    23: np.log1p((el * ev).max(axis=0))}
            for col, per_edge in best.items():
                agg = np.zeros(F)
                np.maximum.at(agg, d["pf"], per_edge[d["pe"]])
                ft[:, col] = agg
        sec = d["is_sec"][S]
        seen_plain = seen[~sec].any(axis=0) if (~sec).any() else np.zeros(F, dtype=bool)
        ft[:, 27] = (seen.any(axis=0) & ~seen_plain).astype(np.float64)
        align = np.abs(d["axis"] @ d["dirs"][S].T)                   # [F, s]
        align = np.where(seen.T, align, np.inf).min(axis=1)
        ft[:, 28] = np.where(d["revolved"] & np.isfinite(align), align, 0.0)
        ft[:, 29] = (np.where(seen, d["cos"][S], 0.0) * frac).max(axis=0)

        area = d["area"]
        def _cov(mask):
            if not mask.any():
                return 0.0
            return float((area[mask] * cov[mask]).sum() / max(float(area[mask].sum()), 1e-12))
        sg[1] = _cov(np.ones(F, dtype=bool))
        sg[2], sg[3], sg[4] = _cov(t), _cov(c), _cov(b)
        if t.any():
            sg[19] = float(np.mean(_target_screen_frac(d, S, t)))
            tp = float(proj[:, t].sum())
            sg[20] = float(vis_px[:, t].sum()) / tp if tp > 0 else 0.0
        stare = _stare(d, S, t.astype(np.float64) if t.any() else np.ones(F))
        sg[21] = float(stare.max())
    if S:
        sg[0] = len(S) / 4.0
        ent = d["entropy"][S] / _ENTROPY_MAX
        sg[5], sg[6] = float(ent.mean()), float(ent.max())
        sg[7] = float(d["palign"][S].mean())
        sg[8] = float((d["palign"][S] > 0.95).mean())
        sec = d["is_sec"][S]
        sg[9], sg[10] = float(sec.any()), float(sec.mean())
        if d["E"]:
            sil_px = (d["e_len"][S] * d["e_vis"][S] * d["e_sil"][S]).sum()
            sg[11] = math.log1p(float(sil_px))
            sg[12] = math.log1p(float(d["edge_q"][S].max(axis=0).sum()))
            seen_e = (d["e_vis"][S] > 0.5).any(axis=0)
            sg[13] = float(d["edge_len"][seen_e].sum() / max(float(d["edge_len"].sum()), 1e-12))
        fp = d["frame_px"]
        sg[14] = float((d["fg"][S] / fp).mean())
        sg[15] = float((d["depth_edge"][S] / fp).mean())
        sg[16] = float((d["cap"][S] / fp).mean())
        dirs = d["dirs"][S]
        if len(S) >= 2:
            dots = np.clip(dirs @ dirs.T, -1.0, 1.0)
            off = ~np.eye(len(S), dtype=bool)
            sg[17] = float(np.degrees(np.arccos(dots[off])).min()) / 180.0
            sg[18] = float(np.abs(dots[off]).mean())
        sg[22] = float((d["bg"][S] / fp).mean())
        sg[23] = float(dirs[:, 2].mean())
    return {"face_tokens": ft.astype(np.float32), "face_mask": np.ones(F, dtype=bool),
            "set_global": sg.astype(np.float32), "context": encode_context(context)}


def target_visible_fraction(metrics: dict, view_index: int,
                            roles: Optional[dict]) -> Optional[float]:
    """Visible / projected pixels of the target faces in one view (None without a target).
    projected_px ignores occlusion, so a bore wall behind the plate reads 0 here."""
    d = _dense(metrics)
    t, _c, _b = _role_masks(d, roles)
    if not t.any() or not 0 <= int(view_index) < d["V"]:
        return None
    proj = float(d["proj_px"][int(view_index), t].sum())
    return min(1.0, float(d["vis_front"][int(view_index), t].sum()) / proj) if proj > 0.0 else 0.0


def set_coverage(metrics: dict, view_indices, roles: Optional[dict]) -> dict:
    """Area-weighted legible coverage of the set: {"all", "target", "context", "bc"}, each
    0..1, or None for an empty role."""
    d = _dense(metrics)
    S = _views(d, view_indices)
    t, c, b = _role_masks(d, roles)
    cov = _face_coverage(d, S, "legible_front") if S and d["F"] else np.zeros(d["F"])
    out: dict = {}
    for key, mask in (("all", np.ones(d["F"], dtype=bool)), ("target", t), ("context", c),
                      ("bc", b)):
        a = d["area"][mask]
        out[key] = (float((a * cov[mask]).sum() / max(float(a.sum()), 1e-12))
                    if mask.any() else None)
    return out


def seen_faces(metrics: dict, view_indices):
    """bool[F]: faces with any visible pixel in any view of the set."""
    d = _dense(metrics)
    S = _views(d, view_indices)
    if not S:
        return np.zeros(d["F"], dtype=bool)
    return (d["vis_px"][S] > 0.0).any(axis=0)
