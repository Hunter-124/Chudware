"""Tests for engine/sim/fea_thermal_transient.py — the Python parity oracle for the C++
transient thermal kernel (core/sim/fea_thermal.cpp, T-FIN18).

Cases mirror core/tests/test_thermal.cpp's transient suite so both stacks are pinned to
the SAME analytic references (the differential-parity convention: never weaken an
assertion; match the analytic truth on both sides):
  1. slab quench       — 1D Fourier-series decay, mid-plane at Fo=0.3 within 2%
  2. steady limit      — long-time transient lands on the linear Dirichlet profile
  3. schedule semantics- sample-and-hold + periodic wrap (lockstep with C++ Case T5)
  4. duty-cycle bracket- periodic peak sits between mean-power and full-power steady maxima
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.sim.fea_thermal_transient import (  # noqa: E402
    Convection, FluxLoad, Schedule, box_mesh, solve_transient)


def _slab_series(T0, alpha, L, x, t, n_terms=199):
    s = 0.0
    for k in range(1, n_terms + 1, 2):
        s += (4.0 * T0 / (k * math.pi) * math.sin(k * math.pi * x / L)
              * math.exp(-alpha * k * k * math.pi * math.pi * t / (L * L)))
    return s


def test_slab_quench_matches_fourier_series():
    L, k, rho_cp, T0 = 20.0, 0.05, 0.004, 100.0
    alpha = k / rho_cp
    t_star = 0.3 * L * L / alpha
    pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, 20, 2, 2)
    fixed = x0 + xL
    temps = [0.0] * len(fixed)
    r = solve_transient(pts, tets, k, fixed, temps, [], [], rho_cp,
                        t_end=t_star, dt=t_star / 400.0, t_init=T0)
    assert r.ok, r.error
    t_last, T = r.frames[-1]
    assert abs(t_last - t_star) < 1e-9
    mid = [i for i in range(len(pts)) if abs(pts[i, 0] - L / 2.0) < 1e-9]
    got = float(np.mean(T[mid]))
    ref = _slab_series(T0, alpha, L, L / 2.0, t_star)
    assert abs(got - ref) / ref < 0.02, f"mid-plane {got} vs series {ref}"
    # Envelope: monotone decay -> run max is the initial condition at t=0.
    assert abs(r.max_temp - T0) < 1e-9
    assert r.max_temp_time == 0.0


def test_long_time_limit_is_the_steady_linear_profile():
    L, k, rho_cp, T1, T2 = 40.0, 0.4, 0.004, 0.0, 200.0
    pts, tets, x0, xL = box_mesh(L, 12.0, 12.0, 8, 2, 2)
    fixed = x0 + xL
    temps = [T1] * len(x0) + [T2] * len(xL)
    tau = rho_cp * L * L / k
    r = solve_transient(pts, tets, k, fixed, temps, [], [], rho_cp,
                        t_end=10.0 * tau, dt=tau / 50.0, t_init=100.0)
    assert r.ok, r.error
    _, T = r.frames[-1]
    exact = T1 + (T2 - T1) * pts[:, 0] / L
    assert float(np.max(np.abs(T - exact))) < 0.001 * (T2 - T1)


def test_schedule_sample_and_hold_and_periodic():
    s = Schedule()
    assert s.scale(3.0) == 1.0            # empty = always on
    s = Schedule(times=[0.0, 2.0, 5.0], scales=[1.0, 0.25, 0.0])
    assert s.scale(0.0) == 1.0
    assert s.scale(1.9) == 1.0
    assert s.scale(2.0) == 0.25
    assert s.scale(4.9) == 0.25
    assert s.scale(7.0) == 0.0            # last holds
    s.period = 10.0
    assert s.scale(11.0) == 1.0           # wraps
    assert s.scale(12.5) == 0.25


def test_duty_cycle_peak_brackets_between_mean_and_full_power():
    L, k, rho_cp, T_cold, q, duty = 30.0, 0.02, 0.004, 40.0, 0.01, 0.5
    pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, 12, 2, 2)
    fixed, temps = xL, [T_cold] * len(xL)
    tau = rho_cp * L * L / k
    period = tau / 5.0
    sched = Schedule(times=[0.0, duty * period], scales=[1.0, 0.0], period=period)

    def run(load):
        return solve_transient(pts, tets, k, fixed, temps, [load], [], rho_cp,
                               t_end=12.0 * tau, dt=period / 40.0, t_init=T_cold)

    r_duty = run(FluxLoad(nodes=x0, q=q, schedule=sched))
    r_full = run(FluxLoad(nodes=x0, q=q))
    assert r_duty.ok and r_full.ok
    # Steady references: 1D wall T0 = T_cold + q_eff*L/k (exact for this linear problem).
    steady_avg_max = T_cold + duty * q * L / k
    steady_full_max = T_cold + q * L / k
    assert r_duty.max_temp > steady_avg_max
    assert r_duty.max_temp < steady_full_max
    # Full-power transient approaches its steady max from below.
    assert abs(r_full.frames[-1][1].max() - steady_full_max) / steady_full_max < 0.01
    # Envelope is well-formed: per-node max >= final field.
    assert np.all(r_duty.envelope_max >= r_duty.frames[-1][1] - 1e-12)


def test_convection_steady_limit_matches_series_resistance():
    L, k, rho_cp, T_hot, T_amb, h = 20.0, 0.05, 0.004, 150.0, 25.0, 0.002
    pts, tets, x0, xL = box_mesh(L, 10.0, 10.0, 10, 2, 2)
    tau = rho_cp * L * L / k
    r = solve_transient(pts, tets, k, x0, [T_hot] * len(x0), [],
                        [Convection(nodes=xL, h=h, t_amb=T_amb)], rho_cp,
                        t_end=10.0 * tau, dt=tau / 50.0, t_init=T_amb)
    assert r.ok, r.error
    _, T = r.frames[-1]
    q = (T_hot - T_amb) / (L / k + 1.0 / h)
    T_L_exact = T_amb + q / h
    got = float(np.mean(T[xL]))
    assert abs(got - T_L_exact) / T_L_exact < 5e-3


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
