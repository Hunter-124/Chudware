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
# stop_reason values that map to escalate (failed build / no model output / auth)
_ESCALATE_REASONS = frozenset(["failed", "no_code", "provider_auth"])

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
                          mission_dispatch=None,
                          provider=None,
                          delegation_tree=None,
                          on_event=None) -> "Callable[[str, dict[str, Any]], Any]":
    """Return the default tool-executing agent_fn with *ledger* bound (T-W30).

    The mission pipeline (loop.run_mission) uses this to thread its run ledger
    into every node's agent turn, so each turn's tool calls / handoffs land as
    ``agent_turn`` events — the inspectability half of the execution-reality
    contract.  A standalone DAG run (no ledger) gets the same behaviour minus
    the events.

    *provider* (optional): when given, EVERY node run through the returned
    agent_fn uses this exact provider instance instead of re-resolving one from
    ambient config. This is the fix for the historical mission-tier leak:
    ``loop.run_mission(request, provider, ...)`` already resolves the provider
    the mission should run on (e.g. a gym cell's bundled local-tier model), but
    previously only used it for INTAKE — every internal DAG node independently
    called the bare ``config.get_provider()`` here and silently fell through to
    the ambient config.json / ``AI_CAD_PROVIDER`` default (typically a cloud
    provider), regardless of the tier the mission was started with.
    ``loop.run_mission`` now passes its own *provider* through to this
    constructor so internal nodes stay on it. Callers that build a graph
    without an in-hand Provider object (mission_start/dag_run over MCP) can
    instead record a ``tier`` on the node/graph inputs — see
    ``_resolve_provider`` below, which falls back to
    ``services.agent.tiers.provider_for_tier`` for that path.

    T-W32: when a *ledger* is bound and no explicit *mission_dispatch* is given,
    a run-scoped :class:`services.agent.mission_dispatch.MissionDispatch` is built
    from it, so run-scoped tools (ledger_get/ledger_note, dag_plan, ask_user,
    delegate, part_search) resolve against THIS run instead of erroring out of
    the process-wide registry.  Best-effort: a failure to build the dispatcher
    never blocks node execution (turns just lack the mission layer)."""
    # Phase 4 (M11): *delegation_tree* + *on_event* thread the mission's live
    # delegation layer into the dispatch, so a node agent that calls ``delegate``
    # actually spawns a registry-listed sub-agent (streaming ``agent.spawn`` live
    # through *on_event*) and — when *provider* is bound — RUNS it synchronously
    # with GUI steering consumed at its turn boundaries.
    if mission_dispatch is None and ledger is not None:
        try:
            from services.agent.mission_dispatch import MissionDispatch  # noqa: PLC0415
            mission_dispatch = MissionDispatch(
                run_id=getattr(ledger, "run_id", "") or "", ledger=ledger,
                delegation_tree=delegation_tree, provider=provider,
                on_event=on_event)
        except Exception:  # noqa: BLE001 — mission layer optional
            mission_dispatch = None

    def _agent_fn(request: str, context_pack: dict[str, Any]) -> Any:
        return _default_agent_fn(request, context_pack, ledger=ledger,
                                 mission_dispatch=mission_dispatch,
                                 provider=provider)
    return _agent_fn


