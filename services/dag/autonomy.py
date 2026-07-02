"""autonomy.py — Per-run autonomy default and per-node override logic.

Contract: contracts/dag_schema.md §6 (AutonomyLevel enum).

Autonomy levels (frozen in aicad.dag/1):
  "auto"    — node runs without user interaction.
  "approve" — gate node inserted before execution; user must approve.
  "manual"  — gate node inserted; user supplies output directly.

The *effective* autonomy for a node is:
  1. The node's own `autonomy` field (always present — schema requires it).
  2. The run-level `autonomy_default` (passed to executor.run/resume) can RAISE
     the floor: if default is "approve" and the node is "auto", the node is
     promoted to "approve".  It can never LOWER a node below its declared level
     (a "manual" node stays "manual" even if default is "auto").

The hierarchy: manual > approve > auto  (higher index = more oversight).
"""
from __future__ import annotations

from typing import Any

# Ordered by oversight level (index = level).
_LEVEL_ORDER = ["auto", "approve", "manual"]


def _level(autonomy: str) -> int:
    """Return the numeric oversight level for an autonomy string."""
    try:
        return _LEVEL_ORDER.index(autonomy)
    except ValueError:
        return 0  # unknown → treat as auto (least oversight)


def effective_autonomy(
    node: dict[str, Any],
    *,
    autonomy_default: str = "auto",
) -> str:
    """Return the effective autonomy level for a node.

    The run-level default can only RAISE the floor; it cannot lower a node's
    declared level.

    Args:
        node:              Node dict (must have an "autonomy" field).
        autonomy_default:  Run-level default; one of "auto", "approve", "manual".

    Returns:
        Effective autonomy string — one of "auto", "approve", "manual".

    Example:
        effective_autonomy({"autonomy": "auto"}, autonomy_default="approve") → "approve"
        effective_autonomy({"autonomy": "manual"}, autonomy_default="auto")  → "manual"
        effective_autonomy({"autonomy": "approve"}, autonomy_default="manual") → "manual"
    """
    node_level    = _level(node.get("autonomy", "auto"))
    default_level = _level(autonomy_default)
    effective     = max(node_level, default_level)
    return _LEVEL_ORDER[effective]


def requires_gate(
    node: dict[str, Any],
    *,
    autonomy_default: str = "auto",
) -> bool:
    """Return True if this node requires a human gate before execution.

    Both "approve" and "manual" require a gate; "auto" does not.

    Args:
        node:              Node dict.
        autonomy_default:  Run-level default autonomy.
    """
    eff = effective_autonomy(node, autonomy_default=autonomy_default)
    return eff in ("approve", "manual")
