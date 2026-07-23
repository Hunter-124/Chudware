"""EM sim family tests — mock-proven openEMS pipeline + REAL builtin-solver validation.

Discipline (offline-ML track rules): nothing here is all-skip.  The FDFD
eigensolver and analytic adapters run for real and are validated against
textbook numbers (WR-90 TE10 cutoff 6.5571 GHz).  The openEMS adapter's plan
builder, S-parameter math and result shaping run for real with only the
``_execute`` solver seam replaced by an analytic synthesiser; one live test
runs iff openEMS is actually installed.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from ...sim.em import analytic, fdfd
from ...sim.em.adapters import (
    AnalyticAdapter, BuiltinFdfdAdapter, EmSolverError, registry, select_adapter,
    resolve_cross_section)
from ...sim.em.openems_adapter import OpenEmsAdapter
from ...sim.em.worker_rpc import WORKER_METHODS

WR90_TE10_HZ = 6.5571e9  # c / (2 * 22.86 mm)


class TestAnalytic(unittest.TestCase):
    def test_wr90_te10_cutoff(self):
        fc = analytic.rect_cutoff_hz(0.02286, 0.01016, 1, 0)
        self.assertAlmostEqual(fc / 1e9, WR90_TE10_HZ / 1e9, places=3)

    def test_standard_lookup_variants(self):
        for name in ("WR-90", "WR90", "wr 90", "wr_90"):
            self.assertEqual(analytic.lookup_standard(name), (22.86, 10.16))
        with self.assertRaises(ValueError):
            analytic.lookup_standard("WR-999")

    def test_mode_table_ordering(self):
        table = analytic.rect_mode_table(0.02286, 0.01016, 5)
        self.assertEqual(table[0]["name"], "TE10")
        fcs = [md["fc_hz"] for md in table]
        self.assertEqual(fcs, sorted(fcs))

    def test_uniform_section_sparams_physics(self):
        f = np.array([4e9, 10e9])  # below and above WR-90 cutoff
        s11, s21 = analytic.uniform_section_sparams(f, WR90_TE10_HZ, 0.10)
        self.assertLess(abs(s21[0]), 1e-3)             # evanescent: alpha*L ~ 10.9 Np
        self.assertAlmostEqual(abs(s21[1]), 1.0, places=6)  # propagating: lossless
        self.assertTrue(np.all(np.abs(s11) == 0.0))
        # phase at 10 GHz: beta = sqrt(k^2 - kc^2), S21 = exp(-j beta L)
        k = 2 * math.pi * 10e9 / analytic.C0
        kc = 2 * math.pi * WR90_TE10_HZ / analytic.C0
        beta = math.sqrt(k**2 - kc**2)
        self.assertAlmostEqual(np.angle(s21[1]), math.atan2(-math.sin(beta * 0.10),
                                                            math.cos(beta * 0.10)), places=6)

    def test_timedomain_pulse_delay(self):
        # group delay of TE10 at f0: L / vg, vg = c*sqrt(1-(fc/f0)^2)
        L, f0 = 0.2, 10e9
        res = analytic.timedomain_response(WR90_TE10_HZ, L, f0, 1e9)
        t_in = res["t_s"][np.argmax(np.abs(res["v_in"]))]
        t_out = res["t_s"][np.argmax(np.abs(res["v_out"]))]
        vg = analytic.C0 * math.sqrt(1 - (WR90_TE10_HZ / f0) ** 2)
        self.assertAlmostEqual((t_out - t_in) * 1e9, L / vg * 1e9, delta=0.15)


class TestFdfd(unittest.TestCase):
    def test_wr90_validation(self):
        """The headline validation: WR-90 TE10 cutoff within 1% of 6.557 GHz."""
        res = fdfd.solve_eigenmodes("rectangular", 0.02286, 0.01016, n_modes=5)
        te10 = res["modes"][0]
        self.assertEqual(te10["name"], "TE10")
        self.assertLess(abs(te10["fc_hz"] - WR90_TE10_HZ) / WR90_TE10_HZ, 0.01)
        names = [md["name"] for md in res["modes"]]
        self.assertIn("TE20", names)
        self.assertIn("TM11", names)

    def test_circular_te11(self):
        res = fdfd.solve_eigenmodes("circular", 0.01, 0.01, n_modes=3, resolution=100)
        te11 = res["modes"][0]
        ref = 1.8412 * analytic.C0 / (2 * math.pi * 0.01)
        self.assertEqual(te11["name"], "TE11")
        self.assertLess(abs(te11["fc_hz"] - ref) / ref, 0.01)

    def test_field_preview(self):
        res = fdfd.solve_eigenmodes("rectangular", 0.02286, 0.01016, n_modes=1,
                                    resolution=48, want_fields=True)
        field = res["modes"][0]["field"]
        self.assertLessEqual(len(field), 48)
        self.assertLessEqual(max(abs(v) for row in field for v in row), 1.0)


class TestAdapters(unittest.TestCase):
    def test_cross_section_resolution(self):
        cs = resolve_cross_section({"standard": "WR-90"})
        self.assertAlmostEqual(cs["a_m"], 0.02286)
        cs = resolve_cross_section({"waveguide": {"shape": "circular", "radius_mm": 10}})
        self.assertEqual(cs["shape"], "circular")
        with self.assertRaises(EmSolverError):
            resolve_cross_section({"waveguide": {"a_mm": 22.86}})

    def test_registry_and_selection(self):
        names = [ad.name for ad in registry()]
        self.assertEqual(names[0], "openems")           # preference order
        self.assertIn("builtin-fdfd", names)
        # eigenmode auto-pick: openEMS doesn't offer it -> builtin-fdfd
        self.assertEqual(select_adapter("eigenmode").name, "builtin-fdfd")
        with self.assertRaises(EmSolverError):
            select_adapter("driven", solver="builtin-fdfd")  # capability mismatch
        with self.assertRaises(EmSolverError):
            select_adapter("driven", solver="no-such")

    def test_analytic_driven_shape(self):
        res = AnalyticAdapter().driven({"standard": "WR90", "length_mm": 100,
                                        "f_start_ghz": 5, "f_stop_ghz": 12, "n_freq": 51})
        self.assertEqual(len(res["freq_ghz"]), 51)
        self.assertEqual(len(res["s21_db"]), 51)
        self.assertEqual(len(res["s21"][0]), 2)          # [re, im]
        below = res["s21_db"][0]                         # 5 GHz < cutoff (~-77 dB @ 100 mm)
        above = res["s21_db"][-1]                        # 12 GHz > cutoff
        self.assertLess(below, -60.0)
        self.assertAlmostEqual(above, 0.0, places=3)

    def test_analytic_refuses_geometry(self):
        with self.assertRaises(EmSolverError):
            AnalyticAdapter().driven({"standard": "WR90",
                                      "geometry": {"stl_path": "/nope.stl"}})


class _MockedOpenEms(OpenEmsAdapter):
    """openEMS adapter with ONLY the solver-binary seam replaced.

    _execute synthesises port spectra from the analytic TE10 model over the
    plan's port separation — so driven()/timedomain() exercise the real plan
    builder, S-parameter division, and result shaping end-to-end.
    """

    executed_plans: list

    def __init__(self):
        self.executed_plans = []

    def available(self):
        return {"installed": True, "version": "mock", "detail": "test seam"}

    def _execute(self, plan, want_time=False):
        self.executed_plans.append(plan)
        fr = plan["freq"]
        freq = np.linspace(fr["start_hz"], fr["stop_hz"], fr["n"])
        sep_m = plan["port_separation_mm"] / 1e3
        _, s21 = analytic.uniform_section_sparams(freq, plan["fc_hz"], sep_m)
        inc = np.ones_like(freq, dtype=complex)
        out = {"freq_hz": freq.tolist(), "p1_inc": inc,
               "p1_ref": np.zeros_like(inc), "p2_ref": s21 * inc}
        if want_time:
            td = analytic.timedomain_response(
                plan["fc_hz"], sep_m,
                plan["excitation"]["f0_hz"], plan["excitation"]["fc_hz"], n_samples=1024)
            out["time"] = {"t_s": td["t_s"].tolist(),
                           "port1_v": td["v_in"].tolist(),
                           "port2_v": td["v_out"].tolist()}
        return out


class TestOpenEmsPipeline(unittest.TestCase):
    """Mock-proven: everything except the FDTD binary itself."""

    def test_plan_builder(self):
        plan = OpenEmsAdapter().build_plan({"standard": "WR-90", "length_mm": 80,
                                            "f_start_ghz": 6, "f_stop_ghz": 13})
        self.assertEqual(plan["mode"], "TE10")
        self.assertAlmostEqual(plan["fc_hz"] / 1e9, 6.5571, places=3)
        self.assertEqual(len(plan["ports"]), 2)
        self.assertEqual(plan["boundaries"][4], "PML_8")
        # mesh resolves >= 20 cells/wavelength at f_stop
        lam_mm = analytic.C0 / 13e9 * 1e3
        dz = plan["mesh"]["z"][1] - plan["mesh"]["z"][0]
        self.assertLessEqual(dz, lam_mm / 20 + 1e-9)
        self.assertGreaterEqual(len(plan["mesh"]["x"]), 15)

    def test_plan_rejects_bad_specs(self):
        ad = OpenEmsAdapter()
        with self.assertRaises(EmSolverError):
            ad.build_plan({"waveguide": {"shape": "circular", "radius_mm": 10}})
        with self.assertRaises(EmSolverError):
            ad.build_plan({"standard": "WR90", "geometry": {"stl_path": "/missing.stl"}})
        with self.assertRaises(EmSolverError):
            ad.build_plan({"standard": "WR90", "f_start_ghz": 10, "f_stop_ghz": 10})

    def test_driven_pipeline_matches_analytic(self):
        ad = _MockedOpenEms()
        res = ad.driven({"standard": "WR-90", "length_mm": 100,
                         "f_start_ghz": 5, "f_stop_ghz": 12, "n_freq": 101})
        self.assertEqual(res["solver"], "openems")
        self.assertEqual(len(ad.executed_plans), 1)
        f = np.array(res["freq_ghz"])
        s21_db = np.array(res["s21_db"])
        # below cutoff: deep attenuation over the 70 mm port separation; above: ~0 dB
        self.assertTrue(np.all(s21_db[f < 5.5] < -40.0))
        self.assertTrue(np.all(np.abs(s21_db[f > 8.0]) < 0.01))
        self.assertEqual(len(res["s11"]), 101)

    def test_timedomain_pipeline(self):
        res = _MockedOpenEms().timedomain({"standard": "WR-90", "length_mm": 100})
        self.assertIn("t_ns", res)
        self.assertEqual(len(res["ports"]["port1_v"]), len(res["t_ns"]))
        self.assertIn("spectrum", res)
        self.assertGreater(np.max(np.abs(res["ports"]["port2_v"])), 0.05)

    def test_live_openems_if_installed(self):
        """REAL WR-90 FDTD validation — runs only where openEMS exists."""
        ad = OpenEmsAdapter()
        if not ad.available()["installed"]:
            self.skipTest("openEMS not installed on this machine (adapter is "
                          "mock-proven above; see docs/EM_SIMULATION.md to install)")
        res = ad.driven({"standard": "WR-90", "length_mm": 60,
                         "f_start_ghz": 7, "f_stop_ghz": 12, "n_freq": 51})
        f = np.array(res["freq_ghz"])
        s21_db = np.array(res["s21_db"])
        self.assertTrue(np.all(s21_db[f > 8.0] > -1.0))  # propagating band ~0 dB


class TestWorkerRpc(unittest.TestCase):
    def test_em_status(self):
        res = WORKER_METHODS["em_status"]({}, None)
        self.assertTrue(res["ok"])
        names = {s["name"] for s in res["solvers"]}
        self.assertLessEqual({"openems", "builtin-fdfd", "analytic-uniform",
                              "gmsh-getdp", "meep", "elmer"}, names)
        self.assertEqual(res["defaults"]["eigenmode"], "builtin-fdfd")
        for s in res["solvers"]:
            for key in ("license", "linkage", "installed", "install", "capabilities"):
                self.assertIn(key, s)

    def test_em_eigenmode_rpc(self):
        res = WORKER_METHODS["em_eigenmode"]({"standard": "WR-90", "n_modes": 3}, None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["modes"][0]["name"], "TE10")
        self.assertLess(abs(res["modes"][0]["fc_ghz"] - 6.5571), 0.05)

    def test_em_driven_rpc_auto_falls_back(self):
        # openEMS absent on CI -> analytic-uniform picks up driven
        res = WORKER_METHODS["em_driven"]({"standard": "WR90", "length_mm": 50,
                                           "n_freq": 21}, None)
        self.assertTrue(res["ok"])
        self.assertIn(res["solver"], ("openems", "analytic-uniform"))
        self.assertEqual(len(res["freq_ghz"]), 21)

    def test_em_timedomain_rpc(self):
        res = WORKER_METHODS["em_timedomain"]({"standard": "WR90", "length_mm": 80}, None)
        self.assertTrue(res["ok"])
        self.assertIn("ports", res)

    def test_error_envelope(self):
        res = WORKER_METHODS["em_driven"]({"solver": "no-such"}, None)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)
        self.assertIn("solvers", res)  # capability report rides along on failure
        res = WORKER_METHODS["em_eigenmode"]({}, None)
        self.assertFalse(res["ok"])   # no waveguide spec


if __name__ == "__main__":
    unittest.main()
