"""Deterministic Chudcode RPC-frame to Chudware feed-event mapping.

Only streamed deltas produce visible assistant text.  Completed message envelopes
are lifecycle records, so a final message cannot duplicate text already displayed.
The function keeps no state and never mutates its inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_MAX_DELTA = 16 * 1024
_MAX_DETAIL = 4096
_MAX_PREVIEW = 2048
_MAX_LABEL = 240


def _cap(value: Any, limit: int) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (bool, int, float)):
        text = str(value)
    else:
        try:
            text = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded(value: Any, depth: int = 0) -> Any:
    """Make unknown event data deterministic and bounded."""
    if depth >= 4:
        return _cap(value, 160)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _cap(value, _MAX_DETAIL)
    if isinstance(value, Mapping):
        pairs = [(str(key), key) for key in value]
        pairs.sort(key=lambda pair: (pair[0], type(pair[1]).__name__))
        out = {
            _cap(text, 80): _bounded(value[key], depth + 1) for text, key in pairs[:32]
        }
        if len(pairs) > 32:
            out["_truncated_keys"] = len(pairs) - 32
        return out
    if isinstance(value, (list, tuple)):
        out = [_bounded(item, depth + 1) for item in value[:32]]
        if len(value) > 32:
            out.append({"_truncated_items": len(value) - 32})
        return out
    return f"<{type(value).__name__}>"


def _canonical(value: Any) -> str:
    return json.dumps(
        _bounded(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any, length: int = 16) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", "replace")).hexdigest()[
        :length
    ]


def _name(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace("/", "_")
    )


def _str(data: Mapping[str, Any], *keys: str, limit: int = _MAX_DETAIL) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)):
            return _cap(value, limit)
    return ""


def _int(data: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            pass
    return default


def _bool(data: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"1", "true", "yes", "on"}:
                return True
            if value in {"0", "false", "no", "off"}:
                return False
    return None


def _flatten(frame: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Prefer raw top-level AgentSessionEvent, but tolerate injected wrappers."""
    outer = dict(frame)
    body: Mapping[str, Any] = outer
    event = outer.get("event")
    if isinstance(event, Mapping):
        body = event
    elif isinstance(outer.get("params"), Mapping) and _name(outer.get("method")) in {
        "event",
        "agent_event",
        "rpc_event",
        "notification",
        "notify",
    }:
        body = outer["params"]
    nested = (
        body.get("data")
        if isinstance(body.get("data"), Mapping)
        else body.get("payload")
    )
    merged: dict[str, Any] = {}
    for source in (outer, body, nested if isinstance(nested, Mapping) else {}):
        for key, value in source.items():
            if key not in {"event", "params", "data", "payload"}:
                merged[key] = value
    event_name = event if isinstance(event, str) else ""
    for source in (body, merged):
        if event_name:
            break
        for key in ("event_type", "type", "kind", "name"):
            candidate = source.get(key)
            if isinstance(candidate, str) and _name(candidate) not in {
                "event",
                "agent_event",
                "rpc_event",
                "notification",
                "notify",
            }:
                event_name = candidate
                break
    return _name(event_name), merged


def _first(data: Mapping[str, Any], context: Mapping[str, Any], *keys: str) -> str:
    return _str(data, *keys) or _str(context, *keys)


def _link(
    data: Mapping[str, Any], context: Mapping[str, Any], event: str
) -> dict[str, Any]:
    run_id = _first(data, context, "run_id", "runId")
    session_id = _first(data, context, "session_id", "sessionId")
    node_id = (
        _first(
            data,
            context,
            "node_id",
            "nodeId",
            "agent_id",
            "agentId",
            "turn_id",
            "turnId",
        )
        or run_id
        or "run"
    )
    span_id = _str(
        data,
        "id",
        "event_id",
        "eventId",
        "span_id",
        "spanId",
        "tool_call_id",
        "toolCallId",
        "call_id",
        "callId",
        "message_id",
        "messageId",
    )
    if not span_id:
        span_id = "rpc-" + _digest({"event": event, "data": data, "context": context})
    parent_id = _first(
        data,
        context,
        "parent_id",
        "parentId",
        "parent_span_id",
        "parentSpanId",
        "parent_tool_call_id",
        "parentToolCallId",
        "parent_call_id",
        "parentCallId",
    )
    actor = _first(data, context, "actor", "role", "agent_name", "agentName")
    out: dict[str, Any] = {"node_id": node_id, "id": span_id}
    if run_id:
        out["run_id"] = run_id
    if session_id:
        out["session_id"] = session_id
    if parent_id and parent_id != span_id:
        out["parent_id"] = parent_id
    if actor:
        out["actor"] = actor
    subagent_id = _str(data, "subagent_id", "subagentId")
    if subagent_id:
        out["subagent_id"] = subagent_id
    return out


