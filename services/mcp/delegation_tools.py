"""Delegation MCP tools — ``delegate`` (aicad.delegation/1, T-W14).

Family: ``delegation``.  Risk class: ``write`` (spawns a sub-agent with a budget).

Contract: contracts/advisor.md §12 (FROZEN).
Hunter directive 2026-07-04: the ``delegate`` tool is the ONLY mechanism by which
role cards spawn sub-agents.  It is listed in role card tool_allowlists for
orchestrator and department_manager.  Registering it here makes it a real resolved
tool (no longer feature-flagged in roles.py FEATURE_FLAGGED_TOOLS).

The live delegation tree (DelegationTree instance) is injected through the mission
context — this module registers the tool signature so validate_role sees it in the
catalog.  The actual tree binding happens at mission launch time.
"""
from __future__ import annotations

from typing import Any, Optional


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

        Args:
            role: Role id to spawn (must match a role card in services/agent/roles/).
            brief: Self-contained task brief for the sub-agent (≤500 chars).
            budget_tokens: Token budget for the sub-agent (inherited by ancestors).
            kind: 'worker' (default) or 'ponder' (read-only, auto-approved any depth).
            parent_id: Parent delegation node id (defaults to orchestrator root).
            docs: Doc lease requests [{doc_id, lease}] for T-W16 (request shape only).

        Returns:
            Delegation node dict with id, status, model, budget.
        """
        # The live tree is bound at mission launch.  If this tool is called without
        # a tree context (e.g. from an MCP client directly), return a clear error.
        try:
            from services.agent.interaction import get_mission_context
            ctx = get_mission_context()
            tree = getattr(ctx, "delegation_tree", None) if ctx else None
        except Exception:  # noqa: BLE001
            tree = None

        if tree is None:
            return {
                "error": (
                    "delegate: no delegation tree available. "
                    "This tool must be called within an active mission context."
                ),
                "status": "failed",
            }

        from services.agent.delegation import delegate as _delegate
        return _delegate(
            role, brief, budget_tokens,
            tree=tree,
            parent_id=parent_id,
            kind=kind,
            docs=docs,
        )
