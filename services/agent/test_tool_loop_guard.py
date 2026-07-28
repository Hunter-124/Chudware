"""Tests for tool_loop_guard.ToolCallLoopGuard (ported from OMP's TS loop guard)."""
from __future__ import annotations

from services.agent.tool_loop_guard import ToolCallLoopGuard


class TestToolCallLoopGuard:
    def test_no_detection_below_threshold(self):
        g = ToolCallLoopGuard(threshold=3)
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        assert g.check("describe_part", {"doc_id": "d1"}) is None

    def test_detects_third_identical_call(self):
        g = ToolCallLoopGuard(threshold=3)
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        detection = g.check("describe_part", {"doc_id": "d1"})
        assert detection is not None
        assert detection.tool == "describe_part"
        assert detection.count == 3

    def test_different_args_resets_streak(self):
        g = ToolCallLoopGuard(threshold=3)
        assert g.check("fs_read", {"path": "a.py"}) is None
        assert g.check("fs_read", {"path": "b.py"}) is None
        assert g.check("fs_read", {"path": "a.py"}) is None
        # Streak reset each time args changed — never hit 3 in a row.

    def test_key_order_does_not_matter(self):
        g = ToolCallLoopGuard(threshold=2)
        assert g.check("describe_part", {"a": 1, "b": 2}) is None
        detection = g.check("describe_part", {"b": 2, "a": 1})
        assert detection is not None

    def test_different_tool_resets_streak(self):
        g = ToolCallLoopGuard(threshold=2)
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        assert g.check("measure_part", {"doc_id": "d1"}) is None
        assert g.check("describe_part", {"doc_id": "d1"}) is None

    def test_exempt_tool_never_flags(self):
        g = ToolCallLoopGuard(threshold=2)
        for _ in range(10):
            assert g.check("get_job", {"job_id": "j1"}) is None

    def test_message_mentions_tool_and_count(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("describe_part", {"doc_id": "d1"})
        detection = g.check("describe_part", {"doc_id": "d1"})
        msg = ToolCallLoopGuard.message(detection)
        assert "describe_part" in msg
        assert "2" in msg

    def test_message_never_suggests_the_tool_it_just_blocked(self):
        """Live 2026-07-27: haiku called ask_user with {} four times and the guard
        answered "change approach (replan/ask_user)" — recommending the very tool
        it had just refused, so the only advice on offer was the loop itself."""
        g = ToolCallLoopGuard(threshold=2)
        g.check("ask_user", {})
        msg = ToolCallLoopGuard.message(g.check("ask_user", {}))
        assert "replan" in msg
        # The blocked tool must not appear as an escape hatch.
        assert "/ask_user" not in msg and "ask_user)" not in msg

    def test_other_tools_still_get_ask_user_as_an_escape_hatch(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("fs_list", {"path": "."})
        msg = ToolCallLoopGuard.message(g.check("fs_list", {"path": "."}))
        assert "replan" in msg and "ask_user" in msg

    def test_empty_arguments_get_a_schema_specific_hint(self):
        """Repeating a call with NO arguments is a schema failure, not a retry:
        "try different arguments" alone leaves the model with nowhere to go."""
        g = ToolCallLoopGuard(threshold=2)
        g.check("ask_user", {})
        msg = ToolCallLoopGuard.message(g.check("ask_user", {}))
        assert "EMPTY" in msg
        assert "continue with what you already know" in msg

    def test_non_empty_arguments_omit_the_empty_hint(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("fs_read", {"path": "a.txt"})
        msg = ToolCallLoopGuard.message(g.check("fs_read", {"path": "a.txt"}))
        assert "EMPTY" not in msg


class TestAskUserEmptyArgs:
    """The loop the guard was firing on: ask_user called with no question text.

    A schema recital did not get haiku out of it — the tool now takes the shapes
    models actually write and, when it truly cannot parse one, says explicitly
    that retrying is wrong.
    """

    def test_empty_call_is_actionable_and_forbids_retry(self):
        from services.agent.agent_turn import _ask_user_sync
        r = _ask_user_sync()
        assert r["ok"] is False and r["status"] == "bad_arguments"
        assert "do NOT retry" in r["error"]
        assert "final message" in r["error"]      # the way out of needing the tool
        assert r["example"]                        # a copy-pasteable shape

    def test_question_under_an_alias_key_is_accepted(self):
        """Models put the question under whatever key they like; those used to
        land in **_ignored and the call looked argument-less."""
        from services.agent.agent_turn import _ask_user_sync
        for alias in ("message", "query", "prompt", "text", "q"):
            r = _ask_user_sync(**{alias: "Which material?"})
            assert r.get("ok") is True, f"{alias} not accepted"

    def test_blank_prompt_is_rejected_not_silently_asked(self):
        from services.agent.agent_turn import _ask_user_sync
        assert _ask_user_sync(questions=[{"id": "q1", "prompt": "   "}])["ok"] is False

    def test_mission_dispatch_ask_user_matches(self):
        from services.agent.mission_dispatch import MissionDispatch
        md = MissionDispatch.__new__(MissionDispatch)
        md.run_id, md.ledger, md.autonomy = "r1", None, None
        r = md.ask_user()
        assert r["ok"] is False and "do NOT retry" in r["error"]
        assert md.ask_user(message="Which material?").get("ok") is True
