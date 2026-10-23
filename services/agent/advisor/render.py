"""Advisory block rendering — budgeted, §9 precedence, no silent truncation.

Contract: ``contracts/advisor.md`` §3 (block format + drop policy) and §9
(injection & precedence).  Layout follows docs/design/ADVISOR_ARCHITECTURE.md §3
(local-24k reference: advisory slot = 600 tokens).

Rules implemented here:
  * Delimited block: ``[ADVISORY seq=.. stage=.. severity=..] ... [/ADVISORY]``.
  * Token budget from the tier profile (callers pass the ``tiers.yaml`` advisory
    budget; the fallback default is :data:`DEFAULT_ADVISORY_BUDGET`).
  * Items dropped **lowest-severity-first** (the engine already sorted
    highest-first, so we drop from the tail).
  * A dropped-count line is **always** appended when anything was dropped — no
    silent truncation (§3).
  * §9.2 user-override: a banner is prepended and the header severity reflects
    the (downgraded) items.
  * ``confidence`` other than kernel/table renders as "suggest:" not "fix:" so a
    model does not treat a heuristic as a fact (§3).
"""
from __future__ import annotations

from typing import Any

from .engine import SEVERITY_ORDER

_CHARS_PER_TOKEN = 4  # repo-wide heuristic (matches ledger.py)

# Fallback when no tier budget is passed; docs §3 local-24k advisory slot = 600 tokens.
DEFAULT_ADVISORY_BUDGET = 600

_FACT_CONFIDENCE = frozenset({"kernel", "table"})


def _fix_line(fix: dict) -> str:
    conf = fix.get("confidence", "heuristic")
    verb = "fix" if conf in _FACT_CONFIDENCE else "suggest"
    action = fix.get("action", "?")
    params = fix.get("params") or {}
    param_str = ", ".join(f"{k}={v}" for k, v in params.items() if v is not None)
    parts = [f"    {verb}: {action}"]
    if param_str:
        parts.append(f"({param_str})")
    basis = fix.get("basis")
    if basis:
        parts.append(f"— {basis}")
    pred = fix.get("predicted_effect")
    if pred:
        parts.append(f"⇒ {pred}")
    parts.append(f"[{conf}]")
    return " ".join(parts)


def _item_lines(item: dict) -> list[str]:
    sev = item.get("severity", "info")
    check = item.get("check", "")
    head = f"  [{sev}] {check}: {item.get('finding', '')}"
    lines = [head]
    nums = item.get("numbers") or {}
    if nums:
        lines.append("    numbers: " + ", ".join(f"{k}={v}" for k, v in nums.items()))
    for fix in item.get("candidate_fixes", []):
        lines.append(_fix_line(fix))
    helpers = item.get("helper_tools") or []
    if helpers:
        lines.append(f"    tools: {', '.join(helpers)}")
    return lines


def render_advisory(advisory: dict, budget_tokens: int = DEFAULT_ADVISORY_BUDGET) -> str:
    """Render an aicad.advisory/1 block within *budget_tokens*.

    Returns the delimited text block, or ``""`` when there are no items (the
    caller should not inject an empty block).
    """
    items = advisory.get("items", [])
    if not items:
        return ""

    budget_chars = max(0, budget_tokens) * _CHARS_PER_TOKEN

    seq = advisory.get("seq", 0)
    stage = advisory.get("stage", "")
    # Header severity = the highest severity actually present.
    header_sev = "info"
    best = -1
    for it in items:
        r = SEVERITY_ORDER.get(it.get("severity", "info"), 0)
        if r > best:
            best = r
            header_sev = it.get("severity", "info")

    header = f"[ADVISORY seq={seq} stage={stage} severity={header_sev}]"
    footer = "[/ADVISORY]"

    body_lines: list[str] = []
    if advisory.get("user_override"):
        # §9.2 banner — always kept (it is the precedence signal itself).
        body_lines.append("  user override active — advisories are informational this turn")

    # Items are pre-sorted highest-severity-first by the engine; drop from the tail.
    used = len(header) + len(footer) + 2
    used += sum(len(l) + 1 for l in body_lines)
    rendered_count = 0
    dropped = 0

    for item in items:
        item_text_lines = _item_lines(item)
        cost = sum(len(l) + 1 for l in item_text_lines)
        # Always keep at least one item even if over budget (a blocker must be
        # seen); otherwise respect the budget.
        if rendered_count == 0 or used + cost <= budget_chars:
            body_lines.extend(item_text_lines)
            used += cost
            rendered_count += 1
        else:
            dropped += 1

    if dropped:
        # Never silent (§3): count + the lowest severity that got dropped.
        dropped_sevs = [it.get("severity", "info") for it in items[rendered_count:]]
        lo = min(dropped_sevs, key=lambda s: SEVERITY_ORDER.get(s, 0)) if dropped_sevs else "info"
        body_lines.append(f"  (+{dropped} lower-severity advisory item(s) dropped for budget; "
                          f"lowest={lo})")

    return "\n".join([header, *body_lines, footer])