def _linked(link: Mapping[str, Any], **payload: Any) -> dict[str, Any]:
    out = dict(link)
    out.update(payload)
    return out


def _status(
    link: Mapping[str, Any], phase: str, text: str, **extra: Any
) -> tuple[str, dict[str, Any]]:
    payload = _linked(link, phase=phase, text=_cap(text, _MAX_LABEL))
    payload.update(
        {key: value for key, value in extra.items() if value not in (None, "")}
    )
    return "status", payload


def _delta_text(data: Mapping[str, Any]) -> str:
    for key in ("text", "delta", "content", "value", "output"):
        value = data.get(key)
        if isinstance(value, str):
            return _cap(value, _MAX_DELTA)
        if isinstance(value, Mapping):
            nested = _delta_text(value)
            if nested or any(
                k in value for k in ("text", "delta", "content", "value", "output")
            ):
                return nested
    return ""


def _channel(event: str, data: Mapping[str, Any]) -> str:
    channel = _name(
        _str(
            data,
            "channel",
            "stream",
            "delta_type",
            "deltaType",
            "content_type",
            "contentType",
        )
    )
    if channel in {"thinking", "reasoning", "analysis", "thought"}:
        return "thinking"
    if any(word in event for word in ("thinking", "reasoning", "thought")):
        return "thinking"
    return "text"


def _tool_target(args: Any, data: Mapping[str, Any]) -> str:
    explicit = _str(data, "target", "label", limit=120)
    if explicit:
        return explicit
    if isinstance(args, Mapping):
        for key in (
            "name",
            "part_name",
            "assembly_name",
            "feature_name",
            "title",
            "query",
            "file_path",
            "folder_path",
            "path",
            "doc_id",
            "id",
        ):
            value = args.get(key)
            if isinstance(value, (str, int, float)) and str(value):
                return _cap(value, 120)
    return ""


