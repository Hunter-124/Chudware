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

import hashlib
import os
import time
from typing import Any, Callable, Optional


# Advisory engine / foreman (T-W5, Pillar 20): after a node step, the foreman runs
# the stage battery over the node's doc (if it carries one) and the advisory block
# is attached to output_state["advisory"] for injection into the next node's
# context pack.  Gated by CHUDWARE_ADVISOR (default on); best-effort.
_ADVISOR_ENABLED = os.environ.get("CHUDWARE_ADVISOR", "1") not in (
    "0",
    "false",
    "False",
)

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
# stop_reason values that map to retry without further disposition data.
# Provider failures are excluded: their structured ``result.retryable`` flag is
# authoritative, so malformed/replay-unsafe requests never re-enter the DAG.
# ``context_overflow`` is explicitly replay-safe: a fresh node attempt rebuilds
# a trimmed transcript, which is the recovery agent_turn requests.
# ``reply_truncated`` likewise: the provider cut the reply off at its output cap
# after agent_turn already asked once for a compact re-emit, and a fresh attempt
# (now funded with the shared completion headroom, see
# tool_budget.completion_cap_for_subagent) can genuinely fit the directive. It
# used to arrive as ``no_code`` and escalate immediately, attributing a provider
# output cap to model inaction.
_RETRY_REASONS = frozenset(
    [
        "max_iters",
        "phase_step_cap",
        "guardrail_blocked",
        "blocked",
        "context_overflow",
        "reply_truncated",
    ]
)
# stop_reason values that map to escalate (failed build / no model output / auth)
_ESCALATE_REASONS = frozenset(
    [
        "failed",
        "no_code",
        "provider_auth",
        "escalated",
    ]
)

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
# Default Chudcode role-turn function
# ---------------------------------------------------------------------------


def make_default_agent_fn(
    ledger=None,
    mission_dispatch=None,
    provider=None,
    delegation_tree=None,
    on_event=None,
) -> "Callable[[str, dict[str, Any]], Any]":
    """Return the default Chudcode-backed DAG agent function with run state bound.

    The ledger, mission dispatcher, delegation tree, and event sink stay attached
    to every node so tool calls and handoffs retain their existing run lineage.
    ``provider`` is retained only for an explicitly offline node; online nodes
    never construct or invoke a Python provider.

    When a ledger is bound and no dispatcher is supplied, this creates the same
    run-scoped mission tool surface used by nested roles.
    """
    # Thread the mission's live delegation layer into the Chudcode host-tool
    # dispatcher. Provider execution itself belongs to Chudcode.
    if mission_dispatch is None and ledger is not None:
        try:
            from services.agent.mission_dispatch import MissionDispatch  # noqa: PLC0415
            from services.agent.corporate import get_org_chart  # noqa: PLC0415

            rid = getattr(ledger, "run_id", "") or ""
            org = get_org_chart(rid, create=False) if rid else None
            if org is not None:
                if on_event is not None and org.on_event is None:
                    org.on_event = on_event
            mission_dispatch = MissionDispatch(
                run_id=rid,
                ledger=ledger,
                delegation_tree=delegation_tree,
                provider=None,
                on_event=on_event,
                org_chart=org,
            )
            # Live-hire heads once a real dispatch+tree exists (gap 2).
            if (
                org is not None
                and getattr(org, "active", False)
                and delegation_tree is not None
            ):
                try:
                    forming = [
                        s
                        for s in org.seats.values()
                        if s.rank == "head"
                        and (s.status == "forming" or not s.delegation_node)
                    ]
                    if forming:
                        org.seat_heads(delegate_fn=mission_dispatch.delegate)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — mission layer optional
            mission_dispatch = None

    def _agent_fn(request: str, context_pack: dict[str, Any]) -> Any:
        return _default_agent_fn(
            request,
            context_pack,
            ledger=ledger,
            mission_dispatch=mission_dispatch,
            provider=provider,
        )

    return _agent_fn


