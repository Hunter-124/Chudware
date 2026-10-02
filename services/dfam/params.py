"""Process parameters and deterministic model-to-build rotation.

Build direction points away from the bed. The antiparallel case rotates about
model X, avoiding an arbitrary numerical axis when Rodrigues is singular.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True)
class PrintParams:
    process: str = "fdm"
    nozzle_mm: float = 0.4
    line_width_mm: float = 0.45
    layer_height_mm: float = 0.2
    overhang_max_deg: float = 45.0
    max_bridge_mm: float = 10.0
    min_wall_mm: float = 1.0
    min_hole_mm: float = 1.0
    small_hole_warn_mm: float = 3.0
    teardrop_hole_mm: float = 6.0
    bed_size_mm: tuple[float, float, float] | None = None
    material: str = "PLA"
    walls: int = 3
    top_bottom_mm: float = 0.8
    infill_fraction: float = 0.15
    support_density: float = 0.15
    filament_density_g_cm3: float = 1.24
    volumetric_speed_mm3_s: float = 8.0
    access_mm: float = 3.0

    def __post_init__(self) -> None:
        for name in ("nozzle_mm", "line_width_mm", "layer_height_mm", "max_bridge_mm",
                     "min_wall_mm", "min_hole_mm", "small_hole_warn_mm", "teardrop_hole_mm",
                     "top_bottom_mm", "filament_density_g_cm3", "volumetric_speed_mm3_s", "access_mm"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not 0 <= self.overhang_max_deg <= 90:
            raise ValueError("overhang_max_deg must be between 0 and 90")
        for name in ("infill_fraction", "support_density"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.walls < 1 or int(self.walls) != self.walls:
            raise ValueError("walls must be a positive integer")
        if self.bed_size_mm is not None:
            if len(self.bed_size_mm) != 3 or any(not math.isfinite(x) or x <= 0 for x in self.bed_size_mm):
                raise ValueError("bed_size_mm must contain three positive finite dimensions")


def frame_for_build_dir(build_dir, yaw_deg: float = 0.0) -> np.ndarray:
    direction = np.asarray(build_dir, dtype=np.float64)
    if direction.shape != (3,) or not np.isfinite(direction).all() or np.linalg.norm(direction) < 1e-12:
        raise ValueError("build_dir must be a finite nonzero 3-vector")
    direction = direction / np.linalg.norm(direction)
    axis = np.cross(direction, [0., 0., 1.])
    cosine = float(direction[2])
    if cosine < -1 + 1e-12:
        rotation = np.diag([1., -1., -1.])
    elif cosine > 1 - 1e-12:
        rotation = np.eye(3)
    else:
        k = np.array([[0., -axis[2], axis[1]], [axis[2], 0., -axis[0]], [-axis[1], axis[0], 0.]])
        rotation = np.eye(3) + k + k @ k / (1 + cosine)
    yaw = math.radians(yaw_deg)
    around_z = np.array([[math.cos(yaw), -math.sin(yaw), 0.],
                         [math.sin(yaw), math.cos(yaw), 0.], [0., 0., 1.]])
    return around_z @ rotation
