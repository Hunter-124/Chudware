"""executor.py — Run an aicad.dag/1 graph with SQLite-persisted checkpoints.

Contract: contracts/dag_schema.md §9–§11
Scaffold: T-A1. Real capability dispatch in T-A2; interrupt() in T-A3; verify in T-A4.

Usage:
    from services.dag import executor
    result = executor.run(graph_json, initial_state={})
"""
from __future__ import annotations

import os
import platform
import uuid
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver

from .graph_loader import DagState, EmitFn, build_state_graph, validate_graph

# ---------------------------------------------------------------------------
# Runs directory resolution
# ---------------------------------------------------------------------------

def default_runs_dir() -> Path:
    """Return the platform default runs directory.

    - Linux/macOS: $XDG_DATA_HOME/AI-CAD/runs/  (default ~/.local/share/AI-CAD/runs/)
    - Windows:     %APPDATA%\\AI-CAD\\runs\\

    Note: engine/paths.py does not have a Linux XDG branch (known issue, out of scope
    for T-A1).  This function is the authoritative runs-dir resolver for services/dag.
    Override via the runs_dir parameter to run() for hermetic tests.
    """
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "AI-CAD" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "AI-CAD" / "runs"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    graph_json: dict[str, Any],
    initial_state: Optional[dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    runs_dir: Optional[Path] = None,
    emit: EmitFn = None,
    agent_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """Execute an aicad.dag/1 graph and persist checkpoints to SQLite.

    Args:
        graph_json:    Validated aicad.dag/1 graph document.
        initial_state: Extra keys merged into the initial DAG state (optional).
        run_id:        UUID4 string. Generated if not supplied.
        runs_dir:      Directory for checkpoint .db files.
                       Defaults to default_runs_dir(). Pass a tmp dir for hermetic tests.
        emit:          Optional event callback ``(event_type: str, payload: dict) -> None``.
                       Receives dag.run.* and dag.node.* events per §11 of the contract.
        agent_fn:      Optional agent callable injected into node handlers.
                       Signature: ``(request: str, context_pack: dict) -> AgentRun``.
                       Defaults to the live agent loop.  Tests inject a mock here.

    Returns:
        dict with keys:
          run_id        : str (UUID4)
          status        : RunState string — "done" | "failed" | "paused" | "running"
          nodes         : {node_id: {"status": NodeState, "output": dict}}
          checkpoint_db : str (absolute path to the SQLite checkpoint file)
    """
    validate_graph(graph_json)

    run_id = run_id or str(uuid.uuid4())
    graph_id = graph_json.get("id", "unknown")
    node_count = len(graph_json["nodes"])

    base_dir: Path = runs_dir if runs_dir is not None else default_runs_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    db_path = base_dir / f"{run_id}.db"

    if emit:
        emit("dag.run.started", {
            "run_id": run_id,
            "graph_id": graph_id,
            "node_count": node_count,
        })

    # Build the uncompiled StateGraph, then compile with checkpointer inside the CM
    # so the SqliteSaver connection is alive for the full invocation.
    uncompiled = build_state_graph(graph_json, emit=emit, agent_fn=agent_fn)

    # NOTE: emit is passed via closure into node handlers by build_state_graph(),
    # NOT carried in state — the LangGraph checkpointer cannot serialise callables.
    initial: DagState = {
        "run_id": run_id,
        "run_status": "running",
        "node_states": {},
        **(initial_state or {}),
    }

    config = {"configurable": {"thread_id": run_id}}

    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        compiled = uncompiled.compile(checkpointer=checkpointer)
        final_state: DagState = compiled.invoke(initial, config=config)

    # Resolve final run_status from node outcomes
    node_states: dict[str, Any] = final_state.get("node_states", {})
    run_status = _resolve_run_status(node_states, final_state.get("run_status", "running"))

    if emit:
        emit("dag.run.done", {
            "run_id": run_id,
            "status": run_status,
            "node_states": node_states,
        })

    return {
        "run_id": run_id,
        "status": run_status,
        "nodes": node_states,
        "checkpoint_db": str(db_path),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_run_status(node_states: dict[str, Any], current: str) -> str:
    """Determine final run status from node outcomes.

    Priority: explicit "failed"/"paused" > observed node failures > all done.
    """
    if current in ("paused", "failed"):
        return current
    statuses = {v.get("status") for v in node_states.values()}
    if "failed" in statuses:
        return "failed"
    if "paused" in statuses:
        return "paused"
    if statuses and all(s in ("done", "skipped") for s in statuses):
        return "done"
    return current
