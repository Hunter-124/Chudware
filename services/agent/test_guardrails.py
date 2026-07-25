"""test_guardrails.py — HARD floors WI-11..15 + WI-17 event kinds.

Mocked dispatch/ledger only (no engine, no network). Covers block / pass /
waive / kill-switch / emitted events for each guardrail.
"""
from __future__ import annotations

from services.agent import guardrails as gr
from services.agent import agent_turn


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class ListLedger:
    def __init__(self):
        self.events = []

    def append(self, actor, kind, payload):
        ev = {"actor": actor, "kind": kind, "payload": payload}
        self.events.append(ev)
        return ev


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, max_tokens=8000):
        return self._replies.pop(0) if self._replies else '{"done":true,"handoff":{}}'


def _kinds(ledger, kind):
    return [e for e in ledger.events if e["kind"] == kind]


def _hg(ledger, name=None):
    out = _kinds(ledger, "hard_guardrail")
    if name is not None:
        out = [e for e in out if e["payload"].get("name") == name]
    return out


# ===========================================================================
# WI-12 — classifier + auto-checkpoint
# ===========================================================================

def test_destructive_ops_classifier():
    assert gr.is_destructive_tool("delete_feature", {})
    assert gr.is_destructive_tool("add_features", {
        "ops": [{"op": "add_feature", "feature": {"type": "shell"}}],
    })
    assert gr.is_destructive_tool("add_features", {
        "ops": [{"feature": {"type": "extrude", "params": {"mode": "cut"}}}],
    })
    assert gr.is_destructive_tool("add_features", {
        "ops": [{"op": "boolean_cut"}],
    })
    assert gr.is_destructive_tool("add_features", {
        "ops": [{"feature": {"type": "pattern"}}],
    })
    # non-destructive
    assert not gr.is_destructive_tool("measure_part", {})
    assert not gr.is_destructive_tool("add_features", {
        "ops": [{"feature": {"type": "box", "params": {"length": 10}}}],
    })
    assert not gr.is_destructive_tool("verify_battery", {})


def test_auto_checkpoint_before_destructive_blocks_on_fail():
    ledger = ListLedger()
    calls = []

    def _ckpt(doc_id, trigger):
        calls.append((doc_id, trigger))
        return {"ok": False, "error": "disk full"}

    v = gr.pre_dispatch(
        "add_features",
        {"doc_id": "d1", "ops": [{"feature": {"type": "shell"}}]},
        ledger=ledger, actor="t",
        checkpoint_fn=_ckpt,
    )
    assert v is not None and not v.ok
    assert v.name == "auto_checkpoint"
    assert calls == [("d1", "add_features")]
    assert any(e["payload"].get("outcome") == "blocked" for e in _hg(ledger, "auto_checkpoint"))


def test_auto_checkpoint_before_destructive_passes_and_emits():
    ledger = ListLedger()
    seen = []

    def _ckpt(doc_id, trigger):
        seen.append(trigger)
        return {"ok": True, "checkpoint": "ck1"}

    v = gr.pre_dispatch(
        "delete_feature",
        {"doc_id": "d1", "id": "f9"},
        ledger=ledger, actor="t",
        checkpoint_fn=_ckpt,
    )
    assert v is None  # proceed
    assert seen == ["delete_feature"]
    ac = _kinds(ledger, "auto_checkpoint")
    assert len(ac) == 1
    assert ac[0]["payload"]["trigger_tool"] == "delete_feature"
    assert any(e["payload"]["outcome"] == "passed" for e in _hg(ledger, "auto_checkpoint"))


def test_auto_checkpoint_kill_switch():
    ledger = ListLedger()
    called = []

    def _ckpt(doc_id, trigger):
        called.append(1)
        return {"ok": True}

    v = gr.pre_dispatch(
        "delete_feature",
        {"doc_id": "d1"},
        ledger=ledger, actor="t",
        checkpoint_fn=_ckpt,
        cfg={"hard_guardrails": {"auto_checkpoint": False}},
    )
    assert v is None
    assert called == []  # never checkpointed
    assert any(e["payload"]["outcome"] == "waived" for e in _hg(ledger, "auto_checkpoint"))


def test_non_destructive_skips_checkpoint():
    ledger = ListLedger()
    called = []
    v = gr.pre_dispatch(
        "measure_part", {"doc_id": "d1"},
        ledger=ledger, checkpoint_fn=lambda *a: called.append(1) or {"ok": True},
    )
    assert v is None
    assert called == []
    assert _kinds(ledger, "auto_checkpoint") == []


