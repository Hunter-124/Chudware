"""Python mesh body — a non-parametric triangle-soup body.

The Python format helpers represent imported meshes (STL / OBJ / PLY / 3MF / glTF,
and Rhino / SolidWorks mesh fallbacks) as ``MeshBody`` rather than an OCCT shape.
It carries raw vertices + triangles and exposes ``.wrapped = None`` so callers
can distinguish it from a B-rep without inventing face/edge topology.

:func:`services.formats.formats.export_document` re-serialises it via the trimesh
mesh writers. The helpers below compute normals, volume, area and centroid from
triangles. This module imports numpy only (no OCP).

Native document import/export is separate and lives in ``core/doc/formats.cpp``;
this Python representation does not register native document or tessellation RPCs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Mesh math (module-level so callers can use them without a MeshBody instance)
# ---------------------------------------------------------------------------
def face_normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-triangle unit normals, shape (M, 3). Degenerate tris get a zero normal."""
    if len(tris) == 0:
        return np.zeros((0, 3), dtype=np.float64)
    v0 = verts[tris[:, 0]].astype(np.float64)
    v1 = verts[tris[:, 1]].astype(np.float64)
    v2 = verts[tris[:, 2]].astype(np.float64)
    n = np.cross(v1 - v0, v2 - v0)
    lens = np.linalg.norm(n, axis=1)
    lens[lens == 0.0] = 1.0
    return n / lens[:, None]


def compute_vertex_normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Area-weighted per-vertex normals, shape (N, 3) float32 (smooth shading)."""
    n = np.zeros((len(verts), 3), dtype=np.float64)
    if len(tris) == 0:
        return n.astype(np.float32)
    v0 = verts[tris[:, 0]].astype(np.float64)
    v1 = verts[tris[:, 1]].astype(np.float64)
    v2 = verts[tris[:, 2]].astype(np.float64)
    fn = np.cross(v1 - v0, v2 - v0)  # un-normalised → area-weighted accumulation
    for k in range(3):
        np.add.at(n, tris[:, k], fn)
    lens = np.linalg.norm(n, axis=1)
    lens[lens == 0.0] = 1.0
    return (n / lens[:, None]).astype(np.float32)


def mesh_volume(verts: np.ndarray, tris: np.ndarray) -> float:
    """Enclosed volume via the divergence theorem: V = 1/6 |Σ v0·(v1×v2)|.

    Meaningful only for a closed mesh; an open mesh returns a non-physical number (the caller
    should treat it as an estimate)."""
    if len(tris) == 0:
        return 0.0
    v0 = verts[tris[:, 0]].astype(np.float64)
    v1 = verts[tris[:, 1]].astype(np.float64)
    v2 = verts[tris[:, 2]].astype(np.float64)
    return abs(float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum()) / 6.0)


def mesh_area(verts: np.ndarray, tris: np.ndarray) -> float:
    """Total surface area = Σ ½‖(v1−v0)×(v2−v0)‖."""
    if len(tris) == 0:
        return 0.0
    v0 = verts[tris[:, 0]].astype(np.float64)
    v1 = verts[tris[:, 1]].astype(np.float64)
    v2 = verts[tris[:, 2]].astype(np.float64)
    return float(0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1).sum())


def mesh_centroid(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Volume centroid via signed tetrahedra from the origin (closed-mesh assumption).

    Falls back to the vertex centroid when the enclosed volume is ~0 (open / degenerate mesh)."""
    if len(tris) == 0:
        return np.zeros(3, dtype=np.float64)
    v0 = verts[tris[:, 0]].astype(np.float64)
    v1 = verts[tris[:, 1]].astype(np.float64)
    v2 = verts[tris[:, 2]].astype(np.float64)
    # Signed volume of each tetra (origin, v0, v1, v2) and its centroid (mean of the 4 pts, with
    # origin contributing 0). Weighted mean gives the solid centroid.
    sv = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    tet_centroid = (v0 + v1 + v2) / 4.0
    total = sv.sum()
    if abs(total) < 1e-12:
        return verts.astype(np.float64).mean(axis=0) if len(verts) else np.zeros(3)
    return (tet_centroid * sv[:, None]).sum(axis=0) / total


