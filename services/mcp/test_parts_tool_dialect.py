"""Input-dialect regression tests for the parts tools (services/mcp/parts_tools.py).

Every argument shape exercised here came out of a REAL recorded agent run and was
verified failing by ``tools/flywheel/replay_ledger_failures.py`` before the fix:

    part_search(description=...)
    part_search(part_type=..., fit=..., shaft_diameter=..., bolt_circle=..., ...)
    part_place(part=..., location=..., size=..., orientation=...)
    part_place(...)  -> "params argument is not valid JSON."   (x2)
    part_place(...)  -> "doc argument is not valid JSON."

The dispatch path under test is the BUILT-IN agent's:
``agent_turn._bind_tool`` -> ``agent_turn._reconcile_kwargs`` -> the tool function.
``_reconcile_kwargs`` refuses to call a tool with a kwarg its signature does not
name, so these tests fail loudly if a tool ever loses its var-keyword parameter.

The FastMCP surface is covered too: a bare ``**kwargs`` turns into a REQUIRED
schema field named "kwargs" and breaks every external call, so the canonical
calls are also driven through ``Tool.run``.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from services.agent import agent_turn as at

# A part id the bundled clean-room fastener table always carries (no network).
M8 = "fastener:ISO 4762:M8"

WIDENED_TOOLS = (
    "part_search", "part_place", "part_get", "parts_taxonomy", "part_source",
    "match_model", "reverse_engineer_product", "vendors_health",
    "cam_tool_import", "vendor_status", "vendor_login", "browser_profiles",
    "scrape_enqueue", "scrape_queue_status", "import_artifact",
)


def _dispatch(tool: str, /, **kwargs):
    """Exactly what the built-in agent does. Returns (payload, reconciler_error)."""
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve in the MCP registry"
    bound, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    if err is not None:
        return None, str(err.get("error", ""))
    out = fn(**bound)
    return (json.loads(out) if isinstance(out, str) else out), None


def _call(tool: str, /, **kwargs) -> dict:
    payload, err = _dispatch(tool, **kwargs)
    assert err is None, f"{tool} rejected {sorted(kwargs)} before calling: {err}"
    return payload


def _notes(payload: dict) -> str:
    return " ".join(payload.get("interpreted") or [])

@pytest.fixture(scope="module", autouse=True)
def local_catalog(tmp_path_factory):
    """Exercise dialects against real bundled records, never a user's live providers."""
    import services.parts as parts
    from services.parts.adapters.bolts_adapter import BoltsAdapter
    from services.parts.adapters.core_families_adapter import CoreFamiliesAdapter

    root = tmp_path_factory.mktemp("parts-dialect-catalog")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CHUDWARE_PARTS_INDEX_CACHE", str(root / "parts.sqlite"))
        hub = parts.PartsHub()
        hub.register(BoltsAdapter())
        hub.register(CoreFamiliesAdapter())
        hub.rebuild_index()
        patch.setattr(parts, "_hub", hub)
        yield hub



@pytest.fixture
def placed(monkeypatch):
    """Capture what part_place forwards to the C++ placement dispatcher."""
    fn = at._bind_tool("part_place")
    pkg = fn.__module__.rsplit(".", 1)[0]
    bridge = importlib.import_module(f"{pkg}.core_bridge")
    seen: dict = {}

    def fake_call(method, params=None):
        seen["method"] = method
        seen["params"] = params or {}
        return {"ok": True, "domain": "mech", "doc_id": "doc-test",
                "entity_id": "e1", "editable": True}

    monkeypatch.setattr(bridge, "call", fake_call)
    return seen


# --------------------------------------------------------------------------
# part_search — describing the part instead of naming it
# --------------------------------------------------------------------------

def test_description_is_read_as_query():
    """Recorded failure: part_search(description=...) -> unknown argument."""
    out = _call("part_search",
                description="flanged bushing sliding fit bore 20mm 4xM4 bolt circle")
    assert out["ok"] is True
    assert "as query" in _notes(out)
    assert out["applied"]["query"].startswith("flanged bushing sliding fit")


@pytest.mark.parametrize("spelling", ["text", "q", "search", "name", "keywords"])
def test_every_documented_query_spelling(spelling):
    out = _call("part_search", **{spelling: "M8 socket head"})
    assert out["ok"] is True
    assert out["applied"]["query"] == "M8 socket head"


