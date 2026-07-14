"""Parametric PCB antenna calculators — textbook dimensions + copper geometry.

Run:
  /home/hunter/.local/share/AI-CAD/mcp-venv/bin/python -X utf8 -m pytest \\
      engine_oracle/ecad/test_antenna.py -q
"""
from __future__ import annotations

import math
import time
import unittest

from engine.ecad import impedance as z0_mod
from engine.ecad import schema
from engine_oracle.ecad import antenna as ant


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1e3


class TestFeedSynthesis(unittest.TestCase):
    def test_microstrip_50ohm_crosscheck(self):
        """50 Ω microstrip width must re-evaluate to ~50 Ω via impedance.py."""
        t0 = time.perf_counter()
        # FR-4 1.6 mm, 1 oz copper — classic ~3 mm 50 Ω microstrip ballpark.
        er, h, t = 4.4, 1.6, 0.035
        feed = ant.synthesize_feed_width(
            50.0, substrate_er=er, substrate_h_mm=h, copper_t_mm=t, kind="microstrip",
        )
        w = feed["width_mm"]
        check = z0_mod.microstrip_z0(w, h, t, er)
        print(f"  feed microstrip 50Ω: w={w:.4f} mm z0={feed['z0']:.3f} "
              f"check={check['z0']:.3f}  [{_ms(t0):.2f} ms]")
        self.assertAlmostEqual(check["z0"], 50.0, delta=0.5)
        self.assertAlmostEqual(feed["z0"], check["z0"], delta=0.05)
        # Sanity: IPC-2141 width for 50 Ω on FR-4 1.6 mm is roughly 2.5–3.5 mm.
        self.assertGreater(w, 1.5)
        self.assertLess(w, 5.0)

    def test_cpwg_50ohm_crosscheck(self):
        t0 = time.perf_counter()
        er, h, t, s = 4.4, 1.6, 0.035, 0.5
        feed = ant.synthesize_feed_width(
            50.0, substrate_er=er, substrate_h_mm=h, copper_t_mm=t,
            kind="cpwg", gap_mm=s,
        )
        w = feed["width_mm"]
        check = z0_mod.coplanar_z0(w, s, h, t, er)
        print(f"  feed cpwg 50Ω: w={w:.4f} mm gap={s} z0={feed['z0']:.3f} "
              f"check={check['z0']:.3f}  [{_ms(t0):.2f} ms]")
        self.assertAlmostEqual(check["z0"], 50.0, delta=1.0)
        self.assertAlmostEqual(feed["z0"], check["z0"], delta=0.1)


class TestPatchTextbook(unittest.TestCase):
    def test_patch_2p4ghz_fr4(self):
        """Rectangular patch @ 2.4 GHz, FR-4 εr=4.4, h=1.6 mm.

        Balanis §14.2 / Pozar §14.2::
            W   = c/(2f √((εr+1)/2))
            εeff = (εr+1)/2 + (εr-1)/2 · (1+12 h/W)^(-1/2)
            ΔL  = 0.412 h (εeff+0.3)(W/h+0.264)/((εeff-0.258)(W/h+0.8))
            L   = c/(2f √εeff) − 2 ΔL
        """
        t0 = time.perf_counter()
        f = 2.4e9
        er, h_mm = 4.4, 1.6
        h = h_mm * 1e-3
        c = ant.C0

        # Hand-worked reference (same equations as the module).
        w_ref = c / (2.0 * f * math.sqrt((er + 1.0) / 2.0))
        u = w_ref / h
        eeff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 12.0 / u) ** -0.5
        dl = 0.412 * h * (eeff + 0.3) * (u + 0.264) / ((eeff - 0.258) * (u + 0.8))
        l_ref = c / (2.0 * f * math.sqrt(eeff)) - 2.0 * dl

        design = ant.design_patch(f, substrate_er=er, substrate_h_mm=h_mm)
        d = design["dimensions"]
        print(f"  patch 2.4GHz: W={d['width_mm']:.3f} L={d['length_mm']:.3f} mm "
              f"(ref W={w_ref*1e3:.3f} L={l_ref*1e3:.3f})  [{_ms(t0):.2f} ms]")

        self.assertAlmostEqual(d["width_mm"], w_ref * 1e3, delta=0.05)
        self.assertAlmostEqual(d["length_mm"], l_ref * 1e3, delta=0.05)
        # Typical FR-4 2.4 GHz patch is ~30–40 mm on a side.
        self.assertGreater(d["width_mm"], 25.0)
        self.assertLess(d["width_mm"], 45.0)
        self.assertGreater(d["length_mm"], 25.0)
        self.assertLess(d["length_mm"], 40.0)
        # Within a few percent of the textbook hand calc.
        self.assertLess(abs(d["width_mm"] - w_ref * 1e3) / (w_ref * 1e3), 0.01)
        self.assertLess(abs(d["length_mm"] - l_ref * 1e3) / (l_ref * 1e3), 0.01)


