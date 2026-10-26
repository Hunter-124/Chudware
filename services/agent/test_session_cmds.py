"""Focused Chudcode session-command and explicit-offline coverage.

No live network: Chudcode hosts and role turns are faked, and local compaction
engines are isolated from optional dependency landing order.
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
- services/agent/worker_rpc.py (folded from engine_oracle/parts_hub/session_rpc.py)

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
    def test_online_handoff_uses_live_session_and_does_not_mutate(self) -> None:
        from services.agent import chudcode_host
        from services.agent import session_cmds as sc

        msgs = _history()
        before = copy.deepcopy(msgs)
        host = _FakeChudcodeHost()
        host.result = {"ok": True, "document": _SAMPLE_HANDOFF}
        with patch.object(
            chudcode_host, "get_default_chudcode_host", return_value=host
        ):
            doc = sc.handoff_document(
                msgs, focus="RPC shapes", session_id="conversation-7"
            )
        self.assertEqual(msgs, before)
        self.assertIn("## Goal", doc)
        self.assertEqual(
            host.calls,
            [
                (
                    "conversation-7",
                    "handoff",
                    {"customInstructions": "RPC shapes"},
                )
            ],
        )

    def test_explicit_offline_handoff_uses_bounded_safe_role_turn(self) -> None:
        from services.agent import session_cmds as sc

        msgs = _history()
        before = copy.deepcopy(msgs)
        with patch.object(
            sc, "_run_one_shot_role", return_value=_SAMPLE_HANDOFF
        ) as run:
            doc = sc.handoff_document(
                msgs,
                focus="offline state",
                provider=_MockProvider(_SAMPLE_HANDOFF),
                standalone=True,
            )
        self.assertEqual(msgs, before)
        self.assertIn("## Goal", doc)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["command"], "handoff")
        self.assertTrue(kwargs["standalone"])
        self.assertIn("Additional focus: offline state", kwargs["request"])

    def test_online_handoff_requires_session_id(self) -> None:
        from services.agent import session_cmds as sc

        with patch.object(sc, "_offline_enabled", return_value=False):
            with self.assertRaises(sc.SessionCommandUnavailable):
                sc.handoff_document(_history())


class TestBtwAnswer(unittest.TestCase):
    def test_bounded_one_shot_role_turn_and_no_history_mutation(self) -> None:
        from services.agent import chudcode_roles
        from services.agent import session_cmds as sc

        msgs = _history()
        before = copy.deepcopy(msgs)
        seen: list[dict[str, Any]] = []

        def fake_turn(request, role, **kwargs):
            seen.append({"request": request, "role": role, **kwargs})
            return {
                "success": True,
                "stop_reason": "completed",
                "result": "The mounting holes should be M5.",
            }

        with patch.object(chudcode_roles, "run_role_turn", side_effect=fake_turn):
            answer = sc.btw_answer(
                msgs,
                "What hole size?",
                provider="fixture-provider",
                model="fixture-model",
                session_id="conversation-7",
            )
        self.assertEqual(msgs, before)
        self.assertIn("M5", answer)
        self.assertEqual(len(seen), 1)
        call = seen[0]
        self.assertEqual(call["role"], "general_agent")
        self.assertEqual(call["session_id"], "conversation-7")
        self.assertTrue(call["close_session"])
        self.assertEqual(call["max_tokens"], 2048)
        self.assertEqual(call["max_total_tokens"], 4096)
        self.assertEqual(call["options"]["allowed_tools"], [])
        self.assertEqual(
            call["options"]["settings"],
            {"provider": "fixture-provider", "model": "fixture-model"},
        )
        self.assertIn("<btw>", call["request"])
        self.assertIn("NEVER use tools", call["request"])
        self.assertEqual(call["history"], _history())

    def test_explicit_offline_btw_marks_role_turn_offline(self) -> None:
        from services.agent import chudcode_roles
        from services.agent import session_cmds as sc

        provider = _MockProvider("offline answer")
        seen: list[dict[str, Any]] = []

        def fake_turn(_request, _role, **kwargs):
            seen.append(kwargs)
            return {
                "success": True,
                "stop_reason": "confirmed",
                "result": {"handoff": {"summary": "offline answer"}},
            }

        with patch.object(chudcode_roles, "run_role_turn", side_effect=fake_turn):
            answer = sc.btw_answer(
                _history(),
                "status?",
                provider=provider,
                standalone=True,
            )
        self.assertEqual(answer, "offline answer")
        self.assertTrue(seen[0]["options"]["offline"])
        self.assertIs(seen[0]["options"]["offline_provider"], provider)

    def test_empty_question(self) -> None:
        from services.agent import session_cmds as sc

        self.assertEqual(sc.btw_answer(_history(), "  "), "")


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
        out = sc.compact_session(msgs, strategy="off", standalone=True)
        self.assertEqual(out["kept_messages"], msgs)
        self.assertIsNot(out["kept_messages"], msgs)

    def test_online_compaction_is_refused_without_touching_host_or_transcript(self) -> None:
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
            # Online conversations compact through the agent_admin_turn compaction run.
            with self.assertRaisesRegex(
                sc.SessionCommandUnavailable, "unavailable for online agent sessions"
            ):
                sc.compact_session(messages, strategy="summary")

        self.assertEqual(host.calls, [])
        self.assertEqual(messages, before)

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
            sc.SessionCommandUnavailable, "unavailable for online agent"
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

        # Worker RPC forwards the live Chudcode identity and explicit-local bit.
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
                    "session_id": "conversation-7",
                },
                lambda *a, **k: None,
            )
        self.assertTrue(r.get("ok"))
        self.assertIn("document", r)
        self.assertIn("## Goal", r["document"])
        self.assertEqual(_hd.call_args.kwargs["session_id"], "conversation-7")
        self.assertFalse(_hd.call_args.kwargs["standalone"])

        with patch.object(
            rpc._cmds(),
            "btw_answer",
            return_value="brief answer",
        ) as _btw:
            r2 = rpc.SESSION_METHODS["session_btw"](
                {
                    "messages": _history(),
                    "question": "status?",
                    "provider": "mock",
                    "model": "m",
                    "session_id": "conversation-7",
                },
                lambda *a, **k: None,
            )
        self.assertTrue(r2.get("ok"))
        self.assertEqual(r2["answer"], "brief answer")
        self.assertEqual(_btw.call_args.kwargs["session_id"], "conversation-7")
        self.assertFalse(_btw.call_args.kwargs["standalone"])

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


def test_cell_btw_carries_parent_scope_and_readonly_mode(monkeypatch):
    import hashlib
    from services.agent import chudcode_host, chudcode_roles, request_context as tenancy
    from services.agent import session_cmds

    actor = "btw-owner"
    ctx = {
        "cell": True, "workspace_key": "team:commands/work", "actor_key": actor,
        "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
        "right": "editor", "epoch": 1,
    }
    inherited = {"workspace_dir": "/work", "mode": "agent"}

    class ScopedHost:
        def session_launch_params(self, session_id):
            assert session_id == "parent"
            assert tenancy.owner_key() == (ctx["workspace_key"], actor)
            return dict(inherited)

    seen = []

    def role(request, role_name, **kwargs):
        seen.append(kwargs)
        return {"success": True, "result": "side answer"}

    monkeypatch.setattr(chudcode_host, "get_default_chudcode_host", lambda: ScopedHost())
    monkeypatch.setattr(chudcode_roles, "run_role_turn", role)
    with tenancy.bind_context(ctx):
        assert session_cmds.btw_answer([], "question", session_id="parent") == "side answer"
    assert seen[0]["options"]["workspace_dir"] == "/work"
    assert seen[0]["options"]["mode"] == "ask"
    assert seen[0]["close_session"]
