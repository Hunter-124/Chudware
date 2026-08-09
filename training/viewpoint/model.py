from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

import numpy as np

Array = np.ndarray
COMPONENTS = (
    "coverage",
    "visibility",
    "foreshortening",
    "scale",
    "silhouette",
    "depth",
    "uniqueness",
)
DEFAULT_COMPONENT_WEIGHTS = np.asarray((1.0, 1.25, 1.0, 1.0, 0.75, 1.25, 0.5), np.float32)



@dataclass(frozen=True)
class ModelSpec:
    """Static tensor contract for the candidate-ranking policy."""

    task_dim: int = 14
    geometry_dim: int = 18
    candidate_dim: int = 48
    hidden_dim: int = 96
    component_count: int = len(COMPONENTS)

    def validate(self) -> "ModelSpec":
        for item in fields(self):
            if int(getattr(self, item.name)) <= 0:
                raise ValueError(f"{item.name} must be positive")
        if self.component_count != len(COMPONENTS):
            raise ValueError(f"component_count must be {len(COMPONENTS)}")
        return self


@dataclass(frozen=True)
class PolicyWeights:
    task_w: Array
    task_b: Array
    geom_k_w: Array
    geom_k_b: Array
    geom_v_w: Array
    geom_v_b: Array
    cand_w: Array
    cand_b: Array
    query_w: Array
    query_b: Array
    feature_bias: Array
    fuse_w: Array
    fuse_b: Array
    out_w: Array
    out_b: Array

    @classmethod
    def random(cls, spec: ModelSpec, seed: int = 0) -> "PolicyWeights":
        spec.validate()
        rng = np.random.default_rng(seed)

        def matrix(n_in: int, n_out: int) -> Array:
            limit = np.sqrt(6.0 / (n_in + n_out))
            return rng.uniform(-limit, limit, (n_in, n_out)).astype(np.float32)

        h = spec.hidden_dim
        return cls(
            task_w=matrix(spec.task_dim, h), task_b=np.zeros(h, np.float32),
            geom_k_w=matrix(spec.geometry_dim, h), geom_k_b=np.zeros(h, np.float32),
            geom_v_w=matrix(spec.geometry_dim, h), geom_v_b=np.zeros(h, np.float32),
            cand_w=matrix(spec.candidate_dim, h), cand_b=np.zeros(h, np.float32),
            query_w=matrix(2 * h, h), query_b=np.zeros(h, np.float32),
            feature_bias=np.asarray(1.0, np.float32),
            fuse_w=matrix(5 * h, h), fuse_b=np.zeros(h, np.float32),
            out_w=matrix(h, 1)[:, 0], out_b=np.asarray(0.0, np.float32),
        )

    def arrays(self) -> Mapping[str, Array]:
        return {item.name: np.asarray(getattr(self, item.name), np.float32) for item in fields(self)}

    def save(self, path: str | Path, spec: ModelSpec) -> None:
        payload = dict(self.arrays())
        payload["spec"] = np.asarray(
            [spec.task_dim, spec.geometry_dim, spec.candidate_dim, spec.hidden_dim, spec.component_count],
            np.int64,
        )
        np.savez_compressed(path, **payload)

    @classmethod
    def load(cls, path: str | Path) -> tuple["PolicyWeights", ModelSpec]:
        with np.load(path, allow_pickle=False) as payload:
            values = payload["spec"].astype(np.int64).tolist()
            spec = ModelSpec(*values).validate()
            weights = cls(**{item.name: payload[item.name].astype(np.float32) for item in fields(cls)})
        return weights, spec


def _silu(value: Array) -> Array:
    clipped = np.clip(value, -30.0, 30.0)
    return value / (1.0 + np.exp(-clipped))


def weighted_geometric_mean(components: Array, weights: Array | None = None) -> Array:
    values = np.asarray(components, np.float32)
    if values.shape[-1] != len(COMPONENTS):
        raise ValueError(f"last component dimension must be {len(COMPONENTS)}")
    component_weights = DEFAULT_COMPONENT_WEIGHTS if weights is None else np.asarray(weights, np.float32)
    if component_weights.shape != (len(COMPONENTS),) or np.any(component_weights < 0) or component_weights.sum() <= 0:
        raise ValueError("weights must be non-negative with positive sum")
    score = np.exp(np.sum(np.log(np.clip(values, 1.0e-6, 1.0)) * component_weights, axis=-1) / component_weights.sum())
    return np.where(np.any(values <= 0, axis=-1), 0.0, score).astype(np.float32)


