"""Session command backends: /handoff, /btw, compact, shake.

Host-facing pure transforms. The host passes its transcript as ``messages[]``;
we return a result the host applies to its store. Inputs are never mutated.

RPCs live in ``engine_oracle.parts_hub.session_rpc`` and call these functions.
"""
from __future__ import annotations

import copy
import importlib
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Handoff prompt — structure LITERALLY from omp handoff-document.md
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
# /btw side-channel prompts — from omp btw-user.md + side-channel-no-tools.md
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
# Provider resolution (oneshot complete)
# ---------------------------------------------------------------------------

def _resolve_provider(provider: Any = None, model: str | None = None):
    """Return a Provider with ``.complete(system, messages, max_tokens=...)``.

    *provider* may be:
      - a Provider instance (tests inject mocks this way)
      - a provider name string (``\"xai\"``, ``\"anthropic\"``, …)
      - None → config default via ``get_provider``
    """
    if provider is not None and hasattr(provider, "complete"):
        return provider
    name = provider if isinstance(provider, str) and provider.strip() else None
    cfg: dict = {}
    if model:
        cfg["model"] = model
    try:
        from agent import config as agent_config  # type: ignore
    except Exception:  # noqa: BLE001
        from services.agent import config as agent_config  # type: ignore
    return agent_config.get_provider(name=name, cfg=cfg or None)


def _complete(provider: Any, system: str, messages: list[dict],
              *, max_tokens: int = 4096) -> str:
    """Oneshot completion; tool_choice is none by construction (no tools passed)."""
    text = provider.complete(system, messages, max_tokens=max_tokens)
    if isinstance(text, tuple):
        text = text[0]
    return (text or "").strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def handoff_document(
    messages: list,
    focus: str | None = None,
    provider: Any = None,
    model: str | None = None,
) -> str:
    """Generate a structured handoff document via oneshot LLM completion.

    Returns the document text only. New-session creation is host-side.
    Does **not** mutate *messages*.
    """
    history = _normalize_messages(_snapshot_messages(messages))
    prompt_msgs = history + [_as_text_message("user", _handoff_user_prompt(focus))]
    prov = _resolve_provider(provider, model)
    return _complete(prov, _HANDOFF_SYSTEM, prompt_msgs, max_tokens=4096)


def btw_answer(
    messages: list,
    question: str,
    provider: Any = None,
    model: str | None = None,
) -> str:
    """Ephemeral side-channel Q&A over a snapshot of *messages*.

    Never mutates *messages* / history. No tools. Brief answer only.
    """
    q = (question or "").strip()
    if not q:
        return ""
    history = _normalize_messages(_snapshot_messages(messages))
    # Append no-tools reminder + <btw> question (omp order)
    prompt_msgs = history + [
        _as_text_message("user", _BTW_NO_TOOLS_REMINDER),
        _as_text_message("user", _btw_user_prompt(q)),
    ]
    prov = _resolve_provider(provider, model)
    return _complete(prov, _BTW_SYSTEM, prompt_msgs, max_tokens=2048)


def compact_session(
    messages: list,
    strategy: str = "off",
    *,
    keep_recent_tokens: int = 20000,
    supersede_reads: bool = True,
    drop_useless: bool = True,
    prior_archive: dict | None = None,
) -> dict:
    """Thin wrapper over compaction engines.

    ``strategy`` ∈ ``off`` | ``snapcompact`` | ``shake`` | ``summary``.
    Returns ``{kept_messages, archive?, summary, short_summary}`` (plus
    optional ``saved_tokens`` for shake).
    """
    msgs = _snapshot_messages(messages)
    strat = (strategy or "off").strip().lower()
    if strat in ("off", "", "none", "disabled"):
        return {
            "kept_messages": msgs,
            "summary": "",
            "short_summary": "",
        }

    # importlib.import_module (not `from agent import compaction`): the
    # from-form short-circuits on the package attribute once the real module
    # was imported anywhere in the process, ignoring sys.modules patches in
    # tests. import_module consults sys.modules first.
    try:
        comp = importlib.import_module("agent.compaction")
    except Exception:  # noqa: BLE001
        comp = importlib.import_module("services.agent.compaction")

    if strat == "shake":
        result = comp.shake_messages(
            msgs, mode="elide", aggressive=False,
        )
        return {
            "kept_messages": result.get("messages", msgs),
            "summary": "",
            "short_summary": "",
            "saved_tokens": int(result.get("saved_tokens") or 0),
        }

    # snapcompact + summary share the archive engine (summary text from archive)
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
    return out


def shake_session(
    messages: list,
    *,
    mode: str = "elide",
    aggressive: bool = False,
    keep_boundary_index: int | None = None,
) -> dict:
    """Thin wrapper over ``compaction.shake_messages``.

    Returns ``{messages, saved_tokens, notice}`` for the RPC layer.
    """
    msgs = _snapshot_messages(messages)
    # importlib.import_module (not `from agent import compaction`): the
    # from-form short-circuits on the package attribute once the real module
    # was imported anywhere in the process, ignoring sys.modules patches in
    # tests. import_module consults sys.modules first.
    try:
        comp = importlib.import_module("agent.compaction")
    except Exception:  # noqa: BLE001
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
    "handoff_document",
    "btw_answer",
    "compact_session",
    "shake_session",
]
