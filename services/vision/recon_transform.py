"""Shared similarity transform from reconstruction coordinates to millimetres.

Points are row vectors at the Python boundary; the mathematical convention is
``x_mm = mm_per_unit * rotation @ x + translation_mm``.  The same transform
must be applied to every artifact belonging to one reconstruction gauge.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass
class ReconTransform:
    mm_per_unit: float
    rotation: list[list[float]]
    translation_mm: list[float]
    source: str
    residual_mm: float | None = None
    confirmed: bool = False

    def __post_init__(self) -> None:
        self.mm_per_unit = float(self.mm_per_unit)
        rotation = np.asarray(self.rotation, dtype=np.float64)
        translation = np.asarray(self.translation_mm, dtype=np.float64)
        if not math.isfinite(self.mm_per_unit) or self.mm_per_unit <= 0:
            raise ValueError("mm_per_unit must be finite and positive")
        if (rotation.shape != (3, 3) or not np.isfinite(rotation).all()
                or not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0, atol=1e-6)
                or not np.isclose(np.linalg.det(rotation), 1, rtol=0, atol=1e-6)):
            raise ValueError("rotation must be a finite proper orthonormal 3x3 matrix")
        if translation.shape != (3,) or not np.isfinite(translation).all():
            raise ValueError("translation_mm must contain three finite values")
        if self.residual_mm is not None:
            self.residual_mm = float(self.residual_mm)
            if not math.isfinite(self.residual_mm) or self.residual_mm < 0:
                raise ValueError("residual_mm must be finite and nonnegative")
        self.rotation = rotation.tolist()
        self.translation_mm = translation.tolist()

    def apply(self, points: np.ndarray) -> np.ndarray:
        """Transform an array of points whose final dimension is three."""
        values = np.asarray(points, dtype=np.float64)
        if values.ndim == 0 or values.shape[-1] != 3:
            raise ValueError("points must have a final dimension of three")
        return self.mm_per_unit * (values @ np.asarray(self.rotation).T) + self.translation_mm

    def to_json(self) -> dict:
        return {
            "mm_per_unit": self.mm_per_unit,
            "rotation": [row[:] for row in self.rotation],
            "translation_mm": self.translation_mm[:],
            "source": self.source,
            "residual_mm": self.residual_mm,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_json(cls, d: dict) -> ReconTransform:
        return cls(mm_per_unit=d["mm_per_unit"], rotation=d["rotation"],
                   translation_mm=d["translation_mm"], source=d["source"],
                   residual_mm=d.get("residual_mm"), confirmed=d.get("confirmed", False))

    @classmethod
    def identity(cls) -> ReconTransform:
        return cls(mm_per_unit=1.0, rotation=[[1.0, 0.0, 0.0],
                                             [0.0, 1.0, 0.0],
                                             [0.0, 0.0, 1.0]],
                   translation_mm=[0.0, 0.0, 0.0], source="identity")
