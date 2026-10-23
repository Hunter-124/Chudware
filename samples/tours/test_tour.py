"""
samples/tours/test_tour.py — one tour script runs end-to-end headless.

Usage:
  CHUDWARE_LIBCHUDWARE=<path>/libchudware.so python -m pytest samples/tours/test_tour.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture(scope="session")
def cb():
    try:
        from services.mcp import core_bridge as _cb
    except ImportError:
        pytest.skip("services.mcp not importable")
    if not _cb.available():
        pytest.skip("libchudware not available — set CHUDWARE_LIBCHUDWARE=<path>")
    return _cb


class TestBracketTour:
    def test_tour_runs_headless(self, cb):
        """The bracket guided tour must complete all steps headless with 0 failures."""
        from samples.tours.bracket_tour import run_bracket_tour
        result = run_bracket_tour(cb)
        assert result["ok"], (
            f"Tour had {result['failed']} failure(s): "
            + str([s for s in result["steps"] if s["status"] != "PASS"])
        )
        assert result["passed"] >= 8, f"Expected >= 8 steps passed, got {result['passed']}"

    def test_tour_cam_step(self, cb):
        """The CAM step in the tour must produce at least 1 operation."""
        from samples.tours.bracket_tour import run_bracket_tour
        result = run_bracket_tour(cb)
        cam_step = next((s for s in result["steps"] if s["step"] == "cam"), None)
        assert cam_step is not None, "CAM step not found in tour results"
        assert cam_step["status"] == "PASS", f"CAM step failed: {cam_step}"
