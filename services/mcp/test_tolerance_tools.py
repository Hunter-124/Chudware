"""MCP envelope tests for stackup_analyze.

Covers tool shape, honest-degrade behavior (assumed dims), mode scoping,
and error envelopes.  Pure-math validation is in engine/tolerance/test_stackup.py.

No sys.modules stubbing here: registration is captured with a local _CaptureMCP
(anything exposing .tool() works — register_tolerance_tools never touches
FastMCP internals), so this file collects cleanly whether or not the real
``mcp`` package is importable, and never poisons other tests' imports.
"""
from __future__ import annotations

import json
import math
import unittest

try:
    from services.mcp.tolerance_tools import register_tolerance_tools
    _IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover — only in venvs missing mcp/cad_tools deps
    register_tolerance_tools = None  # type: ignore[assignment]
    _IMPORT_ERROR = _e


class _CaptureMCP:
    """Minimal stand-in for FastMCP: captures @mcp.tool() registrations."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn
        return dec


DIMS_3 = [
    {"name": "A_slot",   "nominal": 40.0, "tol": 0.050, "direction":  1.0},
    {"name": "B_block1", "nominal": 17.0, "tol": 0.025, "direction": -1.0},
    {"name": "C_block2", "nominal": 20.0, "tol": 0.040, "direction": -1.0},
]


@unittest.skipIf(register_tolerance_tools is None,
                 f"tolerance_tools import failed: {_IMPORT_ERROR}")
class TestStackupAnalyzeTool(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        mcp = _CaptureMCP()
        register_tolerance_tools(mcp)
        cls._tool = staticmethod(mcp.tools["stackup_analyze"])

    def _call(self, dims, mode="both"):
        raw = type(self)._tool(dims=dims, mode=mode)
        return json.loads(raw)

    def test_both_mode_has_wc_and_rss(self):
        d = self._call(DIMS_3)
        self.assertTrue(d["ok"])
        self.assertIn("worst_case", d)
        self.assertIn("rss", d)

    def test_worst_case_mode_omits_rss(self):
        d = self._call(DIMS_3, mode="worst_case")
        self.assertTrue(d["ok"])
        self.assertIn("worst_case", d)
        self.assertNotIn("rss", d)

    def test_rss_mode_omits_wc(self):
        d = self._call(DIMS_3, mode="rss")
        self.assertTrue(d["ok"])
        self.assertIn("rss", d)
        self.assertNotIn("worst_case", d)

    def test_nominal_gap(self):
        d = self._call(DIMS_3)
        self.assertAlmostEqual(d["nominal_gap"], 3.000, places=6)

    def test_wc_total_tol(self):
        d = self._call(DIMS_3)
        self.assertAlmostEqual(d["worst_case"]["total_tol"], 0.115, places=8)

    def test_rss_total_tol(self):
        d = self._call(DIMS_3)
        expected = math.sqrt(0.050**2 + 0.025**2 + 0.040**2)
        self.assertAlmostEqual(d["rss"]["total_tol"], expected, places=8)

    def test_contributors_sorted_descending(self):
        d = self._call(DIMS_3)
        contribs = d["contributors"]
        wcs = [c["contrib_wc"] for c in contribs]
        self.assertEqual(wcs, sorted(wcs, reverse=True))

    def test_first_contributor_is_slot(self):
        d = self._call(DIMS_3)
        self.assertEqual(d["contributors"][0]["name"], "A_slot")

    def test_assumed_dims_reported(self):
        dims_with_assumed = [
            {"name": "guessed_dim", "nominal": 10.0, "tol": 0.05, "assumed": True},
            {"name": "known_dim",   "nominal": 5.0,  "tol": 0.02},
        ]
        d = self._call(dims_with_assumed)
        self.assertIn("guessed_dim", d["assumed_dims"])
        self.assertNotIn("known_dim", d["assumed_dims"])

    def test_disclaimer_present(self):
        d = self._call(DIMS_3)
        self.assertIn("disclaimer", d)
        self.assertIn("Preliminary", d["disclaimer"])

    def test_invalid_mode_returns_error(self):
        d = self._call(DIMS_3, mode="monte_carlo")
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], "invalid_mode")

    def test_empty_dims_returns_error(self):
        d = self._call([])
        self.assertFalse(d["ok"])

    def test_negative_tol_returns_error(self):
        d = self._call([{"name": "d", "nominal": 5.0, "tol": -0.1}])
        self.assertFalse(d["ok"])

    def test_missing_nominal_returns_error(self):
        d = self._call([{"name": "d", "tol": 0.1}])
        self.assertFalse(d["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
