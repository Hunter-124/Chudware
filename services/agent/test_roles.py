"""Tests for services/agent/roles.py — role cards and handoff validation.

Covers:
  - the AUTHORED role cards all load and validate (zero errors);
  - allowlists are enforced through the MCP tool_registry (an out-of-scope tool call
    is refused);
  - structured handoffs validate against the card schema.

Runs headless: no FastMCP, no libchudware, no network, no LLM. The tool catalog is
built by registering MCP tool modules against a stub and includes mission-scoped
handlers resolved by MissionDispatch.
"""
from __future__ import annotations

from dataclasses import replace
import glob
import importlib
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent import roles  # noqa: E402
from services.mcp import tool_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# Tool catalog (headless): register every MCP tool module against a stub so the
# resolvability check has the full universe of tool names without FastMCP.
# ---------------------------------------------------------------------------

class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[k.get("name") or getattr(fn, "__name__", "?")] = fn
            return fn
        return deco


def _build_catalog() -> set[str]:
    reg.clear_catalog()

    def _regmod(modpath: str, family: str) -> None:
        try:
            m = importlib.import_module(modpath)
        except Exception:  # noqa: BLE001
            return
        for n in dir(m):
            if n.startswith("register_") and n.endswith("_tools"):
                try:
                    getattr(m, n)(reg.view(_StubMCP(), family))
                except Exception:  # noqa: BLE001
                    pass

    for path in sorted(glob.glob(str(_REPO / "services" / "mcp" / "*_tools.py"))):
        mod = os.path.basename(path)[:-3]
        if mod.startswith("test_"):
            continue
        _regmod(f"services.mcp.{mod}", mod.replace("_tools", ""))
    _regmod("services.dag.mcp_tools", "dag")
    _regmod("services.agent.buildgraph.mcp_tools", "buildgraph")
    # cam_audit.py is not named *_tools.py (Wave 2); register it under family cam.
    _regmod("services.mcp.cam_audit", "cam")
    # This registrar does not use the *_tools naming convention.
    from services.mcp.fea_probe import register_fea_probe
    register_fea_probe(reg.view(_StubMCP(), "sim"))
    # Delegate and other mission handlers are not global MCP tools.
    from services.agent.mission_dispatch import MissionDispatch
    dispatch = MissionDispatch(run_id="role-catalog-test", coordination_board=object())
    mission_tools = {name for name in dispatch.TOOLS if dispatch.resolve(name) is not None}
    return set(reg.catalog().keys()) | mission_tools


