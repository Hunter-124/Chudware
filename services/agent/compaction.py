"""Deterministic local transcript compaction for offline/standalone consumers.

Online Chudcode sessions own their own history compaction. This module retains
text archival, mechanical shake elision, and archive reinjection only for
callers that explicitly cannot use a live Chudcode session.

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
CHARS_PER_TOKEN = (
    3.2  # conservative: code/JSON-heavy transcripts run ~3.0-3.3 chars/token,
)
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

_INTENT_FIELDS = frozenset({"intent", "i"})


# --------------------------------------------------------------------------- token / truncate helpers


def count_tokens(text: str) -> int:
    """Conservative chars/token ceiling heuristic (see CHARS_PER_TOKEN)."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def truncate_middle(
    text: str, max_chars: int, head_ratio: float = TRUNCATE_HEAD_RATIO
) -> str:
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
                out.append(
                    {
                        "id": str(block.get("id") or block.get("tool_call_id") or ""),
                        "name": str(block.get("name") or block.get("tool") or ""),
                        "arguments": args,
                        "intent": block.get("intent")
                        or args.get("intent")
                        or args.get("i")
                        or "",
                    }
                )
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
            out.append(
                {
                    "id": str(tc.get("id") or ""),
                    "name": name,
                    "arguments": args,
                    "intent": tc.get("intent")
                    or args.get("intent")
                    or args.get("i")
                    or "",
                }
            )
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
        ) or re.fullmatch(
            r"(?i)raw|conflicts|L?\d+(?:[-+.]{1,2}L?\d+)?(?:,L?\d+(?:[-+.]{1,2}L?\d+)?)*",
            sel,
        ):
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
        return truncate_middle(
            ", ".join(entries), TOOL_CALL_MAX_CHARS, TRUNCATE_HEAD_RATIO
        )

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
                        intent = (
                            str(
                                block.get("intent")
                                or args.get("intent")
                                or args.get("i")
                                or ""
                            )
                            .replace("\n", " ")
                            .strip()
                        )
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
                    if isinstance(b, dict)
                    and b.get("type") in ("toolCall", "tool_call")
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


# --------------------------------------------------------------------------- snapcompact_archive


def snapcompact_archive(
    messages: list[dict],
    keep_recent_tokens: int = 20_000,
    prior_archive: dict | None = None,
    supersede_reads: bool = True,
    drop_useless: bool = True,
) -> dict[str, Any]:
    """Local text compaction for explicit offline/standalone consumers.

    No LLM or online bridge call is made. The archive preserves bounded text
    edges, never cuts mid tool-result, and cumulatively folds
    ``prior_archive["text"]``. Ordinary Chudcode sessions must use Chudcode's
    ``compact`` session command instead of this function.
    """
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
        summary = STATIC_READING_GUIDE.replace(
            "Reading HISTORY:\n",
            f"Reading HISTORY:\n"
            f"- About {layout['truncated_chars']:,} characters of older middle history "
            f"were dropped to fit the archive budget.\n",
        )

    return {
        "archive": {
            "text": layout["text"],
            "text_head": layout["text_head"],
            "text_tail": layout["text_tail"],
            "truncated_chars": layout["truncated_chars"],
            "total_chars": layout["total_chars"],
        },
        "summary": summary,
        "short_summary": short,
        "kept_messages": kept_messages,
        "cut_index": cut_index,
    }


# --------------------------------------------------------------------------- reinject_archive


def reinject_archive(archive: dict) -> list[dict]:
    """Build one plain-text archive message for an offline transcript rebuild."""
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


# --------------------------------------------------------------------------- shake adapter


