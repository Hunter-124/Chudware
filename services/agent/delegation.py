"""services/agent/delegation.py — Hierarchical delegation tree + approval chain.

Contract: contracts/advisor.md §12 (aicad.delegation/1).

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

``BudgetMeter``
    Human-visible tree-wide meter: tokens spent vs granted across ALL nodes, plus
    total agents spawned vs the config max.  Updated on every accrue + every new
    node.  Serialised in every ledger artifact flush so the GUI always has the
    current meter state.

Approval chain (contract §12)
    - depth-1 from the orchestrator: auto-approved.
    - kind='ponder' at any depth: auto-approved (read-only + hard token budget).
    - deeper worker: requires the mission overseer's approval (trigger=manual,
      scope=delegation) or human, per approval_policy in routing.yaml.

Recursion rails
    Hard nesting cap is ``agent.max_subagent_depth`` (config.json, default 2;
    root chat = depth 0). Spawns whose child depth would exceed the cap raise
    ``DelegationError`` with ``max subagent nesting depth (N) reached``.
    Additional rails:
      1. ``soft_depth_limit`` (config, default 8): reaching or exceeding this depth
         emits a ``delegation_depth_warning`` advisory event and allows the spawn
         (until the hard nesting cap refuses).
         The overseer-approval requirement at depth>1 is the human-visible gate.
      2. ``max_agents`` (config, default 64): reaching this count emits a
         ``delegation_agent_limit_warning`` event and allows the spawn.  The kill-
         switch is the human-controlled stop.
      3. ``budget_meter``: tree-wide token spend vs grant + agent count, always
         visible in the artifact.  Token exhaustion of any ancestor auto-revokes
         the subtree (unchanged from pre-T-DP9).
      4. ``kill()`` method: human-visible kill-switch — revokes the whole tree from
         root, sets ``killed=True``, and rejects all subsequent spawn requests.

    ``DEPTH_CAP`` is kept as a module-level constant for backward compatibility with
    any external readers (e.g. ``agent_inspector_view.h``).  The active hard nesting
    limit is ``max_subagent_depth`` (from agent.max_subagent_depth).

Budget inheritance (contract §12)
    Child spend accrues to ALL ancestors (single counter tree).  The harness calls
    ``accrue(node_id, tokens)`` on the tree; when any ancestor's remaining budget
    hits zero, the subtree is automatically revoked.  Revocation produces a ledger
    event with kind='delegation_revoked'.

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

# DEPTH_CAP is kept for backward compatibility (artifact readers, inspector UI).
# Soft advisory limit remains soft_depth_limit (config / routing.yaml).
# Hard nesting cap is agent.max_subagent_depth (config.json, default 2) —
# layers of subagent-spawning-subagent; root chat = depth 0.
DEPTH_CAP = 3

# Default soft limits — configurable via routing.yaml ``delegation:`` block or
# DelegationTree constructor kwargs (soft_depth_limit / max_agents).
DEFAULT_SOFT_DEPTH_LIMIT: int = 8
DEFAULT_MAX_AGENTS: int = 64
# Default hard nesting depth (agent.max_subagent_depth). 2 = root may spawn
# depth-1, those may spawn depth-2; depth-2 may not spawn further.
DEFAULT_MAX_SUBAGENT_DEPTH: int = 2

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
class BudgetMeter:
    """Human-visible tree-wide budget meter (T-DP9).

    ``tokens_granted``  — root orchestrator grant.
    ``tokens_spent``    — total tokens accrued across all nodes (mirrors root.budget.tokens_spent).
    ``agents_spawned``  — total non-root delegation nodes ever created in this tree.
    ``max_agents``      — config soft cap on agent count (advisory, not enforced hard).
    ``soft_depth_limit``— config advisory depth threshold; exceeding emits a warning event.
    ``max_subagent_depth`` — hard nesting cap (agent.max_subagent_depth); spawn past it refuses.
    ``killed``          — True after kill() has been called; all subsequent spawns are rejected.
    """
    tokens_granted: int
    tokens_spent: int = 0
    agents_spawned: int = 0
    max_agents: int = DEFAULT_MAX_AGENTS
    soft_depth_limit: int = DEFAULT_SOFT_DEPTH_LIMIT
    max_subagent_depth: int = DEFAULT_MAX_SUBAGENT_DEPTH
    killed: bool = False

    @property
    def tokens_remaining(self) -> int:
        return self.tokens_granted - self.tokens_spent

    @property
    def agents_remaining(self) -> int:
        return max(0, self.max_agents - self.agents_spawned)

    @property
    def token_pct(self) -> float:
        """0.0–1.0 fraction of total token budget consumed."""
        if self.tokens_granted <= 0:
            return 0.0
        return min(1.0, self.tokens_spent / self.tokens_granted)

    @property
    def agent_pct(self) -> float:
        """0.0–1.0 fraction of agent budget consumed."""
        if self.max_agents <= 0:
            return 0.0
        return min(1.0, self.agents_spawned / self.max_agents)

    def to_dict(self) -> dict:
        return {
            "tokens_granted": self.tokens_granted,
            "tokens_spent": self.tokens_spent,
            "tokens_remaining": self.tokens_remaining,
            "token_pct": round(self.token_pct, 4),
            "agents_spawned": self.agents_spawned,
            "agents_remaining": self.agents_remaining,
            "max_agents": self.max_agents,
            "agent_pct": round(self.agent_pct, 4),
            "soft_depth_limit": self.soft_depth_limit,
            "max_subagent_depth": self.max_subagent_depth,
            "killed": self.killed,
        }


@dataclass
class DelegationNode:
    """One node in the delegation tree.

    *id* is always a ``dg_<hex>`` string — explicit, never shared-mutable.
    *parent* is None for the root orchestrator node.
    *effort*: optional low|medium|high assigned by the dispatcher for Auto
              thinking on the subagent role (None = harness judges).
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
    effort: Optional[str] = None  # low|medium|high when dispatcher assigns

    def to_dict(self) -> dict:
        d = {
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
        if self.effort:
            d["effort"] = self.effort
            d["thinking"] = self.effort
            d["reasoning_effort"] = self.effort
        return d


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
    T-DP9 rails + hard nesting cap:
      ``max_subagent_depth`` — hard cap from agent.max_subagent_depth (default 2).
          Spawns whose child depth would exceed this raise DelegationError with a
          clear refusal: "max subagent nesting depth (N) reached".
      ``soft_depth_limit`` — depth at which a ``delegation_depth_warning`` advisory
          event is emitted; spawn is still allowed.  Default: DEFAULT_SOFT_DEPTH_LIMIT.
      ``max_agents`` — agent count at which a ``delegation_agent_limit_warning``
          advisory event is emitted; spawn is still allowed.  Default: DEFAULT_MAX_AGENTS.
      ``kill()`` — kills the whole tree: revokes all active nodes from root and sets
          ``_meter.killed = True`` so subsequent spawns are rejected.
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        orchestrator_budget: int = 50_000,
        ledger: Any | None = None,
        routing: RoutingTable | None = None,
        overseer: Any | None = None,
        soft_depth_limit: int = DEFAULT_SOFT_DEPTH_LIMIT,
        max_agents: int = DEFAULT_MAX_AGENTS,
        max_subagent_depth: int | None = None,
        on_event: Any | None = None,
    ) -> None:
        self._run_id = run_id or f"r_{uuid.uuid4().hex[:8]}"
        self._ledger = ledger
        self._routing = routing
        self._overseer = overseer
        self._nodes: dict[str, DelegationNode] = {}
        self._pending_events: list[dict] = []
        # M20: optional live event sink (GUI stream). When set, request_delegation
        # emits an `agent.spawn` (contract /2) with parent-linkage AND persists it to
        # the run's events.jsonl. Best-effort — a None sink is a no-op.
        self._on_event = on_event
        self._ev_journal = None
        if on_event is not None:
            try:
                from services.dag.event_journal import EventJournal  # noqa: PLC0415
                self._ev_journal = EventJournal(self._run_id)
            except Exception:  # noqa: BLE001
                self._ev_journal = None

        # Tree-wide budget meter (T-DP9) — always current, serialised in every flush.
        # Hard nesting cap: explicit kwarg > config.json agent.max_subagent_depth > default.
        if max_subagent_depth is None:
            try:
                from services.agent.config import get_max_subagent_depth  # noqa: PLC0415
                max_subagent_depth = get_max_subagent_depth()
            except Exception:  # noqa: BLE001
                max_subagent_depth = DEFAULT_MAX_SUBAGENT_DEPTH
        try:
            max_subagent_depth = max(0, int(max_subagent_depth))
        except (TypeError, ValueError):
            max_subagent_depth = DEFAULT_MAX_SUBAGENT_DEPTH
        self._meter = BudgetMeter(
            tokens_granted=orchestrator_budget,
            max_agents=max_agents,
            soft_depth_limit=soft_depth_limit,
            max_subagent_depth=max_subagent_depth,
        )

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
        effort: str | None = None,
    ) -> DelegationNode:
        """Request a new sub-agent delegation.

        *parent_id*: defaults to the root orchestrator node.
        *kind*: "worker" (default) or "ponder" (read-only, auto-approved at any depth).
        *docs*: list of ``{"doc_id": str, "lease": "write"|"read"}`` requests (T-W16
                request shape only — actual leasing lives in T-W16).
        *availability*: passed to routing for source availability probing.
        *effort*: optional low|medium|high reasoning effort for the subagent when
                 its role thinking is Auto (dispatcher assignment).

        Hard errors (DelegationError):
          - Invalid arguments (kind, role, brief, budget_tokens).
          - kill() has been called on this tree (killed=True).
          - Parent node not found or not in a spawnable status.
          - Requested budget exceeds any ancestor's total grant.
          - Child depth would exceed max_subagent_depth
            ("max subagent nesting depth (N) reached").

        Soft advisories (ledger events, spawn still proceeds):
          - depth >= soft_depth_limit  → ``delegation_depth_warning`` event.
          - agents_spawned >= max_agents → ``delegation_agent_limit_warning`` event.

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
        # The ≤500 cap is the RECORD shape (contract §12). Hard-rejecting the
        # whole spawn over it burned a turn and lost the spec; truncate the
        # stored row instead — the same soft-truncate precedent MissionDispatch
        # .delegate already applies before it calls us (mega-5).
        if len(brief) > 500:
            brief = brief[:499].rstrip() + "…"

        # Kill-switch guard (T-DP9): reject all spawns after kill().
        if self._meter.killed:
            raise DelegationError("delegation tree has been killed; no new sub-agents can be spawned")

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

        # Hard nesting cap (agent.max_subagent_depth): refuse spawns past the limit.
        # Root = depth 0; this limits LAYERS of subagent-spawning-subagent, not count.
        max_depth = int(self._meter.max_subagent_depth)
        if depth > max_depth:
            raise DelegationError(
                f"max subagent nesting depth ({max_depth}) reached "
                f"(attempted depth {depth}; root chat is depth 0)"
            )

        # Soft depth advisory (T-DP9): emit warning event but allow the spawn.
        if depth >= self._meter.soft_depth_limit:
            self._emit_event(
                actor=role,
                kind="delegation_depth_warning",
                payload={
                    "schema": DELEGATION_SCHEMA,
                    "depth": depth,
                    "soft_depth_limit": self._meter.soft_depth_limit,
                    "max_subagent_depth": max_depth,
                    "message": (
                        f"Delegation at depth {depth} reached soft limit "
                        f"{self._meter.soft_depth_limit}. "
                        "Overseer approval required; spawn is proceeding. "
                        "Use kill() to halt the tree if this is runaway."
                    ),
                },
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
        # Normalize optional dispatcher effort (low|medium|high).
        effort_s: str | None = None
        if effort is not None and str(effort).strip():
            e = str(effort).strip().lower()
            if e in ("med",):
                e = "medium"
            if e in ("low", "medium", "high"):
                effort_s = e
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
            effort=effort_s,
        )
        self._nodes[node_id] = node
        self._emit_transition(node, "pending", "delegation requested")

        # M20: stream an `agent.spawn` (contract /2 §3.2a) to the GUI + events.jsonl.
        # parent_id links to the SPAWNER span (the parent delegation node's id);
        # subagent_id is the new child node's own execution root. Best-effort.
        if self._on_event is not None or self._ev_journal is not None:
            try:
                from services.agent import agent_events as _ae  # noqa: PLC0415
                _k, _p = _ae.spawn_event(
                    parent_id, node_id, role, kind=kind, brief=brief,
                    budget_tokens=budget_tokens, span_id=node_id,
                    parent_id=parent_id, actor=parent.role)
                _ae.emit_event(_k, _p, live=self._on_event, journal=self._ev_journal)
            except Exception:  # noqa: BLE001 — spawn event never blocks delegation
                pass

        # Phase 4 (M11): every spawned node is also a registry session so the GUI
        # can list it, open its transcript, and queue steering messages into it.
        # Best-effort — a registry failure never blocks delegation.
        try:
            from services.agent import subagent_registry as _sreg  # noqa: PLC0415
            _sreg.register_session(
                node_id, role=role, brief=brief, spawn_kind=kind,
                parent_run_id=self._run_id, parent_node_id=parent_id,
                requested_by=parent.role,
                model=(model_assign.id or ""), budget_tokens=budget_tokens,
                status="pending")
        except Exception:  # noqa: BLE001
            pass

        # Update meter agent count BEFORE the agent-limit advisory so the emitted
        # event reflects the post-spawn count.
        self._meter.agents_spawned += 1
        if self._meter.agents_spawned >= self._meter.max_agents:
            self._emit_event(
                actor=role,
                kind="delegation_agent_limit_warning",
                payload={
                    "schema": DELEGATION_SCHEMA,
                    "agents_spawned": self._meter.agents_spawned,
                    "max_agents": self._meter.max_agents,
                    "message": (
                        f"Agent count {self._meter.agents_spawned} reached soft limit "
                        f"{self._meter.max_agents}. "
                        "Spawn is proceeding. Use kill() to halt the tree if this is runaway."
                    ),
                },
            )

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

        The tree-wide budget meter is synchronised to the root's spent counter so
        the human-visible meter always reflects the true total.
        """
        if tokens <= 0:
            return
        # Accrue to the node itself and all ancestors.
        chain = self._ancestors(node_id, inclusive=True)
        for anc in chain:
            anc.budget.tokens_spent += tokens

        # Sync meter to root's authoritative spent counter (root accumulates all spend).
        root = self._nodes.get(self._root_id)
        if root is not None:
            self._meter.tokens_spent = root.budget.tokens_spent

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

    def kill(self) -> None:
        """Human kill-switch (T-DP9): halt the entire delegation tree.

        Revokes all active nodes from the root down (cascading), sets
        ``meter.killed = True`` so every subsequent ``request_delegation``
        call raises ``DelegationError`` immediately.

        Emits a ``delegation_killed`` ledger event with the current meter
        snapshot so the human-visible log shows when and why the tree was
        stopped.

        Safe to call more than once — a tree that is already killed is a no-op.
        """
        if self._meter.killed:
            return  # idempotent
        self._meter.killed = True
        # Revoke the whole tree from root down.
        self._revoke_subtree(self._root_id, reason="kill-switch activated")
        self._emit_event(
            actor="harness",
            kind="delegation_killed",
            payload={
                "schema": DELEGATION_SCHEMA,
                "run_id": self._run_id,
                "budget_meter": self._meter.to_dict(),
                "message": "Delegation tree killed by harness/human kill-switch.",
            },
        )
        self._flush_artifact()

    @property
    def budget_meter(self) -> BudgetMeter:
        """The live tree-wide budget meter (read-only view).

        Callers must NOT mutate this object — it is the tree's internal state.
        Use ``accrue()`` / ``kill()`` / ``request_delegation()`` to drive changes.
        """
        return self._meter

    def to_artifact(self) -> dict:
        """Serialize the tree to the aicad.delegation/1 ledger artifact shape.

        T-DP9 additive fields (backward-compatible):
          ``budget_meter``     — tree-wide token+agent meter snapshot.
          ``soft_depth_limit`` — config advisory depth threshold.
          ``max_subagent_depth`` — hard nesting cap (agent.max_subagent_depth).
          ``killed``           — True after kill() was called.

        ``depth_cap`` is retained for backward compatibility with artifact
        readers (e.g. agent_inspector_view.h).  Readers that want the active
        hard nesting limit should use ``max_subagent_depth``.
        """
        return {
            "schema": DELEGATION_SCHEMA,
            "run_id": self._run_id,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "depth_cap": DEPTH_CAP,               # kept for backward compat
            "soft_depth_limit": self._meter.soft_depth_limit,
            "max_subagent_depth": self._meter.max_subagent_depth,
            "budget_meter": self._meter.to_dict(),
            "killed": self._meter.killed,
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
            if node.parent is not None:
                try:
                    from services.agent import subagent_registry as _sreg  # noqa: PLC0415
                    _sreg.set_status(node.id, "revoked", note=reason)
                except Exception:  # noqa: BLE001
                    pass

    def _transition(self, node: DelegationNode, new_status: str, note: str) -> None:
        node.status = new_status
        self._emit_transition(node, new_status, note)
        # Phase 4 (M11): mirror node status onto the sub-agent session registry so the
        # GUI's list/viewer stays truthful.  Root has no session; best-effort.
        if node.parent is not None:
            try:
                from services.agent import subagent_registry as _sreg  # noqa: PLC0415
                _sreg.set_status(node.id, new_status, note=note)
            except Exception:  # noqa: BLE001
                pass

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
    effort: str | None = None,
) -> dict:
    """The ``delegate`` tool exposed to role cards per contract §12 + roles.py allowlist.

    Validates and delegates via *tree*.  Returns a serialised node dict on success or
    an error dict on failure (the tool never raises — errors surface in the result so
    the calling role can decide to retry, replan, or escalate).

    *effort* (optional): low|medium|high — when the subagent role's Settings thinking
    is Auto, the dispatcher uses this as the assigned reasoning effort. Omit to let
    the harness judge complexity (default medium).

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
            effort=effort,
        )
        return node.to_dict()
    except DelegationError as exc:
        return {"error": str(exc), "status": "failed"}



# ---------------------------------------------------------------------------
# MCP tool registration (optional thin wrapper for the in-app path)
# ---------------------------------------------------------------------------

def register_delegation_tools(registry: Any, tree: DelegationTree | None = None) -> None:
    """Register delegation tools with an MCP tool registry (T-DP9).

    The registry is the one from services/mcp/tool_registry.py.  This makes
    ``delegate`` and ``kill_delegation_tree`` real registered tools.

    *tree* is optional — when None, the tools return an error asking the caller
    to provide a tree via the mission context.  The actual mission loop injects
    the live tree through the context mechanism.

    Registered tools:
      ``delegate``            — spawn a sub-agent (hard-capped by max_subagent_depth).
      ``kill_delegation_tree``— human kill-switch: halt the whole tree immediately.
      ``delegation_budget``   — read the live budget meter (tokens + agent count).
    """
    def _delegate_tool(
        role: str,
        brief: str,
        budget_tokens: int,
        kind: str = "worker",
        parent_id: Optional[str] = None,
        docs: Optional[list] = None,
        effort: Optional[str] = None,
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
            effort=effort,
        )

    def _kill_tree_tool() -> dict:
        if tree is None:
            return {"error": "kill_delegation_tree: no delegation tree in this context",
                    "status": "failed"}
        try:
            tree.kill()
            return {"killed": True, "budget_meter": tree.budget_meter.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "status": "failed"}

    def _budget_tool() -> dict:
        if tree is None:
            return {"error": "delegation_budget: no delegation tree in this context",
                    "status": "failed"}
        return tree.budget_meter.to_dict()

    try:
        registry.register(
            "delegate",
            _delegate_tool,
            description=(
                "Spawn a sub-agent role with an isolated brief and token budget. "
                "Nesting is hard-capped by agent.max_subagent_depth (default 2; "
                "root chat = depth 0). Spawns past the limit refuse with "
                "'max subagent nesting depth (N) reached'. "
                "depth-1 sub-agents are auto-approved; deeper ones require overseer approval. "
                "'ponder' kind is auto-approved at any allowed depth (read-only + budget-bounded). "
                "A depth_warning or agent_limit_warning advisory is emitted when soft limits "
                "are reached, but the spawn still proceeds (until the hard nesting cap). "
                "Optional 'effort' (low|medium|high): when the subagent role's Thinking is "
                "Auto, the dispatcher uses this as reasoning effort; omit to let the harness "
                "judge complexity (default medium). "
                "Returns the delegation node dict including status and model assignment."
            ),
            parameters={
                "type": "object",
                "required": ["role", "brief", "budget_tokens"],
                "properties": {
                    "role": {"type": "string", "description": "Role id to spawn"},
                    "brief": {"type": "string", "maxLength": 20000,
                              "description": (
                                  "Self-contained assignment for the sub-agent. It CANNOT see "
                                  "your conversation, so include every requirement, dimension "
                                  "and acceptance criterion it needs. Delivered to the child in "
                                  "full; the delegation record keeps a 500-char summary.")},
                    "budget_tokens": {"type": "integer",
                                      "description": "Token budget for the sub-agent"},
                    "kind": {"type": "string", "enum": ["worker", "ponder"],
                             "description": "worker (default) or ponder (read-only, any depth)"},
                    "parent_id": {"type": "string",
                                  "description": "Parent node id (defaults to orchestrator root)"},
                    "docs": {"type": "array",
                             "description": "Doc lease requests [{doc_id, lease}] (T-W16)"},
                    "effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "Optional reasoning effort for the subagent when its role "
                            "Thinking is Auto. Omit for harness judgment (default medium)."
                        ),
                    },
                },
            },
        )
        registry.register(
            "kill_delegation_tree",
            _kill_tree_tool,
            description=(
                "Human kill-switch: immediately revoke the entire delegation tree from root, "
                "halt all active sub-agents, and prevent any new spawns. "
                "Use when the delegation tree is suspected to be runaway. "
                "Returns the final budget meter snapshot."
            ),
            parameters={"type": "object", "required": [], "properties": {}},
        )
        registry.register(
            "delegation_budget",
            _budget_tool,
            description=(
                "Return the live tree-wide budget meter: tokens spent/granted/remaining, "
                "agents spawned/remaining, soft_depth_limit, max_agents, and killed status."
            ),
            parameters={"type": "object", "required": [], "properties": {}},
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
