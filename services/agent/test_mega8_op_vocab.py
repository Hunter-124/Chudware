"""Operation vocabulary and middleware regression tests."""
from __future__ import annotations

import json

import pytest


def test_ecad_oracle_unknown_op_lists_closest(tmp_path, monkeypatch):
    """ECAD op errors come from the C++ core now: ecad_list_ops RPC for the
    vocabulary, OpError (translated from the core's {code,message,detail} envelope by
    the session facade) for unknown-op suggestions."""
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from services.mcp import core_bridge as cb
    from services.mcp.ecad_common import OpError, ecad_session
    ops = cb.call("ecad_list_ops", {})["ops"]
    assert "sch_add_symbol" in ops
    sess = ecad_session.new_session("sch", "t")
    try:
        with pytest.raises(OpError) as ei:
            ecad_session.apply_ops(sess, None, [{"op": "add_resistor"}])
    finally:
        ecad_session.close_session(sess.doc_id)
    assert "unknown op 'add_resistor'" in str(ei.value)
    assert "closest:" in str(ei.value)
    assert "see cad_guide()" in str(ei.value)
    assert ei.value.detail.get("suggestions")


def test_unknown_op_detection_only_reads_errors():
    from services.mcp import op_vocab
    assert op_vocab.is_unknown_op_error({"ok": False, "error": "unknown op 'box'"})
    assert not op_vocab.is_unknown_op_error({"ok": True, "name": "unknown op"})
    assert not op_vocab.is_unknown_op_error({"error": "different error", "note": "unknown op"})
    assert op_vocab.extract_unknown_op_name({"error": "unknown op 'box'"}) == "box"


# ---------------------------------------------------------------------------
# Middleware: inject exactly once per mission
# ---------------------------------------------------------------------------


def test_middleware_injects_exactly_once():
    from services.agent import agent_turn as at

    # Isolate process flag set for this test.
    at._OP_VOCAB_INJECTED_RUNS.clear()
    run = "mega8-test-run-1"
    fail = {"ok": False, "error": "op[0]: unknown op 'box'. closest: add_feature; see cad_guide()"}

    first = at._maybe_inject_op_vocab_cheatsheet(
        "add_features", fail, run_id=run)
    assert first is not None
    assert "op vocabulary" in first.lower() or "add_feature" in first
    assert "once per mission" in first.lower() or "cad_guide" in first

    second = at._maybe_inject_op_vocab_cheatsheet(
        "add_features", fail, run_id=run)
    assert second is None  # never re-inject

    # Different mission still gets one inject.
    other = at._maybe_inject_op_vocab_cheatsheet(
        "add_features", fail, run_id="mega8-test-run-2")
    assert other is not None


def test_middleware_ignores_success_and_unrelated_tools():
    from services.agent import agent_turn as at
    at._OP_VOCAB_INJECTED_RUNS.clear()
    assert at._maybe_inject_op_vocab_cheatsheet(
        "add_features", {"ok": True, "revision": 1}, run_id="r-ok") is None
    assert at._maybe_inject_op_vocab_cheatsheet(
        "describe_part",
        {"ok": False, "error": "op[0]: unknown op 'box'"},
        run_id="r-wrong-tool",
    ) is None


def test_middleware_live_in_run_turn():
    """End-to-end: run_turn with a fake dispatcher that returns unknown-op once."""
    from services.agent import agent_turn as at
    at._OP_VOCAB_INJECTED_RUNS.clear()

    calls_seen = []

    class FakeProvider:
        def __init__(self):
            self.n = 0
            self.messages_log = []

        def complete(self, system, messages, **kwargs):
            self.messages_log.append(list(messages))
            self.n += 1
            if self.n == 1:
                return (
                    '```json\n'
                    '{"tool":"add_features","args":{"ops":[{"op":"box"}]}}\n'
                    '```'
                )
            # After seeing the inject, finish.
            return (
                '```json\n'
                '{"done": true, "handoff": {"status": "done", "summary": "ok"}}\n'
                '```'
            )

    def dispatch(tool, args):
        calls_seen.append((tool, args))
        return {"ok": False, "error": "op[0]: unknown op 'box'"}

    prov = FakeProvider()
    result = at.run_turn(
        "build a block",
        prov,
        allowed_tools=["add_features"],
        tool_dispatch=dispatch,
        max_tool_calls=4,
        run_id="mega8-e2e-run",
    )
    assert calls_seen  # tool was dispatched
    # Flatten all user message texts the model saw after the tool.
    blob = json.dumps(prov.messages_log, default=str)
    assert "unknown op" in blob or "add_feature" in blob
    # Cheatsheet once: count "op vocabulary assist" / "once per mission".
    n_assist = blob.lower().count("once per mission") + blob.lower().count(
        "op vocabulary assist")
    assert n_assist >= 1
    # Second failure would not re-inject — already covered by unit test;
    # here we only fire one unknown-op.
    calls = (result.result or {}).get("calls") or []
    rejected = [c for c in calls if c.get("tool") == "add_features"]
    assert rejected and all(c.get("ok") is False for c in rejected), calls


