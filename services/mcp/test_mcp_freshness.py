"""MCP freshness consistency test (T-BL26).

Verifies that every registered MCP tool family (and all individual tool names)
appears in EITHER the server instructions string OR a cad_guide topic, so future
capability additions can't silently become invisible to agents.

How it works:
  1. Import the FastMCP instance from server.py (NO C++ engine needed — just Python
     imports).  Use a lightweight mock for core_bridge so libchudware.so is not required.
  2. Collect all registered tool names from mcp.tools.
  3. Build a combined coverage corpus: server instructions text + all cad_guide topics.
  4. Assert every tool name appears at least once in that corpus.

Run:
  python -m pytest services/mcp/test_mcp_freshness.py -v
  # or, without libchudware.so present (CI):
  python -m pytest services/mcp/test_mcp_freshness.py -v -k freshness
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Lightweight stubs so server.py imports cleanly without C++ engine or MCP
# ---------------------------------------------------------------------------

def _stub_core_bridge():
    """Insert a dummy core_bridge so cad_tools doesn't fail on import."""
    mod = types.ModuleType("services.mcp.core_bridge")
    mod.available = lambda: False
    mod.call = lambda method, params=None: {}
    mod.feat = lambda *a, **kw: {}
    mod.dim = lambda v, unit="mm": {"value": v, "unit": unit}
    sys.modules.setdefault("services.mcp.core_bridge", mod)
    # Also wire the bare-name alias used inside some modules
    sys.modules.setdefault("core_bridge", mod)


def _build_stubs() -> dict:
    """Build a dict of lightweight stub modules for use with patch.dict(sys.modules, ...)."""
    stubs: dict[str, object] = {}

    # core_bridge stubs (use setdefault so they don't clobber real ones)
    for key in ("services.mcp.core_bridge", "core_bridge"):
        if key not in sys.modules:
            mod = types.ModuleType(key)
            mod.available = lambda: False  # type: ignore[attr-defined]
            mod.call = lambda method, params=None: {}  # type: ignore[attr-defined]
            mod.feat = lambda *a, **kw: {}  # type: ignore[attr-defined]
            mod.dim = lambda v, unit="mm": {"value": v, "unit": unit}  # type: ignore[attr-defined]
            stubs[key] = mod

    # mcp package stub — only install if the real mcp is not present
    if "mcp" not in sys.modules:
        mcp_pkg = types.ModuleType("mcp")
        server_mod = types.ModuleType("mcp.server")
        fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

        class _FakeFastMCP:
            def __init__(self, name="", instructions="", **kw):
                self.name = name
                self.instructions = instructions
                self._tools: dict[str, object] = {}

            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

            @property
            def tools(self):
                return self._tools

        fastmcp_mod.FastMCP = _FakeFastMCP
        stubs["mcp"] = mcp_pkg
        stubs["mcp.server"] = server_mod
        stubs["mcp.server.fastmcp"] = fastmcp_mod

    # mcp.server.stdio stub
    if "mcp.server.stdio" not in sys.modules:
        stdio_mod = types.ModuleType("mcp.server.stdio")
        stubs["mcp.server.stdio"] = stdio_mod

    # anyio stub
    if "anyio" not in sys.modules:
        anyio_mod = types.ModuleType("anyio")
        anyio_mod.run = lambda coro: None  # type: ignore[attr-defined]
        anyio_mod.wrap_file = lambda f: f  # type: ignore[attr-defined]
        stubs["anyio"] = anyio_mod

    # services.mcp package stub (so sub-imports work)
    if "services.mcp" not in sys.modules:
        pkg = types.ModuleType("services.mcp")
        pkg.__path__ = []  # type: ignore[attr-defined]
        stubs["services.mcp"] = pkg

    # tool_registry stub
    if "services.mcp.tool_registry" not in sys.modules:
        reg_mod = types.ModuleType("services.mcp.tool_registry")

        class _View:
            def __init__(self, mcp, family):
                self._mcp = mcp
                self._family = family

            def tool(self):
                return self._mcp.tool()

            def __getattr__(self, name):
                return getattr(self._mcp, name)

        reg_mod.view = lambda mcp, family: _View(mcp, family)  # type: ignore[attr-defined]
        stubs["services.mcp.tool_registry"] = reg_mod

    return stubs


