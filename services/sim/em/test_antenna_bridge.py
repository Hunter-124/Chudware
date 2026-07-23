"""Antenna EM bridge — analytic always + openEMS full-wave / honest skip.

Run:
  /home/hunter/.local/share/Chudware/mcp-venv/bin/python -X utf8 -m pytest \\
      engine_oracle/sim/em/test_antenna_bridge.py -q
"""
from __future__ import annotations

import math
import time
import unittest

import numpy as np

from services.ecad import antenna as ant
from ...sim.em import antenna_bridge as bridge


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1e3


class TestOpenEmsAvailability(unittest.TestCase):
    def test_available_reports_bool(self):
        t0 = time.perf_counter()
        info = bridge.openems_available()
        print(f"  openems available={info['installed']} ver={info.get('version')} "
              f"[{_ms(t0):.2f} ms]")
        self.assertIn("installed", info)
        self.assertIsInstance(info["installed"], bool)
        # This box claims openEMS 0.37.0 in the mcp-venv.
        if info["installed"]:
            self.assertIsNotNone(info.get("version"))


class TestAnalyticResonance(unittest.TestCase):
    def test_patch_analytic_matches_design(self):
        t0 = time.perf_counter()
        design = ant.design_patch(2.4e9, substrate_er=4.4, substrate_h_mm=1.6)
        res = bridge.analytic_resonance(design)
        d = design["dimensions"]
        # Recompute Balanis invert: f = c / (2 (L+2ΔL) √εeff)
        l_m = d["length_mm"] * 1e-3
        dl_m = d["delta_l_mm"] * 1e-3
        eeff = d["eeff"]
        f_ref = bridge.C0 / (2.0 * (l_m + 2.0 * dl_m) * math.sqrt(eeff))
        print(f"  analytic patch f={res['f_res_ghz']:.4f} GHz "
              f"(ref {f_ref/1e9:.4f}, target 2.4)  [{_ms(t0):.2f} ms]")
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["f_res_hz"], f_ref, delta=f_ref * 0.01)
        # Within a few percent of the design target.
        self.assertLess(abs(res["f_res_hz"] - 2.4e9) / 2.4e9, 0.05)
        self.assertIn("bandwidth_hz", res)
        self.assertIn("s11_est_db", res)

    def test_monopole_analytic(self):
        t0 = time.perf_counter()
        design = ant.design_monopole(2.45e9, velocity_factor=1.0)
        res = bridge.analytic_resonance(design)
        l_m = design["dimensions"]["length_mm"] * 1e-3
        f_ref = 1.0 * bridge.C0 / (4.0 * l_m)
        print(f"  analytic monopole f={res['f_res_ghz']:.4f} GHz "
              f"(ref {f_ref/1e9:.4f})  [{_ms(t0):.2f} ms]")
        self.assertAlmostEqual(res["f_res_hz"], f_ref, delta=1e5)

    def test_ifa_analytic(self):
        t0 = time.perf_counter()
        design = ant.design_ifa(2.45e9)
        res = bridge.analytic_resonance(design)
        print(f"  analytic ifa f={res['f_res_ghz']:.4f} GHz  [{_ms(t0):.2f} ms]")
        self.assertTrue(res["ok"])
        self.assertGreater(res["f_res_hz"], 1e9)


class TestFullwaveSkip(unittest.TestCase):
    def test_force_skip_honest(self):
        t0 = time.perf_counter()
        design = ant.design_patch(2.4e9)
        fw = bridge.fullwave_s11(design, force_skip=True)
        print(f"  fullwave force-skip: {fw.get('reason', '')[:60]}  [{_ms(t0):.2f} ms]")
        self.assertFalse(fw["ok"])
        self.assertTrue(fw["skipped"])
        self.assertIn("reason", fw)
        self.assertIn("openEMS", fw["reason"])

    def test_monkeypatch_absence(self):
        t0 = time.perf_counter()
        design = ant.design_monopole(2.45e9)
        real = bridge.openems_available

        def _gone():
            return {"installed": False, "version": None, "detail": "monkeypatched absent"}

        bridge.openems_available = _gone  # type: ignore[assignment]
        try:
            fw = bridge.fullwave_s11(design)
            self.assertTrue(fw["skipped"])
            self.assertFalse(fw["ok"])
            self.assertIn("not installed", fw["reason"])
        finally:
            bridge.openems_available = real  # type: ignore[assignment]
        print(f"  monkeypatch absence ok  [{_ms(t0):.2f} ms]")


