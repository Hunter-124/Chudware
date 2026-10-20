"""regen-fix bench (IP-18): can a strategy repair a corrupted doc so it rebuilds, without
drifting design intent?

Score per example (survey §6): fixed == rebuild passes AND unaffected bodies compare ≈100%
AND no intent-dimension drift. Corpus + ground truth come from
`tools.flywheel.bench.corpus.regen_corpus`.

Two evaluation paths (see verify_fix):
  * fixture mode (offline, default): a fix is scored structurally — the strategy claimed a
    fix, the anomaly is gone, and no intent dim drifted. No C++ core needed.
  * live mode (core present, auto-detected): the AUTHORITATIVE binary verifier — the fix is
    rebuilt through the engine and compared (compare_parts / eval_compare) against the
    known-good part; pass iff it rebuilds AND matches ≈100%. A fix that rebuilds but altered
    geometry scores 0 (T-H11 driver note; zero tolerance for silent intent changes). A live
    verification that cannot run (core fault, reference not rebuilding, degraded compare) is
    an ERROR row scored as not-fixed — never a structural pass — and marks the run
    ``degraded`` (not promotion evidence). Fixture mode is only used when no live verifier
    was requested or the core is unavailable.

A strategy is pluggable via the ``frontier`` Baseline: the deterministic kill-switch always
runs (its result is the reported baseline); a frontier strategy, when credentials make one
available, is scored through the SAME verifier and recorded as the ``frontier`` baseline row
(pending — never fabricated — otherwise). The IP-18 diagnosis strategy lives in
``services.agent.regen_diagnosis``; wire it in with ``make_frontier_baseline``.

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
from ..corpus.regen_corpus import load_good, load_labels
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




# ---- live-core binary verifier (guarded) ------------------------------------
# The AUTHORITATIVE verifier (T-H11 driver note): apply candidate fix -> rebuild passes AND
# compare_parts ≈100% vs the known-good part on unaffected bodies. A fix that rebuilds but
# altered geometry scores 0. Load via doc_new + canonical doc ops, remapping authored feature ids
# to the engine's newly minted ids; failure surfaces as a raised CoreError (schema) OR
# a feature_status entry with ok=false (geometry); compare = eval_compare -> match_pct.

LIVE_MATCH_THRESHOLD = 99.5  # match_pct >= this == "unaffected bodies compare ≈100%"


def _core_err(e) -> str:
    detail = getattr(e, "detail", None)
    if isinstance(detail, dict) and detail.get("reason"):
        return f"{type(e).__name__}: {e} ({detail['reason']})"[:200]
    return f"{type(e).__name__}: {e}"[:200]



def _document_ops(tree: dict) -> list[dict]:
    """Replay source geometry, not compiled/readback fields hoisted as feature params."""
    if tree.get("schema", "aicad.part/1") != "aicad.part/1":
        raise ValueError("regen verifier requires a part document")
    if tree.get("rollback_after"):
        raise ValueError("regen verifier cannot replay an active rollback bar with doc ops")
    ops = []
    if tree.get("units"):
        ops.append({"op": "set_units", "system": tree["units"]["system"]})
    if tree.get("material"):
        ops.append({"op": "set_material", "key": tree["material"]["key"]})
    ops.extend({"op": "eq_set", "name": g["name"], "expr": g["expr"]}
               for g in tree.get("equations", {}).get("globals", []))
    features = tree.get("features", [])
    mapping = {f["id"]: f"${len(ops) + i}" for i, f in enumerate(features)}

    def remap(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        return value

    for feature in features:
        envelope = {k: copy.deepcopy(feature[k])
                    for k in ("type", "name", "suppressed", "params", "refs", "folder")
                    if k in feature}
        for key in ("params", "refs"):
            if key in envelope:
                envelope[key] = remap(envelope[key])
        ops.append({"op": "add_feature", "feature": envelope})
    return ops


def _make_live_verifier():
    """Return (opener, compare_fn) bound to the C++ core, or (None, None) when it can't be
    located/loaded — never a hardcoded path (resolution is core_bridge's, via
    $CHUDWARE_LIBCHUDWARE / $CHUDWARE_DIST / build dirs). Mirrors spec_sketch._make_doc_id_opener.

        opener(document) -> {"doc_id": str|None, "rebuilt": bool, "error": str|None}
        compare_fn(a, b) -> match_pct float|None   (100 == identical; None == degraded)
    """
    try:
        from services.mcp import core_bridge as cb  # Core-only path.
    except Exception:
        return None, None
    if not cb.available():
        return None, None

    def close(doc_id: str) -> None:
        cb.call("doc_close", {"doc_id": doc_id})

    def opener(document: dict) -> dict:
        tree = document.get("tree", document)
        doc_id = None
        try:
            ops = _document_ops(tree)
            doc_id = cb.call("doc_new", {"kind": "part", "name": tree.get("name", "regen_doc"),
                                         "internal": True})["doc_id"]
            resp = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops, "with_tess": False})
            authored = iter(tree.get("features", []))
            source_ids = {}
            for index, op in enumerate(ops):
                if op["op"] == "add_feature":
                    feature = next(authored)
                    minted = (resp.get("id_map") or {}).get(f"${index}")
                    if minted:
                        source_ids[minted] = feature["id"]
            fs = (resp.get("rebuild", {}) or {}).get("feature_status") or resp.get("feature_status") or []
            bad = [f'{source_ids.get(s.get("id"), s.get("id"))}: {s.get("error")}'
                   for s in fs if not s.get("ok", True)]
            if bad:
                close(doc_id)
                return {"doc_id": None, "rebuilt": False, "error": "; ".join(bad)}
            return {"doc_id": doc_id, "rebuilt": True, "error": None}
        except Exception as e:
            if doc_id:
                close(doc_id)
            return {"doc_id": None, "rebuilt": False, "error": _core_err(e)}

    opener.close = close

    def compare_fn(a: str, b: str):
        try:
            r = cb.call("eval_compare", {"doc_a": a, "doc_b": b})
        except Exception:
            return None
        return None if r.get("degraded") else r.get("match_pct")

    return opener, compare_fn


# ---- shared binary verifier -------------------------------------------------

def _verification_error(detail: dict, why: str):
    """The live verifier itself could not judge the fix: a grader/infra ERROR, scored as
    not-fixed. It never falls back to the structural fixture checks — those would credit a
    repair whose rebuild nobody observed."""
    detail["verdict"] = "error"
    detail["live_error"] = why[:200]
    return 0.0, False, detail


def verify_fix(fixed: dict, claimed: bool, intent_dims: dict, *,
               good_document: Optional[dict] = None, opener=None, compare_fn=None):
    """Score one candidate fix. Returns (score{0.0|1.0}, passed, detail).

    Live mode (opener + compare_fn + good_document present): binary — pass iff the fix rebuilds
    AND compare_parts vs the known-good part >= LIVE_MATCH_THRESHOLD. If the live verifier
    itself fails (an opener/compare exception, the known-good reference not rebuilding, or a
    degraded compare), the example is ``detail["verdict"] == "error"`` and NOT fixed; the
    structural fields stay in ``detail`` as diagnostics only. Fixture mode (offline, no live
    verifier configured): structural — pass iff the strategy CLAIMED a fix, the anomaly is
    cleared, and no intent dimension drifted. Both are strictly binary (no partial credit): the
    driver note is explicit that a fix altering unaffected geometry scores 0 even if the
    rebuild passes.
    """
    intent_ok = _intent_preserved(fixed, intent_dims)
    anomaly_gone = not _has_anomaly(fixed)
    detail: dict[str, Any] = {"claimed_fixed": claimed, "anomaly_gone": anomaly_gone,
                              "intent_preserved": intent_ok, "mode": "fixture"}

    if opener is not None and compare_fn is not None and good_document is not None:
        detail["mode"] = "live"
        opened = []
        try:
            try:
                res = opener(fixed)
                if res.get("doc_id"):
                    opened.append(res["doc_id"])
            except Exception as e:  # noqa: BLE001
                return _verification_error(detail, f"opener(fix) {type(e).__name__}: {e}")
            detail["rebuilt"] = bool(res.get("rebuilt"))
            if res.get("error"):
                detail["rebuild_error"] = str(res["error"])[:200]
            if not (claimed and res.get("rebuilt") and res.get("doc_id")):
                detail["verdict"] = "fail"
                return 0.0, False, detail
            try:
                good_res = opener(good_document)
                if good_res.get("doc_id"):
                    opened.append(good_res["doc_id"])
            except Exception as e:  # noqa: BLE001
                return _verification_error(detail, f"opener(known-good) {type(e).__name__}: {e}")
            if not (good_res.get("rebuilt") and good_res.get("doc_id")):
                return _verification_error(
                    detail, f"known-good reference did not rebuild: {good_res.get('error')}")
            try:
                match = compare_fn(res["doc_id"], good_res["doc_id"])
            except Exception as e:  # noqa: BLE001
                return _verification_error(detail, f"compare {type(e).__name__}: {e}")
            detail["match_pct"] = match
            if match is None:
                return _verification_error(detail, "eval_compare degraded (no match_pct)")
            passed = bool(match >= LIVE_MATCH_THRESHOLD)
            detail["verdict"] = "pass" if passed else "fail"
            return (1.0 if passed else 0.0), passed, detail
        finally:
            closer = getattr(opener, "close", None)
            if closer:
                for doc_id in opened:
                    closer(doc_id)

    passed = bool(claimed and anomaly_gone and intent_ok)
    detail["verdict"] = "pass" if passed else "fail"
    return (1.0 if passed else 0.0), passed, detail


# ---- scoring ----------------------------------------------------------------

def score_example(corpus_dir: str, label: dict, *, opener=None, compare_fn=None,
                  frontier_fn=None) -> ExampleScore:
    """Score one corrupted example. Always scores the deterministic kill-switch (its result is
    the ExampleScore.score, preserving the frozen fixture semantics). When ``frontier_fn`` is
    given (a real frontier ran), the frontier fix is scored through the SAME verifier and stored
    in ``baselines['frontier']``. ``opener``/``compare_fn`` (live core) select the binary
    rebuild+compare_parts verifier; without them scoring is structural (fixture)."""
    ex_dir = os.path.join(corpus_dir, label["example_id"])
    with open(os.path.join(ex_dir, "document.json"), encoding="utf-8") as f:
        corrupted = json.load(f)
    intent_dims = label.get("intent_dims", {})
    live = opener is not None and compare_fn is not None
    good_document = load_good(corpus_dir, label["example_id"]) if live else None

    fixed_det, claimed_det = deterministic_fix(corrupted)
    det_score, det_passed, detail = verify_fix(
        fixed_det, claimed_det, intent_dims,
        good_document=good_document, opener=opener, compare_fn=compare_fn)
    detail["mutation_class"] = label["mutation_class"]
    baselines = {"deterministic": det_score}

    if frontier_fn is not None:
        fixed_fr, meta = frontier_fn(corrupted)
        claimed_fr = bool(meta.get("n_applied", 0) > 0)
        fr_score, _fr_passed, fr_detail = verify_fix(
            fixed_fr, claimed_fr, intent_dims,
            good_document=good_document, opener=opener, compare_fn=compare_fn)
        baselines["frontier"] = fr_score
        detail["frontier"] = {**fr_detail, "diagnosis": meta.get("diagnosis", ""),
                              "n_applied": meta.get("n_applied", 0)}

    return ExampleScore(example_id=label["example_id"], score=det_score, passed=det_passed,
                        structure=label["structure"], label=label["det_fixable"],
                        baselines=baselines, detail=detail)


def run(corpus_dir: str, dataset_hash: str, *, split: str = "all", limit: Optional[int] = None,
        opener=None, compare_fn=None, frontier: Optional[Baseline] = None,
        live: bool = False) -> BenchResult:
    labels = load_labels(corpus_dir)
    if limit is not None:
        labels = labels[:limit]

    frontier = frontier or pending_frontier()
    # A frontier row is 'measured' ONLY when a real, available frontier strategy runs. With no
    # credentials it stays pending (baseline.py contract) — never a fabricated number.
    frontier_fn = frontier.apply if frontier.available else None

    # The live binary verifier is OPT-IN (explicit opener, live=True, or a real frontier
    # candidate that needs a geometry compare) — never silently toggled by ambient env, so an
    # offline deterministic run stays reproducibly in fixture mode. Degrades to fixture with a
    # recorded reason when the core can't be located.
    if opener is None and compare_fn is None and (live or frontier.available):
        opener, compare_fn = _make_live_verifier()

    examples = [score_example(corpus_dir, lab, opener=opener, compare_fn=compare_fn,
                              frontier_fn=frontier_fn) for lab in labels]

    det_mean = mean([e.score for e in examples])
    det_pass = pass_rate([e.passed for e in examples])
    auc = roc_auc([bool(e.label) for e in examples], [e.score for e in examples])
    # §6: count only APPLIED fixes that drifted intent, not unrepaired residuals.
    intent_drift = sum(1 for e in examples
                       if e.detail.get("claimed_fixed") and not e.detail.get("intent_preserved", True))

    per_class: dict[str, dict] = {}
    for e in examples:
        c = e.detail["mutation_class"]
        d = per_class.setdefault(c, {"n": 0, "fixed": 0})
        d["n"] += 1
        d["fixed"] += int(e.passed)

    verifier_mode = examples[0].detail.get("mode", "fixture") if examples else "fixture"
    # A live verification that could not run is an ERROR row scored as not-fixed (verify_fix
    # never falls back to structural checks). Any such row marks the run degraded, and a
    # degraded run is not promotion evidence: its fix-rate is bounded by verifier faults, not
    # measured on every example.
    live_error_count = sum(1 for e in examples if e.detail.get("verdict") == "error")
    frontier_error_count = sum(1 for e in examples
                               if (e.detail.get("frontier") or {}).get("verdict") == "error")
    if verifier_mode == "live" and (live_error_count or frontier_error_count):
        verifier_mode = "degraded"
    agg = {
        "pct_fixed": det_pass, "deterministic_mean": det_mean,
        "roc_auc_det_fixable": auc, "intent_drift_count": intent_drift,
        "per_class": per_class, "verifier_mode": verifier_mode,
        "live_error_count": live_error_count,
        "frontier_live_error_count": frontier_error_count,
        "promotion_evidence": verifier_mode == "live",
        "success_bar": {"pct_fixed>=0.70": det_pass >= 0.70, "intent_drift==0": intent_drift == 0,
                        "no_verifier_errors": not (live_error_count or frontier_error_count)},
    }

    frontier_mean = frontier_pass = None
    if frontier_fn is not None:
        fr_scores = [e.baselines.get("frontier", 0.0) for e in examples]
        frontier_mean = mean(fr_scores)
        frontier_pass = pass_rate([s >= 1.0 for s in fr_scores])
        fr_drift = sum(1 for e in examples
                       if e.detail.get("frontier", {}).get("claimed_fixed")
                       and not e.detail.get("frontier", {}).get("intent_preserved", True))
        agg["frontier_mean"] = frontier_mean
        agg["frontier_pct_fixed"] = frontier_pass
        agg["frontier_intent_drift"] = fr_drift
        # PRE-REGISTERED target (frozen before any data; do not move goalposts): ≥70% auto-fix,
        # zero fixes that alter unaffected geometry.
        agg["success_bar"]["frontier_pct_fixed>=0.70"] = frontier_pass >= 0.70
        agg["success_bar"]["frontier_intent_drift==0"] = fr_drift == 0

    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(mean_score=frontier_mean, pass_rate=frontier_pass),
    ]
    if not frontier.available:
        resid = {"status": "pending", "baseline_mean": det_mean,
                 "note": "frontier candidate pending credentials; residual measured when it lands"}
    else:
        resid = {**residual_metric(frontier_mean, det_mean), "status": "measured"}

    return BenchResult(
        bench_id="regen_fix", dataset_id="regen_fix_corpus", dataset_hash=dataset_hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate=agg, residual=resid,
    )


# ---- frontier strategy bridge -----------------------------------------------

def make_frontier_baseline(provider, *, available: bool = True, on_event=None,
                           consent_fn=None, run_id=None, name: str = "frontier") -> Baseline:
    """Build the IP-18 frontier Baseline from a Provider, wrapping
    ``services.agent.regen_diagnosis.diagnose`` (which itself routes the call through T-H7's
    LoggingProvider at IP-18, so every scored example is also a logged correction trace).

    ``available`` MUST reflect whether a REAL frontier provider (credentials + network) backs
    this — pass ``False`` for a mock/offline provider so ``run`` records the frontier row as
    *pending* and never a fabricated score (baseline.py contract; the T-F13 honesty posture).
    """
    from services.agent.regen_diagnosis import diagnose

    def _apply(corrupted_document: dict):
        d = diagnose(corrupted_document, provider, on_event=on_event,
                     consent_fn=consent_fn, run_id=run_id)
        return d.fixed_document, {"diagnosis": d.diagnosis, "n_applied": d.n_applied}

    return Baseline(name=name, kind="frontier", fn=_apply, available=available,
                    note="IP-18 diagnosis via services.agent.regen_diagnosis (logged at IP-18)")
