"""IP-18 regen_diagnose MCP seam: the callable backend fires on a rebuild-failure doc, returns a
fix-preview card, and degrades honestly when no provider is configured. Offline — MockProvider,
no core (structural verify)."""
import json
import random

from services.agent import regen_diagnosis as RD
from services.agent.providers.mock_provider import MockProvider
from services.mcp import regen_tools as RT
from tools.flywheel.bench.corpus import regen_corpus as RC


def _corrupt(mclass, seed=1):
    corrupted, _ = RC.mutate(RC.load_seed(), mclass, random.Random(seed))
    return {"revision": 0, "tree": corrupted, "owner_map": {}}


def _oracle_reply(document):
    ops = []
    for f in RD.detect_failures(document):
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
        elif f.kind == "bad_selector":
            ops.append({"op": "set_selector_kind", "feature": f.feature,
                        "old_kind": f.options["bad_kind"], "kind": "face"})
    return json.dumps({"diagnosis": "repair", "fix_ops": ops})


def test_seam_returns_card_and_logs_ip18():
    doc = _corrupt("suppressed_parent", seed=2)   # un-suppress rebuilds cleanly in fixture OR live
    events = []
    out = RT.diagnose_failed_document(
        doc, provider=MockProvider([_oracle_reply(doc)]),
        on_event=events.append, consent_fn=lambda: "meta")
    assert out["ok"] is True
    assert out["artifact_id"]
    card = out["card"]
    assert card["kind"] == "fix_preview_card" and card["fix_ops"]
    # verify runs with the strongest available signal (fixture w/o core, else live) — robust to env
    assert card["verifier"]["detail"]["mode"] in ("fixture", "live-rebuild-only", "live")
    assert card["verifier"]["score"] == 1.0
    # IP-18 correction trace emitted
    assert any(e.get("kind") == "ai_artifact" and e["insertion_point"] == "IP-18" for e in events)


def test_seam_degrades_without_provider():
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, provider=None)
    # _resolve_provider() yields None here (no credentials) -> honest failure + the failures list
    if out["ok"]:                                    # a local provider happened to resolve
        assert out["card"]["fix_ops"] is not None
    else:
        assert "no provider configured" in out["reason"]
        assert any(f["kind"] == "bad_selector" for f in out["failures"])


def test_tool_registration_and_json_contract():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    fn = captured["regen_diagnose"]
    doc = _corrupt("suppressed_parent", seed=4)
    # inject an oracle provider by monkeypatching the resolver for this call
    orig = RT._resolve_provider
    RT._resolve_provider = lambda: MockProvider([_oracle_reply(doc)])
    try:
        raw = fn(json.dumps(doc))
    finally:
        RT._resolve_provider = orig
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["card"]["kind"] == "fix_preview_card"


def test_tool_reports_bad_json():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    result = json.loads(captured["regen_diagnose"]("{not valid json"))
    assert result["ok"] is False and "invalid JSON" in result["error"]
