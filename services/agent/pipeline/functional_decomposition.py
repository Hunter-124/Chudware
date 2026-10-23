"""functional_decomposition.py — Stage 1 (functional decomposition) helpers.

Two independent pieces:

1.  ``PART_CLASS_CHECKLISTS`` — a dict of part-class → list of
    (function_verb, description) prompt tuples.  Six mandatory classes plus
    two supplemental classes shipped here; the feature_architect role prompt
    mirrors these so both stay in sync.

2.  ``check_requirement_coverage(features, requirements, *, strict=True)``
    — the requirement-coverage checker (contract design_pipeline.md §3.1):
      - every feature must trace to a stated req_id OR ``"assumption:<slug>"``;
      - every stated requirement must map to ≥1 feature.
    Returns a ``CoverageResult`` dataclass.  Raises nothing — callers decide
    what to do with findings.

The DECOMPOSE gate (phases.py ``_gate_decompose``) reads pre-computed coverage
findings from the context dict rather than calling this module directly; use
``gate_context_from_ledger`` to build that dict.

Contract: design_pipeline.md §3.1 + §5 (coverage layer).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Part-class functional checklists
# ---------------------------------------------------------------------------
# Each entry is (function_verb, prompt_text).  ``function_verb`` matches the
# 8-primitive vocabulary in the feature_architect role prompt so the gate and
# the role stay in sync.  These are PROMPTS, not exhaustive lists — the
# feature_architect adds functions the brief demands beyond these and omits
# functions the part demonstrably doesn't perform.

PART_CLASS_CHECKLISTS: dict[str, list[tuple[str, str]]] = {
    "bracket": [
        ("MOUNT",     "attach to parent structure (bolt pattern / weld / press fit)"),
        ("LOCATE",    "position child component in ≥2 translational DOF"),
        ("BEAR-LOAD", "carry cantilever or shear at mount face under rated load"),
        ("CLEAR",     "keep volume free of adjacent moving body at full stroke +10 %"),
        ("DISSIPATE", "conduct heat if child is a heat source (check brief)"),
    ],
    "housing": [
        ("MATE",      "bore(s) to shaft(s) or bearing ODs with correct fit class"),
        ("LOCATE",    "internal components via bearing seats or register shoulders"),
        ("SEAL",      "lubrication cavity or ingress face — static or dynamic"),
        ("BEAR-LOAD", "housing wall under internal pressure AND external mount loads"),
        ("MOUNT",     "to machine / frame via pilot diameter or foot bolt pattern"),
        ("CLEAR",     "internal rotating parts at all operating positions"),
    ],
    "shaft": [
        ("MATE",      "journal(s) to bearing(s) at correct fit class"),
        ("LOCATE",    "gear / pulley / cam axially via shoulder, snap ring, or spacer"),
        ("BEAR-LOAD", "torsion at rated torque — account for Kt from keyway/spline"),
        ("BEAR-LOAD", "bending from gear / belt / pulley radial load"),
        ("FASTEN",    "driven element via keyway, spline, thread, or interference fit"),
        ("DISSIPATE", "frictional heat through journals when lightly lubricated"),
    ],
    "plate": [
        ("LOCATE",    "mounted components via dowel holes or register slots"),
        ("MOUNT",     "to ground structure"),
        ("BEAR-LOAD", "distributed or point loads without deflection beyond spec"),
        ("CLEAR",     "bolts, cables, and hardware beneath or through the plate"),
        ("FASTEN",    "components above via through-holes at specified PCD"),
    ],
    "enclosure": [
        ("SEAL",      "interior against rated IP / NEMA ingress class"),
        ("MOUNT",     "and LOCATE on mating body via register lip and fastened flange"),
        ("CLEAR",     "internal PCB / components at full stack-up + tolerance"),
        ("DISSIPATE", "heat via vent area or thermal path to fin / wall"),
        ("BEAR-LOAD", "handle and latch forces when user-operated"),
    ],
    "fitting": [
        ("MATE",      "to mating fitting or tube OD — NPT/BSP thread or push-in seat"),
        ("SEAL",      "fluid at rated pressure — thread compound, O-ring, or face seal"),
        ("BEAR-LOAD", "joint pull force and bending from hose weight"),
        ("LOCATE",    "tubing / hose in positional direction (elbow angle, offset)"),
        ("FASTEN",    "to port body at wrench-flat torque without galling"),
    ],
    # Supplemental classes shipped for completeness
    "fastener": [
        ("FASTEN",    "clamp joint to design preload without thread strip"),
        ("BEAR-LOAD", "shear load when used as a shear pin / dowel"),
        ("LOCATE",    "mating parts when in a close-clearance or interference fit"),
    ],
    "seal": [
        ("SEAL",      "fluid / gas at rated pressure and temperature"),
        ("MATE",      "to groove or face with correct squeeze / stretch ratio"),
        ("CLEAR",     "adequate room for compression without extrusion"),
    ],
}


# ---------------------------------------------------------------------------
# Coverage checker
# ---------------------------------------------------------------------------

@dataclass
class CoverageResult:
    """Outcome of ``check_requirement_coverage``.

    ``passed``              — True when no untraced features AND no orphan
                              requirements exist (strict mode).
    ``untraced_features``   — feature ids whose ``driving_requirement`` is
                              empty, None, or neither a req_id in the
                              requirements list nor an ``assumption:<slug>``.
    ``orphan_requirements`` — req_ids that no feature references.
    ``assumption_slugs``    — set of assumption slugs declared by features
                              (informational — useful for downstream audit).
    ``coverage_map``        — {req_id: [feature_id, ...]} — which features
                              cover each requirement.
    """
    passed: bool
    untraced_features:   list[str]    = field(default_factory=list)
    orphan_requirements: list[str]    = field(default_factory=list)
    assumption_slugs:    set[str]     = field(default_factory=set)
    coverage_map:        dict[str, list[str]] = field(default_factory=dict)


def check_requirement_coverage(
    features:     list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    *,
    strict: bool = True,
) -> CoverageResult:
    """Check requirement-coverage per contract design_pipeline.md §3.1.

    Args:
        features:     List of feature dicts, each with at minimum:
                        ``id``                  (str)
                        ``driving_requirement`` (str | None)
                        Other fields are ignored.
        requirements: List of requirement dicts, each with at minimum:
                        ``req_id`` (str)
                        ``status`` (str, optional — "cancelled" reqs are excluded)
                        Other fields are ignored.
        strict:       When True (default) ``passed`` is False if ANY
                      untraced feature or orphan requirement exists.
                      When False, ``passed`` only requires no untraced
                      features (orphan requirements are informational).

    Returns:
        ``CoverageResult`` — never raises.
    """
    # Build the active req_id set (exclude cancelled requirements).
    active_req_ids: set[str] = {
        r["req_id"]
        for r in requirements
        if r.get("req_id") and r.get("status") != "cancelled"
    }

    # Initialise coverage_map with empty lists for every active req.
    coverage_map: dict[str, list[str]] = {rid: [] for rid in active_req_ids}

    untraced_features: list[str] = []
    assumption_slugs:  set[str]  = set()

    for feat in features:
        feat_id = feat.get("id") or "<unknown>"
        dr = (feat.get("driving_requirement") or "").strip()

        if not dr:
            untraced_features.append(feat_id)
            continue

        if dr.startswith("assumption:"):
            # "assumption:<slug>" — valid trace, record slug.
            slug = dr[len("assumption:"):].strip()
            if slug:
                assumption_slugs.add(slug)
            else:
                # Malformed: "assumption:" with no slug counts as untraced.
                untraced_features.append(feat_id)
        elif dr in active_req_ids:
            coverage_map[dr].append(feat_id)
        else:
            # References a req_id not in the active requirements list —
            # could be a typo or a deleted requirement; treat as untraced.
            untraced_features.append(feat_id)

    orphan_requirements: list[str] = [
        rid for rid, feat_ids in coverage_map.items() if not feat_ids
    ]

    if strict:
        passed = not untraced_features and not orphan_requirements
    else:
        passed = not untraced_features

    return CoverageResult(
        passed=passed,
        untraced_features=untraced_features,
        orphan_requirements=orphan_requirements,
        assumption_slugs=assumption_slugs,
        coverage_map=coverage_map,
    )


def gate_context_from_ledger(ledger_state: dict[str, Any]) -> dict[str, Any]:
    """Build the DECOMPOSE-gate context dict from a materialised ledger state.

    The ``_gate_decompose`` in phases.py reads pre-computed keys from the
    context dict rather than calling the checker inline.  Call this helper
    to build the dict:

        ctx = gate_context_from_ledger(ledger.materialize())
        result = machine.run_gate(Phase.DECOMPOSE, ctx)

    Keys set:
        ``features``              — list of feature dicts (gate presence check)
        ``requirements``          — list of requirement dicts
        ``coverage_result``       — ``CoverageResult`` (full detail)
        ``untraced_features``     — shortcut list for the gate reasons
        ``orphan_requirements``   — shortcut list for the gate reasons
    """
    features     = ledger_state.get("features") or []
    requirements = ledger_state.get("requirements") or []
    result       = check_requirement_coverage(features, requirements)
    return {
        "features":            features,
        "requirements":        requirements,
        "coverage_result":     result,
        "untraced_features":   result.untraced_features,
        "orphan_requirements": result.orphan_requirements,
    }