def test_attribute_kwargs_are_folded_into_a_synthesized_query():
    """Recorded failure: the model described the part by engineering attributes."""
    out = _call("part_search", part_type="bushing", fit="sliding",
                shaft_diameter="20", bolt_circle="PCD34", bolt_count="4",
                bolt_size="M4")
    assert out["ok"] is True
    query = out["applied"]["query"]
    # The head noun leads; every attribute survives into the search text.
    assert query.startswith("bushing")
    for term in ("sliding", "shaft diameter 20", "bolt circle PCD34",
                 "bolt count 4", "bolt size M4"):
        assert term in query, f"{term!r} missing from synthesized query {query!r}"
    # The synthesized query is echoed twice: in `interpreted` (house convention)
    # and in `hint`, which result_serving never budget-drops.
    assert query in _notes(out)
    assert query in out["hint"]


def test_unit_suffix_becomes_a_unit_on_the_value():
    out = _call("part_search", part_type="bushing", bore_mm=20)
    assert "bore 20mm" in out["applied"]["query"]


def test_attributes_extend_an_explicit_query_rather_than_replacing_it():
    out = _call("part_search", query="flanged bushing", fit="sliding")
    assert out["applied"]["query"] == "flanged bushing fit sliding"


def test_attributes_are_not_smuggled_into_facets():
    """A facet key no record carries rejects EVERY record, so attributes stay text."""
    out = _call("part_search", part_type="bushing", shaft_diameter="20")
    assert out["applied"]["facets"] == {}


