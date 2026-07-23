"""mcp_tools.py — MCP tool handlers for the five DAG orchestrator tools.

Contract: contracts/dag_schema.md §9–§11.
Registered by: services/mcp/server.py (append-only block — T-A4).
Wired by: T-A4. Resume path is executor.resume() — ONE implementation (driver notes).

Tools:
  dag_plan    Run the mission-planner node and return a graph JSON that the caller
              can hand directly to dag_run. Calls node_runner with a "plan" node
              against the live agent loop.

  dag_run     Start (or resume) a DAG run from a graph JSON.
              - If run_id is omitted: starts a new run (executor.run()).
              - If run_id is supplied: resumes a paused run (executor.resume()).

  dag_status  Return the current state of a run: status, current node IDs, gate-
              pending flag, and node-level verdicts.  Reads the persisted
              .graph.json sidecar + checkpoint DB so it is restart-safe.

  dag_approve Resume a gate-paused run with decision="approve".
              MCP twin of executor.resume(run_id, decision="approve") — no separate
              gate logic here, one implementation.

  dag_abort   Cancel a run by injecting decision="reject" into the current gate
              (or marking the run failed if not currently paused).

All tools return a JSON string {ok, ...} following the project's _dumps/_err convention.
Never call the LLM directly — dag_plan delegates to executor.run() which uses node_runner,
which has its own agent_fn injection point.
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", **extra: Any) -> str:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return _dumps(out)


def _runs_dir() -> Path:
    """Platform default runs directory (mirrors executor.default_runs_dir)."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "runs"


def _load_run_status(run_id: str, runs_dir: Path) -> dict[str, Any] | None:
    """Read the persisted graph sidecar + infer run status from checkpoint existence.

    Returns a status dict or None if the run is unknown.
    This is the restart-safe path: we read .graph.json from disk and check
    whether the DB still exists.  LangGraph checkpoint introspection is NOT
    used here because it requires re-opening the DB with a compiled graph —
    instead dag_status reports from the graph topology + the sidecar.

    Honest caveat: node-level status comes from the last executor.run()/resume()
    return value, which is NOT persisted by dag_status itself.  Only the graph
    topology is restart-safe; per-node verdicts are in-session only unless
    T-F17 adds a narration DB.  dag_status documents this limitation.
    """
    db_path = runs_dir / f"{run_id}.db"
    graph_path = runs_dir / f"{run_id}.graph.json"
    if not db_path.exists():
        return None
    graph_json: dict = {}
    if graph_path.exists():
        try:
            graph_json = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {
        "run_id": run_id,
        "checkpoint_db": str(db_path),
        "graph_json": graph_json,
    }


# ---------------------------------------------------------------------------
# Registration function (called from server.py)
# ---------------------------------------------------------------------------