# ===========================================================================
# WI-15 — kernel ref integrity
# ===========================================================================

def test_refs_resolve_blocks_dangling():
    ledger = ListLedger()
    ops = [{
        "op": "add_feature",
        "feature": {
            "type": "fillet",
            "refs": {"edges": {"id": "f_missing"}},
        },
    }]
    known = ({"f1", "s1"}, {"Box"})
    v = gr.pre_dispatch(
        "add_features",
        {"doc_id": "d1", "ops": ops},
        ledger=ledger, actor="t",
        known=known,
    )
    assert v is not None and not v.ok
    assert v.name == "refs_resolve"
    assert "f_missing" in v.message
    assert any(e["payload"]["outcome"] == "blocked" for e in _hg(ledger, "refs_resolve"))


def test_refs_resolve_passes_known_ids():
    ledger = ListLedger()
    ops = [{
        "feature": {
            "type": "fillet",
            "refs": {"edges": {"id": "f1"}},
        },
    }]
    v = gr.pre_dispatch(
        "add_features",
        {"doc_id": "d1", "ops": ops},
        ledger=ledger, known=({"f1"}, set()),
    )
    assert v is None
    assert any(e["payload"]["outcome"] == "passed" for e in _hg(ledger, "refs_resolve"))


def test_refs_resolve_ignores_geom_queries_and_pending():
    missing = gr.check_refs_resolve_ids(
        [{"feature": {"refs": {
            "face": {"id": "top"},  # plain-english
            "edge": {"id": "$0"},   # pending
            "other": {"id": "f1"},
        }}}],
        known_ids={"f1"},
    )
    assert missing == []


def test_refs_resolve_destructive_target_cannot_dodge_via_geom_word():
    """WI-15: a destructive op's target id must resolve even when it collides
    with a geom-query dictionary word — the bypass is for SELECTOR args only."""
    missing = gr.check_refs_resolve_ids(
        [{"op": "delete_feature", "id": "top"}],
        known_ids={"f1"},
    )
    assert missing == ["top"]
    for kind in ("remove_feature", "suppress_feature"):
        assert gr.check_refs_resolve_ids(
            [{"op": kind, "id": "face"}], known_ids={"f1"},
        ) == ["face"], f"{kind} target ids must not dodge WI-15 via dictionary words"


def test_refs_resolve_non_destructive_id_still_bypasses_geom_words():
    """Non-destructive ops keep the geom-query bypass for id_key=='id' (unchanged)."""
    missing = gr.check_refs_resolve_ids(
        [{"op": "add_feature", "id": "top"}],
        known_ids={"f1"},
    )
    assert missing == []


def test_refs_resolve_accepts_features_declared_earlier_in_same_batch():
    """Live trace: [add OuterBody, add InnerCut, boolean target=OuterBody] on a
    fresh doc was a HARD block on 'Missing: OuterBody' — the ref names a feature
    the SAME batch creates. This shape must apply."""
    ops = [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "OuterBody"}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "InnerCut"}},
        {"op": "boolean", "operation": "subtract",
         "target": "OuterBody", "tool": "InnerCut"},
    ]
    assert gr.check_refs_resolve_ids(ops, known_ids=set(), known_names=set()) == []
    assert gr.pre_dispatch_refs("add_features",
                                {"doc_id": "d1", "ops": ops},
                                known=(set(), set())) is None


def test_refs_resolve_still_blocks_a_ref_declared_only_later():
    """Ordering is the contract: forward references remain dangling."""
    ops = [
        {"op": "boolean", "operation": "subtract",
         "target": "OuterBody", "tool": "InnerCut"},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "OuterBody"}},
    ]
    assert gr.check_refs_resolve_ids(
        ops, known_ids=set(), known_names=set()) == ["OuterBody"]


def test_refs_resolve_still_blocks_a_name_nothing_declares():
    ops = [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "OuterBody"}},
        {"op": "boolean", "operation": "subtract", "target": "Ghost"},
    ]
    assert gr.check_refs_resolve_ids(
        ops, known_ids=set(), known_names=set()) == ["Ghost"]


