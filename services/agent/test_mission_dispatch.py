"""Tests for mission_dispatch.py + the agent_turn dispatch order (T-W32).

DoD coverage: the run-scoped dispatcher resolves the six named tool families
(ledger_get/ledger_note, dag_plan, ask_user, delegate, part_search) with mission
context, and agent_turn consults it BEFORE the process-wide registry (mission hit
vs global fallback — both paths tested)."""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn
from services.agent.ledger import Ledger
from services.agent.mission_dispatch import MissionDispatch, _normalize_questions


@pytest.fixture()
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ledger(tmp_path):
    return Ledger("t_w32_run", path=tmp_path / "ledger.json")


@pytest.fixture()
def md(ledger, cache_root):
    return MissionDispatch(run_id="t_w32_run", ledger=ledger)


# ---------------------------------------------------------------------------
# resolution surface
# ---------------------------------------------------------------------------

class TestResolve:
    def test_resolves_all_six_families(self, md):
        for tool in MissionDispatch.TOOLS:
            assert md.resolve(tool) is not None, tool

    def test_unknown_tool_returns_none(self, md):
        assert md.resolve("add_features") is None
        assert md.resolve("no_such_tool") is None


# ---------------------------------------------------------------------------
# ledger tools — bound to the run's ledger handle
# ---------------------------------------------------------------------------

class TestLedgerTools:
    def test_note_then_get_roundtrip(self, md, ledger):
        out = md.ledger_note(kind="decision", payload={
            "id": "D1", "stage": "spec", "text": "use 6061", "rationale": "cheap"})
        assert out["ok"] is True and out["run_id"] == "t_w32_run"
        got = md.ledger_get(section="decisions")
        assert got["ok"] is True
        assert any(d.get("id") == "D1" for d in got["data"])

    def test_get_full_render_within_budget(self, md):
        md.ledger_note(kind="decision", payload={"id": "D1", "stage": "s",
                                                 "text": "t", "rationale": "r"})
        got = md.ledger_get(budget_tokens=200)
        assert got["ok"] is True and "rendered" in got

    def test_get_unknown_section_is_error_data(self, md):
        got = md.ledger_get(section="nonsense")
        assert got["ok"] is False and "allowed" in got

    def test_section_all_aliases_full_render(self, md):
        """mega-2: models pass section='all' meaning full ledger render."""
        md.ledger_note(kind="decision",
                       payload={"id": "D1", "stage": "s",
                                "text": "t", "rationale": "r"})
        got = md.ledger_get(section="all")
        assert got["ok"] is True and "rendered" in got
        got2 = md.ledger_get(section="*")
        assert got2["ok"] is True and "rendered" in got2

    def test_section_events_returns_event_log(self, md):
        """mega-2: section='events' must not be unknown."""
        md.ledger_note(kind="note", payload={"text": "hi"})
        got = md.ledger_get(section="events")
        assert got["ok"] is True
        assert isinstance(got["data"], list) and len(got["data"]) >= 1

    def test_section_manifest_returns_embedded(self, md, ledger):
        """mega-2: intake embeds full manifest under artifacts."""
        ledger.add_artifact(
            "intake", "manifest", "manifest", "", "intake",
            note="1 deliverable",
            manifest={"deliverables": [{"id": "DL1"}]},
        )
        got = md.ledger_get(section="manifest")
        assert got["ok"] is True
        assert got["data"]["deliverables"][0]["id"] == "DL1"

    def test_decision_without_id_synthesizes_from_name(self, md):
        """mega-2 defect 5: name-only decision must not KeyError on next write."""
        r1 = md.ledger_note(
            kind="decision",
            payload={"name": "fc_process", "value": "pcb",
                     "driver": "x", "provenance": "y"},
        )
        assert r1["ok"] is True, r1
        r2 = md.ledger_note(
            kind="decision",
            payload={"name": "fc_material", "value": "FR4",
                     "driver": "x", "provenance": "y"},
        )
        assert r2["ok"] is True, r2
        r3 = md.ledger_note(
            kind="decision",
            payload={"id": "fc_3v3", "name": "fc_3v3", "value": "buck",
                     "driver": "x", "provenance": "y"},
        )
        assert r3["ok"] is True, r3
        got = md.ledger_get(section="decisions")
        assert got["ok"] and len(got["data"]) >= 3

    def test_dimension_without_id_after_named_write(self, md):
        """mega-2: second dimension after name-only first must not KeyError."""
        r1 = md.ledger_note(
            kind="dimension",
            payload={"name": "bore", "value": 20, "unit": "mm",
                     "driver": "x", "provenance": "fit"},
        )
        assert r1["ok"] is True, r1
        r2 = md.ledger_note(
            kind="dimension",
            payload={"name": "od", "value": 60, "unit": "mm",
                     "driver": "x", "provenance": "calc"},
        )
        assert r2["ok"] is True, r2

    def test_dimension_requires_provenance(self, md):
        out = md.ledger_note(kind="dimension",
                             payload={"id": "DIM1", "name": "bore", "value": 10})
        assert out["ok"] is False and "provenance" in out["error"]

    def test_geometry_spec_requires_provenance(self, md):
        # T-DP1: geometry_spec is the geometry-side twin of the dimension rule —
        # the mission-dispatch back door enforces provenance too.
        out = md.ledger_note(kind="geometry_spec",
                             payload={"id": "G1", "realizes": ["FT1"]})
        assert out["ok"] is False and "provenance" in out["error"]
        ok = md.ledger_note(kind="geometry_spec",
                            payload={"id": "G1", "realizes": ["FT1"],
                                     "provenance": "fit_calc"})
        assert ok["ok"] is True

    def test_dp_kinds_accepted_and_readable(self, md):
        # T-DP1: the four design-pipeline kinds write and read back through dispatch.
        assert md.ledger_note(kind="feature",
                              payload={"id": "FT1", "function": "mount",
                                       "driving_requirement": "R1"})["ok"]
        assert md.ledger_note(kind="linkage", payload={"id": "LINK_MAIN"})["ok"]
        assert md.ledger_note(kind="build_node",
                              payload={"id": "BN1", "op": "extrude"})["ok"]
        got = md.ledger_get(section="features")
        assert got["ok"] is True
        assert any(f.get("id") == "FT1" for f in got["data"])

    def test_foreign_run_id_is_scoped_to_own_run(self, md):
        out = md.ledger_note(kind="note", payload={"text": "hi"},
                             run_id="someone_elses_run")
        assert out["ok"] is True and out["run_id"] == "t_w32_run"

    def test_no_ledger_is_structured_error(self, cache_root):
        bare = MissionDispatch(run_id="r")
        assert bare.ledger_get()["ok"] is False
        assert bare.ledger_note(kind="note", payload={})["ok"] is False


