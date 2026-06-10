"""Linear-static FEA on tet4 meshes using scikit-fem.

Solver choice:
  - n_dof <= 60 000: scipy.sparse.linalg.spsolve (direct)
  - n_dof > 60 000 AND pyamg available: CG + AMG preconditioner
  - n_dof > 60 000 AND pyamg absent:    spsolve (warn)

Pressure load:  converted to nodal forces via area-weighted face triangles when
boundary faces are available.  If none are found the force is split equally over
the node set — this is noted in the result.

Von Mises stress is computed per element (from the displacement-to-strain operator),
then averaged onto each node (mean of neighbouring elements).
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import scipy.sparse
import scipy.sparse.linalg

from engine.sim.mesher import MeshResult


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class FaceSet:
    """A set of surface points used to identify constrained / loaded nodes.

    points : (k, 3) float64  — world-space sample points on the face (mm)
    tol_mm : float           — nodes within this radius of any sample point
                               are included in the set
    """
    points: np.ndarray
    tol_mm: float = 1.0


@dataclass
class Load:
    """A force or pressure load applied to a face.

    faceset   : FaceSet  — identifies the loaded nodes
    type      : "force" | "pressure"
    vector    : (3,) array  — total force vector in N (for type="force")
    magnitude : float       — pressure in MPa (N/mm²) (for type="pressure")
    """
    faceset: FaceSet
    type: str                          # "force" or "pressure"
    vector: Optional[np.ndarray] = None  # N, for type="force"
    magnitude: Optional[float] = None   # MPa, for type="pressure"


@dataclass
class StaticResult:
    """Result of a linear-static solve.

    displacement    : (n, 3)  per-node displacement in mm
    disp_magnitude  : (n,)    ||u|| per node in mm
    von_mises       : (n,)    node-averaged von Mises stress in MPa
    summary         : dict    {max_von_mises_mpa, max_disp_mm, min_fos}
    results_path    : str     path to the saved .npz (empty string if not saved)
    solver_notes    : list    human-readable notes about solver / load choices
    n_dof           : int
    solve_time_s    : float
    """
    displacement: np.ndarray
    disp_magnitude: np.ndarray
    von_mises: np.ndarray
    summary: dict
    results_path: str = ""
    solver_notes: list = field(default_factory=list)
    n_dof: int = 0
    solve_time_s: float = 0.0

    def save_npz(self, path: str) -> str:
        np.savez_compressed(
            path,
            displacement=self.displacement,
            disp_magnitude=self.disp_magnitude,
            von_mises=self.von_mises,
        )
        self.results_path = path
        return path


# ── helpers ───────────────────────────────────────────────────────────────────

def _nodes_in_faceset(points: np.ndarray, faceset: FaceSet) -> np.ndarray:
    """Return indices of mesh nodes that are within tol_mm of any faceset point."""
    if len(faceset.points) == 0:
        return np.array([], dtype=np.int32)
    # Compute min distance from each node to any sample point using broadcasting
    # (memory-efficient enough for typical face clouds; split if very large)
    sample = np.asarray(faceset.points, dtype=np.float64)
    tol = float(faceset.tol_mm)
    chunk = 512
    mask = np.zeros(len(points), dtype=bool)
    for i in range(0, len(sample), chunk):
        diff = points[:, np.newaxis, :] - sample[np.newaxis, i:i+chunk, :]  # (n, k, 3)
        dist = np.linalg.norm(diff, axis=-1).min(axis=1)  # (n,)
        mask |= dist <= tol
    return np.where(mask)[0].astype(np.int32)


def _lame_from_material(material: dict) -> tuple[float, float]:
    """Return (lambda, mu) in MPa from a material dict with E_mpa and nu."""
    E = float(material["E_mpa"])
    nu = float(material["nu"])
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
    mu = E / (2.0 * (1.0 + nu))
    return lam, mu


def _build_elasticity_matrix(lam: float, mu: float) -> np.ndarray:
    """6×6 Voigt isotropic elasticity matrix (MPa)."""
    C = np.zeros((6, 6))
    C[0, 0] = C[1, 1] = C[2, 2] = lam + 2 * mu
    C[0, 1] = C[0, 2] = C[1, 0] = C[1, 2] = C[2, 0] = C[2, 1] = lam
    C[3, 3] = C[4, 4] = C[5, 5] = mu
    return C


def _tet4_stiffness_and_B(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Compute the (12, 12) stiffness integrand B^T C B * vol and B matrix for one tet4.

    pts : (4, 3)  node coordinates
    Returns (B, vol)  where B is (6, 12) strain-displacement matrix and vol is volume.
    """
    # Shape function gradients for tet4 are constant
    # [1, x1, y1, z1; ...] system for natural-to-physical mapping
    ones = np.ones((4, 1))
    T = np.hstack([ones, pts])  # (4, 4)
    try:
        Tinv = np.linalg.inv(T)
    except np.linalg.LinAlgError:
        return np.zeros((6, 12)), 0.0

    vol = abs(np.linalg.det(T)) / 6.0

    # dN/dx, dN/dy, dN/dz for each of the 4 nodes — rows 1,2,3 of Tinv
    dN = Tinv[1:4, :]  # (3, 4)  dN[i, j] = d(N_j)/d(x_i)

    # Build B matrix (Voigt notation, 6 x 12)
    B = np.zeros((6, 12))
    for j in range(4):
        col = j * 3
        B[0, col + 0] = dN[0, j]
        B[1, col + 1] = dN[1, j]
        B[2, col + 2] = dN[2, j]
        B[3, col + 0] = dN[1, j]; B[3, col + 1] = dN[0, j]
        B[4, col + 1] = dN[2, j]; B[4, col + 2] = dN[1, j]
        B[5, col + 0] = dN[2, j]; B[5, col + 2] = dN[0, j]

    return B, vol