def test_refs_block_message_names_what_does_resolve():
    """A bare 'Missing: X' gives the model nothing to converge on."""
    v = gr.pre_dispatch_refs(
        "add_features",
        {"doc_id": "d1", "ops": [{"op": "delete_feature", "id": "Ghost"}]},
        known=({"f1"}, {"Base"}),
    )
    assert v is not None and not v.ok
    assert "Ghost" in v.message
    assert "Base" in v.message and "f1" in v.message


def test_refs_block_message_on_empty_doc_explains_batch_local_refs():
    v = gr.pre_dispatch_refs(
        "add_features",
        {"doc_id": "d1", "ops": [{"op": "delete_feature", "id": "Ghost"}]},
        known=(set(), set()),
    )
    assert v is not None and "same ops batch" in v.message


def test_refs_no_waiver():
    """WI-15: pure dangling refs have no waiver path."""
    v = gr.pre_dispatch_refs(
        "add_features",
        {"ops": [{"feature": {"refs": {"x": {"id": "nope"}}}}]},
        known=(set(), set()),
    )
    assert v is not None and not v.ok


# ===========================================================================
# WI-11 — verify_battery before done
# ===========================================================================

def test_done_blocks_without_battery_after_mutation():
    ledger = ListLedger()
    calls = [{"tool": "add_features", "ok": True, "args": {"doc_id": "d1"},
              "result": {"ok": True}}]
    v = gr.check_done({}, ledger=ledger, calls=calls, actor="t",
                      skip={"check_interference", "orphan_sketch"})
    assert not v.ok
    assert v.name == "verify_battery"
    assert "verify_battery" in v.message
    assert any(e["payload"]["outcome"] == "blocked" for e in _hg(ledger, "verify_battery"))


def test_done_passes_with_battery_evidence():
    ledger = ListLedger()
    calls = [
        {"tool": "add_features", "ok": True, "args": {}, "result": {"ok": True}},
        {"tool": "verify_battery", "ok": True, "args": {},
         "result": {"scores": {"is_manifold": 1.0}, "findings": []}},
    ]
    v = gr.check_done({}, ledger=ledger, calls=calls, actor="t",
                      skip={"check_interference", "orphan_sketch"})
    assert v.ok and v.outcome == "passed"
    assert any(e["payload"]["outcome"] == "passed" for e in _hg(ledger, "verify_battery"))
def test_done_waives_battery_with_reason():
    ledger = ListLedger()
    calls = [{"tool": "add_features", "ok": True, "args": {}, "result": {}}]
    handoff = {"waivers": [{"check": "verify_battery", "reason": "visual only prototype"}]}
    v = gr.check_done(handoff, ledger=ledger, calls=calls, actor="t",
                      skip={"check_interference", "orphan_sketch"})
    assert v.ok  # aggregate done proceeds after waiver
    hg = _hg(ledger, "verify_battery")
    assert hg and hg[0]["payload"]["outcome"] == "waived"
    assert "visual only" in hg[0]["payload"]["reason"]

def test_done_rejects_empty_waiver_reason():
    ledger = ListLedger()
    calls = [{"tool": "add_features", "ok": True, "args": {}, "result": {}}]
    handoff = {"waivers": [{"check": "verify_battery", "reason": ""}]}
    v = gr.check_done(handoff, ledger=ledger, calls=calls, actor="t",
                      skip={"check_interference", "orphan_sketch"})
    assert not v.ok  # empty reason = no waiver


def test_done_no_mutation_skips_battery():
    ledger = ListLedger()
    calls = [{"tool": "measure_part", "ok": True, "args": {}, "result": {"mass_g": 1}}]
    v = gr.check_done({}, ledger=ledger, calls=calls, actor="t",
                      skip={"check_interference", "orphan_sketch"})
    assert v.ok


# ===========================================================================
# WI-13 — interference before assembly done
# ===========================================================================

def test_assembly_done_blocks_without_interference():
    ledger = ListLedger()
    calls = [
        {"tool": "add_features", "ok": True, "args": {}, "result": {}},
        {"tool": "verify_battery", "ok": True, "args": {},
         "result": {"scores": {}, "findings": []}},
    ]
    v = gr.check_done(
        {"doc_kind": "assembly"}, ledger=ledger, calls=calls, actor="t",
        force_assembly=True, skip={"orphan_sketch"},
    )
    assert not v.ok
    assert v.name == "check_interference"
    assert any(e["payload"]["outcome"] == "blocked"
               for e in _hg(ledger, "check_interference"))