# ---------------------------------------------------------------------------
# ask_user — autonomous: record + default, never a fabricated human
# ---------------------------------------------------------------------------

class TestAskUser:
    def test_suggested_default_applied_and_recorded(self, md, ledger):
        out = md.ask_user(questions=[{"id": "q1", "prompt": "shaft material?",
                                      "suggested": "steel"}])
        assert out["ok"] is True
        ans = out["answers"]["q1"]
        assert ans["value"] == "steel" and ans["source"] in ("default", "resumed")
        # mirrored onto the mission design ledger for the run record
        qs = ledger.state.get("questions") or []
        assert any(q.get("id") == "q1" for q in qs)

    def test_plain_string_question_normalizes(self, md):
        out = md.ask_user(questions="what clearance class?")
        assert out["ok"] is True and len(out["answers"]) == 1

    def test_no_questions_is_error_data(self, md):
        out = md.ask_user()
        assert out["ok"] is False

    def test_normalize_caps_at_contract_four(self):
        qs = _normalize_questions([f"q {i}" for i in range(9)])
        assert len(qs) == 4
        assert all(q["suggested"] == "" and q["kind"] == "text" for q in qs)

    def test_normalize_design_choice_aliases(self):
        qs = _normalize_questions([{
            "id": "mat", "type": "design_choice", "prompt": "Material?",
            "choices": ["aluminum", "steel"], "default": "aluminum",
        }])
        assert len(qs) == 1
        assert qs[0]["kind"] == "choice"
        assert qs[0]["options"][0]["value"] == "aluminum"
        assert qs[0]["suggested"] == "aluminum"

    def test_normalize_multi_choice(self):
        qs = _normalize_questions([{
            "prompt": "Keep?", "kind": "choice", "multi": True,
            "options": [{"value": "a"}, {"value": "b"}],
            "suggested": "a",
        }])
        assert qs[0]["suggested"] == ["a"]

    def test_normalize_geometry_select(self):
        qs = _normalize_questions([{
            "prompt": "Which face?", "kind": "geometry",
            "targets": ["face"],
        }])
        assert qs[0]["kind"] == "geometry_select"
        assert qs[0]["suggested"] == {}
        assert qs[0]["targets"] == ["face"]

    def test_normalize_approval_defaults_false(self):
        qs = _normalize_questions([{"prompt": "Ship it?", "kind": "approval"}])
        assert qs[0]["suggested"] is False


