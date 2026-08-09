"""Leak-resistant public types for rendered viewpoint corpora."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import numpy as np

PIXEL_SCHEMA_VERSION = "chudware.viewpoint.pixels/1"
COMPONENTS = ("coverage", "visibility", "foreshortening", "scale", "silhouette", "depth", "uniqueness")
SPLITS = ("train", "validation", "test")


def _array(value: object, dtype: object, *, ndim: int, name: str) -> np.ndarray:
    result = np.ascontiguousarray(value, dtype=dtype)
    if result.ndim != ndim:
        raise ValueError(f"{name} must have {ndim} dimensions")
    return result


@dataclass(frozen=True, slots=True)
class PixelCorpusConfig:
    """Deterministic corpus configuration aligned to the frozen limited-observation protocol.

    The default 96x96 pixels and camera intrinsics are exactly the candidate image size used
    by ``training.viewpoint.observation_data.LimitedCorpusConfig``.
    """

    parts: int = 522
    examples_per_part: int = 6
    candidates: int = 16
    seed: int = 1729
    resolution: tuple[int, int] = (96, 96)
    oracle_image_size: tuple[int, int] = (96, 96)
    cache_dir: Path = Path("scratch/viewpoint-pixels-large")
    include_depth: bool = True
    part_indices: tuple[int, ...] | None = None

    def validate(self) -> "PixelCorpusConfig":
        if self.parts < 3 or self.examples_per_part <= 0 or self.candidates <= 0:
            raise ValueError("corpus requires at least three parts and positive row/candidate counts")
        if len(self.resolution) != 2 or min(self.resolution) < 64 or max(self.resolution) > 512:
            raise ValueError("resolution must contain two values in [64, 512]")
        if len(self.oracle_image_size) != 2 or min(self.oracle_image_size) <= 0:
            raise ValueError("oracle_image_size must contain two positive values")
        if abs(self.resolution[0] / self.resolution[1] - self.oracle_image_size[0] / self.oracle_image_size[1]) > 1e-9:
            raise ValueError("render and oracle image sizes must have the same aspect ratio")
        if self.part_indices is not None:
            if len(self.part_indices) != self.parts or len(set(self.part_indices)) != self.parts:
                raise ValueError("part_indices must contain exactly `parts` unique values")
            if any(index < 0 for index in self.part_indices):
                raise ValueError("part_indices must be non-negative")
        return self


@dataclass(frozen=True, slots=True)
class PixelInputs:
    """Inference boundary with query metadata structurally separate from sensor observations."""

    rgb: np.ndarray
    depth: np.ndarray | None
    depth_mask: np.ndarray | None
    cameras: np.ndarray
    coarse_prior: np.ndarray
    task: np.ndarray
    candidate_mask: np.ndarray

    def validate(self) -> "PixelInputs":
        rgb = _array(self.rgb, np.uint8, ndim=5, name="rgb")
        batch, candidates, height, width, channels = rgb.shape
        if channels != 3:
            raise ValueError("rgb must have shape (B, N, H, W, 3)")
        cameras = _array(self.cameras, np.float32, ndim=3, name="cameras")
        prior = _array(self.coarse_prior, np.float32, ndim=3, name="coarse_prior")
        task = _array(self.task, np.float32, ndim=2, name="task")
        mask = _array(self.candidate_mask, bool, ndim=2, name="candidate_mask")
        if cameras.shape != (batch, candidates, 16):
            raise ValueError("cameras must have shape (B, N, 16)")
        if prior.shape != (batch, candidates, 8):
            raise ValueError("coarse_prior must have shape (B, N, 8)")
        if task.shape != (batch, 14) or mask.shape != (batch, candidates):
            raise ValueError("task/candidate_mask shape does not match rgb")
        if not np.isfinite(cameras).all() or not np.isfinite(prior).all() or not np.isfinite(task).all():
            raise ValueError("inference tensors must be finite")
        if self.depth is None or self.depth_mask is None:
            if self.depth is not None or self.depth_mask is not None:
                raise ValueError("depth and depth_mask must either both be present or both be absent")
        else:
            depth = _array(self.depth, np.float32, ndim=4, name="depth")
            depth_mask = _array(self.depth_mask, bool, ndim=4, name="depth_mask")
            if depth.shape != (batch, candidates, height, width) or depth_mask.shape != depth.shape:
                raise ValueError("depth tensors must match rgb's B,N,H,W dimensions")
            if not np.isfinite(depth).all() or np.any(depth < 0.0) or np.any(depth > 1.0):
                raise ValueError("depth must be finite normalized camera depth in [0, 1]")
            if np.any(depth[~depth_mask] != 0.0):
                raise ValueError("invalid depth pixels must be zero")
        return self


@dataclass(frozen=True, slots=True)
class PixelTargets:
    """Training/evaluation-only data.  It is never reachable from :class:`PixelInputs`."""

    components: np.ndarray
    baseline_indices: Mapping[str, np.ndarray] = field(default_factory=dict)

    def validate(self, *, batch: int, candidates: int) -> "PixelTargets":
        components = _array(self.components, np.float32, ndim=3, name="components")
        if components.shape != (batch, candidates, len(COMPONENTS)):
            raise ValueError("components must have shape (B, N, 7)")
        if not np.isfinite(components).all() or np.any((components < 0.0) | (components > 1.0)):
            raise ValueError("component targets must be finite and in [0, 1]")
        for name, values in self.baseline_indices.items():
            indices = _array(values, np.int64, ndim=1, name=f"baseline_indices[{name}]")
            if indices.shape != (batch,) or np.any(indices < 0) or np.any(indices >= candidates):
                raise ValueError(f"baseline {name!r} has invalid candidate indices")
        return self


@dataclass(frozen=True, slots=True)
class PixelCorpus:
    """Materialized training corpus; call :meth:`model_inputs` at the inference seam."""

    inputs: PixelInputs
    targets: PixelTargets
    part_ids: tuple[str, ...]
    difficulties: tuple[str, ...]
    splits: tuple[str, ...]
    manifest_path: Path
    renderer_version: str
    oracle_version: str
    generation_seconds: float
    cache_hits: int = 0

    def validate(self) -> "PixelCorpus":
        self.inputs.validate()
        batch, candidates = self.inputs.rgb.shape[:2]
        self.targets.validate(batch=batch, candidates=candidates)
        if len(self.part_ids) != batch or len(self.difficulties) != batch or len(self.splits) != batch:
            raise ValueError("metadata needs one item per corpus row")
        if any(not item for item in self.part_ids) or any(split not in SPLITS for split in self.splits):
            raise ValueError("invalid corpus metadata")
        split_parts = {split: {part for part, row_split in zip(self.part_ids, self.splits) if row_split == split} for split in SPLITS}
        if split_parts["train"] & split_parts["validation"] or split_parts["train"] & split_parts["test"] or split_parts["validation"] & split_parts["test"]:
            raise ValueError("part identities are mixed across splits")
        return self

    def model_inputs(self) -> PixelInputs:
        """Return only camera-observable inputs; no target key or label reference exists here."""
        return self.inputs

    @property
    def baseline_indices(self) -> Mapping[str, np.ndarray]:
        return MappingProxyType(dict(self.targets.baseline_indices))


__all__ = [
    "COMPONENTS", "PIXEL_SCHEMA_VERSION", "SPLITS", "PixelCorpus", "PixelCorpusConfig",
    "PixelInputs", "PixelTargets",
]
