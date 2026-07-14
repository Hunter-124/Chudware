"""prompt_tiers.py — per-capability SOFT phrasing blocks (harness-tiers-spec WI-16).

Capability letter selects which paragraph is assembled into system prompts.
Prompt text only; the model may override with rationale. HARD floors are enforced
elsewhere (guardrails.py) — these blocks are SOFT nudges.

  A (local)    — mandatory-language discipline (scaffold hard)
  B (budget)   — suggestive build order / group verify
  C (frontier) — minimal two-liner mirroring HARD floors only

Freedom rule (WI-18): C must not receive rigid A checklists or per-mutation
lectures. Assembly is keyed off resolve_capability / letter at prompt build time.
"""
from __future__ import annotations

from typing import Any, Optional, Union

# Marker so tests / ablation can detect the injected block.
SOFT_BLOCK_MARKER = "[CAPABILITY_SOFT]"

# ---------------------------------------------------------------------------
# SOFT phrasing blocks (WI-16)
# ---------------------------------------------------------------------------

SOFT_A = f"""\
{SOFT_BLOCK_MARKER} A — MANDATORY DISCIPLINE (local scaffold)
These are process rules for this capability tier. Follow them unless you have a
specific, recorded reason not to:
- calculate-before-model: never invent a load-bearing number. Run fit_calc /
  research_eng_calc / the matching calculator (or cite a ledger DIM / ADVISORY)
  BEFORE emitting dimensioned features, holes, or seats.
- datum-first: establish datums and primary sketch planes before secondary
  geometry; drive patterns and mates from those datums.
- fillet-last: fillets and chamfers come last within their region, after holes /
  bores / cuts that would otherwise lose the blend.
- read-back after every mutation: after each mutating tool (add_features, cut,
  pattern, …) call describe_part / measure_part / check_sketch (stage-appropriate)
  before the next mutation. Aligns with harness auto-verify inject.
- use templates: follow the plan / playbook / buildgraph skeleton slots; fill
  dimensions into the template rather than free-forming a new feature tree.
"""

SOFT_B = f"""\
{SOFT_BLOCK_MARKER} B — RECOMMENDED DISCIPLINE (budget)
Suggested practice for this capability tier (not hard-gated by the harness beyond
HARD floors):
- build order: datums → primary solids → secondary features → patterns → blends;
  respect buildgraph edges when present.
- verify after feature groups: after a sketch→extrude→fillet (or similar) group,
  run describe_part / check_sketch / check_feature_overlap before starting the
  next group. Per-call read-back is optional.
- naming: name features and sketches for the tree and ledger so handoffs stay
  readable.
- preflight recommended: call preflight_ops on non-trivial ops; treat findings as
  advisory unless refs clearly will not resolve.
- calculate when uncertain: prefer fit_calc / research_* for seats and fits rather
  than recalling ISO bands.
"""

SOFT_C = f"""\
{SOFT_BLOCK_MARKER} C — HARD FLOORS ONLY (frontier freedom)
Prefer verify_battery before done; checkpoint before irreversible ops.
No prescribed build order beyond those process floors — drive the tools as the
task requires.
"""

_SOFT_BY_LETTER: dict[str, str] = {
    "A": SOFT_A.strip(),
    "B": SOFT_B.strip(),
    "C": SOFT_C.strip(),
}

# Phrases that MUST appear in A assembled system text (snapshot acceptance).
A_REQUIRED_SNIPPETS = (
    "calculate-before-model",
    "datum-first",
    "fillet-last",
)

# Phrases that MUST NOT appear in C assembled system text (snapshot acceptance).
C_FORBIDDEN_SNIPPETS = (
    "after every mutation",
    "calculate-before-model",
    "use templates",
    "MANDATORY DISCIPLINE",
    "rigid",
)


def normalize_letter(letter: Optional[str]) -> str:
    """Map a letter-ish value onto A|B|C; default C (freedom)."""
    if not letter:
        return "C"
    L = str(letter).strip().upper()
    return L if L in _SOFT_BY_LETTER else "C"


def soft_phrasing(letter: Optional[str] = "C") -> str:
    """Return the SOFT phrasing block for *letter* (A/B/C)."""
    return _SOFT_BY_LETTER[normalize_letter(letter)]


def letter_from_capability(
    capability: Union[str, Any, None],
) -> str:
    """Extract A|B|C from a letter string or CapabilityProfile-like object."""
    if capability is None:
        return "C"
    if isinstance(capability, str):
        return normalize_letter(capability)
    letter = getattr(capability, "letter", None)
    if letter:
        return normalize_letter(letter)
    return "C"


def append_soft_block(base: str, letter: Optional[str] = "C") -> str:
    """Append the capability SOFT block to a base system prompt.

    Empty *base* → soft block alone. Already-appended blocks (marker present for
    the same letter) are not duplicated; a different letter replaces a prior
    soft block when the marker is found.
    """
    base = (base or "").rstrip()
    block = soft_phrasing(letter)
    if not base:
        return block
    if SOFT_BLOCK_MARKER in base:
        # Drop any prior soft block (from marker to end or next double-blank).
        head, _, _rest = base.partition(SOFT_BLOCK_MARKER)
        base = head.rstrip()
        if not base:
            return block
    return base + "\n\n" + block


def assemble_system_for_capability(
    base: str,
    capability: Union[str, Any, None] = "C",
) -> str:
    """Assemble final system text: base role/prompt + SOFT block for capability."""
    return append_soft_block(base, letter_from_capability(capability))


def mcp_profile_for_letter(letter: Optional[str] = "C") -> str:
    """Which MCP instruction profile C prefers lite; A/B prefer full (WI-16)."""
    return "lite" if normalize_letter(letter) == "C" else "full"
