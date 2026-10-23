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
import os
import secrets
import tempfile
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


# Human-facing arg keys first. Opaque handles (doc_id / path / id) are demoted —
# Doing rows should show part names (mud_bucket), not engine ids (doc-cpp…).
_HUMAN_TARGET_KEYS = (
    "target", "face", "faces", "edge", "edges", "feature", "sketch", "part",
    "part_name", "name", "body", "component", "plane",
)
_OPAQUE_TARGET_KEYS = ("doc_id", "path", "id")


def _is_opaque_doc_id(val: Any) -> bool:
    """True for engine-minted doc handles (doc-cpp00000002 / doc_cpp…)."""
    if not isinstance(val, str):
        return False
    s = val.strip()
    return s.startswith("doc-cpp") or s.startswith("doc_cpp")


def _resolve_doc_name(doc_id: str) -> str:
    """Best-effort live part name for an opaque doc_id via doc_list. Never raises."""
    if not doc_id or not _is_opaque_doc_id(doc_id):
        return ""
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        for e in (cb.call("doc_list", {}) or {}).get("docs", []) or []:
            if not isinstance(e, dict) or e.get("doc_id") != doc_id:
                continue
            name = e.get("name")
            if isinstance(name, str) and name.strip() and not _is_opaque_doc_id(name):
                return name.strip()
            break
    except Exception:  # noqa: BLE001 — display resolve must never break emission
        pass
    return ""


def _feature_summary(op: dict, *, limit: int = 120) -> str:
    """One-line summary of a feature-bearing op (mirrors C++ summarize_feature_op)."""
    feat = op.get("feature")
    if not isinstance(feat, dict):
        return ""
    name = feat.get("name") if isinstance(feat.get("name"), str) else ""
    sub = ""
    params = feat.get("params")
    if isinstance(params, dict):
        family = params.get("family")
        if isinstance(family, str) and family.strip():
            sub = family.strip()
    if not sub:
        ftype = feat.get("type")
        if isinstance(ftype, str) and ftype.strip():
            sub = ftype.strip()
    if name and sub:
        return f"{name} ({sub})"[:limit]
    return (name or sub)[:limit]


def _ops_summary(ops: list, *, limit: int = 120, features_only: bool = False) -> str:
    """Human summary from an ops list (MCP add_features / doc_apply_ops shape).

    When *features_only* is True, only feature.name / part_name style labels
    count — bare op/type verbs are skipped so an opaque doc_id can still
    resolve to a live part name.
    """
    parts: list[str] = []
    show = len(ops) if len(ops) <= 3 else 2
    for op in ops[:show]:
        if not isinstance(op, dict):
            continue
        t = _feature_summary(op, limit=limit)
        if not t:
            keys = ("part_name", "name", "part") if features_only else (
                "part_name", "name", "part", "op", "type")
            for k in keys:
                v = op.get(k)
                if isinstance(v, str) and v.strip() and not _is_opaque_doc_id(v):
                    t = v.strip()
                    break
        if t:
            parts.append(t)
    if not parts:
        return ""
    s = " → ".join(parts)
    if len(ops) > show:
        s += f" → +{len(ops) - show} more"
    return s[:limit]


