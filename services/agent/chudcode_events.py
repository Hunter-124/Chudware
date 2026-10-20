"""Deterministic Chudcode RPC-frame to Chudware feed-event mapping.

Only streamed deltas produce visible assistant text. Completed message envelopes
are lifecycle records, so a final message cannot duplicate text already displayed.
Per-turn tool-call state retains bounded start arguments until the terminal frame.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from services.agent import tool_images

_MAX_DELTA = 16 * 1024
_MAX_DETAIL = 4096
_MAX_PREVIEW = 2048
_MAX_LABEL = 240
_MAX_CALLS = 512
_MAX_TERMINALS = 4096
_MAX_TOOL_CONTENT = 64 * 1024
_MAX_ARTIFACTS = 8


class RpcFrameState:
    """One turn's bounded tool-start previews and terminal call identifiers."""

    def __init__(self) -> None:
        self._calls: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._terminals: OrderedDict[str, None] = OrderedDict()
        self._subagents: OrderedDict[str, str] = OrderedDict()
        self._compactions = 0
        self._open_compaction = ""

    def compaction_id(self, base: str, phase: str) -> str:
        """Shared id for one compaction's start, progress and end frames.

        Chudcode's compaction frames carry no id: ``phase`` "start" opens a new
        compaction, "end" closes the open one (opening one if none was seen).
        """
        if phase == "start" or not self._open_compaction:
            self._compactions += 1
            self._open_compaction = f"{base}:{self._compactions}"
        current = self._open_compaction
        if phase == "end":
            self._open_compaction = ""
        return current

    def remember_subagent(self, subagent_id: str, parent_tool_call_id: str) -> None:
        """Bind a task sub-agent to the parent ``task`` call that spawned it."""
        if not subagent_id or not parent_tool_call_id:
            return
        self._subagents[subagent_id] = parent_tool_call_id
        self._subagents.move_to_end(subagent_id)
        if len(self._subagents) > _MAX_CALLS:
            self._subagents.popitem(last=False)

    def subagent_parent(self, subagent_id: str) -> str:
        return self._subagents.get(subagent_id, "")

    def remember(self, call_id: str, data: Mapping[str, Any]) -> None:
        if not call_id or call_id in self._terminals:
            return
        args = data.get("args", data.get("arguments", data.get("input")))
        display_args, xd_kind = _tool_display_args(args)
        preview = (
            "" if xd_kind else _str(data, "argsPreview", "args_preview", limit=_MAX_PREVIEW)
        )
        if (xd_kind == "call" or not preview) and display_args not in (None, {}, []):
            preview = _cap(_bounded(display_args), _MAX_PREVIEW)
        previous = self._calls.get(call_id, {})
        fields = {
            "tool_name": _str(data, "toolName", "tool_name", "tool", "name", limit=160),
            "target": _tool_target(display_args, data, xd_kind),
            "args_preview": preview,
            "args_digest": _digest(args) if args not in (None, {}, []) else "",
            "intent": _str(data, "intent", limit=_MAX_LABEL),
            "parent_id": _str(
                data, "parentToolCallId", "parent_tool_call_id", "parentId", "parent_id"
            ),
        }
        self._calls[call_id] = {
            key: value or previous.get(key, "") for key, value in fields.items()
        }
        self._calls.move_to_end(call_id)
        if len(self._calls) > _MAX_CALLS:
            self._calls.popitem(last=False)

    def complete(self, call_id: str) -> dict[str, str] | None:
        if not call_id or call_id in self._terminals:
            return None
        saved = self._calls.pop(call_id, {})
        self._terminals[call_id] = None
        if len(self._terminals) > _MAX_TERMINALS:
            self._terminals.popitem(last=False)
        return saved

    def terminal_seen(self, call_id: str) -> bool:
        return bool(call_id) and call_id in self._terminals



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


