"""Modal FEA on tet4 meshes using scikit-fem + scipy eigensolver.

Computes the lowest n_modes natural frequencies and corresponding mode shapes
via shift-invert generalized eigensolution: K phi = omega^2 M phi, sigma=0.

Mass matrix uses consistent lumped approach (row-sum lumping per element)
for efficiency.  Consistent mass could be assembled similarly to K but is
slower and rarely needed for the first few modes.

All results in SI-consistent units:  mm (length), MPa (stress), g (mass)
  => omega in rad/ms  => freq in kHz — we convert to Hz by scaling.

Actually: if E is in MPa = N/mm², density in g/mm³, coordinates in mm:
  [K] = MPa * mm^2 = N  (stiffness in N/mm)
  [M] = (g/mm³) * mm³ = g
  omega² = K/M  [N/mm / g] = [N / (g·mm)] = [mm/s² / mm] = [1/s²] * 1e6
  => omega [rad/s] = sqrt(eigenvalue) * 1000
  => freq [Hz] = omega / (2*pi)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import scipy.sparse
import scipy.sparse.linalg

from engine.sim.mesher import MeshResult
from engine.sim.fea_static import (
    FaceSet,
    _nodes_in_faceset,
    _lame_from_material,
    _build_elasticity_matrix,
    _tet4_stiffness_and_B,
)


@dataclass
class ModalResult:
    """Result of a modal FEA solve.

    freqs_hz    : (n_modes,) array of natural frequencies in Hz
    mode_shapes : (n_modes, n_nodes, 3) per-node displacement modes (unit-normalised)
    results_path: path to saved .npz (empty if not saved)
    n_dof       : total DOF count
    solve_time_s: wall-clock time for the eigensolution
    """
    freqs_hz: np.ndarray
    mode_shapes: np.ndarray
    results_path: str = ""
    n_dof: int = 0
    solve_time_s: float = 0.0

    def save_npz(self, path: str) -> str:
        np.savez_compressed(
            path,
            freqs_hz=self.freqs_hz,
            mode_shapes=self.mode_shapes,
        )
        self.results_path = path
        return path


def run_modal(
    mesh: MeshResult,
    material: dict,
    fixtures: list[FaceSet],
    n_modes: int = 6,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> ModalResult:
    """Run a modal FEA solve.

    Parameters
    ----------
    mesh      : MeshResult from mesher.mesh_step
    material  : dict with E_mpa, nu, density_g_cm3
    fixtures  : list of FaceSet (zero-displacement Dirichlet BCs).
                Pass empty list for free-free modal analysis.
    n_modes   : number of modes to extract (default 6)
    on_progress : optional callback({stage, pct, text})

    Returns
    -------
    ModalResult  with frequencies in Hz and mode shapes (n_modes, n_nodes, 3)
    """
    def _prog(stage: str, pct: int, text: str) -> None:
        if on_progress:
            on_progress({"stage": stage, "pct": pct, "text": text})

    pts = mesh.points    # (n, 3)
    tets = mesh.tets     # (m, 4)
    n_nodes = pts.shape[0]
    n_dof = n_nodes * 3

    _prog("assemble", 0, f"Assembling K+M ({n_dof} DOF)")

    lam, mu = _lame_from_material(material)
    C = _build_elasticity_matrix(lam, mu)

    # density_g_cm3 → g/mm³ : 1 g/cm³ = 0.001 g/mm³
    density = float(material["density_g_cm3"]) * 1e-3   # g/mm³

    # Assemble K (same as static) and diagonal-lumped M in COO format
    k_rows: list[np.ndarray] = []
    k_cols: list[np.ndarray] = []
    k_vals: list[np.ndarray] = []
    m_diag = np.zeros(n_dof, dtype=np.float64)  # lumped mass diagonal

    total_elems = tets.shape[0]
    prog_step = max(1, total_elems // 20)

    for e_idx, tet in enumerate(tets):
        if e_idx % prog_step == 0:
            pct = int(e_idx / total_elems * 50)
            _prog("assemble", pct, f"elem {e_idx}/{total_elems}")

        ep = pts[tet]                   # (4, 3)
        B, vol = _tet4_stiffness_and_B(ep)
        if vol == 0.0:
            continue

        Ke = B.T @ C @ B * vol          # (12, 12)
        dof_map = np.array([tet[j] * 3 + k for j in range(4) for k in range(3)],
                           dtype=np.int32)

        r, c = np.meshgrid(dof_map, dof_map, indexing="ij")
        k_rows.append(r.ravel())
        k_cols.append(c.ravel())
        k_vals.append(Ke.ravel())

        # Lumped mass: m_element / 4  per node (equal lumping)
        m_elem = density * vol          # g
        m_per_node = m_elem / 4.0
        for j in range(4):
            for k in range(3):
                m_diag[tet[j] * 3 + k] += m_per_node

    _prog("assemble", 55, "Building sparse matrices")
    K = scipy.sparse.csr_matrix(
        (np.concatenate(k_vals),
         (np.concatenate(k_rows), np.concatenate(k_cols))),
        shape=(n_dof, n_dof),
    )
    M = scipy.sparse.diags(m_diag, format="csr")

    # ── Boundary conditions ───────────────────────────────────────────────────
    _prog("assemble", 60, "Applying fixtures")
    fixed_dofs: list[int] = []
    for fs in fixtures:
        node_ids = _nodes_in_faceset(pts, fs)
        for nid in node_ids:
            fixed_dofs.extend([nid * 3, nid * 3 + 1, nid * 3 + 2])

    fixed_dofs_arr = np.unique(np.array(fixed_dofs, dtype=np.int32))
    free_dofs = np.setdiff1d(np.arange(n_dof), fixed_dofs_arr)

    K_ff = K[np.ix_(free_dofs, free_dofs)]
    M_ff = M[np.ix_(free_dofs, free_dofs)]

    # ── Eigensolution ─────────────────────────────────────────────────────────
    _prog("solve", 65, f"Eigensolve ({n_modes} modes, {len(free_dofs)} free DOF)")
    t0 = time.perf_counter()

    # shift-invert at sigma=0 for lowest modes
    k_req = min(n_modes, len(free_dofs) - 2)
    if k_req < 1:
        k_req = 1

    eigenvalues, eigenvectors = scipy.sparse.linalg.eigsh(
        K_ff.tocsc(),
        M=M_ff.tocsc(),
        k=k_req,
        sigma=0.0,
        which="LM",
        tol=1e-6,
        maxiter=10 * len(free_dofs),
    )
    solve_time = time.perf_counter() - t0

    # eigenvalues are omega² in (N/mm) / g = 1e6 rad²/s²
    # => omega [rad/s] = sqrt(eigenvalue * 1e6)
    eigenvalues = np.maximum(eigenvalues, 0.0)
    omega = np.sqrt(np.abs(eigenvalues) * 1e6)   # rad/s
    freqs_hz = omega / (2.0 * np.pi)            # Hz

    # Sort by frequency
    order = np.argsort(freqs_hz)
    freqs_hz = freqs_hz[order]
    eigenvectors = eigenvectors[:, order]

    # Map eigenvectors back to full DOF space
    mode_shapes = np.zeros((k_req, n_nodes, 3), dtype=np.float64)
    for m_idx in range(k_req):
        u_full = np.zeros(n_dof, dtype=np.float64)
        u_full[free_dofs] = eigenvectors[:, m_idx]
        mode_shapes[m_idx] = u_full.reshape(n_nodes, 3)
        # Normalize to unit max displacement
        scale = np.max(np.abs(u_full))
        if scale > 0:
            mode_shapes[m_idx] /= scale

    _prog("post", 100, "Done")

    return ModalResult(
        freqs_hz=freqs_hz,
        mode_shapes=mode_shapes,
        n_dof=n_dof,
        solve_time_s=round(solve_time, 3),
    )