def _scalar_target(val: Any, *, resolve_docs: bool = True, limit: int = 120) -> str:
    """Format one arg value as a display target; resolve/skip opaque doc ids."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return str(val)[:limit]
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if _is_opaque_doc_id(s):
            if resolve_docs:
                resolved = _resolve_doc_name(s)
                if resolved:
                    return resolved[:limit]
            return ""  # never surface opaque ids as the human target
        return s[:limit]
    if isinstance(val, (list, tuple)) and val:
        bits: list[str] = []
        for v in val[:4]:
            piece = _scalar_target(v, resolve_docs=resolve_docs, limit=limit)
            if piece:
                bits.append(piece)
        return ", ".join(bits)[:limit] if bits else ""
    if isinstance(val, dict):
        return _feature_summary(val, limit=limit)
    return ""


def derive_target(args: dict | None, *, limit: int = 120) -> str:
    """Best-effort human-readable "what it acts on" string for a tool call (§3.2).

    Prefers part_name/name/feature summaries over opaque doc_id. When only a
    doc-cpp* id is available, best-effort resolves it to the live part name via
    doc_list (never raises). Returns "" when nothing legible is present.
    """
    if not args or not isinstance(args, dict):
        return ""
    for key in _HUMAN_TARGET_KEYS:
        if key not in args:
            continue
        got = _scalar_target(args[key], resolve_docs=False, limit=limit)
        if got:
            return got
    # MCP-style {doc_id, ops}: feature names beat a bare opaque handle.
    ops = args.get("ops")
    if isinstance(ops, list) and ops:
        from_ops = _ops_summary(ops, limit=limit, features_only=True)
        if from_ops:
            return from_ops
    # Demoted handles: resolve opaque doc_id → live name; skip if unresolvable.
    for key in _OPAQUE_TARGET_KEYS:
        if key not in args:
            continue
        got = _scalar_target(args[key], resolve_docs=True, limit=limit)
        if got:
            return got
    # Last resort from ops: bare op/type verbs (still better than nothing).
    if isinstance(ops, list) and ops:
        from_ops = _ops_summary(ops, limit=limit, features_only=False)
        if from_ops:
            return from_ops
    # Fallback: compact join of scalar args (skip long/blob + opaque ids).
    parts: list[str] = []
    for k, v in args.items():
        if k == "ops":
            continue
        if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 40:
            if isinstance(v, str) and _is_opaque_doc_id(v):
                resolved = _resolve_doc_name(v.strip())
                if resolved:
                    parts.append(f"{k}={resolved}")
                continue
            parts.append(f"{k}={v}")
        if len(parts) >= 3:
            break
    return (", ".join(parts))[:limit]


def _name_from_result(result: Any) -> str:
    """Human part name from a tool result dict (or JSON string), if present."""
    payload = result
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return ""
        try:
            payload = json.loads(s)
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("name", "part_name"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip() and not _is_opaque_doc_id(v):
            return v.strip()
    # Nested common envelopes (ok/result wrappers).
    inner = payload.get("result")
    if isinstance(inner, dict):
        for key in ("name", "part_name"):
            v = inner.get(key)
            if isinstance(v, str) and v.strip() and not _is_opaque_doc_id(v):
                return v.strip()
    return ""


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


# Producer allowlist (2026-07-27): ONLY these tools contribute auto-derived
# artifacts to the GUI Artifacts panel. The panel showed 93 junk rows because
# derive_artifacts ran on EVERY tool result — read tools (fs_read / fs_list /
# cad_guide / describe_*) return text full of paths they merely touched, and each
# one surfaced as a "produced" file. Allowlist (not reader-denylist) on purpose:
# a future read-only tool must default to NO artifacts, never to polluting the
# panel. Every name below is verified against the live tool_registry catalog.
_ARTIFACT_PRODUCER_TOOLS = frozenset({
    # CAD file/export deliverables
    "export_part", "save_part", "export_drawing", "make_drawing",
    # CAD render deliverables (the artifacts field's original 2026-07-17
    # purpose: expand render/export rows inline)
    "render_part", "render_views", "contact_sheet", "doc_sketch_contact_sheet",
    "review_drawing",
    # CAD document/report writers (services/deliverables + layout plan)
    "make_report", "make_workbook", "make_presentation", "layout_plan_report",
    "make_work_instruction", "make_weldment_traveler",
    # Office format conversion (LibreOffice bridge)
    "office_convert",
    # CAM deliverables (gcode / .cam+ job / operator setup sheet)
    "cam_post", "cam_save", "cam_setup_sheet",
    # ECAD + reverse-engineering export bundles
    "ecad_export", "export_reveng_bundle",
    # Workspace filesystem writers
    "fs_write", "fs_edit",
    # Office document writers
    "office_edit_doc", "office_edit_docx", "office_edit_pdf",
    "office_edit_pptx", "office_edit_xlsx", "office_edit_csv",
    # Topology optimization study/job artifacts
    "run_topology_optimization",
})


def tool_call_event(
    node_id: str, tool_name: str, args: dict | None, result: Any, *,
    ok: bool, latency_ms: float, span_id: str | None = None,
    parent_id: str | None = None, actor: str | None = None,
    args_preview: str | None = None,
    artifacts: list[dict] | None = None,
    outcome_kind: str | None = None,
    executed: bool | None = None,
) -> tuple[str, dict]:
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    # Prefer result.name when present (new_part / add_features envelopes) over an
    # opaque or empty args-derived target — display only; args keep the real doc_id.
    target = derive_target(args)
    from_result = _name_from_result(result)
    if from_result and (not target or _is_opaque_doc_id(target)):
        target = from_result[:120]
    payload.update({
        "tool_name": tool_name,
        "target": target,
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
    # Additive (2026-08-26): STRUCTURED outcome class, so the GUI never has to
    # parse error prose to tell a harness refusal from a tool crash. Values are
    # agent_turn.TOOL_OUTCOME_KINDS; ``executed`` says whether the tool body
    # actually ran (a refusal ran nothing; a timeout ran and is still running).
    # Omitted when the caller does not classify, so an older consumer keeps
    # exactly its current ok-only behavior.
    if outcome_kind:
        payload["outcome_kind"] = str(outcome_kind)
    if executed is not None:
        payload["executed"] = bool(executed)
    # Additive (2026-07-17): filesystem paths of render/export deliverables so the
    # GUI can expand a tool-call row inline. Cap 8; omit when none.
    arts = artifacts
    if arts is None and tool_name in _ARTIFACT_PRODUCER_TOOLS:
        # An explicitly passed artifacts= is honoured for ANY tool (the caller is
        # asserting a real deliverable); auto-derivation is producer-gated so read
        # tools (fs_read, describe_*, list_*) never pollute the Artifacts panel.
        try:
            arts = derive_artifacts(result)
        except Exception:  # noqa: BLE001
            arts = None
    if arts:
        payload["artifacts"] = arts[:8]
    # Additive (2026-07-30): the token usage of the completion that PRODUCED this
    # call, so the GUI tool row can show the prompt-cache read next to its
    # timestamp. get_last_usage still holds that completion's record here — the
    # next complete() only runs after the tool batch dispatches. cache_read is
    # context_tokens minus billed prompt_tokens (0 for cache-blind providers).
    # Best-effort: a call with no recorded usage simply omits the key.
    try:
        from services.agent.providers import token_usage as _tu  # noqa: PLC0415
        _u = _tu.get_last_usage()
        if _u:
            _cx = int(_u.get("context_tokens") or 0)
            _pt = int(_u.get("prompt_tokens") or 0)
            payload["usage"] = {
                "prompt_tokens": _pt,
                "context_tokens": _cx,
                "cache_read": max(0, _cx - _pt),
            }
    except Exception:  # noqa: BLE001 — telemetry never blocks the event
        pass
    return "agent.tool_call", payload


# Internal scratch prefix minted by services/mcp/cad_common._tmp() (and its
# callers) via tempfile.mkstemp — those files are transient tool plumbing, NOT
# user deliverables, and must never surface in the GUI Artifacts panel.
_SCRATCH_PREFIX = "chudware_mcp_"


def _is_internal_scratch(path: str) -> bool:
    """True for internal scratch paths (cad_common._tmp tempfiles).

    The primary signal is the ``chudware_mcp_`` basename prefix — a scratch file
    is suppressed wherever it lives. Location alone is NEVER enough: a file the
    user explicitly asked for under a temp-ish directory is a real deliverable
    and must survive. The system-temp-dir check (symlink-resolved so /tmp vs
    /private/tmp both match) only ever ADDS suppression for prefixed names.
    """
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.startswith(_SCRATCH_PREFIX):
        return True
    try:
        tmp = os.path.realpath(tempfile.gettempdir())
        real = os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return False
    return (real.startswith(tmp + os.sep)
            and os.path.basename(real).startswith(_SCRATCH_PREFIX))


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

    def _looks_like_path(p: str) -> bool:
        """A filesystem path, not a sentence.

        The old rule accepted ANY string containing a '/', which let a render
        caption become an artifact row: _scene_caption starts with
        ``"/".join(view_names)`` so a multi-view render captioned
        "top/front/right view of a 58x26x4 mm part. front: …" was listed under
        "Produced This Session" (live 2026-07-28). A path is now recognised by a
        plausible file EXTENSION (or by existing on disk); anything
        sentence-shaped must exist to count.
        """
        if not p or "\n" in p or len(p) > 1024:
            return False
        base = p.replace("\\", "/").rsplit("/", 1)[-1]
        dot = base.rfind(".")
        # ".step+" is Chudware's native suffix — strip the '+' before the check.
        ext = base[dot + 1:].rstrip("+").lower() if 0 < dot < len(base) - 1 else ""
        if not ext or len(ext) > 8 or not ext.isalnum():
            # Extensionless deliverables are real but rare — demand proof.
            return ("/" in p or "\\" in p) and os.path.exists(p)
        if len(p) > 260 or ". " in p or ", " in p or "; " in p:
            return os.path.exists(p)   # prose that happens to end word.word
        return True

    def _add(path: Any) -> None:
        if path is None:
            return
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str):
            # mcp.server.fastmcp.Image (or any object with a filesystem path;
            # its .path is a PosixPath, not a str) and the normalised
            # {"kind": "image", "path": …} dict agent_turn now produces.
            inner = getattr(path, "path", None)
            if isinstance(inner, os.PathLike):
                inner = os.fspath(inner)
            if not isinstance(inner, str):
                return
            path = inner
        p = path.strip()
        if not p or p in seen:
            return
        if _is_internal_scratch(p):
            return
        if not _looks_like_path(p):
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


def role_event(
    node_id: str, *,
    role: str,
    goal: str = "",
    from_role: str = "",
    reason: str = "",
    span_id: str | None = None,
    parent_id: str | None = None,
    actor: str | None = None,
) -> tuple[str, dict]:
    """Build the frozen ``agent.role`` event for replay/import compatibility."""
    payload = _linkage(node_id, span_id=span_id or new_span_id(),
                       parent_id=parent_id, actor=actor)
    payload.update({
        "role": (role or "").strip(),
        "goal": _cap(goal, 280),
        "from_role": (from_role or "").strip(),
        "reason": _cap(reason, 280),
    })
    return "agent.role", payload


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


def provider_usage_event(
    *,
    provider: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    context_tokens: int = 0,
    source: str = "api",
) -> tuple[str, dict]:
    """Per-call provider token usage; feeds the local context meter and turn totals."""
    return "agent.provider_usage", {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "prompt_tokens": max(0, int(prompt_tokens or 0)),
        "completion_tokens": max(0, int(completion_tokens or 0)),
        "total_tokens": max(0, int(total_tokens or 0)),
        "context_tokens": max(0, int(context_tokens or 0)),
        "source": str(source or "api"),
    }


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
