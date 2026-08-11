"""Offline tests for session_cmds + session_rpc (batch 2b).

Mock providers only — no live network. Compaction engines are mocked so this
suite is independent of PyCompaction's file landing order.
"""
from __future__ import annotations

import copy
import sys
import types
import unittest
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

class _MockProvider:
    name = "mock"

    def __init__(self, reply: str = "mock reply"):
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self.calls.append({
            "system": system,
            "messages": copy.deepcopy(messages),
            "max_tokens": max_tokens,
        })
        return self.reply


# ---------------------------------------------------------------------------
# Sample handoff document (structure contract)
# ---------------------------------------------------------------------------

_SAMPLE_HANDOFF = """\
## Goal
Implement session command RPCs for /handoff and /btw.

## Constraints & Preferences
- Never mutate host transcript
- Mock providers only in tests

## Progress
### Done
- [x] session_cmds skeleton

### In Progress
- [ ] RPC registration

### Pending
- [ ] Offline tests

## Key Decisions
- **Thin wrappers**: compact/shake delegate to compaction.py

## Critical Context
- services/agent/session_cmds.py
- services/agent/worker_rpc.py (folded from engine_oracle/parts_hub/session_rpc.py in T-RF19)

## Next Steps
1. Register SESSION_METHODS in worker_rpc.py
2. Run offline tests
"""


def _history() -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "Build the bracket"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "I'll sketch a base plate first."}],
        },
        {"role": "user", "content": [{"type": "text", "text": "Also add mounting holes"}]},
    ]


