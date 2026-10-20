"""session.py — per-process session memory shared by the DAG tool family.

Holds two facts: which graph was last PLANNED, and which run was last STARTED.
They exist so a plan → run → status chain never forces a model to copy a whole
aicad.dag/1 document or a UUID back and forth between calls.

This lives in its own module because the state has THREE independent producers —

  * ``dag_plan``                       (services/dag/mcp_tools.py)
  * ``mission_start``                  (services/mcp/mission_tools.py)
  * ``MissionDispatch.dag_plan``       (services/agent/mission_dispatch.py)

— and two consumers, ``dag_run`` and ``dag_status``.  While the state was a
module-private dict inside ``mcp_tools``, the other two producers were invisible
to it, so a graph could be compiled and then be unrunnable.  Live 2026-08-06
(run-92188923731943): ``mission_start`` compiled a 7-node mission graph, and the
next three calls were ``dag_run`` → "no graph has been planned in this session
yet", ``dag_plan`` → "mission plan already compiled" with no graph attached, and
``dag_run`` → the same denial again.  The mission was planned and structurally
unexecutable, and every retry burned a billed completion.
"""
from __future__ import annotations

from typing import Any
from services.agent.request_context import owner_key, scoped_key

#: Canonical slot.  ``mcp_tools`` aliases this exact dict as its ``_LAST`` so
#: reads and writes on either name address one piece of state.
LAST: dict[str, Any] = {"graph_json": None, "graph_from": "", "run_id": ""}

#: Per-run execution state observed in THIS process: {run_id: {"status", "gates"}}.
#: ``dag_status`` reads the checkpoint DB off disk, which tells it the run exists
#: but nothing about whether it is sitting at a gate — so it used to hardcode
#: ``gate_pending: False``, which reads as "no gate pending" on a run that is in
#: fact gate-paused.  Whoever ran or resumed the run in this process DID see the
#: truth; this is where they leave it.
RUNS: dict[str, dict[str, Any]] = {}
_ACTOR_LAST: dict[tuple[str, str], dict[str, Any]] = {}


def _last() -> dict[str, Any]:
    owner = owner_key()
    if owner is None:
        return LAST
    return _ACTOR_LAST.setdefault(owner, {"graph_json": None, "graph_from": "", "run_id": ""})


def remember_graph(graph: Any, source: str = "") -> bool:
    """Record *graph* as this session's planned graph.  Returns whether it took.

    Only a mapping carrying a non-empty ``nodes`` list is remembered: an empty or
    malformed planner output must not shadow a good earlier plan, and it must not
    make ``dag_run()`` claim a graph exists that the executor would reject.
    """
    if not isinstance(graph, dict):
        return False
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    _last()["graph_json"] = graph
    _last()["graph_from"] = str(source or graph.get("id") or "")
    return True


def planned_graph() -> dict | None:
    """The last planned graph, or None when nothing has been planned."""
    graph = _last()["graph_json"]
    return graph if isinstance(graph, dict) else None


def planned_from() -> str:
    """Label of whatever produced the planned graph (run id or graph id)."""
    return str(_last()["graph_from"] or "")


def remember_run(run_id: Any) -> None:
    """Record *run_id* as the most recent run started in this session."""
    if isinstance(run_id, str) and run_id:
        _last()["run_id"] = run_id


def last_run() -> str:
    """The most recent run id started in this session, or ""."""
    return str(_last()["run_id"] or "")


def remember_result(result: Any) -> None:
    """Record what an executor.run/resume return said about the run's live state.

    Stores the run status and the ids of the nodes it is suspended on, so
    ``dag_status`` can report a REAL gate_pending instead of a hardcoded one.
    """
    if not isinstance(result, dict):
        return
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    interrupted = result.get("interrupted") or []
    gates: list[str] = []
    if isinstance(interrupted, list):
        for payload in interrupted:
            if isinstance(payload, dict):
                node = payload.get("node_id") or payload.get("node") or payload.get("id")
                if node:
                    gates.append(str(node))
    RUNS[scoped_key(run_id)] = {
        "status": str(result.get("status") or "unknown"),
        "gates": gates,
        "gate_pending": bool(interrupted),
    }


def run_result(run_id: str) -> dict[str, Any] | None:
    """What this process last observed about *run_id*, or None if it never ran it."""
    state = RUNS.get(scoped_key(str(run_id)))
    return dict(state) if isinstance(state, dict) else None


def reset() -> None:
    """Drop the session memory (tests; a fresh process starts here anyway)."""
    _last().update({"graph_json": None, "graph_from": "", "run_id": ""})
    owner = owner_key()
    if owner is None:
        RUNS.clear()
    else:
        prefix = "(" + repr(owner) + ", "
        for key in list(RUNS):
            if key.startswith(prefix):
                del RUNS[key]