# ── main solver ───────────────────────────────────────────────────────────────

def run_static(
    mesh: MeshResult,
    material: dict,
    fixtures: list[FaceSet],
    loads: list[Load],
    on_progress: Optional[Callable[[dict], None]] = None,
) -> StaticResult:
    """Run a linear-static FEA solve.

    Parameters
    ----------
    mesh        : MeshResult from mesher.mesh_step
    material    : dict with at least E_mpa, nu, yield_mpa (see materials.json)
    fixtures    : list of FaceSet — constrained (zero-displacement) node sets
    loads       : list of Load — applied force / pressure loads
    on_progress : optional callback({stage, pct, text})

    Returns
    -------
    StaticResult
    """
    def _prog(stage: str, pct: int, text: str) -> None:
        if on_progress:
            on_progress({"stage": stage, "pct": pct, "text": text})

    notes: list[str] = []
    pts = mesh.points          # (n, 3)
    tets = mesh.tets           # (m, 4)
    n_nodes = pts.shape[0]
    n_dof = n_nodes * 3

    _prog("assemble", 0, f"Assembling K ({n_dof} DOF)")

    lam, mu = _lame_from_material(material)
    C = _build_elasticity_matrix(lam, mu)

    # Assemble global stiffness in COO format
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []

    # Pre-allocate per-element Von Mises (for later post-processing)
    elem_vm = np.zeros(tets.shape[0], dtype=np.float64)

    total_elems = tets.shape[0]
    prog_step = max(1, total_elems // 20)

    for e_idx, tet in enumerate(tets):
        if e_idx % prog_step == 0:
            pct = int(e_idx / total_elems * 40)
            _prog("assemble", pct, f"elem {e_idx}/{total_elems}")

        ep = pts[tet]          # (4, 3)
        B, vol = _tet4_stiffness_and_B(ep)
        if vol == 0.0:
            continue
        Ke = B.T @ C @ B * vol   # (12, 12)

        # DOF mapping: node i -> dofs [3i, 3i+1, 3i+2]
        dof_map = np.array([tet[j] * 3 + k for j in range(4) for k in range(3)],
                           dtype=np.int32)  # (12,)

        r, c = np.meshgrid(dof_map, dof_map, indexing="ij")
        rows.append(r.ravel())
        cols.append(c.ravel())
        vals.append(Ke.ravel())

    _prog("assemble", 45, "Building sparse matrix")
    rows_arr = np.concatenate(rows)
    cols_arr = np.concatenate(cols)
    vals_arr = np.concatenate(vals)

    K = scipy.sparse.csr_matrix(
        (vals_arr, (rows_arr, cols_arr)), shape=(n_dof, n_dof)
    )

    # ── Force vector ─────────────────────────────────────────────────────────
    _prog("assemble", 50, "Building load vector")
    f = np.zeros(n_dof, dtype=np.float64)

    for load in loads:
        node_ids = _nodes_in_faceset(pts, load.faceset)
        if len(node_ids) == 0:
            notes.append(f"Load on empty node set (tol={load.faceset.tol_mm} mm) — skipped")
            continue

        if load.type == "force":
            force_vec = np.asarray(load.vector, dtype=np.float64)   # (3,)
            # Spread equally over the node set
            f_per_node = force_vec / len(node_ids)
            for nid in node_ids:
                f[nid * 3: nid * 3 + 3] += f_per_node
            notes.append(
                f"Force {force_vec} N spread equally over {len(node_ids)} nodes"
            )

        elif load.type == "pressure":
            # Pressure → area-weighted nodal forces
            # For tet4, boundary faces are triangles; compute face normals & areas
            pressure = float(load.magnitude)  # MPa = N/mm²
            node_set = set(node_ids.tolist())

            # Find all triangular faces of tets whose 3 nodes are all in node_set
            # tet face combos: (0,1,2),(0,1,3),(0,2,3),(1,2,3)
            face_combos = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
            node_force = np.zeros((n_nodes, 3), dtype=np.float64)
            total_area = 0.0
            face_count = 0
            for tet in tets:
                for fc in face_combos:
                    fn = [tet[i] for i in fc]
                    if all(nid in node_set for nid in fn):
                        v0, v1, v2 = pts[fn[0]], pts[fn[1]], pts[fn[2]]
                        cross = np.cross(v1 - v0, v2 - v0)
                        area = np.linalg.norm(cross) / 2.0
                        if area < 1e-12:
                            continue
                        normal = cross / (2.0 * area)   # unit outward (approx)
                        face_force = pressure * area * normal  # N per face
                        for nid in fn:
                            node_force[nid] += face_force / 3.0
                        total_area += area
                        face_count += 1

            if face_count == 0:
                # Fall back to equal split
                equiv_force = pressure * 1.0  # 1 mm² placeholder
                notes.append(
                    f"Pressure load: no boundary faces found — equal split "
                    f"over {len(node_ids)} nodes (area unknown)"
                )
                for nid in node_ids:
                    f[nid * 3: nid * 3 + 3] += equiv_force / len(node_ids)
            else:
                for nid in node_ids:
                    f[nid * 3: nid * 3 + 3] += node_force[nid]
                notes.append(
                    f"Pressure {pressure} MPa on {face_count} faces "
                    f"(total area {total_area:.1f} mm²) → "
                    f"{n_nodes} nodes"
                )

    # ── Boundary conditions (Dirichlet zero) ─────────────────────────────────
    _prog("assemble", 55, "Applying fixtures")
    fixed_dofs: list[int] = []
    for fs in fixtures:
        node_ids = _nodes_in_faceset(pts, fs)
        if len(node_ids) == 0:
            notes.append(f"Fixture on empty node set (tol={fs.tol_mm} mm) — skipped")
            continue
        for nid in node_ids:
            fixed_dofs.extend([nid * 3, nid * 3 + 1, nid * 3 + 2])

    fixed_dofs_arr = np.unique(np.array(fixed_dofs, dtype=np.int32))
    free_dofs = np.setdiff1d(np.arange(n_dof), fixed_dofs_arr)

    if len(free_dofs) == 0:
        raise RuntimeError("All DOFs are constrained — model is fully fixed; no solve needed.")

    K_ff = K[np.ix_(free_dofs, free_dofs)]
    f_f = f[free_dofs]

    # ── Solve ─────────────────────────────────────────────────────────────────
    _prog("solve", 60, f"Solving ({len(free_dofs)} free DOF)")
    t0 = time.perf_counter()

    use_amg = False
    try:
        import pyamg  # type: ignore[import]
        if len(free_dofs) > 60_000:
            use_amg = True
    except ImportError:
        pass

    if use_amg:
        notes.append("Solver: CG + AMG (pyamg)")
        ml = pyamg.ruge_stuben_solver(K_ff.tocsr())
        M = ml.aspreconditioner(cycle="V")
        u_f, info = scipy.sparse.linalg.cg(K_ff, f_f, M=M, rtol=1e-8)
        if info != 0:
            notes.append(f"CG+AMG warning: info={info} (may not have converged)")
    else:
        solver_label = "direct spsolve"
        if len(free_dofs) > 60_000:
            notes.append(f"Solver: {solver_label} (pyamg not available for {len(free_dofs)} DOF)")
        else:
            notes.append(f"Solver: {solver_label}")
        u_f = scipy.sparse.linalg.spsolve(K_ff.tocsc(), f_f)

    solve_time = time.perf_counter() - t0

    u_full = np.zeros(n_dof, dtype=np.float64)
    u_full[free_dofs] = u_f

    # ── Post-processing ───────────────────────────────────────────────────────
    _prog("post", 85, "Computing stresses")
    displacement = u_full.reshape(n_nodes, 3)
    disp_magnitude = np.linalg.norm(displacement, axis=1)

    # Per-element von Mises via strain-displacement
    node_vm_accum = np.zeros(n_nodes, dtype=np.float64)
    node_vm_count = np.zeros(n_nodes, dtype=np.int32)

    for e_idx, tet in enumerate(tets):
        ep = pts[tet]
        B, vol = _tet4_stiffness_and_B(ep)
        if vol == 0.0:
            continue
        # Element displacement vector (12,)
        ue = np.concatenate([u_full[tet[j] * 3: tet[j] * 3 + 3] for j in range(4)])
        strain = B @ ue        # (6,)  [exx, eyy, ezz, gxy, gyz, gxz]
        stress = C @ strain    # (6,)  MPa

        # Von Mises: sigma_vm = sqrt(0.5 * [(sx-sy)^2 + (sy-sz)^2 + (sz-sx)^2 + 6*(txy^2+tyz^2+txz^2)])
        sx, sy, sz = stress[0], stress[1], stress[2]
        txy, tyz, txz = stress[3], stress[4], stress[5]
        vm = np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2
                             + 6 * (txy ** 2 + tyz ** 2 + txz ** 2)))
        for nid in tet:
            node_vm_accum[nid] += vm
            node_vm_count[nid] += 1

    with np.errstate(divide="ignore", invalid="ignore"):
        von_mises = np.where(node_vm_count > 0, node_vm_accum / node_vm_count, 0.0)

    yield_mpa = float(material.get("yield_mpa", 250.0))
    max_vm = float(np.max(von_mises))
    max_disp = float(np.max(disp_magnitude))
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_fos = yield_mpa / np.where(von_mises > 0, von_mises, np.inf)
    min_fos = float(np.clip(np.min(raw_fos[raw_fos > 0]) if np.any(raw_fos > 0) else 15.0,
                            0.0, 15.0))

    summary = {
        "max_von_mises_mpa": round(max_vm, 4),
        "max_disp_mm": round(max_disp, 6),
        "min_fos": round(min_fos, 4),
    }

    _prog("post", 100, "Done")

    return StaticResult(
        displacement=displacement,
        disp_magnitude=disp_magnitude,
        von_mises=von_mises,
        summary=summary,
        solver_notes=notes,
        n_dof=n_dof,
        solve_time_s=round(solve_time, 3),
    )
