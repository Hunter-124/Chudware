"""Triangle mesh inputs, retaining CAD face attribution without engine dependencies.

Coordinates are millimetres in model space; winding must point out of material
(including inward toward cavity air). Degenerate triangles are discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import numpy as np


@dataclass(frozen=True)
class FaceInfo:
    sid: str | None
    body: int
    surf_type: int
    owner_feature: str | None


@dataclass
class PrintMesh:
    vertices: np.ndarray
    triangles: np.ndarray
    tri_face: np.ndarray
    faces: list[FaceInfo]

    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.triangles = np.asarray(self.triangles, dtype=np.int64).reshape(-1, 3)
        self.tri_face = np.asarray(self.tri_face, dtype=np.int32).reshape(-1)
        if not len(self.vertices) or not len(self.triangles):
            raise ValueError("print mesh must contain vertices and triangles")
        if not np.isfinite(self.vertices).all():
            raise ValueError("mesh vertices must be finite")
        if self.triangles.min() < 0 or self.triangles.max() >= len(self.vertices):
            raise ValueError("triangle vertex index out of range")
        if len(self.tri_face) != len(self.triangles):
            raise ValueError("tri_face length must match triangles")
        if np.any((self.tri_face < -1) | (self.tri_face >= len(self.faces))):
            raise ValueError("face index out of range")
        t = self.vertices[self.triangles]
        keep = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1) > 1e-12
        self.triangles = self.triangles[keep]
        self.tri_face = self.tri_face[keep]
        if not len(self.triangles):
            raise ValueError("mesh contains only degenerate triangles")

    @classmethod
    def from_arrays(cls, vertices, triangles) -> PrintMesh:
        return cls(vertices, triangles, np.zeros(len(triangles), dtype=np.int32),
                   [FaceInfo(None, 1, 6, None)])

    @classmethod
    def from_doc_mesh(cls, mesh: dict, face_matches: list[dict]) -> PrintMesh:
        matches = {(int(m.get("body", 0)), int(m.get("topo", -1))): m
                   for m in face_matches if m.get("kind", "face") == "face"}
        faces = []
        for face in mesh.get("faces", []):
            body = int(face.get("body", 0))
            match = matches.get((body, int(face.get("face_id", -1))), {})
            faces.append(FaceInfo(match.get("sid"), body, int(face.get("surf_type", 6)),
                                  match.get("owner_feature")))
        triangles = np.asarray(mesh["idx"]).reshape(-1, 3)
        tri_face = mesh.get("tri_face")
        if tri_face is None:
            tri_face = np.full(len(triangles), -1, dtype=np.int32)
            for i, face in enumerate(mesh.get("faces", [])):
                start = int(face.get("first_tri", 0))
                tri_face[start:start + int(face.get("tri_count", 0))] = i
        if not faces:
            return cls.from_arrays(mesh["pos"], triangles)
        return cls(mesh["pos"], triangles, tri_face, faces)


def load_mesh_file(path: str | Path) -> PrintMesh:
    """Load STL/3MF/OBJ/PLY, applying scene transforms and merging vertices."""
    import trimesh
    loaded = trimesh.load(path, process=True)
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("file contains no triangle mesh")
    loaded.merge_vertices()
    return PrintMesh.from_arrays(loaded.vertices, loaded.faces)
