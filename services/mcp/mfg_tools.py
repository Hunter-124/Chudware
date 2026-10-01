"""Manufacturing MCP surface over the shared chudware-hostd daemon lane.

This module never opens or addresses a device transport. Every operation is one
request through :mod:`services.mcp.daemon_bridge`; hostd's single-owner
manufacturing lane owns device state and the arbiter remains the only approval
validator. Daemon success and refusal dictionaries are returned unchanged.
"""
from __future__ import annotations

from . import daemon_bridge
from .tool_registry import model_selectors


def _call(method: str, **params) -> dict:
    """Make exactly one daemon call, omitting only unspecified optional fields."""
    return daemon_bridge.client().call(
        method, {name: value for name, value in params.items() if value is not None}
    )


def _agent_initiator(identity: str, seat: str | None,
                     agent_run_id: str | None) -> dict:
    initiator = {"kind": "agent", "identity": identity}
    if seat:
        initiator["seat"] = seat
    if agent_run_id:
        initiator["agent_run_id"] = agent_run_id
    return initiator


def register_mfg_tools(mcp) -> None:
    """Register the complete agent-reachable manufacturing gateway surface."""

    @mcp.tool()
    def mfg_device_list() -> dict:
        """List manufacturing devices and their truthful current lane state.

        This is observation only. Physical ``execute`` and
        ``safety_integration`` are never inferred by this tool.
        """
        return _call("mfg_device_list")

    @mcp.tool()
    def mfg_device_describe(device_id: str) -> dict:
        """Describe one device identity, transport summary, and policy."""
        return _call("mfg_device_describe", device_id=device_id)

    @mcp.tool()
    def mfg_device_capability(device_id: str) -> dict:
        """Read all nine explicit capability truth levels for one device."""
        return _call("mfg_device_capability", device_id=device_id)

    @mcp.tool()
    def mfg_telemetry_snapshot(device_id: str) -> dict:
        """Read one FreshValue-wrapped telemetry snapshot; gaps remain unknown."""
        return _call("mfg_telemetry_snapshot", device_id=device_id)

    @mcp.tool()
    def mfg_gate_report(gate_report_ref: str) -> dict:
        """Read a seq-stamped Tier-0 and two-stage gate report by reference."""
        return _call("mfg_gate_report", gate_report_ref=gate_report_ref)

    @mcp.tool()
    def mfg_run_list(device_id: str | None = None, state: str | None = None,
                     limit: int | None = None) -> dict:
        """List durable run history, optionally filtered by device and state."""
        return _call("mfg_run_list", device_id=device_id, state=state, limit=limit)

    @mcp.tool()
    @model_selectors("run_id")
    def mfg_run_describe(run_id: str) -> dict:
        """Read one run's state, artifact, gate, approval, and initiator history."""
        return _call("mfg_run_describe", run_id=run_id)

    @mcp.tool()
    def mfg_journal_query(from_seq: int | None = None,
                          kinds: list[str] | None = None,
                          subject: str | None = None,
                          limit: int | None = None) -> dict:
        """Query ordered manufacturing journal records and waiver rates."""
        return _call("mfg_journal_query", from_seq=from_seq, kinds=kinds,
                     subject=subject, limit=limit)

    @mcp.tool()
    def mfg_approval_inspect(approval_id: str | None = None,
                             proposal_id: str | None = None) -> dict:
        """Inspect a frozen human approval or an unapproved proposal.

        This tool cannot create, amend, revoke, or approve a binding.
        """
        return _call("mfg_approval_inspect", approval_id=approval_id,
                     proposal_id=proposal_id)

    @mcp.tool()
    def mfg_artifact_register(lease_id: str, device_id: str,
                              artifact: dict) -> dict:
        """Register and hash an immutable NC/G-code/print artifact.

        Registration performs no device I/O and does not authorize a run.
        ``artifact`` carries ``kind`` plus exactly one daemon-supported content
        source (``content``, ``lines``, or ``path``) and its post/slicer profile
        binding when applicable.
        """
        return _call("mfg_artifact_register", lease_id=lease_id,
                     device_id=device_id, artifact=artifact)

    @mcp.tool()
    @model_selectors("run_id")
    def mfg_preflight_run(lease_id: str, artifact_id: str,
                          run_id: str | None = None,
                          job: dict | None = None,
                          snapshot: dict | None = None) -> dict:
        """Build a draft run and its seq-stamped Tier-0/two-stage preflight.

        Preflight evidence remains advisory until the arbiter revalidates an
        actual human approval at start.
        """
        return _call("mfg_preflight_run", lease_id=lease_id,
                     artifact_id=artifact_id, run_id=run_id, job=job,
                     snapshot=snapshot)

    @mcp.tool()
    @model_selectors("run_id")
    def mfg_approval_propose(lease_id: str, run_id: str,
                             exercisable_by: str | None = None,
                             expiry_ttl_ms: int | None = None,
                             attendance: dict | None = None,
                             spindle_authority: str | None = None) -> dict:
        """Propose an UNAPPROVED binding for human review.

        This never calls ``mfg_approval_create`` and cannot approve anything.
        The returned proposal has no authority until a human seat creates the
        binding and the arbiter later validates it.
        """
        return _call("mfg_approval_propose", lease_id=lease_id, run_id=run_id,
                     exercisable_by=exercisable_by,
                     expiry_ttl_ms=expiry_ttl_ms, attendance=attendance,
                     spindle_authority=spindle_authority)

    @mcp.tool()
    @model_selectors("run_id")
    def request_mfg_run_start(lease_id: str, run_id: str, approval_id: str,
                              agent_identity: str = "mcp-agent",
                              seat: str | None = None,
                              agent_run_id: str | None = None) -> dict:
        """Request start; the arbiter alone revalidates and may refuse it.

        The initiator is always recorded as ``agent``. Only an active human
        approval explicitly marked ``human_or_agent`` can permit this request.
        """
        return _call(
            "mfg_run_start", lease_id=lease_id, run_id=run_id,
            approval_id=approval_id,
            initiator=_agent_initiator(agent_identity, seat, agent_run_id),
        )

    @mcp.tool()
    @model_selectors("run_id")
    def request_mfg_hold(lease_id: str, run_id: str,
                         kind: str = "feed_hold",
                         agent_identity: str = "mcp-agent",
                         seat: str | None = None,
                         agent_run_id: str | None = None) -> dict:
        """Request a software hold; it is not an E-stop or safety function."""
        return _call(
            "mfg_run_hold", lease_id=lease_id, run_id=run_id, kind=kind,
            initiator=_agent_initiator(agent_identity, seat, agent_run_id),
        )

    @mcp.tool()
    @model_selectors("run_id")
    def request_mfg_abort(lease_id: str, run_id: str,
                          agent_identity: str = "mcp-agent",
                          seat: str | None = None,
                          agent_run_id: str | None = None) -> dict:
        """Request controlled run abort; it is not a safety-rated stop."""
        return _call(
            "mfg_run_abort", lease_id=lease_id, run_id=run_id,
            initiator=_agent_initiator(agent_identity, seat, agent_run_id),
        )
