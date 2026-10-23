"""test_layout_plan.py — end-to-end demo test for plan-first layout capture.

Scenario: plan a 2-part assembly (envelope + 1 interface + 2 critical dims)
  → model it (mocked) → layout_coverage reports the unverified dims
  → verify them (mock evidence) → gate goes green.

All engine calls are mocked via a doc-meta in-memory store so this test runs
without a live C++ engine (same pattern as test_req_tools.py).

Coverage:
  - layout_plan() creates/updates aicad.req/1 requirements correctly
  - _gap_layout_coverage() WARN/FAIL on unverified dims
  - _gap_layout_coverage() passes after req_status sets verified+evidence
  - design_review integration: layout_coverage surfaces as FAIL then PASS
  - layout_plan_report() returns human-readable coverage table
"""
from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# In-memory doc-meta store for mocking core_bridge
# ---------------------------------------------------------------------------
_META_STORE: dict[str, dict[str, Any]] = {}


def _mock_cb_call(op: str, args: dict) -> Any:
    doc_id = args.get("doc_id", "__default__")
    if op == "doc_get_meta":
        key = args["key"]
        val = _META_STORE.get(doc_id, {}).get(key)
        return {"value": val}
    if op == "doc_set_meta":
        key = args["key"]
        val = args["value"]
        _META_STORE.setdefault(doc_id, {})[key] = val
        return {"ok": True}
    raise ValueError(f"unexpected op: {op!r}")


@pytest.fixture(autouse=True)
def fresh_store():
    """Reset meta store between tests."""
    _META_STORE.clear()
    yield
    _META_STORE.clear()


@pytest.fixture()
def mock_cb(monkeypatch):
    """Patch core_bridge.call everywhere layout_plan_tools and battery reach it.

    Both modules use a factory function (_core_bridge / _layout_coverage_cb) that
    returns the bridge object, so we patch those factories to return the same mock.
    """
    cb = MagicMock()
    cb.call.side_effect = _mock_cb_call

    # Patch layout_plan_tools factory
    monkeypatch.setattr(
        "services.mcp.layout_plan_tools._core_bridge",
        lambda: cb,
    )
    # Patch battery factory (used by _gap_layout_coverage)
    monkeypatch.setattr(
        "services.verify.battery._layout_coverage_cb",
        lambda: cb,
    )
    with patch("services.verify.battery._cur_doc", return_value="doc-test"):
        yield cb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _do_layout_plan(doc_id: str, cb) -> dict:
    """Run layout_plan() via the MCP tool and return parsed result."""
    from mcp.server.fastmcp import FastMCP
    from services.mcp.layout_plan_tools import register_layout_plan_tools

    mcp = FastMCP("test")
    register_layout_plan_tools(mcp)

    # Extract the tool function directly
    tool = mcp._tool_manager._tools["layout_plan"]
    raw = tool.fn(
        doc_id=doc_id,
        envelope_x_mm=120.0,
        envelope_y_mm=80.0,
        envelope_z_mm=45.0,
        critical_dims=[
            {
                "param": "bore_dia",
                "value": 22.0,
                "unit": "mm",
                "tol_plus": 0.0,
                "tol_minus": 0.013,
                "why": "mates to shaft bearing — fit-critical",
                "verify_by": "measure_between",
            },
            {
                "param": "plate_thickness",
                "value": 6.0,
                "unit": "mm",
                "tol_plus": 0.1,
                "tol_minus": 0.1,
                "why": "structural load path — min thickness for adequate factor of safety",
                "verify_by": "run_fea_static",
            },
        ],
        interfaces=[
            {
                "id": "IF1",
                "desc": "Bracket to plate via 4-hole M5 bolt pattern",
                "verify_by": "check_interference",
            }
        ],
        note="Test assembly: bracket + plate with bore and bolt pattern",
    )
    return json.loads(raw)


