"""LIVE regen-fix verifier tests — exercise the AUTHORITATIVE binary verifier against the real
C++ core (compare_parts / eval_compare). Skipped with a reason when the core can't be located.

Run against the built engine with:
    CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so \
    PYTHONPATH=$PWD ~/.local/share/Chudware/mcp-venv/bin/python -m pytest \
    tools/flywheel/bench/tests/test_regen_fix_live.py -q
"""
import copy
import json
import random

import pytest

from tools.flywheel.bench.benches import regen_fix as RF
from tools.flywheel.bench.corpus import regen_corpus as RC

_OPENER, _COMPARE = RF._make_live_verifier()
pytestmark = pytest.mark.skipif(_OPENER is None,
                                reason="C++ core not available (set CHUDWARE_LIBCHUDWARE)")


def _good():
    return {"revision": 0, "tree": RC.load_seed(), "owner_map": {}}


# ---- opener + compare contract ----------------------------------------------

def test_opener_rebuilds_a_good_doc():
    res = _OPENER(_good())
    assert res["rebuilt"] is True and res["doc_id"]


def test_opener_reports_rebuild_failure_with_detail():
    corrupted, _ = RC.mutate(RC.load_seed(), "param_out_of_range", random.Random(3))
    res = _OPENER({"tree": corrupted})
    assert res["rebuilt"] is False
    assert res["error"]  # human-readable feature_status / validation detail


def test_compare_good_vs_good_is_100():
    a = _OPENER(_good())["doc_id"]
    b = _OPENER(_good())["doc_id"]
    assert _COMPARE(a, b) >= RF.LIVE_MATCH_THRESHOLD


# ---- the authoritative binary verifier --------------------------------------

def test_perfect_fix_passes_live():
    """A fix that restores the known-good part rebuilds AND compares ≈100% -> score 1."""
    good = _good()
    score, passed, detail = RF.verify_fix(
        copy.deepcopy(good), claimed=True, intent_dims={},
        good_document=good, opener=_OPENER, compare_fn=_COMPARE)
    assert detail["mode"] == "live"
    assert score == 1.0 and passed


def test_rebuildable_but_geometry_changed_scores_zero():
    """THE load-bearing property: a fix that rebuilds but ALTERED unaffected geometry scores 0,
    even though the rebuild passes (zero tolerance for silent intent change)."""
    good = _good()
    drifted = copy.deepcopy(good)
    # bump a design global -> still rebuilds, but the part is geometrically different from good
    for g in drifted["tree"]["equations"]["globals"]:
        if g["name"] == "W":
            g["expr"] = str(float(g["expr"]) + 40)
    # it must actually still rebuild (otherwise the test proves nothing about the AND-clause)
    assert _OPENER(drifted)["rebuilt"] is True
    score, passed, detail = RF.verify_fix(
        drifted, claimed=True, intent_dims={},
        good_document=good, opener=_OPENER, compare_fn=_COMPARE)
    assert detail["mode"] == "live"
    assert detail["match_pct"] is not None and detail["match_pct"] < RF.LIVE_MATCH_THRESHOLD
    assert score == 0.0 and not passed


def test_unrebuilt_fix_scores_zero():
    """A 'fix' that leaves the doc unbuildable scores 0 (rebuild-passes clause fails).
    Uses param_out_of_range (an impossible extrude depth), which the engine rejects at
    geometry time (feature_status ok=false) — a deterministic unbuildable state."""
    corrupted, _ = RC.mutate(RC.load_seed(), "param_out_of_range", random.Random(4))
    good = _good()
    score, passed, detail = RF.verify_fix(
        {"tree": corrupted}, claimed=True, intent_dims={},
        good_document=good, opener=_OPENER, compare_fn=_COMPARE)
    assert detail["rebuilt"] is False
    assert score == 0.0 and not passed


# ---- end-to-end bench run in LIVE mode, frontier plumbing exercised ----------

def test_run_live_frontier_plumbing(tmp_path):
    """The full bench path executes live: build corpus -> diagnose (mock provider) -> apply ->
    live binary verify -> frontier baseline recorded. Uses a MockProvider, so this proves the
    PLUMBING only; it is NOT a real frontier score (available=True here is a test harness knob)."""
    from services.agent.providers.mock_provider import MockProvider
    from services.agent import regen_diagnosis as RD

    corpus_dir = str(tmp_path / "c")
    spec = RC.build_corpus(corpus_dir, count=10, seed=7)

    # An oracle mock: read the failure signal and emit the correct fix ops (plumbing only).
    class _Oracle(MockProvider):
        def complete(self, system, messages, max_tokens=8000):
            prompt = messages[-1]["content"][0]["text"]
            payload = json.loads(prompt.split("\n", 1)[1])
            ops = []
            for f in payload["failures"]:
                k = f["kind"]
                if k == "dangling_ref":
                    ops.append({"op": "repoint_ref", "feature": f["feature"],
                                "from": f["options"]["from"],
                                "to": f["options"]["valid_feature_ids"][0]})
                elif k == "bad_selector":
                    ops.append({"op": "set_selector_kind", "feature": f["feature"],
                                "old_kind": f["options"]["bad_kind"], "kind": "face"})
                elif k == "suppressed_parent":
                    ops.append({"op": "unsuppress", "feature": f["feature"]})
                elif k == "param_out_of_range":
                    ops.append({"op": "set_param", "feature": f["feature"], "param": None, "value": 1.0})
                elif k == "circular_equation":
                    ops.append({"op": "set_equation", "global": f["options"]["cycle"][0], "expr": "10"})
            return json.dumps({"diagnosis": "repair", "fix_ops": ops})

    frontier = RF.make_frontier_baseline(_Oracle([""]), available=True,
                                         consent_fn=lambda: "meta")
    res = RF.run(corpus_dir, spec.hash, opener=_OPENER, compare_fn=_COMPARE, frontier=frontier)
    assert res.aggregate["verifier_mode"] == "live"
    # frontier baseline row is measured (a real strategy ran through the live verifier)
    kinds = {b.kind: b.status for b in res.baselines}
    assert kinds["frontier"] == "measured"
    assert res.residual["status"] == "measured"
    # every example carries a binary frontier score
    for e in res.examples:
        assert e.baselines["frontier"] in (0.0, 1.0)
        assert e.detail["frontier"]["mode"] == "live"