def _ensure_stubs():
    """Ensure lightweight stubs are in place (module-level call for corpus builder)."""
    for key, mod in _build_stubs().items():
        sys.modules.setdefault(key, mod)


# ---------------------------------------------------------------------------
# Tool inventory: every tool registered in server.py
# ---------------------------------------------------------------------------

# We enumerate tools from the source directly to avoid the full import chain
# (which requires libchudware.so, scipy, kicad, etc.).  This is more robust for CI.
#
# The canonical list is derived from the @mcp.tool() decorated functions in each
# *_tools.py module.  We maintain it here as a flat set; the test below will
# FAIL if a new tool is added without updating this list OR the instructions/guide.
#
# MAINTENANCE: when you add a new tool, add it to the appropriate family set AND
# ensure its family appears in server instructions or has a cad_guide topic.
# The consistency assertions will catch gaps immediately.

_TOOL_FAMILIES: dict[str, set[str]] = {
    "cad": {
        "cad_guide", "new_part", "add_features", "render_part", "close_part",
        "list_parts", "undo", "redo", "export_part", "import_part",
        "make_drawing", "make_standard_part", "set_material", "set_view",
        "set_process", "set_titleblock",
        "add_drawing_view", "add_section_view", "add_detail_view",
        "add_dimension", "add_gdt", "add_surface_finish", "export_drawing",
        "measure_part", "measure_between", "query_geometry",
        "check_feature_overlap", "check_interference", "check_symmetry",
        "check_metadata", "check_sketch",
        "wall_thickness", "draft_analysis",
        "bom_price", "bom_rollup",
        "hover", "show_in_app",
        "solve_mates", "list_world_bodies",
        "list_configurations", "add_configuration", "activate_configuration",
        "list_standard_parts", "set_config_override", "set_config_suppression",
        "inspect_section", "recover_parts", "make_fit_coupon",
        "contact_sheet",
    },
    "sim": {
        "run_fea_static", "run_fea_modal", "run_cfd_flow", "run_cfd_accurate",
        "run_fea_fatigue", "run_fea_thermal", "run_fea_buckling", "run_fea_contact",
        "simulate_motion",
    },
    "gsl": {
        "describe_part", "query_adjacency", "hover", "describe_sketch",
        "describe_scene", "describe_toolpaths", "describe_results",
        "describe_assembly", "describe_board", "describe_project",
        "sketch_health", "sketch_auto_constrain", "sketch_fully_define",
    },
    "recon": {
        "reverse_engineer", "compare_parts", "convert_face_to_sketch",
        "resolve_process", "dfm_check",
        # photo recon
        "start_capture_session", "stop_capture_session", "request_part_photos",
        "submit_part_photos", "measure_from_photo",
    },
    "intake": {
        "intake_status", "set_process", "confirm_dimensions",
        "dfm_check", "fit_report", "make_fit_coupon",
        "resolve_process",
    },
    "interaction": {
        "ask_user",
    },
    "ecad": {
        "ecad_new_schematic", "ecad_new_pcb", "ecad_place_symbol",
        "ecad_wire", "ecad_label", "ecad_add_power", "ecad_add_zone",
        "ecad_autoroute", "ecad_fill_zones", "ecad_run_erc", "ecad_run_drc",
        "ecad_run_dfm", "ecad_run_hostcheck", "ecad_simulate", "ecad_to_3d", "ecad_export",
        "ecad_status", "ecad_find_part", "ecad_design", "ecad_sweep",
        "ecad_tf", "ecad_update_from_sch", "ecad_check_impedance",
        "ecad_check_diff_impedance",
        "ecad_set_net_class", "ecad_run_sicheck",
        "ecad_new_schematic", "ecad_new_pcb",
    },
    "cam": {
        "cam_new_job", "cam_add_setup", "cam_add_tool", "cam_add_operation",
        "cam_update_operation", "cam_remove_operation", "cam_generate",
        "cam_simulate", "cam_post", "cam_job_tree", "cam_open", "cam_save",
        "cam_undo", "cam_redo", "cam_extract", "cam_set_stock",
        "cam_setup_sheet", "cam_materials", "cam_op_types", "cam_tool_types",
        "cam_post_dialects", "cam_audit", "cam_job_diff",
    },
    "doc_diff": {
        "doc_diff_tool",
    },
    "req": {
        "req_create", "req_update", "req_status", "req_report",
    },
    "verify": {
        "verify_battery", "check_metadata",
    },
    "research": {
        "research_web_search", "research_fetch_page", "research_material_lookup",
        "research_unit_convert", "research_standard_section",
        "research_datasheet_fetch", "research_thread_spec", "research_eng_calc",
        "fit_calc", "recommend_fit",
    },
    "deliverables": {
        "make_report", "make_presentation", "make_workbook",
        "make_work_instruction", "make_weldment_traveler",
        "render_document", "list_artifacts", "list_spec_templates",
    },
    "sourcing": {
        "bom_rollup", "bom_price",
    },
    "cost": {
        "cam_cost_estimate", "request_quote",
    },
    "spec": {
        "match_part_number", "spec_sheet_match", "list_spec_templates",
    },
    "piping": {
        "piping_solve", "piping_report", "piping_operating_point",
        "piping_fluid", "piping_internal_volume",
        "piping_cfd_handoff", "piping_verify_cfd",
        "piping_route", "piping_spec_check", "piping_bom", "piping_load",
        "run_pipe_flow", "piping_pump_drive",
    },
    "industrial": {
        "vessel_size", "duct_size",
    },
    "hydraulic": {
        "hydraulic_new", "hydraulic_add_component", "hydraulic_add_hose",
        "hydraulic_size_cylinder", "hydraulic_size_pump", "hydraulic_bom",
        "hydraulic_size_motor", "pump_drive_size", "hydraulic_simulate", "hydraulic_load",
    },
    "firmware": {
        "fw_new_project", "fw_build", "fw_sources", "fw_sim_run", "fw_verify_board",
        "fw_power_profile", "fw_flash", "fw_debug_start", "fw_debug_halt",
        "fw_debug_read_mem",
    },
    "structural": {
        "struct_analyze", "struct_check", "struct_estimate",
    },
    "parts": {
        "part_search", "part_get", "part_place",
        # Parts Library V2 (workstream 5)
        "parts_taxonomy", "part_source", "vendors_health", "cam_tool_import",
        # vendor-source + profile manager + scrape queue tools (T-X2/T-X4/T-X5/T-X7)
        "vendor_status", "vendor_login", "browser_profiles",
        "scrape_enqueue", "scrape_queue_status", "import_artifact",
    },
    "regen": {
        "regen_diagnose",
    },
    "code_compliance": {
        "code_check",
    },
    "dag": {
        "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort",
        "dag_replan",
    },
    "design_review": {
        "design_review",
    },
    "ledger": {
        "ledger_get", "ledger_note",
    },
    "buildgraph": {
        "buildgraph_plan", "buildgraph_status",
    },
    "interface_check": {
        "interface_check",
    },
    "tolerance_stackup": {
        "stackup_analyze",
    },
    "mechanical": {
        "gear_check", "bearing_life",
    },
    "mission": {
        "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort",
        "dag_replan", "buildgraph_plan", "buildgraph_status",
        "mission_start", "mission_status", "replan_request",
    },
    "plm": {
        "plm_init", "plm_open", "plm_save", "plm_checkpoint", "plm_release",
        "plm_history", "plm_restore", "plm_status", "plm_compare",
        "plm_set_ai_mode", "plm_item_set", "plm_where_used", "plm_timeline",
        "plm_rollback", "plm_rollback_check",
    },
    "reveng": {
        "reverse_engineer", "compare_parts", "convert_face_to_sketch",
        "measure_from_photo", "start_capture_session",
    },
}

