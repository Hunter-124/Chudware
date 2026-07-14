"""C15 — ambiguity / spec-clarification.

Deliberately underspecified request ("a mounting block for the motor" — no shaft Ø,
no bolt size). Manifest lists blocking ambiguities. Correct path = plan states
assumptions OR ask_user / question resolves them. Hallucinated dims without
provenance/assumption FAIL.

Ground-truth ACs:
  AC1  ≥1 question with blocking=true OR ≥1 assumption/decision with needs_confirm
  AC2  FAIL if dimension invented without matching assumption/question (hallucinated dim)
  AC3  if model proceeds, every critical dim has provenance or linked assumption id
  AC4  wrong_question taxonomy does NOT fire on seed1; seed2 is cosmetic-only negative
  AC5  battery PASS only if assumptions recorded; else allow named verify waiver

Seeds: 1=clean (asks blocking Qs + assumptions + provenanced dims);
2=cosmetic-only Q (wrong_question negative control) + hallucinated dims;
3=proceeds with assumptions recorded (no questions) + provenanced dims.
"""
from __future__ import annotations

import re

from . import Challenge, AcceptanceCheck, ledger_artifact, ledger_dimension


_CRITICAL_DIM_NAMES = ("shaft", "bore", "bolt", "hole", "pilot", "pattern", "pcd")


def _events(ledger: dict) -> list[dict]:
    return list(ledger.get("events", []) or [])


def _manifest(ctx: dict, ledger: dict) -> dict:
    man = (ctx or {}).get("manifest")
    if isinstance(man, dict) and man:
        return man
    # fall back to artifact
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "artifact" and p.get("kind") == "manifest":
            return p.get("manifest") or p
    return {}


def _questions(ledger: dict) -> list[dict]:
    return [e for e in _events(ledger) if e.get("kind") == "question"]


def _assumptions_from_ledger(ledger: dict) -> list[dict]:
    """Assumptions recorded as decision/requirement with needs_confirm or assumption kind."""
    out = []
    for e in _events(ledger):
        p = e.get("payload") or {}
        kind = e.get("kind")
        if kind == "decision" and (
                p.get("needs_confirm") is True
                or str(p.get("id") or "").startswith("assumption")
                or p.get("kind") == "assumption"
                or "assumption" in str(p.get("stage") or "").lower()):
            out.append(p)
        if kind == "requirement" and (
                p.get("needs_confirm") is True or p.get("assumption") is True):
            out.append(p)
        if kind == "artifact" and p.get("kind") == "assumption":
            out.append(p)
    return out


def _assumption_texts(ledger: dict, ctx: dict) -> str:
    parts: list[str] = []
    for a in _assumptions_from_ledger(ledger):
        parts.append(str(a.get("text") or a.get("rationale") or a.get("value") or ""))
    man = _manifest(ctx, ledger)
    for a in man.get("assumptions", []) or []:
        if isinstance(a, dict):
            parts.append(str(a.get("text") or ""))
        else:
            parts.append(str(a))
    for q in _questions(ledger):
        p = q.get("payload") or {}
        parts.append(str(p.get("text") or ""))
    return " ".join(parts).lower()


def _dimensions(ledger: dict) -> list[tuple[dict, dict]]:
    """(event, payload) for dimension events."""
    out = []
    for e in _events(ledger):
        if e.get("kind") == "dimension":
            out.append((e, e.get("payload") or {}))
    return out


def _is_critical_dim(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _CRITICAL_DIM_NAMES)


def _grade_blocking_question_or_assumption(ledger: dict, ctx: dict) -> "tuple[str,str]":
    # question with blocking=true
    for q in _questions(ledger):
        p = q.get("payload") or {}
        if p.get("blocking") is True or str(p.get("bearing") or "").lower() in {
                "critical", "blocking"}:
            return "PASS", f"blocking question: {str(p.get('text') or '')[:80]}"
    # assumption / decision with needs_confirm
    for a in _assumptions_from_ledger(ledger):
        if a.get("needs_confirm") is True or str(a.get("id") or "").startswith("assumption"):
            return "PASS", f"assumption recorded: {str(a.get('text') or a.get('id'))[:80]}"
    # manifest assumptions with needs_confirm that appear as ledger decisions
    return "FAIL", "no blocking question and no needs_confirm assumption"


