"""Built-in 2-D FDFD cross-section eigenmode solver (scalar Helmholtz).

Solves the cutoff eigenproblem of a hollow, perfectly-conducting waveguide:

    -laplacian(psi) = kc^2 * psi      on the cross-section
    TM modes: psi = Ez,  Dirichlet (Ez = 0) on the wall
    TE modes: psi = Hz,  Neumann  (dHz/dn = 0) on the wall

on a uniform finite-difference grid with a boolean in-domain mask, so rectangular
and circular (stair-stepped) cross-sections both work.  Eigenvalues kc^2 map to
cutoff frequencies fc = c * kc / (2*pi*sqrt(eps_r)).

This is a REAL numerical solver (numpy + scipy.sparse), not a mock: it is the
"no external solver installed" eigenmode backend, validated in test_em.py against
the analytic WR-90 TE10 cutoff (6.557 GHz) to < 1 %.

Limits (documented, enforced): 2-D hollow single-conductor cross-sections only —
no dielectric loading profiles, no field singular corners resolution guarantees.
Full-wave 3-D and inhomogeneous problems belong to the openEMS / FEM adapters.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from .analytic import C0, rect_mode_table, circ_mode_table


def _laplacian(mask: np.ndarray, hx: float, hy: float, neumann: bool) -> sp.csr_matrix:
    """Negative 5-point Laplacian on the True cells of ``mask`` (cell-centred grid).

    Cell-centred finite volumes: the wall sits ON the cell face.  Neumann wall
    (TE, dHz/dn = 0): the outside-flux term is dropped.  Dirichlet wall (TM,
    Ez = 0 at the face, half a cell away): ghost reflection contributes 2*w to
    the diagonal.
    """
    nx, ny = mask.shape
    idx = -np.ones(mask.shape, dtype=np.int64)
    idx[mask] = np.arange(int(mask.sum()))
    n = int(mask.sum())
    rows: List[int] = []
    cols: List[int] = []
    vals: List[float] = []
    wx, wy = 1.0 / hx**2, 1.0 / hy**2
    for i in range(nx):
        for j in range(ny):
            if not mask[i, j]:
                continue
            me = idx[i, j]
            diag = 0.0
            for di, dj, w in ((-1, 0, wx), (1, 0, wx), (0, -1, wy), (0, 1, wy)):
                ii, jj = i + di, j + dj
                inside = 0 <= ii < nx and 0 <= jj < ny and mask[ii, jj]
                if inside:
                    rows.append(me)
                    cols.append(idx[ii, jj])
                    vals.append(-w)
                    diag += w
                elif not neumann:      # Dirichlet wall on the cell face: ghost = -psi
                    diag += 2.0 * w
                # Neumann wall: dropped entirely (zero-flux)
            rows.append(me)
            cols.append(me)
            vals.append(diag)
    return sp.csr_matrix((vals, (rows, cols)), shape=(n, n))


def _domain_mask(shape: str, a_m: float, b_m: float, nx: int, ny: int):
    """(mask, hx, hy) for the requested cross-section on an nx x ny cell-centred grid."""
    if shape == "rectangular":
        hx, hy = a_m / nx, b_m / ny
        mask = np.ones((nx, ny), dtype=bool)    # every cell is inside; walls on faces
        return mask, hx, hy
    if shape == "circular":
        r = a_m  # a_m carries the radius
        hx, hy = 2.0 * r / nx, 2.0 * r / ny
        xs = (np.arange(nx) + 0.5) * hx - r
        ys = (np.arange(ny) + 0.5) * hy - r
        X, Y = np.meshgrid(xs, ys, indexing="ij")
        mask = X**2 + Y**2 < r**2
        return mask, hx, hy
    raise ValueError(f"unsupported cross-section shape {shape!r}")


def solve_eigenmodes(shape: str, a_m: float, b_m: float, n_modes: int = 6,
                     resolution: int = 80, eps_r: float = 1.0,
                     want_fields: bool = False) -> Dict:
    """Cutoff eigenmodes (TE and TM) of a hollow guide cross-section.

    shape      : "rectangular" (a_m x b_m) or "circular" (radius = a_m; b_m ignored)
    resolution : grid cells across the larger dimension (default 80; 40..200 sane)
    Returns {"modes": [...], "solver_detail": {...}} — modes sorted by fc, each
    {type, name, fc_hz, kc_per_m, [field]}; rectangular modes are labeled (m,n)
    by matching kc against the analytic table.
    """
    if shape == "circular":
        b_eff = a_m
        aspect = 1.0
    else:
        b_eff = b_m
        aspect = b_m / a_m
    nx = max(24, int(resolution))
    ny = max(12, int(round(nx * aspect)))

    mask, hx, hy = _domain_mask(shape, a_m, b_eff, nx, ny)
    k = max(n_modes + 2, 8)

    def _eigs(neumann: bool) -> tuple:
        A = _laplacian(mask, hx, hy, neumann=neumann)
        # smallest eigenvalues via shift-invert (A is SPD / PSD)
        sigma = -1e-6 if neumann else None
        if neumann:
            vals, vecs = spla.eigsh(A, k=min(k + 1, A.shape[0] - 2), sigma=sigma,
                                    which="LM")
        else:
            vals, vecs = spla.eigsh(A, k=min(k, A.shape[0] - 2), sigma=1e-6,
                                    which="LM")
        order = np.argsort(vals)
        return vals[order], vecs[:, order]

    modes: List[dict] = []
    for te, neumann in ((True, True), (False, False)):
        vals, vecs = _eigs(neumann)
        for col, lam in enumerate(vals):
            if te and lam < 1e-4 * vals[-1]:
                continue  # drop the trivial constant Hz mode (kc = 0)
            if lam <= 0:
                continue
            kc = math.sqrt(float(lam))
            fc = C0 * kc / (2.0 * math.pi * math.sqrt(eps_r))
            entry = {"type": "TE" if te else "TM", "name": None,
                     "fc_hz": fc, "kc_per_m": kc}
            if want_fields:
                full = np.zeros(mask.shape)
                full[mask] = vecs[:, col]
                peak = np.abs(full).max() or 1.0
                # downsample to a <=32x16 preview
                sx = max(1, mask.shape[0] // 32)
                sy = max(1, mask.shape[1] // 16)
                entry["field"] = np.round(full[::sx, ::sy] / peak, 4).tolist()
            modes.append(entry)

    modes.sort(key=lambda md: md["fc_hz"])
    modes = modes[:n_modes]
    _label_modes(modes, shape, a_m, b_eff, eps_r)
    return {
        "modes": modes,
        "solver_detail": {"method": "FDFD scalar Helmholtz (5-point FD)",
                          "grid": [int(nx), int(ny)], "cells": int(mask.sum()),
                          "shape": shape},
    }


def _label_modes(modes: List[dict], shape: str, a_m: float, b_m: float,
                 eps_r: float) -> None:
    """Attach TEmn/TMnm names by matching fc against the analytic table (2% tol).

    Degeneracy: among the references within tolerance, prefer one whose name is
    not already assigned — a square guide's TE10/TE01 pair are distinct modes at
    the same cutoff and must get both names (matching purely by nearest fc used
    to label both "TE01").  If every candidate is taken, reuse the closest:
    circular TEn1/TMn1 (n>=1) pairs are genuinely doubly degenerate and share
    one table entry.
    """
    table = (rect_mode_table(a_m, b_m, 40, eps_r) if shape == "rectangular"
             else circ_mode_table(a_m, 40, eps_r))
    used: set = set()
    for md in modes:
        cands = sorted(
            ((abs(ref["fc_hz"] - md["fc_hz"]) / ref["fc_hz"], ref)
             for ref in table if ref["type"] == md["type"]),
            key=lambda pair: pair[0])
        cands = [(err, ref) for err, ref in cands if err < 0.02]
        best: Optional[dict] = None
        best_err = 0.02
        for err, ref in cands:
            if ref["name"] not in used:
                best, best_err = ref, err
                break
        if best is None and cands:            # all in-tolerance names taken:
            best_err, best = cands[0]         # true degeneracy — reuse closest
        md["name"] = best["name"] if best else f"{md['type']}?"
        if best:
            used.add(best["name"])
            md["analytic_fc_hz"] = best["fc_hz"]
            md["analytic_err_pct"] = round(100.0 * best_err, 3)
