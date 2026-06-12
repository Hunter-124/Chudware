"""Ask mode (Wave D) — answer QUESTIONS about the open parametric part, mutating nothing.

The worker's ``agent_ask_v2`` method is a thin adapter over :func:`answer_question`: it resolves
the doc session (a missing/empty doc is fine — the assistant answers about general CAD usage
instead of erroring), assembles ONE compact prompt from the live document (feature list with ids/
types/names/key params, doc status, units), makes ONE provider call, and returns the text.

Everything prompt-shaped lives here so it is testable without the worker subprocess:
``build_doc_context`` (the document digest), ``build_ask_prompt`` (system + messages) and
``answer_question`` (the single provider call).
"""
from __future__ import annotations

from typing import Optional

from .providers.base import Provider, text_part, user

# The role marker the mock RoleScriptedProvider routes on (run.py uses the same convention for
# the orchestrator roles). Harmless context for real models.
ASK_ROLE = "ask"

_MAX_FEATURE_LINES = 60
_MAX_CONTEXT_CHARS = 8000

_SYSTEM = (
    "You are the AI-CAD assistant. You answer QUESTIONS about the user's parametric CAD part. "
    "Be concise and concrete: quote the real feature names, ids and dimensions from the document "
    "context below your instructions (units are stated there; the default MMGS means millimeters, "
    "grams, seconds). You never modify the document in this mode — if the user asks for a change, "
    "tell them briefly to use Edit-part mode so the change is applied as validated document ops. "
    "If no document is open, answer as a general CAD / AI-CAD usage assistant."
)


def _param_summary(params: dict, limit: int = 8) -> str:
    """A short ``k=v`` summary of a feature's params (dim objects + plain scalars)."""
    out: list[str] = []
    for k, v in list((params or {}).items())[:limit]:
        if isinstance(v, dict) and "value" in v:
            out.append(f"{k}={v['value']}")
        elif isinstance(v, (int, float, str, bool)):
            out.append(f"{k}={v}")
    return ", ".join(out)


def summarize_features(doc: dict) -> str:
    """One line per feature: ``<id> <type> <name> (key params)`` (+ a [suppressed] marker)."""
    feats = (doc or {}).get("features", []) or []
    lines: list[str] = []
    for f in feats[:_MAX_FEATURE_LINES]:
        if not isinstance(f, dict):
            continue
        sup = " [suppressed]" if f.get("suppressed") else ""
        line = f"{f.get('id', '?')} {f.get('type', '?')} {f.get('name', '')}{sup}".rstrip()
        dims = _param_summary(f.get("params") or {})
        if dims:
            line += f" ({dims})"
        lines.append(line)
    if len(feats) > _MAX_FEATURE_LINES:
        lines.append(f"... (+{len(feats) - _MAX_FEATURE_LINES} more features)")
    return "\n".join(lines)


def _status_line(session) -> str:
    """Doc build status from the session's last build (best-effort, never raises)."""
    try:
        res = getattr(session, "last_result", None)
        fs = list(getattr(res, "feature_status", []) or []) if res is not None else []
        if not fs:
            return "STATUS: not built yet."
        bad = [s for s in fs if not s.get("ok")]
        if not bad:
            return f"STATUS: all {len(fs)} feature(s) compile cleanly."
        errs = "; ".join(f"{s.get('id', '?')}: {s.get('error')}" for s in bad[:4])
        return f"STATUS: {len(bad)} of {len(fs)} feature(s) have errors — {errs}"
    except Exception:  # noqa: BLE001
        return "STATUS: unknown."


def build_doc_context(session) -> str:
    """The compact document context block for the ask prompt.

    Handles a missing session or an empty doc gracefully — the assistant is told no document is
    open so it answers about general CAD usage instead of erroring.
    """
    doc = getattr(session, "doc", None) if session is not None else None
    feats = (doc or {}).get("features", []) or []
    if doc is None or not isinstance(doc, dict):
        return ("NO DOCUMENT IS OPEN. Answer as a general CAD / AI-CAD usage assistant "
                "(suggest Design-new mode to start a part).")
    units = ((doc.get("units") or {}).get("system")) or "MMGS"
    head = (f"DOCUMENT: {doc.get('name', 'Part')}  "
            f"(id {getattr(session, 'doc_id', '?')}, revision {getattr(session, 'revision', 0)})\n"
            f"UNITS: {units}")
    if not feats:
        return head + "\nFEATURES: none yet — the part is empty."
    body = (f"{head}\nFEATURES ({len(feats)}):\n{summarize_features(doc)}\n{_status_line(session)}")
    if len(body) > _MAX_CONTEXT_CHARS:
        body = body[:_MAX_CONTEXT_CHARS] + "\n...[context truncated]"
    return body


def build_ask_prompt(question: str, session=None) -> tuple[str, list[dict]]:
    """Assemble ``(system, messages)`` for the single ask-mode provider call."""
    system = f"ROLE: {ASK_ROLE}\n{_SYSTEM}"
    body = f"{build_doc_context(session)}\n\nQUESTION:\n{question}"
    return system, [user([text_part(body)])]


def answer_question(question: str, session=None, provider: Optional[Provider] = None) -> str:
    """Answer *question* about *session*'s part with ONE provider call. Mutates nothing."""
    if provider is None:
        from . import config
        provider = config.get_provider()
    system, messages = build_ask_prompt(question, session)
    reply = provider.complete(system, messages, max_tokens=1024)
    return (reply or "").strip() or (
        "I couldn't produce an answer for that — try rephrasing the question.")
