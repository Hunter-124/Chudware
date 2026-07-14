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

# T-W32: match the fenced BODY (not a brace pair) — the old r"\{.*?\}" non-greedy
# pattern truncated at the FIRST closing brace, so any tool call with nested args
# (e.g. {"tool": "x", "args": {"a": {...}}}) failed to parse and the model's valid
# directive was dropped as free text (observed live in the r4 PLAN node).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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
    "goal is met, reply with:\n"
    "```json\n{\"done\": true, \"handoff\": {<structured summary>}}\n```\n"
    "Call only tools from the allowed list. Do not invent results — run the tool."
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
             task_class: str | None = None) -> TurnResult:
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
    _budget_in = max_tool_calls if max_tool_calls is not None else 8
    _capped = False
    if _sc is not None and _cap is not None:
        _budget_in, _capped = _sc.apply_phase_cap(
            _cap, _phase, int(_budget_in) if max_tool_calls is not None else None,
            ledger=ledger, actor=actor or name,
        )
        if _budget_in is None:
            _budget_in = 8
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

    calls: list[dict] = []
    tool_calls_made = 0
    last_reason = _NOCODE
    _prev_stage: str | None = None
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
            _log(ledger, actor, {"event": "provider_error", "error": str(exc)})
            return TurnResult(False, _FAILED,
                              {"error": f"provider error: {exc}", "calls": calls})

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
                bare_handoff = {"text": reply[:2000]}
                block = _done_guardrail_block(bare_handoff, ledger, actor, calls)
                if block is not None:
                    messages.append({"role": "user", "content": _parts(block)})
                    continue
                return TurnResult(True, _DONE,
                                  {"handoff": bare_handoff, "calls": calls})
            last_reason = _NOCODE
            break

        if parsed.get("done"):
            handoff = parsed.get("handoff") or {}
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

    return TurnResult(tool_calls_made > 0 and last_reason in (_RETRY, "phase_step_cap"),
                      last_reason, {"calls": calls,
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

def _log(ledger, actor: str, payload: dict) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor, "agent_turn", payload)
    except Exception:  # noqa: BLE001
        pass
