"""Unit tests for tools/gsl_gnn/ranker.py.

All tests must work WITHOUT torch and WITHOUT the live engine.
Run with: python3 -m pytest tools/gsl_gnn/test_ranker.py -v
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

# Ensure the package-local ranker is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ranker import (
    FEATURE_DIM,
    KIND_VOCAB,
    candidate_features,
    numpy_forward,
    rank_candidates,
)

# Use _NODE_DIM from the module (it may be called _NODE_DIM or NUM_NODE_FEATURES)
import ranker as _ranker_mod
_NODE_DIM = getattr(_ranker_mod, '_NODE_DIM', getattr(_ranker_mod, 'NUM_NODE_FEATURES', 18))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_face(sid="F001", surf="plane", area=100.0, radius=None,
               concavity=None, normal=None, centroid=None):
    return {
        "sid": sid,
        "surf": surf,
        "area_or_len": area,
        "radius": radius,
        "concavity": concavity,
        "normal": normal or [0.0, 0.0, 1.0],
        "centroid": centroid or [0.0, 0.0, 0.0],
    }


def _make_weights(in_dim=FEATURE_DIM, h1=64, h2=32):
    """Create a minimal (zero) weight dict matching the MLP architecture."""
    import numpy as np
    rng = np.random.default_rng(0)
    return {
        "fc1.weight": rng.standard_normal((h1, in_dim)).tolist(),
        "fc1.bias":   rng.standard_normal((h1,)).tolist(),
        "fc2.weight": rng.standard_normal((h2, h1)).tolist(),
        "fc2.bias":   rng.standard_normal((h2,)).tolist(),
        "fc3.weight": rng.standard_normal((1, h2)).tolist(),
        "fc3.bias":   rng.standard_normal((1,)).tolist(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_candidate_features_empty_sids():
    """candidate_features with no sids => zeros for aggregation, kind one-hot set."""
    cand = {"sids": [], "kind": "hole"}
    faces_by_sid = {}
    feat = candidate_features(cand, faces_by_sid, diag=100.0, total_faces=10)
    assert len(feat) == FEATURE_DIM
    # Mean and max blocks should be all zeros
    assert all(v == 0.0 for v in feat[:_NODE_DIM]), "mean block should be zero"
    assert all(v == 0.0 for v in feat[_NODE_DIM:2 * _NODE_DIM]), "max block should be zero"
    # kind one-hot: "hole" should be 1
    kind_start = 2 * _NODE_DIM + 1
    kind_oh = feat[kind_start:]
    assert len(kind_oh) == len(KIND_VOCAB)
    hole_idx = KIND_VOCAB.index("hole")
    assert kind_oh[hole_idx] == 1.0
    assert sum(kind_oh) == 1.0


def test_candidate_features_shape():
    """candidate_features with a real face => 43-dim output."""
    face = _make_face(sid="F001", surf="cylinder", area=500.0, radius=5.0,
                      concavity="bore", normal=[0.0, 0.0, 1.0],
                      centroid=[10.0, 20.0, 30.0])
    cand = {"sids": ["F001"], "kind": "hole"}
    faces_by_sid = {"F001": face}
    feat = candidate_features(cand, faces_by_sid, diag=100.0, total_faces=5)
    assert len(feat) == FEATURE_DIM
    # frac = 1/5 = 0.2
    frac = feat[2 * _NODE_DIM]
    assert abs(frac - 0.2) < 1e-6


def test_numpy_forward_shape():
    """numpy_forward returns one score per input row."""
    weights = _make_weights()
    n = 5
    x = [[0.0] * FEATURE_DIM for _ in range(n)]
    scores = numpy_forward(weights, x)
    assert len(scores) == n
    assert all(isinstance(s, float) for s in scores)


def test_rank_candidates_empty():
    """rank_candidates with empty list returns empty list."""
    weights = _make_weights()
    result = rank_candidates([], {}, 100.0, weights)
    assert result == []


def test_rank_candidates_fallback_none_weights():
    """rank_candidates with None weights returns candidates unchanged."""
    cands = [{"sids": ["F1"], "kind": "hole"}, {"sids": ["F2"], "kind": "fillet"}]
    result = rank_candidates(cands, {}, 100.0, None)
    assert result is cands or result == cands


def test_rank_candidates_fallback_empty_weights():
    """rank_candidates with empty dict weights returns candidates unchanged."""
    cands = [{"sids": ["F1"], "kind": "hole"}, {"sids": ["F2"], "kind": "fillet"}]
    result = rank_candidates(cands, {}, 100.0, {})
    assert result == cands


def test_rank_candidates_ordering():
    """Ranker re-orders by score: highest-scored candidate comes first."""
    # Build weights that strongly prefer 'hole' kind (last 6 dims = kind one-hot)
    # We craft weights so kind_oh[0] (hole) gets a large positive score.
    import numpy as np

    in_dim = FEATURE_DIM
    h1, h2 = 4, 4  # tiny for manual crafting

    # Identity-ish path: fc1 picks out kind one-hot position 0 (hole index)
    # The kind one-hot starts at position 2*_NODE_DIM+1
    hole_pos = 2 * _NODE_DIM + 1 + KIND_VOCAB.index("hole")

    W1 = np.zeros((h1, in_dim), dtype=np.float32)
    b1 = np.zeros(h1, dtype=np.float32)
    W1[0, hole_pos] = 10.0  # amplify the hole feature

    W2 = np.eye(h2, h1, dtype=np.float32)
    b2 = np.zeros(h2, dtype=np.float32)

    W3 = np.ones((1, h2), dtype=np.float32)
    b3 = np.zeros(1, dtype=np.float32)

    weights = {
        "fc1.weight": W1.tolist(),
        "fc1.bias":   b1.tolist(),
        "fc2.weight": W2.tolist(),
        "fc2.bias":   b2.tolist(),
        "fc3.weight": W3.tolist(),
        "fc3.bias":   b3.tolist(),
    }

    cands = [
        {"sids": [], "kind": "fillet"},   # low score
        {"sids": [], "kind": "hole"},     # high score — should be first after ranking
        {"sids": [], "kind": "chamfer"},  # low score
    ]
    ranked = rank_candidates(cands, {}, 100.0, weights)
    assert len(ranked) == 3
    assert ranked[0]["kind"] == "hole", (
        f"expected 'hole' first but got {ranked[0]['kind']!r}"
    )


def test_export_load_roundtrip(tmp_path):
    """build_ranker_model -> export_weights -> load_weights -> numpy_forward works."""
    torch = pytest.importorskip("torch")
    from ranker import build_ranker_model, export_weights, load_weights

    model = build_ranker_model()
    out = str(tmp_path / "weights.json")
    export_weights(model, out)

    weights = load_weights(out)
    assert "fc1.weight" in weights
    assert "fc3.bias" in weights

    # Forward pass with 3 zero inputs
    x = [[0.0] * FEATURE_DIM for _ in range(3)]
    scores = numpy_forward(weights, x)
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)
