"""EM wave stack regression tests — analytic impedances, FDFD convergence,
adapter selection/fallback, and the worker seam.

Extends test_em.py (which stays untouched) per the simsuite buildout:

* analytic layer: TE10 cutoff fc = c/(2a) (Pozar 4e, eq. 3.84 with m=1,n=0),
  parallel-plate Z0 = eta*d/w (Pozar 4e §3.2), microstrip Z0 via the Hammerstad
  closed form (Pozar 4e eqs. 3.195-3.196) cross-checked against the independent
  IPC-2141A implementation in engine_oracle/ecad/impedance.py;
* BuiltinFdfdAdapter: known-mode checks + a grid-refinement convergence sweep
  (the 5-point scheme is ~2nd order, so halving h must shrink the error);
* adapter selection: openEMS is not installed on CI/dev boxes — requesting it
  must degrade to a clean EmSolverError / {"ok": False} envelope, never crash;
* upstream seam: engine_oracle/worker.py imports ``engine.sim.em.worker_rpc``
  through the ``engine`` -> ``engine_oracle`` symlink; that alias is asserted.

Nothing here is all-skip: only the two live-openEMS guards skip, and only when
openEMS genuinely imports.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from ...sim.em import analytic, fdfd
from ...sim.em.adapters import (
    BuiltinFdfdAdapter, ElmerAdapter, EmSolverError, GetdpAdapter, MeepAdapter,
    registry, select_adapter)
from ...sim.em.openems_adapter import OpenEmsAdapter
from ...sim.em.worker_rpc import WORKER_METHODS


def _openems_installed() -> bool:
    return bool(OpenEmsAdapter().available().get("installed"))


# --------------------------------------------------------------------- analytic


class TestAnalyticCutoffs(unittest.TestCase):
    def test_te10_cutoff_is_c_over_2a(self):
        # TE10 cutoff: fc = c/(2a) — Pozar 4e eq. 3.84, kc = pi/a for (m,n)=(1,0).
        for std, (a_mm, _b_mm) in analytic.STANDARD_WAVEGUIDES.items():
            a_m = a_mm / 1e3
            fc = analytic.rect_cutoff_hz(a_m, 1.0, 1, 0)  # b irrelevant for TE10
            self.assertAlmostEqual(fc, analytic.C0 / (2.0 * a_m), delta=1e-3,
                                   msg=std)

    def test_te10_dielectric_scaling(self):
        # Filled guide: fc scales by 1/sqrt(eps_r) (Pozar 4e eq. 3.84).
        a = 0.02286
        f1 = analytic.rect_cutoff_hz(a, 0.01016, 1, 0, eps_r=1.0)
        f2 = analytic.rect_cutoff_hz(a, 0.01016, 1, 0, eps_r=4.0)
        self.assertAlmostEqual(f1 / f2, 2.0, places=9)


class TestAnalyticImpedances(unittest.TestCase):
    def test_eta0_from_si_constants(self):
        # eta0 = sqrt(mu0/eps0) with mu0 = 4pi*1e-7 and eps0 = 1/(mu0 c^2).
        eta = math.sqrt(analytic.MU0 / analytic.EPS0)
        self.assertAlmostEqual(analytic.ETA0, eta, places=9)
        self.assertAlmostEqual(analytic.ETA0, 4e-7 * math.pi * analytic.C0, places=9)

    def test_parallel_plate_z0_closed_form(self):
        # Z0 = eta0/sqrt(er) * d/w — Pozar 4e §3.2 (parallel-plate line, no fringing).
        w, d, er = 0.010, 0.001, 2.25
        want = analytic.ETA0 / math.sqrt(er) * d / w
        self.assertAlmostEqual(analytic.parallel_plate_z0(w, d, er), want, places=12)
        # scaling: doubling the separation doubles Z0; doubling width halves it
        z = analytic.parallel_plate_z0(w, d, er)
        self.assertAlmostEqual(analytic.parallel_plate_z0(w, 2 * d, er), 2 * z, places=9)
        self.assertAlmostEqual(analytic.parallel_plate_z0(2 * w, d, er), z / 2, places=9)
        with self.assertRaises(ValueError):
            analytic.parallel_plate_z0(0.0, d, er)

    def test_microstrip_z0_vs_ipc2141_oracle(self):
        # Differential oracle: our Hammerstad form (Pozar 3.195-3.196) vs the
        # independent IPC-2141A implementation in engine_oracle/ecad/impedance.py,
        # inside the IPC validity band 0.1 < w/h < 2.0, 1 < er < 15, t = 0.
        # The two published closed forms agree to ~10% there.
        from ...ecad.impedance import microstrip_z0 as ipc_z0
        for er in (2.2, 4.4, 6.15):
            for u in (0.5, 1.0, 1.5, 2.0):
                h = 0.2
                ours = analytic.microstrip_z0(u * h, h, er)["z0"]
                ipc = ipc_z0(u * h, h, 0.0, er)["z0"]
                self.assertLess(abs(ours - ipc) / ipc, 0.10,
                                msg=f"er={er} w/h={u}: {ours:.2f} vs IPC {ipc:.2f}")

    def test_microstrip_eps_eff_bounds_and_monotonicity(self):
        # 1 < eps_eff < er (half the field in air), Z0 monotone decreasing in w/h.
        er, h = 4.4, 0.2
        last = None
        for u in (0.2, 0.5, 1.0, 2.0, 5.0):
            res = analytic.microstrip_z0(u * h, h, er)
            self.assertGreater(res["eps_eff"], 1.0)
            self.assertLess(res["eps_eff"], er)
            if last is not None:
                self.assertLess(res["z0"], last)
            last = res["z0"]

    def test_te_wave_impedance_consistency_with_gamma(self):
        # Z_TE = omega*mu0/beta (Pozar 4e eq. 3.22) with beta taken from the
        # independently-coded gamma() dispersion — internal cross-check.
        fc = analytic.rect_cutoff_hz(0.02286, 0.01016, 1, 0)
        for f in (8e9, 10e9, 12e9):
            beta = float(np.imag(analytic.gamma(np.array([f]), fc)[0]))
            want = 2.0 * math.pi * f * analytic.MU0 / beta
            self.assertAlmostEqual(analytic.te_wave_impedance(f, fc), want, places=6)
        with self.assertRaises(ValueError):
            analytic.te_wave_impedance(fc * 0.9, fc)  # below cutoff: no wave


# ------------------------------------------------------------------------ FDFD


class TestFdfdKnownModes(unittest.TestCase):
    def test_square_guide_degenerate_te10_te01(self):
        # Square guide: TE10 and TE01 are exactly degenerate (fc = c/(2a) both).
        res = fdfd.solve_eigenmodes("rectangular", 0.02, 0.02, n_modes=3,
                                    resolution=48)
        ref = analytic.C0 / (2 * 0.02)  # fc = c/(2a), Pozar 4e eq. 3.84
        names = {md["name"] for md in res["modes"][:2]}
        self.assertEqual(names, {"TE10", "TE01"})
        for md in res["modes"][:2]:
            self.assertLess(abs(md["fc_hz"] - ref) / ref, 0.01)

    def test_circular_tm01(self):
        # TM01 circular cutoff: fc = p01*c/(2*pi*r), p01 = 2.4048 (first J0 root;
        # Pozar 4e Table 3.3).
        res = fdfd.solve_eigenmodes("circular", 0.01, 0.01, n_modes=4,
                                    resolution=100)
        tm = [md for md in res["modes"] if md["type"] == "TM"][0]
        ref = 2.4048 * analytic.C0 / (2 * math.pi * 0.01)
        self.assertEqual(tm["name"], "TM01")
        self.assertLess(abs(tm["fc_hz"] - ref) / ref, 0.01)

    def test_eps_r_scaling(self):
        # Dielectric fill scales every cutoff by 1/sqrt(eps_r).
        r1 = fdfd.solve_eigenmodes("rectangular", 0.02286, 0.01016, n_modes=1,
                                   resolution=48, eps_r=1.0)
        r2 = fdfd.solve_eigenmodes("rectangular", 0.02286, 0.01016, n_modes=1,
                                   resolution=48, eps_r=2.25)
        ratio = r1["modes"][0]["fc_hz"] / r2["modes"][0]["fc_hz"]
        self.assertAlmostEqual(ratio, 1.5, places=6)  # sqrt(2.25)


class TestFdfdConvergence(unittest.TestCase):
    def test_te10_error_shrinks_with_refinement(self):
        """Grid-refinement convergence — catches sign/scaling/BC bugs.

        The cell-centred 5-point scheme is ~2nd order, so each resolution
        doubling should cut the TE10 cutoff error by ~4x; require >= 2x to
        leave slack for eigsh tolerance.
        """
        a, b = 0.02286, 0.01016
        ref = analytic.C0 / (2 * a)  # TE10 fc = c/(2a)
        errs = []
        for res in (24, 48, 96):
            out = fdfd.solve_eigenmodes("rectangular", a, b, n_modes=1,
                                        resolution=res)
            md = out["modes"][0]
            self.assertEqual(md["name"], "TE10", msg=f"res={res}")
            errs.append(abs(md["fc_hz"] - ref) / ref)
        self.assertLess(errs[-1], 5e-4)          # finest grid well under 0.05%
        self.assertGreater(errs[0] / errs[1], 2.0, msg=f"errs={errs}")
        self.assertGreater(errs[1] / errs[2], 2.0, msg=f"errs={errs}")

    def test_tm11_error_shrinks_with_refinement(self):
        # Same sweep for the Dirichlet (TM) branch — exercises the ghost-cell BC.
        a, b = 0.02286, 0.01016
        ref = analytic.rect_cutoff_hz(a, b, 1, 1)  # fc from kc = pi*hypot(1/a,1/b)
        errs = []
        for res in (24, 48, 96):
            out = fdfd.solve_eigenmodes("rectangular", a, b, n_modes=6,
                                        resolution=res)
            tm = [md for md in out["modes"] if md["name"] == "TM11"]
            self.assertTrue(tm, msg=f"res={res}: no TM11 in {out['modes']}")
            errs.append(abs(tm[0]["fc_hz"] - ref) / ref)
        self.assertGreater(errs[0] / errs[2], 4.0, msg=f"errs={errs}")


class TestFdfdAdapterSpec(unittest.TestCase):
    def test_adapter_honours_resolution_and_eps(self):
        res = BuiltinFdfdAdapter().eigenmode(
            {"standard": "WR-90", "n_modes": 2, "resolution": 40,
             "waveguide": {"eps_r": 2.25}})
        self.assertEqual(res["solver"], "builtin-fdfd")
        self.assertEqual(res["solver_detail"]["grid"][0], 40)
        # WR-90 TE10 in eps_r=2.25: c/(2a*1.5) = 4.3714 GHz
        want = analytic.C0 / (2 * 0.02286 * 1.5) / 1e9
        self.assertLess(abs(res["modes"][0]["fc_ghz"] - want) / want, 0.01)


# --------------------------------------------------- adapter selection/fallback


class TestAdapterFallback(unittest.TestCase):
    def test_openems_absent_reports_cleanly(self):
        avail = OpenEmsAdapter().available()
        self.assertIn("installed", avail)
        if avail["installed"]:
            self.skipTest("openEMS installed here; absence path exercised on CI")
        self.assertIn("import failed", avail["detail"])

    def test_explicit_openems_request_raises_emsolvererror(self):
        if _openems_installed():
            self.skipTest("openEMS installed here")
        with self.assertRaises(EmSolverError) as ctx:
            select_adapter("driven", solver="openems")
        self.assertIn("not installed", str(ctx.exception))

    def test_auto_driven_falls_back_below_openems(self):
        ad = select_adapter("driven")
        if _openems_installed():
            self.assertEqual(ad.name, "openems")
        else:
            self.assertEqual(ad.name, "analytic-uniform")

    def test_scaffold_adapters_are_never_selected(self):
        # Gmsh/GetDP, Meep, Elmer are detection-only scaffolds (capabilities=()),
        # so auto-pick must never return them even if their binaries exist.
        for analysis in ("eigenmode", "driven", "timedomain"):
            ad = select_adapter(analysis)
            self.assertNotIn(ad.name, ("gmsh-getdp", "meep", "elmer"))
        for cls in (GetdpAdapter, MeepAdapter, ElmerAdapter):
            self.assertEqual(cls().capabilities, ())
            with self.assertRaises(EmSolverError):
                select_adapter("driven", solver=cls.name)

    def test_registry_infos_are_json_shaped(self):
        for ad in registry():
            info = ad.info()
            for key in ("name", "kind", "license", "linkage", "capabilities",
                        "installed", "version", "detail", "install"):
                self.assertIn(key, info, msg=ad.name)
            self.assertIsInstance(info["capabilities"], list)


class TestWorkerRpcDegradation(unittest.TestCase):
    def test_geometry_spec_degrades_to_error_envelope(self):
        # A full-wave geometry spec with no openEMS: the analytic fallback must
        # REFUSE (not fake) it, surfaced as {"ok": False} — never an exception.
        if _openems_installed():
            self.skipTest("openEMS installed here — geometry spec would solve")
        res = WORKER_METHODS["em_driven"](
            {"standard": "WR90", "geometry": {"stl_path": "/nonexistent.stl"}}, None)
        self.assertFalse(res["ok"])
        self.assertIn("full-wave", res["error"])
        self.assertIn("solvers", res)

    def test_explicit_openems_rpc_error_envelope(self):
        if _openems_installed():
            self.skipTest("openEMS installed here")
        for method in ("em_driven", "em_timedomain"):
            res = WORKER_METHODS[method]({"standard": "WR90",
                                          "solver": "openems"}, None)
            self.assertFalse(res["ok"], msg=method)
            self.assertIn("not installed", res["error"], msg=method)

    def test_eigenmode_solver_capability_mismatch_envelope(self):
        res = WORKER_METHODS["em_eigenmode"]({"standard": "WR90",
                                              "solver": "openems"}, None)
        self.assertFalse(res["ok"])
        self.assertIn("cannot run", res["error"])

    def test_status_never_raises_and_lists_defaults(self):
        res = WORKER_METHODS["em_status"]({}, None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["defaults"]["eigenmode"], "builtin-fdfd")
        self.assertIn(res["defaults"]["driven"], ("openems", "analytic-uniform"))


# --------------------------------------------------------------- upstream seam


class TestUpstreamSeam(unittest.TestCase):
    def test_engine_alias_import_matches_worker_registration(self):
        # engine_oracle/worker.py registers the EM RPCs via
        # `from engine.sim.em.worker_rpc import WORKER_METHODS` — `engine` is a
        # repo-root symlink to engine_oracle. If the alias breaks, em_* silently
        # vanish from the worker (the try/except only logs). Assert it here.
        import importlib
        mod = importlib.import_module("engine.sim.em.worker_rpc")
        self.assertEqual(set(mod.WORKER_METHODS),
                         {"em_status", "em_eigenmode", "em_driven",
                          "em_timedomain"})
        # NOTE: the symlink means `engine.sim.em` and `engine_oracle.sim.em` are
        # two module objects (classic dual-import); the RPC surface is stateless
        # so only the method-name sets must agree.
        from ...sim.em import worker_rpc as canonical
        self.assertEqual(set(mod.WORKER_METHODS), set(canonical.WORKER_METHODS))
        # the alias RPCs actually run
        res = mod.WORKER_METHODS["em_eigenmode"]({"standard": "WR-90",
                                                  "n_modes": 1}, None)
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
