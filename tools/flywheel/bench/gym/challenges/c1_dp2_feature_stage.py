"""C1-DP2 — flanged bushing: Stage 1 feature-architect gym run.

Exercises the feature_architect role and the DECOMPOSE gate for the C1 flanged
bushing.  Scripted turns produce REAL ledger ``feature`` events; the gym grades:

  FA1  ≥1 feature event with id, function, driving_requirement, criticality
  FA2  all features trace to a valid req_id or assumption:<slug>
  FA3  all stated requirements map to ≥1 feature (no orphan)
  FA4  DECOMPOSE gate passes when given the produced feature list
  FA5  DECOMPOSE gate fails when an untraced feature is injected

Seed 1: clean pass — 5 features covering all 3 requirements.
Seed 2: one untraced feature (driving_requirement = "") → FA2/FA3/FA4 fail correctly.
Seed 3: one requirement orphaned (no feature traces to R-03) → FA3/FA4 fail correctly.

All runs are SCRIPTED (MOCK mode) — no live model, no frontier key required.
Labeled as scripted everywhere; do not re-label as "model-produced."
"""
from __future__ import annotations

from . import Challenge, AcceptanceCheck
from services.agent.pipeline.functional_decomposition import check_requirement_coverage

# ---------------------------------------------------------------------------
# Fixture requirements (mirroring the C1 manifest deliverable)
# ---------------------------------------------------------------------------
REQUIREMENTS = [
    {"req_id": "R-01", "text": "bore provides sliding fit for 20 mm shaft",
     "status": "open"},
    {"req_id": "R-02", "text": "flange carries 4xM4 bolt pattern for mount",
     "status": "open"},
    {"req_id": "R-03", "text": "bushing locates axially in housing bore",
     "status": "open"},
]

MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a flanged bushing with a sliding-fit bore for a 20 mm shaft and a "
        "4-hole M4 bolt circle on the flange.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "flanged bushing",
        "acceptance_criteria": [
            {"id": "FA1", "text": "feature list with ≥1 feature event in ledger"},
            {"id": "FA2", "text": "all features trace to req_id or assumption"},
            {"id": "FA3", "text": "all stated requirements covered by ≥1 feature"},
            {"id": "FA4", "text": "DECOMPOSE gate passes on produced feature list"},
            {"id": "FA5",
             "text": "DECOMPOSE gate fails when untraced feature injected (negative)"},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 6061-T6 (machined bushing)", "basis": "default machined",
         "needs_confirm": False},
    ],
    "_requirements": REQUIREMENTS,   # conveyed to graders via manifest context
}


# ---------------------------------------------------------------------------
# Scripted Stage-1 turns
# ---------------------------------------------------------------------------