def _question_payload(q: Mapping[str, Any]) -> dict[str, Any]:
    """One contract-§2 question for the GUI card: known fields, bounded, structure kept.

    ``_bounded`` flattens below depth 4, which would turn every choice option of a
    batch into a string; the card needs ``options[i].value``/``label`` intact.
    """
    out: dict[str, Any] = {
        "id": _str(q, "id", limit=128),
        "kind": _str(q, "kind", limit=32) or "text",
        "prompt": _str(q, "prompt", limit=_MAX_DETAIL),
        "required": q.get("required") is True,
        "suggested": _bounded(q.get("suggested"), 2),
    }
    for key in ("why", "family", "proposal", "media", "doc_id"):
        text = _str(q, key, limit=_MAX_DETAIL if key in ("why", "proposal") else 128)
        if text:
            out[key] = text
    for key in ("multi", "multiline", "multiple"):
        if isinstance(q.get(key), bool):
            out[key] = q[key]
    options = q.get("options")
    if isinstance(options, list):
        rows = []
        for option in options[:32]:
            if isinstance(option, Mapping):
                row = {"value": _cap(option.get("value"), _MAX_LABEL)}
                label = _str(option, "label", limit=_MAX_LABEL)
                if label:
                    row["label"] = label
            else:
                row = {"value": _cap(option, _MAX_LABEL)}
            rows.append(row)
        out["options"] = rows
    for key in ("targets", "accept"):
        values = q.get(key)
        if isinstance(values, list):
            out[key] = [_cap(v, 64) for v in values[:16]]
    return out


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
    if _name(body.get("type")) in {"custom", "custom_message"}:
        custom_type = body.get("customType", body.get("custom_type"))
        if isinstance(custom_type, str) and custom_type.strip():
            return _name(custom_type), merged
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


def _tool_display_args(args: Any) -> tuple[Any, str]:
    """Unwrap CAD host-tool writes; read xd:// calls only consult tool docs."""
    if not isinstance(args, Mapping) or not _str(args, "path").startswith("xd://"):
        return args, ""
    if "content" not in args:
        return {}, "docs"
    content = args["content"]
    if isinstance(content, Mapping):
        return content, "call"
    if isinstance(content, str) and len(content) <= _MAX_TOOL_CONTENT:
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(parsed, Mapping):
                return parsed, "call"
    return {}, "call"


def _tool_target(args: Any, data: Mapping[str, Any], xd_kind: str = "") -> str:
    if xd_kind == "docs":
        return "docs"
    explicit = _str(data, "target", "label", limit=120) if not xd_kind else ""
    if explicit:
        return explicit
    if isinstance(args, Mapping):
        tool = _name(_str(data, "toolName", "tool_name", "tool", limit=160))
        if tool in {"read", "fetch_cad_model"}:
            keys = ("url", "path", "name", "query")
        elif tool == "web_search" or tool.startswith("research_") or tool.endswith("_search"):
            keys = ("query", "url", "name", "path")
        elif tool.startswith("fs_"):
            keys = ("path", "file_path", "folder_path", "name")
        else:
            keys = (
                "name", "part_name", "assembly_name", "feature_name", "title",
                "query", "url", "file_path", "folder_path", "path", "doc_id",
                "feature", "feature_id", "id",
            )
        for key in keys:
            value = args.get(key)
            if isinstance(value, (str, int, float)) and str(value):
                return _cap(value, 120)
        family = _str(args, "family", limit=80)
        size = _str(args, "size", limit=80)
        if family or size:
            return _cap(" · ".join(part for part in (family, size) if part), 120)
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
        text = "Running " + tool
    return _status(link, "tool", text, tool_name=tool, tool_call_id=call_id)


