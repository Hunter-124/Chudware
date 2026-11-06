"""The JSON envelope conventions shared by the physics / office / graph tool modules.

Every error writer here emits ONE error shape: ``ok: false``, ``error`` (the message
string), ``code`` (a stable snake_case token), and — only when there is something to say —
``hint`` and ``detail``. A writer that has always answered ``reason`` keeps that key as an
alias of ``error``, so a caller that reads either finds the message. ``hint`` is never a
copy of ``error``: it appears only when the writer has advice the message lacks.

The conventions still differ in their *wire form*, and the differences are real: a family
that has always answered a ``str`` must keep answering a ``str``, and the piping/hydraulic
helpers must keep returning a ``dict``. Each is spelled out once, on its own terms, with
the modules that speak it named:

* :func:`analysis_envelope` — ``antenna_tools``, ``em_tools``: ``str`` envelopes with
  keyword-only ``hint``/``next_actions`` and a numpy-aware writer.
* :func:`report_err` — ``heatsink``, ``ecad_reliability``: the same error shape, but
  ``hint``/``next_actions`` are positional and the writer is plain.
* :func:`reason_err` — ``piping_tools``, ``hydraulic_tools``: a ``dict``.
* :func:`compact_err` / :func:`sorted_ok` — ``office_tools``, ``plm_tools``,
  ``graph_tools``: single-line error strings, key-sorted success strings.
* :func:`error_payload` — the shape itself, for modules that build their own dict.

The JSON writer itself is one function; every place the conventions disagree
about ``indent`` / ``sort_keys`` / ``default`` is an argument to it, not a fork.
:func:`dumps` is also the shared reply path that stamps ``engine: "private"`` onto a reply
while the process is served by a private in-process engine (see :func:`stamp_engine`).
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Optional

_CODE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")


def stamp_engine(payload: Any) -> Any:
    """*payload* with ``engine: "private"`` and its warning added while hostd was
    unreachable and a standalone client fell back to a private in-process engine.

    Replies from such an engine describe documents the app and other agents cannot see, so
    the caller must be told on every reply, not only in a log line. Any other payload (or
    the healthy path) comes back unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    bridge = sys.modules.get(f"{__package__}.core_bridge")
    notice = bridge.private_engine_notice() if bridge is not None else None
    return payload if notice is None else {**payload, **notice}


def dumps(payload: Any, *, indent: Optional[int] = 2, sort_keys: bool = False,
          default: Optional[Callable[[Any], Any]] = None) -> str:
    """The single JSON writer behind every convention below."""
    return json.dumps(stamp_engine(normalize_error(payload)), indent=indent, sort_keys=sort_keys,
                      default=default)


def code_for(message: Any) -> str:
    """A stable machine token for a refusal: the message itself when it already is a
    snake_case token (the ``reason="no_circuit"`` convention), else the generic ``error``."""
    text = str(message)
    return text if _CODE_TOKEN.match(text) else "error"


def normalize_error(payload: Any) -> Any:
    """Give reason-only or structured-error refusals a top-level message and code."""
    if not isinstance(payload, dict) or payload.get("ok") is True:
        return payload
    error = payload.get("error")
    reason = payload.get("reason")
    if error is None and (payload.get("ok") is not False or reason is None):
        return payload
    nested = error if isinstance(error, dict) else {}
    message = nested.get("message") or nested.get("error") if nested else error
    message = str(message if message is not None else reason)
    code = payload.get("code") or nested.get("code") or code_for(message)
    if error == message and payload.get("code") == code:
        return payload
    result = {**payload, "error": message, "code": str(code)}
    if nested and "detail" not in result and nested.get("detail") is not None:
        result["detail"] = nested["detail"]
    if nested and "hint" not in result and nested.get("hint"):
        result["hint"] = nested["hint"]
    return result


