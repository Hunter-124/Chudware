"""executor.py — Run an aicad.dag/1 graph with SQLite-persisted checkpoints.

Contract: contracts/dag_schema.md §9–§11
Scaffold: T-A1. Real capability dispatch in T-A2; interrupt() in T-A3; verify in T-A4.

Usage:
    from services.dag import executor
    result = executor.run(graph_json, initial_state={})
    # After a gate pause:
    result = executor.resume(run_id, graph_json, decision="approve", runs_dir=...)
"""
from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph_loader import DagState, EmitFn, build_state_graph, validate_graph
from .event_journal import EventJournal
from .run_ids import validate_run_id
from services.agent.request_context import in_cell
from .storage import create_run, private_runs_dir, resolve_run

# ---------------------------------------------------------------------------
# Runs directory resolution
# ---------------------------------------------------------------------------

def default_runs_dir() -> Path:
    """Return the platform default runs directory.

    - Linux/macOS: $XDG_DATA_HOME/Chudware/runs/  (default ~/.local/share/Chudware/runs/)
    - Windows:     %APPDATA%\\Chudware\\runs\\

    Note: services/paths.py does not have a Linux XDG branch (known issue, out of scope
    for T-A1).  This function is the authoritative runs-dir resolver for services/dag.
    Override via the runs_dir parameter to run() for hermetic tests.
    """
    if in_cell():
        return private_runs_dir()
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "runs"


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
    autonomy_default: str = "auto",
) -> dict[str, Any]:
    """Execute an aicad.dag/1 graph and persist checkpoints to SQLite.

    Args:
        graph_json:        Validated aicad.dag/1 graph document.
        initial_state:     Extra keys merged into the initial DAG state (optional).
        run_id:            Safe run identifier. Generated as a UUID4 if not supplied.
        runs_dir:          Directory for checkpoint .db files.
                           Defaults to default_runs_dir(). Pass a tmp dir for hermetic tests.
        emit:              Optional event callback ``(event_type: str, payload: dict) -> None``.
                           Receives dag.run.* and dag.node.* events per §11 of the contract.
        agent_fn:          Optional agent callable injected into node handlers.
                           Signature: ``(request: str, context_pack: dict) -> AgentRun``.
                           Defaults to the live agent loop.  Tests inject a mock here.
        autonomy_default:  Run-level autonomy floor — "auto" | "approve" | "manual".
                           Can only RAISE a node's declared autonomy level, never lower it.
                           When "approve", every "auto" node is promoted to gate-before-exec.

    Returns:
        dict with keys:
          run_id        : str (caller-supplied safe ID or generated UUID4)
          status        : RunState string — "done" | "failed" | "paused" | "running"
          nodes         : {node_id: {"status": NodeState, "output": dict}}
          checkpoint_db : str (absolute path to the SQLite checkpoint file)

        When a gate fires the run suspends: status="paused", and the dict includes
          interrupted     : list of interrupt payload dicts (aicad.interaction/1 shape)
        The caller (or UI) persists graph_json beside the .db for resume().
    """
    validate_graph(graph_json)
    if in_cell():
        record = None
        if run_id is not None:
            try:
                record = resolve_run(run_id)
            except KeyError:
                pass
        if record is None:
            record = create_run(run_id, autonomy_default)
        elif record.db.exists() or record.graph.exists():
            raise ValueError("run already exists")
        run_id = record.run_id
        autonomy_default = record.autonomy
        db_path, graph_json_path = record.db, record.graph
    else:
        run_id = validate_run_id(str(uuid.uuid4()) if run_id is None else run_id)
        base_dir = Path(runs_dir) if runs_dir is not None else default_runs_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_dir / f"{run_id}.db"
        graph_json_path = base_dir / f"{run_id}.graph.json"
    graph_id = graph_json.get("id", "unknown")
    node_count = len(graph_json["nodes"])
    if not graph_json_path.exists():
        graph_json_path.write_text(json.dumps(graph_json), encoding="utf-8")

    # T-F17: wrap the caller's emit with an EventJournal so every dag.* event is
    # persisted to events.jsonl for mission-control replay-on-reopen.
    # The journal's cache/runs dir is intentionally separate from the checkpoint db_path
    # (which lives in runs/) per contracts/agent_events.md §1.
    journal = EventJournal(run_id)
    emit = journal.wrap(emit)

    emit("dag.run.started", {
        "run_id": run_id,
        "graph_id": graph_id,
        "node_count": node_count,
        "graph_name": graph_json.get("name", ""),
    })

    # Build the uncompiled StateGraph, then compile with checkpointer inside the CM
    # so the SqliteSaver connection is alive for the full invocation.
    uncompiled = build_state_graph(
        graph_json, emit=emit, agent_fn=agent_fn,
        autonomy_default=autonomy_default,
    )

    # NOTE: emit is passed via closure into node handlers by build_state_graph(),
    # NOT carried in state — the LangGraph checkpointer cannot serialise callables.
    initial: DagState = {
        **(initial_state or {}),
        "run_id": run_id,
        "run_status": "running",
        "node_states": {},
    }
    if not in_cell():
        initial.update(initial_state or {})

    config = {"configurable": {"thread_id": run_id}}

    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        compiled = uncompiled.compile(checkpointer=checkpointer)
        final_state: DagState = compiled.invoke(initial, config=config)

    # Detect a gate-pause: LangGraph returns {"__interrupt__": [...]} in the state.
    interrupted = final_state.get("__interrupt__", [])
    if interrupted:
        interrupt_payloads = [
            i.value if hasattr(i, "value") else i for i in interrupted
        ]
        _emit_gate_pause_events(emit, run_id, interrupt_payloads)
        return {
            "run_id":       run_id,
            "name":         graph_json.get("name", ""),
            "status":       "paused",
            "nodes":        final_state.get("node_states", {}),
            "open_questions": final_state.get("open_questions", []),
            "checkpoint_db": str(db_path),
            "interrupted":  interrupt_payloads,
        }

    # Resolve final run_status from node outcomes
    node_states: dict[str, Any] = final_state.get("node_states", {})
    run_status = _resolve_run_status(node_states, final_state.get("run_status", "running"))

    if emit:
        emit("dag.run.done", {
            "run_id": run_id,
            "status": run_status,
            "node_states": node_states,
        })

    # §14 narration fields: describe_project(run=...) in gsl_tools.py reads
    # "name"/"project", "open_questions", and "budget" via .get().  Populate the
    # two we can reliably source; budget is omitted (loop.py carries no token
    # usage surface — T-F17 may add it later; the contract degrades gracefully).
    return {
        "run_id": run_id,
        "name": graph_json.get("name", ""),          # §14: project name for narration
        "status": run_status,
        "nodes": node_states,
        "open_questions": final_state.get("open_questions", []),  # §14: escalation log
        "checkpoint_db": str(db_path),
    }


