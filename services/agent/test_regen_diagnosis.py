"""IP-18 regen-diagnosis: structural failure detection, doc-op application, and the logged
frontier call path. Offline — no credentials, no C++ core (MockProvider drives every call)."""
import json
import random

import pytest

from services.agent import regen_diagnosis as RD
from services.agent.providers.mock_provider import MockProvider
from tools.flywheel.bench.corpus import regen_corpus as RC


# ---- oracle: turn the structural failure signal into the correct fix ops -----
# This exercises the parse/apply/verify PLUMBING only. It is never recorded as a frontier
# score (baseline.py keeps the frontier row pending without real credentials).

def _oracle_ops(failures):
    ops = []
    for f in failures:
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "bad_selector":
            ops.append({"op": "set_selector_kind", "feature": f.feature,
                        "old_kind": f.options["bad_kind"], "kind": "face"})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
        elif f.kind == "param_out_of_range":
            ops.append({"op": "set_param", "feature": f.feature, "param": None, "value": 1.0})
        elif f.kind == "circular_equation":
            ops.append({"op": "set_equation", "global": f.options["cycle"][0], "expr": "10"})
    return ops


def _oracle_reply(document):
    failures = RD.detect_failures(document)
    return json.dumps({"diagnosis": "structural repair", "fix_ops": _oracle_ops(failures)})


def _corrupt(mclass, seed=1):
    good = RC.load_seed()
    corrupted, target = RC.mutate(good, mclass, random.Random(seed))
    assert corrupted is not None, mclass
    return {"revision": 0, "tree": corrupted, "owner_map": {}}, target


# ---- detection --------------------------------------------------------------

@pytest.mark.parametrize("mclass", RC.MUTATION_CLASSES)
def test_detect_finds_each_class(mclass):
    doc, _ = _corrupt(mclass)
    failures = RD.detect_failures(doc)
    kinds = {f.kind for f in failures}
    assert mclass in kinds, f"{mclass} not detected: got {kinds}"


def test_clean_doc_has_no_failures():
    good = {"revision": 0, "tree": RC.load_seed(), "owner_map": {}}
    assert RD.detect_failures(good) == []


# ---- application ------------------------------------------------------------

@pytest.mark.parametrize("mclass", RC.MUTATION_CLASSES)
def test_oracle_ops_clear_the_anomaly(mclass):
    doc, _ = _corrupt(mclass)
    failures = RD.detect_failures(doc)
    fixed, n = RD.apply_fix_ops(doc, _oracle_ops(failures))
    assert n >= 1
    assert RD.detect_failures(fixed) == [], f"{mclass} still anomalous after fix"


def test_apply_skips_unknown_and_non_applicable_ops():
    doc, _ = _corrupt("suppressed_parent")
    fixed, n = RD.apply_fix_ops(doc, [
        {"op": "not_a_real_op", "feature": "f1"},
        {"op": "set_selector_kind", "feature": "f1", "kind": "not_a_kind"},  # invalid kind -> skip
        {"op": "unsuppress", "feature": "does_not_exist"},                    # missing feat -> skip
    ])
    assert n == 0  # nothing valid applied
    # the real anomaly is untouched (we didn't fix it) -> still detected
    assert any(f.kind == "suppressed_parent" for f in RD.detect_failures(fixed))


# ---- the logged frontier call path ------------------------------------------

@pytest.mark.parametrize("mclass", RC.MUTATION_CLASSES)
def test_diagnose_end_to_end_repairs(mclass):
    doc, _ = _corrupt(mclass)
    provider = MockProvider([_oracle_reply(doc)])
    result = RD.diagnose(doc, provider, consent_fn=lambda: "meta")
    assert result.n_applied >= 1
    assert RD.detect_failures(result.fixed_document) == []
    assert result.diagnosis


def test_diagnose_emits_ip18_correction_trace():
    doc, _ = _corrupt("dangling_ref")
    events = []
    provider = MockProvider([_oracle_reply(doc)])
    RD.diagnose(doc, provider, on_event=events.append, consent_fn=lambda: "meta")
    arts = [e for e in events if e.get("kind") == "ai_artifact"]
    assert arts, "no ai_artifact event emitted"
    assert arts[0]["insertion_point"] == "IP-18"


def test_diagnose_survives_malformed_reply():
    doc, _ = _corrupt("bad_selector")
    provider = MockProvider(["sorry, I cannot help with that"])
    result = RD.diagnose(doc, provider, consent_fn=lambda: "meta")
    assert result.n_applied == 0
    assert result.fix_ops == []
    # doc unchanged -> the anomaly is still there (honest 'no fix')
    assert any(f.kind == "bad_selector" for f in RD.detect_failures(result.fixed_document))
