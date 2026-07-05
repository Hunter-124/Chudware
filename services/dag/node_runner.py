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


def make_default_agent_fn(ledger=None,
                          mission_dispatch=None) -> "Callable[[str, dict[str, Any]], Any]":
    """Return the default tool-executing agent_fn with *ledger* bound (T-W30).

    The mission pipeline (loop.run_mission) uses this to thread its run ledger
    into every node's agent turn, so each turn's tool calls / handoffs land as
    ``agent_turn`` events — the inspectability half of the execution-reality
    contract.  A standalone DAG run (no ledger) gets the same behaviour minus
    the events.

    T-W32: when a *ledger* is bound and no explicit *mission_dispatch* is given,
    a run-scoped :class:`services.agent.mission_dispatch.MissionDispatch` is built
    from it, so run-scoped tools (ledger_get/ledger_note, dag_plan, ask_user,
    delegate, part_search) resolve against THIS run instead of erroring out of
    the process-wide registry.  Best-effort: a failure to build the dispatcher
    never blocks node execution (turns just lack the mission layer)."""
    if mission_dispatch is None and ledger is not None:
        try:
            from services.agent.mission_dispatch import MissionDispatch  # noqa: PLC0415
            mission_dispatch = MissionDispatch(
                run_id=getattr(ledger, "run_id", "") or "", ledger=ledger)
        except Exception:  # noqa: BLE001 — mission layer optional
            mission_dispatch = None

    def _agent_fn(request: str, context_pack: dict[str, Any]) -> Any:
        return _default_agent_fn(request, context_pack, ledger=ledger,
                                 mission_dispatch=mission_dispatch)
    return _agent_fn


def _default_agent_fn(request: str, context_pack: dict[str, Any], *,
                      ledger=None, mission_dispatch=None) -> Any:
    """Run a real tool-executing agent turn for this DAG node (T-W30).

    Resolves the provider from the agent config seam (``config.get_provider`` —
    honours ``AI_CAD_PROVIDER`` / routing.yaml / the local llama.cpp default), then
    runs a bounded tool-calling turn (``agent_turn.run_turn``): the model proposes
    MCP tool calls, they are dispatched against the live engine, results are fed
    back, and a structured handoff is returned.  The node's ``allowed_tools`` scope
    the callable tools.

    Provider resolution is the actual fix for the historical silent-stub bug: the
    old code imported ``services.agent.providers.anthropic`` (wrong module name —
    the module is ``anthropic_provider``), so the import ALWAYS failed and every
    "live" node degraded to ``_StubAgentResult``.  We now import the real seam.

    Fail-closed on genuine unavailability (no provider constructible — e.g. no model
    server and no API key): return a NON-success stub so the node ESCALATES rather
    than silently reporting a fabricated success.  A raised exception is surfaced by
    ``run()`` as an escalate verdict."""
    from services.agent import config  # noqa: PLC0415 — real import, no wrong name
    from services.agent import agent_turn  # noqa: PLC0415

    node_name = context_pack.get("node_name", "dag_node")
    try:
        provider = config.get_provider()
    except Exception as exc:  # noqa: BLE001 — no constructible provider
        # Do NOT return a confirmed stub: that is the fabrication bug. Signal a
        # non-success so the node escalates and the failure is visible.
        return _UnavailableAgentResult(f"no provider available: {exc}")

    allowed_tools = context_pack.get("allowed_tools")
    # Per-node tool budget: context pack value, else an env cap (lets a bench cap the
    # turn length for a time-boxed real run), else the agent_turn default.
    max_tools = context_pack.get("max_tools_per_turn")
    if max_tools is None:
        env_cap = os.environ.get("AI_CAD_AGENT_MAX_TOOL_CALLS")
        if env_cap:
            try:
                max_tools = int(env_cap)
            except ValueError:
                max_tools = None

    # T-W32: render the §9.1 tools slot — trimmed schemas for EXACTLY the node's
    # allowed tools, within the tier's budgets.tools, resolved through the SAME
    # mission-first resolver the dispatcher uses.  Measured cost is logged to the
    # ledger (one tools_slot event per node turn) — recorded, never invented.
    profile = _tier_profile()
    tools_text = None
    if allowed_tools:
        try:
            from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
            tools_text, meta = render_tools_block(
                list(allowed_tools),
                resolver=agent_turn.resolver_for(mission_dispatch),
                budget_tokens=(profile.budgets.tools if profile else None),
                trim=(profile.tool_schema_trim if profile else True))
            if ledger is not None and tools_text:
                try:
                    ledger.append(node_name, "agent_turn", {
                        "event": "tools_slot",
                        "tier": profile.name if profile else None, **meta})
                except Exception:  # noqa: BLE001 — logging is best-effort
                    pass
        except Exception:  # noqa: BLE001 — schema rendering must never block a node
            tools_text = None

    return agent_turn.run_turn(
        request=request, provider=provider, name=node_name,
        allowed_tools=allowed_tools,
        system_prompt=context_pack.get("role_system_prompt"),
        max_tool_calls=max_tools,
        max_tokens=(int(profile.generation_headroom) if profile else None),
        ledger=ledger, actor=node_name,
        mission_dispatch=mission_dispatch, tools_text=tools_text)


