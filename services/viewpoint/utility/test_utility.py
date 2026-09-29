"""ViewNet utility: set-valued hand utility, feature tokens and numpy model inference."""
from __future__ import annotations

import math

import numpy as np
import pytest

from services.viewpoint.utility import (
    DC,
    DF,
    DG,
    FEATURE_SCHEMA,
    ModelSchemaMismatch,
    UtilityModel,
    build_set_tokens,
    context_from,
    hand_utility,
    load_default,
    seen_faces,
)
from services.viewpoint.utility.model import _SHAPES

# A 40 x 30 x 20 box: +X/-X 600 mm2, +Y/-Y 800 mm2, +Z/-Z 1200 mm2.
_BOX = [((1, 0, 0), 600.0), ((-1, 0, 0), 600.0), ((0, 1, 0), 800.0), ((0, -1, 0), 800.0),
        ((0, 0, 1), 1200.0), ((0, 0, -1), 1200.0)]


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def _metrics(dirs, faces=_BOX):
    """doc_view_metrics-shaped measurements of a convex part: a face is fully visible from
    every camera it faces, at |cos| = n . d."""
    views = []
    for d in dirs:
        d = _unit(d)
        rows = []
        for i, (n, area) in enumerate(faces):
            c = float(np.dot(_unit(n), d))
            if c > 0.05:
                rows.append([i, area * c, area, c, area * c])
        views.append({"face_vis": rows, "edge_vis": [], "bg_px": 0, "cap_px": 0,
                      "depth_edge_px": 0, "normal_entropy": math.log2(max(len(rows), 1)),
                      "principal_align": float(np.abs(d).max()), "fg_px": 1000,
                      "dir": d.tolist(), "section": None})
    return {"ok": True, "revision": 1, "width": 192, "height": 144,
            "faces": [{"body": 1, "face_id": i + 1, "surf_type": 0, "area_mm2": a,
                       "centroid": list(n), "normal": list(n), "tri_count": 2, "ordinal": i}
                      for i, (n, a) in enumerate(faces)],
            "edges": [], "views": views}


def test_hand_utility_is_set_valued_duplicates_add_nothing_complements_add_faces():
    m = _metrics([(-1, -1, 1), (-1, -3, 1), (1, 1, -1)])
    alone = hand_utility(m, [0], None)
    with_twin = hand_utility(m, [0, 1], None)
    with_opposite = hand_utility(m, [0, 2], None)
    # The twin sees the same three faces: no new surface, so (almost) no gain.
    assert with_twin - alone < 0.05
    # The opposite view sees the other three faces: the whole part is now covered.
    assert with_opposite - alone > 1.0
    assert seen_faces(m, [0, 2]).all() and not seen_faces(m, [0, 1]).all()


def test_target_weighting_prefers_the_view_that_shows_the_target():
    m = _metrics([(1, 1, 1), (-1, -1, -1)])
    roles = {"target": [5]}                      # the -Z face
    assert hand_utility(m, [1], roles) > hand_utility(m, [0], roles)
    assert hand_utility(m, [0], None) == pytest.approx(hand_utility(m, [1], None), abs=1e-9)


def _tiny_model(rng, schema=FEATURE_SCHEMA):
    arrays = {k: rng.normal(0.0, 0.2, size=s) for k, s in _SHAPES.items()}
    arrays["norm_face_std"] = rng.uniform(0.5, 2.0, size=_SHAPES["norm_face_std"])
    arrays["norm_ctx_std"] = rng.uniform(0.5, 2.0, size=_SHAPES["norm_ctx_std"])
    arrays["norm_face_std"][3] = 0.0             # a constant feature: divides by 1
    arrays["kind_bias"] = np.zeros(6)
    arrays["feature_schema"] = np.array(schema)
    arrays["trained_on"] = np.array("unit-test")
    arrays["report_sha"] = np.array("0" * 12)
    return arrays


def _reference_logit(w: dict, tokens: dict) -> float:
    """The contract forward pass written out with plain Python loops."""
    def relu(x):
        return x if x > 0.0 else 0.0

    def dense(x, W, b):
        return [sum(x[i] * W[i][j] for i in range(len(x))) + b[j] for j in range(len(b))]

    fstd = [s if s > 1e-12 else 1.0 for s in w["norm_face_std"]]
    cstd = [s if s > 1e-12 else 1.0 for s in w["norm_ctx_std"]]
    xc = list(tokens["set_global"]) + list(tokens["context"])
    xc = [(xc[i] - w["norm_ctx_mean"][i]) / cstd[i] for i in range(len(xc))]
    c = [relu(v) for v in dense([relu(v) for v in dense(xc, w["ctx_w0"], w["ctx_b0"])],
                                w["ctx_w1"], w["ctx_b1"])]
    hs = []
    for row, on in zip(tokens["face_tokens"], tokens["face_mask"]):
        if not on:
            continue
        x = [(row[i] - w["norm_face_mean"][i]) / fstd[i] for i in range(len(row))]
        h = [relu(v) for v in dense(x, w["face_w0"], w["face_b0"])]
        hs.append([relu(v) for v in dense(h, w["face_w1"], w["face_b1"])])
    q = [sum(c[i] * w["att_wq"][i][j] for i in range(len(c))) for j in range(len(c))]
    a = [sum(h[j] * q[j] for j in range(len(q))) / 8.0 for h in hs]
    top = max(a)
    e = [math.exp(v - top) for v in a]
    z = sum(e)
    p = [sum(e[i] / z * hs[i][j] for i in range(len(hs))) for j in range(len(c))]
    mx = [max(h[j] for h in hs) for j in range(len(c))]
    hid = [relu(v) for v in dense(p + mx + c, w["out_w0"], w["out_b0"])]
    return dense(hid, w["out_w1"], w["out_b1"])[0]


def test_numpy_forward_matches_the_contract_computed_by_hand(tmp_path):
    rng = np.random.default_rng(7)
    arrays = _tiny_model(rng)
    path = tmp_path / "utility_v1.npz"
    np.savez(path, **arrays)
    model = UtilityModel.load(path)
    assert model.name.startswith("utility_v1@") and len(model.name) == len("utility_v1@") + 8

    m = _metrics([(-1, -1, 1), (1, 1, -1)], faces=_BOX[:3])
    ctx = context_from(m, target_kind="part", part_bbox={"size": [40, 30, 20]})
    tokens = build_set_tokens(m, [0, 1], None, ctx)
    assert tokens["face_tokens"].shape == (3, DF)
    assert tokens["set_global"].shape == (DG,) and tokens["context"].shape == (DC,)

    w = {k: np.asarray(v, dtype=np.float64).tolist() for k, v in arrays.items()
         if k in _SHAPES}
    ref_tokens = {k: np.asarray(v).tolist() for k, v in tokens.items()}
    assert model.score(tokens) == pytest.approx(_reference_logit(w, ref_tokens), abs=1e-9)
    batch = model.score_batch([tokens, tokens])
    assert batch.shape == (2,) and batch[0] == pytest.approx(model.score(tokens), abs=0)


def test_a_model_trained_on_another_feature_schema_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "utility_v1.npz"
    np.savez(path, **_tiny_model(np.random.default_rng(1), schema="viewutil.features/0"))
    with pytest.raises(ModelSchemaMismatch, match="viewutil.features/0"):
        UtilityModel.load(path)
    model, note = load_default(path)
    assert model is None and "refused" in note and FEATURE_SCHEMA in note


def test_missing_weights_are_reported_not_invented(tmp_path):
    model, note = load_default(tmp_path / "absent.npz")
    assert model is None and "absent.npz" in note