# ---------------------------------------------------------------------------
# delegate / dag_plan / part_search
# ---------------------------------------------------------------------------

class TestDelegate:
    def test_no_tree_is_structured_refusal_and_recorded(self, md, ledger):
        out = md.delegate(role="sketcher", brief="draw it", budget_tokens=500)
        assert out["ok"] is False and out["status"] == "unavailable"
        evs = [e for e in ledger.events
               if (e.get("payload") or {}).get("event") == "delegation_request"]
        assert len(evs) == 1

    def test_with_tree_routes_to_delegation_layer(self, md):
        calls = {}

        class _Tree:
            pass

        md.delegation_tree = _Tree()

        import services.agent.delegation as dlg
        real = dlg.delegate

        def _spy(role, brief, budget, **kw):
            calls["role"] = role
            calls["tree"] = kw.get("tree")
            return {"id": "d1", "status": "pending"}
        dlg.delegate = _spy
        try:
            out = md.delegate(role="sketcher", brief="b", budget_tokens=100)
        finally:
            dlg.delegate = real
        assert out == {"id": "d1", "status": "pending"}
        assert calls["role"] == "sketcher" and calls["tree"] is md.delegation_tree


class TestDagPlan:
    def test_with_graph_returns_summary(self, cache_root):
        graph = {"id": "g1", "name": "mission", "edges": [],
                 "nodes": [{"id": "n1", "type": "design", "name": "Build",
                            "inputs": {"role": "feature_modeler"}}]}
        md = MissionDispatch(run_id="r", graph=graph)
        out = md.dag_plan()
        assert out["ok"] is True
        assert out["graph"]["nodes"][0]["role"] == "feature_modeler"
        assert "replan_request" in out["note"]

    def test_without_graph_falls_back_to_stage_log(self, md, ledger):
        ledger.log_stage("pipeline", "specify")
        out = md.dag_plan()
        assert out["ok"] is True and out.get("stage_log")


class TestPartSearch:
    def test_empty_query_is_error_data(self, md):
        assert md.part_search("")["ok"] is False

    def test_routes_to_parts_hub(self, md, monkeypatch):
        import engine_oracle.parts_hub as hub

        monkeypatch.setattr(hub, "search",
                            lambda q, domain=None, limit=20, **kw:
                            [{"part_id": "fastener:x:M3", "title": "M3 screw",
                              "designation": "M3", "params": {}}])
        out = md.part_search("M3")
        assert out["ok"] is True and out["count"] == 1

    def test_v2_kwargs_forwarded(self, md, monkeypatch):
        seen = {}

        def _v2(query, **kw):
            seen.update(kw, query=query)
            return {"ok": True, "count": 0, "results": []}
        from services.mcp import parts_tools as pt
        monkeypatch.setattr(pt, "search_parts_v2", _v2)
        out = md.part_search("bearing", category_id="bearings_motion",
                             facets={"bore_mm": 12}, price_max=9.0,
                             vendors="mcmaster", in_stock=True)
        assert out["ok"] is True
        assert seen["category_id"] == "bearings_motion"
        assert seen["facets"] == {"bore_mm": 12}
        assert seen["price_max"] == 9.0 and seen["vendors"] == "mcmaster"
        assert seen["in_stock"] is True


# ---------------------------------------------------------------------------
# Parts Library V2 handlers (workstream 5)
# ---------------------------------------------------------------------------

