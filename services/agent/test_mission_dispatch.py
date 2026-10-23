"""Tests for mission_dispatch.py + the agent_turn dispatch order.

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
    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
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
        out = md.ledger_note(
            kind="decision",
            payload={
                "id": "D1",
                "stage": "spec",
                "text": "use 6061",
                "rationale": "cheap",
            },
        )
        assert out["ok"] is True and out["run_id"] == "t_w32_run"
        got = md.ledger_get(section="decisions")
        assert got["ok"] is True
        assert any(d.get("id") == "D1" for d in got["data"])

    def test_memory_learn_off_keeps_ledger_but_skips_project_memory(
        self, md, monkeypatch, tmp_path
    ):
        """The learning switch governs every implicit memory write, not only autolearn."""
        remembered = []
        monkeypatch.setattr("services.agent.config.memory_enabled", lambda: True)
        monkeypatch.setattr("services.agent.config.memory_learn_enabled", lambda: False)
        monkeypatch.setattr(
            "services.agent.project_memory.resolve_project_root",
            lambda _root: tmp_path,
        )
        monkeypatch.setattr(
            "services.agent.project_memory.remember",
            lambda *args, **kwargs: remembered.append((args, kwargs)),
        )

        out = md.ledger_note(
            kind="decision",
            payload={
                "id": "D_NO_LEARN",
                "stage": "verify",
                "text": "do not retain this implicit note",
                "rationale": "learning is disabled",
            },
        )

        assert out["ok"] is True, "memory gating must never block the design ledger"
        assert remembered == [], (
            "memory_learn=false promised that no new fact is written"
        )
        assert any(
            d.get("id") == "D_NO_LEARN"
            for d in md.ledger_get(section="decisions")["data"]
        ), (
            "the source design ledger remains authoritative and must still receive the note"
        )

    def test_memory_learn_on_mirrors_durable_note(self, md, monkeypatch, tmp_path):
        """Positive control: both memory gates on retains the existing mirror behavior."""
        remembered = []
        monkeypatch.setattr("services.agent.config.memory_enabled", lambda: True)
        monkeypatch.setattr("services.agent.config.memory_learn_enabled", lambda: True)
        monkeypatch.setattr(
            "services.agent.project_memory.resolve_project_root",
            lambda _root: tmp_path,
        )
        monkeypatch.setattr(
            "services.agent.project_memory.remember",
            lambda *args, **kwargs: remembered.append((args, kwargs)),
        )

        out = md.ledger_note(kind="note", payload={"text": "retain this note"})

        assert out["ok"] is True
        assert len(remembered) == 1
        args, kwargs = remembered[0]
        assert args == (tmp_path, "retain this note")
        assert kwargs["kind"] == "note"
        assert kwargs["source"] == "ledger:agent"

    def test_get_full_render_within_budget(self, md):
        md.ledger_note(
            kind="decision",
            payload={"id": "D1", "stage": "s", "text": "t", "rationale": "r"},
        )
        got = md.ledger_get(budget_tokens=200)
        assert got["ok"] is True and "rendered" in got

    def test_get_unknown_section_is_error_data(self, md):
        got = md.ledger_get(section="nonsense")
        assert got["ok"] is False and "allowed" in got

    def test_section_all_aliases_full_render(self, md):
        """mega-2: models pass section='all' meaning full ledger render."""
        md.ledger_note(
            kind="decision",
            payload={"id": "D1", "stage": "s", "text": "t", "rationale": "r"},
        )
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
            "intake",
            "manifest",
            "manifest",
            "",
            "intake",
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
            payload={
                "name": "fc_process",
                "value": "pcb",
                "driver": "x",
                "provenance": "y",
            },
        )
        assert r1["ok"] is True, r1
        r2 = md.ledger_note(
            kind="decision",
            payload={
                "name": "fc_material",
                "value": "FR4",
                "driver": "x",
                "provenance": "y",
            },
        )
        assert r2["ok"] is True, r2
        r3 = md.ledger_note(
            kind="decision",
            payload={
                "id": "fc_3v3",
                "name": "fc_3v3",
                "value": "buck",
                "driver": "x",
                "provenance": "y",
            },
        )
        assert r3["ok"] is True, r3
        got = md.ledger_get(section="decisions")
        assert got["ok"] and len(got["data"]) >= 3

    def test_dimension_without_id_after_named_write(self, md):
        """mega-2: second dimension after name-only first must not KeyError."""
        r1 = md.ledger_note(
            kind="dimension",
            payload={
                "name": "bore",
                "value": 20,
                "unit": "mm",
                "driver": "x",
                "provenance": "fit",
            },
        )
        assert r1["ok"] is True, r1
        r2 = md.ledger_note(
            kind="dimension",
            payload={
                "name": "od",
                "value": 60,
                "unit": "mm",
                "driver": "x",
                "provenance": "calc",
            },
        )
        assert r2["ok"] is True, r2

    def test_dimension_requires_provenance(self, md):
        out = md.ledger_note(
            kind="dimension", payload={"id": "DIM1", "name": "bore", "value": 10}
        )
        assert out["ok"] is False and "provenance" in out["error"]

    def test_geometry_spec_requires_provenance(self, md):
        # geometry_spec is the geometry-side twin of the dimension rule —
        # the mission-dispatch back door enforces provenance too.
        out = md.ledger_note(
            kind="geometry_spec", payload={"id": "G1", "realizes": ["FT1"]}
        )
        assert out["ok"] is False and "provenance" in out["error"]
        ok = md.ledger_note(
            kind="geometry_spec",
            payload={"id": "G1", "realizes": ["FT1"], "provenance": "fit_calc"},
        )
        assert ok["ok"] is True

    def test_dp_kinds_accepted_and_readable(self, md):
        # The four design-pipeline kinds write and read back through dispatch.
        assert md.ledger_note(
            kind="feature",
            payload={"id": "FT1", "function": "mount", "driving_requirement": "R1"},
        )["ok"]
        assert md.ledger_note(kind="linkage", payload={"id": "LINK_MAIN"})["ok"]
        assert md.ledger_note(
            kind="build_node", payload={"id": "BN1", "op": "extrude"}
        )["ok"]
        got = md.ledger_get(section="features")
        assert got["ok"] is True
        assert any(f.get("id") == "FT1" for f in got["data"])

    def test_foreign_run_id_is_scoped_to_own_run(self, md):
        out = md.ledger_note(
            kind="note", payload={"text": "hi"}, run_id="someone_elses_run"
        )
        assert out["ok"] is True and out["run_id"] == "t_w32_run"

    def test_unbound_ledger_is_lazily_created_for_the_run(self, cache_root):
        """An advertised tool must not fail 100% of the time: a dispatch built
        without a ledger (the general_agent linear entry) binds the run's own
        on-disk ledger on first use, and the same object is reused."""
        bare = MissionDispatch(run_id="r")
        assert bare.ledger is None
        assert bare.ledger_note(kind="note", payload={"text": "hi"})["ok"] is True
        bound = bare.ledger
        assert bound is not None and bound.run_id == "r"
        got = bare.ledger_get()
        assert got["ok"] is True and got["run_id"] == "r"
        assert bare.ledger is bound

    def test_no_run_id_is_still_a_structured_error(self):
        """Nothing to key a ledger on — the structured error stands."""
        bare = MissionDispatch(run_id="")
        assert bare.ledger_get()["ok"] is False
        assert bare.ledger_note(kind="note", payload={})["ok"] is False


# ---------------------------------------------------------------------------
# ask_user — autonomous: record + default, never a fabricated human
# ---------------------------------------------------------------------------


class TestAskUser:
    def test_suggested_default_applied_and_recorded(self, md, ledger):
        out = md.ask_user(
            questions=[{"id": "q1", "prompt": "shaft material?", "suggested": "steel"}]
        )
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
        qs = _normalize_questions(
            [
                {
                    "id": "mat",
                    "type": "design_choice",
                    "prompt": "Material?",
                    "choices": ["aluminum", "steel"],
                    "default": "aluminum",
                }
            ]
        )
        assert len(qs) == 1
        assert qs[0]["kind"] == "choice"
        assert qs[0]["options"][0]["value"] == "aluminum"
        assert qs[0]["suggested"] == "aluminum"

    def test_normalize_multi_choice(self):
        qs = _normalize_questions(
            [
                {
                    "prompt": "Keep?",
                    "kind": "choice",
                    "multi": True,
                    "options": [{"value": "a"}, {"value": "b"}],
                    "suggested": "a",
                }
            ]
        )
        assert qs[0]["suggested"] == ["a"]

    def test_normalize_geometry_select(self):
        qs = _normalize_questions(
            [
                {
                    "prompt": "Which face?",
                    "kind": "geometry",
                    "targets": ["face"],
                }
            ]
        )
        assert qs[0]["kind"] == "geometry_select"
        assert qs[0]["suggested"] == {}
        assert qs[0]["targets"] == ["face"]

    def test_normalize_approval_defaults_false(self):
        qs = _normalize_questions([{"prompt": "Ship it?", "kind": "approval"}])
        assert qs[0]["suggested"] is False

    def test_normalize_question_key_alias(self):
        """Live regression (run-73073258489640, claude-sonnet-5): every item put
        its text under ``question``; the batch normalized to zero prompts and the
        model was told it "got no question text"."""
        qs = _normalize_questions(
            [
                {
                    "id": "purpose",
                    "question": "New standalone mud bucket part?",
                    "type": "choice",
                    "options": ["Yes", "No"],
                    "suggested": "Yes",
                },
                {"id": "dims", "question": "What size?"},
            ]
        )
        assert [q["id"] for q in qs] == ["purpose", "dims"]
        assert qs[0]["prompt"].startswith("New standalone")
        assert qs[0]["kind"] == "choice"

    @pytest.mark.parametrize(
        "key",
        [
            "question",
            "text",
            "ask",
            "label",
            "title",
            "message",
            "body",
            "content",
            "description",
            "q",
        ],
    )
    def test_normalize_accepts_every_prompt_alias(self, key):
        qs = _normalize_questions([{key: "Which material?"}])
        assert len(qs) == 1 and qs[0]["prompt"] == "Which material?"

    def test_normalize_id_to_prompt_mapping(self):
        qs = _normalize_questions({"purpose": "New part?", "dims": "How big?"})
        assert [(q["id"], q["prompt"]) for q in qs] == [
            ("purpose", "New part?"),
            ("dims", "How big?"),
        ]

    def test_normalize_single_dict_question_is_not_a_mapping(self):
        qs = _normalize_questions({"id": "q1", "prompt": "Material?"})
        assert len(qs) == 1 and qs[0]["id"] == "q1"

    def test_ask_user_accepts_question_key_end_to_end(self, md):
        out = md.ask_user(
            questions=[
                {"id": "mat", "question": "Material?", "suggested": "AL_6061_T6"}
            ]
        )
        assert out["ok"] is True
        assert out["answers"]["mat"]["value"] == "AL_6061_T6"


# ---------------------------------------------------------------------------
# delegate / dag_plan / part_search
# ---------------------------------------------------------------------------


class TestDelegate:
    def test_tree_construction_failure_is_structured_and_recorded(
        self, md, ledger, monkeypatch
    ):
        import services.agent.delegation as delegation

        def fail_tree(**_kwargs):
            raise RuntimeError("tree unavailable")

        monkeypatch.setattr(delegation, "DelegationTree", fail_tree)
        out = md.delegate(role="sketcher", brief="draw it", budget_tokens=500)
        assert out["ok"] is False and out["status"] == "unavailable"
        evs = [
            event
            for event in ledger.events
            if (event.get("payload") or {}).get("event") == "delegation_request"
        ]
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
        graph = {
            "id": "g1",
            "name": "mission",
            "edges": [],
            "nodes": [
                {
                    "id": "n1",
                    "type": "design",
                    "name": "Build",
                    "inputs": {"role": "feature_modeler"},
                }
            ],
        }
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
        import services.parts as hub

        monkeypatch.setattr(
            hub,
            "search",
            lambda q, domain=None, limit=20, **kw: [
                {
                    "part_id": "fastener:x:M3",
                    "title": "M3 screw",
                    "designation": "M3",
                    "params": {},
                }
            ],
        )
        out = md.part_search("M3")
        assert out["ok"] is True and out["count"] == 1

    def test_v2_kwargs_forwarded(self, md, monkeypatch):
        seen = {}

        def _v2(query, **kw):
            seen.update(kw, query=query)
            return {"ok": True, "count": 0, "results": []}

        from services.mcp import parts_tools as pt

        monkeypatch.setattr(pt, "search_parts_v2", _v2)
        out = md.part_search(
            "bearing",
            category_id="bearings_motion",
            facets={"bore_mm": 12},
            price_max=9.0,
            vendors="mcmaster",
            in_stock=True,
        )
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
        for tool in (
            "parts_taxonomy",
            "part_get",
            "part_source",
            "vendors_health",
            "scrape_enqueue",
            "scrape_queue_status",
            "import_artifact",
            "cam_tool_import",
        ):
            assert md.resolve(tool) is not None, tool

    def test_parts_taxonomy_roots(self, md):
        out = md.parts_taxonomy()
        assert out["ok"] is True
        assert any(c["id"] == "fasteners" for c in out["children"])

    def test_part_get(self, md, monkeypatch):
        import services.parts as hub

        monkeypatch.setattr(
            hub, "get", lambda pid: {"part_id": pid} if pid == "x:1" else None
        )
        assert md.part_get("x:1")["ok"] is True
        assert md.part_get("x:2")["ok"] is False
        assert md.part_get("")["ok"] is False

    def test_part_source_delegates_to_impl(self, md, monkeypatch):
        from services.mcp import parts_tools as pt

        monkeypatch.setattr(
            pt,
            "part_source_impl",
            lambda need, constraints, limit: {
                "ok": True,
                "candidates": [],
                "need": need,
            },
        )
        out = md.part_source(need="bearing 12mm bore")
        assert out["ok"] is True and out["need"] == "bearing 12mm bore"

    def test_vendors_health_delegates(self, md, monkeypatch):
        from services.mcp import parts_tools as pt

        monkeypatch.setattr(
            pt,
            "vendors_health_impl",
            lambda sites: {"ok": True, "count": 0, "sites": []},
        )
        assert md.vendors_health()["ok"] is True

    def test_scrape_enqueue_writes_artifact_ledger_note(self, md, ledger, monkeypatch):
        from services.browser import queue as q

        monkeypatch.setattr(q, "ensure_worker_started", lambda: None)
        monkeypatch.setattr(
            q,
            "enqueue",
            lambda site, query, **kw: {"ok": True, "id": "job1", "status": "pending"},
        )
        out = md.scrape_enqueue(site="mcmaster", query="6205 bearing")
        assert out["ok"] is True and out["id"] == "job1"
        arts = [
            e
            for e in ledger.events
            if e.get("kind") == "artifact"
            and (e.get("payload") or {}).get("event") == "scrape_enqueued"
        ]
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
            [
                "parts_taxonomy",
                "part_search",
                "part_source",
                "cam_tool_import",
                "vendors_health",
            ],
            resolver=agent_turn.resolver_for(md),
        )
        assert meta["unresolved"] == []
        assert "part_source(need: str = ''" in text
        assert "category_id" in text  # upgraded part_search kwargs
        assert "cam_tool_import(part_id: str = ''" in text
        assert "parts_taxonomy(node_id: str = ''" in text


# ---------------------------------------------------------------------------
# agent_turn dispatch order — mission handlers use unified policy dispatch
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
    def test_mission_hit_uses_unified_dispatch_policy(self, md, monkeypatch):
        """Mission handlers still pass through the common options/guard path."""
        seen = {}

        def _unified(tool, args, **kw):
            seen.update(
                tool=tool,
                args=args,
                inner=kw.get("_inner"),
                call_counts=kw.get("_call_counts"),
                loop_guard=kw.get("_loop_guard"),
            )
            return kw["_inner"](tool, args)

        monkeypatch.setattr(agent_turn, "_dispatch_tool", _unified)

        res = agent_turn.run_turn(
            "note a decision",
            _ScriptedProvider(
                "ledger_note", {"kind": "note", "payload": {"text": "hi"}}
            ),
            allowed_tools=["ledger_note"],
            mission_dispatch=md,
        )
        assert res.success is True
        call = res.result["calls"][0]
        assert call["ok"] is True and call["tool"] == "ledger_note"
        assert seen["tool"] == "ledger_note"
        assert callable(seen["inner"])
        assert isinstance(seen["call_counts"], dict)

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
            "measure",
            _ScriptedProvider("measure_part", {}),
            allowed_tools=["measure_part"],
            mission_dispatch=md,
        )
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
            "go",
            _CaptureProvider(),
            allowed_tools=["ledger_note"],
            mission_dispatch=md,
            tools_text="[TOOLS] Call tools with EXACTLY these kwargs:\n- ledger_note(kind: str)",
        )
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
            seen.update(part_id=part_id, title=title, mpn=mpn, brand=brand, limit=limit)
            return {
                "ok": True,
                "matches": [
                    {"confidence": "exact", "attach": True},
                    {"confidence": "weak", "attach": False},
                ],
                "best": None,
            }

        monkeypatch.setattr(pt, "match_model_impl", _impl)
        out = md.match_model(mpn="1LNK-4B", brand="Acme", limit=5)
        assert out["ok"] is True
        assert seen == {
            "part_id": "",
            "title": "",
            "mpn": "1LNK-4B",
            "brand": "Acme",
            "limit": 5,
        }
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


# ---------------------------------------------------------------------------
# ledger vocabulary — shared section/kind resolution (services.agent.ledger_vocab)
#
# Measured: 27 `ledger_get unknown section` + 24 `ledger_note unknown kind`
# across 93 recorded bench runs.  Both checks already returned the accepted set,
# so the vocabulary — not the error shape — was the defect.
# ---------------------------------------------------------------------------


def _mcp_ledger_tools():
    """The MCP twins of ledger_get/ledger_note, registered on a fake FastMCP."""
    tools: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):  # noqa: ARG002
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    from services.mcp.ledger_tools import register_ledger_tools

    register_ledger_tools(_FakeMCP())
    return tools


class TestLedgerVocabulary:
    @pytest.mark.parametrize(
        "section,canonical",
        [
            # the measured failure: a path-like section from a design-stage model
            ("/project/mission-123/design/specs/", "geometry_specs"),
            ("ledger/decisions", "decisions"),
            # measured (bench-live C1): section="specifications"
            ("specifications", "geometry_specs"),
            ("state.artifacts", "artifacts"),
            # decoration / case / separator thrash
            ("  Design Specs  ", "geometry_specs"),
            ("'stage-log'", "stage_log"),
            ("Build Nodes", "build_nodes"),
            ("ledger_decisions", "decisions"),
            ("requirements_list", "requirements"),
            # singular⇄plural
            ("decision", "decisions"),
            ("question", "questions"),
            ("criterion_results", "criterion_results"),
            # widened synonyms
            ("deliverables", "artifacts"),
            ("open_questions", "questions"),
            ("constraints", "requirements"),
            ("objective", "brief"),
            # measured: a qualifier bolted onto a real word (head is never used
            # alone — "design_specifications" is specs, not the whole ledger)
            ("design_specifications", "geometry_specs"),
            ("mission_specifications", "geometry_specs"),
        ],
    )
    def test_section_spellings_resolve(self, md, section, canonical):
        got = md.ledger_get(section=section)
        assert got["ok"] is True, got
        assert got["section"] == canonical

    @pytest.mark.parametrize(
        "section",
        [
            "/runs/x/ALL/",
            "Full Ledger",
            "everything",
            "*",
            # measured (bench-live C1): "design" is not one section — render it all
            # rather than guess wrong.
            "design",
        ],
    )
    def test_whole_ledger_spellings_render(self, md, section):
        got = md.ledger_get(section=section)
        assert got["ok"] is True and "rendered" in got

    @pytest.mark.parametrize(
        "kind,canonical",
        [
            # the measured failure
            ("design_spec", "decision"),
            ("Design Spec", "decision"),
            ("specifications", "decision"),
            ("requirements", "requirement"),
            ("open_question", "question"),
            ("milestone", "stage"),
            ("observation", "note"),
            ("test_result", "criterion_result"),
            ("deliverable", "artifact"),
            # measured (bench ledgers): audit_findings / verify_result / error
            ("audit_findings", "note"),
            ("verify_result", "artifact"),
            ("error", "note"),
        ],
    )
    def test_kind_spellings_resolve(self, md, kind, canonical):
        out = md.ledger_note(kind=kind, payload={"id": "K1", "text": "t"})
        assert out["ok"] is True, out
        assert out["kind"] == canonical
        assert out.get("aliased_from") == kind.strip().lower()

    def test_nonsense_section_still_errors_with_teaching_payload(self, md):
        got = md.ledger_get(section="florble_gorb")
        assert got["ok"] is False
        # pre-existing keys survive (consumers read them) …
        assert "decisions" in got["allowed"] and "all" in got["allowed"]
        assert got["aliases"]["interfaces"] == "dimensions"
        # … and the new fuzzy suggestion list is present.
        assert "did_you_mean" in got and isinstance(got["did_you_mean"], list)
        assert "hint" in got

    def test_deliverable_id_as_section_is_named_in_the_hint(self, md):
        """Measured: models pass a deliverable id ("DL1") where a section goes."""
        got = md.ledger_get(section="DL1")
        assert got["ok"] is False
        assert "deliverable" in got["hint"] and "allowed" in got["hint"]

    def test_near_miss_section_suggests_the_real_one(self, md):
        got = md.ledger_get(section="decisons")
        assert got["ok"] is False
        assert "decisions" in got["did_you_mean"]
        assert "decisions" in got["hint"]

    def test_nonsense_kind_still_errors_with_teaching_payload(self, md):
        out = md.ledger_note(kind="florble_gorb", payload={"text": "x"})
        assert out["ok"] is False
        assert "decision" in out["valid_kinds"] and "verdict" in out["valid_kinds"]
        assert out["aliased_to"]["verdict"] == "decision"
        assert isinstance(out["did_you_mean"], list) and "hint" in out

    def test_near_miss_kind_suggests_the_real_one(self, md):
        out = md.ledger_note(kind="dimensionn", payload={"text": "x"})
        assert out["ok"] is False
        assert "dimension" in out["did_you_mean"]

    def test_no_geometry_spec_write_without_provenance(self, md):
        """Widening the vocabulary must not slip past a real check."""
        out = md.ledger_note(kind="geo_spec", payload={"id": "G1"})
        assert out["ok"] is False and "provenance" in out["error"]

    def test_tables_are_the_shared_ones(self):
        """mission_dispatch and ledger_vocab cannot drift — same objects."""
        from services.agent import ledger_vocab as lv
        from services.agent import mission_dispatch as mdisp

        assert mdisp._LEDGER_NOTE_KIND_ALIASES is lv.KIND_ALIASES
        assert mdisp._LEDGER_NOTE_KINDS is lv.LEDGER_NOTE_KINDS
        assert mdisp._LEDGER_GET_SECTION_ALIASES is lv.SECTION_ALIASES
        # every alias target is a real section / kind (no invented vocabulary)
        assert set(lv.SECTION_ALIASES.values()) <= lv.LEDGER_SECTIONS | {"all"}
        assert set(lv.KIND_ALIASES.values()) <= lv.LEDGER_NOTE_KINDS

    @pytest.mark.parametrize(
        "section",
        [
            "/project/mission-123/design/specs/",
            "Decisions",
            "bom",
            "events",
            "florble_gorb",
            "nonsense",
        ],
    )
    def test_mcp_twin_resolves_sections_identically(self, md, section, cache_root):
        mine = md.ledger_get(section=section)
        theirs = _mcp_ledger_tools()["ledger_get"](
            run_id="vocab_parity", section=section
        )
        assert mine["ok"] == theirs["ok"], (mine, theirs)
        assert mine.get("section") == theirs.get("section")
        if not mine["ok"]:
            assert mine["did_you_mean"] == theirs["did_you_mean"]
            assert mine["allowed"] == theirs["allowed"]

    @pytest.mark.parametrize("kind", ["design_spec", "verdict", "florble_gorb"])
    def test_mcp_twin_resolves_kinds_identically(self, md, kind, cache_root):
        payload = {"id": "P1", "stage": "spec", "text": "t", "rationale": "r"}
        mine = md.ledger_note(kind=kind, payload=dict(payload))
        theirs = _mcp_ledger_tools()["ledger_note"](
            run_id="vocab_parity_kind", kind=kind, payload=dict(payload)
        )
        assert mine["ok"] == theirs["ok"], (mine, theirs)
        assert mine.get("kind") == theirs.get("kind")
        if not mine["ok"]:
            assert mine["did_you_mean"] == theirs["did_you_mean"]
            assert mine["valid_kinds"] == theirs["valid_kinds"]
