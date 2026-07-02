"""regen-fix bench: corpus generation, deterministic fixer, and scoring (fixture mode)."""
import copy
import json

import pytest

from tools.flywheel.bench.benches import regen_fix as RF
from tools.flywheel.bench.corpus import regen_corpus as RC


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("regen")
    spec = RC.build_corpus(str(d), count=50, seed=99)
    return str(d), spec


def test_corpus_is_balanced_and_reproducible(corpus, tmp_path):
    corpus_dir, spec = corpus
    assert spec.n_examples == 50
    labels = RC.load_labels(corpus_dir)
    classes = {l["mutation_class"] for l in labels}
    assert classes == set(RC.MUTATION_CLASSES)
    # rebuild elsewhere -> identical hash (OS/path independent)
    spec2 = RC.build_corpus(str(tmp_path / "again"), count=50, seed=99)
    assert spec2.hash == spec.hash


def test_deterministic_fix_unsuppresses_parent():
    seed = RC.load_seed()
    corrupted, target = RC.mutate(seed, "suppressed_parent", __import__("random").Random(1))
    assert target is not None
    fixed, claimed = RF.deterministic_fix({"tree": corrupted})
    assert claimed is True
    assert not RF._has_anomaly(fixed)


def test_deterministic_fix_leaves_dangling_ref_unclaimed():
    seed = RC.load_seed()
    corrupted, _ = RC.mutate(seed, "dangling_ref", __import__("random").Random(2))
    fixed, claimed = RF.deterministic_fix({"tree": corrupted})
    assert claimed is False           # cannot invent the target
    assert RF._has_anomaly(fixed)     # anomaly remains


def test_run_scores_and_residual(corpus):
    corpus_dir, spec = corpus
    res = RF.run(corpus_dir, spec.hash)
    agg = res.aggregate
    # deterministic baseline cleanly fixes exactly the 2 det-fixable classes -> ~40%
    assert 0.30 <= agg["pct_fixed"] <= 0.50
    # the whole point of a kill-switch: it is BELOW the 0.70 model bar (leaves a residual)
    assert agg["success_bar"]["pct_fixed>=0.70"] is False
    # a clean fixer never drifts an intent dimension
    assert agg["intent_drift_count"] == 0
    assert agg["success_bar"]["intent_drift==0"] is True
    # fixer's pass signal ranks det_fixable strictly above residual
    assert agg["roc_auc_det_fixable"] == 1.0
    # both det-fixable classes fully repaired; all three residual classes untouched
    pc = agg["per_class"]
    assert pc["param_out_of_range"]["fixed"] == pc["param_out_of_range"]["n"]
    assert pc["suppressed_parent"]["fixed"] == pc["suppressed_parent"]["n"]
    assert pc["dangling_ref"]["fixed"] == 0 and pc["bad_selector"]["fixed"] == 0
    assert pc["circular_equation"]["fixed"] == 0
    # two baseline rows: deterministic measured, frontier pending
    kinds = {b.kind: b.status for b in res.baselines}
    assert kinds["deterministic"] == "measured"
    assert kinds["frontier"] == "pending"
    assert res.residual["status"] == "pending"


def test_result_json_roundtrips(corpus):
    corpus_dir, spec = corpus
    res = RF.run(corpus_dir, spec.hash, limit=10)
    from tools.flywheel.bench.schema import BenchResult
    back = BenchResult.from_json(res.to_json())
    assert back.bench_id == "regen_fix"
    assert len(back.examples) == 10
    assert back.examples[0].structure  # held-out structure recorded
