"""MCP EM tools — eigenmode accuracy, driven parity, openEMS skip, compact payloads.

Run:  .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_em_tools.py -q
"""
from __future__ import annotations

import json
import math
import unittest

from services.sim.em.worker_rpc import WORKER_METHODS
from services.mcp import em_tools


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
    em_tools.register_em_tools(mcp)
    return mcp.tools


def _call(tools: dict, name: str, **kwargs) -> dict:
    raw = tools[name](**kwargs)
    assert isinstance(raw, str), f"{name} must return a JSON string"
    return json.loads(raw)


class TestEmMcpTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _load()

    def test_registered(self):
        for name in ("em_status", "em_eigenmode", "em_driven", "em_timedomain"):
            self.assertIn(name, self.tools)

    def test_em_status(self):
        r = _call(self.tools, "em_status")
        self.assertTrue(r["ok"])
        self.assertIn("solvers", r)
        self.assertIn("defaults", r)
        self.assertIn("hint", r)
        self.assertIn("next_actions", r)
        self.assertIn("warnings", r)
        names = {s["name"] for s in r["solvers"]}
        self.assertIn("builtin-fdfd", names)
        self.assertIn("analytic-uniform", names)
        self.assertIn("openems", names)

    def test_em_eigenmode_wr90_te10_error_lt_1pct(self):
        r = _call(self.tools, "em_eigenmode", standard="WR-90", n_modes=3)
        self.assertTrue(r["ok"], r)
        self.assertIn("modes", r)
        self.assertEqual(r["modes"][0]["name"], "TE10")
        fc = r["modes"][0]["fc_ghz"]
        # Analytic WR-90 TE10 = c/(2a) with a=22.86 mm → 6.5571 GHz.
        err = abs(fc - 6.5571) / 6.5571 * 100.0
        self.assertLess(err, 1.0, f"TE10 fc={fc} GHz err={err:.3f}% (need <1%)")
        if "error_vs_analytic_pct" in r:
            self.assertLess(r["error_vs_analytic_pct"], 1.0)
        # Default: no raw field mesh.
        for md in r["modes"]:
            for banned in ("field", "fields", "preview", "Ez", "Hz", "psi"):
                self.assertNotIn(banned, md)
        self.assertIn("hint", r)
        self.assertIn("next_actions", r)

    def test_em_driven_analytic_matches_worker(self):
        params = {"standard": "WR90", "length_mm": 50, "n_freq": 21,
                  "solver": "analytic-uniform"}
        worker = WORKER_METHODS["em_driven"](params, None)
        self.assertTrue(worker["ok"], worker)
        r = _call(self.tools, "em_driven", standard="WR90", length_mm=50,
                  n_freq=21, solver="analytic-uniform")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["solver"], "analytic-uniform")
        # Compact: no full freq arrays by default.
        self.assertNotIn("freq_ghz", r)
        self.assertNotIn("s21_db", r)
        # Mid-band S21 matches the worker path.
        mid = len(worker["s21_db"]) // 2
        worker_mid = worker["s21_db"][mid]
        self.assertIn("s21_db_mid", r)
        self.assertAlmostEqual(r["s21_db_mid"], worker_mid, places=4)
        # Above cutoff, matched section → |S21| ≈ 0 dB.
        self.assertGreater(r["s21_db_max"], -1.0)

    def test_em_driven_detail_includes_arrays(self):
        r = _call(self.tools, "em_driven", standard="WR-90", length_mm=50,
                  n_freq=11, solver="analytic-uniform", detail=True)
        self.assertTrue(r["ok"], r)
        self.assertIn("freq_ghz", r)
        self.assertEqual(len(r["freq_ghz"]), 11)
        self.assertIn("s21_db", r)

    def test_openems_absent_honest_skip(self):
        r = _call(self.tools, "em_driven", standard="WR-90", length_mm=50,
                  solver="openems")
        # Either openEMS is installed (ok=True) or we get a clean skip envelope.
        if r.get("ok"):
            self.assertEqual(r.get("solver"), "openems")
            return
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("skipped") or "openems" in str(r.get("error", "")).lower()
                        or r.get("skip_reason") == "openems_not_installed", r)
        self.assertIn("hint", r)
        self.assertIn("next_actions", r)
        # Never a Python traceback string.
        self.assertNotIn("Traceback", json.dumps(r))
        self.assertNotIn("traceback", str(r.get("error", "")).lower())

    def test_em_timedomain_compact(self):
        r = _call(self.tools, "em_timedomain", standard="WR90", length_mm=80,
                  solver="analytic-uniform")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("t_ns", r)  # waveforms behind detail
        self.assertNotIn("ports", r)
        self.assertIn("n_samples", r)
        self.assertIn("hint", r)

    def test_default_no_raw_field_mesh(self):
        r = _call(self.tools, "em_eigenmode", standard="WR-90", n_modes=2,
                  fields=False, detail=False)
        self.assertTrue(r["ok"], r)
        blob = json.dumps(r)
        # Field preview grids are 2-D arrays of floats — should not appear.
        self.assertNotIn('"field"', blob)
        self.assertNotIn('"preview"', blob)


if __name__ == "__main__":
    unittest.main()
