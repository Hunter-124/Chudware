"""agent_events.py — `/2` agent-level event builders + span-linkage helpers.

Contract: contracts/agent_events.md (`aicad.agent_events/2`).

This module is the single place that constructs the net-new `/2` agent-level events
(`agent.tool_call` with a human-readable `target`, `agent.thought`,
`agent.context_injected`, `agent.spawn`, `agent.advisor`) and stamps the §2.1
parent-linkage fields (`id` span id, `parent_id`, `actor`, `seq`).

Two emitter conventions coexist in the codebase and this module bridges them:

* **EventJournal / DAG / ladder convention** — ``sink(kind: str, payload: dict)``.
  This is the canonical persistence signature (``EventJournal.emit``).
* **loop.py live convention** — ``sink(data: dict)`` where ``data["kind"]`` is inside.

`emit_event()` accepts an optional callable of EITHER shape (auto-detected by arity)
plus an optional ``EventJournal`` and fans the event out to both, so a single call
persists to events.jsonl AND streams live to the GUI. No new transport is introduced.
"""
from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import secrets
from typing import Any, Callable, Optional

# A per-process monotonic counter. `seq` is advisory only (see contract §2.1): a
# fresh EventJournal per emit cannot guarantee run-scoped monotonicity, so consumers
# fall back to `ts` + the parent tree. This still gives a stable within-process order.
_seq_counter = itertools.count(1)


def new_span_id() -> str:
    """Mint a fresh opaque span id (the per-event tree key, contract §2.1)."""
    return secrets.token_hex(8)


def _digest(obj: Any) -> str:
    """First 16 hex chars of the SHA-256 of the JSON/text form (contract §3.2)."""
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        text = str(obj)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# Arg keys, in priority order, that tend to name "what a tool acts on" in plain words.
_TARGET_KEYS = (
    "target", "face", "faces", "edge", "edges", "feature", "sketch", "part",
    "part_name", "name", "body", "component", "plane", "doc_id", "path", "id",
)


def derive_target(args: dict | None, *, limit: int = 120) -> str:
    """Best-effort human-readable "what it acts on" string for a tool call (§3.2).

    Shallow by design (contract-honest): pulls the first legible value from a small
    set of known arg keys; falls back to a compact join of scalar args. Returns ""
    when nothing legible is present. NOT a universal arg→English mapper.
    """
    if not args or not isinstance(args, dict):
        return ""
    for key in _TARGET_KEYS:
        if key in args:
            val = args[key]
            if isinstance(val, (str, int, float)) and str(val).strip():
                return str(val).strip()[:limit]
            if isinstance(val, (list, tuple)) and val:
                joined = ", ".join(str(v) for v in val[:4])
                if joined.strip():
                    return joined[:limit]
    # Fallback: compact join of scalar args (skip long/blob values).
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 40:
            parts.append(f"{k}={v}")
        if len(parts) >= 3:
            break
    return (", ".join(parts))[:limit]


def _cap(text: Any, n: int) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def emit_event(
    kind: str,
    payload: dict,
    *,
    live: Optional[Callable[..., None]] = None,
    journal: Any | None = None,
) -> None:
    """Fan one event out to the live sink AND the EventJournal (both optional).

    *live* may be either ``(kind, payload)`` or ``(data)`` shaped — detected by
    arity. Both sinks are best-effort: a raising sink never breaks the caller.
    """
    if journal is not None:
        try:
            journal.emit(kind, dict(payload))
        except Exception:  # noqa: BLE001 — persistence must not break the loop
            pass
    if live is not None:
        try:
            _call_live(live, kind, payload)
        except Exception:  # noqa: BLE001 — a flaky UI sink must not break the loop
            pass


def _call_live(live: Callable[..., None], kind: str, payload: dict) -> None:
    """Dispatch to a live sink of either supported shape."""
    n = _positional_arity(live)
    if n >= 2:
        live(kind, dict(payload))  # (kind, payload) convention
    else:
        data = {"kind": kind, **payload}
        live(data)                 # loop.py single-dict convention


def _positional_arity(fn: Callable[..., Any]) -> int:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 1
    n = 0
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
        elif p.kind == p.VAR_POSITIONAL:
            return 2  # *args → assume it can take (kind, payload)
    return n


# ---------------------------------------------------------------------------
# Event builders — stamp §2.1 linkage + the kind-specific fields.
# ---------------------------------------------------------------------------

def _linkage(node_id: str, *, span_id: str, parent_id: str | None, actor: str | None) -> dict:
    d: dict[str, Any] = {"node_id": node_id, "id": span_id, "seq": next(_seq_counter)}
    if parent_id:
        d["parent_id"] = parent_id
    if actor:
        d["actor"] = actor
    return d