def _shake_images(messages: list[dict]) -> dict[str, Any]:
    """CAD content-shape adapter for `/shake images`.

    Image stripping is product-side because OMP's universal shake intentionally
    selects textual context regions only. No token-selection or text-mutation
    algorithm lives here.
    """
    msgs = copy.deepcopy(list(messages or []))
    saved = 0
    regions: list[dict] = []
    for message_index, message in enumerate(msgs):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        dropped_tokens = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "image",
                "image_url",
                "input_image",
            ):
                blob = str(
                    block.get("path") or block.get("url") or block.get("data") or ""
                )
                dropped_tokens += max(count_tokens(blob), 85)
            else:
                kept.append(block)
        if dropped_tokens:
            message["content"] = kept
            saved += dropped_tokens
            regions.append(
                {
                    "kind": "images",
                    "message_index": message_index,
                    "tokens": dropped_tokens,
                    "original_text": "",
                    "label": "images",
                }
            )
    placeholders = len(regions)
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


def shake_messages(
    messages: list[dict],
    *,
    mode: str = "elide",
    aggressive: bool = False,
    keep_boundary_index: int | None = None,
) -> dict[str, Any]:
    """Mechanically elide heavy local transcript regions without online calls."""
    if mode == "images":
        return _shake_images(messages)

    shaken = copy.deepcopy(list(messages or []))
    calls = _collect_tool_calls_by_id(shaken)
    regions: list[dict[str, Any]] = []
    saved = 0
    protect_tokens = 16_000
    min_savings = 4_000
    suffix_tokens = [0] * (len(shaken) + 1)
    for index in range(len(shaken) - 1, -1, -1):
        suffix_tokens[index] = suffix_tokens[index + 1] + estimate_message_tokens(
            shaken[index]
        )

    def replace_region(
        message: dict,
        message_index: int,
        original: str,
        kind: str,
        label: str,
    ) -> None:
        nonlocal saved
        original_tokens = count_tokens(original)
        region_index = len(regions)
        placeholder = (
            f"[shaken ~{original_tokens:,} tokens — {label} (region {region_index})]"
        )
        message["content"] = placeholder
        saved += max(0, original_tokens - count_tokens(placeholder))
        regions.append(
            {
                "kind": kind,
                "message_index": message_index,
                "tokens": original_tokens,
                "original_text": original,
                "label": label,
                "placeholder": placeholder,
            }
        )

    boundary = (
        max(0, int(keep_boundary_index))
        if keep_boundary_index is not None
        else len(shaken)
    )
    for index, message in enumerate(shaken):
        if index >= boundary:
            continue
        if _is_tool_result(message):
            original = _text_from_content(message.get("content"))
            call = calls.get(_tool_call_id(message), {})
            name = _tool_name(message) or _tool_name(call)
            args = call.get("arguments") or call.get("args") or {}
            path = _read_path_from_args(args) if isinstance(args, dict) else None
            if name == "read" and path and path.startswith("skill://"):
                continue
            tokens = count_tokens(original)
            old_enough = suffix_tokens[index + 1] >= protect_tokens
            if original and (aggressive or (old_enough and tokens >= min_savings)):
                replace_region(message, index, original, "tool_result", name or "tool")
            continue
        if not aggressive:
            continue
        original = _text_from_content(message.get("content"))
        fences = list(re.finditer(r"```[^\n]*\n.*?```", original, flags=re.DOTALL))
        if not fences:
            continue
        rewritten = original
        for match in reversed(fences):
            fenced = match.group(0)
            tokens = count_tokens(fenced)
            region_index = len(regions)
            placeholder = (
                f"[shaken ~{tokens:,} tokens — code fence (region {region_index})]"
            )
            rewritten = (
                rewritten[: match.start()] + placeholder + rewritten[match.end() :]
            )
            saved += max(0, tokens - count_tokens(placeholder))
            regions.append(
                {
                    "kind": "code",
                    "message_index": index,
                    "tokens": tokens,
                    "original_text": fenced,
                    "label": "code fence",
                    "placeholder": placeholder,
                }
            )
        message["content"] = rewritten

    placeholders = len(regions)
    return {
        "messages": shaken,
        "saved_tokens": saved,
        "regions": regions,
        "placeholders": placeholders,
        "notice": (
            f"shook {placeholders} region(s), ~{saved} tokens"
            if placeholders
            else "minSavings not reached; nothing to shake"
        ),
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