def resume(
    run_id: str,
    graph_json: Optional[dict[str, Any]] = None,
    *,
    decision: Any = "approve",
    runs_dir: Optional[Path] = None,
    emit: EmitFn = None,
    agent_fn: Optional[Any] = None,
    autonomy_default: str = "auto",
) -> dict[str, Any]:
    """Resume a gate-paused run with a human decision.

    Reconstructs the graph topology from the persisted .graph.json (or from
    graph_json if supplied), re-opens the SqliteSaver checkpoint DB, and invokes
    the compiled graph with Command(resume=decision).

    This is the app-restart-safe resume path: the SqliteSaver connection from the
    original run() call is fully closed and dropped; a brand-new connection is
    opened against the same file.  The test for DoD "interrupt() persistence
    survives app restart" is: call run() → drop all references → call resume()
    with a fresh executor (new process / new import) → verify completion.

    Args:
        run_id:            UUID4 of the paused run.
        graph_json:        The original aicad.dag/1 graph document.  If None, the
                           executor loads it from the <run_id>.graph.json sidecar
                           written by run() at start time.
        decision:          Human decision fed back to interrupt():
                             "approve"  — proceed (default)
                             "reject"   — mark gate failed
                             {"action": "modify", "output": {...}}  — override output
                           Passed verbatim as Command(resume=decision).
        runs_dir:          Must match the runs_dir used in the original run() call.
        emit:              Optional event callback (same signature as run()).
        agent_fn:          Optional agent callable (same as run()).
        autonomy_default:  Must match the autonomy_default used in the original run().

    Returns:
        Same shape as run() — {run_id, status, nodes, checkpoint_db, …}.
        May be "paused" again if another gate fires downstream.
    """
    if in_cell():
        record = resolve_run(run_id)
        run_id = record.run_id
        db_path, graph_json_path = record.db, record.graph
        # An ordinary approval cannot replace topology or lower the stored gate floor.
        graph_json = None
        autonomy_default = record.autonomy
    else:
        run_id = validate_run_id(run_id)
        base_dir = Path(runs_dir) if runs_dir is not None else default_runs_dir()
        db_path = base_dir / f"{run_id}.db"
        graph_json_path = base_dir / f"{run_id}.graph.json"

    if not db_path.exists():
        if in_cell():
            raise KeyError("not_found")
        raise FileNotFoundError(
            f"Checkpoint DB not found: {db_path}. "
            "Pass the same runs_dir used in executor.run()."
        )

    # Load graph_json from sidecar if not supplied (app-restart path).
    if graph_json is None:
        if not graph_json_path.exists():
            if in_cell():
                raise KeyError("not_found")
            raise FileNotFoundError(
                f"Graph sidecar not found: {graph_json_path}. "
                "Supply graph_json explicitly or ensure run() wrote the sidecar."
            )
        graph_json = json.loads(graph_json_path.read_text(encoding="utf-8"))

    validate_graph(graph_json)

    # T-F17: wrap the caller's emit with the journal so resume events are also persisted.
    journal = EventJournal(run_id)
    emit = journal.wrap(emit)

    # Reconstruct the identical StateGraph topology and compile with the existing DB.
    # LangGraph persists STATE, not structure — the graph must be rebuilt identically.
    uncompiled = build_state_graph(
        graph_json, emit=emit, agent_fn=agent_fn,
        autonomy_default=autonomy_default,
    )

    config = {"configurable": {"thread_id": run_id}}

    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        compiled = uncompiled.compile(checkpointer=checkpointer)
        # Command(resume=decision) feeds the decision back into interrupt().
        final_state: DagState = compiled.invoke(Command(resume=decision), config=config)

    # Another gate may have fired downstream.
    interrupted = final_state.get("__interrupt__", [])
    if interrupted:
        interrupt_payloads = [
            i.value if hasattr(i, "value") else i for i in interrupted
        ]
        _emit_gate_pause_events(emit, run_id, interrupt_payloads)
        return {
            "run_id":        run_id,
            "name":          graph_json.get("name", ""),
            "status":        "paused",
            "nodes":         final_state.get("node_states", {}),
            "open_questions": final_state.get("open_questions", []),
            "checkpoint_db": str(db_path),
            "interrupted":   interrupt_payloads,
        }

    node_states: dict[str, Any] = final_state.get("node_states", {})
    run_status = _resolve_run_status(node_states, final_state.get("run_status", "running"))

    if emit:
        emit("dag.run.done", {
            "run_id":      run_id,
            "status":      run_status,
            "node_states": node_states,
        })

    return {
        "run_id":          run_id,
        "name":            graph_json.get("name", ""),
        "status":          run_status,
        "nodes":           node_states,
        "open_questions":  final_state.get("open_questions", []),
        "checkpoint_db":   str(db_path),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _emit_gate_pause_events(
    emit: EmitFn,
    run_id: str,
    interrupt_payloads: list[Any],
) -> None:
    """Emit §11 gate pause events, exactly once per suspend.

    These events CANNOT be emitted inside the node handler: on resume LangGraph
    re-executes the handler from the top before interrupt() returns, so any emit
    placed before the interrupt double-fires (see gate.run_gate()).  Emitting
    here — from the surfaced '__interrupt__' payloads — fires exactly once per
    suspend: run()/resume() only reach this path when a NEW interrupt surfaced.

    The payloads carry top-level ``node_id`` and ``autonomy`` (effective level)
    keys written by gate.make_gate_payload() for exactly this purpose.
    """
    if not emit:
        return
    for payload in interrupt_payloads:
        if not isinstance(payload, dict):
            continue
        node_id = payload.get("node_id", "unknown")
        autonomy = payload.get("autonomy", "approve")
        if autonomy == "manual":
            emit("dag.node.manual_required", {
                "run_id":  run_id,
                "node_id": node_id,
            })
            emit("dag.node.paused", {
                "run_id":  run_id,
                "node_id": node_id,
                "reason":  "manual",
            })
        else:
            emit("dag.node.paused", {
                "run_id":  run_id,
                "node_id": node_id,
                "reason":  "gate",
            })
            emit("dag.node.approval_required", {
                "run_id":  run_id,
                "node_id": node_id,
            })


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