class TestHandoffDocument(unittest.TestCase):
    def test_structure_and_no_mutation(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        msgs_before = copy.deepcopy(msgs)
        prov = _MockProvider(_SAMPLE_HANDOFF)
        doc = sc.handoff_document(msgs, focus="RPC shapes", provider=prov, model="mock-1")
        self.assertEqual(msgs, msgs_before, "handoff must not mutate input messages")
        self.assertIn("## Goal", doc)
        self.assertIn("## Constraints & Preferences", doc)
        self.assertIn("## Progress", doc)
        self.assertIn("### Done", doc)
        self.assertIn("### In Progress", doc)
        self.assertIn("### Pending", doc)
        self.assertIn("## Key Decisions", doc)
        self.assertIn("## Critical Context", doc)
        self.assertIn("## Next Steps", doc)
        self.assertEqual(len(prov.calls), 1)
        # Prompt must request the structure and carry focus
        last_user = prov.calls[0]["messages"][-1]
        last_text = last_user["content"][0]["text"]
        self.assertIn("## Goal", last_text)
        self.assertIn("Additional focus: RPC shapes", last_text)
        # History preserved in prompt (snapshot, not same object)
        self.assertGreaterEqual(len(prov.calls[0]["messages"]), len(msgs) + 1)


class TestBtwAnswer(unittest.TestCase):
    def test_no_history_mutation(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        msgs_before = copy.deepcopy(msgs)
        prov = _MockProvider("The mounting holes should be M5 on a 40 mm pitch.")
        answer = sc.btw_answer(msgs, "What hole size?", provider=prov, model="mock-1")
        self.assertEqual(msgs, msgs_before)
        self.assertIn("M5", answer)
        self.assertEqual(len(prov.calls), 1)
        # Last user message is the <btw> wrapper
        last = prov.calls[0]["messages"][-1]["content"][0]["text"]
        self.assertIn("<btw>", last)
        self.assertIn("What hole size?", last)
        self.assertIn("NEVER use tools", last)
        # No-tools reminder is present earlier
        texts = [
            m["content"][0]["text"]
            for m in prov.calls[0]["messages"]
            if m.get("content") and isinstance(m["content"], list)
            and m["content"] and m["content"][0].get("type") == "text"
        ]
        self.assertTrue(any("side-channel" in t or "NOT available" in t for t in texts))

    def test_empty_question(self) -> None:
        from services.agent import session_cmds as sc

        prov = _MockProvider("should not be called")
        answer = sc.btw_answer(_history(), "  ", provider=prov)
        self.assertEqual(answer, "")
        self.assertEqual(prov.calls, [])


class TestCompactShakeDelegation(unittest.TestCase):
    def test_compact_off_passthrough(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        out = sc.compact_session(msgs, strategy="off")
        self.assertEqual(out["kept_messages"], msgs)
        self.assertEqual(out["summary"], "")
        self.assertEqual(out["short_summary"], "")
        # Deep copy, not same list object
        self.assertIsNot(out["kept_messages"], msgs)

    def test_compact_snapcompact_shape(self) -> None:
        from services.agent import session_cmds as sc

        fake_archive = {
            "text": "¶user Build…",
            "text_head": "¶user Build…",
            "text_tail": "holes",
            "truncated_chars": 12,
            "total_chars": 80,
        }
        fake_result = {
            "archive": fake_archive,
            "summary": "Built bracket base; holes pending.",
            "short_summary": "Bracket + holes",
            "kept_messages": [{"role": "user", "content": [{"type": "text", "text": "holes"}]}],
            "cut_index": 2,
        }
        fake_mod = types.ModuleType("services.agent.compaction")
        fake_mod.snapcompact_archive = lambda *a, **k: fake_result
        fake_mod.shake_messages = lambda *a, **k: {
            "messages": [], "saved_tokens": 0, "regions": [], "placeholders": 0,
        }
        with patch.dict(sys.modules, {
            "services.agent.compaction": fake_mod,
        }):
            # Force re-import path: compact_session does lazy import each call
            out = sc.compact_session(
                _history(),
                strategy="snapcompact",
                keep_recent_tokens=5000,
                supersede_reads=True,
                drop_useless=True,
            )
        self.assertEqual(out["summary"], "Built bracket base; holes pending.")
        self.assertEqual(out["short_summary"], "Bracket + holes")
        self.assertEqual(out["archive"], fake_archive)
        self.assertEqual(len(out["kept_messages"]), 1)
        self.assertEqual(out["cut_index"], 2)

    def test_compact_summary_uses_snapcompact_engine(self) -> None:
        """summary strategy delegates to snapcompact_archive (text engines only)."""
        from services.agent import session_cmds as sc

        called = {}

        def _snap(*a, **k):
            called["kwargs"] = k
            return {
                "archive": {"text": "x", "text_head": "x", "text_tail": "x",
                            "truncated_chars": 0, "total_chars": 1},
                "summary": "sum",
                "short_summary": "ss",
                "kept_messages": [],
                "cut_index": 0,
            }

        fake_mod = types.ModuleType("services.agent.compaction")
        fake_mod.snapcompact_archive = _snap
        fake_mod.shake_messages = lambda *a, **k: {
            "messages": [], "saved_tokens": 0, "regions": [], "placeholders": 0,
        }
        with patch.dict(sys.modules, {
            "services.agent.compaction": fake_mod,
        }):
            out = sc.compact_session(_history(), strategy="summary")
        self.assertEqual(out["summary"], "sum")
        self.assertIn("kwargs", called)

    def test_shake_session_shape(self) -> None:
        from services.agent import session_cmds as sc

        shaken = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "tool",
                "content": [{
                    "type": "text",
                    "text": "[shaken ~120 tokens — recover: artifact://abc (region 0)]",
                }],
            },
        ]
        fake_mod = types.ModuleType("services.agent.compaction")
        fake_mod.shake_messages = lambda messages, **k: {
            "messages": shaken,
            "saved_tokens": 120,
            "regions": [{"k": 0}],
            "placeholders": 1,
        }
        fake_mod.snapcompact_archive = lambda *a, **k: {
            "archive": {}, "summary": "", "short_summary": "",
            "kept_messages": messages if False else [], "cut_index": 0,
        }
        with patch.dict(sys.modules, {
            "services.agent.compaction": fake_mod,
        }):
            out = sc.shake_session(_history(), mode="elide", aggressive=True)
        self.assertEqual(out["messages"], shaken)
        self.assertEqual(out["saved_tokens"], 120)
        self.assertIn("120", out["notice"])
        self.assertEqual(out["placeholders"], 1)


class TestSessionRpc(unittest.TestCase):
    def test_methods_registered(self) -> None:
        from services.agent.worker_rpc import SESSION_METHODS

        for name in (
            "session_compact",
            "session_shake",
            "session_handoff",
            "session_btw",
        ):
            self.assertIn(name, SESSION_METHODS)
            self.assertTrue(callable(SESSION_METHODS[name]))

    def test_rpc_handoff_and_btw_shapes(self) -> None:
        from services.agent import worker_rpc as rpc

        prov = _MockProvider(_SAMPLE_HANDOFF)
        # Patch get_provider path by injecting provider object via params
        # session_cmds accepts provider instance directly.
        with patch.object(
            rpc._cmds(), "handoff_document",
            return_value=_SAMPLE_HANDOFF,
        ) as _hd:
            r = rpc.SESSION_METHODS["session_handoff"](
                {
                    "messages": _history(),
                    "focus": "tests",
                    "provider": "mock",
                    "model": "m",
                },
                lambda *a, **k: None,
            )
        self.assertTrue(r.get("ok"))
        self.assertIn("document", r)
        self.assertIn("## Goal", r["document"])

        with patch.object(
            rpc._cmds(), "btw_answer",
            return_value="brief answer",
        ):
            r2 = rpc.SESSION_METHODS["session_btw"](
                {
                    "messages": _history(),
                    "question": "status?",
                    "provider": "mock",
                    "model": "m",
                },
                lambda *a, **k: None,
            )
        self.assertTrue(r2.get("ok"))
        self.assertEqual(r2["answer"], "brief answer")

    def test_rpc_btw_requires_question(self) -> None:
        from services.agent import worker_rpc as rpc

        r = rpc.SESSION_METHODS["session_btw"](
            {"messages": _history(), "provider": "mock", "model": "m"},
            lambda *a, **k: None,
        )
        self.assertFalse(r.get("ok"))
        self.assertIn("question", r.get("reason", "").lower())

    def test_rpc_compact_off(self) -> None:
        from services.agent import worker_rpc as rpc

        r = rpc.SESSION_METHODS["session_compact"](
            {"messages": _history(), "strategy": "off"},
            lambda *a, **k: None,
        )
        self.assertTrue(r.get("ok"))
        self.assertIn("kept_messages", r)
        self.assertEqual(r["summary"], "")
        self.assertEqual(len(r["kept_messages"]), 3)

    def test_rpc_shake_shape(self) -> None:
        from services.agent import worker_rpc as rpc

        fake = {
            "messages": _history(),
            "saved_tokens": 0,
            "notice": "Shake found nothing to elide.",
            "regions": [],
            "placeholders": 0,
        }
        with patch.object(rpc._cmds(), "shake_session", return_value=fake):
            r = rpc.SESSION_METHODS["session_shake"](
                {"messages": _history(), "mode": "elide", "aggressive": False},
                lambda *a, **k: None,
            )
        self.assertTrue(r.get("ok"))
        self.assertIn("messages", r)
        self.assertIn("saved_tokens", r)
        self.assertIn("notice", r)


if __name__ == "__main__":
    unittest.main()
