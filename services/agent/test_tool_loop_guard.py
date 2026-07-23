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
