"""test_router_dispatch.py — workflow_router structure=="linear" wiring (harness overhaul).

Proves ``services.agent.mission_dispatch.route_request`` (the live caller that
resolves ``workflow_router.route()``'s ``structure``/``role`` decision):

  * a PROJECT_ADMIN-classified request (file-org/cleanup/PLM) runs ONE
    ``agent_turn.run_turn`` against the ``project_organizer`` role card — no
    manifest, no ``services.dag.mission_template.build_mission_graph`` call,
    no planner/modeler DAG nodes — and advertises fs/plm tools (never CAD
    geometry mutators).
  * a geometry-build request stays on the unchanged ``dag`` structure:
    ``route_request`` never runs a linear turn for it, and
    ``build_mission_graph`` still compiles the classic
    intake_analyst -> .../feature_modeler|assembly_fit_engineer -> reviewer
    shape for it (regression check — this wave does not touch the compiler).

Discipline: scripted providers only (T-mega24-style), no model/network/port.
Run: pytest services/agent/test_router_dispatch.py -q
"""
from __future__ import annotations

from services.agent.mission_dispatch import (
    LINEAR_ROLE_MAX_TOOL_CALLS,
    route_request,
    run_linear_role_turn,
)


class _ScriptedProvider:
    """Returns a fixed sequence of replies; ignores the prompt (mirrors
    test_agent_turn.py's _ScriptedProvider)."""
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


class _LoopingProvider:
    """Always proposes the same tool call — never finishes on its own, so a
    turn against it can only stop via max_tool_calls (proves the budget is
    actually enforced, not just accepted as a parameter)."""
    name = "looping"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```'


_CLEANUP_REQUEST = (
    "clean up my project files and put them into PLM, archive deprecated folders"
)
_BUILD_REQUEST = "design a two-part hinge assembly with clearance fit"

_ORGANIZER_HANDOFF = (
    '```json\n{"done": true, "handoff": {"status": "done", '
    '"summary": "organized the project", "outputs": {'
    '"actions_taken": ["archived /deprecated_v1 to /archive/deprecated_v1"], '
    '"plm_registered": true, "reasoning_summary": "cleaned up and registered"}}}\n```'
)


class TestRouteRequestLinear:
    """PROJECT_ADMIN / cleanup requests -> linear project_organizer turn."""

    def test_cleanup_request_routes_linear_project_organizer(self):
        dispatched: list[str] = []

        def _dispatch(tool, args):
            dispatched.append(tool)
            return {"ok": True}

        provider = _ScriptedProvider([
            '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
            '```json\n{"tool": "plm_checkpoint", "args": {"message": "cleanup"}}\n```',
            _ORGANIZER_HANDOFF,
        ])
        events: list[tuple[str, dict]] = []

        out = route_request(
            _CLEANUP_REQUEST, "local-24k", provider,
            on_event=lambda kind, payload: events.append((kind, payload)),
            tool_dispatch=_dispatch,
        )

        assert out["ok"] is True
        assert out["structure"] == "linear"
        assert out["task_class"] == "project_admin"
        assert out["role"] == "project_organizer"
        # No DAG/swarm ever compiled for this request: no build_mission_graph
        # call happened (nothing in this test even imports it) and no
        # planner/modeler role name appears anywhere in the outcome.
        assert "feature_modeler" not in str(out)
        assert "spec_writer" not in str(out)

        turn = out["turn"]
        assert turn["success"] is True
        assert turn["role"] == "project_organizer"
        assert dispatched == ["fs_list", "plm_checkpoint"]

        # fs/plm tools advertised (the actual T- fix): the role's own
        # allowlist plus the shared subagent file-handling floor.
        allowed = turn["allowed_tools"]
        for t in ("fs_write", "fs_edit", "run_shell", "plm_init", "plm_open",
                  "plm_item_add", "plm_checkpoint", "plm_history"):
            assert t in allowed, f"{t} missing from project_organizer turn tools"
        for t in ("fs_read", "fs_glob", "fs_grep"):  # SUBAGENT_DEFAULT_TOOLS floor
            assert t in allowed, f"{t} (subagent floor) missing"
        # Never CAD geometry mutators — the exact bug this fixes.
        for t in ("new_part", "add_features", "asm_add_component", "asm_mate"):
            assert t not in allowed, f"{t} must never be offered to project_organizer"

        # agent.classification fired once, before dispatch, mirroring the
        # C++ live path's Routing card (contracts/agent_events.md).
        kinds = [k for k, _ in events]
        assert kinds.count("agent.classification") == 1
        _, payload = events[0]
        assert payload["task_class"] == "project_admin"

    def test_linear_turn_max_tool_calls_generous_for_multi_file_cleanup(self):
        """A DAG BUILD node's floor is 8 (mega-6 BUILD mortality fix,
        mission_dispatch._run_subagent). A linear organizer turn handles the
        WHOLE project (many files) in one turn, so its budget must clear
        that floor by a wide margin."""
        assert LINEAR_ROLE_MAX_TOOL_CALLS > 12

        provider = _LoopingProvider()
        turn = run_linear_role_turn(
            _CLEANUP_REQUEST, "project_organizer", provider,
            tool_dispatch=lambda t, a: {"ok": True},
        )
        assert turn["stop_reason"] == "max_iters"
        assert turn["result"]["tool_calls"] == LINEAR_ROLE_MAX_TOOL_CALLS


class TestRouteRequestDag:
    """Geometry-build requests keep the unchanged DAG/swarm structure."""

    def test_build_request_stays_on_dag_structure_no_linear_turn(self):
        provider = _ScriptedProvider([])  # must never be consulted

        def _dispatch(tool, args):
            raise AssertionError(
                "a build request must never run the linear organizer turn")

        out = route_request(
            _BUILD_REQUEST, "local-24k", provider,
            tool_dispatch=_dispatch,
        )
        assert out["ok"] is True
        assert out["structure"] == "dag"
        assert out["task_class"] == "assembly"
        assert "role" not in out or out.get("role") in (None,)
        assert provider.calls == 0  # run_turn was never invoked

    def test_build_mission_graph_still_compiles_unchanged_dag(self):
        """Regression check: this wave routes AROUND build_mission_graph for
        linear classes — it never touches the compiler itself. A build
        request still gets the classic intake -> BUILD -> VERIFY shape."""
        from services.dag.mission_template import build_mission_graph

        manifest = {
            "request_verbatim": _BUILD_REQUEST,
            "ambiguities": [],
            "deliverables": [{
                "id": "d1", "name": "Hinge assembly", "kind": "assembly",
                "description": "Two-part hinge with a clearance fit.",
                "acceptance_criteria": [],
            }],
        }
        graph = build_mission_graph(manifest, graph_id="t_router_dispatch_dag")
        roles = {n["inputs"].get("role") for n in graph["nodes"]}
        assert "intake_analyst" in roles
        assert "assembly_fit_engineer" in roles or "feature_modeler" in roles
        assert "reviewer" in roles
