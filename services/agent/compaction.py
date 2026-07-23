"""Session compaction engines (omp snapcompact text-phase + shake).

Pure, deterministic helpers for transcript serialization, archival compaction
(text edges; PNG frames deferred to a future vision phase), mechanical shake
elision, and archive reinjection.  Consumed by session_cmds RPCs and tests.

Message shape is OpenAI-style role+content dicts (not ChatMsg {u,t}):

* ``user`` / ``assistant`` / ``system`` / ``developer`` — ``content`` str or
  list of parts (``text``, ``thinking``, ``toolCall``/``tool_call``, ``image``).
* Assistant may also carry top-level ``tool_calls`` (OpenAI function-call form).
* Tool results: role ``tool`` or ``toolResult`` with ``tool_call_id`` /
  ``toolCallId``, optional ``useless`` / ``is_error`` / ``pruned_at``.
"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- constants
CHARS_PER_TOKEN = 3.2  # conservative: code/JSON-heavy transcripts run ~3.0-3.3 chars/token,
                       # not the naive 4 — underestimating lets true prompt tokens overflow
                       # the context window while compaction thinks it's under budget.

TOOL_RESULT_MAX_CHARS = 2000
TOOL_ARG_MAX_CHARS = 500
TOOL_CALL_MAX_CHARS = 2000
TRUNCATE_HEAD_RATIO = 0.6

# Text-phase edge capacity (~one HQ page). Frames are future/vision-only.
TEXT_EDGE_CHARS = 8000

SUPERSEDED_NOTICE = "[Superseded by a newer read of this file]"
MIDDLE_OMIT_MARKER = (
    "\n-------------- middle history omitted ({n} chars) --------------\n"
)

DEFAULT_PROTECT_TOKENS = 16_000
DEFAULT_MIN_SAVINGS = 4_000
FENCE_MIN_TOKENS = 400
PLACEHOLDER_TOKEN_ESTIMATE = 16

SKILL_URL_PREFIX = "skill://"
PROTECTED_TOOL_NAMES = frozenset({"skill"})

# Static reading guide (omp snapcompact-summary.md text-phase subset; no frames).
STATIC_READING_GUIDE = """\
You are resuming a prior conversation. Its earlier turns were archived to reclaim context and are reproduced under HISTORY below, oldest to newest. Read HISTORY in full, then continue from the live conversation that follows it.

The archived transcript uses compact scopes:
- `¶user:`, `¶think:`, `¶ai:`, and `¶call:` open user, assistant reasoning, assistant reply, and tool-call scopes.
- Following lines without a `¶…:` prefix remain in the current scope. Consecutive blocks of the same kind omit the repeated prefix.
- A tool call reads `¶call:name(args)//intent`; the trailing `//intent` is optional. Its `<out>…</out>` block is tool output.

Reading HISTORY:
- Plain-text sections are the verbatim transcript — rely on them exactly.
- When an exact earlier detail matters and a section reads unclearly, re-derive it from the workspace (re-read files, re-run commands) rather than guessing.

