"""Worker-RPC bridge for session commands (/compact, /shake, /handoff, /btw).

Host passes its ChatMsg transcript as ``messages[]``; each RPC returns a
transformed result the host applies to its store. Pure transforms — never
mutate the host transcript server-side.

Registered append-only via ``WORKER_METHODS.update`` in
``engine_oracle/parts_hub/worker_rpc.py`` (same try/except pattern as
``subscription_rpc`` / ``skills_rpc``).

Contract shapes (batch 2b shared):

* ``session_compact``
  in:  {messages, strategy, keep_recent_tokens?, supersede_reads?,
        drop_useless?, prior_archive?}
  out: {kept_messages, archive?, summary, short_summary}

* ``session_shake``
  in:  {messages, mode, aggressive?}
  out: {messages, saved_tokens, notice}

* ``session_handoff``
  in:  {messages, focus?, provider, model}
  out: {document}

* ``session_btw``
  in:  {messages, question, provider, model}
  out: {answer}
"""
from __future__ import annotations

from typing import Callable


def _cmds():
    try:
        from services.agent import session_cmds as sc
    except Exception:  # noqa: BLE001 — agent package alias
        from agent import session_cmds as sc  # type: ignore
    return sc


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _session_compact(params: dict, _emit: Callable) -> dict:
    """``session_compact`` — snapcompact / shake / summary / off over host messages."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        strategy = str(p.get("strategy") or "off")
        keep = p.get("keep_recent_tokens", 20000)
        try:
            keep_i = int(keep)
        except (TypeError, ValueError):
            keep_i = 20000
        supersede = p.get("supersede_reads", True)
        drop_useless = p.get("drop_useless", True)
        prior = p.get("prior_archive")
        if prior is not None and not isinstance(prior, dict):
            prior = None
        result = _cmds().compact_session(
            messages,
            strategy,
            keep_recent_tokens=keep_i,
            supersede_reads=bool(supersede),
            drop_useless=bool(drop_useless),
            prior_archive=prior,
        )
        out = {
            "ok": True,
            "kept_messages": result.get("kept_messages") or [],
            "summary": result.get("summary") or "",
            "short_summary": result.get("short_summary") or "",
        }
        if result.get("archive") is not None:
            out["archive"] = result["archive"]
        if "cut_index" in result:
            out["cut_index"] = result["cut_index"]
        if "saved_tokens" in result:
            out["saved_tokens"] = result["saved_tokens"]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_compact failed: {exc}", "hint": ""}


def _session_shake(params: dict, _emit: Callable) -> dict:
    """``session_shake`` — mechanical elision of heavy tool/code content."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        mode = str(p.get("mode") or "elide")
        aggressive = bool(p.get("aggressive", False))
        keep_boundary = p.get("keep_boundary_index")
        if keep_boundary is not None:
            try:
                keep_boundary = int(keep_boundary)
            except (TypeError, ValueError):
                keep_boundary = None
        result = _cmds().shake_session(
            messages,
            mode=mode,
            aggressive=aggressive,
            keep_boundary_index=keep_boundary,
        )
        return {
            "ok": True,
            "messages": result.get("messages") or [],
            "saved_tokens": int(result.get("saved_tokens") or 0),
            "notice": result.get("notice") or "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_shake failed: {exc}", "hint": ""}


def _session_handoff(params: dict, _emit: Callable) -> dict:
    """``session_handoff`` — oneshot handoff document (host creates new session)."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        focus = p.get("focus")
        if focus is not None:
            focus = str(focus)
        provider = p.get("provider")
        model = p.get("model")
        if model is not None:
            model = str(model)
        document = _cmds().handoff_document(
            messages,
            focus=focus,
            provider=provider,
            model=model,
        )
        return {"ok": True, "document": document or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_handoff failed: {exc}", "hint": ""}


def _session_btw(params: dict, _emit: Callable) -> dict:
    """``session_btw`` — ephemeral side Q&A; history not mutated."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        question = str(p.get("question") or "").strip()
        if not question:
            return {"ok": False, "reason": "question required", "hint": "session_btw {messages, question, provider, model}"}
        provider = p.get("provider")
        model = p.get("model")
        if model is not None:
            model = str(model)
        answer = _cmds().btw_answer(
            messages,
            question,
            provider=provider,
            model=model,
        )
        return {"ok": True, "answer": answer or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_btw failed: {exc}", "hint": ""}


SESSION_METHODS: dict[str, Callable] = {
    "session_compact": _session_compact,
    "session_shake": _session_shake,
    "session_handoff": _session_handoff,
    "session_btw": _session_btw,
}
