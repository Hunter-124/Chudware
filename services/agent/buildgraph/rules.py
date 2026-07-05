"""rules.py — Prerequisite rules as data for aicad.buildgraph/1.

Every rule is a dict with:
  id       : str  — unique rule identifier
  matcher  : callable(feature_text: str) -> bool  — True if this rule fires for a feature
  requires : callable(feature_text: str, plan: dict) -> list[str]
               — returns list of node labels that must precede this node;
                 empty list means "rule matched but no explicit requires found"
  why      : str  — human-readable rationale; written onto the edge in the graph

Rules are LLM-free: they match on keyword/pattern in plain-text feature strings
from loop.py's PLAN_PROMPT output shape. Where the text is ambiguous the planner
emits an aicad.advisory/1 finding; it never guesses.

Contract: contracts/advisor.md §13 (FROZEN) — "standard prerequisite rules live in
the planner as data."
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

def _words(text: str) -> str:
    return text.lower()


def _has_any(text: str, *kws: str) -> bool:
    t = _words(text)
    return any(k in t for k in kws)


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------
#
# Each rule is applied per feature node.  The planner iterates RULES and for
# each rule that fires, collects the rule's requires() results and attaches the
# combined why strings to the node's `why` field.
#
# ORDERING: rules are tried in list order; ALL matching rules fire (not first-match).

RULES: list[dict[str, Any]] = [
    # ------------------------------------------------------------------
    # R1 — Datum/reference geometry before any sketch that depends on it
    # ------------------------------------------------------------------
    {
        "id": "R1_datum_before_sketch",
        "matcher": lambda feat, _plan: _has_any(feat, "sketch", "profile", "on plane", "on face"),
        "requires": lambda feat, plan: _find_datums(feat, plan),
        "why": "datum/reference geometry must exist before dependent sketch",
    },
    # ------------------------------------------------------------------
    # R2 — Base solid before its modifications (cut, fillet, chamfer, shell, hole)
    # ------------------------------------------------------------------
    {
        "id": "R2_base_before_modification",
        "matcher": lambda feat, _plan: _has_any(feat,
            "fillet", "chamfer", "shell", "hole", "bore", "cut", "pocket",
            "extrude cut", "revolve cut", "loft cut", "sweep cut", "subtract",
        ),
        "requires": lambda feat, plan: _find_base_solid(feat, plan),
        "why": "base solid must exist before it can be modified",
    },
    # ------------------------------------------------------------------
    # R3 — Boss must exist before a feature that pierces or blends it
    # ------------------------------------------------------------------
    {
        "id": "R3_boss_before_pierce_blend",
        "matcher": lambda feat, _plan: _has_any(feat,
            "through boss", "into boss", "pierce", "on boss", "blend", "bore through",
        ),
        "requires": lambda feat, plan: _find_boss(feat, plan),
        "why": "boss must exist before a feature pierces or blends it",
    },
    # ------------------------------------------------------------------
    # R4 — Pattern after seed feature
    # ------------------------------------------------------------------
    {
        "id": "R4_pattern_after_seed",
        "matcher": lambda feat, _plan: _has_any(feat,
            "pattern", "linear pattern", "circular pattern", "mirror",
        ),
        "requires": lambda feat, plan: _find_seed_for_pattern(feat, plan),
        "why": "pattern seed feature must exist before it is replicated",
    },
    # ------------------------------------------------------------------
    # R5 — Shell after the features it hollows around
    # ------------------------------------------------------------------
    {
        "id": "R5_shell_ordering",
        "matcher": lambda feat, _plan: _has_any(feat, "shell"),
        "requires": lambda feat, plan: _shell_requires(feat, plan),
        "why": "shell applied after solid is complete; if shell must precede cuts, state it explicitly",
    },
    # ------------------------------------------------------------------
    # R6 — Parts before mates
    # ------------------------------------------------------------------
    {
        "id": "R6_parts_before_mates",
        "matcher": lambda feat, _plan: _has_any(feat,
            "mate", "coincident", "concentric", "parallel", "flush", "align",
        ),
        "requires": lambda feat, plan: _find_mated_parts(feat, plan),
        "why": "all mated parts must be fully modelled before mates are applied",
    },
    # ------------------------------------------------------------------
    # R7 — Mates before assembly verify / interference check
    # ------------------------------------------------------------------
    {
        "id": "R7_mates_before_verify",
        "matcher": lambda feat, _plan: _has_any(feat,
            "verify", "check interference", "interference check", "fit check", "assembly verify",
        ),
        "requires": lambda feat, plan: _find_mate_nodes(feat, plan),
        "why": "mates must be solved before assembly-level verification",
    },
    # ------------------------------------------------------------------
    # R8 — Checkpoint nodes after their preceding group
    # ------------------------------------------------------------------
    {
        "id": "R8_checkpoint_after_group",
        "matcher": lambda feat, _plan: _has_any(feat,
            "part_done", "mission_done", "checkpoint",
        ),
        "requires": lambda feat, plan: _find_checkpoint_precursors(feat, plan),
        "why": "checkpoint emits PLM history entry after all upstream nodes complete",
    },
]


# ---------------------------------------------------------------------------
# Finder helpers — each returns a list of *node labels* (not ids) that must
# precede the matched node.  The planner resolves labels → node ids.
# ---------------------------------------------------------------------------

def _find_datums(feat: str, plan: dict) -> list[str]:
    """Return labels of datum/reference nodes referenced by this sketch feature."""
    # Explicit "on <plane/face/ref>" references
    refs: list[str] = []
    for m in re.finditer(r"on\s+([a-zA-Z0-9_\-\s]+?)(?:,|;|\.|$)", feat, re.IGNORECASE):
        label = m.group(1).strip()
        if label and len(label) < 40:
            refs.append(label)
    # Look for any node whose kind is datum/reference and is in the same part
    part_name = _guess_part(feat, plan)
    for part in plan.get("parts", []):
        if part_name and _words(part.get("name", "")) != _words(part_name):
            continue
        for f in part.get("features", []):
            if isinstance(f, str) and _has_any(f,
                "datum", "reference plane", "ref plane", "ref point", "ref axis",
                "coordinate sys", "coord sys",
            ):
                refs.append(f)
    return refs


def _find_base_solid(feat: str, plan: dict) -> list[str]:
    """Return the primary base solid feature in the same part."""
    part_name = _guess_part(feat, plan)
    candidates: list[str] = []
    for part in plan.get("parts", []):
        if part_name and _words(part.get("name", "")) != _words(part_name):
            continue
        for f in part.get("features", []):
            if isinstance(f, str) and _has_any(f,
                "base", "extrude", "revolve", "loft", "sweep", "box", "cylinder",
            ) and not _has_any(f,
                "fillet", "chamfer", "hole", "bore", "cut", "pocket", "shell",
            ):
                candidates.append(f)
                break  # first solid only
    return candidates


def _find_boss(feat: str, plan: dict) -> list[str]:
    """Return boss features that this feature references.

    When the feature text explicitly mentions "boss" (e.g. "bore through boss"),
    at least a synthetic label is returned so the planner can detect a dangling
    requires when no boss feature exists.  If a real boss feature is found, its
    label is returned instead (the planner resolves it to a node id).

    The current feature (feat) is always excluded from results to prevent
    self-reference loops.
    """
    # Search for boss features in the same part, excluding the current feature itself.
    part_name = _guess_part(feat, plan)
    found_real: list[str] = []
    feat_lower = _words(feat)
    for part in plan.get("parts", []):
        if part_name and _words(part.get("name", "")) != _words(part_name):
            continue
        for f in part.get("features", []):
            if not isinstance(f, str):
                continue
            if _words(f) == feat_lower:
                continue  # skip the feature being matched
            # A "boss" feature is one that creates a boss — not one that references it.
            # It should contain "boss" and NOT contain "through|bore|pierce|blend"
            # (those are consumers of a boss, not producers).
            if (_has_any(f, "boss") and
                    not _has_any(f, "through boss", "bore through", "pierce", "into boss")):
                found_real.append(f)

    if found_real:
        return found_real

    # No real boss producer found — but the current feature text explicitly names
    # "boss" as a target.  Return a sentinel so the planner emits a dangling blocker.
    return ["boss (referenced but not found in plan)"]


def _find_seed_for_pattern(feat: str, plan: dict) -> list[str]:
    """Return the seed feature label for a pattern/mirror."""
    # Try explicit "of <something>" pattern
    m = re.search(r"(?:of|seed)\s+([a-zA-Z0-9_\-\s]+?)(?:,|;|\.|$)", feat, re.IGNORECASE)
    if m:
        return [m.group(1).strip()]
    # Fall back: any non-pattern feature in the same part (planner emits advisory if ambiguous)
    part_name = _guess_part(feat, plan)
    candidates: list[str] = []
    for part in plan.get("parts", []):
        if part_name and _words(part.get("name", "")) != _words(part_name):
            continue
        for f in part.get("features", []):
            if isinstance(f, str) and not _has_any(f, "pattern", "mirror"):
                candidates.append(f)
    return candidates


def _shell_requires(feat: str, plan: dict) -> list[str]:
    """Shell normally runs after other solid features in the same part.

    Returns the base solid(s).  When there are also cuts/holes, order is
    ambiguous (shell-first vs shell-last) — the planner emits an advisory.
    """
    return _find_base_solid(feat, plan)


def _find_mated_parts(feat: str, plan: dict) -> list[str]:
    """Return the part node labels referenced by this mate feature."""
    refs: list[str] = []
    for part in plan.get("parts", []):
        refs.append(part.get("name", ""))
    return [r for r in refs if r]


def _find_mate_nodes(feat: str, plan: dict) -> list[str]:
    """Return mate/constraint feature labels that must precede this verify."""
    refs: list[str] = []
    for part in plan.get("parts", []):
        for f in part.get("features", []):
            if isinstance(f, str) and _has_any(f,
                "mate", "coincident", "concentric", "align",
            ):
                refs.append(f)
    return refs


def _find_checkpoint_precursors(feat: str, plan: dict) -> list[str]:
    """Return all non-checkpoint node labels in the plan as precursors."""
    refs: list[str] = []
    for part in plan.get("parts", []):
        for f in part.get("features", []):
            if isinstance(f, str) and not _has_any(f, "checkpoint", "part_done", "mission_done"):
                refs.append(f)
    return refs


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _guess_part(feat: str, plan: dict) -> str | None:
    """Guess which part a feature string belongs to by substring match."""
    feat_lower = _words(feat)
    for part in plan.get("parts", []):
        name = part.get("name", "")
        if name and _words(name) in feat_lower:
            return name
    return None
