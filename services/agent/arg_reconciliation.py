"""Argument-name reconciliation shared by BOTH tool-dispatch paths.

Two callers reach the same tool functions with different calling conventions:

- the built-in agent (``services/agent/agent_turn.py::_bind_tool`` /
  ``_reconcile_kwargs``) resolves a tool to its raw python callable and calls
  ``fn(**kwargs)`` directly, bypassing FastMCP's ``ToolManager`` entirely;
- external MCP clients (``services/mcp/server.py::_install_arg_teaching``) go
  through ``ToolManager.call_tool``, which validates arguments against a
  pydantic model built from the same function's signature.

Both need to answer the same question for an argument name that is NOT a real
parameter: is it an unambiguous synonym of one (accept it, echo what it was
interpreted as), or a genuine mistake (reject it, teach the real parameter
set)? That decision — the synonym tables, the name-matching heuristics, and
the shape of the teaching payload — used to be implemented twice and had
already drifted (``export_part(format=...)`` was fixed for the agent path and
never ported to the MCP path). This module is the ONE place it lives now;
both dispatch paths import it, so a fix here reaches both by construction.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# Explicit argument-name synonyms the affix heuristic in `resolve_unknown` can
# never reach ("format" shares no prefix/suffix with "fmt"). Live 2026-07-27
# (claude-opus, flyswatter run): export_part(format="step") was rejected
# "no parameter of that name and no unambiguous synonym" even though `format`
# IS the canonical English name of that parameter. Keyed by the NORMALISED
# supplied name -> normalised target; applied only when exactly one free
# parameter matches the target and its annotation admits the value, so a
# synonym can never outvote a real parameter or force a wrong-typed call.
ARG_SYNONYMS: dict[str, str] = {
    "format": "fmt",
    "fileformat": "fmt",
}

# Synonyms that are only unambiguous for ONE tool. "type" is far too generic
# to map globally, but export_part(fmt, path, doc_id) has no other parameter
# a str `type=` could mean.
TOOL_ARG_SYNONYMS: dict[str, dict[str, str]] = {
    "export_part": {"type": "fmt"},
}


def synonyms_for(tool: str) -> dict[str, str]:
    """The global synonym table plus *tool*'s own overrides, merged."""
    merged = dict(ARG_SYNONYMS)
    merged.update(TOOL_ARG_SYNONYMS.get(tool) or {})
    return merged


def norm_arg(name: str) -> str:
    """Fold an argument name for matching: case/underscore/dash/space are noise."""
    return str(name).lower().replace("_", "").replace("-", "").replace(" ", "")


def annotation_admits(ann: str, value: Any) -> bool:
    """True when *value* is plausibly what annotation *ann* asks for.

    The synonym mapper's safety catch: describe_project(run_id="m1") must NOT be
    rewritten to run=<str> when ``run`` is annotated ``Optional[dict]`` — a
    silently wrong call is worse than an honest teaching error. Ambiguous or
    absent annotations admit everything (we only ever REFUSE on a clear clash).

    *ann* accepts EITHER vocabulary: a lowercased python ``repr(annotation)``
    string (the agent-turn path, e.g. ``"optional[dict]"``) or a JSON-Schema
    type name (the MCP path, e.g. ``"object"``) — both map onto the same five
    kinds below, so one function serves both callers.
    """
    if not ann or value is None:
        return True
    kinds = {
        "dict": ("dict" in ann or "mapping" in ann or "object" in ann),
        "list": ("list" in ann or "sequence" in ann or "tuple" in ann or "array" in ann),
        "str": ("str" in ann or "string" in ann),
        "num": ("int" in ann or "float" in ann or "number" in ann or "integer" in ann),
        "bool": ("bool" in ann or "boolean" in ann),
    }
    hit = [k for k, v in kinds.items() if v]
    if len(hit) != 1:
        return True  # union / unannotated / unrecognised → cannot judge
    kind = hit[0]
    if kind == "dict":
        return isinstance(value, dict)
    if kind == "list":
        return isinstance(value, (list, tuple))
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def resolve_unknown(supplied: str, value: Any, params: Sequence[str],
                    ann: Mapping[str, str], used: set,
                    synonyms: Mapping[str, str] | None = None) -> str | None:
    """Map an unknown argument onto a real parameter, or None.

    Only UNAMBIGUOUS mappings are made: an exact match ignoring case and
    underscores, an explicit synonym (``synonyms_for(tool)``) whose target is
    exactly one free parameter, or an affix relation (one name is a
    prefix/suffix of the other, e.g. shaft_diameter -> diameter) that exactly
    ONE free parameter satisfies and whose annotation admits the supplied
    value. *params* is every parameter the tool accepts; *used* is the subset
    already bound by this call (so a synonym can never steal a slot a real
    argument already filled)."""
    free = [p for p in params if p not in used]
    ns = norm_arg(supplied)
    if not ns:
        return None
    exact = [p for p in free if norm_arg(p) == ns]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if synonyms:
        want = synonyms.get(ns)
        if want:
            cand = [p for p in free if norm_arg(p) == want]
            if len(cand) == 1 and annotation_admits(ann.get(cand[0], ""), value):
                return cand[0]
    affix = []
    for p in free:
        np = norm_arg(p)
        if min(len(np), len(ns)) < 3:
            continue  # 1–2 char names collide by accident, never by intent
        if ns.startswith(np) or ns.endswith(np) or np.startswith(ns) or np.endswith(ns):
            affix.append(p)
    if len(affix) != 1:
        return None
    target = affix[0]
    return target if annotation_admits(ann.get(target, ""), value) else None


def unknown_arg_payload(tool: str, params: Sequence[str], required: Sequence[str],
                        supplied: Mapping[str, Any], unmapped: Sequence[str],
                        config_keys: Sequence[str] = ()) -> dict:
    """Teaching error for argument names that are not (and could not be
    unambiguously mapped onto) a parameter of *tool*. Shared payload SHAPE
    used by both dispatch paths — the anti-drift property this module exists
    for: the same bad call produces the same keys, not just "similar" ones.
    """
    good = {k: v for k, v in supplied.items() if k in params}
    corrected = [f"{k}={v!r}" for k, v in good.items()]
    corrected += [f"{p}=<{p}>" for p in params if p in required and p not in good]
    payload = {
        "ok": False,
        "status": "unknown_argument",
        "error": (
            f"{tool}() got unknown argument(s) "
            f"{', '.join(repr(u) for u in unmapped)} — no parameter of that name "
            f"and no unambiguous synonym. The call was NOT made (nothing was "
            f"dropped silently). Re-issue it using only the accepted parameters."
        ),
        "accepted_params": [
            f"{p} (required)" if p in required else f"{p} (optional)" for p in params
        ],
        "unknown_params": list(unmapped),
        "hint": f"{tool}({', '.join(corrected)})",
    }
    config_keys = set(config_keys)
    from_config = [u for u in unmapped if u in config_keys]
    if from_config:
        # Not the model's mistake: a param_default the user configured is not a
        # parameter of this tool. Say so, or the model burns the turn rephrasing.
        payload["from_config"] = from_config
        payload["error"] += (
            f" NOTE: {', '.join(repr(c) for c in from_config)} came from "
            f"agent.tool_options param_defaults (Settings → AI → Tools), NOT "
            f"from your call — you cannot fix it by rephrasing; report it."
        )
    try:
        from services.agent.tool_hints import hint_for  # noqa: PLC0415
        extra = hint_for(tool)
        if extra:
            payload["tool_hint"] = extra
    except Exception:  # noqa: BLE001
        pass
    return payload
