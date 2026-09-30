"""Pixel-exact mesh attribution for cached Observer grids; never re-renders RGB.

The class plane uses 0 for background, 1..len(CANON) for manifest classes, and the
last id for a section cap. A second plane marks feature-instance/silhouette/depth edges.
Camera poses are reconstructed from the same planner candidates that made each grid.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from training.viewpoint.datasets.importers import CANON
from training.viewpoint.datasets.build_manifest import default_manifest_path
from training.viewpoint.utility.trials import load_labels
from training.viewpoint.durable import durable_replace, nonempty
from .render import RenderPool, _open_part, _W

N_CLASSES = len(CANON) + 2


def _dense_dir(root, tile_hw):
    return Path(root).expanduser() / "observer" / "dense" / f"{tile_hw[0]}x{tile_hw[1]}"


def dense_output_path(root, image, tile_hw):
    return _dense_dir(root, tile_hw) / f"{Path(image).stem}.npz"


def dense_path(root, image, tile_hw):
    compressed = dense_output_path(root, image, tile_hw)
    return compressed if nonempty(compressed) else compressed.with_suffix(".npy")


def compress_dense(root, tile_hw=(192, 256), *, workers=8, log=print):
    """Migrate existing masks losslessly: tiny ZIP/deflate files replace huge raw arrays."""
    paths = list(_dense_dir(root, tile_hw).glob("*.npy"))
    if not paths:
        return {"converted": 0, "corrupt": 0}

    def convert(src):
        dest = src.with_suffix(".npz")
        try:
            arr = np.load(src)
            if arr.dtype != np.uint8 or arr.ndim != 4 or arr.shape[1:] != (*tile_hw, 2) \
                    or not 1 <= arr.shape[0] <= 4:
                raise ValueError(f"invalid dense mask {arr.shape}/{arr.dtype}")
        except (OSError, ValueError, EOFError):
            src.unlink()
            return "corrupt"
        if not nonempty(dest):
            tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.{id(src)}.npz")
            np.savez_compressed(tmp, labels=arr)
            durable_replace(tmp, dest)
        src.unlink()
        return "converted"

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(convert, paths))
    converted, corrupt = results.count("converted"), results.count("corrupt")
    log(f"[observer] dense compression: {converted} migrated, {corrupt} corrupt rebuilt "
        f"in {time.monotonic()-t0:.0f}s")
    return {"converted": converted, "corrupt": corrupt}


def _candidates(record, group):
    from services.mcp import shot_planner as sp
    from services.mcp.cad_viewpoint import perspective_camera
    from training.viewpoint.utility.trials import FIXED_DIRS
    from .render import _target
    bb, doc = _W["bbox"], _W["doc"]
    W, H = 512, 384
    # Both render pipelines currently use 512x384 source tiles. Existing grids retain
    # their original tile dimensions in the PNG; inference below checks that dimension.
    if group[0]["mode"] == "part":
        cands = sp.candidate_cameras(bb, part_points=sp.part_points(doc), width=W, height=H)
    else:
        inst = int(group[0]["target_instance"])
        feat = next(f for f in record["features"] if int(f["instance"]) == inst)
        tgt = _target(tuple(int(o) for o in feat["faces"]))
        cands = sp.candidate_cameras(bb, target_bbox=tgt["bbox"],
                                     target_normals=tgt["normals"], target_points=tgt["points"],
                                     internal=tgt["internal"], width=W, height=H)
        if group[0].get("view_indices") is not None:
            offsets = {int(i) - len(cands) - list(FIXED_DIRS).index(name)
                       for s in group for name, i in zip(s["view_labels"], s["view_indices"])
                       if name in FIXED_DIRS}
            if len(offsets) > 1:
                raise ValueError(f"{record['part_id']}: inconsistent fixed-camera offsets {offsets}")
            # Resume may contain only non-fixed cameras: their base indices do not
            # depend on whether unselected section candidates were appended.
            include_sections = (next(iter(offsets)) > 0 if offsets else
                                any(any(s.get("sections", ())) for s in group))
        else:
            include_sections = any(any(s.get("sections", ())) for s in group)
        if include_sections:
            cands += sp.section_candidates(bb, tgt["bbox"], tgt["normals"],
                                            target_points=tgt["points"], width=W, height=H)
    fixed = FIXED_DIRS
    cands += [{"camera": perspective_camera(bb, d, W, H, scene_bbox=bb),
               "dir": list(d), "label": name, "section": None}
              for name, d in fixed.items()]
    return cands


def _views(record, group):
    cands = _candidates(record, group)
    for s in group:
        if s.get("view_indices") is not None:
            idx = [int(i) for i in s["view_indices"]]
        else:
            idx = []
            for name, is_section in zip(s["view_labels"], s["sections"]):
                found = [i for i, c in enumerate(cands)
                         if c["label"] == name and bool(c["section"]) == bool(is_section)]
                if not found or any(cands[i]["camera"] != cands[found[0]]["camera"] or
                                    cands[i]["section"] != cands[found[0]]["section"]
                                    for i in found[1:]):
                    raise ValueError(f"{s['set_key']}: ambiguous/unavailable camera {name}: {found}")
                idx.append(found[0])
        views = [cands[i] for i in idx]
        # Cache keys from old runs contain the renderer/library digest; valid cached RGB
        # may therefore have a different key after overlay code changes. The silhouette
        # registration check in _job_dense catches truly incompatible camera reconstructions.
        yield s, views


def raster_labels(mesh, record, view, tile_hw):
    """uint8 [H,W,2] class and feature-boundary/depth-discontinuity edge."""
    from services.mcp import cad_common as cc
    from services.mcp.cad_viewpoint import perspective_frame


    H, W = tile_hw
    camera = dict(view["camera"], width=W, height=H)
    frame = perspective_frame(camera)
    if view.get("section"):
        frame["section"] = view["section"]
    raster = cc._mesh_depth_buffer(mesh, frame)
    if raster is None:
        raise ValueError("mesh exceeds raster triangle budget")
    depth, fi, _ = raster
    by_topo, _ = cc._mesh_topo_index(mesh)
    if view.get("xray"):
        # The renderer overlays a second, target-only depth pass over the translucent
        # shell. The ordinary z-buffer labels the occluder instead of those red pixels.
        target_faces = tuple(int(o) for o in view.get("target_faces", ()))
        if not target_faces:
            raise ValueError("x-ray labels require target_faces")
        target_idx = {by_topo[o + 1] for o in target_faces if o + 1 in by_topo}
        select = np.isin(mesh["tri_face"], list(target_idx))
        target_mesh = {"pos": mesh["pos"], "idx": mesh["idx"][select],
                       "tri_face": mesh["tri_face"][select]}
        target = cc._mesh_depth_buffer(target_mesh, frame)
        if target is None:
            raise ValueError("x-ray target exceeds raster triangle budget")
        target_depth, target_fi, _ = target
        draw = target_fi >= 0
        fi = np.where(draw, target_fi, fi)
        depth = np.where(draw, target_depth, depth)
    # fi is an index into mesh['faces']; map topology ordinals in the manifest, never
    # face array order (multi-body documents have distinct body-local face_id values).
    cls = np.full(len(mesh["faces"]), CANON.index("other") + 1, dtype=np.uint8)
    inst = np.full(len(mesh["faces"]), -2, dtype=np.int32)
    for face in record["faces"]:
        j = by_topo.get(int(face["ordinal"]) + 1)
        if j is not None:
            cls[j] = CANON.index(face["class"]) + 1
            inst[j] = int(face["instance"])
    ids = np.clip(fi, 0, max(len(cls) - 1, 0))
    classes = np.where(fi == -2, N_CLASSES - 1, np.where(fi < 0, 0, cls[ids])).astype(np.uint8)
    instances = np.where(fi < 0, -3, inst[ids])
    edge = np.zeros((H, W), dtype=np.uint8)
    for a, b, ea, eb in ((instances[:, 1:], instances[:, :-1], edge[:, 1:], edge[:, :-1]),
                         (instances[1:], instances[:-1], edge[1:], edge[:-1])):
        discontinuity = a != b
        ea[discontinuity] = 1
        eb[discontinuity] = 1
    # Depth jumps inside one feature can reveal a slot or hole that the instance map
    # alone misses. Perspective reciprocal depth is normalized by the visible range.
    foreground = fi >= 0
    if foreground.any():
        near, far = np.percentile(depth[foreground], (5, 95))
        span = max(float(far - near), 1e-9)
        for z0, z1, a, b in ((depth[:, 1:], depth[:, :-1], edge[:, 1:], edge[:, :-1]),
                             (depth[1:], depth[:-1], edge[1:], edge[:-1])):
            jump = (np.abs(z0 - z1) > 0.07 * span) & np.isfinite(z0) & np.isfinite(z1)
            a[jump] = 1
            b[jump] = 1
    return np.stack((classes, edge), -1)


def _job_dense(record, sets, root, tile_hw):
    from services.mcp import cad_common as cc
    from PIL import Image
    from .train import grid_layout, TILE_GAP
    out = {"built": 0, "errors": []}
    try:
        _open_part(record)
        mesh = cc._doc_mesh(_W["doc"])
        if mesh is None:
            raise ValueError("mesh unavailable")
        groups = defaultdict(list)
        for s in sets:
            groups[(s["mode"], s.get("target_instance", -1),
                    s.get("view_indices") is not None)].append(s)
        for group in groups.values():
            for s, views in _views(record, group):
                try:
                    if nonempty(dense_path(root, s["image"], tile_hw)):
                        continue
                    dest = dense_output_path(root, s["image"], tile_hw)
                    with Image.open(Path(root) / s["image"]) as im:
                        cols, rows = grid_layout(len(views))
                        sw = (im.width - TILE_GAP * (cols - 1)) // cols
                        sh = (im.height - TILE_GAP * (rows - 1)) // rows
                    if (sw, sh) != (512, 384):
                        raise ValueError(f"unexpected source tile {sw}x{sh}")
                    if any(v.get("xray") for v in views):
                        target = next(f["faces"] for f in record["features"]
                                      if int(f["instance"]) == int(s["target_instance"]))
                        views = [dict(v, target_faces=target) if v.get("xray") else v
                                 for v in views]
                    arr = np.stack([raster_labels(mesh, record, view, tile_hw) for view in views])
                    from .train import split_grid
                    with Image.open(Path(root) / s["image"]) as source:
                        rgb = [np.asarray(t.resize((tile_hw[1], tile_hw[0]), Image.Resampling.BOX))
                               for t in split_grid(source.convert("RGB"), len(views))]
                    for tile, image in zip(arr, rgb):
                        foreground = tile[:, :, 0] > 0
                        if foreground.any() and (image[foreground].mean(axis=1) < 230).mean() < 0.75:
                            raise ValueError(f"{s['set_key']}: raster and RGB silhouettes disagree")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.npz")
                    np.savez_compressed(tmp, labels=arr)
                    durable_replace(tmp, dest)
                    out["built"] += 1
                except Exception as exc:
                    out["errors"].append(f"{record['part_id']}/{s['set_key']}: {exc}")
    except Exception as exc:
        out["errors"].append(f"{record['part_id']}: {exc}")
    return out


def prepare_dense(root, sets, tile_hw=(192, 256), *, labels=None, workers=None,
                  limit_parts=None, log=print):
    """Resume label rendering over existing RGB grids, parallelized by part and engine."""
    from training.viewpoint.utility.store import TrialStore
    from .train import training_sets
    root = Path(root).expanduser()
    compression = compress_dense(root, tile_hw, workers=workers or 8, log=log)
    sets = training_sets(TrialStore(root)) if sets is None else sets
    records = {r["part_id"]: r for r in load_labels(labels or default_manifest_path(),
                                                       ("train", "val", "test"))}
    groups = defaultdict(list)
    for s in sets:
        if s.get("image") and not nonempty(dense_path(root, s["image"], tile_hw)):
            groups[s["part_id"]].append(s)
    if limit_parts is not None:
        groups = dict(list(sorted(groups.items()))[:limit_parts])
    missing = set(groups) - records.keys()
    if missing:
        raise ValueError(f"{len(missing)} sets' parts absent from labels: {list(missing)[:3]}")
    t0 = time.monotonic()
    done, errors = 0, []
    jobs = [(records[pid], group, str(root), tuple(tile_hw)) for pid, group in groups.items()]
    # All recycling workers vanished after 54k masks with pending futures: Python's
    # ProcessPoolExecutor stopped respawning them. Keep these bounded, one-part-at-a-time
    # workers alive for the dense stage; the pool stall watchdog still retries crashes.
    with RenderPool(workers=workers, max_tasks_per_child=None, stall_s=180) as pool:
        for _job, result in pool.run(_job_dense, jobs):
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                done += result["built"]
                errors.extend(result["errors"])
            if isinstance(result, dict) and done and done % 1000 < result["built"]:
                log(f"[observer] dense {done}/{sum(map(len, groups.values()))} "
                    f"in {time.monotonic()-t0:.0f}s; errors={len(errors)}")
    summary = {"built": done, "total": sum(map(len, groups.values())), "errors": errors,
               "seconds": round(time.monotonic()-t0, 2), "compression": compression}
    if errors:
        err_path = _dense_dir(root, tile_hw).parent / "errors.txt"
        err_path.write_text("\n".join(errors) + "\n")
    log(f"[observer] dense: {summary['built']}/{summary['total']} sets in {summary['seconds']}s, "
        f"{len(errors)} failures (observer/dense/errors.txt): {errors[:3]}")
    return summary
