"""Extra analytic anchors for engine/sim/fea_thermal_transient.py (the Python parity
oracle for core/sim/fea_thermal.cpp).

Complements test_thermal_transient.py — cases here pin DIFFERENT closed-form truths
(same differential-parity convention: never weaken; both stacks share these anchors):

  1. semi-infinite solid — early-time surface step, probe point vs the erf solution
     T(x,t) = Ts + (T0-Ts)·erf(x / (2√(αt)))            (Incropera §5.7, eq. 5.60)
  2. adiabatic flux heat-up — discrete energy conservation of backward Euler:
     with no Dirichlet node and no convection, K·1 = 0 (zero row sums of the
     conductivity matrix), so  Σᵢ cᵢ·ΔTᵢ = Δt·Σᵢ fᵢ  holds EXACTLY per step and the
     capacitance-weighted mean temperature rises by q·A·t / (ρcp·V) to round-off
     (first law: E_in = q·A·t, ΔU = ρcp·V·ΔT_mean).
  3. fin (pin fin / rod) — steady limit of the transient solve with lateral
     convection vs 1-D fin theory with adiabatic tip:
     θ(L)/θ_b = 1/cosh(mL),  m = √(hP/(kA))             (Incropera Table 3.4 case B)

Note on facesets: convection node sets are built PER PLANE. A single merged set that
spans several boundary planes makes interior diagonal faces (all 3 corners boundary
nodes on different planes) count as "surface" in the element-face enumeration — the
kernel's documented node-set semantics, not a solver bug — which inflates the film
area on coarse meshes.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np

# realpath: pytest may collect this file through a symlinked package dir
# (e.g. services/engine -> engine_oracle); dirname() without resolving
# yields services/ and putting THAT on sys.path shadows the third-party
# `mcp` SDK with services/mcp (collection-time ImportError).
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.sim.fea_thermal_transient import (  # noqa: E402
    Convection, FluxLoad, box_mesh, solve_transient)


def test_semi_infinite_solid_probe_matches_erf_solution():
    # Bar long enough that the far end stays at T0 for the whole run (penetration
    # depth √(αt) = 5 mm << L = 40 mm; erf(L/(2√(αt))) = erf(4) ≈ 1).
    L, k, rho_cp = 40.0, 0.05, 0.004        # mm, W/(mm·K), J/(mm^3·K)
    alpha = k / rho_cp                       # 12.5 mm^2/s
    T0, Ts = 20.0, 100.0
    t_pr, x_pr = 2.0, 4.0
    pts, tets, x0, xL = box_mesh(L, 6.0, 6.0, 40, 2, 2)
    r = solve_transient(pts, tets, k, x0, [Ts] * len(x0), [], [], rho_cp,
                        t_end=t_pr, dt=t_pr / 400.0, t_init=T0)
    assert r.ok, r.error
    _, T = r.frames[-1]
    probe = [i for i in range(len(pts)) if abs(pts[i, 0] - x_pr) < 1e-9]
    got = float(np.mean(T[probe]))
    # T(x,t) = Ts + (T0-Ts)·erf(x/(2√(αt)))  — Incropera §5.7 eq. 5.60.
    ref = Ts + (T0 - Ts) * math.erf(x_pr / (2.0 * math.sqrt(alpha * t_pr)))
    assert abs(got - ref) / abs(ref - T0) < 0.01, f"probe {got} vs erf {ref}"
    # Far end still undisturbed — validates the semi-infinite assumption itself.
    far = float(np.mean(T[xL]))
    assert abs(far - T0) < 0.01 * (Ts - T0)


def test_adiabatic_flux_heatup_conserves_energy_exactly():
    # No Dirichlet node, no convection: pure-Neumann TRANSIENT is well-posed and the
    # backward-Euler update conserves energy exactly (see module docstring).
    L, b, k, rho_cp, q, T0 = 30.0, 10.0, 0.05, 0.004, 0.01, 20.0
    t_end = 5.0
    pts, tets, x0, _ = box_mesh(L, b, b, 6, 2, 2)
    r = solve_transient(pts, tets, k, [], [], [FluxLoad(nodes=x0, q=q)], [], rho_cp,
                        t_end=t_end, dt=t_end / 50.0, t_init=T0)
    assert r.ok, r.error
    _, T = r.frames[-1]
    # Lumped nodal capacitances c_i = Σ_tets rho_cp·V/4 (row-sum lumping, tet4).
    c = np.zeros(len(pts))
    for el in tets:
        a, bb, cc, d = pts[el[0]], pts[el[1]], pts[el[2]], pts[el[3]]
        vol = abs(np.linalg.det(np.stack([bb - a, cc - a, d - a]))) / 6.0
        c[el] += rho_cp * vol / 4.0
    V, A = L * b * b, b * b
    # First law: ΔU = ρcp·V·ΔT_mean = E_in = q·A·t  →  ΔT_mean = q·A·t/(ρcp·V).
    dT_mean_ref = q * A * t_end / (rho_cp * V)
    dT_mean_got = float(np.dot(c, T - T0) / c.sum())
    assert abs(c.sum() - rho_cp * V) / (rho_cp * V) < 1e-12   # lumping partitions V
    assert abs(dT_mean_got - dT_mean_ref) / dT_mean_ref < 1e-9, \
        f"energy balance {dT_mean_got} vs {dT_mean_ref}"


def test_fin_steady_limit_matches_1d_fin_theory():
    # 100×10×10 mm pin fin, base held at Tb, four lateral faces under a film h.
    # Transverse Biot h·(b/2)/k = 2.5e-3 << 0.1 → 1-D fin theory is the truth here.
    Lf, bf = 100.0, 10.0
    k = 0.2          # 200 W/(m·K) aluminium-grade, in W/(mm·K)
    h = 1e-4         # 100 W/(m^2·K) in W/(mm^2·K)
    Tb, Tamb, rho_cp = 100.0, 25.0, 0.004
    pts, tets, x0, xL = box_mesh(Lf, bf, bf, 25, 2, 2)
    # One convection set PER lateral plane (see the module docstring note).
    lat_sets = []
    for ax in (1, 2):
        for v in (0.0, bf):
            lat_sets.append([i for i in range(len(pts)) if abs(pts[i, ax] - v) < 1e-9])
    tau = rho_cp * Lf * Lf / k
    r = solve_transient(pts, tets, k, x0, [Tb] * len(x0), [],
                        [Convection(nodes=s, h=h, t_amb=Tamb) for s in lat_sets],
                        rho_cp, t_end=8.0 * tau, dt=tau / 40.0, t_init=Tamb)
    assert r.ok, r.error
    _, T = r.frames[-1]
    # θ(L)/θ_b = 1/cosh(mL), m = √(hP/(kA)) — Incropera Table 3.4 case B (adiabatic
    # tip; the tip face carries no film because its interior nodes are not in any
    # lateral plane set).
    m = math.sqrt(h * 4.0 * bf / (k * bf * bf))
    tip_ref = Tamb + (Tb - Tamb) / math.cosh(m * Lf)
    tip_got = float(np.mean(T[xL]))
    assert abs(tip_got - tip_ref) / abs(tip_ref - Tamb) < 0.01, \
        f"fin tip {tip_got} vs 1D theory {tip_ref}"
    # Base-to-tip monotone decay (no interior extremum — maximum principle).
    planes = sorted(set(round(float(x), 9) for x in pts[:, 0]))
    means = [float(np.mean(T[[i for i in range(len(pts))
                              if abs(pts[i, 0] - xp) < 1e-9]])) for xp in planes]
    assert all(means[i] >= means[i + 1] - 1e-9 for i in range(len(means) - 1))


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