def _result_text(result: Any) -> str:
    """Text of a Chudcode ``{content:[{type:"text",text}]}`` tool result.

    A failed builtin (unknown tool, a call skipped for a queued steer, a thrown
    tool error) reports its reason only here, with ``isError`` beside it.
    """
    if isinstance(result, str):
        return _cap(result, _MAX_PREVIEW)
    if not isinstance(result, Mapping) or not isinstance(result.get("content"), list):
        return ""
    parts = [
        item["text"]
        for item in result["content"][:8]
        if isinstance(item, Mapping)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return _cap("\n".join(parts).strip(), _MAX_PREVIEW)


def cad_result_payload(result: Any) -> Mapping[str, Any] | None:
    """The CAD tool's own structured result inside a Chudcode tool result.

    A direct host-tool call carries it as ``details``; ``write xd://<tool>`` nests
    it as ``details.xdev.inner``. Both reach Chudcode with ``isError`` unset for a
    CAD ``{"ok": false, ...}`` result, which is a normal return, not an exception.
    """
    if not isinstance(result, Mapping):
        return None
    details = result.get("details")
    if isinstance(details, Mapping) and isinstance(details.get("xdev"), Mapping):
        details = details["xdev"].get("inner")
    return details if isinstance(details, Mapping) else None


def _cad_failure(result: Any) -> Mapping[str, Any] | None:
    payload = cad_result_payload(result)
    return payload if payload is not None and payload.get("ok") is False else None


def _cad_failure_outcome(failure: Mapping[str, Any]) -> str:
    """``outcome_kind`` for a CAD ``ok:false`` payload (the feed's one vocabulary)."""
    status = _name(_str(failure, "status"))
    if status == "guardrail_blocked":
        return "guard_block"
    if failure.get("policy_refusal") is True:
        return "policy_refusal"
    if failure.get("timed_out") is True:
        return "timeout_unknown"
    try:
        from services.agent.tool_loop_guard import VALIDATION_FAILURE_STATUSES
    except Exception:  # noqa: BLE001 - classification is best-effort
        VALIDATION_FAILURE_STATUSES = frozenset()
    if status in VALIDATION_FAILURE_STATUSES:
        return "validation_error"
    return "tool_error"



def _tool_end(
    event: str,
    data: Mapping[str, Any],
    context: Mapping[str, Any],
    saved: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    saved = saved or {}
    link = _link(data, context, event)
    call_id = _str(data, "toolCallId", "tool_call_id", "callId", "call_id")
    if call_id:
        link["id"] = call_id
    if saved.get("parent_id") and not _str(
        data, "parentToolCallId", "parent_tool_call_id", "parentId", "parent_id"
    ):
        link["parent_id"] = saved["parent_id"]
    tool = (
        _str(data, "toolName", "tool_name", "tool", "name", limit=160)
        or saved.get("tool_name")
        or "tool"
    )
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
    failure = _cad_failure(result) if ok else None
    if failure is not None:
        ok = False
        error = error or _str(failure, "error", "reason", "status", limit=_MAX_PREVIEW)
    display_args, xd_kind = _tool_display_args(args)
    args_preview = (
        "" if xd_kind else _str(data, "argsPreview", "args_preview", limit=_MAX_PREVIEW)
    )
    if (xd_kind == "call" or not args_preview) and display_args not in (None, {}, []):
        args_preview = _cap(_bounded(display_args), _MAX_PREVIEW)
    args_preview = args_preview or saved.get("args_preview", "")
    target = _tool_target(display_args, data, xd_kind) or saved.get("target", "")
    payload: dict[str, Any] = {
        "tool_name": tool,
        "target": target,
        "args_digest": _str(data, "argsDigest", "args_digest", limit=64)
        or (saved.get("args_digest") if args in (None, {}, []) else None)
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
    intent = _str(data, "intent", limit=_MAX_LABEL) or saved.get("intent", "")
    if intent:
        payload["intent"] = intent
    if not ok:
        payload["error_preview"] = (
            error
            or _result_text(result)
            or ("cancelled" if cancelled else "tool failed")
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
    elif failure is not None:
        payload["outcome_kind"] = _cad_failure_outcome(failure)
    executed = _bool(data, "executed")
    if executed is None and failure is not None:
        executed = _bool(failure, "executed")
    if executed is not None:
        payload["executed"] = executed
    bounded: list[dict[str, str]] = []
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts[:_MAX_ARTIFACTS]:
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
    # Visual evidence the tool returned to the model (renders, section/clash
    # views) rides as path references, never inline bytes; failed geometry
    # checks carry diagnostic images too.
    evidence = cad_result_payload(result)
    images = tool_images.image_refs(evidence) if evidence is not None else []
    seen = {item["path"] for item in bounded}
    took_image = False
    for ref in images:
        path = ref.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        path = _cap(path, _MAX_PREVIEW)
        if path in seen:
            took_image = took_image or any(
                item["path"] == path and item["kind"] == "image" for item in bounded
            )
            continue
        if len(bounded) >= _MAX_ARTIFACTS:
            break
        seen.add(path)
        bounded.append({"path": path, "kind": "image"})
        took_image = True
    if bounded:
        payload["artifacts"] = bounded
    caption = evidence.get("caption") if took_image and evidence is not None else None
    if isinstance(caption, str) and caption.strip():
        payload["image_caption"] = _cap(caption.strip(), _MAX_PREVIEW)
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
    name = _str(data, "name", limit=160)
    if link.get("subagent_id") and name:
        payload["name"] = name
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
        "purpose": _cap("Agent · " + label.replace("_", " "), 120),
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
    data: Mapping[str, Any], context: Mapping[str, Any], state: RpcFrameState | None
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
        "messageId",
        "message_id",
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
    if _name(child.get("type")) == "start":
        return _response_restarted(child, context)
    return map_rpc_frame(child, context, state)


def _response_restarted(
    data: Mapping[str, Any], context: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """A second ``start`` inside one assistant message: the provider request restarted.

    Chudcode turns a response's first ``start`` into ``message_start``; a later one
    means the stream was recovered (dropped/stalled connection, steer commit) and
    the partial output was discarded. It used to surface as an "Agent · start" row
    after minutes of silence.
    """
    ids = {
        key: data[key]
        for key in ("id", "messageId", "message_id", "runId", "run_id", "turnId", "turn_id")
        if key in data
    }
    link = _link(ids, context, "response_restart")
    row = _linked(
        link,
        id="response-restart:" + link["id"],
        purpose="Model response restarted",
        status="retry",
        detail="The provider stream was interrupted; the request was re-sent and its "
        "partial output discarded.",
    )
    return [
        ("sub_run", row),
        _status(link, "response_restart", "Waiting for model (response restarted)"),
    ]


_COMPACTION_TRIGGERS = {
    "overflow": " (window overflowed)",
    "idle": " (idle cleanup)",
    "incomplete": " (recovering an incomplete reply)",
}


def _compaction(
    event: str,
    data: Mapping[str, Any],
    context: Mapping[str, Any],
    link: Mapping[str, Any],
    state: RpcFrameState | None,
) -> tuple[str, dict[str, Any]]:
    """One ``sub_run`` per compaction, upserted from its start to its end frame.

    Chudcode's compaction frames carry no shared id, and their ``reason``
    (``threshold``) / ``action`` (``context-full``) are enum values. Until
    2026-10-04 the feed showed a "threshold" row that never closed beside a
    separate "context-full" row, and nothing said the agent was compacting.
    """
    if event.endswith(("start", "started")):
        phase, status = "start", "running"
    elif event.endswith(("update", "progress")):
        phase, status = "update", "running"
    elif event.endswith("failed") or data.get("errorMessage"):
        phase, status = "end", "fail"
    elif _bool(data, "aborted") is True:
        phase, status = "end", "cancelled"
    elif _bool(data, "skipped") is True:
        phase, status = "end", "skipped"
    else:
        phase, status = "end", "done"
    item_id = _str(data, "compactionId", "compaction_id", "id", limit=160)
    if not item_id:
        base = "compaction:" + (_first(data, context, "run_id", "runId") or link["node_id"])
        item_id = state.compaction_id(base, phase) if state is not None else base
    verb = "Auto-compacting" if event.startswith("auto_") else "Compacting"
    if status == "running":
        trigger = _name(data.get("reason"))
        purpose = f"{verb} context" + _COMPACTION_TRIGGERS.get(trigger, "")
    elif status == "done":
        purpose = "Context compacted"
        result = data.get("result")
        before = _int(result, "tokensBefore", "tokens_before") if isinstance(result, Mapping) else 0
        if before > 0:
            purpose += f" (was {before / 1000:.1f}k tokens)"
    else:
        purpose = f"Context compaction {'failed' if status == 'fail' else status}"
    if status != "running" and _bool(data, "willRetry", "will_retry") is True:
        purpose += " \u00b7 retrying the request"
    row = _linked(link, id=item_id, purpose=purpose, status=status)
    error = _str(data, "errorMessage", "error", limit=_MAX_DETAIL)
    if error:
        row["detail"] = error
    return "sub_run", row


def _remember_tool_call(tool_call: Any, state: RpcFrameState | None) -> None:
    if state is None or not isinstance(tool_call, Mapping):
        return
    call_id = _str(tool_call, "toolCallId", "id")
    if not call_id:
        return
    normalized = dict(tool_call)
    normalized["toolName"] = _str(tool_call, "toolName", "name")
    state.remember(call_id, normalized)


_SUBAGENT_STATUS = {
    "pending": "running",
    "started": "running",
    "running": "running",
    "completed": "done",
    "failed": "fail",
    "aborted": "fail",
}
_SUBAGENT_TOOL_EVENTS = frozenset(
    {"tool_execution_start", "tool_execution_update", "tool_execution_end"}
)


def _subagent_frame(
    kind: str,
    frame: Mapping[str, Any],
    ctx: Mapping[str, Any],
    state: RpcFrameState | None,
) -> list[tuple[str, dict[str, Any]]] | None:
    """Map Chudcode's ``subagent_*`` subscription frames with their lineage.

    ``subagent_lifecycle``/``subagent_progress`` upsert ONE ``sub_run`` per task
    sub-agent (id = the sub-agent id, parent = the spawning ``task`` call).
    ``subagent_event`` wraps the sub-agent's own AgentSessionEvent: its tool
    frames map exactly like the parent's, stamped with ``subagent_id`` and the
    ``task`` call as ``parent_id`` so the execution trace nests them; its text,
    thinking and lifecycle frames are the sub-agent's private transcript and
    must never render as the parent's reply. ``None`` falls back to the generic
    mapping for a shape this does not recognize.
    """
    payload = frame.get("payload")
    if not isinstance(payload, Mapping):
        return None
    if kind == "subagent_event":
        subagent_id = _str(payload, "id", limit=160)
        inner = payload.get("event")
        if not subagent_id or not isinstance(inner, Mapping):
            return None
        if _name(inner.get("type")) not in _SUBAGENT_TOOL_EVENTS:
            return []
        parent = state.subagent_parent(subagent_id) if state is not None else ""
        mapped = map_rpc_frame(inner, ctx, state)
        for _kind, body in mapped:
            body["subagent_id"] = subagent_id
            if parent:
                body["parent_id"] = parent
        return mapped
    if kind == "subagent_progress":
        progress = payload.get("progress")
        if not isinstance(progress, Mapping):
            return None
        body: Mapping[str, Any] = {**payload, **progress}
    else:
        body = payload
    subagent_id = _str(body, "id", limit=160)
    if not subagent_id:
        return None
    parent = _str(body, "parentToolCallId", "parent_tool_call_id", limit=160)
    if state is not None:
        parent = parent or state.subagent_parent(subagent_id)
        state.remember_subagent(subagent_id, parent)
    raw_status = _name(_str(body, "status"))
    # The id IS the spawner-assigned task name (`tasks[].name`, `Parent.Child` when
    # nested); `agent` is only the definition type ("task", "scout") and
    # `description` a late tiny-model label, so neither may stand in for the name.
    description = _str(body, "description", limit=240)
    work: dict[str, Any] = {
        "subagent_id": subagent_id,
        "purpose": description or subagent_id,
    }
    if parent:
        work["parent_tool_call_id"] = parent
    session_file = _str(body, "sessionFile", limit=_MAX_PREVIEW)
    if session_file:
        work["sessionFile"] = session_file
    detail = "aborted" if raw_status == "aborted" else _str(body, "lastIntent")
    if detail:
        work["detail"] = detail
    event, row = _subrun(work, ctx, kind, _SUBAGENT_STATUS.get(raw_status, "running"))
    row["name"] = subagent_id
    agent_type = _str(body, "agent", limit=120)
    if agent_type:
        row["agent_type"] = agent_type
    if description:
        row["description"] = description
    return [(event, row)]



# Chudcode IRC record customTypes -> agent.message channel. `irc:incoming` is a
# message delivered to the session whose stream carries it (the top-level stream
# is the main agent's); `irc:relay` mirrors sub-agent <-> sub-agent traffic onto
# the main session; `irc:workpool` is a workpool dispatch card.
_IRC_CHANNELS = {"irc:incoming": "irc", "irc:relay": "irc", "irc:workpool": "pool"}
_IRC_MAIN_AGENT = "Main"   # Chudcode MAIN_AGENT_ID
_IRC_HEADER = re.compile(r"^\[(?:IRC|pool)[^\]\n]{0,300}\]\s*", re.IGNORECASE)


def _irc_message(
    data: Mapping[str, Any], link: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """One ``irc_message`` frame as an ``agent.message`` transcript row.

    Display-only forwarding of inter-agent traffic: sender, recipient and the
    message body (never the rendered prompt wrapper around it).
    """
    message = data.get("message")
    if not isinstance(message, Mapping):
        return []
    raw_details = message.get("details")
    details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
    custom = _str(message, "customType", limit=64)
    content = _str(message, "content", limit=_MAX_DETAIL)
    sender = _str(details, "from", limit=120)
    if not sender:
        found = re.search(r"from agent `([^`]{1,120})`", content)
        sender = found.group(1) if found else ""
    recipient = _str(details, "to", limit=120)
    if not recipient and custom == "irc:incoming":
        recipient = _IRC_MAIN_AGENT
    text = _str(details, "message", "body", limit=_MAX_DETAIL)
    if not text:
        text = _IRC_HEADER.sub("", content, count=1).strip()
    if not text:
        return []
    payload = _linked(
        link,
        channel=_IRC_CHANNELS.get(custom, "irc"),
        to=recipient,
        text=text,
    )
    payload["from"] = sender or "agent"
    message_id = _str(details, "id", limit=160)
    if message_id:
        payload["message_id"] = message_id
    reply_to = _str(details, "replyTo", "reply_to", limit=160)
    if reply_to:
        payload["reply_to"] = reply_to
    mode = _str(details, "mode", limit=32)
    if mode:
        payload["mode"] = mode
    return [("agent.message", payload)]


def map_rpc_frame(
    frame: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    state: RpcFrameState | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Map one raw ``AgentSessionEvent`` to ordered Chudware feed shapes."""
    ctx: Mapping[str, Any] = context if isinstance(context, Mapping) else {}
    if not isinstance(frame, Mapping):
        return [_unknown("invalid_frame", {"value": _bounded(frame)}, ctx)]
    kind = _name(frame.get("type"))
    if kind in {"subagent_event", "subagent_lifecycle", "subagent_progress"}:
        mapped = _subagent_frame(kind, frame, ctx, state)
        if mapped is not None:
            return mapped
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
        if event in {"message_end", "message_completed", "message_complete"}:
            content = data["message"].get("content")
            if isinstance(content, list):
                for item in content[:32]:
                    if isinstance(item, Mapping) and _name(item.get("type")) == "toolcall":
                        _remember_tool_call(item, state)
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
        return _message_update(data, ctx, state)

    # RPC control snapshots and prompt correlations are not agent feed activity.
    # In particular, prompt_result follows agent_end and must not create a
    # second completion row; session_settled can arrive after background jobs.
    if event in {
        "available_commands_update",
        "available_commands_updated",
        "credential_pin",
        "credential_unpin",
        "eager_todo_prelude",
        "prompt_result",
        "session_settled",
        "queue_update",
    }:
        return []
    if event == "session_exit":
        return [_status(link, "session_end", "Session exited")]
    if event == "title_change":
        title = _str(data, "title", limit=_MAX_LABEL)
        return [
            _status(link, "title_change", title or "Conversation title changed", title=title)
        ]
    if event in {"notice", "agent_progress_nudge"}:
        text = _str(data, "text", "message", "content", limit=_MAX_LABEL)
        if not text:
            return []
        if event == "notice":
            return [_status(link, "notice", text)]
        # reason "turn_budget" marks the pre-expiry reminder of a budgeted turn.
        return [_status(link, "progress_nudge", text, reason=_str(data, "reason"),
                        budget_s=data.get("budget_s"), remaining_s=data.get("remaining_s"))]
    if event == "irc_message":
        return _irc_message(data, link)
    if event == "agent_question":
        questions = [
            _question_payload(q)
            for q in (data.get("questions") or [])[:8]
            if isinstance(q, Mapping)
        ]
        batch_id = _str(data, "batch_id", "batchId", limit=128)
        if not batch_id or not questions:
            return []
        payload = _linked(
            link,
            batch_id=batch_id,
            prompt=questions[0]["prompt"],
            questions=questions,
        )
        agent_id = _str(data, "agent_id", "agentId", limit=128)
        if agent_id:
            payload["agent_id"] = agent_id
        return [("agent.question", payload)]
    if event == "agent_answer":
        batch_id = _str(data, "batch_id", "batchId", limit=128)
        if not batch_id:
            return []
        payload = _linked(
            link,
            batch_id=batch_id,
            source=_str(data, "source", limit=32) or "cancelled",
        )
        answers = data.get("answers")
        if isinstance(answers, Mapping) and answers:
            payload["answers"] = _bounded(answers, 1)
        reply = _str(data, "reply", limit=_MAX_DETAIL)
        if reply:
            payload["reply"] = reply
        events = [("agent.answer", payload)]
        steer_id = _str(data, "steer_id", "steerId", limit=128)
        if payload["source"] == "reply" and steer_id:
            events.append(("agent.steer", _linked(link, steer_id=steer_id, status="delivered")))
        return events
    if event == "agent_steer":
        steer_id = _str(data, "steer_id", "steerId", limit=128)
        status = _str(data, "status", limit=32)
        if not steer_id or status not in {"delivered", "carried"}:
            return []
        return [("agent.steer", _linked(link, steer_id=steer_id, status=status))]

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
    if event == "toolcall_end":
        _remember_tool_call(data.get("toolCall"), state)
    if event in {
        "text_start",
        "text_end",
        "thinking_start",
        "thinking_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        # Streamed tool arguments (edit previews): the tool_execution_* lifecycle owns the row.
        "tool_stream_update",
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
        # The provider request is in flight from turn_start until its first delta;
        # the chat's quiet-wait line shows this text with the elapsed silence.
        "turn_start": ("turn_start", "Waiting for model"),
        "turn_started": ("turn_start", "Waiting for model"),
        "message_start": ("message_start", "Waiting for model"),
        "message_started": ("message_start", "Waiting for model"),
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
    if event in tool_start | tool_update | tool_end | tool_cancel:
        call_id = _str(data, "toolCallId", "tool_call_id", "callId", "call_id")
        if not call_id and event.startswith("host_tool"):
            call_id = _str(data, "id")
        if state is not None and state.terminal_seen(call_id):
            return []
        # A synchronous host call carrying a result is already complete.
        terminal = event in tool_end | tool_cancel or (
            event == "host_tool_call" and any(
                key in data for key in ("result", "output", "isError")
            )
        )
        if terminal:
            saved = state.complete(call_id) if state is not None else None
            return [_tool_end(event, data, ctx, saved)]
        if state is not None and event in tool_start:
            state.remember(call_id, data)
        return [_tool_progress(event, data, ctx)]

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
        "thinking_level_change",
        "thinking_level_changed",
        "thinking_mode",
        "reasoning_effort_changed",
        "reasoning_change",
    }:
        mode = _str(
            data,
            "thinkingLevel",
            "thinking_level",
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
    if event == "session_bound":
        # Resume key for the GUI's conversation link, sent when the turn starts: a turn that
        # never returns (daemon/worker death) must still leave its session resumable.
        payload = {
            "chudcode_session_id": _str(data, "sessionId", "session_id", limit=256),
            "chudcode_session_file": _str(data, "sessionFile", "session_file", limit=4_096),
        }
        if not payload["chudcode_session_file"]:
            return []
        return [("agent.session", _linked(link, **payload))]
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
        return [_compaction(event, data, ctx, link, state)]

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
            "role": _str(data, "role", "agent", limit=120) or "worker",
            "spawn_kind": _str(data, "spawnKind", "spawn_kind", limit=80) or "worker",
            "brief": _str(data, "brief", "task", "purpose", limit=120),
            "budget_tokens": _int(
                data, "budgetTokens", "budget_tokens", "tokenBudget", "token_budget"
            ),
        }
        name = _str(data, "name", "title", limit=160)
        if name:
            payload["name"] = name
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


__all__ = ["RpcFrameState", "map_rpc_frame"]