def _base_feature_turns() -> list[dict]:
    """Clean seed 1: 5 features, all traced, all requirements covered."""
    return [
        # Seed requirements into ledger first (so coverage checker can read them)
        {"_kind": "requirement", "req_id": "R-01",
         "text": "bore provides sliding fit for 20 mm shaft", "source": "brief",
         "status": "open"},
        {"_kind": "requirement", "req_id": "R-02",
         "text": "flange carries 4xM4 bolt pattern for mount", "source": "brief",
         "status": "open"},
        {"_kind": "requirement", "req_id": "R-03",
         "text": "bushing locates axially in housing bore", "source": "brief",
         "status": "open"},
        # Features — written exactly as feature_architect role would note them
        {"_kind": "feature", "_actor": "feature_architect",
         "id": "FT-01",
         "function": "present sliding-fit bore surface to d20 h7 shaft",
         "driving_requirement": "R-01",
         "interfaces_with": ["shaft_S1"],
         "criticality": "critical"},
        {"_kind": "feature", "_actor": "feature_architect",
         "id": "FT-02",
         "function": "carry 4×M4 bolt fastening pattern on flange face",
         "driving_requirement": "R-02",
         "interfaces_with": ["parent_structure"],
         "criticality": "critical"},
        {"_kind": "feature", "_actor": "feature_architect",
         "id": "FT-03",
         "function": "mount bushing body to parent structure via flange",
         "driving_requirement": "R-02",
         "interfaces_with": ["parent_structure"],
         "criticality": "important"},
        {"_kind": "feature", "_actor": "feature_architect",
         "id": "FT-04",
         "function": "locate bushing axially via shoulder against housing bore face",
         "driving_requirement": "R-03",
         "interfaces_with": ["housing_H1"],
         "criticality": "critical"},
        {"_kind": "feature", "_actor": "feature_architect",
         "id": "FT-05",
         "function": "bear-load wall hoop stress from press-fit outer diameter",
         "driving_requirement": "assumption:no_explicit_wall_req",
         "interfaces_with": ["housing_H1"],
         "criticality": "important"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_feature_turns()

    # seed 2: one feature has no driving_requirement → untraced
    untraced = [dict(t) for t in _base_feature_turns()]
    for t in untraced:
        if t.get("id") == "FT-04":
            t["driving_requirement"] = ""   # stripped → untraced

    # seed 3: R-03 orphaned — FT-04 redirected to R-01 → no feature for R-03
    orphan = [dict(t) for t in _base_feature_turns()]
    for t in orphan:
        if t.get("id") == "FT-04":
            t["driving_requirement"] = "R-01"   # R-03 now uncovered

    return {
        1: {"DL1": clean},
        2: {"DL1": untraced},
        3: {"DL1": orphan},
    }


# ---------------------------------------------------------------------------
# Ground-truth graders
# ---------------------------------------------------------------------------

def _extract_features(ledger: dict) -> list[dict]:
    return [e["payload"] for e in ledger.get("events", [])
            if e.get("kind") == "feature"]


def _extract_requirements(ledger: dict) -> list[dict]:
    return [e["payload"] for e in ledger.get("events", [])
            if e.get("kind") == "requirement"]


def _grade_fa1_features_present(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FA1: ≥1 feature event with required fields."""
    feats = _extract_features(ledger)
    if not feats:
        return "FAIL", "no feature events in ledger"
    # Check all required fields present on each feature
    missing = []
    required = {"id", "function", "driving_requirement", "criticality"}
    for f in feats:
        absent = required - set(f.keys())
        if absent:
            missing.append(f"{f.get('id','?')}: missing {absent}")
    if missing:
        return "FAIL", f"features missing required fields: {missing}"
    return "PASS", f"{len(feats)} feature(s) with required fields"


def _grade_fa2_traceability(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FA2: all features trace to a valid req_id or assumption:<slug>."""
    feats = _extract_features(ledger)
    reqs  = _extract_requirements(ledger)
    result = check_requirement_coverage(feats, reqs, strict=False)
    if result.untraced_features:
        return "FAIL", f"untraced features: {result.untraced_features}"
    return "PASS", "all features traced to req_id or assumption"


def _grade_fa3_coverage(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FA3: all stated requirements covered by ≥1 feature."""
    feats = _extract_features(ledger)
    reqs  = _extract_requirements(ledger)
    result = check_requirement_coverage(feats, reqs, strict=True)
    if result.orphan_requirements:
        return "FAIL", f"orphan requirements (no feature): {result.orphan_requirements}"
    return "PASS", f"all {len(reqs)} requirements covered"


def _grade_fa4_gate_passes(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FA4: DECOMPOSE gate passes on the produced feature list."""
    from services.agent.pipeline.phases import Phase, PhaseMachine
    from services.agent.pipeline.functional_decomposition import gate_context_from_ledger

    # Reconstruct a minimal ledger state from the produced events
    feats = _extract_features(ledger)
    reqs  = _extract_requirements(ledger)
    state = {"features": feats, "requirements": reqs}
    gate_ctx = gate_context_from_ledger(state)
    m = PhaseMachine()
    g = m.run_gate(Phase.DECOMPOSE, gate_ctx)
    if not g.passed:
        return "FAIL", f"DECOMPOSE gate failed: {g.reasons}"
    return "PASS", f"DECOMPOSE gate passed ({g.gate})"


def _grade_fa5_gate_rejects_untraced(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FA5: negative — inject an untraced feature, confirm gate fails."""
    from services.agent.pipeline.phases import Phase, PhaseMachine

    # Build a context with a known-bad untraced feature
    feats = _extract_features(ledger)
    # Inject one untraced feature for the negative test
    bad_feats = feats + [{"id": "FT-BAD", "function": "phantom",
                          "driving_requirement": "", "criticality": "important"}]
    gate_ctx = {
        "features": bad_feats,
        "untraced_features": ["FT-BAD"],
        "orphan_requirements": [],
    }
    m = PhaseMachine()
    g = m.run_gate(Phase.DECOMPOSE, gate_ctx)
    if g.passed:
        return "FAIL", "gate should have failed with untraced feature FT-BAD but passed"
    if "FT-BAD" not in " ".join(g.reasons):
        return "FAIL", f"gate failed but FT-BAD not mentioned in reasons: {g.reasons}"
    return "PASS", f"gate correctly rejected untraced feature (reasons: {g.reasons[:1]})"


CHECKS = [
    AcceptanceCheck("FA1", "≥1 feature event with id/function/driving_requirement/criticality",
                    _grade_fa1_features_present),
    AcceptanceCheck("FA2", "all features trace to req_id or assumption:<slug>",
                    _grade_fa2_traceability),
    AcceptanceCheck("FA3", "all stated requirements covered by ≥1 feature",
                    _grade_fa3_coverage),
    AcceptanceCheck("FA4", "DECOMPOSE gate passes on produced feature list",
                    _grade_fa4_gate_passes),
    AcceptanceCheck("FA5", "DECOMPOSE gate correctly rejects an untraced feature (negative)",
                    _grade_fa5_gate_rejects_untraced),
]


CHALLENGE = Challenge(
    id="C1-DP2",
    name="flanged bushing — Stage 1 feature decomposition",
    lane="ai-copilot",
    request=MANIFEST["request_verbatim"],
    manifest=MANIFEST,
    checks=CHECKS,
    seeds=(1, 2, 3),
    scripts=_seed_scripts(),
    notes=(
        "Stage 1 (feature_architect) scripted run. "
        "seed1=clean pass; seed2=untraced feature (FA2/FA3/FA4 must fail); "
        "seed3=orphan requirement (FA3/FA4 must fail). "
        "All runs are SCRIPTED/MOCK — no live model required."
    ),
)