class TestPartsV2Handlers:
    def test_all_v2_tools_resolve(self, md):
        for tool in ("parts_taxonomy", "part_get", "part_source",
                     "vendors_health", "scrape_enqueue", "scrape_queue_status",
                     "import_artifact", "cam_tool_import"):
            assert md.resolve(tool) is not None, tool

    def test_parts_taxonomy_roots(self, md):
        out = md.parts_taxonomy()
        assert out["ok"] is True
        assert any(c["id"] == "fasteners" for c in out["children"])

    def test_part_get(self, md, monkeypatch):
        import engine_oracle.parts_hub as hub
        monkeypatch.setattr(hub, "get",
                            lambda pid: {"part_id": pid} if pid == "x:1" else None)
        assert md.part_get("x:1")["ok"] is True
        assert md.part_get("x:2")["ok"] is False
        assert md.part_get("")["ok"] is False

    def test_part_source_delegates_to_impl(self, md, monkeypatch):
        from services.mcp import parts_tools as pt
        monkeypatch.setattr(pt, "part_source_impl",
                            lambda need, constraints, limit:
                            {"ok": True, "candidates": [], "need": need})
        out = md.part_source(need="bearing 12mm bore")
        assert out["ok"] is True and out["need"] == "bearing 12mm bore"

    def test_vendors_health_delegates(self, md, monkeypatch):
        from services.mcp import parts_tools as pt
        monkeypatch.setattr(pt, "vendors_health_impl",
                            lambda sites: {"ok": True, "count": 0, "sites": []})
        assert md.vendors_health()["ok"] is True

    def test_scrape_enqueue_writes_artifact_ledger_note(self, md, ledger, monkeypatch):
        from services.browser import queue as q
        monkeypatch.setattr(q, "ensure_worker_started", lambda: None)
        monkeypatch.setattr(q, "enqueue",
                            lambda site, query, **kw:
                            {"ok": True, "id": "job1", "status": "pending"})
        out = md.scrape_enqueue(site="mcmaster", query="6205 bearing")
        assert out["ok"] is True and out["id"] == "job1"
        arts = [e for e in ledger.events if e.get("kind") == "artifact"
                and (e.get("payload") or {}).get("event") == "scrape_enqueued"]
        assert len(arts) == 1 and arts[0]["payload"]["job_id"] == "job1"

    def test_scrape_enqueue_requires_args(self, md):
        assert md.scrape_enqueue()["ok"] is False

    def test_scrape_queue_status_delegates(self, md, monkeypatch):
        from services.browser import queue as q
        monkeypatch.setattr(q, "queue_status", lambda: {"ok": True, "jobs": []})
        assert md.scrape_queue_status() == {"ok": True, "jobs": []}

    def test_import_artifact_missing_file(self, md, tmp_path):
        assert md.import_artifact("")["ok"] is False
        out = md.import_artifact(str(tmp_path / "nope.step"))
        assert out["ok"] is False and "not found" in out["error"]

    def test_cam_tool_import_delegates(self, md, monkeypatch):
        from services.mcp import parts_tools as pt
        seen = {}

        def _impl(part_id, number, material):
            seen.update(part_id=part_id, number=number, material=material)
            return {"ok": True, "tool_number": 3}
        monkeypatch.setattr(pt, "cam_tool_import_impl", _impl)
        out = md.cam_tool_import(part_id="catalog:x::u", number=0, material="hss")
        assert out["ok"] is True and seen["part_id"] == "catalog:x::u"

    def test_render_tools_block_yields_real_signatures(self, md):
        """The §9.1 tools slot renders REAL kwargs for the V2 wrappers via the
        same resolver run_turn dispatches with (schema = the mission wrapper)."""
        from services.agent.tool_schemas import render_tools_block
        text, meta = render_tools_block(
            ["parts_taxonomy", "part_search", "part_source", "cam_tool_import",
             "vendors_health"],
            resolver=agent_turn.resolver_for(md))
        assert meta["unresolved"] == []
        assert "part_source(need: str = ''" in text
        assert "category_id" in text          # upgraded part_search kwargs
        assert "cam_tool_import(part_id: str = ''" in text
        assert "parts_taxonomy(node_id: str = ''" in text


# ---------------------------------------------------------------------------
# agent_turn dispatch order — mission hit BEFORE global fallback
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """Emit one tool call, then finish."""
    def __init__(self, tool, args=None):
        self._replies = [
            json.dumps({"tool": tool, "args": args or {}}),
            json.dumps({"done": True, "handoff": {"summary": "ok"}}),
        ]

    def complete(self, system, messages, max_tokens=None):
        return f"```json\n{self._replies.pop(0)}\n```"


