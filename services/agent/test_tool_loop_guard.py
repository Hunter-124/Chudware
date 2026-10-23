"""Tests for tool_loop_guard.ToolCallLoopGuard."""

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

    def test_time_varying_status_pollers_never_flag(self):
        for tool in ("sim_status", "fw_status", "scrape_queue_status"):
            guard = ToolCallLoopGuard(threshold=3)
            for _ in range(10):
                assert guard.check(tool, {"job_id": "same"}) is None

    def test_deterministic_describe_part_still_flags_third_call(self):
        guard = ToolCallLoopGuard(threshold=3)
        assert guard.check("describe_part", {"doc_id": "d1"}) is None
        assert guard.check("describe_part", {"doc_id": "d1"}) is None
        assert guard.check("describe_part", {"doc_id": "d1"}) is not None

    def test_coordinate_claim_intent_is_a_semantic_argument(self):
        guard = ToolCallLoopGuard(threshold=3)
        base = {"agent": "a1", "scope": ["part"], "__intent": "scratch"}
        for intent in ("inspect", "edit", "release"):
            assert guard.check("coordinate_claim", {**base, "intent": intent}) is None

    def test_message_mentions_tool_and_count(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("describe_part", {"doc_id": "d1"})
        detection = g.check("describe_part", {"doc_id": "d1"})
        msg = ToolCallLoopGuard.message(detection)
        assert "describe_part" in msg
        assert "2" in msg

    def test_message_names_only_universal_escape_routes(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("ask_user", {})
        msg = ToolCallLoopGuard.message(g.check("ask_user", {}))
        assert "prior result" in msg
        assert "real argument" in msg
        assert "different tool" in msg
        assert "status='blocked'" in msg
        assert "replan" not in msg
        assert "will not change" not in msg
        assert "did not change" not in msg

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


class TestValidationFailureGrace:
    """A repeat after a call that failed BEFORE reaching the tool body (arg
    validation / tool-binding) must not be loop-guarded on the first repeat —
    the model is reacting to the teaching error, which is exactly what we want
    it to do. A repeat after an identical SUCCESS is still refused at the
    normal threshold. A model that never fixes its arguments is still
    eventually cut off (bounded grace, not unlimited)."""

    def test_repeat_after_validation_failure_is_allowed_through(self):
        """Live 2026-07-27: ask_user({}) failed argument validation, and the
        very next identical attempt was refused by the guard before the model
        ever saw the real teaching error. That must no longer happen."""
        g = ToolCallLoopGuard(threshold=3)
        assert g.check("ask_user", {}) is None  # call #1
        g.note_outcome("ask_user", {}, failed_before_body=True)  # failed validation
        assert g.check("ask_user", {}) is None  # call #2 - below threshold anyway
        g.note_outcome("ask_user", {}, failed_before_body=True)
        # call #3 would trip a plain threshold=3 guard; it must not here.
        assert g.check("ask_user", {}) is None

    def test_repeat_after_identical_success_is_still_refused(self):
        g = ToolCallLoopGuard(threshold=3)
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        g.note_outcome("describe_part", {"doc_id": "d1"}, failed_before_body=False)
        assert g.check("describe_part", {"doc_id": "d1"}) is None
        g.note_outcome("describe_part", {"doc_id": "d1"}, failed_before_body=False)
        detection = g.check("describe_part", {"doc_id": "d1"})
        assert detection is not None
        assert detection.count == 3
        assert detection.after_validation_failure is False

    def test_genuinely_stuck_model_is_eventually_cut_off(self):
        """Grace is bounded — repeating validation failures forever must still
        trip the guard once the (threshold + validation_retry_allowance) bound
        is reached."""
        g = ToolCallLoopGuard(threshold=3, validation_retry_allowance=2)
        detection = None
        for _ in range(10):
            detection = g.check("ask_user", {})
            if detection is not None:
                break
            g.note_outcome("ask_user", {}, failed_before_body=True)
        assert detection is not None, (
            "a model stuck failing validation must still be cut off"
        )
        assert detection.count == 5  # threshold(3) + allowance(2)
        assert detection.after_validation_failure is True

    def test_message_names_the_validation_failure_case(self):
        g = ToolCallLoopGuard(threshold=2, validation_retry_allowance=1)
        g.check("ask_user", {})
        g.note_outcome("ask_user", {}, failed_before_body=True)
        g.check("ask_user", {})
        g.note_outcome("ask_user", {}, failed_before_body=True)
        detection = g.check("ask_user", {})
        assert detection is not None
        msg = ToolCallLoopGuard.message(detection)
        assert "argument validation" in msg
        assert "extra retry slack" in msg
        assert "immediately preceding" in msg

    def test_note_outcome_ignores_a_stale_key(self):
        """A call for a (tool, args) that is no longer the active streak (the
        model already moved on) must not corrupt the current streak."""
        g = ToolCallLoopGuard(threshold=3)
        g.check("fs_read", {"path": "a.py"})
        g.check("fs_read", {"path": "b.py"})  # streak moved on
        g.note_outcome("fs_read", {"path": "a.py"}, failed_before_body=True)  # stale
        g.check("fs_read", {"path": "b.py"})
        g.note_outcome("fs_read", {"path": "b.py"}, failed_before_body=False)
        detection = g.check("fs_read", {"path": "b.py"})
        assert detection is not None
        assert detection.after_validation_failure is False

    def test_note_outcome_is_a_noop_for_exempt_tools(self):
        g = ToolCallLoopGuard(threshold=2)
        g.check("get_job", {"job_id": "j1"})
        g.note_outcome("get_job", {"job_id": "j1"}, failed_before_body=True)
        for _ in range(10):
            assert g.check("get_job", {"job_id": "j1"}) is None


class TestExemptTools:
    def test_verify_battery_repeats_are_guarded(self):
        """verify_battery is deliberately NOT exempt: an identical repeat loops."""
        g = ToolCallLoopGuard(threshold=2)
        assert g.check("verify_battery", {"doc_id": "d1"}) is None
        detection = g.check("verify_battery", {"doc_id": "d1"})
        assert detection is not None
        assert detection.tool == "verify_battery"

    def test_swept_poller_set_is_pinned(self):
        import services.agent.tool_loop_guard as mod

        assert mod.DEFAULT_EXEMPT_TOOLS == frozenset(
            {
                "box_status",
                "buildgraph_status",
                "dag_status",
                "ecad_status",
                "em_status",
                "fw_status",
                "get_job",
                "intake_status",
                "ledger_events",
                "ledger_get",
                "mission_status",
                "plm_status",
                "reveng_gate_status",
                "reveng_status",
                "scrape_queue_status",
                "sim_jobs",
                "sim_status",
                "vendor_status",
                "workspace_doc_events",
                "workspace_status",
            }
        )
        assert "req_status" not in mod.DEFAULT_EXEMPT_TOOLS


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
        assert "final message" in r["error"]  # the way out of needing the tool
        assert r["example"]  # a copy-pasteable shape

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


class TestDispatchToolLoopGuardWiring:
    """End-to-end through the REAL dispatch path (_dispatch_tool), not just the
    guard in isolation — this is where note_outcome is actually threaded in."""

    def test_ask_user_empty_args_retries_see_the_real_teaching_error(self):
        """Live 2026-07-27: ask_user({}) failed argument validation and the
        loop guard refused the model's very next attempt, so it never saw the
        "do NOT retry" teaching text at all. Now: repeats past threshold=3
        keep returning the real ask_user payload (thanks to the validation
        grace) until the bound (threshold + allowance) is hit."""
        from services.agent.agent_turn import _dispatch_tool
        from services.agent.tool_loop_guard import ToolCallLoopGuard

        guard = ToolCallLoopGuard(threshold=3, validation_retry_allowance=2)
        counts: dict = {}
        for _ in range(4):  # threshold(3) + allowance(2) - 1: still real payloads
            r = _dispatch_tool("ask_user", {}, _call_counts=counts, _loop_guard=guard)
            assert r["ok"] is False and r["status"] == "bad_arguments"
            assert "do NOT retry" in r["error"]
        refusal = _dispatch_tool("ask_user", {}, _call_counts=counts, _loop_guard=guard)
        assert refusal == {
            "ok": False,
            "status": "loop_guard_refused",
            "policy_refusal": True,
            "executed": False,
            "error": refusal["error"],
        }
        assert "LOOP GUARD" in refusal["error"]
        assert "argument validation" in refusal["error"]
        assert "replan" not in refusal["error"]

    def test_repeated_identical_success_still_trips_at_threshold(
        self, tmp_path, monkeypatch
    ):
        """A tool that succeeds identically N times in a row (nothing wrong
        with its arguments) gets NO grace — that is the real waste this guard
        exists to stop.

        fs_list needs a real workspace root, so the test provides one: without
        it the tool returned ok:false/workspace_root_unavailable on any box
        that had not exported CHUDWARE_FS_ROOT, and this gate failed for a
        reason that had nothing to do with the loop guard.
        """
        from services.agent.agent_turn import _dispatch_tool
        from services.agent.tool_loop_guard import ToolCallLoopGuard

        monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
        guard = ToolCallLoopGuard(threshold=3)
        counts: dict = {}
        for _ in range(2):
            r = _dispatch_tool(
                "fs_list", {"path": "."}, _call_counts=counts, _loop_guard=guard
            )
            assert r.get("ok") is True and "status" not in r, r
        refusal = _dispatch_tool(
            "fs_list",
            {"path": "."},
            _call_counts=counts,
            _loop_guard=guard,
        )
        assert refusal["ok"] is False
        assert refusal["status"] == "loop_guard_refused"
        assert refusal["policy_refusal"] is True
        assert refusal["executed"] is False
        assert "LOOP GUARD" in refusal["error"]
        assert "argument validation" not in refusal["error"]
        assert "will not change" not in refusal["error"]
        assert "replan" not in refusal["error"]
