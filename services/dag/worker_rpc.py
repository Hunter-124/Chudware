"""Worker-RPC bridge for the DAG steering seam (mission-control UX).

Exposes ``dag_status``, ``dag_approve``, and ``dag_abort`` as **engine-worker
methods** so the C++/ImGui mission-control panel
(``ui/imgui/src/ui/mission_control_panel.cpp``) reaches them through the same
newline-delimited-JSON worker seam (``WorkerClient`` / ``AsyncRpc``,
``services/worker/``) it already uses for every other service — no bespoke
transport.  These are thin wrappers over :mod:`services.dag.executor` and the
persisted run sidecars; they carry NO gate logic of their own (single
implementation rule — same delegation as services/dag/mcp_tools.py, whose MCP
twins share executor.resume()).

Each method degrades gracefully (returns an ``ok: false`` envelope, never
raises).  Registered append-only in ``engine/worker.py``, following the
services.deliverables.worker_rpc / parts_hub.worker_rpc precedent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from services.dag.run_ids import validate_run_id
from services.agent.request_context import in_cell
from services.dag.storage import resolve_run


def _runs_dir() -> Path:
    """Resolve the default runs dir, importing the executor only when asked.

    ``services.dag.executor`` drags in langgraph -> langchain_core -> langsmith ->
    pydantic_core: 352 ms of the services worker's 457 ms import, for this one
    symbol. hostd spawns the worker lazily on the first engine-unknown RPC and
    serves it behind a single process-wide mutex, so that import lands squarely in
    front of the first Settings/PLM call the GUI makes after connecting. Keep this
    import inside the function.
    """
    from services.dag.executor import default_runs_dir

    return default_runs_dir()


def _dag_status(params: dict, _emit: Callable) -> dict:
    """Worker method ``dag_status`` → persisted run topology + events.jsonl path.

    Restart-safe: reads the .graph.json sidecar + checkpoint DB from disk.
    Adds ``events_jsonl`` (the EventJournal path) so the panel can replay
    the full event trail after an app restart.
    """
    try:
        run_id = (params or {}).get("run_id", "")
        if not run_id and not in_cell():
            return {"ok": False, "reason": "run_id is required"}
        if in_cell():
            record = resolve_run(run_id)
            run_id, db_path, graph_path = record.run_id, record.db, record.graph
        else:
            run_id = validate_run_id(run_id)
            base = Path((params or {}).get("runs_dir") or _runs_dir())
            db_path = base / f"{run_id}.db"
            graph_path = base / f"{run_id}.graph.json"
        if not db_path.exists():
            if in_cell():
                raise KeyError("not_found")
            return {"ok": False, "reason": f"Run {run_id!r} not found",
                    "hint": f"Check the run_id or the runs_dir (default: {base})."}
        graph: dict = {}
        if graph_path.exists():
            try:
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        nodes = graph.get("nodes", [])

        from services.dag.event_journal import run_events_path
        events_jsonl = run_events_path(run_id)

        return {
            "ok": True,
            "run_id": run_id,
            "db_exists": True,
            "graph_name": graph.get("name", ""),
            "node_count": len(nodes),
            "node_ids": [n.get("id", "") for n in nodes],
            "checkpoint_db": str(db_path),
            "events_jsonl": str(events_jsonl) if events_jsonl.exists() else "",
        }
    except KeyError:
        return {"ok": False, "reason": "not_found", "code": "not_found"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"dag_status failed: {exc}"}


def _resume_with(run_id: str, decision: str, params: dict) -> dict:
    """Shared resume path for approve/abort — delegates to executor.resume()."""
    try:
        if in_cell():
            run_id = resolve_run(run_id).run_id
    except KeyError:
        return {"ok": False, "reason": "not_found", "code": "not_found"}
    try:
        from services.dag import executor
    except ImportError as exc:
        return {"ok": False, "reason": f"DAG executor unavailable: {exc}"}
    try:
        result = executor.resume(
            run_id,
            graph_json=(params or {}).get("graph_json"),
            decision=decision,
            autonomy_default=(params or {}).get("autonomy_default", "approve"),
        )
    except KeyError:
        return {"ok": False, "reason": "not_found", "code": "not_found"}
    except FileNotFoundError as exc:
        return {"ok": False, "reason": str(exc),
                "hint": "Verify run_id matches an existing paused run."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"resume({decision}) failed: {exc}",
                "hint": "Ensure the run is currently paused at a gate."}
    out = {
        "ok": True,
        "run_id": result.get("run_id", run_id),
        "status": result.get("status", "unknown"),
        "nodes": result.get("nodes", {}),
        "checkpoint_db": result.get("checkpoint_db", ""),
    }
    interrupted = result.get("interrupted", [])
    if interrupted:
        out["interrupted"] = interrupted
    return out


def _dag_approve(params: dict, _emit: Callable) -> dict:
    """Worker method ``dag_approve`` → executor.resume(decision='approve')."""
    run_id = (params or {}).get("run_id", "")
    if not run_id and not in_cell():
        return {"ok": False, "reason": "run_id is required"}
    return _resume_with(run_id, "approve", params)


def _dag_abort(params: dict, _emit: Callable) -> dict:
    """Worker method ``dag_abort`` → executor.resume(decision='reject')."""
    run_id = (params or {}).get("run_id", "")
    if not run_id and not in_cell():
        return {"ok": False, "reason": "run_id is required"}
    return _resume_with(run_id, "reject", params)


# Registered by engine/worker.py via ``_METHODS.update(WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "dag_status": _dag_status,
    "dag_approve": _dag_approve,
    "dag_abort": _dag_abort,
}
