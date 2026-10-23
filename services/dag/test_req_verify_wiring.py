"""End-to-end test: requirement with verify_by: run_fea_static gets auto-verified
when the DAG orchestrator runs a verify node with check_type='requirement'.

The binding demo.
No C++ engine needed: the verifier tool is injected as a mock via tool_fn_override.
The req doc is persisted as a .req.json file (standalone, engine-free).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def req_file(tmp_path) -> str:
    return str(tmp_path / "demo.req.json")


def _build_req_doc(req_file: str) -> str:
    """Create a requirements doc with R1 verify_by: run_fea_static. Returns req_id."""
    from mcp.server.fastmcp import FastMCP
    from services.mcp.req_tools import register_req_tools

    mcp = FastMCP("test-req")
    register_req_tools(mcp)

    # We need the raw tool functions
    tools = {name: t.fn for name, t in mcp._tool_manager._tools.items()}

    result = json.loads(tools["req_create"](title="FEA Demo Brief", req_path=req_file))
    assert result["ok"]

    tools["req_update"](
        action="add_need",
        need_text="The bracket must withstand service loads without yielding.",
        req_path=req_file,
    )
    tools["req_update"](
        action="add_requirement",
        need_id="N1",
        kind="functional",
        text="The bracket must survive loading conditions without permanent deformation.",
        verify_by=[{"verifier": "run_fea_static"}],
        req_path=req_file,
    )
    # User confirms
    tools["req_status"](
        requirement_id="R1",
        new_status="user_confirmed",
        who="user",
        req_path=req_file,
    )
    return result["req_id"]


def _fea_pass_mock(**kwargs) -> str:
    """Mock run_fea_static that returns a passing FEA result."""
    return json.dumps({
        "ok": True,
        "min_fos": 2.5,
        "max_von_mises_mpa": 80.0,
        "summary": "FoS=2.5 (mock)",
    })


def _fea_fail_mock(**kwargs) -> str:
    """Mock run_fea_static that returns a failing FEA result."""
    return json.dumps({
        "ok": False,
        "min_fos": 0.8,
        "max_von_mises_mpa": 350.0,
        "summary": "FoS=0.8 below threshold (mock)",
    })


# ---------------------------------------------------------------------------
# DAG verify node plumbing helpers
# ---------------------------------------------------------------------------

def _make_node(node_id: str = "v1") -> dict:
    return {
        "id": node_id,
        "name": "verify-fea",
        "type": "verify",
        "inputs": {},
        "end_condition": None,
    }


def _make_context(req_id: str, req_file: str, doc_id: str = "doc-abc") -> dict:
    return {
        "node_inputs": {
            "check_type": "requirement",
            "req_id": req_id,
            "req_path": req_file,
            "doc_id": doc_id,
        },
        "retry_remaining": 2,
        "end_condition": None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReqVerifyWiring:
    def test_passing_verifier_flips_status_to_verified(self, req_file):
        """Core DoD: run_fea_static PASS → R1 status becomes 'verified', evidence written."""
        from services.dag.verify import run as dag_run

        _build_req_doc(req_file)
        node = _make_node()
        ctx = _make_context("R1", req_file)

        verdict, output = dag_run(
            node, ctx,
            tool_fn_override=_fea_pass_mock,
        )

        assert verdict == "done", f"Expected 'done', got {verdict!r}: {output.get('summary')}"
        assert output["data"]["passed"] is True

        # Verify the req doc was updated
        data = json.loads(Path(req_file).read_text())
        req = data["requirements"][0]
        assert req["status"] == "verified", f"Expected 'verified', got {req['status']!r}"
        assert len(req["trace"]["evidence"]) >= 1

        # Evidence carries source="battery"
        ev = req["trace"]["evidence"][0]
        assert ev.get("source") == "battery"
        assert ev.get("verifier") == "run_fea_static"

    def test_failing_verifier_flips_status_to_failed(self, req_file):
        """Failing verifier → R1 status becomes 'failed', evidence still written."""
        from services.dag.verify import run as dag_run

        _build_req_doc(req_file)
        node = _make_node()
        ctx = _make_context("R1", req_file)
        ctx["node_inputs"]["retry_remaining"] = 0
        ctx["retry_remaining"] = 0

        verdict, output = dag_run(
            node, ctx,
            tool_fn_override=_fea_fail_mock,
        )

        # Budget=0 → escalate on fail
        assert verdict in ("escalate", "retry")

        data = json.loads(Path(req_file).read_text())
        req = data["requirements"][0]
        assert req["status"] == "failed"
        assert len(req["trace"]["evidence"]) >= 1
        ev = req["trace"]["evidence"][0]
        assert ev.get("source") == "battery"

    def test_req_report_shows_battery_evidence_distinct(self, req_file):
        """req_report must distinguish battery evidence from hand-entered."""
        from services.dag.verify import run as dag_run
        from mcp.server.fastmcp import FastMCP
        from services.mcp.req_tools import register_req_tools

        _build_req_doc(req_file)
        node = _make_node()
        ctx = _make_context("R1", req_file)

        dag_run(node, ctx, tool_fn_override=_fea_pass_mock)

        mcp = FastMCP("test-req")
        register_req_tools(mcp)
        tools = {name: t.fn for name, t in mcp._tool_manager._tools.items()}

        report = tools["req_report"](req_path=req_file, format="markdown")
        assert "Battery-populated evidence" in report, (
            "req_report must show a 'Battery-populated evidence' section when "
            "battery evidence is present."
        )
        assert "run_fea_static" in report

    def test_missing_req_id_escalates(self, req_file):
        """Missing req_id in node_inputs → ESCALATE (fail-closed)."""
        from services.dag.verify import run as dag_run

        node = _make_node()
        ctx = {
            "node_inputs": {"check_type": "requirement", "req_path": req_file},
            "retry_remaining": 1,
            "end_condition": None,
        }
        verdict, output = dag_run(node, ctx)
        assert verdict == "escalate"
        assert "req_id" in output["summary"].lower()

    def test_unknown_req_id_escalates(self, req_file):
        """req_id not found in doc → ESCALATE."""
        from services.dag.verify import run as dag_run

        _build_req_doc(req_file)
        node = _make_node()
        ctx = _make_context("R99", req_file)
        verdict, output = dag_run(node, ctx, tool_fn_override=_fea_pass_mock)
        assert verdict == "escalate"
        assert "R99" in output["summary"]

    def test_unsupported_only_verifiers_escalate(self, req_file, tmp_path):
        """Requirement with only unsupported verifiers → ESCALATE with reason."""
        from mcp.server.fastmcp import FastMCP
        from services.mcp.req_tools import register_req_tools
        from services.dag.verify import run as dag_run

        req_file2 = str(tmp_path / "unsupported.req.json")
        mcp = FastMCP("test-req")
        register_req_tools(mcp)
        tools = {name: t.fn for name, t in mcp._tool_manager._tools.items()}

        tools["req_create"](title="Unsupported brief", req_path=req_file2)
        tools["req_update"](action="add_need", need_text="A need.", req_path=req_file2)
        tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="standard",
            text="Must comply with DFM standard.",
            verify_by=[{"verifier": "dfm_check"}],  # not in battery.CHECKS
            req_path=req_file2,
        )
        tools["req_status"](requirement_id="R1", new_status="user_confirmed", who="user", req_path=req_file2)

        node = _make_node()
        ctx = _make_context("R1", req_file2)
        verdict, output = dag_run(node, ctx)
        assert verdict == "escalate"
        assert "no_supported_verifiers" in output["data"].get("reason", "")


class TestDesignReviewReqWiring:
    """design_review battery accepts requirement IDs."""

    def test_design_review_verifies_requirement(self, req_file):
        from services.mcp.design_review_tools import _run_design_review

        _build_req_doc(req_file)
        card = _run_design_review(
            doc_id="doc-abc",
            checks=[],  # skip standard checks — engine-free
            req_ids=["R1"],
            req_path=req_file,
            req_tool_fn_override=_fea_pass_mock,
        )
        assert "R1" in card
        assert "VERIFIED" in card

        data = json.loads(Path(req_file).read_text())
        req = data["requirements"][0]
        assert req["status"] == "verified"
        assert req["trace"]["evidence"][0].get("source") == "battery"

    def test_design_review_req_failure_narrated(self, req_file):
        from services.mcp.design_review_tools import _run_design_review

        _build_req_doc(req_file)
        card = _run_design_review(
            doc_id="doc-abc",
            checks=[],
            req_ids=["R1"],
            req_path=req_file,
            req_tool_fn_override=_fea_fail_mock,
        )
        assert "R1" in card
        assert "FAILED" in card

        data = json.loads(Path(req_file).read_text())
        assert data["requirements"][0]["status"] == "failed"
