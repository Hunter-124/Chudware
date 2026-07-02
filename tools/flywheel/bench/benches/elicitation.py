"""elicitation bench (IP-12): does a conversation turn into a schema-valid `aicad.req/1`
brief that covers what the human actually asked for?

Score per example (survey §6): slot coverage + precision against a human-audited gold
brief. Bar: >=90% coverage, 100% schema-valid.

Two roles per example:
  * GOLD brief — hand-authored, ground truth for what the conversation asked for.
  * CANDIDATE brief — what a strategy produced from the raw conversation text. The
    deterministic baseline (this module's kill-switch) is a regex/keyword extractor that
    never reads the gold brief; a trained/frontier candidate would replace it 1:1.

Slot model
----------
Each requirement exposes a small set of typed "slots":
  kind                        -- the requirement's `kind` enum value
  quantity.param               -- the measured/controlled parameter name (if any)
  quantity.op                  -- =='/'>='/'<='/'range'
  quantity.value                -- the numeric target
  quantity.unit                 -- the unit string
  quantity.value_max            -- only present when op == 'range'
  verify_by:<verifier-name>     -- one boolean-membership slot per attached verifier

Coverage = fraction of GOLD slots (across all gold requirements) that are present with an
equal value in the CANDIDATE brief. A gold requirement that has no candidate counterpart at
all contributes zero covered slots (all its slots count against coverage).

Precision = fraction of CANDIDATE slots (across all candidate requirements) that equal a
slot in the matched gold requirement. A spurious/unmatched candidate requirement, or a
correct requirement with a wrong value, drags precision down without touching coverage.

Requirement matching rule (documented, not implicit): a GOLD requirement `g` and a
CANDIDATE requirement `c` are paired 1:1 in two passes so surface differences in `id`/`need`
strings (which are arbitrary opaque tokens, not shared vocabulary between two independently
produced briefs) never block matching:
  1. exact `quantity.param` (case-insensitive) -- the most stable identifier of "what this
     requirement is about" since it's drawn from a physical-parameter vocabulary both a
     human-authored gold brief and any reasonable extractor converge on.
  2. leftover gold/candidate requirements (quantity is None, or its param didn't match
     anything) are paired by same `kind`, in first-seen order -- the next unmatched
     candidate requirement of the same kind is assumed to address the same qualitative ask.
Requirements that still have no partner after both passes count fully against coverage (if
gold) or fully against precision (if candidate), as described above.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from typing import Any, Optional

from ..baseline import Baseline, pending_frontier
from ..dataset import DatasetSpec
from ..metrics import mean, pass_rate
from ..schema import BenchResult, ExampleScore

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
GOLD_DIR = os.path.join(_REPO_ROOT, "training", "benches", "elicitation", "gold")

# ---- schema-valid constants: reuse the real substrate module, fall back to a local copy
# if the repo-root import path isn't wired (e.g. this module imported outside the pytest
# conftest that adds the repo root to sys.path). Kept byte-identical to
# engine_oracle/requirements/requirements.py / contracts/requirements.md §3.
try:
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from engine_oracle.requirements.requirements import (  # type: ignore
        VALID_KINDS,
        VALID_STATUSES,
        VALID_VERIFIERS,
    )
    _CONSTANTS_SOURCE = "engine_oracle.requirements.requirements"
except Exception:
    _CONSTANTS_SOURCE = "local fallback copy (engine_oracle import failed)"
    VALID_VERIFIERS = frozenset({
        "check_sketch", "check_feature_overlap", "check_interference", "check_symmetry",
        "compare_parts", "confirm_dimensions", "dfm_check", "draft_analysis", "fit_report",
        "measure_between", "measure_part", "query_geometry", "run_cfd_accurate",
        "run_cfd_flow", "run_fea_modal", "run_fea_static", "solve_mates", "wall_thickness",
    })
    VALID_KINDS = frozenset({
        "dimensional", "functional", "material", "interface", "cost", "standard", "process",
    })
    VALID_STATUSES = frozenset({
        "proposed", "user_confirmed", "verified", "failed", "waived",
    })


# ---- deterministic kill-switch extractor -------------------------------------

_UNIT_ALT = r"(?:mm/s|mm²|mm2|mm³|mm3|mm|deg|MPa|N|g|€|USD)"
_NUM_UNIT_RE = re.compile(
    r"(?P<pre>(?:[A-Za-z][A-Za-z\-]*\s+){0,4})"
    r"(?P<cmp>at least|no more than|minimum|maximum|within)?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>" + _UNIT_ALT + r")\b",
)

# keyword -> canonical quantity.param name. Longest/most-specific keys first so e.g.
# "bolt circle diameter" wins over the generic "diameter".
_PARAM_KEYWORDS: list[tuple[str, str]] = [
    ("bolt circle diameter", "bolt_circle_dia"),
    ("bolt circle", "bolt_circle_dia"),
    ("wall thickness", "wall_thickness"),
    ("wall be", "wall_thickness"),
    ("plate thickness", "plate_thickness"),
    ("standoff height", "standoff_height"),
    ("fan cutout", "fan_cutout_dia"),
    ("bearing bore", "bearing_bore"),
    ("keyway", "keyway_width"),
    ("shaft diameter", "shaft_dia"),
    ("diameter", "hole_dia"),
    ("clearance", "board_clearance"),
    ("flatness", "flatness"),
    ("deflection", "tip_deflection"),
    ("span", "span"),
    ("length", "shaft_length"),
    ("height", "standoff_height"),
    ("thickness", "wall_thickness"),
    ("load", "load"),
    ("flow", "vent_flow"),
    ("airflow", "vent_flow"),
]

# keyword -> (kind, verifier). Scanned over the whole sentence a number/unit was found in
# (or the surrounding phrase when there's no number at all) to guess a requirement's kind
# and a single verifier. Order matters: first match wins.
_KIND_VERIFIER_KEYWORDS: list[tuple[str, tuple[str, str]]] = [
    ("ip6", ("standard", "confirm_dimensions")),
    ("rating", ("standard", "confirm_dimensions")),
    ("interfer", ("interface", "check_interference")),
    ("mate", ("interface", "solve_mates")),
    ("overlap", ("dimensional", "check_feature_overlap")),
    ("symmetric", ("dimensional", "check_symmetry")),
    ("symmetry", ("dimensional", "check_symmetry")),
    ("flatness", ("dimensional", "query_geometry")),
    ("fit", ("dimensional", "fit_report")),
    ("wall thickness", ("dimensional", "wall_thickness")),
    ("thick", ("dimensional", "wall_thickness")),
    ("resonan", ("functional", "run_fea_modal")),
    ("modal", ("functional", "run_fea_modal")),
    ("airflow", ("functional", "run_cfd_flow")),
    ("flow", ("functional", "run_cfd_flow")),
    ("load", ("functional", "run_fea_static")),
    ("yield", ("functional", "run_fea_static")),
    ("deflect", ("functional", "run_fea_static")),
    ("material", ("material", "dfm_check")),
    ("aluminum", ("material", "dfm_check")),
    ("steel", ("material", "dfm_check")),
    ("flame", ("material", "dfm_check")),
    ("grade", ("material", "dfm_check")),
    ("cost", ("cost", "confirm_dimensions")),
    ("budget", ("cost", "confirm_dimensions")),
    ("process", ("process", "dfm_check")),
    ("mold", ("process", "dfm_check")),
]

_OP_KEYWORDS = [
    ("at least", ">="),
    ("no more than", "<="),
    ("minimum", ">="),
    ("maximum", "<="),
    ("within", "<="),
]


def _guess_param(pre_context: str) -> str:
    low = pre_context.lower()
    for kw, param in _PARAM_KEYWORDS:
        if kw in low:
            return param
    return "value"


def _guess_kind_verifier(sentence: str) -> tuple[str, str]:
    low = sentence.lower()
    for kw, kv in _KIND_VERIFIER_KEYWORDS:
        if kw in low:
            return kv
    return ("dimensional", "measure_part")


def _guess_op(pre_context: str) -> str:
    low = pre_context.lower()
    for kw, op in _OP_KEYWORDS:
        if kw in low:
            return op
    return "=="


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.\n])", text) if s.strip()]


def deterministic_extract(conversation: str) -> dict[str, Any]:
    """Rule-based conversation -> draft `aicad.req/1` brief. Decent but imperfect: it only
    finds requirements anchored to an explicit `NUMBER UNIT` token in the transcript, so
    purely qualitative asks (symmetry, interface fit, standards compliance stated without a
    number nearby) are frequently missed or mis-typed -- that gap is the measured residual a
    trained model must close. Never reads the gold brief.
    """
    needs = [{"id": "N1", "text": "Requirements elicited from the conversation.", "source": "chat"}]
    requirements: list[dict[str, Any]] = []
    seen_params: set[str] = set()
    idx = 0
    for sentence in _sentences(conversation):
        for m in _NUM_UNIT_RE.finditer(sentence):
            pre = m.group("pre") or ""
            param = _guess_param(pre)
            if param in seen_params:
                continue  # keep the extractor's first guess per param (avoid duplicate slots)
            seen_params.add(param)
            kind, verifier = _guess_kind_verifier(sentence)
            op = _guess_op((m.group("cmp") or "") + " " + pre)
            unit = m.group("unit")
            idx += 1
            requirements.append({
                "id": f"R{idx}",
                "need": "N1",
                "kind": kind,
                "text": f"Extracted requirement concerning {param.replace('_', ' ')}.",
                "quantity": {"param": param, "op": op, "value": float(m.group("num")), "unit": unit},
                "verify_by": [{"verifier": verifier}],
                "status": "proposed",
                "trace": {"features": [], "study": None, "evidence": []},
            })

    return {
        "schema": "aicad.req/1",
        "id": "det-extract",
        "title": "Draft brief (deterministic extractor)",
        "revision": 1,
        "created": "2026-01-01T00:00:00Z",
        "modified": "2026-01-01T00:00:00Z",
        "stakeholder_needs": needs,
        "requirements": requirements,
        "interfaces": [],
        "assumptions": [],
        "open_questions": [],
        "audit": [{"who": "agent", "ts": "2026-01-01T00:00:00Z", "change": "Draft extracted"}],
    }


# ---- schema validity ----------------------------------------------------------

def schema_valid(brief_doc: dict) -> tuple[bool, list[str]]:
    """Narrow schema-valid check per T-H8 spec: every enum value used by the candidate
    brief is a member of VALID_KINDS/VALID_STATUSES/VALID_VERIFIERS, plus the minimal
    structural requirements the schema mandates (>=1 verifier per requirement, `need`
    resolves to a declared stakeholder need). Does NOT enforce the stricter
    no-numbers-in-text convention (contracts/requirements.md §0) -- that is a style rule
    for hand-authored briefs, not a wire-format validity rule, and a regex extractor's
    auto-generated text is exempt by design.
    """
    errors: list[str] = []
    if brief_doc.get("schema") != "aicad.req/1":
        errors.append(f"schema != aicad.req/1 (got {brief_doc.get('schema')!r})")
    need_ids = {n.get("id") for n in brief_doc.get("stakeholder_needs", [])}
    for req in brief_doc.get("requirements", []):
        rid = req.get("id", "?")
        if req.get("kind") not in VALID_KINDS:
            errors.append(f"{rid}: invalid kind {req.get('kind')!r}")
        if req.get("status", "proposed") not in VALID_STATUSES:
            errors.append(f"{rid}: invalid status {req.get('status')!r}")
        if req.get("need") not in need_ids:
            errors.append(f"{rid}: need {req.get('need')!r} not declared in stakeholder_needs")
        vb = req.get("verify_by") or []
        if not vb:
            errors.append(f"{rid}: verify_by must name >=1 verifier")
        for v in vb:
            if v.get("verifier") not in VALID_VERIFIERS:
                errors.append(f"{rid}: invalid verifier {v.get('verifier')!r}")
    return (len(errors) == 0, errors)


# ---- slot model + matching -----------------------------------------------------

def _requirement_slots(req: dict) -> dict[str, Any]:
    slots: dict[str, Any] = {"kind": req.get("kind")}
    q = req.get("quantity")
    if q:
        slots["quantity.param"] = (q.get("param") or "").strip().lower()
        slots["quantity.op"] = q.get("op")
        slots["quantity.value"] = q.get("value")
        slots["quantity.unit"] = q.get("unit")
        if q.get("op") == "range" and q.get("value_max") is not None:
            slots["quantity.value_max"] = q.get("value_max")
    for vb in req.get("verify_by") or []:
        v = vb.get("verifier")
        if v:
            slots[f"verify_by:{v}"] = True
    return slots


def _slots_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def match_requirements(gold_reqs: list[dict], cand_reqs: list[dict]) -> list[tuple[Optional[dict], Optional[dict]]]:
    """Pair gold/candidate requirements per the module-docstring matching rule. Returns a
    list of (gold_req_or_None, cand_req_or_None) -- unmatched entries appear paired with
    None on the other side.
    """
    gold_left = list(gold_reqs)
    cand_left = list(cand_reqs)
    pairs: list[tuple[Optional[dict], Optional[dict]]] = []

    def param_of(r: dict) -> Optional[str]:
        q = r.get("quantity")
        p = (q or {}).get("param") if q else None
        return p.strip().lower() if isinstance(p, str) and p.strip() else None

    # pass 1: exact quantity.param match
    for g in list(gold_left):
        gp = param_of(g)
        if gp is None:
            continue
        for c in list(cand_left):
            if param_of(c) == gp:
                pairs.append((g, c))
                gold_left.remove(g)
                cand_left.remove(c)
                break

    # pass 2: same kind, first-seen order
    for g in list(gold_left):
        gk = g.get("kind")
        for c in list(cand_left):
            if c.get("kind") == gk:
                pairs.append((g, c))
                gold_left.remove(g)
                cand_left.remove(c)
                break

    # leftovers: unmatched on one side
    for g in gold_left:
        pairs.append((g, None))
    for c in cand_left:
        pairs.append((None, c))
    return pairs


def score_slots(gold_doc: dict, cand_doc: dict) -> dict[str, Any]:
    """Coverage/precision over the whole brief (all requirements), per the matching rule."""
    pairs = match_requirements(gold_doc.get("requirements", []), cand_doc.get("requirements", []))

    gold_total = 0
    gold_covered = 0
    cand_total = 0
    cand_correct = 0
    per_req: list[dict[str, Any]] = []

    for g, c in pairs:
        g_slots = _requirement_slots(g) if g is not None else {}
        c_slots = _requirement_slots(c) if c is not None else {}
        gold_total += len(g_slots)
        cand_total += len(c_slots)
        covered_here = 0
        correct_here = 0
        for k, v in g_slots.items():
            if k in c_slots and _slots_equal(v, c_slots[k]):
                covered_here += 1
        for k, v in c_slots.items():
            if k in g_slots and _slots_equal(v, g_slots[k]):
                correct_here += 1
        gold_covered += covered_here
        cand_correct += correct_here
        per_req.append({
            "gold_id": g.get("id") if g else None,
            "cand_id": c.get("id") if c else None,
            "gold_slots": len(g_slots), "gold_covered": covered_here,
            "cand_slots": len(c_slots), "cand_correct": correct_here,
        })

    coverage = (gold_covered / gold_total) if gold_total else 1.0
    precision = (cand_correct / cand_total) if cand_total else 0.0
    return {
        "coverage": coverage, "precision": precision,
        "gold_total": gold_total, "gold_covered": gold_covered,
        "cand_total": cand_total, "cand_correct": cand_correct,
        "per_requirement": per_req,
    }


# ---- per-example scoring -------------------------------------------------------

def score_example(example_id: str, structure: str, conversation: str, gold_brief: dict,
                  *, candidate_brief: Optional[dict] = None) -> ExampleScore:
    """Score one (conversation, gold_brief) example. `candidate_brief`, when omitted, is
    produced by the deterministic extractor (offline kill-switch); pass an explicit dict to
    score any other strategy's output through the same rubric.
    """
    candidate = candidate_brief if candidate_brief is not None else deterministic_extract(conversation)

    valid, errors = schema_valid(candidate)
    if not valid:
        detail = {"schema_valid": False, "schema_errors": errors[:10]}
        return ExampleScore(example_id=example_id, score=0.0, passed=False, structure=structure,
                            label=None, baselines={"deterministic": 0.0}, detail=detail)

    slots = score_slots(gold_brief, candidate)
    coverage = slots["coverage"]
    precision = slots["precision"]
    score = mean([coverage, precision])
    passed = bool(coverage >= 0.90 and valid)
    detail = {
        "schema_valid": True,
        "coverage": coverage, "precision": precision,
        "gold_total": slots["gold_total"], "gold_covered": slots["gold_covered"],
        "cand_total": slots["cand_total"], "cand_correct": slots["cand_correct"],
    }
    return ExampleScore(example_id=example_id, score=score, passed=passed, structure=structure,
                        label=None, baselines={"deterministic": score}, detail=detail)


# ---- gold set loading -----------------------------------------------------------

def load_gold(gold_dir: str = GOLD_DIR) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(gold_dir, "*.json")))
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def freeze_spec(gold_dir: str = GOLD_DIR) -> DatasetSpec:
    examples = load_gold(gold_dir)
    return DatasetSpec.freeze(
        "elicitation_gold", gold_dir, split="all",
        n_examples=len(examples),
        structures=sorted({e["structure"] for e in examples}),
    )


# ---- run() -------------------------------------------------------------------

def run(gold_dir: str = GOLD_DIR, *, split: str = "all", limit: Optional[int] = None,
        frontier: Optional[Baseline] = None) -> BenchResult:
    examples_raw = load_gold(gold_dir)
    if limit is not None:
        examples_raw = examples_raw[:limit]
    spec = DatasetSpec.freeze("elicitation_gold", gold_dir, split=split,
                              n_examples=len(examples_raw),
                              structures=sorted({e["structure"] for e in examples_raw}))

    examples = [
        score_example(e["id"], e["structure"], e["conversation"], e["brief"])
        for e in examples_raw
    ]

    coverage_mean = mean([e.detail.get("coverage", 0.0) for e in examples])
    precision_mean = mean([e.detail.get("precision", 0.0) for e in examples])
    schema_valid_rate = pass_rate([bool(e.detail.get("schema_valid")) for e in examples])
    det_mean = mean([e.score for e in examples])
    det_pass = pass_rate([e.passed for e in examples])

    frontier = frontier or pending_frontier()
    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(),
    ]
    resid = ({"status": "pending", "baseline_mean": det_mean,
              "note": "frontier candidate pending; residual measured when it lands"}
             if not frontier.available else
             {"status": "measured",
              "candidate_mean": mean([e.baselines.get(frontier.name, 0.0) for e in examples]),
              "baseline_mean": det_mean,
              "residual": mean([e.baselines.get(frontier.name, 0.0) for e in examples]) - det_mean})

    return BenchResult(
        bench_id="elicitation", dataset_id=spec.id, dataset_hash=spec.hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate={
            "coverage": coverage_mean, "precision": precision_mean,
            "schema_valid_rate": schema_valid_rate, "mean_score": det_mean, "pct_passed": det_pass,
            "success_bar": {"coverage>=0.90": coverage_mean >= 0.90,
                           "schema_valid==1.0": schema_valid_rate == 1.0},
            "constants_source": _CONSTANTS_SOURCE,
        },
        residual=resid,
    )


# ---- driver registration -------------------------------------------------------

from ..driver import BenchDriver  # noqa: E402  (after helpers, mirrors regen_fix import order)


def _run(smoke: bool, workdir: str) -> BenchResult:
    limit = 4 if smoke else None
    return run(GOLD_DIR, split="smoke" if smoke else "all", limit=limit)


DRIVER = BenchDriver("elicitation", _run)
