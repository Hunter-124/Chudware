"""Delegation MCP tools — ``delegate`` (aicad.delegation/1).

Family: ``delegation``.  Risk class: ``write`` (spawns a sub-agent with a budget).

Contract: contracts/advisor.md §12 (FROZEN).
Design decision (2026-07-04): the ``delegate`` tool is the ONLY mechanism by which
role cards spawn sub-agents.  It is listed in role card tool_allowlists for
orchestrator and department_manager.  Registering it here makes it a real resolved
tool (no longer feature-flagged in roles.py FEATURE_FLAGGED_TOOLS).

WHICH ``delegate`` RUNS:
    Inside a mission turn, ``agent_turn.run_turn`` resolves ``delegate`` through
    ``MissionDispatch.resolve`` — that one owns the live tree (and binds one when
    the mission has a provider but no tree).  THIS registration is the fallback
    reached only when no mission dispatch is bound at all: an MCP client, a bare
    tool listing, or a turn constructed without a dispatcher.

    In that fallback there is no run to spawn into and no provider to execute a
    child, so this tool refuses instead of recording a node nobody will ever run.
    The refusal names the alternative rather than repeating the missing field —
    "no delegation tree available" told 15 recorded calls nothing they could act
    on.  (The old lookup here imported ``services.agent.interaction.get_mission_context``,
    which has never existed in this repo: the import raised, the tree was always
    None, and the tool failed 100% of the time by construction.)
"""
from __future__ import annotations

from typing import Any, Optional

# Set by whoever owns a live mission when this fallback registration must work
# (nothing does today — the mission path resolves ``delegate`` through
# MissionDispatch). A module attribute, not a fabricated import: the previous
# code imported a symbol that has never existed, so the tree was unconditionally
# None and the tool failed by construction.
DELEGATION_TREE: Any = None


def bind_delegation_tree(tree: Any) -> None:
    """Bind (or clear, with None) the tree this fallback ``delegate`` spawns into."""
    global DELEGATION_TREE
    DELEGATION_TREE = tree


def register_delegation_tools(mcp) -> None:
    """Register the ``delegate`` tool under the ``delegation`` family."""

    @mcp.tool(name="delegate")
    def delegate(
        role: str,
        brief: str,
        budget_tokens: int,
        kind: str = "worker",
        parent_id: Optional[str] = None,
        docs: Optional[list] = None,
    ) -> dict:
        """Spawn a sub-agent role with an isolated brief and token budget.

        depth-1 sub-agents (direct children of the orchestrator) are auto-approved.
        Deeper worker sub-agents require the mission overseer's approval; approval is
        asynchronous — the returned node will have status 'pending' until granted.
        'ponder' kind (read-only, deep-dive) is auto-approved at any depth.

        Only available INSIDE a mission turn: a sub-agent needs a run to spawn into
        and a model provider to execute.  Called outside one it returns
        ``{"ok": false, "status": "unavailable"}`` with the concrete alternative —
        it never records a node that nothing will run.

        Args:
            role: Role id to spawn (must match a role card in services/agent/roles/).
            brief: Self-contained task brief for the sub-agent (≤500 chars).
            budget_tokens: Token budget for the sub-agent (inherited by ancestors).
            kind: 'worker' (default) or 'ponder' (read-only, auto-approved any depth).
            parent_id: Parent delegation node id (defaults to orchestrator root).
            docs: Doc lease requests [{doc_id, lease}] for the multi-doc workspace (request shape only).

        Returns:
            Delegation node dict with id, status, model, budget.
        """
        tree = DELEGATION_TREE

        if tree is None:
            return {
                "ok": False,
                "status": "unavailable",
                "error": (
                    "delegate is not available here: this call is outside a mission "
                    "turn, so there is no run to spawn into and no provider to run a "
                    "sub-agent. Do NOT retry delegate — do the "
                    f"{role or 'requested'} work yourself with the concrete tools in "
                    "your allowlist, and record anything you cannot finish with "
                    "ledger_note(kind='handoff', ...)."
                ),
                "role_requested": role,
                "do_instead": [
                    "do the work directly with your own tools",
                    "ledger_note(kind='handoff', payload={...}) for the remainder",
                ],
            }

        from services.agent.delegation import delegate as _delegate
        return _delegate(
            role, brief, budget_tokens,
            tree=tree,
            parent_id=parent_id,
            kind=kind,
            docs=docs,
        )
