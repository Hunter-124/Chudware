"""One MFCAD++ part per seed: feature sequence (main.py), feature application
(feature_creation.shape_from_directive), STEP + label sidecar (main.shape_with_fid_to_step).

Needs OCP; import it only in a worker process (runner.py) or where OCP is known to be present."""
from __future__ import annotations

import json
import os
import random
from math import comb
from pathlib import Path
from typing import Optional

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.STEPConstruct import STEPConstruct
from OCP.TopLoc import TopLoc_Location
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TCollection import TCollection_HAsciiString

from . import GENERATOR_VERSION, SIDECAR_SCHEMA, failed_path, sidecar_path, step_path
from . import occ, params
from .features import FEATURE_CLASSES, Chamfer, Ctx, Round

_GROUPS = (
    ("rectangular_through_step", "2sides_through_step", "slanted_through_step",
     "triangular_blind_step", "circular_blind_step", "rectangular_blind_step"),
    ("triangular_through_slot", "rectangular_through_slot", "circular_through_slot",
     "rectangular_blind_slot", "v_circular_end_blind_slot", "h_circular_end_blind_slot"),
    ("through_hole", "triangular_passage", "rectangular_passage", "6sides_passage"),
    ("blind_hole", "triangular_pocket", "rectangular_pocket", "6sides_pocket",
     "circular_end_pocket"),
    ("Oring",),
    ("chamfer", "round"),
)


def sample_combo(rng: random.Random) -> list:
    """A uniform draw from main.py's combo list: every multiset
    (combinations_with_replacement) of 3..9 of the 24 feature classes, equally likely. The size
    is drawn by its multiset count, the multiset by stars and bars; sorted like the original."""
    lo, hi = params.COMBO_RANGE
    n_cls = params.NUM_FEATURES
    sizes = list(range(lo, hi))
    n = rng.choices(sizes, weights=[comb(n_cls + k - 1, k) for k in sizes])[0]
    bars = sorted(rng.sample(range(n + n_cls - 1), n))
    return [b - i for i, b in enumerate(bars)]


def rearrange_combo(combo) -> list:
    """steps, slots, through, blind, o-ring, then chamfer/round — each group in combo order."""
    out = []
    for group in _GROUPS:
        ids = {params.FEAT_NAMES.index(n) for n in group}
        out.extend(v for v in combo if v in ids)
    return out


def shape_from_directive(combo, rng: random.Random) -> tuple:
    """(FaceLabels, applied feature names in instance order, stock dims, ctx log)."""
    combo = rearrange_combo(combo)
    stock = tuple(rng.uniform(a, b) for a, b in zip(params.STOCK_MIN, params.STOCK_MAX))
    ctx = Ctx(rng=rng, stock=stock)
    labels = occ.FaceLabels.uniform(BRepPrimAPI_MakeBox(*stock).Shape(), params.STOCK, -1)
    applied = []
    count = 0
    bounds: list = []
    edges: list = []
    find_edges = True
    for fid in combo:
        name = params.FEAT_NAMES[fid]
        if name == "chamfer":
            edges = occ.list_edge(labels.shape)
            labels, edges, ok = Chamfer(ctx, labels, edges).add_feature()
        elif name == "round":
            if find_edges:
                edges = occ.list_edge(labels.shape)
                find_edges = False
            labels, edges, ok = Round(ctx, labels, edges).add_feature()
        else:
            # bounds are searched per step/slot; the first through/blind feature searches the
            # inner bounds and every later one reuses (and shifts) that same list
            occ.triangulate_shape(labels.shape)
            feat = FEATURE_CLASSES[name](ctx, labels)
            if count == 0:
                labels, bounds, ok = feat.add_feature(bounds, find_bounds=True)
                if name in params.THROUGH_BLIND_FEATURES:
                    count += 1
            else:
                labels, bounds, ok = feat.add_feature(bounds, find_bounds=False)
                count += 1
        if ok:
            applied.append(name)
        if name in ("chamfer", "round") and len(edges) == 0:
            break
    return labels, applied, stock, ctx.log


def write_step(path: Path, labels: occ.FaceLabels) -> None:
    """STEP AP214 with every ADVANCED_FACE named by its face id (index in list_face order)."""
    writer = STEPControl_Writer()
    finder = writer.WS().TransferWriter().FinderProcess()
    writer.Transfer(labels.shape, STEPControl_AsIs)
    for fid, face in enumerate(labels.faces):
        # the 3-argument form resolves located faces (BRepFeat returns some) like the original
        entity = STEPConstruct.FindEntity_s(finder, face, TopLoc_Location())
        if entity is None:
            raise RuntimeError(f"face {fid} has no STEP entity")
        entity.SetName(TCollection_HAsciiString(str(fid)))
    tmp = path.with_name(path.name + ".tmp")
    if writer.Write(str(tmp)) != 1:          # IFSelect_RetDone
        raise RuntimeError(f"STEP write failed: {path}")
    os.replace(tmp, path)


def generate_part(seed: int, combo: Optional[list] = None) -> tuple:
    """(labels, sidecar dict without file-level keys) for `seed`; `combo` overrides the drawn
    feature sequence (class indices or names)."""
    rng = random.Random(seed)
    drawn = sample_combo(rng)
    if combo is not None:
        drawn = [params.FEAT_NAMES.index(c) if isinstance(c, str) else int(c) for c in combo]
    labels, applied, stock, log = shape_from_directive(drawn, rng)
    if not occ.is_valid_single_solid(labels.shape):
        raise RuntimeError("result is not one valid solid")
    faces = []
    for fid, (face, c, k) in enumerate(zip(labels.faces, labels.cls, labels.inst)):
        faces.append({"id": fid, "class": params.FEAT_NAMES[c], "instance": k,
                      **occ.face_geometry(face)})
    side = {"schema": SIDECAR_SCHEMA, "generator_version": GENERATOR_VERSION, "seed": seed,
            "combo": [params.FEAT_NAMES[c] for c in drawn],
            "applied": [{"instance": i, "class": n} for i, n in enumerate(applied)],
            "stock_mm": list(stock), "notes": log, "faces": faces}
    return labels, side


def write_part(seed: int, out_dir: Path, combo: Optional[list] = None) -> dict:
    """Generate `seed` into out_dir/<seed>.step + .labels.json. A generation error writes
    <seed>.failed.json instead. Returns {"seed", "ok", "reason", "n_faces"}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    step = step_path(out_dir, seed)
    try:
        labels, side = generate_part(seed, combo)
        write_step(step, labels)
    except Exception as e:  # noqa: BLE001 — recorded per seed, the run continues
        reason = f"{type(e).__name__}: {e}"
        failed_path(out_dir, seed).write_text(json.dumps(
            {"seed": seed, "generator_version": GENERATOR_VERSION, "reason": reason}))
        return {"seed": seed, "ok": False, "reason": reason, "n_faces": 0}
    tmp = sidecar_path(step).with_name(sidecar_path(step).name + ".tmp")
    tmp.write_text(json.dumps(side, indent=1))
    os.replace(tmp, sidecar_path(step))
    failed_path(out_dir, seed).unlink(missing_ok=True)
    return {"seed": seed, "ok": True, "reason": None, "n_faces": len(side["faces"])}