class TestMonopoleTextbook(unittest.TestCase):
    def test_monopole_quarter_wave(self):
        """Quarter-wave monopole: L = vf · c / (4 f).

        Free-space (vf=1) @ 2.45 GHz → L = 30.59 mm.
        """
        t0 = time.perf_counter()
        f = 2.45e9
        l_free = ant.C0 / (4.0 * f) * 1e3  # mm
        design = ant.design_monopole(f, velocity_factor=1.0)
        d = design["dimensions"]
        print(f"  monopole 2.45GHz vf=1: L={d['length_mm']:.4f} mm "
              f"(ref {l_free:.4f})  [{_ms(t0):.2f} ms]")
        self.assertAlmostEqual(d["length_mm"], l_free, delta=0.05)
        self.assertLess(abs(d["length_mm"] - l_free) / l_free, 0.01)

        # With auto vf (microstrip eeff) length must be shorter than free-space.
        design2 = ant.design_monopole(f)  # auto vf
        d2 = design2["dimensions"]
        print(f"  monopole 2.45GHz auto-vf={d2['velocity_factor']:.4f}: "
              f"L={d2['length_mm']:.4f} mm  [{_ms(t0):.2f} ms]")
        self.assertLess(d2["length_mm"], l_free)
        self.assertGreater(d2["length_mm"], l_free * 0.4)


class TestIFA(unittest.TestCase):
    def test_ifa_2p4_total_length(self):
        """Meandered IFA total path ≈ λg/4 = c/(4 f √εeff)."""
        t0 = time.perf_counter()
        f = 2.45e9
        design = ant.design_ifa(f, substrate_er=4.4, substrate_h_mm=1.6, meander_turns=3)
        d = design["dimensions"]
        eeff = d["eeff"]
        l_ref = ant.C0_MM_S / (4.0 * f * math.sqrt(eeff))
        print(f"  ifa 2.45GHz: Ltot={d['total_length_mm']:.3f} mm "
              f"(ref {l_ref:.3f}) span={d['span_mm']:.3f}  [{_ms(t0):.2f} ms]")
        self.assertAlmostEqual(d["total_length_mm"], l_ref, delta=0.1)
        self.assertLess(abs(d["total_length_mm"] - l_ref) / l_ref, 0.02)
        self.assertEqual(d["meander_turns"], 3)
        self.assertGreater(d["span_mm"], 0.0)

    def test_pifa_alias(self):
        t0 = time.perf_counter()
        d = ant.design_pifa(2.45e9)
        self.assertEqual(d["kind"], "pifa")
        self.assertEqual(d["dimensions"]["kind"], "pifa")
        print(f"  pifa kind ok  [{_ms(t0):.2f} ms]")


class TestGeometryCopper(unittest.TestCase):
    def _check_geometry(self, geom: dict):
        self.assertIn("tracks", geom)
        self.assertIn("zones", geom)
        self.assertTrue(len(geom["tracks"]) + len(geom["zones"]) >= 1)
        for t in geom["tracks"]:
            self.assertIn(t.get("kind"), ("segment", "arc", None))
            self.assertIn("layer", t)
            self.assertIn("width", t)
            self.assertGreater(float(t["width"]), 0)
            if "p1" in t and "p2" in t:
                self.assertEqual(len(t["p1"]), 2)
                self.assertEqual(len(t["p2"]), 2)
            if "points" in t:
                self.assertGreaterEqual(len(t["points"]), 2)
        for z in geom["zones"]:
            self.assertIn("polygon", z)
            self.assertGreaterEqual(len(z["polygon"]), 3)
            self.assertIn(z.get("fill", "solid"), ("solid", "hatch"))
            self.assertIn("layer", z)

    def test_patch_geometry_on_board(self):
        t0 = time.perf_counter()
        design = ant.design_antenna("patch", 2.4e9, substrate_er=4.4, substrate_h_mm=1.6)
        geom = design["geometry"]((10.0, 10.0))
        self._check_geometry(geom)

        board = schema.new_board("ant_patch")
        placed = ant.place_antenna_on_board(board, design, (10.0, 10.0))
        self.assertGreaterEqual(len(board["tracks"]), 1)
        self.assertGreaterEqual(len(board["zones"]), 1)
        # Nets registered.
        names = {n["name"] for n in board["nets"]}
        self.assertIn("ANT", names)
        self.assertIn("GND", names)
        # Schema validation should not flag our copper (layers / kinds / nets).
        errs = schema.validate_board(board)
        # Filter only our concerns: no track/zone layer or kind errors.
        serious = [e for e in errs if "layer" in e or "kind" in e or "fill" in e]
        print(f"  patch place: tracks={len(board['tracks'])} zones={len(board['zones'])} "
              f"validate_errs={len(errs)} serious={serious}  [{_ms(t0):.2f} ms]")
        self.assertEqual(serious, [])
        self._check_geometry(placed)

    def test_monopole_and_ifa_geometry(self):
        t0 = time.perf_counter()
        for kind in ("monopole", "ifa", "pifa"):
            design = ant.design_antenna(kind, 2.45e9)
            geom = design["geometry"]((0.0, 0.0))
            self._check_geometry(geom)
            board = schema.new_board(f"ant_{kind}")
            ant.place_antenna_on_board(board, design, (0.0, 0.0))
            errs = schema.validate_board(board)
            serious = [e for e in errs if "layer" in e or "kind" in e or "fill" in e]
            self.assertEqual(serious, [], f"{kind}: {serious}")
        print(f"  monopole/ifa/pifa geometry ok  [{_ms(t0):.2f} ms]")


class TestDispatch(unittest.TestCase):
    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            ant.design_antenna("helix", 1e9)

    def test_design_antenna_dispatch(self):
        t0 = time.perf_counter()
        for k in ant.SUPPORTED_KINDS:
            d = ant.design_antenna(k, 2.4e9)
            self.assertEqual(d["kind"], k if k != "pifa" else "pifa")
            self.assertIn("dimensions", d)
            self.assertTrue(callable(d["geometry"]))
        print(f"  dispatch all kinds  [{_ms(t0):.2f} ms]")


if __name__ == "__main__":
    unittest.main()