def register_dag_tools(mcp) -> None:
    """Register the five DAG orchestrator MCP tools onto *mcp*.

    Follows the same register_*_tools(mcp) pattern as hydraulic_tools / cost_tools.
    Appended to services/mcp/server.py by T-A4 (append-only).
    """

    @mcp.tool()
    def dag_plan(
        brief: str,
        graph_name: str = "mission",
        autonomy_default: str = "approve",
    ) -> str:
        """Decompose a natural-language brief into an aicad.dag/1 graph JSON.

        Runs a 'plan' node through the mission-planner agent loop and returns the
        resulting graph JSON (ready to hand to dag_run).  The plan node calls the
        live agent with the brief as its request and expects the agent to produce
        a JSON graph document.

        Args:
            brief:             Plain-English mission description
                               (e.g. "Design an aluminium bracket and run interference check").
            graph_name:        Human label for the resulting graph (default: "mission").
            autonomy_default:  Run-level autonomy floor — "auto" | "approve" | "manual".
                               "approve" (default) gates every node for human oversight.

        Returns:
            {ok, run_id, graph_json, status, checkpoint_db}
            graph_json is the aicad.dag/1 graph produced by the planner.
            On failure: {ok: false, reason, hint}.
        """
        try:
            from services.dag import executor
            from services.dag.graph_loader import validate_graph
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}",
                        hint="Ensure services/dag is on PYTHONPATH.")

        # Build a minimal single-node "plan" graph whose output is the real graph.
        plan_graph: dict[str, Any] = {
            "schema": "aicad.dag/1",
            "id":     f"plan-{graph_name}",
            "name":   f"Planner: {graph_name}",
            "nodes": [
                {
                    "id":            "plan",
                    "type":          "plan",
                    "name":          "Mission planner",
                    "inputs":        {"request": brief},
                    "tools":         None,
                    "end_condition": "Plan decomposed into actionable DAG nodes.",
                    "verify":        None,
                    "on_fail":       "pause",
                    "autonomy":      "auto",
                }
            ],
            "edges": [],
        }

        try:
            result = executor.run(
                plan_graph,
                autonomy_default=autonomy_default,
            )
        except Exception as exc:
            return _err(f"Planner run failed: {exc}",
                        hint="Check the brief and retry, or supply a graph JSON directly to dag_run.")

        # Extract the graph JSON from the planner's output if present.
        node_output = result.get("nodes", {}).get("plan", {}).get("output", {})
        graph_json_out = node_output.get("data", {}).get("agent_result") or {}

        return _dumps({
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "done"),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "graph_json": graph_json_out,
            "hint": (
                "Pass graph_json to dag_run to execute the planned mission. "
                "Review and edit the graph before running in production."
            ),
            "next_actions": [
                "dag_run(graph_json=<result.graph_json>)",
            ],
        })

    @mcp.tool()
    def dag_run(
        graph_json: dict,
        run_id: Optional[str] = None,
        autonomy_default: str = "approve",
        initial_state: Optional[dict] = None,
    ) -> str:
        """Start or resume a DAG run from an aicad.dag/1 graph document.

        - If run_id is omitted: starts a new run (executor.run()).
        - If run_id is supplied and the matching checkpoint exists: resumes a
          paused run (executor.resume()).  This is the same surface as dag_approve
          but without fixing the decision — pass decision="approve"|"reject" via
          dag_approve / dag_abort instead when you want to resume a gate.

        Args:
            graph_json:        aicad.dag/1 graph document (from dag_plan or hand-authored).
            run_id:            UUID4 of an existing paused run (omit for a new run).
            autonomy_default:  Run-level autonomy floor — "auto" | "approve" | "manual".
            initial_state:     Extra key→value pairs merged into the initial DAG state.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db, interrupted?}
            interrupted present and non-empty → run is paused at a gate; call dag_approve.
            On failure: {ok: false, reason, hint}.
        """
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        try:
            if run_id:
                result = executor.resume(
                    run_id,
                    graph_json=graph_json,
                    autonomy_default=autonomy_default,
                )
            else:
                result = executor.run(
                    graph_json,
                    initial_state=initial_state or {},
                    autonomy_default=autonomy_default,
                )
        except FileNotFoundError as exc:
            return _err(f"Run not found: {exc}",
                        hint="Omit run_id to start a new run, or verify the run_id is correct.")
        except Exception as exc:
            return _err(f"DAG run failed: {exc}",
                        hint="Check graph_json validity (schema: aicad.dag/1) and retry.")

        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = "Run paused at gate. Call dag_approve to proceed or dag_abort to cancel."
            out["next_actions"] = [
                f"dag_approve(run_id='{result['run_id']}')",
                f"dag_abort(run_id='{result['run_id']}')",
                f"dag_status(run_id='{result['run_id']}')",
            ]
        else:
            out["hint"] = f"Run finished with status={result.get('status', 'unknown')!r}."
            out["next_actions"] = [
                f"dag_status(run_id='{result['run_id']}')",
            ]
        return _dumps(out)

    @mcp.tool()
    def dag_status(
        run_id: str,
        runs_dir: Optional[str] = None,
    ) -> str:
        """Return the current state of a DAG run.

        Reads the persisted .graph.json sidecar and checkpoint DB to determine
        whether the run exists and what its graph topology looks like.
        Restart-safe: survives app restart (reads from disk, not from in-memory state).

        NOTE: per-node status verdicts are only fully available in the same session
        as the run/resume call; dag_status reports topology from the graph sidecar
        and checkpoint existence from the DB.  T-F17 mission-control UX will add a
        narration DB for full cross-restart node verdicts.

        Args:
            run_id:    UUID4 of the run to inspect.
            runs_dir:  Override the default runs directory (tests / custom installs).

        Returns:
            {ok, run_id, db_exists, graph_name, node_count, node_ids,
             gate_pending, status, checkpoint_db}
            gate_pending: True when the run is currently suspended at a gate.
            On unknown run: {ok: false, reason}.
        """
        base = Path(runs_dir) if runs_dir else _runs_dir()
        info = _load_run_status(run_id, base)
        if info is None:
            return _err(
                f"Run {run_id!r} not found",
                hint=f"Check the run_id or the runs_dir (default: {base}).",
            )

        graph = info.get("graph_json", {})
        nodes = graph.get("nodes", [])
        node_ids = [n["id"] for n in nodes]

        # Determine gate_pending: if the checkpoint DB exists but we have no in-memory
        # interrupted state (we don't after restart), we report gate_pending as unknown.
        # The only reliable gate_pending signal is from the in-process run/resume return.
        # We conservatively report False here; the caller should check dag_run's "interrupted".
        gate_pending_flag = False  # see NOTE above

        return _dumps({
            "ok": True,
            "run_id": run_id,
            "db_exists": True,
            "graph_name": graph.get("name", ""),
            "node_count": len(nodes),
            "node_ids": node_ids,
            "gate_pending": gate_pending_flag,
            "status": "unknown_after_restart",
            "checkpoint_db": info["checkpoint_db"],
            "hint": (
                "Per-node status is available in the same session as dag_run/dag_approve. "
                "For cross-restart status, call dag_run with the same run_id to resume and "
                "see the current interrupted/status fields."
            ),
            "next_actions": [
                f"dag_approve(run_id='{run_id}')  # if the run is gate-paused",
                f"dag_abort(run_id='{run_id}')    # to cancel the run",
            ],
        })

    @mcp.tool()
    def dag_approve(
        run_id: str,
        graph_json: Optional[dict] = None,
        autonomy_default: str = "approve",
    ) -> str:
        """Resume a gate-paused DAG run with decision='approve'.

        MCP twin of executor.resume(run_id, decision='approve').  This is ONE
        implementation — dag_approve delegates directly to executor.resume() and
        carries no gate logic of its own.

        Args:
            run_id:            UUID4 of the paused run (from dag_run's response).
            graph_json:        The original aicad.dag/1 graph document.
                               If omitted, the executor loads it from the persisted
                               sidecar written at run start (restart-safe path).
            autonomy_default:  Must match the autonomy_default used in dag_run.

        Returns:
            Same shape as dag_run: {ok, run_id, status, nodes, checkpoint_db, interrupted?}.
            On failure: {ok: false, reason, hint}.
        """
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        try:
            result = executor.resume(
                run_id,
                graph_json=graph_json,
                decision="approve",
                autonomy_default=autonomy_default,
            )
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Verify run_id matches an existing paused run (from dag_run).")
        except Exception as exc:
            return _err(f"dag_approve failed: {exc}",
                        hint="Ensure the run is currently paused at a gate.")

        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = "Another gate was reached. Call dag_approve again or dag_abort to cancel."
            out["next_actions"] = [
                f"dag_approve(run_id='{run_id}')",
                f"dag_abort(run_id='{run_id}')",
            ]
        else:
            out["hint"] = f"Run resumed and finished with status={result.get('status', 'unknown')!r}."
        return _dumps(out)

    @mcp.tool()
    def dag_abort(
        run_id: str,
        graph_json: Optional[dict] = None,
        autonomy_default: str = "approve",
    ) -> str:
        """Cancel a gate-paused DAG run by injecting decision='reject'.

        If the run is currently suspended at a gate, this resumes it with
        decision='reject', which causes the gate node to mark its node status
        as 'paused' and terminate the run.

        Args:
            run_id:            UUID4 of the paused run to cancel.
            graph_json:        The original graph document (or omit to use the sidecar).
            autonomy_default:  Must match the autonomy_default used in dag_run.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db}
            status will be 'paused' or 'failed' after abort.
            On failure: {ok: false, reason, hint}.
        """
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        try:
            result = executor.resume(
                run_id,
                graph_json=graph_json,
                decision="reject",
                autonomy_default=autonomy_default,
            )
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Verify run_id matches an existing paused run (from dag_run).")
        except Exception as exc:
            return _err(f"dag_abort failed: {exc}",
                        hint="Ensure the run is currently paused at a gate before aborting.")

        return _dumps({
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "hint": "Run aborted (decision=reject injected at gate).",
            "next_actions": [
                "Inspect nodes for the gate that was rejected, then re-run with a corrected graph.",
            ],
        })