class TestDispatchOrder:
    def test_mission_hit_wins_over_global(self, md, monkeypatch):
        """ledger_note resolves mission-scoped: the global registry is never asked."""
        def _boom(tool, args):
            raise AssertionError("global dispatch must not be consulted")
        monkeypatch.setattr(agent_turn, "_dispatch_tool", _boom)

        res = agent_turn.run_turn(
            "note a decision", _ScriptedProvider(
                "ledger_note", {"kind": "note", "payload": {"text": "hi"}}),
            allowed_tools=["ledger_note"], mission_dispatch=md)
        assert res.success is True
        call = res.result["calls"][0]
        assert call["ok"] is True and call["tool"] == "ledger_note"

    def test_global_fallback_when_mission_misses(self, md, monkeypatch):
        """A non-mission tool falls through to the process-wide registry path."""
        seen = {}

        # The global fallback is invoked with run_turn's per-tool call counter
        # (agent_turn._dispatch_tool(tool, args, _call_counts=...)).
        def _global(tool, args, **_kw):
            seen["tool"] = tool
            return {"ok": True}
        monkeypatch.setattr(agent_turn, "_dispatch_tool", _global)

        res = agent_turn.run_turn(
            "measure", _ScriptedProvider("measure_part", {}),
            allowed_tools=["measure_part"], mission_dispatch=md)
        assert res.success is True and seen["tool"] == "measure_part"

    def test_resolver_for_mirrors_dispatch_order(self, md):
        resolve = agent_turn.resolver_for(md)
        # mission hit: the bound method, not the registry fn
        assert resolve("ledger_get").__self__ is md
        # global fallback for a real registry tool
        assert resolve("fit_calc") is not None
        # invented tool: honestly unresolvable
        assert resolve("design_part_magically") is None

    def test_tools_text_replaces_bare_name_list(self, md):
        """The §9.1 tools slot lands in the system prompt when provided."""
        captured = {}

        class _CaptureProvider:
            def complete(self, system, messages, max_tokens=None):
                captured["system"] = system
                return '```json\n{"done": true, "handoff": {}}\n```'

        agent_turn.run_turn(
            "go", _CaptureProvider(), allowed_tools=["ledger_note"],
            mission_dispatch=md,
            tools_text="[TOOLS] Call tools with EXACTLY these kwargs:\n- ledger_note(kind: str)")
        assert "EXACTLY these kwargs" in captured["system"]
        assert "Allowed tools:" not in captured["system"]


# ---------------------------------------------------------------------------
# match_model + reverse_engineer_product dispatch wrappers (contracts §2a/§2b)
# ---------------------------------------------------------------------------

class TestModelMatchDispatch:
    def test_both_tools_resolve(self, md):
        for tool in ("match_model", "reverse_engineer_product"):
            assert md.resolve(tool) is not None, tool
            assert tool in MissionDispatch.TOOLS

    def test_match_model_forwards_and_returns_envelope(self, md, monkeypatch):
        from services.mcp import parts_tools as pt
        seen = {}

        def _impl(part_id="", title="", mpn="", brand="", limit=8):
            seen.update(part_id=part_id, title=title, mpn=mpn, brand=brand,
                        limit=limit)
            return {"ok": True, "matches": [
                {"confidence": "exact", "attach": True},
                {"confidence": "weak", "attach": False}], "best": None}
        monkeypatch.setattr(pt, "match_model_impl", _impl)
        out = md.match_model(mpn="1LNK-4B", brand="Acme", limit=5)
        assert out["ok"] is True
        assert seen == {"part_id": "", "title": "", "mpn": "1LNK-4B",
                        "brand": "Acme", "limit": 5}
        attaching = [m for m in out["matches"] if m.get("attach")]
        assert len(attaching) == 1 and attaching[0]["confidence"] == "exact"

    def test_reverse_engineer_product_forwards(self, md, monkeypatch):
        from services.mcp import parts_tools as pt
        seen = {}

        def _impl(part_id="", url="", title="", mpn="", brand=""):
            seen.update(part_id=part_id, url=url)
            return {"ok": True, "session_id": "s1", "activeWorkspace": "ReverseEng"}
        monkeypatch.setattr(pt, "reverse_engineer_product_impl", _impl)
        out = md.reverse_engineer_product(url="https://a/x")
        assert out["ok"] is True and out["session_id"] == "s1"
        assert seen["url"] == "https://a/x"
