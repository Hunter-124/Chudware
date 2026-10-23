"""tool_schemas.py — trimmed tool-schema rendering for the turn prompt.

Benchmark gym cycles diagnosed *tool-schema blindness* as the top lever: the
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


def tier_tools_slot(tier: str | None) -> tuple[int, bool]:
    """``(budgets.tools, tool_schema_trim)`` for *tier*, from tiers.yaml.

    Every caller used to omit both, so every run — frontier desktop copilot
    included — silently got this module's local-24k fallback (2500 tokens,
    first-sentence trim) instead of its own tier's allocation. Frontier grants
    10000 and sets ``tool_schema_trim: false``; at 2500 the single most-called
    tool (``add_features``, ~2750 tokens of authored guidance) cannot fit at all.

    Unknown/unset tier falls back to the conservative default rather than
    guessing generously — an over-large slot is a real context cost.
    """
    if tier:
        try:
            from services.agent.tiers import (  # noqa: PLC0415
                profile_tier_for,
                load_tier,
            )
            profile = load_tier(profile_tier_for(tier) or tier)
        except Exception:  # noqa: BLE001 — an unknown tier name is not fatal
            pass
        else:
            return int(profile.budgets.tools), bool(profile.tool_schema_trim)
    return DEFAULT_TOOLS_BUDGET_TOKENS, True

# Params that are call-plumbing, not model-suppliable semantics.
_SKIP_PARAMS = frozenset({"self", "ctx", "context", "ask_human"})

# Hand-written schemas for agent pseudo-tools that have no live registry
# callable outside a mission dispatcher (signatures mirror
# mission_dispatch.MissionDispatch.delegate).
_FALLBACK_SCHEMAS: dict[str, str] = {
    "enable_tools": (
        "- enable_tools(categories: list[str])\n"
        "    Request more tool categories mid-turn "
        "(see [TOOL CATEGORIES] menu)."
    ),
    "list_tool_categories": (
        "- list_tool_categories()\n"
        "    List requestable tool category packs (name + one line)."
    ),
    "design_recipe": (
        "- design_recipe(query: str, max_results: int = 3)\n"
        "    NL intent → ordered tool recipe steps (empty query = catalog)."
    ),
    "delegate": (
        "- delegate(role: str = '', brief: str = '', budget_tokens: int = 0, "
        "kind: str = 'worker', parent_id: str | None = None, docs: list | None = None)\n"
        "    Spawn a sub-agent role with an isolated brief and token budget."
    ),
}


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
    """One description line: first sentence when *trim*, else first paragraph.

    Internal newlines collapse to single spaces so a multi-line source
    paragraph can never break the tools-slot block indent.
    """
    if not desc:
        return ""
    if trim:
        out = trim_tool_schema({"description": desc})["description"]
    else:
        out = desc.split("\n\n", 1)[0]
    return " ".join(out.split())


def _description_block(desc: str) -> str:
    """The WHOLE docstring, indented into the tools block.

    Unlike ``_description_line`` this preserves line structure: the authored
    guidance is mostly worked examples (JSON op envelopes, ref selectors, alias
    tables) whose meaning lives in their line breaks and indentation. Blank lines
    keep the block indent so the slot can never break out of its own frame.
    """
    out = []
    for raw in desc.strip().splitlines():
        line = raw.rstrip()
        out.append(f"    {line}" if line else "    ")
    return "\n".join(out)


def render_tools_block(
    allowed_tools: list[str],
    *,
    resolver: Callable[[str], Callable[..., Any] | None],
    budget_tokens: int | None = None,
    trim: bool | None = None,
    tier: str | None = None,
    include_category_menu: bool | None = None,
    expand: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Render the §9.1 tools slot for *allowed_tools* and MEASURE its cost.

    Args:
        allowed_tools: the node's tool allowlist, in role-card order.
        resolver: ``(tool_name) -> callable | None`` — MUST be the same resolution
            the dispatcher uses (mission-scoped first, then the global registry),
            so the rendered schema is dispatch-faithful.
        budget_tokens: the tier's ``budgets.tools``. Omit to take it from *tier*.
        trim: the tier's ``tool_schema_trim`` dial. Omit to take it from *tier*.
        tier: tier name, used to resolve the two above from tiers.yaml when they
            are not passed explicitly. Omit both and pass no tier and you get the
            conservative local-24k fallback.
        include_category_menu: when True, append a compact category pack menu +
            enable_tools / list_tool_categories meta schemas (anti-context-flood).
            Default: auto when the allowlist looks restricted (contains the meta
            tools or is a finite floor, not the full catalog).
        expand: spend leftover budget upgrading tools from their first-paragraph
            floor to their FULL authored docstring, in allowlist order. On by
            default — the floor alone discards ~87% of the guidance the tools were
            written to give. Pass False for a floor-only render (prompt previews,
            and as the kill switch if a tier ever needs the slot minimal).

    Returns:
        ``(text, meta)`` where meta = {tokens, budget_tokens, rendered, omitted,
        unresolved, disabled, trimmed, compacted, expanded, categories} — tokens
        is the MEASURED count of the returned text, ``expanded`` names the tools
        that got their full docstring, ``compacted`` names first-paragraph floors
        reduced to their first sentence to fund one of those authored blocks, and
        ``disabled`` names tools dropped because Settings → AI → Tools has them
        switched off (never advertise a tool whose every call dispatch would
        refuse).
    """
    tier_budget, tier_trim = tier_tools_slot(tier)
    budget = int(budget_tokens) if budget_tokens else tier_budget
    trim = tier_trim if trim is None else bool(trim)
    lines: list[str] = ["[TOOLS] Call tools with EXACTLY these kwargs:"]
    rendered: list[str] = []
    unresolved: list[str] = []
    omitted: list[str] = []
    disabled: list[str] = []
    used = _count_tokens(lines[0])

    allow_list = list(allowed_tools or [])
    # Auto: show category menu for restricted floors that already carry meta tools
    # (SHARED_DEFAULT_TOOLS) — never flood unrestricted / huge role cards.
    if include_category_menu is None:
        include_category_menu = (
            "enable_tools" in allow_list or "list_tool_categories" in allow_list
        )

    # Pass 1 — every tool at its FLOOR cost (first sentence / first paragraph), so
    # one tool's prose can never push another tool out of the slot entirely.
    slots: list[dict[str, Any]] = []
    for tool in allow_list:
        fn = None
        try:
            fn = resolver(tool)
        except Exception:  # noqa: BLE001 — a broken resolver entry is unresolved
            fn = None
        full: str | None = None
        compact: str | None = None
        if fn is None:
            # Built-in meta-tool schemas when no live callable is registered yet.
            entry = _FALLBACK_SCHEMAS.get(tool) or f"- {tool}(…)  # schema unavailable"
        else:
            schema = describe_callable(tool, fn)
            # Per-tool agent.tool_options may override the included tool context
            # (description) and the tier's schema_trim dial.
            use_trim = trim
            note = ""
            defaults: dict[str, Any] = {}
            call_cap: int | None = None
            try:
                from services.agent.config import (  # noqa: PLC0415
                    effective_tool_call_cap,
                    get_tool_options,
                )
                opts = get_tool_options(None, tool) or {}
                if opts.get("enabled") is False:
                    # A tool switched OFF in Settings → AI → Tools can only be
                    # refused at dispatch, so advertising it spends a completion
                    # on a call that cannot run. Drop it from the slot entirely.
                    disabled.append(tool)
                    continue
                override = (opts.get("description_override") or "").strip()
                if override:
                    schema = dict(schema)
                    schema["description"] = override
                st = opts.get("schema_trim")
                if st is not None:
                    use_trim = bool(st)
                note = (opts.get("prompt_note") or "").strip()
                defaults = dict(opts.get("param_defaults") or {})
                call_cap = effective_tool_call_cap(tool, opts=opts)
            except Exception:  # noqa: BLE001
                pass
            desc = _description_line(schema["description"], trim=use_trim)
            compact_desc = _description_line(schema["description"], trim=True)
            head = f"- {schema['signature']}"
            default_note = ""
            if defaults:
                rendered_defaults = ", ".join(
                    f"{key}={value!r}" for key, value in sorted(defaults.items())
                )
                default_note = (
                    f"\n    Effective defaults when omitted: {rendered_defaults}."
                )
            # An UNDISCLOSED cap is a guardrail the model cannot plan around: it
            # budgets for the work it intends, then a mid-chain call is refused
            # with no warning (live 2026-08-26: research_web_search, cap 3).
            # Same resolver the dispatcher enforces with, so this number is true.
            cap_note = (
                f"\n    Per-turn limit: at most {call_cap} call(s) to this tool "
                f"per turn — plan the calls you need."
                if call_cap else ""
            )
            prompt_note = f"\n    Note: {note}" if note else ""
            tail = default_note + cap_note + prompt_note
            entry = head + (f"\n    {desc}" if desc else "") + tail
            compact = head + (
                f"\n    {compact_desc}" if compact_desc else ""
            ) + tail
            body = (schema["description"] or "").strip()
            block = _description_block(body) if body else ""
            # Only worth an upgrade when there is genuinely more to say.
            if block and block != f"    {desc}":
                full = f"{head}\n{block}{tail}"
        cost = _count_tokens(entry)
        if used + cost > budget:
            omitted.append(tool)
            continue
        slots.append({
            "tool": tool,
            "text": entry,
            "cost": cost,
            "compact": compact or entry,
            "compact_cost": _count_tokens(compact or entry),
            "full": full,
        })
        used += cost
        if fn is None and tool not in _FALLBACK_SCHEMAS:
            unresolved.append(tool)
        else:
            rendered.append(tool)

    # Pass 2 — spend what the tier actually allocated. Everything after a docstring's
    # first paragraph used to be discarded unconditionally, which threw away 87% of
    # the authored guidance (add_features shipped 77 of its 10,989 chars) while the
    # slot sat at ~15% of its own budget. A large authored block that cannot fit in
    # the conservative 2500-token slot gets first claim on a larger tier's surplus.
    # Otherwise dozens of small alphabetically-earlier upgrades can dilute that
    # surplus below the one threshold only the larger tier can pay.
    #
    # The grown 64-seat modeling pack now leaves 2822 tokens after every tool's
    # untrimmed floor, while add_features needs 3114 to replace its floor with the
    # authored block. Reclaim only that shortfall by compacting the few floor entries
    # with the largest first-paragraph savings. Signatures and first sentences remain,
    # so the priority expansion never costs another tool its slot. Within each class,
    # preserve allowlist order: role cards still lead with their principal tools.
    expanded: list[str] = []
    compacted: list[str] = []
    if expand:
        upgrades: list[tuple[bool, int, dict[str, Any], int]] = []
        for index, slot in enumerate(slots):
            block = slot["full"]
            if not block:
                continue
            full_cost = _count_tokens(block)
            frontier_only = full_cost > DEFAULT_TOOLS_BUDGET_TOKENS
            upgrades.append((not frontier_only, index, slot, full_cost))
        upgrades.sort(key=lambda item: (item[0], item[1]))
        for ordinary, _index, slot, full_cost in upgrades:
            shortfall = used - slot["cost"] + full_cost - budget
            if not ordinary and 0 < shortfall and full_cost <= budget:
                donors = [
                    donor for donor in slots
                    if donor is not slot
                    and donor["tool"] not in expanded
                    and donor["cost"] > donor["compact_cost"]
                ]
                donors.sort(
                    key=lambda donor: donor["cost"] - donor["compact_cost"],
                    reverse=True,
                )
                if sum(
                    donor["cost"] - donor["compact_cost"] for donor in donors
                ) >= shortfall:
                    for donor in donors:
                        saving = donor["cost"] - donor["compact_cost"]
                        donor["text"] = donor["compact"]
                        donor["cost"] = donor["compact_cost"]
                        used -= saving
                        compacted.append(donor["tool"])
                        shortfall -= saving
                        if shortfall <= 0:
                            break
            if used - slot["cost"] + full_cost > budget:
                continue
            used += full_cost - slot["cost"]
            slot["text"], slot["cost"] = slot["full"], full_cost
            if slot["tool"] in compacted:
                compacted.remove(slot["tool"])
            expanded.append(slot["tool"])

    lines.extend(slot["text"] for slot in slots)

    categories_listed: list[str] = []
    if include_category_menu:
        try:
            from services.agent.tool_categories import (  # noqa: PLC0415
                category_catalog,
            )
            cat_lines = ["[TOOL CATEGORIES] Call enable_tools(categories=[...]) to unlock:"]
            for name, desc in category_catalog():
                cat_lines.append(f"- {name}: {desc}")
                categories_listed.append(name)
            cat_block = "\n".join(cat_lines)
            cost = _count_tokens(cat_block)
            if used + cost <= budget:
                lines.append(cat_block)
                used += cost
            else:
                # Budget tight: keep a one-line pointer only.
                tail = (
                    "[TOOL CATEGORIES] enable_tools(categories=[...]) — "
                    f"{len(categories_listed)} packs (list_tool_categories for full menu)"
                )
                if used + _count_tokens(tail) <= budget:
                    lines.append(tail)
                    used += _count_tokens(tail)
        except Exception:  # noqa: BLE001 — menu is best-effort
            categories_listed = []

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
        "disabled": disabled,
        "trimmed": bool(trim),
        "compacted": compacted,
        "expanded": expanded,
        "categories": categories_listed,
    }
    return text, meta
