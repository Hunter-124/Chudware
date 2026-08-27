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
from pathlib import Path
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


# --------------------------------------------------------------------------- OMP snap rendering
# Optional: under the omp backend (env CHUDWARE_COMPACTION_BACKEND=omp / config.json
# agent.compaction_backend="omp") the canonical local archive below is ADDITIONALLY
# rendered to OMP snapcompact PNG frames via the bridge's ``snap_render`` RPC
# (vendor/omp/bridge/src/snap-render.ts — foveated text-to-image frames in the active
# provider's optimal shape; the local cut/serialization stays canonical either way).
# The full OG source text round-trips through the bridge (``text`` in, ``text`` out)
# and persists in the snap store (services/agent/snap_store.py), so the next round
# re-renders with new history appended and a mid-session provider switch re-renders
# the same source in the new provider's shape — the original contents are never lost.
# Any bridge/store failure degrades to the text-only archive with a warning.

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


# Machine-readable first line of every reinjected snap archive message — the
# conversation itself is the re-snap chain record (see _snap_anchor).
_SNAP_STAMP_RE = re.compile(
    r"\[snap:([0-9a-f]{8,32})(?:\s+api=([\w.-]*))?(?:\s+model=([^\]]*))?\]")


def _snap_stamp_line(snap: dict) -> str:
    return f"[snap:{snap.get('snap_id')} api={snap.get('api') or ''} model={snap.get('model') or ''}]"


def _snap_anchor(prior_archive: dict | None, messages: list) -> dict | None:
    """Resolve the chain parent for the next snap render.

    An explicit ``prior_archive["snap"]`` wins (Python-side callers that thread
    the archive through); otherwise the NEWEST message carrying a snap stamp
    line (reinject writes it). If the conversation no longer carries a stamped
    archive message — the user deleted it, or this is a fresh session — the
    chain deliberately starts fresh instead of appending history the
    conversation has lost.
    """
    if isinstance(prior_archive, dict):
        snap = prior_archive.get("snap")
        if isinstance(snap, dict) and snap.get("snap_id"):
            return snap
    for msg in reversed(list(messages or [])):
        if not isinstance(msg, dict):
            continue
        m = _SNAP_STAMP_RE.search(_message_text_blob(msg))
        if m:
            return {
                "snap_id": m.group(1),
                "api": m.group(2) or "",
                "model": (m.group(3) or "").strip(),
            }
    return None


def _snap_render_and_store(out: dict, *, serialized: str, prior_archive: dict | None,
                           messages: list, provider: dict,
                           session_id: str | None) -> None:
    """Best-effort snap render of this round's archive.

    Renders ``serialized`` (this round's newly discarded history) unfolded onto
    the chain parent's OG source via the bridge ``snap_render`` RPC, persists
    OG source + frames in the snap store, and attaches a small
    ``archive["snap"]`` reference (no base64 on the RPC wire). Any failure
    leaves ``archive["warning"]`` and the archive stays text-only.
    """
    archive = out.get("archive")
    if not isinstance(archive, dict) or not isinstance(provider, dict):
        return
    if not provider.get("vision"):
        return
    if _compaction_backend() != "omp":
        return
    try:
        from services.agent.config import (  # noqa: PLC0415
            compaction_snap_frames, compaction_snap_max_frames)
        if not compaction_snap_frames():
            return
        max_frames = compaction_snap_max_frames()
    except Exception:  # noqa: BLE001 — config unreadable: defaults (on, 80)
        max_frames = 80
    api = str(provider.get("api") or "anthropic")
    model = str(provider.get("model") or provider.get("model_id") or "")
    try:
        from services.agent import omp_bridge, snap_store  # noqa: PLC0415
        anchor = _snap_anchor(prior_archive, messages)
        prior_source = ""
        if anchor:
            prior_source = snap_store.source_of(str(anchor.get("snap_id"))) or ""
        if not prior_source and isinstance(prior_archive, dict):
            # Legacy text-only prior: seed the OG chain from its folded text.
            prior_source = str(prior_archive.get("text") or "")
            if not prior_source:
                head = str(prior_archive.get("text_head") or "")
                tail = str(prior_archive.get("text_tail") or "")
                prior_source = "\n".join(p for p in (head, tail) if p)
        if not serialized and not prior_source:
            return  # nothing archived this round
        resp = omp_bridge.call("snap_render", {
            "text": serialized or "",
            "priorText": prior_source,
            "modelId": model,
            "api": api,
            "maxFrames": max_frames,
        }, timeout=180)
        og_source = str(resp.get("text") or "")
        frames = [f for f in (resp.get("frames") or []) if isinstance(f, dict)]
        meta = snap_store.put(
            og_source, frames,
            parent_id=str(anchor.get("snap_id")) if anchor else None,
            model=model, api=api,
            truncated_chars=int(resp.get("truncatedChars") or 0),
        )
        if not meta:
            raise RuntimeError("snap store write failed")
        snap_ref = {
            "snap_id": meta["snap_id"],
            "api": api,
            "model": model,
            "frame_count": len(frames),
            "truncated_chars": int(resp.get("truncatedChars") or 0),
            "checksum": str(resp.get("checksum") or ""),
        }
        if session_id:
            snap_ref["session_id"] = str(session_id)
        archive["snap"] = snap_ref
        if frames:
            out["short_summary"] = (
                f'{out.get("short_summary") or ""} (+{len(frames)} snap frames)')
    except Exception as exc:  # noqa: BLE001 — snap must never break compaction
        archive["warning"] = f"snap render unavailable ({exc}); archive is text-only"