def _resolve_provider(context_pack: dict[str, Any], provider=None):
    """Resolve the provider for one node's agent turn.

    Priority (highest first) — mega-5 SuperGrok quota fix:
      1. Difficulty-aware auto / ai_roles pick via ``role_models`` (full enabled
         provider set). Leaf / low min_rank work must NOT inherit the mission's
         frontier xAI binding.
      2. *provider* injected by the caller — fallback when auto-pick fails
         (keeps gym local-tier missions from crashing; never overrides a
         successful cheaper auto pick).
      3. A ``tier`` recorded on this node / context pack via
         ``services.agent.tiers.provider_for_tier``.
      4. Ambient ``config.get_provider()`` default.
    """
    node_inputs = context_pack.get("node_inputs") or {}
    # WS2/WS4 + mega-5: difficulty-aware pick first (even without ai_roles).
    try:
        from services.agent.role_models import (  # noqa: PLC0415
            cost_tier_for,
            min_rank_for_difficulty,
            resolve_for_node,
            resolve_role_model,
        )
        from services.agent import config as agent_config  # noqa: PLC0415
        role = (
            context_pack.get("role_id")
            or node_inputs.get("role")
            or context_pack.get("role")
            or "default"
        )
        # Map DAG/mission roles onto harness ai_roles keys.
        role_s = str(role).strip().lower()
        harness_role = "task"
        if role_s in ("overseer", "orchestrator", "department_manager", "planning"):
            harness_role = "planning"
        elif role_s in ("advisor", "reviewer"):
            harness_role = "advisor"
        elif role_s in ("default", "intake", "intake_analyst"):
            harness_role = "default"

        try:
            depth = int(
                context_pack.get("depth")
                or node_inputs.get("depth")
                or 0
            )
        except (TypeError, ValueError):
            depth = 0
        task_class = (
            context_pack.get("task_class")
            or node_inputs.get("task_class")
            or context_pack.get("taskClass")
        )
        risk = context_pack.get("risk") or node_inputs.get("risk")
        min_rank = min_rank_for_difficulty(
            task_class=str(task_class) if task_class else None,
            node_depth=depth,
            risk=str(risk) if risk else None,
        )
        cfg = dict(agent_config.load_config() or {})
        assignment = resolve_for_node(harness_role, context_pack, cfg=cfg)
        if assignment is None or (
            assignment.mode == "fixed"
            and str(assignment.provider).startswith("xai")
            and min_rank < 5
            and depth > 0
        ):
            try:
                assignment = resolve_role_model(
                    harness_role,
                    cfg,
                    task_class=str(task_class) if task_class else None,
                    node_depth=depth,
                    risk=str(risk) if risk else None,
                    min_rank=min_rank,
                )
            except Exception:  # noqa: BLE001
                if assignment is None:
                    raise
        if assignment is not None:
            cfg_pick = dict(cfg)
            cfg_pick["model"] = assignment.model
            # Cap reasoning effort for non-frontier ranks (quota + cost).
            rank = int(assignment.rank or min_rank or 3)
            if rank <= 2:
                cfg_pick["reasoning_effort"] = "low"
            elif rank <= 3:
                cfg_pick["reasoning_effort"] = "medium"
            # Retries are cheaper: drop one effort band.
            try:
                retry_rem = int(context_pack.get("retry_remaining") or 0)
            except (TypeError, ValueError):
                retry_rem = 0
            # Heuristic: if already retried (retry_remaining small relative to
            # typical budget 2+), force low effort.
            if retry_rem == 1 or context_pack.get("retry_cheap"):
                cfg_pick["reasoning_effort"] = "low"
            if assignment.provider == "ollama":
                # OpenAI-compatible local Ollama endpoint.
                ocfg = dict(cfg_pick.get("ollama") or {})
                ocfg.setdefault("base_url", "http://127.0.0.1:11434/v1")
                cfg_pick["ollama"] = ocfg
                oai = dict(cfg_pick.get("openai") or {})
                oai["base_url"] = ocfg.get("base_url") or "http://127.0.0.1:11434/v1"
                oai.setdefault("api_key", "ollama")
                cfg_pick["openai"] = oai
            try:
                context_pack["role_model"] = {
                    **assignment.to_dict(),
                    "min_rank": min_rank,
                    "cost_tier": cost_tier_for(
                        assignment.provider, assignment.model),
                    "depth": depth,
                    "task_class": task_class or "unknown",
                    "reasoning_effort": cfg_pick.get("reasoning_effort"),
                }
            except Exception:  # noqa: BLE001
                pass
            # mega-6: emit model_assignment for EVERY DAG node resolve, not only
            # delegate spawns — live mega-6 had 0 assignment rows despite auto_pick.
            try:
                from services.agent import agent_events as _ae  # noqa: PLC0415
                node_id = str(
                    context_pack.get("node_id")
                    or node_inputs.get("id")
                    or role_s
                    or "node"
                )
                _k, _p = _ae.model_assignment_event(
                    node_id,
                    depth=depth,
                    task_class=str(task_class) if task_class else "unknown",
                    min_rank=min_rank,
                    picked_model=assignment.model,
                    picked_provider=assignment.provider,
                    cost_tier=cost_tier_for(
                        assignment.provider, assignment.model),
                    mode=assignment.mode,
                    rank=assignment.rank,
                    reason=assignment.reason,
                    role=str(role),
                    actor=str(
                        context_pack.get("node_name")
                        or context_pack.get("role_id")
                        or role
                    ),
                )
                sink = context_pack.get("_event_sink")
                _ae.emit_event(_k, _p, live=sink, journal=None)
                ledger = context_pack.get("ledger")
                if ledger is not None:
                    try:
                        ledger.append(
                            str(context_pack.get("node_name") or role or "agent"),
                            "note",
                            {
                                "event": "model_assignment",
                                **{k: _p.get(k) for k in (
                                    "depth", "task_class", "min_rank",
                                    "picked_model", "picked_provider",
                                    "cost_tier", "mode", "rank", "reason",
                                    "role",
                                )},
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — never block on telemetry
                pass
            prov_name = assignment.provider
            if prov_name == "ollama":
                prov_name = "openai"
                cfg_pick.setdefault(
                    "api_key",
                    (cfg_pick.get("openai") or {}).get("api_key") or "ollama",
                )
            try:
                return agent_config.get_provider(name=prov_name, cfg=cfg_pick)
            except Exception as exc:  # noqa: BLE001 — try degrade chain
                # mega-6: local leaf may need pull / not running — never block.
                # Prefer openrouter free when keyed, else fall through to injected.
                try:
                    from services.agent.providers import token_usage as _tu  # noqa: PLC0415
                    _ = _tu  # silence unused when import works
                except Exception:  # noqa: BLE001
                    pass
                err_s = f"{type(exc).__name__}: {exc}".lower()
                local_fail = (
                    assignment.provider in ("ollama", "llamacpp", "lmstudio", "openai")
                    or "connection" in err_s
                    or "not found" in err_s
                    or "model" in err_s and "load" in err_s
                )
                if local_fail:
                    or_cfg = dict(cfg.get("openrouter") or {})
                    or_key = (
                        or_cfg.get("api_key")
                        or cfg.get("openrouter_api_key")
                        or cfg.get("api_key")
                    )
                    import os as _os  # noqa: PLC0415
                    or_key = or_key or _os.environ.get("OPENROUTER_API_KEY")
                    if or_key:
                        try:
                            cfg_or = dict(cfg)
                            cfg_or["api_key"] = or_key
                            cfg_or["model"] = "mistralai/mistral-small:free"
                            cfg_or["openrouter"] = {**or_cfg, "api_key": or_key}
                            prov = agent_config.get_provider(
                                name="openrouter", cfg=cfg_or)
                            try:
                                context_pack["role_model"] = {
                                    **(context_pack.get("role_model") or {}),
                                    "provider": "openrouter",
                                    "model": cfg_or["model"],
                                    "degraded_from": assignment.provider,
                                    "degrade_reason": str(exc)[:200],
                                }
                            except Exception:  # noqa: BLE001
                                pass
                            return prov
                        except Exception:  # noqa: BLE001
                            pass
                # fall through to injected / tier / ambient
    except Exception:  # noqa: BLE001 — never block node on role_models
        pass

    if provider is not None:
        return provider
    tier = node_inputs.get("tier") or context_pack.get("tier")
    if tier:
        from services.agent.tiers import provider_for_tier  # noqa: PLC0415
        return provider_for_tier(tier)
    from services.agent import config  # noqa: PLC0415 — real import, no wrong name
    return config.get_provider()


def _default_agent_fn(request: str, context_pack: dict[str, Any], *,
                      ledger=None, mission_dispatch=None, provider=None) -> Any:
    """Run a real tool-executing agent turn for this DAG node (T-W30).

    Resolves the provider via ``_resolve_provider`` (an injected provider, else a
    node/graph-recorded tier via ``tiers.provider_for_tier``, else the ambient
    ``config.get_provider`` seam — honours ``AI_CAD_PROVIDER`` / routing.yaml /
    the local llama.cpp default), then runs a bounded tool-calling turn
    (``agent_turn.run_turn``): the model proposes MCP tool calls, they are
    dispatched against the live engine, results are fed back, and a structured
    handoff is returned.  The node's ``allowed_tools`` scope the callable tools.

    mega-8 (advisor Q3): cheap local/tiny leaves skip the multi-turn tool loop
    and take a one-shot structured envelope fill-in. On schema fail → count
    leaf_miss and fall through to the existing frontier degrade / tool-loop path.

    Fail-closed on genuine unavailability (no provider constructible — e.g. no model
    server and no API key): return a NON-success stub so the node ESCALATES rather
    than silently reporting a fabricated success.  A raised exception is surfaced by
    ``run()`` as an escalate verdict."""
    from services.agent import agent_turn  # noqa: PLC0415

    node_name = context_pack.get("node_name", "dag_node")
    # mega-6: make ledger available to _resolve_provider for model_assignment notes.
    if ledger is not None and context_pack.get("ledger") is None:
        try:
            context_pack["ledger"] = ledger
        except Exception:  # noqa: BLE001
            pass
    # mega-8: seed run_id from the bound ledger when the pack is missing it
    # (graph_loader historically assembled packs without state.run_id).
    if ledger is not None and not context_pack.get("run_id"):
        try:
            rid = getattr(ledger, "run_id", None)
            if rid:
                context_pack = dict(context_pack)
                context_pack["run_id"] = rid
        except Exception:  # noqa: BLE001
            pass
    try:
        resolved_provider = _resolve_provider(context_pack, provider)
    except Exception as exc:  # noqa: BLE001 — no constructible provider
        # Do NOT return a confirmed stub: that is the fabrication bug. Signal a
        # non-success so the node escalates and the failure is visible.
        return _UnavailableAgentResult(f"no provider available: {exc}")

    # mega-8: cheap-leaf one-shot envelope (advisor Q3 primary). Local/tiny
    # models on leaf task classes do NOT enter the multi-turn tool loop.
    oneshot = _maybe_run_cheap_leaf_oneshot(
        request, context_pack, resolved_provider,
        ledger=ledger, node_name=node_name,
    )
    if oneshot is not None:
        return oneshot
    # On oneshot miss: fall through to tool-loop on a frontier/mid provider
    # (existing degrade path) rather than re-looping the same tiny model.
    if context_pack.get("leaf_oneshot_miss"):
        upgraded = _frontier_fallback_provider(context_pack, provider)
        if upgraded is not None:
            resolved_provider = upgraded

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
        request=request, provider=resolved_provider, name=node_name,
        allowed_tools=allowed_tools,
        system_prompt=context_pack.get("role_system_prompt"),
        max_tool_calls=max_tools,
        max_tokens=(int(profile.generation_headroom) if profile else None),
        ledger=ledger, actor=node_name,
        mission_dispatch=mission_dispatch, tools_text=tools_text,
        # M20: thread the live event sink + span context so agent_turn emits
        # `agent.tool_call` (target + latency) to BOTH the GUI stream and the
        # per-run events.jsonl. run()/context_pack.assemble() seed these keys;
        # absent → emission is a no-op (back-compat with standalone DAG runs).
        on_event=context_pack.get("_event_sink"),
        run_id=context_pack.get("run_id"),
        node_id=context_pack.get("node_id"))


def _maybe_run_cheap_leaf_oneshot(
    request: str,
    context_pack: dict[str, Any],
    provider: Any,
    *,
    ledger=None,
    node_name: str = "dag_node",
) -> Any | None:
    """One-shot structured envelope for cheap local leaves (mega-8 / advisor Q3).

    Returns a TurnResult-like object on success, or None to fall through to the
    multi-turn tool loop / frontier degrade path. Schema failure increments
    ``leaf_miss`` telemetry.
    """
    try:
        from services.agent.role_models import (  # noqa: PLC0415
            is_cheap_leaf_provider,
            is_leaf_task_class,
            parse_leaf_envelope_text,
            record_leaf_miss,
            validate_leaf_envelope,
        )
    except Exception:  # noqa: BLE001
        return None

    node_inputs = context_pack.get("node_inputs") or {}
    task_class = (
        context_pack.get("task_class")
        or node_inputs.get("task_class")
        or ""
    )
    if not is_leaf_task_class(str(task_class) if task_class else None):
        return None

    # Prefer role_model stamp from _resolve_provider; fall back to provider attrs.
    rm = context_pack.get("role_model") or {}
    prov_name = str(rm.get("provider") or getattr(provider, "name", "") or "")
    model_name = str(rm.get("model") or getattr(provider, "model", "") or "")
    # openai-compat ollama is stamped as openai after resolve — recover.
    if prov_name == "openai" and (
        "11434" in str(getattr(provider, "base_url", "") or "")
        or "ollama" in model_name.lower()
        or (rm.get("degraded_from") in (None, "") and model_name.endswith(":latest"))
    ):
        # Keep cheap if original assignment was ollama.
        if str(rm.get("provider") or "") == "ollama" or "ollama" in model_name.lower():
            prov_name = "ollama"
    if rm.get("provider") == "ollama":
        prov_name = "ollama"
    if not is_cheap_leaf_provider(prov_name, model_name):
        # Also accept cost_tier stamp ≤ 1 from assignment telemetry.
        try:
            ct = int(rm.get("cost_tier")) if rm.get("cost_tier") is not None else None
        except (TypeError, ValueError):
            ct = None
        if ct is None or ct > 1:
            return None

    # Force-disable via env for tests / emergency.
    if os.environ.get("AI_CAD_LEAF_ONESHOT", "1") in ("0", "false", "False"):
        return None

    system = (
        "You are a leaf worker filling a fixed JSON envelope. "
        "Reply with ONE JSON object only (no tools, no prose). Schema:\n"
        "{\n"
        '  "goal": string,           // required: what this leaf achieves\n'
        '  "ops": [object, ...],     // optional: concrete actions/ops\n'
        '  "no_op": boolean,         // true only when nothing to do\n'
        '  "reason": string,         // required when no_op=true\n'
        '  "artifacts": [object, ...] // optional outputs\n'
        "}\n"
        "Rules: either non-empty ops[] OR (no_op=true AND non-empty reason). "
        "Do not invent tool calls. Do not wrap in markdown unless needed."
    )
    if context_pack.get("role_system_prompt"):
        system = (
            str(context_pack["role_system_prompt"])[:1200]
            + "\n\n---\nLEAF ONE-SHOT MODE (mandatory):\n"
            + system
        )
    user_msg = (
        f"Node request:\n{request}\n\n"
        f"task_class={task_class}. Fill the leaf envelope JSON now."
    )
    # Prefer agent_turn list-of-parts form so Anthropic-shaped providers work;
    # OpenAI/ollama also accept plain strings after the mega-8 provider fix.
    messages = [{"role": "user", "content": [{"type": "text", "text": user_msg}]}]
    try:
        reply = provider.complete(system, messages, max_tokens=800)
    except Exception as exc:  # noqa: BLE001 — degrade to tool-loop / frontier
        record_leaf_miss(1)
        if ledger is not None:
            try:
                ledger.append(node_name, "agent_turn", {
                    "event": "leaf_oneshot_miss",
                    "error": str(exc)[:200],
                    "task_class": task_class,
                    "provider": prov_name,
                    "model": model_name,
                })
            except Exception:  # noqa: BLE001
                pass
        # Signal caller to fall through; also stash so degrade path can fire.
        context_pack["leaf_oneshot_miss"] = True
        context_pack["leaf_oneshot_error"] = str(exc)[:200]
        return None

    env, errs = parse_leaf_envelope_text(str(reply or ""))
    if env is None or errs:
        # Double-check if reply was already a dict-like via validate.
        if env is None:
            record_leaf_miss(1)
            if ledger is not None:
                try:
                    ledger.append(node_name, "agent_turn", {
                        "event": "leaf_oneshot_miss",
                        "errors": list(errs or [])[:8],
                        "task_class": task_class,
                        "provider": prov_name,
                        "model": model_name,
                        "reply_head": str(reply or "")[:240],
                    })
                except Exception:  # noqa: BLE001
                    pass
            context_pack["leaf_oneshot_miss"] = True
            context_pack["leaf_oneshot_errors"] = list(errs or [])[:8]
            return None

    # Valid envelope → confirmed leaf handoff (no multi-turn tools).
    handoff = {
        "status": "done" if not env.get("no_op") else "done",
        "summary": (
            env.get("goal")
            or ("no_op: " + str(env.get("reason") or ""))
        )[:600],
        "outputs": {
            "leaf_envelope": env,
            "one_shot": True,
            "ops": env.get("ops") or [],
            "no_op": bool(env.get("no_op")),
            "reason": env.get("reason"),
            "artifacts": env.get("artifacts") or [],
            "reasoning_summary": (
                "Cheap-leaf one-shot envelope (mega-8); no multi-turn tool loop."
            ),
        },
    }
    if ledger is not None:
        try:
            ledger.append(node_name, "agent_turn", {
                "event": "leaf_oneshot_ok",
                "task_class": task_class,
                "provider": prov_name,
                "model": model_name,
                "no_op": bool(env.get("no_op")),
                "n_ops": len(env.get("ops") or []),
            })
        except Exception:  # noqa: BLE001
            pass
    return _LeafOneshotResult(handoff, env)



def _frontier_fallback_provider(context_pack: dict[str, Any], injected=None):
    """After a cheap-leaf oneshot miss, pick a non-local provider for tool-loop.

    Order: openrouter free (if keyed) → injected mission provider (if non-cheap)
    → None (keep current; ambient tool-loop still runs).
    """
    try:
        from services.agent import config as agent_config  # noqa: PLC0415
        from services.agent.role_models import is_cheap_leaf_provider  # noqa: PLC0415
        import os as _os  # noqa: PLC0415
        cfg = dict(agent_config.load_config() or {})
        or_cfg = dict(cfg.get("openrouter") or {})
        or_key = (
            or_cfg.get("api_key")
            or cfg.get("openrouter_api_key")
            or _os.environ.get("OPENROUTER_API_KEY")
        )
        if or_key:
            cfg_or = dict(cfg)
            cfg_or["api_key"] = or_key
            cfg_or["model"] = "mistralai/mistral-small:free"
            cfg_or["openrouter"] = {**or_cfg, "api_key": or_key}
            prov = agent_config.get_provider(name="openrouter", cfg=cfg_or)
            try:
                context_pack["role_model"] = {
                    **(context_pack.get("role_model") or {}),
                    "provider": "openrouter",
                    "model": cfg_or["model"],
                    "degraded_from": (context_pack.get("role_model") or {}).get(
                        "provider"),
                    "degrade_reason": "leaf_oneshot_miss",
                }
            except Exception:  # noqa: BLE001
                pass
            return prov
        if injected is not None:
            p_name = str(getattr(injected, "name", "") or "")
            p_model = str(getattr(injected, "model", "") or "")
            if not is_cheap_leaf_provider(p_name, p_model):
                return injected
    except Exception:  # noqa: BLE001
        return None
    return None

class _LeafOneshotResult:
    """TurnResult duck-type for a successful cheap-leaf one-shot."""

    def __init__(self, handoff: dict[str, Any], envelope: dict[str, Any]) -> None:
        self.success = True
        self.stop_reason = "confirmed"
        self.final_code = None
        self.result = {
            "handoff": handoff,
            "leaf_envelope": envelope,
            "one_shot": True,
            "calls": [],
            "tool_calls": 0,
        }


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
        # WI-16: append per-capability SOFT phrasing (A mandatory / B suggestive /
        # C minimal HARD-floor mirror). Letter from active tier; default C.
        try:
            from services.agent.prompt_tiers import (  # noqa: PLC0415
                assemble_system_for_capability,
                letter_from_capability,
            )
            from services.agent.capability import resolve_capability  # noqa: PLC0415
            _prof = _tier_profile()
            _tier_name = _prof.name if _prof is not None else ""
            _cap = resolve_capability(_tier_name) if _tier_name else None
            _letter = letter_from_capability(_cap)
            context_pack["capability_letter"] = _letter
            context_pack["role_system_prompt"] = assemble_system_for_capability(
                role_card.system_prompt, _letter)
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

    # M20: seed the agent-execution event sink + span context into context_pack so
    # the agent turn can emit `agent.tool_call` (etc.) through the SAME §11 `emit`
    # callback the DAG lifecycle events use. run_id may already be on the pack
    # (mission path); node_id/node_name come from the node here. Best-effort — a
    # None emit leaves the keys harmless (agent_turn no-ops emission).
    if emit is not None:
        context_pack = dict(context_pack)
        context_pack.setdefault("_event_sink", emit)
        context_pack.setdefault("node_id", node_id)
        context_pack.setdefault("node_name", node_name)

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
    # mega-5: a single retryable provider read-timeout must not exhaust the
    # node — honor agent_result.result["retryable"] when stop_reason=failed.
    if (
        verdict == VERDICT_ESCALATE
        and not success
        and stop_reason == "failed"
    ):
        agent_result_dict_probe = getattr(agent_result, "result", None) or {}
        if isinstance(agent_result_dict_probe, dict) and agent_result_dict_probe.get("retryable") is True:
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
            node, context_pack, agent_result,
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

    # mega-8 F9: multi-domain PLAN/BUILD must see the typed-refusal escape
    # hatch in the request (gate kills free-form auto-complete otherwise).
    try:
        from services.agent.role_models import (  # noqa: PLC0415
            domain_tags_from_context,
            is_multi_domain_plan_or_build,
        )
        role_id = (
            context_pack.get("role_id")
            or node_inputs.get("role")
            or ""
        )
        tags = domain_tags_from_context(context_pack, node_inputs)
        if is_multi_domain_plan_or_build(
            role_id=str(role_id) if role_id else None,
            node_type=str(node_type or ""),
            domain_tags=tags,
            task_class=str(
                context_pack.get("task_class")
                or node_inputs.get("task_class")
                or ""
            ) or None,
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
        "stop_reason", "one_shot", "leaf_envelope", "delegate_gate",
        "typed_refusal", "success", "auto_complete",
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
                "typed_refusal", "leaf_envelope", "one_shot",
                "auto_complete", "productive_tools", "wrote_ledger",
                "no_op", "reason", "doc_id", "checks_run",
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
                    if isinstance(out.get("handoff"), dict) else None,
                    "summary": str(
                        (out.get("handoff") or {}).get("summary")
                        if isinstance(out.get("handoff"), dict) else ""
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
        context_pack.get("role_id")
        or node.get("role")
        or node_inputs.get("role")
        or ""
    )
    task_class = (
        context_pack.get("task_class")
        or node_inputs.get("task_class")
    )
    tags = domain_tags_from_context(context_pack, node_inputs)
    # If the pack already stamped domain_tags, prefer those.
    if context_pack.get("domain_tags"):
        try:
            tags = [str(t).strip().lower() for t in context_pack["domain_tags"] if str(t).strip()]
        except Exception:  # noqa: BLE001
            pass

    applies = is_multi_domain_plan_or_build(
        role_id=str(role_id) if role_id else None,
        node_type=str(node.get("type") or ""),
        domain_tags=tags,
        task_class=str(task_class) if task_class else None,
    )
    info.update({
        "gated": applies,
        "role_id": role_id,
        "task_class": task_class,
        "domain_tags": tags,
        "n_domains": len(tags),
    })
    if not applies:
        return False, info

    # Depth check: only enforce when depth < max.
    try:
        depth = int(
            context_pack.get("depth")
            or node_inputs.get("depth")
            or 0
        )
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
        "orchestrator", "department_manager", "layout_engineer", "planning",
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