def _gap_layout_coverage_direct(doc_id: str) -> dict:
    """Call _gap_layout_coverage directly (not through MCP)."""
    from services.verify import battery
    raw = battery._gap_layout_coverage(doc_id=doc_id)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLayoutPlanCapture:
    """layout_plan() correctly creates aicad.req/1 requirements."""

    def test_layout_plan_creates_req_doc(self, mock_cb):
        doc_id = "doc-test"
        result = _do_layout_plan(doc_id, mock_cb)

        assert result["ok"] is True
        assert result["created_req_doc"] is True
        # 2 critical dims + 1 interface
        assert len(result["critical_req_ids"]) == 2
        assert len(result["interface_req_ids"]) == 1
        assert result["envelope"] == {"x_mm": 120.0, "y_mm": 80.0, "z_mm": 45.0}

    def test_layout_plan_stores_req_doc(self, mock_cb):
        doc_id = "doc-test"
        _do_layout_plan(doc_id, mock_cb)

        # Verify the aicad.req/1 doc was persisted
        req_raw = _META_STORE.get(doc_id, {}).get("requirements")
        assert req_raw is not None
        assert req_raw["schema"] == "aicad.req/1"
        reqs = req_raw["requirements"]
        assert len(reqs) == 3  # 2 critical dims + 1 interface

        kinds = [r["kind"] for r in reqs]
        assert "dimensional" in kinds
        assert "interface" in kinds

    def test_layout_plan_stores_envelope_meta(self, mock_cb):
        doc_id = "doc-test"
        _do_layout_plan(doc_id, mock_cb)

        plan_raw = _META_STORE.get(doc_id, {}).get("layout_plan")
        assert plan_raw is not None
        assert plan_raw["schema"] == "aicad.layout/1"
        assert plan_raw["envelope"]["x_mm"] == 120.0
        assert len(plan_raw["critical_req_ids"]) == 2
        assert len(plan_raw["interface_req_ids"]) == 1

    def test_layout_plan_idempotent_adds(self, mock_cb):
        """Calling layout_plan twice adds to, not replaces, the existing plan."""
        doc_id = "doc-test"
        r1 = _do_layout_plan(doc_id, mock_cb)
        assert len(r1["critical_req_ids"]) == 2

        # Second call with one more dim
        from mcp.server.fastmcp import FastMCP
        from services.mcp.layout_plan_tools import register_layout_plan_tools
        mcp2 = FastMCP("test2")
        register_layout_plan_tools(mcp2)
        tool = mcp2._tool_manager._tools["layout_plan"]
        raw2 = tool.fn(
            doc_id=doc_id,
            critical_dims=[{
                "param": "flange_od",
                "value": 50.0,
                "unit": "mm",
                "tol_plus": 0.05,
                "tol_minus": 0.05,
                "why": "clearance to housing",
            }],
        )
        r2 = json.loads(raw2)
        assert r2["ok"] is True
        # Should now have 3 critical dims total
        assert len(r2["critical_req_ids"]) == 3


class TestLayoutCoverageGate:
    """_gap_layout_coverage correctly WARN/FAILs and then passes."""

    def test_no_layout_plan_passes(self, mock_cb):
        """When no layout_plan meta exists, gate passes with informational note."""
        result = _gap_layout_coverage_direct("doc-test")
        assert result["ok"] is True
        assert result["total"] == 0
        assert "no layout plan" in result["note"]

    def test_unverified_dims_fail(self, mock_cb):
        """After layout_plan but before verification, gate fails."""
        doc_id = "doc-test"
        _do_layout_plan(doc_id, mock_cb)

        result = _gap_layout_coverage_direct(doc_id)
        assert result["ok"] is False
        assert result["covered"] == 0
        assert result["total"] == 3  # 2 dims + 1 interface
        assert len(result["uncovered"]) == 3
        # All should be status=proposed
        for u in result["uncovered"]:
            assert u["status"] == "proposed"

    def test_verified_dim_reduces_uncovered(self, mock_cb):
        """Verifying one dim reduces uncovered count by one."""
        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        first_req_id = plan_result["critical_req_ids"][0]

        # Simulate verifier setting status=verified + evidence
        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] == first_req_id:
                r["status"] = "verified"
                r["trace"]["evidence"] = [{
                    "verifier": "measure_between",
                    "result": {"ok": True, "distance_mm": 22.0},
                    "ts": "2026-07-04T00:00:00Z",
                }]
        _META_STORE[doc_id]["requirements"] = req_raw

        result = _gap_layout_coverage_direct(doc_id)
        assert result["ok"] is False
        assert result["covered"] == 1
        assert result["total"] == 3
        assert len(result["uncovered"]) == 2

    def test_all_verified_gate_passes(self, mock_cb):
        """When all critical dims and interfaces are verified, gate passes."""
        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        all_ids = plan_result["critical_req_ids"] + plan_result["interface_req_ids"]

        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] in all_ids:
                r["status"] = "verified"
                r["trace"]["evidence"] = [{
                    "verifier": "measure_between",
                    "result": {"ok": True},
                    "ts": "2026-07-04T00:00:00Z",
                }]
        _META_STORE[doc_id]["requirements"] = req_raw

        result = _gap_layout_coverage_direct(doc_id)
        assert result["ok"] is True
        assert result["covered"] == 3
        assert result["total"] == 3
        assert result["uncovered"] == []

    def test_user_confirmed_counts_as_covered(self, mock_cb):
        """user_confirmed status (no evidence needed) counts as covered."""
        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        req_id = plan_result["critical_req_ids"][0]

        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] == req_id:
                r["status"] = "user_confirmed"
                # no evidence — user signed off directly
        _META_STORE[doc_id]["requirements"] = req_raw

        result = _gap_layout_coverage_direct(doc_id)
        # 1 covered (user_confirmed), 2 still proposed
        assert result["covered"] == 1

    def test_verified_without_evidence_is_inconsistent(self, mock_cb):
        """status=verified but empty evidence[] is caught as inconsistent (same as req_join)."""
        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        req_id = plan_result["critical_req_ids"][0]

        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] == req_id:
                r["status"] = "verified"
                r["trace"]["evidence"] = []  # inconsistent
        _META_STORE[doc_id]["requirements"] = req_raw

        result = _gap_layout_coverage_direct(doc_id)
        assert result["ok"] is False
        inconsistent = [u for u in result["uncovered"] if "inconsistent" in u.get("reason", "")]
        assert len(inconsistent) == 1
        assert inconsistent[0]["id"] == req_id

    def test_waived_counts_as_covered(self, mock_cb):
        """waived status exempts a requirement from the gate."""
        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        req_id = plan_result["interface_req_ids"][0]

        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] == req_id:
                r["status"] = "waived"
        _META_STORE[doc_id]["requirements"] = req_raw

        result = _gap_layout_coverage_direct(doc_id)
        # interface waived → covered; 2 dims still proposed
        assert result["covered"] == 1
        assert result["total"] == 3