def _resolve_role_settings(
    context_pack: dict[str, Any],
    *,
    injected: Any = None,
    ledger: Any = None,
) -> dict[str, Any]:
    """Resolve CAD role/model governance without constructing a provider."""
    from services.agent import config as agent_config  # noqa: PLC0415
    from services.agent import role_models  # noqa: PLC0415
    from services.agent.agent_events import emit_event  # noqa: PLC0415

    node_inputs = context_pack.get("node_inputs") or {}
    role = str(
        context_pack.get("role_id")
        or context_pack.get("role")
        or node_inputs.get("role")
        or "general_agent"
    )
    harness_role = role_models.harness_role_for_mission_role(role)
    task_class = context_pack.get("task_class") or node_inputs.get("task_class")
    risk = context_pack.get("risk") or node_inputs.get("risk")
    try:
        depth = int(context_pack.get("depth") or node_inputs.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        cfg = dict(agent_config.load_config() or {})
    except Exception:  # noqa: BLE001 — Chudcode still receives explicit settings
        cfg = {}

    existing = context_pack.get("role_model")
    row = dict(existing) if isinstance(existing, dict) else {}
    assignment = None
    if not (row.get("provider") and row.get("model")):
        try:
            assignment = role_models.resolve_for_node(role, context_pack, cfg=cfg)
            if assignment is None:
                assignment = role_models.resolve_role_model(
                    harness_role,
                    cfg,
                    task_class=str(task_class) if task_class else None,
                    node_depth=depth,
                    risk=str(risk) if risk else None,
                )
        except Exception:  # noqa: BLE001 — use session/injected identifiers
            assignment = None

    injected_provider = str(
        getattr(injected, "name", None) or getattr(injected, "provider", None) or ""
    ).strip()
    injected_model = str(getattr(injected, "model", None) or "").strip()
    if assignment is not None:
        try:
            row.update(assignment.to_dict())
        except Exception:  # noqa: BLE001
            pass
        if getattr(assignment, "provider", None):
            row["provider"] = str(assignment.provider)
        if getattr(assignment, "model", None):
            row["model"] = str(assignment.model)
    if not row.get("provider"):
        row["provider"] = injected_provider or str(cfg.get("provider") or "")
    if not row.get("model"):
        row["model"] = injected_model or str(cfg.get("model") or "")

    min_rank = role_models.min_rank_for_difficulty(
        task_class=str(task_class) if task_class else None,
        node_depth=depth,
        risk=str(risk) if risk else None,
    )
    row["min_rank"] = min_rank
    if row.get("provider"):
        row["cost_tier"] = role_models.cost_tier_for(
            str(row["provider"]), str(row.get("model") or "")
        )
    effort = row.get("reasoning_effort")
    if effort is None and assignment is None and injected is not None:
        effort = getattr(injected, "reasoning_effort", None)
    if effort is None:
        try:
            effort = role_models.resolve_role_thinking(
                harness_role,
                cfg,
                task_class=str(task_class) if task_class else None,
                node_depth=depth,
                risk=str(risk) if risk else None,
                min_rank=min_rank,
                rank=getattr(assignment, "rank", None),
            )
        except Exception:  # noqa: BLE001
            effort = None
    if context_pack.get("retry_cheap"):
        effort = "low"
    if effort is None:
        effort = getattr(injected, "reasoning_effort", None) or cfg.get(
            "reasoning_effort"
        )
    effort = role_models.clamp_subagent_effort(effort)
    if effort is not None:
        row["reasoning_effort"] = effort

    context_pack["role_model"] = row
    event = {"event": "model_assignment", **row}
    actor = str(context_pack.get("node_name") or context_pack.get("node_id") or role)
    target_ledger = ledger or context_pack.get("ledger")
    if target_ledger is not None:
        try:
            target_ledger.append(actor, "agent_turn", event)
        except Exception:  # noqa: BLE001
            pass
    emit_event(
        "agent.model_assignment",
        event,
        live=context_pack.get("_event_sink"),
        journal=None,
    )
    return {
        key: row[key]
        for key in ("provider", "model", "reasoning_effort")
        if row.get(key) not in (None, "")
    }


def _default_agent_fn(
    request: str,
    context_pack: dict[str, Any],
    *,
    ledger=None,
    mission_dispatch=None,
    provider=None,
) -> Any:
    """Run one DAG node through the shared Chudcode role-turn service.

    A node is a persistent child lane keyed by its parent session/run and node
    identity.  Only an explicitly offline node may reuse the deterministic local
    provider loop; an injected legacy provider is ignored for online execution.
    """
    from services.agent.chudcode_roles import run_role_turn  # noqa: PLC0415
    from services.agent.tool_budget import (  # noqa: PLC0415
        completion_cap_for_subagent,
    )

    node_inputs = context_pack.get("node_inputs") or {}
    node_id = str(
        context_pack.get("node_id")
        or node_inputs.get("node_id")
        or context_pack.get("node_name")
        or "dag-node"
    )
    run_id = str(context_pack.get("run_id") or node_inputs.get("run_id") or "")
    session_root = str(context_pack.get("session_id") or "")
    if mission_dispatch is not None and session_root:
        current = mission_dispatch.execution_options.get("session_id")
        if current and current != session_root:
            raise ValueError("not_found")
        mission_dispatch.execution_options["session_id"] = session_root
    parent_id = str(
        context_pack.get("parent_id") or f"{run_id or session_root}:{node_id}"
    )
    role = str(
        context_pack.get("role_id")
        or context_pack.get("role")
        or node_inputs.get("role")
        or "general_agent"
    )
    profile = _tier_profile(context_pack)
    tier = (
        context_pack.get("tier")
        or node_inputs.get("tier")
        or (profile.name if profile is not None else None)
    )
    capability = _capability_profile(profile)
    offline = context_pack.get("offline") is True or node_inputs.get("offline") is True
    role_run_id = (
        "dag-node-"
        + hashlib.sha256(
            f"{run_id or session_root}|{node_id}".encode("utf-8")
        ).hexdigest()[:24]
    )
    options: dict[str, Any] = {
        "ledger": ledger,
        "session_id": session_root,
        "mission_dispatch": mission_dispatch,
        "allowed_tools": context_pack.get("allowed_tools"),
        "autonomy": context_pack.get("autonomy") or node_inputs.get("autonomy"),
        "doc_id": node_inputs.get("doc_id") or context_pack.get("doc_id"),
        "phase": context_pack.get("phase"),
        "task_class": context_pack.get("task_class") or node_inputs.get("task_class"),
        "node_id": node_id,
        "subagent_id": node_id,
        "mode": context_pack.get("mode"),
        "plan_only": context_pack.get("plan_only"),
        "workspace_dir": (
            context_pack.get("workspace_dir")
            or node_inputs.get("workspace_dir")
            or node_inputs.get("project_dir")
        ),
        "project_dir": (
            context_pack.get("project_dir")
            or node_inputs.get("project_dir")
            or node_inputs.get("workspace_dir")
        ),
        "categories": (
            context_pack.get("categories")
            or context_pack.get("enabled_categories")
            or ()
        ),
        "offline": offline,
    }
    options["settings"] = _resolve_role_settings(
        context_pack,
        injected=provider,
        ledger=ledger,
    )
    if offline and provider is not None:
        options["offline_provider"] = provider

    turn = run_role_turn(
        request,
        role,
        session_id=session_root,
        run_id=role_run_id,
        parent_id=parent_id,
        on_event=context_pack.get("_event_sink"),
        options=options,
        tier=tier,
        capability=capability,
        max_tokens=completion_cap_for_subagent(profile),
        close_session=False,
    )
    return _RoleTurnAgentResult(turn)


class _RoleTurnAgentResult:
    """AgentResult-compatible view over the shared role-turn result contract."""

    def __init__(self, turn: Any) -> None:
        payload = (
            dict(turn)
            if isinstance(turn, dict)
            else {
                "success": False,
                "stop_reason": "provider_error",
                "result": {"error": "Agent role turn returned a non-object result"},
            }
        )
        self.success = bool(payload.get("success"))
        self.stop_reason = str(payload.get("stop_reason") or "provider_error")
        self.final_code = payload.get("final_code")
        raw_result = payload.get("result")
        if isinstance(raw_result, dict):
            result = dict(raw_result)
        else:
            result = {
                "handoff": {
                    "status": "done" if self.success else "failed",
                    "summary": str(raw_result or payload.get("error") or ""),
                }
            }
        result.setdefault("success", self.success)
        result.setdefault("stop_reason", self.stop_reason)
        for key in (
            "run_id",
            "session_id",
            "parent_id",
            "chudcode_session_id",
            "chudcode_session_file",
            "usage",
            "cancelled",
            "offline",
            "calls",
            "tool_calls",
        ):
            if key in payload:
                result[key] = payload[key]
        self.result = result


def _tier_profile(context_pack: dict[str, Any] | None = None):
    """Resolve the active tier profile from node context before the environment.

    Compiled DAG nodes carry their assigned tier in ``context_pack`` or
    ``node_inputs``. Falling back to ``CHUDWARE_TIER`` only when absent keeps
    per-node provider, generation, schema, and scaffold policy on one profile.
    Parameterized xAI/OpenRouter tier strings normalize to ``frontier``.
    """
    try:
        from services.agent.tiers import (  # noqa: PLC0415
            detect_tier_from_env,
            profile_tier_for,
            load_tier,
        )

        pack = context_pack or {}
        node_inputs = pack.get("node_inputs") or {}
        tier_name = (
            pack.get("tier") or node_inputs.get("tier") or detect_tier_from_env()
        )
        if not tier_name:
            return None
        return load_tier(profile_tier_for(str(tier_name)) or str(tier_name))
    except Exception:  # noqa: BLE001 — tiers layer optional at node level
        return None


def _capability_profile(profile: Any | None) -> Any | None:
    if profile is None:
        return None
    try:
        from services.agent.capability import resolve_capability  # noqa: PLC0415

        return resolve_capability(profile.name)
    except Exception:  # noqa: BLE001 — capability scaffolds are best-effort
        return None


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
        # Chudcode receives the role/tool scope in-band; never mutate the
        # process-global registry around a concurrent node turn.
        context_pack = dict(context_pack)
        context_pack["role_id"] = role_card.id
        # WI-16: append per-capability SOFT phrasing (A mandatory / B suggestive /
        # C minimal HARD-floor mirror). Letter from active tier; default C.
        try:
            from services.agent.prompt_tiers import (  # noqa: PLC0415
                assemble_system_for_capability,
                letter_from_capability,
            )
            from services.agent.capability import resolve_capability  # noqa: PLC0415

            _prof = _tier_profile(context_pack)
            _tier_name = _prof.name if _prof is not None else ""
            _cap = resolve_capability(_tier_name) if _tier_name else None
            _letter = letter_from_capability(_cap)
            context_pack["capability_letter"] = _letter
            context_pack["role_system_prompt"] = assemble_system_for_capability(
                role_card.system_prompt, _letter
            )
        except Exception:  # noqa: BLE001 — never block node on soft phrasing
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

    # Seed the Chudcode event sink + span context through the same callback used
    # by DAG lifecycle events. A None sink is harmless.
    if emit is not None:
        context_pack = dict(context_pack)
        context_pack.setdefault("_event_sink", emit)
        context_pack.setdefault("node_id", node_id)
        context_pack.setdefault("node_name", node_name)

    # Emit node.started (§11).
    _emit(
        emit,
        "dag.node.started",
        {
            "node_id": node_id,
            "node_type": node_type,
            "node_name": node_name,
        },
    )

    t0 = time.monotonic()
    agent_error: Exception | None = None
    try:
        agent_result = _agent_fn(request, context_pack)
    except Exception as exc:  # noqa: BLE001
        agent_error = exc

    if agent_error is not None:
        # Unrecoverable agent error → escalate immediately.
        _emit(
            emit,
            "dag.node.failed",
            {
                "node_id": node_id,
                "error": str(agent_error),
            },
        )
        output_state = {
            "status": "failed",
            "summary": f"Agent raised exception: {agent_error}",
            "data": {"error": str(agent_error)},
            "retry_remaining": retry_remaining,
            "doc_id": node_inputs.get("doc_id"),
        }
        return VERDICT_ESCALATE, output_state

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    # Map stop_reason → initial verdict.
    stop_reason: str = getattr(agent_result, "stop_reason", "") or ""
    success: bool = bool(getattr(agent_result, "success", False))

    verdict = _map_verdict(stop_reason, success)
    # Provider retryability is structured data, not a stop-reason guess. Same
    # predicate the `delegate` lane gates its own one-shot recovery on
    # (mission_dispatch.retryable_child_failure) so the two dispatch paths
    # cannot drift on WHICH faults are transient; it covers both the legacy
    # "failed" and the current "provider_error" stop_reason shapes.
    try:
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            retryable_child_failure,
        )

        _retryable = retryable_child_failure(agent_result)
    except Exception:  # noqa: BLE001 — an unclassifiable failure escalates
        _retryable = False
    if _retryable:
        verdict = VERDICT_RETRY
        # Next attempt should prefer cheaper effort/provider band.
        try:
            context_pack["retry_cheap"] = True
        except Exception:  # noqa: BLE001
            pass

    # mega-8 (advisor Q2): multi-domain PLAN/BUILD soft gate — auto-complete
    # blocks until a real delegate call OR a TYPED refusal envelope
    # {reason, domains_considered, why_flat}. Free-form ledger_note does NOT
    # satisfy. Only when depth < max_spawn_depth.
    if verdict == VERDICT_DONE:
        blocked, gate_info = _multi_domain_delegate_gate(
            node,
            context_pack,
            agent_result,
        )
        if blocked:
            verdict = VERDICT_RETRY
            stop_reason = "delegate_or_typed_refusal_required"
            success = False
            # Stash gate info on the result for output_state / tests / retry request.
            try:
                rd = getattr(agent_result, "result", None)
                if isinstance(rd, dict):
                    rd["delegate_gate"] = gate_info
                else:
                    agent_result.result = {
                        "delegate_gate": gate_info,
                        "handoff": rd,
                    }
            except Exception:  # noqa: BLE001
                pass
            try:
                context_pack["delegate_gate"] = gate_info
            except Exception:  # noqa: BLE001
                pass

    # Consume retry budget when the verdict is retry.
    new_retry_remaining = retry_remaining
    retry_budget_exhausted = False
    if verdict == VERDICT_RETRY:
        new_retry_remaining = max(0, retry_remaining - 1)
        if new_retry_remaining == 0:
            verdict = VERDICT_ESCALATE
            retry_budget_exhausted = True
    # Build output_state.
    final_code: str | None = getattr(agent_result, "final_code", None)
    agent_result_dict: dict | None = getattr(agent_result, "result", None)
    doc_id = node_inputs.get("doc_id")
    # mega-9 a2: BUILD often creates the part via new_part without stamping
    # inputs.doc_id — lift it from the agent result so VERIFY/battery can target
    # the mission-built doc when ≥2 parts are open.
    if not doc_id:
        doc_id = _extract_doc_id_from_agent_result(agent_result_dict)

    # mega-8 a4: SqliteSaver raised "string or blob too big" when node_states
    # accumulated full tool results / handoffs. Keep a compact agent_result for
    # checkpointing (ledger already has full tool_call trails).
    compact_ar = _compact_agent_result(agent_result_dict)

    output_state: dict[str, Any] = {
        "status": _verdict_to_node_status(verdict),
        "summary": _build_summary(node_name, verdict, stop_reason, elapsed_ms),
        "data": {
            "stop_reason": stop_reason,
            "final_code": final_code,
            "agent_result": compact_ar,
            "elapsed_ms": round(elapsed_ms, 1),
            **({"doc_id": doc_id} if doc_id else {}),
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

    # Every completed run-scoped node gets a one-shot Chudcode overseer audit.
    # The audit itself remains best-effort and cannot break node completion.
    if verdict == VERDICT_DONE:
        ov_out = _run_overseer_stage_exit(node, context_pack)
        if ov_out:
            output_state["overseer_audit"] = ov_out

    # Emit appropriate §11 completion event.
    if verdict == VERDICT_DONE:
        _emit(
            emit,
            "dag.node.done",
            {
                "node_id": node_id,
                "output": output_state,
            },
        )
    elif verdict == VERDICT_RETRY:
        # Still has budget — will be re-routed; emit paused so mission-control
        # UI can show the node as waiting to retry.
        _emit(
            emit,
            "dag.node.paused",
            {
                "node_id": node_id,
                "reason": f"retry (budget remaining: {new_retry_remaining})",
            },
        )
    else:  # escalate
        error = f"escalated: {stop_reason}"
        if retry_budget_exhausted:
            error += " (retry budget exhausted)"
        _emit(
            emit,
            "dag.node.failed",
            {
                "node_id": node_id,
                "error": error,
            },
        )

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

    # mega-8 F9: multi-domain PLAN/BUILD must see the typed-refusal escape
    # hatch in the request (gate kills free-form auto-complete otherwise).
    try:
        from services.agent.role_models import (  # noqa: PLC0415
            domain_tags_from_context,
            is_multi_domain_plan_or_build,
        )

        role_id = context_pack.get("role_id") or node_inputs.get("role") or ""
        tags = domain_tags_from_context(context_pack, node_inputs)
        if is_multi_domain_plan_or_build(
            role_id=str(role_id) if role_id else None,
            node_type=str(node_type or ""),
            domain_tags=tags,
            task_class=str(
                context_pack.get("task_class") or node_inputs.get("task_class") or ""
            )
            or None,
        ):
            parts.append(
                "\n[CAPABILITY / DELEGATION GATE] Multi-domain PLAN/BUILD: "
                "before you finish you MUST either (1) call delegate(role, brief, "
                "budget_tokens) for ≥1 independent slice, OR (2) emit a typed "
                "refusal object in your handoff outputs. Exact schema (all three "
                "keys required; free-form ledger_note does NOT satisfy):\n"
                '  "typed_refusal": {\n'
                '    "reason": "<short why you stay flat — non-empty string>",\n'
                '    "domains_considered": ["geometry","assembly"],\n'
                '    "why_flat": "<why spawn would not help — non-empty string>"\n'
                "  }\n"
                "Example valid envelope:\n"
                '  {"reason":"single integrated solid","domains_considered":'
                '["geometry","assembly"],"why_flat":"mates share one doc; '
                'spawn thrash"}\n'
                "Place it under handoff.outputs.typed_refusal (or top-level "
                "typed_refusal). domains_considered MUST be a non-empty array "
                "of non-empty strings."
            )
            # Retry path: prior gate failure hint with paste-ready envelope.
            prev = context_pack.get("delegate_gate") or node_inputs.get("delegate_gate")
            if isinstance(prev, dict) and prev.get("blocked"):
                parts.append(
                    "\n[GATE RETRY] Previous turn was blocked: "
                    f"{prev.get('reason') or 'missing_delegate_or_typed_refusal'}. "
                    "Call delegate(role, brief, budget_tokens) NOW, OR return ONLY this "
                    "exact JSON (no free-form prose) under handoff.outputs.typed_refusal:\n"
                    '{"reason":"single integrated solid","domains_considered":'
                    '["geometry","assembly"],"why_flat":"mates share one doc; spawn thrash"}\n'
                    "Do not free-form complete. ledger_note alone will be blocked again."
                )
    except Exception:  # noqa: BLE001
        pass

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


def _extract_doc_id_from_agent_result(agent_result_dict: Any) -> str | None:
    """Best-effort doc_id from handoff outputs / tool call results."""
    if not isinstance(agent_result_dict, dict):
        return None
    handoff = agent_result_dict.get("handoff")
    if isinstance(handoff, dict):
        outs = handoff.get("outputs")
        if isinstance(outs, dict) and outs.get("doc_id"):
            return str(outs["doc_id"]).strip() or None
        if handoff.get("doc_id"):
            return str(handoff["doc_id"]).strip() or None
    if agent_result_dict.get("doc_id"):
        return str(agent_result_dict["doc_id"]).strip() or None
    calls = agent_result_dict.get("calls")
    if isinstance(calls, list):
        for c in reversed(calls):
            if not isinstance(c, dict):
                continue
            if c.get("tool") not in ("new_part", "add_features", "open_part"):
                continue
            res = c.get("result")
            if isinstance(res, str):
                try:
                    import json as _json  # noqa: PLC0415

                    res = _json.loads(res)
                except Exception:  # noqa: BLE001
                    res = None
            if isinstance(res, dict) and res.get("doc_id"):
                return str(res["doc_id"]).strip() or None
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            if args.get("doc_id"):
                return str(args["doc_id"]).strip() or None
    return None


def _compact_agent_result(agent_result_dict: Any) -> Any:
    """Shrink agent_result for SqliteSaver node_states (mega-8 a4 blob crash).

    Keep stop/handoff summary + tool names/ok flags; drop large result bodies.
    """
    if not isinstance(agent_result_dict, dict):
        return agent_result_dict
    out: dict[str, Any] = {}
    for k in (
        "stop_reason",
        "one_shot",
        "leaf_envelope",
        "delegate_gate",
        "typed_refusal",
        "success",
        "auto_complete",
        "cancelled",
        "offline",
        "run_id",
        "session_id",
        "parent_id",
        "chudcode_session_id",
        "chudcode_session_file",
    ):
        if k in agent_result_dict:
            out[k] = agent_result_dict[k]
    handoff = agent_result_dict.get("handoff")
    if isinstance(handoff, dict):
        out["handoff"] = {
            "status": handoff.get("status"),
            "summary": str(handoff.get("summary") or "")[:600],
        }
        outs = handoff.get("outputs")
        if isinstance(outs, dict):
            slim = {}
            _KEEP_OUT = (
                "typed_refusal",
                "leaf_envelope",
                "one_shot",
                "auto_complete",
                "productive_tools",
                "wrote_ledger",
                "no_op",
                "reason",
                "doc_id",
                "checks_run",
            )
            for ok, ov in outs.items():
                if ok not in _KEEP_OUT:
                    # Drop unknown fat keys (mesh dumps, free-form blobs).
                    if isinstance(ov, str) and len(ov) > 120:
                        continue
                    if isinstance(ov, (dict, list)):
                        continue
                if ok in _KEEP_OUT:
                    slim[ok] = ov
                elif isinstance(ov, str):
                    slim[ok] = ov[:120]
                elif isinstance(ov, (int, float, bool)) or ov is None:
                    slim[ok] = ov
            if slim:
                out["handoff"]["outputs"] = slim
    calls = agent_result_dict.get("calls")
    if isinstance(calls, list):
        out["calls"] = [
            {"tool": c.get("tool"), "ok": c.get("ok")}
            for c in calls[:40]
            if isinstance(c, dict)
        ]
        out["tool_calls"] = len(calls)
    elif "tool_calls" in agent_result_dict:
        out["tool_calls"] = agent_result_dict.get("tool_calls")
    # Cap overall JSON size hard (checkpoint + emit paths share this).
    try:
        import json as _json  # noqa: PLC0415

        blob = _json.dumps(out, default=str)
        if len(blob) > 8000:
            out = {
                "stop_reason": out.get("stop_reason"),
                "handoff": {
                    "status": (out.get("handoff") or {}).get("status")
                    if isinstance(out.get("handoff"), dict)
                    else None,
                    "summary": str(
                        (out.get("handoff") or {}).get("summary")
                        if isinstance(out.get("handoff"), dict)
                        else ""
                    )[:300],
                },
                "tool_calls": out.get("tool_calls"),
                "calls": out.get("calls", [])[:15],
                "truncated": True,
            }
    except Exception:  # noqa: BLE001
        pass
    return out


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
        return "running"  # will be re-queued
    return "failed"  # escalate


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


def _run_overseer_stage_exit(
    node: dict[str, Any], context_pack: dict[str, Any]
) -> dict | None:
    """Run the one-shot Chudcode overseer audit for a completed node.

    The run id is the persistence/checkpoint root; the node id becomes the audit
    lineage parent. A missing run id remains the only no-op case.
    """
    if not _ADVISOR_ENABLED:
        return None
    run_id = context_pack.get("run_id")
    if not run_id:
        return None
    node_type = node.get("type", "")
    stage = _NODE_TYPE_STAGE.get(node_type, node_type)
    try:
        from services.agent.advisor.overseer_tools import audit_now

        offline = (
            context_pack.get("offline") is True
            or (context_pack.get("node_inputs") or {}).get("offline") is True
        )
        return audit_now(
            run_id,
            {
                "stage": stage,
                "buildgraph_nodes": [node.get("id")] if node.get("id") else [],
            },
            trigger="stage_exit",
            providers=context_pack.get("overseer_providers") if offline else None,
            session_id=str(context_pack.get("session_id") or ""),
            on_event=context_pack.get("_event_sink"),
            options={
                "workspace_dir": context_pack.get("workspace_dir"),
                "project_dir": context_pack.get("project_dir"),
            },
            offline=offline,
        )
    except Exception:  # noqa: BLE001 — a broken auditor never breaks node execution
        return None


# ---------------------------------------------------------------------------
# mega-8: multi-domain PLAN/BUILD typed-refusal / delegate soft gate
# ---------------------------------------------------------------------------


def _multi_domain_delegate_gate(
    node: dict[str, Any],
    context_pack: dict[str, Any],
    agent_result: Any,
) -> tuple[bool, dict[str, Any]]:
    """Return ``(blocked, info)`` for the multi-domain soft gate.

    blocked=True means auto-complete must not stand — the node needs either a
    ``delegate`` tool call this turn or a schema-valid typed refusal.
    """
    info: dict[str, Any] = {"gated": False, "blocked": False}
    try:
        from services.agent.role_models import (  # noqa: PLC0415
            domain_tags_from_context,
            extract_typed_refusal,
            is_multi_domain_plan_or_build,
            max_spawn_depth_for_capability,
            validate_typed_refusal,
        )
    except Exception:  # noqa: BLE001
        return False, info

    node_inputs = context_pack.get("node_inputs") or node.get("inputs") or {}
    role_id = (
        context_pack.get("role_id") or node.get("role") or node_inputs.get("role") or ""
    )
    task_class = context_pack.get("task_class") or node_inputs.get("task_class")
    tags = domain_tags_from_context(context_pack, node_inputs)
    # If the pack already stamped domain_tags, prefer those.
    if context_pack.get("domain_tags"):
        try:
            tags = [
                str(t).strip().lower()
                for t in context_pack["domain_tags"]
                if str(t).strip()
            ]
        except Exception:  # noqa: BLE001
            pass

    applies = is_multi_domain_plan_or_build(
        role_id=str(role_id) if role_id else None,
        node_type=str(node.get("type") or ""),
        domain_tags=tags,
        task_class=str(task_class) if task_class else None,
    )
    info.update(
        {
            "gated": applies,
            "role_id": role_id,
            "task_class": task_class,
            "domain_tags": tags,
            "n_domains": len(tags),
        }
    )
    if not applies:
        return False, info

    # Depth check: only enforce when depth < max.
    try:
        depth = int(context_pack.get("depth") or node_inputs.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    letter = context_pack.get("capability_letter")
    try:
        max_d = max_spawn_depth_for_capability(
            str(letter) if letter else None,
        )
    except Exception:  # noqa: BLE001
        max_d = 8
    info["depth"] = depth
    info["max_spawn_depth"] = max_d
    if depth >= max_d:
        info["reason"] = "depth_at_max"
        return False, info

    # ≥2 domain tags required (is_multi_domain may true for plan roles with
    # fewer tags — still require multi-slice evidence via tags OR plan role
    # with multi task_class; gate applies as long as applies=True above).
    # Explicit: if tags < 2 AND not a coordinating plan role, skip.
    plan_roles = {
        "orchestrator",
        "department_manager",
        "layout_engineer",
        "planning",
    }
    if len(tags) < 2 and str(role_id).lower() not in plan_roles:
        info["reason"] = "single_domain"
        return False, info

    result = getattr(agent_result, "result", None) or {}
    if not isinstance(result, dict):
        result = {}

    if _agent_had_delegate_call(result, agent_result):
        info["reason"] = "delegate_called"
        info["blocked"] = False
        return False, info

    refusal = extract_typed_refusal(result)
    if refusal is not None:
        # Re-validate strictly (extract already validates, belt-and-suspenders).
        errs = validate_typed_refusal(refusal)
        if not errs:
            info["reason"] = "typed_refusal"
            info["typed_refusal"] = refusal
            info["blocked"] = False
            return False, info
        info["typed_refusal_errors"] = errs

    # Free-form ledger_note / bare handoff does NOT satisfy.
    # mega-24 a1 residual: pure plan-read thrash (N× ledger_get, no writes)
    # finished DONE then escalated as delegate_or_typed_refusal_required —
    # thrash-kill at 4 node fails. Soft-pass ONLY for read-only productive
    # turns (layout skeleton class). Free-form "doing it myself" with
    # ledger_note/buildgraph_plan still blocks (mega-8 contract).
    try:
        calls = result.get("calls") if isinstance(result, dict) else None
        if not isinstance(calls, list):
            calls = getattr(agent_result, "calls", None) or []
        _READ = {
            "ledger_get",
            "part_search",
            "parts_taxonomy",
            "part_source",
            "describe_part",
            "describe_scene",
            "describe_project",
            "workspace_status",
            "buildgraph_status",
        }
        ok_tools = [
            c.get("tool")
            for c in calls
            if isinstance(c, dict) and c.get("ok") is not False and c.get("tool")
        ]
        n_read = sum(1 for t in ok_tools if t in _READ)
        pure_read = bool(ok_tools) and all(t in _READ for t in ok_tools)
    except Exception:  # noqa: BLE001
        n_read = 0
        pure_read = False
    if pure_read and n_read >= 2:
        synth = {
            "reason": (
                "plan-read thrash without delegate; staying flat with "
                "synthesised typed refusal"
            ),
            "domains_considered": list(tags)
            if tags
            else [
                "plan",
                "geometry",
                "assembly",
                "ecad",
            ],
            "why_flat": (
                "Node only performed plan-read tools (ledger_get/describe/…); "
                "mega-24 soft-pass via synthesised typed_refusal."
            ),
        }
        try:
            if isinstance(result, dict):
                ho = result.get("handoff")
                if not isinstance(ho, dict):
                    ho = {"status": "done", "outputs": {}}
                    result["handoff"] = ho
                outs = ho.get("outputs")
                if not isinstance(outs, dict):
                    outs = {}
                    ho["outputs"] = outs
                outs.setdefault("typed_refusal", synth)
                result.setdefault("typed_refusal", synth)
        except Exception:  # noqa: BLE001
            pass
        info["reason"] = "typed_refusal_synth_plan_read"
        info["typed_refusal"] = synth
        info["blocked"] = False
        return False, info

    info["reason"] = "missing_delegate_or_typed_refusal"
    info["blocked"] = True
    info["hint"] = (
        "Multi-domain PLAN/BUILD at depth < max must call delegate(...) "
        "or emit typed_refusal {reason, domains_considered, why_flat}."
    )
    return True, info


def _agent_had_delegate_call(result: dict[str, Any], agent_result: Any) -> bool:
    """True if this turn successfully invoked the ``delegate`` tool."""
    calls = result.get("calls") if isinstance(result, dict) else None
    if not isinstance(calls, list):
        calls = getattr(agent_result, "calls", None)
    if not isinstance(calls, list):
        return False
    for c in calls:
        if not isinstance(c, dict):
            continue
        if c.get("tool") == "delegate" and c.get("ok") is not False:
            return True
    return False


def _emit(emit: EmitFn, event_type: str, payload: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:  # noqa: BLE001
        pass  # event sink failures must never break execution