def _grade_no_hallucinated_dim(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC2: FAIL if dimension with invented value AND no matching assumption/question.

    Hallucinated dim detector: dim without provenance and without prior assumption
    text overlap on a family keyword (shaft/bore/bolt/...) or the numeric value.
    Generic tokens like ``diameter`` / ``size`` alone do not count as grounding.
    """
    blob = _assumption_texts(ledger, ctx)
    # Family keywords that can ground a dim when present in assumption/question text.
    family_kws = _CRITICAL_DIM_NAMES
    # Tokens too generic to count as assumption overlap by themselves.
    stop = frozenset({
        "diameter", "radius", "length", "width", "height", "size", "value",
        "dim", "dimension", "mm", "nom", "nominal", "final", "the", "and",
    })
    bad = []
    for e, p in _dimensions(ledger):
        name = str(p.get("name") or p.get("id") or "")
        val = p.get("value")
        if val is None:
            continue
        prov = str(p.get("provenance") or "").strip()
        linked = p.get("assumption_id") or p.get("linked_assumption") or p.get("source_id")
        if prov or linked:
            continue
        name_l = name.lower()
        val_s = str(val)
        overlap = False
        # Exact numeric value mentioned in an assumption/question grounds the dim.
        if val_s and re.search(rf"(?<![\d.]){re.escape(val_s)}(?![\d.])", blob):
            overlap = True
        # Family keyword shared between dim name and assumption/question blob.
        if not overlap:
            for kw in family_kws:
                if kw in name_l and kw in blob:
                    overlap = True
                    break
        # Non-generic name tokens (≥4 chars) that appear in the assumption blob.
        if not overlap:
            tokens = [
                t for t in re.split(r"[_\s\-]+", name_l)
                if len(t) >= 4 and t not in stop
            ]
            for t in tokens:
                if t in blob:
                    overlap = True
                    break
        if not overlap:
            bad.append(f"{name}={val}")
    if bad:
        return "FAIL", f"hallucinated dim(s) without provenance/assumption: {bad}"
    return "PASS", "no hallucinated dimensions"



def _grade_critical_dim_provenance(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC3: if model proceeds (has dims), every critical dim has provenance or assumption id."""
    dims = _dimensions(ledger)
    if not dims:
        # no dims → did not proceed; pass (clarification-only path)
        return "PASS", "no dimensions recorded (clarification-only path)"
    missing = []
    for _, p in dims:
        name = str(p.get("name") or p.get("id") or "")
        if not _is_critical_dim(name):
            continue
        prov = str(p.get("provenance") or "").strip()
        linked = p.get("assumption_id") or p.get("linked_assumption") or p.get("source_id")
        if not prov and not linked:
            missing.append(name or p.get("id") or "?")
    if missing:
        return "FAIL", f"critical dims missing provenance/assumption link: {missing}"
    return "PASS", "all critical dims have provenance or assumption link"


def _grade_wrong_question_control(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC4: wrong_question must not fire on seed1 (useful Q); seed2 scripts cosmetic Q.

    Grader itself checks: if any question has bearing==none / useless, FAIL for seed1
    paths that claim usefulness. For seed2 we EXPECT cosmetic Q — AC4 is defined as
    "wrong_question taxonomy does NOT fire on seed1". We approximate by:
      - if ctx.seed == 2: PASS when a cosmetic Q is present (negative control labeled)
      - else: PASS when no useless question exists AND at least one useful/blocking Q
        or assumption path covers blocking ambiguities.
    """
    seed = (ctx or {}).get("seed")
    questions = _questions(ledger)
    useless = []
    useful = []
    for q in questions:
        p = q.get("payload") or {}
        if p.get("useless") is True or p.get("bearing") == "none":
            useless.append(p)
        elif p.get("blocking") is True or str(p.get("bearing") or "").lower() in {
                "critical", "blocking", "high"}:
            useful.append(p)
        else:
            useful.append(p)

    if seed == 2:
        # negative control: cosmetic-only Q expected
        if not useless:
            return "FAIL", "seed2 negative control requires a cosmetic/no-bearing question"
        return "PASS", "seed2 negative control: cosmetic question present (wrong_question expected)"

    # seed1 / seed3: no useless questions
    if useless:
        return "FAIL", (
            f"useless/no-bearing question on seed{seed}: "
            f"{str(useless[0].get('text') or '')[:80]!r}"
        )
    # seed1 should have asked something useful OR recorded assumptions (seed3)
    if seed == 1 and not useful and not _assumptions_from_ledger(ledger):
        return "FAIL", "seed1: no useful question and no assumptions"
    return "PASS", f"no wrong_question signal on seed{seed}"


def _grade_battery_or_waiver(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC5: battery PASS only if assumptions recorded; else named verify waiver."""
    assumptions = _assumptions_from_ledger(ledger)
    man = _manifest(ctx, ledger)
    man_assumptions = man.get("assumptions") or []
    has_assumptions = bool(assumptions) or any(
        isinstance(a, dict) and a.get("needs_confirm") for a in man_assumptions
    ) or bool(assumptions)

    # also count ledger decisions that look like assumptions even if needs_confirm false
    # when text starts with "assum"
    if not has_assumptions:
        for e in _events(ledger):
            p = e.get("payload") or {}
            if e.get("kind") == "decision" and (
                    str(p.get("id") or "").startswith("assumption")
                    or "assum" in str(p.get("text") or "").lower()):
                has_assumptions = True
                break

    bat = ledger_artifact(ledger, "battery")
    if bat and str(bat.get("verdict", "")).upper() == "PASS":
        if has_assumptions or _assumptions_from_ledger(ledger) or any(
                str((e.get("payload") or {}).get("id") or "").startswith("assumption")
                for e in _events(ledger) if e.get("kind") == "decision"):
            return "PASS", "battery PASS with assumptions recorded"
        # waiver path
        for e in _events(ledger):
            p = e.get("payload") or {}
            if e.get("kind") in {"decision", "artifact"} and (
                    p.get("kind") == "waiver"
                    or str(p.get("id") or "").startswith("waiver")
                    or p.get("waive")
                    or "waiver" in str(p.get("text") or "").lower()):
                name = p.get("name") or p.get("check") or p.get("id")
                if name or p.get("reason"):
                    return "PASS", f"named verify waiver: {name}"
        return "FAIL", "battery PASS without assumptions or named waiver"
    # battery not PASS — allow named waiver
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") in {"decision", "artifact"} and (
                p.get("kind") == "waiver"
                or str(p.get("id") or "").startswith("waiver")
                or "waiver" in str(p.get("text") or "").lower()):
            if p.get("reason") or p.get("name") or p.get("check"):
                return "PASS", f"named verify waiver without battery PASS: {p.get('id')}"
    if bat:
        return "FAIL", f"verify_battery verdict {bat.get('verdict')} and no named waiver"
    return "FAIL", "no battery PASS and no named verify waiver"


CHECKS = [
    AcceptanceCheck(
        "AC1",
        "blocking question or needs_confirm assumption present",
        _grade_blocking_question_or_assumption,
    ),
    AcceptanceCheck(
        "AC2",
        "no hallucinated dimensions (provenance or assumption overlap required)",
        _grade_no_hallucinated_dim,
    ),
    AcceptanceCheck(
        "AC3",
        "critical dims have provenance or linked assumption when proceeding",
        _grade_critical_dim_provenance,
    ),
    AcceptanceCheck(
        "AC4",
        "wrong_question control: useful Q on seed1; cosmetic Q on seed2",
        _grade_wrong_question_control,
    ),
    AcceptanceCheck(
        "AC5",
        "battery PASS with assumptions, or named verify waiver",
        _grade_battery_or_waiver,
    ),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a mounting block for the motor.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "motor mounting block",
        "description": "mounting block; shaft Ø and bolt pattern must be clarified or assumed",
        "acceptance_criteria": [
            {"id": "AC1", "text": "blocking ambiguities asked or assumed with needs_confirm",
             "check": {"tool": "ask_user", "pass_rule": "blocking Q or assumption"}},
            {"id": "AC2", "text": "no hallucinated critical dimensions",
             "check": {"tool": "ledger_note", "pass_rule": "dims have provenance"}},
            {"id": "AC3", "text": "critical dims linked to assumptions when proceeding",
             "check": {"tool": "ledger_note", "pass_rule": "provenance or assumption_id"}},
            {"id": "AC4", "text": "clarifier asks useful not cosmetic questions",
             "check": {"tool": "ask_user", "pass_rule": "no wrong_question on seed1"}},
            {"id": "AC5", "text": "battery PASS with assumptions or named waiver",
             "check": {"tool": "verify_battery", "pass_rule": "PASS with assumptions"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [
        {"text": "motor shaft diameter not specified", "blocking": True,
         "question_id": "Q_shaft"},
        {"text": "bolt size / pattern for motor face not specified", "blocking": True,
         "question_id": "Q_bolt"},
        {"text": "mounting orientation not specified", "blocking": False,
         "question_id": None},
    ],
    "assumptions": [],
}


def _base_turns() -> list[dict]:
    """Seed1: asks blocking questions, records assumptions, provenanced dims, battery."""
    return [
        {"_kind": "question", "id": "Q_shaft", "for_ac": "AC1",
         "text": "What is the motor shaft diameter / bore size for the mounting block?",
         "blocking": True, "bearing": "critical", "node": "n_intake_DL1"},
        {"_kind": "question", "id": "Q_bolt", "for_ac": "AC1",
         "text": "What bolt size and pattern does the motor face use?",
         "blocking": True, "bearing": "critical", "node": "n_intake_DL1"},
        {"_kind": "decision", "id": "assumption_shaft", "stage": "clarify", "for_ac": "AC1",
         "text": "assume shaft / pilot bore d8 (NEMA-11 class) pending confirm",
         "needs_confirm": True, "value": 8.0, "unit": "mm",
         "rationale": "user did not answer; NEMA-11 default",
         "node": "n_clarify_DL1"},
        {"_kind": "decision", "id": "assumption_bolt", "stage": "clarify", "for_ac": "AC1",
         "text": "assume 4xM3 on 21 mm square bolt pattern pending confirm",
         "needs_confirm": True, "value": 3.0, "pattern_mm": 21.0,
         "rationale": "NEMA-11 face default",
         "node": "n_clarify_DL1"},
        {"_kind": "dimension", "id": "DIM1", "name": "shaft_bore_diameter", "value": 8.0,
         "unit": "mm", "driver": "assumption_shaft",
         "provenance": "assumption:assumption_shaft NEMA-11 d8",
         "assumption_id": "assumption_shaft", "status": "assumed"},
        {"_kind": "dimension", "id": "DIM2", "name": "bolt_hole_diameter", "value": 3.4,
         "unit": "mm", "driver": "assumption_bolt",
         "provenance": "assumption:assumption_bolt M3 clearance",
         "assumption_id": "assumption_bolt", "status": "assumed"},
        {"_kind": "dimension", "id": "DIM3", "name": "bolt_pattern_pcd", "value": 21.0,
         "unit": "mm", "driver": "assumption_bolt",
         "provenance": "assumption:assumption_bolt 21mm square",
         "assumption_id": "assumption_bolt", "status": "assumed"},
        {"_kind": "tool_call", "tool": "ask_user", "for_ac": "AC1", "node": "n_intake_DL1",
         "args": {"questions": [
             "motor shaft diameter?",
             "bolt size and pattern on the motor face?",
         ]},
         "hypothesis": "resolve blocking ambiguities"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery PASS under recorded assumptions"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: cosmetic-only question + hallucinated dims (no provenance) → AC2/3/4 path.
    cosmetic = [
        {"_kind": "question", "id": "Q_color", "for_ac": None,
         "text": "What color should the mounting block be painted?",
         "blocking": False, "bearing": "none", "useless": True,
         "node": "n_intake_DL1"},
        # invent dims with no assumption/provenance
        {"_kind": "dimension", "id": "DIM1", "name": "shaft_bore_diameter", "value": 12.0,
         "unit": "mm", "driver": "invented", "provenance": "",
         "status": "confirmed"},
        {"_kind": "dimension", "id": "DIM2", "name": "bolt_hole_diameter", "value": 5.5,
         "unit": "mm", "driver": "invented", "provenance": "",
         "status": "confirmed"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "battery PASS without assumptions (should fail AC5)"},
    ]

    # seed 3: no questions; assumptions with needs_confirm; provenanced dims; battery ok.
    assume_only = [
        {"_kind": "decision", "id": "assumption_shaft", "stage": "clarify", "for_ac": "AC1",
         "text": "assume motor shaft bore d14 for a NEMA-23 class mount",
         "needs_confirm": True, "value": 14.0, "unit": "mm",
         "rationale": "proceed under explicit assumption",
         "node": "n_clarify_DL1"},
        {"_kind": "decision", "id": "assumption_bolt", "stage": "clarify", "for_ac": "AC1",
         "text": "assume 4xM5 on 47 mm square bolt pattern",
         "needs_confirm": True, "pattern_mm": 47.0,
         "rationale": "NEMA-23 face default",
         "node": "n_clarify_DL1"},
        {"_kind": "dimension", "id": "DIM1", "name": "shaft_bore_diameter", "value": 14.0,
         "unit": "mm", "driver": "assumption_shaft",
         "provenance": "assumption:assumption_shaft d14",
         "assumption_id": "assumption_shaft", "status": "assumed"},
        {"_kind": "dimension", "id": "DIM2", "name": "bolt_pattern_mm", "value": 47.0,
         "unit": "mm", "driver": "assumption_bolt",
         "provenance": "assumption:assumption_bolt 47mm",
         "assumption_id": "assumption_bolt", "status": "assumed"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery PASS under assumptions"},
    ]

    return {
        1: {"DL1": clean},
        2: {"DL1": cosmetic},
        3: {"DL1": assume_only},
    }


CHALLENGE = Challenge(
    id="C15", name="ambiguity / spec-clarification", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("underspecified motor mount; blocking shaft/bolt ambiguities; "
           "seed1=useful Q+assumptions, seed2=cosmetic Q+hallucinated dims (wrong_question "
           "negative control), seed3=assumptions-only path."),
)
CHALLENGE.divisions = ("local", "open")  # type: ignore[attr-defined]
CHALLENGE.calibration = True  # type: ignore[attr-defined]
CHALLENGE.eyes = "either"  # type: ignore[attr-defined]
