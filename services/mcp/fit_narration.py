"""Shared fit/clearance narration (contracts/narration.md) — the single home of
``_format_fit`` (T-F8).

Kept free of any ``mcp`` SDK import on purpose: the agent build loop
(``services/agent/loop.py``) imports this module, and under pytest the sibling
``services/mcp`` package can shadow the ``mcp`` SDK name — a heavy import chain here
would break agent-test collection. ``gsl_tools`` re-exports ``_format_fit`` for the
narration layer (describe_assembly).
"""
from __future__ import annotations


def _format_fit(fit: dict | None) -> str:
    """Render the kernel's fit/interference report as narration text.

    Input shape (``check_interference`` / ``eval_interference``):
      {summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}.
    Returns "" when fit is None or empty. Numbers narrated verbatim — never recomputed.
    """
    if not fit:
        return ""
    lines = [f"FIT REPORT: {fit.get('summary', '')}"]
    for it in fit.get("interferences", []):
        lines.append(f"  INTERFERENCE  {it['a']} <-> {it['b']}: {it['volume']:.2f} mm^3 overlap")
    for cl in fit.get("clearances", []):
        lines.append(f"  clearance     {cl['a']} <-> {cl['b']}: {cl['distance']:.2f} mm gap")
    if fit.get("interferences"):
        lines.append("  -> If that overlap is NOT an intended press-fit, fix it: REPOSITION one "
                     "part (change its Pos along the axis where the bodies overlap) or CUT a "
                     "matching opening/pocket in the other part so the inserted part has room. "
                     "A part placed AT a wall coordinate overlaps that wall - offset it by at "
                     "least half its own thickness plus the wall thickness to sit flush outside, "
                     "or place it in an actual gap/opening.")
    return "\n".join(lines)