class TestDesignReviewIntegration:
    """layout_coverage surfaces correctly through design_review."""

    def test_design_review_layout_coverage_fail(self, mock_cb):
        """design_review shows layout_coverage FAIL when dims are unverified."""
        from services.mcp.design_review_tools import _run_design_review
        from services.verify.battery import _gap_layout_coverage as gap_fn

        doc_id = "doc-test"
        _do_layout_plan(doc_id, mock_cb)

        # Override all other checks so only layout_coverage runs cleanly
        override_pass = lambda **kw: json.dumps({"ok": True})

        verdict = _run_design_review(
            doc_id=doc_id,
            checks=["layout_coverage"],
            tool_fn_overrides={},
        )
        # layout_coverage is a GAP check — it hits core_bridge directly via mock_cb
        # The verdict should contain FAIL for layout_coverage
        assert "layout_coverage" in verdict.lower() or "Layout coverage" in verdict
        assert "FAIL" in verdict

    def test_design_review_layout_coverage_pass(self, mock_cb):
        """design_review shows layout_coverage PASS after all dims verified."""
        from services.mcp.design_review_tools import _run_design_review

        doc_id = "doc-test"
        plan_result = _do_layout_plan(doc_id, mock_cb)
        all_ids = plan_result["critical_req_ids"] + plan_result["interface_req_ids"]

        req_raw = _META_STORE[doc_id]["requirements"]
        for r in req_raw["requirements"]:
            if r["id"] in all_ids:
                r["status"] = "verified"
                r["trace"]["evidence"] = [{"verifier": "measure_between",
                                            "result": {"ok": True},
                                            "ts": "2026-07-04T00:00:00Z"}]
        _META_STORE[doc_id]["requirements"] = req_raw

        verdict = _run_design_review(
            doc_id=doc_id,
            checks=["layout_coverage"],
        )
        assert "PASS" in verdict
        assert "FAIL" not in verdict


class TestLayoutPlanReport:
    """layout_plan_report() returns human-readable coverage."""

    def test_no_plan_returns_guidance(self, mock_cb):
        from mcp.server.fastmcp import FastMCP
        from services.mcp.layout_plan_tools import register_layout_plan_tools
        mcp = FastMCP("test")
        register_layout_plan_tools(mcp)
        tool = mcp._tool_manager._tools["layout_plan_report"]
        result = tool.fn(doc_id="doc-test")
        assert "No layout plan found" in result

    def test_report_shows_unverified(self, mock_cb):
        from mcp.server.fastmcp import FastMCP
        from services.mcp.layout_plan_tools import register_layout_plan_tools
        doc_id = "doc-test"
        _do_layout_plan(doc_id, mock_cb)

        mcp = FastMCP("test")
        register_layout_plan_tools(mcp)
        tool = mcp._tool_manager._tools["layout_plan_report"]
        result = tool.fn(doc_id=doc_id)

        assert "Layout Plan Report" in result
        assert "bore_dia" in result
        assert "plate_thickness" in result
        assert "Bracket to plate" in result
        assert "Coverage: 0/3 verified" in result
        assert "WARN" in result
