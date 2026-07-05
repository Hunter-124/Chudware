"""tool_schemas.py — trimmed tool-schema rendering for the turn prompt (T-W32).

T-W30's REAL gym cycles diagnosed *tool-schema blindness* as the top lever: the
turn prompt carried tool NAMES only, so the model called real tools but guessed
kwargs ("unexpected keyword argument" on every mutating call).  This module renders
the §9.1 **tools slot**: for exactly the node's allowed tools, a compact signature +
description block the model can copy kwargs from, within the tier's ``budgets.tools``
token budget (contracts/advisor.md §8).

Honesty rules:
  * The schema is derived from the REAL dispatchable callable (``inspect.signature``
    on the same function the dispatcher will invoke) — the prompt never promises a
    kwarg the dispatch layer would reject.
  * A tool that cannot be resolved is listed name-only and reported in the meta —
    never a fabricated signature.
  * Token cost is MEASURED (repo-wide 4-chars/token heuristic, the same measuring
    stick as tiers.TurnAssembler) and returned in the meta so callers can record it.

The trim dial is the tier's ``tool_schema_trim`` (tiers.trim_tool_schema — first
sentence of the description only); untrimmed tiers get the first paragraph.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from services.agent.tiers import _count_tokens, trim_tool_schema

# Conservative default when no tier is known: local-24k's budgets.tools value.
# (tiers.yaml is the source; this is a fallback, not a new budget.)
DEFAULT_TOOLS_BUDGET_TOKENS = 2500

# Params that are call-plumbing, not model-suppliable semantics.
_SKIP_PARAMS = frozenset({"self", "ctx", "context"})


def describe_callable(name: str, fn: Callable[..., Any]) -> dict[str, Any]:
    """Build ``{name, signature, description}`` from a real dispatchable callable.

    ``signature`` is a compact python-ish arg list with annotations and defaults —
    exactly what a model needs to emit valid kwargs.  Never raises."""
    params: list[str] = []
    try:
        sig = inspect.signature(fn)
        for pname, p in sig.parameters.items():
            if pname in _SKIP_PARAMS or pname.startswith("_"):
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            part = pname
            if p.annotation is not inspect.Parameter.empty:
                part += f": {_ann(p.annotation)}"
            if p.default is not inspect.Parameter.empty:
                part += f" = {p.default!r}"
            params.append(part)
    except (TypeError, ValueError):
        pass
    doc = inspect.getdoc(fn) or ""
    return {"name": name, "signature": f"{name}({', '.join(params)})",
            "description": doc}


def _ann(annotation: Any) -> str:
    """Render a type annotation compactly (never raises)."""
    try:
        if isinstance(annotation, str):
            return annotation
        if getattr(annotation, "__module__", "") == "builtins":
            return getattr(annotation, "__name__", str(annotation))
        s = str(annotation)
        return s.replace("typing.", "")
    except Exception:  # noqa: BLE001
        return "?"


def _description_line(desc: str, *, trim: bool) -> str:
    """One description line: first sentence when *trim*, else first paragraph."""
    if not desc:
        return ""
    if trim:
        return trim_tool_schema({"description": desc})["description"].strip()
    return desc.split("\n\n", 1)[0].strip()


def render_tools_block(
    allowed_tools: list[str],
    *,
    resolver: Callable[[str], Callable[..., Any] | None],
    budget_tokens: int | None = None,
    trim: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Render the §9.1 tools slot for *allowed_tools* and MEASURE its cost.

    Args:
        allowed_tools: the node's tool allowlist, in role-card order.
        resolver: ``(tool_name) -> callable | None`` — MUST be the same resolution
            the dispatcher uses (mission-scoped first, then the global registry),
            so the rendered schema is dispatch-faithful.
        budget_tokens: the tier's ``budgets.tools`` (default: local-24k's 2500).
        trim: the tier's ``tool_schema_trim`` dial.

    Returns:
        ``(text, meta)`` where meta = {tokens, budget_tokens, rendered, omitted,
        unresolved, trimmed} — tokens is the MEASURED count of the returned text.
    """
    budget = int(budget_tokens) if budget_tokens else DEFAULT_TOOLS_BUDGET_TOKENS
    lines: list[str] = ["[TOOLS] Call tools with EXACTLY these kwargs:"]
    rendered: list[str] = []
    unresolved: list[str] = []
    omitted: list[str] = []
    used = _count_tokens(lines[0])

    for tool in allowed_tools or []:
        fn = None
        try:
            fn = resolver(tool)
        except Exception:  # noqa: BLE001 — a broken resolver entry is unresolved
            fn = None
        if fn is None:
            entry = f"- {tool}(…)  # schema unavailable"
        else:
            schema = describe_callable(tool, fn)
            desc = _description_line(schema["description"], trim=trim)
            entry = f"- {schema['signature']}"
            if desc:
                entry += f"\n    {desc}"
        cost = _count_tokens(entry)
        if used + cost > budget:
            omitted.append(tool)
            continue
        lines.append(entry)
        used += cost
        if fn is None:
            unresolved.append(tool)
        else:
            rendered.append(tool)

    if omitted:
        tail = f"(+{len(omitted)} allowed tools omitted for budget: {', '.join(omitted)})"
        if used + _count_tokens(tail) <= budget:
            lines.append(tail)

    text = "\n".join(lines) if len(lines) > 1 else ""
    meta = {
        "tokens": _count_tokens(text),
        "budget_tokens": budget,
        "rendered": rendered,
        "omitted": omitted,
        "unresolved": unresolved,
        "trimmed": bool(trim),
    }
    return text, meta
