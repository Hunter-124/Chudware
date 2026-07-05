"""services/agent/delegation.py — Hierarchical delegation tree + approval chain.

Contract: contracts/advisor.md §12 (aicad.delegation/1, FROZEN).

What this module provides
--------------------------
``DelegationTree``
    The stateful tree of delegation nodes.  Lives as a ledger artifact of kind
    ``delegation`` (contract §12).  Every node transition is a ledger event.

``delegate(role, brief, budget, ...)``
    The callable exposed to roles as the ``delegate`` tool (contract §12 + roles.py
    FEATURE_FLAGGED_TOOLS).  Returns a request handle; the caller polls status.

``DelegationNode``
    One spawned sub-agent slot: role + model (resolved via routing) + brief +
    budget + docs lease requests (T-W16) + approval state.

Approval chain (contract §12)
    - depth-1 from the orchestrator: auto-approved.
    - kind='ponder' at any depth: auto-approved (read-only + hard token budget).
    - deeper worker: requires the mission overseer's approval (trigger=manual,
      scope=delegation) or human, per approval_policy in routing.yaml.

Budget inheritance (contract §12)
    Child spend accrues to ALL ancestors (single counter tree).  The harness calls
    ``accrue(node_id, tokens)`` on the tree; when any ancestor's remaining budget
    hits zero, the harness calls ``revoke(node_id)``.  Revocation produces a
    ledger event with kind='delegation_revoked'.

Sub-agent identities
    Every node gets an explicit ``dg_<hex>`` id.  No shared mutable defaults —
    each call to ``request_delegation`` returns a new node with its own identity.
    (Memory lesson: sub-agents get explicit dg_ ids on everything they write.)

Offline discipline
    The tree is deterministic around a pluggable ``RoutingTable`` and ``Ledger``.
    Tests inject mocks; no all-skip.

Doc leases
    The ``docs`` field carries lease requests for T-W16 (request shape only here).
    Shape: ``[{"doc_id": str, "lease": "write"|"read"}]``.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from services.agent.routing import Resolution, RoutingTable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DELEGATION_SCHEMA = "aicad.delegation/1"
DEPTH_CAP = 3  # contract §12: hard cap on delegation depth

# Node statuses (contract §12).
STATUSES = frozenset({"pending", "approved", "running", "done", "failed", "revoked"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DocLease:
    """A doc lease request (T-W16 implements the actual lease; this is the shape)."""
    doc_id: str
    lease: str  # "write" | "read"

    def to_dict(self) -> dict:
        return {"doc_id": self.doc_id, "lease": self.lease}


@dataclass
class ApprovalState:
    """Approval record for one delegation node."""
    policy: str          # "auto" | "overseer" | "human"
    approved_by: str     # role id / "auto" / "human" / ""
    ts: str              # ISO timestamp or ""


@dataclass
class ModelAssignment:
    """The model resolved for a delegation node."""
    provider: str
    id: str
    tier: str

    def to_dict(self) -> dict:
        return {"provider": self.provider, "id": self.id, "tier": self.tier}

    @classmethod
    def from_resolution(cls, res: Resolution) -> "ModelAssignment":
        return cls(provider=res.source_id, id=res.model_id, tier=res.model_tier)

    @classmethod
    def unresolved(cls) -> "ModelAssignment":
        return cls(provider="", id="", tier="")


@dataclass
class Budget:
    """Per-node token budget.  Children's spend accrues to this node too."""
    tokens_granted: int
    tokens_spent: int = 0
    attempts: int = 0

    @property
    def tokens_remaining(self) -> int:
        return self.tokens_granted - self.tokens_spent

    def to_dict(self) -> dict:
        return {
            "tokens_granted": self.tokens_granted,
            "tokens_spent": self.tokens_spent,
            "attempts": self.attempts,
        }