def test_the_interpretation_echo_survives_output_budget_pressure():
    """The echo must NAME the rewrite, and must outlive the token budget.

    Regression: `applied` (and `interpreted`) sat in
    result_serving._SOFT_DROP, so a search big enough to need trimming had its
    read-back deleted BEFORE the result list was even sampled. The model then
    saw hits for a query it never wrote, with no record of the rewrite — the
    silent-rewrite defect this echo exists to prevent.
    """
    from services.mcp import result_serving as rs

    payload = _call("part_search", part_type="bushing", bore_mm=20, detail="full")
    assert payload["ok"] is True
    summary = rs.project_part_search(payload, rs.ServeOpts(detail="summary"))
    # Apply real budget pressure explicitly, independent of live vendor notices
    # or the size of whichever local catalog a preceding test happened to leave.
    budget = max(1, rs.count_tokens(summary) // 2)
    out = json.loads(rs.serve(summary, rs.ServeOpts(budget_tokens=budget)))
    assert out["meta"]["truncated"] is True, "expected a budget-trimmed payload"
    dropped = out["meta"]["omitted_keys"]
    for key in ("applied", "interpreted", "interpreted_args"):
        assert key not in dropped, f"{key} was sacrificed to the token budget"
    # Names the transformation, not merely present / not merely non-empty.
    assert out["applied"]["query"] == "bushing bore 20mm"
    assert out["applied"]["facets"] == {}
    note = _notes(out)
    assert "bore_mm" in note and "bore 20mm" in note


def test_nothing_to_search_for_is_a_teaching_error():
    out = _call("part_search")
    assert out["ok"] is False
    assert "query" in out["hint"]
    assert "part_type" in out["hint"]  # the describe-the-part form is named
    assert "parts_taxonomy" in out["hint"]


def test_malformed_facets_quotes_the_parse_failure():
    out = _call("part_search", query="tube", facets="{not json")
    assert out["ok"] is False
    assert "not valid JSON" in out["reason"]
    assert "Expecting" in out["reason"]        # the json error itself
    assert "{not json" in out["reason"]        # what we actually got
    assert "parts_taxonomy" in out["hint"]     # how to find real facet keys


def test_canonical_search_is_byte_identical():
    """A documented call must gain no keys and no `interpreted` chatter."""
    from services.mcp.parts_tools import search_parts_v2

    out = search_parts_v2("M8 socket head")
    assert out["ok"] is True
    assert set(out) == {"ok", "count", "has_live", "results", "notices",
                        "facet_summary", "applied"}
    assert out["applied"]["query"] == "M8 socket head"
    assert out == search_parts_v2(query="M8 socket head")


# --------------------------------------------------------------------------
# part_place — dict params/doc, flat placement intent, honest JSON errors
# --------------------------------------------------------------------------

def test_params_may_be_a_real_dict(placed):
    """Recorded failure: `params` is typed str and JSON-parsed; models pass dicts."""
    out = _call("part_place", part_id=M8, params={"length_mm": 25})
    assert out["ok"] is True
    assert placed["params"]["params"] == {"length_mm": 25}
    assert "read params as a dict" in _notes(out)


def test_params_json_string_still_works(placed):
    out = _call("part_place", part_id=M8, params='{"length_mm": 25}')
    assert out["ok"] is True
    assert placed["params"]["params"] == {"length_mm": 25}
    assert "interpreted" not in out


def test_doc_may_be_a_real_dict(placed):
    doc = {"kind": "schematic", "devices": []}
    out = _call("part_place", part_id=M8, doc=doc)
    assert out["ok"] is True
    assert placed["params"]["doc"] == doc


def test_malformed_params_json_quotes_the_error_and_shows_an_example():
    """Recorded failure: params="{" -> "params argument is not valid JSON." """
    out = _call("part_place", part_id=M8, params="{")
    assert out["ok"] is False
    assert "not valid JSON" in out["reason"]
    assert "Expecting" in out["reason"]      # the parser's own message
    assert "'{'" in out["reason"]            # the offending input
    assert 'params={"length_mm": 25}' in out["hint"]


def test_malformed_doc_json_quotes_the_error_and_shows_an_example():
    out = _call("part_place", part_id=M8, doc='{"kind": ')
    assert out["ok"] is False
    assert "doc is not valid JSON" in out["reason"]
    assert "Expecting" in out["reason"]
    assert "json.dumps(doc)" in out["hint"]


def test_doc_id_string_is_ignored_for_a_part_that_makes_its_own_doc(placed):
    """Recorded failure: doc="doc-cpp0000000d" -> "doc argument is not valid JSON."

    A mechanical placement CREATES its document, so a doc id there is harmless —
    but it must be reported, never swallowed.
    """
    out = _call("part_place", part_id=M8, doc="doc-cpp0000000d", params="{}")
    assert out["ok"] is True
    assert placed["params"]["doc"] is None
    note = _notes(out)
    assert "doc-cpp0000000d" in note and "core_family" in note


def test_flat_placement_kwargs_fold_into_params(placed):
    """Recorded failure: part_place(part=..., location=..., size=..., orientation=...)."""
    out = _call("part_place", part=M8, location="origin", size=[80, 20, 10],
                orientation=[0, 0, 0])
    assert out["ok"] is True
    assert placed["params"]["params"] == {"pos": "origin", "size": [80, 20, 10],
                                          "rotation": [0, 0, 0]}
    note = _notes(out)
    assert "part=... as part_id=..." in note
    assert "params['pos']" in note


def test_explicit_params_wins_over_a_flat_keyword(placed):
    out = _call("part_place", part_id=M8, params={"pos": {"x": 1}}, location="origin")
    assert placed["params"]["params"]["pos"] == {"x": 1}
    assert "wins" in _notes(out)


def test_unknown_part_id_names_the_tool_that_would_have_worked():
    """"bushing_body" is a body the model modelled — part_place is the wrong tool."""
    out = _call("part_place", part="bushing_body", location="origin")
    assert out["ok"] is False
    assert "bushing_body" in out["reason"]
    hint = out["hint"]
    assert "part_search" in hint
    assert "add_features" in hint
    assert "asm_add_component" in hint


def test_missing_part_id_is_a_teaching_error():
    out = _call("part_place")
    assert out["ok"] is False
    assert "part_id" in out["reason"]
    assert "part_search" in out["hint"]


def test_canonical_place_forwards_exactly_what_it_used_to(placed):
    out = _call("part_place", part_id=M8)
    assert out["ok"] is True
    assert placed["method"] == "part_place"
    assert placed["params"]["part_id"] == M8
    assert placed["params"]["doc"] is None
    assert placed["params"]["params"] is None
    assert "interpreted" not in out


# --------------------------------------------------------------------------
# The rest of the file — same two defect classes
# --------------------------------------------------------------------------

def test_part_get_accepts_id_spellings():
    out = _call("part_get", id=M8)
    assert out["ok"] is True
    assert "as part_id" in _notes(out)


def test_part_get_unknown_id_teaches_the_id_vocabulary():
    out = _call("part_get", part_id="bushing_body")
    assert out["ok"] is False
    assert "<domain>:<catalog_id>" in out["hint"]
    assert "part_search" in out["hint"]


def test_part_get_without_an_id_does_not_pretend_to_look_anything_up():
    out = _call("part_get")
    assert out["ok"] is False
    assert "part_id" in out["reason"]


def test_parts_taxonomy_accepts_category_id():
    out = _call("parts_taxonomy", category_id="fasteners")
    assert out["ok"] is True
    assert out["node"]["id"] == "fasteners"
    assert "as node_id" in _notes(out)


def test_parts_taxonomy_rejects_an_attribute_call_with_a_pointer_to_part_search():
    out = _call("parts_taxonomy", bore_mm=20)
    assert out["ok"] is False
    assert "part_search" in out["hint"]


def test_part_source_folds_attributes_into_the_need():
    out = _call("part_source", need="flanged bushing", bore_mm=20, fit="sliding")
    assert out["ok"] is True
    # part_source's own `interpreted` read-back is preserved, not clobbered.
    assert isinstance(out.get("interpreted"), dict)
    assert "bore 20mm" in " ".join(out["interpreted_args"])


def test_part_source_query_alias():
    out = _call("part_source", query="bearing 12mm bore")
    assert out["ok"] is True
    assert "as need" in " ".join(out.get("interpreted_args") or [])


def test_part_source_without_a_need_teaches_the_form():
    out = _call("part_source")
    assert out["ok"] is False
    assert "part_source(need=" in out["hint"]


def test_cam_tool_import_accepts_tool_id_spellings():
    out = _call("cam_tool_import", tool="tools:not-a-real-cutter")
    # The record lookup is what fails now — not the argument name.
    assert out["ok"] is False
    assert "<domain>:<catalog_id>" in out["hint"]


def test_import_artifact_accepts_file_path_spellings(tmp_path):
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")
    out = _call("import_artifact", file_path=str(step))
    reason = str(out.get("reason", ""))
    # The alias resolved to a real, existing path: neither the "no place for"
    # rejection nor the missing/absent-path guards fired.
    assert "no place for" not in reason
    assert "needs an artifact_path" not in reason
    assert "file not found" not in reason


def test_import_artifact_without_a_path_points_at_the_queue():
    out = _call("import_artifact")
    assert out["ok"] is False
    assert "scrape_queue_status" in out["hint"]


def test_vendors_health_accepts_a_vendor_list(monkeypatch):
    import services.browser.health as health

    seen: dict = {}

    def fake_run(sites=None):
        seen["sites"] = sites
        return [{"site": "mcmaster", "ok": True}]

    monkeypatch.setattr(health, "run_health_check", fake_run)
    out = _call("vendors_health", vendor=["mcmaster"])
    assert out["ok"] is True
    assert seen["sites"] == ["mcmaster"]      # list -> CSV -> parsed site filter
    note = _notes(out)
    assert "as sites" in note and "CSV" in note


def test_scrape_enqueue_missing_arguments_name_the_login_flow():
    out = _call("scrape_enqueue", vendor="mcmaster")
    assert out["ok"] is False
    assert "query" in out["reason"]
    assert "vendor_login" in out["hint"]


def test_vendor_login_without_a_site_lists_real_keys():
    out = _call("vendor_login")
    assert out["ok"] is False
    assert "mcmaster" in out["hint"]


def test_browser_profiles_accepts_action_and_site_aliases_before_validation():
    out = _call("browser_profiles", operation="delete", vendor=".")
    assert out["ok"] is False
    assert out["reason"] == "invalid_site"


def test_browser_profiles_is_reachable_through_sourcing_pack():
    from services.agent.tool_categories import resolve_categories

    assert "browser_profiles" in resolve_categories(["sourcing"])


# --------------------------------------------------------------------------
# Both dispatch surfaces stay intact
# --------------------------------------------------------------------------

def test_every_widened_tool_bypasses_the_reconciler():
    """var_kw=True is the mechanic that lets a tool own its own input dialect."""
    for tool in WIDENED_TOOLS:
        fn = at._bind_tool(tool)
        assert fn is not None, tool
        assert at._signature_spec(fn)["var_kw"] is True, tool


def _fastmcp_tools():
    from mcp.server.fastmcp import FastMCP

    from services.mcp.parts_tools import register_parts_tools

    mcp = FastMCP("parts-dialect-test")
    register_parts_tools(mcp)
    return mcp._tool_manager


def test_var_kw_did_not_become_a_required_fastmcp_field():
    """A bare **kwargs makes FastMCP demand a field literally named "kwargs"."""
    mgr = _fastmcp_tools()
    for tool in WIDENED_TOOLS:
        required = mgr.get_tool(tool).parameters.get("required") or []
        assert "kwargs" not in required, f"{tool} advertises a bogus 'kwargs' arg"


def test_canonical_calls_still_work_through_fastmcp(monkeypatch):
    """The path _bind_tool bypasses — and therefore the one that breaks silently."""
    mgr = _fastmcp_tools()

    search = asyncio.run(mgr.get_tool("part_search").run({"query": "M8 socket head"}))
    assert json.loads(_text(search))["ok"] is True

    import services.mcp.core_bridge as bridge
    monkeypatch.setattr(bridge, "call",
                        lambda method, params=None: {"ok": True, "domain": "mech",
                                                     "doc_id": "doc-test",
                                                     "editable": True})
    place = asyncio.run(mgr.get_tool("part_place").run({"part_id": M8}))
    assert json.loads(_text(place))["ok"] is True


def _text(result) -> str:
    """FastMCP Tool.run returns the raw value or a (unstructured, structured) pair."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list) and result:
        return getattr(result[0], "text", str(result[0]))
    return result if isinstance(result, str) else str(result)