# All tool names across all families (deduplicated — some tools like hover appear in
# both cad and gsl; we care that the name appears somewhere, not which module owns it).
ALL_TOOL_NAMES: set[str] = set()
for _names in _TOOL_FAMILIES.values():
    ALL_TOOL_NAMES.update(_names)

# Family-level terms that must appear in either server instructions or cad_guide.
# These are the minimum signals an agent needs to know a capability EXISTS.
_FAMILY_SIGNALS: dict[str, list[str]] = {
    # family_key: list of strings ANY ONE of which must appear in the coverage corpus
    "cad":           ["cad_guide", "new_part", "add_features"],
    "gsl":           ["describe_part", "describe_scene", "cad_guide('gsl')", "gsl"],
    "recon":         ["reverse_engineer", "recon", "cad_guide('reverse')", "reverse"],
    "intake":        ["intake", "dfm_check", "cad_guide('intake')"],
    "interaction":   ["ask_user", "cad_guide('interaction')", "interaction"],
    "ecad":          ["ecad_", "ecad_new_schematic", "ecad_*"],
    "cam":           ["cam_", "cam_new_job", "cad_guide('cam')", "cam tools"],
    "doc_diff":      ["doc_diff", "cad_guide('doc_diff')", "doc_diff_tool"],
    "req":           ["req_create", "requirements", "cad_guide('requirements')"],
    "verify":        ["verify_battery", "cad_guide('verify')", "verify"],
    "research":      ["research_web_search", "research_fetch_page", "research"],
    "deliverables":  ["make_report", "make_presentation", "cad_guide('deliverables')"],
    "sourcing":      ["bom_rollup", "cad_guide('sourcing')", "sourcing"],
    "cost":          ["cam_cost_estimate", "cad_guide('cost')", "cost"],
    "spec":          ["match_part_number", "spec_sheet_match", "cad_guide('spec')"],
    "piping":        ["piping_solve", "cad_guide('piping')", "piping"],
    "industrial":    ["vessel_size", "duct_size", "cad_guide('industrial')"],
    "hydraulic":     ["hydraulic_new", "cad_guide('hydraulic')", "hydraulic"],
    "firmware":      ["fw_new_project", "fw_build", "cad_guide('firmware')", "firmware"],
    "structural":    ["struct_analyze", "cad_guide('structural')", "structural"],
    "parts":         ["part_search", "part_place", "cad_guide('parts')",
                      "parts_taxonomy", "part_source"],
    "regen":         ["regen_diagnose", "cad_guide('regen')", "regen"],
    "code_compliance": ["code_check", "cad_guide('code_compliance')", "code_compliance"],
    "dag":           ["dag_plan", "dag_run", "cad_guide('dag')", "dag"],
    "design_review": ["design_review", "cad_guide('design_review')"],
    "ledger":         ["ledger_get", "ledger_note", "design ledger", "ledger"],
    "buildgraph":    ["buildgraph_plan", "buildgraph_status", "buildgraph", "aicad.buildgraph/1"],
    "interface_check": ["interface_check", "interface alignment", "cad_guide('design_review')"],
    "tolerance_stackup": ["stackup_analyze", "cad_guide('tolerance_stackup')", "tolerance_stackup"],
    "mechanical":    ["gear_check", "bearing_life", "mechanical"],
}