@dataclass
class DelegationNode:
    """One node in the delegation tree.

    *id* is always a ``dg_<hex>`` string — explicit, never shared-mutable.
    *parent* is None for the root orchestrator node.
    """
    id: str
    parent: Optional[str]
    role: str
    brief: str
    requested_by: str
    kind: str              # "worker" | "ponder"
    depth: int             # 0 = orchestrator root
    approval: ApprovalState
    model: ModelAssignment
    budget: Budget
    docs: list[DocLease] = field(default_factory=list)
    status: str = "pending"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent": self.parent,
            "role": self.role,
            "brief": self.brief,
            "requested_by": self.requested_by,
            "kind": self.kind,
            "depth": self.depth,
            "approval": {
                "policy": self.approval.policy,
                "approved_by": self.approval.approved_by,
                "ts": self.approval.ts,
            },
            "model": self.model.to_dict(),
            "budget": self.budget.to_dict(),
            "docs": [d.to_dict() for d in self.docs],
            "status": self.status,
        }


class DelegationError(ValueError):
    """A delegation request is invalid or cannot be granted."""


# ---------------------------------------------------------------------------
# DelegationTree
# ---------------------------------------------------------------------------

class DelegationTree:
    """Stateful delegation tree — the live registry of sub-agent nodes.

    Persisted as a ledger artifact of kind ``delegation`` (contract §12); every
    node status transition is a ledger event.

    The tree is initialised with a root orchestrator node (depth=0, auto-approved).
    All child nodes come through ``request_delegation()``.

    ``ledger`` and ``routing`` are injected (test-mockable).  Both may be None:
    a None ledger means events are captured in ``_pending_events`` only; a None
    routing means model assignment is deferred (unresolved).
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        orchestrator_budget: int = 50_000,
        ledger: Any | None = None,
        routing: RoutingTable | None = None,
        overseer: Any | None = None,
    ) -> None:
        self._run_id = run_id or f"r_{uuid.uuid4().hex[:8]}"
        self._ledger = ledger
        self._routing = routing
        self._overseer = overseer
        self._nodes: dict[str, DelegationNode] = {}
        self._pending_events: list[dict] = []

        # Root orchestrator node (depth=0).
        root_id = _fresh_id()
        self._root_id = root_id
        root = DelegationNode(
            id=root_id,
            parent=None,
            role="orchestrator",
            brief="Mission orchestrator (root)",
            requested_by="harness",
            kind="worker",
            depth=0,
            approval=ApprovalState(policy="auto", approved_by="auto", ts=_now()),
            model=ModelAssignment.unresolved(),
            budget=Budget(tokens_granted=orchestrator_budget),
            status="approved",
        )
        self._nodes[root_id] = root
        self._emit_transition(root, "approved", "root node auto-approved at init")
        self._flush_artifact()

    # -- public API ---------------------------------------------------------

    @property
    def root_id(self) -> str:
        return self._root_id

    def get_node(self, node_id: str) -> Optional[DelegationNode]:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[DelegationNode]:
        return list(self._nodes.values())

    def request_delegation(
        self,
        role: str,
        brief: str,
        budget_tokens: int,
        *,
        parent_id: str | None = None,
        kind: str = "worker",
        docs: list[dict] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> DelegationNode:
        """Request a new sub-agent delegation.

        *parent_id*: defaults to the root orchestrator node.
        *kind*: "worker" (default) or "ponder" (read-only, auto-approved at any depth).
        *docs*: list of ``{"doc_id": str, "lease": "write"|"read"}`` requests (T-W16
                request shape only — actual leasing lives in T-W16).
        *availability*: passed to routing for source availability probing.

        Raises ``DelegationError`` on depth cap exceeded or invalid arguments.
        Returns the newly created node (status pending, then transitioned to approved
        or awaiting overseer approval).
        """
        if kind not in ("worker", "ponder"):
            raise DelegationError(f"kind must be 'worker' or 'ponder', got {kind!r}")
        if not role:
            raise DelegationError("role must be non-empty")
        if not brief:
            raise DelegationError("brief must be non-empty")
        if budget_tokens <= 0:
            raise DelegationError(f"budget_tokens must be positive, got {budget_tokens}")
        if len(brief) > 500:
            raise DelegationError(f"brief must be ≤500 chars (contract §12), got {len(brief)}")

        parent_id = parent_id or self._root_id
        parent = self._nodes.get(parent_id)
        if parent is None:
            raise DelegationError(f"parent node {parent_id!r} not found in tree")
        if parent.status not in ("approved", "running"):
            raise DelegationError(
                f"parent node {parent_id!r} is in status {parent.status!r}; "
                "can only delegate from approved/running nodes"
            )

        depth = parent.depth + 1
        if depth > DEPTH_CAP:
            raise DelegationError(
                f"delegation depth {depth} exceeds cap {DEPTH_CAP} (contract §12)"
            )

        # Budget check: the requested budget must not exceed any ancestor's total grant.
        # Actual spend accrual (not grant) revokes nodes; this check prevents obviously
        # impossible grants (requesting more than the root was ever given).
        ancestors = self._ancestors(parent_id, inclusive=True)
        for anc in ancestors:
            if budget_tokens > anc.budget.tokens_granted:
                raise DelegationError(
                    f"ancestor node {anc.id!r} (role={anc.role}) total grant "
                    f"({anc.budget.tokens_granted}) < requested child budget ({budget_tokens})"
                )

        # Resolve model via routing.
        res = None
        if self._routing is not None:
            res = self._routing.resolve(role, availability=availability,
                                        ledger=self._ledger)
        model_assign = (ModelAssignment.from_resolution(res)
                        if res is not None else ModelAssignment.unresolved())

        # Build approval state.
        approval_policy_str = (
            "auto" if self._routing is None
            else self._routing.approval_for_depth(depth, kind=kind)
        )

        node_id = _fresh_id()
        doc_leases = [DocLease(doc_id=d["doc_id"], lease=d["lease"])
                      for d in (docs or []) if "doc_id" in d and "lease" in d]
        node = DelegationNode(
            id=node_id,
            parent=parent_id,
            role=role,
            brief=brief,
            requested_by=parent.role,
            kind=kind,
            depth=depth,
            approval=ApprovalState(policy=approval_policy_str,
                                   approved_by="", ts=""),
            model=model_assign,
            budget=Budget(tokens_granted=budget_tokens),
            docs=doc_leases,
            status="pending",
        )
        self._nodes[node_id] = node
        self._emit_transition(node, "pending", "delegation requested")

        # Approve or route to overseer.
        if approval_policy_str == "auto":
            self._approve(node, approved_by="auto")
        else:
            self._request_overseer_approval(node)

        self._flush_artifact()
        return node

    def approve(self, node_id: str, *, approved_by: str = "overseer") -> DelegationNode:
        """Externally approve a pending delegation (overseer or human approval).

        Only pending nodes can be approved.  The caller (mission loop or human relay)
        calls this after the overseer has approved the delegation request.
        """
        node = self._nodes.get(node_id)
        if node is None:
            raise DelegationError(f"node {node_id!r} not found")
        if node.status != "pending":
            raise DelegationError(
                f"node {node_id!r} is in status {node.status!r}; only pending nodes can be approved"
            )
        self._approve(node, approved_by=approved_by)
        self._flush_artifact()
        return node

    def start(self, node_id: str) -> DelegationNode:
        """Transition a node from approved → running."""
        node = self._nodes.get(node_id)
        if node is None:
            raise DelegationError(f"node {node_id!r} not found")
        if node.status != "approved":
            raise DelegationError(
                f"node {node_id!r} is in status {node.status!r}; only approved nodes can start"
            )
        self._transition(node, "running", "sub-agent started")
        self._flush_artifact()
        return node

    def complete(self, node_id: str, *, tokens_used: int = 0) -> DelegationNode:
        """Transition a node from running → done, accruing token spend."""
        node = self._nodes.get(node_id)
        if node is None:
            raise DelegationError(f"node {node_id!r} not found")
        if node.status != "running":
            raise DelegationError(
                f"node {node_id!r} is in status {node.status!r}; only running nodes can complete"
            )
        if tokens_used > 0:
            self.accrue(node_id, tokens_used)
        self._transition(node, "done", f"sub-agent completed (spent {tokens_used} tokens)")
        self._flush_artifact()
        return node

    def fail(self, node_id: str, *, reason: str = "") -> DelegationNode:
        """Transition a node to failed."""
        node = self._nodes.get(node_id)
        if node is None:
            raise DelegationError(f"node {node_id!r} not found")
        self._transition(node, "failed", f"sub-agent failed: {reason}")
        self._flush_artifact()
        return node

    def accrue(self, node_id: str, tokens: int) -> None:
        """Accrue token spend to *node_id* AND all its ancestors (contract §12).

        After accrual, any ancestor whose remaining budget hits zero (<=0) is
        automatically revoked — the harness never needs to poll manually.
        """
        if tokens <= 0:
            return
        # Accrue to the node itself and all ancestors.
        chain = self._ancestors(node_id, inclusive=True)
        for anc in chain:
            anc.budget.tokens_spent += tokens

        # Revoke any node in the chain (bottom-up) whose budget is exhausted.
        # Revoke the child first so ancestors know to cascade.
        for anc in reversed(chain):
            if anc.budget.tokens_remaining <= 0 and anc.status in ("approved", "running", "pending"):
                self._revoke_subtree(anc.id, reason="budget exhausted")

        self._flush_artifact()

    def revoke(self, node_id: str, *, reason: str = "explicit revocation") -> None:
        """Explicitly revoke a node and all its descendants."""
        self._revoke_subtree(node_id, reason=reason)
        self._flush_artifact()

    def to_artifact(self) -> dict:
        """Serialize the tree to the aicad.delegation/1 ledger artifact shape."""
        return {
            "schema": DELEGATION_SCHEMA,
            "run_id": self._run_id,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "depth_cap": DEPTH_CAP,
        }

    # -- internal helpers ---------------------------------------------------

    def _ancestors(self, node_id: str, *, inclusive: bool = False) -> list[DelegationNode]:
        """Return ancestors from immediate parent up to root (oldest last)."""
        result: list[DelegationNode] = []
        if inclusive:
            n = self._nodes.get(node_id)
            if n:
                result.append(n)
        cur = self._nodes.get(node_id)
        while cur and cur.parent:
            parent = self._nodes.get(cur.parent)
            if parent is None:
                break
            result.append(parent)
            cur = parent
        return result

    def _approve(self, node: DelegationNode, *, approved_by: str) -> None:
        node.approval.approved_by = approved_by
        node.approval.ts = _now()
        self._transition(node, "approved", f"approved by {approved_by}")

    def _request_overseer_approval(self, node: DelegationNode) -> None:
        """Emit a ledger event requesting overseer approval.

        The overseer (or human relay) calls ``approve(node_id)`` when it grants.
        The requesting node stays 'pending' — it blocks only itself, not siblings.
        """
        self._emit_event(
            actor=node.role,
            kind="delegation_approval_request",
            payload={
                "schema": DELEGATION_SCHEMA,
                "node_id": node.id,
                "role": node.role,
                "depth": node.depth,
                "kind": node.kind,
                "policy": node.approval.policy,
                "brief": node.brief,
                "message": (
                    f"Delegation approval requested: role={node.role!r} depth={node.depth} "
                    f"kind={node.kind!r}. Awaiting {node.approval.policy} approval."
                ),
            },
        )

    def _revoke_subtree(self, node_id: str, *, reason: str) -> None:
        """Revoke *node_id* and all its active descendants."""
        node = self._nodes.get(node_id)
        if node is None:
            return
        # Revoke all active children first.
        children = [n for n in self._nodes.values()
                    if n.parent == node_id and n.status in ("pending", "approved", "running")]
        for child in children:
            self._revoke_subtree(child.id, reason=f"ancestor {node_id!r} revoked ({reason})")
        if node.status in ("pending", "approved", "running"):
            self._emit_event(
                actor="harness",
                kind="delegation_revoked",
                payload={
                    "schema": DELEGATION_SCHEMA,
                    "node_id": node.id,
                    "role": node.role,
                    "reason": reason,
                    "budget": node.budget.to_dict(),
                },
            )
            node.status = "revoked"

    def _transition(self, node: DelegationNode, new_status: str, note: str) -> None:
        node.status = new_status
        self._emit_transition(node, new_status, note)

    def _emit_transition(self, node: DelegationNode, status: str, note: str) -> None:
        self._emit_event(
            actor=node.role,
            kind="delegation",
            payload={
                "schema": DELEGATION_SCHEMA,
                "node_id": node.id,
                "role": node.role,
                "status": status,
                "depth": node.depth,
                "kind": node.kind,
                "model": node.model.to_dict(),
                "budget": node.budget.to_dict(),
                "note": note,
            },
        )

    def _emit_event(self, actor: str, kind: str, payload: dict) -> None:
        if self._ledger is not None:
            try:
                self._ledger.append(actor, kind, payload)
            except Exception:  # noqa: BLE001 — ledger failure must not crash delegation
                self._pending_events.append({"actor": actor, "kind": kind, "payload": payload})
        else:
            self._pending_events.append({"actor": actor, "kind": kind, "payload": payload})

    def _flush_artifact(self) -> None:
        """Write/update the delegation artifact on the ledger."""
        if self._ledger is None:
            return
        try:
            art = self.to_artifact()
            self._ledger.add_artifact(
                "harness", "delegation",
                f"delegation_{self._run_id}",
                art,
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# ``delegate`` tool callable (exposed to roles per card policy)
# ---------------------------------------------------------------------------

def delegate(
    role: str,
    brief: str,
    budget: int,
    *,
    tree: DelegationTree,
    parent_id: str | None = None,
    kind: str = "worker",
    docs: list[dict] | None = None,
    availability: dict[str, bool] | None = None,
) -> dict:
    """The ``delegate`` tool exposed to role cards per contract §12 + roles.py allowlist.

    Validates and delegates via *tree*.  Returns a serialised node dict on success or
    an error dict on failure (the tool never raises — errors surface in the result so
    the calling role can decide to retry, replan, or escalate).

    Usage by a role card::

        result = delegate(role="sketcher", brief="...", budget=4000, tree=mission_tree)
        if result.get("status") == "approved":
            ...  # proceed with the sub-agent
        elif result.get("status") == "pending":
            ...  # awaiting overseer approval — proceed with sibling work
    """
    try:
        node = tree.request_delegation(
            role, brief, budget,
            parent_id=parent_id,
            kind=kind,
            docs=docs,
            availability=availability,
        )
        return node.to_dict()
    except DelegationError as exc:
        return {"error": str(exc), "status": "failed"}


# ---------------------------------------------------------------------------
# MCP tool registration (optional thin wrapper for the in-app path)
# ---------------------------------------------------------------------------

def register_delegation_tools(registry: Any, tree: DelegationTree | None = None) -> None:
    """Register the ``delegate`` tool with an MCP tool registry.

    The registry is the one from services/mcp/tool_registry.py.  This makes
    ``delegate`` a real registered tool so validate_role no longer flags it as
    feature-flagged.

    *tree* is optional — when None, the tool returns an error asking the caller
    to provide a tree via the mission context.  The actual mission loop injects
    the live tree through the context mechanism.
    """
    def _delegate_tool(
        role: str,
        brief: str,
        budget_tokens: int,
        kind: str = "worker",
        parent_id: Optional[str] = None,
        docs: Optional[list] = None,
    ) -> dict:
        if tree is None:
            return {"error": "delegate tool: no delegation tree available in this context",
                    "status": "failed"}
        return delegate(
            role, brief, budget_tokens,
            tree=tree,
            parent_id=parent_id,
            kind=kind,
            docs=docs,
        )

    try:
        registry.register(
            "delegate",
            _delegate_tool,
            description=(
                "Spawn a sub-agent role with an isolated brief and token budget. "
                "depth-1 sub-agents are auto-approved; deeper ones require overseer approval. "
                "'ponder' kind is auto-approved at any depth (read-only + budget-bounded). "
                "Returns the delegation node dict including status and model assignment."
            ),
            parameters={
                "type": "object",
                "required": ["role", "brief", "budget_tokens"],
                "properties": {
                    "role": {"type": "string", "description": "Role id to spawn"},
                    "brief": {"type": "string", "maxLength": 500,
                              "description": "Self-contained brief for the sub-agent (≤500 chars)"},
                    "budget_tokens": {"type": "integer",
                                      "description": "Token budget for the sub-agent"},
                    "kind": {"type": "string", "enum": ["worker", "ponder"],
                             "description": "worker (default) or ponder (read-only, any depth)"},
                    "parent_id": {"type": "string",
                                  "description": "Parent node id (defaults to orchestrator root)"},
                    "docs": {"type": "array",
                             "description": "Doc lease requests [{doc_id, lease}] (T-W16)"},
                },
            },
        )
    except Exception:  # noqa: BLE001 — registration failure must not crash startup
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_id() -> str:
    """Generate a fresh dg_<hex> delegation node id.  No shared mutable default."""
    return f"dg_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