class NumpyViewpointPolicy:
    """Dependency-light reference forward pass for feature-conditioned scoring.

    Geometry tokens are an unordered set. Candidate-conditioned cross-attention
    lets each offered pose ask a different layout question, while the candidate
    vector carries explicit coarse depth and visibility evidence. The scalar
    output is an unconstrained ranking logit, not a regressed pose or oracle score.
    """

    def __init__(self, weights: PolicyWeights, spec: ModelSpec | None = None):
        self.spec = (spec or ModelSpec()).validate()
        self.weights = weights
        self._validate_weights()

    @classmethod
    def random(cls, spec: ModelSpec | None = None, seed: int = 0) -> "NumpyViewpointPolicy":
        actual = (spec or ModelSpec()).validate()
        return cls(PolicyWeights.random(actual, seed), actual)

    def _validate_weights(self) -> None:
        s, w, h = self.spec, self.weights, self.spec.hidden_dim
        expected = {
            "task_w": (s.task_dim, h), "task_b": (h,),
            "geom_k_w": (s.geometry_dim, h), "geom_k_b": (h,),
            "geom_v_w": (s.geometry_dim, h), "geom_v_b": (h,),
            "cand_w": (s.candidate_dim, h), "cand_b": (h,),
            "query_w": (2 * h, h), "query_b": (h,), "feature_bias": (),
            "fuse_w": (5 * h, h), "fuse_b": (h,),
            "out_w": (h,), "out_b": (),
        }
        for name, shape in expected.items():
            value = np.asarray(getattr(w, name))
            if value.shape != shape:
                raise ValueError(f"{name} has shape {value.shape}, expected {shape}")
            if not np.isfinite(value).all():
                raise ValueError(f"{name} contains non-finite values")

    def forward(
        self,
        task: Array,
        geometry: Array,
        geometry_mask: Array,
        feature_mask: Array,
        candidates: Array,
        candidate_mask: Array | None = None,
    ) -> Array:
        task = np.asarray(task, np.float32)
        geometry = np.asarray(geometry, np.float32)
        geometry_mask = np.asarray(geometry_mask, bool)
        feature_mask = np.asarray(feature_mask, bool)
        candidates = np.asarray(candidates, np.float32)
        if task.ndim != 2 or geometry.ndim != 3 or candidates.ndim != 3:
            raise ValueError("task, geometry, candidates must have ranks 2, 3, 3")
        b, tokens, gd = geometry.shape
        if task.shape != (b, self.spec.task_dim) or gd != self.spec.geometry_dim:
            raise ValueError("task or geometry shape violates ModelSpec")
        if candidates.shape[0] != b or candidates.shape[2] != self.spec.candidate_dim:
            raise ValueError("candidate shape violates ModelSpec")
        if geometry_mask.shape != (b, tokens) or feature_mask.shape != (b, tokens):
            raise ValueError("geometry_mask and feature_mask must match geometry tokens")
        if np.any(feature_mask & ~geometry_mask) or np.any(~geometry_mask.any(axis=1)) or np.any(~feature_mask.any(axis=1)):
            raise ValueError("each item needs geometry and a marked valid feature token")
        if not np.isfinite(task).all() or not np.isfinite(geometry).all() or not np.isfinite(candidates).all():
            raise ValueError("policy inputs must be finite")

        w = self.weights
        task_h = _silu(task @ w.task_w + w.task_b)
        keys = _silu(geometry @ w.geom_k_w + w.geom_k_b)
        values = _silu(geometry @ w.geom_v_w + w.geom_v_b)
        cand_h = _silu(candidates @ w.cand_w + w.cand_b)
        repeated_task = np.broadcast_to(task_h[:, None, :], cand_h.shape)
        query = _silu(np.concatenate((cand_h, repeated_task), axis=-1) @ w.query_w + w.query_b)
        logits = np.einsum("bnh,bgh->bng", query, keys) / np.sqrt(self.spec.hidden_dim)
        logits = logits + feature_mask[:, None, :].astype(np.float32) * w.feature_bias
        logits = np.where(geometry_mask[:, None, :], logits, -1.0e9)
        logits = logits - logits.max(axis=-1, keepdims=True)
        attention = np.exp(logits)
        attention /= attention.sum(axis=-1, keepdims=True)
        context = np.einsum("bng,bgh->bnh", attention, values)
        fusion = np.concatenate((cand_h, repeated_task, context, cand_h * context, np.abs(cand_h - context)), axis=-1)
        hidden = _silu(fusion @ w.fuse_w + w.fuse_b)
        output = (hidden @ w.out_w + w.out_b).astype(np.float32)
        if candidate_mask is not None:
            valid = np.asarray(candidate_mask, bool)
            if valid.shape != candidates.shape[:2] or np.any(~valid.any(axis=1)):
                raise ValueError("candidate_mask must match candidates and leave one valid candidate per item")
            output = np.where(valid, output, -np.inf)
        return output

    def select(self, *args: Array, **kwargs: Array) -> tuple[Array, Array]:
        scores = self.forward(*args, **kwargs)
        selected = np.argmax(scores, axis=1).astype(np.int64)
        return selected, scores
