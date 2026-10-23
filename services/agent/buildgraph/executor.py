"""executor.py — Topological executor for aicad.buildgraph/1 graphs.

Contract: contracts/advisor.md §13 (FROZEN).

This is the FINE-GRAINED executor for modeling jobs — NOT the mission executor
(services/dag/executor.py uses LangGraph + SQLite).  BuildGraphExecutor is a
plain in-memory topo walk with per-node status tracking and a ready_nodes()
surface for multi-doc fan-out.

Key design decisions
--------------------
* No LangGraph, no SQLite here — this runs inside a modeling job.
* ready_nodes() returns the current parallel frontier (nodes whose requires are
  all "done") — a fan-out caller uses this to assign sub-agents.
* Failed nodes block their dependents; dependents are marked "skipped" with a
  reason string in status_detail.
* Status transitions:  pending → building → done | failed
                       pending → skipped  (when a required node failed)
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_PENDING  = "pending"
STATUS_BUILDING = "building"
STATUS_DONE     = "done"
STATUS_FAILED   = "failed"
STATUS_SKIPPED  = "skipped"

TERMINAL_STATUSES = frozenset([STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED])

# ---------------------------------------------------------------------------
# Emit callback type
# ---------------------------------------------------------------------------

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]


# ---------------------------------------------------------------------------
# BuildGraphExecutor
# ---------------------------------------------------------------------------

class BuildGraphExecutor:
    """Walk an aicad.buildgraph/1 graph in topological order.

    Usage
    -----
      executor = BuildGraphExecutor(graph, emit=my_emit)
      while not executor.is_done():
          ready = executor.ready_nodes()          # parallel-ready set
          for node_id in ready:
              executor.start_node(node_id)        # marks building
          # ... do work on the nodes ...
          executor.finish_node(node_id, success=True)  # marks done/failed

    A multi-doc fan-out caller iterates ready_nodes() and dispatches sub-agents.
    """

    def __init__(
        self,
        graph: dict[str, Any],
        emit: EmitFn = None,
    ) -> None:
        self._graph = graph
        self._emit = emit
        self._nodes: dict[str, dict[str, Any]] = {
            n["id"]: n for n in graph.get("nodes", [])
        }
        # Mutable status map (separate from node dicts so originals stay pure)
        self._status: dict[str, str] = {
            nid: STATUS_PENDING for nid in self._nodes
        }
        self._status_detail: dict[str, str] = {}
        self._output: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def node_status(self, node_id: str) -> str:
        """Return the current status of a node."""
        return self._status.get(node_id, STATUS_PENDING)

    def all_statuses(self) -> dict[str, str]:
        """Return a snapshot of {node_id: status} for all nodes."""
        return dict(self._status)

    def ready_nodes(self) -> list[str]:
        """Return ids of nodes that are ready to start (parallel frontier).

        A node is ready when:
          - Its status is "pending"
          - All its requires[] are in TERMINAL_STATUSES and specifically "done"
            (failed/skipped required nodes cause the dependents to be skipped,
             not made ready)
        """
        ready: list[str] = []
        for nid, node in self._nodes.items():
            if self._status[nid] != STATUS_PENDING:
                continue
            reqs = node.get("requires", [])
            if all(self._status.get(r) == STATUS_DONE for r in reqs):
                ready.append(nid)
        return ready

    def is_done(self) -> bool:
        """True when every node has reached a terminal status."""
        return all(s in TERMINAL_STATUSES for s in self._status.values())

    def is_succeeded(self) -> bool:
        """True when every node is done (no failures or skips)."""
        return all(s == STATUS_DONE for s in self._status.values())

    def failed_nodes(self) -> list[str]:
        """Return ids of nodes with status "failed"."""
        return [nid for nid, s in self._status.items() if s == STATUS_FAILED]

    def skipped_nodes(self) -> list[str]:
        """Return ids of nodes with status "skipped"."""
        return [nid for nid, s in self._status.items() if s == STATUS_SKIPPED]

    def summary(self) -> dict[str, Any]:
        """Return a serialisable run summary."""
        return {
            "schema": "aicad.buildgraph/1",
            "mission": self._graph.get("mission", ""),
            "is_done": self.is_done(),
            "is_succeeded": self.is_succeeded(),
            "node_statuses": self.all_statuses(),
            "status_detail": dict(self._status_detail),
            "ready": self.ready_nodes(),
            "failed": self.failed_nodes(),
            "skipped": self.skipped_nodes(),
        }

    # ------------------------------------------------------------------
    # Transition API
    # ------------------------------------------------------------------

    def start_node(self, node_id: str) -> None:
        """Mark a node as building.  Only valid from pending state."""
        if self._status.get(node_id) != STATUS_PENDING:
            raise ValueError(
                f"start_node({node_id!r}): node is {self._status.get(node_id)!r}, "
                "expected 'pending'."
            )
        self._status[node_id] = STATUS_BUILDING
        self._emit_event("buildgraph.node.started", node_id)

    def finish_node(
        self,
        node_id: str,
        success: bool,
        output: Optional[dict[str, Any]] = None,
        detail: str = "",
    ) -> None:
        """Mark a node done or failed, then propagate skips to blocked dependents.

        Args:
            node_id: The node to finish.
            success: True → done; False → failed.
            output:  Optional output dict stored against the node.
            detail:  Human-readable detail for status_detail (shown in advisory).
        """
        if self._status.get(node_id) not in (STATUS_BUILDING, STATUS_PENDING):
            raise ValueError(
                f"finish_node({node_id!r}): node is {self._status.get(node_id)!r}, "
                "expected 'building' or 'pending'."
            )

        new_status = STATUS_DONE if success else STATUS_FAILED
        self._status[node_id] = new_status
        if detail:
            self._status_detail[node_id] = detail
        if output:
            self._output[node_id] = output

        event = "buildgraph.node.done" if success else "buildgraph.node.failed"
        self._emit_event(event, node_id, detail=detail)

        if not success:
            self._propagate_skips(node_id)

    def skip_node(self, node_id: str, reason: str = "") -> None:
        """Mark a node skipped (e.g. because a prerequisite failed)."""
        self._status[node_id] = STATUS_SKIPPED
        self._status_detail[node_id] = reason or "prerequisite failed"
        self._emit_event("buildgraph.node.skipped", node_id, detail=reason)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _propagate_skips(self, failed_id: str) -> None:
        """Recursively skip all pending nodes that depend on failed_id."""
        for nid, node in self._nodes.items():
            if self._status[nid] != STATUS_PENDING:
                continue
            if failed_id in node.get("requires", []):
                reason = f"skipped because required node {failed_id!r} failed"
                self.skip_node(nid, reason)
                # Recurse so chains of dependents are all skipped
                self._propagate_skips(nid)

    def _emit_event(self, event_type: str, node_id: str, **extra: Any) -> None:
        if self._emit is None:
            return
        payload: dict[str, Any] = {
            "mission": self._graph.get("mission", ""),
            "node_id": node_id,
            "status":  self._status.get(node_id, ""),
        }
        payload.update(extra)
        try:
            self._emit(event_type, payload)
        except Exception:  # noqa: BLE001
            pass  # emit errors never crash the executor


# ---------------------------------------------------------------------------
# Convenience: run a graph synchronously with a callable per node
# ---------------------------------------------------------------------------

def run_sync(
    graph: dict[str, Any],
    node_fn: Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]],
    emit: EmitFn = None,
) -> BuildGraphExecutor:
    """Execute a buildgraph synchronously (single-process, sequential).

    node_fn(node_dict) -> (success: bool, output: dict)

    Returns the executor so the caller can inspect statuses.
    This is the simple path; fan-out callers use ready_nodes() directly.
    """
    ex = BuildGraphExecutor(graph, emit=emit)
    while not ex.is_done():
        ready = ex.ready_nodes()
        if not ready:
            # No ready nodes but not done — shouldn't happen after validation
            # (would indicate a cycle the validator missed).
            break
        for nid in ready:
            node = ex._nodes[nid]
            ex.start_node(nid)
            try:
                success, output = node_fn(node)
            except Exception as e:  # noqa: BLE001
                success, output = False, {"error": str(e)}
            ex.finish_node(nid, success=success, output=output)
    return ex
