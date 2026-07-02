"""Tests for requirements CRUD MCP tools (req_create, req_update, req_status, req_report).

Covers the MCP tool layer (req_tools.py) via standalone .req.json persistence so
these tests run without a live C++ engine.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# Patch the FastMCP registration so we can test tools directly.
from unittest.mock import MagicMock, patch


def _make_mcp():
    """Return a mock FastMCP that records registered tool callables."""
    tools: dict = {}
    mcp = MagicMock()

    def _tool_decorator():
        def decorator(fn):
            tools[fn.__name__] = fn
            return fn
        return decorator

    mcp.tool = _tool_decorator
    mcp._tools = tools
    return mcp


def _register():
    from mcp_server.req_tools import register_req_tools  # noqa: PLC0415
    mcp = _make_mcp()
    register_req_tools(mcp)
    return mcp._tools


@pytest.fixture()
def tools():
    return _register()


@pytest.fixture()
def req_file(tmp_path):
    return str(tmp_path / "test.req.json")


# ---------------------------------------------------------------------------
# req_create
# ---------------------------------------------------------------------------

class TestReqCreate:
    def test_creates_new_doc(self, tools, req_file):
        result = json.loads(tools["req_create"](title="My Brief", req_path=req_file))
        assert result["ok"] is True
        assert "req_id" in result
        assert Path(req_file).exists()

    def test_file_is_valid_schema(self, tools, req_file):
        tools["req_create"](title="Test", req_path=req_file)
        data = json.loads(Path(req_file).read_text())
        assert data["schema"] == "aicad.req/1"
        assert data["title"] == "Test"
        assert data["revision"] == 1

    def test_audit_trail_initialized(self, tools, req_file):
        tools["req_create"](title="Brief", req_path=req_file)
        data = json.loads(Path(req_file).read_text())
        assert len(data["audit"]) >= 1
        assert data["audit"][0]["who"] == "agent"


# ---------------------------------------------------------------------------
# req_update — add_need
# ---------------------------------------------------------------------------

class TestReqUpdateAddNeed:
    def test_add_need(self, tools, req_file):
        tools["req_create"](title="T", req_path=req_file)
        result = json.loads(tools["req_update"](
            action="add_need",
            need_text="The bracket must fit the bearing housing.",
            req_path=req_file,
        ))
        assert result["ok"] is True
        data = json.loads(Path(req_file).read_text())
        assert len(data["stakeholder_needs"]) == 1
        assert data["stakeholder_needs"][0]["id"] == "N1"

    def test_add_need_no_text_fails(self, tools, req_file):
        tools["req_create"](title="T", req_path=req_file)
        result = json.loads(tools["req_update"](action="add_need", req_path=req_file))
        assert result["ok"] is False
        assert "need_text" in result["error"]

    def test_add_need_missing_doc_fails(self, tools, tmp_path):
        missing = str(tmp_path / "missing.req.json")
        result = json.loads(tools["req_update"](action="add_need", need_text="X", req_path=missing))
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# req_update — add_requirement
# ---------------------------------------------------------------------------

class TestReqUpdateAddRequirement:
    def _setup(self, tools, req_file):
        tools["req_create"](title="T", req_path=req_file)
        tools["req_update"](
            action="add_need",
            need_text="Bracket accepts a standard press-fit bearing.",
            req_path=req_file,
        )

    def test_add_dimensional_requirement(self, tools, req_file):
        self._setup(tools, req_file)
        result = json.loads(tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="dimensional",
            text="Bore accepts a standard bearing with press fit, no gap.",
            verify_by=[{"verifier": "measure_between"}, {"verifier": "fit_report"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm",
                      "tol": {"plus": 0.0, "minus": 0.013}},
            req_path=req_file,
        ))
        assert result["ok"] is True
        data = json.loads(Path(req_file).read_text())
        assert len(data["requirements"]) == 1
        r = data["requirements"][0]
        assert r["id"] == "R1"
        assert r["status"] == "proposed"
        assert r["quantity"]["param"] == "bore_dia"
        assert len(r["verify_by"]) == 2

    def test_text_with_numbers_rejected(self, tools, req_file):
        self._setup(tools, req_file)
        result = json.loads(tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="dimensional",
            text="Bore must be 22 mm.",  # number in text — should fail
            verify_by=[{"verifier": "measure_between"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm"},
            req_path=req_file,
        ))
        assert result["ok"] is False
        assert "prose" in result["error"].lower() or "numeral" in result["error"].lower() or "quantity" in result["error"].lower()

    def test_invalid_verifier_rejected(self, tools, req_file):
        self._setup(tools, req_file)
        result = json.loads(tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="functional",
            text="Part must meet load requirements under service conditions.",
            verify_by=[{"verifier": "nonexistent_tool"}],
            req_path=req_file,
        ))
        assert result["ok"] is False
        assert "verifier" in result["error"].lower() or "nonexistent" in result["error"].lower()

    def test_missing_verify_by_rejected(self, tools, req_file):
        self._setup(tools, req_file)
        result = json.loads(tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="functional",
            text="Part meets functional criteria.",
            verify_by=[],  # empty — must fail
            req_path=req_file,
        ))
        assert result["ok"] is False

    def test_qualitative_requirement_no_quantity(self, tools, req_file):
        self._setup(tools, req_file)
        result = json.loads(tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="standard",
            text="Part must comply with applicable material standards.",
            verify_by=[{"verifier": "dfm_check"}],
            # no quantity — qualitative requirement
            req_path=req_file,
        ))
        assert result["ok"] is True
        data = json.loads(Path(req_file).read_text())
        assert data["requirements"][0]["quantity"] is None


# ---------------------------------------------------------------------------
# req_status
# ---------------------------------------------------------------------------

class TestReqStatus:
    def _setup_with_req(self, tools, req_file):
        tools["req_create"](title="T", req_path=req_file)
        tools["req_update"](action="add_need", need_text="Need.", req_path=req_file)
        tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="dimensional",
            text="Bore meets press-fit specification.",
            verify_by=[{"verifier": "measure_between"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm"},
            req_path=req_file,
        )

    def test_user_can_confirm(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        result = json.loads(tools["req_status"](
            requirement_id="R1",
            new_status="user_confirmed",
            who="user",
            req_path=req_file,
        ))
        assert result["ok"] is True
        assert result["status"] == "user_confirmed"

    def test_agent_cannot_confirm(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        result = json.loads(tools["req_status"](
            requirement_id="R1",
            new_status="user_confirmed",
            who="agent",
            req_path=req_file,
        ))
        assert result["ok"] is False
        assert "user" in result["error"].lower() or "agent" in result["error"].lower()

    def test_agent_can_verify_after_user_confirmed(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        tools["req_status"](requirement_id="R1", new_status="user_confirmed", who="user", req_path=req_file)
        evidence = [{"verifier": "measure_between", "result": {"measured_mm": 22.0, "within_tol": True}, "ts": "2026-07-02T12:00:00Z"}]
        result = json.loads(tools["req_status"](
            requirement_id="R1",
            new_status="verified",
            who="agent",
            evidence=evidence,
            req_path=req_file,
        ))
        assert result["ok"] is True
        assert result["status"] == "verified"
        assert result["evidence_count"] == 1

    def test_agent_can_fail(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        tools["req_status"](requirement_id="R1", new_status="user_confirmed", who="user", req_path=req_file)
        result = json.loads(tools["req_status"](
            requirement_id="R1",
            new_status="failed",
            who="agent",
            req_path=req_file,
        ))
        assert result["ok"] is True
        assert result["status"] == "failed"

    def test_agent_cannot_waive(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        result = json.loads(tools["req_status"](
            requirement_id="R1",
            new_status="waived",
            who="agent",
            req_path=req_file,
        ))
        assert result["ok"] is False

    def test_unknown_req_id_fails(self, tools, req_file):
        self._setup_with_req(tools, req_file)
        result = json.loads(tools["req_status"](
            requirement_id="R99",
            new_status="verified",
            who="agent",
            req_path=req_file,
        ))
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# req_report
# ---------------------------------------------------------------------------

class TestReqReport:
    def _setup(self, tools, req_file):
        tools["req_create"](title="Bracket Brief", req_path=req_file)
        tools["req_update"](action="add_need", need_text="Bore fits bearing.", req_path=req_file)
        tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="dimensional",
            text="Bore accepts standard press-fit bearing.",
            verify_by=[{"verifier": "measure_between"}, {"verifier": "fit_report"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm",
                      "tol": {"plus": 0.0, "minus": 0.013}},
            req_path=req_file,
        )

    def test_markdown_report_contains_title(self, tools, req_file):
        self._setup(tools, req_file)
        md = tools["req_report"](req_path=req_file, format="markdown")
        assert "Bracket Brief" in md
        assert "aicad.req/1" in md

    def test_markdown_has_requirements_table(self, tools, req_file):
        self._setup(tools, req_file)
        md = tools["req_report"](req_path=req_file, format="markdown")
        assert "R1" in md
        assert "bore_dia" in md
        assert "measure_between" in md

    def test_json_report_roundtrips(self, tools, req_file):
        self._setup(tools, req_file)
        raw = tools["req_report"](req_path=req_file, format="json")
        data = json.loads(raw)
        assert data["schema"] == "aicad.req/1"
        assert len(data["requirements"]) == 1

    def test_report_missing_doc(self, tools, tmp_path):
        result = tools["req_report"](req_path=str(tmp_path / "missing.req.json"))
        # returns error JSON string
        parsed = json.loads(result)
        assert parsed["ok"] is False


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_full_roundtrip(self, tools, req_file):
        """Create → add need → add req → confirm → verify → load back → markdown."""
        tools["req_create"](title="RT Brief", req_path=req_file)
        tools["req_update"](action="add_need", need_text="Part must fit housing.", req_path=req_file)
        tools["req_update"](
            action="add_requirement",
            need_id="N1",
            kind="dimensional",
            text="Bore fits housing precisely.",
            verify_by=[{"verifier": "measure_between"}],
            quantity={"param": "bore_dia", "op": "==", "value": 25.0, "unit": "mm"},
            req_path=req_file,
        )
        tools["req_status"](requirement_id="R1", new_status="user_confirmed", who="user", req_path=req_file)
        tools["req_status"](
            requirement_id="R1",
            new_status="verified",
            who="agent",
            evidence=[{"verifier": "measure_between", "result": {"measured_mm": 25.0, "within_tol": True}, "ts": "2026-07-02T12:00:00Z"}],
            req_path=req_file,
        )

        data = json.loads(Path(req_file).read_text())
        assert data["requirements"][0]["status"] == "verified"
        assert len(data["requirements"][0]["trace"]["evidence"]) == 1
        assert len(data["audit"]) >= 4  # create + add_need + add_req + confirm + verify

        # Markdown projection must contain verified status
        md = tools["req_report"](req_path=req_file, format="markdown")
        assert "verified" in md
        assert "bore_dia" in md