def tool_reply(value: Any) -> Any:
    """Normalize tool refusals and add private-engine disclosure without changing schema."""
    if isinstance(value, dict):
        return stamp_engine(normalize_error(value))
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(parsed, dict):
            result = stamp_engine(normalize_error(parsed))
            if result is not parsed:
                return json.dumps(result, ensure_ascii=False)
    return value


def error_payload(message: Any, *, code: str = "", hint: str = "", detail: Any = None,
                  **extra: Any) -> dict:
    """The one error shape: ``{ok: false, error, code, [hint], [detail], **extra}``.

    ``code`` defaults to :func:`code_for`. ``hint`` and ``detail`` are present only when
    given; a hint that merely repeats the message is not advice, so it is never invented.
    """
    text = str(message)
    out: dict[str, Any] = {**extra, "ok": False, "error": text, "code": code or code_for(text)}
    if hint:
        out["hint"] = hint
    if detail is not None:
        out["detail"] = detail
    return out


def json_default(obj: Any, *, mark_callables: bool = False) -> Any:
    """Fallback for values ``json`` cannot write.

    numpy scalars/arrays (which leak out of worker payloads) become plain Python
    values; anything else is stringified. ``mark_callables`` renders functions as
    ``"<callable>"`` instead of their address-bearing repr.
    """
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:  # noqa: BLE001
        pass
    if mark_callables and callable(obj):
        return "<callable>"
    return str(obj)


def plain_dumps(payload: dict) -> str:
    """Indented JSON with no coercion — heatsink / ecad_reliability / piping / hydraulic."""
    return dumps(payload)


def stringly_dumps(obj: Any) -> str:
    """Indented JSON with a ``str`` fallback for exotic values — graph_tools."""
    return dumps(obj, default=str)


def analysis_envelope(*, mark_callables: bool = False) -> tuple[
        Callable[..., str], Callable[..., str], Callable[..., str]]:
    """The antenna_tools / em_tools convention, as ``(dumps, err, ok)``.

    The three helpers share one serializer, so the numpy-default difference
    between the two modules is bound here once instead of forking the envelope
    builders: ``antenna_tools`` marks callables, ``em_tools`` does not.
    """
    def _default(obj: Any) -> Any:
        return json_default(obj, mark_callables=mark_callables)

    def analysis_dumps(payload: dict) -> str:
        return dumps(payload, default=_default)

    def analysis_err(reason: str, *, hint: str = "", next_actions: Optional[list] = None,
                     **extra: Any) -> str:
        extra.setdefault("warnings", [])
        extra.setdefault("next_actions", next_actions or [])
        return analysis_dumps(error_payload(reason, hint=hint, **extra))

    def analysis_ok(payload: dict, *, hint: str = "", next_actions: Optional[list] = None,
                    warnings: Optional[list] = None) -> str:
        out = {
            "ok": True,
            "warnings": list(warnings or []),
            "hint": hint,
            "next_actions": list(next_actions or []),
            **payload,
        }
        return analysis_dumps(out)

    return analysis_dumps, analysis_err, analysis_ok


def report_err(reason: str, hint: str = "", next_actions: Optional[list] = None,
               **extra: Any) -> str:
    """Error string with a positional hint — heatsink / ecad_reliability."""
    extra.setdefault("warnings", [])
    extra.setdefault("next_actions", next_actions or [])
    return plain_dumps(error_payload(reason, hint=hint, **extra))


def reason_err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    """Refusal **dict** (never serialized here) — piping / hydraulic.

    ``reason`` is kept beside ``error`` for the callers that have always read it.
    """
    return error_payload(reason, hint=hint or "", reason=reason, **extra)


def compact_err(reason: str, hint: str = "") -> str:
    """Single-line refusal string — office_tools / plm_tools / graph_tools."""
    return dumps(error_payload(reason, hint=hint, reason=reason), indent=None)


def sorted_ok(**fields: Any) -> str:
    """Key-sorted success string — office_tools / plm_tools."""
    return dumps({"ok": True, **fields}, sort_keys=True)
