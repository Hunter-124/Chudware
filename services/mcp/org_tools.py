"""org_tools.py — MCP tool registrations for the org-layer tools.

Family: ``org``.  Risk class: ``write`` (mutates org state / ledger).

These registrations make channel_send / request_resolve / council_convene
resolve in the tool catalog so validate_role sees them as real tools (no longer
feature-flagged in roles.py FEATURE_FLAGGED_TOOLS).

Per the in-app pivot directive: registration is thin (one decorator each); real
logic lives in services/agent/org.py and services/agent/councils.py.  No MCP
freshness checks; no complex dispatch — the catalog resolution is the goal here.

Tools registered:
  * ``channel_send``     — send a message on a channel (org.OrgState.channel_send)
  * ``request_resolve``  — resolve a worker request explicitly (no silent expiry)
  * ``request_raise``    — raise a worker request (workers use this)
  * ``council_convene``  — convene a build-strategy or other council (councils.py, MERGED)
"""
from __future__ import annotations

from typing import Any, Optional

# Bound by the owner of a live mission for the process-global MCP fallback.
# MissionDispatch serves its own run-scoped tools directly; this registration is
# the same explicit fallback seam used by delegation_tools.DELEGATION_TREE.
MISSION_CONTEXT: Any = None


def bind_mission_context(context: Any) -> None:
    """Bind, or clear with ``None``, the live context for org fallback tools."""
    global MISSION_CONTEXT
    MISSION_CONTEXT = context




def register_org_tools(mcp) -> None:
    """Register the four org-layer tools under the ``org`` family."""

    @mcp.tool(name="channel_send")
    def channel_send(
        channel_id: str,
        from_dept: str,
        kind: str,
        body: str,
        refs: Optional[dict] = None,
        needs_ack: bool = False,
    ) -> dict:
        """Send a message on a channel between departments.

        ``kind`` is one of: info | question | decision | warning | interface_change.
        ``interface_change`` messages automatically set needs_ack=True (§18).
        Body is capped at 120 words (§18).

        An unacked ``interface_change`` blocks the SENDER's stage exit — not the
        send itself.  Use channel_send freely; the exit gate enforces discipline.

        Returns the message event dict.
        """
        ctx = MISSION_CONTEXT
        org = getattr(ctx, "org_state", None) if ctx is not None else None

        if org is None:
            return {
                "error": (
                    "channel_send: no org_state in mission context. "
                    "Org layer must be active."
                ),
                "status": "failed",
            }

        return org.channel_send(
            channel_id, from_dept, kind, body,
            refs=refs, needs_ack=needs_ack,
        )

    @mcp.tool(name="request_resolve")
    def request_resolve(
        request_id: str,
        by: str,
        action: str,
        note: str = "",
    ) -> dict:
        """Resolve a worker request explicitly.

        ``action`` must be one of: grant | deny | escalate.
        Every request MUST be explicitly resolved — silent expiry is impossible.
        An unresolved ``concern`` that ages past one manager turn auto-surfaces
        in the overseer's next audit context (concern aging, §18).

        Returns the resolved request dict.
        """
        ctx = MISSION_CONTEXT
        org = getattr(ctx, "org_state", None) if ctx is not None else None

        if org is None:
            return {
                "error": (
                    "request_resolve: no org_state in mission context. "
                    "Org layer must be active."
                ),
                "status": "failed",
            }

        try:
            return org.request_resolve(request_id, by, action, note=note)
        except ValueError as exc:
            return {"error": str(exc), "status": "failed"}

    @mcp.tool(name="request_raise")
    def request_raise(
        from_dg: str,
        to: str,
        kind: str,
        body: str,
        refs: Optional[dict] = None,
    ) -> dict:
        """Raise a worker request to the manager, orchestrator, or overseer.

        ``kind`` is one of: context | resource | information | communication |
                             concern | escalation.
        ``to`` is "manager", "orchestrator", or "overseer".

        Returns the request dict with id and sla fields.
        """
        ctx = MISSION_CONTEXT
        org = getattr(ctx, "org_state", None) if ctx is not None else None

        if org is None:
            return {
                "error": (
                    "request_raise: no org_state in mission context. "
                    "Org layer must be active."
                ),
                "status": "failed",
            }

        return org.request_raise(from_dg, to, kind, body, refs=refs)

    @mcp.tool(name="council_convene")
    def council_convene(
        kind: str,
        topic: str,
        inputs: Optional[dict] = None,
        stage: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> dict:
        """Convene a council (§19: build_strategy | spec_review | failure_review | ...).

        Runs a full 2-round deliberation (manufacturing / tolerancing /
        fitment / simplicity) and returns the aicad.council/1 record.  Seats run
        as ponder delegations (kind='ponder', auto-approved, §12).

        The council record is written as a ledger artifact; the synthesis is also
        written as a decision event.  Dissents are registered in the mission's
        DissentsRegistry so the foreman can watch trigger_conditions.

        ``stage`` and ``tier`` are optional — when omitted they are read
        from the mission context automatically.  They drive the council policy
        engine (breadth / diverse-model selection).  Local tiers run wide (all 4
        seats); frontier runs narrow (1 seat, cost-minimising).

        Returns the completed council record dict.
        """
        ctx = MISSION_CONTEXT
        provider = getattr(ctx, "provider", None) if ctx is not None else None
        ledger = getattr(ctx, "ledger", None) if ctx is not None else None
        registry = (
            getattr(ctx, "dissents_registry", None) if ctx is not None else None
        )
        # Read stage/tier from mission context when not explicitly given.
        ctx_stage: Optional[str] = (
            getattr(ctx, "stage", None) if ctx is not None else None
        )
        ctx_tier: Optional[str] = (
            getattr(ctx, "tier", None) if ctx is not None else None
        )
        # Multiple providers for diverse-model selection.
        ctx_providers: Optional[list] = (
            getattr(ctx, "providers", None) if ctx is not None else None
        )

        if ctx is None or provider is None:
            return {
                "ok": False,
                "policy_refusal": True,
                "executed": False,
                "status": "no_mission_context",
                "error": (
                    "council_convene is only available inside a mission turn with "
                    "a bound provider."
                ),
            }

        resolved_stage = stage or ctx_stage
        resolved_tier = tier or ctx_tier

        from services.agent.councils import council_convene as _convene
        return _convene(
            kind=kind,
            topic=topic,
            inputs=inputs or {},
            provider=provider,
            ledger=ledger,
            registry=registry,
            providers=ctx_providers if ctx_providers else None,
            stage=resolved_stage,
            tier=resolved_tier,
        )