_CATALOG = _build_catalog()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoadAndValidate(unittest.TestCase):
    def test_all_roles_load(self):
        # Pipeline roles plus design_thinker (ladder) and feature_architect
        # (design-pipeline Stage 1) +
        # geometry_designer (design-pipeline Stage 2) +
        # linkage_planner (design-pipeline Stage 3) +
        # sourcing_specialist (Parts Library V2).
        cards = roles.load_all_roles()
        self.assertGreaterEqual(len(cards), 19, sorted(cards))
        self.assertIn("sketch2d", cards)
        self.assertIn("model3d", cards)
        for rid in ("intake_analyst", "clarifier", "spec_writer", "orchestrator",
                    "layout_engineer", "sketcher", "feature_modeler",
                    "assembly_fit_engineer", "sim_engineer", "cam_programmer",
                    "reviewer", "fixer", "overseer",
                    "design_thinker",
                    "feature_architect", "geometry_designer",
                    "linkage_planner", "sourcing_specialist"):
            self.assertIn(rid, cards)

    def test_suite_has_zero_errors(self):
        findings = roles.validate_suite(tool_names=_CATALOG)
        errs = roles.errors(findings)
        self.assertEqual(errs, [], "\n".join(str(f) for f in errs))

    def test_catalog_includes_mission_and_nonstandard_registrars(self):
        self.assertIn("delegate", _CATALOG)
        self.assertIn("fea_probe", _CATALOG)

    def test_validator_still_rejects_unknown_tools(self):
        card = roles.load_role("sketch2d")
        card = replace(card, tool_allowlist=(roles.ToolRef(tool="not_a_real_tool"),))
        findings = roles.errors(roles.validate_role(card, tool_names=_CATALOG))
        self.assertTrue(any("not_a_real_tool" in f.message for f in findings), findings)

    def test_assembly_role_keeps_advanced_checks_requestable(self):
        from services.agent.tool_categories import resolve_categories

        card = roles.load_role("assembly_fit_engineer")
        self.assertLessEqual(len(card.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)
        self.assertIn("delegate", card.concrete_tools)
        self.assertIn("enable_tools", card.concrete_tools)
        checks = {"interface_check", "assembly_dof", "fastener_check",
                  "tool_access_check", "disassembly_check", "mate_fit_check",
                  "bom_consistency_check"}
        self.assertTrue(checks <= resolve_categories(["assembly"]), checks)
        self.assertTrue(checks <= _CATALOG, checks - _CATALOG)

    def test_stage_scopes_in_vocabulary(self):
        for card in roles.load_all_roles().values():
            for st in card.stage_scope:
                self.assertIn(st, roles.STAGES, f"{card.id}: {st}")

    def test_bad_role_schema_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text("schema: aicad.role/2\nid: bad\n")
            with self.assertRaises(roles.RoleError):
                roles.load_role("bad", roles_dir=Path(d))


class TestAllowlistEnforcement(unittest.TestCase):
    def setUp(self):
        reg.reset_policy()

    def tearDown(self):
        reg.reset_policy()

    def test_out_of_scope_tool_blocked(self):
        sketcher = roles.load_role("sketcher")
        sketcher.apply_tool_policy(reg)
        # in-scope tool allowed
        self.assertTrue(reg.is_allowed("sketch_health"))
        # out-of-scope tool refused (cam is not in the sketcher card)
        self.assertFalse(reg.is_allowed("cam_post"))
        refusal = reg.refusal("cam_post")
        self.assertFalse(refusal["ok"])
        self.assertIn("allow-list", refusal["reason"])

    def test_sim_engineer_can_use_structural_diagnostics(self):
        roles.load_role("sim_engineer").apply_tool_policy(reg)
        for name in ("wall_thickness", "query_adjacency", "find_stress_risers",
                     "fea_probe", "run_fea_fatigue", "run_fea_buckling"):
            self.assertTrue(reg.is_allowed(name), name)

    def test_geometry_designer_can_use_calculators_and_diagnostics(self):
        roles.load_role("geometry_designer").apply_tool_policy(reg)
        for name in ("research_eng_calc", "dfm_check", "find_stress_risers",
                     "draft_analysis"):
            self.assertTrue(reg.is_allowed(name), name)

    def test_guard_returns_refusal_on_dispatch(self):
        """A guarded tool function returns the refusal dict instead of running when
        the tool is outside the active role allowlist."""
        stub = _StubMCP()
        view = reg.view(stub, "cam")

        @view.tool()
        def cam_post():  # registered under family cam
            return {"ok": True, "posted": True}

        roles.load_role("sketcher").apply_tool_policy(reg)
        out = stub.tools["cam_post"]()
        self.assertFalse(out["ok"])  # refused, did not run


def _envelope(card, *, status, summary, outputs):
    """An ``aicad.handoff/1`` envelope for ``card`` (contract §4)."""
    return {
        "schema": "aicad.handoff/1",
        "role": card.id,
        "status": status,
        "summary": summary,
        "outputs": outputs,
    }


class TestHandoffValidation(unittest.TestCase):
    def test_valid_handoff_validates(self):
        card = roles.load_role("sketcher")
        env = _envelope(
            card,
            status="done",
            summary="closed, DOF=0",
            outputs={
                "sketch_ref": {"doc_id": "d1", "sketch_id": "s1"},
                "health": {"closed": True, "dof": 0, "conflicts": []},
                "driving_dimensions": [
                    {"name": "bore_d", "value": 20.02, "ledger_dim_id": "DIM1"}
                ],
                "reasoning_summary": "constrained geometry then dimensions",
            },
        )
        self.assertEqual(roles.validate_handoff(card, env), [])

    def test_missing_required_output_rejected(self):
        card = roles.load_role("sketcher")
        env = _envelope(card, status="done", summary="x",
                        outputs={"sketch_ref": {"doc_id": "d", "sketch_id": "s"}})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("health" in e for e in errs), errs)

    def test_bad_status_rejected(self):
        card = roles.load_role("sketcher")
        env = _envelope(card, status="finished", summary="x", outputs={})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("status" in e for e in errs), errs)

    def test_maxlength_enforced_post_parse(self):
        card = roles.load_role("clarifier")  # summary maxLength 500
        env = _envelope(
            card, status="done", summary="x" * 600,
            outputs={"resolved": [], "still_open": [], "reasoning_summary": "ok"})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("maxLength" in e for e in errs), errs)