def test_assembly_done_passes_with_interference_check():
    ledger = ListLedger()
    calls = [
        {"tool": "verify_battery", "ok": True, "args": {},
         "result": {"scores": {}, "findings": []}},
        {"tool": "check_interference", "ok": True, "args": {},
         "result": {"ok": True, "pairs": []}},
    ]
    v = gr.check_interference_before_done(
        {"doc_kind": "assembly"}, ledger=ledger, calls=calls,
        force_assembly=True, actor="t",
    )
    assert v.ok and v.outcome == "passed"


def test_assembly_done_waives_interference():
    ledger = ListLedger()
    handoff = {"waivers": [
        {"check": "check_interference", "reason": "intentional press-fit overlap"},
    ]}
    v = gr.check_interference_before_done(
        handoff, ledger=ledger, force_assembly=True, actor="t",
    )
    assert v.ok and v.outcome == "waived"


def test_part_only_skips_interference():
    ledger = ListLedger()
    v = gr.check_interference_before_done(
        {}, ledger=ledger, force_assembly=False, actor="t",
    )
    assert v.ok and "not assembly" in v.reason


# ===========================================================================
# WI-14 — no orphan sketch at done
# ===========================================================================

def test_orphan_sketch_blocks():
    ledger = ListLedger()
    tree = {
        "features": [
            {"id": "s1", "name": "Profile", "type": "sketch"},
            {"id": "e1", "name": "Boss", "type": "extrude",
             "refs": {"profile": "s_other"}},  # does not consume s1
        ],
    }
    v = gr.check_no_orphan_sketch_at_done(
        {}, ledger=ledger, tree=tree, actor="t",
    )
    assert not v.ok
    assert "s1" in v.message
    assert any(e["payload"]["outcome"] == "blocked" for e in _hg(ledger, "orphan_sketch"))


def test_orphan_sketch_cleared_when_consumed():
    ledger = ListLedger()
    tree = {
        "features": [
            {"id": "s1", "name": "Profile", "type": "sketch"},
            {"id": "e1", "type": "extrude", "refs": {"profile_sketch": "s1"}},
        ],
    }
    v = gr.check_no_orphan_sketch_at_done({}, ledger=ledger, tree=tree, actor="t")
    assert v.ok


def test_orphan_sketch_waived_per_id():
    ledger = ListLedger()
    handoff = {"waivers": [
        {"check": "orphan_sketch:s1", "reason": "reference sketch for drawing"},
    ]}
    v = gr.check_no_orphan_sketch_at_done(
        handoff, ledger=ledger, orphan_ids=["s1"], actor="t",
    )
    assert v.ok and v.outcome == "waived"


def test_orphan_sketch_blanket_waiver():
    ledger = ListLedger()
    handoff = {"waivers": [
        {"check": "orphan_sketch", "reason": "all construction sketches kept"},
    ]}
    v = gr.check_no_orphan_sketch_at_done(
        handoff, ledger=ledger, orphan_ids=["s1", "s2"], actor="t",
    )
    assert v.ok and v.outcome == "waived"


# ===========================================================================
# WI-11 mission gate
# ===========================================================================

def test_gate_verify_missing_battery_fails():
    from services.agent.pipeline.phases import PhaseMachine, Phase

    m = PhaseMachine()
    ctx = {
        "criterion_results": {"AC1": "PASS"},
        "geometry_built": True,
        "require_battery": True,
    }
    g = m.run_gate(Phase.VERIFY, ctx)
    assert not g.passed
    assert any("verify_battery" in r for r in g.reasons)


def test_gate_verify_with_battery_ran_passes():
    from services.agent.pipeline.phases import PhaseMachine, Phase

    m = PhaseMachine()
    ctx = {
        "criterion_results": {"AC1": "PASS"},
        "geometry_built": True,
        "battery_ran": True,
    }
    g = m.run_gate(Phase.VERIFY, ctx)
    assert g.passed


def test_gate_verify_battery_waived_passes():
    from services.agent.pipeline.phases import PhaseMachine, Phase

    m = PhaseMachine()
    ctx = {
        "criterion_results": {"AC1": "PASS"},
        "geometry_built": True,
        "waived_criteria": ["verify_battery"],
    }
    g = m.run_gate(Phase.VERIFY, ctx)
    assert g.passed