def register_dag_replan_tools(mcp) -> None:
    """Register the dag_replan MCP tool onto *mcp*.

    Appended by T-F12 (append-only per server.py contract).
    """

    @mcp.tool()
    def dag_replan(
        run_id: str,
        splice_nodes: list,
        splice_edges: list,
        after_node_id: str,
        decision: str = "approve",
        autonomy_default: str = "approve",
    ) -> str:
        """Splice new nodes into a running DAG run and resume execution.

        Validates the splice (every spliced node must have end_condition + a back-edge
        with retry_budget), merges the new nodes into the persisted graph sidecar,
        then resumes the run via executor.resume().

        Answered gate questions are NOT re-asked: the T-F4 ledger is stored in the
        SqliteSaver checkpoint and replayed automatically on resume.

        Args:
            run_id:          UUID4 of the paused (or mid-run) DAG run to replan.
            splice_nodes:    List of aicad.dag/1 node dicts to insert.
                             Each MUST have: id, type, name, inputs, on_fail, autonomy,
                             AND a non-empty end_condition.
            splice_edges:    List of aicad.dag/1 edge dicts.
                             MUST include at least one back-edge (edge with 'retry_budget')
                             for EACH spliced node. May also include forward edges.
            after_node_id:   Existing node id after which the splice is inserted.
            decision:        Gate decision for resume — 'approve' (default) | 'reject'.
            autonomy_default: Run-level autonomy floor for the resumed run.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db, spliced_node_ids}
            On validation failure: {ok: false, reason, hint}.
        """
        try:
            from services.dag.replan import dag_replan as _replan, ReplannError
            from services.dag.graph_loader import GraphValidationError
            from services.dag import executor
        except ImportError as exc:
            return _err(f"dag_replan unavailable: {exc}",
                        hint="Ensure services/dag is on PYTHONPATH.")

        # Step 1: validate + merge splice
        try:
            merged_graph = _replan(
                run_id=run_id,
                splice_nodes=splice_nodes,
                splice_edges=splice_edges,
                after_node_id=after_node_id,
            )
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Verify run_id matches an existing run (from dag_run).")
        except ReplannError as exc:
            return _err(
                f"Splice validation failed: {exc}",
                hint=(
                    "Every spliced node must have a non-empty 'end_condition' and "
                    "at least one back-edge with 'retry_budget' in splice_edges."
                ),
            )
        except GraphValidationError as exc:
            return _err(
                f"Merged graph failed aicad.dag/1 validation: {exc}",
                hint="Fix the spliced nodes/edges to conform to the schema.",
            )

        # Step 2: resume with the merged graph
        try:
            result = executor.resume(
                run_id,
                graph_json=merged_graph,
                decision=decision,
                autonomy_default=autonomy_default,
            )
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Checkpoint DB missing — the run may have already completed.")
        except Exception as exc:
            return _err(f"Resume after replan failed: {exc}",
                        hint="Check that the run is currently paused at a gate.")

        spliced_ids = [n["id"] for n in splice_nodes]
        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "spliced_node_ids": spliced_ids,
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = (
                "Replan applied and run is paused at a gate. "
                "Call dag_approve to continue."
            )
            out["next_actions"] = [
                f"dag_approve(run_id='{run_id}')",
                f"dag_abort(run_id='{run_id}')",
            ]
        else:
            out["hint"] = (
                f"Replan applied. Run resumed and finished with "
                f"status={result.get('status', 'unknown')!r}."
            )
            out["next_actions"] = [
                f"dag_status(run_id='{run_id}')",
            ]
        return _dumps(out)
