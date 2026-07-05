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

_TOOL_CALL_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


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
             tool_dispatch: Callable[[str, dict], Any] | None = None) -> TurnResult:
    """Run a bounded tool-executing turn and return a :class:`TurnResult`.

    *tool_dispatch* is injectable ``(tool_name, args) -> result``; the default
    dispatches against the live MCP registry via :func:`_dispatch_tool`.  Tests
    inject a deterministic dispatcher (no engine).

    *max_tokens* caps each model completion.  Callers on a known tier pass the
    tier's ``generation_headroom`` (tiers.yaml — the same budget source the
    aicad.effort/1 resolution path reads), so the turn is bounded by tier
    budgets rather than an ad-hoc constant.  Default 2048 when no tier is known."""
    budget = int(max_tool_calls if max_tool_calls is not None else 8)
    completion_cap = int(max_tokens) if max_tokens else 2048
    dispatch = tool_dispatch or _dispatch_tool
    allow = set(allowed_tools or [])
    actor = actor or name

    system = (system_prompt or "") + "\n\n" + _SYSTEM_BASE
    if allow:
        system += "\nAllowed tools: " + ", ".join(sorted(allow))

    messages: list[dict] = [{"role": "user", "content": _parts(request)}]

    calls: list[dict] = []
    tool_calls_made = 0
    last_reason = _NOCODE

    for _ in range(budget + 1):
        try:
            reply = provider.complete(system, messages, max_tokens=completion_cap)
        except Exception as exc:  # noqa: BLE001 — provider/network error → escalate
            _log(ledger, actor, {"event": "provider_error", "error": str(exc)})
            return TurnResult(False, _FAILED,
                              {"error": f"provider error: {exc}", "calls": calls})

        messages.append({"role": "assistant", "content": _parts(reply)})
        parsed = _parse_directive(reply)

        if parsed is None:
            # No structured directive. If the model already ran tools, treat a bare
            # natural-language finish as done-with-handoff; otherwise it produced
            # nothing actionable (no_code).
            _log(ledger, actor, {"event": "turn", "kind": "text", "text": reply[:400]})
            if tool_calls_made > 0:
                return TurnResult(True, _DONE,
                                  {"handoff": {"text": reply[:2000]}, "calls": calls})
            last_reason = _NOCODE
            break

        if parsed.get("done"):
            handoff = parsed.get("handoff") or {}
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
            last_reason = _RETRY
            break

        tool_calls_made += 1
        try:
            result = dispatch(tool, args)
            ok = True
        except Exception as exc:  # noqa: BLE001 — dispatch error fed back to model
            result = {"ok": False, "error": str(exc)}
            ok = False
        call_rec = {"tool": tool, "args": args, "ok": ok,
                    "result": _truncate(result)}
        calls.append(call_rec)
        _log(ledger, actor, {"event": "tool_call", **call_rec})
        messages.append({"role": "user", "content": _parts(
            f"Result of {tool}:\n{_as_text(result)}")})

    return TurnResult(tool_calls_made > 0 and last_reason == _RETRY,
                      last_reason, {"calls": calls,
                                    "tool_calls": tool_calls_made})


# ---------------------------------------------------------------------------
# Parsing + dispatch
# ---------------------------------------------------------------------------

def _parse_directive(reply: str) -> dict | None:
    """Extract the first fenced JSON object from *reply*; None if absent/invalid."""
    if not reply:
        return None
    m = _TOOL_CALL_RE.search(reply)
    candidate = m.group(1) if m else None
    if candidate is None:
        # Some models emit a bare JSON object with no fence.
        stripped = reply.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidate = stripped
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


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


def _log(ledger, actor: str, payload: dict) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor, "agent_turn", payload)
    except Exception:  # noqa: BLE001
        pass
