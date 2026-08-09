from __future__ import annotations

from typing import Mapping

import numpy as np

from .data import FEATURE_KINDS, ViewpointBatch
from .model import weighted_geometric_mean

Array = np.ndarray
RANDOM_RENDER_BUDGETS = (1, 2, 4, 8, 16, 32)




def _masked_argmax(scores: Array, mask: Array) -> Array:
    return np.argmax(np.where(mask, scores, -np.inf), axis=1).astype(np.int64)


def fixed_isometric(batch: ViewpointBatch) -> Array:
    """Nearest offered camera to the historical +X,+Y,+Z isometric view."""
    forward = batch.candidates[..., :3]
    ideal = -np.ones(3, np.float32) / np.sqrt(3.0)
    angular = np.einsum("bnc,c->bn", forward, ideal)
    return _masked_argmax(angular, batch.candidate_mask)


def best_of_n_random(batch: ViewpointBatch, n: int = 8) -> Array:
    """Oracle-select the best nested-prefix pose after N random renders.

    The hard-corpus sampler emits candidates in seeded random order. Taking
    prefixes makes random@1, @2, ... a genuine cumulative compute curve.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    result = np.empty(len(batch.part_ids), np.int64)
    actual = weighted_geometric_mean(batch.components)
    for row in range(len(batch.part_ids)):
        valid = np.flatnonzero(batch.candidate_mask[row])
        offered = valid[: min(n, len(valid))]
        result[row] = offered[int(np.argmax(actual[row, offered]))]
    return result


def greedy_geometric(batch: ViewpointBatch) -> Array:
    """Single-step hand-engineered aim/alignment/scale proxy, with no labels."""
    result = np.empty(len(batch.part_ids), np.int64)
    for row in range(len(batch.part_ids)):
        feature_tokens = batch.geometry[row, batch.feature_mask[row]]
        feature = feature_tokens.mean(axis=0)
        axis = feature[6:9]
        axis_norm = np.linalg.norm(axis)
        axis = axis / axis_norm if axis_norm > 1.0e-6 else np.asarray((0.0, 0.0, 1.0), np.float32)
        feature_kind = int(np.argmax(batch.task[row, : len(FEATURE_KINDS)]))
        direction = batch.candidates[row, :, :3]
        alignment = np.abs(direction @ axis)
        if FEATURE_KINDS[feature_kind] == "edge":
            alignment = 1.0 - alignment
        target_offset = batch.candidates[row, :, 6:9]
        centering = np.exp(-np.linalg.norm(target_offset - feature[:3], axis=-1))
        distance = np.exp(batch.candidates[row, :, 9])
        scale_proxy = np.exp(-np.abs(np.log(np.clip(distance, 1.0e-4, None)) - np.log(2.2)))
        fov_proxy = np.exp(-2.0 * np.abs(batch.candidates[row, :, 10] - (45.0 / 180.0)))
        score = 0.45 * alignment + 0.30 * centering + 0.15 * scale_proxy + 0.10 * fov_proxy
        result[row] = _masked_argmax(score[None, :], batch.candidate_mask[row:row + 1])[0]
    return result

def random_search_baselines(
    batch: ViewpointBatch,
    budgets: tuple[int, ...] = RANDOM_RENDER_BUDGETS,
) -> dict[int, Array]:
    """Return nested best-of-N random-search choices for compute comparison."""
    if tuple(sorted(set(budgets))) != budgets:
        raise ValueError("random-search budgets must be unique and increasing")
    return {n: best_of_n_random(batch, n=n) for n in budgets}



def all_baselines(
    batch: ViewpointBatch,
    *,
    random_n: int = 8,
    random_budgets: tuple[int, ...] | None = None,
) -> Mapping[str, Array]:
    result = {
        "fixed_isometric": fixed_isometric(batch),
        "greedy_geometric": greedy_geometric(batch),
    }
    budgets = (random_n,) if random_budgets is None else random_budgets
    result.update({
        f"best_of_{n}_random": indices
        for n, indices in random_search_baselines(batch, budgets).items()
    })
    return result
