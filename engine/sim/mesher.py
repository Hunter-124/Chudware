"""Mesh STEP geometry with gmsh, returning first-order tet4 elements.

License note: gmsh is GPL-2+ — this module (inside engine/sim) is the ONLY place
it may be imported.  The sim worker runs in a separate process from the doc worker,
so GPL isolation is maintained at the process boundary.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class MeshResult:
    """Result of meshing a STEP file.

    points    : (n, 3) float64 — node coordinates in mm
    tets      : (m, 4) int32   — tet connectivity (zero-based node indices, first-order tet4)
    mesh_path : path to saved .msh file (gmsh format 2.2)
    char_len_mm: characteristic element length used
    n_nodes   : number of nodes
    n_elems   : number of tet4 elements
    """
    points: np.ndarray
    tets: np.ndarray
    mesh_path: str
    char_len_mm: float
    n_nodes: int
    n_elems: int


def mesh_step(step_path: str, density: float = 0.5) -> MeshResult:
    """Mesh a STEP file and return a MeshResult.

    Parameters
    ----------
    step_path : str
        Path to the input .step / .stp file (mm units).
    density : float
        Mesh density in [0, 1].  0 = coarsest (25% of bbox diagonal),
        1 = finest (5% of bbox diagonal).  Linearly interpolated.

    Returns
    -------
    MeshResult
        Node array, tet connectivity, saved .msh path, and mesh statistics.

    Notes
    -----
    Runs gmsh in-process with try/finally finalize to avoid leaving gmsh in an
    initialized state on error.  gmsh must ONLY be imported inside engine/sim.
    """
    # Import gmsh only here — GPL isolation
    import gmsh  # type: ignore[import]

    step_path = str(step_path)
    density = float(np.clip(density, 0.0, 1.0))

    # Derive a .msh output path next to the step file
    base = Path(step_path).with_suffix(".msh")
    mesh_path = str(base)

    try:
        gmsh.initialize()
        gmsh.option.setNumber("General.Verbosity", 1)

        gmsh.model.add("model")
        gmsh.model.occ.importShapes(step_path)
        gmsh.model.occ.synchronize()

        # Compute bounding box to derive characteristic length
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        side_lens = [abs(xmax - xmin), abs(ymax - ymin), abs(zmax - zmin)]
        # Use the smallest non-zero bounding-box dimension as the reference so
        # that thin features (e.g. a beam with a small cross-section) receive a
        # mesh that is fine enough relative to that dimension.  For roughly
        # equiaxed geometries this converges to a fraction of the bbox diagonal.
        min_dim = min(d for d in side_lens if d > 1e-6)

        # lerp: density=0 → 0.25 × min_dim,  density=1 → 0.05 × min_dim
        char_len = min_dim * (0.25 - 0.20 * density)

        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", char_len * 0.1)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", char_len)
        # Force first-order tets (tet4); skfem P2 enrichment is done in-solver
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)  # Delaunay 3D

        gmsh.model.mesh.generate(3)
        gmsh.write(mesh_path)

        # Extract nodes and tet4 elements
        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        coords = np.array(coords, dtype=np.float64).reshape(-1, 3)

        # Build a contiguous node-index mapping (gmsh tags may not be 0-based)
        tag_to_idx = {int(t): i for i, t in enumerate(node_tags)}

        # Element type 4 = 4-node tetrahedron (tet4)
        elem_types, elem_tags, elem_node_tags = gmsh.model.mesh.getElements(dim=3)
        tets_list: list[np.ndarray] = []
        for etype, etags, entags in zip(elem_types, elem_tags, elem_node_tags):
            if int(etype) == 4:  # tet4
                nodes_per_elem = 4
                connectivity = np.array(
                    [tag_to_idx[int(t)] for t in entags], dtype=np.int32
                ).reshape(-1, nodes_per_elem)
                tets_list.append(connectivity)

        if not tets_list:
            raise RuntimeError(
                f"gmsh produced no tet4 elements for {step_path!r}. "
                "Check that the geometry is a valid closed solid."
            )
        tets = np.vstack(tets_list)

    finally:
        gmsh.finalize()

    return MeshResult(
        points=coords,
        tets=tets,
        mesh_path=mesh_path,
        char_len_mm=float(char_len),
        n_nodes=int(coords.shape[0]),
        n_elems=int(tets.shape[0]),
    )
