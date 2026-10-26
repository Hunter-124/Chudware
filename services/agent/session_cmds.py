"""Session command backends for persistent Chudcode and explicit offline use.

Online compaction and retry state are delegated to the live Chudcode session.
Deterministic transcript transforms remain available only when a caller opts
into standalone mode or Chudware is explicitly offline. Inputs are never mutated.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

# ---------------------------------------------------------------------------
# Handoff prompt
# ---------------------------------------------------------------------------

_HANDOFF_SYSTEM = (
    "You write handoff documents for another instance of yourself. "
    "Output ONLY the handoff document. No preamble, no commentary, no wrapper text. "
    "Capture exact technical state (paths, symbols, commands, test results), not abstractions. "
    "Do not call tools."
)

_HANDOFF_PROMPT_BODY = """\
<critical>
Write a handoff document for another instance of yourself.
The handoff MUST be sufficient for seamless continuation without access to this conversation.
Output ONLY the handoff document. No preamble, no commentary, no wrapper text.
</critical>

<instruction>
Capture exact technical state, not abstractions.
- File paths, symbol names, commands run
- Test results, observed failures
- Decisions made
- Partial work affecting the next step
</instruction>

<output>
Use exactly this structure:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]

## Progress
### Done
- [x] [Completed tasks with specifics]

### In Progress
- [ ] [Current work if any]

### Pending
- [ ] [Tasks mentioned but not started]

## Key Decisions
- **[Decision]**: [Rationale]

## Critical Context
- Code snippets, file paths, function/type names, error messages, data essential to continue
- Repository state if relevant

## Next Steps
1. [What should happen next]
</output>
"""

# ---------------------------------------------------------------------------
# /btw side-channel prompts
# ---------------------------------------------------------------------------

_BTW_NO_TOOLS_REMINDER = (
    "<system-reminder>\n"
    "This is an ephemeral side-channel turn that reuses the current conversation's "
    "context. Tools are NOT available on this turn. Do NOT emit any tool call; reply "
    "with plain text only. Answer briefly and directly. NEVER ask follow-up questions. "
    "Any tool call you produce is discarded without executing.\n"
    "</system-reminder>"
)

_BTW_SYSTEM = (
    "You answer brief side questions about the current session. "
    "No tools. No follow-up questions. Plain text only."
)


def _btw_user_prompt(question: str) -> str:
    return (
        "<btw>\n"
        "This is an ephemeral side question for the current interactive session.\n"
        "Answer briefly and directly using the conversation context already provided.\n"
        "NEVER use tools.\n"
        "NEVER ask follow-up questions.\n"
        "Question:\n"
        f"{question}\n"
        "</btw>"
    )


def _handoff_user_prompt(focus: str | None = None) -> str:
    body = _HANDOFF_PROMPT_BODY
    focus = (focus or "").strip()
    if focus:
        body = (
            body
            + "\n<instruction>\n"
            + f"Additional focus: {focus}\n"
            + "</instruction>\n"
        )
    return body


# ---------------------------------------------------------------------------
# Message helpers — never mutate caller input
# ---------------------------------------------------------------------------


def _snapshot_messages(messages: list | None) -> list[dict]:
    """Deep-copy messages so oneshots / engines cannot mutate the host store."""
    if not messages:
        return []
    return copy.deepcopy(list(messages))


def _as_text_message(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _normalize_message(msg: Any) -> dict | None:
    """Coerce host ChatMsg-ish or provider dicts into {role, content}."""
    if not isinstance(msg, dict):
        return None
    # Provider form
    role = msg.get("role")
    if isinstance(role, str) and role:
        content = msg.get("content")
        if content is None and "text" in msg:
            content = [{"type": "text", "text": str(msg.get("text") or "")}]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if content is None:
            content = []
        out = {"role": role, "content": content}
        # Preserve optional fields used by compaction (tool meta, etc.)
        for k in ("name", "tool_call_id", "tool_calls", "id"):
            if k in msg:
                out[k] = msg[k]
        return out
    # Host chat_history form: {u, t, e, m?} or ChatMsg {text, ...}
    text = msg.get("text") or msg.get("u") or msg.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    # t: "user" | "assistant" | "ai" | "system" | ...
    t = str(msg.get("t") or msg.get("kind") or msg.get("type") or "").lower()
    if t in ("user", "u", "human"):
        role = "user"
    elif t in ("assistant", "ai", "a", "bot", "model"):
        role = "assistant"
    elif t in ("system", "sys", "developer"):
        role = "system"
    elif t in ("tool", "function"):
        role = "tool"
    else:
        # Heuristic: empty type with text → user if no model tag, else assistant
        role = "assistant" if msg.get("model") or msg.get("m") else "user"
    if not text and role == "user":
        return None
    return _as_text_message(role, text)


def _normalize_messages(messages: list | None) -> list[dict]:
    out: list[dict] = []
    for m in messages or []:
        nm = _normalize_message(m)
        if nm is not None:
            out.append(nm)
    return out


# ---------------------------------------------------------------------------
# One-shot role helpers
# ---------------------------------------------------------------------------


def _role_turn_text(turn: dict[str, Any], command: str) -> str:
    if not isinstance(turn, dict):
        raise SessionCommandUnavailable(f"{command} returned a non-object response")
    if turn.get("success") is not True:
        nested = turn.get("result") if isinstance(turn.get("result"), dict) else {}
        reason = str(
            nested.get("error")
            or nested.get("error_detail")
            or turn.get("stop_reason")
            or f"{command} failed"
        )
        raise SessionCommandUnavailable(reason)
    result = turn.get("result")
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        handoff = result.get("handoff")
        if isinstance(handoff, dict):
            for key in ("document", "summary", "text", "response"):
                value = handoff.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("document", "answer", "text", "response", "summary"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _run_one_shot_role(
    *,
    command: str,
    request: str,
    messages: list[dict],
    session_id: str | None,
    provider: Any,
    model: str | None,
    max_tokens: int,
    standalone: bool,
) -> str:
    from services.agent.chudcode_roles import run_role_turn  # noqa: PLC0415

    local = _local_compaction(standalone)
    parent_session = str(session_id or "").strip()
    if not parent_session:
        if not local:
            raise SessionCommandUnavailable(
                f"{command} requires a live agent session_id"
            )
        parent_session = f"offline-{command}"
    settings: dict[str, Any] = {}
    if isinstance(provider, str) and provider.strip():
        settings["provider"] = provider.strip()
    if model and str(model).strip():
        settings["model"] = str(model).strip()
    options: dict[str, Any] = {
        "allowed_tools": [],
        "settings": settings,
        "offline": local,
    }
    if not local:
        from services.agent.chudcode_host import get_default_chudcode_host
        host = get_default_chudcode_host()
        inherit = getattr(host, "session_launch_params", None)
        if callable(inherit):
            try:
                options = {**inherit(parent_session), **options}
            except KeyError as exc:
                from services.agent.request_context import in_cell
                if in_cell():
                    raise SessionCommandUnavailable("not_found") from exc
        options["mode"] = "ask"
    if local and provider is not None and not isinstance(provider, str):
        options["offline_provider"] = provider
    turn = run_role_turn(
        request,
        "general_agent",
        session_id=parent_session,
        run_id=f"{parent_session}:{command}",
        parent_id=f"{parent_session}:{command}",
        options=options,
        history=messages,
        max_tokens=max_tokens,
        max_total_tokens=max_tokens * 2,
        close_session=True,
    )
    return _role_turn_text(turn, command)


class SessionCommandUnavailable(RuntimeError):
    """A command requiring a live Chudcode session could not be served."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _offline_enabled() -> bool:
    try:
        from services.agent.config import offline_enabled  # noqa: PLC0415

        return offline_enabled()
    except Exception:  # noqa: BLE001 - unreadable config is not an offline opt-in
        return False


