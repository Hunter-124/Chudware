"""Op vocabulary bridge — the single source is the C++ engine registry.

``doc_list_ops`` is the live query surface: the closed op list, the feature types, the
param keys each feature type reads (``params``) and the cheatsheet text all come from
``core/doc/op_registry`` / ``core/doc/validate.cpp``. This module caches that one reply
and provides:

  * ``doc_ops()`` / ``feature_types()`` / ``feature_params()`` for tool schema enumeration
    and the dialect normalizers
  * ``cheatsheet_snippet()`` for one-shot agent middleware inject
  * ``is_unknown_op_error()`` / ``extract_unknown_op_name()`` to recognise an engine
    unknown-op rejection

There is no Python copy of the vocabulary. When the engine cannot be reached every
accessor raises ``OpVocabUnavailable`` with the reason, rather than answering from a
second list that can drift.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_CACHE: dict[str, Any] | None = None


class OpVocabUnavailable(RuntimeError):
    """The engine's ``doc_list_ops`` reply could not be obtained."""


def _load_from_core() -> dict[str, Any]:
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        if not cb.available():
            raise OpVocabUnavailable("the engine (libchudware) is not loaded")
        r = cb.call("doc_list_ops", {})
    except OpVocabUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpVocabUnavailable(f"doc_list_ops failed: {exc}") from exc
    if not isinstance(r, dict) or not r.get("ops"):
        raise OpVocabUnavailable("doc_list_ops returned no op list")
    return r


def refresh() -> dict[str, Any]:
    """Force-reload the registry from the engine (tests / after an engine rebuild)."""
    global _CACHE
    _CACHE = _load_from_core()
    return _CACHE


def _cache() -> dict[str, Any]:
    if _CACHE is None:
        return refresh()
    return _CACHE


def doc_ops() -> list[str]:
    return list(_cache().get("ops") or [])


def feature_types() -> list[str]:
    return list(_cache().get("feature_types") or [])


def feature_params() -> dict[str, list[str]]:
    """Feature type -> the parameter keys enforced by the engine."""
    params = _cache().get("params")
    if not isinstance(params, dict):
        raise OpVocabUnavailable("doc_list_ops returned no per-feature parameter schema")
    return {str(k): list(v) for k, v in params.items()}


def cheatsheet_snippet() -> str:
    s = _cache().get("cheatsheet")
    if isinstance(s, str) and s.strip():
        return s
    raise OpVocabUnavailable("doc_list_ops carried no cheatsheet")


def _error_text(result: Any) -> str:
    """The error message of a tool result, and nothing else. A dict contributes its
    `error`; a JSON string is parsed first; a plain non-JSON string is itself the message.
    A dispatch wrapper that marks the call ok while carrying the rejected batch under
    `result` is looked through. Success payloads (notes, interpreted lists, tree text) are
    never scanned: a part named "unknown op" is not an unknown-op rejection."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return result
        return _error_text(parsed) if isinstance(parsed, dict) else result
    if not isinstance(result, dict):
        return ""
    err = result.get("error")
    if isinstance(err, str) and err:
        return err
    nested = result.get("result")
    if isinstance(nested, (dict, str)):
        return _error_text(nested)
    return ""


def is_unknown_op_error(result: Any) -> bool:
    """True when a tool result's ``error`` reports an unknown document/ECAD op."""
    return "unknown op" in _error_text(result).lower()


def extract_unknown_op_name(result: Any) -> Optional[str]:
    """The invented op name quoted in an engine unknown-op error, if there is one."""
    m = re.search(r"unknown op '([^']+)'", _error_text(result))
    return m.group(1) if m else None
