"""The JSON envelope conventions shared by the physics / office / graph tool modules.

Four *different* wire conventions are in service across these tools and they are
NOT interchangeable: a family that has always answered ``{"ok": false, "error":
…}`` must keep answering exactly that, and the piping/hydraulic helpers must keep
returning a ``dict`` rather than a string. So each convention is spelled out here
once, on its own terms, with the modules that speak it named:

* :func:`analysis_envelope` — ``antenna_tools``, ``em_tools``: ``str`` envelopes
  keyed ``error``, keyword-only ``hint``/``next_actions``, numpy-aware writer.
* :func:`report_err` — ``heatsink``, ``ecad_reliability``: the same ``error``
  shape, but ``hint``/``next_actions`` are positional and the writer is plain.
* :func:`reason_err` — ``piping_tools``, ``hydraulic_tools``: a ``dict`` keyed
  ``reason``, carrying ``hint`` only when one was supplied.
* :func:`compact_err` / :func:`sorted_ok` — ``office_tools``, ``plm_tools``,
  ``graph_tools``: single-line error strings, key-sorted success strings.

The JSON writer itself is one function; every place the conventions disagree
about ``indent`` / ``sort_keys`` / ``default`` is an argument to it, not a fork.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional


def dumps(payload: Any, *, indent: Optional[int] = 2, sort_keys: bool = False,
          default: Optional[Callable[[Any], Any]] = None) -> str:
    """The single JSON writer behind every convention below."""
    return json.dumps(payload, indent=indent, sort_keys=sort_keys, default=default)


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
        out: dict[str, Any] = {
            "ok": False,
            "error": reason,
            "warnings": [],
            "hint": hint or reason,
            "next_actions": next_actions or [],
        }
        out.update(extra)
        return analysis_dumps(out)

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
    """``error``-keyed refusal string with a positional hint — heatsink / ecad_reliability."""
    out: dict = {"ok": False, "error": reason, "warnings": [],
                 "hint": hint or reason, "next_actions": next_actions or []}
    out.update(extra)
    return plain_dumps(out)


def reason_err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    """``reason``-keyed refusal **dict** (never serialized here) — piping / hydraulic."""
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def compact_err(reason: str, hint: str = "") -> str:
    """Single-line refusal string — office_tools / plm_tools / graph_tools."""
    return dumps({"ok": False, "reason": reason, "hint": hint}, indent=None)


def sorted_ok(**fields: Any) -> str:
    """Key-sorted success string — office_tools / plm_tools."""
    return dumps({"ok": True, **fields}, sort_keys=True)