def mesh_mass_props(verts: np.ndarray, tris: np.ndarray, density_g_cm3: float = 7.85) -> dict:
    """Mass-property dict for a mesh body (same shape as :func:`evaluate.mass_props`).

    Volume / mass / centre-of-mass are computed from the triangles (closed-mesh assumption).
    Inertia is reported as zeros with ``inertia_approx: True`` — a dumb mesh body does not carry the
    B-rep needed for an exact tensor, and we prefer an honest flag to a wrong number."""
    verts = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    vol = mesh_volume(verts, tris)
    density_g_mm3 = density_g_cm3 * 1e-3
    com = mesh_centroid(verts, tris)
    return {
        "volume_mm3": float(vol),
        "mass_g": float(vol * density_g_mm3),
        "com": [float(c) for c in com],
        "area_mm2": mesh_area(verts, tris),
        "inertia": [0.0] * 9,
        "inertia_approx": True,
        "is_mesh": True,
        "material": {"density_g_cm3": float(density_g_cm3)},
    }


# ---------------------------------------------------------------------------
# MeshBody carrier
# ---------------------------------------------------------------------------
@dataclass
class MeshBody:
    """A triangle-soup body: ``verts`` (N,3 float32) + ``tris`` (M,3 uint32).

    ``normals`` (N,3) is optional — computed on demand. ``color`` is an sRGB triple in [0,1] or
    None. ``wrapped`` mirrors the build123d ``Shape.wrapped`` duck-type and is ALWAYS None (a mesh
    has no OCCT shape), which is exactly what the compiler's ``_topo_map`` guard checks."""

    verts: np.ndarray
    tris: np.ndarray
    name: str = "Body1"
    normals: Optional[np.ndarray] = None
    color: Optional[tuple] = None

    def __post_init__(self) -> None:
        self.verts = np.asarray(self.verts, dtype=np.float32).reshape(-1, 3)
        self.tris = np.asarray(self.tris, dtype=np.uint32).reshape(-1, 3)
        if self.normals is not None:
            self.normals = np.asarray(self.normals, dtype=np.float32).reshape(-1, 3)
            if len(self.normals) != len(self.verts):
                self.normals = None
        # OCCT shape duck-type: a mesh carries no B-rep topology.
        self.wrapped = None

    # -- derived geometry --------------------------------------------------
    def vertex_normals(self) -> np.ndarray:
        if self.normals is None or len(self.normals) != len(self.verts):
            self.normals = compute_vertex_normals(self.verts, self.tris)
        return self.normals

    @property
    def volume(self) -> float:
        return mesh_volume(self.verts, self.tris)

    @property
    def area(self) -> float:
        return mesh_area(self.verts, self.tris)

    def bbox(self) -> tuple[tuple, tuple]:
        if len(self.verts) == 0:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        lo = self.verts.min(axis=0)
        hi = self.verts.max(axis=0)
        return tuple(float(x) for x in lo), tuple(float(x) for x in hi)

    @property
    def triangle_count(self) -> int:
        return int(len(self.tris))

    def transformed(self, matrix: np.ndarray) -> "MeshBody":
        """Return a copy with a 4×4 row-major affine ``matrix`` applied to the vertices.

        Used for assembly placement (a mesh-imported component placed in world space). Normals are
        rotated by the upper-left 3×3 and re-normalised; a non-uniform/mirror transform is handled
        by recomputing normals from the transformed triangles instead."""
        m = np.asarray(matrix, dtype=np.float64).reshape(4, 4)
        v = self.verts.astype(np.float64)
        vt = v @ m[:3, :3].T + m[:3, 3]
        return MeshBody(verts=vt.astype(np.float32), tris=self.tris.copy(),
                        name=self.name, normals=None, color=self.color)