def _error_text(result: Any) -> str:
    """Human-readable failure text from a tool result.

    ``agent_turn`` builds ``{"ok": False, "error": ...}`` for raised dispatch
    errors; tools themselves may return the same shape, a JSON-encoded string,
    or plain text. Returns "" when nothing legible.
    """
    if result is None:
        return ""
    if isinstance(result, BaseException):
        return f"{type(result).__name__}: {result}"
    payload = result
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return ""
        try:
            payload = json.loads(s)
        except Exception:  # noqa: BLE001
            return s
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            v = payload.get(key)
            if v:
                return v if isinstance(v, str) else json.dumps(v, default=str,
                                                               ensure_ascii=False)
        errs = payload.get("errors")
        if isinstance(errs, list) and errs:
            return "\n".join(str(e) for e in errs)
    try:
        return json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(payload)


def tool_call_event(
    node_id: str, tool_name: str, args: dict | None, result: Any, *,
    ok: bool, latency_ms: float, span_id: str | None = None,
    parent_id: str | None = None, actor: str | None = None,
    args_preview: str | None = None,
    artifacts: list[dict] | None = None,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload.update({
        "tool_name": tool_name,
        "target": derive_target(args),
        "args_digest": _digest(args or {}),
        "result_digest": _digest(result),
        "ok": bool(ok),
        "latency_ms": round(float(latency_ms), 2),
    })
    # Additive: optional capped args JSON for Doing drawer (consumers ignore if unknown).
    preview = args_preview
    if preview is None and args is not None:
        try:
            preview = json.dumps(args, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            preview = str(args)
    if preview:
        payload["args_preview"] = _cap(preview, 2048)
    # Additive (2026-07-24): capped failure text so the GUI can show WHY a tool
    # call failed instead of only a red border. Omitted on success — successful
    # events stay lean.
    if not ok:
        err = _error_text(result)
        if err:
            payload["error_preview"] = _cap(err, 2048)
    # Additive (2026-07-17): filesystem paths of render/export deliverables so the
    # GUI can expand a tool-call row inline. Cap 8; omit when none.
    arts = artifacts
    if arts is None:
        try:
            arts = derive_artifacts(result)
        except Exception:  # noqa: BLE001
            arts = None
    if arts:
        payload["artifacts"] = arts[:8]
    return "agent.tool_call", payload


def derive_artifacts(result: Any, *, limit: int = 8) -> list[dict]:
    """Best-effort extract of produced file paths from a tool result.

    Contract (2026-07-17 additive): ``[{path, kind}]`` with
    ``kind in image|mesh|file``. Looks at common keys (path / contact_sheet /
    stl_path / step_path / paths), Image-like objects with a ``.path``, list
    returns from render tools, and JSON-encoded dict/list results. Returns []
    when nothing legible — callers omit the field.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _kind_for(path: str) -> str:
        low = path.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
            return "image"
        if low.endswith((".stl", ".3mf", ".obj", ".ply")):
            return "mesh"
        return "file"

    def _add(path: Any) -> None:
        if path is None:
            return
        if not isinstance(path, str):
            # mcp.server.fastmcp.Image (or any object with a filesystem path)
            path = getattr(path, "path", None)
            if not isinstance(path, str):
                return
        p = path.strip()
        if not p or p in seen:
            return
        # Only treat path-looking strings as artifacts (absolute/relative file paths).
        if not (p.startswith("/") or p.startswith(".") or (len(p) > 2 and p[1] == ":")
                or "/" in p or "\\" in p or p.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".stl", ".step", ".stp",
                     ".step+", ".3mf", ".obj", ".ply", ".iges", ".igs", ".brep",
                     ".pdf", ".dxf", ".dwg", ".gltf", ".glb"))):
            return
        seen.add(p)
        out.append({"path": p, "kind": _kind_for(p)})

    def _walk(obj: Any, depth: int = 0) -> None:
        if len(out) >= limit or depth > 4 or obj is None:
            return
        if isinstance(obj, str):
            s = obj.strip()
            if not s:
                return
            # JSON-encoded payload from MCP tools that return dumps(...)
            if (s.startswith("{") and s.endswith("}")) or (
                    s.startswith("[") and s.endswith("]")):
                try:
                    _walk(json.loads(s), depth + 1)
                    return
                except Exception:  # noqa: BLE001
                    pass
            _add(s)
            return
        if isinstance(obj, dict):
            # Prefer well-known deliverable keys first.
            for key in (
                "contact_sheet", "path", "stl_path", "step_path", "png_path",
                "out_path", "image_path", "file_path", "paths",
            ):
                if key not in obj:
                    continue
                val = obj[key]
                if isinstance(val, (list, tuple)):
                    for item in val:
                        _add(item)
                        if len(out) >= limit:
                            return
                else:
                    _add(val)
                    if len(out) >= limit:
                        return
            # Nested common containers.
            for key in ("result", "artifact", "artifacts", "output", "data"):
                if key in obj:
                    _walk(obj[key], depth + 1)
                    if len(out) >= limit:
                        return
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                # Image object or path string; skip free-form captions.
                if hasattr(item, "path") or isinstance(item, str):
                    _add(item)
                elif isinstance(item, (dict, list, tuple)):
                    _walk(item, depth + 1)
                if len(out) >= limit:
                    return
            return
        # Bare Image-like object.
        if hasattr(obj, "path"):
            _add(obj)

    try:
        _walk(result)
    except Exception:  # noqa: BLE001
        return []
    return out[:limit]


def classification_event(
    node_id: str, *,
    task_class: str = "unknown",
    scaffold_level: str = "",
    tier: str = "",
    reason: str = "",
    request_snippet: str = "",
    picked_model: str | None = None,
    picked_provider: str | None = None,
    alternatives: list | None = None,
    span_id: str | None = None,
    parent_id: str | None = None,
    actor: str | None = None,
) -> tuple[str, dict]:
    """Emit pre-flight task classification (2026-07-17 additive).

    Mirrors ``workflow_router.RouterResult``. Emitted ONCE at request entry,
    BEFORE dispatch/routing, so the GUI can show a Routing card while work starts.
    Optional ``picked_model`` / ``picked_provider`` only when already resolved.
    Optional ``alternatives`` lists other plausible task classes (excl. chosen).
    """
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    # Enums (TaskClass / ScaffoldLevel) may arrive as str or .value-bearing.
    def _val(v: Any) -> str:
        if v is None:
            return ""
        return str(getattr(v, "value", v) or "")

    payload.update({
        "task_class": _val(task_class) or "unknown",
        "scaffold_level": _val(scaffold_level),
        "tier": _val(tier),
        # 280 chars: domain match terms + complexity + capability (Routing card).
        "reason": _cap(reason, 280),
        "request_snippet": _cap(request_snippet, 120),
    })
    if picked_model:
        payload["picked_model"] = str(picked_model)
    if picked_provider:
        payload["picked_provider"] = str(picked_provider)
    if alternatives is not None:
        # Normalize to list[dict] with string fields for the Routing card.
        norm: list[dict] = []
        for item in list(alternatives):
            if isinstance(item, dict):
                tc = _val(item.get("task_class", item.get("class", "")))
                if not tc:
                    continue
                entry: dict[str, Any] = {"task_class": tc}
                rs = item.get("reason")
                if rs is not None and str(rs):
                    entry["reason"] = _cap(str(rs), 160)
                norm.append(entry)
            else:
                tc = _val(item)
                if tc:
                    norm.append({"task_class": tc})
        payload["alternatives"] = norm
    return "agent.classification", payload


def thought_event(
    node_id: str, text: str, *, model: str | None = None,
    span_id: str | None = None, parent_id: str | None = None,
    actor: str | None = None, delta: bool = False,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload["text"] = _cap(text, 500)
    if model:
        payload["model"] = model
    return ("agent.thought.delta" if delta else "agent.thought"), payload


def context_injected_event(
    node_id: str, source: str, label: str, *, token_est: int = 0,
    span_id: str | None = None, parent_id: str | None = None,
    actor: str | None = None,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload.update({"source": source, "label": _cap(label, 80)})
    if token_est:
        payload["token_est"] = int(token_est)
    return "agent.context_injected", payload


def spawn_event(
    node_id: str, subagent_id: str, role: str, *, kind: str = "worker",
    brief: str = "", budget_tokens: int = 0, span_id: str | None = None,
    parent_id: str | None = None, actor: str | None = None,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload.update({
        "subagent_id": subagent_id,
        "role": role,
        # `spawn_kind` (NOT `kind`) — `kind` is the reserved envelope discriminator
        # (contract §3.2a note); a payload `kind` would clobber it on serialization.
        "spawn_kind": kind,
        "brief": _cap(brief, 120),
        "budget_tokens": int(budget_tokens),
    })
    return "agent.spawn", payload


def model_assignment_event(
    node_id: str, *, depth: int = 0, task_class: str | None = None,
    min_rank: int | None = None, picked_model: str = "",
    picked_provider: str = "", cost_tier: int | None = None,
    mode: str = "", rank: int | None = None, reason: str = "",
    subagent_id: str | None = None, role: str = "",
    span_id: str | None = None, parent_id: str | None = None,
    actor: str | None = None,
) -> tuple[str, dict]:
    """Emit auto/fixed model assignment audit for a spawn (mega-5 watch-list).

    Payload is the assignment table row: depth, task_class, min_rank,
    picked_model, cost_tier (+ provider/mode/rank/reason for grading).
    """
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload.update({
        "depth": int(depth),
        "task_class": (task_class or "") or "unknown",
        "min_rank": int(min_rank) if min_rank is not None else None,
        "picked_model": picked_model or "",
        "picked_provider": picked_provider or "",
        "cost_tier": int(cost_tier) if cost_tier is not None else None,
        "mode": mode or "",
        "rank": int(rank) if rank is not None else None,
        "reason": _cap(reason, 160),
        "role": role or "",
    })
    if subagent_id:
        payload["subagent_id"] = subagent_id
    return "agent.model_assignment", payload



def advisor_event(
    node_id: str, kind: str, verdict_summary: str = "", *,
    span_id: str | None = None, parent_id: str | None = None,
    actor: str | None = None,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    # `advisor_kind` (NOT `kind`) — reserved-envelope-key rule (contract §3.2a note).
    payload.update({"advisor_kind": kind, "verdict_summary": _cap(verdict_summary, 120)})
    return "agent.advisor", payload
