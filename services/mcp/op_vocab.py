"""Op vocabulary bridge — single source from the C++ engine registry.

``doc_list_ops`` / ``ecad_list_ops`` are the live query surface. This module
caches the result and provides:

  * ``doc_ops()`` / ``feature_types()`` for MCP tool schema enumeration
  * ``cheatsheet_snippet()`` for one-shot agent middleware inject
  * ``unknown_op_help(kind)`` for rich rejection text offline

Falls back to ``engine_oracle.doc.op_registry`` when libaicad is unavailable
(unit tests / offline), so middleware tests stay deterministic.
"""
from __future__ import annotations

from typing import Any, Optional

_CACHE: dict[str, Any] | None = None


def _fallback():
    try:
        from engine.doc import op_registry as reg  # type: ignore
        return reg
    except Exception:  # noqa: BLE001
        from engine_oracle.doc import op_registry as reg  # type: ignore
        return reg


def _load_from_core() -> dict[str, Any] | None:
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        if not cb.available():
            return None
        r = cb.call("doc_list_ops", {})
        if not isinstance(r, dict) or not r.get("ops"):
            return None
        return r
    except Exception:  # noqa: BLE001
        return None


def refresh() -> dict[str, Any]:
    """Force-reload the registry (tests / after engine rebuild)."""
    global _CACHE
    core = _load_from_core()
    if core is not None:
        _CACHE = core
        return _CACHE
    reg = _fallback()
    _CACHE = {
        "ok": True,
        "ops": list(reg.doc_op_kinds()),
        "feature_types": list(getattr(reg, "COMMON_FEATURE_TYPES", ())),
        "cheatsheet": reg.doc_ops_cheatsheet_snippet(),
        "hint": "Feature types (box/cylinder/sketch/…) are NOT ops — wrap with "
                "op=add_feature. Call cad_guide() for worked examples.",
        "source": "python_twin",
    }
    return _CACHE


def _cache() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        return refresh()
    return _CACHE


def doc_ops() -> list[str]:
    return list(_cache().get("ops") or [])


def feature_types() -> list[str]:
    return list(_cache().get("feature_types") or [])


def cheatsheet_snippet() -> str:
    s = _cache().get("cheatsheet")
    if isinstance(s, str) and s.strip():
        return s
    return _fallback().doc_ops_cheatsheet_snippet()


def vocab_block_for_tool_description() -> str:
    """Compact vocabulary block for the add_features MCP tool docstring."""
    ops = doc_ops()
    ftypes = feature_types()
    # Keep description bounded for the model context.
    op_head = ", ".join(ops[:30]) + (", …" if len(ops) > 30 else "")
    # Prefer the solid-building types first for thrash recovery.
    preferred = [
        "box", "cylinder", "sphere", "sketch", "extrude", "revolve", "loft",
        "sweep", "fillet", "chamfer", "hole", "mirror", "linear_pattern",
        "circular_pattern", "shell", "boolean", "transform",
    ]
    ft_list = [t for t in preferred if t in set(ftypes)] or preferred
    ft_rest = [t for t in ftypes if t not in set(ft_list)]
    ft_show = ft_list + ft_rest
    ft_head = ", ".join(ft_show[:40]) + (", …" if len(ft_show) > 40 else "")
    return (
        "OP VOCABULARY (closed — do NOT invent names):\n"
        f"  ops (field op=…): {op_head}\n"
        f"  feature types (under op=add_feature only): {ft_head}\n"
        "  WRONG: {\"op\":\"box\",…} or {\"op\":\"sketch\",…} or {\"op\":\"extrude\",…}\n"
        "  RIGHT: {\"op\":\"add_feature\",\"feature\":{\"type\":\"box\"|\"cylinder\"|\"sketch\"|…, …}}\n"
        "  Call cad_guide() for worked examples and full param shapes."
    )


def is_unknown_op_error(result: Any) -> bool:
    """True when a tool result reports an unknown document/ECAD op.

    Handles several shapes seen on the agent path:
      * ``{"ok": False, "error": "… unknown op 'box' …"}`` (engine/CoreError)
      * a bare JSON/text string containing ``unknown op``
      * ``{"ok": True, "result": "<stringified {ok:false,…}>"}`` (some dispatch
        wrappers mark the *call* ok even when the op batch was rejected — mega-7)
    """
    text = ""
    if isinstance(result, dict):
        err = result.get("error") or result.get("message") or ""
        text = str(err)
        nested = result.get("result")
        if isinstance(nested, dict):
            text += " " + str(nested.get("error") or nested.get("message") or "")
            text += " " + str(nested)
        elif isinstance(nested, str):
            text += " " + nested
        # Also scan the whole dict (covers stringified payloads).
        try:
            import json as _json
            text += " " + _json.dumps(result, default=str)
        except Exception:  # noqa: BLE001
            text += " " + str(result)
    elif isinstance(result, str):
        text = result
    else:
        text = str(result or "")
    return "unknown op" in text.lower()

def extract_unknown_op_name(result: Any) -> Optional[str]:
    """Best-effort pull of the invented op name from an error string."""
    import re
    text = ""
    if isinstance(result, dict):
        text = str(result.get("error") or result.get("message") or "")
        nested = result.get("result")
        if isinstance(nested, dict):
            text += " " + str(nested.get("error") or "")
        elif isinstance(nested, str):
            text += " " + nested
    else:
        text = str(result or "")
    m = re.search(r"unknown op '([^']+)'", text)
    return m.group(1) if m else None


def unknown_op_help(kind: str, index: int = 0) -> str:
    """Rich unknown-op message (closest + feature-type hint + cad_guide)."""
    try:
        return _fallback().unknown_doc_op_message(index, kind)
    except Exception:  # noqa: BLE001
        return (
            f"op[{index}]: unknown op '{kind}'. "
            "Feature types (box/cylinder/sketch/…) are NOT ops — wrap with "
            '{"op":"add_feature","feature":{"type":"…",…}}. '
            "See cad_guide()."
        )


def enrich_unknown_op_error(result: Any) -> Any:
    """If *result* is a thin unknown-op error, rewrite with closest/cad_guide text.

    Live mega-8: C++ path returned only ``op[0]: unknown op 'cylinder'`` without
    closest suggestions. MCP/agent teaching depends on the rich form.
    """
    if not is_unknown_op_error(result):
        return result
    unknown = extract_unknown_op_name(result) or "?"
    # Already rich?
    text = ""
    if isinstance(result, dict):
        text = str(result.get("error") or result.get("message") or "")
    else:
        text = str(result or "")
    if "closest:" in text.lower() or "cad_guide" in text.lower():
        return result
    # Extract op index if present.
    import re
    m = re.search(r"op\[(\d+)\]", text)
    idx = int(m.group(1)) if m else 0
    rich = unknown_op_help(unknown, idx)
    if isinstance(result, dict):
        out = dict(result)
        out["error"] = rich
        out["unknown_op"] = unknown
        out["op_vocab_enriched"] = True
        return out
    if isinstance(result, str):
        # Replace thin JSON error field if present.
        try:
            import json as _json
            obj = _json.loads(result)
            if isinstance(obj, dict):
                obj["error"] = rich
                obj["unknown_op"] = unknown
                obj["op_vocab_enriched"] = True
                return _json.dumps(obj)
        except Exception:  # noqa: BLE001
            pass
        return rich
    return result
