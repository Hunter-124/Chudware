"""workspace.py — Multi-document agent workspace.

T-W16: doc leases, buildgraph fan-out, per-doc event streams.
Contract: contracts/advisor.md §12 (delegation docs/lease field) §13 (buildgraph).
Architecture: docs/ADVISOR_ARCHITECTURE.md §2.14.

Key design decisions (locked)
------------------------------
* Write leases are single-holder per doc.  Read leases are unlimited.
* Steal is HARNESS-ONLY — agents never call steal() directly.  A model trying
  steal() must be rejected (WorkspaceError raised at the call site).
* Every lease transition is a ledger event (kind="workspace_lease") so the GUI
  (T-W17) can replay the full ownership history.
* Per-doc event streams are append-only JSONL files under the run dir:
      <cache>/runs/<run_id>/docs/<doc_id>/events.jsonl
  Each line is one JSON event.  The GUI polls or tails these files; no engine
  coupling required here (T-W17 concern).
* Fan-out: BuildGraphExecutor.ready_nodes() drives parallel part dispatch.
  T-W14 delegation is behind a TODO seam (not yet merged); this module spawns
  roles directly via a simple callable until that seam closes.
* workspace_status() is the single-call poll target for the GUI until a push
  channel exists.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from services.agent.interaction import cache_root

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEASE_WRITE = "write"
LEASE_READ  = "read"

EVENT_OPENED        = "doc.opened"
EVENT_CLOSED        = "doc.closed"
EVENT_OP_APPLIED    = "doc.op_applied"
EVENT_REBUILT       = "doc.rebuilt"
EVENT_RENDERED      = "doc.rendered"
EVENT_ADVISORY      = "doc.advisory"
EVENT_LEASE_CHANGE  = "doc.lease_change"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WorkspaceError(Exception):
    """A workspace contract violation (bad lease, unknown doc, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _doc_stream_dir(run_id: str, doc_id: str) -> Path:
    """Return (creating) the per-doc event stream directory."""
    d = cache_root() / "runs" / run_id / "docs" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_stream_path(run_id: str, doc_id: str) -> Path:
    return _doc_stream_dir(run_id, doc_id) / "events.jsonl"


# ---------------------------------------------------------------------------
# Per-doc entry kept by the registry
# ---------------------------------------------------------------------------

@dataclass
class _DocRecord:
    doc_id: str
    write_owner: Optional[str] = None          # agent_id or None
    read_owners: set[str]      = field(default_factory=set)
    status: str                = "open"        # open | closed
    latest_render: Optional[str] = None        # artifact path


# ---------------------------------------------------------------------------
# LeaseRegistry
# ---------------------------------------------------------------------------

class LeaseRegistry:
    """Thread-safe write/read lease registry for a multi-doc workspace.

    Rules (contract §12, §2.14)
    ---------------------------
    * acquire_write(doc_id, agent_id): single-holder, raises if another agent
      already holds it — names the current holder in the error.
    * acquire_read(doc_id, agent_id): unlimited, always succeeds for open docs.
    * release(doc_id, agent_id): releases write or read lease.
    * steal(doc_id, agent_id, reason): HARNESS ONLY — forcibly revokes the
      current write holder and grants write to agent_id.  Never call from a
      model role.
    * All transitions emit a ledger event via the provided emit callback.
    """

    def __init__(self, run_id: str, emit: Optional[Callable] = None) -> None:
        self.run_id = run_id
        self._emit = emit          # callback(event_type, payload) or None
        self._lock = threading.Lock()
        self._docs: dict[str, _DocRecord] = {}

    # ------------------------------------------------------------------ open/close

    def open_doc(self, doc_id: str, owner: str, lease: str = LEASE_WRITE) -> None:
        """Register a new doc and immediately grant the opening agent a lease."""
        with self._lock:
            if doc_id in self._docs:
                raise WorkspaceError(f"doc {doc_id!r} is already open")
            rec = _DocRecord(doc_id=doc_id)
            self._docs[doc_id] = rec
        # Emit opened before acquiring so the stream has the event first
        self._emit_doc_event(doc_id, EVENT_OPENED, {"opener": owner, "lease": lease})
        if lease == LEASE_WRITE:
            self.acquire_write(doc_id, owner)
        else:
            self.acquire_read(doc_id, owner)

    def close_doc(self, doc_id: str, agent_id: str) -> None:
        """Mark a doc closed; release any leases held by agent_id."""
        with self._lock:
            rec = self._require_doc(doc_id)
            rec.status = "closed"
            rec.write_owner = None
            rec.read_owners.discard(agent_id)
        self._emit_doc_event(doc_id, EVENT_CLOSED, {"closed_by": agent_id})
        self._emit_lease_event(doc_id, "closed", agent_id, None)

    # ------------------------------------------------------------------ acquire / release

    def acquire_write(self, doc_id: str, agent_id: str) -> None:
        """Acquire a write lease.  Raises WorkspaceError naming the current holder."""
        with self._lock:
            rec = self._require_doc(doc_id)
            if rec.write_owner and rec.write_owner != agent_id:
                raise WorkspaceError(
                    f"doc {doc_id!r} write lease is held by {rec.write_owner!r}; "
                    f"agent {agent_id!r} was rejected.  "
                    f"Request a handoff via workspace_status() or wait for the owner to release."
                )
            rec.write_owner = agent_id
            # Also register as a read owner so is_reader() checks are consistent
            rec.read_owners.add(agent_id)
        self._emit_lease_event(doc_id, "write_acquired", agent_id, None)

    def acquire_read(self, doc_id: str, agent_id: str) -> None:
        """Acquire a read lease (unlimited holders)."""
        with self._lock:
            rec = self._require_doc(doc_id)
            rec.read_owners.add(agent_id)
        self._emit_lease_event(doc_id, "read_acquired", agent_id, None)

    def release(self, doc_id: str, agent_id: str) -> None:
        """Release write or read lease for agent_id."""
        with self._lock:
            rec = self._require_doc(doc_id)
            released_write = False
            if rec.write_owner == agent_id:
                rec.write_owner = None
                released_write = True
            rec.read_owners.discard(agent_id)
        kind = "write_released" if released_write else "read_released"
        self._emit_lease_event(doc_id, kind, agent_id, None)

    def steal(self, doc_id: str, new_owner: str, reason: str = "") -> None:
        """HARNESS-ONLY: revoke the current write lease and grant it to new_owner.

        This is the recovery path for dead/revoked agent owners — never called
        by model roles.  Emits a "write_stolen" lease event for audit trail.
        """
        with self._lock:
            rec = self._require_doc(doc_id)
            old_owner = rec.write_owner
            rec.write_owner = new_owner
            rec.read_owners.add(new_owner)
        self._emit_lease_event(doc_id, "write_stolen", new_owner, old_owner,
                               extra={"reason": reason})

    # ------------------------------------------------------------------ query

    def write_owner(self, doc_id: str) -> Optional[str]:
        """Return the current write owner or None."""
        with self._lock:
            rec = self._docs.get(doc_id)
            return rec.write_owner if rec else None

    def is_writer(self, doc_id: str, agent_id: str) -> bool:
        with self._lock:
            rec = self._docs.get(doc_id)
            return bool(rec and rec.write_owner == agent_id)

    def is_reader(self, doc_id: str, agent_id: str) -> bool:
        with self._lock:
            rec = self._docs.get(doc_id)
            return bool(rec and agent_id in rec.read_owners)

    def list_docs(self) -> list[dict]:
        """Snapshot of all doc records (for workspace_status)."""
        with self._lock:
            return [
                {
                    "doc_id": r.doc_id,
                    "status": r.status,
                    "write_owner": r.write_owner,
                    "read_owners": sorted(r.read_owners),
                    "latest_render": r.latest_render,
                }
                for r in self._docs.values()
            ]

    def set_latest_render(self, doc_id: str, artifact_path: str) -> None:
        """Record the latest render artifact path (contact sheet etc.)."""
        with self._lock:
            rec = self._docs.get(doc_id)
            if rec:
                rec.latest_render = artifact_path

    # ------------------------------------------------------------------ internal

    def _require_doc(self, doc_id: str) -> _DocRecord:
        """Return doc record; caller MUST hold self._lock."""
        rec = self._docs.get(doc_id)
        if rec is None:
            raise WorkspaceError(f"doc {doc_id!r} is not registered in this workspace")
        return rec

    def _emit_lease_event(
        self,
        doc_id: str,
        transition: str,
        agent_id: str,
        previous_owner: Optional[str],
        extra: Optional[dict] = None,
    ) -> None:
        payload: dict = {
            "doc_id": doc_id,
            "transition": transition,
            "agent_id": agent_id,
        }
        if previous_owner is not None:
            payload["previous_owner"] = previous_owner
        if extra:
            payload.update(extra)
        self._emit_doc_event(doc_id, EVENT_LEASE_CHANGE, payload)

    def _emit_doc_event(self, doc_id: str, event_type: str, payload: dict) -> None:
        event = {
            "ts": _now(),
            "event": event_type,
            **payload,
        }
        # Append to per-doc JSONL stream
        try:
            path = _doc_stream_path(self.run_id, doc_id)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # stream write errors never crash the workspace

        # Also call the optional external emit (ledger integration)
        if self._emit is not None:
            try:
                self._emit(event_type, {"run_id": self.run_id, **event})
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Per-doc event stream helpers (public API for callers)
# ---------------------------------------------------------------------------

def append_doc_event(
    run_id: str,
    doc_id: str,
    event_type: str,
    payload: dict,
    render_path: Optional[str] = None,
) -> None:
    """Append an event to the per-doc JSONL stream.

    This is the public API for the modeling loop to push progress events
    (op_applied, rebuilt, rendered, advisory) without going through the
    registry lock.  The registry owns lease events; callers own model events.
    """
    event: dict = {
        "ts": _now(),
        "event": event_type,
        "doc_id": doc_id,
        **payload,
    }
    if render_path:
        event["render_path"] = render_path
    try:
        path = _doc_stream_path(run_id, doc_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_doc_events(run_id: str, doc_id: str, since_seq: int = 0) -> list[dict]:
    """Read all events from a doc's JSONL stream starting at *since_seq*.

    Events are 0-indexed lines in the JSONL file.  Returns a list of parsed
    event dicts.  Returns [] if the stream doesn't exist yet.
    """
    path = _doc_stream_path(run_id, doc_id)
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i < since_seq:
                    continue
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return events


# ---------------------------------------------------------------------------
# Mutation guard hook (called from tool_registry._guard seam)
# ---------------------------------------------------------------------------

# Module-level registry reference — set by WorkspaceSession.bind_to_registry()
_active_registry: Optional["LeaseRegistry"] = None
_active_agent_id: Optional[str] = None

# Mutating tool families (anything not read-only needs a write lease)
_MUTATING_RISK_CLASSES = {"write", "spend", "external"}


def bind_mutation_guard(registry: "LeaseRegistry", agent_id: str) -> None:
    """Bind the active workspace registry to the dispatch guard.

    Called once per agent turn setup.  After this, check_write_lease() gates
    all mutating tool calls dispatched through tool_registry._guard.
    """
    global _active_registry, _active_agent_id
    _active_registry = registry
    _active_agent_id = agent_id


def unbind_mutation_guard() -> None:
    """Unbind the mutation guard (end of turn / session cleanup)."""
    global _active_registry, _active_agent_id
    _active_registry = None
    _active_agent_id = None


def check_write_lease(doc_id: str, tool_name: str) -> Optional[dict]:
    """Check write-lease for a mutating tool call against the active registry.

    Returns None (proceed) if:
      - No registry is bound (workspace guard disabled — safe default).
      - The calling agent holds the write lease.
    Returns a refusal dict if another agent holds the write lease, naming the
    current owner and how to request handoff.

    This function is called from the _guard() seam in tool_registry.py for
    tools that carry a doc_id argument.
    """
    reg = _active_registry
    agent = _active_agent_id
    if reg is None or agent is None:
        return None  # guard disabled — allow
    owner = reg.write_owner(doc_id)
    if owner is None or owner == agent:
        return None  # no one holds write, or we hold it — allow
    return {
        "ok": False,
        "reason": (
            f"doc {doc_id!r} write lease is held by {owner!r}; "
            f"agent {agent!r} is not permitted to mutate it."
        ),
        "hint": (
            f"Call workspace_status(run_id=...) to see the current ownership map. "
            f"The write holder ({owner!r}) must release the lease or the harness must "
            f"perform a steal() on owner death."
        ),
        "owner": owner,
    }


# ---------------------------------------------------------------------------
# Fan-out executor (T-W16 core)
# ---------------------------------------------------------------------------

# Type alias for the modeler spawn callable.
# Signature: spawn_modeler(node, doc_id, run_id) -> None
# Called once per ready part node; runs asynchronously (the caller is responsible
# for concurrency — threads, asyncio, subprocesses).
SpawnFn = Callable[[dict, str, str], Any]


def _default_spawn_modeler(node: dict, doc_id: str, run_id: str) -> None:
    """TODO T-W14 seam — direct role spawn placeholder.

    When T-W14 delegation lands, replace this with:
        delegate(role="modeler", doc_id=doc_id, brief=node.get("label",""),
                 run_id=run_id, node_id=node["id"])
    Until then this stub records the intent without running a real agent.
    """
    # Record that spawn was requested so callers can track in tests
    append_doc_event(run_id, doc_id, EVENT_ADVISORY, {
        "msg": f"[TODO T-W14] spawn modeler for node {node['id']!r} label={node.get('label','')!r}",
        "node_id": node["id"],
    })


def fanout_ready_nodes(
    executor,          # BuildGraphExecutor from services/agent/buildgraph/executor.py
    registry: "LeaseRegistry",
    run_id: str,
    orchestrator_id: str,
    spawn_modeler: Optional[SpawnFn] = None,
    *,
    join_kinds: tuple[str, ...] = ("mate", "verify", "checkpoint"),
) -> list[str]:
    """Dispatch currently-ready part nodes to per-doc sub-agents.

    For each ready node:
      - "part" nodes: open a new doc, acquire write lease, call spawn_modeler().
      - join_kinds (mate/verify/checkpoint): the orchestrator handles them
        directly (assembly doc single-writer model).
      - Other kinds (feature, op): fall through as-is (not part of the fan-out
        pattern; the orchestrator runs them inline or via another mechanism).

    Returns the list of node_ids dispatched (started via start_node).

    Contract (§13): failed nodes block their dependents via executor internals;
    this function does NOT retry.
    """
    if spawn_modeler is None:
        spawn_modeler = _default_spawn_modeler

    dispatched: list[str] = []
    ready = executor.ready_nodes()

    for node_id in ready:
        node = executor._nodes[node_id]
        kind = node.get("kind", "op")

        if kind == "part":
            # Each part gets its own doc
            doc_id = node.get("doc_id") or f"doc_{node_id}_{uuid.uuid4().hex[:8]}"
            node = dict(node)  # shallow copy so we can annotate
            node["doc_id"] = doc_id

            try:
                registry.open_doc(doc_id, orchestrator_id, lease=LEASE_WRITE)
            except WorkspaceError:
                # Doc already open (idempotent re-dispatch guard)
                pass

            executor.start_node(node_id)
            dispatched.append(node_id)

            append_doc_event(run_id, doc_id, EVENT_OPENED, {
                "node_id": node_id,
                "label": node.get("label", ""),
                "orchestrator": orchestrator_id,
            })

            try:
                spawn_modeler(node, doc_id, run_id)
            except Exception as exc:  # noqa: BLE001
                # spawn failure = node failed immediately
                executor.finish_node(node_id, success=False,
                                     detail=f"spawn failed: {exc}")
                registry.release(doc_id, orchestrator_id)

        elif kind in join_kinds:
            # Join nodes: orchestrator runs them in the assembly doc context
            executor.start_node(node_id)
            dispatched.append(node_id)
            # The orchestrator is expected to call finish_node after its work

    return dispatched


# ---------------------------------------------------------------------------
# WorkspaceSession — top-level coordinator
# ---------------------------------------------------------------------------

class WorkspaceSession:
    """Coordinator for a multi-doc modeling session.

    One WorkspaceSession per modeling run.  The orchestrator agent creates it,
    opens the assembly doc, then calls dispatch_fanout() whenever the buildgraph
    executor has ready part nodes.

    Usage
    -----
        session = WorkspaceSession(run_id="r_abc")
        session.open_assembly_doc(assembly_doc_id, orchestrator_id)

        executor = BuildGraphExecutor(graph)
        while not executor.is_done():
            session.dispatch_fanout(executor, orchestrator_id)
            # ... wait for sub-agents to call finish_node via callbacks ...

        status = session.workspace_status()
    """

    def __init__(
        self,
        run_id: str,
        emit: Optional[Callable] = None,
        spawn_modeler: Optional[SpawnFn] = None,
    ) -> None:
        self.run_id = run_id
        self.registry = LeaseRegistry(run_id=run_id, emit=emit)
        self._spawn_modeler = spawn_modeler
        self._node_statuses: dict[str, dict] = {}  # node_id -> status snapshot

    def open_assembly_doc(self, doc_id: str, orchestrator_id: str) -> None:
        """Open the assembly (world) doc with write lease for the orchestrator."""
        self.registry.open_doc(doc_id, orchestrator_id, lease=LEASE_WRITE)

    def dispatch_fanout(
        self,
        executor,
        orchestrator_id: str,
    ) -> list[str]:
        """Dispatch all currently-ready nodes via fanout_ready_nodes().

        Returns the node_ids dispatched this cycle.
        """
        return fanout_ready_nodes(
            executor=executor,
            registry=self.registry,
            run_id=self.run_id,
            orchestrator_id=orchestrator_id,
            spawn_modeler=self._spawn_modeler,
        )

    def workspace_status(self, executor=None) -> dict:
        """Return a single-call status snapshot (GUI poll target).

        Includes: docs (owners, status, latest render), node statuses (if
        executor provided), run_id.
        """
        docs = self.registry.list_docs()
        result: dict = {
            "schema": "aicad.workspace/1",
            "run_id": self.run_id,
            "ts": _now(),
            "docs": docs,
        }
        if executor is not None:
            result["buildgraph"] = executor.summary()
        return result

    def record_render(self, doc_id: str, artifact_path: str) -> None:
        """Record a render artifact path for a doc (for workspace_status)."""
        self.registry.set_latest_render(doc_id, artifact_path)
        append_doc_event(self.run_id, doc_id, EVENT_RENDERED, {
            "artifact_path": artifact_path,
        }, render_path=artifact_path)


# ---------------------------------------------------------------------------
# Process-wide session registry (for workspace_status MCP tool lookup)
# ---------------------------------------------------------------------------

# run_id -> WorkspaceSession
_session_registry: dict[str, "WorkspaceSession"] = {}


def register_session(session: "WorkspaceSession") -> None:
    """Register a WorkspaceSession so workspace_status() can find it by run_id."""
    _session_registry[session.run_id] = session


def unregister_session(run_id: str) -> None:
    """Deregister a session on teardown."""
    _session_registry.pop(run_id, None)