def _local_compaction(standalone: bool) -> bool:
    return bool(standalone) or _offline_enabled()


def _chudcode_session_call(
    session_id: str | None,
    command: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = str(session_id or "").strip()
    if not key:
        raise SessionCommandUnavailable(
            f"{command} requires a live agent session_id; "
            "pass standalone=True only for explicit local/offline use"
        )
    try:
        from services.agent.chudcode_host import (  # noqa: PLC0415
            get_default_chudcode_host,
        )

        result = get_default_chudcode_host().session_call(key, command, params)
    except Exception as exc:  # noqa: BLE001 - process boundary
        code = getattr(exc, "code", None)
        if code is None:
            code = exc.args[0] if isinstance(exc, KeyError) and len(exc.args) == 1 else str(exc)
        raise SessionCommandUnavailable(
            f"Agent session {command} unavailable: {exc}", code=code,
        ) from exc
    if not isinstance(result, dict):
        raise SessionCommandUnavailable(
            f"Agent session {command} returned a non-object response"
        )
    if result.get("ok") is False:
        reason = str(result.get("reason") or result.get("error") or "request failed")
        raise SessionCommandUnavailable(
            f"Agent session {command} unavailable: {reason}",
            code=result.get("code") or reason,
        )
    return result


def set_auto_compaction(session_id: str, enabled: bool) -> dict[str, Any]:
    """Set Chudcode's per-session automatic compaction authority."""
    return _chudcode_session_call(
        session_id, "set_auto_compaction", {"enabled": bool(enabled)}
    )


def set_auto_retry(session_id: str, enabled: bool) -> dict[str, Any]:
    """Set Chudcode's per-session retry/cache policy."""
    return _chudcode_session_call(
        session_id, "set_auto_retry", {"enabled": bool(enabled)}
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def handoff_document(
    messages: list,
    focus: str | None = None,
    provider: Any = None,
    model: str | None = None,
    *,
    session_id: str | None = None,
    standalone: bool = False,
) -> str:
    """Generate a handoff from the authoritative live Chudcode session.

    Online mode calls the session's native ``handoff`` side request so it sees
    Chudcode's persisted/compacted context and selected provider.  Explicit
    offline/standalone mode uses one bounded offline-safe role turn.  Caller
    messages are snapshotted and never mutated.
    """
    history = _normalize_messages(_snapshot_messages(messages))
    if _local_compaction(standalone):
        request = _HANDOFF_SYSTEM + "\n\n" + _handoff_user_prompt(focus)
        return _run_one_shot_role(
            command="handoff",
            request=request,
            messages=history,
            session_id=session_id,
            provider=provider,
            model=model,
            max_tokens=4096,
            standalone=True,
        )

    payload: dict[str, Any] = {}
    note = str(focus or "").strip()
    if note:
        payload["customInstructions"] = note
    result = _chudcode_session_call(session_id, "handoff", payload)
    for key in ("document", "handoff", "text", "summary", "result"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("document", "text", "summary"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    raise SessionCommandUnavailable(
        "Agent session handoff returned no document text"
    )


def btw_answer(
    messages: list,
    question: str,
    provider: Any = None,
    model: str | None = None,
    *,
    session_id: str | None = None,
    standalone: bool = False,
) -> str:
    """Answer one bounded side question without mutating the parent session."""
    q = (question or "").strip()
    if not q:
        return ""
    history = _normalize_messages(_snapshot_messages(messages))
    request = "\n\n".join(
        (
            _BTW_SYSTEM,
            _BTW_NO_TOOLS_REMINDER,
            _btw_user_prompt(q),
        )
    )
    return _run_one_shot_role(
        command="btw",
        request=request,
        messages=history,
        session_id=session_id,
        provider=provider,
        model=model,
        max_tokens=2048,
        standalone=standalone,
    )


def compact_session(
    messages: list,
    strategy: str = "off",
    *,
    keep_recent_tokens: int = 20000,
    supersede_reads: bool = True,
    drop_useless: bool = True,
    prior_archive: dict | None = None,
    standalone: bool = False,
) -> dict:
    """Compact a transcript locally, only for explicit offline/standalone use.

    Online conversations compact on their own Chudcode session through the
    ``agent_admin_turn`` compaction run, never here.
    """
    if not _local_compaction(standalone):
        raise SessionCommandUnavailable(
            "local session compaction is unavailable for online agent sessions"
        )
    msgs = _snapshot_messages(messages)
    strat = (strategy or "off").strip().lower()
    if strat in ("off", "", "none", "disabled"):
        return {
            "kept_messages": msgs,
            "summary": "",
            "short_summary": "",
        }

    comp = importlib.import_module("services.agent.compaction")
    if strat == "shake":
        result = comp.shake_messages(msgs, mode="elide", aggressive=False)
        return {
            "kept_messages": result.get("messages", msgs),
            "summary": "",
            "short_summary": "",
            "saved_tokens": int(result.get("saved_tokens") or 0),
        }

    result = comp.snapcompact_archive(
        msgs,
        keep_recent_tokens=int(keep_recent_tokens),
        prior_archive=prior_archive,
        supersede_reads=bool(supersede_reads),
        drop_useless=bool(drop_useless),
    )
    out: dict[str, Any] = {
        "kept_messages": result.get("kept_messages", msgs),
        "summary": result.get("summary") or "",
        "short_summary": result.get("short_summary") or "",
    }
    if result.get("archive") is not None:
        out["archive"] = result["archive"]
    if "cut_index" in result:
        out["cut_index"] = result["cut_index"]
    out["messages"] = comp.reinject_archive(result) + out["kept_messages"]
    return out


def shake_session(
    messages: list,
    *,
    mode: str = "elide",
    aggressive: bool = False,
    keep_boundary_index: int | None = None,
    standalone: bool = False,
) -> dict:
    """Mechanically elide a transcript only for explicit offline/standalone use."""
    if not _local_compaction(standalone):
        raise SessionCommandUnavailable(
            "local session shake is unavailable for online agent sessions"
        )
    msgs = _snapshot_messages(messages)
    comp = importlib.import_module("services.agent.compaction")

    result = comp.shake_messages(
        msgs,
        mode=mode or "elide",
        aggressive=bool(aggressive),
        keep_boundary_index=keep_boundary_index,
    )
    saved = int(result.get("saved_tokens") or 0)
    placeholders = int(result.get("placeholders") or 0)
    regions = result.get("regions") or []
    if saved > 0 or placeholders > 0:
        notice = (
            f"Shook session: ~{saved} tokens saved"
            f" ({placeholders} placeholder(s), {len(regions)} region(s))."
        )
    else:
        notice = "Shake found nothing to elide."
    return {
        "messages": result.get("messages", msgs),
        "saved_tokens": saved,
        "notice": notice,
        "regions": regions,
        "placeholders": placeholders,
    }


__all__ = [
    "SessionCommandUnavailable",
    "handoff_document",
    "btw_answer",
    "compact_session",
    "shake_session",
    "set_auto_compaction",
    "set_auto_retry",
]
