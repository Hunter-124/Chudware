"""agent_turn.py — bounded tool-executing agent turn for DAG role nodes (T-W30).

The historical DAG "live" agent path was a stub: ``node_runner._default_agent_fn``
imported a non-existent module and silently degraded to ``_StubAgentResult``.  Even
after fixing the import, the only agent surface was ``loop.run`` — the single-part
*code-extraction* loop (``run_code_multi``), which builds a part from a model-authored
code block but never dispatches MCP tool calls.  A role node (spec_writer, reviewer,
sourcing, …) needs to CALL TOOLS, not emit a code block.

This module provides that missing surface: a minimal, provider-agnostic tool-calling
turn over the ``Provider.complete(system, messages) -> str`` seam.  The model emits a
tool call as a fenced JSON object; we parse it, dispatch it against the live MCP tool
registry (the SAME ``services.verify.battery._get_fn`` singleton the verify node uses),
feed the result back, and repeat until the model finishes or the tool budget is spent.

It is deliberately small and honest:
  * bounded by ``max_tool_calls`` (tier budget) — never an unbounded loop;
  * every turn is recorded (one event) when a ledger is supplied — inspectable;
  * a model that emits no valid tool call and no completion is ``no_code`` (escalate),
    not a fabricated success;
  * tool dispatch errors are fed back to the model (it may recover) but a turn that
    exhausts its budget without a clean finish is ``max_iters`` (retry), never
    ``confirmed``.

Return type mirrors the ``AgentResult`` duck-type node_runner expects:
    .success: bool  .stop_reason: str  .final_code: None  .result: dict
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

# stop_reason values node_runner._map_verdict understands.
_DONE = "confirmed"
_RETRY = "max_iters"
_NOCODE = "no_code"
_FAILED = "failed"
_PROVIDER_AUTH = "provider_auth"  # non-retryable auth/bad-request (mega-1 TOP-4)

# T-W32: match the fenced BODY (not a brace pair) — the old r"\{.*?\}" non-greedy
# pattern truncated at the FIRST closing brace, so any tool call with nested args
# (e.g. {"tool": "x", "args": {"a": {...}}}) failed to parse and the model's valid
# directive was dropped as free text (observed live in the r4 PLAN node).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def classify_provider_error(exc: BaseException) -> tuple[str, bool]:
    """Classify a provider exception → ``(stop_reason, retryable)``.

    Auth + permanent bad-request (400/401/403, invalid_api_key, Incorrect API key)
    are NON-RETRYABLE → ``provider_auth`` so the mission aborts instead of burning
    escalate/budget as ``failed``/``budget exhausted`` (mega-1 TOP-4).

    Rate limits (429) and 5xx remain retryable (map to ``failed`` so the node can
    still escalate, but callers that inspect ``retryable`` may back off).
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    # Prefer structured status codes when the SDK attaches them.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None

    auth_markers = (
        "incorrect api key",
        "invalid api key",
        "invalid_api_key",
        "authentication",
        "unauthorized",
        "auth_missing",
        "permission denied",
        "forbidden",
        "invalid-argument",  # xAI 400 envelope for bad key
        "api key provided",
    )
    if status_i in (400, 401, 403) or any(m in text for m in auth_markers):
        # 400 alone is ambiguous (bad request vs auth). Treat as non-retryable
        # when auth-ish markers present OR status is 401/403; bare 400 with
        # invalid-argument/key wording also non-retryable.
        if status_i in (401, 403):
            return _PROVIDER_AUTH, False
        if status_i == 400 or any(m in text for m in auth_markers):
            return _PROVIDER_AUTH, False
    if status_i == 429 or "rate limit" in text or "too many requests" in text:
        return _FAILED, True
    if status_i is not None and status_i >= 500:
        return _FAILED, True
    # Default: treat as failed (node escalates); not marked non-retryable auth.
    return _FAILED, True


class TurnResult:
    """AgentResult duck-type returned by :func:`run_turn`."""
    def __init__(self, success: bool, stop_reason: str, result: dict) -> None:
        self.success = success
        self.stop_reason = stop_reason
        self.final_code = None
        self.result = result


