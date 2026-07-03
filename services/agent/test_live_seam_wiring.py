"""T-BL22 — the dormant seams are LIVE: adapter routing tags real loop responses, and a
rebuild failure produces a diagnosis event + a recordable accept/edit/reject trace.

End-to-end per the DoD:
  1. a ROUTED live call (real loop.run_turn, MockProvider) carries the adapter tag on its
     ai_artifact event — and the unrouted path still emits adapter=None (fallback preserved);
  2. a corrupt doc's rebuild failure produces an IP-18 diagnosis event and the resolve
     step emits the correlated ai_artifact_action trace.

Offline — MockProvider, no core, no network.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from services.agent import loop
from services.agent.providers.mock_provider import MockProvider
from services.serving.routing import RoutingError, load_routing_table

# A minimal script + done verdict for the mocked design loop.
_SCRIPT_REPLY = (
    "```python\nresult = Box(10, 10, 10)\n```\n"
    '```json\n{"done": false, "notes": "first pass"}\n```'
)
_DONE_REPLY = '```json\n{"done": true, "notes": "matches"}\n```'


def _routing_cfg(adapter="IP-1/v3", pct=100.0):
    """A config dict with an IP-1 route entry serving *adapter* at *pct*% canary
    (RouteEntry.from_dict schema: nested canary {pct, salt, opt_in})."""
    return {
        "routing": {
            "IP-1": {
                "provider": "vllm",
                "model": "base-model",
                "adapter_version": adapter,
                "canary": {"pct": pct, "salt": "t-bl22", "opt_in": []},
                "fallback": {"mode": "frontier", "provider": "anthropic", "model": ""},
            }
        }
    }


@dataclass
class _FakeBuild:
    """Minimal run_code_multi result the loop accepts (ok build, no render)."""
    ok: bool = True
    stl_path: str | None = None      # None -> the loop skips the render branch
    error: str | None = None
    stdout: str = ""
    volume: float = 1000.0
    triangles: int = 12
    bbox: dict = field(default_factory=lambda: {"size": [10.0, 10.0, 10.0]})
    is_assembly: bool = False
    parts: list | None = None
    fit: dict | None = None


def _run_loop(provider, monkeypatch, tmp_path):
    """Run one REAL loop turn with the kernel + output dir mocked out."""
    monkeypatch.setattr(loop, "run_code_multi", lambda code, out_dir, name: _FakeBuild())
    monkeypatch.setattr(loop.paths, "model_dir", lambda name: tmp_path)
    events: list[dict] = []
    run = loop.run_turn(loop.Session(name="t-bl22"), "a 10mm cube", provider,
                        on_event=events.append, max_iters=3)
    return run, events


# --------------------------------------------------------------------- DoD test 1: adapter tag

def test_routed_live_call_carries_adapter_tag(monkeypatch, tmp_path):
    """The worker-side wiring: resolve IP-1 through the table, stamp the decision on the
    provider (what engine/worker._agent_design does), run the REAL loop — every ai_artifact
    event carries the adapter tag from RouteDecision.telemetry_tag()."""
    decision = load_routing_table(_routing_cfg()).resolve("IP-1", user_id="erik")
    assert not decision.is_fallback and decision.adapter == "IP-1/v3"

    provider = MockProvider([_SCRIPT_REPLY, _DONE_REPLY])
    provider._routing_decision = decision

    _run, events = _run_loop(provider, monkeypatch, tmp_path)
    arts = [e for e in events if e.get("kind") == "ai_artifact"]
    assert arts, "the live loop must emit ai_artifact telemetry"
    for e in arts:
        assert e["model"]["adapter"] == "IP-1/v3", "routed response must carry the adapter tag"
        assert e["model"]["provider"] == "vllm"
        assert e["insertion_point"] == "IP-1"


def test_unrouted_loop_keeps_fallback_shape(monkeypatch, tmp_path):
    """No routing decision stamped -> the loop behaves exactly as before: provider's own
    name/model, adapter None (the DoD's 'fallback path preserved when no table configured')."""
    provider = MockProvider([_SCRIPT_REPLY, _DONE_REPLY])
    _run, events = _run_loop(provider, monkeypatch, tmp_path)
    arts = [e for e in events if e.get("kind") == "ai_artifact"]
    assert arts
    for e in arts:
        assert e["model"]["adapter"] is None
        assert e["model"]["provider"] == "mock"


def test_no_table_resolve_raises_and_excluded_traffic_untagged():
    """No IP-1 entry -> resolve raises RoutingError (the worker wiring swallows it,
    provider unchanged). 0% canary -> everyone gets the fallback, tag adapter None."""
    import pytest

    with pytest.raises(RoutingError):
        load_routing_table({}).resolve("IP-1", user_id="x")

    d = load_routing_table(_routing_cfg(pct=0.0)).resolve("IP-1", user_id="erik")
    assert d.is_fallback and d.telemetry_tag()["adapter"] is None


# --------------------------------------------------- DoD test 2: rebuild failure -> diagnosis

def _corrupt_doc(mclass="dangling_ref", seed=7):
    from tools.flywheel.bench.corpus import regen_corpus as RC
    corrupted, _ = RC.mutate(RC.load_seed(), mclass, random.Random(seed))
    return {"revision": 0, "tree": corrupted, "owner_map": {}}


def _oracle_reply(doc):
    from services.agent import regen_diagnosis as RD
    ops = []
    for f in RD.detect_failures(doc):
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
        elif f.kind == "bad_selector":
            ops.append({"op": "set_selector_kind", "feature": f.feature,
                        "old_kind": f.options["bad_kind"], "kind": "face"})
    return json.dumps({"diagnosis": "repair", "fix_ops": ops})


def test_rebuild_failure_produces_diagnosis_event_and_action_trace():
    """End-to-end: corrupt doc -> diagnose_failed_document (the add_features binding) emits
    the IP-18 ai_artifact diagnosis event; resolve() then emits the correlated accept
    action trace carrying the verifier score (the T-H1 telemetry envelope)."""
    from services.agent import regen_card
    from services.mcp import regen_tools as RT

    doc = _corrupt_doc("suppressed_parent", seed=2)
    events: list[dict] = []
    out = RT.diagnose_failed_document(
        doc, provider=MockProvider([_oracle_reply(doc)]),
        on_event=events.append, consent_fn=lambda: "meta")
    assert out["ok"] is True

    diag = [e for e in events
            if e.get("kind") == "ai_artifact" and e.get("insertion_point") == "IP-18"]
    assert diag, "rebuild failure must produce an IP-18 diagnosis event"
    assert diag[0]["artifact_id"] == out["artifact_id"]

    # accept -> ai_artifact_action correlated to the SAME artifact, verifier score aboard
    card = out["card"]
    fixed = regen_card.resolve(card, "accept", events.append, consent="meta")
    assert fixed is not None, "accept must return the fixed document"
    actions = [e for e in events if e.get("kind") == "ai_artifact_action"]
    assert actions and actions[0]["action"] == "accept"
    assert actions[0]["artifact_id"] == out["artifact_id"]
    assert actions[0]["verifier_scores"]["regen_fix"] == card["verifier"]["score"]


def test_mcp_default_sink_journals_ip18_trace(monkeypatch, tmp_path):
    """The MCP tool path (no caller sink): the IP-18 trace is journaled to
    CACHE/telemetry/events.jsonl instead of dropped (T-BL22 journal-sink default)."""
    from engine import paths as engine_paths
    from services.mcp import regen_tools as RT

    monkeypatch.setattr(engine_paths, "CACHE", tmp_path)
    doc = _corrupt_doc("dangling_ref", seed=3)
    out = RT.diagnose_failed_document(doc, provider=MockProvider([_oracle_reply(doc)]))
    assert out["ok"] is True

    journal = tmp_path / "telemetry" / "events.jsonl"
    assert journal.exists(), "IP-18 trace must be journaled when no sink is supplied"
    kinds = [json.loads(ln).get("kind") for ln in journal.read_text().splitlines()]
    assert "ai_artifact" in kinds


def test_regen_resolve_tool_records_action(monkeypatch, tmp_path):
    """The registered regen_resolve MCP tool: reject on a diagnosed card journals the
    correlated action trace and returns document: null."""
    from engine import paths as engine_paths
    from services.mcp import regen_tools as RT

    monkeypatch.setattr(engine_paths, "CACHE", tmp_path)
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured and "regen_resolve" in captured

    doc = _corrupt_doc("bad_selector", seed=4)
    orig = RT._resolve_provider
    RT._resolve_provider = lambda: MockProvider([_oracle_reply(doc)])
    try:
        diag = json.loads(captured["regen_diagnose"](json.dumps(doc)))
    finally:
        RT._resolve_provider = orig
    assert diag["ok"] is True

    res = json.loads(captured["regen_resolve"](json.dumps(diag["card"]), "reject"))
    assert res["ok"] is True and res["action"] == "reject" and res["document"] is None

    lines = [json.loads(ln) for ln in
             (tmp_path / "telemetry" / "events.jsonl").read_text().splitlines()]
    actions = [e for e in lines if e.get("kind") == "ai_artifact_action"]
    assert actions and actions[-1]["action"] == "reject"
    assert actions[-1]["artifact_id"] == diag["artifact_id"]
