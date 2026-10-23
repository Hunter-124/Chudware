"""IP-18 fix-preview card: diff render, accept/edit/reject actions, and ai_artifact_action
telemetry (correlated to the diagnosis, carrying the verifier score). Offline — MockProvider."""
import json
import random

from services.agent import regen_card as CARD
from services.agent import regen_diagnosis as RD
from services.agent.providers.mock_provider import MockProvider
from tools.flywheel.bench.corpus import regen_corpus as RC


def _diagnose(mclass, seed=1):
    corrupted, _ = RC.mutate(RC.load_seed(), mclass, random.Random(seed))
    doc = {"revision": 0, "tree": corrupted, "owner_map": {}}
    failures = RD.detect_failures(doc)
    ops = []
    for f in failures:
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
    reply = json.dumps({"diagnosis": "repair", "fix_ops": ops})
    events = []
    d = RD.diagnose(doc, MockProvider([reply]), on_event=events.append, consent_fn=lambda: "meta")
    return doc, d, events


def test_card_has_diagnosis_diff_and_verifier():
    doc, d, _ = _diagnose("suppressed_parent")
    card = CARD.build_fix_preview(doc, d, verifier_score=1.0, verifier_detail={"mode": "live"})
    assert card["kind"] == "fix_preview_card"
    assert card["insertion_point"] == "IP-18"
    assert card["diagnosis"]
    assert card["fix_ops"]                          # editable ops shown
    assert card["changes"] and card["changes"][0]["text"]   # plain-English preview (always present)
    assert "semantic_diff" in card                  # doc_diff slot (may be enrichment/unavailable)
    assert card["verifier"]["score"] == 1.0 and card["verifier"]["passed"] is True
    assert set(card["actions"]) == {"accept", "edit", "reject"}


def test_card_correlates_to_the_diagnosis_artifact():
    doc, d, events = _diagnose("dangling_ref", seed=2)
    assert d.artifact_id is not None                # captured from the logged IP-18 emit
    card = CARD.build_fix_preview(doc, d, verifier_score=1.0)
    assert card["artifact_id"] == d.artifact_id


def test_accept_emits_action_with_verifier_score():
    doc, d, _ = _diagnose("suppressed_parent")
    card = CARD.build_fix_preview(doc, d, verifier_score=1.0)
    out = []
    result_doc = CARD.resolve(card, CARD.ACCEPT, out.append, dwell_ms=1200)
    assert result_doc is d.fixed_document
    act = out[0]
    assert act["kind"] == "ai_artifact_action"
    assert act["action"] == "accept"
    assert act["artifact_id"] == d.artifact_id      # same artifact as the diagnosis
    assert act["verifier_scores"]["regen_fix"] == 1.0
    assert act["dwell_ms"] == 1200


def test_reject_emits_action_and_keeps_failed_doc():
    doc, d, _ = _diagnose("dangling_ref", seed=3)
    card = CARD.build_fix_preview(doc, d, verifier_score=0.0)
    out = []
    result_doc = CARD.resolve(card, CARD.REJECT, out.append)
    assert result_doc is None                       # rejected -> do not load the fix
    assert out[0]["action"] == "reject"
    assert out[0]["verifier_scores"]["regen_fix"] == 0.0


def test_edit_reapplies_user_ops_to_original():
    doc, d, _ = _diagnose("suppressed_parent")
    card = CARD.build_fix_preview(doc, d, verifier_score=1.0)
    # user amends: unsuppress a specific feature by hand
    sup = [f for f in RD.detect_failures(doc) if f.kind == "suppressed_parent"][0]
    user_ops = [{"op": "unsuppress", "feature": sup.feature}]
    out = []
    result_doc = CARD.resolve(card, CARD.EDIT, out.append, edit_ops=user_ops)
    assert out[0]["action"] == "edit"
    assert out[0]["edit_op_count"] == 1
    assert RD.detect_failures(result_doc) == []      # the edited fix is authoritative + clears it


def test_action_consent_gate_strips_refs_at_meta():
    doc, d, _ = _diagnose("suppressed_parent")
    card = CARD.build_fix_preview(doc, d, verifier_score=1.0)
    out = []
    CARD.emit_action(card, CARD.ACCEPT, out.append, consent="meta", edit_ops_ref="/tmp/x.jsonl")
    assert out[0]["edit_ops_ref"] == ""              # bodies must not leave the machine at meta
    out2 = []
    CARD.emit_action(card, CARD.ACCEPT, out2.append, consent="full", edit_ops_ref="/tmp/x.jsonl")
    assert out2[0]["edit_ops_ref"] == "/tmp/x.jsonl"
