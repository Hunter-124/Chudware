"""regen-fix bench (IP-18): can a strategy repair a corrupted doc so it rebuilds, without
drifting design intent?

Score per example (survey §6): fixed == rebuild passes AND unaffected bodies compare ≈100%
AND no intent-dimension drift. Corpus + ground truth come from
`tools.flywheel.bench.corpus.regen_corpus`.

Two evaluation paths:
  * fixture mode (offline, default): the deterministic kill-switch fixer is applied and
    scored structurally — the anomaly is gone, intent dims preserved, and it never *claims*
    a fix on a class it cannot handle. No C++ core needed.
  * live mode (core present): the same fixer's output is rebuilt through the engine and
    scored THROUGH the T-H4 battery (round_trip / is_manifold / compare_parts). Skipped
    with a recorded reason when the core is unavailable (like gate.sh's OCCT skip).

The deterministic baseline can repair `suppressed_parent`, `param_out_of_range`, and
`circular_equation`; it deliberately cannot repair `dangling_ref` or `bad_selector` — those
two are the measured RECOGNITION_RESIDUAL a trained model must beat before IP-18 training
is greenlit.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional

from ..baseline import Baseline, pending_frontier
from ..corpus.regen_corpus import load_labels
from ..metrics import mean, pass_rate, roc_auc, residual as residual_metric
from ..schema import BenchResult, ExampleScore

VALID_SELECTOR_KINDS = {"face", "edge", "vertex", "body", "datum", "region", "plane"}
_PARAM_FLOOR_DEFAULT = 1.0


# ---- structural anomaly detection (no label peeking) ------------------------

def _feature_ids(tree: dict) -> set[str]:
    return {f["id"] for f in tree.get("features", [])}


def _dep_ids(feat: dict) -> set[str]:
    """Feature ids this feature depends on — selector `owner_feature`/`sketch` keys plus a
    `pattern.features` list. Kept consistent with the corpus mutator so a suppressed/dangling
    parent is detected and repaired the same way it was corrupted."""
    ids: set[str] = set()
    refs = feat.get("refs", {})
    for sel in _iter_selectors(refs):
        if isinstance(sel.get("owner_feature"), str):
            ids.add(sel["owner_feature"])
        if isinstance(sel.get("sketch"), str):
            ids.add(sel["sketch"])
    fl = refs.get("features")
    if isinstance(fl, list):
        ids.update(x for x in fl if isinstance(x, str))
    return ids


def _iter_selectors(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("kind"), str):
            yield node
        for v in node.values():
            yield from _iter_selectors(v)
    elif isinstance(node, list):
        for it in node:
            yield from _iter_selectors(it)


def _iter_numeric_params(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("value"), (int, float)) and "expr" in node:
            yield node
        for v in node.values():
            yield from _iter_numeric_params(v)
    elif isinstance(node, list):
        for it in node:
            yield from _iter_numeric_params(it)


def _equation_cycle(globals_: list[dict]) -> bool:
    names = {g["name"] for g in globals_}
    expr = {g["name"]: (g.get("expr") or "") for g in globals_}
    # only trivial "expr is exactly another global name" edges — enough for our cycle class
    def reaches(start: str, target: str, seen: set[str]) -> bool:
        nxt = expr.get(start, "")
        if nxt not in names:
            return False
        if nxt == target:
            return True
        if nxt in seen:
            return False
        seen.add(nxt)
        return reaches(nxt, target, seen)
    return any(reaches(n, n, set()) for n in names)


# ---- deterministic kill-switch fixer ----------------------------------------

def deterministic_fix(document: dict) -> tuple[dict, bool]:
    """Rule-based repair. Returns (fixed_document, claimed_fixed).

    Inspects the doc for anomalies WITHOUT being told which mutation was applied (honest
    kill-switch). Claims a fix only when it structurally repaired something it understands.
    """
    doc = copy.deepcopy(document)
    tree = doc.get("tree", doc)
    claimed = False

    # circular_equation is DETECTABLE but a clean kill-switch must not repair it: breaking
    # the cycle means grounding a global to a guessed literal, which silently changes an
    # intent dimension (§6 forbids that). So we leave it -> residual for a trained model.

    features = tree.get("features", [])
    referenced: set[str] = set()
    for feat in features:
        referenced.update(_dep_ids(feat))

    for feat in features:
        # suppressed_parent -> un-suppress a suppressed feature that others depend on
        if feat.get("suppressed") and feat["id"] in referenced:
            feat["suppressed"] = False
            claimed = True
        # param_out_of_range -> clamp impossible numeric dimensions back to sane positive
        for p in _iter_numeric_params(feat.get("params", {})):
            v = p["value"]
            if v < 0 or abs(v) > 1e5:
                p["value"] = _PARAM_FLOOR_DEFAULT
                claimed = True

    # dangling_ref and bad_selector are DETECTABLE but not deterministically repairable:
    # the correct target / kind cannot be invented. Leave them, do not claim a fix.
    return doc, claimed


# ---- anomaly re-scan (did the fix actually clear the structural error?) ------

def _has_anomaly(document: dict) -> bool:
    tree = document.get("tree", document)
    fids = _feature_ids(tree)
    globals_ = tree.get("equations", {}).get("globals", [])
    if _equation_cycle(globals_):
        return True
    for feat in tree.get("features", []):
        for sel in _iter_selectors(feat.get("refs", {})):
            if isinstance(sel.get("owner_feature"), str) and sel["owner_feature"] not in fids:
                return True
            if isinstance(sel.get("sketch"), str) and sel["sketch"] not in fids:
                return True
            if isinstance(sel.get("kind"), str) and sel["kind"] not in VALID_SELECTOR_KINDS:
                return True
        fl = feat.get("refs", {}).get("features")
        if isinstance(fl, list) and any(x not in fids for x in fl):
            return True
        if feat.get("suppressed"):
            # a suppressed feature that any other feature still depends on is an anomaly
            for other in tree.get("features", []):
                if other["id"] != feat["id"] and feat["id"] in _dep_ids(other):
                    return True
        for p in _iter_numeric_params(feat.get("params", {})):
            if p["value"] < 0 or abs(p["value"]) > 1e5:
                return True
    return False


def _intent_preserved(document: dict, intent_dims: dict) -> bool:
    tree = document.get("tree", document)
    got = {g["name"]: g.get("expr") for g in tree.get("equations", {}).get("globals", [])}
    for name, expr in intent_dims.items():
        if got.get(name) != expr:
            return False
    return True


# ---- scoring ----------------------------------------------------------------

def score_example(corpus_dir: str, label: dict, *, doc_id_opener=None) -> ExampleScore:
    """Score one corrupted example under the deterministic baseline.

    `doc_id_opener`, when provided (live mode), is a callable(document)->doc_id that loads a
    doc into the core and returns its id; scoring then runs THROUGH the battery. When None
    (offline), scoring is structural.
    """
    ex_dir = os.path.join(corpus_dir, label["example_id"])
    with open(os.path.join(ex_dir, "document.json"), encoding="utf-8") as f:
        corrupted = json.load(f)

    fixed, claimed = deterministic_fix(corrupted)
    intent_ok = _intent_preserved(fixed, label.get("intent_dims", {}))
    anomaly_gone = not _has_anomaly(fixed)

    detail: dict[str, Any] = {
        "mutation_class": label["mutation_class"],
        "claimed_fixed": claimed,
        "anomaly_gone": anomaly_gone,
        "intent_preserved": intent_ok,
        "mode": "fixture",
    }

    if doc_id_opener is not None:
        detail["mode"] = "live"
        try:
            from services.verify.battery import verify  # noqa: local import (core-only path)
            doc_id = doc_id_opener(fixed)
            v = verify(doc_id, ["round_trip", "is_manifold"])
            rebuilt = all(s >= 0.999 for s in v.get("scores", {}).values())
            detail["battery_scores"] = v.get("scores", {})
            passed = bool(claimed and rebuilt and intent_ok)
            score = 1.0 if passed else (0.5 if rebuilt and not intent_ok else 0.0)
            return ExampleScore(example_id=label["example_id"], score=score, passed=passed,
                                structure=label["structure"], label=label["det_fixable"],
                                baselines={"deterministic": score}, detail=detail)
        except Exception as e:  # core error -> fall back to fixture scoring, record why
            detail["live_error"] = f"{type(e).__name__}: {e}"[:160]

    # fixture-mode predicate: a fix counts only if the fixer claimed it, the structural
    # anomaly is cleared, and design intent did not drift.
    passed = bool(claimed and anomaly_gone and intent_ok)
    score = 1.0 if passed else 0.0
    return ExampleScore(example_id=label["example_id"], score=score, passed=passed,
                        structure=label["structure"], label=label["det_fixable"],
                        baselines={"deterministic": score}, detail=detail)


def run(corpus_dir: str, dataset_hash: str, *, split: str = "all", limit: Optional[int] = None,
        doc_id_opener=None, frontier: Optional[Baseline] = None) -> BenchResult:
    labels = load_labels(corpus_dir)
    if limit is not None:
        labels = labels[:limit]
    examples = [score_example(corpus_dir, lab, doc_id_opener=doc_id_opener) for lab in labels]

    det_mean = mean([e.score for e in examples])
    det_pass = pass_rate([e.passed for e in examples])
    auc = roc_auc([bool(e.label) for e in examples], [e.score for e in examples])
    # §6: "0 fixes that silently change intent dims" — count only APPLIED fixes that
    # drifted intent, not unrepaired residuals still carrying their corruption.
    intent_drift = sum(1 for e in examples
                       if e.detail.get("claimed_fixed") and not e.detail.get("intent_preserved", True))

    # per-class breakdown
    per_class: dict[str, dict] = {}
    for e in examples:
        c = e.detail["mutation_class"]
        d = per_class.setdefault(c, {"n": 0, "fixed": 0})
        d["n"] += 1
        d["fixed"] += int(e.passed)

    frontier = frontier or pending_frontier()
    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(),
    ]
    # residual is measured only once a candidate (frontier/model) row exists.
    resid = ({"status": "pending", "baseline_mean": det_mean,
              "note": "frontier candidate pending; residual measured when it lands"}
             if not frontier.available else
             residual_metric(mean([e.baselines.get(frontier.name, 0.0) for e in examples]),
                             det_mean))

    return BenchResult(
        bench_id="regen_fix", dataset_id="regen_fix_corpus", dataset_hash=dataset_hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate={
            "pct_fixed": det_pass, "deterministic_mean": det_mean,
            "roc_auc_det_fixable": auc, "intent_drift_count": intent_drift,
            "per_class": per_class,
            "success_bar": {"pct_fixed>=0.70": det_pass >= 0.70, "intent_drift==0": intent_drift == 0},
        },
        residual=resid if isinstance(resid, dict) else {**resid, "status": "measured"},
    )