_SYSTEM_BASE = (
    "You are an AI-CAD role agent executing one node of a mission. You may call "
    "tools to inspect and modify the engine. To call a tool, reply with a single "
    "fenced json block:\n"
    "```json\n{\"tool\": \"<tool_name>\", \"args\": {<kwargs>}}\n```\n"
    "After you see the tool result, either call another tool or, when the node's "
    "goal is met, finish with ONE of these fenced JSON forms (both accepted):\n"
    "```json\n{\"done\": true, \"handoff\": {<structured summary>}}\n```\n"
    "or the role handoff envelope (preferred when your role card defines one):\n"
    "```json\n{\"status\": \"done\", \"summary\": \"...\", \"outputs\": {…}}\n```\n"
    "status may be done|blocked|escalated. Call only tools from the allowed list. "
    "Do not invent results — run the tool."
)


def run_turn(request: str, provider: Any, *, name: str = "dag_node",
             allowed_tools: list[str] | None = None,
             system_prompt: str | None = None,
             max_tool_calls: int | None = None,
             max_tokens: int | None = None,
             ledger=None,
             actor: str | None = None,
             tool_dispatch: Callable[[str, dict], Any] | None = None,
             mission_dispatch: Any | None = None,
             tools_text: str | None = None,
             on_event: Callable[..., None] | None = None,
             run_id: str | None = None,
             node_id: str | None = None,
             parent_id: str | None = None,
             subagent_id: str | None = None,
             capability: Any | None = None,
             tier: str | None = None,
             model_id: str | None = None,
             phase: str | None = None,
             task_class: str | None = None,
             msg_idx: int | None = None,
             doc_id: str | None = None) -> TurnResult:
    """Run a bounded tool-executing turn and return a :class:`TurnResult`.

    *tool_dispatch* is injectable ``(tool_name, args) -> result``; the default
    dispatches against the live MCP registry via :func:`_dispatch_tool`.  Tests
    inject a deterministic dispatcher (no engine).

    *mission_dispatch* (T-W32) is the run-scoped dispatch layer
    (``services.agent.mission_dispatch.MissionDispatch``): consulted BEFORE the
    global registry so mission-context tools (ledger_get/ledger_note, dag_plan,
    ask_user, delegate, part_search) resolve against THIS run.  Ignored when a
    custom *tool_dispatch* is injected (the injector owns resolution).

    *tools_text* (T-W32) is the rendered §9.1 tools slot (trimmed schemas for the
    node's allowed tools — see ``services.agent.tool_schemas``).  When present it
    replaces the bare "Allowed tools:" name list, so the model sees real kwargs
    instead of guessing them.

    *max_tokens* caps each model completion.  Callers on a known tier pass the
    tier's ``generation_headroom`` (tiers.yaml — the same budget source the
    aicad.effort/1 resolution path reads), so the turn is bounded by tier
    budgets rather than an ad-hoc constant.  Default 2048 when no tier is known.

    *capability* / *tier* / *model_id* resolve a CapabilityProfile for A/B
    scaffolds (WI-2/4/6/7). Letter C is always a no-op for scaffolds.
    *phase* applies HARD phase_caps (WI-4) when set.
    *task_class* feeds WI-8 reference-build novelty (when injected upstream)."""
    # Capability profile: prefer explicit, else mission_dispatch, else C.
    try:
        from services.agent import scaffolds as _sc  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        _sc = None  # type: ignore
    _cap = capability
    if _cap is None and mission_dispatch is not None:
        _cap = getattr(mission_dispatch, "capability", None)
    if _cap is None and _sc is not None:
        _cap = _sc.coerce_profile(
            tier=tier or getattr(mission_dispatch, "tier", None)
            if mission_dispatch is not None else tier,
            model_id=model_id,
        )
    _phase = phase or (
        getattr(mission_dispatch, "phase", None) if mission_dispatch is not None else None
    )
    _budget_in = max_tool_calls if max_tool_calls is not None else 16
    _capped = False
    if _sc is not None and _cap is not None:
        _budget_in, _capped = _sc.apply_phase_cap(
            _cap, _phase, int(_budget_in) if max_tool_calls is not None else None,
            ledger=ledger, actor=actor or name,
        )
        if _budget_in is None:
            _budget_in = 16
    budget = int(_budget_in)
    completion_cap = int(max_tokens) if max_tokens else 2048
    if tool_dispatch is not None:
        dispatch = tool_dispatch
    elif mission_dispatch is not None:
        def dispatch(tool: str, args: dict) -> Any:
            fn = mission_dispatch.resolve(tool)
            if fn is not None:
                return fn(**(args or {}))
            return _dispatch_tool(tool, args)
    else:
        dispatch = _dispatch_tool
    allow = set(allowed_tools or [])
    actor = actor or name
    # WI-8: inject reference builds into the user request for A/B specify.
    _req = request
    if _sc is not None and _cap is not None:
        try:
            _ref = _sc.reference_build_context(
                _cap, request,
                task_class=task_class or getattr(mission_dispatch, "task_class", None)
                if mission_dispatch is not None else task_class,
                phase=_phase or "specify",
                ledger=ledger, actor=actor,
            )
            if _ref:
                _req = request + "\n\n" + _ref
        except Exception:  # noqa: BLE001
            pass

    # M20: agent-execution event stream. Emit `agent.tool_call` (with human `target`
    # + latency) with a per-turn root span so the GUI can build the execution DAG
    # tree. The DAG path threads an *already journal-wrapped* `on_event` (the executor
    # does `EventJournal(run_id).wrap(emit)` in T-F17), so streaming through it also
    # persists to events.jsonl — we must NOT double-write with a second journal here.
    # A standalone journal is created ONLY when a run_id is given WITHOUT a live sink
    # (a headless caller that wants replay but no GUI). Best-effort: never blocks.
    _ev_journal = None
    if run_id and on_event is None:
        try:
            from services.dag.event_journal import EventJournal  # noqa: PLC0415
            _ev_journal = EventJournal(run_id)
        except Exception:  # noqa: BLE001
            _ev_journal = None
    _ev_node = node_id or name
    _turn_span = None
    if on_event is not None or _ev_journal is not None:
        from services.agent import agent_events as _ae  # noqa: PLC0415
        _turn_span = _ae.new_span_id()

    system = (system_prompt or "") + "\n\n" + _SYSTEM_BASE
    if tools_text:
        system += "\n\n" + tools_text
    elif allow:
        system += "\nAllowed tools: " + ", ".join(sorted(allow))

    messages: list[dict] = [{"role": "user", "content": _parts(_req)}]

    # Skills catalog + activation (contracts/skills.md). Best-effort.
    try:
        from services.agent import skills as _skills  # noqa: PLC0415
        _sid = subagent_id or run_id or name or "_turn"
        _clean, _hits = _skills.resolve_slash(_req)
        if _hits and _clean.strip():
            _req = _clean
            messages = [{"role": "user", "content": _parts(_req)}]
        _sk = _skills.session_system_block(_sid, user_text=request, phase=_phase)
        if _sk:
            system = system.rstrip() + "\n\n" + _sk
    except Exception:  # noqa: BLE001
        pass

    calls: list[dict] = []
    tool_calls_made = 0
    last_reason = _NOCODE
    _prev_stage: str | None = None

    # AI-VC v2: checkpoint BEFORE the user prompt turn (kind=prompt).
    # msg_idx is the GUI chat message ordinal; optional, default None.
    try:
        from services.plm import ai_vc as _ai_vc  # noqa: PLC0415
        _ai_vc.set_session_context(
            session_id=run_id,
            turn_id=msg_idx if msg_idx is not None else 0,
            msg_idx=msg_idx,
            doc_id=doc_id,
        )
        _ai_vc.on_prompt_start(
            request,
            doc_id=doc_id,
            msg_idx=msg_idx,
            turn_id=msg_idx if msg_idx is not None else 0,
            session_id=run_id,
            ledger=ledger,
            actor=actor or "agent_turn",
        )
    except Exception:  # noqa: BLE001 — AI-VC never breaks the turn
        pass
    for _ in range(budget + 1):
        # Phase 4 (M11/M16/M17): the sub-agent turn boundary.  When this turn IS a
        # sub-agent session (subagent_id set), drain the cross-process steering queue
        # BEFORE each model completion — a user steer queued from the GUI lands as a
        # user-role message at the very next model call — and honor a pending
        # terminate flag by stopping cleanly (never a hard kill).  Best-effort: a
        # registry failure never breaks the turn.
        if subagent_id:
            try:
                from services.agent import subagent_registry as _sreg  # noqa: PLC0415
                if _sreg.terminate_requested(subagent_id):
                    _sreg.set_status(subagent_id, "terminated",
                                     note="terminated by user at turn boundary")
                    _sreg.append_transcript(
                        subagent_id, "system",
                        "Sub-agent stopped: user termination honored at turn boundary.")
                    _log(ledger, actor, {"event": "subagent_terminated",
                                         "subagent_id": subagent_id})
                    return TurnResult(False, "terminated_by_user",
                                      {"calls": calls, "tool_calls": tool_calls_made})
                for _steer in _sreg.consume_steers(subagent_id):
                    messages.append({"role": "user", "content": _parts(
                        "[Human steering] " + str(_steer.get("text", "")))})
                    _sreg.append_transcript(
                        subagent_id, "system", "steering applied at turn boundary",
                        steer_id=_steer.get("steer_id", ""))
                    _log(ledger, actor, {"event": "subagent_steer_applied",
                                         "subagent_id": subagent_id,
                                         "steer_id": _steer.get("steer_id", "")})
            except Exception:  # noqa: BLE001
                pass
        try:
            reply = provider.complete(system, messages, max_tokens=completion_cap)
        except Exception as exc:  # noqa: BLE001 — provider/network error → escalate
            stop, retryable = classify_provider_error(exc)
            _log(ledger, actor, {
                "event": "provider_error",
                "error": str(exc),
                "stop_reason": stop,
                "retryable": retryable,
            })
            if stop == _PROVIDER_AUTH:
                _log(ledger, actor, {
                    "event": "provider_fatal",
                    "error": str(exc),
                    "stop_reason": stop,
                })
            return TurnResult(False, stop,
                              {"error": f"provider error: {exc}",
                               "calls": calls,
                               "retryable": retryable,
                               "stop_reason": stop})

        messages.append({"role": "assistant", "content": _parts(reply)})
        # Phase 4: sub-agent transcripts capture every model reply so the GUI viewer
        # shows the real conversation (never fabricated).
        if subagent_id:
            try:
                from services.agent import subagent_registry as _sreg  # noqa: PLC0415
                _sreg.append_transcript(subagent_id, "subagent", str(reply)[:4000])
            except Exception:  # noqa: BLE001
                pass
        parsed = _parse_directive(reply)

        if parsed is None:
            # No structured directive. If the model already ran tools, treat a bare
            # natural-language finish as done-with-handoff; otherwise it produced
            # nothing actionable (no_code).
            _log(ledger, actor, {"event": "turn", "kind": "text", "text": reply[:400]})
            if tool_calls_made > 0:
                bare_handoff = {
                    "status": "done",
                    "summary": (reply or "")[:600],
                    "outputs": {
                        "text": reply[:2000],
                        "tool_calls": tool_calls_made,
                        "reasoning_summary": (
                            "Bare natural-language finish after tool calls; "
                            "accepted as role handoff (plan/build/specify class)."
                        ),
                    },
                }
                block = _done_guardrail_block(bare_handoff, ledger, actor, calls)
                if block is not None:
                    messages.append({"role": "user", "content": _parts(block)})
                    continue
                return TurnResult(True, _DONE,
                                  {"handoff": bare_handoff, "calls": calls})
            last_reason = _NOCODE
            break

        # Finish forms accepted:
        #   1. {"done": true, "handoff": {...}}  — agent_turn envelope
        #   2. {"status": "done"|"blocked"|"escalated", "summary", "outputs"}
        #      — role-card handoff_schema (spec_writer/overseer/…). Observed
        #      live on grok-4.5: the role prompt + GBNF/schema training teach
        #      form (2); only form (1) was previously treated as done, so a
        #      perfect role handoff fell through to no_code and killed SPECIFY.
        role_status = parsed.get("status")
        is_role_handoff = (
            role_status in ("done", "blocked", "escalated")
            and "tool" not in parsed
            and not parsed.get("done")
        )
        if parsed.get("done") or is_role_handoff:
            if parsed.get("done"):
                handoff = parsed.get("handoff") or {}
            else:
                # Role-schema envelope IS the handoff; wrap for downstream.
                handoff = dict(parsed)
                # blocked/escalated still end the turn (node_runner maps success
                # + confirmed → done). Surface non-done status in the handoff so
                # the ledger/overseer can see it; the node still completes so
                # the phase machine can advance past a honest blocked report
                # rather than looping as specify_incomplete forever.
            block = _done_guardrail_block(handoff, ledger, actor, calls)
            if block is not None:
                messages.append({"role": "user", "content": _parts(block)})
                continue
            _log(ledger, actor, {"event": "turn", "kind": "done", "handoff": handoff})
            return TurnResult(True, _DONE, {"handoff": handoff, "calls": calls})

        tool = parsed.get("tool")
        args = parsed.get("args") or {}
        if not tool:
            last_reason = _NOCODE
            break

        if allow and tool not in allow:
            note = {"tool": tool, "error": f"tool {tool!r} not in allowed list"}
            calls.append(note)
            _log(ledger, actor, {"event": "tool_denied", **note})
            messages.append({"role": "user", "content": _parts(
                f"Tool {tool!r} is not permitted. Choose an allowed tool.")})
            continue

        if tool_calls_made >= budget:
            last_reason = (
                "phase_step_cap" if _capped else _RETRY
            )
            break

        tool_calls_made += 1
        import time as _time  # noqa: PLC0415
        _t0 = _time.monotonic()
        # Scaffold pre-dispatch (WI-7 calc gate, WI-6 A aggressive checkpoint).
        # HARD floors (WI-12/15) run next via _pre_dispatch_guardrail.
        _blocked = None
        if _sc is not None and _cap is not None:
            try:
                _blocked = _sc.pre_dispatch_scaffolds(
                    tool, args, profile=_cap, ledger=ledger,
                    calls=calls, actor=actor,
                )
            except Exception:  # noqa: BLE001
                _blocked = None
        if _blocked is None:
            _blocked = _pre_dispatch_guardrail(tool, args, ledger, actor)
        # AI-VC v2: checkpoint BEFORE mutating tool call (kind=tool).
        # call.turn = msg_idx when provided, else internal tool-call ordinal.
        if _blocked is None:
            try:
                from services.plm import ai_vc as _ai_vc  # noqa: PLC0415
                _call_turn = msg_idx if msg_idx is not None else tool_calls_made
                _ai_vc.set_session_context(
                    session_id=run_id,
                    turn_id=_call_turn,
                    msg_idx=msg_idx,
                    doc_id=(args or {}).get("doc_id") or doc_id,
                )
                _ai_vc.on_before_tool(
                    tool, args,
                    turn_id=_call_turn,
                    call_i=tool_calls_made - 1,
                    msg_idx=msg_idx,
                    session_id=run_id,
                    ledger=ledger,
                    actor=actor or "agent_turn",
                )
            except Exception:  # noqa: BLE001 — AI-VC never breaks the turn
                pass
        if _blocked is not None:
            result = {"ok": False, "error": _blocked}
            ok = False
        else:
            try:
                result = dispatch(tool, args)
                ok = True
            except Exception as exc:  # noqa: BLE001 — dispatch error fed back to model
                result = {"ok": False, "error": str(exc)}
                ok = False
        _latency_ms = (_time.monotonic() - _t0) * 1000.0
        call_rec = {"tool": tool, "args": args, "ok": ok,
                    "result": _truncate(result)}
        calls.append(call_rec)
        _log(ledger, actor, {"event": "tool_call", **call_rec})
        # M20: stream the tool call to the live GUI + persist to events.jsonl.
        if _turn_span is not None:
            try:
                from services.agent import agent_events as _ae  # noqa: PLC0415
                _kind, _payload = _ae.tool_call_event(
                    _ev_node, tool, args, result, ok=ok, latency_ms=_latency_ms,
                    parent_id=_turn_span, actor=actor)
                _ae.emit_event(_kind, _payload, live=on_event, journal=_ev_journal)
            except Exception:  # noqa: BLE001 — event emission never blocks the turn
                pass
        messages.append({"role": "user", "content": _parts(
            f"Result of {tool}:\n{_as_text(result)}")})

        # AI-VC: post-success path retains create tools (doc_id only after call)
        # and stamps provenance. Pre-tool checkpoint (above) is the v2 primary.
        if ok:
            try:
                from services.plm import ai_vc as _ai_vc  # noqa: PLC0415
                _call_turn = msg_idx if msg_idx is not None else tool_calls_made
                _ai_vc.set_session_context(
                    session_id=run_id,
                    turn_id=_call_turn,
                    msg_idx=msg_idx,
                )
                _ai_vc.on_ai_tool_success(
                    tool, args, result,
                    turn_id=_call_turn,
                    session_id=run_id,
                    msg_idx=msg_idx,
                    call_i=tool_calls_made - 1,
                    ledger=ledger,
                    actor=actor or "agent_turn",
                )
            except Exception:  # noqa: BLE001 — AI-VC never breaks the turn
                pass

        # mega-2 / C1 AC2–AC4 grading path: when the model runs fit_calc /
        # fit_report / bolt_circle / verify_battery successfully, mirror the
        # evidence the gym graders read (fit_bore.clearance, bolt_circle.holes,
        # battery artifact). Models often compute correctly but forget the
        # ledger_note decision shape the grader requires.
        if ok and ledger is not None:
            try:
                _mirror_c1_grading_evidence(
                    tool, args, result, ledger, actor or "agent_turn")
            except Exception:  # noqa: BLE001 — grading mirror never blocks
                pass

        # WI-2: auto-verify inject after successful mutating dispatch (A/B only).
        if ok and _sc is not None and _cap is not None:
            try:
                _do_inject, _vtool, _prev_stage = _sc.should_auto_verify(
                    _cap, tool, args, ok=ok, prev_stage=_prev_stage,
                )
                if _do_inject and _vtool:
                    _vres = _sc.run_auto_verify(
                        dispatch, _vtool, args,
                        ledger=ledger, actor=actor, profile=_cap,
                    )
                    messages.append({"role": "user", "content": _parts(
                        f"[auto-verify] Result of {_vtool}:\n{_as_text(_vres)}")})
            except Exception:  # noqa: BLE001 — inject never breaks the turn
                pass

    # No clean finish. Historically this returned max_iters / no_code →
    # escalate (DEFAULT_RETRY_BUDGET=1), which killed every SPECIFY node that
    # researched thoroughly but never emitted a formal done envelope —
    # observed on grok-4.5 and locals alike (tool syntax perfect, 7–8 calls,
    # zero handoff). Two failure modes:
    #   * budget exhaust (max_iters / phase_step_cap)
    #   * post-write junk JSON / prose that is neither tool nor done (no_code)
    # Strong signal: a successful WRITE / BUILD tool means the node did its job;
    # synthesise a done handoff so the phase can exit. Research-only tools
    # (fit_calc, research_*) alone do NOT auto-complete — that must still
    # return max_iters so pure thrash is visible.
    # mega-2: also accept buildgraph_plan / new_part / fw_new_project so
    # PLAN/BUILD handoffs of the same class complete (plan_incomplete class).
    _PRODUCTIVE_TOOLS = frozenset({
        "ledger_note", "buildgraph_plan", "dag_plan", "add_features",
        "new_part", "ecad_new_schematic", "fw_new_project", "fw_build",
        "verify_battery",
    })
    wrote = any(
        c.get("ok") and c.get("tool") in _PRODUCTIVE_TOOLS for c in calls
    )
    if (tool_calls_made > 0
            and last_reason in (_RETRY, "phase_step_cap", _NOCODE)
            and wrote):
        synth = {
            "status": "done",
            "summary": (
                f"auto-completed after {tool_calls_made} tool calls "
                f"(budget={budget}, reason={last_reason}); model did not "
                f"emit a formal handoff envelope"
            ),
            "outputs": {
                "auto_complete": True,
                "tool_calls": tool_calls_made,
                "wrote_ledger": any(
                    c.get("ok") and c.get("tool") == "ledger_note" for c in calls
                ),
                "productive_tools": sorted({
                    c.get("tool") for c in calls
                    if c.get("ok") and c.get("tool") in _PRODUCTIVE_TOOLS
                }),
                "reasoning_summary": (
                    "Turn ended without a formal handoff after productive tool "
                    "work; synthesised a done handoff so the phase can exit."
                ),
            },
        }
        _log(ledger, actor, {"event": "turn", "kind": "done",
                             "handoff": synth, "auto": True})
        return TurnResult(True, _DONE,
                          {"handoff": synth, "calls": calls,
                           "tool_calls": tool_calls_made,
                           "auto_complete": True})
    if tool_calls_made > 0 and last_reason in (_RETRY, "phase_step_cap"):
        return TurnResult(True, last_reason, {"calls": calls,
                                              "tool_calls": tool_calls_made})
    return TurnResult(False, last_reason, {"calls": calls,
                                           "tool_calls": tool_calls_made})


