"""mcp_tools.py — MCP tool handlers for buildgraph_plan and buildgraph_status.

Contract: contracts/advisor.md §13 (FROZEN).
Registered by: services/mcp/server.py (append-only block — T-W15).
MCP freshness: both tools must appear in server instructions (services/mcp/server.py).

Tools:
  buildgraph_plan(plan_json)
      Consume a loop.py BUILD PLAN or aicad.dag/1 mission JSON, run the
      LLM-free planner, validate, and return the aicad.buildgraph/1 graph +
      any advisory findings.

  buildgraph_status(run_id)
      Return node statuses for a previously-planned run.
      Reads from the lightweight sidecar written by buildgraph_plan.

Persistence:  buildgraph_plan writes a <run_id>.buildgraph.json sidecar into
the runs directory (mirrors the dag executor sidecar pattern).
buildgraph_status reads it back.

Returns {ok, ...} following the project _dumps/_err convention from dag/mcp_tools.py.
"""
from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers — mirrors services/dag/mcp_tools.py
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
        return Path(appdata) / "AI-CAD" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "AI-CAD" / "runs"


def _sidecar_path(run_id: str, runs_dir: Path) -> Path:
    return runs_dir / f"{run_id}.buildgraph.json"


def _write_sidecar(run_id: str, runs_dir: Path, data: dict) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    _sidecar_path(run_id, runs_dir).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _read_sidecar(run_id: str, runs_dir: Path) -> dict | None:
    path = _sidecar_path(run_id, runs_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Registration function
# ---------------------------------------------------------------------------

def register_buildgraph_tools(mcp, runs_dir: Optional[Path] = None) -> None:
    """Register buildgraph_plan and buildgraph_status MCP tools onto *mcp*.

    Follows the same register_*_tools(mcp) pattern as services/dag/mcp_tools.py.
    Appended to services/mcp/server.py by T-W15 (append-only block).

    Args:
        mcp:       FastMCP / mcp_server instance.
        runs_dir:  Override for tests; defaults to platform default.
    """
    _base_runs_dir = runs_dir  # captured in closures

    @mcp.tool()
    def buildgraph_plan(
        plan_json: dict,
        mission: str = "",
        runs_dir_override: Optional[str] = None,
    ) -> str:
        """Consume a BUILD PLAN or dag/1 mission, return an aicad.buildgraph/1 graph.

        The planner is LLM-free: it infers prerequisite edges from declarative rules
        (datum→sketch, boss→pierce, parts→mates, pattern→seed, etc.) and emits
        aicad.advisory/1 findings for any ambiguity — it never guesses.

        Args:
            plan_json:
                Either a loop.py BUILD PLAN dict (keys: name, parts[], fit) or an
                aicad.dag/1 graph dict (schema: "aicad.dag/1").  The planner detects
                the shape automatically.
            mission:
                Optional human label for the resulting graph.
            runs_dir_override:
                Optional filesystem path to store the buildgraph sidecar.
                Defaults to the platform runs dir (~/.local/share/AI-CAD/runs/).

        Returns:
            {ok, run_id, graph, advisories, sidecar_path}
            graph conforms to aicad.buildgraph/1.
            advisories is a list of aicad.advisory/1 items (may be empty = clean plan).
            On failure: {ok: false, reason, hint}.
        """
        try:
            from services.agent.buildgraph.planner import build_graph
            from services.agent.buildgraph.schema import BuildGraphCycleError
        except ImportError as exc:
            return _err(
                f"buildgraph package unavailable: {exc}",
                hint="Ensure services/agent/buildgraph is on PYTHONPATH.",
            )

        try:
            graph, advisories = build_graph(plan_json, mission=mission)
        except BuildGraphCycleError as exc:
            return _err(
                f"Dependency cycle in build plan: {exc}",
                hint="Remove the circular prerequisite reference from the plan.",
            )
        except Exception as exc:
            return _err(
                f"Build graph planning failed: {exc}",
                hint="Check plan_json shape: needs 'parts' (BUILD PLAN) or schema='aicad.dag/1'.",
            )

        run_id = str(uuid.uuid4())
        base = (
            Path(runs_dir_override) if runs_dir_override
            else (_base_runs_dir or _runs_dir())
        )
        sidecar_data = {"run_id": run_id, "graph": graph, "advisories": advisories}
        try:
            _write_sidecar(run_id, base, sidecar_data)
            sidecar_str = str(_sidecar_path(run_id, base))
        except Exception:  # noqa: BLE001
            sidecar_str = ""  # non-fatal

        blocker_count = sum(
            1 for adv in advisories
            for item in adv.get("items", [])
            if item.get("severity") == "blocker"
        )
        return _dumps({
            "ok": True,
            "run_id": run_id,
            "graph": graph,
            "advisories": advisories,
            "sidecar_path": sidecar_str,
            "node_count": len(graph.get("nodes", [])),
            "advisory_count": len(advisories),
            "blocker_count": blocker_count,
            "hint": (
                "Use buildgraph_status(run_id) to inspect node statuses. "
                "Review any advisory findings before executing — blockers indicate "
                "missing prerequisites or cycles."
            ),
            "next_actions": [
                f"buildgraph_status(run_id='{run_id}')",
            ],
        })

    @mcp.tool()
    def buildgraph_status(
        run_id: str,
        runs_dir_override: Optional[str] = None,
    ) -> str:
        """Return the current node statuses of a buildgraph run.

        Reads the sidecar file written by buildgraph_plan.  Restart-safe.

        Args:
            run_id:            UUID4 returned by buildgraph_plan.
            runs_dir_override: Override the default runs directory.

        Returns:
            {ok, run_id, schema, mission, nodes, advisory_count}
            Each node entry: {id, kind, label, status, requires, why}.
            On unknown run: {ok: false, reason, hint}.
        """
        base = (
            Path(runs_dir_override) if runs_dir_override
            else (_base_runs_dir or _runs_dir())
        )
        data = _read_sidecar(run_id, base)
        if data is None:
            return _err(
                f"Build graph run {run_id!r} not found",
                hint=(
                    f"Check run_id (from buildgraph_plan) and runs_dir "
                    f"(default: {base})."
                ),
            )

        graph = data.get("graph", {})
        advisories = data.get("advisories", [])
        nodes = graph.get("nodes", [])

        node_summaries = [
            {
                "id":       n.get("id"),
                "kind":     n.get("kind"),
                "label":    n.get("label"),
                "status":   n.get("status", "pending"),
                "requires": n.get("requires", []),
                "why":      n.get("why", ""),
            }
            for n in nodes
        ]

        return _dumps({
            "ok": True,
            "run_id": run_id,
            "schema": graph.get("schema", "aicad.buildgraph/1"),
            "mission": graph.get("mission", ""),
            "node_count": len(nodes),
            "nodes": node_summaries,
            "advisory_count": len(advisories),
            "next_actions": [
                f"# Start execution: iterate ready_nodes() from executor",
            ],
        })
