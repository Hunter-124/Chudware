"""test_router_dispatch.py — model-driven routing (harness overhaul).

Proves ``services.agent.mission_dispatch.route_request`` after the deterministic
structure gate was removed:

  * EVERY request — file-org/cleanup/PLM *and* geometry-build — runs ONE
    ``agent_turn.run_turn`` against the ``general_agent`` role card. There is no
    keyword/regex classifier deciding dag-vs-linear anymore; ``general_agent`` is
    the universal entry and decides for itself whether to finish inline,
    ``delegate`` a scoped build, or ``mission_start`` the full build swarm.
  * ``workflow_router.route`` still runs, but only to emit the
    ``agent.classification`` telemetry the GUI Routing card renders — its
    ``task_class`` is advisory; the executed ``role`` is always general_agent.
  * The build swarm compiler (``build_mission_graph``) is untouched and still
    compiles the classic intake -> BUILD -> VERIFY shape when the model escalates.

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
    the mega24 offline scripted provider discipline)."""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


class _LoopingProvider:
    """Always proposes the same tool call — never finishes on its own, so a
    turn runs to its max_tool_calls budget."""

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


class TestRouteRequestGeneralAgent:
    """Every request runs the linear general_agent lane — the model decides."""

    def test_cleanup_request_runs_general_agent(self):
        dispatched: list[str] = []

        def _dispatch(tool, args):
            dispatched.append(tool)
            return {"ok": True}

        provider = _ScriptedProvider([
            '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
            '```json\n{"tool": "plm_checkpoint", "args": {"message": "cleanup"}}\n```',
            "Cleaned up the project and checkpointed it; nothing else to do.",
        ])
        events: list[tuple[str, dict]] = []

        out = route_request(
            _CLEANUP_REQUEST, "local-24k", provider,
            on_event=lambda kind, payload: events.append((kind, payload)),
            tool_dispatch=_dispatch,
        )

        assert out["ok"] is True
        assert out["structure"] == "linear"
        # Classification stays advisory (Routing card), role is always general.
        assert out["task_class"] == "project_admin"
        assert out["role"] == "general_agent"
        # No DAG/swarm auto-compiled: no planner/modeler role name in the outcome.
        assert "feature_modeler" not in str(out)
        assert "spec_writer" not in str(out)

        turn = out["turn"]
        assert turn["success"] is True
        assert turn["role"] == "general_agent"
        assert dispatched == ["fs_list", "plm_checkpoint"]

        # fs/plm floor advertised, plus the escalation levers the model uses to
        # reach the build swarm on its own decision.
        allowed = turn["allowed_tools"]
        for t in ("fs_write", "fs_edit", "run_shell", "plm_open", "plm_checkpoint"):
            assert t in allowed, f"{t} missing from general_agent turn tools"
        for t in ("fs_read", "fs_glob", "fs_grep"):  # SUBAGENT_DEFAULT_TOOLS floor
            assert t in allowed, f"{t} (subagent floor) missing"
        for t in ("delegate", "mission_start", "enable_tools"):
            assert t in allowed, f"{t} (escalation lever) missing"
        # Geometry mutators are NOT in the base loadout — the model pulls them
        # via enable_tools(['modeling']) only when it decides to build.
        for t in ("new_part", "add_features", "asm_add_component", "asm_mate"):
            assert t not in allowed, f"{t} must not be in the base general loadout"

        # agent.classification fired once, before dispatch (Routing card).
        kinds = [k for k, _ in events]
        assert kinds.count("agent.classification") == 1
        _, payload = events[0]
        assert payload["task_class"] == "project_admin"

    def test_build_request_also_runs_general_agent(self):
        """A geometry-build request is no longer force-routed into the swarm:
        it runs the general_agent turn, which decides whether/how to escalate."""
        dispatched: list[str] = []
        provider = _ScriptedProvider([
            "This needs the build swarm; escalating.",
        ])

        out = route_request(
            _BUILD_REQUEST, "local-24k", provider,
            tool_dispatch=lambda t, a: (dispatched.append(t) or {"ok": True}),
        )
        assert out["ok"] is True
        assert out["structure"] == "linear"
        assert out["role"] == "general_agent"
        assert out["task_class"] == "assembly"  # advisory classification only
        # The turn actually ran (model in the loop), not a deterministic bypass.
        assert provider.calls >= 1
        allowed = out["turn"]["allowed_tools"]
        for t in ("mission_start", "delegate", "enable_tools"):
            assert t in allowed, f"{t} (escalation lever) missing"

    def test_linear_turn_max_tool_calls_generous_for_multi_step_work(self):
        """A DAG BUILD node's floor is 8 (mega-6). A linear general turn handles
        the whole task in one turn, so its budget clears that floor widely."""
        assert LINEAR_ROLE_MAX_TOOL_CALLS > 12

        provider = _LoopingProvider()
        turn = run_linear_role_turn(
            _CLEANUP_REQUEST, "general_agent", provider,
            tool_dispatch=lambda t, a: {"ok": True},
        )
        assert turn["stop_reason"] == "max_iters"
        assert turn["result"]["tool_calls"] == LINEAR_ROLE_MAX_TOOL_CALLS


class TestBuildSwarmCompilerUnchanged:
    """The build swarm the model escalates to still compiles unchanged."""

    def test_build_mission_graph_still_compiles_classic_dag(self):
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
