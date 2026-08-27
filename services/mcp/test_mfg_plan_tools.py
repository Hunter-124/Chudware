from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.agent import ledger as ledger_module
from services.agent.ledger import Ledger
from services.mcp.mfg_plan_tools import register_mfg_plan_tools


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.fixture()
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(ledger_module, "cache_root", lambda: tmp_path)
    mcp = _FakeMCP()
    register_mfg_plan_tools(mcp)
    return mcp.tools


def _plan(*, revision: str = "A", provenance: str = "fit_calc(20,H7/g6)") -> dict:
    return {
        "id": "main",
        "revision": revision,
        "source_doc": {"doc_id": "doc-1", "revision": "3"},
        "process": {"id": "cnc_mill"},
        "material": {"key": "aluminum_6061", "condition": "T6"},
        "quantity": 12,
        "stock": {"form": "bar", "size": "25 mm round", "provenance": "user drawing"},
        "machine": {"id": "vmc-3", "provenance": "shop machine registry"},
        "setups": [{
            "id": "S1", "order": 10, "datum": "A", "wcs": "G54",
            "fixturing": "Soft jaws", "geometry_spec_ids": ["G1"],
        }],
        "operations": [{
            "id": "OP10", "order": 10, "name": "Finish bore", "setup_id": "S1",
            "geometry_spec_ids": ["G1"], "geometry_kind": "mating_bore",
            "machine": "vmc-3", "tool": "T12 boring head", "machined": True,
            "helper_inputs": {"nominal_mm": 20.0, "fit": "H7/g6"},
            "parameters": [{"name": "spindle_speed", "value": 4200, "unit": "rpm",
                            "provenance": "feeds_speeds(aluminum_6061,boring)"}],
            "inspection": [{"feature": "Finished bore", "gauge": "Air gauge",
                            "requirement_id": "DIM7"}],
            "provenance": "approved route R1",
        }],
        "critical_dimensions": [{
            "requirement_id": "DIM7", "type": "dimension_tolerance", "name": "bore_d",
            "value": 20.0, "unit": "mm",
            "tolerance": {"type": "deviation", "upper": 0.021, "lower": 0.0},
            "critical": True, "provenance": provenance,
            "derived_from": ["calc:fit_calc:20:H7/g6"],
        }],
        "other_callouts": [{
            "requirement_id": "FIN1", "type": "finish", "ra": 1.6,
            "critical": True, "provenance": "drawing requirement FIN1",
        }],
        "inspection_points": [{"id": "I1", "operation_id": "OP10",
                               "requirement_id": "DIM7", "gauge": "Air gauge"}],
        "hold_points": [{"id": "H1", "after_operation_id": "OP10",
                         "release_authority": "Quality", "record": "FAI"}],
        "work_steps": [{"id": "W1", "operation_id": "OP10", "text": "Finish bore."}],
        "references": [{"id": "ISO286", "citation": "ISO 286-2:2010, clause 4"}],
        "assumptions": [],
        "open_questions": [],
    }


def _upsert(tools: dict[str, object], plan: dict) -> dict:
    return tools["mfg_plan_upsert"]("run-1", plan, "manufacturing_planner")


def test_provenance_less_numeric_critical_row_is_refused(tools):
    result = _upsert(tools, _plan(provenance=""))
    assert result["ok"] is False
    assert "provenance" in result["error"]


def test_revision_updates_same_plan_in_place(tools):
    first = _upsert(tools, _plan(revision="A"))
    second = _upsert(tools, _plan(revision="B"))
    assert first["ok"] and second["ok"]
    stored = Ledger.load("run-1").mfg_plans()
    assert len(stored) == 1
    assert stored[0]["id"] == "main"
    assert stored[0]["revision"] == "B"


def test_audit_projection_is_accepted_by_drawing_row_validator(tools):
    assert _upsert(tools, _plan())["ok"]
    spec = tools["mfg_plan_get"]("run-1", "main", "audit_spec")
    from services.mcp.cad_drawing_tools import _audit_spec_rows
    rows, error = _audit_spec_rows(spec)
    assert error is None
    assert [row["requirement_id"] for row in rows] == ["DIM7", "FIN1"]


def test_work_instruction_projection_matches_template_contract(tools):
    assert _upsert(tools, _plan())["ok"]
    spec = tools["mfg_plan_get"]("run-1", "main", "work_instructions")
    assert spec["kind"] == spec["template"] == "work_instruction"
    assert spec["outline"][0]["type"] == "part"
    operation = next(row for row in spec["outline"] if row["type"] == "operation")
    assert operation["setup"]["wcs"] == "G54"
    assert operation["inspection"][0]["limits_slot"]
    assert operation["provenance"] == "approved route R1"
    assert spec["attached_results"]
    from services.deliverables.spec import validate_and_resolve
    from services.deliverables.templates import get_template
    from services.deliverables.templates.work_instruction import _validate_document
    resolved = validate_and_resolve(
        spec, "work_instruction",
        get_template("work_instruction", "work_instruction"), [],
    )
    _validate_document(resolved["blocks"])


def test_check_uses_three_states_when_helper_unavailable(tools, monkeypatch):
    assert _upsert(tools, _plan())["ok"]
    Ledger.load("run-1").add_geometry_spec(
        "geometry_designer", "G1", ["FT1"], [{"kind": "mating_bore"}],
        "fit_calc(20,H7/g6)",
    )
    from services.agent import geometry_helpers
    monkeypatch.setattr(geometry_helpers, "resolve_helpers",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("engine offline")))
    checked = tools["mfg_plan_check"]("run-1", "main")
    dfm = next(row for row in checked["checks"] if row["check"] == "dfm:OP10")
    assert dfm["status"] == "not_evaluated"
    assert checked["status"] == "not_evaluated"


def test_geometry_reference_failure_and_open_question_block(tools):
    plan = _plan()
    plan["open_questions"] = [{"id": "Q1", "text": "Confirm coating", "blocking": True}]
    assert _upsert(tools, plan)["ok"]
    checked = tools["mfg_plan_check"]("run-1", "main")
    assert checked["status"] == "fail"
    assert checked["blocking"] is True
    assert any(row["check"] == "geometry_spec_refs" and row["status"] == "fail"
               for row in checked["checks"])


def test_render_records_manufacturing_plan_artifact(tools):
    assert _upsert(tools, _plan())["ok"]
    rendered = tools["mfg_plan_render"]("run-1", "main", "json")
    assert rendered["ok"] is True
    path = Path(rendered["path"])
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "main"
    artifacts = Ledger.load("run-1").state["artifacts"]
    assert artifacts[-1]["kind"] == "manufacturing_plan"


def test_read_tools_are_classified_read_only():
    from services.mcp.tool_registry import classify_risk
    assert classify_risk("mfg_plan_get") == "read"
    assert classify_risk("mfg_plan_check") == "read"
    assert classify_risk("mfg_plan_upsert") == "write"
    assert classify_risk("mfg_plan_render") == "write"
    for name in ("machining_ref_search", "machining_ref_get", "machining_ref_search_pdf"):
        assert classify_risk(name) == "read"