def _tier_profile():
    """The ACTIVE tier's profile from ``AI_CAD_TIER`` (T-W28 seam), or None.

    Same manual-override knob the effort resolution path honours.  None when no
    tier is set or the tiers layer is unavailable — best-effort, never blocks
    node execution (callers fall back to agent_turn / tool_schemas defaults)."""
    try:
        from services.agent.tiers import detect_tier_from_env, load_tier
        tier_name = detect_tier_from_env()
        if not tier_name:
            return None
        return load_tier(tier_name)
    except Exception:  # noqa: BLE001 — tiers layer optional at node level
        return None


class _UnavailableAgentResult:
    """Returned when no provider can be constructed — a NON-success result so the
    node escalates (never a fabricated ``confirmed`` success)."""
    def __init__(self, reason: str) -> None:
        self.stop_reason = "failed"
        self.success = False
        self.final_code = None
        self.result = {"error": reason}


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
    restore_tool_policy = None
    if role_card is not None:
        # T-W30: the registry policy is PROCESS-GLOBAL — scope it to THIS node's
        # turn (snapshot now, restore after the agent returns). Leaving it set
        # leaked one node's role allowlist into every later dispatch in the
        # process (caught as ~78 cad_tools/gsl_tools refusals in the gate sweep
        # once mission nodes started resolving their cards).
        restore_tool_policy = _snapshot_tool_policy()
        _apply_role_tool_policy(role_card)
        context_pack = dict(context_pack)
        context_pack["role_id"] = role_card.id
        context_pack["role_system_prompt"] = role_card.system_prompt
        # The node's tools: allowlist and the card's allowlist should agree; the card
        # is authoritative for a role-typed node, so expose its concrete tools.
        # T-W30: context_pack.assemble() ALWAYS sets "allowed_tools" (None = open),
        # so the old setdefault() never fired and the card allowlist was silently
        # dropped — the real C1 run showed the model free-inventing tool names
        # because its prompt listed none. The card fills the OPEN case only; an
        # explicit node ``tools:`` list still wins.
        if context_pack.get("allowed_tools") is None:
            context_pack["allowed_tools"] = list(role_card.concrete_tools)
    elif node_inputs.get("role_prompt"):
        # No loadable card, but the mission compiler embedded the role prompt
        # verbatim in inputs (mission_template._node) — use it so the node still
        # runs with its role's system prompt.
        context_pack = dict(context_pack)
        context_pack["role_system_prompt"] = str(node_inputs["role_prompt"])

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
    finally:
        # Restore the pre-turn registry policy (role scoping is per-turn).
        if restore_tool_policy is not None:
            restore_tool_policy()

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
    returns None so the node runs with default behaviour.

    T-W30: the mission compiler (services/dag/mission_template._node) carries the
    role in ``inputs.role`` — accept it as well as a top-level ``role`` field.
    Reading only ``node['role']`` meant NO mission-graph node ever resolved its
    card, so role prompts + tool allowlists were silently dropped (the real C1
    run showed the model free-inventing tool names as a direct result)."""
    role_id = node.get("role") or (node.get("inputs") or {}).get("role")
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


def _snapshot_tool_policy() -> "Optional[Callable[[], None]]":
    """Capture the tool_registry's current policy; return a restore callable.

    T-W30: ``set_policy`` mutates module globals, so a role's allowlist applied
    for one node turn would otherwise leak into the whole process (every later
    dispatch, and every later test in a combined sweep). Reads the registry's
    private fields deliberately — there is no public getter — with the same
    best-effort discipline as ``_apply_role_tool_policy``."""
    try:
        from mcp_server import tool_registry as reg  # noqa: PLC0415
        prev_allow = set(reg._ACTIVE_ALLOW) if reg._ACTIVE_ALLOW is not None else None
        prev_deny = set(reg._ACTIVE_DENY_RISK) if reg._ACTIVE_DENY_RISK is not None else None
        prev_set = bool(reg._POLICY_SET)

        def _restore() -> None:
            try:
                if prev_set:
                    reg.set_policy(allow=prev_allow, deny_risk=prev_deny)
                else:
                    reg.reset_policy()
            except Exception:  # noqa: BLE001
                pass
        return _restore
    except Exception:  # noqa: BLE001
        return None


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
