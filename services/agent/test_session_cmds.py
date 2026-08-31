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

    def complete(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "messages": copy.deepcopy(messages),
                "max_tokens": max_tokens,
            }
        )
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
        {
            "role": "user",
            "content": [{"type": "text", "text": "Also add mounting holes"}],
        },
    ]


class TestHandoffDocument(unittest.TestCase):
    def test_structure_and_no_mutation(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        msgs_before = copy.deepcopy(msgs)
        prov = _MockProvider(_SAMPLE_HANDOFF)
        doc = sc.handoff_document(
            msgs, focus="RPC shapes", provider=prov, model="mock-1"
        )
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
            if m.get("content")
            and isinstance(m["content"], list)
            and m["content"]
            and m["content"][0].get("type") == "text"
        ]
        self.assertTrue(any("side-channel" in t or "NOT available" in t for t in texts))

    def test_empty_question(self) -> None:
        from services.agent import session_cmds as sc

        prov = _MockProvider("should not be called")
        answer = sc.btw_answer(_history(), "  ", provider=prov)
        self.assertEqual(answer, "")
        self.assertEqual(prov.calls, [])


class _FakeChudcodeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.result: dict = {"ok": True, "compacted": True}

    def session_call(
        self, session_id: str, command: str, params: dict | None = None
    ) -> dict:
        self.calls.append((session_id, command, dict(params or {})))
        return dict(self.result)


class TestCompactShakeDelegation(unittest.TestCase):
    def test_compact_off_passthrough(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        out = sc.compact_session(msgs, strategy="off")
        self.assertEqual(out["kept_messages"], msgs)
        self.assertIsNot(out["kept_messages"], msgs)

    def test_online_compact_uses_live_chudcode_once(self) -> None:
        from services.agent import chudcode_host
        from services.agent import session_cmds as sc

        host = _FakeChudcodeHost()
        messages = _history()
        before = copy.deepcopy(messages)
        with (
            patch.object(
                chudcode_host,
                "get_default_chudcode_host",
                return_value=host,
                create=True,
            ),
            patch.object(
                sc.importlib,
                "import_module",
                side_effect=AssertionError("online compact invoked local compaction"),
            ),
        ):
            out = sc.compact_session(
                messages,
                strategy="snapcompact",
                session_id="session-7",
                instructions="preserve CAD decisions",
            )

        self.assertEqual(out, {"ok": True, "compacted": True})
        self.assertEqual(
            host.calls,
            [
                (
                    "session-7",
                    "compact",
                    {"customInstructions": "preserve CAD decisions"},
                )
            ],
        )
        self.assertEqual(messages, before)

    def test_online_compact_without_live_session_is_honest(self) -> None:
        from services.agent import session_cmds as sc

        with self.assertRaisesRegex(
            sc.SessionCommandUnavailable, "requires a live Chudcode session_id"
        ):
            sc.compact_session(_history(), strategy="summary")

    def test_online_compact_propagates_unavailable_result(self) -> None:
        from services.agent import chudcode_host
        from services.agent import session_cmds as sc

        host = _FakeChudcodeHost()
        host.result = {"ok": False, "reason": "session not live"}
        with patch.object(
            chudcode_host,
            "get_default_chudcode_host",
            return_value=host,
            create=True,
        ):
            with self.assertRaisesRegex(
                sc.SessionCommandUnavailable, "session not live"
            ):
                sc.compact_session(_history(), strategy="summary", session_id="closed")

    def test_compaction_and_retry_toggles_use_chudcode(self) -> None:
        from services.agent import chudcode_host
        from services.agent import session_cmds as sc

        host = _FakeChudcodeHost()
        with patch.object(
            chudcode_host,
            "get_default_chudcode_host",
            return_value=host,
            create=True,
        ):
            sc.set_auto_compaction("s1", False)
            sc.set_auto_retry("s1", True)
        self.assertEqual(
            host.calls,
            [
                ("s1", "set_auto_compaction", {"enabled": False}),
                ("s1", "set_auto_retry", {"enabled": True}),
            ],
        )

    def test_standalone_compaction_is_local_and_never_calls_host(self) -> None:
        from services.agent import session_cmds as sc

        fake_result = {
            "archive": {
                "text": "old",
                "text_head": "old",
                "text_tail": "",
                "truncated_chars": 0,
                "total_chars": 3,
            },
            "summary": "sum",
            "short_summary": "short",
            "kept_messages": [],
            "cut_index": 2,
        }
        fake_mod = types.ModuleType("services.agent.compaction")
        fake_mod.snapcompact_archive = lambda *args, **kwargs: fake_result
        fake_mod.reinject_archive = lambda result: [
            {"role": "user", "content": "archive"}
        ]
        fake_mod.shake_messages = lambda messages, **kwargs: {
            "messages": messages,
            "saved_tokens": 0,
            "regions": [],
            "placeholders": 0,
        }
        with patch.dict(sys.modules, {"services.agent.compaction": fake_mod}):
            out = sc.compact_session(
                _history(), strategy="snapcompact", standalone=True
            )
        self.assertEqual(out["summary"], "sum")
        self.assertEqual(out["messages"][0]["content"], "archive")

    def test_online_shake_refuses_second_history_authority(self) -> None:
        from services.agent import session_cmds as sc

        with self.assertRaisesRegex(
            sc.SessionCommandUnavailable, "unavailable for online Chudcode"
        ):
            sc.shake_session(_history())

    def test_standalone_shake_never_mutates_input(self) -> None:
        from services.agent import session_cmds as sc

        messages = _history()
        before = copy.deepcopy(messages)
        out = sc.shake_session(messages, aggressive=True, standalone=True)
        self.assertEqual(messages, before)
        self.assertIn("notice", out)


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
            rpc._cmds(),
            "handoff_document",
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
            rpc._cmds(),
            "btw_answer",
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


if __name__ == "__main__":
    unittest.main()