# ---------------------------------------------------------------------------
# Parsing + dispatch
# ---------------------------------------------------------------------------

def _parse_directive(reply: str) -> dict | None:
    """Extract the first JSON directive object from *reply*; None if absent/invalid.

    Accepts, in order: fenced ```json blocks (nested braces fine — the whole fence
    body is parsed), a bare JSON object reply, and an UNTERMINATED fence (a local
    model hitting its completion cap mid-fence still yields its complete JSON
    object). ``raw_decode`` parses the first complete object and tolerates
    trailing prose — never a truncated-brace parse."""
    if not reply:
        return None
    candidates: list[str] = [m.group(1) for m in _FENCE_RE.finditer(reply)]
    stripped = reply.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    else:
        # Unterminated fence: parse from the first '{' after the opening fence.
        fence = stripped.find("```")
        if fence != -1:
            brace = stripped.find("{", fence)
            if brace != -1:
                candidates.append(stripped[brace:])
    for cand in candidates:
        obj = _loads_first_object(cand)
        if obj is not None:
            return obj
    return None


def _loads_first_object(text: str) -> dict | None:
    """Parse the first complete JSON object at the start of *text* (trailing
    prose/junk tolerated); None when it isn't a dict or doesn't parse."""
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def resolver_for(mission_dispatch: Any | None = None) -> Callable[[str], Any]:
    """Return a ``(tool_name) -> callable | None`` resolver mirroring run_turn's
    dispatch order: mission-scoped layer first, then the global registry (T-W32).

    The tool-schema renderer (``tool_schemas.render_tools_block``) uses this SAME
    resolver, so the schemas injected into the prompt are exactly the callables the
    dispatcher will invoke — never a schema the dispatch layer would reject."""
    def _resolve(tool: str):
        if mission_dispatch is not None:
            fn = mission_dispatch.resolve(tool)
            if fn is not None:
                return fn
        from services.agent.reviewer_exec import _resolve_fn
        return _resolve_fn(tool)
    return _resolve


