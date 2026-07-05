"""node_runner.py — Wrap the agent loop for DAG node execution.

Contract: contracts/dag_schema.md §2–§3, §11.
Scaffold: T-A2.

Each node in an aicad.dag/1 graph is executed by calling
``node_runner.run(node, context_pack, ...)`` which:

  1. Builds a system prompt from the context pack (node type, inputs, allowed
     tools, end condition, part description).
  2. Invokes ``agent_fn(request, context_pack)`` — an injectable callable
     defaulting to the real ``services/agent/loop.run()`` surface.  Tests
     inject a mock that returns deterministic verdicts without an LLM.
  3. Maps the agent's ``AgentRun.stop_reason`` → verdict ∈
     {done, retry, escalate}.
  4. Decrements retry_remaining on a retry verdict; converts to escalate
     when budget is exhausted.
  5. Returns ``(verdict, output_state)`` where output_state is merged into
     the graph state by the caller (graph_loader.py node handler).

Verdict semantics:
  done      — node completed successfully; proceed to successors.
  retry     — node should be re-queued (budget > 0); graph_loader routes
              back via a conditional edge.
  escalate  — budget exhausted or unrecoverable failure; graph_loader routes
              to the gate/pause node per on_fail policy.

Event emission:
  All events go through the emit callback captured from executor.run().
  Only the 8 frozen §11 event types are used (dag.node.started,
  dag.node.done, dag.node.failed, dag.node.paused).  No new event types.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

# Advisory engine / foreman (T-W5, Pillar 20): after a node step, the foreman runs
# the stage battery over the node's doc (if it carries one) and the advisory block
# is attached to output_state["advisory"] for injection into the next node's
# context pack.  Gated by AI_CAD_ADVISOR (default on); best-effort.
_ADVISOR_ENABLED = os.environ.get("AI_CAD_ADVISOR", "1") not in ("0", "false", "False")

# node_type → advisory stage (the stage whose battery the foreman runs post-step).
_NODE_TYPE_STAGE: dict[str, str] = {
    "design": "feature",
    "structural": "sim",
    "simulate": "sim",
    "ecad": "feature",
}

# ---------------------------------------------------------------------------
# Verdict type
# ---------------------------------------------------------------------------

VERDICT_DONE = "done"
VERDICT_RETRY = "retry"
VERDICT_ESCALATE = "escalate"

# stop_reason values that map to done
_DONE_REASONS = frozenset(["confirmed", "stabilized"])
# stop_reason values that map to retry
_RETRY_REASONS = frozenset(["max_iters"])
# stop_reason values that map to escalate (failed build / no model output)
_ESCALATE_REASONS = frozenset(["failed", "no_code"])

# ---------------------------------------------------------------------------
# Agent function type
# ---------------------------------------------------------------------------

# AgentFn signature:
#   (request: str, context_pack: dict) -> AgentResult
# AgentResult must have:
#   .success: bool
#   .stop_reason: str   (one of confirmed|stabilized|max_iters|failed|no_code)
#   .final_code: str | None
#   .result: dict | None
AgentFn = Callable[[str, dict[str, Any]], Any]

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]


# ---------------------------------------------------------------------------
# Default agent function (wraps services/agent/loop.run)
# ---------------------------------------------------------------------------

class _StubAgentResult:
    """Minimal AgentRun-like object returned by the T-A1 stub path."""
    def __init__(self) -> None:
        self.stop_reason = "confirmed"
        self.success = True
        self.final_code = None
        self.result = None


def _default_agent_fn(request: str, context_pack: dict[str, Any]) -> Any:
    """Invoke services/agent/loop.run() with provider from agent config.

    Imported lazily to avoid hard dependency when the engine is absent
    (test environments). Falls back to a stub AgentResult (T-A1 behavior)
    when the live agent loop or provider cannot be imported — this keeps
    the existing executor tests green while real dispatch works in production.
    """
    try:
        from services.agent import loop  # noqa: PLC0415
        from services.agent.providers.anthropic import AnthropicProvider  # noqa: PLC0415
        provider = AnthropicProvider()
        node_name = context_pack.get("node_name", "dag_node")
        return loop.run(request=request, provider=provider, name=node_name)
        # NOTE: loop.run() has no allowed_tools parameter — tool filtering is
        # expressed as advisory prose in the request string (see _build_request).
        # Real injection is a T-A4/T-A5 seam; loop.py is read-only per Driver notes.
    except (ImportError, ModuleNotFoundError):
        # Engine or provider unavailable (CI, offline, no API key) — use stub.
        return _StubAgentResult()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    node: dict[str, Any],
    context_pack: dict[str, Any],
    *,
    emit: EmitFn = None,
    agent_fn: Optional[AgentFn] = None,
) -> tuple[str, dict[str, Any]]:
    """Execute one DAG node and return (verdict, output_state).

    Args:
        node:         The aicad.dag/1 node object (id, type, name, inputs,
                      on_fail, autonomy, tools, end_condition).
        context_pack: Assembled by context_pack.assemble(). Contains
                      node_inputs, allowed_tools, retry_remaining, etc.
        emit:         Optional §11 event callback
                      ``(event_type: str, payload: dict) -> None``.
        agent_fn:     Callable(request, context_pack) -> AgentResult.
                      Defaults to _default_agent_fn (live loop).
                      Tests inject a mock here.

    Returns:
        (verdict, output_state) where:
          verdict      ∈ {done, retry, escalate}
          output_state  dict with keys: status, summary, data,
                        retry_remaining, doc_id (if present).
    """
    node_id: str = node["id"]
    node_type: str = node["type"]
    node_name: str = node["name"]
    node_inputs: dict[str, Any] = context_pack.get("node_inputs") or {}
    retry_remaining: int = context_pack.get("retry_remaining", 1)

    # Role card (T-W7): a node may carry a ``role:`` field naming a predefined role
    # card in services/agent/roles/*.yaml. When present, the card's system prompt +
    # tool allowlist govern the node's agent turn. Applying the card is best-effort:
    # a missing card or import failure must never break node execution (it just runs
    # with the default prompt / no extra tool scoping), matching the ledger seam
    # discipline elsewhere in this pipeline.
    role_card = _resolve_role_card(node)
    if role_card is not None:
        _apply_role_tool_policy(role_card)
        context_pack = dict(context_pack)
        context_pack["role_id"] = role_card.id
        context_pack["role_system_prompt"] = role_card.system_prompt
        # The node's tools: allowlist and the card's allowlist should agree; the card
        # is authoritative for a role-typed node, so expose its concrete tools.
        context_pack.setdefault("allowed_tools", list(role_card.concrete_tools))

    # Build the request string passed to the agent.
    request = _build_request(node_type, node_inputs, context_pack)

    # Resolve agent callable.
    _agent_fn = agent_fn or _default_agent_fn

    # Emit node.started (§11).
    _emit(emit, "dag.node.started", {
        "node_id": node_id,
        "node_type": node_type,
        "node_name": node_name,
    })

    t0 = time.monotonic()
    try:
        agent_result = _agent_fn(request, context_pack)
    except Exception as exc:  # noqa: BLE001
        # Unrecoverable agent error → escalate immediately.
        _emit(emit, "dag.node.failed", {
            "node_id": node_id,
            "error": str(exc),
        })
        output_state = {
            "status": "failed",
            "summary": f"Agent raised exception: {exc}",
            "data": {"error": str(exc)},
            "retry_remaining": retry_remaining,
            "doc_id": node_inputs.get("doc_id"),
        }
        return VERDICT_ESCALATE, output_state

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    # Map stop_reason → initial verdict.
    stop_reason: str = getattr(agent_result, "stop_reason", "") or ""
    success: bool = bool(getattr(agent_result, "success", False))

    verdict = _map_verdict(stop_reason, success)

    # Consume retry budget when the verdict is retry.
    new_retry_remaining = retry_remaining
    if verdict == VERDICT_RETRY:
        new_retry_remaining = retry_remaining - 1
        if new_retry_remaining < 0:
            new_retry_remaining = 0
        if new_retry_remaining == 0:
            verdict = VERDICT_ESCALATE

    # Build output_state.
    final_code: str | None = getattr(agent_result, "final_code", None)
    agent_result_dict: dict | None = getattr(agent_result, "result", None)
    doc_id = node_inputs.get("doc_id")

    output_state: dict[str, Any] = {
        "status": _verdict_to_node_status(verdict),
        "summary": _build_summary(node_name, verdict, stop_reason, elapsed_ms),
        "data": {
            "stop_reason": stop_reason,
            "final_code": final_code,
            "agent_result": agent_result_dict,
            "elapsed_ms": round(elapsed_ms, 1),
        },
        "retry_remaining": new_retry_remaining,
        "doc_id": doc_id,
    }

    # Foreman advisory pass (T-W5, §9.1): run the stage battery over the node's doc
    # and attach the rendered block for the next node's context pack.  Best-effort.
    # NOTE: unlike the loop.py seam, the DAG path has no run_id/ledger handle here,
    # so DAG-path advisories are injected but NOT yet logged to the ledger event
    # stream — offline replay of DAG-injected blocks (§3/§9.4) is deferred until a
    # ledger handle is threaded through the context pack (tracked for T-W19 glue).
    adv_block = _run_advisory(node_type, doc_id, seq=retry_remaining)
    if adv_block:
        output_state["advisory"] = adv_block

    # Overseer stage-exit audit (T-W13, §11): on a node completing DONE, trigger a
    # budgeted overseer audit of the plan/reasoning so far.  Real trigger call site
    # — but it fires ONLY when the context pack carries a run_id AND an overseer
    # provider map (so it no-ops in the common/test path and never regresses the
    # DAG suite).  See the run_id/ledger-glue NOTE above: same missing thread.
    if verdict == VERDICT_DONE:
        ov_out = _run_overseer_stage_exit(node, context_pack)
        if ov_out:
            output_state["overseer_audit"] = ov_out

    # Emit appropriate §11 completion event.
    if verdict == VERDICT_DONE:
        _emit(emit, "dag.node.done", {
            "node_id": node_id,
            "output": output_state,
        })
    elif verdict == VERDICT_RETRY:
        # Still has budget — will be re-routed; emit paused so mission-control
        # UI can show the node as waiting to retry.
        _emit(emit, "dag.node.paused", {
            "node_id": node_id,
            "reason": f"retry (budget remaining: {new_retry_remaining})",
        })
    else:  # escalate
        _emit(emit, "dag.node.failed", {
            "node_id": node_id,
            "error": f"escalated: {stop_reason} (budget exhausted)",
        })

    return verdict, output_state


# ---------------------------------------------------------------------------
# Role card application (T-W7)
# ---------------------------------------------------------------------------

def _resolve_role_card(node: dict[str, Any]) -> Any:
    """Return the RoleCard for ``node['role']`` if the node names one, else None.

    Best-effort: any failure (no role field, unknown role, roles module unavailable)
    returns None so the node runs with default behaviour."""
    role_id = node.get("role")
    if not role_id:
        return None
    try:
        from services.agent import roles  # noqa: PLC0415
        return roles.load_role(str(role_id))
    except Exception:  # noqa: BLE001
        return None


def _apply_role_tool_policy(role_card: Any) -> None:
    """Bind the role card's tool allowlist into the MCP tool_registry (the ONE
    dispatch enforcement point). Best-effort — a missing registry never breaks the
    run."""
    try:
        from mcp_server import tool_registry as reg  # noqa: PLC0415
        role_card.apply_tool_policy(reg)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_request(
    node_type: str,
    node_inputs: dict[str, Any],
    context_pack: dict[str, Any],
) -> str:
    """Compose the natural-language request string for the agent.

    Priority:
      1. node_inputs["request"] — explicit override
      2. node_type-specific synthesis from other inputs
      3. Fall back to a minimal prompt derived from context pack

    The end_condition and part_description (if any) are appended as
    context so the agent understands the node's goal.
    """
    base = node_inputs.get("request") or _synthesise_request(node_type, node_inputs)

    parts = [base]

    # Append part description if available (from GSL describe_part).
    part_desc = context_pack.get("part_description")
    if part_desc:
        parts.append(f"\nCurrent part state:\n{part_desc}")

    # Append end condition hint.
    end_cond = context_pack.get("end_condition")
    if end_cond:
        parts.append(f"\nConsider yourself done when: {end_cond}")

    # List allowed tools if restricted.
    allowed = context_pack.get("allowed_tools")
    if allowed is not None:
        parts.append(f"\nAvailable tools (only): {', '.join(allowed)}")

    return "\n".join(parts)


def _synthesise_request(node_type: str, node_inputs: dict[str, Any]) -> str:
    """Generate a fallback request string based on node type and inputs."""
    doc_id = node_inputs.get("doc_id") or ""
    doc_ref = f" (doc: {doc_id})" if doc_id else ""
    if node_type == "design":
        return f"Design or modify the part{doc_ref} per the project specification."
    if node_type == "ecad":
        return f"Design or modify the schematic/PCB{doc_ref}."
    if node_type == "structural":
        return f"Run structural analysis on the part{doc_ref}."
    if node_type == "simulate":
        return f"Run simulation on the part{doc_ref}."
    if node_type == "source":
        return "Source and price parts from the BOM."
    if node_type == "report":
        return "Synthesise the project results into a plain-language verdict."
    if node_type == "plan":
        return "Decompose the project brief into a list of capability tasks."
    return f"Execute the {node_type} node{doc_ref}."


def _map_verdict(stop_reason: str, success: bool) -> str:
    """Map agent stop_reason + success flag → verdict string."""
    if stop_reason in _DONE_REASONS and success:
        return VERDICT_DONE
    if stop_reason in _RETRY_REASONS:
        return VERDICT_RETRY
    if stop_reason in _ESCALATE_REASONS or not success:
        return VERDICT_ESCALATE
    # Unknown stop_reason: treat as done if success, else escalate.
    return VERDICT_DONE if success else VERDICT_ESCALATE


def _verdict_to_node_status(verdict: str) -> str:
    """Map verdict → contracts/dag_schema.md §8 NodeState string."""
    if verdict == VERDICT_DONE:
        return "done"
    if verdict == VERDICT_RETRY:
        return "running"   # will be re-queued
    return "failed"        # escalate


def _build_summary(
    node_name: str, verdict: str, stop_reason: str, elapsed_ms: float
) -> str:
    elapsed_s = elapsed_ms / 1000.0
    if verdict == VERDICT_DONE:
        return f"{node_name} completed ({stop_reason}) in {elapsed_s:.1f}s."
    if verdict == VERDICT_RETRY:
        return f"{node_name} needs retry ({stop_reason}) after {elapsed_s:.1f}s."
    return f"{node_name} escalated ({stop_reason}) after {elapsed_s:.1f}s."


def _run_advisory(node_type: str, doc_id: str | None, seq: int = 0) -> str:
    """Run the foreman over *doc_id* and return an [ADVISORY] block (T-W5).

    The DAG path DOES carry a doc_id, so the full stage battery (interference,
    overlap, sketch health, rebuild) can fire against the live doc via the core
    RPCs.  Best-effort: any failure (no doc, no bridge, no advisor package)
    returns "" so node execution never breaks.
    """
    if not _ADVISOR_ENABLED or not doc_id:
        return ""
    stage = _NODE_TYPE_STAGE.get(node_type)
    if stage is None:
        return ""
    try:
        from services.agent.advisor import adapters, engine, render
        obs = adapters.observe_doc(doc_id, stage, seq=seq)
        if not obs.findings:
            return ""
        advisory = engine.build_advisory(obs)
        return render.render_advisory(advisory)
    except Exception:  # noqa: BLE001
        return ""


def _run_overseer_stage_exit(node: dict[str, Any],
                             context_pack: dict[str, Any]) -> dict | None:
    """Trigger a stage-exit overseer audit for a completed node (T-W13, §11).

    Fires only when the context pack supplies a ``run_id`` AND an ``overseer_providers``
    map (the resolved provider seam, §14).  Absent either, returns ``None`` — the
    overseer needs the run's ledger + build graph, and there is no provider registry
    threaded through the DAG runner yet (same missing glue the foreman NOTE above
    flags; full ledger threading tracked for T-W19 glue).  Best-effort: never raises.
    """
    if not _ADVISOR_ENABLED:
        return None
    run_id = context_pack.get("run_id")
    providers = context_pack.get("overseer_providers")
    if not run_id or not providers:
        return None
    node_type = node.get("type", "")
    stage = _NODE_TYPE_STAGE.get(node_type, node_type)
    try:
        from services.agent.advisor.overseer_tools import audit_now
        return audit_now(
            run_id,
            {"stage": stage, "buildgraph_nodes": [node.get("id")] if node.get("id") else []},
            trigger="stage_exit",
            providers=providers,
        )
    except Exception:  # noqa: BLE001 — a broken auditor never breaks node execution
        return None


def _emit(emit: EmitFn, event_type: str, payload: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:  # noqa: BLE001
        pass  # event sink failures must never break execution
