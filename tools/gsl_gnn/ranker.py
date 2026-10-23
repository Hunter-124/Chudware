"""Micro-evaluator (NNUE-style MLP ranker) for feature-recognition hypotheses.

Ranks a list of ``candidate`` dicts emitted by the 5-pass recogniser.
The ranker re-orders only — it never generates new candidates.

``torch`` is imported lazily (only training needs it) so this
module stays importable and ``py_compile``-clean without torch installed.
``numpy_forward`` is pure-numpy only and never touches torch at all.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

# --- feature vocabularies (match the corpus labels in corpora/gsl_dataset) --

SURF_KINDS = ["plane", "cylinder", "cone", "sphere", "torus", "bspline", "other"]
CONCAVITY_KINDS = ["bore", "boss", "none"]

# Candidate "kind" vocabulary used for the kind one-hot (6 dims)
KIND_VOCAB = ["hole", "fillet", "chamfer", "circular_pattern", "linear_pattern", "other"]

# Feature dims
_NODE_DIM = len(SURF_KINDS) + 1 + 1 + len(CONCAVITY_KINDS) + 3 + 3  # 18
_AGG_DIM = _NODE_DIM * 2  # mean + max = 36
_FRAC_DIM = 1
_KIND_DIM = len(KIND_VOCAB)  # 6
FEATURE_DIM = _AGG_DIM + _FRAC_DIM + _KIND_DIM  # 43


# ---------------------------------------------------------------------------
# Lazy torch import
# ---------------------------------------------------------------------------

def _require_torch():
    """Import torch + nn, or raise a clear error."""
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "tools/gsl_gnn ranker requires torch for this call. Install it from "
            "https://pytorch.org/get-started/locally/. "
            f"Original import error: {exc}"
        ) from exc
    return torch, nn


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------

def _one_hot(value: Optional[str], vocab: list) -> list:
    vec = [0.0] * len(vocab)
    key = value if value is not None else "none"
    if key in vocab:
        vec[vocab.index(key)] = 1.0
    return vec


def _node_features(match: dict, diag: float) -> list:
    surf = _one_hot(match.get("surf"), SURF_KINDS)
    area = float(match.get("area_or_len") or 0.0) / max(diag * diag, 1e-9)
    radius = match.get("radius")
    log_radius = math.log1p(float(radius)) if radius else 0.0
    concavity = _one_hot(match.get("concavity"), CONCAVITY_KINDS)
    normal = [float(c) for c in (match.get("normal") or (0.0, 0.0, 0.0))]
    centroid = [
        float(c) / max(diag, 1e-9) for c in (match.get("centroid") or (0.0, 0.0, 0.0))
    ]
    return surf + [area, log_radius] + concavity + normal + centroid


# ---------------------------------------------------------------------------
# Candidate feature extraction
# ---------------------------------------------------------------------------

def candidate_features(
    candidate: dict,
    faces_by_sid: dict,
    diag: float,
    total_faces: Optional[int] = None,
) -> list:
    """Compute a 43-dim feature vector for a single candidate dict.

    Args:
        candidate:   dict with at least ``"sids"`` (list[str]) and ``"kind"``
        faces_by_sid: mapping sid -> face match dict (from faces.json matches)
        diag:        bounding-box diagonal of the part (for normalisation)
        total_faces: total number of faces in the part; if None, falls back to
                     len(faces_by_sid) (may be 0 → fraction becomes 0)

    Returns:
        list of 43 floats  [mean(18) | max(18) | frac(1) | kind_oh(6)]
    """
    sids = candidate.get("sids") or []
    kind = candidate.get("kind") or "other"

    # Collect per-face node features
    face_vecs: list[list[float]] = []
    for sid in sids:
        face = faces_by_sid.get(str(sid))
        if face is not None:
            face_vecs.append(_node_features(face, diag))

    # Aggregate: mean + max across claimed faces
    if face_vecs:
        n = len(face_vecs)
        mean_v = [sum(face_vecs[r][c] for r in range(n)) / n for c in range(_NODE_DIM)]
        max_v = [max(face_vecs[r][c] for r in range(n)) for c in range(_NODE_DIM)]
    else:
        mean_v = [0.0] * _NODE_DIM
        max_v = [0.0] * _NODE_DIM

    # Fraction of part faces claimed
    n_total = total_faces if total_faces is not None else len(faces_by_sid)
    frac = len(sids) / max(n_total, 1)

    # Kind one-hot
    kind_oh = _one_hot(kind, KIND_VOCAB)

    return mean_v + max_v + [frac] + kind_oh


# ---------------------------------------------------------------------------
# Model definition (torch, lazy)
# ---------------------------------------------------------------------------

def build_ranker_model(in_dim: int = FEATURE_DIM, hidden: int = 64):
    """2-layer MLP ranker: Linear(43,64)->ReLU->Linear(64,32)->ReLU->Linear(32,1).

    Returns an ``nn.Module``. Only callable when torch is installed.
    """
    torch, nn = _require_torch()

    class RankerMLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc1 = nn.Linear(in_dim, hidden)
            self.fc2 = nn.Linear(hidden, hidden // 2)
            self.fc3 = nn.Linear(hidden // 2, 1)

        def forward(self, x):
            h = torch.relu(self.fc1(x))
            h = torch.relu(self.fc2(h))
            return self.fc3(h)

    return RankerMLP()


# ---------------------------------------------------------------------------
# Weight serialisation
# ---------------------------------------------------------------------------

def export_weights(model, path: str) -> None:
    """Save model weights as JSON (each param -> nested list of floats)."""
    import numpy as np  # numpy is a hard dep of the corpus pipeline

    weights: dict = {}
    for name, param in model.named_parameters():
        weights[name] = param.detach().cpu().numpy().tolist()
    Path(path).write_text(json.dumps(weights))


def load_weights(path: str) -> dict:
    """Load weights JSON written by ``export_weights``."""
    return json.loads(Path(path).read_text())


# ---------------------------------------------------------------------------
# Pure-numpy forward pass
# ---------------------------------------------------------------------------

def numpy_forward(weights: dict, x: list) -> list:
    """Run the MLP forward pass with pure numpy (no torch).

    Args:
        weights: dict loaded by ``load_weights`` — must contain keys
                 fc1.weight, fc1.bias, fc2.weight, fc2.bias, fc3.weight, fc3.bias
        x:       list of N feature vectors, each of length FEATURE_DIM

    Returns:
        list of N scalar scores (floats)
    """
    import numpy as np

    if not x:
        return []

    arr = np.array(x, dtype=np.float32)  # (N, D)

    def _linear(inp, w_key, b_key):
        W = np.array(weights[w_key], dtype=np.float32)  # (out, in)
        b = np.array(weights[b_key], dtype=np.float32)  # (out,)
        return inp @ W.T + b

    def _relu(inp):
        return np.maximum(inp, 0.0)

    h = _relu(_linear(arr, "fc1.weight", "fc1.bias"))
    h = _relu(_linear(h, "fc2.weight", "fc2.bias"))
    h = _linear(h, "fc3.weight", "fc3.bias")  # (N, 1)

    return h[:, 0].tolist()


# ---------------------------------------------------------------------------
# Public ranking entry point
# ---------------------------------------------------------------------------

def rank_candidates(
    candidates: list,
    faces_by_sid: dict,
    diag: float,
    weights: Optional[dict],
) -> list:
    """Re-order ``candidates`` descending by ranker score.

    Falls back to the original order when:
    - ``candidates`` is empty
    - ``weights`` is None or empty (deterministic fallback)
    """
    if not candidates:
        return candidates
    if not weights:
        return candidates

    total_faces = len(faces_by_sid)
    feats = [
        candidate_features(c, faces_by_sid, diag, total_faces)
        for c in candidates
    ]
    scores = numpy_forward(weights, feats)

    ranked = sorted(
        zip(scores, range(len(candidates)), candidates),
        key=lambda t: t[0],
        reverse=True,
    )
    return [c for _, _, c in ranked]