def _dispatch_tool(tool: str, args: dict) -> Any:
    """Dispatch a tool call against the live MCP registry (battery singleton).

    Uses the reviewer_exec resolver (which extends battery._get_fn to the intake /
    verify modules), so ANY §16-ish tool resolves through ONE process-wide registry.
    Raises on unresolved tool or dispatch failure — the caller feeds the error back
    to the model (never a fabricated success)."""
    from services.agent.reviewer_exec import _resolve_fn
    fn = _resolve_fn(tool)
    if fn is None:
        raise KeyError(f"tool {tool!r} could not be resolved")
    return fn(**(args or {}))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _parts(text: str) -> list[dict]:
    return [{"type": "text", "text": str(text)}]


def _as_text(result: Any) -> str:
    if isinstance(result, str):
        return result[:2000]
    try:
        return json.dumps(result, default=str)[:2000]
    except Exception:  # noqa: BLE001
        return str(result)[:2000]


def _truncate(result: Any, limit: int = 1000) -> Any:
    s = _as_text(result)
    return s if len(s) <= limit else s[:limit] + "…"



def _done_guardrail_block(handoff: dict, ledger, actor: str,
                          calls: list) -> str | None:
    """WI-11/13/14 done intercept. Return model-visible block message or None."""
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — ImportError-safe: treat as profile C no-op
        return None
    try:
        verdict = _gr.check_done(
            handoff if isinstance(handoff, dict) else {},
            ledger=ledger, calls=calls, actor=actor or "agent_turn",
        )
        if verdict.ok:
            return None
        return verdict.message or verdict.reason or _gr.MSG_VERIFY_BATTERY
    except Exception:  # noqa: BLE001 — never break the turn on guardrail bugs
        return None