HISTORY
===================
"""

_OPENING_XML = re.compile(r"^<([a-z_-]+)(?:\s+[^>]*)?>$")
_CLOSING_XML = re.compile(r"^</([a-z_-]+)>$")
_INTENT_FIELDS = frozenset({"intent", "i"})


# --------------------------------------------------------------------------- token / truncate helpers

def count_tokens(text: str) -> int:
    """Conservative chars/token ceiling heuristic (see CHARS_PER_TOKEN)."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def truncate_middle(text: str, max_chars: int, head_ratio: float = TRUNCATE_HEAD_RATIO) -> str:
    """Keep head+tail of *text*, eliding the middle beyond *max_chars*."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    ratio = min(max(head_ratio, 0.0), 1.0)
    head_chars = int(round(max_chars * ratio))
    tail_chars = max_chars - head_chars
    elided = len(text) - max_chars
    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars > 0 else ""
    return f"{head} […{elided}ch elided…] {tail}"


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


# --------------------------------------------------------------------------- message accessors

def _role(msg: dict) -> str:
    return str(msg.get("role") or "")


def _is_tool_result(msg: dict) -> bool:
    return _role(msg) in ("tool", "toolResult")


def _is_assistant(msg: dict) -> bool:
    return _role(msg) == "assistant"


def _is_userish(msg: dict) -> bool:
    return _role(msg) in ("user", "system", "developer")


def _tool_call_id(msg: dict) -> str:
    return str(msg.get("tool_call_id") or msg.get("toolCallId") or "")


def _tool_name(msg: dict) -> str:
    return str(msg.get("tool_name") or msg.get("name") or msg.get("tool") or "")


def _is_error_result(msg: dict) -> bool:
    if msg.get("is_error") is True or msg.get("isError") is True:
        return True
    return False


def _is_useless_result(msg: dict) -> bool:
    return msg.get("useless") is True and not _is_error_result(msg)


def _is_pruned(msg: dict) -> bool:
    return msg.get("pruned_at") is not None or msg.get("prunedAt") is not None


def _content_as_list(content: Any) -> list:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif btype == "thinking" and block.get("thinking"):
            parts.append(str(block["thinking"]))
        elif btype in ("input_text",) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _message_text_blob(msg: dict) -> str:
    """Flatten a message for token estimation."""
    role = _role(msg)
    chunks: list[str] = []
    chunks.append(_text_from_content(msg.get("content")))
    if role == "assistant":
        for tc in _iter_tool_calls(msg):
            chunks.append(tc.get("name") or "")
            chunks.append(_json_dumps(tc.get("arguments") or {}))
            if tc.get("intent"):
                chunks.append(str(tc["intent"]))
    return "\n".join(c for c in chunks if c)


def estimate_message_tokens(msg: dict) -> int:
    return count_tokens(_message_text_blob(msg))


def _iter_tool_calls(msg: dict) -> list[dict]:
    """Normalize assistant tool calls from content parts and/or tool_calls."""
    out: list[dict] = []
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("toolCall", "tool_call"):
                args = block.get("arguments")
                if args is None and "input" in block:
                    args = block.get("input")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        args = {"_raw": args}
                if not isinstance(args, dict):
                    args = {}
                out.append({
                    "id": str(block.get("id") or block.get("tool_call_id") or ""),
                    "name": str(block.get("name") or block.get("tool") or ""),
                    "arguments": args,
                    "intent": block.get("intent") or args.get("intent") or args.get("i") or "",
                })
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list):
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "")
            raw_args = fn.get("arguments", tc.get("arguments", {}))
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            out.append({
                "id": str(tc.get("id") or ""),
                "name": name,
                "arguments": args,
                "intent": tc.get("intent") or args.get("intent") or args.get("i") or "",
            })
    return out


def _read_path_from_args(args: dict) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in ("path", "file", "filepath", "file_path"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _base_read_path(path: str) -> str:
    """Strip trailing read selectors (:50-200, :raw, …) for supersede grouping."""
    # Conservative: peel trailing :chunks that look like selectors.
    base = path
    while True:
        colon = base.rfind(":")
        if colon <= 0:
            break
        sel = base[colon + 1 :]
        if not sel:
            break
        # scheme:// paths — only strip if last segment looks like a selector
        if "://" in base[:colon] and base[:colon].count(":") == 0:
            # e.g. skill://foo — no selector
            break
        if re.fullmatch(
            r"(?i)(?:L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?(?:,L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?)*|raw|conflicts"
            r"|(?:L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?(?:,L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?)*:)?raw"
            r"|raw:(?:L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?(?:,L?\d+(?:(?:[-+]|\.\.)L?\d+|-|\.\.)?)*))",
            sel,
        ) or re.fullmatch(r"(?i)raw|conflicts|L?\d+(?:[-+.]{1,2}L?\d+)?(?:,L?\d+(?:[-+.]{1,2}L?\d+)?)*", sel):
            base = base[:colon]
            continue
        # compound range:raw already handled; stop on non-selector
        break
    return base


# --------------------------------------------------------------------------- serialize_transcript

def serialize_transcript(messages: list[dict], *, drop_useless: bool = True) -> str:
    """Dense ``¶user/¶think/¶ai/¶call`` transcript of *messages*.

    Caps: tool body 2000 (head 0.6 / tail 0.4), per-arg 500, per-call args 2000.
    When *drop_useless*, tool pairs with ``useless=True`` (non-error) are omitted.
    """
    if not messages:
        return ""

    useless_ids: set[str] = set()
    result_text_by_id: dict[str, str] = {}
    for msg in messages:
        if not _is_tool_result(msg):
            continue
        tid = _tool_call_id(msg)
        if drop_useless and _is_useless_result(msg):
            if tid:
                useless_ids.add(tid)
            continue
        text = _text_from_content(msg.get("content"))
        if text and tid:
            result_text_by_id[tid] = text

    parts: list[str] = []
    last_prefix: str | None = None
    merged_ids: set[str] = set()

    def push_part(prefix: str, content: str) -> None:
        nonlocal last_prefix
        if not content:
            return
        if parts and last_prefix == prefix:
            sep = "" if parts[-1].endswith("\n") or content.startswith("\n") else "\n"
            parts[-1] += sep + content
        else:
            parts.append(prefix + content)
            last_prefix = prefix

    def render_result_block(raw: str) -> str:
        body = truncate_middle(raw, TOOL_RESULT_MAX_CHARS, TRUNCATE_HEAD_RATIO)
        return f"<out>\n{body}\n</out>"

    def format_args(args: dict) -> str:
        entries = []
        for key, value in args.items():
            if key in _INTENT_FIELDS:
                continue
            entries.append(
                f"{key}={truncate_middle(_json_dumps(value), TOOL_ARG_MAX_CHARS, TRUNCATE_HEAD_RATIO)}"
            )
        return truncate_middle(", ".join(entries), TOOL_CALL_MAX_CHARS, TRUNCATE_HEAD_RATIO)

    for msg in messages:
        role = _role(msg)
        if role in ("user", "system", "developer"):
            content = _text_from_content(msg.get("content"))
            if content:
                push_part("¶user:", content)
        elif role == "assistant":
            pending_think: list[str] = []
            pending_text: list[str] = []

            def flush_assistant() -> None:
                nonlocal pending_think, pending_text
                if pending_think:
                    push_part("¶think:", "\n".join(pending_think))
                if pending_text:
                    push_part("¶ai:", "\n".join(pending_text))
                pending_think = []
                pending_text = []

            content = msg.get("content")
            emitted_from_parts = False
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        t = str(block.get("text") or "")
                        if t.strip():
                            pending_text.append(t)
                    elif btype == "thinking":
                        t = str(block.get("thinking") or "")
                        if t.strip():
                            pending_think.append(t)
                    elif btype in ("toolCall", "tool_call"):
                        tc_id = str(block.get("id") or block.get("tool_call_id") or "")
                        if drop_useless and tc_id in useless_ids:
                            continue
                        flush_assistant()
                        emitted_from_parts = True
                        args = block.get("arguments")
                        if args is None:
                            args = block.get("input") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                args = {"_raw": args}
                        if not isinstance(args, dict):
                            args = {}
                        intent = str(
                            block.get("intent") or args.get("intent") or args.get("i") or ""
                        ).replace("\n", " ").strip()
                        name = str(block.get("name") or block.get("tool") or "tool")
                        first = f"{name}({format_args(args)})"
                        if intent:
                            first += f"//{intent}"
                        lines = [first]
                        if tc_id and tc_id in result_text_by_id:
                            merged_ids.add(tc_id)
                            lines.append(render_result_block(result_text_by_id[tc_id]))
                        push_part("¶call:", "\n".join(lines))
                flush_assistant()
            elif isinstance(content, str) and content.strip():
                pending_text.append(content)
                flush_assistant()

            # OpenAI top-level tool_calls (if not already emitted from content parts)
            if not emitted_from_parts:
                for tc in _iter_tool_calls({"tool_calls": msg.get("tool_calls")}):
                    tc_id = tc["id"]
                    if drop_useless and tc_id in useless_ids:
                        continue
                    flush_assistant()
                    intent = str(tc.get("intent") or "").replace("\n", " ").strip()
                    first = f"{tc['name'] or 'tool'}({format_args(tc.get('arguments') or {})})"
                    if intent:
                        first += f"//{intent}"
                    lines = [first]
                    if tc_id and tc_id in result_text_by_id:
                        merged_ids.add(tc_id)
                        lines.append(render_result_block(result_text_by_id[tc_id]))
                    push_part("¶call:", "\n".join(lines))
                # if only string content was pending and we already flushed, ok
            else:
                # still emit any tool_calls not represented as content parts
                part_ids = {
                    str(b.get("id") or "")
                    for b in (content if isinstance(content, list) else [])
                    if isinstance(b, dict) and b.get("type") in ("toolCall", "tool_call")
                }
                for tc in _iter_tool_calls({"tool_calls": msg.get("tool_calls")}):
                    if tc["id"] and tc["id"] in part_ids:
                        continue
                    if drop_useless and tc["id"] in useless_ids:
                        continue
                    intent = str(tc.get("intent") or "").replace("\n", " ").strip()
                    first = f"{tc['name'] or 'tool'}({format_args(tc.get('arguments') or {})})"
                    if intent:
                        first += f"//{intent}"
                    lines = [first]
                    if tc["id"] and tc["id"] in result_text_by_id:
                        merged_ids.add(tc["id"])
                        lines.append(render_result_block(result_text_by_id[tc["id"]]))
                    push_part("¶call:", "\n".join(lines))

        elif _is_tool_result(msg):
            tid = _tool_call_id(msg)
            if (drop_useless and tid in useless_ids) or tid in merged_ids:
                continue
            text = result_text_by_id.get(tid) or _text_from_content(msg.get("content"))
            if text:
                push_part("¶call:", "\n" + render_result_block(text))

    return "\n\n".join(parts)


# --------------------------------------------------------------------------- cut / supersede

def _valid_cut_index(messages: list[dict], index: int) -> int:
    """Never cut mid tool-result: advance to first non-tool-result at/after *index*."""
    n = len(messages)
    i = max(0, min(index, n))
    while i < n and _is_tool_result(messages[i]):
        i += 1
    return i


def find_cut_index(messages: list[dict], keep_recent_tokens: int) -> int:
    """Index of first kept message; entries before are archived.

    Walks newest→oldest until *keep_recent_tokens* is filled, then snaps to a
    valid cut (user/assistant, never tool-result). If the whole transcript fits
    in the keep budget, returns 0 (keep everything).
    """
    n = len(messages)
    if n == 0:
        return 0
    if keep_recent_tokens <= 0:
        # Keep nothing recent → archive all (cut at end).
        return n

    accumulated = 0
    cut = 0
    for i in range(n - 1, -1, -1):
        accumulated += estimate_message_tokens(messages[i])
        if accumulated >= keep_recent_tokens:
            cut = _valid_cut_index(messages, i)
            # If snapping pushed past end, try previous non-tool boundary.
            if cut >= n:
                # Keep only trailing tool results? Prefer cutting at this tool's
                # owning assistant by walking back.
                j = i
                while j > 0 and _is_tool_result(messages[j]):
                    j -= 1
                cut = j
            break
    else:
        return 0
    return cut


def _collect_tool_calls_by_id(messages: list[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for msg in messages:
        if not _is_assistant(msg):
            continue
        for tc in _iter_tool_calls(msg):
            if tc["id"]:
                by_id[tc["id"]] = tc
    return by_id


def apply_supersede_reads(messages: list[dict]) -> list[dict]:
    """Return a shallow-copied list where older reads of the same path are blanked.

    Newest-first: a later ``read`` of path P supersedes earlier reads of P
    (selector-stripped). Mutates only copies of superseded tool-result messages.
    """
    if not messages:
        return []
    tool_calls = _collect_tool_calls_by_id(messages)
    # Walk newest → oldest; mark older keys when a newer one is seen.
    seen_bases: set[str] = set()
    superseded_indices: set[int] = set()
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not _is_tool_result(msg) or _is_pruned(msg):
            continue
        tid = _tool_call_id(msg)
        tc = tool_calls.get(tid)
        name = (tc or {}).get("name") or _tool_name(msg)
        if name != "read":
            continue
        args = (tc or {}).get("arguments") or {}
        path = _read_path_from_args(args if isinstance(args, dict) else {})
        if not path:
            continue
        base = _base_read_path(path)
        if base in seen_bases:
            superseded_indices.add(i)
        else:
            seen_bases.add(base)

    if not superseded_indices:
        return list(messages)

    out: list[dict] = []
    for i, msg in enumerate(messages):
        if i not in superseded_indices:
            out.append(msg)
            continue
        m = dict(msg)
        m["content"] = SUPERSEDED_NOTICE
        m["pruned_at"] = m.get("pruned_at") or m.get("prunedAt") or True
        out.append(m)
    return out


def _plan_text_archive(text: str, edge_chars: int = TEXT_EDGE_CHARS) -> dict[str, Any]:
    """Split archive *text* into verbatim head/tail with truncated middle (text phase)."""
    if not text:
        return {
            "text": "",
            "text_head": "",
            "text_tail": "",
            "truncated_chars": 0,
            "total_chars": 0,
        }
    edge = max(1, edge_chars)
    if len(text) <= 2 * edge:
        return {
            "text": text,
            "text_head": text,
            "text_tail": "",
            "truncated_chars": 0,
            "total_chars": len(text),
        }
    text_head = text[:edge]
    text_tail = text[-edge:]
    middle_len = len(text) - len(text_head) - len(text_tail)
    marker = MIDDLE_OMIT_MARKER.format(n=f"{middle_len:,}")
    # Persist full kept source for cumulative fold: head + marker + tail
    # (middle dropped under text-phase budget; prior_archive folds this text).
    kept = text_head + marker + text_tail
    return {
        "text": kept,
        "text_head": text_head,
        "text_tail": text_tail,
        "truncated_chars": middle_len,
        "total_chars": len(text_head) + len(text_tail),
    }


# --------------------------------------------------------------------------- OMP bridge hybrid backend
# Optional: snapcompact_archive can route through the OMP bridge's ``compact`` RPC
# (real reuse of OMP's snapcompact engine over vendor/omp/bridge — see
# OMP_INTEGRATION_PLAN.md §RPC CONTRACT, docs/omp-integration/01-DECISIONS.md
# §Compaction) instead of the pure-Python path below. Off by default (unchanged
# behavior); opt in with env CHUDWARE_COMPACTION_BACKEND=omp or config.json
# agent.compaction_backend="omp" (legacy nested agent.compaction.backend still
# honored). Any bridge failure (bun/script missing, crash, timeout, RPC error)
# raises omp_bridge.BridgeUnavailable, caught here to fall back to the pure-Python
# path transparently.

def _bridge():
    from services.agent import omp_bridge  # noqa: PLC0415
    return omp_bridge


def _compaction_backend(cfg: dict | None = None) -> str:
    """``"local"`` (default) or ``"omp"``.

    Precedence ENV > config.json > default (omp port) — resolution lives in
    services.agent.config.compaction_backend (returns "builtin"/"omp"; "builtin"
    maps to this module's "local"); CHUDWARE_COMPACTION_BACKEND still wins, and
    config.json ``agent.compaction_backend`` is honored when the env is unset.
    """
    try:
        from services.agent.config import compaction_backend  # noqa: PLC0415
        return "omp" if compaction_backend(cfg) == "omp" else "local"
    except Exception:  # noqa: BLE001 — bad/missing config never breaks compaction
        return "local"


def _to_wire_message(msg: dict) -> dict[str, Any]:
    """Adapt one CAD transcript message onto the bridge's stricter ``compact``
    wire shape (vendor/omp/bridge/src/compact.ts ``readCompactMessage``): role
    ``toolResult`` (not ``tool``) with camelCase ``toolCallId``/``toolName``/
    ``isError``, assistant tool calls as content parts (not a top-level OpenAI
    ``tool_calls``). Reuses this module's own role/content accessors
    (_is_tool_result/_tool_call_id/_tool_name/_is_error_result/_iter_tool_calls)
    so both the local and bridge paths agree on what a message means.
    """
    if _is_tool_result(msg):
        out: dict[str, Any] = {
            "role": "toolResult",
            "toolCallId": _tool_call_id(msg),
            "content": _content_as_list(msg.get("content")),
        }
        name = _tool_name(msg)
        if name:
            out["toolName"] = name
        if _is_error_result(msg):
            out["isError"] = True
        if msg.get("useless") is True:
            out["useless"] = True
        return out

    if _is_assistant(msg):
        parts: list[dict[str, Any]] = []
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append({"type": "text", "text": str(block["text"])})
                elif block.get("type") == "thinking" and block.get("thinking"):
                    parts.append({"type": "thinking", "thinking": str(block["thinking"])})
        elif isinstance(content, str) and content:
            parts.append({"type": "text", "text": content})
        for tc in _iter_tool_calls(msg):
            if not tc.get("id") or not tc.get("name"):
                continue
            part: dict[str, Any] = {
                "type": "toolCall", "id": tc["id"], "name": tc["name"],
                "arguments": tc.get("arguments") or {},
            }
            if tc.get("intent"):
                part["intent"] = str(tc["intent"])
            parts.append(part)
        return {"role": "assistant", "content": parts}

    return {
        "role": "developer" if _role(msg) == "developer" else "user",
        "content": _content_as_list(msg.get("content")),
    }


def _to_wire_archive(archive: dict) -> dict[str, Any]:
    """CAD's ``archive`` dict (snake_case — this module's own shape, possibly a
    previous bridge response already mapped back to it) onto compact.ts's
    camelCase ``Archive`` wire shape (``readArchive``), so a ``prior_archive``
    round-trips correctly no matter which backend produced it.
    """
    out: dict[str, Any] = {"frames": archive.get("frames") or []}
    total_chars = archive.get("total_chars", archive.get("totalChars"))
    if total_chars is not None:
        out["totalChars"] = total_chars
    truncated_chars = archive.get("truncated_chars", archive.get("truncatedChars"))
    if truncated_chars is not None:
        out["truncatedChars"] = truncated_chars
    if archive.get("text"):
        out["text"] = archive["text"]
    text_head = archive.get("text_head", archive.get("textHead"))
    if text_head:
        out["textHead"] = text_head
    text_tail = archive.get("text_tail", archive.get("textTail"))
    if text_tail:
        out["textTail"] = text_tail
    checksum = archive.get("checksum")
    if checksum:
        out["checksum"] = checksum
    return out


def _bridge_compact(bridge, messages: list[dict], keep_recent_tokens: int, prior_archive: dict | None,
                     supersede_reads: bool) -> dict[str, Any]:
    """``snapcompact_archive`` via the bridge's ``compact`` RPC — OMP's real
    snapcompact algorithm (vendor/omp/packages/snapcompact: foveation + PNG
    frame planning), reused verbatim server-side; see
    vendor/omp/bridge/src/compact.ts and OMP_INTEGRATION_PLAN.md §RPC CONTRACT.

    The bridge owns the keep/discard boundary (it re-derives it from
    ``messages`` + ``keepRecentTokens`` with its own tool-result-safe cut,
    mirroring but not reusing this module's ``find_cut_index``); this function
    only trusts the *count* of ``keptMessages`` it returns and re-slices the
    original CAD-shaped ``msgs`` at that boundary, so ``kept_messages`` in the
    result is always this module's own message objects — never the bridge's
    re-serialized wire copies (role/content get lossily normalized on the
    wire, e.g. ``tool`` -> ``toolResult``, ``tool_call_id`` -> ``toolCallId``).

    Maps ``{keptMessages,summary,shortSummary,archive?,checksum,frames?,
    recoverable?,warning?}`` back onto this module's ``{archive,summary,
    short_summary,kept_messages,cut_index}`` shape (plus ``checksum``/
    ``recoverable``/``warning`` as additive extra keys when present).
    """
    msgs = list(messages or [])
    if supersede_reads:
        msgs = apply_supersede_reads(msgs)

    params: dict[str, Any] = {
        "messages": [_to_wire_message(m) for m in msgs],
        "keepRecentTokens": int(keep_recent_tokens),
    }
    if isinstance(prior_archive, dict):
        params["priorArchive"] = _to_wire_archive(prior_archive)

    result = bridge.call("compact", params, timeout=120)

    wire_kept = result.get("keptMessages")
    kept_count = len(wire_kept) if isinstance(wire_kept, list) else len(msgs)
    cut_index = max(0, min(len(msgs), len(msgs) - kept_count))
    kept_messages = msgs[cut_index:]

    raw_archive = result.get("archive")
    raw_archive = raw_archive if isinstance(raw_archive, dict) else {}
    frames = result.get("frames")
    if not isinstance(frames, list):
        frames = raw_archive.get("frames") or []
    text_head = str(raw_archive.get("textHead") or raw_archive.get("text") or "")
    text_tail = str(raw_archive.get("textTail") or "")
    if not text_head and not text_tail:
        # No native/foveated archive (e.g. no pi-natives binary for this platform, or the
        # renderer threw) — compact.ts's textFallbackResult puts the whole serialized
        # archive into `summary` instead (vendor/omp/bridge/src/compact.ts:277-292). Mirror
        # that into archive.text/text_head so reinject_archive() finds it there too.
        text_head = str(result.get("summary") or "")
    archive: dict[str, Any] = {
        "text": str(raw_archive.get("text") or text_head),
        "text_head": text_head,
        "text_tail": text_tail,
        "truncated_chars": int(raw_archive.get("truncatedChars") or 0),
        "total_chars": int(raw_archive.get("totalChars") or (len(text_head) + len(text_tail))),
        "frames": frames,
    }

    out: dict[str, Any] = {
        "archive": archive,
        "summary": str(result.get("summary") or ""),
        "short_summary": str(result.get("shortSummary") or ""),
        "kept_messages": kept_messages,
        "cut_index": cut_index,
    }
    if "checksum" in result:
        archive["checksum"] = result["checksum"]  # embedded too, so a later prior_archive round-trip carries it
        out["checksum"] = result["checksum"]
    if isinstance(result.get("recoverable"), dict):
        out["recoverable"] = result["recoverable"]
    if result.get("warning"):
        out["warning"] = result["warning"]
    return out


# --------------------------------------------------------------------------- snapcompact_archive

def snapcompact_archive(
    messages: list[dict],
    keep_recent_tokens: int = 20_000,
    prior_archive: dict | None = None,
    supersede_reads: bool = True,
    drop_useless: bool = True,
) -> dict[str, Any]:
    """Text-phase snapcompact: archive discarded turns, keep recent tail.

    Returns::

        {
          "archive": {text, text_head, text_tail, truncated_chars, total_chars},
          "summary": <static reading guide>,
          "short_summary": "Archived N chars of history (+M as text)",
          "kept_messages": [...],
          "cut_index": int,
        }

    No LLM call. PNG frames are intentionally omitted (future/vision phase).
    Never cuts mid tool-result. Cumulative fold of ``prior_archive["text"]``.

    Hybrid backend: when gated on (env CHUDWARE_COMPACTION_BACKEND=omp /
    agent.compaction_backend="omp" in config.json), routes through the OMP
    bridge's ``compact`` RPC (vendor/omp/bridge) instead of the pure-Python
    path below, falling back here whenever the bridge is unavailable. Same
    return shape either way (see docstring above).
    """
    if _compaction_backend() == "omp":
        bridge = _bridge()
        try:
            return _bridge_compact(bridge, messages, keep_recent_tokens, prior_archive, supersede_reads)
        except bridge.BridgeUnavailable:
            pass  # OMP bridge unavailable — fall back to the pure-Python path below
    msgs = list(messages or [])
    if supersede_reads:
        msgs = apply_supersede_reads(msgs)

    cut_index = find_cut_index(msgs, keep_recent_tokens)
    discarded = msgs[:cut_index]
    kept_messages = msgs[cut_index:]

    serialized = serialize_transcript(discarded, drop_useless=drop_useless)

    prior_text = ""
    prior_trunc = 0
    if isinstance(prior_archive, dict):
        prior_text = str(prior_archive.get("text") or "")
        if not prior_text:
            head = str(prior_archive.get("text_head") or "")
            tail = str(prior_archive.get("text_tail") or "")
            prior_text = "\n".join(p for p in (head, tail) if p)
        try:
            prior_trunc = int(prior_archive.get("truncated_chars") or 0)
        except (TypeError, ValueError):
            prior_trunc = 0

    if prior_text and serialized:
        archive_source = f"{prior_text}\n{serialized}"
    elif prior_text:
        archive_source = prior_text
    else:
        archive_source = serialized

    layout = _plan_text_archive(archive_source)
    layout["truncated_chars"] = int(layout["truncated_chars"]) + prior_trunc

    total = int(layout["total_chars"])
    text_chars = len(layout.get("text_head") or "") + len(layout.get("text_tail") or "")
    short = f"Archived {total:,} chars of history (+{text_chars:,} as text)"

    summary = STATIC_READING_GUIDE
    if layout["truncated_chars"]:
        summary = (
            STATIC_READING_GUIDE.replace(
                "Reading HISTORY:\n",
                f"Reading HISTORY:\n"
                f"- About {layout['truncated_chars']:,} characters of older middle history "
                f"were dropped to fit the archive budget.\n",
            )
        )

    return {
        "archive": {
            "text": layout["text"],
            "text_head": layout["text_head"],
            "text_tail": layout["text_tail"],
            "truncated_chars": layout["truncated_chars"],
            "total_chars": layout["total_chars"],
            # frames reserved for vision phase
            "frames": [],
        },
        "summary": summary,
        "short_summary": short,
        "kept_messages": kept_messages,
        "cut_index": cut_index,
    }


# --------------------------------------------------------------------------- reinject_archive

def reinject_archive(archive: dict) -> list[dict]:
    """Build prepend messages (lead-in summary + archive text edges) for rebuild."""
    if not isinstance(archive, dict):
        return []

    # Accept either bare archive dict or snapcompact_archive result.
    body = archive
    summary = archive.get("summary")
    if "archive" in archive and isinstance(archive["archive"], dict):
        body = archive["archive"]
        summary = archive.get("summary") or summary

    text_head = str(body.get("text_head") or "")
    text_tail = str(body.get("text_tail") or "")
    full_text = str(body.get("text") or "")
    truncated = int(body.get("truncated_chars") or 0)

    if not summary:
        summary = STATIC_READING_GUIDE

    history_parts: list[str] = []
    if text_head:
        history_parts.append(text_head)
    if truncated > 0 and text_tail:
        history_parts.append(MIDDLE_OMIT_MARKER.format(n=f"{truncated:,}").strip())
    elif full_text and not text_head and not text_tail:
        history_parts.append(full_text)
    if text_tail:
        history_parts.append(text_tail)
    elif full_text and text_head and not text_tail and truncated == 0:
        # entire archive in text_head already
        pass

    history = "\n".join(history_parts) if history_parts else full_text
    content = summary.rstrip() + "\n\n" + history if history else summary
    return [{"role": "user", "content": content}]


# --------------------------------------------------------------------------- shake

def _is_protected_tool_result(msg: dict, tool_calls_by_id: dict[str, dict]) -> bool:
    name = _tool_name(msg)
    tid = _tool_call_id(msg)
    tc = tool_calls_by_id.get(tid)
    if tc and tc.get("name"):
        name = tc["name"]
    if name in PROTECTED_TOOL_NAMES:
        return True
    if name == "read" and tc:
        path = _read_path_from_args(tc.get("arguments") or {})
        if path and path.startswith(SKILL_URL_PREFIX):
            return True
    # Also protect skill:// in content for harness-only results without paired call
    if name == "read":
        blob = _text_from_content(msg.get("content"))
        # no path — not protected
        _ = blob
    return False


def _scan_text_for_block_ranges(text: str) -> list[tuple[int, int]]:
    """Fenced code + top-level lowercase XML spans; ranges are [start, end)."""
    ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_start = -1
    tag_stack: list[str] = []
    xml_start = -1
    line_start = 0
    n = len(text)
    i = 0
    while i <= n:
        if i != n and text[i] != "\n":
            i += 1
            continue
        line = text[line_start:i]
        line_end = i  # excludes newline
        trimmed = line.lstrip()
        is_fence = trimmed.startswith("```") or trimmed.startswith("~~~")
        if is_fence:
            if not in_fence:
                in_fence = True
                fence_start = line_start
            else:
                in_fence = False
                ranges.append((fence_start, line_end))
                fence_start = -1
            line_start = i + 1
            i += 1
            continue
        if not in_fence:
            if line and len(line) == len(trimmed) and _OPENING_XML.match(trimmed):
                m = _OPENING_XML.match(trimmed)
                if m:
                    if not tag_stack:
                        xml_start = line_start
                    tag_stack.append(m.group(1))
            else:
                cm = _CLOSING_XML.match(trimmed) if trimmed else None
                if cm and tag_stack and tag_stack[-1] == cm.group(1):
                    tag_stack.pop()
                    if not tag_stack and xml_start >= 0:
                        ranges.append((xml_start, line_end))
                        xml_start = -1
        line_start = i + 1
        i += 1

    # Merge overlaps (outermost wins)
    if len(ranges) <= 1:
        return ranges
    ranges.sort(key=lambda r: r[0])
    kept: list[tuple[int, int]] = []
    last_end = -1
    for start, end in ranges:
        if start < last_end:
            continue
        kept.append((start, end))
        last_end = end
    return kept


def _placeholder(tokens: int, artifact_id: str | None, region_index: int) -> str:
    if artifact_id:
        return f"[shaken ~{tokens} tokens — recover: artifact://{artifact_id} (region {region_index})]"
    return f"[shaken ~{tokens} tokens]"


def shake_messages(
    messages: list[dict],
    *,
    mode: str = "elide",
    aggressive: bool = False,
    keep_boundary_index: int | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Mechanical in-place elision of heavy tool results / large fences / XML.

    *mode*:
      - ``"elide"`` (default): tool results + large fenced code / lowercase XML.
      - ``"images"``: drop image content blocks only.

    *aggressive* (manual /shake): protectTokens=0, minSavings=0.
    Default auto: protectTokens=16000, minSavings=4000.

    Never splits messages; never touches toolCall argument blocks.
    Useless non-error tool results are eligible even inside the protect window.
    """
    msgs = copy.deepcopy(list(messages or []))
    if not msgs:
        return {
            "messages": msgs,
            "saved_tokens": 0,
            "regions": [],
            "placeholders": 0,
            "notice": "nothing to shake",
        }

    if mode == "images":
        saved = 0
        placeholders = 0
        regions: list[dict] = []
        for mi, msg in enumerate(msgs):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            new_parts = []
            dropped_here = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "image",
                    "image_url",
                    "input_image",
                ):
                    # estimate ~tokens for image as path/data length or fixed
                    blob = str(
                        block.get("path")
                        or block.get("url")
                        or block.get("data")
                        or ""
                    )
                    tok = max(count_tokens(blob), 85)
                    saved += tok
                    dropped_here += tok
                    continue
                new_parts.append(block)
            if dropped_here:
                msg["content"] = new_parts
                regions.append({
                    "kind": "images",
                    "message_index": mi,
                    "tokens": dropped_here,
                    "original_text": "",
                    "label": "images",
                })
                placeholders += 1
        return {
            "messages": msgs,
            "saved_tokens": saved,
            "regions": regions,
            "placeholders": placeholders,
            "notice": (
                f"shook {placeholders} image region(s), ~{saved} tokens"
                if placeholders
                else "no images to drop"
            ),
        }

    protect_tokens = 0 if aggressive else DEFAULT_PROTECT_TOKENS
    min_savings = 0 if aggressive else DEFAULT_MIN_SAVINGS
    boundary = 0 if keep_boundary_index is None else max(0, int(keep_boundary_index))

    n = len(msgs)
    # tokens strictly after index i
    accumulated_after = [0] * n
    acc = 0
    for i in range(n - 1, -1, -1):
        accumulated_after[i] = acc
        acc += estimate_message_tokens(msgs[i])

    tool_calls_by_id = _collect_tool_calls_by_id(msgs)
    regions = []

    for i in range(n):
        if i < boundary:
            continue
        msg = msgs[i]
        useless_result = _is_tool_result(msg) and _is_useless_result(msg)
        if not useless_result and accumulated_after[i] < protect_tokens:
            continue

        if _is_tool_result(msg):
            if _is_pruned(msg):
                continue
            if _is_protected_tool_result(msg, tool_calls_by_id):
                continue
            text = _text_from_content(msg.get("content"))
            if not text:
                continue
            tokens = estimate_message_tokens(msg)
            regions.append({
                "kind": "toolResult",
                "message_index": i,
                "tokens": tokens,
                "original_text": text,
                "label": _tool_name(msg) or "tool",
            })
            continue

        # Text blocks in user/assistant/developer/system — never toolCall parts
        content = msg.get("content")
        if isinstance(content, str):
            for start, end in _scan_text_for_block_ranges(content):
                slice_ = content[start:end]
                tokens = count_tokens(slice_)
                if tokens < FENCE_MIN_TOKENS:
                    continue
                regions.append({
                    "kind": "block",
                    "message_index": i,
                    "block_index": -1,
                    "start": start,
                    "end": end,
                    "tokens": tokens,
                    "original_text": slice_,
                    "label": _role(msg) or "message",
                })
        elif isinstance(content, list):
            for bi, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue  # never touch toolCall / thinking for fences? omp does assistant text only
                text = str(block.get("text") or "")
                for start, end in _scan_text_for_block_ranges(text):
                    slice_ = text[start:end]
                    tokens = count_tokens(slice_)
                    if tokens < FENCE_MIN_TOKENS:
                        continue
                    regions.append({
                        "kind": "block",
                        "message_index": i,
                        "block_index": bi,
                        "start": start,
                        "end": end,
                        "tokens": tokens,
                        "original_text": slice_,
                        "label": _role(msg) or "message",
                    })

    savings = sum(max(0, r["tokens"] - PLACEHOLDER_TOKEN_ESTIMATE) for r in regions)
    if savings < min_savings:
        return {
            "messages": msgs,
            "saved_tokens": 0,
            "regions": [],
            "placeholders": 0,
            "notice": f"minSavings not met ({savings} < {min_savings})",
        }

    art = artifact_id or "shake"
    # Apply block regions high-start-first per message; tool results independent.
    # Group by (message_index, block_index) for block splices.
    tool_regions = [r for r in regions if r["kind"] == "toolResult"]
    block_regions = [r for r in regions if r["kind"] == "block"]
    block_regions.sort(key=lambda r: (r["message_index"], r.get("block_index", -1), -r["start"]))

    placeholders = 0
    saved_tokens = 0
    region_counter = 0

    for r in tool_regions:
        region_counter += 1
        ph = _placeholder(r["tokens"], art, region_counter)
        mi = r["message_index"]
        m = msgs[mi]
        m["content"] = ph
        m["pruned_at"] = True
        placeholders += 1
        saved_tokens += max(0, r["tokens"] - PLACEHOLDER_TOKEN_ESTIMATE)
        r["region_index"] = region_counter
        r["placeholder"] = ph

    for r in block_regions:
        region_counter += 1
        ph = _placeholder(r["tokens"], art, region_counter)
        mi = r["message_index"]
        bi = r.get("block_index", -1)
        m = msgs[mi]
        if bi == -1 and isinstance(m.get("content"), str):
            text = m["content"]
            m["content"] = text[: r["start"]] + ph + text[r["end"] :]
        elif isinstance(m.get("content"), list) and 0 <= bi < len(m["content"]):
            block = m["content"][bi]
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                block["text"] = text[: r["start"]] + ph + text[r["end"] :]
        placeholders += 1
        saved_tokens += max(0, r["tokens"] - PLACEHOLDER_TOKEN_ESTIMATE)
        r["region_index"] = region_counter
        r["placeholder"] = ph

    # Document-order regions for artifact writers
    ordered = sorted(
        regions,
        key=lambda r: (
            r["message_index"],
            0 if r["kind"] == "toolResult" else 1,
            r.get("start", 0),
        ),
    )

    return {
        "messages": msgs,
        "saved_tokens": saved_tokens,
        "regions": ordered,
        "placeholders": placeholders,
        "notice": f"shook {placeholders} region(s), ~{saved_tokens} tokens",
    }


__all__ = [
    "serialize_transcript",
    "snapcompact_archive",
    "shake_messages",
    "reinject_archive",
    "count_tokens",
    "truncate_middle",
    "find_cut_index",
    "apply_supersede_reads",
    "STATIC_READING_GUIDE",
    "SUPERSEDED_NOTICE",
    "TOOL_RESULT_MAX_CHARS",
    "TOOL_ARG_MAX_CHARS",
    "TOOL_CALL_MAX_CHARS",
    "TEXT_EDGE_CHARS",
]
