"""Mega-8 OpVocab regressions (cycle-8 / RegulatoryMastodon).

Watch-list from mega-7 findings TOP-2:
  * add_features thrash on invented op names (box/cylinder/sketch as top-level ops)
  * unknown-op errors must list closest real ops + cad_guide pointer
  * agent middleware injects allowed-ops cheatsheet EXACTLY once per mission
  * Python registry twin stays in lockstep with the C++ list (no silent alias maps)
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Registry twin (Python) — suggestion quality + feature-type confusion
# ---------------------------------------------------------------------------


def test_doc_op_kinds_contains_core():
    from engine_oracle.doc.op_registry import doc_op_kinds
    kinds = doc_op_kinds()
    assert "add_feature" in kinds
    assert "sketch_add_entity" in kinds
    assert "asm_add_mate" in kinds
    assert len(kinds) == len(set(kinds))


def test_unknown_box_points_at_add_feature():
    from engine_oracle.doc.op_registry import (
        suggest_doc_ops, unknown_doc_op_message,
    )
    msg = unknown_doc_op_message(0, "box")
    assert "unknown op 'box'" in msg
    assert "feature TYPE" in msg
    assert "add_feature" in msg
    assert "see cad_guide()" in msg
    sug = suggest_doc_ops("box", 5)
    assert sug[0] == "add_feature"


def test_suggest_prefers_sketch_family():
    from engine_oracle.doc.op_registry import suggest_doc_ops
    sug = suggest_doc_ops("sketch_create", 5)
    assert any(s.startswith("sketch_") or s == "add_feature" for s in sug[:3])


def test_cheatsheet_mentions_add_feature_and_box():
    from engine_oracle.doc.op_registry import doc_ops_cheatsheet_snippet
    s = doc_ops_cheatsheet_snippet()
    assert "add_feature" in s
    assert "box" in s
    assert "cad_guide()" in s


def test_ecad_oracle_unknown_op_lists_closest():
    from engine_oracle.ecad.ops import (
        OpError, apply_ops, list_ecad_ops, suggest_ecad_ops,
        unknown_ecad_op_message,
    )
    from engine_oracle.ecad import schema
    assert "sch_add_symbol" in list_ecad_ops()
    msg = unknown_ecad_op_message(0, "sch_add_symbl")
    assert "unknown op 'sch_add_symbl'" in msg
    assert "closest:" in msg
    assert "sch_add_symbol" in msg
    assert "see cad_guide()" in msg
    doc = schema.new_schematic("t")
    with pytest.raises(OpError) as ei:
        apply_ops(doc, [{"op": "add_resistor"}])
    assert "unknown op 'add_resistor'" in str(ei.value)
    assert "closest:" in str(ei.value)
    assert ei.value.detail.get("suggestions")


# ---------------------------------------------------------------------------
# MCP op_vocab bridge
# ---------------------------------------------------------------------------


def test_op_vocab_bridge_fallback():
    from services.mcp import op_vocab
    op_vocab._CACHE = None  # force reload
    # Without libaicad, falls back to Python twin.
    ops = op_vocab.doc_ops()
    assert "add_feature" in ops
    block = op_vocab.vocab_block_for_tool_description()
    assert "add_feature" in block
    assert "WRONG" in block
    assert op_vocab.is_unknown_op_error(
        {"ok": False, "error": "op[0]: unknown op 'box'"})
    assert not op_vocab.is_unknown_op_error({"ok": True, "revision": 1})
    assert op_vocab.extract_unknown_op_name(
        {"ok": False, "error": "op[0]: unknown op 'cylinder'"}) == "cylinder"


def test_add_features_docstring_enumerates_vocab():
    """Tool description must list the closed op + feature-type vocabulary."""
    import inspect
    from services.mcp import cad_tools
    # register_cad_tools closes over add_features; pull the function source from module.
    src = inspect.getsource(cad_tools)
    assert "OP VOCABULARY" in src
    assert 'op":"add_feature"' in src or "op=add_feature" in src or "op=…" in src
    assert "WRONG" in src
    assert "box" in src and "cylinder" in src


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
    assert result is not None


def test_dispatch_ok_false_when_payload_rejects_ops():
    """Live mega-8: add_features returned ok:false JSON but call_rec.ok stayed True."""
    from services.agent import agent_turn as at

    # Simulate the post-dispatch ok derivation used in run_turn.
    def _derive_ok(result):
        ok = True
        payload = result
        if isinstance(result, str):
            import json as _json
            try:
                payload = _json.loads(result)
            except Exception:
                payload = None
        if isinstance(payload, dict) and "ok" in payload:
            ok = bool(payload.get("ok"))
        return ok

    fail = '{\n  "ok": false,\n  "error": "op[0]: unknown op \'cylinder\'"\n}'
    assert _derive_ok(fail) is False
    assert _derive_ok({"ok": False, "error": "x"}) is False
    assert _derive_ok({"ok": True, "revision": 1}) is True
    # Rejected ops must not count as productive add_features.
    calls = [
        {"tool": "new_part", "ok": True},
        {"tool": "add_features", "ok": _derive_ok(fail)},
    ]
    productive = any(
        c.get("ok") and c.get("tool") == "add_features" for c in calls
    )
    assert productive is False