def _pre_dispatch_guardrail(tool: str, args: dict, ledger, actor: str) -> str | None:
    """WI-12/15 pre-dispatch. Return error string to feed the model, or None."""
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        verdict = _gr.pre_dispatch(
            tool, args or {}, ledger=ledger, actor=actor or "agent_turn",
        )
        if verdict is None or verdict.ok:
            return None
        return verdict.message or verdict.reason or "hard guardrail blocked tool"
    except Exception:  # noqa: BLE001
        return None

def _mirror_c1_grading_evidence(tool: str, args: dict, result: Any,
                                ledger, actor: str) -> None:
    """Write C1-shaped ledger evidence from successful domain tool calls.

    Gym graders (``c1_flanged_bushing``) require:
      * decision id=fit_bore with numeric ``clearance`` (AC2)
      * decision id=bolt_circle with ``holes`` / ``hole_d`` / ``pcd`` (AC3)
      * artifact kind=battery with ``verdict`` (AC4)
    Models often run fit_calc / bolt_circle / verify_battery correctly but never
    emit the ledger decision/artifact shape. Mirror once per successful call
    (idempotent upsert via ledger append last-write-wins on id/kind).
    """
    if not tool or ledger is None:
        return
    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except Exception:  # noqa: BLE001
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Unwrap common envelopes: {ok, answer} / aicad.calc/1 / nested result.
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else None
    if answer is None and isinstance(payload.get("result"), dict):
        inner = payload["result"]
        answer = inner.get("answer") if isinstance(inner.get("answer"), dict) else inner

    if tool in ("fit_calc", "fit_report", "recommend_fit"):
        clearance = None
        for src in (answer, payload):
            if not isinstance(src, dict):
                continue
            clr = src.get("clearance")
            if isinstance(clr, (list, tuple)) and len(clr) >= 2:
                try:
                    lo, hi = float(clr[0]), float(clr[1])
                    clearance = (lo + hi) / 2.0
                    break
                except (TypeError, ValueError):
                    pass
            if isinstance(clr, (int, float)):
                clearance = float(clr)
                break
            # fit_report may expose mean_clearance / clearance_mm
            for k in ("mean_clearance", "clearance_mm", "clearance_mean"):
                if src.get(k) is not None:
                    try:
                        clearance = float(src[k])
                        break
                    except (TypeError, ValueError):
                        continue
            if clearance is not None:
                break
        if clearance is not None:
            try:
                ledger.add_decision(
                    actor, "fit_bore", "specify",
                    text=f"fit from {tool}: clearance≈{clearance}",
                    rationale=f"{tool} → clearance {clearance}",
                )
                # add_decision doesn't take free fields; append a richer decision
                # event so grader sees clearance key.
                ledger.append(actor, "decision", {
                    "id": "fit_bore",
                    "stage": "specify",
                    "text": f"fit from {tool}: clearance≈{clearance}",
                    "rationale": f"{tool} mean/interval clearance",
                    "clearance": clearance,
                    "source_tool": tool,
                })
            except Exception:  # noqa: BLE001
                pass

    if tool in ("bolt_circle",) or (
        tool in ("add_features", "doc_apply_ops")
        and _args_contain_bolt_circle(args)
    ):
        holes = args.get("count") if isinstance(args, dict) else None
        hole_d = args.get("diameter") if isinstance(args, dict) else None
        pcd = args.get("pcd") if isinstance(args, dict) else None
        if holes is None and isinstance(args, dict):
            # dig into features/ops list
            for feat in _iter_feature_ops(args):
                if str(feat.get("type") or feat.get("op") or "").lower() == "bolt_circle":
                    params = feat.get("params") or feat
                    holes = params.get("count")
                    hole_d = params.get("diameter")
                    pcd = params.get("pcd")
                    break
        if holes is not None:
            try:
                ledger.append(actor, "decision", {
                    "id": "bolt_circle",
                    "stage": "plan",
                    "text": f"{holes}× holes on bolt circle",
                    "rationale": f"{tool} bolt_circle",
                    "holes": int(holes) if holes is not None else None,
                    "hole_d": hole_d,
                    "pcd": pcd,
                    "source_tool": tool,
                })
            except Exception:  # noqa: BLE001
                pass

    if tool == "verify_battery":
        verdict = None
        for src in (payload, answer):
            if not isinstance(src, dict):
                continue
            for k in ("verdict", "status", "result"):
                v = src.get(k)
                if isinstance(v, str) and v.strip():
                    verdict = v.strip().upper()
                    break
            if verdict:
                break
            # scores/findings shape: PASS when no failing findings
            findings = src.get("findings")
            scores = src.get("scores")
            if findings is not None or scores is not None:
                failing = []
                if isinstance(findings, list):
                    failing = [f for f in findings
                               if isinstance(f, dict)
                               and str(f.get("severity") or f.get("level") or "")
                               .lower() in ("fail", "error", "blocker")]
                verdict = "FAIL" if failing else "PASS"
                break
        if verdict is None and payload.get("ok") is True:
            verdict = "PASS"
        if verdict:
            try:
                ledger.add_artifact(
                    actor, "battery", "battery",
                    path="", stage="verify",
                    note=f"verify_battery {verdict}",
                    verdict=verdict,
                )
            except Exception:  # noqa: BLE001
                pass


def _args_contain_bolt_circle(args: dict | None) -> bool:
    if not isinstance(args, dict):
        return False
    for feat in _iter_feature_ops(args):
        if str(feat.get("type") or feat.get("op") or "").lower() == "bolt_circle":
            return True
    return False


def _iter_feature_ops(args: dict) -> list[dict]:
    out: list[dict] = []
    for key in ("features", "ops", "steps", "ops_list"):
        raw = args.get(key)
        if isinstance(raw, list):
            out.extend(x for x in raw if isinstance(x, dict))
    if str(args.get("type") or "").lower() == "bolt_circle":
        out.append(args)
    return out


def _log(ledger, actor: str, payload: dict) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor, "agent_turn", payload)
    except Exception:  # noqa: BLE001
        pass