def _tool_progress(
    event: str, data: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    link = _link(data, context, event)
    call_id = _str(data, "toolCallId", "tool_call_id", "callId", "call_id")
    if call_id:
        link["id"] = call_id
    tool = _str(data, "toolName", "tool_name", "tool", "name", limit=160) or "tool"
    text = _str(
        data,
        "message",
        "statusText",
        "status_text",
        "text",
        "partialResult",
        "partial_result",
        limit=_MAX_LABEL,
    )
    if not text:
        text = (
            "Starting " if event.endswith(("start", "started", "call")) else "Running "
        ) + tool
    return _status(link, "tool", text, tool_name=tool, tool_call_id=call_id)


def _tool_end(
    event: str, data: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    link = _link(data, context, event)
    call_id = _str(data, "toolCallId", "tool_call_id", "callId", "call_id")
    if call_id:
        link["id"] = call_id
    tool = _str(data, "toolName", "tool_name", "tool", "name", limit=160) or "tool"
    args = data.get("args", data.get("arguments", data.get("input", {})))
    result = data.get("result", data.get("output", data.get("response")))
    error = _str(
        data,
        "errorMessage",
        "error_preview",
        "error",
        "message",
        "reason",
        limit=_MAX_PREVIEW,
    )
    cancelled = event in {
        "tool_cancel",
        "tool_cancelled",
        "tool_canceled",
        "tool_call_cancelled",
        "tool_call_canceled",
        "host_tool_cancel",
    }
    ok = _bool(data, "ok", "success")
    if ok is None:
        is_error = _bool(data, "isError", "is_error")
        ok = not cancelled and is_error is not True and not bool(error)
    args_preview = _str(data, "argsPreview", "args_preview", limit=_MAX_PREVIEW)
    if not args_preview and args not in (None, {}, []):
        args_preview = _cap(_bounded(args), _MAX_PREVIEW)
    payload: dict[str, Any] = {
        "tool_name": tool,
        "target": _tool_target(args, data),
        "args_digest": _str(data, "argsDigest", "args_digest", limit=64)
        or _digest(args),
        "result_digest": _str(data, "resultDigest", "result_digest", limit=64)
        or _digest(result),
        "ok": bool(ok),
        "latency_ms": _int(
            data, "latencyMs", "latency_ms", "durationMs", "duration_ms"
        ),
    }
    if args_preview:
        payload["args_preview"] = args_preview
    if not ok:
        payload["error_preview"] = error or (
            "cancelled" if cancelled else "tool failed"
        )
    outcome = _name(_str(data, "outcomeKind", "outcome_kind", "outcome"))
    if outcome in {
        "ok",
        "policy_refusal",
        "guard_block",
        "validation_error",
        "timeout_unknown",
        "internal_error",
        "tool_error",
    }:
        payload["outcome_kind"] = outcome
    executed = _bool(data, "executed")
    if executed is not None:
        payload["executed"] = executed
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        bounded = []
        for item in artifacts[:8]:
            if not isinstance(item, Mapping):
                continue
            path = _str(item, "path", limit=_MAX_PREVIEW)
            if not path:
                continue
            kind = _name(_str(item, "kind"))
            bounded.append(
                {
                    "path": path,
                    "kind": kind if kind in {"image", "mesh", "file"} else "file",
                }
            )
        if bounded:
            payload["artifacts"] = bounded
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        payload["usage"] = {
            "prompt_tokens": _int(
                usage, "prompt_tokens", "input_tokens", "inputTokens"
            ),
            "context_tokens": _int(usage, "context_tokens", "contextTokens"),
            "cache_read": _int(
                usage, "cache_read", "cache_read_tokens", "cacheReadTokens"
            ),
        }
    return "agent.tool_call", _linked(link, **payload)


def _todos(
    data: Mapping[str, Any], context: Mapping[str, Any], event: str
) -> tuple[str, dict[str, Any]]:
    values = data.get("todos", data.get("items", data.get("tasks", [])))
    if not isinstance(values, list):
        values = []
    aliases = {
        "in_progress": "running",
        "inprogress": "running",
        "active": "running",
        "started": "running",
        "completed": "done",
        "complete": "done",
        "passed": "done",
        "pass": "done",
        "cancelled": "abandoned",
        "canceled": "abandoned",
        "skipped": "abandoned",
        "failed": "fail",
        "error": "fail",
        "open": "pending",
        "queued": "pending",
    }
    items = []
    for index, value in enumerate(values[:256]):
        item = {"title": value} if isinstance(value, str) else value
        if not isinstance(item, Mapping):
            continue
        raw_id = _str(item, "id", "todoId", "todo_id", "key", limit=120) or str(index)
        item_id = raw_id if ":" in raw_id else "todo:" + raw_id
        title = (
            _str(item, "title", "text", "content", "task", "name", limit=120)
            or f"Task {index + 1}"
        )
        status = _name(_str(item, "status", "state")) or "pending"
        status = aliases.get(
            status,
            status
            if status in {"pending", "running", "done", "fail", "abandoned"}
            else "pending",
        )
        row: dict[str, Any] = {
            "id": item_id,
            "title": title,
            "status": status,
            "kind": "task",
        }
        desc = _str(item, "desc", "description", "detail", limit=500)
        if desc:
            row["desc"] = desc
        parent = _str(item, "parentId", "parent_id", limit=120)
        if parent:
            row["parent_id"] = parent if ":" in parent else "todo:" + parent
        items.append(row)
    payload: dict[str, Any] = {
        "shape": "checklist",
        "title": _str(data, "title", limit=120) or "Plan",
        "revision": _int(data, "revision", "version"),
        "items": items,
    }
    summary = _str(data, "summary", limit=500)
    if summary:
        payload["summary"] = summary
    return "plan_state", _linked(_link(data, context, event), **payload)


def _subrun(
    data: Mapping[str, Any],
    context: Mapping[str, Any],
    event: str,
    status: str,
    default_purpose: str = "Subagent",
) -> tuple[str, dict[str, Any]]:
    link = _link(data, context, event)
    item_id = _str(data, "subagentId", "subagent_id", "agentId", "agent_id", "id")
    if item_id:
        link["id"] = item_id
        if "subagent" in event or _str(data, "subagentId", "subagent_id"):
            link["subagent_id"] = item_id
    purpose = _str(
        data, "purpose", "brief", "task", "title", "name", "message", limit=240
    )
    purpose = purpose or _str(data, "role", limit=120) or default_purpose
    payload: dict[str, Any] = {"purpose": purpose, "status": status}
    parent_stage = _str(
        data, "parentStage", "parent_stage", "parentId", "parent_id", limit=120
    )
    if parent_stage:
        payload["parent_stage"] = parent_stage
    progress = data.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        payload["progress"] = max(0.0, min(1.0, float(progress)))
    detail = _str(
        data,
        "detail",
        "output",
        "result",
        "error",
        "errorMessage",
        "finalError",
        "partialResult",
        limit=_MAX_DETAIL,
    )
    if detail:
        payload["detail"] = detail
    session_file = _str(data, "sessionFile", "session_file", limit=_MAX_PREVIEW)
    if session_file:
        payload["session_file"] = session_file
    return "sub_run", _linked(link, **payload)


def _terminal(
    data: Mapping[str, Any], context: Mapping[str, Any], default: bool
) -> bool:
    explicit = _bool(
        data, "isTerminal", "is_terminal", "terminal", "isFinal", "is_final", "final"
    )
    if explicit is None:
        explicit = _bool(context, "isTerminal", "is_terminal", "terminal")
    if explicit is not None:
        return explicit
    if _name(_str(data, "scope")) in {"run", "session", "root"}:
        return True
    if not _is_root(data, context):
        return False
    return default


def _is_root(data: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """False for frames a nested agent produced inside the top-level stream."""
    subagent = _str(data, "subagentId", "subagent_id")
    agent = _str(data, "agentId", "agent_id")
    root = _str(context, "rootAgentId", "root_agent_id", "agentId", "agent_id")
    return not (
        subagent
        or (agent and root and agent != root)
        or _bool(context, "isSubagent", "is_subagent") is True
    )


def _context_tokens(usage: Mapping[str, Any]) -> int:
    """Chudcode's ``calculateContextTokens``: the prefix the next request resends.

    Provider-reported occupancy wins; otherwise the completion total (the reply
    joins the prefix) minus provider orchestration tokens that never replay.
    """
    explicit = usage.get("contextTokens", usage.get("context_tokens"))
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return max(0, int(explicit))
    orchestration = usage.get("orchestration")
    replayless = (
        _int(orchestration, "input") + _int(orchestration, "output")
        + _int(orchestration, "cacheRead")
        if isinstance(orchestration, Mapping)
        else 0
    )
    total = _int(usage, "totalTokens", "total_tokens") or sum(
        _int(usage, key) for key in ("input", "output", "cacheRead", "cacheWrite")
    )
    return max(0, total - replayless)


def _assistant_context(
    data: Mapping[str, Any], context: Mapping[str, Any], link: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Measured occupancy from a finished top-level assistant message, if any."""
    message = data.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    # Chudcode's own anchor skips aborted/error completions: their usage is partial.
    if not isinstance(usage, Mapping) or not _is_root(data, context) or _name(
        _str(message, "stopReason", "stop_reason")
    ) in {"error", "aborted"}:
        return None
    used = _context_tokens(usage)
    if used <= 0:
        return None
    return (
        "agent.context",
        _linked(
            link,
            used=used,
            cache_read=_int(usage, "cacheRead", "cache_read"),
            source="api",
            model=_str(message, "model", limit=200),
            provider=_str(message, "provider", limit=120),
        ),
    )


def _finished(
    data: Mapping[str, Any], context: Mapping[str, Any], event: str
) -> tuple[str, dict[str, Any]]:
    status = _name(_str(data, "status", "outcome", "phase"))
    if status in {"ok", "success", "completed", "complete", "pass", "passed"}:
        status = "done"
    elif status in {"cancel", "canceled", "cancelled", "aborted"}:
        status = "cancelled"
    elif status in {"error", "failed", "failure"}:
        status = "failed"
    payload: dict[str, Any] = {"status": status or "done"}
    stop = _str(data, "stopReason", "stop_reason", "reason", limit=160)
    error = _str(
        data, "errorDetail", "error_detail", "errorMessage", "error", limit=_MAX_PREVIEW
    )
    human = _str(data, "errorHuman", "error_human", limit=500)
    model = _str(data, "model", "modelId", "model_id", limit=200)
    if stop:
        payload["stop_reason"] = stop
    if error:
        payload["error_detail"] = error
    if human:
        payload["error_human"] = human
    if model:
        payload["model"] = model
    return "run_finished", _linked(_link(data, context, event), **payload)


def _unknown(
    event: str, data: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    label = event or "unknown"
    status = _name(_str(data, "status", "state")) or "done"
    status = {
        "started": "running",
        "start": "running",
        "active": "running",
        "in_progress": "running",
        "complete": "done",
        "completed": "done",
        "success": "done",
        "ok": "done",
        "failed": "fail",
        "failure": "fail",
        "error": "fail",
    }.get(status, status)
    payload = {
        "purpose": _cap("Chudcode · " + label.replace("_", " "), 120),
        "status": status,
        "rpc_event": _cap(label, 120),
        "detail": _cap(_canonical(data), _MAX_DETAIL),
    }
    parent = _first(
        data, context, "parentStage", "parent_stage", "parentId", "parent_id"
    )
    if parent:
        payload["parent_stage"] = _cap(parent, 120)
    return "sub_run", _linked(_link(data, context, label), **payload)


def _message_update(
    data: Mapping[str, Any], context: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    assistant_event = data.get("assistantMessageEvent")
    if not isinstance(assistant_event, Mapping):
        assistant_event = data.get("assistant_message_event")
    if not isinstance(assistant_event, Mapping):
        return [_unknown("message_update", data, context)]
    child = dict(assistant_event)
    message = data.get("message")
    if isinstance(message, Mapping):
        for key in ("id", "messageId", "message_id", "parentId", "parent_id", "role"):
            if key in message and key not in child:
                child[key] = message[key]
    for key in (
        "runId",
        "run_id",
        "sessionId",
        "session_id",
        "turnId",
        "turn_id",
        "agentId",
        "agent_id",
        "subagentId",
        "subagent_id",
    ):
        if key in data and key not in child:
            child[key] = data[key]
    return map_rpc_frame(child, context)


def map_rpc_frame(
    frame: Mapping[str, Any], context: Mapping[str, Any] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Map one raw ``AgentSessionEvent`` to ordered Chudware feed shapes."""
    ctx: Mapping[str, Any] = context if isinstance(context, Mapping) else {}
    if not isinstance(frame, Mapping):
        return [_unknown("invalid_frame", {"value": _bounded(frame)}, ctx)]
    event, data = _flatten(frame)
    if event in {
        "message_start",
        "message_started",
        "message_end",
        "message_completed",
        "message_complete",
        "turn_end",
        "turn_completed",
        "turn_complete",
    } and isinstance(data.get("message"), Mapping):
        # Lifecycle frames carry linkage inside message; copy only identifiers.
        # Content remains intentionally suppressed because its deltas already rendered.
        normalized = dict(data)
        message = data["message"]
        for key in ("id", "messageId", "message_id", "parentId", "parent_id", "role"):
            if key in message and key not in normalized:
                normalized[key] = message[key]
        data = normalized
    link = _link(data, ctx, event or "unknown")

    if event == "message_update":
        return _message_update(data, ctx)

    deltas = {
        "assistant_delta",
        "message_delta",
        "text_delta",
        "output_delta",
        "output_text_delta",
        "content_delta",
        "thinking_delta",
        "reasoning_delta",
        "thought_delta",
        "text",
        "thinking",
        "reasoning",
    }
    if event in deltas:
        # Empty/whitespace text flushes retain their text channel.  feed.cpp ignores
        # them as boundaries, so they cannot fragment an active Thinking box.
        return [
            (
                "assistant_delta",
                _linked(link, channel=_channel(event, data), text=_delta_text(data)),
            )
        ]

    # Assistant content-block boundaries. Each is fully represented by the stream it
    # brackets: text_*/thinking_* by their own deltas above, toolcall_* by the
    # tool_execution_* lifecycle that actually runs the call. The *_end frames also
    # carry the finished content/toolCall, which must never replay as visible text or
    # as a second tool row. They are boundaries, not activity — emit no row.
    # Until 2026-09-03 they fell through to _unknown() and every turn's live feed was
    # dozens of "Chudcode · toolcall delta" / "Chudcode · text end" rows that pushed
    # the actual answer out of view.
    if event in {
        "text_start",
        "text_end",
        "thinking_start",
        "thinking_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
    }:
        return []

    if event in {"agent_start", "agent_started", "run_start", "run_started"}:
        payload = _linked(link, status="running")
        model = _str(data, "model", "modelId", "model_id", limit=200)
        if model:
            payload["model"] = model
        return [("run_started", payload)]
    lifecycle = {
        "session_start": ("session_start", "Session started"),
        "session_started": ("session_start", "Session started"),
        "session_created": ("session_start", "Session started"),
        "turn_start": ("turn_start", "Turn started"),
        "turn_started": ("turn_start", "Turn started"),
        "message_start": ("message_start", "Message started"),
        "message_started": ("message_start", "Message started"),
        "message_end": ("message_end", "Message complete"),
        "message_completed": ("message_end", "Message complete"),
        "message_complete": ("message_end", "Message complete"),
        "turn_end": ("turn_end", "Turn complete"),
        "turn_completed": ("turn_end", "Turn complete"),
        "turn_complete": ("turn_end", "Turn complete"),
        "session_end": ("session_end", "Session ended"),
        "session_closed": ("session_end", "Session ended"),
        "prompt_result": ("prompt_result", "Prompt complete"),
    }
    if event in lifecycle:
        phase, fallback = lifecycle[event]
        # Never copy message/prompt-result final content into assistant_delta.
        mapped = [
            _status(
                link,
                phase,
                _str(data, "statusText", "message", limit=_MAX_LABEL) or fallback,
            )
        ]
        # Chudcode reports provider usage ONLY on the assistant message envelope.
        if phase == "message_end":
            measured = _assistant_context(data, ctx, link)
            if measured is not None:
                mapped.append(measured)
        return mapped

    tool_start = {
        "tool_start",
        "tool_started",
        "tool_call_start",
        "tool_call_started",
        "tool_execution_start",
        "builtin_tool_start",
        "host_tool_start",
        "host_tool_call",
    }
    tool_update = {
        "tool_update",
        "tool_progress",
        "tool_call_update",
        "tool_call_progress",
        "tool_execution_update",
        "update",
        "builtin_tool_update",
        "host_tool_update",
        "host_tool_progress",
    }
    tool_end = {
        "tool_end",
        "tool_completed",
        "tool_complete",
        "tool_result",
        "tool_call_end",
        "tool_call_completed",
        "tool_execution_end",
        "end",
        "builtin_tool_end",
        "builtin_tool_result",
        "host_tool_end",
        "host_tool_result",
    }
    tool_cancel = {
        "tool_cancel",
        "tool_cancelled",
        "tool_canceled",
        "tool_call_cancelled",
        "tool_call_canceled",
        "host_tool_cancel",
    }
    if event in tool_start | tool_update:
        # A raw host_tool_call carrying a synchronous result is already complete.
        if event == "host_tool_call" and any(
            key in data for key in ("result", "output", "isError")
        ):
            return [_tool_end(event, data, ctx)]
        return [_tool_progress(event, data, ctx)]
    if event in tool_end | tool_cancel:
        return [_tool_end(event, data, ctx)]

    if event in {
        "todo_update",
        "todo_updated",
        "todos",
        "todo_snapshot",
        "plan_update",
        "plan_state",
        "todo_reminder",
        "todo_auto_clear",
        "auto_clear",
    }:
        if (
            event in {"todo_auto_clear", "auto_clear"}
            and "todos" not in data
            and "items" not in data
        ):
            data = dict(data)
            data["todos"] = []
        if event == "todo_reminder" and not any(
            key in data for key in ("todos", "items", "tasks")
        ):
            return [_unknown(event, data, ctx)]
        return [_todos(data, ctx, event)]

    if event in {
        "model_change",
        "model_changed",
        "model_update",
        "model_selected",
        "model_assignment",
        "succeeded",
    }:
        payload = {
            "depth": _int(data, "depth"),
            "task_class": _str(data, "taskClass", "task_class", limit=120) or "unknown",
            "min_rank": data.get("minRank", data.get("min_rank")),
            "picked_model": _str(
                data,
                "pickedModel",
                "picked_model",
                "model",
                "modelId",
                "model_id",
                limit=200,
            ),
            "picked_provider": _str(
                data, "pickedProvider", "picked_provider", "provider", limit=120
            ),
            "cost_tier": data.get("costTier", data.get("cost_tier")),
            "mode": _str(data, "mode", limit=80),
            "rank": data.get("rank"),
            "reason": _str(data, "reason", limit=160),
            "role": _str(data, "role", limit=120),
        }
        return [("agent.model_assignment", _linked(link, **payload))]
    if event in {
        "thinking_change",
        "thinking_changed",
        "thinking_mode",
        "reasoning_effort_changed",
        "reasoning_change",
    }:
        mode = _str(
            data,
            "thinking",
            "mode",
            "effort",
            "reasoningEffort",
            "reasoning_effort",
            limit=120,
        )
        return [
            _status(
                link,
                "thinking_change",
                "Thinking mode" + (f" · {mode}" if mode else ""),
                thinking=mode,
            )
        ]

    if event in {"context_injected", "memory_injected", "prompt_context"}:
        payload: dict[str, Any] = {
            "source": _str(data, "source", limit=80) or "other",
            "label": _str(data, "label", "name", "title", limit=80) or "context",
        }
        tokens = _int(data, "tokenEst", "token_est", "tokens")
        if tokens:
            payload["token_est"] = tokens
        return [("agent.context_injected", _linked(link, **payload))]
    if event in {
        "usage",
        "usage_update",
        "token_usage",
        "provider_usage",
        "context_usage",
    }:
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else data
        provider = {
            "provider": _str(usage, "provider", limit=120),
            "model": _str(usage, "model", "modelId", "model_id", limit=200),
            "prompt_tokens": _int(
                usage, "promptTokens", "prompt_tokens", "inputTokens", "input_tokens"
            ),
            "completion_tokens": _int(
                usage,
                "completionTokens",
                "completion_tokens",
                "outputTokens",
                "output_tokens",
            ),
            "total_tokens": _int(usage, "totalTokens", "total_tokens"),
            "context_tokens": _int(usage, "contextTokens", "context_tokens"),
            "source": _str(usage, "source", limit=80) or "api",
        }
        if not provider["total_tokens"]:
            provider["total_tokens"] = (
                provider["prompt_tokens"] + provider["completion_tokens"]
            )
        # agent.context is the frozen meter contract (`used`/`window`, see
        # agent_turn); `cache_read` feeds the bubble's per-turn cache figure.
        context_payload = {
            "used": provider["context_tokens"] or provider["prompt_tokens"],
            "cache_read": _int(
                usage, "cacheRead", "cache_read", "cacheReadTokens", "cache_read_tokens"
            ),
            "source": provider["source"],
        }
        return [
            ("agent.provider_usage", _linked(link, **provider)),
            ("agent.context", _linked(link, **context_payload)),
        ]
    if event in {"context_update", "context_changed", "context_window"}:
        payload: dict[str, Any] = {
            "cache_read": _int(
                data, "cacheRead", "cache_read", "cacheReadTokens", "cache_read_tokens"
            ),
        }
        # Absent is not zero: a window-only update must not blank the measured fill.
        if any(key in data for key in ("contextTokens", "context_tokens", "tokens")):
            payload["used"] = _int(data, "contextTokens", "context_tokens", "tokens")
        window = _int(data, "contextWindow", "context_window", "window")
        if window > 0:
            payload["window"] = window
            payload["window_source"] = _str(data, "windowSource", "window_source", limit=40)
        return [("agent.context", _linked(link, **payload))]

    compaction = {
        "auto_compaction_start",
        "auto_compaction_end",
        "compaction_start",
        "compaction_started",
        "compaction_update",
        "compaction_progress",
        "compaction_end",
        "compaction_completed",
        "compaction_failed",
    }
    if event in compaction:
        if event.endswith(("start", "started", "update", "progress")):
            status = "running"
        elif (
            event.endswith("failed")
            or _bool(data, "aborted") is True
            or data.get("errorMessage")
        ):
            status = "fail"
        elif _bool(data, "skipped") is True:
            status = "skipped"
        else:
            status = "done"
        work = dict(data)
        work.setdefault(
            "id",
            _str(data, "compactionId", "compaction_id") or "compaction:" + link["id"],
        )
        work.setdefault(
            "purpose",
            _str(data, "message", "summary", "reason", "action", limit=240)
            or "Compacting context",
        )
        return [_subrun(work, ctx, event, status, "Compacting context")]

    retry_start = {
        "auto_retry_start",
        "retry",
        "retry_scheduled",
        "provider_retry",
        "request_retry",
    }
    if event in retry_start:
        attempt = _int(data, "attempt", "retry", default=1) or 1
        payload = {
            "provider": _str(data, "provider", limit=120),
            "model": _str(data, "model", "modelId", "model_id", limit=200),
            "attempt": attempt,
            "max_attempts": _int(data, "maxAttempts", "max_attempts"),
            "delay_ms": _int(data, "delayMs", "delay_ms"),
            "ok": False,
            "http_status": _int(data, "httpStatus", "http_status"),
            "latency_ms": _int(data, "latencyMs", "latency_ms"),
            "error_excerpt": _str(
                data, "errorMessage", "reason", "error", "message", limit=_MAX_PREVIEW
            )
            or "retry scheduled",
            "request_id": _str(data, "requestId", "request_id", limit=160),
        }
        return [("provider_call", _linked(link, **payload))]
    if event == "auto_retry_end":
        work = dict(data)
        success = _bool(data, "success") is True
        work.setdefault("purpose", "Retry " + ("succeeded" if success else "failed"))
        return [_subrun(work, ctx, event, "done" if success else "fail", "Retry")]
    if event in {
        "fallback",
        "provider_fallback",
        "model_fallback",
        "fallback_selected",
        "retry_fallback_applied",
    }:
        work = dict(data)
        work.setdefault(
            "id", _str(data, "fallbackId", "fallback_id") or "fallback:" + link["id"]
        )
        target = _str(data, "toModel", "to_model", "model", "provider", limit=160)
        work.setdefault("purpose", "Fallback" + (f" · {target}" if target else ""))
        return [_subrun(work, ctx, event, "done", "Fallback")]

    if event in {"subagent_spawn", "subagent_spawned", "agent_spawn", "spawn"}:
        subagent = (
            _str(data, "subagentId", "subagent_id", "agentId", "agent_id", "id")
            or link["id"]
        )
        payload = {
            "subagent_id": subagent,
            "role": _str(data, "role", "name", limit=120) or "worker",
            "spawn_kind": _str(data, "spawnKind", "spawn_kind", limit=80) or "worker",
            "brief": _str(data, "brief", "task", "purpose", limit=120),
            "budget_tokens": _int(
                data, "budgetTokens", "budget_tokens", "tokenBudget", "token_budget"
            ),
        }
        return [("agent.spawn", _linked(link, **payload))]
    if event in {"subagent_start", "subagent_started"}:
        return [_subrun(data, ctx, event, "running")]
    if event in {
        "subagent_update",
        "subagent_progress",
        "subagent_event",
        "agent_progress",
    }:
        status = _name(_str(data, "status", "state")) or "running"
        status = {
            "in_progress": "running",
            "active": "running",
            "started": "running",
        }.get(status, status)
        return [_subrun(data, ctx, event, status)]
    if event in {
        "subagent_end",
        "subagent_completed",
        "subagent_result",
        "subagent_failed",
    }:
        raw = _name(_str(data, "status", "state", "outcome"))
        status = (
            "fail"
            if event.endswith("failed") or raw in {"fail", "failed", "error"}
            else "done"
        )
        return [_subrun(data, ctx, event, status)]
    # Generic subscription notifications retain lineage without requiring a frozen
    # Chudcode notification vocabulary.
    terminal_events = {
        "agent_end",
        "agent_completed",
        "agent_complete",
        "run_end",
        "run_completed",
        "run_complete",
    }
    error_events = {
        "error",
        "agent_error",
        "turn_error",
        "provider_error",
        "rpc_error",
        "protocol_error",
    }
    if (
        (
            _str(data, "subagentId", "subagent_id")
            or _str(data, "sessionFile", "session_file")
        )
        and event not in terminal_events
        and event not in error_events
    ):
        status = _name(_str(data, "status", "state"))
        if not status:
            status = "fail" if event.endswith(("error", "failed")) else "running"
        return [_subrun(data, ctx, event, status)]

    if event in {
        "extension_ui_request",
        "ui_request",
        "extension_request",
        "elicitation_request",
    }:
        # A UI request is only a question when its method is one of Chudcode's
        # four INTERACTIVE methods (protocol.py INTERACTIVE_EXTENSION_UI_METHODS:
        # select/confirm/input/editor). Every other method — notify, setStatus,
        # setWidget, setTitle, set_editor_text, open_url — is PASSIVE: it carries
        # information and expects no answer. Mapping the whole family to
        # "waiting" parked a spinner row in the feed that NOTHING could ever
        # resolve, because no terminal frame exists for a passive notice, so
        # every turn that emitted one ended looking hung.
        #
        # The interactive four cannot be answered either, but for the opposite
        # reason: chudcode_host._register_login_ui cancels them outright (the
        # desktop app has no prompt surface to show), so the honest row is a
        # declined request rather than a pending one.
        method = _name(_str(data, "method"))
        interactive = method in {"select", "confirm", "input", "editor"}
        work = dict(data)
        work.setdefault(
            "purpose",
            _str(data, "title", "label", "prompt", "message", "request", limit=240)
            or ("Extension UI request" if interactive else "Extension notice"),
        )
        if interactive:
            work.setdefault(
                "detail", "declined: the desktop app has no interactive prompt"
            )
        result = _subrun(
            work,
            ctx,
            event,
            "fail" if interactive else "done",
            "Extension UI request",
        )
        result[1]["request"] = _bounded(data.get("request", data.get("ui", {})))
        return [result]
    if event in {"extension_error"}:
        work = dict(data)
        work.setdefault(
            "purpose", _str(data, "message", "error", limit=240) or "Extension error"
        )
        return [_subrun(work, ctx, event, "fail", "Extension error")]
    if event in {
        "command_output",
        "command_stdout",
        "command_stderr",
        "shell_output",
        "process_output",
    }:
        work = dict(data)
        stream = (
            "stderr"
            if "stderr" in event
            else _str(data, "stream", limit=40) or "stdout"
        )
        work.setdefault(
            "purpose",
            _str(data, "command", "cmd", "name", limit=160) or "Command " + stream,
        )
        work["detail"] = _str(
            data, "output", "text", "data", "chunk", limit=_MAX_DETAIL
        )
        work.setdefault(
            "id",
            _str(data, "commandId", "command_id", "processId", "process_id")
            or link["id"],
        )
        status = _name(_str(data, "status", "state")) or "running"
        if _bool(data, "done", "complete") is True:
            status = "done"
        return [_subrun(work, ctx, event, status, "Command output")]

    if event in {
        "error",
        "agent_error",
        "turn_error",
        "provider_error",
        "rpc_error",
        "protocol_error",
    }:
        if _terminal(data, ctx, False):
            error = (
                _str(
                    data,
                    "errorMessage",
                    "error",
                    "message",
                    "detail",
                    limit=_MAX_PREVIEW,
                )
                or "Agent error"
            )
            status = _status(
                link,
                "error",
                error,
                stop_reason=_str(data, "stopReason", "stop_reason", limit=160)
                or "provider_error",
                error_detail=error,
                error_human=_str(data, "errorHuman", "error_human", limit=500),
            )
            return [status, _finished(data, ctx, event)]
        work = dict(data)
        work.setdefault(
            "purpose",
            _str(data, "message", "errorMessage", "error", limit=240) or "Agent error",
        )
        return [_subrun(work, ctx, event, "fail", "Agent error")]

    if event in {
        "agent_end",
        "agent_completed",
        "agent_complete",
        "run_end",
        "run_completed",
        "run_complete",
    }:
        # Chudcode's contract: isTerminal absent/true ends the run; false is a
        # maintenance boundary and the session resumes afterwards.
        if _terminal(data, ctx, True):
            return [_finished(data, ctx, event)]
        raw = _name(_str(data, "status", "state", "outcome"))
        return [
            _subrun(
                data,
                ctx,
                event,
                "fail" if raw in {"fail", "failed", "error"} else "done",
                "Agent maintenance",
            )
        ]

    return [_unknown(event, data, ctx)]


__all__ = ["map_rpc_frame"]
