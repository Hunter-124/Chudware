"""MCP antenna tools — design + analyze envelopes, openEMS skip.

Run:
  python -X utf8 -m pytest \\
      services/mcp/test_antenna_tools.py -q
"""
from __future__ import annotations

import json
import time
import unittest

from services.mcp import antenna_tools


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1e3


class _FakeMcp:
    """Minimal FastMCP stand-in that records @mcp.tool() registrations."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):  # noqa: ARG002
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _load() -> dict:
    mcp = _FakeMcp()
    antenna_tools.register_antenna_tools(mcp)
    return mcp.tools


def _call(tools: dict, name: str, **kwargs) -> dict:
    raw = tools[name](**kwargs)
    assert isinstance(raw, str), f"{name} must return a JSON string"
    return json.loads(raw)


class TestAntennaMcpTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _load()

    def test_registered(self):
        for name in ("ecad_antenna_design", "ecad_antenna_analyze"):
            self.assertIn(name, self.tools)

    def test_design_patch(self):
        t0 = time.perf_counter()
        r = _call(self.tools, "ecad_antenna_design",
                  kind="patch", freq_hz=2.4e9,
                  substrate_er=4.4, substrate_h_mm=1.6)
        print(f"  mcp design patch ok={r.get('ok')} "
              f"W={r.get('dimensions', {}).get('width_mm')}  [{_ms(t0):.2f} ms]")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "patch")
        self.assertIn("dimensions", r)
        self.assertIn("geometry", r)
        self.assertGreater(r["dimensions"]["width_mm"], 20.0)
        self.assertIn("feed", r["dimensions"])
        self.assertAlmostEqual(r["dimensions"]["feed"]["z0"], 50.0, delta=1.0)
        self.assertIn("hint", r)
        self.assertIn("next_actions", r)
        # Geometry is real copper.
        g = r["geometry"]
        self.assertGreaterEqual(g.get("n_tracks", 0) + g.get("n_zones", 0), 1)

    def test_design_monopole_and_ifa(self):
        t0 = time.perf_counter()
        for kind in ("monopole", "ifa", "pifa"):
            r = _call(self.tools, "ecad_antenna_design",
                      kind=kind, freq_hz=2.45e9)
            self.assertTrue(r["ok"], f"{kind}: {r}")
            self.assertEqual(r["kind"], kind)
        print(f"  mcp design mono/ifa/pifa  [{_ms(t0):.2f} ms]")

    def test_design_bad_kind(self):
        r = _call(self.tools, "ecad_antenna_design", kind="yagi", freq_hz=1e9)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        self.assertIn("hint", r)

    def test_analyze_analytic_only(self):
        t0 = time.perf_counter()
        r = _call(self.tools, "ecad_antenna_analyze",
                  kind="patch", freq_hz=2.4e9, want_fullwave=False)
        print(f"  mcp analyze analytic: f={r.get('f_res_ghz')} "
              f"source={r.get('source')}  [{_ms(t0):.2f} ms]")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "analytic")
        self.assertIn("analytic", r)
        self.assertTrue(r["analytic"]["ok"])
        self.assertIn("f_res_ghz", r)

    def test_analyze_fullwave_skip_or_real(self):
        """With want_fullwave=True: either real openEMS result or honest skip.

        Monkeypatch absence path is covered separately; here we accept either
        outcome but require a structured envelope.
        """
        t0 = time.perf_counter()
        r = _call(self.tools, "ecad_antenna_analyze",
                  kind="monopole", freq_hz=2.45e9,
                  want_fullwave=True, n_freq=21, max_timesteps=8000)
        elapsed = _ms(t0)
        fw = r.get("fullwave") or {}
        print(f"  mcp analyze fullwave: ok={r.get('ok')} source={r.get('source')} "
              f"fw_ok={fw.get('ok')} skipped={fw.get('skipped')}  [{elapsed:.1f} ms]")
        self.assertTrue(r["ok"], r)
        self.assertIn("analytic", r)
        self.assertIn("fullwave", r)
        # Analytic always present.
        self.assertTrue(r["analytic"]["ok"])
        # Fullwave is either ok or skipped/reason — never a traceback.
        blob = json.dumps(r)
        self.assertNotIn("Traceback", blob)
        if fw.get("skipped"):
            self.assertIn("reason", fw)
            self.assertTrue(r.get("fullwave_skipped") or fw.get("skipped"))
        elif fw.get("ok"):
            self.assertIn("s11_min_db", fw)

    def test_analyze_monkeypatch_skip_envelope(self):
        t0 = time.perf_counter()
        from services.sim.em import antenna_bridge as bridge  # em lives services-side
        real = bridge.openems_available

        def _gone():
            return {"installed": False, "version": None, "detail": "mcp-test absent"}

        bridge.openems_available = _gone  # type: ignore[assignment]
        try:
            r = _call(self.tools, "ecad_antenna_analyze",
                      kind="patch", freq_hz=2.4e9, want_fullwave=True)
        finally:
            bridge.openems_available = real  # type: ignore[assignment]
        print(f"  mcp analyze monkeypatch skip: "
              f"fw={r.get('fullwave')}  [{_ms(t0):.2f} ms]")
        self.assertTrue(r["ok"], r)
        fw = r["fullwave"]
        self.assertTrue(fw.get("skipped") or r.get("fullwave_skipped"))
        self.assertIn("hint", r)
        self.assertNotIn("Traceback", json.dumps(r))


if __name__ == "__main__":
    unittest.main()