def _budget_frames(frames: list, cap: int, byte_budget: int) -> list:
    """Chronological edge-keeping frame budget: when over *cap*, drop MIDDLE
    frames first (the foveal edges carry the densest context); then stop adding
    frames once *byte_budget* of PNG bytes is exhausted."""
    n = len(frames)
    if n <= cap:
        chosen = list(frames)
    else:
        head_n = max(1, cap // 2)
        tail_n = cap - head_n
        chosen = frames[:head_n] + frames[n - tail_n:]
    out: list = []
    total = 0
    for fr in chosen:
        size = 0
        path = fr.get("path")
        if path:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
        if out and total + size > byte_budget:
            break
        total += size
        out.append(fr)
    return out


def _resnap_for_provider(meta: dict, *, api: str, model: str) -> dict | None:
    """Re-render a snap's stored OG source in a different provider's shape
    (mid-session provider switch). Returns the freshly stored meta (chained to
    the old snap via parent_id), or None to keep the old-shape frames."""
    try:
        from services.agent import omp_bridge, snap_store  # noqa: PLC0415
        from services.agent.config import compaction_snap_max_frames  # noqa: PLC0415
        source = snap_store.source_of(str(meta.get("snap_id") or ""))
        if not source:
            return None
        resp = omp_bridge.call("snap_render", {
            "text": "",
            "priorText": source,
            "modelId": model,
            "api": api,
            "maxFrames": compaction_snap_max_frames(),
        }, timeout=180)
        new_meta = snap_store.put(
            str(resp.get("text") or source),
            [f for f in (resp.get("frames") or []) if isinstance(f, dict)],
            parent_id=str(meta.get("snap_id") or "") or None,
            model=model, api=api,
            truncated_chars=int(resp.get("truncatedChars") or 0),
        )
        if not new_meta:
            return None
        return snap_store.get(str(new_meta["snap_id"]))
    except Exception:  # noqa: BLE001 — keep the old-shape frames on any failure
        return None


def _snap_vision_parts(body: dict, provider: dict, text_content: str) -> list | None:
    """Content-parts reinject for vision providers: the archive text part plus
    the snap's PNG frame images, capped to the provider's image budget.

    Re-renders from the stored OG source when the active provider differs from
    the one that rendered the snap (provider switch) or when frame files are
    missing. Returns None to fall back to the plain-text reinject.
    """
    snap = body.get("snap")
    if not isinstance(snap, dict) or not snap.get("snap_id"):
        return None
    try:
        from services.agent import snap_store, snap_spec  # noqa: PLC0415
        from services.agent.providers.base import image_part  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    meta = snap_store.get(str(snap["snap_id"]))
    if meta is None:
        return None  # store lost/corrupt — the text reinject still carries history
    api = str(provider.get("api") or "anthropic")
    model = str(provider.get("model") or provider.get("model_id") or "")
    if (str(meta.get("api") or "") != api) or (str(meta.get("model") or "") != model):
        resnap = _resnap_for_provider(meta, api=api, model=model)
        if resnap is not None:
            meta = resnap
    frames = [f for f in (meta.get("frames") or []) if isinstance(f, dict) and f.get("path")]
    if not frames:
        return None  # small archive: everything is in the text edges already
    cap = snap_spec.FAMILY_IMAGE_BUDGETS.get(api, snap_spec.DEFAULT_IMAGE_BUDGET)
    keep = _budget_frames(frames, int(cap), int(snap_spec.FRAME_BYTES_BUDGET))
    omitted = len(frames) - len(keep)
    text = text_content
    if omitted > 0:
        text += (f"\n\n({omitted} middle snap frame(s) omitted to fit the provider "
                 "image budget; the full archive is preserved in the snap store.)")
    parts = [{"type": "text", "text": text}]
    for fr in keep:
        parts.append(image_part(str(fr["path"]), "image/png"))
    return parts if len(parts) > 1 else None


# --------------------------------------------------------------------------- snapcompact_archive

def snapcompact_archive(
    messages: list[dict],
    keep_recent_tokens: int = 20_000,
    prior_archive: dict | None = None,
    supersede_reads: bool = True,
    drop_useless: bool = True,
    provider: dict | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Text-phase snapcompact: archive discarded turns, keep recent tail.

    Returns::

        {
          "archive": {text, text_head, text_tail, truncated_chars, total_chars,
                      snap?, warning?},
          "summary": <static reading guide>,
          "short_summary": "Archived N chars of history (+M as text)",
          "kept_messages": [...],
          "cut_index": int,
        }

    No LLM call. Never cuts mid tool-result. Cumulative fold of
    ``prior_archive["text"]``.

    OMP snap rendering: when the omp backend is gated on (env
    CHUDWARE_COMPACTION_BACKEND=omp / agent.compaction_backend="omp") AND
    *provider* carries ``vision: true``, the archived source is additionally
    rendered to OMP snapcompact PNG frames (provider-shaped via
    ``provider["api"]``/``provider["model"]``), the full OG source + frames are
    persisted in the snap store, and ``archive["snap"]`` carries a small
    reference (snap_id/api/model/frame_count/checksum; no base64 here).
    Re-snap chaining resolves via :func:`_snap_anchor` (explicit prior_archive
    snap, else the stamped archive message in *messages*). Bridge/store
    failure degrades to text-only with ``archive["warning"]`` — the canonical
    local archive is produced identically either way.
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
        summary = (
            STATIC_READING_GUIDE.replace(
                "Reading HISTORY:\n",
                f"Reading HISTORY:\n"
                f"- About {layout['truncated_chars']:,} characters of older middle history "
                f"were dropped to fit the archive budget.\n",
            )
        )

    out: dict[str, Any] = {
        "archive": {
            "text": layout["text"],
            "text_head": layout["text_head"],
            "text_tail": layout["text_tail"],
            "truncated_chars": layout["truncated_chars"],
            "total_chars": layout["total_chars"],
            # frames reserved for the snap store (paths, not base64, on this wire)
            "frames": [],
        },
        "summary": summary,
        "short_summary": short,
        "kept_messages": kept_messages,
        "cut_index": cut_index,
    }
    if provider is not None:
        _snap_render_and_store(
            out, serialized=serialized, prior_archive=prior_archive,
            messages=messages, provider=provider, session_id=session_id,
        )
    return out


# --------------------------------------------------------------------------- reinject_archive

def reinject_archive(archive: dict, *, provider: dict | None = None) -> list[dict]:
    """Build prepend messages (lead-in summary + archive text edges) for rebuild.

    Default return is one plain-text user message. When the archive carries a
    ``snap`` reference, a machine-readable ``[snap:<id> api=… model=…]`` stamp
    line leads the text — the conversation itself is the re-snap chain record
    (:func:`_snap_anchor` finds it next round). With a vision *provider* and a
    snap present, the message becomes content parts: the same text plus the
    snap's PNG frames (budget-capped, re-rendered from the stored OG source
    when the provider changed — :func:`_snap_vision_parts`)."""
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

    snap = body.get("snap")
    if isinstance(snap, dict) and snap.get("snap_id"):
        content = _snap_stamp_line(snap) + "\n" + content

    if provider and provider.get("vision") and isinstance(snap, dict):
        try:
            parts = _snap_vision_parts(body, provider, content)
        except Exception:  # noqa: BLE001 — reinject must never lose the text
            parts = None
        if parts is not None:
            return [{"role": "user", "content": parts}]
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
                    "image", "image_url", "input_image"):
                blob = str(
                    block.get("path")
                    or block.get("url")
                    or block.get("data")
                    or ""
                )
                dropped_tokens += max(count_tokens(blob), 85)
            else:
                kept.append(block)
        if dropped_tokens:
            message["content"] = kept
            saved += dropped_tokens
            regions.append({
                "kind": "images",
                "message_index": message_index,
                "tokens": dropped_tokens,
                "original_text": "",
                "label": "images",
            })
    placeholders = len(regions)
    return {
        "messages": msgs,
        "saved_tokens": saved,
        "regions": regions,
        "placeholders": placeholders,
        "notice": (
            f"shook {placeholders} image region(s), ~{saved} tokens"
            if placeholders else "no images to drop"
        ),
    }


def shake_messages(
    messages: list[dict],
    *,
    mode: str = "elide",
    aggressive: bool = False,
    keep_boundary_index: int | None = None,
) -> dict[str, Any]:
    """Shake a deep-copied transcript through private OMP's canonical algorithm.

    CAD owns only JSON/OpenAI message conversion and the image-only product mode.
    Region selection, protection, token thresholds, mutation ordering, and
    placeholders are owned and tested once by ``pi-agent-core``.
    """
    if mode == "images":
        return _shake_images(messages)
    from services.agent import omp_bridge  # noqa: PLC0415

    params: dict[str, Any] = {
        "messages": copy.deepcopy(list(messages or [])),
        "aggressive": bool(aggressive),
    }
    if keep_boundary_index is not None:
        params["keepBoundaryIndex"] = int(keep_boundary_index)
    response = omp_bridge.call("shake", params)
    if not isinstance(response, dict):
        raise omp_bridge.BridgeUnavailable("OMP shake returned a non-object result")
    shaken = response.get("messages")
    regions = response.get("regions")
    if not isinstance(shaken, list) or not isinstance(regions, list):
        raise omp_bridge.BridgeUnavailable("OMP shake result is missing messages/regions")
    return {
        "messages": shaken,
        "saved_tokens": int(response.get("saved_tokens") or 0),
        "regions": regions,
        "placeholders": int(response.get("placeholders") or 0),
        "notice": str(response.get("notice") or ""),
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