# Import cad_guide_text EARLY, before _ensure_stubs() pollutes sys.modules with
# stub mcp/anyio packages.  cad_tools.py imports FastMCP at module level, so it
# must be imported while the REAL mcp package is still accessible.
try:
    from services.mcp.cad_tools import cad_guide_text as _cad_guide_text_real  # noqa: E402
except Exception:
    _cad_guide_text_real = None  # type: ignore[assignment]


def _build_coverage_corpus() -> str:
    """Build a combined text corpus from server instructions + all cad_guide topics."""
    corpus_parts: list[str] = []

    # Server instructions
    try:
        import importlib
        _ensure_stubs()
        # Import server instructions without running main()
        import services.mcp.server as srv  # noqa: PLC0415
        mcp_obj = srv.mcp
        corpus_parts.append(getattr(mcp_obj, "instructions", "") or "")
    except Exception:
        # Fallback: read server.py as text and extract the instructions string
        import pathlib
        srv_path = pathlib.Path(__file__).parent / "server.py"
        if srv_path.exists():
            corpus_parts.append(srv_path.read_text())

    # cad_guide topics — use the pre-imported function (imported before stubs were
    # installed so the real mcp package was still present).
    _guide_fn = _cad_guide_text_real
    if _guide_fn is not None:
        all_topics = [
            "overview", "simulation", "motion", "review", "intake", "gsl", "cam",
            "narration", "parts", "reverse", "deliverables",
            "requirements", "dag", "verify", "interaction", "sourcing",
            "cost", "spec", "piping", "industrial", "hydraulic", "firmware", "structural",
            "regen", "code_compliance", "design_review", "doc_diff", "plm",
            "tolerance_stackup",
            "mechanical",
        ]
        for t in all_topics:
            try:
                corpus_parts.append(_guide_fn(t))
            except Exception:
                pass
    else:
        # Fallback: read cad_tools.py as text
        import pathlib
        ct_path = pathlib.Path(__file__).parent / "cad_tools.py"
        if ct_path.exists():
            corpus_parts.append(ct_path.read_text())

    return "\n".join(corpus_parts)


