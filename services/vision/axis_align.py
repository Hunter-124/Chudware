"""Estimate a proper rotation from a reconstruction gauge to world axes.

Plane normals are unoriented: parallel and antiparallel patches contribute to the
same dominant direction. PCA is a geometric fallback, not evidence that a curved
or incomplete scan has the intended manufacturing datum axes.
"""
from __future__ import annotations

import numpy as np


_PLANE_TOLERANCE_DEG = 15.0
_MAX_PLANE_CANDIDATES = 256


def _proper_rotation(rotation) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=float)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6, rtol=0)
            or not np.isclose(np.linalg.det(matrix), 1.0, atol=1e-6, rtol=0)):
        raise ValueError("rotation must be a finite proper orthonormal 3x3 matrix")
    return matrix


def _orient(axis: np.ndarray) -> np.ndarray:
    """Choose a repeatable sign without changing the underlying unoriented axis."""
    return -axis if axis[np.argmax(np.abs(axis))] < 0 else axis


def _pca_rotation(points) -> np.ndarray:
    values = np.asarray(points, dtype=float)
    if values.ndim != 2 or values.shape[1] != 3 or len(values) < 3:
        raise ValueError("PCA alignment requires at least three 3D points")
    if not np.isfinite(values).all():
        raise ValueError("alignment points must be finite")
    centered = values - values.mean(axis=0)
    eigenvalues, axes = np.linalg.eigh(centered.T @ centered)
    if eigenvalues[-1] <= 1e-20 or eigenvalues[-2] <= eigenvalues[-1] * 1e-12:
        raise ValueError("PCA alignment requires non-collinear points")
    axes = axes[:, ::-1]
    x = _orient(axes[:, 0])
    y = _orient(axes[:, 1])
    z = np.cross(x, y)
    return np.stack((x, y, z))


def _plane_rotation(normals, weights) -> np.ndarray:
    values = np.asarray(normals, dtype=float)
    if values.ndim != 2 or values.shape[1] != 3 or len(values) < 2:
        raise ValueError("at least two orthogonal plane directions are needed")
    lengths = np.linalg.norm(values, axis=1)
    if weights is None:
        weight = np.ones(len(values), dtype=float)
    else:
        weight = np.asarray(weights, dtype=float)
        if weight.shape != (len(values),):
            raise ValueError("plane weights must match the normals")
    valid = (np.isfinite(values).all(axis=1) & np.isfinite(weight)
             & (lengths > 1e-12) & (weight > 0))
    values, weight, lengths = values[valid], weight[valid], lengths[valid]
    if len(values) < 2:
        raise ValueError("at least two finite weighted plane directions are needed")
    values = values / lengths[:, None]
    candidates = values[np.argsort(weight)[-min(len(weight), _MAX_PLANE_CANDIDATES):]]
    cosine = np.cos(np.deg2rad(_PLANE_TOLERANCE_DEG))
    support = np.empty(len(candidates))
    # Bound temporary memory even for large triangle meshes.
    for start in range(0, len(candidates), 16):
        dots = np.abs(values @ candidates[start:start + 16].T)
        support[start:start + 16] = (dots >= cosine).T @ weight
    first = candidates[int(np.argmax(support))]
    orthogonal = np.abs(candidates @ first) <= np.sin(np.deg2rad(_PLANE_TOLERANCE_DEG))
    if not orthogonal.any():
        raise ValueError("no dominant orthogonal plane pair was found")
    second_index = np.flatnonzero(orthogonal)[np.argmax(support[orthogonal])]
    second = candidates[second_index]

    def average_direction(reference):
        dots = values @ reference
        selected = np.abs(dots) >= cosine
        mean = ((values[selected] * np.sign(dots[selected, None]))
                * weight[selected, None]).sum(axis=0)
        return mean / np.linalg.norm(mean)

    x = _orient(average_direction(first))
    y = average_direction(second)
    y = y - np.dot(y, x) * x
    y = _orient(y / np.linalg.norm(y))
    z = np.cross(x, y)
    return np.stack((x, y, z))


def align_axes(points=None, *, method="planes", normals=None, weights=None, rotation=None) -> dict:
    """Return a proper row-major native-to-world rotation and the method actually used.

    ``planes`` uses area/inlier-weighted plane normals; if no orthogonal pair is
    supported it falls back to PCA of ``points``. ``manual`` takes an absolute
    native-to-world rotation, not a rotation relative to the old session gauge.
    """
    if method == "manual":
        if rotation is None:
            raise ValueError("manual alignment requires rotation")
        matrix = _proper_rotation(rotation)
        return {"rotation": matrix.tolist(), "method": "manual"}
    if method == "pca":
        return {"rotation": _pca_rotation(points).tolist(), "method": "pca"}
    if method != "planes":
        raise ValueError("method must be planes, pca or manual")
    try:
        matrix = _plane_rotation(normals, weights)
        return {"rotation": matrix.tolist(), "method": "planes"}
    except (TypeError, ValueError) as exc:
        matrix = _pca_rotation(points)
        return {"rotation": matrix.tolist(), "method": "pca", "fallback_reason": str(exc)}