class TestSketch2dModel3dRoles(unittest.TestCase):
    """New 2D/3D split roles (sketch2d + model3d)."""

    def test_load_sketch2d_and_model3d(self):
        s2 = roles.load_role("sketch2d")
        m3 = roles.load_role("model3d")
        self.assertEqual(s2.id, "sketch2d")
        self.assertEqual(m3.id, "model3d")

    def test_model3d_prompt_contract(self):
        m3 = roles.load_role("model3d")
        prompt = m3.system_prompt
        low = prompt.lower()
        self.assertIn("delegate", m3.concrete_tools)
        self.assertIn("sketch2d", low)
        self.assertIn("vision", low)
        self.assertIn("delegate", low)

    def test_sketch2d_no_solids_contract(self):
        s2 = roles.load_role("sketch2d")
        low = s2.system_prompt.lower()
        self.assertIn("do not", low)
        self.assertIn("solid features", low)
        self.assertIn("never attempt them here", low)
        self.assertNotIn("delegate", s2.concrete_tools)
        # 2D tools only — no pure solid planners beyond sketch-capable add_features
        self.assertIn("sketch_health", s2.concrete_tools)

    def test_roles_validate_against_catalog(self):
        for rid in ("sketch2d", "model3d"):
            card = roles.load_role(rid)
            findings = roles.validate_role(card, tool_names=_CATALOG)
            errs = [f for f in findings if f.level == "error"]
            self.assertEqual(errs, [], f"{rid}: " + "; ".join(str(e) for e in errs))



class TestPromptOverrideIsolation(unittest.TestCase):
    def test_mission_override_wins_and_catalog_lists_only_mission_rows(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"CHUDWARE_APPDATA": td}
        ):
            prompt_dir = Path(td) / "prompts"
            prompt_dir.mkdir(parents=True)
            mission_override = prompt_dir / "reviewer.mission.md"
            mission_override.write_text(
                "MISSION REVIEWER OUTPUT: criterion_results", encoding="utf-8"
            )

            self.assertEqual(
                roles.resolve_role_system_prompt("reviewer", "YAML FALLBACK"),
                "MISSION REVIEWER OUTPUT: criterion_results",
            )
            rows = {row["role"]: row for row in roles.list_editable_prompts()}
            self.assertEqual(
                rows["mission:reviewer"]["override_path"], str(mission_override)
            )
            self.assertIn("criterion_results", rows["mission:reviewer"]["text"])
            self.assertFalse([role for role in rows if role.startswith("core:")])

    def test_role_guidance_uses_real_meta_and_datasheet_call_shapes(self):
        prompts = {
            role_id: card.system_prompt
            for role_id, card in roles.load_all_roles().items()
        }
        self.assertNotIn("enable_tools([", "\n".join(prompts.values()))
        datasheet = prompts["re_datasheet_reader"]
        self.assertIn("research_datasheet_fetch(source=<PDF URL>)", datasheet)
        self.assertNotIn("research_datasheet_fetch(mpn)", datasheet)

if __name__ == "__main__":
    unittest.main()


def test_role_prompts_do_not_delegate_to_unknown_roles():
    roles_dir = _REPO / "services" / "agent" / "roles"
    known = {
        line.split(":", 1)[1].strip()
        for path in roles_dir.glob("*.yaml")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("id:")
    }
    referenced: set[str] = set()
    for root in (roles_dir, _REPO / "services" / "agent" / "prompts"):
        for path in root.glob("*.yaml" if root == roles_dir else "*.md"):
            text = path.read_text(encoding="utf-8")
            referenced.update(re.findall(r"delegate\(role=[\"']([^\"']+)", text))
    assert referenced <= known, f"role prompts delegate to unknown roles: {referenced - known}"
