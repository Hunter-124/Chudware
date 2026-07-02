"""vlm render-review bench (IP-21): score a reviewer's CLAIMS about geometry against the
KNOWN geometry behind a synthetic render.

Division of labor (survey Appendix A, LOCKED — never invert): a VLM proposes COARSE
claims about what a render shows; deterministic code verifies/measures those claims
against known geometry. This bench IS that verification step in isolated, offline form —
it never asks a model to grade itself, and it never lets a fluent claim substitute for a
grounded one. **Precision is king**: a single hallucinated/ungrounded claim must sink an
example's `passed` bar regardless of how much else it got right (see `score_example`).

Ground truth model: each example (fixture) carries
  * `known_geometry` — a list of atomic geometry FACTS, e.g.
        {"kind": "hole", "count": 4, "diameter": 6.0}
        {"kind": "fillet", "edges": "top"}
        {"kind": "bbox", "x": 80, "y": 50, "z": 6}
        {"kind": "boss", "count": 1, "visible": false}
    `visible` (default true) marks whether the fact is something the non-vision
    `describe_scene` fallback could plausibly surface from B-rep face normals (internal
    ribs, thread pitch, etc. are not — see `deterministic_claims`).
  * `claims` — a candidate reviewer's list of claim dicts of the same shape. Some may be
    correct, some hallucinated (no matching fact), some simply wrong (right kind, wrong
    attribute, e.g. miscounted holes).

Claim matching (`_matches`): a claim matches a fact iff same `kind` and every attribute
the claim asserts is compatible with the fact's value — numeric attributes (bbox dims,
diameters, counts, depths) within tolerance (`_numeric_close`: max(1.0mm abs, 5% rel)),
everything else by exact equality. A claim need not restate every fact attribute (coarse
claims are fine), but every attribute it DOES assert must be grounded.

Score = F1(precision, recall) per example, where
  precision = correct_claims / total_claims   (0 hallucinations -> precision 1.0)
  recall    = matched_facts   / total_facts
`passed` = recall >= RECALL_TARGET (0.8) AND precision == 1.0 — i.e. the bar rewards
completeness but a single ungrounded claim fails the example outright, per the locked
division-of-labor rule (a VLM's job is coarse recall; code's job is to catch anything it
got wrong before it reaches a human).

Baselines:
  * `deterministic` (measured) — `deterministic_claims()` is the describe_scene-style
    non-vision fallback: it emits a claim for every VISIBLE known fact, verbatim. That
    makes it near-perfect precision (it only ever restates a fact) but modest recall (it
    never proposes anything describe_scene could not see — internal features, thread
    pitch, etc.). This is the ceiling a trained VLM must beat to be worth shipping.
  * `frontier` (pending) — the real offline-VLM candidate. No credentials/network in
    CI/offline, so this row is an honest `pending_frontier()` stub; it is filled once
    T-H7 logging + credentials exist, and its residual over `deterministic` feeds T-H10.

Occlusion caveat: `describe_scene` (services/mcp/gsl_tools.py) does NOT model occlusion —
a face behind another still lists if it faces the camera. A claim about a hidden feature
is therefore not automatically wrong; this bench does not penalize claims purely for
naming something that *might* be occluded — matching is by geometry-fact equality only,
never by a visibility heuristic on the CLAIM side (visibility only gates what the
deterministic fallback itself would propose).

Two evaluation paths, mirroring regen_fix's fixture/live split:
  * fixture mode (offline, default): pure data — claims vs known_geometry from committed
    JSON fixtures under training/benches/vlm/scenes/. No C++ core, no model call.
  * live mode (core present): GUARDED. `_core_available()` probes whether
    `mcp_server.core_bridge` importable (never a hardcoded sibling .so path) and records
    an honest skip-with-reason when it is not. `score_example(..., live_check=...)` is the
    extension point for cross-checking a fixture's known_geometry against a real
    `describe_scene()` call once a fixture carries a live `doc_id` — none do today (the
    ticket that shipped this bench treats rendered PNGs as optional), so the live path is
    exercised as an availability probe only, never gating the offline score.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from ..baseline import Baseline, pending_frontier
from ..dataset import DatasetSpec
from ..metrics import mean, pass_rate, roc_auc, residual as residual_metric
from ..driver import BenchDriver
from ..schema import BenchResult, ExampleScore

RECALL_TARGET = 0.8
_BBOX_TOL_ABS = 1.0    # mm
_BBOX_TOL_REL = 0.05   # 5% relative, whichever tolerance is larger

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
FIXTURES_DIR = os.path.join(_REPO_ROOT, "training", "benches", "vlm")
SCENES_DIR = os.path.join(FIXTURES_DIR, "scenes")


# ---- fixture loading ----------------------------------------------------------

def load_fixtures(scenes_dir: str) -> list[dict]:
    """Load every committed `<id>.json` scene fixture, sorted by filename for a stable,
    reproducible example order."""
    out: list[dict] = []
    for name in sorted(os.listdir(scenes_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(scenes_dir, name), encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("example_id", os.path.splitext(name)[0])
        out.append(d)
    return out


# ---- claim <-> fact matching ---------------------------------------------------

def _numeric_close(a: float, b: float) -> bool:
    tol = max(_BBOX_TOL_ABS, _BBOX_TOL_REL * max(abs(a), abs(b)))
    return abs(a - b) <= tol


def _matches(claim: dict, fact: dict) -> bool:
    """True iff `claim` is grounded by `fact`: same `kind`, and every attribute the claim
    asserts is compatible with the fact (numeric within tolerance, else exact equality).
    `visible` is fact-side-only metadata (drives the deterministic baseline), never
    compared. A claim omitting an attribute the fact carries is still a match — a coarse
    claim need not restate everything, but nothing it DOES say may be wrong."""
    if claim.get("kind") != fact.get("kind"):
        return False
    for k, v in claim.items():
        if k in ("kind", "visible"):
            continue
        if k not in fact:
            return False
        fv = fact[k]
        is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
        fv_num = isinstance(fv, (int, float)) and not isinstance(fv, bool)
        if is_num and fv_num:
            if not _numeric_close(float(v), float(fv)):
                return False
        elif v != fv:
            return False
    return True


def _score_claims(claims: list[dict], facts: list[dict]) -> dict[str, Any]:
    """Precision/recall/F1 of a claim set against a fact set. Not a paired-array metric
    (metrics.precision_recall assumes one label per prediction) — this is many-to-many:
    each claim may ground on any fact, each fact may be grounded by any claim."""
    correct = [c for c in claims if any(_matches(c, f) for f in facts)]
    matched_facts = [f for f in facts if any(_matches(c, f) for c in claims)]
    total_claims = len(claims)
    total_facts = len(facts)
    precision = (len(correct) / total_claims) if total_claims else 1.0
    recall = (len(matched_facts) / total_facts) if total_facts else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "correct_claims": len(correct), "total_claims": total_claims,
        "matched_facts": len(matched_facts), "total_facts": total_facts,
        "hallucinations": total_claims - len(correct),
    }


# ---- deterministic claim generator (the non-vision fallback ceiling) ----------

def deterministic_claims(known_geometry: list[dict]) -> list[dict]:
    """describe_scene-style non-vision fallback: emit a claim for every fact it could
    plausibly surface from B-rep face normals (`visible` != False). Restating a known
    fact verbatim makes this near-perfect precision by construction; it never invents
    anything, so its recall is capped by how much of the part is actually visible to a
    face-normal read (internal ribs, thread pitch, etc. are marked `visible: false` in
    fixtures and never proposed)."""
    return [{k: v for k, v in f.items() if k != "visible"}
            for f in known_geometry if f.get("visible", True)]


# ---- guarded live-core probe (never a hardcoded sibling .so path) -------------

def _core_available() -> tuple[bool, str]:
    try:
        from mcp_server import core_bridge as cb  # noqa: F401  (availability probe only)
        return True, "mcp_server.core_bridge importable"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]


# ---- scoring --------------------------------------------------------------------

def score_example(fixture: dict, *, target_recall: float = RECALL_TARGET,
                  live_check: Optional[Callable[[list[dict]], str]] = None) -> ExampleScore:
    """Score one fixture's `claims` against its `known_geometry`. Pure offline data by
    default. `live_check(known_geometry) -> text`, if provided, is a GUARDED hook for a
    real `describe_scene()` cross-check (skip-with-reason on any exception) — it never
    changes the offline score, only annotates `detail`."""
    facts = fixture["known_geometry"]
    claims = fixture["claims"]
    result = _score_claims(claims, facts)
    det_claims = deterministic_claims(facts)
    det_result = _score_claims(det_claims, facts)

    passed = bool(result["recall"] >= target_recall and result["precision"] == 1.0)
    detail: dict[str, Any] = {
        "precision": result["precision"], "recall": result["recall"],
        "hallucinations": result["hallucinations"],
        "total_claims": result["total_claims"], "total_facts": result["total_facts"],
        "deterministic_precision": det_result["precision"],
        "deterministic_recall": det_result["recall"],
        "mode": "fixture",
    }
    if live_check is not None:
        try:
            detail["live_describe_scene"] = live_check(facts)[:400]
            detail["mode"] = "live"
        except Exception as e:  # guarded: no core / no doc_id -> skip-with-reason
            detail["live_error"] = f"{type(e).__name__}: {e}"[:160]

    return ExampleScore(
        example_id=fixture["example_id"], score=result["f1"], passed=passed,
        structure=fixture.get("structure", "unknown"), label=passed,
        baselines={"deterministic": det_result["f1"]}, detail=detail,
    )


def run(scenes_dir: str, dataset_hash: str, *, split: str = "all", limit: Optional[int] = None,
        live_check: Optional[Callable[[list[dict]], str]] = None,
        frontier: Optional[Baseline] = None) -> BenchResult:
    fixtures = load_fixtures(scenes_dir)
    if limit is not None:
        fixtures = fixtures[:limit]
    examples = [score_example(fx, live_check=live_check) for fx in fixtures]

    claim_precision = mean([e.detail["precision"] for e in examples])
    claim_recall = mean([e.detail["recall"] for e in examples])
    claim_accuracy = mean([e.score for e in examples])
    hallucination_count = sum(e.detail["hallucinations"] for e in examples)
    auc = roc_auc([bool(e.label) for e in examples], [e.score for e in examples])

    det_mean = mean([e.baselines["deterministic"] for e in examples])
    det_pass = pass_rate([
        (e.detail["deterministic_recall"] >= RECALL_TARGET
         and e.detail["deterministic_precision"] == 1.0)
        for e in examples
    ])

    per_structure: dict[str, dict] = {}
    for e in examples:
        d = per_structure.setdefault(e.structure, {"n": 0, "passed": 0})
        d["n"] += 1
        d["passed"] += int(e.passed)

    frontier = frontier or pending_frontier()
    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(),
    ]
    if frontier.available:
        cand_mean = mean([e.baselines.get(frontier.name, 0.0) for e in examples])
        resid = {**residual_metric(cand_mean, det_mean), "status": "measured"}
    else:
        resid = {"status": "pending", "baseline_mean": det_mean,
                 "note": "frontier candidate pending; residual measured when it lands (feeds T-H10)"}

    core_available, core_note = _core_available()

    return BenchResult(
        bench_id="vlm", dataset_id="vlm_scenes", dataset_hash=dataset_hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate={
            "claim_precision": claim_precision, "claim_recall": claim_recall,
            "claim_accuracy": claim_accuracy, "hallucination_count": hallucination_count,
            "roc_auc": auc, "per_structure": per_structure,
            "success_bar": {
                f"recall>={RECALL_TARGET}": claim_recall >= RECALL_TARGET,
                "precision==1.0": claim_precision == 1.0,
            },
            "live_core": {"available": core_available, "note": core_note},
        },
        residual=resid,
    )


# ---- driver registration (wired into runner.REGISTRY by the caller) -----------

def _run(smoke: bool, workdir: str) -> BenchResult:
    spec = DatasetSpec.load(os.path.join(FIXTURES_DIR, "spec.json"), SCENES_DIR)
    limit = 4 if smoke else None
    return run(SCENES_DIR, spec.hash, split="smoke" if smoke else "all", limit=limit)


DRIVER = BenchDriver("vlm", _run)