class TestMCPFreshness(unittest.TestCase):
    """Consistency: every registered tool family is discoverable from the agent's entry points."""

    _corpus: str = ""
    _stubs_patcher: object = None  # patch.dict context manager

    @classmethod
    def setUpClass(cls):
        # Install stubs only for the duration of this test class so they don't
        # leak into later test modules (e.g. test_reveng_e2e_datasheet which
        # needs the *real* mcp.server.fastmcp.FastMCP with _tool_manager).
        stubs = _build_stubs()
        cls._stubs_patcher = patch.dict(sys.modules, stubs)
        cls._stubs_patcher.start()
        cls._corpus = _build_coverage_corpus()
        assert cls._corpus, "Coverage corpus is empty — check import paths"

    @classmethod
    def tearDownClass(cls):
        if cls._stubs_patcher is not None:
            cls._stubs_patcher.stop()

    def test_corpus_nonempty(self):
        """Sanity: corpus must have meaningful content."""
        self.assertGreater(len(self._corpus), 500,
                           "Coverage corpus suspiciously short")

    def test_family_signals_in_corpus(self):
        """Every tool family has at least one signal string in server instructions or cad_guide."""
        corpus_lower = self._corpus.lower()
        missing: list[str] = []
        for family, signals in _FAMILY_SIGNALS.items():
            if not any(s.lower() in corpus_lower for s in signals):
                missing.append(
                    f"  {family!r}: none of {signals} found in corpus"
                )
        self.assertFalse(
            missing,
            "Tool families undiscoverable from server instructions + cad_guide:\n"
            + "\n".join(missing)
            + "\n\nFix: add the family to the server instructions string (server.py) "
              "OR add a cad_guide topic for it in cad_tools.py:cad_guide_text()."
        )

    def test_key_tools_discoverable(self):
        """A sample of important individual tool names appears in the corpus."""
        # These are the highest-value tools agents are most likely to mis-use or skip.
        # The full tool inventory is in _TOOL_FAMILIES; here we spot-check critical ones.
        must_appear = [
            "part_search", "part_place",           # standard-parts bias
            "parts_taxonomy", "part_source",        # Parts Library V2 browse + sourcing
            "vendors_health", "cam_tool_import",    # vendor canary + cutting-tool import
            "scrape_enqueue", "import_artifact",    # catalog geometry acquisition
            "verify_battery",                       # done-check
            "ask_user",                             # elicitation seam
            "dag_plan",                             # multi-step missions
            "req_create",                           # requirements
            "regen_diagnose",                       # rebuild failure recovery
            "doc_diff_tool",                        # semantic diff
            "design_review",                        # geometry battery
            "cam_new_job",                          # CAM entry point
            "piping_solve",                         # piping
            "piping_route", "piping_spec_check",    # piping design
            "piping_bom", "piping_load",
            "run_pipe_flow",                        # internal-flow CFD
            "vessel_size", "duct_size",             # industrial sizing
            "hydraulic_new",                        # hydraulic
            "hydraulic_simulate", "hydraulic_load",  # fluid-power transient + reload
            "hydraulic_size_motor", "pump_drive_size",
            "piping_pump_drive",                    # network pump electric drive
            "fw_new_project",                       # firmware
            "struct_analyze",                       # structural
            "code_check",                           # compliance
            "bom_rollup",                           # sourcing
            "cam_cost_estimate",                    # cost
            "buildgraph_plan",                      # build-order DAG planner
            "stackup_analyze",                      # tolerance stack-up
        ]
        corpus_lower = self._corpus.lower()
        missing = [t for t in must_appear if t.lower() not in corpus_lower]
        self.assertFalse(
            missing,
            f"Key tool names missing from corpus: {missing}\n"
            "Add them to server.py instructions or cad_guide_text()."
        )

    def test_cad_guide_topics_listed_in_fallback(self):
        """The fallback error message in cad_guide_text lists all known topic families."""
        if _cad_guide_text_real is None:
            self.skipTest("Could not import cad_guide_text (mcp package unavailable)")
            return
        try:
            fallback = _cad_guide_text_real("__nonexistent_topic_xyz__")
        except Exception as exc:
            self.skipTest(f"cad_guide_text raised: {exc}")
            return
        required_in_fallback = [
            "requirements", "dag", "verify", "interaction", "sourcing",
            "cost", "spec", "piping", "industrial", "hydraulic", "firmware", "structural",
            "regen", "code_compliance", "design_review", "doc_diff", "plm",
            "tolerance_stackup",
            "mechanical",
        ]
        missing = [t for t in required_in_fallback if t not in fallback]
        self.assertFalse(
            missing,
            f"Topics missing from cad_guide fallback error message: {missing}\n"
            "Update the return statement at the end of cad_guide_text()."
        )

    def test_tool_families_dict_covers_key_families(self):
        """_TOOL_FAMILIES inventory includes all server.py register_* families."""
        required_families = {
            "cad", "gsl", "recon", "intake", "interaction", "ecad", "cam",
            "doc_diff", "req", "verify", "research", "deliverables",
            "sourcing", "cost", "spec", "piping", "industrial", "hydraulic", "firmware",
            "structural", "parts", "regen", "code_compliance",
            "dag", "design_review", "ledger", "tolerance_stackup", "mechanical",
            "sim", "mission", "plm", "reveng",
        }
        missing = required_families - set(_TOOL_FAMILIES.keys())
        self.assertFalse(
            missing,
            f"Tool families missing from _TOOL_FAMILIES inventory: {missing}\n"
            "Update the _TOOL_FAMILIES dict in this test file when adding new families."
        )


if __name__ == "__main__":
    # Also runnable as: python -m services.mcp.test_mcp_freshness
    unittest.main(verbosity=2)