class TestFullwaveRealOrMock(unittest.TestCase):
    def test_analyze_always_has_analytic(self):
        t0 = time.perf_counter()
        design = ant.design_patch(2.4e9)
        # Force skip fullwave so this is instant and deterministic.
        out = bridge.analyze_antenna(design, want_fullwave=True, force_skip_fullwave=True)
        print(f"  analyze (skip fw): source={out['source']} "
              f"f={out['f_res_ghz']}  [{_ms(t0):.2f} ms]")
        self.assertTrue(out["ok"])
        self.assertIn("analytic", out)
        self.assertTrue(out["analytic"]["ok"])
        self.assertTrue(out.get("fullwave_skipped") or out["fullwave"].get("skipped"))
        self.assertEqual(out["source"], "analytic")

    def test_fullwave_with_injected_execute(self):
        """Plan + S11 shaping path without the FDTD binary (mock-proven)."""
        t0 = time.perf_counter()
        design = ant.design_patch(2.4e9)
        f0 = 2.4e9
        freq = np.linspace(f0 * 0.7, f0 * 1.3, 41)
        # Synthetic S11: deep notch at f0.
        s11 = 0.3 * np.ones(len(freq), dtype=complex)
        s11[np.argmin(np.abs(freq - f0))] = 0.05 + 0j

        def _fake_execute(plan):
            return {
                "freq_hz": freq.tolist(),
                "s11": s11,
                "solver_detail": {"method": "mock"},
            }

        fw = bridge.fullwave_s11(design, _execute=_fake_execute)
        print(f"  fullwave mock: s11_min={fw.get('s11_min_db')} "
              f"f={fw.get('f_res_ghz')}  [{_ms(t0):.2f} ms]")
        self.assertTrue(fw["ok"])
        self.assertFalse(fw.get("skipped"))
        self.assertLess(fw["s11_min_db"], -20.0)
        self.assertAlmostEqual(fw["f_res_ghz"], 2.4, delta=0.05)

    def test_real_openems_if_present(self):
        """Exercise the real openEMS path when installed (this box has 0.37.0).

        Uses a coarse mesh / short timestep budget so wall-time stays bounded.
        If the run fails for environmental reasons we still require a structured
        result (ok or skipped/reason) — never a traceback out of the bridge.
        """
        info = bridge.openems_available()
        if not info.get("installed"):
            self.skipTest(f"openEMS not installed: {info.get('detail')}")

        t0 = time.perf_counter()
        # Slightly lower frequency + short budget for CI friendliness.
        design = ant.design_patch(
            2.4e9, substrate_er=4.4, substrate_h_mm=1.6,
        )
        out = bridge.analyze_antenna(
            design,
            want_fullwave=True,
            n_freq=21,
            max_timesteps=8_000,
        )
        elapsed = _ms(t0)
        print(f"  real openEMS analyze: source={out.get('source')} "
              f"fw_ok={out.get('fullwave', {}).get('ok')} "
              f"skipped={out.get('fullwave', {}).get('skipped')} "
              f"s11={out.get('s11_min_db')}  [{elapsed:.1f} ms]")
        self.assertTrue(out["ok"])
        self.assertIn("analytic", out)
        self.assertTrue(out["analytic"]["ok"])
        fw = out["fullwave"]
        # Either a real result or a structured failure/skip — never missing keys.
        self.assertIn("ok", fw)
        if fw.get("ok") and not fw.get("skipped"):
            self.assertIn("s11_min_db", fw)
            self.assertIn("f_res_ghz", fw)
            self.assertIsInstance(fw["s11_min_db"], (int, float))
            # Sanity: S11 in dB should be negative-ish for a passive antenna.
            self.assertLess(fw["s11_min_db"], 5.0)
        else:
            # Structured failure is acceptable if the lightweight mesh diverges;
            # still must not look fabricated (no fake deep null without ok=True).
            self.assertTrue(
                fw.get("skipped") or fw.get("reason"),
                msg=f"expected skip/reason on failure, got {fw}",
            )


if __name__ == "__main__":
    unittest.main()
