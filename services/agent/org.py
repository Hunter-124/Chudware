"""org.py — Organization runtime (aicad.org/1).

Contract: contracts/advisor.md §18 (FROZEN) + §12 ponder/kind amendment.
Pipeline: docs/design/COPILOT_MISSION_PIPELINE.md Phase 2.5 (ORGANIZE).

## What this module provides

``build_org_chart(manifest)``
    Cluster a deliverable manifest (aicad.deliverables/1) into departments per §18:
    each deliverable + its depends_on satellites → one department.  Returns the
    ``aicad.org/1`` org chart dict.  Deterministic, no LLM.

``OrgState``
    Stateful org-runtime: channels, requests, and report cadence.  All mutations
    emit ledger events (actor = dept id or "org").  Built on top of the ledger,
    the delegation seam, and the councils seam.

Channel rules (§18):
    * Auto-created from manifest ``depends_on`` edges + layout interfaces.
    * An ``interface_change`` message is MANDATORY before altering a dimension
      another department cites.
    * An unacked ``interface_change`` blocks the SENDER's stage exit (not the send).

Request rules (§18):
    * Every request (aicad.request/1) gets an explicit resolution — grant, deny-
      with-reason, or escalate.  Silent expiry is STRUCTURALLY IMPOSSIBLE: no code
      path removes a request without recording a ``resolution``.
    * Unresolved ``concern`` requests older than one manager turn auto-surface in
      the overseer's next audit context (concern aging).

Report cadence:
    * Every N node completions (config: ORG_REPORT_EVERY, default 3), a structured
      department report is emitted as a ledger event.

Overseer feed (§18 → §11):
    * ``unresolved_concern_contexts(org_state)`` — collect all open ``concern``
      requests that have aged past the current turn; the caller feeds this list
      into the overseer's next ``assemble_evidence`` call.

Interface-change gating (§18 → advisor/stages.py):
    * ``has_unacked_interface_changes(org_state, from_dept)`` — True when dept has
      sent at least one interface_change that is not yet acked.  The foreman exit-gate
      checks this; an unacked change blocks the SENDER's stage exit.

Feature-flagged seams:
    * Overseer concern feed is gated on ``CHUDWARE_OVERSEER=1`` (mirrors overseer.py).
    * council_convene delegates to councils.council_convene.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

ORG_SCHEMA = "aicad.org/1"
REQUEST_SCHEMA = "aicad.request/1"

# Valid department statuses (§18).
DEPT_STATUSES = frozenset({"forming", "active", "blocked", "delivered", "dissolved"})

# Valid channel message kinds (§18).
MSG_KINDS = frozenset({"info", "question", "decision", "warning", "interface_change"})

# Valid request kinds (§18).
REQUEST_KINDS = frozenset({"context", "resource", "information", "communication",
                            "concern", "escalation"})

# Valid resolution actions (§18).
RESOLUTION_ACTIONS = frozenset({"grant", "deny", "escalate"})

# How many node-completions trigger a department report.
_DEFAULT_REPORT_EVERY = int(os.environ.get("ORG_REPORT_EVERY", "3"))

# Overseer seam flag (mirrors overseer.py).
_OVERSEER_ENABLED = os.environ.get("CHUDWARE_OVERSEER", "0") in ("1", "true", "True")


# ---------------------------------------------------------------------------
# Clustering rule (§18): deliverable + its depends_on satellites → one dept.
# ---------------------------------------------------------------------------

def _dept_id(deliverable_id: str) -> str:
    return f"dept_{deliverable_id}"


def build_org_chart(manifest: dict[str, Any]) -> dict[str, Any]:
    """Cluster a validated deliverable manifest into an aicad.org/1 org chart.

    Clustering rule (§18): each deliverable plus its direct ``depends_on``
    satellites forms one department.  Satellites are drawn into the primary
    deliverable's department; they do not get departments of their own unless
    they also appear as top-level deliverables with their own ACs.

    Channels are auto-created for every ``depends_on`` edge that crosses department
    boundaries (i.e. where both ends are primary deliverables).

    Returns the org chart dict (not yet written to the ledger — call
    ``ledger.append(actor, "artifact", {"kind": "org_chart", ...})`` after this).
    """
    deliverables: list[dict] = manifest.get("deliverables", []) or []

    # --- build cluster map (§18): each deliverable + its depends_on satellites.
    # "Satellites" are the deliverables that list this one in their depends_on —
    # i.e., the reverse edges: if DL2.depends_on = [DL1], then DL1's dept includes
    # DL2 as a satellite (the part + its drawing + its CAM = one department).
    # The primary deliverable also records its own direct depends_on entries (since
    # a drawing that depends_on a part brings the part into the drawing's cluster).
    all_ids = {d["id"] for d in deliverables if isinstance(d, dict) and d.get("id")}
    # Forward map: primary_id → list of direct depends_on ids
    fwd: dict[str, list[str]] = {}
    for d in deliverables:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        fwd[d["id"]] = [dep for dep in (d.get("depends_on") or []) if dep in all_ids]
    # Reverse map: for each primary, which other deliverables depend on it?
    rev: dict[str, list[str]] = {did: [] for did in all_ids}
    for did, deps in fwd.items():
        for dep in deps:
            rev[dep].append(did)

    # Each primary's dept = itself + its "reverse" dependents (the satellites that
    # depend ON it) + its own forward dependencies.
    departments: list[dict] = []
    for d in deliverables:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = d["id"]
        # Satellites = things that depend on this deliverable (reverse deps)
        sat_ids = rev.get(did, [])
        # Also include direct forward deps (e.g. DL2's dept includes DL1, its part)
        forward_deps = fwd.get(did, [])
        all_dept_deliverables = [did] + [
            s for s in (sat_ids + forward_deps) if s != did
        ]
        dept: dict[str, Any] = {
            "id": _dept_id(did),
            "deliverables": all_dept_deliverables,
            "manager": {
                "role": "department_manager",
                "delegation_node": None,  # filled when delegate() is called
            },
            "members": [],
            "advisors": ["foreman", "overseer"],
            "status": "forming",
        }
        departments.append(dept)

    # --- auto-create channels from manifest depends_on edges between primary depts.
    # Each primary deliverable's canonical dept is "dept_<deliverable_id>".
    # Channels exist between those primary depts; same-dept edges are skipped.
    channels: list[dict] = []
    seen_pairs: set[frozenset] = set()
    primary_dept_ids = {_dept_id(d["id"]) for d in deliverables
                        if isinstance(d, dict) and d.get("id")}
    for d in deliverables:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = d["id"]
        a = _dept_id(did)
        for dep in (d.get("depends_on") or []):
            b = _dept_id(dep)
            if a == b or b not in primary_dept_ids:
                continue
            pair = frozenset({a, b})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            channels.append({
                "id": f"ch_{uuid.uuid4().hex[:8]}",
                "between": sorted([a, b]),
                "created_from": f"depends_on: {did} → {dep}",
                "status": "open",
            })

    return {
        "schema": ORG_SCHEMA,
        "departments": departments,
        "channels": channels,
        "chart_revision": 1,
    }


# ---------------------------------------------------------------------------
# OrgState — stateful runtime
# ---------------------------------------------------------------------------

@dataclass
class OrgState:
    """Stateful org runtime: channels, messages, requests, reports.

    All mutations emit ledger events when a Ledger is supplied.

    Args:
        org_chart:   The ``aicad.org/1`` dict produced by ``build_org_chart``.
        ledger:      Optional Ledger instance; mutations are
                     recorded as ledger events when present.
        report_every: Department-report cadence (node completions).
    """
    org_chart: dict[str, Any]
    ledger: Any = None                           # services.agent.ledger.Ledger
    report_every: int = _DEFAULT_REPORT_EVERY

    # Internal state — not exposed directly; mutated via methods.
    _messages: list[dict] = field(default_factory=list)
    _requests: dict[str, dict] = field(default_factory=dict)  # id → request
    _completion_counts: dict[str, int] = field(default_factory=dict)  # dept_id → count
    _turn: int = 0  # manager-turn counter; incremented by advance_turn()

    # ---------------------------------------------------------------------------
    # Dept / channel helpers
    # ---------------------------------------------------------------------------

    def _get_dept(self, dept_id: str) -> Optional[dict]:
        for d in self.org_chart.get("departments", []):
            if d.get("id") == dept_id:
                return d
        return None

    def _get_channel(self, channel_id: str) -> Optional[dict]:
        for ch in self.org_chart.get("channels", []):
            if ch.get("id") == channel_id:
                return ch
        return None

    def _channel_between(self, dept_a: str, dept_b: str) -> Optional[dict]:
        """Return the channel between two depts (order-independent), or None."""
        for ch in self.org_chart.get("channels", []):
            pair = set(ch.get("between", []))
            if pair == {dept_a, dept_b}:
                return ch
        return None

    def _ensure_channel(
        self,
        dept_a: str,
        dept_b: str,
        reason: str,
        *,
        actor: str = "org",
    ) -> dict:
        """Return existing channel or open a new one (ledger event on creation)."""
        ch = self._channel_between(dept_a, dept_b)
        if ch is not None:
            return ch
        ch = {
            "id": f"ch_{uuid.uuid4().hex[:8]}",
            "between": sorted([dept_a, dept_b]),
            "created_from": reason,
            "status": "open",
        }
        self.org_chart.setdefault("channels", []).append(ch)
        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "channel_opened",
                "channel": ch,
                "reason": reason,
            })
        return ch

    # ---------------------------------------------------------------------------
    # Channel messaging
    # ---------------------------------------------------------------------------

    def channel_send(
        self,
        channel_id: str,
        from_dept: str,
        kind: str,
        body: str,
        *,
        refs: Optional[dict] = None,
        needs_ack: bool = False,
        actor: str = "org",
    ) -> dict:
        """Send a message on a channel.  Returns the message event dict.

        ``interface_change`` messages set ``needs_ack=True`` automatically (§18).
        Body is capped at 120 words (§18).
        """
        if kind not in MSG_KINDS:
            raise ValueError(f"channel_send: kind {kind!r} not in {sorted(MSG_KINDS)}")

        ch = self._get_channel(channel_id)
        if ch is None:
            raise ValueError(f"channel_send: channel {channel_id!r} not found")
        if ch.get("status") != "open":
            raise ValueError(f"channel_send: channel {channel_id!r} is closed")

        # Enforce 120-word cap (§18).
        words = body.split()
        if len(words) > 120:
            body = " ".join(words[:120]) + " …"

        # interface_change always requires ack.
        if kind == "interface_change":
            needs_ack = True

        msg: dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "channel": channel_id,
            "from_dept": from_dept,
            "kind": kind,
            "body": body,
            "refs": refs or {},
            "needs_ack": needs_ack,
            "acked_by": [],          # dept ids that have acknowledged
            "ts": _now(),
        }
        self._messages.append(msg)

        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "channel_message",
                "message": msg,
            })

        return msg

    def channel_ack(
        self,
        msg_id: str,
        by_dept: str,
        *,
        actor: str = "org",
    ) -> dict:
        """Acknowledge a channel message (e.g. an interface_change).

        Returns the acknowledged message dict.  Raises ValueError if msg not found
        or dept is not a recipient (must be in channel).
        """
        for msg in self._messages:
            if msg["id"] != msg_id:
                continue
            ch = self._get_channel(msg["channel"])
            if ch is None:
                raise ValueError(f"channel_ack: channel for msg {msg_id!r} not found")
            # Any dept in the channel can ack (§18: "read your channels, acknowledge").
            if by_dept not in msg["acked_by"]:
                msg["acked_by"].append(by_dept)
            if self.ledger is not None:
                self.ledger.append(actor, "artifact", {
                    "kind": "channel_ack",
                    "msg_id": msg_id,
                    "by_dept": by_dept,
                })
            return msg
        raise ValueError(f"channel_ack: message {msg_id!r} not found")

    def has_unacked_interface_changes(self, from_dept: str) -> bool:
        """True if dept has sent any interface_change that has not been fully acked.

        The foreman exit-gate checks this: an unacked change blocks the SENDER's
        stage exit (not the send itself — §18).  A message is considered acked when
        every OTHER dept in the channel has acknowledged it.
        """
        for msg in self._messages:
            if msg.get("from_dept") != from_dept:
                continue
            if msg.get("kind") != "interface_change":
                continue
            if not msg.get("needs_ack"):
                continue
            ch = self._get_channel(msg["channel"])
            if ch is None:
                continue
            others = [d for d in ch.get("between", []) if d != from_dept]
            if not all(d in msg.get("acked_by", []) for d in others):
                return True
        return False

    # ---------------------------------------------------------------------------
    # Worker requests (§18: aicad.request/1)
    # ---------------------------------------------------------------------------

    def request_raise(
        self,
        from_dg: str,
        to: str,
        kind: str,
        body: str,
        *,
        refs: Optional[dict] = None,
        actor: str = "org",
    ) -> dict:
        """Raise a worker request.

        ``to`` is "manager", "orchestrator", or "overseer".
        ``kind`` is one of REQUEST_KINDS.
        Returns the request dict.
        """
        if kind not in REQUEST_KINDS:
            raise ValueError(f"request_raise: kind {kind!r} not in {sorted(REQUEST_KINDS)}")

        req: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "id": f"rq_{uuid.uuid4().hex[:8]}",
            "from": from_dg,
            "to": to,
            "kind": kind,
            "body": body,
            "refs": refs or {},
            "resolution": {"by": None, "action": None, "ts": None},
            "sla": "next_manager_turn",
            "raised_turn": self._turn,  # track when raised for aging
        }
        self._requests[req["id"]] = req

        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "request_raised",
                "request": req,
            })

        return req

    def request_resolve(
        self,
        request_id: str,
        by: str,
        action: str,
        *,
        note: str = "",
        actor: str = "org",
    ) -> dict:
        """Resolve a worker request explicitly.

        ``action`` must be one of "grant", "deny", "escalate".
        Silent expiry is structurally impossible: a request with an unset resolution
        can never leave ``_requests`` resolved without calling this function.
        Raises ValueError if already resolved or if action is invalid.
        """
        if action not in RESOLUTION_ACTIONS:
            raise ValueError(
                f"request_resolve: action {action!r} not in {sorted(RESOLUTION_ACTIONS)}"
            )

        req = self._requests.get(request_id)
        if req is None:
            raise ValueError(f"request_resolve: request {request_id!r} not found")

        if req["resolution"]["action"] is not None:
            raise ValueError(
                f"request_resolve: request {request_id!r} already resolved "
                f"(action={req['resolution']['action']!r})"
            )

        req["resolution"] = {
            "by": by,
            "action": action,
            "ts": _now(),
            "note": note,
        }

        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "request_resolved",
                "request_id": request_id,
                "by": by,
                "action": action,
                "note": note,
            })

        return req

    def open_requests(self) -> list[dict]:
        """All requests with no resolution."""
        return [r for r in self._requests.values() if r["resolution"]["action"] is None]

    def unresolved_concern_contexts(self) -> list[dict]:
        """Collect open ``concern`` requests that have aged past one manager turn.

        These are fed into the overseer's next audit context (§18 → §11).
        Aging does NOT remove or close the request — it surfaces it for human
        attention while leaving resolution required.

        Returns empty list when the CHUDWARE_OVERSEER env flag is off.
        """
        # Read env var dynamically (not at module import) so tests can toggle it.
        overseer_on = os.environ.get("CHUDWARE_OVERSEER", "0") in ("1", "true", "True")
        if not overseer_on:
            return []
        aged = []
        for req in self._requests.values():
            if req["resolution"]["action"] is not None:
                continue  # already resolved
            if req.get("kind") != "concern":
                continue
            raised_turn = req.get("raised_turn", self._turn)
            if self._turn > raised_turn:  # aged at least one turn
                aged.append({
                    "request_id": req["id"],
                    "from": req["from"],
                    "body": req["body"],
                    "raised_turn": raised_turn,
                    "current_turn": self._turn,
                    "kind": "unresolved_concern",
                })
        return aged

    # ---------------------------------------------------------------------------
    # Turn advancement
    # ---------------------------------------------------------------------------

    def advance_turn(self) -> None:
        """Advance the manager-turn counter.

        Call this once per manager turn.  Unresolved concerns from the previous
        turn will be flagged by ``unresolved_concern_contexts`` on the next call.
        """
        self._turn += 1

    # ---------------------------------------------------------------------------
    # Department reports
    # ---------------------------------------------------------------------------

    def record_node_completion(
        self,
        dept_id: str,
        *,
        actor: str = "org",
    ) -> Optional[dict]:
        """Record one build-node completion for a department.

        When the completion count reaches a multiple of ``report_every``, a
        structured department report is emitted as a ledger event (§18).

        Returns the report dict when emitted, None otherwise.
        """
        self._completion_counts[dept_id] = self._completion_counts.get(dept_id, 0) + 1
        count = self._completion_counts[dept_id]
        if count % self.report_every == 0:
            return self._emit_dept_report(dept_id, actor=actor)
        return None

    def _emit_dept_report(self, dept_id: str, *, actor: str = "org") -> dict:
        """Build and ledger-emit a structured department report (§18)."""
        dept = self._get_dept(dept_id)
        open_reqs = [
            {"id": r["id"], "kind": r["kind"], "body": r["body"][:80]}
            for r in self._requests.values()
            if r["resolution"]["action"] is None
            and r.get("from", "").startswith(dept_id + "_")
        ]

        # Unacked interface changes this dept sent.
        interfaces_touched = [
            msg["id"]
            for msg in self._messages
            if msg.get("from_dept") == dept_id
            and msg.get("kind") == "interface_change"
        ]

        report: dict[str, Any] = {
            "schema": "aicad.dept_report/1",
            "dept_id": dept_id,
            "deliverables": dept.get("deliverables", []) if dept else [],
            "completions": self._completion_counts.get(dept_id, 0),
            "status": dept.get("status", "unknown") if dept else "unknown",
            "done": [],       # caller fills from buildgraph
            "next": [],       # caller fills from buildgraph
            "blocked": [],    # caller fills from buildgraph
            "requests_open": open_reqs,
            "interfaces_touched": interfaces_touched,
            "ts": _now(),
        }

        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "dept_report",
                "report": report,
            })

        return report

    # ---------------------------------------------------------------------------
    # Department manager spawning (§18 + §12 delegation seam)
    # ---------------------------------------------------------------------------

    def spawn_manager(
        self,
        dept_id: str,
        *,
        delegation_tree: Any = None,   # DelegationTree from delegation.py
        brief: str = "",
        budget_tokens: int = 8000,
        actor: str = "org",
    ) -> Optional[str]:
        """Spawn a department_manager worker via the delegation tree (§18 + §12).

        Kind = "worker" (not ponder — managers run multi-turn, stateful).
        Returns the delegation node id, or None if no tree is available.
        """
        dept = self._get_dept(dept_id)
        if dept is None:
            raise ValueError(f"spawn_manager: dept {dept_id!r} not found in org chart")

        if delegation_tree is None:
            return None

        from services.agent.delegation import delegate as _delegate_fn
        node = _delegate_fn(
            "department_manager",
            brief or f"Manage department {dept_id}",
            budget_tokens,
            tree=delegation_tree,
            kind="worker",
        )
        node_id = node.get("id") if isinstance(node, dict) else None

        # Record the delegation node id on the department record.
        if node_id:
            dept["manager"]["delegation_node"] = node_id
            if self.ledger is not None:
                self.ledger.append(actor, "artifact", {
                    "kind": "manager_spawned",
                    "dept_id": dept_id,
                    "delegation_node": node_id,
                })

        return node_id

    # ---------------------------------------------------------------------------
    # Re-org (replan scope=org)
    # ---------------------------------------------------------------------------

    def reorg(
        self,
        new_manifest: dict[str, Any],
        *,
        actor: str = "org",
    ) -> dict[str, Any]:
        """Re-cluster the org from a new manifest (replan scope=org, §17).

        Bumps ``chart_revision``.  Returns the new org chart.
        The caller must write the new chart to the ledger as an artifact event.
        """
        new_chart = build_org_chart(new_manifest)
        new_chart["chart_revision"] = self.org_chart.get("chart_revision", 1) + 1
        self.org_chart = new_chart

        if self.ledger is not None:
            self.ledger.append(actor, "artifact", {
                "kind": "org_reorg",
                "chart_revision": new_chart["chart_revision"],
                "org_chart": new_chart,
            })

        return new_chart


# ---------------------------------------------------------------------------
# Overseer concern-feed (public helper for pipeline/phases + tests)
# ---------------------------------------------------------------------------

def unresolved_concern_contexts(org_state: OrgState) -> list[dict]:
    """Public helper: collect aged unresolved concerns from org_state.

    Feature-flagged on CHUDWARE_OVERSEER (mirrors overseer.py).  Returns [] when
    flag is off, so callers are safe to call unconditionally.
    """
    return org_state.unresolved_concern_contexts()


# ---------------------------------------------------------------------------
# Interface-change gating helper (public, used in pipeline exit-gate checks)
# ---------------------------------------------------------------------------

def has_unacked_interface_changes(org_state: OrgState, from_dept: str) -> bool:
    """True when dept has sent an unacked interface_change message.

    The foreman exit-gate calls this; an unacked change blocks the SENDER's
    stage exit (§18).
    """
    return org_state.has_unacked_interface_changes(from_dept)



# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