def test_gate_verify_still_fails_on_ac_fail():
    from services.agent.pipeline.phases import PhaseMachine, Phase

    m = PhaseMachine()
    ctx = {"criterion_results": {"AC1": "FAIL"}, "battery_ran": True}
    g = m.run_gate(Phase.VERIFY, ctx)
    assert not g.passed
    assert "AC1" in g.reasons[0]


# ===========================================================================
# agent_turn integration (mocked dispatch)
# ===========================================================================

def test_agent_turn_blocks_done_without_battery_after_mutation():
    provider = ScriptedProvider([
        '```json\n{"tool":"add_features","args":{"doc_id":"d1","ops":[{"feature":{"type":"box"}}]}}\n```',
        '```json\n{"done":true,"handoff":{"summary":"built"}}\n```',
        # after block, model waives
        '```json\n{"done":true,"handoff":{"waivers":[{"check":"verify_battery","reason":"demo"}]}}\n```',
    ])
    ledger = ListLedger()
    dispatched = []

    def _dispatch(tool, args):
        dispatched.append(tool)
        return {"ok": True}

    res = agent_turn.run_turn(
        "build a box", provider,
        allowed_tools=["add_features", "verify_battery"],
        tool_dispatch=_dispatch, ledger=ledger, actor="feature_modeler",
        max_tool_calls=5,
    )
    assert res.success is True
    assert res.stop_reason == "confirmed"
    # first done was rejected; second (waived) accepted
    assert any(e["payload"].get("outcome") == "blocked"
               for e in _hg(ledger, "verify_battery"))
    assert any(e["payload"].get("outcome") == "waived"
               for e in _hg(ledger, "verify_battery"))


def test_agent_turn_blocks_destructive_when_checkpoint_fails():
    provider = ScriptedProvider([
        '```json\n{"tool":"delete_feature","args":{"doc_id":"d1","id":"f1"}}\n```',
        '```json\n{"done":true,"handoff":{}}\n```',
    ])
    ledger = ListLedger()

    def _dispatch(tool, args):
        raise AssertionError("dispatch must not run when checkpoint fails")

    import services.agent.agent_turn as at
    orig = at._pre_dispatch_guardrail

    def _wrapped(tool, args, ledger, actor):
        try:
            verdict = gr.pre_dispatch(
                tool, args or {}, ledger=ledger, actor=actor or "agent_turn",
                checkpoint_fn=lambda d, t: {"ok": False, "error": "ckpt boom"},
            )
            if verdict is None or verdict.ok:
                return None
            return verdict.message or verdict.reason
        except Exception:
            return None

    at._pre_dispatch_guardrail = _wrapped
    try:
        res = agent_turn.run_turn(
            "delete it", provider,
            allowed_tools=["delete_feature"],
            tool_dispatch=_dispatch, ledger=ledger, actor="t",
        )
    finally:
        at._pre_dispatch_guardrail = orig

    assert res.result["calls"]
    assert res.result["calls"][0]["ok"] is False
    assert "ckpt boom" in str(res.result["calls"][0]["result"])


def test_agent_turn_measure_only_done_still_works():
    """Existing canary path: measure then done with no mutation must not block."""
    provider = ScriptedProvider([
        '```json\n{"tool":"measure_part","args":{"doc_id":"d1"}}\n```',
        '```json\n{"done":true,"handoff":{"summary":"measured"}}\n```',
    ])
    res = agent_turn.run_turn(
        "measure", provider,
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"mass_g": 1.0},
    )
    assert res.success is True
    assert res.stop_reason == "confirmed"


# ===========================================================================
# Event shape contract (WI-17)
# ===========================================================================

def test_hard_guardrail_event_shape():
    ledger = ListLedger()
    gr.emit_hard_guardrail(ledger, "verify_battery", "blocked", reason="missing")
    gr.emit_auto_checkpoint(ledger, "delete_feature")
    hg = ledger.events[0]
    assert hg["kind"] == "hard_guardrail"
    assert set(hg["payload"]) >= {"name", "outcome", "reason"}
    assert hg["payload"]["outcome"] in ("blocked", "passed", "waived")
    ac = ledger.events[1]
    assert ac["kind"] == "auto_checkpoint"
    assert "trigger_tool" in ac["payload"]


def test_destructive_ops_frozenset_exported():
    assert isinstance(gr.DESTRUCTIVE_OPS, frozenset)
    assert "shell" in gr.DESTRUCTIVE_OPS
    assert "boolean_cut" in gr.DESTRUCTIVE_OPS or "cut" in gr.DESTRUCTIVE_OPS
